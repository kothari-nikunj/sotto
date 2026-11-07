"""Cursor, permanent slot fence and ordinary work share one durable transaction."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest


MODULE_PATH = Path(__file__).parents[1] / '_shared' / 'lib' / 'work_queue.py'
spec = importlib.util.spec_from_file_location('cursor_work_queue', MODULE_PATH)
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
SLOT = datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc)
NEXT = SLOT + timedelta(days=1)
ROUTE = {'platform': 'imessage', 'chat_id': 'iMessage;-;+15555550123', 'thread_id': None}
PAYLOAD = {'runner': [sys.executable, '/app/trigger-receiver/routine_runner.py'],
           'prompt': 'Synthetic routine', 'label': 'routine:native-job-1',
           'decision_ids': []}
BIND = {'owner_scope': 'tenant-1:owner-1', 'profile_id': 'profile-default',
        'legacy_id': 'native-job-1', 'activation_epoch': 'activated-1',
        'source_revision': 'a' * 64, 'owner_route': ROUTE}


def bind(root, **changes):
    return queue.bind_routine_cursor(root, **{**BIND, 'recorded_at_utc': SLOT, **changes})


def admit(root, **changes):
    return queue.admit_routine_cursor_slot(root, **{
        **BIND, 'expected_recorded_at_utc': SLOT,
        'next_provisional_at_utc': NEXT, 'payload': PAYLOAD, **changes})


def counts(root):
    with queue._db(root) as db:
        return tuple(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                     for table in ('routine_cursors', 'routine_slots', 'jobs'))


def test_reopen_replay_and_pending_blocks_later_slot(tmp_path):
    assert bind(tmp_path)['generation'] == 0
    first = admit(tmp_path)
    assert first['admission'] == 'new'
    assert first['cursor']['current_utc'] == '2026-11-02T08:30:00.000000Z'
    assert first['cursor']['pending_job_id'] == first['job_id']
    assert counts(tmp_path) == (1, 1, 1)
    with queue._db(tmp_path) as db:
        cursor = dict(db.execute('SELECT * FROM routine_cursors').fetchone())
    assert 'Synthetic routine' not in str(cursor)
    assert 'iMessage;-;' not in str(cursor)
    assert bind(tmp_path)['pending_job_id'] == first['job_id']
    assert admit(tmp_path)['admission'] == 'existing'
    with pytest.raises(RuntimeError, match='unresolved pending'):
        admit(tmp_path, expected_recorded_at_utc=NEXT,
              next_provisional_at_utc=NEXT + timedelta(days=1))
    with pytest.raises(RuntimeError, match='protected CAS'):
        queue.admit_routine_slot(tmp_path, **{
            **BIND, 'scheduled_at_utc': NEXT, 'payload': PAYLOAD})
    assert counts(tmp_path) == (1, 1, 1)


def test_changed_binding_payload_and_successor_conflict_without_mutation(tmp_path):
    bind(tmp_path)
    first = admit(tmp_path)
    for change in ({'activation_epoch': 'activated-2'},
                   {'source_revision': 'b' * 64},
                   {'owner_route': {**ROUTE, 'chat_id': 'iMessage;-;+15555550999'}}):
        with pytest.raises(RuntimeError, match='binding changed'):
            admit(tmp_path, **change)
    for change in ({'payload': {**PAYLOAD, 'prompt': 'Changed'}},
                   {'next_provisional_at_utc': NEXT + timedelta(days=1)}):
        with pytest.raises(RuntimeError):
            admit(tmp_path, **change)
    with pytest.raises(RuntimeError, match='prior binding'):
        bind(tmp_path, source_revision='b' * 64)
    assert admit(tmp_path)['job_id'] == first['job_id']
    assert counts(tmp_path) == (1, 1, 1)


def test_concurrent_connections_cas_one_job(tmp_path):
    bind(tmp_path)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: admit(tmp_path), range(6)))
    assert [r['admission'] for r in results].count('new') == 1
    assert [r['admission'] for r in results].count('existing') == 5
    assert len({r['job_id'] for r in results}) == 1
    assert counts(tmp_path) == (1, 1, 1)


@pytest.mark.parametrize('failing_step', ['job', 'cursor'])
def test_transaction_failure_rolls_back_cursor_slot_and_work(tmp_path, failing_step):
    bind(tmp_path)
    with queue._db(tmp_path) as db:
        if failing_step == 'job':
            db.execute("""CREATE TRIGGER fail_work BEFORE INSERT ON jobs
                       BEGIN SELECT RAISE(ABORT, 'synthetic job failure'); END""")
        else:
            db.execute("""CREATE TRIGGER fail_cursor BEFORE UPDATE ON routine_cursors
                       BEGIN SELECT RAISE(ABORT, 'synthetic cursor failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic'):
        admit(tmp_path)
    assert counts(tmp_path) == (1, 0, 0)
    assert bind(tmp_path)['generation'] == 0
    with queue._db(tmp_path) as db:
        db.execute('DROP TRIGGER ' + ('fail_work' if failing_step == 'job' else 'fail_cursor'))
    assert admit(tmp_path)['admission'] == 'new'
    assert counts(tmp_path) == (1, 1, 1)


def test_child_process_exit_before_commit_rolls_back_all_three(tmp_path):
    # Open the same real DB in another process, insert all three records and
    # terminate without committing. Reopen must see only the earlier binding.
    bind(tmp_path)
    child = '''
import importlib.util, os, pathlib, sys, time
spec=importlib.util.spec_from_file_location('queue',sys.argv[1])
queue=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue)
with queue._db(pathlib.Path(sys.argv[2])) as db:
    row=db.execute('SELECT * FROM routine_cursors').fetchone()
    db.execute('INSERT INTO routine_slots(slot_hash,job_id,request_digest,admitted,routine_hash) '
               'VALUES (?,?,?,?,?)',('slot','job','digest',time.time(),row['cursor_hash']))
    db.execute("INSERT INTO jobs(id,kind,payload,status,created,due,priority) "
               "VALUES ('job','run','{}','pending',0,0,20)")
    db.execute("UPDATE routine_cursors SET current_utc='future',pending_job_id='job', "
               "pending_slot_utc='now',pending_provisional_utc='future',generation=1")
    os._exit(0)
'''
    run = subprocess.run([sys.executable, '-c', child, str(MODULE_PATH), str(tmp_path)],
                         check=False, timeout=10)
    assert run.returncode == 0
    assert counts(tmp_path) == (1, 0, 0)
    assert bind(tmp_path)['generation'] == 0
    assert admit(tmp_path)['admission'] == 'new'


def test_child_process_exit_after_commit_reopens_all_three(tmp_path):
    child = '''
import importlib.util, os, pathlib, sys
from datetime import datetime, timezone
spec=importlib.util.spec_from_file_location('queue',sys.argv[1])
queue=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue)
root=pathlib.Path(sys.argv[2])
common=dict(owner_scope='tenant-1:owner-1',profile_id='profile-default',
            legacy_id='native-job-1',activation_epoch='activated-1',
            source_revision='a'*64,
            owner_route={'platform':'imessage','chat_id':'iMessage;-;+15555550123',
                         'thread_id':None})
slot=datetime(2026,11,1,8,30,tzinfo=timezone.utc)
next_slot=datetime(2026,11,2,8,30,tzinfo=timezone.utc)
queue.bind_routine_cursor(root,**common,recorded_at_utc=slot)
queue.admit_routine_cursor_slot(root,**common,expected_recorded_at_utc=slot,
    next_provisional_at_utc=next_slot,
    payload={'runner':[sys.executable,'/app/trigger-receiver/routine_runner.py'],
             'prompt':'Synthetic routine','label':'routine:native-job-1','decision_ids':[]})
os._exit(0)
'''
    run = subprocess.run([sys.executable, '-c', child, str(MODULE_PATH), str(tmp_path)],
                         check=False, timeout=10)
    assert run.returncode == 0
    assert counts(tmp_path) == (1, 1, 1)
    assert admit(tmp_path)['admission'] == 'existing'


def test_terminal_prune_leaves_pending_and_slot_evidence(tmp_path):
    bind(tmp_path)
    first = admit(tmp_path)
    with queue._db(tmp_path) as db:
        db.execute("UPDATE jobs SET status='failed',finished=? WHERE id=?",
                   (time.time() - queue.RETENTION_SECONDS - 100, first['job_id']))
    queue.claim(tmp_path, 'maintenance', now=time.time())
    assert queue.get(tmp_path, first['job_id']) is None
    replay = admit(tmp_path)
    assert replay['admission'] == 'held'
    assert replay['work_status'] == 'failed'
    assert replay['cursor']['pending_job_id'] == first['job_id']
    assert counts(tmp_path) == (1, 1, 0)
    with pytest.raises(RuntimeError, match='unresolved pending'):
        admit(tmp_path, expected_recorded_at_utc=NEXT,
              next_provisional_at_utc=NEXT + timedelta(days=1))


def test_job_done_alone_does_not_reanchor_cursor(tmp_path):
    bind(tmp_path)
    first = admit(tmp_path)
    with queue._db(tmp_path) as db:
        db.execute("UPDATE jobs SET status='done',finished=? WHERE id=?",
                   (time.time(), first['job_id']))
    assert admit(tmp_path)['admission'] == 'terminal'
    with pytest.raises(RuntimeError, match='unresolved pending'):
        admit(tmp_path, expected_recorded_at_utc=NEXT,
              next_provisional_at_utc=NEXT + timedelta(days=1))
    assert counts(tmp_path) == (1, 1, 1)


def test_legacy_unattributable_fence_holds_binding(tmp_path):
    with queue._db(tmp_path) as db:
        db.execute("INSERT INTO routine_slots(slot_hash,job_id,request_digest,admitted) "
                   "VALUES ('old-slot','old-job','old-digest',0)")
    with pytest.raises(RuntimeError, match='history requires reconciliation'):
        bind(tmp_path)
    assert counts(tmp_path) == (0, 1, 0)


def test_new_attributable_fence_for_same_routine_holds_binding(tmp_path):
    queue.admit_routine_slot(tmp_path, **{**BIND, 'scheduled_at_utc': SLOT,
                                          'payload': PAYLOAD})
    with pytest.raises(RuntimeError, match='history requires reconciliation'):
        bind(tmp_path)
