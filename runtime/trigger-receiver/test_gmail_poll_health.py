"""Repeated Gmail poll failures reach the brief's source note (audit E1)."""
from pathlib import Path

import pytest

import receiver as rec

PACK = Path(__file__).resolve().parents[2] / 'sotto-chief-of-staff'


def _run_polls(monkeypatch, outcomes, observe=lambda _n: None):
    """One poll loop over `outcomes` (True = success); it stops at the sleep after the last poll."""
    calls = []

    def poll():
        observe(len(calls))
        calls.append(True)
        if outcomes[len(calls) - 1]:
            return []
        raise RuntimeError('PRIVATE provider body')
    ticks = iter(range(len(outcomes) - 1))
    monkeypatch.setattr(rec, '_poll_gmail_once', poll)
    monkeypatch.setattr(rec.time, 'sleep', lambda _secs: next(ticks))
    with pytest.raises(StopIteration):
        rec._gmail_poll_loop(90)


def test_ten_failed_polls_pause_email_alerts_in_the_brief_and_a_success_clears_it(tmp_path, monkeypatch):
    monkeypatch.setenv('SOTTO_DATA', str(tmp_path))
    monkeypatch.delenv('SOTTO_DEPLOYMENT_MODE', raising=False)
    monkeypatch.syspath_prepend(str(PACK / '_shared' / 'scripts'))
    import compose_brief as cb
    import gmail_poll_health as health
    paused_before_last = []
    _run_polls(monkeypatch, [False] * rec.GMAIL_FAIL_ALERT_AFTER,
               lambda n: n == rec.GMAIL_FAIL_ALERT_AFTER - 1 and paused_before_last.append(health.paused_since()))
    assert paused_before_last == [None]
    assert health.paused_since()
    assert 'PRIVATE' not in (tmp_path / 'config' / 'source-state.json').read_text()
    prompt = cb.build_prompt(cb._load_prompt(), {'type': 'morning', 'google': {'events': []}, 'local': {}})
    assert 'Email alerts' in prompt
    _run_polls(monkeypatch, [True])
    assert health.paused_since() is None


def test_health_bookkeeping_never_stops_or_blocks_polling(monkeypatch):
    from types import SimpleNamespace
    def locked(_paused):
        raise TimeoutError('receipt lock busy')
    fake = SimpleNamespace(paused_since=lambda: '2026-11-09T00:00:00+00:00', record=locked)
    real = rec._load_shared_lib
    monkeypatch.setattr(rec, '_load_shared_lib', lambda name: fake if name == 'gmail_poll_health' else real(name))
    handled = []
    monkeypatch.setattr(rec, 'handle_events', lambda payload: handled.append(payload) or (200, {}))
    monkeypatch.setattr(rec, '_ack_gmail_events', lambda rows: None)
    outcomes = iter([[{'rowid': 'fresh'}]] + [None] * rec.GMAIL_FAIL_ALERT_AFTER + [[{'rowid': 'later'}]])

    def poll():
        value = next(outcomes)
        if value is None:
            raise RuntimeError('poll failed')
        return value
    ticks = iter(range(rec.GMAIL_FAIL_ALERT_AFTER + 1))
    monkeypatch.setattr(rec, '_poll_gmail_once', poll)
    monkeypatch.setattr(rec.time, 'sleep', lambda _secs: next(ticks))
    with pytest.raises(StopIteration):  # the loop ran every poll; a receipt failure ended nothing
        rec._gmail_poll_loop(90)
    assert [p['events'][0]['rowid'] for p in handled] == ['fresh', 'later']
