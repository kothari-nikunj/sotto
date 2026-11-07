"""Adversarial task admission, retained receipts, and independent source availability."""
import copy
from pathlib import Path

import pytest

from test_runtime_owner_loop_tool import context, call, request, rows, ke
from test_runtime_source_loop_tool import actual_writer_module
from test_runtime_loop_cleanup import prepare_cleanup
import runtime_owner_loop_tool as tool

__all__=['context','actual_writer_module']


def snapshot(context):
    return {path:path.read_bytes() for path in context[0].rglob('*.md')}


def test_keep_two_and_five_cannot_resolve_five(context):
    actions=[{'index':2,'action':'keep','correction':'keep 2'},{'index':4,'action':'resolve','correction':'4'}]
    body=prepare_cleanup(context,actions)
    body=request(body['args'],event='e'*64,text='keep 2 and 4')
    before=snapshot(context)
    assert tool.cleanup_selections_match('keep 2 and 5',[actions[0],{'index':5,'action':'resolve','correction':'5'}]) is False
    assert call(body,context)['code']=='owner_span_unavailable'
    assert snapshot(context)==before


@pytest.mark.parametrize('text,actions',[
    ('keep 2 and 4',[{'index':2,'action':'keep','correction':'keep 2'},{'index':4,'action':'resolve','correction':'4'}]),
    ('1 and 3 are done, 5 is still open',[{'index':1,'action':'resolve','correction':'1'},{'index':3,'action':'resolve','correction':'3'}]),
    ('1 and 3 are done. Close those 2.',[{'index':1,'action':'resolve','correction':'1'},{'index':3,'action':'resolve','correction':'3'}]),
])
@pytest.mark.parametrize('unknown',[False,True])
def test_saved_receipt_is_inspectable_but_never_applied_again(context,monkeypatch,unknown,text,actions):
    body=prepare_cleanup(context,actions)
    body=request(body['args'],event='e'*64,text=text)
    original=tool.cleanup_selections_match
    before=snapshot(context)
    assert call(body,context)['code']=='owner_span_unavailable'
    import preferences
    with monkeypatch.context() as missing:
        missing.setattr(preferences,'_durable_preferences_replace',lambda *a:pytest.fail('missing inspection cannot create a receipt'))
        assert call(dict(body,operation='inspect'),context)['status']=='not_found'
    assert snapshot(context)==before
    # Simulate the exact old admission rule; the current wire schema remains unchanged.
    def legacy_match(text,items,**kwargs):
        if all(item['action']=='resolve' for item in items):
            return tool.resolve_many_names_match(text,[{'name':item['correction'],'correction':item['correction']} for item in items],'owner_tasks')
        return original(text,items,allow_stored_mixed_resolve=True)
    with monkeypatch.context() as legacy:
        legacy.setattr(tool,'cleanup_selections_match',legacy_match)
        if unknown:
            cr=ke._load_continuity();persist=cr._persist;writes=0
            def lose(row,**kwargs):
                nonlocal writes
                writes+=1;persist(row,**kwargs)
                if writes==1:
                    raise OSError('old receipt response lost')
            legacy.setattr(cr,'_persist',lose)
        stored=call(body,context)
    before=snapshot(context)
    monkeypatch.setattr(ke._load_continuity(),'_persist',lambda *a,**k:pytest.fail('inspection never replays a mutation'))
    monkeypatch.setattr(ke,'_owner_close_rows',lambda *a:pytest.fail('inspection never scans current rows'))
    inspected=call(dict(body,operation='inspect'),context)
    assert inspected==stored
    assert [item['status'] for item in inspected['items']]==(['unknown','skipped'] if unknown else ['applied','applied'])
    assert call(dict(body,operation='inspect'),context)==inspected
    assert call(body,context)['code']=='owner_span_unavailable'
    assert snapshot(context)==before


@pytest.mark.parametrize('text',[
    '1 and 3 are not done, 5 is still open',
    '1 and 3 are done. Close those 3.',
    '1 and 3 are done, 3 is still open',
    '1 and 3 are done. Cancel that.',
])
def test_stored_all_resolve_compatibility_keeps_full_text_guards(context,text):
    body=prepare_cleanup(context,[{'index':index,'action':'resolve','correction':str(index)} for index in (1,3)])
    body=request(body['args'],event='e'*64,text=text)
    before=snapshot(context)
    assert call(dict(body,operation='inspect'),context)['code']=='owner_span_unavailable'
    assert snapshot(context)==before


@pytest.mark.parametrize('kind',['folded','active','uncertain'])
def test_duplicate_anchor_is_withheld_without_blocking_independent_list_rows(context,kind):
    prepare_cleanup(context)
    cr=ke._load_continuity();duplicate=copy.deepcopy(rows()[0])
    duplicate['_path']=str(Path(cr._dir())/'legacy-duplicate.md')
    if kind=='folded':
        cr._terminate(duplicate,'dismissed','merged_duplicate','2026-10-05')
        duplicate['merged_into']=duplicate['anchor_key']
    elif kind=='uncertain':
        duplicate.update(status='mystery',resolution='merged_duplicate',merged_into=duplicate['anchor_key'])
    cr._persist(duplicate)
    before=snapshot(context)
    result=call(request({'operation':'propose_cleanup','scope':'open','source_revision':'a'*64},
        event='c'*64,operation='preview',text='What is open?'),context)
    assert result['status']=='preview'
    review=result['review'];withheld=duplicate['anchor_key']
    assert withheld not in {row['anchor_key'] for row in review['rows']}
    assert len(review['rows'])==3
    assert review['remaining']==(2 if kind=='active' else 1)
    assert [row['index'] for row in review['rows']]==[1,2,3]
    assert 'more tasks are not shown' in ke.render_cleanup_list(review)
    assert snapshot(context)==before


def mixed_source_request(context, *, direct=False, allow_partial=True):
    prepare_cleanup(context)
    cr=ke._load_continuity()
    row=next(row for row in rows() if row['anchor_key']=='cleanup-2')
    row.update(source='imessage',channel='imessage');cr._persist(row)
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':['gmail'],'bridge_sources':['imessage']}
    if direct:
        from delivery_effects import loop_identity,loop_version
        args={'operation':'resolve_many','source_revision':'a'*64,'selection_mode':'named_people',
            'items':[{'name':row['contact_name'],'correction':row['contact_name'],'anchor_key':row['anchor_key'],
                'loop_version':loop_version(row),'loop_identity':loop_identity(row)}
                for row in sorted(rows(),key=lambda row:row['anchor_key'])[:3]]}
        if allow_partial:
            args['allow_partial']=True
        return request(args,event='e'*64,text='Person 1 and Person 2 and Person 3 are done')
    proposal=request({'operation':'propose_cleanup','scope':'open','source_revision':'a'*64},
        event='c'*64,operation='preview',text='Show my tasks')
    review=call(proposal,context)['review']
    args={'operation':'cleanup','list':review,'proposal':{'event_id':proposal['event_id'],
        'action_id':proposal['action_id'],'args_hash':tool._identity(proposal['event_id'],proposal['args'])[1],
        'accepted_dispatch_id':'accepted-mixed-source-list','source_revision':'a'*64},
        'items':[{'index':index,'action':'resolve','correction':str(index)} for index in (1,2,3)]}
    return request(args,event='e'*64,text='1, 2 and 3 are done')


@pytest.mark.parametrize('direct',[False,True])
@pytest.mark.parametrize('phase',['gather','pre_effect'])
def test_unavailable_bridge_refuses_only_its_item_with_current_global_authority(context,direct,phase):
    body=mixed_source_request(context,direct=direct)
    calls=0
    def authority():
        nonlocal calls
        calls+=1
        available=phase=='pre_effect' and calls<9
        return {'complete':True,'revision':'a'*64,'google_sources':['gmail'],
            'bridge_sources':['imessage'] if available else []}
    context[1]['_source_authority']=authority
    result=call(body,context)
    assert [item['status'] for item in result['items']]==['applied','refused','applied']
    assert result['items'][1]['reason']=='source_unavailable'
    after={row['anchor_key']:row['status'] for row in rows()}
    assert after=={'cleanup-1':'resolved','cleanup-2':'open','cleanup-3':'resolved','cleanup-4':'open'}
    assert call(dict(body,operation='inspect'),context)==result


@pytest.mark.parametrize('mode',['incomplete','revision'])
@pytest.mark.parametrize('phase',['gather','pre_effect'])
def test_global_authority_change_holds_all_not_yet_attempted_items(context,mode,phase):
    body=mixed_source_request(context)
    calls=0
    def authority():
        nonlocal calls
        calls+=1
        changed=calls>=(3 if phase=='gather' else 9)
        return {'complete':not(changed and mode=='incomplete'),'revision':'b'*64 if changed and mode=='revision' else 'a'*64,
            'google_sources':['gmail'],'bridge_sources':['imessage']}
    context[1]['_source_authority']=authority
    before=snapshot(context)
    result=call(body,context)
    if phase=='gather':
        assert result['status']=='rejected' and result['code']=='source_unavailable'
        assert snapshot(context)==before
    else:
        assert [item['status'] for item in result['items']]==['applied','refused','skipped']
        assert next(row for row in rows() if row['anchor_key']=='cleanup-3')['status']=='open'
        assert call(dict(body,operation='inspect'),context)==result


def test_legacy_nonpartial_named_batch_still_refuses_before_any_effect(context):
    body=mixed_source_request(context,direct=True,allow_partial=False)
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':['gmail'],'bridge_sources':[]}
    before=snapshot(context)
    result=call(body,context)
    assert result['status']=='rejected' and result['code']=='source_unavailable'
    assert snapshot(context)==before


@pytest.mark.parametrize('selector,name,expected',[
    ('Flash text thread','Flash Thompson',False),
    ('Flash chat thread','Flash Thompson',False),
    ('Flash thread','Flash Thompson',False),
    ('Flash text thread','FLASH‼',True),
    ('Flash','Flash Thompson',True),
])
def test_explicit_thread_selector_never_falls_back_to_another_person(context,selector,name,expected):
    from delivery_effects import loop_version,loop_identity
    prepare_cleanup(context)
    cr=ke._load_continuity();row=next(row for row in rows() if row['anchor_key']=='cleanup-1')
    row['contact_name']=name;cr._persist(row)
    args={'operation':'resolve_many','source_revision':'a'*64,'selection_mode':'named_people','allow_partial':True,
        'items':[{'name':selector,'correction':selector,'anchor_key':row['anchor_key'],
            'loop_version':loop_version(row),'loop_identity':loop_identity(row)}]}
    before=snapshot(context)
    result=call(request(args,event='e'*64,text=selector+' is handled'),context)
    assert result['items'][0]['status']==('applied' if expected else 'refused')
    if not expected:
        assert result['items'][0]['reason']=='target_unavailable'
        assert snapshot(context)==before


def test_snapshot_keeps_one_initial_read_and_every_post_effect_strict_read(context,monkeypatch):
    body=mixed_source_request(context)
    reads=0;original=ke._owner_close_rows
    def load(cr):
        nonlocal reads
        reads+=1
        return original(cr)
    monkeypatch.setattr(ke,'_owner_close_rows',load)
    result=call(body,context)
    assert [item['status'] for item in result['items']]==['applied']*3
    assert reads==4
    monkeypatch.setattr(ke,'_owner_close_rows',lambda cr:pytest.fail('stored inspection never scans or applies'))
    assert call(dict(body,operation='inspect'),context)==result


def test_snapshot_does_not_hide_an_initial_active_anchor_ambiguity(context,monkeypatch):
    body=mixed_source_request(context)
    original=ke._owner_close_rows
    def ambiguous(cr):
        current=original(cr)
        current.append(copy.deepcopy(next(row for row in current if row['anchor_key']=='cleanup-1')))
        return current
    monkeypatch.setattr(ke,'_owner_close_rows',ambiguous)
    result=call(body,context)
    assert [item['status'] for item in result['items']]==['refused','applied','applied']
    assert next(row for row in rows() if row['anchor_key']=='cleanup-1')['status']=='open'


def test_real_prior_file_validation_still_blocks_mutation_after_snapshot(context):
    body=mixed_source_request(context)
    cr=ke._load_continuity();calls=0
    def authority():
        nonlocal calls
        calls+=1
        if calls==6:
            row=next(row for row in rows() if row['anchor_key']=='cleanup-1')
            row['summary']='changed after strict snapshot';cr._persist(row)
        return {'complete':True,'revision':'a'*64,'google_sources':['gmail'],'bridge_sources':['imessage']}
    context[1]['_source_authority']=authority
    result=call(body,context)
    assert [item['status'] for item in result['items']]==['unknown','skipped','skipped']
    first=next(row for row in rows() if row['anchor_key']=='cleanup-1')
    assert first['status']=='open' and first['summary']=='changed after strict snapshot'
    assert call(dict(body,operation='inspect'),context)==result


def test_failed_strict_post_write_readback_keeps_unknown_stop_and_no_replay(context,monkeypatch):
    body=mixed_source_request(context)
    original=ke._owner_close_rows;reads=0
    def fail_readback(cr):
        nonlocal reads
        reads+=1
        if reads==2:
            raise ke.EditError('synthetic strict readback failure')
        return original(cr)
    monkeypatch.setattr(ke,'_owner_close_rows',fail_readback)
    result=call(body,context)
    assert [item['status'] for item in result['items']]==['unknown','skipped','skipped']
    assert next(row for row in original(ke._load_continuity()) if row['anchor_key']=='cleanup-1')['status']=='resolved'
    before=snapshot(context)
    monkeypatch.setattr(ke._load_continuity(),'_persist',lambda *a,**k:pytest.fail('unknown effect must not be replayed'))
    assert call(dict(body,operation='inspect'),context)==result
    assert snapshot(context)==before and reads==2


def test_snapshot_holds_existing_ledger_lock_against_other_canonical_writers(context):
    import threading
    body=mixed_source_request(context)
    cr=ke._load_continuity();attempted=threading.Event();acquired=threading.Event();worker=None;calls=0
    def writer():
        attempted.set()
        with cr._ledger_lock():
            acquired.set()
    def authority():
        nonlocal calls,worker
        calls+=1
        if calls==2:
            worker=threading.Thread(target=writer)
            worker.start()
            assert attempted.wait(1)
            assert not acquired.wait(.05),'the snapshot requires the canonical ledger lock throughout the batch'
        return {'complete':True,'revision':'a'*64,'google_sources':['gmail'],'bridge_sources':['imessage']}
    context[1]['_source_authority']=authority
    try:
        assert call(body,context)['outcome']=='complete'
    finally:
        if worker:
            worker.join(2)
    assert acquired.is_set()
