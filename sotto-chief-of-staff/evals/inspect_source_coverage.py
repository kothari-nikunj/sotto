#!/usr/bin/env python3
"""Inspect synthetic coverage through existing receiver/state/prompt boundaries; no model or network."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone


DENIED = []


def deny_external(event, _args):
    if event.startswith('socket.') or event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.exec'}:
        DENIED.append(event)
        raise RuntimeError('unexpected external operation in isolated coverage inspection')


# Install before production imports; caught exceptions still fail the final inspection.
sys.addaudithook(deny_external)
PACK = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PACK / '_shared/lib'), str(PACK / '_shared/scripts'),
                str(PACK.parent / 'runtime/trigger-receiver')]
import compose_brief as brief  # noqa: E402
import history_coverage  # noqa: E402
import managed  # noqa: E402
import source_context  # noqa: E402


AT = '2026-10-20T08:00:00+00:00'
LATER = '2026-10-20T09:00:00+00:00'


def inspect_rust_receipt():
    """A captured real MacHandlers read of invented SQLite databases, shared with the TS test."""
    payload = json.loads((PACK.parent / 'runtime/sotto-chat/test/fixtures/source-coverage-v2.json').read_text())
    with tempfile.TemporaryDirectory(prefix='sotto-rust-coverage-inspection-') as directory:
        root = Path(directory)
        os.environ['SOTTO_DATA'] = str(root)
        (root / 'config').mkdir()
        (root / 'config/managed-capabilities.json').write_text(json.dumps({
            'tenant_id': 'synthetic', 'sources': {source: {'consented': True, 'connected': True}
                for source in payload['source_coverage'] if not source.startswith('deferred_unread_')}}))
        managed.validate_local_ingress(root, {'local_data': payload})
        source_context.record_bridge_status(payload)
        health = source_context.source_health(now=datetime.fromisoformat(payload['generated_at'].replace('Z', '+00:00')))
        chrome = next(row for row in health['sources'] if row['source'] == 'chrome')
        assert chrome['status'] == 'partial'
        assert chrome['coverage']['components']['search_queries']['status'] == 'failed'
        assert chrome['coverage']['omitted_rows'] is None
        assert payload['source_coverage']['reminders']['effective_window']['basis'] == 'due_or_created'
        return {'payload': payload, 'health': health, 'brief_model_input': brief._collection_limits(payload)}


def inspect(root):
    os.environ.update(SOTTO_DATA=str(root), SOTTO_DEPLOYMENT_MODE='managed', SOTTO_TENANT_ID='synthetic')
    config = root / 'config'
    config.mkdir()
    (config / 'managed-capabilities.json').write_text(json.dumps({
        'tenant_id': 'synthetic', 'sources': {
            source: {'consented': True, 'connected': True} for source in ('imessage', 'gmail', 'calendar')}}))
    detail = {'status': 'partial', 'observed_at': AT, 'rows_scanned': 24, 'scan_observed': True,
              'limit_reached': True, 'content_partial': False, 'bounded': True,
              'requested_window': {'since': '2026-07-22T08:00:00+00:00', 'until': AT},
              'effective_window': {'since': '2026-10-19T08:00:00+00:00', 'until': AT, 'basis': 'sent'},
              'last_successful_read_at': AT, 'rows_returned': 1, 'query_row_caps': [24],
              'omitted_rows': None, 'failure_category': None,
              'components': {'deferred_unread': {'status': 'failed', 'failure_category': 'query',
                  'last_successful_read_at': None, 'omitted_rows': None}},
              'attachments': {'metadata': 'available', 'body': 'not_read'}}
    primary = {'generated_at': AT, 'source_status': {'imessage': 'partial'},
               'imessage': [{'text': 'Invented retained primary result'}], 'deferred_unread_imessage': [],
               'source_coverage': {'imessage': detail}}
    managed.validate_local_ingress(root, {'local_data': primary})
    source_context.record_bridge_status(primary)

    def health():
        return next(row for row in source_context.source_health(
            now=datetime(2026, 10, 20, 9, tzinfo=timezone.utc))['sources'] if row['source'] == 'imessage')

    usable = health()
    assert usable['status'] == 'partial' and usable['field_counts']['imessage'] == 1
    assert usable['last_successful_read_at'] == AT
    prompt = brief._collection_limits(primary)
    assert 'deferred_unread: failed' in prompt and 'attachment bodies not read' in prompt
    assert 'omitted records: unknown' in prompt

    failed = {'generated_at': LATER, 'source_status': {'imessage': 'degraded'},
              'imessage': [], 'deferred_unread_imessage': [], 'source_coverage': {'imessage': {
                  'status': 'failed', 'observed_at': LATER, 'last_successful_read_at': None,
                  'failure_category': 'query', 'rows_returned': 0}}}
    managed.validate_local_ingress(root, {'local_data': failed})
    source_context.record_bridge_status(failed)
    after_failure = health()
    assert after_failure['coverage']['status'] == 'failed'
    assert after_failure['last_successful_read_at'] == AT
    # A health probe succeeds without a source read and cannot refresh success.
    source_context.record_bridge_status({'generated_at': LATER, 'sources': {'imessage': 'ok'}})
    after_probe = health()
    assert after_probe['last_successful_read_at'] == AT
    assert after_probe['coverage']['status'] == 'failed'

    legacy = {'source_status': {'imessage': 'ok'}, 'imessage': [], 'source_coverage': {
        'imessage': {'status': 'ok', 'rows_scanned': 0, 'scan_observed': True,
                     'limit_reached': False, 'content_partial': False, 'bounded': True}}}
    managed.validate_local_ingress(root, {'local_data': legacy})
    rejected = []
    for name, row in [('private_error', {**detail, 'failure_category': 'PRIVATE body'}),
                      ('attachment_body_claim', {**detail, 'attachments': {'metadata': 'available', 'body': 'complete'}}),
                      ('negative_count', {**detail, 'rows_returned': -1})]:
        try:
            managed.validate_local_ingress(root, {'local_data': {**primary, 'source_coverage': {'imessage': row}}})
        except ValueError:
            rejected.append(name)
    assert len(rejected) == 3
    local = brief._normalize_local({'source_results': {s: {'status': 'ok'} for s in ('gmail', 'calendar')}})
    empty_google = brief._coverage_line(local, local['_source_availability'], [], [])
    assert 'your email and calendar' in empty_google and 'Link Gmail' not in empty_google
    since = int(datetime(2026, 9, 8, tzinfo=timezone.utc).timestamp())
    until = int(datetime.fromisoformat(AT).timestamp())
    checkpoint = {'since': since, 'until': until, 'initial_since': since, 'initial_until': until,
                  'pages': 1, 'rows': 24, 'window_page_base': 0,
                  'observation_totals': {'pages': 1, 'rows': 24, 'eligible': 24, 'selected': 24,
                                         'skipped': 0, 'omitted': 0, 'truncated': 0, 'rejected_people': 0},
                  'last_read_success': AT, 'last_success': '2026-10-20T07:00:00Z',
                  'last_attempt': {'status': 'failed', 'at': LATER, 'stage': 'memory_extraction'},
                  'error': 'PRIVATE provider body', 'error_stage': 'memory_extraction', 'http_status': 503}
    learning = history_coverage.window_coverage(checkpoint)
    assert learning['latest_attempt']['category'] == 'service_unavailable'
    assert learning['last_read_success_at'] != learning['last_extraction_success_at']
    assert 'PRIVATE' not in json.dumps(learning)
    source_context.record_bridge_status({'generated_at': LATER, 'sources': {'imessage': 'disabled'}})
    revoked = health()
    assert 'coverage' not in revoked and 'last_successful_read_at' not in revoked
    persisted = json.loads((config / 'source-state.json').read_text())
    assert 'Invented retained primary result' not in json.dumps(persisted)
    return {'execution': 'scripted synthetic production boundaries; no actual model or signed Mac',
            'primary_payload': primary, 'legacy_payload': legacy, 'usable_parent': usable,
            'after_failed_read': after_failure, 'after_health_probe': after_probe,
            'brief_model_input': prompt, 'authorized_empty_google_line': empty_google,
            'learning': learning, 'after_revocation': revoked, 'rejected_metadata': rejected,
            'rust_fixture': inspect_rust_receipt()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--self-test-denied', action='store_true', help='must fail even when a denied call is caught')
    args = parser.parse_args()
    if args.self_test_denied:
        import socket
        try:
            socket.getaddrinfo('example.invalid', 443)
        except RuntimeError:
            pass
    with tempfile.TemporaryDirectory(prefix='sotto-coverage-inspection-') as directory:
        report = inspect(Path(directory))
    report['unexpected_external_operations'] = list(DENIED)
    if DENIED:
        raise RuntimeError('isolated coverage inspection denied unexpected external operations')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
