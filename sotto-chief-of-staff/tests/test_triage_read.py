"""Original gather/queue behavior with only external transport boundaries synthetic."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / '_shared/scripts/triage_read.py'
spec = importlib.util.spec_from_file_location('triage_read', SCRIPT)
triage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(triage)


@pytest.fixture
def transport(monkeypatch):
    calls = []
    monkeypatch.setattr(triage.google, '_find_google_api', lambda: 'fixed-google-api')
    monkeypatch.setattr(triage.google, '_refresh_google_token', lambda: None)
    monkeypatch.setattr(triage.google, '_token_path', lambda: '')
    def run(api, argv, timeout=60):
        calls.append(argv)
        if argv[1] == 'get':
            return {}
        if argv[2].startswith('in:sent'):
            return [{'id': 's', 'threadId': 'answered', 'date': '2026-10-06T12:00', 'snippet': 'done'}]
        return [
            {'id': 'a', 'threadId': 'answered', 'date': '2026-10-06T11:00', 'snippet': 'reply?'},
            {'id': 'b', 'threadId': 'ordinary', 'date': '2026-10-06T11:00', 'snippet': 'reply?'},
            {'id': 'c', 'threadId': 'priority', 'date': '2026-10-06T11:00', 'labels': ['IMPORTANT', 'UNREAD'], 'snippet': 'reply?'},
            {'id': 'p', 'threadId': 'promo', 'date': '2026-10-06T11:00', 'labels': ['CATEGORY_PROMOTIONS']},
        ]
    monkeypatch.setattr(triage.google, '_run', run)
    monkeypatch.setattr(triage.sources, 'allowed', lambda source: True)
    monkeypatch.setattr(triage.sources, 'consent_receipt_readable', lambda: True)
    local = {'source_status': {'imessage': 'ok', 'whatsapp': 'ok', 'contacts': 'ok'},
             'contacts': [{'name': 'Dana', 'phones': ['+12069994970']}],
             'imessage': [{'handle': '+12069994970', 'text': 'can you send it?', 'is_from_me': False, 'timestamp': '2026-10-06T09:00'},
                          {'handle': '+18885551234', 'text': 'your code', 'is_from_me': False, 'timestamp': '2026-10-06T09:00'}],
             'whatsapp': []}
    def bridge(name, args):
        assert name == 'read_local' and args == {'since_hours': 72}
        return local
    monkeypatch.setattr(triage.sources, 'bridge_call', bridge)
    return calls, local


def statuses(result):
    return {entry['source']: entry for entry in result['coverage']['sources']}


def test_original_gather_merge_and_priority_local_identity(transport):
    calls, _ = transport
    result = triage.query(triage.ORDER)
    assert [row['threadId'] for row in result['email']] == ['priority', 'ordinary']
    assert [row['name'] for row in result['imessage']] == ['Dana']
    assert result['counts'] == {'email': 2, 'imessage': 1, 'whatsapp': 0}
    assert all(row['complete'] for row in result['coverage']['sources'])
    assert [args[2] for args in calls if args[1] == 'search'] == ['newer_than:1d', 'in:sent newer_than:1d']
    assert all(args[0] == 'gmail' for args in calls)
    assert result['email'][0]['message_id'] == 'c'


def test_sent_failure_is_partial_not_complete(transport, monkeypatch):
    original = triage.google._run
    def fail_sent(api, argv, timeout=60):
        if argv[1] == 'search' and argv[2].startswith('in:sent'):
            raise OSError('private provider error')
        return original(api, argv, timeout)
    monkeypatch.setattr(triage.google, '_run', fail_sent)
    result = triage.query(['gmail'])
    assert statuses(result)['gmail'] == {'source': 'gmail', 'status': 'partial', 'complete': False}
    assert 'private' not in str(result)
    assert all(statuses(result)[s]['status'] == 'disabled' for s in triage.ORDER[1:])


def test_bridge_unreachable_differs_from_empty_and_no_disabled_reads(transport, monkeypatch):
    def fail(*args):
        raise OSError('private connection details')
    monkeypatch.setattr(triage.sources, 'bridge_call', fail)
    result = triage.query(['gmail', 'imessage', 'contacts'])
    assert result['email'] and result['imessage'] == []
    assert statuses(result)['imessage']['status'] == 'unavailable'
    assert statuses(result)['whatsapp']['status'] == 'disabled'
    assert 'private' not in str(result)
    monkeypatch.setattr(triage.google, '_find_google_api', lambda: pytest.fail('disabled Google read'))
    monkeypatch.setattr(triage.sources, 'bridge_call', lambda *args: pytest.fail('disabled Bridge read'))
    empty = triage.query([])
    assert empty['counts'] == {'email': 0, 'imessage': 0, 'whatsapp': 0}
    assert all(row['status'] == 'disabled' for row in empty['coverage']['sources'])


def test_captured_projection_never_borrows_disabled_contacts(transport):
    result = triage.query(['imessage'])
    assert result['imessage'] == []
    assert statuses(result)['contacts']['status'] == 'disabled'


def test_missing_availability_receipt_does_not_claim_empty_success(transport):
    _, local = transport
    local.pop('source_status')
    result = triage.query(['imessage', 'contacts'])
    assert result['imessage'] == []
    assert statuses(result)['imessage']['status'] == 'unavailable'


def test_triage_skips_attachment_download_conversion_legacy_default_preserved(transport, monkeypatch):
    monkeypatch.setattr(triage.google, '_attachments_for', lambda *a, **k: pytest.fail('triage attachment contact'))
    result = triage.query(['gmail'])
    assert result['email'] and result['coverage']['gmail_query'] == 'newer_than:1d'
    calls = []
    monkeypatch.setattr(triage.google, '_search_gmail', lambda *a, **k: calls.append(k['attachments']) or [])
    triage.google.gather_gmail('api',40,12)
    triage.google.gather_gmail('api',40,12,attachments=False)
    assert calls == [True,False]


def test_queue_matches_full_gather_without_unused_full_message_reads(transport):
    calls, _ = transport
    # Existing gather/normalize/sent merge/classifier, with only Google transport synthetic.
    full = triage.google.merge_sent(triage.google.gather_gmail('fixed-google-api', 40, 12, attachments=False),
                                   triage.google.gather_sent('fixed-google-api'))
    expected = triage.queue.email_queue(full)
    assert sum(args[1] == 'get' for args in calls) == 5
    calls.clear()
    result = triage.query(['gmail'])
    assert result['email'] == expected
    assert [row['threadId'] for row in result['email']] == ['priority', 'ordinary']
    assert [row['message_id'] for row in result['email']] == ['c', 'b']
    assert [row['last_snippet'] for row in result['email']] == ['reply?', 'reply?']
    assert result['counts']['email'] == 2
    assert statuses(result)['gmail'] == {'source': 'gmail', 'status': 'ok', 'complete': True}
    assert [args[2] for args in calls] == ['newer_than:1d', 'in:sent newer_than:1d']


def test_missing_metadata_snippet_keeps_exact_item_for_selected_read(transport, monkeypatch):
    original = triage.google._run
    body_reads = []
    def read(api, argv, timeout=60):
        if argv[1] == 'get':
            body_reads.append(argv[2])
            return {}
        rows = original(api, argv, timeout)
        if not argv[2].startswith('in:sent'):
            rows = [*rows, {'id': 'empty-preview', 'threadId': 'needs-exact-read',
                           'from': 'Dana <dana@example.test>', 'subject': 'A current request',
                           'date': '2026-10-06T10:00', 'labels': ['UNREAD']}]
        return rows
    monkeypatch.setattr(triage.google, '_run', read)
    result = triage.query(['gmail'])
    row = next(row for row in result['email'] if row['message_id'] == 'empty-preview')
    assert row['last_snippet'] == '' and row['threadId'] == 'needs-exact-read'
    assert row['from'] == 'Dana <dana@example.test>' and row['subject'] == 'A current request'
    assert result['counts']['email'] == 3 and statuses(result)['gmail']['complete'] is True
    assert body_reads == []


def test_metadata_only_queue_retains_bounded_coverage(transport, monkeypatch):
    original = triage.google._run
    body_reads = []
    def read(api, argv, timeout=60):
        if argv[1] == 'get':
            body_reads.append(argv[2])
            return {}
        if argv[2].startswith('in:sent'):
            return original(api, argv, timeout)
        return [{'id': str(i), 'threadId': 'thread-' + str(i), 'date': '2026-10-06T11:00',
                 'from': 'Dana <dana@example.test>', 'snippet': 'Current ask'} for i in range(40)]
    monkeypatch.setattr(triage.google, '_run', read)
    result = triage.query(['gmail'])
    assert len(result['email']) == 40
    assert body_reads == []
    assert statuses(result)['gmail'] == {'source': 'gmail', 'status': 'partial', 'complete': False}
