"""proactive_scan.py — what is DUE (the lead window, birthdays, the chase), the once-per-day dedup,
and the wiring that hands a tick to the funnel.

`scan()` is pure: it decides what is due and nothing else. Every GATE — the snooze, quiet hours, the
mutes, the in-meeting hold, the daily interrupt budget — belongs to `triage_event.triage()` now, and
is asserted there (tests/test_event_triage.py::…proactive…). What is asserted HERE is the seam: the
tick goes in as one bundle, what comes back is delivered, and the dedup state records it once."""
import importlib.util, os, sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
spec = importlib.util.spec_from_file_location("ps", os.path.join(ROOT, "proactive", "scripts", "proactive_scan.py"))
ps = importlib.util.module_from_spec(spec); spec.loader.exec_module(ps)


def _at(hour):
    return datetime.now(timezone.utc).replace(hour=hour, minute=0, second=0, microsecond=0)


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
    now = ps._now_local("+00:00")
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


def test_retune_cooldown_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert ps.RETUNE_OFFER_COOLDOWN_DAYS == 7          # the window this test walks
    assert ps._retune_cooldown_ok("2026-06-25") is True          # never offered → allowed
    ps._stamp_retune_offer("2026-06-25")
    assert ps._retune_cooldown_ok("2026-06-28") is False         # 3 days later → still cooling down
    assert ps._retune_cooldown_ok("2026-07-03") is True          # 8 days later → allowed again


def _touch_brief_marker(tmp_path, date, kind, mtime=None):
    briefs = tmp_path / "briefs"
    briefs.mkdir(parents=True, exist_ok=True)
    p = briefs / f"{date}.{kind}.delivered"
    p.write_text("")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_recent_brief_marker_detection(tmp_path, monkeypatch):
    """Sprint 0 §6: brief_marker.py claim files under $SOTTO_DATA/briefs mark delivery — a fresh
    mtime within 2h means the brief just covered the loop pile."""
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    now = datetime.now(timezone.utc)
    assert ps._recent_brief_delivered(now) is False              # no markers at all
    p = _touch_brief_marker(tmp_path, now.strftime("%Y-%m-%d"), "morning")
    assert ps._recent_brief_delivered(now) is True               # fresh marker → suppress window
    old = (now - timedelta(hours=3)).timestamp()
    os.utime(p, (old, old))
    assert ps._recent_brief_delivered(now) is False              # 3h old → window over


def test_yesterdays_late_evening_brief_still_counts(tmp_path, monkeypatch):
    # A brief delivered at 23:30 carries yesterday's date but is still "within 2h" shortly after
    # midnight — the marker filename's date must not hide it.
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    _touch_brief_marker(tmp_path, yesterday, "evening", mtime=(now - timedelta(hours=1)).timestamp())
    assert ps._recent_brief_delivered(now) is True


def test_main_suppresses_retune_offer_near_fresh_brief(tmp_path, monkeypatch, capsys):
    """End-to-end wiring: a heavy stale pile that would fire retune_offer stays silent while a
    brief marker is fresh, then fires once the 2h window has passed."""
    import json
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_QUIET_START", "0")                 # start == end → quiet never
    monkeypatch.setenv("SOTTO_QUIET_END", "0")
    monkeypatch.setattr(ps, "_stale_loop_count", lambda: 10)     # pile well over the threshold
    monkeypatch.setattr(sys, "argv", ["proactive_scan.py"])
    now = ps._now_local("+00:00")
    marker = _touch_brief_marker(tmp_path, now.strftime("%Y-%m-%d"), "evening")
    ps.main()
    out = json.loads(capsys.readouterr().out)
    assert not [n for n in out["nudges"] if n["kind"] == "retune_offer"]   # brief just went out
    old = (now - timedelta(hours=3)).timestamp()
    os.utime(marker, (old, old))                                 # brief was 3h ago
    ps.main()
    out = json.loads(capsys.readouterr().out)
    assert [n for n in out["nudges"] if n["kind"] == "retune_offer"]      # now the offer fires


# ── ONE rulebook: the proactive lane spends the funnel's budget and writes its verdicts ──────────

def _quiet_never(monkeypatch, tmp_path):
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_QUIET_START", "0")
    monkeypatch.setenv("SOTTO_QUIET_END", "0")
    monkeypatch.setattr(ps, "_stale_loop_count", lambda: 0)


def _run_main(monkeypatch, capsys, *argv):
    import json
    monkeypatch.setattr(sys, "argv", ["proactive_scan.py", *argv])
    ps.main()
    return json.loads(capsys.readouterr().out)


def _surfaced_rows(tmp_path):
    import json
    p = tmp_path / "events" / "surfaced.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_a_proactive_push_spends_one_interrupt_however_many_nudges_it_carries(tmp_path, monkeypatch,
                                                                             capsys):
    """The tick's nudges go out as ONE message (the skill's own rule), so they cost ONE unit
    between them — charging per kind let a single 7am push spend the whole day and starve the event
    lane until midnight."""
    import json
    _quiet_never(monkeypatch, tmp_path)
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "1")
    now = ps._now_local("+00:00")
    today = now.strftime("%Y-%m-%d")
    cont = tmp_path / "cont.json"
    cont.write_text(json.dumps([{"id": "c1", "title": "Send the LOI", "deadline": today},
                                {"id": "c2", "title": "Call the bank", "deadline": today}]))
    out = _run_main(monkeypatch, capsys, "--continuity", str(cont))
    assert len(out["nudges"]) == 2 and out["held"] == []
    budget = json.loads((tmp_path / "events" / "budget.json").read_text())
    assert budget == {"date": today, "count": 1}          # one push, one interrupt
    rows = _surfaced_rows(tmp_path)
    assert {r["verdict"] for r in rows} == {"agent"}
    assert all(r["channel"] == "proactive" and r["class"] == "commitment" for r in rows)


def test_a_push_beyond_the_budget_queues_whole_for_the_digest(tmp_path, monkeypatch, capsys):
    """Beyond the cap the push doesn't fire at all: every nudge in it demotes to the SAME digest
    queue a budget-held event lands in, with its kind preserved as held_class."""
    import json
    _quiet_never(monkeypatch, tmp_path)
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "0")
    now = ps._now_local("+00:00")
    cont = tmp_path / "cont.json"
    cont.write_text(json.dumps([{"id": "c1", "title": "Send the LOI",
                                 "deadline": now.strftime("%Y-%m-%d")}]))
    out = _run_main(monkeypatch, capsys, "--continuity", str(cont))
    assert out["nudges"] == [] and [n["kind"] for n in out["held"]] == ["commitment"]
    queued = [json.loads(l) for l in (tmp_path / "events" / "queue.jsonl").read_text().splitlines()]
    assert queued[0]["verdict_class"] == "budget" and queued[0]["held_class"] == "commitment"
    rows = _surfaced_rows(tmp_path)
    assert rows[0]["verdict"] == "queue"
    assert "daily interrupt budget spent" in rows[0]["reason"]


def test_proactive_verdicts_are_recorded_even_on_a_quiet_budget(tmp_path, monkeypatch, capsys):
    """Nothing is silently discarded: a fired nudge writes an `agent` row with its reason."""
    import json
    _quiet_never(monkeypatch, tmp_path)
    now = ps._now_local("+00:00")
    cont = tmp_path / "cont.json"
    cont.write_text(json.dumps([{"id": "c1", "title": "Send the LOI",
                                 "deadline": now.strftime("%Y-%m-%d")}]))
    out = _run_main(monkeypatch, capsys, "--continuity", str(cont))
    assert len(out["nudges"]) == 1 and out["held"] == []
    row = _surfaced_rows(tmp_path)[0]
    assert row["verdict"] == "agent" and "Send the LOI" in row["reason"]


def test_meeting_prep_skipped_when_todays_research_covers_the_attendees(tmp_path, monkeypatch):
    """The docstring's 'that you haven't prepped', made real: today's research cache means a prep
    or brief run already covered this meeting's people."""
    import json
    now = _at(10)
    soon = (now + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S%z")
    cal = [{"id": "ev1", "summary": "Pitch", "start": soon,
            "attendees": [{"email": "me@x.com", "self": True}, {"email": "VC@fund.com"}]}]
    assert ps.scan(cal, [], {}, "me@x.com", now)["nudges"]                       # not prepped → fires
    prepped = {"vc@fund.com"}                                                    # case-insensitive
    assert not ps.scan(cal, [], {}, "me@x.com", now, prepped_emails=prepped)["nudges"]
    # someone ELSE's research doesn't count as having prepped this meeting
    assert ps.scan(cal, [], {}, "me@x.com", now, prepped_emails={"other@else.com"})["nudges"]


def test_research_cache_reader(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    assert ps._research_cache_emails("2026-08-08") == set()                      # no cache yet
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "research_2026-08-08.json").write_text(json.dumps(
        {"attendees": [{"email": "VC@Fund.com"}, {"title": "no email"}]}))
    assert ps._research_cache_emails("2026-08-08") == {"vc@fund.com"}


def test_the_day_of_birthday_is_dedupd_by_a_delivered_brief_not_delayed(tmp_path, monkeypatch,
                                                                        capsys):
    """The brief that delivered today carried the 🎂 Coming Up line AND the quick-wish tap with the
    same link — so the day-of nudge is the same nudge twice, not a late one. The 2h window only
    delayed it into the same morning."""
    import json
    _quiet_never(monkeypatch, tmp_path)
    now = ps._now_local("+00:00")
    local_path = tmp_path / "local.json"
    local_path.write_text(json.dumps({"contacts": [{"name": "Jordan",
                                                    "birthday": now.strftime("%m-%d")}]}))
    marker = _touch_brief_marker(tmp_path, now.strftime("%Y-%m-%d"), "morning")
    assert _run_main(monkeypatch, capsys, "--local", str(local_path))["nudges"] == []
    old = (now - timedelta(hours=3)).timestamp()
    os.utime(marker, (old, old))                                 # 2h window over — still nothing
    assert _run_main(monkeypatch, capsys, "--local", str(local_path))["nudges"] == []


def test_the_lead_birthday_keeps_the_two_hour_delay(tmp_path, monkeypatch, capsys):
    """A gift nudge days out is in no brief, so a delivered brief only delays it — the same 2h
    window the tidy-up offer waits out."""
    import json
    _quiet_never(monkeypatch, tmp_path)
    monkeypatch.setenv("SOTTO_BIRTHDAY_LEAD_DAYS", "3")
    now = ps._now_local("+00:00")
    local_path = tmp_path / "local.json"
    local_path.write_text(json.dumps({"contacts": [
        {"name": "Jordan", "birthday": (now + timedelta(days=3)).strftime("%m-%d")}]}))
    marker = _touch_brief_marker(tmp_path, now.strftime("%Y-%m-%d"), "morning")
    assert _run_main(monkeypatch, capsys, "--local", str(local_path))["nudges"] == []
    old = (now - timedelta(hours=3)).timestamp()
    os.utime(marker, (old, old))
    assert [n["lead_days"] for n in
            _run_main(monkeypatch, capsys, "--local", str(local_path))["nudges"]] == [3]


def test_open_loops_read_straight_from_the_ledger_one_direction_only(tmp_path, monkeypatch):
    """No /tmp/sotto_cont.json hand-reshape: the deadline-bearing loops come from loops_query, in
    the shape scan() reads (undated loops — which can never be 'due today' — are dropped). Only the
    ones YOU owe: a loop you're waiting on belongs to the chase lane, and walking both directions
    produced two contradictory nudges about one loop."""
    import loops_query as lq
    monkeypatch.setattr(lq, "query", lambda: {
        "you_owe": [{"name": "Sarah", "what": "send the deck", "deadline": "2026-08-08",
                     "channel": "email", "identifier": "sarah@x.com"},
                    {"name": "NoDeadline", "what": "someday", "deadline": None}],
        "waiting_on_them": [{"name": "Ben", "what": "the contract", "deadline": "2026-08-01",
                             "channel": "imessage", "identifier": "+15551112222"}]})
    loops = ps._open_loops()
    assert [l["title"] for l in loops] == ["Sarah — send the deck"]
    assert loops[0]["identifier"] == "sarah@x.com" and loops[0]["name"] == "Sarah"


# ── Cadence: the same nudge snooze the event funnel honors ────────────────────────────────────────

def test_main_reads_the_snooze_from_preferences_and_burns_no_dedup_state(tmp_path, monkeypatch, capsys):
    """End-to-end wiring: the funnel reads explicit.nudge_snooze_until (what the sotto-feedback
    verbs write) off the same preferences.json — and because the run reports `quiet`, nothing is
    marked as nudged, so a birthday held during the snooze can still surface after it lifts."""
    import json
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_QUIET_START", "0")                 # start == end → quiet never
    monkeypatch.setenv("SOTTO_QUIET_END", "0")
    monkeypatch.setattr(ps, "_stale_loop_count", lambda: 0)
    now = ps._now_local("+00:00")
    date = now.strftime("%Y-%m-%d")
    local_path = tmp_path / "local.json"
    local_path.write_text(json.dumps({"contacts": [{"name": "Jordan",
                                                    "birthday": now.strftime("%m-%d")}]}))
    monkeypatch.setattr(sys, "argv", ["proactive_scan.py", "--local", str(local_path)])
    (tmp_path / "preferences.json").write_text(json.dumps(
        {"explicit": {"nudge_snooze_until": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")}}))
    ps.main()
    out = json.loads(capsys.readouterr().out)
    assert out["nudges"] == [] and out["quiet"] is True and "snoozed until" in out["reason"]
    # The nudge itself is NOT burned (it fires when the snooze lifts) — only a record-once marker.
    assert ps._load_state(date) == {f"held:bday:jordan:{now.year}"}
    # Clear it ("back to normal") and the same run nudges.
    (tmp_path / "preferences.json").write_text(json.dumps({"explicit": {"nudge_snooze_until": ""}}))
    ps.main()
    out = json.loads(capsys.readouterr().out)
    assert [n["kind"] for n in out["nudges"]] == ["birthday"]


# ── The rest of the funnel's gates: mute, the room you're in, and the Record ──────────────────────

def _due_commitment(tmp_path, now, name="Sarah Chen", ident="sarah@acme.com"):
    import json
    cont = tmp_path / "cont.json"
    cont.write_text(json.dumps([{"id": "c1", "title": f"{name} — send the deck", "name": name,
                                 "identifier": ident, "channel": "email",
                                 "deadline": now.strftime("%Y-%m-%d")}]))
    return str(cont)


def test_a_muted_person_gets_no_proactive_nudge(tmp_path, monkeypatch, capsys):
    """"Stop surfacing X" has to stick for the nudges Sotto raises itself or it doesn't mean
    anything — the same two lists Tier 0 drops a message on, in the same funnel."""
    import json
    _quiet_never(monkeypatch, tmp_path)
    now = ps._now_local("+00:00")
    cont = _due_commitment(tmp_path, now)
    (tmp_path / "preferences.json").write_text(json.dumps(
        {"explicit": {"mute_people": ["Sarah Chen"]}}))
    out = _run_main(monkeypatch, capsys, "--continuity", cont)
    assert out["nudges"] == []          # nothing to deliver; a dropped nudge is not a held one…
    row = _surfaced_rows(tmp_path)[0]   # …but The Record still says what happened, in plain words
    assert row["verdict"] == "drop" and row["class"] == "muted"
    assert row["reason"] == ("send the deck for Sarah Chen — due today — "
                             "you asked Sotto to stop surfacing Sarah Chen")
    # a muted sender address is the same rule (a second loop, so the dedup state isn't the reason)
    (tmp_path / "preferences.json").write_text(json.dumps(
        {"explicit": {"mute_senders": ["sarah@acme.com"]}}))
    cont2 = tmp_path / "cont2.json"
    cont2.write_text(json.dumps([{"id": "c2", "title": "Sarah Chen — sign the SOW",
                                  "identifier": "sarah@acme.com", "channel": "email",
                                  "deadline": now.strftime("%Y-%m-%d")}]))
    assert _run_main(monkeypatch, capsys, "--continuity", str(cont2))["nudges"] == []
    assert _surfaced_rows(tmp_path)[-1]["reason"].endswith(
        "you asked Sotto to stop surfacing sarah@acme.com")


def test_the_in_meeting_hold_applies_to_this_lane_too(tmp_path, monkeypatch, capsys):
    """HOW-SOTTO-DECIDES says the hold applies to would-be nudges, unqualified. It is the funnel's
    own read of the shared calendar cache — including its posture that a stale snapshot never
    holds — and a held nudge queues for the digest exactly like a budget-held one."""
    import json
    _quiet_never(monkeypatch, tmp_path)
    now = ps._now_local("+00:00")
    cont = _due_commitment(tmp_path, now)
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    ev = {"summary": "Roadmap sync", "attendees": 2, "all_day": False,
          "start": (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S"),
          "end": (now + timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%S")}
    (cache / "calendar_today.json").write_text(json.dumps(
        {"date": now.strftime("%Y-%m-%d"), "refresh_secs": 900,
         "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "events": [ev]}))
    out = _run_main(monkeypatch, capsys, "--continuity", cont)
    assert out["nudges"] == [] and [n["kind"] for n in out["held"]] == ["commitment"]
    queued = [json.loads(l) for l in (tmp_path / "events" / "queue.jsonl").read_text().splitlines()]
    assert queued[0]["verdict_class"] == "meeting_hold" and queued[0]["held_class"] == "commitment"
    assert "in a meeting until" in _surfaced_rows(tmp_path)[0]["reason"]
    assert not (tmp_path / "events" / "budget.json").exists()   # a held nudge spends nothing


def test_a_stale_calendar_never_holds_this_lane_either(tmp_path, monkeypatch, capsys):
    """Same posture as the funnel: never act on a stale belief about where you are."""
    import json
    _quiet_never(monkeypatch, tmp_path)
    now = ps._now_local("+00:00")
    cont = _due_commitment(tmp_path, now)
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "calendar_today.json").write_text(json.dumps(
        {"date": now.strftime("%Y-%m-%d"), "refresh_secs": 900,
         "generated_at": (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "events": [{"summary": "Roadmap sync", "attendees": 2, "all_day": False,
                     "start": (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S"),
                     "end": (now + timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%S")}]}))
    assert [n["kind"] for n in
            _run_main(monkeypatch, capsys, "--continuity", cont)["nudges"]] == ["commitment"]


def test_quiet_hours_record_what_they_suppressed_once_a_day(tmp_path, monkeypatch, capsys):
    """The blocker this flattening had to clear: the funnel records every event it is HANDED, and
    the cron ticks every 15 minutes, so a quiet night would write forty identical rows per nudge.
    The watcher submits each nudge once for the day under a `held:` marker instead — one row, one
    queue entry — and because the marker is not the nudge's own key, the nudge still fires when the
    hold lifts."""
    import json
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setattr(ps, "_stale_loop_count", lambda: 0)
    now = ps._now_local("+00:00")
    date = now.strftime("%Y-%m-%d")
    monkeypatch.setenv("SOTTO_QUIET_START", str(now.hour))       # quiet, right now
    monkeypatch.setenv("SOTTO_QUIET_END", str((now.hour + 1) % 24))
    cont = _due_commitment(tmp_path, now)
    out = _run_main(monkeypatch, capsys, "--continuity", cont)
    assert out["nudges"] == [] and out["quiet"] is True and "quiet hours" in out["reason"]
    rows = _surfaced_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["verdict"] == "queue" and rows[0]["class"] == "quiet"
    assert rows[0]["reason"] == "send the deck for Sarah Chen — due today"
    queued = [json.loads(l) for l in (tmp_path / "events" / "queue.jsonl").read_text().splitlines()]
    assert len(queued) == 1 and queued[0]["held_class"] == "commitment"   # the kind isn't forgotten
    for _ in range(3):                                           # the next ticks, 15 min apart
        _run_main(monkeypatch, capsys, "--continuity", cont)
    assert len(_surfaced_rows(tmp_path)) == 1                    # …record nothing new
    assert len((tmp_path / "events" / "queue.jsonl").read_text().splitlines()) == 1
    assert ps._load_state(date) == {"held:loop:c1"}              # and the nudge itself is unburned
    # the hold lifts → the nudge fires on the normal cadence, not as a burst
    monkeypatch.setenv("SOTTO_QUIET_START", "0")
    monkeypatch.setenv("SOTTO_QUIET_END", "0")
    assert [n["kind"] for n in
            _run_main(monkeypatch, capsys, "--continuity", cont)["nudges"]] == ["commitment"]


# ── The chase: a nag is not a reply (Step 2.7 item 1c) ────────────────────────────────────────────

def _chase(name="Acme", what="the signed contract", **over):
    """One chase candidate in the shape _chase_candidates returns (the `name` matters: it is what
    lets the funnel compose "still no word from Acme on the signed contract")."""
    c = {"id": f"chase:{name}:{what}", "title": f"{name} — {what}", "name": name,
         "detail": "asked 4 days ago",
         "anchor_key": f"email:waiting_on:id:{name.lower()}",
         "channel": "email", "identifier": "ops@acme.com"}
    c.update(over)
    return c


def test_chase_fires_as_its_own_kind_at_most_once_a_day():
    now = _at(10)
    out = ps.scan([], [], {}, "me@x.com", now,
                  chase_candidates=[_chase(), _chase("Ben", "the deck")])
    chases = [n for n in out["nudges"] if n["kind"] == "chase"]
    assert len(chases) == 1                                   # queued, not fired in a burst
    assert chases[0]["title"] == "Acme — the signed contract"
    assert chases[0]["identifier"] == "ops@acme.com"


def test_one_loop_one_nudge_one_register(tmp_path, monkeypatch):
    """The production path, not an empty list: an overdue waiting-on that the ledger marked
    chase-pending produces the chase and NOTHING else. It used to produce a `commitment` nudge
    ("draft the reply") and a `chase` nudge ("a nag is not a reply") in the same tick, under
    different keys, for two budget units and one message."""
    import loops_query as lq
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    now = _at(10)
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(lq, "query", lambda: {"you_owe": [], "waiting_on_them": [
        {"name": "Acme", "what": "the contract", "channel": "email", "identifier": "ops@acme.com",
         "deadline": yesterday, "overdue": True, "age_days": 6, "chased_count": 0,
         "chase_pending": today, "anchor_key": "email:waiting_on:id:ops@acme.com"}]})
    out = ps.scan([], ps._open_loops(), {}, "me@x.com", now,
                  chase_candidates=ps._chase_candidates(today))
    assert [n["kind"] for n in out["nudges"]] == ["chase"]
    assert out["nudges"][0]["anchor_key"] == "email:waiting_on:id:ops@acme.com"


def test_a_chase_held_by_the_clock_is_never_counted(tmp_path, monkeypatch, capsys):
    """Quiet hours hold the chase like everything else — and because phase two runs on DELIVERY
    only, a chase the clock held is not one of the two this item gets."""
    finalized = []
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setattr(ps, "_stale_loop_count", lambda: 0)
    now = ps._now_local("+00:00")
    monkeypatch.setenv("SOTTO_QUIET_START", str(now.hour))
    monkeypatch.setenv("SOTTO_QUIET_END", str((now.hour + 1) % 24))
    monkeypatch.setattr(ps, "_chase_candidates", lambda date: [_chase()])
    monkeypatch.setattr(ps, "_finalize_chase", lambda key: finalized.append(key))
    out = _run_main(monkeypatch, capsys)
    assert out["nudges"] == [] and out["quiet"] is True and finalized == []
    assert _surfaced_rows(tmp_path)[0]["class"] == "quiet"


def test_chase_candidates_read_the_pending_stamp_not_the_count(monkeypatch):
    """Two phases, one writer: continuity_resolve stamps `chase_pending` when an item ripens, this
    lane delivers what is pending TODAY, and only a delivered chase is counted (--finalize-chase).
    Keying on the pending stamp is what stops a chase burning undelivered."""
    import loops_query as lq
    monkeypatch.setattr(lq, "query", lambda: {"you_owe": [], "waiting_on_them": [
        {"name": "Acme", "what": "the contract", "channel": "email", "identifier": "ops@acme.com",
         "age_days": 5, "overdue": False, "chased_count": 0, "chase_pending": "2026-08-09",
         "anchor_key": "email:waiting_on:id:ops@acme.com"},
        {"name": "Ben", "what": "the deck", "age_days": 9, "overdue": True,
         "chased_count": 1, "chase_pending": "2026-08-09"},
        {"name": "NotDue", "what": "someday", "age_days": 2, "chased_count": 0,
         "chase_pending": None, "last_chased_at": "2026-08-09"}]})
    got = ps._chase_candidates("2026-08-09")
    assert [c["title"] for c in got] == ["Acme — the contract", "Ben — the deck"]
    assert got[0]["detail"] == "asked 5 days ago"
    assert got[0]["anchor_key"] == "email:waiting_on:id:ops@acme.com"
    assert got[1]["detail"] == "overdue · chased once already"
    assert ps._chase_candidates("2026-08-10") == []            # yesterday's stamp is not today's


def test_a_delivered_chase_is_finalized_and_a_held_one_is_not(tmp_path, monkeypatch, capsys):
    """Phase two runs on DELIVERY only: the fired chase shells `--finalize-chase <anchor_key>`;
    a chase the budget held is queued, left pending, and counted against nothing."""
    finalized = []
    _quiet_never(monkeypatch, tmp_path)
    monkeypatch.setattr(ps, "_chase_candidates", lambda date: [_chase()])
    monkeypatch.setattr(ps, "_finalize_chase", lambda key: finalized.append(key))
    assert [n["kind"] for n in _run_main(monkeypatch, capsys)["nudges"]] == ["chase"]
    assert finalized == ["email:waiting_on:id:acme"]
    # …and with no budget left, nothing is finalized
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "0")
    monkeypatch.setattr(ps, "_chase_candidates", lambda date: [_chase("Ben", "the deck")])
    out = _run_main(monkeypatch, capsys)
    assert [n["kind"] for n in out["held"]] == ["chase"] and len(finalized) == 1


def test_chase_waits_out_a_freshly_delivered_brief(tmp_path, monkeypatch, capsys):
    """The chase stamp is written in the brief's OWN Learn step, so without this guard the chase
    fires minutes after the brief that just listed the item — the same 2h window its two siblings
    already respect, applied to the one case where the collision is guaranteed."""
    _quiet_never(monkeypatch, tmp_path)
    monkeypatch.setattr(ps, "_chase_candidates", lambda date: [_chase()])
    now = ps._now_local("+00:00")
    marker = _touch_brief_marker(tmp_path, now.strftime("%Y-%m-%d"), "morning")
    assert _run_main(monkeypatch, capsys)["nudges"] == []
    old = (now - timedelta(hours=3)).timestamp()
    os.utime(marker, (old, old))
    assert [n["kind"] for n in _run_main(monkeypatch, capsys)["nudges"]] == ["chase"]


# ── The hand-off: chased twice, no answer, so Sotto asks in plain words ───────────────────────────

def _handoff_ledger(monkeypatch, **over):
    import loops_query as lq
    row = {"name": "Maya", "what": "the contract", "channel": "imessage",
           "identifier": "+15551112222", "age_days": 12, "chased_count": 2, "chased_out": True}
    row.update(over)
    monkeypatch.setattr(lq, "query", lambda: {"you_owe": [], "waiting_on_them": [row]})


def test_a_chased_out_loop_asks_its_own_named_question(tmp_path, monkeypatch):
    """Not "your open-loops list is getting heavy" — a first-time user has no idea what that means.
    Person, thing, binary choice, and none of Sotto's own vocabulary."""
    _handoff_ledger(monkeypatch)
    now = _at(10)
    out = ps.scan([], [], {}, "me@x.com", now, retune_offer_allowed=True,
                  handoff_candidates=ps._handoff_candidates())
    n = [x for x in out["nudges"] if x["kind"] == "handoff"]
    assert len(n) == 1 and n[0]["person"] == "Maya"
    assert n[0]["detail"] == ("I've nudged Maya twice about the contract — "
                             "nudge them again, or let it go?")
    assert not [x for x in out["nudges"] if x["kind"] == "retune_offer"]


def test_the_named_question_ignores_the_pile_threshold_but_shares_its_cooldown(tmp_path, monkeypatch):
    """One unanswered ask deserves the question even on a tidy day — but it rides the same
    periodic cooldown, so it is never a daily nag."""
    _handoff_ledger(monkeypatch)
    now = _at(10)
    kinds = lambda **kw: [n["kind"] for n in ps.scan([], [], {}, "me@x.com", now, **kw)["nudges"]]
    assert kinds(stale_count=0, retune_offer_allowed=True,
                 handoff_candidates=ps._handoff_candidates()) == ["handoff"]
    assert kinds(stale_count=0, retune_offer_allowed=False,
                 handoff_candidates=ps._handoff_candidates()) == []
    # a heavy pile with nothing chased out still gets the generic tidy-up offer
    assert kinds(stale_count=9, retune_offer_allowed=True, handoff_candidates=[]) == ["retune_offer"]


def test_the_named_question_stamps_the_shared_cooldown(tmp_path, monkeypatch, capsys):
    _quiet_never(monkeypatch, tmp_path)
    _handoff_ledger(monkeypatch)
    date = ps._now_local("+00:00").strftime("%Y-%m-%d")
    assert [n["kind"] for n in _run_main(monkeypatch, capsys)["nudges"]] == ["handoff"]
    assert ps._retune_cooldown_ok(date) is False               # the window is now running


def test_chase_spends_the_shared_budget_and_queues_beyond_it(tmp_path, monkeypatch, capsys):
    """Same rulebook as every other nudge: it pays the daily interrupt budget or it queues."""
    _quiet_never(monkeypatch, tmp_path)
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "0")
    monkeypatch.setattr(ps, "_chase_candidates", lambda date: [_chase()])
    out = _run_main(monkeypatch, capsys)
    assert out["nudges"] == [] and [n["kind"] for n in out["held"]] == ["chase"]
    rows = _surfaced_rows(tmp_path)
    assert rows and rows[0]["verdict"] == "queue" and rows[0]["class"] == "budget"
    assert rows[0]["reason"].endswith("still no word from Acme on the signed contract "
                                      "(asked 4 days ago)")


def test_chase_is_not_repeated_by_the_next_cron_tick(tmp_path, monkeypatch, capsys):
    _quiet_never(monkeypatch, tmp_path)
    monkeypatch.setattr(ps, "_chase_candidates", lambda date: [_chase()])
    assert [n["kind"] for n in _run_main(monkeypatch, capsys)["nudges"]] == ["chase"]
    assert _run_main(monkeypatch, capsys)["nudges"] == []      # dedup state burned the key


# ── Birthday lead time (Step 2.7 item 5): a gift idea 3 days early ────────────────────────────────

def test_birthday_lead_nudge_fires_at_the_lead_window_and_again_day_of(monkeypatch):
    monkeypatch.setenv("SOTTO_BIRTHDAY_LEAD_DAYS", "3")
    now = _at(10)
    bday = (now + timedelta(days=3)).strftime("%m-%d")
    local = {"contacts": [{"name": "Jordan", "birthday": bday}]}
    lead = [n for n in ps.scan([], [], local, "me@x.com", now)["nudges"] if n["kind"] == "birthday"]
    assert len(lead) == 1 and lead[0]["lead_days"] == 3
    assert "in 3 days" in lead[0]["title"] and "gift" in lead[0]["detail"]
    # …and the day-of nudge still fires, under its own key (so neither dedups the other away)
    day_of_now = now + timedelta(days=3)
    day_of = [n for n in ps.scan([], [], local, "me@x.com", day_of_now)["nudges"]
              if n["kind"] == "birthday"]
    assert len(day_of) == 1 and day_of[0]["lead_days"] == 0 and "is today" in day_of[0]["title"]
    assert day_of[0]["key"] != lead[0]["key"]
    assert str(day_of_now.year) in day_of[0]["key"]            # the key carries the occurrence year


def test_birthday_lead_zero_keeps_the_old_day_of_only_behavior(monkeypatch):
    monkeypatch.setenv("SOTTO_BIRTHDAY_LEAD_DAYS", "0")
    now = _at(10)
    local = {"contacts": [{"name": "Jordan", "birthday": (now + timedelta(days=3)).strftime("%m-%d")}]}
    assert not [n for n in ps.scan([], [], local, "me@x.com", now)["nudges"] if n["kind"] == "birthday"]


def test_birthday_lead_fires_once_per_day_not_per_tick(tmp_path, monkeypatch, capsys):
    import json
    _quiet_never(monkeypatch, tmp_path)
    monkeypatch.setenv("SOTTO_BIRTHDAY_LEAD_DAYS", "3")
    now = ps._now_local("+00:00")
    local_path = tmp_path / "local.json"
    local_path.write_text(json.dumps({"contacts": [
        {"name": "Jordan", "birthday": (now + timedelta(days=3)).strftime("%m-%d")}]}))
    first = _run_main(monkeypatch, capsys, "--local", str(local_path))
    assert [n["kind"] for n in first["nudges"]] == ["birthday"]
    assert _run_main(monkeypatch, capsys, "--local", str(local_path))["nudges"] == []


# ── The two-phase chase, end to end across both halves ────────────────────────────────────────────

def test_a_chase_counts_only_once_it_has_actually_gone_out(tmp_path, monkeypatch, capsys):
    """The whole contract, on real files: continuity_resolve marks ONE waiting-on chase-pending, the
    watcher delivers it, and only then does the real `--finalize-chase` subprocess count it. A tick
    that never fires (here: no budget) leaves the item pending and the count at zero — which is the
    leak this replaces, where a chase the user never saw was one of the two they get."""
    import importlib.util
    import yaml
    monkeypatch.setenv("SOTTO_DATA", str(tmp_path))
    monkeypatch.setenv("SOTTO_TIMEZONE", "+00:00")
    monkeypatch.setenv("SOTTO_QUIET_START", "0")
    monkeypatch.setenv("SOTTO_QUIET_END", "0")
    monkeypatch.setattr(ps, "_stale_loop_count", lambda: 0)
    cr_spec = importlib.util.spec_from_file_location(
        "cr_e2e", os.path.join(ROOT, "morning-brief", "scripts", "continuity_resolve.py"))
    cr = importlib.util.module_from_spec(cr_spec); cr_spec.loader.exec_module(cr)

    now = ps._now_local("+00:00")
    today = now.strftime("%Y-%m-%d")
    d = tmp_path / "knowledge" / "continuity"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "wait_maya.md"
    path.write_text("---\n" + yaml.safe_dump(
        {"anchor_key": "imessage:waiting_on:id:5551112222", "status": "open",
         "action_type": "waiting_on", "contact_name": "Maya", "channel": "imessage",
         "contact_identifier": "+15551112222", "summary": "the signed contract",
         "created_at": (now - timedelta(days=9)).strftime("%Y-%m-%d")}, sort_keys=False) + "---\n")

    def _fm():
        return yaml.safe_load(path.read_text().split("---")[1])

    cr.resolve({"today": today, "local": {}}, now.replace(tzinfo=None))   # phase one
    assert _fm()["chase_pending"] == today and not _fm().get("chased_count")

    # A tick with no budget: queued for the digest, still pending, still uncounted.
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "0")
    assert [n["kind"] for n in _run_main(monkeypatch, capsys)["held"]] == ["chase"]
    assert _fm()["chase_pending"] == today and not _fm().get("chased_count")

    # Budget restored, dedup state cleared (a new tick's worth) → it fires, and NOW it counts.
    monkeypatch.setenv("SOTTO_NUDGE_BUDGET", "4")
    ps._save_state(today, set())
    out = _run_main(monkeypatch, capsys)
    assert [n["kind"] for n in out["nudges"]] == ["chase"]
    fm = _fm()
    assert fm["chased_count"] == 1 and fm["last_chased_at"] == today
    assert "chase_pending" not in fm                       # phase two cleared it
