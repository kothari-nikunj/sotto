"""proactive_scan.py — quiet hours, meeting lead window, birthdays, dedup."""
import importlib.util, os, sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
spec = importlib.util.spec_from_file_location("ps", os.path.join(ROOT, "proactive", "scripts", "proactive_scan.py"))
ps = importlib.util.module_from_spec(spec); spec.loader.exec_module(ps)


def _at(hour):
    return datetime.now(timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0)


def test_quiet_hours_suppress_everything():
    out = ps.scan(calendar=[], continuity=[], local={}, user_email="me@x.com", now_local=_at(23))
    assert out["quiet"] is True and out["nudges"] == []


def test_meeting_prep_fires_for_external_meeting_in_window():
    now = _at(10)
    soon = (now + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S%z")
    cal = [{"id": "ev1", "summary": "Pitch", "start": soon,
            "attendees": [{"email": "me@x.com", "self": True}, {"email": "vc@fund.com", "displayName": "VC"}]}]
    out = ps.scan(cal, [], {}, "me@x.com", now)
    kinds = {n["kind"] for n in out["nudges"]}
    assert "meeting_prep" in kinds
    # an internal-only meeting in-window does NOT nudge
    cal2 = [{"id": "ev2", "summary": "Standup", "start": soon, "attendees": [{"email": "me@x.com", "self": True}]}]
    assert not ps.scan(cal2, [], {}, "me@x.com", now)["nudges"]


def test_meeting_outside_window_skipped():
    now = _at(10)
    far = (now + timedelta(minutes=120)).strftime("%Y-%m-%dT%H:%M:%S%z")
    cal = [{"id": "ev3", "summary": "Later", "start": far, "attendees": [{"email": "x@y.com"}]}]
    assert not ps.scan(cal, [], {}, "me@x.com", now)["nudges"]


def test_commitment_due_today_and_birthday():
    now = _at(10)
    today = now.strftime("%Y-%m-%d")
    cont = [{"id": "c1", "title": "Send the LOI", "deadline": today}]
    local = {"contacts": [{"name": "Jordan", "birthday": now.strftime("%m-%d")}]}
    out = ps.scan([], cont, local, "me@x.com", now)
    kinds = {n["kind"] for n in out["nudges"]}
    assert "commitment" in kinds and "birthday" in kinds


def test_dedup_marks_and_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    now = ps.cb._now_local("+00:00")
    date = now.strftime("%Y-%m-%d")
    seen = ps._load_state(date)
    assert seen == set()
    ps._save_state(date, {"bday:jordan"})
    assert "bday:jordan" in ps._load_state(date)


def test_retune_offer_fires_when_pile_heavy_and_cooldown_ok():
    now = _at(10)
    out = ps.scan([], [], {}, "me@x.com", now, stale_count=6, retune_offer_allowed=True)
    n = [x for x in out["nudges"] if x["kind"] == "retune_offer"]
    assert n and "6 items" in n[0]["detail"]


def test_retune_offer_suppressed_below_threshold_or_in_cooldown():
    now = _at(10)
    # below threshold → nothing even if allowed
    assert not [x for x in ps.scan([], [], {}, "me@x.com", now, 5, True)["nudges"] if x["kind"] == "retune_offer"]
    # at threshold but still in cooldown → nothing
    assert not [x for x in ps.scan([], [], {}, "me@x.com", now, 9, False)["nudges"] if x["kind"] == "retune_offer"]
    # quiet hours suppress it regardless
    assert ps.scan([], [], {}, "me@x.com", _at(23), 20, True)["nudges"] == []


def test_retune_cooldown_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_RETUNE_OFFER_COOLDOWN_DAYS", "7")
    assert ps._retune_cooldown_ok("2026-06-25") is True          # never offered → allowed
    ps._stamp_retune_offer("2026-06-25")
    assert ps._retune_cooldown_ok("2026-06-28") is False         # 3 days later → still cooling down
    assert ps._retune_cooldown_ok("2026-07-03") is True          # 8 days later → allowed again
