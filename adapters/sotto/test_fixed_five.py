"""Offline selected fixed-procedure profile checks; no live provider or model."""
import importlib.util
import json
import itertools
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_process_handoff import authorities, photon_manifest, stable_identity
from process_handoff import build_child_handoff, HandoffError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'runtime' / 'trigger-receiver'))
SPEC = importlib.util.spec_from_file_location('sotto_fixed_five_test', HERE / 'fixed_five.py')
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)
# Source-tree fixtures are an explicit in-process test seam; no environment override exists.
profile._PACKAGED_ROOT = ROOT / 'sotto-chief-of-staff'
profile._PACKAGED_RECEIVER = ROOT / 'runtime' / 'trigger-receiver'
profile._image_owned = lambda _meta: True
# Source-tree tests cannot create root/UID10000/UID10001-owned directories.
# The separate private-PID image fixture exercises the real kernel mode check.
_real_validate_role_layout = profile.validate_role_layout
profile.validate_role_layout = lambda _root: None
STARTUP = importlib.util.spec_from_file_location('sotto_startup_fixed_five', HERE / 'startup_config.py')
startup = importlib.util.module_from_spec(STARTUP)
STARTUP.loader.exec_module(startup)
RUNTIME = importlib.util.spec_from_file_location('sotto_runtime_fixed_five', HERE / 'runtime_api.py')
runtime = importlib.util.module_from_spec(RUNTIME)
RUNTIME.loader.exec_module(runtime)
runtime._fixed_five_module = lambda: profile


def environment(tmp_path):
    root = tmp_path / 'data'
    root.mkdir()
    root.chmod(0o700)
    (root / 'sotto-home').mkdir(mode=0o700)
    return {
        'SOTTO_RUNTIME_PROFILE': profile.PROFILE,
        'SOTTO_DEPLOYMENT_MODE': 'managed', 'SOTTO_CRON_DELIVER': 'photon',
        'SOTTO_TENANT_ID': 'tenant-fixture', 'SOTTO_DATA': str(root),
        'SOTTO_HOME': str(root / 'sotto-home'), 'SOTTO_SKILLS_ROOT': str(ROOT / 'sotto-chief-of-staff'),
        'SOTTO_CHAT_RUNTIME_URL': 'http://127.0.0.1:19981',
        'SOTTO_CHAT_RUNTIME_TOKEN': 'runtime-api-fixture-token-0123456789',
        'SOTTO_TRIGGER_PORT': '19982', 'SOTTO_MCP_TOKEN': 'm' * 32,
        'SOTTO_CONTROL_TOKEN': 'c' * 32, 'SOTTO_MODEL_PROXY_URL': 'https://proxy.example',
        'SOTTO_MODEL_PROXY_TOKEN': 'p' * 32,
        'PHOTON_HOME_CHANNEL': '+15551234567', 'PHOTON_ALLOWED_USERS': '+15551234567',
        'PHOTON_PROJECT_ID': 'synthetic-project',
        'PHOTON_PROJECT_SECRET': 'photon-project-secret-fixture',
    }


def test_five_schedule_and_packaged_procedures_are_exact(tmp_path):
    env = environment(tmp_path)
    assert profile.validate_offline(env) == Path(env['SOTTO_DATA'])
    for name, value in [('SOTTO_PROACTIVE_CRON', '*/5 * * * *'),
                        ('SOTTO_DIGEST', '0'), ('SOTTO_RUN_SKILL', 'hermes -z'),
                        ('SOTTO_CRON_DELIVER', 'telegram')]:
        changed = {**env, name: value}
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_offline(changed)
    spec = tmp_path / 'crons.json'
    rows = json.loads((ROOT / 'adapters/sotto/crons.json').read_text())
    rows[0]['prompt'] = 'Different instruction'
    spec.write_text(json.dumps(rows))
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_schedules(env, spec)


@pytest.mark.skipif(os.geteuid() != 0, reason='role ownership needs root to chown')
def test_role_layout_requires_private_role_and_credential_home_directories(tmp_path):
    def layout():
        root = tmp_path / 'tenant'
        root.mkdir()
        os.chown(root, 0, 10001)
        root.chmod(0o1771)
        for name, uid in (('node-config', 10000), ('node-state', 10000),
                          ('node-media', 10000), ('runtime', 10001), ('sotto-home', 10001)):
            (root / name).mkdir(mode=0o700)
            os.chown(root / name, uid, uid)
        return root

    root = layout()
    _real_validate_role_layout(root)
    home = root / 'sotto-home'
    for change in (lambda: home.chmod(0o750), lambda: os.chown(home, 10000, 10000),
                   lambda: os.chown(home, 10001, 0), lambda: home.rmdir()):
        change()
        with pytest.raises(profile.ProfileUnavailableError):
            _real_validate_role_layout(root)
        subprocess.run(['rm', '-rf', str(root)], check=True)
        root = layout()
        home = root / 'sotto-home'
    home.rmdir()
    home.symlink_to(root / 'runtime')
    with pytest.raises(profile.ProfileUnavailableError):
        _real_validate_role_layout(root)


def test_active_queue_checks_nested_pack_and_refuses_routine_cursors(tmp_path):
    env = environment(tmp_path)
    dbpath = Path(env['SOTTO_DATA']) / 'events/work.sqlite3'
    dbpath.parent.mkdir()
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE jobs (kind TEXT,payload TEXT,status TEXT)')
        db.execute('CREATE TABLE routine_slots (slot_hash TEXT)')
        db.execute('CREATE TABLE routine_cursors (cursor_hash TEXT)')
        payload = {'runner': [sys.executable, str(ROOT / 'runtime/trigger-receiver/procedure_runner.py')],
                   'prompt': json.dumps({'pack': env['SOTTO_SKILLS_ROOT'], 'kind': 'digest'}),
                   'label': 'cron:sotto-midday-digest', 'decision_ids': []}
        db.execute('INSERT INTO jobs VALUES (?,?,?)', ('run', json.dumps(payload), 'pending'))
    dbpath.chmod(0o600)
    profile.validate_offline(env)
    payload['prompt'] = json.dumps({'pack': str(tmp_path / 'foreign'), 'kind': 'digest'})
    with sqlite3.connect(dbpath) as db:
        db.execute('UPDATE jobs SET payload=?', (json.dumps(payload),))
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_offline(env)
    payload['prompt'] = json.dumps({'pack': env['SOTTO_SKILLS_ROOT'], 'kind': 'digest'})
    with sqlite3.connect(dbpath) as db:
        db.execute('UPDATE jobs SET payload=?', (json.dumps(payload),))
        db.execute('INSERT INTO routine_cursors VALUES (?)', ('held',))
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_offline(env)


@pytest.mark.parametrize('direction', [None, 'focusing on tomorrow’s pricing decision'])
def test_requested_brief_survives_queue_restart_with_closed_runner_shape(tmp_path, direction):
    env = environment(tmp_path)
    dbpath = Path(env['SOTTO_DATA']) / 'events/work.sqlite3'
    dbpath.parent.mkdir()
    label = 'requested:' + 'a' * 32 + ':sotto-morning-brief'
    request = {'pack': env['SOTTO_SKILLS_ROOT'], 'kind': 'morning',
               'day': '2026-10-03', 'work_key': label, 'refresh': False}
    if direction is not None:
        request['composition_direction'] = direction
    payload = {'runner': [sys.executable, str(ROOT / 'runtime/trigger-receiver/brief_runner.py')],
               'prompt': json.dumps(request), 'label': label, 'decision_ids': []}
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE jobs (kind TEXT,payload TEXT,status TEXT)')
        db.execute('CREATE TABLE routine_slots (slot_hash TEXT)')
        db.execute('CREATE TABLE routine_cursors (cursor_hash TEXT)')
        db.execute('INSERT INTO jobs VALUES (?,?,?)', ('run', json.dumps(payload), 'pending'))
    dbpath.chmod(0o600)
    profile.validate_offline(env, allow_supported_history=True)
    for field, value in [('kind', 'evening'), ('kind', 'welcome'),
                         ('refresh', 'false'), ('refresh', 1),
                         ('day', '2026-02-30'), ('work_key', 'unrelated'),
                         ('pack', '/tmp/foreign'), ('target', 'foreign'),
                         ('payload_path', '/tmp/untrusted'), ('prepare', True),
                         ('composition_direction', ''), ('composition_direction', ' '),
                         ('composition_direction', 1), ('composition_direction', None),
                         ('composition_direction', 'x' * 16_001)]:
        changed = {**payload, 'prompt': json.dumps({**request, field: value})}
        with sqlite3.connect(dbpath) as db:
            db.execute('UPDATE jobs SET payload=?', (json.dumps(changed),))
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_offline(env, allow_supported_history=True)
    for changed_label in ['requested:abc:sotto-morning-brief',
                          'requested:' + 'A' * 32 + ':sotto-morning-brief',
                          label + ':extra', 'cron:sotto-morning-brief']:
        changed = {**payload, 'label': changed_label,
                   'prompt': json.dumps({**request, 'work_key': changed_label})}
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_work('run', changed, env)
    evening_label = label.replace('morning', 'evening')
    profile.validate_work('run', {**payload, 'label': evening_label,
        'prompt': json.dumps({**request, 'kind': 'evening', 'refresh': True,
                              'work_key': evening_label})}, env)


def test_configured_identity_is_checked_without_activation_ack(tmp_path):
    env = environment(tmp_path)
    manifest = photon_manifest()
    config, _ = startup.render(manifest)
    expected = startup.receiver_channel_expectation(config)
    runtime = Path(env['SOTTO_DATA']) / 'runtime'
    startup.stage_receiver_channel_expectation(runtime, expected)
    class Client:
        url = 'http://127.0.0.1:19981'
        calls = 0
        def verify_configured_identity(self, supplied):
            self.calls += 1
            assert supplied['owner'] == manifest['owner']
            assert supplied['credentials']['photonProjectSecret'] == env['PHOTON_PROJECT_SECRET']
            return True
    client = Client()
    profile.verify_configured_channel(env, client, timeout=0)
    assert client.calls == 1
    changed = {**env, 'PHOTON_PROJECT_ID': 'foreign-project'}
    with pytest.raises(profile.ProfileUnavailableError):
        profile.verify_configured_channel(changed, client, timeout=0)


def test_cold_node_retry_can_pass_after_five_seconds_but_permanent_failure_is_bounded(
        tmp_path, monkeypatch):
    env = environment(tmp_path)
    config, _ = startup.render(photon_manifest())
    startup.stage_receiver_channel_expectation(
        Path(env['SOTTO_DATA']) / 'runtime', startup.receiver_channel_expectation(config))
    elapsed = itertools.count()
    monkeypatch.setattr(profile, 'time', SimpleNamespace(
        monotonic=lambda: next(elapsed), sleep=lambda _seconds: None))
    class LateClient:
        url = env['SOTTO_CHAT_RUNTIME_URL']
        calls = 0
        def verify_configured_identity(self, _expected):
            self.calls += 1
            if self.calls < 4:
                raise RuntimeError('synthetic cold Node')
            return True
    client = LateClient()
    profile.verify_configured_channel(env, client, timeout=30)
    assert client.calls == 4
    expired = itertools.count(step=5)
    monkeypatch.setattr(profile, 'time', SimpleNamespace(
        monotonic=lambda: next(expired), sleep=lambda _seconds: None))
    class MissingClient:
        url = env['SOTTO_CHAT_RUNTIME_URL']
        calls = 0
        def verify_configured_identity(self, _expected):
            self.calls += 1
            raise RuntimeError('synthetic wrong or absent Node')
    missing = MissingClient()
    with pytest.raises(profile.ProfileUnavailableError):
        profile.verify_configured_channel(env, missing, timeout=30)
    assert missing.calls == 4


def test_selected_runtime_readiness_checks_real_profile_not_bypass(tmp_path, monkeypatch):
    env = environment(tmp_path)
    config, _ = startup.render(photon_manifest())
    startup.stage_receiver_channel_expectation(
        Path(env['SOTTO_DATA']) / 'runtime', startup.receiver_channel_expectation(config))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    class Client:
        url = env['SOTTO_CHAT_RUNTIME_URL']
        def verify_configured_identity(self, supplied):
            assert supplied['owner'] == config['owner']
            return True
    monkeypatch.setattr(runtime, '_client', lambda timeout=60: Client())
    assert runtime.runtime_readiness() == (True, '')
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'telegram')
    assert runtime.runtime_readiness()[0] is False
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'photon')
    assert runtime.runtime_readiness() == (True, '')


def test_handoff_profile_is_opt_in_only():
    manifest = photon_manifest()
    config, _ = startup.render(manifest)
    arguments = dict(stable_identity=stable_identity(config),
                     node_config_path='/run/node/runtime-config.json',
                     data_directory='/data', skills_root='/app/sotto-skills')
    base = build_child_handoff(manifest, authorities(config), **arguments)
    assert 'SOTTO_RUNTIME_PROFILE' not in base.receiver_env
    with pytest.raises(HandoffError):
        build_child_handoff(manifest, authorities(config),
                            limited_profile=profile.PROFILE, **arguments)
    limited_manifest = {**manifest, 'stateDirectory': '/data/node-state',
                        'mediaRoot': '/data/node-media'}
    limited_config, _ = startup.render(limited_manifest)
    limited = build_child_handoff(limited_manifest, authorities(limited_config),
                                  stable_identity=stable_identity(limited_config),
                                  node_config_path='/data/node-config/config/runtime-config.json',
                                  data_directory='/data', skills_root='/app/sotto-skills',
                                  limited_profile=profile.PROFILE)
    assert limited.receiver_env['SOTTO_RUNTIME_PROFILE'] == profile.PROFILE
    for wrong in (
            {**limited_manifest, 'mediaRoot': '/other/media'},
            {**limited_manifest, 'stateDirectory': '/data/runtime/state'}):
        changed, _ = startup.render(wrong)
        with pytest.raises(HandoffError):
            build_child_handoff(wrong, authorities(changed),
                                stable_identity=stable_identity(changed),
                                node_config_path='/data/node-config/config/runtime-config.json',
                                data_directory='/data', skills_root='/app/sotto-skills',
                                limited_profile=profile.PROFILE)
    with pytest.raises(HandoffError):
        build_child_handoff(manifest, authorities(config), limited_profile='generic', **arguments)


def test_image_root_is_fixed_not_an_environment_choice(tmp_path):
    env = environment(tmp_path)
    profile.validate_environment(env)
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_environment({**env, 'SOTTO_SKILLS_ROOT': str(tmp_path / 'skills')})
    original = profile._image_owned
    try:
        profile._image_owned = lambda _meta: False
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_environment(env)
    finally:
        profile._image_owned = original


def test_receiver_vault_removes_control_token_before_readiness(tmp_path):
    env = environment(tmp_path)
    env.pop('SOTTO_CONTROL_TOKEN')
    # The trusted handoff validates the root token, while managed_exec captures
    # it into control_vault and removes it before the receiver imports this API.
    assert profile.validate_offline(env) == Path(env['SOTTO_DATA'])


def test_offline_receiver_main_starts_after_node_identity_and_stops_cleanly(tmp_path, monkeypatch):
    """Exercise the real receiver startup ordering with synthetic Node and no provider IO."""
    import managed  # noqa: PLC0415
    from test_fixed_five_receiver import rec  # noqa: PLC0415
    env = environment(tmp_path)
    env.pop('SOTTO_CONTROL_TOKEN')
    config, _ = startup.render(photon_manifest())
    startup.stage_receiver_channel_expectation(
        Path(env['SOTTO_DATA']) / 'runtime', startup.receiver_channel_expectation(config))
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(rec, 'DATA', env['SOTTO_DATA'])
    events = []

    class NodeClient:
        url = env['SOTTO_CHAT_RUNTIME_URL']
        def verify_configured_identity(self, supplied):
            assert supplied['owner'] == config['owner']
            events.append('node-identity')
            return True

    monkeypatch.setattr(runtime, '_client', lambda timeout=60: NodeClient())
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: runtime)
    monkeypatch.setattr(rec.CONNECTORS, 'service_status', lambda: [])
    monkeypatch.setattr(managed, 'reconcile_connector_capabilities', lambda *_: None)
    monkeypatch.setattr(rec, '_shared_effects', lambda: type('Effects', (), {'valid': lambda self, _: True})())
    monkeypatch.setattr(rec, 'resolve_setup_code', lambda: 'synthetic-only')
    monkeypatch.setattr(rec.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(rec, '_backfill_google_account_email', lambda: None)
    for name in ('start_gmail_poll_thread', 'start_valve_thread',
                 'start_work_thread', 'start_cron_thread', 'start_update_check_thread'):
        monkeypatch.setattr(rec, name, lambda name=name: events.append(name))
    monkeypatch.setattr(rec.OUTBOX, 'start_drain_thread', lambda: events.append('outbox'))
    monkeypatch.setattr(rec.CALCACHE, 'start_refresh_thread', lambda: events.append('calendar'))

    class LocalServer:
        def __init__(self, address, _handler):
            assert address == ('127.0.0.1', int(env['SOTTO_TRIGGER_PORT']))
        def serve_forever(self):
            events.append('listening')

    monkeypatch.setattr(rec, 'ThreadingHTTPServer', LocalServer)
    rec.main()
    assert events[0] == 'node-identity'
    assert events[-1] == 'listening'
    assert 'start_cron_thread' in events and 'start_work_thread' in events


def test_receiver_cold_start_refuses_before_threads_when_node_identity_unavailable(tmp_path, monkeypatch):
    import managed  # noqa: PLC0415
    from test_fixed_five_receiver import rec  # noqa: PLC0415
    env = environment(tmp_path)
    env.pop('SOTTO_CONTROL_TOKEN')
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: runtime)
    monkeypatch.setattr(rec.CONNECTORS, 'service_status', lambda: [])
    monkeypatch.setattr(managed, 'reconcile_connector_capabilities', lambda *_: None)
    monkeypatch.setattr(profile, 'verify_configured_channel',
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            profile.ProfileUnavailableError('synthetic Node unavailable')))
    monkeypatch.setattr(rec, 'start_cron_thread', lambda: pytest.fail('scheduler started'))
    monkeypatch.setattr(rec, 'start_work_thread', lambda: pytest.fail('worker started'))
    with pytest.raises(SystemExit, match='managed Photon fixed procedure profile unavailable'):
        rec.main()


def test_fresh_profile_holds_existing_outbox_effects_and_malformed_rows(tmp_path):
    env = environment(tmp_path)
    events = Path(env['SOTTO_DATA']) / 'events'
    events.mkdir()
    events.chmod(0o700)
    path = events / 'outbox.json'
    for document in (
            {'rows': [{'id': 'pending', 'status': 'pending'}]},
            {'rows': [{'id': 'accepted', 'status': 'delivered', 'effects_pending': True}]},
            {'rows': [{'id': 'unknown', 'status': 'failed', 'acceptance': 'unknown'}]},
            {'rows': [{'id': 'old', 'status': 'delivered'}]},
            {'rows': 'not-a-list'},
            {'rows': [], 'unexpected': True}):
        path.write_text(json.dumps(document))
        path.chmod(0o600)
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_offline(env)
    path.write_text('{"rows":[],"rows":[]}')
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_offline(env)
    path.unlink()
    path.symlink_to(tmp_path / 'foreign')
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_offline(env)
    path.unlink()
    path.write_text('{"rows":[]}')
    path.chmod(0o600)
    profile.validate_offline(env)


def test_provisioning_proves_current_node_without_owner_activation(tmp_path, monkeypatch):
    env = environment(tmp_path)
    config, _ = startup.render(photon_manifest())
    startup.stage_receiver_channel_expectation(
        Path(env['SOTTO_DATA']) / 'runtime', startup.receiver_channel_expectation(config))
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    class Client:
        url = env['SOTTO_CHAT_RUNTIME_URL']
        calls = 0
        unavailable = False

        def verify_configured_identity(self, expected):
            self.calls += 1
            if self.unavailable:
                raise RuntimeError('Node unavailable')
            assert expected['owner'] == config['owner']

    client = Client()
    monkeypatch.setattr(runtime, '_client', lambda **_: client)
    assert runtime.provisioning_status() == {
        'name': 'sotto', 'profile': profile.PROFILE, 'configured': True}
    assert client.calls == 1
    assert not (Path(env['SOTTO_DATA']) / 'config/photon-activation.json').exists()
    client.unavailable = True
    with pytest.raises(profile.ProfileUnavailableError):
        runtime.provisioning_status()
    assert client.calls == 2
    client.unavailable = False
    monkeypatch.setenv('PHOTON_PROJECT_ID', 'changed-project')
    with pytest.raises(profile.ProfileUnavailableError):
        runtime.provisioning_status()
    assert client.calls == 2


def test_focused_meeting_prep_is_closed_photon_job_shape(tmp_path):
    env = environment(tmp_path)
    offer = {'offer_id': 'a'*64, 'kind': 'meeting_prep', 'target': 'photon',
             'chat_id': 'owner-chat', 'accepted_at': 1,
             'meeting': {'event_id': 'calendar-event', 'start': '2026-10-06T18:00:00+00:00'}}
    label = 'meeting-prep:' + offer['offer_id']
    request = {'kind': 'focused-meeting-prep', 'pack': env['SOTTO_SKILLS_ROOT'],
               'offer': offer, 'work_key': label}
    payload = {'runner': [sys.executable, str(ROOT/'runtime/trigger-receiver/procedure_runner.py')],
               'prompt': json.dumps(request), 'label': label, 'decision_ids': []}
    profile.validate_work('run', payload, env)
    for changed in ({**request, 'pack': '/foreign'}, {**request, 'work_key': 'other'},
                    {**request, 'offer': {**offer, 'meeting': {'event_id': 'calendar-event'}}}):
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_work('run', {**payload, 'prompt': json.dumps(changed)}, env)
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_work('run', payload, {**env, 'SOTTO_RUNTIME_PROFILE': 'self-host-telegram-fixed-five-v1'})


def test_shared_meeting_prep_is_closed_photon_skill_job(tmp_path):
    env = environment(tmp_path)
    key = 'skill:' + 'a'*64
    request = {'kind':'shared-meeting-prep','pack':env['SOTTO_SKILLS_ROOT'],
               'selection':{'mode':'focus','focus':'Dana'},'work_key':key}
    payload = {'runner':[sys.executable,str(ROOT/'runtime/trigger-receiver/procedure_runner.py')],
               'prompt':json.dumps(request),'label':key+':meeting-prep','decision_ids':[]}
    profile.validate_work('run',payload,env)
    for changed in ({**request,'pack':'/foreign'},{**request,'work_key':'skill:wrong'},
                    {**request,'selection':{'mode':'sweep','script':'/tmp/x'}},
                    {**request,'selection':{'mode':'focus','focus':'Dana\n'}},
                    {**request,'selection':{'mode':'focus','focus':'Dana','event_id':'model-selected'}}):
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_work('run',{**payload,'prompt':json.dumps(changed)},env)
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_work('run',payload,{**env,'SOTTO_RUNTIME_PROFILE':'self-host-telegram-fixed-five-v1'})


@pytest.mark.parametrize('valid,meeting', [
    (True, {'event_id': 'calendar-id', 'start': '2026-10-28T15:00:00Z'}),
    (True, {'event_id': 'calendar-id', 'start': '2026-10-28T08:00:00-07:00'}),
    (False, {'event_id': 'calendar-id', 'start': 'tomorrow'}),
    (False, {'event_id': 'calendar-id', 'start': '2026-10-28T15:00:00'}),
    (False, {'event_id': 'calendar-id', 'start': '2026-10-28'}),
    (False, {'event_id': 'calendar-id\n', 'start': '2026-10-28T15:00:00Z'}),
    (False, {'event_id': 'x' * 513, 'start': '2026-10-28T15:00:00Z'}),
    (False, {'event_id': 'calendar-id', 'start': '2026-10-28T15:00:00Z', 'script': '/tmp/x'}),
    (False, {'start': '2026-10-28T15:00:00Z'}),
    (False, None),
])
def test_shared_prep_persisted_occurrence_uses_admission_schema(tmp_path, valid, meeting):
    env = environment(tmp_path)
    key = 'skill:' + 'a' * 64
    selection = {'mode': 'focus', 'focus': 'Dana', 'meeting': meeting}
    request = {'kind': 'shared-meeting-prep', 'pack': env['SOTTO_SKILLS_ROOT'],
               'selection': selection, 'work_key': key}
    payload = {'runner': [sys.executable, str(ROOT / 'runtime/trigger-receiver/procedure_runner.py')],
               'prompt': json.dumps(request), 'label': key + ':meeting-prep', 'decision_ids': []}
    # Explicit outcomes catch accidental refusal of every valid occurrence.
    if valid:
        profile.validate_work('run', payload, env)
    else:
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_work('run', payload, env)
    request['selection'] = {'mode': 'sweep', 'meeting': meeting}
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_work('run', {**payload, 'prompt': json.dumps(request)}, env)


def test_shared_ask_is_closed_photon_original_question_job(tmp_path):
    env=environment(tmp_path); key='skill:'+'b'*64
    request={'kind':'shared-ask-research','pack':env['SOTTO_SKILLS_ROOT'],
             'selection':{'mode':'person','person':'Dana'},'question':'Research Dana',
             'work_key':key}
    payload={'runner':[sys.executable,str(ROOT/'runtime/trigger-receiver/procedure_runner.py')],
             'prompt':json.dumps(request),'label':key+':ask-research','decision_ids':[]}
    profile.validate_work('run',payload,env)
    for change in ({**request,'question':'Other person'}, {**request,'pack':'/foreign'},
                   {**request,'selection':{'mode':'person','person':'Dana','email':'model@example.com'}},
                   {**request,'question':False}):
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_work('run',{**payload,'prompt':json.dumps(change)},env)
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_work('run',payload,{**env,'SOTTO_RUNTIME_PROFILE':'self-host-telegram-fixed-five-v1'})


def test_shared_followup_closed_lanes_and_photon_profile(tmp_path):
    env = environment(tmp_path)
    key = 'skill:'+'c'*64
    request = {'kind': 'shared-followup', 'pack': env['SOTTO_SKILLS_ROOT'],
               'selection': {'mode': 'recent'}, 'work_key': key}
    payload = {'runner': [sys.executable, str(ROOT/'runtime/trigger-receiver/procedure_runner.py')],
               'prompt': json.dumps(request), 'label': key+':followup', 'decision_ids': []}
    for mode in ('recent', 'backlog'):
        profile.validate_work('run', {**payload, 'prompt': json.dumps({**request, 'selection': {'mode': mode}})}, env)
    for changed in ({**request, 'pack': '/foreign'}, {**request, 'selection': {'mode': 'other'}},
                    {**request, 'selection': {'mode': 'recent', 'path': '/tmp/x'}}):
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_work('run', {**payload, 'prompt': json.dumps(changed)}, env)
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_work('run', payload, {**env, 'SOTTO_RUNTIME_PROFILE': 'self-host-telegram-fixed-five-v1'})


def test_shared_company_ask_original_profile_accepts_only_closed_owner_span(tmp_path):
    env = environment(tmp_path)
    key = 'skill:' + 'b'*64
    request = {'kind':'shared-ask-research', 'pack':env['SOTTO_SKILLS_ROOT'],
        'selection':{'mode':'company', 'company':'Cedar Labs'},
        'question':'Research Cedar Labs', 'work_key':key}
    payload = {'runner':[sys.executable,str(ROOT/'runtime/trigger-receiver/procedure_runner.py')],
        'prompt':json.dumps(request), 'label':key+':ask-research', 'decision_ids':[]}
    for runtime_profile in ('managed-photon-fixed-five-v1', 'self-host-photon-fixed-five-v1'):
        profile.validate_work('run', payload, {**env,'SOTTO_RUNTIME_PROFILE':runtime_profile})
    for change in ({**request,'question':'Research something else'},
        {**request,'selection':{'mode':'company','company':'Cedar Labs','person':'Dana'}},
        *({**request,'selection':{'mode':'company','company':value}, 'question':'Research '+value}
          for value in (' Cedar Labs', 'Cedar\nLabs', 'Cedar\x85Labs', 'Cedar\u2028Labs', 'x'*201))):
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_work('run', {**payload,'prompt':json.dumps(change)}, env)
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_work('run', payload, {**env,'SOTTO_RUNTIME_PROFILE':'self-host-telegram-fixed-five-v1'})


def _crash_mid_transaction(tmp_path):
    """A receiver SIGKILLed while its write transaction has spilled pages to the database."""
    env = environment(tmp_path)
    data = Path(env['SOTTO_DATA'])
    spec = importlib.util.spec_from_file_location(
        'fixed_five_hot_journal_queue', ROOT / 'sotto-chief-of-staff/_shared/lib/work_queue.py')
    work_queue = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(work_queue)
    job = work_queue.enqueue(data, 'run', {
        'runner': [sys.executable, str(ROOT / 'runtime/trigger-receiver/procedure_runner.py')],
        'prompt': json.dumps({'pack': env['SOTTO_SKILLS_ROOT'], 'kind': 'digest'}),
        'label': 'cron:sotto-midday-digest', 'decision_ids': []}, key='committed')
    db = data / 'events' / 'work.sqlite3'
    script = ("import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1], isolation_level=None);"
              "c.execute('PRAGMA cache_size=1'); c.execute('BEGIN IMMEDIATE');"
              "c.execute('CREATE TABLE IF NOT EXISTS filler(x)');"
              "[c.execute('INSERT INTO filler VALUES (?)', ('x'*4000,)) for _ in range(200)];"
              "os._exit(0)")
    subprocess.run([sys.executable, '-c', script, str(db)], check=True)
    journal = data / 'events' / 'work.sqlite3-journal'
    assert journal.stat().st_size > 0
    return env, data, db, journal, job


def test_restart_preflight_rolls_back_hot_journal_left_by_killed_receiver(tmp_path):
    env, data, db, journal, job = _crash_mid_transaction(tmp_path)
    # A read-only open cannot roll back a hot journal: this was a permanent restart refusal.
    with pytest.raises(sqlite3.OperationalError):
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as ro:
            ro.execute('SELECT count(*) FROM jobs').fetchone()
    profile.validate_work_queue(data, env, allow_supported_history=True)   # same-epoch restart
    assert not journal.exists()
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as ro:
        assert ro.execute('SELECT id FROM jobs').fetchall() == [(job,)]
        assert ro.execute("SELECT 1 FROM sqlite_master WHERE name='filler'").fetchone() is None
    profile.validate_offline(env, allow_supported_history=True)


def test_hot_journal_recovery_never_runs_as_another_uid(tmp_path):
    _env, data, _db, journal, _job = _crash_mid_transaction(tmp_path)
    # The root supervisor validates the UID10001 queue read-only; only the receiver UID's
    # own stager may let SQLite rewrite that database.
    with pytest.raises(profile.ProfileUnavailableError):
        profile.recover_work_queue_journal(data, expected_uid=os.geteuid() + 1)
    assert journal.stat().st_size > 0
    journal.chmod(0o622)
    with pytest.raises(profile.ProfileUnavailableError):
        profile.recover_work_queue_journal(data)
    assert journal.exists()
    journal.chmod(0o600)
    profile.recover_work_queue_journal(data)
    assert not journal.exists()
    profile.recover_work_queue_journal(data)   # no journal: a no-op


def test_history_discovery_has_closed_runner_request_and_label(tmp_path):
    env = environment(tmp_path)
    label = 'onboarding:history-discovery:sotto-welcome-brief'
    request = {'pack': env['SOTTO_SKILLS_ROOT'], 'kind': 'welcome', 'day': '2026-10-10',
               'work_key': 'onboarding:history-discovery:v1:2026-10-10', 'history_discovery': True}
    payload = {'runner': [sys.executable, str(ROOT / 'runtime/trigger-receiver/brief_runner.py')],
               'prompt': json.dumps(request), 'label': label, 'decision_ids': []}
    profile.validate_work('run', payload, env)
    for patch in ({'history_discovery': False}, {'history_discovery': 1},
                  {'kind': 'morning'}, {'work_key': 'ordinary-welcome'}, {'prepare': True}):
        with pytest.raises(profile.ProfileUnavailableError):
            profile.validate_work('run', {**payload, 'prompt': json.dumps({**request, **patch})}, env)
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_work('run', {**payload, 'label': 'onboarding:sotto-welcome-brief'}, env)


def test_exact_calendar_scope_fixed_gate_isolated_without_action_imports(tmp_path):
    env = environment(tmp_path)
    code = """import importlib.util,json,pathlib,sys
root=pathlib.Path(sys.argv[1])
def guard(event,args):
 if event=='import' and args[0] in ('runtime_owner_calendar_tool','owner_google_effect','runtime_owner_reminder_tool','fresh_routines'):
  raise AssertionError('pure fixed-gate selection imported action stack')
sys.addaudithook(guard)
spec=importlib.util.spec_from_file_location('isolated_fixed_profile',root/'adapters/sotto/fixed_five.py')
profile=importlib.util.module_from_spec(spec);spec.loader.exec_module(profile)
profile._PACKAGED_ROOT=root/'sotto-chief-of-staff'
profile._PACKAGED_RECEIVER=root/'runtime/trigger-receiver'
profile._image_owned=lambda meta:True
env=json.loads(sys.argv[2]);key='skill:'+'a'*64
request={'kind':'shared-meeting-prep','pack':env['SOTTO_SKILLS_ROOT'],'work_key':key,
 'selection':{'mode':'focus','focus':'Dana','meeting':{'event_id':'raw-calendar-event','start':'2026-10-28T15:00:00Z','calendar_id':'primary'}}}
payload={'runner':[sys.executable,str(profile._PACKAGED_RECEIVER/'procedure_runner.py')],
 'prompt':json.dumps(request),'label':key+':meeting-prep','decision_ids':[]}
profile.validate_work('run',payload,env)
"""
    result = subprocess.run([sys.executable, '-I', '-c', code, str(ROOT), json.dumps(env)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_scheduled_brief_cannot_carry_owner_composition_direction(tmp_path):
    env = environment(tmp_path)
    request = {'pack': env['SOTTO_SKILLS_ROOT'], 'kind': 'morning',
               'composition_direction': 'focusing on pricing'}
    payload = {'runner': [sys.executable, str(ROOT / 'runtime/trigger-receiver/brief_runner.py')],
               'prompt': json.dumps(request), 'label': 'cron:sotto-morning-brief', 'decision_ids': []}
    with pytest.raises(profile.ProfileUnavailableError):
        profile.validate_work('run', payload, env)
