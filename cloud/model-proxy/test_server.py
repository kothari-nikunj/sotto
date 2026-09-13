import concurrent.futures
import hashlib
import io
import json
import threading
import time
import urllib.error
import urllib.request

import pytest
import server


class Response(io.BytesIO):
    headers = {'Content-Type': 'application/json'}


@pytest.fixture
def proxy(tmp_path):
    calls = []
    def upstream(req, **kwargs):
        calls.append(req)
        return Response(b'{"candidates":[],"usageMetadata":{"promptTokenCount":7,"candidatesTokenCount":3}}')
    tenants = [{'id': 'a', 'token_sha256': hashlib.sha256(b'a-token').hexdigest(), 'budget_cents': 200,
                'enabled': True, 'expires_at': time.time()+3600}]
    http = server.serve(('127.0.0.1', 0), tenants, 'upstream-only', tmp_path/'ledger.sqlite3', upstream)
    thread = threading.Thread(target=http.serve_forever, daemon=True);thread.start()
    yield http, calls
    http.shutdown();http.server_close();thread.join()


def post(http, path='/native/v1beta/models/gemini-3.8-flash:generateContent', token='a-token', body=None, headers=None):
    raw = json.dumps(body if body is not None else {'contents': [{'parts': [{'text': 'hello'}]}]}).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{http.server_port}'+path, data=raw,
                                 headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as r:
        return r.code, r.read()


def get(http, path, token='a-token'):
    req = urllib.request.Request(f'http://127.0.0.1:{http.server_port}'+path,
                                 headers={'Authorization': 'Bearer '+token})
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as response:
        return response.code, json.loads(response.read())


def test_reject_before_upstream_and_preserve_native_body(proxy):
    http,calls=proxy
    assert post(http, token='wrong')[0] == 401
    assert post(http,path='/native/v1beta/models/other:generateContent')[0] == 400
    assert post(http,path='/native/v1beta/models/gemini-3.8-flash:generateContent?key=bad')[0] == 400
    assert calls == []
    body={'contents':[{'parts':[{'inline_data':{'mime_type':'image/png','data':'AA=='}}]}],
          'tools':[{'google_search':{}}], 'generationConfig':{'responseSchema':{'type':'object'}}}
    assert post(http,body=body)[0] == 200
    assert json.loads(calls[0].data) == body
    assert calls[0].get_header('X-goog-api-key') == 'upstream-only'
    assert 'a-token' not in str(calls[0].headers)


def test_budget_reservation_serializes_concurrent_requests_and_survives_restart(proxy):
    http,calls=proxy
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        statuses=list(pool.map(lambda _:post(http)[0],range(2)))
    assert sorted(statuses) == [200,402]
    assert len(calls)==1
    reopened=server.Ledger(http.ledger.path)
    with pytest.raises(PermissionError):
        reopened.reserve('a',200,'native','gemini-3.8-flash')
    with reopened.connect() as db:
        assert db.execute('SELECT tenant,status,input_tokens,output_tokens FROM calls').fetchone()==('a',200,7,3)


def test_expired_credential_rejected(proxy):
    http,calls=proxy
    http.tenants[0]['expires_at']=0
    assert post(http)[0]==401 and not calls


def test_credential_expiring_during_body_read_is_rejected_before_reservation(tmp_path):
    calls = []
    raw = json.dumps({'contents': [{'parts': [{'text': 'hello'}]}]}).encode()
    tenant = {'id': 'a', 'token_sha256': hashlib.sha256(b'a-token').hexdigest(),
              'budget_cents': 200, 'enabled': True, 'expires_at': time.time() + 3600}

    class ExpireAfterBodyRead:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def read(self, size=-1):
            body = self.wrapped.read(size)
            tenant['expires_at'] = 0
            return body

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    class FakeConnection:
        def settimeout(self, _): pass

    class FakeServer:
        tenants = [tenant]
        ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
        upstream_key = 'upstream-only'

        @staticmethod
        def open_upstream(req, **kwargs):
            calls.append(req)
            raise OSError('upstream must not be reached')

    handler = object.__new__(server.Handler)
    handler.connection = FakeConnection()
    handler.server = FakeServer()
    handler.path = '/native/v1beta/models/gemini-3.8-flash:generateContent'
    handler.headers = {'Authorization': 'Bearer a-token', 'Content-Length': str(len(raw))}
    handler.rfile = ExpireAfterBodyRead(io.BytesIO(raw))
    responses = []
    handler.respond = lambda status, body: responses.append((status, body))

    handler.do_POST()

    assert responses[0][0] == 401
    assert calls == []
    with handler.server.ledger.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 0


def test_credential_expiring_while_waiting_for_ledger_is_rejected(tmp_path, monkeypatch):
    now = [100]
    monkeypatch.setattr(server.time, 'time', lambda: now[0])
    tenant = {'id': 'a', 'token_sha256': hashlib.sha256(b'a-token').hexdigest(),
              'budget_cents': 200, 'enabled': True, 'expires_at': 150}
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    connect = ledger.connect

    class AdvanceClockAfterBegin:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __enter__(self):
            self.wrapped.__enter__()
            return self

        def __exit__(self, *args):
            return self.wrapped.__exit__(*args)

        def execute(self, statement, parameters=()):
            result = self.wrapped.execute(statement, parameters)
            if statement == 'BEGIN IMMEDIATE':
                now[0] = 200
            return result

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    monkeypatch.setattr(ledger, 'connect', lambda: AdvanceClockAfterBegin(connect()))
    with pytest.raises(server.AuthenticationError):
        ledger.reserve('a', 200, 'native', 'gemini-3.8-flash',
                       credential=(tenant, tenant['token_sha256']))
    with connect() as db:
        assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 0


def test_lease_renewal_uses_independent_auth_and_survives_restart(proxy):
    http, calls = proxy
    tenant = http.tenants[0]
    tenant['renewal_token_sha256'] = hashlib.sha256(b'renew-fixture').hexdigest()
    body = {'tenant_id': 'a', 'token_sha256': tenant['token_sha256']}
    assert post(http, '/v1/lease/renew', body=body)[0] == 401
    assert post(http, '/v1/lease/renew', token='renew-fixture', body={**body, 'tenant_id': 'other'})[0] == 401
    assert post(http, '/v1/lease/renew', token='renew-fixture', body={**body, 'token_sha256': 'wrong'})[0] == 401
    status, result = post(http, '/v1/lease/renew', token='renew-fixture', body=body)
    assert status == 200 and not calls
    expiry = json.loads(result)['expires_at']
    assert expiry > time.time() + 29 * 86400
    http.ledger = server.Ledger(http.ledger.path)
    tenant['expires_at'] = 0
    assert http.ledger.expiry(tenant) == expiry
    assert post(http)[0] == 200
    tenant['enabled'] = False
    assert post(http)[0] == 401
    assert post(http, '/v1/lease/renew', token='renew-fixture', body=body)[0] == 401
    tenant['enabled'] = True
    tenant['token_sha256'] = hashlib.sha256(b'rotated-model').hexdigest()
    assert post(http)[0] == 401


def test_chat_uses_separate_fixed_surface(proxy):
    http,calls=proxy
    body={'model':'gemini-3.8-flash','messages':[{'role':'user','content':'hello'}]}
    assert post(http,path='/openai/v1/chat/completions',body=body)[0]==200
    assert calls[0].full_url=='https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    assert calls[0].get_header('Authorization')=='Bearer upstream-only'


def test_shared_cache_and_multiple_candidates_cannot_bypass_tenant_budget(proxy):
    http, calls = proxy
    assert post(http, body={'cachedContent': 'cachedContents/foreign-tenant'})[0] == 400
    assert post(http, body={'generationConfig': {'candidateCount': 8}})[0] == 400
    assert post(http, path='/openai/v1/chat/completions', body={
        'model': 'gemini-3.8-flash', 'messages': [], 'n': 8})[0] == 400
    assert calls == []


def test_native_thinking_tokens_are_included_in_output_usage(tmp_path):
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    call = ledger.reserve('a', 200, 'native', 'gemini-3.8-flash')
    ledger.finish(call, 200, {'promptTokenCount': 10, 'candidatesTokenCount': 5, 'thoughtsTokenCount': 20})
    with ledger.connect() as db:
        assert db.execute('SELECT input_tokens,output_tokens FROM calls').fetchone() == (10, 25)


def test_settlement_keeps_ambiguous_and_native_calls_reserved(tmp_path, monkeypatch):
    monkeypatch.setattr(server.time, 'time', lambda: server.PRICING_START + 86400)
    ledger = server.Ledger(tmp_path / 'settlement.sqlite3')
    chat = ledger.reserve('a', 1000, 'chat', 'gemini-3.8-flash')
    ledger.finish(chat, 200, {'prompt_tokens': 20000, 'completion_tokens': 1})
    native = ledger.reserve('a', 1000, 'native', 'gemini-3.8-flash')
    ledger.finish(native, 200, {'promptTokenCount': 20000})
    failed = ledger.reserve('a', 1000, 'chat', 'gemini-3.8-flash')
    ledger.finish(failed, 502, {'prompt_tokens': 20})
    missing = ledger.reserve('a', 1000, 'chat', 'gemini-3.8-flash')
    ledger.finish(missing, 200, {})
    reopened = server.Ledger(ledger.path)
    with reopened.connect() as db:
        assert db.execute('SELECT allowance FROM calls ORDER BY id').fetchall() == [(27,), (200,), (200,), (200,)]
    assert server.settled_chat_allowance('chat', 'gemini-3.8-flash', server.PRICING_END, 200, 1) == 200


def test_chat_rejects_unpriced_features():
    for extra in [{'service_tier': 'priority'}, {'tools': [{'type': 'google_search'}]},
                  {'messages': [{'content': [{'type': 'input_audio'}]}]}]:
        with pytest.raises(ValueError):
            server.route('/openai/v1/chat/completions', {'model': 'gemini-3.8-flash', **extra})


def test_unlimited_tenant_keeps_metering_and_authentication(proxy):
    http, calls = proxy
    http.tenants[0]['budget_cents'] = None
    assert post(http)[0] == 200
    assert post(http)[0] == 200
    assert post(http, token='wrong')[0] == 401
    with http.ledger.connect() as db:
        assert db.execute('SELECT COUNT(*),SUM(allowance) FROM calls').fetchone() == (2, 400)
    http.tenants[0]['budget_cents'] = 200
    status, body = post(http)
    assert status == 402
    assert json.loads(body)['error']['code'] == 'sotto_budget_exhausted'
    assert len(calls) == 2


def test_background_finite_budget_requirement_rejects_unlimited_before_upstream(proxy):
    http, calls = proxy
    http.tenants[0]['budget_cents'] = None
    status, body = post(http, headers={'X-Sotto-Require-Finite-Budget': 'true'})
    assert status == 402
    assert json.loads(body)['error']['code'] == 'sotto_budget_exhausted'
    assert calls == []
    with http.ledger.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 0


def test_background_budget_capability_is_authenticated_content_free_and_tracks_same_ledger(proxy):
    http, calls = proxy
    assert get(http, '/v1/capabilities/background-budget', token='wrong')[0] == 401
    status, capability = get(http, '/v1/capabilities/background-budget')
    assert status == 200 and capability == {
        'version': 1, 'finite': True, 'remaining_cents': 200, 'can_admit': True}
    assert calls == []
    assert post(http)[0] == 200
    assert get(http, '/v1/capabilities/background-budget')[1] == {
        'version': 1, 'finite': True, 'remaining_cents': 0, 'can_admit': False}
    http.tenants[0]['budget_cents'] = None
    assert get(http, '/v1/capabilities/background-budget')[1] == {
        'version': 1, 'finite': False, 'remaining_cents': None, 'can_admit': False}


@pytest.mark.parametrize('change', [
    {'budget_cents': '200'}, {'budget_cents': True}, {'budget_cents': -1},
    {'enabled': 1}, {'expires_at': None}, {'expires_at': float('nan')}, {'expires_at': float('inf')}, {'token_sha256': 'not-a-hash'},
])
def test_invalid_tenant_policy_cannot_become_an_implicit_bypass(tmp_path, change):
    tenant = {'id': 'a', 'token_sha256': hashlib.sha256(b'a-token').hexdigest(),
              'budget_cents': 200, 'enabled': True, 'expires_at': time.time() + 3600}
    tenant.update(change)
    with pytest.raises(ValueError):
        server.serve(('127.0.0.1', 0), [tenant], 'upstream', tmp_path / 'ledger.sqlite3')


def test_tenant_ids_and_bearers_are_unique_and_unlimited_is_explicit(tmp_path):
    tenant = {'id': 'a', 'token_sha256': hashlib.sha256(b'a-token').hexdigest(),
              'budget_cents': None, 'enabled': True, 'expires_at': time.time() + 3600}
    server.validate_tenants([tenant])
    with pytest.raises(ValueError, match='unique'):
        server.validate_tenants([tenant, {**tenant, 'id': 'b'}])


def test_settlement_backfill_is_a_one_time_startup_migration(tmp_path, monkeypatch):
    path = tmp_path / 'ledger.sqlite3'
    ledger = server.Ledger(path)
    with ledger.connect() as db:
        db.execute('DELETE FROM proxy_migrations WHERE name=?', (server.SETTLEMENT_MIGRATION,))
        db.execute('INSERT INTO calls(tenant,route,model,created,allowance,status,input_tokens) '
                   'VALUES(?,?,?,?,?,?,?)',
                   ('a', 'chat', 'gemini-3.8-flash', server.PRICING_START + 1, 200, 200, 1))
    server.Ledger(path)
    with ledger.connect() as db:
        assert db.execute('SELECT allowance FROM calls').fetchone()[0] < 200
        assert db.execute('SELECT COUNT(*) FROM proxy_migrations WHERE name=?',
                          (server.SETTLEMENT_MIGRATION,)).fetchone()[0] == 1
        db.execute('UPDATE calls SET allowance=199')
    server.Ledger(path)
    with ledger.connect() as db:
        assert db.execute('SELECT allowance FROM calls').fetchone()[0] == 199


def test_rate_limit_is_atomic_persistent_and_separate_from_spend(proxy, monkeypatch):
    http, calls = proxy
    http.tenants[0]['budget_cents'] = None
    monkeypatch.setattr(server, 'REQUESTS_PER_WINDOW', 2)
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        statuses = list(pool.map(lambda _: post(http)[0], range(4)))
    assert sorted(statuses) == [200, 200, 429, 429]
    assert len(calls) == 2
    reopened = server.Ledger(http.ledger.path)
    with pytest.raises(server.RateLimitError):
        reopened.reserve('a', None, 'native', 'gemini-3.8-flash')
    with reopened.connect() as db:
        db.execute('UPDATE calls SET created=0')
    assert reopened.reserve('a', None, 'native', 'gemini-3.8-flash')


def test_upstream_receives_only_validated_canonical_json(proxy):
    http, calls = proxy
    raw = b'{"contents": [], "generationConfig": {"candidateCount": 100, "candidateCount": 1}}'
    req = urllib.request.Request(f'http://127.0.0.1:{http.server_port}/native/v1beta/models/gemini-3.8-flash:generateContent',
        data=raw, headers={'Authorization': 'Bearer a-token', 'Content-Type': 'application/json'})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(req)
    assert error.value.code == 400 and not calls
    body = {'contents': [{'parts': [{'text': 'hello'}]}]}
    assert post(http, body=body)[0] == 200
    assert calls[0].data == json.dumps(body, allow_nan=False, separators=(',', ':')).encode()
