"""Offline provider diagnostics and real scheduled learning recovery contracts."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError

import pytest
import context_learning
import gemini
import history_coverage
import model_work


class TruncatedErrorBody(io.BytesIO):
    def read(self, *args):
        raise IncompleteRead(b'{"error":', 30)


@pytest.mark.parametrize('mode,expected', [
    ('malformed', 'malformed_request'), ('unsupported', 'unsupported_model_settings'),
    ('oversized', 'input_too_large'), ('auth', 'authentication_failed'),
    ('authorization', 'authorization_denied'),
    ('allowance', 'allowance_exhausted'), ('rate', 'rate_limited'),
    ('transient', 'service_unavailable'), ('output', 'output_limit'),
    ('validation', 'extraction_validation_failed'),
    ('truncated', 'provider_rejected'),
])
def test_production_extraction_failure_preserves_prior_checkpoint_and_knowledge(tmp_path, monkeypatch, mode, expected):
    import memory_cycle
    import knowledge
    import knowledge_update
    now = datetime.now(timezone.utc)
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'true')
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', '["gmail"]')
    monkeypatch.setenv('SOTTO_BRIEF_MODEL', 'gemini/gemini-3.8-flash')
    monkeypatch.setenv('GOOGLE_AI_API_KEY', 'fixture-only')
    prior = 'Mira works at Harbor Instruments.'
    knowledge_update.apply({'person_updates': [{'identifier': 'mira@example.com',
        'person_name': 'Mira', 'facts': [{'fact': prior, 'confidence': .65,
        'source': 'observed_message', 'source_ref': 'gmail:prior', 'evidence_refs': ['gmail:prior'],
        'observed_date': now.date().isoformat(), 'memory_type': 'context'}]}]}, now=now)
    knowledge_path = Path(knowledge.find_person_file(identifier='mira@example.com'))
    knowledge_before = knowledge.parse_person_file(knowledge_path.read_text()).facts
    totals = dict.fromkeys(('pages', 'rows', 'eligible', 'selected'), 2)
    totals.update(dict.fromkeys(('skipped', 'omitted', 'truncated', 'rejected_people'), 0))
    checkpoint = {'since': int(now.timestamp()) - 86400, 'until': int(now.timestamp()),
        'cursor': 'retained-page', 'complete': False, 'pages': 2, 'window_page_base': 0,
        'rows': 2, 'reviewed': 2, 'observation_totals': totals,
        'last_success': (now - timedelta(hours=1)).isoformat()}
    path = tmp_path / 'knowledge/history-state.json'
    path.write_text(json.dumps({'sources': {'gmail': checkpoint}}))
    row = {'id': 'new-source', 'from': 'Mira <mira@example.com>',
           'date': now.isoformat(), 'body': 'Mira builds coastal flood sensors.'}
    calls = []
    statuses = {'malformed': (400, 'Invalid JSON payload received'),
        'unsupported': (400, 'Thinking level PRIVATE is not supported'),
        'oversized': (413, 'PRIVATE'), 'auth': (401, 'PRIVATE'), 'authorization': (403, 'PRIVATE'),
        'allowance': (402, 'PRIVATE'), 'rate': (429, 'PRIVATE'), 'transient': (503, 'PRIVATE')}
    def provider(request, **kwargs):
        if (model_work.current() or {}).get('task') == 'memory_curate':
            body = json.loads(request.data)
            candidates = json.loads(body['contents'][0]['parts'][0]['text'])
            result = {'people': [{'id': item['id'], 'summary_refs': list(item['facts'])[:4],
                                  'conflicts': [], 'equivalent_groups': []} for item in candidates]}
            return io.BytesIO(json.dumps({'candidates': [{'finishReason': 'STOP',
                'content': {'parts': [{'text': json.dumps(result)}]}}]}).encode())
        calls.append(True)
        if mode == 'truncated':
            raise HTTPError('https://private.invalid', 400, 'PRIVATE', {}, TruncatedErrorBody())
        if mode in statuses:
            status, message = statuses[mode]
            raise HTTPError('https://private.invalid', status, 'PRIVATE', {},
                io.BytesIO(json.dumps({'error': {'message': message}}).encode()))
        text = '{"people": [{}]}'
        return io.BytesIO(json.dumps({'candidates': [{'finishReason': 'MAX_TOKENS' if mode == 'output' else 'STOP',
            'content': {'parts': [{'text': text}]}}]}).encode())
    monkeypatch.setattr(gemini.urllib.request, 'urlopen', provider)
    for hour in range(3):
        memory_cycle.run(now + timedelta(hours=hour),
            fetch=lambda *a: {'status': 'ok', 'rows': [row], 'next_cursor': 'later-page', 'complete': False})
    after = json.loads(path.read_text())['sources']['gmail']
    for field in ('since', 'until', 'cursor', 'pages', 'reviewed', 'rows', 'last_success', 'observation_totals'):
        assert after[field] == checkpoint[field]
    assert knowledge.parse_person_file(knowledge_path.read_text()).facts == knowledge_before
    assert history_coverage.learning_coverage(tmp_path, ['gmail'])['sources']['gmail'][
        'latest_attempt']['category'] == expected
    assert len(calls) == (1 if mode in ('malformed', 'unsupported', 'oversized', 'truncated') else
                          2 if mode in ('output', 'validation', 'auth', 'authorization') else 3)
    assert 'PRIVATE' not in path.read_text()


def test_rejected_learning_contract_tracks_policy_client_and_writing_changes(monkeypatch):
    original = context_learning.request_revision()
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'new-heartbeat')
    assert context_learning.request_revision() == original
    monkeypatch.setitem(model_work.POLICIES, 'memory_extract',
                        replace(model_work.POLICIES['memory_extract'], thinking='corrected-setting'))
    changed_policy = context_learning.request_revision()
    assert changed_policy != original
    monkeypatch.setattr(model_work, 'implementation_revision', lambda: 'repaired-transport')
    changed_client = context_learning.request_revision()
    assert changed_client != changed_policy
    monkeypatch.setattr(gemini.gemini_transport, 'WRITING_STYLE', 'Changed system instructions')
    assert context_learning.request_revision() != changed_client


@pytest.mark.parametrize('selector,value', [
    ('SOTTO_OPENAI_BASE_URL', 'http://127.0.0.1:9999/v1'),
    ('SOTTO_DEPLOYMENT_MODE', 'managed'),
])
def test_rejected_learning_contract_tracks_supported_route_repairs(monkeypatch, selector, value):
    monkeypatch.delenv(selector, raising=False)
    original = context_learning.request_revision()
    monkeypatch.setenv(selector, value)
    corrected = context_learning.request_revision()
    assert corrected != original
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'later-heartbeat')
    assert context_learning.request_revision() == corrected
    monkeypatch.setenv(selector, value)
    assert context_learning.request_revision() == corrected


@pytest.mark.parametrize('status,detail,expected', [
    (400, {'message': 'Invalid JSON payload received. PRIVATE'}, 'malformed_request'),
    (400, {'message': 'Thinking level PRIVATE is not supported'}, 'unsupported_model_settings'),
    (400, {'message': 'Input token count exceeds maximum PRIVATE'}, 'input_too_large'),
    (413, {'message': 'PRIVATE'}, 'input_too_large'),
    (400, {'status': 'INVALID_ARGUMENT', 'message': 'PRIVATE'}, 'provider_rejected'),
    (422, {'message': 'PRIVATE'}, 'provider_rejected'),
    (401, {'message': 'PRIVATE'}, 'authentication_failed'),
    (403, {'message': 'PRIVATE'}, 'authorization_denied'),
    (402, {'message': 'PRIVATE'}, 'allowance_exhausted'),
    (429, {'details': [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                       'violations': [{'quotaId': 'RequestsPerMinutePerProject'}]}]}, 'rate_limited'),
    (503, {'message': 'PRIVATE'}, 'service_unavailable'),
    (504, {'message': 'PRIVATE'}, 'timeout'),
])
def test_provider_categories_remain_content_free(status, detail, expected):
    error = HTTPError('https://private.invalid', status, 'PRIVATE', {},
                      io.BytesIO(json.dumps({'error': detail}).encode()))
    category = gemini.request_failure(error)
    assert category == expected
    window = {'error': 'HTTPError', 'error_stage': 'memory_extraction',
              'http_status': status, 'provider_failure': category,
              'last_attempt': {'status': 'failed', 'stage': 'memory_extraction'}}
    view = history_coverage.window_coverage(window)
    assert view['latest_attempt']['category'] == expected
    assert 'PRIVATE' not in json.dumps(view) and 'private.invalid' not in json.dumps(view)


@pytest.mark.parametrize('body', [b'{bad-json', b'[]', b'"PRIVATE"',
                                 b'{"error":{"message":"' + b'x' * 17000 + b'"}}'])
def test_unrecognized_or_oversized_provider_body_keeps_cause_unknown(body):
    error = HTTPError('https://private.invalid', 400, 'PRIVATE', {}, io.BytesIO(body))
    assert gemini.request_failure(error) == 'provider_rejected'


@pytest.mark.parametrize('optimize', [0, 2])
def test_real_history_scheduler_repairs_policy_then_replays_without_duplicate_facts(tmp_path, monkeypatch, optimize):
    module_path = Path(__file__).resolve().parents[2] / 'runtime/sotto-chat/evals/learning_journey.py'
    spec = importlib.util.spec_from_file_location('fixture_learning_journey', module_path)
    journey = importlib.util.module_from_spec(spec)
    exec(compile(module_path.read_text(), str(module_path), 'exec', optimize=optimize), journey.__dict__)
    world = {'savedSourceGrants': ['gmail'], 'learningJourney': {
        'email': 'mira@example.com', 'name': 'Mira Patel',
        'fact': 'Mira Patel designs coastal flood sensors at Harbor Instruments.'}}
    # Unexpected source/model HTTP fails the test; only the fixture's exact boundary is replaced.
    monkeypatch.setattr(gemini.urllib.request, 'urlopen',
                        lambda *a, **k: pytest.fail('unexpected external HTTP'))
    first = journey.run('rejected', world, tmp_path)
    assert first['facts'] == first['pages'] == 0
    assert first['coverage']['last_extraction_success_at'] is None
    second = journey.run('resumed', world, tmp_path)
    assert second['facts'] == second['pages'] == 1
    third = journey.run('replayed', world, tmp_path)
    assert third['facts'] == 1 and third['pages'] == 2
    assert second['source_reads'][0]['cursor'] is None
    assert third['source_reads'][0]['cursor'] == 'learning-next'
    assert third['coverage']['review_scope'] == 'reviewed_window'
    receipt = tmp_path.with_name(tmp_path.name + '-learning-journey.json')
    assert json.loads(receipt.read_text())['stages'] == ['rejected', 'resumed', 'replayed']
    assert not receipt.is_relative_to(tmp_path)


@pytest.mark.parametrize('optimize', [0, 2])
def test_caught_unexpected_history_transport_still_fails_journey(tmp_path, monkeypatch, optimize):
    import memory_cycle
    module_path = Path(__file__).resolve().parents[2] / 'runtime/sotto-chat/evals/learning_journey.py'
    spec = importlib.util.spec_from_file_location('fixture_denied_learning', module_path)
    journey = importlib.util.module_from_spec(spec)
    exec(compile(module_path.read_text(), str(module_path), 'exec', optimize=optimize), journey.__dict__)
    world = {'savedSourceGrants': ['gmail'], 'learningJourney': {
        'email': 'mira@example.com', 'name': 'Mira Patel', 'fact': 'Mira builds flood sensors.'}}
    original = memory_cycle.prewarm_graph.prewarm
    def unexpected(*args, **kwargs):
        original(*args, **kwargs)
        gemini.urllib.request.urlopen('https://unexpected.invalid')
    monkeypatch.setattr(memory_cycle.prewarm_graph, 'prewarm', unexpected)
    with pytest.raises(AssertionError, match='Unexpected learning fixture boundaries'):
        journey.run('rejected', world, tmp_path)


@pytest.mark.parametrize('direct_first', [True, False])
def test_background_route_repair_releases_both_learning_latches(tmp_path, monkeypatch, direct_first):
    import memory_cycle
    import knowledge
    import sqlite3
    now = datetime.now(timezone.utc)
    for key, value in {
        'SOTTO_DATA': str(tmp_path), 'SOTTO_DEPLOYMENT_MODE': 'self-host',
        'SOTTO_PROACTIVE_CAPTURED_SOURCES': '["gmail"]',
        'SOTTO_BRIEF_MODEL': 'gemini/gemini-3.8-flash',
        'SOTTO_MODEL_PROXY_URL': 'https://proxy.example.test',
        'SOTTO_MODEL_PROXY_TOKEN': 'fixture-only', 'GOOGLE_AI_API_KEY': 'fixture-only',
    }.items():
        monkeypatch.setenv(key, value)
    calls, reads = [], []
    fact = 'Mira builds coastal flood sensors.'
    row = {'id': 'route-source', 'from': 'Mira <mira@example.com>',
           'date': now.isoformat(), 'body': fact}

    def fetch(source, window):
        assert source == 'gmail'
        reads.append(window.get('cursor'))
        return {'status': 'ok', 'rows': [row], 'complete': True}

    def provider(request, **kwargs):
        if request.full_url == 'https://proxy.example.test/v1/capabilities/background-budget':
            return io.BytesIO(json.dumps({'version': 1, 'finite': True, 'can_admit': True,
                'supported_native_models': ['gemini-3.8-flash']}).encode())
        route = ('proxy' if request.full_url.startswith('https://proxy.example.test/native/')
                 else 'direct' if request.full_url.startswith('https://generativelanguage.googleapis.com/')
                 else None)
        assert route is not None, 'unexpected model endpoint'
        task = model_work.current()['task']
        body = json.loads(request.data)
        records = json.loads(body['contents'][0]['parts'][0]['text'])
        if task == 'memory_extract':
            calls.append(route)
            if len(calls) == 1:
                raise HTTPError(request.full_url, 400, 'Bad Request', {}, io.BytesIO(b'{}'))
            result = {'people': [{'subject_id': records[0]['subject_id'],
                                  'facts': [{'text': fact, 'refs': [records[0]['ref']]}]}]}
        else:
            assert task == 'memory_curate'
            result = {'people': [{'id': item['id'], 'summary_refs': list(item['facts'])[:4],
                                  'conflicts': [], 'equivalent_groups': []} for item in records]}
        return io.BytesIO(json.dumps({'candidates': [{'finishReason': 'STOP',
            'content': {'parts': [{'text': json.dumps(result)}]}}]}).encode())

    monkeypatch.setattr(gemini.urllib.request, 'urlopen', provider)
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'true' if direct_first else 'false')
    memory_cycle.run(now, fetch=fetch)
    path = Path(memory_cycle.state_path())
    rejected = json.loads(path.read_text())['sources']['gmail']
    assert rejected['error_code'] == 'provider_request_rejected'
    assert rejected['pages'] == 0 and rejected['cursor'] is None
    # Neither elapsed time nor another scheduler occurrence releases either latch.
    memory_cycle.run(now + timedelta(hours=1), fetch=fetch)
    assert len(calls) == len(reads) == 1
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'false' if direct_first else 'true')
    memory_cycle.run(now + timedelta(hours=2), fetch=fetch)
    recovered = json.loads(path.read_text())['sources']['gmail']
    assert calls == (['direct', 'proxy'] if direct_first else ['proxy', 'direct'])
    assert reads == [None, None]
    assert recovered['pages'] == recovered['reviewed'] == 1
    assert 'rejected_request_revision' not in recovered and 'error_code' not in recovered
    person = knowledge.parse_person_file(Path(knowledge.find_person_file(identifier='mira@example.com')).read_text())
    assert [item.text for item in person.facts.values()] == [fact]
    # Recovery did not delete the old parked request; the corrected contract got a new identity.
    with sqlite3.connect(tmp_path / 'events/model-work.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM parked_requests').fetchone()[0] == 1


@pytest.mark.parametrize('status', [400, 422])
def test_truncated_diagnostic_preserves_rejection_and_other_source_work(tmp_path, monkeypatch, status):
    import memory_cycle
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    monkeypatch.setenv('SOTTO_BACKGROUND_UNMETERED', 'true')
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', '["gmail", "imessage", "whatsapp"]')
    for processor, name in [(memory_cycle.style_extract, 'extract'),
                            (memory_cycle.prewarm_graph, 'prewarm'),
                            (memory_cycle.relationship_pulse, 'observe_history')]:
        monkeypatch.setattr(processor, name, lambda *a, **k: None)
    seen, curated = [], []

    def fetch(source, window):
        seen.append(source)
        return {'status': 'ok', 'rows': [], 'complete': True}

    def learn(records, **kwargs):
        if seen[-1] == 'imessage':
            raise HTTPError('https://provider.example.test', status, 'Bad Request', {}, TruncatedErrorBody())
        return {'reviewed': 0, 'facts': 0}

    def curate(**kwargs):
        curated.append(True)
        return {'reviewed': 0}

    now = datetime.now(timezone.utc)
    memory_cycle.run(now, fetch=fetch, learn=learn, curate=curate)
    state = json.loads(Path(memory_cycle.state_path()).read_text())
    failed = state['sources']['imessage']
    assert failed['http_status'] == status and failed['provider_failure'] == 'provider_rejected'
    assert failed['rejected_request_revision'] and failed['retry_after'] > now.timestamp()
    assert failed['pages'] == 0 and failed['cursor'] is None
    assert seen == ['imessage', 'whatsapp', 'gmail'] and curated == [True]
    assert all(state['sources'][source]['pages'] == 1 for source in ('whatsapp', 'gmail'))


@pytest.mark.parametrize('status,expected', [(400, 'unsupported_model_settings'),
                                           (402, 'allowance_exhausted')])
def test_managed_proxy_helpers_preserve_provider_failure(status, expected, monkeypatch):
    import gemini_transport
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example.test')
    error = HTTPError('https://proxy.example.test/native/v1beta/models/gemini-3.8-flash:generateContent',
        status, 'PRIVATE', {}, io.BytesIO(json.dumps({'error': {
            'code': 'sotto_budget_exhausted',
            'message': 'Thinking level PRIVATE is not supported'}}).encode()))
    assert gemini_transport.proxy_budget_exhausted(error) is (status == 402)
    assert gemini_transport.proxy_admission_busy(error) is False
    assert gemini.request_failure(error) == expected


def test_optimized_journey_acceptance_checks_reject_corrupt_cursor(tmp_path, monkeypatch):
    import sys
    # Save the original object before the journey promotes its pack paths in place.
    monkeypatch.setattr(sys, 'path', list(sys.path))
    import memory_cycle
    module_path = Path(__file__).resolve().parents[2] / 'runtime/sotto-chat/evals/learning_journey.py'
    spec = importlib.util.spec_from_file_location('fixture_corrupt_learning', module_path)
    journey = importlib.util.module_from_spec(spec)
    exec(compile(module_path.read_text(), str(module_path), 'exec', optimize=2), journey.__dict__)
    world = {'savedSourceGrants': ['gmail'], 'learningJourney': {
        'email': 'mira@example.com', 'name': 'Mira Patel', 'fact': 'Mira builds flood sensors.'}}
    original = memory_cycle.run
    def corrupt(*args, **kwargs):
        receipt = original(*args, **kwargs)
        path = Path(memory_cycle.state_path())
        state = json.loads(path.read_text())
        state['sources']['gmail']['cursor'] = 'invalid-cursor'
        path.write_text(json.dumps(state))
        return receipt
    monkeypatch.setattr(memory_cycle, 'run', corrupt)
    with pytest.raises(AssertionError, match='Rejected learning advanced its checkpoint'):
        journey.run('rejected', world, tmp_path)
    pack = module_path.parents[3] / 'sotto-chief-of-staff'
    desired = [str(pack / 'relationship-pulse/scripts'),
               *(str(pack / '_shared' / folder) for folder in ('lib', 'scripts', 'knowledge'))]
    # Other production imports also manage their paths. Check the eval selection itself.
    monkeypatch.setattr(sys, 'path', ['unrelated', *reversed(desired), desired[0]])
    journey._select_pack_paths(pack)
    assert sys.path[:4] == desired
    assert all(sys.path.count(directory) == 1 for directory in desired)
