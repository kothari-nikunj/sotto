"""Parking needs an accepted warning, including upgrades, retries and intervening user edits."""
from datetime import datetime

from test_parked_loops import _accept_notice, _env, _fm, _loop, _resolve, cb, cr, li
from delivery_effects import finalize, loop_version


def test_old_upgrade_row_survives_failed_evening_until_warning_acceptance(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {})
    _loop(tmp_path, "old", created_at="2026-07-01")
    # The real runner resolves before composing. An old row must still reach that composition.
    assert [r["anchor_key"] for r in _resolve()["active"]] == ["old"]
    out = cb._append_receipts({"brief_markdown": "Good evening\n\n## Filtered\nNothing else.\n"},
                             {"type": "evening"}, datetime(2026, 9, 18, 18))
    effects = out["_parking_notices"]
    assert len(effects) == 1 and effects[0]["anchor_key"] == "old"
    assert "Maya Chen" in out["brief_markdown"]
    assert not finalize(effects, {})  # composition, failure and no acceptance cannot hide a task
    assert _resolve(today="2026-09-19")["parked"] == []
    assert finalize(effects, {"accepted_at": "2026-09-19T18:00:00Z"})
    assert _resolve(today="2026-09-19")["parked"] == []  # a full local-date boundary to act
    assert [r["anchor_key"] for r in _resolve(today="2026-09-20")["parked"]] == ["old"]


def test_warning_ack_is_idempotent_and_uses_accepted_local_day(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv("SOTTO_TIMEZONE", "America/Los_Angeles")
    _loop(tmp_path, "old", created_at="2026-07-01")
    row = _fm(tmp_path, "old")
    effects = [{"kind": "parking_notice", "anchor_key": "old",
                "loop_version": loop_version(row), "touch": li.last_touch_day(row)}]
    receipt = {"accepted_at": "2026-09-19T01:30:00Z"}  # September 18 locally
    assert finalize(effects, receipt)
    assert _fm(tmp_path, "old")["parking_notice_at"] == "2026-09-18"
    assert finalize(effects, receipt)
    assert _resolve()["parked"] == []
    assert [r["anchor_key"] for r in _resolve(today="2026-09-19")["parked"]] == ["old"]


def test_recapture_between_compose_and_ack_invalidates_old_notice(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "old", created_at="2026-07-01")
    row = _fm(tmp_path, "old")
    effects = [{"kind": "parking_notice", "anchor_key": "old",
                "loop_version": loop_version(row), "touch": li.last_touch_day(row)}]
    current = cr._load_items()["old"]
    current["source_brief_at"] = "2026-09-18 18:05:00"
    cr._persist(current)
    assert finalize(effects, {"accepted_at": "2026-09-18T18:06:00Z"})
    assert not _fm(tmp_path, "old").get("parking_notice_at")
    assert _resolve(today="2026-10-10")["parked"] == []  # a new notice is owed for the new touch


def test_deadline_edit_after_acceptance_requires_new_notice(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {})
    _loop(tmp_path, "old", created_at="2026-07-01")
    _accept_notice(tmp_path, "old")
    row = cr._load_items()["old"]
    row["deadline"] = "2026-09-19"
    cr._persist(row)
    assert cb._parking_tomorrow("2026-09-18") == []
    assert [r["anchor_key"] for r in cb._parking_tomorrow("2026-09-19")] == ["old"]
    assert _resolve(today="2026-09-20")["parked"] == []
    assert not li.parking_notice_current(_fm(tmp_path, "old"))
