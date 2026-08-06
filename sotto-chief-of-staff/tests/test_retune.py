"""retune_scan / retune_apply — clear stale loops (dismiss/snooze/keep) + mute suggestions."""
import importlib.util
import json
import os
import sys

import yaml

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))
sys.path.insert(0, os.path.join(ROOT, "_shared", "scripts"))
sys.path.insert(0, os.path.join(ROOT, "morning-brief", "scripts"))


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
