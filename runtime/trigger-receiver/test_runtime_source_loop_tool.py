"""Canonical source corrections and exact interrupted local intent recovery."""
import json
import hashlib

import pytest
from test_runtime_owner_loop_tool import context, call, request, rows, ke
import runtime_owner_loop_tool as tool

__all__ = ['context']


@pytest.fixture(autouse=True)
def actual_writer_module(monkeypatch):
    monkeypatch.setattr(tool.loops, '_load_writer', lambda: ke)
    cr = ke._load_continuity()
    monkeypatch.setattr(ke, '_load_continuity', lambda: cr)


def source(context):
    from delivery_effects import loop_version, loop_identity
    cr = ke._load_continuity()
    row = {'anchor_key':'source-loop','action_type':'follow_up','channel':'email','status':'open',
        'created_at':'2026-10-05 10:00:00','summary':'send the deck','ask':'send the deck',
        'contact_name':'Dana','contact_identifier':'dana@example.com','source':'gmail',
        'source_message_id':'m1','source_thread_id':'thread1'}
    cr._persist(row)
    context[1]['_source_authority'] = lambda:{'complete':True,'revision':'a'*64,'google_sources':['gmail']}
    return {'anchor':row['anchor_key'],'loop_version':loop_version(row),'loop_identity':loop_identity(row),
            'source_revision':'a'*64,'correction':'Dana will send the deck'}


def body(binding, operation='retarget', event='e'*64, **changes):
    args = {**binding,'operation':operation, **({'direction':'them'} if operation=='retarget' else {'to':'resolved'}),**changes}
    return request(args,event=event,text='Dana will send the deck')


def test_source_retarget_and_original_receipt_after_round_trip(context):
    from delivery_effects import loop_version, loop_identity
    first = call(body(source(context)), context)
    assert first['operation']=='retarget' and first['direction']=='them'
    active = [row for row in rows() if row['status']=='open']
    assert len(active)==1 and active[0]['contact_identifier']=='dana@example.com'
    assert ke.classify_ledger_sources(active[0]) == ('owner',frozenset())
    binding = {'anchor':active[0]['anchor_key'],'loop_version':loop_version(active[0]),
        'loop_identity':loop_identity(active[0]),'source_revision':'a'*64,'correction':'Dana will send the deck'}
    second = call(body(binding,event='f'*64,direction='you'),context)
    assert second['direction']=='you'
    active = [row for row in rows() if row['status']=='open'][0]
    binding.update(anchor=active['anchor_key'],loop_version=loop_version(active),loop_identity=loop_identity(active))
    assert call(body(binding,operation='correct',event='b'*64),context)['loop_status']=='resolved'
    first_args=next(row for row in rows() if row['anchor_key']=='source-loop')['source_loop_correction']['proof']['args']
    assert call(request(first_args,event='e'*64,operation='inspect',text='Dana will send the deck'),context)==first


@pytest.mark.parametrize('failure', [1,2,3,4])
def test_each_retarget_write_interruption_has_no_two_active_and_inspect_finishes(context,monkeypatch,failure):
    bound=source(context)
    cr=ke._load_continuity()
    original=cr._persist
    count=[0]
    def fail(row,**kwargs):
        count[0]+=1
        original(row,**kwargs)
        if count[0]==failure:
            raise OSError('synthetic crash after fsynced write')
    monkeypatch.setattr(cr,'_persist',fail)
    with pytest.raises(OSError):
        call(body(bound),context)
    assert len([row for row in rows() if row['status']=='open']) <= 1
    monkeypatch.setattr(cr,'_persist',original)
    receipt=call(dict(body(bound),operation='inspect'),context)
    assert receipt['status']=='committed' and len([row for row in rows() if row['status']=='open'])==1
    before=[json.dumps(row,sort_keys=True,default=str) for row in rows()]
    assert call(dict(body(bound),operation='inspect'),context)==receipt
    assert before==[json.dumps(row,sort_keys=True,default=str) for row in rows()]


@pytest.mark.parametrize('status', ['resolved','dismissed'])
def test_correct_terminal_interruption_recovers_exact_plan(context,monkeypatch,status):
    bound=source(context); cr=ke._load_continuity(); original=cr._persist; count=[0]
    def fail(row,**kwargs):
        count[0]+=1; original(row,**kwargs)
        if count[0]==2:
            raise OSError('lost after terminate')
    monkeypatch.setattr(cr,'_persist',fail)
    req=body(bound,operation='correct',to=status)
    with pytest.raises(OSError):
        call(req,context)
    monkeypatch.setattr(cr,'_persist',original)
    assert call(dict(req,operation='inspect'),context)['loop_status']==status


def test_pending_reconnect_needs_complete_original_grants_then_inspect_once(context,monkeypatch):
    bound=source(context); cr=ke._load_continuity(); original=cr._persist
    def fail(row,**kwargs):
        original(row,**kwargs); raise OSError('lost after intent')
    monkeypatch.setattr(cr,'_persist',fail)
    req=body(bound)
    with pytest.raises(OSError):
        call(req,context)
    monkeypatch.setattr(cr,'_persist',original)
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'c'*64,'google_sources':[]}
    with pytest.raises(ke.EditError,match='intent remains held'):
        call(dict(req,operation='inspect'),context)
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'c'*64,'google_sources':['gmail']}
    done=call(dict(req,operation='inspect'),context)
    assert done['status']=='committed'
    context[1]['_source_authority']=lambda:{'complete':False}
    assert call(dict(req,operation='inspect'),context)==done


def test_changed_source_snapshot_and_contact_identifier_borrowing_refuse(context):
    bound=source(context)
    assert call(body({**bound,'loop_version':'f'*64}),context)['code']=='target_changed'
    corrected = {**bound,'correction':'James will send the deck'}
    req = body(corrected,contact='James',identifier='dana@example.com')
    req['text'] = corrected['correction']
    req['text_hash'] = hashlib.sha256(req['text'].encode()).hexdigest()
    assert call(req,context)['code']=='correction_unavailable'
    assert len(rows())==1 and rows()[0]['status']=='open'


def test_same_day_retired_replacement_collision_never_reopens(context):
    from delivery_effects import loop_version, loop_identity
    bound=source(context)
    call(body(bound),context)
    def binding():
        row=next(row for row in rows() if row['status']=='open')
        return {**bound,'anchor':row['anchor_key'],'loop_version':loop_version(row),'loop_identity':loop_identity(row)}
    call(body(binding(),event='f'*64,direction='you'),context)
    before=rows()
    assert call(body(binding(),event='b'*64,direction='them'),context)['code']=='anchor_collision'
    assert rows()==before


def test_mixed_dependency_requires_full_current_grants(context):
    bound=source(context); cr=ke._load_continuity()
    row=rows()[0]; row['source_refs']=[{'source':'granola','source_id':'meeting1'}]; cr._persist(row)
    from delivery_effects import loop_version, loop_identity
    bound.update(loop_version=loop_version(row),loop_identity=loop_identity(row))
    assert call(body(bound),context)['code']=='source_unavailable'
    context[1]['_source_authority']=lambda:{'complete':True,'revision':'a'*64,'google_sources':['gmail'],
                                           'connector_sources':['granola']}
    assert call(body(bound),context)['status']=='committed'


def test_retained_receipt_corruption_and_competing_terminalization_hold(context,monkeypatch):
    bound=source(context); cr=ke._load_continuity(); original=cr._persist
    def fail(row,**kwargs):
        original(row,**kwargs)
        raise OSError('after intent')
    monkeypatch.setattr(cr,'_persist',fail)
    req=body(bound)
    with pytest.raises(OSError):
        call(req,context)
    monkeypatch.setattr(cr,'_persist',original)
    row=rows()[0]; row.update(status='dismissed',resolution='user_other_correction',resolved_at='2026-10-05')
    cr._persist(row,source_loop_correction=row['source_loop_correction'])
    with pytest.raises(ke.EditError,match='retained state'):
        call(dict(req,operation='inspect'),context)
    assert len(rows())==1


def test_committed_receipt_validation_and_retention(context):
    from datetime import timedelta
    from test_runtime_owner_loop_close import WHEN
    bound=source(context); req=body(bound,operation='correct')
    done=call(req,context); cr=ke._load_continuity()
    cr.resolve({},now=WHEN+timedelta(days=90),merge=False,resolve_existing=False)
    assert len(rows())==1 and call(dict(req,operation='inspect'),context)==done
    row=rows()[0]; record=row['source_loop_correction']
    record['receipt']['loop_status']='dismissed'
    cr._persist(row,source_loop_correction=record)
    with pytest.raises(ke.EditError,match='receipt unavailable'):
        call(dict(req,operation='inspect'),context)


def test_committed_inspect_reestablishes_directory_durability_without_mutation(context,monkeypatch):
    req=body(source(context))
    done=call(req,context)
    cr=ke._load_continuity()
    original=cr._fsync_ledger_directory_tree
    before=rows()
    attempts=[]
    def fail():
        attempts.append('fsync')
        raise OSError('synthetic directory fsync failure')
    monkeypatch.setattr(cr,'_fsync_ledger_directory_tree',fail)
    with pytest.raises(OSError):
        call(dict(req,operation='inspect'),context)
    assert attempts==['fsync'] and rows()==before
    monkeypatch.setattr(cr,'_fsync_ledger_directory_tree',original)
    assert call(dict(req,operation='inspect'),context)==done and rows()==before


def _auto_closed(context):
    """A task loop_check closed on a booked meeting (signal_closes row shape)."""
    from delivery_effects import loop_version, loop_identity
    cr = ke._load_continuity()
    row = {'anchor_key': 'auto-loop', 'action_type': 'reply', 'channel': 'email', 'status': 'resolved',
           'created_at': '2026-10-05 10:00:00', 'summary': 'pick a time', 'ask': 'pick a time',
           'contact_name': 'Dana', 'contact_identifier': 'dana@example.com', 'source': 'gmail',
           'source_message_id': 'm1', 'source_thread_id': 'thread1', 'fulfillment': 'schedule',
           'resolution': 'scheduled_meeting', 'resolved_at': '2026-10-06', 'closed_at': '2026-10-06T09:00:00+00:00',
           'resolution_source_refs': [{'sourceType': 'calendar', 'sourceId': 'ev-1'}],
           'resolution_loop_version': 'f' * 64,
           'resolution_basis': {'basis': 'scheduled_meeting', 'source': 'calendar',
                                'at': '2026-10-06T09:00:00+00:00', 'recorded_at': '2026-10-06T09:05:00+00:00'}}
    cr._persist(row)
    context[1]['_source_authority'] = lambda: {'complete': True, 'revision': 'a' * 64,
                                               'google_sources': ['gmail', 'calendar']}
    return {'anchor': row['anchor_key'], 'loop_version': loop_version(row), 'loop_identity': loop_identity(row),
            'source_revision': 'a' * 64, 'correction': 'the Dana meeting is not booked'}


def test_owner_reopens_an_automatic_close_on_the_same_row(context):
    binding = _auto_closed(context)
    body = request({**binding, 'operation': 'correct', 'to': 'open'}, event='c' * 64,
                   text='No, the Dana meeting is not booked')
    receipt = call(body, context)
    assert receipt['operation'] == 'correct' and receipt['loop_status'] == 'open'
    matches = [row for row in rows() if row['anchor_key'] == 'auto-loop']
    assert len(matches) == 1 and len(rows()) == 1                       # no duplicate row
    row = matches[0]
    assert row['status'] == 'open' and 'resolution_basis' not in row and 'resolution' not in row
    assert row['rejected_signals'] == [{'basis': 'scheduled_meeting', 'source_id': 'ev-1'}]
    assert call(body, context) == receipt                                 # replay is idempotent
    import os
    with open(os.path.join(os.environ['SOTTO_DATA'], 'outcomes.jsonl'), encoding='utf-8') as handle:
        labels = [json.loads(line) for line in handle if '"loop_label"' in line]
    assert [(label['decision'], label['hint']) for label in labels] == [
        ('reopened', {'basis': 'scheduled_meeting', 'source': 'calendar'})]   # one label names the rejected signal


def test_reopen_refuses_a_task_the_owner_or_a_review_closed(context):
    binding = _auto_closed(context)
    cr = ke._load_continuity()
    row = cr._load_items()['auto-loop']
    row.pop('resolution_basis')
    row['resolution'] = 'replied'
    cr._persist(row)
    from delivery_effects import loop_version
    binding['loop_version'] = loop_version(row)
    body = request({**binding, 'operation': 'correct', 'to': 'open'}, event='c' * 64,
                   text='No, the Dana meeting is not booked')
    assert call(body, context)['code'] == 'target_unavailable'
    assert cr._load_items()['auto-loop']['status'] == 'resolved'


def _reopened(context):
    from delivery_effects import loop_version, loop_identity
    binding = _auto_closed(context)
    call(request({**binding, 'operation': 'correct', 'to': 'open'}, event='c' * 64,
                 text='No, the Dana meeting is not booked'), context)
    row = ke._load_continuity()._load_items()['auto-loop']
    return {**binding, 'loop_version': loop_version(row), 'loop_identity': loop_identity(row)}


def test_the_owner_can_close_a_reopened_task_by_hand(context):
    binding = _reopened(context)
    out = call(request({**binding, 'operation': 'correct', 'to': 'resolved',
                        'correction': 'the Dana meeting is done'}, event='d' * 64,
                       text='Actually the Dana meeting is done'), context)
    assert out.get('loop_status') == 'resolved', out


def test_a_second_wrong_automatic_close_can_be_undone(context):
    from delivery_effects import loop_version, loop_identity
    _reopened(context)
    cr = ke._load_continuity()
    row = cr._load_items()['auto-loop']
    row.update(status='resolved', resolution='scheduled_meeting', resolved_at='2026-10-08',
               closed_at='2026-10-08T09:00:00+00:00',
               resolution_source_refs=[{'sourceType': 'calendar', 'sourceId': 'ev-2'}],
               resolution_loop_version='e' * 64,
               resolution_basis={'basis': 'scheduled_meeting', 'source': 'calendar',
                                 'at': '2026-10-08T09:00:00+00:00', 'recorded_at': '2026-10-08T09:05:00+00:00'})
    cr._persist(row, signal_close='close')
    row = cr._load_items()['auto-loop']
    binding = {'anchor': 'auto-loop', 'loop_version': loop_version(row), 'loop_identity': loop_identity(row),
               'source_revision': 'a' * 64, 'correction': 'the Dana meeting is not booked'}
    out = call(request({**binding, 'operation': 'correct', 'to': 'open'}, event='d' * 64,
                       text='No, the Dana meeting is not booked'), context)
    assert out.get('loop_status') == 'open', out
    assert cr._load_items()['auto-loop']['rejected_signals'] == [
        {'basis': 'scheduled_meeting', 'source_id': 'ev-1'}, {'basis': 'scheduled_meeting', 'source_id': 'ev-2'}]
