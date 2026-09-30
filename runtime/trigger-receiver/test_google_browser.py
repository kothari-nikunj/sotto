"""Exercise browser cookies, the real consent CLI, and callback/token boundary offline."""
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from http.server import ThreadingHTTPServer

import pytest

HERE = Path(__file__).parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def browser_server(tmp_path, monkeypatch):
    receiver = load('browser_callback_receiver', HERE / 'receiver.py')
    helper = load('browser_callback_helper', HERE.parents[1] / 'adapters/hermes/google_setup.py')
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    monkeypatch.setenv('RAILWAY_PUBLIC_DOMAIN', 'sotto.example')
    monkeypatch.setattr(receiver, 'RAILWAY_DOMAIN', 'sotto.example')
    monkeypatch.setattr(receiver, 'SETUP_CODE', 'setup-fixture')
    monkeypatch.setattr(receiver, 'GAUTH_FILE', str(tmp_path / 'old-url'))
    monkeypatch.setattr(receiver, 'capture_google_account_email', lambda: None)
    monkeypatch.setattr(receiver, 'google_connected', lambda: ((home / 'google_token.json').exists(), ''))
    monkeypatch.setattr(receiver.shutil, 'which', lambda _name: sys.executable)
    callback = 'https://sotto.example/google/oauth/callback'
    client = {'web': {'client_id': 'fixture', 'client_secret': 'fixture-secret',
                      'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                      'token_uri': 'https://oauth2.googleapis.com/token', 'redirect_uris': [callback]}}
    ok, detail = receiver.setup_google_client(json.dumps(client))
    assert ok, detail
    exchanges = []
    grants = [helper.SCOPES['email'], helper.SCOPES['calendar']]
    credentials = SimpleNamespace(refresh_token='fixture-refresh', granted_scopes=grants,
                                  to_json=lambda: json.dumps({'token': 'fixture-token'}))
    monkeypatch.setattr(helper.Flow, 'from_client_secrets_file',
                        lambda *a, **kw: SimpleNamespace(credentials=credentials,
                                                        fetch_token=lambda **kw: exchanges.append(kw)))
    real_run = receiver.subprocess.run

    def run(argv, **kwargs):
        if '--auth-code' not in argv:
            return real_run(argv, **kwargs)  # actual auth URL generation and PKCE persistence
        try:
            helper.exchange(home, argv[argv.index('--auth-code') + 1],
                            argv[argv.index('--browser-binding') + 1])
            return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr='')
        except (OSError, ValueError):
            return SimpleNamespace(returncode=1, stdout='rejected', stderr='')

    monkeypatch.setattr(receiver.subprocess, 'run', run)
    server = ThreadingHTTPServer(('127.0.0.1', 0), receiver.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(path, method='GET', headers=None, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        result = response.status, response.read().decode(), response.getheaders()
        conn.close()
        return result

    try:
        yield SimpleNamespace(receiver=receiver, helper=helper, home=home, client=client,
                              request=request, exchanges=exchanges, callback=callback)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def begin(b):
    status, body, headers = b.request('/google/auth?code=setup-fixture')
    assert status == 200
    assert 'localhost:1' not in body and 'paste the code' not in body
    assert 'automatically' in body
    cookies = [value.split(';', 1)[0] for key, value in headers if key == 'Set-Cookie']
    csrf = re.search(r"name='csrf' value='([^']+)'", body).group(1)
    browser_cookie = next(c for c in cookies if c.startswith('sotto_google_browser='))
    value = browser_cookie.split('=', 1)[1]
    assert csrf == hashlib.sha256(value.encode()).hexdigest()
    for key, cookie in headers:
        if key == 'Set-Cookie':
            assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
    return '; '.join(cookies), csrf


def start(b, cookies, csrf, origin='null'):
    return b.request('/google/start', 'POST',
                     {'Cookie': cookies, 'Origin': origin,
                      'Content-Type': 'application/x-www-form-urlencoded'}, urlencode({'csrf': csrf}))


def test_google_browser_success_returns_to_clean_sotto_page(browser_server):
    b = browser_server
    cookies, csrf = begin(b)
    status, body, headers = start(b, cookies, csrf)
    assert status == 200  # no form redirect across origins under form-action 'self'
    assert 'window.location.replace(' in body and 'Continue with Google' in body
    assert 'form-action \'self\'' in dict(headers)['Content-Security-Policy']
    assert dict(headers)['Referrer-Policy'] == 'no-referrer'
    pending = json.loads((b.home / 'google_oauth_pending.json').read_text())
    auth = parse_qs(urlsplit((b.home / 'google_oauth_last_url.txt').read_text()).query)
    assert auth['redirect_uri'] == [b.callback]
    assert auth['code_challenge_method'] == ['S256']
    callback = '/google/oauth/callback?' + urlencode({'state': pending['state'], 'code': 'fixture-code'})
    status, body, headers = b.request(callback, headers={'Cookie': cookies, 'Host': 'attacker.example'})
    assert status == 302 and dict(headers)['Location'] == '/google/connected'
    assert not body and 'fixture-code' not in str(headers) and pending['state'] not in str(headers)
    assert dict(headers)['Cache-Control'] == 'no-store'
    assert len(b.exchanges) == 1
    token = b.home / 'google_token.json'
    assert token.stat().st_mode & 0o777 == 0o600
    assert not (b.home / 'google_oauth_pending.json').exists()
    status, body, _ = b.request('/google/connected', headers={'Cookie': cookies})
    assert status == 200 and 'Google is connected' in body and 'Continue with Google' not in body
    # Even if an old browser cookie is replayed, no second token exchange occurs.
    status, _, headers = b.request(callback, headers={'Cookie': cookies})
    assert dict(headers)['Location'] == '/google/retry' and len(b.exchanges) == 1


@pytest.mark.parametrize('failure', ['wrong-browser', 'wrong-state', 'denied', 'expired', 'duplicate-code'])
def test_rejected_callback_preserves_existing_grant(browser_server, failure):
    b = browser_server
    cookies, csrf = begin(b)
    assert start(b, cookies, csrf)[0] == 200
    pending_file = b.home / 'google_oauth_pending.json'
    pending = json.loads(pending_file.read_text())
    query = {'state': pending['state'], 'code': 'private-fixture'}
    if failure == 'wrong-browser':
        cookies = 'sotto_setup=setup-fixture; sotto_google_browser=' + 'x' * 43
    elif failure == 'wrong-state':
        query['state'] = 'different'
    elif failure == 'denied':
        query = {'state': pending['state'], 'error': 'access_denied'}
    elif failure == 'expired':
        pending['created_at'] = 0
        pending_file.write_text(json.dumps(pending))
    token = b.home / 'google_token.json'
    token.write_text('existing-grant')
    path = '/google/oauth/callback?' + urlencode(query)
    if failure == 'duplicate-code':
        path += '&code=another'
    status, _, headers = b.request(path, headers={'Cookie': cookies})
    assert status == 302 and dict(headers)['Location'] == '/google/retry'
    assert token.read_text() == 'existing-grant' and not b.exchanges
    status, body, _ = b.request('/google/retry')
    assert status == 200 and 'Return to setup' in body and 'private-fixture' not in body


def test_start_requires_authenticated_page_and_browser_form_secret(browser_server):
    b = browser_server
    assert b.request('/google/start', 'POST', body='csrf=x')[0] == 403
    cookies, csrf = begin(b)
    for supplied_cookie, supplied_csrf, origin in [
        ('sotto_setup=setup-fixture', csrf, 'null'),
        (cookies, 'wrong', 'null'),
        (cookies, csrf, 'https://attacker.example'),
    ]:
        status, _, headers = start(b, supplied_cookie, supplied_csrf, origin)
        assert status == 302 and dict(headers)['Location'] == '/google/retry'
        assert not (b.home / 'google_oauth_pending.json').exists()


def test_desktop_client_gets_migration_form_without_broken_authorization(browser_server):
    b = browser_server
    (b.home / 'google_client_secret.json').write_text(json.dumps({'installed': {'client_id': 'legacy'}}))
    status, body, _ = b.request('/google/auth?code=setup-fixture')
    assert status == 200 and 'Web application' in body and b.callback in body
    assert 'localhost:1' not in body and '/google/start' not in body
    (b.home / 'google_token.json').write_text('existing-grant')
    status, body, _ = b.request('/google/auth?code=setup-fixture')
    assert status == 200 and 'Google is connected' in body and 'textarea' not in body


def test_failed_exchange_names_client_causes_and_lets_user_replace_client(browser_server, monkeypatch):
    # A structurally valid Web client whose secret was rotated fails only at token exchange.
    b = browser_server
    real = b.receiver.subprocess.run

    def failing(argv, **kw):
        if '--auth-code' in argv:
            return SimpleNamespace(returncode=1, stdout='', stderr='invalid_client')
        return real(argv, **kw)
    monkeypatch.setattr(b.receiver.subprocess, 'run', failing)
    cookies, csrf = begin(b)
    assert start(b, cookies, csrf)[0] == 200
    pending = json.loads((b.home / 'google_oauth_pending.json').read_text())
    callback = '/google/oauth/callback?' + urlencode({'state': pending['state'], 'code': 'c'})
    assert dict(b.request(callback, headers={'Cookie': cookies})[2])['Location'] == '/google/retry'
    _, retry, _ = b.request('/google/retry')
    assert 'secret was rotated or deleted' in retry and 'redirect URI' in retry
    assert 'Return to setup' in retry and "href='/google/auth?replace=1'" in retry
    _, auth, _ = b.request('/google/auth?code=setup-fixture')
    _, setup, _ = b.request('/setup?code=setup-fixture')
    for page in (auth, setup):
        assert 'Use a different Google client' in page and 'textarea' in page
        assert "action='/setup/google-client?code=setup-fixture'" in page
    assert 'Continue with Google' in auth and '<details open>' not in auth
    # The retry link opens the replacement form, even while an older grant still works.
    token = b.home / 'google_token.json'
    token.write_text('existing-grant')
    _, replace, _ = b.request('/google/auth?replace=1', headers={'Cookie': cookies})
    assert '<details open>' in replace and 'textarea' in replace
    _, setup, _ = b.request('/setup?code=setup-fixture')
    assert "replace=1'>Use a different Google client" in setup

    def post_client(client):
        return b.request('/setup/google-client', 'POST',
                         {'Cookie': cookies, 'Content-Type': 'application/x-www-form-urlencoded'},
                         urlencode({'client_json': json.dumps(client)}))
    saved = b.home / 'google_client_secret.json'
    assert post_client({'installed': {'client_id': 'desktop'}})[0] == 400
    assert json.loads(saved.read_text()) == b.client
    replacement = json.loads(json.dumps(b.client))
    replacement['web'].update(client_id='replacement', client_secret='replacement-secret')
    status, body, _ = post_client(replacement)
    assert status == 200 and 'Client saved' in body and 'replacement-secret' not in body
    assert json.loads(saved.read_text()) == replacement
    assert token.read_text() == 'existing-grant'
    assert not (b.home / 'google_oauth_pending.json').exists()


def test_reloading_google_page_keeps_consent_in_flight(browser_server):
    b = browser_server
    cookies, csrf = begin(b)
    assert start(b, cookies, csrf)[0] == 200
    pending = json.loads((b.home / 'google_oauth_pending.json').read_text())
    # A reload or second tab presents the browser's existing binding cookie.
    status, body, headers = b.request('/google/auth?code=setup-fixture', headers={'Cookie': cookies})
    assert status == 200
    reissued = [v.split(';', 1)[0] for k, v in headers
                if k == 'Set-Cookie' and v.startswith('sotto_google_browser=')]
    assert reissued and reissued[0] in cookies.split('; ')
    assert re.search(r"name='csrf' value='([^']+)'", body).group(1) == csrf
    callback = '/google/oauth/callback?' + urlencode({'state': pending['state'], 'code': 'c'})
    status, _, headers = b.request(callback, headers={'Cookie': cookies})
    assert dict(headers)['Location'] == '/google/connected' and len(b.exchanges) == 1


def test_callback_without_this_browsers_pending_flow_spawns_nothing(browser_server, monkeypatch):
    b = browser_server
    spawned = []
    real = b.receiver.subprocess.run
    monkeypatch.setattr(b.receiver.subprocess, 'run',
                        lambda argv, **kw: spawned.append(argv) or real(argv, **kw))
    forged = {'Cookie': 'sotto_google_browser=' + 'A' * 43}
    callback = '/google/oauth/callback?code=x&state=y'
    assert dict(b.request(callback, headers=forged)[2])['Location'] == '/google/retry'
    cookies, csrf = begin(b)
    assert start(b, cookies, csrf)[0] == 200
    spawned.clear()
    # Another browser's flow is pending: a forged cookie still must not fork the setup CLI.
    assert dict(b.request(callback, headers=forged)[2])['Location'] == '/google/retry'
    assert not spawned and (b.home / 'google_oauth_pending.json').exists()


def test_second_start_keeps_consent_in_flight_until_the_client_changes(browser_server):
    b = browser_server
    cookies, csrf = begin(b)
    assert start(b, cookies, csrf)[0] == 200
    first = json.loads((b.home / 'google_oauth_pending.json').read_text())
    assert start(b, cookies, csrf)[0] == 200  # second tab or back button
    assert json.loads((b.home / 'google_oauth_pending.json').read_text())['state'] == first['state']
    replacement = json.loads(json.dumps(b.client))
    replacement['web'].update(client_id='replacement')
    assert b.receiver.setup_google_client(json.dumps(replacement))[0]
    assert start(b, cookies, csrf)[0] == 200
    fresh = json.loads((b.home / 'google_oauth_pending.json').read_text())
    assert fresh['state'] != first['state'] and fresh['client_id'] == 'replacement'
    callback = '/google/oauth/callback?' + urlencode({'state': fresh['state'], 'code': 'c'})
    assert dict(b.request(callback, headers={'Cookie': cookies})[2])['Location'] == '/google/connected'
