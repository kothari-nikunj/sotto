"""Friday's review lists open tasks that look done; only the owner closes them.

A task you owe looks done when its "maybe done" note is 3 days old or more with no later message
from them (`replied_since`), or when it carries a `dates_passed` note. The scan reads
`events/loop-checks.json` only; it writes nothing, calls no model and never closes a task. Every
person and address here is fictional.
"""
import importlib.util
import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = _load("retune_scan_looks_done", "_shared/scripts/retune_scan.py")
cb = _load("cb_looks_done", "_shared/scripts/compose_brief.py")
import ledger_io  # noqa: E402
import loop_checks  # noqa: E402
from delivery_effects import loop_version  # noqa: E402

NOW = datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc)   # a Friday evening


def _task(tmp_path, monkeypatch, key, note=None, **fields):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    folder = tmp_path / "knowledge" / "continuity"
    folder.mkdir(parents=True, exist_ok=True)
    row = {"anchor_key": key, "status": "open", "action_type": "reply", "channel": "gmail",
           "contact_name": f"Avery {key.title()}", "contact_identifier": f"{key}@example.net",
           "created_at": "2026-11-11 09:00:00", "summary": f"Avery {key} asked for the deck", **fields}
    (folder / f"{key}.md").write_text("---\n" + yaml.safe_dump(row, sort_keys=False) + "---\n")
    if note:
        saved = next(r for r in ledger_io.load_entries() if r["anchor_key"] == key)
        loop_checks.add(loop_version(saved), note)


def _looks_done():
    return [row["anchor_key"] for row in scan.scan(now=NOW)["looks_done"]]


def _status(key):
    return next(r for r in ledger_io.load_entries() if r["anchor_key"] == key)["status"]


def test_a_four_day_old_you_messaged_note_looks_done_and_stays_open(tmp_path, monkeypatch):
    _task(tmp_path, monkeypatch, "old", {"basis": "you_messaged", "at": "2026-11-09T10:00:00+00:00", "source": "gmail"},
          created_at="2026-11-08 09:00:00")
    out = scan.scan(now=NOW)
    assert [row["anchor_key"] for row in out["looks_done"]] == ["old"]
    assert out["looks_done"][0]["note"] == "maybe done: you messaged them on 2026-11-09"
    assert out["counts"]["looks_done"] == 1
    assert _status("old") == "open"


def test_a_one_day_old_note_does_not_look_done(tmp_path, monkeypatch):
    _task(tmp_path, monkeypatch, "new", {"basis": "you_messaged", "at": "2026-11-12T10:00:00+00:00", "source": "gmail"})
    assert _looks_done() == []


def test_a_reply_from_them_since_the_note_does_not_look_done(tmp_path, monkeypatch):
    _task(tmp_path, monkeypatch, "kai", {"basis": "you_messaged", "at": "2026-11-09T10:00:00+00:00", "source": "gmail",
                                         "replied_since": "2026-11-09T12:00:00+00:00"},
          created_at="2026-11-08 09:00:00", action_type="propose_times")
    assert _looks_done() == []


def test_a_dates_passed_note_looks_done_at_any_age(tmp_path, monkeypatch):
    _task(tmp_path, monkeypatch, "piya", {"basis": "dates_passed", "at": "2026-11-12", "source": "calendar"},
          action_type="propose_times", deadline="2026-11-12")
    assert _looks_done() == ["piya"]


def test_waiting_on_rows_a_reopened_note_and_no_note_never_look_done(tmp_path, monkeypatch):
    old = "2026-11-01T10:00:00+00:00"
    _task(tmp_path, monkeypatch, "wait", {"basis": "you_messaged", "at": old, "source": "gmail"}, action_type="waiting_on")
    _task(tmp_path, monkeypatch, "undo", {"basis": "meeting_cancelled", "at": old, "source": "calendar"})
    _task(tmp_path, monkeypatch, "none")
    assert _looks_done() == []


def test_fridays_review_lists_the_looks_done_task_for_the_owner_to_confirm(tmp_path, monkeypatch):
    _task(tmp_path, monkeypatch, "old", {"basis": "you_messaged", "at": "2026-11-09T10:00:00+00:00", "source": "gmail"},
          created_at="2026-11-08 09:00:00")
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {})
    out = cb._append_weekly_review({"brief_markdown": "## Filtered\nNothing else.\n"}, {"type": "evening"}, NOW)
    review = out["brief_markdown"].split("## Weekly review\n", 1)[1]
    assert "These look done. Ask for a fresh numbered task list to select any you want to close:" in review
    line = next(line for line in review.splitlines() if "<!--loop:old-->" in line)
    assert "Avery Old" in line and "maybe done: you messaged them on 2026-11-09" in line
    assert out["_rendered_loop_lines"]["old"] == line
    assert "tell me if it's done" not in line
    assert _status("old") == "open"


def test_fridays_review_lists_a_looks_done_task_once_even_when_it_is_also_stale(tmp_path, monkeypatch):
    _task(tmp_path, monkeypatch, "old", {"basis": "you_messaged", "at": "2026-11-01T10:00:00+00:00", "source": "gmail"},
          created_at="2026-10-20 09:00:00")
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {})
    out = cb._append_weekly_review({"brief_markdown": "## Filtered\nNothing else.\n"}, {"type": "evening"}, NOW)
    assert out["brief_markdown"].count("<!--loop:old-->") == 1
    assert "look done" in out["brief_markdown"]


def test_delivered_looks_done_task_has_one_line_and_no_repeated_offer(tmp_path, monkeypatch):
    _task(tmp_path, monkeypatch, "old", {"basis": "you_messaged", "at": "2026-11-09T10:00:00Z", "source": "gmail"},
          created_at="2026-11-08 09:00:00",
          delivery_surface={"schema": 1, "delivery_keys": ["morning", "evening", "today"]})
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {})
    out = cb._append_weekly_review({"brief_markdown": "## Filtered\nNothing else.\n"}, {"type": "evening"}, NOW)
    review = out["brief_markdown"]
    assert review.count("<!--loop:old-->") == 1
    assert "Keep it, snooze it, or let it go?" not in review
    assert "more to review" not in review


@pytest.mark.parametrize(("offset", "qualifies"), [(0, True), (1, False)])
def test_friday_uses_actual_elapsed_72_hours(tmp_path, monkeypatch, offset, qualifies):
    _task(tmp_path, monkeypatch, "old",
          {"basis": "you_messaged", "at": (NOW - timedelta(days=3, seconds=-offset)).isoformat(), "source": "gmail"},
          created_at="2026-11-09 09:00:00")
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {})
    out = cb._append_weekly_review({"brief_markdown": "## Filtered\nNothing else.\n"}, {"type": "evening"}, NOW)
    assert ("These look done." in out["brief_markdown"]) is qualifies


@pytest.mark.parametrize("fourth_overdue", [False, True])
def test_friday_counts_fourth_looks_done_task_once(tmp_path, monkeypatch, fourth_overdue):
    shown_cap = 3
    for n in range(shown_cap + 1):
        _task(tmp_path, monkeypatch, str(n),
              {"basis": "you_messaged", "at": "2026-11-10T10:00:00Z", "source": "gmail"},
              created_at="2026-11-10 09:00:00", deadline="2026-11-12" if n == shown_cap and fourth_overdue else None)
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {})
    review = cb._append_weekly_review({"brief_markdown": "## Filtered\nNothing else.\n"},
                                     {"type": "evening"}, NOW)["brief_markdown"]
    assert review.count("<!--loop:") == shown_cap
    assert "- 1 more to review." in review


def test_friday_uses_real_instant_with_legacy_timezone_prefix(tmp_path, monkeypatch):
    _task(tmp_path, monkeypatch, "old",
          {"basis": "you_messaged", "at": (NOW - timedelta(days=3) + timedelta(seconds=1)).isoformat(),
           "source": "gmail"}, created_at="2026-11-09 09:00:00")
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {})
    inputs = {"type": "evening", "google": {"userTimezone": "+05:30 (legacy)"}}
    review = cb._append_weekly_review({"brief_markdown": "## Filtered\nNothing else.\n"}, inputs, NOW)
    assert "These look done." not in review["brief_markdown"]
