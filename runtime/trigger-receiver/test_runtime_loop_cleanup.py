"""One exact owner cleanup intent preserves original transforms and durable partial truth."""
import pytest
import json
from pathlib import Path
from datetime import datetime, timezone

from test_runtime_owner_loop_tool import context, call, request, rows, ke
from test_runtime_source_loop_tool import source, actual_writer_module

__all__ = ['context', 'actual_writer_module']


def test_numbered_completion_shared_reference_and_ambiguity_vectors():
    from runtime_owner_loop_tool import cleanup_selections_match
    path = Path(__file__).resolve().parents[1] / 'sotto-chat/test/fixtures/numbered-completion-vectors.json'
    for vector in json.loads(path.read_text()):
        items = [{'index':index, 'action':'resolve',
                  'correction':vector.get('corrections', list(map(str, vector['indices'])))[i]}
                 for i,index in enumerate(vector['indices'])]
        assert cleanup_selections_match(vector['text'], items) is vector['expected'], vector['text']


def test_numbered_completion_bare_followup_closes_only_saved_choices(context):
    body = prepare_cleanup(context, [{'index':index, 'action':'resolve', 'correction':str(index)}
                                     for index in (1, 3)])
    before = {row['anchor_key']:row for row in rows()}
    body = request(body['args'], event=body['event_id'], text='1 and 3 are done. close them')
    result = call(body, context)
    assert result['outcome'] == 'complete'
    assert [item['status'] for item in result['items']] == ['applied', 'applied']
    after = {row['anchor_key']:row for row in rows()}
    selected = {item['anchor_key'] for item in result['items']}
    for anchor, row in after.items():
        if anchor in selected:
            assert row['status'] == 'resolved'
        else:
            assert row == before[anchor]
    assert call(dict(body, operation='inspect'), context) == result


@pytest.mark.parametrize('text', [
    '1 and 3 are done. Just kidding.',
    '1 and 3 are done. Ignore that.',
    '1 and 3 are done except the third.',
    '1 and 3 are done, except the third.',
    'Close 1 and 3 after Dana agrees.',
    'Close 1 and 3 once Dana agrees.',
])
def test_numbered_completion_retractions_carveouts_and_conditions_never_mutate(context, text):
    body = prepare_cleanup(context, [{'index':index, 'action':'resolve', 'correction':str(index)}
                                     for index in (1, 3)])
    before = {path:path.read_bytes() for path in context[0].rglob('*.md')}
    result = call(request(body['args'], event=body['event_id'], text=text), context)
    assert result['status'] == 'rejected' and result['code'] == 'owner_span_unavailable'
    assert {path:path.read_bytes() for path in context[0].rglob('*.md')} == before


def test_strict_original_dismiss_hook_preserves_source_and_rejects_other_edits(context):
    source(context)
    cr = ke._load_continuity()
    row = rows()[0]
    previous = dict(row)
    # Source rows with an authentic digest exercise the sanctioned refresh rather than a legacy save.
    from ledger_provenance import digest_frontmatter, classify_ledger_sources
    row['content_provenance'] = {'version':1, 'kind':'sources', 'sources':['gmail'],
        'digest':digest_frontmatter(row)}
    cr._persist(row)
    row = rows()[0]
    assert classify_ledger_sources(row)[1] == frozenset({'gmail'})
    instant = datetime(2026, 10, 6, 18, tzinfo=timezone.utc)
    bad = dict(row)
    cr._terminate(bad, 'dismissed', 'user_dismissed', '2026-10-06', now=instant)
    bad['summary'] = 'changed unrelated meaning'
    with pytest.raises(ValueError, match='unrelated'):
        cr._persist(bad, loop_dismiss={'today':'2026-10-06', 'now':instant.isoformat()})
    good = dict(row)
    cr._terminate(good, 'dismissed', 'user_dismissed', '2026-10-06', now=instant)
    cr._persist(good, loop_dismiss={'today':'2026-10-06', 'now':instant.isoformat()})
    post = rows()[0]
    assert post['status'] == 'dismissed' and post['created_at'] == previous['created_at']
    assert classify_ledger_sources(post)[1] == frozenset({'gmail'})


def prepare_cleanup(context, actions=None):
    cr=ke._load_continuity()
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':['gmail']}
    for index in range(1,5):
        row={'anchor_key':f'cleanup-{index}','action_type':'waiting_on' if index==1 else 'follow_up',
            'channel':'email','status':'open','created_at':'2026-09-01','summary':f'Obligation {index}',
            'contact_name':f'Person {index}','contact_identifier':f'p{index}@example.com','source':'gmail',
            'source_message_id':f'm{index}','source_thread_id':f't{index}'}
        if index==1:
            row.update(chased_count=2,chase_pending=True,handoff_asked_at='2026-09-20')
        cr._persist(row)
    proposal_body=request({'operation':'propose_cleanup','source_revision':'a'*64},event='a'*64,
        operation='preview',text='Show stale loops')
    preview=call(proposal_body,context)
    assert preview['status']=='preview'
    listed=preview['review']
    assert listed['rows'][0]['chased_out']
    items=actions or [{'index':1,'action':'keep','correction':'keep first'},
        {'index':2,'action':'dismiss','correction':'dismiss 2'},
        {'index':3,'action':'snooze','days':7,'correction':'snooze item 3 for 7 days'}]
    args={'operation':'cleanup','list':listed,'proposal':{'event_id':proposal_body['event_id'],
        'action_id':proposal_body['action_id'],'args_hash':tool_identity(proposal_body)[1],
        'accepted_dispatch_id':'accepted-original-cleanup-list','source_revision':'a'*64},'items':items}
    return request(args,event='e'*64,text='; '.join(i['correction'] for i in items))


def tool_identity(body):
    import runtime_owner_loop_tool
    return runtime_owner_loop_tool._identity(body['event_id'],body['args'])


def test_original_cleanup_all_transforms_and_exact_historical_receipt(context):
    body=prepare_cleanup(context)
    before={r['anchor_key']:r for r in rows()}
    result=call(body,context)
    assert result['outcome']=='complete' and [i['status'] for i in result['items']]==['applied']*3
    after={r['anchor_key']:r for r in rows()}
    for key,row in after.items():
        assert row['created_at']==before[key]['created_at']
    assert after['cleanup-1'].get('chased_count',0)==0 and 'chase_pending' not in after['cleanup-1']
    assert after['cleanup-2']['status']=='dismissed'
    assert after['cleanup-3']['snoozed_until']==result['items'][2]['snoozed_until']
    assert after['cleanup-4']==before['cleanup-4']
    from pathlib import Path
    files={p.name:p.read_bytes() for p in Path(ke._load_continuity()._dir()).glob('*.md')}
    context[1]['_source_authority']=lambda:{'complete':False}
    context[1]['_configured_tz_name']=lambda:'Pacific/Kiritimati'
    assert call(dict(body,operation='inspect'),context)==result
    assert files=={p.name:p.read_bytes() for p in Path(ke._load_continuity()._dir()).glob('*.md')}


def test_cleanup_selected_stale_refused_other_valid_applies(context):
    body=prepare_cleanup(context)
    cr=ke._load_continuity()
    row=next(r for r in rows() if r['anchor_key']=='cleanup-2')
    row['summary']='Changed after displayed list';cr._persist(row)
    result=call(body,context)
    assert result['outcome']=='partial'
    assert [i['status'] for i in result['items']]==['applied','refused','applied']
    assert result['items'][1]['reason']=='target_changed'
    assert next(r for r in rows() if r['anchor_key']=='cleanup-2')['status']=='open'


@pytest.mark.parametrize('failed_write',[1,2])
def test_cleanup_partial_loss_no_replay_and_later_skipped(context,monkeypatch,failed_write):
    body=prepare_cleanup(context)
    cr=ke._load_continuity();original=cr._persist;count=[0]
    def lose(row,**kwargs):
        count[0]+=1
        original(row,**kwargs)
        if count[0]==failed_write:
            raise OSError('lost after actual canonical write')
    monkeypatch.setattr(cr,'_persist',lose)
    result=call(body,context)
    expected=['unknown','skipped','skipped'] if failed_write==1 else ['applied','unknown','skipped']
    assert [i['status'] for i in result['items']]==expected and result['outcome']=='partial'
    monkeypatch.setattr(cr,'_persist',lambda *a,**k:pytest.fail('inspect never loop writes'))
    assert call(dict(body,operation='inspect'),context)==result
    assert call(body,context)==result


def test_cleanup_invalid_explicit_selection_and_proposal_consumption(context):
    body=prepare_cleanup(context,[{'index':2,'action':'dismiss','correction':'dismiss second.'}])
    bad=__import__('copy').deepcopy(body['args'])
    bad['items'][0]['correction']='yes'
    with pytest.raises(ValueError,match='selection'):
        call(request(bad,event='e'*64,text='yes'),context)
    assert call(body,context)['outcome']=='complete'
    assert call(request(body['args'],event='f'*64,text=body['text']),context)['code']=='event_conflict'


def test_cleanup_revoked_complete_revision_refuses_before_any_write(context):
    body=prepare_cleanup(context)
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'b'*64,'google_sources':['gmail']}
    assert call(body,context)['code']=='source_unavailable'
    assert all(r['status']=='open' and not r.get('snoozed_until') for r in rows())


def test_cleanup_restart_finalizes_contacting_without_any_loop_write(context,monkeypatch):
    body=prepare_cleanup(context)
    import preferences
    original=preferences._durable_preferences_replace
    failed=[False]
    def lose(path,data):
        original(path,data)
        saved=data.get('owner_preference_receipts',{}).get(body['event_id'])
        if saved and saved['progress'][1]['status']=='contacting' and not failed[0]:
            failed[0]=True
            raise OSError('lost immediately after second durable intent')
    monkeypatch.setattr(preferences,'_durable_preferences_replace',lose)
    assert call(body,context)=={'version':1,'status':'unknown','cleanup_stage':'writer','cleanup_category':'io'}
    cr=ke._load_continuity()
    monkeypatch.setattr(cr,'_persist',lambda *a,**k:pytest.fail('inspect must not apply'))
    recovered=call(dict(body,operation='inspect'),context)
    assert [i['status'] for i in recovered['items']]==['applied','unknown','skipped']
    assert recovered['outcome']=='partial'
    assert call(dict(body,operation='inspect'),context)==recovered
    assert next(r for r in rows() if r['anchor_key']=='cleanup-2')['status']=='open'


@pytest.mark.parametrize('change',['duplicate','outside','mismatch','inferred','wrong_span'])
def test_cleanup_closed_selection_refuses_before_writes(context,change):
    import copy
    body=prepare_cleanup(context)
    args=copy.deepcopy(body['args'])
    if change=='duplicate':
        args['items'][1]['index']=1
    elif change=='outside':
        args['items'][2]['index']=20
    elif change=='mismatch':
        args['items'][0]['action']='dismiss'
    elif change=='inferred':
        args['items'][2]['correction']='snooze 3 for a week'
    else:
        args['items'][0]['correction']='keep item 1'
    changed=request(args,event='e'*64,text=body['text'])
    if change=='wrong_span':
        assert call(changed,context)['code']=='owner_span_unavailable'
    else:
        with pytest.raises(ValueError):
            call(changed,context)
    assert all(r['status']=='open' for r in rows())


def test_cleanup_unselected_change_and_mixed_source_revocation(context):
    body=prepare_cleanup(context)
    cr=ke._load_continuity()
    row=next(r for r in rows() if r['anchor_key']=='cleanup-4')
    row['summary']='Unselected change';cr._persist(row)
    assert call(body,context)['outcome']=='complete'
    assert next(r for r in rows() if r['anchor_key']=='cleanup-4')['summary']=='Unselected change'


def test_cleanup_list_empty_bound_and_readonly(context):
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':[]}
    proposed=request({'operation':'propose_cleanup','source_revision':'a'*64},operation='preview',text='List')
    result=call(proposed,context)
    assert result['review']['rows']==[] and result['review']['total']==0
    assert rows()==[]


@pytest.mark.parametrize('deadline, projected', [
    ('tomorrow', None),
    ('2026-02-30', None),
    ('2026-09-01 (soon)', None),
    ('2026-09-01 (fri)', '2026-09-01'),
    ('2026-09-01T23:30:00-07:00', '2026-09-01'),
])
@pytest.mark.parametrize('scope', ['open', None])
def test_cleanup_list_projects_legacy_deadlines_without_changing_ledger(context, deadline, projected, scope):
    from delivery_effects import loop_version, loop_identity

    source(context)
    cr = ke._load_continuity()
    legacy = rows()[0]
    legacy.update(created_at='2026-09-01', deadline=deadline)
    cr._persist(legacy)
    for index, name in enumerate(('amber', 'birch', 'cedar'), 1):
        title = f'Sotto loop check {name}'
        assert call(request({'operation':'add', 'text':title}, event=str(index)*64,
                            text=f'Add {title}'), context)['status'] == 'committed'
    before = {path: path.read_bytes() for path in context[0].rglob('*.md')}
    proofs = {row['anchor_key']:(loop_version(row), loop_identity(row)) for row in rows()}
    args = {'operation':'propose_cleanup', 'source_revision':'a'*64}
    if scope is not None:
        args['scope'] = scope
    result = call(request(args, operation='preview', text='Show a numbered task list'), context)
    assert result['status'] == 'preview'
    review = result['review']
    assert review['total'] == (4 if scope == 'open' else 1)
    assert review['remaining'] == 0
    listed = {row['anchor_key']:row for row in review['rows']}
    assert listed['source-loop']['deadline'] == projected
    assert listed['source-loop']['overdue'] == (projected is not None)
    for anchor, row in listed.items():
        assert (row['loop_version'], row['loop_identity']) == proofs[anchor]
    if scope == 'open':
        assert {row['what'] for row in listed.values()} == {
            'send the deck', 'Sotto loop check amber', 'Sotto loop check birch', 'Sotto loop check cedar'}
    ke.validate_cleanup_list(review)
    assert {path: path.read_bytes() for path in context[0].rglob('*.md')} == before


@pytest.mark.parametrize('admitted_day',['2026-10-06','2026-09-01'])
def test_dismiss_frozen_original_instant_today_and_past_date(context,admitted_day):
    source(context)
    instant=datetime.fromisoformat(admitted_day+'T18:00:00+00:00')
    cr=ke._load_continuity();row=rows()[0]
    today=cr._to_user_zone(instant).date().isoformat()
    cr._terminate(row,'dismissed','user_dismissed',today,now=instant)
    cr._persist(row,loop_dismiss={'today':today,'now':instant.isoformat()})
    assert datetime.fromisoformat(rows()[0]['closed_at'])==instant


def test_cleanup_completion_marker_loss_preserves_known_item_on_inspect(context,monkeypatch):
    body=prepare_cleanup(context)
    import preferences
    original=preferences._durable_preferences_replace
    lost=[False]
    def lose(path,data):
        original(path,data)
        saved=data.get('owner_preference_receipts',{}).get(body['event_id'])
        if saved and saved['progress'][0]['status']=='applied' and not lost[0]:
            lost[0]=True
            raise OSError('lost after durable applied marker')
    monkeypatch.setattr(preferences,'_durable_preferences_replace',lose)
    assert call(body,context)=={'version':1,'status':'unknown','cleanup_stage':'writer','cleanup_category':'unavailable'}
    monkeypatch.setattr(ke._load_continuity(),'_persist',lambda *a,**k:pytest.fail('never resume loop writes'))
    result=call(dict(body,operation='inspect'),context)
    assert [i['status'] for i in result['items']]==['applied','skipped','skipped']


def test_cleanup_readonly_list_and_unicode_shared_vector(context):
    import json
    from pathlib import Path
    body=prepare_cleanup(context)
    cr=ke._load_continuity()
    before={p.name:p.read_bytes() for p in Path(cr._dir()).glob('*.md')}
    preview=call(request({'operation':'propose_cleanup','source_revision':'a'*64},operation='preview'),context)
    assert before=={p.name:p.read_bytes() for p in Path(cr._dir()).glob('*.md')}
    assert preview['review']==body['args']['list']
    data=json.loads('{"review":{"operation":"cleanup_list","source_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","rows":[{"index":1,"anchor_key":"cedar-🗓","name":"Dana 李\\nSaved","what":"Return Café deck","direction":"you_owe","age_days":7,"times_surfaced":3,"surface_count_provenance":"accepted_delivery","overdue":true,"deadline":"2026-10-01","chased_count":2,"chased_out":true,"suggestion":"chased twice — nudge again or drop","loop_version":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","loop_identity":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}],"total":2,"remaining":1,"review_hash":"f546b0fedbddaacf33a5e66deb675a64bf007b834dee7b53078f8aab9edf9f4c"},"render":"Stale loops in your currently permitted sources:\\n\\n1. Dana 李\\\\u000aSaved — Return Café deck\\n   You owe · overdue; 7 days old; surfaced 3 times; chased twice; automatic nudging has stopped\\n   chased twice — nudge again or drop\\n\\n1 more stale loops are not shown in this list.\\n\\nTell me which numbers to dismiss, snooze, or keep. For example: dismiss 1; snooze 2 for 7 days; keep 3. Nothing has changed yet."}')
    ke.validate_cleanup_list(data['review'])
    assert ke.render_cleanup_list(data['review'])==data['render']
    # New scans use reminder language; the historical review/hash above remains replayable.
    from retune_scan import REMINDER_HANDOFF
    current = json.loads(json.dumps(data['review']))
    current['rows'][0]['suggestion'] = REMINDER_HANDOFF
    current['review_hash'] = ke._cleanup_hash({k:v for k,v in current.items() if k!='review_hash'})
    ke.validate_cleanup_list(current)
    rendered = ke.render_cleanup_list(current)
    assert 'reminded you twice; automatic reminders have stopped' in rendered
    assert REMINDER_HANDOFF in rendered and 'chased twice' not in rendered
    assert current['review_hash'] != data['review']['review_hash']
    for suggestion in ('do it or dismiss', 'nudge or drop'):
        legacy = json.loads(json.dumps(data['review']))
        legacy['rows'][0]['suggestion'] = suggestion
        legacy['review_hash'] = ke._cleanup_hash({k:v for k,v in legacy.items() if k!='review_hash'})
        ke.validate_cleanup_list(legacy)
        assert 'chased twice; automatic nudging has stopped' in ke.render_cleanup_list(legacy)


@pytest.mark.parametrize('text', ["Don't dismiss item 2", 'Should I dismiss item 2?', 'dismiss item 2; keep everything else', 'dismiss item 2 and dismiss item 2'])
def test_cleanup_whole_owner_consent(context, text):
    body=prepare_cleanup(context)
    body['args']['items']=[{'index':2,'action':'dismiss','correction':'dismiss item 2'}]
    body=request(body['args'],event=body['event_id'],text=text)
    assert call(body,context)=={'version':1,'status':'rejected','code':'owner_span_unavailable'}
    assert all(r['status']=='open' for r in rows() if r['action_type']!='waiting_on')


def test_cleanup_whole_owner_positive_separators():
    from runtime_owner_loop_tool import cleanup_selections_match
    items=[{'correction':'dismiss 1'},{'correction':'keep 2'}]
    assert cleanup_selections_match('dismiss 1; and keep 2.',items)
    assert cleanup_selections_match('keep 2, dismiss 1',items)
    assert not cleanup_selections_match('Please dismiss 1; keep 2',items)


def test_cleanup_saved_dismiss_inspect_survives_timezone_change(context,monkeypatch):
    body=prepare_cleanup(context)
    result=call(body,context)
    assert result['status']=='committed'
    cr=ke._load_continuity()
    monkeypatch.setattr(cr,'_to_user_zone',lambda instant: instant.astimezone(timezone.utc))
    monkeypatch.setattr(cr,'_persist',lambda *a,**k:pytest.fail('inspection cannot write loops'))
    assert call(dict(body,operation='inspect'),context)==result
