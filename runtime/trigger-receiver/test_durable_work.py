"""Crash/clock contracts; all providers and sources are synthetic."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import threading
import time

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
        def join(self, timeout=None):
            pass
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
    rec._WORK_THREADS.clear()


def test_stop_between_claim_and_dispatch_refunds_without_starting_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    identity = q.enqueue(tmp_path, 'run', {'label': 'synthetic'}, key='stop-before-dispatch')
    started = []

    class NeverStartedThread:
        def __init__(self, *args, **kwargs):
            pass
        def start(self):
            started.append(True)
        def join(self, timeout=None):
            pass

    monkeypatch.setattr(rec.threading, 'Thread', NeverStartedThread)
    rec._WORK_STOP.set()
    try:
        # Exercise the post-claim branch directly: the loop condition normally prevents a claim
        # once STOP is visible, while the production race can set it immediately after claim.
        original = rec._WORK_STOP.is_set
        calls = iter((False, True))
        monkeypatch.setattr(rec._WORK_STOP, 'is_set', lambda: next(calls, True))
        rec._drain_work()
    finally:
        monkeypatch.setattr(rec._WORK_STOP, 'is_set', original)
        rec._WORK_STOP.clear()
    row = q.get(tmp_path, identity)
    assert started == []
    assert row['status'] == 'pending' and row['attempts'] == 0 and row['owner'] is None


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


def test_retrying_one_terminal_event_preserves_other_terminal_event_ownership(tmp_path):
    events = [{'source': 'imessage', 'rowid': value} for value in (1, 2)]
    keys = [q.event_item_key(event) for event in events]
    jobs = []
    for event, key in zip(events, keys):
        job_id = q.enqueue(tmp_path, 'event', {'bundle': {'events': [event]}}, item_keys=[key])
        jobs.append(job_id)
        now = q.get(tmp_path, job_id)['due']
        for attempt in range(q.MAX_ATTEMPTS):
            job = q.claim(tmp_path, f'worker-{event["rowid"]}', now=now)
            q.fail(tmp_path, job_id, job['owner'], 'TimeoutError', now=now)
            now += 1000
    assert q.owned_items(tmp_path, 'event', keys) == dict(zip(keys, jobs))
    assert q.enqueue(tmp_path, 'event', {'bundle': {'events': [events[0]]}},
                     item_keys=[keys[0]], retry_terminal=True) == jobs[0]
    assert q.owned_items(tmp_path, 'event', keys) == dict(zip(keys, jobs))
    assert q.get(tmp_path, jobs[1])['status'] == 'failed'


def test_claim_skips_exhausted_head_and_returns_next_runnable_job(tmp_path):
    exhausted = q.enqueue(tmp_path, 'event', {}, key='exhausted', priority=10)
    fresh = q.enqueue(tmp_path, 'event', {}, key='fresh', priority=10)
    with q._db(tmp_path) as db:
        db.execute('UPDATE jobs SET attempts=? WHERE id=?', (q.MAX_ATTEMPTS, exhausted))
    claimed = q.claim(tmp_path, 'worker')
    assert claimed['id'] == fresh
    row = q.get(tmp_path, exhausted)
    assert row['status'] == 'failed' and row['error'] == 'composition attempts exhausted'


def test_saved_result_handoff_failures_are_bounded_without_recomposition(tmp_path):
    identity = q.enqueue(tmp_path, 'run', {'prompt': 'compose once'}, key='handoff')
    first = q.claim(tmp_path, 'composer')
    q.save_result(tmp_path, identity, 'composer', {'text': 'prepared'})
    saved = q.get(tmp_path, identity)
    assert saved['handoff_attempts'] == 1
    now = first['due']
    q.fail(tmp_path, identity, 'composer', 'OSError', now=now)
    for attempt in range(1, q.MAX_ATTEMPTS):
        now += 1000
        job = q.claim(tmp_path, f'handoff-{attempt}', now=now)
        assert job['result'] == {'text': 'prepared'}
        assert job['attempts'] == 1 and job['handoff_attempts'] == attempt + 1
        q.fail(tmp_path, identity, job['owner'], 'OSError', now=now)
    row = q.get(tmp_path, identity)
    assert row['status'] == 'failed' and row['result'] is None
    assert row['error'] == 'delivery handoff attempts exhausted'
    assert q.claim(tmp_path, 'never', now=now) is None


def test_existing_work_database_gains_handoff_budget_column(tmp_path):
    events = tmp_path / 'events'
    events.mkdir()
    with sqlite3.connect(events / 'work.sqlite3') as db:
        db.execute('''CREATE TABLE jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL, due REAL NOT NULL,
            valid_until REAL, priority INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            owner TEXT, lease_until REAL, result TEXT, error TEXT, finished REAL)''')
        db.execute('''CREATE TABLE work_items (
            kind TEXT NOT NULL, item_key TEXT NOT NULL, job_id TEXT NOT NULL,
            PRIMARY KEY(kind,item_key))''')
    identity = q.enqueue(tmp_path, 'run', {}, key='after-migration')
    assert q.get(tmp_path, identity)['handoff_attempts'] == 0


RACE_WORKERS = 24
# One aligned start catches the converting code roughly four times in five; three fresh stores make
# a regression essentially impossible to miss, and the whole case still costs a couple of seconds.
RACE_ROUNDS = 3
LEGACY_SCHEMA =('''CREATE TABLE jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL, due REAL NOT NULL,
            valid_until REAL, priority INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            owner TEXT, lease_until REAL, result TEXT, error TEXT, finished REAL)''',
        '''CREATE TABLE work_items (
            kind TEXT NOT NULL, item_key TEXT NOT NULL, job_id TEXT NOT NULL,
            PRIMARY KEY(kind,item_key))''')

# One process, one open. It loads the queue module BY PATH, so the same driver can be aimed at any
# copy of work_queue.py, announces itself, and then spins on a gate file — every interpreter is
# already warm and waiting when the gate drops, which is what makes the opens actually collide.
RACE_CHILD = '''
import importlib.util, os, sys, time
source, root, gate, index = sys.argv[1:5]
spec = importlib.util.spec_from_file_location('race_work_queue', source)
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
open(os.path.join(gate, 'ready-' + index), 'w').close()
while not os.path.exists(os.path.join(gate, 'go')):
    time.sleep(.001)
start = float(open(os.path.join(gate, 'go')).read())
time.sleep(max(0, start - time.time()))
try:
    queue.enqueue(root, 'run', {}, key='concurrent-' + index)
except BaseException as error:
    sys.stderr.write(f'{type(error).__name__}: {error}')
    raise SystemExit(3)
'''


def race_concurrent_opens(queue_source, root, journal_mode, workers=RACE_WORKERS):
    """Open one legacy store from `workers` separate PROCESSES at once; return their failures."""
    events = Path(root) / 'events'
    events.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(events / 'work.sqlite3') as db:
        db.execute(f'PRAGMA journal_mode={journal_mode}')
        for statement in LEGACY_SCHEMA:
            db.execute(statement)
    gate = Path(root) / 'gate'
    gate.mkdir()
    children = [subprocess.Popen(
        [sys.executable, '-c', RACE_CHILD, str(queue_source), str(root), str(gate), str(index)],
        stderr=subprocess.PIPE, text=True) for index in range(workers)]
    deadline = time.monotonic() + 60
    while len(list(gate.glob('ready-*'))) < workers and time.monotonic() < deadline:
        time.sleep(.01)
    # Published by rename, so a child never reads a half-written instant.
    (gate / 'go.tmp').write_text(str(time.time() + .25))
    (gate / 'go.tmp').rename(gate / 'go')
    failures = []
    for child in children:
        _out, stderr = child.communicate(timeout=120)
        if child.returncode:
            failures.append(stderr.strip() or f'exit {child.returncode}')
    return failures


@pytest.mark.parametrize('journal_mode', ['delete', 'wal'])
def test_concurrent_first_opens_serialize_legacy_schema_migration(tmp_path, journal_mode):
    """PROCESSES, not threads. The reservation this contract is about is held across processes, and
    the GIL hands the pragma from one thread to the next so cleanly that threads never collide —
    twenty-four of them pass against the converting code that this fixes. Twenty-four processes do
    not: the pragma that switched journal mode before taking the write reservation loses the file
    lock to a peer and the open dies with `database is locked`."""
    for round_number in range(RACE_ROUNDS):
        root = tmp_path / f'round-{round_number}'
        failures = race_concurrent_opens(Path(q.__file__), root, journal_mode)
        assert not failures, failures[:3]
        with sqlite3.connect(root / 'events' / 'work.sqlite3') as db:
            assert db.execute('PRAGMA journal_mode').fetchone()[0] == journal_mode
            columns = [row[1] for row in db.execute('PRAGMA table_info(jobs)')]
            assert columns.count('handoff_attempts') == 1
            assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == RACE_WORKERS


def test_graceful_release_refunds_saved_result_handoff_attempt(tmp_path):
    identity = q.enqueue(tmp_path, 'run', {}, key='released-handoff')
    assert q.claim(tmp_path, 'worker')['id'] == identity
    q.save_result(tmp_path, identity, 'worker', {'text': 'prepared'})
    assert q.get(tmp_path, identity)['handoff_attempts'] == 1
    assert q.release(tmp_path, identity, 'worker')
    released = q.get(tmp_path, identity)
    assert released['status'] == 'ready' and released['handoff_attempts'] == 0
    resumed = q.claim(tmp_path, 'next-worker')
    assert resumed['result'] == {'text': 'prepared'} and resumed['handoff_attempts'] == 1


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


def test_slow_delivery_effect_cannot_replay_after_retry_interval(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    box = rec.OUTBOX
    clock = [1000.0]
    monkeypatch.setattr(box.time, 'time', lambda: clock[0])
    monkeypatch.setitem(box.HOOKS, 'send', lambda *_: (True, '', {'message_id': 'accepted-slow'}))
    monkeypatch.setitem(box.HOOKS, 'record', lambda *a, **k: None)
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: True)
    entered, release, calls = threading.Event(), threading.Event(), []

    def slow_effect(payload):
        calls.append(payload)
        entered.set()
        assert release.wait(5)
        return True

    monkeypatch.setitem(box.HOOKS, 'on_delivered', slow_effect)
    worker = threading.Thread(target=box.deliver, args=({
        'label': 'event', 'body': 'One reminder', 'target': 'test:owner', 'run_id': 'slow'},))
    worker.start()
    assert entered.wait(5)
    clock[0] += 61
    assert box.drain()['attempted'] == 0
    assert len(calls) == 1
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert box.counts()['effects_pending'] == 0


def test_permanent_post_delivery_effect_quarantines_without_keeping_identifiers(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    box = rec.OUTBOX
    monkeypatch.setattr(box, 'MAX_EFFECT_ATTEMPTS', 2)
    monkeypatch.setitem(box.HOOKS, 'send', lambda *_: (True, '', {'message_id': 'accepted-2'}))
    receipts = []
    monkeypatch.setitem(box.HOOKS, 'record', lambda label, status, detail='', **k: receipts.append((label, status, detail)))
    monkeypatch.setitem(box.HOOKS, 'valid', lambda payload: True)
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: False)
    assert box.deliver({'label': 'event', 'body': 'Delivered once', 'target': 'test:owner',
                        'effects': [{'kind': 'test', 'anchor_key': 'safe-id',
                                     'handle': 'someone@example.test'}], 'run_id': 'two'})
    with rec.CONNECTORS.json_transaction(box.path(), default={}) as doc:
        doc['rows'][0]['effects_next_at'] = 0
    box.drain()
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['status'] == 'delivered' and row['effects_status'] == 'quarantined'
    assert row['receipt']['message_id'] == 'accepted-2'
    # What a human needs to diagnose the quarantine, and nothing the effects addressed themselves
    # with: a row that will never replay keeps no handle, jid, phone, email or thread id.
    assert row['payload'] == {'label': 'event', 'run_id': 'two'}
    assert 'someone@example.test' not in json.dumps(row)
    assert row['effects_error'] == 'post-delivery effect failed permanently'
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


def test_signal_handler_waits_for_real_worker_to_refund_lease(tmp_path, monkeypatch):
    """The production handler raises SystemExit on the main thread; its daemon worker must finish
    the interruption path before that exit is allowed to end the process."""
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    monkeypatch.setattr(rec, 'WORK_SHUTDOWN_GRACE_SECS', 5)
    rec._WORK_STOP.clear()
    identity = q.enqueue(tmp_path, 'run', {
        'runner': [sys.executable, '-c', 'import time; time.sleep(30)'],
        'prompt': '', 'label': 'cron:sotto-morning-brief', 'decision_ids': []}, key='signal-stop')
    rec._drain_work()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with rec._WORK_LOCK:
            if rec._WORK_PROCESSES:
                break
        time.sleep(.01)
    else:
        pytest.fail('worker child never started')

    with pytest.raises(SystemExit, match=str(128 + signal.SIGTERM)):
        rec._stop_work(signal.SIGTERM, None)

    row = q.get(tmp_path, identity)
    assert row['status'] == 'pending' and row['attempts'] == 0 and row['owner'] is None
    with rec._WORK_LOCK:
        assert not rec._WORK_THREADS and not rec._WORK_PROCESSES
    rec._WORK_STOP.clear()


def _await_child(timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with rec._WORK_LOCK:
            if rec._WORK_PROCESSES:
                return next(iter(rec._WORK_PROCESSES.values()))
        time.sleep(.01)
    pytest.fail('worker child never started')


def test_shutdown_refunds_a_lease_stranded_in_the_delivery_handoff(tmp_path, monkeypatch):
    """THE COMPOSED BRIEF THIS EXISTS FOR. Past `save_result` the worker is inside `_deliver_text`,
    which has no interrupt path and may legitimately spend SEND_TIMEOUT_SECS — longer than the whole
    shutdown grace. Left leased, that lease ages out CHARGED, and the third redeploy makes claim()
    call the handoff budget exhausted and DESTROY a brief that can never be recomposed."""
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    monkeypatch.setattr(rec, 'WORK_SHUTDOWN_GRACE_SECS', .5)
    composed = {'text': 'Composed once', 'label': 'cron:sotto-morning-brief'}
    monkeypatch.setattr(rec, '_execute_work', lambda job: composed)
    entered, release = threading.Event(), threading.Event()

    def hanging_delivery(text, *args, **kwargs):
        entered.set()
        assert release.wait(30)
        return True

    monkeypatch.setattr(rec, '_deliver_text', hanging_delivery)
    identity = q.enqueue(tmp_path, 'run', {'label': 'cron:sotto-morning-brief'}, key='stranded-handoff')
    rec._WORK_STOP.clear()
    rec._drain_work()
    assert entered.wait(5), 'the worker never reached the delivery handoff'
    try:
        with pytest.raises(SystemExit, match=str(128 + signal.SIGTERM)):
            rec._stop_work(signal.SIGTERM, None)
        row = q.get(tmp_path, identity)
        assert row['status'] == 'ready' and row['handoff_attempts'] == 0 and row['owner'] is None
        # Immediately claimable, with the composition intact and never recomposed.
        monkeypatch.setattr(rec, '_execute_work', lambda job: pytest.fail('recomposed a saved brief'))
        resumed = q.claim(tmp_path, 'next-instance')
        assert resumed['id'] == identity and resumed['result'] == composed
        assert resumed['attempts'] == 1 and resumed['handoff_attempts'] == 1
    finally:
        release.set()
        rec._WORK_STOP.clear()
    # The stranded worker settling late cannot take the job back from the instance that now owns it.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with rec._WORK_LOCK:
            if not rec._WORK_THREADS:
                break
        time.sleep(.01)
    assert q.get(tmp_path, identity)['owner'] == resumed['owner']


def test_shutdown_kills_a_child_that_ignores_its_termination_signal(tmp_path, monkeypatch):
    """The grace is a chance to unwind, not permission to stay. An orphan keeps running against the
    same volume and would execute alongside the next instance's reclaim of the very same job."""
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    monkeypatch.setattr(rec, 'WORK_SHUTDOWN_GRACE_SECS', .5)
    ready = tmp_path / 'deaf-child-armed'
    deaf = ('import pathlib, signal, time\n'
            'signal.signal(signal.SIGTERM, lambda *a: None)\n'
            f'pathlib.Path({str(ready)!r}).touch()\n'
            'time.sleep(300)\n')
    identity = q.enqueue(tmp_path, 'run', {'runner': [sys.executable, '-c', deaf],
                                           'prompt': '', 'label': 'synthetic', 'decision_ids': []},
                         key='deaf-child')
    rec._WORK_STOP.clear()
    rec._drain_work()
    child = _await_child()
    armed = time.monotonic() + 15
    while not ready.exists() and time.monotonic() < armed:
        time.sleep(.01)
    assert ready.exists(), 'the child never installed its SIGTERM handler'
    try:
        with pytest.raises(SystemExit, match=str(128 + signal.SIGTERM)):
            rec._stop_work(signal.SIGTERM, None)
    finally:
        rec._WORK_STOP.clear()
    assert child.wait(10) == -signal.SIGKILL
    row = q.get(tmp_path, identity)
    assert row['status'] == 'pending' and row['attempts'] == 0 and row['owner'] is None


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
