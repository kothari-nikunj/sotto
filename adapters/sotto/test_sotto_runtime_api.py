"""Static adapter contract checks; no live channel or model calls."""
import importlib.util
from copy import deepcopy
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def adapter():
    path = Path(__file__).with_name('runtime_api.py')
    spec = importlib.util.spec_from_file_location('sotto_runtime_api_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_selfhost_readiness_verifies_current_node_identity(monkeypatch):
    api = adapter()
    calls = []
    profile = SimpleNamespace(
        TELEGRAM_PROFILE='self-host-telegram-fixed-five-v1',
        SELF_HOST_PHOTON_PROFILE='self-host-photon-fixed-five-v1',
        validate_environment=lambda env: calls.append('environment'),
        verify_configured_channel=lambda env, client, **kw: calls.append((client, kw)))
    monkeypatch.setattr(api, '_fixed_five_module', lambda: profile)
    monkeypatch.setattr(api, '_client', lambda timeout: ('private-node', timeout))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self_host')
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', profile.TELEGRAM_PROFILE)
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture-only')
    assert api.selfhost_provisioning_status() == {'channel': 'telegram', 'model_configured': True}
    assert calls == ['environment', (('private-node', 1), {'timeout': 0})]
    calls.clear()
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    with pytest.raises(api.UnsupportedRuntimeOperationError):
        api.selfhost_provisioning_status()
    assert calls == []


def test_native_selfhost_readiness_never_caches_a_failed_node_proof(monkeypatch):
    api = adapter()
    def unavailable(*args, **kwargs):
        raise RuntimeError('node identity mismatch')
    profile = SimpleNamespace(
        TELEGRAM_PROFILE='self-host-telegram-fixed-five-v1',
        SELF_HOST_PHOTON_PROFILE='self-host-photon-fixed-five-v1',
        validate_environment=lambda env: None, verify_configured_channel=unavailable)
    monkeypatch.setattr(api, '_fixed_five_module', lambda: profile)
    monkeypatch.setattr(api, '_client', lambda timeout: object())
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self_host')
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', profile.SELF_HOST_PHOTON_PROFILE)
    with pytest.raises(RuntimeError):
        api.selfhost_provisioning_status()


def test_send_capability_is_static_and_selected_runtime_readiness_fails_closed(monkeypatch):
    api = adapter()
    monkeypatch.setenv('SOTTO_CHAT_RUNTIME_URL', 'http://127.0.0.1:8789')
    monkeypatch.setenv('SOTTO_CHAT_RUNTIME_TOKEN', 'x' * 32)
    assert api.send_capability() == (True, '')
    capable, reason = api.runtime_readiness()
    assert capable is False
    assert 'unattended skill run' in reason
    assert 'personal routine scheduler' in reason
    monkeypatch.setenv('SOTTO_CHAT_RUNTIME_URL', 'https://example.com')
    assert api.send_capability()[0] is False


def test_dispatch_identity_and_attempt_are_forwarded_without_fallback(monkeypatch, tmp_path):
    api = adapter()
    calls = []
    data = tmp_path / 'data'
    artifact = data / 'cache' / 'visual-briefs' / ('a' * 24)
    artifact.mkdir(parents=True, mode=0o700)
    artifact.chmod(0o700)
    paths = []
    for index in range(1, 5):
        path = artifact / f'{index:02d}.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        path.chmod(0o600)
        paths.append(str(path))
    monkeypatch.setenv('SOTTO_DATA', str(data))

    class FakeClient:
        def send_receipt(self, *args, **kwargs):
            calls.append(('send', args, kwargs))
            return True, '', {'acceptance': 'accepted', 'message_id': 'provider'}

        def send_gallery_bytes(self, *args):
            calls.append(('gallery', args))
            return True, '', {'acceptance': 'accepted', 'message_id': 'parent'}

        def receipt(self, dispatch_id):
            calls.append(('receipt', dispatch_id))
            return {'acceptance': 'unknown'}

    monkeypatch.setattr(api, '_client', lambda timeout=60: FakeClient())
    assert api.send_receipt('body', 'owner')[2]['acceptance'] == 'not_attempted'
    assert not calls
    assert api.send_receipt('body', 'owner', 30, 'stable-id', 3)[0]
    assert api.send_gallery({'images': paths, 'summary': 'summary'}, 'owner', 'gallery-id')[0]
    assert api.gallery_receipt('gallery-id') == {'acceptance': 'unknown'}
    assert calls == [
        ('send', ('body', 'owner', 'stable-id'), {'attempt': 3}),
        ('gallery', ([{'name': f'{index:02d}.png', 'bytes': b'\x89PNG\r\n\x1a\nfixture'}
                     for index in range(1, 5)], 'summary', 'owner', 'gallery-id')),
        ('receipt', 'gallery-id'),
    ]


def test_private_gallery_rejects_symlink_and_oversize_before_transport(monkeypatch, tmp_path):
    api = adapter()
    artifact = tmp_path / 'data' / 'cache' / 'visual-briefs' / ('b' * 24)
    artifact.mkdir(parents=True, mode=0o700)
    artifact.chmod(0o700)
    paths = []
    for index in range(1, 5):
        path = artifact / f'{index:02d}.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        path.chmod(0o600)
        paths.append(str(path))
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path / 'data'))
    monkeypatch.setattr(api, '_client', lambda timeout=60: pytest.fail('transport contacted'))
    first = artifact / '01.png'
    first.unlink()
    first.symlink_to(artifact / '02.png')
    assert api.send_gallery({'images': paths, 'summary': 'brief'}, 'owner', 'stable')[2] == {
        'acceptance': 'not_attempted'}
    first.unlink()
    first.write_bytes(b'\x89PNG\r\n\x1a\n' + b'x' * 5_000_000)
    first.chmod(0o600)
    assert api.send_gallery({'images': paths, 'summary': 'brief'}, 'owner', 'stable')[2] == {
        'acceptance': 'not_attempted'}


def test_activation_calls_use_the_explicit_three_second_runtime_client(monkeypatch):
    api = adapter()
    calls = []
    class FakeClient:
        def activation_evidence(self):
            calls.append('evidence')
            return None
        def activation_ack(self, event_id):
            calls.append(('ack', event_id))
            return True
    monkeypatch.setattr(api, '_client', lambda timeout=60: calls.append(('timeout', timeout)) or FakeClient())
    assert api.activation_evidence() is None
    assert api.activation_ack('a' * 64) is True
    assert calls == [('timeout', 3), 'evidence', ('timeout', 3), ('ack', 'a' * 64)]


def test_unported_contracts_raise_explicitly():
    api = adapter()
    with pytest.raises(api.UnsupportedRuntimeOperationError, match='personal_routines'):
        api.personal_routines()


def test_telegram_token_requires_telegram_delivery_and_private_link_bound_to_owner(tmp_path, monkeypatch):
    api = adapter()
    data = tmp_path / 'data'
    data.mkdir()
    link = data / 'telegram-link.json'
    link.write_text('{"bot_token":"123456:synthetic_token_with_sufficient_length","allowed_user":8675309, '
                    '"bot_username":"test_bot"}\n', encoding='utf-8')
    link.chmod(0o600)
    monkeypatch.setenv('SOTTO_CRON_DELIVER', ' telegram ')
    monkeypatch.setenv('SOTTO_DATA', str(data))
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '8675309')
    monkeypatch.setenv('TELEGRAM_HOME_CHANNEL', '8675309')
    assert api.telegram_bot_token() == '123456:synthetic_token_with_sufficient_length'

    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '111')
    assert api.telegram_bot_token() == ''
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '8675309')
    monkeypatch.setenv('TELEGRAM_HOME_CHANNEL', '111')
    assert api.telegram_bot_token() == ''

    monkeypatch.delenv('TELEGRAM_HOME_CHANNEL')
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'photon')
    assert api.telegram_bot_token() == ''


def test_telegram_token_never_falls_back_to_ambient_env_or_a_home_dotenv(tmp_path, monkeypatch):
    api = adapter()
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '111111:ambient-only-token')
    monkeypatch.setenv('SOTTO_HOME', str(tmp_path / 'sotto-home'))
    home = tmp_path / 'sotto-home'
    home.mkdir()
    (home / '.env').write_text('TELEGRAM_BOT_TOKEN=222222:dotenv-token\n', encoding='utf-8')
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path / 'missing-data'))
    assert api.telegram_bot_token() == ''


@pytest.mark.parametrize('payload', [
    '{"bot_token":"123456:token","bot_token":"123456:other","allowed_user":8675309}',
    '{"bot_token":"123456:token","allowed_user":true}',
    '{"bot_token":"123456:token","allowed_user":0}',
    '{"bot_token":"123456:token","allowed_user":8675309,"unexpected":"x"}',
])
def test_telegram_token_fails_closed_on_invalid_link_identity_or_shape(tmp_path, monkeypatch, payload):
    api = adapter()
    data = tmp_path / 'data'
    data.mkdir()
    link = data / 'telegram-link.json'
    link.write_text(payload, encoding='utf-8')
    link.chmod(0o600)
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    monkeypatch.setenv('SOTTO_DATA', str(data))
    assert api.telegram_bot_token() == ''


def test_telegram_token_rejects_nonprivate_symlink_and_oversized_files(tmp_path, monkeypatch):
    api = adapter()
    data = tmp_path / 'data'
    data.mkdir()
    target = tmp_path / 'outside.json'
    target.write_text('{"bot_token":"123456:token","allowed_user":8675309}', encoding='utf-8')
    target.chmod(0o600)
    link = data / 'telegram-link.json'
    link.symlink_to(target)
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    monkeypatch.setenv('SOTTO_DATA', str(data))
    assert api.telegram_bot_token() == ''
    link.unlink()
    link.write_text('{"bot_token":"123456:token","allowed_user":8675309}', encoding='utf-8')
    link.chmod(0o644)
    assert api.telegram_bot_token() == ''
    link.chmod(0o600)
    link.write_text(' ' * (16 * 1024 + 1), encoding='utf-8')
    assert api.telegram_bot_token() == ''


def test_telegram_token_rejects_fifo_without_blocking(tmp_path, monkeypatch):
    api = adapter()
    data = tmp_path / 'data'
    data.mkdir()
    os.mkfifo(data / 'telegram-link.json', 0o600)
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    monkeypatch.setenv('SOTTO_DATA', str(data))
    assert api.telegram_bot_token() == ''


def test_telegram_token_reads_actual_persisted_pairing_record(tmp_path, monkeypatch):
    writer_path = Path(__file__).parents[2] / 'runtime' / 'trigger-receiver' / 'telegram_link.py'
    spec = importlib.util.spec_from_file_location('telegram_link_writer_fixture', writer_path)
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    token = '123456:fixture_token_with_at_least_twenty_chars'
    writer.persist(token, 8675309, str(data / 'telegram-link.json'), 'fixture_bot')
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    monkeypatch.setenv('SOTTO_DATA', str(data))
    monkeypatch.setenv('TELEGRAM_ALLOWED_USERS', '8675309')
    assert (data / 'telegram-link.json').stat().st_mode & 0o777 == 0o600
    assert adapter().telegram_bot_token() == token


def test_telegram_token_rejects_data_root_replacement_during_read(tmp_path, monkeypatch):
    api = adapter()
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    link = data / 'telegram-link.json'
    link.write_text('{"bot_token":"123456:fixture_token_with_at_least_twenty_chars", '
                    '"allowed_user":8675309}', encoding='utf-8')
    link.chmod(0o600)
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    monkeypatch.setenv('SOTTO_DATA', str(data))
    real_read = os.read
    replaced = False

    def replace_root(fd, size):
        nonlocal replaced
        value = real_read(fd, size)
        if not replaced and value:
            data.rename(tmp_path / 'old-data')
            data.mkdir(mode=0o700)
            replaced = True
        return value

    monkeypatch.setattr(api.os, 'read', replace_root)
    assert api.telegram_bot_token() == ''


def test_telegram_token_rejects_data_root_permission_change_during_read(tmp_path, monkeypatch):
    api = adapter()
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    link = data / 'telegram-link.json'
    link.write_text('{"bot_token":"123456:fixture_token_with_at_least_twenty_chars", '
                    '"allowed_user":8675309}', encoding='utf-8')
    link.chmod(0o600)
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    monkeypatch.setenv('SOTTO_DATA', str(data))
    real_read = os.read
    changed = False

    def change_root_mode(fd, size):
        nonlocal changed
        value = real_read(fd, size)
        if not changed and value:
            data.chmod(0o750)
            changed = True
        return value

    monkeypatch.setattr(api.os, 'read', change_root_mode)
    assert api.telegram_bot_token() == ''


def test_telegram_token_rejects_file_mutation_during_read(tmp_path, monkeypatch):
    api = adapter()
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    link = data / 'telegram-link.json'
    link.write_text('{"bot_token":"123456:fixture_token_with_at_least_twenty_chars", '
                    '"allowed_user":8675309}', encoding='utf-8')
    link.chmod(0o600)
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    monkeypatch.setenv('SOTTO_DATA', str(data))
    real_read = os.read
    mutated = False

    def mutate_file(fd, size):
        nonlocal mutated
        value = real_read(fd, size)
        if not mutated and value:
            link.write_text('{"bot_token":"123456:replacement_token_with_long_secret", '
                            '"allowed_user":8675309}', encoding='utf-8')
            link.chmod(0o600)
            mutated = True
        return value

    monkeypatch.setattr(api.os, 'read', mutate_file)
    assert api.telegram_bot_token() == ''




def test_unconfigured_runtime_is_not_ready(monkeypatch):
    api = adapter()
    monkeypatch.delenv('SOTTO_CHAT_RUNTIME_URL', raising=False)
    capable, reason = api.runtime_readiness()
    assert capable is False


def test_procedure_runner_receives_only_its_request_argument():
    api = adapter()
    assert api.run_argv(['python3', '/app/procedure_runner.py'], '{}') == ['python3', '/app/procedure_runner.py', '{}']
    assert api.run_argv(['python3', '/app/brief_runner.py'], '--literal') == ['python3', '/app/brief_runner.py', '--literal']
    with pytest.raises(ValueError):
        api.run_argv([], 'prompt')
    assert api.read_usage(None) is None


def test_google_paths_reuse_one_existing_writer_and_fixed_sotto_cli(tmp_path, monkeypatch):
    api = adapter()
    monkeypatch.setenv('SOTTO_HOME', str(tmp_path / 'volume' / 'sotto-home'))
    assert api.home_path('google_token.json') == str(tmp_path / 'volume' / 'sotto-home' / 'google_token.json')
    assert Path(api.google_setup_path()).name == 'google_setup.py'
    assert Path(api.google_setup_path()).parent.name == 'sotto'
    assert Path(api.google_setup_path()).resolve() == Path(__file__).with_name('google_setup.py').resolve()
    assert Path(api.google_api_path()).name == 'google_api.py'
    assert Path(api.google_api_path()).parent.name == 'sotto'
    assert api.google_api_path() != api.google_setup_path()


def test_google_home_requires_explicit_absolute_sotto_home_without_ambient_fallback(
        tmp_path, monkeypatch):
    api = adapter()
    ambient = tmp_path / 'ambient'
    (ambient / '.hermes').mkdir(parents=True)
    (ambient / '.hermes' / 'google_token.json').write_text('{"token":"ambient"}')
    monkeypatch.setenv('HOME', str(ambient))
    for value in (None, '', 'relative/home', '/tmp/../other', '/tmp/./other'):
        if value is None:
            monkeypatch.delenv('SOTTO_HOME', raising=False)
        else:
            monkeypatch.setenv('SOTTO_HOME', value)
        with pytest.raises(ValueError, match='explicit SOTTO_HOME required'):
            api.home_path('google_token.json')
    explicit = tmp_path / 'volume' / 'sotto-home'
    monkeypatch.setenv('SOTTO_HOME', str(explicit))
    assert api.home_path('google_token.json') == str(explicit / 'google_token.json')


def test_archive_sessions_uses_runtime_rotation_count_and_fail_closed(monkeypatch):
    api = adapter()
    calls = []
    class Client:
        def __init__(self, response): self.response = response
        def sessions(self, action): calls.append(action); return self.response
    monkeypatch.setattr(api, '_client', lambda timeout=30: Client({'count': 3}))
    assert api.archive_sessions() == 3 and calls == ['rotate']
    for invalid in ({'count': True}, {'count': -1}, {'rotated': True}, None):
        monkeypatch.setattr(api, '_client', lambda timeout=30: Client(invalid))
        assert api.archive_sessions() is None
    monkeypatch.setattr(api, '_client', lambda timeout=30: (_ for _ in ()).throw(OSError('offline')))
    assert api.archive_sessions() is None


def test_feedback_status_closed_projection_and_bounded_health(monkeypatch):
    api = adapter()
    stages = [{'stage': name, 'outcome': 'timeout', 'elapsed_ms': 1000,
               'step': 'lookup', 'lookup_ms': 1000, 'effect_ms': None,
               'error_class': None, 'late_outcome': 'success'}
              for name in ('read', 'processing', 'completion')]
    feedback = {'available': True, 'scope': 'latest_fresh_turn', 'turn_sequence': 1,
                'age_ms': 10, 'reply_accepted': True, 'uncertain': False, 'stages': stages}
    calls = []
    class Client:
        def call(self, path, **kwargs):
            calls.append((path, kwargs))
            return SimpleNamespace(ok=True, value={'feedback': feedback, 'secret': 'not exposed'})
    def factory(timeout):
        assert timeout == 1
        return Client()
    monkeypatch.setattr(api, '_client', factory)
    assert api.feedback_status() == feedback
    assert calls == [('health', {'response_limit': 16384, 'strict_json': True})]
    feedback.update(reply_accepted=False, work_accepted=True)
    assert api.feedback_status() == feedback
    feedback['work_accepted'] = False
    assert api.feedback_status() == feedback
    for invalid in (None, 1, 'true'):
        feedback['work_accepted'] = invalid
        assert api.feedback_status() == {'available': False}
    feedback['work_accepted'] = True
    feedback['stages'][0]['raw_error'] = 'private'
    assert api.feedback_status() == {'available': False}


@pytest.mark.parametrize('value', [None, {}, {'available': False, 'id': 'private'},
                                   {'available': True}, {'available': False}])
def test_feedback_status_missing_or_malformed_is_unavailable(monkeypatch, value):
    api = adapter()
    monkeypatch.setattr(api, '_client', lambda timeout: SimpleNamespace(
        call=lambda *a, **k: SimpleNamespace(ok=True, value={'feedback': value})))
    assert api.feedback_status() == {'available': False}
    monkeypatch.setattr(api, '_client', lambda timeout: (_ for _ in ()).throw(TimeoutError('private')))
    assert api.feedback_status() == {'available': False}


def test_runtime_observation_single_read_closed_health_and_no_fresh_turn(monkeypatch):
    api = adapter()
    value = {'runtime': 'sotto', 'ready': True, 'providerLifecycle': 'active',
             'providerConnection': 'not_observed', 'feedback': {'available': False},
             'private': 'never exposed', 'pendingInbound': 99}
    calls = []
    def factory(timeout):
        assert timeout == 1
        def call(path, **kwargs):
            calls.append((path, kwargs))
            return SimpleNamespace(ok=True, value=value)
        return SimpleNamespace(call=call)
    monkeypatch.setattr(api, '_client', factory)
    assert api.runtime_observation() == {
        'feedback': {'available': False}, 'runtime_health': {
            'status': 'observed', 'ready': True, 'provider_lifecycle': 'active',
            'provider_connection': 'not_observed', 'feedback_status': 'no_fresh_turn'}}
    assert calls == [('health', {'response_limit': 16384, 'strict_json': True})]
    value.update(ready=False, providerLifecycle='failed')
    observed = api.runtime_observation()
    assert observed['runtime_health']['ready'] is False
    assert observed['runtime_health']['provider_lifecycle'] == 'failed'
    value['feedback'] = {'available': False, 'private': 'not a valid feedback shape'}
    assert api.runtime_observation()['runtime_health']['feedback_status'] == 'invalid_response'
    value['feedback'] = {'available': 0}
    assert api.runtime_observation()['runtime_health']['feedback_status'] == 'invalid_response'
    for key, invalid in [('ready', 1), ('providerLifecycle', 'private'),
                         ('providerConnection', 'connected'), ('runtime', 'other')]:
        previous = value[key]
        value[key] = invalid
        assert api.runtime_observation()['runtime_health'] == {'status': 'invalid_response'}
        value[key] = previous


def test_runtime_observation_read_failure_not_no_fresh_turn(monkeypatch):
    api = adapter()
    monkeypatch.setattr(api, '_client', lambda timeout: SimpleNamespace(
        call=lambda *a, **k: SimpleNamespace(ok=False, value={})))
    expected = {'feedback': {'available': False}, 'runtime_health': {'status': 'read_failed'}}
    assert api.runtime_observation() == expected
    monkeypatch.setattr(api, '_client', lambda timeout: (_ for _ in ()).throw(TimeoutError('secret')))
    assert api.runtime_observation() == expected


def test_runtime_observation_projects_only_bounded_chat_timings(monkeypatch):
    api = adapter()
    timing = {'sourceCheckMs': 10, 'standingContextMs': 20, 'toolSetupMs': 30,
              'generationMs': 40, 'finalSourceCheckMs': 50, 'totalMs': 150}
    value = {'runtime': 'sotto', 'ready': True, 'providerLifecycle': 'active',
             'providerConnection': 'not_observed', 'feedback': {'available': False},
             'chatTiming': timing, 'chatTimingCompletedAtMs': 1791450000000, 'chatTimingOutcome': 'failed',
             'privatePrompt': 'never expose'}
    monkeypatch.setattr(api, '_client', lambda timeout: SimpleNamespace(
        call=lambda *a, **k: SimpleNamespace(ok=True, value=value)))
    assert api.runtime_observation()['runtime_health']['chat_timing'] == timing
    assert api.runtime_observation()['runtime_health']['chat_timing_completed_at_ms'] == 1791450000000
    assert api.runtime_observation()['runtime_health']['chat_timing_outcome'] == 'failed'
    for extra in ({'queueMs': 300000}, {'promptPreparationMs': 0}, {'queueMs': 25, 'promptPreparationMs': 60}):
        value['chatTiming'] = {**timing, **extra}
        assert api.runtime_observation()['runtime_health']['chat_timing'] == value['chatTiming']
    for key in ('queueMs', 'promptPreparationMs'):
        for invalid in (-1, 300001, 1.5, 'PRIVATE', True, None, float('nan'), float('inf')):
            value['chatTiming'] = {**timing, key: invalid}
            assert 'chat_timing' not in api.runtime_observation()['runtime_health']
    assert api._chat_timing_projection({'queueMs': 1, 'promptPreparationMs': 2}) is None
    assert api._chat_timing_projection({**timing, 'queueMs': 1, 'prompt': 'PRIVATE'}) is None
    value['chatTiming'] = timing
    value['chatTimingOutcome'] = 'private error'
    assert 'chat_timing_outcome' not in api.runtime_observation()['runtime_health']
    for invalid in ({**timing, 'privatePrompt': 'secret'}, {**timing, 'totalMs': -1},
                    {**timing, 'totalMs': 300001}, {**timing, 'totalMs': True},
                    {**timing, 'totalMs': 1.5}):
        value['chatTiming'] = invalid
        assert 'chat_timing' not in api.runtime_observation()['runtime_health']
    value['chatTiming'] = timing
    value['chatTimingCompletedAtMs'] = '1791450000000'
    assert 'chat_timing' not in api.runtime_observation()['runtime_health']
    value['chatTimingCompletedAtMs'] = 1791450000000
    assert 'privatePrompt' not in api.runtime_observation()['runtime_health']


def test_task_diagnostics_projection_rejects_content_and_preserves_empty_batch(monkeypatch):
    api = adapter()
    event = {'tool': 'run_sotto_script', 'script': 'loops-query', 'status': 'unknown', 'error': 'none'}
    batch = {'events': [event], 'completedAtMs': 1791450000000}
    value = {'runtime': 'sotto', 'ready': True, 'providerLifecycle': 'active',
             'providerConnection': 'not_observed', 'feedback': {'available': False},
             'taskToolDiagnostics': batch}
    monkeypatch.setattr(api, '_client', lambda timeout: SimpleNamespace(
        call=lambda *a, **k: SimpleNamespace(ok=True, value=value)))
    assert api.runtime_observation()['runtime_health']['task_tool_diagnostics'] == {
        'events': [event], 'completed_at_ms': batch['completedAtMs']}
    invalid_events = [{**event, 'args': 'PRIVATE'}, {**event, 'status': 'PRIVATE'},
                      {**event, 'error': 'PRIVATE'}, {**event, 'script': 'PRIVATE'},
                      {**event, 'tool': 'PRIVATE'},
                      {'tool': 'run_sotto_script', 'status': 'unknown', 'error': 'none'},
                      {'tool': 'owner_task_close', 'status': 'recorded', 'error': 'none', 'script': 'loops-query'}]
    for invalid in ([{**batch, 'prompt': 'PRIVATE'}, {**batch, 'completedAtMs': True},
                     {**batch, 'completedAtMs': -1}, {**batch, 'events': [event] * 9}]
                    + [{**batch, 'events': [item]} for item in invalid_events]):
        value['taskToolDiagnostics'] = invalid
        result = api.runtime_observation()
        assert 'task_tool_diagnostics' not in result['runtime_health']
        assert 'PRIVATE' not in str(result)
    value['taskToolDiagnostics'] = {**batch, 'events': []}
    assert api.runtime_observation()['runtime_health']['task_tool_diagnostics']['events'] == []


def test_model_context_projection_is_bounded_and_content_free(monkeypatch):
    api = adapter()
    metrics = {'version': 1, 'mode': 'chat', 'outcome': 'success', 'completedAtMs': 123,
               'promptBytes': 1200, 'rememberedHistory': {'users': 2, 'assistants': 1},
               'lastMemoryPurge': {'reason': 'authority_unavailable', 'atMs': 122}}
    value = {'runtime': 'sotto', 'ready': True, 'providerLifecycle': 'active',
             'providerConnection': 'not_observed', 'feedback': {'available': False}, 'modelMetrics': metrics}
    monkeypatch.setattr(api, '_client', lambda timeout: SimpleNamespace(
        call=lambda *a, **k: SimpleNamespace(ok=True, value=value)))
    assert api.runtime_observation()['runtime_health']['model_context'] == {
        'completed_at_ms': 123, 'prompt_bytes': 1200,
        'remembered_history': {'users': 2, 'assistants': 1},
        'last_memory_purge': {'reason': 'authority_unavailable', 'at_ms': 122}}
    for invalid in ({**metrics, 'prompt': 'private'}, {**metrics, 'promptBytes': True},
                    {**metrics, 'rememberedHistory': {'users': -1, 'assistants': 1}},
                    {**metrics, 'rememberedHistory': {'users': 1, 'assistants': 1, 'text': 'private'}},
                    {**metrics, 'lastMemoryPurge': {'reason': 'secret error', 'atMs': 1}},
                    {**metrics, 'lastMemoryPurge': {'reason': 'revision_changed', 'atMs': 1, 'text': 'private'}}):
        value['modelMetrics'] = invalid
        assert 'model_context' not in api.runtime_observation()['runtime_health']


def test_task_diagnostic_rejection_is_a_closed_enum_not_receiver_text():
    runtime_api = adapter()
    event={'tool':'owner_task_resolve_many','status':'rejected','error':'none','rejection':'target_unavailable'}
    raw={'events':[event],'completedAtMs':1}
    assert runtime_api._task_tool_diagnostics_projection(raw)['events']==[event]
    for extra in ({'rejection':'PRIVATE CONTENT'},{'rejection':None},{'rejection':['target_unavailable']},
                  {'status':'recorded'},{'tool':'run_sotto_script','script':'loops-query'}, {'private':'secret'}):
        assert runtime_api._task_tool_diagnostics_projection(dict(raw,events=[dict(event,**extra)])) is None


def test_model_failure_projection_is_closed_and_content_free(monkeypatch):
    api = adapter()
    metrics = {'version': 1, 'mode': 'chat', 'outcome': 'failed', 'completedAtMs': 123,
               'promptBytes': 1200, 'failure': {'category': 'provider_api', 'httpStatus': 429}}
    value = {'runtime': 'sotto', 'ready': True, 'providerLifecycle': 'active',
             'providerConnection': 'not_observed', 'feedback': {'available': False}, 'modelMetrics': metrics}
    monkeypatch.setattr(api, '_client', lambda timeout: SimpleNamespace(
        call=lambda *a, **k: SimpleNamespace(ok=True, value=value)))
    assert api.runtime_observation()['runtime_health']['model_context']['failure'] == {
        'category': 'provider_api', 'http_status': 429}
    for failure in ({'category': 'private-error'}, {'category': 'unknown', 'message': 'private'},
                    {'category': 'provider_api', 'httpStatus': True},
                    {'category': 'provider_api', 'httpStatus': 600}):
        metrics['failure'] = failure
        assert 'model_context' not in api.runtime_observation()['runtime_health']


def timing_metrics():
    return {'version': 1, 'mode': 'chat', 'outcome': 'success', 'completedAtMs': 123,
            'promptBytes': 1200, 'stepCount': 2,
            'steps': [{'durationMs': 1100, 'modelMs': 600, 'inputTokens': 50,
                       'outputTokens': 12, 'finishReason': 'tool-calls', 'processorRetries': 0},
                      {'durationMs': 500, 'modelMs': 450, 'inputTokens': 80,
                       'outputTokens': 20, 'finishReason': 'stop', 'processorRetries': 2}],
            'tools': [{'tool': 'PRIVATE_TOOL_NAME', 'durationMs': 500, 'outcome': 'success'},
                      {'tool': 'other_tool', 'durationMs': 400, 'outcome': 'failed'}]}


def test_runtime_observation_sums_existing_model_tool_samples_without_names(monkeypatch):
    api = adapter()
    metrics = timing_metrics()
    value = {'runtime': 'sotto', 'ready': True, 'providerLifecycle': 'active',
             'providerConnection': 'not_observed', 'feedback': {'available': False}, 'modelMetrics': metrics}
    calls = []
    def call(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(ok=True, value=value)
    monkeypatch.setattr(api, '_client', lambda timeout: SimpleNamespace(call=call))
    observed = api.runtime_observation()
    assert observed['runtime_health']['model_context']['timing'] == {
        'step_count': 2, 'recorded_steps': 2, 'recorded_tools': 2,
        'model_ms': 1050, 'tool_ms': 900, 'processor_retries': 2}
    assert len(calls) == 1 and calls[0][0] == ('health',)
    assert 'PRIVATE' not in str(observed)
    assert 'finishReason' not in str(observed)
    # Failed runs retain the recorded samples; this is not success-only latency.
    metrics['outcome'] = 'failed'
    assert api.runtime_observation()['runtime_health']['model_context']['timing']['model_ms'] == 1050


def test_model_timing_empty_and_truncated_samples_are_explicit_and_bounded():
    api = adapter()
    metrics = timing_metrics()
    metrics.update(stepCount=0, steps=[], tools=[])
    assert api._model_context_projection(metrics)['timing'] == {
        'step_count': 0, 'recorded_steps': 0, 'recorded_tools': 0,
        'model_ms': 0, 'tool_ms': 0, 'processor_retries': 0}
    sample = dict(timing_metrics()['steps'][0], durationMs=300000, modelMs=300000, processorRetries=1000000000)
    metrics.update(stepCount=70, steps=[sample] * 60,
                   tools=[{'tool': 'read', 'durationMs': 300000, 'outcome': 'success'}] * 256)
    assert api._model_context_projection(metrics)['timing'] == {
        'step_count': 70, 'recorded_steps': 60, 'recorded_tools': 256,
        'model_ms': 18000000, 'tool_ms': 76800000, 'processor_retries': 60000000000}


def test_model_timing_rejects_unbounded_or_private_samples_without_losing_existing_context():
    api = adapter()
    original = timing_metrics()
    mutations = [
        lambda m: m.update(stepCount=True), lambda m: m.update(stepCount=-1),
        lambda m: m.update(stepCount=1000000001), lambda m: m.update(stepCount=1),
        lambda m: m.update(steps=m['steps'] * 31), lambda m: m.update(tools=m['tools'] * 129),
        lambda m: m.update(steps=None), lambda m: m.update(tools={}),
        lambda m: m['steps'][0].update(private='PRIVATE'),
        lambda m: m['steps'][0].update(finishReason='PRIVATE'),
        lambda m: m['tools'][0].update(args='PRIVATE'),
        lambda m: m['tools'][0].update(tool='https://PRIVATE'),
        lambda m: m['tools'][0].update(outcome='PRIVATE'),
        lambda m: m['tools'][0].update(durationMs=300001),
    ]
    for field in ('modelMs', 'durationMs', 'processorRetries', 'inputTokens', 'outputTokens'):
        for bad in (True, -1, float('nan'), float('inf'), 1.5, 'PRIVATE', 1000000001):
            mutations.append(lambda m, field=field, bad=bad: m['steps'][0].update({field: bad}))
    for mutate in mutations:
        metrics = deepcopy(original)
        mutate(metrics)
        result = api._model_context_projection(metrics)
        assert result == {'completed_at_ms': 123, 'prompt_bytes': 1200}
        assert 'PRIVATE' not in str(result)


def test_runtime_intake_diagnostics_are_counts_only(monkeypatch):
    api = adapter()
    value = {'runtime': 'sotto', 'ready': False, 'providerLifecycle': 'failed',
             'providerConnection': 'not_observed', 'feedback': {'available': False},
             'providerIntake': {'receivedEvents': 0, 'handedOffEvents': 0,
                               'listenerStarts': 1, 'listenerStops': 1,
                               'streamErrors': 1, 'handlerErrors': 0}}
    monkeypatch.setattr(api, '_client', lambda **_: SimpleNamespace(
        call=lambda *_args, **_kwargs: SimpleNamespace(ok=True, value=value)))
    assert api.runtime_observation()['runtime_health']['provider_intake'] == value['providerIntake']
    for invalid in ({'receivedEvents': 0, 'handedOffEvents': 0, 'message': 'private'},
                    {'receivedEvents': True, 'handedOffEvents': 0},
                    {'receivedEvents': -1, 'handedOffEvents': 0}):
        value['providerIntake'] = invalid
        assert 'provider_intake' not in api.runtime_observation()['runtime_health']


def test_google_read_stage_health_projection_is_closed_and_bounded():
    api = adapter()
    event = {'tool': 'gmail_search', 'status': 'unknown', 'error': 'tool_error',
             'google_read_stage': 'provider_forbidden'}
    raw = {'events': [event], 'completedAtMs': 1}
    assert api._task_tool_diagnostics_projection(raw)['events'] == [event]
    for extra in ({'google_read_stage': 'PRIVATE'}, {'google_read_stage': ['provider_forbidden']},
                  {'tool': 'read_writing_style'}, {'error': 'none'}, {'private_query': 'PRIVATE'}):
        assert api._task_tool_diagnostics_projection({**raw, 'events': [{**event, **extra}]}) is None
    assert len(api._task_tool_diagnostics_projection({**raw, 'events': [event] * 8})['events']) == 8
    assert api._task_tool_diagnostics_projection({**raw, 'events': [event] * 9}) is None


def test_google_http_status_phase_projection_preserves_only_closed_bounded_fields():
    api = adapter()
    event = {'tool': 'gmail_search', 'status': 'unknown', 'error': 'tool_error',
             'google_read_stage': 'provider_http_other', 'google_read_http_status': 404,
             'google_read_phase': 'search_metadata'}
    raw = {'events': [event], 'completedAtMs': 1}
    assert api._task_tool_diagnostics_projection(raw)['events'] == [event]
    for extra in ({'google_read_http_status': True}, {'google_read_http_status': '404'},
                  {'google_read_http_status': 99}, {'google_read_http_status': 600},
                  {'google_read_http_status': 404.5}, {'google_read_phase': 'message_get'},
                  {'google_read_phase': ['search_list']}, {'google_read_phase': 'PRIVATE'},
                  {'google_read_stage': None}, {'tool': 'read_writing_style'}):
        assert api._task_tool_diagnostics_projection({**raw, 'events': [{**event, **extra}]}) is None


def test_cleanup_diagnostic_projection_is_fixed_content_free_and_tool_scoped():
    runtime_api = adapter()
    event = {'tool': 'owner_task_cleanup', 'status': 'unknown', 'error': 'tool_error',
             'cleanup_stage': 'receiver_perform', 'cleanup_category': 'unavailable', 'cleanup_http_status': 503}
    raw = {'events': [event], 'completedAtMs': 1}
    assert runtime_api._task_tool_diagnostics_projection(raw)['events'] == [event]
    for extra in ({'cleanup_stage': 'PRIVATE'}, {'cleanup_category': 'PRIVATE'}, {'cleanup_http_status': True},
                  {'tool': 'owner_task_close'}, {'owner_text': 'PRIVATE'}):
        assert runtime_api._task_tool_diagnostics_projection({**raw, 'events': [{**event, **extra}]}) is None
