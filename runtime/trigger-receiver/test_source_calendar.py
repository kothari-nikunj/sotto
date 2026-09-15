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
                    local_today=lambda: now.date().isoformat())
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
    cc.HOOKS['calendar_change'] = lambda event: changes.append(event) or True
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
    assert changes[0]['change'] == 'cancelled'


def test_no_prior_success_has_no_fabricated_fresh_timestamp(calendar):
    cc, state, _, _ = calendar
    state['timeout'] = True
    result = cc.snapshot()
    assert result['events'] == [] and result['generated_at'] == ''
    assert result['unavailable'] and not cc.refresh_once()


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
    cc.HOOKS['calendar_change'] = lambda event: attempts.append(event['summary']) or len(attempts) == 1
    assert cc.change_tick(now) == 1
    assert attempts == ['First invite', 'Second invite']
    _restart_change_detector(cc)
    cc.HOOKS['calendar_change'] = lambda event: changes.append(event) or True
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
