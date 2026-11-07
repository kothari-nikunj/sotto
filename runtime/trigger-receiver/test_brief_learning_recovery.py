"""Required brief writes survive queue outages and generation replacement."""
import json
from pathlib import Path

import pytest
import brief_runner
from test_brief_runner import PACK, procedure as procedure  # noqa: PLC0414 — pytest fixture export


def test_unqueued_essential_failure_cannot_acknowledge_success(procedure, tmp_path, monkeypatch):
    request, state = procedure
    import work_queue

    original_enqueue = work_queue.enqueue
    parent_id = original_enqueue(str(tmp_path), 'run', {'prompt': json.dumps(request)},
                                 key='accepted-brief', valid_until=brief_runner.time.time() + 3600)
    parent = work_queue.claim(str(tmp_path), 'before-restart')
    assert parent['id'] == parent_id

    def unavailable(*args, **kwargs):
        raise OSError('synthetic queue outage')

    monkeypatch.setattr(work_queue, 'enqueue', unavailable)
    state['fail_phase'] = 'essential'
    with pytest.raises(RuntimeError, match='required learning retry admission failed'):
        brief_runner.run(request)
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    assert (scratch / 'current/continuity.json').exists()
    assert not (scratch / 'essential-done.json').exists()
    effects_path = tmp_path / 'events' / ('delivery-effects-' + 'a' * 32 + '.json')
    first_effects = json.loads(effects_path.read_text())['effects']
    assert {effect['kind'] for effect in first_effects} >= {'source_permissions', 'eligibility'}
    assert work_queue.fail(str(tmp_path), parent_id, parent['owner'], 'OSError') == 'pending'
    monkeypatch.setattr(work_queue, 'enqueue', original_enqueue)
    state['fail_phase'] = ''
    # A new worker resumes only the admitted parent; no new owner request or admission.
    import importlib.util
    spec = importlib.util.spec_from_file_location('restarted_brief_runner', brief_runner.__file__)
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    resumed = work_queue.claim(str(tmp_path), 'after-restart', now=brief_runner.time.time() + 61)
    assert resumed['id'] == parent_id and resumed['result'] is None
    text = restarted.run(json.loads(resumed['payload']['prompt']))
    assert text == 'A useful brief'
    assert json.loads(effects_path.read_text())['effects'] == first_effects
    work_queue.save_result(str(tmp_path), parent_id, resumed['owner'], {'text': text})
    assert work_queue.finish(str(tmp_path), parent_id, resumed['owner'])
    learner = work_queue.claim(str(tmp_path), 'learner-after-restart')
    assert learner is not None and learner['id'] != parent_id
    assert brief_runner.run(json.loads(learner['payload']['prompt'])) == 'NO_NUDGES'
    assert work_queue.finish(str(tmp_path), learner['id'], learner['owner'])
    assert (scratch / 'essential-done.json').exists()
    assert not (scratch / 'current').exists()
    assert sum(name == 'compose_brief.py' for name, _, _ in state['calls']) == 1


def test_recomposition_commits_pending_old_actions_before_replacing_inputs(
        procedure, tmp_path, monkeypatch):
    request, state = procedure
    state['artifact'] = {'brief_text': 'A useful brief',
                         'actions': [{'summary': 'Preserve the first obligation'}]}
    state['fail_phase'] = 'essential'
    assert brief_runner.run(request) == 'A useful brief'
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    artifact = json.loads((scratch / 'artifact.json').read_text())
    artifact['composed_at'] = 1
    (scratch / 'artifact.json').write_text(json.dumps(artifact))
    state['artifact'] = {'brief_text': 'A useful brief', 'actions': []}
    old_inputs = (scratch / 'current/continuity.json').read_text()
    with pytest.raises(RuntimeError, match='learn_step.py failed'):
        brief_runner.run(request)
    assert (scratch / 'current/continuity.json').read_text() == old_inputs
    assert json.loads((scratch / 'artifact.json').read_text()) == artifact
    state['fail_phase'] = ''
    applied = []
    invoke = brief_runner.subprocess.run

    def capture(argv, **kwargs):
        result = invoke(argv, **kwargs)
        if '--phase' in argv and argv[argv.index('--phase') + 1] == 'essential':
            continuity = json.loads(Path(argv[argv.index('--continuity') + 1]).read_text())
            applied.extend(continuity.get('new_actions', []))
        return result

    monkeypatch.setattr(brief_runner.subprocess, 'run', capture)
    assert brief_runner.run(request) == 'A useful brief'
    assert applied == [{'summary': 'Preserve the first obligation'}]


def test_partial_essential_apply_replayed_before_recomposition_is_idempotent(
        procedure, tmp_path, monkeypatch):
    import importlib.util
    from datetime import datetime, timezone
    monkeypatch.syspath_prepend(str(PACK / '_shared/knowledge'))
    import knowledge
    spec = importlib.util.spec_from_file_location(
        'brief_recovery_knowledge_update', PACK / '_shared/knowledge/knowledge_update.py')
    update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update)
    request, state = procedure
    state['artifact'] = {'brief_text': 'A useful brief', 'actions': [],
                         'extracted_knowledge': {'person_updates': [{
                             'identifier': 'invented@example.com', 'person_name': 'Invented',
                             'facts': [{'fact': 'Builds ocean buoys.', 'confidence': .65,
                                        'source': 'observed_message', 'source_ref': 'gmail:invented',
                                        'evidence_refs': ['gmail:invented'],
                                        'observed_date': '2026-09-08', 'memory_type': 'context'}]}]}}
    invoke = brief_runner.subprocess.run
    applied = []

    def partially_apply(argv, **kwargs):
        if '--phase' in argv and argv[argv.index('--phase') + 1] == 'essential':
            payload = json.loads(Path(argv[argv.index('--knowledge-out') + 1]).read_text())
            if payload.get('person_updates'):
                update.apply(payload, now=datetime(2026, 9, 8, 12, tzinfo=timezone.utc))
                applied.append(payload)
        return invoke(argv, **kwargs)

    monkeypatch.setattr(brief_runner.subprocess, 'run', partially_apply)
    state['fail_phase'] = 'essential'
    assert brief_runner.run(request) == 'A useful brief'
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    artifact = json.loads((scratch / 'artifact.json').read_text())
    artifact['composed_at'] = 1
    (scratch / 'artifact.json').write_text(json.dumps(artifact))
    state['artifact'] = {'brief_text': 'A useful brief', 'actions': []}
    state['fail_phase'] = ''
    assert brief_runner.run(request) == 'A useful brief'
    person = knowledge.parse_person_file(Path(knowledge.find_person_file(
        identifier='invented@example.com')).read_text())
    first_apply, resumed_apply = applied
    assert first_apply == resumed_apply
    assert [fact.text for fact in person.facts.values()] == ['Builds ocean buoys.']
    assert next(iter(person.facts.values())).evidence_refs == ['gmail:invented']


def test_replacement_essential_replay_rechecks_revoked_source(procedure, tmp_path, monkeypatch):
    import source_context
    request, state = procedure
    state['fail_phase'] = 'essential'
    assert brief_runner.run(request) == 'A useful brief'
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    artifact = json.loads((scratch / 'artifact.json').read_text())
    artifact['composed_at'] = 1
    (scratch / 'artifact.json').write_text(json.dumps(artifact))
    state['fail_phase'] = ''
    allowed = source_context.allowed
    monkeypatch.setattr(source_context, 'allowed', lambda source: source != 'imessage' and allowed(source))
    invoke = brief_runner.subprocess.run
    replayed_local = []

    def observe(argv, **kwargs):
        if '--phase' in argv and argv[argv.index('--phase') + 1] == 'essential':
            replayed_local.append(json.loads(Path(argv[argv.index('--local') + 1]).read_text()))
        return invoke(argv, **kwargs)

    monkeypatch.setattr(brief_runner.subprocess, 'run', observe)
    assert brief_runner.run(request) == 'A useful brief'
    old_local, new_local = replayed_local
    assert not old_local.get('imessage') and not new_local.get('imessage')


def test_persistent_old_essential_failure_withholds_replacement_until_work_expires(
        procedure, tmp_path):
    import work_queue
    request, state = procedure
    state['fail_phase'] = 'essential'
    assert brief_runner.run(request) == 'A useful brief'
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    artifact = json.loads((scratch / 'artifact.json').read_text())
    artifact['composed_at'] = 1
    (scratch / 'artifact.json').write_text(json.dumps(artifact))
    old_inputs = {path.name: path.read_bytes() for path in (scratch / 'current').iterdir()
                  if path.suffix == '.json'}
    now = brief_runner.time.time()
    parent_id = work_queue.enqueue(str(tmp_path), 'run', {'prompt': json.dumps(request)},
                                   key='replacement-brief', valid_until=now + 100)
    for owner, attempt_at, expected in (('first-replacement', now + 1, 'pending'),
                                        ('restarted-replacement', now + 62, 'expired')):
        parent = work_queue.claim(str(tmp_path), owner, now=attempt_at)
        assert parent['id'] == parent_id
        with pytest.raises(RuntimeError, match='learn_step.py failed'):
            brief_runner.run(json.loads(parent['payload']['prompt']))
        assert work_queue.fail(str(tmp_path), parent_id, owner, 'RuntimeError',
                               now=attempt_at) == expected
        assert json.loads((scratch / 'artifact.json').read_text()) == artifact
        assert {path.name: path.read_bytes() for path in (scratch / 'current').iterdir()
                if path.suffix == '.json'} == old_inputs
        assert not (scratch / 'essential-done.json').exists()
    assert work_queue.get(str(tmp_path), parent_id)['status'] == 'expired'
    assert sum(name == 'compose_brief.py' for name, _, _ in state['calls']) == 1
