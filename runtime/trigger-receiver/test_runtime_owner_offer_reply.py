"""Actual producer/accepted offer/canonical source correction, with external inputs synthetic."""
import hashlib
import json
import importlib.util

import pytest
import yaml
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, WHEN, THREAD, capability

import pending_offer
import knowledge_edit as writer
import source_context
import compose_notification as notification
import delivery_effects
import runtime_owner_offer_reply as tool
__all__ = ['context']


def request(text='Done.', event='d'*64, operation='apply', anchor='accepted-source-offer'):
    args = {'reply_to_message_id': anchor, 'provider_sent_at_ms': None, 'provider_message_id': 'owner-provider-reply'}
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([event, 'owner_offer_reply', canonical],
                                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return dict(version=1, operation=operation, identity=IDENTITY, action_id=action, event_id=event,
        payload_hash='b'*64, text_hash=hashlib.sha256(text.encode()).hexdigest(), text=text,
        admitted_at_ms=int(WHEN.timestamp()*1000), provider_thread_id=THREAD, args=args)


def call(body, context):
    data, policy, activation = context
    return tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT, policy=policy,
        data_root=data, capability=capability, activation=activation, offers=pending_offer, writer=writer)


def source_offer(context, monkeypatch, source='gmail', changed_before_compose=None, contextual_source=None):
    data, policy, activation = context
    policy.update(SOTTO_DATA=str(data), SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1')
    monkeypatch.setattr(pending_offer, '_now', lambda: WHEN)
    monkeypatch.setattr(source_context, 'allowed', lambda source: True)
    directory = data/'knowledge/continuity'
    directory.mkdir(parents=True)
    row = {'anchor_key':'source:cedar', 'status':'open', 'summary':'Cedar packet review',
        'source':source, 'channel':'email' if source=='gmail' else source,
        'source_thread_id':'provider-thread-cedar', 'source_message_id':'provider-message-cedar',
        'source_refs':[{'sourceType':source,'sourceId':'provider-message-cedar'}],
        'contact_name':'Dana','contact_identifier':'dana@outside.com', 'action_type':'waiting_on',
        'created_at':'2026-10-04','chase_pending':WHEN.date().isoformat(),
        'chased_count':0 if source=='gmail' else 2}
    path=directory/'cedar.md'
    if contextual_source:
        row['source_refs'].append({'sourceType': contextual_source, 'sourceId': 'context-message'})
    path.write_text('---\n'+yaml.safe_dump(row)+'---\n'); path.chmod(0o600)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID','e'*32)
    monkeypatch.setenv('PHOTON_HOME_CHANNEL',IDENTITY['owner_id'])
    monkeypatch.setattr(notification, '_calendar', lambda: {})
    monkeypatch.setattr(notification, '_read_json_script', lambda *a, **k: {'unproven':'Must not enter model'})
    monkeypatch.setattr(notification.style_apply, 'apply', lambda *_: {})
    import personal_context
    monkeypatch.setattr(personal_context, 'conversation_snapshot', lambda: [])
    monkeypatch.setattr(notification.gemini, 'provider_key', lambda *_: 'synthetic-key')
    def model(*args, **kwargs):
        items=json.loads(args[3]); assert all('person_facts' not in item for item in items)
        return json.dumps({'items':[{'id':items[0]['id'],'text':'Dana has not returned the Cedar review.',
                                     'draft':'Any word on the Cedar review?', 'decline':''}]})
    spec=importlib.util.spec_from_file_location('source_offer_actual_scan', notification.PACK/'proactive/scripts/proactive_scan.py')
    scanner=importlib.util.module_from_spec(spec); spec.loader.exec_module(scanner)
    candidates=(scanner._chase_candidates(WHEN.date().isoformat()) if source=='gmail' else scanner._handoff_candidates())
    assert len(candidates)==1 and candidates[0]['title']=='Dana — Cedar packet review'
    selected=scanner.scan([],[],{},'',WHEN,chase_candidates=candidates if source=='gmail' else None,
                          handoff_candidates=candidates if source!='gmail' else None,handoff_allowed=True)
    assert len(selected['nudges'])==1
    if changed_before_compose:
        changed_before_compose(path,row)
    text=notification.compose('proactive',selected['nudges'],now=WHEN,llm=model)
    assert text.endswith('Want this in your Gmail drafts?' if source=='gmail' else 'want a follow-up draft, or let it go?')
    effects=json.loads((data/'events'/('delivery-effects-'+'e'*32+'.json')).read_text())['effects']
    offer=next(effect['offer'] for effect in effects if effect['kind']=='pending_offer')
    if changed_before_compose:
        assert offer.get('loop_snapshot') in (None,{})
    else:
        assert offer['loop_snapshot']['anchor_key']==row['anchor_key']
    delivery_effects.finalize(effects, {'message_id':'accepted-source-offer','target':'photon',
        'chat_id':'iMessage;-;owner','accepted_at':WHEN.timestamp()-1})
    # Same accepted-delivery bookkeeping changes neither semantic version nor identity.
    cr=writer._load_continuity()
    (cr.finalize_chase if source=='gmail' else cr.finalize_handoff)(row['anchor_key'], now=WHEN)
    return path, row, pending_offer.get_offer(message_id='accepted-source-offer')


@pytest.mark.parametrize('source',['gmail','imessage','whatsapp'])
def test_real_producer_terminal_receipt_and_original_inspection(context,monkeypatch,source):
    path,row,offer=source_offer(context,monkeypatch,source)
    current=yaml.safe_load(path.read_text().split('---')[1])
    current['chased_count']=2
    path.write_text('---\n'+yaml.safe_dump(current)+'---\n')
    receipt=call(request(),context)
    assert receipt['status']=='committed' and receipt['outcome']['loop_changed']
    terminal=yaml.safe_load(path.read_text().split('---')[1])
    assert terminal['status']=='resolved' and terminal['source']==source
    assert 'content_provenance' not in terminal
    assert call(request(operation='inspect'),context)==receipt
    assert call(request(),context)==receipt
    assert call(request(text='Drop it.'),context)['code']=='event_conflict'
    assert not pending_offer.get_offer(message_id='accepted-source-offer')


def test_decline_retention_competing_reply_and_semantic_revocation(context,monkeypatch):
    path,row,offer=source_offer(context,monkeypatch)
    receipt=call(request(text='No thanks.'),context)
    assert receipt['outcome']=={'operation':'apply','offer_id':offer['offer_id'],'action':'declined','loop_changed':False}
    assert yaml.safe_load(path.read_text().split('---')[1])['status']=='open'
    assert call(request(text='Done.',event='f'*64),context)['status']=='rejected'
    monkeypatch.delenv('SOTTO_DELIVERY_RUN_ID')
    pending_offer.set_offer('meeting_prep','Want the prep?')
    pending_offer.clear_offer()
    assert call(request(text='No thanks.',operation='inspect'),context)==receipt


def test_current_source_and_row_changes_refuse(context,monkeypatch):
    path,row,offer=source_offer(context,monkeypatch)
    monkeypatch.setattr(source_context,'allowed',lambda source:source!='gmail')
    assert call(request(text='No.'),context)['code']=='source_unavailable'
    assert call(request(),context)['code']=='source_unavailable'
    monkeypatch.setattr(source_context,'allowed',lambda source:True)
    row['summary']='Changed obligation'
    path.write_text('---\n'+yaml.safe_dump(row)+'---\n')
    assert call(request(),context)['code']=='target_changed'
    assert yaml.safe_load(path.read_text().split('---')[1])['status']=='open'


def test_known_non_loop_decline_only(context,monkeypatch):
    data,policy,_=context; policy.update(SOTTO_DATA=str(data), SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1')
    monkeypatch.setattr(pending_offer,'_now',lambda:WHEN)
    offer=pending_offer.set_offer('meeting_prep','Want full prep?')
    pending_offer.activate_offer(offer,{'message_id':'accepted-source-offer','target':'photon',
        'chat_id':'iMessage;-;owner','accepted_at':WHEN.timestamp()-1})
    assert call(request(),context)['code']=='unsupported_offer'
    receipt=call(request(text='No.'),context)
    assert receipt['outcome']['action']=='declined' and not receipt['outcome']['loop_changed']
    assert call(request(text='No.',operation='inspect'),context)==receipt


def test_competing_no_and_done_cannot_mutate_then_reject(context,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    path,row,offer=source_offer(context,monkeypatch)
    barrier=threading.Barrier(2)
    original=pending_offer.get_offer
    def both_read(*args,**kwargs):
        result=original(*args,**kwargs)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(pending_offer,'get_offer',both_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda body:call(body,context),
                             [request(text='No.'),request(text='Done.',event='f'*64)]))
    committed=[result for result in results if result['status']=='committed']
    assert len(committed)==1 and sum(result['status']=='rejected' for result in results)==1
    current=yaml.safe_load(path.read_text().split('---')[1])
    assert current['status']==('open' if committed[0]['outcome']['action']=='declined' else 'resolved')
    if current['status']=='open':
        assert 'source_offer_reply' not in current
    else:
        assert current['source_offer_reply']['receipt']==committed[0]


def test_terminal_receipt_recovers_after_offer_settlement_response_lost(context,monkeypatch):
    path,row,offer=source_offer(context,monkeypatch)
    original=pending_offer.jsonstore.write_atomic
    def lost(path,value,*args,**kwargs):
        if str(path)==pending_offer._path():
            raise OSError('synthetic lost settlement')
        return original(path,value,*args,**kwargs)
    monkeypatch.setattr(pending_offer.jsonstore,'write_atomic',lost)
    with pytest.raises(OSError):
        call(request(),context)
    terminal=yaml.safe_load(path.read_text().split('---')[1])
    receipt=terminal['source_offer_reply']['receipt']
    monkeypatch.setattr(pending_offer.jsonstore,'write_atomic',original)
    assert call(request(operation='inspect'),context)==receipt
    assert call(request(),context)==receipt
    assert yaml.safe_load(path.read_text().split('---')[1])==terminal


def test_stale_persisted_candidate_not_rebound_to_changed_row(context,monkeypatch):
    path,row,offer=source_offer(context,monkeypatch)
    row['summary']='Different obligation'
    path.write_text('---\n'+yaml.safe_dump(row)+'---\n')
    text=notification.compose('proactive',[{'kind':'chase','key':'stale','person':'Dana',
        'identifier':'dana@outside.com','channel':'email','anchor_key':row['anchor_key'],
        'title':'Cedar packet review'}],now=WHEN,llm=lambda *a,**k:json.dumps({'items':[{'id':json.loads(a[3])[0]['id'],
            'text':'Dana has not returned the Cedar review.','draft':'Any word on the Cedar review?','decline':''}]}))
    assert text
    effects=json.loads((context[0]/'events'/('delivery-effects-'+'e'*32+'.json')).read_text())['effects']
    staged=[effect['offer'] for effect in effects if effect['kind']=='pending_offer']
    assert staged[-1].get('loop_snapshot') in (None,{})


@pytest.mark.parametrize('source',['apple_mail','mail'])
def test_ambiguous_mail_is_not_gmail_grant_authority(context,monkeypatch,source):
    from ledger_provenance import source_loop_sources, UnknownProvenanceError
    with pytest.raises(UnknownProvenanceError):
        source_loop_sources({'source':source,'channel':source,'source_thread_id':'opaque',
            'source_refs':[{'sourceType':source,'sourceId':'message'}]})


@pytest.mark.parametrize('field,value',[('contact_identifier','different@outside.com'),
    ('source_message_id','new-source-message'),('deadline','2026-10-07')])
def test_same_title_changed_identity_since_real_scan_never_mints_authority(context,monkeypatch,field,value):
    def change(path,row):
        current=dict(row); current[field]=value
        path.write_text('---\n'+yaml.safe_dump(current)+'---\n')
    path,row,offer=source_offer(context,monkeypatch,changed_before_compose=change)
    assert call(request(),context)['code']=='target_unavailable'
    assert yaml.safe_load(path.read_text().split('---')[1])['status']=='open'


def test_shared_writer_batch_unions_all_attributable_sources_and_omits_all_unproven_facts(context,monkeypatch):
    source_offer(context,monkeypatch)
    items=[{'id':'first','kind':'chase','person':'Dana','_loop_snapshot':{'sources':['gmail']},
            '_composition_sources':['gmail'],'person_facts':{'unknown':'private'}},
           {'id':'second','kind':'birthday','person':'Sam','_composition_sources':['whatsapp'],
            'person_facts':{'unknown':'sibling private'}}]
    def model(*args,**kwargs):
        rows=json.loads(args[3]); assert all('person_facts' not in row for row in rows)
        return json.dumps({'items':[{'id':'first','text':'Dana asked about Cedar.','draft':'','decline':''}]})
    notification._write(items,model)
    assert items[0]['_loop_snapshot']['sources']==['gmail','whatsapp']


def test_legacy_weekly_offer_without_producer_snapshot_is_not_rebound(context,monkeypatch):
    path,row,offer=source_offer(context,monkeypatch)
    newer=pending_offer.set_offer('handoff','Keep waiting?',anchor_key=row['anchor_key'])
    assert 'loop_snapshot' not in newer


def test_terminal_reply_uses_configured_local_day_across_utc_midnight(context,monkeypatch):
    from datetime import timedelta
    path,row,offer=source_offer(context,monkeypatch)
    monkeypatch.setenv('SOTTO_TIMEZONE','America/Los_Angeles')
    body=request()
    body['admitted_at_ms']=int((WHEN+timedelta(hours=12)).timestamp()*1000)
    assert call(body,context)['status']=='committed'
    saved=yaml.safe_load(path.read_text().split('---')[1])
    assert saved['resolved_at']=='2026-10-05'
    assert saved['closed_at'].startswith('2026-10-05')


def selected_request(message_id, operation, *, text=None):
    body=request(anchor=None,operation=operation) if text is None else request(anchor=None,operation=operation,text=text)
    stamp=int(WHEN.timestamp()*1000)
    body['args']['provider_sent_at_ms']=stamp
    body['args']['selected_offer']={'mode':'accepted_dispatch','dispatch_id':'accepted-dispatch',
        'message_id':message_id,'provider_sent_at_ms':stamp-1}
    return rebind_selected(body)


def rebind_selected(body):
    canonical=json.dumps(body['args'],sort_keys=True,ensure_ascii=False,separators=(',',':'))
    body['action_id']=hashlib.sha256(json.dumps([body['event_id'],'owner_offer_reply',canonical],
        ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return body


def test_selected_unthreaded_decline_preview_and_exact_receipt(context,monkeypatch):
    path,row,offer=source_offer(context,monkeypatch)
    before=path.read_bytes()
    body=selected_request('accepted-source-offer','preview',text='No.')
    assert call(body,context)=={'version':1,'status':'match','offer_id':offer['offer_id'],'kind':'chase'}
    assert path.read_bytes()==before and pending_offer.inspect_reply(body['event_id'],'unused') is None
    body['operation']='apply'
    receipt=call(body,context)
    assert receipt['outcome']['action']=='declined' and path.read_bytes()==before
    assert call(dict(body,operation='inspect'),context)==receipt
    body['args']['selected_offer']['dispatch_id']='different'
    assert call(rebind_selected(body),context)['code']=='event_conflict'


def test_selected_unthreaded_terminal_wording_never_applies(context,monkeypatch):
    path,row,offer=source_offer(context,monkeypatch)
    before=path.read_bytes()
    for text in ('Done.','Drop it.'):
        body=selected_request('accepted-source-offer','apply',text=text)
        with pytest.raises(ValueError,match='wording'):
            call(body,context)
    assert path.read_bytes()==before


def test_selected_unthreaded_decline_no_match_no_effect(context,monkeypatch):
    path,row,offer=source_offer(context,monkeypatch)
    before=path.read_bytes()
    body=selected_request('ordinary-question','preview',text='No.')
    assert call(body,context)=={'version':1,'status':'no_match'}
    assert path.read_bytes()==before and pending_offer.get_offer(message_id='accepted-source-offer')
