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
