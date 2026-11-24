"""Exercise admission, work, durable recovery and owner delivery together with fictional prep."""
import importlib.util
import json
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
import test_runtime_owner_skill_request as direct
from test_request_feedback import HERE, _rows
from test_runtime_owner_meeting_prep_request import WHEN, call, context, rec, request, route_proof, setup

__all__ = ['context', 'rec']

OLD, CURRENT = 'Private old context.', 'Public agenda only.'
TEN_MINUTES = 10 * 60


@pytest.fixture
def journey(context, rec, monkeypatch):
    clock = [WHEN.timestamp()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    setup(context, rec, monkeypatch)
    revision = {'source_revision': 'a' * 64, 'route_digest': 'b' * 64}
    composed, sent = [], []

    def child(argv, *, env, **kwargs):
        proof = json.loads(env['SOTTO_SKILL_COMPOSITION_PROOF'])
        composed.append(proof)
        text = OLD if proof['source_revision'] == 'a' * 64 else CURRENT
        return SimpleNamespace(pid=999999, returncode=0, poll=lambda: 0,
                               communicate=lambda **kwargs: (text, ''))

    def send(body, target, key, attempts, **kwargs):
        sent.append((body, target, key))
        return True, '', {'message_id': 'fixture-ack'}

    def wire(receiver):
        monkeypatch.setattr(receiver, '_composition_evidence', lambda _: dict(revision))
        monkeypatch.setattr(receiver, '_candidate_retained_route_proof',
                            lambda: route_proof(route_digest=revision['route_digest']))
        monkeypatch.setattr(receiver, '_retained_proof_current', lambda proof: proof == revision)
        monkeypatch.setattr(receiver.subprocess, 'Popen', child)
        monkeypatch.setitem(receiver.OUTBOX.HOOKS, 'send', send)

    def admit(kind, meeting=None):
        if kind == 'offered':
            anchor = 'offered-message'
            if meeting:
                import pending_offer
                offer = pending_offer.set_offer('meeting_prep', 'Full prep for Dana?', person='Dana',
                                                meeting=meeting, prepare_only=True)
                anchor = 'near-meeting'
                pending_offer.activate_offer(offer, {'message_id': anchor, 'target': 'photon',
                    'chat_id': 'iMessage;-;owner', 'accepted_at': clock[0]})
            receipt = call(request(anchor=anchor), context, rec)
        else:
            invoke = direct.setup(context, rec, monkeypatch)
            selection = {'mode': 'focus', 'focus': 'Dana'}
            if meeting:
                selection['meeting'] = meeting
            receipt = invoke(direct.request(selection))
        wire(rec)
        return receipt['outcome']['request_id']

    def restart():
        spec = importlib.util.spec_from_file_location('restarted_prep_receiver', HERE / 'receiver.py')
        receiver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(receiver)
        monkeypatch.setattr(receiver, 'DATA', rec.DATA)
        wire(receiver)
        return receiver

    def advance(receiver, job_id):
        row = receiver.WORK_QUEUE.get(receiver.DATA, job_id)
        if row['status'] in ('pending', 'ready'):
            clock[0] = max(clock[0], row['due'])
            job = receiver.WORK_QUEUE.claim(receiver.DATA, 'fixture-worker')
            assert job and job['id'] == job_id
            receiver._work_one(job)

    return SimpleNamespace(admit=admit, restart=restart, advance=advance, revision=revision,
                           composed=composed, sent=sent, clock=clock)


@pytest.mark.parametrize('kind', ['offered', 'direct'])
@pytest.mark.parametrize('change_again', [False, True], ids=['stable-refresh', 'changed-again'])
def test_changed_source_at_handoff_recomposes_once_for_original_owner(journey, rec, monkeypatch, kind, change_again):
    job_id = journey.admit(kind)

    def change_after_save(receiver, new_revision):
        original = receiver.WORK_QUEUE.save_result
        def save(*args):
            original(*args)
            journey.revision['source_revision'] = new_revision
        monkeypatch.setattr(receiver.WORK_QUEUE, 'save_result', save)

    # The real child stages effects under the stable job id; a fresh answer must not inherit them.
    stale_effect = {'kind': 'source_permissions', 'sources': ['whatsapp']}
    assert rec._shared_effects().stage([stale_effect], run_id=job_id)
    change_after_save(rec, 'c' * 64)
    journey.advance(rec, job_id)
    assert not journey.sent
    receiver = journey.restart()
    acknowledged_effects = []
    on_delivered = receiver.OUTBOX.HOOKS['on_delivered']

    def record_effects(payload):
        acknowledged_effects.extend(payload.get('effects') or [])
        return on_delivered(payload)

    monkeypatch.setitem(receiver.OUTBOX.HOOKS, 'on_delivered', record_effects)
    if change_again:
        change_after_save(receiver, 'd' * 64)
    journey.advance(receiver, job_id)
    journey.advance(receiver, job_id)
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    assert [p['source_revision'] for p in journey.composed] == ['a' * 64, 'c' * 64]
    assert len(journey.sent) == 1
    body, target, key = journey.sent[0]
    assert target == 'photon'
    if change_again:
        assert OLD not in body and CURRENT not in body
        assert 'prep' in body and key != job_id
        assert journey.clock[0] - WHEN.timestamp() < TEN_MINUTES
    else:
        assert (body, key) == (CURRENT, job_id)
    assert stale_effect not in acknowledged_effects
    assert _rows(receiver)[0]['status'] == 'delivered'
    assert not receiver.WORK_QUEUE.claim(receiver.DATA, 'another-worker')


def test_access_changes_while_the_prep_child_is_writing(journey, rec, monkeypatch):
    job_id = journey.admit('direct')
    start_child = rec.subprocess.Popen

    def change_during_first_child(*args, **kwargs):
        process = start_child(*args, **kwargs)
        collect_output = process.communicate

        def finish(**opts):
            if len(journey.composed) == 1:
                journey.revision['source_revision'] = 'c' * 64
            return collect_output(**opts)

        process.communicate = finish
        return process

    monkeypatch.setattr(rec.subprocess, 'Popen', change_during_first_child)
    journey.advance(rec, job_id)
    assert not journey.sent
    receiver = journey.restart()
    for _ in range(2):
        journey.advance(receiver, job_id)
    receiver.OUTBOX.drain()
    assert [proof['source_revision'] for proof in journey.composed] == ['a' * 64, 'c' * 64]
    assert journey.sent == [(CURRENT, 'photon', job_id)]


@pytest.mark.parametrize('accepted', [True, False], ids=['accepted', 'unknown'])
def test_outbox_contact_survives_restart_and_source_change_without_replacement(journey, rec, monkeypatch, accepted):
    job_id = journey.admit('offered')

    def first_contact(body, target, key, attempts, **kwargs):
        journey.sent.append((body, target, key))
        if accepted:
            return True, '', {'message_id': 'fixture-ack'}
        return False, 'fixture response lost', {'acceptance': 'unknown'}

    def stop_after_handoff(*args, **kwargs):
        raise rec._WorkInterruptedError('fixture restart')

    with monkeypatch.context() as first_worker:
        first_worker.setitem(rec.OUTBOX.HOOKS, 'send', first_contact)
        first_worker.setattr(rec.WORK_QUEUE, 'finish', stop_after_handoff)
        journey.advance(rec, job_id)
    assert journey.sent == [(OLD, 'photon', job_id)]
    original = _rows(rec)[0]
    assert original['acceptance'] == ('accepted' if accepted else 'unknown')
    journey.revision['source_revision'] = 'c' * 64
    receiver = journey.restart()
    journey.advance(receiver, job_id)
    assert receiver.WORK_QUEUE.get(receiver.DATA, job_id)['status'] == 'done'
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    assert len(journey.composed) == 1
    assert journey.sent == [(OLD, 'photon', job_id)]
    assert _rows(receiver)[0]['acceptance'] == original['acceptance']


def test_changed_owner_route_never_rebinds_previously_accepted_prep(journey, rec, monkeypatch):
    job_id = journey.admit('offered')
    original = rec.WORK_QUEUE.save_result

    def move_route(*args):
        original(*args)
        journey.revision['route_digest'] = 'f' * 64

    monkeypatch.setattr(rec.WORK_QUEUE, 'save_result', move_route)
    journey.advance(rec, job_id)
    receiver = journey.restart()
    journey.advance(receiver, job_id)
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    assert receiver.WORK_QUEUE.get(receiver.DATA, job_id)['status'] == 'failed'
    assert len(journey.composed) == 1
    assert not journey.sent


def test_unchanged_prep_arrives_without_recomposition(journey, rec):
    job_id = journey.admit('offered')
    journey.advance(rec, job_id)
    assert journey.sent == [(OLD, 'photon', job_id)]
    assert _rows(rec)[0]['status'] == 'delivered'
    assert len(journey.composed) == 1
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['status'] == 'done'


def test_crash_inside_refresh_consumes_only_rebuild_and_delivers_one_notice(journey, rec, monkeypatch):
    job_id = journey.admit('offered')
    save = rec.WORK_QUEUE.save_result

    def change_after_save(*args):
        save(*args)
        journey.revision['source_revision'] = 'c' * 64

    class FixtureCrash(BaseException):
        pass

    child = rec.subprocess.Popen

    def crash_in_refresh(*args, **kwargs):
        process = child(*args, **kwargs)
        if journey.composed[-1]['source_revision'] == 'c' * 64:
            def interrupted(**opts):
                raise FixtureCrash()
            process.communicate = interrupted
        return process

    monkeypatch.setattr(rec.WORK_QUEUE, 'save_result', change_after_save)
    monkeypatch.setattr(rec.subprocess, 'Popen', crash_in_refresh)
    journey.advance(rec, job_id)
    with pytest.raises(FixtureCrash):
        journey.advance(rec, job_id)
    assert not journey.sent
    leased = rec.WORK_QUEUE.get(rec.DATA, job_id)
    assert leased['status'] == 'leased'
    journey.clock[0] = leased['lease_until'] + 1
    receiver = journey.restart()
    recovered = receiver.WORK_QUEUE.claim(receiver.DATA, 'after-crash')
    assert recovered['id'] == job_id
    receiver._work_one(recovered)
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    assert [p['source_revision'] for p in journey.composed] == ['a' * 64, 'c' * 64]
    assert len(journey.sent) == 1
    body, target, key = journey.sent[0]
    assert 'prep' in body and OLD not in body and CURRENT not in body
    assert target == 'photon' and key != job_id
    assert receiver.WORK_QUEUE.claim(receiver.DATA, 'no-replay') is None


def test_shutdown_before_refresh_launch_keeps_the_single_rebuild(journey, rec, monkeypatch):
    job_id = journey.admit('offered')
    save = rec.WORK_QUEUE.save_result

    def change_after_save(*args):
        save(*args)
        journey.revision['source_revision'] = 'c' * 64

    monkeypatch.setattr(rec.WORK_QUEUE, 'save_result', change_after_save)
    journey.advance(rec, job_id)
    rec._WORK_STOP.set()
    try:
        journey.advance(rec, job_id)
    finally:
        rec._WORK_STOP.clear()
    assert len(journey.composed) == 1
    receiver = journey.restart()
    journey.advance(receiver, job_id)
    assert journey.sent == [(CURRENT, 'photon', job_id)]
    assert receiver.WORK_QUEUE.get(receiver.DATA, job_id)['status'] == 'done'

@pytest.mark.parametrize('kind', ['direct', 'offered'])
@pytest.mark.parametrize('receipt', ['unknown', 'before-drain', 'before-contact', 'offline-until-meeting', 'slow-route-check'])
def test_five_minute_prep_reconciles_before_meeting_without_resending(journey, rec, monkeypatch, kind, receipt):
    start = WHEN + timedelta(minutes=5)
    job_id = journey.admit(kind, {'event_id': 'near-meeting', 'start': start.isoformat()})

    def lost_response(body, target, key, attempts, **kwargs):
        journey.sent.append((body, target, key))
        return False, 'fixture response lost', {'acceptance': 'unknown'}

    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lost_response)
    journey.advance(rec, job_id)
    assert journey.sent == [(OLD, 'photon', job_id)]
    rec._requested_brief_feedback()
    assert len(_rows(rec)) == 1

    receiver = journey.restart()
    # A prep that starts in five minutes already has a four-minute content deadline.
    # Even a full outbox tick after the notice is queued must leave time before the meeting.
    journey.clock[0] = (start - timedelta(minutes=2, seconds=-5)).timestamp()
    receiver._requested_brief_feedback()
    original, notice = _rows(receiver)
    assert original['acceptance'] == 'unknown'
    assert notice['payload']['feedback_for'] == job_id
    if receipt == 'before-drain':
        monkeypatch.setitem(receiver.OUTBOX.HOOKS, 'gallery_receipt', lambda key: {
            'acceptance': 'accepted', 'message_id': 'fixture-late-prep'} if key == job_id else {})
    if receipt in ('before-contact', 'slow-route-check'):
        valid = receiver.OUTBOX.HOOKS['valid']

        def check_route(payload):
            if payload.get('feedback_for') == job_id:
                if receipt == 'before-contact':
                    # Receipt reconciliation wins after the drain claims the notice.
                    receiver.OUTBOX._settle(job_id, True, '', 1, {'message_id': 'fixture-late-prep'})
                else:
                    journey.clock[0] = start.timestamp()
            return valid(payload)

        monkeypatch.setitem(receiver.OUTBOX.HOOKS, 'valid', check_route)
    if receipt == 'offline-until-meeting':
        receiver = journey.restart()
        journey.clock[0] = start.timestamp()
    else:
        journey.clock[0] += receiver.OUTBOX.DRAIN_INTERVAL_SECS
        assert journey.clock[0] < start.timestamp()
    receiver.OUTBOX.drain()
    assert len(journey.sent) == (2 if receipt == 'unknown' else 1)
    if receipt == 'unknown':
        body, target, key = journey.sent[-1]
        assert "couldn't confirm whether your requested meeting prep arrived" in body
        assert target == 'photon' and key != job_id

    receiver = journey.restart()
    journey.clock[0] = start.timestamp() + TEN_MINUTES + 1
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    assert len(journey.sent) == (2 if receipt == 'unknown' else 1)
    assert [key for _, _, key in journey.sent].count(job_id) == 1


def test_unresolved_prep_keeps_normal_uncertainty_notice_window(journey, rec, monkeypatch):
    job_id = journey.admit('direct')
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'send', lambda *_args, **_kwargs:
                        (False, 'fixture response lost', {'acceptance': 'unknown'}))
    journey.advance(rec, job_id)
    journey.clock[0] += TEN_MINUTES - 1
    rec._requested_brief_feedback()
    assert len(_rows(rec)) == 1
    receiver = journey.restart()
    journey.clock[0] += 61
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    [notice] = journey.sent
    assert "couldn't confirm whether your requested meeting prep arrived" in notice[0]
    assert notice[2] != job_id


@pytest.mark.parametrize('kind', ['direct', 'offered'])
@pytest.mark.parametrize('worker_started', [False, True], ids=['expired-in-queue', 'failed-worker'])
def test_known_meeting_caps_failure_notice_after_restart(journey, rec, kind, worker_started):
    from test_request_feedback import _finish
    meeting = WHEN + timedelta(minutes=5)
    job_id = journey.admit(kind, {'event_id': 'fixture', 'start': meeting.isoformat()})
    if worker_started:
        _finish(rec, job_id)
        rec._requested_brief_feedback()
        [notice] = _rows(rec)
        assert notice['payload']['valid_until'] == meeting.timestamp()
    journey.clock[0] = meeting.timestamp()
    receiver = journey.restart()
    assert receiver.WORK_QUEUE.claim(receiver.DATA, 'recovery') is None
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    assert journey.sent == []
    assert receiver.WORK_QUEUE.get(receiver.DATA, job_id)['feedback'] is None


@pytest.mark.parametrize('kind', ['direct', 'offered'])
def test_original_prep_cannot_start_sending_after_its_deadline(journey, rec, monkeypatch, kind):
    meeting = WHEN + timedelta(minutes=5)
    job_id = journey.admit(kind, {'event_id': 'fixture', 'start': meeting.isoformat()})
    check = rec.OUTBOX.HOOKS['valid']

    def slow_check(payload):
        valid = check(payload)
        if payload.get('run_id') == job_id:
            journey.clock[0] = payload['valid_until']
        return valid

    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'valid', slow_check)
    journey.advance(rec, job_id)
    assert journey.sent == []
    receiver = journey.restart()
    receiver.OUTBOX.drain()
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    assert len(journey.sent) == 1
    assert "couldn't finish your requested meeting prep" in journey.sent[0][0]
    assert journey.sent[0][2] != job_id


def test_prep_held_when_final_source_check_loses_revision(journey, rec, monkeypatch):
    job_id = journey.admit('direct')
    check = rec.OUTBOX.HOOKS['valid']

    def revoke_after_validation(payload):
        valid = check(payload)
        if payload.get('run_id') == job_id:
            journey.revision['source_revision'] = 'c' * 64
        return valid

    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'valid', revoke_after_validation)
    journey.advance(rec, job_id)
    assert journey.sent == []
    receiver = journey.restart()
    journey.clock[0] += TEN_MINUTES
    receiver.OUTBOX.drain()
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    assert len(journey.sent) == 1
    assert "couldn't finish your requested meeting prep" in journey.sent[0][0]


def test_failure_notice_remembers_meeting_discovered_by_worker(journey, rec, monkeypatch):
    job_id = journey.admit('direct')
    meeting = WHEN + timedelta(minutes=3)
    def compose(job):
        journey.clock[0] = meeting.timestamp() - 60
        return {'text': OLD, 'label': job['payload']['label'], 'effects': [{
            'kind': 'meeting_prep_delivered', 'mode': 'full', 'calendar_start': meeting.isoformat()}]}
    monkeypatch.setattr(rec, '_execute_work', compose)
    journey.advance(rec, job_id)
    assert rec.WORK_QUEUE.get(rec.DATA, job_id)['status'] == 'expired'
    rec._requested_brief_feedback()
    [notice] = _rows(rec)
    assert notice['payload']['valid_until'] == meeting.timestamp()
    receiver = journey.restart()
    journey.clock[0] = meeting.timestamp()
    receiver.OUTBOX.drain()
    assert journey.sent == []


def test_pre_upgrade_feedback_uses_timed_outbox_row_after_expiry(journey, rec, monkeypatch):
    meeting = WHEN + timedelta(minutes=5)
    job_id = journey.admit('direct', {'event_id': 'fixture', 'start': meeting.isoformat()})
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET feedback=json_remove(feedback,'$.prep_meeting_start') WHERE id=?", (job_id,))
    monkeypatch.setitem(rec.OUTBOX.HOOKS, 'delivery_ready', lambda payload: False)
    journey.advance(rec, job_id)
    assert journey.sent == []
    receiver = journey.restart()
    journey.clock[0] = meeting.timestamp()
    receiver.OUTBOX.drain()
    receiver._requested_brief_feedback()
    [original] = _rows(receiver)
    assert original['status'] == 'expired'
    assert original['payload']['prep_meeting_start'] == meeting.timestamp()
    assert receiver.WORK_QUEUE.get(receiver.DATA, job_id)['feedback'] is None


@pytest.mark.parametrize('kind', ['direct', 'offered', 'focus-result', 'unknown'])
def test_expired_pre_upgrade_prep_keeps_only_its_known_occurrence(journey, rec, kind):
    meeting = WHEN + timedelta(minutes=5)
    occurrence = {'event_id': 'fixture', 'start': meeting.isoformat()}
    key = journey.admit('offered' if kind == 'offered' else 'direct',
                        occurrence if kind in ('direct', 'offered') else None)
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET feedback=json_remove(feedback,'$.prep_meeting_start') WHERE id=?", (key,))
        if kind == 'focus-result':
            result = {'text': 'private', 'effects': [{'kind': 'meeting_prep_delivered', 'mode': 'full',
                                                      'calendar_start': meeting.isoformat()}]}
            db.execute("UPDATE jobs SET result=?,status='ready' WHERE id=?", (json.dumps(result), key))
    journey.clock[0] = WHEN.timestamp() + TEN_MINUTES + 1
    receiver = journey.restart()
    receiver._drain_work()
    job = receiver.WORK_QUEUE.get(receiver.DATA, key)
    assert job['status'] == 'expired' and job['payload'] == {} and job['result'] is None
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    assert len(journey.sent) == (1 if kind == 'unknown' else 0)


def test_legacy_prep_claimed_before_cutoff_retains_occurrence_on_worker_deferral(journey, rec, monkeypatch):
    meeting = WHEN + timedelta(minutes=5)
    key = journey.admit('direct', {'event_id': 'fixture', 'start': meeting.isoformat()})
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute("UPDATE jobs SET feedback=json_remove(feedback,'$.prep_meeting_start') WHERE id=?", (key,))
    receiver = journey.restart()
    journey.clock[0] = meeting.timestamp() - 90
    finished = receiver.threading.Event()
    run = receiver._work_one

    def worker(job):
        run(job)
        finished.set()

    def busy(job):
        raise receiver._WorkAdmissionBusyError()

    monkeypatch.setattr(receiver, '_work_one', worker)
    monkeypatch.setattr(receiver, '_execute_work', busy)
    receiver._drain_work()
    assert finished.wait(5)
    job = receiver.WORK_QUEUE.get(receiver.DATA, key)
    assert job['status'] == 'expired' and job['payload'] == {}
    assert json.loads(job['feedback'])['prep_meeting_start'] == meeting.timestamp()
    journey.clock[0] = meeting.timestamp()
    receiver._requested_brief_feedback()
    receiver.OUTBOX.drain()
    assert not journey.sent


@pytest.mark.parametrize('feedback', ['bad-json', '[]', '{"label":42}'])
def test_unrelated_old_feedback_cannot_block_queue_expiry(journey, rec, feedback):
    key = rec.WORK_QUEUE.enqueue(rec.DATA, 'run', {'label': 'routine:test'}, key='legacy-routine',
                                 valid_until=WHEN.timestamp() + 1)
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute('UPDATE jobs SET feedback=? WHERE id=?', (feedback, key))
    journey.clock[0] += TEN_MINUTES
    journey.restart()._drain_work()
    assert rec.WORK_QUEUE.get(rec.DATA, key)['status'] == 'expired'
