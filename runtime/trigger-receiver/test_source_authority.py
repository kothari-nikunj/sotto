"""Offline source-authority contract and grant-transition tests."""
import io
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import managed
import receiver as rec


def _health(allowed=('imessage',), *, bridge='bridge-1', cloud='cloud-1', managed_mode=True):
    return {'connected': True, 'source_policy': {
        'version': 1, 'bridge_revision': bridge, 'cloud_revision': cloud if managed_mode else None,
        'managed': managed_mode, 'allowed_sources': list(allowed)}}


class Relay:
    def __init__(self, health):
        self.health = health
        self.calls = []

    def bridge_connected(self):
        return self.health is not None

    def mcp_call(self, request, timeout):
        self.calls.append((request, timeout))
        return {'result': {'structuredContent': self.health, 'isError': False}}


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant-a')
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))


def test_managed_revision_tracks_transitions_and_noops_across_all_writers(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    first = managed.source_authority_state(tmp_path)['revision']
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    assert managed.source_authority_state(tmp_path)['revision'] == first
    managed.record_google_consent(tmp_path, ['https://www.googleapis.com/auth/gmail.readonly'])
    second = managed.source_authority_state(tmp_path)['revision']
    assert second != first
    managed.record_google_consent(tmp_path, ['https://www.googleapis.com/auth/gmail.readonly'])
    assert managed.source_authority_state(tmp_path)['revision'] == second
    managed.record_connector_consent(tmp_path, 'granola', True)
    third = managed.source_authority_state(tmp_path)['revision']
    assert third != second
    managed.reconcile_connector_capabilities(tmp_path, [{'service': 'granola', 'connected': True}])
    assert managed.source_authority_state(tmp_path)['revision'] == third
    managed.record_bridge_consent(tmp_path, [], [])
    assert managed.source_authority_state(tmp_path)['revision'] != third


def test_missing_corrupt_recreated_and_foreign_ledger_revision_never_passes(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    path = Path(tmp_path) / 'config/managed-capabilities.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'tenant_id': 'tenant-a', 'sources': {'imessage': {
        'consented': True, 'connected': True}}}))
    assert managed.source_authority_state(tmp_path) is None
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    first = managed.source_authority_state(tmp_path)['revision']
    state = json.loads(path.read_text())
    state['authority_revision'] = 'corrupt'
    path.write_text(json.dumps(state))
    assert managed.source_authority_state(tmp_path) is None
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    assert managed.source_authority_state(tmp_path)['revision'] != first
    repaired = managed.source_authority_state(tmp_path)['revision']
    state = json.loads(path.read_text())
    state['sources']['imessage']['consented'] = False
    path.write_text(json.dumps(state))
    assert managed.source_authority_state(tmp_path) is None
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    assert managed.source_authority_state(tmp_path)['revision'] != repaired
    state = json.loads(path.read_text())
    state['tenant_id'] = 'other'
    path.write_text(json.dumps(state))
    assert managed.source_authority_state(tmp_path) is None
    path.write_text('{broken')
    assert managed.source_authority_state(tmp_path) is None
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    assert managed.source_authority_state(tmp_path)['revision'] != first
    raw = path.read_text()
    path.write_text(raw.replace('"tenant_id":', '"tenant_id": "other", "tenant_id":', 1))
    assert managed.source_authority_state(tmp_path) is None


def test_live_policy_intersects_managed_grants_and_binds_both_revisions(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv('SOTTO_HOME', raising=False)
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    relay = Relay(_health(('contacts', 'imessage')))
    monkeypatch.setattr(rec, 'RELAY', relay)
    first = rec._runtime_source_authority()
    assert first['complete'] is True and first['bridge_sources'] == ['imessage']
    assert relay.calls[0][0]['params'] == {'name': 'health', 'arguments': {}}
    assert relay.calls[0][1] == 2.0
    assert rec._runtime_source_authority() == first
    relay.health = _health(('contacts', 'imessage'), bridge='bridge-2')
    assert rec._runtime_source_authority()['revision'] != first['revision']
    relay.health = _health(('contacts', 'imessage'), cloud='cloud-2')
    assert rec._runtime_source_authority()['revision'] != first['revision']
    managed.record_bridge_consent(tmp_path, [], [])
    changed = rec._runtime_source_authority()
    assert changed['complete'] is True and changed['bridge_sources'] == []
    assert changed['revision'] != first['revision']


def test_unknown_offline_foreign_or_malformed_bridge_policy_fails_closed(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    relay = Relay(None)
    monkeypatch.setattr(rec, 'RELAY', relay)
    offline = rec._runtime_source_authority()
    assert offline['version'] == 2 and offline['complete'] is True
    assert offline['bridge_sources'] == offline['google_sources'] == []
    for health in (_health(bridge=None), _health(cloud=None), _health(managed_mode=False),
                   _health(('unknown',)), _health(('imessage', 'imessage')),
                   {'connected': True, 'sources': {'imessage': 'ok'}}):
        relay.health = health
        assert rec._runtime_source_authority() == offline
    monkeypatch.setenv('SOTTO_TENANT_ID', 'foreign')
    relay.health = _health()
    assert rec._runtime_source_authority()['complete'] is False


def test_managed_google_only_source_survives_absent_bridge_but_not_token_replacement(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    home = tmp_path / 'sotto-home'
    home.mkdir()
    monkeypatch.setenv('SOTTO_HOME', str(home))
    token = home / 'google_token.json'
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(home_path=lambda name: str(token)))
    monkeypatch.setattr(rec, 'RELAY', Relay(None))
    managed.record_google_consent(tmp_path, ['https://www.googleapis.com/auth/gmail.readonly'])
    disconnected = rec._runtime_source_authority()
    assert disconnected['complete'] and disconnected['google_sources'] == disconnected['bridge_sources'] == []
    request_id = 'd' * 64
    generation = hashlib.sha256(('sotto-cloud-google-v1\0tenant-a\0' + request_id + '\0' + '4').encode()).hexdigest()[:32]
    token.write_text(json.dumps({'refresh_token': 'synthetic', 'client_id': 'synthetic-client',
        'client_secret': 'synthetic-secret', 'token_uri': 'https://oauth2.googleapis.com/token',
        'scopes': ['https://www.googleapis.com/auth/gmail.readonly'],
        'sotto_source_generation': generation, 'sotto_cloud_request_id': request_id}))
    token.chmod(0o600)
    database = tmp_path / 'config/cloud-pairing.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE bootstraps (id TEXT PRIMARY KEY, generation INTEGER, applied INTEGER)')
        db.execute('INSERT INTO bootstraps VALUES (?,?,?)', (request_id, 4, 0))
    database.chmod(0o600)
    assert rec._runtime_source_authority()['google_sources'] == []
    with sqlite3.connect(database) as db:
        db.execute('UPDATE bootstraps SET applied=1')
    authorized = rec._runtime_source_authority()
    assert authorized['google_sources'] == ['gmail'] and authorized['bridge_sources'] == []
    assert authorized['revision'] != disconnected['revision']
    def expired(_request, timeout):
        assert timeout == 2.0
        raise TimeoutError('synthetic Mac probe deadline')
    monkeypatch.setattr(rec, 'RELAY', SimpleNamespace(bridge_connected=lambda: True, mcp_call=expired))
    assert rec._runtime_source_authority() == authorized
    monkeypatch.delenv('SOTTO_HOME')
    assert rec._runtime_source_authority()['google_sources'] == []
    assert rec._runtime_source_authority()['revision'] != authorized['revision']
    monkeypatch.setenv('SOTTO_HOME', str(home))
    with sqlite3.connect(database) as db:
        db.execute('INSERT INTO bootstraps VALUES (?,?,?)', ('e' * 64, 5, 1))
    assert rec._runtime_source_authority()['google_sources'] == [], 'restored old applied token cannot revive its grant'
    token.unlink()
    assert rec._runtime_source_authority()['google_sources'] == []
    assert rec._runtime_source_authority()['revision'] == disconnected['revision']


def test_private_route_requires_loopback_read_bearer_and_exact_empty_body(monkeypatch):
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {
        'version': 2, 'revision': 'r', 'complete': True, 'bridge_sources': [], 'google_sources': []})

    def request(peer, authorized, body):
        sent = []
        fake = SimpleNamespace(path='/runtime/source-authority', client_address=(peer, 1234),
                               headers={'Content-Length': str(len(body))}, rfile=io.BytesIO(body),
                               _authed=lambda token: authorized and token == rec.MCP_TOKEN,
                               _send=lambda status, payload: sent.append((status, payload)))
        rec.Handler.do_POST(fake)
        return sent[0]

    assert request('203.0.113.1', True, b'{}')[0] == 401
    assert request('127.0.0.1', False, b'{}')[0] == 401
    assert request('127.0.0.1', True, b'{"extra":1}')[0] == 400
    assert request('127.0.0.1', True, b'{}')[0] == 200


def test_google_read_route_needs_current_source_revision_before_and_after_empty_success(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_HOME', str(tmp_path / 'sotto-home'))
    reader = tmp_path / 'google_reader.py'
    reader.write_text("def read_owner(operation, arguments):\n"
                      "    return {'version': 1, 'source': 'gmail', 'operation': operation, "
                      "'items': [], 'observation': {'status': 'ok', 'complete': True, "
                      "'observed_at': '2026-09-30T00:00:00Z', "
                      "'coverage': {'since': None, 'until': None}}}\n")
    monkeypatch.setattr(rec, '_google_api_py', lambda: str(reader))
    current = {'version': 2, 'revision': 'a' * 64, 'complete': True,
               'bridge_sources': [], 'google_sources': ['gmail']}
    calls = 0
    def authority():
        nonlocal calls
        calls += 1
        return current
    monkeypatch.setattr(rec, '_runtime_source_authority', authority)
    body = json.dumps({'operation': 'gmail_search', 'arguments': {'query': 'in:inbox', 'max': 4},
                       'source_revision': 'a' * 64}).encode()
    def request(peer='127.0.0.1', authorized=True, payload=body):
        sent = []
        fake = SimpleNamespace(path='/runtime/google-read', client_address=(peer, 1234),
                               headers={'Content-Length': str(len(payload))}, rfile=io.BytesIO(payload),
                               _authed=lambda token: authorized and token == rec.MCP_TOKEN,
                               _send=lambda status, result: sent.append((status, result)))
        rec.Handler.do_POST(fake)
        return sent[0]
    assert request('203.0.113.1')[0] == 401
    assert request(authorized=False)[0] == 401
    assert request(payload=b'{"operation":"gmail_search","operation":"gmail_get"}')[0] == 400
    assert request()[0] == 200 and calls == 2
    current = {**current, 'revision': 'b' * 64}
    assert request()[0] == 503


def _granola_credential(root, generation='a'*32, **changes):
    path = root / 'connectors/granola.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({'service':'granola','access_token':'synthetic-access',
                               'sotto_source_generation':generation, **changes}))
    path.chmod(0o600)
    return path


def test_capabilities_reports_effective_original_grants_without_provider_access(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_HOME', str(tmp_path / 'sotto-home'))
    current = {'version': 2, 'revision': 'a' * 64, 'complete': True,
               'bridge_sources': [], 'google_sources': ['google_contacts']}
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: dict(current))
    calls = []
    def capabilities():
        calls.append('saved-grants')
        return {'status': 'ok', 'connected': True, 'contacts_read': True, 'contacts_write': False,
                'gmail_read': True, 'gmail_draft': True, 'gmail_send': True, 'gmail_write': True,
                'calendar_read': True, 'calendar_write': True}
    monkeypatch.setattr(rec.RUNTIME_OWNER_GMAIL_DRAFT_TOOL, '_google',
                        lambda: SimpleNamespace(capabilities=capabilities))
    def forbidden():
        raise AssertionError('Capabilities must not load a provider reader')
    monkeypatch.setattr(rec, '_google_api_py', forbidden)
    def request(*, authorized=True, peer='127.0.0.1', arguments=None):
        body = json.dumps({'operation': 'capabilities', 'arguments': arguments or {},
                           'source_revision': 'a' * 64}).encode()
        sent = []
        fake = SimpleNamespace(path='/runtime/google-read', client_address=(peer, 1234),
                               headers={'Content-Length': str(len(body))}, rfile=io.BytesIO(body),
                               _authed=lambda token: authorized and token == rec.MCP_TOKEN,
                               _send=lambda status, result: sent.append((status, result)))
        rec.Handler.do_POST(fake)
        return sent[0]
    assert request(authorized=False)[0] == 401
    assert request(peer='203.0.113.1')[0] == 401
    assert request(arguments={'verb': 'contacts-delete'})[0] == 400
    assert calls == []
    status, result = request()
    assert status == 200 and calls == ['saved-grants']
    assert result['source'] == 'google' and result['operation'] == 'capabilities'
    assert result['item'] == {'status': 'ok', 'connected': True, 'contacts_read': True,
                             'contacts_write': False, 'gmail_read': False, 'gmail_draft': False,
                             'gmail_send': False, 'gmail_write': False,
                             'calendar_read': False, 'calendar_write': False}
    current['google_sources'] = []
    assert request()[1]['item']['connected'] is False
    assert request()[1]['item']['contacts_read'] is False
    snapshots = iter([dict(current), {**current, 'revision': 'b' * 64}])
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: next(snapshots))
    assert request()[0] == 503


def test_managed_granola_tenant_consent_and_generation_independent_of_google(tmp_path, monkeypatch):
    _setup(tmp_path,monkeypatch)
    monkeypatch.delenv('SOTTO_HOME',raising=False)
    monkeypatch.setattr(rec,'RELAY',Relay(None))
    managed.record_connector_consent(tmp_path,'granola',True)
    absent=rec._runtime_source_authority()
    assert absent['connector_sources']==[]
    token=_granola_credential(tmp_path)
    granted=rec._runtime_source_authority()
    assert granted['complete'] and granted['connector_sources']==['granola']
    assert granted['bridge_sources']==granted['google_sources']==[]
    policy_revision=managed.source_authority_state(tmp_path)['revision']
    _granola_credential(tmp_path,access_token='refreshed-synthetic-access')
    assert rec._runtime_source_authority()==granted
    _granola_credential(tmp_path,'b'*32)
    replaced=rec._runtime_source_authority()
    assert replaced['revision']!=granted['revision'] and replaced['connector_sources']==['granola']
    assert managed.source_authority_state(tmp_path)['revision']==policy_revision
    managed.record_connector_consent(tmp_path,'granola',False)
    revoked=rec._runtime_source_authority()
    assert revoked['connector_sources']==[] and revoked['revision']!=replaced['revision']
    monkeypatch.setenv('SOTTO_TENANT_ID','foreign')
    assert not rec._runtime_source_authority()['complete']
    assert token.exists()


def test_bad_granola_does_not_erase_independent_managed_sources(tmp_path,monkeypatch):
    _setup(tmp_path,monkeypatch)
    monkeypatch.delenv('SOTTO_HOME',raising=False)
    managed.record_bridge_consent(tmp_path,['imessage'],['imessage'])
    managed.record_google_consent(tmp_path,['https://www.googleapis.com/auth/gmail.readonly'])
    managed.record_connector_consent(tmp_path,'granola',True)
    monkeypatch.setattr(rec,'RELAY',Relay(_health()))
    import google_source_authority
    monkeypatch.setattr(google_source_authority,'state',lambda *a,**k:{'generation':'google-fixture','sources':['gmail']})
    token=_granola_credential(tmp_path)
    for content,mode in [('{broken',0o600), (json.dumps({'service':'granola','access_token':'legacy'}),0o600),
                         (token.read_text(),0o644)]:
        token.write_text(content);token.chmod(mode)
        result=rec._runtime_source_authority()
        assert result['complete'] and result['connector_sources']==[]
        assert result['bridge_sources']==['imessage'] and result['google_sources']==[]
    # Explicit selected Google source remains independently permitted with malformed Granola.
    monkeypatch.setenv('SOTTO_HOME',str(tmp_path/'sotto-home'))
    monkeypatch.setattr(rec,'_runtime_adapter',lambda:SimpleNamespace(home_path=lambda _:str(tmp_path/'google.json')))
    assert rec._runtime_source_authority()['google_sources']==['gmail']


def test_granola_change_during_final_google_probe_invalidates_capture(tmp_path,monkeypatch):
    _setup(tmp_path,monkeypatch)
    managed.record_connector_consent(tmp_path,'granola',True)
    _granola_credential(tmp_path)
    monkeypatch.setattr(rec,'RELAY',Relay(None))
    monkeypatch.setenv('SOTTO_HOME',str(tmp_path/'sotto-home'))
    monkeypatch.setattr(rec,'_runtime_adapter',lambda:SimpleNamespace(home_path=lambda _:str(tmp_path/'google.json')))
    import google_source_authority
    calls=[0]
    def probe(*args,**kwargs):
        calls[0]+=1
        if calls[0]==2:
            _granola_credential(tmp_path,'b'*32)
        return {'generation':'google-fixture','sources':[]}
    monkeypatch.setattr(google_source_authority,'state',probe)
    assert not rec._runtime_source_authority()['complete']


def _google_calendar(tmp_path, monkeypatch):
    home = tmp_path / 'sotto-home'
    home.mkdir()
    monkeypatch.setenv('SOTTO_HOME', str(home))
    token = home / 'google_token.json'
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(home_path=lambda name: str(token)))
    managed.record_google_consent(tmp_path, ['https://www.googleapis.com/auth/calendar'])
    request_id = 'd' * 64
    generation = hashlib.sha256(('sotto-cloud-google-v1\0tenant-a\0' + request_id + '\0' + '4').encode()).hexdigest()[:32]
    token.write_text(json.dumps({'refresh_token': 's', 'client_id': 'c', 'client_secret': 'x',
        'token_uri': 'https://oauth2.googleapis.com/token', 'scopes': ['https://www.googleapis.com/auth/calendar'],
        'sotto_source_generation': generation, 'sotto_cloud_request_id': request_id}))
    token.chmod(0o600)
    database = tmp_path / 'config/cloud-pairing.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE bootstraps (id TEXT PRIMARY KEY, generation INTEGER, applied INTEGER)')
        db.execute('INSERT INTO bootstraps VALUES (?,?,?)', (request_id, 4, 1))
    database.chmod(0o600)
    return token


def test_sleeping_mac_keeps_google_only_approval_revision_but_not_bridge_availability(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    _google_calendar(tmp_path, monkeypatch)
    relay = Relay(_health(('imessage',)))
    monkeypatch.setattr(rec, 'RELAY', relay)
    preview = rec._runtime_source_authority()          # owner sees "Move this meeting?"
    assert preview['bridge_sources'] == ['imessage'] and preview['google_sources'] == ['calendar']
    monkeypatch.setattr(rec, 'RELAY', Relay(None))       # Mac lid closes; Bridge offline
    asleep = rec._runtime_source_authority()           # owner replies "yes"
    assert asleep['google_sources'] == ['calendar'] and asleep['bridge_sources'] == []
    assert asleep['revision'] == preview['revision']
    def expired(_request, timeout):
        raise TimeoutError('synthetic Mac probe deadline')
    monkeypatch.setattr(rec, 'RELAY', SimpleNamespace(bridge_connected=lambda: True, mcp_call=expired))
    assert rec._runtime_source_authority() == asleep
    monkeypatch.setattr(rec, 'RELAY', relay)             # Mac wakes with unchanged consent
    assert rec._runtime_source_authority() == preview


def test_sleeping_mac_never_hides_a_consent_change(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    token = _google_calendar(tmp_path, monkeypatch)
    relay = Relay(_health(('imessage',)))
    monkeypatch.setattr(rec, 'RELAY', relay)
    preview = rec._runtime_source_authority()
    monkeypatch.setattr(rec, 'RELAY', Relay(None))
    asleep = rec._runtime_source_authority()
    # The Mac changes consent while asleep: it is visible as soon as it reconnects.
    relay.health = _health(('imessage',), bridge='bridge-2')
    monkeypatch.setattr(rec, 'RELAY', relay)
    assert rec._runtime_source_authority()['revision'] != preview['revision']
    relay.health = _health(('imessage',))
    assert rec._runtime_source_authority()['revision'] == preview['revision']
    monkeypatch.setattr(rec, 'RELAY', Relay(None))
    # Revoking a source while the Mac sleeps still invalidates: the tool's own Google source...
    token.unlink()
    revoked = rec._runtime_source_authority()
    assert revoked['google_sources'] == [] and revoked['revision'] != asleep['revision']
    # ...and the owner's managed Bridge consent ledger.
    managed.record_bridge_consent(tmp_path, [], [])
    assert rec._runtime_source_authority()['revision'] != revoked['revision']


def test_google_reconnect_stale_calendar_selection_keeps_independent_sources(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    token = _google_calendar(tmp_path, monkeypatch)
    raw = json.loads(token.read_text())
    raw['scopes'].append('https://www.googleapis.com/auth/gmail.readonly')
    token.write_text(json.dumps(raw))
    managed.record_google_consent(tmp_path, raw['scopes'])
    monkeypatch.setattr(rec, 'RELAY', Relay(_health()))
    settings = tmp_path / 'config/settings.json'
    settings.write_text(json.dumps({'google_calendar_selection': {
        'mode': 'selected', 'calendar_ids': ['private@example.com'], 'generation': 'old-consent'}}))
    authority = rec._runtime_source_authority()
    assert authority['complete'] is True
    assert authority['bridge_sources'] == ['imessage']
    assert authority['google_sources'] == ['gmail']
    assert rec._runtime_source_authority()['revision'] == authority['revision']


def test_google_access_refresh_during_bridge_probe_keeps_consent_snapshot(tmp_path, monkeypatch):
    import google_source_authority
    _setup(tmp_path, monkeypatch)
    token = _google_calendar(tmp_path, monkeypatch)
    monkeypatch.setattr(rec, 'RELAY', Relay(_health()))
    original = rec._runtime_source_authority()
    before = google_source_authority._token(token)
    def refresh(_request, timeout):
        assert timeout == 2.0
        record = json.loads(token.read_text())
        record.update(token='new-synthetic-access-token', expiry='2026-10-14T14:00:00Z')
        token.write_text(json.dumps(record))
        return {'result': {'structuredContent': _health(), 'isError': False}}
    monkeypatch.setattr(rec, 'RELAY', SimpleNamespace(bridge_connected=lambda: True, mcp_call=refresh))
    assert rec._runtime_source_authority() == original
    after = google_source_authority._token(token)
    assert before['body_digest'] != after['body_digest'], 'full credential snapshot remains sensitive for other consumers'
    assert before['grant_digest'] == after['grant_digest']


def test_google_unknown_snapshot_suspends_but_missing_token_is_disconnect(tmp_path, monkeypatch):
    import google_source_authority
    _setup(tmp_path, monkeypatch)
    token = _google_calendar(tmp_path, monkeypatch)
    monkeypatch.setattr(rec, 'RELAY', Relay(_health()))
    original = rec._runtime_source_authority()
    with monkeypatch.context() as patch:
        patch.setattr(google_source_authority, 'state', lambda *args, **kwargs: None)
        unknown = rec._runtime_source_authority()
        assert unknown['complete'] is False and unknown['revision'] is None
    assert rec._runtime_source_authority() == original
    token.unlink()
    disconnected = rec._runtime_source_authority()
    assert disconnected['complete'] is True and disconnected['google_sources'] == []
    assert disconnected['revision'] != original['revision']
    monkeypatch.delenv('SOTTO_HOME')
    assert rec._runtime_source_authority()['complete'] is True


def test_google_grant_replacement_during_bridge_probe_is_never_refresh(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    token = _google_calendar(tmp_path, monkeypatch)
    original = token.read_text()
    for changes in ({'refresh_token': 'other-refresh'}, {'client_id': 'other-client'},
                    {'client_secret': 'other-secret'}, {'sotto_source_generation': 'f'*32},
                    {'sotto_cloud_request_id': 'f'*64}, {'scopes': []}):
        token.write_text(original)
        def replace(_request, timeout):
            record = json.loads(original)
            record.update(changes)
            token.write_text(json.dumps(record))
            return {'result': {'structuredContent': _health(), 'isError': False}}
        monkeypatch.setattr(rec, 'RELAY', SimpleNamespace(bridge_connected=lambda: True, mcp_call=replace))
        result = rec._runtime_source_authority()
        assert result['complete'] is False and result['revision'] is None, changes.keys()
    token.write_text(original)
    def revoke(_request, timeout):
        managed.record_google_consent(tmp_path, [])
        return {'result': {'structuredContent': _health(), 'isError': False}}
    monkeypatch.setattr(rec, 'RELAY', SimpleNamespace(bridge_connected=lambda: True, mcp_call=revoke))
    assert rec._runtime_source_authority()['complete'] is False


def test_google_failure_stage_log_excludes_exception_query_and_ids(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('SOTTO_RUNTIME', 'sotto')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('SOTTO_HOME', str(tmp_path / 'home'))
    current = {'version': 2, 'revision': 'a' * 64, 'complete': True,
               'bridge_sources': [], 'google_sources': ['gmail']}
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: dict(current))
    secret = 'PRIVATE-QUERY-ID-TOKEN-EXCEPTION'
    reader = tmp_path / 'google_reader.py'
    monkeypatch.setattr(rec, '_google_api_py', lambda: str(reader))
    body = json.dumps({'operation': 'gmail_search', 'arguments': {'query': secret, 'max': 1},
                       'source_revision': 'a' * 64}).encode()
    for supplied, expected in [('provider_forbidden', 'provider_forbidden'), (secret, 'unknown'),
                               ([secret], 'unknown'), ({'secret': secret}, 'unknown')]:
        reader.write_text(f"def read_owner(operation, arguments):\n"
                          f"    error = RuntimeError({secret!r})\n"
                          f"    error.sotto_read_stage = {supplied!r}\n    raise error\n")
        sent = []
        fake = SimpleNamespace(path='/runtime/google-read', client_address=('127.0.0.1', 1234),
                               headers={'Content-Length': str(len(body))}, rfile=io.BytesIO(body),
                               _authed=lambda token: token == rec.MCP_TOKEN,
                               _send=lambda status, result: sent.append((status, result)))
        rec.Handler.do_POST(fake)
        captured = capsys.readouterr()
        assert sent == [(503, {'error': 'Google read unavailable', 'read_stage': expected})]
        assert not captured.out and secret not in captured.err
        assert json.loads(captured.err) == {'event': 'google_read_unavailable',
                                           'operation': 'gmail_search', 'stage': expected}

    for status, phase, extra in [(404, 'search_metadata', {'read_http_status': 404, 'read_phase': 'search_metadata'}),
                                 ('404 PRIVATE', secret, {}), (True, 'message_get', {}),
                                 (600, ['search_list'], {})]:
        reader.write_text(f"def read_owner(operation, arguments):\n"
                          f"    error = RuntimeError({secret!r})\n"
                          f"    error.sotto_read_stage = 'provider_http_other'\n"
                          f"    error.sotto_read_http_status = {status!r}\n"
                          f"    error.sotto_read_phase = {phase!r}\n    raise error\n")
        sent = []
        fake.rfile = io.BytesIO(body)
        fake._send = lambda status, result: sent.append((status, result))
        rec.Handler.do_POST(fake)
        assert sent == [(503, {'error': 'Google read unavailable', 'read_stage': 'provider_http_other', **extra})]
        assert secret not in json.dumps(sent)
        capsys.readouterr()

    closed = io.StringIO()
    closed.close()
    sent = []
    fake.rfile = io.BytesIO(body)
    fake._send = lambda status, result: sent.append((status, result))
    with monkeypatch.context() as context:
        context.setattr(rec.sys, 'stderr', closed)
        rec.Handler.do_POST(fake)
    assert sent == [(503, {'error': 'Google read unavailable', 'read_stage': 'provider_http_other'})]


def test_google_failure_stages_separate_late_authority_from_result_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('SOTTO_RUNTIME', 'sotto')
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('SOTTO_HOME', str(tmp_path / 'home'))
    before = {'version': 2, 'revision': 'a' * 64, 'complete': True,
              'bridge_sources': [], 'google_sources': ['gmail']}
    reader = tmp_path / 'google_contract_reader.py'
    monkeypatch.setattr(rec, '_google_api_py', lambda: str(reader))
    body = json.dumps({'operation': 'gmail_search', 'arguments': {'query': 'PRIVATE-QUERY', 'max': 1},
                       'source_revision': 'a' * 64}).encode()
    for mode, expected in [('authority', 'authority_after'), ('source', 'result_contract'),
                           ('size', 'result_contract')]:
        calls = []
        def authority():
            calls.append(True)
            return {**before, 'revision': 'b' * 64 if mode == 'authority' and len(calls) == 2 else 'a' * 64}
        monkeypatch.setattr(rec, '_runtime_source_authority', authority)
        value = {'source': 'calendar' if mode == 'source' else 'gmail'}
        if mode == 'size':
            value['private'] = 'PRIVATE-CONTENT' * 6000
        reader.write_text(f"def read_owner(operation, arguments):\n    return {value!r}\n")
        sent = []
        fake = SimpleNamespace(path='/runtime/google-read', client_address=('127.0.0.1', 1234),
                               headers={'Content-Length': str(len(body))}, rfile=io.BytesIO(body),
                               _authed=lambda token: token == rec.MCP_TOKEN,
                               _send=lambda status, result: sent.append((status, result)))
        rec.Handler.do_POST(fake)
        captured = capsys.readouterr()
        assert sent == [(503, {'error': 'Google read unavailable', 'read_stage': expected})]
        assert 'PRIVATE' not in captured.err and not captured.out
        assert json.loads(captured.err)['stage'] == expected
