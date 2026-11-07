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
    assert any(e['kind'] == 'pending_offer' for e in doc['effects'])
    prep = next(e for e in doc['effects'] if e['kind'] == 'meeting_prep_delivered')
    assert prep == {'kind': 'meeting_prep_delivered', 'mode': 'offer',
                    'calendar_event_id': '', 'calendar_start': '2026-09-17T12:30:00Z', 'date': ''}
    assert not (isolated / 'pending_offer.json').exists()


@pytest.mark.parametrize('sent, expected', [
    ('Fri, 18 Sep 2026 16:34:02 +0000', 'From September 18:'),
    ('2026-09-25T14:00:00Z', 'From 7:00 AM today:'),
    ('2025-09-18T16:34:02Z', 'From September 18, 2025:'),
])
def test_old_ask_keeps_original_date_not_queue_arrival(isolated, monkeypatch, sent, expected):
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 25, 9, tzinfo=ZoneInfo('America/Los_Angeles'))
    captured = []
    def write(items, llm):
        captured.extend(notification._writer_item(i) for i in items)
        return [{'id': 'old-ask', 'text': 'Alex asked about meeting the founder.', 'draft': '', 'decline': ''}]
    monkeypatch.setattr(notification, '_write', write)
    bundle = {'events': [{'decision_id': 'old-ask', 'class': 'scheduling_ask', 'ts': now.isoformat(),
                          'event': {'source': 'email', 'date': sent, 'body': 'Want to join me?'}}]}
    text = notification.compose('event', bundle, now=now)
    assert text.startswith(expected) and 'Alex asked' in text
    assert captured[0]['source_timing']['sent_at'].startswith(sent[:4] if sent[:4].isdigit() else '2026-09-18')
    assert captured[0]['source_timing']['as_of_date'] == '2026-09-25'


def test_calendar_claim_cannot_be_rewritten_as_cancellation(isolated, monkeypatch):
    monkeypatch.setattr(notification, '_write', lambda *a: pytest.fail('detector claim reached writer'))
    bundle = {'events': [{'class': 'calendar_change', 'event': {'source': 'calendar_change',
        'change': 'removed', 'text': 'Acme on Fri, Sep 25 at 10:00 AM is no longer on your calendar.'}}]}
    assert notification.compose('event', bundle) == bundle['events'][0]['event']['text']


def test_handoff_delivers_reminder_receipt_without_model_or_send(isolated, monkeypatch):
    monkeypatch.setattr(notification, '_write', lambda *a: pytest.fail('handoff must not call model'))
    monkeypatch.setattr(notification.action_links, 'link_for', lambda *a, **k: pytest.fail('no send link'))
    text = "I've reminded you twice about Maya and the contract — want a follow-up draft, or let it go?"
    item = {'kind': 'handoff', 'key': 'handoff-maya', 'person': 'Maya', 'detail': text}
    assert notification.compose('proactive', [item]) == text
    effects = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())['effects']
    offer = next(effect['offer'] for effect in effects if effect['kind'] == 'pending_offer')
    assert offer['question'] == text and offer['kind'] == 'handoff'


def test_held_calendar_change_gets_a_short_detection_prefix(isolated, monkeypatch):
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 25, 9, tzinfo=ZoneInfo('America/Los_Angeles'))
    monkeypatch.setattr(notification, '_write', lambda *a: pytest.fail('detector claim reached writer'))
    text = 'Acme on Fri, Sep 25 at 10:00 AM is no longer on your calendar.'
    bundle = {'events': [{'class': 'calendar_change', 'event': {'source': 'calendar_change',
        'change': 'removed', 'timestamp': '2026-09-25T14:00:00Z', 'text': text}}]}
    assert notification.compose('event', bundle, now=now) == 'Noticed 7:00 AM today: ' + text


def test_dated_ask_rules_reach_the_writer_once():
    # _write appends relevance.md to SYSTEM; the writer-specific field stays here, the shared
    # rules live only in the policy, so the combined prompt carries each rule exactly once.
    policy = (notification.PACK / '_shared/references/relevance.md').read_text()
    combined = notification.SYSTEM + '\n' + policy
    assert 'source_timing' in notification.SYSTEM
    for rule in ('automated reminder', 'Interpret "today" and "tomorrow"'):
        assert combined.count(rule) == 1, rule


@pytest.mark.parametrize('source', ['imessage', 'whatsapp'])
def test_bridge_wall_time_does_not_turn_today_into_yesterday(isolated, monkeypatch, source):
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 25, 9, tzinfo=ZoneInfo('America/Los_Angeles'))
    monkeypatch.setattr(notification, '_write', lambda *a: [
        {'id': 'x', 'text': 'Sam asked for a call.', 'draft': '', 'decline': ''}])
    bundle = {'events': [{'decision_id': 'x', 'class': 'urgent',
        'event': {'source': source, 'timestamp': '2026-09-25 06:00:00', 'text': 'Call me'}}]}
    assert notification.compose('event', bundle, now=now).startswith('From 6:00 AM today:')


def test_cached_name_permission_is_rechecked_before_offer(isolated, monkeypatch):
    monkeypatch.setattr(notification, 'allowed', lambda _: False)
    text = notification.compose('proactive', [_prep_item(name_source='gmail', title='Acme meeting')],
                                now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert 'Sam' not in text and text.startswith('Acme meeting')


def _prep_item(**values):
    return {'kind': 'meeting_prep', 'key': 'meeting-1', 'decision_id': 'prep-1',
            'person': 'Sam', 'who': 'Partner at Example', 'identifier': 'sam@example.com',
            'calendar_start': '2026-09-17T12:30:00Z', **values}


def test_prep_reminder_joins_intro_and_reason_without_losing_offer(isolated, monkeypatch):
    calls = []
    context = 'Alex connected you through Priya for a robotics-fund discussion. You also promised Sam the deck.'
    monkeypatch.setattr(notification, '_prep_threads', lambda item, now=None: [
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
    assert text.count('promised Sam the deck') == 1 and text.endswith('Want the full prep on Sam?')
    assert 'Open with them:' not in text
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
    def lookup(item, now=None):
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


def test_prep_uses_cached_exact_attendee_and_skips_revoked_gmail(isolated, monkeypatch):
    import personal_context
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    event = {'source': 'email', 'from': 'Sam Founder <sam@example.com>',
             'to': 'Owner <owner@fund.example>', 'date': '2026-09-16T12:00:00Z',
             'body': 'Useful context', 'threadId': 'known-thread'}
    unrelated = {**event, 'from': 'other@example.com', 'body': 'sam@example.com mentioned only'}
    old = {**event, 'date': '2026-08-01T12:00:00Z', 'body': 'Obsolete context'}
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: [(r, '', '') for r in (event, unrelated, old)])
    monkeypatch.setattr(notification.subprocess, 'run', lambda *a, **k: pytest.fail('prep fetched a source'))
    monkeypatch.setattr(notification, 'allowed', lambda _: False)
    assert notification._prep_threads(_prep_item(), now) == []
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    assert notification._prep_threads(_prep_item(identifier='Sam'), now) == []
    row, = notification._prep_threads(_prep_item(), now)
    assert row['snippet'] == 'Useful context' and row['sender_name'] == 'Sam Founder'
    assert row['relation_to_event'] == 'recent_background' and row['from_me'] is None
    bound, = notification._prep_threads(_prep_item(event={'threadId': 'known-thread'}), now)
    assert bound['relation_to_event'] == 'event_thread'
    consent = iter([True, False])
    monkeypatch.setattr(notification, 'allowed', lambda _: next(consent))
    assert notification._prep_threads(_prep_item(), now) == []


def test_prep_permission_change_during_write_keeps_plain_reminder(isolated, monkeypatch):
    monkeypatch.setattr(notification, '_prep_threads', lambda item, now=None: [{'snippet': 'Introduced by Alex'}])
    monkeypatch.setattr(notification, 'allowed', lambda _: False)
    monkeypatch.setattr(gemini, '_gemini_once', lambda *a, **kw: json.dumps({'items': [
        {'id': 'prep-1', 'text': 'Alex introduced you.', 'draft': '', 'decline': ''}]}))
    text = notification.compose('proactive', [_prep_item()], now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc))
    assert 'Alex' not in text and text.endswith('Want the full prep on Sam?')
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert not any(e['kind'] == 'source_permissions' for e in doc['effects'])


@pytest.mark.parametrize('mute', [{'mute_senders': ['muted@example.com']}, {'mute_people': ['Muted']}])
def test_cached_prep_context_honors_mutes_and_bounds_text(isolated, monkeypatch, mute):
    import personal_context
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    base = {'source': 'email', 'to': 'sam@example.com', 'date': '2026-09-16T12:00:00Z'}
    rows = [{**base, 'from': 'Muted <muted@example.com>', 'body': 'Private'},
            {**base, 'from': 'Priya <priya@example.com>', 'body': 'You two should meet.'},
            {**base, 'from': 'Sam <sam@example.com>', 'body': 'Long excerpt ' * 250}]
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: [(r, '', '') for r in rows])
    monkeypatch.setattr(notification, 'allowed', lambda _: True)
    monkeypatch.setattr(notification.preferences, 'load_explicit', lambda: mute)
    context = notification._prep_threads(_prep_item(), now)
    assert len(context) == 2 and 'Private' not in json.dumps(context)
    assert all(len(row['snippet']) <= personal_context.CONVERSATION_TEXT_CHARS for row in context)
    projected = notification._writer_item({'id': 'prep-1', **_prep_item(), 'attendee_comms': context})
    assert 'Priya' in json.dumps(projected) and 'priya@example.com' not in json.dumps(projected)


def test_unselected_or_started_prep_never_fetches_or_writes_context(isolated, monkeypatch):
    monkeypatch.setattr(notification, '_prep_threads', lambda item, now=None: pytest.fail('unselected read'))
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


@pytest.mark.parametrize('identity_source', ['environment', 'settings'])
def test_verified_slots_ignore_free_and_other_owners_events_but_keep_real_commitments(monkeypatch, tmp_path, identity_source):
    if identity_source == 'environment':
        monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    else:
        monkeypatch.delenv('SOTTO_USER_EMAIL', raising=False)
        monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
        (tmp_path / 'config').mkdir()
        (tmp_path / 'config/settings.json').write_text(json.dumps({'google_account_email': 'owner@example.com'}))
    now = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
    day = {'start': '2026-09-17', 'end': '2026-09-18', 'calendar_id': 'primary'}
    calendar = {'all_calendars_complete': True, 'since': now.isoformat(),
                'until': '2026-09-18T08:00:00Z', 'events': [
                    {**day, 'transparency': 'transparent'},
                    {**day, 'eventType': 'workingLocation'},
                    {**day, 'calendar_id': 'colleague@example.com',
                     'attendees': [{'email': 'colleague@example.com', 'self': True}]},
                    {**day, 'my_response': 'declined'}, {**day, 'status': 'cancelled'},
                    {**day, 'start': '2026-09-17T09:30:00Z', 'end': '2026-09-17T10:00:00Z',
                     'calendar_id': 'team@example.com',
                     'attendees': [{'email': 'owner@example.com', 'responseStatus': 'accepted'}]},
                    {**day, 'start': '2026-09-17T10:00:00Z', 'end': '2026-09-17T10:30:00Z',
                     'eventType': 'focusTime'}]}
    assert notification.slots('30 minutes Thursday?', calendar, now) == [
        'Thu Sep 17, 09:00 AM UTC', 'Thu Sep 17, 10:30 AM UTC', 'Thu Sep 17, 11:00 AM UTC']
    calendar['events'].append({**day, 'eventType': 'outOfOffice'})
    assert notification.slots('30 minutes Thursday?', calendar, now) == []


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
    # The fixture replaces enrich; load the original function.
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


def test_reminder_uses_stored_relationship_memory_without_mail_or_another_model_stage(real_enrichment, monkeypatch):
    calls, lookups = [], []
    memory = 'On September 10, Sam shared the new fund plans after your last walk.'
    def knowledge(*args):
        lookups.append(args)
        return {'sam': memory}
    monkeypatch.setattr(notification, '_read_json_script', knowledge)
    monkeypatch.setattr(notification, '_prep_threads', lambda *a: [])
    monkeypatch.setattr(notification.subprocess, 'run', lambda *a, **k: pytest.fail('unexpected source fetch'))
    monkeypatch.setattr(gemini, 'provider_key', lambda _: 'test')
    def writer(provider, model, key, prompt, **kwargs):
        calls.append(json.loads(prompt)[0])
        return json.dumps({'items': [{'id': 'prep-1', 'text': memory, 'draft': '', 'decline': ''}]})
    now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
    item = _prep_item(event={'summary': 'Walk'})
    text = notification.compose('proactive', [item], now=now, llm=writer)
    assert notification.compose('proactive', [item], now=now, llm=writer) == text
    assert len(calls) == 1 and memory in text
    assert all(args[1:3] == ('--person', 'sam@example.com') for args in lookups)
    assert calls[0]['person_facts']['sam'] == memory and calls[0]['as_of_date'] == '2026-09-17'
    assert text.endswith('Want the full prep on Sam?') and text.count('?') == 1


def test_conversation_alone_reaches_reminder_writer_and_stale_scanner_task_does_not(isolated, monkeypatch):
    monkeypatch.setattr(notification, '_prep_threads', lambda *a: [])
    calls = []
    def writer(provider, model, key, prompt, **kwargs):
        calls.append(json.loads(prompt)[0])
        return json.dumps({'items': [{'id': 'prep-1', 'text': 'Sam thanked you for the introduction yesterday.',
                                     'draft': '', 'decline': ''}]})
    item = _prep_item(open_loop='make the old introduction', detail='open with them: make the old introduction',
                      current_conversation=[{'ts': '2026-09-16T12:00:00Z', 'is_from_me': False,
                                             'text': 'Thanks for the introduction.'}])
    text = notification.compose('proactive', [item], now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc), llm=writer)
    assert len(calls) == 1 and 'Thanks for the introduction.' in json.dumps(calls)
    assert 'make the old introduction' not in json.dumps(calls) and 'make the old introduction' not in text
    assert 'thanked you' in text and 'Open with them:' not in text


def test_writer_failure_does_not_append_unreviewed_commitment(isolated, monkeypatch):
    monkeypatch.setattr(notification, '_prep_threads', lambda *a: [])
    def writer(*args, **kwargs):
        raise notification.model_work.ModelWorkHeldError('held')
    item = _prep_item(open_loop='send the already delivered deck',
                      open_loops=[{'what': 'Review the new proposal', 'direction': 'you_owe'}])
    text = notification.compose('proactive', [item], now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc), llm=writer)
    assert 'deck' not in text and 'proposal' not in text and text.endswith('Want the full prep on Sam?')


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
    import yaml
    now = datetime(2026, 9, 24, 14, tzinfo=timezone.utc)
    loop = {'anchor_key': 'ask', 'status': 'open', 'action_type': 'reply',
            'contact_identifier': '', 'contact_name': 'Planning group', 'group_id': 'group-id',
            'source': 'imessage', 'channel': 'imessage', 'summary': 'Confirm the group plan',
            'created_at': '2026-09-23'}
    directory = real_enrichment / 'knowledge' / 'continuity'
    directory.mkdir(parents=True)
    (directory / 'group.md').write_text('---\n' + yaml.safe_dump(loop) + '---\n')
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: [({'source': 'imessage',
        'chat_guid': 'group-id', 'text': 'Booked, see you Thursday.', 'timestamp': '2026-09-23T20:00:00Z'}, '', '')])
    monkeypatch.setattr(notification, '_calendar', lambda: {'events': [{'summary': 'Jordan meeting'}]})
    item = {'id': 'x', 'kind': 'commitment', 'channel': 'imessage', 'anchor_key': 'ask'}
    enriched, = notification.enrich([item], now)
    sent = notification._writer_item(enriched)
    assert sent['calendar']['events'][0]['summary'] == 'Jordan meeting'
    assert sent['current_conversation'][0]['text'] == 'Booked, see you Thursday.'
    assert 'group-id' not in json.dumps(sent)


_CEDAR = {'anchor_key': 'gmail:waiting_on:cedar', 'status': 'open', 'source': 'gmail',
          'source_message_id': 'cedar-message', 'source_thread_id': 'cedar-thread',
          'source_refs': [{'sourceType': 'gmail', 'sourceId': 'cedar-message'}],
          'contact_name': 'Dana', 'contact_identifier': 'dana@example.com',
          'action_type': 'waiting_on', 'summary': 'Send Cedar deck', 'created_at': '2026-08-20'}


@pytest.fixture
def chase_ledger(real_enrichment, monkeypatch):
    import continuity_resolve as cr
    import personal_context
    monkeypatch.setattr(notification, '_calendar', lambda: None)
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: [])
    cr._persist(dict(_CEDAR))
    return real_enrichment / 'knowledge' / 'continuity'


def _chase():
    import continuity_resolve as cr
    row = cr._load_items()[_CEDAR['anchor_key']]
    return {'id': 'c1', 'kind': 'chase', 'anchor_key': _CEDAR['anchor_key'], 'person': 'Dana',
            'identifier': 'dana@example.com', 'title': 'Dana - Send Cedar deck', 'detail': 'asked 7 days ago',
            'loop_version': notification.delivery_effects.loop_version(row),
            'loop_identity': notification.delivery_effects.loop_identity(row)}


def test_a_loop_nudge_survives_a_ledger_that_grew_past_the_old_bound(chase_ledger):
    """PR #817 review: 1024 files was reachable by design (parked rows are never pruned), and
    every batch with a chase, commitment or hand-off then failed to compose."""
    for i in range(1030):
        (chase_ledger / f'old-{i}.md').write_text(
            f"---\nanchor_key: k{i}\nstatus: parked\nparked_at: '2026-03-01'\n"
            "action_type: follow_up\nsummary: x\n---\n")
    enriched, = notification.enrich([_chase()], datetime(2026, 8, 27, 12, tzinfo=timezone.utc))
    assert not enriched.get('resolved')
    assert enriched['_loop_snapshot']['anchor_key'] == _CEDAR['anchor_key']


def test_a_malformed_ledger_file_does_not_fail_a_loop_nudge(chase_ledger):
    (chase_ledger / 'notes.md').write_text("hello, not frontmatter\n")
    (chase_ledger / 'broken.md').write_text("---\nsummary: [unclosed\n---\n")
    enriched, = notification.enrich([_chase()], datetime(2026, 8, 27, 12, tzinfo=timezone.utc))
    assert not enriched.get('resolved')
    assert enriched['_loop_snapshot']['anchor_key'] == _CEDAR['anchor_key']


@pytest.mark.parametrize('failure', ['untrusted_row', 'unavailable'])
def test_a_partial_loop_read_composes_from_the_ordinary_view_without_binding(chase_ledger, monkeypatch,
                                                                             capsys, failure):
    import protected_ledger_io
    if failure == 'untrusted_row':
        (chase_ledger / 'dup.md').write_text('---\nsource: gmail\nsource: user_added\nsummary: x\n---\n')
    else:
        monkeypatch.setattr(protected_ledger_io, 'MAX_FILES', 0)
    view = {'you_owe': [], 'waiting_on_them': [{'anchor_key': _CEDAR['anchor_key'], 'name': 'Dana',
                                                'identifier': 'dana@example.com', 'what': 'Send Cedar deck'}]}
    monkeypatch.setattr(notification.loops_query, 'query', lambda: view)
    enriched, = notification.enrich([_chase()], datetime(2026, 8, 27, 12, tzinfo=timezone.utc))
    assert not enriched.get('resolved')
    assert enriched['_loop_snapshot'] is None and enriched.get('_composition_sources') is None
    assert 'coverage partial' in capsys.readouterr().err


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


@pytest.mark.parametrize('code', [429, 500, 502, 503, 504, 400, 401, 402, 403])
def test_notification_cli_uses_typed_provider_retry_without_payload(isolated, monkeypatch, capsys, code):
    from urllib.error import HTTPError
    bundle = isolated / 'bundle.json'
    bundle.write_text('{}')
    monkeypatch.setattr(sys, 'argv', ['compose_notification.py', json.dumps({'kind': 'event', 'bundle_path': str(bundle)})])
    def fail(*args):
        raise HTTPError('https://private-provider', code, 'private response body', {}, None)
    monkeypatch.setattr(notification, 'compose', fail)
    if code in (429, 500, 502, 503, 504):
        assert notification.main() == notification.work_queue.PROVIDER_RETRY_EXIT
        captured = capsys.readouterr()
        assert captured.out == '' and captured.err == 'notification provider temporarily unavailable\n'
    else:
        with pytest.raises(HTTPError):
            notification.main()


def test_notification_cli_refunds_only_typed_proxy_busy(isolated, monkeypatch, capsys):
    from urllib.error import HTTPError
    bundle = isolated / 'bundle.json'
    bundle.write_text('{}')
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    monkeypatch.setattr(sys, 'argv', ['compose_notification.py', json.dumps({
        'kind': 'event', 'bundle_path': str(bundle)})])
    def fail(*args):
        raise HTTPError('https://proxy.example/native/v1beta/models/gemini-3.8-flash:generateContent',
                        429, 'private reason', {'X-Sotto-Admission': 'busy'}, None)
    monkeypatch.setattr(notification, 'compose', fail)
    assert notification.main() == notification.work_queue.MODEL_ADMISSION_BUSY_EXIT
    assert capsys.readouterr().err == 'notification model admission busy\n'


def test_busy_candidate_stops_before_later_error_can_hide_deferral(isolated, monkeypatch):
    from urllib.error import HTTPError
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_MODEL_PROXY_URL', 'https://proxy.example')
    busy = HTTPError('https://proxy.example/native/v1beta/models/gemini-3.8-flash:generateContent',
                     429, 'private reason', {'X-Sotto-Admission': 'busy'}, None)
    calls = []
    def compose_prep(item, now, llm, may_write=True):
        calls.append(item['id'])
        if len(calls) == 1:
            raise busy
        raise ValueError('later malformed candidate')
    monkeypatch.setattr(notification, '_meeting_prep', compose_prep)
    items = [{'key': 'first', 'kind': 'meeting_prep', 'person': 'Alex'},
             {'key': 'second', 'kind': 'meeting_prep', 'person': 'Blair'}]
    with pytest.raises(HTTPError) as error:
        notification.compose('proactive', items)
    assert error.value is busy
    assert len(calls) == 1


def test_reminder_without_textual_evidence_makes_no_model_call(real_enrichment, monkeypatch):
    # Self-host consent admits a sourceless calendar event, which comes back as one empty message.
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.setattr(notification, '_prep_threads', lambda *a: [])
    item = _prep_item(event={'summary': 'Intro call', 'start': '2026-09-17T12:30:00Z'})
    text = notification.compose('proactive', [item], now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
                                llm=lambda *a, **k: pytest.fail('no grounded evidence, no writer'))
    assert text == "You're meeting Sam (Partner at Example) in about 30 minutes. Want the full prep on Sam?"


def test_reminder_open_items_join_by_identifier_before_shared_first_name(real_enrichment, monkeypatch):
    other = {'name': 'Sam', 'identifier': '+15550001111', 'what': 'Send Sam the lease addendum',
             'direction': 'you_owe', 'anchor_key': 'imessage:reply:id:5550001111', 'deadline': None}
    same = {'name': 'Samantha Lee', 'identifier': 'SAM@example.com', 'what': 'Send the fund memo',
            'direction': 'you_owe', 'anchor_key': 'gmail:reply:id:memo', 'deadline': None}
    unbound = {'name': 'Sam', 'identifier': '', 'what': 'Return the borrowed book',
               'direction': 'you_owe', 'anchor_key': 'note:book', 'deadline': None}
    monkeypatch.setattr(notification.loops_query, 'query',
                        lambda: {'you_owe': [other, same, unbound], 'waiting_on_them': []})
    monkeypatch.setattr(notification, '_prep_threads', lambda *a: [])
    calls = []
    def writer(provider, model, key, prompt, **kwargs):
        calls.append(json.loads(prompt)[0])
        return json.dumps({'items': [{'id': 'prep-1', 'text': 'Sam asked for the fund memo last week.',
                                      'draft': '', 'decline': ''}]})
    item = _prep_item(event={'summary': 'Walk'})  # attendee sam@example.com, not the Sam on +1555…
    text = notification.compose('proactive', [item], now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc), llm=writer)
    supplied = [row['what'] for row in calls[0]['open_items']]
    assert supplied == ['Send the fund memo', 'Return the borrowed book']
    assert 'lease' not in json.dumps(calls) and 'lease' not in text and 'fund memo' in text


def test_reminder_after_failed_batch_writer_spends_no_second_attempt_pair(isolated, monkeypatch):
    monkeypatch.setattr(notification, '_prep_threads', lambda *a: [])
    calls = []
    def writer(provider, model, key, prompt, **kwargs):
        calls.append(prompt)
        return 'not json'
    items = [{'kind': 'commitment', 'key': 'c', 'decision_id': 'c1', 'title': 'Send deck', 'person': 'Ana',
              'deadline': '2026-09-17T00:00:00Z'},
             _prep_item(person_facts={'sam': 'Sam shared fund plans on September 10.'})]
    text = notification.compose('proactive', items, now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc), llm=writer)
    assert len(calls) == 2  # the job's one writer budget, spent by the batch writer
    assert text == "You're meeting Sam (Partner at Example) in about 30 minutes. Want the full prep on Sam?"


def test_reminder_offer_does_not_carry_unchecked_scanner_task(isolated, monkeypatch):
    monkeypatch.setattr(notification, '_prep_threads', lambda *a: [])
    item = _prep_item(open_loop='send the already delivered deck',
                      detail='starts in ~30 min · Sam · open with them: send the already delivered deck')
    text = notification.compose('proactive', [item], now=datetime(2026, 9, 17, 12, tzinfo=timezone.utc),
                                llm=lambda *a, **k: pytest.fail('no writer'))
    assert 'deck' not in text
    doc = json.loads((isolated / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    offer = next(e['offer'] for e in doc['effects'] if e['kind'] == 'pending_offer')
    assert 'deck' not in json.dumps(offer) and offer['detail'] == 'starts in ~30 min · Sam'
