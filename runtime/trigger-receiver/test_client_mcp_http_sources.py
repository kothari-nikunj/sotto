"""Production client HTTP routing with synthetic transports and original cloud readers."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import client_mcp_audit

from test_commitment_history_read import _write
from test_receiver import rec
from test_runtime_knowledge_read import PACK, REVISION, tree

NEW_TOOLS = {
    'sotto_mac_context': {'sources': ['chrome'], 'since_hours': 24},
    'sotto_note_get': {'id': 42},
    'sotto_contacts_search': {'query': 'Ada', 'max': 1},
    'sotto_contacts_get': {'resource_name': 'people/c123'},
    'sotto_granola_history': {'days': 7, 'meeting_id': 'm1'},
}


@pytest.fixture
def source_http(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    root.mkdir()
    monkeypatch.setattr(rec, 'DATA', str(root))
    monkeypatch.setenv('SOTTO_MCP_OAUTH', '1')
    monkeypatch.setenv('SOTTO_MCP_OAUTH_ORIGIN', 'https://sotto.example')
    state = {'complete': True, 'revision': REVISION, 'bridge_sources': ['chrome', 'apple_notes'],
             'google_sources': ['google_contacts'], 'connector_sources': ['granola'],
             'local_search': {'status': 'known', 'enabled': False, 'history_days': 30}}
    observations = []
    control = SimpleNamespace(after=None, calls=[], google_sessions=[], coverage_invalid=False, on_read=None, native_note=False)
    def authority(**options):
        observations.append(dict(options))
        value = dict(state)
        if len(observations) > 1 and control.after:
            value.update(control.after)
        if not options.get('include_search_policy'):
            value.pop('local_search')
        return value
    monkeypatch.setattr(rec, '_runtime_source_authority', authority)
    def bridge(request, **_):
        params = request['params']
        control.calls.append(params)
        if control.on_read:
            control.on_read()
        if params['name'] == 'get_note':
            payload = {'status': 'ok', 'id': 42, 'body': 'Synthetic original Apple Note', 'partial': False}
            if control.native_note:
                payload.update(body='x' * 3000, note_page_version=1, client_page={
                    'offset': 0, 'returned_chars': 3000, 'total_chars': 6000, 'complete': False,
                    'next_cursor': REVISION + ':3000', 'byte_limit': 32768})
        else:
            assert params == {'name': 'read_local', 'arguments': {'sources': ['chrome'], 'since_hours': 24}}
            coverage = {'status': 'partial', 'rows_returned': 1, 'omitted_rows': None,
                        'effective_window': {'since': None, 'until': None, 'basis': 'snapshot'},
                        'query_row_caps': [20]}
            if control.coverage_invalid:
                coverage['private_error'] = 'must not cross HTTP'
            payload = {'chrome_history': [{'title': 'Synthetic Cedar page'}], 'search_queries': [],
                       'contacts': [{'name': 'Unrequested Mac contact'}],
                       'source_status': {'chrome': 'partial', 'contacts': 'ok'},
                       'source_coverage': {'chrome': coverage, 'contacts': {'status': 'ok'}},
                       '_bridge_request_started_at': 'internal-only'}
        return {'result': {'structuredContent': payload}}
    monkeypatch.setattr(rec, 'RELAY', SimpleNamespace(mcp_call=bridge,
        bridge_connected=lambda: bool(state['bridge_sources']),
        bridge_last_seen_at='2026-11-13T12:30:00+00:00'))

    google_fixture = PACK.parent / 'adapters/sotto/test_google_owner_read.py'
    spec = importlib.util.spec_from_file_location('http_source_google_fixture', google_fixture)
    google = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(google)
    def google_read(operation, arguments):
        session = google.ContactsSession([google.contact()])
        control.google_sessions.append(session)
        return google.api.read_owner(operation, arguments, lambda: session)
    monkeypatch.setattr(rec, '_sotto_adapter', lambda name: SimpleNamespace(read_owner=google_read))

    # The child executes the real main/query/gather parser; only provider transport and clock
    # are replaced by the existing Granola fixture. No token lookup or network can occur.
    entry = tmp_path / 'granola_transport_fixture.py'
    entry.write_text(
        'import sys\n'
        f'sys.path[:0] = {repr([str(PACK / "_shared/scripts"), str(PACK / "_shared/lib"), str(Path(__file__).parent)])}\n'
        'import granola_history as history\n'
        'from test_granola_history_read import factory, NOW\n'
        'create, calls = factory(history)\n'
        'history.bounded_factory = lambda deadline: create\n'
        'original = history.query\n'
        'history.query = lambda *a, **k: original(*a, **k, now=NOW)\n'
        'raise SystemExit(history.main())\n')
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *parts: str(entry)
        if parts == ('_shared', 'scripts', 'granola_history.py') else str(PACK.joinpath(*parts)))
    monkeypatch.setattr(rec, '_spawn_env', lambda _: {**os.environ, 'SOTTO_DATA': str(root)})
    server = ThreadingHTTPServer(('127.0.0.1', 0), rec.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    def call(token, name=None, arguments=None, method='tools/call'):
        observations.clear()
        params = {'name': name, 'arguments': arguments or {}} if name else {}
        body = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
        connection = http.client.HTTPConnection(*server.server_address, timeout=10)
        try:
            connection.request('POST', rec.CLIENT_MCP.PATH, json.dumps(body), {
                'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                'Accept': 'application/json, text/event-stream'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()
    def grant(names):
        return rec.CLIENT_MCP.create_grant(str(root), 'synthetic HTTP client', names, 1)[0]
    control.call, control.grant, control.root, control.state = call, grant, root, state
    control.address = server.server_address
    yield control
    server.shutdown()
    server.server_close()
    worker.join()


def value(response):
    status, body = response
    assert status == 200 and body['result']['isError'] is False, response
    return body['result']['structuredContent']


@pytest.mark.parametrize('zone,expected', [('America/Los_Angeles', 'America/Los_Angeles'),
                                           ('+99:00', None), ('', None)])
def test_http_status_reports_configured_timezone_and_bridge_last_seen(source_http, monkeypatch, zone, expected):
    monkeypatch.setenv('SOTTO_TIMEZONE', zone)
    monkeypatch.delenv('TZ', raising=False)
    token = source_http.grant(['sotto_source_status'])
    rec.RELAY.bridge_connected = lambda: False
    status = value(source_http.call(token, 'sotto_source_status'))
    assert status['owner_timezone'] == expected and status['bridge_online'] is False
    assert status['now'].endswith('+00:00')
    assert status['bridge_last_seen_at'] == '2026-11-13T12:30:00+00:00'
    assert source_http.calls == []


def test_http_commitment_evidence_and_handled_are_bounded_and_source_fenced(source_http):
    fixture = source_http
    (fixture.root / 'knowledge').mkdir()
    fixture.state['google_sources'] = ['gmail', 'google_contacts']
    fixture.state['bridge_sources'].append('imessage')
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    open_row = _write(fixture.root, 'open1', status='open', resolution='', resolution_source_refs=[],
           source_refs=[{'sourceType': 'email', 'sourceId': f'ask-{i}', 'snippet': 'not returned'}
                        for i in range(3)], recorded_at=recent, chase_pending='2026-06-10')
    _write(fixture.root, 'open2', status='open', resolution='', resolution_source_refs=[])
    _write(fixture.root, 'recent', closed_at=recent, resolution='intro_sent',
           resolution_basis={'basis': 'intro_sent', 'recorded_at': recent})
    _write(fixture.root, 'expired', status='expired', closed_at=recent)
    _write(fixture.root, 'old')
    from delivery_effects import loop_version
    events = fixture.root / 'events'
    events.mkdir()
    (events / 'loop-checks.json').write_text(json.dumps({'checks': {loop_version(open_row):
        {'basis': 'you_messaged', 'source': 'whatsapp', 'at': recent}}}))
    token = fixture.grant(['sotto_commitments'])
    before = tree(fixture.root)
    first = value(fixture.call(token, 'sotto_commitments', {'limit': 2}))
    task, = first['you_owe']
    handled, = first['recently_handled']
    assert [ref['sourceId'] for ref in task['source_refs']] == ['ask-0', 'ask-2']
    assert 'snippet' not in task['source_refs'][0] and 'check' not in task
    assert task['recorded_at'] == recent and 'chase_pending' not in task and 'anchor_key' not in task
    assert handled['closed_by'] == 'intro_sent' and handled['resolution_source_refs'][0]['sourceId'] == 'later-text'
    assert first['counts'] == {'you_owe': 2, 'waiting_on_them': 0, 'recently_handled': 1}
    cursor = first['client_page']['next_cursor']
    assert cursor and first['client_page']['complete'] is False
    second = value(fixture.call(token, 'sotto_commitments', {'limit': 2, 'cursor': cursor}))
    assert second['you_owe'][0]['loop_identity'] != task['loop_identity']
    assert second['recently_handled'] == [] and second['client_page']['next_cursor'] is None
    assert tree(fixture.root) == before
    fixture.state['bridge_sources'].remove('imessage')
    withheld = value(fixture.call(token, 'sotto_commitments'))
    assert withheld['recently_handled'] == [] and withheld['counts']['recently_handled'] == 0
    changed = value(fixture.call(token, 'sotto_commitments', {'limit': 2, 'cursor': cursor}))
    assert changed['status'] == 'changed'


def test_http_commitment_direction_and_handled_window_are_independent(source_http):
    fixture = source_http
    (fixture.root / 'knowledge').mkdir()
    fixture.state['google_sources'].append('gmail')
    fixture.state['bridge_sources'].append('imessage')
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    _write(fixture.root, 'you-open', status='open', resolution='', resolution_source_refs=[])
    _write(fixture.root, 'they-open', status='open', action_type='waiting_on', resolution='', resolution_source_refs=[])
    _write(fixture.root, 'you-done', closed_at=old)
    _write(fixture.root, 'they-done', action_type='waiting_on', closed_at=old)
    token = fixture.grant(['sotto_commitments'])
    defaults = value(fixture.call(token, 'sotto_commitments'))
    assert defaults['recently_handled'] == []
    waiting = value(fixture.call(token, 'sotto_commitments', {
        'direction': 'they_owe', 'include_handled_days': 14, 'limit': 1}))
    assert waiting['counts'] == {'you_owe': 0, 'waiting_on_them': 1, 'recently_handled': 1}
    assert waiting['waiting_on_them'][0]['action_type'] == 'waiting_on'
    cursor = waiting['client_page']['next_cursor']
    handled = value(fixture.call(token, 'sotto_commitments', {
        'direction': 'they_owe', 'include_handled_days': 14, 'limit': 1, 'cursor': cursor}))
    assert handled['recently_handled'][0]['direction'] == 'they_owe_you'
    different = value(fixture.call(token, 'sotto_commitments', {
        'direction': 'they_owe', 'include_handled_days': 30, 'cursor': cursor}))
    assert different['status'] == 'changed'
    active = value(fixture.call(token, 'sotto_commitments', {'direction': 'you_owe', 'include_handled_days': 0}))
    assert active['counts'] == {'you_owe': 1, 'waiting_on_them': 0, 'recently_handled': 0}
    _, invalid = fixture.call(token, 'sotto_commitments', {'include_handled_days': 31})
    assert invalid['result']['structuredContent']['reason'] == 'invalid_arguments'
    fixture.state['bridge_sources'].remove('imessage')
    withheld = value(fixture.call(token, 'sotto_commitments', {'direction': 'they_owe', 'include_handled_days': 14}))
    assert withheld['recently_handled'] == []


def test_http_new_readers_retain_original_evidence_bounds_and_no_data_writes(source_http):
    fixture = source_http
    token = fixture.grant([*NEW_TOOLS, 'sotto_source_status'])
    before = tree(fixture.root)
    results = {name: value(fixture.call(token, name, args)) for name, args in NEW_TOOLS.items()}
    mac = results['sotto_mac_context']
    assert mac['chrome_history'][0]['title'] == 'Synthetic Cedar page'
    assert mac['source_coverage']['chrome']['omitted_rows'] is None
    assert mac['source_coverage']['chrome']['query_row_caps'] == [20]
    assert mac['source_status'] == {'chrome': 'partial'}
    assert 'contacts' not in mac and '_bridge_request_started_at' not in mac
    assert results['sotto_note_get']['body'] == 'Synthetic original Apple Note'
    contacts = results['sotto_contacts_search']
    assert contacts['items'][0]['resource_name'] == 'people/c123'
    assert contacts['observation']['complete'] is False
    assert 'private-profile' not in json.dumps(contacts) and 'photos' not in json.dumps(contacts)
    assert results['sotto_contacts_get']['item']['fields']['emailAddresses'][0]['value'] == 'ada@example.com'
    assert all(session.closed and all(row.closed for row in session.responses)
               for session in fixture.google_sessions)
    meeting = results['sotto_granola_history']
    assert meeting['notes'] == {'your_notes': 'Actual note', 'ai_summary': 'Actual decision'}
    assert len(meeting['transcript']) == 12000 and meeting['transcript_truncated'] is True
    discovery = value(fixture.call(token, 'sotto_granola_history', {'days': 7}))
    assert discovery['meetings'][0]['meeting_id'] == 'm1' and 'transcript' not in discovery
    assert tree(fixture.root) == before


@pytest.mark.parametrize('native', [False, True])
def test_http_client_revocation_during_original_read_withholds_content(source_http, native):
    fixture = source_http
    token = fixture.grant(['sotto_note_get'])
    grant = rec.CLIENT_MCP.authenticate(str(fixture.root), token)
    fixture.native_note = native
    fixture.on_read = lambda: rec.CLIENT_MCP.revoke(str(fixture.root), grant['client_id'])
    status, body = fixture.call(token, 'sotto_note_get', {'id': 42})
    assert status == 401 and 'Synthetic original' not in json.dumps(body)
    assert 'x' * 100 not in json.dumps(body)
    assert fixture.calls == [{'name': 'get_note', 'arguments': {'id': 42},
                              '_meta': {'sotto/accepts': ['note_page_v1'], 'sotto/note_cursor': 'first'}}]


def test_http_status_reports_granted_candidates_and_legacy_grants_stay_scoped(source_http, monkeypatch):
    fixture = source_http
    oauth = rec.CLIENT_OAUTH
    status_only = fixture.grant(['sotto_source_status'])
    partial = fixture.grant(['sotto_source_status', 'sotto_mac_context'])
    status = value(fixture.call(status_only, 'sotto_source_status'))
    assert status['source_readers']['google_contacts']['available'] is True
    assert all(not row['tools'] for row in status['source_readers'].values())
    assert status['local_search']['enabled'] is False
    scoped = value(fixture.call(partial, 'sotto_source_status'))
    assert scoped['source_readers']['chrome']['tools'] == ['sotto_mac_context']
    assert scoped['source_readers']['granola']['tools'] == []
    assert fixture.calls == [] and fixture.google_sessions == []

    # A pre-expansion connection has a persisted tool snapshot. Exercise the real OAuth
    # authenticator while supplying a synthetic live dashboard session, not a login flow.
    oauth_token = 'sotto_oauth_access_' + 'x' * 43
    expires = int(time.time()) + 3600
    with oauth.store(str(fixture.root)) as db:
        db.execute('INSERT INTO connections VALUES(?,?,?,?,?,?,0)',
            ('old-connection', 'old-client', 'synthetic-owner-session',
             'https://sotto.example/client-mcp', expires, json.dumps(['sotto_source_status'])))
        db.execute('INSERT INTO tokens VALUES(?,?,?,?,0)',
            (oauth.digest(oauth_token), 'old-connection', 'access', expires))
    monkeypatch.setattr(rec.DASHBOARD, 'session_by_digest',
        lambda digest: {'valid': True} if digest == 'synthetic-owner-session' else None)
    before = tree(fixture.root)
    for token in (status_only, oauth_token):
        code, body = fixture.call(token, method='tools/list')
        assert code == 200 and [row['name'] for row in body['result']['tools']] == ['sotto_source_status']
        for name, arguments in NEW_TOOLS.items():
            code, body = fixture.call(token, name, arguments)
            assert code == 200 and body['error']['code'] == -32602
        assert not any(row['tools'] for row in value(fixture.call(token, 'sotto_source_status'))['source_readers'].values())
    assert not fixture.calls and not fixture.google_sessions
    assert tree(fixture.root) == before


@pytest.mark.parametrize('boundary', ['source_revoked', 'bridge_sleep', 'malformed_coverage'])
def test_http_source_fences_and_coverage_fail_closed(source_http, boundary):
    fixture = source_http
    token = fixture.grant(list(NEW_TOOLS))
    before = tree(fixture.root)
    if boundary == 'source_revoked':
        fixture.after = {'revision': 'b' * 64}
    elif boundary == 'bridge_sleep':
        fixture.after = {'bridge_sources': []}
    else:
        fixture.coverage_invalid = True
    code, body = fixture.call(token, 'sotto_mac_context', NEW_TOOLS['sotto_mac_context'])
    # A Mac that sleeps after answering keeps its result: the after-read check is consent only.
    assert code == 200 and body['result']['isError'] is (boundary != 'bridge_sleep')
    if boundary != 'bridge_sleep':
        assert 'Synthetic Cedar' not in json.dumps(body) and 'must not cross' not in json.dumps(body)
    for name in ('sotto_contacts_get', 'sotto_granola_history'):
        response = fixture.call(token, name, NEW_TOOLS[name])
        if boundary == 'source_revoked':
            assert response[0] == 200 and response[1]['result']['isError'] is True
        else:
            assert value(response)
    assert tree(fixture.root) == before


@pytest.mark.parametrize('failure', ['grant_revoked', 'source_revoked', 'bridge_sleep'])
def test_http_native_note_continuation_rechecks_current_authority(source_http, failure):
    fixture = source_http
    fixture.native_note = True
    token = fixture.grant(['sotto_note_get'])
    first = value(fixture.call(token, 'sotto_note_get', {'id': 42}))
    assert len(first['body']) == 3000
    if failure == 'grant_revoked':
        grant = rec.CLIENT_MCP.authenticate(str(fixture.root), token)
        rec.CLIENT_MCP.revoke(str(fixture.root), grant['client_id'])
    else:
        fixture.state['bridge_sources'] = []
        if failure == 'source_revoked':
            fixture.state['revision'] = 'b' * 64
    status, response = fixture.call(token, 'sotto_note_get', {
        'id': 42, 'cursor': first['client_page']['next_cursor']})
    assert 'x' * 100 not in json.dumps(response)
    if failure == 'grant_revoked':
        assert status == 401
    else:
        assert response['result']['isError'] is True
    assert len(fixture.calls) == 1


def test_partial_book_never_names_a_handle_and_complete_lookup_is_reused(source_http, monkeypatch):
    fixture = source_http
    fixture.state['bridge_sources'] = ['imessage', 'contacts']
    token = fixture.grant(['sotto_message_history', 'sotto_source_status'])
    args = {'source': 'imessage', 'since': int(time.time()) - 90, 'until': int(time.time())}
    books = []
    status = 'partial'
    ticks = [0]
    contacts = rec.CLIENT_MCP_BACKEND.contacts
    monkeypatch.setattr(contacts, 'time', SimpleNamespace(monotonic=lambda: ticks[0]))

    def bridge(request, **_):
        if request['params']['name'] == 'read_history':
            payload = {'status': 'ok', 'rows': [{'handle': '+14155550101'}]}
        else:
            books.append(status)
            payload = {'source_status': {'contacts': status}, 'contacts': [
                {'name': 'Fictional Avery', 'phones': ['4155550101']}]}
        return {'result': {'structuredContent': payload}}
    monkeypatch.setattr(rec.RELAY, 'mcp_call', bridge)
    def row():
        return value(fixture.call(token, 'sotto_message_history', args))['rows'][0]

    assert 'handle_name' not in row()
    assert 'handle_name' not in row() and books == ['partial']
    status = 'ok'
    ticks[0] += contacts.CACHE_SECONDS
    assert row()['handle_name'] == row()['handle_name'] == 'Fictional Avery'
    assert books == ['partial', 'ok']
    fixture.state['revision'] = 'd' * 64
    status = 'partial'
    assert 'handle_name' not in row()
    fixture.state['bridge_sources'].remove('contacts')
    assert 'handle_name' not in row()
    assert books == ['partial', 'ok', 'partial']


def test_status_says_when_history_can_name_handles_without_contacts_tool(source_http):
    source_http.state['bridge_sources'] += ['imessage', 'contacts']
    token = source_http.grant(['sotto_message_history', 'sotto_source_status'])
    contacts = value(source_http.call(token, 'sotto_source_status'))['source_readers']['contacts']
    assert contacts['tools'] == []
    assert contacts['label_identifiers_in'] == ['sotto_message_history']


@pytest.mark.parametrize('native', [False, True])
def test_failed_note_has_closed_status_and_no_native_diagnostics(source_http, monkeypatch, native):
    data = {'status': 'degraded', 'error': 'unable to open /fictional/notes/db'}
    if native:
        data['note_page_version'] = 1
    monkeypatch.setattr(rec.RELAY, 'mcp_call', lambda *_, **kw: {'result': {'structuredContent': data}})
    token = source_http.grant(['sotto_note_get'])
    assert value(source_http.call(token, 'sotto_note_get', {'id': 42})) == {'status': 'degraded'}


def test_person_historical_reader_scoped_to_grant():
    saved = {'coverage': {'historical_sources': [
        {'source': 'gmail', 'reader': 'sotto_email_search'},
        {'source': 'granola', 'reader': 'sotto_granola_history'}]}}
    value = rec.CLIENT_MCP.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
        'name': 'sotto_person', 'arguments': {'person': 'Avery'}}},
        {'tools': ['sotto_person', 'sotto_email_search']}, lambda *_: saved)['result']['structuredContent']
    gmail, granola = value['coverage']['historical_sources']
    assert gmail['reader'] == 'sotto_email_search'
    assert not granola.get('reader')


def test_mcp_access_audit_distinguishes_http_success_from_partial_read(source_http):
    token = source_http.grant(['sotto_mac_context', 'sotto_contacts_search'])
    value(source_http.call(token, 'sotto_mac_context', {'sources': ['chrome']}))
    value(source_http.call(token, 'sotto_contacts_search', {'query': 'Ada', 'max': 1}))
    tools = {'sotto_mac_context', 'sotto_contacts_search'}
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        rows = client_mcp_audit.recent(source_http.root, time.time())
        if {row['tool'] for row in rows} == tools:
            break
        time.sleep(.01)
    assert {row['tool'] for row in rows} == tools
    assert all(r['outcome'] == 'ok' and r.get('read_status') == 'partial' for r in rows)


@pytest.mark.parametrize('tool,payload,expected', [
    ('sotto_contacts', {'mac_contacts': {'status': 'unavailable'},
                        'google_contacts': {'observation': {'status': 'ok'}}}, 'partial'),
    ('sotto_search_local', {'status': 'ok', 'coverage': {'recent_files': {'status': 'not_configured'}}},
     'not_configured'),
    ('sotto_note_get', {'status': 'locked'}, 'locked'),
    ('sotto_person', {'person': {'status': 'fictional-private'}}, None),
])
def test_content_free_read_status_uses_only_source_observations(tool, payload, expected):
    assert client_mcp_audit.read_status(payload, tool) == expected
