"""Failure sequences at the boundary between decisions and accepted delivery."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '_shared/lib'))
sys.path.insert(0, str(ROOT / '_shared/scripts'))
import delivery_effects as effects
import pending_offer
import schedule_wakeup
import work_queue


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a' * 32)


def test_staging_merges_producers_and_retry_keeps_one_question(tmp_path):
    offer = pending_offer.set_offer('meeting_prep', 'Pull full prep for Maya?')
    pending_offer.set_offer('meeting_prep', 'Pull full prep for Maya?')
    effects.stage([{'kind': 'intention', 'id': 'test'}], ['decision'])
    doc = json.loads((tmp_path / 'events' / ('delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert not pending_offer.get_offer()
    receipt = {'message_id': 'provider-1', 'target': 'test', 'accepted_at': 1234}
    offer_effects = [e for e in doc['effects'] if e['kind'] == 'pending_offer']
    assert effects.finalize(offer_effects, receipt)
    delivered = pending_offer.get_offer()
    assert delivered['question'] == offer['question'] and delivered['message_id'] == 'provider-1'
    assert pending_offer.clear_offer(expected=delivered)
    assert effects.finalize(offer_effects, receipt)
    assert not pending_offer.get_offer()  # ACK replay cannot resurrect an answered question


def test_two_delivered_offers_require_selection(monkeypatch):
    first = pending_offer.set_offer('meeting_prep', 'Pull prep for Maya?')
    pending_offer.activate_offer(first, {'message_id': 'm1', 'accepted_at': 1})
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'b' * 32)
    second = pending_offer.set_offer('meeting_prep', 'Pull prep for Ben?')
    pending_offer.activate_offer(second, {'message_id': 'm2', 'accepted_at': 2})
    assert pending_offer.get_offer()['ambiguous']
    assert pending_offer.dismiss_reply('done')['action'] == 'clarify'
    assert pending_offer.get_offer(message_id='m1')['question'] == 'Pull prep for Maya?'


def test_offer_bound_action_cannot_clear_a_new_question(monkeypatch):
    import google_action
    monkeypatch.delenv('SOTTO_UNATTENDED', raising=False)
    monkeypatch.setattr(google_action, '_pending_offer', lambda: pending_offer)
    first = pending_offer.set_offer('commitment', 'Save this draft?',
                                    payload_sha256=pending_offer.payload_hash(b'Approved body'),
                                    action='gmail-draft')
    pending_offer.activate_offer(first, {'message_id': 'm1', 'accepted_at': 1})
    def local_action():
        monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'b' * 32)
        second = pending_offer.set_offer('meeting_prep', 'Pull prep for Maya?')
        pending_offer.activate_offer(second, {'message_id': 'm2', 'accepted_at': 2})
        return {'status': 'drafted'}
    result, refused = google_action._gated('gmail-draft', {}, 'Approved body', local_action, True,
                                           first['offer_id'])
    assert result['status'] == 'drafted' and not refused
    assert pending_offer.get_offer()['question'] == 'Pull prep for Maya?'


def test_intention_and_seen_wait_for_ack_and_finalize_is_idempotent():
    intention = schedule_wakeup.create((datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
                                       'Check the waiver')
    date = datetime.now(timezone.utc).date().isoformat()
    key = 'intention:' + intention['id']
    with effects.proactive_state(date) as state:
        state['pending'][key] = {'run_id': 'a' * 32, 'valid_until': 9999999999}
    staged = [{'kind': 'intention', 'id': intention['id']},
              {'kind': 'proactive_seen', 'date': date, 'key': key, 'run_id': 'a' * 32}]
    assert not effects.finalize(staged, {})
    assert schedule_wakeup.current()[0]['status'] == 'scheduled'
    assert effects.finalize(staged, {'accepted_at': 1234})
    assert effects.finalize(staged, {'accepted_at': 1234})
    assert schedule_wakeup.current()[0]['status'] == 'fired'
    with effects.proactive_state(date) as state:
        assert state['nudged'] == [key] and not state['pending']


@pytest.mark.parametrize('mode', ['offer', 'full'])
def test_meeting_prep_suppression_requires_accepted_exact_occurrence(tmp_path, mode):
    start = '2026-09-21T17:30:00Z'
    effect = {'kind': 'meeting_prep_delivered', 'mode': mode,
              'calendar_event_id': 'event-1', 'calendar_start': start}
    assert not effects.finalize([effect], {})
    date = '2026-09-21'
    with effects.proactive_state(date) as state:
        assert state.get('prep_deliveries', []) == []
    assert effects.finalize([effect], {'accepted_at': 1234})
    with effects.proactive_state(date) as state:
        assert state['prep_deliveries'] == [effects.meeting_occurrence('event-1', start)]


def test_meeting_prep_effect_uses_user_local_day_across_utc_midnight(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    start = '2026-09-22T00:30:00Z'  # Sep 21 afternoon in Los Angeles
    effect = {'kind': 'meeting_prep_delivered', 'mode': 'full',
              'calendar_event_id': 'event-west', 'calendar_start': start}
    assert effects.finalize([effect], {'accepted_at': 1234})
    with effects.proactive_state('2026-09-21') as state:
        assert state['prep_deliveries'] == [effects.meeting_occurrence('event-west', start)]
    with effects.proactive_state('2026-09-22') as state:
        assert state.get('prep_deliveries', []) == []


def test_calendar_absence_only_means_cancelled_with_fresh_complete_same_day(tmp_path):
    now = datetime.now(timezone.utc)
    start = now + timedelta(minutes=30)
    candidate = [{'kind': 'eligibility', 'source': 'calendar', 'calendar_event_id': 'e1',
                  'calendar_start': start.isoformat()}]
    cache = {'status': 'ok', 'complete': True, 'projection': 'day',
             'date': start.date().isoformat(), 'generated_at': now.isoformat(), 'events': []}
    path = tmp_path / 'cache/calendar_today.json'
    path.parent.mkdir()
    def write(**changes):
        path.write_text(json.dumps({**cache, **changes}))
    write()
    assert not effects.valid(candidate, now.timestamp())
    write(complete=False)
    assert effects.valid(candidate, now.timestamp())
    write(generated_at=(now - timedelta(hours=2)).isoformat())
    assert effects.valid(candidate, now.timestamp())
    write(date=(start - timedelta(days=1)).date().isoformat())
    assert effects.valid(candidate, now.timestamp())
    write(events=[{'id': 'e1', 'start': start.isoformat()}])
    assert effects.valid(candidate, now.timestamp())
    write(events=[{'id': 'e1', 'start': (start + timedelta(minutes=10)).isoformat()}])
    assert not effects.valid(candidate, now.timestamp())
    candidate[0]['calendar_observed_at'] = (now + timedelta(seconds=10)).isoformat()
    assert effects.valid(candidate, now.timestamp())  # older cache cannot cancel newly gathered data


def test_source_revocation_and_newer_reply_supersede_cached_message(tmp_path):
    now = datetime.now(timezone.utc)
    event = {'source': 'imessage', 'handle': '+15551234567', 'thread_id': 'thread-1',
             'timestamp': (now - timedelta(minutes=20)).isoformat()}
    descriptor = effects.for_bundle({'events': [{'event': event}]})['effects']
    path = tmp_path / 'events/queue.jsonl'
    path.parent.mkdir(exist_ok=True)
    outgoing = {**event, 'timestamp': (now - timedelta(minutes=10)).isoformat(), 'is_from_me': True}
    path.write_text(json.dumps({'verdict_class': 'signal', 'event': outgoing}) + '\n')
    assert not effects.valid(descriptor, now.timestamp())
    outgoing['thread_id'] = 'unrelated-room'
    path.write_text(json.dumps({'verdict_class': 'signal', 'event': outgoing}) + '\n')
    assert effects.valid(descriptor, now.timestamp())
    import source_context
    source_context.record_bridge_status({'source_status': {'imessage': 'disabled'}})
    assert not effects.valid([{'kind': 'source_permissions', 'sources': ['imessage']}])


def test_partial_overlap_bundles_transfer_each_item_only_once(tmp_path):
    def put(keys):
        return work_queue.enqueue(tmp_path, 'event', {'bundle': {'events': [{'id': k} for k in keys]}},
                                  item_keys=keys)
    first = put(['a', 'b'])
    second = put(['b', 'c'])
    assert first != second
    assert work_queue.get(tmp_path, second)['payload']['bundle']['events'] == [{'id': 'c'}]
    assert put(['c', 'a']) in {first, second}
    assert work_queue.status(tmp_path)['counts'] == {'pending': 2}


def test_thread_context_orders_mail_headers_and_iso_by_actual_time(tmp_path):
    from personal_context import current_conversation
    rows = [
        {'source': 'email', 'threadId': 'mail-1', 'date': 'Mon, 7 Sep 2026 10:00:00 -0700',
         'from': 'maya@example.com', 'text': 'Please review'},
        {'source': 'email', 'threadId': 'mail-1', 'date': '2026-09-07T17:05:00Z',
         'to': 'maya@example.com', 'is_from_me': True, 'text': 'Done'},
        {'source': 'email', 'threadId': 'unrelated', 'date': '2026-09-07T17:06:00Z', 'text': 'Other'},
    ]
    path = tmp_path / 'events/queue.jsonl'
    path.parent.mkdir()
    path.write_text('\n'.join(json.dumps({'event': row}) for row in reversed(rows)))
    messages = current_conversation(rows[0], now=datetime(2026, 9, 7, 18, tzinfo=timezone.utc))
    assert [m['text'] for m in messages] == ['Please review', 'Done']


def test_expired_lease_does_not_restart_past_deadline_and_health_is_readonly(tmp_path):
    empty = tmp_path / 'missing'
    assert work_queue.status(empty) == {'counts': {}, 'oldest_due': None}
    assert not empty.exists()
    job = work_queue.enqueue(tmp_path, 'run', {}, not_before=100, valid_until=200)
    assert work_queue.claim(tmp_path, 'worker', now=110)['id'] == job
    assert work_queue.claim(tmp_path, 'restart', now=300) is None
    assert work_queue.get(tmp_path, job)['status'] == 'expired'


def test_terminal_key_needs_explicit_new_admission_and_gets_a_fresh_bounded_budget(tmp_path):
    job = work_queue.enqueue(tmp_path, 'run', {'generation': 1}, key='stable')
    with work_queue._db(tmp_path) as db:
        db.execute("UPDATE jobs SET status='failed',attempts=?,finished=? WHERE id=?",
                   (work_queue.MAX_ATTEMPTS, time.time(), job))
    assert work_queue.get(tmp_path, job)['status'] == 'failed'
    with pytest.raises(RuntimeError, match='terminal'):
        work_queue.enqueue(tmp_path, 'run', {'generation': 2}, key='stable')
    assert work_queue.get(tmp_path, job)['attempts'] == work_queue.MAX_ATTEMPTS

    assert work_queue.enqueue(tmp_path, 'run', {'generation': 2}, key='stable',
                              retry_terminal=True) == job
    fresh = work_queue.get(tmp_path, job)
    assert fresh['status'] == 'pending' and fresh['attempts'] == 0
    assert fresh['payload'] == {'generation': 2}


def test_terminal_item_ownership_can_be_replaced_only_by_explicit_ingress_retry(tmp_path):
    payload = {'bundle': {'events': [{'id': 'a'}]}}
    job = work_queue.enqueue(tmp_path, 'event', payload, item_keys=['a'])
    # Expiry is enough to make the old ownership terminal without consuming more model attempts.
    with work_queue._db(tmp_path) as db:
        db.execute("UPDATE jobs SET status='expired',finished=101 WHERE id=?", (job,))
    with pytest.raises(RuntimeError, match='terminal'):
        work_queue.enqueue(tmp_path, 'event', payload, item_keys=['a'])
    assert work_queue.enqueue(tmp_path, 'event', payload, item_keys=['a'], retry_terminal=True) == job
    assert work_queue.get(tmp_path, job)['status'] == 'pending'


def test_pending_unsolicited_nudge_rechecks_zero_and_freshness_but_held_survives(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_NUDGE_BUDGET', '4')
    now = datetime.now(timezone.utc)
    fresh = {'event': {'source': 'imessage', 'timestamp': now.isoformat()}}
    descriptor = effects.for_bundle({'events': [fresh]})
    assert effects.valid(descriptor['effects'], now.timestamp() + 29 * 60)
    assert not effects.valid(descriptor['effects'], now.timestamp() + 31 * 60)
    urgent = effects.for_bundle({'events': [{**fresh,
        'relevance_deadline': (now + timedelta(minutes=5)).isoformat()}]})
    assert effects.valid(urgent['effects'], now.timestamp() + 4 * 60)
    assert not effects.valid(urgent['effects'], now.timestamp() + 6 * 60)
    held = effects.for_bundle({'events': [{**fresh, 'deferred_class': 'meeting_hold'}]})
    assert effects.valid(held['effects'], now.timestamp() + 5 * 3600)
    (tmp_path / 'preferences.json').write_text(json.dumps({'explicit': {'nudge_budget': '0'}}))
    assert not effects.valid(held['effects'], now.timestamp() + 5 * 3600)


def test_class_deadlines_are_not_shortened_to_the_generic_message_window():
    now = datetime.now(timezone.utc)
    event = {'source': 'phonecalls', 'timestamp': now.isoformat()}
    missed = effects.for_bundle({'events': [{'event': event, 'class': 'missed_call'}]})
    assert missed['valid_until'] is None
    assert effects.valid(missed['effects'], now.timestamp() + 45 * 60)

    meeting = now + timedelta(hours=2)
    changed = effects.for_bundle({'events': [{
        'event': {'source': 'calendar_change', 'timestamp': now.isoformat(),
                  'start': meeting.isoformat()},
        'class': 'calendar_change',
    }]})
    assert changed['valid_until'] == meeting.timestamp()
    assert effects.valid(changed['effects'], now.timestamp() + 45 * 60)
    assert not effects.valid(changed['effects'], meeting.timestamp())


def test_user_requested_promotion_survives_zero_but_ordinary_bundle_does_not(tmp_path):
    now = datetime.now(timezone.utc)
    row = {'event': {'source': 'imessage', 'timestamp': now.isoformat()}}
    requested = effects.for_bundle({'_user_requested_delivery': True, 'events': [row]})
    ordinary = effects.for_bundle({'events': [row]})
    (tmp_path / 'preferences.json').write_text(json.dumps({'explicit': {'nudge_budget': '0'}}))
    assert effects.valid(requested['effects'], now.timestamp() + 60)
    assert not effects.valid(ordinary['effects'], now.timestamp() + 60)


def test_held_request_rechecks_exact_completion_at_delivery(monkeypatch):
    rows = {'a': {'anchor_key': 'a', 'status': 'open', 'channel': 'email',
                  'source_refs': [{'sourceType': 'email', 'sourceId': 'original-ask'}]}}
    monkeypatch.setattr(effects, '_loops', lambda: rows)
    bundle = {'events': [{'class': 'scheduling_ask', 'event': {
        'source': 'email', 'id': 'original-ask', 'threadId': 'same-thread'}}]}
    staged = [{'kind': 'eligibility', 'source': 'gmail',
               'request_reference': effects.request_reference(bundle['events'][0]['event'])}]
    assert effects.valid(staged)
    rows['a']['status'] = 'resolved'
    assert not effects.valid(staged)
    # Same thread/person does not make a new request the old obligation.
    bundle['events'][0]['event']['id'] = 'new-ask'
    assert effects.valid([{**staged[0], 'request_reference': effects.request_reference(bundle['events'][0]['event'])}])
    # Nor does one completed task pay off a second task in the original message.
    rows['b'] = {**rows['a'], 'anchor_key': 'b', 'status': 'open'}
    assert effects.valid(staged)
    rows['b']['status'] = 'dismissed'
    assert not effects.valid(staged)


def test_local_request_uses_the_same_evidence_identity_as_extraction(monkeypatch):
    from render_local import message_evidence_id
    event = {'source': 'imessage', 'handle': '+15550000000', 'chat_guid': 'group-1',
             'is_from_me': False, 'text': 'Thursday at 3?', 'timestamp': '2026-09-21T12:00:00Z'}
    ref = effects.request_reference(event)
    assert ref['id'] == message_evidence_id(event, 'imessage')
    rows = {'a': {'status': 'resolved', 'source_refs': [{'sourceType': 'imessage', 'sourceId': ref['id']}]}}
    assert effects.request_closed(ref, rows)
    assert not effects.request_closed({**ref, 'source': 'whatsapp'}, rows)
    assert not effects.request_closed(effects.request_reference({**event, 'text': 'Another question'}), rows)


def test_unselected_bundle_event_does_not_gain_a_closure_gate(monkeypatch):
    monkeypatch.setattr(effects, '_loops', lambda: {'closed': {'status': 'dismissed',
        'source_refs': [{'sourceType': 'email', 'sourceId': 'old-message'}]}})
    legacy = {'kind': 'eligibility', 'source': 'gmail', 'event': {'source': 'gmail', 'id': 'old-message'}}
    assert effects.valid([legacy])
    assert effects.request_reference({'source': 'proactive', 'channel': 'email', 'id': 'old-message'}) is None
