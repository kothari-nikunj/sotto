"""Native readiness is bounded, bearer-only and never a configuration writer."""
import io
from email.message import Message

import pytest

import selfhost_status as status


class Handler:
    def __init__(self, body=b'{}', *, authorized=True, path='/selfhost/status'):
        self.path, self.authorized = path, authorized
        self.rfile = io.BytesIO(body)
        self.headers = Message()
        self.headers['Content-Type'] = 'application/json'
        self.headers['Content-Length'] = str(len(body))

    def _bridge_authed(self):
        return self.authorized

    def _send(self, code, body):
        return code, body


@pytest.fixture(autouse=True)
def selfhost(monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self_host')


def snapshot(**overrides):
    args = dict(runtime={'channel': 'photon', 'model_configured': True},
                google_connected=False, context_connected=False,
                messaging_active=False, first_brief='waiting', number='+15555551234')
    return status.response(**(args | overrides))


def test_readiness_does_not_confuse_configured_model_with_channel_or_context():
    result = snapshot()
    assert result == {'version': 1, 'deployment': 'self_host', 'channel': 'photon',
                      'configuration_mode': 'read_only', 'model_configured': True,
                      'google_connected': False, 'context_connected': False,
                      'messaging_active': False, 'first_brief': 'waiting',
                      'sotto_number': '+15555551234'}
    assert status.handle(Handler(), status=lambda: result) == (200, result)


def test_telegram_returns_only_validated_chat_link_not_new_pairing_authority():
    result = snapshot(runtime={'channel': 'telegram', 'model_configured': True},
                      telegram_bot='sotto_owner_bot', first_brief='delivered',
                      messaging_active=True)
    assert result['telegram_chat_url'] == 'https://t.me/sotto_owner_bot'
    assert 'sotto_number' not in result and 'telegram_pairing_url' not in result
    for bot in ('https://evil.test', '../name', 'owner?start=secret', 'bad\nname'):
        assert 'telegram_chat_url' not in snapshot(
            runtime={'channel': 'telegram', 'model_configured': True}, telegram_bot=bot)


@pytest.mark.parametrize('field,value', [('messaging_active', 'true'),
                                        ('context_connected', 1),
                                        ('first_brief', 'unrecognized'),
                                        ('runtime', {'channel': 'other', 'model_configured': True})])
def test_unknown_status_cannot_be_presented_as_success(field, value):
    with pytest.raises(ValueError):
        snapshot(**{field: value})


@pytest.mark.parametrize('path', ['/selfhost/status?code=secret', '/selfhost/configure',
                                  '/selfhost/status/'])
def test_only_exact_read_only_endpoint_exists(path):
    assert status.handle(Handler(path=path), status=lambda: pytest.fail('status read'))[0] == 404


def test_managed_never_uses_root_bearer_selfhost_surface(monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    assert status.handle(Handler(), status=lambda: pytest.fail('status read'))[0] == 404


def test_auth_precedes_body_and_status():
    handler = Handler(body=b'{bad', authorized=False)
    assert status.handle(handler, status=lambda: pytest.fail('status read'))[0] == 401
    assert handler.rfile.tell() == 0


@pytest.mark.parametrize('body', [b'', b'[]', b'null', b'{"update":true}', b'false',
                                  b'{bad', b'{}' + b' ' * 1024])
def test_body_is_small_empty_object(body):
    assert status.handle(Handler(body), status=lambda: pytest.fail('status read'))[0] == 400


@pytest.mark.parametrize('header,value', [('Origin', 'https://evil.test'), ('Origin', 'null'),
                                        ('Transfer-Encoding', 'chunked'),
                                        ('Content-Length', '2'),
                                        ('Content-Type', 'text/plain')])
def test_browser_and_ambiguous_requests_are_rejected(header, value):
    handler = Handler()
    if header == 'Content-Type':
        del handler.headers[header]
    handler.headers[header] = value
    assert status.handle(handler, status=lambda: pytest.fail('status read'))[0] == 400


def test_short_read_and_non_decimal_length_rejected():
    for length in ('3', '-1', '+2', '1.0'):
        handler = Handler()
        handler.headers.replace_header('Content-Length', length)
        assert status.handle(handler, status=lambda: pytest.fail('status read'))[0] == 400


def test_status_failure_is_sanitized():
    def broken():
        raise RuntimeError('private-provider-token or /private/source/path')
    assert status.handle(Handler(), status=broken) == (
        503, {'error': 'self-host readiness unavailable'})


def test_setup_link_is_explicit_authority_checked_and_not_part_of_status():
    calls = []
    def current():
        calls.append('authority')
        return snapshot()
    def capability():
        calls.append('capability')
        return 'privateCode_123'
    result = status.handle(Handler(path='/selfhost/setup-link'), status=current, setup_code=capability)
    assert result == (200, {'version': 1, 'setup_code': 'privateCode_123'})
    assert calls == ['authority', 'capability']
    calls.clear()
    code, body = status.handle(Handler(), status=current, setup_code=capability)
    assert code == 200 and 'setup_code' not in body
    assert calls == ['authority']


def test_setup_link_requires_root_auth_before_reading_body_or_issuing_secret():
    handler = Handler(path='/selfhost/setup-link', body=b'{bad', authorized=False)
    assert status.handle(handler, status=lambda: pytest.fail('authority read'),
                         setup_code=lambda: pytest.fail('capability issued'))[0] == 401
    assert handler.rfile.tell() == 0


@pytest.mark.parametrize('path', ['/selfhost/setup-link?code=secret', '/selfhost/setup-link/',
                                  '/selfhost/setup-link?host=https://evil.test'])
def test_setup_link_rejects_url_credentials_and_arbitrary_destinations(path):
    assert status.handle(Handler(path=path), status=lambda: pytest.fail('authority read'),
                         setup_code=lambda: pytest.fail('capability issued'))[0] == 404


@pytest.mark.parametrize('header,value', [('Origin', 'https://evil.test'), ('Origin', 'null'),
    ('Cookie', 'sotto_setup=known-code'), ('Transfer-Encoding', 'chunked'),
    ('Content-Length', '2'), ('Content-Type', 'text/plain')])
def test_setup_link_refuses_browser_and_ambiguous_requests(header, value):
    handler = Handler(path='/selfhost/setup-link')
    if header == 'Content-Type':
        del handler.headers[header]
    handler.headers[header] = value
    assert status.handle(handler, status=lambda: pytest.fail('authority read'),
                         setup_code=lambda: pytest.fail('capability issued'))[0] == 400


@pytest.mark.parametrize('body', [b'', b'[]', b'null', b'{"host":"https://evil.test"}',
                                  b'{bad', b'{}' + b' ' * 1024])
def test_setup_link_accepts_only_bounded_empty_object(body):
    assert status.handle(Handler(body, path='/selfhost/setup-link'),
        status=lambda: pytest.fail('authority read'),
        setup_code=lambda: pytest.fail('capability issued'))[0] == 400


@pytest.mark.parametrize('code', ['short', 'a' * 129, 'abc?token=secret', 'private\nCode_123', None])
def test_setup_link_sanitizes_invalid_or_secret_bearing_errors(code):
    assert status.handle(Handler(path='/selfhost/setup-link'), status=snapshot, setup_code=lambda: code) == (
        503, {'error': 'self-host readiness unavailable'})


def test_setup_link_failure_never_resolves_secret_and_managed_route_stays_absent(monkeypatch):
    def broken():
        raise RuntimeError('provider-secret or private-path')
    assert status.handle(Handler(path='/selfhost/setup-link'), status=broken,
        setup_code=lambda: pytest.fail('capability issued')) == (
            503, {'error': 'self-host readiness unavailable'})
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    assert status.handle(Handler(path='/selfhost/setup-link'), status=lambda: pytest.fail('authority read'),
        setup_code=lambda: pytest.fail('capability issued'))[0] == 404


def test_receiver_dispatches_explicit_setup_link_through_same_native_guards(monkeypatch):
    import receiver
    monkeypatch.setattr(receiver, 'selfhost_bridge_status', snapshot)
    monkeypatch.setattr(receiver, 'resolve_setup_code', lambda: 'privateCode_123')
    assert receiver.Handler.do_POST(Handler(path='/selfhost/setup-link')) == (
        200, {'version': 1, 'setup_code': 'privateCode_123'})


def test_http_setup_link_uses_root_bridge_bearer_and_no_store(monkeypatch):
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    import receiver
    monkeypatch.setattr(receiver, 'RELAY_TOKEN', 'private-root-bridge')
    monkeypatch.setattr(receiver, 'MCP_TOKEN', 'scoped-model-reader')
    monkeypatch.setattr(receiver, 'selfhost_bridge_status', snapshot)
    calls = []
    monkeypatch.setattr(receiver, 'resolve_setup_code', lambda: calls.append('issue') or 'privateCode_123')
    server = ThreadingHTTPServer(('127.0.0.1', 0), receiver.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f'http://127.0.0.1:{server.server_port}/selfhost/setup-link'
    def post(bearer=None, cookie=None):
        headers = {'Content-Type': 'application/json'}
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        if cookie:
            headers['Cookie'] = cookie
        request = urllib.request.Request(endpoint, data=b'{}', headers=headers)
        try:
            return urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            return error
    try:
        for bearer, cookie in ((None, 'sotto_setup=privateCode_123'), ('scoped-model-reader', None)):
            with post(bearer, cookie) as response:
                assert response.status == 401
                assert 'setup_code' not in json.load(response)
        with post('private-root-bridge', 'sotto_setup=privateCode_123') as response:
            assert response.status == 400
        assert calls == []
        with post('private-root-bridge') as response:
            assert response.status == 200
            assert response.headers['Cache-Control'] == 'no-store'
            assert response.headers.get('Access-Control-Allow-Origin') is None
            assert json.load(response) == {'version': 1, 'setup_code': 'privateCode_123'}
        assert calls == ['issue']
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
