"""Typed selection derives frozen indices; current owner semantics are independently checked."""
import copy
import json
from pathlib import Path
import pytest
from test_runtime_owner_loop_tool import context, call, request, rows, ke
from test_runtime_source_loop_tool import actual_writer_module
import runtime_owner_loop_tool as tool

__all__ = ['context', 'actual_writer_module']
VECTORS=json.loads((Path(__file__).resolve().parents[1]/'sotto-chat/test/fixtures/numbered-selection-vectors.json').read_text())


def full_list():
    review={'operation':'cleanup_list','scope':'open','source_revision':'a'*64,'total':45,'remaining':0,
        'rows':[{'index':i,'anchor_key':f'task-{i}','name':'Owner','what':f'task {i}',
            'direction':'you_owe','age_days':0,'times_surfaced':0,'surface_count_provenance':'accepted_delivery',
            'overdue':False,'deadline':None,'chased_count':0,'chased_out':False,'suggestion':'do it or dismiss',
            'loop_version':'b'*64,'loop_identity':'c'*64} for i in range(1,46)]}
    review['review_hash']=ke._cleanup_hash(review)
    return review


@pytest.mark.parametrize('vector',VECTORS,ids=lambda vector:vector['label'])
def test_shared_typed_selection_vectors(vector):
    review=full_list();review.update(vector.get('review',{}))
    if review.get('scope') is None:
        review.pop('scope',None)
    review['review_hash']=ke._cleanup_hash({k:v for k,v in review.items() if k!='review_hash'})
    def derive():
        return ke.cleanup_selection_items(review,vector['selection'],owner_text=vector['selection']['correction'],
            completion_matches=lambda text,items:tool.resolve_many_names_match(text,items,'owner_tasks',exact_clause=True))
    if vector['indices'] is None:
        with pytest.raises(ValueError):
            derive()
    else:
        assert [row['index'] for row in derive()]==vector['indices']


def prepared(context,selection):
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    for i in range(1,46):
        call(request({'operation':'add','text':f'Temporary synthetic task {i:02d}'},event=f'{i:064x}',text=f'Add Temporary synthetic task {i:02d}'),context)
    proposal=request({'operation':'propose_cleanup','scope':'open','source_revision':'a'*64},event='a'*64,operation='preview',text='List current tasks')
    preview=call(proposal,context)
    assert len(preview['review']['rows'])==45
    args={'operation':'cleanup','list':preview['review'],'selection':selection,
        'items':ke.cleanup_selection_items(preview['review'],selection),
        'proposal':{'event_id':proposal['event_id'],'action_id':proposal['action_id'],
            'args_hash':tool._identity(proposal['event_id'],proposal['args'])[1],
            'accepted_dispatch_id':'synthetic-accepted-list','source_revision':'a'*64}}
    return request(args,event='f'*64,text=selection['correction'])


@pytest.mark.parametrize('label',['live all-except','exception before predicate','exception range before predicate','mixed untouched and ranges'])
def test_actual_receiver_exact_selected_store_and_untouched_bytes_replay(context,label):
    vector=next(vector for vector in VECTORS if vector['label']==label)
    body=prepared(context,vector['selection'])
    before={path:path.read_bytes() for path in Path(ke._load_continuity()._dir()).glob('*.md')}
    excluded={row['anchor_key'] for row in body['args']['list']['rows'] if row['index'] not in vector['indices']}
    receipt=call(body,context)
    assert receipt['outcome']=='complete' and len(receipt['items'])==len(vector['indices'])
    assert all(item['status']=='applied' for item in receipt['items'])
    after=rows();assert sum(row['status']=='resolved' for row in after)==len(vector['indices'])
    for row in after:
        if row['anchor_key'] in excluded:
            assert row['status']=='open'
            path=next(path for path,raw in before.items() if ('anchor_key: '+row['anchor_key']+'\n').encode() in raw)
            assert path.read_bytes()==before[path]
    files={path:path.read_bytes() for path in before}
    assert call(body,context)==receipt
    assert call(dict(body,operation='inspect'),context)==receipt
    assert files=={path:path.read_bytes() for path in before}


def test_exception_before_predicate_refusals_have_no_canonical_effect(context):
    body=prepared(context,VECTORS[0]['selection'])
    before={path:path.read_bytes() for path in Path(ke._load_continuity()._dir()).glob('*.md')}
    for vector in VECTORS:
        if vector['indices'] is not None or not (vector['label'].startswith(('exception ','predicate before exception ')) or vector['label']=='negated all before exception'):
            continue
        args=copy.deepcopy(body['args']);args['selection']=vector['selection']
        excluded={index for ref in vector['selection']['excluded'] for index in range(ref['first'],ref['last']+1)}
        # Fabricated derived items cannot bypass independent whole-owner validation.
        args['items']=[{'index':index,'action':'resolve','correction':vector['selection']['correction']} for index in range(1,46) if index not in excluded]
        try:
            result=call(request(args,event=body['event_id'],text=vector['selection']['correction']),context)
        except ValueError:
            pass
        else:
            assert result['status']=='rejected' and result['code']=='owner_span_unavailable', vector['label']
        assert before=={path:path.read_bytes() for path in before}, vector['label']
    assert all(row['status']=='open' for row in rows())


@pytest.mark.parametrize('label',['exception trailing unreferenced number','exception trailing unreferenced range','exception trailing unreferenced hash range','exception trailing unreferenced colon','exception trailing unreferenced slash'])
def test_exception_tail_rejects_unreferenced_numbers_before_completion_matching(label):
    selection=next(vector['selection'] for vector in VECTORS if vector['label']==label)
    with pytest.raises(ValueError,match='unreferenced numeric'):
        ke.cleanup_selection_items(full_list(),selection,owner_text=selection['correction'],completion_matches=lambda *_:True)


@pytest.mark.parametrize('label',['exception empty excluded','explicit empty references'])
def test_empty_numbered_spans_fail_before_completion_matching(label):
    selection=next(vector['selection'] for vector in VECTORS if vector['label']==label)
    with pytest.raises(ValueError,match='references invalid'):
        ke.cleanup_selection_items(full_list(),selection,owner_text=selection['correction'],completion_matches=lambda *_:True)


@pytest.mark.parametrize('case',['source','target','forged-derived','owner-text','duplicate-consumption','numeric-carveout'])
def test_receiver_typed_selection_current_checks(context,case):
    body=prepared(context,VECTORS[0]['selection'])
    if case=='source':
        context[1]['_source_authority']=lambda:{'complete':True,'revision':'b'*64,'google_sources':[]}
    if case=='target':
        row=next(row for row in rows() if row['anchor_key']==body['args']['list']['rows'][0]['anchor_key'])
        row['summary']='Changed since display';ke._load_continuity()._persist(row)
    if case=='numeric-carveout':
        vector=next(vector for vector in VECTORS if vector['label']=='review multi-sentence carve-out 1')
        changed=copy.deepcopy(body['args']);changed['selection']=vector['selection']
        changed['items']=[{'index':index,'action':'resolve','correction':vector['selection']['correction']} for index in range(1,11)]
        before={path:path.read_bytes() for path in Path(ke._load_continuity()._dir()).glob('*.md')}
        with pytest.raises(ValueError,match='unreferenced numeric'):
            call(request(changed,event=body['event_id'],text=vector['selection']['correction']),context)
        assert before=={path:path.read_bytes() for path in before}
        assert all(row['status']=='open' for row in rows())
        return
    if case=='forged-derived':
        changed=copy.deepcopy(body['args']);changed['items']=changed['items'][:-1]
        with pytest.raises(ValueError):
            call(request(changed,event=body['event_id'],text=body['text']),context)
        assert all(row['status']=='open' for row in rows());return
    if case=='owner-text':
        body=request(body['args'],event=body['event_id'],text='The email says '+body['text'])
    if case=='duplicate-consumption':
        assert call(body,context)['outcome']=='complete'
        result=call(request(body['args'],event='e'*64,text=body['text']),context)
        assert result['status']=='rejected' and result['code']=='event_conflict';return
    result=call(body,context)
    if case=='target':
        assert result['outcome']=='partial' and result['items'][0]['status']=='refused'
    else:
        assert result['status']=='rejected'
        assert all(row['status']=='open' for row in rows())


@pytest.mark.parametrize('failed_write',[1,2])
def test_typed_selector_unknown_effect_recovery_never_replays(context,monkeypatch,failed_write):
    body=prepared(context,VECTORS[0]['selection'])
    cr=ke._load_continuity();original=cr._persist;count=[0]
    def lose(row,**kwargs):
        count[0]+=1;original(row,**kwargs)
        if count[0]==failed_write:
            raise OSError('synthetic loss after canonical write')
    monkeypatch.setattr(cr,'_persist',lose)
    receipt=call(body,context)
    statuses=[item['status'] for item in receipt['items']]
    assert statuses==['applied']*(failed_write-1)+['unknown']+['skipped']*(43-failed_write)
    monkeypatch.setattr(cr,'_persist',lambda *a,**k:pytest.fail('inspection never repeats a canonical write'))
    assert call(dict(body,operation='inspect'),context)==receipt
    assert call(body,context)==receipt


@pytest.mark.parametrize('vector',[vector for vector in VECTORS if vector['label'].startswith('review multi-sentence')])
def test_exact_completion_flag_survives_sentence_recursion(vector):
    selection=vector['selection'];references=selection['clauses'][0]['references']
    assert not tool.resolve_many_names_match(selection['correction'],
        [{'name':ref['correction'],'correction':ref['correction']} for ref in references],
        'owner_tasks',exact_clause=True)
    with pytest.raises(ValueError,match='unreferenced numeric'):
        ke.cleanup_selection_items(full_list(),selection,owner_text=selection['correction'],completion_matches=lambda *_:True)


@pytest.mark.parametrize('correction',['All done except 1','All except 1 are done.'])
def test_requested_subset_all_except_only_touches_displayed_complement(context,correction):
    from test_runtime_task_list_subset import subset
    anchors,proposal,preview=subset(context)
    review=preview['review']
    assert review['total']==4 and review['remaining']==1 and len(review['rows'])==3
    selection={'mode':'all_except','correction':correction,'excluded':[{'first':1,'last':1,'correction':'1'}]}
    args={'operation':'cleanup','list':review,'selection':selection,'items':ke.cleanup_selection_items(review,selection),
        'proposal':{'event_id':proposal['event_id'],'action_id':proposal['action_id'],
            'args_hash':tool._identity(proposal['event_id'],proposal['args'])[1],
            'accepted_dispatch_id':'accepted-subset','source_revision':'a'*64}}
    before={path:path.read_bytes() for path in Path(ke._load_continuity()._dir()).glob('*.md')}
    receipt=call(request(args,event='f'*64,text=selection['correction']),context)
    assert receipt['outcome']=='complete' and [item['index'] for item in receipt['items']]==[2,3]
    assert {row['anchor_key']:row['status'] for row in rows()}=={
        anchors[0]:'resolved',anchors[1]:'resolved',anchors[2]:'open',anchors[3]:'open'}
    for anchor in (anchors[2],anchors[3]):
        path=next(path for path,raw in before.items() if ('anchor_key: '+anchor+'\n').encode() in raw)
        assert path.read_bytes()==before[path]


def test_subset_anchor_keys_are_schema_checked_hashed_and_bound_to_request():
    review=full_list();keyed=copy.deepcopy(review);keyed['anchor_keys']=[row['anchor_key'] for row in keyed['rows']]
    keyed['review_hash']=ke._cleanup_hash({k:v for k,v in keyed.items() if k!='review_hash'})
    assert keyed['review_hash']!=review['review_hash']
    with pytest.raises(ValueError):
        ke.validate_cleanup_list({**review,'anchor_keys':None})
    original={'operation':'propose_cleanup','scope':'open','source_revision':'a'*64}
    assert tool._identity('a'*64,original)!=tool._identity('a'*64,{**original,'anchor_keys':keyed['anchor_keys']})


LEGACY_VECTORS=json.loads((Path(__file__).resolve().parents[1]/'sotto-chat/test/fixtures/legacy-named-completion-vectors.json').read_text())


@pytest.mark.parametrize('vector',LEGACY_VECTORS)
def test_legacy_named_punctuation_parity(vector):
    assert tool.resolve_many_names_match(vector['text'],[{'name':name,'correction':name} for name in vector['names']],'owner_tasks') is vector['expected']


def test_existing_c0_admission_refuses_before_receiver_effect(context):
    suffix='\u001c'
    from test_runtime_task_list_subset import subset
    _,proposal,preview=subset(context);review=preview['review']
    selection={'mode':'all_except','correction':'All done except 1','excluded':[{'first':1,'last':1,'correction':'1'}]}
    args={'operation':'cleanup','list':review,'selection':selection,'items':ke.cleanup_selection_items(review,selection),
        'proposal':{'event_id':proposal['event_id'],'action_id':proposal['action_id'],
            'args_hash':tool._identity(proposal['event_id'],proposal['args'])[1],
            'accepted_dispatch_id':'accepted-subset','source_revision':'a'*64}}
    body=request(args,event='f'*64,text=selection['correction']+suffix)
    before={path:path.read_bytes() for path in Path(ke._load_continuity()._dir()).glob('*.md')}
    with pytest.raises(ValueError,match='text is invalid'):
        call(body,context)
    assert before=={path:path.read_bytes() for path in before}


def prepared_singleton(context):
    from test_runtime_task_list_subset import subset
    anchors,_,_=subset(context)
    proposal=request({'operation':'propose_cleanup','scope':'open','source_revision':'a'*64,'anchor_keys':[anchors[1]]},
        event='b'*64,operation='preview',text='What remains of those three tasks?')
    preview=call(proposal,context);review=preview['review']
    selection={'mode':'singleton','correction':'That one is done too.','reference':'That one'}
    args={'operation':'cleanup','list':review,'selection':selection,'items':ke.cleanup_selection_items(review,selection,derive_only=True),
        'proposal':{'event_id':proposal['event_id'],'action_id':proposal['action_id'],
            'args_hash':tool._identity(proposal['event_id'],proposal['args'])[1],
            'accepted_dispatch_id':'accepted-singleton','source_revision':'a'*64}}
    return anchors,request(args,event='f'*64,text=selection['correction'])


@pytest.mark.parametrize('changed',[None,'title','identity'])
def test_actual_receiver_singleton_continuation_retains_current_target_proofs(context,changed):
    anchors,body=prepared_singleton(context)
    if changed:
        row=next(row for row in rows() if row['anchor_key']==anchors[1])
        row['summary' if changed=='title' else 'source_message_id']='Changed after display'
        ke._load_continuity()._persist(row)
    before={path:path.read_bytes() for path in Path(ke._load_continuity()._dir()).glob('*.md')}
    receipt=call(body,context)
    if changed:
        assert receipt['outcome']=='partial' and receipt['items'][0]['status']=='refused'
        assert before=={path:path.read_bytes() for path in before}
        assert all(row['status']=='open' for row in rows())
    else:
        assert receipt['outcome']=='complete' and receipt['items'][0]['anchor_key']==anchors[1]
        assert {row['anchor_key']:row['status'] for row in rows()}=={anchor:('resolved' if anchor==anchors[1] else 'open') for anchor in anchors}
        assert call(body,context)==receipt


@pytest.mark.parametrize('case',['mismatched-owner','negative','isnt','not-yet','question','conditional','future','reported','thanks','extra-completion','unicode'])
def test_actual_receiver_singleton_refuses_unadmitted_or_noncompletion_text(context,case):
    _,body=prepared_singleton(context)
    text,reference={
        'mismatched-owner':('Thanks','That one'),
        'negative':('That one is not done.','That one'),
        'isnt':("That one isn't done.",'That one'),
        'not-yet':('That one is not yet done.','That one'),
        'question':('Is that one done?','that one'),
        'conditional':('Close it once Dana replies.','it'),
        'future':("I'll finish it tomorrow.",'it'),
        'reported':('Dana says it is done.','it'),
        'thanks':('Thanks for that one.','that one'),
        'extra-completion':('That one is done. Dana is done too.','That one'),
        'unicode':('ıt is done.','ıt'),
    }[case]
    args=copy.deepcopy(body['args'])
    if case!='mismatched-owner':
        args['selection']={'mode':'singleton','correction':text,'reference':reference}
        # A hostile caller can fabricate derived items; receiver admission still checks intent.
        args['items']=[{'index':1,'action':'resolve','correction':text}]
    before={path:path.read_bytes() for path in Path(ke._load_continuity()._dir()).glob('*.md')}
    try:
        result=call(request(args,event=body['event_id'],text=text),context)
    except ValueError:
        assert case=='unicode', 'only malformed reference shape fails before admission'
    else:
        assert result['status']=='rejected' and result['code']=='owner_span_unavailable'
    assert before=={path:path.read_bytes() for path in before}
    assert all(row['status']=='open' for row in rows())


def test_singleton_derivation_requires_explicit_shape_only_opt_in_or_actual_owner_authority():
    review=full_list();review.update(next(vector['review'] for vector in VECTORS if vector['label']=='singleton long s'))
    review['review_hash']=ke._cleanup_hash({k:v for k,v in review.items() if k!='review_hash'})
    selection={'mode':'singleton','correction':'Thanks for that one.','reference':'that one'}
    with pytest.raises(ValueError,match='admitted owner text'):
        ke.cleanup_selection_items(review,selection)
    assert ke.cleanup_selection_items(review,selection,derive_only=True)==[{'index':1,'action':'resolve','correction':selection['correction']}]
    with pytest.raises(ValueError,match='shape-only'):
        ke.cleanup_selection_items(review,selection,derive_only=True,owner_text=selection['correction'])
    with pytest.raises(ValueError,match='completion unavailable'):
        ke.cleanup_selection_items(review,selection,owner_text=selection['correction'],completion_matches=lambda text,items:tool.resolve_many_names_match(text,items,'owner_tasks',exact_clause=True))
    assert type(tool.resolve_many_names_match('That one is done.',[{'name':'That one','correction':'That one'}],'owner_tasks',exact_clause=True)) is bool
    assert type(tool.resolve_many_names_match('Thanks for that one.',[{'name':'that one','correction':'that one'}],'owner_tasks',exact_clause=True)) is bool


@pytest.mark.parametrize('text,reference', [('Close all of them', 'all of them'), ('all of this is done', 'all of this'), ('all of that is done', 'all of that')])
def test_requested_subset_all_completion_never_resolves_unshown_task(context, text, reference):
    from test_runtime_task_list_subset import subset
    anchors, proposal, preview = subset(context)
    review = preview['review']
    selection = {'mode': 'all', 'correction': text, 'reference': reference}
    args = {'operation': 'cleanup', 'list': review, 'selection': selection,
            'items': ke.cleanup_selection_items(review, selection, derive_only=True),
            'proposal': {'event_id': proposal['event_id'], 'action_id': proposal['action_id'],
                         'args_hash': tool._identity(proposal['event_id'], proposal['args'])[1],
                         'accepted_dispatch_id': 'accepted-subset', 'source_revision': 'a'*64}}
    receipt = call(request(args, event='f'*64, text=selection['correction']), context)
    assert receipt['outcome'] == 'complete'
    assert [item['index'] for item in receipt['items']] == [1, 2, 3]
    assert {row['anchor_key']: row['status'] for row in rows()} == {
        anchors[0]: 'resolved', anchors[1]: 'resolved', anchors[2]: 'resolved', anchors[3]: 'open'}


@pytest.mark.parametrize('text', ['1-3', 'All? 1–3?'])
def test_actual_receiver_short_numeric_done_answer_preserves_unshown_task(context, text):
    from test_runtime_task_list_subset import subset
    anchors, proposal, preview = subset(context)
    review = preview['review']
    token = '1–3' if 'All' in text else '1-3'
    selection = {'mode': 'explicit', 'correction': text, 'clauses': [
        {'action': 'resolve', 'correction': text, 'references': [{'first': 1, 'last': 3, 'correction': token}]}]}
    args = {'operation': 'cleanup', 'list': review, 'selection': selection,
            'items': ke.cleanup_selection_items(review, selection, derive_only=True),
            'proposal': {'event_id': proposal['event_id'], 'action_id': proposal['action_id'],
                         'args_hash': tool._identity(proposal['event_id'], proposal['args'])[1],
                         'accepted_dispatch_id': 'accepted-subset', 'source_revision': 'a'*64}}
    receipt = call(request(args, event='f'*64, text=text), context)
    assert receipt['outcome'] == 'complete'
    assert [item['index'] for item in receipt['items']] == [1, 2, 3]
    assert {row['anchor_key']: row['status'] for row in rows()} == {
        anchors[0]: 'resolved', anchors[1]: 'resolved', anchors[2]: 'resolved', anchors[3]: 'open'}


@pytest.mark.parametrize('text', [
    'all of this is not done', 'all of this might be done',
    'all of this is done, cancel', 'all of this project is done',
])
def test_collective_demonstrative_rejects_incomplete_owner_completion(text):
    selection = {'mode': 'all', 'correction': text, 'reference': 'all of this'}
    with pytest.raises(ValueError, match='explicit all completion unavailable'):
        ke.cleanup_selection_items(full_list(), selection, owner_text=text,
            completion_matches=lambda claim, items: tool.resolve_many_names_match(
                claim, items, 'owner_tasks', exact_clause=True))


@pytest.mark.parametrize('case', ['done', 'changed-shown', 'source', 'bare', 'them', 'negative', 'uncertain', 'cancel', 'project'])
def test_default_page_demonstrative_completion_never_selects_hidden_task(context, case):
    context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'a'*64, 'google_sources': []}
    for index in range(1, 52):
        text = f'Temporary page task {index:02d}'
        call(request({'operation': 'add', 'text': text}, event=f'{index:064x}', text='Add '+text), context)
    proposal = request({'operation': 'propose_cleanup', 'scope': 'open', 'source_revision': 'a'*64},
                       event='a'*64, operation='preview', text='Give me all the open items?')
    review = call(proposal, context)['review']
    assert len(review['rows']) == 50 and review['remaining'] == 1 and 'anchor_keys' not in review
    shown = {row['anchor_key'] for row in review['rows']}
    hidden = next(row for row in rows() if row['anchor_key'] not in shown)
    hidden['summary'] = 'Hidden row changed after display'
    ke._load_continuity()._persist(hidden)
    reference = 'all' if case == 'bare' else 'all of them' if case == 'them' else 'all of this'
    claim = {'negative': 'all of this is not done', 'uncertain': 'all of this might be done',
             'cancel': 'all of this is done, cancel', 'project': 'all of this project is done',
             'them': 'Close all of them'}.get(case, reference+' is done')
    selection = {'mode': 'all', 'correction': claim, 'reference': reference}
    args = {'operation': 'cleanup', 'list': review, 'selection': selection,
            'items': [{'index': row['index'], 'action': 'resolve', 'correction': claim} for row in review['rows']],
            'proposal': {'event_id': proposal['event_id'], 'action_id': proposal['action_id'],
                         'args_hash': tool._identity(proposal['event_id'], proposal['args'])[1],
                         'accepted_dispatch_id': 'accepted-default-page', 'source_revision': 'a'*64}}
    if case in ('bare', 'negative', 'uncertain', 'cancel', 'project'):
        before = {path: path.read_bytes() for path in Path(ke._load_continuity()._dir()).glob('*.md')}
        if case == 'bare':
            with pytest.raises(ValueError, match='complete displayed open list'):
                call(request(args, event='f'*64, text=claim), context)
        else:
            result = call(request(args, event='f'*64, text=claim), context)
            assert result['status'] == 'rejected' and result['code'] == 'owner_span_unavailable'
        assert before == {path: path.read_bytes() for path in before}
        assert all(row['status'] == 'open' for row in rows())
        return
    if case == 'changed-shown':
        target = next(row for row in rows() if row['anchor_key'] in shown)
        target['summary'] = 'Shown task changed after display'
        ke._load_continuity()._persist(target)
    if case == 'source':
        context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'b'*64, 'google_sources': []}
    result = call(request(args, event='f'*64, text=selection['correction']), context)
    current = {row['anchor_key']: row['status'] for row in rows()}
    assert current[hidden['anchor_key']] == 'open'
    if case == 'source':
        assert result['status'] == 'rejected' and all(status == 'open' for status in current.values())
    elif case == 'changed-shown':
        assert result['outcome'] == 'partial' and current[target['anchor_key']] == 'open'
        assert sum(status == 'resolved' for status in current.values()) == 49
    else:
        assert result['outcome'] == 'complete'
        assert {key for key, status in current.items() if status == 'resolved'} == shown
