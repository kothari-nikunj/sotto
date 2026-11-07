"""Fresh fixed profile must not mistake pruned work for a new tenant volume."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('fixed_five_slot_test', HERE / 'fixed_five.py')
fixed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixed)
WORK = HERE.parents[1] / 'sotto-chief-of-staff' / '_shared' / 'lib' / 'work_queue.py'
WORK_SPEC = importlib.util.spec_from_file_location('fixed_queue_preflight_test', WORK)
queue = importlib.util.module_from_spec(WORK_SPEC)
WORK_SPEC.loader.exec_module(queue)


def test_compact_fixed_history_blocks_fresh_profile_after_work_prunes(tmp_path):
    job = queue.enqueue_fixed_slot(
        tmp_path, {'label': 'cron:sotto-midday-digest', 'prompt': '{}',
                   'runner': ['python', 'procedure_runner.py']},
        key='digest:day', authority_digest='a' * 64, schedule_digest='c' * 64,
        name='sotto-midday-digest', wall_slot='2026-11-01')
    assert queue.claim(tmp_path, 'worker')['id'] == job
    assert queue.finish(tmp_path, job, 'worker')
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        db.execute('UPDATE jobs SET finished=0 WHERE id=?', (job,))
    queue.claim(tmp_path, 'other', now=queue.RETENTION_SECONDS + 1)
    assert queue.get(tmp_path, job) is None
    with pytest.raises(fixed.ProfileUnavailableError):
        fixed.validate_work_queue(tmp_path, {})


def test_wake_only_history_blocks_fresh_profile_after_work_prunes(tmp_path):
    job = queue.enqueue(
        tmp_path, 'run',
        {'label': 'brief:sotto-morning-brief', 'prompt': '{}',
         'runner': ['python', 'brief_runner.py']},
        key='brief:day', shared_fixed_brief_wake=True)
    assert queue.claim(tmp_path, 'worker')['id'] == job
    assert queue.finish(tmp_path, job, 'worker')
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        db.execute('UPDATE jobs SET finished=0 WHERE id=?', (job,))
    queue.claim(tmp_path, 'other', now=queue.RETENTION_SECONDS + 1)
    assert queue.get(tmp_path, job) is None
    with pytest.raises(fixed.ProfileUnavailableError):
        fixed.validate_work_queue(tmp_path, {})
