"""Selected-Sotto Google status/setup must not discover credentials under HOME."""
import importlib.util
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest


HERE = Path(__file__).parent


def receiver():
    spec = importlib.util.spec_from_file_location('receiver_credential_home', HERE / 'receiver.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_explicit_home_withholds_ambient_google_status_setup_and_first_use(
        tmp_path, monkeypatch):
    rec = receiver()
    ambient = tmp_path / 'ambient'
    (ambient / '.hermes').mkdir(parents=True)
    for name in ('google_client_secret.json', 'google_token.json'):
        (ambient / '.hermes' / name).write_text('{}')
    monkeypatch.setenv('HOME', str(ambient))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    monkeypatch.delenv('SOTTO_HOME', raising=False)
    monkeypatch.setattr(rec, 'DATA', str(tmp_path / 'data'))
    monkeypatch.setattr(rec, '_google_setup_py', lambda: '/fixture/google_setup.py')
    monkeypatch.setattr(rec.subprocess, 'run', lambda *_args, **_kwargs:
                        pytest.fail('credential-less Sotto contacted Google setup'))
    monkeypatch.setattr(rec, '_local_source_ready', lambda: False)
    monkeypatch.setattr(rec, 'RELAY', SimpleNamespace(bridge_connected=lambda: False))
    monkeypatch.setattr(rec.CONNECTORS, 'service_status', lambda: [])
    monkeypatch.setattr(rec, '_validate_google_web_client', lambda _obj: 'https://fixture.invalid/callback')
    monkeypatch.setattr(rec.CONNECTORS, 'write_json', lambda *_args, **_kwargs:
                        pytest.fail('credential-less Sotto wrote a Google client'))

    assert rec._context_sources_ready() is False
    authority = rec._runtime_source_authority()
    assert authority['google_sources'] == authority['bridge_sources'] == []
    rec._GOOGLE_CHECK_CACHE = (time.monotonic(), (True, 'previously connected'))
    assert rec.google_connected()[0] is False
    assert rec._google_connected_uncached() == (
        False, 'Google credential home is unavailable. Configure SOTTO_HOME before connecting.')
    assert rec.exchange_google_code('synthetic-code')[0] is False
    assert rec.setup_google_client(json.dumps({'web': {}}))[0] is False
    with pytest.raises(ValueError, match='credential home is unavailable'):
        rec.start_google_authorization('synthetic-browser-binding')

    monkeypatch.setattr(rec, 'google_connected', lambda: (False, 'unavailable'))
    monkeypatch.setattr(rec, '_first_brief_pending', lambda: False)
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'photon')
    monkeypatch.setattr(rec, '_channel_status', lambda: 'unavailable')
    monkeypatch.setattr(rec, '_last_event_at', lambda: None)
    monkeypatch.setattr(rec, 'update_status', lambda: {})
    assert rec.setup_status()['google_client_present'] is False


def test_explicit_home_is_used_for_google_status_without_home_fallback(tmp_path, monkeypatch):
    rec = receiver()
    ambient = tmp_path / 'ambient'
    (ambient / '.hermes').mkdir(parents=True)
    (ambient / '.hermes' / 'google_client_secret.json').write_text('{}')
    selected = tmp_path / 'data' / 'sotto-home'
    selected.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(ambient))
    monkeypatch.setenv('SOTTO_HOME', str(selected))
    monkeypatch.setattr(rec, '_google_setup_py', lambda: '/fixture/google_setup.py')
    assert rec._google_connected_uncached()[0] is False  # ambient client ignored
    (selected / 'google_client_secret.json').write_text('{}')
    calls = []
    monkeypatch.setattr(rec.subprocess, 'run', lambda *_args, **_kwargs:
                        calls.append(1) or SimpleNamespace(returncode=0))
    assert rec._google_connected_uncached() == (True, 'connected ✓')
    assert calls == [1]


def test_real_cloud_bootstrap_installer_never_uses_ambient_home(tmp_path, monkeypatch):
    import cloud_pairing

    rec = receiver()
    ambient = tmp_path / 'ambient'
    legacy = ambient / '.hermes'
    legacy.mkdir(parents=True)
    marker = legacy / 'google_token.json'
    marker.write_text('old ambient credential')
    monkeypatch.setenv('HOME', str(ambient))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant')
    monkeypatch.delenv('SOTTO_HOME', raising=False)
    adapter = rec._cloud_google_adapter()
    assert adapter is not None
    pairing = cloud_pairing.Pairing(tmp_path / 'receiver', adapter)
    body = {'entry': 'browser', 'tenant_id': 'tenant', 'request_id': 'd' * 64,
            'sub': 'google-sub', 'credentials': None,
            'timezone': 'America/Los_Angeles'}
    with pytest.raises(ValueError, match='explicit SOTTO_HOME'):
        pairing.bootstrap(body)
    assert marker.read_text() == 'old ambient credential'
    assert sorted(path.name for path in legacy.iterdir()) == ['google_token.json']
    assert not (tmp_path / 'receiver' / 'config' / 'cloud-policy.json').exists()


@pytest.mark.parametrize('selected', ['relative', '/tmp/../other', '', '/tmp/./other'])
def test_shared_google_writer_rejects_invalid_sotto_home(tmp_path, monkeypatch, selected):
    rec = receiver()
    monkeypatch.setenv('SOTTO_HOME', selected)
    adapter = rec._cloud_google_adapter()
    with pytest.raises(ValueError, match='explicit SOTTO_HOME'):
        adapter.install_cloud_google(None)
