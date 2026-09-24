from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_shared/scripts'))
import compose_notification as notification
import gemini
from source_context import source_result


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a' * 32)
    monkeypatch.setattr(notification, 'enrich', lambda items, now: items)
    monkeypatch.setattr(gemini, 'provider_key', lambda _: 'test')
    return tmp_path


def test_empty_and_templates_make_no_model_call(isolated, monkeypatch):
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **k: pytest.fail('unexpected inference'))
    assert notification.compose('proactive', []) == 'NO_NUDGES'
    items = [{'kind': 'intention', 'key': 'x', 'decision_id': 'x', 'title': 'Call Sam', 'detail': 'About the deck'}]
    assert notification.compose('proactive', items) == 'Call Sam About the deck'
    items = [{'kind': 'meeting_prep', 'key': 'y', 'person': 'Sam', 'calendar_start': '2026-09-17T12:30:00Z'}]
    text = notification.compose('proactive', items, now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert '30 minutes' in text and text.endswith('Want the full prep on Sam?')
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert doc['effects'][0]['kind'] == 'pending_offer'
    prep = next(e for e in doc['effects'] if e['kind'] == 'meeting_prep_delivered')
    assert prep == {'kind': 'meeting_prep_delivered', 'mode': 'offer',
                    'calendar_event_id': '', 'calendar_start': '2026-09-17T12:30:00Z', 'date': ''}
    assert not (isolated / 'pending_offer.json').exists()


def _prep_item(**values):
    return {'kind': 'meeting_prep', 'key': 'meeting-1', 'decision_id': 'prep-1',
            'person': 'Sam', 'who': 'Partner at Example', 'identifier': 'sam@example.com',
            'calendar_start': '2026-09-17T12:30:00Z', **values}


def test_prep_reminder_joins_intro_and_reason_without_losing_offer(isolated, monkeypatch):
    calls = []
    context = 'Alex connected you through Priya. Sam suggested this coffee to discuss the robotics fund.'
    monkeypatch.setattr(notification, '_prep_threads', lambda item: [
        {'date': '2026-09-16', 'subject': 'Coffee', 'snippet': 'Sam suggested coffee to discuss the robotics fund.', 'from_me': False}])

    def write(*args, **kwargs):
        calls.append(json.loads(args[2]))
        return json.dumps({'items': [{'id': 'prep-1', 'text': context, 'draft': '', 'decline': ''}]})
    monkeypatch.setattr(gemini, '_gemini_once', write)
    item = _prep_item(person_facts={'sam': 'Alex introduced you to Priya, who connected you with Sam.'},
                      event={'summary': 'Coffee', 'description': 'Discuss robotics fund'}, open_loop='the deck you promised')
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    text = notification.compose('proactive', [item], now=now)
    assert notification.compose('proactive', [item], now=now) == text
    assert len(calls) == 1  # the existing composition cache owns retries
    assert context in text and '30 minutes' in text and 'Partner at Example' in text
    assert 'the deck you promised' in text and text.endswith('Want the full prep on Sam?')
    assert 'sam@example.com' not in text and text.count('?') == 1
    supplied = calls[0][0]
    assert supplied['event']['description'] == 'Discuss robotics fund'
    assert supplied['attendee_comms'][0]['date'] == '2026-09-16'
    assert 'Alex introduced' in supplied['person_facts']['sam']
    effects = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())['effects']
    offer = next(e['offer'] for e in effects if e['kind'] == 'pending_offer')
    assert offer['person'] == 'Sam' and offer['question'] == 'Want the full prep on Sam?'
    assert any(e['kind'] == 'source_permissions' and e['sources'] == ['gmail'] for e in effects)
    assert not (isolated / 'pending_offer.json').exists()  # acceptance still finalizes the offer


@pytest.mark.parametrize('failure', ['lookup', 'held', 'provider', 'invalid', 'empty'])
def test_optional_prep_context_cannot_swallow_the_reminder(isolated, monkeypatch, failure):
    def lookup(item):
        if failure == 'lookup':
            raise OSError('source unavailable')
        return []
    monkeypatch.setattr(notification, '_prep_threads', lookup)

    def write(*args, **kwargs):
        if failure == 'held':
            raise notification.model_work.ModelWorkHeldError('budget held')
        if failure == 'provider':
            raise OSError('provider unavailable')
        if failure == 'invalid':
            return json.dumps({'items': [{'id': 'prep-1', 'text': 'Want a draft?', 'draft': '', 'decline': ''}]})
        return '{"items":[]}'
    monkeypatch.setattr(gemini, '_gemini_once', write)
    text = notification.compose('proactive', [_prep_item(person_facts={'sam': 'Partner at Example'})],
                                now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert text == "You're meeting Sam (Partner at Example) in about 30 minutes. Want the full prep on Sam?"
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert doc['notification_decision_ids'] == ['prep-1']
    assert any(e['kind'] == 'pending_offer' for e in doc['effects'])


def test_prep_uses_exact_attendee_and_skips_revoked_gmail(isolated, monkeypatch):
    from types import SimpleNamespace
    calls = []
    gather = SimpleNamespace(_find_google_api=lambda: 'google_api.py',
        _fetch_attendee_comms=lambda api, email: calls.append(email) or (email, [{'snippet': 'Useful context'}]))
    monkeypatch.setattr(notification, '_load', lambda *a: gather)
    monkeypatch.setattr(notification, 'allowed', lambda _: False)
    assert notification._prep_threads(_prep_item()) == [] and calls == []
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    assert notification._prep_threads(_prep_item(identifier='Sam')) == [] and calls == []
    assert notification._prep_threads(_prep_item()) == [
        {'snippet': 'Useful context', 'relation_to_event': 'recent_background'}]
    assert calls == ['sam@example.com']
    consent = iter([True, False])
    monkeypatch.setattr(notification, 'allowed', lambda _: next(consent))
    assert notification._prep_threads(_prep_item()) == []


def test_prep_permission_change_during_write_keeps_plain_reminder(isolated, monkeypatch):
    monkeypatch.setattr(notification, '_prep_threads', lambda item: [{'snippet': 'Introduced by Alex'}])
    monkeypatch.setattr(notification, 'allowed', lambda _: False)
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: json.dumps({'items': [
        {'id': 'prep-1', 'text': 'Alex introduced you.', 'draft': '', 'decline': ''}]}))
    text = notification.compose('proactive', [_prep_item()], now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert 'Alex' not in text and text.endswith('Want the full prep on Sam?')
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert not any(e['kind'] == 'source_permissions' for e in doc['effects'])


def test_prep_thread_context_honors_muted_introducers_and_drops_oversized_excerpts(isolated, monkeypatch):
    from types import SimpleNamespace
    rows = [{'sender_name': 'Muted', 'sender_identifier': 'muted@example.com', 'snippet': 'Private'},
            {'sender_name': 'Priya', 'sender_identifier': 'priya@example.com', 'snippet': 'You two should meet.'},
            {'snippet': 'Long excerpt ' * 250}]
    gather = SimpleNamespace(_find_google_api=lambda: 'google_api.py',
                              _fetch_attendee_comms=lambda *a: ('sam@example.com', rows))
    monkeypatch.setattr(notification, '_load', lambda *a: gather)
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    monkeypatch.setattr(notification.preferences, 'load_explicit', lambda: {'mute_senders': ['muted@example.com']})
    assert notification._prep_threads(_prep_item()) == [rows[1]]
    assert rows[1]['relation_to_event'] == 'recent_background'
    projected = notification._writer_item({'id': 'prep-1', **_prep_item(), 'attendee_comms': [rows[1]]})
    assert 'Priya' in json.dumps(projected) and 'priya@example.com' not in json.dumps(projected)


def test_unselected_or_started_prep_never_fetches_or_writes_context(isolated, monkeypatch):
    monkeypatch.setattr(notification, '_prep_threads', lambda item: pytest.fail('unselected read'))
    monkeypatch.setattr(notification, '_write', lambda *a: pytest.fail('unselected model call'))
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    first = {'kind': 'intention', 'key': 'x', 'title': 'Call Alex', 'deadline': now.isoformat()}
    assert notification.compose('proactive', [first, _prep_item()], now=now) == 'Call Alex'
    assert notification.compose('proactive', [_prep_item(calendar_start=now.isoformat())], now=now) == 'NO_NUDGES'


@pytest.mark.parametrize('row', [
    {'text': 'Some background.', 'draft': 'Let us meet.', 'decline': ''},
    {'text': 'Some background.', 'draft': '', 'decline': 'No thanks.'},
    {'text': 'Where should we meet?', 'draft': '', 'decline': ''},
    {'text': 'A' * 501, 'draft': '', 'decline': ''},
    {'text': 'word ' * 61, 'draft': '', 'decline': ''},
    {'text': 'Alex introduced you.\nWant more?', 'draft': '', 'decline': ''},
])
def test_prep_context_has_no_extra_action_or_wall_of_text(isolated, row):
    with pytest.raises(ValueError):
        notification._validate(json.dumps({'items': [{'id': 'prep-1', **row}]}),
                               [{'id': 'prep-1', **_prep_item()}])


def test_one_direct_call_reuses_composed_artifact(isolated, monkeypatch):
    calls = []
    def write(*args, **kw):
        calls.append(args)
        return json.dumps({'items': [{'id': 'x', 'text': 'Sam needs the deck.',
                                     'draft': 'Here is the deck.', 'decline': ''}]})
    monkeypatch.setattr(gemini, '_gemini_once', write)
    items = [{'kind': 'commitment', 'key': 'x', 'decision_id': 'x', 'person': 'Sam', 'channel': 'email',
              'identifier': 'sam@example.com', 'thread_id': 'thread-1'}]
    first = notification.compose('proactive', items)
    assert notification.compose('proactive', items) == first
    assert len(calls) == 1
    assert 'Want this in your Gmail drafts?' in first and 'mailto:' not in first
    effects = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())['effects']
    details = json.loads(effects[0]['offer']['detail'])
    assert details['to'] == 'sam@example.com' and details['thread_id'] == 'thread-1'


def test_invalid_ids_have_only_two_attempts_even_after_queue_retry(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: calls.append(1) or json.dumps({
        'items': [{'id': 'invented', 'text': 'bad', 'draft': '', 'decline': ''}]}))
    for _ in range(3):
        with pytest.raises((ValueError, notification.model_work.ModelWorkHeldError)):
            notification.compose('proactive', [{'key': 'x', 'decision_id': 'x', 'kind': 'commitment'}])
    assert len(calls) == 2


def test_oversized_context_is_held_before_inference(isolated, monkeypatch):
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: pytest.fail('oversized input sent'))
    with pytest.raises(notification.model_work.ModelWorkHeldError):
        notification.compose('proactive', [{'kind': 'commitment', 'key': 'x', 'decision_id': 'x', 'detail': 'x' * 48000}])


def test_group_draft_has_no_person_link(isolated, monkeypatch):
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: json.dumps({
        'items': [{'id': 'x', 'text': 'The team needs the deck.', 'draft': 'Here it is.', 'decline': ''}]}))
    bundle = {'events': [{'decision_id': 'x', 'class': 'urgent', 'sender': 'Sam',
                          'event': {'source': 'whatsapp', 'sender_jid': '123@s.whatsapp.net', 'is_group_chat': True}}]}
    assert notification.compose('event', bundle) == 'The team needs the deck.\nHere it is.'


def test_verified_slots_do_not_overlap_and_unknown_constraints_do_not_guess():
    now = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
    calendar = {'all_calendars_complete': True, 'since': now.isoformat(), 'until': '2026-09-20T08:00:00Z',
                'events': [{'start': '2026-09-17T09:00:00Z', 'end': '2026-09-17T10:00:00Z'}]}
    slots = notification.slots('30 minutes Thursday?', calendar, now)
    assert slots and all('09:' not in slot for slot in slots)
    assert notification.slots('30 minutes Thursday after 3?', calendar, now) == []
    assert notification.slots('30 minutes Thursday?', None, now) == []
    assert notification.slots('Thursday or Friday?', calendar, now) == []


def test_scheduling_requires_both_choices_and_uses_code_slots(isolated, monkeypatch):
    row = {'id': 'x', 'text': 'Sam asked to meet.', 'draft': '{{slots}} works for me.', 'decline': 'I will pass, thanks.'}
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: json.dumps({'items': [row]}))
    item = {'kind': 'scheduling_ask', 'key': 'x', 'decision_id': 'x', 'channel': 'email', 'identifier': 'sam@example.com',
            'slots': ['Thu Sep 17, 10:00 AM UTC']}
    text = notification.compose('proactive', [item])
    assert 'Accept: Thu Sep 17, 10:00 AM UTC works for me.' in text
    assert 'Decline: I will pass, thanks.' in text
    assert 'Want this in your Gmail drafts?' not in text


def test_post_meeting_no_evidence_is_silent(isolated, monkeypatch):
    monkeypatch.setattr(notification, 'allowed', lambda _: False)
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: pytest.fail('no evidence'))
    assert notification.compose('event', {'events': [{'class': 'post_meeting', 'event': {'summary': 'Sync'}}]}) == 'NO_NUDGES'


def test_empty_post_meeting_yields_to_the_actionable_item_behind_it(isolated, monkeypatch):
    """A post-meeting item with no usable notes is not a reason to say nothing: the next
    candidate is tried, and the writer runs once, only when that candidate needs it."""
    monkeypatch.setattr(notification, 'allowed', lambda source: source != 'granola')
    calls = []

    def writer(items, llm):
        calls.append([i['id'] for i in items])
        return [{'id': items[0]['id'], 'text': 'Sam still needs the deck from you.', 'draft': '', 'decline': ''}]
    monkeypatch.setattr(notification, '_write', writer)
    monkeypatch.setattr(notification, 'enrich', lambda items, now: items)
    bundle = {'events': [{'class': 'post_meeting', 'event': {'summary': 'Sync'}},
                         {'class': 'chase', 'id': 'ask-1', 'person': 'Sam', 'detail': 'the deck', 'event': {}}]}
    text = notification.compose('event', bundle, now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert 'Sam still needs the deck' in text and len(calls) == 1
    # …and the same ask alone gives the same answer
    alone = {'events': [{'class': 'chase', 'id': 'ask-1', 'person': 'Sam', 'detail': 'the deck', 'event': {}}]}
    assert notification.compose('event', alone, now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc)) == text


def test_unrendered_candidates_do_not_earn_delivery_effects(isolated, monkeypatch):
    effects = notification.delivery_effects
    effects.stage([{'kind': 'proactive_seen', 'key': 'x', 'decision_id': 'x'}, {'kind': 'proactive_seen', 'key': 'y'},
                   {'kind': 'intention', 'id': 'unrendered'}, {'kind': 'source_permissions', 'sources': ['calendar']}])
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: '{"items":[]}')
    assert notification.compose('proactive', [{'kind': 'commitment', 'key': 'x', 'decision_id': 'x'}]) == 'NO_NUDGES'
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert doc['notification_decision_ids'] == []
    assert doc['effects'] == [{'kind': 'source_permissions', 'sources': ['calendar']}]


def test_post_meeting_uses_shared_persisted_capture_and_restricts_recipient(isolated, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    event = {'summary': 'Planning', 'start': '2026-09-17T10:00:00Z',
             'attendees': [{'email': 'sam@example.com', 'name': 'Sam'}]}
    def gather(argv, **kwargs):
        Path(argv[-1]).write_text(json.dumps({'meetings': [
            {'title': 'Planning', 'start': event['start'], 'transcript': 'I will send the deck.'}]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(notification.subprocess, 'run', gather)
    monkeypatch.setattr(notification, '_load', lambda *args: SimpleNamespace(
        extract_apply_meeting=lambda *a, **k: {'ledger': {'written': 1}, 'followup': {'drafts': [
            {'to_email': 'sam@example.com', 'body': 'Here is the deck.'},
            {'to_email': 'invented@example.com', 'body': 'Invented.'}]}}))
    rendered = notification._post_meeting({'event': event, 'title': 'Planning'})
    assert 'Here is the deck.' in rendered and 'Invented' not in rendered
    assert 'Want this in your Gmail drafts?' in rendered


def test_calendar_permission_revocation_prevents_availability(isolated, monkeypatch):
    from types import SimpleNamespace
    consent = iter([True, False])
    monkeypatch.setattr(notification, 'allowed', lambda _: next(consent))
    def gather(argv, **kwargs):
        Path(argv[argv.index('--cal-out') + 1]).write_text('[]')
        Path(argv[-1]).write_text(json.dumps({'calendar': source_result('ok', complete=True,
            since='2026-09-17T08:00:00Z', until='2026-09-20T08:00:00Z')}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(notification.subprocess, 'run', gather)
    assert notification._calendar() is None


def test_slots_must_be_inside_verified_coverage_and_constraints():
    now = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
    calendar = {'all_calendars_complete': True, 'events': [], 'since': now.isoformat(), 'until': '2026-09-17T09:30:00Z'}
    assert notification.slots('30 minutes Thursday?', calendar, now) == ['Thu Sep 17, 09:00 AM UTC']
    assert notification.slots('30 minutes Friday?', calendar, now) == []
    assert notification.slots('30 minutes Thursday 2pm?', calendar, now) == []
    assert notification.slots('30 minutes Thursday, I can only do lunch.', calendar, now) == []


def test_partial_calendar_is_not_evidence_of_free_time(isolated, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    def gather(argv, **kwargs):
        Path(argv[argv.index('--cal-out') + 1]).write_text('[]')
        Path(argv[-1]).write_text(json.dumps({'calendar': source_result('partial', complete=False,
            since='2026-09-17T08:00:00Z', until='2026-09-20T08:00:00Z')}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(notification.subprocess, 'run', gather)
    assert notification._calendar() is None


def test_complete_primary_calendar_cannot_establish_all_calendar_availability(isolated, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    def gather(argv, **kwargs):
        Path(argv[argv.index('--cal-out') + 1]).write_text('[]')
        Path(argv[-1]).write_text(json.dumps({'calendar': source_result('ok', complete=True,
            since='2026-09-17T08:00:00Z', until='2026-09-17T10:00:00Z')}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(notification.subprocess, 'run', gather)
    observed = notification._calendar()
    assert observed is not None
    proposed = notification.slots('30 minutes Thursday?', observed,
                                   datetime(2026, 9, 17, 8, tzinfo=timezone.utc))
    assert proposed == []


def test_unknown_scheduling_keeps_grounded_copy_without_false_acceptance(isolated):
    row = {'id': 'x', 'text': 'Sam wants to discuss the revised launch date.',
           'draft': '', 'decline': ''}
    text = notification._render(row, {'kind': 'scheduling_ask', 'slots': []})
    assert 'revised launch date' in text and text.endswith('How would you like to respond?')
    assert 'Accept:' not in text and 'Decline:' not in text


def test_reminder_survives_oversized_writer_and_only_selected_item_is_covered(isolated):
    items = [{'decision_id': 'urgent', 'key': 'a', 'kind': 'urgent', 'detail': 'x' * 48000},
             {'decision_id': 'reminder', 'key': 'b', 'kind': 'intention', 'title': 'Call Sam'}]
    assert notification.compose('proactive', items) == 'Call Sam'
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert doc['notification_decision_ids'] == ['reminder']


def test_scanner_fallback_identity_is_opaque(isolated):
    notification.compose('proactive', [{'key': 'birthday:Sam-Jones', 'kind': 'intention', 'title': 'Call Sam'}])
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert len(doc['notification_decision_ids'][0]) == 64
    assert 'Sam' not in json.dumps(doc['notification_decision_ids'])


def test_writer_projection_and_echo_guard(isolated):
    item = {'id': 'x', 'kind': 'chase', 'person': 'Sam', 'identifier': 'sam@example.com',
            'thread_id': 'thread-secret', 'open_loops': [{'name': 'Sam', 'what': 'Send deck',
            'identifier': '123@s.whatsapp.net', 'anchor_key': 'secret-anchor', 'chased_count': 2}]}
    prompt = json.dumps(notification._writer_item(item))
    for token in ('sam@example.com', 'thread-secret', 'secret-anchor', 'chased_count', '123@s.whatsapp.net'):
        assert token not in prompt
    for text in ('Sam: needs deck', 'The ledger needs updating', 'Sam uses 123@s.whatsapp.net', 'thread-secret', '{{slots}}', 'x' * 1201):
        with pytest.raises(ValueError):
            notification._validate(json.dumps({'items': [{'id': 'x', 'text': text, 'draft': '', 'decline': ''}]}), [item])
    # prose with a colon in it is not a labelled field
    assert notification._validate(json.dumps({'items': [{'id': 'x', 'text': 'Meeting at noon: bring the deck.',
                                                          'draft': '', 'decline': ''}]}), [item])


def test_empty_decision_is_not_cached_across_new_ticks(isolated, monkeypatch):
    calls = []
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: calls.append(1) or '{"items":[]}')
    for tick in ('b', 'c', 'd'):
        monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', tick * 32)
        assert notification.compose('proactive', [{'key': 'x', 'kind': 'commitment'}]) == 'NO_NUDGES'
    assert len(calls) == 3
    assert not list((isolated / 'events/notification-artifacts').glob('*.json'))


def test_transient_writer_failure_waits_for_queue_backoff(isolated, monkeypatch):
    from urllib.error import HTTPError
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise HTTPError('https://provider', 503, 'busy', {}, None)
    monkeypatch.setattr(gemini, '_gemini_once', fail)
    with pytest.raises(HTTPError):
        notification.compose('proactive', [{'key': 'x', 'kind': 'commitment'}])
    assert len(calls) == 1


def test_short_offer_question_survives_delivery_whitespace_normalization(isolated, monkeypatch):
    item = {'kind': 'commitment', 'identifier': 'sam@example.com', 'channel': 'email'}
    rendered = notification._render({'text': 'Sam needs the deck.', 'draft': 'Here is the deck.', 'decline': ''}, item)
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    question = doc['effects'][0]['offer']['question']
    assert question == 'Want this in your Gmail drafts?'
    assert question in ' '.join(rendered.split())


def test_one_primary_item_and_one_offer_per_tick(isolated, monkeypatch):
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: pytest.fail('deterministic primary'))
    items = [{'decision_id': 'later', 'key': 'later', 'kind': 'meeting_prep', 'person': 'Later',
              'calendar_start': '2026-09-17T12:50:00Z'},
             {'decision_id': 'soon', 'key': 'soon', 'kind': 'meeting_prep', 'person': 'Soon',
              'calendar_start': '2026-09-17T12:10:00Z'}]
    text = notification.compose('proactive', items, now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert 'Soon' in text and 'Later' not in text
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert doc['notification_decision_ids'] == ['soon']
    assert len([e for e in doc['effects'] if e['kind'] == 'pending_offer']) == 1


def test_optional_knowledge_failure_does_not_drop_candidate(isolated, monkeypatch):
    monkeypatch.setattr(notification.loops_query, 'query', lambda: {'you_owe': [], 'waiting_on_them': []})
    monkeypatch.setattr(notification.style_apply, 'apply', lambda _: {})
    monkeypatch.setattr(notification, '_read_json_script', lambda *a: (_ for _ in ()).throw(RuntimeError('offline')))
    items = [{'id': 'x', 'kind': 'commitment', 'person': 'Sam'}]
    # Fixture replaces enrich; call the original function captured by its module source.
    import importlib.util
    spec = importlib.util.spec_from_file_location('notification_real_enrich', notification.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '_read_json_script', notification._read_json_script)
    assert module.enrich(items, datetime.now(timezone.utc))[0]['person_facts'] == {}


@pytest.fixture
def real_enrichment(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'b' * 32)
    monkeypatch.setattr(notification.style_apply, 'apply', lambda _: {})
    monkeypatch.setattr(notification, '_read_json_script', lambda *a: {})
    monkeypatch.setattr(notification.loops_query, 'query', lambda: {'you_owe': [], 'waiting_on_them': []})
    monkeypatch.setattr(notification.delivery_effects, '_loops', lambda: {})
    return tmp_path


def test_stale_scheduling_candidate_gets_current_invitation_and_thread_before_writing(real_enrichment, monkeypatch):
    import personal_context
    now = datetime(2026, 9, 24, 14, tzinfo=timezone.utc)
    old = {'source': 'email', 'id': 'ask', 'threadId': 'invite-thread',
           'from': 'assistant@example.com', 'date': '2026-09-21T20:00:00Z',
           'body': 'Please schedule Thursday at 3:45 with Jordan.'}
    newer = {**old, 'id': 'confirmation', 'date': '2026-09-23T20:00:00Z',
             'body': 'Confirmed Thursday at 3:45. Invite sent.'}
    unrelated = {**newer, 'threadId': 'different-thread', 'body': 'unrelated-private-text'}
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: [(newer, '', ''), (unrelated, '', '')])
    monkeypatch.setattr(notification, '_calendar', lambda: {'all_calendars_complete': False, 'events': [
        {'summary': 'Zoom: Jordan <> Fund', 'start': '2026-09-24T15:45:00-07:00',
         'end': '2026-09-24T16:30:00-07:00', 'my_response': 'accepted'}]})
    def write(model, key, prompt, **kwargs):
        system = kwargs["system"]
        value = json.loads(prompt)
        data = json.dumps(value)
        assert 'Invite sent.' in data and 'Zoom: Jordan' in data and '2026-09-21' in data
        assert 'unrelated-private-text' not in data
        assert 'Omit a scheduling question once the' in system
        return json.dumps({'items': []})
    monkeypatch.setattr(gemini, 'provider_key', lambda _: 'test')
    monkeypatch.setattr(gemini, '_gemini_once', write)
    bundle = {'events': [{'class': 'scheduling_ask', 'decision_id': 'held-ask', 'event': old}]}
    assert notification.compose('event', bundle, now=now) == 'NO_NUDGES'


def test_group_commitment_gets_calendar_and_the_group_confirmation(real_enrichment, monkeypatch):
    import personal_context
    now = datetime(2026, 9, 24, 14, tzinfo=timezone.utc)
    loop = {'anchor_key': 'ask', 'identifier': '', 'name': 'Planning group', 'group_id': 'group-id'}
    monkeypatch.setattr(notification.loops_query, 'query', lambda: {'you_owe': [loop], 'waiting_on_them': []})
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: [({'source': 'imessage',
        'chat_guid': 'group-id', 'text': 'Booked, see you Thursday.', 'timestamp': '2026-09-23T20:00:00Z'}, '', '')])
    monkeypatch.setattr(notification, '_calendar', lambda: {'events': [{'summary': 'Jordan meeting'}]})
    item = {'id': 'x', 'kind': 'commitment', 'channel': 'imessage', 'anchor_key': 'ask'}
    enriched, = notification.enrich([item], now)
    sent = notification._writer_item(enriched)
    assert sent['calendar']['events'][0]['summary'] == 'Jordan meeting'
    assert sent['current_conversation'][0]['text'] == 'Booked, see you Thursday.'
    assert 'group-id' not in json.dumps(sent)


def test_resolved_original_message_is_removed_before_writer(real_enrichment, monkeypatch):
    monkeypatch.setattr(notification, '_calendar', lambda: None)
    monkeypatch.setattr(notification.delivery_effects, '_loops', lambda: {'a': {'status': 'resolved',
        'source_refs': [{'sourceType': 'email', 'sourceId': 'original'}]}})
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **k: pytest.fail('closed ask reached writer'))
    bundle = {'events': [{'class': 'scheduling_ask', 'event': {'source': 'email', 'id': 'original'}}]}
    assert notification.compose('event', bundle) == 'NO_NUDGES'


def test_unknown_attendee_uses_meeting_title_instead_of_a_handle(isolated):
    text = notification.compose('proactive', [_prep_item(person='', title='Board meeting')],
                                now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert text.startswith('Board meeting starts in about 30 minutes.')
    assert text.endswith('Want the full prep?')
    effects = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())['effects']
    offer = next(e['offer'] for e in effects if e['kind'] == 'pending_offer')
    assert offer['question'] == 'Want the full prep?'


@pytest.mark.parametrize('initial_status', ['open', 'resolved', 'retry'])
def test_closure_recheck_is_bound_to_selected_request_not_whole_bundle(real_enrichment, monkeypatch, initial_status):
    effects = notification.delivery_effects
    rows = {'old': {'status': 'open' if initial_status == 'retry' else initial_status,
                    'source_refs': [{'sourceType': 'email', 'sourceId': 'old'}]},
            'fresh': {'status': 'open', 'source_refs': [{'sourceType': 'email', 'sourceId': 'fresh'}]}}
    monkeypatch.setattr(effects, '_loops', lambda: rows)
    monkeypatch.setattr(notification, '_calendar', lambda: None)
    bundle = {'events': [{'class': 'scheduling_ask', 'decision_id': ident,
                         'event': {'source': 'email', 'id': ident, 'threadId': ident}} for ident in rows]}
    preflight = effects.for_bundle(bundle)['effects']
    assert effects.valid(preflight)
    if initial_status == 'retry':
        monkeypatch.setattr(notification, '_write', lambda items, llm: [
            {'id': 'old', 'text': 'Sam needs a meeting time.', 'draft': '', 'decline': ''}])
        assert 'Sam needs' in notification.compose('event', bundle)
        rows['old']['status'] = 'resolved'
    monkeypatch.setattr(notification, '_write', lambda items, llm: [
        {'id': 'fresh', 'text': 'Alex needs a meeting time.', 'draft': '', 'decline': ''}])
    assert 'Alex needs' in notification.compose('event', bundle)
    staged = json.loads((real_enrichment / ('events/delivery-effects-' + 'b' * 32 + '.json')).read_text())['effects']
    rows['old']['status'] = 'resolved'
    assert effects.valid([*preflight, *staged])  # closed or newly closed unselected ask cannot cancel this
    rows['fresh']['status'] = 'resolved'
    assert not effects.valid([*preflight, *staged])  # selected ask closes while awaiting acceptance


def test_notification_thread_context_still_honors_mutes_and_source_consent(real_enrichment, monkeypatch):
    import personal_context
    now = datetime(2026, 9, 24, 14, tzinfo=timezone.utc)
    event = {'source': 'imessage', 'chat_guid': 'group', 'text': 'Ask', 'timestamp': '2026-09-23T21:00:00Z'}
    private = {**event, 'handle': '+15551234567', 'text': 'muted-private-text'}
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: [(private, '', '')])
    monkeypatch.setattr(notification.preferences, 'load_explicit', lambda: {'mute_senders': ['+15551234567']})
    monkeypatch.setattr(notification, '_calendar', lambda: None)
    item = {'id': 'x', 'kind': 'scheduling_ask', 'event': event}
    assert 'muted-private-text' not in json.dumps(notification.enrich([item], now))
    import source_context
    monkeypatch.setattr(source_context, 'allowed', lambda source: False)
    assert notification.enrich([item], now)[0]['current_conversation'] == []
