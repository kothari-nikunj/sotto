"""Browser possession, verified identity and DM proof cannot substitute for each other."""
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from test_broker import device
import requests

from browser import COOKIE
import pages
from test_broker import broker, m  # noqa: F401 — shared isolated fixture


def begin_browser(broker, cookie=None):  # noqa: F811
    cookie = cookie or broker.browser.create()
    start = broker.start({'entry': 'browser'}, browser_token=cookie)
    return cookie, parse_qs(urlsplit(start['authorization_url']).query)['state'][0]


def login(broker, monkeypatch, cookie=None, sub='immutable-owner', email='owner@example.com'):  # noqa: F811
    monkeypatch.setattr(broker, 'exchange_google', lambda *a: ({'scope': 'openid email'},
                        {'iss': 'https://accounts.google.com', 'sub': sub, 'email': email, 'email_verified': True}))
    cookie, state = begin_browser(broker, cookie)
    return broker.authorize(state, 'fixture-code', browser_token=cookie)['browser_token']


@pytest.fixture
def tenant_reply(broker, monkeypatch):  # noqa: F811
    calls = []
    def post(url, *, json, headers, **kwargs):
        calls.append((url, headers['Authorization']))
        body = ({'tenant_id': json['tenant_id'], 'account_connected': True,
                 'credential_generation': json['credential_generation']} if url.endswith('/cloud/bootstrap') else
                {'messaging_active': False, 'context_connected': False, 'first_brief': 'waiting',
                 'private': 'never-show-in-browser'})
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return body
        return Response()
    monkeypatch.setattr(m.requests, 'post', post)
    return calls


def test_callback_requires_initiating_browser_and_rotates_session(broker, monkeypatch, tenant_reply):  # noqa: F811
    original, state = begin_browser(broker)
    outsider = broker.browser.create()
    for cookie in ('', outsider):
        with pytest.raises(PermissionError):
            broker.authorize(state, 'stolen-code', browser_token=cookie)
    # Attempts from a different browser do not consume the legitimate callback.
    with broker.db() as db:
        assert db.execute('SELECT status FROM sessions').fetchone()[0] == 'pending'
    result = broker.authorize(state, 'fixture-code', browser_token=original)
    cookie = result['browser_token']
    assert cookie != original
    with pytest.raises(PermissionError):
        broker.browser.require(original)
    session = broker.browser.require(cookie, authenticated=True)
    poll = session['secret']['poll']
    for other in ('', outsider):
        with pytest.raises(PermissionError):
            broker.status(poll, browser_token=other)
    journey = broker.journey(cookie)
    assert journey['next_action'] == 'wait_for_messages'
    assert 'never-show-in-browser' not in json.dumps(journey)
    assert cookie not in json.dumps(journey) and poll not in json.dumps(journey)
    assert set(journey) == {'journey_id', 'google_connection', 'next_action', 'context_connected',
                            'messaging_active', 'first_brief'}


def test_returning_account_resumes_journey_after_restart_and_session_expiry(broker, monkeypatch, tenant_reply):  # noqa: F811
    first = login(broker, monkeypatch)
    journey = broker.journey(first)
    restarted = m.Broker(broker.path, broker.config)
    assert restarted.journey(first) == journey
    with broker.db() as db:
        db.execute('UPDATE browser_sessions SET expires=0')
    with pytest.raises(PermissionError):
        broker.journey(first)
    again = login(broker, monkeypatch, email='renamed@example.com')
    assert broker.journey(again)['journey_id'] == journey['journey_id']
    assert sum(url.endswith('/cloud/bootstrap') for url, _ in tenant_reply) == 2


def test_two_accounts_read_only_their_own_routes_and_cannot_switch_in_place(broker, monkeypatch, tenant_reply):  # noqa: F811
    broker.registry.register('second', 'https://second.example.com', 'second-token', 'second@example.com')
    first = login(broker, monkeypatch)
    second = login(broker, monkeypatch, sub='second-sub', email='second@example.com')
    a, b = broker.journey(first), broker.journey(second)
    assert a['journey_id'] != b['journey_id']
    assert tenant_reply == [('https://tenant.example.com/cloud/bootstrap', 'Bearer fixture-control'),
                           ('https://tenant.example.com/cloud/status', 'Bearer fixture-control'),
                           ('https://second.example.com/cloud/bootstrap', 'Bearer second-token'),
                           ('https://second.example.com/cloud/status', 'Bearer second-token')]
    with pytest.raises(PermissionError):
        login(broker, monkeypatch, cookie=first, sub='second-sub', email='second@example.com')
    assert broker.browser.require(first)['account_id'] != broker.browser.require(second)['account_id']
    with broker.db() as db:
        db.execute("UPDATE tenants SET status='suspended' WHERE id='second'")
    with pytest.raises(PermissionError):
        broker.journey(second)


def test_challenge_needs_ready_line_account_csrf_and_observed_sender(broker, monkeypatch, tenant_reply):  # noqa: F811
    cookie = login(broker, monkeypatch)
    session = broker.browser.require(cookie)
    csrf = session['secret']['csrf']
    with pytest.raises(ValueError):
        broker.start_link(cookie, csrf)
    broker.messaging_line = {'id': 'fixture-line', 'number': '+15555550100'}
    for bad in (None, '', 'forged'):
        with pytest.raises(PermissionError):
            broker.start_link(cookie, bad)
    intent = broker.start_link(cookie, csrf)
    with pytest.raises(PermissionError):
        broker.confirm_link(cookie, csrf, intent['intent_id'], 'guessed')
    event = {'kind': 'phone', 'handle': '+15555550200', 'conversation': 'fixture-dm',
             'text': intent['message'], 'is_group': False}
    assert not broker.linking.observe(line='wrong-line', **event)
    assert broker.linking.observe(line='fixture-line', **event)
    observed = broker.start_link(cookie, csrf)
    assert observed['candidate']['sender'] == '+15555550200'
    stranger = broker.browser.create()
    with pytest.raises(PermissionError):
        broker.confirm_link(stranger, csrf, observed['intent_id'], observed['proof_revision'])
    broker.messaging_line = {'id': 'replacement-line', 'number': '+15555550300'}
    with pytest.raises(PermissionError):
        broker.confirm_link(cookie, csrf, observed['intent_id'], observed['proof_revision'])
    broker.messaging_line = {'id': 'fixture-line', 'number': '+15555550100'}
    confirmation = broker.confirm_link(cookie, csrf, observed['intent_id'], observed['proof_revision'])
    assert confirmation['status'] == 'verified'
    assert broker.journey(cookie)['next_action'] == 'wait_for_activation'
    with broker.db() as db:
        secret = db.execute('SELECT secret FROM link_intents').fetchone()[0]
        assert broker.open(secret) == {}  # erase completed challenge payload


def test_new_oauth_start_supersedes_previous_tab_and_logout_cancels_callback(broker):  # noqa: F811
    cookie, earlier = begin_browser(broker)
    _, current = begin_browser(broker, cookie)
    with pytest.raises(PermissionError):
        broker.authorize(earlier, 'earlier-code', browser_token=cookie)
    broker.browser.logout(cookie)
    with pytest.raises(PermissionError):
        broker.authorize(current, 'code-after-logout', browser_token=cookie)
    with broker.db() as db:
        assert db.execute('SELECT COUNT(*) FROM cloud_accounts').fetchone()[0] == 0


def test_unreachable_tenant_preserves_journey_without_claiming_readiness(broker, monkeypatch, tenant_reply):  # noqa: F811
    cookie = login(broker, monkeypatch)
    before = broker.journey(cookie)
    def unavailable(*a, **k):
        raise requests.ConnectionError('private upstream detail')
    monkeypatch.setattr(m.requests, 'post', unavailable)
    result = broker.journey(cookie)
    assert result['journey_id'] == before['journey_id'] and result['next_action'] == 'retry_status'
    assert result['context_connected'] is None and 'private' not in json.dumps(result)


@pytest.fixture
def http(broker):  # noqa: F811
    server = m.ThreadingHTTPServer(('127.0.0.1', 0), m.Handler)
    server.broker = broker
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, *, cookie='', origin=None, body=None, form=False, bearer=None):
        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        headers = {'Cookie': f'{COOKIE}={cookie}'} if cookie else {}
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        if origin:
            headers['Origin'] = origin
        payload = None
        if body is not None:
            payload = urlencode(body) if form else json.dumps(body)
            headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
        connection.request(method, path, body=payload, headers=headers)
        result = connection.getresponse()
        response = result.status, dict(result.getheaders()), result.read().decode()
        connection.close()
        return response
    try:
        yield request
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_http_csrf_cookie_rotation_and_no_bearer_in_browser_response(broker, http, tenant_reply):  # noqa: F811
    code, headers, body = http('GET', '/')
    assert code == 200 and 'Connect Google' in body
    header = headers['Set-Cookie']
    assert all(flag in header for flag in ('Secure', 'HttpOnly', 'SameSite=Lax', 'Path=/'))
    cookie = SimpleCookie(header)[COOKIE].value
    csrf = broker.browser.require(cookie)['secret']['csrf']
    for origin, proof in [(None, csrf), ('https://attacker.invalid', csrf), (broker.origin, 'forged')]:
        assert http('POST', '/v1/signin/start', cookie=cookie, origin=origin,
                    body={'entry': 'browser', 'csrf': proof})[0] == 403
    code, _, body = http('POST', '/v1/signin/start', cookie=cookie, origin=broker.origin,
                         body={'entry': 'browser', 'csrf': csrf})
    assert code == 200 and 'poll_token' not in body
    state = parse_qs(urlsplit(json.loads(body)['authorization_url']).query)['state'][0]
    callback = '/oauth/google/callback?' + urlencode({'state': state, 'code': 'fixture-code'})
    assert http('GET', callback)[0] == 400
    code, headers, body = http('GET', callback, cookie=cookie)
    assert code == 303 and headers['Location'] == '/setup'
    rotated = SimpleCookie(headers['Set-Cookie'])[COOKIE].value
    assert rotated not in body
    assert http('GET', '/v1/journey', cookie=cookie)[0] == 401
    code, headers, body = http('GET', '/v1/journey', cookie=rotated)
    assert code == 200 and json.loads(body)['next_action'] == 'wait_for_messages'
    assert headers['Cache-Control'] == 'no-store' and headers['Referrer-Policy'] == 'no-referrer'
    assert 'fixture-control' not in body and 'never-show-in-browser' not in body
    csrf = broker.browser.require(rotated)['secret']['csrf']
    code, headers, _ = http('POST', '/browser/logout', cookie=rotated, origin=broker.origin,
                            body={'csrf': csrf}, form=True)
    assert code == 303 and 'Max-Age=0' in headers['Set-Cookie']
    assert http('GET', '/v1/journey', cookie=rotated)[0] == 401


def test_sender_text_is_escaped_in_confirmation_page():
    page = pages.setup({'account_id': 'account', 'secret': {'csrf': 'fixture'}},
                       {'next_action': 'connect_messages'}, {'status': 'observed', 'intent_id': 'id',
                       'proof_revision': 'revision', 'candidate': {'sender': '<script>alert(1)</script>'}})
    assert '<script>' not in page and '&lt;script&gt;' in page
    assert 'Confirm my Messages identity' in page


def test_legacy_signin_table_retains_rollback_insert_contract(broker):  # noqa: F811
    with broker.db() as db:
        assert len(list(db.execute('PRAGMA table_info(sessions)'))) == 6
        db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,NULL)',
                   (m.digest('legacy-state'), m.digest('legacy-poll'), m.time.time() + m.TTL,
                    'pending', broker.seal({'entry': 'mac'})))
    assert broker.status('legacy-poll') == {'status': 'pending'}


def test_malformed_tenant_handoff_remains_retryable_without_claiming_connection(broker, monkeypatch):  # noqa: F811
    cookie = login(broker, monkeypatch)
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return ['unexpected', 'private-upstream-field']
    monkeypatch.setattr(m.requests, 'post', lambda *a, **k: Response())
    result = broker.journey(cookie)
    assert result['next_action'] == 'wait_for_google'
    assert result['google_connection'] == 'provisioning'
    assert 'private-upstream-field' not in json.dumps(result)


def test_http_device_code_is_browser_only_and_native_confirmation_required(broker, http, tenant_reply):  # noqa: F811
    started = broker.start({'public_key': device(), 'challenge': 'c' * 43})
    assert 'confirmation_code' not in started
    path = urlsplit(started['authorization_url'])
    status, headers, _ = http('GET', path.path + '?' + path.query)
    assert status == 303 and urlsplit(headers['Location']).netloc == 'accounts.google.com'
    cookie = SimpleCookie(headers['Set-Cookie'])[COOKIE].value
    state = parse_qs(path.query)['state'][0]
    callback = '/oauth/google/callback?' + urlencode({'state': state, 'code': 'fixture-code'})
    assert http('GET', callback)[0] == 400
    status, _, body = http('GET', callback, cookie=cookie)
    assert status == 200 and 'Enter this code' in body
    browser_code = broker.device_code(state, cookie)
    assert browser_code in body
    assert http('GET', path.path + '?' + path.query)[0] == 400
    status, _, body = http('GET', '/v1/signin/status', bearer=started['poll_token'])
    assert status == 200 and json.loads(body) == {'status': 'awaiting_device'}
    assert browser_code not in body and not tenant_reply
    assert http('POST', '/v1/signin/confirm', body={'code': browser_code})[0] == 403
    assert http('POST', '/v1/signin/confirm', origin=broker.origin,
                bearer=started['poll_token'], body={'code': browser_code})[0] == 403
    assert http('POST', '/v1/signin/confirm', bearer=started['poll_token'],
                body={'code': browser_code})[0] == 200
