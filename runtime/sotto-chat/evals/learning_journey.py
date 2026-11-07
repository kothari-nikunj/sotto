"""Synthetic learning failure/recovery inside the existing isolated conversation peer.

Only the Gmail client and model HTTP boundary are fixtures. The scheduler, source
adapter, extraction, local processors, curation and canonical writer are production code.
"""
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.error import HTTPError
import urllib.request
import urllib.parse


def _require(condition, message):
    # Unlike assert, fixture safety and acceptance checks survive Python optimization.
    if not condition:
        raise AssertionError(message)


def _select_pack_paths(pack):
    # Preserve the explicit pack precedence while promoting and deduplicating its paths.
    desired = [str(pack / 'relationship-pulse/scripts'),
               *(str(pack / '_shared' / folder) for folder in ('lib', 'scripts', 'knowledge'))]
    sys.path[:] = desired + [directory for directory in sys.path if directory not in desired]


def run(stage, world, data):
    if stage not in ('rejected', 'resumed', 'replayed'):
        raise ValueError('Unknown learning journey stage')
    config = world.get('learningJourney')
    if not isinstance(config, dict) or 'gmail' not in world.get('savedSourceGrants', []):
        raise ValueError('Learning journey requires explicit synthetic Gmail source consent')
    email, name, fact = (config.get(key) for key in ('email', 'name', 'fact'))
    if not all(isinstance(value, str) and value for value in (email, name, fact)):
        raise ValueError('Learning journey requires email, name and durable fact')
    root = Path(__file__).resolve().parents[3]
    pack = root / 'sotto-chief-of-staff'
    _select_pack_paths(pack)
    import gather_google  # noqa: PLC0415 — load only after the explicit eval pack path is selected
    import history_coverage  # noqa: PLC0415
    import knowledge  # noqa: PLC0415
    import memory_cycle  # noqa: PLC0415
    import model_work  # noqa: PLC0415

    data = Path(data)
    # Harness chronology belongs beside the isolated receiver volume in the run
    # output directory; it is not a shared production SOTTO_DATA store.
    receipt_path = data.with_name(data.name + '-learning-journey.json')
    if stage == 'rejected':
        if receipt_path.exists():
            raise ValueError('Learning journey was already initialized')
        saved = {'at': datetime.now(timezone.utc).isoformat(), 'stages': []}
    else:
        saved = json.loads(receipt_path.read_text())
    expected = {'rejected': [], 'resumed': ['rejected'], 'replayed': ['rejected', 'resumed']}
    if saved['stages'] != expected[stage]:
        raise ValueError('Learning journey must run chronologically')
    now = datetime.fromisoformat(saved['at']) + timedelta(hours=len(saved['stages']))
    message_id = 'learning-fresh-source'
    native = {'id': message_id, 'threadId': 'learning-fresh-thread', 'labelIds': ['INBOX'],
              'internalDate': str(int(datetime.fromisoformat(saved['at']).timestamp() * 1000)),
              'payload': {'mimeType': 'text/plain', 'headers': [
                  {'name': 'From', 'value': f'{name} <{email}>'},
                  {'name': 'To', 'value': 'owner@example.com'},
                  {'name': 'Subject', 'value': 'What I build'}],
                  'body': {'data': base64.urlsafe_b64encode(fact.encode()).decode()}}}
    calls, reads, unexpected = [], [], []

    def deny(kind):
        # Production intentionally catches source/provider failures. Keep a sticky failure
        # outside those catches so an unknown boundary cannot become an empty result.
        unexpected.append(kind)
        raise AssertionError('Unexpected learning fixture boundary: ' + kind)

    class Result:
        def __init__(self, value): self.value = value
        def execute(self): return self.value

    class Gmail:
        def users(self): return self
        def messages(self): return self
        def close(self): pass
        def list(self, **kwargs):
            reads.append({'operation': 'list', 'cursor': kwargs.get('pageToken')})
            if kwargs.get('pageToken') not in (None, 'learning-next'):
                deny('gmail_continuation')
            return Result({'messages': [{'id': message_id}],
                           **({} if kwargs.get('pageToken') else {'nextPageToken': 'learning-next'})})
        def get(self, **kwargs):
            if kwargs != {'userId': 'me', 'id': message_id, 'format': 'full'}:
                deny('gmail_get')
            reads.append({'operation': 'get', 'id': message_id})
            return Result(native)

    def provider(request, *args, **kwargs):
        parsed = urllib.parse.urlsplit(getattr(request, 'full_url', str(request)))
        if (parsed.hostname != 'generativelanguage.googleapis.com'
                or parsed.path != '/v1beta/models/gemini-3.8-flash:generateContent'):
            deny('model_endpoint')
        body = json.loads(request.data)
        task = (model_work.current() or {}).get('task')
        calls.append(task)
        records = json.loads(body['contents'][0]['parts'][0]['text'])
        if task == 'memory_extract':
            if (len(records) != 1 or records[0]['ref'] != 'gmail:' + message_id
                    or records[0]['text'] != fact):
                deny('model_evidence')
            if body['generationConfig'].get('thinkingConfig', {}).get('thinkingLevel') == 'unsupported-fixture':
                raise HTTPError(request.full_url, 400, 'Bad Request', {}, io.BytesIO(json.dumps({
                    'error': {'status': 'INVALID_ARGUMENT',
                              'message': 'Thinking level unsupported-fixture is not supported'}}).encode()))
            result = {'people': [{'subject_id': records[0]['subject_id'],
                                 'facts': [{'text': fact, 'refs': [records[0]['ref']]}]}]}
        elif task == 'memory_curate':
            result = {'people': [{'id': row['id'], 'summary_refs': list(row['facts'])[:4],
                                  'conflicts': [], 'equivalent_groups': []} for row in records]}
        else:
            deny('model_operation')
        return io.BytesIO(json.dumps({'candidates': [{'finishReason': 'STOP',
            'content': {'parts': [{'text': json.dumps(result)}]}}],
            'usageMetadata': {'promptTokenCount': 0, 'candidatesTokenCount': 0}}).encode())

    baseline = model_work.POLICIES['memory_extract']
    policy = replace(baseline, thinking='unsupported-fixture') if stage == 'rejected' else baseline
    # Explicit offline self-host fixture policy. It cannot confer a grant on receiver chat;
    # the saved world/receiver capability still owns that independent authority boundary.
    env = {'SOTTO_DATA': str(data), 'SOTTO_DEPLOYMENT_MODE': 'self-host',
           'SOTTO_BACKGROUND_UNMETERED': 'true', 'SOTTO_BRIEF_MODEL': 'gemini/gemini-3.8-flash',
           'SOTTO_PROACTIVE_CAPTURED_SOURCES': '["gmail"]', 'GOOGLE_AI_API_KEY': 'fixture-only'}
    # One page per stage preserves the resume checkpoint before duplicate replay.
    with patch.dict(os.environ, env), patch.dict(model_work.POLICIES, {'memory_extract': policy}), \
            patch.object(memory_cycle, 'PAGES_PER_RUN', 1), \
            patch.object(gather_google, '_gmail_service', Gmail), \
            patch.object(urllib.request, 'urlopen', provider):
        receipt = memory_cycle.run(now)
        if stage == 'rejected':
            # An unchanged retry performs no fetch or paid attempt, even after backoff.
            memory_cycle.run(now + timedelta(hours=1))
        _require(not unexpected, 'Unexpected learning fixture boundaries: ' + ', '.join(unexpected))
        state = json.loads(Path(memory_cycle.state_path()).read_text())['sources']['gmail']
        coverage = history_coverage.learning_coverage(data, ['gmail'])['sources']['gmail']
        path = knowledge.find_person_file(identifier=email)
        person = knowledge.parse_person_file(Path(path).read_text()) if path else None
        facts = [row for row in person.facts.values() if row.text == fact] if person else []
        if stage == 'rejected':
            _require(state['cursor'] is None and state['pages'] == state['reviewed'] == 0,
                     'Rejected learning advanced its checkpoint')
            _require(state['error_code'] == 'provider_request_rejected', 'Rejection was not recorded')
            _require(coverage['latest_attempt']['category'] == 'unsupported_model_settings',
                     'Rejection lost its settings category')
            _require(coverage['last_read_success_at'] and coverage['last_extraction_success_at'] is None,
                     'Rejected extraction has incorrect success coverage')
            _require(not facts and calls == ['memory_extract'] and len(reads) == 2,
                     'Rejected request learned facts or retried unchanged work')
        else:
            _require(state['pages'] == (1 if stage == 'resumed' else 2), 'Incorrect recovered page count')
            _require(state['cursor'] == ('learning-next' if stage == 'resumed' else None),
                     'Incorrect recovered cursor')
            _require('rejected_request_revision' not in state and 'error_code' not in state,
                     'Recovered learning retained its rejection')
            _require(coverage['latest_attempt']['status'] == 'succeeded', 'Recovered attempt did not succeed')
            _require(len(facts) == 1 and facts[0].evidence_refs == ['gmail:' + message_id],
                     'Recovered fact is missing, duplicated or incorrectly sourced')
            _require(calls.count('memory_extract') == 1, 'Recovered page repeated extraction')
    saved['stages'].append(stage)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(saved))
    return {'stage': stage, 'fact': fact, 'source_ref': 'gmail:' + message_id,
            'model_calls': calls, 'source_reads': reads, 'coverage': coverage, 'receipt': receipt,
            'facts': len(facts), 'pages': state['pages'], 'reviewed': state['reviewed'],
            'unexpected_calls': unexpected}
