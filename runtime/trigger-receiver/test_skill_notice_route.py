"""Content-free skill notices retain owner route proof, never an owner-name alias."""
import importlib.util
import json
import time
from pathlib import Path

import pytest

from test_runtime_owner_skill_request import context, rec, setup, request, route_proof, IDENTITY

__all__ = ['context', 'rec']


def failed_request(context, rec, monkeypatch):
    call = setup(context, rec, monkeypatch)
    body = request()
    receipt = call(body)
    job_id = receipt['outcome']['request_id']
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET status='failed',payload='{}',result=NULL,error=?,finished=? WHERE id=?",
                   ('PRIVATE source and provider failure', time.time(), job_id))
    return call, body, receipt, job_id


def inspect_only(rec, monkeypatch, reason):
    real = rec.OUTBOX.enqueue_work_feedback
    monkeypatch.setattr(rec.OUTBOX, 'enqueue_work_feedback', lambda job, enqueue=True:
                        pytest.fail(reason) if enqueue else real(job, enqueue=False))


def legacy(rec, job_id):
    meta = json.loads(rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'])
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        meta.pop('route_digest')
        meta['chat_id'] = IDENTITY['owner_id']
        db.execute('UPDATE jobs SET feedback=? WHERE id=?', (json.dumps(meta), job_id))
    return meta


def delivery_notes(rec):
    path = Path(rec.DATA) / 'events' / 'delivery.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def rows(rec):
    path = Path(rec.OUTBOX.path())
    return json.loads(path.read_text())['rows'] if path.exists() else []


def restart(rec, monkeypatch):
    spec = importlib.util.spec_from_file_location('skill_notice_restarted_receiver', rec.__file__)
    reopened = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reopened)
    monkeypatch.setattr(reopened, '_local_now', rec._local_now)
    monkeypatch.setattr(reopened, '_candidate_retained_route_proof', route_proof)
    return reopened


BAD_ROUTES = [None, route_proof(owner_id='other-owner'), route_proof(account_id='other-account'),
              route_proof(tenant_id='other-tenant'), route_proof(line_key='other-line'),
              route_proof(project_fingerprint='c'*64), route_proof(provider_thread_id='other-thread'),
              route_proof(route_digest='c'*64),
              route_proof(owner_route={'platform': 'imessage', 'chat_id': ''})]


@pytest.mark.parametrize('proof', BAD_ROUTES)
def test_admission_requires_matching_current_route_before_queue(context, rec, monkeypatch, proof):
    call = setup(context, rec, monkeypatch)
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: proof)
    monkeypatch.setattr(rec, '_spawn_and_deliver', lambda *a, **k: pytest.fail('no queue effect'))
    assert call(request())['code'] == 'unavailable'
    assert rec.WORK_QUEUE.admission_for_event(rec.DATA, 'd'*64) is None
    assert rows(rec) == []


@pytest.mark.parametrize('proof', [None, route_proof(owner_id='other-owner'),
    route_proof(route_digest='c'*64),
    route_proof(owner_route={'platform': 'imessage', 'chat_id': 'other-chat'})])
def test_terminal_notice_waits_for_original_verified_route(context, rec, monkeypatch, proof):
    _, _, _, job_id = failed_request(context, rec, monkeypatch)
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: proof)
    rec._requested_brief_feedback()
    assert rows(rec) == []
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'] is not None
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', route_proof)
    # Revoked private-source consent must still permit the constant status notice.
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: pytest.fail('no private source read'))
    rec._requested_brief_feedback()
    notice = rows(rec)[0]
    assert notice['payload']['feedback_chat_id'] == 'iMessage;-;owner'
    assert notice['payload']['feedback_route_digest'] == 'b'*64
    assert 'PRIVATE' not in json.dumps(notice)
    assert rec._delivery_valid(notice['payload'])
    rec._requested_brief_feedback()
    assert len(rows(rec)) == 1


@pytest.mark.parametrize('proof', [None, route_proof(owner_id='other-owner'),
    route_proof(route_digest='c'*64),
    route_proof(owner_route={'platform': 'imessage', 'chat_id': 'other-chat'})])
def test_enqueued_notice_rechecks_route_without_provider_effect(context, rec, monkeypatch, proof):
    failed_request(context, rec, monkeypatch)
    rec._requested_brief_feedback()
    notice = rows(rec)[0]
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: proof)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *a: pytest.fail('no provider contact'))
    assert not rec.OUTBOX._attempt(notice['id'])
    assert rows(rec)[0]['status'] == 'superseded'
    assert rows(rec)[0]['acceptance'] == 'not_attempted'


def test_verified_notice_sends_once_after_reopen_and_original_replay(context, rec, monkeypatch):
    call, body, receipt, _ = failed_request(context, rec, monkeypatch)
    rec._requested_brief_feedback()
    notice_id = rows(rec)[0]['id']
    rec = restart(rec, monkeypatch)
    sent = []
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *a: sent.append(a) or
                        (True, '', {'acceptance': 'accepted', 'message_id': 'notice-ack'}))
    assert rec.OUTBOX._attempt(notice_id)
    assert len(sent) == 1
    assert call(body) == receipt  # original event cannot admit replacement work/feedback
    assert call(dict(body, operation='inspect')) == receipt
    rec = restart(rec, monkeypatch)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *a: pytest.fail('accepted notice never replays'))
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: None)
    assert not rec.OUTBOX._attempt(notice_id)
    rec._requested_brief_feedback()
    assert len(rows(rec)) == 1
    assert rows(rec)[0]['status'] == 'delivered'
    assert rows(rec)[0]['acceptance'] == 'accepted'


def test_unknown_notice_is_held_after_restart_route_change_and_late_receipt(context, rec, monkeypatch):
    failed_request(context, rec, monkeypatch)
    rec._requested_brief_feedback()
    notice_id = rows(rec)[0]['id']
    contacted = []
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *a: contacted.append(a) or
                        (False, 'lost receipt', {'acceptance': 'unknown'}))
    assert not rec.OUTBOX._attempt(notice_id)
    assert len(contacted) == 1 and rows(rec)[0]['acceptance'] == 'unknown'
    rec = restart(rec, monkeypatch)
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: None)
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *a: pytest.fail('never resend unknown'))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'gallery_receipt', lambda _: {'acceptance': 'unknown'})
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={'rows': []}) as doc:
        doc['rows'][0]['next_at'] = 0
    assert not rec.OUTBOX._attempt(notice_id)
    assert rows(rec)[0]['status'] == 'pending' and rows(rec)[0]['acceptance'] == 'unknown'
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'gallery_receipt', lambda _: {
        'acceptance': 'accepted', 'message_id': 'late-notice-ack'})
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={'rows': []}) as doc:
        doc['rows'][0]['next_at'] = 0  # advance to the next durable receipt poll
    assert rec.OUTBOX._attempt(notice_id)
    assert rows(rec)[0]['status'] == 'delivered'
    assert rows(rec)[0]['acceptance'] == 'accepted'
    assert len(contacted) == 1


def test_legacy_skill_notice_alias_is_never_upgraded_to_current_route(context, rec, monkeypatch):
    _, _, _, job_id = failed_request(context, rec, monkeypatch)
    meta = json.loads(rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'])
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        meta.pop('route_digest')
        meta['chat_id'] = IDENTITY['owner_id']
        db.execute('UPDATE jobs SET feedback=? WHERE id=?', (json.dumps(meta), job_id))
    rec._requested_brief_feedback()
    assert rows(rec) == []
    # A pre-upgrade already-enqueued notice no longer has work feedback, but the durable
    # original admission identifies it as a skill notice, so alias validation cannot bypass proof.
    job = rec.WORK_QUEUE.feedback_jobs(rec.DATA)[0]
    assert rec.OUTBOX.enqueue_work_feedback(job)
    rec.WORK_QUEUE.clear_feedback(rec.DATA, job_id)
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={'rows': []}) as doc:
        doc['rows'][0]['payload'].pop('feedback_route_digest')
    notice = rows(rec)[0]
    assert not rec._delivery_valid(notice['payload'])
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *a: pytest.fail('legacy alias never sends'))
    assert not rec.OUTBOX._attempt(notice['id'])
    assert rows(rec)[0]['acceptance'] == 'not_attempted'


def test_legacy_terminal_notice_records_one_content_free_withheld_note_across_restart(context, rec, monkeypatch):
    _, _, _, job_id = failed_request(context, rec, monkeypatch)
    meta = json.loads(rec.WORK_QUEUE.get(rec.DATA, job_id)['feedback'])
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        meta.pop('route_digest')
        meta['chat_id'] = IDENTITY['owner_id']
        db.execute('UPDATE jobs SET feedback=? WHERE id=?', (json.dumps(meta), job_id))
    before = rec.WORK_QUEUE.get(rec.DATA, job_id)
    inspect_only(rec, monkeypatch, 'legacy route never enqueues')
    monkeypatch.setattr(rec, '_runtime_source_authority', lambda: pytest.fail('no source read for fixed note'))
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', lambda: pytest.fail('legacy needs no route proof'))
    rec._requested_brief_feedback()
    rec._requested_brief_feedback()
    reopened = restart(rec, monkeypatch)
    inspect_only(reopened, monkeypatch, 'restart never enqueues')
    reopened._requested_brief_feedback()
    path = Path(rec.DATA) / 'events' / 'delivery.jsonl'
    notes = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(notes) == 1
    assert notes[0]['label'] == meta['label'] and notes[0]['status'] == 'skipped'
    assert notes[0]['run_id'] == job_id
    assert notes[0]['detail'] == (
        'Terminal skill notice withheld: original verified owner route proof is unavailable.')
    assert not any(value in path.read_text() for value in ('PRIVATE', IDENTITY['owner_id'], 'chat_id', 'route_digest'))
    assert reopened.WORK_QUEUE.get(rec.DATA, job_id) == before
    assert rows(reopened) == []


def test_notice_route_uses_real_current_activation_proof(rec, tmp_path, monkeypatch):
    import test_runtime_channel_proof as canonical
    _, path, activation_receipt, data, chat, _ = canonical.setup(tmp_path)
    proof = canonical.check(path, data, chat)
    identity = {'channel': 'photon', **{key: proof[key] for key in
                ('tenant_id', 'project_fingerprint', 'owner_id', 'account_id', 'line_key')}}
    monkeypatch.setenv('PHOTON_HOME_CHANNEL', canonical.OWNER)
    def current():
        try:
            return canonical.check(path, data, chat)
        except Exception:
            return None
    monkeypatch.setattr(rec, '_candidate_retained_route_proof', current)
    meta = rec._skill_notice_binding(identity, proof['provider_thread_id'], proof)
    assert meta['chat_id'] == canonical.GUID
    assert rec._skill_notice_current(meta)
    activation_receipt.unlink()
    assert not rec._skill_notice_current(meta)
    assert rec._skill_notice_binding(identity, proof['provider_thread_id'], proof) is None


def test_legacy_job_whose_original_was_delivered_clears_without_a_withheld_note(context, rec, monkeypatch):
    _, _, _, job_id = failed_request(context, rec, monkeypatch)
    legacy(rec, job_id)
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={'rows': []}) as doc:
        doc.setdefault('rows', []).append({'id': job_id, 'status': rec.OUTBOX.STATUS_DELIVERED,
                                           'acceptance': 'accepted', 'payload': {}})
    inspect_only(rec, monkeypatch, 'nothing is owed')
    rec._requested_brief_feedback()
    assert delivery_notes(rec) == []
    assert rec.WORK_QUEUE.feedback_jobs(rec.DATA) == []


def test_legacy_withheld_note_is_written_once_per_process_and_cleared_after_the_window(context, rec, monkeypatch):
    _, _, _, job_id = failed_request(context, rec, monkeypatch)
    legacy(rec, job_id)
    scans = []
    real = rec._record_delivery
    monkeypatch.setattr(rec, '_record_delivery', lambda *a, **k: scans.append(a) or real(*a, **k))
    for _ in range(3):
        rec._requested_brief_feedback()
    assert len(scans) == 1 and len(delivery_notes(rec)) == 1
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute('UPDATE jobs SET finished=? WHERE id=?', (time.time() - 5 * 3600, job_id))
    rec._requested_brief_feedback()
    assert rec.WORK_QUEUE.feedback_jobs(rec.DATA) == [], 'the notice window bounds a held legacy job'
    assert rows(rec) == [] and len(delivery_notes(rec)) == 1


def test_bound_jobs_share_one_route_proof_read_and_clear_after_a_changed_route_window(context, rec, monkeypatch):
    _, _, _, job_id = failed_request(context, rec, monkeypatch)
    reads = []
    monkeypatch.setattr(rec, '_candidate_retained_route_proof',
                        lambda: reads.append(1) or route_proof(owner_id='other-owner'))
    rec._requested_brief_feedback()
    assert reads == [1] and rows(rec) == [] and delivery_notes(rec) == []
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute('UPDATE jobs SET finished=? WHERE id=?', (time.time() - 5 * 3600, job_id))
    rec._requested_brief_feedback()
    assert rec.WORK_QUEUE.feedback_jobs(rec.DATA) == [] and rows(rec) == []
