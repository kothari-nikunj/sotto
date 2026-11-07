"""Original owner proof admits the actual declared shared skill through the work queue."""
import hashlib
import json
import time
from pathlib import Path
import pytest

import runtime_owner_skill_request as tool
from test_runtime_owner_loop_close import context, IDENTITY, ROOT, WHEN, THREAD, capability
from test_requested_brief import rec

__all__ = ['context', 'rec']


def request(selection=None, *, event='d'*64, operation='admit', text=None):
    args = {'skill': 'meeting-prep', 'selection': selection or {'mode': 'sweep'}}
    if text is None:
        text = 'Prep Dana' if args['selection'].get('mode') == 'focus' else 'Prep my meetings'
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    action = hashlib.sha256(json.dumps([event, 'owner_skill_request', canonical],
                                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return dict(version=1, operation=operation, identity=IDENTITY, action_id=action, event_id=event,
                payload_hash='b'*64, text_hash=hashlib.sha256(text.encode()).hexdigest(), text=text,
                admitted_at_ms=int(WHEN.timestamp()*1000), provider_thread_id=THREAD, args=args)


def route_proof(**changes):
    return {'version': 1, 'status': 'current_channel_proved', 'action_authorized': False,
            'owner_route': {'platform': 'imessage', 'chat_id': 'iMessage;-;owner'},
            'provider_thread_id': THREAD, 'route_digest': 'b'*64,
            **{key: IDENTITY[key] for key in
               ('tenant_id', 'project_fingerprint', 'owner_id', 'account_id', 'line_key')},
            **changes}


def setup(context, rec, monkeypatch):
    data, policy, activation = context
    policy.update(SOTTO_DATA=str(data), SOTTO_RUNTIME_PROFILE='managed-photon-fixed-five-v1')
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', IDENTITY['owner_id'])
    monkeypatch.setattr(rec, '_activation_tick', lambda: None)
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', route_proof)
    monkeypatch.setattr(rec, '_composition_evidence', lambda label: {'source_revision': 'a'*64, 'route_digest': 'b'*64})
    return lambda body: tool.perform(body, bearer=capability.derive(ROOT, IDENTITY), root=ROOT,
        policy=policy, data_root=data, capability=capability, activation=activation,
        queue=rec.WORK_QUEUE, admit=rec.request_owner_skill)


def test_sweep_and_owner_focus_durable_queue_original_inspect_terminal_scrub(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    for event, selection in [('d'*64, {'mode': 'sweep'}), ('e'*64, {'mode': 'focus', 'focus': 'Dana'})]:
        body = request(selection, event=event)
        receipt = call(body)
        assert receipt['status'] == 'committed'
        job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
        procedure = json.loads(job['payload']['prompt'])
        assert procedure['kind'] == 'shared-meeting-prep' and procedure['selection'] == selection
        assert procedure['work_key'] == 'skill:'+event
        assert job['id'] == rec.WORK_QUEUE.job_id_for('run', 'skill:'+event)
        assert call(body) == receipt
        with rec.WORK_QUEUE._db(rec.DATA) as db:
            db.execute("UPDATE jobs SET status='done',payload='{}',result=NULL WHERE id=?", (job['id'],))
        assert call(dict(body, operation='inspect')) == receipt
        assert call(request({'mode':'focus','focus':'Dana'},event=event,text='Different Dana request'))['code']=='event_conflict'


def test_skill_closed_args_focus_span_and_source_refusal(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    for selection in ({'mode':'sweep','path':'/tmp/x'},
                      {'mode':'focus','focus':'Dana\n'}):
        with pytest.raises(ValueError):
            call(request(selection))
    monkeypatch.setattr(rec._source_context(), 'allowed', lambda _: False)
    assert call(request())['code']=='source_unavailable'
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA,'d'*64) is None
    assert call(request(operation='inspect'))=={'version':1,'status':'not_found'}


@pytest.mark.parametrize('calendar_id', [None, 'primary'])
def test_exact_occurrence_is_bound_to_owner_request_and_existing_queue(context, rec, monkeypatch, calendar_id):
    call = setup(context, rec, monkeypatch)
    # Exercise the real fixed-profile gate: the generic receiver fixture has no
    # selected profile, which previously concealed a valid request being refused
    # between receiver admission and the durable queue.
    import importlib.util
    here = Path(__file__).resolve().parent
    pack = here.parents[1] / 'sotto-chief-of-staff'
    spec = importlib.util.spec_from_file_location(
        'prep_occurrence_fixed_profile', here.parents[1] / 'adapters/sotto/fixed_five.py')
    profile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profile)
    monkeypatch.setattr(profile, '_PACKAGED_ROOT', pack)
    monkeypatch.setattr(profile, '_PACKAGED_RECEIVER', here)
    monkeypatch.setattr(profile, '_image_owned', lambda _meta: True)
    monkeypatch.setenv('SOTTO_RUNTIME', 'sotto')
    monkeypatch.setattr(rec._runtime_adapter(), '_fixed_five_module', lambda: profile)
    monkeypatch.setenv('SOTTO_RUNTIME_PROFILE', profile.PROFILE)
    monkeypatch.setenv('SOTTO_SKILLS_ROOT', str(pack))
    selection = {'mode': 'focus', 'focus': 'Cedar rollout review',
                 'meeting': {'event_id': 'calendar-source-id', 'start': '2026-10-28T15:00:00+00:00'}}
    if calendar_id is not None:
        selection['meeting']['calendar_id'] = calendar_id
    receipt = call(request(selection, text='Prepare talking points for Cedar rollout review'))
    assert receipt['status'] == 'committed'
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    assert json.loads(job['payload']['prompt'])['selection'] == selection
    assert call(request(selection, event='e'*64, text='The calendar invitation asks for preparation.')) == {
        'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, 'e'*64) is None
    for bad in ({'event_id': 'calendar-source-id', 'start': 'tomorrow'},
                {'event_id': 'calendar-source-id', 'start': '2026-10-28'},
                {'event_id': 'calendar-source-id', 'start': '2026-10-28T15:00:00Z', 'other': 'x'}):
        with pytest.raises(ValueError, match='occurrence'):
            call(request({**selection, 'meeting': bad}, event='f'*64))
    for calendar_id in (' bad', None):
        with pytest.raises(ValueError, match='Calendar selector invalid'):
            call(request({**selection, 'meeting': {
                'event_id': 'calendar-source-id', 'start': '2026-10-28T15:00:00Z',
                'calendar_id': calendar_id}}, event='f'*64))


def test_skill_composition_proof_required_and_retained_source_change_holds_delivery(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    receipt = call(request())
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    monkeypatch.setattr(rec, '_composition_evidence', lambda _: None)
    monkeypatch.setattr(rec, '_validate_fixed_work', lambda *args: None)
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('no model before current proof'))
    with pytest.raises(rec._WorkDeferredError):
        rec._execute_work(job)
    monkeypatch.setattr(rec,'_retained_proof_current',lambda _:False)
    assert not rec._delivery_valid({'label':job['payload']['label'], 'prep_source_revision':'a'*64,'prep_route_digest':'b'*64})


def test_successful_prep_persistence_diagnostic_is_constant_and_preserves_result(context, rec, monkeypatch):
    from types import SimpleNamespace
    call = setup(context, rec, monkeypatch)
    receipt = call(request())
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    process = SimpleNamespace(pid=999999, returncode=0, poll=lambda:0,
        communicate=lambda **kwargs: ('Actual prep', '[meeting-prep] research persistence failed; prep remains available\nPRIVATE ERROR CONTENT'))
    monkeypatch.setattr(rec.subprocess,'Popen',lambda *args,**kwargs:process)
    recorded=[]
    monkeypatch.setattr(rec,'_record_delivery',lambda *args,**kwargs:recorded.append((args,kwargs)))
    result=rec._execute_work(job)
    assert result['text']=='Actual prep'
    diagnostics=[row for row in recorded if row[0][1]=='research_persistence_failed']
    assert len(diagnostics)==1 and 'PRIVATE' not in str(diagnostics)


def test_ask_person_original_owner_question_no_calendar_gate_and_same_event_fence(context, rec, monkeypatch):
    call=setup(context,rec,monkeypatch)
    monkeypatch.setattr(rec._source_context(),'allowed',lambda _:False)
    body=request({'mode':'person','person':'Dana'},text='Deep research on Dana')
    body['args']['skill']='ask-research'
    canonical=json.dumps(body['args'],sort_keys=True,ensure_ascii=False,separators=(',',':'))
    body['action_id']=hashlib.sha256(json.dumps([body['event_id'],'owner_skill_request',canonical],
        ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    receipt=call(body)
    assert receipt['outcome']['skill']=='ask-research'
    job=rec.WORK_QUEUE.get(rec.DATA,receipt['outcome']['request_id'])
    procedure=json.loads(job['payload']['prompt'])
    assert procedure['kind']=='shared-ask-research' and procedure['question']==body['text']
    assert job['payload']['label'].endswith(':ask-research')
    assert json.loads(job['feedback']) == {
        'label': job['payload']['label'], 'owner': IDENTITY['owner_id'],
        'chat_id': 'iMessage;-;owner', 'target': 'photon', 'route_digest': 'b'*64}
    assert call(dict(body,operation='inspect'))==receipt
    assert call(request())['code']=='event_conflict'
    for person in ('someone else','Dana\n'):
        with pytest.raises(ValueError):
            tool.validate_args({'skill':'ask-research','selection':{'mode':'person','person':person}},body['text'])


def test_research_terminal_work_notifies_once_without_private_failure_or_recontact(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    body = request({'mode':'person','person':'Dana'}, text='Deep research on Dana')
    body['args']['skill'] = 'ask-research'
    canonical = json.dumps(body['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    job_id = call(body)['outcome']['request_id']
    rec._requested_brief_feedback()
    assert not Path(rec.OUTBOX.path()).exists()  # queued still means work is in progress
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='failed',payload='{}',result=NULL,error=?,finished=? WHERE id=?",
                   ('private research failure and prompt', time.time(), job_id))
    rec._requested_brief_feedback()
    rows = json.loads(Path(rec.OUTBOX.path()).read_text())['rows']
    assert len(rows) == 1
    assert rows[0]['payload']['body'].startswith("I couldn't finish your requested research")
    assert 'ask me for it again' not in rows[0]['payload']['body']
    assert 'private research failure' not in json.dumps(rows)
    assert body['text'] not in json.dumps(rows)
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None
    rec._requested_brief_feedback()
    assert len(json.loads(Path(rec.OUTBOX.path()).read_text())['rows']) == 1


def test_research_accepted_result_suppresses_failure_notice(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    body = request({'mode':'company','company':'Cedar'}, text='Research Cedar')
    body['args']['skill'] = 'ask-research'
    canonical = json.dumps(body['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    job_id = call(body)['outcome']['request_id']
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='done',payload='{}',result=NULL,finished=? WHERE id=?",
                   (time.time(), job_id))
    path = Path(rec.OUTBOX.path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'rows': [{'id': job_id, 'status': 'delivered', 'acceptance': 'accepted'}]}))
    rec._requested_brief_feedback()
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None
    assert len(json.loads(path.read_text())['rows']) == 1


def test_research_saved_result_hands_off_once_and_waits_for_delivery(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    body = request({'mode':'company','company':'Cedar'}, text='Research Cedar')
    body['args']['skill'] = 'ask-research'
    canonical = json.dumps(body['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    job_id = call(body)['outcome']['request_id']
    output = 'Cedar makes collaboration tools.'
    result = {'text': output, 'label': 'skill:' + body['event_id'] + ':ask-research',
              'source_revision_at_composition': 'a'*64, 'route_digest_at_composition': 'b'*64,
              'source_text_sha256': hashlib.sha256(output.encode()).hexdigest()}
    monkeypatch.setattr(rec, '_execute_work', lambda _: result)
    monkeypatch.setattr(rec, '_retained_proof_current', lambda _: True)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send',
                        lambda text, target, *_identity, **_context: (True, '', {'message_id': 'fixture-ack'}))
    job = rec.WORK_QUEUE.claim(rec.DATA, 'research-worker')
    assert job['id'] == job_id
    rec._work_one(job)
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['status'] == 'done'
    rows = json.loads(Path(rec.OUTBOX.path()).read_text())['rows']
    assert len(rows) == 1 and rows[0]['id'] == job_id and rows[0]['status'] == 'delivered'
    rec._requested_brief_feedback()
    assert len(json.loads(Path(rec.OUTBOX.path()).read_text())['rows']) == 1
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None


def test_research_unknown_delivery_gets_uncertain_notice_without_replay(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    body = request({'mode':'company','company':'Cedar'}, text='Research Cedar')
    body['args']['skill'] = 'ask-research'
    canonical = json.dumps(body['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    job_id = call(body)['outcome']['request_id']
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='done',payload='{}',result=NULL,finished=? WHERE id=?",
                   (time.time() - 700, job_id))
    path = Path(rec.OUTBOX.path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'rows': [{'id': job_id, 'status': 'pending', 'acceptance': 'unknown',
        'acceptance_uncertain': True, 'created_at': time.time() - 700,
        'payload': {'label': 'skill:' + body['event_id'] + ':ask-research',
                    'presentation': {'summary': 'private research'}}}]}))
    rec._requested_brief_feedback()
    rows = json.loads(path.read_text())['rows']
    assert len(rows) == 2 and rows[0]['id'] == job_id
    assert "couldn't confirm" in rows[1]['payload']['body']
    assert 'private research' not in rows[1]['payload']['body']
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None


def test_text_only_research_unknown_delivery_is_held_and_noticed_once(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    body = request({'mode':'company','company':'Cedar'}, text='Research Cedar')
    body['args']['skill'] = 'ask-research'
    canonical = json.dumps(body['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    job_id = call(body)['outcome']['request_id']
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='done',payload='{}',result=NULL,finished=? WHERE id=?",
                   (time.time() - 700, job_id))
    path = Path(rec.OUTBOX.path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'rows': [{'id': job_id, 'status': 'pending', 'acceptance': 'unknown',
        'acceptance_uncertain': True, 'created_at': time.time() - 590,
        'payload': {'label': 'skill:' + body['event_id'] + ':ask-research',
                    'body': 'Private text-only research result'}}]}))
    rec._requested_brief_feedback()
    assert len(json.loads(path.read_text())['rows']) == 1  # ten-minute ambiguity window
    pending = json.loads(path.read_text())
    pending['rows'][0]['created_at'] = time.time() - 700
    path.write_text(json.dumps(pending))
    rec._requested_brief_feedback()
    rows = json.loads(path.read_text())['rows']
    assert len(rows) == 2 and rows[0]['id'] == job_id and rows[0]['status'] == 'pending'
    assert rows[0]['acceptance'] == 'unknown'
    assert rows[1]['payload']['feedback_for'] == job_id
    assert "couldn't confirm whether your requested research arrived" in rows[1]['payload']['body']
    assert 'Private text-only research result' not in rows[1]['payload']['body']
    assert rec.OUTBOX._claim(job_id, time.time(), '2026-10-09')[0] == 'held'
    rec._requested_brief_feedback()
    assert len(json.loads(path.read_text())['rows']) == 2
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None


def test_uncontacted_text_result_still_waits_for_original_delivery(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    job_id = call(request())['outcome']['request_id']
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='done',payload='{}',result=NULL,finished=? WHERE id=?",
                   (time.time() - 700, job_id))
    path = Path(rec.OUTBOX.path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'rows': [{'id': job_id, 'status': 'pending',
        'acceptance': 'not_attempted', 'acceptance_uncertain': False,
        'created_at': time.time() - 700,
        'payload': {'label': 'skill:' + 'd'*64 + ':meeting-prep', 'body': 'Pending result'}}]}))
    rec._requested_brief_feedback()
    assert len(json.loads(path.read_text())['rows']) == 1
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is not None


def test_completed_proactive_scan_with_no_output_is_silent(context, rec, monkeypatch):
    setup(context, rec, monkeypatch)
    label = 'skill:' + 'd'*64 + ':proactive'
    job_id = rec._spawn_and_deliver([rec.sys.executable, str(Path(rec.__file__).with_name('procedure_runner.py'))],
        json.dumps({'kind':'proactive'}), label, feedback={
            'label': label, 'owner': IDENTITY['owner_id'], 'chat_id': 'iMessage;-;owner', 'target': 'photon', 'route_digest': 'b'*64})
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='done',payload='{}',result=NULL,finished=? WHERE id=?",
                   (time.time(), job_id))
    rec._requested_brief_feedback()
    assert json.loads(Path(rec.OUTBOX.path()).read_text())['rows'] == []
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None


@pytest.mark.parametrize('skill', ['meeting-prep', 'followup'])
def test_completed_skill_with_no_output_says_finished_not_failed(context, rec, monkeypatch, skill):
    # Empty prep or "nothing to follow up" is a successful run; it must not read as a failure.
    setup(context, rec, monkeypatch)
    label = 'skill:' + 'e'*64 + ':' + skill
    job_id = rec._spawn_and_deliver([rec.sys.executable, str(Path(rec.__file__).with_name('procedure_runner.py'))],
        json.dumps({'kind': skill}), label, feedback={
            'label': label, 'owner': IDENTITY['owner_id'], 'chat_id': 'iMessage;-;owner', 'target': 'photon', 'route_digest': 'b'*64})
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='done',payload='{}',result=NULL,finished=? WHERE id=?",
                   (time.time(), job_id))
    rec._requested_brief_feedback()
    rows = json.loads(Path(rec.OUTBOX.path()).read_text())['rows']
    assert len(rows) == 1
    assert rows[0]['payload']['body'].startswith('I finished your requested ')
    assert "couldn't" not in rows[0]['payload']['body']


def test_research_expiry_before_claim_still_gets_terminal_notice(context, rec, monkeypatch):
    setup(context, rec, monkeypatch)
    code, response = rec.request_owner_skill({'mode':'person','person':'Dana'},
        work_key='skill:' + 'f'*64, admission={'event_id':'f'*64}, skill='ask-research',
        question='Research Dana', feedback_identity=IDENTITY, feedback_thread_id=THREAD)
    assert code == 200
    job_id = response['request_id']
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute('UPDATE jobs SET valid_until=? WHERE id=?', (1, job_id))
    assert rec.WORK_QUEUE.claim(rec.DATA, 'offline-worker') is None
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['status'] == 'expired'
    rec._requested_brief_feedback()
    rows = json.loads(Path(rec.OUTBOX.path()).read_text())['rows']
    assert len(rows) == 1 and 'requested research' in rows[0]['payload']['body']
    rec._requested_brief_feedback()
    assert len(json.loads(Path(rec.OUTBOX.path()).read_text())['rows']) == 1


def test_followup_fixed_lanes_granola_grant_dedupe_and_proof_env(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    authority = {'complete': True, 'connector_sources': ['granola']}
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: authority)
    for index, mode in enumerate(('recent', 'backlog')):
        body = request({'mode': mode}, event=str(index+1)*64, text='Follow up on my meetings')
        body['args']['skill'] = 'followup'
        canonical = json.dumps(body['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical],
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        receipt = call(body)
        job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
        procedure = json.loads(job['payload']['prompt'])
        assert procedure['kind'] == 'shared-followup' and procedure['selection'] == {'mode': mode}
        assert rec._job_key(job['payload']['label'], job['payload']['prompt']) == 'skill:'+body['event_id']
        assert call(dict(body, operation='inspect')) == receipt
    from types import SimpleNamespace
    captured = []
    process = SimpleNamespace(pid=999999, returncode=0, poll=lambda: 0,
        communicate=lambda **kwargs: ('Follow-up output', ''))
    monkeypatch.setattr(rec.subprocess, 'Popen', lambda *args, **kwargs: captured.append(kwargs['env']) or process)
    rec._execute_work(job)
    assert json.loads(captured[0]['SOTTO_SKILL_COMPOSITION_PROOF']) == {
        'source_revision': 'a'*64, 'route_digest': 'b'*64}
    authority['connector_sources'] = []
    assert rec.request_owner_skill({'mode': 'recent'}, skill='followup', work_key='skill:'+'f'*64,
        admission=None) == (409, {'status': 'meeting_notes_not_connected'})
    monkeypatch.setattr(rec.SELFHOST_SOURCE_AUTHORITY, 'granola_generation', lambda _: '1'*32)
    assert rec.request_owner_skill({'mode': 'recent'}, skill='followup', work_key='skill:'+'f'*64,
        admission=None) == (409, {'status': 'source_unavailable'})
    authority.update(complete=False, connector_sources=['granola'])
    assert rec.request_owner_skill({'mode': 'recent'}, skill='followup', work_key='skill:'+'f'*64,
        admission=None) == (409, {'status': 'source_unavailable'})
    for selection in ({'mode': 'other'}, {'mode': 'recent', 'path': '/tmp/x'}):
        with pytest.raises(ValueError):
            tool.validate_args({'skill': 'followup', 'selection': selection})


def test_relationship_pulse_admits_without_private_lane_and_reopens(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {'complete':True,'revision':'a'*64,
                                                                'bridge_sources':[],'google_sources':[]})
    body = request(text='Show my relationship pulse')
    body['args'] = {'skill':'relationship-pulse','selection':{'mode':'current'}}
    canonical = json.dumps(body['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'],'owner_skill_request',canonical],
                                                separators=(',', ':')).encode()).hexdigest()
    receipt = call(body)
    assert receipt['outcome']['skill'] == 'relationship-pulse'
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    assert json.loads(job['payload']['prompt'])['kind'] == 'shared-relationship-pulse'
    body['operation'] = 'inspect'
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {'complete':False})
    assert call(body) == receipt
    body['operation'] = 'admit'
    assert call(body) == receipt


@pytest.mark.parametrize('skill',['relationship-pulse','proactive'])
def test_relationship_pulse_retained_source_fence(context, rec, monkeypatch,skill):
    setup(context, rec, monkeypatch)
    label = 'skill:'+'d'*64+':'+skill
    monkeypatch.setattr(rec, '_retained_proof_current', lambda _:False)
    assert not rec._delivery_valid({'label':label,'prep_source_revision':'a'*64,'prep_route_digest':'b'*64})


def test_proactive_current_admits_original_queue_and_replays_before_authority(context,rec,monkeypatch):
    call=setup(context,rec,monkeypatch)
    monkeypatch.setattr(rec,'_runtime_source_authority',lambda:{'complete':True,'revision':'a'*64})
    body=request(text='Check for anything urgent')
    body['args']={'skill':'proactive','selection':{'mode':'current'}}
    canonical=json.dumps(body['args'],sort_keys=True,ensure_ascii=False,separators=(',',':'))
    body['action_id']=hashlib.sha256(json.dumps([body['event_id'],'owner_skill_request',canonical],
        ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    receipt=call(body)
    assert receipt['status']=='committed'
    job=rec.WORK_QUEUE.get(rec.DATA,receipt['outcome']['request_id'])
    assert job['payload']['label']=='skill:'+body['event_id']+':proactive'
    assert json.loads(job['payload']['prompt'])['kind']=='proactive'
    monkeypatch.setattr(rec,'_runtime_source_authority',lambda:{'complete':False})
    assert call(dict(body,operation='inspect'))==receipt
    assert call(body)==receipt
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==1


def proactive_request(text):
    body = request(text=text)
    body['args'] = {'skill': 'proactive', 'selection': {'mode': 'current'}}
    canonical = json.dumps(body['args'], sort_keys=True, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps(
        [body['event_id'], 'owner_skill_request', canonical], separators=(',', ':')).encode()).hexdigest()
    return body


@pytest.mark.parametrize('text', ['Hello, Sotto.', '"check for anything urgent"',
    "Don't check for anything urgent", 'Tell me about an urgent meeting'])
def test_nonurgent_proactive_refused_without_job(context, rec, monkeypatch, text):
    call = setup(context, rec, monkeypatch)
    body = proactive_request(text)
    assert call(body) == {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    assert call(dict(body, operation='inspect')) == {'version': 1, 'status': 'not_found'}
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, body['event_id']) is None
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


def test_historical_nonurgent_proactive_receipt_remains_inspectable_without_readmission(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {'complete': True, 'revision': 'a'*64})
    body = proactive_request('Hello, Sotto.')
    # Seed an exact pre-fix receipt, then restore current admission rules.
    with monkeypatch.context() as old:
        old.setattr(tool, 'is_owner_urgent_check', lambda _: True)
        receipt = call(body)
    assert receipt['status'] == 'committed'
    monkeypatch.setattr(rec, 'request_owner_skill', lambda *args, **kwargs: pytest.fail('must not readmit'))
    call = setup(context, rec, monkeypatch)
    assert call(dict(body, operation='inspect')) == receipt
    assert call(body) == receipt
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1


@pytest.mark.parametrize('text', ['Check for anything urgent', ' please CHECK for anything urgent! ',
    'Could you please check for anything urgent?', 'Check for anything urgent please.'])
def test_proactive_original_trigger_polite_variants(text):
    assert tool.is_owner_urgent_check(text)


def test_company_selection_exact_owner_span_and_same_original_queue(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    body = request(text='Research Cedar Labs')
    body['args'] = {'skill':'ask-research', 'selection':{'mode':'company','company':'Cedar Labs'}}
    canonical = json.dumps(body['args'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    receipt = call(body)
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    procedure = json.loads(job['payload']['prompt'])
    assert procedure['selection'] == body['args']['selection']
    assert procedure['kind'] == 'shared-ask-research' and procedure['question'] == body['text']
    assert call(dict(body, operation='inspect')) == receipt
    for company in ('Different company', 'Cedar Labs\n', 'Cedar\x85Labs', 'Cedar\u2028Labs'):
        with pytest.raises(ValueError):
            tool.validate_args({'skill':'ask-research', 'selection':{'mode':'company','company':company}}, body['text'])


@pytest.mark.parametrize('text', ['what does tomorrow look like?', '"pulse pls"', "don't run a pulse", 'she asked for a relationship pulse', '"who am I losing touch with"', "don't tell me who's waiting on me", 'she asked who am I losing touch with'])
def test_nonpulse_current_text_cannot_queue_pulse(context, rec, monkeypatch, text):
    call = setup(context, rec, monkeypatch)
    body = proactive_request(text)
    body['args']['skill'] = 'relationship-pulse'
    canonical = json.dumps(body['args'], sort_keys=True, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical], separators=(',', ':')).encode()).hexdigest()
    assert call(body) == {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, body['event_id']) is None


@pytest.mark.parametrize('text', ['quick relationship pulse?', 'pulse pls', 'Show my relationship pulse', 'please run a fresh pulse', 'who am I losing touch with?', "who haven't I talked to", "who haven’t I talked to?", "who's waiting on me", 'who is waiting on me please'])
def test_pulse_current_intent_shorthand(text):
    assert tool.is_owner_relationship_pulse_request(text)


@pytest.mark.parametrize('text', ['what does tomorrow look like?', 'yes',
    '"who do I owe a reply?"', "don't run follow-ups", 'she asked who do I owe a reply',
    'follow up but do not run it'])
def test_followup_tool_output_cannot_authorize_unrequested_work(context, rec, monkeypatch, text):
    call = setup(context, rec, monkeypatch)
    body = request(text=text)
    body['args'] = {'skill': 'followup', 'selection': {'mode': 'recent'}}
    canonical = json.dumps(body['args'], sort_keys=True, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps(
        [body['event_id'], 'owner_skill_request', canonical], separators=(',', ':')).encode()).hexdigest()
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {'complete': True, 'connector_sources': ['granola']})
    assert call(body) == {'version': 1, 'status': 'rejected', 'code': 'needs_context'}
    assert call(dict(body, operation='inspect')) == {'version': 1, 'status': 'not_found'}
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, body['event_id']) is None
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
    assert not Path(rec.OUTBOX.path()).exists()

def test_historical_followup_receipt_remains_inspectable_without_readmission(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: {'complete': True, 'revision': 'a'*64, 'connector_sources': ['granola']})
    body = request(text='Hello, Sotto.')
    body['args'] = {'skill': 'followup', 'selection': {'mode': 'recent'}}
    canonical = json.dumps(body['args'], sort_keys=True, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical], separators=(',', ':')).encode()).hexdigest()
    # Seed an exact pre-fix receipt, then restore current admission rules.
    with monkeypatch.context() as old:
        old.setattr(tool, 'is_owner_followup_request', lambda _: True)
        receipt = call(body)
    assert receipt['status'] == 'committed'
    monkeypatch.setattr(rec, 'request_owner_skill', lambda *args, **kwargs: pytest.fail('must not readmit'))
    call = setup(context, rec, monkeypatch)
    assert call(dict(body, operation='inspect')) == receipt
    assert call(body) == receipt
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1



# Kept identical to the runtime's lists in owner-skill-request.test.ts.
@pytest.mark.parametrize('text', [
    'who do I owe a reply?', 'Who do I owe replies to?', 'show my follow-ups pls',
    'Please run my backlog followups.', 'Follow up on my meetings',
    'what did I promise in yesterday’s meetings?',
    'Could you list my recent follow-ups?', 'what did I commit to',
    'open items from my Granola notes', 'post-meeting follow-ups',
    'anything to send after my meetings', 'any followups?', 'what follow-ups do I have?',
    'can you please run my follow-ups', 'run my follow-ups again', 'run a follow-up',
    'follow up on today’s meetings',
])
def test_affirmative_followup_phrasing(text):
    assert tool.is_owner_followup_request(text)


@pytest.mark.parametrize('text', [
    'follow up with Sam by email', 'check my follow-up with Sam', "don't run follow-ups",
    'never run my follow-ups', 'no follow-ups', 'stop follow-ups', 'did you run my follow-ups?',
    'should I run follow-ups?', 'she asked for my follow-ups', '"any followups?"',
    '"what did I promise in yesterday’s meetings?"',
    'what did I commit to Dana', 'any follow-ups from Dana?', 'follow up',
    'follow up but do not run it', 'yes',
])
def test_followup_phrasing_that_is_not_a_current_request(text):
    assert not tool.is_owner_followup_request(text)


def company_request(text, *, event='c'*64, company='Tinfoil', prior=None, revision='a'*64):
    body = request(text=text, event=event)
    body['args'] = {'skill': 'ask-research', 'selection': {'mode': 'company', 'company': company},
                    'source_revision': revision}
    if prior:
        body['args']['prior_selection'] = {key: prior[key] for key in ('event_id', 'action_id', 'args_hash')}
    canonical = json.dumps(body['args'], sort_keys=True, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([event, 'owner_skill_request', canonical], separators=(',', ':')).encode()).hexdigest()
    return body


@pytest.mark.parametrize('scenario', ['valid', 'restart', 'different', 'ambiguous', 'revoked', 'thread', 'stale', 'forged', 'redirect'])
def test_company_continuation_requires_receiver_saved_authority(context, rec, monkeypatch, scenario):
    call = setup(context, rec, monkeypatch)
    context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'a'*64}
    first = company_request('tell me about Tinfoil and Cedar' if scenario == 'ambiguous' else 'tell me about Tinfoil')
    receipt = call(first)
    assert receipt['status'] == 'committed'
    body = company_request('check their website too', event='d'*64, prior=receipt)
    if scenario == 'restart':
        call = setup(context, rec, monkeypatch)
    if scenario == 'different':
        body = company_request('check Cedar website', event='d'*64, company='Cedar')
    if scenario == 'revoked':
        context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'b'*64}
    if scenario == 'stale':
        body['admitted_at_ms'] += 900001
    if scenario in ('thread', 'forged', 'redirect'):
        with rec.WORK_QUEUE._db(rec.DATA) as db:
            row = db.execute('SELECT admission FROM jobs WHERE id=?', (receipt['outcome']['request_id'],)).fetchone()
            saved = json.loads(row[0])
            if scenario == 'thread':
                saved['selected_company']['thread_digest'] = tool.selection_digest('other-thread')
            if scenario == 'forged':
                saved['selected_company']['identity_digest'] = 'f'*64
            if scenario == 'redirect':
                saved['selected_company']['company_digest'] = tool.selection_digest('Cedar')
            db.execute('UPDATE jobs SET admission=? WHERE id=?', (json.dumps(saved), receipt['outcome']['request_id']))
    result = call(body)
    if scenario in ('valid', 'restart', 'different', 'ambiguous'):
        assert result['status'] == 'committed'
        assert call(body) == result
        context[1]['_source_authority'] = lambda: {'complete': False}
        assert call(dict(body, operation='inspect')) == result
        assert call(body) == result
        job = rec.WORK_QUEUE.get(rec.DATA, result['outcome']['request_id'])
        assert json.loads(job['payload']['prompt'])['selection']['company'] == ('Cedar' if scenario == 'different' else 'Tinfoil')
    else:
        assert result['status'] == 'rejected'
        assert rec.WORK_QUEUE.admission_for_event(rec.DATA, body['event_id']) is None
        with rec.WORK_QUEUE._db(rec.DATA) as db:
            assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1


@pytest.mark.parametrize('text', ['check out their website', 'can you check their website?', 'their site', 'check their website - anything else worth knowing?', 'anything on their site that changes this?', 'look into that a bit more', 'what about their customers?', 'check it and give me the short version'])
@pytest.mark.parametrize('legacy', [False, True])
def test_company_admission_uses_digest_metadata_and_accepts_legacy_receipts(context, rec, monkeypatch, text, legacy):
    call = setup(context, rec, monkeypatch)
    context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'a'*64}
    first = company_request('tell me about Tinfoil')
    receipt = call(first)
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        row = db.execute('SELECT admission FROM jobs WHERE id=?', (receipt['outcome']['request_id'],)).fetchone()
        saved = json.loads(row[0])
        assert 'Tinfoil' not in row[0] and THREAD not in row[0]
        assert saved['selected_company']['company_digest'] == tool.selection_digest('Tinfoil')
        if legacy:
            selected = saved['selected_company']
            selected.pop('company_digest')
            selected.pop('thread_digest')
            selected.update(company='Tinfoil', provider_thread_id=THREAD)
            db.execute('UPDATE jobs SET admission=? WHERE id=?', (json.dumps(saved), receipt['outcome']['request_id']))
    assert call(dict(first, operation='inspect')) == receipt
    result = call(company_request(text, event='e'*64, prior=receipt))
    assert result['status'] == 'committed'
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 2


@pytest.mark.parametrize('text', ['check their website - anything else worth knowing?',
    'look into that a bit more', 'what about their customers?'])
def test_company_followup_without_grounded_subject_is_not_admitted(text):
    with pytest.raises(ValueError, match='Ask company invalid'):
        tool.validate_args({'skill': 'ask-research', 'selection': {'mode': 'company', 'company': 'Tinfoil'}}, text)


@pytest.mark.parametrize('scenario', ['valid', 'company-first', 'ambiguous', 'extra-clause', 'revoked', 'thread', 'stale', 'future', 'same-event', 'redirect'])
def test_inline_company_followup_bound_turn_proof(context, rec, monkeypatch, scenario):
    call = setup(context, rec, monkeypatch)
    context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'a'*64}
    body = company_request('their site too?', event='d'*64)
    prior = {'event_id': 'c'*64, 'text': 'Can you write me a blurb for Tinfoil for our IC tomorrow? What they do etc. short 3-4 sentences',
             'admitted_at_ms': body['admitted_at_ms'] - 1000, 'accepted_dispatch_id': 'accepted-inline',
             'source_revision': 'a'*64, 'thread_digest': tool.selection_digest(THREAD)}
    if scenario == 'company-first':
        prior['text'] = 'Tinfoil. give me a short blurb for IC'
    if scenario == 'ambiguous':
        prior['text'] = 'write me a blurb for Tinfoil and Cedar for IC'
    if scenario == 'extra-clause':
        prior['text'] = 'write me a blurb for Tinfoil. send an email too'
    if scenario == 'revoked':
        prior['source_revision'] = 'b'*64
    if scenario == 'thread':
        prior['thread_digest'] = tool.selection_digest('different')
    if scenario == 'stale':
        prior['admitted_at_ms'] = body['admitted_at_ms'] - 900001
    if scenario == 'future':
        prior['admitted_at_ms'] = body['admitted_at_ms'] + 1
    if scenario == 'same-event':
        prior['event_id'] = body['event_id']
    if scenario == 'redirect':
        body['args']['selection']['company'] = 'Cedar'
    body['args']['prior_selection'] = prior
    canonical = json.dumps(body['args'], sort_keys=True, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical], separators=(',', ':')).encode()).hexdigest()
    result = call(body)
    if scenario in ('valid', 'company-first', 'ambiguous', 'extra-clause'):
        assert result['status'] == 'committed'
        job = rec.WORK_QUEUE.get(rec.DATA, result['outcome']['request_id'])
        assert json.loads(job['payload']['prompt'])['question'] == (
            'their site too?\nSubject from accepted conversation: Tinfoil')
        assert body['text'] == 'their site too?'
        assert call(body) == result
        context[1]['_source_authority'] = lambda: {'complete': False}
        assert call(dict(body, operation='inspect')) == result
    else:
        assert result['status'] == 'rejected'
        assert rec.WORK_QUEUE.admission_for_event(rec.DATA, body['event_id']) is None




@pytest.mark.parametrize('text', ['prep me for that pls', 'full prep on Tanya'])
def test_contextual_prep_selection_is_queued_exactly_once(context, rec, monkeypatch, text):
    call = setup(context, rec, monkeypatch)
    body = request({'mode': 'focus', 'focus': 'Tanya Reed'}, text=text)
    receipt = call(body)
    assert receipt['status'] == 'committed'
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    assert json.loads(job['payload']['prompt'])['selection'] == {'mode': 'focus', 'focus': 'Tanya Reed'}
    assert call(dict(body, operation='inspect')) == receipt
    assert call(body) == receipt
    assert call(request({'mode': 'focus', 'focus': 'Different Person'}, text=text))['code'] == 'event_conflict'


def test_historical_prep_receipt_remains_inspectable(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    body = request({'mode': 'focus', 'focus': 'on Tanya'}, text='full prep on Tanya')
    receipt = call(body)
    assert receipt['status'] == 'committed'
    assert call(dict(body, operation='inspect')) == receipt
    assert call(body) == receipt






@pytest.mark.parametrize('text', ['thanks', 'ok', 'what time is my dentist?', 'Do not prep Dana', 'She said prep Dana', 'She said prep my meetings', 'When should I prep my meetings?', '"prep Dana"', 'Cancel meeting prep'])
def test_no_prep_without_current_intent(text):
    assert not tool.is_owner_meeting_prep_request(text, 'focus')
    assert not tool.is_owner_meeting_prep_request(text, 'sweep')


@pytest.mark.parametrize('text', ['prep went well', 'prep is done, thanks', 'prep for Dana was great, thanks', 'Prep looked really good', 'the prep was helpful'])
def test_a_remark_about_finished_prep_is_not_a_focus_request(text):
    assert not tool.is_owner_meeting_prep_request(text, 'focus')


@pytest.mark.parametrize('text', ['prep me for my 3pm', 'prep for Dana', 'Prep Spencer Kim', 'prep for the call Dana was asking about'])
def test_focus_requests_that_mention_a_past_event_still_admit(text):
    assert tool.is_owner_meeting_prep_request(text, 'focus')




def test_explicit_company_website_operation_reaches_procedure(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'a'*64}
    prior = call(company_request('tell me about Tinfoil'))
    body = company_request('check their website - anything else worth knowing?', event='e'*64, prior=prior)
    body['args']['selection']['operation'] = 'website'
    canonical = json.dumps(body['args'], sort_keys=True, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical], separators=(',', ':')).encode()).hexdigest()
    result = call(body)
    assert result['status'] == 'committed'
    job = rec.WORK_QUEUE.get(rec.DATA, result['outcome']['request_id'])
    assert json.loads(job['payload']['prompt'])['selection']['operation'] == 'website'
    third = call(company_request('what about their customers?', event='f'*64, prior=result))
    assert third['status'] == 'committed'
    assert third['outcome']['request_id'] != result['outcome']['request_id']


@pytest.mark.parametrize('mode,subject,text,prior_text,allowed', [
    ('person', 'Dana Reed', 'look into her a little more', 'tell me about Dana Reed', True),
    ('person', 'Dana Reed', 'ok thanks', 'tell me about Dana Reed', False),
    ('person', 'Dana Reed', 'yes', 'Dana Reed has not replied. Want me to look her up?', False),
    ('person', 'Dana Reed', 'sure', 'Dana Reed, Alex Stone and John Smith are waiting.', False),
    ('company', '12 Oak St lease renewal', 'look into it', 'my 12 Oak St lease renewal', False),
    ('person', 'Dana Reed', 'look into her', "don't look up Dana Reed", False),
    ('person', 'Dana Reed', 'look into her', 'tell me about Dana Reedson', False),
    ('person', '12 Oak St lease, $4,200 due Friday', 'look into it', '12 Oak St lease, $4,200 due Friday', False),
    ('company', 'vendor.example', 'check their website', 'bob@vendor.example', False),
    ('company', "don't research Cedar", 'check their website', "don't research Cedar", False),
])
def test_receiver_checks_read_request_and_identity(context, rec, monkeypatch, mode, subject, text, prior_text, allowed):
    call = setup(context, rec, monkeypatch)
    context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'a'*64}
    body = company_request(text, event='d'*64)
    body['args']['selection'] = {'mode': mode, mode: subject}
    body['args']['prior_selection'] = {'event_id': 'c'*64, 'text': prior_text,
        'admitted_at_ms': body['admitted_at_ms'] - 1000, 'accepted_dispatch_id': 'accepted-inline',
        'source_revision': 'a'*64, 'thread_digest': tool.selection_digest(THREAD)}
    canonical = json.dumps(body['args'], sort_keys=True, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical], separators=(',', ':')).encode()).hexdigest()
    result = call(body)
    assert result['status'] == ('committed' if allowed else 'rejected')
    if not allowed:
        assert rec.WORK_QUEUE.admission_for_event(rec.DATA, body['event_id']) is None


FOCUS_PREP_VECTORS = Path(__file__).resolve().parent.parent / 'sotto-chat/test/fixtures/owner-focus-prep-vectors.json'


def test_focus_prep_matches_shared_node_vectors():
    if not FOCUS_PREP_VECTORS.is_file():
        pytest.skip('shared focus prep vectors are not present')
    for vector in json.loads(FOCUS_PREP_VECTORS.read_text(encoding='utf-8')):
        assert tool.is_owner_meeting_prep_request(vector['text'], 'focus') is vector['focus'], vector['text']


def test_owner_lowercase_company_reaches_exact_packaged_request(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    body = request(text="check cedar labs website, what's new?")
    body['args'] = {'skill': 'ask-research', 'selection': {
        'mode': 'company', 'company': 'Cedar Labs', 'operation': 'website'}}
    canonical = json.dumps(body['args'], sort_keys=True, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps(
        [body['event_id'], 'owner_skill_request', canonical], separators=(',', ':')).encode()).hexdigest()
    receipt = call(body)
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    procedure = json.loads(job['payload']['prompt'])
    # The packaged runner binds its exact selected target, not a capitalization guess.
    assert procedure['selection']['company'] in procedure['question']
    assert procedure['question'].startswith(body['text'])
    assert call(dict(body, operation='inspect')) == receipt

@pytest.mark.parametrize('scenario', ['domain', 'homepage', 'stale', 'future', 'revoked', 'thread', 'cancel', 'thanks', 'query', 'different'])
def test_research_domain_answer_keeps_prior_owner_request(context, rec, monkeypatch, scenario):
    call = setup(context, rec, monkeypatch)
    context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'a'*64}
    text = 'https://spurtest.example/' if scenario == 'homepage' else 'spurtest.example'
    if scenario == 'query':
        text = 'https://spurtest.example/?private=secret'
    body = company_request(text, event='d'*64, company='other.example' if scenario == 'different' else 'spurtest.example')
    prior = {'event_id': 'c'*64, 'text': 'Queue research for Spur?',
             'admitted_at_ms': body['admitted_at_ms'] - 1000, 'accepted_dispatch_id': 'accepted-request',
             'source_revision': 'a'*64, 'thread_digest': tool.selection_digest(THREAD)}
    if scenario == 'stale':
        prior['admitted_at_ms'] = body['admitted_at_ms'] - 900001
    if scenario == 'future':
        prior['admitted_at_ms'] = body['admitted_at_ms'] + 1
    if scenario == 'revoked':
        prior['source_revision'] = 'b'*64
    if scenario == 'thread':
        prior['thread_digest'] = tool.selection_digest('other')
    if scenario == 'cancel':
        prior['text'] = "don't research Spur"
    if scenario == 'thanks':
        prior['text'] = 'thanks'
    body['args']['prior_request'] = prior
    canonical = json.dumps(body['args'], sort_keys=True, separators=(',', ':'))
    body['action_id'] = hashlib.sha256(json.dumps([body['event_id'], 'owner_skill_request', canonical], separators=(',', ':')).encode()).hexdigest()
    result = call(body)
    assert result['status'] == ('committed' if scenario in ('domain', 'homepage') else 'rejected')
    if result['status'] == 'committed':
        job = rec.WORK_QUEUE.get(rec.DATA, result['outcome']['request_id'])
        payload = json.loads(job['payload']['prompt'])
        assert 'Queue research for Spur?' in payload['question']
        assert 'spurtest.example' in payload['question']
