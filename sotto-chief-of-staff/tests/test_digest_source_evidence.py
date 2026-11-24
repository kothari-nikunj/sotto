"""Midday digest source binding, grounded output and delivery eligibility."""
import json
from datetime import timedelta

import pytest

from digest_fixture import NOW, _entry, _excerpt, _proactive_entry, _write_queue

import digest_check as dc


def test_saved_task_reviews_exact_pre_cutoff_conversation(tmp_path, monkeypatch):
    import ledger_io

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DIGEST_MIN', '0')
    original = _entry(701, sender='Alex', minutes_ago=90, text='Can we find a time?')
    offer = _entry(702, sender='Alex', cls='signal', minutes_ago=30, text='I can do 2 pm.')
    offer['event']['is_from_me'] = True
    other = _entry(703, sender='Alex', cls='signal', minutes_ago=25, text='Other group reply')
    other['event'].update(chat_guid='different-group', is_group_chat=True, is_from_me=True)
    future = _entry(704, sender='Alex', cls='signal', minutes_ago=-10, text='Future reply')
    future['event']['is_from_me'] = True
    saved = _proactive_entry('commitment', 'Alex — find a time', minutes_ago=55)
    saved['event'].update(channel='imessage', anchor_key='follow_up:exact', **{'from': original['event']['handle']})
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: [{
        'anchor_key': 'follow_up:exact', 'channel': 'imessage', 'status': 'open',
        'source_refs': [{'sourceType': 'imessage', 'sourceId': '701'}],
    }])
    _write_queue(tmp_path, [original, saved, offer, other, future])
    dc.write_stamp(NOW - timedelta(minutes=60))
    seen = []

    def review(rows):
        seen.extend(rows)
        return [{'id': row['id'], 'class': 'ignore', 'why': 'Waiting for confirmation'} for row in rows]

    monkeypatch.setattr(dc, 'review_conversations', review)
    assert dc.run_check(NOW) == {'deliver': False}
    assert len(seen) == 1
    assert [m['text'] for m in seen[0]['messages']] == ['Can we find a time?', 'I can do 2 pm.']


def test_digest_drops_a_cross_thread_claim_with_valid_message_index(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    left = _entry(711, sender='Alex', cls='actionable', text='Can you move the Launch call at 3:30?')
    right = _entry(712, sender='Alex', cls='actionable', text='The fellowship conflicts with the Hiring call at 9.')
    left['event']['chat_guid'], right['event']['chat_guid'] = 'launch-thread', 'hiring-thread'

    def review(rows):
        judgments = []
        for row in rows:
            source = row['messages'][0]['text']
            excerpt = 'The fellowship conflicts with the Launch call at 3:30.' if 'Launch' in source else source
            judgments.append({'id': row['id'], 'class': 'actionable', 'why': excerpt,
                              'message_index': 0, 'excerpt': excerpt})
        return judgments

    monkeypatch.setattr(dc, 'review_conversations', review)
    _write_queue(tmp_path, [left, right])
    result = dc.run_check(NOW)
    assert result['deliver']
    assert 'coverage_until' not in result
    assert [item['excerpt'] for item in result['items']] == [right['event']['text']]
    assert dc.read_stamp() is None


def test_group_task_keeps_new_sibling_ask_and_exact_delivery_route(tmp_path, monkeypatch):
    import delivery_effects
    import ledger_io

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    original = _entry(721, sender='Alex', minutes_ago=90, text='Can we meet?')
    offer = _entry(722, sender='Alex', cls='signal', minutes_ago=30, text='I can do 2 pm.')
    fresh = _entry(723, sender='Alex', cls='actionable', minutes_ago=20, text='Please send the deck first.')
    for entry in (original, offer, fresh):
        entry['event'].update(chat_guid='exact-group', is_group_chat=True)
    direct = _entry(724, sender='Alex', cls='signal', minutes_ago=15, text='Direct reply')
    saved = _proactive_entry('commitment', 'Alex — find a time', minutes_ago=55)
    saved['event'].update(channel='imessage', anchor_key='follow_up:exact', **{'from': original['event']['handle']})
    ledger = [{'anchor_key': 'follow_up:exact', 'channel': 'imessage', 'status': 'open',
               'source_refs': [{'sourceType': 'imessage', 'sourceId': '721'}]}]
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: ledger)
    _write_queue(tmp_path, [original, saved, offer, fresh, direct])
    dc.write_stamp(NOW - timedelta(minutes=60))

    def review(rows):
        assert len(rows) == 1
        assert [m['text'] for m in rows[0]['messages']] == [r['event']['text'] for r in (original, offer, fresh)]
        return [{'id': 0, 'class': 'actionable', 'why': 'The deck is a separate ask',
                 'message_index': 2, 'excerpt': fresh['event']['text']}]

    monkeypatch.setattr(dc, 'review_conversations', review)
    result = dc.run_check(NOW)
    assert result['items'][0]['excerpt'] == fresh['event']['text']
    effect = result['effects'][0]
    assert effect['request_reference'] == {'source': 'imessage', 'id': '723'}
    assert effect['event']['chat_guid'] == 'exact-group'
    ledger[0]['status'] = 'resolved'
    assert delivery_effects.valid(result['effects'], NOW.timestamp())
    later = _entry(725, sender='Alex', cls='signal', text='Sent the deck.')
    later['event'].update(chat_guid='exact-group', is_group_chat=True, is_from_me=True)
    _write_queue(tmp_path, [original, saved, offer, fresh, direct, later])
    assert not delivery_effects.valid(result['effects'], NOW.timestamp())


def test_unproved_muted_or_revoked_original_never_enters_digest(tmp_path, monkeypatch):
    import ledger_io
    import source_context

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DIGEST_MIN', '0')
    original = _entry(731, sender='Muted sender', minutes_ago=90, text='Private original')
    saved = _proactive_entry('commitment', 'Display alias — private original', minutes_ago=55)
    saved['event'].update(channel='imessage', anchor_key='follow_up:exact', **{'from': original['event']['handle']})
    ledger = [{'anchor_key': 'follow_up:exact', 'channel': 'imessage', 'status': 'open',
               'source_refs': [{'sourceType': 'imessage', 'sourceId': 'wrong'}]}]
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: ledger)
    _write_queue(tmp_path, [original, saved])
    monkeypatch.setattr(dc, 'review_conversations', lambda _: pytest.fail('private or unproved review context'))
    cutoff = NOW - timedelta(minutes=60)
    dc.write_stamp(cutoff)
    assert dc.run_check(NOW) == {'deliver': False}

    ledger[0]['source_refs'][0]['sourceId'] = '731'
    dc.write_stamp(cutoff)
    (tmp_path / 'preferences.json').write_text(json.dumps({'explicit': {'mute_people': ['Muted sender']}}))
    assert dc.run_check(NOW) == {'deliver': False}

    (tmp_path / 'preferences.json').write_text('{}')
    dc.write_stamp(cutoff)
    monkeypatch.setattr(source_context, 'allowed', lambda source: source != 'imessage')
    assert dc.run_check(NOW) == {'deliver': False}


def test_waiting_offer_preserves_original_saved_task_gate_until_delivery(tmp_path, monkeypatch):
    import delivery_effects
    import ledger_io

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DIGEST_MIN', '0')
    original = _entry(741, sender='Alex', minutes_ago=90, text='Can we meet?')
    offer = _entry(742, sender='Owner', cls='signal', minutes_ago=30, text="I can't do 3 pm, but I can do 4.")
    offer['event'].update(handle=original['event']['handle'], is_from_me=True)
    saved = _proactive_entry('commitment', 'Alex — find a time', minutes_ago=55)
    saved['event'].update(channel='imessage', anchor_key='follow_up:exact')
    ledger = [{'anchor_key': 'follow_up:exact', 'channel': 'imessage', 'status': 'open',
               'source_refs': [{'sourceType': 'imessage', 'sourceId': '741'}]}]
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: ledger)
    _write_queue(tmp_path, [original, saved, offer])
    dc.write_stamp(NOW - timedelta(minutes=60))
    monkeypatch.setattr(dc, 'review_conversations', lambda _: [{
        'id': 0, 'class': 'ambient', 'why': 'Waiting on the offer', 'status': 'waiting',
        'request_index': 0, 'message_index': 1, 'excerpt': 'do 3 pm',
    }])
    result = dc.run_check(NOW)
    assert result['items'][0]['excerpt'] == "I can't do 3 pm, but I can do 4."
    assert result['items'][0]['status'] == 'waiting'
    assert result['items'][0]['request_sender'] == 'Alex'
    anchor, route = result['effects']
    assert anchor['anchor_key'] == 'follow_up:exact' and anchor['loop_version']
    assert 'event' not in anchor
    assert route['request_reference'] == {'source': 'imessage', 'id': '741'}
    assert delivery_effects.valid(result['effects'], NOW.timestamp())
    ledger[0]['status'] = 'resolved'
    assert not delivery_effects.valid(result['effects'], NOW.timestamp())
    ledger[0]['status'] = 'open'
    reply = _entry(743, sender='Alex', text='4 works.')
    _write_queue(tmp_path, [original, saved, offer, reply])
    assert not delivery_effects.valid(result['effects'], NOW.timestamp())


@pytest.mark.parametrize('include_open', [False, True])
def test_closed_native_request_records_reviewed_silence(tmp_path, monkeypatch, include_open):
    import ledger_io

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    closed = _entry(751, sender='Alex', cls='actionable', text='Please send the deck.')
    opened = _entry(752, sender='Jordan', cls='actionable', text='Could you check the itinerary?')
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: [{
        'anchor_key': 'follow_up:deck', 'channel': 'imessage', 'status': 'resolved',
        'source_refs': [{'sourceType': 'imessage', 'sourceId': '751'}],
    }])
    calls = []

    def review(rows):
        calls.append(rows)
        return [{'id': row['id'], 'class': 'actionable', 'why': 'The model proposed this ask', **_excerpt(row)}
                for row in rows]

    monkeypatch.setattr(dc, 'review_conversations', review)
    _write_queue(tmp_path, [closed, opened] if include_open else [closed])

    result = dc.run_check(NOW)
    assert len(calls) == 1
    closed_id = dc._item_id(dc._conversation_key(closed), [closed])
    assert dc._accepted() == {closed_id}
    if include_open:
        assert result['coverage_until'] == NOW.strftime('%Y-%m-%dT%H:%M:%SZ')
        assert [item['excerpt'] for item in result['items']] == [opened['event']['text']]
        assert dc.read_stamp() is None
    else:
        assert result == {'deliver': False}
        assert dc.read_stamp() == NOW


@pytest.mark.parametrize('choose_open', [False, True])
def test_closed_selection_cannot_consume_same_thread_open_ask(tmp_path, monkeypatch, choose_open):
    import ledger_io

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    closed = _entry(801, sender='Alex', cls='actionable', minutes_ago=20, text='Please send the deck.')
    sibling = _entry(802, sender='Alex', cls='actionable', minutes_ago=10, text='Could you check the itinerary?')
    _write_queue(tmp_path, [closed, sibling])
    dc.write_stamp(NOW - timedelta(hours=1))
    before = dc.read_stamp()
    monkeypatch.setattr(ledger_io, 'load_entries', lambda: [{
        'anchor_key': 'follow_up:deck', 'channel': 'imessage', 'status': 'resolved',
        'source_refs': [{'sourceType': 'imessage', 'sourceId': '801'}],
    }])
    calls = []

    def review(rows):
        calls.append(rows)
        index = int(choose_open)
        return [{'id': 0, 'class': 'actionable', 'why': 'Selected this ask',
                 'message_index': index, 'excerpt': rows[0]['messages'][index]['text']}]

    monkeypatch.setattr(dc, 'review_conversations', review)
    result = dc.run_check(NOW)
    assert dc.read_stamp() == before
    assert dc._accepted() == set()
    assert len(calls) == 1 and len(calls[0]) == 1
    messages = calls[0][0]['messages']
    assert [m.get('request_closed', False) for m in messages] == [True, False]
    if choose_open:
        assert [item['excerpt'] for item in result['items']] == [sibling['event']['text']]
        assert result['effects'][0]['request_reference'] == {'source': 'imessage', 'id': '802'}
        assert result['coverage_until'] == NOW.strftime('%Y-%m-%dT%H:%M:%SZ')
    else:
        assert result['deliver'] is False and result['retryable'] is True


@pytest.mark.parametrize('event,queue_sender,request_sender', [
    ({'source': 'whatsapp', 'contact_jid': '12345@g.us', 'sender_jid': '15551234567@s.whatsapp.net',
      'chat_guid': '12345@g.us', 'partner_name': 'Cedar Planning', 'is_group_chat': True},
     'Cedar Planning', None),
    ({'source': 'whatsapp', 'chat_guid': '12345@g.us', 'sender_jid': '15551234567@s.whatsapp.net',
      'partner_name': 'Cedar Planning'}, 'Cedar Planning', None),
    ({'source': 'gmail', 'threadId': 'fixture-thread', 'from': 'person@example.com'},
     'person@example.com', 'person@example.com'),
    ({'source': 'gmail', 'threadId': 'fixture-thread', 'from': 'Alex Finch <person@example.com>'},
     'Alex Finch', 'Alex Finch'),
])
def test_waiting_requester_uses_only_direct_sender_labels(tmp_path, monkeypatch, event, queue_sender,
                                                         request_sender):
    import triage_event as triage

    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DIGEST_MIN', '0')
    request = {**event, 'rowid': 751, 'id': '751', 'text': 'Can we meet?'}
    response = {**event, 'rowid': 752, 'id': '752', 'text': 'I can do 4.', 'is_from_me': True}
    # The production queue writer stores the resolver's label. For an unrecognized
    # WhatsApp member this is the group name, even when the member's JID is present.
    rows = []
    sender, _ = triage._resolve_sender(request, {})
    assert sender == queue_sender
    triage._append_queue('actionable', sender, request, pending=rows)
    triage._append_queue('signal', 'Owner', response, pending=rows)
    rows[0]['ts'] = (NOW - timedelta(minutes=60)).isoformat()
    rows[1]['ts'] = (NOW - timedelta(minutes=30)).isoformat()
    _write_queue(tmp_path, rows)
    monkeypatch.setattr(dc, 'review_conversations', lambda _: [{
        'id': 0, 'class': 'ambient', 'why': 'Waiting on the offer', 'status': 'waiting',
        'request_index': 0, 'message_index': 1, 'excerpt': 'I can do 4.',
    }])
    item, = dc.run_check(NOW)['items']
    assert item.get('request_sender') == request_sender
