"""Receiver restarts must not lose the nightly session rotation or a wake payload's snapshot seed."""
import importlib.util
import json
import os
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location("receiver_restart_gaps", os.path.join(HERE, "receiver.py"))
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)


class SyncThread:
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _rotations(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    rec._RETENTION_FIRED.clear()
    ran = []
    monkeypatch.setattr(rec, "rotate_chat_sessions", lambda before: ran.append(before) or 1)
    monkeypatch.setattr(rec.threading, "Thread", SyncThread)
    return ran


def _at(monkeypatch, when):
    monkeypatch.setattr(rec, "_local_now", lambda: when)


def test_a_receiver_down_at_the_sweep_minute_still_rotates_the_session_that_day(tmp_path, monkeypatch):
    ran = _rotations(tmp_path, monkeypatch)
    hour, minute = rec.RETENTION.SWEEP_LOCAL
    _at(monkeypatch, datetime(2026, 9, 2, hour - 1, minute))
    rec._session_archive_tick()
    assert ran == [], "never before the sweep minute"
    _at(monkeypatch, datetime(2026, 9, 2, 9, 15, tzinfo=timezone(-timedelta(hours=7))))
    rec._session_archive_tick()
    assert ran == ["2026-09-02T10:30:00Z"]


def test_a_restart_after_todays_rotation_keeps_the_live_conversation(tmp_path, monkeypatch):
    ran = _rotations(tmp_path, monkeypatch)
    hour, minute = rec.RETENTION.SWEEP_LOCAL
    _at(monkeypatch, datetime(2026, 9, 2, hour, minute))
    rec._session_archive_tick()
    assert ran == ["2026-09-02T03:30:00Z"]
    rec._RETENTION_FIRED.clear()                       # a mid-day restart forgets the memory stamp
    _at(monkeypatch, datetime(2026, 9, 2, 14, 0))
    rec._session_archive_tick()
    assert ran == ["2026-09-02T03:30:00Z"], "a restart must not wipe today's conversation context"
    _at(monkeypatch, datetime(2026, 9, 3, hour, minute))
    rec._session_archive_tick()
    assert ran == ["2026-09-02T03:30:00Z", "2026-09-03T03:30:00Z"]


def test_an_unavailable_runtime_does_not_count_as_todays_rotation(tmp_path, monkeypatch):
    ran = _rotations(tmp_path, monkeypatch)
    answers = iter([None, 1])
    monkeypatch.setattr(rec, "rotate_chat_sessions", lambda *args: ran.append(args) or next(answers))
    _at(monkeypatch, datetime(2026, 9, 2, 9, 0))
    rec._session_archive_tick()
    rec._session_archive_tick()
    rec._session_archive_tick()
    cutoff = ("2026-09-02T03:30:00Z",)
    assert ran == [cutoff, cutoff], "retry after runtime startup, then stop for the day"


def _payload(tmp_path, name, age_secs, generated_at="2026-09-02T08:00:00Z"):
    path = os.path.join(str(tmp_path), "briefs", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": generated_at, "contacts": [{"name": "Dana Okafor"}]}, f)
    stamp = time.time() - age_secs
    os.utime(path, (stamp, stamp))
    return path


def _snapshot(tmp_path, age_secs):
    path = os.path.join(str(tmp_path), "knowledge", "last_local_snapshot.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"captured_at": "2026-09-01T08:00:00Z", "local": {}}, f)
    stamp = time.time() - age_secs
    os.utime(path, (stamp, stamp))


def test_the_background_tick_reseeds_a_payload_whose_seed_was_lost(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    rec._RESEEDED.clear()
    seeded = []
    monkeypatch.setattr(rec, "_seed_snapshot_from", lambda p: seeded.append(p) or True)
    monkeypatch.setattr(rec.threading, "Thread", SyncThread)
    monkeypatch.setattr(rec, "_maybe_bootstrap_selfhost_source_read", lambda: None)
    monkeypatch.setattr(rec, "_context_sources_ready", lambda: False)
    _snapshot(tmp_path, 3600)
    lost = _payload(tmp_path, "2026-09-02.morning_ready.payload.json", 600)
    _payload(tmp_path, "2026-09-01.evening_ready.payload.json", 90000)       # outside the recovery window
    _payload(tmp_path, "2026-09-02.evening_ready.payload.json", 5)           # its own seed is in flight
    rec._background_context_tick()
    assert seeded == [lost]
    rec._background_context_tick()
    assert seeded == [lost], "one retry per payload per process, not one per heartbeat"


def test_a_new_snapshot_file_does_not_hide_an_unseeded_payload(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    rec._RESEEDED.clear()
    seeded = []
    monkeypatch.setattr(rec, "_seed_snapshot_from", lambda p: seeded.append(p) or True)
    monkeypatch.setattr(rec.threading, "Thread", SyncThread)
    lost = _payload(tmp_path, "2026-09-02.morning_ready.payload.json", 600)
    _snapshot(tmp_path, 300)  # written later, but captured before the staged wake
    rec._reseed_lost_payloads()
    assert seeded == [lost]


def test_a_failed_wake_seed_is_retried(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    rec._RESEEDED.clear()
    ran = []
    answers = iter([False, True])
    monkeypatch.setattr(rec, "_seed_snapshot_from", lambda p: ran.append(p) or next(answers))
    monkeypatch.setattr(rec.threading, "Thread", SyncThread)
    lost = _payload(tmp_path, "2026-09-02.morning_ready.payload.json", 600)
    for _ in range(3):
        rec._reseed_lost_payloads()
    assert ran == [lost, lost]


def test_seed_subprocess_exit_zero_does_not_confirm_a_failed_write(monkeypatch):
    class Result:
        returncode = 0
        stdout = '{"seeded": false, "reason": "unreadable seed"}'

    monkeypatch.setattr(rec.subprocess, "run", lambda *a, **k: Result())
    assert rec._seed_snapshot_from("/fixture/briefs/test.payload.json") is False


def test_seed_reports_snapshot_write_failure(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    lost = _payload(tmp_path, "2026-09-02.morning_ready.payload.json", 600)
    (tmp_path / "knowledge").touch()  # impossible to create the snapshot below a file
    assert rec._seed_snapshot_from(lost) is False


def test_wake_payload_writes_are_atomic(tmp_path, monkeypatch):
    rec.DATA = str(tmp_path)
    writes = []
    real = rec.CONNECTORS.write_json
    monkeypatch.setattr(rec.CONNECTORS, "write_json",
                        lambda p, o, *a, **k: writes.append(p) or real(p, o, *a, **k))
    monkeypatch.setattr(rec, "_seed_snapshot_from", lambda p: True)
    monkeypatch.setattr(rec.threading, "Thread", SyncThread)
    monkeypatch.setattr(rec, "_record_delivery", lambda *a: None)
    monkeypatch.setattr(rec, "run_skill", lambda s, p: None)
    rec._fold_into_snapshot("morning_ready", "2026-09-02", {"contacts": []}, "already_delivered", "x")
    payload = os.path.join(str(tmp_path), "briefs", "2026-09-02.morning_ready.payload.json")
    assert writes == [payload]
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "onboarding.json").write_text(json.dumps({"phase": "delivered", "completed_at": 1}))
    _at(monkeypatch, datetime(2026, 8, 30, 10, 0))     # outside both brief cron windows
    code, r = rec.handle_trigger({"type": "evening_ready", "date": "2026-09-03",
                                  "local_data": {"contacts": [{"name": "Dana Okafor"}]}})
    assert (code, r["status"]) == (202, "enqueued")
    assert writes[1:] == [os.path.join(str(tmp_path), "briefs", "2026-09-03.evening_ready.payload.json")]
