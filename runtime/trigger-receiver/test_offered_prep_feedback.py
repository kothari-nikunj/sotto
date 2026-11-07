"""Accepted prep offers use the same durable, route-bound terminal notices as research."""
import importlib.util
import json
import time

import pytest
from test_runtime_owner_meeting_prep_request import context as context, rec as rec, setup, call, request, route_proof  # noqa: PLC0414 — explicit pytest fixture re-exports
from test_request_feedback import _finish, _rows, _original, HERE

# Re-export the imported fixtures for pytest's name-based injection.
__all__ = ['context', 'rec']


@pytest.mark.parametrize('terminal', ['failed', 'expired'])
def test_offered_prep_failure_is_content_free_once_across_restart(context, rec, monkeypatch, terminal):
    setup(context, rec, monkeypatch)
    body = request()
    receipt = call(body, context, rec)
    job_id = receipt['outcome']['request_id']
    job = rec.WORK_QUEUE.get(rec.DATA, job_id)
    assert json.loads(job['feedback']) == {'label': job['payload']['label'],
        'owner': body['identity']['owner_id'], 'chat_id': 'iMessage;-;owner',
        'target': 'photon', 'route_digest': 'b'*64}
    rec._requested_brief_feedback()
    assert not _rows(rec)
    if terminal == 'expired':
        with rec.WORK_QUEUE._db(rec.DATA) as db:
            db.execute('UPDATE jobs SET valid_until=1 WHERE id=?', (job_id,))
        assert rec.WORK_QUEUE.claim(rec.DATA, 'offline-worker') is None
    else:
        _finish(rec, job_id, error='PRIVATE calendar identity and traceback')
    rec._requested_brief_feedback()
    rows = _rows(rec)
    assert len(rows) == 1
    payload = rows[0]['payload']
    assert payload['body'] == "I couldn't finish your requested meeting prep. I won't keep retrying this request."
    assert 'PRIVATE' not in json.dumps(rows) and 'Dana' not in json.dumps(rows)
    assert rec._delivery_valid(payload)
    # Only content-free failure feedback may survive source revocation.
    monkeypatch.setattr(rec, '_retained_proof_current', lambda _: False)
    assert rec._delivery_valid(payload)
    sent = []
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *args: sent.append(args) or
        (True, '', {'message_id': 'notice-ack'}))
    rec.OUTBOX.drain()
    assert len(sent) == 1 and _rows(rec)[0]['status'] == 'delivered'
    spec = importlib.util.spec_from_file_location('restarted_offered_prep_receiver', HERE / 'receiver.py')
    restarted = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(restarted)
    monkeypatch.setattr(restarted, 'DATA', rec.DATA)
    monkeypatch.setattr(restarted, '_candidate_retained_route_proof', route_proof)
    monkeypatch.setitem(restarted.OUTBOX.HOOKS, 'send', lambda *_: pytest.fail('notice replay'))
    restarted._requested_brief_feedback()
    restarted.OUTBOX.drain()
    assert len(_rows(restarted)) == 1
    assert call(dict(body, operation='inspect'), context, rec) == receipt
    assert rec.WORK_QUEUE.claim(rec.DATA, 'no-work-replay') is None


@pytest.mark.parametrize('changes', [None, {'route_digest': 'c'*64},
    {'provider_thread_id': 'other-thread'}, {'owner_id': 'someone-else'}])
def test_offered_prep_requires_notice_route_before_admission(context, rec, monkeypatch, changes):
    setup(context, rec, monkeypatch)
    monkeypatch.setattr(rec, '_candidate_retained_route_proof',
        lambda: None if changes is None else route_proof(**changes))
    assert call(request(), context, rec)['code'] == 'unavailable'
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, 'd'*64) is None
    assert not _rows(rec)


def test_offered_prep_route_checked_at_reconcile_and_delivery(context, rec, monkeypatch):
    setup(context, rec, monkeypatch)
    job_id = call(request(), context, rec)['outcome']['request_id']
    _finish(rec, job_id)
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: route_proof(route_digest='c'*64))
    rec._requested_brief_feedback()
    assert not _rows(rec)
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', route_proof)
    rec._requested_brief_feedback()
    assert len(_rows(rec)) == 1
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: route_proof(route_digest='c'*64))
    assert not rec._delivery_valid(_rows(rec)[0]['payload'])


@pytest.mark.parametrize('acceptance', ['accepted', 'unknown', 'not_attempted'])
def test_offered_prep_original_delivery_is_never_replayed(context, rec, monkeypatch, acceptance):
    setup(context, rec, monkeypatch)
    job_id = call(request(), context, rec)['outcome']['request_id']
    _finish(rec, job_id, status='done')
    _original(rec, job_id, status='delivered' if acceptance == 'accepted' else 'pending',
              acceptance=acceptance, created=time.time()-601)
    before = _rows(rec)[0]
    rec._requested_brief_feedback()
    rows = _rows(rec)
    assert rows[0] == before
    assert len(rows) == (2 if acceptance == 'unknown' else 1)
    if acceptance == 'unknown':
        assert "couldn't confirm whether your requested meeting prep arrived" in rows[1]['payload']['body']
    assert rec.WORK_QUEUE.claim(rec.DATA, 'no-work-replay') is None


def test_legacy_failed_offered_prep_is_not_backfilled(context, rec, monkeypatch):
    setup(context, rec, monkeypatch)
    body = request()
    receipt = call(body, context, rec)
    job_id = receipt['outcome']['request_id']
    _finish(rec, job_id)
    rec.WORK_QUEUE.clear_feedback(rec.DATA, job_id)
    rec._requested_brief_feedback()
    assert call(body, context, rec) == receipt
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is None
    assert not _rows(rec)
