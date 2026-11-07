"""Real WorkQueue/outbox handoff with a fake channel; no scheduler or model."""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('routine_completion_receiver', HERE / 'receiver.py')
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)
q = rec.WORK_QUEUE
ROUTE = {'platform': 'imessage', 'chat_id': 'iMessage;-;+15555550123', 'thread_id': None}
BASE = {'owner_scope': 'tenant-1:owner-1', 'profile_id': 'owner-profile',
        'legacy_id': 'native-routine-1', 'activation_epoch': 'epoch-1',
        'source_revision': 'a' * 64, 'owner_route': ROUTE}
EXPR = '0 9 * * *'
ZONE = 'UTC'


def install_authority(monkeypatch, tmp_path, proof):
    """The fresh-routine authority seam: current proof for this exact cursor generation, or none."""
    def fresh(job_id, target):
        if proof is None or target != 'photon':
            return None
        return proof if q.routine_job_matches_authority(tmp_path, pending_job_id=job_id, **proof) else None
    monkeypatch.setattr(rec, '_fresh_routine_authority', fresh)


def setup(tmp_path, monkeypatch, *, text='A real routine result', send=None, scheduled_at=None):
    monkeypatch.setenv('SOTTO_CRON_DELIVER', 'photon')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.delenv('SOTTO_TENANT_ID', raising=False)
    monkeypatch.setattr(rec, '_fresh_routines_supported', lambda: True)
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_deliver_target', lambda: 'photon')
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: None)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', send or
                        (lambda *_: (True, '', {'message_id': 'synthetic-accepted'})))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'on_delivered', lambda *_: True)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'valid', lambda *_: True)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'brief_gate', lambda *_: 'send')
    monkeypatch.setattr(rec, '_execute_work', lambda _job: {
        'text': text, 'label': 'routine:fixture', 'effects': [], 'decision_ids': []})
    slot = scheduled_at or (datetime.now(timezone.utc) - timedelta(days=2))
    slot = slot.replace(hour=9, minute=0, second=0, microsecond=0) if scheduled_at is None else slot
    next_slot = slot + timedelta(days=1)
    q.bind_routine_cursor(tmp_path, **BASE, recorded_at_utc=slot,
                          schedule_expr=EXPR, schedule_zone=ZONE, completed_count=0)
    job = q.admit_routine_cursor_slot(tmp_path, **BASE, expected_recorded_at_utc=slot,
                                      next_provisional_at_utc=next_slot,
                                      payload={'runner': [rec.sys.executable, str(HERE / 'brief_runner.py')],
                                               'prompt': 'synthetic private prompt',
                                               'label': 'routine:fixture', 'decision_ids': []})
    authority = {**BASE, 'schedule_expr': EXPR, 'schedule_zone': ZONE, 'expected_generation': 1}
    install_authority(monkeypatch, tmp_path, authority)
    return job['job_id'], authority


def cursor(tmp_path):
    with q._db(tmp_path) as db:
        return dict(db.execute('SELECT * FROM routine_cursors').fetchone())


def outbox_rows(tmp_path):
    path = tmp_path / 'events' / 'outbox.json'
    return json.loads(path.read_text())['rows'] if path.exists() else []


def binding(authority):
    return dict(authority)


def test_accepted_real_outbox_handoff_reanchors_once(tmp_path, monkeypatch):
    job_id, authority = setup(tmp_path, monkeypatch)
    rec._work_one(q.claim(tmp_path, 'fixture-worker'))
    assert q.get(tmp_path, job_id)['status'] == 'done'
    row = cursor(tmp_path)
    assert row['pending_job_id'] is None
    assert row['completed_count'] == 1
    assert row['last_run_utc'] is not None
    assert row['current_utc'] > row['initial_utc']
    slot = q.routine_slot_for_job(tmp_path, job_id)
    assert slot['handoff_kind'] == 'outbox'
    assert slot['handoff_digest'] == outbox_rows(tmp_path)[0]['routine_handoff_digest']
    assert slot['completion_utc'] == row['last_run_utc']
    assert outbox_rows(tmp_path)[0]['status'] == 'delivered'
    receipt = rec.OUTBOX.receipt_for_run(job_id, slot['handoff_digest'])
    assert q.reconcile_routine_cursor(tmp_path, pending_job_id=job_id,
                                      evidence=receipt, **binding(authority))['status'] == 'existing'
    with q._db(tmp_path) as db:
        durable = str(dict(db.execute('SELECT * FROM routine_cursors').fetchone()))
    assert 'synthetic private prompt' not in durable
    assert 'A real routine result' not in durable


def test_unknown_outbox_and_done_job_hold_without_automatic_retry(tmp_path, monkeypatch):
    calls = []
    def send(*_args):
        calls.append(1)
        return False, 'synthetic timeout', {'acceptance': 'unknown'}
    job_id, authority = setup(tmp_path, monkeypatch, send=send)
    rec._work_one(q.claim(tmp_path, 'fixture-worker'))
    assert q.get(tmp_path, job_id)['status'] == 'done'
    assert cursor(tmp_path)['pending_job_id'] == job_id
    assert len(calls) == 1
    slot = q.routine_slot_for_job(tmp_path, job_id)
    receipt = rec.OUTBOX.receipt_for_run(job_id, slot['handoff_digest'])
    assert receipt['status'] == 'pending'
    with pytest.raises(RuntimeError, match='not authoritatively settled'):
        q.reconcile_routine_cursor(tmp_path, pending_job_id=job_id,
                                   evidence=receipt, **binding(authority))
    assert cursor(tmp_path)['pending_job_id'] == job_id
    with pytest.raises(RuntimeError, match='digest is invalid'):
        rec.OUTBOX.deliver({'label': 'routine:fixture', 'body': 'changed',
                            'target': 'photon', 'run_id': job_id,
                            'routine_handoff_digest': slot['handoff_digest']})
    late_calls = []
    def late_send(*_args):
        late_calls.append(1)
        return True, '', {'message_id': 'late-accepted'}
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', late_send)
    with rec.CONNECTORS.json_transaction(rec.OUTBOX.path(), default={'rows': []}) as doc:
        doc['rows'][0]['next_at'] = 0
    rec.OUTBOX.drain()  # an unknown provider effect is polled, never sent again
    late_receipt = rec.OUTBOX.receipt_for_run(job_id, slot['handoff_digest'])
    assert late_receipt['status'] == 'pending'
    assert late_receipt['acceptance'] == 'unknown'
    assert late_receipt['acceptance_uncertain'] is True
    assert len(calls) == 1
    assert late_calls == []
    assert outbox_rows(tmp_path)[0]['status'] == 'pending'
    rec._reconcile_pending_routines()  # existing work heartbeat hook
    assert cursor(tmp_path)['pending_job_id'] == job_id
    assert cursor(tmp_path)['completed_count'] == 0


def test_definite_no_contact_then_accepted_retry_reanchors(tmp_path, monkeypatch):
    calls = []
    def send(*_args):
        calls.append(1)
        return False, 'synthetic precontact refusal', {'acceptance': 'not_attempted'}
    job_id, _ = setup(tmp_path, monkeypatch, send=send)
    rec._work_one(q.claim(tmp_path, 'fixture-worker'))
    assert cursor(tmp_path)['pending_job_id'] == job_id
    assert len(calls) == 1
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send',
                        lambda *_: (True, '', {'message_id': 'late-accepted'}))
    with rec.CONNECTORS.json_transaction(rec.OUTBOX.path(), default={'rows': []}) as doc:
        doc['rows'][0]['next_at'] = 0
    rec.OUTBOX.drain()
    rec._reconcile_pending_routines()
    assert cursor(tmp_path)['pending_job_id'] is None
    assert cursor(tmp_path)['completed_count'] == 1


def test_silent_result_has_explicit_no_outbox_proof(tmp_path, monkeypatch):
    job_id, _ = setup(tmp_path, monkeypatch, text='NO_NUDGES',
                      send=lambda *_: pytest.fail('silent result reached provider'))
    rec._work_one(q.claim(tmp_path, 'fixture-worker'))
    assert q.get(tmp_path, job_id)['status'] == 'done'
    assert outbox_rows(tmp_path) == []
    assert q.routine_slot_for_job(tmp_path, job_id)['handoff_kind'] == 'silent'
    assert cursor(tmp_path)['pending_job_id'] is None
    assert cursor(tmp_path)['completed_count'] == 1


def test_saved_result_restart_does_not_recompose_and_missing_outbox_holds(tmp_path, monkeypatch):
    job_id, authority = setup(tmp_path, monkeypatch)
    q.claim(tmp_path, 'fixture-worker')
    executed = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    q.save_result(tmp_path, job_id, 'fixture-worker', {
        'text': 'Saved routine result', 'label': 'routine:fixture',
        'effects': [], 'decision_ids': [], '_routine_completed_at': executed})
    # A fresh interpreter reopens both stores and completes the saved result.
    # Its execution stub refuses to regenerate the routine after the crash.
    child = r'''
import importlib.util, json, sys
from pathlib import Path
receiver_path, data_path, job_id, authority_json = sys.argv[1:]
spec = importlib.util.spec_from_file_location('recovered_receiver', receiver_path)
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)
rec.DATA = data_path
rec._deliver_target = lambda: 'photon'
rec._record_delivery = lambda *a, **k: None
rec._execute_work = lambda _: (_ for _ in ()).throw(AssertionError('result recomposed'))
authority = json.loads(authority_json)
rec._fresh_routines_supported = lambda: True
rec._fresh_routine_authority = lambda _job, target: authority if target == 'photon' else None
rec.OUTBOX.HOOKS.update({'send': lambda *_: (True, '', {'message_id': 'child-accepted'}),
                         'on_delivered': lambda *_: True, 'valid': lambda *_: True,
                         'brief_gate': lambda *_: 'send'})
rec._work_one(rec.WORK_QUEUE.get(data_path, job_id))
assert rec.WORK_QUEUE.get(data_path, job_id)['status'] == 'done'
'''
    process = subprocess.run([sys.executable, '-c', child, str(HERE / 'receiver.py'),
                              str(tmp_path), job_id, json.dumps(authority)],
                             capture_output=True, text=True, timeout=15)
    assert process.returncode == 0, process.stderr
    assert q.routine_slot_for_job(tmp_path, job_id)['completion_utc'] == q._routine_utc(
        datetime.fromisoformat(executed), 'completion')
    assert cursor(tmp_path)['pending_job_id'] is None
    # Removing the terminal outbox before a separate reconciliation cannot
    # manufacture proof from the WorkQueue done row.
    cursor_hash = cursor(tmp_path)['cursor_hash']
    with q._db(tmp_path) as db:
        db.execute("UPDATE routine_cursors SET pending_job_id=?,pending_slot_utc=initial_utc, "
                   "pending_provisional_utc=current_utc,completed_count=0,last_run_utc=NULL, "
                   "last_evidence_digest=NULL,last_reconciled_job_id=NULL WHERE cursor_hash=?",
                   (job_id, cursor_hash))
    (tmp_path / 'events' / 'outbox.json').unlink()
    assert rec._reconcile_routine_job(job_id) is False
    assert cursor(tmp_path)['pending_job_id'] == job_id
    assert q.get(tmp_path, job_id)['status'] == 'done'


def test_packaged_work_queue_loads_its_native_cursor_sibling(tmp_path):
    source = HERE.parents[1] / 'sotto-chief-of-staff' / '_shared' / 'lib'
    dockerfile = (HERE.parents[1] / 'Dockerfile').read_text()
    assert ('COPY sotto-chief-of-staff/_shared/lib/routine_cursor.py '
            '/app/trigger-receiver/routine_cursor.py') in dockerfile
    assert ('COPY sotto-chief-of-staff/_shared/lib/routine_cursor.NOTICE '
            '/app/trigger-receiver/routine_cursor.NOTICE') in dockerfile
    for name in ('work_queue.py', 'routine_cursor.py'):
        shutil.copyfile(source / name, tmp_path / name)
    packaged_spec = importlib.util.spec_from_file_location('packaged_work_queue', tmp_path / 'work_queue.py')
    packaged = importlib.util.module_from_spec(packaged_spec)
    packaged_spec.loader.exec_module(packaged)
    assert packaged._captured_schedule(EXPR, ZONE, 0, None) == (EXPR, ZONE, 0, None)


def test_captured_last_run_must_precede_recorded_slot(tmp_path):
    slot = datetime(2026, 9, 30, 9, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match='precede recorded cursor'):
        q.bind_routine_cursor(tmp_path, **BASE, recorded_at_utc=slot,
                              schedule_expr=EXPR, schedule_zone=ZONE,
                              completed_count=1, last_run_at=slot.isoformat())
    assert q.bind_routine_cursor(tmp_path, **BASE, recorded_at_utc=slot,
                                 schedule_expr=EXPR, schedule_zone=ZONE,
                                 completed_count=1,
                                 last_run_at=(slot - timedelta(days=1)).isoformat())['completed_count'] == 1


def test_completion_at_exact_scheduled_instant_reanchors(tmp_path, monkeypatch):
    job_id, _ = setup(tmp_path, monkeypatch)
    q.claim(tmp_path, 'fixture-worker')
    scheduled = cursor(tmp_path)['initial_utc']
    q.save_result(tmp_path, job_id, 'fixture-worker', {
        'text': 'On-time result', 'label': 'routine:fixture', 'effects': [],
        'decision_ids': [], '_routine_completed_at': scheduled})
    monkeypatch.setattr(rec, '_execute_work', lambda _: pytest.fail('saved result recomposed'))
    rec._work_one(q.get(tmp_path, job_id))
    assert cursor(tmp_path)['pending_job_id'] is None
    assert cursor(tmp_path)['last_run_utc'] == scheduled


def test_future_occurrence_is_not_claimable_or_executable_early(tmp_path, monkeypatch):
    future = datetime.now(timezone.utc) + timedelta(days=2)
    future = future.replace(hour=9, minute=0, second=0, microsecond=0)
    job_id, _ = setup(tmp_path, monkeypatch, scheduled_at=future,
                      send=lambda *_: pytest.fail('early provider contact'))
    monkeypatch.setattr(rec, '_execute_work', lambda _: pytest.fail('early execution'))
    assert q.get(tmp_path, job_id)['due'] == pytest.approx(future.timestamp())
    assert q.claim(tmp_path, 'fixture-worker') is None
    # Even an older/corrupt queue row made due prematurely cannot bypass the
    # receiver's current scheduled-instant check before model or channel work.
    with q._db(tmp_path) as db:
        db.execute('UPDATE jobs SET due=? WHERE id=?', (0, job_id))
    rec._work_one(q.claim(tmp_path, 'fixture-worker'))
    assert q.get(tmp_path, job_id)['attempts'] == 0
    assert outbox_rows(tmp_path) == []


def test_explicit_early_not_before_is_rejected_and_fold_uses_utc_instant(tmp_path):
    fold = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo('America/Los_Angeles'), fold=1)
    assert fold.astimezone(timezone.utc).hour == 9
    q.bind_routine_cursor(tmp_path, **BASE, recorded_at_utc=fold,
                          schedule_expr='30 1 * * *', schedule_zone='America/Los_Angeles',
                          completed_count=0)
    payload = {'runner': [sys.executable, str(HERE / 'brief_runner.py')],
               'prompt': 'synthetic', 'label': 'routine:fixture', 'decision_ids': []}
    with pytest.raises(ValueError, match='precedes scheduled occurrence'):
        q.admit_routine_cursor_slot(tmp_path, **BASE, expected_recorded_at_utc=fold,
                                    next_provisional_at_utc=fold + timedelta(days=1),
                                    payload=payload, not_before=fold.timestamp() - 1)
    assert q.pending_routine_job_ids(tmp_path) == []
    receipt = q.admit_routine_cursor_slot(tmp_path, **BASE, expected_recorded_at_utc=fold,
                                          next_provisional_at_utc=fold + timedelta(days=1),
                                          payload=payload)
    assert q.get(tmp_path, receipt['job_id'])['due'] == pytest.approx(1_793_524_200)


def test_reconciliation_page_rotates_past_permanently_held_first_32(tmp_path):
    with q._db(tmp_path) as db:
        for index in range(40):
            db.execute('''INSERT INTO routine_cursors
                (cursor_hash,binding_digest,initial_utc,current_utc,pending_job_id)
                VALUES (?,?,?,?,?)''', (f'{index:064x}', 'held', '2026-09-30T09:00:00Z',
                                     '2026-10-01T09:00:00Z', f'held-{index}'))
    first = q.pending_routine_job_ids(tmp_path)
    second = q.pending_routine_job_ids(tmp_path)
    assert len(first) == 32
    assert len(second) == 32
    assert set(first + second) == {f'held-{index}' for index in range(40)}
    assert second[0] == 'held-32'


def test_uninstalled_or_changed_authority_prevents_execution_and_saved_handoff(tmp_path, monkeypatch):
    job_id, authority = setup(tmp_path, monkeypatch,
                              send=lambda *_: pytest.fail('unauthorized provider contact'))
    monkeypatch.setattr(rec, '_execute_work', lambda _: pytest.fail('unauthorized execution'))
    install_authority(monkeypatch, tmp_path, None)
    rec._work_one(q.claim(tmp_path, 'fixture-worker'))
    assert q.get(tmp_path, job_id)['attempts'] == 0
    assert outbox_rows(tmp_path) == []
    q.claim(tmp_path, 'fixture-worker', now=10**12)
    q.save_result(tmp_path, job_id, 'fixture-worker', {
        'text': 'Saved routine result', 'label': 'routine:fixture', 'effects': [],
        'decision_ids': [], '_routine_completed_at': datetime.now(timezone.utc).isoformat()})
    install_authority(monkeypatch, tmp_path, {**authority, 'owner_route': {
        **ROUTE, 'chat_id': 'iMessage;-;+15555550777'}})
    rec._work_one(q.get(tmp_path, job_id))
    assert outbox_rows(tmp_path) == []
    assert q.get(tmp_path, job_id)['result'] is not None


def test_revoked_authority_holds_later_outbox_retry_and_effect(tmp_path, monkeypatch):
    calls = []
    def send(*_args):
        calls.append(1)
        return False, 'precontact refusal', {'acceptance': 'not_attempted'}
    job_id, authority = setup(tmp_path, monkeypatch, send=send)
    rec._work_one(q.claim(tmp_path, 'fixture-worker'))
    assert len(calls) == 1
    install_authority(monkeypatch, tmp_path, {**authority, 'source_revision': 'b' * 64})
    with rec.CONNECTORS.json_transaction(rec.OUTBOX.path(), default={'rows': []}) as doc:
        doc['rows'][0]['next_at'] = 0
    rec.OUTBOX.drain()
    assert len(calls) == 1
    assert outbox_rows(tmp_path)[0]['attempts'] == 1
    assert cursor(tmp_path)['pending_job_id'] == job_id


def test_revoked_authority_holds_post_acceptance_effect_retry(tmp_path, monkeypatch):
    job_id, authority = setup(tmp_path, monkeypatch)
    effects = []
    def first_effect(_payload):
        effects.append('first')
        return False
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'on_delivered', first_effect)
    rec._work_one(q.claim(tmp_path, 'fixture-worker'))
    row = outbox_rows(tmp_path)[0]
    assert row['status'] == 'delivered'
    assert row['effects_pending'] is True
    assert effects == ['first']
    assert cursor(tmp_path)['pending_job_id'] == job_id
    install_authority(monkeypatch, tmp_path, {**authority, 'source_revision': 'b' * 64})
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'on_delivered',
                        lambda _: pytest.fail('revoked effect executed'))
    with rec.CONNECTORS.json_transaction(rec.OUTBOX.path(), default={'rows': []}) as doc:
        doc['rows'][0]['effects_next_at'] = 0
    rec.OUTBOX.drain()
    assert cursor(tmp_path)['pending_job_id'] == job_id
    assert outbox_rows(tmp_path)[0]['effects_attempts'] == 1


def test_missing_outbox_certainty_flags_never_become_positive_proof(tmp_path, monkeypatch):
    job_id, authority = setup(tmp_path, monkeypatch)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'on_delivered', lambda _: False)
    rec._work_one(q.claim(tmp_path, 'fixture-worker'))
    assert cursor(tmp_path)['pending_job_id'] == job_id
    slot = q.routine_slot_for_job(tmp_path, job_id)
    with rec.CONNECTORS.json_transaction(rec.OUTBOX.path(), default={'rows': []}) as doc:
        row = doc['rows'][0]
        del row['acceptance_uncertain']
        row['effects_pending'] = False
        row['effects_status'] = 'applied'
    receipt = rec.OUTBOX.receipt_for_run(job_id, slot['handoff_digest'])
    assert receipt['acceptance_uncertain'] is None
    with pytest.raises(RuntimeError, match='not authoritatively settled'):
        q.reconcile_routine_cursor(tmp_path, pending_job_id=job_id,
                                   evidence=receipt, **binding(authority))
    with rec.CONNECTORS.json_transaction(rec.OUTBOX.path(), default={'rows': []}) as doc:
        row = doc['rows'][0]
        row['acceptance_uncertain'] = False
        del row['effects_pending']
    receipt = rec.OUTBOX.receipt_for_run(job_id, slot['handoff_digest'])
    assert receipt['effects_pending'] is None
    with pytest.raises(RuntimeError, match='not authoritatively settled'):
        q.reconcile_routine_cursor(tmp_path, pending_job_id=job_id,
                                   evidence=receipt, **binding(authority))


def test_fresh_routines_need_a_fixed_profile_and_routine_epoch(monkeypatch):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'managed-photon-fixed-five-v1')
    monkeypatch.setenv('SOTTO_ROUTINE_EPOCH', 'a' * 32)
    assert rec._fresh_routines_supported() is True
    monkeypatch.setenv('SOTTO_ROUTINE_EPOCH', 'not-an-epoch')
    assert rec._fresh_routines_supported() is False
    monkeypatch.setenv('SOTTO_ROUTINE_EPOCH', 'a' * 32)
    monkeypatch.delenv('SOTTO_RUNTIME_PROFILE')
    assert rec._fresh_routines_supported() is False
