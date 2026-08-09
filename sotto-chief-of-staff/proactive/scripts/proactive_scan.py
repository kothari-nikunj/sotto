#!/usr/bin/env python3
"""
proactive_scan.py — decide what (if anything) Sotto should proactively nudge about RIGHT NOW.

This is the deterministic core of the `sotto-proactive` skill (a ~15-min cron). It is intentionally
conservative and the decision — what is DUE (lead times, the chase clock, once-per-item dedup) —
lives here, testable, so the agent only DRAFTS and DELIVERS what this returns. Whether any of it
reaches the user is the event funnel's call, below. PRINCIPLE: auto-draft, never auto-send; a nudge
surfaces a ready draft, it never sends on the user's behalf.

Six nudge kinds:
  - meeting_prep  — an external meeting starting within the lead window that you haven't prepped
                    (deterministic test: none of its external attendees are in TODAY's research
                    cache, i.e. no prep or brief run has covered this meeting's people yet)
  - commitment    — a continuity open-loop YOU owe whose deadline is today or overdue. Deliberately
                    one direction: a loop you are WAITING ON belongs to the chase lane and nowhere
                    else — one loop, one nudge, one register (the two branches draft opposite
                    things, and firing both about one loop is a contradiction, not a reminder).
  - chase         — something you're WAITING ON that has gone quiet past its chase clock. A nag is
                    not a reply: this kind exists so the draft can be a short warm "any word on X?"
                    instead of the commitment branch's reply. TWO-PHASE, one writer:
                    continuity_resolve stamps `chase_pending: <today>` and this lane delivers what
                    is pending today (at most ONE), then finalizes the count by shelling
                    `continuity_resolve.py --finalize-chase <anchor_key>` — so a chase that is never
                    delivered (quiet hours, a snooze, a dead container) is never counted against the
                    two this item gets.
  - birthday      — a saved contact whose birthday is today, or `SOTTO_BIRTHDAY_LEAD_DAYS` out
                    (the lead nudge is the one that can still become a gift). The day-of nudge is
                    skipped entirely once a brief has DELIVERED today: that brief carried the 🎂
                    line and the quick-wish tap, so this would be the same nudge twice.
  - handoff       — one thing you're owed has been chased its two times and still has no answer, so
                    Sotto stops guessing and asks: "I've nudged Maya twice about the contract —
                    nudge her again, or let it go?" Person, thing, binary choice. It shares the
                    tidy-up cooldown below but NOT its pile threshold, and it never hides inside the
                    generic offer — "your open-loops list is getting heavy" tells a first-time user
                    nothing about Maya.
  - retune_offer  — the stale pile is getting heavy (you keep seeing items you don't act on); offer
                    a quick cleanup. Throttled to once per cooldown window, NOT daily — and
                    suppressed within 2h of a delivered brief (Sprint 0 §6): the brief just listed
                    those very loops, so an immediate "your list is heavy" reads as a duplicate.

Inputs (argv JSON files; all optional except --now is derived):
  --calendar /tmp/sotto_cal.json     (gather_google calendar: [{id,summary,start,end,attendees[]}])
  --local /tmp/sotto_local.json      (read_local: contacts[] for birthdays)
  --user-email <addr>                (to detect EXTERNAL attendees)
Open loops are read HERE, straight from loops_query (the one sanctioned ledger read view) — there is
no hand-reshaped /tmp/sotto_cont.json step any more. `--continuity <file>` still overrides it for
tests and for a caller that already has the list.
Cadence: the user's explicit nudge snooze (`preferences.explicit.nudge_snooze_until`, written by the
sotto-feedback verbs — "quieter today" / "quiet until 3" / "back to normal") suppresses every nudge
while it is in the future, exactly like quiet hours — the funnel applies both, to nudges and to
messages, off the same preferences file.

ONE RULEBOOK, STRUCTURALLY (there is no second nudge path — docs/HOW-SOTTO-DECIDES.md): this file
decides WHAT is due; whether any of it reaches the user is `triage_event.triage()`'s call, and this
file asks it directly. Each due nudge becomes a synthetic `source: "proactive"` event
(`_proactive_event`, the one adapter) and the whole tick goes in as ONE bundle; the bundle that
comes back IS the nudge list. So the snooze, quiet hours, the mutes, the in-meeting hold, the daily
interrupt budget (ONE unit per bundle — a tick's nudges go out as one message, this SKILL's own
rule) and the surfaced.jsonl row per verdict are the funnel's, in the funnel's order, written once.
The one gate that is NOT here is delivery health: the receiver owns it (receiver.run_proactive_skill
probes the delivery channel before it ever spawns this skill), for the same reason the valve and the
tap do — never spend a nudge that cannot arrive.

Env: SOTTO_DATA (state dir), SOTTO_TIMEZONE (local day/quiet-hours), SOTTO_QUIET_START/END (default 21/7),
     SOTTO_USER_EMAIL,
     SOTTO_BIRTHDAY_LEAD_DAYS (how many days ahead the gift-idea nudge fires, default 3),
     SOTTO_NUDGE_BUDGET (shared daily interrupt cap, default 4).
Named constants, not knobs (defaults matter — see CLAUDE.md): PROACTIVE_LEAD_MIN (meeting lead
     window), RETUNE_OFFER_MIN (stale-loop threshold), RETUNE_OFFER_COOLDOWN_DAYS.

Output (stdout JSON): {"nudges":[…], "held":[…], "quiet":bool, "reason"?} — `nudges` are the ones to
deliver, `held` everything the funnel did NOT hand back (queued for the digest, or dropped): deliver
nothing for those.
Dedup — the one piece of bookkeeping the funnel has no analogue for, because a 15-minute cron has no
external event to dedupe on: keys already nudged today are recorded in
$SOTTO_DATA/proactive/<date>.json and skipped. Returned nudges are MARKED optimistically so a tick
never repeats one — a rare missed nudge is acceptable (the brief is the backstop); a repeated one is
annoying. In one sentence: a nudge burns its key once the funnel has decided about it (delivered,
queued, dropped); a nudge held by the CLOCK (quiet hours, a snooze) keeps its key, fires when the
hold lifts, and is submitted once for the day under a `held:<key>` marker so The Record carries the
verdict without one copy per 15-minute tick.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

# Reuse the brief's tz + contact helpers so "today"/external/birthday logic matches the brief exactly.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "_shared", "lib"))
from textutil import _arr, _s, unwrap_tool_result  # noqa: E402
from timeutil import _now_local, _parse_ts, configured_tz, configured_user_email  # noqa: E402
# The funnel itself — this file calls triage() in-process, so there is one gate order and not a
# second copy of it. (Same cross-skill sys.path pattern this file already uses for retune_scan;
# skill dirs aren't packages.)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                "event-triage", "scripts"))


def _load(path, default):
    if not path:
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _state_path(date: str) -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "proactive", f"{date}.json")


@contextmanager
def _state_lock(date: str):
    """The dedup state is a read-modify-write, and two producers run it (the */15 cron and the
    Bridge's wake trigger) — without a lock they both read an empty set and both fire the same
    nudge. This is triage_event._locked, IMPORTED not re-implemented: one lock, one
    implementation."""
    with _funnel()._locked(_state_path(date)):
        yield


def _load_state(date: str) -> set:
    try:
        with open(_state_path(date), encoding="utf-8") as f:
            return set(json.load(f).get("nudged") or [])
    except Exception:
        return set()


def _save_state(date: str, nudged: set):
    """Atomic (tmp + os.replace), like every other state file under $SOTTO_DATA. Callers hold
    _state_lock for the whole read-decide-write, so the file can never be half-updated either."""
    try:
        p = _state_path(date)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"nudged": sorted(nudged)}, f)
        os.replace(tmp, p)
    except Exception:
        pass


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _retune_marker() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "proactive", "retune_offer.last")


BRIEF_SUPPRESS_HOURS = 2   # a brief within this window just covered the open loops — don't restate
PROACTIVE_LEAD_MIN = 45    # how long before a meeting the prep nudge fires — enough time to read the
#                            prep and still walk in, not so early you've forgotten it by then.
RETUNE_OFFER_MIN = 6       # stale loops it takes before the "want to tidy up?" offer is worth making
RETUNE_OFFER_COOLDOWN_DAYS = 7   # …and how long before it may be made again — periodic, never a nag


def _recent_brief_delivered(now_local: datetime, within_hours: int = BRIEF_SUPPRESS_HOURS) -> bool:
    """True when a morning/evening brief was delivered within the last `within_hours` — read off
    the brief_marker.py claim files ($SOTTO_DATA/briefs/<date>.<kind>.delivered) by mtime. Checks
    today's AND yesterday's local dates so a brief delivered just before midnight still counts."""
    briefs = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "briefs")
    try:
        now_ts = now_local.timestamp()
    except (OSError, OverflowError, ValueError):
        return False
    for days_back in (0, 1):
        date = (now_local - timedelta(days=days_back)).strftime("%Y-%m-%d")
        for kind in ("morning", "evening"):
            try:
                mtime = os.path.getmtime(os.path.join(briefs, f"{date}.{kind}.delivered"))
            except OSError:
                continue
            if now_ts - mtime <= within_hours * 3600:
                return True
    return False


def _brief_delivered_today(now_local: datetime) -> bool:
    """Did a morning/evening brief DELIVER today (the brief_marker claim file exists)? The 2h
    suppression window above is a delay; this is the dedup: today's brief already carried the 🎂
    line AND the quick-wish tap for a birthday that is today, so the day-of nudge is the same nudge
    a second time, whatever the hour."""
    briefs = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "briefs")
    date = now_local.strftime("%Y-%m-%d")
    return any(os.path.exists(os.path.join(briefs, f"{date}.{kind}.delivered"))
               for kind in ("morning", "evening"))


def _retune_cooldown_ok(today_str: str) -> bool:
    """True when it's been at least the cooldown window since the last retune offer (or never offered),
    so we nudge to tidy up periodically rather than every single day."""
    cooldown = RETUNE_OFFER_COOLDOWN_DAYS
    try:
        with open(_retune_marker(), encoding="utf-8") as f:
            last = f.read().strip()[:10]
        days = (datetime.strptime(today_str, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
        return days >= cooldown
    except Exception:
        return True   # never offered → allowed


def _stamp_retune_offer(today_str: str):
    try:
        p = _retune_marker()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(today_str)
    except Exception:
        pass


def _funnel():
    """triage_event — the funnel, and the whole rulebook. Imported lazily (skill dirs aren't
    packages) but NOT optional: every gate a nudge passes lives there now, so a funnel that will
    not import stops the watcher rather than letting it nudge ungated."""
    import triage_event as te  # noqa: PLC0415  (event-triage/scripts is on sys.path)
    return te


def _proactive_event(n: dict) -> dict:
    """THE adapter: one nudge rendered as the synthetic event `triage()` reads. `source` is
    "proactive", which is what tells the funnel to classify it as a nudge Sotto planned (and what
    keeps it out of the escalation join, the release valve and the digest's heavy-day count)."""
    return {"source": "proactive", "kind": _s(n.get("kind")), "key": _s(n.get("key")),
            "text": _s(n.get("title")), "detail": _s(n.get("detail")),
            "person": _s(n.get("person")), "from": _s(n.get("identifier")),
            "channel": _s(n.get("channel")), "timestamp":
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def _clock_hold(te, now_local: datetime) -> str:
    """"" or the plain reason the funnel is holding EVERY nudge on the clock right now — the user's
    own snooze, or quiet hours, asked of the funnel and never decided here.

    It is not a gate: the funnel still classifies and records each nudge with its own reason. The
    watcher asks only so a quiet night submits each nudge ONCE instead of once per 15-minute tick —
    forty identical rows in The Record is not a better record."""
    prefs = te._load_prefs()
    if te._snoozed(prefs, now_local):
        return f"nudges snoozed until {_s(prefs.get('nudge_snooze_until'))}"
    if te._in_quiet_hours(now_local):
        start, end = te.quiet_window()
        return f"quiet hours ({start}:00–{end}:00)"
    return ""


def _submit(te, fresh: list, now_local: datetime) -> set:
    """Hand this tick's nudges to the funnel as ONE bundle and return the keys it decided to
    deliver; everything it decided otherwise it has already queued or dropped, with a reason, in
    the same ledgers every event writes to.

    The bundle is the unit on purpose: triage() charges the daily interrupt budget once per bundle,
    which IS this lane's "one unit per delivered push" rule — the tick's nudges go out as a single
    message (the skill's own rule), so they cost one unit between them."""
    if not fresh:
        return set()
    out = te.triage({"events": [_proactive_event(n) for n in fresh]}, now_local=now_local)
    return {_s((ev.get("event") or {}).get("key"))
            for ev in ((out.get("bundle") or {}).get("events") or [])}


def _finalize_chase(anchor_key: str) -> None:
    """Phase two of the chase: continuity_resolve stamped `chase_pending` when the item ripened;
    this counts it — `chased_count += 1`, `last_chased_at = today` — only now that the nudge has
    actually gone out. Shelled, not imported, so continuity_resolve stays the ledger's ONE writer.
    Best-effort: an unfinalized pending simply expires at day end, uncounted."""
    key = _s(anchor_key).strip()
    if not key:
        return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                          "morning-brief", "scripts", "continuity_resolve.py")
    try:
        subprocess.run([sys.executable, os.path.abspath(script), "--finalize-chase", key],
                       capture_output=True, text=True, timeout=30, check=False)
    except Exception:  # noqa: BLE001
        pass


def _research_cache_emails(date: str) -> set:
    """Emails researched TODAY ($SOTTO_DATA/cache/research_<date>.json, written by every successful
    research_attendees run — brief or prep). Read-only, best-effort, empty on any failure."""
    path = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "cache", f"research_{date}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:  # noqa: BLE001
        return set()
    return {_s(a.get("email")).lower().strip()
            for a in (data.get("attendees") or []) if isinstance(a, dict) and a.get("email")}


def _open_loops() -> list:
    """The deadline-bearing loops YOU OWE, in the shape scan() reads — straight from loops_query,
    the one sanctioned ledger read view. Deliberately ONE direction: a loop you are waiting on is
    the chase lane's, and walking both directions here meant one overdue waiting-on produced a
    `commitment` nudge ("draft the reply") and a `chase` nudge ("a nag is not a reply") in the same
    tick, under different keys, for two budget units and one message. Empty on any failure."""
    try:
        import loops_query as lq  # noqa: PLC0415  (_shared/scripts is on sys.path)
        data = lq.query()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in (data.get("you_owe") or []):
        if not isinstance(it, dict) or not it.get("deadline"):
            continue            # only a dated loop can be "due today or overdue"
        out.append({"id": f"you_owe:{_s(it.get('name'))}:{_s(it.get('what'))[:40]}",
                    "title": f"{_s(it.get('name'))} — {_s(it.get('what'))}",
                    "name": _s(it.get("name")),
                    "deadline": _s(it.get("deadline")),
                    "channel": _s(it.get("channel")),
                    "identifier": _s(it.get("identifier"))})
    return out


def _chase_candidates(today: str) -> list:
    """The waiting-ons the LEDGER marked chase-PENDING today. Read-only by design: continuity_resolve
    is the single writer of chase state — it stamps `chase_pending` (at most one item per local day)
    in the brief's Learn step, and this lane finalizes the count via `--finalize-chase` only once the
    nudge has actually gone out. Keying on the pending stamp rather than on `last_chased_at` is what
    makes that two-phase: a chase the user never received is never counted against the two."""
    try:
        import loops_query as lq  # noqa: PLC0415  (_shared/scripts is on sys.path)
        data = lq.query()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in (data.get("waiting_on_them") or []):
        if not isinstance(it, dict) or _s(it.get("chase_pending"))[:10] != today:
            continue
        chased = int(it.get("chased_count") or 0)
        age = it.get("age_days")
        out.append({"id": f"chase:{_s(it.get('name'))}:{_s(it.get('what'))[:40]}",
                    "title": f"{_s(it.get('name'))} — {_s(it.get('what'))}",
                    "name": _s(it.get("name")),
                    "anchor_key": _s(it.get("anchor_key")),
                    "detail": ("overdue" if it.get("overdue") else f"asked {age} days ago"
                               if age else "still open")
                              + (" · chased once already" if chased >= 1 else ""),
                    "channel": _s(it.get("channel")), "identifier": _s(it.get("identifier"))})
    return out


def _handoff_candidates() -> list:
    """The waiting-ons that have been chased their two times and still have no answer — the ones
    the ledger hands back to the user. They are NOT a heavy-pile problem, so they never surface as
    the generic tidy-up offer: each gets its own plain, named question, because "your open-loops
    list is getting heavy" tells a first-time user nothing about Maya and the contract."""
    try:
        import loops_query as lq  # noqa: PLC0415  (_shared/scripts is on sys.path)
        data = lq.query()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in (data.get("waiting_on_them") or []):
        if not isinstance(it, dict) or not it.get("chased_out"):
            continue
        name = _s(it.get("name")) or "them"
        what = _s(it.get("what"))
        out.append({"id": f"handoff:{name}:{what[:40]}",
                    "title": f"{name} — {what}",
                    "name": name,
                    # The whole message, written here so it is the same sentence every time:
                    # person + thing + a binary choice, and not one word of Sotto's vocabulary.
                    "question": (f"I've nudged {name} twice about {what.lower() or 'this'} — "
                                 "nudge them again, or let it go?"),
                    "channel": _s(it.get("channel")),
                    "identifier": _s(it.get("identifier"))})
    return out


def _stale_loop_count() -> int:
    """Reuse retune_scan's exact stale definition (overdue / 3–7d / repeat-surfaced) so the offer
    triggers on the same pile the cleanup would act on. Best-effort; 0 on any error."""
    try:
        import retune_scan  # noqa: PLC0415  (sibling in _shared/scripts, already on sys.path)
        return int(retune_scan.scan().get("counts", {}).get("stale", 0))
    except Exception:
        return 0


def scan(calendar, continuity, local, user_email, now_local,
         stale_count: int = 0, retune_offer_allowed: bool = False,
         prepped_emails=None, brief_recent: bool = False,
         chase_candidates=None, brief_today: bool = False, handoff_candidates=None) -> dict:
    """Pure decision (no I/O, no gates): given the inputs and the local 'now', return every nudge
    that is DUE now. Whether any of them reaches the user — the snooze, quiet hours, the mutes, the
    in-meeting hold, the daily interrupt budget — is the funnel's call, made in one place, on the
    bundle main hands it. `stale_count` / `retune_offer_allowed` / `prepped_emails` /
    `brief_recent` / `brief_today` / `chase_candidates` / `handoff_candidates` are computed by main
    (they need disk: the ledger, a cooldown marker, today's research cache, the delivered markers).
    """
    lead = PROACTIVE_LEAD_MIN
    user_email = (user_email or "").lower()
    user_domain = user_email.split("@")[1] if "@" in user_email else ""
    today = now_local.strftime("%Y-%m-%d")
    nudges = []

    # 1) Meeting prep — external meeting starting within the lead window (and not already started).
    if isinstance(calendar, dict):
        events = calendar.get("events") or calendar.get("items") or []
    else:
        events = calendar if isinstance(calendar, list) else []
    for e in events:
        if not isinstance(e, dict):
            continue
        st = _parse_ts(_s(e.get("start")))
        if st is None:
            continue
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        mins_away = (st.astimezone(timezone.utc) - now_local.astimezone(timezone.utc)).total_seconds() / 60.0
        if not (0 <= mins_away <= lead):
            continue
        ext = [a for a in _arr(e, "attendees")
               if _s(a.get("email")).lower() != user_email
               and not (user_domain and _s(a.get("email")).lower().endswith("@" + user_domain))]
        if not ext:
            continue  # internal/solo meeting — no prep nudge
        # "…that you haven't prepped" — the honest, deterministic signal for that is today's
        # research cache: an external attendee only lands in it because a meeting-prep or brief run
        # TODAY researched a meeting they're on. Any hit means this meeting's people are already
        # prepped, so the nudge would be offering work the user has. (Chosen over an agent-side
        # "did you prep?" question, which nothing can answer deterministically.)
        if prepped_emails and {_s(a.get("email")).lower().strip() for a in ext} & set(prepped_emails):
            continue
        nudges.append({"kind": "meeting_prep", "key": f"mtg:{_s(e.get('id'))}",
                       "title": _s(e.get("summary")) or "Meeting",
                       "detail": f"starts in ~{int(mins_away)} min · "
                                 + ", ".join(_s(a.get('displayName') or a.get('email')) for a in ext[:4])})

    # 2) Commitments — an open loop whose deadline is today or overdue. ONE loop, ONE nudge, ONE
    #    register: a loop that came back as a chase candidate belongs to the chase branch below and
    #    is skipped here, because "draft the reply" and "a nag is not a reply" cannot both be right
    #    about the same loop in the same tick.
    chase_titles = {_s(c.get("title")) for c in (chase_candidates or []) if isinstance(c, dict)}
    for c in (continuity if isinstance(continuity, list) else _arr(continuity, "items")):
        if not isinstance(c, dict):
            continue
        dl = _s(c.get("deadline") or c.get("due"))[:10]
        title = _s(c.get("title")) or "Open commitment"
        if title in chase_titles:
            continue
        if dl and dl <= today:
            nudges.append({"kind": "commitment", "key": f"loop:{_s(c.get('id')) or dl + _s(c.get('title'))[:20]}",
                           "title": title, "person": _s(c.get("name")),
                           "detail": ("overdue" if dl < today else "due today"),
                           "channel": _s(c.get("channel")), "identifier": _s(c.get("identifier"))})

    # 2b) The chase — something you're WAITING ON that the ledger marked chase-pending today. At
    #     most ONE per day even when several ripen (the rest keep their stamp and wait their turn):
    #     a day of chasing three people is a nag, and the whole point is that this stays warm.
    #     Guarded by the same 2h post-brief window as its two siblings, and for a stronger reason:
    #     the stamp is written in the brief's own Learn step, so without the guard every chase fires
    #     minutes after the brief that just listed the very item.
    if not brief_recent:
        for c in (chase_candidates or [])[:1]:
            nudges.append({"kind": "chase", "key": _s(c.get("id")) or f"chase:{today}",
                           "title": _s(c.get("title")) or "Still waiting on this",
                           "person": _s(c.get("name")),
                           "anchor_key": _s(c.get("anchor_key")),
                           "detail": _s(c.get("detail")),
                           "channel": _s(c.get("channel")), "identifier": _s(c.get("identifier"))})

    # 3) Birthdays — a saved contact whose birthday is today (MM-DD), plus a LEAD nudge
    #    `SOTTO_BIRTHDAY_LEAD_DAYS` (default 3) out: a gift idea three days early beats a reminder
    #    the morning of. Both are suppressed for 2h after a delivered brief (the same window and
    #    the same reason as the retune offer): the brief already carried the 🎂 line.
    #    The dedup key carries the OCCURRENCE YEAR (and the lead/day-of distinction), so each of
    #    the two fires exactly once per birthday — never twice, never again next year's worth.
    if not brief_recent:
        lead_days = max(0, _int_env("SOTTO_BIRTHDAY_LEAD_DAYS", 3))
        lead_at = now_local + timedelta(days=lead_days)
        windows = [(now_local.strftime("%m-%d"), now_local.year, 0)]
        if lead_days:
            windows.append((lead_at.strftime("%m-%d"), lead_at.year, lead_days))
        for mmdd, year, days_out in windows:
            # A brief DELIVERED today already carried the 🎂 Coming Up line and the quick-wish tap
            # for a birthday that is today — so the day-of nudge is dedup'd away, not delayed by 2h
            # into the same morning. The lead nudge (a gift, days out) is not in any brief.
            if not days_out and brief_today:
                continue
            for ct in _arr(local, "contacts"):
                if _s(ct.get("birthday"))[:5] != mmdd or not _s(ct.get("name")):
                    continue
                nm = _s(ct.get("name"))
                nudges.append({
                    "kind": "birthday",
                    "key": f"bday:{nm.lower()}:{year}" + (f":lead{days_out}" if days_out else ""),
                    "lead_days": days_out, "person": nm,
                    "title": (f"{nm}'s birthday is today" if not days_out
                              else f"{nm}'s birthday is in {days_out} days"),
                    "detail": ("send a quick note" if not days_out
                               else "enough time for a real gift — want me to pull what you know about them?")})

    # 4) The tidy-up lane, throttled by main's cooldown (NOT once a day) so it is a gentle periodic
    #    ask, never a daily nag. Two shapes, and the NAMED one wins:
    #    4a) a loop chased its two times with no answer — that is one person and one thing, so the
    #        nudge says so and asks the binary question. It ignores the pile threshold entirely: a
    #        single unanswered ask deserves the question even on a tidy day.
    #    4b) otherwise, the pile itself is heavy — offer a cleanup.
    threshold = RETUNE_OFFER_MIN
    if retune_offer_allowed and (handoff_candidates or []):
        h = (handoff_candidates or [])[0]         # one question at a time; the rest keep their turn
        nudges.append({"kind": "handoff", "key": _s(h.get("id")) or f"handoff:{today}",
                       "title": _s(h.get("title")) or "Still no answer",
                       "person": _s(h.get("name")), "detail": _s(h.get("question")),
                       "channel": _s(h.get("channel")),
                       "identifier": _s(h.get("identifier"))})
    elif retune_offer_allowed and stale_count >= threshold:
        nudges.append({"kind": "retune_offer", "key": "retune_offer",
                       "title": "Your open-loops list is getting heavy",
                       "detail": f"{stale_count} items keep showing up without action — want a quick cleanup?"})
    return {"nudges": nudges}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calendar")
    ap.add_argument("--continuity", help="OPTIONAL override; open loops are read from loops_query")
    ap.add_argument("--local")
    ap.add_argument("--user-email", dest="user_email")
    args = ap.parse_args()

    now_local = _now_local(configured_tz() or "+00:00")
    date = now_local.strftime("%Y-%m-%d")

    local = unwrap_tool_result(_load(args.local, {}))
    calendar = _load(args.calendar, [])
    # Open loops come from the ledger itself. --continuity is an override for tests/callers that
    # already hold the list; the agent no longer hand-reshapes one into /tmp.
    continuity = _load(args.continuity, None)
    if continuity is None:
        continuity = _open_loops()
    # Same "who am I" chain as the brief: an explicit --user-email, else SOTTO_USER_EMAIL, else the
    # google_account_email the Google connect learned (cb re-exports timeutil's one copy of it).
    user_email = args.user_email or configured_user_email()

    # Retune offer: the pile + its multi-day cooldown + the brief-collision window all need disk,
    # so compute here and pass in. A brief delivered in the last 2h already covered the loop pile —
    # offering a cleanup right after it is a rerun, so the offer waits for the next scan cycle.
    # The same 2h window mutes the birthday nudge (the brief carries the 🎂 line).
    stale_count = _stale_loop_count()
    brief_recent = _recent_brief_delivered(now_local)
    retune_ok = _retune_cooldown_ok(date) and not brief_recent
    due = scan(calendar, continuity, local, user_email, now_local,
               stale_count=stale_count, retune_offer_allowed=retune_ok,
               prepped_emails=_research_cache_emails(date), brief_recent=brief_recent,
               chase_candidates=_chase_candidates(date),
               brief_today=_brief_delivered_today(now_local),
               handoff_candidates=_handoff_candidates())["nudges"]

    te = _funnel()
    result = {"nudges": [], "held": [], "quiet": False}
    with _state_lock(date):
        seen = _load_state(date)
        fresh = [n for n in due if n["key"] not in seen]
        hold = _clock_hold(te, now_local)
        if hold:
            # Don't submit what today already recorded: the funnel writes a verdict for every nudge
            # it is handed, and a quiet night is forty ticks of the same list.
            fresh = [n for n in fresh if f"held:{n['key']}" not in seen]
            result["quiet"], result["reason"] = True, hold
        fired_keys = _submit(te, fresh, now_local)
        fired = [n for n in fresh if n["key"] in fired_keys]
        result["nudges"] = fired
        result["held"] = [n for n in fresh if n["key"] not in fired_keys]
        # A nudge burns its key once the funnel has DECIDED about it — delivered, queued for the
        # digest, or dropped — so a 15-min cron never repeats it. One held by the CLOCK keeps its
        # key (it fires when the hold lifts) and burns a `held:` marker instead, so The Record
        # carries the verdict once for the day rather than once per tick.
        burned = ({f"held:{n['key']}" for n in result["held"]} if hold
                  else {n["key"] for n in fresh})
        if burned:
            _save_state(date, seen | burned)
    if any(n["kind"] in ("retune_offer", "handoff") for n in fired):
        _stamp_retune_offer(date)   # one cooldown window covers both tidy-up shapes
    for n in fired:
        if n["kind"] == "chase":
            _finalize_chase(n.get("anchor_key"))  # phase two: it went out, so it counts
    try:
        from sotto_log import diag
        diag(f"[proactive_scan] {len(result['nudges'])} nudge(s)"
             + (f", {len(result['held'])} held for the digest" if result["held"] else "")
             + (f" — {result.get('reason')}" if result.get("quiet") else ""))
    except Exception:
        pass
    print(json.dumps(result))


if __name__ == "__main__":
    main()
