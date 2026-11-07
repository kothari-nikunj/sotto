"""Accepted offer selection and actual queue admission remain bound to original owner proof."""
import hashlib
import json
from datetime import timedelta
import pytest

import runtime_owner_meeting_prep_request as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, WHEN, THREAD, capability
from test_requested_brief import rec
import pending_offer
from test_runtime_owner_skill_request import route_proof

__all__ = ['context', 'rec']


def request(event='d' * 64, anchor='offered-message', operation='admit', stamp=None):
    args = {'reply_to_message_id': anchor, 'provider_sent_at_ms': stamp, 'provider_message_id': 'actual-provider-yes'}
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([event, 'owner_meeting_prep_request', canonical],
                                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return dict(version=1, operation=operation, identity=IDENTITY, action_id=action, event_id=event,
        payload_hash='b' * 64, text_hash=hashlib.sha256(b'Yes.').hexdigest(), text='Yes.',
        admitted_at_ms=int(WHEN.timestamp() * 1000), provider_thread_id=THREAD, args=args)


def setup(context, rec, monkeypatch, person='Dana'):
    data, policy, activation = context
    policy.update(SOTTO_DATA=str(data), SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1')
    monkeypatch.setenv('SOTTO_DATA', str(data))
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', IDENTITY['owner_id'])
    monkeypatch.setenv('SOTTO_TENANT_ID', 'tenant')
    monkeypatch.setattr(pending_offer, '_now', lambda: WHEN)
    config = data / 'config/photon-activation.json'
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps({'activated': True, 'owner': IDENTITY['owner_id'],
                                 'tenant_id': 'tenant', 'chat_id': 'iMessage;-;owner'}))
    monkeypatch.setattr(rec, '_composition_evidence', lambda label: {'source_revision': 'a'*64, 'route_digest': 'b'*64})
    monkeypatch.setattr(rec, '_activation_tick', lambda: None)
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', route_proof)
    offer = pending_offer.set_offer('meeting_prep', 'Pull full prep?', person)
    offer['meeting'] = {'event_id': 'calendar-event', 'start': (WHEN + timedelta(hours=1)).isoformat()}
    pending_offer.activate_offer(offer, {'message_id': 'offered-message', 'target': 'photon',
        'chat_id': 'iMessage;-;owner', 'accepted_at': WHEN.timestamp() - 1})
    return offer


def call(body, context, rec):
    data, policy, activation = context
    return tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT, policy=policy,
        data_root=data, capability=capability, activation=activation, queue=rec.WORK_QUEUE,
        admit=rec.request_owner_meeting_prep, offers=pending_offer)


def test_exact_receipt_survives_lost_response_terminal_scrub_and_offer_clear(context, rec, monkeypatch):
    setup(context, rec, monkeypatch)
    body = request()
    assert call(dict(body, operation='inspect'), context, rec)['status'] == 'not_found'
    receipt = call(body, context, rec)
    assert receipt['status'] == 'committed'
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    assert job['payload']['label'].startswith('meeting-prep:')
    assert 'Yes.' not in job['admission']
    assert call(body, context, rec) == receipt
    assert call(request(event='e'*64), context, rec)['code'] == 'already_requested'
    assert call(request(anchor='different'), context, rec)['code'] == 'event_conflict'
    pending_offer.clear_offer(expected=pending_offer.get_offer(message_id='offered-message'))
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='done',payload='{}',result=NULL WHERE id=?", (job['id'],))
    assert call(dict(body, operation='inspect'), context, rec) == receipt


@pytest.mark.parametrize('suffix', ['', ' Want me to?'])
def test_single_brief_offer_stages_then_accepted_yes_queues_its_exact_occurrence(context, rec, monkeypatch, suffix):
    import brief_runner
    import delivery_effects
    setup(context, rec, monkeypatch)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a'*32)
    monkeypatch.setattr(rec._source_context(), 'allowed', lambda source: source == 'calendar')
    meeting = {'event_id': 'advance-review', 'start': (WHEN + timedelta(hours=54)).isoformat()}
    question = 'I can prepare a briefing for Cedar rollout review.' + suffix
    artifact = {'brief_markdown': question, 'brief_text': question, '_source_permissions': ['calendar'],
                '_calendar_eligibility': [{'kind': 'eligibility', 'source': 'calendar',
                    'calendar_event_id': meeting['event_id'], 'calendar_start': meeting['start']}],
                '_prep_offer': {'person': 'Dana Rivers', 'question': question, 'meeting': meeting, 'ambiguous': False}}
    brief_runner._stage_prep_offer(artifact)
    original_expiry = artifact['_prep_offer_record']['expires_at']
    monkeypatch.setattr(pending_offer, '_now', lambda: WHEN + timedelta(minutes=10))
    brief_runner._stage_prep_offer(artifact)  # saved-artifact recovery never renews authority
    assert artifact['_prep_offer_record']['expires_at'] == original_expiry
    assert pending_offer.get_offer(message_id='accepted-brief') == {}
    effects = json.loads((context[0]/'events'/('delivery-effects-'+'a'*32+'.json')).read_text())['effects']
    assert len([effect for effect in effects if effect['kind'] == 'pending_offer']) == 1
    assert all(effect['kind'] != 'meeting_prep_delivered' for effect in effects)
    receipt = {'message_id': 'accepted-brief', 'target': 'photon',
               'chat_id': 'iMessage;-;owner', 'accepted_at': WHEN.timestamp() - 1}
    assert delivery_effects.finalize(effects, receipt)
    assert delivery_effects.finalize(effects, receipt)
    assert pending_offer.get_offer(message_id='accepted-brief')['meeting'] == meeting
    answer = call(request(anchor='accepted-brief'), context, rec)
    assert answer['status'] == 'committed'
    job = rec.WORK_QUEUE.get(rec.DATA, answer['outcome']['request_id'])
    assert json.loads(job['payload']['prompt'])['offer']['meeting'] == meeting
    assert call(request(anchor='accepted-brief'), context, rec) == answer
    assert not list(context[0].glob('proactive/????-??-??.json'))  # no imminent-prep delivery stamp
    monkeypatch.setattr(pending_offer, '_now', lambda: WHEN + timedelta(hours=4))
    brief_runner._stage_prep_offer(artifact)
    assert artifact['_prep_offer_record']['expires_at'] == original_expiry
    assert call(request(event='e'*64, anchor='accepted-brief'), context, rec)['code'] == 'no_matching_offer'


@pytest.mark.parametrize('other_question', [False, True])
def test_brief_offer_requires_rendered_source_proof_and_preserves_other_question_ambiguity(context, rec, monkeypatch, other_question):
    import brief_runner
    import delivery_effects
    setup(context, rec, monkeypatch)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a'*32)
    monkeypatch.setattr(rec._source_context(), 'allowed', lambda source: source == 'calendar')
    meeting = {'event_id': 'advance-review', 'start': (WHEN + timedelta(hours=54)).isoformat()}
    question = 'I can prepare a briefing for Cedar rollout review.'
    artifact = {'brief_markdown': question, 'brief_text': question, '_source_permissions': ['calendar'],
                '_calendar_eligibility': [], '_prep_offer': {
                    'person': 'Dana Rivers', 'question': question, 'meeting': meeting, 'ambiguous': False}}
    brief_runner._stage_prep_offer(artifact)
    assert not (context[0]/'events'/('delivery-effects-'+'a'*32+'.json')).exists()
    artifact['_calendar_eligibility'] = [{'source': 'calendar', 'calendar_event_id': meeting['event_id'],
                                         'calendar_start': meeting['start']}]
    artifact['_prep_offer']['ambiguous'] = not other_question
    brief_runner._stage_prep_offer(artifact)
    if other_question:
        pending_offer.set_offer('procedure', 'Should I remember this preference?')
    effects = json.loads((context[0]/'events'/('delivery-effects-'+'a'*32+'.json')).read_text())['effects']
    delivery_effects.finalize(effects, {'message_id': 'accepted-brief', 'target': 'photon',
        'chat_id': 'iMessage;-;owner', 'accepted_at': WHEN.timestamp() - 1})
    assert pending_offer.get_offer(message_id='accepted-brief')['ambiguous'] is True
    assert call(request(anchor='accepted-brief'), context, rec)['code'] == 'ambiguous_offer'


def test_unseen_prep_question_cannot_activate_from_markdown_only(context, rec, monkeypatch):
    import brief_runner
    setup(context, rec, monkeypatch)
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a'*32)
    monkeypatch.setattr(rec._source_context(), 'allowed', lambda source: source == 'calendar')
    meeting = {'event_id': 'advance-review', 'start': (WHEN + timedelta(hours=54)).isoformat()}
    question = 'I can prepare a briefing for Cedar rollout review.'
    artifact = {'brief_markdown': question, 'brief_text': 'The delivered card has no offer.',
                '_source_permissions': ['calendar'], '_calendar_eligibility': [{
                    'source': 'calendar', 'calendar_event_id': meeting['event_id'], 'calendar_start': meeting['start']}],
                '_prep_offer': {'person': 'Dana Rivers', 'question': question, 'meeting': meeting, 'ambiguous': False}}
    brief_runner._stage_prep_offer(artifact)
    assert '_prep_offer_record' not in artifact
    assert not (context[0]/'events'/('delivery-effects-'+'a'*32+'.json')).exists()


def test_no_unthreaded_timestamp_fallback_or_mismatched_anchor(context, rec, monkeypatch):
    setup(context, rec, monkeypatch)
    assert call(request(anchor=None, stamp=int(WHEN.timestamp()*1000)), context, rec)['code'] == 'needs_context'
    assert call(request(anchor='wrong'), context, rec)['code'] == 'no_matching_offer'
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, 'd'*64) is None


def test_missing_composition_proof_defers_before_child_and_changed_proof_blocks_delivery(context, rec, monkeypatch):
    import pytest
    setup(context, rec, monkeypatch)
    receipt = call(request(), context, rec)
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    monkeypatch.setattr(rec, '_composition_evidence', lambda _: None)
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *args: None)
    monkeypatch.setattr(rec, '_require_sotto_procedure', lambda *args: None)
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('no model before proof'))
    with pytest.raises(rec._WorkDeferredError):
        rec._execute_work(job)
    monkeypatch.setattr(rec, '_retained_proof_current', lambda proof: proof == {
        'source_revision': 'a'*64, 'route_digest': 'b'*64})
    payload = {'label': job['payload']['label'], 'prep_source_revision': 'a'*64,
               'prep_route_digest': 'b'*64}
    assert rec._delivery_valid(payload)
    assert not rec._delivery_valid({**payload, 'prep_source_revision': 'c'*64})


def test_exact_provider_reply_anchor_wins_over_cross_clock_timestamp(context, rec, monkeypatch):
    setup(context, rec, monkeypatch)
    body = request(stamp=int((WHEN-timedelta(minutes=1)).timestamp()*1000))
    receipt = call(body, context, rec)
    assert receipt['status'] == 'committed'
    assert call(dict(body, operation='inspect'), context, rec) == receipt


def selected_request(message_id, operation, *, text=None):
    body=request(anchor=None,operation=operation) if text is None else request(anchor=None,operation=operation,text=text)
    stamp=int(WHEN.timestamp()*1000)
    body['args']['provider_sent_at_ms']=stamp
    body['args']['selected_offer']={'mode':'accepted_dispatch','dispatch_id':'accepted-dispatch',
        'message_id':message_id,'provider_sent_at_ms':stamp-1}
    return rebind_selected(body)


def rebind_selected(body):
    canonical=json.dumps(body['args'],sort_keys=True,ensure_ascii=False,separators=(',',':'))
    body['action_id']=hashlib.sha256(json.dumps([body['event_id'],'owner_meeting_prep_request',canonical],
        ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return body


def test_selected_unthreaded_prep_preview_then_admit_original_inspect(context,rec,monkeypatch):
    offer=setup(context,rec,monkeypatch)
    body=selected_request('offered-message','preview')
    assert call(body,context,rec)=={'version':1,'status':'match','offer_id':offer['offer_id'],'kind':'meeting_prep'}
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA,body['event_id']) is None
    body['operation']='admit'
    receipt=call(body,context,rec)
    assert receipt['status']=='committed'
    pending_offer.clear_offer()
    assert call(dict(body,operation='inspect'),context,rec)==receipt
    body['args']['selected_offer']['dispatch_id']='different'
    assert call(rebind_selected(body),context,rec)['code']=='event_conflict'


def test_selected_unthreaded_preview_has_no_latest_fallback(context,rec,monkeypatch):
    setup(context,rec,monkeypatch)
    body=selected_request('ordinary-question','preview')
    assert call(body,context,rec)=={'version':1,'status':'no_match'}
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA,body['event_id']) is None


def test_selected_binding_rejects_missing_time_nonprior_time_and_explicit_alias(context,rec,monkeypatch):
    import pytest
    setup(context,rec,monkeypatch)
    for field,value in [('provider_sent_at_ms',None),('reply_to_message_id','offered-message')]:
        body=selected_request('offered-message','admit')
        body['args'][field]=value
        with pytest.raises(ValueError,match='selection'):
            call(rebind_selected(body),context,rec)
    for value in (0,True,int(WHEN.timestamp()*1000),int(WHEN.timestamp()*1000)+1):
        body=selected_request('offered-message','admit')
        body['args']['selected_offer']['provider_sent_at_ms']=value
        with pytest.raises(ValueError,match='timestamp'):
            call(rebind_selected(body),context,rec)
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA,'d'*64) is None


def test_selected_preview_does_not_fall_back_after_exact_offer_consumed(context,rec,monkeypatch):
    setup(context,rec,monkeypatch)
    body=selected_request('offered-message','preview')
    pending_offer.clear_offer(expected=pending_offer.get_offer(message_id='offered-message'))
    newer=pending_offer.set_offer('meeting_prep','Newer question?','Sam')
    newer['meeting']={'event_id':'new-event','start':(WHEN+timedelta(hours=2)).isoformat()}
    pending_offer.activate_offer(newer,{'message_id':'newer-message','target':'photon',
        'chat_id':'iMessage;-;owner','accepted_at':WHEN.timestamp()})
    assert call(body,context,rec)=={'version':1,'status':'no_match'}
    body['operation']='admit'
    assert call(body,context,rec)['code']=='no_matching_offer'
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA,body['event_id']) is None


def test_explicit_anchor_preview_is_read_only_and_positive_only(context, rec, monkeypatch):
    offer = setup(context, rec, monkeypatch)
    preview = call(request(operation='preview'), context, rec)
    assert preview == {'version': 1, 'status': 'match', 'offer_id': offer['offer_id'], 'kind': 'meeting_prep'}
    assert call(request(anchor='other', operation='preview'), context, rec) == {'version': 1, 'status': 'no_match'}
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, 'd'*64) is None


def test_named_prep_accepts_full_identity_and_rejects_other_person(context, rec, monkeypatch):
    offer = setup(context, rec, monkeypatch, person='Akanksha Jagwani')
    body = request(operation='preview')
    for text, status in [('Full prep on Akanksha', 'match'),
                         ('Give me full prep on Akanksha Jagwani', 'match'),
                         ('Full prep on Dana', 'no_match')]:
        body['text'] = text
        body['text_hash'] = hashlib.sha256(text.encode()).hexdigest()
        assert call(body, context, rec)['status'] == status
    body.update(operation='admit', text='Full prep on Akanksha',
                text_hash=hashlib.sha256(b'Full prep on Akanksha').hexdigest())
    receipt = call(body, context, rec)
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    payload = json.loads(job['payload']['prompt'])
    assert payload['offer']['person'] == 'Akanksha Jagwani'
    assert payload['offer']['meeting'] == offer['meeting']


def test_named_prep_respects_expiry_route_ambiguity_and_calendar_consent(context, rec, monkeypatch):
    from types import SimpleNamespace
    import pytest
    offer = setup(context, rec, monkeypatch, person='Akanksha Jagwani')
    body = selected_request('offered-message', 'preview')
    body.update(text='Full prep on Akanksha', text_hash=hashlib.sha256(b'Full prep on Akanksha').hexdigest())
    with monkeypatch.context() as patch:
        patch.setattr(pending_offer, '_now', lambda: WHEN + timedelta(hours=4))
        assert call(body, context, rec)['status'] == 'no_match'
    with monkeypatch.context() as patch:
        patch.setattr(context[2], 'verified_current_photon_route', lambda *_: {'chat_id': 'other-route'})
        with pytest.raises(Exception, match='route changed'):
            call(body, context, rec)
    with monkeypatch.context() as patch:
        patch.setattr(pending_offer, 'get_offer', lambda **_: {'ambiguous': True, 'offers': [offer, offer]})
        assert call(body, context, rec)['status'] == 'no_match'
    body['operation'] = 'admit'
    monkeypatch.setattr(rec, '_source_context', lambda: SimpleNamespace(allowed=lambda _: False))
    assert call(body, context, rec)['code'] == 'needs_context'
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, body['event_id']) is None


def proposal_request(operation='preview', **changes):
    body = request(event='9'*64, operation=operation)
    body['text'] = 'Any of tomorrow’s meetings worth preparing for?'
    body['text_hash'] = hashlib.sha256(body['text'].encode()).hexdigest()
    body['args'] = {'operation': 'propose_prep', 'calendar_id': 'owner@inside.test',
        'event_id': 'source-event', 'start': (WHEN + timedelta(hours=24)).isoformat(),
        'attendee_email': 'dana@outside.test', 'source_revision': 'a'*64, **changes}
    canonical = json.dumps(body['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_meeting_prep_offer', canonical],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return body


def proposal_fixture(context, rec, monkeypatch):
    setup(context, rec, monkeypatch)
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@inside.test')
    monkeypatch.setenv('SOTTO_TIMEZONE', 'UTC')
    monkeypatch.setattr(tool, '_proposal_now', lambda: WHEN)
    context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'a'*64, 'google_sources': ['calendar']}
    event = {'id': 'source-event', 'calendar_id': 'owner@inside.test', 'owner_participates': True,
        'summary': 'Pilot pricing review', 'start': {'dateTime': (WHEN+timedelta(hours=24)).isoformat()},
        'attendees': [{'email': 'owner@inside.test'}, {'email': 'dana@outside.test'}]}
    reads = []
    def reader(operation, args):
        reads.append((operation, args))
        return {'item': event, 'observation': {'complete': True}}
    def invoke(body, reader_override=None):
        data, policy, activation = context
        return tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT, policy=policy,
            data_root=data, capability=capability, activation=activation, queue=rec.WORK_QUEUE,
            admit=lambda *a, **k: pytest.fail('proposal cannot queue research'), offers=pending_offer,
            calendar_reader=reader_override or reader)
    return invoke, event, reads


def test_chat_prep_proposal_activates_only_exact_accepted_delivery_and_recovers(context, rec, monkeypatch):
    invoke, event, reads = proposal_fixture(context, rec, monkeypatch)
    body = proposal_request()
    assert invoke(dict(body, operation='inspect')) == {'version': 1, 'status': 'not_found'}
    preview = invoke(body)
    assert preview['status'] == 'preview' and len(reads) == 1
    assert invoke(body) == preview and len(reads) == 1
    review = preview['review']
    assert review['person'] == 'dana@outside.test'
    assert review['meeting']['event_id'].startswith('gcal_')
    assert 'Pilot pricing review on' in review['question'] and review['question'].endswith('Want me to?')
    assert pending_offer.get_offer(message_id='chat-question') == {}
    delivery = {'accepted_dispatch_id': 'dispatch-1', 'provider_message_id': 'chat-question',
        'source_revision': 'a'*64, 'review_hash': review['review_hash'],
        'payload_hash': hashlib.sha256(review['question'].encode()).hexdigest(),
        'accepted_at_ms': int(WHEN.timestamp()*1000)}
    accepted = {**body, 'operation': 'record_offer', 'accepted_delivery': delivery}
    result = invoke(accepted)
    assert result['status'] == 'recorded' and result['accepted_at_ms'] == delivery['accepted_at_ms']
    assert invoke(accepted) == result
    assert invoke(dict(body, operation='inspect')) == result
    assert pending_offer.get_offer(message_id='chat-question')['meeting'] == review['meeting']
    assert len(reads) == 2  # initial source proof plus fresh proof before activation
    conflict = {**accepted, 'accepted_delivery': {**delivery, 'provider_message_id': 'other-question'}}
    assert invoke(conflict)['code'] == 'event_conflict'


@pytest.mark.parametrize('change', ['wrong_email', 'unowned', 'multiple', 'moved', 'revoked', 'partial'])
def test_chat_prep_proposal_source_failures_never_activate(context, rec, monkeypatch, change):
    invoke, event, reads = proposal_fixture(context, rec, monkeypatch)
    body = proposal_request()
    if change == 'wrong_email':
        body = proposal_request(attendee_email='other@outside.test')
    elif change == 'unowned':
        event['owner_participates'] = False
    elif change == 'multiple':
        event['attendees'].append({'email': 'other@outside.test'})
    elif change == 'moved':
        event['start']['dateTime'] = (WHEN+timedelta(hours=25)).isoformat()
    elif change == 'revoked':
        context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'b'*64, 'google_sources': []}
    elif change == 'partial':
        event['status'] = 'cancelled'
    if change == 'revoked':
        with pytest.raises(tool.UnavailableError):
            invoke(body)
    else:
        assert invoke(body) == {'version':1,'status':'rejected','code':'invalid_target'}
    assert pending_offer.get_offer(message_id='chat-question') == {}


@pytest.mark.parametrize('field,value', [('payload_hash','0'*64), ('source_revision','b'*64),
    ('review_hash','c'*64), ('accepted_at_ms',0)])
def test_chat_prep_proposal_refuses_changed_delivery(context, rec, monkeypatch, field, value):
    invoke, event, reads = proposal_fixture(context, rec, monkeypatch)
    body = proposal_request()
    review = invoke(body)['review']
    delivery = {'accepted_dispatch_id': 'dispatch-1', 'provider_message_id': 'chat-question',
        'source_revision': 'a'*64, 'review_hash': review['review_hash'],
        'payload_hash': hashlib.sha256(review['question'].encode()).hexdigest(),
        'accepted_at_ms': int(WHEN.timestamp()*1000), field:value}
    with pytest.raises(ValueError):
        invoke({**body, 'operation':'record_offer', 'accepted_delivery':delivery})
    assert pending_offer.get_offer(message_id='chat-question') == {}


def test_chat_prep_primary_alias_resolves_only_one_complete_exact_occurrence(context, rec, monkeypatch):
    import copy
    invoke, event, reads = proposal_fixture(context, rec, monkeypatch)
    calls = []
    def reader(operation, args):
        calls.append((operation, args))
        if operation == 'calendar_list':
            return {'items':[event], 'observation':{'complete':True}}
        row = copy.deepcopy(event)
        row['calendar_id'] = args['calendar_id']
        return {'item':row, 'observation':{'complete':True}}
    review = invoke(proposal_request(calendar_id='primary'), reader)['review']
    assert review['calendar_id'] == 'owner@inside.test'
    assert [operation for operation, _ in calls] == ['calendar_get','calendar_list','calendar_get']
    from datetime import datetime
    window = calls[1][1]
    assert (datetime.fromisoformat(window['end']) - datetime.fromisoformat(window['start'])).total_seconds() == 2


@pytest.mark.parametrize('failure', ['ambiguous', 'partial', 'changed'])
def test_chat_prep_primary_alias_refuses_unproven_resolution(context, rec, monkeypatch, failure):
    import copy
    invoke, event, reads = proposal_fixture(context, rec, monkeypatch)
    def reader(operation, args):
        if operation == 'calendar_list':
            return {'items':[event, dict(event,calendar_id='shared@inside.test')] if failure=='ambiguous' else [event],
                    'observation':{'complete':failure!='partial'}}
        row = copy.deepcopy(event)
        row['calendar_id'] = args['calendar_id']
        if args['calendar_id'] != 'primary' and failure == 'changed':
            row['summary'] += ' changed'
        return {'item':row,'observation':{'complete':True}}
    if failure == 'partial':
        with pytest.raises(tool.UnavailableError):
            invoke(proposal_request(calendar_id='primary'), reader)
    else:
        assert invoke(proposal_request(calendar_id='primary'), reader)['code']=='invalid_target'
    assert pending_offer.get_offer(message_id='chat-question') == {}


def test_chat_prep_proposal_refuses_revocation_after_calendar_read(context, rec, monkeypatch):
    invoke, event, reads = proposal_fixture(context, rec, monkeypatch)
    def reader(operation,args):
        context[1]['_source_authority'] = lambda: {'complete':True,'revision':'b'*64,'google_sources':[]}
        return {'item':event,'observation':{'complete':True}}
    with pytest.raises(tool.UnavailableError):
        invoke(proposal_request(),reader)
    assert pending_offer.get_offer(message_id='chat-question') == {}


def test_chat_prep_accepted_receipt_repairs_activation_with_fresh_source_without_new_expiry(context, rec, monkeypatch):
    import delivery_effects
    invoke, event, reads = proposal_fixture(context, rec, monkeypatch)
    body = proposal_request()
    preview = invoke(body)
    review = preview['review']
    path = context[0]/'events/owner-meeting-prep-offers.json'
    expiry = json.loads(path.read_text())['actions'][body['event_id']]['offer']['expires_at']
    delivery = {'accepted_dispatch_id':'dispatch-1','provider_message_id':'chat-question',
        'source_revision':'a'*64,'review_hash':review['review_hash'],
        'payload_hash':hashlib.sha256(review['question'].encode()).hexdigest(),
        'accepted_at_ms':int(WHEN.timestamp()*1000)}
    original = delivery_effects.finalize
    monkeypatch.setattr(delivery_effects,'finalize',lambda *a,**k:False)
    assert invoke({**body,'operation':'record_offer','accepted_delivery':delivery})['status']=='unknown'
    assert pending_offer.get_offer(message_id='chat-question') == {}
    monkeypatch.setattr(delivery_effects,'finalize',original)
    monkeypatch.setattr(pending_offer,'_now',lambda:WHEN+timedelta(minutes=10))
    assert invoke({**body,'operation':'inspect'})['status']=='unknown'
    assert len(reads)==2
    assert invoke({**body,'operation':'record_offer','accepted_delivery':delivery})['status']=='recorded'
    assert len(reads)==3  # each unconfirmed activation gets a current read; recorded replay does not
    assert pending_offer.get_offer(message_id='chat-question')['expires_at']==expiry


@pytest.mark.parametrize('change', ['moved','cancelled','title','attendee','unowned'])
def test_chat_prep_offer_revalidates_current_occurrence_before_activation(context, rec, monkeypatch, change):
    invoke, event, reads = proposal_fixture(context, rec, monkeypatch)
    body = proposal_request()
    review = invoke(body)['review']
    if change == 'moved':
        event['start']['dateTime']=(WHEN+timedelta(hours=25)).isoformat()
    elif change == 'cancelled':
        event['status']='cancelled'
    elif change == 'title':
        event['summary']='Different meeting'
    elif change == 'attendee':
        event['attendees'][1]['email']='other@outside.test'
    else:
        event['owner_participates']=False
    delivery={'accepted_dispatch_id':'dispatch-1','provider_message_id':'chat-question',
        'source_revision':'a'*64,'review_hash':review['review_hash'],
        'payload_hash':hashlib.sha256(review['question'].encode()).hexdigest(),
        'accepted_at_ms':int(WHEN.timestamp()*1000)}
    assert invoke({**body,'operation':'record_offer','accepted_delivery':delivery})['code']=='invalid_target'
    assert pending_offer.get_offer(message_id='chat-question') == {}


def test_expired_chat_prep_proposals_are_pruned_and_old_owner_proof_cannot_renew(context, rec, monkeypatch):
    invoke, event, reads=proposal_fixture(context, rec, monkeypatch)
    body=proposal_request()
    invoke(body)
    path=context[0]/'events/owner-meeting-prep-offers.json'
    doc=json.loads(path.read_text())
    row=doc['actions'][body['event_id']]
    doc['actions']={str(i):row for i in range(1024)}
    path.write_text(json.dumps(doc))
    late=WHEN+timedelta(hours=4)
    monkeypatch.setattr(pending_offer,'_now',lambda:late)
    monkeypatch.setattr(tool,'_proposal_now',lambda:late)
    assert invoke(body)['code']=='invalid_target'
    assert json.loads(path.read_text())['actions']=={}
    fresh={**body,'event_id':'8'*64,'admitted_at_ms':int(late.timestamp()*1000)}
    canonical=json.dumps(fresh['args'],sort_keys=True,ensure_ascii=False,separators=(',',':'))
    fresh['action_id']=hashlib.sha256(json.dumps([fresh['event_id'],'owner_meeting_prep_offer',canonical],
        ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    assert invoke(fresh)['status']=='preview'
    assert len(json.loads(path.read_text())['actions'])==1
    assert len(reads)==2


def _accepted_proposal(body, preview, at=None):
    delivery = {'accepted_dispatch_id':'dispatch-1','provider_message_id':'chat-question',
        'source_revision':'a'*64,'review_hash':preview['review']['review_hash'],
        'payload_hash':hashlib.sha256(preview['review']['question'].encode()).hexdigest(),
        'accepted_at_ms': int(WHEN.timestamp()*1000) if at is None else at}
    return {**body,'operation':'record_offer','accepted_delivery':delivery}


def test_chat_prep_activation_crash_receipt_recovers_without_reactivating_or_calendar(context, rec, monkeypatch):
    invoke,event,reads=proposal_fixture(context,rec,monkeypatch)
    body=proposal_request(); preview=invoke(body); accepted=_accepted_proposal(body,preview)
    result=invoke(accepted)
    path=context[0]/'events/owner-meeting-prep-offers.json'
    doc=json.loads(path.read_text()); doc['actions'][body['event_id']]['recorded']=False
    path.write_text(json.dumps(doc))
    event['status']='cancelled'
    assert invoke({**body,'operation':'inspect'})==result
    assert len(reads)==2
    assert pending_offer.get_offer(message_id='chat-question')['offer_id']==preview['review']['offer_id']


def test_recorded_chat_prep_receipt_survives_offer_expiry_without_renewal(context, rec, monkeypatch):
    invoke,event,reads=proposal_fixture(context,rec,monkeypatch)
    body=proposal_request(); result=invoke(_accepted_proposal(body,invoke(body)))
    late=WHEN+timedelta(hours=4)
    monkeypatch.setattr(tool,'_proposal_now',lambda:late)
    monkeypatch.setattr(pending_offer,'_now',lambda:late)
    assert invoke({**body,'operation':'inspect'})==result
    assert pending_offer.get_offer(message_id='chat-question')=={}
    assert len(reads)==2


@pytest.mark.parametrize('offset_ms',[1,3*60*60*1000])
def test_chat_prep_rejects_future_or_expired_acceptance_timestamp(context,rec,monkeypatch,offset_ms):
    invoke,event,reads=proposal_fixture(context,rec,monkeypatch)
    body=proposal_request(); preview=invoke(body)
    with pytest.raises(ValueError,match='acceptance invalid'):
        invoke(_accepted_proposal(body,preview,int(WHEN.timestamp()*1000)+offset_ms))
    assert pending_offer.get_offer(message_id='chat-question')=={}


def test_chat_preview_does_not_replace_previously_delivered_offer(context,rec,monkeypatch):
    invoke,event,reads=proposal_fixture(context,rec,monkeypatch)
    old=pending_offer.set_offer('meeting_prep','Prepare the other meeting?',person='Other Person',prepare_only=True)
    pending_offer.activate_offer(old,{'message_id':'previous-question','target':'photon','chat_id':'iMessage;-;+15555550123','accepted_at':WHEN.timestamp()})
    before=pending_offer.get_offer(message_id='previous-question')
    invoke(proposal_request())
    assert pending_offer.get_offer(message_id='previous-question')==before


def test_chat_prep_same_instant_different_offset_remains_same_occurrence(context,rec,monkeypatch):
    from datetime import datetime,timezone
    invoke,event,reads=proposal_fixture(context,rec,monkeypatch)
    body=proposal_request(); preview=invoke(body)
    original=datetime.fromisoformat(event['start']['dateTime'])
    event['start']['dateTime']=original.astimezone(timezone(timedelta(hours=5,minutes=30))).isoformat()
    assert invoke(_accepted_proposal(body,preview))['status']=='recorded'
    assert pending_offer.get_offer(message_id='chat-question')['meeting']==preview['review']['meeting']


def test_chat_prep_missing_preview_is_permanent_but_bad_proof_and_transport_are_not(context,rec,monkeypatch):
    invoke,event,reads=proposal_fixture(context,rec,monkeypatch)
    body=proposal_request()
    missing={**body,'operation':'record_offer','accepted_delivery':{
        'accepted_dispatch_id':'dispatch-1','provider_message_id':'chat-question','source_revision':'a'*64,
        'review_hash':'c'*64,'payload_hash':'d'*64,'accepted_at_ms':int(WHEN.timestamp()*1000)}}
    assert invoke(missing)=={'version':1,'status':'rejected','code':'invalid_target'}
    assert reads==[]
    with pytest.raises(ValueError,match='acceptance invalid'):
        invoke({**missing,'accepted_delivery':{}})
    with pytest.raises(ValueError,match='hash'):
        invoke({**body,'text_hash':'0'*64})
    def failed_reader(*args):
        raise RuntimeError('synthetic temporary provider outage')
    with pytest.raises(RuntimeError,match='temporary provider outage'):
        invoke(body,failed_reader)


@pytest.mark.parametrize('provider_start', [
    {'date':'2026-10-30'}, {'dateTime':'not-a-date'}, {'dateTime':'2026-10-30T10:00:00'}, None])
def test_provider_changed_to_untimed_or_malformed_start_is_permanent_target_rejection(context,rec,monkeypatch,provider_start):
    invoke,event,reads=proposal_fixture(context,rec,monkeypatch)
    body=proposal_request(); preview=invoke(body)
    event['start']=provider_start
    assert invoke(_accepted_proposal(body,preview))=={'version':1,'status':'rejected','code':'invalid_target'}
    assert pending_offer.get_offer(message_id='chat-question')=={}


@pytest.mark.parametrize('title', ['😀'*101, 'Cedar\x7f review'])
def test_provider_title_uses_client_utf16_and_control_bounds(context,rec,monkeypatch,title):
    invoke,event,reads=proposal_fixture(context,rec,monkeypatch)
    event['summary']=title
    assert invoke(proposal_request())=={'version':1,'status':'rejected','code':'invalid_target'}
    assert pending_offer.get_offer(message_id='chat-question')=={}


def test_malformed_owner_start_remains_bad_request_not_provider_rejection(context,rec,monkeypatch):
    invoke,event,reads=proposal_fixture(context,rec,monkeypatch)
    with pytest.raises(ValueError):
        invoke(proposal_request(start='not-a-date'))
    assert reads==[]


def test_original_admission_repairs_record_offer_after_61_minutes_without_renewal(context,rec,monkeypatch):
    import delivery_effects
    invoke,event,reads=proposal_fixture(context,rec,monkeypatch)
    body=proposal_request(); preview=invoke(body); accepted=_accepted_proposal(body,preview)
    path=context[0]/'events/owner-meeting-prep-offers.json'
    expiry=json.loads(path.read_text())['actions'][body['event_id']]['offer']['expires_at']
    original=delivery_effects.finalize
    monkeypatch.setattr(delivery_effects,'finalize',lambda *args,**kwargs:False)
    assert invoke(accepted)['status']=='unknown'
    later=WHEN+timedelta(minutes=61)
    monkeypatch.setattr(tool,'_proposal_now',lambda:later)
    monkeypatch.setattr(pending_offer,'_now',lambda:later)
    monkeypatch.setattr(delivery_effects,'finalize',original)
    assert invoke(accepted)['status']=='recorded'  # original owner admission and actual delivery unchanged
    live=pending_offer.get_offer(message_id='chat-question')
    assert live['expires_at']==expiry and live['offer_id']==preview['review']['offer_id']
    assert len(reads)==3  # preview, initial activation proof, fresh proof on the delayed repair
