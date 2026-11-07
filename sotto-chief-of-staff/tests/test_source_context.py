"""Source truth: empty is observed, failures retain age, revocation gates every cached read."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

import compose_brief as cb
import gather_google as gg
import source_context as sc
import jsonstore


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)


def stamp(hours=0):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def cached_message():
    return {'generated_at': stamp(1), 'source_status': {'imessage': 'ok'},
            'imessage': [{'text': 'cached synthetic message'}]}


def test_derived_gmail_rows_obey_source_dependency_grants(monkeypatch):
    original = sc.allowed
    payload = {'emails': [
        {'id': 'recent', 'body': 'ordinary Gmail'},
        {'id': 'derived', 'body': 'older context',
         '_loop_context_sources': ['gmail', 'imessage']}],
        'truncated_at': 5}
    assert sc.email_dependency_sources(payload) == ['gmail', 'imessage']
    monkeypatch.setattr(sc, 'allowed', lambda source: source != 'imessage' and original(source))
    filtered = sc.project_emails(payload)
    assert [row['id'] for row in filtered['emails']] == ['recent']
    assert filtered['truncated_at'] == 5
    assert len(payload['emails']) == 2  # projection never rewrites the saved observation
    assert sc.used_sources(gmail=payload, already_projected=True) == ['gmail']
    monkeypatch.setattr(sc, 'allowed', lambda source: source != 'gmail' and original(source))
    assert sc.project_emails(payload) == []
    assert sc.used_sources(gmail=payload, already_projected=True) == ['gmail']


@pytest.mark.parametrize('mode', ['self-host', 'managed'])
def test_revocation_does_not_resurrect_snapshot_when_another_source_keeps_brief_ready(tmp_path, monkeypatch, mode):
    cb._save_local_snapshot(cached_message())
    if mode == 'managed':
        monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
        monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
        (tmp_path / 'config/managed-capabilities.json').write_text(json.dumps({
            'tenant_id': 'test', 'sources': {'calendar': {'consented': True, 'connected': True},
                                          'imessage': {'consented': False, 'connected': False}}}))
    local = {'generated_at': stamp(), 'source_status': {'imessage': 'disabled'}, 'imessage': []}
    assert cb._live_local_observation(local)
    current = cb._save_local_snapshot(local)
    assert not current.get('imessage')
    assert current['source_status']['imessage'] == 'disabled'
    assert not cb._local_fallback({}).get('imessage')


def test_valid_empty_read_clears_previous_rows_and_stays_empty_offline():
    cb._save_local_snapshot(cached_message())
    current = cb._save_local_snapshot({'generated_at': stamp(), 'source_status': {'imessage': 'ok'}, 'imessage': []})
    assert current['imessage'] == []
    assert '_local_stale_since' not in current
    assert not cb._local_fallback({}).get('imessage')


def test_partial_pull_drops_missing_old_rows_and_records_the_new_observation():
    old = stamp(cb.LOCAL_SNAPSHOT_TTL_HOURS + 1)
    cb._save_local_snapshot({'generated_at': old, 'source_status': {'imessage': 'ok', 'whatsapp': 'ok'},
                            'imessage': [{'text': 'expired'}], 'whatsapp': [{'text': 'old'}]})
    current = cb._save_local_snapshot({'generated_at': stamp(),
                                      'source_status': {'imessage': 'degraded', 'whatsapp': 'ok'},
                                      'imessage': [], 'whatsapp': [{'text': 'fresh'}]})
    assert not current.get('imessage')
    assert current['whatsapp'][0]['text'] == 'fresh'
    assert current['_source_observed_at']['imessage'] != old
    assert not cb._local_fallback({}).get('imessage')


def test_out_of_order_snapshot_does_not_roll_back_live_data():
    fresh = cached_message()
    fresh.update(generated_at=stamp(), imessage=[{'text': 'newest'}])
    cb._save_local_snapshot(fresh)
    returned = cb._save_local_snapshot(cached_message())
    assert returned['imessage'][0]['text'] == 'newest'
    assert cb._local_fallback({})['imessage'][0]['text'] == 'newest'


def test_self_host_status_receipt_records_source_revocation_without_storing_history(tmp_path):
    now = datetime.now(timezone.utc)
    sc.record_bridge_status({'generated_at': now.isoformat(), 'sources': {'imessage': 'disabled'}})
    assert not sc.allowed('imessage')
    # A slower earlier read cannot re-enable the source; a newer explicit health state can.
    sc.record_bridge_status({'generated_at': stamp(1), 'source_status': {'imessage': 'ok'}})
    assert not sc.allowed('imessage')
    sc.record_bridge_status({'generated_at': (now + timedelta(seconds=1)).isoformat(), 'sources': {'imessage': 'ok'}})
    assert sc.allowed('imessage')
    state = json.loads((tmp_path / 'config/source-state.json').read_text())
    assert 'episodes' not in json.dumps(state)


def test_revocation_erases_finished_learning_diagnostic_even_without_another_pass(tmp_path):
    path = tmp_path / 'events/learning-diagnostics/brief-old.json'
    path.parent.mkdir(parents=True)
    # Schema 1 has no complete source manifest. Older diagnostics must be erased too.
    path.write_text(json.dumps({'schema': 1, 'rejected': [{'evidence_snippets': ['private']}]}))
    sc.record_bridge_status({'sources': {'imessage': 'disabled'}})
    assert not path.exists()
    assert not sc.allowed('imessage')

    # A later status report for the already-disabled source must not erase new diagnostics
    # about a different, still-allowed source.
    path.write_text('{"schema": 1, "rejected": [{"evidence_snippets": ["other source"]}]}')
    sc.record_bridge_status({'sources': {'imessage': 'disabled'}})
    assert path.exists()


def test_revocation_waits_for_diagnostic_creation_inside_consent_gate(tmp_path):
    path = tmp_path / 'events/learning-diagnostics/brief-late.json'
    path.parent.mkdir(parents=True)
    gate = str(path.parent / '.consent')
    errors = []

    def disable():
        try:
            sc.record_bridge_status({'sources': {'imessage': 'disabled'}})
        except Exception as error:
            errors.append(error)

    with jsonstore.lock(gate):
        thread = threading.Thread(target=disable)
        thread.start()
        # The new consent is durable before the cleaner takes the directory gate.
        for _ in range(100):
            if not sc.allowed('imessage'):
                break
            threading.Event().wait(0.01)
        assert not sc.allowed('imessage')
        path.write_text('{"schema": 1, "rejected": [{"evidence_snippets": ["private"]}]}')
        assert thread.is_alive()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert not path.exists()


def test_google_source_receipts_distinguish_failed_empty_from_complete_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(gg, '_find_google_api', lambda: 'synthetic-api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, '_refresh_google_token', lambda: None)
    outputs = [str(tmp_path / name) for name in ('gmail.json', 'cal.json', 'sources.json')]
    monkeypatch.setattr('sys.argv', ['gather_google', '--skip-gmail', '--gmail-out', outputs[0],
                                    '--cal-out', outputs[1], '--source-results-out', outputs[2]])
    def failed(*a, **k):
        raise RuntimeError('synthetic provider failure')
    monkeypatch.setattr(gg, 'gather_calendar', failed)
    gg.main()
    assert json.loads((tmp_path / 'cal.json').read_text()) == []  # legacy shape retained
    observation = json.loads((tmp_path / 'sources.json').read_text())['calendar']
    assert observation['status'] == 'unavailable' and observation['complete'] is False
    assert observation['error'] == 'RuntimeError'
    def empty(*a, observation=None, **k):
        observation.update(complete=True, coverage={'since': stamp(), 'until': stamp(-72)})
        return []
    monkeypatch.setattr(gg, 'gather_calendar', empty)
    gg.main()
    observation = json.loads((tmp_path / 'sources.json').read_text())['calendar']
    assert observation['status'] == 'ok' and observation['complete'] is True
    assert json.loads((tmp_path / 'cal.json').read_text()) == []


def test_source_failure_reaches_brief_availability_even_when_no_rows():
    local = cb._normalize_local({'source_results': {'calendar': sc.source_result('unavailable')}})
    assert local['_source_availability']['calendar'] == 'unavailable'


def test_delivery_source_manifest_includes_stale_email_threads_without_current_inbox_rows():
    assert sc.used_sources(gmail={'emails': [], 'staleThreads': [{'id': 'synthetic-thread'}]}) == ['gmail']


def test_permission_fingerprint_changes_only_when_source_permission_changes():
    before = sc.permission_fingerprint()
    sc.record_bridge_status({'sources': {'imessage': 'unavailable'}})
    assert sc.permission_fingerprint() == before  # availability is not revocation
    sc.record_bridge_status({'sources': {'imessage': 'disabled'}})
    assert sc.permission_fingerprint() != before


@pytest.mark.parametrize('reply,code', [
    ({'error': {'code': -32000, 'message': 'PRIVATE'}}, 'bridge_offline'),
    ({'error': {'code': -32001, 'message': 'PRIVATE'}}, 'bridge_timeout'),
    ({'error': {'code': -32002, 'message': 'PRIVATE'}}, 'bridge_not_permitted'),
    ({'error': {'code': -999, 'message': 'PRIVATE'}}, 'bridge_rpc_error'),
    ({'result': {'isError': True, 'structuredContent': {'error_code': 'bridge_offline'}}}, 'bridge_offline'),
    ({'result': {'isError': True, 'content': [{'text': 'PRIVATE'}]}}, 'bridge_tool_error'),
])
def test_bridge_errors_keep_only_fixed_codes(monkeypatch, reply, code):
    import io
    monkeypatch.setenv('BRIDGE_TOKEN', 'synthetic')
    monkeypatch.setattr(sc.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(json.dumps(reply).encode()))
    with pytest.raises(sc.BridgeReadError) as caught:
        sc.bridge_call('read_history', {})
    assert caught.value.code == code and 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('error,code', [
    (sc.BridgeReadError('bridge_timeout'), 'bridge_timeout'),
    (RuntimeError('private source body must not be printed'), 'bridge_read_failed'),
])
def test_local_read_failure_logs_only_bounded_reason(monkeypatch, error, code):
    import sotto_log
    messages = []
    monkeypatch.setattr(sotto_log, 'diag', messages.append)
    def fail(*args):
        raise error
    monkeypatch.setattr(sc, 'bridge_call', fail)
    assert sc.read_local() == {}
    assert messages == [f'[read_local] {code}; using permitted snapshot fallback']


def test_revoked_excerpt_source_strips_only_the_excerpt_from_an_inbox_row(monkeypatch):
    original = sc.allowed
    inbox = {'id': 'm', 'body': 'ordinary Gmail', 'labelIds': ['INBOX'], '_loop_context': True,
             '_loop_context_body': 'steered excerpt', '_loop_context_body_sources': ['gmail', 'whatsapp']}
    assert sc.email_dependency_sources([inbox]) == ['gmail', 'whatsapp']
    assert sc.project_emails([inbox]) == [inbox]
    monkeypatch.setattr(sc, 'allowed', lambda source: source != 'whatsapp' and original(source))
    assert sc.project_emails([inbox]) == [{'id': 'm', 'body': 'ordinary Gmail', 'labelIds': ['INBOX']}]
    assert sc.email_dependency_sources(sc.project_emails([inbox])) == []


def test_email_evidence_parts_keep_body_and_excerpt_separate_for_quote_matching(monkeypatch):
    original = sc.allowed
    row = {'id': 'm', 'body': 'Plain text: will do next week.', '_loop_context': True,
           '_loop_context_body': '<div>Attached is the deck.</div>',
           '_loop_context_body_sources': ['gmail', 'whatsapp']}
    assert sc.email_evidence_parts(row) == [row['body'], row['_loop_context_body']]
    # The prompt rendering still shows both, labelled; quote matching never spans the label.
    assert '[Additional excerpt from the same email]' in sc.email_evidence_text(row)
    assert sc.email_quote_found(row, 'Attached is the deck.')
    assert not sc.email_quote_found(row, 'next week. [Additional excerpt from the same email] <div>')
    # One read containing the other is a single part (no label at all).
    assert sc.email_evidence_parts({**row, '_loop_context_body': 'will do next week'}) == [row['body']]
    # Revoking the steering source drops only the excerpt part.
    monkeypatch.setattr(sc, 'allowed', lambda source: source != 'whatsapp' and original(source))
    assert sc.email_evidence_parts(row) == [row['body']]
    assert not sc.email_quote_found(row, 'Attached is the deck.')


def test_revoked_source_loses_coverage_as_well_as_content(monkeypatch):
    monkeypatch.setattr(sc, 'allowed', lambda source: source == 'imessage')
    original = {'source_coverage': {'imessage': {'rows_scanned': 3},
                                   'whatsapp': {'rows_scanned': 4},
                                   'deferred_unread_whatsapp': {'rows_scanned': 5}}}
    assert sc.project_local(original)['source_coverage'] == {'imessage': {'rows_scanned': 3}}
    assert len(original['source_coverage']) == 3


def test_successful_empty_google_read_does_not_request_reconnection():
    receipts = {source: sc.source_result('ok', complete=True) for source in ('gmail', 'calendar')}
    inputs = {'first_run': True, 'google': {'emails': [], 'events': []}, 'source_results': receipts}
    local = cb._normalize_local(inputs)
    assert local['_source_availability']['gmail'] == 'available'
    assert local['_source_availability']['calendar'] == 'available'
    note = cb._coverage_line(local, local['_source_availability'], [], [])
    assert 'Gmail + Calendar' not in note
    assert 'your email and calendar' in note


def test_real_read_retains_coverage_separate_from_health_and_failures():
    read_at = stamp(1)
    detail = {'status': 'ok', 'observed_at': read_at, 'rows_scanned': 0,
              'scan_observed': True, 'limit_reached': False, 'content_partial': False, 'bounded': True}
    sc.record_bridge_status({'generated_at': read_at, 'source_status': {'imessage': 'ok'},
                            'imessage': [], 'deferred_unread_imessage': [],
                            'source_coverage': {'imessage': detail}})
    item = next(row for row in sc.source_health()['sources'] if row['source'] == 'imessage')
    assert item['status'] == 'empty'
    assert item['coverage'] == detail
    assert item['last_successful_read_at'] == read_at
    sc.record_bridge_status({'generated_at': stamp(.5), 'sources': {'imessage': 'ok'}})
    sc.record_bridge_status({'generated_at': stamp(), 'source_status': {'imessage': 'unavailable'},
                            'imessage': [], 'deferred_unread_imessage': []})
    item = next(row for row in sc.source_health()['sources'] if row['source'] == 'imessage')
    assert item['status'] == 'unavailable'
    assert item['last_successful_read_at'] == read_at


def test_unread_receipt_is_not_summarized_as_successful_empty():
    sc.record_bridge_status({'generated_at': stamp(), 'source_status': {'imessage': 'ok'},
        'imessage': [], 'deferred_unread_imessage': [], 'source_coverage': {'imessage': {
            'status': 'unread', 'last_successful_read_at': None, 'rows_returned': 0}}})
    item = next(row for row in sc.source_health()['sources'] if row['source'] == 'imessage')
    assert item['status'] == 'unverified'
    assert item['coverage']['status'] == 'unread'
    assert item['last_successful_read_at'] is None


def rich_coverage(at, *, status='partial'):
    return {'status': status, 'observed_at': at, 'rows_scanned': 100, 'scan_observed': True,
            'limit_reached': True, 'content_partial': False, 'bounded': True,
            'requested_window': {'since': stamp(24), 'until': at},
            'effective_window': {'since': stamp(24), 'until': at, 'basis': 'sent'},
            'last_successful_read_at': at, 'rows_returned': 1, 'query_row_caps': [100],
            'omitted_rows': None, 'failure_category': None,
            'components': {'deferred_unread': {'status': 'failed', 'failure_category': 'query'}},
            'attachments': {'metadata': 'available', 'body': 'not_read'}}


def test_partial_primary_and_failed_companion_keep_independent_coverage_and_success(tmp_path):
    at = stamp(1)
    primary = rich_coverage(at)
    companion = {'status': 'failed', 'failure_category': 'query', 'last_successful_read_at': None}
    sc.record_bridge_status({'generated_at': at, 'source_status': {
        'imessage': 'ok', 'deferred_unread_imessage': 'degraded'},
        'imessage': [{'text': 'usable primary result'}], 'deferred_unread_imessage': [],
        'source_coverage': {'imessage': primary, 'deferred_unread_imessage': companion}})
    item = next(row for row in sc.source_health()['sources'] if row['source'] == 'imessage')
    assert item['status'] == 'partial' and item['field_counts']['imessage'] == 1
    assert item['coverage'] == primary and item['deferred_unread_coverage'] == companion
    assert item['last_successful_read_at'] == at
    # A late older read and a later health probe cannot overwrite extraction evidence.
    sc.record_bridge_status({'generated_at': stamp(2), 'source_status': {'imessage': 'ok'},
        'imessage': [], 'deferred_unread_imessage': [], 'source_coverage': {'imessage': {'status': 'empty'}}})
    sc.record_bridge_status({'generated_at': stamp(), 'sources': {'imessage': 'ok'}})
    again = next(row for row in sc.source_health()['sources'] if row['source'] == 'imessage')
    assert again['coverage'] == primary and again['last_successful_read_at'] == at
    stored = json.loads((tmp_path / 'config/source-state.json').read_text())
    assert 'usable primary result' not in json.dumps(stored)
    sc.record_bridge_status({'sources': {'imessage': 'disabled'}})
    disabled = next(row for row in sc.source_health()['sources'] if row['source'] == 'imessage')
    assert 'coverage' not in disabled and 'last_successful_read_at' not in disabled


def test_rich_coverage_reaches_prompt_as_bounded_read_evidence():
    detail = rich_coverage(stamp())
    text = cb._collection_limits({'source_coverage': {'imessage': detail}})
    assert 'read status: partial' in text and 'requested window:' in text and 'effective window:' in text
    assert 'last successful read:' in text and '1 records returned' in text and 'per-query row caps: 100' in text
    assert 'omitted records: unknown' in text and 'deferred_unread: failed (query)' in text
    assert 'attachment bodies not read' in text and 'do not infer absence' in text
    malicious = {**detail, 'failure_category': 'private provider body'}
    assert cb._collection_limits({'source_coverage': {'imessage': malicious}}) == ''


def test_component_measurements_reach_prompt_without_becoming_parent_coverage():
    detail = rich_coverage(stamp())
    component = detail['components']['deferred_unread']
    component.update(requested_window={'since': stamp(720), 'until': stamp(24)},
                     effective_window={'since': stamp(480), 'until': stamp(24), 'basis': 'sent'},
                     last_successful_read_at=stamp(25), query_row_caps=[12], rows_returned=2, omitted_rows=7)
    text = cb._collection_limits({'source_coverage': {'imessage': detail}})
    assert component['requested_window']['since'] in text
    assert component['effective_window']['since'] in text
    assert component['last_successful_read_at'] in text
    assert 'per-query row caps: 12' in text and 'omitted records: 7' in text
    assert '2 records returned' in text and '1 records returned' in text


def test_healthy_measured_reads_are_not_presented_as_collection_gaps():
    detail = rich_coverage(stamp())
    detail.update(status='ok', omitted_rows=0, limit_reached=False, content_partial=False,
                  failure_category=None, components={}, attachments={'metadata': 'available', 'body': 'not_read'})
    assert cb._collection_limits({'source_coverage': {'imessage': detail}}) == ''
    detail.update(status='empty', rows_returned=0)
    assert cb._collection_limits({'source_coverage': {'imessage': detail}}) == ''
    detail['omitted_rows'] = None
    assert 'omitted records: unknown' in cb._collection_limits({'source_coverage': {'imessage': detail}})
    detail['omitted_rows'] = 0
    detail['components'] = {'deferred_unread': {'status': 'failed', 'failure_category': 'query',
                                               'last_successful_read_at': None}}
    assert 'deferred_unread: failed' in cb._collection_limits({'source_coverage': {'imessage': detail}})


@pytest.mark.parametrize('detail', [None, 'invalid', {'status': 'failed', 'private': 'do not retain'}])
def test_invalid_present_coverage_cannot_refresh_success(detail):
    prior = stamp(1)
    sc.record_bridge_status({'generated_at': prior, 'source_status': {'imessage': 'ok'},
                            'imessage': [], 'deferred_unread_imessage': []})
    sc.record_bridge_status({'generated_at': stamp(), 'source_status': {'imessage': 'ok'},
                            'imessage': [], 'deferred_unread_imessage': [],
                            'source_coverage': {'imessage': detail}})
    item = next(row for row in sc.source_health()['sources'] if row['source'] == 'imessage')
    assert item['last_successful_read_at'] == prior
    assert item['status'] == 'partial'
    assert 'do not retain' not in json.dumps(item)


@pytest.mark.parametrize('coverage', [None, [], 'invalid'])
def test_malformed_coverage_container_cannot_refresh_success(coverage):
    prior = stamp(1)
    sc.record_bridge_status({'generated_at': prior, 'source_status': {'imessage': 'ok'},
                            'imessage': [], 'deferred_unread_imessage': []})
    sc.record_bridge_status({'generated_at': stamp(), 'source_status': {'imessage': 'ok'},
                            'imessage': [], 'deferred_unread_imessage': [],
                            'source_coverage': coverage})
    item = next(row for row in sc.source_health()['sources'] if row['source'] == 'imessage')
    assert item['last_successful_read_at'] == prior
    assert item['status'] == 'partial'
