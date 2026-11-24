"""A due owner timer bound to a route that is no longer current ends once, without its text."""
import time

from test_owner_reminder_tick import timer as timer, rec as rec  # noqa: PLC0414, F401 — pytest fixtures


def test_due_timer_on_an_old_route_cancels_once_with_a_content_free_notice(timer):
    rec, store, create, sent, identity = timer
    row = create(minutes_ago=16)
    identity['route_id'] = 'imessage:new~line'
    for _ in range(3):
        rec._owner_reminder_tick()
        rec.OUTBOX.drain()
    saved = next(item for item in store.current() if item['id'] == row['id'])
    assert saved['status'] == 'canceled' and saved['delivery_outcome'] == 'superseded'
    assert saved['delivery_notice_outcome'] == 'superseded'
    assert [item['id'] for item in store.due()] == []
    assert [args[:2] for args in sent] == [(rec.OWNER_REMINDER_ORPHANED_NOTICE, 'photon')]
    assert 'Check test results' not in rec.OWNER_REMINDER_ORPHANED_NOTICE


def test_route_change_inside_the_grace_still_waits(timer):
    rec, store, create, sent, identity = timer
    row = create(minutes_ago=1)
    identity['route_id'] = 'imessage:new~line'
    rec._owner_reminder_tick()
    assert not sent
    assert next(item for item in store.current() if item['id'] == row['id'])['status'] == 'scheduled'


def test_new_owner_status_notices_wait_through_quiet_hours_then_send_once(timer, monkeypatch):
    from types import SimpleNamespace
    rec, store, create, sent, identity = timer
    quiet = [True]
    policy = SimpleNamespace(_in_quiet_hours=lambda now: quiet[0], _prefs=SimpleNamespace(
        load_explicit=lambda: {}, snooze_active=lambda **kw: False, effective_nudge_budget=lambda value: 5))
    monkeypatch.setattr(rec, '_discovery_interruption_policy', lambda: policy)
    rec.OUTBOX.HOOKS['delivery_ready'] = rec._history_discovery_delivery_ready
    create(minutes_ago=16)
    identity['route_id'] = 'imessage:new~line'
    rec._owner_reminder_tick()
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={'rows': []}) as doc:
        rows = rec.OUTBOX._rows(doc)
        brief = {'id': 'brief-row', 'status': 'failed', 'last_error': rec.OUTBOX.UNCONFIRMED_DETAIL,
                 'payload': {'label': 'cron:sotto-evening-brief', 'target': 'photon'}}
        rec.OUTBOX._unconfirmed_close(rows, dict(brief, status='pending'), time.time(), '2026-11-09')
        for row in rows:
            row['created_at'] = 0  # composed long ago: a nudge-age expiry must not end them
        doc['rows'] = rows
    for _ in range(2):
        rec.OUTBOX.drain()
    assert not sent
    quiet[0] = False
    for _ in range(3):
        rec.OUTBOX.drain()
        rec._owner_reminder_tick()
    assert sorted(args[0] for args in sent) == sorted([rec.OWNER_REMINDER_ORPHANED_NOTICE,
                                                       rec.OUTBOX.unconfirmed_notice_body('evening')])
