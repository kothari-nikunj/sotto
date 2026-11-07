"""Current channel crosscheck uses the real authenticated Python client schema."""
import hashlib
import hmac
import importlib.util
import io
import json
from pathlib import Path

import pytest

import runtime_channel_proof as proof

STARTUP_PATH = Path(__file__).resolve().parents[2] / 'adapters/sotto/startup_config.py'
STARTUP_SPEC = importlib.util.spec_from_file_location('channel_proof_startup', STARTUP_PATH)
startup = importlib.util.module_from_spec(STARTUP_SPEC)
STARTUP_SPEC.loader.exec_module(startup)

HERE = Path(__file__).parent
CLIENT_PATH = HERE.parents[1] / 'adapters/sotto/client.py'
SPEC = importlib.util.spec_from_file_location('channel_proof_client', CLIENT_PATH)
client_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client_module)
OWNER = '+15555550123'
GUID = 'iMessage;-;+15555550123'
LINE = '+15555550999'
PROJECT = 'fixture-project'
SECRET = 'synthetic-project-secret'
BEARER = 'synthetic-private-runtime-bearer-123456789'
ZONE = 'America/Los_Angeles'
POLICY = {
    'SOTTO_CRON_DELIVER': 'photon',
    'PHOTON_HOME_CHANNEL': OWNER, 'PHOTON_ALLOWED_USERS': OWNER,
    'PHOTON_PROJECT_ID': PROJECT, 'PHOTON_PROJECT_SECRET': SECRET,
    'PHOTON_ALLOW_ALL_USERS': 'false',
    'SOTTO_CHAT_RUNTIME_URL': 'http://127.0.0.1:48317',
    'SOTTO_CHAT_RUNTIME_TOKEN': BEARER,
}


class Opener:
    def __init__(self, wire):
        self.wire = wire

    def open(self, _request, timeout):
        assert timeout > 0
        return io.BytesIO(json.dumps(self.wire).encode())


def setup(tmp_path, *, mode='fixed'):
    config_dir = tmp_path / 'rendered'
    config_dir.mkdir(mode=0o700)
    config_dir.chmod(0o700)
    thread = f'imessage:{GUID}~' + ('shared' if mode == 'first_contact_shared_project' else LINE)
    config = {
        'version': 1, 'port': 48317, 'bearerToken': BEARER, 'channel': 'photon',
        'owner': {'accountId': 'account-1', 'ownerId': OWNER},
        'lineKey': 'line-1', 'target': 'photon',
        'credentials': {'mode': 'cloud', 'photonProjectId': PROJECT,
                        'photonProjectSecret': SECRET},
    }
    if mode == 'fixed':
        config['providerThreadId'] = thread
    elif mode == 'first_contact_shared_project':
        config['firstContact'] = {'mode': 'owner_dm_shared_project'}
    else:
        config['firstContact'] = {'mode': 'owner_dm', 'expectedProviderLine': LINE}
    path = config_dir / 'expected-channel.json'
    path.write_text(json.dumps(startup.receiver_channel_expectation(config)))
    path.chmod(0o600)
    receiver_root = tmp_path / 'receiver'
    receiver_config = receiver_root / 'config'
    receiver_config.mkdir(parents=True)
    receiver_config.chmod(0o755)
    receipt = receiver_config / 'photon-activation.json'
    receipt.write_text(json.dumps({
        'tenant_id': '', 'owner': OWNER, 'chat_id': GUID, 'activated': True,
        'project_fingerprint': hashlib.sha256(
            ('sotto-photon-project-v1:' + PROJECT).encode()).hexdigest(),
    }))
    receipt.chmod(0o600)
    identity, fields = client_module._expected_configured_channel(config)
    configured = {
        'version': 1, 'identity': identity,
        'credential_proof': hmac.new(
            BEARER.encode(), client_module._identity_transcript(fields),
            hashlib.sha256).hexdigest(),
    }
    activation = {
        'status': 'acknowledged', 'mode': mode,
        'binding': {
            'tenant_id': '', 'project_fingerprint': hashlib.sha256(
                ('sotto-photon-project-v1:' + PROJECT).encode()).hexdigest(),
            'owner_id': OWNER, 'account_id': 'account-1', 'line_key': 'line-1',
            'provider_thread_id': thread,
        },
        'event_id': 'a' * 64, 'admitted_at': 1780000000000,
        'acknowledged_at': 1780000000001,
    }
    wire = {'version': 1, 'configured': configured, 'activation': activation}
    opener = Opener(wire)
    chat = client_module.ChatClient('http://127.0.0.1:48317', BEARER, opener=opener)
    return config, path, receipt, receiver_root, chat, opener


def check(path, receiver_root, chat, *, zone=lambda: ZONE, policy=POLICY):
    return proof.check_current_channel(
        chat_client=chat, expected_channel_path=path,
        receiver_data_root=receiver_root, receiver_policy=policy,
        configured_zone_name=zone, expected_zone=ZONE)


@pytest.mark.parametrize('mode', ['fixed', 'first_contact'])
def test_acknowledged_binding_matches_current_receipt_without_raw_event(tmp_path, mode):
    _, path, _, receiver_root, chat, _ = setup(tmp_path, mode=mode)
    result = check(path, receiver_root, chat)
    assert result == {
        'version': 1, 'status': 'current_channel_proved',
        'action_authorized': False, 'activation_event_id': 'a' * 64,
        'owner_route': {'platform': 'imessage', 'chat_id': GUID, 'thread_id': None},
        'provider_thread_id': f'imessage:{GUID}~{LINE}', 'schedule_zone': ZONE,
        'tenant_id': '', 'project_fingerprint': hashlib.sha256(
            ('sotto-photon-project-v1:' + PROJECT).encode()).hexdigest(),
        'owner_id': OWNER, 'account_id': 'account-1', 'line_key': 'line-1',
        'route_digest': '0354943eeebeef1e0c1293ea5ed7a91362edf6ff5a2081e86e9f5351c39b0ddb',
    }


@pytest.mark.parametrize('status', ['absent', 'pending'])
def test_unacknowledged_state_is_withheld(tmp_path, status):
    _, path, _, receiver_root, chat, opener = setup(tmp_path)
    if status == 'absent':
        opener.wire['activation'] = {'status': 'absent', 'mode': 'fixed'}
    else:
        opener.wire['activation'].pop('acknowledged_at')
        opener.wire['activation']['status'] = 'pending'
    with pytest.raises(proof.ChannelProofUnavailableError):
        check(path, receiver_root, chat)


def test_selfhost_photon_uses_same_physical_line_receipt_and_loaded_ack(tmp_path):
    _, path, receipt, receiver_root, chat, opener = setup(tmp_path, mode='first_contact')
    selfhost = {**POLICY, 'SOTTO_DEPLOYMENT_MODE': 'self_host',
                'SOTTO_RUNTIME_PROFILE': 'self-host-photon-fixed-five-v1'}
    result = check(path, receiver_root, chat, policy=selfhost)
    assert result['provider_thread_id'] == f'imessage:{GUID}~{LINE}'
    assert result['activation_event_id'] == 'a' * 64
    opener.wire['activation']['status'] = 'pending'
    with pytest.raises(proof.ChannelProofUnavailableError):
        check(path, receiver_root, chat, policy=selfhost)
    opener.wire['activation']['status'] = 'acknowledged'
    # The exact project fingerprint is also checked through the persisted receipt.
    receipt.write_text(json.dumps({**json.loads(receipt.read_text()),
                                   'project_fingerprint': '0' * 64}))
    with pytest.raises(proof.ChannelProofUnavailableError):
        check(path, receiver_root, chat, policy=selfhost)


def test_changed_receipt_config_zone_or_runtime_endpoint_withheld(tmp_path):
    config, path, receipt, receiver_root, chat, opener = setup(tmp_path)
    before = receipt.read_text()
    receipt.write_text(before.replace(GUID, 'iMessage;-;wrong'))
    with pytest.raises(proof.ChannelProofUnavailableError):
        check(path, receiver_root, chat)
    receipt.write_text(before)
    expectation = startup.receiver_channel_expectation(config)
    path.write_text(json.dumps({**expectation, 'lineKey': 'changed'}))
    with pytest.raises(proof.ChannelProofUnavailableError):
        check(path, receiver_root, chat)
    path.write_text(json.dumps(expectation))
    with pytest.raises(proof.ChannelProofUnavailableError):
        check(path, receiver_root, chat, zone=lambda: 'UTC')
    chat.url = 'http://127.0.0.1:48318'
    with pytest.raises(proof.ChannelProofUnavailableError):
        check(path, receiver_root, chat)
    chat.url = 'http://127.0.0.1:48317'
    opener.wire['activation']['binding']['provider_thread_id'] = f'imessage:{GUID}~+15555550000'
    with pytest.raises(proof.ChannelProofUnavailableError):
        check(path, receiver_root, chat)


def test_duplicate_config_fields_and_midcheck_ack_change_withheld(tmp_path):
    config, path, _, receiver_root, chat, opener = setup(tmp_path)
    expectation = startup.receiver_channel_expectation(config)
    path.write_text(json.dumps(expectation)[:-1] + ',"port":48317}')
    with pytest.raises(proof.ChannelProofUnavailableError):
        check(path, receiver_root, chat)
    path.write_text(json.dumps(expectation))
    # Change the durable ACK between the two authenticated reads.
    class ChangingOpener(Opener):
        def __init__(self, wire):
            super().__init__(wire)
            self.count = 0

        def open(self, request, timeout):
            self.count += 1
            if self.count == 2:
                self.wire['activation']['event_id'] = 'b' * 64
            return super().open(request, timeout)

    chat._opener = ChangingOpener(opener.wire)
    with pytest.raises(proof.ChannelProofUnavailableError):
        check(path, receiver_root, chat)


def test_shared_project_real_client_ack_proof_and_owner_route(tmp_path):
    _, path, _, receiver_root, chat, opener = setup(tmp_path, mode='first_contact_shared_project')
    result = check(path, receiver_root, chat)
    assert result['provider_thread_id'] == f'imessage:{GUID}~shared'
    assert result['project_fingerprint'] == hashlib.sha256(('sotto-photon-project-v1:' + PROJECT).encode()).hexdigest()
    import runtime_owner_reminder as authority
    identity = {k:result[k] for k in ('tenant_id','project_fingerprint','owner_id','account_id','line_key')}
    identity.update(channel='photon', target='photon', route_mode='first_contact_shared_project', route_id='shared_project')
    # Match the real receiver-private projection location, not a substituted proof.
    runtime = receiver_root / 'runtime'
    runtime.mkdir(mode=0o700)
    target = runtime / 'expected-channel.json'
    target.write_bytes(path.read_bytes()); target.chmod(0o600)
    policy = dict(POLICY, _configured_tz_name=lambda: ZONE)
    authority.validate_route(f'imessage:{GUID}~shared',identity,policy,receiver_root,proof.runtime_activation,chat_client=chat)
    import runtime_owner_routine_tool as routine
    routine_policy = dict(policy, SOTTO_DEPLOYMENT_MODE='self_host', GOOGLE_AI_API_KEY='<test-key>', SOTTO_TENANT_ID='',
                          SOTTO_DATA=str(receiver_root),
                          SOTTO_RUNTIME_PROFILE='self-host-photon-fixed-five-v1',
                          SOTTO_ROUTINE_EPOCH='c' * 32, SOTTO_IMESSAGE_NUMBER=LINE)
    binding = routine.current_binding(identity,routine_policy,receiver_root,
                                      proof.runtime_activation,chat_client=chat)
    assert binding['identity']['route_id'] == 'shared_project'
    assert binding['route']['chat_id'] == GUID
    changed_policy = dict(routine_policy, PHOTON_PROJECT_ID='other-project')
    with pytest.raises(authority.UnavailableError):
        routine.current_binding(identity,changed_policy,receiver_root,
                                proof.runtime_activation,chat_client=chat)
    for bad in (f'imessage:{GUID}~{LINE}', f'imessage:{GUID}~shared~shared'):
        with pytest.raises(authority.UnavailableError):
            authority.validate_route(bad,identity,policy,receiver_root,proof.runtime_activation,chat_client=chat)
    opener.wire['activation']['status']='pending';opener.wire['activation'].pop('acknowledged_at')
    with pytest.raises(authority.UnavailableError):
        authority.validate_route(f'imessage:{GUID}~shared',identity,policy,receiver_root,proof.runtime_activation,chat_client=chat)


def test_shared_requested_brief_inspection_uses_real_injected_ack_client(tmp_path):
    import runtime_owner_brief_request as brief
    from adapters.sotto import startup_config
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo
    _, path, _, data, chat, opener = setup(tmp_path, mode='first_contact_shared_project')
    runtime = data / 'runtime'
    runtime.mkdir(mode=0o700)
    target = runtime / 'expected-channel.json'
    target.write_bytes(path.read_bytes()); target.chmod(0o600)
    identity = {'tenant_id':'', 'channel':'photon', 'target':'photon', 'owner_id':OWNER,
                'account_id':'account-1', 'line_key':'line-1', 'route_mode':'first_contact_shared_project',
                'route_id':'shared_project', 'project_fingerprint':hashlib.sha256(('sotto-photon-project-v1:' + PROJECT).encode()).hexdigest()}
    capability = startup_config._owner_loop_capability()
    root = 'synthetic-root-capability-01234567890123456789'
    args = {'kind':'auto','refresh':False,'remainder':None,'provider_message_id':'synthetic-requested-shared'}
    event = 'e' * 64
    canonical = json.dumps(args,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    text = 'Show my brief.'
    action = hashlib.sha256(json.dumps([event,'owner_brief_request',canonical],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    body = dict(version=1,operation='inspect',identity=identity,event_id=event,action_id=action,
                payload_hash='b'*64,text_hash=hashlib.sha256(text.encode()).hexdigest(),text=text,
                admitted_at_ms=1780000000000,provider_thread_id=f'imessage:{GUID}~shared',args=args)
    policy = dict(POLICY,SOTTO_DEPLOYMENT_MODE='self_host',GOOGLE_AI_API_KEY='<test-key>',
                  SOTTO_DATA=str(data),SOTTO_TENANT_ID='',SOTTO_RUNTIME_PROFILE='self-host-photon-fixed-five-v1',
                  _configured_tz_name=lambda:ZONE,_tz_resolve=ZoneInfo)
    queue = SimpleNamespace(job_id_for=lambda *_:'f'*32,get=lambda *_:None,admission_for_event=lambda *_:None)
    def call():
        return brief.perform(body,bearer=capability.derive(root,identity),root=root,policy=policy,
            data_root=data,capability=capability,activation=proof.runtime_activation,queue=queue,
            admit=lambda *_a,**_k:pytest.fail('inspection must not admit work'),chat_client=chat)
    assert call() == {'version':1,'status':'not_found'}
    opener.wire['activation']['status']='pending';opener.wire['activation'].pop('acknowledged_at')
    with pytest.raises(brief.UnavailableError):
        call()


VECTORS = HERE.parent / 'sotto-chat/test/fixtures/route-digest-vectors.json'


def _python_digests(identity, thread):
    """Every Python route-digest caller, which must all be the one shared implementation."""
    import telegram_channel_proof
    shared = proof.retained_proof_module().route_digest(
        identity['channel'], identity['tenantId'], identity['projectFingerprint'], identity['ownerId'],
        identity['accountId'], identity['lineKey'], thread)
    expectation = {'tenantId': identity['tenantId'], 'projectFingerprint': identity['projectFingerprint'],
                   'owner': {'ownerId': identity['ownerId'], 'accountId': identity['accountId']},
                   'lineKey': identity['lineKey'], 'route': {'providerThreadId': thread}}
    if identity['channel'] == 'photon':
        return {shared, proof._route_digest(expectation, thread)}
    if identity['projectFingerprint'] == '':
        return {shared, telegram_channel_proof._route_digest(expectation)}
    return {shared}


def test_route_digest_matches_shared_node_vectors():
    if not VECTORS.is_file():
        pytest.skip('shared route-digest vectors are not present')
    vectors = json.loads(VECTORS.read_text(encoding='utf-8'))
    assert vectors['version'] == 1 and len(vectors['valid']) >= 4 and vectors['invalid']
    assert vectors['fields'] == ['channel', 'tenantId', 'projectFingerprint', 'ownerId', 'accountId',
                                 'lineKey', 'providerThreadId']
    for case in vectors['valid']:
        assert _python_digests(case['input'], case['input']['providerThreadId']) == {case['digest']}, case['name']
    for case in vectors['invalid']:
        with pytest.raises((ValueError, TypeError)):
            proof.retained_proof_module().route_digest(*(case['input'][key] for key in vectors['fields']))
        identity = case['input']
        if identity['channel'] == 'photon':
            with pytest.raises(proof.ChannelProofUnavailableError):
                proof._route_digest({'tenantId': identity['tenantId'],
                                     'projectFingerprint': identity['projectFingerprint'],
                                     'owner': {'ownerId': identity['ownerId'],
                                               'accountId': identity['accountId']},
                                     'lineKey': identity['lineKey']}, identity['providerThreadId'])


@pytest.mark.parametrize('field', ['ownerId', 'accountId', 'lineKey', 'thread'])
def test_python_route_digest_rejects_empty_identity_fields_like_node(field):
    import telegram_channel_proof
    expectation = {'tenantId': 'tenant-a', 'projectFingerprint': 'a' * 64,
                   'owner': {'ownerId': OWNER, 'accountId': 'account-1'}, 'lineKey': 'line-1',
                   'route': {'providerThreadId': 'telegram:8675309'}}
    thread = 'imessage:x;-;y~line-1'
    if field == 'thread':
        thread = expectation['route']['providerThreadId'] = ''
    elif field == 'lineKey':
        expectation['lineKey'] = ''
    else:
        expectation['owner'][field] = ''
    with pytest.raises(proof.ChannelProofUnavailableError):
        proof._route_digest(expectation, thread)
    with pytest.raises(telegram_channel_proof.TelegramChannelProofUnavailableError):
        telegram_channel_proof._route_digest(expectation)


def test_python_route_digest_rejects_a_malformed_project_fingerprint_like_node():
    expectation = {'tenantId': 'tenant-a', 'projectFingerprint': 'not-a-fingerprint',
                   'owner': {'ownerId': OWNER, 'accountId': 'account-1'}, 'lineKey': 'line-1'}
    with pytest.raises(proof.ChannelProofUnavailableError):
        proof._route_digest(expectation, 'imessage:x;-;y~line-1')
