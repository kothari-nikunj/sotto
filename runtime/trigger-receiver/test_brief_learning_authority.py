"""Delayed brief learning must keep the authority of each saved proposal."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
import brief_runner
import test_brief_runner

PACK = test_brief_runner.PACK
procedure = test_brief_runner.procedure

REAL_SUBPROCESS_RUN = subprocess.run


@pytest.mark.parametrize('source', ['imessage', 'whatsapp', 'gmail', 'calendar', 'granola'])
def test_delayed_learning_revalidates_revoked_proposals(procedure, tmp_path, monkeypatch, source):
    request, state = procedure
    ref = source + ':record-1'
    state['artifact'] = {'brief_text': 'A useful brief', 'actions': [],
        '_source_permissions': [source], 'learning_evidence': {'accepted': 1, 'rejected': 0},
        'extracted_knowledge': {'person_updates': [{'person_name': 'Dana', 'identifier': '+15550000101',
            'profile_patch': {'title': 'Engineer'}, 'relations': [{'type': 'colleague', 'other_person_name': 'Lee'}],
            'facts': [{'fact': 'Builds buoys.', 'source_ref': ref}]}],
            'company_updates': [{'company_name': 'Buoy Co', 'about': 'Builds buoys.'}]}}
    state['fail_phase'] = 'essential'
    assert brief_runner.run(request) == 'A useful brief'
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    artifact = json.loads((scratch / 'artifact.json').read_text())
    consent = tmp_path / 'config/source-state.json'
    consent.parent.mkdir(parents=True, exist_ok=True)
    consent.write_text(json.dumps({'sources': {source: {'status': 'disabled'}}}))
    import source_context
    monkeypatch.setenv('SOTTO_PROACTIVE_CAPTURED_SOURCES', json.dumps(
        sorted(set(source_context.CONTEXT_SOURCES) - {source})))
    stub = brief_runner.subprocess.run
    seen = []
    def invoke(argv, **kwargs):
        if '--knowledge-out' in argv:
            seen.append(json.loads(Path(argv[argv.index('--knowledge-out') + 1]).read_text()))
        return stub(argv, **kwargs)
    monkeypatch.setattr(brief_runner.subprocess, 'run', invoke)
    retry = {**request, 'learn_only': True, 'learning_revision': artifact['learning_revision']}
    with pytest.raises(RuntimeError, match='learn_step.py failed'):
        brief_runner.run(retry)
    # Restart after a source is reenabled must not resurrect the now unsupported proposals.
    consent.write_text(json.dumps({'sources': {source: {'status': 'ok'}}}))
    monkeypatch.delenv('SOTTO_PROACTIVE_CAPTURED_SOURCES')
    state['fail_phase'] = ''
    assert brief_runner.run(retry) == 'NO_NUDGES'
    assert seen[1]
    for payload in seen:
        assert payload['person_updates'] == []
        assert payload['company_updates'] == []
        assert payload['_learning_evidence']['rejected'] > 0


def test_delayed_mixed_learning_writes_only_permitted_facts_and_retries_idempotently(
        procedure, tmp_path, monkeypatch):
    import sys
    from datetime import datetime, timezone
    from render_local import message_version_reference
    sys.path.insert(0, str(PACK / '_shared' / 'knowledge'))
    import knowledge as kg
    from knowledge_query import pack_person
    real_run = REAL_SUBPROCESS_RUN
    request, state = procedure
    permitted = {'id': 'wa-1', 'source_id': 'wa-1', 'handle': '+15550000102',
                 'text': 'Lee builds buoys.', 'timestamp': '2026-09-08T10:00:00Z'}
    permitted.update(source_ref='msg:v2:whatsapp:' + 'a' * 64, content_version='b' * 64)
    canonical = message_version_reference(permitted, 'whatsapp')
    Path(request['payload_path']).write_text(json.dumps({
        'imessage': [{'id': 'im-1', 'text': 'Dana builds rafts.'}], 'whatsapp': [permitted]}))
    state['artifact'] = {'brief_text': 'A useful brief', 'actions': [],
        'learning_evidence': {'accepted': 2, 'rejected': 0},
        'extracted_knowledge': {'person_updates': [
            {'person_name': 'Dana', 'identifier': '+15550000101',
             'profile_patch': {'title': 'Raft engineer'},
             'facts': [{'fact': 'Builds rafts.', 'confidence': 0.65,
                        'source_ref': 'imessage:im-1', 'source': 'brief_extraction'}]},
            {'person_name': 'Lee', 'identifier': '+15550000102',
             'profile_patch': {'title': 'Buoy engineer'},
             'facts': [{'fact': 'Builds buoys.', 'confidence': 0.65,
                        'source_ref': canonical, 'source': 'brief_extraction'},
                       {'fact': 'Shares rafts and buoys.', 'confidence': 0.65,
                        'source_ref': canonical, 'evidence_refs': ['imessage:im-1'],
                        'source': 'brief_extraction'}]}]}}
    state['fail_phase'] = 'essential'
    assert brief_runner.run(request) == 'A useful brief'
    scratch = next((tmp_path / 'events/work-inputs').glob('brief-*'))
    artifact = json.loads((scratch / 'artifact.json').read_text())
    consent = tmp_path / 'config/source-state.json'
    consent.parent.mkdir(parents=True, exist_ok=True)
    consent.write_text(json.dumps({'sources': {'imessage': {'status': 'disabled'}}}))
    stub = brief_runner.subprocess.run
    receipts = []
    payloads = []
    def invoke(argv, **kwargs):
        if '--phase' in argv and argv[argv.index('--phase') + 1] == 'essential':
            payloads.append(Path(argv[argv.index('--knowledge-out') + 1]).read_bytes())
            result = real_run(argv, **kwargs)
            receipts.append(json.loads(result.stdout.strip().splitlines()[-1]))
            return result
        return stub(argv, **kwargs)
    monkeypatch.setattr(brief_runner.subprocess, 'run', invoke)
    retry = {**request, 'learn_only': True, 'learning_revision': artifact['learning_revision']}
    assert brief_runner.run(retry) == 'NO_NUDGES'
    assert kg.find_person_file(identifier='+15550000101') is None
    path = kg.find_person_file(identifier='+15550000102')
    person = kg.parse_person_file(Path(path).read_text())
    assert [fact.text for fact in person.facts.values()] == ['Builds buoys.']
    assert not person.title
    assert 'Builds buoys.' in pack_person(person, True, datetime(2026, 9, 8, tzinfo=timezone.utc))
    assert receipts[0]['completion'] == 'partial'
    assert receipts[0]['steps']['knowledge']['status'] == 'partial'
    payload = payloads[0]
    assert receipts[0]['input_evidence']['knowledge']['sha256'] == hashlib.sha256(payload).hexdigest()
    before = Path(path).read_bytes()
    assert brief_runner.run(retry) == 'NO_NUDGES'
    assert len(receipts) == 1
    assert Path(path).read_bytes() == before


@pytest.mark.parametrize('malformed', [None, 'invalid person', 7])
def test_delayed_projection_keeps_valid_sibling_after_normalized_malformed_proposal(monkeypatch, malformed):
    monkeypatch.syspath_prepend(str(PACK / '_shared/scripts'))
    import compose_brief
    import source_context
    from brief_evidence import project_learning_payload
    monkeypatch.setattr(source_context, 'allowed', lambda source: source == 'gmail')
    normalized = compose_brief._normalize_output({'extracted_knowledge': {'person_updates': [
        malformed, {'person_name': 'Dana', 'facts': [{'fact': 'Builds buoys.',
            'source_ref': 'gmail:mail-1'}]}]}})
    projected = project_learning_payload(normalized['extracted_knowledge'], ['gmail', 'imessage'],
        {'google': {'emails': [{'id': 'mail-1', 'date': '2026-11-03T12:00:00Z'}]}})
    assert [person['person_name'] for person in projected['person_updates']] == ['Dana']
    assert projected['_learning_evidence'] == {'accepted': 1, 'rejected': 1}
