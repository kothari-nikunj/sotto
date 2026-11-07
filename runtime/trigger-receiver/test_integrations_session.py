"""Dashboard sign-in opens Integrations without upgrading the browser's credentials."""
import json
import time
from urllib.parse import urlencode

import pytest

from test_dashboard import _get, _login, _post, _server, SETUP_CODE


@pytest.fixture
def integrations(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('SOTTO_DASHBOARD', '1')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    rec, server, base = _server(tmp_path, with_static=False)
    rec.RAILWAY_DOMAIN = 'sotto.example'
    rec.RELAY_TOKEN = 'private-fixture-relay'
    rec.google_connected = lambda: (False, 'not connected')
    calls = []
    monkeypatch.setattr(rec.CONNECTORS, 'start_auth',
        lambda service, redirect: calls.append(service) or 'https://provider.example/approve')
    try:
        yield rec, base, calls
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_login_opens_integrations_and_connector_without_bootstrap_secret(integrations):
    rec, base, calls = integrations
    cookie = _login(base)
    assert _get(base, '/app', cookie)[0] == 200
    status, body, headers = _get(base, '/setup', cookie)
    assert status == 200
    assert 'Integrations' in body and '/connect/granola/start' in body
    assert SETUP_CODE not in body and rec.RELAY_TOKEN not in body
    assert 'sotto_setup=' not in headers.get('Set-Cookie', '')
    status, body, _ = _get(base, '/google/auth?replace=1', cookie)
    assert status == 200 and SETUP_CODE not in body
    csrf = json.loads(_get(base, '/api/session', cookie)[1])['csrf']
    assert f"name='csrf' value='{csrf}'" in body
    status, _, headers = _get(base, '/connect/granola/start', cookie)
    assert status == 302 and headers['Location'] == 'https://provider.example/approve'
    assert calls == ['granola']
    assert 'sotto_setup=' not in headers.get('Set-Cookie', '')
    assert _get(base, '/pair', cookie)[0] == 403
    assert _get(base, '/whatsapp/qr', cookie)[0] in (403, 404)


def test_session_setup_writes_require_existing_csrf_and_stay_in_session(integrations, monkeypatch):
    rec, base, _ = integrations
    cookie = _login(base)
    csrf = json.loads(_get(base, '/api/session', cookie)[1])['csrf']
    changes = []
    monkeypatch.setattr(rec, 'set_timezone', lambda tz: changes.append(tz) or (True, tz))
    body = b'{"timezone":"America/Los_Angeles"}'
    for token in ('', 'wrong'):
        assert _post(base, '/setup/timezone', body,
            {**cookie, 'Content-Type': 'application/json', 'X-Sotto-CSRF': token})[0] == 403
    assert changes == []
    assert _post(base, '/setup/timezone', body,
        {**cookie, 'Content-Type': 'application/json', 'X-Sotto-CSRF': csrf})[0] == 200
    assert changes == ['America/Los_Angeles']
    disconnects = []
    monkeypatch.setattr(rec.CONNECTORS, 'disconnect', lambda service, revoke:
        disconnects.append(service) or {'disconnected': True})
    body = b'{"service":"granola"}'
    assert _post(base, '/setup/disconnect', body,
        {**cookie, 'Content-Type': 'application/json'})[0] == 403
    assert disconnects == []
    assert _post(base, '/setup/disconnect', body,
        {**cookie, 'Content-Type': 'application/json', 'X-Sotto-CSRF': csrf})[0] == 200
    assert disconnects == ['granola']
    monkeypatch.setattr(rec, 'setup_google_client', lambda _: (True, 'Client saved'))
    status, body, headers = _post(base, '/setup/google-client',
        urlencode({'client_json': '{}', 'csrf': csrf}).encode(), cookie)
    assert status == 200 and SETUP_CODE not in body
    assert 'sotto_setup=' not in headers.get('Set-Cookie', '')


@pytest.mark.parametrize('mode', ['expired', 'deleted', 'cloud', 'disabled'])
def test_invalid_or_scoped_sessions_cannot_open_setup(integrations, monkeypatch, mode):
    rec, base, _ = integrations
    cookie = _login(base)
    sessions = rec.DASHBOARD._load_sessions()
    if mode == 'expired':
        next(iter(sessions.values()))['last_seen'] = '2000-01-01T00:00:00+00:00'
    elif mode == 'deleted':
        sessions.clear()
    elif mode == 'cloud':
        import cloud_pairing
        next(iter(sessions.values()))['cloud'] = {'grant': 'lease', 'expires_at': time.time() + 3600}
        monkeypatch.setattr(cloud_pairing, 'dashboard_session_active', lambda _: True)
    else:
        monkeypatch.setenv('SOTTO_DASHBOARD', '0')
    rec.DASHBOARD._write_sessions(sessions)
    assert _get(base, '/setup', cookie)[0] == 403

