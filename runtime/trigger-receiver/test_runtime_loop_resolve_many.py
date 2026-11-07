"""Explicit multiple closures reuse the cleanup writer's durable per-item boundary."""
from test_runtime_loop_cleanup import prepare_cleanup, rows, call, request, ke
from delivery_effects import loop_version, loop_identity
import pytest
import runtime_owner_loop_tool as tool

pytest_plugins=['test_runtime_loop_cleanup']
pytestmark=pytest.mark.usefixtures('actual_writer_module')


def resolve_request(context, names=('Person 1', 'Person 2', 'Person 3')):
    prepare_cleanup(context)
    current={row['contact_name']:row for row in rows()}
    items=[{'name':name,'anchor_key':current[name]['anchor_key'],
            'loop_version':loop_version(current[name]),'loop_identity':loop_identity(current[name]),
            'correction':name} for name in names]
    args={'operation':'resolve_many','source_revision':'a'*64,'selection_mode':'named_people','items':items}
    return request(args,event='e'*64,text=', '.join(names)+' loops are closed. What else is open?')


def test_unique_first_names_bind_full_current_contact_names(context):
    prepare_cleanup(context)
    cr=ke._load_continuity()
    names=['Tina Jiang','Shimolee Kothari','Hari Raghavan']
    selected=[]
    for index,name in enumerate(names,1):
        row=next(r for r in rows() if r['anchor_key']==f'cleanup-{index}')
        row['contact_name']=name
        cr._persist(row)
        current=next(r for r in rows() if r['anchor_key']==row['anchor_key'])
        word=name.split()[0] if index!=2 else 'shimolee'
        selected.append({'name':word,'anchor_key':current['anchor_key'],
            'loop_version':loop_version(current),'loop_identity':loop_identity(current),
            'correction':word})
    body=request({'operation':'resolve_many','source_revision':'a'*64,'selection_mode':'named_people','items':selected},event='e'*64,
        text='Tina shimolee Hari loops are closed. What else is open?')
    assert call(body,context)['outcome']=='complete'
    assert all(row['status']=='resolved' for row in rows() if row['contact_name'] in names)


@pytest.mark.parametrize('separator,question',[('.', 'What else is open?'),('.', 'what’s left?'),('.', 'what is left?'),('.', 'what remains?'),('.', 'any Sotto pilot test tasks left?'),('.', 'any onboarding tasks left?'),(' —', 'what’s left?')])
def test_natural_fragment_with_named_people_resolves(context,separator,question):
    body=resolve_request(context,('Person 1','Person 2'))
    body=request(body['args'],event=body['event_id'],text='Person 1 and Person 2 are sorted'+separator+' '+question)
    assert call(body,context)['outcome']=='complete'
    assert [row['status'] for row in rows() if row['contact_name'] in ('Person 1','Person 2')]==['resolved','resolved']


def test_readback_question_does_not_override_an_explicitly_open_peer(context):
    body=resolve_request(context,('Person 1','Person 2'))
    body=request(body['args'],event=body['event_id'],text='Person 1 done. Person 2 is still open. what’s left?')
    assert call(body,context)['code']=='owner_span_unavailable'
    assert all(row['status']=='open' for row in rows())


def test_exact_owner_task_titles_close_two_and_leave_third_open(context):
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    names=['Review acceptance A','Review acceptance B','Review acceptance C']
    for index,name in enumerate(names,1):
        assert call(request({'operation':'add','text':name},event=str(index)*64,text='Add '+name),context)['status']=='committed'
    current={row['summary']:row for row in rows()}
    selected=[{'name':name,'anchor_key':current[name]['anchor_key'],
        'loop_version':loop_version(current[name]),'loop_identity':loop_identity(current[name]),
        'correction':name} for name in names[:2]]
    body=request({'operation':'resolve_many','source_revision':'a'*64,'selection_mode':'owner_tasks','items':selected},event='e'*64,
        text='I finished Review acceptance A and Review acceptance B; leave Review acceptance C open')
    receipt=call(body,context)
    assert receipt['outcome']=='complete'
    status={row['summary']:row['status'] for row in rows()}
    assert status==dict(zip(names,['resolved','resolved','open'],strict=True))


def test_contradictory_leave_open_target_refuses_all(context):
    body=resolve_request(context,('Person 1','Person 2'))
    body=request(body['args'],event=body['event_id'],text='I finished Person 1 and Person 2; leave Person 1 open')
    assert call(body,context)['code']=='owner_span_unavailable'
    assert all(row['status']=='open' for row in rows())


def test_unique_owner_task_fragments_bind_full_saved_titles(context):
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    titles=['Review Sotto acceptance 11A checklist','Review Sotto acceptance 11B checklist','Review Sotto acceptance 11C checklist']
    for index,title in enumerate(titles,1):
        assert call(request({'operation':'add','text':title},event=str(index)*64,text='Add '+title),context)['status']=='committed'
    current={row['summary']:row for row in rows()}
    names=['Sotto acceptance 11A','Sotto acceptance 11B']
    items=[{'name':name,'anchor_key':current[title]['anchor_key'],
        'loop_version':loop_version(current[title]),'loop_identity':loop_identity(current[title]),
        'correction':name} for name,title in zip(names,titles[:2],strict=True)]
    body=request({'operation':'resolve_many','source_revision':'a'*64,'selection_mode':'owner_tasks','items':items},event='e'*64,
        text='Sotto acceptance 11A and Sotto acceptance 11B done, Sotto acceptance 11C still open.')
    assert call(body,context)['outcome']=='complete'
    assert {row['summary']:row['status'] for row in rows()}==dict(zip(titles,['resolved','resolved','open'],strict=True))


def test_ambiguous_owner_task_fragment_changes_none(context):
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    titles=['Review Sotto acceptance 11A checklist','Review Sotto acceptance 11A notes','Review Sotto acceptance 11B checklist']
    for index,title in enumerate(titles,1):
        assert call(request({'operation':'add','text':title},event=str(index)*64,text='Add '+title),context)['status']=='committed'
    current={row['summary']:row for row in rows()}
    items=[{'name':name,'anchor_key':current[title]['anchor_key'],
        'loop_version':loop_version(current[title]),'loop_identity':loop_identity(current[title]),
        'correction':name} for name,title in zip(['Sotto acceptance 11A','Sotto acceptance 11B'],[titles[0],titles[2]],strict=True)]
    body=request({'operation':'resolve_many','source_revision':'a'*64,'selection_mode':'owner_tasks','items':items},event='e'*64,
        text='Finished Sotto acceptance 11A and Sotto acceptance 11B.')
    assert call(body,context)['code']=='target_unavailable'
    assert all(row['status']=='open' for row in rows())


def test_resolves_three_exact_rows_once_with_readable_receipt(context):
    body=resolve_request(context)
    receipt=call(body,context)
    assert receipt['operation']=='resolve_many' and receipt['outcome']=='complete'
    assert [item['status'] for item in receipt['items']]==['applied']*3
    assert [row['status'] for row in rows() if row['contact_name'] in ('Person 1','Person 2','Person 3')]==['resolved']*3
    assert next(row for row in rows() if row['contact_name']=='Person 4')['status']=='open'
    assert call(dict(body,operation='inspect'),context)==receipt
    assert call(body,context)==receipt


@pytest.mark.parametrize('text',[
    "Don't close Person 1, Person 2, Person 3 loops",
    'Should Person 1, Person 2, Person 3 loops be closed?',
    'Person 1, Person 2, Person 3 loops might be closed.',
    'Person 1, Person 2 loops are closed. What else is open?',
    'Person 1 is done. Person 2 is pending. Person 3 is open.',
    'Person 1 is done; remind me to call Person 2 and Person 3.',
])
def test_unclear_or_partial_owner_clause_refuses_without_write(context,text):
    body=resolve_request(context)
    body=request(body['args'],event=body['event_id'],text=text)
    assert call(body,context)=={'version':1,'status':'rejected','code':'owner_span_unavailable'}
    assert all(row['status']=='open' for row in rows())


def test_revocation_refuses_every_item_before_contact(context):
    body=resolve_request(context)
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'b'*64,'google_sources':['gmail']}
    assert call(body,context)['code']=='source_unavailable'
    assert all(row['status']=='open' for row in rows())


def test_ambiguous_first_name_preflight_changes_none(context):
    body=resolve_request(context)
    cr=ke._load_continuity()
    first=next(row for row in rows() if row['anchor_key']=='cleanup-1')
    first['contact_name']='Tina Jiang'
    cr._persist(first)
    other=next(row for row in rows() if row['anchor_key']=='cleanup-4')
    other['contact_name']='Tina Lee'
    cr._persist(other)
    current=next(row for row in rows() if row['anchor_key']=='cleanup-1')
    body['args']['items'][0].update(name='Tina',correction='Tina',
        loop_version=loop_version(current),loop_identity=loop_identity(current))
    body=request(body['args'],event=body['event_id'],text='Tina, Person 2, Person 3 loops are closed. What else is open?')
    assert call(body,context)['code']=='target_unavailable'
    assert all(row['status']=='open' for row in rows())


def test_lost_first_write_stays_unknown_and_later_rows_skipped(context,monkeypatch):
    body=resolve_request(context)
    cr=ke._load_continuity()
    original=cr._persist
    count=[0]
    def lose(row,**kwargs):
        count[0]+=1
        original(row,**kwargs)
        if count[0]==1:
            raise OSError('lost after canonical write')
    monkeypatch.setattr(cr,'_persist',lose)
    receipt=call(body,context)
    assert [item['status'] for item in receipt['items']]==['unknown','skipped','skipped']
    monkeypatch.setattr(cr,'_persist',lambda *a,**k:pytest.fail('inspect must not write'))
    assert call(dict(body,operation='inspect'),context)==receipt


def test_single_close_cannot_consume_multi_completion_event(context):
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    titles=['Review alpha checklist','Review beta checklist','Review gamma checklist']
    for index,title in enumerate(titles,1):
        assert call(request({'operation':'add','text':title},event=str(index)*64,text='Add '+title),context)['status']=='committed'
    current={row['summary']:row for row in rows()}
    text='alpha and beta done, gamma still open'
    wrong={'operation':'close','anchor':current[titles[0]]['anchor_key'],'correction':'alpha'}
    assert call(request(wrong,event='e'*64,text=text),context)['code']=='owner_span_unavailable'
    assert all(row['status']=='open' for row in rows())
    items=[{'name':name,'correction':name,'anchor_key':current[title]['anchor_key'],
            'loop_version':loop_version(current[title]),'loop_identity':loop_identity(current[title])}
           for name,title in zip(['alpha','beta'],titles[:2],strict=True)]
    body=request({'operation':'resolve_many','selection_mode':'owner_tasks','source_revision':'a'*64,'items':items},event='e'*64,text=text)
    assert call(body,context)['outcome']=='complete'
    assert {row['summary']:row['status'] for row in rows()}==dict(zip(titles,['resolved','resolved','open'],strict=True))


@pytest.mark.parametrize('text',['alpha done','alpha done, beta still open','alpha done beta still open','alpha is done too','alpha is done now','please close alpha','close alpha','mark alpha done','please mark alpha as complete','alpha is complete','I have completed alpha','please close alpha, beta still open','mark alpha done; leave beta open','alpha is complete, beta still open','I have completed alpha; leave beta open'])
def test_single_close_exact_selection_preserves_open_peer(context,text):
    for index,title in enumerate(['Review alpha checklist','Review beta checklist'],1):
        assert call(request({'operation':'add','text':title},event=str(index)*64,text='Add '+title),context)['status']=='committed'
    alpha=next(row for row in rows() if row['summary']=='Review alpha checklist')
    body=request({'operation':'close','anchor':alpha['anchor_key'],'correction':'alpha'},event='e'*64,text=text)
    assert call(body,context)['loop_status']=='resolved'
    assert next(row for row in rows() if row['summary']=='Review beta checklist')['status']=='open'


def test_single_close_wrong_or_ambiguous_title_refuses_without_event_consumption(context):
    for index,title in enumerate(['Review alpha checklist','Review alpha notes','Review beta checklist'],1):
        assert call(request({'operation':'add','text':title},event=str(index)*64,text='Add '+title),context)['status']=='committed'
    current={row['summary']:row for row in rows()}
    for phrase in ['alpha','beta']:
        body=request({'operation':'close','anchor':current['Review alpha checklist']['anchor_key'],'correction':phrase},event='e'*64,text=phrase+' done')
        assert call(body,context)['code']=='target_unavailable'
    assert all(row['status']=='open' for row in rows())


@pytest.mark.parametrize('text',['gamma is not done too','gamma is not done now'])
def test_conversational_completion_suffix_cannot_override_negation(context,text):
    title='Review gamma checklist'
    added=call(request({'operation':'add','text':title},event='1'*64,text='Add '+title),context)
    body=request({'operation':'close','anchor':added['anchor_key'],'correction':'gamma'},event='e'*64,text=text)
    assert call(body,context)['code']=='owner_span_unavailable'
    assert rows()[0]['status']=='open'


@pytest.mark.parametrize('text',["don't close alpha", 'please do not close alpha', 'mark alpha not done', 'alpha is not complete', 'I have not completed alpha', 'please close alpha?', 'please close alpha but alpha still open', 'please close alpha and beta still open', 'mark alpha done and call beta', 'I have completed alpha if beta agrees', 'please close alpha and beta', 'mark alpha and beta done', 'alpha and beta are complete', 'I have completed alpha and beta',
    "I finished alpha, actually don't, keep it open", 'alpha done; wait, leave it open'])
def test_explicit_completion_cannot_drop_peer_or_override_negation(context,text):
    title='Review alpha checklist'
    added=call(request({'operation':'add','text':title},event='1'*64,text='Add '+title),context)
    body=request({'operation':'close','anchor':added['anchor_key'],'correction':'alpha'},event='e'*64,text=text)
    assert call(body,context)['code']=='owner_span_unavailable'
    assert rows()[0]['status']=='open'


@pytest.mark.parametrize('text',['alpha and beta are done, actually leave them open',"alpha and beta are done, but I'm not sure, so keep both open",
    'alpha and beta are done; never mind, everything stays open','alpha and beta are done, just kidding, leave all open'])
def test_hedged_or_reversed_carve_out_withdraws_the_claim(text):
    items=[{'name':name,'correction':name} for name in ('alpha','beta')]
    assert tool.resolve_many_names_match(text,items,'owner_tasks') is False
    assert tool.resolve_many_names_match('alpha and beta are done; leave gamma open',items,'owner_tasks') is True


@pytest.mark.parametrize('text',['please close Person 1 and Person 2','mark Person 1 and Person 2 done',
    'Person 1 and Person 2 are complete','I have completed Person 1 and Person 2'])
def test_explicit_human_multi_completion_resolves_every_selected_item(context,text):
    body=resolve_request(context,('Person 1','Person 2'))
    body=request(body['args'],event=body['event_id'],text=text+'; leave Person 3 open')
    assert call(body,context)['outcome']=='complete'
    assert {r['contact_name']:r['status'] for r in rows()}=={'Person 1':'resolved','Person 2':'resolved','Person 3':'open','Person 4':'open'}


@pytest.mark.parametrize('suffix',['any test tasks left?','anything else open?','what else?'])
def test_remaining_task_question_preserves_exact_completion_clause(suffix):
    from runtime_owner_loop_tool import resolve_many_names_match
    selected=[{'name':name,'correction':name} for name in ('beta','gamma')]
    assert resolve_many_names_match('beta and gamma are done. '+suffix,selected,'owner_tasks')
    for clause in ['beta and gamma are not done','beta is done; gamma still open','beta and gamma might be done']:
        assert not resolve_many_names_match(clause+'. '+suffix,selected,'owner_tasks')


@pytest.mark.parametrize('variant',['valid','ascii','wrong-description','ambiguous-person','two-tasks','wrong-count','negation','revoked','changed'])
def test_possessive_descriptions_close_only_unique_current_tasks(context,variant):
    prepare_cleanup(context)
    cr=ke._load_continuity()
    fixtures=[('Tina Jiang','Discuss her product lead offer','Message Tina about the offer check-in'),
              ('Shimolee Kothari','Choose dates and locations for family photos','Pick a date and location')]
    selected=[]
    for index,(name,summary,ask) in enumerate(fixtures,1):
        row=next(r for r in rows() if r['anchor_key']==f'cleanup-{index}')
        row.update(contact_name=name,summary=summary,ask=ask);cr._persist(row)
        current=next(r for r in rows() if r['anchor_key']==row['anchor_key'])
        phrase=["Tina’s offer check-in","Shimolee’s family photo plans"][index-1]
        if variant=='ascii':
            phrase=phrase.replace('’',"'")
        if variant=='wrong-description' and index==1:
            phrase='Tina’s school application'
        selected.append(dict(name=phrase,correction=phrase,anchor_key=current['anchor_key'],loop_version=loop_version(current),loop_identity=loop_identity(current)))
    if variant in ('ambiguous-person','two-tasks'):
        row=next(r for r in rows() if r['anchor_key']=='cleanup-3')
        row.update(contact_name='Tina Smith' if variant=='ambiguous-person' else 'Tina Jiang',
                   summary='Message Tina about the offer check-in' if variant=='ambiguous-person' else 'Different task');cr._persist(row)
    if variant=='changed':
        row=next(r for r in rows() if r['anchor_key']=='cleanup-1');row['ask']='New changed ask';cr._persist(row)
    if variant=='revoked':
        context[1]['_source_authority']=lambda:{'complete':True,'revision':'b'*64,'google_sources':[]}
    text=' and '.join(item['name'] for item in selected)+' are both done. Close those two.'
    if variant=='wrong-count':
        text=text.replace('those two','those three')
    if variant=='negation':
        text=text.replace('are both done','are not done')
    body=request(dict(operation='resolve_many',source_revision='a'*64,selection_mode='named_people',items=selected),event='e'*64,text=text)
    result=call(body,context)
    if variant in ('valid','ascii','two-tasks'):
        assert result['outcome']=='complete'
        assert [r['status'] for r in rows() if r['anchor_key'] in ('cleanup-1','cleanup-2')]==['resolved','resolved']
        assert all(r['status']=='open' for r in rows() if r['anchor_key'] not in ('cleanup-1','cleanup-2'))
        assert call(body,context)==result
    else:
        assert result['status']=='rejected' or result.get('outcome')=='partial'
        assert all(r['status']=='open' for r in rows())


@pytest.mark.parametrize('origin',['source','refs'])
def test_legacy_imessage_reply_pair_uses_protected_query_and_original_writer(context,origin):
    import loops_query
    from test_runtime_owner_loop_close import WHEN
    cr=ke._load_continuity()
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'bridge_sources':['imessage'],'google_sources':[]}
    fixtures=[('Avery Stone','offer check-in','Avery texted about a product lead offer.', 'Message Avery to reschedule your check-in about the offer.'),
              ('Blair Reed','family photo plans','Blair sent a booking link for fall family photos.', 'Pick a date and location so Blair can complete the booking.')]
    for index,(name,desc,summary,ask) in enumerate(fixtures):
        row=dict(anchor_key=f'follow_up:cid:c_fixture{index}',canonical_id=f'c_fixture{index}',action_type='reply',channel='imessage',
          source='imessage' if origin=='source' else None,status='open',created_at='2026-10-04 07:15:00',summary=summary,ask=ask,
          contact_name=name,contact_identifier=f'+1555000000{index}',times_surfaced=15,deadline=None,meeting_time=None)
        if origin=='refs':
            row['source_refs']=[{'source':'imessage','id':f'fixture-message-{index}'}]
        cr._persist(row)
    projected=loops_query.query_permitted_rows(cr._data_root(),['imessage'],now=WHEN,include_ask=True)
    current={r['name']:r for r in projected['you_owe']}
    assert len(current)==2
    items=[]
    for name,desc,_,_ in fixtures:
        row=current[name];phrase=name.split()[0]+'’s '+desc
        items.append(dict(name=phrase,correction=phrase,anchor_key=row['anchor_key'],loop_version=row['loop_version'],loop_identity=row['loop_identity']))
    text=' and '.join(r['name'] for r in items)+' are both done. Close those two.'
    body=request(dict(operation='resolve_many',source_revision='a'*64,selection_mode='named_people',items=items),text=text)
    result=call(body,context)
    assert result.get('outcome')=='complete',result
    assert all(r['status']=='resolved' for r in rows())
    assert call(body,context)==result


@pytest.mark.parametrize('variant',['exact-task','bare-name','same-description','different-person','different-identifier','missing-identity'])
def test_description_selects_one_task_without_merging_person_identities(context,variant):
    prepare_cleanup(context)
    cr=ke._load_continuity()
    current={r['anchor_key']:r for r in rows()}
    for anchor,summary,ask in [('cleanup-1','Choose family photo dates','Pick a date and location'),
                               ('cleanup-3','Choose catering menu','Confirm catering')]:
        row=current[anchor]
        row.update(contact_name='Dana Reed',contact_identifier='dana@example.com',canonical_id='c_dana',summary=summary,ask=ask)
        if anchor=='cleanup-3':
            if variant=='same-description':
                row['summary']='Choose family photo dates'
            if variant=='different-person':
                row['canonical_id']='c_other'
            if variant=='different-identifier':
                row['contact_identifier']='other@example.com'
            if variant=='missing-identity':
                row.pop('canonical_id')
        cr._persist(row)
    current={r['anchor_key']:r for r in rows()}
    phrases=['Dana' if variant=='bare-name' else 'Dana’s family photo plans','Person 2']
    selected=[]
    for anchor,phrase in zip(['cleanup-1','cleanup-2'],phrases):
        row=current[anchor]
        selected.append(dict(name=phrase,correction=phrase,anchor_key=anchor,
            loop_version=loop_version(row),loop_identity=loop_identity(row)))
    body=request(dict(operation='resolve_many',source_revision='a'*64,selection_mode='named_people',items=selected),
        text=' and '.join(phrases)+' are both done. Close those two.')
    before={r['anchor_key']:r for r in rows()}
    result=call(body,context)
    after={r['anchor_key']:r for r in rows()}
    if variant in ('exact-task','different-person','different-identifier','missing-identity'):
        assert result['outcome']=='complete',result
        assert after['cleanup-1']['status']==after['cleanup-2']['status']=='resolved'
        assert after['cleanup-3']==before['cleanup-3'] and after['cleanup-4']==before['cleanup-4']
        assert call(body,context)==result
    else:
        assert result['status']=='rejected' or result.get('outcome')=='partial'
        assert after==before


@pytest.mark.parametrize('branch', ['missing-anchor', 'duplicate-anchor', 'identity-conflict',
                                   'description-selection', 'provenance-unavailable'])
def test_selection_failure_logs_only_fixed_branch_without_effects(context, monkeypatch, capsys, branch):
    from pathlib import Path
    import ledger_provenance
    body = resolve_request(context, ('Person 1', 'Person 2'))
    current = rows()
    private = 'PRIVATE-selector-proof-source-message'
    candidate = next(r for r in current if r['anchor_key'] == 'cleanup-1')
    candidate['private_fixture'] = private
    if branch == 'missing-anchor':
        body['args']['items'][0]['anchor_key'] = private
    elif branch == 'duplicate-anchor':
        current.append(dict(candidate))
    elif branch == 'identity-conflict':
        current[-1]['contact_name'] = candidate['contact_name']
    elif branch == 'description-selection':
        name = "Person 1's unavailable request"
        body['args']['items'][0].update(name=name, correction=name)
    else:
        def unavailable(_row):
            raise ValueError(private)
        monkeypatch.setattr(ledger_provenance, 'source_loop_sources', unavailable)
        monkeypatch.setattr(ke, 'classify_ledger_sources', unavailable)
    monkeypatch.setattr(ke, '_owner_close_rows', lambda _cr: current)
    text = ' and '.join(i['name'] for i in body['args']['items']) + ' are done.'
    body = request(body['args'], event=body['event_id'], text=text)
    before = {p.name: p.read_bytes() for p in Path(ke._load_continuity()._dir()).glob('*.md')}
    result = call(body, context)
    assert result == {'version': 1, 'status': 'rejected', 'code': 'target_unavailable'}
    captured = capsys.readouterr()
    expected = ('anchor-selection' if branch in ('missing-anchor', 'duplicate-anchor')
                else 'description-selection' if branch == 'identity-conflict' else branch)
    assert captured.err.strip() == '[owner_loop_target_refused] ' + expected
    assert captured.out == '' and private not in captured.err
    assert before == {p.name: p.read_bytes() for p in Path(ke._load_continuity()._dir()).glob('*.md')}
    log = (context[0] / 'logs' / 'compose_brief.log').read_text()
    assert private not in log and body['event_id'] not in log
    assert body['args']['items'][0]['name'] not in log


def test_selection_diagnostic_failure_does_not_change_refusal(context, monkeypatch):
    import sotto_log
    body = resolve_request(context, ('Person 1', 'Person 2'))
    body['args']['items'][0]['anchor_key'] = 'missing'
    body = request(body['args'], event=body['event_id'], text=body['text'])
    monkeypatch.setattr(sotto_log, 'diag', lambda _line: (_ for _ in ()).throw(OSError('private log failure')))
    before = rows()
    assert call(body, context) == {'version': 1, 'status': 'rejected', 'code': 'target_unavailable'}
    assert rows() == before


def test_successful_selection_and_receipt_inspection_emit_no_failure_label(context, capsys):
    body = resolve_request(context, ('Person 1', 'Person 2'))
    result = call(body, context)
    assert result['status'] == 'committed' and result['outcome'] == 'complete'
    assert call(dict(body, operation='inspect'), context) == result
    captured = capsys.readouterr()
    assert 'owner_loop_target_refused' not in captured.out + captured.err


@pytest.mark.parametrize('variant', ['different-people', 'missing-id', 'same-ask', 'hidden-matching',
                                    'wrong-anchor', 'wrong-name-proof', 'stale', 'revoked', 'empty-description', 'bare'])
def test_qualified_task_selection_checks_all_active_peers_and_exact_proof(context, variant):
    prepare_cleanup(context)
    cr = ke._load_continuity()
    for anchor, name, cid, identifier, summary in [
            ('cleanup-1', 'Dana Reed', 'c_a1b2c3', 'dana@example.com', 'Choose family photo dates'),
            ('cleanup-3', 'Dana Morris', 'c_d4e5f6', 'different@example.com', 'Choose catering menu')]:
        row = next(r for r in rows() if r['anchor_key'] == anchor)
        row.update(contact_name=name, canonical_id=cid, contact_identifier=identifier, summary=summary, ask=summary)
        if anchor == 'cleanup-3':
            if variant in ('same-ask', 'hidden-matching'):
                row.update(summary='Choose family photo dates', ask='Choose family photo dates')
            if variant == 'hidden-matching':
                row['snoozed_until'] = '2099-01-01'
            if variant == 'missing-id':
                row.pop('canonical_id')
        cr._persist(row)
    current = {r['anchor_key']: r for r in rows()}
    phrase = 'Dana' if variant == 'bare' else 'Dana’s tasks' if variant == 'empty-description' else 'Dana’s family photo plans'
    selected = []
    for anchor, name in [('cleanup-3' if variant == 'wrong-anchor' else 'cleanup-1', phrase), ('cleanup-2', 'Person 2')]:
        row = current[anchor]
        selected.append(dict(name=name, correction=name, anchor_key=anchor,
                             loop_version=loop_version(row), loop_identity=loop_identity(row)))
    if variant == 'wrong-name-proof':
        selected[0]['name'] = selected[0]['correction'] = 'Dana Morris’s family photo plans'
    body = request(dict(operation='resolve_many', source_revision='a'*64, selection_mode='named_people', items=selected),
                   text=' and '.join(r['name'] for r in selected) + ' are both done. Close those two.')
    if variant == 'stale':
        row = dict(current['cleanup-1'], summary='A changed family photo request')
        cr._persist(row)
    if variant == 'revoked':
        context[1]['_source_authority'] = lambda: dict(complete=True, revision='b'*64, google_sources=[])
    before = {r['anchor_key']: r for r in rows()}
    result = call(body, context)
    after = {r['anchor_key']: r for r in rows()}
    if variant in ('different-people', 'missing-id'):
        assert result.get('outcome') == 'complete', result
        assert after['cleanup-1']['status'] == after['cleanup-2']['status'] == 'resolved'
        assert after['cleanup-3'] == before['cleanup-3'] and after['cleanup-4'] == before['cleanup-4']
        for anchor in ('cleanup-1', 'cleanup-2'):
            for key in ('canonical_id', 'contact_identifier', 'contact_name'):
                assert after[anchor].get(key) == before[anchor].get(key)
        context[1]['_source_authority'] = lambda: dict(complete=False)
        assert call(dict(body, operation='inspect'), context) == result
        assert call(body, context) == result
    else:
        assert result['status'] == 'rejected' and after == before


@pytest.mark.parametrize('suffix', ['any Sotto pilot test tasks left?', 'what else is open?'])
def test_completion_question_before_readback_changes_nothing(context, suffix):
    body = resolve_request(context, ('Person 1', 'Person 2'))
    body = request(body['args'], event=body['event_id'], text='Person 1 and Person 2 done? ' + suffix)
    assert call(body, context)['code'] == 'owner_span_unavailable'
    assert all(row['status'] == 'open' for row in rows())
