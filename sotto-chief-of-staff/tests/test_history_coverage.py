"""Content-free extraction coverage uses current grants and keeps source-wide scope."""
import json
import os
import subprocess
import sys

import pytest

import history_coverage as coverage


def _window(**changes):
    return {'since': 1791936000, 'until': 1792022400, 'complete': True,
            'initial_since': 1791936000, 'initial_until': 1792022400,
            'initial_complete': True, 'pages': 3, 'window_page_base': 2,
            'cursor': 'PRIVATE-CURSOR', 'error': 'PRIVATE-MESSAGE',
            'observation_totals': dict(pages=1, rows=20, eligible=20, selected=10,
                skipped=0, omitted=10, truncated=2, rejected_people=0), **changes}


def _write(root, sources):
    path = root / 'knowledge/history-state.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({'sources': sources}))
    return path


def test_learning_coverage_keeps_initial_gaps_separate_and_never_copies_private_state(tmp_path):
    window = _window(since=1792022400, until=1792108800,
        observation_totals=dict(pages=1, rows=1, eligible=1, selected=1,
            skipped=0, omitted=0, truncated=0, rejected_people=0),
        initial_coverage=_window())
    _write(tmp_path, {'gmail': window, 'whatsapp': _window()})
    result = coverage.learning_coverage(tmp_path, ['gmail'])
    assert set(result['sources']) == {'gmail', 'imessage', 'whatsapp'}
    gmail = result['sources']['gmail']
    assert gmail['checkpoint'] == 'present' and gmail['review_scope'] == 'reviewed_window'
    assert gmail['initial_review']['review_scope'] == 'partial'
    assert gmail['initial_review']['observation_counts']['omitted'] == 10
    assert gmail['outside_window_review'] == 'unknown'
    assert result['sources']['whatsapp'] == {'checkpoint': 'withheld'}
    assert result['sources']['imessage'] == {'checkpoint': 'withheld'}
    assert 'not person- or topic-specific completeness' in result['scope']
    assert 'PRIVATE' not in json.dumps(result)


def test_learning_coverage_reads_one_checkpoint_and_no_file_for_withheld_sources(tmp_path, monkeypatch):
    calls = []
    def read(root):
        calls.append(root)
        return 'available', {'gmail': _window(), 'whatsapp': _window()}
    monkeypatch.setattr(coverage, 'read_state', read)
    withheld = coverage.learning_coverage(tmp_path, [])
    assert calls == []
    assert all(value == {'checkpoint': 'withheld'} for value in withheld['sources'].values())
    result = coverage.learning_coverage(tmp_path, ['gmail', 'imessage'])
    assert calls == [tmp_path]
    assert result['sources']['gmail']['review_scope'] == 'partial'
    assert result['sources']['imessage'] == {'checkpoint': 'absent'}
    assert result['sources']['whatsapp'] == {'checkpoint': 'withheld'}


def test_learning_coverage_unknown_and_legacy_do_not_become_measured_completeness(tmp_path):
    absent = coverage.learning_coverage(tmp_path, ['gmail'])
    assert absent['sources']['gmail'] == {'checkpoint': 'absent'}
    path = _write(tmp_path, {'gmail': {'complete': True}})
    legacy = coverage.learning_coverage(tmp_path, ['gmail'])
    assert legacy['sources']['gmail']['review_scope'] == 'unknown'
    path.write_text('{broken')
    unknown = coverage.learning_coverage(tmp_path, ['gmail'])
    assert unknown['sources']['gmail'] == {'checkpoint': 'unknown'}
    assert unknown['sources']['whatsapp'] == absent['sources']['whatsapp']


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'directory', 'oversized', 'invalid_utf8'])
def test_checkpoint_read_refuses_nonregular_or_oversized_inputs_without_blocking(tmp_path, monkeypatch, kind):
    path = tmp_path / 'knowledge/history-state.json'
    path.parent.mkdir()
    monkeypatch.setattr(coverage, 'MAX_STATE_BYTES', 128)
    if kind == 'symlink':
        target = tmp_path / 'target.json'
        target.write_text('{"sources":{"gmail":{}}}')
        path.symlink_to(target)
    elif kind == 'fifo':
        os.mkfifo(path)
        # A regression to a blocking open must fail this test, not hang the whole suite.
        code = 'import history_coverage,json,sys; print(json.dumps(history_coverage.read_state(sys.argv[1])))'
        result = subprocess.check_output([sys.executable, '-c', code, str(tmp_path)], timeout=3,
            env={**os.environ, 'PYTHONPATH': os.path.dirname(coverage.__file__)}, text=True)
        assert json.loads(result) == ['unknown', {}]
        return
    elif kind == 'directory':
        path.mkdir()
    elif kind == 'oversized':
        # Fewer than 128 characters, more than 128 encoded bytes.
        path.write_text(json.dumps({'sources': {}, 'padding': '界' * 40}, ensure_ascii=False))
    else:
        path.write_bytes(b'\xff')
    assert coverage.read_state(tmp_path) == ('unknown', {})


def test_learning_metadata_has_its_own_small_bound_even_with_maximum_counters(tmp_path):
    totals = dict.fromkeys(('pages', 'rows', 'eligible', 'selected', 'skipped', 'omitted',
                            'truncated', 'rejected_people'), 1_000_000_000)
    window = _window(since=0, until=253402300799, initial_since=0, initial_until=253402300799,
        pages=1_000_000_000, window_page_base=0, excluded_page_count=1_000_000_000,
        observation_totals=totals,
        last_attempt={'status': 'failed', 'at': '9999-12-31T23:59:59.999999+00:00', 'stage': 'memory_extraction'},
        last_read_success='9999-12-31T23:59:59.999999+00:00',
        last_success='9999-12-31T23:59:59.999999+00:00')
    _write(tmp_path, {source: window for source in ('gmail', 'imessage', 'whatsapp')})
    result = coverage.learning_coverage(tmp_path, ['gmail', 'imessage', 'whatsapp'])
    assert all(row['checkpoint'] == 'present' for row in result['sources'].values())
    assert len(json.dumps(result).encode()) <= coverage.MAX_LEARNING_BYTES
    assert 'other sources are unmeasured' in result['scope']
    assert 'absent checkpoint means no saved measurement, not no history' in result['scope']
    assert all(row['latest_attempt']['status'] == 'failed' for row in result['sources'].values())
    if result.get('detail_omitted'):
        assert result['review_scope'] == 'unknown'
        assert all(row['last_read_success_at'] == window['last_read_success']
                   for row in result['sources'].values())
    else:
        assert all(row['initial_review']['observation_counts_omitted'] for row in result['sources'].values())


def test_latest_learning_failure_is_visible_without_exposing_diagnostics(tmp_path):
    _write(tmp_path, {'gmail': _window(complete=False,
        last_attempt={'at': '2026-10-15T10:00:00Z', 'status': 'failed', 'stage': 'fetch'},
        last_read_success='2026-10-14T09:00:00Z', last_success='2026-10-14T09:01:00Z',
        http_status=503, error='PRIVATE-MESSAGE', error_code='PRIVATE-CURSOR')})
    view = coverage.learning_coverage(tmp_path, ['gmail'])['sources']['gmail']
    assert view['latest_attempt'] == {'at': '2026-10-15T10:00:00+00:00',
        'status': 'failed', 'stage': 'fetch', 'category': 'service_unavailable'}
    assert view['last_read_success_at'] == '2026-10-14T09:00:00+00:00'
    assert view['last_extraction_success_at'] == '2026-10-14T09:01:00+00:00'
    assert 'PRIVATE' not in json.dumps(view)
    assert 'latest_attempt' not in view['initial_review']


def test_learning_attempt_legacy_and_invalid_fields_do_not_invent_success(tmp_path):
    _write(tmp_path, {'gmail': {'pages': 0}, 'imessage': {'pages': 3},
        'whatsapp': {'last_attempt': {'at': 'PRIVATE', 'status': 'PRIVATE', 'stage': 'PRIVATE'},
                     'last_success': 'PRIVATE'}})
    result = coverage.learning_coverage(tmp_path, ['gmail', 'imessage', 'whatsapp'])['sources']
    assert result['gmail']['latest_attempt'] == {'status': 'unread'}
    assert result['imessage']['latest_attempt'] == {'status': 'unknown'}
    assert result['whatsapp']['latest_attempt'] == {'status': 'unknown'}
    assert result['whatsapp']['last_extraction_success_at'] is None
    assert 'PRIVATE' not in json.dumps(result)


def test_final_bounded_projection_retains_latest_failure(tmp_path, monkeypatch):
    window = _window(last_attempt={'at': '2026-10-15T10:00:00Z',
        'status': 'failed', 'stage': 'memory_extraction'},
        provider_failure='unsupported_model_settings', http_status=400)
    _write(tmp_path, {source: window for source in ('gmail', 'imessage', 'whatsapp')})
    # Force the final compaction path, rather than relying on today's field lengths.
    monkeypatch.setattr(coverage, 'MAX_LEARNING_BYTES', 1600)
    result = coverage.learning_coverage(tmp_path, ['gmail', 'imessage', 'whatsapp'])
    assert result['detail_omitted'] is True
    assert len(json.dumps(result).encode()) <= coverage.MAX_LEARNING_BYTES
    assert all(row['checkpoint'] == 'present' and row['latest_attempt']['status'] == 'failed'
               and row['latest_attempt']['category'] == 'unsupported_model_settings'
               for row in result['sources'].values())

    assert result['review_scope'] == 'unknown'
    assert all('initial_traversal_complete' not in row for row in result['sources'].values())


def test_production_bound_with_maximum_validated_projection(tmp_path, monkeypatch):
    totals = dict.fromkeys(('pages', 'rows', 'eligible', 'selected', 'skipped', 'omitted',
                            'truncated', 'rejected_people'), 1_000_000_000)
    stamp = '9999-12-31T23:59:59.999999+00:00'
    window = _window(since=0, until=253402300799, initial_since=0, initial_until=253402300799,
        pages=1_000_000_000, window_page_base=0, excluded_page_count=1_000_000_000,
        observation_totals=totals, provider_failure='extraction_validation_failed',
        last_attempt={'status': 'failed', 'at': stamp, 'stage': 'memory_extraction'},
        last_read_success=stamp, last_success=stamp)
    _write(tmp_path, {source: window for source in ('gmail', 'imessage', 'whatsapp')})
    production = coverage.learning_coverage(tmp_path, ['gmail', 'imessage', 'whatsapp'])
    assert len(json.dumps(production).encode()) <= 4096 == coverage.MAX_LEARNING_BYTES
    # Force final compaction on the same longest retained fields, and measure that
    # shape independently of the artificial trigger used to reach it.
    monkeypatch.setattr(coverage, 'MAX_LEARNING_BYTES', 1)
    compact = coverage.learning_coverage(tmp_path, ['gmail', 'imessage', 'whatsapp'])
    assert compact['detail_omitted'] is True
    assert len(json.dumps(compact).encode()) <= 1600
    assert all(row['latest_attempt']['category'] == 'extraction_validation_failed'
               for row in compact['sources'].values())


def test_attempt_enum_combinations_stay_within_production_projection_bound(monkeypatch):
    stamp = '9999-12-31T23:59:59.999999+00:00'
    provider_categories = ('malformed_request', 'unsupported_model_settings', 'input_too_large',
        'output_limit', 'authentication_failed', 'authorization_denied', 'allowance_exhausted',
        'rate_limited', 'service_unavailable', 'timeout', 'provider_rejected',
        'extraction_validation_failed')
    diagnostics = ([{'provider_failure': value} for value in provider_categories]
        + [{'http_status': value} for value in (400, 401, 402, 403, 408, 413, 422, 429, 500, 503, 504, 599)]
        + [{'error_code': value} for value in ('bridge_offline', 'bridge_not_configured',
            'source_unavailable', 'source_not_allowed', 'source_consent_changed', 'bridge_timeout')]
        + [{}])
    maximum = 0
    for status in ('succeeded', 'failed', 'held', 'excluded', 'unknown'):
        for stage in ('fetch', 'local_learning', 'memory_extraction'):
            for diagnostic in diagnostics:
                window = _window(last_attempt={'status': status, 'stage': stage, 'at': stamp,
                    'reason': 'background_budget_exhausted'}, last_read_success=stamp,
                    last_success=stamp, **diagnostic)
                sources = {source: window for source in ('gmail', 'imessage', 'whatsapp')}
                monkeypatch.setattr(coverage, 'read_state', lambda root: ('present', sources))
                # The artificial threshold only selects the compact shape; the contract
                # under test is the real production bound, not arbitrary tiny thresholds.
                monkeypatch.setattr(coverage, 'MAX_LEARNING_BYTES', 1)
                result = coverage.learning_coverage('/unused', list(sources))
                maximum = max(maximum, len(json.dumps(result).encode()))
    assert 0 < maximum <= 1600 < 4096


def test_requested_model_window_does_not_claim_failed_interval_was_read(tmp_path):
    since, until = 1792022400, 1792108800
    initial = _window()
    window = _window(since=since, until=until, complete=False, pages=3,
        window_page_base=3, observation_totals=None, initial_coverage=initial,
        last_attempt={'at': '2026-10-16T00:00:00Z', 'status': 'failed', 'stage': 'fetch'},
        last_read_success='2026-10-15T00:00:00Z', last_success='2026-10-15T00:00:00Z')
    _write(tmp_path, {'gmail': window})
    result = coverage.learning_coverage(tmp_path, ['gmail'])
    view = result['sources']['gmail']
    assert 'window' not in view and 'window' not in view['initial_review']
    assert view['requested_window']['since'] == view['last_read_success_at']
    assert view['review_scope'] == 'unknown'
    assert view['initial_traversal_complete'] is True
    assert view['initial_review']['requested_window']['until'] == view['requested_window']['since']
    assert view['latest_attempt']['status'] == 'failed'
    assert view['latest_attempt']['stage'] == 'fetch'
    assert view['last_extraction_success_at'] == view['last_read_success_at']
    assert view['outside_window_review'] == 'unknown'
    assert len(json.dumps(result).encode()) <= coverage.MAX_LEARNING_BYTES
    # Dashboard callers retain the original API and the same underlying evidence.
    dashboard = coverage.window_coverage(window)
    assert dashboard['window'] == view['requested_window']
    assert dashboard['initial_review']['window'] == view['initial_review']['requested_window']
    assert 'requested_window' not in dashboard


def test_compacted_requested_windows_preserve_prior_success_and_failed_attempt(tmp_path, monkeypatch):
    stamp = '9999-12-31T23:59:59.999999+00:00'
    window = _window(last_attempt={'at': stamp, 'status': 'failed', 'stage': 'fetch'},
        last_read_success=stamp, last_success=stamp)
    _write(tmp_path, {source: window for source in ('gmail', 'imessage', 'whatsapp')})
    monkeypatch.setattr(coverage, 'MAX_LEARNING_BYTES', 1600)
    result = coverage.learning_coverage(tmp_path, ['gmail', 'imessage', 'whatsapp'])
    assert result['detail_omitted'] is True and result['review_scope'] == 'unknown'
    assert len(json.dumps(result).encode()) <= coverage.MAX_LEARNING_BYTES
    for row in result['sources'].values():
        assert row['checkpoint'] == 'present'
        assert row['latest_attempt']['status'] == 'failed'
        assert row['last_read_success_at'] == stamp
        assert row['last_extraction_success_at'] == stamp
        assert 'initial_traversal_complete' not in row
        assert 'requested_window' not in row
