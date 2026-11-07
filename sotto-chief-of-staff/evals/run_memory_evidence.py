#!/usr/bin/env python3
"""Compare current and source-grounded Dreamer in disposable, invented memory stores.

Default: scripted contract rehearsal, NOT a model-quality score. --live explicitly buys one
native model attempt per arm/case; no source adapters, private data or production activation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

PACK = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(PACK / '_shared/lib'),
                str(PACK / '_shared/scripts'), str(PACK / '_shared/knowledge')]
import dreamer
import gemini
import knowledge as kg
import memory_evidence as evidence
import metrics
import model_work

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
FIXTURES = Path(__file__).with_name('fixtures') / 'memory_evidence.json'
MAX_REPEATS = 10
GUARD = ContextVar('memory_eval_guard', default=None)
CONTACT = ContextVar('memory_eval_contact', default=False)


class ExternalOperationError(RuntimeError):
    """A forbidden operation was attempted, even if a production function catches it."""


def _audit(event, _args):
    denied = GUARD.get()
    if denied is None:
        return
    process = event in {'subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.spawn', 'os.exec', 'os.fork', 'os.forkpty'}
    if process or event.startswith('socket.') and not CONTACT.get():
        denied.append(event)
        raise ExternalOperationError('unexpected_external_operation')


sys.addaudithook(_audit)


@contextmanager
def sandbox():
    """Never adopt an inherited volume, deployment mode, schedule or conversation session."""
    with tempfile.TemporaryDirectory(prefix='sotto-memory-evidence-') as directory:
        with patch.dict(os.environ, {'SOTTO_DATA': directory, 'SOTTO_TIMEZONE': '+00:00',
                                     'SOTTO_DEPLOYMENT_MODE': 'selfhost',
                                     'SOTTO_BACKGROUND_UNMETERED': 'true'}):
            yield Path(directory)


def fixtures():
    return json.loads(FIXTURES.read_text())['cases']


def seed(case):
    person = kg.PersonFile(canonical_id=case['person_id'], name=case['name'])
    for fid, fact in case['facts'].items():
        person.facts[fid] = kg.FactMeta(text=fact['text'], source=fact['source'],
            source_ref=next(iter(fact['refs']), ''), evidence_refs=list(fact['refs']),
            first=NOW.date().isoformat(), last=NOW.date().isoformat())
    path = Path(kg.people_dir()) / f'{person.canonical_id}.md'
    kg.write_person_file(str(path), person, NOW)
    return path


def scripted(case, supplied, grounded):
    """An explicit test oracle, never the live adapter and never model-quality evidence."""
    plans = []
    for person in supplied:
        plan = {'id': person['id'], 'summary_refs': list(person['facts'])[:4],
                'conflicts': [], 'equivalent_groups': []}
        if grounded:
            checks = []
            for fid, item in person['evidence'].items():
                status = case['expected'][fid]
                spans = [{'source_ref': row['source_ref'], 'source_revision': row['source_revision'],
                          'quote': row['text']} for row in item['records']]
                checks.append({'fact_id': fid, 'fact_version': item['fact_version'],
                               'status': status, 'spans': spans})
            plan['checks'] = checks
            plan['summary_refs'] = [check['fact_id'] for check in checks
                                    if check['status'] in evidence.SELECTABLE][:4]
        plans.append(plan)
    return json.dumps({'people': plans})


def native_model(model_ref):
    """Use the existing native client and credential selectors; no new provider implementation."""
    provider, model = gemini.parse_model_ref(model_ref)
    if provider not in gemini.PROVIDERS or not model:
        raise ValueError('unsupported_model')
    key = gemini.provider_key(provider)
    if not key:
        raise ValueError('configured_model_credential_required')

    def call(prompt, config, **kwargs):
        token = CONTACT.set(True)
        try:
            return gemini.model_once(provider, model, key, prompt,
                                     system=kwargs.get('system'), schema=kwargs.get('schema'))
        finally:
            CONTACT.reset(token)
    return call


class ModelArm:
    def __init__(self, case, arm, model):
        self.case, self.arm, self.model = case, arm, model
        self.calls = []
        self.assessments = {}

    def __call__(self, prompt, config, **kwargs):
        supplied = json.loads(prompt)
        grounded = self.arm == 'grounded'
        if grounded:
            supplied = evidence.evidence_input(supplied, self.case['sources'], self.case['permitted'])
            prompt = json.dumps(supplied, ensure_ascii=False)
            kwargs = {**kwargs, 'system': kwargs['system'] + '\n' + evidence.GUIDANCE,
                      'schema': evidence.schema(kwargs['schema'])}
        call = {'input_bytes': len(prompt.encode()), 'system_bytes': len(kwargs['system'].encode()),
                'output_bytes': None, 'elapsed_seconds': None, 'usage': None, 'completed': False}
        self.calls.append(call)
        metrics.start_run()
        started = time.monotonic()
        try:
            raw = (self.model(prompt, config, **kwargs) if self.model
                   else scripted(self.case, supplied, grounded))
            call['output_bytes'] = len(raw.encode())
            call['completed'] = True
        finally:
            if self.model:
                call['elapsed_seconds'] = round(time.monotonic() - started, 6)
                usage = metrics.summary()
                # No recorded response is unknown usage, including a failed provider attempt.
                call['usage'] = usage if usage['calls'] else None
        if grounded:
            clean, self.assessments = evidence.validate_response(raw, supplied)
            return json.dumps(clean)
        return raw


def _readback(path):
    person = kg.parse_person_file(path.read_text())
    return {'facts': {fid: fact.to_yaml_dict() for fid, fact in person.facts.items()},
            'summary_refs': list(person.summary_refs)}


def score(case, stored, assessments):
    selected = stored['summary_refs']
    eligible = {fid for fid, status in case['expected'].items() if status in evidence.SELECTABLE}
    checks = assessments.get(case['person_id'], {})
    return {'selected': selected, 'eligible': sorted(eligible),
            'unsupported_selections': [fid for fid in selected if fid not in eligible],
            'eligible_omitted': sorted(eligible - set(selected)),
            'assessments': [{'fact_id': fid, 'expected': status,
                             'observed': checks.get(fid, {}).get('status'),
                             'fact_version': checks.get(fid, {}).get('fact_version'),
                             'spans': checks.get(fid, {}).get('spans'),
                             'matched': checks.get(fid, {}).get('status') == status}
                            for fid, status in case['expected'].items()] if checks else []}


def run_arm(case, arm, model=None):
    adapter = ModelArm(case, arm, model)
    denied = []
    token = GUARD.set(denied)
    result = {'case': case['id'], 'arm': arm, 'error': None, 'contracts': {}, 'score': None}
    try:
        with sandbox(), redirect_stderr(io.StringIO()):
            path = seed(case)
            before = _readback(path)
            receipt = dreamer.run(adapter, NOW)
            stored = _readback(path)
            committed = path.read_bytes()
            calls_before = len(adapter.calls)
            repeated = dreamer.run(adapter, NOW)
            result['contracts'] = {
                'canonical_write_completed': receipt.get('reviewed') == 1,
                'facts_and_provenance_preserved': stored['facts'] == before['facts'],
                'unchanged_repeat_has_no_model_call': len(adapter.calls) == calls_before,
                'unchanged_repeat_has_no_write': path.read_bytes() == committed,
                'unchanged_repeat_has_no_review': repeated.get('reviewed') == 0}
            result['score'] = score(case, stored, adapter.assessments)
    except Exception as error:
        result['error'] = (str(error) if isinstance(error, evidence.EvidenceContractError)
                           else type(error).__name__)
    finally:
        GUARD.reset(token)
    result['contracts']['no_unexpected_external_operation'] = not denied
    result['calls'] = adapter.calls
    result['unexpected_external_operations'] = denied
    return result


def _passed(row, live):
    if row['error'] or not all(row['contracts'].values()):
        return False
    if not live or row['arm'] != 'grounded':
        return True
    scored = row['score']
    return (not scored['unsupported_selections']
            and all(check['matched'] for check in scored['assessments']))


def summary(rows, live):
    result = {}
    for arm in ('curation', 'grounded'):
        selected = [row for row in rows if row['arm'] == arm]
        calls = [call for row in selected for call in row['calls']]
        scored = [row['score'] for row in selected if row['score'] is not None]
        assessments = [check for score in scored for check in score['assessments']]
        semantic = [check for check in assessments if check['expected'] in
                    ('supported', 'contradicted', 'inconclusive')]
        policy = [check for check in assessments if check['expected'] in
                  ('owner_protected', 'unavailable')]
        result[arm] = {
            'samples': len(selected), 'completed_samples': len(scored),
            'failed_samples': sum(not row['passed'] for row in selected),
            'model_attempts': len(calls) if live else 0, 'scripted_calls': 0 if live else len(calls),
            'prompt_and_system_bytes': sum(call['input_bytes'] + call['system_bytes'] for call in calls),
            'elapsed_seconds': round(sum(call['elapsed_seconds'] or 0 for call in calls), 6) if live else None,
            'quality': {'unsupported_summary_selections': sum(len(s['unsupported_selections']) for s in scored),
                        'eligible_summary_omissions': sum(len(s['eligible_omitted']) for s in scored),
                        'semantic_judgments': len(semantic),
                        'correct_semantic_judgments': sum(c['matched'] for c in semantic),
                        'policy_checks': len(policy),
                        'correct_policy_checks': sum(c['matched'] for c in policy)} if live else None}
    return result


def evaluate(cases=None, *, model=None, repeats=1):
    cases = deepcopy(cases if cases is not None else fixtures())
    fingerprints = {str(path.relative_to(PACK)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (FIXTURES, Path(__file__).resolve(), Path(evidence.__file__), Path(dreamer.__file__),
                     Path(dreamer.ku.__file__), Path(kg.__file__), Path(gemini.__file__),
                     Path(gemini.gemini_transport.__file__), PACK / '_shared/references/writing-style.md',
                     Path(model_work.__file__), Path(metrics.__file__))}
    rows = []
    for sample in range(repeats):
        for case in cases:
            # Alternate order to reduce systematic warm-up/order bias in repeated live samples.
            for arm in (('curation', 'grounded') if sample % 2 == 0 else ('grounded', 'curation')):
                row = run_arm(deepcopy(case), arm, model)
                row.update(sample=sample + 1, passed=_passed(row, model is not None))
                rows.append(row)
    return {'schema_version': 1, 'execution': 'live_model' if model else 'scripted_contract_rehearsal',
            'judgment_measured': model is not None,
            'scope': 'Invented evidence; canonical curation and readback only, not answer quality or production.',
            'quality_gate': 'Grounded judgments/selection only; baseline quality is reported, not required to improve.',
            'source_fingerprints': fingerprints,
            'input_fingerprint': hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'passed': all(row['passed'] for row in rows), 'summary': summary(rows, model is not None),
            'results': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='make paid model requests on invented inputs')
    parser.add_argument('--model', help='explicit provider/model for --live, using existing configured credentials')
    parser.add_argument('--case', action='append', dest='cases', help='run only this fixture ID (repeatable)')
    parser.add_argument('--repeat', type=int, default=1, help='fresh isolated samples per arm, 1 through 10')
    parser.add_argument('--output', type=Path, help='create a new report file; existing files are refused')
    args = parser.parse_args()
    if bool(args.model) != args.live:
        parser.error('--live and --model must be supplied together')
    if not 1 <= args.repeat <= MAX_REPEATS:
        parser.error('--repeat must be between 1 and 10')
    selected = [case for case in fixtures() if not args.cases or case['id'] in args.cases]
    if not selected or args.cases and set(args.cases) - {case['id'] for case in selected}:
        parser.error('unknown case')
    with sandbox(), patch.dict(os.environ, {'SOTTO_BRIEF_MODEL': args.model or ''}):
        model = native_model(args.model) if args.live else None
        # Reserve output before any paid contact; an existing report must never cause wasted calls.
        with (args.output.open('x') if args.output else context_stdout()) as output:
            report = evaluate(selected, model=model, repeats=args.repeat)
            report['model'] = args.model
            json.dump(report, output, indent=2)
            output.write('\n')
    return int(not report['passed'])


@contextmanager
def context_stdout():
    yield sys.stdout


if __name__ == '__main__':
    raise SystemExit(main())
