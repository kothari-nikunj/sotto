"""Offline contracts for only the Google verbs Sotto currently consumes."""
import ast
import base64
import importlib.util
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest


class FakeClock:
    def __init__(self):
        self.now, self.waits = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


def module():
    path = Path(__file__).with_name('google_api.py')
    spec = importlib.util.spec_from_file_location('sotto_google_api_test', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    result.test_clock = FakeClock()
    result._GMAIL_METADATA_PACER = result._MetadataPacer(
        result.test_clock.monotonic, result.test_clock.sleep)
    return result


class Call:
    def __init__(self, result): self.result = result
    def execute(self): return self.result


class GmailMessages:
    def __init__(self): self.calls = []
    def list(self, **kwargs):
        self.calls.append(('list', kwargs))
        if kwargs.get('pageToken'):
            return Call({'messages': [{'id': 'm2', 'threadId': 't2'}]})
        return Call({'messages': [{'id': 'm1', 'threadId': 't1'}], 'nextPageToken': 'next'})
    def get(self, **kwargs):
        self.calls.append(('get', kwargs))
        ident = kwargs['id']
        payload = {'headers': [{'name': 'From', 'value': 'Ada <ada@example.com>'},
                               {'name': 'To', 'value': 'owner@example.com'},
                               {'name': 'Subject', 'value': 'Hello'},
                               {'name': 'Date', 'value': 'Wed, 30 Sep 2026 10:00:00 -0700'},
                               {'name': 'Message-ID', 'value': '<origin@example.com>'},
                               {'name': 'References', 'value': '<prior@example.com>'}],
                   'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(b'full body').decode()}}
        return Call({'id': ident, 'threadId': 't' + ident[-1], 'snippet': 'snippet ' + ident,
                     'labelIds': ['INBOX'], 'payload': payload})
    def send(self, **kwargs):
        self.calls.append(('send', kwargs))
        return Call({'id': 'sent-1', 'threadId': kwargs['body'].get('threadId', 'new-thread')})


class Gmail:
    def __init__(self): self.messages_api = GmailMessages(); self.closed = False
    def users(self): return self
    def messages(self): return self.messages_api
    def close(self): self.closed = True


class CalendarEvents:
    def __init__(self): self.calls = []
    def list(self, **kwargs):
        self.calls.append(('list', kwargs))
        if kwargs.get('pageToken'):
            return Call({'items': [{'id': 'e2', 'summary': 'Second'}]})
        return Call({'items': [{'id': 'e1', 'summary': 'First'}, {'id': 'cancelled', 'status': 'cancelled'}],
                     'nextPageToken': 'next'})
    def get(self, **kwargs): self.calls.append(('get', kwargs)); return Call({'id': kwargs['eventId']})
    def insert(self, **kwargs): self.calls.append(('insert', kwargs)); return Call({'id': 'new-event', 'htmlLink': 'https://calendar.google.com/event'})
    def patch(self, **kwargs): self.calls.append(('patch', kwargs)); return Call({'id': kwargs['eventId']})
    def delete(self, **kwargs): self.calls.append(('delete', kwargs)); return Call({})


class Calendar:
    def __init__(self): self.events_api = CalendarEvents(); self.closed = False
    def calendarList(self):  # noqa: N802 - Google SDK interface
        return SimpleNamespace(list=lambda **kwargs: Call({'items': [{'id': 'primary', 'accessRole': 'owner'}]}))
    def events(self): return self.events_api
    def close(self): self.closed = True


def test_gmail_search_get_and_send_reply_shapes_without_credentials(monkeypatch):
    api = module(); gmail = Gmail()
    factory = lambda kind, version: gmail if (kind, version) == ('gmail', 'v1') else pytest.fail('wrong service')
    search = api.execute(api.build_parser().parse_args(['gmail', 'search', 'newer_than:1d', '--max', '4']), factory)
    assert [item['id'] for item in search] == ['m1', 'm2']
    assert search[0]['from'] == 'Ada <ada@example.com>' and search[0]['labels'] == ['INBOX']
    assert any(kind == 'list' and args.get('pageToken') == 'next' for kind, args in gmail.messages_api.calls)
    full = api.execute(api.build_parser().parse_args(['gmail', 'get', 'm1']), factory)
    assert full['body'] == 'full body' and full['threadId'] == 't1'
    sent = api.execute(api.build_parser().parse_args(['gmail', 'send', '--to', 'ada@example.com', '--subject', 'Hi', '--body', 'Thanks']), factory)
    assert sent == {'status': 'sent', 'id': 'sent-1', 'threadId': 'new-thread'}
    reply = api.execute(api.build_parser().parse_args(['gmail', 'reply', 'm1', '--body', 'Yes']), factory)
    assert reply == {'status': 'sent', 'id': 'sent-1', 'threadId': 't1'}
    raw = gmail.messages_api.calls[-1][1]['body']['raw']
    decoded = base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode()
    assert 'To: ada@example.com' in decoded and 'In-Reply-To: <origin@example.com>' in decoded
    assert 'References: <prior@example.com> <origin@example.com>' in decoded
    assert gmail.closed


def _search_factory(ids, read, *, list_page=None):
    """Provider double that rejects shared transports and records exact request counts."""
    clients, calls = [], []

    class Service:
        def __init__(self):
            self.owner = threading.get_ident()
            self.closed = False
            self.closed_signal = threading.Event()
            clients.append(self)

        def users(self):
            assert threading.get_ident() == self.owner
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            calls.append(('list', kwargs))
            return Call(list_page(kwargs) if list_page else
                        {'messages': [{'id': mid, 'threadId': 't-' + mid} for mid in ids]})

        def get(self, **kwargs):
            assert kwargs['format'] == 'metadata'
            assert kwargs['metadataHeaders'] == ['From', 'To', 'Subject', 'Date']
            calls.append(('get', kwargs))
            self.mid = kwargs['id']
            return type('Read', (), {'execute': lambda _: read(kwargs['id'])})()

        def close(self):
            assert threading.get_ident() == self.owner
            self.closed = True
            self.closed_signal.set()

    def factory(kind, version):
        assert (kind, version) == ('gmail', 'v1')
        return Service()

    return factory, clients, calls


def test_search_metadata_uses_two_bounded_rounds_preserves_order_and_reuses_clients():
    api = module()
    ids = [f'm{i}' for i in range(10)]
    barrier = threading.Barrier(api.GMAIL_METADATA_WORKERS, timeout=2)
    rounds, starts = [], []

    def admission_clock():
        now = api.test_clock.monotonic()
        # The injected clock runs under the pacer's lock. Record eligible starts
        # there, before another worker can advance the shared fake clock.
        if now >= api._GMAIL_METADATA_PACER._next_start:
            starts.append(now)
        return now
    api._GMAIL_METADATA_PACER._clock = admission_clock

    def read(mid):
        # Ten serial reads cannot reach this barrier; five worker transports finish
        # in two request rounds, independent of machine speed or completion order.
        rounds.append(mid)
        barrier.wait()
        return {'snippet': 'snippet ' + mid, 'internalDate': '123',
                'labelIds': ['SENT'], 'payload': {'headers': [{'name': 'To', 'value': 'a@example.com'}]}}

    factory, clients, calls = _search_factory(ids, read)
    rows = api.execute(api.build_parser().parse_args(['gmail', 'search', 'in:sent', '--max', '10']), factory)
    assert [row['id'] for row in rows] == ids  # IDs/thread IDs may come from the list response
    assert [row['threadId'] for row in rows] == ['t-' + mid for mid in ids]
    assert all(row['labels'] == ['SENT'] and row['to'] == 'a@example.com' and row['date'] == '123'
               for row in rows)
    assert set(rounds[:5]) == set(ids[:5]) and set(rounds[5:]) == set(ids[5:])
    assert starts == [index * 0.25 for index in range(10)]
    assert api.test_clock.waits == [0.25] * 9
    assert len(clients) == 1 + api.GMAIL_METADATA_WORKERS
    assert all(client.closed for client in clients)
    assert sum(kind == 'list' for kind, _ in calls) == 1
    assert sorted(args['id'] for kind, args in calls if kind == 'get') == sorted(ids)


def test_pacing_spans_sequential_searches_including_single_message():
    api = module()
    starts = []
    factory, clients, _ = _search_factory(['m0'], lambda mid: (
        starts.append(api.test_clock.now) or {'id': mid, 'payload': {}}))
    args = api.build_parser().parse_args(['gmail', 'search', 'q', '--max', '1'])
    for _ in range(3):
        assert api.execute(args, factory)[0]['id'] == 'm0'
    assert starts == [0.0, 0.25, 0.5]
    # Idle time grants one immediate start, never a catch-up burst.
    api.test_clock.now = 10.0
    api.execute(args, factory)
    api.execute(args, factory)
    assert starts[-2:] == [10.0, 10.25]
    assert all(client.closed for client in clients)


def test_pacing_rechecks_clock_after_early_wake_and_stop_before_request():
    api = module()
    clock = api.test_clock
    def early_wait(delay):
        clock.sleep(min(0.125, delay))
    pacer = api._MetadataPacer(clock.monotonic, early_wait)
    assert pacer.wait_turn() is True
    assert pacer.wait_turn() is True
    assert clock.now == 0.25 and clock.waits == [0.125, 0.125]

    stop = threading.Event()
    def fail_while_waiting(delay):
        clock.sleep(delay)
        stop.set()
    pacer._wait = fail_while_waiting
    api._GMAIL_METADATA_PACER = pacer
    gmail = Gmail()
    assert api._gmail_metadata(gmail, {'id': 'never-read'}, stop) is None
    assert gmail.messages_api.calls == []
    assert pacer.wait_turn(stop) is False


@pytest.mark.parametrize('rate_limited', [False, True])
def test_search_metadata_error_stops_queued_reads_fails_whole_search_and_closes_clients(monkeypatch, rate_limited):
    api = module()
    monkeypatch.setattr(api, 'GMAIL_METADATA_WORKERS', 2)
    started = threading.Barrier(2, timeout=2)
    failed = threading.Event()

    class ProviderError(Exception):
        resp = type('Response', (), {'status': 429})()

    error = ProviderError('rate limited') if rate_limited else RuntimeError('message unavailable')

    def read(mid):
        started.wait()
        if mid == 'm0':
            failed.set()
            raise error
        assert failed.wait(timeout=2)
        # Wait for the stop flag to be set by the failing worker before returning.
        # Its client closes only after that flag is set.
        failing_client = next(client for client in clients if getattr(client, 'mid', None) == 'm0')
        assert failing_client.closed_signal.wait(timeout=2)
        return {'id': mid, 'payload': {}}

    factory, clients, calls = _search_factory([f'm{i}' for i in range(8)], read)
    with pytest.raises(type(error)) as raised:
        api.execute(api.build_parser().parse_args(['gmail', 'search', 'q', '--max', '8']), factory)
    assert raised.value is error  # preserve typed provider status for the caller's quota handling
    assert sorted(args['id'] for kind, args in calls if kind == 'get') == ['m0', 'm1']
    assert all(client.closed for client in clients)


def test_search_finishes_pagination_before_metadata_and_preserves_per_query_cap():
    api = module()

    def page(kwargs):
        if not kwargs.get('pageToken'):
            assert kwargs['maxResults'] == 3
            return {'messages': [{'id': 'm0'}, {'id': 'm1'}], 'nextPageToken': 'second'}
        assert kwargs['pageToken'] == 'second' and kwargs['maxResults'] == 1
        return {'messages': [{'id': 'm2'}, {'id': 'm3'}], 'nextPageToken': 'not-read-past-cap'}

    factory, clients, calls = _search_factory([], lambda mid: {'id': mid, 'payload': {}}, list_page=page)
    rows = api.execute(api.build_parser().parse_args(['gmail', 'search', 'q', '--max', '3']), factory)
    assert [row['id'] for row in rows] == ['m0', 'm1', 'm2']
    assert [kind for kind, _ in calls] == ['list', 'list', 'get', 'get', 'get']
    assert all(client.closed for client in clients)


@pytest.mark.parametrize('failure', ['provider', 'repeated_page'])
def test_search_list_failure_never_starts_metadata_workers(failure):
    api = module()

    def page(kwargs):
        if kwargs.get('pageToken') and failure == 'provider':
            raise RuntimeError('provider unavailable')
        return {'messages': [{'id': 'm'}], 'nextPageToken': 'same-page'}

    factory, clients, calls = _search_factory([], lambda _: pytest.fail('read after incomplete list'), list_page=page)
    with pytest.raises(RuntimeError):
        api.execute(api.build_parser().parse_args(['gmail', 'search', 'q']), factory)
    assert len(clients) == 1 and clients[0].closed
    assert all(kind == 'list' for kind, _ in calls)


@pytest.mark.parametrize('ids', [[], ['m0']])
def test_empty_or_single_search_reuses_listing_client(ids):
    api = module()
    factory, clients, calls = _search_factory(ids, lambda mid: {'id': mid, 'payload': {}})
    rows = api.execute(api.build_parser().parse_args(['gmail', 'search', 'q']), factory)
    assert [row['id'] for row in rows] == ids
    assert len(clients) == 1 and clients[0].closed
    assert len(calls) == 1 + len(ids)


def test_calendar_list_create_get_patch_delete_shapes():
    api = module(); calendar = Calendar()
    factory = lambda kind, version: calendar if (kind, version) == ('calendar', 'v3') else pytest.fail('wrong service')
    stamp1, stamp2 = '2026-09-30T10:00:00-07:00', '2026-09-30T10:30:00-07:00'
    listed = api.execute(api.build_parser().parse_args(['calendar', 'list', '--start', stamp1, '--end', stamp2]), factory)
    assert [event['id'] for event in listed] == ['e1', 'e2']
    assert api.execute(api.build_parser().parse_args(['calendar', 'get', 'e1']), factory) == {'id': 'e1'}
    created = api.execute(api.build_parser().parse_args(['calendar', 'create', '--summary', 'Sync', '--start', stamp1,
        '--end', stamp2, '--attendees', 'ada@example.com', '--location', 'Room', '--description', 'Notes']), factory)
    assert created['id'] == 'new-event'
    insert = next(args for kind, args in calendar.events_api.calls if kind == 'insert')
    assert insert['body']['attendees'] == [{'email': 'ada@example.com'}]
    assert insert['body']['location'] == 'Room' and insert['sendUpdates'] == 'all'
    patched = api.execute(api.build_parser().parse_args(['calendar', 'patch', 'e1', '--attendees-json',
        '[{"email":"ada@example.com","responseStatus":"accepted"}]', '--send-updates', 'all']), factory)
    assert patched['id'] == 'e1'
    patch = next(args for kind, args in calendar.events_api.calls if kind == 'patch')
    assert patch['body']['attendees'][0]['responseStatus'] == 'accepted'
    assert api.execute(api.build_parser().parse_args(['calendar', 'delete', 'e1']), factory) == {'status': 'deleted', 'id': 'e1'}
    assert calendar.closed


@pytest.mark.parametrize('argv', [
    ['gmail', 'send', '--to', 'ada@example.com', '--body', 'Hello'],
    ['gmail', 'reply', 'm1', '--body', 'Hello'],
    ['calendar', 'create', '--summary', 'S', '--start', '2026-09-30T10:00:00Z', '--end', '2026-09-30T11:00:00Z'],
    ['calendar', 'patch', 'e1', '--location', 'Room'],
    ['calendar', 'delete', 'e1'],
])
def test_unattended_write_refused_before_service_construction(monkeypatch, argv):
    api = module(); monkeypatch.setenv('SOTTO_UNATTENDED', '1')
    called = []
    with pytest.raises(PermissionError, match='unattended'):
        api.execute(api.build_parser().parse_args(argv), lambda *a: called.append(a))
    assert called == []


def test_main_json_and_sanitized_failure(capsys):
    api = module(); gmail = Gmail()
    assert api.main(['gmail', 'search', 'in:sent', '--max', '1'], lambda *_: gmail) == 0
    assert json.loads(capsys.readouterr().out)[0]['id'] == 'm1'
    def fail(*_): raise RuntimeError('secret provider payload')
    assert api.main(['gmail', 'search', 'in:sent'], fail) == 1
    captured = capsys.readouterr()
    assert 'secret provider payload' not in captured.err


def test_write_recipient_validation_precedes_provider_call():
    api = module(); gmail = Gmail(); calendar = Calendar()
    with pytest.raises(ValueError, match='recipient'):
        api.execute(api.build_parser().parse_args(
            ['gmail', 'send', '--to', 'invalid', '--body', 'Hello']), lambda *_: gmail)
    assert not gmail.messages_api.calls
    with pytest.raises(ValueError, match='attendees'):
        api.execute(api.build_parser().parse_args(
            ['calendar', 'create', '--summary', 'Sync', '--start', '2026-09-30T10:00:00Z',
             '--end', '2026-09-30T11:00:00Z', '--attendees', 'invalid']), lambda *_: calendar)
    assert not calendar.events_api.calls


def test_main_http_status_diagnostic_never_exposes_provider_details(capsys):
    api = module()
    class HttpError(Exception):
        resp = type('Response', (), {'status': 403})()
    def fail(*_):
        raise HttpError('private token URL and provider body')
    assert api.main(['gmail', 'search', 'in:sent'], fail) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'Google operation unavailable or refused. diagnostic=provider_http http_status=403\n'


def test_main_unrecognized_class_and_status_text_are_not_emitted(capsys):
    api = module()
    class PrivateTokenError(Exception):
        response = type('Response', (), {'status_code': '403 private body'})()
    def fail(*_):
        raise PrivateTokenError('secret URL')
    assert api.main(['gmail', 'search', 'in:sent'], fail) == 1
    assert capsys.readouterr().err == 'Google operation unavailable or refused. diagnostic=unavailable\n'


@pytest.mark.parametrize('reason', [
    'rateLimitExceeded', 'userRateLimitExceeded', 'quotaExceeded',
    'insufficientPermissions', 'forbidden', 'authError',
    'accessNotConfigured', 'backendError',
])
def test_main_http_reason_diagnostic_is_fixed_and_omits_all_other_content(capsys, reason):
    api = module()
    class HttpError(Exception):
        resp = type('Response', (), {'status': 403})()
        content = json.dumps({'error': {'message': 'secret token and URL', 'errors': [
            {'reason': reason, 'message': 'private body', 'location': 'secret URL'},
        ]}}).encode()
    def fail(*_):
        raise HttpError('private exception text')
    assert api.main(['gmail', 'search', 'in:sent'], fail) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == ('Google operation unavailable or refused. '
                            f'diagnostic=provider_http http_status=403 provider_reason={reason}\n')


@pytest.mark.parametrize('content', [
    b'not JSON', b'\xff', b'[]', b'{"error": []}',
    b'{"error": {"errors": "rateLimitExceeded"}}',
    b'{"error": {"errors": [{"reason": ["rateLimitExceeded"]}]}}',
    b'{"error": {"errors": [{"reason": "unknownSecretToken"}]}}',
    b'{"error": {"errors": [{"reason": "rateLimitExceeded\\nsecret URL"}]}}',
    b'{"error": {"message": "rateLimitExceeded secret token"}}',
    b'[' * 2000 + b']' * 2000,
])
def test_http_reason_ignores_unknown_malformed_and_untrusted_content(content):
    api = module()
    class HttpError(Exception):
        resp = type('Response', (), {'status': 403})()
    error = HttpError('secret exception')
    error.content = content
    assert api._failure_diagnostic(error) == 'provider_http http_status=403'


def test_http_reason_bounds_content_before_json_parse(monkeypatch):
    api = module()
    class HttpError(Exception):
        resp = type('Response', (), {'status': 403})()
        content = b'{"error":{"errors":[{"reason":"rateLimitExceeded"}],"message":"' + b'x' * 16384 + b'"}}'
    def unexpected_parse(*_):
        pytest.fail('oversized provider content must not be parsed')
    monkeypatch.setattr(api.json, 'loads', unexpected_parse)
    assert api._failure_diagnostic(HttpError()) == 'provider_http http_status=403'
    error = HttpError()
    error.content = '\u2603' * 6000  # under the character limit, over the byte limit
    assert api._failure_diagnostic(error) == 'provider_http http_status=403'


def test_http_reason_bounds_error_entries_and_requires_http_error():
    api = module()
    class HttpError(Exception):
        content = json.dumps({'error': {'errors': [{}] * 16 + [{'reason': 'rateLimitExceeded'}]}})
    assert api._provider_reason(HttpError()) is None
    error = RuntimeError('private')
    error.content = b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}'
    assert api._provider_reason(error) is None


def test_attended_pulse_refresh_fits_its_child_deadline_with_paced_metadata_reads():
    """The pulse's fresh Gmail check must return a capped receipt inside its 12-second child deadline,
    not be killed into `unavailable`: provider pacing alone (zero network latency) plus a fixed reserve
    for interpreter start, credential refresh and the list call must fit."""
    gather = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff/_shared/scripts/gather_google.py'
    constants = {node.targets[0].id: node.value.value for node in ast.parse(gather.read_text()).body
                 if isinstance(node, ast.Assign) and len(node.targets) == 1
                 and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)}
    limit, deadline = constants['PULSE_REFRESH_MAX_MESSAGES'], constants['PULSE_REFRESH_TIMEOUT_SECONDS']
    api = module()
    factory, _, calls = _search_factory([f'm{i}' for i in range(limit)], lambda mid: {'id': mid})
    rows = api._gmail_search(factory('gmail', 'v1'), 'after:1 before:2', limit, factory)
    assert len(rows) == limit and sum(kind == 'get' for kind, _ in calls) == limit
    startup_reserve = 6.0   # interpreter + googleapiclient import, token refresh, list, client builds
    assert api.test_clock.now + startup_reserve <= deadline, api.test_clock.now


@pytest.mark.parametrize('mode', ['all', 'selected'])
def test_shared_calendar_inventory_selection_pages_partial_and_downstream_identity(tmp_path, monkeypatch, mode):
    api = module()
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.json').write_text(json.dumps({'google_calendar_selection':
        {'mode': mode, 'calendar_ids': [] if mode == 'all' else ['primary', 'shared@group', 'revoked']}}))
    calls = []
    class Inventory:
        def list(self, **kwargs):
            calls.append(('inventory', kwargs))
            return Call({'items': [{'id':'shared@group','accessRole':'reader'},
                {'id':'unselected','accessRole':'reader'}, {'id':'revoked','accessRole':'reader'},
                {'id':'limited-writer','accessRole':'writerWithoutPrivateAccess'},
                {'id':'busy-only','accessRole':'freeBusyReader'}]} if kwargs.get('pageToken') else
                {'items':[{'id':'primary','accessRole':'owner'}], 'nextPageToken':'inventory-next'})
    class Events:
        def list(self, **kwargs):
            calls.append(('events', kwargs))
            if kwargs['calendarId'] == 'revoked':
                raise RuntimeError('revoked fixture')
            return Call({'items':[{'id':'recurring-instance', 'recurringEventId':'series',
                'originalStartTime':{'dateTime':'2026-11-01T01:30:00-07:00','timeZone':'America/Los_Angeles'},
                'start':{'dateTime':'2026-11-01T01:30:00-07:00','timeZone':'America/Los_Angeles'},
                'end':{'dateTime':'2026-11-01T01:30:00-08:00','timeZone':'America/Los_Angeles'}}]} if kwargs.get('pageToken') else
                {'items':[{'id':'same', 'summary':'All day', 'start':{'date':'2026-11-01'}, 'end':{'date':'2026-11-02'}}], 'nextPageToken':'events-next'})
    class Service:
        def calendarList(self): return Inventory()  # noqa: N802 - Google SDK interface
        def events(self): return Events()
    rows = api._calendar_list(Service(), '2026-11-01T00:00:00-07:00', '2026-11-02T00:00:00-08:00')
    assert rows.complete is False
    assert {'calendar_id':'revoked','status':'unavailable'} in rows.calendars
    expected = {'primary', 'shared@group'} | ({'unselected', 'limited-writer'} if mode == 'all' else set())
    assert {row['calendar_id'] for row in rows} == expected
    assert len(rows) == 2 * len(expected)
    assert all(args['singleEvents'] is True for kind, args in calls if kind == 'events')
    assert all(args['calendarId'] != 'busy-only' for kind, args in calls if kind == 'events')
    if mode == 'selected':
        assert not any(kind == 'inventory' for kind, _ in calls)
        assert not any(args.get('calendarId') == 'unselected' for _, args in calls)
    # The same IDs survive both the brief normalizer and its calendar-cache identity.
    gather_path = Path(__file__).parents[2] / 'sotto-chief-of-staff/_shared/scripts/gather_google.py'
    spec = importlib.util.spec_from_file_location('multi_calendar_gather', gather_path)
    gather = importlib.util.module_from_spec(spec); spec.loader.exec_module(gather)
    normalized = [gather.normalize_event(row) for row in rows]
    assert len({row['id'] for row in normalized}) == len(rows)
    assert len([row for row in normalized if row['provider_event_id'] == 'same']) == len(expected)
    recurring = next(row for row in normalized if row['provider_event_id'] == 'recurring-instance')
    assert recurring['originalStartTime']['timeZone'] == 'America/Los_Angeles'
    assert recurring['start'] == '2026-11-01T01:30:00-07:00'


@pytest.mark.parametrize('selected', [None, [], ['shared@group']])
def test_calendar_discovery_refusal_preserves_existing_primary_reads_without_expanding_selection(tmp_path, monkeypatch, selected):
    api = module()
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    if selected is not None:
        (tmp_path / 'config').mkdir()
        (tmp_path / 'config/settings.json').write_text(json.dumps({'google_calendar_selection':
            {'mode': 'selected', 'calendar_ids': selected}}))
    calls = []
    class Inventory:
        def list(self, **kwargs):
            calls.append('inventory')
            # Existing event-only OAuth grants authorize events.list, not calendarList.list.
            raise PermissionError('insufficientPermissions')
    class Events:
        def list(self, **kwargs):
            calls.append(kwargs['calendarId'])
            return Call({'items': [{'id': 'existing-event', 'summary': 'Fixture meeting'}]})
    class Service:
        def calendarList(self): return Inventory()  # noqa: N802 - Google SDK interface
        def events(self): return Events()
    rows = api._calendar_list(Service(), '2026-11-01T00:00:00Z', '2026-11-02T00:00:00Z')
    if selected is None:
        assert calls == ['inventory', 'primary']
        assert [row['calendar_id'] for row in rows] == ['primary']
        assert rows.complete is False
        assert rows.calendars == [{'calendar_id': '*', 'status': 'unavailable'},
                                  {'calendar_id': 'primary', 'status': 'ok'}]
    else:
        assert calls == selected
        assert [row['calendar_id'] for row in rows] == selected
        assert rows.complete is True


def test_calendar_get_respects_explicit_read_selection_before_provider_io(tmp_path, monkeypatch):
    api = module()
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.json').write_text(json.dumps({'google_calendar_selection':
        {'mode': 'selected', 'calendar_ids': ['chosen']}}))
    calls = []
    monkeypatch.setattr(api, '_owner_fetch', lambda *a: calls.append(a) or {'id': 'abc', 'summary': 'Meeting'})
    with pytest.raises(ValueError, match='outside the current read selection'):
        api._read_owner_with_session('calendar_get', {'calendar_id': 'not-chosen', 'event_id': 'abc'}, object())
    assert calls == []


def test_calendar_chat_read_projects_existing_owner_participation_rule(tmp_path, monkeypatch):
    api = module()
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_USER_EMAIL', raising=False)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.json').write_text(json.dumps({'google_account_email': 'owner@example.com'}))
    def fetch(_session, url, arguments, _deadline):
        if url.endswith('/calendarList'):
            return {'items': [{'id': 'owner@example.com', 'accessRole': 'owner', 'primary': True},
                              {'id': 'team', 'accessRole': 'reader'}]}
        if url.endswith('/events'):
            assert 'organizer(email,self)' in arguments['fields']
            assert 'attendees(email,responseStatus,self,resource)' in arguments['fields']
            if '/team/' not in url:
                return {'items': [{'id': 'solo', 'summary': 'Private block'}]}
            return {'items': [
                {'id': 'other', 'summary': 'Colleague meeting', 'attendees': [
                    {'email': 'colleague@example.com', 'self': True, 'responseStatus': 'accepted'}]},
                {'id': 'mine', 'attendees': [{'email': 'owner@example.com', 'responseStatus': 'accepted'}]},
                {'id': 'declined', 'attendees': [{'email': 'owner@example.com', 'responseStatus': 'declined'}]},
                {'id': 'organized', 'organizer': {'email': 'owner@example.com'}},
            ]}
        raise AssertionError('Unexpected provider call')
    monkeypatch.setattr(api, '_owner_fetch', fetch)
    result = api._read_owner_with_session('calendar_list',
        {'start': '2026-11-01T00:00:00Z', 'end': '2026-11-02T00:00:00Z'}, object())
    assert result['observation']['complete'] is True
    assert {row['id']: row['owner_participates'] for row in result['items']} == {
        'solo': True, 'other': False, 'mine': True, 'declined': False, 'organized': True}
    assert next(row for row in result['items'] if row['id'] == 'other')['summary'] == 'Colleague meeting'


@pytest.mark.parametrize(('calendar_id', 'provider', 'participates'), [
    ('primary', {}, True),
    ('team', {'attendees': [{'email': 'colleague@example.com', 'self': True}]}, False),
    ('team', {'organizer': {'email': 'owner@example.com'}}, True),
    ('owner@example.com', {'attendees': [{'email': 'owner@example.com', 'responseStatus': 'declined'}]}, False),
])
def test_calendar_get_preserves_owner_scope(tmp_path, monkeypatch, calendar_id, provider, participates):
    api = module()
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    def fetch(_session, url, arguments, _deadline):
        if '/calendarList/' in url:
            assert arguments['fields'] == 'id,hidden,deleted,accessRole,primary'
            return {'id': calendar_id, 'accessRole': 'reader', 'primary': calendar_id == 'owner@example.com'}
        assert url.endswith('/events/abc')
        assert 'organizer(email,self)' in arguments['fields']
        return {'id': 'abc', **provider}
    monkeypatch.setattr(api, '_owner_fetch', fetch)
    result = api._read_owner_with_session('calendar_get', {'event_id': 'abc', 'calendar_id': calendar_id}, object())
    assert result['item']['owner_participates'] is participates
    assert result['item']['calendar_id'] == calendar_id


def test_stale_candidates_preserve_search_cohort_without_metadata_fanout():
    ids = [f'm{i}' for i in range(40)]
    def page(kwargs):
        offset = 20 if kwargs.get('pageToken') == 'next' else 0
        return {'messages': [{'id': mid, 'threadId': 't-' + mid} for mid in ids[offset:offset+20]],
                **({'nextPageToken': 'next'} if offset == 0 else {})}
    results, requests, pacing = {}, {}, {}
    for ids_only in (False, True):
        api = module()
        factory, _, calls = _search_factory(ids, lambda mid: {'id': mid, 'threadId': 't-' + mid}, list_page=page)
        results[ids_only] = api._gmail_search(factory('gmail', 'v1'), 'in:sent older_than:3d newer_than:14d -in:chats', 40,
                                            factory, ids_only=ids_only)
        requests[ids_only] = calls
        pacing[ids_only] = api.test_clock.now
    assert [(row['id'], row['threadId']) for row in results[False]] == [(row['id'], row['threadId']) for row in results[True]]
    assert [args for kind,args in requests[False] if kind=='list'] == [args for kind,args in requests[True] if kind=='list']
    assert sum(kind=='get' for kind,_ in requests[False]) == 40
    assert sum(kind=='get' for kind,_ in requests[True]) == 0
    assert all(set(row) == {'id','threadId'} for row in results[True])
    assert pacing[False] == 9.75 and pacing[True] == 0


def test_stale_candidate_missing_thread_id_retains_metadata_resolution():
    api=module()
    factory,_,calls=_search_factory(['m'], lambda mid:{'id':mid,'threadId':'resolved'},
                                   list_page=lambda _: {'messages':[{'id':'m'}]})
    result=api.execute(api.build_parser().parse_args(['gmail','search','in:sent','--max','1','--ids-only']), factory)
    assert result[0]['threadId']=='resolved'
    assert sum(kind=='get' for kind,_ in calls)==1


def test_calendar_chat_dedupes_exact_provider_copies_not_recurring_occurrences(tmp_path, monkeypatch):
    api = module()
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    timed = {'id': 'original', 'iCalUID': 'invitation', 'summary': 'Same invitation',
             'start': {'dateTime': '2026-11-01T10:00:00Z'}, 'end': {'dateTime': '2026-11-01T11:00:00Z'}}
    all_day = {'id': 'day-original', 'iCalUID': 'all-day', 'summary': 'Travel',
               'start': {'date': '2026-11-01'}, 'end': {'date': '2026-11-02'}}
    def fetch(_session, url, args, _deadline):
        if url.endswith('/calendarList'):
            return {'items': [{'id': 'primary', 'primary': True, 'accessRole': 'owner'},
                              {'id': 'team', 'accessRole': 'reader'}]}
        assert 'iCalUID' in args['fields']
        if '/primary/' in url:
            return {'items': [timed, all_day]}
        return {'items': [{**timed, 'id': 'copy'}, {**all_day, 'id': 'day-copy'},
                         {**timed, 'id': 'next-instance', 'start': {'dateTime': '2026-11-02T10:00:00Z'},
                          'end': {'dateTime': '2026-11-02T11:00:00Z'}},
                         {**timed, 'id': 'same-title', 'iCalUID': 'different-invite'},
                         {**timed, 'id': 'missing-uid', 'iCalUID': ''}]}
    monkeypatch.setattr(api, '_owner_fetch', fetch)
    result = api._read_owner_with_session('calendar_list',
        {'start': '2026-11-01T00:00:00Z', 'end': '2026-11-03T00:00:00Z'}, object())
    assert [row['id'] for row in result['items']] == ['day-original', 'original', 'missing-uid', 'same-title', 'next-instance']
    assert result['items'][1]['owner_participates'] is True
    assert result['observation']['complete'] is True


@pytest.mark.parametrize('discovery', ['primary', 'selected-hidden-primary', 'shared', 'unavailable'])
def test_selected_calendar_get_primary_alias_requires_provider_proof(tmp_path, monkeypatch, discovery):
    api = module()
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner-alias@example.com')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.json').write_text(json.dumps({'google_calendar_selection':
        {'mode': 'selected', 'calendar_ids': ['canonical@example.com']}}))
    calls = []
    def fetch(_session, url, args, _deadline):
        calls.append(url)
        if '/calendarList/' in url:
            if discovery == 'unavailable':
                raise RuntimeError('Google message read unavailable')
            return {'id': 'canonical@example.com', 'accessRole': 'reader',
                    'primary': discovery in ('primary', 'selected-hidden-primary'),
                    'hidden': discovery == 'selected-hidden-primary'}
        return {'id': 'abc', 'attendees': [{'email': 'canonical@example.com', 'self': True}]}
    monkeypatch.setattr(api, '_owner_fetch', fetch)
    result = api._read_owner_with_session('calendar_get',
        {'calendar_id': 'canonical@example.com', 'event_id': 'abc'}, object())
    assert result['item']['owner_participates'] is (discovery in ('primary', 'selected-hidden-primary'))
    assert result['item']['calendar_id'] == 'canonical@example.com'
    assert len(calls) == 2


def test_calendar_get_without_calendar_id_preserves_legacy_shape(tmp_path, monkeypatch):
    api = module()
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    def fetch(_session, url, args, _deadline):
        assert url.endswith('/calendars/primary/events/abc')
        return {'id': 'abc', 'summary': 'Solo primary block'}
    monkeypatch.setattr(api, '_owner_fetch', fetch)
    result = api._read_owner_with_session('calendar_get', {'event_id': 'abc'}, object())
    assert result['item']['owner_participates'] is True
    assert 'calendar_id' not in result['item']


@pytest.mark.parametrize('case,expected', [
    ('credentials', 'credential_setup'), ('transport', 'provider_transport'),
    ('http', 'provider_forbidden'), ('bounds', 'provider_bounds'),
    ('decode', 'provider_decode'), ('shape', 'provider_shape'),
    ('projection', 'projection'), ('input', 'input_validation'),
])
def test_owner_read_failure_retains_only_closed_stage_and_original_failure(case, expected):
    api = module()
    secret = 'PRIVATE-QUERY-MESSAGE-TOKEN'
    class Response:
        status_code = 403 if case == 'http' else 200
        headers = {'Content-Length': str(300 * 1024)} if case == 'bounds' else {}
        def iter_content(self, chunk_size):
            if case == 'decode':
                yield secret.encode()
            elif case == 'shape':
                yield b'[]'
            else:
                yield json.dumps({'messages': [{'id': 'synthetic'}]} if self.listing else {
                    'id': 'synthetic', 'threadId': 'thread', 'payload': {'headers': [
                        {'name': 'From', 'value': 'X' * 1025 if case == 'projection' else 'sender@example.test'}]}}).encode()
        def close(self): pass
    class Session:
        def get(self, url, **kwargs):
            if case == 'transport':
                raise TimeoutError(secret)
            result = Response(); result.listing = url.endswith('/messages'); return result
        def close(self): pass
    def factory():
        if case == 'credentials':
            raise RuntimeError(secret)
        return Session()
    with pytest.raises(Exception) as caught:
        api.read_owner('gmail_search', {'query': '\n' if case == 'input' else secret, 'max': 1}, session_factory=factory)
    assert caught.value.sotto_read_stage == expected
    assert secret not in caught.value.sotto_read_stage


@pytest.mark.parametrize('status,expected', [(400, 'provider_bad_request'),
    (401, 'provider_unauthorized'), (403, 'provider_forbidden'),
    (429, 'provider_rate_limited'), (500, 'provider_server'),
    (503, 'provider_server'), (404, 'provider_http_other'), (418, 'provider_http_other')])
def test_owner_provider_http_status_maps_to_closed_category_without_body(status, expected):
    api = module()
    class Response:
        status_code = status
        def iter_content(self, chunk_size):
            raise AssertionError('private provider error body must never be read')
        def close(self): pass
    class Session:
        def get(self, *args, **kwargs): return Response()
        def close(self): pass
    with pytest.raises(RuntimeError) as caught:
        api.read_owner('gmail_search', {'query': 'PRIVATE-QUERY', 'max': 1}, session_factory=Session)
    assert caught.value.sotto_read_stage == expected


@pytest.mark.parametrize('status', [200, 204])
def test_owner_empty_search_is_success_without_metadata_reads(status):
    api = module()
    closed, requests = [], []
    class Response:
        status_code = status
        headers = {}
        def iter_content(self, chunk_size):
            assert status == 200, '204 has no representation to decode'
            yield b'{}'
        def close(self): closed.append('response')
    class Session:
        def get(self, url, **kwargs):
            requests.append((url, kwargs['params']))
            return Response()
        def close(self): closed.append('session')
    result = api.read_owner('gmail_search', {'query': 'no-such-message', 'max': 3}, session_factory=Session)
    assert result['items'] == []
    assert result['observation']['status'] == 'ok'
    assert result['observation']['complete'] is True
    assert result['source'] == 'gmail'
    assert result['operation'] == 'gmail_search'
    assert len(requests) == 1 and requests[0][0].endswith('/messages')
    assert closed == ['response', 'session']


@pytest.mark.parametrize('phase', [None, 'message_get', 'search_metadata'])
def test_no_content_is_not_success_for_reads_requiring_a_representation(phase):
    api = module()
    closed = []
    class Response:
        status_code = 204
        def iter_content(self, chunk_size):
            raise AssertionError('No content is not a readable message')
        def close(self): closed.append(True)
    class Session:
        def get(self, *args, **kwargs): return Response()
    with pytest.raises(RuntimeError) as caught:
        api._owner_fetch(Session(), 'https://gmail.googleapis.com/gmail/v1/users/me/messages/abc',
                         {}, api.time.monotonic() + 10, phase=phase)
    assert caught.value.sotto_read_http_status == 204
    assert caught.value.sotto_read_stage == 'provider_http_other'
    assert closed == [True]


@pytest.mark.parametrize('operation,phase', [('gmail_search', 'search_list'), ('gmail_search', 'search_metadata'), ('gmail_get', 'message_get')])
def test_typed_google_error_retains_only_response_status_and_closed_subrequest(operation, phase):
    api = module()
    class Response:
        status_code = 404
        def iter_content(self, chunk_size):
            raise AssertionError('Provider failure body must not be read')
        def close(self): pass
    class Session:
        calls = 0
        def get(self, *args, **kwargs):
            self.calls += 1
            if phase == 'search_metadata' and self.calls == 1:
                class Listed:
                    status_code = 200
                    headers = {}
                    def iter_content(self, chunk_size):
                        yield b'{"messages":[{"id":"abc","threadId":"def"}]}'
                    def close(self): pass
                return Listed()
            return Response()
        def close(self): pass
    arguments = {'query': 'PRIVATE-QUERY', 'max': 1} if operation == 'gmail_search' else {'message_id': 'abc'}
    with pytest.raises(RuntimeError) as caught:
        api.read_owner(operation, arguments, session_factory=Session)
    assert caught.value.sotto_read_stage == 'provider_http_other'
    assert caught.value.sotto_read_http_status == 404
    assert caught.value.sotto_read_phase == phase
    assert 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('status', [True, '404 PRIVATE', 99, 600, 404.5, None])
def test_typed_google_never_attaches_invalid_response_status(status):
    api = module()
    class Response:
        status_code = status
        def close(self): pass
    class Session:
        def get(self, *args, **kwargs): return Response()
        def close(self): pass
    with pytest.raises(RuntimeError) as caught:
        api.read_owner('gmail_search', {'query': 'PRIVATE', 'max': 1}, session_factory=Session)
    assert not hasattr(caught.value, 'sotto_read_http_status')
    assert caught.value.sotto_read_phase == 'search_list'
# Entirely fictional thread fixture: a request, forwarding consent, then sent approval.
def _thread_message(index, *, thread='thread-1', body=None):
    return {'id': f'msg-{index}', 'threadId': thread, 'internalDate': str(1000 + index),
            'labelIds': ['SENT'] if index == 3 else ['INBOX'],
            'payload': {'headers': [{'name': 'From', 'value': 'owner@example.test' if index == 3 else 'counsel@example.test'},
                                    {'name': 'Date', 'value': f'2026-10-{index:02d}T12:00:00Z'}],
                        'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(
                            (body if body is not None else {1: 'Please approve the Cedar grant.', 2: 'May I forward this?', 3: 'Approved, please proceed.'}.get(index, 'Other message.')).encode()).decode()}}}


def _thread_session(raw, status=200):
    class Response:
        status_code = status
        headers = {}
        def iter_content(self, chunk_size):
            yield json.dumps(raw).encode()
        def close(self): self.closed = True
    class Session:
        def __init__(self): self.calls = []; self.closed = False
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs)); self.response = Response(); return self.response
        def close(self): self.closed = True
    return Session()


def test_owner_thread_read_one_provider_call_ordered_wording_and_original_identity():
    api = module()
    session = _thread_session({'id': 'thread-1', 'messages': [_thread_message(3), _thread_message(1), _thread_message(2)]})
    value = api.read_owner('gmail_thread_get', {'thread_id': 'thread-1'}, session_factory=lambda: session)
    assert [row['id'] for row in value['items']] == ['msg-1', 'msg-2', 'msg-3']
    assert value['items'][-1]['body'] == 'Approved, please proceed.'
    assert value['items'][-1]['from'] == 'owner@example.test'
    assert value['items'][-1]['date'] == '2026-10-03T12:00:00Z'
    assert value['observation']['complete'] is True and value['omitted_messages'] == 0
    assert len(session.calls) == 1 and session.calls[0][0].endswith('/threads/thread-1')
    assert session.calls[0][1]['params']['format'] == 'full'
    assert session.closed and session.response.closed


def test_owner_thread_newest_selection_body_bound_and_response_bound_are_honestly_partial():
    api = module()
    raw = {'id': 'thread-1', 'messages': [_thread_message(i) for i in range(25, 0, -1)]}
    value = api.read_owner('gmail_thread_get', {'thread_id': 'thread-1'}, session_factory=lambda: _thread_session(raw))
    assert [row['id'] for row in value['items']] == [f'msg-{i}' for i in range(6, 26)]
    assert value['omitted_messages'] == 5 and value['observation']['status'] == 'partial'
    raw['messages'] = [_thread_message(i, body='漢' * 6000) for i in range(1, 4)]
    value = api.read_owner('gmail_thread_get', {'thread_id': 'thread-1'}, session_factory=lambda: _thread_session(raw))
    assert all(row['body_truncated'] and len(row['body'].encode()) <= 16 * 1024 for row in value['items'])
    assert value['observation']['complete'] is False
    assert value['items'][-1]['id'] == 'msg-3'
    assert value['omitted_messages'] == 2  # Actual ASCII JSON budget drops oldest whole messages.
    assert len(json.dumps(value, ensure_ascii=False).encode()) <= 64 * 1024


@pytest.mark.parametrize('mutation', ['thread', 'message_thread', 'missing_id', 'duplicate', 'time', 'missing_time', 'empty', 'payload'])
def test_owner_thread_unknown_or_cross_identity_fails_closed(mutation):
    api = module()
    raw = {'id': 'thread-1', 'messages': [_thread_message(1), _thread_message(3)]}
    if mutation == 'thread':
        raw['id'] = 'other'
    elif mutation == 'message_thread':
        raw['messages'][0]['threadId'] = 'other'
    elif mutation == 'missing_id':
        del raw['messages'][0]['id']
    elif mutation == 'duplicate':
        raw['messages'][1]['id'] = raw['messages'][0]['id']
    elif mutation == 'time':
        raw['messages'][0]['internalDate'] = 'not-time'
    elif mutation == 'missing_time':
        del raw['messages'][0]['internalDate']
    elif mutation == 'empty':
        raw['messages'] = []
    else:
        del raw['messages'][0]['payload']
    session = _thread_session(raw)
    with pytest.raises((ValueError, RuntimeError)):
        api.read_owner('gmail_thread_get', {'thread_id': 'thread-1'}, session_factory=lambda: session)
    assert session.closed and session.response.closed


def test_owner_thread_invalid_input_error_and_wire_bound_have_no_fallback():
    api = module()
    for arguments, raw, status in [({'thread_id': '../bad'}, {}, 200),
                                  ({'thread_id': 'thread-1', 'extra': True}, {}, 200),
                                  ({'thread_id': 'thread-1'}, {}, 403),
                                  ({'thread_id': 'thread-1'}, {'private': 'x' * (256 * 1024)}, 200)]:
        session = _thread_session(raw, status)
        with pytest.raises((ValueError, RuntimeError)):
            api.read_owner('gmail_thread_get', arguments, session_factory=lambda: session)
        assert len(session.calls) <= 1 and session.closed



def test_owner_thread_escaping_cannot_publish_empty_partial_success():
    api = module()
    # Wire and decoded-body bounds fit, but JSON escapes exceed the item budget.
    session = _thread_session({'id': 'thread-1', 'messages': [
        _thread_message(3, body='\x01' * (16 * 1024))]})
    with pytest.raises(ValueError, match='no message within its response bound'):
        api.read_owner('gmail_thread_get', {'thread_id': 'thread-1'}, session_factory=lambda: session)
    assert len(session.calls) == 1 and session.closed and session.response.closed



def test_owner_thread_excludes_newest_draft_without_hiding_latest_sent_correspondence():
    api = module()
    draft = _thread_message(99, body='Draft only: approved everything.')
    draft['labelIds'] = ['DRAFT', 'SENT']  # Even an inconsistent SENT label cannot make a draft evidence.
    session = _thread_session({'id': 'thread-1', 'messages': [_thread_message(3), draft, _thread_message(1)]})
    value = api.read_owner('gmail_thread_get', {'thread_id': 'thread-1'}, session_factory=lambda: session)
    assert [item['id'] for item in value['items']] == ['msg-1', 'msg-3']
    assert value['omitted_messages'] == 0 and value['observation']['complete'] is True
    assert all('DRAFT' not in item['labels'] for item in value['items'])
    session = _thread_session({'id': 'thread-1', 'messages': [draft]})
    with pytest.raises(ValueError, match='no message within its response bound'):
        api.read_owner('gmail_thread_get', {'thread_id': 'thread-1'}, session_factory=lambda: session)
    assert session.closed and session.response.closed


@pytest.mark.parametrize('labels', ['DRAFT', None, [12], ['x' * 129], ['INBOX'] * 41])
def test_owner_thread_label_shape_is_validated_before_excluding_drafts(labels):
    api = module(); message = _thread_message(3); message['labelIds'] = labels
    with pytest.raises(ValueError, match='labels are invalid'):
        api.read_owner('gmail_thread_get', {'thread_id': 'thread-1'},
                       session_factory=lambda: _thread_session({'id': 'thread-1', 'messages': [message]}))
