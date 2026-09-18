"""Park, don't delete, don't keep forever (Sep 2026).

One sentence: something the user OWES that nothing has touched for PARK_AFTER_DAYS parks — kept on
disk with its history, out of every view, revived by the next touch. These tests pin the rule at the
one writer (continuity_resolve), the one loader (ledger_io.load_active), the user's verb (`keep`)
and the two surfaces that speak about it (the evening receipts, Friday's review)."""
import importlib.util
import os
from datetime import datetime, timezone

import yaml

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


cr = _load("cr_parked", "morning-brief/scripts/continuity_resolve.py")
li = _load("li_parked", "_shared/scripts/ledger_io.py")
ap = _load("ap_parked", "_shared/scripts/retune_apply.py")
cb = _load("cb_parked", "_shared/scripts/compose_brief.py")

TODAY = "2026-09-18"                                  # a Friday
NOW = datetime(2026, 9, 18, 9, 0, 0)


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")


def _loop(tmp_path, key, **fm):
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    fm.setdefault("anchor_key", key)
    fm.setdefault("status", "open")
    fm.setdefault("action_type", "reply")
    fm.setdefault("contact_name", "Maya Chen")
    fm.setdefault("contact_identifier", "+14155551234")
    fm.setdefault("summary", "the deck")
    (d / f"{key}.md").write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n")


def _fm(tmp_path, key):
    return yaml.safe_load((tmp_path / "knowledge" / "continuity" / f"{key}.md").read_text().split("---")[1])


def _resolve(today=TODAY, **payload):
    return cr.resolve({"today": today, "new_actions": [], "local": {}, **payload},
                      datetime.strptime(today, "%Y-%m-%d").replace(hour=9))


def _capture(text="Send the deck over"):
    return {"type": "reply", "channel": "imessage", "contactName": "Maya Chen",
            "contactIdentifier": "+14155551234", "contextSummary": text}


def _accept_notice(tmp_path, key, day="2026-09-17"):
    from delivery_effects import finalize, loop_version
    row = _fm(tmp_path, key)
    effect = {"kind": "parking_notice", "anchor_key": row["anchor_key"],
              "loop_version": loop_version(row), "touch": li.last_touch_day(row)}
    assert finalize([effect], {"accepted_at": day + "T18:00:00+00:00"})


# ── the clock ────────────────────────────────────────────────────────────────────────────────────

def test_the_park_clock_reads_the_latest_touch():
    assert li.days_untouched({"created_at": "2026-09-01"}, TODAY) == 17
    # a brief re-capture and a revival are touches; the clock reads the newest of the three
    assert li.days_untouched({"created_at": "2026-09-01", "source_brief_at": "2026-09-10 06:30:00"}, TODAY) == 8
    assert li.days_untouched({"created_at": "2026-09-01", "reopened_at": "2026-09-17"}, TODAY) == 1
    assert li.days_untouched({}, TODAY) is None
    assert li.days_untouched({"created_at": "soon"}, TODAY) is None


# ── the writer ───────────────────────────────────────────────────────────────────────────────────

def test_a_debt_you_owe_parks_after_fourteen_untouched_days_and_not_before(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "stale", created_at="2026-09-04")                      # exactly 14 days
    _loop(tmp_path, "fresh", created_at="2026-09-05")                      # 13 days
    _accept_notice(tmp_path, "stale")
    out = _resolve()
    assert [it["anchor_key"] for it in out["parked"]] == ["stale"]
    assert [it["anchor_key"] for it in out["active"]] == ["fresh"]
    assert _fm(tmp_path, "stale")["status"] == "parked" and _fm(tmp_path, "stale")["parked_at"] == TODAY
    assert _fm(tmp_path, "fresh")["status"] == "open"
    # the file and its history are exactly where they were — parking deletes nothing
    assert _fm(tmp_path, "stale")["summary"] == "the deck"


def test_a_deadline_still_ahead_or_your_own_explicit_word_holds_off_the_park(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "due", created_at="2026-08-01", deadline="2026-09-30")     # a date is a reason to show it
    _loop(tmp_path, "today", created_at="2026-08-01", deadline=TODAY)
    _loop(tmp_path, "past", created_at="2026-08-01", deadline="2026-09-01")    # overdue AND untouched: parks
    _loop(tmp_path, "mine", created_at="2026-08-01", resolution_mode="explicit", source="followup_commitment")
    _accept_notice(tmp_path, "past")
    out = _resolve()
    assert sorted(it["anchor_key"] for it in out["active"]) == ["due", "mine", "today"]
    assert [it["anchor_key"] for it in out["parked"]] == ["past"]


def test_a_brief_re_capture_is_a_touch_that_holds_off_the_park(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "carried", created_at="2026-08-01", source_brief_at="2026-09-10 06:30:00")
    assert _resolve()["parked"] == []
    assert _fm(tmp_path, "carried")["status"] == "open"


def test_what_you_are_owed_never_parks_it_is_chased_instead(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "theirs", action_type="waiting_on", created_at="2026-06-01", chased_count=2)
    out = _resolve()
    assert out["parked"] == [] and [it["anchor_key"] for it in out["active"]] == ["theirs"]


def test_a_parked_loop_is_out_of_every_view_and_never_pruned(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "p", status="parked", parked_at="2026-08-01", created_at="2026-07-01")
    now = datetime(2026, 9, 18, 9, tzinfo=timezone.utc)
    assert li.load_active(now=now) == []                     # brief, count line, sotto-loops, chase clock
    out = _resolve()
    assert out["active"] == [] and [it["anchor_key"] for it in out["parked"]] == ["p"]
    assert (tmp_path / "knowledge" / "continuity" / "p.md").exists()   # 79 days old: retention is for TERMINAL rows
    assert _fm(tmp_path, "p")["status"] == "parked"


def test_a_re_captured_parked_loop_comes_back_without_new_request_evidence(tmp_path, monkeypatch):
    """Parking was Sotto's silence, not the user's closure — so unlike a dismissed row, a parked one
    reopens on the next capture alone, and `reopened_at` restarts its clock."""
    _env(tmp_path, monkeypatch)
    key = cr.compute_anchor_key(cr._normalize_action(_capture()))
    _loop(tmp_path, "q", anchor_key=key, status="parked", parked_at="2026-09-01",
          created_at="2026-08-01", times_surfaced=3)
    out = _resolve(new_actions=[_capture()])
    assert [it["anchor_key"] for it in out["active"]] == [key] and out["parked"] == []
    fm = _fm(tmp_path, "q")
    assert fm["status"] == "open" and "parked_at" not in fm and fm["reopened_at"] == TODAY
    assert fm["times_surfaced"] == 3 and fm["created_at"] == "2026-08-01"   # legacy count stays inert


# ── the user's verb ──────────────────────────────────────────────────────────────────────────────

def test_keep_un_parks_and_restarts_the_clock(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    today = ap._today()
    _loop(tmp_path, "p", status="parked", parked_at="2026-09-01", created_at="2026-08-01")
    out = ap.apply("keep", "p")
    assert out == {"ok": True, "action": "keep", "anchor_key": "p", "detail": "kept (un-parked, clock reset)"}
    fm = _fm(tmp_path, "p")
    assert fm["status"] == "open" and "parked_at" not in fm
    assert fm["created_at"] == "2026-08-01" and fm["reopened_at"] == today


def test_snooze_un_parks_too_so_it_surfaces_when_the_snooze_lifts(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "p", status="parked", parked_at="2026-09-01", created_at="2026-08-01")
    assert ap.apply("snooze", "p", 3)["ok"] is True
    fm = _fm(tmp_path, "p")
    assert fm["status"] == "open" and fm.get("snoozed_until")


# ── the two surfaces ─────────────────────────────────────────────────────────────────────────────

def test_the_evening_names_what_parks_tomorrow_once_with_the_word_that_keeps_it(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    _loop(tmp_path, "d13", created_at="2026-09-05", contact_name="Maya Chen", summary="the deck")
    _loop(tmp_path, "d12", created_at="2026-09-06", contact_name="Ron", summary="the invoice")
    _loop(tmp_path, "w13", action_type="waiting_on", created_at="2026-09-05", contact_name="Acme")
    _loop(tmp_path, "due13", created_at="2026-09-05", contact_name="Dana", deadline="2026-09-25")
    _loop(tmp_path, "mine13", created_at="2026-09-05", contact_name="Me", resolution_mode="explicit")
    _loop(tmp_path, "already", status="parked", parked_at="2026-09-10", created_at="2026-08-01")
    lines = cb._receipt_lines({"type": "evening", "google": {"events": []}}, TODAY, "+00:00")
    assert lines == ["Parks tomorrow unless you say *keep*: Maya Chen: the deck."]
    # Every row the delivery receipt can acknowledge must be named in the warning.
    for i in range(3):
        _loop(tmp_path, f"more{i}", created_at="2026-09-05", contact_name=f"Person {i}", summary=f"thing {i}")
    lines = cb._receipt_lines({"type": "evening", "google": {"events": []}}, TODAY, "+00:00")
    assert len(lines) == 1 and all(f"Person {i}: thing {i}" in lines[0] for i in range(3))
    assert "Maya Chen: the deck" in lines[0] and "/app#loops" not in lines[0]


def test_fridays_review_counts_the_parked_so_nothing_kept_is_invisible(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    monkeypatch.setattr(cb, "explicit_prefs", lambda: {})
    _loop(tmp_path, "p1", status="parked", parked_at="2026-09-10", created_at="2026-08-01")
    _loop(tmp_path, "p2", status="parked", parked_at="2026-09-11", created_at="2026-08-02")
    original = {"brief_markdown": "## Filtered\nNothing else.\n"}
    out = cb._append_weekly_review(dict(original), {"type": "evening"}, NOW)["brief_markdown"]
    assert "## Weekly review\n" in out
    assert "- 2 parked because nothing touched them in 14 days. Say *keep* to bring one back.\n" in out
    # …and the parked count never puts a parked row in the named stale rows
    assert "Maya Chen" not in out
    # not a Friday → no review, parked or not
    assert cb._append_weekly_review(dict(original), {"type": "evening"}, datetime(2026, 9, 17, 18)) == original
