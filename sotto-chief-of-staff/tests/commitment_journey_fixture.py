"""Reusable synthetic chronological journey through the production ledger functions.

The completion verdict is an explicit offline critic acceptance, not a model-quality
claim. No providers or transport are called. Callers may use the resulting data root
as the canonical continuity input to a separate conversation replay.
"""
from datetime import datetime, timezone
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

import continuity_resolve as cr
import knowledge as kg
from delivery_effects import loop_version
from ledger_provenance import digest_frontmatter

CAPTURE_AT = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)
CLOSE_AT = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)
IDENTIFIER = 'maya.journey@example.test'
PHONE = '+14155550193'
PERSON_ID = 'c_abc193'
PROMISE = {
    'id': 'journey-promise-mail', 'threadId': 'journey-email-thread',
    'from': 'owner@example.test', 'to': IDENTIFIER, 'isSent': True,
    'date': '2026-10-18T09:00:00Z',
    'body': 'I will send the pricing document tomorrow. I will also send the launch checklist tomorrow.',
}
DELIVERY = {
    'id': 'journey-pricing-text', 'handle': PHONE, 'is_from_me': True,
    'timestamp': '2026-10-19T08:00:00Z',
    'text': 'Here is the pricing document: https://example.test/pricing-final. The launch checklist is still pending.',
}


def native_promise(message=None):
    """Exact frozen original Gmail transport row; no later source evidence is added."""
    import base64
    message = PROMISE if message is None else message
    return {'id': message['id'], 'threadId': message['threadId'], 'labelIds': ['SENT'],
        'internalDate': str(int(datetime.fromisoformat(message['date'].replace('Z', '+00:00')).timestamp() * 1000)),
        'payload': {'mimeType': 'text/plain', 'headers': [{'name': key, 'value': value}
            for key, value in [('From', message['from']), ('To', message['to']),
                               ('Date', message['date']), ('Subject', 'Original promises')]],
            'body': {'data': base64.urlsafe_b64encode(message['body'].encode()).decode()}}}


def capture_payload():
    asks = [('Send the pricing document', 'I will send the pricing document tomorrow.'),
            ('Send the launch checklist', 'I will also send the launch checklist tomorrow.')]
    return {'today': '2026-10-18', 'local': {'emails': [dict(PROMISE)]},
            'new_actions': [
                {'type': 'commitment', 'channel': 'email', 'contactName': 'Maya Journey',
                 'contactIdentifier': IDENTIFIER, 'contextSummary': summary,
                 'contextDeadline': '2026-10-19', 'emailThreadId': PROMISE['threadId'],
                 'emailMessageId': PROMISE['id'], 'newObligation': True,
                 'evidence': [{'sourceType': 'email', 'sourceId': PROMISE['id'], 'snippet': quote}]}
                for summary, quote in asks]}


def reviewed_proposal(row, message=None):
    message = message or DELIVERY
    update = {'loopId': row['anchor_key'], 'loopVersion': loop_version(row), 'status': 'resolved',
              'reason': 'Pricing document supplied in a later text; checklist remains outstanding.',
              'evidence': [{'sourceType': 'imessage', 'sourceId': message['id'],
                            'snippet': message['text']}]}
    update['criticReceipt'] = cr.record_critic_approval(update)
    return update


def seed_capture(data_root, *, payload=None, capture_at=CAPTURE_AT):
    """Caller sets SOTTO_DATA to an empty synthetic directory before using this helper."""
    assert Path(os.environ['SOTTO_DATA']).resolve() == Path(data_root).resolve()
    people = Path(kg.people_dir())
    people.mkdir(parents=True, exist_ok=True)
    kg.write_person_file(str(people / (PERSON_ID + '.md')), kg.PersonFile(
        name='Maya Journey', canonical_id=PERSON_ID, identifiers=[IDENTIFIER, PHONE]))
    payload = capture_payload() if payload is None else payload
    result = cr.resolve(payload, capture_at)
    assert len(result['active']) == 2
    return payload, {row['summary']: row for row in cr._load_items().values()}


def run_journey(data_root, *, stamped=False):
    """Write only synthetic state under data_root; return replay inputs and actual rows."""
    with patch.dict(os.environ, {'SOTTO_DATA': str(data_root), 'SOTTO_TIMEZONE': 'UTC',
                                 'SOTTO_DEPLOYMENT_MODE': 'self-hosted'}):
        capture, rows = seed_capture(data_root)
        activity = {'id': 'journey-noncompletion-text', 'handle': PHONE, 'is_from_me': True,
                    'timestamp': '2026-10-18T11:00:00Z', 'text': 'I will follow up shortly.'}
        before = cr.resolve({'today': '2026-10-18', 'local': {'imessage': [activity]}},
                            datetime(2026, 10, 18, 12, tzinfo=timezone.utc))
        assert len(before['active']) == 2 and not before['resolved']
        row = cr._load_items()[rows['Send the pricing document']['anchor_key']]
        if stamped:
            row['content_provenance'] = {'version': 1, 'kind': 'sources', 'sources': ['gmail'],
                                         'digest': digest_frontmatter(row)}
            cr._persist(row)
        update = reviewed_proposal(row)
        result = cr.resolve({'today': '2026-10-19', 'local': {'imessage': [dict(DELIVERY)]},
                             'loop_updates': [update]}, CLOSE_AT)
        assert len(result['resolved']) == 1 and len(result['active']) == 1
        return {'capture_payload': capture, 'completion_local': {'imessage': [dict(DELIVERY)]},
                'completion_proposal': update, 'result': result,
                'rows': list(cr._load_items().values()), 'data_root': str(data_root),
                'review_basis': 'offline critic acceptance; semantic model quality is not evaluated'}


CHRONOLOGICAL_SCENARIOS = (
    'email-to-text', 'siblings', 'partial', 'acknowledgment', 'noncompletion',
    'ambiguous-group', 'missing-access', 'partial-access', 'revoked-access',
    'restart', 'new-request', 'later-explanation',
)


def _golden_runner():
    """Reuse the existing replay, including its production composer/critic/writer path."""
    name = '_commitment_journey_golden'
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / 'evals/run_golden.py'
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def run_chronological_stage(data_root, stage, scenario='email-to-text', *, actual_model=False):
    """Incremental synthetic fixture for Golden and the existing conversation receiver.

    Only this stage's source rows are revealed. Model judgments default to scripted;
    explicit actual mode uses the peer's existing localhost relay with Node-owned credentials.
    No saved final ledger, direct approval receipt, or second evidence store is used.
    Each call can run in a fresh process against the same isolated data directory.
    """
    import compose_brief
    import gather_google
    import loop_context
    import loops_query
    import proactive_scan
    import source_context

    if scenario not in CHRONOLOGICAL_SCENARIOS:
        raise ValueError('unknown chronological commitment scenario')
    if stage not in ('request', 'fulfillment', 'resolve', 'restart', 'new-request'):
        raise ValueError('unknown chronological commitment stage')
    started = time.monotonic()
    root = Path(data_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    clock = CAPTURE_AT if stage == 'request' else CLOSE_AT
    if stage in ('restart', 'new-request'):
        clock = datetime(2026, 10, 20, 10, tzinfo=timezone.utc)
    grants = ['gmail', 'imessage']
    if stage != 'request' and scenario == 'revoked-access':
        grants = ['gmail']
    env = {'SOTTO_DATA': str(root), 'SOTTO_TIMEZONE': 'UTC',
           'SOTTO_DEPLOYMENT_MODE': 'managed' if actual_model else 'self-hosted', 'SOTTO_CRITIC': 'always',
           'SOTTO_PROACTIVE_CAPTURED_SOURCES': json.dumps(grants)}
    if actual_model:
        import socket
        from urllib.parse import urlparse
        relay = urlparse(json.loads(os.environ.get('SOTTO_EVAL_NATIVE_PREP_RELAY', '{}')).get('url', ''))
        if (relay.scheme != 'http' or relay.hostname != '127.0.0.1' or not relay.port or relay.path != '/compose'
                or relay.username is not None or relay.password is not None or relay.query or relay.fragment):
            raise ValueError('Actual commitment mode requires the existing exact localhost relay')
        env.update(SOTTO_MODEL_PROXY_URL='https://eval-source.invalid',
                   SOTTO_MODEL_PROXY_TOKEN='<synthetic-token>', SOTTO_BRIEF_MODEL='gemini-3.8-flash',
                   SOTTO_GEMINI_MODEL='gemini-3.8-flash', SOTTO_FALLBACK_MODEL='')
        original_connect = socket.socket.connect
    model_calls, source_reads, unexpected_calls = [], [], []
    def unexpected_external(*args, **kwargs):
        unexpected_calls.append('unexpected external operation')
        raise AssertionError('unexpected external operation')
    def connect(sock, address):
        if actual_model and address == ('127.0.0.1', relay.port):
            return original_connect(sock, address)
        return unexpected_external()
    with (patch.dict(os.environ, env),
          patch('socket.socket.connect', new=connect),
          patch.object(source_context, 'bridge_call', side_effect=unexpected_external)):
        rg = _golden_runner()
        # Golden owns the loaded canonical modules; imports may have been refreshed by
        # another eval during collection, so retain its writer for this entire stage.
        writer = rg.cr
        actual_call = rg.gemini_call
        identity_file = root / 'commitment-journey-original-identities.json'
        original_tasks = json.loads(identity_file.read_text()) if actual_model and stage != 'request' else {}
        def task_text(row):
            return row.get('ask') or row.get('summary') or ''
        def original_task(kind):
            if actual_model:
                return writer._load_items()[original_tasks[kind]['anchor_key']]
            return next(row for row in writer._load_items().values()
                        if row['summary'] == {'pricing': 'Send the pricing document',
                                              'checklist': 'Send the launch checklist'}[kind])
        if stage == 'request':
            if writer._load_items():
                raise AssertionError('request must start with an empty isolated ledger')
            people = Path(kg.people_dir())
            people.mkdir(parents=True, exist_ok=True)
            kg.write_person_file(str(people / (PERSON_ID + '.md')), kg.PersonFile(
                name='Maya Journey', canonical_id=PERSON_ID, identifiers=[IDENTIFIER, PHONE]))
        elif len(writer._load_items()) < 2:
            raise AssertionError('reveal the original requests before subsequent evidence')
        before = deepcopy(writer._load_items())

        payload = capture_payload()
        actions = payload['new_actions'] if stage == 'request' else []
        emails = [deepcopy(PROMISE)] if stage == 'request' else []
        local, updates = {}, []
        message = deepcopy(DELIVERY)
        rejected_meaning = scenario in ('partial', 'acknowledgment', 'noncompletion')
        if scenario == 'partial':
            message['text'] = 'Only the first pricing table is ready; the document is incomplete.'
        elif scenario == 'acknowledgment':
            message['text'] = 'Thanks, Maya. I enjoyed our unrelated lunch yesterday.'
        elif scenario == 'noncompletion':
            message['text'] = 'I have not sent the pricing document yet.'
        elif scenario == 'ambiguous-group':
            message.update(is_group_chat=True, chat_guid='iMessage;+;unrelated-group',
                           group_participants=[PHONE, '+14155550999'])
        gather_receipt = None
        if stage in ('fulfillment', 'resolve'):
            class NoMail:
                def users(self): return self
                def threads(self): self.collection = 'threads'; return self
                def messages(self): self.collection = 'messages'; return self
                def list(self, **kwargs):
                    expected = {'userId': 'me', 'q': f'{{from:"{IDENTIFIER}" to:"{IDENTIFIER}" cc:"{IDENTIFIER}"}} newer_than:30d -in:trash -in:spam', 'maxResults': 2}
                    if not actual_model or self.collection != 'messages' or kwargs != expected:
                        return unexpected_external()
                    source_reads.append({'source': 'gmail', 'operation': 'exact_counterpart_inventory'})
                    self.response = {'messages': [{'id': PROMISE['id'], 'threadId': PROMISE['threadId']}]}
                    return self
                def get(self, **kwargs):
                    identifier = PROMISE['threadId'] if self.collection == 'threads' else PROMISE['id']
                    if (not actual_model and self.collection != 'threads'
                            or kwargs != {'userId': 'me', 'id': identifier, 'format': 'full'}):
                        unexpected_calls.append('unexpected Gmail read')
                        raise AssertionError('unexpected Gmail read')
                    source_reads.append({'source': 'gmail', 'collection': self.collection, 'id': identifier})
                    self.response = ({'messages': [native_promise()] if actual_model else []}
                                     if self.collection == 'threads' else native_promise())
                    return self
                def execute(self): return self.response
                def __getattr__(self, name):
                    unexpected_calls.append('unexpected Gmail operation')
                    raise AssertionError('unexpected Gmail operation')

            def read_messages(identifier, *, timeout):
                if identifier != PHONE or not 0 < timeout <= loop_context.READ_BUDGET_SECONDS:
                    unexpected_calls.append('unexpected native source read')
                    raise AssertionError('unexpected native source read')
                source_reads.append({'source': 'imessage', 'identifier': identifier})
                if scenario == 'missing-access':
                    raise ConnectionError('synthetic unavailable source')
                return {'identifier': identifier,
                        'messages': [] if scenario == 'partial-access' else [deepcopy(message)]}

            gather_started = time.monotonic()
            context, gather_receipt = loop_context.refresh_context(
                list(writer._load_items().values()), {}, NoMail(), gather_google.normalize_email,
                now=clock, allowed=source_context.allowed, read_messages=read_messages)
            gather_receipt['elapsed_ms'] = round((time.monotonic() - gather_started) * 1000, 3)
            local = {'imessage': context['imessage']}
            emails = context['emails']
            row = original_task('pricing')
            # Even an optimistic model proposal cannot bypass missing evidence,
            # wrong group identity, chronology, consent, or critic denial.
            updates = [{'loopId': row['anchor_key'], 'loopVersion': loop_version(row),
                        'status': 'resolved', 'reason': 'Synthetic task-specific source review.',
                        'evidence': [{'sourceType': 'imessage', 'sourceId': message['id'],
                                      'snippet': message['text']}]}]
        if stage == 'new-request':
            new_mail = {**PROMISE, 'id': 'journey-new-pricing-request',
                        'date': '2026-10-20T09:00:00Z',
                        'body': 'I will send the revised pricing document tomorrow.'}
            action = deepcopy(payload['new_actions'][0])
            action.update(emailMessageId=new_mail['id'], contextSummary='Send revised pricing document',
                          contextDeadline='2026-10-21', evidence=[{'sourceType': 'email',
                          'sourceId': new_mail['id'], 'snippet': new_mail['body']}])
            emails, actions = [new_mail], [action]

        def model(prompt, inputs, **kwargs):
            kind = 'critic' if inputs.get('_critic') else 'revision' if inputs.get('_revise') else 'extract'
            call = {'kind': kind, 'input_chars': len(prompt),
                    'has_closing_source': message['id'] in prompt and message['text'] in prompt}
            model_calls.append(call)
            if stage == 'request' and (DELIVERY['id'] in prompt or DELIVERY['text'] in prompt):
                raise AssertionError('future fulfillment leaked into original request replay')
            if actual_model:
                output = actual_call(prompt, inputs, **kwargs)
                call['output_chars'] = len(output)
                try:
                    call['parsed_output'] = json.loads(output)
                except ValueError:
                    call['unparsed_output'] = output
                # Preserve real semantic decisions even if a later invariant fails.
                (root / f'commitment-model-{stage}.json').write_text(json.dumps(model_calls))
                return output
            if kind == 'critic':
                output = json.dumps({'patches': [], 'score': 95,
                    'summary': 'Scripted semantic judgment; not actual-model evidence.',
                    'loop_verdicts': [{'proposal_id': writer.critic_review_receipt(update),
                                       'verdict': 'reject' if rejected_meaning else 'approve'}
                                      for update in updates]})
            elif kind != 'extract':
                raise AssertionError('unexpected extra model step in synthetic journey')
            else:
                output = json.dumps({'markdown': '## Updates\n\nSynthetic source review.',
                                     'actionItems': actions, 'loopUpdates': updates})
            call['output_chars'] = len(output)
            return output

        replay = None
        if stage not in ('fulfillment', 'restart'):
            day = {'name': f'{scenario}:{stage}', 'inputs': {'type': 'morning', 'local': local,
                   'google': {'emails': emails, 'events': [], 'userEmail': 'owner@example.test'}}}
            with patch.object(rg, 'gemini_call', side_effect=model):
                replay = rg.replay_day(day, clock, str(root), live=True)
            if replay['exception']:
                raise AssertionError(replay['exception'])
        if unexpected_calls:
            raise AssertionError('; '.join(unexpected_calls))
        rows = list(writer._load_items().values())
        if actual_model and stage == 'request':
            for kind in ('pricing', 'checklist'):
                matches = [row for row in rows if kind in task_text(row).lower()]
                if len(matches) != 1 or not any(ref.get('sourceId') == PROMISE['id']
                        for ref in matches[0].get('source_refs', [])):
                    raise AssertionError('original task/source identity not uniquely captured')
                original_tasks[kind] = matches[0]
            if original_tasks['pricing']['anchor_key'] == original_tasks['checklist']['anchor_key']:
                raise AssertionError('original distinct tasks merged')
            identity_file.write_text(json.dumps(original_tasks))
        if stage == 'request' and any(row.get('canonical_id') != PERSON_ID
                or row.get('contact_identifier') not in (IDENTIFIER, PHONE) for row in rows):
            raise AssertionError('original tasks captured against the wrong counterpart')
        if actual_model:
            by_key = {row['anchor_key']: row for row in rows}
            for original in original_tasks.values():
                current = by_key.get(original['anchor_key'])
                if current is None or any(current.get(key) != original.get(key) for key in
                        ('ask', 'summary', 'canonical_id', 'contact_identifier', 'source_refs')):
                    raise AssertionError('original task identity/evidence changed')
        for kind in ('pricing', 'checklist'):
            if original_task(kind).get('deadline') != '2026-10-19':
                raise AssertionError('original obligation deadline missing or changed from dated source')
        eligible = [row['anchor_key'] for row in compose_brief._protected_action_ledger({'now': clock.isoformat()})]
        proactive = [row['anchor_key'] for row in proactive_scan._open_loops()]
        history = loops_query.topic_history(str(root), grants, topic='pricing', now=clock)
        held = scenario in ('partial', 'acknowledgment', 'noncompletion', 'ambiguous-group',
                            'missing-access', 'partial-access', 'revoked-access')
        expected_closed = stage not in ('request', 'fulfillment') and not held
        closed = [row for row in rows if row['status'] == 'resolved']
        if len(closed) != int(expected_closed) or len(rows) != (3 if stage == 'new-request' else 2):
            raise AssertionError('chronological scenario persisted an unexpected obligation state')
        pricing = original_task('pricing')
        if pricing['status'] != ('resolved' if expected_closed else 'open'):
            raise AssertionError('pricing task has an unexpected exact status')
        if not expected_closed and pricing['anchor_key'] not in eligible:
            raise AssertionError('unfinished pricing task lost brief eligibility')
        if stage == 'new-request':
            old_keys = {original_task(kind)['anchor_key'] for kind in ('pricing', 'checklist')}
            fresh = [row for row in rows if row['anchor_key'] not in old_keys]
            if (len(fresh) != 1 or fresh[0]['status'] != 'open'
                    or fresh[0].get('canonical_id') != PERSON_ID
                    or fresh[0]['anchor_key'] not in eligible
                    or not any(ref.get('sourceId') == 'journey-new-pricing-request'
                               for ref in fresh[0].get('source_refs', []))):
                raise AssertionError('new request lost distinct identity, source or open eligibility')
            if fresh[0].get('deadline') != '2026-10-21':
                raise AssertionError('new request deadline missing or resolved against the wrong source date')
        if stage == 'resolve' and scenario in ('partial', 'acknowledgment', 'noncompletion'):
            if (not any(call['kind'] == 'extract' and call['has_closing_source'] for call in model_calls)
                    or not any(read.get('source') == 'imessage' and read.get('identifier') == PHONE
                               for read in source_reads)):
                raise AssertionError('held outcome did not review the exact closing source')
        sibling = original_task('checklist')
        if sibling['status'] != 'open':
            raise AssertionError('reconciliation changed the unrelated sibling')
        if stage == 'fulfillment' and writer._load_items() != before:
            raise AssertionError('evidence collection changed the canonical ledger')
        if closed:
            completed, = closed
            if ((actual_model and completed['anchor_key'] != original_tasks['pricing']['anchor_key'])
                    or (not actual_model and completed['summary'] != 'Send the pricing document')
                    or not any(ref.get('sourceId') == PROMISE['id'] for ref in completed['source_refs'])
                    or not any(ref.get('sourceId') == DELIVERY['id'] for ref in completed['resolution_source_refs'])
                    or completed['anchor_key'] in eligible or completed['anchor_key'] in proactive):
                raise AssertionError('closure lost evidence or remains eligible for a chase')
        return {'scenario': scenario, 'stage': stage, 'state': rows,
                'brief_eligible': eligible, 'reminder_eligible': proactive,
                'history': history, 'evidence': {'emails': emails, **local},
                'source_reads': source_reads, 'gather_receipt': gather_receipt,
                'model_calls': model_calls, 'total_ms': round((time.monotonic() - started) * 1000, 3),
                'cost_usd': None, 'actual_model': actual_model,
                'review_basis': ('synthetic sources and actual external model via existing localhost relay'
                    if actual_model else 'synthetic sources and scripted external model; production composer, critic and writer'),
                'original_source': PROMISE['id'], 'closing_source': DELIVERY['id']}
