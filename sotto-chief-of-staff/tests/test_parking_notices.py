"""Accepted parking notices are composed once, muted correctly, and exported as effects."""
import importlib.util
import importlib
import os
from datetime import datetime, timezone


HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cb = _load("cb_parking_notices", "_shared/scripts/compose_brief.py")
li = importlib.import_module("ledger_io")
de = importlib.import_module("delivery_effects")


def _row(key, name="Maya Chen", identifier="maya@example.com", summary="the deck"):
    return {"anchor_key": key, "status": "open", "action_type": "reply",
            "contact_name": name, "contact_identifier": identifier, "summary": summary,
            "created_at": "2026-08-01"}


def _quiet_receipts(monkeypatch):
    monkeypatch.setattr(cb, "_ledger_receipts", lambda _day: ([], []))
    monkeypatch.setattr(cb, "_held_count", lambda *_: 0)
    monkeypatch.setattr(cb, "_prepped_count", lambda *_: 0)
    monkeypatch.setattr(cb, "_taps_count", lambda *_: 0)


def test_candidate_read_uses_tomorrow_and_filters_current_notice_and_mutes(monkeypatch):
    rows = [_row("shown"), _row("current", "Current"), _row("person", "Muted Person"),
            _row("sender", "Sender Muted", "muted@example.com")]
    seen = []
    monkeypatch.setattr(li, "load_active", lambda now=None: rows)
    def candidate(row, day):
        seen.append((row["anchor_key"], day))
        return True
    monkeypatch.setattr(li, "park_candidate", candidate, raising=False)
    monkeypatch.setattr(li, "parking_notice_current",
                        lambda row: row["anchor_key"] == "current", raising=False)
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {
        "mute_people": ["Muted Person"], "mute_senders": ["muted@example.com"],
        "mute_sections": [], "tone_notes": []})

    assert [row["anchor_key"] for row in cb._parking_tomorrow("2026-09-18")] == ["shown"]
    assert seen and {day for _key, day in seen} == {"2026-09-19"}

    monkeypatch.setattr(cb, "explicit_prefs", lambda: {
        "mute_people": [], "mute_senders": [], "mute_sections": ["what moved today"],
        "tone_notes": []})
    assert cb._parking_tomorrow("2026-09-18") == []


def test_append_exports_every_rendered_notice_without_stamping_ledger(monkeypatch):
    _quiet_receipts(monkeypatch)
    rows = [_row(f"row-{i}", f"Person {i}", summary=f"task {i}") for i in range(4)]
    monkeypatch.setattr(cb, "_parking_tomorrow", lambda _day: rows)
    monkeypatch.setattr(li, "last_touch_day", lambda row: row["created_at"][:10])
    original = {"brief_markdown": "## Filtered\nNothing else.\n"}

    out = cb._append_receipts(dict(original), {"type": "evening", "google": {"events": []}},
                              datetime(2026, 9, 18, 18))

    assert ("Parks tomorrow unless you say *keep*: Person 0: task 0; Person 1: task 1; "
            "Person 2: task 2; Person 3: task 3.") in out["brief_markdown"]
    assert "/app#loops" not in out["brief_markdown"]
    assert [effect["anchor_key"] for effect in out["_parking_notices"]] == [
        "row-0", "row-1", "row-2", "row-3"]
    assert all(effect["kind"] == "parking_notice" and effect["touch"] == "2026-08-01"
               and effect["loop_version"] for effect in out["_parking_notices"])
    assert all("parking_notice_at" not in row for row in rows)


def test_direct_receipt_names_small_notice_without_em_dash(monkeypatch):
    _quiet_receipts(monkeypatch)
    monkeypatch.setattr(cb, "_parking_tomorrow", lambda _day: [_row("one")])
    lines = cb._receipt_lines({"type": "evening", "google": {"events": []}},
                              "2026-09-18", "+00:00")
    assert lines == ["Parks tomorrow unless you say *keep*: Maya Chen: the deck."]
    assert "—" not in lines[0] and "/app#loops" not in lines[0]


def test_weekly_parked_count_excludes_muted_rows_and_uses_natural_keep_copy(monkeypatch):
    retune_scan = importlib.import_module("retune_scan")
    rows = [{**_row("visible"), "status": "parked"},
            {**_row("person", "Muted Person"), "status": "parked"},
            {**_row("sender", "Muted Sender", "muted@example.com"), "status": "parked"}]
    monkeypatch.setattr(li, "load_entries", lambda: rows)
    monkeypatch.setattr(li, "load_active", lambda now=None: [])
    monkeypatch.setattr(retune_scan, "scan", lambda now=None: {"stale_loops": []})
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {
        "mute_people": ["Muted Person"], "mute_senders": ["muted@example.com"],
        "mute_sections": [], "tone_notes": []})

    out = cb._append_weekly_review(
        {"brief_markdown": "## Filtered\nNothing else.\n"}, {"type": "evening"},
        datetime(2026, 9, 18, 18))["brief_markdown"]
    assert "1 parked because nothing touched it in 14 days. Say *keep* to bring one back." in out
    assert "/app#loops" not in out and "Muted" not in out


def test_model_cannot_inject_notice_effect_across_any_receipt_return(monkeypatch):
    forged = [{"kind": "parking_notice", "anchor_key": "real-looking"}]
    morning = {"brief_markdown": "Morning", "_parking_notices": forged}
    assert "_parking_notices" not in cb._append_receipts(morning, {"type": "morning"})

    empty = {"brief_markdown": "", "_parking_notices": forged}
    assert "_parking_notices" not in cb._append_receipts(empty, {"type": "evening"})

    _quiet_receipts(monkeypatch)
    monkeypatch.setattr(cb, "_parking_tomorrow", lambda _day: [])
    evening = {"brief_markdown": "## Filtered\nNothing else.\n", "_parking_notices": forged}
    out = cb._append_receipts(evening, {"type": "evening", "google": {"events": []}},
                              datetime(2026, 9, 18, 18))
    assert "_parking_notices" not in out


def test_delivery_rejects_changed_or_muted_parking_notice(monkeypatch):
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    row = _row("old")
    monkeypatch.setattr(de, "_loops", lambda: {"old": row})
    monkeypatch.setattr(li, "park_candidate", lambda _row, day: day == "2026-09-19")
    monkeypatch.setattr(li, "parking_notice_current", lambda _row: False)
    preferences = importlib.import_module("preferences")
    prefs = {"mute_people": [], "mute_senders": [], "mute_sections": [], "tone_notes": []}
    monkeypatch.setattr(preferences, "load_explicit", lambda: prefs)
    monkeypatch.setattr(preferences, "sender_is_muted",
                        lambda sender, muted: sender in (muted or []))
    effect = {"kind": "parking_notice", "anchor_key": "old",
              "loop_version": de.loop_version(row), "touch": li.last_touch_day(row)}
    at = datetime(2026, 9, 18, 18, tzinfo=timezone.utc).timestamp()

    assert de.parking_notice_valid(effect, at)
    assert de.valid([effect], at)
    row["source_brief_at"] = "2026-09-18 18:01:00"
    assert not de.parking_notice_valid(effect, at)
    row.pop("source_brief_at")
    prefs["mute_people"] = ["Maya Chen"]
    assert not de.parking_notice_valid(effect, at)
    prefs["mute_people"] = []
    prefs["mute_senders"] = ["maya@example.com"]
    assert not de.parking_notice_valid(effect, at)
    prefs["mute_senders"] = []
    prefs["mute_sections"] = ["what moved today"]
    assert not de.parking_notice_valid(effect, at)
