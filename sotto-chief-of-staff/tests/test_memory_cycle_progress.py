"""Eligible history budget and real memory-cycle progress under partial failures."""
import json
from datetime import timedelta
from pathlib import Path

import pytest
import context_learning as learning
import dreamer
import knowledge as kg
import knowledge_update as ku
import memory_cycle
from test_continuous_memory import NOW, fact, person, records, extraction, isolated  # noqa: F401


def test_history_read_success_is_separate_from_extraction_and_failed_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_cycle, 'PAGES_PER_RUN', 1)
    from history_coverage import learning_coverage
    from urllib.error import HTTPError
    monkeypatch.setattr(memory_cycle, 'allowed', lambda source: source == 'imessage')
    monkeypatch.setattr(memory_cycle.prewarm_graph, 'prewarm', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.style_extract, 'extract', lambda *a, **k: None)
    monkeypatch.setattr(memory_cycle.relationship_pulse, 'observe_history', lambda *a, **k: None)
    quiet = lambda **k: {'reviewed': 0}
    def fetch(*args):
        return {'status': 'ok', 'rows': [], 'next_cursor': 'next-page', 'complete': False}
    def failed_learn(*args, **kwargs):
        raise RuntimeError('PRIVATE extraction failure')
    memory_cycle.run(NOW, fetch, failed_learn, quiet)
    first = learning_coverage(tmp_path, ['imessage'])['sources']['imessage']
    assert first['last_read_success_at'] == NOW.isoformat()
    assert first['last_extraction_success_at'] is None
    assert first['latest_attempt']['status'] == 'failed'
    assert first['latest_attempt']['stage'] == 'memory_extraction'
    assert first['latest_attempt']['category'] == 'extraction_failed'
    def failed_fetch(*args):
        raise HTTPError('https://private.invalid', 503, 'PRIVATE provider body', {}, None)
    later = NOW + timedelta(hours=1)
    memory_cycle.run(later, failed_fetch, failed_learn, quiet)
    failed = learning_coverage(tmp_path, ['imessage'])['sources']['imessage']
    assert failed['last_read_success_at'] == NOW.isoformat()
    assert failed['last_extraction_success_at'] is None
    assert failed['latest_attempt'] == {'status': 'failed', 'at': later.isoformat(),
        'stage': 'fetch', 'category': 'service_unavailable'}
    # Retry backoff performs no read and must not refresh either attempted or successful time.
    memory_cycle.run(later + timedelta(minutes=1), failed_fetch, failed_learn, quiet)
    assert learning_coverage(tmp_path, ['imessage'])['sources']['imessage'] == failed
    recovered_at = later + timedelta(hours=1)
    memory_cycle.run(recovered_at, fetch, lambda *a, **k: {'reviewed': 0, 'facts': 0}, quiet)
    recovered = learning_coverage(tmp_path, ['imessage'])['sources']['imessage']
    assert recovered['last_read_success_at'] == recovered_at.isoformat()
    assert recovered['last_extraction_success_at'] == recovered_at.isoformat()
    assert recovered['latest_attempt'] == {'status': 'succeeded', 'at': recovered_at.isoformat()}
    assert 'PRIVATE' not in json.dumps(recovered)


@pytest.mark.parametrize('enabled, expected', [
    (('gmail',), ['gmail'] * 3),
    (('imessage', 'whatsapp', 'gmail'), ['imessage', 'whatsapp', 'gmail']),
    ((), []),
])
def test_history_page_allowance_counts_fetches_not_disabled_visits(monkeypatch, enabled, expected):
    monkeypatch.setattr(memory_cycle, 'allowed', lambda source: source in enabled)
    calls = []
    def fetch(source, window):
        calls.append(source)
        return {'status': 'ok', 'rows': [], 'next_cursor': len(calls), 'complete': False}
    receipt = memory_cycle.run(NOW, fetch, lambda *a, **k: {'reviewed': 0, 'facts': 0},
                               lambda **k: {'reviewed': 0})
    assert calls == expected
    assert len(receipt['history']) <= memory_cycle.PAGES_PER_RUN * len(memory_cycle.SOURCES)


def test_history_backoff_reassigns_pages_and_complete_source_stops_for_pass(monkeypatch):
    monkeypatch.setattr(memory_cycle, 'allowed', lambda source: source in ('imessage', 'gmail'))
    calls = []
    def fetch(source, window):
        calls.append(source)
        if source == 'imessage':
            raise OSError('unavailable')
        return {'status': 'ok', 'rows': [], 'next_cursor': len(calls), 'complete': False}
    quiet = lambda **k: {'reviewed': 0}
    learn = lambda *a, **k: {'reviewed': 0, 'facts': 0}
    memory_cycle.run(NOW, fetch, learn, quiet)
    assert calls == ['imessage', 'gmail', 'gmail']
    calls.clear()
    result = memory_cycle.run(NOW + timedelta(minutes=15), fetch, learn, quiet)
    assert any(row.get('source') == 'imessage' and row.get('error_code') == 'retry_backoff'
               for row in result['history'])
    assert calls == ['gmail'] * 3
    calls.clear()
    def complete(source, window):
        calls.append(source)
        return {'status': 'ok', 'rows': [], 'next_cursor': None, 'complete': True}
    memory_cycle.run(NOW + timedelta(minutes=30), complete, learn, quiet)
    assert calls == ['gmail']


def test_reclaimed_history_budget_stops_on_second_model_admission_hold(monkeypatch):
    from urllib.error import HTTPError
    monkeypatch.setattr(memory_cycle, 'allowed', lambda source: source == 'gmail')
    for target, method in ((memory_cycle.prewarm_graph, 'prewarm'), (memory_cycle.style_extract, 'extract'),
                           (memory_cycle.relationship_pulse, 'observe_history')):
        monkeypatch.setattr(target, method, lambda *a, **k: None)
    observed = records()
    monkeypatch.setattr(learning, 'observations', lambda *a, **kw: observed)
    fetches, models, curations = [], [], []
    blocked_model_call, expected_page_allowance = 2, 3
    def fetch(source, window):
        fetches.append(window['cursor'])
        return {'status': 'ok', 'rows': observed, 'next_cursor': len(fetches), 'complete': False}
    def model(*args, **kwargs):
        models.append(True)
        if len(models) == blocked_model_call:
            raise HTTPError('https://proxy.invalid/native', 402, 'PRIVATE', {}, None)
        return json.dumps(extraction())
    receipt = memory_cycle.run(NOW, fetch, lambda rows, now: learning.learn(rows, model, now),
                               lambda **kw: curations.append(True))
    window = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['gmail']
    assert memory_cycle.PAGES_PER_RUN == expected_page_allowance
    assert fetches == [None, 1] and len(models) == blocked_model_call and curations == []
    assert window['pages'] == 1 and window['cursor'] == 1
    assert receipt['dreamer'] == {'status': 'held', 'reason': 'background_budget_exhausted'}
    assert 'PRIVATE' not in json.dumps(receipt)


def test_memory_cycle_curates_healthy_person_and_reports_unreadable_count(monkeypatch):
    ku.apply(fact(), NOW)
    _, healthy = person()
    broken = Path(kg.people_dir()) / 'c_99999999999999999999.md'
    bad_bytes = b'---\nname: [PRIVATE malformed text\n---\n'
    broken.write_bytes(bad_bytes)
    monkeypatch.setattr(memory_cycle, 'allowed', lambda source: False)
    def model_call(kind, prompt, *args, **kwargs):
        assert kind == 'memory_curate'
        return json.dumps({'people': [{'id': row['id'], 'summary_refs': [], 'conflicts': [],
                                       'equivalent_groups': []} for row in json.loads(prompt)]})
    monkeypatch.setattr(dreamer.model_work, 'call', model_call)
    receipt = memory_cycle.run(NOW)
    assert receipt['dreamer']['reviewed'] == 1
    assert receipt['dreamer']['unreadable_people'] == 1
    assert broken.read_bytes() == bad_bytes
    assert 'PRIVATE' not in json.dumps(receipt)
    assert dreamer.prepare()['candidates'] == []
    assert healthy.canonical_id in receipt['dreamer']['results']
