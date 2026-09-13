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
