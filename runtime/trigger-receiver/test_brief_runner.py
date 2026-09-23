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
                              'extracted_knowledge': {'person_updates': []},
                              'loop_updates': [{'loopId': 'fixture', 'status': 'resolved'}]})
        if name == 'learn_step.py':
            assert argv[argv.index('--phase')+1] == 'essential'
            assert json.loads(path('--continuity').read_text())['new_actions'] == [{'summary': 'fixture debt'}]
            assert json.loads(path('--continuity').read_text())['loop_updates'] == [{'loopId': 'fixture', 'status': 'resolved'}]
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


def test_an_artifact_past_its_window_is_recomposed_and_a_fresh_one_is_reused(procedure, tmp_path):
    """A saved composition is the day's brief only while it is still today's news. The greeting and
    every "today" in it are stamped at compose time, so a retry hours later must re-gather rather
    than resend a "good morning" with hours-old content — while a retry minutes later still costs
    nothing. Same day, same work key, same marker: only the words are fresher."""
    request, state = procedure
    assert brief_runner.run(request) == 'A useful brief'
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    artifact = json.loads((scratch / 'artifact.json').read_text())
    assert artifact['composed_at'] > 0
    assert brief_runner.run(request) == 'A useful brief'
    assert [name for name, _, _ in state['calls']].count('compose_brief.py') == 1, 'fresh: reused'
    artifact['composed_at'] -= brief_runner.ARTIFACT_MAX_AGE_SECONDS + 1
    (scratch / 'artifact.json').write_text(json.dumps(artifact))
    assert brief_runner.run(request) == 'A useful brief'
    names = [name for name, _, _ in state['calls']]
    assert names.count('compose_brief.py') == 2 and names.count('gather_google.py') == 2
    assert json.loads((scratch / 'artifact.json').read_text())['composed_at'] > artifact['composed_at']
    # …and one written before this field existed carries no age it can prove: recompose.
    (scratch / 'artifact.json').write_text(json.dumps({'brief_text': 'A stale brief'}))
    assert brief_runner.run(request) == 'A useful brief'
    assert [name for name, _, _ in state['calls']].count('compose_brief.py') == 3


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


def test_managed_granola_capability_allows_scheduled_preparation_gather(
        procedure, tmp_path, monkeypatch):
    request, state = procedure
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    monkeypatch.setenv('SOTTO_TENANT_ID', 'test')
    (tmp_path / 'config').mkdir(exist_ok=True)
    (tmp_path / 'config/managed-capabilities.json').write_text(json.dumps({
        'tenant_id': 'test', 'sources': {
            'granola': {'consented': True, 'connected': True}}}))
    assert brief_runner.run({**request, 'prepare': True}) == 'NO_NUDGES'
    assert 'gather_granola.py' in [name for name, _, _ in state['calls']]


@pytest.mark.parametrize('mode', ['self-host', 'managed'])
@pytest.mark.parametrize('revoke', [False, True])
def test_prepared_x_reaches_brief_and_permission_manifest(procedure, tmp_path, monkeypatch, mode, revoke):
    request, state = procedure
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', mode)
    monkeypatch.setenv('X_BEARER_TOKEN', 'fixture')
    monkeypatch.delenv('SOTTO_X_STUB', raising=False)
    original = brief_runner.subprocess.run
    x = {'attendees': [{'handle': 'fixture', 'recent_posts': [{'text': 'Fresh X fixture'}]}],
         'warnings': ['X bookmarks unavailable']}
    def invoke(argv, **kwargs):
        result = original(argv, **kwargs)
        name = Path(argv[1]).name
        if name == 'x_connectivity.py':
            assert '--calendar' in argv and '--research' in argv
            Path(argv[argv.index('--out') + 1]).write_text(json.dumps(x))
        if name == 'compose_brief.py' and '--seed-snapshot' not in argv:
            context = json.loads(Path(argv[argv.index('--x-context') + 1]).read_text())
            if revoke:
                assert not context['attendees']
            else:
                assert context['attendees'][0]['recent_posts'][0]['text'] == 'Fresh X fixture'
                assert context['warnings'] == ['X bookmarks unavailable']
        return result
    monkeypatch.setattr(brief_runner.subprocess, 'run', invoke)
    assert brief_runner.run({**request, 'prepare': True}) == 'NO_NUDGES'
    assert 'x_connectivity.py' in [name for name, _, _ in state['calls']]
    if revoke:
        monkeypatch.delenv('X_BEARER_TOKEN')
    assert brief_runner.run(request) == 'A useful brief'
    manifest = json.loads((tmp_path / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    sources = next(effect['sources'] for effect in manifest['effects'] if effect['kind'] == 'source_permissions')
    assert ('x' in sources) is not revoke
    if not revoke:
        monkeypatch.delenv('X_BEARER_TOKEN')
        with pytest.raises(RuntimeError, match='permission changed'):
            brief_runner.run(request)


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


def test_saved_parking_notices_reach_outbox_effects_on_initial_run_and_retry(procedure, tmp_path, monkeypatch):
    request, state = procedure
    invoke = brief_runner.subprocess.run
    notice = {'kind': 'parking_notice', 'anchor_key': 'owed-task',
              'loop_version': 'b' * 64, 'touch': '2026-08-01'}

    def with_notice(argv, **kwargs):
        result = invoke(argv, **kwargs)
        if Path(argv[1]).name == 'compose_brief.py' and '--seed-snapshot' not in argv:
            value = json.loads(result.stdout)
            value['_parking_notices'] = [notice]
            result.stdout = json.dumps(value)
        return result

    monkeypatch.setattr(brief_runner.subprocess, 'run', with_notice)
    assert brief_runner.run(request) == 'A useful brief'
    manifest = tmp_path / ('events/delivery-effects-' + 'a' * 32 + '.json')
    assert notice in json.loads(manifest.read_text())['effects']
    before = list(state['calls'])
    manifest.unlink()  # Simulate a new delivery attempt reusing the saved composition.
    assert brief_runner.run(request) == 'A useful brief'
    assert state['calls'] == before
    assert notice in json.loads(manifest.read_text())['effects']


def _write_loop(tmp_path, row):
    import yaml
    path = tmp_path / 'knowledge/continuity/represented-loop.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('---\n' + yaml.safe_dump(row, sort_keys=True) + '---\n')
    return path


@pytest.mark.parametrize('change, expected', [('bookkeeping', True), ('restatement', True),
                                               ('new_obligation', False)])
def test_represented_loop_is_bound_to_post_learning_material_version(
        procedure, tmp_path, monkeypatch, change, expected):
    request, state = procedure
    monkeypatch.syspath_prepend(str(PACK / '_shared/lib'))
    monkeypatch.syspath_prepend(str(PACK / '_shared/scripts'))
    import delivery_effects
    import ledger_io

    row = {'anchor_key': 'contact:reply:thread-1', 'status': 'open',
           'summary': 'Send the revised proposal', 'action_type': 'reply',
           'contact_name': 'Jordan', 'created_at': '2026-09-01T12:00:00Z'}
    _write_loop(tmp_path, row)
    represented = {'anchor_key': row['anchor_key'],
                   'loop_version': delivery_effects.loop_version(row),
                   'loop_identity': delivery_effects.loop_identity(row)}
    invoke = brief_runner.subprocess.run

    def with_represented_loop(argv, **kwargs):
        result = invoke(argv, **kwargs)
        name = Path(argv[1]).name
        if name == 'compose_brief.py' and '--seed-snapshot' not in argv:
            value = json.loads(result.stdout)
            value['_represented_loops'] = [represented]
            result.stdout = json.dumps(value)
        if name == 'learn_step.py' and argv[argv.index('--phase') + 1] == 'essential':
            changed = ledger_io.load_entries()[0]
            if change == 'bookkeeping':
                changed['source_brief_at'] = '2026-09-08T09:00:00-07:00'
            elif change == 'restatement':
                changed['summary'] = 'Send the revised proposal and pricing appendix'
            else:
                changed['created_at'] = '2026-09-08T09:00:00-07:00'
                changed['source_message_id'] = 'new-message'
            _write_loop(tmp_path, changed)
        return result

    monkeypatch.setattr(brief_runner.subprocess, 'run', with_represented_loop)
    assert brief_runner.run({**request, 'work_key': f'represented-{change}'}) == 'A useful brief'
    manifest_path = tmp_path / ('events/delivery-effects-' + 'a' * 32 + '.json')
    effects = json.loads(manifest_path.read_text())['effects']
    surfaced = [effect for effect in effects if effect.get('kind') == 'loop_surfaced']
    assert bool(surfaced) is expected
    if expected:
        assert surfaced == [{'anchor_key': row['anchor_key'], 'kind': 'loop_surfaced',
                             'loop_identity': delivery_effects.loop_identity(
                                 ledger_io.load_entries()[0]),
                             'loop_version': delivery_effects.loop_version(
                                 ledger_io.load_entries()[0]), 'delivery_id': 'a' * 32}]
        assert delivery_effects.finalize(surfaced, {'accepted_at': 1788883200.0})
        assert delivery_effects.finalize(surfaced, {'accepted_at': 1788883200.0})
        saved = ledger_io.load_entries()[0]
        assert delivery_effects.delivered_surface_count(saved) == 1

        before = list(state['calls'])
        assert brief_runner.run({**request, 'work_key': f'represented-{change}'}) == 'A useful brief'
        assert state['calls'] == before
        effects = json.loads(manifest_path.read_text())['effects']
        assert [effect for effect in effects if effect.get('kind') == 'loop_surfaced'] == surfaced


def test_saved_review_candidate_is_staged_with_delivery_identity(procedure, tmp_path, monkeypatch):
    request, state = procedure
    candidate = {'kind': 'review_candidate_offer', 'canonical_id': 'c_aaa111bbb222',
                 'candidate_type': 'preferred_channel', 'payload_hash': 'b' * 64}
    invoke = brief_runner.subprocess.run

    def with_candidate(argv, **kwargs):
        result = invoke(argv, **kwargs)
        if Path(argv[1]).name == 'compose_brief.py' and '--seed-snapshot' not in argv:
            value = json.loads(result.stdout)
            value['_review_candidate'] = candidate
            result.stdout = json.dumps(value)
        return result

    monkeypatch.setattr(brief_runner.subprocess, 'run', with_candidate)
    assert brief_runner.run({**request, 'work_key': 'review-candidate'}) == 'A useful brief'
    manifest_path = tmp_path / ('events/delivery-effects-' + 'a' * 32 + '.json')
    effects = json.loads(manifest_path.read_text())['effects']
    assert {**candidate, 'delivery_id': 'a' * 32} in effects
    before = list(state['calls'])
    assert brief_runner.run({**request, 'work_key': 'review-candidate'}) == 'A useful brief'
    assert state['calls'] == before
    effects = json.loads(manifest_path.read_text())['effects']
    assert effects.count({**candidate, 'delivery_id': 'a' * 32}) == 1


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


@pytest.mark.parametrize('followup_completed', [False, True])
def test_recomposition_learns_new_generation_and_fences_old_followup(procedure, tmp_path, followup_completed):
    import work_queue
    request, state = procedure
    assert brief_runner.run(request) == 'A useful brief'
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    old_job = work_queue.claim(str(tmp_path), 'old-learner')
    old_request = json.loads(old_job['payload']['prompt'])
    if followup_completed:
        assert brief_runner.run(old_request) == 'NO_NUDGES'
        work_queue.finish(str(tmp_path), old_job['id'], 'old-learner')
    artifact = json.loads((scratch / 'artifact.json').read_text())
    artifact['composed_at'] -= brief_runner.ARTIFACT_MAX_AGE_SECONDS + 1
    (scratch / 'artifact.json').write_text(json.dumps(artifact))
    assert brief_runner.run(request) == 'A useful brief'
    assert sum(phase == 'essential' for _, phase, _ in state['calls']) == 2
    before = list(state['calls'])
    assert brief_runner.run(old_request) == 'NO_NUDGES'
    assert state['calls'] == before and (scratch / 'current').exists()
    if not followup_completed:
        work_queue.finish(str(tmp_path), old_job['id'], 'old-learner')
    new_job = work_queue.claim(str(tmp_path), 'new-learner')
    new_request = json.loads(new_job['payload']['prompt'])
    assert new_job['id'] != old_job['id']
    assert new_request['learning_revision'] != old_request['learning_revision']
    assert brief_runner.run(new_request) == 'NO_NUDGES'
    assert not (scratch / 'current').exists()


def test_failed_recomposition_never_exposes_partial_inputs_to_old_learner(procedure, tmp_path, monkeypatch):
    import work_queue
    request, state = procedure
    brief_runner.run(request)
    old_job = work_queue.claim(str(tmp_path), 'old')
    old_request = json.loads(old_job['payload']['prompt'])
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    artifact = json.loads((scratch / 'artifact.json').read_text())
    artifact['composed_at'] = 1
    (scratch / 'artifact.json').write_text(json.dumps(artifact))
    original = brief_runner.subprocess.run
    def crash(argv, **kwargs):
        if Path(argv[1]).name == 'compose_brief.py':
            raise RuntimeError('synthetic composition interruption')
        return original(argv, **kwargs)
    monkeypatch.setattr(brief_runner.subprocess, 'run', crash)
    with pytest.raises(RuntimeError, match='synthetic composition'):
        brief_runner.run(request)
    assert not (scratch / 'artifact.json').exists()
    before = list(state['calls'])
    assert brief_runner.run(old_request) == 'NO_NUDGES'
    assert state['calls'] == before
    monkeypatch.setattr(brief_runner.subprocess, 'run', original)
    assert brief_runner.run(request) == 'A useful brief'
    assert sum(phase == 'essential' for _, phase, _ in state['calls']) == 2


def test_composition_age_is_staged_for_every_delivery_attempt(procedure, tmp_path):
    import delivery_effects
    request, _ = procedure
    brief_runner.run(request)
    manifest = json.loads((tmp_path / ('events/delivery-effects-' + 'a' * 32 + '.json')).read_text())
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    artifact = json.loads((scratch / 'artifact.json').read_text())
    expiry = artifact['composed_at'] + brief_runner.ARTIFACT_MAX_AGE_SECONDS
    assert delivery_effects.valid(manifest['effects'], expiry - 1)
    assert not delivery_effects.valid(manifest['effects'], expiry)


def test_pre_upgrade_artifact_can_still_admit_its_legacy_learning_job(procedure, tmp_path):
    import work_queue
    request, state = procedure
    brief_runner.run(request)
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    artifact = json.loads((scratch / 'artifact.json').read_text())
    artifact.pop('learning_revision')  # the persisted shape before this change
    (scratch / 'artifact.json').write_text(json.dumps(artifact))
    before = list(state['calls'])
    assert brief_runner.run(request) == 'A useful brief'
    assert state['calls'] == before
    run_key = hashlib.sha256(request['work_key'].encode()).hexdigest()[:24]
    legacy = work_queue.get(str(tmp_path), work_queue.job_id_for('run', 'brief-learn:' + run_key))
    assert legacy is not None
    followup = json.loads(legacy['payload']['prompt'])
    followup.pop('learning_revision', None)  # pre-upgrade queued requests omitted it as well
    assert brief_runner.run(followup) == 'NO_NUDGES'
    assert not (scratch / 'current').exists()


def test_generation_body_timeout_is_not_classified_as_contention(tmp_path):
    with pytest.raises(TimeoutError, match='operation timed out'):
        with brief_runner._generation_lock(tmp_path):
            raise TimeoutError('operation timed out')
