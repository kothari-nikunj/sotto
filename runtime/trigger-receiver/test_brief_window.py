"""A missed or failed brief run widens the next read window, within a named cap (audit 2.2)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import brief_runner

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'
DAY, CAP, GAP = 24, 72, 52  # the default window, the catch-up cap, a 51.5-hour gap rounded up


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PACK / '_shared' / 'lib'))
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    import brief_window
    return brief_window


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_window_is_24h_by_default_and_reaches_back_to_the_last_success_within_the_cap(window, tmp_path):
    assert window.local_hours() == window.BRIEF_WINDOW_HOURS == DAY
    path = tmp_path / 'cache' / 'brief-window.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'imessage': _ago(51.5), 'whatsapp': _ago(3), 'gmail': _ago(200)}))
    assert window.hours('imessage') == window.local_hours() == GAP  # the oldest Mac source sets the read
    assert window.hours('gmail') == window.CATCH_UP_MAX_HOURS == CAP
    path.write_text(json.dumps({'imessage': _ago(3)}))
    assert window.local_hours() == DAY
    path.write_text('not json')
    assert window.local_hours() == DAY


def test_a_brief_after_a_missed_run_reads_since_the_last_success_and_says_so(window, tmp_path, monkeypatch):
    import gemini_transport
    monkeypatch.setattr(gemini_transport, 'brief_model_preflight', lambda: None)
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    (tmp_path / 'cache').mkdir()
    (tmp_path / 'cache' / 'brief-window.json').write_text(json.dumps(
        {'imessage': _ago(51.5), 'whatsapp': _ago(51.5), 'gmail': _ago(51.5)}))
    reads, argv_by_script = [], {}
    monkeypatch.setattr('source_context.read_local', lambda hours, *a, **k: reads.append(hours) or {
        'imessage': [{'text': 'Synthetic'}], 'whatsapp': [], 'source_status': {'imessage': 'ok', 'whatsapp': 'degraded'},
        'source_coverage': {'whatsapp': {'status': 'failed', 'failure_category': 'timeout'}}})

    def invoke(argv, **kwargs):
        name = Path(argv[1]).name
        argv_by_script[name] = argv
        def output(flag, data):
            Path(argv[argv.index(flag) + 1]).write_text(json.dumps(data))
        out = '{}'
        if name == 'gather_google.py':
            output('--gmail-out', [])
            output('--cal-out', [])
            output('--source-results-out', {'gmail': {'status': 'ok'}, 'calendar': {'status': 'ok', 'complete': True}})
        if '--out' in argv:
            output('--out', {})
        if name == 'select_attendees.py':
            out = '[]'
        if name == 'compose_brief.py':
            out = json.dumps({'brief_text': 'Synthetic brief', 'actions': [], 'extracted_knowledge': {}})
        if name == 'learn_step.py':
            out = json.dumps({'completion': 'complete', 'ok': True, 'steps': {}})
        return SimpleNamespace(returncode=0, stdout=out, stderr='')
    monkeypatch.setattr(brief_runner.subprocess, 'run', invoke)
    assert brief_runner.run({'pack': str(PACK), 'kind': 'morning'}) == 'Synthetic brief'
    assert reads == [52]
    google = argv_by_script['gather_google.py']
    assert google[google.index('--catch-up-days') + 1] == '3'
    compose = argv_by_script['compose_brief.py']
    assert compose[compose.index('--window-hours') + 1] == '52'
    assert window.hours('imessage') == window.hours('gmail') == DAY  # these successes close the gap
    assert window.hours('whatsapp') == GAP == window.local_hours()  # a failed read never advances it


def test_the_model_is_told_the_actual_window(window, monkeypatch):
    monkeypatch.syspath_prepend(str(PACK / '_shared' / 'scripts'))
    import compose_brief as cb
    note = cb._catch_up_note({'type': 'morning', 'window_hours': 52})
    assert 'Catch-Up Window' in note and '52 hours' in note
    assert cb._catch_up_note({'type': 'morning', 'window_hours': 24}) == ''
    assert cb._catch_up_note({'type': 'welcome', 'window_hours': 168}) == ''
    prompt = cb.build_prompt(cb._load_prompt(), {'type': 'morning', 'window_hours': 52,
                                                 'google': {'events': []}, 'local': {}})
    assert 'Catch-Up Window' in prompt
