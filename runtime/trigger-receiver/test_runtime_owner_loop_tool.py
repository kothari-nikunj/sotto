"""Natural task effects retain exact owner spans and durable canonical receipts."""
import hashlib
import json
from datetime import timedelta

import pytest

import runtime_owner_loop_tool as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, WHEN, THREAD, capability, ke

__all__ = ['context']
TEXT = 'Add reviewing the cedar packet to my tasks.'
ARGS = {'operation': 'add', 'text': 'reviewing the cedar packet'}


def request(args=ARGS, event='d' * 64, operation='apply', text=TEXT):
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([event, 'owner_loop_tool', canonical],
                                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return dict(version=1, operation=operation, identity=IDENTITY, action_id=action,
                event_id=event, payload_hash='b' * 64, text_hash=hashlib.sha256(text.encode()).hexdigest(),
                text=text, admitted_at_ms=int(WHEN.timestamp() * 1000), provider_thread_id=THREAD,
                args=args)


def call(body, context):
    data, policy, activation = context
    policy.update(SOTTO_DATA=str(data), SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1')
    return tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT, policy=policy,
                        data_root=data, capability=capability, activation=activation)


def rows():
    return ke._owner_close_rows(ke._load_continuity())


def test_exact_span_add_close_inspect_and_retention(context):
    body = request()
    assert call(dict(body, operation='inspect'), context)['status'] == 'not_found'
    added = call(body, context)
    assert added['operation'] == 'add' and added['loop_status'] == 'open'
    assert call(dict(body, operation='inspect'), context) == added
    assert call(body, context) == added
    original = rows()[0]
    proof = original['owner_loop_tool_create']
    assert proof['domain'] == 'sotto.owner-loop-tool.v1' and 'text' not in proof
    assert original['summary'] == ARGS['text']
    closing = request({'operation': 'close', 'anchor': added['anchor_key']}, event='e' * 64,
                      text='Mark that task done.')
    closed = call(closing, context)
    assert closed['loop_status'] == 'resolved' and closed['post_digest'] != added['post_digest']
    assert call(dict(closing, operation='inspect'), context) == closed
    assert call(closing, context) == closed
    assert rows()[0]['owner_loop_tool_create'] == proof
    replay = call(dict(body, operation='inspect'), context)
    assert replay['anchor_key'] == added['anchor_key'] and replay['loop_status'] == 'resolved'
    cr = ke._load_continuity()
    cr.resolve({}, now=WHEN + timedelta(days=90), merge=False, resolve_existing=False)
    assert call(dict(closing, operation='inspect'), context) == closed
    assert len(rows()) == 1


def test_changed_args_cross_domain_and_missing_target(context):
    added = call(request(), context)
    changed = request({'operation': 'add', 'text': 'cedar packet'})
    assert call(changed, context)['code'] == 'event_conflict'
    assert call(request({'operation': 'close', 'anchor': added['anchor_key']}), context)['code'] == 'event_conflict'
    with pytest.raises(ke.OwnerLoopPreEffectRefusalError):
        ke.op_loop_add_admitted_owner_text(ARGS['text'], owner_text_span=ARGS['text'],
                                           owner_event_id='d' * 64, now=WHEN)
    assert call(request({'operation': 'close', 'anchor': 'missing'}, event='f' * 64), context)['code'] == 'target_unavailable'
    assert len(rows()) == 1 and rows()[0]['status'] == 'open'


def test_paraphrase_and_invalid_original_are_pre_effect(context):
    assert call(request({'operation': 'add', 'text': 'Review the cedar packet'}), context)['code'] == 'owner_span_unavailable'
    with pytest.raises(ValueError, match='hash'):
        call(dict(request(), text_hash='c' * 64), context)
    for span in (' review', 'review\npacket', 'a' * 501):
        with pytest.raises(ValueError):
            call(request({'operation': 'add', 'text': span}), context)
    assert rows() == []


def test_literal_creation_cannot_be_reinterpreted_and_terminal_close_refuses(context):
    anchor = ke.op_loop_add_admitted_owner_text(ARGS['text'], owner_text_span=ARGS['text'],
                                               owner_event_id='d' * 64, now=WHEN)['anchor_key']
    assert call(request(), context)['code'] == 'event_conflict'
    closed = call(request({'operation': 'close', 'anchor': anchor}, event='e' * 64), context)
    assert closed['loop_status'] == 'resolved'
    assert call(request({'operation': 'close', 'anchor': anchor}, event='f' * 64), context)['code'] == 'target_unavailable'
    assert rows()[0].get('owner_loop_tool_create') is None


def test_ambiguous_anchor_refuses_before_write(context, monkeypatch):
    added = call(request(), context)
    snapshot = rows()[0]
    monkeypatch.setattr(tool.loops, '_load_writer', lambda: ke)
    monkeypatch.setattr(ke, '_owner_close_rows', lambda _cr: [snapshot, dict(snapshot)])
    with pytest.raises(ke.EditError):
        call(request({'operation': 'close', 'anchor': added['anchor_key']}, event='e' * 64), context)
    assert snapshot['status'] == 'open'


def test_direct_waiting_on_original_projection_chase_and_recovery(context,monkeypatch):
    value={'operation':'add','text':'send the deck','direction':'them','contact':'Maya','identifier':'maya@example.com'}
    body=request(value,text='Maya at maya@example.com owes me: send the deck')
    receipt=call(body,context)
    assert receipt['direction']=='them'
    row=rows()[0]
    assert row['action_type']=='waiting_on' and row['contact_name']=='Maya'
    from ledger_provenance import classify_ledger_sources
    assert classify_ledger_sources(row)==('owner',frozenset())
    import importlib.util
    from test_runtime_owner_loop_close import PACK
    spec=importlib.util.spec_from_file_location('direction_loops_query',PACK/'_shared/scripts/loops_query.py')
    query=importlib.util.module_from_spec(spec);spec.loader.exec_module(query)
    projection=query.query_owner_rows(rows(),now=WHEN)
    assert len(projection['waiting_on_them'])==1 and not projection['you_owe']
    monkeypatch.setattr(ke,'_op_loop_add_unlocked',lambda *a,**k:pytest.fail('repeated waiting writer'))
    replay=call(dict(body,operation='inspect'),context)
    assert replay['anchor_key']==receipt['anchor_key'] and replay['direction']=='them'
    assert call(request({**value,'contact':'M'},text=body['text']),context)['code']=='event_conflict'
    cr=ke._load_continuity()
    later=WHEN+timedelta(days=20);day=later.strftime('%Y-%m-%d')
    assert cr.chase_due(row,day,later,{})
    cr._stamp_chase([row],day,later)
    assert cr._load_items()[receipt['anchor_key']]['chase_pending']==day
    assert len(query.query_owner_rows(rows(),now=later)['waiting_on_them'])==1
    assert cr.finalize_chase(receipt['anchor_key'],now=later)['ok'] is True
    assert len(query.query_owner_rows(rows(),now=later)['waiting_on_them'])==1
    assert call(dict(body,operation='inspect'),context)['direction']=='them'
    current=rows()[0];current['chase_pending']=day
    cr._persist(current,owner_chase=True)
    cr._clear_stale_pending([current],(later+timedelta(days=1)).strftime('%Y-%m-%d'))
    assert not cr._load_items()[receipt['anchor_key']].get('chase_pending')
    assert cr.finalize_handoff(receipt['anchor_key'],now=later)['ok'] is True
    assert len(query.query_owner_rows(rows(),now=later)['waiting_on_them'])==1
    assert call(dict(body,operation='inspect'),context)['direction']=='them'



@pytest.mark.parametrize('field,value',[('contact','Other'),('identifier','other@example.com')])
def test_direction_add_wrong_spans_refuse(context,field,value):
    args={'operation':'add','text':'send deck','direction':'them',field:value}
    assert call(request(args,text='Maya owes send deck'),context)['code']=='owner_span_unavailable'
    assert rows()==[]


def test_contact_requires_direction_and_direction_only_unknown(context):
    with pytest.raises(ValueError):
        call(request({'operation':'add','text':'send deck','contact':'Maya'},text='Maya send deck'),context)
    result=call(request({'operation':'add','text':'send deck','direction':'them'},text='They owe send deck'),context)
    assert result['direction']=='them' and not rows()[0].get('contact_identifier')
    assert 'direction' not in call(request(event='f'*64),context)


def test_direction_add_lost_writer_response_inspects_without_second_write(context,monkeypatch):
    monkeypatch.setattr(tool.loops,'_load_writer',lambda:ke)
    original=ke._op_loop_add_unlocked
    calls=[]
    def lost(*a,**k):
        calls.append(1)
        original(*a,**k)
        raise OSError('response lost after canonical write')
    monkeypatch.setattr(ke,'_op_loop_add_unlocked',lost)
    body=request({'operation':'add','text':'send deck','direction':'them'},text='They owe send deck')
    with pytest.raises(OSError):
        call(body,context)
    receipt=call(dict(body,operation='inspect'),context)
    assert receipt['direction']=='them' and len(calls)==1
    assert call(body,context)==receipt and len(calls)==1


@pytest.mark.parametrize('field,value',[('summary','Different obligation'),('source','gmail'),('owner_loop_tool_create',{})])
def test_owner_chase_rejects_unrelated_mutation(context,field,value):
    call(request({'operation':'add','text':'send deck','direction':'them'},text='They owe send deck'),context)
    row=rows()[0];row[field]=value;row['chase_pending']='2026-01-02'
    cr=ke._load_continuity()
    with pytest.raises(ValueError,match='unrelated'):
        cr._persist(row,owner_chase=True)
