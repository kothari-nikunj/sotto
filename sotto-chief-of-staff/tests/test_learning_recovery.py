"""Chronological recovery through the real scheduler, extraction, curation and HTTP boundary."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
from email.message import Message
from types import SimpleNamespace
import time
import sqlite3
import subprocess
import sys
from urllib.error import HTTPError

import pytest
import dreamer
import gemini
import knowledge as kg
import knowledge_update as ku
import memory_cycle
import model_work


NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
PROXY = 'https://proxy.example.test'


def _setup(tmp_path, monkeypatch):
    for key, value in {'SOTTO_DATA': str(tmp_path), 'SOTTO_DEPLOYMENT_MODE': 'self-host',
        'SOTTO_PROACTIVE_CAPTURED_SOURCES': '["gmail"]',
        'SOTTO_BRIEF_MODEL': 'gemini/gemini-3.8-flash', 'GOOGLE_AI_API_KEY': 'fixture-only',
        'SOTTO_MODEL_PROXY_URL': PROXY, 'SOTTO_MODEL_PROXY_TOKEN': 'fixture-only',
        'SOTTO_BACKGROUND_UNMETERED': 'true'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(memory_cycle.gather_google, 'history_page',
        lambda *a: {'status': 'ok', 'rows': [], 'complete': True})


def _fact(email, text):
    ku.apply({'person_updates': [{'identifier': email, 'person_name': email.split('@')[0],
        'facts': [{'fact': text, 'confidence': .65, 'source': 'observed_message',
            'source_ref': 'gmail:' + email, 'evidence_refs': ['gmail:' + email],
            'observed_date': NOW.date().isoformat(), 'memory_type': 'context'}]}]}, now=NOW)
    path = kg.find_person_file(identifier=email)
    return kg.parse_person_file(Path(path).read_text()).canonical_id


def _response(value):
    return io.BytesIO(json.dumps({'candidates': [{'finishReason': 'STOP',
        'content': {'parts': [{'text': json.dumps(value)}]}}]}).encode())


def _provider(calls, failure):
    def request(req, **kwargs):
        if req.full_url == PROXY + '/v1/capabilities/background-budget':
            return io.BytesIO(json.dumps({'version': 1, 'finite': True, 'can_admit': True,
                'supported_native_models': ['gemini-3.8-flash']}).encode())
        assert req.full_url.startswith((PROXY + '/native/', 'https://generativelanguage.googleapis.com/'))
        task = model_work.current()['task']
        body = json.loads(req.data)
        records = json.loads(body['contents'][0]['parts'][0]['text'])
        calls.append({'task': task, 'records': records, 'config': body['generationConfig'],
                      'route': 'proxy' if req.full_url.startswith(PROXY) else 'direct'})
        if failure.get('status'):
            raise HTTPError(req.full_url, failure['status'], 'PRIVATE', {}, io.BytesIO(b'{}'))
        if task == 'memory_curate':
            return _response({'people': [{'id': row['id'], 'summary_refs': list(row['facts'])[:4],
                'conflicts': [], 'equivalent_groups': []} for row in records]})
        assert task == 'memory_extract'
        return _response({'people': [{'subject_id': records[0]['subject_id'], 'facts': [
            {'text': records[0]['text'], 'refs': [records[0]['ref']]}]}]})
    return request


@pytest.mark.parametrize('direct_first', [True, False])
def test_parked_curation_recovers_effective_route_without_recurating_covered_memory(tmp_path, monkeypatch, direct_first):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', str(direct_first).lower())
    calls, failure = [], {}
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', _provider(calls, failure))
    covered = _fact('covered@example.com', 'Covered builds ocean buoys.')
    memory_cycle.run(NOW)
    assert calls[-1]['records'][0]['id'] == covered
    covered_rows = json.loads(Path(dreamer._state_path()).read_text())['facts'][covered]
    pending = _fact('pending@example.com', 'Pending builds flood sensors.')
    failure['status'] = 400
    memory_cycle.run(NOW + timedelta(hours=1))
    assert calls[-1]['records'][0]['id'] == pending
    after_failure = json.loads(Path(dreamer._state_path()).read_text())
    assert all(row['attempts'] == dreamer.ATTEMPTS and not row['covered']
               for row in after_failure['facts'][pending].values())
    memory_cycle.run(NOW + timedelta(hours=2))
    assert len(calls) == 2  # elapsed time alone never resubmits a deterministic failure
    failure.clear()
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', str(not direct_first).lower())
    memory_cycle.run(NOW + timedelta(hours=3))
    assert len(calls) == 3
    assert calls[-1]['route'] == ('proxy' if direct_first else 'direct')
    assert [row['id'] for row in calls[-1]['records']] == [pending]
    repaired = json.loads(Path(dreamer._state_path()).read_text())
    assert repaired['facts'][covered] == covered_rows
    assert all(row['covered'] for row in repaired['facts'][pending].values())
    memory_cycle.run(NOW + timedelta(hours=4))
    assert len(calls) == 3
    with sqlite3.connect(tmp_path / 'events/model-work.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM parked_requests').fetchone()[0] == 1


def test_noop_model_setting_and_credential_rotation_do_not_reopen_failed_model_output(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _fact('pending@example.com', 'Pending builds flood sensors.')
    calls, failure = [], {'status': 400}
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', _provider(calls, failure))
    memory_cycle.run(NOW)
    contract = dreamer.request_revision()
    monkeypatch.setenv('SOTTO_GEMINI_MODEL', 'ignored-because-brief-model-is-set')
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'rotated-fixture-only')
    assert dreamer.request_revision() == contract
    memory_cycle.run(NOW + timedelta(hours=1))
    assert len(calls) == 1
    assert 'rotated-fixture-only' not in Path(dreamer._state_path()).read_text()


@pytest.mark.parametrize('repair', ['output', 'thinking', 'writing'])
def test_parked_curation_tracks_actual_generation_policy_and_writing_wrapper(tmp_path, monkeypatch, repair):
    _setup(tmp_path, monkeypatch)
    pending = _fact('pending@example.com', 'Pending builds flood sensors.')
    calls, failure = [], {'status': 400}
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', _provider(calls, failure))
    memory_cycle.run(NOW)
    original = dreamer.request_revision()
    policy = model_work.POLICIES['memory_curate']
    if repair == 'writing':
        monkeypatch.setattr(gemini.gemini_transport, 'WRITING_STYLE', 'Repaired writing instructions.')
    else:
        monkeypatch.setitem(model_work.POLICIES, 'memory_curate', replace(policy,
            **({'output': 16384} if repair == 'output' else {'thinking': 'high'})))
    assert dreamer.request_revision() != original
    failure.clear()
    memory_cycle.run(NOW + timedelta(hours=1))
    assert len(calls) == 2
    if repair == 'output':
        assert calls[-1]['config']['maxOutputTokens'] == 16384
    elif repair == 'thinking':
        assert calls[-1]['config']['thinkingConfig']['thinkingLevel'] == 'high'
    assert all(row['covered'] for row in json.loads(Path(dreamer._state_path()).read_text())['facts'][pending].values())


@pytest.fixture
def learning_proxy(tmp_path):
    """Production proxy admission/settlement with invented upstream replies and no sockets."""
    source = Path(__file__).resolve().parents[2] / 'cloud/model-proxy/server.py'
    spec = importlib.util.spec_from_file_location('learning_proxy_fixture', source)
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)
    calls, failure = [], {}
    ledger = proxy.Ledger(tmp_path / 'proxy.sqlite3')
    tenant = {'id': 'invented', 'token_sha256': hashlib.sha256(b'fixture-only').hexdigest(),
              'budget_cents': 1000, 'enabled': True, 'expires_at': time.time() + 3600}
    provider = _provider(calls, failure)

    def upstream(req, **kwargs):
        result = provider(req, **kwargs)
        result.headers = {'Content-Type': 'application/json'}
        return result

    service = SimpleNamespace(tenants=[tenant], ledger=ledger, upstream_key='fixture-only',
                              open_upstream=upstream)

    def request(req, **kwargs):
        assert req.full_url.startswith(PROXY + '/')  # never reach a real provider
        handler = object.__new__(proxy.Handler)
        handler.server, handler.path = service, req.full_url[len(PROXY):]
        handler.headers = Message()
        for key, value in req.header_items():
            handler.headers[key] = value
        if req.data is not None:
            handler.headers['Content-Length'] = str(len(req.data))
        handler.rfile, handler.wfile = io.BytesIO(req.data or b''), io.BytesIO()
        handler.connection = SimpleNamespace(settimeout=lambda *_: None)
        response, headers = {}, {}
        handler.send_response = lambda status: response.update(status=status)
        handler.send_header = lambda key, value: headers.update({key: value})
        handler.end_headers = lambda: None
        (handler.do_POST if req.data is not None else handler.do_GET)()
        content = handler.wfile.getvalue()
        if response['status'] >= 400:
            raise HTTPError(req.full_url, response['status'], 'fixture-only', headers, io.BytesIO(content))
        result = io.BytesIO(content)
        result.headers = headers
        return result

    return SimpleNamespace(request=request, calls=calls, failure=failure, ledger=ledger)


@pytest.mark.parametrize('status', [401, 403])
@pytest.mark.parametrize('task', ['memory_extract', 'memory_curate'])
def test_upstream_auth_failure_keeps_allowance_but_stops_after_two_cycles(
        tmp_path, monkeypatch, learning_proxy, status, task):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'false')
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', learning_proxy.request)
    if task == 'memory_curate':
        cid = _fact('pending@example.com', 'Pending builds flood sensors.')
    else:
        row = {'id': 'new', 'from': 'New <new@example.com>', 'date': NOW.isoformat(),
               'body': 'New designs underwater robots.'}
        monkeypatch.setattr(memory_cycle.gather_google, 'history_page',
            lambda *a: {'status': 'ok', 'rows': [row], 'next_cursor': 'after-new', 'complete': False})
    learning_proxy.failure['status'] = status
    for hour in range(5):
        # The next scheduled occurrence must not reset the unchanged source/candidate limit.
        monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', f'scheduled-occurrence-{hour}')
        memory_cycle.run(NOW + timedelta(hours=hour))
    assert [call['task'] for call in learning_proxy.calls] == [task, task]
    with learning_proxy.ledger.connect() as db:
        assert db.execute('SELECT status,allowance,active_until FROM calls ORDER BY id').fetchall() == [
            (status, 200, None), (status, 200, None)]
    assert learning_proxy.ledger.budget_capability('invented', 1000)['remaining_cents'] == 600
    if task == 'memory_curate':
        facts = json.loads(Path(dreamer._state_path()).read_text())['facts'][cid]
        assert all(not row['covered'] and row['attempts'] == 2 for row in facts.values())
    else:
        window = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['gmail']
        assert window['cursor'] is None and window['pages'] == 0 and window['reviewed'] == 0
        with gemini.gemini_transport.background_learning():
            assert window['rejected_request_revision'] == memory_cycle.context_learning.request_revision()
        assert window['extraction_failure']['attempts'] == 2
    # Repairing an upstream credential alone cannot erase an admitted/ambiguous paid history.
    learning_proxy.failure.clear()
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'another-scheduled-occurrence')
    memory_cycle.run(NOW + timedelta(hours=6))
    assert len(learning_proxy.calls) == 2
    with sqlite3.connect(tmp_path / 'events/model-work.sqlite3') as db:
        assert db.execute('SELECT status FROM attempts ORDER BY rowid').fetchall() == [
            ('http_' + str(status),), ('http_' + str(status),)]


@pytest.mark.parametrize('task', ['memory_extract', 'memory_curate'])
def test_proxy_auth_preflight_repair_never_spends_a_candidate_or_allowance(
        tmp_path, monkeypatch, learning_proxy, task):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'false')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'invalid-fixture-only')
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', learning_proxy.request)
    source_reads = []
    if task == 'memory_curate':
        cid = _fact('pending@example.com', 'Pending builds flood sensors.')
    else:
        row = {'id': 'new', 'from': 'New <new@example.com>', 'date': NOW.isoformat(),
               'body': 'New designs underwater robots.'}

        def history_page(*args):
            source_reads.append(args)
            return {'status': 'ok', 'rows': [row], 'complete': True}

        monkeypatch.setattr(memory_cycle.gather_google, 'history_page', history_page)
    for hour in range(3):
        receipt = memory_cycle.run(NOW + timedelta(hours=hour))
        assert receipt['dreamer'] == {'status': 'held', 'reason': 'background_budget_capability_unavailable'}
    assert source_reads == []  # even a nonempty extraction page cannot be fetched before preflight
    assert not Path(dreamer._state_path()).exists()
    assert not (tmp_path / 'events/model-work.sqlite3').exists()
    with learning_proxy.ledger.connect() as db:
        assert db.execute('SELECT count(*) FROM calls').fetchone()[0] == 0
    assert learning_proxy.calls == []
    monkeypatch.setenv('SOTTO_MODEL_PROXY_TOKEN', 'fixture-only')
    memory_cycle.run(NOW + timedelta(hours=3))
    if task == 'memory_extract':
        assert len(source_reads) == 1
        person_path = Path(kg.find_person_file(identifier='new@example.com'))
        person = kg.parse_person_file(person_path.read_text())
        cid = person.canonical_id
        assert [fact.text for fact in person.facts.values()] == ['New designs underwater robots.']
    facts = json.loads(Path(dreamer._state_path()).read_text())['facts'][cid]
    assert all(row['covered'] and row['attempts'] == 1 for row in facts.values())
    assert [call['task'] for call in learning_proxy.calls] == (
        ['memory_extract', 'memory_curate'] if task == 'memory_extract' else ['memory_curate'])


@pytest.mark.parametrize('task', ['memory_extract', 'memory_curate'])
@pytest.mark.parametrize('status', [401, 403])
def test_auth_and_unknown_outcomes_share_the_finite_paid_ladder(tmp_path, monkeypatch, task, status):
    _setup(tmp_path, monkeypatch)
    with model_work.scope(task, 'same-evidence'), pytest.raises(HTTPError):
        with model_work.attempt('gemini', 'gemini-3.8-flash', 'PRIVATE'):
            raise HTTPError('https://provider.invalid', status, 'PRIVATE', {}, None)
    with model_work.scope(task, 'same-evidence'), pytest.raises(TimeoutError):
        with model_work.attempt('gemini', 'gemini-3.8-flash', 'PRIVATE'):
            raise TimeoutError('PRIVATE')
    with model_work.scope(task, 'same-evidence'), pytest.raises(model_work.ModelWorkHeldError):
        with model_work.attempt('gemini', 'gemini-3.8-flash', 'PRIVATE'):
            pytest.fail('Authorization or unknown paid outcomes must not get unlimited replacements')
    with sqlite3.connect(tmp_path / 'events/model-work.sqlite3') as db:
        assert db.execute('SELECT status FROM attempts ORDER BY attempt').fetchall() == [
            ('http_' + str(status),), ('TimeoutError',)]
        assert db.execute('SELECT attempts FROM operations').fetchone()[0] == 2
    assert b'PRIVATE' not in (tmp_path / 'events/model-work.sqlite3').read_bytes()


@pytest.mark.parametrize('failure_status', [400, 401, 403])
def test_fresh_process_recovers_saved_work_then_another_restart_makes_no_model_call(tmp_path, monkeypatch, failure_status):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'restart-preserved-occurrence')
    calls, failure = [], {}
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', _provider(calls, failure))
    covered = _fact('covered@example.com', 'Covered builds ocean buoys.')
    memory_cycle.run(NOW)
    covered_rows = json.loads(Path(dreamer._state_path()).read_text())['facts'][covered]
    pending = _fact('pending@example.com', 'Pending builds flood sensors.')
    failure['status'] = failure_status
    memory_cycle.run(NOW + timedelta(hours=1))
    memory_cycle.run(NOW + timedelta(hours=2))
    pack = Path(__file__).resolve().parents[1]
    child_env = dict(os.environ)
    # Explicit roots avoid inheriting repeated suite sys.path mutations into subprocess arguments.
    child_env['PYTHONPATH'] = os.pathsep.join(str(path) for path in [
        pack / 'tests', *(pack / '_shared' / folder for folder in ('lib', 'scripts', 'knowledge')),
        pack / 'relationship-pulse/scripts'])
    # A genuine effective route correction releases parked work; credential repair alone does not.
    child_env['SOTTO_BACKGROUND_UNMETERED'] = 'false'  # repaired direct -> proxy route
    # Each child installs only synthetic external boundaries.
    script = '''
import json
from datetime import timedelta
from test_learning_recovery import NOW, _provider
import gemini, memory_cycle
calls = []
gemini.urllib.request.urlopen = _provider(calls, {})
memory_cycle.gather_google.history_page = lambda *a: {'status': 'ok', 'rows': [], 'complete': True}
receipt = memory_cycle.run(NOW + timedelta(hours=int(__import__('sys').argv[1])))
print(json.dumps({'calls': len(calls), 'dreamer': receipt['dreamer']}))
'''
    first = subprocess.run([sys.executable, '-c', script, '3'], env=child_env,
                           capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(first.stdout)['calls'] == 1
    assert json.loads(first.stdout)['dreamer']['reviewed'] == 1
    recovered = json.loads(Path(dreamer._state_path()).read_text())['facts']
    assert recovered[covered] == covered_rows
    assert all(row['covered'] for row in recovered[pending].values())
    second = subprocess.run([sys.executable, '-c', script, '4'], env=child_env,
                            capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(second.stdout)['calls'] == 0
    assert json.loads(second.stdout)['dreamer']['reviewed'] == 0
    assert json.loads(Path(dreamer._state_path()).read_text())['facts'] == recovered
