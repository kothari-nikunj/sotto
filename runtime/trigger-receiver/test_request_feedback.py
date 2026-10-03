"""Owner feedback for terminal requested briefs, without model or live channel calls."""
import importlib.util
import json
import time
from pathlib import Path

import test_requested_brief as fixtures

HERE, rec, request = fixtures.HERE, fixtures.rec, fixtures.request


def _finish(rec, job_id, status='failed', *, error='private failure text'):
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status=?,payload='{}',result=NULL,error=?,finished=? WHERE id=?",
                   (status, error, time.time(), job_id))


def _rows(rec):
    path = Path(rec.OUTBOX.path())
    return json.loads(path.read_text()).get('rows', []) if path.exists() else []


def _original(rec, job_id, *, status, acceptance='not_attempted', created=None):
    path = Path(rec.OUTBOX.path())
    path.parent.mkdir(parents=True, exist_ok=True)
    original = {'id': job_id, 'kind': 'brief', 'status': status,
                'created_at': time.time() if created is None else created,
                'day': '2026-10-02', 'acceptance': acceptance,
                'acceptance_uncertain': acceptance in ('unknown', 'in_flight'),
                'payload': {'label': 'requested:fixture:sotto-evening-brief',
                            'presentation': {'summary': 'private original'}}}
    path.write_text(json.dumps({'rows': [original]}))


def test_ingress_feedback_is_atomic_content_free_and_pending_work_is_silent(rec):
    code, response = rec.request_owner_brief(request())
    assert code == 200
    job = rec.WORK_QUEUE.get(rec.DATA, response['request_id'])
    assert job['status'] == 'pending'
    assert job['feedback']  # stored on the job at admission, before a worker can claim it
    assert set(json.loads(job['feedback'])) == {'label', 'owner', 'chat_id', 'target'}
    assert 'prompt' not in job['feedback'] and 'message-1' not in job['feedback']
    rec._requested_brief_feedback()
    assert _rows(rec) == []
    assert rec.WORK_QUEUE.get(rec.DATA, job['id'])['feedback'] is not None


def test_expiry_before_claim_gets_one_durable_notice_and_no_raw_error(rec, monkeypatch):
    job_id = rec.request_owner_brief(request())[1]['request_id']
    # claim performs this expiry without ever handing a job to a worker.
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute('UPDATE jobs SET valid_until=? WHERE id=?', (1, job_id))
    assert rec.WORK_QUEUE.claim(rec.DATA, 'offline-worker') is None
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['status'] == 'expired'
    sent = []
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send',
                        lambda body, target: sent.append((body, target)) or
                        (True, '', {'message_id': 'offline-ack'}))
    rec._requested_brief_feedback()
    rows = _rows(rec)
    assert len(rows) == 1 and rows[0]['status'] == 'pending'
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None
    assert rows[0]['payload']['run_id'] == rows[0]['id']
    assert job_id not in rows[0]['payload']['body']
    assert 'private' not in json.dumps(rows)
    rec.OUTBOX.drain()
    assert len(sent) == 1 and sent[0][1] == 'photon'
    assert 'requested evening brief' in sent[0][0]
    assert _rows(rec)[0]['status'] == 'delivered'

    # A fresh receiver loads the same stores after a restart; feedback is not recreated or resent.
    spec = importlib.util.spec_from_file_location('restarted_feedback_receiver', HERE / 'receiver.py')
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    monkeypatch.setattr(restarted, 'DATA', rec.DATA)
    monkeypatch.setitem(restarted.OUTBOX.HOOKS, 'send',
                        lambda *_: (_ for _ in ()).throw(AssertionError('duplicate notice')))
    restarted._requested_brief_feedback()
    restarted.OUTBOX.drain()
    assert len(_rows(restarted)) == 1


def test_done_work_waits_for_delivery_and_accepted_original_is_silent(rec):
    job_id = rec.request_owner_brief(request())[1]['request_id']
    _finish(rec, job_id, status='done')
    _original(rec, job_id, status='pending')
    rec._requested_brief_feedback()
    assert len(_rows(rec)) == 1
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is not None

    _original(rec, job_id, status='delivered', acceptance='accepted')
    path = Path(rec.OUTBOX.path())
    state = json.loads(path.read_text())
    state['rows'][0]['effects_pending'] = True  # accepted delivery; failed effects are separate
    path.write_text(json.dumps(state))
    rec._requested_brief_feedback()
    assert len(_rows(rec)) == 1
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None


def test_done_work_with_no_output_reports_failure_without_exposing_diagnostic(rec, monkeypatch):
    job_id = rec.request_owner_brief(request())[1]['request_id']
    _finish(rec, job_id, status='done', error='secret@example.com private traceback')
    rec._requested_brief_feedback()
    rows = _rows(rec)
    assert len(rows) == 1
    assert 'requested evening brief' in rows[0]['payload']['body']
    assert 'secret@example.com' not in json.dumps(rows)

    calls = []
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send',
                        lambda body, target: calls.append(body) or
                        (False, 'offline', {'acceptance': 'rejected'}))
    rec.OUTBOX.drain()
    assert len(calls) == 1 and _rows(rec)[0]['status'] == 'pending'
    rec._requested_brief_feedback()
    assert len(_rows(rec)) == 1  # retry the same durable notice, never enqueue another
    path = Path(rec.OUTBOX.path())
    state = json.loads(path.read_text())
    state['rows'][0]['next_at'] = 0
    path.write_text(json.dumps(state))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send',
                        lambda body, target: calls.append(body) or
                        (True, '', {'message_id': 'recovered-ack'}))
    rec.OUTBOX.drain()
    assert len(calls) == 2 and calls[0] == calls[1]
    assert _rows(rec)[0]['status'] == 'delivered'


def test_accepted_original_suppresses_queued_notice_before_send(rec, monkeypatch):
    job_id = rec.request_owner_brief(request())[1]['request_id']
    _finish(rec, job_id)
    rec._requested_brief_feedback()
    assert len(_rows(rec)) == 1
    notice = _rows(rec)[0]
    _original(rec, job_id, status='delivered', acceptance='accepted')
    path = Path(rec.OUTBOX.path())
    state = json.loads(path.read_text())
    state['rows'].append(notice)
    path.write_text(json.dumps(state))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send',
                        lambda *_: (_ for _ in ()).throw(AssertionError('stale feedback sent')))
    rec.OUTBOX.drain()
    assert _rows(rec)[1]['status'] == 'superseded'


def test_unknown_gallery_gets_honest_notice_without_replaying_original(rec, monkeypatch):
    job_id = rec.request_owner_brief(request())[1]['request_id']
    _finish(rec, job_id, status='done')
    _original(rec, job_id, status='pending', acceptance='unknown', created=time.time() - 599)
    rec._requested_brief_feedback()
    assert len(_rows(rec)) == 1
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is not None
    _original(rec, job_id, status='pending', acceptance='unknown', created=time.time() - 601)
    before = _rows(rec)[0]
    rec._requested_brief_feedback()
    rows = _rows(rec)
    assert len(rows) == 2
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None
    assert rows[0] == before
    body = rows[1]['payload']['body'].lower()
    assert 'couldn\'t confirm' in body and 'check the conversation' in body
    assert 'arrived' in body and 'couldn\'t finish' not in body
    sends = []
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send_gallery',
                        lambda *_: (_ for _ in ()).throw(AssertionError('original gallery replayed')))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send',
                        lambda body, target: sends.append(body) or (True, '', {'message_id': 'notice-ack'}))
    rec.OUTBOX.drain()
    assert sends == [rows[1]['payload']['body']]
    assert _rows(rec)[0]['status'] == 'pending'


def test_pending_text_delivery_with_unknown_acceptance_remains_silent(rec):
    job_id = rec.request_owner_brief(request())[1]['request_id']
    _finish(rec, job_id, status='done')
    _original(rec, job_id, status='pending', acceptance='unknown', created=time.time() - 601)
    path = Path(rec.OUTBOX.path())
    state = json.loads(path.read_text())
    state['rows'][0]['payload'].pop('presentation')
    state['rows'][0]['payload']['body'] = 'still retrying original text'
    path.write_text(json.dumps(state))
    rec._requested_brief_feedback()
    assert len(_rows(rec)) == 1
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is not None


def test_owner_route_change_and_old_failures_do_not_generate_notifications(rec, monkeypatch):
    job_id = rec.request_owner_brief(request())[1]['request_id']
    _finish(rec, job_id)
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'new-owner')
    rec._requested_brief_feedback()
    assert _rows(rec) == []
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is not None
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'owner')
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute('UPDATE jobs SET finished=? WHERE id=?', (time.time() - 5 * 3600, job_id))
    rec._requested_brief_feedback()
    assert _rows(rec) == []
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None


def test_route_change_suppresses_notice_already_queued(rec, monkeypatch):
    job_id = rec.request_owner_brief(request())[1]['request_id']
    _finish(rec, job_id)
    rec._requested_brief_feedback()
    assert len(_rows(rec)) == 1
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', 'new-owner')
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send',
                        lambda *_: (_ for _ in ()).throw(AssertionError('old owner notified')))
    rec.OUTBOX.drain()
    assert _rows(rec)[0]['status'] == 'superseded'
