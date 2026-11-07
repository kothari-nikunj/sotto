"""Ordinary brief gathering supplies bounded old meeting context without new action authority."""
import base64
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace

import pytest

import compose_brief as cb
import gather_google as gg
import source_context as sc
from render_local import _format_meeting_context


@pytest.fixture
def gathered(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    import textutil
    monkeypatch.setattr(textutil, 'FREEMAIL_DOMAINS', textutil.FREEMAIL_DOMAINS | {'freemail.test'})
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@inside.test')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'self-host')
    grants = {'gmail', 'calendar'}
    monkeypatch.setattr(sc, 'allowed', lambda source: source in grants)
    monkeypatch.setattr(gg, '_find_google_api', lambda: '/synthetic/api.py')
    monkeypatch.setattr(gg, '_ensure_google_deps', lambda: True)
    monkeypatch.setattr(gg, '_refresh_google_token', lambda: None)
    monkeypatch.setattr(gg, '_token_path', lambda: None)
    import ledger_io
    monkeypatch.setattr(ledger_io, 'load_active', lambda: [])

    def event(key, email, **extra):
        return {'id': key, 'summary': key, 'start': (now + timedelta(hours=54)).isoformat(),
                'end': (now + timedelta(hours=55)).isoformat(),
                'attendees': [{'email': 'owner@inside.test', 'self': True, 'responseStatus': 'accepted'},
                              {'email': email, 'displayName': 'Dana Rivers'}], **extra}
    events = [event('substantive-review', 'dana@outside.test'),
              event('personal', 'friend@freemail.test'),
              event('cancelled', 'cancelled@outside.test', status='cancelled'),
              event('shared', 'stranger@outside.test', calendar_id='team@outside.test',
                    attendees=[{'email': 'stranger@outside.test'}])]

    def message(ident, text, days=7):
        return {'id': ident, 'threadId': 'old-thread', 'labelIds': ['SENT'],
                'internalDate': str(int((now - timedelta(days=days)).timestamp() * 1000)),
                'payload': {'mimeType': 'text/plain', 'headers': [
                    {'name': 'From', 'value': 'Owner <owner@inside.test>'},
                    {'name': 'To', 'value': 'Dana Rivers <dana@outside.test>'},
                    {'name': 'Subject', 'value': 'Decision review'}],
                    'body': {'data': base64.urlsafe_b64encode(text.encode()).decode()}}}
    thread = {'id': 'old-thread', 'messages': [
        message('original-promise', 'Compare the three migration plans before our decision review.'),
        message('old-latest', 'The synthetic deck needs time for a comparison.', 6)]}
    searches, reads, timeouts = [], [], []
    state = {'failure': None, 'revoke': None}

    def run(api, args, timeout=60):
        if args[:2] == ['calendar', 'list']:
            return events
        assert args[:2] == ['gmail', 'search']
        searches.append((args[2], timeout))
        if 'newer_than:30d' not in args[2]:
            return [{'id': 'recent', 'from': 'Other <other@else.test>', 'snippet': 'Current ordinary inbox',
                     'labels': ['INBOX']}]
        assert 'dana@outside.test' in args[2]
        assert not any(word in args[2] for word in ('friend@freemail.test', 'cancelled@', 'stranger@'))
        if state['failure'] == 'search':
            raise TimeoutError('synthetic provider text must not be retained')
        if state['revoke'] == 'search':
            grants.remove('calendar')
        return [{'id': 'old-latest', 'threadId': 'old-thread', 'from': 'Owner <owner@inside.test>',
                 'to': 'dana@outside.test', 'subject': 'Decision review', 'date': 'six days ago',
                 'snippet': 'The synthetic deck needs time for a comparison.', 'labels': ['SENT']},
                {'id': 'unrelated', 'threadId': 'unrelated-thread', 'from': 'other@else.test',
                 'to': 'someone@else.test', 'snippet': 'UNRELATED_BACKGROUND_SENTINEL'}]
    monkeypatch.setattr(gg, '_run', run)

    class Service:
        def users(self): return self
        def threads(self): return self
        def get(self, **kwargs):
            reads.append(kwargs)
            return self
        def execute(self):
            if state['revoke'] == 'thread':
                grants.remove('calendar')
            if state['failure'] == 'thread':
                raise TimeoutError('synthetic provider text must not be retained')
            return thread
        def close(self): pass
    monkeypatch.setattr(gg, '_gmail_service', lambda **kwargs: (timeouts.append(kwargs.get('timeout')) or Service()))
    local, gmail, calendar, receipt = [tmp_path / name for name in ('local.json', 'gmail.json', 'cal.json', 'sources.json')]
    local.write_text('{}')
    monkeypatch.setattr('sys.argv', ['gather_google.py', '--loop-local', str(local), '--gmail-out', str(gmail),
        '--cal-out', str(calendar), '--source-results-out', str(receipt), '--skip-sent', '--skip-stale', '--bodies', '0'])

    def invoke():
        gg.main()
        return json.loads(gmail.read_text()), json.loads(calendar.read_text()), json.loads(receipt.read_text())
    return SimpleNamespace(invoke=invoke, grants=grants, state=state, searches=searches,
                           reads=reads, timeouts=timeouts, events=events, thread=thread)


def test_real_daily_gather_adds_old_exact_thread_as_background_to_writer_and_critic(gathered):
    payload, events, receipts = gathered.invoke()
    context = payload['meeting_context']
    assert len(gathered.searches) == 2  # the ordinary query plus one aggregate query
    assert len(gathered.reads) == 1 and 0 < gathered.timeouts[0] <= 15
    assert context['sources'] == ['calendar', 'gmail']
    assert context['receipt']['requests'] == 2 and context['receipt']['complete'] is False
    assert receipts['gmail']['meeting_context'] == context['receipt']
    assert [row['id'] for row in payload['emails']] == ['recent']  # native IDs/evidence stay untouched
    google = {**payload, 'events': events, 'userEmail': 'owner@inside.test'}
    text = _format_meeting_context(google)
    assert 'Compare the three migration plans' in text and 'original-promise' in text
    assert 'owner sent' in text and 'not current-message evidence' in text
    assert 'UNRELATED_BACKGROUND_SENTINEL' not in text
    prompt = cb.build_prompt('{{calendar}}', {'google': google, 'local': {}})
    assert 'Compare the three migration plans' in prompt
    assert 'Compare the three migration plans' in cb.build_data_manifest({'google': google, 'local': {}})['meeting_conversation_context']
    assert sc.email_dependency_sources(payload) == ['calendar', 'gmail']
    assert sc.used_sources(gmail=payload, already_projected=True) == ['calendar', 'gmail']
    proof = {'sourceType': 'gmail', 'sourceId': 'original-promise', 'snippet': 'Compare the three migration plans'}
    sources = cb._review_message_sources({'google': google})
    assert cb._review_source_evidence(proof, sources, 500) is None  # unavailable, not approval
    native = cb._review_source_evidence({'sourceType': 'gmail', 'sourceId': 'recent',
        'snippet': 'Current ordinary inbox'}, sources, 500)
    assert native is not None and native[0]['quote_matches_source'] is True
    assert native[1] == 'Current ordinary inbox'


@pytest.mark.parametrize('when', ['before', 'search', 'thread', 'projection'])
def test_calendar_revocation_removes_background_without_erasing_recent_inbox(gathered, when):
    if when == 'before':
        gathered.grants.remove('calendar')
    elif when != 'projection':
        gathered.state['revoke'] = when
    payload, events, receipts = gathered.invoke()
    if when in ('search', 'thread'):
        assert receipts['gmail']['meeting_context']['status'] == 'disabled'
        assert receipts['gmail']['meeting_context']['messages'] == 0
    if when == 'projection':
        gathered.grants.remove('calendar')
        payload = sc.project_emails(payload)
    rows = payload if isinstance(payload, list) else payload['emails']
    assert [row['id'] for row in rows] == ['recent']
    assert not isinstance(payload, dict) or 'meeting_context' not in payload
    if when == 'before':
        assert len(gathered.searches) == 1 and not gathered.reads
    assert not _format_meeting_context({**(payload if isinstance(payload, dict) else {'emails': payload}), 'events': events})


@pytest.mark.parametrize('failure', ['search', 'thread'])
def test_optional_failure_keeps_daily_inbox_and_discloses_partial_background(gathered, failure):
    gathered.state['failure'] = failure
    payload, _, receipts = gathered.invoke()
    rows = payload if isinstance(payload, list) else payload['emails']
    assert [row['id'] for row in rows] == ['recent']
    receipt = receipts['gmail']['meeting_context']
    assert receipt['complete'] is False
    assert 'synthetic provider text' not in json.dumps(receipts)
    if failure == 'search':
        assert receipt['status'] == 'unavailable' and not gathered.reads
    else:
        assert receipt['status'] == 'partial' and receipt['thread_reads_failed'] == 1
        assert 'unavailable' in payload['meeting_context']['messages'][0]['thread_context']


def test_old_background_is_not_rendered_after_occurrence_or_attendee_changes(gathered):
    payload, events, _ = gathered.invoke()
    google = {**payload, 'events': events}
    assert 'Compare the three' in _format_meeting_context(google)
    events[0]['start'] = (datetime.now(timezone.utc) + timedelta(hours=55)).isoformat()
    assert not _format_meeting_context(google)


def test_one_deadline_stops_all_optional_thread_reads(gathered, monkeypatch):
    clock = [0]
    monkeypatch.setattr(gg, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    original = gg._run
    def run(api, args, **kwargs):
        value = original(api, args, **kwargs)
        if 'newer_than:30d' in args[2]:
            clock[0] = 16
        return value
    monkeypatch.setattr(gg, '_run', run)
    payload, _, receipts = gathered.invoke()
    assert not gathered.reads
    assert receipts['gmail']['meeting_context']['budget_exhausted'] is True
    assert 'unavailable' in payload['meeting_context']['messages'][0]['thread_context']


def test_recent_exact_thread_background_is_reused_without_another_thread_read(gathered, monkeypatch):
    monkeypatch.setattr(gg, 'gather_gmail', lambda *args, **kwargs: [
        {'id': 'recent', 'threadId': 'old-thread', 'from': 'Other <other@else.test>',
         'body': 'Current ordinary inbox', 'thread_context': 'Exact dated previously gathered context'}])
    payload, events, receipts = gathered.invoke()
    assert not gathered.reads
    assert receipts['gmail']['meeting_context']['reused_threads'] == 1
    assert receipts['gmail']['meeting_context']['requests'] == 1
    assert 'Exact dated previously gathered context' in _format_meeting_context({**payload, 'events': events})


def test_aggregate_query_and_content_budget_do_not_multiply_by_attendee_count(gathered):
    for index in range(30):
        gathered.events[0]['attendees'].append({'email': f'person{index}@outside.test'})
    payload, _, receipts = gathered.invoke()
    assert len(gathered.searches) == 2 and len(gathered.reads) == 1
    receipt = receipts['gmail']['meeting_context']
    assert receipt['sampled_attendees'] == gg.ATTENDEE_COMMS_CAP
    assert receipt['omitted_attendees'] == 16
    assert receipt['requests'] <= 1 + gg.ATTENDEE_THREAD_CAP
    assert len(json.dumps(payload['meeting_context'], ensure_ascii=False)) <= gg.ATTENDEE_THREADS_TOTAL_CHARS


def test_empty_inbox_still_records_both_context_dependencies(gathered):
    payload, _, _ = gathered.invoke()
    payload['emails'] = []
    assert sc.used_sources(gmail=payload, already_projected=True) == ['calendar', 'gmail']
    assert sc.email_dependency_sources(payload) == ['calendar', 'gmail']
    gathered.grants.remove('gmail')
    assert sc.project_emails(payload) == []


def test_file_composer_retains_background_and_both_delivery_permissions(gathered, monkeypatch, tmp_path, capsys):
    gathered.invoke()
    seen = []
    def compose(inputs, **kwargs):
        seen.append(inputs)
        return {'brief_markdown': 'Synthetic ordinary brief', 'brief_text': 'Synthetic ordinary brief', 'actions': []}
    monkeypatch.setattr(cb, 'compose', compose)
    monkeypatch.setattr(cb, '_archive_brief', lambda *args: None)
    monkeypatch.setattr('sys.argv', ['compose_brief.py', '--type', 'morning', '--local', str(tmp_path/'local.json'),
        '--local-resolved', '--gmail', str(tmp_path/'gmail.json'), '--calendar', str(tmp_path/'cal.json'),
        '--source-results', str(tmp_path/'sources.json')])
    capsys.readouterr()
    cb.main()
    output = json.loads(capsys.readouterr().out)
    assert seen[0]['google']['meeting_context']['messages'][0]['message_id'] == 'old-latest'
    assert {'calendar', 'gmail'} <= set(output['_source_permissions'])


@pytest.mark.parametrize('identifier', [None, '', '   '])
def test_missing_calendar_identity_cannot_choose_context_or_create_an_occurrence(gathered, identifier):
    gathered.events[0]['id'] = identifier
    payload, _, receipts = gathered.invoke()
    assert len(gathered.searches) == 1 and not gathered.reads
    assert isinstance(payload, list) or 'meeting_context' not in payload
    assert receipts['gmail']['meeting_context']['status'] == 'not_applicable'


def test_removed_attendee_and_cancelled_occurrence_cannot_render_retained_context(gathered):
    payload, events, _ = gathered.invoke()
    google = {**payload, 'events': events}
    events[0]['attendees'] = [{'email': 'owner@inside.test', 'self': True}]
    assert 'Compare the three' not in _format_meeting_context(google)
    events[0]['status'] = 'cancelled'
    assert not _format_meeting_context(google)


def test_optional_cohort_exception_never_prevents_daily_source_files(gathered, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError('synthetic private source must not appear in receipt')
    monkeypatch.setattr(gg, 'gather_brief_meeting_context', broken)
    payload, events, receipts = gathered.invoke()
    assert events and payload[0]['id'] == 'recent'
    assert receipts['gmail']['meeting_context'] == {
        'status': 'unavailable', 'complete': False, 'error': 'read_failed'}


@pytest.mark.parametrize('response', ['needsAction', 'tentative', 'declined', ''])
def test_unaccepted_external_invitation_cannot_select_private_context(gathered, response):
    gathered.events[0]['attendees'][0]['responseStatus'] = response
    payload, _, _ = gathered.invoke()
    assert len(gathered.searches) == 1 and not gathered.reads
    assert isinstance(payload, list) or 'meeting_context' not in payload


def test_native_owner_organizer_can_select_context_without_attendee_response(gathered):
    gathered.events[0]['attendees'][0].pop('responseStatus')
    gathered.events[0]['organizer'] = {'self': True, 'email': 'owner@inside.test'}
    payload, events, _ = gathered.invoke()
    assert payload['meeting_context']['messages']
    assert 'Compare the three' in _format_meeting_context({**payload, 'events': events})


@pytest.mark.parametrize('key,value', [('mute_people', 'Dana Rivers'), ('mute_senders', '@outside.test')])
@pytest.mark.parametrize('when', ['before', 'compose'])
def test_existing_person_and_sender_mutes_hide_owner_authored_thread_context(gathered, monkeypatch, key, value, when):
    import preferences
    state = {'mute_people': [], 'mute_senders': []}
    monkeypatch.setattr(preferences, 'load_explicit', lambda: state)
    if when == 'before':
        state[key] = [value]
    payload, events, _ = gathered.invoke()
    if when == 'compose':
        state[key] = [value]
    google = {**(payload if isinstance(payload, dict) else {'emails': payload}), 'events': events}
    assert 'Compare the three' not in _format_meeting_context(google)
    if when == 'before':
        assert len(gathered.searches) == 1 and not gathered.reads


def test_muted_native_header_name_is_removed_before_thread_rendering(gathered, monkeypatch):
    import preferences
    monkeypatch.setattr(preferences, 'load_explicit', lambda: {'mute_people': ['Muted Person'], 'mute_senders': []})
    message = gathered.thread['messages'][0]
    message['labelIds'] = ['INBOX']
    message['payload']['headers'][0]['value'] = 'Muted Person <muted@else.test>'
    payload, events, _ = gathered.invoke()
    text = _format_meeting_context({**payload, 'events': events})
    assert 'Compare the three migration plans' not in text
    assert 'Muted Person' not in text
    assert 'The synthetic deck needs time' in text


def test_compose_uses_existing_contact_name_lookup_for_person_mute(gathered, monkeypatch):
    import preferences
    monkeypatch.setattr(preferences, 'load_explicit', lambda: {'mute_people': ['Dana Rivers'], 'mute_senders': []})
    gathered.events[0]['attendees'][1]['displayName'] = 'Dana'
    payload, events, _ = gathered.invoke()
    google = {**payload, 'events': events}
    assert 'Compare the three' not in _format_meeting_context(google, contact_lookup={'dana@outside.test': 'Dana Rivers'})


@pytest.mark.parametrize('case', ['same_event', 'two_events', 'changed_calendar_name'])
def test_newly_resolved_person_mute_discards_mixed_attendee_background(gathered, monkeypatch, case):
    import preferences
    monkeypatch.setattr(preferences, 'load_explicit', lambda: {'mute_people': ['Dana Rivers'], 'mute_senders': []})
    gathered.events[0]['attendees'][1]['displayName'] = 'Dana'
    if case == 'two_events':
        import copy
        other_event = copy.deepcopy(gathered.events[0])
        other_event['id'] = 'other-review'
        other_event['attendees'][1] = {'email': 'other@outside.test', 'displayName': 'Other Person'}
        gathered.events.append(other_event)
    else:
        gathered.events[0]['attendees'].append({'email': 'other@outside.test', 'displayName': 'Other Person'})
    original = gg._run
    def run(api, args, **kwargs):
        rows = original(api, args, **kwargs)
        if 'newer_than:30d' in args[2]:
            rows[0].update({'from': 'Dana <dana@outside.test>', 'cc': 'other@outside.test', 'labels': ['INBOX']})
        return rows
    monkeypatch.setattr(gg, '_run', run)
    for message in gathered.thread['messages']:
        message['labelIds'] = ['INBOX']
        message['payload']['headers'][0]['value'] = 'Dana <dana@outside.test>'
        message['payload']['headers'][1]['value'] = 'Other Person <other@outside.test>'
    payload, events, _ = gathered.invoke()
    assert payload['meeting_context']['messages'][0]['attendee_identifiers'] == ['dana@outside.test', 'other@outside.test']
    google = {**payload, 'events': events}
    assert 'Compare the three' in _format_meeting_context(google)  # gather's names could not match the mute
    if case == 'changed_calendar_name':
        events[0]['attendees'][1]['displayName'] = 'Dana Rivers'
        # A stale unmuted contact name must not mask the current native calendar name.
        assert _format_meeting_context(google, contact_lookup={'dana@outside.test': 'Dana'}) == ''
    else:
        assert _format_meeting_context(google, contact_lookup={'dana@outside.test': 'Dana Rivers'}) == ''


def test_omitted_event_receipt_counts_targets_outside_the_attendee_sample(gathered):
    import copy
    original = gathered.events[0]
    gathered.events[:] = [copy.deepcopy(original) for _ in range(16)]
    for index, event in enumerate(gathered.events):
        event['id'] = f'event-{index}'
        if index:
            event['attendees'][1]['email'] = f'person{index}@outside.test'
    _, _, receipts = gathered.invoke()
    receipt = receipts['gmail']['meeting_context']
    assert receipt['omitted_attendees'] == 1 and receipt['omitted_events'] == 1


def test_render_budget_keeps_complete_rows_and_an_explicit_omission_note(gathered):
    import copy
    payload, events, _ = gathered.invoke()
    row = payload['meeting_context']['messages'][0]
    row['thread_context'] = 'SYNTHETIC_LONG_BACKGROUND' * 300
    payload['meeting_context']['messages'] = [copy.deepcopy(row) for _ in range(10)]
    text = _format_meeting_context({**payload, 'events': events})
    assert len(text) <= 24000 and text.endswith('Additional background rows omitted to fit the context limit.')
    for line in text.splitlines():
        if line.startswith('{'):
            json.loads(line)


@pytest.mark.parametrize('email', ['"quoted"@outside.test', 'a{b}@outside.test', 'a:b@outside.test', r'a\b@outside.test'])
def test_query_operator_addresses_do_not_enter_aggregate_search(gathered, email):
    gathered.events[0]['attendees'][1]['email'] = email
    payload, _, _ = gathered.invoke()
    assert len(gathered.searches) == 1 and not gathered.reads
    assert isinstance(payload, list) or 'meeting_context' not in payload


def test_expired_deadline_still_reuses_later_exact_cached_context(gathered, monkeypatch):
    monkeypatch.setattr(gg, 'time', SimpleNamespace(monotonic=lambda: 20))
    rows = [{'message_id': 'a', 'thread_id': 'missing'}, {'message_id': 'b', 'thread_id': 'cached'},
            {'message_id': 'c', 'thread_id': 'cached'}]
    result = gg._expand_attendee_threads(rows, threading.Event(), deadline=15,
                                         known_context={'cached': 'Exact previously gathered background'})
    assert not gathered.reads
    assert result[1]['thread_context'] == 'Exact previously gathered background'
    assert 'thread_context' not in result[2]  # no false claim that a new read omitted it


def test_native_automated_sender_metadata_cannot_enter_background(gathered, monkeypatch):
    original = gg._run
    def run(api, args, **kwargs):
        rows = original(api, args, **kwargs)
        if 'newer_than:30d' in args[2]:
            rows[0]['is_bot'] = True
        return rows
    monkeypatch.setattr(gg, '_run', run)
    payload, events, _ = gathered.invoke()
    assert not gathered.reads and 'Compare the three' not in _format_meeting_context({**payload, 'events': events})
