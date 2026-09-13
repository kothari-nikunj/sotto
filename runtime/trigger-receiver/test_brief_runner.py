import json
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import brief_runner

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'


@pytest.mark.parametrize('kind', ['evening', 'welcome'])
@pytest.mark.parametrize('wrapped', [False, True])
@pytest.mark.parametrize('fail', [None, 'compose_brief.py', 'learn_step.py'])
def test_procedure_order_learning_and_failure_gate(tmp_path, monkeypatch, fail, wrapped, kind):
    payload = tmp_path / 'payload.json'
    local = {'imessage': [{'text': 'fixture'}]}
    payload.write_text(json.dumps({'local_data': local} if wrapped else local))
    seen = []
    temp_paths = []
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))

    def invoke(argv, **kwargs):
        name = Path(argv[1]).name
        seen.append(name)
        assert kwargs['capture_output'] and 0 < kwargs['timeout'] <= 600
        def path(flag):
            p = Path(argv[argv.index(flag) + 1]); temp_paths.append(p); return p
        if name == fail:
            return SimpleNamespace(returncode=1, stdout='', stderr='private input must stay private')
        out = '{}'
        if name == 'gather_google.py':
            path('--gmail-out').write_text('[{"id":"email"}]')
            path('--cal-out').write_text('[]')
            path('--source-results-out').write_text(json.dumps({'gmail': {'status': 'ok'}, 'calendar': {'status': 'ok', 'complete': True}}))
        if '--out' in argv:
            path('--out').write_text('{}')
        if name == 'select_attendees.py':
            out = '[]'
        if name == 'continuity_resolve.py':
            assert '--resolve-only' in argv
            assert 'new_actions' not in json.loads(Path(argv[-1]).read_text())
        if name == 'compose_brief.py' and '--seed-snapshot' in argv:
            return SimpleNamespace(returncode=0, stdout='{}', stderr='')
        if name == 'compose_brief.py':
            assert json.loads(path('--local').read_text())['imessage'][0]['text'] == 'fixture'
            out = json.dumps({'brief_text': 'Exact composed brief', 'actions': [{'summary': 'fixture debt'}],
                              'extracted_knowledge': {'person_updates': []}})
        if name == 'learn_step.py':
            assert argv[argv.index('--phase')+1] == 'essential'
            assert json.loads(path('--continuity').read_text())['new_actions'] == [{'summary': 'fixture debt'}]
            assert json.loads(path('--knowledge-out').read_text()) == {'person_updates': []}
        return SimpleNamespace(returncode=0, stdout=out, stderr='')

    monkeypatch.setattr(brief_runner.subprocess, 'run', invoke)
    request = {'pack': str(PACK), 'payload_path': str(payload), 'kind': kind}
    if fail == 'compose_brief.py':
        with pytest.raises(RuntimeError, match=fail):
            brief_runner.run(request)
    else:
        assert brief_runner.run(request) == 'Exact composed brief'
        assert seen.index('continuity_resolve.py') < len(seen)-1-seen[::-1].index('compose_brief.py') < seen.index('learn_step.py')
        if kind == 'welcome':
            assert seen.index('style_extract.py') < seen.index('compose_brief.py')
            assert 'select_attendees.py' not in seen
        assert seen.count('persist_prep.py') == 0  # optional research runs ahead of the deadline
        if fail is None:
            before = list(seen)
            assert brief_runner.run(request) == 'Exact composed brief'
            assert seen == before  # retry reuses the saved artifact and required-write receipt
    assert all(p.exists() for p in temp_paths)  # durable follow-up owns cleanup


@pytest.fixture
def procedure(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a' * 32)
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    payload = tmp_path / 'payload.json'
    payload.write_text(json.dumps({'imessage': [{'text': 'Synthetic local context'}]}))
    state = {'fail_phase': '', 'calls': [], 'seed_timeout': False, 'clock': 0}
    monkeypatch.setattr(brief_runner.time, 'monotonic', lambda: state['clock'])
    def invoke(argv, **kwargs):
        name = Path(argv[1]).name
        phase = argv[argv.index('--phase') + 1] if '--phase' in argv else ''
        state['calls'].append((name, phase, kwargs['timeout']))
        def output(flag, data):
            Path(argv[argv.index(flag) + 1]).write_text(json.dumps(data))
        if phase and phase == state['fail_phase']:
            return SimpleNamespace(returncode=1, stdout='', stderr='synthetic failure')
        if name == 'style_extract.py' and state['seed_timeout']:
            state['clock'] += kwargs['timeout']
            raise brief_runner.subprocess.TimeoutExpired(argv, kwargs['timeout'])
        result = {}
        if name == 'gather_google.py':
            output('--gmail-out', [])
            output('--cal-out', [{'id': 'meeting-1', 'start': '2026-09-08T10:00:00-07:00'}])
            output('--source-results-out', {'gmail': {'status': 'ok'},
                                            'calendar': {'status': 'ok', 'complete': True}})
        elif name == 'gather_granola.py':
            output('--out', {'meetings': [{'id': 'note-1'}]})
        elif '--out' in argv:
            output('--out', {'attendees': []})
        if name == 'compose_brief.py':
            result = {'brief_text': 'A useful brief', 'actions': [], 'extracted_knowledge': {}}
        if name == 'select_attendees.py':
            result = []
        return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr='')
    monkeypatch.setattr(brief_runner.subprocess, 'run', invoke)
    request = {'pack': str(PACK), 'payload_path': str(payload), 'kind': 'morning',
               'day': '2026-09-08', 'work_key': 'synthetic-brief-generation'}
    return request, state


def test_optional_learning_failure_keeps_deliverable_and_retries_from_durable_inputs(procedure, tmp_path):
    request, state = procedure
    assert brief_runner.run(request) == 'A useful brief'
    assert not any(name == 'gather_granola.py' for name, _, _ in state['calls'])
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    import work_queue
    job = work_queue.claim(str(tmp_path), 'synthetic-worker')
    assert job['payload']['label'].startswith('background:sotto-learn:')
    followup = json.loads(job['payload']['prompt'])
    state['fail_phase'] = 'ancillary'
    with pytest.raises(RuntimeError, match='learn_step.py'):
        brief_runner.run(followup)
    assert (scratch / 'current/local.json').exists()
    assert not (scratch / 'ancillary-done.json').exists()
    assert brief_runner.run(request) == 'A useful brief'  # ancillary failure cannot gate delivery
    state['fail_phase'] = ''
    assert brief_runner.run(followup) == 'NO_NUDGES'
    assert not (scratch / 'current').exists()
    assert (scratch / 'artifact.json').exists()
    assert brief_runner.run(request) == 'A useful brief'  # primary crash after cleanup still resumes
    assert sum(name == 'compose_brief.py' for name, _, _ in state['calls']) == 1
    assert sum(phase == 'essential' for _, phase, _ in state['calls']) == 1


def test_essential_failure_retries_writes_without_paying_for_composition_again(procedure):
    request, state = procedure
    state['fail_phase'] = 'essential'
    assert brief_runner.run(request) == 'A useful brief'
    state['fail_phase'] = ''
    import work_queue
    followup_job = work_queue.claim(os.environ['SOTTO_DATA'], 'worker')
    assert brief_runner.run(json.loads(followup_job['payload']['prompt'])) == 'NO_NUDGES'
    assert sum(name == 'compose_brief.py' for name, _, _ in state['calls']) == 1
    assert sum(phase == 'essential' for _, phase, _ in state['calls']) == 2


@pytest.mark.parametrize('failure', [OSError('volume unavailable'), json.JSONDecodeError('bad', 'x', 0)])
def test_non_runtime_essential_failure_also_defers_after_delivery_security_gate(
        procedure, monkeypatch, failure):
    request, _state = procedure
    original = brief_runner.subprocess.run
    def fail_learning(argv, **kwargs):
        if Path(argv[1]).name == 'learn_step.py':
            raise failure
        return original(argv, **kwargs)
    monkeypatch.setattr(brief_runner.subprocess, 'run', fail_learning)
    assert brief_runner.run({**request, 'work_key': 'broad-essential-failure'}) == 'A useful brief'


def test_subprocess_diagnostic_is_bounded_and_does_not_echo_stderr(procedure, monkeypatch):
    request, state = procedure
    state['fail_phase'] = 'essential'
    # Essential learning is deferred, so exercise a composition-path command failure instead.
    original = brief_runner.subprocess.run
    def fail(argv, **kwargs):
        if Path(argv[1]).name == 'gather_google.py':
            return SimpleNamespace(returncode=1, stdout='', stderr='secret@example.com bearer abc123')
        return original(argv, **kwargs)
    monkeypatch.setattr(brief_runner.subprocess, 'run', fail)
    with pytest.raises(RuntimeError) as raised:
        brief_runner.run({**request, 'work_key': 'diagnostic-test'})
    assert 'secret@example.com' not in str(raised.value)
    assert 'stderr_present' in str(raised.value)


def test_preparation_does_optional_work_once_then_current_sources_are_gathered_at_composition(procedure):
    request, state = procedure
    assert brief_runner.run({**request, 'prepare': True}) == 'NO_NUDGES'
    first = list(state['calls'])
    assert brief_runner.run({**request, 'prepare': True}) == 'NO_NUDGES'
    assert state['calls'] == first
    assert brief_runner.run(request) == 'A useful brief'
    names = [name for name, _, _ in state['calls']]
    assert names.count('gather_google.py') == 2  # current communications are never a prep snapshot
    assert names.count('research_attendees.py') == 1
    assert names.count('gather_granola.py') == 1
    assert names.index('research_attendees.py') < names.index('compose_brief.py')


def test_preparation_finishing_after_due_composition_does_not_publish_stale_receipt(
        procedure, tmp_path, monkeypatch):
    request, _state = procedure
    original = brief_runner.subprocess.run
    run_key = hashlib.sha256(request['work_key'].encode()).hexdigest()[:24]
    scratch = tmp_path / 'events/work-inputs' / ('brief-' + run_key)

    def compose_wins(argv, **kwargs):
        result = original(argv, **kwargs)
        if Path(argv[1]).name == 'x_connectivity.py':
            (scratch / 'artifact.json').write_text(json.dumps({'brief_text': 'already composed'}))
        return result

    monkeypatch.setattr(brief_runner.subprocess, 'run', compose_wins)
    assert brief_runner.run({**request, 'prepare': True}) == 'NO_NUDGES'
    assert not (scratch / 'prepared.json').exists()
    assert not (scratch / 'prepared').exists()


def test_welcome_optional_seeders_share_one_small_deadline_budget(procedure):
    request, state = procedure
    state['seed_timeout'] = True
    assert brief_runner.run({**request, 'kind': 'welcome'}) == 'A useful brief'
    assert ('style_extract.py', '', brief_runner.WELCOME_SEED_BUDGET_SECONDS) in state['calls']
    assert not any(name == 'prewarm_graph.py' for name, _, _ in state['calls'])


def test_source_and_calendar_manifest_survives_retry_and_revocation_blocks_old_text(procedure, tmp_path):
    request, state = procedure
    assert brief_runner.run(request) == 'A useful brief'
    manifest = json.loads((tmp_path / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    source = next(effect for effect in manifest['effects'] if effect['kind'] == 'source_permissions')
    assert source['sources'] == ['calendar', 'imessage'] and source['coverage_until']
    assert any(effect.get('calendar_event_id') == 'meeting-1' for effect in manifest['effects'])
    import source_context
    source_context.record_bridge_status({'sources': {'imessage': 'disabled'}})
    with pytest.raises(RuntimeError, match='permission changed'):
        brief_runner.run(request)
    # The stale composition is discarded with the refusal, so the bounded retry composes afresh
    # under current consent instead of reloading the same artifact into the same error.
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    assert not (scratch / 'artifact.json').exists()
    composes = len([c for c in state['calls'] if c[0] == 'compose_brief.py'])
    assert brief_runner.run(request) == 'A useful brief'
    assert len([c for c in state['calls'] if c[0] == 'compose_brief.py']) == composes + 1


def test_learning_admission_failure_does_not_withhold_validated_brief(procedure, tmp_path, monkeypatch, capsys):
    request, state = procedure
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    import work_queue
    original = work_queue.enqueue
    attempts = []
    def unavailable(*args, **kwargs):
        attempts.append(1)
        raise OSError('private source payload must not be logged')
    monkeypatch.setattr(work_queue, 'enqueue', unavailable)
    state['fail_phase'] = 'essential'
    assert brief_runner.run(request) == 'A useful brief'
    assert len(attempts) == 1
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    assert (scratch / 'artifact.json').exists() and (scratch / 'current').exists()
    assert 'private source payload' not in capsys.readouterr().err
    monkeypatch.setattr(work_queue, 'enqueue', original)
    state['fail_phase'] = ''
    assert brief_runner.run(request) == 'A useful brief'
    assert work_queue.claim(str(tmp_path), 'worker') is not None
    assert sum(name == 'compose_brief.py' for name, _, _ in state['calls']) == 1
