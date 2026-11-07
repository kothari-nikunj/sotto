"""Calendar source failures must never manufacture a fresh empty schedule or cancellation."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('isolated_calcache', Path(__file__).with_name('calcache.py'))
    cc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cc)
    now = datetime.now(timezone.utc)
    cc.HOOKS.update(data_root=lambda: str(tmp_path), find_script=lambda *a: 'synthetic_gather.py',
                    local_today=lambda: now.date().isoformat(), event_handled=lambda event: False)
    def write(path, value):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(value))
    cc.HOOKS['write_json'] = write
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    meeting = {'id': 'synthetic-meeting', 'summary': 'Synthetic meeting',
               'start': (now + timedelta(hours=1)).isoformat(), 'end': (now + timedelta(hours=2)).isoformat(),
               'attendees': [{'email': 'guest@example.com', 'displayName': 'Guest'}]}
    observation = {'status': 'ok', 'complete': True, 'observed_at': now.isoformat(),
                   'coverage': {'since': now.isoformat(), 'until': (now + timedelta(days=3)).isoformat()}}
    state = {'events': [meeting], 'observation': observation, 'timeout': False}
    def gather(command, **kwargs):
        if state['timeout']:
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        write(command[command.index('--cal-out') + 1], state['events'])
        write(command[command.index('--source-results-out') + 1], {'calendar': state['observation']})
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(cc.subprocess, 'run', gather)
    changes = []
    cc.HOOKS['calendar_change_batch'] = (
        lambda events: (changes.extend(events), {event['rowid'] for event in events})[1])
    return cc, state, now, changes


@pytest.mark.parametrize('failure', ['timeout', 'partial', 'unavailable'])
def test_failed_read_retains_valid_cache_age_and_does_not_cancel_then_recovers(calendar, failure):
    cc, state, now, changes = calendar
    assert cc.refresh_once()
    first = json.loads(Path(cc.cache_path()).read_text())
    cc.change_tick(now)
    assert changes == []
    cc._CAL_CACHE['ts'] = 0
    state['events'] = []
    if failure == 'timeout':
        state['timeout'] = True
    else:
        state['observation'] = {**state['observation'], 'status': failure, 'complete': False}
    assert not cc.refresh_once()
    failed = cc.snapshot()
    assert failed['events'] and failed['generated_at'] == first['generated_at']
    assert json.loads(Path(cc.cache_path()).read_text()) == first
    assert cc.change_tick(now) == 0 and changes == []
    # A verified complete empty observation is different: it can clear a valid window.
    state['timeout'] = False
    state['observation'] = {**state['observation'], 'status': 'ok', 'complete': True}
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    assert cc.snapshot()['events'] == []
    assert cc.change_tick(now) == 1
    assert changes[0]['change'] == 'removed'
    assert 'cancelled' not in changes[0]['text']


def test_no_prior_success_has_no_fabricated_fresh_timestamp(calendar):
    cc, state, _, _ = calendar
    state['timeout'] = True
    result = cc.snapshot()
    assert result['events'] == [] and result['generated_at'] == ''
    assert result['unavailable'] and not cc.refresh_once()


@pytest.mark.parametrize('extra, taps', [({'transparency': 'transparent'}, 0), ({'eventType': 'workingLocation'}, 0)])
def test_free_event_remains_visible_without_holding_but_only_meetings_get_followup(calendar, extra, taps):
    cc, state, now, _ = calendar
    state['events'][0].update(extra, calendar_id='primary',
                              start=(now - timedelta(hours=1)).isoformat(),
                              end=(now - timedelta(minutes=10)).isoformat())
    # Pin the local date to the event even when this test happens to run near UTC midnight.
    day = state['events'][0]['start'][:10]
    cc.HOOKS['local_today'] = lambda: day
    assert cc.refresh_once()
    events = cc.snapshot()['events']
    assert len(events) == 1 and events[0]['busy'] is False
    rows = json.loads(Path(cc.cache_path()).read_text())['events']
    assert len(rows) == 1 and rows[0]['busy'] is False
    assert rows[0]['id'] == 'synthetic-meeting'  # presence remains available to delivery checks
    assert len(cc.ended_meetings(events, now, today=day)) == taps


def test_disabled_calendar_clears_cached_projection_without_emitting_cancellations(calendar):
    cc, state, now, changes = calendar
    cc.refresh_once()
    cc.change_tick(now)
    state['observation'] = {**state['observation'], 'status': 'disabled', 'complete': False}
    state['events'] = []
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    result = cc.snapshot()
    assert result['status'] == 'disabled' and result['events'] == []
    assert cc.change_tick(now) == 0 and changes == []


def test_supporting_context_does_not_create_another_invite_or_prep(calendar):
    cc, state, now, changes = calendar
    state['events'][0]['summary'] = 'Meeting with Acme'
    cc.refresh_once()
    cc.change_tick(now)
    note = {**state['events'][0], 'id': 'notes', 'summary': 'CONTEXT: Acme', 'description': 'Prep evidence'}
    state['events'].append(note)
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    assert len(cc.snapshot()['events']) == 1
    assert cc.change_tick(now) == 0 and changes == []


def test_removing_orphan_prep_note_does_not_cancel_moved_meeting(calendar):
    cc, state, now, changes = calendar
    meeting = {**state['events'][0], 'summary': 'Meeting with Acme'}
    # Before a move, the prep note can have a different end time and remain unfused.
    note = {**meeting, 'id': 'notes', 'summary': 'CONTEXT: Acme',
            'end': (now + timedelta(hours=3)).isoformat()}
    state['events'] = [meeting, note]
    cc.refresh_once()
    cc.change_tick(now)
    state['events'] = [{**meeting, 'start': (now + timedelta(minutes=30)).isoformat()}]
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once() and cc.change_tick(now) == 1
    assert [row['change'] for row in changes] == ['moved']
    assert 'CONTEXT' not in changes[0]['text']


def test_only_explicit_status_proves_cancellation(calendar):
    cc, state, now, _ = calendar
    meeting = state['events'][0]
    rows = cc.calendar_changes([meeting], [{**meeting, 'status': 'cancelled'}], now, 'owner@example.com')
    assert len(rows) == 1 and rows[0]['kind'] == 'cancelled'
    assert 'cancelled' in cc.change_event(rows[0])['text']
    removed, = cc.calendar_changes([meeting], [], now, 'owner@example.com')
    assert removed['kind'] == 'removed' and 'cancelled' not in cc.change_event(removed)['text']


def test_replaced_id_uses_unique_uid_to_recognize_move(calendar):
    cc, state, now, _ = calendar
    old = {**state['events'][0], 'iCalUID': 'stable@example.com'}
    new = {**old, 'id': 'replacement', 'start': (now + timedelta(minutes=30)).isoformat()}
    move, = cc.calendar_changes([old], [new], now, 'owner@example.com')
    assert move['kind'] == 'moved' and move['calendar_event_id'] == 'replacement'
    assert 'moved to' in cc.change_event(move)['text']
    assert now.strftime('%b') in cc.change_event(move)['text']
    # Two occurrences of a recurring event share a UID; never pair by similarity.
    another = {**old, 'id': 'another', 'start': (now + timedelta(days=1)).isoformat()}
    rows = cc.calendar_changes([old, another], [new], now, 'owner@example.com')
    assert all(row['kind'] != 'moved' for row in rows)
    cc._LAST_RAW.update(observed_at=now.isoformat())
    assert cc._change_events([old])[0]['iCalUID'] == old['iCalUID']


def test_single_recurring_occurrence_is_not_joined_to_another_occurrence(calendar):
    cc, state, now, _ = calendar
    old = {**state['events'][0], 'iCalUID': 'recurring@example.com',
           'recurringEventId': 'series', 'originalStartTime': {'dateTime': state['events'][0]['start']}}
    new = {**old, 'id': 'next-occurrence', 'start': (now + timedelta(hours=4)).isoformat(),
           'originalStartTime': {'dateTime': (now + timedelta(hours=4)).isoformat()}}
    rows = cc.calendar_changes(cc._change_events([old]), [new], now, 'owner@example.com')
    assert all(row['kind'] != 'moved' for row in rows)


def _restart_change_detector(cc):
    cc._CHANGE_BASELINE.update(events=None, source=None, account="", loaded=False,
                               acknowledged=set())


def test_complete_baseline_survives_restart_and_detects_deployment_window_change(calendar):
    cc, state, now, changes = calendar
    assert cc.refresh_once()
    assert cc.change_tick(now) == 0
    state['events'][0]['start'] = (now + timedelta(hours=2)).isoformat()
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    _restart_change_detector(cc)
    assert cc.change_tick(now) == 1
    assert [change['change'] for change in changes] == ['moved']
    persisted = json.loads(Path(cc.change_state_path()).read_text())
    assert persisted['events'][0]['start'] == state['events'][0]['start']
    assert persisted['source']['status'] == 'ok' and persisted['source']['complete'] is True


def test_invalid_persisted_baseline_is_never_used_to_infer_changes(calendar):
    cc, state, now, changes = calendar
    write = cc.HOOKS['write_json']
    write(cc.change_state_path(), {'version': 1, 'events': [], 'acknowledged': [],
                                   'account': 'owner@example.com',
                                   'source': {'status': 'partial', 'complete': False,
                                              'coverage': {}}})
    assert cc.refresh_once()
    _restart_change_detector(cc)
    assert cc.change_tick(now) == 0 and changes == []
    saved = json.loads(Path(cc.change_state_path()).read_text())
    assert saved['events'] == state['events'] and saved['source']['complete'] is True


def test_restart_resumes_partial_batch_without_replaying_acknowledged_change(calendar):
    cc, state, now, changes = calendar
    assert cc.refresh_once() and cc.change_tick(now) == 0
    first = {**state['events'][0], 'id': 'first', 'summary': 'First invite',
             'start': (now + timedelta(minutes=30)).isoformat()}
    second = {**state['events'][0], 'id': 'second', 'summary': 'Second invite',
              'start': (now + timedelta(minutes=45)).isoformat()}
    state['events'].extend([first, second])
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    attempts = []
    def partial(events):
        attempts.extend(event['summary'] for event in events)
        return {events[0]['rowid']}
    cc.HOOKS['calendar_change_batch'] = partial
    assert cc.change_tick(now) == 1
    assert attempts == ['First invite', 'Second invite']
    _restart_change_detector(cc)
    cc.HOOKS['calendar_change_batch'] = (
        lambda events: (changes.extend(events), {event['rowid'] for event in events})[1])
    assert cc.change_tick(now) == 1
    assert [change['summary'] for change in changes] == ['Second invite']
    _restart_change_detector(cc)
    assert cc.change_tick(now) == 0


def test_disabled_lane_advances_durable_baseline_without_replaying_on_reenable(calendar,
                                                                                monkeypatch):
    cc, state, now, changes = calendar
    assert cc.refresh_once() and cc.change_tick(now) == 0
    state['events'].append({**state['events'][0], 'id': 'while-disabled',
                            'start': (now + timedelta(minutes=30)).isoformat()})
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    monkeypatch.setenv('SOTTO_CALENDAR_NUDGES', '0')
    assert cc.change_tick(now) == 0 and changes == []
    monkeypatch.setenv('SOTTO_CALENDAR_NUDGES', '1')
    _restart_change_detector(cc)
    assert cc.change_tick(now) == 0 and changes == []


def test_calendar_permission_revocation_invalidates_baseline_before_regrant(calendar):
    cc, state, now, changes = calendar
    assert cc.refresh_once() and cc.change_tick(now) == 0
    state['observation'] = {**state['observation'], 'status': 'disabled', 'complete': False}
    state['events'] = []
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    revoked = json.loads(Path(cc.change_state_path()).read_text())
    assert revoked['source']['status'] == 'disabled' and revoked['events'] == []
    state['observation'] = {**state['observation'], 'status': 'ok', 'complete': True}
    state['events'] = [{
        'id': 'after-regrant', 'summary': 'New calendar meeting',
        'start': (now + timedelta(minutes=30)).isoformat(),
        'end': (now + timedelta(hours=1)).isoformat(),
        'attendees': [{'email': 'guest@example.com', 'displayName': 'Guest'}],
    }]
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    _restart_change_detector(cc)
    assert cc.change_tick(now) == 0 and changes == []


def test_connected_account_change_seeds_new_baseline_without_cross_account_diff(calendar,
                                                                                monkeypatch):
    cc, state, now, changes = calendar
    assert cc.refresh_once() and cc.change_tick(now) == 0
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'other-owner@example.net')
    state['events'] = [{**state['events'][0], 'id': 'other-account-event',
                        'start': (now + timedelta(minutes=30)).isoformat()}]
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    _restart_change_detector(cc)
    assert cc.change_tick(now) == 0 and changes == []
    saved = json.loads(Path(cc.change_state_path()).read_text())
    assert saved['account'] == 'other-owner@example.net'


def test_complete_status_without_observation_bounds_cannot_advance_baseline(calendar):
    cc, state, now, changes = calendar
    assert cc.refresh_once() and cc.change_tick(now) == 0
    before = Path(cc.change_state_path()).read_text()
    state['events'].append({**state['events'][0], 'id': 'unbounded'})
    state['observation'] = {'status': 'ok', 'complete': True, 'observed_at': ''}
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    assert cc.change_tick(now) == 0 and changes == []
    assert Path(cc.change_state_path()).read_text() == before


def test_unknown_account_identity_never_seeds_or_compares_durable_baseline(calendar,
                                                                           monkeypatch):
    cc, state, now, changes = calendar
    monkeypatch.delenv('SOTTO_USER_EMAIL')
    assert cc.refresh_once()
    assert cc.change_tick(now) == 0 and changes == []
    marker = json.loads(Path(cc.change_state_path()).read_text())
    assert marker['source']['status'] == 'identity_unknown' and marker['events'] == []
    state['events'].append({**state['events'][0], 'id': 'unknown-calendar-invite',
                            'start': (now + timedelta(minutes=30)).isoformat()})
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    assert cc.change_tick(now) == 0 and changes == []
    assert json.loads(Path(cc.change_state_path()).read_text())['source']['status'] == 'identity_unknown'


def test_unknown_to_known_account_transition_seeds_without_replaying(calendar, monkeypatch):
    cc, state, now, changes = calendar
    monkeypatch.delenv('SOTTO_USER_EMAIL')
    assert cc.refresh_once() and cc.change_tick(now) == 0
    state['events'].append({**state['events'][0], 'id': 'before-identity',
                            'start': (now + timedelta(minutes=30)).isoformat()})
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once() and cc.change_tick(now) == 0
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    assert cc.change_tick(now) == 0 and changes == []
    saved = json.loads(Path(cc.change_state_path()).read_text())
    assert saved['account'] == 'owner@example.com'


def test_known_to_unknown_to_same_known_account_does_not_diff_across_identity_gap(calendar,
                                                                                  monkeypatch):
    cc, state, now, changes = calendar
    assert cc.refresh_once() and cc.change_tick(now) == 0
    monkeypatch.delenv('SOTTO_USER_EMAIL')
    assert cc.change_tick(now) == 0
    marker = json.loads(Path(cc.change_state_path()).read_text())
    assert marker['source']['status'] == 'identity_unknown'
    state['events'].append({**state['events'][0], 'id': 'during-unknown-identity',
                            'start': (now + timedelta(minutes=30)).isoformat()})
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once() and cc.change_tick(now) == 0
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    assert cc.change_tick(now) == 0 and changes == []
    saved = json.loads(Path(cc.change_state_path()).read_text())
    assert saved['account'] == 'owner@example.com'


def test_calendar_selection_or_consent_revision_invalidates_cached_events(calendar):
    cc, state, _now, _changes = calendar
    revision = ['a'*64]
    cc.HOOKS['source_revision'] = lambda: revision[0]
    assert cc.snapshot()['events']
    revision[0] = 'b'*64
    state['events'] = []
    state['observation']['status'] = 'partial'
    state['observation']['complete'] = False
    assert cc.snapshot()['events'] == []
    revision[0] = None
    assert cc.snapshot()['status'] == 'unavailable'
    assert cc._LAST_RAW.get('valid') is False


def test_shared_calendar_uid_does_not_join_distinct_calendars(calendar):
    cc, state, now, _ = calendar
    old = {**state['events'][0], 'calendar_id': 'primary', 'iCalUID': 'shared@example.com'}
    new = {**old, 'id': 'shared-event', 'calendar_id': 'team@example.com',
           'start': (now + timedelta(minutes=30)).isoformat()}
    saved = cc._change_events([old])
    assert saved[0]['calendar_id'] == 'primary'
    assert all(row['kind'] != 'moved'
               for row in cc.calendar_changes(saved, [new], now, 'owner@example.com'))


def test_current_calendar_revision_preserves_durable_baseline_across_restart(calendar):
    cc, state, now, changes = calendar
    revision = ['a' * 64]
    cc.HOOKS['source_revision'] = lambda: revision[0]
    assert cc.refresh_once() and cc.change_tick(now) == 0
    cc._CAL_CACHE.clear()
    cc._CAL_CACHE.update(ts=0, value=None)
    _restart_change_detector(cc)
    state['events'][0]['start'] = (now + timedelta(hours=2)).isoformat()
    assert cc.refresh_once() and cc.change_tick(now) == 1
    assert [event['change'] for event in changes] == ['moved']
    # A restart after a selection/consent change still starts a fresh comparison.
    cc._CAL_CACHE.clear()
    cc._CAL_CACHE.update(ts=0, value=None)
    _restart_change_detector(cc)
    revision[0] = 'b' * 64
    state['events'][0]['start'] = (now + timedelta(hours=3)).isoformat()
    assert cc.refresh_once() and cc.change_tick(now) == 0
    assert len(changes) == 1


def test_successful_partial_rows_keep_holds_and_taps_without_absence_claims(calendar):
    cc, state, now, changes = calendar
    event = state['events'][0]
    # The cache projects the local day, which may differ for the future/past fixture near midnight.
    cc.HOOKS['local_today'] = lambda: event['start'][:10]
    event.update(calendar_id='primary', attendees=[{'email': 'owner@example.com', 'responseStatus': 'accepted'},
                                                  {'email': 'guest@example.com'}])
    state['observation'].update(status='partial', complete=False)
    assert cc.refresh_once()
    snap = cc.snapshot()
    assert snap['status'] == 'partial' and snap['complete'] is False
    cache = json.loads(Path(cc.cache_path()).read_text())
    assert cache['events'][0]['attendees'] == 1 and cache['complete'] is False
    event.update(start=(now-timedelta(minutes=30)).isoformat(), end=(now-timedelta(minutes=10)).isoformat())
    cc._CAL_CACHE['ts'] = 0
    sent = []
    cc.HOOKS['meeting_tap'] = lambda row: sent.append(row) or True
    assert cc.tap_tick(now) == 1 and len(sent) == 1
    assert cc.change_tick(now) == 0 and changes == []


def test_other_calendar_owners_meeting_is_context_only(calendar):
    cc, state, now, changes = calendar
    event = state['events'][0]
    event.update(calendar_id='colleague@example.com', organizer={'email': 'colleague@example.com', 'self': True})
    event['attendees'].append({'email': 'colleague@example.com', 'self': True})
    assert cc.refresh_once()
    assert len(cc.snapshot()['events']) == 1
    assert json.loads(Path(cc.cache_path()).read_text())['events'] == []
    event.update(start=(now-timedelta(minutes=30)).isoformat(), end=(now-timedelta(minutes=10)).isoformat())
    cc._CAL_CACHE['ts'] = 0
    assert cc.tap_tick(now) == 0
    assert cc.calendar_changes([], [event], now-timedelta(hours=1), 'owner@example.com') == []


def test_removed_calendar_is_not_a_cancelled_meeting(calendar):
    cc, state, now, changes = calendar
    state['events'][0].update(calendar_id='shared', attendees=[{'email': 'owner@example.com'}, {'email': 'guest@example.com'}])
    state['observation']['calendars'] = [{'calendar_id': 'shared', 'status': 'ok'}, {'calendar_id': 'primary', 'status': 'ok'}]
    assert cc.refresh_once() and cc.change_tick(now) == 0
    state['observation']['calendars'] = [{'calendar_id': 'primary', 'status': 'ok'}]
    state['events'] = []
    cc._CAL_CACHE['ts'] = 0
    assert cc.refresh_once()
    assert cc.change_tick(now) == 0 and changes == []


def test_invite_copies_produce_one_move_and_do_not_create_removed_copy(calendar, monkeypatch):
    cc, state, now, changes = calendar
    monkeypatch.setenv('SOTTO_USER_EMAIL', 'owner@example.com')
    own = {**state['events'][0], 'calendar_id': 'owner@example.com', 'iCalUID': 'invite',
           'attendees': [{'email': 'owner@example.com'}, {'email': 'guest@example.com'}]}
    shared = {**own, 'id': 'copy', 'calendar_id': 'team@group'}
    moved = {**own, 'start': (now+timedelta(hours=2)).isoformat(), 'end': (now+timedelta(hours=3)).isoformat()}
    current = [moved, {**moved, 'id': 'copy', 'calendar_id': 'team@group'}]
    changes = cc.calendar_changes([own, shared], current, now, 'owner@example.com')
    assert len(changes) == 1 and changes[0]['kind'] == 'moved'
    assert cc.calendar_changes([own, shared], [shared], now, 'owner@example.com') == []


def test_primary_copy_disappearing_leaves_same_recurring_shared_invite_silent(calendar):
    cc, state, now, changes = calendar
    own = {**state['events'][0], 'calendar_id': 'primary', 'iCalUID': 'invite', 'recurringEventId': 'series',
           'originalStartTime': {'dateTime': state['events'][0]['start']},
           'attendees': [{'email': 'owner@example.com'}, {'email': 'guest@example.com'}]}
    shared = {**own, 'id': 'copy', 'calendar_id': 'team@group'}
    assert cc.calendar_changes([own, shared], [shared], now, 'owner@example.com', calendar_ids=['team@group']) == []


def test_owner_day_and_overnight_meeting_survive_calendar_projection(calendar, monkeypatch):
    cc, _, _, _ = calendar
    monkeypatch.setenv('SOTTO_TIMEZONE', 'America/Los_Angeles')
    event = {'summary': 'Late meeting', 'start': '2026-10-16T06:30:00Z',
             'end': '2026-10-16T07:15:00Z', 'attendees': [{'email': 'guest@example.com'}]}
    assert cc.today_rows([event], '2026-10-15')
    assert cc.today_rows([event], '2026-10-16')
    assert cc.ended_meetings([event], datetime(2026, 10, 16, 7, 30, tzinfo=timezone.utc), '2026-10-16')
    all_day = {'summary': 'Conference', 'start': '2026-10-14', 'end': '2026-10-17'}
    assert cc.today_rows([all_day], '2026-10-15')
    assert not cc.today_rows([all_day], '2026-10-17')


def test_equivalent_timestamp_offsets_are_not_calendar_moves(calendar):
    cc, state, now, _ = calendar
    old = state['events'][0]
    shifted = {**old, 'start': datetime.fromisoformat(old['start']).astimezone(
        timezone(timedelta(hours=2))).isoformat()}
    assert cc.calendar_changes([old], [shifted], now, 'owner@example.com') == []


def test_periodic_calendar_gather_requests_ended_meetings(calendar, monkeypatch):
    cc, _, _, _ = calendar
    original = cc.subprocess.run
    commands = []
    def capture(argv, **kwargs):
        commands.append(argv)
        return original(argv, **kwargs)
    monkeypatch.setattr(cc.subprocess, 'run', capture)
    cc._run_calendar_gather()
    assert commands[0][commands[0].index('--window-days') + 1] == '2'
