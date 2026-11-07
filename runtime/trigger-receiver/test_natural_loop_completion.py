"""Human completion replies use frozen numbers and independent canonical effects."""
import pytest
from test_runtime_owner_loop_tool import context, call, request, rows, ke
from test_runtime_loop_cleanup import prepare_cleanup, tool_identity
from test_runtime_source_loop_tool import actual_writer_module
from delivery_effects import loop_version, loop_identity

__all__=['context','actual_writer_module']


def open_list(context):
    prepare_cleanup(context)
    cr=ke._load_continuity()
    for index in range(5,25):
        cr._persist(dict(anchor_key=f'cleanup-{index}',action_type='follow_up',channel='email',status='open',
            created_at='2026-10-06',summary=f'Obligation {index}',contact_name=f'Person {index}',
            contact_identifier=f'p{index}@example.com',source='gmail',source_message_id=f'm{index}',source_thread_id=f't{index}'))
    proposal=request(dict(operation='propose_cleanup',scope='open',source_revision='a'*64),event='b'*64,operation='preview',text="what's open?")
    review=call(proposal,context)['review']
    assert len(review['rows'])==24  # Fresh obligations are included, not just stale ones.
    return proposal,review


def choose(proposal,review,numbers,text):
    return request(dict(operation='cleanup',list=review,proposal=dict(event_id=proposal['event_id'],
        action_id=proposal['action_id'],args_hash=tool_identity(proposal)[1],accepted_dispatch_id='accepted-list',source_revision='a'*64),
        items=[dict(index=i,action='resolve',correction=str(i)) for i in sorted(numbers)]),event='e'*64,text=text)


def test_screenshot_numbers_close_exact_shown_rows_and_replay_once(context):
    proposal,review=open_list(context)
    numbers=[1,4,6,7,8,11,14,13,17,19,20,22]
    body=choose(proposal,review,numbers,'1,4,6,7,8,11,14,13,17,19,20,22 are done. You can close them')
    selected={review['rows'][i-1]['anchor_key'] for i in numbers}
    before={r['anchor_key']:r for r in rows()}
    result=call(body,context)
    assert result['outcome']=='complete',result
    assert len(result['items'])==12 and all(i['action']=='resolve' for i in result['items'])
    after={r['anchor_key']:r for r in rows()}
    for key,row in after.items():
        if key in selected:
            assert row['status']=='resolved'
        else:
            assert row==before[key]
    assert call(body,context)==result
    assert {r['anchor_key']:r for r in rows()}==after
    assert call(dict(body,operation='inspect'),context)==result


def test_changed_task_refused_without_renumbering_or_blocking_peer(context):
    proposal,review=open_list(context)
    before={r['anchor_key']:r for r in rows()}
    target=review['rows'][21]['anchor_key']
    changed=before[target].copy();changed['summary']='A new request';ke._load_continuity()._persist(changed)
    body=choose(proposal,review,[1,22],'1 and 22 are done')
    result=call(body,context)
    assert result['outcome']=='partial'
    assert [i['status'] for i in result['items']]==['applied','refused']
    after={r['anchor_key']:r for r in rows()}
    assert after[target]['status']=='open' and after[target]['summary']=='A new request'
    assert after[review['rows'][0]['anchor_key']]['status']=='resolved'


@pytest.mark.parametrize('text',["1 and 22 aren't done",'Are 1 and 22 done?','1 and 22 are done. Actually no.','1,22,23 are done','11 and 22 are done','1 and 22 are done; send everyone a text'])
def test_numeric_negation_mismatch_and_extra_intent_never_close(context,text):
    proposal,review=open_list(context)
    before=rows()
    result=call(choose(proposal,review,[1,22],text),context)
    assert result['status']=='rejected' and rows()==before


def test_missing_annabel_does_not_block_uniquely_named_flash_thread(context):
    prepare_cleanup(context)
    cr=ke._load_continuity()
    row=next(r for r in rows() if r['anchor_key']=='cleanup-2');row['contact_name']='FLASH ‼';cr._persist(row)
    row=next(r for r in rows() if r['anchor_key']=='cleanup-2')
    phrase='Flash text thread'
    items=[dict(name='Annabel',correction='Annabel'),dict(name=phrase,correction=phrase,anchor_key=row['anchor_key'],loop_version=loop_version(row),loop_identity=loop_identity(row))]
    body=request(dict(operation='resolve_many',allow_partial=True,source_revision='a'*64,selection_mode='named_people',items=items),
        text='Annabel is handled. Flash text thread is handled too. What else?')
    result=call(body,context)
    assert result['outcome']=='partial',result
    assert [i['status'] for i in result['items']]==['refused','applied']
    assert result['items'][0]['anchor_key']==''
    assert all(r['status']==('resolved' if r['anchor_key']=='cleanup-2' else 'open') for r in rows())
    assert call(body,context)==result


def test_ambiguous_same_name_keeps_both_but_closes_independent_task(context):
    prepare_cleanup(context);cr=ke._load_continuity()
    for row in rows():
        if row['anchor_key'] in ('cleanup-1','cleanup-2'):
            row['contact_name']='Annabel';cr._persist(row)
    current={r['anchor_key']:r for r in rows()}
    items=[dict(name=name,correction=name,anchor_key=key,loop_version=loop_version(current[key]),loop_identity=loop_identity(current[key])) for name,key in [('Annabel','cleanup-1'),('Person 3','cleanup-3')]]
    body=request(dict(operation='resolve_many',allow_partial=True,source_revision='a'*64,selection_mode='named_people',items=items),text='Annabel and Person 3 are done')
    result=call(body,context)
    assert [i['status'] for i in result['items']]==['refused','applied']
    assert all(r['status']==('resolved' if r['anchor_key']=='cleanup-3' else 'open') for r in rows())


def test_uncertain_clear_selection_does_not_replay_or_hide_missing_name(context,monkeypatch):
    prepare_cleanup(context);cr=ke._load_continuity()
    row=next(r for r in rows() if r['anchor_key']=='cleanup-3')
    items=[dict(name='Person 3',correction='Person 3',anchor_key=row['anchor_key'],loop_version=loop_version(row),loop_identity=loop_identity(row)),dict(name='Annabel',correction='Annabel')]
    body=request(dict(operation='resolve_many',allow_partial=True,source_revision='a'*64,selection_mode='named_people',items=items),text='Person 3 and Annabel are done')
    original=cr._persist
    def lost_response(*args,**kwargs):
        original(*args,**kwargs)
        raise OSError('lost completion response')
    monkeypatch.setattr(cr,'_persist',lost_response)
    result=call(body,context)
    assert [i['status'] for i in result['items']]==['unknown','refused']
    monkeypatch.setattr(cr,'_persist',lambda *a,**k:pytest.fail('never retry uncertain completion'))
    assert call(body,context)==result
    assert call(dict(body,operation='inspect'),context)==result
