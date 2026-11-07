"""Canonical subset publication retains selection order and existing apply proofs."""
import copy
from pathlib import Path
import pytest

from test_runtime_owner_loop_tool import context, call, request, rows, ke
from test_runtime_source_loop_tool import actual_writer_module
import runtime_owner_loop_tool as tool

__all__ = ['context', 'actual_writer_module']


def subset(context, keys=None):
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    anchors=[]
    for index,name in enumerate(['amber','birch','cedar','unrelated'],1):
        result=call(request({'operation':'add','text':name},event=str(index)*64,text='Add '+name),context)
        anchors.append(result['anchor_key'])
    selected=keys if keys is not None else [anchors[2],anchors[0],anchors[1]]
    original={'operation':'propose_cleanup','scope':'open','source_revision':'a'*64,'anchor_keys':selected}
    proposal=request(original,event='a'*64,operation='preview',text='Show those three tasks')
    return anchors,proposal,call(proposal,context)


def completion(proposal,preview,index=1):
    return request({'operation':'cleanup','list':preview['review'],
        'proposal':{'event_id':proposal['event_id'],'action_id':proposal['action_id'],
            'args_hash':tool._identity(proposal['event_id'],proposal['args'])[1],
            'accepted_dispatch_id':'accepted-subset','source_revision':'a'*64},
        'items':[{'index':index,'action':'resolve','correction':str(index)}]},
        event='f'*64,text=f'{index} is done')


def test_subset_order_is_receiver_owned_and_apply_reconstructs_original_args(context):
    anchors,proposal,preview=subset(context)
    assert preview['status']=='preview'
    review=preview['review']
    assert review['anchor_keys']==[anchors[2],anchors[0],anchors[1]]
    assert [row['what'] for row in review['rows']]==['cedar','amber','birch']
    assert [row['index'] for row in review['rows']]==[1,2,3]
    assert review['total']==4 and review['remaining']==1
    assert all(row['loop_version'] and row['loop_identity'] for row in review['rows'])
    assert all(row['status']=='open' for row in rows()), 'publication is read-only'
    result=call(completion(proposal,preview),context)
    assert result['outcome']=='complete'
    assert result['items'][0]['anchor_key']==anchors[2]
    assert {row['anchor_key']:row['status'] for row in rows()}=={
        anchors[0]:'open',anchors[1]:'open',anchors[2]:'resolved',anchors[3]:'open'}


def test_unknown_subset_anchor_refuses_without_falling_back_to_a_different_list(context):
    _,_,result=subset(context,keys=['missing'])
    assert result['status']=='rejected' and result['code']=='target_unavailable'
    assert all(row['status']=='open' for row in rows())


@pytest.mark.parametrize('keys',[[],['duplicate','duplicate'],['x']*51,['\nforged']])
def test_subset_shape_refuses_before_any_list_or_effect(context,keys):
    with pytest.raises(ValueError,match='subset'):
        subset(context,keys=keys)
    assert all(row['status']=='open' for row in rows())


def test_subset_review_order_and_proposal_are_bound(context):
    _,proposal,preview=subset(context)
    changed=copy.deepcopy(preview)
    changed['review']['anchor_keys'].reverse()
    changed['review']['review_hash']=ke._cleanup_hash({k:v for k,v in changed['review'].items() if k!='review_hash'})
    with pytest.raises(ValueError,match='subset binding'):
        call(completion(proposal,changed),context)
    # Even an internally valid replacement list cannot impersonate the accepted proposal.
    changed['review']['rows'].reverse()
    for index,row in enumerate(changed['review']['rows'],1):
        row['index']=index
    changed['review']['review_hash']=ke._cleanup_hash({k:v for k,v in changed['review'].items() if k!='review_hash'})
    with pytest.raises(ValueError,match='proposal'):
        call(completion(proposal,changed),context)
    assert all(row['status']=='open' for row in rows())


@pytest.mark.parametrize('changed',['source','target'])
def test_stale_subset_source_or_target_never_closes_a_different_task(context,changed):
    anchors,proposal,preview=subset(context)
    if changed=='source':
        context[1]['_source_authority']=lambda:{'complete':True,'revision':'b'*64,'google_sources':[]}
    else:
        row=next(row for row in rows() if row['anchor_key']==anchors[2])
        row['summary']='changed task'
        ke._load_continuity()._persist(row)
    result=call(completion(proposal,preview),context)
    if changed=='source':
        assert result['status']=='rejected' and result['code']=='source_unavailable'
    else:
        assert result['items'][0]['status']=='refused' and result['items'][0]['reason'] in ('target_changed','target_unavailable')
    assert all(row['status']=='open' for row in rows())


@pytest.mark.parametrize('kind', ['active', 'folded'])
def test_subset_refuses_duplicate_anchor_instead_of_renumbering_remaining_tasks(context, kind):
    if kind == 'folded':
        # Real migrated source rows may retain an old filename. Owner-only proof rows
        # cannot be generically folded: doing so invalidates their content digest.
        from test_runtime_loop_cleanup import prepare_cleanup
        prepare_cleanup(context)
        anchors = [row['anchor_key'] for row in sorted(rows(), key=lambda row: row['anchor_key'])]
        proposal = request({'operation': 'propose_cleanup', 'scope': 'open',
            'source_revision': 'a'*64, 'anchor_keys': [anchors[2], anchors[0]]},
            event='a'*64, operation='preview', text='Show those tasks')
    else:
        anchors, proposal, _ = subset(context)
    cr = ke._load_continuity()
    original = next(row for row in rows() if row['anchor_key'] == anchors[2])
    duplicate = copy.deepcopy(original)
    duplicate['_path'] = str(Path(cr._dir()) / 'legacy-duplicate.md')
    if kind == 'folded':
        cr._terminate(duplicate, 'dismissed', 'merged_duplicate', '2026-10-05')
        duplicate['merged_into'] = duplicate['anchor_key']
        cr._persist(duplicate)
    else:
        # A second writer call gives the clone a new transition timestamp and
        # invalidates its owner-content digest once the clock crosses a second.
        # The fixture needs two valid copies, not one valid and one withheld row.
        canonical = Path(cr._dir()) / (cr._safe(original['anchor_key']) + '.md')
        Path(duplicate['_path']).write_bytes(canonical.read_bytes())
    from loops_query import permitted_rows
    permitted = context[1]['_source_authority']().get('google_sources', [])
    assert sum(row['anchor_key'] == anchors[2]
               for row in permitted_rows(str(context[0]), permitted)) == 2
    before = {path: path.read_bytes() for path in context[0].rglob('*.md')}
    result = call(request(proposal['args'], event='b'*64, operation='preview', text='Show those three tasks'), context)
    assert result['status'] == 'rejected' and result['code'] == 'target_unavailable'
    assert {path: path.read_bytes() for path in context[0].rglob('*.md')} == before
