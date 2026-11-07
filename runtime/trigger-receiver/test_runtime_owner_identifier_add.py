"""Original identifier teaching, exact canonical selection and durable uncertainty."""
from pathlib import Path

import pytest
from test_runtime_owner_fact_correct import person, readback, REVISION
from test_runtime_owner_loop_tool import context, request, call as direct_call, ke

__all__ = ['person', 'context']



def call(body, context):
    if body['args']['operation']!='add_identifier':
        return direct_call(body,context)
    if body['operation']=='inspect':
        body={**body,'args':{**body['args'],'operation':'identifier'}} if 'review' in body['args'] else body
        return direct_call(body,context)
    proposed=request(args={**body['args'],'operation':'propose_identifier'},operation='preview',event='f'*64,text=body['text'])
    preview=direct_call(proposed,context)
    if preview['status']!='preview':
        return preview
    import runtime_owner_loop_tool as tool
    _,args_hash=tool._identity(proposed['event_id'],proposed['args'])
    body['args']={'operation':'identifier','review':preview['review'],'proposal':{'event_id':proposed['event_id'],'action_id':proposed['action_id'],'args_hash':args_hash,'accepted_dispatch_id':'accepted-identifier-review','source_revision':REVISION}}
    updated=request(args=body['args'],event=body['event_id'],text='Yes')
    body.update(updated)
    return direct_call(body,context)


def args():
    return {'operation':'add_identifier','canonical_id':'c_a1b2c3',
        'identifier':'+1 (310) 924-5269','source_revision':REVISION}


def test_original_writer_resolver_and_historical_receipt(context,person,monkeypatch):
    body=request(args=args(),text='That '+args()['identifier']+' number is Dana.')
    first=call(body,context)
    assert first['status']=='committed' and first['added'] is True
    actual=readback()
    import render_local
    resolver=render_local.build_identity_resolver({'contacts':[], 'contact_index':[
        {'name':actual.name,'canonical_id':actual.canonical_id,'identifiers':actual.identifiers}]})
    assert resolver('+13109245269')['name']=='Dana'
    before={str(p):p.read_bytes() for p in Path(ke.kg.people_dir()).glob('*.md')}
    monkeypatch.setattr(ke,'op_add_identifier',lambda *a,**k:pytest.fail('repeated writer'))
    context[1]['_source_authority']=lambda:{'complete':False,'revision':None}
    assert call(dict(body,operation='inspect'),context)==first
    import preferences
    preferences.add('tone_notes','Be concise')
    assert call(dict(body,operation='inspect'),context)==first
    assert before=={str(p):p.read_bytes() for p in Path(ke.kg.people_dir()).glob('*.md')}


def test_duplicate_graph_bytes_unchanged(context,person):
    value={**args(),'identifier':'DANA@example.com'}
    before={str(p):p.read_bytes() for p in Path(ke.kg.people_dir()).glob('*.md')}
    result=call(request(args=value,text=value['identifier']),context)
    assert result['status']=='committed' and result['added'] is False
    assert before=={str(p):p.read_bytes() for p in Path(ke.kg.people_dir()).glob('*.md')}


def test_collision_and_duplicate_canonical_refuse_before_marker(context,person):
    _,path=person
    other=ke.kg.PersonFile(canonical_id='c_123456',name='Other',identifiers=[args()['identifier']],facts={})
    ke.kg.write_person_file(str(path.with_name('other.md')),other)
    assert call(request(args=args(),text=args()['identifier']),context)['code']=='identifier_collision'
    import preferences
    assert not Path(preferences._path()).exists()
    path.with_name('other.md').write_bytes(path.read_bytes())
    assert call(request(args=args(),text=args()['identifier']),context)['code']=='target_changed'


def test_lost_graph_write_and_fsync_failure_hold(context,person,monkeypatch):
    original=ke.op_add_identifier
    def lost(*a,**k):
        original(*a,**k)
        raise OSError('lost after original write')
    monkeypatch.setattr(ke,'op_add_identifier',lost)
    body=request(args=args(),text=args()['identifier'])
    assert call(body,context)=={'version':1,'status':'unknown'}
    monkeypatch.setattr(ke,'op_add_identifier',lambda *a,**k:pytest.fail('uncertain replay'))
    assert call(dict(body,operation='inspect'),context)=={'version':1,'status':'unknown'}
    assert call(body,context)=={'version':1,'status':'unknown'}


def test_authority_change_after_write_holds(context,person,monkeypatch):
    state={'complete':True,'revision':REVISION}
    context[1]['_source_authority']=lambda:dict(state)
    original=ke.op_add_identifier
    def changed(*a,**k):
        result=original(*a,**k)
        state['revision']='b'*64
        return result
    monkeypatch.setattr(ke,'op_add_identifier',changed)
    assert call(request(args=args(),text=args()['identifier']),context)=={'version':1,'status':'unknown'}


def test_owner_span_and_revoked_no_write(context,person):
    _,path=person
    before=path.read_bytes()
    assert call(request(args=args(),text='Teach Dana'),context)['code']=='owner_span_unavailable'
    context[1]['_source_authority']=lambda:{'complete':False,'revision':None}
    assert call(request(args=args(),text=args()['identifier']),context)['code']=='source_unavailable'
    assert path.read_bytes()==before


def test_graph_sync_failure_never_commits_or_reapplies(context,person,monkeypatch):
    import preferences
    original=preferences._sync_existing_preferences
    def fail_graph(path):
        if str(path).endswith('.md'):
            raise OSError('graph sync unavailable')
        return original(path)
    monkeypatch.setattr(preferences,'_sync_existing_preferences',fail_graph)
    body=request(args=args(),text=args()['identifier'])
    assert call(body,context)=={'version':1,'status':'unknown'}
    monkeypatch.setattr(ke,'op_add_identifier',lambda *a,**k:pytest.fail('sync-loss replay'))
    assert call(dict(body,operation='inspect'),context)=={'version':1,'status':'unknown'}


def test_committed_receipt_corruption_is_not_accepted(context,person):
    import json
    import preferences
    body=request(args=args(),text=args()['identifier'])
    assert call(body,context)['status']=='committed'
    path=Path(preferences._path());data=json.loads(path.read_text())
    data['owner_preference_receipts'][body['event_id']]['post']['canonical_id']='c_123456'
    path.write_text(json.dumps(data))
    with pytest.raises(ke.EditError):
        call(dict(body,operation='inspect'),context)


@pytest.mark.parametrize('identifier',['not-an@email','123456789@lid'])
def test_invalid_original_identifier_refuses_before_marker(context,person,monkeypatch,identifier):
    import preferences
    monkeypatch.setattr(ke,'op_add_identifier',lambda *a,**k:pytest.fail('invalid identifier writer'))
    result=call(request(args={**args(),'identifier':identifier},text=identifier),context)
    assert result['code']=='correction_unavailable'
    assert not Path(preferences._path()).exists()


def test_lookup_preview_and_legacy_fresh_apply_never_write(context,person,monkeypatch):
    import preferences
    _,path=person
    before=path.read_bytes()
    monkeypatch.setattr(ke,'op_add_identifier',lambda *a,**k:pytest.fail('lookup writer'))
    body=request(args=args(),text='Who is '+args()['identifier']+'?')
    assert direct_call(body,context)['code']=='correction_unavailable'
    proposed=request(args={**args(),'operation':'propose_identifier'},operation='preview',text=body['text'])
    assert direct_call(proposed,context)['status']=='preview'
    assert path.read_bytes()==before and not Path(preferences._path()).exists()


@pytest.mark.parametrize('status',['committed','contacting'])
def test_authentic_legacy_record_recovers_without_new_direct_admission(context,person,monkeypatch,status):
    import hashlib,json,preferences
    from datetime import datetime,timezone
    import runtime_owner_loop_tool as tool
    body=request(args=args(),text=args()['identifier'])
    assert direct_call(dict(body,operation='inspect'),context)['status']=='not_found'
    _,args_hash=tool._identity(body['event_id'],body['args'])
    proof={k:body[k] for k in ('action_id','event_id','payload_hash','text_hash','args','admitted_at_ms','provider_thread_id')}
    proof['domain']='sotto.owner-loop-tool.v1'
    proof['args_hash']=args_hash
    proof['identity_digest']=hashlib.sha256(json.dumps(body['identity'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    saved={'tag':'person_identifier_add','proof':proof,'status':status}
    if status=='committed':
        ke.op_add_identifier('legacy-dana',args()['identifier'],datetime.now(timezone.utc))
        after=readback();post={'canonical_id':after.canonical_id,'identifiers':after.identifiers}
        digest=hashlib.sha256(json.dumps(post,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        receipt={k:proof[k] for k in ('action_id','event_id','payload_hash','args_hash')}
        receipt.update(version=1,status='committed',operation='add_identifier',canonical_id=args()['canonical_id'],identifier=args()['identifier'],added=True,post_digest=digest)
        saved.update(post=post,receipt=receipt)
    preferences._durable_preferences_replace(preferences._path(),{'owner_preference_receipts':{body['event_id']:saved}})
    monkeypatch.setattr(ke,'op_add_identifier',lambda *a,**k:pytest.fail('legacy replay writer'))
    context[1]['_source_authority']=lambda:{'complete':False}
    expected=saved.get('receipt',{'version':1,'status':'unknown'})
    assert direct_call(dict(body,operation='inspect'),context)==expected
    assert direct_call(body,context)==expected


@pytest.mark.parametrize('field,value',[('name','Another Dana'),('present',True),('normalized_identifier','different')])
def test_forged_review_hash_refused_before_marker(context,person,field,value):
    import preferences
    proposed=request(args={**args(),'operation':'propose_identifier'},operation='preview',event='f'*64,text=args()['identifier'])
    result=direct_call(proposed,context)
    review=result['review'];review[field]=value
    review['review_hash']=ke._relation_digest({k:v for k,v in review.items() if k!='review_hash'})
    import runtime_owner_loop_tool as tool
    _,ah=tool._identity(proposed['event_id'],proposed['args'])
    confirm=request(args={'operation':'identifier','review':review,'proposal':{'event_id':proposed['event_id'],'action_id':proposed['action_id'],'args_hash':ah,'accepted_dispatch_id':'accepted','source_revision':REVISION}},text='Yes')
    assert direct_call(confirm,context)['code']=='target_changed'
    assert not Path(preferences._path()).exists()


def test_distinct_yes_and_consumed_review(context,person):
    import runtime_owner_loop_tool as tool
    proposed=request(args={**args(),'operation':'propose_identifier'},operation='preview',event='f'*64,text=args()['identifier'])
    review=direct_call(proposed,context)['review']
    _,ah=tool._identity(proposed['event_id'],proposed['args'])
    value={'operation':'identifier','review':review,'proposal':{'event_id':proposed['event_id'],'action_id':proposed['action_id'],'args_hash':ah,'accepted_dispatch_id':'accepted','source_revision':REVISION}}
    assert direct_call(request(args=value,event='f'*64,text='Yes'),context)['code']=='target_unavailable'
    assert direct_call(request(args=value,text='No'),context)['code']=='target_unavailable'
    assert direct_call(request(args=value,text='Yes'),context)['status']=='committed'
    assert direct_call(request(args=value,event='e'*64,text='Yes'),context)['code']=='target_unavailable'
