"""Offline self-host credential generations are source context, not grant writers."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import receiver as rec
import selfhost_source_authority as authority


class Relay:
    def __init__(self, during_health=None):
        self.during_health = during_health

    def bridge_connected(self):
        return True

    def mcp_call(self, request, timeout):
        assert timeout == 2 and request['params']['name'] == 'health'
        if self.during_health:
            self.during_health()
        return {'result': {'structuredContent': {'connected': True, 'source_policy': {
            'version': 1, 'bridge_revision': 'bridge-unchanged', 'cloud_revision': None,
            'managed': False, 'allowed_sources': ['imessage']}}, 'isError': False}}


def _credential(path, generation, *, google=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    value = ({'refresh_token': 'fixture-refresh', 'client_id': 'fixture-client',
              'client_secret': 'fixture-secret', 'token_uri': 'https://oauth2.googleapis.com/token',
              'scopes': ['https://www.googleapis.com/auth/gmail.readonly']} if google else
             {'service': 'granola', 'access_token': 'fixture-access'})
    if generation is not None:
        value[authority.GENERATION_KEY] = generation
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def _setup(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    google = tmp_path / 'sotto-home/google_token.json'
    granola = root / 'connectors/granola.json'
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'selfhost')
    monkeypatch.setenv('SOTTO_DATA', str(root))
    monkeypatch.setattr(rec, 'DATA', str(root))
    monkeypatch.setattr(rec, 'RELAY', Relay())
    monkeypatch.setenv('SOTTO_HOME', str(google.parent))
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(home_path=lambda name: str(google)))
    return root, google, granola


def test_selfhost_google_only_requires_an_explicit_token_root(tmp_path, monkeypatch):
    _root, google, _granola = _setup(tmp_path, monkeypatch)
    monkeypatch.delenv('SOTTO_HOME')
    monkeypatch.setattr(rec, 'RELAY', SimpleNamespace(bridge_connected=lambda: False))
    _credential(google, 'a' * 32, google=True)
    google.write_text(json.dumps({'refresh_token': 'fixture-refresh', 'client_id': 'synthetic-client',
        'client_secret': 'synthetic-secret', 'token_uri': 'https://oauth2.googleapis.com/token',
        'scopes': ['https://www.googleapis.com/auth/gmail.readonly'],
        authority.GENERATION_KEY: 'a' * 32}))
    google.chmod(0o600)
    assert rec._runtime_source_authority()['google_sources'] == []
    monkeypatch.setenv('SOTTO_HOME', str(google.parent))
    granted = rec._runtime_source_authority()
    assert granted['complete'] and granted['google_sources'] == ['gmail']
    monkeypatch.delenv('SOTTO_HOME')
    assert rec._runtime_source_authority()['google_sources'] == []
    assert rec._runtime_source_authority()['revision'] != granted['revision']


def test_selfhost_off_on_regrant_and_credential_replacement_change_durable_revision(tmp_path, monkeypatch):
    root, google, granola = _setup(tmp_path, monkeypatch)
    empty = rec._runtime_source_authority()
    assert empty['complete'] is True
    _credential(google, 'a' * 32, google=True)
    first = rec._runtime_source_authority()
    assert first['complete'] and first['revision'] != empty['revision']
    assert rec._runtime_source_authority() == first  # no-op health/credential probe
    # No authority poll occurs while the owner disconnects and reconnects.
    # The next probe must still detect that complete off/on cycle.
    google.unlink()
    _credential(google, 'b' * 32, google=True)
    regranted = rec._runtime_source_authority()
    assert regranted['revision'] != first['revision']
    google.unlink()
    denied = rec._runtime_source_authority()
    assert denied['revision'] != regranted['revision']
    _credential(google, 'e' * 32, google=True)
    _credential(granola, 'c' * 32)
    linked = rec._runtime_source_authority()
    assert linked['revision'] != regranted['revision']
    _credential(granola, 'd' * 32)
    replaced = rec._runtime_source_authority()
    assert replaced['revision'] != linked['revision']
    granola.unlink()
    assert rec._runtime_source_authority()['revision'] != replaced['revision']
    # A fresh receiver process reads the same persisted generations, not an
    # in-memory epoch. Re-importing the module stands in for process restart.
    spec = importlib.util.spec_from_file_location('receiver_selfhost_restart', Path(rec.__file__))
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    monkeypatch.setattr(restarted, 'DATA', str(root))
    monkeypatch.setattr(restarted, 'RELAY', Relay())
    monkeypatch.setattr(restarted, '_runtime_adapter', lambda: SimpleNamespace(home_path=lambda name: str(google)))
    assert restarted._runtime_source_authority() == rec._runtime_source_authority()


def test_legacy_malformed_or_replaced_during_health_is_unknown(tmp_path, monkeypatch):
    _root, google, granola = _setup(tmp_path, monkeypatch)
    _credential(google, None, google=True)
    first = rec._runtime_source_authority()
    assert first['complete'] is False and first['revision'] is None
    assert first['bridge_sources'] == []
    _credential(google, 'a' * 32, google=True)
    _credential(granola, 'b' * 32)
    assert rec._runtime_source_authority()['complete'] is True
    granola.write_text('{"service":"granola","service":"granola"}')
    assert rec._runtime_source_authority()['bridge_sources'] == []
    _credential(granola, 'b' * 32)
    # A token transition during the live Bridge probe cannot stamp either
    # old or new source material with a complete current revision.
    monkeypatch.setattr(rec, 'RELAY', Relay(lambda: _credential(google, 'c' * 32, google=True)))
    assert rec._runtime_source_authority()['bridge_sources'] == []
    monkeypatch.setattr(rec, 'RELAY', Relay())
    assert rec._runtime_source_authority()['complete'] is True
    google.unlink()
    google.symlink_to(granola)
    assert rec._runtime_source_authority()['bridge_sources'] == []


def test_stale_granola_refresh_cannot_resurrect_disconnect_or_replace_reconnect(tmp_path, monkeypatch):
    root, _google, granola = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(rec.CONNECTORS, 'DATA', str(root))
    source = Path(rec.__file__).resolve().parents[2] / 'sotto-chief-of-staff/_shared/lib/connector_tokens.py'
    spec = importlib.util.spec_from_file_location('selfhost_connector_tokens', source)
    tokens = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tokens)
    old = {'service': 'granola', 'access_token': 'old-access', 'refresh_token': 'old-refresh',
           'expires_at': 1, 'token_endpoint': 'https://fixture.example/token', 'client_id': 'client',
           'resource': 'https://fixture.example/mcp', 'mcp_url': 'https://fixture.example/mcp',
           'obtained_at': 1, authority.GENERATION_KEY: 'a' * 32}
    granola.parent.mkdir(parents=True)
    granola.write_text(json.dumps(old))
    granola.chmod(0o600)
    response = (200, b'{"access_token":"refreshed","expires_in":3600}')

    def disconnect_during_provider_call(*_args):
        rec.CONNECTORS.disconnect('granola')
        return response

    with pytest.raises(tokens.ConnectorMissing):
        tokens._refresh('granola', old, disconnect_during_provider_call, 100)
    assert not granola.exists(), 'a stale refresh must not restore a revoked token'

    granola.write_text(json.dumps(old))
    granola.chmod(0o600)
    replacement = {**old, 'access_token': 'new-grant', authority.GENERATION_KEY: 'b' * 32}

    def reconnect_during_provider_call(*_args):
        with rec.CONNECTORS.file_lock(str(granola)):
            rec.CONNECTORS.write_json(str(granola), replacement)
        return response

    result = tokens._refresh('granola', old, reconnect_during_provider_call, 100)
    assert result == replacement
    assert json.loads(granola.read_text()) == replacement


def test_granola_only_no_google_home_grant_refresh_reconnect_and_refusal(tmp_path,monkeypatch):
    root,_google,granola=_setup(tmp_path,monkeypatch)
    monkeypatch.delenv('SOTTO_HOME',raising=False)
    absent=rec._runtime_source_authority()
    assert absent['connector_sources']==[]
    _credential(granola,'a'*32)
    granted=rec._runtime_source_authority()
    assert granted['complete'] and granted['connector_sources']==['granola']
    row=json.loads(granola.read_text());row['access_token']='refreshed-fixture';granola.write_text(json.dumps(row))
    assert rec._runtime_source_authority()==granted
    _credential(granola,'b'*32)
    assert rec._runtime_source_authority()['revision']!=granted['revision']
    for generation in (None,'invalid'):
        _credential(granola,generation)
        assert rec._runtime_source_authority()['connector_sources']==[]
    _credential(granola,'a'*32);granola.chmod(0o644)
    assert rec._runtime_source_authority()['connector_sources']==[]
    granola.unlink()
    assert rec._runtime_source_authority()['connector_sources']==[]
    assert not (root/'knowledge').exists()


def test_calendar_selection_persists_and_changes_revision_without_widening_writes(tmp_path, monkeypatch):
    root, google, _granola = _setup(tmp_path, monkeypatch)
    _credential(google, 'a'*32, google=True)
    token = {'refresh_token':'fixture', 'client_id':'fixture', 'client_secret':'fixture',
        'token_uri':'https://oauth2.googleapis.com/token',
        'scopes':['https://www.googleapis.com/auth/calendar.readonly', 'https://www.googleapis.com/auth/gmail.readonly'], 'sotto_source_generation':'a'*32}
    google.write_text(json.dumps(token))
    monkeypatch.setattr(rec, 'SETTINGS_FILE', str(root/'config/settings.json'))
    before = rec._runtime_source_authority()
    assert before['google_sources'] == ['calendar', 'gmail']
    assert rec._google_calendar_settings()['mode'] == 'all'
    choice = {'mode':'selected','calendar_ids':['primary','team@group']}
    assert rec._google_calendar_settings(choice)['calendar_ids'] == choice['calendar_ids']
    after = rec._runtime_source_authority()
    assert after['complete'] and after['revision'] != before['revision']
    assert json.loads(google.read_text()) == token
    assert rec._google_calendar_settings()['calendar_ids'] == choice['calendar_ids']
    # A settings edit during a source read is fenced; a regrant cannot revive an old selection.
    monkeypatch.setattr(rec, 'RELAY', Relay(lambda: rec.write_setting('google_calendar_selection', {'mode':'selected','calendar_ids':[]})))
    assert not rec._runtime_source_authority()['complete']
    monkeypatch.setattr(rec, 'RELAY', Relay())
    rec._google_calendar_settings(choice)
    google.write_text(json.dumps({**token,'sotto_source_generation':'b'*32}))
    reconnected = rec._runtime_source_authority()
    assert reconnected['complete'] is True
    assert reconnected['google_sources'] == ['gmail']
    assert reconnected['bridge_sources'] == ['imessage']
    assert rec._google_calendar_settings()['needs_reselection']
    assert rec._google_calendar_settings({'mode':'all','calendar_ids':[]})['mode'] == 'all'
    assert rec._runtime_source_authority()['complete']
