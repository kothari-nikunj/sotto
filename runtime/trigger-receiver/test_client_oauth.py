"""Hosted authorization through real HTTP, owner login, token exchange and MCP reads."""
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import urlencode, urlsplit, parse_qs

import pytest

import client_mcp as mcp
import client_oauth as oauth
import dashboard

BASE = 'https://sotto.example'
CALLBACK = 'https://chatgpt.com/connector_platform_oauth_redirect'
VERIFIER = 'v' * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip('=')


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_MCP_OAUTH', '1')
    monkeypatch.setenv('SOTTO_MCP_OAUTH_ORIGIN', BASE)
    monkeypatch.setitem(dashboard.HOOKS, 'data_root', lambda: str(tmp_path))
    monkeypatch.setitem(dashboard.HOOKS, 'setup_code', lambda: 'owner-test-code')
    def write(path, value):
        import os
        fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
    monkeypatch.setitem(dashboard.HOOKS, 'write_json', write)
    reads = []
    def backend(name, args):
        reads.append((name, args))
        return {'status': 'complete', 'sources': []}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.run('GET')
        def do_POST(self):
            self.run('POST')
        def run(self, method):
            path = urlsplit(self.path).path
            if path in oauth.PATHS:
                oauth.handle(self, method, str(tmp_path), dashboard)
            elif path == mcp.PATH:
                mcp.handle(self, method, str(tmp_path), backend,
                    oauth_authenticate=lambda token: oauth.authenticate(str(tmp_path), token, dashboard.session_by_digest),
                    oauth_challenge=oauth.challenge)
            else:
                dashboard.handle(self, method, path)
        def _send(self, code, body):
            oauth.reply(self, code, body)
        def _write(self, code, ctype, data):
            oauth.reply(self, code, {})
        def log_message(self, *_):
            pass
    server_http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server_http.serve_forever, daemon=True)
    thread.start()
    def call(path, method='GET', data=None, headers=None, form=False):
        head = dict(headers or {})
        if data is not None:
            head['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
            data = urlencode(data) if form else json.dumps(data)
        conn = http.client.HTTPConnection(*server_http.server_address, timeout=5)
        conn.request(method, path, data, head)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        conn.close()
        return result
    yield call, str(tmp_path), reads
    server_http.shutdown()
    server_http.server_close()
    thread.join()


def flow(server, *, approve=True):
    call, root, _ = server
    status, _, raw = call('/client-oauth/register', 'POST', {
        'client_name': 'Hosted fixture', 'redirect_uris': [CALLBACK], 'token_endpoint_auth_method': 'none'})
    assert status == 201
    client = json.loads(raw)['client_id']
    query = dict(client_id=client, redirect_uri=CALLBACK, response_type='code',
        code_challenge_method='S256', code_challenge=CHALLENGE, resource=BASE + '/client-mcp',
        state='client-secret-state', scope=oauth.SCOPE)
    status, headers, _ = call('/client-oauth/authorize?' + urlencode(query))
    assert status == 302 and headers['Location'] == '/app'
    pending_cookie = headers['Set-Cookie'].split(';')[0]
    # Sign in through the real existing dashboard. Login must resume THIS request.
    status, headers, _ = call('/app/login', 'POST', {'code': 'owner-test-code'},
                              {'Cookie': pending_cookie}, form=True)
    assert status == 302 and headers['Location'].startswith('/client-oauth/authorize?request=')
    owner_cookie = headers['Set-Cookie'].split(';')[0]
    resume = headers['Location']
    status, consent_headers, page = call(resume, headers={'Cookie': owner_cookie})
    assert consent_headers['Referrer-Policy'] == 'same-origin'
    assert "form-action 'self' https://chatgpt.com;" in consent_headers['Content-Security-Policy']
    assert status == 200 and 'Allow read-only access' in page
    rid = parse_qs(urlsplit(resume).query)['request'][0]
    rec = dashboard.session_by_digest(oauth.digest(owner_cookie.split('=', 1)[1]))
    consent = {'request': rid, 'csrf': rec['csrf'], 'decision': 'allow' if approve else 'deny'}
    status, headers, _ = call('/client-oauth/authorize', 'POST', consent,
        {'Cookie': owner_cookie, 'Origin': BASE}, form=True)
    assert status == 302
    result = {k: v[0] for k, v in parse_qs(urlsplit(headers['Location']).query).items()}
    assert result['state'] == query['state'] and result['iss'] == BASE
    return client, result, owner_cookie, consent


def token_request(client, code):
    return dict(grant_type='authorization_code', client_id=client, code=code,
        redirect_uri=CALLBACK, resource=BASE + '/client-mcp', code_verifier=VERIFIER)


def exchange(call, data):
    status, _, raw = call('/client-oauth/token', 'POST', data, form=True)
    return status, json.loads(raw)


def read(call, token):
    return call('/client-mcp', 'POST', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'sotto_source_status', 'arguments': {}}},
        {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'})


def test_complete_hosted_login_refresh_revoke_and_existing_client(server):
    call, root, reads = server
    status, headers, _ = call('/client-mcp')
    assert status == 401 and BASE + oauth.RESOURCE_METADATA in headers['WWW-Authenticate']
    resource = json.loads(call(oauth.RESOURCE_METADATA)[2])
    assert resource['resource'] == BASE + '/client-mcp'
    metadata = json.loads(call('/.well-known/oauth-authorization-server')[2])
    assert metadata['code_challenge_methods_supported'] == ['S256']
    assert metadata['authorization_response_iss_parameter_supported'] is True
    client, result, _, _ = flow(server)
    status, tokens = exchange(call, token_request(client, result['code']))
    assert status == 200
    assert read(call, tokens['access_token'])[0] == 200 and len(reads) == 1
    assert exchange(call, token_request(client, result['code']))[0] == 400
    refresh = dict(grant_type='refresh_token', client_id=client,
                   refresh_token=tokens['refresh_token'], resource=BASE + '/client-mcp')
    status, newer = exchange(call, refresh)
    assert status == 200 and newer['refresh_token'] != tokens['refresh_token']
    assert read(call, newer['access_token'])[0] == 200
    # Replay revokes the whole connection, including the newly issued access token.
    assert exchange(call, refresh)[0] == 400
    assert read(call, newer['access_token'])[0] == 401
    native, _ = mcp.create_grant(root, 'Existing native client', list(mcp.TOOLS), 1)
    assert read(call, native)[0] == 200
    # Tokens and authorization codes must never be stored in clear.
    from pathlib import Path
    contents = (Path(root) / 'config/mcp-clients/oauth.sqlite3').read_bytes()
    for val in (*tokens.values(), result['code']):
        if isinstance(val, str) and val.startswith(('sotto_oauth_', result['code'])):
            assert val.encode() not in contents


@pytest.mark.parametrize('field,value', [
    ('client_id', 'other-client'), ('resource', 'https://attacker.example/client-mcp'),
    ('redirect_uri', 'https://attacker.example/callback'), ('code_verifier', 'x' * 43),
    ('code_verifier', 'short'), ('scope', 'write')])
def test_bad_exchange_cannot_consume_valid_code(server, field, value):
    call, _, _ = server
    client, result, _, _ = flow(server)
    data = token_request(client, result['code'])
    assert exchange(call, {**data, field: value})[0] == 400
    assert exchange(call, data)[0] == 200


def test_denial_csrf_consent_replay_and_owner_revocation(server):
    call, _, reads = server
    client, result, cookie, consent = flow(server, approve=False)
    assert result['error'] == 'access_denied' and 'code' not in result
    assert call('/client-oauth/authorize', 'POST', consent, {'Cookie': cookie, 'Origin': BASE}, form=True)[0] == 400
    client, result, cookie, consent = flow(server)
    assert call('/client-oauth/authorize', 'POST', consent, {'Cookie': cookie, 'Origin': 'https://evil.example'}, form=True)[0] == 403
    status, tokens = exchange(call, token_request(client, result['code']))
    assert status == 200
    assert read(call, tokens['access_token'])[0] == 200
    sessions = dashboard._load_sessions()
    sessions.pop(oauth.digest(cookie.split('=', 1)[1]))
    dashboard._write_sessions(sessions)
    assert read(call, tokens['access_token'])[0] == 401
    assert len(reads) == 1


@pytest.mark.parametrize('uri', ['http://example.com/cb', 'https://user:pass@example.com',
    'https://example.com/cb#token', 'https://example.com/cb?code=x', 'javascript:alert(1)',
    'https://example.com\\@evil.example/cb', 'https://example.com/\r\nX:foo'])
def test_bad_registration_redirects(server, uri):
    call, _, _ = server
    assert call('/client-oauth/register', 'POST', {'redirect_uris': [uri]})[0] == 400


def test_disabling_oauth_and_wrong_origin_invalidates_access(server, monkeypatch):
    call, _, _ = server
    client, result, _, _ = flow(server)
    _, tokens = exchange(call, token_request(client, result['code']))
    monkeypatch.setenv('SOTTO_MCP_OAUTH_ORIGIN', 'https://different.example')
    assert read(call, tokens['access_token'])[0] == 401
    monkeypatch.setenv('SOTTO_MCP_OAUTH', '0')
    assert call(oauth.RESOURCE_METADATA)[0] == 404
    assert read(call, tokens['access_token'])[0] == 401


def test_no_host_header_trust_and_no_automatic_authority(server):
    call, _, reads = server
    raw = call(oauth.RESOURCE_METADATA, headers={'Host': 'attacker.example', 'X-Forwarded-Host': 'attacker.example'})[2]
    assert 'attacker.example' not in raw
    assert call('/client-oauth/authorize?client_id=made-up')[0] == 400
    assert call('/client-oauth/authorize?state=x&state=y')[0] == 400
    assert read(call, 'internal-root-bearer')[0] == 401
    assert reads == []


def test_owner_disconnect_and_oauth_revocation(server):
    call, root, _ = server
    client, result, cookie, consent = flow(server)
    _, tokens = exchange(call, token_request(client, result['code']))
    with oauth.store(root) as db:
        cid = db.execute('SELECT id FROM connections').fetchone()[0]
    page = call('/client-oauth/connections', headers={'Cookie': cookie})
    assert page[0] == 200 and 'Hosted fixture' in page[2]
    data = {'connection': cid, 'csrf': consent['csrf']}
    assert call('/client-oauth/connections', 'POST', data, {'Cookie': cookie}, form=True)[0] == 403
    assert read(call, tokens['access_token'])[0] == 200
    assert call('/client-oauth/connections', 'POST', data, {'Cookie': cookie, 'Origin': BASE}, form=True)[0] == 303
    assert read(call, tokens['access_token'])[0] == 401
    client, result, _, _ = flow(server)
    _, tokens = exchange(call, token_request(client, result['code']))
    assert call('/client-oauth/revoke', 'POST', {'client_id': client, 'token': tokens['refresh_token']}, form=True)[0] == 200
    assert read(call, tokens['access_token'])[0] == 401


def test_expiry_and_frozen_tool_scope(server, monkeypatch):
    call, root, reads = server
    client, result, _, _ = flow(server)
    with oauth.store(root) as db:
        db.execute('UPDATE codes SET expires=0')
    assert exchange(call, token_request(client, result['code']))[0] == 400
    client, result, _, _ = flow(server)
    _, tokens = exchange(call, token_request(client, result['code']))
    monkeypatch.setitem(mcp.TOOLS, 'future_tool', mcp.tool('Not approved'))
    grant = oauth.authenticate(root, tokens['access_token'], dashboard.session_by_digest)
    assert 'future_tool' not in grant['tools']
    with oauth.store(root) as db:
        db.execute("UPDATE tokens SET expires=0 WHERE kind='access'")
    assert read(call, tokens['access_token'])[0] == 401
    assert not reads


def test_concurrent_code_exchange_only_issues_once(server):
    from concurrent.futures import ThreadPoolExecutor
    call, _, _ = server
    client, result, _, _ = flow(server)
    data = token_request(client, result['code'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: exchange(call, data)[0], range(2)))
    assert sorted(results) == [200, 400]


def test_revoked_during_tool_read_withholds_content(server):
    call, root, _ = server
    client, result, _, _ = flow(server)
    _, tokens = exchange(call, token_request(client, result['code']))
    from test_client_mcp import handler, request
    def backend(*_):
        with oauth.store(root) as db:
            db.execute('UPDATE connections SET revoked=1')
        return {'private': 'must not leave'}
    h = handler(tokens['access_token'], request(name='sotto_source_status', arguments={}))
    response = mcp.handle(h, 'POST', root, backend,
        oauth_authenticate=lambda token: oauth.authenticate(root, token, dashboard.session_by_digest))
    assert response[0] == 401 and 'private' not in json.dumps(response)


def test_receiver_routes_discovery_and_mcp_challenge(server, monkeypatch):
    # Use the actual production HTTP handler as well as the small protocol fixture.
    from test_receiver import rec
    _, root, _ = server
    monkeypatch.setattr(rec, 'DATA', root)
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), rec.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        for path in (oauth.RESOURCE_METADATA, '/.well-known/oauth-authorization-server', '/client-mcp'):
            conn = http.client.HTTPConnection(*httpd.server_address, timeout=5)
            conn.request('GET', path)
            response = conn.getresponse()
            raw = response.read()
            assert response.status == (401 if path == '/client-mcp' else 200), raw
            if path == '/client-mcp':
                assert 'resource_metadata=' in response.getheader('WWW-Authenticate')
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join()


def test_consent_requires_matching_session_csrf_and_origin_before_issuing_code(server):
    call, root, _ = server
    client, _, cookie, _ = flow(server, approve=False)
    query = dict(client_id=client, redirect_uri=CALLBACK, response_type='code',
        code_challenge_method='S256', code_challenge=CHALLENGE, resource=BASE + '/client-mcp', state='new-state')
    status, _, page = call('/client-oauth/authorize?' + urlencode(query), headers={'Cookie': cookie})
    assert status == 200
    import re
    rid = re.search(r'name="request" value="([^"]+)"', page).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    good = {'request': rid, 'csrf': csrf, 'decision': 'allow'}
    for headers, fields, expected in [
        ({'Origin': BASE}, good, 401),
        ({'Cookie': cookie, 'Origin': BASE}, {**good, 'csrf': 'wrong'}, 403),
        ({'Cookie': cookie, 'Origin': 'https://attacker.example'}, good, 403)]:
        assert call('/client-oauth/authorize', 'POST', fields, headers, form=True)[0] == expected
    with oauth.store(root) as db:
        assert db.execute('SELECT count(*) FROM codes').fetchone()[0] == 0
    other = dashboard.mint_session()
    other_rec = dashboard.session_by_digest(oauth.digest(other))
    assert call('/client-oauth/authorize', 'POST', {**good, 'csrf': other_rec['csrf']},
        {'Cookie': 'sotto_session=' + other, 'Origin': BASE}, form=True)[0] == 400
    assert call('/client-oauth/authorize', 'POST', good, {'Cookie': cookie, 'Origin': BASE}, form=True)[0] == 302


def test_public_capacity_evicts_unapproved_records_not_live_connections(server):
    call, root, _ = server
    client, result, _, _ = flow(server)
    _, tokens = exchange(call, token_request(client, result['code']))
    with oauth.store(root) as db:
        for n in range(255):
            db.execute('INSERT INTO clients VALUES(?,?,?,?)', (str(n), 'Unapproved', '[]', 9999999999))
        new = oauth.register(db, {'redirect_uris': [CALLBACK]})
        assert db.execute('SELECT count(*) FROM clients').fetchone()[0] == 256
        assert db.execute('SELECT id FROM clients WHERE id=?', (client,)).fetchone()
        for n in range(256):
            db.execute('INSERT INTO requests VALUES(?,?,?,NULL)', (str(n), '{}', 9999999999))
        rid = oauth.request_authorization(db, dict(client_id=new['client_id'], redirect_uri=CALLBACK,
            response_type='code', code_challenge_method='S256', code_challenge=CHALLENGE,
            resource=BASE + '/client-mcp', state='fixture-state'), BASE)
        assert db.execute('SELECT id FROM requests WHERE id=?', (rid,)).fetchone()
        assert db.execute('SELECT count(*) FROM requests').fetchone()[0] == 256
    assert read(call, tokens['access_token'])[0] == 200


def test_oauth_reads_do_not_extend_browser_session_and_refresh_can_omit_resource(server):
    call, _, _ = server
    client, result, cookie, _ = flow(server)
    _, tokens = exchange(call, token_request(client, result['code']))
    key = oauth.digest(cookie.split('=', 1)[1])
    sessions = dashboard._load_sessions()
    sessions[key]['last_seen'] = dashboard._iso(oauth.time.time() - 2 * 86400)
    prior = sessions[key]['last_seen']
    dashboard._write_sessions(sessions)
    assert read(call, tokens['access_token'])[0] == 200
    assert dashboard._load_sessions()[key]['last_seen'] == prior
    assert exchange(call, dict(grant_type='refresh_token', client_id=client, refresh_token=tokens['refresh_token']))[0] == 200
    assert dashboard._load_sessions()[key]['last_seen'] == prior



def test_consent_refuses_evicted_client_and_orphans_remain_visible(server):
    call, root, _ = server
    client, _, cookie, _ = flow(server, approve=False)
    query = dict(client_id=client, redirect_uri=CALLBACK, response_type='code',
        code_challenge_method='S256', code_challenge=CHALLENGE, resource=BASE + '/client-mcp', state='new-state')
    _, _, page = call('/client-oauth/authorize?' + urlencode(query), headers={'Cookie': cookie})
    import re
    rid = re.search(r'name="request" value="([^"]+)"', page).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    with oauth.store(root) as db:
        db.execute('DELETE FROM clients WHERE id=?', (client,))
    assert call('/client-oauth/authorize', 'POST', {'request': rid, 'csrf': csrf, 'decision': 'allow'},
        {'Cookie': cookie, 'Origin': BASE}, form=True)[0] == 400
    client, _, cookie, _ = flow(server)
    with oauth.store(root) as db:
        db.execute('DELETE FROM clients WHERE id=?', (client,))
    assert 'Disconnected registration' in call('/client-oauth/connections', headers={'Cookie': cookie})[2]


def test_revoked_connections_are_removed_and_do_not_exhaust_slots(server):
    _, root, _ = server
    with oauth.store(root) as db:
        for n in range(128):
            db.execute('INSERT INTO connections VALUES(?,?,?,?,?,?,1)',
                       (str(n), 'old-client', 'old-session', BASE + '/client-mcp', 9999999999, '[]'))
            db.execute('INSERT INTO tokens VALUES(?,?,?,?,0)', (str(n), str(n), 'access', 9999999999))
    flow(server)
    with oauth.store(root) as db:
        assert db.execute('SELECT count(*) FROM connections').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM tokens').fetchone()[0] == 0



@pytest.mark.parametrize('value', ['https://host:abc', 'https://host:70000', 'https://[::1]', 'https://[broken'])
def test_invalid_operator_origin_disables_oauth(server, monkeypatch, value):
    call, _, _ = server
    monkeypatch.setenv('SOTTO_MCP_OAUTH_ORIGIN', value)
    assert call(oauth.RESOURCE_METADATA)[0] == 404
    assert read(call, 'not-a-token')[0] == 401


def test_refresh_capacity_requests_reauthorization_instead_of_endless_retries(server, monkeypatch):
    call, _, _ = server
    client, result, _, _ = flow(server)
    _, tokens = exchange(call, token_request(client, result['code']))
    monkeypatch.setattr(oauth, 'MAX_TOKEN_ROWS', 2)
    status, error = exchange(call, dict(grant_type='refresh_token', client_id=client, refresh_token=tokens['refresh_token']))
    assert status == 400 and error['error'] == 'invalid_grant'
    assert read(call, tokens['access_token'])[0] == 401


def test_optional_setup_returns_to_own_connection_page_without_granting_access(server):
    call, root, _ = server
    status, headers, _ = call('/client-oauth/connections')
    assert status == 302 and headers['Location'] == '/app'
    status, headers, _ = call('/app/login', 'POST', {'code': 'owner-test-code'},
                             {'Cookie': headers['Set-Cookie'].split(';')[0]}, form=True)
    assert status == 302 and headers['Location'] == '/client-oauth/connections'
    status, _, page = call(headers['Location'], headers={'Cookie': headers['Set-Cookie'].split(';')[0]})
    assert status == 200
    assert BASE + '/client-mcp' in page
    assert 'No API key or client secret' in page
    assert 'Nothing is shared until you approve' in page
    assert 'No skill pack is required' in page
    with oauth.store(root) as db:
        assert db.execute('SELECT count(*) FROM connections').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM tokens').fetchone()[0] == 0


def test_disabled_installation_explains_optional_connection_without_auth(server, monkeypatch):
    call, _, _ = server
    monkeypatch.setenv('SOTTO_MCP_OAUTH', '0')
    status, _, page = call('/client-oauth/connections')
    assert status == 200 and 'not enabled here' in page
    assert call('/client-oauth/register', 'POST', {'client_name': 'unused'})[0] == 404
