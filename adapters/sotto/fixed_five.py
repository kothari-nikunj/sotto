"""Closed receiver procedure profiles for the supervised Sotto handoff.

The receiver owns the five schedules. This module validates its persisted work
before startup and again immediately before a child can execute it. It never
turns model text into a script path or grants a personal-routine scheduler.
"""
from __future__ import annotations

import json
import importlib.util
from datetime import datetime
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time


class ProfileUnavailableError(ValueError):
    pass


PROFILE = 'managed-photon-fixed-five-v1'
TELEGRAM_PROFILE = 'self-host-telegram-fixed-five-v1'
SELF_HOST_PHOTON_PROFILE = 'self-host-photon-fixed-five-v1'
SCHEDULES = {
    'sotto-morning-brief': ('30 6 * * *', 'sotto-morning-brief', 'Run my morning brief', None, None),
    'sotto-evening-brief': ('30 17 * * *', 'sotto-evening-brief', 'Run my evening brief', None, None),
    'sotto-relationship-pulse': ('0 9 * * 1', 'sotto-relationship-pulse',
                                 'Run my relationship pulse', None, None),
    'sotto-proactive': ('*/15 * * * *', 'sotto-proactive', 'Run my proactive check',
                        'SOTTO_PROACTIVE', 'SOTTO_PROACTIVE_CRON'),
    'sotto-midday-digest': ('30 12 * * *', 'sotto-event', 'Run my midday digest',
                            'SOTTO_DIGEST', None),
}
_HEX24 = re.compile(r'[0-9a-f]{24}\Z')
_TELEGRAM_TOKEN = re.compile(r'[0-9]{1,15}:[A-Za-z0-9_-]{20,4095}\Z')
_ACTIVE = ('pending', 'ready', 'leased')


def _fail():
    raise ProfileUnavailableError('managed Photon fixed procedure profile unavailable')


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result
    def nonfinite(_):
        _fail()
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        _fail()


_PACKAGED_ROOT = Path('/app/sotto-skills')
_PACKAGED_RECEIVER = Path('/app/trigger-receiver')


def _image_owned(meta):
    return meta.st_uid == 0 and not meta.st_mode & 0o022


def _pack(env):
    """Select the image-owned skill tree, never a path supplied by work/model text."""
    root = env.get('SOTTO_SKILLS_ROOT')
    if root != str(_PACKAGED_ROOT):
        _fail()
    candidate = _PACKAGED_ROOT
    try:
        # The production image seals /app and its copied scripts as root-owned,
        # with no group/other write permission. Check each traversed directory.
        for directory in (candidate.parent, candidate):
            meta = directory.lstat()
            if (not stat.S_ISDIR(meta.st_mode) or not _image_owned(meta)):
                _fail()
    except OSError:
        _fail()
    return candidate


def validate_environment(env):
    """Accept the closed fixed-five deployments; refuse generic selectors."""
    profile = env.get('SOTTO_RUNTIME_PROFILE')
    telegram = profile == TELEGRAM_PROFILE
    selfhost_photon = profile == SELF_HOST_PHOTON_PROFILE
    if (profile not in (PROFILE, TELEGRAM_PROFILE, SELF_HOST_PHOTON_PROFILE)
            or env.get('SOTTO_DEPLOYMENT_MODE') != ('managed' if profile == PROFILE else 'self_host')
            or env.get('SOTTO_CRON_DELIVER') != ('telegram' if telegram else 'photon')):
        _fail()
    root, home = env.get('SOTTO_DATA'), env.get('SOTTO_HOME')
    if (not isinstance(root, str) or not root.startswith('/')
            or not isinstance(home, str) or home != root.rstrip('/') + '/sotto-home'
            or any(part in ('.', '..') for part in root.split('/'))):
        _fail()
    if not isinstance(env.get('SOTTO_TENANT_ID'), str) or not env['SOTTO_TENANT_ID']:
        _fail()
    if telegram:
        owner = env.get('TELEGRAM_HOME_CHANNEL')
        if (not isinstance(owner, str) or not re.fullmatch(r'[1-9][0-9]{0,15}', owner)
                or int(owner) > 9007199254740991
                or env.get('TELEGRAM_ALLOWED_USERS') != owner
                or not isinstance(env.get('TELEGRAM_BOT_TOKEN'), str)
                or _TELEGRAM_TOKEN.fullmatch(env['TELEGRAM_BOT_TOKEN']) is None
                or any(env.get(name) for name in ('PHOTON_PROJECT_ID',
                                                  'PHOTON_PROJECT_SECRET',
                                                  'SOTTO_MODEL_PROXY_URL',
                                                  'SOTTO_MODEL_PROXY_TOKEN'))):
            _fail()
        required = ('TELEGRAM_BOT_TOKEN', 'GOOGLE_AI_API_KEY')
    else:
        if (env.get('PHOTON_HOME_CHANNEL') != env.get('PHOTON_ALLOWED_USERS')
                or not isinstance(env.get('PHOTON_PROJECT_ID'), str)
                or not isinstance(env.get('PHOTON_PROJECT_SECRET'), str)
                or not env['PHOTON_PROJECT_ID'] or not env['PHOTON_PROJECT_SECRET']):
            _fail()
        if selfhost_photon:
            if (not isinstance(env.get('SOTTO_IMESSAGE_NUMBER'), str)
                    or not re.fullmatch(r'\+[1-9][0-9]{7,14}', env['SOTTO_IMESSAGE_NUMBER'])
                    or not isinstance(env.get('PHOTON_HOME_CHANNEL'), str)
                    or not re.fullmatch(r'\+[1-9][0-9]{7,14}', env['PHOTON_HOME_CHANNEL'])
                    or any(env.get(name) for name in ('SOTTO_MODEL_PROXY_URL',
                                                       'SOTTO_MODEL_PROXY_TOKEN',
                                                       'TELEGRAM_BOT_TOKEN'))):
                _fail()
            required = ('GOOGLE_AI_API_KEY',)
        else:
            if env.get('GOOGLE_AI_API_KEY'):
                _fail()
            required = ('SOTTO_MODEL_PROXY_URL', 'SOTTO_MODEL_PROXY_TOKEN')
    if any(not isinstance(env.get(name), str) or not env[name]
           for name in ('SOTTO_CHAT_RUNTIME_URL', 'SOTTO_CHAT_RUNTIME_TOKEN',
                        'SOTTO_MCP_TOKEN', *required)):
        _fail()
    if (env.get('PORT') is not None
            and env.get('PORT') != env.get('SOTTO_TRIGGER_PORT')):
        _fail()
    if (env.get('PORT') is None) != (env.get('RAILWAY_PUBLIC_DOMAIN') is None):
        _fail()
    _pack(env)
    return Path(root)


def validate_role_layout(root):
    """Require the limited profile's archived, disjoint UID-private directories.

    This is read-only. A trusted supervisor must stage this layout before
    selection; the general managed identity protector remains unchanged.
    """
    def directory(path, owner, group, mode):
        meta = path.lstat()
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != owner
                or meta.st_gid != group or stat.S_IMODE(meta.st_mode) != mode):
            _fail()

    try:
        directory(root, 0, 10001, 0o1771)
        for name in ('node-config', 'node-state', 'node-media'):
            directory(root / name, 10000, 10000, 0o700)
        directory(root / 'runtime', 10001, 10001, 0o700)
        # SOTTO_HOME is the receiver's private credential home (Google token).
        directory(root / 'sotto-home', 10001, 10001, 0o700)
    except OSError:
        _fail()


def _script(pack, *relative):
    candidate = pack.joinpath(*relative)
    try:
        current = pack
        for component in relative[:-1]:
            current = current / component
            meta = current.lstat()
            if (not stat.S_ISDIR(meta.st_mode) or not _image_owned(meta)):
                _fail()
        meta = candidate.lstat()
        if (not stat.S_ISREG(meta.st_mode) or not _image_owned(meta)):
            _fail()
    except OSError:
        _fail()
    return str(candidate)


def validate_schedules(env, spec_path):
    """Refuse missing/changed system spec and any gate or schedule override."""
    if any(env.get(name) for name in ('SOTTO_RUN_SKILL', 'SOTTO_SPAWN_TOOLSETS',
                                      'SOTTO_PROACTIVE_CRON')):
        _fail()
    if env.get('SOTTO_PROACTIVE') not in (None, '1') or env.get('SOTTO_DIGEST') not in (None, '1'):
        _fail()
    path = Path(spec_path)
    try:
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > 16 * 1024:
            _fail()
        rows = _json(path.read_bytes())
    except OSError:
        _fail()
    if not isinstance(rows, list) or len(rows) != 5:
        _fail()
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            _fail()
        name = row.get('name')
        if name not in SCHEDULES or name in seen:
            _fail()
        schedule, skill, prompt, gate, schedule_env = SCHEDULES[name]
        required = {'name', 'schedule', 'prompt', 'skill'}
        if gate:
            required.add('gate')
        if schedule_env:
            required.add('schedule_env')
        if (set(row) != required or row['schedule'] != schedule or row['skill'] != skill
                or row['prompt'] != prompt
                or row.get('gate') != gate or row.get('schedule_env') != schedule_env
                or not isinstance(row['prompt'], str)):
            _fail()
        seen.add(name)
    if seen != set(SCHEDULES):
        _fail()


def validate_packaged_procedures(env, receiver_root):
    """Every procedure the receiver can start in this profile must be packaged."""
    pack = _pack(env)
    for relative in (
            ('_shared', 'scripts', 'memory_cycle.py'),
            ('_shared', 'scripts', 'compose_notification.py'),
            ('_shared', 'scripts', 'compose_brief.py'),
            ('_shared', 'scripts', 'gather_google.py'),
            ('proactive', 'scripts', 'proactive_scan.py'),
            ('event-triage', 'scripts', 'digest_check.py'),
            ('relationship-pulse', 'scripts', 'relationship_pulse.py')):
        _script(pack, *relative)
    receiver = Path(receiver_root)
    if receiver != _PACKAGED_RECEIVER:
        _fail()
    try:
        for directory in (receiver.parent, receiver):
            meta = directory.lstat()
            if not stat.S_ISDIR(meta.st_mode) or not _image_owned(meta):
                _fail()
    except OSError:
        _fail()
    for name in ('receiver.py', 'brief_runner.py', 'procedure_runner.py'):
        _script(receiver, name)


def _receiver_module(name):
    """Load a packaged receiver sibling without ambient Python path discovery."""
    receiver = _PACKAGED_RECEIVER
    source = receiver / name
    if not source.is_file():
        _fail()
    spec = importlib.util.spec_from_file_location('sotto_fixed_' + name[:-3], source)
    module = importlib.util.module_from_spec(spec)
    previous = list(sys.path)
    try:
        sys.path.insert(0, str(receiver))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


def verify_configured_channel(env, client, *, timeout=30):
    """Verify the receiver's private projection against loaded Node config.

    Activation ACK is deliberately not required: the receiver writes that
    receipt after the first admitted owner DM. No provider or model is called.
    """
    root = Path(env['SOTTO_DATA'])
    telegram = env.get('SOTTO_RUNTIME_PROFILE') == TELEGRAM_PROFILE
    module = _receiver_module('telegram_channel_proof.py' if telegram else
                              'runtime_channel_proof.py')
    try:
        startup = module._startup_module()
        expectation = startup.read_receiver_channel_expectation(root / 'runtime' /
                                                                 'expected-channel.json')
        if (expectation['channel'] != ('telegram' if telegram else 'photon')
                or expectation['tenantId'] != env['SOTTO_TENANT_ID']
                or expectation['port'] != int(env['SOTTO_CHAT_RUNTIME_URL'].rsplit(':', 1)[1])
                or expectation['owner']['ownerId'] !=
                    env['TELEGRAM_HOME_CHANNEL' if telegram else 'PHOTON_HOME_CHANNEL']):
            _fail()
        expected = module._expected_config(expectation, env)
        url = f"http://127.0.0.1:{expectation['port']}"
        if client.url != url or env['SOTTO_CHAT_RUNTIME_URL'] != url:
            _fail()
    except (KeyError, ValueError, TypeError, OSError):
        _fail()
    deadline = time.monotonic() + timeout
    while True:
        try:
            client.verify_configured_identity(expected)
            break
        except (RuntimeError, OSError):
            if time.monotonic() >= deadline:
                _fail()
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    if startup.read_receiver_channel_expectation(root / 'runtime' /
                                                  'expected-channel.json') != expectation:
        _fail()


def _fresh_store(root, expected_uid=None):
    path = Path(__file__).resolve().parents[2] / 'runtime/trigger-receiver/fresh_routines.py'
    if not path.is_file():
        path = Path('/app/trigger-receiver/fresh_routines.py')
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location('sotto_fresh_routine_startup', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return module, module.load(root, expected_uid=expected_uid)
    except FileNotFoundError:
        return module, {'routines': {}}


def _validate_fresh_request(request, label, env, *, expected_uid=None):
    if env.get('SOTTO_RUNTIME_PROFILE') not in (PROFILE, SELF_HOST_PHOTON_PROFILE, TELEGRAM_PROFILE):
        _fail()
    if (not isinstance(request, dict) or set(request) != {'pack', 'kind', 'routine_id', 'definition_hash', 'prompt'}
            or request['pack'] != str(_pack(env)) or request['kind'] != 'personal-routine'):
        _fail()
    module, doc = _fresh_store(env['SOTTO_DATA'], expected_uid)
    row = doc['routines'].get(request['routine_id'])
    if (not row or row['definition_hash'] != request['definition_hash'] or
            row['definition']['prompt'] != request['prompt'] or label != 'routine:fresh:' + row['routine_id']):
        _fail()


def _validate_fresh_bindings(root, env, *, expected_uid=None):
    module, doc = _fresh_store(root, expected_uid)
    if not doc['routines']:
        return module, doc
    try:
        epoch = (env.get('SOTTO_ROUTINE_EPOCH') if expected_uid is None else
                 _json((Path(root) / '.sotto-epoch.json').read_text())['epoch_id'])
        if not isinstance(epoch, str) or re.fullmatch(r'[a-f0-9]{32}', epoch) is None:
            _fail()
        for row in doc['routines'].values():
            if row['binding']['epoch'] != epoch or row['binding']['profile'] != env.get('SOTTO_RUNTIME_PROFILE'):
                _fail()
    except (KeyError, TypeError, ValueError, OSError):
        _fail()
    return module, doc


def _validate_fresh_history(db, root, env, *, expected_uid=None):
    module, doc = _validate_fresh_bindings(root, env, expected_uid=expected_uid)
    try:
        allowed = {}
        for row in doc['routines'].values():
            binding = module.cursor_binding(row)
            from shared_lib import load_shared_lib  # noqa: PLC0415 -- _fresh_store installs the receiver path
            work_queue = load_shared_lib('work_queue', anchor=module.__file__)
            key = work_queue._routine_hash(binding['owner_scope'], binding['profile_id'], binding['legacy_id'])
            allowed[key] = (row, work_queue._routine_binding(binding['activation_epoch'], binding['source_revision'], binding['owner_route']))
        for key, binding_digest, expression, zone in db.execute('SELECT cursor_hash,binding_digest,schedule_expr,schedule_zone FROM routine_cursors'):
            if key not in allowed:
                _fail()
            row, expected = allowed[key]
            if binding_digest != expected or expression != module.expression(row['definition']) or zone != row['definition']['zone']:
                _fail()
        for routine_hash, job_id in db.execute('SELECT routine_hash,job_id FROM routine_slots'):
            if routine_hash not in allowed:
                _fail()
            job = db.execute('SELECT payload,status FROM jobs WHERE id=?', (job_id,)).fetchone()
            if job and job[1] in _ACTIVE:
                payload = _json(job[0])
                _validate_fresh_request(_json(payload['prompt']), payload['label'], env, expected_uid=expected_uid)
    except (KeyError, TypeError, ValueError, OSError):
        _fail()


def validate_work(kind, payload, env, *, python_executable=sys.executable, expected_uid=None):
    """Validate one persisted work request, including nested pack and bundle paths."""
    pack = _pack(env)
    if kind == 'event':
        if not isinstance(payload, dict) or set(payload) != {'bundle'}:
            _fail()
        bundle = payload['bundle']
        if not isinstance(bundle, dict) or not isinstance(bundle.get('events'), list):
            _fail()
        if len(bundle['events']) > 128:
            _fail()
        try:
            raw = json.dumps(bundle, allow_nan=False, ensure_ascii=False).encode('utf-8')
        except (TypeError, ValueError, UnicodeError, RecursionError):
            _fail()
        if len(raw) > 100_000 or not isinstance(_json(raw), dict):
            _fail()
        return
    if kind != 'run' or not isinstance(payload, dict) or set(payload) != {
            'runner', 'prompt', 'label', 'decision_ids'}:
        _fail()
    runner, prompt, label = payload['runner'], payload['prompt'], payload['label']
    if (not isinstance(runner, list) or len(runner) != 2 or runner[0] != python_executable
            or not isinstance(prompt, str) or len(prompt) > 100_000
            or not isinstance(label, str) or len(label) > 160
            or not isinstance(payload['decision_ids'], list)
            or len(payload['decision_ids']) > 128
            or not all(isinstance(item, str) and 0 < len(item) <= 160
                       for item in payload['decision_ids'])):
        _fail()
    script = runner[1]
    receiver = _PACKAGED_RECEIVER
    brief = str(receiver / 'brief_runner.py')
    procedure = str(receiver / 'procedure_runner.py')
    memory = _script(pack, '_shared', 'scripts', 'memory_cycle.py')
    event = _script(pack, '_shared', 'scripts', 'compose_notification.py')
    request = _json(prompt)
    if script == memory:
        if label != 'background:sotto-memory' or request != {}:
            _fail()
        return
    if script == event:
        if (label != 'event' or not isinstance(request, dict)
                or set(request) != {'kind', 'bundle_path'} or request['kind'] != 'event'):
            _fail()
        path = request['bundle_path']
        events = Path(env['SOTTO_DATA']) / 'events'
        if (not isinstance(path, str) or Path(path).parent != events
                or not _HEX24.fullmatch(Path(path).stem.removeprefix('bundle-'))):
            _fail()
        return
    if script == procedure and isinstance(request, dict) and request.get('kind') == 'proactive' and label.startswith('skill:'):
        if (env.get('SOTTO_RUNTIME_PROFILE') not in ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1')
                or set(request) != {'kind', 'pack', 'selection', 'work_key'} or request['pack'] != str(pack)
                or type(request['work_key']) is not str or re.fullmatch(r'skill:[a-f0-9]{64}', request['work_key']) is None
                or label != request['work_key'] + ':proactive' or request['selection'] != {'mode':'current'}):
            _fail()
        return
    if script == procedure and isinstance(request, dict) and request.get('kind') == 'shared-relationship-pulse':
        selection = request.get('selection')
        if (env.get('SOTTO_RUNTIME_PROFILE') not in ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1')
                or set(request) != {'kind', 'pack', 'selection', 'work_key'} or request['pack'] != str(pack)
                or type(request['work_key']) is not str or re.fullmatch(r'skill:[a-f0-9]{64}', request['work_key']) is None
                or label != request['work_key'] + ':relationship-pulse' or type(selection) is not dict
                or set(selection) != {'mode'} or selection.get('mode') != 'current'):
            _fail()
        return
    if script == procedure and isinstance(request, dict) and request.get('kind') == 'shared-followup':
        selection = request.get('selection')
        if (env.get('SOTTO_RUNTIME_PROFILE') not in ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1')
                or set(request) != {'kind', 'pack', 'selection', 'work_key'} or request['pack'] != str(pack)
                or type(request['work_key']) is not str or re.fullmatch(r'skill:[a-f0-9]{64}', request['work_key']) is None
                or label != request['work_key'] + ':followup' or type(selection) is not dict
                or set(selection) != {'mode'} or selection.get('mode') not in ('recent', 'backlog')):
            _fail()
        return
    if script == procedure and isinstance(request, dict) and request.get('kind') == 'shared-ask-research':
        selection = request.get('selection')
        company = type(selection) is dict and selection.get('mode') == 'company'
        field = 'company' if company else 'person'
        target = selection.get(field) if type(selection) is dict else None
        question = request.get('question')
        if (env.get('SOTTO_RUNTIME_PROFILE') not in ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1')
                or set(request) != {'kind', 'pack', 'selection', 'work_key', 'question'}
                or request['pack'] != str(pack) or not isinstance(request['work_key'], str)
                or re.fullmatch(r'skill:[a-f0-9]{64}', request['work_key']) is None
                or label != request['work_key'] + ':ask-research' or type(selection) is not dict
                or (set(selection) != {'mode', field} and not (company and set(selection) == {'mode', field, 'operation'} and selection['operation'] == 'website')) or selection['mode'] != field
                or type(target) is not str or not target or target.strip() != target or len(target) > 200
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                       or company and (128 <= ord(c) <= 159 or ord(c) in (0x2028, 0x2029))
                       for c in target)
                or type(question) is not str or not question or len(question.encode()) > 64000 or target not in question):
            _fail()
        return
    if script == procedure and isinstance(request, dict) and request.get('kind') == 'shared-meeting-prep':
        selection = request.get('selection')
        if (env.get('SOTTO_RUNTIME_PROFILE') not in ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1')
                or set(request) != {'kind', 'pack', 'selection', 'work_key'} or request['pack'] != str(pack)
                or not isinstance(request['work_key'], str) or re.fullmatch(r'skill:[a-f0-9]{64}', request['work_key']) is None
                or label != request['work_key'] + ':meeting-prep' or type(selection) is not dict):
            _fail()
        # Admission and persisted-work validation share the same closed selection
        # schema, including an optional exact calendar occurrence. Rechecking the
        # worker envelope above still confines the executable, pack and work key.
        try:
            _script(_PACKAGED_RECEIVER, 'runtime_meeting_prep_selection.py')
            _receiver_module('runtime_meeting_prep_selection.py').validate_selection(selection)
        except (ValueError, ImportError, SyntaxError, OSError, AttributeError):
            _fail()
        return
    if script == procedure and isinstance(request, dict) and request.get('kind') == 'focused-meeting-prep':
        offer = request.get('offer')
        if (env.get('SOTTO_RUNTIME_PROFILE') not in ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1')
                or set(request) != {'kind', 'pack', 'offer', 'work_key'} or request['pack'] != str(pack)
                or not isinstance(offer, dict) or offer.get('kind') != 'meeting_prep'
                or not isinstance(offer.get('offer_id'), str)
                or re.fullmatch(r'[a-f0-9]{32}(?:[a-f0-9]{32})?', offer['offer_id']) is None
                or label != 'meeting-prep:' + offer['offer_id'] or request['work_key'] != label
                or offer.get('target') != 'photon' or not offer.get('chat_id')
                or not offer.get('accepted_at') or not isinstance(offer.get('meeting'), dict)
                or set(offer['meeting']) != {'event_id', 'start'}
                or any(not isinstance(offer['meeting'][key], str) or not offer['meeting'][key]
                       for key in ('event_id', 'start'))):
            _fail()
        return
    if script == procedure and isinstance(request, dict) and request.get('kind') == 'personal-routine':
        _validate_fresh_request(request, label, env, expected_uid=expected_uid)
        return
    if script == procedure:
        if (not isinstance(request, dict) or set(request) != {'pack', 'kind'}
                or request['pack'] != str(pack)
                or request['kind'] not in ('proactive', 'digest', 'pulse')):
            _fail()
        expected = {'proactive': ('proactive', 'cron:sotto-proactive',
                                  'run-now:sotto-proactive'),
                    'digest': ('cron:sotto-midday-digest', 'run-now:sotto-midday-digest'),
                    'pulse': ('cron:sotto-relationship-pulse', 'run-now:sotto-relationship-pulse')}
        if label not in expected[request['kind']]:
            _fail()
        return
    if script == brief:
        # Owner requests use the same confined brief runner. Keep their persisted
        # shape closed so restart validation cannot admit arbitrary runner inputs.
        if label.startswith('requested:'):
            if (not isinstance(request, dict)
                    or set(request) not in ({'pack', 'kind', 'day', 'work_key', 'refresh'},
                                           {'pack', 'kind', 'day', 'work_key', 'refresh', 'composition_direction'})
                    or ('composition_direction' in request and (
                        not isinstance(request['composition_direction'], str)
                        or not request['composition_direction'].strip()
                        or len(request['composition_direction']) > 16_000))
                    or request.get('pack') != str(pack)
                    or request.get('kind') not in ('morning', 'evening')
                    or not re.fullmatch(
                        rf"requested:[0-9a-f]{{32}}:sotto-{request['kind']}-brief", label)
                    or request.get('work_key') != label
                    or type(request.get('refresh')) is not bool
                    or not isinstance(request.get('day'), str)
                    or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', request['day'])):
                _fail()
            try:
                datetime.strptime(request['day'], '%Y-%m-%d')
            except ValueError:
                _fail()
            return
        if (not isinstance(request, dict) or request.get('pack') != str(pack)
                or request.get('kind') not in ('morning', 'evening', 'welcome')
                or set(request) - {'pack', 'kind', 'payload_path', 'day', 'work_key',
                                   'deliver_not_before', 'prepare', 'learn_only',
                                   'learning_revision', 'history_discovery'}):
            _fail()
        names = {'morning': 'sotto-morning-brief',
                 'evening': 'sotto-evening-brief', 'welcome': 'sotto-welcome-brief'}
        name = names[request['kind']]
        labels = {f'cron:{name}', f'brief:{name}', f'run-now:{name}',
                  f'background:prepare:{name}'}
        if request['kind'] == 'welcome':
            labels.add('onboarding:sotto-welcome-brief')
        discovery_label = 'onboarding:history-discovery:sotto-welcome-brief'
        if request.get('history_discovery') is not None:
            if (request.get('history_discovery') is not True or request['kind'] != 'welcome'
                    or request.get('work_key') != 'onboarding:history-discovery:v1:' + str(request.get('day'))
                    or (label != discovery_label and not label.startswith('background:sotto-learn:'))):
                _fail()
            labels.add(discovery_label)
        elif label == discovery_label:
            _fail()
        if label.startswith('background:sotto-learn:'):
            if request.get('learn_only') is not True or not re.fullmatch(
                    r'background:sotto-learn:[0-9a-f]{24}(?::[0-9a-f]{1,64})?', label):
                _fail()
        elif label not in labels or request.get('learn_only') is True:
            _fail()
        if request.get('prepare') is not None and (request['prepare'] is not True
                or label != f'background:prepare:{name}'):
            _fail()
        if request.get('payload_path') not in (None, ''):
            source = request['payload_path']
            expected_path = (Path(env['SOTTO_DATA']) / 'briefs' /
                             f"{request.get('day')}.{request['kind']}.payload.json")
            if not isinstance(source, str) or source != str(expected_path):
                _fail()
        if (not isinstance(request.get('day'), str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', request['day'])
                or not isinstance(request.get('work_key'), str)
                or not 1 <= len(request['work_key']) <= 256):
            _fail()
        return
    _fail()


def recover_work_queue_journal(data_root, *, expected_uid=None):
    """Roll back a crash-left SQLite hot journal as the queue's own UID, never as another.

    A receiver killed mid-transaction leaves `work.sqlite3-journal`; a read-only connection
    cannot roll it back, so restart validation would refuse forever. This opens the database
    read-write only to let SQLite restore its last committed state; it creates no schema.
    """
    events = Path(data_root) / 'events'
    journal = events / 'work.sqlite3-journal'
    try:
        journal_meta = journal.lstat()
    except FileNotFoundError:
        return
    except OSError:
        _fail()
    owner = os.geteuid() if expected_uid is None else expected_uid
    if os.geteuid() != owner:
        _fail()
    path = events / 'work.sqlite3'
    try:
        meta = path.lstat()
    except OSError:
        _fail()
    if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != owner
            or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size > 16 * 1024 * 1024
            or not stat.S_ISREG(journal_meta.st_mode) or journal_meta.st_uid != owner
            or journal_meta.st_nlink != 1 or journal_meta.st_mode & 0o022
            or journal_meta.st_size > 64 * 1024 * 1024):
        _fail()
    try:
        db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=2,
                             isolation_level=None)
        try:
            db.execute('SELECT count(*) FROM sqlite_master').fetchone()
        finally:
            db.close()
    except sqlite3.Error:
        _fail()


def validate_work_queue(data_root, env, *, expected_uid=None,
                        allow_supported_history=False):
    """Read bounded active jobs without creating/migrating the WorkQueue database."""
    _, fresh = _validate_fresh_bindings(data_root, env, expected_uid=expected_uid)
    if fresh['routines'] and not allow_supported_history:
        _fail()
    path = Path(data_root) / 'events' / 'work.sqlite3'
    try:
        meta = path.lstat()
    except FileNotFoundError:
        return
    owner = os.geteuid() if expected_uid is None else expected_uid
    if (expected_uid is not None and os.geteuid() != 0):
        _fail()
    if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != owner
            or stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size > 16 * 1024 * 1024):
        _fail()
    if os.geteuid() == owner:
        # The supervisor (root) validates read-only; its receiver-UID stager recovered first.
        recover_work_queue_journal(data_root)
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            rows = db.execute("SELECT kind,payload FROM jobs WHERE status IN ('pending','ready','leased') LIMIT 1025").fetchall()
            if len(rows) > 1024:
                _fail()
            for kind, raw in rows:
                if not isinstance(raw, str) or len(raw) > 100_000:
                    _fail()
                validate_work(kind, _json(raw), env, expected_uid=expected_uid)
            has_routines = any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is not None
                               for table in ('routine_slots', 'routine_cursors'))
            if has_routines:
                if not allow_supported_history:
                    _fail()
                _validate_fresh_history(db, data_root, env, expected_uid=expected_uid)
            # Historical fixed fences never prove acceptance. Fresh admission requires
            # empty history; a verified same-epoch restart keeps them for receiver-side
            # deduplication and per-record recovery without treating them as grants.
            if not allow_supported_history:
                for table in ('fixed_slots', 'fixed_brief_wakes'):
                    if (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                   (table,)).fetchone()
                            and db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() is not None):
                        _fail()
    except (sqlite3.Error, OSError):
        _fail()


def validate_empty_outbox(data_root, *, expected_uid=None,
                          allow_supported_history=False):
    """Require no receiver send/effect history before switching this fresh profile.

    A pending row would be retried at boot, and even terminal historical rows can
    hide an uncertain send. This limited profile does not migrate those effects.
    Absence is a fresh-install precondition, not proof of historical non-delivery.
    """
    events = Path(data_root) / 'events'
    try:
        directory = os.open(events, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError:
        _fail()
    descriptor = None
    try:
        uid = os.geteuid() if expected_uid is None else expected_uid
        if (expected_uid is not None and os.geteuid() != 0):
            _fail()
        meta = os.fstat(directory)
        if meta.st_uid != uid or meta.st_mode & 0o022:
            _fail()
        try:
            descriptor = os.open('outbox.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
        except FileNotFoundError:
            return
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > 4 * 1024 * 1024):
            _fail()
        raw = os.read(descriptor, 4 * 1024 * 1024 + 1)
        after = os.fstat(descriptor)
        linked = os.stat('outbox.json', dir_fd=directory, follow_symlinks=False)
        if (len(raw) != before.st_size or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                 before.st_ctime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 after.st_ctime_ns) or
                (before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)):
            _fail()
        doc = _json(raw)
        if (not isinstance(doc, dict) or set(doc) != {'rows'}
                or not isinstance(doc['rows'], list)
                or len(doc['rows']) > 8192
                or any(not isinstance(row, dict) for row in doc['rows'])
                or (not allow_supported_history and doc['rows'] != [])):
            _fail()
    except (OSError, ValueError, TypeError, RecursionError):
        _fail()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def validate_offline(env, *, native_uid=None, allow_supported_history=False):
    """Read-only receiver preflight before Node is started or any worker claims work."""
    root = validate_environment(env)
    validate_role_layout(root)
    if env['SOTTO_RUNTIME_PROFILE'] == TELEGRAM_PROFILE:
        link = root / 'telegram-link.json'
        try:
            parent = root.lstat()
            before = link.lstat()
            if (parent.st_uid != 0 or parent.st_gid != 10001
                    or stat.S_IMODE(parent.st_mode) != 0o1771
                    or not stat.S_ISREG(before.st_mode) or before.st_uid != 10001
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > 8192):
                _fail()
            fd = os.open(link, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if os.fstat(fd).st_ino != before.st_ino:
                    _fail()
                payload = _json(os.read(fd, 8193))
                after = os.fstat(fd)
                if ((before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)):
                    _fail()
            finally:
                os.close(fd)
            if payload != {'bot_token': env['TELEGRAM_BOT_TOKEN'],
                           'allowed_user': int(env['TELEGRAM_HOME_CHANNEL'])}:
                _fail()
        except (OSError, ValueError, TypeError):
            _fail()
    repository = Path(__file__).resolve().parents[2]
    validate_schedules(env, repository / 'adapters' / 'sotto' / 'crons.json')
    validate_packaged_procedures(env, _PACKAGED_RECEIVER)
    module, fresh = _fresh_store(root, expected_uid=native_uid)
    if fresh['routines'] and not allow_supported_history:
        _fail()
    validate_empty_outbox(root, expected_uid=native_uid,
                          allow_supported_history=allow_supported_history)
    validate_work_queue(root, env, expected_uid=native_uid,
                        allow_supported_history=allow_supported_history)
    return root
