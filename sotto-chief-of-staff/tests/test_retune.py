"""retune_scan / retune_apply — clear stale loops (dismiss/snooze/keep) + mute suggestions."""
import importlib.util
import json
import os
import sys

import yaml

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


scan = _load("retune_scan", "_shared/scripts/retune_scan.py")
ap = _load("retune_apply", "_shared/scripts/retune_apply.py")
lq = _load("loops_query", "_shared/scripts/loops_query.py")


def _loop(tmp_path, key, **fm):
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    fm.setdefault("anchor_key", key)
    fm.setdefault("status", "open")
    fm.setdefault("contact_name", "Someone")
    fm.setdefault("action_type", "reply")
    fm.setdefault("created_at", "2026-01-01")           # very old → stale by age
    (d / f"{key}.md").write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n")


def test_scan_flags_stale_and_classifies(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    _loop(tmp_path, "k1", contact_name="Maria", action_type="reply", summary="reply re: budget")
    _loop(tmp_path, "k2", contact_name="Acme", action_type="waiting_on", summary="awaiting their quote")
    _loop(tmp_path, "k3", contact_name="Fresh", created_at="2099-01-01", times_surfaced=1)  # future = not stale
    out = scan.scan()
    keys = {s["anchor_key"] for s in out["stale_loops"]}
    assert "k1" in keys and "k2" in keys and "k3" not in keys
    owe = next(s for s in out["stale_loops"] if s["anchor_key"] == "k1")
    wait = next(s for s in out["stale_loops"] if s["anchor_key"] == "k2")
    assert owe["direction"] == "you_owe" and owe["suggestion"] == "do it or dismiss"
    assert wait["direction"] == "waiting_on_them" and wait["suggestion"] == "nudge or drop"


def test_scan_mute_suggestions_from_deprioritization(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    # behavioral learner output + an already-muted person who must NOT be re-suggested
    (tmp_path / "preferences.json").write_text(json.dumps({
        "deprioritization_hints": ["Bob|reply", "Carol|follow_up"],
        "explicit": {"mute_people": ["Carol"], "mute_senders": [], "mute_sections": [], "tone_notes": []}}))
    out = scan.scan()
    names = [m["name"] for m in out["mute_suggestions"]]
    assert names == ["Bob"]                              # Carol already muted → filtered out
    assert out["current"]["mute_people"] == ["Carol"]


def test_dismiss_removes_from_scan_and_loops(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    _loop(tmp_path, "k1", contact_name="Maria")
    assert ap.apply("dismiss", "k1")["ok"] is True
    assert scan.scan()["stale_loops"] == []             # gone from the retune list
    assert lq.query()["counts"]["you_owe"] == 0         # and from the open-loops view
    fm = yaml.safe_load((tmp_path / "knowledge" / "continuity" / "k1.md").read_text().split("---")[1])
    assert fm["status"] == "dismissed"


def test_snooze_hides_until_date(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    _loop(tmp_path, "k1", contact_name="Maria")
    r = ap.apply("snooze", "k1", 7)
    assert r["ok"] and "until" in r["detail"]
    assert scan.scan()["stale_loops"] == []             # hidden from retune
    assert lq.query()["counts"]["you_owe"] == 0         # and from open loops
    fm = yaml.safe_load((tmp_path / "knowledge" / "continuity" / "k1.md").read_text().split("---")[1])
    assert fm["status"] == "open" and fm.get("snoozed_until")   # still open, just deferred


def test_apply_unknown_key_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    r = ap.apply("dismiss", "nope")
    assert r["ok"] is False


def test_twice_chased_waiting_on_is_handed_to_the_retune_lane(tmp_path, monkeypatch):
    """After CHASE_MAX chases continuity_resolve stops nudging; the item lands here and the verb
    says what happened, so "nudge or drop" isn't offered as if nothing had been tried."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    _loop(tmp_path, "k1", contact_name="Acme", action_type="waiting_on",
          summary="the signed contract", chased_count=2, last_chased_at="2026-01-05")
    _loop(tmp_path, "k2", contact_name="Vendor", action_type="waiting_on", summary="a quote")
    out = {s["anchor_key"]: s for s in scan.scan()["stale_loops"]}
    assert out["k1"]["suggestion"] == "chased twice — nudge again or drop"
    assert out["k1"]["chased_count"] == 2
    assert out["k2"]["suggestion"] == "nudge or drop" and out["k2"]["chased_count"] == 0


def test_keep_on_a_waiting_on_resets_the_chase_not_the_age_clock(tmp_path, monkeypatch):
    """"I still care" about a debt someone owes you means a fresh chase clock. It must NOT reset
    created_at: a waiting_on has no 7-day expiry to reset, and the reset only pushes the next chase
    out and hides the item from the cleanup list — the opposite of what the user just asked for."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    _loop(tmp_path, "w", contact_name="Acme", action_type="waiting_on", summary="the contract",
          created_at="2026-01-01", chased_count=2, last_chased_at="2026-01-05",
          chase_after="2026-01-08", chase_pending="2026-01-05")
    assert ap.apply("keep", "w")["ok"] is True
    fm = yaml.safe_load((tmp_path / "knowledge" / "continuity" / "w.md").read_text().split("---")[1])
    assert fm["created_at"] == "2026-01-01"                       # age clock untouched
    assert not any(k in fm for k in ("chased_count", "chase_after", "last_chased_at", "chase_pending"))
    assert scan.scan()["stale_loops"][0]["suggestion"] == "nudge or drop"   # chaseable again


def test_keep_and_snooze_still_reset_the_age_clock_on_what_the_user_owes(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    today = scan._now_local("+00:00").strftime("%Y-%m-%d")
    _loop(tmp_path, "k", contact_name="Maria", action_type="reply", created_at="2026-01-01")
    ap.apply("keep", "k")
    fm = yaml.safe_load((tmp_path / "knowledge" / "continuity" / "k.md").read_text().split("---")[1])
    assert fm["created_at"] == today
    # …and a snoozed waiting_on keeps its real age too
    _loop(tmp_path, "w", contact_name="Acme", action_type="waiting_on", created_at="2026-01-01")
    ap.apply("snooze", "w", 7)
    fm = yaml.safe_load((tmp_path / "knowledge" / "continuity" / "w.md").read_text().split("---")[1])
    assert fm["created_at"] == "2026-01-01" and fm.get("snoozed_until")


def test_chased_out_is_exposed_so_the_hand_off_can_bypass_the_tidy_up_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    _loop(tmp_path, "k1", contact_name="Acme", action_type="waiting_on", chased_count=2)
    _loop(tmp_path, "k2", contact_name="Vendor", action_type="waiting_on", chased_count=1)
    out = {s["anchor_key"]: s for s in scan.scan()["stale_loops"]}
    assert out["k1"]["chased_out"] is True and out["k2"]["chased_out"] is False
