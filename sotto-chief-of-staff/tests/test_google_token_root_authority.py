"""Offline root-authority fixtures for the Sotto Google modules."""
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules():
    reader = load('root_authority_gmail_read', 'sotto-chief-of-staff/_shared/lib/gmail_read.py')
    writer = load('root_authority_google_setup', 'adapters/sotto/google_setup.py')
    runtime = load('root_authority_runtime_api', 'adapters/sotto/runtime_api.py')
    return reader, writer, runtime


def payload():
    return {'type': 'authorized_user', 'token_uri': 'https://oauth2.googleapis.com/token',
            'client_id': 'fixture-client', 'client_secret': 'fixture-secret',
            'refresh_token': 'fixture-refresh', 'token': 'fixture-token',
            'scopes': ['https://www.googleapis.com/auth/gmail.readonly']}


def test_explicit_root_missing_token_cannot_revive_stale_fallback(tmp_path, monkeypatch, modules):
    reader, writer, runtime = modules
    selected = tmp_path / 'selected'
    selected.mkdir()
    stale = tmp_path / 'home' / '.hermes'
    stale.mkdir(parents=True)
    (stale / 'google_token.json').write_text('stale grant')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('SOTTO_HOME', str(selected))
    assert writer.home() == selected
    assert runtime.home_path('google_token.json') == str(selected / 'google_token.json')
    assert reader.token_path() == ''
    with pytest.raises(RuntimeError, match="isn't connected"):
        reader.google_service('gmail', 'v1')  # fails before importing/building a provider client


def test_disconnect_and_reconnect_stay_on_selected_root(tmp_path, monkeypatch, modules):
    reader, writer, _runtime = modules
    selected = tmp_path / 'selected'
    selected.mkdir()
    stale = tmp_path / 'home' / '.hermes'
    stale.mkdir(parents=True)
    (stale / 'google_token.json').write_text('stale grant')
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('SOTTO_HOME', str(selected))
    assert writer.install_cloud_google(payload()) == payload()['scopes']
    assert json.loads((selected / 'google_token.json').read_text())['token'] == 'fixture-token'
    assert reader.token_path() == str(selected / 'google_token.json')
    assert writer.install_cloud_google(None) == []
    assert reader.token_path() == ''
    assert (stale / 'google_token.json').exists()
    writer.install_cloud_google(payload())
    assert reader.token_path() == str(selected / 'google_token.json')


def test_unset_or_empty_root_never_reads_a_legacy_home_token(tmp_path, monkeypatch, modules):
    reader, writer, runtime = modules
    legacy = tmp_path / '.hermes'
    legacy.mkdir()
    token = legacy / 'google_token.json'
    token.write_text('legacy grant')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('SOTTO_HOME', raising=False)
    assert reader.token_path() == ''
    with monkeypatch.context() as patch:
        patch.setattr(reader.os.path, 'isfile', lambda path: path == '/root/.hermes/google_token.json')
        assert reader.token_path() == ''
    with pytest.raises(ValueError, match='SOTTO_HOME'):
        writer.home()
    with pytest.raises(ValueError, match='SOTTO_HOME'):
        runtime.home_path('google_token.json')
    monkeypatch.setenv('SOTTO_HOME', '')
    assert reader.token_path() == ''
    with pytest.raises(ValueError, match='SOTTO_HOME'):
        writer.home()
    with pytest.raises(ValueError, match='SOTTO_HOME'):
        runtime.home_path('google_token.json')
    assert token.read_text() == 'legacy grant'


@pytest.mark.parametrize('selected', [None, '', 'relative', '/tmp/../other', '/tmp/./other'])
def test_shared_reader_and_writer_refuse_ambient_grants(
        tmp_path, monkeypatch, modules, selected):
    reader, writer, runtime = modules
    legacy = tmp_path / '.hermes'
    legacy.mkdir()
    token = legacy / 'google_token.json'
    token.write_text('ambient grant must remain untouched')
    monkeypatch.setenv('HOME', str(tmp_path))
    if selected is None:
        monkeypatch.delenv('SOTTO_HOME', raising=False)
    else:
        monkeypatch.setenv('SOTTO_HOME', selected)
    assert reader.token_path() == ''
    with pytest.raises(RuntimeError, match="isn't connected"):
        reader.google_service('gmail', 'v1')
    with pytest.raises(ValueError, match='SOTTO_HOME'):
        writer.install_cloud_google(None)
    with pytest.raises(ValueError, match='SOTTO_HOME'):
        runtime.home_path('google_token.json')
    assert token.read_text() == 'ambient grant must remain untouched'
