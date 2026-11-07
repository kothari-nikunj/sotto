"""Crash/clock contracts; all providers and sources are synthetic."""
import importlib.util
import hashlib
import json
from datetime import datetime
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('durable_receiver', HERE / 'receiver.py')
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)
q = rec.WORK_QUEUE


@pytest.fixture
def inline_runner(monkeypatch):
    """Worker mechanics below run tiny inline Python runners; the declared-procedure fence that
    refuses them in production is covered by the admission tests above."""
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_args: None)


def test_sotto_job_admission_keeps_fixed_python_procedure_and_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    runner = [sys.executable, str(HERE / 'procedure_runner.py')]
    prompt = json.dumps({'kind': 'digest', 'pack': '/app/sotto-skills'})
    monkeypatch.setattr(rec.shutil, 'which', lambda executable: executable if executable == sys.executable else None)
    identity = rec._spawn_and_deliver(runner, prompt, 'cron:sotto-midday-digest')
    assert rec._spawn_and_deliver(runner, prompt, 'cron:sotto-midday-digest') == identity
    saved = q.get(tmp_path, identity)
    assert saved['payload']['runner'] == runner
    assert saved['payload']['prompt'] == prompt
    assert saved['attempts'] == 0
    rec._WORK_WAKE.clear()


def test_sotto_hermes_one_shot_fallback_is_refused_before_queue_admission(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: pytest.fail('one-shot translated to /v1/run'))
    monkeypatch.setattr(rec.shutil, 'which', lambda executable: pytest.fail('one-shot executable probed'))
    for runner in (['hermes', '-z'], ['/usr/local/bin/hermes', '-z']):
        with pytest.raises(RuntimeError, match='unattended skill procedure is unsupported'):
            rec._spawn_and_deliver(runner, 'Run the skill', 'cron:sotto-morning-brief')
    assert not (tmp_path / 'events' / 'work.sqlite3').exists()


def test_selected_sotto_wake_cannot_improvise_when_fixed_brief_is_not_selected(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_managed_brief', lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match='unattended skill procedure is unsupported'):
        rec.run_skill('sotto-morning-brief', '/data/staged.json')
    assert not (tmp_path / 'events' / 'work.sqlite3').exists()


def test_selected_sotto_rejects_foreign_script_and_stale_one_shot_at_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec.shutil, 'which', lambda executable: executable)
    for runner in ([sys.executable, str(HERE / 'other.py')], [sys.executable, '-c', 'print(1)']):
        with pytest.raises(RuntimeError, match='unattended skill procedure is unsupported'):
            rec._spawn_and_deliver(runner, '{}', 'background:fixture')
    assert not (tmp_path / 'events' / 'work.sqlite3').exists()
    legacy_id = q.enqueue(tmp_path, 'run', {
        'runner': ['hermes', '-z'], 'prompt': 'Run the skill', 'label': 'cron:sotto-morning-brief'},
        key='legacy-morning', valid_until=time.time() + 3600)
    stale = q.claim(tmp_path, 'recovered-worker')
    assert stale['id'] == legacy_id
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: pytest.fail('stale run reached receipt'))
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *a, **k: pytest.fail('stale run spawned'))
    with pytest.raises(RuntimeError, match='unattended skill procedure is unsupported'):
        rec._execute_work(stale)
    assert q.get(tmp_path, legacy_id)['result'] is None
    assert q.get(tmp_path, legacy_id)['status'] == 'leased'
    receipts = []
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: receipts.append(a))
    monkeypatch.setattr(rec, '_deliver_text', lambda *a, **k: pytest.fail('stale run delivered'))
    rec._work_one(stale)
    retained = q.get(tmp_path, legacy_id)
    assert retained['status'] == 'pending'
    assert retained['result'] is None and retained['attempts'] == 1
    assert receipts and receipts[-1][1] == 'failed'


def test_selected_sotto_event_and_background_have_exact_fixed_scripts(monkeypatch):
    memory = rec._find_sotto_script('_shared', 'scripts', 'memory_cycle.py')
    event = rec._find_sotto_script('_shared', 'scripts', 'compose_notification.py')
    assert memory and event
    rec._require_sotto_procedure([sys.executable, memory], 'run')
    rec._require_sotto_procedure([sys.executable, event], 'event')
    for runner, kind in (([sys.executable, event], 'run'),
                         ([sys.executable, memory], 'event'),
                         ([sys.executable, memory, '--extra'], 'run'),
                         ([sys.executable, event], 'unknown')):
        with pytest.raises(RuntimeError, match='unattended skill procedure is unsupported'):
            rec._require_sotto_procedure(runner, kind)


def test_selected_sotto_preserves_saved_result_handoff_without_rerunning(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    identity = q.enqueue(tmp_path, 'run', {
        'runner': ['hermes', '-z'], 'prompt': 'former runtime request',
        'label': 'cron:sotto-morning-brief'}, key='saved-former-result')
    claimed = q.claim(tmp_path, 'recovered-worker')
    assert claimed['id'] == identity
    q.save_result(tmp_path, identity, 'recovered-worker', {
        'text': 'Saved final text', 'label': 'cron:sotto-morning-brief'})
    assert q.get(tmp_path, identity)['result']['text'] == 'Saved final text'
    monkeypatch.setattr(rec, '_execute_work', lambda job: pytest.fail('saved result reran'))
    handed_off = []
    monkeypatch.setattr(rec, '_deliver_text', lambda text, label, **kwargs:
                        handed_off.append((text, label, kwargs['run_id'])))
    rec._work_one(q.get(tmp_path, identity))
    assert handed_off == [('Saved final text', 'cron:sotto-morning-brief', identity)]
    assert q.get(tmp_path, identity)['status'] == 'done'


@pytest.mark.parametrize('name,kind,runner_name', [
    ('sotto-morning-brief', 'morning', 'brief_runner.py'),
    ('sotto-evening-brief', 'evening', 'brief_runner.py'),
    ('sotto-proactive', 'proactive', 'procedure_runner.py'),
    ('sotto-midday-digest', 'digest', 'procedure_runner.py'),
    ('sotto-relationship-pulse', 'pulse', 'procedure_runner.py'),
])
def test_selected_sotto_system_job_admits_only_its_packaged_procedure(
        tmp_path, monkeypatch, name, kind, runner_name):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_delivery_channel_ready', lambda label: True)
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda: [(name, '0 9 * * *', 'Run it',
                                                        'sotto-event' if kind == 'digest' else name)])
    seen = []

    adapter = rec._runtime_adapter()
    original_argv = adapter.run_argv

    def argv(runner, prompt, *_args):
        assert Path(runner[-1]).name == runner_name
        seen.append(runner)
        return original_argv(runner, prompt, *_args)

    monkeypatch.setattr(adapter, 'run_argv', argv)
    admitted = []
    original = rec._spawn_and_deliver

    def capture(*args, **kwargs):
        identity = original(*args, **kwargs)
        admitted.append(identity)
        return identity

    monkeypatch.setattr(rec, '_spawn_and_deliver', capture)
    assert rec._fire_cron_job(name, f'run-now:{name}')['ok']
    assert len(admitted) == 1 and len(seen) == 1
    saved = q.get(tmp_path, admitted[0])
    assert Path(saved['payload']['runner'][-1]).name == runner_name
    request = json.loads(saved['payload']['prompt'])
    assert request['kind'] == kind
    assert Path(request['pack']).resolve() == (HERE.parents[1] / 'sotto-chief-of-staff').resolve()
    rec._WORK_WAKE.clear()


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
    monkeypatch.setattr(rec, '_send_with_identity', lambda body, target, *_identity: (sent.append(body) is None, '', {'message_id': 'provider-1'}))
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
    monkeypatch.setitem(box.HOOKS, 'send', lambda body, target, *_identity: (sent.append(body) is None, '', {'message_id': 'accepted-1'}))
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
    monkeypatch.setitem(box.HOOKS, 'send', lambda body, target, *_identity: sent.append(body) or (True, '', {'message_id': 'm'}))
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


def test_a_graceful_stop_hands_the_lease_back_without_charging_an_attempt(tmp_path, monkeypatch, inline_runner):
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


def test_signal_handler_waits_for_real_worker_to_refund_lease(tmp_path, monkeypatch, inline_runner):
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


def test_shutdown_kills_a_child_that_ignores_its_termination_signal(tmp_path, monkeypatch, inline_runner):
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
        # Production exits after _stop_work. This test keeps the interpreter alive:
        # join the worker before clearing the stop flag or restoring DATA via monkeypatch.
        # Otherwise it can report the killed child against the next test's /data root.
        try:
            child.wait(10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(10)
            raise  # Cleanup must not turn a missing production SIGKILL into a passing test.
        with rec._WORK_LOCK:
            workers = list(rec._WORK_THREADS)
        for worker in workers:
            worker.join(10)
            assert not worker.is_alive(), 'shutdown fixture left a background worker running'
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


def test_real_subprocess_execution_is_bounded_and_saves_output(tmp_path, monkeypatch, inline_runner):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    q.enqueue(tmp_path, 'run', {'runner': [sys.executable, '-c', 'import sys; print(sys.argv[1])'],
                                         'prompt': 'synthetic-output', 'label': 'test'}, key='process')
    job = q.claim(tmp_path, rec._WORK_OWNER)
    result = rec._execute_work(job)
    assert result['text'].strip() == 'synthetic-output'
    assert not rec._WORK_PROCESSES



def test_worker_failure_retains_safe_category_without_private_stderr(tmp_path, monkeypatch, inline_runner):
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


def test_proxy_admission_busy_retries_uncharged_until_success_without_duplicate_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    rec._WORK_STOP.clear()
    sent = []
    monkeypatch.setattr(rec, '_deliver_text', lambda text, *a, **k: sent.append(text))
    exits = iter([q.MODEL_ADMISSION_BUSY_EXIT] * 3 + [0])

    class Process:
        def __init__(self, *args, **kwargs):
            self.returncode = next(exits)
        def communicate(self, **kwargs):
            return ('synthetic-output' if self.returncode == 0 else '', 'private provider body')
        def poll(self):
            return self.returncode

    monkeypatch.setattr(rec.subprocess, 'Popen', Process)
    canonical = [sys.executable, str(HERE / 'brief_runner.py')]
    deadline = time.time() + 300
    identity = q.enqueue(tmp_path, 'run', {'runner': canonical, 'prompt': '{}',
                                          'label': 'synthetic', 'decision_ids': []},
                         key='brief-capacity', valid_until=deadline)
    for attempt in range(3):
        job = q.claim(tmp_path, 'worker', now=time.time() + attempt * 61)
        assert job['id'] == identity
        rec._work_one(job)
        row = q.get(tmp_path, identity)
        assert row['status'] == 'pending' and row['attempts'] == 0
        assert row['valid_until'] == deadline and row['result'] is None
        assert sent == []
    job = q.claim(tmp_path, 'worker', now=time.time() + 3 * 61)
    rec._work_one(job)
    assert q.get(tmp_path, identity)['status'] == 'done'
    assert sent == ['synthetic-output']


def test_proxy_admission_busy_still_obeys_original_deadline_and_foreign_exit_is_charged(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    rec._WORK_STOP.clear()

    class Process:
        returncode = q.MODEL_ADMISSION_BUSY_EXIT
        def __init__(self, *args, **kwargs):
            pass
        def communicate(self, **kwargs):
            return '', 'private provider body'
        def poll(self):
            return self.returncode

    monkeypatch.setattr(rec.subprocess, 'Popen', Process)
    canonical = [sys.executable, str(HERE / 'brief_runner.py')]
    now = time.time()
    identity = q.enqueue(tmp_path, 'run', {'runner': canonical, 'prompt': '{}',
                                          'label': 'synthetic'}, key='deadline', valid_until=now + 30)
    rec._work_one(q.claim(tmp_path, 'worker'))
    row = q.get(tmp_path, identity)
    assert row['status'] == 'expired' and row['result'] is None
    assert row['valid_until'] == now + 30
    assert q.claim(tmp_path, 'later', now=now + 61) is None

    foreign = q.enqueue(tmp_path, 'run', {'runner': [sys.executable, '-c', 'pass'],
                                          'prompt': '{}', 'label': 'synthetic'}, key='foreign-78')
    rec._work_one(q.claim(tmp_path, 'worker', now=now + 62))
    assert q.get(tmp_path, foreign)['attempts'] == 1


@pytest.mark.parametrize('runner_name,kind', [('procedure_runner.py', 'run'),
                                               ('compose_notification.py', 'event')])
def test_other_declared_model_runners_refund_typed_proxy_busy(tmp_path, monkeypatch, runner_name, kind):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    rec._WORK_STOP.clear()
    class Process:
        returncode = q.MODEL_ADMISSION_BUSY_EXIT
        def __init__(self, *args, **kwargs):
            pass
        def communicate(self, **kwargs):
            return '', 'private provider response'
        def poll(self):
            return self.returncode
    monkeypatch.setattr(rec.subprocess, 'Popen', Process)
    runner = ([sys.executable, str(HERE / runner_name)] if kind == 'run'
              else [sys.executable, rec._find_sotto_script('_shared', 'scripts', runner_name)])
    payload = ({'runner': runner, 'prompt': '{}', 'label': 'synthetic', 'decision_ids': []}
               if kind == 'run' else {'bundle': {'events': []}, 'label': 'synthetic'})
    identity = q.enqueue(tmp_path, kind, payload, key=runner_name, valid_until=time.time() + 300)
    if kind == 'event':
        monkeypatch.setattr(rec, '_shared_effects', lambda: type('Effects', (), {
            'for_bundle': staticmethod(lambda bundle: {'effects': [], 'valid_until': None}),
            'valid': staticmethod(lambda effects, now: True)})())
        monkeypatch.setattr(rec, '_stage_bundle', lambda bundle: tmp_path / 'staged.json')
        monkeypatch.setattr(rec, '_event_request', lambda staged: {
            'runner': runner, 'prompt': '{}', 'label': 'synthetic', 'decision_ids': []})
    rec._work_one(q.claim(tmp_path, 'worker'))
    row = q.get(tmp_path, identity)
    assert row['status'] == 'pending' and row['attempts'] == 0


def test_admission_deferral_preserves_existing_longer_deadline(tmp_path):
    now = time.time()
    deadline = now + 20_000
    identity = q.enqueue(tmp_path, 'run', {'prompt': 'synthetic'},
                         key='long-validity', valid_until=deadline)
    job = q.claim(tmp_path, 'worker')
    assert job['id'] == identity
    # A busy refusal hours after enqueue remains eligible under the admitted job's own deadline.
    assert q.defer(tmp_path, identity, 'worker', max_age_seconds=3600, now=now + 4000) == 'pending'
    row = q.get(tmp_path, identity)
    assert row['status'] == 'pending' and row['valid_until'] == deadline
    assert row['due'] == now + 4060 and row['attempts'] == 0


def test_only_canonical_brief_runner_budget_exit_parks_one_attempt_and_welcome(tmp_path, monkeypatch):
    import onboarding
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    reports = []
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: reports.append(a))
    rec._WORK_STOP.clear()

    class DeniedProcess:
        returncode = q.MODEL_BUDGET_EXIT
        def __init__(self, *args, **kwargs):
            pass
        def communicate(self, **kwargs):
            return '', 'private account and source details'
        def poll(self):
            return self.returncode

    monkeypatch.setattr(rec.subprocess, 'Popen', DeniedProcess)
    canonical = [sys.executable, str(HERE / 'brief_runner.py')]
    identity = q.enqueue(tmp_path, 'run', {'runner': canonical, 'prompt': '{}',
                                          'label': onboarding.LABEL}, key='budget-welcome')
    rec._work_one(q.claim(tmp_path, 'budget-worker'))
    row = q.get(tmp_path, identity)
    assert row['status'] == 'failed' and row['attempts'] == 1
    assert row['error'] == q.MODEL_BUDGET_EXHAUSTED and row['payload'] == {}
    assert onboarding.status(tmp_path) == 'model_held'
    failed = [report for report in reports if report[1] == 'failed']
    assert len(failed) == 1
    assert 'model allowance reached; no retry queued' in failed[0][2]
    assert 'private account and source details' not in str(reports)
    assert q.claim(tmp_path, 'again') is None


def test_foreign_runner_exit_77_keeps_normal_retry(tmp_path, monkeypatch, inline_runner):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    rec._WORK_STOP.clear()

    class DeniedProcess:
        returncode = q.MODEL_BUDGET_EXIT
        def __init__(self, *args, **kwargs):
            pass
        def communicate(self, **kwargs):
            return '', 'private foreign stderr'
        def poll(self):
            return self.returncode

    monkeypatch.setattr(rec.subprocess, 'Popen', DeniedProcess)
    identity = q.enqueue(tmp_path, 'run', {'runner': [sys.executable, '-c', 'pass'],
                                          'prompt': '{}', 'label': 'synthetic'}, key='foreign-77')
    rec._work_one(q.claim(tmp_path, 'worker'))
    row = q.get(tmp_path, identity)
    assert row['status'] == 'pending' and row['attempts'] == 1
    assert row['error'].startswith('worker_exit_77:')


@pytest.mark.parametrize('runner_name,exit_code,terminal', [
    ('procedure_runner.py', q.MODEL_BUDGET_EXIT, True),
    ('procedure_runner.py', 1, False),
    ('brief_runner.py', q.MODEL_BUDGET_EXIT, True),
])
def test_declared_runner_budget_exit_is_terminal_only_for_exit_77(
        tmp_path, monkeypatch, runner_name, exit_code, terminal):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    rec._WORK_STOP.clear()
    class Process:
        returncode = exit_code
        def __init__(self, *args, **kwargs):
            pass
        def communicate(self, **kwargs):
            return '', 'private stderr'
        def poll(self):
            return self.returncode
    monkeypatch.setattr(rec.subprocess, 'Popen', Process)
    identity = q.enqueue(tmp_path, 'run', {
        'runner': [sys.executable, str(HERE / runner_name)], 'prompt': '{}',
        'label': 'cron:sotto-midday-digest'}, key=f'{runner_name}:{exit_code}')
    rec._work_one(q.claim(tmp_path, 'worker'))
    row = q.get(tmp_path, identity)
    assert row['status'] == ('failed' if terminal else 'pending')
    assert row['attempts'] == 1
    if terminal:
        assert row['error'] == q.MODEL_BUDGET_EXHAUSTED
    else:
        assert row['error'].startswith('worker_exit_1:')


def test_stale_budget_worker_cannot_hold_welcome_or_record_receipt(tmp_path, monkeypatch):
    import onboarding
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    reports = []
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: reports.append(a))
    identity = q.enqueue(tmp_path, 'run', {'label': onboarding.LABEL}, key='stale-budget')
    old = q.claim(tmp_path, 'old')
    current = q.claim(tmp_path, 'new', now=old['lease_until'] + 1)
    def denied(job):
        error = rec._WorkerError(q.MODEL_BUDGET_EXIT, 'private stderr')
        error.model_budget_exhausted = True
        raise error
    monkeypatch.setattr(rec, '_execute_work', denied)
    rec._work_one(old)
    assert q.get(tmp_path, identity)['owner'] == current['owner']
    assert not reports and onboarding.status(tmp_path) == 'waiting'


def test_budget_flag_cannot_discard_saved_result(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    identity = q.enqueue(tmp_path, 'run', {'label': 'synthetic'}, key='saved-budget')
    job = q.claim(tmp_path, 'worker')
    result = {'text': 'Already prepared', 'label': 'synthetic'}
    q.save_result(tmp_path, identity, 'worker', result)
    job = q.get(tmp_path, identity)
    def denied(*args, **kwargs):
        error = rec._WorkerError(q.MODEL_BUDGET_EXIT, 'private stderr')
        error.model_budget_exhausted = True
        raise error
    monkeypatch.setattr(rec, '_deliver_text', denied)
    rec._work_one(job)
    row = q.get(tmp_path, identity)
    assert row['status'] == 'ready' and row['result'] == result
    assert row['error'] != q.MODEL_BUDGET_EXHAUSTED


def test_budget_terminal_key_readmits_only_after_positive_capability(tmp_path, monkeypatch):
    import onboarding
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *parts: '/fixture/_shared/scripts/compose_brief.py')
    monkeypatch.setattr(rec, '_brief_revision', lambda kind: 'fixed')
    assert rec._managed_brief('sotto-welcome-brief', onboarding.LABEL)
    identity = q.job_id_for('run', rec._job_key(onboarding.LABEL, ''))
    q.claim(tmp_path, 'worker')
    assert q.fail(tmp_path, identity, 'worker', 'private', model_budget_exhausted=True) == 'failed'
    denied = q.get(tmp_path, identity)
    assert denied['error'] == q.MODEL_BUDGET_EXHAUSTED
    monkeypatch.setattr(rec, '_brief_model_ready', lambda: False)
    assert rec._managed_brief('sotto-welcome-brief', onboarding.LABEL) is False
    assert q.get(tmp_path, identity) == denied
    monkeypatch.setattr(rec, '_brief_model_ready', lambda: True)
    assert rec._managed_brief('sotto-welcome-brief', onboarding.LABEL)
    resumed = q.get(tmp_path, identity)
    assert resumed['status'] == 'pending' and resumed['attempts'] == 0


def test_welcome_capability_race_does_not_claim_nonexistent_work(tmp_path, monkeypatch):
    import onboarding
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *parts: '/fixture/_shared/scripts/compose_brief.py')
    monkeypatch.setattr(rec, '_brief_revision', lambda kind: 'fixed')
    assert onboarding.tick(tmp_path, True,
                           lambda: rec._managed_brief('sotto-welcome-brief', onboarding.LABEL), 100) == 'started'
    identity = q.job_id_for('run', rec._job_key(onboarding.LABEL, ''))
    q.claim(tmp_path, 'worker')
    assert q.fail(tmp_path, identity, 'worker', 'private', model_budget_exhausted=True) == 'failed'
    onboarding.model_budget_held(tmp_path)
    denied = q.get(tmp_path, identity)
    monkeypatch.setattr(rec, '_brief_model_ready', lambda: False)
    assert onboarding.tick(tmp_path, True,
                           lambda: rec._managed_brief('sotto-welcome-brief', onboarding.LABEL),
                           2000, model_ready=lambda: True) == 'model_held'
    state = json.loads((tmp_path / 'config/onboarding.json').read_text())
    assert state['phase'] == 'model_held' and state['attempts'] == 0
    assert q.get(tmp_path, identity) == denied


def test_provider_recovery_is_bounded_persistent_and_truthful(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(q.time, 'time', lambda: clock[0])
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    reports = []
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: reports.append(a))
    identity = q.enqueue(tmp_path, 'event', {'bundle': {}}, key='provider-down', valid_until=10000)
    def fail(job):
        error = rec._WorkerError(q.PROVIDER_RETRY_EXIT, 'private provider payload')
        error.retry_provider = True
        raise error
    monkeypatch.setattr(rec, '_execute_work', fail)
    for attempt in range(q.MAX_PROVIDER_RECOVERIES + 1):
        job = q.claim(tmp_path, 'worker')
        assert job and job['id'] == identity
        rec._work_one(job)
        row = q.get(tmp_path, identity)
        if attempt < q.MAX_PROVIDER_RECOVERIES:
            assert row['status'] == 'pending'
            assert row['provider_recoveries'] == attempt + 1
            assert reports[-1][2].endswith('; retry queued')
            clock[0] = row['due']
        else:
            assert row['status'] == 'failed' and row['payload'] == {}
            assert reports[-1][2].endswith('; attempt limit reached; no retry queued')
    assert 'private provider payload' not in str(reports)
    assert q.claim(tmp_path, 'worker', now=clock[0] + 2000) is None


def test_provider_retry_never_extends_deadline_or_refunds_handoff(tmp_path, monkeypatch):
    monkeypatch.setattr(q.time, 'time', lambda: 1000.0)
    identity = q.enqueue(tmp_path, 'event', {}, key='expiring', valid_until=1059)
    q.claim(tmp_path, 'worker')
    assert q.fail(tmp_path, identity, 'worker', 'provider refused', retry_provider=True) == 'expired'
    assert q.get(tmp_path, identity)['payload'] == {}
    identity = q.enqueue(tmp_path, 'event', {}, key='handoff')
    job = q.claim(tmp_path, 'worker')
    q.save_result(tmp_path, identity, 'worker', {'text': 'already composed'})
    for attempt in range(q.MAX_ATTEMPTS):
        assert q.fail(tmp_path, identity, 'worker', 'handoff failed', retry_provider=True)
        row = q.get(tmp_path, identity)
        assert row['provider_recoveries'] == 0
        if attempt < q.MAX_ATTEMPTS - 1:
            job = q.claim(tmp_path, 'worker', now=row['due'])
            assert job['result']['text'] == 'already composed'
    assert row['status'] == 'failed'


def test_event_recovers_after_three_503s_through_real_worker_and_model_accounting(tmp_path, monkeypatch):
    """Provider and channel are fixtures; the queue, subprocess, composer and model ledger are real."""
    import textwrap
    pack = HERE.parents[1] / 'sotto-chief-of-staff'
    worker = tmp_path / 'notification-fixture.py'
    worker.write_text(textwrap.dedent(f'''
        import sys, json
        from urllib.error import HTTPError
        sys.path.insert(0, {str(pack / '_shared/scripts')!r})
        import compose_notification as notification
        import gemini, model_work
        item = {{'id': 'fixture', 'kind': 'urgent', 'person': 'Sam', 'detail': 'Review the deck'}}
        notification.candidates = lambda *a: [item]
        notification.enrich = lambda items, now: items
        gemini.provider_key = lambda *a: 'fixture-key'
        def respond(*a, **k):
            if model_work.current()['attempt'] <= 3:
                raise HTTPError('https://fixture.invalid', 503, 'private provider body', {{}}, None)
            return json.dumps({{'items': [{{'id': 'fixture', 'text': 'Sam needs the deck reviewed.', 'draft': '', 'decline': ''}}]}})
        gemini._gemini_once = respond
        sys.exit(notification.main())
    '''))
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_LLM_STUB', raising=False)
    monkeypatch.setenv('SOTTO_COMPOSE_PROVIDER', 'gemini')
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *a: str(worker))
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'test:owner')
    sent = []
    monkeypatch.setattr(rec, '_send_with_identity', lambda body, target, *_identity:
                        (sent.append(body) is None, '', {'message_id': 'fixture-accepted'}))
    monkeypatch.setattr(rec, '_on_delivered', lambda payload: True)
    identity = q.enqueue(tmp_path, 'event', {'bundle': {'events': []}}, key='recover-after-503',
                         valid_until=time.time() + 3600)
    for attempt in range(4):
        row = q.get(tmp_path, identity)
        job = q.claim(tmp_path, 'worker', now=row['due'])
        assert job and job['id'] == identity
        rec._work_one(job)
        row = q.get(tmp_path, identity)
        assert row['status'] == ('pending' if attempt < 3 else 'done'), row['error']
    assert len(sent) == 1 and 'Sam needs the deck reviewed.' in sent[0]
    assert row['attempts'] == 4 and row['provider_recoveries'] == 3
    with sqlite3.connect(tmp_path / 'events/model-work.sqlite3') as db:
        assert db.execute('SELECT status FROM attempts ORDER BY attempt').fetchall() == [
            ('http_503',), ('http_503',), ('http_503',), ('succeeded',)]
    assert q.claim(tmp_path, 'worker', now=time.time() + 2000) is None


@pytest.mark.parametrize('label', ['cron:sotto-relationship-pulse', 'run-now:sotto-relationship-pulse'])
@pytest.mark.parametrize('target', ['photon', 'telegram'])
def test_scheduled_pulse_binds_current_proof_before_child_spawn(tmp_path, monkeypatch, label, target):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *a: None)
    monkeypatch.setattr(rec, '_deliver_target', lambda: target)
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: 'b' * 64)
    authority = {'complete': True, 'revision': 'a' * 64}
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: authority.copy())
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    monkeypatch.setattr(rec, '_read_usage', lambda *a: None)
    runner = [sys.executable, str(HERE / 'procedure_runner.py')]
    job = {'id': 'c' * 32, 'kind': 'run', 'payload': {'runner': runner,
           'prompt': json.dumps({'pack': str(HERE.parents[1] / 'sotto-chief-of-staff'), 'kind': 'pulse'}),
           'label': label}}
    calls = []
    class Child:
        returncode = 0
        def __init__(self, argv, **kwargs):
            calls.append(kwargs['env'])
        def communicate(self, **kwargs):
            return 'Your weekly relationships', ''
        def poll(self):
            return 0
    monkeypatch.setattr(rec.subprocess, 'Popen', Child)
    authority['complete'] = False
    with pytest.raises(rec._WorkDeferredError):
        rec._execute_work(job)
    assert not calls
    authority['complete'] = True
    result = rec._execute_work(job)
    assert json.loads(calls[0]['SOTTO_SKILL_COMPOSITION_PROOF']) == {
        'source_revision': 'a' * 64, 'route_digest': 'b' * 64}
    assert calls[0]['SOTTO_DELIVERY_RUN_ID'] == job['id']
    assert result['source_revision_at_composition'] == 'a' * 64
    assert result['route_digest_at_composition'] == 'b' * 64
    assert rec._composition_evidence('background:sotto-relationship-pulse') is None


@pytest.mark.parametrize('changed', ['none', 'source_generation', 'route'])
def test_scheduled_pulse_quiet_delivery_rechecks_proof_and_restarts_once(tmp_path, monkeypatch, changed):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'telegram')
    current = {'revision': 'a' * 64, 'route': 'b' * 64}
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {'complete': True, 'revision': current['revision']})
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: current['route'])
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(gallery_available=lambda: False))
    monkeypatch.setattr(rec, '_load_shared_lib', lambda name: SimpleNamespace(prepare=lambda *a, **k: None))
    box = rec.OUTBOX
    sent = []
    ready = [False]
    monkeypatch.setitem(box.HOOKS, 'valid', rec._delivery_valid)
    monkeypatch.setitem(box.HOOKS, 'delivery_ready', lambda payload: ready[0])
    monkeypatch.setitem(box.HOOKS, 'send', lambda body, target, *_, **_context: (sent.append((body, target)) is None, '', {'message_id': 'm'}))
    monkeypatch.setitem(box.HOOKS, 'record', lambda *a, **k: None)
    monkeypatch.setitem(box.HOOKS, 'on_delivered', lambda payload: True)
    monkeypatch.setitem(box.HOOKS, 'on_invalid', lambda payload: True)
    text, label, ident = 'Your weekly relationships', 'cron:sotto-relationship-pulse', 'd' * 32
    kwargs = {'run_id': ident, 'source_revision_at_composition': current['revision'],
              'route_digest_at_composition': current['route'],
              'source_text_sha256': hashlib.sha256(text.encode()).hexdigest()}
    assert rec._deliver_text(text, label, **kwargs) is False
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['payload']['prep_source_revision'] == 'a' * 64
    assert row['payload']['prep_route_digest'] == 'b' * 64
    assert row['attempts'] == 0 and not sent
    if changed == 'source_generation':
        current['revision'] = 'e' * 64  # disable/regrant: allowed booleans alone are insufficient
    elif changed == 'route':
        current['route'] = 'f' * 64
    ready[0] = True
    # A later process drains the durable row, without recomputing or restoring authority.
    box.drain()
    box.drain()
    assert sent == ([(text, 'telegram')] if changed == 'none' else [])
    row = json.loads(Path(box.path()).read_text())['rows'][0]
    assert row['status'] == ('delivered' if changed == 'none' else 'superseded')


@pytest.mark.parametrize('runner_name,terminal', [('brief_runner.py', True), ('procedure_runner.py', False)])
def test_uncertain_brief_exit_stops_worker_retry_only_for_brief(tmp_path, monkeypatch, runner_name, terminal):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    reports = []
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: reports.append(a))
    rec._WORK_STOP.clear()
    class Process:
        returncode = q.MODEL_OUTCOME_UNCERTAIN_EXIT
        def __init__(self, *args, **kwargs):
            pass
        def communicate(self, **kwargs):
            return '', 'private provider details'
        def poll(self):
            return self.returncode
    monkeypatch.setattr(rec.subprocess, 'Popen', Process)
    runner = [sys.executable, str(HERE / runner_name)]
    identity = q.enqueue(tmp_path, 'run', {'runner': runner, 'prompt': '{}',
                        'label': 'cron:sotto-morning-brief'}, key='uncertain-brief')
    rec._work_one(q.claim(tmp_path, 'worker'))
    row = q.get(tmp_path, identity)
    assert row['status'] == ('failed' if terminal else 'pending')
    assert row['attempts'] == 1
    assert 'private' not in str(reports)
    if terminal:
        assert row['error'] == 'model_outcome_uncertain'
        assert row['payload'] == {}
        assert q.claim(tmp_path, 'restart', now=time.time() + 3600) is None
        assert 'model outcome uncertain; no retry queued' in reports[-1][2]
