"""Carried-over obligations get bounded evidence, never automatic completion."""
import base64
import datetime as dt
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_shared/scripts'))
import loop_context as lc
import gather_google as gg

NOW = dt.datetime(2026, 10, 2, 13, 30, tzinfo=dt.timezone.utc)


def loop(**kw):
    return dict({'anchor_key': 'follow_up:cid:person', 'status': 'open', 'channel': 'gmail',
                 'source_thread_id': 'original-thread', 'created_at': '2026-09-28'}, **kw)


def raw(mid='reply', body='Works. I will send the invitation later.', sent=True):
    return {'id': mid, 'threadId': 'original-thread', 'internalDate': '1790880000000',
            'labelIds': ['SENT'] if sent else ['INBOX'], 'payload': {
                'mimeType': 'text/plain', 'headers': [
                    {'name': 'From', 'value': 'owner@example.com' if sent else 'peer@example.com'},
                    {'name': 'To', 'value': 'peer@example.com' if sent else 'owner@example.com'},
                    {'name': 'Cc', 'value': 'other@example.com'},
                    {'name': 'Date', 'value': 'Thu, 1 Oct 2026 11:40:00 -0700'}],
                'body': {'data': base64.urlsafe_b64encode(body.encode()).decode()}}}


class Service:
    def __init__(self, rows=None, fail_at=None, hook=lambda: None):
        self.rows = rows if rows is not None else [raw()]
        self.calls = []
        self.fail_at = fail_at
        self.hook = hook
    def users(self): return self
    def threads(self): self.kind = 'thread'; return self
    def messages(self): self.kind = 'message'; return self
    def get(self, **kwargs): self.calls.append(('get', kwargs)); return self
    def list(self, **kwargs): self.calls.append(('list', kwargs)); return self
    def execute(self):
        self.hook()
        if len(self.calls) == self.fail_at:
            raise RuntimeError('private provider text should not enter receipts')
        if self.calls[-1][0] == 'list':
            return {'messages': [{'id': 'hit', 'threadId': 'original-thread'}]}
        return {'messages': self.rows}


def run(rows=None, service=None, **kw):
    return lc.refresh(rows or [loop()], {}, service or Service(), gg.normalize_email,
                      allowed=kw.pop('allowed', lambda _: True), now=NOW, aliases=lambda _: (), **kw)


def test_old_original_thread_refresh_keeps_both_directions_and_does_not_close():
    source = loop()
    result, receipt = run([source], Service([raw('sent'), raw('answer', sent=False)]))
    assert [r['isSent'] for r in result] == [True, False]
    assert result[0]['cc'] == 'other@example.com'
    assert result[0]['date'].startswith('Thu, 1 Oct')
    assert source['status'] == 'open'
    assert receipt['complete'] is False and receipt['messages'] == 2


def test_legacy_exact_gmail_thread_anchor_refreshes_without_name_or_relay_alias():
    current = loop(anchor_key='thread:original-thread', source_thread_id='',
                   contact_name='Alex Stone', contact_identifier='')
    relay = loop(anchor_key='follow_up:cid:relay', source_thread_id='',
                 channel='imessage', contact_name='Alex Stone',
                 contact_identifier='+15550001000')
    service = Service([raw('owner-reply'), raw('new-request', sent=False)])
    result, receipt = run([current, relay], service)
    assert [call for call in service.calls if call[0] == 'get'] == [
        ('get', {'userId': 'me', 'id': 'original-thread', 'format': 'full'})]
    assert [m['isSent'] for m in result] == [True, False]
    assert receipt['complete'] is False
    assert current['status'] == relay['status'] == 'open'


def test_legacy_thread_anchor_cannot_override_other_source_or_explicit_thread_id():
    other_source = loop(anchor_key='thread:original-thread', source_thread_id='',
                        channel='imessage', contact_identifier='')
    assert lc.targets([other_source], {}, now=NOW, allowed=lambda _: True,
                      graph_aliases=lambda _: ()) == []
    explicit = loop(anchor_key='thread:old-thread', source_thread_id='new-thread')
    assert lc.targets([explicit], {}, now=NOW, allowed=lambda _: True,
                      graph_aliases=lambda _: ()) == [('thread', 'new-thread')]


def test_no_name_based_lookup_and_no_reads_for_explicit_closed_or_snoozed():
    service = Service()
    rows = [loop(source_thread_id='', contact_name='Same Name'),
            loop(status='resolved'), loop(resolution_mode='explicit'),
            loop(snoozed_until='2026-10-03')]
    result, receipt = run(rows, service)
    assert result == [] and service.calls == [] and receipt['status'] == 'not_applicable'


def test_known_contact_search_is_bounded_and_binds_address_not_name():
    service = Service()
    rows = [loop(channel='imessage', source_thread_id='', contact_identifier='peer@example.com')]
    result, receipt = run(rows, service)
    assert result and receipt['requests'] == 2
    query = service.calls[0][1]
    assert query['maxResults'] == 2
    assert query['q'] == '{from:"peer@example.com" to:"peer@example.com" cc:"peer@example.com"} newer_than:30d -in:trash -in:spam'


def test_source_revoked_mid_read_discards_all_rows_and_stops_requests():
    grants = {'gmail': True}
    service = Service(hook=lambda: grants.update(gmail=False))
    result, receipt = run(service=service, allowed=lambda s: grants.get(s, True))
    assert result == [] and len(service.calls) == 1 and receipt['status'] == 'disabled'


def test_disabled_original_source_cannot_drive_cross_channel_search():
    service = Service()
    result, _ = run([loop(channel='imessage', source_thread_id='', contact_identifier='peer@example.com')],
                    service, allowed=lambda s: s != 'imessage')
    assert result == [] and service.calls == []


def test_failure_keeps_earlier_rows_and_stops_fanout():
    service = Service(fail_at=2)
    result, receipt = run([loop(anchor_key=str(i), source_thread_id=str(i)) for i in range(10)], service)
    assert len(result) == 1 and len(service.calls) == 2
    assert receipt['error'] == 'read_failed' and 'private' not in str(receipt)


def test_budget_caps_context_and_deduplicates_sources():
    rows = [loop(anchor_key=str(i), source_thread_id=str(i)) for i in range(40)]
    service = Service([raw(str(i), 'x' * 9000) for i in range(50)])
    result, receipt = run(rows, service)
    assert len(service.calls) <= lc.MAX_TARGETS
    assert len(result) <= lc.MAX_MESSAGES
    assert sum(len(r['body']) for r in result) <= lc.MAX_CONTEXT_CHARS
    assert all(r['_body_truncated'] for r in result)
    assert receipt['targets'] == 40 and not receipt['complete']


def test_time_budget_prevents_further_calls():
    ticks = iter([0, 25])
    service = Service()
    result, _ = run(service=service, clock=lambda: next(ticks))
    assert result == [] and not service.calls


def test_future_evidence_not_used():
    message = raw(); message['internalDate'] = str(int(NOW.timestamp() * 1000) + 1)
    result, _ = run(service=Service([message]))
    assert result == []


def test_merge_never_changes_inbound_direction_or_duplicates_id():
    before = {'id': 'inbound', 'body': '', 'isSent': False, 'labelIds': ['INBOX']}
    after = dict(before, body='Fresh evidence', _loop_context=True)
    result = lc.merge_context([before], [after, after])
    assert len(result) == 1 and result[0]['body'] == 'Fresh evidence'
    assert result[0]['isSent'] is False and before['body'] == ''


def test_group_targets_use_observed_roster_and_verified_aliases_only():
    rows = [loop(channel='imessage', source_thread_id='', group_id='any;+;opaque')]
    local = {'imessage': [{'chat_guid': 'any;+;opaque', 'group_participants': ['+15555550101']}]}
    aliases = lambda i: ['peer@example.com'] if i == '+15555550101' else []
    assert lc.targets(rows, local, now=NOW, allowed=lambda _: True, graph_aliases=aliases) == [('email', 'peer@example.com')]
    assert lc.targets(rows, {}, now=NOW, allowed=lambda _: True, graph_aliases=aliases) == []


def test_revoked_group_source_drops_derived_mail_even_with_gmail_enabled():
    grants = {'gmail': True, 'imessage': True}
    service = Service(hook=lambda: grants.update(imessage=False))
    rows = [loop(channel='imessage', source_thread_id='', contact_identifier='peer@example.com')]
    result, receipt = run(rows, service, allowed=lambda s: grants.get(s, True))
    assert result == [] and receipt['status'] == 'disabled'
    assert len(service.calls) == 1 and grants['gmail']


def test_richer_refresh_upgrades_short_body_only_from_a_gmail_only_read():
    prior = {'id': 'm', 'body': 'Opening sentence', 'isSent': False}
    longer = 'Opening sentence. Actual answer follows.'
    value = lc.merge_context([prior], [dict(prior, body=longer, _loop_context_sources=['gmail'])])[0]
    assert value['body'] == longer and value['isSent'] is False
    # Another source steered this read: its text stays in the gated excerpt, the inbox body is kept.
    value = lc.merge_context([prior], [dict(prior, body=longer,
                                             _loop_context_sources=['gmail', 'imessage'])])[0]
    assert value['body'] == prior['body'] and value['_loop_context_body'] == longer
    assert value['_loop_context_body_sources'] == ['gmail', 'imessage']
    assert '_loop_context_sources' not in value


def test_native_gather_merges_refreshed_evidence_and_partial_receipt(tmp_path, monkeypatch):
    import json
    import ledger_io
    service = Service([raw('inbound', body='A longer answer to the original request.', sent=False)])
    service.close = lambda: None
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, '_refresh_google_token', lambda: None)
    monkeypatch.setattr(gg, '_gmail_service', lambda: service)
    monkeypatch.setattr(gg, 'gather_gmail', lambda *a, **k: [
        {'id': 'inbound', 'body': 'Short', 'isSent': False, 'labelIds': ['INBOX']}])
    monkeypatch.setattr(ledger_io, 'load_active', lambda: [loop()])
    monkeypatch.setattr(lc, 'graph_aliases', lambda: lambda _: ())
    local, gmail, cal, receipt = [tmp_path / name for name in ('local.json', 'gmail.json', 'cal.json', 'sources.json')]
    local.write_text('{}')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    monkeypatch.setattr(sys, 'argv', ['gather_google.py', '--loop-local', str(local),
                                    '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--source-results-out', str(receipt),
                                    '--skip-calendar', '--skip-sent', '--skip-stale'])
    gg.main()
    result = json.loads(gmail.read_text())
    assert len(result) == 1 and result[0]['isSent'] is False
    assert result[0]['body'] == 'A longer answer to the original request.'
    context = json.loads(receipt.read_text())['gmail']['loop_context']
    assert context['messages'] == 1 and context['complete'] is False


@pytest.mark.parametrize('failure', ['partial', 'exception'])
def test_stale_quota_stops_loop_reads_preserving_inbox(tmp_path, monkeypatch, failure):
    import json
    import ledger_io
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/fake/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, '_refresh_google_token', lambda: None)
    monkeypatch.setattr(gg, 'gather_gmail', lambda *a, **k: [{'id': 'keep'}])
    def stale(*a, **k):
        if failure == 'exception':
            raise gg.GoogleCLIError('<HttpError 429: Too many requests>')
        result = gg.GmailRows()
        result.rate_limited = True
        return result
    monkeypatch.setattr(gg, 'gather_stale_sent', stale)
    monkeypatch.setattr(ledger_io, 'load_active', lambda: [loop()])
    calls = []
    monkeypatch.setattr(gg, '_gmail_service', lambda: calls.append('unwanted'))
    monkeypatch.setattr(gg, 'gather_calendar', lambda *a, **k: calls.append('calendar') or [])
    local, gmail, cal, receipt = [tmp_path / name for name in ('local', 'gmail', 'cal', 'receipt')]
    local.write_text('{}')
    monkeypatch.setattr(sys, 'argv', ['gather_google.py', '--loop-local', str(local), '--skip-sent',
                                    '--gmail-out', str(gmail), '--cal-out', str(cal),
                                    '--source-results-out', str(receipt)])
    gg.main()
    assert calls == ['calendar']
    assert json.loads(gmail.read_text()) == [{'id': 'keep'}]
    assert json.loads(receipt.read_text())['gmail']['error'] == 'rate_limited'


def test_group_addresses_rotate_without_expanding_target_count():
    rows = [loop(channel='imessage', source_thread_id='', group_id='any;+;opaque')]
    addresses = ['amy@example.com', 'lee@example.com', 'zoe@example.com']
    local = {'imessage': [{'chat_guid': 'any;+;opaque', 'group_participants': addresses}]}
    picked = []
    for halfday in range(6):
        args = dict(now=NOW + dt.timedelta(hours=12 * halfday), allowed=lambda _: True)
        choices = lc.targets(rows, local, **args)
        assert choices == lc.targets(rows, local, **args) and len(choices) == 1
        picked.append(choices[0][1])
    assert set(picked[:3]) == set(addresses) and picked[:3] == picked[3:]


def test_targeted_archived_reply_survives_rendering_without_reclassifying_it():
    from render_local import _trim_email, _format_emails
    prior = {'id': 'evidence-id', 'threadId': 'thread', 'from': 'peer@example.com',
             'cc': 'other@example.com', 'body': 'The requested answer is supplied.',
             'labelIds': ['IMPORTANT'], 'isSent': False}
    fresh = dict(prior, _loop_context=True, _loop_context_sources=['gmail'])
    merged = lc.merge_context([prior], [fresh])[0]
    trimmed = _trim_email(merged)
    rendered = _format_emails([trimmed])
    assert trimmed['isArchived'] and not trimmed['isSent']
    assert prior['body'] in rendered and prior['id'] in rendered
    assert 'Cc: other@example.com' in rendered and 'archive status is not completion' in rendered
    assert prior['body'] not in _format_emails([_trim_email(prior)])


def test_archived_evidence_does_not_expand_existing_long_body_or_attachments():
    from render_local import _trim_email, _format_emails
    prior = {'id': 'm', 'body': 'x' * 10000 + 'unselected-tail', 'labelIds': ['IMPORTANT'],
             'attachments': [{'filename': 'private.txt', 'text': 'not-selected-attachment'}]}
    fresh = dict(prior, body='x' * 2000, _loop_context=True, _loop_context_sources=['gmail'])
    rendered = _format_emails([_trim_email(lc.merge_context([prior], [fresh])[0])])
    assert rendered.count('x') == 2000
    assert 'unselected-tail' not in rendered and 'not-selected-attachment' not in rendered


def test_current_complete_thread_inventory_reuses_native_bodies_without_fetching():
    from gmail_read import message_fields
    message = raw()
    known = gg.normalize_email(message_fields(message), message_fields(message))
    known['_body_complete'] = True
    service = Service()
    result, receipt = run(service=service, known_messages=[known],
                          known_threads={'original-thread': {'messages': [message]}})
    assert service.calls == [] and receipt['requests'] == 0 and receipt['reused_threads'] == 1
    assert result[0]['body'] == known['body'] and result[0]['isSent']


def test_only_native_body_reads_qualify_for_inventory_reuse(monkeypatch):
    from gmail_read import message_fields
    native = message_fields(raw('native'))
    monkeypatch.setattr(gg, '_run', lambda *a, **k: [
        {'id': mid, 'snippet': 'Search preview'} for mid in ('native', 'flat', 'failed')])
    fetched = {'native': native, 'flat': {'body': 'CLI body'}, 'failed': None}
    monkeypatch.setattr(gg, '_fetch_body', lambda api, mid, stop: (mid, fetched[mid]))
    rows = gg._search_gmail('/fake/api.py', 'query', 3, 3)
    assert rows[0]['_body_complete'] is True and rows[0]['body'] == native['body']
    assert all('_body_complete' not in row for row in rows[1:])


@pytest.mark.parametrize('missing', ['inventory', 'body', 'member'])
def test_partial_cached_evidence_cannot_substitute_for_a_whole_thread(missing):
    message = raw()
    known = {'id': 'reply', 'body': 'only a snippet', '_body_complete': missing != 'body'}
    inventory = {'original-thread': {'messages': [message]}}
    if missing == 'inventory':
        inventory = {}
    if missing == 'member':
        inventory['original-thread']['messages'].append(raw('another'))
    service = Service()
    result, receipt = run(service=service, known_messages=[known], known_threads=inventory)
    assert result and len(service.calls) == 1 and receipt['reused_threads'] == 0


@pytest.mark.parametrize('jid', ['15551234567@s.whatsapp.net', '1203630@g.us', '15551234567@c.us'])
def test_messaging_jids_are_never_gmail_targets(jid):
    rows = [loop(channel='whatsapp', source_thread_id='', contact_identifier=jid)]
    assert lc.targets(rows, {}, now=NOW, allowed=lambda _: True) == []
    group = [loop(channel='whatsapp', source_thread_id='', group_id='1203@g.us')]
    local = {'whatsapp': [{'chat_guid': '1203@g.us', 'group_participants': [jid, 'peer@example.com']}]}
    assert lc.targets(group, local, now=NOW, allowed=lambda _: True) == [('email', 'peer@example.com')]


@pytest.mark.parametrize('row, source', [
    (dict(channel='calendar'), 'calendar'), (dict(channel='calls'), 'calls'),
    (dict(channel='phone'), 'calls'), (dict(channel='sms'), 'imessage'),
    (dict(channel='whatsapp_call'), 'whatsapp_calls'),
    (dict(channel='email', source='granola', source_thread_id='t1'), 'granola')])
def test_revoked_origin_source_never_drives_gmail_and_granted_one_is_recorded(row, source):
    row = loop(**dict(dict(source_thread_id='', contact_identifier='peer@example.com'), **row))
    service = Service()
    result, receipt = run([row], service, allowed=lambda s: s != source)
    assert result == [] and service.calls == [] and receipt['status'] == 'not_applicable'
    result, receipt = run([row], Service())
    assert source in receipt['sources'] and all(source in r['_loop_context_sources'] for r in result)


def test_drafts_trash_and_spam_are_never_loop_evidence():
    rows = [raw('draft', 'Unsent deck draft'), raw('trash', 'deleted', sent=False),
            raw('spam', 'junk', sent=False), raw('reply', 'Real answer', sent=False)]
    for row, label in zip(rows, ('DRAFT', 'TRASH', 'SPAM')):
        row['labelIds'] = [label]
    result, _ = run(service=Service(rows))
    assert [r['id'] for r in result] == ['reply']


def test_only_quota_is_rate_limited_when_the_gmail_classifier_judges_a_403():
    class ForbiddenError(Exception):
        resp = type('resp', (), {'status': 403})
    def failing(reason):
        class Failing(Service):
            def execute(self):
                raise ForbiddenError(reason)
        return Failing()
    _, receipt = run(service=failing('insufficientPermissions'), classify_error=gg._source_error)
    assert receipt['error'] == 'read_failed'
    _, receipt = run(service=failing('userRateLimitExceeded'), classify_error=gg._source_error)
    assert receipt['error'] == 'rate_limited'
    _, receipt = run(service=failing('insufficientPermissions'))  # default never guesses quota
    assert receipt['error'] == 'read_failed'


def test_snooze_and_due_day_use_the_configured_local_day(monkeypatch):
    monkeypatch.setenv('SOTTO_TIMEZONE', 'Pacific/Auckland')
    evening_utc = dt.datetime(2026, 10, 2, 17, 30, tzinfo=dt.timezone.utc)  # Oct 3, 06:30 local
    rows = [loop(snoozed_until='2026-10-03')]
    assert lc.targets(rows, {}, now=evening_utc, allowed=lambda _: True) == [('thread', 'original-thread')]
    rows = [loop(anchor_key=str(i), source_thread_id=str(i)) for i in range(8)]
    rows.append(loop(anchor_key='due', source_thread_id='due', deadline='2026-10-03'))
    assert lc.targets(rows, {}, now=evening_utc, allowed=lambda _: True)[0] == ('thread', 'due')


def test_unrelated_revocation_keeps_inbox_mail_and_drops_only_a_steered_excerpt(monkeypatch):
    import source_context as sc
    inbox = [{'id': 'm1', 'threadId': 'original-thread', 'body': 'today inbox mail'},
             {'id': 'm2', 'threadId': 'peer-thread', 'body': 'other inbox mail'}]
    rows = [loop(), loop(anchor_key='wa', channel='whatsapp', source_thread_id='',
                         contact_identifier='+15550000000')]
    loop_rows, receipt = run(rows, Service([raw('m1', sent=False)]))
    assert receipt['sources'] == ['gmail']  # the WhatsApp row chose no target
    merged = lc.merge_context(inbox, loop_rows)
    monkeypatch.setattr(sc, 'allowed', lambda s: s != 'whatsapp')
    assert [r['id'] for r in sc.project_emails(merged)] == ['m1', 'm2']
    # An inbox row whose excerpt a now-revoked source steered keeps everything but that excerpt.
    steered = dict(raw('m2', sent=False), threadId='peer-thread')
    wa_rows, _ = run([loop(anchor_key='wa', channel='whatsapp', source_thread_id='',
                           contact_identifier='peer@example.com')], Service([steered]))
    assert wa_rows[0]['_loop_context_sources'] == ['gmail', 'whatsapp']
    kept = sc.project_emails(lc.merge_context(inbox, wa_rows))
    assert [r['id'] for r in kept] == ['m1', 'm2'] and kept[1]['body'] == 'other inbox mail'
    assert not any(key.startswith('_loop_context') for key in kept[1])


@pytest.mark.parametrize('sent', [True, False])
def test_supplemental_body_reaches_writer_and_respects_revocation(monkeypatch, sent):
    import render_local
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda _: True)
    original = {'id': 'sent-1', 'threadId': 'thread-1', 'isSent': sent,
                'labelIds': ['SENT'] if sent else ['INBOX'], 'from': 'owner@example.test',
                'to': 'dana@example.test', 'body': 'Hello Dana.'}
    full = 'Hello Dana. The deck is attached as promised.'
    refreshed = {**original, 'body': full, '_loop_context_sources': ['gmail', 'imessage'],
                 '_loop_context': True}
    merged = lc.merge_context([original], [refreshed])
    assert merged[0]['body'] == 'Hello Dana.'  # ordinary Gmail evidence stays independent
    assert source_context.email_evidence_text(merged[0]) == full
    rendered = render_local._format_emails([render_local._trim_email(row) for row in merged])
    assert 'The deck is attached as promised.' in rendered
    monkeypatch.setattr(source_context, 'allowed', lambda s: s != 'imessage')
    assert source_context.email_evidence_text(merged[0]) == 'Hello Dana.'
    rendered = render_local._format_emails([render_local._trim_email(row) for row in merged])
    assert 'The deck is attached' not in rendered
