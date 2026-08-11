#!/usr/bin/env python3
"""
triage_event.py — Tier 0 + Tier 1 of the event-driven proactivity funnel (Phase 2 §3).

The receiver POSTs raw Bridge/email events here SYNCHRONOUSLY (stdin in, verdict on stdout):

  stdin:  {"events":[{source, rowid, ...}, ...], "catchup": bool}
  stdout: {"verdict":"drop|queue|agent", "reason":"...", "bundle":{...}}

Tier 0 (deterministic, free) — the verdict matrix:
  is_from_me                           → queue (class "signal"; ledger fodder, never a nudge)
  answered / outgoing call             → drop  (no action to take)
  missed call from a KNOWN person      → agent (the interrupt bar) — VIPs only during quiet hours
  missed call from an unknown number   → queue
  OTP / shortcode / system message     → drop  (silent — _is_likely_automated + render_local's filter)
  muted sender / muted person (prefs)  → drop
  a meeting that just ENDED            → agent (class "post_meeting" — the tap; see below)
  a nudge the WATCHER planned          → agent, class = its kind (see _classify_proactive)
  quiet hours (everything else)        → queue (class "quiet")
  group without a name-mention         → queue (class "group")
  unknown non-VIP 1:1                  → queue (class "unknown"; never agent)
  survivors (known 1:1 / mentioned group / known email sender) → Tier 1

Tier 1 (one small LLM call per survivor): SOTTO_TRIAGE_MODEL (default "gemini-3.5-flash-lite") with a
tight prompt (event text + a sender one-liner from the graph/pulse, ≤ 2k tokens) → strict JSON
{"class":"urgent|actionable|scheduling_ask|ambient|ignore","why"}. urgent|actionable|scheduling_ask →
agent (scheduling_ask additionally tells the sotto-event skill to gather the calendar and propose
slots); ambient → queue; ignore → drop. ANY Tier-1 error → queue (fail toward silence, never toward
noise).

Cooldown: one agent verdict per thread key per SOTTO_EVENT_COOLDOWN_MIN (default 20 min), persisted in
$SOTTO_DATA/events/cooldowns.json; a suppressed event queues (class "cooldown").

Daily interrupt budget (the CROSS-THREAD volume control — per-thread cooldowns can't stop ten
different senders in one hour): $SOTTO_DATA/events/budget.json holds {date, count} keyed by the
LOCAL date, so it rolls over at local midnight with no cleanup job. The budget is charged per
DELIVERED INTERRUPT, not per event: a bundle is one message however many events it carries, so the
first non-exempt agent verdict in a batch spends the day's unit and the rest of that batch ride it
free (same for a valve tick, which promotes up to 2 events into ONE bundle). Beyond
SOTTO_NUDGE_BUDGET (default 4/day) further agent verdicts demote to queue with class "budget"
(held_class preserved, exactly like cooldown/quiet/stale, so the digest ranks them as
deferred-actionable). The check and the spend are ONE atomic step under the budget lock
(_budget_try_spend) — five producers run at once, and a read-then-write gap is exactly where the cap
gets exceeded. BUDGET_EXEMPT_CLASSES are
never counted and never demoted: Tier-0 missed calls, the "escalation" class the multi-channel join
below emits, and the "post_meeting" tap — which has its own named daily cap (SOTTO_TAP_MAX_PER_DAY,
default 3) instead. In one sentence: interrupts spend the daily budget (4/day), post-meeting taps
have their own cap (3/day), and neither eats the other. The valve honors the same budget, so
promotion can never push past the day's cap.

Real-time escalation join (Editor Step 2 item 4): at event time, for a KNOWN sender, the funnel
looks back over the retained event ledgers ($SOTTO_DATA/events/queue.jsonl — every queued verdict,
carrying the full event — plus surfaced.jsonl, the one-line-per-verdict ledger, which is the ONLY
place an agent verdict such as a live missed call is retained) for the SAME person on a DIFFERENT
channel within ESCALATION_WINDOW_MIN_DEFAULT (45 min). Identity matching is deliberately
conservative — an exact resolved-name match or an exact normalized-identifier match (the same
last-10-digit/lowercase-email rule the graph uses); nothing fuzzy, ever. Sotto's OWN output is never
evidence that a person reached you (a post-meeting tap, a proactive nudge), and neither is the
user's own outbound (class "signal", whose recorded sender is the RECIPIENT). The join fires only when
the evidence is real: at least one of the joined events is a CALL, or one of them carries an
ask-like Tier-1 class (urgent / actionable / scheduling_ask). Then the verdict becomes agent with
class "escalation" — which BUDGET_EXEMPT_CLASSES / MEETING_HOLD_EXEMPT_CLASSES already exempt from
the daily budget and the in-meeting hold, and which COOLDOWN_EXEMPT_CLASSES exempts from the
per-thread cooldown (in triage AND in the valve): someone reaching you twice across two channels in
half an hour is the highest-stakes moment of the day, and it is exactly the moment a cooldown
stamped by the FIRST of those two messages would silence. The reason string carries the name and the
fact for The Record ("Sarah called AND emailed within 20 min"), so the nudge can lead with it.
Cadence still wins: a snoozed or quiet-hours event never escalates (the user asked for silence by
the clock), and neither does an unknown sender, a group message with no name-mention, or the user's
own outbound. v1 skips the calls-pairing subtleties (call bursts, same-channel repeats).

Snooze (the user-facing cadence lever, written by the sotto-feedback skill through
preferences.py → explicit.nudge_snooze_until): while the stamp is in the future, Tier 0 demotes
EVERY would-be nudge — missed calls included, because unlike quiet hours this is an explicit
request — to queue with class "snoozed", and the release valve holds entirely. "snoozed" is
deliberately NOT in PROMOTABLE_CLASSES: when the snooze lifts, held events do not come back as a
burst of late nudges; they ride the digest/brief (digest_check counts "snoozed" like the other
deferred-agent classes, subject to its own window).

Queue: $SOTTO_DATA/events/queue.jsonl — one {ts, verdict_class, sender, event} per line (bounded via
sotto_log.bounded_append). Entries demoted from an agent verdict (cooldown/quiet/catchup-stale) also
carry `held_class` — the Tier-0/1 class the event earned BEFORE demotion, so the release valve and
digest can tell "was interrupt-worthy, deterministically held" from born-ambient. digest_check.py
consumes it; the release valve (below) promotes from it; brief composers may later.

Surfaced ledger: $SOTTO_DATA/events/surfaced.jsonl — ONE line per triaged event at verdict time:
{ts, sender, channel, verdict: agent|queue|drop|promoted, reason, class}. Bounded like the queue.
This is the diagnostic substrate for "why didn't I get nudged?" — the dashboard's Record view
(GET /api/ledger, source "triage") renders it, and the learning loop's surfaced/outcome join reads it.

In-meeting hold (Editor Step 2 item 2): the receiver's shared calendar cache (runtime/trigger-
receiver/calcache.py — the SAME fetch + TTL that serves the dashboard's /api/calendar) refreshes
$SOTTO_DATA/cache/calendar_today.json every 15 min with today's events as {summary, start, end,
attendees, all_day}, where `attendees` counts OTHER humans. When NOW falls inside a timed event with
at least one other attendee, would-be agent verdicts demote to queue with class "meeting_hold" and
the reason "in a meeting until H:MM AM/PM — <name>". Solo blocks (attendees 0) and all-day events
never hold — the docket already learned that distinction. Exempt: MEETING_HOLD_EXEMPT_CLASSES (a
missed call or an escalation is exactly what SHOULD reach you mid-meeting; the post-meeting tap is
NOT exempt here even though it is budget-exempt — a tap firing while you're in the next meeting must
still hold).
The hold is checked BEFORE the cooldown stamp and the budget spend, so a held ask burns neither and
can promote intact. Stale-cache honesty, two checks with one posture — never act on a stale belief
about where you are: a file whose `generated_at` is older than TWO refresh intervals does NOT hold,
and neither does one whose `date` (the day calcache gathered) isn't the local today. Missing or
unparseable file → no hold, silently.

Post-meeting tap (Editor Step 2 item 3, the additive half): the receiver's calendar refresh thread
(calcache.py) detects meetings that ENDED since the last tick — ≥ its grace window ago, with at
least one other human — and pushes each one through THIS funnel exactly once as a synthetic event
with `source: "meeting_end"` ({summary, start, end, attendees:[{name,email}], timestamp = the end}).
Tier 0 classes it "post_meeting" → agent, and the agent side (event-triage/SKILL.md) runs the
battle-tested follow-up composition for that meeting: "Your 2:00 PM with Sarah Chen just wrapped.
Want me to send the follow-up? — [draft]".

The tap is a NUDGE, and the whole point of routing it through triage as an event rather than
nudging from the receiver is that every existing gate then applies with no second implementation:
it holds during quiet hours and an active snooze, it holds while the user is in the NEXT meeting
(back-to-back: the tap for A queues as "meeting_hold" with held_class "post_meeting" and the release
valve promotes it intact when B ends), it burns a per-meeting cooldown key, it demotes when stale,
and it lands in surfaced.jsonl like every other verdict. The one gate it does NOT share is the daily
interrupt budget: taps are in BUDGET_EXEMPT_CLASSES because they already have their own named cap —
SOTTO_TAP_MAX_PER_DAY (default 3), enforced exactly-once at dispatch by calcache.tap_tick via
meeting_taps.json. Sharing one 4/day pot meant three taps could starve a genuinely urgent fifth
event; two caps, each with a plain name, is the legible rule. The receiver's other gate is channel
health before dispatch.

Release valve (`triage_event.py --valve`, invoked by the receiver's heartbeat thread): when no hold
is active (not quiet hours, not in a meeting), promote up to VALVE_MAX_PER_TICK queued KNOWN-sender
events that were demoted for cooldown/quiet/catchup/meeting reasons and are younger than
VALVE_MAX_AGE_MIN (4h), respecting the per-thread cooldown and an hourly budget
(VALVE_MAX_PER_HOUR = 2). "meeting_hold" entries are exempt from the max-age check —
an ask Sotto itself held is never too old to deliver, and a 3h meeting would otherwise expire an
ask that arrived before it started; the ≤2/hr valve cap still bounds the volume.
Promotion returns the same {"verdict":"agent","bundle":…} shape triage() does, so the receiver routes
it through the identical sotto-event nudge path; promoted entries leave the queue and land in
surfaced.jsonl as verdict "promoted". SOTTO_VALVE=0 disables. This is the fix for the audit's worst
failure: an actionable event arriving during cooldown/quiet/catchup NEVER nudged — silently deferred
until digest (needs 8+ signals) or the evening brief.

Sender resolution reuses the brief's own machinery over the cached local snapshot
($SOTTO_DATA/knowledge/last_local_snapshot.json — compose_brief._save_local_snapshot writes it):
contacts → build_contact_lookup → resolve_*_name, plus relationship_state.json for the pulse.

VIP (deliberately simple, documented): a sender is VIP when (a) the user SAID SO — their display
name is on preferences.explicit.vip_people, written by the same preferences.py CLI chat and the
dashboard's VIP toggle both use — or (b) their attention-queue entry in relationship_state.json has
priority >= VIP_PRIORITY_MIN (10 — the pulse's priority is interactions × days-waiting ×
type-weight, so 10+ means a top-of-queue relationship), or (c) their knowledge-graph person file
mentions "family" (family clears the quiet-hours bar for missed calls). The stated list is checked
FIRST because a user's own word outranks any heuristic.

User promotion (`triage_event.py --promote <queue-key>`, the dashboard's "nudge me now"): the same
per-entry eligibility the valve applies (_valve_candidate), the same budget spend, the same
surfaced.jsonl "promoted" row, the same bundle shape — one queue entry instead of a tick's worth.
The gates it does NOT apply are the clock ones: quiet hours and the nudge snooze exist to stop
UNPROMPTED interruptions, and this one was prompted by the user, who is demonstrably awake. The
in-meeting hold DOES apply (that is about where the message lands, not about the hour), and so does
the daily interrupt budget.

The proactive watcher (proactive_scan.py, the ~15-min cron) is not a second lane: it derives its six
kinds, hands them to `triage()` IN-PROCESS as one bundle of `source: "proactive"` events, and
delivers whatever comes back in the bundle. So there is one gate order, written once, in
_classify_proactive + triage() — not a second copy kept in step by discipline. Its own bookkeeping
(once-per-day-per-key dedup, the chase's two-phase count) stays with the watcher, because a
15-minute cron with no external event has nothing else to dedupe on.

Quiet hours: SOTTO_QUIET_START/END + SOTTO_TIMEZONE, and this file OWNS the rule — for messages, for
the tap and for the watcher's nudges, which pass through the same _in_quiet_hours() call as
everything else.

Env: SOTTO_DATA, SOTTO_TIMEZONE, SOTTO_QUIET_START/END (default 21/7), SOTTO_TRIAGE_MODEL,
     SOTTO_EVENT_COOLDOWN_MIN (default 20), SOTTO_NUDGE_BUDGET (agent verdicts per local day,
     default 4; 0 = nudge nothing but the exempt classes),
     SOTTO_USER_NAME (group name-mention detection; unset → groups always queue), GOOGLE_AI_API_KEY,
     SOTTO_VALVE (0 disables the release valve), SOTTO_CALENDAR_REFRESH_SECS (the receiver's
     calendar-cache cadence, default 900; the in-meeting hold's staleness bound is 2× it).
Named constants, not knobs (defaults matter — see CLAUDE.md): VIP_PRIORITY_MIN,
     VALVE_MAX_PER_HOUR, VALVE_MAX_AGE_MIN (promotion window — a real ask from 3h ago still deserves
     a nudge; a 2-day-old one doesn't. "meeting_hold" entries ignore it: an ask Sotto itself held is
     never too old to deliver), EVENT_MAX_AGE_MIN, ESCALATION_WINDOW_MIN_DEFAULT (the cross-channel
     join window).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

# Same cross-skill reuse pattern as proactive_scan: put _shared on sys.path and import
# the brief's own helpers, so triage and the brief agree on "automated", "system message", "known".
_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED_LIB = os.path.join(_HERE, "..", "..", "_shared", "lib")
_SHARED_SCRIPTS = os.path.join(_HERE, "..", "..", "_shared", "scripts")
_SHARED_KNOWLEDGE = os.path.join(_HERE, "..", "..", "_shared", "knowledge")
for _p in (_SHARED_LIB, _SHARED_SCRIPTS, _SHARED_KNOWLEDGE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from keys import queue_key  # noqa: E402,F401  (the queue-line id, shared with the dashboard)
from textutil import (  # noqa: E402
    _s, _is_likely_automated, _looks_like_phone_number, _sender_addr, _extract_sender_name,
    _normalize_identifier,
)
from render_local import (  # noqa: E402
    _is_system_message, build_contact_lookup, resolve_imessage_name, resolve_whatsapp_name,
    resolve_call_name,
)
from timeutil import _now_local, _parse_ts, configured_tz  # noqa: E402
import gemini as _gemini  # noqa: E402  (module-level so tests can stub _gemini_once)
import preferences as _prefs  # noqa: E402

# Bounds for the ambient queue file (rotate-keeping-tail, same mechanism as compose_brief.log).
QUEUE_MAX_BYTES = 4 * 1024 * 1024
QUEUE_KEEP_LINES = 4000
# The surfaced ledger mirrors the queue's bounds — same volume, same growth profile.
SURFACED_MAX_BYTES = 4 * 1024 * 1024
SURFACED_KEEP_LINES = 4000
COOLDOWN_PRUNE_SECS = 24 * 3600   # cooldown entries older than a day are dead weight
TIER1_TEXT_MAX = 1500             # chars of event text sent to Tier 1 (keeps the prompt ≤ 2k tokens)
# Release valve: ≤ this many promotions per tick, deterministically-deferred classes only.
VALVE_MAX_PER_TICK = 2
# …and across ticks: the valve is a trickle, not a flood. Promotions spend the daily interrupt
# budget like any other nudge, so this is the second, tighter ceiling on the same spend.
VALVE_MAX_PER_HOUR = 2
# How old a held event may be and still be promoted: a real ask from 3h ago still deserves a nudge,
# a 2-day-old one doesn't. "meeting_hold" entries skip this check — a long meeting must not expire
# an ask Sotto itself held.
VALVE_MAX_AGE_MIN = 240
# Reconnect grace: an event older than this (or any message in a Bridge catch-up batch) never nudges
# in real time — it queues for the digest/next brief. Missed calls are exempt.
EVENT_MAX_AGE_MIN = 30
# The attention-queue priority at which a contact counts as a VIP — VIPs are the only senders whose
# missed call clears quiet hours. A stated vip_people entry or a "family" mention also qualifies.
VIP_PRIORITY_MIN = 10
# "budget" belongs here (a held real ask, same as the others) — the valve's own budget check keeps a
# promotion from exceeding the day's cap, so in practice these wait for the day to roll over.
# "snoozed" deliberately does NOT: an explicit "be quiet" must not end in a burst when it lifts.
# "meeting_hold" belongs here for the same reason the others do — it is a deterministically-deferred
# real ask, and the valve's next tick after the meeting ends IS its release path (no new machinery).
PROMOTABLE_CLASSES = frozenset({"quiet", "cooldown", "stale", "budget", "meeting_hold"})
# Classes that neither spend nor are capped by the daily interrupt budget: a missed call is rare and
# high-signal, "escalation" (the multi-channel join) bypasses cooldown AND budget by design, and the
# post-meeting tap has its OWN daily cap (SOTTO_TAP_MAX_PER_DAY, default 3, enforced exactly-once at
# dispatch in calcache.tap_tick via meeting_taps.json). The rule in one sentence: interrupts spend
# the daily budget (4/day), post-meeting taps have their own cap (3/day), neither eats the other.
BUDGET_EXEMPT_CLASSES = frozenset({"missed_call", "escalation", "post_meeting"})
# The in-meeting hold exempts LESS than the budget does: a missed call or a multi-channel escalation
# is precisely what should reach you mid-meeting, but a tap that fires while you're in your NEXT
# meeting must still hold (that's the back-to-back case the valve promotes out of). So this is
# deliberately its own set, not an alias of BUDGET_EXEMPT_CLASSES — taps are hold-able but
# budget-free.
MEETING_HOLD_EXEMPT_CLASSES = frozenset({"missed_call", "escalation"})
# The per-thread cooldown exempts ONLY the escalation join — deliberately NOT aliased to the budget
# set. A missed call still respects its thread's cooldown (three calls in ten minutes is one nudge),
# but an escalation is defined by a SECOND channel arriving inside the window, which is precisely
# when the first message's cooldown stamp would otherwise swallow it.
COOLDOWN_EXEMPT_CLASSES = frozenset({"escalation"})

# ── Real-time escalation join (Editor Step 2 item 4) ───────────────────────────────────────────────
ESCALATION_CLASS = "escalation"
ESCALATION_WINDOW_MIN_DEFAULT = 45
# Tail of each ledger scanned per event. The window is minutes wide, the files are bounded at 4000
# lines — this is the cheap read that makes the join O(1)-ish per event instead of O(file).
ESCALATION_SCAN_LINES = 500
# One side of the join must be a real ask: a call (someone tried to reach you live) or a Tier-1
# class that means "a human is waiting". Ambient chatter on two channels is not an escalation.
ESCALATION_ASK_CLASSES = frozenset({"urgent", "actionable", "scheduling_ask"})
# Classes that never escalate: the user's own outbound, the cadence holds (an explicit snooze and
# quiet hours outrank the join — the user asked for silence by the clock), a group message nobody
# named the user in, an unknown sender, and the synthetic post-meeting tap (it has no sender).
# "missed_call" is deliberately ABSENT: a call landing minutes after their email is the textbook
# escalation, and upgrading it costs nothing (both classes are budget-exempt) while gaining the
# name-carrying cross-channel reason and the cooldown bypass.
ESCALATION_SKIP_CLASSES = frozenset({"signal", "snoozed", "quiet", "group", "unknown",
                                     "post_meeting", "automated", "system", "muted", "call"})
# Channel → the verb the reason string uses. "Sarah called AND emailed within 20 min".
ESCALATION_VERBS = {"calls": "called", "email": "emailed", "imessage": "texted",
                    "whatsapp": "messaged on WhatsApp"}

# The post-meeting tap's synthetic event: the `source` string IS the contract with calcache.py
# (runtime/trigger-receiver/calcache.MEETING_END_SOURCE). Nothing else in the funnel emits it.
MEETING_END_SOURCE = "meeting_end"
# The proactive watcher's synthetic event: the `source` string IS the contract with
# proactive_scan._proactive_event, the one adapter between the watcher's nudge shape and this
# funnel. A chase/birthday/commitment nudge is Sotto talking, not a person.
PROACTIVE_SOURCE = "proactive"
# Rows that are never EVIDENCE for the escalation join: Sotto's own output (the tap, the proactive
# nudge) is never evidence that a person reached you; neither is the user's own outbound, whose
# recorded sender is the person they wrote TO. Both would manufacture a cross-channel escalation
# out of one real message — the loudest nudge Sotto can send, on the strength of its own voice.
NON_EVIDENCE_SOURCES = frozenset({MEETING_END_SOURCE, PROACTIVE_SOURCE})
NON_EVIDENCE_CLASSES = frozenset({"signal"})

# ── The shared calendar cache (written by runtime/trigger-receiver/calcache.py) ────────────────────
CALENDAR_CACHE_REL = ("cache", "calendar_today.json")
CALENDAR_REFRESH_SECS_DEFAULT = 900     # mirrors calcache.REFRESH_SECS_DEFAULT
CALENDAR_REFRESH_SECS_BOUNDS = (60, 3600)   # a file claiming an absurd cadence is clamped, not trusted
CALENDAR_STALE_INTERVALS = 2            # older than 2 refresh intervals ⇒ never hold on it


def _data_root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _events_dir() -> str:
    return os.path.join(_data_root(), "events")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


@contextmanager
def _locked(path: str):
    """Hold an exclusive advisory lock on `<path>.lock` for the block. Up to four producers run this
    script at once (the Bridge push, the Gmail poll, the valve heartbeat, the meeting tap), and both
    the budget spend and the valve's queue rewrite are read-modify-write — without this, two
    concurrent spends can each read 3 and write 4, handing out a free nudge. The lock file sits next
    to its data file under $SOTTO_DATA; the atomic tmp+os.replace writes stay exactly as they were,
    now inside the lock. Best-effort: if the lock can't be taken (unwritable volume, no flock), the
    block still runs — a rare race beats a silenced nudge."""
    fh = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path + ".lock", "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        if fh is not None:
            fh.close()
        fh = None
    try:
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()


def quiet_window() -> tuple[int, int]:
    """The quiet-hours window as (start_hour, end_hour). ONE reading of SOTTO_QUIET_START/END for
    every caller — the proactive lane prints the window in its hold reason and must never state a
    different one from the window this file enforces."""
    return _int_env("SOTTO_QUIET_START", 21), _int_env("SOTTO_QUIET_END", 7)   # 9pm → 7am


def _in_quiet_hours(now_local) -> bool:
    """The quiet-hours rule, owned here and called (never re-implemented) by the other nudge
    producer: proactive_scan.py imports this module and asks. Wraps midnight (21..24 and 0..7 by
    default)."""
    quiet_start, quiet_end = quiet_window()
    h = now_local.hour
    return (h >= quiet_start or h < quiet_end) if quiet_start > quiet_end else (quiet_start <= h < quiet_end)


# ── Cached-state loaders (all best-effort; a missing file means "know nothing", never a crash) ──────

def _load_snapshot_local() -> dict:
    """The brief's cached LocalData (compose_brief._save_local_snapshot → last_local_snapshot.json).
    Source of contacts (name resolution) and person_knowledge (Tier-1 one-liners)."""
    try:
        path = os.path.join(_data_root(), "knowledge", "last_local_snapshot.json")
        with open(path, encoding="utf-8") as f:
            local = (json.load(f) or {}).get("local") or {}
        return local if isinstance(local, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _load_relationship_state() -> dict:
    try:
        path = os.path.join(_data_root(), "knowledge", "relationship_state.json")
        with open(path, encoding="utf-8") as f:
            state = json.load(f) or {}
        return state if isinstance(state, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _load_prefs() -> dict:
    try:
        return _prefs.load_explicit()
    except Exception:  # noqa: BLE001
        return {"mute_senders": [], "mute_people": [], "mute_sections": [], "tone_notes": [],
                "vip_people": [], "nudge_snooze_until": ""}


def _snoozed(prefs: dict, now_local) -> bool:
    """Is the user's nudge snooze (preferences.explicit.nudge_snooze_until) still in effect?
    Delegated to preferences.snooze_active so triage, proactive and the verbs share ONE rule.
    Any failure reads as 'not snoozed' — a broken stamp must never silence Sotto permanently."""
    try:
        return bool(_prefs.snooze_active(now_local=now_local, explicit=prefs))
    except Exception:  # noqa: BLE001
        return False


# ── In-meeting hold (quiet hours, but for rooms) ───────────────────────────────────────────────────
# Reads the receiver's shared calendar cache. Deliberately CHEAP and TOLERANT: one small JSON file,
# no network, no subprocess — and every unknown (missing file, bad JSON, stale belief, unparseable
# times) resolves to "no hold". A silence Sotto can't justify is worse than a nudge.

def _calendar_cache_path() -> str:
    return os.path.join(_data_root(), *CALENDAR_CACHE_REL)


def _load_calendar_today() -> dict:
    try:
        with open(_calendar_cache_path(), encoding="utf-8") as f:
            cal = json.load(f)
        return cal if isinstance(cal, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _calendar_refresh_secs(cal: dict) -> int:
    """The writer's real cadence, clamped: the file carries `refresh_secs`, and the env knob is the
    fallback. Reading it from the file means the staleness bound tracks the receiver's actual
    cadence instead of a constant that drifts the day someone retunes the thread."""
    for raw in (cal.get("refresh_secs"), os.environ.get("SOTTO_CALENDAR_REFRESH_SECS")):
        try:
            secs = int(float(_s(raw) if isinstance(raw, str) else raw))
        except (TypeError, ValueError):
            continue
        lo, hi = CALENDAR_REFRESH_SECS_BOUNDS
        if secs > 0:
            return max(lo, min(hi, secs))
    return CALENDAR_REFRESH_SECS_DEFAULT


def _calendar_is_stale(cal: dict, now_utc: datetime) -> bool:
    """Older than CALENDAR_STALE_INTERVALS refresh intervals ⇒ stale. `generated_at` is the age of
    the BELIEF (when the gather ran), not of the write — which is the thing that must be fresh.
    No parseable stamp at all reads as stale: never hold on a belief you can't date."""
    ts = _parse_ts(_s(cal.get("generated_at")))
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    age = (now_utc - ts).total_seconds()
    return age > CALENDAR_STALE_INTERVALS * _calendar_refresh_secs(cal)


def _wall_clock(ts: str) -> str:
    """'2026-08-07T14:30:00-07:00' → '2:30 PM'. The event's OWN wall clock is already the user's
    (Google renders event times in the calendar's timezone), so there is no tz math here to drift
    from the receiver's resolved zone — and nothing to get wrong at a DST boundary."""
    m = re.search(r"T(\d{2}):(\d{2})", _s(ts))
    if not m:
        return ""
    h = int(m.group(1))
    return f"{h % 12 or 12}:{m.group(2)} {'AM' if h < 12 else 'PM'}"


def _event_attendee_count(ev: dict) -> int:
    """Other-human count. calcache writes an int; a list is tolerated in case a future writer
    changes shape — anything else reads as 0 (no hold)."""
    n = ev.get("attendees")
    if isinstance(n, bool):
        return 0
    if isinstance(n, int):
        return max(0, n)
    if isinstance(n, list):
        return len(n)
    return 0


def _spans_now(ev: dict, now_local, now_utc: datetime) -> bool:
    """Is NOW inside [start, end)? Offset-carrying event times are compared in UTC; bare wall-clock
    times are compared against the user's local clock. Either endpoint unparseable → False."""
    start, end = _parse_ts(_s(ev.get("start"))), _parse_ts(_s(ev.get("end")))
    if start is None or end is None:
        return False
    if start.tzinfo is not None and end.tzinfo is not None:
        now = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=timezone.utc)
    else:
        start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
        now = now_local.replace(tzinfo=None) if now_local.tzinfo is not None else now_local
    return start <= now < end


def _meeting_hold_until(now_local, now_utc: datetime) -> str:
    """"2:30 PM" when the user is in a meeting right now, else "" (no hold). A meeting is a TIMED
    event with at least one other attendee — a solo focus block is not a room, and an all-day event
    is a label on the day, not a place you are. Overlaps pick the latest end: the hold should lift
    when the user is actually free."""
    cal = _load_calendar_today()
    events = cal.get("events")
    if not isinstance(events, list) or _calendar_is_stale(cal, now_utc):
        return ""
    # The writer stamps the day it gathered (calcache.refresh_once → `date`). A file for any other
    # day is yesterday's belief about where you are, whatever its `generated_at` says: never hold
    # on yesterday's calendar. Same posture as the staleness check — an unknown reads as "no hold".
    if _s(cal.get("date")) != _local_day(now_local):
        return ""
    ends = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("all_day"):
            continue
        if _event_attendee_count(ev) < 1:
            continue
        if _spans_now(ev, now_local, now_utc):
            ends.append(_s(ev.get("end")))
    if not ends:
        return ""

    def _end_key(e: str):
        """Naive sort key — one calendar means one offset, so stripping it orders correctly and
        can never raise on a mixed aware/naive comparison."""
        p = _parse_ts(e)
        return p.replace(tzinfo=None) if p is not None else datetime.min

    return _wall_clock(max(ends, key=_end_key))


# ── Tier-0 primitives ──────────────────────────────────────────────────────────────────────────────

_OTP_RE = re.compile(r"\b(verification code|one[- ]?time (?:pass)?code|security code|login code|"
                     r"2fa code|otp)\b", re.I)


def _looks_like_otp(text: str) -> bool:
    """OTP/2FA blast: the canonical phrasing plus an actual 4-8 digit code in the body."""
    t = _s(text)
    return bool(_OTP_RE.search(t)) and bool(re.search(r"\b\d{4,8}\b", t))


def _is_shortcode(handle: str) -> bool:
    """SMS shortcode sender (3-6 digit 'number') — always automated, never a person."""
    d = _s(handle).strip()
    return d.isdigit() and 3 <= len(d) <= 6


def _event_text(e: dict) -> str:
    return _s(e.get("text")) or _s(e.get("body"))


def _is_missed_call(e: dict) -> bool:
    return not e.get("is_outgoing") and not e.get("is_answered")


def _is_group(e: dict) -> bool:
    return bool(e.get("is_group_chat") or _s(e.get("chat_guid"))
                or _s(e.get("contact_jid")).endswith("@g.us"))


def _thread_key(e: dict) -> str:
    """Stable per-conversation key for the agent cooldown: group id when present, else the 1:1
    identifier, else (source,rowid) so an unkeyable event still cools down against itself."""
    guid = _s(e.get("chat_guid"))
    if guid:
        return f"group:{guid}"
    src = _s(e.get("source"))
    if src == PROACTIVE_SOURCE:
        # Sotto's own nudges cool down per KIND: a chase and a birthday are not one conversation,
        # and a proactive event carries none of the identifier fields below — so without this every
        # one of them collapsed onto the single key "proactive:" and one cooled down all of them.
        return f"{src}:{_s(e.get('kind'))}"
    for k in ("handle", "contact_jid", "phone", "threadId"):
        v = _s(e.get(k))
        if v:
            return f"{src}:{v.lower()}"
    return f"{src}:{_s(e.get('rowid'))}"


def _resolve_sender(e: dict, lookup: dict) -> tuple[str, str]:
    """(display name, raw identifier) via the brief's own resolvers over the snapshot's contacts."""
    src = _s(e.get("source"))
    if src == "email":
        frm = _s(e.get("from"))
        addr = _sender_addr(frm)
        name = (lookup.get(addr) if addr else "") or _extract_sender_name(frm)
        return (name or addr or "Unknown"), addr
    if src == "whatsapp":
        jid = _s(e.get("sender_jid")) or _s(e.get("contact_jid"))
        return resolve_whatsapp_name(jid, _s(e.get("partner_name")), lookup), jid
    if src == "calls":
        phone = _s(e.get("phone"))
        return (resolve_call_name(phone, lookup) or ""), phone
    handle = _s(e.get("handle"))
    return resolve_imessage_name(handle, lookup), handle


def _is_known_name(name: str) -> bool:
    """Known = Contacts (or WhatsApp push name) resolved a real human name — same standard the
    brief's _thread_is_known_person applies to a 1:1 thread."""
    return bool(name) and name != "Unknown" and not _looks_like_phone_number(name)


def _graph_knows(name: str, ident: str) -> bool:
    """Does the knowledge graph have a person file for this sender? (best-effort)"""
    try:
        import knowledge  # noqa: PLC0415  (_shared/knowledge, already on sys.path)
        return bool(knowledge.find_person_file(name=name or "", identifier=ident or ""))
    except Exception:  # noqa: BLE001
        return False


def _is_vip(name: str, ident: str, rel_state: dict, prefs: dict | None = None) -> bool:
    """VIP (simple, documented — see module docstring): the user's STATED vip_people list first,
    then a top-of-queue attention_queue priority (>= VIP_PRIORITY_MIN), then a
    'family' mention in their graph file."""
    n = _s(name).strip().lower()
    if not n:
        return False
    try:
        if _prefs.is_vip(name, (prefs or {}).get("vip_people") or []):
            return True
    except Exception:  # noqa: BLE001 — a broken prefs file must never break triage
        pass
    vip_min = float(VIP_PRIORITY_MIN)
    for q in (rel_state.get("attention_queue") or []):
        if _s(q.get("display_name")).strip().lower() == n:
            try:
                if float(q.get("priority") or 0) >= vip_min:
                    return True
            except (TypeError, ValueError):
                pass
    try:
        import knowledge  # noqa: PLC0415
        path = knowledge.find_person_file(name=name, identifier=ident or "")
        if path:
            with open(path, encoding="utf-8") as f:
                if re.search(r"\bfamily\b", f.read(), re.I):
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _mentions_user(text: str) -> bool:
    """Group name-mention: the user's first name (SOTTO_USER_NAME), bare or @-prefixed. Unset →
    never a mention, so groups always queue (the conservative default)."""
    uname = (os.environ.get("SOTTO_USER_NAME") or "").strip()
    if not uname or not text:
        return False
    first = uname.split()[0]
    return bool(re.search(rf"@?\b{re.escape(first)}\b", text, re.I))


# ── Cooldown (one agent verdict per thread per window) ─────────────────────────────────────────────

def _cooldown_path() -> str:
    return os.path.join(_events_dir(), "cooldowns.json")


def _load_cooldowns() -> dict:
    try:
        with open(_cooldown_path(), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _cooldown_ok(key: str, now_ts: float) -> bool:
    window = _int_env("SOTTO_EVENT_COOLDOWN_MIN", 20) * 60
    last = _load_cooldowns().get(key)
    try:
        return last is None or (now_ts - float(last)) >= window
    except (TypeError, ValueError):
        return True


def _cooldown_applies(e: dict, cls: str) -> bool:
    """Does the per-thread cooldown ("one nudge per conversation per SOTTO_EVENT_COOLDOWN_MIN")
    govern this event? Not for an escalation (COOLDOWN_EXEMPT_CLASSES — the second channel inside
    the window is the whole signal), and not for a nudge Sotto planned: that is not a conversation,
    and two meetings needing prep inside one lead window are two nudges, not a repeat. What stops
    those repeating is the watcher's own once-per-day-per-key state."""
    return cls not in COOLDOWN_EXEMPT_CLASSES and _s(e.get("source")) != PROACTIVE_SOURCE


def _stamp_cooldown(key: str, now_ts: float) -> None:
    try:
        cd = {k: v for k, v in _load_cooldowns().items()
              if isinstance(v, (int, float)) and (now_ts - v) < COOLDOWN_PRUNE_SECS}
        cd[key] = now_ts
        os.makedirs(_events_dir(), exist_ok=True)
        tmp = _cooldown_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cd, f)
        os.replace(tmp, _cooldown_path())
    except OSError:
        pass


# ── Daily interrupt budget (cross-thread; the volume control cooldowns can't be) ───────────────────

def _budget_path() -> str:
    return os.path.join(_events_dir(), "budget.json")


def _local_day(now_local) -> str:
    return now_local.strftime("%Y-%m-%d")


def _budget_cap() -> int:
    return max(0, _int_env("SOTTO_NUDGE_BUDGET", 4))


def _budget_spent(day: str) -> int:
    """Agent verdicts already spent on this LOCAL date. A stamp from another date reads as 0 —
    that IS the day rollover (no cleanup job, no cron). Unreadable → 0 (fail open: a broken
    counter must not silence every nudge; the cooldown/valve budgets still bound the volume)."""
    try:
        with open(_budget_path(), encoding="utf-8") as f:
            st = json.load(f) or {}
        return max(0, int(st.get("count") or 0)) if _s(st.get("date")) == day else 0
    except Exception:  # noqa: BLE001
        return 0


def _budget_left(day: str) -> int:
    return max(0, _budget_cap() - _budget_spent(day))


def _budget_write(day: str, count: int) -> None:
    """The atomic write itself (tmp + os.replace). Callers hold _locked(_budget_path())."""
    os.makedirs(_events_dir(), exist_ok=True)
    tmp = _budget_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"date": day, "count": count}, f)
    os.replace(tmp, _budget_path())


def _budget_spend(day: str, n: int = 1) -> None:
    """Record n more agent verdicts against `day` — read-modify-write under _locked() so two
    concurrent producers can't both read the same count and write the same +1 (that race handed out
    a free nudge). Best-effort."""
    if n <= 0:
        return
    try:
        with _locked(_budget_path()):
            _budget_write(day, _budget_spent(day) + n)
    except OSError:
        pass


def _budget_try_spend(day: str, n: int = 1) -> bool:
    """CHECK AND SPEND AS ONE STEP: True when `n` units were still available and have now been
    spent. Reading _budget_left and then calling _budget_spend is two steps, and five producers run
    at once (Bridge push, Gmail poll, valve heartbeat, meeting tap, proactive cron) — N of them
    passing the gate together overshoot the cap by N−1, which is precisely the hole _locked exists
    to close. Fails OPEN on an unwritable volume (a broken counter must not silence every nudge)."""
    if n <= 0:
        return True
    try:
        with _locked(_budget_path()):
            if _budget_left(day) < n:
                return False
            _budget_write(day, _budget_spent(day) + n)
            return True
    except OSError:
        return True


# ── Queue ──────────────────────────────────────────────────────────────────────────────────────────

def _queue_path() -> str:
    return os.path.join(_events_dir(), "queue.jsonl")


def _append_queue(verdict_class: str, sender: str, event: dict, held_class: str = "") -> None:
    """One JSONL line per queued event. Bounded (rotate-keeping-tail) so the file can't grow forever
    on the /data volume. `held_class` (only on entries demoted FROM an agent verdict) preserves the
    class the event earned before cooldown/quiet/catchup demotion — the release valve and the
    sotto-event skill use it so a demoted scheduling_ask still gets the scheduling treatment when
    promoted. Best-effort — a failed append must never fail the triage."""
    try:
        from sotto_log import bounded_append  # noqa: PLC0415
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "verdict_class": verdict_class,
            "sender": sender,
            "event": event,
        }
        if held_class:
            entry["held_class"] = held_class
        # Same lock the valve's rewrite takes: an append that landed mid-rewrite would be dropped.
        with _locked(_queue_path()):
            bounded_append(_queue_path(), json.dumps(entry), QUEUE_MAX_BYTES, QUEUE_KEEP_LINES)
    except Exception:  # noqa: BLE001
        pass


# ── Surfaced ledger (one line per verdict — the "why didn't I get nudged?" substrate) ─────────────

def _surfaced_path() -> str:
    return os.path.join(_events_dir(), "surfaced.jsonl")


def _record_surfaced(verdict: str, cls: str, reason: str, sender: str, event: dict) -> None:
    """Append one {ts, sender, channel, verdict, reason, class} line to surfaced.jsonl at verdict
    time. ts uses the same ISO-Z format the dashboard's /api/ledger parses, so The Record renders
    these rows as-is. Best-effort — recording must never fail (or slow) the triage."""
    try:
        from sotto_log import bounded_append  # noqa: PLC0415
        ev = event if isinstance(event, dict) else {}
        line = json.dumps({
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sender": _s(sender) or _s(ev.get("from")) or _s(ev.get("handle"))
                      or _s(ev.get("contact_jid")) or _s(ev.get("phone")) or "",
            "channel": _s(ev.get("source")),
            "verdict": verdict,
            "reason": _s(reason)[:300],
            "class": cls,
        })
        bounded_append(_surfaced_path(), line, SURFACED_MAX_BYTES, SURFACED_KEEP_LINES)
    except Exception:  # noqa: BLE001
        pass


# ── Real-time escalation join (same person, 2+ channels, inside the window) ───────────────────────
# Reads the two retained event ledgers and answers ONE question: "has this exact person already
# reached the user on a DIFFERENT channel in the last N minutes?" Deliberately cheap (a bounded tail
# read of two small JSONL files) and deliberately literal (exact name / exact normalized identifier)
# — a fuzzy match here would manufacture the loudest nudge Sotto can send about the wrong person.

def _escalation_window_min() -> int:
    return max(1, ESCALATION_WINDOW_MIN_DEFAULT)


def _event_identifiers(e: dict) -> set:
    """Every normalized 1:1 identifier an event carries (phone → last 10 digits, email/JID →
    lowercase), by the SAME rule the knowledge graph and the brief normalize by. Group ids are
    excluded on purpose: a chat guid identifies a room, not a person."""
    out = set()
    if not isinstance(e, dict):
        return out
    for k in ("handle", "contact_jid", "sender_jid", "phone"):
        v = _s(e.get(k))
        if not v or v.endswith("@g.us"):
            continue
        n = _normalize_identifier(v)
        if n:
            out.add(n)
    addr = _sender_addr(_s(e.get("from")))
    if addr:
        out.add(_normalize_identifier(addr))
    return out


def _identity_key(name: str) -> str:
    """The name half of the match — only a REAL resolved human name counts (a raw phone number as
    a 'name' would join every unknown number under one identity)."""
    return _s(name).strip().lower() if _is_known_name(name) else ""


def _tail_lines(path: str, limit: int) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            return f.readlines()[-limit:]
    except OSError:
        return []


def _ledger_rows(now_utc: datetime, window_min: int) -> list:
    """Recent rows from BOTH event ledgers as {channel, name, idents, cls, age} (age in minutes).

    queue.jsonl carries the full event (identifiers, class, held_class) but ONLY queued verdicts;
    surfaced.jsonl carries one line per verdict — including the agent ones (a live missed call is
    retained nowhere else) — with the resolved sender and channel. Both are read; duplicates are
    harmless because the join only asks which CHANNELS a person used.

    NON_EVIDENCE_SOURCES / NON_EVIDENCE_CLASSES are dropped on both sides, by the same rule: Sotto's
    own output is never evidence that a person reached you, and neither is the user's own outbound."""
    rows = []
    for line in _tail_lines(_queue_path(), ESCALATION_SCAN_LINES):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        ev = entry.get("event") if isinstance(entry.get("event"), dict) else {}
        age = _event_age_min(ev, now_utc)
        if age is None:
            age = _row_age_min(_s(entry.get("ts")), now_utc)
        if age is None or age > window_min:
            continue
        channel = _s(ev.get("source"))
        cls = _s(entry.get("held_class")) or _s(entry.get("verdict_class"))
        if channel in NON_EVIDENCE_SOURCES or cls in NON_EVIDENCE_CLASSES:
            continue  # Sotto's own output (the tap, a proactive nudge) is never evidence that a
                      # person reached you; neither is the user's own outbound
        rows.append({"channel": channel, "name": _s(entry.get("sender")),
                     "idents": _event_identifiers(ev), "age": age, "cls": cls})
    for line in _tail_lines(_surfaced_path(), ESCALATION_SCAN_LINES):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict) or _s(entry.get("verdict")) == "drop":
            continue          # a dropped event (automated/muted/system) is not someone reaching you
        age = _row_age_min(_s(entry.get("ts")), now_utc)
        if age is None or age > window_min:
            continue
        # The surfaced row's `sender` is whatever triage resolved — a display name when Contacts
        # knew them, otherwise the raw address/handle it fell back to. Treat it as both: a name key
        # AND (when it looks like an address or a number) a normalized identifier.
        channel = _s(entry.get("channel"))
        cls = _s(entry.get("class"))
        if channel in NON_EVIDENCE_SOURCES or cls in NON_EVIDENCE_CLASSES:
            continue  # same rule as the queue side — and it matters most here: a surfaced
                      # meeting_prep row's `sender` is the raw calendar summary, which for a 1:1 is
                      # routinely the contact's exact name
        sender = _s(entry.get("sender"))
        ident_like = bool(sender) and ("@" in sender or _looks_like_phone_number(sender))
        rows.append({"channel": channel, "name": sender,
                     "idents": {_normalize_identifier(sender)} if ident_like else set(),
                     "age": age, "cls": cls})
    return rows


def _row_age_min(ts: str, now_utc: datetime):
    """Minutes since an ISO-Z ledger stamp, or None when it can't be dated (never join on a guess)."""
    parsed = _parse_ts(ts)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc - parsed).total_seconds() / 60.0)


def _escalation_verb(channel: str) -> str:
    return ESCALATION_VERBS.get(_s(channel), "messaged")


def _escalation_reason(name: str, channels: list, span_min: int) -> str:
    """"Sarah called AND emailed within 20 min" — name-carrying, so The Record and the nudge can
    both lead with the fact. Chronological: the prior channels first, this event's last."""
    verbs = [_escalation_verb(c) for c in channels]
    if len(verbs) == 1:
        phrase = verbs[0]
    else:
        phrase = ", ".join(verbs[:-1]) + f" AND {verbs[-1]}"
    return f"{name} {phrase} within {max(1, span_min)} min"


def _escalation_join(e: dict, name: str, cls: str, now_utc: datetime) -> str:
    """"" (no join) or the name-carrying reason for an escalation verdict. Conditions, all required:
    a KNOWN sender, at least one prior event from the SAME person (exact name or exact normalized
    identifier) on a DIFFERENT channel inside the window, and real evidence — a call on either side,
    or an ask-like Tier-1 class."""
    key = _identity_key(name)
    if not key:
        return ""
    channel = _s(e.get("source"))
    if not channel or channel in NON_EVIDENCE_SOURCES:
        return ""   # Sotto's own output is not evidence a person reached you — on EITHER side of
                    # the join: a nudge whose title happens to be someone's name must never be the
                    # second "channel" that manufactures an escalation about them.
    idents = _event_identifiers(e)
    window = _escalation_window_min()
    matched = {}          # channel → oldest age seen, for the chronological verb order
    ask = cls in ESCALATION_ASK_CLASSES or channel == "calls"
    for row in _ledger_rows(now_utc, window):
        if _identity_key(row["name"]) != key and not (idents & row["idents"]):
            continue
        if row["cls"] == ESCALATION_CLASS:
            # Already escalated for this person inside the window. ONE cooldown-and-budget-exempt
            # nudge per escalation, never a burst: the next three emails after "Sarah called AND
            # emailed" are the same event, and the exemptions that make the first one land are
            # exactly what would make the rest a storm.
            return ""
        row_channel = row["channel"]
        if not row_channel or row_channel == channel:
            continue      # same channel is a repeat, not an escalation (v1 skips call pairing)
        matched[row_channel] = max(matched.get(row_channel, 0.0), row["age"])
        if row_channel == "calls" or row["cls"] in ESCALATION_ASK_CLASSES:
            ask = True
    if not matched or not ask:
        return ""
    order = sorted(matched.items(), key=lambda kv: -kv[1])      # oldest first
    return _escalation_reason(name, [c for c, _ in order] + [channel],
                              int(round(max(matched.values()))))


# ── Tier 1 ─────────────────────────────────────────────────────────────────────────────────────────

def _sender_one_liner(name: str, ident: str, snapshot_local: dict, rel_state: dict) -> str:
    """One line of who-this-is for the Tier-1 prompt: name + their pulse status + their packed graph
    head line, whatever exists. Hard-capped so the prompt stays tiny."""
    bits = [name or ident or "unknown sender"]
    n = _s(name).strip().lower()
    for q in (rel_state.get("attention_queue") or []):
        if n and _s(q.get("display_name")).strip().lower() == n:
            bits.append(f"{_s(q.get('queue_type'))}: {_s(q.get('reason'))}")
            break
    pk = snapshot_local.get("person_knowledge")
    if n and isinstance(pk, dict):
        for packed in pk.values():
            head = _s(packed).split("\n", 1)[0]
            if head.lower().startswith(n):
                bits.append(head)
                break
    return " | ".join(b for b in bits if b)[:300]


def _classify_tier1(e: dict, one_liner: str) -> tuple[str, str, str]:
    """One Flash-Lite call → (verdict, class, reason). Raises on ANY problem; the caller maps every
    raise to queue (fail toward silence)."""
    model = os.environ.get("SOTTO_TRIAGE_MODEL", "gemini-3.5-flash-lite")
    key = os.environ.get("GOOGLE_AI_API_KEY") or ""
    if not key:
        raise RuntimeError("GOOGLE_AI_API_KEY not set")
    group_note = " (group chat — the user was mentioned by name)" if _is_group(e) else ""
    prompt = (
        'You are the triage layer of a personal chief-of-staff. Classify ONE inbound event.\n'
        'Respond with STRICT JSON only: {"class":"urgent|actionable|scheduling_ask|ambient|ignore","why":"<one short sentence>"}\n'
        "Definitions:\n"
        "- urgent: time-sensitive or a direct ask from someone who matters — worth interrupting the user now\n"
        "- actionable: a real ask/commitment, but it can wait for a nudge\n"
        '- scheduling_ask: a direct request to find time to meet or talk ("can we do coffee Thursday?",'
        ' "got 30 min next week?") — nudge-worthy; the agent will propose real slots from the calendar\n'
        "- ambient: FYI, social chatter, scheduling noise that asks nothing — batch it into a digest\n"
        "- ignore: automated or no-signal noise\n"
        f"Sender: {one_liner}\n"
        f"Channel: {_s(e.get('source'))}{group_note}\n"
        f"Subject: {_s(e.get('subject'))[:200]}\n"
        f"Event text:\n{_event_text(e)[:TIER1_TEXT_MAX]}\n"
    )
    raw = _gemini._gemini_once(model, key, prompt, label=" [triage]")
    m = re.search(r"\{.*\}", raw, re.S)   # peel any accidental fencing/prose
    obj = json.loads(m.group(0) if m else raw)
    cls = _s(obj.get("class")).strip().lower()
    why = _s(obj.get("why")).strip()[:200]
    if cls in ("urgent", "actionable", "scheduling_ask"):
        return "agent", cls, why or f"tier1 {cls}"
    if cls == "ambient":
        return "queue", "ambient", why or "tier1 ambient"
    if cls == "ignore":
        return "drop", "ignore", why or "tier1 ignore"
    raise RuntimeError(f"unexpected tier1 class {cls!r}")


# ── The post-meeting tap (a meeting that just ended, injected by the receiver's refresh thread) ────

def _meeting_attendee_names(e: dict) -> list:
    """Display names for the OTHER humans on the ended meeting, in calendar order. calcache already
    dropped the user and room resources; an attendee with no display name falls back to their
    address (better "sarah@acme.com wrapped" than an anonymous nudge)."""
    out = []
    for a in (e.get("attendees") or []):
        if not isinstance(a, dict):
            continue
        who = _s(a.get("name")) or _s(a.get("email"))
        if who:
            out.append(who)
    return out


def _meeting_with(names: list) -> str:
    """"Sarah Chen" · "Sarah Chen and Dhruv Patel" · "Sarah Chen and 3 others" — the human half of
    the nudge's first line, and of the surfaced row's reason."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{names[0]} and {len(names) - 1} others"


def _proactive_line(e: dict) -> str:
    """The ONE plain sentence a nudge Sotto planned carries into The Record, which renders it as
    "Nudged you — <line>" or "Held — <line> (<class>)". So it has to read as a sentence on its own,
    in the words a person would use about their own day — never the machinery's ("a nudge Sotto
    planned", never "a proactive item"), and never the kind's name, which is already the class.

    Everything it needs rides on the synthetic event (proactive_scan._proactive_event): the title,
    the one-line detail, and the person the nudge is about."""
    kind = _s(e.get("kind"))
    title = _s(e.get("text")).strip()
    detail = _s(e.get("detail")).strip()
    who = _s(e.get("person")).strip()
    # A loop's title leads with the person ("Sarah Chen — send the deck"); the sentences below name
    # her themselves, so they want the thing alone.
    what = title[len(who):].lstrip(" —") if who and title.lower().startswith(who.lower()) else title
    if kind == "meeting_prep":
        return f"{title} {detail}".strip()          # "Pitch starts in ~20 min · VC, Sam"
    if kind == "chase" and who and what:
        return f"still no word from {who} on {what}" + (f" ({detail})" if detail else "")
    if kind == "commitment" and who and what:
        return f"{what} for {who}" + (f" — {detail}" if detail else "")
    if kind in ("handoff", "retune_offer") and detail:
        return detail                               # the detail IS the whole sentence for these two
    if title and detail:
        return f"{title} — {detail}"
    return title or detail or "a nudge Sotto planned"


def _proactive_mute(e: dict, ctx: dict) -> str:
    """"" or the plain reason a nudge ABOUT A PERSON is dropped — the same two lists Tier 0 drops a
    message on (`mute_people`, `mute_senders`), through the same preferences.py. "Stop surfacing X"
    has to stick for the nudges Sotto raises itself or it doesn't mean anything."""
    person = _s(e.get("person")).strip()
    ident = _s(e.get("from")).strip()
    if person and any(person.lower() == _s(m).strip().lower()
                      for m in (ctx["prefs"].get("mute_people") or [])):
        return f"you asked Sotto to stop surfacing {person}"
    try:
        if ident and _prefs.sender_is_muted(ident, ctx["prefs"].get("mute_senders") or []):
            return f"you asked Sotto to stop surfacing {ident}"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _classify_proactive(e: dict, ctx: dict) -> tuple[str, str, str, str]:
    """Tier 0 for a nudge the proactive watcher planned (`source: "proactive"`). No Tier-1 call: the
    watcher already made the whole judgment deterministically — a meeting you haven't prepped, a
    commitment due today, an ask nobody answered. What the funnel decides is the CADENCE, and it
    decides it HERE so there is exactly one place that knows the order: the snooze, then quiet
    hours, then the mutes — the same order the calls branch and the post-meeting tap apply, and
    everything downstream of the verdict (the in-meeting hold, the daily budget, the queue and
    surfaced writes) is shared with every other event by construction.

    The nudge's KIND is its class, so The Record can say "a birthday" or "something you're owed"
    where a message would say "urgent". A cadence hold writes that kind into `ctx["held_class"]`,
    exactly as a quiet-hours missed call does, so the queue entry doesn't forget what it was.

    A nudge has no sender — its own sentence stands in, so every reason the gates downstream compose
    ("in a meeting until 2:30 PM — <sender>") still says what the nudge was about."""
    line = _proactive_line(e)
    who = line
    kind = _s(e.get("kind")) or "nudge"
    if ctx["snoozed"]:
        ctx["held_class"] = kind
        return "queue", "snoozed", f"{line} — you asked for quiet until {ctx['snooze_until']}", who
    if ctx["quiet"]:
        ctx["held_class"] = kind
        # The class already says "quiet hours" wherever this row is read, so the reason doesn't.
        return "queue", "quiet", line, who
    muted = _proactive_mute(e, ctx)
    if muted:
        return "drop", "muted", f"{line} — {muted}", who
    return "agent", kind, line, who


def _classify_meeting_end(e: dict, ctx: dict) -> tuple[str, str, str, str]:
    """Tier 0 for a `meeting_end` event. No Tier-1 call: the calendar already told us this was a
    real meeting with real people, which is the whole judgment. The cadence gates that live INSIDE
    classify_event for messages (snooze, then quiet hours) are applied here explicitly, exactly as
    the calls branch does — everything downstream of the verdict (meeting hold, cooldown, budget,
    staleness, queue + surfaced writes) is shared with every other event by construction.

    Reason shape is the one the Record's sentence composer wants: "your 2:00 PM with Sarah Chen
    wrapped" — it carries the name, so "Held — <reason>" reads as a sentence on its own."""
    names = _meeting_attendee_names(e)
    who = names[0] if names else ""
    when = _wall_clock(_s(e.get("start")))
    head = f"your {when}" if when else f"your {_s(e.get('summary')) or 'meeting'}"
    reason = f"{head} with {_meeting_with(names)} wrapped"
    if not names:
        # calcache never emits one of these (a solo block is not a meeting), but a hand-fed event
        # must not produce a nudge about nobody.
        return "drop", "post_meeting", "meeting with no other attendees — nothing to follow up", who
    if ctx["snoozed"]:
        return "queue", "snoozed", f"nudges snoozed until {ctx['snooze_until']} — {reason}", who
    if ctx["quiet"]:
        return "queue", "quiet", f"quiet hours — {reason}", who
    return "agent", "post_meeting", reason, who


# ── The per-event decision ─────────────────────────────────────────────────────────────────────────

def classify_event(e: dict, ctx: dict) -> tuple[str, str, str, str]:
    """(verdict, class, reason, sender_name) for ONE event, per the Tier-0 matrix + Tier 1.
    Cooldown is applied by the caller (it needs disk + a shared stamp across the batch).

    A Tier-0 CADENCE demotion (a non-VIP missed call in quiet hours) writes the class the event
    earned into `ctx["held_class"]`, which triage() resets per event and carries into the queue
    entry — the same preservation the later gates do. Without it the 7am promotion spends a budget
    unit the fresh verdict never would, and the bundle loses its missed-call phrasing."""
    src = _s(e.get("source"))
    # 0) A meeting that just ended — the post-meeting tap. First, because it is not a message from
    #    anyone and none of the sender machinery below applies to it.
    if src == MEETING_END_SOURCE:
        return _classify_meeting_end(e, ctx)
    # 0b) A nudge Sotto planned — the proactive watcher's tick, handed to the funnel in-process.
    #     Same reason it comes first: it is not a message from anyone.
    if src == PROACTIVE_SOURCE:
        return _classify_proactive(e, ctx)
    name, ident = _resolve_sender(e, ctx["lookup"])

    # 1) The user's own outbound — a loop-closing signal for deterministic resolution, never a nudge.
    if e.get("is_from_me"):
        return "queue", "signal", "own outbound message (ledger signal)", name

    # 2) Calls carry no text — decide entirely on direction + who.
    if src == "calls":
        if not _is_missed_call(e):
            return "drop", "call", "answered/outgoing call — nothing to do", name
        if _is_known_name(name):
            if ctx["snoozed"]:
                # An explicit snooze outranks the VIP carve-out quiet hours grants: the user asked
                # for silence by the clock, and the call still lands in the queue + the ledger.
                return "queue", "snoozed", f"nudges snoozed until {ctx['snooze_until']} — {name}", name
            if not ctx["quiet"] or _is_vip(name, ident, ctx["rel_state"], ctx["prefs"]):
                return "agent", "missed_call", f"missed call from {name}", name
            ctx["held_class"] = "missed_call"   # it was interrupt-worthy; only the hour held it
            return "queue", "quiet", f"missed call from {name} in quiet hours (non-VIP)", name
        return "queue", "missed_call", "missed call from unknown number", name

    text = _event_text(e)

    # 3) Automated noise — the same filters the brief applies before the LLM ever sees a thread.
    if src == "email":
        if _is_likely_automated(ident):
            return "drop", "automated", f"automated sender {ident}", name
    else:
        if _is_system_message(text):
            return "drop", "system", "system/status message", name
        if _looks_like_otp(text) or _is_shortcode(ident.split("@")[0]):
            return "drop", "automated", "OTP/shortcode blast", name

    # 4) Explicit mutes (the sotto-feedback channel) — "stop surfacing X" must stick here too.
    if src == "email" and ident:
        try:
            if _prefs.sender_is_muted(ident, ctx["prefs"]["mute_senders"]):
                return "drop", "muted", f"muted sender {ident}", name
        except Exception:  # noqa: BLE001
            pass
    if name and any(_s(name).strip().lower() == _s(m).strip().lower()
                    for m in ctx["prefs"]["mute_people"]):
        return "drop", "muted", f"muted person {name}", name

    # 5) Cadence: an active snooze queues everything that survives the drops — before Tier 1, so a
    #    snoozed hour costs nothing in LLM calls. Then quiet hours (VIP missed calls handled above).
    if ctx["snoozed"]:
        return "queue", "snoozed", f"nudges snoozed until {ctx['snooze_until']} — {name}", name
    if ctx["quiet"]:
        return "queue", "quiet", "quiet hours", name

    # 6) Groups only clear the bar when the user is called out by name.
    group = _is_group(e)
    if group and not _mentions_user(text):
        return "queue", "group", "group message without a name-mention", name

    # 7) Unknown non-VIP 1:1 never reaches the agent (VIP requires being known, by construction).
    known = (bool(ctx["lookup"].get(ident)) or _graph_knows(name, ident)) if src == "email" \
        else _is_known_name(name)
    if not known and not group:
        return "queue", "unknown", "unknown sender 1:1", name

    # 8) Nothing to judge (attachment-only etc.) — don't burn a Tier-1 call on empty text.
    if not text.strip():
        return "queue", "ambient", "no text content", name

    # 9) Survivor → Tier 1. ANY error → queue (fail toward silence, never toward noise).
    try:
        one_liner = _sender_one_liner(name, ident, ctx["snapshot"], ctx["rel_state"])
        verdict, cls, reason = _classify_tier1(e, one_liner)
        return verdict, cls, reason, name
    except Exception as err:  # noqa: BLE001
        return "queue", "ambient", f"tier1 error → queue: {err}", name


def _event_age_min(e: dict, now_utc: datetime):
    """Event age in minutes, or None when the timestamp is missing/unparseable (never gate on a
    guess). Naive timestamps are treated as UTC — the Bridge readers emit UTC wall-clock strings."""
    ts = _parse_ts(_s(e.get("timestamp")) or _s(e.get("date")))
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now_utc - ts).total_seconds() / 60.0)


def triage(payload: dict, now_local=None, now_utc=None) -> dict:
    """The whole funnel for one batch: Tier 0 → Tier 1 → cooldown → queue writes → verdict.
    `now_local`/`now_utc` are injectable for tests; production uses the configured timezone."""
    events = [e for e in (payload.get("events") or []) if isinstance(e, dict)]
    if now_local is None:
        now_local = _now_local(configured_tz() or "+00:00")
    ctx = {
        "quiet": _in_quiet_hours(now_local),
        "snapshot": _load_snapshot_local(),
        "rel_state": _load_relationship_state(),
        "prefs": _load_prefs(),
    }
    ctx["snooze_until"] = _s(ctx["prefs"].get("nudge_snooze_until"))
    ctx["snoozed"] = _snoozed(ctx["prefs"], now_local)
    ctx["lookup"] = build_contact_lookup(ctx["snapshot"].get("contacts")
                                         if isinstance(ctx["snapshot"].get("contacts"), list) else [])
    now_ts = time.time()
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    # Resolved ONCE per batch (one small file read, not one per event): "2:30 PM" while the user is
    # in a peopled meeting, "" otherwise. Quiet hours, but for rooms.
    ctx["meeting_until"] = _meeting_hold_until(now_local, now_utc)
    catchup = bool(payload.get("catchup"))
    max_age = EVENT_MAX_AGE_MIN
    day = _local_day(now_local)      # the interrupt budget's key — the user's day, not UTC's
    agent_events, reasons, queued = [], [], False
    bundle_charged = False           # ONE interrupt per bundle, not one per event (below)

    def _charge_once() -> bool:
        """May this bundle still nudge? A bundle is ONE message however many events it carries
        (event-triage/SKILL.md: "one message total, one interrupt spent"), so the first non-exempt
        agent verdict spends the day's unit — atomically — and the rest of the batch ride it free."""
        nonlocal bundle_charged
        if not bundle_charged:
            bundle_charged = _budget_try_spend(day, 1)
        return bundle_charged

    for e in events:
        ctx["held_class"] = ""        # per-event scratch: a Tier-0 cadence demotion writes it
        verdict, cls, reason, name = classify_event(e, ctx)
        held_class = _s(ctx.get("held_class"))
        age = _event_age_min(e, now_utc)
        # Real-time escalation join: the same known person on a SECOND channel inside the window
        # outranks whatever this event earned on its own (an "ambient" text minutes after their
        # missed call is not ambient). Checked HERE — after the classification, before every
        # downstream gate — so the escalation class carries its exemptions into the meeting hold,
        # the cooldown and the budget. Dropped events (automated/muted/system) and the cadence
        # holds in ESCALATION_SKIP_CLASSES never escalate — and neither does a BACKLOG event: a
        # catchup batch or a message older than the freshness bar is history, not someone reaching
        # you right now, and the loudest nudge Sotto sends must never fire on replayed events.
        if (verdict != "drop" and cls not in ESCALATION_SKIP_CLASSES
                and not catchup and (age is None or age <= max_age)):
            esc = _escalation_join(e, name, cls, now_utc)
            if esc:
                verdict, cls, reason = "agent", ESCALATION_CLASS, esc
        # Reconnect grace: after a Bridge gap (Mac asleep/off for hours) the backlog arrives in
        # bursts. A real-time nudge only makes sense for a real-time event — a stale message was
        # probably already handled on another device, so anything older than max_age (or any
        # message in an explicit catchup batch) goes to the queue and surfaces in the digest/next
        # brief instead of a barrage of nudges. Missed calls stay exempt: rare, high-signal, and
        # still worth surfacing hours later.
        if verdict == "agent" and cls != "missed_call":
            if catchup:
                held_class = cls
                verdict, cls, reason = "queue", "stale", f"catchup batch → queued ({name})"
            elif age is not None and age > max_age:
                held_class = cls
                verdict, cls, reason = "queue", "stale", f"{int(age)}m old → queued ({name})"
        if verdict == "agent":
            key = _thread_key(e)
            # In a meeting → hold. FIRST in the chain on purpose: a held ask must burn neither its
            # thread cooldown nor a unit of the day's budget, so when the meeting ends the valve
            # can promote it intact and spend the allowance then. Missed calls and escalations are
            # exempt — they are exactly what should reach the user mid-meeting.
            if ctx["meeting_until"] and cls not in MEETING_HOLD_EXEMPT_CLASSES:
                held_class = cls
                verdict, cls, reason = ("queue", "meeting_hold",
                                        f"in a meeting until {ctx['meeting_until']} — {name}")
            # The escalation join is exempt (COOLDOWN_EXEMPT_CLASSES): the second channel arriving
            # inside the window is the whole signal, and the FIRST message's stamp is exactly what
            # would swallow it.
            elif _cooldown_applies(e, cls) and not _cooldown_ok(key, now_ts):
                held_class = cls
                verdict, cls, reason = "queue", "cooldown", f"agent suppressed by cooldown ({key})"
            # Cross-thread daily cap: ten different senders in an hour is exactly what per-thread
            # cooldowns cannot stop. Checked AFTER the cooldown (a suppressed event never spends
            # budget) and BEFORE the cooldown stamp (a budget-held event doesn't burn its thread's
            # cooldown either — it may still promote via the valve tomorrow). The check IS the
            # spend, once per bundle: _charge_once.
            elif cls not in BUDGET_EXEMPT_CLASSES and not _charge_once():
                held_class = cls
                verdict, cls, reason = ("queue", "budget",
                                        f"daily interrupt budget spent ({_budget_cap()} nudges "
                                        f"today) — {name}")
            else:
                _stamp_cooldown(key, now_ts)
        if verdict == "queue":
            queued = True
            _append_queue(cls, name, e, held_class=held_class)
        elif verdict == "agent":
            agent_events.append({"event": e, "sender": name, "class": cls, "why": reason})
        _record_surfaced(verdict, cls, reason, name, e)   # the diagnostic ledger, every verdict
        reasons.append(reason)
    overall = "agent" if agent_events else ("queue" if queued else "drop")
    bundle = {}
    if agent_events:
        bundle = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "catchup": bool(payload.get("catchup")),
            "events": agent_events,
        }
    return {"verdict": overall,
            "reason": "; ".join(reasons[:5]) if reasons else "no events",
            "bundle": bundle}


# ── Release valve (the deferred queue's way back to a nudge) ──────────────────────────────────────

def _valve_candidate(entry: dict, now_utc: datetime, now_ts: float, max_age: int):
    """The promotable core of ONE queue entry, or None. THE single definition of "promotable", used
    by the valve's scan and by --promote so the two can never disagree: a demoted-agent class
    (PROMOTABLE_CLASSES), a KNOWN sender, a real event, an age inside the window ("meeting_hold"
    exempt — an ask Sotto itself held is never too old to deliver), and a clear per-thread cooldown
    (COOLDOWN_EXEMPT_CLASSES skips that, as in triage()). Budget accounting stays with the caller:
    the valve spends a running allowance across a tick, --promote spends exactly one."""
    if not isinstance(entry, dict):
        return None
    cls = _s(entry.get("verdict_class"))
    if cls not in PROMOTABLE_CLASSES:
        return None
    held = _s(entry.get("held_class")) or cls
    sender = _s(entry.get("sender"))
    if not _is_known_name(sender):
        return None
    ev = entry.get("event") if isinstance(entry.get("event"), dict) else None
    if not ev:
        return None
    if _s(ev.get("source")) == PROACTIVE_SOURCE:
        # A held proactive nudge is Sotto's own, and the event skill has no branch for one — a
        # promotion would ask it to draft a reply to "Your open-loops list is getting heavy". Held
        # proactive nudges ride the digest; the proactive lane's own cadence is their only way back.
        return None
    age = _event_age_min(ev, now_utc)
    if age is None:                  # no event ts → fall back to when it was queued
        qts = _parse_ts(_s(entry.get("ts")))
        if qts is None:
            return None              # unparseable both ways — never promote on a guess
        if qts.tzinfo is None:
            qts = qts.replace(tzinfo=timezone.utc)
        age = max(0.0, (now_utc - qts).total_seconds() / 60.0)
    if age > max_age and cls != "meeting_hold":
        return None
    key = _thread_key(ev)
    if held not in COOLDOWN_EXEMPT_CLASSES and not _cooldown_ok(key, now_ts):
        return None
    return {"event": ev, "sender": sender, "cls": cls, "held": held, "age": age,
            "thread_key": key, "exempt": held in BUDGET_EXEMPT_CLASSES}


def _valve_state_path() -> str:
    return os.path.join(_events_dir(), "valve_state.json")


def _valve_recent(now_ts: float) -> list:
    """Promotion timestamps within the last hour (the budget window). Unreadable → empty."""
    try:
        with open(_valve_state_path(), encoding="utf-8") as f:
            ts_list = (json.load(f) or {}).get("promotions") or []
        return [t for t in ts_list if isinstance(t, (int, float)) and (now_ts - t) < 3600]
    except Exception:  # noqa: BLE001
        return []


def _valve_record(recent: list, n: int, now_ts: float) -> None:
    try:
        os.makedirs(_events_dir(), exist_ok=True)
        tmp = _valve_state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"promotions": recent + [now_ts] * n}, f)
        os.replace(tmp, _valve_state_path())
    except OSError:
        pass


def release_valve(now_local=None, now_utc=None, now_ts=None) -> dict:
    """Promote up to VALVE_MAX_PER_TICK deferred queue entries back into the agent path (module
    docstring has the full contract). Returns the same {"verdict","reason","bundle"} shape triage()
    does so the receiver stages + spawns identically. Eligibility, in order:
      class ∈ PROMOTABLE_CLASSES (demoted-agent verdicts only — never drop-class/born-ambient) →
      KNOWN sender (the resolved name triage wrote) → younger than VALVE_MAX_AGE_MIN (which
      "meeting_hold" entries skip — an ask Sotto itself held is never too old to deliver) →
      per-thread cooldown clear at promotion time (stamped on promotion, so two queue entries from
      one thread can't both promote in a tick; COOLDOWN_EXEMPT_CLASSES — a held escalation — skips
      that check, the same exemption triage() applies) → the day's interrupt budget still has room
      (exempt held classes — missed calls, escalations — skip that check and don't spend it).
    Holds: quiet hours, an active nudge snooze, and being IN A MEETING → no promotion at all. The
    meeting hold closes the loop with "meeting_hold" ∈ PROMOTABLE_CLASSES: while the meeting runs
    the valve stays shut (it must not push OTHER queued asks into the room either), and its next
    tick after the meeting ends is the release path — no new machinery, no meeting-end watcher.
    Budget: VALVE_MAX_PER_HOUR (2) across ticks, AND the shared cross-thread daily
    budget (SOTTO_NUDGE_BUDGET) — a promotion is a nudge, so it spends the same allowance a fresh
    agent verdict does and can never push the day past its cap. A tick's promotions land in ONE
    bundle, so like a fresh batch they cost ONE unit between them (spent atomically at the first
    non-exempt promotion). SOTTO_VALVE=0 disables entirely.
    Channel health is the CALLER's gate (the receiver's positive WhatsApp probe) — this function
    only decides what deserves promotion."""
    if (os.environ.get("SOTTO_VALVE", "").strip() or "1") == "0":
        return {"verdict": "drop", "reason": "valve disabled (SOTTO_VALVE=0)", "bundle": {}}
    if now_local is None:
        now_local = _now_local(configured_tz() or "+00:00")
    if _in_quiet_hours(now_local):
        return {"verdict": "drop", "reason": "quiet hours hold — nothing promoted", "bundle": {}}
    prefs = _load_prefs()
    if _snoozed(prefs, now_local):
        return {"verdict": "drop", "bundle": {},
                "reason": f"nudges snoozed until {_s(prefs.get('nudge_snooze_until'))} — "
                          "nothing promoted"}
    now_ts = time.time() if now_ts is None else now_ts
    now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc
    meeting_until = _meeting_hold_until(now_local, now_utc)
    if meeting_until:
        return {"verdict": "drop", "bundle": {},
                "reason": f"in a meeting until {meeting_until} — nothing promoted"}
    recent = _valve_recent(now_ts)
    budget = max(0, VALVE_MAX_PER_HOUR - len(recent))
    cap = min(VALVE_MAX_PER_TICK, budget)
    if cap <= 0:
        return {"verdict": "drop", "reason": "valve budget spent this hour", "bundle": {}}
    # One lock for the whole read → scan → rewrite: the queue is a read-modify-write here, and a
    # producer appending mid-scan would otherwise be erased by the rewrite (_append_queue takes the
    # same lock). Everything inside is local computation and small atomic writes — nothing blocking.
    with _locked(_queue_path()):
        try:
            with open(_queue_path(), encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            lines = []
        max_age = VALVE_MAX_AGE_MIN
        day = _local_day(now_local)
        # A tick promotes up to VALVE_MAX_PER_TICK events into ONE bundle — one message, so one
        # interrupt. The first non-exempt promotion spends the day's unit (atomically); the second
        # rides it free. `budget_snapshot` is diagnostic only — the real check is the spend.
        budget_snapshot = _budget_left(day)
        promoted, promoted_idx, charged, budget_blocked = [], set(), False, False
        for i, line in enumerate(lines):     # oldest first — FIFO fairness for the longest-held ask
            if len(promoted) >= cap:
                break
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            # PROMOTABLE_CLASSES / known sender / age window (meeting_hold exempt: an ask Sotto
            # itself held is never too old to deliver) / per-thread cooldown — all of it in
            # _valve_candidate, which --promote applies to the one entry the user picked.
            cand = _valve_candidate(entry, now_utc, now_ts, max_age)
            if cand is None:
                # The one check the shared helper can't make: budget is a running allowance here.
                if (_s(entry.get("verdict_class")) in PROMOTABLE_CLASSES and not charged
                        and budget_snapshot <= 0
                        and (_s(entry.get("held_class")) or _s(entry.get("verdict_class")))
                        not in BUDGET_EXEMPT_CLASSES):
                    budget_blocked = True
                continue
            if not cand["exempt"] and not charged:
                if not _budget_try_spend(day, 1):
                    budget_blocked = True     # keep scanning: an exempt entry may still deserve a nudge
                    continue
                charged = True
            _stamp_cooldown(cand["thread_key"], now_ts)
            reason = f"held: {cand['cls']}, {int(cand['age'])}m old"
            promoted.append({"event": cand["event"], "sender": cand["sender"],
                             "class": cand["held"],
                             "why": f"promoted from the deferred queue ({reason})"})
            promoted_idx.add(i)
            _record_surfaced("promoted", cand["held"], reason, cand["sender"], cand["event"])
        if not promoted:
            return {"verdict": "drop", "bundle": {},
                    "reason": (f"daily interrupt budget spent ({_budget_cap()} nudges today) — "
                               "nothing promoted") if budget_blocked
                              else "nothing promotable in the queue"}
        try:                                 # promoted entries leave the queue (atomic rewrite)
            rest = [ln for i, ln in enumerate(lines) if i not in promoted_idx]
            tmp = _queue_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(rest)
            os.replace(tmp, _queue_path())
        except OSError:
            pass
    _valve_record(recent, len(promoted), now_ts)
    bundle = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "catchup": False,
        "promoted": True,
        "events": promoted,
    }
    return {"verdict": "agent",
            "reason": "; ".join(p["why"] for p in promoted),
            "bundle": bundle}


def promote_one(key: str, now_local=None, now_utc=None, now_ts=None) -> dict:
    """Promote the ONE queued entry whose queue_key matches — the dashboard's "nudge me now".

    Same mechanics as the valve, one entry instead of a tick: _valve_candidate decides
    promotability, the day's interrupt budget is spent, the per-thread cooldown is stamped, the
    entry leaves the queue in the same atomic rewrite under the same lock, surfaced.jsonl gets the
    same "promoted" row, and the returned bundle is the shape the receiver already stages + spawns.
    Two gates differ, both on purpose: the CLOCK holds (quiet hours, the nudge snooze) do not apply
    — they stop unprompted interruptions, and the user prompted this one — and neither does the
    valve's ≤2/hour pacing, which governs how fast Sotto lets itself talk, not how fast you may ask.
    The in-meeting hold DOES apply (it is about the room you are in, not the hour) and so does the
    budget: the day's cap is the day's cap, however it is spent.

    Returns {"ok": True, "verdict": "agent", "reason", "bundle"} or {"ok": False, "error": <code>,
    "reason": <one human sentence>} with error ∈ not_found | not_promotable | meeting_hold | budget.
    """
    if now_local is None:
        now_local = _now_local(configured_tz() or "+00:00")
    now_ts = time.time() if now_ts is None else now_ts
    now_utc = datetime.now(timezone.utc) if now_utc is None else now_utc
    key = _s(key).strip()
    if not key:
        return {"ok": False, "error": "not_found", "reason": "no queue entry named"}
    meeting_until = _meeting_hold_until(now_local, now_utc)
    if meeting_until:
        return {"ok": False, "error": "meeting_hold",
                "reason": f"in a meeting until {meeting_until}"}
    day = _local_day(now_local)
    max_age = VALVE_MAX_AGE_MIN
    with _locked(_queue_path()):
        try:
            with open(_queue_path(), encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            lines = []
        hit = next(((i, ln) for i, ln in enumerate(lines)
                    if ln.strip() and queue_key(ln) == key), None)
        if hit is None:
            return {"ok": False, "error": "not_found",
                    "reason": "that item is no longer in the queue"}
        idx, line = hit
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            entry = None
        cand = _valve_candidate(entry or {}, now_utc, now_ts, max_age)
        if cand is None:
            return {"ok": False, "error": "not_promotable",
                    "reason": "that item can't be promoted (held for a reason the valve never "
                              "releases, an unknown sender, or a cooldown still running)"}
        # Check and spend in one atomic step — a promotion IS a nudge, and the dashboard button can
        # race the valve heartbeat and a Bridge push for the day's last unit.
        if not cand["exempt"] and not _budget_try_spend(day, 1):
            return {"ok": False, "error": "budget",
                    "reason": f"the day's interrupt budget is spent ({_budget_cap()} nudges today)"}
        _stamp_cooldown(cand["thread_key"], now_ts)
        reason = "user promoted from dashboard"
        promoted = [{"event": cand["event"], "sender": cand["sender"], "class": cand["held"],
                     "why": f"{reason} (held: {cand['cls']}, {int(cand['age'])}m old)"}]
        try:                                 # the promoted entry leaves the queue (atomic rewrite)
            rest = [ln for i, ln in enumerate(lines) if i != idx]
            tmp = _queue_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(rest)
            os.replace(tmp, _queue_path())
        except OSError:
            pass
        _record_surfaced("promoted", cand["held"], reason, cand["sender"], cand["event"])
    return {"ok": True, "verdict": "agent", "reason": promoted[0]["why"],
            "bundle": {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "catchup": False, "promoted": True, "events": promoted}}


def main():
    argv = sys.argv[1:]
    if "--promote" in argv:
        i = argv.index("--promote")
        key = argv[i + 1] if i + 1 < len(argv) else ""
        try:
            out = promote_one(key)
        except Exception as e:  # noqa: BLE001 — the receiver parses stdout; never die JSON-less
            out = {"ok": False, "error": "failed", "reason": f"promotion failed: {e}"}
        print(json.dumps(out))
        return
    if "--valve" in sys.argv[1:]:
        try:
            out = release_valve()
        except Exception as e:  # noqa: BLE001 — fail toward silence, same posture as triage
            out = {"verdict": "drop", "reason": f"valve error → no promotion: {e}", "bundle": {}}
        try:
            from sotto_log import diag  # noqa: PLC0415
            diag(f"[triage_event --valve] {out['verdict']}: {_s(out.get('reason'))[:200]}")
        except Exception:  # noqa: BLE001
            pass
        print(json.dumps(out))
        return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
    except Exception:  # noqa: BLE001
        print(json.dumps({"verdict": "drop", "reason": "unparseable input", "bundle": {}}))
        return
    try:
        out = triage(payload)
    except Exception as e:  # noqa: BLE001
        # Fail toward silence: preserve the raw events in the queue and answer "queue", never crash
        # the receiver's synchronous call or nudge the user off a broken pipeline.
        for ev in (payload.get("events") or []):
            if isinstance(ev, dict):
                _append_queue("error", "", ev)
        out = {"verdict": "queue", "reason": f"triage error → queue: {e}", "bundle": {}}
    try:
        from sotto_log import diag  # noqa: PLC0415
        diag(f"[triage_event] {len(payload.get('events') or [])} event(s) → "
             f"{out['verdict']}: {_s(out.get('reason'))[:200]}")
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(out))


if __name__ == "__main__":
    main()
