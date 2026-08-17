#!/usr/bin/env python3
"""
calcache.py — the ONE receiver-side calendar cache.

ROADMAP Step 2 item 2 + its Aug-6 post-audit amendment: *"the dashboard already forks
gather_google --skip-gmail behind a 10-minute module cache (/api/calendar) — extract ONE shared
receiver-side calendar cache that both the dashboard endpoint and the triage in-meeting-hold
consume — two competing caches is how drift starts."* This module IS that cache. Nothing else in
the receiver image forks gather_google for calendar data.

Two consumers, one fetch:

  1. **The dashboard's GET /api/calendar** (on demand, session-gated). `dashboard.api_calendar()`
     calls `snapshot()` through `HOOKS["calendar_snapshot"]` and joins attendees against the
     people index at SERVE time — the cache itself stays raw, exactly as before the extraction.
     Same shape, same 10-minute TTL, same quiet degrade (skills tree absent → `unavailable`, a
     failed gather IS cached so a broken Google setup can't fork a 60s subprocess per refresh).

  2. **The in-meeting hold** (`triage_event.py`, a different PROCESS): a daemon thread here
     refreshes `$SOTTO_DATA/cache/calendar_today.json` every SOTTO_CALENDAR_REFRESH_SECS
     (default 900 = the */15 cadence the valve and the proactive heartbeat already use). The file
     carries today's events as {summary, start, end, attendees, all_day} — TIMES and a count, not
     people: triage needs to know it's in a room, not who's in it.

Both go through `snapshot()`, which holds `_CAL_LOCK` ACROSS the fork. That is the single-flight
guarantee the amendment asks for: a second caller inside the TTL window waits and gets the first
caller's result rather than starting a competing gather. With TTL=600s under a 900s refresh, the
thread normally does the fetching and a dashboard hit within 10 minutes of a tick rides it free.

  3. **The post-meeting tap** (Editor Step 2 item 3 — the one ADDITIVE feature of the Editor week):
     the SAME refresh thread, on the SAME tick, also asks `tap_tick()` which of today's events
     ENDED since the last tick (≥ TAP_GRACE_MIN_DEFAULT ago, so the user is actually out of the room)
     and had at least one other human on them. Each such event fires EXACTLY ONCE — `cache/
     meeting_taps.json` is a date-keyed, atomically-written record of the event-ends already
     handled — through `HOOKS["meeting_tap"]`, which the receiver wires to a synthetic
     `source: "meeting_end"` event pushed through the NORMAL triage funnel. Detection lives here
     because the raw snapshot (unlike calendar_today.json) still carries attendee names and emails,
     which the nudge needs; the whole discipline (quiet hours, snooze, the in-meeting hold for the
     NEXT meeting) is triage's, not this module's. The tap's OWN cap lives here:
     SOTTO_TAP_MAX_PER_DAY (default 3), enforced exactly-once at dispatch below. It is the only cap
     on taps — triage exempts them from the daily interrupt budget precisely because this one
     exists, so a nine-meeting day can't starve a genuinely urgent interrupt, and vice versa.

Attendee counting reuses the docket's learned distinction (static/app.js `inferSelfEmail` /
`attendeeInfo`): the address present in EVERY peopled event is the user's own (SOTTO_USER_EMAIL
wins when the environment names it, then the `google_account_email` the Google connect learned),
rooms (`resource.calendar.google`) are not people, and what's
stored is the count of OTHER humans — so a solo focus block counts 0 and never holds a nudge.

Loaded by receiver.py via importlib (the relay.py/connectors.py/dashboard.py pattern); the receiver
wires HOOKS — late-bound lambdas over ITS globals — so this module never imports the receiver and
stays import-safe on its own. `local_today` is deliberately a hook rather than a second
implementation: the ROADMAP's first-night-timezone amendment requires the wall-clock features to
read the SAME resolved tz path the rest of the receiver does (dashboard._local_today: SOTTO_TIMEZONE
→ config/settings.json → server local).

Env: SOTTO_CALENDAR_REFRESH_SECS (default 900; 0 disables the daemon thread and the file entirely),
     SOTTO_MEETING_TAP (0 disables the post-meeting tap), SOTTO_TAP_MAX_PER_DAY (default 3).
Named constants, not knobs (defaults matter — see CLAUDE.md): TAP_GRACE_MIN_DEFAULT,
     TAP_LOOKBACK_INTERVALS/TAP_LOOKBACK_FLOOR_MIN, TAP_SKIP_INTERNAL.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

# ── Wiring surface (receiver overrides these; the defaults keep the module import-safe) ──────────

def _unwired_write_json(*_a, **_k):
    raise RuntimeError("calcache.HOOKS['write_json'] is unwired — load this module from receiver.py")


HOOKS = {
    "data_root": lambda: os.environ.get("SOTTO_DATA", "/data"),
    "find_script": lambda *rel: None,          # skills-tree discovery (receiver._find_sotto_script)
    # THE atomic JSON write for the image — connectors.write_json, wired by the receiver. No sane
    # default: a silently dropped tap record would replay a nudge the user already got.
    "write_json": _unwired_write_json,
    "local_today": lambda: time.strftime("%Y-%m-%d"),   # ONE tz resolution per process
    # Post-meeting tap: receiver._dispatch_meeting_tap(synthetic_event) → True when the tap was
    # handed to triage (and so counts against the day's tap cap). The default no-op keeps this
    # module import-safe and makes an unwired process detect nothing, quietly.
    "meeting_tap": lambda ev: False,
    # Calendar-diff nudge (a decline, a last-minute invite, a move, a cancellation): the receiver
    # wires the same dispatch the tap uses. Default no-op for import-safety, as above.
    "calendar_change": lambda ev: False,
}

CALENDAR_TTL_SECS = 600                # the plan's 10-minute number, unchanged by the extraction
GATHER_TIMEOUT_SECS = 60               # gather_google.py --skip-gmail subprocess budget
REFRESH_SECS_DEFAULT = 900             # the */15 cadence the valve heartbeat already uses
CACHE_DIRNAME = "cache"
CACHE_FILENAME = "calendar_today.json"
# Post-meeting tap: the date-keyed record of event-ends already handled (exactly-once), and the
# `source` string that IS the contract with triage_event.py's post_meeting branch.
TAP_STATE_FILENAME = "meeting_taps.json"
MEETING_END_SOURCE = "meeting_end"
# Calendar-diff nudges: the `source` string that IS the contract with triage_event.py's
# calendar_change branch, and the relevance windows (named constants, not knobs). A change matters
# in real time only while it is imminent — everything further out is the brief's job.
CALENDAR_CHANGE_SOURCE = "calendar_change"
CHANGE_WINDOW_HOURS = 24           # invited/moved/cancelled: the meeting starts within this window
DECLINE_WINDOW_HOURS = 48          # a decline is worth knowing a bit earlier — reschedules take time
INVITE_GRACE_MIN = 15              # an invite for a meeting that started minutes ago still counts
TAP_GRACE_MIN_DEFAULT = 5          # "ended ≥5 min ago" — you're out of the room, not packing up
TAP_MAX_PER_DAY_DEFAULT = 3        # the taps' own daily cap (they don't spend the interrupt budget)
TAP_LOOKBACK_FLOOR_MIN = 30        # ≥2 ticks at the default cadence, so one missed tick still fires
TAP_LOOKBACK_INTERVALS = 2         # how far back a tick looks, in refresh intervals — wide enough
#                                    that one skipped tick still delivers, narrow enough that a
#                                    restart doesn't replay the whole day.
TAP_SKIP_INTERNAL = True           # skip taps for internal-only standups/syncs (title shape ×
#                                    everyone on the user's own domain) — the one meeting class
#                                    nobody wants a follow-up draft for.
# Internal-only *standups* are the one meeting class nobody wants a follow-up draft for. Recurrence
# isn't in the cache shape (normalize_event drops recurringEventId), so the cheap test is
# title-shape × everyone-shares-the-user's-domain — narrow on purpose: a founder whose whole day is
# internal still gets taps for everything that isn't literally a standup/sync.
STANDUP_RE = re.compile(r"\b(stand[\s-]?up|scrum|daily sync|daily huddle|team sync|weekly sync|"
                        r"all[\s-]?hands)\b", re.I)
# Google's room resources are attendees on the wire but not humans — the docket drops them, so
# does the count that decides whether a block is a meeting.
ROOM_MARKER = "resource.calendar.google"
DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")


def _s(v) -> str:
    return v.strip() if isinstance(v, str) else ""


def _root() -> str:
    return HOOKS["data_root"]()


def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def _date_shift(date: str, days: int) -> str:
    """YYYY-MM-DD ± days, or "" for an unparseable input. (Pure date arithmetic — dashboard.py
    keeps its own copy for the research/brief windows; there is no shared state to drift.)"""
    try:
        return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


# ── Normalization (moved verbatim from dashboard.py — the cache's shape IS the Today-view shape) ──

def _norm_cal_attendee(a):
    """gather_google leaves attendees RAW from the Calendar API — {email, displayName,
    responseStatus, organizer, self, …} dicts (or bare address strings from some MCP hosts).
    The Today view needs exactly {name, email}; RSVP state and flags never leave the server."""
    if isinstance(a, str):
        a = {"email": a}
    if not isinstance(a, dict):
        return None
    email = _s(a.get("email") or a.get("address"))
    name = _s(a.get("displayName") or a.get("name"))
    return {"name": name, "email": email} if (email or name) else None


def _norm_cal_event(e):
    """One gather_google cal-out event ({id, summary, start, end, location, description,
    meetingLink, attendees}) → the Today-view shape: {summary, start, end, attendees[{name,email}]}
    plus location/meeting_link only when present. description and id are dropped server-side."""
    if not isinstance(e, dict):
        return None
    raw_att = e.get("attendees")
    ev = {
        "summary": _s(e.get("summary") or e.get("title")),
        "start": _s(e.get("start")),
        "end": _s(e.get("end")),
        "attendees": [a for a in ((_norm_cal_attendee(x) for x in raw_att)
                                  if isinstance(raw_att, list) else ()) if a],
    }
    loc = _s(e.get("location"))
    if loc:
        ev["location"] = loc
    link = _s(e.get("meetingLink") or e.get("hangoutLink") or e.get("htmlLink"))
    if link:
        ev["meeting_link"] = link
    return ev


def _run_calendar_gather():
    """Fork the skills tree's gather_google.py --skip-gmail into a private temp dir and return the
    normalized today+tomorrow events, sorted by start. None ⇒ the skills tree is absent on this
    box. The gather itself fails-empty by design (exit 0 + empty file); a crash/timeout here
    degrades to [] the same way — the Today view never 500s over Google, and the refresh thread
    just leaves yesterday's file alone rather than writing an empty belief."""
    try:
        script = HOOKS["find_script"]("_shared", "scripts", "gather_google.py")
    except Exception:  # noqa: BLE001
        script = None
    if not script:
        return None
    raw = []
    with tempfile.TemporaryDirectory(prefix="sotto-cal-") as td:
        cal_out = os.path.join(td, "cal.json")
        try:
            subprocess.run([sys.executable, script, "--skip-gmail", "--cal-out", cal_out,
                            "--gmail-out", os.path.join(td, "gmail.json")],
                           capture_output=True, text=True, timeout=GATHER_TIMEOUT_SECS,
                           env={**os.environ, "SOTTO_DATA": _root()})
            with open(cal_out, encoding="utf-8") as f:
                raw = json.load(f)
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, ValueError):
            raw = []
    if isinstance(raw, dict):                      # tolerate an {events: […]} envelope
        raw = raw.get("events") or []
    raw = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    # The RAW wire events, in memory only — the calendar-diff detector needs the two fields
    # normalization deliberately strips from everything served or written (the Calendar `id`, which
    # is the only identity that survives a move, and attendee `responseStatus`, which is what a
    # decline IS). They live in this process and never reach a file or the browser.
    _LAST_RAW["events"] = raw
    events = [ev for ev in map(_norm_cal_event, raw) if ev]
    today = HOOKS["local_today"]()
    keep = {d for d in (today, _date_shift(today, 1)) if d}
    # The gather's window is now→+3d; the Today view wants today + tomorrow. Filter on the start's
    # date prefix (the event's own wall-clock date); a non-date start stays in (tolerant).
    events = [ev for ev in events if not DATE_RE.match(ev["start"][:10]) or ev["start"][:10] in keep]
    events.sort(key=lambda ev: ev["start"])
    return events


# ── The one cache: (ts, value) under one lock, held ACROSS the fetch (single flight) ──────────────

_CAL_LOCK = threading.RLock()
_CAL_CACHE: dict = {"ts": 0.0, "value": None}
_LAST_RAW: dict = {"events": None}         # the latest gather's RAW events (see _run_calendar_gather)
_CHANGE_BASELINE: dict = {"events": None}  # the previous change-tick's raw events — the diff's left side


def snapshot() -> dict | None:
    """{events, generated_at, cached} — the whole calendar truth this process has, or None when
    the skills tree isn't on this box (the caller degrades quietly; nothing worth caching).

    `_CAL_LOCK` is held across the gather ON PURPOSE: the refresh thread and a dashboard request
    landing in the same TTL window must produce ONE fork, not two. The waiter blocks for the
    (≤60s, once) gather and then reads the fresh value — which is strictly better than starting a
    competing 60s subprocess. A failed/empty gather is cached like any other result."""
    with _CAL_LOCK:
        val = _CAL_CACHE["value"]
        if val is not None and time.time() - _CAL_CACHE["ts"] < CALENDAR_TTL_SECS:
            return {**val, "cached": True}
        events = _run_calendar_gather()
        if events is None:
            return None
        val = {"events": events, "generated_at": _iso()}
        _CAL_CACHE["ts"] = time.time()
        _CAL_CACHE["value"] = val
        return {**val, "cached": False}


# ── The triage-side projection: today's events as times + a count ────────────────────────────────

def _infer_self_email(events) -> str:
    """The user's own address, inferred exactly the way the docket does (static/app.js
    inferSelfEmail): the one address present in EVERY peopled event, with at least two such events.
    Two addresses everywhere (a recurring pair) is inconclusive → "" and nobody is skipped."""
    counts, peopled = {}, 0
    for ev in events:
        seen = set()
        for a in (ev.get("attendees") or []):
            em = _s(a.get("email")).lower() if isinstance(a, dict) else ""
            if em:
                seen.add(em)
        if seen:
            peopled += 1
            for em in seen:
                counts[em] = counts.get(em, 0) + 1
    if peopled < 2:
        return ""
    everywhere = [em for em, n in counts.items() if n == peopled]
    return everywhere[0] if len(everywhere) == 1 else ""


def _settings_email() -> str:
    """`google_account_email` — the address the Google connect derived onto the volume
    (receiver.capture_google_account_email). Tolerates a missing or garbage settings file."""
    try:
        with open(os.path.join(_root(), "config", "settings.json"), encoding="utf-8") as f:
            return _s((json.load(f) or {}).get("google_account_email")).lower()
    except Exception:  # noqa: BLE001
        return ""


def _self_email(events) -> str:
    """The user's own address, in the CANONICAL order every consumer uses: SOTTO_USER_EMAIL when the
    environment names it (an override), else the `google_account_email` the Google connect learned,
    else the docket's inference. A named address matters for the post-meeting tap on a light day —
    inference needs at least two peopled events, and without an answer the user would be listed as
    an attendee of their own meeting."""
    return _s(os.environ.get("SOTTO_USER_EMAIL")).lower() or _settings_email() or _infer_self_email(events)


def _other_attendee_rows(ev, self_email: str) -> list:
    """The OTHER humans on this event as {name, email} — the docket's meeting-vs-solo-block test
    (attendeeInfo): the user is skipped, room resources are skipped, nameless-and-emailless entries
    were already dropped by normalization. A solo focus block yields []. The in-meeting hold only
    ever needs the LENGTH of this (see _other_attendees); the post-meeting tap needs the names."""
    rows = []
    for a in (ev.get("attendees") or []):
        if not isinstance(a, dict):
            continue
        em = _s(a.get("email")).lower()
        if em and (em == self_email or ROOM_MARKER in em):
            continue
        rows.append({"name": _s(a.get("name")), "email": _s(a.get("email"))})
    return rows


def _other_attendees(ev, self_email: str) -> int:
    """How many OTHER humans are on this event. A solo focus block counts 0."""
    return len(_other_attendee_rows(ev, self_email))


def today_rows(events, today: str = "") -> list:
    """Today's events in the triage shape: {summary, start, end, attendees, all_day}. `attendees`
    is the OTHER-human count (see _other_attendees) — triage needs times, not people. `all_day` is
    a bare YYYY-MM-DD start (gather_google passes Google's `date` through when there's no
    `dateTime`), which the in-meeting hold must never treat as being in a room."""
    today = today or HOOKS["local_today"]()
    self_email = _self_email(events)
    rows = []
    for ev in events:
        start = _s(ev.get("start"))
        if start[:10] != today:
            continue
        rows.append({
            "summary": _s(ev.get("summary")),
            "start": start,
            "end": _s(ev.get("end")),
            "attendees": _other_attendees(ev, self_email),
            "all_day": bool(DATE_RE.match(start)),
        })
    return rows


def refresh_secs() -> int:
    try:
        return int((os.environ.get("SOTTO_CALENDAR_REFRESH_SECS") or "").strip()
                   or REFRESH_SECS_DEFAULT)
    except ValueError:
        return REFRESH_SECS_DEFAULT


def cache_path() -> str:
    return os.path.join(_root(), CACHE_DIRNAME, CACHE_FILENAME)


def refresh_once() -> bool:
    """One refresh: `snapshot()` (shared fetch + TTL) → atomically rewrite calendar_today.json.
    False (silently) when the skills tree is absent or the write fails — the in-meeting hold reads
    a missing/stale file as "no hold", so a quiet no-op is the correct degrade.

    `generated_at` is carried through from the snapshot, NOT stamped at write time: it is the age
    of the BELIEF, which is what triage's stale-cache honesty check must measure. `refresh_secs`
    travels with the file so the reader can derive its own staleness bound from the writer's real
    cadence instead of guessing."""
    snap = snapshot()
    if snap is None:
        return False
    today = HOOKS["local_today"]()
    payload = {
        "generated_at": _s(snap.get("generated_at")) or _iso(),
        "date": today,
        "refresh_secs": refresh_secs(),
        "events": today_rows(snap.get("events") or [], today),
    }
    try:
        HOOKS["write_json"](cache_path(), payload)   # THE atomic write (connectors.write_json)
        return True
    except OSError:
        return False


# ── Post-meeting tap: which of today's meetings ENDED since the last tick ─────────────────────────
# Editor Step 2 item 3, the additive half: "your 2:00 PM with Sarah Chen just wrapped — want me to
# send the follow-up?". Detection ONLY. Every question of whether the user should be interrupted
# right now (budget, quiet hours, snooze, being in the NEXT meeting) belongs to triage_event.py, and
# is answered there by pushing the tap through the ordinary funnel as a synthetic event — this
# module never re-implements a gate. The two things it does own are exactly-once (the state file)
# and the tap-specific daily cap.

def tap_state_path() -> str:
    return os.path.join(_root(), CACHE_DIRNAME, TAP_STATE_FILENAME)


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip() or default)
    except ValueError:
        return default


def taps_enabled() -> bool:
    return ((os.environ.get("SOTTO_MEETING_TAP") or "").strip() or "1") != "0"


def tap_grace_min() -> int:
    """Minutes after the end time before a tap may fire — the "you're actually out of the room"
    grace. TAP_GRACE_MIN_DEFAULT is the one writer."""
    return max(0, TAP_GRACE_MIN_DEFAULT)


def tap_max_per_day() -> int:
    return max(0, _int_env("SOTTO_TAP_MAX_PER_DAY", TAP_MAX_PER_DAY_DEFAULT))


def tap_lookback_min() -> int:
    """How far back a tick looks for an unhandled end: TAP_LOOKBACK_INTERVALS refresh intervals
    (floor TAP_LOOKBACK_FLOOR_MIN) so a single skipped/slow tick still delivers the tap, while a
    receiver that boots at 4pm doesn't replay the whole day's meetings as a burst of nudges. Always
    at least a minute wider than the grace, so a window narrower than the grace can't silently
    disable the feature."""
    window = max(TAP_LOOKBACK_FLOOR_MIN, (refresh_secs() * TAP_LOOKBACK_INTERVALS) // 60)
    return max(tap_grace_min() + 1, window)


def _parse_aware(ts: str):
    """An offset-carrying timestamp → aware datetime; anything naive or unparseable → None.
    Google returns RFC3339 dateTime with an offset for every timed event, so this is the normal
    path. A naive time is deliberately NOT tapped: dating it would need a SECOND timezone
    resolution in this module (the amendment's "two competing X is how drift starts"), and a
    missed delight is a much cheaper mistake than a nudge fired at the wrong hour. The in-meeting
    hold — which must never MISS — keeps its own naive branch in triage_event.py."""
    t = _s(ts)
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _tap_key(ev: dict) -> str:
    """Identity of an event-END for the exactly-once record. normalize_event drops the Calendar id
    before the cache, so the key is the tuple that IS unique on one calendar: start|end|summary."""
    return f"{_s(ev.get('start'))}|{_s(ev.get('end'))}|{_s(ev.get('summary'))}"


def _is_internal_standup(summary: str, others: list, self_email: str) -> bool:
    """Internal-only standup/sync → skip (see STANDUP_RE). Requires BOTH a standup-shaped title and
    every other attendee carrying an address in the user's own domain — an attendee whose email we
    don't have makes "internal" unprovable, and unprovable means don't skip."""
    if not self_email or not STANDUP_RE.search(_s(summary)):
        return False
    domain = self_email.split("@")[-1]
    if not domain or not others:
        return False
    return all(_s(o.get("email")).lower().split("@")[-1] == domain for o in others)


def ended_meetings(events, now_utc: datetime, today: str = "") -> list:
    """Today's events that ended in [now - lookback, now - grace] and had at least one other human,
    oldest end first. Pure over the RAW snapshot events (the ones that still carry attendee names —
    calendar_today.json deliberately keeps only a count). Skipped, silently: all-day events (a
    label on the day, not a meeting), solo blocks, naive/unparseable times, and internal-only
    standups when TAP_SKIP_INTERNAL is on."""
    today = today or HOOKS["local_today"]()
    self_email = _self_email(events)
    grace, lookback = tap_grace_min() * 60, tap_lookback_min() * 60
    skip_internal = TAP_SKIP_INTERNAL
    out = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        start, end = _s(ev.get("start")), _s(ev.get("end"))
        if start[:10] != today or DATE_RE.match(start) or DATE_RE.match(end):
            continue
        end_dt = _parse_aware(end)
        if end_dt is None:
            continue
        age = (now_utc - end_dt).total_seconds()
        if age < grace or age > lookback:
            continue
        others = _other_attendee_rows(ev, self_email)
        if not others:
            continue
        summary = _s(ev.get("summary"))
        if skip_internal and _is_internal_standup(summary, others, self_email):
            continue
        out.append({"key": _tap_key(ev), "summary": summary, "start": start, "end": end,
                    "attendees": others, "meeting_link": _s(ev.get("meeting_link")),
                    "location": _s(ev.get("location"))})
    out.sort(key=lambda c: c["end"])
    return out


def _load_tap_state(today: str) -> list:
    """The event-end keys already handled TODAY. A stamp from another date reads as empty — that IS
    the day rollover (no cleanup job), and it resets the daily cap at the same instant."""
    try:
        with open(tap_state_path(), encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and _s(st.get("date")) == today:
            return [k for k in (st.get("fired") or []) if isinstance(k, str)]
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return []


def _save_tap_state(today: str, fired: list) -> None:
    """THE atomic write (connectors.write_json) — written after EVERY dispatch, not once per tick,
    so a crash between two taps can never replay the first one."""
    try:
        HOOKS["write_json"](tap_state_path(), {"date": today, "fired": fired})
    except OSError:
        pass


def tap_event(cand: dict) -> dict:
    """The synthetic event triage_event.py's post_meeting branch consumes. `timestamp` is the
    meeting's END, so the funnel's ordinary staleness gate measures the right thing: a tap the
    receiver only notices an hour late is stale by the same rule a late message is."""
    return {
        "source": MEETING_END_SOURCE,
        "rowid": cand["key"],          # → thread key "meeting_end:<key>": one cooldown per meeting
        "timestamp": cand["end"],
        "summary": cand["summary"],
        "start": cand["start"],
        "end": cand["end"],
        "attendees": cand["attendees"],
        "meeting_link": cand.get("meeting_link") or "",
        "location": cand.get("location") or "",
        "is_from_me": False,
        "text": "",
    }


def tap_tick(now_utc: datetime | None = None) -> int:
    """One post-meeting-tap pass, riding the refresh thread's clock. Returns how many taps were
    dispatched. The hook returning False (triage unavailable, channel unhealthy) does NOT mark the
    end handled — the next tick retries it while it's still inside the lookback window."""
    if not taps_enabled():
        return 0
    cap = tap_max_per_day()
    if cap <= 0:
        return 0
    snap = snapshot()
    if snap is None:
        return 0                      # no skills tree on this box — nothing to detect
    now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc
    today = HOOKS["local_today"]()
    cands = ended_meetings(snap.get("events") or [], now_utc, today)
    if not cands:
        return 0
    fired = _load_tap_state(today)
    dispatched = 0
    for cand in cands:
        if len(fired) >= cap:
            break                     # the day's tap allowance is spent; the budget is safe
        if cand["key"] in fired:
            continue
        try:
            ok = bool(HOOKS["meeting_tap"](tap_event(cand)))
        except Exception as e:  # noqa: BLE001 — a broken tap must never kill the refresh thread
            print(f"[sotto] meeting tap error: {e}", flush=True)
            ok = False
        if ok:
            fired.append(cand["key"])
            _save_tap_state(today, fired)
            dispatched += 1
    return dispatched


# ── Calendar-diff nudges: what CHANGED about the imminent calendar since the last tick ────────────
# "Ali Panju just declined your 11am" / "Jake sent a last-minute invite for 11:30" — the two Poke
# examples that were unbuildable here because nothing watched the diff. Detection ONLY, the tap's
# exact posture: every interruption question (quiet hours, snooze, mutes) is triage's, reached by
# pushing a synthetic `source: "calendar_change"` event through the ordinary funnel. Exactly-once
# is TWO layers: the in-memory baseline advances only when every change dispatched, and the
# receiver's (source,rowid) seen-ring dedupes any re-detection after a partial failure. A restart
# only resets the baseline (first tick after boot detects nothing) — it can never replay the day.

def calendar_nudges_enabled() -> bool:
    return ((os.environ.get("SOTTO_CALENDAR_NUDGES") or "").strip() or "1") != "0"


def _raw_others(ev: dict, self_email: str) -> list:
    """The OTHER humans on a RAW wire event as {name, email, status} — same self/room skips as
    _other_attendee_rows, plus the responseStatus a decline is detected from."""
    rows = []
    for a in (ev.get("attendees") or []):
        if isinstance(a, str):
            a = {"email": a}
        if not isinstance(a, dict):
            continue
        em = _s(a.get("email") or a.get("address")).lower()
        if em and (em == self_email or ROOM_MARKER in em):
            continue
        if not em and not _s(a.get("displayName") or a.get("name")):
            continue
        rows.append({"name": _s(a.get("displayName") or a.get("name")), "email": em,
                     "status": _s(a.get("responseStatus")).lower()})
    return rows


def _hours_until(start: str, now_utc: datetime):
    """Hours from now until a timed start, or None for all-day/naive/unparseable — the same
    "never act on a guess" rule the tap applies."""
    if DATE_RE.match(_s(start)):
        return None
    dt = _parse_aware(start)
    if dt is None:
        return None
    return (dt - now_utc).total_seconds() / 3600.0


def _wall(start: str) -> str:
    """'11:00 AM' from an ISO start, best-effort — the human half of the change sentence."""
    dt = _parse_aware(start)
    if dt is None:
        return ""
    return dt.strftime("%-I:%M %p") if os.name != "nt" else dt.strftime("%I:%M %p").lstrip("0")


def calendar_changes(baseline: list, current: list, now_utc: datetime, self_email: str) -> list:
    """The diff that matters, as candidate dicts — pure, so it tests like ended_meetings.
    Four kinds, each one sentence: a NEW event with another human starting within
    CHANGE_WINDOW_HOURS is a last-minute invite; an attendee whose responseStatus turned
    "declined" on a meeting within DECLINE_WINDOW_HOURS is a decline; a changed start on a
    meeting within the window is a move; an event that vanished (or turned status=cancelled)
    within the window is a cancellation. Skipped, silently: all-day events, solo blocks,
    internal-only standups (the tap's own rule), and anything already past."""
    old_by_id = {_s(e.get("id")): e for e in baseline if _s(e.get("id"))}
    new_by_id = {_s(e.get("id")): e for e in current if _s(e.get("id"))}
    out = []

    def _relevant(ev, window_h, allow_started_min=0.0):
        h = _hours_until(_s(ev.get("start")), now_utc)
        return h is not None and (-allow_started_min / 60.0) <= h <= window_h

    def _eligible(ev):
        others = _raw_others(ev, self_email)
        if not others:
            return None
        if _is_internal_standup(_s(ev.get("summary")), others, self_email):
            return None
        return others

    for eid, ev in new_by_id.items():
        others = _eligible(ev)
        if others is None:
            continue
        summary, start = _s(ev.get("summary")), _s(ev.get("start"))
        old = old_by_id.get(eid)
        if old is None:
            if _relevant(ev, CHANGE_WINDOW_HOURS, allow_started_min=INVITE_GRACE_MIN):
                out.append({"kind": "invited", "key": f"{eid}:invited:{start}",
                            "summary": summary, "start": start, "old_start": "",
                            "who": "", "attendees": others})
            continue
        old_start = _s(old.get("start"))
        if old_start and start and old_start != start and (
                _relevant(ev, CHANGE_WINDOW_HOURS) or
                _relevant(old, CHANGE_WINDOW_HOURS)):
            out.append({"kind": "moved", "key": f"{eid}:moved:{start}",
                        "summary": summary, "start": start, "old_start": old_start,
                        "who": "", "attendees": others})
        if _relevant(ev, DECLINE_WINDOW_HOURS):
            old_status = {r["email"]: r["status"] for r in _raw_others(old, self_email) if r["email"]}
            for r in others:
                if (r["email"] and r["status"] == "declined"
                        and old_status.get(r["email"], "") != "declined"):
                    out.append({"kind": "declined", "key": f"{eid}:declined:{r['email']}",
                                "summary": summary, "start": start, "old_start": "",
                                "who": r["name"] or r["email"], "attendees": others})
    for eid, old in old_by_id.items():
        gone = eid not in new_by_id
        cancelled = not gone and _s(new_by_id[eid].get("status")).lower() == "cancelled"
        if not (gone or cancelled):
            continue
        others = _eligible(old)
        if others is None or not _relevant(old, CHANGE_WINDOW_HOURS):
            continue
        out.append({"kind": "cancelled", "key": f"{eid}:cancelled",
                    "summary": _s(old.get("summary")), "start": _s(old.get("start")),
                    "old_start": "", "who": "", "attendees": others})
    out.sort(key=lambda c: c["start"])
    return out


def change_event(cand: dict) -> dict:
    """The synthetic event triage_event.py's calendar_change branch consumes. `text` is the human
    sentence the Record and the agent both read — composed HERE so detection and phrasing can't
    drift apart. `timestamp` is now: the change just happened, whatever the meeting's time."""
    when = _wall(cand["start"])
    title = cand["summary"] or "a meeting"
    kind = cand["kind"]
    if kind == "declined":
        text = f"{cand['who']} just declined your {when or title}" + (f" — {title}" if when else "")
    elif kind == "invited":
        first = next((r["name"] or r["email"] for r in cand["attendees"]), "")
        text = f"last-minute invite: {title} at {when}" + (f" with {first}" if first else "")
    elif kind == "moved":
        text = f"{title} moved to {when}" + (f" (was {_wall(cand['old_start'])})" if cand.get("old_start") else "")
    else:
        text = f"your {when or ''} {title} was cancelled".replace("  ", " ")
    return {
        "source": CALENDAR_CHANGE_SOURCE,
        "rowid": cand["key"],
        "change": kind,
        "timestamp": _iso(),
        "summary": cand["summary"],
        "start": cand["start"],
        "old_start": cand.get("old_start") or "",
        "who": cand.get("who") or "",
        "attendees": cand["attendees"],
        "is_from_me": False,
        "text": text,
    }


def change_tick(now_utc: datetime | None = None) -> int:
    """One calendar-diff pass, riding the refresh thread's clock right after the tap. Returns how
    many changes were dispatched. First tick (or first after restart) only sets the baseline."""
    if not calendar_nudges_enabled():
        return 0
    current = _LAST_RAW["events"]
    if current is None:
        return 0                              # no gather has run yet this process
    baseline = _CHANGE_BASELINE["events"]
    if baseline is None:
        _CHANGE_BASELINE["events"] = current
        return 0
    now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc
    self_email = _self_email([e for e in map(_norm_cal_event, current) if e])
    cands = calendar_changes(baseline, current, now_utc, self_email)
    dispatched, all_ok = 0, True
    for cand in cands:
        try:
            ok = bool(HOOKS["calendar_change"](change_event(cand)))
        except Exception as e:  # noqa: BLE001 — a broken dispatch must never kill the refresh thread
            print(f"[sotto] calendar change error: {e}", flush=True)
            ok = False
        if ok:
            dispatched += 1
        else:
            all_ok = False
    if all_ok:
        _CHANGE_BASELINE["events"] = current  # every change made it — the diff is settled
    return dispatched


def start_refresh_thread():
    """Start the calendar refresh daemon at server boot (same pattern as the Gmail poll and the
    release-valve heartbeat). SOTTO_CALENDAR_REFRESH_SECS=0 disables it. Refreshes IMMEDIATELY
    and then every interval: the in-meeting hold is worthless until the file exists, and a
    would-be nudge in the first quarter-hour is exactly the one that shouldn't land mid-meeting.
    Every tick is fully guarded — a broken gather must never kill the thread, and with no skills
    tree on the box the thread simply idles quietly. Returns the thread (or None when disabled).

    The post-meeting tap rides THIS clock (Step 2 item 3 — least machinery: the tap needs exactly
    the calendar the refresh already fetched, on exactly the cadence it already runs). Ordered
    refresh → tap so the tap reads a snapshot from this tick, and guarded SEPARATELY so neither
    half can take the other down."""
    secs = refresh_secs()
    if secs <= 0:
        return None

    def _loop():
        while True:
            try:
                refresh_once()
            except Exception as e:  # noqa: BLE001 — the heartbeat must never die
                print(f"[sotto] calendar refresh error: {e}", flush=True)
            try:
                tap_tick()
            except Exception as e:  # noqa: BLE001 — nor may the tap
                print(f"[sotto] meeting tap tick error: {e}", flush=True)
            try:
                change_tick()
            except Exception as e:  # noqa: BLE001 — nor may the calendar diff
                print(f"[sotto] calendar change tick error: {e}", flush=True)
            time.sleep(max(secs, 60))

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t
