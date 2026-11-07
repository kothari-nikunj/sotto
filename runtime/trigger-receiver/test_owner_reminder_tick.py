"""Actual heartbeat/outbox delivery of admitted owner timers; no provider or model."""
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from test_proactive_procedure import rec as rec, PACK  # noqa: PLC0414 — pytest fixture export
from test_outbox import _load_outbox


@pytest.fixture
def timer(rec, monkeypatch):
    identity = {'tenant_id': 'tenant', 'channel': 'photon', 'account_id': 'account',
        'owner_id': '+15550000001', 'line_key': 'line', 'target': 'photon',
        'route_mode': 'fixed', 'route_id': 'imessage:chat~line', 'project_fingerprint': 'a' * 64}
    rec._actual_owner_timer_authority = rec._owner_timer_authority
    monkeypatch.setattr(rec, '_owner_timer_authority', lambda: (identity, identity['route_id']))
    monkeypatch.setattr(rec, '_activation_tick', lambda: None)
    monkeypatch.setattr(rec, '_selected_fixed_profile', lambda: False)
    monkeypatch.setattr(rec, '_background_context_tick', lambda: None)
    monkeypatch.setattr(rec, '_prepare_briefs', lambda: None)
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda: [])
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'photon')
    sent = []
    rec.OUTBOX.HOOKS.update(send=lambda *args, **kw: (sent.append(args) or
        (True, '', {'acceptance': 'accepted', 'message_id': 'accepted'})),
        delivery_ready=lambda payload: True, brief_gate=lambda *args: rec.OUTBOX.GATE_SEND,
        gallery_receipt=lambda key: None)
    store = rec.RUNTIME_OWNER_REMINDER._writer()
    def create(*, minutes_ago=1, **changes):
        due = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
        args = {'due': due, 'action': 'Check test results'}
        event = hashlib.sha256(str(len(store.current())).encode()).hexdigest()
        action, digest = rec.RUNTIME_OWNER_REMINDER_TOOL._identity(event, args)
        proof = {'domain': 'sotto.owner-reminder-tool.v1', 'event_id': event,
            'action_id': action, 'args_hash': digest, 'args': args, 'payload_hash': 'b'*64,
            'text_hash': 'c'*64, 'identity_digest': hashlib.sha256(json.dumps(identity,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
            'provider_thread_id': identity['route_id'], 'admitted_at_ms': 1780000000000}
        proof.update(changes)
        return store.create_tool_admitted(due, args['action'], proof)[1]
    return rec, store, create, sent, identity


def test_heartbeat_sends_due_owner_timer_once_without_proactive_gates(timer, monkeypatch):
    rec, store, create, sent, _ = timer
    create()
    # No source gathering, model, or proactive cadence consultation is needed.
    monkeypatch.setattr(rec, '_delivery_channel_ready', lambda *_: False)
    rec._cron_tick()
    rec._cron_tick()
    assert len(sent) == 1
    assert sent[0][:2] == ('Check test results', 'photon')
    assert store.current()[0]['status'] == 'fired'


@pytest.mark.parametrize('change', [{'domain': 'legacy'}, {'identity_digest': 'd'*64},
    {'provider_thread_id': 'imessage:other~line'}, {'args_hash': 'e'*64}])
def test_invalid_proof_is_neither_sent_nor_returned_to_proactive(timer, change, monkeypatch):
    rec, store, create, sent, _ = timer
    create(**change)
    rec._cron_tick()
    assert not sent and store.current()[0]['status'] == 'scheduled'
    monkeypatch.syspath_prepend(str(PACK / '_shared/scripts'))
    spec = importlib.util.spec_from_file_location('timer_proactive', PACK / 'proactive/scripts/proactive_scan.py')
    scanner = importlib.util.module_from_spec(spec); spec.loader.exec_module(scanner)
    assert scanner._intention_candidates(datetime.now(timezone.utc), continuity=[]) == []


def test_cancel_or_changed_route_before_outbox_contact_withholds(timer):
    rec, store, create, sent, identity = timer
    row = create()
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: False
    rec._cron_tick()
    assert not sent
    store.transition(row['id'], 'canceled')
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: True
    rec.OUTBOX.drain()
    assert not sent
    row = create()
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: False
    rec._cron_tick()
    identity['route_id'] = 'imessage:other~line'
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: True
    rec.OUTBOX.drain()
    assert not sent


def test_unknown_across_restart_and_late_acceptance_never_resends(timer):
    rec, store, create, sent, _ = timer
    create()
    rec.OUTBOX.HOOKS['send'] = lambda *args, **kw: (sent.append(args) or
        (False, 'response lost', {'acceptance': 'unknown'}))
    rec._cron_tick()
    restarted = _load_outbox('timer_restarted')
    restarted.HOOKS.update(rec.OUTBOX.HOOKS)
    rec.OUTBOX = restarted
    rec._cron_tick()
    assert len(sent) == 1 and store.current()[0]['status'] == 'scheduled'
    restarted.HOOKS['gallery_receipt'] = lambda key: {'acceptance': 'accepted', 'message_id': 'late'}
    with restarted.HOOKS['json_transaction'](restarted.path(), default={}) as doc:
        doc['rows'][0]['next_at'] = 0
    restarted.drain()
    assert len(sent) == 1 and store.current()[0]['status'] == 'fired'


def test_old_proactive_reservation_keeps_unknown_delivery_ownership(timer):
    rec, store, create, sent, _ = timer
    row = create()
    path = Path(rec.DATA) / 'proactive/2026-10-13.json'
    path.parent.mkdir(); path.write_text(json.dumps({'pending': {
        'intention:' + row['id']: {'run_id': 'old', 'valid_until': 0}}}))
    rec._cron_tick()
    assert not sent
    payload = {'label': 'cron:sotto-proactive', 'effects': [{'kind': 'intention', 'id': row['id']}]}
    assert rec._delivery_valid(payload) is False


def test_legacy_and_conditional_intentions_keep_proactive_lane(timer, monkeypatch):
    rec, store, create, sent, _ = timer
    row = create()
    legacy = dict(row, id='int_' + 'f'*32, created_by='legacy', owner_create={})
    conditional = dict(row, id='int_'+'e'*32, anchor_key='loop:one', condition_binding={
        'anchor_key': 'loop:one', 'loop_identity': 'f'*64, 'sources': ['gmail']})
    args = {**row['owner_create']['args'], 'operation': 'conditional', 'condition': 'if no reply',
        'anchor_key': 'loop:one', 'loop_version': 'a'*64, 'loop_identity': 'f'*64, 'source_revision': 'b'*64}
    action, digest = rec.RUNTIME_OWNER_REMINDER_TOOL._identity(row['owner_create']['event_id'], args)
    conditional['owner_create'] = {**row['owner_create'], 'args': args, 'args_hash': digest, 'action_id': action}
    assert not store.owner_timer(legacy) and not store.owner_timer(conditional)
    with store._lock():
        store._write_row(dict(row, status='canceled'))
        store._write_row(legacy)
        store._write_row(conditional)
    rec._cron_tick()
    assert not sent
    assert rec._owner_timer_delivery_valid({'label': 'cron:proactive', 'effects': [
        {'kind': 'intention', 'id': legacy['id']}, {'kind': 'intention', 'id': conditional['id']}]})


def test_pending_old_outbox_unknown_is_not_replayed_by_new_lane(timer):
    rec, store, create, sent, _ = timer
    row = create()
    box = rec.OUTBOX
    import time
    box._enqueue('old-proactive', box.KIND_NUDGE, {'label': 'cron:proactive', 'body': 'old',
        'target': 'photon', 'effects': [{'kind': 'intention', 'id': row['id']}]},
        time.time(), '2026-10-14')
    with box.HOOKS['json_transaction'](box.path(), default={}) as doc:
        doc['rows'][0].update(acceptance='unknown', acceptance_uncertain=True)
    rec._cron_tick()
    assert not sent
    box.HOOKS['gallery_receipt'] = lambda key: {'acceptance': 'accepted', 'message_id': 'old-accepted'}
    box.drain()
    assert not sent and store.current()[0]['status'] == 'fired'


def test_concurrent_ticks_use_one_outbox_send(timer):
    rec, store, create, sent, _ = timer
    create()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: rec._cron_tick(), range(2)))
    assert len(sent) == 1 and store.current()[0]['status'] == 'fired'


@pytest.mark.parametrize('shared', [False, True])
def test_exact_route_projection_and_saved_owner_digest(timer, monkeypatch, tmp_path, shared):
    rec, store, create, sent, identity = timer
    if shared:
        identity.update(route_mode='first_contact_shared_project', route_id='shared_project')
    thread = 'imessage:chat;-;+15550000001~shared' if shared else identity['route_id']
    # Exercise production reconstruction independently of the authenticated transport call.
    expectation = {'channel': 'photon', 'tenantId': 'tenant', 'owner': {
        'accountId': 'account', 'ownerId': identity['owner_id']}, 'lineKey': 'line',
        'target': 'photon', 'route': ({'mode': 'first_contact_shared_project'} if shared else
            {'mode': 'fixed', 'providerThreadId': thread}),
        'projectFingerprint': 'a'*64}
    from types import SimpleNamespace
    loader = SimpleNamespace(read_receiver_channel_expectation=lambda path: expectation)
    original_load = rec._load_shared_lib
    monkeypatch.setattr(rec, '_load_shared_lib', lambda name: SimpleNamespace(
        _startup_module=lambda: loader) if name == 'runtime_channel_proof' else original_load(name))
    proof = {**identity, 'provider_thread_id': thread}
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: proof)
    calls = []
    monkeypatch.setattr(rec.RUNTIME_OWNER_REMINDER, 'validate_authority', lambda *args: calls.append(args))
    current = rec._actual_owner_timer_authority
    assert current() == (identity, thread)
    assert calls
    proof['owner_id'] = '+15550000002'
    with pytest.raises(ValueError):
        current()


def test_cancel_during_unknown_keeps_late_accepted_truth(timer):
    rec, store, create, sent, _ = timer
    row = create()
    rec.OUTBOX.HOOKS['send'] = lambda *args, **kw: (sent.append(args) or
        (False, 'lost', {'acceptance': 'unknown'}))
    rec._cron_tick()
    proof = {**row['owner_create'], 'event_id': 'f'*64}
    status, canceled = store.cancel_admitted(row['id'], proof)
    assert status == 'committed' and canceled['status'] == 'canceled'
    rec.OUTBOX.HOOKS['gallery_receipt'] = lambda key: {'acceptance': 'accepted', 'message_id': 'late'}
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={}) as doc:
        doc['rows'][0]['next_at'] = 0
    rec.OUTBOX.drain()
    assert len(sent) == 1 and store.current()[0]['status'] == 'fired'


def test_not_due_timer_does_not_send(timer):
    rec, store, create, sent, _ = timer
    row = create()
    with store._lock():
        store._write_row(dict(row, due=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()))
    rec._cron_tick()
    assert not sent


@pytest.mark.parametrize('outcome', ['superseded', 'expired', 'failed'])
def test_terminal_timer_reconciles_before_retention_and_never_replays(timer, outcome):
    rec, store, create, sent, _ = timer
    create()
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: False
    rec._owner_reminder_tick()
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={}) as doc:
        delivery = doc['rows'][0]
        delivery.update(status=outcome, finished_at=1, effects_pending=False)
        delivery['payload'] = {'label': delivery['payload']['label']}
    rec.OUTBOX.drain()
    assert store.current()[0]['status'] == 'canceled'
    assert store.current()[0]['delivery_outcome'] == outcome
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: True
    rec._owner_reminder_tick()
    assert len(sent) == 1 and "couldn't deliver" in sent[0][0]
    assert store.current()[0]['delivery_notice_outcome'] == 'delivered'
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={}) as doc:
        doc['rows'][0]['finished_at'] = 1
    rec.OUTBOX.drain()
    rec._owner_reminder_tick()
    assert len(sent) == 1
    assert json.loads(Path(rec.OUTBOX.path()).read_text())['rows'] == []


def test_failed_timer_reconciliation_retains_outcome_for_recovery(timer):
    rec, store, create, sent, _ = timer
    create()
    rec.OUTBOX.HOOKS['valid'] = lambda payload: False
    real = rec.OUTBOX.HOOKS['on_terminal']
    rec.OUTBOX.HOOKS['on_terminal'] = lambda row: False
    rec._owner_reminder_tick()
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={}) as doc:
        doc['rows'][0]['finished_at'] = 1
    rec.OUTBOX.drain()
    assert len(json.loads(Path(rec.OUTBOX.path()).read_text())['rows']) == 1
    rec.OUTBOX.HOOKS['on_terminal'] = real
    rec.OUTBOX.drain()
    assert store.current()[0]['delivery_outcome'] == 'superseded'
    assert not sent


def test_unreadable_route_proof_on_retry_holds_reminder_until_it_can_send(timer, monkeypatch):
    rec, store, create, sent, _ = timer
    create()
    rec.OUTBOX.HOOKS['send'] = lambda *args, **kw: (sent.append(args) or
        (False, 'gateway 503', {'acceptance': 'rejected'}))
    rec._cron_tick()
    assert len(sent) == 1
    healthy = rec._owner_timer_authority
    def unavailable():
        raise ValueError('timer route unavailable')
    monkeypatch.setattr(rec, '_owner_timer_authority', unavailable)
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={}) as doc:
        doc['rows'][0]['next_at'] = 0
    rec.OUTBOX.drain()
    row = json.loads(Path(rec.OUTBOX.path()).read_text())['rows'][0]
    assert row['status'] == rec.OUTBOX.STATUS_PENDING
    assert store.current()[0]['status'] == 'scheduled'
    monkeypatch.setattr(rec, '_owner_timer_authority', healthy)
    rec.OUTBOX.HOOKS['send'] = lambda *args, **kw: (sent.append(args) or
        (True, '', {'acceptance': 'accepted', 'message_id': 'accepted'}))
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={}) as doc:
        doc['rows'][0]['next_at'] = 0
    rec.OUTBOX.drain()
    rec._cron_tick()
    assert len(sent) == 2 and store.current()[0]['status'] == 'fired'


def test_reminder_discovered_late_expires_from_due_time_and_notifies_once(timer):
    rec, store, create, sent, _ = timer
    create(minutes_ago=16)
    rec._owner_reminder_tick()
    assert not sent
    assert store.current()[0]['delivery_outcome'] == 'expired'
    rec._owner_reminder_tick()
    rec._owner_reminder_tick()
    assert len(sent) == 1 and "couldn't deliver" in sent[0][0]
    assert store.current()[0]['delivery_notice_outcome'] == 'delivered'


@pytest.mark.parametrize('legacy', [False, True])
def test_held_reminder_cannot_fire_after_grace_even_from_old_outbox(timer, monkeypatch, legacy):
    rec, store, create, sent, _ = timer
    row = create()
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: False
    rec._owner_reminder_tick()
    if legacy:
        with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={}) as doc:
            doc['rows'][0]['payload'].pop('valid_until')
    deadline = datetime.fromisoformat(row['due'].replace('Z', '+00:00')).timestamp() + rec.OWNER_REMINDER_GRACE_SECS
    monkeypatch.setattr(rec.time, 'time', lambda: deadline)
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: True
    rec.OUTBOX.drain()
    rec._owner_reminder_tick()
    assert len(sent) == 1 and "couldn't deliver" in sent[0][0]
    assert store.current()[0]['status'] == 'canceled'


def test_expired_unknown_send_never_claims_cancellation_or_sends_a_notice(timer, monkeypatch):
    rec, store, create, sent, _ = timer
    row = create()
    rec.OUTBOX.HOOKS['send'] = lambda *args, **kw: (sent.append(args) or
        (False, 'response lost', {'acceptance': 'unknown'}))
    rec._owner_reminder_tick()
    deadline = datetime.fromisoformat(row['due'].replace('Z', '+00:00')).timestamp() + rec.OWNER_REMINDER_GRACE_SECS
    monkeypatch.setattr(rec.time, 'time', lambda: deadline + 86400)
    rec.OUTBOX.drain()
    rec._owner_reminder_tick()
    assert len(sent) == 1 and store.current()[0]['status'] == 'scheduled'
    assert 'delivery_outcome' not in store.current()[0]
    rec.OUTBOX.HOOKS['gallery_receipt'] = lambda key: {'acceptance': 'accepted', 'message_id': 'late'}
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={}) as doc:
        doc['rows'][0]['next_at'] = 0
    rec.OUTBOX.drain()
    assert store.current()[0]['status'] == 'fired' and len(sent) == 1


def test_missed_notice_keeps_route_binding_and_recovers_after_restart(timer):
    rec, store, create, sent, identity = timer
    create(minutes_ago=16)
    rec._owner_reminder_tick()
    original_route = identity['route_id']
    identity['route_id'] = 'imessage:other~line'
    rec._owner_reminder_tick()
    assert not sent
    identity['route_id'] = original_route
    restarted = _load_outbox('notice_restarted')
    restarted.HOOKS.update(rec.OUTBOX.HOOKS)
    rec.OUTBOX = restarted
    rec._owner_reminder_tick()
    rec._owner_reminder_tick()
    assert len(sent) == 1 and store.current()[0]['delivery_notice_outcome'] == 'delivered'


def test_missed_notices_batch_once_with_local_times_and_expire_old_backlog(timer, monkeypatch):
    rec, store, create, sent, _ = timer
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(rec, '_local_now', lambda: datetime.now(ZoneInfo('America/Los_Angeles')))
    old = create(minutes_ago=1500)
    recent = [create(minutes_ago=20), create(minutes_ago=30)]
    for row in [old, *recent]:
        store.mark_undelivered(row['id'], 'expired')
    rec._owner_reminder_tick()
    rec._owner_reminder_tick()
    assert len(sent) == 1
    body = sent[0][0]
    expected_zone = datetime.fromisoformat(recent[0]['due'].replace('Z', '+00:00')).astimezone(
        ZoneInfo('America/Los_Angeles')).tzname()
    assert body.count('• ') == 2 and expected_zone in body and '+00:00' not in body
    rows = {row['id']: row for row in store.current()}
    assert rows[old['id']]['delivery_notice_outcome'] == 'expired'
    assert all(rows[row['id']]['delivery_notice_outcome'] == 'delivered' for row in recent)


def test_missed_notice_waits_for_quiet_hours_but_explicit_due_timer_does_not(timer, monkeypatch):
    from types import SimpleNamespace
    rec, store, create, sent, _ = timer
    quiet = [True]
    policy = SimpleNamespace(_in_quiet_hours=lambda now: quiet[0], _prefs=SimpleNamespace(
        load_explicit=lambda: {}, snooze_active=lambda **kw: False, effective_nudge_budget=lambda value: 5))
    monkeypatch.setattr(rec, '_discovery_interruption_policy', lambda: policy)
    rec.OUTBOX.HOOKS['delivery_ready'] = rec._history_discovery_delivery_ready
    missed = create(minutes_ago=20)
    store.mark_undelivered(missed['id'], 'expired')
    create()
    rec._owner_reminder_tick()
    assert len(sent) == 1 and sent[0][0] == 'Check test results'
    quiet[0] = False
    rec.OUTBOX.drain()
    rec._owner_reminder_tick()
    assert len(sent) == 2 and "couldn't deliver" in sent[1][0]


def test_unknown_batched_notice_is_not_reenqueued_when_another_reminder_expires(timer):
    rec, store, create, sent, _ = timer
    for _ in range(2):
        row = create(minutes_ago=20)
        store.mark_undelivered(row['id'], 'expired')
    rec.OUTBOX.HOOKS['send'] = lambda *args, **kw: (sent.append(args) or
        (False, 'lost receipt', {'acceptance': 'unknown'}))
    rec._owner_reminder_tick()
    row = create(minutes_ago=20)
    store.mark_undelivered(row['id'], 'expired')
    rec._owner_reminder_tick()
    rec._owner_reminder_tick()
    assert len(sent) == 2
    assert sent[0][0].count('• ') == 2 and '• ' not in sent[1][0]
    assert not any(row.get('delivery_notice_outcome') for row in store.current())


def test_expired_unattempted_batch_preserves_fresh_notice(timer, monkeypatch):
    rec, store, create, sent, _ = timer
    old, fresh = create(minutes_ago=1439), create(minutes_ago=20)
    for row in (old, fresh):
        store.mark_undelivered(row['id'], 'expired')
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: False
    rec._owner_reminder_tick()
    later = rec.time.time() + 120
    monkeypatch.setattr(rec.time, 'time', lambda: later)
    rec.OUTBOX.drain()
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: True
    rec._owner_reminder_tick()
    rec._owner_reminder_tick()
    rows = {row['id']: row for row in store.current()}
    assert rows[old['id']]['delivery_notice_outcome'] == 'expired'
    assert rows[fresh['id']]['delivery_notice_outcome'] == 'delivered'
    assert len(sent) == 1 and 'these reminders' not in sent[0][0]


@pytest.mark.parametrize('cross_midnight', [False, True])
def test_held_notice_survives_timezone_change_and_midnight(timer, monkeypatch, cross_midnight):
    from zoneinfo import ZoneInfo
    rec, store, create, sent, _ = timer
    monkeypatch.setattr(rec, '_local_now', lambda: datetime.now(ZoneInfo('America/Los_Angeles')))
    row = create(minutes_ago=20)
    store.mark_undelivered(row['id'], 'expired')
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: False
    rec._owner_reminder_tick()
    monkeypatch.setattr(rec, '_local_now', lambda: datetime.now(ZoneInfo('Asia/Tokyo')))
    # A held status belongs to the timer's 24-hour window, not the brief's calendar day.
    if cross_midnight:
        rec.OUTBOX.HOOKS['local_today'] = lambda: '2099-01-01'
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: True
    rec.OUTBOX.drain()
    rec._owner_reminder_tick()
    assert len(sent) == 1 and datetime.now(ZoneInfo('America/Los_Angeles')).tzname() in sent[0][0]
    assert store.current()[0]['delivery_notice_outcome'] == 'delivered'


def test_notice_keeps_a_fixed_offset_zone(timer, monkeypatch):
    rec, store, create, sent, _ = timer
    monkeypatch.setattr(rec, '_local_now', lambda: datetime.now(rec.TZCHAIN.resolve('+05:30')))
    row = create(minutes_ago=20)
    store.mark_undelivered(row['id'], 'expired')
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: False
    rec._owner_reminder_tick()
    monkeypatch.setattr(rec, '_local_now', lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: True
    rec.OUTBOX.drain()
    due = datetime.fromisoformat(row['due'].replace('Z', '+00:00')).astimezone(rec.TZCHAIN.resolve('+05:30'))
    assert len(sent) == 1 and due.strftime('%-I:%M %p UTC+05:30') in sent[0][0]


def test_notice_timezone_metadata_does_not_bypass_body_binding(timer):
    rec, store, create, sent, _ = timer
    row = create(minutes_ago=20)
    store.mark_undelivered(row['id'], 'expired')
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: False
    rec._owner_reminder_tick()
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={'rows': []}) as doc:
        doc['rows'][0]['payload']['body'] = 'An unrelated request'
    rec.OUTBOX.HOOKS['delivery_ready'] = lambda payload: True
    rec.OUTBOX.drain()
    assert not sent


def test_unknown_batch_keeps_fresh_member_reserved_past_old_deadline(timer, monkeypatch):
    rec, store, create, sent, _ = timer
    old, fresh = create(minutes_ago=1439), create(minutes_ago=20)
    for row in (old, fresh):
        store.mark_undelivered(row['id'], 'expired')
    rec.OUTBOX.HOOKS['send'] = lambda *args, **kw: (sent.append(args) or
        (False, 'lost receipt', {'acceptance': 'unknown'}))
    rec._owner_reminder_tick()
    later = rec.time.time() + 120
    monkeypatch.setattr(rec.time, 'time', lambda: later)
    rec.OUTBOX.drain()
    rec._owner_reminder_tick()
    assert len(sent) == 1
    assert not any(row.get('delivery_notice_outcome') for row in store.current())


def test_old_unreserved_notice_expires_without_owner_route(timer, monkeypatch):
    rec, store, create, sent, _ = timer
    old = create(minutes_ago=1500)
    store.mark_undelivered(old['id'], 'expired')
    monkeypatch.setattr(rec, '_owner_timer_authority', lambda: pytest.fail('no route needed for local expiry'))
    rec._owner_reminder_tick()
    assert store.current()[0]['delivery_notice_outcome'] == 'expired'
    assert not sent
