"""Fixed receiver slots are durable work admission, never delivery receipts."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parents[1] / '_shared' / 'lib' / 'work_queue.py'
SPEC = importlib.util.spec_from_file_location('fixed_slot_work_queue', HERE)
work = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(work)
AUTH = 'a' * 64
SCHEDULE = 'c' * 64
PAYLOAD = {'runner': ['python', 'brief.py'], 'prompt': 'fixture', 'label': 'cron:sotto-morning-brief'}


def admit(root, key='brief:morning:day:revision1', **kwargs):
    kwargs.setdefault('schedule_digest', SCHEDULE)
    return work.enqueue_fixed_slot(root, PAYLOAD, key=key, authority_digest=AUTH,
                                   name='sotto-morning-brief', wall_slot='2026-11-01',
                                   **kwargs)


def test_fixed_admission_dedup_and_separate_manual_work(tmp_path):
    first = admit(tmp_path)
    assert first == admit(tmp_path)
    slot = work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')
    assert slot['job_id'] == first and slot['revision'] == 0
    with pytest.raises(RuntimeError, match='fixed slot requires'):
        work.enqueue(tmp_path, 'run', PAYLOAD, key='brief:morning:day:revision1')
    manual = work.enqueue(tmp_path, 'run', {'label': 'run-now:sotto-morning-brief'},
                          key='manual:distinct')
    assert manual != first
    with pytest.raises(RuntimeError, match='authority changed'):
        work.fixed_slot(tmp_path, authority_digest='b' * 64,
                        name='sotto-morning-brief', wall_slot='2026-11-01')


def test_pending_revision_replacement_is_atomic_and_leased_result_is_preserved(tmp_path):
    old = admit(tmp_path)
    new = admit(tmp_path, 'brief:morning:day:revision2', replace_pending=True)
    assert old != new and work.get(tmp_path, old) is None
    claimed = work.claim(tmp_path, 'worker')
    assert claimed['id'] == new
    # A third revision cannot replace composition already in progress.
    assert admit(tmp_path, 'brief:morning:day:revision3', replace_pending=True) == new
    assert work.get(tmp_path, new)['status'] == 'leased'


def test_terminal_model_budget_needs_explicit_readmission(tmp_path):
    job = admit(tmp_path)
    assert work.claim(tmp_path, 'worker')['id'] == job
    assert work.fail(tmp_path, job, 'worker', 'model unavailable',
                     model_budget_exhausted=True) == 'failed'
    with pytest.raises(RuntimeError, match='work request is terminal'):
        admit(tmp_path)
    assert work.get(tmp_path, job)['status'] == 'failed'
    assert admit(tmp_path, retry_terminal=True) == job
    assert work.get(tmp_path, job)['status'] == 'pending'


def test_cron_first_explicit_bridge_wake_shares_identity_and_budget(tmp_path):
    job = admit(tmp_path)
    wake = {'runner': ['python', 'brief.py'], 'prompt': 'wake input',
            'label': 'brief:sotto-morning-brief'}
    assert work.enqueue(tmp_path, 'run', wake, key='brief:morning:day:revision1',
                        retry_terminal=True, shared_fixed_brief_wake=True) == job
    assert work.claim(tmp_path, 'worker')['id'] == job
    assert work.fail(tmp_path, job, 'worker', 'model unavailable',
                     model_budget_exhausted=True) == 'failed'
    with pytest.raises(RuntimeError, match='work request is terminal'):
        work.enqueue(tmp_path, 'run', wake, key='brief:morning:day:revision1',
                     shared_fixed_brief_wake=True)
    assert work.enqueue(tmp_path, 'run', wake, key='brief:morning:day:revision1',
                        retry_terminal=True, shared_fixed_brief_wake=True) == job
    assert work.get(tmp_path, job)['status'] == 'pending'
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')['job_id'] == job
    with pytest.raises(RuntimeError, match='fixed brief wake identity changed'):
        work.enqueue(tmp_path, 'run', {'label': 'brief:sotto-evening-brief'},
                     key='brief:morning:day:revision1', shared_fixed_brief_wake=True)


def test_wake_first_cron_adopts_existing_work_without_recomposition(tmp_path):
    wake = {'runner': ['python', 'brief.py'], 'prompt': 'wake input',
            'label': 'brief:sotto-morning-brief'}
    job = work.enqueue(tmp_path, 'run', wake, key='brief:morning:day:revision1',
                       shared_fixed_brief_wake=True)
    assert admit(tmp_path) == job
    assert work.get(tmp_path, job)['payload']['label'] == 'brief:sotto-morning-brief'
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')['job_id'] == job
    assert work.claim(tmp_path, 'worker')['id'] == job
    assert work.finish(tmp_path, job, 'worker')
    assert admit(tmp_path) == job
    assert work.get(tmp_path, job)['status'] == 'done'


def test_wake_first_completed_or_pruned_work_is_adopted_without_remint(tmp_path):
    wake = {'runner': ['python', 'brief.py'], 'prompt': 'wake input',
            'label': 'brief:sotto-morning-brief'}
    job = work.enqueue(tmp_path, 'run', wake, key='brief:morning:day:revision1',
                       shared_fixed_brief_wake=True)
    assert work.claim(tmp_path, 'worker')['id'] == job
    assert work.finish(tmp_path, job, 'worker')
    assert admit(tmp_path) == job  # Payload already scrubbed to {}.
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        db.execute('UPDATE jobs SET finished=0 WHERE id=?', (job,))
    work.claim(tmp_path, 'other', now=work.RETENTION_SECONDS + 1)
    assert work.get(tmp_path, job) is None
    assert admit(tmp_path) == job
    assert work.get(tmp_path, job) is None
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        wake_row = db.execute('SELECT name,last_work_status FROM fixed_brief_wakes').fetchone()
        assert wake_row == ('sotto-morning-brief', 'done')


def test_pruned_wake_before_cron_is_adopted_from_compact_identity(tmp_path):
    wake = {'runner': ['python', 'brief.py'], 'prompt': 'wake input',
            'label': 'brief:sotto-morning-brief'}
    job = work.enqueue(tmp_path, 'run', wake, key='brief:morning:day:revision1',
                       shared_fixed_brief_wake=True)
    assert work.claim(tmp_path, 'worker')['id'] == job
    assert work.finish(tmp_path, job, 'worker')
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        db.execute('UPDATE jobs SET finished=0 WHERE id=?', (job,))
    work.claim(tmp_path, 'other', now=work.RETENTION_SECONDS + 1)
    assert work.get(tmp_path, job) is None
    assert admit(tmp_path) == job
    assert work.get(tmp_path, job) is None
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')['last_work_status'] == 'done'


def test_saved_result_disposition_is_not_delivery_acceptance(tmp_path):
    job = admit(tmp_path)
    assert work.claim(tmp_path, 'worker')['id'] == job
    work.save_result(tmp_path, job, 'worker', {'text': 'saved, not sent'})
    slot = work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')
    assert slot['last_work_status'] == 'result_saved'
    assert work.release(tmp_path, job, 'worker')
    slot = work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')
    assert slot['last_work_status'] == 'ready'


def test_pruned_work_row_keeps_compact_fence_and_no_delivery_claim(tmp_path):
    job = admit(tmp_path)
    assert work.claim(tmp_path, 'worker')['id'] == job
    assert work.finish(tmp_path, job, 'worker')
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        db.execute('UPDATE jobs SET finished=0 WHERE id=?', (job,))
    work.claim(tmp_path, 'other', now=work.RETENTION_SECONDS + 1)
    assert work.get(tmp_path, job) is None
    row = work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                          wall_slot='2026-11-01')
    assert row['job_id'] == job and row['last_work_status'] == 'done'
    assert admit(tmp_path) == job
    assert work.get(tmp_path, job) is None
    assert work.enqueue(tmp_path, 'run', {'label': 'brief:sotto-morning-brief'},
                        key='brief:morning:day:revision1', retry_terminal=True,
                        shared_fixed_brief_wake=True) == job
    assert work.get(tmp_path, job) is None
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')['last_work_status'] == 'done'
    assert admit(tmp_path, 'brief:morning:day:revision2') == job
    # Only an exact, separately verified unaccepted old handoff may replace it.
    replacement = admit(tmp_path, 'brief:morning:day:revision2', recovery_from=job)
    assert replacement != job
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')['revision'] == 1


def test_failed_admission_rolls_back_fence_and_job(tmp_path, monkeypatch):
    original = work._check_fixed_authority
    def fail_after_binding(db, authority, schedule=None):
        original(db, authority, schedule)
        raise OSError('injected')
    monkeypatch.setattr(work, '_check_fixed_authority', fail_after_binding)
    with pytest.raises(OSError):
        admit(tmp_path)
    monkeypatch.setattr(work, '_check_fixed_authority', original)
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        for table in ('fixed_slots', 'jobs'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (table,)).fetchone():
                assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    assert admit(tmp_path)


def test_failure_after_work_insert_rolls_back_both_rows(tmp_path):
    # Create the schema and an unrelated prior slot, then make the fence write fail
    # after enqueue_fixed_slot has inserted its ordinary work row.
    admit(tmp_path)
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        db.execute("""CREATE TRIGGER fail_next_fixed BEFORE INSERT ON fixed_slots
                   WHEN NEW.slot_hash != (SELECT slot_hash FROM fixed_slots LIMIT 1)
                   BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.IntegrityError, match='injected'):
        work.enqueue_fixed_slot(
            tmp_path, {'label': 'cron:sotto-evening-brief', 'prompt': 'fixture'},
            key='evening-new', authority_digest=AUTH, schedule_digest=SCHEDULE,
            name='sotto-evening-brief',
            wall_slot='2026-11-01')
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM fixed_slots').fetchone()[0] == 1


def test_interval_skip_spends_only_same_wall_slot_and_does_not_mint_job(tmp_path):
    assert work.skip_fixed_slot(tmp_path, authority_digest=AUTH, schedule_digest=SCHEDULE,
                                name='sotto-proactive', wall_slot='2026-11-01T01:15')
    assert work.skip_fixed_slot(tmp_path, authority_digest=AUTH, schedule_digest=SCHEDULE,
                                name='sotto-proactive', wall_slot='2026-11-01T01:15')
    row = work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-proactive',
                          wall_slot='2026-11-01T01:15')
    assert row['job_id'] is None and row['last_work_status'] == 'skipped_unlinked'
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-proactive',
                           wall_slot='2026-11-01T01:30') is None


def test_missing_authority_metadata_never_rebinds_existing_history(tmp_path):
    admit(tmp_path)
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        db.execute('DELETE FROM fixed_schedule_authority')
    with pytest.raises(RuntimeError, match='authority evidence is missing'):
        admit(tmp_path, 'brief:morning:day:revision2')
    with pytest.raises(RuntimeError, match='authority evidence is missing'):
        work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                        wall_slot='2026-11-01')


def _authority_row(root):
    with sqlite3.connect(root / 'events' / 'work.sqlite3') as db:
        return db.execute('SELECT authority_digest,schedule_digest '
                          'FROM fixed_schedule_authority').fetchall()


def test_schedule_change_rebinds_in_place_and_keeps_slot_history(tmp_path, capsys):
    # A zone, crons.json or route change used to make every later tick refuse forever.
    first = admit(tmp_path)
    assert work.claim(tmp_path, 'worker')['id'] == first
    assert work.finish(tmp_path, first, 'worker')
    changed = 'd' * 64
    # The already run slot is still fenced: no second composition after the rebind.
    assert admit(tmp_path, 'brief:morning:day:revision2', schedule_digest=changed) == first
    assert work.get(tmp_path, first)['status'] == 'done'
    assert _authority_row(tmp_path) == [(AUTH, changed)]
    assert 'rebound, slot history kept' in capsys.readouterr().out
    # The next wall slot is admitted under the new schedule, so none is lost.
    later = work.enqueue_fixed_slot(tmp_path, PAYLOAD, key='brief:morning:next:revision1',
                                    authority_digest=AUTH, schedule_digest=changed,
                                    name='sotto-morning-brief', wall_slot='2026-11-02')
    assert later not in (None, first) and work.get(tmp_path, later)['status'] == 'pending'
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')['job_id'] == first
    assert capsys.readouterr().out == ''   # an unchanged schedule is silent


def test_foreign_identity_still_refuses_without_touching_history(tmp_path):
    first = admit(tmp_path)
    with pytest.raises(RuntimeError, match='authority changed; reconciliation required'):
        work.enqueue_fixed_slot(tmp_path, PAYLOAD, key='other:tenant', authority_digest='b' * 64,
                                schedule_digest=SCHEDULE, name='sotto-morning-brief',
                                wall_slot='2026-11-02')
    with pytest.raises(RuntimeError, match='authority changed'):
        work.skip_fixed_slot(tmp_path, authority_digest='b' * 64, schedule_digest=SCHEDULE,
                             name='sotto-proactive', wall_slot='2026-11-01T01:15')
    assert _authority_row(tmp_path) == [(AUTH, SCHEDULE)]
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')['job_id'] == first


def test_a_read_never_binds_or_rebinds(tmp_path):
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01') is None
    assert _authority_row(tmp_path) == []
    admit(tmp_path)
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01') is not None
    assert _authority_row(tmp_path) == [(AUTH, SCHEDULE)]


def test_legacy_combined_authority_row_is_adopted_with_its_history(tmp_path, capsys):
    # Pre-release stores kept one combined digest (identity + schedule) and no schedule column.
    legacy = SCHEDULE
    first = admit(tmp_path)
    with sqlite3.connect(tmp_path / 'events' / 'work.sqlite3') as db:
        db.execute('DROP TABLE fixed_schedule_authority')
        db.execute('CREATE TABLE fixed_schedule_authority (singleton INTEGER PRIMARY KEY '
                   'CHECK(singleton=1), authority_digest TEXT NOT NULL)')
        db.execute('INSERT INTO fixed_schedule_authority VALUES (1,?)', (legacy,))
    assert work.fixed_slot(tmp_path, authority_digest=AUTH, name='sotto-morning-brief',
                           wall_slot='2026-11-01')['job_id'] == first
    assert _authority_row(tmp_path) == [(legacy, None)]   # a read does not adopt
    assert admit(tmp_path) == first
    assert _authority_row(tmp_path) == [(AUTH, SCHEDULE)]
    assert 'adopted legacy binding (same schedule)' in capsys.readouterr().out
    with pytest.raises(RuntimeError, match='authority changed'):
        work.fixed_slot(tmp_path, authority_digest='b' * 64, name='sotto-morning-brief',
                        wall_slot='2026-11-01')
