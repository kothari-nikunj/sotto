"""Crash/clock contracts; all providers and sources are synthetic."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('durable_receiver', HERE / 'receiver.py')
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)
q = rec.WORK_QUEUE


def test_synthetic_event_uses_the_job_already_committed_by_triage(tmp_path, monkeypatch):
    """Calendar and post-meeting producers must not fork the durable decision into a second send."""
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_delivery_channel_ready', lambda label: True)
    accepted = []

    def triage(events, catchup):
        job_id = q.enqueue(tmp_path, 'event', {'bundle': {'events': events}}, key='decision:fixture')
        accepted.append(job_id)
        return {'verdict': 'agent', 'job_id': job_id, 'bundle': {'events': events}}

    monkeypatch.setattr(rec, 'run_triage', triage)
    monkeypatch.setattr(rec, '_stage_bundle', lambda *a: pytest.fail('staged a duplicate bundle'))
    monkeypatch.setattr(rec, '_spawn_event_agent', lambda *a: pytest.fail('launched a duplicate agent'))
    rec._WORK_WAKE.clear()
    assert rec._dispatch_synthetic({'source': 'calendar_change', 'rowid': 'fixture'}, 'calendar')
    assert rec._WORK_WAKE.is_set()
    assert q.claim(tmp_path, 'worker')['id'] == accepted[0]
    assert q.claim(tmp_path, 'second-worker') is None
    rec._WORK_WAKE.clear()


def test_accepted_event_survives_restart_before_first_worker(tmp_path):
    identity = q.enqueue(tmp_path, 'event', {'bundle': {'events': [{'rowid': 71}]}}, key='imessage:71')
    assert q.enqueue(tmp_path, 'event', {'bundle': {}}, key='imessage:71') == identity
    claimed = q.claim(tmp_path, 'new-process')
    assert claimed['id'] == identity
    assert claimed['payload']['bundle']['events'][0]['rowid'] == 71
    assert q.claim(tmp_path, 'second-process') is None


def test_completed_output_survives_lease_expiry_without_recomposition(tmp_path):
    identity = q.enqueue(tmp_path, 'run', {'prompt': 'synthetic'}, key='one')
    first = q.claim(tmp_path, 'old')
    q.save_result(tmp_path, identity, 'old', {'text': 'Prepared once'})
    second = q.claim(tmp_path, 'new', now=first['lease_until'] + 1)
    assert second['id'] == identity
    assert second['result'] == {'text': 'Prepared once'}
    assert not q.finish(tmp_path, identity, 'old')  # an old owner cannot settle the recovered job
    assert q.finish(tmp_path, identity, 'new')
    assert q.get(tmp_path, identity)['payload'] == {}


def test_worker_recovers_saved_result_into_outbox_once(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'test:owner')
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(rec, '_send_via_channel', lambda body, target: (sent.append(body) is None, '', {'message_id': 'provider-1'}))
    monkeypatch.setattr(rec, '_on_delivered', lambda payload: True)
    identity = q.enqueue(tmp_path, 'run', {}, key='prepared')
    first = q.claim(tmp_path, 'old')
    result = {'text': 'Prepared once', 'label': 'event', 'effects': []}
    q.save_result(tmp_path, identity, 'old', result)
    recovered = q.claim(tmp_path, rec._WORK_OWNER, now=first['lease_until'] + 1)
    monkeypatch.setattr(rec, '_execute_work', lambda job: pytest.fail('must not recompose'))
    rec._work_one(recovered)
    assert sent == ['Prepared once']
    rec.OUTBOX.deliver({'run_id': identity, 'label': 'event', 'body': 'Prepared once', 'target': 'test:owner'})
    assert len(sent) == 1
    assert q.get(tmp_path, identity)['status'] == 'done'


def test_background_capacity_cannot_occupy_both_slots(tmp_path):
    q.enqueue(tmp_path, 'run', {}, key='maintenance1', priority=60)
    q.enqueue(tmp_path, 'run', {}, key='maintenance2', priority=60)
    assert q.claim(tmp_path, 'one') is not None
    assert q.claim(tmp_path, 'two') is None
    urgent = q.enqueue(tmp_path, 'event', {}, key='urgent', priority=10)
    assert q.claim(tmp_path, 'two')['id'] == urgent


def test_continuous_foreground_cannot_starve_due_maintenance(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(q.time, 'time', lambda: clock[0])
    background = q.enqueue(tmp_path, 'run', {}, key='maintenance', priority=60)
    foreground = q.enqueue(tmp_path, 'event', {}, key='first-user-work', priority=10)
    assert q.claim(tmp_path, 'foreground')['id'] == foreground
    clock[0] += q.BACKGROUND_MAX_WAIT_SECONDS + 1
    assert q.renew(tmp_path, foreground, 'foreground')
    next_foreground = q.enqueue(tmp_path, 'event', {}, key='next-user-work', priority=10)
    q.enqueue(tmp_path, 'run', {}, key='other-maintenance', priority=60)
    assert q.claim(tmp_path, 'maintenance')['id'] == background
    assert q.claim(tmp_path, 'third') is None
    assert q.finish(tmp_path, foreground, 'foreground')
    assert q.claim(tmp_path, 'next-foreground')['id'] == next_foreground


def test_maintenance_aging_starts_when_due_not_when_future_job_was_created(tmp_path, monkeypatch):
    monkeypatch.setattr(q.time, 'time', lambda: 1000.0)
    q.enqueue(tmp_path, 'run', {}, key='tomorrows-maintenance', priority=60, not_before=10000)
    foreground = q.enqueue(tmp_path, 'event', {}, key='current-user-work', priority=10)
    assert q.claim(tmp_path, 'worker', now=10001)['id'] == foreground


def test_same_process_recovered_lease_fences_the_original_worker(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(q.time, 'time', lambda: clock[0])
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    dispatched = []
    class DeferredThread:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args
        def start(self):
            if self.target is rec._work_one:
                dispatched.append(self.args[0])
    monkeypatch.setattr(rec.threading, 'Thread', DeferredThread)
    identity = q.enqueue(tmp_path, 'run', {'label': 'synthetic'}, key='same-process-recovery')
    rec._drain_work()
    old = dispatched[0]
    clock[0] = old['lease_until'] + 1
    rec._drain_work()
    current = dispatched[1]
    assert current['id'] == old['id'] and current['owner'] != old['owner']
    monkeypatch.setattr(rec, '_execute_work', lambda job: {'text': 'Prepared once', 'label': 'synthetic'})
    delivered = []
    monkeypatch.setattr(rec, '_deliver_text', lambda text, *a, **k: delivered.append(text))
    rec._work_one(old)
    assert delivered == [] and q.get(tmp_path, identity)['owner'] == current['owner']
    rec._work_one(current)
    assert delivered == ['Prepared once'] and q.get(tmp_path, identity)['status'] == 'done'


def test_failed_process_has_bounded_retries(tmp_path):
    identity = q.enqueue(tmp_path, 'run', {}, key='failing')
    now = q.get(tmp_path, identity)['due']
    for attempt in range(q.MAX_ATTEMPTS):
        job = q.claim(tmp_path, 'worker', now=now)
        assert job['attempts'] == attempt + 1
        q.fail(tmp_path, identity, 'worker', 'TimeoutError', now=now)
        now += 1000
    assert q.get(tmp_path, identity)['status'] == 'failed'
    assert q.claim(tmp_path, 'worker', now=now) is None


def test_clock_recovers_missed_brief_without_claiming_yesterday(monkeypatch):
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(2026, 9, 7, 6, 41))
    assert rec._cron_slot('30 6 * * *') is None
    assert rec._scheduled_slot('30 6 * * *') == '2026-09-07'
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(2026, 9, 7, 15, 0))
    assert rec._scheduled_slot('30 6 * * *') is None


def test_provider_accepted_effects_failure_retries_without_resending(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    box = rec.OUTBOX
    sent, effects = [], []
    monkeypatch.setitem(box.HOOKS, 'send', lambda body, target: (sent.append(body) is None, '', {'message_id': 'accepted-1'}))
    monkeypatch.setitem(box.HOOKS, 'record', lambda *a, **k: None)
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: True)
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: effects.append(payload) or False)
    assert box.deliver({'label': 'event', 'body': 'One reminder', 'target': 'test:owner',
                        'effects': [{'kind': 'test'}], 'run_id': 'one'})
    stored = json.loads(Path(box.path()).read_text())['rows'][0]
    assert stored['effects_pending'] is True
    assert 'body' not in stored['payload']
    assert stored['receipt']['message_id'] == 'accepted-1'
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: effects.append(payload) or True)
    with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
        doc['rows'][0]['effects_next_at'] = 0
    assert box.drain()['attempted'] == 0
    assert len(sent) == 1 and len(effects) == 2
    assert box.counts()['effects_pending'] == 0


def test_permanent_post_delivery_effect_is_quarantined_with_recovery_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    box = rec.OUTBOX
    monkeypatch.setattr(box, 'MAX_EFFECT_ATTEMPTS', 2)
    monkeypatch.setitem(box.HOOKS, 'send', lambda *_: (True, '', {'message_id': 'accepted-2'}))
    receipts = []
    monkeypatch.setitem(box.HOOKS, 'record', lambda label, status, detail='', **k: receipts.append((label, status, detail)))
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: True)
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: False)
    assert box.deliver({'label': 'event', 'body': 'Delivered once', 'target': 'test:owner',
                        'effects': [{'kind': 'test', 'anchor_key': 'safe-id'}], 'run_id': 'two'})
    with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
        doc['rows'][0]['effects_next_at'] = 0
    box.drain()
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['status'] == 'delivered' and row['effects_status'] == 'quarantined'
    assert row['receipt']['message_id'] == 'accepted-2'
    assert row['payload']['effects'] == [{'kind': 'test', 'anchor_key': 'safe-id'}]
    assert box.counts()['effects_failed'] == 1 and box.counts()['effects_pending'] == 0
    # The quarantine is a receipt in the Record, not only a print line: the message went out and
    # what was supposed to follow it did not — the dashboard can say so.
    assert receipts[-1][0] == 'event' and receipts[-1][1] == box.STATUS_FAILED
    assert 'delivered, but its follow-up effects failed permanently after 2 attempts' in receipts[-1][2]


def test_a_row_whose_validity_check_raises_holds_without_stalling_the_rows_behind_it(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    box = rec.OUTBOX
    sent = []
    monkeypatch.setitem(box.HOOKS, 'send', lambda body, target: sent.append(body) or (True, '', {'message_id': 'm'}))
    monkeypatch.setitem(box.HOOKS, 'record', lambda *a, **k: None)

    def valid(payload):
        if payload.get('label') == 'poison':
            raise ModuleNotFoundError('ledger_io')
        return True

    monkeypatch.setitem(box.HOOKS, 'valid', valid)
    assert box.deliver({'label': 'poison', 'body': 'Never checked', 'target': 'test:owner',
                        'effects': [{'kind': 'eligibility', 'anchor_key': 'k'}]}) is False
    assert box.deliver({'label': 'proactive', 'body': 'Behind it', 'target': 'test:owner'}) is True
    with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
        for row in doc['rows']:
            row['next_at'] = 0
    assert box.drain()['attempted'] == 1    # the poison row is retried, and does not raise out of drain
    assert sent == ['Behind it']
    poison = next(r for r in json.loads(Path(box.path()).read_text())['rows'] if r['status'] == 'pending')
    assert poison['attempts'] == 2, 'each raising check still spends an attempt — the budget bounds it'


def test_a_graceful_stop_hands_the_lease_back_without_charging_an_attempt(tmp_path, monkeypatch):
    """A redeploy's SIGTERM mid-composition is not the job failing. Charging it made a day's
    brief terminal in three pushes on a platform that redeploys on every push."""
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    reports = []
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: reports.append(a))
    identity = q.enqueue(tmp_path, 'run', {
        'runner': [sys.executable, '-c', 'import time; time.sleep(30)'],
        'prompt': '', 'label': 'cron:sotto-morning-brief', 'decision_ids': []}, key='interrupted')
    job = q.claim(tmp_path, 'worker')
    assert job['attempts'] == 1
    rec._WORK_STOP.set()
    try:
        rec._work_one(job)
    finally:
        rec._WORK_STOP.clear()
    row = q.get(tmp_path, identity)
    assert row['status'] == 'pending' and row['attempts'] == 0 and row['owner'] is None
    assert reports[-1][1] == 'skipped' and 'interrupted by shutdown' in reports[-1][2]
    resumed = q.claim(tmp_path, 'next-instance')
    assert resumed['id'] == identity and resumed['attempts'] == 1


def test_expired_meeting_reminder_never_touches_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *args: pytest.fail('expired reminder was sent'))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'record', lambda *a, **k: None)
    assert not rec.OUTBOX.deliver({'label': 'event', 'body': 'Meeting soon', 'target': 'test:owner', 'valid_until': 1})
    row = json.loads(Path(rec.OUTBOX.path()).read_text())['rows'][0]
    assert row['status'] == 'expired'


def test_control_credential_never_reaches_detached_worker(monkeypatch):
    monkeypatch.setenv('SOTTO_CONTROL_TOKEN', 'operator-secret')
    assert 'SOTTO_CONTROL_TOKEN' not in rec._spawn_env('stable-work-id')
    assert rec._spawn_env('stable-work-id')['SOTTO_DELIVERY_RUN_ID'] == 'stable-work-id'


def test_real_subprocess_execution_is_bounded_and_saves_output(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    q.enqueue(tmp_path, 'run', {'runner': [sys.executable, '-c', 'import sys; print(sys.argv[1])'],
                                         'prompt': 'synthetic-output', 'label': 'test'}, key='process')
    job = q.claim(tmp_path, rec._WORK_OWNER)
    result = rec._execute_work(job)
    assert result['text'].strip() == 'synthetic-output'
    assert not rec._WORK_PROCESSES



def test_worker_failure_retains_safe_category_without_private_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    reports = []
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: reports.append(a))
    rec._WORK_STOP.clear()
    identity = q.enqueue(tmp_path, 'run', {
        'runner': [sys.executable, '-c', "raise ValueError('private-token-and-message-body')"],
        'prompt': '', 'label': 'synthetic', 'decision_ids': []}, key='diagnostic')
    rec._work_one(q.claim(tmp_path, 'worker'))
    row = q.get(tmp_path, identity)
    assert row['status'] == 'pending'
    assert row['error'] == 'worker_exit_1:ValueError'
    assert 'private-token-and-message-body' not in str(reports)
    assert 'worker_exit_1:ValueError' in str(reports)
