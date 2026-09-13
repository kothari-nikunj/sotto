#!/usr/bin/env python3
"""
proactive_scan.py — decide what (if anything) Sotto should proactively nudge about RIGHT NOW.

This is the deterministic core of the `sotto-proactive` skill (a ~15-min cron). It is intentionally
conservative and the decision — what is DUE (lead times, the chase clock, once-per-item dedup) —
lives here, testable, so the agent only DRAFTS and DELIVERS what this returns. Whether any of it
reaches the user is the event funnel's call, below. PRINCIPLE: auto-draft, never auto-send; a nudge
surfaces a ready draft, it never sends on the user's behalf.

Seven nudge kinds:
  - intention     — a one-shot plain-language recipe whose due time has arrived
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
                    two this item gets. Held back only when today's DELIVERED brief already named
                    that same loop (`_brief_named_keys`) — the one collision that is a double-tell.
  - birthday      — a saved contact whose birthday is today, or a VIP/VVIP birthday `SOTTO_BIRTHDAY_LEAD_DAYS` out
                    (the lead nudge is the one that can still become a gift). The day-of nudge is
                    skipped entirely once a brief has DELIVERED today: that brief carried the 🎂
                    line and the quick-wish tap, so this would be the same nudge twice.
  - handoff       — one thing you're owed has been chased its two times and still has no answer, so
                    Sotto stops guessing and asks: "I've nudged Maya twice about the contract —
                    nudge her again, or let it go?" Person, thing, binary choice. It has its OWN
                    clock — asked the first tick it comes due, outside the 2h post-brief window —
                    not the tidy-up cooldown below, and it ignores that offer's pile threshold; it
                    never hides inside the generic offer — "your open-loops list is getting heavy"
                    tells a first-time user nothing about Maya. ASKED ONCE: the delivery is stamped on the row
                    (`--finalize-handoff`), which both retires the question and ends that loop's
                    claim on a named line in every brief — it is the user's move now.
  - retune_offer  — the stale pile is getting heavy (you keep seeing items you don't act on); offer
                    a quick cleanup. Throttled to once per cooldown window, NOT daily — and
                    suppressed within 2h of a delivered brief (Sprint 0 §6): the brief just told the
                    user how many loops are open and where to see them, so an immediate "your list
                    is heavy" is that same sentence a second time.

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
from calendar_context import human_attendees, meeting_events  # noqa: E402
from timeutil import _now_local, _parse_ts, configured_tz, configured_user_email  # noqa: E402
import delivery_effects  # noqa: E402
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
    import jsonstore
    with jsonstore.lock(_state_path(date)):
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
    with delivery_effects.proactive_state(date) as state:
        state['nudged'] = sorted(nudged)


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


def _brief_named_keys(now_local: datetime):
    """The anchor_keys today's DELIVERED brief(s) actually NAMED — a set — or None when a brief
    delivered without leaving that record (an older brief, a failed write), which is the caller's
    signal to fall back to the blunt time window.

    compose_brief writes `<date>.<kind>.named.json` beside brief_marker's `<date>.<kind>.delivered`
    claim; only a kind with BOTH is read, so a brief that composed and never sent silences nothing.
    No delivered brief today = an empty set: nothing was said, so nothing can be said twice."""
    briefs = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "briefs")
    date = now_local.strftime("%Y-%m-%d")
    keys: set = set()
    for kind in ("morning", "evening"):
        if not os.path.exists(os.path.join(briefs, f"{date}.{kind}.delivered")):
            continue
        try:
            with open(os.path.join(briefs, f"{date}.{kind}.named.json"), encoding="utf-8") as f:
                keys |= {_s(k) for k in (json.load(f) or {}).get("anchor_keys") or []}
        except Exception:  # noqa: BLE001
            return None      # it delivered but we cannot tell what it said — use the time guard
    return keys


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
    delivery_effects.stamp_retune(today_str)


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
    return {**{k: n[k] for k in ('anchor_key', 'intention_id', 'calendar_event_id',
                                'calendar_start', 'calendar_observed_at', 'valid_until', 'thread_id') if k in n},
            "source": "proactive", "kind": _s(n.get("kind")), "key": _s(n.get("key")),
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


def _submit(te, fresh: list, now_local: datetime) -> dict:
    """Hand this tick's nudges to the funnel as ONE bundle and return key → decision_id for what it decided to
    deliver; everything it decided otherwise it has already queued or dropped, with a reason, in
    the same ledgers every event writes to.

    The bundle is the unit on purpose: triage() charges the daily interrupt budget once per bundle,
    which IS this lane's "one unit per delivered push" rule — the tick's nudges go out as a single
    message (the skill's own rule), so they cost one unit between them."""
    if not fresh:
        return {}
    out = te.triage({"events": [_proactive_event(n) for n in fresh]}, now_local=now_local)
    return {_s((ev.get("event") or {}).get("key")): _s(ev.get("decision_id"))
            for ev in ((out.get("bundle") or {}).get("events") or [])}


def _defer_delivery_effects(fired: list, date=None, result=None) -> bool:
    """Detached receiver runs learn whether the host send succeeded only after this process exits.
    Leave the small correlated receipt it needs; interactive runs return False and keep the historical
    immediate-finalize behavior because their response is the delivery surface."""
    run_id = delivery_effects.run_id()
    if not run_id:
        return False
    date = date or _now_local(configured_tz() or '+00:00').strftime('%Y-%m-%d')
    captured = result.get('_eligibility') if isinstance(result, dict) else None
    effects = list(captured if captured is not None else
                   delivery_effects.for_bundle({'events': [_proactive_event(n) for n in fired]})['effects'])
    for n in fired:
        effects.append({'kind': 'proactive_seen', 'date': date, 'key': n['key'], 'run_id': run_id})
        if n.get('kind') in _FINALIZE_FLAG and n.get('anchor_key'):
            effects.append({'kind': n['kind'], 'anchor_key': n['anchor_key']})
        if n.get('intention_id'):
            effects.append({'kind': 'intention', 'id': n['intention_id']})
        if n.get('kind') == 'retune_offer':
            effects.append({'kind': 'retune_offer', 'date': date})
    return delivery_effects.stage(effects, [n['decision_id'] for n in fired if n.get('decision_id')],
                                  result=result)


# Phase two, per lane: what continuity_resolve records once the nudge has ACTUALLY gone out.
# A chase counts (`chased_count += 1`, `last_chased_at = today`); a hand-off marks that the user has
# been asked (`handoff_asked_at`), which is what stops the question repeating and stops the loop
# holding a named line in every brief. Both are stamped on DELIVERY, never on intent.
_FINALIZE_FLAG = {"chase": "--finalize-chase", "handoff": "--finalize-handoff"}


def _finalize(kind: str, anchor_key: str) -> None:
    """Hand the delivery back to continuity_resolve — shelled, not imported, so it stays the
    ledger's ONE writer. Best-effort: an unfinalized chase simply expires at day end, uncounted, and
    an unfinalized hand-off is asked again next cycle."""
    key, flag = _s(anchor_key).strip(), _FINALIZE_FLAG.get(kind)
    if not (key and flag):
        return
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                          "morning-brief", "scripts", "continuity_resolve.py")
    try:
        subprocess.run([sys.executable, os.path.abspath(script), flag, key],
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


def _prep_lines(attendee: dict, continuity) -> tuple:
    """(who, open_loop) for the first external attendee of an imminent meeting — the prep the nudge
    carries. `who` is the graph's typed title/company ("VP Eng at Acme"), never a guess; empty when
    the graph has no file. `open_loop` is the one dated loop you owe them from the scan's own
    continuity input, else "". Both degrade to "" on any failure — a nudge without prep beats no
    nudge."""
    email = _s(attendee.get("email")).lower().strip()
    name = _s(attendee.get("displayName")).strip()
    who = ""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                        "_shared", "knowledge"))
        import knowledge as kg  # noqa: PLC0415
        path = kg.find_person_file(name=name, identifier=email) if (name or email) else None
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                p = kg.parse_person_file(f.read())
            bits = [b for b in (_s(p.title), _s(p.company)) if b]
            who = " at ".join(bits) if len(bits) == 2 else (bits[0] if bits else "")
    except Exception:  # noqa: BLE001
        who = ""
    loop = ""
    try:
        rows = continuity if isinstance(continuity, list) else _arr(continuity, "items")
        for c in rows:
            if not isinstance(c, dict):
                continue
            ident = _s(c.get("identifier")).lower().strip()
            if (email and ident == email) or (name and _s(c.get("name")).strip().lower() == name.lower()):
                loop = _s(c.get("title")).split(" — ", 1)[-1][:80]
                break
    except Exception:  # noqa: BLE001
        loop = ""
    return who, loop


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
                    "anchor_key": _s(it.get("anchor_key")),
                    "deadline": _s(it.get("deadline")),
                    "channel": _s(it.get("channel")),
                    "identifier": _s(it.get("identifier")),
                    "thread_id": _s(it.get("thread_id"))})
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
                    "chased_count": chased,
                    "detail": ("overdue" if it.get("overdue") else f"asked {age} days ago"
                               if age else "still open")
                              + (" · chased once already" if chased >= 1 else ""),
                    "channel": _s(it.get("channel")), "identifier": _s(it.get("identifier")),
                    "thread_id": _s(it.get("thread_id"))})
    return out


def _handoff_candidates() -> list:
    """The waiting-ons that have been chased their two times, still have no answer, and have NOT yet
    been handed back — the ones the ledger owes the user a question about. They are NOT a heavy-pile
    problem, so they never surface as the generic tidy-up offer: each gets its own plain, named
    question, because "your open-loops list is getting heavy" tells a first-time user nothing about
    Maya and the contract.

    ASKED ONCE, NOT EVERY CYCLE. `handoff_asked_at` is stamped when the question was DELIVERED, and
    a row carrying it is gone from this list for good — until the user answers (resolve / drop /
    keep waiting, each of which clears the chase state). Without it the same question re-fired every
    cooldown window with nothing on the row remembering it had ever been asked."""
    try:
        import loops_query as lq  # noqa: PLC0415  (_shared/scripts is on sys.path)
        data = lq.query()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in (data.get("waiting_on_them") or []):
        if not isinstance(it, dict) or not it.get("chased_out") or it.get("handoff_asked_at"):
            continue
        name = _s(it.get("name")) or "them"
        what = _s(it.get("what"))
        out.append({"id": f"handoff:{name}:{what[:40]}",
                    "title": f"{name} — {what}",
                    "name": name,
                    "anchor_key": _s(it.get("anchor_key")),
                    # The whole message, written here so it is the same sentence every time:
                    # person + thing + a binary choice, and not one word of Sotto's vocabulary.
                    "question": (f"I've nudged {name} twice about {what.lower() or 'this'} — "
                                 "nudge them again, or let it go?"),
                    "channel": _s(it.get("channel")),
                    "identifier": _s(it.get("identifier"))})
    return out


def _all_open_anchor_keys() -> set[str] | None:
    """Every live loop anchor, both directions; conditional recipes may track either direction."""
    try:
        import loops_query as lq  # noqa: PLC0415
        data = lq.query()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return {_s(item.get("anchor_key"))
            for lane in (data.get("you_owe") or [], data.get("waiting_on_them") or [])
            for item in lane if isinstance(item, dict) and _s(item.get("anchor_key"))}


def _intention_candidates(now_local: datetime, continuity=None) -> list:
    """Due recipes, auto-canceling conditional ones whose tracked open loop already closed.

    `continuity` is an explicit test/caller override. Production reads both ledger directions:
    "if Sarah hasn't replied" tracks waiting_on_them, which the due-commitment view omits on
    purpose.
    """
    try:
        import schedule_wakeup  # noqa: PLC0415
        if continuity is None:
            open_anchors = _all_open_anchor_keys()
        else:
            open_anchors = {_s(item.get("anchor_key")) for item in
                            (continuity if isinstance(continuity, list)
                             else _arr(continuity, "items"))
                            if isinstance(item, dict) and _s(item.get("anchor_key"))}
        out = []
        for item in schedule_wakeup.due(now_local):
            anchor = _s(item.get("anchor_key"))
            if anchor and open_anchors is None:
                continue  # ledger unavailable: retry later; never guess that the condition passed
            if anchor and anchor not in open_anchors:
                schedule_wakeup.transition(item["id"], "canceled")
            else:
                out.append(item)
        return out
    except Exception:  # noqa: BLE001
        return []


def _finish_intention(ident: str) -> None:
    try:
        import schedule_wakeup  # noqa: PLC0415
        schedule_wakeup.transition(ident, "fired")
    except Exception:  # noqa: BLE001
        pass


def _stale_loop_count() -> int:
    """Reuse retune_scan's exact stale definition (overdue / 3–7d / repeat-surfaced) so the offer
    triggers on the same pile the cleanup would act on. Best-effort; 0 on any error."""
    try:
        import retune_scan  # noqa: PLC0415  (sibling in _shared/scripts, already on sys.path)
        return int(retune_scan.scan().get("counts", {}).get("stale", 0))
    except Exception:
        return 0


def _birthday_importance(local, now):
    from relationship_importance import for_contact  # noqa: PLC0415
    from collections import Counter  # noqa: PLC0415
    root = os.environ.get("SOTTO_DATA", "/data")
    def read(relative):
        try:
            with open(os.path.join(root, relative), encoding="utf-8") as stream:
                value = json.load(stream)
                return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    history = read("knowledge/relationship_state.json").get("history", {})
    vips = read("preferences.json").get("explicit", {}).get("vip_people", [])
    contacts = _arr(local, "contacts")
    names = Counter(_s(ct.get("name")).strip().casefold() for ct in contacts)
    # Contacts can contain two cards for one canonical person (for example a phone-only card and
    # an email card). Preserve every explicit-VIP alias inside that identity, while keeping two
    # genuinely different canonical people separate even when their display names match.
    by_cid = {}
    for ct in contacts:
        cid = _s(ct.get("canonical_id")).strip()
        name = _s(ct.get("name")).strip().casefold()
        if cid and name:
            by_cid.setdefault(cid, []).append(name)
    result = {}
    for ct in contacts:
        name = _s(ct.get("name")).strip().casefold()
        cid = _s(ct.get("canonical_id")).strip()
        if not name or (not cid and names[name] != 1):
            continue
        importance = for_contact(ct, history, vips, now, aliases=by_cid.get(cid, ()))
        if cid:
            result[f"canonical:{cid}"] = importance
        if names[name] == 1:
            result[name] = importance
    return result


def scan(calendar, continuity, local, user_email, now_local,
         stale_count: int = 0, retune_offer_allowed: bool = False,
         prepped_emails=None, brief_recent: bool = False,
         chase_candidates=None, brief_today: bool = False, handoff_candidates=None,
         brief_named=None, intentions=None, handoff_allowed: bool = False,
         birthday_importance=None) -> dict:
    """Pure decision (no I/O, no gates): given the inputs and the local 'now', return every nudge
    that is DUE now. Whether any of them reaches the user — the snooze, quiet hours, the mutes, the
    in-meeting hold, the daily interrupt budget — is the funnel's call, made in one place, on the
    bundle main hands it. `stale_count` / `retune_offer_allowed` / `prepped_emails` /
    `brief_recent` / `brief_today` / `brief_named` / `chase_candidates` / `handoff_candidates` are
    computed by main (they need disk: the ledger, a cooldown marker, today's research cache, the
    delivered markers and what the delivered brief named).
    """
    lead = PROACTIVE_LEAD_MIN
    user_email = (user_email or "").lower()
    user_domain = user_email.split("@")[1] if "@" in user_email else ""
    today = now_local.strftime("%Y-%m-%d")
    nudges = []

    # 0) One-shot intentions — explicitly scheduled by the user or a source-backed follow-up. The
    # heartbeat supplies timing; the ordinary funnel still owns whether this tick may interrupt.
    for item in (intentions or []):
        if not isinstance(item, dict) or not _s(item.get("id")) or not _s(item.get("action")):
            continue
        nudges.append({"kind": "intention", "key": f"intention:{_s(item.get('id'))}",
                       "intention_id": _s(item.get("id")), "title": _s(item.get("action")),
                       **({'anchor_key': _s(item['anchor_key'])} if item.get('anchor_key') else {}),
                       "detail": _s(item.get("context"))})

    # 1) Meeting prep — external meeting starting within the lead window (and not already started).
    if isinstance(calendar, dict):
        events = calendar.get("events") or calendar.get("items") or []
    else:
        events = calendar if isinstance(calendar, list) else []
    for e in meeting_events(events):
        if not isinstance(e, dict) or _s(e.get("my_response")).lower() == "declined":
            continue                  # a meeting you declined is not a room you're walking into
        st = _parse_ts(_s(e.get("start")))
        if st is None:
            continue
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        mins_away = (st.astimezone(timezone.utc) - now_local.astimezone(timezone.utc)).total_seconds() / 60.0
        if not (0 <= mins_away <= lead):
            continue
        ext = [a for a in human_attendees(e, user_email)
               if _s(a.get('status')).lower() != 'declined'
               and not (user_domain and a['email'].endswith("@" + user_domain))]
        if not ext:
            continue  # internal/solo meeting — no prep nudge
        # "…that you haven't prepped" — the honest, deterministic signal for that is today's
        # research cache: an external attendee only lands in it because a meeting-prep or brief run
        # TODAY researched a meeting they're on. Any hit means this meeting's people are already
        # prepped, so the nudge would be offering work the user has. (Chosen over an agent-side
        # "did you prep?" question, which nothing can answer deterministically.)
        if prepped_emails and {_s(a.get("email")).lower().strip() for a in ext} & set(prepped_emails):
            continue
        # The nudge CARRIES the prep instead of asking whether to do it: who they are (the graph's
        # own title/company for the first external attendee) and the one open loop with them, if
        # any. Two lines a chief of staff would say at the door; the deeper prep is behind a yes.
        who, loop = _prep_lines(ext[0], continuity)
        nudges.append({"kind": "meeting_prep", "key": f"mtg:{_s(e.get('id'))}",
                       "calendar_event_id": _s(e.get('id')), "calendar_start": st.isoformat(),
                       "calendar_observed_at": _s(e.get('calendar_observed_at')) or now_local.isoformat(),
                       "valid_until": st.isoformat(),
                       "title": _s(e.get("summary")) or "Meeting",
                       "person": _s(ext[0].get("displayName")) or _s(ext[0].get("email")).split("@")[0],
                       "who": who, "open_loop": loop,
                       "detail": f"starts in ~{int(mins_away)} min · "
                                 + ", ".join(_s(a.get('displayName') or a.get('email')) for a in ext[:4])
                                 + (f" · {who}" if who else "") + (f" · open with them: {loop}" if loop else "")})

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
                           "channel": _s(c.get("channel")), "identifier": _s(c.get("identifier")),
                           "anchor_key": _s(c.get('anchor_key')),
                           "thread_id": _s(c.get("thread_id"))})

    # 2b) The chase — something you're WAITING ON that the ledger marked chase-pending today. At
    #     most ONE per day even when several ripen (the rest keep their stamp and wait their turn):
    #     a day of chasing three people is a nag, and the whole point is that this stays warm.
    #     ONE GUARD, AND IT IS ABOUT THIS ITEM: a chase is held back when today's delivered brief
    #     already NAMED this very loop, because that is the double-tell — and only then. The 2h
    #     post-brief window it replaces was wrong twice over: for a loop the brief did print it
    #     merely postponed the repeat to 08:45, and for a first chase (a quiet loop the brief never
    #     names) it delayed a nudge over a collision that could not happen. `brief_named` is the
    #     brief's own record of what it said; None means no such record, so the old window stands.
    for c in (chase_candidates or [])[:1]:
        # A loop chased once is urgent by contract and the brief NAMES it every day from then on
        # (a Still-open line) — so "the brief already said it" would block the second chase forever,
        # chased_count never reached the cap, and the hand-off never came (Day-7 simulation, Sep
        # 2026). A chase and a Still-open line are different acts: only a FIRST chase yields to
        # the brief having named the loop.
        first_chase = int(c.get("chased_count") or 0) == 0
        if first_chase and ((_s(c.get("anchor_key")) in brief_named) if brief_named is not None
                            else brief_recent):
            continue
        nudges.append({"kind": "chase", "key": _s(c.get("id")) or f"chase:{today}",
                       "title": _s(c.get("title")) or "Still waiting on this",
                       "person": _s(c.get("name")),
                       "anchor_key": _s(c.get("anchor_key")),
                       "detail": _s(c.get("detail")),
                       "channel": _s(c.get("channel")), "identifier": _s(c.get("identifier")),
                       "thread_id": _s(c.get("thread_id"))})

    # 3) Birthdays — a saved contact whose birthday is today (MM-DD), plus a VIP/VVIP LEAD nudge
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
                importance = ((birthday_importance or {}).get(
                    f"canonical:{_s(ct.get('canonical_id')).strip()}")
                    or (birthday_importance or {}).get(nm.strip().casefold(), {}))
                if days_out and importance.get("tier") not in {"vip", "vvip"}:
                    continue
                nudges.append({
                    "kind": "birthday",
                    "key": f"bday:{nm.lower()}:{year}" + (f":lead{days_out}" if days_out else ""),
                    "lead_days": days_out, "person": nm,
                    "importance": importance if days_out else {},
                    "title": (f"{nm}'s birthday is today" if not days_out
                              else f"{nm}'s birthday is in {days_out} days"),
                    "detail": ("send a quick note" if not days_out
                               else "enough time for a real gift — want me to pull what you know about them?")})

    # 4) The hand-off and the tidy-up. Two shapes, and the NAMED one wins:
    #    4a) a loop chased its two times with no answer — that is one person and one thing, so the
    #        nudge says so and asks the binary question. It has its OWN clock (`handoff_allowed`:
    #        just "not inside the 2h post-brief window"), not the tidy-up's 7-day cooldown: the
    #        question is owed, not offered, and it is asked the first tick it comes due — until it
    #        is, `brief_validate.is_urgent` keeps that loop taking a named line in every brief. A
    #        generic cleanup offer on day 5 used to push Maya's question to day 12 (Day-15
    #        simulation, Sep 2026). Asked once: `handoff_asked_at` is stamped on delivery — and
    #        while a delivery never acks (the question never reached the user), the per-day dedup
    #        key bounds it to one attempt a day, which is the right cadence for a question nobody
    #        has received.
    #    4b) otherwise, the pile itself is heavy — offer a cleanup, throttled by main's multi-day
    #        cooldown (NOT once a day) so it is a gentle periodic ask, never a daily nag.
    threshold = RETUNE_OFFER_MIN
    if handoff_allowed and (handoff_candidates or []):
        h = (handoff_candidates or [])[0]         # one question at a time; the rest keep their turn
        nudges.append({"kind": "handoff", "key": _s(h.get("id")) or f"handoff:{today}",
                       "title": _s(h.get("title")) or "Still no answer",
                       "person": _s(h.get("name")), "detail": _s(h.get("question")),
                       "anchor_key": _s(h.get("anchor_key")),
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
    from source_context import allowed, project_local
    local = project_local(local if isinstance(local, dict) else {})
    if not allowed('calendar'):
        calendar = []
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
               birthday_importance=_birthday_importance(local, now_local),
               stale_count=stale_count, retune_offer_allowed=retune_ok,
               prepped_emails=_research_cache_emails(date), brief_recent=brief_recent,
               chase_candidates=_chase_candidates(date),
               brief_today=_brief_delivered_today(now_local),
               handoff_candidates=_handoff_candidates(), handoff_allowed=not brief_recent,
               brief_named=_brief_named_keys(now_local),
               intentions=_intention_candidates(now_local))["nudges"]

    te = _funnel()
    ident = delivery_effects.run_id()
    result = delivery_effects.cached_result()
    if result is None:
        result = {"nudges": [], "held": [], "quiet": False}
        with delivery_effects.proactive_state(date) as state:
            seen = set(state['nudged'])
            # A pending result is owned by its durable run, never by the clock tick. If its
            # delivery expires, the next scan can reconsider it against current evidence.
            state['pending'] = {k: v for k, v in state['pending'].items()
                                if (delivery_effects.instant(v.get('valid_until')) or 0) > now_local.timestamp()}
            fresh = [n for n in due if n['key'] not in seen and n['key'] not in state['pending']]
            hold = _clock_hold(te, now_local)
            if hold:
                fresh = [n for n in fresh if f"held:{n['key']}" not in seen]
                result['quiet'], result['reason'] = True, hold
            fired_ids = _submit(te, fresh, now_local)
            fired = [dict(n, decision_id=fired_ids[n['key']])
                     for n in fresh if n['key'] in fired_ids]
            result['nudges'] = fired
            result['held'] = [n for n in fresh if n['key'] not in fired_ids]
            if hold:
                seen |= {f"held:{n['key']}" for n in result['held']}
            if ident:
                for n in fired:
                    until = delivery_effects.instant(n.get('valid_until'))
                    state['pending'][n['key']] = {
                        'run_id': ident,
                        'valid_until': min(until or float('inf'), now_local.timestamp()
                                           + delivery_effects.MAX_PENDING_SECONDS)}
                # The result and its reservation commit together. A retry returns these exact
                # decisions without spending the interrupt budget or asking the model again.
                result['_delivery_date'] = date
                result['_eligibility'] = delivery_effects.for_bundle(
                    {'events': [_proactive_event(n) for n in fired]})['effects']
                state.setdefault('runs', {})[ident] = result
            else:
                # Direct interactive invocations return into their visible conversation.
                seen |= {n['key'] for n in fired}
            state['nudged'] = sorted(seen)
    fired = result.get('nudges', [])
    deferred = _defer_delivery_effects(fired, result.get('_delivery_date', date), result)
    if not deferred:
        for n in fired:
            if n.get('intention_id'):
                _finish_intention(n['intention_id'])
            if n['kind'] == 'retune_offer':
                _stamp_retune_offer(date)
            if n['kind'] in _FINALIZE_FLAG:
                _finalize(n['kind'], n.get('anchor_key'))
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
