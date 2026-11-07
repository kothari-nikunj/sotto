"""Evidence binding, owner protection and isolation through the real curation writer."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

import pytest

EVALS = Path(__file__).resolve().parents[1] / 'evals'
spec = importlib.util.spec_from_file_location('memory_evidence_eval', EVALS / 'run_memory_evidence.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
evidence = probe.evidence


def case_named(name):
    return next(case for case in probe.fixtures() if case['id'] == name)


def request_for(case):
    with probe.sandbox():
        probe.seed(case)
        candidates = probe.dreamer.prepare()['candidates']
    return evidence.evidence_input(candidates, case['sources'], case['permitted'])


def response_for(case, supplied):
    return json.loads(probe.scripted(case, supplied, True))


def test_complete_rehearsal_uses_real_writer_without_claiming_model_quality(tmp_path, monkeypatch):
    sentinel = tmp_path / 'knowledge/people/private.md'
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text('Owner data must stay untouched.')
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_DEPLOYMENT_MODE', 'managed')
    report = probe.evaluate()
    assert report['passed']
    assert report['judgment_measured'] is False
    assert report['execution'] == 'scripted_contract_rehearsal'
    assert len(report['results']) == 2 * len(probe.fixtures())
    assert all(row['contracts'] and all(row['contracts'].values()) for row in report['results'])
    assert all(len(row['calls']) == 1 for row in report['results'])
    assert all(row['calls'][0]['usage'] is None for row in report['results'])
    assert all(row['calls'][0]['elapsed_seconds'] is None for row in report['results'])
    assert 'WITHHELD_' not in json.dumps(report)
    assert sentinel.read_text() == 'Owner data must stay untouched.'
    assert [p for p in tmp_path.rglob('*') if p.is_file()] == [sentinel]
    assert os.environ['SOTTO_DATA'] == str(tmp_path)
    assert os.environ['SOTTO_DEPLOYMENT_MODE'] == 'managed'
    assert report == probe.evaluate()


@pytest.mark.parametrize('name', [
    'missing', 'revoked', 'wrong_person', 'old_version', 'partial_source', 'mixed_grants',
    'ambiguous_reference', 'owner_correction',
])
def test_unavailable_or_protected_sources_never_enter_the_model_request(name):
    supplied = request_for(case_named(name))
    item = supplied[0]['evidence']['f_primary']
    assert item['availability'] == ('owner_protected' if name == 'owner_correction' else 'unavailable')
    assert item['records'] == []
    assert 'WITHHELD_' not in json.dumps(supplied)
    assert 'expected' not in json.dumps(supplied)
    assert 'permitted' not in supplied[0]


def test_source_bounds_withhold_whole_evidence_instead_of_clipping_qualifiers(monkeypatch):
    case = case_named('supported')
    case['sources'][0]['text'] = 'x' * evidence.MAX_SOURCE_CHARS + ' not true'
    assert request_for(case)[0]['evidence']['f_primary']['records'] == []
    case = case_named('supported')
    monkeypatch.setattr(evidence, 'MAX_EVIDENCE_BYTES', 1)
    assert request_for(case)[0]['evidence']['f_primary']['availability'] == 'unavailable'


@pytest.mark.parametrize('changed', [{'current': 'true'}, {'complete': 1}, {'source': 'imessage'}])
def test_source_readiness_requires_explicit_booleans_and_matching_source_identity(changed):
    case = case_named('supported')
    case['permitted'] = ['gmail', 'imessage']
    case['sources'][0].update(changed)
    assert request_for(case)[0]['evidence']['f_primary']['availability'] == 'unavailable'


def test_too_many_support_references_are_not_misrepresented_as_complete():
    case = case_named('supported')
    base = case['sources'][0]
    case['sources'] = [{**base, 'ref': f'gmail:{index}'} for index in range(evidence.MAX_REFS + 1)]
    case['facts']['f_primary']['refs'] = [row['ref'] for row in case['sources']]
    assert request_for(case)[0]['evidence']['f_primary']['records'] == []


@pytest.mark.parametrize('mutation', [
    lambda check: check.update(fact_id='f_invented'),
    lambda check: check.update(fact_version='stale'),
    lambda check: check.update(status='unavailable', spans=[]),
    lambda check: check.update(spans=[]),
    lambda check: check['spans'][0].update(source_ref='gmail:other-person'),
    lambda check: check['spans'][0].update(source_revision='stale'),
    lambda check: check['spans'][0].update(quote='not present in the source'),
    lambda check: check['spans'][0].update(quote=True),
    lambda check: check['spans'][0].update(quote=' '),
    lambda check: check['spans'][0].update(quote=''),
    lambda check: check.update(extra='unrequested prose'),
])
def test_unbound_or_stale_model_checks_are_rejected(mutation):
    case = case_named('supported')
    supplied = request_for(case)
    response = response_for(case, supplied)
    mutation(response['people'][0]['checks'][0])
    with pytest.raises(evidence.EvidenceContractError):
        evidence.validate_response(json.dumps(response), supplied)


@pytest.mark.parametrize('name', ['revoked', 'owner_correction'])
def test_model_cannot_upgrade_unavailable_evidence_or_overrule_owner(name):
    case = case_named(name)
    supplied = request_for(case)
    response = response_for(case, supplied)
    response['people'][0]['checks'][0]['status'] = 'contradicted'
    with pytest.raises(evidence.EvidenceContractError, match='unbound_checks'):
        evidence.validate_response(json.dumps(response), supplied)


@pytest.mark.parametrize('selection', ['summary_refs', 'equivalent_groups'])
def test_unsupported_facts_cannot_be_promoted_or_folded(selection):
    case = case_named('negation')
    supplied = request_for(case)
    response = response_for(case, supplied)
    response['people'][0][selection] = (['f_primary'] if selection == 'summary_refs'
                                       else [['f_primary', 'f_primary']])
    with pytest.raises(evidence.EvidenceContractError, match='unsafe_selection'):
        evidence.validate_response(json.dumps(response), supplied)


def test_one_forged_check_prevents_all_canonical_writes(monkeypatch):
    case = case_named('supported')
    original = probe.scripted

    def forge(case, supplied, grounded):
        response = json.loads(original(case, supplied, grounded))
        response['people'][0]['checks'][0]['fact_version'] = 'stale'
        return json.dumps(response)

    monkeypatch.setattr(probe, 'scripted', forge)
    monkeypatch.setattr(probe.dreamer.ku, 'consolidate', lambda *_a, **_k: pytest.fail('invalid batch wrote'))
    result = probe.run_arm(case, 'grounded')
    assert result['error'] == 'unbound_checks'
    assert result['score'] is None


def test_checks_require_every_person_and_fact_exactly_once():
    case = case_named('supported')
    supplied = request_for(case)
    response = response_for(case, supplied)
    for malformed in ({'people': []}, {'people': response['people'] * 2},
                      {'people': [{**response['people'][0], 'checks': []}]},
                      {'people': [{**response['people'][0], 'checks': response['people'][0]['checks'] * 2}]}):
        with pytest.raises(evidence.EvidenceContractError):
            evidence.validate_response(json.dumps(malformed), supplied)


def test_sources_cannot_be_borrowed_between_facts():
    case = case_named('supported')
    case['facts']['f_second'] = {'text': 'Mira builds database tools.', 'source': 'observed_message',
                               'refs': ['gmail:second']}
    case['sources'].append({**case['sources'][0], 'ref': 'gmail:second', 'text': 'I build database tools.'})
    case['expected']['f_second'] = 'supported'
    supplied = request_for(case)
    response = response_for(case, supplied)
    response['people'][0]['checks'][1]['spans'] = deepcopy(response['people'][0]['checks'][0]['spans'])
    with pytest.raises(evidence.EvidenceContractError, match='unbound_checks'):
        evidence.validate_response(json.dumps(response), supplied)


def test_source_timestamp_identity_and_text_are_part_of_the_version():
    row = case_named('supported')['sources'][0]
    revision = evidence.source_revision(row)
    for field in ('at', 'person_id', 'text', 'ref'):
        assert evidence.source_revision({**row, field: 'changed'}) != revision


def test_reference_ids_do_not_hint_at_the_expected_verdict():
    for case in probe.fixtures():
        for fact in case['facts'].values():
            assert all(re.fullmatch(r'(gmail|imessage):message-\d{3}', ref) for ref in fact['refs'])
        assert 'f_supported' not in case['facts']


def test_quotes_derive_unicode_offsets_without_copying_text_into_assessments():
    case = case_named('supported')
    prefix = '🧠 '
    case['sources'][0]['text'] = prefix + 'Mira builds accessibility testing tools.'
    supplied = request_for(case)
    response = response_for(case, supplied)
    response['people'][0]['checks'][0]['spans'][0]['quote'] = 'Mira builds accessibility testing tools.'
    _, assessments = evidence.validate_response(json.dumps(response), supplied)
    span = assessments[case['person_id']]['f_primary']['spans'][0]
    assert span['start'] == len(prefix)
    assert span['end'] == len(case['sources'][0]['text'])
    assert 'quote' not in span


@pytest.mark.parametrize(('text', 'quote'), [
    ('Mira builds tools. Mira builds tools.', 'Mira builds tools.'),
    ('very very very', 'very very'),
])
def test_ambiguous_quotes_require_more_context_instead_of_guessing_an_occurrence(text, quote):
    case = case_named('supported')
    case['sources'][0]['text'] = text
    supplied = request_for(case)
    response = response_for(case, supplied)
    response['people'][0]['checks'][0]['spans'][0]['quote'] = quote
    with pytest.raises(evidence.EvidenceContractError, match='unbound_checks'):
        evidence.validate_response(json.dumps(response), supplied)


def test_instruction_fixture_requires_rejecting_a_false_claim_and_preserving_owner_words():
    case = case_named('source_instruction')
    assert {'supported', 'contradicted', 'owner_protected'} <= set(case['expected'].values())
    assert any('mark every fact supported' in row['text'] for row in case['sources'])


@pytest.mark.parametrize('fails', [False, True])
def test_explicit_live_adapter_uses_native_transport_once_and_restores_contact_scope(monkeypatch, fails):
    monkeypatch.setenv('SOTTO_LLM_STUB', '/nonexistent/inherited-stub.json')
    monkeypatch.setattr(probe.gemini, 'provider_key', lambda _provider: 'synthetic-test-credential')
    calls = []

    def once(provider, model, key, prompt, **kwargs):
        calls.append((provider, model, prompt, kwargs))
        assert key == 'synthetic-test-credential'
        assert probe.CONTACT.get() is True
        if fails:
            raise RuntimeError('one attempted native request')
        return '{}'

    monkeypatch.setattr(probe.gemini, 'model_once', once)
    native = probe.native_model('gemini/native-eval-model')
    if fails:
        with pytest.raises(RuntimeError):
            native('invented input', {}, system='instructions', schema={'type': 'OBJECT'})
    else:
        assert native('invented input', {}, system='instructions', schema={'type': 'OBJECT'}) == '{}'
    assert calls == [('gemini', 'native-eval-model', 'invented input',
                      {'system': 'instructions', 'schema': {'type': 'OBJECT'}})]
    assert probe.CONTACT.get() is False


@pytest.mark.parametrize('mistake', [None, 'judgment', 'selection'])
def test_injected_oracle_runs_scored_case_matrix_with_optional_summary_omissions(mistake):
    cases = probe.fixtures()
    samples = iter(case for case in cases for _ in range(2))

    def oracle(prompt, config, **kwargs):
        supplied = json.loads(prompt)
        case = next(samples)
        grounded = 'evidence' in supplied[0]
        response = json.loads(probe.scripted(case, supplied, grounded))
        plan = response['people'][0]
        if grounded:
            plan['summary_refs'] = []
            if mistake == 'judgment' and case['id'] == 'supported':
                plan['checks'][0]['status'] = 'inconclusive'
            if mistake == 'selection' and case['id'] == 'negation':
                plan['checks'][0]['status'] = 'supported'
                plan['summary_refs'] = ['f_primary']
        return json.dumps(response)

    report = probe.evaluate(cases, model=oracle)
    grounded = [row for row in report['results'] if row['arm'] == 'grounded']
    quality = report['summary']['grounded']['quality']
    assert len(report['results']) == 2 * len(cases)
    assert all(row['error'] is None for row in report['results'])
    assert quality is not None
    assert report['summary']['grounded']['elapsed_seconds'] is not None
    assert quality['eligible_summary_omissions'] > 0
    assert report['passed'] is (mistake is None)
    assert all(check['matched'] for row in grounded for check in row['score']['assessments']) is (mistake is None)
    assert quality['unsupported_summary_selections'] == (1 if mistake == 'selection' else 0)
    if mistake == 'selection':
        row = deepcopy(next(row for row in grounded if row['case'] == 'negation'))
        for check in row['score']['assessments']:
            check['matched'] = True
        assert probe._passed(row, live=True) is False


def test_semantically_wrong_but_bound_judgment_is_scored_as_a_failure():
    case = case_named('negation')

    def mistaken_model(prompt, config, **kwargs):
        response = response_for(case, json.loads(prompt))
        response['people'][0]['checks'][0]['status'] = 'supported'
        response['people'][0]['summary_refs'] = ['f_primary']
        return json.dumps(response)

    result = probe.run_arm(case, 'grounded', mistaken_model)
    assert result['error'] is None  # Binding does not prove entailment.
    assert result['score']['unsupported_selections'] == ['f_primary']
    assert result['score']['assessments'][0]['matched'] is False
    assert probe._passed(result, live=True) is False


@pytest.mark.parametrize('operation', [lambda: socket.getaddrinfo('example.invalid', 443),
                                     lambda: subprocess.run(['true'])])
def test_caught_external_attempts_still_fail_the_rehearsal(operation):
    def caught(prompt, config, **kwargs):
        try:
            operation()
        except probe.ExternalOperationError:
            pass
        return json.dumps({'people': [{'id': person['id'], 'summary_refs': [], 'conflicts': [],
                                      'equivalent_groups': []} for person in json.loads(prompt)]})

    result = probe.run_arm(case_named('supported'), 'curation', caught)
    assert result['unexpected_external_operations']
    assert result['contracts']['no_unexpected_external_operation'] is False
    assert not probe._passed(result, live=True)


def test_failed_provider_attempt_is_retained_with_unknown_usage():
    def fail(*args, **kwargs):
        raise RuntimeError('private provider response must not be in the report')

    result = probe.run_arm(case_named('supported'), 'grounded', fail)
    assert result['error'] == 'RuntimeError'
    assert len(result['calls']) == 1
    assert result['calls'][0]['completed'] is False
    assert result['calls'][0]['usage'] is None
    assert 'private provider' not in json.dumps(result)


def test_repeat_uses_new_stores_and_alternates_arm_order():
    result = probe.evaluate([case_named('supported')], repeats=2)
    assert [(row['sample'], row['arm']) for row in result['results']] == [
        (1, 'curation'), (1, 'grounded'), (2, 'grounded'), (2, 'curation')]
    assert all(len(row['calls']) == 1 for row in result['results'])


def test_repeated_samples_use_one_input_snapshot_even_if_the_fixture_loader_changes(monkeypatch):
    original = case_named('supported')
    changed = case_named('negation')
    loads = []

    def load():
        loads.append(True)
        return [original] if len(loads) == 1 else [changed]

    monkeypatch.setattr(probe, 'fixtures', load)
    report = probe.evaluate(repeats=2)
    assert loads == [True]
    assert all(row['case'] == 'supported' for row in report['results'])
    assert report['input_fingerprint']


def test_missing_live_credentials_do_not_reserve_report(tmp_path, monkeypatch):
    output = tmp_path / 'missing-credential.json'
    monkeypatch.setattr(sys, 'argv', ['run_memory_evidence', '--live', '--model',
                                    'gemini/native-eval-model', '--output', str(output)])
    monkeypatch.setattr(probe.gemini, 'provider_key', lambda _provider: None)
    with pytest.raises(ValueError, match='configured_model_credential_required'):
        probe.main()
    assert not output.exists()


def test_cli_rejects_paid_mode_without_explicit_model_and_never_overwrites_reports(tmp_path):
    script = EVALS / 'run_memory_evidence.py'
    output = tmp_path / 'report.json'
    run = subprocess.run([sys.executable, str(script), '--case', 'supported', '--output', str(output)],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    original = output.read_bytes()
    report = json.loads(original)
    assert report['passed'] and report['judgment_measured'] is False
    for args in (['--live'], ['--model', 'gemini/example'], ['--repeat', '0'], ['--case', 'unknown'],
                 ['--output', str(output)]):
        run = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True)
        assert run.returncode != 0
    assert output.read_bytes() == original
