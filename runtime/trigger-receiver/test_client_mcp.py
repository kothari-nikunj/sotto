"""External-client authority and real stdio → HTTP → canonical-reader acceptance."""
import io
import importlib.util
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from email.message import Message
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import client_mcp as mcp
import client_mcp_backend as backend
from test_runtime_knowledge_read import graph, tree, PACK, REVISION

__all__ = ['graph']
ADAPTER = Path(__file__).resolve().parents[2] / 'tools/sotto_mcp_client.py'


def request(method='tools/call', **params):
    return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}


def handler(token, body=None):
    headers = Message()
    headers['Authorization'] = 'Bearer ' + token
    headers['Accept'] = 'application/json, text/event-stream'
    headers['Content-Type'] = 'application/json'
    raw = json.dumps(body or request('tools/list')).encode()
    headers['Content-Length'] = str(len(raw))
    return SimpleNamespace(path=mcp.PATH, headers=headers, rfile=io.BytesIO(raw),
        _send=lambda code, body: (code, body), _write=lambda code, *rest: (code, None))


@pytest.fixture
def access(tmp_path):
    token, grant = mcp.create_grant(str(tmp_path), 'fixture-client', list(mcp.TOOLS), 1)
    return str(tmp_path), token, grant


def test_grant_is_opt_in_scoped_expiring_and_not_a_relay_token(access, tmp_path, monkeypatch):
    root, token, grant = access
    assert mcp.authenticate(root, token) == grant
    assert token not in (tmp_path / 'config/mcp-clients' / (grant['client_id'] + '.json')).read_text()
    for invalid in ('', 'internal-root', 'a' * 64, token + 'x', token[:-1] + '!'):
        assert mcp.authenticate(root, invalid) is None
    other = tmp_path / 'other-tenant'
    other.mkdir()
    assert mcp.authenticate(str(other), token) is None
    monkeypatch.setattr(mcp.time, 'time', lambda: grant['expires_at'])
    assert mcp.authenticate(root, token) is None


def test_grant_revoke_and_private_file_checks(access):
    root, token, grant = access
    path = Path(root) / 'config/mcp-clients' / (grant['client_id'] + '.json')
    path.chmod(0o644)
    assert mcp.authenticate(root, token) is None
    path.chmod(0o600)
    mcp.revoke(root, grant['client_id'])
    assert mcp.authenticate(root, token) is None
    path.symlink_to(Path(root) / 'outside')
    assert mcp.authenticate(root, token) is None


def test_revocation_during_read_withholds_all_content(access):
    root, token, grant = access
    def read(*_):
        mcp.revoke(root, grant['client_id'])
        return {'private': 'must not leave receiver'}
    response = mcp.handle(handler(token, request(name='sotto_person', arguments={'person': 'Dana'})),
                          'POST', root, read)
    assert response[0] == 401
    assert 'private' not in json.dumps(response)


def test_ungranted_tool_cannot_call_backend(access):
    _, _, grant = access
    grant = {**grant, 'tools': ['sotto_company']}
    tools = mcp.dispatch(request('tools/list'), grant, None)['result']['tools']
    assert [tool['name'] for tool in tools] == ['sotto_company']
    for name in ('sotto_person', 'send_message', 'run_sotto_script', 'calendar_create'):
        result = mcp.dispatch(request(name=name, arguments={}), grant,
                              lambda *_: pytest.fail('forbidden backend called'))
        assert result['error']['code'] == -32602


@pytest.mark.parametrize('name,arguments', [
    ('sotto_person', {'person': 'Dana', 'operation': 'send'}),
    ('sotto_person', {'person': '../secret\n'}),
    ('sotto_company', {'company': True}),
    ('sotto_email_search', {'query': 'Cedar', 'max': True}),
    ('sotto_email_search', {'query': 'x' * 257}),
    ('sotto_search_local', {'query': 'Cedar', 'sources': ['gmail'], 'since': 1, 'until': 2}),
    ('sotto_search_local', {'query': 'Cedar', 'sources': ['imessage'] * 2, 'since': 1, 'until': 2}),
    ('sotto_message_history', {'source': 'imessage', 'since': 2, 'until': 1}),
    ('sotto_message_history', {'source': 'imessage', 'since': 1, 'until': 40000000}),
    ('sotto_message_history', {'source': 'imessage', 'since': 1, 'until': 2, 'limit': 500}),
])
def test_invalid_tool_inputs_never_reach_readers(access, name, arguments):
    result = mcp.dispatch(request(name=name, arguments=arguments), access[2],
                          lambda *_: pytest.fail('invalid arguments reached backend'))
    assert result['error']['code'] == -32602


def test_notifications_and_unknown_methods_cannot_read(access):
    for method in ('tools/call', 'notifications/initialized'):
        body = request(method, name='sotto_person', arguments={'person': 'Dana'})
        del body['id']
        assert mcp.dispatch(body, access[2], lambda *_: pytest.fail('notification ran')) is None
    result = mcp.dispatch(request('resources/read', uri='file:///private'), access[2], None)
    assert result['error']['code'] == -32601


def test_protocol_and_read_only_catalog(access):
    result = mcp.dispatch(request('initialize', protocolVersion='2025-06-18'), access[2], None)['result']
    assert result['protocolVersion'] == '2025-06-18'
    assert result['capabilities'] == {'tools': {}}
    assert 'source archives' not in result['serverInfo']
    for name, tool in mcp.TOOLS.items():
        assert tool['annotations']['readOnlyHint']
        assert not tool['inputSchema']['additionalProperties']
        assert not any(word in name for word in ('send', 'write', 'save', 'close'))


@pytest.mark.parametrize('params', [{'_meta': {'progressToken': 1}}, {'cursor': None},
                                  {'cursor': '', '_meta': {}}])
def test_catalog_accepts_native_client_metadata_without_changing_grants(access, params):
    result = mcp.dispatch(request('tools/list', **params), access[2], None)
    assert {row['name'] for row in result['result']['tools']} == set(access[2]['tools'])


def test_backend_errors_and_oversize_never_leak(access):
    def broken(*_):
        raise RuntimeError('Bearer super-secret /private/email.txt')
    for read in (broken, lambda *_: {'private': 'z' * (mcp.MAX_RESULT + 1)}):
        result = mcp.dispatch(request(name='sotto_person', arguments={'person': 'Dana'}), access[2], read)
        assert result['result']['isError']
        assert 'super-secret' not in json.dumps(result)
        assert '/private' not in json.dumps(result)
        assert 'No result was established' in result['result']['content'][0]['text']


def test_unicode_result_bound_measures_utf8_not_json_escape_expansion(access):
    value = {'text': '🧑' * 30000}
    result = mcp.dispatch(request(name='sotto_person', arguments={'person': 'Dana'}), access[2], lambda *_: value)
    assert result['result']['isError'] is False
    assert result['result']['structuredContent'] == value
    assert len(json.dumps(result).encode()) < 1024 * 1024


@pytest.mark.parametrize('mutation,code', [
    (lambda h: h.headers.__setitem__('Origin', 'https://attacker.example'), 403),
    (lambda h: h.headers.replace_header('Accept', 'application/json'), 406),
    (lambda h: h.headers.replace_header('Content-Length', '999999'), 400),
    (lambda h: h.headers.__setitem__('Transfer-Encoding', 'chunked'), 400),
    (lambda h: h.headers.__setitem__('MCP-Protocol-Version', 'unknown'), 400),
    (lambda h: setattr(h, 'path', '/client-mcp?token=secret'), 404),
    (lambda h: h.headers.replace_header('Authorization', 'Bearer internal-root'), 401),
])
def test_http_boundary(access, mutation, code):
    h = handler(access[1])
    mutation(h)
    assert mcp.handle(h, 'POST', access[0], lambda *_: pytest.fail('read occurred'))[0] == code


def test_http_duplicate_json_and_busy_bound(access):
    h = handler(access[1])
    raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","method":"tools/call"}'
    h.rfile = io.BytesIO(raw)
    h.headers.replace_header('Content-Length', str(len(raw)))
    assert mcp.handle(h, 'POST', access[0], None)[0] == 400
    for _ in range(4):
        assert mcp._ACTIVE.acquire(False)
    try:
        assert mcp.handle(handler(access[1]), 'POST', access[0], None)[0] == 429
    finally:
        for _ in range(4):
            mcp._ACTIVE.release()
    assert mcp.handle(handler(access[1]), 'GET', access[0], None)[0] == 405


def test_unread_body_rejection_closes_connection_and_notification_is_202(access):
    h = handler(access[1])
    h.headers['Transfer-Encoding'] = 'chunked'
    assert mcp.handle(h, 'POST', access[0], None)[0] == 400
    assert h.close_connection is True
    h = handler(access[1], {'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    assert mcp.handle(h, 'POST', access[0], None) == (202, None)


def test_source_status_uses_real_offline_authority_and_safe_coverage(tmp_path, monkeypatch):
    from test_source_authority import rec, _setup, _google_calendar, Relay
    _setup(tmp_path, monkeypatch)
    _google_calendar(tmp_path, monkeypatch)
    monkeypatch.setattr(rec, 'RELAY', Relay(None))
    knowledge = tmp_path / 'knowledge'
    knowledge.mkdir()
    (knowledge / 'history-state.json').write_text(json.dumps({'sources': {
        'imessage': {'error': 'PRIVATE MESSAGE SHOULD NOT LEAVE'},
        'gmail': {'error': 'PRIVATE PROVIDER ERROR'}}}))
    before = rec._runtime_source_authority()
    assert before['complete'] is True and before['google_sources'] == ['calendar']
    assert before['bridge_sources'] == []
    assert rec._runtime_source_authority() == before  # no volatile timestamp fields
    result = backend.read(rec, 'sotto_source_status', {})
    assert result['bridge_online'] is False
    assert result['available_sources'] == ['calendar']
    assert 'PRIVATE' not in json.dumps(result)
    assert result['history_learning']['sources']['imessage'] == {'checkpoint': 'withheld'}


def test_source_status_reuses_authority_health_probe(tmp_path, monkeypatch):
    from test_source_authority import rec, _setup, _health, Relay, managed
    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv('SOTTO_HOME', raising=False)
    managed.record_bridge_consent(tmp_path, ['imessage'], ['imessage'])
    health = _health()
    health['local_search'] = {'status': 'known', 'enabled': True, 'history_days': 30,
                              'document_folders': ['PRIVATE']}
    relay = Relay(health)
    monkeypatch.setattr(rec, 'RELAY', relay)
    result = backend.read(rec, 'sotto_source_status', {})
    assert len(relay.calls) == 2  # existing before/after authority checks; no extra probe
    assert result['local_search']['history_days'] == 30
    assert 'PRIVATE' not in json.dumps(result)
    # The Node runtime strictly validates this separate wire contract.
    assert set(rec._runtime_source_authority()) == {
        'version', 'revision', 'complete', 'bridge_sources', 'google_sources', 'connector_sources'}


@pytest.mark.parametrize('policy,expected', [
    ({'status': 'known', 'enabled': True, 'history_days': 90, 'document_folders': ['PRIVATE']},
     {'status': 'known', 'enabled': True, 'history_days': 90}),
    ({'status': 'known', 'enabled': False, 'history_days': 30},
     {'status': 'known', 'enabled': False, 'history_days': 30}),
    ({}, {'status': 'unknown'}),
    ({'status': 'known', 'enabled': True, 'history_days': True}, {'status': 'unknown'}),
    ({'status': 'known', 'enabled': True, 'history_days': 366}, {'status': 'unknown'}),
])
def test_status_projects_actual_bridge_search_policy(policy, expected):
    from source_context import bridge_search_policy
    assert bridge_search_policy({'connected': True, 'local_search': policy}) == expected


def test_status_offline_and_failed_health_leave_search_window_unknown():
    from source_context import bridge_search_policy
    for health in (None, {}, {'connected': False, 'local_search': {
            'status': 'known', 'enabled': True, 'history_days': 90}}):
        assert bridge_search_policy(health) == {'status': 'unknown'}


@pytest.mark.parametrize('name,args,responses,source', [
    ('sotto_email_search', {'query': 'Cedar', 'max': 1}, [
        {'messages': [{'id': 'abc'}], 'nextPageToken': 'more'},
        {'id': 'abc', 'threadId': 'def', 'snippet': 'Pricing requested', 'payload': {'headers': []}}], 'gmail'),
    ('sotto_email_get', {'message_id': 'abc'}, [
        {'id': 'abc', 'threadId': 'def', 'snippet': 'Pricing requested', 'payload': {'headers': []}}], 'gmail'),
    ('sotto_email_thread', {'thread_id': 'def'}, [
        {'id': 'def', 'messages': [{'id': 'abc', 'threadId': 'def', 'internalDate': '1',
                                   'snippet': 'Pricing requested', 'payload': {'headers': []}}]}], 'gmail'),
    ('sotto_calendar', {'start': '2026-11-02T00:00:00Z', 'end': '2026-11-03T00:00:00Z'}, [
        {'items': [{'id': 'primary', 'accessRole': 'owner', 'primary': True}]},
        {'items': [{'id': 'e1', 'summary': 'Cedar meeting', 'organizer': {'email': 'owner@example.test', 'self': True},
                    'attendees': [{'email': 'dana@cedar.example'}],
                    'start': {'dateTime': '2026-11-02T10:00:00Z'},
                    'end': {'dateTime': '2026-11-02T11:00:00Z'}}]}], 'calendar'),
])
def test_google_mcp_calls_use_actual_read_owner_projection(tmp_path, monkeypatch, name, args, responses, source):
    from test_receiver import rec
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    # The receiver owns credential-root resolution (including compatible pilots).
    # MCP must use its authority result rather than repeat an environment check.
    monkeypatch.delenv('SOTTO_HOME', raising=False)
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.test')
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {
        'complete': True, 'revision': REVISION, 'bridge_sources': [], 'google_sources': [source]})
    api = rec._sotto_adapter('google_api')
    original = api.read_owner
    provider = iter(responses)
    calls = []
    def fetch(*a, **kw):
        calls.append(a[1])
        return next(provider)
    monkeypatch.setattr(api, '_owner_fetch', fetch)  # only the external transport is replaced
    monkeypatch.setattr(api, 'read_owner', lambda op, args: original(op, args, lambda: SimpleNamespace(close=lambda: None)))
    result = backend.read(rec, name, args)
    assert result['source'] == source
    assert len(calls) == len(responses)
    assert result['observation']['status'] == ('partial' if name == 'sotto_email_search' else 'ok')
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {
        'complete': True, 'revision': REVISION, 'bridge_sources': [], 'google_sources': []})
    with pytest.raises(RuntimeError):
        backend.read(rec, name, args)
    assert len(calls) == len(responses)


def test_calendar_original_reader_enforces_window_before_provider(tmp_path, monkeypatch):
    from test_receiver import rec
    monkeypatch.setenv('SOTTO_HOME', str(tmp_path))
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {
        'complete': True, 'revision': REVISION, 'google_sources': ['calendar']})
    api = rec._sotto_adapter('google_api')
    original = api.read_owner
    monkeypatch.setattr(api, '_owner_fetch', lambda *_a, **_k: pytest.fail('invalid window reached provider'))
    monkeypatch.setattr(api, 'read_owner', lambda op, args: original(op, args, lambda: SimpleNamespace(close=lambda: None)))
    for start, end in [('2026-01-01T00:00:00Z', '2026-11-01T00:00:00Z'), ('Monday', 'Tuesday')]:
        with pytest.raises(ValueError):
            backend.read(rec, 'sotto_calendar', {'start': start, 'end': end})


def test_stdio_survives_one_broken_http_response(access, monkeypatch, tmp_path, capsys):
    spec = importlib.util.spec_from_file_location('client_stdio_test', ADAPTER)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    private = tmp_path / 'adapter.json'
    private.write_text(json.dumps({'token': access[1]}))
    private.chmod(0o600)
    monkeypatch.setenv('SOTTO_CLIENT_MCP_URL', 'https://fixture.example/client-mcp')
    monkeypatch.setenv('SOTTO_CLIENT_MCP_CREDENTIAL_FILE', str(private))
    monkeypatch.setattr(adapter.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(
        (json.dumps(request('ping')) + '\n' + json.dumps(request('ping')) + '\n').encode())))
    calls = []
    def forward(*_):
        calls.append(True)
        if len(calls) == 1:
            raise http.client.IncompleteRead(b'PRIVATE', 100)
        return {'jsonrpc': '2.0', 'id': 1, 'result': {}}
    monkeypatch.setattr(adapter, 'forward', forward)
    assert adapter.main() == 0
    output = capsys.readouterr()
    assert len(output.out.splitlines()) == 2 and 'PRIVATE' not in output.out + output.err
    assert json.loads(output.out.splitlines()[1])['result'] == {}


def test_backend_preserves_bridge_coverage_and_checks_consent():
    calls = []
    authority = {'complete': True, 'revision': REVISION, 'bridge_sources': ['imessage']}
    r = SimpleNamespace(_runtime_source_authority=lambda: authority,
        RELAY=SimpleNamespace(mcp_call=lambda req, **kw: calls.append(req) or
              {'result': {'content': [{'type': 'text', 'text': '{"partial":true,"matches":[]}'}]}}),
        _source_context=lambda: SimpleNamespace(unwrap_tool_result=lambda v: json.loads(v['content'][0]['text'])))
    args = {'query': 'Cedar', 'sources': ['imessage'], 'since': 1, 'until': 2}
    assert backend.read(r, 'sotto_search_local', args) == {'partial': True, 'matches': []}
    assert calls[0]['params']['name'] == 'search_local'
    authority['bridge_sources'] = []
    with pytest.raises(RuntimeError):
        backend.read(r, 'sotto_search_local', args)
    assert len(calls) == 1


def test_backend_withholds_source_change():
    before = {'complete': True, 'revision': REVISION, 'bridge_sources': ['imessage']}
    snapshots = iter([before, {**before, 'revision': 'b' * 64}])
    r = SimpleNamespace(_runtime_source_authority=lambda **kwargs: next(snapshots),
        RELAY=SimpleNamespace(bridge_connected=lambda: True),
        DATA='/fixture', _load_shared_lib=lambda _: SimpleNamespace(learning_coverage=lambda *_: {'sources': []}))
    with pytest.raises(RuntimeError, match='changed'):
        backend.read(r, 'sotto_source_status', {})


def test_cloud_read_survives_mac_sleep_but_not_google_revocation(monkeypatch):
    monkeypatch.setenv('SOTTO_HOME', '/synthetic-home')
    before = {'complete': True, 'revision': REVISION, 'bridge_sources': ['imessage'], 'google_sources': ['gmail']}
    for after, allowed in [({**before, 'bridge_sources': []}, True),
                           ({**before, 'revision': 'b' * 64}, False),
                           ({**before, 'google_sources': []}, False)]:
        snapshots = iter([before, after])
        r = SimpleNamespace(_runtime_source_authority=lambda **kwargs: next(snapshots),
            _sotto_adapter=lambda _: SimpleNamespace(read_owner=lambda *_: {'source': 'gmail', 'items': []}))
        if allowed:
            assert backend.read(r, 'sotto_email_search', {'query': 'Cedar'})['items'] == []
        else:
            with pytest.raises(RuntimeError):
                backend.read(r, 'sotto_email_search', {'query': 'Cedar'})


def test_stdio_http_real_canonical_readers_and_no_writes(graph, monkeypatch):
    from test_receiver import rec
    root, _ = graph
    company_path = root / 'knowledge/companies/cedar.md'
    company_path.write_text(company_path.read_text() + '\n## Context\n' + 'Saved context. ' * 60 + 'Saved competitor evidence.\n')
    token, _ = mcp.create_grant(str(root), 'stdio-test', list(mcp.TOOLS), 1)
    private = root / 'credential.json'
    private.write_text(json.dumps({'token': token}))
    private.chmod(0o600)
    monkeypatch.setattr(rec, 'DATA', str(root))
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {
        'complete': True, 'revision': REVISION, 'bridge_sources': [], 'google_sources': ['gmail']})
    monkeypatch.setattr(rec, '_spawn_env', lambda _: {**os.environ, 'SOTTO_DATA': str(root)})
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *parts: str(PACK.joinpath(*parts)))
    before = tree(root)
    server = ThreadingHTTPServer(('127.0.0.1', 0), rec.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    turns = [request('initialize', protocolVersion='2025-11-25', capabilities={}, clientInfo={'name': 'fixture', 'version': '1'}),
             {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, request('tools/list'),
             request(name='sotto_person', arguments={'person': 'Dana', 'topic': 'clinic procurement'}),
             request(name='sotto_company', arguments={'company': 'Cedar'}),
             request(name='sotto_commitments', arguments={'topic': 'pricing'}),
             request(name='send_message', arguments={'text': 'hi'}),
             request(name='sotto_search_local', arguments={'query': 'Cedar', 'sources': ['imessage'],
                 'since': int(mcp.time.time()) - 3600, 'until': int(mcp.time.time())}),
             request(name='sotto_company', arguments={'company': 'Cedar', 'cursor': 'first'})]
    try:
        run = subprocess.run([sys.executable, str(ADAPTER)], input=''.join(json.dumps(turn) + '\n' for turn in turns),
            text=True, capture_output=True, timeout=30, env={**os.environ,
                'SOTTO_CLIENT_MCP_URL': f'http://127.0.0.1:{server.server_port}/client-mcp',
                'SOTTO_CLIENT_MCP_CREDENTIAL_FILE': str(private)})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert run.returncode == 0, run.stderr
    assert run.stderr == ''
    replies = [json.loads(line) for line in run.stdout.splitlines()]
    assert len(replies) == len(turns) - 1
    assert replies[0]['result']['serverInfo']['name'] == 'sotto'
    assert len(replies[1]['result']['tools']) == len(mcp.TOOLS)
    person = replies[2]['result']
    assert not person['isError'], person
    assert 'clinic procurement' in json.dumps(person['structuredContent'])
    company = replies[3]['result']['structuredContent']
    assert company['status'] == 'saved' and 'clinic tools' in company['company_knowledge']['cedar']
    assert len(company['company_knowledge']['cedar']) > 600
    assert 'Saved competitor evidence.' in company['company_knowledge']['cedar']
    assert company['next_cursor'] is None and company['truncated'] is False
    assert 'obligation_history' in replies[4]['result']['structuredContent']
    assert replies[5]['error']['code'] == -32602
    assert replies[6]['result']['isError']  # offline is not an empty search
    details = replies[7]['result']['structuredContent']
    assert 'Saved competitor evidence.' in details['company_knowledge']['cedar']
    assert details['next_cursor'] is None and details['truncated'] is False
    assert tree(root) == before


def test_grant_command_writes_private_credentials_without_printing_token(tmp_path):
    private = tmp_path / 'client.json'
    run = subprocess.run([sys.executable, mcp.__file__, '--data', str(tmp_path), 'grant',
        '--label', 'Claude fixture', '--tool', 'sotto_company', '--credential-file', str(private)],
        capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    token = json.loads(private.read_text())['token']
    assert token not in run.stdout + run.stderr
    assert private.stat().st_mode & 0o777 == 0o600
    assert mcp.authenticate(str(tmp_path), token)['tools'] == ['sotto_company']


def test_email_limit_error_is_actionable_and_never_echoes_query(access):
    seen = []
    result = mcp.dispatch(request(name='sotto_email_search', arguments={'query': 'PRIVATE', 'max': 40}),
                          access[2], lambda *args: seen.append(args))
    assert result['error']['code'] == -32602
    assert 'max: use an integer from 1 to 20' in result['error']['message']
    assert 'PRIVATE' not in json.dumps(result) and seen == []
    result = mcp.dispatch(request(name='sotto_email_search', arguments={'query': 'PRIVATE', 'max': 20}),
                          access[2], lambda *args: seen.append(args) or {'items': []})
    assert result['result']['isError'] is False and len(seen) == 1


def test_local_future_window_is_recoverable_not_source_outage(access, monkeypatch):
    monkeypatch.setattr(mcp.time, 'time', lambda: 1793660000)
    result = mcp.dispatch(request(name='sotto_search_local', arguments={
        'query': 'PRIVATE', 'sources': ['imessage'], 'since': 1762128000, 'until': 1793664000}),
        access[2], lambda *_: pytest.fail('invalid future window reached Bridge'))
    assert result['error']['code'] == -32602
    assert 'ending no later than now' in result['error']['message']
    assert 'PRIVATE' not in json.dumps(result)


def test_company_bad_cursor_is_recoverable_without_private_echo(access):
    result = mcp.dispatch(request(name='sotto_company', arguments={'company': 'Cedar', 'cursor': 'PRIVATE'}),
                          access[2], lambda *_: pytest.fail('invalid cursor reached reader'))
    assert result['error']['code'] == -32602
    assert 'next_cursor' in result['error']['message'] and 'PRIVATE' not in json.dumps(result)


def test_call_logs_are_content_free_and_distinguish_unavailable(access, capsys):
    root, token, grant = access
    body = request(name='sotto_email_search', arguments={'query': 'private subject'})
    body['id'] = 'private request id'
    response = mcp.handle(handler(token, body), 'POST', root,
                          lambda *_: {'status': 'partial', 'text': 'private email contents'})
    assert response[0] == 200
    raw = capsys.readouterr().err
    row = json.loads(raw)
    assert row['tool'] == 'sotto_email_search' and row['outcome'] == 'ok'
    assert row['read_status'] == 'partial' and row['duration_ms'] >= 0
    assert len(row['client']) == 12
    assert all(secret not in raw for secret in (token, grant['client_id'], 'private', 'fixture-client'))
    def failed(*_):
        raise ValueError('private backend error credential')
    mcp.handle(handler(token, body), 'POST', root, failed)
    row = json.loads(capsys.readouterr().err)
    assert row['outcome'] == 'unavailable' and row['http_status'] == 200


def test_call_logs_refusals_and_revocation_without_untrusted_fields(access, capsys):
    root, token, grant = access
    body = request('private method', name='private tool')
    mcp.handle(handler(token, body), 'POST', root, None)
    raw = capsys.readouterr().err
    assert 'private' not in raw
    assert json.loads(raw)['outcome'] == 'invalid_request'
    def revoke(*_):
        mcp.revoke(root, grant['client_id'])
        return {'private': 'withheld'}
    response = mcp.handle(handler(token, request(name='sotto_source_status')), 'POST', root, revoke)
    assert response[0] == 401
    assert json.loads(capsys.readouterr().err)['outcome'] == 'unauthorized'
    mcp.handle(handler(token), 'POST', root, None)
    row = json.loads(capsys.readouterr().err)
    assert row['http_status'] == 401 and 'client' not in row


@pytest.mark.parametrize("failure", [OSError, ValueError])
def test_logging_failure_does_not_break_a_read(access, monkeypatch, failure):
    root, token, _ = access
    def unavailable(*_, **__):
        raise failure('logging closed')
    monkeypatch.setattr(mcp, 'print', unavailable, raising=False)
    assert mcp.handle(handler(token), 'POST', root, None)[0] == 200


def test_recent_diagnostics_survive_discarded_stderr_and_are_bounded(access, monkeypatch):
    root, token, _ = access
    with mcp._RECENT_LOCK:
        mcp._RECENT.clear()
    monkeypatch.setattr(mcp, 'print', lambda *a, **k: None, raising=False)
    for _ in range(103):
        mcp.handle(handler(token, request(name='sotto_source_status')), 'POST', root,
                   lambda *_: {'status': 'partial', 'text': 'secret results'})
    result = mcp.diagnostics()
    assert result['limit'] == 100 and len(result['requests']) == 100
    assert result['retention'] == 'current_process'
    assert all(row['outcome'] == 'ok' and row['read_status'] == 'partial'
               and row['at'] >= result['started_at'] for row in result['requests'])
    assert token not in json.dumps(result) and 'secret results' not in json.dumps(result)
    result['requests'][0]['outcome'] = 'changed by caller'
    assert mcp.diagnostics()['requests'][0]['outcome'] == 'ok'


@pytest.mark.parametrize('name,args', [
    ('sotto_company', {'company': 'Cedar'}),
    ('sotto_person', {'person': 'Dana'}),
    ('sotto_commitments', {'topic': 'Cedar'}),
    ('sotto_commitments', {}),
])
@pytest.mark.parametrize('revocation', ['revision', 'incomplete'])
def test_canonical_reads_share_initial_check_but_recheck_after_read(graph, monkeypatch, name, args, revocation):
    from test_receiver import rec
    root, _ = graph
    monkeypatch.setattr(rec, 'DATA', str(root))
    monkeypatch.setattr(rec, '_spawn_env', lambda _: {**os.environ, 'SOTTO_DATA': str(root)})
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *parts: str(PACK.joinpath(*parts)))
    calls = []
    revoked = False
    def authority():
        calls.append(len(calls))
        changed = revoked and len(calls) == 2
        return {'complete': not (changed and revocation == 'incomplete'),
                'revision': 'b' * 64 if changed and revocation == 'revision' else REVISION,
                'bridge_sources': [], 'google_sources': ['gmail']}
    monkeypatch.setattr(rec, '_runtime_source_authority', authority)
    before = tree(root)
    assert isinstance(backend.read(rec, name, args), dict)
    assert len(calls) == 2
    assert tree(root) == before
    calls.clear()
    revoked = True
    with pytest.raises(RuntimeError, match='authority unavailable'):
        backend.read(rec, name, args)
    assert len(calls) == 2
    assert tree(root) == before


@pytest.mark.parametrize('name,arguments', [
    ('sotto_mac_context', {'sources': ['chrome', 'calls'], 'since_hours': 48}),
    ('sotto_note_get', {'id': 42}),
    ('sotto_contacts_search', {'query': 'Dana', 'max': 10}),
    ('sotto_contacts_get', {'resource_name': 'people/c123'}),
    ('sotto_granola_history', {'days': 7, 'meeting_id': 'meeting-123'}),
])
def test_extended_source_catalog_routes_only_explicit_grants(access, name, arguments):
    calls = []
    response = mcp.dispatch(request(name=name, arguments=arguments), access[2],
        lambda tool, args: calls.append((tool, args)) or {'status': 'partial'})
    assert response['result']['structuredContent']['status'] == 'partial'
    assert calls == [(name, arguments)]
    old_grant = {**access[2], 'tools': ['sotto_source_status', 'sotto_search_local']}
    denied = mcp.dispatch(request(name=name, arguments=arguments), old_grant,
        lambda *_: pytest.fail('existing grant gained a new reader'))
    assert denied['error']['code'] == -32602


@pytest.mark.parametrize('name,arguments', [
    ('sotto_mac_context', {}),
    ('sotto_mac_context', {'sources': []}),
    ('sotto_mac_context', {'sources': ['chrome', 'chrome']}),
    ('sotto_mac_context', {'sources': ['gmail']}),
    ('sotto_mac_context', {'sources': ['calls'], 'since_hours': 8761}),
    ('sotto_mac_context', {'sources': ['calls'], 'since_hours': True}),
    ('sotto_note_get', {'id': 0}),
    ('sotto_note_get', {'id': 42, 'path': '/private'}),
    ('sotto_contacts_search', {'query': 'Dana', 'max': 31}),
    ('sotto_contacts_get', {'resource_name': 'https://example.test'}),
    ('sotto_granola_history', {'days': 91}),
    ('sotto_granola_history', {'meeting_id': ' bad-id '}),
])
def test_extended_source_inputs_refuse_before_backend(access, name, arguments):
    result = mcp.dispatch(request(name=name, arguments=arguments), access[2],
        lambda *_: pytest.fail('invalid source arguments reached backend'))
    assert result['error']['code'] == -32602


def test_source_status_distinguishes_client_readers_from_source_availability(access):
    snapshot = {'available_sources': ['chrome', 'apple_notes', 'google_contacts', 'granola'],
                'local_search': {'status': 'known', 'enabled': False, 'history_days': 90},
                'bridge_online': True}
    grant = {**access[2], 'tools': ['sotto_source_status', 'sotto_search_local', 'sotto_mac_context']}
    value = mcp.dispatch(request(name='sotto_source_status'), grant,
        lambda *_: snapshot)['result']['structuredContent']
    assert value['source_readers']['chrome']['tools'] == ['sotto_mac_context']
    assert value['source_readers']['google_contacts']['available'] is True
    assert value['source_readers']['google_contacts']['tools'] == []
    assert value['source_readers']['granola']['tools'] == []
    assert value['source_readers']['apple_notes']['tools'] == ['sotto_mac_context', 'sotto_search_local']
    assert value['source_readers']['gmail']['available'] is False
    assert value['local_search']['enabled'] is False
    assert 'source_readers' not in snapshot  # backend authority object remains unchanged


def test_mac_catalog_covers_the_canonical_bridge_sources():
    from test_source_authority import rec
    assert set(mcp.MAC_SOURCES) == set(rec._load_shared_lib('source_catalog').SOURCE_FIELDS)
    assert set(mcp.TOOLS['sotto_mac_context']['inputSchema']['properties']['sources']['items']['enum']) == set(mcp.MAC_SOURCES)
