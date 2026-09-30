import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

spec = importlib.util.spec_from_file_location('google_setup', Path(__file__).with_name('google_setup.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
CALLBACK = 'https://sotto.example/google/oauth/callback'
BINDING = hashlib.sha256(b'browser-cookie').hexdigest()


def web_client():
    return {'web': {'client_id': 'fixture', 'client_secret': 'fixture-secret',
                    'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                    'token_uri': 'https://oauth2.googleapis.com/token',
                    'redirect_uris': [CALLBACK]}}


def pending_web(tmp_path, *, scopes=None):
    (tmp_path / 'google_client_secret.json').write_text(json.dumps(web_client()))
    pending = {'state': 'right', 'code_verifier': 'proof', 'created_at': module.time.time(),
               'scopes': scopes or [module.SCOPES['calendar']], 'redirect_uri': CALLBACK,
               'client_id': 'fixture', 'client_kind': 'web', 'browser_binding': BINDING}
    (tmp_path / 'google_oauth_pending.json').write_text(json.dumps(pending))
    return pending


def test_consent_persists_pkce_and_only_requested_read_scopes(tmp_path, monkeypatch):
    (tmp_path / 'google_client_secret.json').write_text(json.dumps(web_client()))
    flow = SimpleNamespace(code_verifier='pkce-proof', authorization_url=lambda **kw: ('https://accounts.google.com/test', 'state'))
    def create(client, **kw):
        assert kw['scopes'] == [module.SCOPES['calendar']]
        assert kw['autogenerate_code_verifier'] is True
        return flow
    monkeypatch.setattr(module.Flow, 'from_client_config', create)
    module.auth_url(tmp_path, 'calendar', browser_binding=BINDING, redirect_uri=CALLBACK)
    pending = json.loads((tmp_path / 'google_oauth_pending.json').read_text())
    assert pending['code_verifier'] == 'pkce-proof'
    assert pending['scopes'] == [module.SCOPES['calendar']]
    assert pending['redirect_uri'] == CALLBACK
    assert pending['browser_binding'] == BINDING
    assert (tmp_path / 'google_oauth_pending.json').stat().st_mode & 0o777 == 0o600


def test_exchange_rejects_wrong_state_and_expiry_before_network(tmp_path, monkeypatch):
    pending = {'state': 'right', 'code_verifier': 'proof', 'created_at': module.time.time(),
               'scopes': [module.SCOPES['calendar']], 'redirect_uri': CALLBACK,
               'client_id': 'fixture', 'client_kind': 'web', 'browser_binding': BINDING}
    path = tmp_path / 'google_oauth_pending.json'
    path.write_text(json.dumps(pending))
    (tmp_path / 'google_client_secret.json').write_text(json.dumps(web_client()))
    monkeypatch.setattr(module.Flow, 'from_client_secrets_file', lambda *a, **kw: pytest.fail('network flow created'))
    with pytest.raises(ValueError, match='state mismatch'):
        module.exchange(tmp_path, CALLBACK + '?code=code&state=wrong', BINDING)
    pending['created_at'] = 0
    path.write_text(json.dumps(pending))
    with pytest.raises(ValueError, match='expired'):
        module.exchange(tmp_path, CALLBACK + '?code=code&state=right', BINDING)


def test_exchange_writes_actual_grants_and_consumes_pending(tmp_path, monkeypatch):
    pending = {'state': 'right', 'code_verifier': 'proof', 'created_at': module.time.time(),
               'scopes': [module.SCOPES['calendar']], 'redirect_uri': CALLBACK,
               'client_id': 'fixture', 'client_kind': 'web', 'browser_binding': BINDING}
    path = tmp_path / 'google_oauth_pending.json'
    path.write_text(json.dumps(pending))
    (tmp_path / 'google_client_secret.json').write_text(json.dumps(web_client()))
    def create(client, **kw):
        assert kw['code_verifier'] == 'proof' and kw['state'] == 'right'
        def fetch(**kwargs):
            assert kwargs == {'code': 'code'}
        creds = SimpleNamespace(refresh_token='offline', granted_scopes=[module.SCOPES['calendar'], 'openid'],
                                to_json=lambda: '{"refresh_token":"offline"}')
        return SimpleNamespace(fetch_token=fetch, credentials=creds)
    monkeypatch.setattr(module.Flow, 'from_client_secrets_file', create)
    module.exchange(tmp_path, CALLBACK + '?' + urlencode({'code': 'code', 'state': 'right'}), BINDING)
    token_path = tmp_path / 'google_token.json'
    assert json.loads(token_path.read_text())['scopes'] == [module.SCOPES['calendar'], 'openid']
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert not path.exists()
    with pytest.raises(FileNotFoundError):
        module.exchange(tmp_path, CALLBACK + '?code=code&state=right', BINDING)


def test_new_desktop_authorization_and_untrusted_web_clients_are_refused(tmp_path, monkeypatch):
    (tmp_path / 'google_client_secret.json').write_text(json.dumps({'installed': {'client_id': 'old'}}))
    with pytest.raises(ValueError, match='Create a Web application'):
        module.auth_url(tmp_path, 'calendar', browser_binding=BINDING, redirect_uri=CALLBACK)
    assert not (tmp_path / 'google_oauth_pending.json').exists()
    for key, bad in [('auth_uri', 'https://attacker.example/authorize'),
                     ('token_uri', 'https://attacker.example/token')]:
        client = web_client()
        client['web'][key] = bad
        (tmp_path / 'google_client_secret.json').write_text(json.dumps(client))
        with pytest.raises(ValueError, match='official Google OAuth endpoints'):
            module.auth_url(tmp_path, 'calendar', browser_binding=BINDING, redirect_uri=CALLBACK)
    (tmp_path / 'google_client_secret.json').write_text(json.dumps(web_client()))
    monkeypatch.setenv('RAILWAY_PUBLIC_DOMAIN', 'other.example')
    with pytest.raises(ValueError, match='configured Railway public domain'):
        module.auth_url(tmp_path, 'calendar', browser_binding=BINDING, redirect_uri=CALLBACK)
    monkeypatch.delenv('RAILWAY_PUBLIC_DOMAIN')
    with pytest.raises(ValueError, match='browser binding'):
        module.auth_url(tmp_path, 'calendar', redirect_uri=CALLBACK)


def test_web_callback_rejections_preserve_existing_token_and_pending(tmp_path, monkeypatch):
    pending_web(tmp_path)
    old_token = '{"refresh_token":"existing"}'
    (tmp_path / 'google_token.json').write_text(old_token)
    monkeypatch.setattr(module.Flow, 'from_client_secrets_file', lambda *a, **kw: pytest.fail('network flow created'))
    cases = [
        ('code', BINDING, 'complete Google HTTPS callback URL'),
        (CALLBACK + '?code=x&state=right', '', 'browser binding mismatch'),
        (CALLBACK + '?code=x&state=right', 'a' * 64, 'browser binding mismatch'),
        (CALLBACK.replace('sotto.example', 'other.example') + '?code=x&state=right', BINDING, 'callback URL mismatch'),
        (CALLBACK + '/?code=x&state=right', BINDING, 'callback URL mismatch'),
        (CALLBACK + '?code=x&state=wrong', BINDING, 'state mismatch'),
        (CALLBACK + '?code=x&code=y&state=right', BINDING, 'one Google authorization code'),
    ]
    for raw, binding, message in cases:
        with pytest.raises(ValueError, match=message):
            module.exchange(tmp_path, raw, binding)
        assert (tmp_path / 'google_token.json').read_text() == old_token
        assert (tmp_path / 'google_oauth_pending.json').exists()


def test_missing_offline_or_partial_grant_never_replaces_token(tmp_path, monkeypatch):
    pending_web(tmp_path, scopes=[module.SCOPES['email'], module.SCOPES['calendar']])
    (tmp_path / 'google_token.json').write_text('old-token')
    credential = SimpleNamespace(refresh_token=None, granted_scopes=[module.SCOPES['calendar']],
                                 to_json=lambda: '{"refresh_token":"new"}')
    monkeypatch.setattr(module.Flow, 'from_client_secrets_file', lambda *a, **kw:
                        SimpleNamespace(fetch_token=lambda **kw: None, credentials=credential))
    callback = CALLBACK + '?code=code&state=right'
    with pytest.raises(ValueError, match='offline access'):
        module.exchange(tmp_path, callback, BINDING)
    credential.refresh_token = 'refresh'
    with pytest.raises(ValueError, match='did not grant all requested'):
        module.exchange(tmp_path, callback, BINDING)
    credential.granted_scopes = None
    with pytest.raises(ValueError, match='did not report granted scopes'):
        module.exchange(tmp_path, callback, BINDING)
    assert (tmp_path / 'google_token.json').read_text() == 'old-token'
    assert (tmp_path / 'google_oauth_pending.json').exists()


def test_concurrent_callback_consumes_pending_only_once(tmp_path, monkeypatch):
    pending_web(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def create(*args, **kwargs):
        def fetch(**_):
            calls.append(1)
            entered.set()
            assert release.wait(5)
        creds = SimpleNamespace(refresh_token='refresh', granted_scopes=[module.SCOPES['calendar']],
                                to_json=lambda: '{"refresh_token":"refresh"}')
        return SimpleNamespace(fetch_token=fetch, credentials=creds)

    monkeypatch.setattr(module.Flow, 'from_client_secrets_file', create)
    callback = CALLBACK + '?code=code&state=right'
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(module.exchange, tmp_path, callback, BINDING)
        assert entered.wait(5)
        second = pool.submit(module.exchange, tmp_path, callback, BINDING)
        release.set()
        first.result(timeout=5)
        with pytest.raises(FileNotFoundError):
            second.result(timeout=5)
    assert len(calls) == 1


def test_cloud_token_installs_only_allowed_grants_and_clears_on_decline(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('cloud_google_test', Path(__file__).with_name('google_setup.py'))
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    credential = {'type': 'authorized_user', 'token_uri': 'https://oauth2.googleapis.com/token',
                  'client_id': 'client', 'client_secret': 'private-fixture', 'refresh_token': 'refresh',
                  'token': 'access', 'scopes': ['https://www.googleapis.com/auth/gmail.compose']}
    assert adapter.install_cloud_google(credential) == credential['scopes']
    path = tmp_path / 'google_token.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == credential
    with pytest.raises(ValueError):
        adapter.install_cloud_google({**credential, 'token_uri': 'https://other.example/token'})
    assert json.loads(path.read_text()) == credential
    assert adapter.install_cloud_google(None) == []
    assert not path.exists()


def test_cloud_accepts_full_or_partial_grants_without_inventing_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    base = {'type': 'authorized_user', 'token_uri': 'https://oauth2.googleapis.com/token',
            'client_id': 'client', 'client_secret': 'fixture', 'refresh_token': 'fixture', 'token': 'fixture'}
    for grants in [list(module.SCOPES.values()), ['https://www.googleapis.com/auth/contacts.readonly']]:
        assert module.install_cloud_google({**base, 'scopes': grants}) == grants
        assert json.loads((tmp_path / 'google_token.json').read_text())['scopes'] == grants


def test_real_cli_reuses_live_session_and_renews_expired_session(tmp_path):
    client = tmp_path / 'client.json'
    client.write_text(json.dumps(web_client()))
    root = tmp_path / 'hermes'
    env = {**os.environ, 'HERMES_HOME': str(root), 'RAILWAY_PUBLIC_DOMAIN': 'sotto.example'}
    argv = [sys.executable, str(Path(module.__file__))]

    def run(*args):
        result = subprocess.run([*argv, *args], env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {'ok': True}
        assert not result.stderr

    run('--client-secret', str(client))
    auth_args = ('--auth-url', '--services', 'email,calendar', '--format', 'json',
                 '--browser-binding', BINDING, '--redirect-uri', CALLBACK, '--reuse-pending')
    run(*auth_args)
    pending_path = root / 'google_oauth_pending.json'
    url_path = root / 'google_oauth_last_url.txt'
    original = pending_path.read_text()
    original_url = url_path.read_text()
    pending = json.loads(original)
    query = parse_qs(urlparse(original_url).query)
    assert query['state'] == [pending['state']]
    assert query['code_challenge_method'] == ['S256']
    assert query['redirect_uri'] == [CALLBACK]
    assert set(query['scope'][0].split()) == {module.SCOPES['email'], module.SCOPES['calendar']}
    assert pending['code_verifier'] and not (root / 'google_token.json').exists()
    for path in (pending_path, url_path, root / 'google_client_secret.json'):
        assert path.stat().st_mode & 0o777 == 0o600
    run(*auth_args)
    assert pending_path.read_text() == original and url_path.read_text() == original_url
    pending['created_at'] = 0
    pending_path.write_text(json.dumps(pending))
    run(*auth_args)
    assert json.loads(pending_path.read_text())['state'] != pending['state']
    assert url_path.read_text() != original_url
    run('--auth-url', '--services', 'calendar', '--reuse-pending',
        '--browser-binding', BINDING, '--redirect-uri', CALLBACK)
    assert json.loads(pending_path.read_text())['scopes'] == [module.SCOPES['calendar']]
    replacement = json.loads(client.read_text())
    replacement['web']['client_id'] = 'replacement-fixture'
    client.write_text(json.dumps(replacement))
    run('--client-secret', str(client))
    run('--auth-url', '--services', 'calendar', '--reuse-pending',
        '--browser-binding', BINDING, '--redirect-uri', CALLBACK)
    assert json.loads(pending_path.read_text())['client_id'] == 'replacement-fixture'


@pytest.mark.parametrize('pending', [
    # main's managed helper and upstream Hermes setup.py wrote these before browser callbacks.
    {'state': 's', 'code_verifier': 'v', 'scopes': [module.SCOPES['calendar']],
     'created_at': module.time.time(), 'redirect_uri': 'http://localhost:1'},
    {'state': 's', 'code_verifier': 'v', 'redirect_uri': 'http://localhost:1'},
])
def test_pre_browser_pending_files_are_refused_before_network(tmp_path, monkeypatch, pending):
    (tmp_path / 'google_client_secret.json').write_text(
        json.dumps({'installed': {'client_id': 'desk', 'client_secret': 'fixture'}}))
    path = tmp_path / 'google_oauth_pending.json'
    path.write_text(json.dumps(pending))
    (tmp_path / 'google_token.json').write_text('existing-grant')
    monkeypatch.setattr(module.Flow, 'from_client_secrets_file', lambda *a, **kw: pytest.fail('network flow created'))
    for raw in ('legacy-code', 'http://localhost:1/?code=legacy-code&state=s'):
        with pytest.raises((ValueError, KeyError)):
            module.exchange(tmp_path, raw)
    assert json.loads(path.read_text()) == pending
    assert (tmp_path / 'google_token.json').read_text() == 'existing-grant'


def expired_token(path, refresh):
    path.write_text(json.dumps({'token': 'old', 'refresh_token': refresh, 'client_id': 'c',
                                'client_secret': 's', 'token_uri': 'https://oauth2.googleapis.com/token',
                                'expiry': '2000-01-01T00:00:00Z'}))


def test_check_refresh_writes_token_when_unchanged(tmp_path, monkeypatch):
    token = tmp_path / 'google_token.json'
    expired_token(token, 'old-refresh')

    def refresh(self, request):
        self.token, self.expiry = 'refreshed', None
    monkeypatch.setattr(module.Credentials, 'refresh', refresh)
    assert module.check(tmp_path)
    saved = json.loads(token.read_text())
    assert saved['token'] == 'refreshed' and saved['refresh_token'] == 'old-refresh'
    assert token.stat().st_mode & 0o777 == 0o600


def test_check_refresh_never_overwrites_token_replaced_during_refresh(tmp_path, monkeypatch):
    token = tmp_path / 'google_token.json'
    expired_token(token, 'old-refresh')
    entered, release = threading.Event(), threading.Event()

    def slow_refresh(self, request):
        entered.set()
        assert release.wait(5)
        self.token, self.expiry = 'old-refreshed', None
    monkeypatch.setattr(module.Credentials, 'refresh', slow_refresh)
    with ThreadPoolExecutor(max_workers=1) as pool:
        checked = pool.submit(module.check, tmp_path)
        assert entered.wait(5)
        # A reconnect completes meanwhile, writing under the same lock as exchange().
        with module.pending_lock(tmp_path):
            module.write_private(token, json.dumps({'token': 'new', 'refresh_token': 'NEW-refresh'}))
        release.set()
        assert checked.result(timeout=5)
    assert json.loads(token.read_text())['refresh_token'] == 'NEW-refresh'
