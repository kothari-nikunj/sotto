import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).parent))
spec = importlib.util.spec_from_file_location('account_broker', Path(__file__).with_name('server.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def device():
    key = ec.generate_private_key(ec.SECP256R1())
    return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.DER,
                         serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


@pytest.fixture
def broker(tmp_path, monkeypatch):
    cfg = {'state_key': Fernet.generate_key().decode(), 'origin': 'https://accounts.example.com',
           'instance': 'https://tenant.example.com', 'client': {'client_id': 'client', 'client_secret': 'secret'},
           'owner_email': 'owner@example.com', 'tenant': 'tenant-fixture', 'control_token': 'fixture-control'}
    broker = m.Broker(tmp_path / 'state.db', cfg)
    monkeypatch.setattr(broker, 'exchange_google', lambda *a: ({'access_token': 'access-fixture',
        'refresh_token': 'refresh-fixture', 'scope': ' '.join(m.SCOPES)},
        {'iss': 'https://accounts.google.com', 'sub': 'immutable-owner', 'email': cfg['owner_email'], 'email_verified': True}))
    return broker


def begin(broker):
    start = broker.start({'public_key': device(), 'challenge': 'a' * 43})
    assert urlsplit(start['authorization_url']).netloc == 'accounts.example.com'
    state = parse_qs(urlsplit(start['authorization_url']).query)['state'][0]
    cookie = broker.browser.create()
    assert 'confirmation_code' not in start
    url = broker.begin_device(state, cookie)
    query = parse_qs(urlsplit(url).query)
    assert query['code_challenge_method'] == ['S256']
    assert query['redirect_uri'] == [broker.redirect]
    assert set(query['scope'][0].split()) == {'openid', 'email', 'profile',
        'https://www.googleapis.com/auth/gmail.modify',
        'https://www.googleapis.com/auth/calendar', 'https://www.googleapis.com/auth/contacts'}
    return start['poll_token'], query['state'][0], cookie


def test_retry_resumes_tenant_and_erases_google_credentials_after_handoff(broker, monkeypatch):
    poll, state, cookie = begin(broker)
    assert broker.status(poll) == {'status': 'pending'}
    broker.authorize(state, 'code', browser_token=cookie)
    assert broker.status(poll) == {'status': 'awaiting_device'}
    broker.confirm_device(poll, broker.device_code(state, cookie))
    calls = []
    def deliver(body):
        calls.append(body)
        if len(calls) == 1:
            raise requests.ConnectionError('tenant restarting')
        return {'tenant_id': broker.config['tenant'], 'pairing_grant': 'device-bound',
                'instance_url': broker.instance, 'challenge': body['challenge']}
    monkeypatch.setattr(broker, 'deliver', deliver)
    assert broker.status(poll) == {'status': 'provisioning'}
    assert broker.status(poll)['status'] == 'ready'
    assert calls[0]['request_id'] == calls[1]['request_id']
    with broker.db() as db:
        row = db.execute('SELECT secret,result FROM sessions').fetchone()
        assert row['secret'] is None
        assert 'refresh-fixture' not in broker.open(row['result']).values()
    restarted = m.Broker(broker.path, broker.config)
    assert restarted.status(poll)['status'] == 'ready'
    assert b'refresh-fixture' not in Path(broker.path).read_bytes()
    with pytest.raises(ValueError):
        broker.authorize(state, 'replayed-code', browser_token=cookie)


@pytest.mark.parametrize('identity', [
    {'iss': 'https://accounts.google.com', 'sub': 'other', 'email': 'other@example.com', 'email_verified': True},
    {'iss': 'https://accounts.google.com', 'sub': 'owner', 'email': 'owner@example.com', 'email_verified': False},
    {'email': 'owner@example.com', 'email_verified': True}])
def test_wrong_or_unverified_account_cannot_adopt(broker, monkeypatch, identity):
    poll, state, cookie = begin(broker)
    monkeypatch.setattr(broker, 'exchange_google', lambda *a: ({}, identity))
    with pytest.raises(ValueError):
        broker.authorize(state, 'code', browser_token=cookie)
    assert broker.status(poll) == {'status': 'failed'}
    with broker.db() as db:
        assert db.execute('SELECT COUNT(*) FROM account').fetchone()[0] == 0
        assert db.execute('SELECT secret FROM sessions').fetchone()[0] is None


def test_mutable_email_cannot_reassign_existing_google_sub(broker, monkeypatch):
    _, state, cookie = begin(broker)
    broker.authorize(state, 'code', browser_token=cookie)
    _, second, cookie = begin(broker)
    monkeypatch.setattr(broker, 'exchange_google', lambda *a: ({'scope': ''},
        {'iss': 'https://accounts.google.com', 'sub': 'different-google-account', 'email': 'owner@example.com', 'email_verified': True}))
    with pytest.raises(ValueError):
        broker.authorize(second, 'code', browser_token=cookie)


def test_no_data_consent_is_valid_identity_without_source_credentials(broker, monkeypatch):
    poll, state, cookie = begin(broker)
    monkeypatch.setattr(broker, 'exchange_google', lambda *a: ({'scope': 'openid email'},
        {'iss': 'https://accounts.google.com', 'sub': 'immutable-owner', 'email': 'owner@example.com', 'email_verified': True}))
    broker.authorize(state, 'code', browser_token=cookie)
    with broker.db() as db:
        handoff = broker.open(db.execute('SELECT secret FROM sessions').fetchone()[0])
        assert handoff['credentials'] is None


def test_expiry_and_unknown_poll_fail_closed(broker):
    poll, state, cookie = begin(broker)
    with broker.db() as db:
        db.execute('UPDATE sessions SET expires=0')
    with pytest.raises(PermissionError):
        broker.status(poll)
    with pytest.raises(ValueError):
        broker.authorize(state, 'code', browser_token=cookie)
    with pytest.raises(PermissionError):
        broker.status('unknown')


def test_existing_account_follows_google_sub_when_email_changes(broker, monkeypatch):
    _, first, cookie = begin(broker)
    broker.authorize(first, 'code', browser_token=cookie)
    _, next_state, cookie = begin(broker)
    monkeypatch.setattr(broker, 'exchange_google', lambda *a: ({'scope': ''},
        {'iss': 'https://accounts.google.com', 'sub': 'immutable-owner', 'email': 'renamed@example.com', 'email_verified': True}))
    broker.authorize(next_state, 'code', browser_token=cookie)
    with broker.db() as db:
        assert db.execute('SELECT COUNT(*) FROM account').fetchone()[0] == 1


def test_two_invited_accounts_route_only_to_their_registered_tenants(broker, monkeypatch):
    broker.registry.register('second', 'https://second.example.com', 'second-control', 'second@example.com')
    polls = []
    for sub, email in [('immutable-owner', 'owner@example.com'), ('second-sub', 'second@example.com')]:
        monkeypatch.setattr(broker, 'exchange_google', lambda *a, sub=sub, email=email:
            ({'scope': ''}, {'iss': 'https://accounts.google.com', 'sub': sub, 'email': email, 'email_verified': True}))
        poll, state, cookie = begin(broker)
        broker.authorize(state, 'fixture-code', browser_token=cookie)
        broker.confirm_device(poll, broker.device_code(state, cookie))
        polls.append(poll)
    calls = []
    def post(url, *, json, headers, **kwargs):
        calls.append((url, json['tenant_id'], headers['Authorization']))
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {'tenant_id': json['tenant_id'], 'pairing_grant': 'signed-device-grant',
                        'credential_generation': json['credential_generation'],
                        'credentials': 'must-never-leave-tenant'}
        return Response()
    monkeypatch.setattr(m.requests, 'post', post)
    for poll in polls:
        result = broker.status(poll)
        assert result['status'] == 'ready' and 'credentials' not in result
    assert calls == [('https://tenant.example.com/cloud/bootstrap', 'tenant-fixture', 'Bearer fixture-control'),
                     ('https://second.example.com/cloud/bootstrap', 'second', 'Bearer second-control')]
    with broker.db() as db:
        owners = list(db.execute('SELECT account_id FROM tenants ORDER BY id'))
    with pytest.raises(ValueError):
        broker.registry.route('tenant-fixture', owners[0]['account_id'])
    assert b'second-control' not in Path(broker.path).read_bytes()
    restarted = m.Broker(broker.path, broker.config)
    assert restarted.status(polls[1])['tenant_id'] == 'second'


def test_browser_identity_does_not_issue_any_device_capability(broker, monkeypatch):
    cookie = broker.browser.create()
    start = broker.start({'entry': 'browser'}, browser_token=cookie)
    state = parse_qs(urlsplit(start['authorization_url']).query)['state'][0]
    cookie = broker.authorize(state, 'code', browser_token=cookie)['browser_token']
    poll = broker.browser.require(cookie)['secret']['poll']
    with broker.db() as db:
        handoff = broker.open(db.execute('SELECT secret FROM sessions').fetchone()[0])
    assert 'public_key' not in handoff and 'challenge' not in handoff
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {'tenant_id': 'tenant-fixture', 'account_connected': True,
                    'credential_generation': 1,
                    'bridge_token': 'never-return-this', 'pairing_grant': 'never-return-this'}
    monkeypatch.setattr(m.requests, 'post', lambda *a, **k: Response())
    assert 'poll_token' not in start
    with pytest.raises(PermissionError):
        broker.status(poll)
    result = broker.status(poll, browser_token=cookie)
    assert result == {'status': 'ready', 'tenant_id': 'tenant-fixture',
                      'instance_url': broker.instance, 'account_connected': True}
    with pytest.raises(ValueError):
        broker.start({'entry': 'browser', 'public_key': device()})


def test_legacy_account_is_adopted_before_any_new_callback(broker, monkeypatch):
    with broker.db() as db:
        db.execute('INSERT INTO account VALUES (1,?,?)', ('old-sub', 'tenant-fixture'))
    restarted = m.Broker(broker.path, broker.config)
    _, state, cookie = begin(restarted)
    monkeypatch.setattr(restarted, 'exchange_google', lambda *a: ({'scope': ''},
        {'iss': 'https://accounts.google.com', 'sub': 'replacement-sub', 'email': 'owner@example.com', 'email_verified': True}))
    with pytest.raises(ValueError):
        restarted.authorize(state, 'code', browser_token=cookie)
    with restarted.db() as db:
        assert db.execute('SELECT sub FROM cloud_accounts').fetchone()[0] == 'old-sub'


def test_parallel_signins_reuse_one_identity_and_tenant(broker, monkeypatch):
    starts = [begin(broker) for _ in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda pair: broker.authorize(pair[1], 'code', browser_token=pair[2]), starts))
    with broker.db() as db:
        assert db.execute('SELECT COUNT(*) FROM cloud_accounts').fetchone()[0] == 1
        handoffs = [broker.open(r['secret']) for r in db.execute('SELECT secret FROM sessions')]
    assert len({h['account_id'] for h in handoffs}) == 1
    assert len({h['tenant_id'] for h in handoffs}) == 1
    assert sorted(h['credential_generation'] for h in handoffs) == [1, 2, 3, 4]


def test_suspended_account_and_bad_issuer_cannot_reconnect(broker, monkeypatch):
    _, state, cookie = begin(broker)
    broker.authorize(state, 'code', browser_token=cookie)
    with broker.db() as db:
        db.execute("UPDATE tenants SET status='suspended'")
    _, state, cookie = begin(broker)
    with pytest.raises(ValueError):
        broker.authorize(state, 'code', browser_token=cookie)
    with pytest.raises(ValueError):
        broker.registry.route('tenant-fixture')
    monkeypatch.setattr(broker, 'exchange_google', lambda *a: ({'scope': ''},
        {'iss': 'https://attacker.invalid', 'sub': 'immutable-owner',
         'email': 'owner@example.com', 'email_verified': True}))
    _, state, cookie = begin(broker)
    with pytest.raises(ValueError):
        broker.authorize(state, 'code', browser_token=cookie)


def test_google_consent_does_not_enroll_attacker_device_without_browser_code(broker, monkeypatch):
    started = broker.start({'public_key': device(), 'challenge': 'c' * 43})
    state = parse_qs(urlsplit(started['authorization_url']).query)['state'][0]
    original, stranger = broker.browser.create(), broker.browser.create()
    exchanges, deliveries = [], []
    exchange = broker.exchange_google
    monkeypatch.setattr(broker, 'exchange_google', lambda *args: (exchanges.append(args), exchange(*args))[1])
    monkeypatch.setattr(broker, 'deliver', lambda handoff: deliveries.append(handoff))
    for cookie in (None, original, stranger):
        with pytest.raises(PermissionError):
            broker.authorize(state, 'forwarded-google-code', browser_token=cookie)
    assert not exchanges
    broker.begin_device(state, original)
    with pytest.raises(PermissionError):
        broker.begin_device(state, stranger)
    with pytest.raises(PermissionError):
        broker.authorize(state, 'forwarded-google-code', browser_token=stranger)
    broker.authorize(state, 'real-code', browser_token=original)
    assert len(exchanges) == 1
    assert broker.status(started['poll_token']) == {'status': 'awaiting_device'}
    assert not deliveries
    with pytest.raises(PermissionError):
        broker.device_code(state, stranger)
    correct = broker.device_code(state, original)
    assert correct not in str(started) and correct not in str(broker.status(started['poll_token']))
    other_poll, _, _ = begin(broker)
    with pytest.raises(PermissionError):
        broker.confirm_device(other_poll, correct)
    assert not deliveries
    assert broker.confirm_device(started['poll_token'], correct) == {'status': 'provisioning'}
    assert broker.confirm_device(started['poll_token'], correct) == {'status': 'provisioning'}
    with pytest.raises(PermissionError):
        broker.device_code(state, original)


def test_device_confirmation_attempts_are_durable_and_bounded(broker):
    poll, state, cookie = begin(broker)
    broker.authorize(state, 'real-code', browser_token=cookie)
    correct = broker.device_code(state, cookie)
    for _ in range(m.DEVICE_CODE_ATTEMPTS):
        with pytest.raises(PermissionError):
            broker.confirm_device(poll, 'WRONG')
    restarted = m.Broker(broker.path, broker.config)
    with pytest.raises(PermissionError):
        restarted.confirm_device(poll, correct)
    assert restarted.status(poll) == {'status': 'failed'}
    with restarted.db() as db:
        assert db.execute('SELECT secret FROM sessions').fetchone()[0] is None
        assert db.execute('SELECT code_secret FROM device_confirmation').fetchone()[0] is None


def test_legacy_unconfirmed_device_session_cannot_authorize_after_upgrade(broker):
    poll, state, cookie = begin(broker)
    with broker.db() as db:
        db.execute('DELETE FROM device_confirmation')
    with pytest.raises(PermissionError):
        broker.authorize(state, 'code', browser_token=cookie)
    assert broker.status(poll) == {'status': 'pending'}



def test_anonymous_starts_cannot_fill_verified_handoff_capacity(broker, monkeypatch):
    poll, state, cookie = begin(broker)
    broker.authorize(state, 'code', browser_token=cookie)
    pending = []
    for _ in range(m.PENDING_SIGNINS + 1):
        pending.append(broker.start({'public_key': device(), 'challenge': 'd' * 43}))
    with broker.db() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions WHERE status='pending'").fetchone()[0] == m.PENDING_SIGNINS
        assert db.execute("SELECT status FROM sessions WHERE poll=?", (m.digest(poll),)).fetchone()[0] == 'awaiting_device'
    with pytest.raises(PermissionError):
        broker.status(pending[0]['poll_token'])
    assert broker.status(pending[-1]['poll_token']) == {'status': 'pending'}
    new_cookie = broker.browser.create()
    latest = pending[-1]
    state = parse_qs(urlsplit(latest['authorization_url']).query)['state'][0]
    broker.begin_device(state, new_cookie)
    broker.authorize(state, 'legitimate-code', browser_token=new_cookie)



def test_missing_issuer_cannot_be_adopted_by_registry(broker):
    with broker.db() as db:
        with pytest.raises(ValueError):
            broker.registry.claim(db, {'sub': 'owner', 'email': 'owner@example.com', 'email_verified': True})


def test_anonymous_flood_preserves_cookie_bound_callbacks_and_browser(broker):
    poll, state, cookie = begin(broker)
    browser_cookie = broker.browser.create()
    browser_start = broker.start({'entry': 'browser'}, browser_token=browser_cookie)
    browser_state = parse_qs(urlsplit(browser_start['authorization_url']).query)['state'][0]
    for _ in range(m.PENDING_SIGNINS + 2):
        broker.start({'public_key': device(), 'challenge': 'd' * 43})
    for _ in range(502):
        broker.browser.create()
    broker.authorize(state, 'code', browser_token=cookie)
    assert broker.status(poll) == {'status': 'awaiting_device'}
    assert broker.device_code(state, cookie)
    assert broker.authorize(browser_state, 'code', browser_token=browser_cookie)['browser_token']
