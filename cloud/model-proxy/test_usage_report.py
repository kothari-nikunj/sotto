from datetime import datetime, timezone
import json
import sqlite3

import pytest
import server
import report


def test_metadata_is_content_free_and_unknown_is_explicit(monkeypatch):
    monkeypatch.setenv('RAILWAY_DEPLOYMENT_ID', 'deploy-a')
    payload = {'messages': [{'role': 'system', 'content': 'secret instructions'},
                            {'role': 'user', 'content': 'secret question'},
                            {'role': 'tool', 'content': 'secret result'}], 'tools': []}
    metadata = server.request_metadata({'X-Sotto-Workload': 'interactive\nforged'}, payload, 'chat')
    assert metadata['workload'] == 'unknown'
    assert metadata['proxy_deployment'] == 'deploy-a'
    assert metadata['deployment'] == 'unknown'
    assert 'secret' not in json.dumps(metadata)
    assert metadata['context_chars']['tool_results'] > 0


def test_report_keeps_unknown_usage_and_does_not_mutate_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(server.time, "time", lambda: 1789680000)
    path = tmp_path / 'calls.sqlite3'
    ledger = server.Ledger(path)
    complete = {'promptTokenCount': 1000, 'cachedContentTokenCount': 800,
                'candidatesTokenCount': 100, 'thoughtsTokenCount': 200}
    first = ledger.reserve('tenant', None, 'native', 'gemini-3.8-flash', metadata={
        'application': 'sotto', 'deployment': 'deploy-a', 'workload': 'notification', 'operation_id': 'a' * 64})
    ledger.finish(first, 200, complete)
    second = ledger.reserve('tenant', None, 'native', 'gemini-3.8-flash')
    ledger.finish(second, 500, None)
    # Keep pricing evaluation deterministic across test dates.
    with sqlite3.connect(path) as db:
        db.execute('UPDATE calls SET created=?', (1789680000,))
    before = path.read_bytes()
    result = report.report(path, 1, 'UTC', datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc))
    # Sep 17 data are outside Sep 18's one-day window.
    assert result['groups'] == []
    result = report.report(path, 2, 'UTC', datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc), billed_total=.01)
    unknown = next(g for g in result['groups'] if g['workload'] == 'unknown')
    assert unknown['input_tokens'] is None and unknown['cached_input_tokens'] is None
    assert unknown['known_token_cost'] is None and unknown['unknown_cost_requests'] == 1
    known = next(g for g in result['groups'] if g['workload'] == 'notification')
    assert known['cache_ratio'] == .8 and known['billed_output_tokens'] == 300
    assert known['attempts_per_operation'] == 1
    assert path.read_bytes() == before


def test_usage_migration_preserves_old_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(server.time, "time", lambda: 1789680000)
    path = tmp_path / 'old.sqlite3'
    ledger = server.Ledger(path)
    call = ledger.reserve('a', None, 'chat', 'gemini-3.8-flash')
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE calls DROP COLUMN usage_json')
        db.execute('ALTER TABLE calls DROP COLUMN metadata_json')
    ledger = server.Ledger(path)
    ledger.finish(call, 200, {'prompt_tokens': 100, 'completion_tokens': 30,
                             'prompt_tokens_details': {'cached_tokens': 80},
                             'completion_tokens_details': {'reasoning_tokens': 20}})
    with sqlite3.connect(path) as db:
        usage = json.loads(db.execute('SELECT usage_json FROM calls WHERE id=?', (call,)).fetchone()[0])
    assert usage['billed_output_tokens'] == 30 and usage['visible_output_tokens'] == 10
    assert usage['estimated_token_cost'] == pytest.approx(.0001335)


def test_chat_cost_bounds_and_authenticated_owner_are_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(server.time, 'time', lambda: 1789680000)
    path = tmp_path / 'calls.sqlite3'
    ledger = server.Ledger(path)
    metadata = server.request_metadata({}, {'messages': []}, 'chat',
                                       {'application': 'sotto', 'deployment': 'pilot'})
    for owner in ('owner-one', 'owner-two'):
        call = ledger.reserve(owner, None, 'chat', 'gemini-3.8-flash', metadata=metadata)
        ledger.finish(call, 200, {'prompt_tokens': 1000, 'completion_tokens': 300})
    result = report.report(path, 2, now=datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc))
    assert result['timezone'] == 'UTC'
    assert {g['owner'] for g in result['groups']} == {'owner-one', 'owner-two'}
    for group in result['groups']:
        assert group['deployment'] == 'pilot'
        assert group['known_token_cost'] is None
        assert group['bounded_token_cost'] == {'lower': .0012, 'upper': .001875}
        assert group['unbounded_cost_requests'] == 0


def test_legacy_rows_report_their_proxy_id_and_bounded_totals_never_double_count(tmp_path, monkeypatch):
    monkeypatch.setattr(server.time, 'time', lambda: 1789680000)
    path = tmp_path / 'calls.sqlite3'
    ledger = server.Ledger(path)
    # A row written before callers declared a deployment: `deployment` held the proxy's Railway id.
    legacy = ledger.reserve('owner', None, 'native', 'gemini-3.8-flash',
                            metadata={'application': 'sotto', 'deployment': 'railway-abc', 'workload': 'brief'})
    ledger.finish(legacy, 200, {'promptTokenCount': 1000, 'cachedContentTokenCount': 0,
                               'candidatesTokenCount': 100, 'thoughtsTokenCount': 0})
    chat = ledger.reserve('owner', None, 'chat', 'gemini-3.8-flash',
                          metadata=server.request_metadata({}, {'messages': []}, 'chat',
                                                           {'application': 'sotto', 'deployment': 'pilot'}))
    ledger.finish(chat, 200, {'prompt_tokens': 1000, 'completion_tokens': 300})
    result = report.report(path, 2, now=datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc), billed_total=.01)
    old = next(g for g in result['groups'] if g['workload'] == 'brief')
    assert old['deployment'] == 'unknown' and old['proxy_deployment'] == 'railway-abc'
    assert old['known_token_cost'] is not None and old['bounded_token_cost'] is None
    assert old['unbounded_cost_requests'] == 0
    new = next(g for g in result['groups'] if g['route'] == 'chat')
    assert new['known_token_cost'] is None and new['bounded_token_cost']['upper'] == .001875
    assert result['bounded_token_cost'] == {'lower': .0012, 'upper': .001875}
    assert result['billing_reconciliation']['residual_to_upper_bound'] == round(
        .01 - result['known_token_cost'] - .001875, 6)
