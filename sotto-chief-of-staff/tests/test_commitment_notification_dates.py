"""A deadline to send an invite does not establish when the meeting takes place."""
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pytest

import compose_notification as notification
import personal_context
import proactive_scan


@pytest.mark.parametrize(('channel', 'related', 'allowed'), [
    ('email', False, True), ('email', True, True), ('email', True, False),
    ('imessage', False, True), ('whatsapp', False, True),
])
def test_due_commitment_uses_bound_conversation_and_omits_unrelated_calendar(
        tmp_path, monkeypatch, channel, related, allowed):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DELIVERY_RUN_ID', raising=False)
    monkeypatch.setattr(notification, 'allowed', lambda source: allowed if source == 'calendar' else True)
    monkeypatch.setattr(notification.gemini, 'provider_key', lambda _: 'fixture')
    monkeypatch.setattr(notification.delivery_effects, '_loops', lambda: {})
    monkeypatch.setattr(notification.style_apply, 'apply', lambda _: {})
    monkeypatch.setattr(notification, '_read_json_script', lambda *a: {})

    zone = ZoneInfo('America/Los_Angeles')
    now = datetime(2026, 11, 16, 7, 3, tzinfo=zone)
    peer = {'email': 'avery@example.test', 'imessage': '+15555550144',
            'whatsapp': '15555550144@s.whatsapp.net'}[channel]
    thread_id = 'exact-thread' if channel == 'email' else ''
    native_field = {'email': 'from', 'imessage': 'handle', 'whatsapp': 'contact_jid'}[channel]
    binding = ({'threadId': thread_id} if thread_id else {'chat_guid': 'native-chat'})
    row = {'anchor_key': 'fixture-ask', 'name': 'Avery', 'identifier': peer, 'channel': channel,
           'deadline': '2026-11-16', 'what': 'Send Avery an invite for the 10am call', 'group_id': '',
           'direction': 'you_owe', 'thread_id': thread_id}
    thread = {'source': channel, native_field: peer, 'is_group_chat': False, **binding}
    confirmed_day = {**thread, 'text': 'Friday Nov 20 at 11am PT works.',
                     'timestamp': '2026-11-15T16:00:00-08:00'}
    exact = {**thread, 'text': 'Yes, 10am PT instead works.', 'timestamp': '2026-11-16T06:35:00-08:00'}
    unrelated = {**thread, 'is_group_chat': True, 'chat_guid': 'unrelated-group', 'threadId': 'unrelated-thread',
                 'text': 'Different conversation at 10 on Monday.', 'timestamp': '2026-11-15T17:00:00-08:00'}
    monkeypatch.setattr(notification, '_loop_ledger', lambda _: ({'you_owe': [row], 'waiting_on_them': []}, {}))
    monkeypatch.setattr(personal_context, 'conversation_snapshot',
                        lambda: [(confirmed_day, '', ''), (exact, '', ''), (unrelated, '', '')])
    calendar_calls = []
    event = {'summary': 'Call with Avery', 'start': '2026-11-20T10:00:00-08:00',
             'status': 'confirmed', 'threadId': 'exact-thread'}
    observed = [{'summary': 'Commute', 'start': '2026-11-16T10:00:00-08:00'},
                {**event, 'summary': 'Other call with Avery', 'threadId': 'other-thread'},
                {**event, 'summary': 'Cancelled call', 'status': 'cancelled'}]
    if related:
        observed.append(event)

    def calendar():
        calendar_calls.append('calendar')
        return {'all_calendars_complete': False, 'events': observed}

    monkeypatch.setattr(notification, '_calendar', calendar)
    nudges = proactive_scan.scan([], [{**row, 'id': 'saved-task', 'title': row['what']}], {}, '', now)['nudges']
    assert [n.get('action_due_date') for n in nudges] == ['2026-11-16']

    def writer(provider, model, key, prompt, **kwargs):
        evidence, = json.loads(prompt)
        assert evidence['action_due_date'] == '2026-11-16'
        assert evidence['as_of_date'] == '2026-11-16'
        assert [entry['text'] for entry in evidence['current_conversation'] if entry['text']] == [
            confirmed_day['text'], exact['text']]
        assert 'Different conversation' not in json.dumps(evidence)
        assert all(text not in json.dumps(evidence) for text in ['Commute', 'Other call', 'Cancelled call'])
        if related and allowed:
            assert evidence['calendar']['events'] == [{
                'summary': 'Call with Avery', 'start': event['start'], 'status': 'confirmed'}]
            return json.dumps({'items': []})
        assert 'calendar' not in evidence
        assert 'a meeting date or a calendar conflict' in kwargs['system']
        return json.dumps({'items': [{'id': evidence['id'], 'text': "Send Avery the invite for Friday's 10am call.",
                                      'draft': '', 'decline': ''}]})

    expected = "NO_NUDGES" if related and allowed else "Send Avery the invite for Friday's 10am call."
    assert notification.compose('proactive', nudges, now=now, llm=writer) == expected
    assert calendar_calls == (["calendar"] if channel == "email" and allowed else [])



def test_competing_direct_chats_do_not_get_joined_by_the_same_peer():
    direct = {'source': 'imessage', 'handle': '+15555550144', 'is_group_chat': False}
    rows = [({**direct, 'chat_id': thread}, '', '') for thread in ('first-direct', 'other-direct')]
    assert personal_context.direct_conversation_event('imessage', direct['handle'], rows, lambda _: True) == {}



def test_ambiguous_or_unbound_calendar_cannot_confirm_commitment():
    event = {'threadId': 'exact-thread', 'start': '2026-11-20T10:00:00-08:00'}
    item = {'thread_id': 'exact-thread', 'channel': 'email'}
    assert personal_context.commitment_calendar({'events': [event, {**event, 'start': '2026-11-21'}]}, item) is None
    assert personal_context.commitment_calendar({'events': [event]}, {**item, 'thread_id': ''}) is None
