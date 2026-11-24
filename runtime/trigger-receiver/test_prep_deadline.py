"""Requested prep shares one usefulness deadline through queue recovery and delivery."""
import time
from datetime import timedelta

import pytest

from test_runtime_owner_meeting_prep_request import WHEN, call, context, rec, request, setup
from test_runtime_owner_skill_request import request as direct_request, setup as direct_setup
from test_shared_meeting_prep_pipeline import pipeline

__all__ = ['context', 'rec', 'pipeline']


@pytest.mark.parametrize('case', [
    ('offered', 4, 180), ('direct', 4, 180), ('direct', 180, 600), ('sweep', None, 600),
])
def test_accepted_prep_deadline(context, rec, monkeypatch, case):
    path, minutes, expected_seconds = case
    now = WHEN.timestamp()
    monkeypatch.setattr(time, 'time', lambda: now)
    meeting = {'event_id': 'calendar-event', 'start': (WHEN + timedelta(minutes=minutes or 60)).isoformat()}
    if path == 'offered':
        import pending_offer
        setup(context, rec, monkeypatch)
        offer = pending_offer.set_offer('meeting_prep', 'Full prep for Dana?', person='Dana', meeting=meeting, prepare_only=True)
        pending_offer.activate_offer(offer, {'message_id': 'near-meeting', 'target': 'photon',
            'chat_id': 'iMessage;-;owner', 'accepted_at': now})
        receipt = call(request(anchor='near-meeting'), context, rec)
    else:
        admit = direct_setup(context, rec, monkeypatch)
        receipt = admit(direct_request({'mode': 'focus', 'focus': 'Dana', 'meeting': meeting}
                                       if minutes else {'mode': 'sweep'}))
    assert receipt['status'] == 'committed'
    job = rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])
    assert job['valid_until'] == now + expected_seconds


def test_legacy_prep_with_four_hour_deadline_expires_before_restarting_worker(context, rec, monkeypatch):
    clock = [WHEN.timestamp()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    admit = direct_setup(context, rec, monkeypatch)
    receipt = admit(direct_request())
    key = receipt['outcome']['request_id']
    with rec.WORK_QUEUE._db(rec.DATA) as db:
        db.execute('UPDATE jobs SET valid_until=? WHERE id=?', (clock[0] + 14400, key))
    clock[0] += 601
    job = rec.WORK_QUEUE.claim(rec.DATA, 'recovered')
    monkeypatch.setattr(rec, '_execute_work', lambda *args: pytest.fail('expired prep ran'))
    rec._work_one(job)
    assert rec.WORK_QUEUE.get(rec.DATA, key)['status'] == 'expired'
    assert admit(direct_request(operation='inspect')) == receipt
    rec._requested_brief_feedback()
    with rec.OUTBOX.HOOKS['json_transaction'](rec.OUTBOX.path(), default={'rows': []}) as doc:
        assert [r['payload'].get('feedback_for') for r in doc['rows']] == [key]


def test_focus_resolves_nearby_occurrence_while_worker_runs(context, rec, monkeypatch):
    clock = [WHEN.timestamp()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    admit = direct_setup(context, rec, monkeypatch)
    receipt = admit(direct_request({'mode':'focus', 'focus':'Dana'}))
    job = rec.WORK_QUEUE.claim(rec.DATA, 'first-worker')

    def compose(_):
        clock[0] += 90
        return {'text': 'Fictional prep.', 'label': job['payload']['label'],
                'effects': [{'kind': 'meeting_prep_delivered', 'mode': 'full',
                             'calendar_event_id': 'fixture', 'calendar_start': (WHEN + timedelta(minutes=2)).isoformat()}]}

    monkeypatch.setattr(rec, '_execute_work', compose)
    monkeypatch.setattr(rec, '_deliver_text', lambda *a, **kw: pytest.fail('late prep sent'))
    rec._work_one(job)
    assert rec.WORK_QUEUE.get(rec.DATA, receipt['outcome']['request_id'])['status'] == 'expired'


def test_active_prep_child_is_stopped_at_the_deadline(context, rec, monkeypatch):
    """A real child process owns no sources or transport; its whole outcome is the bound."""
    import signal
    import sys
    clock = [WHEN.timestamp()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    receipt = direct_setup(context, rec, monkeypatch)(direct_request())
    key = receipt['outcome']['request_id']
    job = rec.WORK_QUEUE.claim(rec.DATA, 'running')
    original_popen = rec.subprocess.Popen
    children = []

    def start(*_, **kwargs):
        process = original_popen([sys.executable, '-c', 'import time;time.sleep(15)'], **kwargs)
        children.append(process)
        clock[0] = job['valid_until']
        return process

    monkeypatch.setattr(rec.subprocess, 'Popen', start)
    monkeypatch.setattr(rec, '_deliver_text', lambda *a, **kw: pytest.fail('expired prep sent'))
    rec._work_one(job)
    assert len(children) == 1 and children[0].poll() == -signal.SIGKILL
    assert rec.WORK_QUEUE.get(rec.DATA, key)['status'] == 'expired'


def test_focused_all_day_prep_keeps_interactive_deadline(pipeline):
    import json
    from datetime import datetime, timezone
    from runtime_owner_meeting_prep_request import prep_window
    from test_shared_meeting_prep_pipeline import PACK
    module, command, events, _, _, _, _, data = pipeline
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    events[0]['start'] = tomorrow
    selection = {'mode': 'focus', 'focus': 'Dana'}
    result = module.run(PACK, selection, command)
    assert result['text'] == 'Actual shared prep'
    effects = json.loads((data/'events'/('delivery-effects-' + 'a'*32 + '.json')).read_text())['effects']
    assert any(row.get('kind') == 'meeting_prep_delivered' and row['calendar_start'] == tomorrow for row in effects)
    accepted = time.time()
    assert prep_window(json.dumps({'selection': selection}), accepted, effects) == (accepted + 600, None)


@pytest.mark.parametrize('path', ['direct', 'offered'])
@pytest.mark.parametrize('case', [(30, False), (60, False), (61, True)])
def test_prep_admission_requires_a_live_delivery_window(context, rec, monkeypatch, path, case):
    import pending_offer
    from test_request_feedback import _rows
    seconds_to_start, expect_queued = case
    clock = [WHEN.timestamp()]
    meeting = {'event_id': 'fictional', 'start': (WHEN + timedelta(seconds=seconds_to_start)).isoformat()}
    if path == 'direct':
        admit = direct_setup(context, rec, monkeypatch)
        body = direct_request({'mode': 'focus', 'focus': 'Dana', 'meeting': meeting})
    else:
        setup(context, rec, monkeypatch)
        offer = pending_offer.set_offer('meeting_prep', 'Full prep for Dana?', person='Dana', meeting=meeting,
                                        prepare_only=True)
        pending_offer.activate_offer(offer, {'message_id': 'near', 'target': 'photon',
                                            'chat_id': 'iMessage;-;owner', 'accepted_at': clock[0]})
        body = request(anchor='near')

        def admit(message):
            return call(message, context, rec)
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    receipt = admit(body)
    if not expect_queued:
        assert receipt == {'version': 1, 'status': 'rejected', 'code': 'prep_window_closed'}
        assert rec.WORK_QUEUE.admission_for_event(rec.DATA, body['event_id']) is None
        with rec.WORK_QUEUE._db(rec.DATA) as db:
            assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0
        rec._requested_brief_feedback()
        assert not _rows(rec)
    else:
        assert receipt['status'] == 'committed'
        clock[0] += 2
        assert rec.WORK_QUEUE.claim(rec.DATA, 'worker') is None
        assert admit(body) == admit(dict(body, operation='inspect')) == receipt
