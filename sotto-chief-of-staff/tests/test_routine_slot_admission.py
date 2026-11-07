"""Routine slot admission shares WorkQueue's durable transaction and retention."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest


MODULE_PATH = Path(__file__).parents[1] / '_shared' / 'lib' / 'work_queue.py'
spec = importlib.util.spec_from_file_location('routine_slot_work_queue', MODULE_PATH)
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)


def request(**changes):
    value = {
        'owner_scope': 'tenant-1:owner-1', 'profile_id': 'profile-default',
        'legacy_id': 'native-job-1',
        'scheduled_at_utc': datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc),
        'activation_epoch': 'activated-1', 'source_revision': 'a' * 64,
        'owner_route': {'platform': 'imessage', 'chat_id': 'iMessage;-;+15555550123',
                        'thread_id': None},
        'payload': {'runner': [sys.executable, '/app/trigger-receiver/routine_runner.py'],
                    'prompt': 'Synthetic routine', 'label': 'routine:native-job-1',
                    'decision_ids': []},
        'not_before': None, 'valid_until': None, 'priority': 20,
    }
    value.update(changes)
    return value


def slot_key():
    return queue._routine_slot_key(
        'tenant-1:owner-1', 'profile-default', 'native-job-1',
        '2026-11-01T08:30:00.000000Z')


def test_reopen_and_process_exit_preserve_one_admitted_work_row(tmp_path):
    child = '''
import importlib.util, os, pathlib, sys
from datetime import datetime, timezone
spec=importlib.util.spec_from_file_location('queue',sys.argv[1])
queue=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue)
queue.admit_routine_slot(pathlib.Path(sys.argv[2]),
    owner_scope='tenant-1:owner-1',profile_id='profile-default',legacy_id='native-job-1',
    scheduled_at_utc=datetime(2026,11,1,8,30,tzinfo=timezone.utc),
    activation_epoch='activated-1',source_revision='a'*64,
    owner_route={'platform':'imessage','chat_id':'iMessage;-;+15555550123','thread_id':None},
    payload={'runner':[sys.executable,'/app/trigger-receiver/routine_runner.py'],
             'prompt':'Synthetic routine','label':'routine:native-job-1','decision_ids':[]})
os._exit(0)
'''
    run = subprocess.run([sys.executable, '-c', child, str(MODULE_PATH), str(tmp_path)],
                         check=False, timeout=10)
    assert run.returncode == 0
    replay = queue.admit_routine_slot(tmp_path, **request())
    assert replay['admission'] == 'existing'
    job = queue.get(tmp_path, replay['job_id'])
    assert job['kind'] == 'run'
    assert job['payload'] == request()['payload']
    assert job['status'] == 'pending'
    with queue._db(tmp_path) as db:
        assert db.execute('SELECT COUNT(*) FROM routine_slots').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 1


def test_duplicate_concurrent_admissions_and_changed_bindings_conflict(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        admissions = list(pool.map(lambda _index: queue.admit_routine_slot(
            tmp_path, **request()), range(4)))
    assert [item['admission'] for item in admissions].count('new') == 1
    assert [item['admission'] for item in admissions].count('existing') == 3
    job_id = admissions[0]['job_id']
    same_utc_in_other_zone = datetime.fromisoformat('2026-11-01T01:30:00-07:00')
    assert queue.admit_routine_slot(
        tmp_path, **request(scheduled_at_utc=same_utc_in_other_zone))['job_id'] == job_id
    for changed in (
        request(payload={**request()['payload'], 'prompt': 'Different prompt'}),
        request(owner_route={**request()['owner_route'], 'chat_id': 'another recipient'}),
        request(source_revision='b' * 64),
        request(activation_epoch='activated-2'),
        request(valid_until=1783000000.0),
    ):
        with pytest.raises(RuntimeError, match='conflicts with prior admission'):
            queue.admit_routine_slot(tmp_path, **changed)
    assert queue.get(tmp_path, job_id)['payload'] == request()['payload']
    with queue._db(tmp_path) as db:
        fence = dict(db.execute('SELECT * FROM routine_slots').fetchone())
    assert set(fence) == {'slot_hash', 'job_id', 'request_digest', 'admitted',
                          'last_work_status', 'routine_hash', 'completion_utc',
                          'handoff_kind', 'handoff_digest'}
    assert all(fence[name] is None for name in ('completion_utc', 'handoff_kind', 'handoff_digest'))
    assert 'Synthetic routine' not in str(fence)
    assert 'iMessage;-;' not in str(fence)


def test_initial_work_insert_failure_rolls_back_slot_fence(tmp_path):
    with queue._db(tmp_path) as db:
        db.execute('''CREATE TRIGGER refuse_routine_work BEFORE INSERT ON jobs
                      BEGIN SELECT RAISE(ABORT, 'synthetic job insert failure'); END''')
    with pytest.raises(sqlite3.IntegrityError, match='synthetic job insert failure'):
        queue.admit_routine_slot(tmp_path, **request())
    with queue._db(tmp_path) as db:
        assert db.execute('SELECT COUNT(*) FROM routine_slots').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
        db.execute('DROP TRIGGER refuse_routine_work')
    admitted = queue.admit_routine_slot(tmp_path, **request())
    assert admitted['admission'] == 'new'
    assert queue.get(tmp_path, admitted['job_id'])['status'] == 'pending'


def test_process_exit_before_transaction_commit_leaves_no_slot_or_work(tmp_path):
    child = '''
import importlib.util, os, pathlib, sys, time
spec=importlib.util.spec_from_file_location('queue',sys.argv[1])
queue=importlib.util.module_from_spec(spec);spec.loader.exec_module(queue)
with queue._db(pathlib.Path(sys.argv[2])) as db:
    db.execute('INSERT INTO routine_slots(slot_hash,job_id,request_digest,admitted) '
               'VALUES (?,?,?,?)', ('synthetic-slot','synthetic-job','synthetic-request',time.time()))
    os._exit(0)
'''
    run = subprocess.run([sys.executable, '-c', child, str(MODULE_PATH), str(tmp_path)],
                         check=False, timeout=10)
    assert run.returncode == 0
    with queue._db(tmp_path) as db:
        assert db.execute('SELECT COUNT(*) FROM routine_slots').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    admitted = queue.admit_routine_slot(tmp_path, **request())
    assert admitted['admission'] == 'new'


def test_missing_work_without_terminal_proof_is_held_not_recreated(tmp_path):
    admitted = queue.admit_routine_slot(tmp_path, **request())
    with queue._db(tmp_path) as db:
        db.execute('DELETE FROM jobs WHERE id=?', (admitted['job_id'],))
    assert queue.admit_routine_slot(tmp_path, **request()) == {
        'job_id': admitted['job_id'], 'admission': 'held', 'work_status': 'admitted',
    }
    assert queue.get(tmp_path, admitted['job_id']) is None
    with pytest.raises(RuntimeError, match='protected admission'):
        queue.enqueue(tmp_path, 'run', request()['payload'], key=slot_key())


def test_unrelated_work_retains_explicit_terminal_retry(tmp_path):
    job_id = queue.enqueue(tmp_path, 'run', {'generation': 1}, key='other-work')
    with queue._db(tmp_path) as db:
        db.execute("UPDATE jobs SET status='failed',attempts=?,finished=? WHERE id=?",
                   (queue.MAX_ATTEMPTS, time.time(), job_id))
    with pytest.raises(RuntimeError, match='terminal'):
        queue.enqueue(tmp_path, 'run', {'generation': 2}, key='other-work')
    assert queue.enqueue(tmp_path, 'run', {'generation': 2}, key='other-work',
                         retry_terminal=True) == job_id
    assert queue.get(tmp_path, job_id)['payload'] == {'generation': 2}
    assert queue.get(tmp_path, job_id)['attempts'] == 0


@pytest.mark.parametrize('terminal', ['done', 'failed', 'expired'])
def test_terminal_and_pruned_work_never_replenishes_slot_attempts(tmp_path, terminal):
    now = time.time()
    options = request(valid_until=now - 1 if terminal == 'expired' else None)
    admitted = queue.admit_routine_slot(tmp_path, **options)
    job_id = admitted['job_id']
    # Claim only once the recorded occurrence is due, independent of wall-clock date.
    now = max(time.time(), options['scheduled_at_utc'].timestamp()) + 1
    if terminal == 'expired':
        assert queue.claim(tmp_path, 'worker', now=now) is None
    else:
        lease = queue.claim(tmp_path, 'worker', now=now)
        assert lease['id'] == job_id
        if terminal == 'done':
            queue.save_result(tmp_path, job_id, 'worker', {
                '_routine_completed_at': datetime.fromtimestamp(now, timezone.utc).isoformat()})
            assert queue.finish(tmp_path, job_id, 'worker', routine_handoff={'kind': 'silent'})
        else:
            with queue._db(tmp_path) as db:
                db.execute('UPDATE jobs SET attempts=? WHERE id=?',
                           (queue.MAX_ATTEMPTS, job_id))
            assert queue.fail(tmp_path, job_id, 'worker', 'synthetic failure', now=now) == 'failed'
    assert queue.admit_routine_slot(tmp_path, **options) == {
        'job_id': job_id, 'admission': 'terminal', 'work_status': terminal,
    }
    with queue._db(tmp_path) as db:
        db.execute('UPDATE jobs SET finished=? WHERE id=?',
                   (now - queue.RETENTION_SECONDS - 1, job_id))
    assert queue.claim(tmp_path, 'maintenance', now=now) is None
    assert queue.get(tmp_path, job_id) is None
    assert queue.admit_routine_slot(tmp_path, **options) == {
        'job_id': job_id, 'admission': 'held', 'work_status': terminal,
    }
    with pytest.raises(RuntimeError, match='protected admission'):
        queue.enqueue(tmp_path, 'run', request()['payload'], key=slot_key(), retry_terminal=True)
    assert queue.get(tmp_path, job_id) is None
    with queue._db(tmp_path) as db:
        assert db.execute('SELECT COUNT(*) FROM routine_slots').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
