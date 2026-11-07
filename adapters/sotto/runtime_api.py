"""Runtime boundary for Sotto's local conversation service.

The receiver's only runtime adapter. It talks to the explicitly configured
loopback service and never searches for another runtime's state or tools.
"""
import importlib.util
import json
import os
import re
import stat
import sys
from pathlib import Path


class UnsupportedRuntimeOperationError(RuntimeError):
    pass


_CLIENT_MODULE = None
_RUN_MODULE = None
_FIXED_FIVE_MODULE = None
_UNPORTED = (
    'general unattended skill run tool and approval parity',
    'personal routine scheduler activation',
    'unverified general runtime startup profile',
)


def _client_module():
    global _CLIENT_MODULE
    if _CLIENT_MODULE is None:
        path = Path(__file__).with_name('client.py')
        spec = importlib.util.spec_from_file_location('sotto_runtime_client', path)
        module = importlib.util.module_from_spec(spec)
        # dataclass resolves its module through sys.modules during execution.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _CLIENT_MODULE = module
    return _CLIENT_MODULE


def _client(timeout=60):
    return _client_module().ChatClient(
        os.environ.get('SOTTO_CHAT_RUNTIME_URL', ''),
        os.environ.get('SOTTO_CHAT_RUNTIME_TOKEN', ''), timeout=timeout)


def _feedback_projection(value):
    """Keep the established feedback shape independent of runtime health metadata."""
    unavailable = {'available': False}
    if isinstance(value, dict) and set(value) == {'available'} and value['available'] is False:
        return unavailable
    keys = {'available', 'scope', 'turn_sequence', 'age_ms', 'reply_accepted',
            'uncertain', 'stages'}
    def bounded(number):
        return type(number) is int and 0 <= number <= 86400000
    if (not isinstance(value, dict) or set(value) not in (keys, keys | {'work_accepted'}) or value['available'] is not True
            or value['scope'] != 'latest_fresh_turn'
            or type(value['turn_sequence']) is not int or not 0 < value['turn_sequence'] <= 9007199254740991
            or not bounded(value['age_ms']) or type(value['reply_accepted']) is not bool
            or ('work_accepted' in value and type(value['work_accepted']) is not bool)
            or type(value['uncertain']) is not bool or not isinstance(value['stages'], list)
            or len(value['stages']) != 3):
        return unavailable
    stage_keys = {'stage', 'outcome', 'elapsed_ms', 'step', 'lookup_ms', 'effect_ms',
                  'error_class', 'late_outcome'}
    for stage, name in zip(value['stages'], ('read', 'processing', 'completion')):
        if (not isinstance(stage, dict) or set(stage) != stage_keys or stage['stage'] != name
                or stage['outcome'] not in ('not_started', 'running', 'success', 'timeout', 'error', 'skipped')
                or stage['step'] not in (None, 'lookup', 'effect')
                or stage['error_class'] not in (None, 'target_unavailable', 'provider_unavailable', 'provider_error')
                or stage['late_outcome'] not in (None, 'success', 'error')
                or any(stage[key] is not None and not bounded(stage[key])
                       for key in ('elapsed_ms', 'lookup_ms', 'effect_ms'))):
            return unavailable
    return {key: value[key] for key in (keys | ({'work_accepted'} if 'work_accepted' in value else set()))}


def _chat_timing_projection(value):
    keys = ('sourceCheckMs', 'standingContextMs', 'toolSetupMs', 'generationMs',
            'finalSourceCheckMs', 'totalMs')
    optional = ('queueMs', 'promptPreparationMs')
    if (not isinstance(value, dict) or not set(keys).issubset(value)
            or not set(value).issubset((*keys, *optional))
            or any(type(number) is not int or not 0 <= number <= 300000 for number in value.values())):
        return None
    return {key: value[key] for key in (*keys, *optional) if key in value}


def _model_timing_projection(value):
    """Sum existing bounded samples; parallel tools can overlap, so not wall time.

    The producer retains at most 60 steps and 256 tool samples. Recorded counts
    make that limit visible; processor retries are not HTTP/provider retries.
    """
    def bounded(number, maximum=1000000000):
        return type(number) is int and 0 <= number <= maximum
    steps, tools = value.get('steps'), value.get('tools')
    if (not bounded(value.get('stepCount')) or not isinstance(steps, list) or len(steps) > 60
            or value['stepCount'] < len(steps) or not isinstance(tools, list) or len(tools) > 256):
        return None
    for step in steps:
        if (not isinstance(step, dict) or set(step) != {
                'durationMs', 'modelMs', 'inputTokens', 'outputTokens', 'finishReason', 'processorRetries'}
                or any(not bounded(step[key], 300000) for key in ('durationMs', 'modelMs'))
                or any(not bounded(step[key]) for key in ('inputTokens', 'outputTokens', 'processorRetries'))
                or step['finishReason'] not in ('stop', 'length', 'tool-calls', 'content-filter', 'error', 'other', 'unknown')):
            return None
    for tool in tools:
        if (not isinstance(tool, dict) or set(tool) != {'tool', 'durationMs', 'outcome'}
                or not isinstance(tool['tool'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', tool['tool'])
                or not bounded(tool['durationMs'], 300000) or tool['outcome'] not in ('success', 'failed')):
            return None
    return {'step_count': value['stepCount'], 'recorded_steps': len(steps), 'recorded_tools': len(tools),
            'model_ms': sum(step['modelMs'] for step in steps),
            'tool_ms': sum(tool['durationMs'] for tool in tools),
            'processor_retries': sum(step['processorRetries'] for step in steps)}


def _model_context_projection(value):
    """First model-step history counts and process-local purge diagnostics; no source text."""
    allowed = {'version', 'mode', 'outcome', 'completedAtMs', 'standingChars', 'promptBytes',
               'toolCount', 'toolSchemaBytes', 'schemaUnavailable', 'stepCount', 'steps', 'tools',
               'rememberedHistory', 'lastMemoryPurge', 'failure'}
    if (not isinstance(value, dict) or not set(value) <= allowed or value.get('version') != 1
            or value.get('mode') != 'chat' or value.get('outcome') not in ('success', 'failed')):
        return None
    def bounded(number, maximum=1000000000):
        return type(number) is int and 0 <= number <= maximum
    if not bounded(value.get('completedAtMs'), 9007199254740991) or not bounded(value.get('promptBytes')):
        return None
    result = {'completed_at_ms': value['completedAtMs'], 'prompt_bytes': value['promptBytes']}
    timing = _model_timing_projection(value)
    if timing is not None:
        result['timing'] = timing
    if 'failure' in value:
        failure = value['failure']
        if (not isinstance(failure, dict) or set(failure) not in ({'category'}, {'category', 'httpStatus'})
                or failure.get('category') not in ('provider_api', 'provider_unavailable', 'budget_exhausted',
                    'admission_busy', 'request_too_large', 'aborted', 'invalid_output', 'invalid_tool_call',
                    'tool_error', 'source_authority', 'unknown')
                or 'httpStatus' in failure and (type(failure['httpStatus']) is not int
                                                or not 100 <= failure['httpStatus'] <= 599)):
            return None
        result['failure'] = {'category': failure['category'],
            **({'http_status': failure['httpStatus']} if 'httpStatus' in failure else {})}
    if 'rememberedHistory' in value:
        history = value['rememberedHistory']
        if (not isinstance(history, dict) or set(history) != {'users', 'assistants'}
                or not all(bounded(n) for n in history.values())):
            return None
        result['remembered_history'] = dict(history)
    if 'lastMemoryPurge' in value:
        purge = value['lastMemoryPurge']
        if (not isinstance(purge, dict) or set(purge) != {'reason', 'atMs'}
                or purge['reason'] not in ('authority_recovered', 'authority_unavailable', 'baseline_unavailable',
                                          'revision_changed', 'authority_changed_during_turn')
                or not bounded(purge['atMs'], 9007199254740991)):
            return None
        result['last_memory_purge'] = {'reason': purge['reason'], 'at_ms': purge['atMs']}
    return result


_diagnostic_path = Path(__file__).with_name('google_read_contract.py')
_diagnostic_spec = importlib.util.spec_from_file_location('sotto_google_read_contract', _diagnostic_path)
_GOOGLE_READ_DIAGNOSTICS = importlib.util.module_from_spec(_diagnostic_spec)
_diagnostic_spec.loader.exec_module(_GOOGLE_READ_DIAGNOSTICS)
_GOOGLE_READ_FAILURE_STAGES = _GOOGLE_READ_DIAGNOSTICS.STAGES


def _task_tool_diagnostics_projection(value):
    if (not isinstance(value, dict) or set(value) != {'events', 'completedAtMs'}
            or type(value['completedAtMs']) is not int
            or not 0 <= value['completedAtMs'] <= 9007199254740991
            or not isinstance(value['events'], list) or len(value['events']) > 8):
        return None
    events = []
    for event in value['events']:
        if not isinstance(event, dict):
            return None
        keys = {'tool', 'status', 'error'} | ({'script'} if event.get('tool') == 'run_sotto_script' else set())
        cleanup_keys = {'cleanup_stage', 'cleanup_category', 'cleanup_http_status'} & set(event)
        if cleanup_keys:
            status = event.get('cleanup_http_status')
            if (event.get('tool') != 'owner_task_cleanup' or event.get('error') != 'tool_error'
                    or event.get('cleanup_stage') not in ('transport', 'response', 'receiver_perform', 'receiver_serialize', 'writer')
                    or event.get('cleanup_category') not in ('network', 'aborted', 'http', 'invalid_response', 'unavailable', 'io', 'type', 'import', 'edit', 'other')
                    or 'cleanup_http_status' in event and (type(status) is not int or not 100 <= status <= 599)):
                return None
            keys |= cleanup_keys
        if 'google_read_stage' in event:
            if (event.get('tool') not in ('gmail_get', 'gmail_search', 'gmail_thread_get') or event.get('error') != 'tool_error'
                    or not isinstance(event['google_read_stage'], str)
                    or event['google_read_stage'] not in _GOOGLE_READ_FAILURE_STAGES):
                return None
            keys.add('google_read_stage')
        if 'google_read_http_status' in event or 'google_read_phase' in event:
            status, phase = event.get('google_read_http_status'), event.get('google_read_phase')
            if ('google_read_stage' not in event
                    or 'google_read_http_status' in event and (not _GOOGLE_READ_DIAGNOSTICS.http_status(status))
                    or 'google_read_phase' in event and not (
                        _GOOGLE_READ_DIAGNOSTICS.phase_matches(event.get('tool'), phase))):
                return None
            keys |= {key for key in ('google_read_http_status', 'google_read_phase') if key in event}
        if 'rejection' in event:
            if (event.get('tool') not in ('owner_task_list', 'owner_task_add', 'owner_task_cleanup', 'owner_task_close', 'owner_task_resolve_many') or event.get('status') != 'rejected'
                    or type(event['rejection']) is not str or event['rejection'] not in (
                        'event_conflict', 'target_unavailable', 'owner_span_unavailable', 'owner_anchor_collision',
                        'target_changed', 'source_unavailable', 'correction_unavailable', 'anchor_collision',
                        'identifier_collision', 'similar_fact_exists')):
                return None
            keys.add('rejection')
        if (set(event) != keys
                or event['tool'] not in ('owner_task_list', 'owner_task_add', 'owner_task_cleanup', 'owner_task_close', 'owner_task_resolve_many', 'run_sotto_script', 'gmail_get', 'gmail_search', 'gmail_thread_get', 'read_writing_style', 'bridge_search_contacts')
                or event['status'] not in ('recorded', 'rejected', 'proposed', 'needs_selection', 'needs_clarification', 'unknown')
                or event['error'] not in ('none', 'tool_error', 'input_validation', 'result_unavailable')
                or (event['tool'] == 'run_sotto_script' and event['script'] not in ('loops-query', 'knowledge-edit', 'retune-apply', 'knowledge-query', 'style-apply', 'unknown'))):
            return None
        events.append({key: event[key] for key in keys})
    return {'events': events, 'completed_at_ms': value['completedAtMs']}


def runtime_observation():
    """One bounded local read for authenticated diagnostics, never a delivery gate."""
    unavailable = {'feedback': {'available': False}, 'runtime_health': {'status': 'read_failed'}}
    try:
        result = _client(timeout=1).call('health', response_limit=16384, strict_json=True)
        if not result.ok:
            return unavailable
        value = result.value
        if not isinstance(value, dict):
            return {'feedback': {'available': False}, 'runtime_health': {'status': 'invalid_response'}}
        raw_feedback = value.get('feedback')
        feedback = _feedback_projection(raw_feedback)
        no_fresh_turn = (isinstance(raw_feedback, dict) and set(raw_feedback) == {'available'}
                         and raw_feedback['available'] is False)
        if (value.get('runtime') != 'sotto' or type(value.get('ready')) is not bool
                or value.get('providerLifecycle') not in ('starting', 'active', 'failed', 'stopped', 'not_managed')
                or value.get('providerConnection') != 'not_observed'):
            return {'feedback': feedback, 'runtime_health': {'status': 'invalid_response'}}
        task_tools = _task_tool_diagnostics_projection(value.get('taskToolDiagnostics'))
        timing = _chat_timing_projection(value.get('chatTiming'))
        model_context = _model_context_projection(value.get('modelMetrics'))
        intake = value.get('providerIntake')
        intake_keys = {'receivedEvents', 'handedOffEvents', 'listenerStarts',
                       'listenerStops', 'streamErrors', 'handlerErrors'}
        valid_intake = (isinstance(intake, dict) and set(intake) <= intake_keys
                        and {'receivedEvents', 'handedOffEvents'} <= set(intake)
                        and all(type(v) is int and 0 <= v <= 9007199254740991
                                for v in intake.values()))
        completed_at_ms = value.get('chatTimingCompletedAtMs')
        outcome = value.get('chatTimingOutcome')
        valid_timing = (timing is not None and type(completed_at_ms) is int
                        and 0 <= completed_at_ms <= 9007199254740991)
        return {'feedback': feedback, 'runtime_health': {
            'status': 'observed', 'ready': value['ready'],
            **({'model_context': model_context} if model_context is not None else {}),
            **({'task_tool_diagnostics': task_tools} if task_tools is not None else {}),
            'provider_lifecycle': value['providerLifecycle'], 'provider_connection': 'not_observed',
            **({'provider_intake': intake} if valid_intake else {}),
            'feedback_status': ('available' if feedback['available'] else
                                'no_fresh_turn' if no_fresh_turn else
                                'invalid_response'),
            **({'chat_timing': timing, 'chat_timing_completed_at_ms': completed_at_ms,
                **({'chat_timing_outcome': outcome} if outcome in ('success', 'failed') else {})}
               if valid_timing else {})}}
    except Exception:  # noqa: BLE001  # Observation never blocks the dashboard.
        return unavailable


def feedback_status():
    """Legacy feedback-only projection; never a readiness or provider probe."""
    return runtime_observation()['feedback']


def send_capability():
    """Check the configured interface without treating HTTP liveness as capability."""
    try:
        _client(timeout=5)
    except (ValueError, OSError):
        return False, 'Sotto chat loopback URL and private bearer are not configured'
    return True, ''


def runtime_readiness():
    """The receiver may not start in Sotto mode until all invoked contracts exist."""
    capable, guidance = send_capability()
    if not capable:
        return False, guidance
    if os.environ.get('SOTTO_RUNTIME_PROFILE') in (
            'managed-photon-fixed-five-v1', 'self-host-telegram-fixed-five-v1',
            'self-host-photon-fixed-five-v1'):
        try:
            profile = _fixed_five_module()
            # The trusted root supervisor admits first launch only with empty
            # history and verifies exact epoch lineage on reentry. The receiver
            # then retains supported operational rows for per-record recovery.
            profile.validate_offline(os.environ, allow_supported_history=True)
            profile.verify_configured_channel(os.environ, _client(timeout=1), timeout=30)
        except (ValueError, RuntimeError, OSError, TypeError, ImportError):
            if os.environ.get('SOTTO_RUNTIME_PROFILE') == 'managed-photon-fixed-five-v1':
                return False, 'managed Photon fixed procedure profile unavailable'
            if os.environ.get('SOTTO_RUNTIME_PROFILE') == 'self-host-photon-fixed-five-v1':
                return False, 'self-host Photon fixed procedure profile unavailable'
            return False, 'self-host Telegram fixed procedure profile unavailable'
        return True, ''
    return False, 'Sotto receiver selection is not ready: ' + '; '.join(_UNPORTED)


def _fixed_five_module():
    global _FIXED_FIVE_MODULE
    if _FIXED_FIVE_MODULE is None:
        path = Path(__file__).with_name('fixed_five.py')
        spec = importlib.util.spec_from_file_location('sotto_fixed_five', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _FIXED_FIVE_MODULE = module
    return _FIXED_FIVE_MODULE


def provisioning_status():
    """Prove the loaded private channel without requiring the owner's first DM.

    This is installation readiness, not delivery, model, source or activation
    evidence. The receiver has already passed the supervisor's startup gate;
    recheck Node now so a stale receiver cannot admit an unavailable runtime.
    """
    profile = _fixed_five_module()
    if os.environ.get('SOTTO_RUNTIME_PROFILE') != profile.PROFILE:
        raise UnsupportedRuntimeOperationError('managed runtime profile unavailable')
    profile.verify_configured_channel(os.environ, _client(timeout=1), timeout=0)
    return {'name': 'sotto', 'profile': profile.PROFILE, 'configured': True}


def selfhost_provisioning_status():
    """Recheck the loaded self-host identity without a model/provider request.

    This is configuration evidence, not a credential-quality or delivery probe.
    Never let a managed receiver expose the root-bearer self-host setup surface.
    """
    profile = _fixed_five_module()
    selected = os.environ.get('SOTTO_RUNTIME_PROFILE')
    channels = {profile.TELEGRAM_PROFILE: 'telegram',
                profile.SELF_HOST_PHOTON_PROFILE: 'photon'}
    if os.environ.get('SOTTO_DEPLOYMENT_MODE') != 'self_host' or selected not in channels:
        raise UnsupportedRuntimeOperationError('self-host runtime profile unavailable')
    profile.validate_environment(os.environ)
    profile.verify_configured_channel(os.environ, _client(timeout=1), timeout=0)
    return {'channel': channels[selected],
            'model_configured': bool(os.environ.get('GOOGLE_AI_API_KEY'))}


def validate_fixed_work(kind, payload):
    """Same closed request guard at admission, recovery and immediately before exec."""
    if os.environ.get('SOTTO_RUNTIME_PROFILE') in (
            'managed-photon-fixed-five-v1', 'self-host-telegram-fixed-five-v1',
            'self-host-photon-fixed-five-v1'):
        _fixed_five_module().validate_work(kind, payload, os.environ)


def send_receipt(body, target, timeout=60, dispatch_id=None, attempt=1, *, source_revision=None):
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return False, 'stable dispatch identity required', {'acceptance': 'not_attempted', 'target': target}
    return _client(timeout).send_receipt(body, target, dispatch_id, attempt=attempt, **({'source_revision': source_revision} if source_revision is not None else {}))


def gallery_available():
    return send_capability()[0]


def _read_private_gallery(paths, data_root):
    """Read only the four renderer-owned ordered PNGs as the receiver UID."""
    if not isinstance(paths, list) or len(paths) != 4 or not all(isinstance(path, str) for path in paths):
        raise ValueError('invalid gallery paths')
    root = Path(data_root)
    if not root.is_absolute():
        raise ValueError('invalid gallery root')
    artifact = Path(paths[0]).parent.name
    if re.fullmatch(r'[a-f0-9]{24}', artifact) is None:
        raise ValueError('invalid gallery artifact')
    base = root / 'cache' / 'visual-briefs' / artifact
    if any(Path(path) != base / f'{index:02d}.png' for index, path in enumerate(paths, 1)):
        raise ValueError('invalid gallery paths')
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        current = os.open(root, directory_flags)
        descriptors.append(current)
        root_meta = os.fstat(current)
        selected_shared_root = (
            os.environ.get('SOTTO_RUNTIME_PROFILE') in (
                'managed-photon-fixed-five-v1', 'self-host-telegram-fixed-five-v1',
                'self-host-photon-fixed-five-v1')
            and root_meta.st_uid == 0 and root_meta.st_gid == os.getegid()
            and stat.S_IMODE(root_meta.st_mode) == 0o1771
        )
        if (not stat.S_ISDIR(root_meta.st_mode) or not selected_shared_root
                and (root_meta.st_uid != os.geteuid() or root_meta.st_mode & 0o022)):
            raise ValueError('gallery root is not protected')
        for name in ('cache', 'visual-briefs', artifact):
            current = os.open(name, directory_flags, dir_fd=current)
            descriptors.append(current)
            meta = os.fstat(current)
            if meta.st_uid != os.geteuid() or meta.st_mode & 0o022:
                raise ValueError('gallery directory is not protected')
        if stat.S_IMODE(os.fstat(current).st_mode) != 0o700:
            raise ValueError('gallery artifact is not private')
        images = []
        for index in range(1, 5):
            name = f'{index:02d}.png'
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
            try:
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                        or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                        or not 0 < before.st_size <= 5_000_000):
                    raise ValueError('gallery image is not private')
                image = os.read(fd, 5_000_001)
                after = os.fstat(fd)
                current_file = os.stat(name, dir_fd=current, follow_symlinks=False)
                if (len(image) != before.st_size or
                        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                         before.st_ctime_ns) !=
                        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                         after.st_ctime_ns) or
                        (before.st_dev, before.st_ino) !=
                        (current_file.st_dev, current_file.st_ino) or
                        not image.startswith(b'\x89PNG\r\n\x1a\n')):
                    raise ValueError('gallery image changed or is not PNG')
                images.append({'name': name, 'bytes': image})
            finally:
                os.close(fd)
        return images
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def send_gallery(presentation, target, dispatch_id, timeout=60):
    if not isinstance(presentation, dict) or not isinstance(presentation.get('images'), list) \
            or not isinstance(presentation.get('summary'), str):
        return False, 'gallery payload invalid', {'acceptance': 'not_attempted'}
    try:
        images = _read_private_gallery(presentation['images'], os.environ.get('SOTTO_DATA', '/data'))
    except (OSError, ValueError, TypeError):
        return False, 'gallery files unavailable', {'acceptance': 'not_attempted'}
    return _client(timeout).send_gallery_bytes(images, presentation['summary'], target, dispatch_id)


def gallery_receipt(dispatch_id, timeout=5):
    return _client(timeout).receipt(dispatch_id)


def activation_evidence(timeout=3):
    """Read only Node's durable owner-DM evidence; this grants nothing by itself."""
    return _client(timeout).activation_evidence()


def activation_ack(event_id, timeout=3):
    """Acknowledge only after the receiver's existing Photon writer has succeeded."""
    return _client(timeout).activation_ack(event_id)


def activation_state(expected_config, timeout=3):
    """Read authenticated Node admission/ACK state for an explicit rendered config.

    This is read-only and does not infer activation from static configured
    identity. The receiver must separately verify its current owner policy and
    sole Photon receipt before treating an acknowledged binding as usable.
    """
    return _client(timeout).activation_state(expected_config)


def _unsupported(name):
    raise UnsupportedRuntimeOperationError(f'Sotto runtime does not implement receiver operation: {name}')


def run_argv(runner, prompt):
    """The receiver's declared procedure runner with its one request argument."""
    argv = list(runner)
    if not argv:
        raise ValueError('A procedure runner is required')
    return [*argv, prompt]


def read_usage(path=None):
    global _RUN_MODULE
    if _RUN_MODULE is None:
        spec = importlib.util.spec_from_file_location('sotto_unattended_run', Path(__file__).with_name('run.py'))
        _RUN_MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_RUN_MODULE)
    return _RUN_MODULE.read_usage(path)


def home_path(name=''):
    """Use only the explicitly selected Google credential home in Sotto mode."""
    selected = os.environ.get('SOTTO_HOME')
    if (not isinstance(selected, str) or not selected or not Path(selected).is_absolute()
            or '\x00' in selected or any(part in ('.', '..') for part in selected.split('/'))):
        raise ValueError('explicit SOTTO_HOME required for Sotto credentials')
    root = Path(selected)
    return str(root / name)


def telegram_bot_token(*_args, **_kwargs):
    """Return only the bot credential bound to Sotto's private, captured Telegram link.

    This deliberately does not read TELEGRAM_BOT_TOKEN, a dotenv file or HOME. The
    existing telegram_link.py capture is the authority: it records the bot token and the
    private-chat owner together after the pairing phrase succeeds.
    """
    if (os.environ.get('SOTTO_CRON_DELIVER') or '').strip() != 'telegram':
        return ''
    data_root = os.environ.get('SOTTO_DATA')
    if (not data_root or len(data_root) > 4096 or not os.path.isabs(data_root)
            or '\x00' in data_root):
        return ''
    record = _read_telegram_link(data_root)
    if record is None:
        return ''
    token = record.get('bot_token')
    owner = record.get('allowed_user')
    if not _valid_telegram_token(token) or type(owner) is not int or owner <= 0 or owner > 9007199254740991:
        return ''
    # If deployment configuration also names the recipient, it must corroborate the
    # captured owner exactly. A mismatch means stale/ambiguous configuration, not a link.
    for variable in ('TELEGRAM_ALLOWED_USERS', 'TELEGRAM_HOME_CHANNEL'):
        configured = os.environ.get(variable)
        if configured is not None and configured.strip() != str(owner):
            return ''
    return token


def _valid_telegram_token(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or value.strip() != value:
        return False
    try:
        value.encode('ascii')
    except UnicodeEncodeError:
        return False
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        return False
    bot_id, separator, secret = value.partition(':')
    return (separator == ':' and bot_id.isascii() and bot_id.isdigit()
            and 1 <= len(bot_id) <= 15 and len(secret) >= 20
            and all(char.isascii() and (char.isalnum() or char in '_-') for char in secret))


def _read_telegram_link(data_root):
    """Read the captured owner-only link through a stable data-directory descriptor.

    The record's existence is persisted configuration, not fresh pairing proof or proof
    that the selected Node bot currently has the same credential.
    """
    limit = 16 * 1024
    base_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    directory_flags = base_flags | getattr(os, 'O_DIRECTORY', 0)
    root_fd = None
    file_fd = None
    try:
        root_fd = os.open(data_root, directory_flags)
        root_before = os.fstat(root_fd)
        root_mode = stat.S_IMODE(root_before.st_mode)
        owner_uid = os.geteuid()
        trusted_user_root = root_before.st_uid == owner_uid and not root_mode & 0o022
        trusted_sticky_root = root_before.st_uid == 0 and bool(root_mode & stat.S_ISVTX)
        if (not stat.S_ISDIR(root_before.st_mode)
                or not (trusted_user_root or trusted_sticky_root)):
            os.close(root_fd)
            root_fd = None
            return None
        root_path_before = os.stat(data_root, follow_symlinks=False)
        if (not stat.S_ISDIR(root_path_before.st_mode)
                or (root_path_before.st_dev, root_path_before.st_ino)
                != (root_before.st_dev, root_before.st_ino)
                or not _same_root_permissions(root_before, root_path_before)):
            os.close(root_fd)
            root_fd = None
            return None
        file_fd = os.open('telegram-link.json', base_flags, dir_fd=root_fd)
    except (OSError, ValueError):
        if root_fd is not None:
            os.close(root_fd)
        return None
    try:
        before = os.fstat(file_fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != owner_uid
                or before.st_mode & 0o077 or before.st_nlink != 1 or before.st_size > limit):
            return None
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(file_fd, min(4096, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit:
            return None
        after = os.fstat(file_fd)
        file_path_after = os.stat('telegram-link.json', dir_fd=root_fd, follow_symlinks=False)
        root_after = os.fstat(root_fd)
        root_path_after = os.stat(data_root, follow_symlinks=False)
        if (not _stable_file_metadata(before, after)
                or (file_path_after.st_dev, file_path_after.st_ino) != (after.st_dev, after.st_ino)
                or (root_after.st_dev, root_after.st_ino) != (root_before.st_dev, root_before.st_ino)
                or not _same_root_permissions(root_before, root_after)
                or (root_path_after.st_dev, root_path_after.st_ino) != (root_before.st_dev, root_before.st_ino)
                or not _same_root_permissions(root_before, root_path_after)):
            return None

        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate link field')
                result[key] = value
            return result

        record = json.loads(bytes(raw).decode('utf-8'), object_pairs_hook=unique_keys)
        if not isinstance(record, dict) or set(record) - {
                'bot_token', 'allowed_user', 'linked_at', 'bot_username', 'bot_id'}:
            return None
        return record
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if root_fd is not None:
            os.close(root_fd)


def _stable_file_metadata(before, after):
    fields = ('st_dev', 'st_ino', 'st_uid', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    return all(getattr(before, field) == getattr(after, field) for field in fields)


def _same_root_permissions(before, after):
    return (before.st_uid == after.st_uid
            and stat.S_IMODE(before.st_mode) == stat.S_IMODE(after.st_mode))


def google_setup_path(managed=False):
    # Sotto owns OAuth now; keep one authorized-user writer inside its runtime package.
    path = Path(__file__).with_name('google_setup.py').resolve()
    return str(path) if path.is_file() else None


def google_api_path():
    # Fixed packaged CLI; no install-tree search.
    path = Path(__file__).with_name('google_api.py').resolve()
    return str(path) if path.is_file() else None


def personal_routines():
    if not personal_routines_supported():
        _unsupported('personal_routines')
    path = Path(__file__).resolve().parents[2] / 'runtime/trigger-receiver/fresh_routines.py'
    if not path.exists():
        path = Path('/app/trigger-receiver/fresh_routines.py')
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('sotto_fresh_routine_projection', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.list_rows(module.load(os.environ['SOTTO_DATA']))


def personal_routines_supported():
    return (os.environ.get('SOTTO_RUNTIME_PROFILE') in
            ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1', 'self-host-telegram-fixed-five-v1')
            and re.fullmatch('[a-f0-9]{32}', os.environ.get('SOTTO_ROUTINE_EPOCH', '')) is not None)


def archive_sessions():
    """Rotate runtime chat sessions through the existing private loopback API."""
    try:
        result = _client(timeout=30).sessions('rotate')
        count = result.get('count') if isinstance(result, dict) else None
        return count if type(count) is int and count >= 0 else None
    except (ValueError, OSError, RuntimeError, TypeError):
        return None
