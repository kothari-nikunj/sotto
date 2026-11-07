"""Scoped owner admission uses real generic authority and existing durable WorkQueue."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import runtime_owner_brief_request as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, WHEN, THREAD, capability
from test_requested_brief import rec

__all__ = ['context', 'rec']
ARGS = {'kind': 'auto', 'refresh': False, 'remainder': 'Tell me a joke.', 'provider_message_id': 'actual-provider-1'}
TEXT = 'Show my brief. Tell me a joke.'


def request(args=None, event='d' * 64, operation='admit'):
    args = ARGS if args is None else args
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([event, 'owner_brief_request', canonical],
                                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return dict(version=1, operation=operation, identity=IDENTITY, action_id=action, event_id=event,
        payload_hash='b' * 64, text_hash=hashlib.sha256(TEXT.encode()).hexdigest(), text=TEXT,
        admitted_at_ms=int(WHEN.timestamp() * 1000), provider_thread_id=THREAD, args=args)


def call(body, context, rec, monkeypatch):
    data, policy, activation = context
    policy.update(SOTTO_DATA=str(data), SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1')
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', IDENTITY['owner_id'])
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant')
    config = data / 'config/photon-activation.json'
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({'activated': True, 'owner': IDENTITY['owner_id'],
                                 'tenant_id': 'tenant', 'chat_id': 'iMessage;-;owner'}))
    return tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT, policy=policy,
        data_root=data, capability=capability, activation=activation, queue=rec.WORK_QUEUE,
        admit=rec.request_owner_brief)


def test_atomic_receipt_inspect_changed_args_terminal_and_zone(context, rec, monkeypatch):
    body = request()
    assert call(dict(body, operation='inspect'), context, rec, monkeypatch)['status'] == 'not_found'
    receipt = call(body, context, rec, monkeypatch)
    assert receipt['status'] == 'committed' and receipt['outcome']['kind'] == 'evening'
    jid = receipt['outcome']['request_id']
    job = rec.WORK_QUEUE.get(rec.DATA, jid)
    assert 'Tell me a joke.' not in job['admission'] and TEXT not in job['admission']
    assert call(body, context, rec, monkeypatch) == receipt
    for args in ({**ARGS, 'refresh': True}, {**ARGS, 'provider_message_id': 'different'}):
        assert call(request(args), context, rec, monkeypatch)['code'] == 'event_conflict'
    rec.WORK_QUEUE.clear_feedback(rec.DATA, jid)
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='done',payload='{}',result=NULL WHERE id=?", (jid,))
    context[1]['_configured_tz_name'] = lambda: 'Pacific/Honolulu'
    context[1]['_tz_resolve'] = ZoneInfo
    assert call(dict(body, operation='inspect'), context, rec, monkeypatch) == receipt
    assert call(body, context, rec, monkeypatch) == receipt


def test_original_proof_conflict_and_untrusted_remainder(context, rec, monkeypatch):
    receipt = call(request(), context, rec, monkeypatch)
    body = request()
    body['payload_hash'] = 'f' * 64
    assert call(body, context, rec, monkeypatch)['code'] == 'event_conflict'
    assert receipt['outcome']['request_id']
    with pytest.raises(ValueError):
        call(request({**ARGS, 'remainder': 'Invented remainder'}), context, rec, monkeypatch)
    body = request()
    body['text_hash'] = 'e' * 64
    with pytest.raises(ValueError):
        call(body, context, rec, monkeypatch)


def test_legacy_job_without_proof_is_never_upgraded(context, rec, monkeypatch):
    data, _, _ = context
    from requested_brief import identity
    jid = rec.WORK_QUEUE.enqueue(data, 'run', {}, key='requested:' + identity(
        IDENTITY['owner_id'], 'iMessage;-;owner', ARGS['provider_message_id']))
    assert call(request(), context, rec, monkeypatch)['code'] == 'event_conflict'
    assert rec.WORK_QUEUE.get(data, jid)['admission'] is None


def test_matching_admission_race_returns_original_receipt(context, rec, monkeypatch):
    original = rec.request_owner_brief
    def raced(body, **kwargs):
        first = original(body, **kwargs)
        assert first[1]['status'] == 'queued'
        return original(body, **kwargs)
    monkeypatch.setattr(rec, 'request_owner_brief', raced)
    receipt = call(request(), context, rec, monkeypatch)
    assert receipt['status'] == 'committed'
    assert rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])['admission']


@pytest.mark.parametrize('text', ['thanks', 'ok', 'what time is my dentist?', 'Do not send my brief', 'She said send my brief', '"send my brief"',
    'do not send me an updated brief', '"send me an updated brief"',
    'send me an updated brief and then stop', "send me an updated brief. Actually, don't.",
    'Give me a fresh evening brief, but don’t send it.',
    'Give me a fresh evening brief, including meeting prep, actually cancel that.',
    'The email says, give me a fresh evening brief, including meeting prep.',
    '"Give me a fresh evening brief, including meeting prep."',
    'Give me my evening brief, actually make it the morning one.',
    'Send my brief, she said.',
    'Send my morning brief. Focus on Alex. Actually, cancel that.',
    'Send my morning brief. Send my evening brief.',
    'Make me a fresh brief, for Alex.',
    "Give me a fresh evening brief, including tasks and I don't want it sent."])
def test_unrequested_brief_cannot_enqueue(context, rec, monkeypatch, text):
    body = request({**ARGS, 'remainder': None})
    body.update(text=text, text_hash=hashlib.sha256(text.encode()).hexdigest())
    assert call(body, context, rec, monkeypatch)['code'] == 'needs_context'
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, body['event_id']) is None


INTENT_VECTORS = Path(__file__).resolve().parent.parent / 'sotto-chat/test/fixtures/owner-brief-intent-vectors.json'


def test_owner_brief_intent_matches_shared_node_vectors():
    if not INTENT_VECTORS.is_file():
        pytest.skip('shared owner brief intent vectors are not present')
    for vector in json.loads(INTENT_VECTORS.read_text(encoding='utf-8')):
        assert tool.owner_brief_intent(vector['text']) == vector['intent'], vector['text']


@pytest.mark.parametrize('text,kind,refresh', [
    ('send me an updated brief', 'auto', True),
    ('send me a fresh brief', 'auto', True),
    ('send me an evening brief', 'evening', False),
    ('Give me a fresh evening brief, including anything worth preparing for over the next few days. '
     'Keep the help offer focused on meeting prep this time.', 'evening', True),
    ('Please prepare my morning brief, focusing on tomorrow’s meetings.', 'morning', False),
])
def test_normal_article_request_commits_once(context, rec, monkeypatch, text, kind, refresh):
    body = request({**ARGS, 'kind': kind, 'refresh': refresh, 'remainder': None})
    body.update(text=text, text_hash=hashlib.sha256(text.encode()).hexdigest())
    receipt = call(body, context, rec, monkeypatch)
    assert receipt['status'] == 'committed'
    assert receipt['outcome']['kind'] == ('evening' if kind == 'auto' else kind)
    assert receipt['outcome']['refresh'] == refresh
    assert call(body, context, rec, monkeypatch) == receipt
    assert call(dict(body, operation='inspect'), context, rec, monkeypatch) == receipt
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, body['event_id']) is not None


@pytest.mark.parametrize('direction', [
    "focusing on tomorrow’s meetings and the open pricing decision.",
    "including useful prep for customer calls. Keep the evidence specific.",
    "focusing on " + "pricing context " * 200 + "for the call.",
])
def test_verified_composition_direction_survives_durable_request(context, rec, monkeypatch, direction):
    text = "Please prepare my evening brief, " + direction
    body = request({**ARGS, 'kind': 'evening', 'remainder': None})
    body.update(text=text, text_hash=hashlib.sha256(text.encode()).hexdigest())
    receipt = call(body, context, rec, monkeypatch)
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    procedure = json.loads(job['payload']['prompt'])
    assert procedure['composition_direction'] == direction
    assert not procedure['refresh']
    assert call(body, context, rec, monkeypatch) == receipt


@pytest.mark.parametrize(('text', 'directed'), [
    ('Send my brief, please', False),
    ("Send my brief, and tell me what's on today", True),
    ('Prepare my brief, and draft a reply to Bob', True),
    ('Send my morning brief. Focus on tomorrow morning’s call.', True),
    ('Send my brief. Thanks again!', True),
    ('Send my morning brief. Make it the evening one.', True),
    ('Send my brief. Make it a fresh one.', True),
])
def test_companion_clauses_are_composition_context_but_courtesy_is_not(context, rec, monkeypatch, text, directed):
    expected_kind = 'morning' if 'morning brief' in text else 'evening'
    body = request({**ARGS, 'kind': expected_kind, 'remainder': None})
    body.update(text=text, text_hash=hashlib.sha256(text.encode()).hexdigest())
    receipt = call(body, context, rec, monkeypatch)
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    procedure = json.loads(job['payload']['prompt'])
    assert procedure.get('composition_direction') == (text if directed else None)
    assert receipt['outcome']['kind'] == expected_kind
    assert not receipt['outcome']['refresh']


def test_courtesy_only_brief_replays_the_accepted_ordinary_edition(context, rec, monkeypatch):
    from test_requested_brief import seed
    text = 'Send my brief, please'
    body = request({**ARGS, 'remainder': None})
    body.update(text=text, text_hash=hashlib.sha256(text.encode()).hexdigest())
    receipt = call(body, context, rec, monkeypatch)
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    procedure = json.loads(job['payload']['prompt'])
    path = seed(Path(rec.DATA))
    artifact = json.loads(path.read_text())
    outbox = Path(rec.DATA) / 'events/outbox.json'
    state = json.loads(outbox.read_text())
    state['rows'][0]['day'] = procedure['day']
    outbox.write_text(json.dumps(state))
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *_: None)
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_: None)
    monkeypatch.setattr(rec, '_source_context', lambda: SimpleNamespace(allowed=lambda _: True))
    proof = {'source_revision': 'a' * 64, 'route_digest': 'b' * 64}
    monkeypatch.setattr(rec, '_composition_evidence', lambda _: proof)
    monkeypatch.setattr(rec, '_requested_cache_current', lambda *_: True)
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *_a, **_kw: pytest.fail('courtesy spawned model'))
    assert rec._execute_work(job)['text'] == artifact['brief_text']


def test_multisentence_owner_direction_reaches_fresh_composition(context, rec, monkeypatch):
    from test_requested_brief import seed
    text = ('Send my morning brief. For the meeting prep, focus on Alex Coleman: '
            "the investment decision, what's changed, and the key questions.")
    body = request({**ARGS, 'kind': 'morning', 'remainder': None})
    body.update(text=text, text_hash=hashlib.sha256(text.encode()).hexdigest())
    receipt = call(body, context, rec, monkeypatch)
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    procedure = json.loads(job['payload']['prompt'])
    assert procedure['composition_direction'] == text
    assert not procedure['refresh']
    path = seed(Path(rec.DATA))
    artifact = json.loads(path.read_text())
    outbox = Path(rec.DATA) / 'events/outbox.json'
    state = json.loads(outbox.read_text())
    state['rows'][0]['day'] = procedure['day']
    state['rows'][0]['payload']['label'] = 'brief:sotto-morning-brief'
    outbox.write_text(json.dumps(state))
    artifact['brief_text'] = 'Old ordinary brief'
    path.write_text(json.dumps(artifact))
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *_: None)
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *_: None)
    monkeypatch.setattr(rec, '_source_context', lambda: SimpleNamespace(allowed=lambda _: True))
    proof = {'source_revision': 'a' * 64, 'route_digest': 'b' * 64}
    monkeypatch.setattr(rec, '_composition_evidence', lambda _: proof)
    monkeypatch.setattr(rec, '_requested_cache_current', lambda *_: True)
    assert rec.REQUESTED_BRIEF.accepted_artifact(rec.DATA, procedure['day'], 'morning', lambda _: True)
    monkeypatch.setattr(rec, '_record_delivery', lambda *_a, **_kw: None)
    def fresh_composition(*_a, **_kw):
        raise RuntimeError('fresh composition boundary reached')
    monkeypatch.setattr(rec.subprocess, 'Popen', fresh_composition)
    with pytest.raises(RuntimeError, match='fresh composition boundary reached'):
        rec._execute_work(job)
