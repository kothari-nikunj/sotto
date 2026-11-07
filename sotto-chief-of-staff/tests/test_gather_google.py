"""gather_google.py — normalizes google_api.py output to compose_brief's shapes; never crashes."""
import importlib.util, json, os, re

import pytest

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("gg", os.path.join(HERE, "..", "_shared", "scripts", "gather_google.py"))
gg = importlib.util.module_from_spec(spec); spec.loader.exec_module(gg)
_real_refresh = gg._refresh_google_token


def test_google_cli_discovery_uses_only_the_packaged_sotto_path(tmp_path, monkeypatch):
    # An installed upstream google-workspace skill under SOTTO_HOME is never discovered.
    stray = tmp_path / 'skills' / 'google-workspace' / 'scripts' / 'google_api.py'
    stray.parent.mkdir(parents=True)
    stray.write_text('')
    monkeypatch.setenv('SOTTO_HOME', str(tmp_path))
    path = gg._find_google_api()
    assert path.endswith('/adapters/sotto/google_api.py')
    monkeypatch.setattr(gg.os.path, 'isfile', lambda _path: False)
    assert gg._find_google_api() is None


def test_sotto_locked_refresh_uses_only_sotto_setup_path(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    here = root / 'sotto-chief-of-staff' / '_shared' / 'scripts'
    here.mkdir(parents=True)
    (root / 'adapters' / 'sotto').mkdir(parents=True)
    (root / 'adapters' / 'sotto' / 'google_setup.py').write_text('# neutral setup fixture')
    (root / 'adapters' / 'other').mkdir(parents=True)
    (root / 'adapters' / 'other' / 'google_setup.py').write_text('# other adapter trap')
    monkeypatch.setattr(gg, '__file__', str(here / 'gather_google.py'))
    calls = []
    monkeypatch.setattr(gg.subprocess, 'run', lambda argv, **kwargs:
                        calls.append((argv, kwargs)) or type('Result', (), {'returncode': 0})())

    _real_refresh()

    assert len(calls) == 1
    assert calls[0][0] == [gg.sys.executable, str(root / 'adapters' / 'sotto' / 'google_setup.py'), '--check']


def test_sotto_locked_refresh_fails_closed_without_sotto_setup_even_if_another_adapter_has_one(monkeypatch, tmp_path):
    root = tmp_path / 'repo'
    here = root / 'sotto-chief-of-staff' / '_shared' / 'scripts'
    here.mkdir(parents=True)
    other = root / 'adapters' / 'other' / 'google_setup.py'
    other.parent.mkdir(parents=True)
    other.write_text('# other adapter trap')
    monkeypatch.setattr(gg, '__file__', str(here / 'gather_google.py'))
    monkeypatch.setattr(gg.os.path, 'isfile', lambda path: path == str(other))
    monkeypatch.setattr(gg.subprocess, 'run', lambda *_a, **_k: pytest.fail("must not use another adapter's setup"))

    with pytest.raises(gg.GoogleCLIError, match='Sotto Google setup implementation is missing'):
        _real_refresh()


@pytest.fixture(autouse=True)
def _no_token_refresh(monkeypatch):
    monkeypatch.setattr(gg, '_refresh_google_token', lambda: None)


def test_email_normalization_maps_labels_and_body():
    e = gg.normalize_email(
        {"id": "1", "threadId": "t", "from": "a@b.com", "subject": "Hi", "date": "d",
         "snippet": "sn", "labels": ["INBOX", "SENT", "IMPORTANT"]},
        {"body": "the full body", "to": "me@x.com"})
    assert e["labelIds"] == ["INBOX", "SENT", "IMPORTANT"]   # compose_brief keys flags off labelIds
    assert e["isSent"] is True and e["body"] == "the full body" and e["to"] == "me@x.com"


def test_event_normalization_flattens_start_and_maps_link():
    ev = gg.normalize_event({"id": "e", "summary": "Sync",
                             "start": {"dateTime": "2026-06-26T10:00:00Z"},
                             "end": {"dateTime": "2026-06-26T10:30:00Z"}, "htmlLink": "http://x"})
    assert ev["start"] == "2026-06-26T10:00:00Z" and ev["meetingLink"] == "http://x"
    # plain-string start also passes through
    assert gg.normalize_event({"start": "2026-06-26"})["start"] == "2026-06-26"


def test_event_normalization_preserves_exact_provider_linkage():
    ev = gg.normalize_event({'id': 'e', 'threadId': 'gmail-thread-7', 'iCalUID': 'ical-7',
                             'hangoutLink': 'https://meet.google.com/abc'})
    assert ev['threadId'] == 'gmail-thread-7' and ev['iCalUID'] == 'ical-7'
    assert ev['hangoutLink'] == 'https://meet.google.com/abc'


def test_event_normalization_preserves_cancellation_and_recurring_identity():
    raw = {'id': 'occurrence', 'status': 'cancelled', 'recurringEventId': 'series',
           'originalStartTime': {'dateTime': '2026-09-25T10:00:00-07:00'}}
    ev = gg.normalize_event(raw)
    assert all(ev[key] == raw[key] for key in ('status', 'recurringEventId', 'originalStartTime'))


def test_event_normalization_preserves_provider_availability():
    raw = {'id': 'location', 'transparency': 'transparent', 'eventType': 'workingLocation'}
    ev = gg.normalize_event(raw)
    assert ev['transparency'] == 'transparent' and ev['eventType'] == 'workingLocation'


def test_as_list_unwraps_common_envelopes():
    assert gg._as_list([1, 2]) == [1, 2]
    assert gg._as_list({"messages": [1]}) == [1]
    assert gg._as_list({"events": [2]}) == [2]
    assert gg._as_list({"nope": 1}) == []


def test_email_normalization_tolerates_mcp_field_names():
    # A Gmail MCP server uses different field names + a {name,email} sender — must still normalize.
    e = gg.normalize_email(
        {"message_id": "9", "thread_id": "t9", "sender": {"name": "Dana", "email": "dana@acme.com"},
         "title": "Re: LOI", "preview": "quick note", "received_at": "2026-06-28",
         "label_ids": ["inbox", "important"]}, {})
    assert e["id"] == "9" and e["threadId"] == "t9"
    assert e["from"] == "Dana <dana@acme.com>" and e["subject"] == "Re: LOI"
    assert e["snippet"] == "quick note" and e["labelIds"] == ["INBOX", "IMPORTANT"]


def test_event_normalization_tolerates_mcp_field_names():
    ev = gg.normalize_event({"event_id": "e9", "title": "Pitch", "start_time": "2026-06-29T09:00:00-07:00",
                             "participants": [{"email": "x@y.com"}], "conferenceLink": "http://meet"})
    assert ev["id"] == "e9" and ev["summary"] == "Pitch"
    assert ev["start"] == "2026-06-29T09:00:00-07:00" and ev["meetingLink"] == "http://meet"
    assert ev["attendees"] == [{"email": "x@y.com"}]


def test_main_writes_empty_files_when_api_missing(tmp_path, monkeypatch):
    # No google_api.py found → write empty files + WARNING, exit 0 (the brief still runs).
    monkeypatch.setattr(gg, "_find_google_api", lambda: None)
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    assert json.load(open(g)) == [] and json.load(open(c)) == []


@pytest.mark.parametrize('check_rc', [0, 1])
def test_locked_refresh_runs_before_upstream_cli_and_failure_reads_unavailable(tmp_path, monkeypatch, check_rc):
    calls = []
    monkeypatch.setattr(gg, '_refresh_google_token', _real_refresh)
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)

    def run(argv, **kw):
        assert argv[1].endswith(os.path.join('adapters', 'sotto', 'google_setup.py')) and argv[2:] == ['--check']
        assert os.path.isfile(argv[1])
        calls.append('check')
        return type('R', (), {'returncode': check_rc})()
    monkeypatch.setattr(gg.subprocess, 'run', run)
    monkeypatch.setattr(gg, 'gather_gmail', lambda *a, **k: calls.append('inbox') or [])
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: calls.append('calendar') or [])
    receipt = tmp_path / 'sources.json'
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--skip-sent', '--skip-stale', '--gmail-out',
                                    str(tmp_path / 'g.json'), '--cal-out', str(tmp_path / 'c.json'),
                                    '--source-results-out', str(receipt)])
    gg.main()
    results = json.loads(receipt.read_text())
    if check_rc:
        assert calls == ['check']
        assert results['gmail']['status'] == results['calendar']['status'] == 'unavailable'
    else:
        assert calls.count('calendar') == 1
        assert [call for call in calls if call != 'calendar'] == ['check', 'inbox']


@pytest.mark.parametrize('message,expected', [
    ('<HttpError 403: Quota exceeded for quota metric Queries and limit Total Query Cost Units per minute per user>', 'rate_limited'),
    ('<HttpError 403: userRateLimitExceeded>', 'rate_limited'),
    ('<HttpError 403: rateLimitExceeded>', 'rate_limited'),
    ('<HttpError 403: quotaExceeded>', 'rate_limited'),
    ('<HttpError 429: Too many requests>', 'rate_limited'),
    ('<HttpError 403: insufficientPermissions>', 'GoogleCLIError'),
    ('<HttpError 403: forbidden>', 'GoogleCLIError'),
    ('Google operation unavailable or refused. diagnostic=provider_http http_status=403 provider_reason=rateLimitExceeded', 'rate_limited'),
    ('Google operation unavailable or refused. diagnostic=provider_http http_status=403 provider_reason=userRateLimitExceeded', 'rate_limited'),
    ('Google operation unavailable or refused. diagnostic=provider_http http_status=403 provider_reason=quotaExceeded', 'rate_limited'),
    ('Google operation unavailable or refused. diagnostic=provider_http http_status=403 provider_reason=insufficientPermissions', 'GoogleCLIError'),
    ('Google operation unavailable or refused. diagnostic=provider_http http_status=403', 'GoogleCLIError'),
    ('Google operation unavailable or refused. diagnostic=provider_http http_status=429', 'rate_limited'),
    ('Google operation unavailable or refused. diagnostic=provider_http http_status=500 provider_reason=rateLimitExceeded', 'GoogleCLIError'),
    ('Google operation unavailable or refused. diagnostic=permission provider_reason=rateLimitExceeded', 'GoogleCLIError'),
    ('local error: quota exceeded', 'GoogleCLIError'),
])
def test_google_cli_error_classification_requires_typed_status_and_quota_signal(message, expected):
    assert gg._source_error(gg.GoogleCLIError(message)) == expected


def test_typed_provider_http_error_classification():
    class ProviderError(Exception):
        def __init__(self, status, detail):
            self.resp = type('Response', (), {'status': status})()
            super().__init__(detail)

    assert gg._source_error(ProviderError(429, 'Too many requests')) == 'rate_limited'
    assert gg._source_error(ProviderError(403, 'quotaExceeded')) == 'rate_limited'
    assert gg._source_error(ProviderError(403, 'insufficientPermissions')) == 'ProviderError'


def test_sent_quota_denial_keeps_inbox_and_skips_stale(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, 'gather_gmail', lambda *a, **k: calls.append('inbox') or [{'id': 'message'}])

    def sent(*args, **kwargs):
        calls.append('sent')
        raise gg.GoogleCLIError('<HttpError 429: Too many requests>')

    monkeypatch.setattr(gg, 'gather_sent', sent)
    monkeypatch.setattr(gg, 'gather_stale_sent', lambda *a, **k: calls.append('stale'))
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: calls.append('calendar') or [])
    gmail, cal, receipt = (tmp_path / name for name in ('gmail.json', 'cal.json', 'source-results.json'))
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--source-results-out', str(receipt)])
    gg.main()
    assert calls.count('calendar') == 1
    assert [call for call in calls if call != 'calendar'] == ['inbox', 'sent']
    assert json.loads(gmail.read_text()) == [{'id': 'message'}]
    assert json.loads(receipt.read_text())['gmail']['error'] == 'rate_limited'


def _run_lane_quota(tmp_path, monkeypatch, inbox, sent, stale=None):
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, 'gather_gmail', inbox)
    monkeypatch.setattr(gg, 'gather_sent', sent)
    monkeypatch.setattr(gg, 'gather_stale_sent', stale or (lambda *a, **k: gg.GmailRows()))
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: [])
    gmail, cal, receipt = (tmp_path / name for name in ('gmail.json', 'cal.json', 'source-results.json'))
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--source-results-out', str(receipt)])
    gg.main()
    return json.loads(receipt.read_text())['gmail'], json.loads(gmail.read_text())


def _lane_quota(*args, **kwargs):
    raise gg.GoogleCLIError('<HttpError 429: Too many requests>')


@pytest.mark.parametrize('lane', ['sent', 'stale'])
def test_lane_quota_keeps_complete_inbox_coverage(tmp_path, monkeypatch, lane):
    inbox = lambda *a, **k: gg.GmailRows([{'id': 'm1'}])
    sent = _lane_quota if lane == 'sent' else (lambda *a, **k: gg.GmailRows())
    result, emails = _run_lane_quota(tmp_path, monkeypatch, inbox, sent,
                                     _lane_quota if lane == 'stale' else None)
    assert emails == [{'id': 'm1'}]
    assert result['status'] == 'partial' and result['complete'] is False
    assert result['error'] == 'rate_limited'
    assert result['coverage']['since'] and result['coverage']['until']


def test_lane_quota_keeps_failed_inbox_unavailable_with_its_own_error(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise gg.GoogleCLIError('<HttpError 500: backend error>')
    result, emails = _run_lane_quota(tmp_path, monkeypatch, broken, _lane_quota)
    assert emails == []
    assert result['status'] == 'unavailable'
    assert result['error'] == gg._source_error(gg.GoogleCLIError('<HttpError 500: backend error>'))
    assert result['error'] != 'rate_limited'


def test_lane_quota_keeps_capped_inbox_partial_and_its_coverage(tmp_path, monkeypatch):
    def inbox(*args, **kwargs):
        rows = gg.GmailRows([{'id': 'm1'}])
        rows.capped = True
        return rows
    result, _ = _run_lane_quota(tmp_path, monkeypatch, inbox, _lane_quota)
    assert result['status'] == 'partial' and result['complete'] is False
    assert result['error'] == 'rate_limited'
    assert result['coverage']['since'] and result['coverage']['until']


def test_body_quota_keeps_search_snippet_and_marks_partial(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, '_token_path', lambda: '')

    def run(api, args, timeout=60):
        calls.append(args[1])
        if args[1] == 'search':
            return [{'id': 'one', 'subject': 'Useful search result', 'snippet': 'usable snippet'}]
        raise gg.GoogleCLIError('<HttpError 403: Quota exceeded for quota metric Total Query Cost>')

    monkeypatch.setattr(gg, '_run', run)
    monkeypatch.setattr(gg, 'gather_sent', lambda *a, **k: pytest.fail('sent read after quota'))
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: calls.append('calendar') or [])
    gmail, cal, receipt = (tmp_path / name for name in ('gmail.json', 'cal.json', 'source-results.json'))
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--window-days', '7', '--bodies', '1',
                                    '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--source-results-out', str(receipt)])
    gg.main()
    assert calls.count('calendar') == 1
    assert [call for call in calls if call != 'calendar'] == ['search', 'get']  # no second date slice after quota
    assert json.loads(gmail.read_text())[0]['snippet'] == 'usable snippet'
    result = json.loads(receipt.read_text())
    assert result['gmail']['status'] == 'partial'
    assert result['gmail']['complete'] is False and result['gmail']['error'] == 'rate_limited'
    assert result['calendar']['status'] == 'partial'


def test_body_quota_does_not_start_queued_full_reads(monkeypatch):
    calls = []
    monkeypatch.setattr(gg, 'BODY_FETCH_WORKERS', 1)
    monkeypatch.setattr(gg, '_token_path', lambda: '')

    def run(api, args, timeout=60):
        if args[1] == 'search':
            return [{'id': str(i), 'snippet': f'snippet-{i}'} for i in range(4)]
        calls.append(args[2])
        raise gg.GoogleCLIError('<HttpError 429: Too many requests>')

    monkeypatch.setattr(gg, '_run', run)
    rows = gg.gather_gmail('/fake/api.py', 4, 4)
    assert calls == ['0']
    assert rows.rate_limited is True
    assert [row['snippet'] for row in rows] == [f'snippet-{i}' for i in range(4)]


def test_later_slice_search_quota_keeps_earlier_rows(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)

    def run(api, args, timeout=60):
        calls.append('search')
        if calls.count('search') == 1:
            return [{'id': 'first', 'snippet': 'earlier slice survived'}]
        raise gg.GoogleCLIError('<HttpError 403: quotaExceeded>')

    monkeypatch.setattr(gg, '_run', run)
    monkeypatch.setattr(gg, 'gather_sent', lambda *a, **k: pytest.fail('sent read after quota'))
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: calls.append('calendar') or [])
    gmail, cal, receipt = (tmp_path / name for name in ('gmail.json', 'cal.json', 'source-results.json'))
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--window-days', '7', '--bodies', '0',
                                    '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--source-results-out', str(receipt)])
    gg.main()
    assert calls.count('calendar') == 1
    assert [call for call in calls if call != 'calendar'] == ['search', 'search']
    assert json.loads(gmail.read_text()) == [{'id': 'first', 'threadId': None, 'from': '',
        'to': '', 'cc': '', 'subject': '', 'date': '', 'snippet': 'earlier slice survived', 'body': '',
        'labelIds': [], 'isSent': False}]
    result = json.loads(receipt.read_text())['gmail']
    assert result['status'] == 'partial' and result['complete'] is False
    assert result['error'] == 'rate_limited'


def test_later_slice_untyped_failure_is_not_disguised_as_quota(monkeypatch):
    calls = []

    def run(api, args, timeout=60):
        calls.append(args[2])
        if len(calls) == 1:
            return [{'id': 'first'}]
        raise RuntimeError('local error: quota exceeded')

    monkeypatch.setattr(gg, '_run', run)
    with pytest.raises(RuntimeError, match='local error'):
        gg.gather_gmail('/fake/api.py', 2, 0, days=7)


def test_nonquota_body_failure_remains_snippet_fallback_with_healthy_search(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, '_token_path', lambda: '')

    def run(api, args, timeout=60):
        if args[1] == 'search':
            return [{'id': 'one', 'snippet': 'usable snippet'}]
        raise RuntimeError('one message is unreadable')

    monkeypatch.setattr(gg, '_run', run)
    monkeypatch.setattr(gg, 'gather_sent', lambda *a, **k: calls.append('sent') or [])
    monkeypatch.setattr(gg, 'gather_stale_sent', lambda *a, **k: calls.append('stale') or [])
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: calls.append('calendar') or [])
    gmail, cal, receipt = (tmp_path / name for name in ('gmail.json', 'cal.json', 'source-results.json'))
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--source-results-out', str(receipt)])
    gg.main()
    assert json.loads(gmail.read_text())[0]['snippet'] == 'usable snippet'
    assert calls.count('calendar') == 1
    assert [call for call in calls if call != 'calendar'] == ['sent', 'stale']
    result = json.loads(receipt.read_text())
    assert result['gmail']['status'] == 'ok' and result['gmail']['complete'] is True


def test_bodies_fetch_preserves_order_and_tolerates_failure(monkeypatch):
    # 4 search hits, --bodies 3: full bodies for the first 3 only; one fetch fails → that email
    # stays snippet-only; output order matches the search order regardless of fetch completion order.
    items = [{"id": f"m{i}", "subject": f"S{i}", "snippet": f"sn{i}"} for i in range(4)]

    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return items
        mid = args[2]
        if mid == "m1":
            raise RuntimeError("timeout")           # failed body fetch — tolerated
        return {"body": f"full-{mid}"}

    monkeypatch.setattr(gg, "_run", fake_run)
    emails = gg.gather_gmail("/fake/api.py", 25, 3)
    assert [e["id"] for e in emails] == ["m0", "m1", "m2", "m3"]   # search order preserved
    assert emails[0]["body"] == "full-m0" and emails[2]["body"] == "full-m2"
    assert emails[1]["body"] == "" and emails[1]["snippet"] == "sn1"   # failed → snippet-only
    assert emails[3]["body"] == ""                                     # beyond --bodies → not fetched


def test_bodies_fetched_concurrently(monkeypatch):
    # Two body fetches meet at a barrier — only possible if they run in parallel. A sequential
    # implementation deadlocks the barrier (0.5s timeout → BrokenBarrierError → test fails loudly).
    import threading
    barrier = threading.Barrier(2, timeout=5)

    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return [{"id": "a", "subject": "A"}, {"id": "b", "subject": "B"}]
        barrier.wait(timeout=0.5)
        return {"body": f"full-{args[2]}"}

    monkeypatch.setattr(gg, "_run", fake_run)
    emails = gg.gather_gmail("/fake/api.py", 25, 2)
    assert [e["body"] for e in emails] == ["full-a", "full-b"]


# --- email-window honesty (Sprint 0 #5): cap raised + truncation envelope ------

def _wire_fake_gather(monkeypatch, items):
    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return items
        return {"body": "b"}
    monkeypatch.setattr(gg, "_run", fake_run)
    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)
    monkeypatch.setattr(gg, "gather_calendar", lambda *a: [])


def test_truncation_envelope_written_when_search_hits_cap(tmp_path, monkeypatch, capsys):
    _wire_fake_gather(monkeypatch, [{"id": f"m{i}", "subject": f"S{i}"} for i in range(3)])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--max", "3", "--bodies", "0",
                                     "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    payload = json.load(open(g))
    assert payload["truncated_at"] == 3                              # exactly at cap → truncated
    assert "truncation_scope" not in payload                        # single-day legacy wording
    assert [e["id"] for e in payload["emails"]] == ["m0", "m1", "m2"]
    assert payload["truncation_note"] == "(inbox window truncated at 3 — more arrived)"
    assert "truncated at 3 — more arrived" in capsys.readouterr().out  # operator line says so too


def test_no_envelope_when_under_cap(tmp_path, monkeypatch):
    _wire_fake_gather(monkeypatch, [{"id": "m0", "subject": "S0"}])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--max", "3", "--bodies", "0",
                                     "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    payload = json.load(open(g))
    assert isinstance(payload, list) and payload[0]["id"] == "m0"    # bare array — full back-compat


def test_two_uncapped_slices_equal_aggregate_cap_stay_complete(tmp_path, monkeypatch):
    calls = []

    def run(api, args, timeout=60):
        calls.append(args[2])
        return [{'id': f'm{len(calls)}'}]

    monkeypatch.setattr(gg, '_run', run)
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: [])
    gmail, cal, receipt = (tmp_path / name for name in ('gmail.json', 'cal.json', 'source-results.json'))
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--window-days', '7', '--max', '2',
                                    '--bodies', '0', '--skip-sent', '--gmail-out', str(gmail),
                                    '--cal-out', str(cal), '--source-results-out', str(receipt)])
    gg.main()
    assert len(calls) == 2
    assert [row['id'] for row in json.loads(gmail.read_text())] == ['m1', 'm2']
    result = json.loads(receipt.read_text())['gmail']
    assert result['status'] == 'ok' and result['complete'] is True


def test_capped_slice_disclosed_even_when_aggregate_exceeds_cap(tmp_path, monkeypatch):
    calls = []

    def run(api, args, timeout=60):
        calls.append(args[2])
        return ([{'id': 'm1'}, {'id': 'm2'}] if len(calls) == 1 else [{'id': 'm3'}])

    monkeypatch.setattr(gg, '_run', run)
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: [])
    gmail, cal, receipt = (tmp_path / name for name in ('gmail.json', 'cal.json', 'source-results.json'))
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--window-days', '7', '--max', '2',
                                    '--bodies', '0', '--skip-sent', '--gmail-out', str(gmail),
                                    '--cal-out', str(cal), '--source-results-out', str(receipt)])
    gg.main()
    assert len(calls) == 2
    payload = json.loads(gmail.read_text())
    assert [row['id'] for row in payload['emails']] == ['m1', 'm2', 'm3']
    assert payload['truncated_at'] == 2
    assert payload['truncation_scope'] == 'date_slices'
    assert payload['truncation_note'] == (
        '(email history is partial — some date ranges reached the read limit)')
    result = json.loads(receipt.read_text())['gmail']
    assert result['status'] == 'partial' and result['complete'] is False


def test_sent_slices_retain_per_search_cap_metadata(monkeypatch):
    calls = []

    def run(api, args, timeout=60):
        calls.append(args[2])
        return ([{'id': 's1'}, {'id': 's2'}] if len(calls) == 1 else [{'id': 's3'}])

    monkeypatch.setattr(gg, '_run', run)
    sent = gg.gather_sent('/fake/api.py', max_n=2, bodies=0, days=7)
    assert sent.capped is True and sent.rate_limited is False
    assert [row['id'] for row in sent] == ['s1', 's2', 's3']
    assert all(row['isSent'] for row in sent)


def test_default_max_raised_to_40(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)
    monkeypatch.setattr(gg, "gather_gmail", lambda api, mx, bodies, days=1: captured.update(max=mx) or [])
    monkeypatch.setattr(gg, "gather_calendar", lambda *a, **k: [])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    assert captured["max"] == 40


def test_ensure_deps_fast_path_skips_subprocess(monkeypatch):
    # When googleapiclient imports cleanly, NO subprocess work happens (the 240s pip install
    # stays out of the brief's hot path; it remains the backstop when the import fails).
    import sys as _sys
    import types
    monkeypatch.setitem(_sys.modules, "googleapiclient", types.ModuleType("googleapiclient"))
    monkeypatch.setattr(gg.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess used on fast path")))
    assert gg._ensure_google_deps() is True


def test_ensure_deps_cli_mode_only_heals(monkeypatch, capsys):
    # `--ensure-deps` runs ONLY the self-heal (setup-time), no CLI discovery / gather / file writes.
    called = {"heal": 0}
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: called.__setitem__("heal", called["heal"] + 1) or True)
    monkeypatch.setattr(gg, "_find_google_api", lambda: (_ for _ in ()).throw(AssertionError("gather ran")))
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--ensure-deps"])
    gg.main()
    assert called["heal"] == 1
    assert "googleapiclient OK" in capsys.readouterr().out


# --- --attendee-comms mode (meeting-prep: per-attendee Gmail threads) ------------------------

def test_attendee_comms_normalizes_and_derives_direction(tmp_path, monkeypatch):
    # Per-attendee search → {"<email>": [{date,subject,snippet,from_me}]}. from_me: SENT label
    # proves the owner; an exact attendee sender proves inbound, anything else stays unknown.
    att = tmp_path / "att.json"
    json.dump([{"name": "Dana Roe", "email": "Dana@acme.com"}], open(att, "w"))

    def fake_run(api, args, timeout=60):
        assert args[:2] == ["gmail", "search"]
        assert args[2] == "{from:dana@acme.com to:dana@acme.com} newer_than:30d"
        return [
            {"from": "Dana Roe <dana@acme.com>", "subject": "Pricing", "date": "Tue, 04 Aug",
             "snippet": "circling back on the pilot", "labels": ["INBOX"]},
            {"from": "Me <me@myco.com>", "to": "dana@acme.com", "subject": "Re: Pricing",
             "date": "Wed, 05 Aug", "snippet": "sending the revised SOW", "labels": ["SENT"]},
            {"from": "Me <me@myco.com>", "subject": "no labels at all", "date": "d", "snippet": "s"},
        ]

    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)
    monkeypatch.setattr(gg, "_run", fake_run)
    out = tmp_path / "comms.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--attendee-comms", str(att),
                                     "--comms-out", str(out)])
    gg.main()
    comms = json.load(open(out))
    rows = comms["dana@acme.com"]                      # keyed by LOWERCASED attendee email
    assert rows[0] == {"date": "Tue, 04 Aug", "subject": "Pricing",
                       "snippet": "circling back on the pilot", "from_me": False,
                       "sender_name": "Dana Roe", "sender_identifier": "dana@acme.com"}
    assert rows[1]["from_me"] is True                  # SENT label
    assert rows[2]["from_me"] is None                  # missing labels cannot establish authorship


def test_attendee_comms_does_not_attribute_an_introducer_or_similar_address_to_owner(monkeypatch):
    monkeypatch.setattr(gg, '_run', lambda *a, **k: [
        {'from': 'Priya <priya@other.com>', 'to': 'dana@acme.com, me@myco.com',
         'subject': 'An introduction', 'snippet': 'You two should compare notes.'},
        {'from': 'not-dana@acme.com', 'to': 'dana@acme.com', 'snippet': 'A different sender.'},
    ])
    _, rows = gg._fetch_attendee_comms('/fake/api.py', 'dana@acme.com')
    assert all(row['from_me'] is None for row in rows)
    assert rows[0]['sender_name'] == 'Priya' and rows[0]['sender_identifier'] == 'priya@other.com'


def test_attendee_comms_fail_empty_when_api_missing(tmp_path, monkeypatch, capsys):
    # No google_api.py → empty {} + WARNING, exit 0 (the prep still runs, just without threads).
    att = tmp_path / "att.json"
    json.dump([{"name": "Dana", "email": "dana@acme.com"}], open(att, "w"))
    monkeypatch.setattr(gg, "_find_google_api", lambda: None)
    out = tmp_path / "comms.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--attendee-comms", str(att),
                                     "--comms-out", str(out)])
    gg.main()
    assert json.load(open(out)) == {}
    assert "WARNING" in capsys.readouterr().out


def test_attendee_comms_one_failed_search_never_fails_the_rest(tmp_path, monkeypatch):
    att = tmp_path / "att.json"
    json.dump([{"email": "a@x.com"}, {"email": "b@y.com"}], open(att, "w"))

    def fake_run(api, args, timeout=60):
        if "a@x.com" in args[2]:
            raise RuntimeError("timeout")              # one attendee's search dies — tolerated
        return [{"from": "b@y.com", "subject": "S", "date": "d", "snippet": "sn"}]

    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)
    monkeypatch.setattr(gg, "_run", fake_run)
    out = tmp_path / "comms.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--attendee-comms", str(att),
                                     "--comms-out", str(out)])
    gg.main()
    comms = json.load(open(out))
    assert "a@x.com" not in comms and comms["b@y.com"][0]["subject"] == "S"


def test_attendee_emails_accepts_both_shapes_dedupes_and_caps():
    # research_in shape [{name,email}] + {"attendees":[…]} envelope
    import json as _json, tempfile, os as _os
    def _emails(data):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            _json.dump(data, f)
        try:
            return gg._attendee_emails_from_file(f.name)
        finally:
            _os.unlink(f.name)

    assert _emails([{"name": "A", "email": "A@x.com"}, {"email": "a@x.com"}]) == ["a@x.com"]
    assert _emails({"attendees": [{"email": "b@y.com"}]}) == ["b@y.com"]
    # calendar-derived list: events carrying attendees[] (dicts or bare strings)
    assert _emails([{"summary": "Sync", "attendees": [{"email": "c@z.com"}, "d@z.com"]}]) == \
        ["c@z.com", "d@z.com"]
    # cap at 15 unique emails
    many = [{"email": f"p{i}@x.com"} for i in range(20)]
    assert len(_emails(many)) == 15
    # unreadable file → []
    assert gg._attendee_emails_from_file("/nonexistent/path.json") == []


# ── the stale sent lane: an email you sent that nobody answered is a debt owed to you ────────────

def _thread(tid, msgs):
    return {"id": tid, "messages": [
        {"id": f"{tid}-{i}", "internalDate": str(int(ts.timestamp() * 1000)),
         "labelIds": ["SENT"] if mine else ["INBOX"], "snippet": snip,
         "payload": {"headers": [{"name": "To", "value": to}, {"name": "Subject", "value": subj}]}}
        for i, (ts, mine, to, subj, snip) in enumerate(msgs)]}


def test_stale_is_the_last_word_being_yours_for_three_to_fourteen_days():
    import datetime as dt
    now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone.utc)
    d = lambda days, h=9: now - dt.timedelta(days=days, hours=12 - h)
    threads = {
        # yours, 4 days silent → stale
        "t-stale": _thread("t-stale", [(d(6), False, "me@example.com", "Deck", "here you go"),
                                       (d(4), True, "Victor Yeung <victor@acme.com>", "Re: Deck",
                                        "sending the revised deck, let me know on allocation")]),
        # they answered after you → not stale
        "t-answered": _thread("t-answered", [(d(5), True, "dana@acme.com", "Q", "quick question"),
                                             (d(2), False, "me@example.com", "Re: Q", "answer")]),
        # yours, but only 2 days → not yet
        "t-fresh": _thread("t-fresh", [(d(2), True, "ben@acme.com", "Thu?", "does thursday work")]),
        # yours, 20 days → dead, not a debt
        "t-dead": _thread("t-dead", [(d(20), True, "old@acme.com", "Ping", "any thoughts here")]),
    }
    cands = [{"threadId": t, "to": "x", "subject": "s", "snippet": "sn"} for t in
             ("t-stale", "t-answered", "t-fresh", "t-dead", "t-missing")]
    rows = gg.stale_from_threads(cands, threads, now)
    assert [r["threadId"] for r in rows] == ["t-stale"]
    r = rows[0]
    assert r["toEmail"] == "victor@acme.com" and r["subject"] == "Re: Deck"
    assert r["daysSinceSent"] == 4 and r["sentDate"].startswith("2026-08-31")
    assert "revised deck" in r["snippet"]


def test_gather_stale_sent_searches_once_and_reads_each_thread_once(monkeypatch):
    import datetime as dt
    now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=dt.timezone.utc)
    searches, gets = [], []

    def fake_run(api, args, timeout=60):
        searches.append(args)
        return [{"id": "m1", "threadId": "t1", "to": "victor@acme.com", "subject": "Deck"},
                {"id": "m2", "threadId": "t1", "to": "victor@acme.com", "subject": "Deck"},
                {"id": "m3", "threadId": "t2", "to": "dana@acme.com", "subject": "Q"}]
    monkeypatch.setattr(gg, "_run", fake_run)

    class _Get:
        def __init__(self, tid): self.tid = tid
        def execute(self):
            gets.append(self.tid)
            if self.tid == "t2":
                raise RuntimeError("gone")
            return _thread("t1", [(now - dt.timedelta(days=5), True, "victor@acme.com", "Deck",
                                   "the revised deck is attached, thoughts on allocation")])

    class _Svc:
        def users(self): return self
        def threads(self): return self
        def get(self, userId, id, format, metadataHeaders): return _Get(id)

    rows = gg.gather_stale_sent("/fake/api.py", service=_Svc(), now=now)
    assert len(searches) == 1 and searches[0][2] == "in:sent older_than:3d newer_than:14d -in:chats"
    assert gets == ["t1", "t2"]                              # one read per thread, once
    assert [r["threadId"] for r in rows] == ["t1"]           # the unreadable thread costs itself only
    assert set(rows.threads) == {"t1"}                      # reuse only successful current inventories


def test_stale_thread_quota_keeps_prior_evidence_and_marks_partial(tmp_path, monkeypatch):
    import datetime as dt
    calls = []
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=4)
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, '_token_path', lambda: '')

    def run(api, args, timeout=60):
        if args[2].startswith('in:sent older_than:'):
            calls.append('stale-search')
            return [{'id': f'm{i}', 'threadId': f't{i}', 'to': 'owner@example.com'}
                    for i in (1, 2, 3)]
        calls.append('inbox-search')
        return []

    class QuotaError(Exception):
        resp = type('Response', (), {'status': 403})()

    class Get:
        def __init__(self, tid): self.tid = tid
        def execute(self):
            calls.append(self.tid)
            if self.tid == 't1':
                return _thread('t1', [(old, True, 'recipient@example.com', 'Followup', 'checking in')])
            if self.tid == 't2':
                raise QuotaError('quotaExceeded')
            pytest.fail('third thread read after quota')

    class Service:
        def users(self): return self
        def threads(self): return self
        def get(self, userId, id, format, metadataHeaders): return Get(id)
        def close(self): calls.append('closed')

    monkeypatch.setattr(gg, '_run', run)
    monkeypatch.setattr(gg, '_gmail_service', Service)
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: calls.append('calendar') or [])
    gmail, cal, receipt = (tmp_path / name for name in ('gmail.json', 'cal.json', 'source-results.json'))
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--bodies', '0', '--skip-sent',
                                    '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--source-results-out', str(receipt)])
    gg.main()
    assert calls.count('calendar') == 1
    assert [call for call in calls if call != 'calendar'] == ['inbox-search', 'stale-search', 't1', 't2', 'closed']
    payload = json.loads(gmail.read_text())
    assert [row['threadId'] for row in payload['stale_threads']] == ['t1']
    result = json.loads(receipt.read_text())
    assert result['gmail']['status'] == 'partial'
    assert result['gmail']['complete'] is False and result['gmail']['error'] == 'rate_limited'
    assert result['calendar']['status'] == 'partial'


def test_stale_rows_ride_the_gmail_envelope(tmp_path, monkeypatch):
    _wire_fake_gather(monkeypatch, [{"id": "m0", "subject": "S0"}])
    monkeypatch.setattr(gg, "gather_stale_sent", lambda api: [{"threadId": "t1", "toEmail": "v@acme.com"}])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--max", "3", "--bodies", "0",
                                     "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    payload = json.load(open(g))
    assert payload["stale_threads"] == [{"threadId": "t1", "toEmail": "v@acme.com"}]
    assert [e["id"] for e in payload["emails"]] == ["m0"] and "truncated_at" not in payload
    # --skip-stale, or a backfill window, never runs the lane
    monkeypatch.setattr(gg, "gather_stale_sent", lambda api: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--max", "3", "--bodies", "0", "--skip-stale",
                                     "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    assert isinstance(json.load(open(g)), list)


def test_event_normalization_reads_your_own_rsvp():
    ev = gg.normalize_event({"id": "e", "summary": "Pitch", "start": "2026-09-05T10:00:00Z",
                             "attendees": [{"email": "vc@fund.com", "responseStatus": "accepted"},
                                           {"email": "me@example.com", "self": True,
                                            "responseStatus": "declined"}]})
    assert ev["my_response"] == "declined"
    assert gg.normalize_event({"id": "e2", "attendees": [{"email": "a@b.com"}]})["my_response"] == ""


def test_skip_gmail_calendar_only(tmp_path, monkeypatch):
    # meeting-prep uses --skip-gmail: gmail not even attempted; both files still written.
    called = {"gmail": False, "cal": False}
    monkeypatch.setattr(gg, "_find_google_api", lambda: "/fake/google_api.py")
    monkeypatch.setattr(gg, "_ensure_google_deps", lambda: True)   # don't shell out to pip in tests
    monkeypatch.setattr(gg, "gather_gmail", lambda *a, **k: called.__setitem__("gmail", True) or [{"id": "x"}])
    monkeypatch.setattr(gg, "gather_calendar", lambda *a, **k: called.__setitem__("cal", True) or [{"id": "e"}])
    g, c = tmp_path / "g.json", tmp_path / "c.json"
    monkeypatch.setattr("sys.argv", ["gather_google.py", "--skip-gmail", "--gmail-out", str(g), "--cal-out", str(c)])
    gg.main()
    assert called["gmail"] is False and called["cal"] is True
    import json
    assert json.load(open(g)) == [] and len(json.load(open(c))) == 1


# ── Sent lane (roadmap Step 2 item 0) ─────────────────────────────────────────────────────────────

def _sent_row(mid="s1", to="Sarah Chen <sarah@acme.com>", body="Thanks — locking Thursday 3pm.",
              labels=None):
    row = {"id": mid, "threadId": "t" + mid, "from": "me@mine.com", "to": to,
           "subject": "Re: Contract", "date": "2026-08-06T09:00:00Z", "snippet": body[:20]}
    if labels is not None:
        row["labels"] = labels
    return row


def test_mark_sent_sets_both_signals_even_without_labels():
    """The `in:sent` QUERY is the ground truth; some hosts return search rows with no labels at all.
    Set isSent AND the SENT label so the two downstream readers can never disagree."""
    e = gg.mark_sent(gg.normalize_email(_sent_row(), {}))
    assert e["isSent"] is True and "SENT" in e["labelIds"]
    # already-labelled rows aren't duplicated
    e2 = gg.mark_sent(gg.normalize_email(_sent_row(labels=["SENT"]), {}))
    assert e2["labelIds"].count("SENT") == 1


def test_merge_sent_dedupes_by_id_and_upgrades_the_inbox_row():
    """`newer_than:1d` has no in: operator, so Gmail already returns some sent mail in the inbox
    lane — merging must not double-count it (that would hand style_extract the same sample twice)."""
    inbox = [gg.normalize_email({"id": "a", "from": "x@y.com"}, {}),
             gg.normalize_email(_sent_row("s1"), {})]          # same message, no SENT label
    sent = [gg.mark_sent(gg.normalize_email(_sent_row("s1"), {"body": "full body"})),
            gg.mark_sent(gg.normalize_email(_sent_row("s2"), {}))]
    merged = gg.merge_sent(inbox, sent)
    assert [e["id"] for e in merged] == ["a", "s1", "s2"]      # no duplicate s1
    dup = next(e for e in merged if e["id"] == "s1")
    assert dup["isSent"] is True and "SENT" in dup["labelIds"] # the inbox copy GAINS the markers
    assert dup["body"] == "full body"                          # and the body the sent lane fetched
    assert merged[0]["isSent"] is False                        # inbound rows untouched


def test_merge_sent_tolerates_missing_ids_and_junk():
    assert gg.merge_sent([], []) == []
    merged = gg.merge_sent([{"id": None}], [{"id": None, "isSent": True}, "junk"])
    assert len(merged) == 2                                    # id-less sent row still appended


def test_sent_lane_shape_is_what_style_extract_consumes():
    """The contract that matters: gather_google's sent rows must survive style_extract's --gmail
    adapter untouched (it keys off isSent/SENT, the To header, and body-or-snippet)."""
    import style_extract as se
    rows = gg.merge_sent(
        [gg.normalize_email({"id": "in1", "from": "dana@acme.com", "subject": "hi",
                             "snippet": "inbound"}, {})],
        [gg.mark_sent(gg.normalize_email(_sent_row("s1"),
                                         {"body": "Thanks — locking Thursday 3pm."})),
         gg.mark_sent(gg.normalize_email(_sent_row("s2", to="mom@yahoo.com"),
                                         {"body": "call you tonight"}))])
    samples = se._adapt_gmail(rows)
    assert len(samples) == 2                                   # the inbound row is NOT a sample
    assert [s["channel"] for s in samples] == ["email", "email"]
    assert samples[0]["recipient"] == "Sarah Chen"
    # the full body, not the 20-char snippet — bodies are why the lane fetches them
    assert samples[0]["text"] == "Thanks — locking Thursday 3pm."
    assert samples[0]["work"] is True                          # acme.com → work_email bucket
    assert samples[1]["work"] is False                         # yahoo.com → personal
    assert se.bucket_for_sample(samples[0]) == "work_email"
    # the file-shaped envelope compose_brief writes on a truncated window works too
    assert len(se._adapt_gmail({"emails": rows, "truncated_at": 40})) == 2


def test_gather_sent_uses_the_in_sent_query(monkeypatch):
    calls = []

    def fake_run(api, args, timeout=60):
        calls.append(args)
        if args[:2] == ["gmail", "search"]:
            return [_sent_row("s1")]
        return {"body": "the full sent body", "to": "Sarah Chen <sarah@acme.com>"}

    monkeypatch.setattr(gg, "_run", fake_run)
    rows = gg.gather_sent("api.py", 15, 10)
    assert calls[0] == ["gmail", "search", "in:sent newer_than:1d", "--max", "15"]
    assert len(rows) == 1 and rows[0]["isSent"] is True
    assert rows[0]["body"] == "the full sent body"             # bodies fetched: snippets can't teach voice


def test_window_days_slices_the_backfill_past_the_page_caps(monkeypatch):
    """One Gmail search clamps at ~500 results and one calendar list pages at ~25 events — the
    owner's 42-day backfill came back ~5 days deep. A wide window becomes date-bounded slices
    (after:/before: for Gmail, short --start/--end spans for calendar), deduped by id; the daily
    1-day gather keeps its single query and tight 60s leash."""
    import datetime
    calls, touts = [], []

    def fake_run(api, args, timeout=60):
        calls.append(args)
        touts.append(timeout)
        return [{"id": "same-row"}]                    # every slice returns the same id

    monkeypatch.setattr(gg, "_run", fake_run)
    rows = gg.gather_gmail("api.py", 500, 0, days=42)
    gmail_calls = list(calls)
    assert len(gmail_calls) >= 6                       # ~weekly slices cover the window
    assert all(c[0] == "gmail" and "after:" in c[2] and "before:" in c[2] for c in gmail_calls)
    assert len(rows) == 1                              # boundary overlap deduped by id

    calls.clear()
    sent = gg.gather_sent("api.py", 15, 0, days=42)
    assert all(c[2].startswith("in:sent after:") for c in calls)
    assert len(sent) == 1 and sent[0]["isSent"] is True

    calls.clear()
    monkeypatch.setattr(gg, "_token_path", lambda: "")
    evs = gg.gather_calendar("api.py", back_days=41)
    assert len(calls) >= 20                            # ~2-day spans beat the 25-event page
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    first = datetime.datetime.strptime(calls[0][calls[0].index("--start") + 1], fmt)
    last = datetime.datetime.strptime(calls[-1][calls[-1].index("--end") + 1], fmt)
    assert (last - first).days == 44                   # 41 back + 3 forward, covered end to end
    assert len(evs) == 1                               # deduped by event id
    assert all(t == gg.BACKFILL_TIMEOUT for t in touts)

    calls.clear()
    touts.clear()
    gg.gather_gmail("api.py", 40, 0)
    assert calls == [["gmail", "search", "newer_than:1d", "--max", "40"]]
    assert touts == [60]                               # the daily brief is untouched


# --- the attachment lane -------------------------------------------------------------------
# An attachment Sotto can read becomes text under its email; one it can't is named, never guessed.
# The Gmail service is faked here because the lane talks to the Gmail API DIRECTLY (the host's
# google_api.py has no attachments verb and its `gmail get` discards the MIME part tree) — the same
# direct-to-Google precedent google_action.py's gmail-draft set.


class _FakeGmail:
    """The three chained calls the lane makes, and nothing else. `parts` is the payload's part list;
    `blobs` maps attachmentId → base64url data for the ones Gmail didn't inline."""

    def __init__(self, parts, blobs=None, boom=False):
        self._parts, self._blobs, self._boom = parts, blobs or {}, boom
        self.attachment_gets = []

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, userId=None, id=None, format=None):
        if self._boom:
            raise RuntimeError("Gmail said no")
        return _Exec({"id": id, "payload": {"parts": self._parts}})

    def attachments(self):
        return _Attachments(self)


class _Attachments:
    def __init__(self, gmail):
        self._g = gmail

    def get(self, userId=None, messageId=None, id=None):
        self._g.attachment_gets.append(id)
        return _Exec({"data": self._g._blobs.get(id, "")})


class _Exec:
    def __init__(self, payload):
        self._p = payload

    def execute(self):
        return self._p


def _b64(raw):
    import base64
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _wire_attachment_gather(monkeypatch, parts, blobs=None, boom=False):
    """The inbox search + body fetch stay on the fake CLI; the attachment lane gets a fake Gmail."""
    svc = _FakeGmail(parts, blobs, boom)

    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return [{"id": "m1", "subject": "Q3 numbers", "labels": ["INBOX"]}]
        return {"body": "see attached"}

    monkeypatch.setattr(gg, "_run", fake_run)
    monkeypatch.setattr(gg, "_token_path", lambda: "/fake/google_token.json")
    monkeypatch.setattr(gg, "_gmail_service", lambda: svc)
    return svc


def test_inbox_attachment_is_converted_and_hung_on_the_email(monkeypatch):
    """The end-to-end claim: a CSV attached to an inbox email in the bodies cohort arrives on the
    normalized row as converted Markdown."""
    parts = [{"filename": "", "mimeType": "text/plain", "body": {"size": 4}},
             {"filename": "numbers.csv", "mimeType": "text/csv",
              "body": {"size": 8, "attachmentId": "att-1"}}]
    _wire_attachment_gather(monkeypatch, parts, {"att-1": _b64(b"a,b\n1,2\n")})
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    atts = emails[0]["attachments"]
    assert len(atts) == 1 and atts[0]["filename"] == "numbers.csv"
    assert "| a | b |" in atts[0]["text"]


def test_gmail_inlined_bytes_need_no_second_api_call(monkeypatch):
    """Gmail inlines small attachment bodies. When it has, the lane uses them — an attachments().get
    for an attachment we already hold is a call spent for nothing."""
    parts = [{"filename": "numbers.csv", "mimeType": "text/csv",
              "body": {"size": 8, "data": _b64(b"a,b\n1,2\n")}}]
    svc = _wire_attachment_gather(monkeypatch, parts)
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    assert "| 1 | 2 |" in emails[0]["attachments"][0]["text"]
    assert svc.attachment_gets == []                 # nothing was downloaded twice


def test_an_email_with_no_attachments_makes_no_attachment_call(monkeypatch):
    """A part with no filename is not an attachment. No filename anywhere → no attachments().get,
    and no `attachments` key on the row at all."""
    svc = _wire_attachment_gather(monkeypatch, [{"filename": "", "mimeType": "text/html",
                                                 "body": {"size": 20}}])
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    assert "attachments" not in emails[0]
    assert svc.attachment_gets == []


def test_the_sent_lane_never_fetches_attachments(monkeypatch):
    """v1 is the inbox. The sent lane is style exhaust — converting the files the user attached to
    their own mail would spend API calls and prompt budget telling them what they already know."""
    monkeypatch.setattr(gg, "_token_path", lambda: "")
    monkeypatch.setattr(gg, "_attachments_for",
                        lambda *a, **k: pytest.fail("attachment lane touched on the sent path"))

    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return [{"id": "s1", "subject": "my reply"}]
        return {"body": "sent body"}

    monkeypatch.setattr(gg, "_run", fake_run)
    sent = gg.gather_sent("/fake/api.py", 15, 10)
    assert sent[0]["isSent"] is True and "attachments" not in sent[0]


def test_an_attachment_failure_never_drops_the_email(monkeypatch):
    """Fail toward silence: Gmail erroring on the metadata call costs that email its attachments,
    never its place in the brief."""
    _wire_attachment_gather(monkeypatch, [], boom=True)
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    assert len(emails) == 1 and emails[0]["subject"] == "Q3 numbers"
    assert "attachments" not in emails[0]


def test_google_not_connected_costs_the_lane_nothing(monkeypatch):
    """No token file → the lane returns immediately, without building a client or importing the
    Google libs. The gather is otherwise untouched."""
    def fake_run(api, args, timeout=60):
        if args[:2] == ["gmail", "search"]:
            return [{"id": "m1", "subject": "Q3"}]
        return {"body": "b"}

    monkeypatch.setattr(gg, "_run", fake_run)
    monkeypatch.setattr(gg, "_token_path", lambda: "")
    monkeypatch.setattr(gg, "_gmail_service",
                        lambda: pytest.fail("client built without a token"))
    emails = gg.gather_gmail("/fake/api.py", 25, 12)
    assert len(emails) == 1 and "attachments" not in emails[0]


def test_an_oversized_attachment_is_named_and_never_downloaded(monkeypatch):
    """The byte cap is enforced BEFORE the download, so a 9MB file costs one metadata read and a
    name — not 9MB of the brief's wall clock."""
    parts = [{"filename": "video.mov", "mimeType": "video/quicktime",
              "body": {"size": gg.MAX_ATTACHMENT_BYTES + 1, "attachmentId": "att-big"}}]
    svc = _wire_attachment_gather(monkeypatch, parts, {"att-big": _b64(b"x" * 10)})
    atts = gg.gather_gmail("/fake/api.py", 25, 12)[0]["attachments"]
    assert atts == [{"filename": "video.mov", "unreadable": "too large to read"}]
    assert svc.attachment_gets == []                 # never fetched


def test_every_filename_is_listed_even_past_the_per_email_cap(monkeypatch):
    """ALL filenames are named; only the CONVERTED ones are capped. "There were two more files" is
    itself information the brief must not lose."""
    parts = [{"filename": f"f{i}.csv", "mimeType": "text/csv",
              "body": {"size": 8, "data": _b64(b"a,b\n1,2\n")}} for i in range(5)]
    _wire_attachment_gather(monkeypatch, parts)
    atts = gg.gather_gmail("/fake/api.py", 25, 12)[0]["attachments"]
    assert len(atts) == 5                                             # every file named
    assert sum(1 for a in atts if "text" in a) == gg.MAX_ATTACHMENTS_PER_EMAIL
    assert all(f"over the {gg.MAX_ATTACHMENTS_PER_EMAIL}-attachment limit" == a["unreadable"]
               for a in atts[gg.MAX_ATTACHMENTS_PER_EMAIL:])


def test_attachment_parts_walks_nested_multipart():
    """multipart/mixed wrapping multipart/alternative is the ordinary shape of a real email — a flat
    scan of payload["parts"] misses everything one level down."""
    payload = {"parts": [
        {"mimeType": "multipart/alternative", "filename": "",
         "parts": [{"filename": "", "mimeType": "text/plain", "body": {"size": 4}},
                   {"filename": "inner.pdf", "mimeType": "application/pdf",
                    "body": {"size": 100, "attachmentId": "deep"}}]},
        {"filename": "outer.csv", "mimeType": "text/csv", "body": {"size": 9}}]}
    names = [p["filename"] for p in gg._attachment_parts(payload)]
    assert names == ["inner.pdf", "outer.csv"]                # depth-first, message order


def test_a_download_failure_names_that_one_attachment_only(monkeypatch):
    """One attachment failing to download names IT — the others on the same email still convert."""
    parts = [{"filename": "good.csv", "mimeType": "text/csv",
              "body": {"size": 8, "data": _b64(b"a,b\n1,2\n")}},
             {"filename": "bad.csv", "mimeType": "text/csv",
              "body": {"size": 8, "attachmentId": "nope"}}]
    svc = _wire_attachment_gather(monkeypatch, parts)

    def _boom(*a, **k):
        raise RuntimeError("download failed")

    monkeypatch.setattr(gg, "_attachment_bytes",
                        lambda s, m, p: _boom() if p["filename"] == "bad.csv" else b"a,b\n1,2\n")
    atts = gg.gather_gmail("/fake/api.py", 25, 12)[0]["attachments"]
    assert "text" in atts[0] and atts[1]["unreadable"] == "could not be downloaded"
    assert svc is not None


def test_attachment_quota_marks_partial_and_stops_later_downloads(tmp_path, monkeypatch):
    parts = [{'filename': name, 'mimeType': 'text/csv',
              'body': {'size': 8, 'attachmentId': name}} for name in ('first.csv', 'second.csv')]
    _wire_attachment_gather(monkeypatch, parts)
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    calls = []

    class QuotaError(Exception):
        resp = type('Response', (), {'status': 403})()

    def fetch(service, mid, part):
        calls.append(part['filename'])
        raise QuotaError('quotaExceeded')

    monkeypatch.setattr(gg, '_attachment_bytes', fetch)
    monkeypatch.setattr(gg, 'gather_sent', lambda *a, **k: pytest.fail('sent read after quota'))
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: calls.append('calendar') or [])
    gmail, cal, receipt = (tmp_path / name for name in ('gmail.json', 'cal.json', 'source-results.json'))
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--window-days', '7', '--bodies', '1',
                                    '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--source-results-out', str(receipt)])
    gg.main()
    payload = json.loads(gmail.read_text())
    assert payload[0]['subject'] == 'Q3 numbers'
    assert [a['filename'] for a in payload[0]['attachments']] == ['first.csv', 'second.csv']
    assert calls.count('calendar') == 1
    assert [call for call in calls if call != 'calendar'] == ['first.csv']
    result = json.loads(receipt.read_text())
    assert result['gmail']['status'] == 'partial'
    assert result['gmail']['complete'] is False and result['gmail']['error'] == 'rate_limited'


def test_the_caps_have_one_owner():
    """gather_google IMPORTS the caps from attachments.py rather than keeping its own copy — the
    fetch side and the render side cannot state two different numbers. Asserted on the import
    statement itself, because equal-but-separately-declared constants is exactly the drift this
    guards against."""
    src = open(os.path.join(HERE, "..", "_shared", "scripts", "gather_google.py"),
               encoding="utf-8").read()
    assert "from attachments import" in src
    assert "MAX_ATTACHMENTS_PER_EMAIL" in src and "MAX_ATTACHMENT_BYTES" in src
    # …and nowhere does it declare one of its own
    assert not re.search(r"^MAX_ATTACHMENT\w*\s*=", src, re.M)
    assert (gg.MAX_ATTACHMENTS_PER_EMAIL, gg.MAX_ATTACHMENT_BYTES) == (3, 8_000_000)


def test_attachment_workers_never_share_google_transport(monkeypatch):
    import threading
    clients = []
    barrier = threading.Barrier(4)
    class Client:
        def __init__(self):
            self.owner = threading.get_ident()
            self.closed = False
        def close(self):
            self.closed = True
    def build():
        client = Client()
        clients.append(client)
        return client
    def fetch(client, mid):
        assert client.owner == threading.get_ident()
        barrier.wait(timeout=3)
        return [{'filename': mid}]
    monkeypatch.setattr(gg, '_token_path', lambda: 'connected')
    monkeypatch.setattr(gg, '_gmail_service', build)
    monkeypatch.setattr(gg, '_fetch_attachments', fetch)
    result = gg._attachments_for(['a', 'b', 'c', 'd'])
    assert list(result) == ['a', 'b', 'c', 'd']
    assert len(clients) == 4 and all(c.closed for c in clients)


def test_calendar_native_pages_preserve_identity_rsvp_and_all_day(monkeypatch):
    from types import SimpleNamespace
    calls = []
    pages = [
        {'items': [{'id': str(i), 'summary': 'Event'} for i in range(25)], 'nextPageToken': 'second'},
        {'items': [{'id': 'meeting', 'summary': 'Owner / Alex',
                    'start': {'dateTime': '2026-09-08T10:00:00-07:00'},
                    'attendees': [{'email': 'alex@company.test', 'displayName': 'Alex Example'},
                                  {'email': 'me@test', 'self': True, 'responseStatus': 'accepted'}]},
                   {'id': 'ooo', 'summary': 'OOO', 'start': {'date': '2026-09-07'},
                    'end': {'date': '2026-09-08'}},
                   {'id': 'cancelled', 'status': 'cancelled'}, {'id': '0', 'summary': 'Event'}]}]
    class Service:
        def calendarList(self):
            return SimpleNamespace(list=lambda **kwargs: SimpleNamespace(execute=lambda: {'items': [{'id': 'primary', 'accessRole': 'owner'}]}))
        def events(self): return self
        def list(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(execute=lambda: pages.pop(0))
    events = gg.gather_calendar('unused.py', service=Service())
    assert len(events) == 27
    assert calls[0]['maxResults'] == 250 and calls[1]['pageToken'] == 'second'
    meeting = next(e for e in events if e['provider_event_id'] == 'meeting')
    assert meeting['attendees'][0]['displayName'] == 'Alex Example'
    assert meeting['my_response'] == 'accepted'
    assert next(e for e in events if e['provider_event_id'] == 'ooo')['start'] == '2026-09-07'


def test_calendar_native_fails_on_repeated_page_and_closes_owned_client(monkeypatch):
    import pytest
    from types import SimpleNamespace
    closed = []
    class Service:
        def calendarList(self):
            return SimpleNamespace(list=lambda **kwargs: SimpleNamespace(execute=lambda: {'items': [{'id': 'primary', 'accessRole': 'owner'}]}))
        def events(self): return self
        def list(self, **kwargs):
            return SimpleNamespace(execute=lambda: {'items': [], 'nextPageToken': 'same'})
        def close(self): closed.append(True)
    monkeypatch.setattr(gg, '_token_path', lambda: 'connected')
    monkeypatch.setattr(gg, '_google_service', lambda api, version: Service())
    with pytest.raises(RuntimeError, match='repeated a page token'):
        gg.gather_calendar('unused.py')
    assert closed == [True]


def test_gmail_grant_restored_between_inbox_and_loop_gates_does_not_fail_the_gather(tmp_path, monkeypatch):
    import ledger_io
    import source_context
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, '_refresh_google_token', lambda: None)
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: [])
    monkeypatch.setattr(ledger_io, 'load_active', lambda: [])
    gmail_checks = []
    # Off at the inbox gate, re-granted by the loop gate: the inbox lane never ran.
    monkeypatch.setattr(source_context, 'allowed',
                        lambda source: source != 'gmail' or bool(gmail_checks.append(1) or len(gmail_checks) > 1))
    local, gmail, cal, receipt = [tmp_path / name for name in ('local.json', 'g.json', 'c.json', 'r.json')]
    local.write_text('{}')
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--loop-local', str(local), '--source-results-out', str(receipt)])
    gg.main()
    assert json.loads(gmail.read_text()) == []
    assert len(gmail_checks) >= 2 and json.loads(cal.read_text()) == []
    assert json.loads(receipt.read_text())['gmail']['status'] == 'unavailable'


@pytest.mark.parametrize('count,status', [(0, 'ok'), (2, 'ok'), (gg.PULSE_REFRESH_MAX_MESSAGES, 'partial')])
def test_attended_pulse_is_one_bounded_both_direction_metadata_query(tmp_path, monkeypatch, count, status):
    import source_context
    now = gg.datetime.datetime.now(gg.datetime.timezone.utc)
    since = int(now.timestamp()) - 3600
    out, receipt = tmp_path / 'gmail.json', tmp_path / 'sources.json'
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: pytest.fail('foreground dependency install'))
    monkeypatch.setattr(gg, '_refresh_google_token', lambda: pytest.fail('extra foreground token-check process'))
    calls = []
    def read(api, args, timeout):
        calls.append((api, args, timeout))
        assert args[:2] == ['gmail', 'search'] and args[-2:] == ['--max', str(gg.PULSE_REFRESH_MAX_MESSAGES)]
        assert args[2].startswith(f'after:{since} before:')
        assert 'in:inbox' not in args[2] and 'in:sent' not in args[2] and timeout == 12
        return [{'id': f'm{i}', 'from': 'a@example.com', 'date': now.isoformat(), 'labels': ['SENT']}
                for i in range(count)]
    monkeypatch.setattr(gg, '_run', read)
    monkeypatch.setattr(gg.sys, 'argv', ['gather_google.py', '--pulse-since-epoch', str(since),
        '--gmail-out', str(out), '--cal-out', str(tmp_path / 'cal.json'), '--source-results-out', str(receipt)])
    gg.main()
    result = json.loads(receipt.read_text())['gmail']
    assert len(calls) == 1 and len(json.loads(out.read_text())) == count
    assert result['status'] == status and result['complete'] is (status == 'ok')
    assert result['coverage']['since'] == gg.datetime.datetime.fromtimestamp(since, gg.datetime.timezone.utc).isoformat()


def test_attended_pulse_timeout_or_revoke_never_returns_complete_coverage(tmp_path, monkeypatch):
    import source_context
    now = gg.datetime.datetime.now(gg.datetime.timezone.utc)
    since = int(now.timestamp()) - 60
    out, receipt = tmp_path / 'gmail.json', tmp_path / 'sources.json'
    permission = {'allowed': True}
    monkeypatch.setattr(source_context, 'allowed', lambda source: permission['allowed'])
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg.sys, 'argv', ['gather_google.py', '--pulse-since-epoch', str(since),
        '--gmail-out', str(out), '--cal-out', str(tmp_path / 'cal.json'), '--source-results-out', str(receipt)])
    def timeout(*args, **kwargs):
        assert kwargs['timeout'] == 12
        raise gg.subprocess.TimeoutExpired('private provider request', 12)
    monkeypatch.setattr(gg, '_run', timeout)
    gg.main()
    result = json.loads(receipt.read_text())['gmail']
    assert result['status'] == 'unavailable' and result['complete'] is False
    assert json.loads(out.read_text()) == [] and 'private' not in receipt.read_text()
    def revoked(*args, **kwargs):
        permission['allowed'] = False
        return [{'id': 'private', 'from': 'a@example.com'}]
    monkeypatch.setattr(gg, '_run', revoked)
    gg.main()
    assert json.loads(receipt.read_text())['gmail']['status'] == 'disabled'
    assert json.loads(out.read_text()) == []


def test_attended_refresh_interrupts_a_real_slow_child_and_returns_unavailable(tmp_path, monkeypatch):
    import time
    import source_context
    now = gg.datetime.datetime.now(gg.datetime.timezone.utc)
    slow = tmp_path / 'slow_google.py'
    slow.write_text('import time\ntime.sleep(5)\n')
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    monkeypatch.setattr(gg, '_find_google_api', lambda: str(slow))
    monkeypatch.setattr(gg, 'PULSE_REFRESH_TIMEOUT_SECONDS', 0.05)
    out, receipt = tmp_path / 'gmail.json', tmp_path / 'sources.json'
    monkeypatch.setattr(gg.sys, 'argv', ['gather_google.py', '--pulse-since-epoch', str(int(now.timestamp()) - 60),
        '--gmail-out', str(out), '--cal-out', str(tmp_path / 'cal.json'), '--source-results-out', str(receipt)])
    started = time.monotonic()
    gg.main()
    elapsed = time.monotonic() - started
    assert elapsed < 2  # shortened test-only deadline kills the five-second fake reader
    result = json.loads(receipt.read_text())['gmail']
    assert result['status'] == 'unavailable' and result['complete'] is False
    assert result['error'] == 'TimeoutExpired' and json.loads(out.read_text()) == []


def test_calendar_response_uses_connected_email_not_shared_self(monkeypatch):
    monkeypatch.setattr(gg, 'configured_user_email', lambda: 'owner@example.com')
    event = {'calendar_id': 'shared', 'attendees': [
        {'email': 'colleague@example.com', 'self': True, 'responseStatus': 'accepted'},
        {'email': 'owner@example.com', 'responseStatus': 'declined'}]}
    assert gg.normalize_event(event)['my_response'] == 'declined'
    event['attendees'].pop()
    assert gg.normalize_event(event)['my_response'] == ''


@pytest.mark.parametrize('mode', ['complete', 'fallback-in-thread', 'fallback-read', 'partial-candidate', 'empty-thread'])
def test_stale_id_candidates_keep_exact_original_evidence_and_thread_inventory(monkeypatch, mode):
    import datetime as dt
    now=dt.datetime(2026,10,13,12,tzinfo=dt.timezone.utc)
    old=int((now-dt.timedelta(days=5)).timestamp()*1000)
    def message(mid, headers, snippet):
        return {'id':mid,'threadId':'t','internalDate':str(old),'labelIds':['SENT'],
                'payload':{'headers':[{'name':k,'value':v} for k,v in headers.items()]},'snippet':snippet}
    candidate=message('candidate',{'To':'Original <original@example.com>','Subject':'Original subject'},'original snippet')
    last=message('last',{'To':'Current <current@example.com>','Subject':'Current subject','Cc':'copy@example.com'},'latest snippet')
    last['internalDate']=str(old+1000)
    if mode!='complete':
        last['payload']['headers']=[{'name':'Cc','value':'copy@example.com'}]
        last['snippet']=''
    thread={'id':'t','messages':([candidate] if mode!='fallback-read' else [])+[last]}
    if mode=='partial-candidate':
        thread['messages'][0]={'id':'candidate','internalDate':str(old),'payload':{'headers':[]},'snippet':''}
    if mode=='empty-thread':
        thread['messages']=None
    original=[gg.normalize_email({'id':'candidate','threadId':'t','to':'Original <original@example.com>',
                                  'subject':'Original subject','snippet':'original snippet'}, {})]
    expected=gg.stale_from_threads(original, {'t':thread}, now)
    def search(api,args,timeout=60):
        assert args==['gmail','search','in:sent older_than:3d newer_than:14d -in:chats','--max','40','--ids-only']
        assert timeout==60
        return [{'id':'candidate','threadId':'t'}]
    monkeypatch.setattr(gg,'_run',search)
    calls=[]
    class Service:
        kind=''
        def users(self):return self
        def threads(self):self.kind='thread';return self
        def messages(self):self.kind='message';return self
        def get(self,**kwargs):
            calls.append((self.kind,kwargs))
            value=thread if self.kind=='thread' else candidate
            return type('Call',(),{'execute':lambda _:value})()
    actual=gg.gather_stale_sent('/fake',service=Service(),now=now)
    assert list(actual)==expected
    assert actual.threads=={'t':thread}
    assert not actual.rate_limited
    assert [kind for kind,_ in calls]==(['thread','message'] if mode in ('fallback-read','partial-candidate') else ['thread'])
    if mode in ('fallback-read','partial-candidate'):
        assert calls[-1][1]=={'userId':'me','id':'candidate','format':'metadata','metadataHeaders':['From','To','Subject','Date']}


def test_stale_fallback_quota_preserves_completed_threads_and_stops_reads(monkeypatch):
    import datetime as dt
    now=dt.datetime(2026,10,13,12,tzinfo=dt.timezone.utc)
    old=now-dt.timedelta(days=5)
    monkeypatch.setattr(gg,'_run',lambda *a,**k:[{'id':f'm{i}','threadId':f't{i}'} for i in range(3)])
    calls=[]
    class QuotaError(Exception):
        resp=type('Response',(),{'status':429})()
    class Service:
        kind=''
        def users(self):return self
        def threads(self):self.kind='thread';return self
        def messages(self):self.kind='message';return self
        def get(self,**kwargs):
            calls.append((self.kind,kwargs['id']))
            if self.kind=='message':
                return type('Call',(),{'execute':lambda _:(_ for _ in ()).throw(QuotaError())})()
            thread=_thread(kwargs['id'],[(old,True,'person@example.com','Subject','Complete context')])
            if kwargs['id']=='t1':
                thread['messages'][0]['snippet']=''
            return type('Call',(),{'execute':lambda _:thread})()
    result=gg.gather_stale_sent('/fake',service=Service(),now=now)
    assert calls==[('thread','t0'),('thread','t1'),('message','m1')]
    assert result.rate_limited and set(result.threads)=={'t0'}
    assert [row['threadId'] for row in result]==['t0']


@pytest.fixture
def attendee_thread(monkeypatch):
    import base64
    import datetime
    import source_context
    now = datetime.datetime.now(datetime.timezone.utc)
    def message(index, body, sender='Dana <dana@acme.com>'):
        stamp = now - datetime.timedelta(days=16-index)
        return {'id': f'm{index}', 'threadId': 'thread-1', 'internalDate': str(int(stamp.timestamp()*1000)),
                'labelIds': ['INBOX'], 'snippet': body[:20],
                'payload': {'mimeType': 'text/plain', 'headers': [
                    {'name': 'From', 'value': sender}, {'name': 'To', 'value': 'owner@example.com'},
                    {'name': 'Cc', 'value': 'dana@acme.com'}, {'name': 'Date', 'value': stamp.isoformat()},
                    {'name': 'Subject', 'value': 'Intro then scheduling'}],
                    'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()}}}
    messages = [message(0, 'Happy to connect you both. Discuss the new infrastructure.', 'Introducer <intro@other.com>')]
    messages += [message(i, f'Scheduling reply {i}') for i in range(1, 7)]
    messages.append(message(8, 'Correction: 10 on the 14th is easiest.'))
    search = [{'id': row['id'], 'threadId': 'thread-1', 'from': 'Dana <dana@acme.com>',
               'subject': 'Intro then scheduling', 'snippet': row['snippet'], 'date': row['internalDate']} for row in reversed(messages[3:])]
    calls=[]; grants={'gmail'}; response={'id': 'thread-1', 'messages': messages}; fail=[]
    class Service:
        def users(self): return self
        def threads(self): return self
        def get(self, **kwargs):
            calls.append(kwargs)
            return self
        def execute(self):
            if fail:
                raise fail[0]
            return response
        def close(self): pass
    monkeypatch.setattr(source_context, 'allowed', lambda source: source in grants)
    monkeypatch.setattr(gg, '_gmail_service', Service)
    def run(api,args,**kwargs):
        assert 'cc:dana@acme.com' in args[2]
        return search
    monkeypatch.setattr(gg, '_run', run)
    return response, messages, search, calls, grants, fail, message


def test_focused_attendee_threads_keep_intro_and_latest_correction_for_local_prep_only(attendee_thread, monkeypatch, tmp_path):
    import compose_meeting_prep as prep
    import research_attendees as research
    response, messages, search, calls, grants, fail, message = attendee_thread
    email, rows = gg._fetch_attendee_comms('fake', 'dana@acme.com', expand_threads=True)
    assert len(calls) == 1 and calls[0] == {'userId': 'me', 'id': 'thread-1', 'format': 'full'}
    assert len(rows) == 1  # full thread replaces duplicate current search snippets
    assert 'Happy to connect you both' in rows[0]['thread_context']
    assert 'Correction: 10 on the 14th' in rows[0]['thread_context']
    prompt = '\n'.join(prep._thread_lines(rows))
    assert 'Introducer <intro@other.com>' in prompt
    assert 'Happy to connect you both' in prompt
    assert 'Correction: 10 on the 14th' in prompt
    sent = []
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_LLM_STUB', raising=False)
    monkeypatch.setattr(research.wr, 'provider_chain', lambda _: ['parallel'])
    monkeypatch.setattr(research.wr, '_parallel_task',
                        lambda prompt, *args: sent.append(prompt) or {'attendees': []})
    research.research([{'name': 'Dana Example', 'email': email}], 'PRIVATE-MEETING',
                      {email: rows}, focus='Dana Example')
    assert sent
    assert all('Happy to connect you both' not in prompt and 'Introducer <intro@other.com>' not in prompt
               and rows[0]['snippet'] not in prompt and 'PRIVATE-MEETING' not in prompt for prompt in sent)


def test_muted_sender_text_never_enters_thread_background(attendee_thread, monkeypatch):
    """Compose drops a muted sender's own rows; their words in a shared thread are dropped by the
    one bounded thread reader, for both focused prep and the brief cohort, before rendering."""
    import preferences
    response, messages, search, calls, grants, fail, message = attendee_thread
    owner_reply = message(5, 'Owner reply: Tuesday works.', 'Owner <owner@example.com>')
    owner_reply['labelIds'] = ['SENT']
    messages[5] = owner_reply
    monkeypatch.setattr(preferences, 'load_explicit', lambda: {'mute_senders': ['@other.com', 'owner@example.com']})
    email, rows = gg._fetch_attendee_comms('fake', 'dana@acme.com', expand_threads=True)
    context = rows[0]['thread_context']
    assert 'Happy to connect you both' not in context and 'intro@other.com' not in context
    assert 'Correction: 10 on the 14th' in context and 'partial' in context
    assert 'Owner reply: Tuesday works.' in context  # the owner's own SENT words are never muted
    monkeypatch.setattr(gg, '_token_path', lambda: 'connected')
    monkeypatch.setattr(gg, '_run', lambda *a, **k: [
        {'id': 'm8', 'threadId': 'thread-1', 'labelIds': ['INBOX'], 'snippet': 'Thin excerpt'}])
    brief = gg.gather_gmail('fake', 2, 2, attachments=False)
    assert brief and 'Correction: 10 on the 14th' in brief[0]['body']
    assert 'Happy to connect you both' not in json.dumps(brief)


@pytest.mark.parametrize('case', ['denied', 'revoked', 'rate_limit', 'wrong_thread', 'missing_payload'])
def test_focused_thread_failure_keeps_only_permitted_partial_search(attendee_thread, monkeypatch, case):
    response, messages, search, calls, grants, fail, message = attendee_thread
    if case == 'denied':
        grants.clear()
    elif case == 'revoked':
        class Response(dict):
            def get(self, key, default=None):
                grants.clear()
                return super().get(key, default)
        response['messages'] = Response()  # malformed after the read and revoke
    elif case == 'rate_limit':
        error = RuntimeError('provider body must stay out of context')
        error.resp = type('Response', (), {'status': 429})()
        fail.append(error)
        search[1] = {**search[0], 'id': 'other', 'threadId': 'thread-2'}
    elif case == 'wrong_thread':
        response['id'] = 'unrelated'
    else:
        messages[0].pop('payload')
    if case == 'revoked':
        original = gg._gmail_service
        def service():
            client = original()
            execute = client.execute
            def revoke():
                result = execute(); grants.clear(); return result
            client.execute = revoke
            return client
        monkeypatch.setattr(gg, '_gmail_service', service)
    _, rows = gg._fetch_attendee_comms('fake', 'dana@acme.com', expand_threads=True)
    if case in ('denied', 'revoked'):
        assert rows == []
        if case == 'denied':
            assert calls == []
    else:
        assert len(calls) == 1
        assert rows and all('Happy to connect' not in row.get('thread_context', '') for row in rows)
        assert 'unavailable' in rows[0]['thread_context']
        assert all('provider body' not in row.get('thread_context', '') for row in rows)


def test_focused_thread_caps_preserve_opening_latest_and_exclude_non_evidence(attendee_thread):
    response, messages, search, calls, grants, fail, message = attendee_thread
    response['messages'] = [message(i, ('opening ' if i==0 else 'latest ' if i==15 else 'middle ') + 'x'*5000) for i in range(16)]
    response['messages'].append({**message(16, 'DRAFT DO NOT INCLUDE'), 'labelIds': ['DRAFT']})
    _, rows = gg._fetch_attendee_comms('fake', 'dana@acme.com', expand_threads=True)
    context = rows[0]['thread_context']
    assert len(context) <= gg.ATTENDEE_THREAD_CHARS
    assert 'opening ' in context and 'latest ' in context and 'DRAFT DO NOT INCLUDE' not in context
    assert 'partial' in context
    assert context.count('[message middle omitted]') == gg.ATTENDEE_THREAD_MESSAGES



def test_focused_thread_read_cap_and_shared_quota_stop(attendee_thread):
    import threading
    response, messages, search, calls, grants, fail, message = attendee_thread
    search[1] = {**search[0], 'id': 'other', 'threadId': 'thread-2'}
    search[2] = {**search[0], 'id': 'third', 'threadId': 'thread-3'}
    _, rows = gg._fetch_attendee_comms('fake', 'dana@acme.com', expand_threads=True)
    assert len(calls) == gg.ATTENDEE_THREAD_CAP
    stop = threading.Event(); stop.set()
    assert gg._fetch_attendee_comms('fake', 'dana@acme.com', expand_threads=True, quota_stop=stop) == ('dana@acme.com', [])
    assert len(calls) == gg.ATTENDEE_THREAD_CAP



def test_focused_thread_long_latest_ending_survives(attendee_thread):
    response, messages, search, calls, grants, fail, message = attendee_thread
    messages[-1] = message(8, 'Initial proposal. ' + 'x'*12000 + ' Final correction: do not meet on Friday.')
    _, rows = gg._fetch_attendee_comms('fake', 'dana@acme.com', expand_threads=True)
    text = rows[0]['thread_context']
    assert 'Initial proposal.' in text
    assert 'Final correction: do not meet on Friday.' in text
    assert '[message middle omitted]' in text and 'partial' in text



def test_focused_thread_context_has_one_run_budget_across_fifteen_attendees(tmp_path, monkeypatch):
    import base64
    import datetime
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda _: True)
    now = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp()*1000)-1000)
    class Service:
        def users(self): return self
        def threads(self): return self
        def get(self, **kwargs): self.tid=kwargs['id']; return self
        def execute(self):
            return {'id': self.tid, 'messages': [{'id': f'm{i}', 'threadId': self.tid,
                'internalDate': now, 'labelIds': ['INBOX'], 'payload': {'mimeType': 'text/plain',
                'headers': [{'name': 'From', 'value': 'Person <person@example.test>'}],
                'body': {'data': base64.urlsafe_b64encode((f'opening{i} '+'x'*12000+f' ending{i}').encode()).decode()}}}
                for i in range(10)]}
        def close(self): pass
    monkeypatch.setattr(gg, '_gmail_service', Service)
    monkeypatch.setattr(gg, '_run', lambda *a, **k: [{'id': 'a', 'threadId': 'one'}, {'id': 'b', 'threadId': 'two'}])
    path=tmp_path/'attendees.json';path.write_text(json.dumps([{'email': f'person{i}@example.test'} for i in range(15)]))
    result = gg.gather_attendee_comms('fake', str(path), expand_threads=True)
    assert len(result) == 15
    assert sum(len(row.get('thread_context', '')) for rows in result.values() for row in rows) <= gg.ATTENDEE_THREADS_TOTAL_CHARS
    for rows in result.values():
        for row in rows:
            assert 'opening0' in row['thread_context'] and 'ending9' in row['thread_context']
            assert 'partial' in row['thread_context']



def test_partial_thread_inventory_does_not_discard_current_search_snippets(attendee_thread):
    response, messages, search, calls, grants, fail, message = attendee_thread
    response['messages'] = messages[:1]
    _, rows = gg._fetch_attendee_comms('fake', 'dana@acme.com', expand_threads=True)
    assert len(rows) == len(search)
    assert {row['message_id'] for row in rows} == {row['id'] for row in search}
    assert 'partial' in rows[0]['thread_context']
    assert 'Happy to connect you both' in rows[0]['thread_context']
    assert rows[-1]['snippet'] == search[-1]['snippet']


def test_brief_reads_one_exact_thread_for_current_messages_and_keeps_background_separate(attendee_thread, monkeypatch):
    import compose_brief as compose
    import render_local
    response, messages, search, calls, grants, fail, message = attendee_thread
    opening = message(0, 'Loved your article.')
    proposal = message(1, 'How about 11 am at the park on October 21?', 'Owner <owner@example.com>')
    proposal['labelIds'] = ['SENT']
    correction = message(7, 'Can we do 12:30 instead on that day?')
    latest = message(8, 'My earlier meeting that day was cancelled. 11 am works again.')
    draft = {**message(9, 'Unsent proposal: October 30.'), 'labelIds': ['DRAFT']}
    response['messages'] = [opening, proposal, correction, latest, draft]
    monkeypatch.setattr(gg, '_token_path', lambda: 'connected')
    monkeypatch.setattr(gg, '_run', lambda *a, **k: [
        {'id': m['id'], 'threadId': 'thread-1', 'labelIds': ['INBOX'], 'snippet': 'Thin excerpt'}
        for m in (latest, correction)])
    rows = gg.gather_gmail('fake', 2, 2, attachments=False)
    assert calls == [{'userId': 'me', 'id': 'thread-1', 'format': 'full'}]
    assert rows[0]['id'] == latest['id']
    assert rows[0]['body'] == 'My earlier meeting that day was cancelled. 11 am works again.'
    assert 'October 21' not in rows[0]['body']  # older evidence must not inherit the latest message ID
    assert sum(bool(row.get('thread_context')) for row in rows) == 1
    assert 'October 21' in rows[0]['thread_context'] and 'owner sent' in rows[0]['thread_context']
    assert 'October 30' not in rows[0]['thread_context']
    rendered = render_local._format_emails([render_local._trim_email(row) for row in rows])
    assert 'October 21' in rendered and "not part of the current MessageId's body" in rendered
    manifest = compose.build_data_manifest({'google': {'emails': rows}})
    assert 'October 21' in json.dumps(manifest) and 'October 30' not in json.dumps(manifest)


@pytest.mark.parametrize('case', ['wrong_thread', 'missing_target', 'revoked', 'quota'])
def test_brief_thread_failure_does_not_retry_or_leak_background(attendee_thread, monkeypatch, case):
    import threading
    response, messages, search, calls, grants, fail, message = attendee_thread
    if case == 'wrong_thread':
        response['id'] = 'other-thread'
    elif case == 'missing_target':
        response['messages'] = messages[:-1]
    elif case == 'quota':
        error = RuntimeError('private provider text')
        error.resp = type('Response', (), {'status': 429})()
        fail.append(error)
    else:
        original = gg._gmail_service
        def service():
            client = original(); execute = client.execute
            def revoke():
                value = execute(); grants.clear(); return value
            client.execute = revoke
            return client
        monkeypatch.setattr(gg, '_gmail_service', service)
    stop = threading.Event()
    assert gg._fetch_thread_bodies('thread-1', ['m8'], stop) == {}
    assert len(calls) == 1
    assert stop.is_set() is (case == 'quota')


def test_brief_background_retains_middle_agreement_without_repeating_current_messages(attendee_thread, monkeypatch):
    response, messages, search, calls, grants, fail, message = attendee_thread
    response['messages'] = [message(i, f'Historical message {i}.') for i in range(8)]
    response['messages'][3] = message(3, 'Coffee is October 21 at 11 AM.', 'Owner <owner@example.com>')
    response['messages'][3]['labelIds'] = ['SENT']
    response['messages'][-1] = message(7, 'The earlier conflict cleared; that time works.')
    monkeypatch.setattr(gg, '_token_path', lambda: 'connected')
    # Use the fixture's actual opaque message ID rather than inventing a selector.
    current = response['messages'][-1]
    monkeypatch.setattr(gg, '_run', lambda *a, **k: [
        {'id': current['id'], 'threadId': 'thread-1', 'labelIds': ['INBOX'], 'snippet': 'Thin excerpt'}])
    rows = gg.gather_gmail('fake', 2, 2, attachments=False)
    assert len(calls) == 1
    assert 'October 21 at 11 AM' in rows[0]['thread_context']
    assert 'The earlier conflict cleared' not in rows[0]['thread_context']
    assert 'The earlier conflict cleared' in rows[0]['body']
    assert len(rows[0]['thread_context']) <= gg.BRIEF_THREAD_CHARS


def test_sent_body_cohort_preserves_native_bcc(monkeypatch):
    import base64
    from gmail_read import message_fields
    raw = {'id': 's1', 'threadId': 't1', 'labelIds': ['SENT'], 'internalDate': '1780000000000',
           'payload': {'mimeType': 'text/plain', 'headers': [
               {'name': 'From', 'value': 'owner@example.test'},
               {'name': 'To', 'value': 'recipient@example.test'},
               {'name': 'Bcc', 'value': 'introducer@example.test'}],
               'body': {'data': base64.urlsafe_b64encode(b'Thanks for the introduction.').decode()}}}
    calls = []
    def read(_api, args, timeout=60):
        calls.append(args)
        return [_sent_row('s1')] if args[:2] == ['gmail', 'search'] else message_fields(raw)
    monkeypatch.setattr(gg, '_token_path', lambda: None)
    monkeypatch.setattr(gg, '_run', read)
    rows = gg.gather_sent('fixture-api', max_n=1, bodies=1)
    assert ['gmail', 'get', 's1'] in calls
    assert rows[0]['bcc'] == 'introducer@example.test'
    assert rows[0]['isSent'] is True
    assert rows[0]['body'] == 'Thanks for the introduction.'
