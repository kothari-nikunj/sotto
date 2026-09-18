"""Retune timing keeps lifecycle clocks separate from obligation chronology."""
import importlib.util
from datetime import datetime
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cr = _load("cr_retune_chronology", "morning-brief/scripts/continuity_resolve.py")
ap = _load("ap_retune_chronology", "_shared/scripts/retune_apply.py")


def _seed(tmp_path, *, status="open"):
    directory = tmp_path / "knowledge" / "continuity"
    directory.mkdir(parents=True, exist_ok=True)
    row = {
        "anchor_key": "maya-deck",
        "status": status,
        "action_type": "reply",
        "channel": "imessage",
        "contact_name": "Maya",
        "contact_identifier": "+14155551234",
        "summary": "Send the deck",
        "created_at": "2026-09-01 09:00:00",
    }
    if status == "parked":
        row["parked_at"] = "2026-09-15"
    path = directory / "maya-deck.md"
    path.write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    return path


def _frontmatter(path):
    return cr.ledger_io.parse_frontmatter(path.read_text())


@pytest.mark.parametrize("verb", ["keep", "snooze"])
def test_retune_preserves_created_at_and_earlier_completion_evidence(tmp_path, monkeypatch, verb):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setattr(ap, "_today", lambda: "2026-09-18")
    path = _seed(tmp_path, status="parked" if verb == "keep" else "open")

    result = ap.apply(verb, "maya-deck", 30)
    row = _frontmatter(path)
    assert result["ok"] is True
    assert row["created_at"] == "2026-09-01 09:00:00"
    if verb == "keep":
        assert row["reopened_at"] == "2026-09-18"
    else:
        assert row["snoozed_until"] == "2026-10-18"

    message = {"rowid": 7, "handle": "+14155551234", "is_from_me": True,
               "timestamp": "2026-09-10 12:00:00", "text": "Sent the deck."}
    update = {"evidence": [{"sourceType": "imessage", "sourceId": "7",
                            "snippet": "Sent the deck."}]}
    assert cr._completion_evidence(row, update, {"imessage": [message]}) is not None


def test_thirty_day_snooze_starts_park_clock_on_release(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setattr(ap, "_today", lambda: "2026-09-18")
    path = _seed(tmp_path, status="parked")

    assert ap.apply("snooze", "maya-deck", 30)["ok"] is True
    deferred = _frontmatter(path)
    assert deferred["status"] == "open"
    assert deferred["created_at"] == "2026-09-01 09:00:00"
    assert deferred["snoozed_until"] == "2026-10-18"
    assert cr.ledger_io.days_untouched(deferred, "2026-10-18") == 0

    out = cr.resolve({"today": "2026-10-18", "new_actions": [], "local": {}},
                     datetime(2026, 10, 18, 9))
    assert [row["anchor_key"] for row in out["active"]] == ["maya-deck"]
    assert out["parked"] == []
