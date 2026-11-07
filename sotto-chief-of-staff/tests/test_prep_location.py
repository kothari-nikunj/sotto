"""Accepted prep location changes use exact occurrence evidence through delivery."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pytest

PACK = Path(__file__).resolve().parents[1]
for relative in ('_shared/lib', '_shared/scripts', 'proactive/scripts'):
    sys.path.insert(0, str(PACK / relative))
import delivery_effects as effects
import proactive_scan as scan
import compose_notification as notification

NOW = datetime(2026, 11, 4, 18, tzinfo=timezone.utc)
START = (NOW + timedelta(minutes=20)).isoformat()
ROUTE = 'b' * 64


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    monkeypatch.setenv('SOTTO_DELIVERY_RUN_ID', 'a' * 32)
    monkeypatch.setattr(notification, 'enrich', lambda items, now: items)


def full(mode='full', location='Room A'):
    return {'kind': 'meeting_prep_delivered', 'mode': mode, 'calendar_event_id': 'cal:event',
            'calendar_start': START, 'prep_location': location, 'prep_route_digest': ROUTE}


def accept(effect, at=NOW.timestamp() - 60, route=ROUTE):
    return effects.finalize([effect], {'accepted_at': at, 'route_digest': route, 'route_proof_version': 1})


def event(location='Room B', **extra):
    return {'id': 'cal:event', 'start': START, 'summary': 'Design review', 'location': location,
            'attendees': [{'email': 'guest@example.com', 'displayName': 'Guest'}], **extra}


def candidates(ev=None):
    ev = ev or event()
    baseline = effects.prep_location_baseline(ev.get('id'), ev.get('start'))
    location = effects.prep_location(ev.get('location'))
    update = ({'kind': 'prep_location_update', 'calendar_event_id': ev['id'], 'calendar_start': ev['start'],
               'location': location, 'baseline': baseline, 'calendar_observed_at': NOW.isoformat()}
              if baseline and location and location != baseline['location'] else None)
    return scan.scan([ev], [], {}, 'owner@inside.test', NOW,
                     prep_location_updates={effects.meeting_occurrence(ev['id'], ev['start']): update} if update else {},
                     prepped_occurrences={effects.meeting_occurrence('cal:event', START)})['nudges']


def test_only_accepted_full_prep_with_matching_proven_route_creates_location_baseline():
    assert not effects.finalize([full()], {})
    assert not candidates()
    accept(full('offer'))
    assert not candidates()
    accept(full(), at=NOW.timestamp() - 90, route='c' * 64)
    assert not candidates()
    accept(full())
    assert len(candidates()) == 1
    assert not candidates(event(' Room   A '))
    assert not candidates(event(''))
    assert not any(row.get('location_update') for row in candidates(event(id='sibling')))


def test_location_update_is_factual_no_model_or_offer_and_requires_current_cache(tmp_path, monkeypatch):
    accept(full())
    item = candidates()[0]
    text = notification.compose('proactive', [item], now=NOW,
                                llm=lambda *_: pytest.fail('location update cannot call model'))
    assert text == 'Design review location changed to Room B.'
    staged = json.loads((tmp_path / 'events' / ('delivery-effects-' + 'a' * 32 + '.json')).read_text())['effects']
    assert not any(row['kind'] in ('pending_offer', 'meeting_prep_delivered') for row in staged)
    update = next(row for row in staged if row['kind'] == 'prep_location_update')
    assert not effects.valid(staged, NOW.timestamp())
    cache = {'status': 'ok', 'complete': True, 'projection': 'day', 'date': '2026-11-04',
             'generated_at': NOW.isoformat(), 'refresh_secs': 120,
             'events': [{'id': 'cal:event', 'start': START, 'prep_location': 'Room B'}]}
    path = tmp_path / 'cache/calendar_today.json'
    path.parent.mkdir(); path.write_text(json.dumps(cache))
    monkeypatch.setattr(__import__('source_context'), 'allowed', lambda _: True)
    assert effects.valid(staged, NOW.timestamp())
    for changed in ({'complete': False}, {'generated_at': (NOW - timedelta(hours=2)).isoformat()},
                    {'date': '2026-11-03'}, {'events': []},
                    {'events': [{'id': 'cal:event', 'start': START, 'prep_location': 'Room C'}]}):
        path.write_text(json.dumps({**cache, **changed}))
        assert not effects.valid(staged, NOW.timestamp())
    path.write_text(json.dumps(cache))
    monkeypatch.setattr(__import__('source_context'), 'allowed', lambda _: False)
    assert not effects.valid(staged, NOW.timestamp())
    assert not effects.finalize([update], {})
    accept(update, NOW.timestamp())
    assert not candidates()
    accept(update, NOW.timestamp())
    assert not candidates()
    accept(full(), at=NOW.timestamp() - 30)  # delayed older full-prep finalization
    assert not candidates()
    newer = full(location='Room C'); accept(newer, NOW.timestamp() + 1)
    accept(update, NOW.timestamp() + 2)
    assert not candidates(event('Room C'))
    assert candidates(event('Room B'))


def test_calendar_cache_carries_same_normalized_location():
    spec = importlib.util.spec_from_file_location('prep_location_calcache', PACK.parents[0] / 'runtime/trigger-receiver/calcache.py')
    cache = importlib.util.module_from_spec(spec); spec.loader.exec_module(cache)
    rows = cache.today_rows([cache._norm_cal_event(event(' Room  B '))], '2026-11-04')
    assert rows[0]['prep_location'] == 'Room B'


def test_latest_blank_full_prep_invalidates_old_baseline_and_old_effect(tmp_path, monkeypatch):
    accept(full())
    old = candidates()[0]['location_update']
    accept(full(location=''), at=NOW.timestamp())
    assert not candidates()
    assert not effects.valid([old], NOW.timestamp())
    accept(full(), at=NOW.timestamp() - 60)
    assert not candidates()


def test_location_receipt_uses_occurrence_local_day_across_midnight():
    start = '2026-11-05T08:20:00Z'  # November 5, 00:20 Pacific.
    effect = {**full(), 'calendar_start': start}
    accept(effect)
    with effects.proactive_state('2026-11-05') as state:
        assert effects.meeting_occurrence('cal:event', start) in state['prep_locations']
    assert effects.prep_location_baseline('cal:event', start)['location'] == 'Room A'


def test_declined_cancelled_and_unbounded_location_stay_unavailable():
    spec = importlib.util.spec_from_file_location('location_cancel_cache', PACK.parents[0] / 'runtime/trigger-receiver/calcache.py')
    cache = importlib.util.module_from_spec(spec); spec.loader.exec_module(cache)
    for extra in ({'status': 'cancelled'}, {'my_response': 'declined'}):
        normalized = cache._norm_cal_event(event(**extra))
        rows = cache.today_rows([normalized], '2026-11-04')
        assert not rows or not rows[0]['prep_location']
    assert effects.prep_location('A' * (effects.PREP_LOCATION_MAX_CHARS + 1)) == ''


def test_outbox_acceptance_advances_location_once_and_refuses_changed_again(tmp_path, monkeypatch):
    import jsonstore
    accept(full())
    item = candidates()[0]
    body = notification.compose('proactive', [item], now=NOW,
                                llm=lambda *_: pytest.fail('unexpected model'))
    staged = json.loads((tmp_path / 'events' / ('delivery-effects-' + 'a' * 32 + '.json')).read_text())['effects']
    cache = {'status': 'ok', 'complete': True, 'projection': 'day', 'date': '2026-11-04',
             'generated_at': NOW.isoformat(), 'refresh_secs': 120,
             'events': [{'id': 'cal:event', 'start': START, 'prep_location': 'Room B'}]}
    cache_path = tmp_path / 'cache/calendar_today.json'; cache_path.parent.mkdir()
    cache_path.write_text(json.dumps(cache))
    spec = importlib.util.spec_from_file_location('location_outbox', PACK.parents[0] / 'runtime/trigger-receiver/outbox.py')
    box = importlib.util.module_from_spec(spec); spec.loader.exec_module(box)
    sent = []
    monkeypatch.setattr(box.time, 'time', lambda: NOW.timestamp())
    box.HOOKS.update(data_root=lambda: str(tmp_path), json_transaction=jsonstore.transaction,
                     brief_gate=lambda *_: box.GATE_SEND,
                     local_today=lambda: '2026-11-04', valid=lambda payload: effects.valid(payload['effects'], NOW.timestamp()),
                     send=lambda *args: (sent.append(args) or (True, '', {'acceptance': 'accepted',
                         'message_id': 'location-message', 'route_digest': ROUTE, 'route_proof_version': 1})),
                     record=lambda *args, **kwargs: None,
                     on_delivered=lambda payload: effects.finalize(payload['effects'], payload['receipt']))
    monkeypatch.setattr(__import__('source_context'), 'allowed', lambda _: True)
    box._enqueue('location-1', box.KIND_NUDGE, {'label': 'event', 'body': body, 'target': 'owner', 'effects': staged},
                 NOW.timestamp(), '2026-11-04')
    assert box._attempt('location-1') is True
    assert len(sent) == 1 and not candidates()
    box._attempt('location-1')
    assert len(sent) == 1
    next_item = candidates(event('Room C'))[0]
    box._enqueue('location-2', box.KIND_NUDGE,
                 {'label': 'event', 'body': 'update', 'target': 'owner', 'effects': [next_item['location_update']]},
                 NOW.timestamp(), '2026-11-04')
    assert box._attempt('location-2') is False  # cache still says B, candidate says C.
    assert len(sent) == 1


def test_ordinary_scan_uses_actual_cache_observation_not_later_scan_clock(tmp_path):
    accept(full())
    observed = (NOW - timedelta(seconds=45)).isoformat()
    path = tmp_path / 'cache/calendar_today.json'; path.parent.mkdir()
    cache = {'status': 'ok', 'complete': True, 'projection': 'day', 'date': '2026-11-04',
             'generated_at': observed, 'refresh_secs': 120,
             'events': [{'id': 'cal:event', 'start': START, 'prep_location': 'Room B'}]}
    path.write_text(json.dumps(cache))
    update = effects.prep_location_candidate(event(), NOW.timestamp())
    assert update['calendar_observed_at'] == observed
    nudges = scan.scan([event()], [], {}, 'owner@inside.test', NOW,
                       prepped_occurrences={effects.meeting_occurrence('cal:event', START)},
                       prep_location_updates={effects.meeting_occurrence('cal:event', START): update})['nudges']
    assert len(nudges) == 1
    assert effects.prep_location_update_valid(nudges[0]['location_update'], NOW.timestamp())
    assert effects.prep_location_candidate(event('Room C'), NOW.timestamp()) is None
    path.write_text(json.dumps({**cache, 'complete': False}))
    assert effects.prep_location_candidate(event(), NOW.timestamp()) is None


def test_corrupt_location_receipt_withholds_update_without_rewriting_file(tmp_path):
    path = Path(effects.proactive_path(effects.meeting_day(START)))
    path.parent.mkdir(); path.write_text('{')
    assert effects.prep_location_candidate(event(), NOW.timestamp()) is None
    assert path.read_text() == '{'


def test_full_composition_stages_only_covered_location_and_route(monkeypatch, tmp_path):
    import test_meeting_prep as prep_tests
    monkeypatch.setenv('SOTTO_SKILL_COMPOSITION_PROOF', json.dumps({'route_digest': ROUTE}))
    start = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    inputs = {'google': {'userEmail': 'owner@inside.test', 'events': [
        {**event(' Room  A '), 'id': 'covered', 'start': start},
        {**event('Room C'), 'id': 'uncovered', 'start': start}]}}
    response = {'prep_markdown': 'Useful prep', 'meetings': [
        {'event_id': 'covered', 'start': start,
         'talking_points': ['Their September 18 launch moved enterprise onboarding to October.']}]}
    prep_tests.mp.compose(inputs, llm=lambda *_: json.dumps(response))
    staged = json.loads((tmp_path / 'events' / ('delivery-effects-' + 'a' * 32 + '.json')).read_text())
    full_effects = [row for row in staged['effects'] if row['kind'] == 'meeting_prep_delivered']
    assert full_effects == [{'kind': 'meeting_prep_delivered', 'mode': 'full',
                            'calendar_event_id': 'covered', 'calendar_start': start,
                            'prep_location': 'Room A', 'prep_route_digest': ROUTE}]
