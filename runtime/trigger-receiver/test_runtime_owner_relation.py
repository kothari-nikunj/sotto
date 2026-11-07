"""Reviewed original paired relation writer through the admitted task lane."""

import pytest
from test_runtime_owner_fact_correct import person,REVISION
from test_runtime_owner_loop_tool import context,request,call,ke

__all__=['person','context']
OTHER='c_123456'


@pytest.fixture
def pair(person):
    _,path=person
    other=path.with_name('legacy-vishnu.md')
    ke.kg.write_person_file(str(other),ke.kg.PersonFile(canonical_id=OTHER,name='Vishnu',identifiers=['vishnu@example.com'],facts={}))
    return path,other


def preview(context,mode='add',kind='introduced_by',date='2026-01-02',event='d'*64):
    args={'operation':'propose_relation','mode':mode,'canonical_id':'c_a1b2c3','other_canonical_id':OTHER,
        'relation_type':kind,'date':date if mode=='add' else '', 'source_revision':REVISION}
    body=request(args,event=event,operation='preview',text='Vishnu introduced me to Dana' if mode=='add' else 'Vishnu did not introduce me to Dana')
    result=call(body,context)
    return result,body


def confirm(result,body,event='e'*64,text='Yes'):
    _,args_hash=ke_identity(body['event_id'],body['args'])
    args={'operation':'relation','review':result['review'],'proposal':{'event_id':body['event_id'],
        'action_id':body['action_id'],'args_hash':args_hash,'accepted_dispatch_id':'accepted-full-review',
        'source_revision':REVISION}}
    return request(args,event=event,text=text)


def ke_identity(event,args):
    import runtime_owner_loop_tool
    return runtime_owner_loop_tool._identity(event,args)


def read(path):
    return ke.kg.parse_person_file(path.read_text())


@pytest.mark.parametrize('kind',list(ke.kg.RELATION_INVERSE))
def test_original_add_remove_all_directions_and_replay(context,pair,kind,monkeypatch):
    before={str(p):p.read_bytes() for p in pair}
    result,body=preview(context,kind=kind)
    assert result['status']=='preview' and result['review']['present'] is False
    assert before=={str(p):p.read_bytes() for p in pair}
    added=call(confirm(result,body),context)
    assert added['changed'] is True and added['status']=='committed'
    assert read(pair[0]).relations[0].type==kind
    assert read(pair[1]).relations[0].type==ke.kg.RELATION_INVERSE[kind]
    assert read(pair[0]).relations[0].date=='2026-01-02'
    # Original downstream person pack uses the reciprocal relationship.
    from test_runtime_owner_loop_close import PACK
    import importlib.util
    spec=importlib.util.spec_from_file_location('relation_original_query',PACK/'_shared/knowledge/knowledge_query.py')
    query=importlib.util.module_from_spec(spec);spec.loader.exec_module(query)
    from datetime import datetime,timezone
    assert ke.kg.RELATION_SENTENCE[kind].format(name='Vishnu') in query.pack_person(read(pair[0]),True,datetime.now(timezone.utc))
    result,body=preview(context,mode='remove',kind=kind,event='f'*64)
    removal=confirm(result,body,event='a'*64)
    removed=call(removal,context)
    assert removed['changed'] is True and not read(pair[0]).relations and not read(pair[1]).relations
    assert ke.kg.RELATION_SENTENCE[kind].format(name='Vishnu') not in query.pack_person(read(pair[0]),True,datetime.now(timezone.utc))
    monkeypatch.setattr(ke,'op_relation',lambda *a,**k:pytest.fail('receipt writer replay'))
    context[1]['_source_authority']=lambda:{'complete':False}
    assert call(dict(removal,operation='inspect'),context)==removed
    import preferences
    preferences.add('tone_notes','Be concise')
    assert call(dict(removal,operation='inspect'),context)==removed


def test_duplicate_absent_noop_date_conflict_and_pair_concurrency(context,pair):
    result,body=preview(context)
    call(confirm(result,body),context)
    before={str(p):p.read_bytes() for p in pair}
    duplicate,dupbody=preview(context,event='f'*64)
    assert duplicate['review']['present'] is True
    assert call(confirm(duplicate,dupbody,event='a'*64),context)['changed'] is False
    assert before=={str(p):p.read_bytes() for p in pair}
    bad,_=preview(context,date='2026-01-03',event='b'*64)
    assert bad['code']=='target_changed'
    removal,removebody=preview(context,mode='remove',event='c'*64)
    # Unrelated concurrent fact remains; pair version deliberately ignores it.
    p=read(pair[0]);p.facts['f_new']=ke.kg.FactMeta(text='New independent context',status='active')
    ke.kg.write_person_file(str(pair[0]),p)
    assert call(confirm(removal,removebody,event='9'*64),context)['changed'] is True
    assert read(pair[0]).facts['f_new'].text=='New independent context'
    absent,absbody=preview(context,mode='remove',event='8'*64)
    assert absent['review']['present'] is False
    assert call(confirm(absent,absbody,event='7'*64),context)['changed'] is False


def test_stale_names_revoked_wrong_yes_and_consumed(context,pair):
    result,body=preview(context)
    assert call(confirm(result,body,text='No'),context)['code']=='target_unavailable'
    p=read(pair[0]);p.name='Different Dana';ke.kg.write_person_file(str(pair[0]),p)
    assert call(confirm(result,body),context)['code']=='target_changed'
    result,body=preview(context,event='f'*64)
    first=confirm(result,body,event='a'*64)
    assert call(first,context)['status']=='committed'
    assert call(confirm(result,body,event='b'*64),context)['code']=='target_unavailable'
    result,body=preview(context,mode='remove',event='c'*64)
    context[1]['_source_authority']=lambda:{'complete':False}
    assert call(confirm(result,body,event='9'*64),context)['code']=='source_unavailable'


@pytest.mark.parametrize('fault',['writer','sync','receipt'])
def test_postcontact_uncertainty_never_reapplies(context,pair,monkeypatch,fault):
    import preferences
    result,body=preview(context)
    original=ke.op_relation
    if fault=='writer':
        def lost(*a,**k):
            original(*a,**k)
            raise OSError('lost original write')
        monkeypatch.setattr(ke,'op_relation',lost)
    elif fault=='sync':
        sync=preferences._sync_existing_preferences
        def failed(path):
            if str(path).endswith('.md'):
                raise OSError('sync unavailable')
            return sync(path)
        monkeypatch.setattr(preferences,'_sync_existing_preferences',failed)
    else:
        replace=preferences._durable_preferences_replace
        def failed(path,data):
            if data['owner_preference_receipts'].get('e'*64,{}).get('status')=='committed':
                raise OSError('receipt unavailable')
            return replace(path,data)
        monkeypatch.setattr(preferences,'_durable_preferences_replace',failed)
    applied=confirm(result,body)
    assert call(applied,context)=={'version':1,'status':'unknown'}
    monkeypatch.setattr(ke,'op_relation',lambda *a,**k:pytest.fail('unknown repeated writer'))
    assert call(dict(applied,operation='inspect'),context)=={'version':1,'status':'unknown'}
    assert call(applied,context)=={'version':1,'status':'unknown'}
    assert read(pair[0]).relations and read(pair[1]).relations


@pytest.mark.parametrize('field,value',[('name','Forged Dana'),('present',True)])
def test_self_consistent_forged_review_refused_before_contact(context,pair,field,value):
    result,body=preview(context)
    result['review'][field]=value
    result['review']['review_hash']=ke._relation_digest({k:v for k,v in result['review'].items() if k!='review_hash'})
    before={str(p):p.read_bytes() for p in pair}
    assert call(confirm(result,body),context)['code']=='target_changed'
    assert before=={str(p):p.read_bytes() for p in pair}


def test_original_journal_partial_pair_recovers_without_wrapper_reapply(context,pair,monkeypatch):
    result,body=preview(context)
    applied=confirm(result,body)
    original=ke.kg._apply_journal_op
    count=0
    def interrupted(op,now):
        nonlocal count
        count+=1
        if count==2:
            raise OSError('second original person write interrupted')
        return original(op,now)
    monkeypatch.setattr(ke.kg,'_apply_journal_op',interrupted)
    assert call(applied,context)=={'version':1,'status':'unknown'}
    assert read(pair[0]).relations and not read(pair[1]).relations
    monkeypatch.setattr(ke.kg,'_apply_journal_op',original)
    monkeypatch.setattr(ke,'op_relation',lambda *a,**k:pytest.fail('unknown wrapper repeated writer'))
    # An unrelated original graph reader enters the existing lock and replays its journal.
    import knowledge_update as ku
    with ku.graph_lock():
        assert read(pair[0]).name=='Dana'
        assert read(pair[0]).relations and read(pair[1]).relations
    assert call(dict(applied,operation='inspect'),context)=={'version':1,'status':'unknown'}
    assert call(applied,context)=={'version':1,'status':'unknown'}
