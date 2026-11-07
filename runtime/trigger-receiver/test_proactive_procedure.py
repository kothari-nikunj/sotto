"""No-provider contracts for scheduled/wake scan-before-compose and terminal memory slots."""
import importlib.util
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import time
from types import SimpleNamespace

import pytest
import procedure_runner

HERE = Path(__file__).parent
PACK = HERE.parents[1] / 'sotto-chief-of-staff'


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    spec = importlib.util.spec_from_file_location('proactive_receiver', HERE / 'receiver.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'DATA', str(tmp_path))
    monkeypatch.setattr(module, '_find_sotto_script', lambda *rel: str(PACK.joinpath(*rel)))
    monkeypatch.setattr(module, '_record_delivery', lambda *a, **k: None)
    monkeypatch.setattr(module.shutil, 'which', lambda command: command)
    return module


@pytest.mark.parametrize('mode', ['managed', 'self-host'])
def test_real_empty_scanner_stages_its_result_without_an_agent(mode, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', mode)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a' * 32)
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    import source_context
    monkeypatch.setattr(source_context, 'read_local', lambda: {})
    actual_run = procedure_runner.subprocess.run
    calls = []

    def invoke(argv, **kwargs):
        calls.append(Path(argv[1]).name)
        if calls[-1] == 'gather_google.py':
            Path(argv[argv.index('--cal-out') + 1]).write_text('[]')
            return SimpleNamespace(returncode=0, stdout='Saved 0 events\n')
        assert calls[-1] == 'proactive_scan.py', 'empty scan started a composer'
        return actual_run(argv, **kwargs)

    monkeypatch.setattr(procedure_runner.subprocess, 'run', invoke)
    request = {'pack': str(PACK), 'kind': 'proactive', 'compose_argv': ['never-start', 'prompt']}
    assert procedure_runner.run(request) == 'NO_NUDGES'
    receipt = json.loads((tmp_path / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    assert receipt['proactive_result']['nudges'] == []
    assert calls == ['gather_google.py', 'proactive_scan.py']


@pytest.mark.parametrize('quiet,eligible,expected', [
    (False, True, 'Meeting soon'), (True, True, 'NO_NUDGES'), (False, False, 'NO_NUDGES'),
])
def test_only_valid_accepted_candidates_reach_composition(quiet, eligible, expected, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    import source_context
    import delivery_effects
    monkeypatch.setattr(source_context, 'read_local', lambda: {})
    monkeypatch.setattr(delivery_effects, 'valid', lambda *args: eligible)
    result = {'nudges': [{'kind': 'meeting_prep', 'key': 'meeting:1', 'decision_id': 'd1',
                          'detail': 'private candidate body', 'person_id': 'private-contact-id'}],
              'quiet': quiet, '_eligibility': delivery_effects.for_bundle(
                  {'events': [{'event': {}}]})['effects']}
    calls = []

    def invoke(argv, **kwargs):
        calls.append(list(argv))
        if Path(argv[1]).name == 'gather_google.py':
            return SimpleNamespace(returncode=0, stdout='Saved events\n')
        assert Path(argv[1]).name == 'proactive_scan.py'
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))

    sys.path.insert(0, str(PACK / '_shared/scripts'))   # the runner adds this at run time
    import compose_notification
    composed = []
    monkeypatch.setattr(compose_notification, 'compose', lambda kind, rows: composed.append((kind, rows)) or 'Meeting soon')
    monkeypatch.setattr(procedure_runner.subprocess, 'run', invoke)
    request = {'pack': str(PACK), 'kind': 'proactive'}
    assert procedure_runner.run(request) == expected
    assert bool(composed) == (not quiet and eligible)
    if composed:
        assert composed == [('proactive', result['nudges'])]



def test_scan_failure_does_not_start_composer(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    import source_context
    monkeypatch.setattr(source_context, 'read_local', lambda: {})
    calls = []

    def invoke(argv, **kwargs):
        calls.append(Path(argv[1]).name)
        return SimpleNamespace(returncode=1 if calls[-1] == 'proactive_scan.py' else 0, stdout='')

    monkeypatch.setattr(procedure_runner.subprocess, 'run', invoke)
    with pytest.raises(RuntimeError, match='proactive_scan.py exit 1'):
        procedure_runner.run({'pack': str(PACK), 'kind': 'proactive'})
    assert calls == ['gather_google.py', 'proactive_scan.py']


@pytest.mark.parametrize('proof', [None, [], [{'kind': 'unrecognized'}]])
def test_nonempty_scanner_without_eligibility_fails_closed(proof, tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    import source_context
    monkeypatch.setattr(source_context, 'read_local', lambda: {})
    def invoke(argv, **kwargs):
        assert Path(argv[1]).name in ('gather_google.py', 'proactive_scan.py')
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            'nudges': [{'key': 'nudge'}], '_eligibility': proof}))
    monkeypatch.setattr(procedure_runner.subprocess, 'run', invoke)
    with pytest.raises(RuntimeError, match='omitted eligibility'):
        procedure_runner.run({'pack': str(PACK), 'kind': 'proactive'})


def test_wake_and_schedule_admit_the_same_procedure(rec, monkeypatch):
    monkeypatch.setattr(rec, '_delivery_channel_ready', lambda label: True)
    monkeypatch.setattr(rec, '_managed_brief', lambda *args: False)
    monkeypatch.setattr(rec, '_sotto_cron_jobs', lambda *a: [
        ('sotto-proactive', '*/15 * * * *', 'Run my proactive check', 'sotto-proactive')])
    calls = []
    monkeypatch.setattr(rec, '_spawn_and_deliver', lambda *args: calls.append(args))
    assert rec.run_proactive_skill()
    assert rec._fire_cron_job('sotto-proactive', 'run-now:sotto-proactive')['ok']
    assert calls[0][:2] == calls[1][:2]
    assert calls[0][0] == [sys.executable, str(HERE / 'procedure_runner.py')]
    assert json.loads(calls[0][1])['kind'] == 'proactive'


@pytest.mark.parametrize('composer_exit', [0, 1])
def test_proactive_worker_uses_direct_procedure_and_run_identity(rec, tmp_path, monkeypatch, composer_exit):
    request = {'pack': str(PACK), 'kind': 'proactive'}
    job = {'id': 'b' * 32, 'kind': 'run', 'payload': {'label': 'proactive',
           'runner': [sys.executable, str(HERE / 'procedure_runner.py')], 'prompt': json.dumps(request)}}
    class Process:
        pid = 123
        returncode = composer_exit
        def __init__(self, argv, **kwargs):
            assert argv[:2] == job['payload']['runner']
            assert json.loads(argv[-1]) == request
            assert kwargs['env']['SOTTO_DELIVERY_RUN_ID'] == job['id']
            assert kwargs['env']['SOTTO_UNATTENDED'] == '1'
        def communicate(self, **kwargs):
            return 'Meeting soon', ''
        def poll(self):
            return 0
    monkeypatch.setattr(rec.subprocess, 'Popen', Process)
    if composer_exit:
        with pytest.raises(rec._WorkerError):
            rec._execute_work(job)
    else:
        assert rec._execute_work(job)['text'] == 'Meeting soon'
    assert not list(tmp_path.glob('events/bundle-*'))
    assert not rec._WORK_PROCESSES


def test_proactive_queues_its_declared_procedure_without_another_runner(rec, monkeypatch):
    monkeypatch.setattr(rec.shutil, 'which', lambda command: None)
    monkeypatch.setattr(rec, '_delivery_channel_ready', lambda label: True)
    calls = []
    monkeypatch.setattr(rec, '_spawn_and_deliver', lambda *args: calls.append(args))
    assert rec.run_proactive_skill()
    assert json.loads(calls[0][1]) == {'pack': str(PACK), 'kind': 'proactive'}


@pytest.mark.parametrize('failure_at', ['adapter', 'receipt'])
def test_pre_spawn_failure_cleans_private_event_staging(rec, tmp_path, monkeypatch, failure_at):
    job = {'id': 'b' * 32, 'kind': 'event', 'payload': {'bundle': {'events': []}}}
    real_adapter = rec._runtime_adapter()
    staged_paths = []
    def adapter(runner, prompt, *args):
        path = Path(json.loads(prompt)['bundle_path'])
        assert path.exists() and path.stat().st_mode & 0o777 == 0o600
        staged_paths.append(path)
        if failure_at == 'adapter':
            raise RuntimeError('adapter failed')
        return real_adapter.run_argv(runner, prompt)
    def receipt(*args, **kwargs):
        raise RuntimeError('receipt failed')
    monkeypatch.setattr(rec, '_runtime_adapter', lambda: SimpleNamespace(
        run_argv=adapter, validate_fixed_work=lambda *_args: None))
    monkeypatch.setattr(rec, '_record_delivery', receipt)
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *a, **k: pytest.fail('child started'))
    with pytest.raises(RuntimeError, match=f'{failure_at} failed'):
        rec._execute_work(job)
    assert staged_paths and all(not path.exists() for path in staged_paths)
    assert not rec._WORK_PROCESSES


def test_worker_timeout_stops_nested_composer(rec, tmp_path, monkeypatch):
    """Exercise real process-group cancellation, including a live grandchild."""
    lock_path, ready = tmp_path / 'composer.lock', tmp_path / 'composer.ready'
    child = (
        'import fcntl, pathlib, sys, time; '
        'lock = open(sys.argv[1], "w"); fcntl.flock(lock, fcntl.LOCK_EX); '
        'pathlib.Path(sys.argv[2]).touch(); time.sleep(60)'
    )
    parent = 'import subprocess, sys; subprocess.run([sys.executable, "-c", *sys.argv[1:4]])'
    job = {'id': 'c' * 32, 'kind': 'run', 'payload': {'label': 'proactive',
           'runner': [sys.executable, '-c', parent, child, str(lock_path), str(ready)], 'prompt': ''}}
    actual_popen, processes = rec.subprocess.Popen, []

    def start(argv, **kwargs):
        process = actual_popen(argv, **kwargs)
        processes.append(process)
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), 'nested composer did not start'
        return process

    monkeypatch.setattr(rec.subprocess, 'Popen', start)
    monkeypatch.setattr(rec, 'ONESHOT_TIMEOUT_SECS', 0)
    # An inline parent/grandchild pair stands in for the composer; the fence is tested elsewhere.
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_args: None)
    try:
        with pytest.raises(TimeoutError, match='work timed out'):
            rec._execute_work(job)
        # A live child would still own this lock even after its parent exits.
        deadline = time.monotonic() + 5
        with lock_path.open('a') as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        pytest.fail('nested composer survived worker timeout')
                    time.sleep(.01)
        assert not rec._WORK_PROCESSES
    finally:
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def test_terminal_memory_bucket_is_consumed_but_next_bucket_runs(rec, monkeypatch):
    import managed
    import onboarding
    monkeypatch.setattr(managed, 'enabled', lambda: True)
    monkeypatch.setattr(managed, 'has_sources', lambda _: True)
    monkeypatch.setattr(onboarding, 'tick', lambda *args, **kwargs: None)
    monkeypatch.setattr(rec, '_CONTEXT_LAST_STARTED', 0)
    monkeypatch.setattr(rec, '_RUNS_INFLIGHT', {})
    clock, attempts = [1500], []
    monkeypatch.setattr(rec.time, 'time', lambda: clock[0])

    def enqueue(*args):
        attempts.append(clock[0])
        if len(attempts) == 1:
            raise RuntimeError('work request is terminal (failed)')

    monkeypatch.setattr(rec, '_spawn_and_deliver', enqueue)
    rec._background_context_tick()
    clock[0] = 1560
    rec._background_context_tick()
    clock[0] = 1800
    rec._background_context_tick()
    assert attempts == [1500, 1800]


def test_transient_memory_admission_failure_still_retries(rec, monkeypatch):
    import managed
    import onboarding
    monkeypatch.setattr(managed, 'enabled', lambda: True)
    monkeypatch.setattr(managed, 'has_sources', lambda _: True)
    monkeypatch.setattr(onboarding, 'tick', lambda *args, **kwargs: None)
    monkeypatch.setattr(rec, '_CONTEXT_LAST_STARTED', 0)
    calls = []
    def enqueue(*args):
        calls.append(True)
        raise RuntimeError('temporary admission failure')
    monkeypatch.setattr(rec, '_spawn_and_deliver', enqueue)
    rec._background_context_tick()
    rec._background_context_tick()
    assert len(calls) == 2


@pytest.mark.parametrize('quiet',[False,True])
def test_attended_actual_intention_scanner_and_composer_overlap(quiet,tmp_path,monkeypatch):
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE','UTC')
    monkeypatch.setenv('SOTTO_QUIET_START','0');monkeypatch.setenv('SOTTO_QUIET_END','0')
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID','a'*32)
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES','[]')
    monkeypatch.setenv('SOTTO_SKILL_COMPOSITION_PROOF',json.dumps({'source_revision':'a'*64,'route_digest':'b'*64}))
    monkeypatch.syspath_prepend(str(PACK/'_shared/lib'));monkeypatch.syspath_prepend(str(PACK/'_shared/scripts'))
    import ask_research_pipeline
    import schedule_wakeup
    from datetime import datetime,timedelta,timezone
    monkeypatch.setattr(ask_research_pipeline,'current_authority',lambda:{'complete':True,'revision':'a'*64})
    row=schedule_wakeup.create((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),'Review the Cedar deck')
    if quiet:
        import preferences
        preferences.set_scalar('nudge_snooze_until',preferences.resolve_snooze_spec('tomorrow'))
    request={'pack':str(PACK),'kind':'proactive','selection':{'mode':'current'},'work_key':'skill:'+'d'*64}
    output=procedure_runner.run(request)
    assert (output=='NO_NUDGES')==quiet
    if not quiet:
        assert 'Cedar deck' in output
        # A different scheduled run shares the original pending candidate reservation.
        monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID','b'*32)
        assert procedure_runner.run(request)=='NO_NUDGES'
    assert schedule_wakeup.current()[0]['id']==row['id']


def test_attended_revoked_authority_stops_before_scan(tmp_path,monkeypatch):
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path))
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES','[]')
    monkeypatch.setenv('SOTTO_SKILL_COMPOSITION_PROOF',json.dumps({'source_revision':'a'*64,'route_digest':'b'*64}))
    monkeypatch.syspath_prepend(str(PACK/'_shared/lib'));monkeypatch.syspath_prepend(str(PACK/'_shared/scripts'))
    import ask_research_pipeline
    monkeypatch.setattr(ask_research_pipeline,'current_authority',lambda:{'complete':False})
    monkeypatch.setattr(procedure_runner.subprocess,'run',lambda *a,**k:pytest.fail('revoked scanner'))
    with pytest.raises(RuntimeError,match='source authority changed'):
        procedure_runner.run({'pack':str(PACK),'kind':'proactive','work_key':'skill:'+'d'*64})


@pytest.mark.parametrize('mode',['eligible','quiet','off','empty'])
def test_attended_calendar_actual_pipeline_and_shared_outbox(mode,tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    monkeypatch.setenv('SOTTO_DATA',str(tmp_path));monkeypatch.setenv('SOTTO_TIMEZONE','UTC')
    monkeypatch.setenv('SOTTO_QUIET_START','0');monkeypatch.setenv('SOTTO_QUIET_END','0')
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID','a'*32)
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES','["calendar"]')
    monkeypatch.setenv('SOTTO_SKILL_COMPOSITION_PROOF',json.dumps({'source_revision':'a'*64,'route_digest':'b'*64}))
    monkeypatch.syspath_prepend(str(PACK/'_shared/lib'));monkeypatch.syspath_prepend(str(PACK/'_shared/scripts'))
    import ask_research_pipeline, source_context, preferences, delivery_effects
    monkeypatch.setattr(ask_research_pipeline,'current_authority',lambda:{'complete':True,'revision':'a'*64})
    monkeypatch.setattr(source_context,'read_local',lambda:pytest.fail('ungranted local read'))
    if mode=='quiet':
        preferences.set_scalar('nudge_snooze_until',preferences.resolve_snooze_spec('tomorrow'))
    if mode=='off':
        preferences.set_scalar('nudge_budget','0')
    now=datetime.now(timezone.utc)
    events=[] if mode=='empty' else [{'id':'cedar-occurrence','summary':'Cedar review',
        'start':(now+timedelta(minutes=10)).isoformat(),'end':(now+timedelta(minutes=40)).isoformat(),
        'attendees':[{'email':'dana@example.com','displayName':'Dana','responseStatus':'accepted'}]}]
    actual=procedure_runner.subprocess.run
    def transport(argv,**kwargs):
        if Path(argv[1]).name=='gather_google.py':
            Path(argv[argv.index('--cal-out')+1]).write_text(json.dumps(events))
            return SimpleNamespace(returncode=0,stdout='Saved events')
        return actual(argv,**kwargs)
    monkeypatch.setattr(procedure_runner.subprocess,'run',transport)
    request={'pack':str(PACK),'kind':'proactive','work_key':'skill:'+'d'*64,'selection':{'mode':'current'}}
    output=procedure_runner.run(request)
    if mode!='eligible':
        assert output=='NO_NUDGES'
        return
    assert 'Dana' in output
    doc=json.loads((tmp_path/('events/delivery-effects-'+'a'*32+'.json')).read_text())
    assert any(e['kind']=='unsolicited_nudge' for e in doc['effects'])
    assert any(e.get('calendar_event_id')=='cedar-occurrence' for e in doc['effects'])
    # Concurrent cron uses the same scanner state and cannot reserve the same occurrence.
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID','b'*32)
    monkeypatch.delenv('SOTTO_PROACTIVE_CAPTURED_SOURCES')
    monkeypatch.setattr(source_context,'read_local',lambda:{})
    assert procedure_runner.run({'pack':str(PACK),'kind':'proactive'})=='NO_NUDGES'
    from test_outbox import _load_outbox,_transaction
    box=_load_outbox('attended_calendar_box');sends=[]
    box.HOOKS.update(data_root=lambda:str(tmp_path),json_transaction=_transaction,
        local_today=lambda:now.date().isoformat(),valid=lambda payload:delivery_effects.valid(payload['effects'],time.time()),
        send=lambda *a:(sends.append(a) or (True,'',{'acceptance':'accepted','message_id':'synthetic-accepted'})),
        on_delivered=lambda payload:delivery_effects.finalize(payload['effects'],payload.get('receipt') or {}),
        record=lambda *a,**k:None)
    box._enqueue('a'*32,box.KIND_NUDGE,{'label':'skill:'+'d'*64+':proactive','body':output,
        'target':'owner','effects':doc['effects']},time.time(),now.date().isoformat())
    assert box._attempt('a'*32)
    assert len(sends)==1
    box._attempt('a'*32)
    assert len(sends)==1


def test_real_scheduled_location_update_uses_prior_cache_observation(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    clock = datetime.now(timezone.utc)
    zone = ZoneInfo("Pacific/Honolulu" if clock.hour in (0, 23) else "UTC")
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a' * 32)
    monkeypatch.setenv('SOTTO_TIMEZONE', zone.key)
    monkeypatch.setenv('SOTTO_QUIET_START', '0')
    monkeypatch.setenv('SOTTO_QUIET_END', '0')
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    import delivery_effects
    import source_context
    monkeypatch.setattr(source_context, 'read_local', lambda: {})
    now = clock.astimezone(zone)
    start = (now + timedelta(minutes=20)).isoformat()
    assert all((now + delta).date() == now.date() for delta in
               (timedelta(minutes=20), timedelta(minutes=-5), timedelta(seconds=-60)))
    observed = (now - timedelta(seconds=60)).isoformat()
    event = {'id': 'cal:location', 'start': start, 'summary': 'Design review', 'location': 'Room B',
             'attendees': [{'email': 'guest@outside.test', 'displayName': 'Guest'}]}
    delivery_effects.finalize([{'kind': 'meeting_prep_delivered', 'mode': 'full',
        'calendar_event_id': event['id'], 'calendar_start': start,
        'prep_location': 'Room A', 'prep_route_digest': 'b' * 64}],
        {'accepted_at': (now - timedelta(minutes=5)).timestamp(), 'route_proof_version': 1, 'route_digest': 'b' * 64})
    cache = tmp_path / 'cache/calendar_today.json'; cache.parent.mkdir()
    cache.write_text(json.dumps({'status': 'ok', 'complete': True, 'projection': 'day',
        'date': now.date().isoformat(), 'generated_at': observed, 'refresh_secs': 120,
        'events': [{'id': event['id'], 'start': start, 'prep_location': 'Room B'}]}))
    actual = procedure_runner.subprocess.run
    calls = []
    def invoke(argv, **kwargs):
        calls.append(Path(argv[1]).name)
        if calls[-1] == 'gather_google.py':
            Path(argv[argv.index('--cal-out') + 1]).write_text(json.dumps([event]))
            return SimpleNamespace(returncode=0, stdout='Synthetic calendar\n')
        return actual(argv, **kwargs)
    monkeypatch.setattr(procedure_runner.subprocess, 'run', invoke)
    assert procedure_runner.run({'pack': str(PACK), 'kind': 'proactive'}) == 'Design review location changed to Room B.'
    staged = json.loads((tmp_path / 'events' / ('delivery-effects-' + 'a' * 32 + '.json')).read_text())
    update = next(row for row in staged['effects'] if row['kind'] == 'prep_location_update')
    assert update['calendar_observed_at'] == observed
    assert delivery_effects.valid(staged['effects'], now.timestamp())
    assert calls == ['gather_google.py', 'proactive_scan.py']
