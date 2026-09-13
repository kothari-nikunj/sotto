import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

spec = importlib.util.spec_from_file_location('google_setup', Path(__file__).with_name('google_setup.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_consent_persists_pkce_and_only_requested_read_scopes(tmp_path, monkeypatch):
    (tmp_path / 'google_client_secret.json').write_text('{"installed": {}}')
    flow = SimpleNamespace(code_verifier='pkce-proof', authorization_url=lambda **kw: ('https://accounts.google.com/test', 'state'))
    def create(client, **kw):
        assert kw['scopes'] == [module.SCOPES['calendar']]
        assert kw['autogenerate_code_verifier'] is True
        return flow
    monkeypatch.setattr(module.Flow, 'from_client_config', create)
    module.auth_url(tmp_path, 'calendar')
    pending = json.loads((tmp_path / 'google_oauth_pending.json').read_text())
    assert pending['code_verifier'] == 'pkce-proof'
    assert pending['scopes'] == [module.SCOPES['calendar']]
    assert (tmp_path / 'google_oauth_pending.json').stat().st_mode & 0o777 == 0o600


def test_exchange_rejects_wrong_state_and_expiry_before_network(tmp_path, monkeypatch):
    pending = {'state': 'right', 'code_verifier': 'proof', 'created_at': module.time.time(),
               'scopes': [module.SCOPES['calendar']], 'redirect_uri': module.REDIRECT}
    path = tmp_path / 'google_oauth_pending.json'
    path.write_text(json.dumps(pending))
    monkeypatch.setattr(module.Flow, 'from_client_secrets_file', lambda *a, **kw: pytest.fail('network flow created'))
    with pytest.raises(ValueError, match='state mismatch'):
        module.exchange(tmp_path, module.REDIRECT + '/?code=code&state=wrong')
    pending['created_at'] = 0
    path.write_text(json.dumps(pending))
    with pytest.raises(ValueError, match='expired'):
        module.exchange(tmp_path, 'code')


def test_exchange_writes_actual_grants_and_consumes_pending(tmp_path, monkeypatch):
    pending = {'state': 'right', 'code_verifier': 'proof', 'created_at': module.time.time(),
               'scopes': list(module.SCOPES.values()), 'redirect_uri': module.REDIRECT}
    path = tmp_path / 'google_oauth_pending.json'
    path.write_text(json.dumps(pending))
    def create(client, **kw):
        assert kw['code_verifier'] == 'proof' and kw['state'] == 'right'
        def fetch(**kwargs):
            assert kwargs == {'code': 'code'}
        creds = SimpleNamespace(refresh_token='offline', granted_scopes=[module.SCOPES['calendar']],
                                to_json=lambda: '{"refresh_token":"offline"}')
        return SimpleNamespace(fetch_token=fetch, credentials=creds)
    monkeypatch.setattr(module.Flow, 'from_client_secrets_file', create)
    module.exchange(tmp_path, module.REDIRECT + '/?' + urlencode({'code': 'code', 'state': 'right'}))
    token_path = tmp_path / 'google_token.json'
    assert json.loads(token_path.read_text())['scopes'] == [module.SCOPES['calendar']]
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert not path.exists()


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
