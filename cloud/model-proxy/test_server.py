import concurrent.futures
import calendar
import hashlib
import http.client
import io
import json
import math
import threading
import time
import types
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


@pytest.mark.parametrize('method', ['POST', 'GET'])
@pytest.mark.parametrize('credential', ['valid', 'expired', 'disabled', 'unknown'])
def test_authentication_reads_expiry_only_for_matching_enabled_tenant(proxy, monkeypatch, method, credential):
    http, calls = proxy
    matching = http.tenants[0]
    http.tenants[:0] = [
        {'id': f'unrelated-{index}',
         'token_sha256': hashlib.sha256(f'unrelated-token-{index}'.encode()).hexdigest(),
         'budget_cents': 200, 'enabled': True, 'expires_at': time.time() + 3600}
        for index in range(100)
    ]
    if credential == 'expired':
        matching['expires_at'] = 0
    elif credential == 'disabled':
        matching['enabled'] = False
    token = 'unknown-token' if credential == 'unknown' else 'a-token'
    expiry = http.ledger.expiry
    checked = []

    def record_expiry(tenant):
        checked.append(tenant['id'])
        return expiry(tenant)

    monkeypatch.setattr(http.ledger, 'expiry', record_expiry)
    if method == 'POST':
        status, _ = post(http, token=token)
    else:
        status, _ = get(http, '/v1/capabilities/background-budget', token=token)

    accepted = credential == 'valid'
    assert status == (200 if accepted else 401)
    assert checked == (['a'] if credential in ('valid', 'expired') else [])
    assert len(calls) == (1 if accepted and method == 'POST' else 0)
    with http.ledger.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == len(calls)


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


def test_chat_joins_only_contiguous_leading_system_text_before_upstream(proxy):
    http, calls = proxy
    messages = [
        {'role': 'system', 'content': 'Owner voice rule.'},
        {'role': 'system', 'content': 'Skill index and boundaries.'},
        {'role': 'system', 'content': 'Current context is unavailable.'},
        {'role': 'user', 'content': 'Just the wording.'},
        {'role': 'assistant', 'content': 'A draft.'},
        {'role': 'system', 'content': 'Later system content is not leading.'},
    ]
    tools = [{'type': 'function', 'function': {
        'name': 'skill', 'description': 'Activate a skill',
        'parameters': {'type': 'object', 'properties': {}}}}]
    body = {'model': 'gemini-3.8-flash', 'messages': messages,
            'tools': tools, 'tool_choice': 'auto'}
    assert post(http, path='/openai/v1/chat/completions', body=body)[0] == 200
    upstream = json.loads(calls[0].data)
    assert upstream == {**body, 'messages': [
        {'role': 'system', 'content': 'Owner voice rule.\n\nSkill index and boundaries.\n\nCurrent context is unavailable.'},
        *messages[3:],
    ]}
    assert body['messages'] == messages


def test_chat_one_leading_system_message_is_unchanged(proxy):
    http, calls = proxy
    body = {'model': 'gemini-3.8-flash', 'messages': [
        {'role': 'system', 'content': 'Owner voice rule.'},
        {'role': 'user', 'content': 'Just the wording.'},
    ]}
    assert post(http, path='/openai/v1/chat/completions', body=body)[0] == 200
    assert json.loads(calls[0].data) == body


@pytest.mark.parametrize('leading', [
    [{'role': 'system', 'content': 'Voice'}, {'role': 'system', 'content': None}],
    [{'role': 'system', 'content': 'Voice'}, {'role': 'system', 'content': [{'type': 'text', 'text': 'More'}]}],
    [{'role': 'system', 'content': 'Voice'}, {'role': 'system', 'content': 'More', 'name': 'other'}],
])
def test_chat_rejects_ambiguous_leading_system_merge_before_contact(proxy, leading):
    http, calls = proxy
    body = {'model': 'gemini-3.8-flash', 'messages': [*leading, {'role': 'user', 'content': 'Hello'}]}
    assert post(http, path='/openai/v1/chat/completions', body=body)[0] == 400
    assert calls == []
    with http.ledger.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0] == 0


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


@pytest.mark.parametrize('model', sorted(server.MODELS))
def test_complete_native_receipt_uses_model_tariff_and_all_prompt_tokens(model):
    usage = {'promptTokenCount': 20000, 'cachedContentTokenCount': 19000,
             'candidatesTokenCount': 500, 'thoughtsTokenCount': 500,
             'totalTokenCount': 21000, 'serviceTier': 'standard',
             'promptTokensDetails': [{'modality': 'TEXT', 'tokenCount': 20000}]}
    allowance = server.native_receipt_allowance(model, server.PRICING_START + 1, 200, usage)
    rates = server.PRICES[model]
    assert allowance == min(200, math.ceil(100 * (20000 * rates[0] + 1000 * rates[2]) / 1_000_000))
    assert allowance < 200
    without_thoughts = {key: value for key, value in usage.items() if key != 'thoughtsTokenCount'}
    assert server.native_receipt_allowance(model, server.PRICING_START + 1, 200, without_thoughts) == allowance


@pytest.mark.parametrize('change', [
    {'promptTokenCount': None}, {'promptTokenCount': True}, {'promptTokenCount': -1},
    {'promptTokenCount': 1048577}, {'candidatesTokenCount': None},
    {'candidatesTokenCount': False}, {'candidatesTokenCount': -1},
    {'candidatesTokenCount': server.MAX_OUTPUT + 1}, {'totalTokenCount': None},
    {'totalTokenCount': 999}, {'totalTokenCount': 1001},
    {'totalTokenCount': 1000 + server.MAX_OUTPUT + 1},
    {'thoughtsTokenCount': -1}, {'thoughtsTokenCount': True},
    {'thoughtsTokenCount': 2}, {'cachedContentTokenCount': 1001},
    {'cachedContentTokenCount': False}, {'serviceTier': 'priority'},
    {'groundingTokenCount': 1}, {'toolUsePromptTokenCount': 1},
    {'toolUsePromptTokensDetails': []},
    {'promptTokensDetails': [{'modality': 'AUDIO', 'tokenCount': 1}]},
    {'candidatesTokensDetails': [{'modality': 'TEXT', 'tokenCount': True}]},
    {'cacheTokensDetails': [{'modality': 'TEXT', 'tokenCount': -1}]},
])
def test_native_receipt_rejects_missing_inconsistent_or_unpriced_usage(change):
    usage = {'promptTokenCount': 1000, 'candidatesTokenCount': 10,
             'thoughtsTokenCount': 10, 'totalTokenCount': 1020}
    usage.update(change)
    assert server.native_receipt_allowance('gemini-3.8-flash', server.PRICING_START + 1, 200, usage) is None


def test_native_receipt_respects_tariff_and_status_bounds():
    usage = {'promptTokenCount': 1000, 'candidatesTokenCount': 10, 'totalTokenCount': 1020}
    assert server.native_receipt_allowance('gemini-3.8-flash', server.PRICING_START - 1, 200, usage) is None
    assert server.native_receipt_allowance('gemini-3.8-flash', server.PRICING_END, 200, usage) is None
    assert server.native_receipt_allowance('gemini-3.8-flash', server.PRICING_START + 1, 502, usage) is None
    assert server.native_receipt_allowance('unknown', server.PRICING_START + 1, 200, usage) is None


def test_native_receipt_settles_prospectively_and_monthly_rollover_uses_admission_time(tmp_path, monkeypatch):
    def utc(year, month, day):
        return calendar.timegm((year, month, day, 0, 0, 0))
    now = [utc(2026, 10, 31) + 86390]
    monkeypatch.setattr(server, 'time', types.SimpleNamespace(time=lambda: now[0], gmtime=time.gmtime))
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    policy = {'budget_period': 'calendar_month', 'budget_started_at': utc(2026, 10, 15)}
    usage = {'promptTokenCount': 1000, 'candidatesTokenCount': 10, 'totalTokenCount': 1020}
    call = ledger.reserve('a', 200, 'native', 'gemini-3.5-flash-lite', **policy)
    assert ledger.budget_capability('a', 200, **policy)['remaining_cents'] == 0
    now[0] = utc(2026, 11, 1)
    ledger.finish(call, 200, usage, proven_native_text=True,
                  native_text_request_eligible=True, native_text_response_eligible=True)
    assert ledger.budget_capability('a', 200, **policy)['remaining_cents'] == 200
    with ledger.connect() as db:
        allowance, output_tokens, usage_json = db.execute(
            'SELECT allowance,output_tokens,usage_json FROM calls').fetchone()
    assert allowance == 1
    assert output_tokens == 20
    stored_usage = json.loads(usage_json)
    assert stored_usage['allowance_policy'] == 'native_text_receipt_v2'
    assert stored_usage['native_text_settlement_reason'] == 'receipt_settled'
    assert stored_usage['reasoning_tokens'] == 10
    assert stored_usage['billed_output_tokens'] == 20
    assert stored_usage['estimated_token_cost'] == pytest.approx((1000 * .30 + 20 * 2.50) / 1_000_000)
    assert ledger.reserve('a', 200, 'native', 'gemini-3.5-flash-lite', **policy)
    assert ledger.budget_capability('a', 200, **policy)['remaining_cents'] == 0


@pytest.mark.parametrize('usage', [
    {'promptTokenCount': 20000, 'candidatesTokenCount': 2, 'totalTokenCount': False},
    {'promptTokenCount': 20000, 'candidatesTokenCount': 2, 'totalTokenCount': 9999999},
    {'promptTokenCount': 20000, 'candidatesTokenCount': 2,
     'totalTokenCount': 20002, 'cachedContentTokenCount': 20001},
    {'promptTokenCount': 20000, 'candidatesTokenCount': 2,
     'cachedContentTokenCount': 20001},
])
def test_malformed_native_receipt_keeps_full_reservation(tmp_path, monkeypatch, usage):
    monkeypatch.setattr(server.time, 'time', lambda: server.PRICING_START + 1)
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    call = ledger.reserve('a', 200, 'native', 'gemini-3.8-flash')
    ledger.finish(call, 200, usage, proven_native_text=True,
                  native_text_request_eligible=True, native_text_response_eligible=True)
    with ledger.connect() as db:
        allowance, usage_json = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    assert allowance == 200
    assert 'allowance_policy' not in json.loads(usage_json)


def test_sparse_native_usage_keeps_prior_conservative_ceiling_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(server.time, 'time', lambda: server.PRICING_START + 1)
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    call = ledger.reserve('a', 200, 'native', 'gemini-3.8-flash')
    ledger.finish(call, 200, {'promptTokenCount': 20000, 'candidatesTokenCount': 2},
                  proven_native_text=True, native_text_request_eligible=True,
                  native_text_response_eligible=True)
    with ledger.connect() as db:
        allowance, usage_json = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    assert allowance == server.settled_text_allowance('gemini-3.8-flash', server.PRICING_START + 1, 200, 20000)
    assert json.loads(usage_json)['allowance_policy'] == 'native_text_v1'


@pytest.mark.parametrize('extra,expected', [
    ({}, 1),
    ({'serviceTier': 'priority'}, 200),
    ({'unexpectedCount': 1}, 200),
    ({'promptTokensDetails': [{'modality': 'IMAGE', 'tokenCount': 1}]}, 200),
    ({'toolUsePromptTokensDetails': []}, 200),
])
def test_native_http_receipt_requires_safe_usage_features(proxy, extra, expected):
    http, _ = proxy
    http.open_upstream = lambda *_args, **_kwargs: Response(json.dumps({
        'candidates': [{'content': {'parts': [{'text': 'hello'}]}, 'finishReason': 'STOP'}],
        'usageMetadata': {'promptTokenCount': 1000, 'candidatesTokenCount': 10,
                          'totalTokenCount': 1020, **extra}}).encode())
    assert post(http)[0] == 200
    with http.ledger.connect() as db:
        allowance, usage_json = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    assert allowance == expected
    assert ('allowance_policy' in json.loads(usage_json)) == (expected < 200)


@pytest.mark.parametrize('finish_reason', ['STOP', 'MAX_TOKENS'])
@pytest.mark.parametrize('brief', [False, True])
def test_proven_text_native_settles_prospectively_and_admits_again(proxy, finish_reason, brief):
    http, calls = proxy
    http.tenants[0]['budget_cents'] = 227  # the next admission still reserves its full 200c
    http.open_upstream = lambda *_args, **_kwargs: Response(json.dumps({
        'candidates': [{'content': {'role': 'model', 'parts': [{'text': '{}'}]},
                        'finishReason': finish_reason}],
        'usageMetadata': {'promptTokenCount': 20000, 'candidatesTokenCount': 2,
                          'thoughtsTokenCount': 25}}).encode())
    body = {'contents': [{'parts': [{'text': 'hello'}]}],
            'systemInstruction': {'parts': [{'text': 'Answer as JSON.'}]},
            'generationConfig': {'maxOutputTokens': 8192, 'temperature': .4,
                                 'response_mime_type': 'application/json',
                                 'thinkingConfig': {'thinkingLevel': 'low'},
                                 'responseSchema': {'type': 'OBJECT'}}}
    headers = ({'X-Sotto-Workload': 'brief_extract', 'X-Sotto-Operation': 'a' * 64,
                'X-Sotto-Run': 'b' * 32, 'X-Sotto-Attempt': '1'} if brief else {})
    assert post(http, body=body, headers=headers)[0] == 200
    with http.ledger.connect() as db:
        allowance, usage_json = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    assert allowance == server.settled_text_allowance('gemini-3.8-flash', time.time(), 200, 20000)
    assert allowance == 27
    assert json.loads(usage_json)['allowance_policy'] == 'native_text_v1'
    assert json.loads(usage_json)['native_text_settlement_reason'] == 'settled'
    assert post(http, body=body, headers=headers)[0] == 200
    assert len(calls) == 0  # the replacement upstream is the only provider used
    reopened = server.Ledger(http.ledger.path)
    with reopened.connect() as db:
        assert db.execute('SELECT COUNT(*),SUM(allowance) FROM calls').fetchone() == (2, 54)


@pytest.mark.parametrize('part', [
    {'text': '{}', 'thoughtSignature': 'c2lnbmF0dXJl'},
    {'text': '{}', 'thought': False, 'thoughtSignature': 'c2lnbmF0dXJl'},
    {'text': 'reasoning', 'thought': True},
])
def test_native_text_annotations_settle_without_loosening_request_proof(proxy, part):
    http, _ = proxy
    http.open_upstream = lambda *_args, **_kwargs: Response(json.dumps({
        'candidates': [{'content': {'role': 'model', 'parts': [part]},
                        'finishReason': 'STOP'}],
        'usageMetadata': {'promptTokenCount': 7, 'candidatesTokenCount': 2,
                          'thoughtsTokenCount': 2}}).encode())
    assert post(http)[0] == 200
    with http.ledger.connect() as db:
        allowance, usage_json = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    assert allowance < 200
    assert json.loads(usage_json)['native_text_settlement_reason'] == 'settled'
    assert not server.native_text_request({'contents': [{'parts': [part]}]})

@pytest.mark.parametrize('usage', [
    {'candidatesTokenCount': 100, 'thoughtsTokenCount': 500000},
    {'candidatesTokenCount': server.MAX_OUTPUT + 1},
    {'candidatesTokenCount': server.MAX_OUTPUT, 'thoughtsTokenCount': 1},
    {},
    {'candidatesTokenCount': '3'},
    {'candidatesTokenCount': 3, 'thoughtsTokenCount': 1.5},
])
def test_native_text_output_missing_or_above_ceiling_keeps_full_allowance(proxy, usage):
    http, _ = proxy
    http.tenants[0]['budget_cents'] = 10000
    http.open_upstream = lambda *_args, **_kwargs: Response(json.dumps({
        'candidates': [{'content': {'parts': [{'text': 'x'}]}, 'finishReason': 'STOP'}],
        'usageMetadata': {'promptTokenCount': 1000, **usage}}).encode())
    body = {'contents': [{'parts': [{'text': 'hello'}]}],
            'generationConfig': {'maxOutputTokens': 8192, 'thinkingConfig': {'thinkingLevel': 'high'}}}
    assert post(http, body=body)[0] == 200
    with http.ledger.connect() as db:
        allowance, usage_json = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    assert allowance == 200
    assert 'allowance_policy' not in json.loads(usage_json)
    assert json.loads(usage_json)['native_text_settlement_reason'] == 'output_unbounded'


def test_chat_output_missing_or_above_ceiling_keeps_full_allowance(tmp_path, monkeypatch):
    monkeypatch.setattr(server.time, 'time', lambda: server.PRICING_START + 86400)
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    for usage in ({'prompt_tokens': 20000}, {'prompt_tokens': 20000, 'completion_tokens': server.MAX_OUTPUT + 1},
                  {'prompt_tokens': 20000, 'completion_tokens': server.MAX_OUTPUT}):
        ledger.finish(ledger.reserve('a', None, 'chat', 'gemini-3.8-flash'), 200, usage)
    with ledger.connect() as db:
        assert db.execute('SELECT allowance FROM calls ORDER BY id').fetchall() == [(200,), (200,), (200,)]


def test_google_chat_receipt_settles_reported_total_including_thinking_and_keeps_counts(proxy):
    http, calls = proxy
    http.tenants[0]['budget_cents'] = 201
    http.open_upstream = lambda *_args, **_kwargs: Response(json.dumps({
        'id': 'response-fixture', 'object': 'chat.completion', 'model': 'gemini-3.8-flash',
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': 'Done.'}}],
        'usage': {'prompt_tokens': 9000, 'completion_tokens': 20, 'total_tokens': 9120,
                  'completion_tokens_details': {'reasoning_tokens': 100}},
    }).encode())
    body = {'model': 'gemini-3.8-flash', 'messages': [{'role': 'user', 'content': 'hello'}],
            'max_completion_tokens': 512}
    assert post(http, path='/openai/v1/chat/completions', body=body)[0] == 200
    with http.ledger.connect() as db:
        allowance, output, usage_json = db.execute(
            'SELECT allowance,output_tokens,usage_json FROM calls').fetchone()
    usage = json.loads(usage_json)
    assert allowance == 1
    assert output == 120
    assert usage['allowance_policy'] == 'chat_text_receipt_v1'
    assert usage['chat_text_settlement_reason'] == 'receipt_settled'
    assert usage['reported_total_tokens'] == 9120
    assert usage['visible_output_tokens'] == 20
    assert usage['reasoning_tokens'] == 100
    assert usage['billed_output_tokens'] == 120
    assert usage['usage_completeness'] == 'partial'  # cache details were not reported
    assert post(http, path='/openai/v1/chat/completions', body=body)[0] == 200
    assert len(calls) == 0  # the replacement upstream is the only provider used


def test_google_chat_receipt_prices_reported_cache_without_erasing_thinking(tmp_path, monkeypatch):
    monkeypatch.setattr(server.time, 'time', lambda: server.PRICING_START + 86400)
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    call = ledger.reserve('a', None, 'chat', 'gemini-3.8-flash')
    ledger.finish(call, 200, {'prompt_tokens': 100000, 'completion_tokens': 100,
                              'total_tokens': 101000,
                              'prompt_tokens_details': {'cached_tokens': 90000},
                              'completion_tokens_details': {'reasoning_tokens': 900}},
                  proven_chat_text=True)
    with ledger.connect() as db:
        allowance, usage_json = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    usage = json.loads(usage_json)
    assert allowance == 2
    assert usage['estimated_token_cost'] == pytest.approx(.018)
    assert usage['reported_cached_tokens'] == 90000
    assert usage['reported_reasoning_tokens'] == 900
    assert usage['usage_completeness'] == 'complete'


@pytest.mark.parametrize('usage', [
    {'prompt_tokens': 9000, 'completion_tokens': 20},
    {'prompt_tokens': 9000, 'completion_tokens': 20, 'total_tokens': 9019},
    {'prompt_tokens': 9000, 'completion_tokens': 20, 'total_tokens': 9100,
     'completion_tokens_details': {'reasoning_tokens': 79}},
    {'prompt_tokens': 9000, 'completion_tokens': 20, 'total_tokens': 9000 + server.MAX_OUTPUT + 1},
    {'prompt_tokens': 9000, 'completion_tokens': 20, 'total_tokens': True},
    {'prompt_tokens': 9000, 'completion_tokens': 20, 'total_tokens': 9100,
     'prompt_tokens_details': {'cached_tokens': 9001}},
    {'prompt_tokens': 9000, 'completion_tokens': 20, 'total_tokens': 9100,
     'completion_tokens_details': {'audio_tokens': 1}},
])
def test_google_chat_bad_or_unpriced_receipt_keeps_full_allowance(tmp_path, monkeypatch, usage):
    monkeypatch.setattr(server.time, 'time', lambda: server.PRICING_START + 86400)
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    call = ledger.reserve('a', None, 'chat', 'gemini-3.8-flash')
    ledger.finish(call, 200, usage, proven_chat_text=True)
    with ledger.connect() as db:
        allowance, usage_json = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    assert allowance == 200
    assert 'allowance_policy' not in json.loads(usage_json)


def test_google_chat_receipt_rejects_unknown_request_or_response_shape(proxy):
    http, _ = proxy
    http.tenants[0]['budget_cents'] = 600
    reply = {
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': 'Done.'}}],
        'usage': {'prompt_tokens': 9000, 'completion_tokens': 20, 'total_tokens': 9100},
        'unknown_paid_feature': {},
    }
    http.open_upstream = lambda *_args, **_kwargs: Response(json.dumps(reply).encode())
    base = {'model': 'gemini-3.8-flash', 'messages': [{'role': 'user', 'content': 'hello'}]}
    assert post(http, path='/openai/v1/chat/completions', body=base)[0] == 200
    del reply['unknown_paid_feature']
    assert post(http, path='/openai/v1/chat/completions', body={**base, 'extra_body': {}})[0] == 200
    with http.ledger.connect() as db:
        assert db.execute('SELECT allowance FROM calls ORDER BY id').fetchall() == [(200,), (200,)]
        stored=[json.loads(row[0]) for row in db.execute('SELECT usage_json FROM calls ORDER BY id')]
        assert [row['chat_text_settlement_reason'] for row in stored]==['response_ineligible','request_ineligible']
        assert 'unknown_paid_feature' not in json.dumps(stored)


@pytest.mark.parametrize('extra', [
    {'tools': [{'google_search': {}}]},
    {'contents': [{'parts': [{'inline_data': {'mime_type': 'image/png', 'data': 'AA=='}}]}]},
    {'contents': [{'parts': [{'fileData': {'fileUri': 'gs://example'}}]}]},
    {'contents': [{'parts': [{'functionResponse': {'name': 'lookup', 'response': {}}}]}]},
    {'generationConfig': {'responseModalities': ['AUDIO']}},
    {'toolConfig': {}},
    {'cachedContent': ''},
])
def test_native_billable_or_unknown_request_shapes_keep_full_allowance(proxy, extra):
    http, _ = proxy
    http.open_upstream = lambda *_args, **_kwargs: Response(json.dumps({
        'candidates': [{'content': {'parts': [{'text': 'hello'}]}, 'finishReason': 'STOP'}],
        'usageMetadata': {'promptTokenCount': 7, 'candidatesTokenCount': 3}}).encode())
    body = {'contents': [{'parts': [{'text': 'hello'}]}], **extra}
    assert post(http, body=body)[0] == 200
    with http.ledger.connect() as db:
        allowance, usage_json = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    assert allowance == 200
    assert json.loads(usage_json)['native_text_settlement_reason'] == 'request_ineligible'


@pytest.mark.parametrize('reply', [
    {'candidates': [{'content': {'parts': [{'text': 'hello'}]}, 'finishReason': 'STOP',
                     'groundingMetadata': {}}],
     'usageMetadata': {'promptTokenCount': 7}},
    {'candidates': [{'content': {'parts': [{'inlineData': {'data': 'AA=='}}]},
                     'finishReason': 'STOP'}],
     'usageMetadata': {'promptTokenCount': 7}},
    {'candidates': [{'content': {'parts': [{'text': 'hello'}]}, 'finishReason': 'STOP'}],
     'usageMetadata': {}},
    {'candidates': [{'content': {'parts': [{'text': 'hello'}]}, 'finishReason': 'STOP'}],
     'usageMetadata': {'promptTokenCount': True}},
    {'candidates': [{'content': {'parts': [{'text': 'hello'}]}, 'finishReason': 'STOP'}],
     'usageMetadata': {'promptTokenCount': 1048577}},
    {'candidates': [{'content': {'parts': [{'text': 'hello'}]}}],
     'usageMetadata': {'promptTokenCount': 7}},
    {'candidates': [{'content': {'parts': [{'text': 'hello'}]}, 'finishReason': 'OTHER'}],
     'usageMetadata': {'promptTokenCount': 7}},
    {'candidates': [{'content': {'parts': [{'text': 'hello'}]}, 'finishReason': 'STOP'}],
     'usage': {'prompt_tokens': 7}},
    {'candidates': [{'content': {'parts': [{'text': 'hello'}]}, 'finishReason': 'STOP'}],
     'usageMetadata': {'promptTokenCount': 7, 'toolUsePromptTokenCount': 1}},
    {'candidates': [{'content': {'parts': [{'text': 'hello'}]}, 'finishReason': 'STOP'}],
     'usageMetadata': {'promptTokenCount': 7, 'groundingTokenCount': 1}},
    {'candidates': [{'content': {'parts': [{'text': 'hello', 'inlineData': {}}]},
                     'finishReason': 'STOP'}],
     'usageMetadata': {'promptTokenCount': 7}},
    {'candidates': [{'content': {'parts': [{'text': 'hello', 'thoughtSignature': 42}]},
                     'finishReason': 'STOP'}],
     'usageMetadata': {'promptTokenCount': 7}},
])
def test_native_ambiguous_response_or_usage_keeps_full_allowance(proxy, reply):
    http, _ = proxy
    http.open_upstream = lambda *_args, **_kwargs: Response(json.dumps(reply).encode())
    assert post(http)[0] == 200
    with http.ledger.connect() as db:
        allowance, usage_json = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    assert allowance == 200
    assert json.loads(usage_json)['native_text_settlement_reason'] == 'response_ineligible'


@pytest.mark.parametrize('mode', ['malformed', 'truncated', 'stream'])
def test_native_incomplete_or_streamed_reply_keeps_full_allowance(proxy, monkeypatch, mode):
    http, _ = proxy
    valid = json.dumps({'candidates': [{'content': {'parts': [{'text': 'hello'}]},
                                        'finishReason': 'STOP'}],
                        'usageMetadata': {'promptTokenCount': 7}}).encode()
    if mode == 'truncated':
        monkeypatch.setattr(server, 'MAX_BODY', 200)
        body = valid[:-1] + b' ' * 200 + b'}'
    elif mode == 'stream':
        body = b'data: ' + valid + b'\n\n'
    else:
        body = valid[:-1]

    class NativeResponse(Response):
        headers = {'Content-Type': 'text/event-stream' if mode == 'stream' else 'application/json'}

    http.open_upstream = lambda *_args, **_kwargs: NativeResponse(body)
    assert post(http)[0] == 200
    with http.ledger.connect() as db:
        assert db.execute('SELECT allowance FROM calls').fetchone()[0] == 200


def test_native_settlement_requires_current_tariff_and_new_request_proof(tmp_path, monkeypatch):
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    monkeypatch.setattr(server.time, 'time', lambda: server.PRICING_END)
    expired = ledger.reserve('a', None, 'native', 'gemini-3.8-flash')
    ledger.finish(expired, 200, {'promptTokenCount': 7, 'candidatesTokenCount': 1}, proven_native_text=True,
                  native_text_request_eligible=True, native_text_response_eligible=True)
    monkeypatch.setattr(server.time, 'time', lambda: server.PRICING_START + 1)
    legacy = ledger.reserve('a', None, 'native', 'gemini-3.8-flash')
    ledger.finish(legacy, 200, {'promptTokenCount': 7})
    alias_only = ledger.reserve('a', None, 'native', 'gemini-3.8-flash')
    ledger.finish(alias_only, 200, {'prompt_tokens': 7}, proven_native_text=True)
    failed = ledger.reserve('a', None, 'native', 'gemini-3.8-flash')
    ledger.finish(failed, 502, {'promptTokenCount': 7}, proven_native_text=True)
    with ledger.connect() as db:
        assert [row[0] for row in db.execute('SELECT allowance FROM calls ORDER BY id')] == [200] * 4
        first_usage = json.loads(db.execute('SELECT usage_json FROM calls WHERE id=?', (expired,)).fetchone()[0])
        assert first_usage['native_text_settlement_reason'] == 'tariff_unavailable'
    with server.Ledger(ledger.path).connect() as db:
        assert [row[0] for row in db.execute('SELECT allowance FROM calls ORDER BY id')] == [200] * 4


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
        assert db.execute('SELECT allowance FROM calls ORDER BY id').fetchall() == [(200,)] * 4
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
    assert json.loads(body)['error']['period'] == 'lifetime'
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
        'version': 1, 'finite': True, 'remaining_cents': 200, 'can_admit': True,
        'supported_native_models': sorted(server.MODELS)}
    assert calls == []
    assert post(http)[0] == 200
    assert get(http, '/v1/capabilities/background-budget')[1] == {
        'version': 1, 'finite': True, 'remaining_cents': 0, 'can_admit': False,
        'supported_native_models': sorted(server.MODELS)}
    http.tenants[0]['budget_cents'] = None
    assert get(http, '/v1/capabilities/background-budget')[1] == {
        'version': 1, 'finite': False, 'remaining_cents': None, 'can_admit': False,
        'supported_native_models': sorted(server.MODELS)}


def test_monthly_allowance_uses_activation_then_utc_calendar_months(tmp_path, monkeypatch):
    def utc(year, month, day):
        return calendar.timegm((year, month, day, 0, 0, 0))

    now = [utc(2026, 10, 20)]
    monkeypatch.setattr(server, 'time', types.SimpleNamespace(time=lambda: now[0], gmtime=time.gmtime))
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    activation = utc(2026, 10, 15)
    with ledger.connect() as db:
        db.executemany('INSERT INTO calls(tenant,route,model,created,allowance) VALUES(?,?,?,?,?)', [
            ('a', 'native', 'gemini-3.8-flash', utc(2026, 9, 20), 200),
            ('a', 'native', 'gemini-3.8-flash', utc(2026, 10, 10), 200),
            ('b', 'native', 'gemini-3.8-flash', utc(2026, 10, 16), 200),
        ])
    policy = {'budget_period': 'calendar_month', 'budget_started_at': activation}
    assert ledger.budget_capability('a', 200, **policy)['remaining_cents'] == 200
    assert ledger.reserve('a', 200, 'native', 'gemini-3.8-flash', **policy)
    assert ledger.budget_capability('a', 200, **policy)['remaining_cents'] == 0
    with pytest.raises(PermissionError):
        ledger.reserve('a', 200, 'native', 'gemini-3.8-flash', **policy)
    assert ledger.budget_capability('b', 200)['remaining_cents'] == 0

    now[0] = utc(2026, 11, 1)
    assert ledger.budget_capability('a', 200, **policy)['remaining_cents'] == 200
    assert ledger.reserve('a', 200, 'native', 'gemini-3.8-flash', **policy)
    assert ledger.budget_capability('a', 200, **policy)['remaining_cents'] == 0
    assert ledger.budget_capability('a', 1000)['remaining_cents'] == 200
    with ledger.connect() as db:
        assert db.execute('SELECT COUNT(*),SUM(allowance) FROM calls WHERE tenant=?', ('a',)).fetchone() == (4, 800)


def test_monthly_allowance_serializes_concurrent_admission(tmp_path):
    ledger = server.Ledger(tmp_path / 'ledger.sqlite3')
    policy = {'budget_period': 'calendar_month', 'budget_started_at': time.time() - 1}
    def attempt(_):
        try:
            ledger.reserve('a', 200, 'native', 'gemini-3.8-flash', **policy)
            return 'admitted'
        except PermissionError:
            return 'exhausted'
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ['admitted', 'exhausted']
    assert ledger.budget_capability('a', 200, **policy)['remaining_cents'] == 0


def test_monthly_capability_and_denial_keep_authentication(proxy):
    http, calls = proxy
    http.tenants[0].update(budget_period='calendar_month', budget_started_at=time.time() - 1)
    assert get(http, '/v1/capabilities/background-budget', token='wrong')[0] == 401
    assert get(http, '/v1/capabilities/background-budget')[1]['remaining_cents'] == 200
    assert post(http)[0] == 200
    status, body = post(http)
    assert status == 402
    assert 'next UTC month' in json.loads(body)['error']['message']
    assert json.loads(body)['error']['period'] == 'calendar_month'
    assert len(calls) == 1


@pytest.mark.parametrize('change', [
    {'budget_cents': '200'}, {'budget_cents': True}, {'budget_cents': -1},
    {'enabled': 1}, {'expires_at': None}, {'expires_at': float('nan')}, {'expires_at': float('inf')}, {'token_sha256': 'not-a-hash'},
    {'budget_period': 'weekly'}, {'budget_started_at': time.time() - 100},
    {'budget_period': 'calendar_month'},
    {'budget_period': 'calendar_month', 'budget_started_at': 0},
    {'budget_period': 'calendar_month', 'budget_started_at': float('nan')},
    {'budget_period': 'calendar_month', 'budget_started_at': time.time() + 3600},
    {'budget_period': 'calendar_month', 'budget_started_at': time.time() - 100, 'budget_cents': None},
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
    for bad in ({**tenant, 'deployment': 'owner@example.com'}, {**tenant, 'application': {'x': 1}},
                {**tenant, 'deployment': 'x' * 65}):
        with pytest.raises(ValueError):
            server.validate_tenants([bad])
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
    monkeypatch.setattr(server, 'REQUESTS_PER_WINDOW', 3)  # one tenant may use two of three
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


def test_internal_workload_headers_stay_at_the_proxy(proxy):
    http, calls = proxy
    headers = {'X-Sotto-Workload': 'notification', 'X-Sotto-Operation': 'a' * 64,
               'X-Sotto-Run': 'b' * 32, 'X-Sotto-Attempt': '1'}
    assert post(http, headers=headers)[0] == 200
    assert not any(name.lower().startswith('x-sotto') for name in calls[0].headers)
    with http.ledger.connect() as db:
        metadata = json.loads(db.execute('SELECT metadata_json FROM calls').fetchone()[0])
    assert metadata['workload'] == 'notification' and metadata['operation_id'] == 'a' * 64


def test_shared_active_admission_protects_foreground(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'MAX_ACTIVE_CALLS', 3)
    monkeypatch.setattr(server, 'MAX_ACTIVE_BACKGROUND_CALLS', 2)
    ledger = server.Ledger(tmp_path / 'shared.sqlite3')
    background = {'workload': 'triage', 'input_chars': 10}
    foreground = {'workload': 'brief_extract', 'input_chars': 10}
    first = ledger.reserve('a', None, 'native', 'gemini-3.8-flash', metadata=background)
    second = ledger.reserve('b', None, 'native', 'gemini-3.8-flash', metadata=background)
    with pytest.raises(server.RateLimitError, match='shared provider concurrent') as denied:
        ledger.reserve('c', None, 'native', 'gemini-3.8-flash', metadata=background)
    assert 3 <= denied.value.retry_after <= 7
    ledger.reserve('c', None, 'native', 'gemini-3.8-flash', metadata=foreground)
    with pytest.raises(server.RateLimitError, match='concurrent'):
        ledger.reserve('a', None, 'chat', 'gemini-3.8-flash', metadata={'workload': 'chat'})
    ledger.finish(first, 502, {})
    assert ledger.reserve('a', None, 'chat', 'gemini-3.8-flash', metadata={'workload': 'chat'})
    with pytest.raises(server.RateLimitError, match='concurrent'):
        ledger.reserve('a', None, 'native', 'gemini-3.8-flash', metadata=background)
    assert second


def test_opening_another_ledger_handle_preserves_live_active_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'MAX_ACTIVE_CALLS', 1)
    ledger = server.Ledger(tmp_path / 'shared.sqlite3')
    active = ledger.reserve('a', None, 'native', 'gemini-3.8-flash',
                            metadata={'workload': 'chat', 'input_chars': 1})
    # A rolling replacement or second process can open the shared ledger while this request lives.
    replacement = server.Ledger(ledger.path)
    with pytest.raises(server.RateLimitError, match='shared provider concurrent'):
        replacement.reserve('b', None, 'native', 'gemini-3.8-flash',
                            metadata={'workload': 'chat', 'input_chars': 1})
    ledger.release(active)
    assert replacement.reserve('b', None, 'native', 'gemini-3.8-flash',
                               metadata={'workload': 'chat', 'input_chars': 1})


def test_one_tenant_leaves_shared_room_for_another_tenant(tmp_path):
    ledger = server.Ledger(tmp_path / 'shared.sqlite3')
    chat = {'workload': 'chat', 'input_chars': 10}
    held = [ledger.reserve('a', None, 'chat', 'gemini-3.8-flash', metadata=chat)
            for _ in range(server.MAX_ACTIVE_CALLS * server.TENANT_SHARE_PERCENT // 100)]
    with pytest.raises(server.RateLimitError, match='tenant concurrent'):
        ledger.reserve('a', None, 'chat', 'gemini-3.8-flash', metadata=chat)
    assert ledger.reserve('b', None, 'chat', 'gemini-3.8-flash', metadata=chat)
    for call in held:
        ledger.release(call)
    with ledger.connect() as db:
        db.execute('UPDATE calls SET active_until=NULL')
    for _ in range(server.REQUESTS_PER_WINDOW * server.TENANT_SHARE_PERCENT // 100 - len(held)):
        ledger.release(ledger.reserve('a', None, 'chat', 'gemini-3.8-flash', metadata=chat))
    with pytest.raises(server.RateLimitError, match='tenant request rate'):
        ledger.reserve('a', None, 'chat', 'gemini-3.8-flash', metadata=chat)
    assert ledger.reserve('b', None, 'chat', 'gemini-3.8-flash', metadata=chat)


def test_request_larger_than_a_window_is_refused_permanently(proxy, tmp_path):
    ledger = server.Ledger(tmp_path / 'size.sqlite3')
    with pytest.raises(server.RequestTooLargeError):
        ledger.reserve('a', None, 'native', 'gemini-3.8-flash',
                       metadata={'workload': 'triage', 'input_chars': server.BACKGROUND_INPUT_CHARS_PER_WINDOW * server.TENANT_SHARE_PERCENT // 100 + 1})
    # The tenant share applies to the first request too; its boundary remains admissible.
    assert ledger.reserve('a', None, 'native', 'gemini-3.8-flash',
                          metadata={'workload': 'triage', 'input_chars': server.BACKGROUND_INPUT_CHARS_PER_WINDOW * server.TENANT_SHARE_PERCENT // 100})
    http, calls = proxy
    status = post(http, body={'contents': [{'parts': [{'text': 'x' * (server.BACKGROUND_INPUT_CHARS_PER_WINDOW + 1)}]}]})[0]
    assert status == 413 and not calls


def test_first_tenant_volume_request_leaves_shared_room(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'INPUT_CHARS_PER_WINDOW', 100)
    monkeypatch.setattr(server, 'BACKGROUND_INPUT_CHARS_PER_WINDOW', 100)
    ledger = server.Ledger(tmp_path / 'shared.sqlite3')
    background = {'workload': 'triage', 'input_chars': 75}
    ledger.reserve('a', None, 'native', 'gemini-3.8-flash', metadata=background)
    ledger.reserve('b', None, 'native', 'gemini-3.8-flash',
                   metadata={'workload': 'triage', 'input_chars': 25})
    with pytest.raises(server.RateLimitError, match='shared provider input volume'):
        ledger.reserve('b', None, 'native', 'gemini-3.8-flash',
                       metadata={'workload': 'triage', 'input_chars': 1})


def test_tenant_volume_413_ceiling_covers_foreground_and_background(tmp_path):
    cases = [
        ('brief_extract', server.INPUT_CHARS_PER_WINDOW),
        ('triage', server.BACKGROUND_INPUT_CHARS_PER_WINDOW),
    ]
    for index, (workload, shared_limit) in enumerate(cases):
        ceiling = shared_limit * server.TENANT_SHARE_PERCENT // 100
        ledger = server.Ledger(tmp_path / f'volume-{index}.sqlite3')
        with pytest.raises(server.RequestTooLargeError):
            ledger.reserve('a', None, 'native', 'gemini-3.8-flash',
                           metadata={'workload': workload, 'input_chars': ceiling + 1})
        assert ledger.reserve('a', None, 'native', 'gemini-3.8-flash',
                              metadata={'workload': workload, 'input_chars': ceiling})


def test_shared_rate_and_input_volume_leave_foreground_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'REQUESTS_PER_WINDOW', 5)
    monkeypatch.setattr(server, 'BACKGROUND_REQUESTS_PER_WINDOW', 3)
    monkeypatch.setattr(server, 'MAX_ACTIVE_CALLS', 10)
    monkeypatch.setattr(server, 'MAX_ACTIVE_BACKGROUND_CALLS', 10)
    monkeypatch.setattr(server, 'BACKGROUND_INPUT_CHARS_PER_WINDOW', 100)
    monkeypatch.setattr(server, 'INPUT_CHARS_PER_WINDOW', 200)
    ledger = server.Ledger(tmp_path / 'shared.sqlite3')
    for tenant in ('a', 'b'):
        ledger.reserve(tenant, None, 'native', 'gemini-3.8-flash',
                       metadata={'workload': 'triage', 'input_chars': 45})
    with pytest.raises(server.RateLimitError, match='shared provider input volume') as denied:
        ledger.reserve('b', None, 'native', 'gemini-3.8-flash',
                       metadata={'workload': 'triage', 'input_chars': 20})
    assert 45 <= denied.value.retry_after <= 60
    ledger.reserve('b', None, 'chat', 'gemini-3.8-flash',
                   metadata={'workload': 'chat', 'input_chars': 100})
    with pytest.raises(server.RateLimitError, match='shared provider input volume'):
        ledger.reserve('a', None, 'chat', 'gemini-3.8-flash',
                       metadata={'workload': 'chat', 'input_chars': 11})


def test_known_chat_route_is_attributed_without_optional_headers(proxy):
    http, _ = proxy
    assert post(http, path='/openai/v1/chat/completions',
                body={'model': 'gemini-3.8-flash', 'messages': [{'role': 'user', 'content': 'hello'}]})[0] == 200
    with http.ledger.connect() as db:
        metadata = json.loads(db.execute('SELECT metadata_json FROM calls').fetchone()[0])
    assert metadata['workload'] == 'chat'
    assert metadata['input_chars'] >= sum(metadata['context_chars'].values())


@pytest.mark.parametrize('lane,payload', [
    ('chat', {'messages': [{'role': 'assistant', 'content': None,
                           'tool_calls': [{'function': {'arguments': 'x' * 1000}}]}]}),
    ('native', {'contents': [], 'tools': [{'functionDeclarations': [{'description': 'x' * 1000}]}]}),
])
def test_admission_input_size_covers_tool_calls_and_native_tools(lane, payload):
    metadata = server.request_metadata({}, payload, lane)
    assert metadata['input_chars'] > 1000
    assert metadata['input_chars'] > sum(metadata['context_chars'].values())


def test_failed_upstream_releases_shared_concurrency_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(server, 'MAX_ACTIVE_CALLS', 1)
    calls = []
    def upstream(req, **kwargs):
        calls.append(req)
        if len(calls) == 1:
            raise OSError('offline injected failure')
        return Response(b'{"candidates":[],"usageMetadata":{"promptTokenCount":7,"candidatesTokenCount":3}}')
    tenants = [{'id': 'a', 'token_sha256': hashlib.sha256(b'a-token').hexdigest(),
                'budget_cents': None, 'enabled': True, 'expires_at': time.time() + 3600}]
    http = server.serve(('127.0.0.1', 0), tenants, 'upstream-only', tmp_path / 'ledger.sqlite3', upstream)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        assert post(http)[0] == 502
        assert post(http)[0] == 200
        with http.ledger.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM calls WHERE active_until IS NOT NULL').fetchone()[0] == 0
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


@pytest.mark.parametrize('status,request_ok,response_ok,proven,valid_usage,reason,expected', [
    (502, True, False, False, False, 'upstream_failure', 200),
    (200, False, True, False, True, 'request_ineligible', 200),
    (200, True, False, False, True, 'response_ineligible', 200),
    (200, True, True, True, False, 'receipt_unavailable', 200),
    (200, True, True, True, True, 'receipt_settled', 1),
])
def test_chat_settlement_diagnostic_is_fixed_and_does_not_change_accounting(tmp_path, monkeypatch,
        status, request_ok, response_ok, proven, valid_usage, reason, expected):
    monkeypatch.setattr(server.time, 'time', lambda: server.PRICING_START + 86400)
    ledger=server.Ledger(tmp_path/'ledger.sqlite3')
    call=ledger.reserve('a', None, 'chat', 'gemini-3.8-flash')
    usage={'prompt_tokens':9000,'completion_tokens':20,'total_tokens':9120,
           'completion_tokens_details':{'reasoning_tokens':100}} if valid_usage else {}
    ledger.finish(call,status,usage,proven_chat_text=proven,
        chat_text_request_eligible=request_ok,chat_text_response_eligible=response_ok)
    with ledger.connect() as db:
        allowance,raw=db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    counts=json.loads(raw)
    assert allowance==expected
    assert counts['chat_text_settlement_reason']==reason
    assert set(k for k in counts if k.startswith('chat_text_'))=={'chat_text_settlement_reason'}


def signed_chat_call():
    return {'id':'fixture-call','type':'function','function':{'name':'fixture_read','arguments':'{}'},
            'extra_content':{'google':{'thought_signature':'opaque-fixture-signature'}}}


@pytest.mark.parametrize('extra', [None, {}, {'google':None}, {'google':{}},
    {'google':{'thought_signature':''}}, {'google':{'thought_signature':1}},
    {'google':{'thought_signature':False}}, {'google':{'thought_signature':['opaque']}},
    {'google':{'thought_signature':'opaque','paid_feature':True}},
    {'google':{'thought_signature':'opaque'},'other_provider':{}},
    {'thought_signature':'opaque'}, {'google':{'other':'opaque'}}])
def test_chat_signature_metadata_rejects_every_unsupported_nested_shape(extra):
    call={**signed_chat_call(),'extra_content':extra}
    message={'role':'assistant','content':None,'tool_calls':[call]}
    request={'model':'gemini-3.8-flash','messages':[message]}
    response={'choices':[{'index':0,'finish_reason':'tool_calls','message':message}]}
    assert not server.chat_text_request(request)
    assert not server.chat_text_response(response)


def test_chat_signature_support_does_not_relax_other_keys_or_roles():
    for call in [{**signed_chat_call(),'unpriced':True},{**signed_chat_call(),'type':'other'}]:
        assert not server.chat_function_call_supported(call)
    message={'role':'model','content':None,'tool_calls':[signed_chat_call()]}
    assert not server.chat_text_request({'model':'gemini-3.8-flash','messages':[message]})
    assert not server.chat_text_response({'choices':[{'finish_reason':'tool_calls','message':message}]})
    # Existing assistant content/refusal/logprobs null are allowed, while null tool_calls are not.
    message={'role':'assistant','content':None,'refusal':None,'tool_calls':[signed_chat_call()]}
    assert server.chat_text_response({'choices':[{'finish_reason':'tool_calls','logprobs':None,'message':message}]})
    message['tool_calls']=None
    assert not server.chat_text_response({'choices':[{'finish_reason':'stop','message':message}]})


def test_documented_chat_signatures_settle_and_forward_unchanged_without_refunding_old_holds(proxy):
    http,_=proxy
    http.tenants[0]['budget_cents']=1000
    historical=http.ledger.reserve('a',None,'chat','gemini-3.8-flash')
    http.ledger.finish(historical,200,{})
    signed=signed_chat_call()
    message={'role':'assistant','content':None,'tool_calls':[signed]}
    reply={'choices':[{'index':0,'finish_reason':'tool_calls','message':message}],
        'usage':{'prompt_tokens':9000,'completion_tokens':20,'total_tokens':9120,
                 'completion_tokens_details':{'reasoning_tokens':100}}}
    contacted=[]
    def upstream(req,**_kwargs):
        contacted.append(json.loads(req.data))
        return Response(json.dumps(reply).encode())
    http.open_upstream=upstream
    first={'model':'gemini-3.8-flash','messages':[{'role':'user','content':'fixture'}]}
    status,raw=post(http,path='/openai/v1/chat/completions',body=first)
    assert status==200 and json.loads(raw)==reply
    continuation={**first,'messages':[*first['messages'],message,
        {'role':'tool','tool_call_id':'fixture-call','content':'fixture result'}]}
    status,raw=post(http,path='/openai/v1/chat/completions',body=continuation)
    assert status==200 and json.loads(raw)==reply
    assert contacted==[first,continuation]
    with http.ledger.connect() as db:
        rows=db.execute('SELECT allowance,usage_json FROM calls ORDER BY id').fetchall()
    assert [row[0] for row in rows]==[200,1,1]
    for _,raw in rows[1:]:
        counts=json.loads(raw)
        assert counts['billed_output_tokens']==120
        assert counts['allowance_policy']=='chat_text_receipt_v1'
        assert counts['chat_text_settlement_reason']=='receipt_settled'
        assert 'opaque-fixture-signature' not in raw


def test_final_message_signatures_settle_and_continue_without_refunding_old_holds(proxy):
    http,_=proxy
    http.tenants[0]['budget_cents']=1000
    historical=http.ledger.reserve('a',None,'chat','gemini-3.8-flash')
    http.ledger.finish(historical,200,{})
    message={'role':'assistant','content':'Hello.',
             'extra_content':{'google':{'thought_signature':'opaque-fixture-signature'}}}
    reply={'choices':[{'index':0,'finish_reason':'stop','message':message}],
        'usage':{'prompt_tokens':9000,'completion_tokens':20,'total_tokens':9120,
                 'completion_tokens_details':{'reasoning_tokens':100}}}
    contacted=[]
    def upstream(req,**_kwargs):
        contacted.append(json.loads(req.data))
        return Response(json.dumps(reply).encode())
    http.open_upstream=upstream
    first={'model':'gemini-3.8-flash','messages':[{'role':'user','content':'fixture'}]}
    status,raw=post(http,path='/openai/v1/chat/completions',body=first)
    assert status==200 and json.loads(raw)==reply
    continuation={**first,'messages':[*first['messages'],message,
        {'role':'user','content':'fixture follow-up'}]}
    status,raw=post(http,path='/openai/v1/chat/completions',body=continuation)
    assert status==200 and json.loads(raw)==reply
    assert contacted==[first,continuation]
    with http.ledger.connect() as db:
        rows=db.execute('SELECT allowance,usage_json FROM calls ORDER BY id').fetchall()
    assert [row[0] for row in rows]==[200,1,1]
    for _,raw in rows[1:]:
        counts=json.loads(raw)
        assert counts['billed_output_tokens']==120
        assert counts['allowance_policy']=='chat_text_receipt_v1'
        assert counts['chat_text_settlement_reason']=='receipt_settled'
        assert 'opaque-fixture-signature' not in raw


@pytest.mark.parametrize('extra', [None, {}, {'google': {}},
    {'google': {'thought_signature': ''}}, {'google': {'thought_signature': 1}},
    {'google': {'thought_signature': 'opaque', 'paid_feature': True}},
    {'google': {'thought_signature': 'opaque'}, 'other': {}},
    {'google': {'thought_signature': ['opaque']}}])
def test_final_message_invalid_signature_metadata_keeps_reservation(proxy, extra):
    http, _ = proxy
    message = {'role': 'assistant', 'content': 'Hello.', 'extra_content': extra}
    reply = {'choices': [{'finish_reason': 'stop', 'message': message}],
             'usage': {'prompt_tokens': 9000, 'completion_tokens': 20, 'total_tokens': 9120}}
    http.open_upstream = lambda *_args, **_kwargs: Response(json.dumps(reply).encode())
    body = {'model': 'gemini-3.8-flash', 'messages': [{'role': 'user', 'content': 'fixture'}]}
    assert not server.chat_text_request({**body, 'messages': [message]})
    assert post(http, path='/openai/v1/chat/completions', body=body)[0] == 200
    with http.ledger.connect() as db:
        allowance, raw = db.execute('SELECT allowance,usage_json FROM calls').fetchone()
    assert allowance == 200
    assert json.loads(raw)['chat_text_settlement_reason'] == 'response_ineligible'
    assert 'opaque' not in raw and 'paid_feature' not in raw


def test_message_signature_does_not_allow_other_fields_or_user_metadata():
    message = {'role': 'assistant', 'content': 'Hello.',
               'extra_content': {'google': {'thought_signature': 'opaque'}}}
    body = {'model': 'gemini-3.8-flash', 'messages': [message]}
    assert server.chat_text_request(body)
    assert not server.chat_text_request({**body, 'messages': [{**message, 'role': 'user'}]})
    reply = {'choices': [{'finish_reason': 'stop', 'message': {**message, 'unknown': True}}]}
    assert not server.chat_text_response(reply)


@pytest.mark.parametrize('workload,proof,timeout', [
    ('brief_extract', True, 120), ('brief_critic', True, 120), ('brief_revise', True, 120),
    ('brief_extract', False, 300), ('research', True, 300), ('chat', True, 300),
])
@pytest.mark.parametrize('failure', [None, 'opening', 'body', 'gateway'])
def test_brief_wait_is_scoped_and_uncertain_hold_is_preserved(tmp_path, workload, proof, timeout, failure):
    calls = []
    body = b'{"candidates":[],"usageMetadata":{"promptTokenCount":7,"candidatesTokenCount":3}}'
    class BodyTimeout(Response):
        def read(self, *_):
            raise TimeoutError('private provider details')
        def read1(self, *_):
            raise TimeoutError('private provider details')
    def upstream(req, **kwargs):
        calls.append(kwargs['timeout'])
        if failure == 'opening':
            raise urllib.error.URLError(TimeoutError('private provider details'))
        if failure == 'gateway':
            raise urllib.error.HTTPError(req.full_url, 504, 'timeout', {}, io.BytesIO(b'provider timeout'))
        return BodyTimeout(body) if failure == 'body' else Response(body)
    tenants = [{'id': 'a', 'token_sha256': hashlib.sha256(b'a-token').hexdigest(),
                'budget_cents': 200, 'enabled': True, 'expires_at': time.time() + 3600}]
    http = server.serve(('127.0.0.1', 0), tenants, 'upstream-only', tmp_path / 'ledger.sqlite3', upstream)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        headers = {'X-Sotto-Workload': workload}
        if proof:
            headers.update({'X-Sotto-Operation': 'a' * 64, 'X-Sotto-Run': 'b' * 32,
                            'X-Sotto-Attempt': '1'})
        status, result = post(http, headers=headers)
        assert calls == [timeout]
        if timeout == 120 and failure:
            assert status == 504
            assert json.loads(result)['error']['code'] == 'sotto_brief_outcome_uncertain'
            assert b'private' not in result
        elif failure == 'gateway':
            assert status == 504 and result == b'provider timeout'
        elif failure == 'opening':
            assert status == 502
        else:
            assert status == 200
            if not failure:
                assert result == body
        if failure:
            with http.ledger.connect() as db:
                assert db.execute('SELECT allowance FROM calls').fetchone()[0] == 200
        assert post(http, headers=headers)[0] == 402  # unknown usage is not refunded
        assert len(calls) == 1
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


@pytest.mark.parametrize('failure,expected', [
    ('remote_disconnected', 504), ('incomplete_body', 504), ('reset_body', 504),
    ('refused', 502),
])
def test_brief_loss_after_send_is_uncertain_but_connect_refusal_still_retries(tmp_path, failure, expected):
    class Broken(Response):
        def read(self, *_):
            if failure == 'incomplete_body':
                raise http.client.IncompleteRead(b'{"cand', 100)
            raise ConnectionResetError('private provider details')
    def upstream(req, **_kwargs):
        if failure == 'remote_disconnected':
            raise http.client.RemoteDisconnected('private provider details')
        if failure == 'refused':
            raise urllib.error.URLError(ConnectionRefusedError('private provider details'))
        return Broken(b'{"candidates":[]}')
    tenants = [{'id': 'a', 'token_sha256': hashlib.sha256(b'a-token').hexdigest(),
                'budget_cents': 200, 'enabled': True, 'expires_at': time.time() + 3600}]
    proxy_http = server.serve(('127.0.0.1', 0), tenants, 'upstream-only', tmp_path / 'ledger.sqlite3', upstream)
    thread = threading.Thread(target=proxy_http.serve_forever, daemon=True)
    thread.start()
    try:
        status, result = post(proxy_http, headers={'X-Sotto-Workload': 'brief_extract', 'X-Sotto-Operation': 'a' * 64,
                                             'X-Sotto-Run': 'b' * 32, 'X-Sotto-Attempt': '1'})
        assert status == expected
        assert b'private' not in result
        if expected == 504:
            assert json.loads(result)['error']['code'] == 'sotto_brief_outcome_uncertain'
    finally:
        proxy_http.shutdown()
        proxy_http.server_close()
        thread.join()


def test_unsupported_sdk_field_keeps_hold_on_otherwise_valid_tool_continuation(proxy):
    http, _ = proxy
    http.tenants[0]['budget_cents'] = 1000
    reply = {'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': 'Done.'}}],
             'usage': {'prompt_tokens': 9000, 'completion_tokens': 20, 'total_tokens': 9120,
                       'completion_tokens_details': {'reasoning_tokens': 100}}}
    http.open_upstream = lambda *args, **kwargs: Response(json.dumps(reply).encode())
    body = {'model': 'gemini-3.8-flash', 'messages': [
        {'role': 'user', 'content': 'Read my item'},
        {'role': 'assistant', 'content': None, 'tool_calls': [signed_chat_call()]},
        {'role': 'tool', 'tool_call_id': 'fixture-call', 'content': 'fixture item'}]}
    assert server.chat_text_request(body)
    assert not server.chat_text_request({**body, 'unsupported_paid_feature': True})
    for request in (body, {**body, 'unsupported_paid_feature': True}):
        assert post(http, path='/openai/v1/chat/completions', body=request)[0] == 200
    with http.ledger.connect() as db:
        rows = db.execute('SELECT allowance,usage_json FROM calls ORDER BY id').fetchall()
    assert [row[0] for row in rows] == [1, 200]
    assert [json.loads(row[1])['chat_text_settlement_reason'] for row in rows] == [
        'receipt_settled', 'request_ineligible']
