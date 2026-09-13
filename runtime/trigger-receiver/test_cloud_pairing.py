import base64
import io
import json
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import cloud_pairing as cp


@pytest.fixture
def pairing(tmp_path, monkeypatch):
    for key, value in {'SOTTO_DEPLOYMENT_MODE': 'managed', 'SOTTO_TENANT_ID': 'tenant',
                       'SOTTO_CONTROL_TOKEN': 'test-control', 'BRIDGE_TOKEN': 'fixture-relay',
                       'SOTTO_IMESSAGE_NUMBER': '+15555551234'}.items():
        monkeypatch.setenv(key, value)
    installed = []
    adapter = SimpleNamespace(install_cloud_google=lambda value: installed.append(value) or [])
    return cp.Pairing(tmp_path, adapter), installed


def handoff():
    key = ec.generate_private_key(ec.SECP256R1())
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.DER,
                             serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    return key, {'tenant_id': 'tenant', 'request_id': 'a' * 64, 'sub': 'google-sub',
                 'public_key': public, 'challenge': 'device-challenge' * 3, 'credentials': None}


def signed(key, grant, challenge):
    signature = key.sign(('sotto-cloud-pair\n' + grant + '\n' + challenge).encode(), ec.ECDSA(hashes.SHA256()))
    return {'pairing_grant': grant, 'signature': base64.b64encode(signature).decode()}


def test_google_install_once_pairing_requires_device_key_and_retry_is_stable(pairing):
    service, installed = pairing
    key, body = handoff()
    grant = service.bootstrap(body)
    assert service.bootstrap(body) == grant and installed == [None]
    wrong = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(InvalidSignature):
        service.redeem(signed(wrong, grant['pairing_grant'], body['challenge']))
    request = signed(key, grant['pairing_grant'], body['challenge'])
    response = service.redeem(request)
    assert response['bridge_token'] != 'fixture-relay'
    assert service.authenticated(response['bridge_token'])
    assert not service.authenticated('fixture-relay')
    assert service.redeem(request) == response  # lost HTTP response is safe to retry
    assert set(response) == {'tenant_id', 'bridge_token', 'device_id', 'setup_code', 'sotto_number', 'allowed_sources'}
    assert response['setup_code'] == ''
    assert 'credentials' not in response


def test_tenant_account_and_device_cannot_be_substituted(pairing):
    service, installed = pairing
    _, body = handoff()
    with pytest.raises(ValueError):
        service.bootstrap({**body, 'tenant_id': 'other'})
    service.bootstrap(body)
    with pytest.raises(PermissionError):
        service.bootstrap({**body, 'sub': 'other'})
    with pytest.raises(PermissionError):
        service.bootstrap({**body, 'public_key': handoff()[1]['public_key']})
    assert installed == [None]


def test_expired_grant_never_returns_bridge_token(pairing):
    service, _ = pairing
    key, body = handoff()
    grant = service.bootstrap(body)
    with service.db() as db:
        db.execute('UPDATE grants SET expires=0')
    with pytest.raises(PermissionError):
        service.redeem(signed(key, grant['pairing_grant'], body['challenge']))


def test_device_revocation_is_individual_and_old_grants_cannot_reactivate(pairing, monkeypatch):
    service, _ = pairing
    clock = [1000.0]
    monkeypatch.setattr(cp.time, 'time', lambda: clock[0])
    assert service.authenticated('fixture-relay')
    first_key, first = handoff()
    first_grant = service.bootstrap(first)
    first_request = signed(first_key, first_grant['pairing_grant'], first['challenge'])
    first_response = service.redeem(first_request)
    second_key, second = handoff()
    second['request_id'] = 'b' * 64
    second_grant = service.bootstrap(second)
    second_response = service.redeem(signed(second_key, second_grant['pairing_grant'], second['challenge']))
    assert first_response['bridge_token'] != second_response['bridge_token']
    clock[0] += 10
    assert service.revoke(first_response['device_id'])
    assert not service.revoke(first_response['device_id'])
    assert not service.authenticated(first_response['bridge_token'])
    assert service.authenticated(second_response['bridge_token'])
    assert not service.authenticated('fixture-relay')
    with pytest.raises(PermissionError, match='revoked'):
        service.redeem(first_request)
    # Only a new authenticated Google handoff can reenroll the same device.
    clock[0] += 10
    fresh = {**first, 'request_id': 'c' * 64}
    grant = service.bootstrap(fresh)
    current = service.redeem(signed(first_key, grant['pairing_grant'], fresh['challenge']))
    assert current['device_id'] == first_response['device_id']
    assert current['bridge_token'] != first_response['bridge_token']
    assert service.authenticated(current['bridge_token'])
    assert not service.authenticated(first_response['bridge_token'])
    with pytest.raises(PermissionError, match='revoked'):
        service.redeem(first_request)
    assert len(service.devices()) == 2


def test_browser_connects_google_without_mac_or_messaging_activation(pairing):
    service, installed = pairing
    body = {'entry': 'browser', 'tenant_id': 'tenant', 'request_id': 'd' * 64,
            'sub': 'google-sub', 'credentials': None}
    result = service.bootstrap(body)
    assert result == {'tenant_id': 'tenant', 'account_connected': True}
    assert service.bootstrap(body) == result and installed == [None]
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM grants').fetchone()[0] == 0
    assert service.devices() == []
    assert not (service.root / 'config/photon-activation.json').exists()
    # A later Mac sign-in belongs to the same account and has its own signed grant.
    key, mac = handoff()
    grant = service.bootstrap(mac)
    assert service.redeem(signed(key, grant['pairing_grant'], mac['challenge']))['device_id']
    with pytest.raises(PermissionError):
        service.bootstrap({**body, 'request_id': 'e' * 64, 'sub': 'other-account'})


def test_bootstrap_retry_cannot_replace_credentials_or_change_entry(pairing):
    service, installed = pairing
    _, body = handoff()
    service.bootstrap(body)
    with pytest.raises(PermissionError):
        service.bootstrap({**body, 'credentials': {'refresh_token': 'replacement'}})
    browser = {k: v for k, v in body.items() if k not in ('public_key', 'challenge')}
    with pytest.raises(PermissionError):
        service.bootstrap({**browser, 'entry': 'browser'})
    assert installed == [None]


def test_readiness_endpoint_accepts_device_or_control_and_reports_receipts_not_app_clicks(pairing, monkeypatch):
    service, _ = pairing
    key, body = handoff()
    grant = service.bootstrap(body)
    paired = service.redeem(signed(key, grant['pairing_grant'], body['challenge']))
    def request(token, origin=None, path='/cloud/status'):
        headers = {'Authorization': 'Bearer ' + token, 'Content-Length': '2'}
        if origin:
            headers['Origin'] = origin
        handler = SimpleNamespace(headers=headers, rfile=io.BytesIO(b'{}'), _send=lambda code, body: (code, body),
                                  _authed=lambda expected: token == expected)
        return cp.handle(handler, path, service.root, service.adapter)
    assert request('fixture-relay')[0] == 401  # legacy bearer retired after device enrollment
    assert request('another-device')[0] == 401
    assert request(paired['bridge_token'], 'https://attacker.invalid')[0] == 400
    assert request(paired['bridge_token']) == (200, {
        'messaging_active': False, 'context_connected': False, 'first_brief': 'waiting'})
    assert request('test-control') == request(paired['bridge_token'])
    assert request('test-control', path='/cloud/consent')[0] == 401  # status access cannot consent for the Mac
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', '+15555550000')
    (service.root / 'config/photon-activation.json').write_text(json.dumps({
        'tenant_id': 'tenant', 'activated': True, 'owner': '+15555550000'}))
    (service.root / 'config/onboarding.json').write_text('{"phase":"delivered","private":"not for the UI"}')
    result = request(paired['bridge_token'])[1]
    assert result['messaging_active'] and result['first_brief'] == 'delivered'
    assert 'private' not in result
    assert service.revoke(paired['device_id'])
    assert request(paired['bridge_token'])[0] == 401


def test_contacts_alone_do_not_open_personal_brief_gate(pairing):
    service, _ = pairing
    cp.managed.record_bridge_consent(service.root, ['contacts'], ['contacts'])
    assert not cp.managed.connection_status(service.root)['context_connected']
    cp.managed.record_bridge_consent(service.root, ['contacts', 'imessage'], ['contacts', 'imessage'])
    assert cp.managed.connection_status(service.root)['context_connected']


def test_delayed_older_google_grant_cannot_restore_revoked_scopes(pairing):
    service, installed = pairing
    _, body = handoff()
    new = {**body, 'request_id': 'b' * 64, 'credential_generation': 2, 'credentials': None}
    result = service.bootstrap(new)
    assert result['credential_generation'] == 2
    assert service.bootstrap(new) == result
    for old in ({**body, 'credential_generation': 1}, body):
        with pytest.raises(PermissionError, match='newer Google'):
            service.bootstrap(old)
    assert installed == [None]
    assert service.bootstrap({**body, 'credential_generation': 3})['credential_generation'] == 3


def test_crash_after_google_file_write_cannot_restore_older_consent(pairing, monkeypatch):
    service, installed = pairing
    _, body = handoff()
    newer = {**body, 'credential_generation': 2}
    record = cp.managed.record_google_consent
    def crash(*args):
        raise RuntimeError('simulated crash after credential file installation')
    monkeypatch.setattr(cp.managed, 'record_google_consent', crash)
    with pytest.raises(RuntimeError):
        service.bootstrap(newer)
    # Reopen the database as a restarted receiver, with no in-memory generation state.
    restarted = cp.Pairing(service.root, service.adapter)
    monkeypatch.setattr(cp.managed, 'record_google_consent', record)
    with pytest.raises(PermissionError, match='newer'):
        restarted.bootstrap({**body, 'request_id': 'b' * 64, 'credential_generation': 1})
    assert installed == [None]
    result = restarted.bootstrap(newer)
    assert result['credential_generation'] == 2
    assert restarted.bootstrap(newer) == result
    assert installed == [None, None]  # Only the exact newer operation was retried.


def test_interrupted_handoff_is_not_retried_after_newer_authorization(pairing, monkeypatch):
    service, installed = pairing
    _, body = handoff()
    older = {**body, 'credential_generation': 1}
    record = cp.managed.record_google_consent
    def crash(*args):
        raise RuntimeError('interrupted consent')
    monkeypatch.setattr(cp.managed, 'record_google_consent', crash)
    with pytest.raises(RuntimeError):
        service.bootstrap(older)
    monkeypatch.setattr(cp.managed, 'record_google_consent', record)
    service.bootstrap({**body, 'request_id': 'b' * 64, 'credential_generation': 2})
    with pytest.raises(PermissionError, match='newer'):
        service.bootstrap(older)
    assert installed == [None, None]
