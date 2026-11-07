"""Selected fixed cron admission uses the permanent WorkQueue fence."""
import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import managed
import pytest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('receiver_fixed_slots_test', HERE / 'receiver.py')
rec = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rec)
WORK_SPEC = importlib.util.spec_from_file_location(
    'fixed_slot_queue_test', HERE.parents[1] / 'sotto-chief-of-staff' /
    '_shared' / 'lib' / 'work_queue.py')
queue = importlib.util.module_from_spec(WORK_SPEC)
WORK_SPEC.loader.exec_module(queue)


def test_selected_cron_admits_once_and_manual_is_separate(monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'managed-photon-fixed-five-v1')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant')
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, 'WORK_QUEUE', queue)
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_: None)
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *_: None)
    monkeypatch.setattr(rec.shutil, 'which', lambda *_: '/usr/bin/python3')
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: type('Adapter', (), {
        'run_argv': staticmethod(lambda *_: ['/usr/bin/python3'])})())
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: 'b' * 64)
    monkeypatch.setattr(rec, '_configured_tz_name', lambda: 'America/Los_Angeles')
    now = datetime(2026, 11, 1, 12, 1, tzinfo=ZoneInfo('America/Los_Angeles'))
    monkeypatch.setattr(rec, '_local_now', lambda: now)
    jobs = [('sotto-morning-brief', '0 8 * * *', '', ''),
            ('sotto-evening-brief', '0 18 * * *', '', ''),
            ('sotto-midday-digest', '0 12 * * *', '', ''),
            ('sotto-relationship-pulse', '0 10 * * 1', '', ''),
            ('sotto-proactive', '*/15 * * * *', '', '')]
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *_: jobs)
    payload = '{}'
    first = rec._spawn_and_deliver(['/usr/bin/python3', 'procedure.py'], payload,
                                   'cron:sotto-midday-digest')
    assert rec._spawn_and_deliver(['/usr/bin/python3', 'procedure.py'], payload,
                                  'cron:sotto-midday-digest') == first
    assert queue.fixed_slot(tmp_path, authority_digest=rec._fixed_schedule_authority(),
                            name='sotto-midday-digest', wall_slot='2026-11-01')['job_id'] == first
    manual = rec._spawn_and_deliver(['/usr/bin/python3', 'procedure.py'], payload,
                                    'run-now:sotto-midday-digest')
    assert manual != first


def test_wall_slot_collapses_second_dst_fold():
    first = datetime(2026, 11, 1, 1, 15, tzinfo=ZoneInfo('America/Los_Angeles'), fold=0)
    second = first.replace(fold=1)
    old = rec._local_now
    try:
        rec._local_now = lambda: first
        a = rec._cron_slot('*/15 * * * *')
        rec._local_now = lambda: second
        b = rec._cron_slot('*/15 * * * *')
        assert a == b == '2026-11-01T01:15'
    finally:
        rec._local_now = old


def test_current_route_loss_before_queue_commit_withholds_fixed_work(monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'managed-photon-fixed-five-v1')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant')
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, 'WORK_QUEUE', queue)
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_: None)
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *_: None)
    monkeypatch.setattr(rec.shutil, 'which', lambda *_: '/usr/bin/python3')
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: type('Adapter', (), {
        'run_argv': staticmethod(lambda *_: ['/usr/bin/python3'])})())
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: None)
    monkeypatch.setattr(rec, '_configured_tz_name', lambda: 'America/Los_Angeles')
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(
        2026, 11, 1, 12, 1, tzinfo=ZoneInfo('America/Los_Angeles')))
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *_: [
        ('sotto-midday-digest', '0 12 * * *', '', '')] * 5)
    with pytest.raises(RuntimeError, match='fixed schedule authority is unavailable'):
        rec._spawn_and_deliver(['/usr/bin/python3', 'procedure.py'], '{}',
                               'cron:sotto-midday-digest')
    assert not (tmp_path / 'events' / 'work.sqlite3').exists()


def _brief_receiver(monkeypatch, tmp_path):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'managed-photon-fixed-five-v1')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant')
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, 'WORK_QUEUE', queue)
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_: None)
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *_: None)
    monkeypatch.setattr(rec.shutil, 'which', lambda *_: '/usr/bin/python3')
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: type('Adapter', (), {
        'run_argv': staticmethod(lambda *_: ['/usr/bin/python3'])})())
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: 'b' * 64)
    monkeypatch.setattr(rec, '_configured_tz_name', lambda: 'America/Los_Angeles')
    monkeypatch.setattr(rec, '_local_now', lambda: datetime(
        2026, 11, 1, 8, 1, tzinfo=ZoneInfo('America/Los_Angeles')))
    monkeypatch.setattr(rec, '_brief_revision', lambda *_: 'revision1')
    monkeypatch.setattr(rec, '_find_sotto_script', lambda *_: (
        '/app/sotto-skills/_shared/scripts/compose_brief.py'))
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *_: [
        ('sotto-morning-brief', '0 8 * * *', '', 'sotto-morning-brief'),
        ('sotto-evening-brief', '0 18 * * *', '', 'sotto-evening-brief'),
        ('sotto-midday-digest', '0 12 * * *', '', 'sotto-midday-digest'),
        ('sotto-relationship-pulse', '0 10 * * 1', '', 'sotto-relationship-pulse'),
        ('sotto-proactive', '*/15 * * * *', '', 'sotto-proactive')])


def test_cron_first_wake_reuses_job_and_model_ready_budget(monkeypatch, tmp_path):
    _brief_receiver(monkeypatch, tmp_path)
    # The queue and scheduler must observe the same instant, regardless of test date.
    now = rec._local_now().timestamp()
    monkeypatch.setattr(queue.time, 'time', lambda: now)
    assert rec._job_key('cron:sotto-morning-brief', '') == rec._job_key(
        'brief:sotto-morning-brief', '')
    assert rec._managed_brief('sotto-morning-brief', 'cron:sotto-morning-brief')
    slot = queue.fixed_slot(tmp_path, authority_digest=rec._fixed_schedule_authority(),
                            name='sotto-morning-brief', wall_slot='2026-11-01')
    job = slot['job_id']
    rec.run_skill('sotto-morning-brief', '')
    assert queue.get(tmp_path, job)['payload']['label'] == 'cron:sotto-morning-brief'
    assert queue.claim(tmp_path, 'worker')['id'] == job
    assert queue.fail(tmp_path, job, 'worker', 'model unavailable',
                      model_budget_exhausted=True) == 'failed'
    monkeypatch.setattr(rec, '_brief_model_ready', lambda: False)
    rec.run_skill('sotto-morning-brief', '')
    assert queue.get(tmp_path, job)['status'] == 'failed'
    monkeypatch.setattr(rec, '_brief_model_ready', lambda: True)
    rec.run_skill('sotto-morning-brief', '')
    assert queue.get(tmp_path, job)['status'] == 'pending'
    assert queue.fixed_slot(tmp_path, authority_digest=rec._fixed_schedule_authority(),
                            name='sotto-morning-brief', wall_slot='2026-11-01')['job_id'] == job


def test_wake_first_cron_adopts_exact_job(monkeypatch, tmp_path):
    _brief_receiver(monkeypatch, tmp_path)
    rec.run_skill('sotto-morning-brief', '')
    key = rec._job_key('brief:sotto-morning-brief', '')
    job = queue.job_id_for('run', key)
    assert rec._managed_brief('sotto-morning-brief', 'cron:sotto-morning-brief')
    slot = queue.fixed_slot(tmp_path, authority_digest=rec._fixed_schedule_authority(),
                            name='sotto-morning-brief', wall_slot='2026-11-01')
    assert slot['job_id'] == job and slot['last_work_status'] == 'pending'
    assert queue.get(tmp_path, job)['payload']['label'] == 'brief:sotto-morning-brief'


def test_only_validated_unaccepted_handoff_passes_recovery_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, '_release_invalid_brief_claim', lambda *_: 'open')
    monkeypatch.setattr(rec, '_validated_invalid_brief_recovery', lambda *_: True)
    monkeypatch.setattr(rec.WORK_QUEUE, 'active_job', lambda *_: None)
    monkeypatch.setattr(managed, 'brief_hold', lambda *_: None)
    observed = []
    monkeypatch.setattr(rec, '_fire_cron_job',
                        lambda *args, **kwargs: observed.append((args, kwargs)) or {'ok': True})
    assert rec._invalid_delivery({'run_id': 'a' * 32, 'day': '2026-11-01',
                                  'label': 'cron:sotto-morning-brief'})
    assert observed == [(('sotto-morning-brief', 'cron:sotto-morning-brief'),
                         {'_invalid_recovery': True, '_fixed_recovery_from': 'a' * 32})]
    observed.clear()
    monkeypatch.setattr(rec, '_validated_invalid_brief_recovery', lambda *_: False)
    assert rec._invalid_delivery({'run_id': 'a' * 32, 'day': '2026-11-01',
                                  'label': 'cron:sotto-morning-brief'})
    assert observed == [(('sotto-morning-brief', 'cron:sotto-morning-brief'), {})]


def _fixed_receiver_in_zone(monkeypatch, tmp_path, zone, now):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'managed-photon-fixed-five-v1')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant')
    monkeypatch.setattr(rec, 'DATA', str(tmp_path))
    monkeypatch.setattr(rec, 'WORK_QUEUE', queue)
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_: None)
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *_: None)
    monkeypatch.setattr(rec.shutil, 'which', lambda *_: '/usr/bin/python3')
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: type('Adapter', (), {
        'run_argv': staticmethod(lambda *_: ['/usr/bin/python3'])})())
    monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda: 'b' * 64)
    monkeypatch.setattr(rec, '_configured_tz_name', lambda: zone)
    monkeypatch.setattr(rec, '_local_now', lambda: now.replace(tzinfo=ZoneInfo(zone)))
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *_: [
        ('sotto-morning-brief', '0 8 * * *', '', ''), ('sotto-evening-brief', '0 18 * * *', '', ''),
        ('sotto-midday-digest', '0 12 * * *', '', ''),
        ('sotto-relationship-pulse', '0 10 * * 1', '', ''),
        ('sotto-proactive', '*/15 * * * *', '', '')])


def test_owner_timezone_change_keeps_fixed_slots_admissible_without_rerun(monkeypatch, tmp_path):
    digest = ['/usr/bin/python3', 'procedure.py']
    _fixed_receiver_in_zone(monkeypatch, tmp_path, 'America/Los_Angeles',
                            datetime(2026, 11, 1, 12, 1))
    first = rec._spawn_and_deliver(digest, '{}', 'cron:sotto-midday-digest')
    # The owner corrects the zone via /setup/timezone: same tenant, same route. Every later
    # fixed tick used to raise "authority changed" and all five procedures stopped for good.
    _fixed_receiver_in_zone(monkeypatch, tmp_path, 'America/New_York',
                            datetime(2026, 11, 1, 12, 1))
    assert rec._spawn_and_deliver(digest, '{}', 'cron:sotto-midday-digest') == first
    assert queue.fixed_slot(tmp_path, authority_digest=rec._fixed_schedule_authority(),
                            name='sotto-midday-digest', wall_slot='2026-11-01')['job_id'] == first
    _fixed_receiver_in_zone(monkeypatch, tmp_path, 'America/New_York',
                            datetime(2026, 11, 2, 12, 1))
    second = rec._spawn_and_deliver(digest, '{}', 'cron:sotto-midday-digest')
    assert second != first and queue.get(tmp_path, second)['status'] == 'pending'
    # A crons.json release rebinds the same way; only another tenant's history refuses.
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *_: [
        ('sotto-morning-brief', '30 7 * * *', '', ''), ('sotto-evening-brief', '0 18 * * *', '', ''),
        ('sotto-midday-digest', '0 12 * * *', '', ''),
        ('sotto-relationship-pulse', '0 10 * * 1', '', ''),
        ('sotto-proactive', '*/15 * * * *', '', '')])
    assert rec._spawn_and_deliver(digest, '{}', 'cron:sotto-midday-digest') == second
    monkeypatch.setenv('SOTTO_TENANT_ID', 'other-tenant')
    with pytest.raises(RuntimeError, match='authority changed; reconciliation required'):
        rec._spawn_and_deliver(digest, '{}', 'cron:sotto-midday-digest')


def test_fixed_authority_identity_excludes_zone_route_and_schedule(monkeypatch):
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', 'managed-photon-fixed-five-v1')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant')
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *_: [
        (f'job-{index}', f'{index} * * * *', '', '') for index in range(5)])
    observed = set()
    for zone, route in (('UTC', 'a' * 64), ('Asia/Tokyo', 'a' * 64), ('UTC', 'e' * 64)):
        monkeypatch.setattr(rec, '_configured_tz_name', lambda zone=zone: zone)
        monkeypatch.setattr(rec, '_candidate_retained_route_digest', lambda route=route: route)
        observed.add((rec._fixed_schedule_authority(), rec._fixed_schedule_digest()))
    assert len({authority for authority, _ in observed}) == 1
    assert len({schedule for _, schedule in observed}) == 3
