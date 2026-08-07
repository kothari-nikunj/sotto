#!/usr/bin/env python3
"""
digest_check.py — the adaptive midday catch-up gate (Phase 2 §3).

Reads $SOTTO_DATA/events/queue.jsonl entries queued SINCE the last digest stamp
($SOTTO_DATA/events/last_digest.txt) and decides deterministically whether a midday digest is worth
delivering. Heavy day (SIGNAL count >= SOTTO_DIGEST_MIN, default 8) →
  {"deliver": true, "items": [{sender, class, preview, ts}, …]}
(one item per sender — the NEWEST entry per sender wins; hard cap 6, matching the skill's 6-line
rule); a quiet day →
  {"deliver": false}
so the sotto-event skill's digest mode stays silent.

What counts toward the threshold (Sprint 0 §2a — the gate measures SIGNAL, not noise):
  - class "ambient" from a KNOWN sender (Tier-1 judged it FYI-worthy), and
  - classes "quiet"/"cooldown"/"stale"/"budget"/"snoozed"/"meeting_hold" from a KNOWN sender
    (agent-worthy verdicts that were deterministically deferred — the closest thing the queue has
    to "actionable"; "budget" is the daily interrupt cap, "snoozed" the user's cadence lever, and
    since neither promotes back through the release valve the digest is their way out;
    "meeting_hold" DOES promote when the meeting ends, but a day spent entirely in rooms is
    exactly the day the digest exists for).
KNOWN = the resolved sender name triage wrote is a real name: non-empty, not the "Unknown"
sentinel, not phone-shaped (same standard as triage_event._is_known_name). Cold-sender email
("unknown"), group chatter ("group"), unknown-number missed calls and "error" entries never trip
"heavy day" — but when a digest DOES deliver they may still ride along in items, below the fold
(counted classes first: deferred-actionable, then known-ambient, then the rest; newest first
within each band). "signal" (the user's own outbound) is ledger fodder — never an item.

  digest_check.py              → the decision JSON (the skill consumes it verbatim);
                                 ALWAYS stamps last_digest.txt at the end of the run (§2b) —
                                 silent runs advance the window too, so it can't grow unbounded
  digest_check.py --stamp      → records now to last_digest.txt (kept for the skill's post-deliver
                                 stamp; harmless now that check runs self-stamp)
                                 (the BRIEF also advances this window — brief_marker.claim() calls
                                 advance_stamp() when it WINS the deliver-once claim, so the 12:30
                                 digest can't re-surface what the delivered brief just covered;
                                 Sprint 0 §2c)
  digest_check.py --now ISO    → clock override (tests)

Mirrors followup_cron.py's marker style (--silent-check/--stamp): the decision is pure and testable;
the skill only composes and delivers what this prints.

Env: SOTTO_DATA (state dir), SOTTO_DIGEST_MIN (signal threshold, default 8).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED_LIB = os.path.join(_HERE, "..", "..", "_shared", "lib")
if _SHARED_LIB not in sys.path:
    sys.path.insert(0, _SHARED_LIB)
from textutil import _looks_like_phone_number, _s  # noqa: E402
from timeutil import _parse_ts  # noqa: E402

ITEM_CAP = 6                                     # matches the skill's 6-line rule (Sprint 0 §2d)
# Classes that count toward "heavy day" (when the sender is known): Tier-1 ambient plus the
# deterministically-deferred agent verdicts (quiet hours / cooldown / stale-catchup / daily
# interrupt budget / user snooze / in-meeting hold).
COUNT_CLASSES = frozenset({"ambient", "quiet", "cooldown", "stale", "budget", "snoozed",
                           "meeting_hold"})
# Of those, the deferred-agent ones sort first in items — they were judged interrupt-worthy once.
ACTIONABLE_CLASSES = frozenset({"quiet", "cooldown", "stale", "budget", "snoozed", "meeting_hold"})


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _events_dir() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "events")


def queue_path() -> str:
    return os.path.join(_events_dir(), "queue.jsonl")


def stamp_path() -> str:
    return os.path.join(_events_dir(), "last_digest.txt")


def _parse_iso(raw):
    """ISO-8601 → aware UTC datetime, or None (naive treated as UTC — the marker's own semantics,
    same wrapper followup_cron keeps local)."""
    if not raw:
        return None
    dt = _parse_ts(str(raw).strip())
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_stamp():
    """Last digest time as an aware UTC datetime, or None if never stamped / unreadable."""
    try:
        with open(stamp_path(), encoding="utf-8") as f:
            return _parse_iso(f.read())
    except OSError:
        return None


def write_stamp(now: datetime) -> None:
    """Persist `now` as the last digest (atomic). Best-effort — a missed stamp just widens the next
    window, never loses an item."""
    try:
        os.makedirs(_events_dir(), exist_ok=True)
        tmp = stamp_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        os.replace(tmp, stamp_path())
    except OSError:
        pass


def advance_stamp(now: datetime) -> None:
    """Move the digest window FORWARD to `now` — never backward (Sprint 0 §2c).

    The brief calls this (brief_marker.claim, on the claim that wins delivery) so the 12:30 digest
    can't re-surface what the 6:30a brief just covered: the window starts after the DELIVERED
    brief, not after yesterday's digest — a compose that loses the claim never stamps.
    Forward-only because this write has callers outside the digest's own clock — a re-run, an eval,
    or a backfilled compose must never rewind the window and replay items the user already saw.
    Best-effort, like write_stamp."""
    try:
        now = now.astimezone(timezone.utc)
        current = read_stamp()
        if current is not None and now <= current:
            return
        write_stamp(now)
    except (OSError, ValueError, AttributeError):
        pass


def entries_since(stamp) -> list:
    """queue.jsonl entries with ts AFTER `stamp` (all entries when never stamped). One bad line
    never poisons the read."""
    out = []
    try:
        with open(queue_path(), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if stamp is not None:
            ts = _parse_iso(entry.get("ts"))
            if ts is None or ts <= stamp:
                continue
        out.append(entry)
    return out


def _item(entry: dict) -> dict:
    ev = entry.get("event") if isinstance(entry.get("event"), dict) else {}
    sender = (_s(entry.get("sender")) or _s(ev.get("from")) or _s(ev.get("handle"))
              or _s(ev.get("contact_jid")) or _s(ev.get("phone")) or "unknown")
    preview = _s(ev.get("subject")) or _s(ev.get("text")) or _s(ev.get("body"))
    return {"sender": sender, "class": _s(entry.get("verdict_class")),
            "preview": preview[:200], "ts": _s(entry.get("ts"))}


def _sender_known(sender) -> bool:
    """The queue entry's `sender` (the resolved display name triage_event wrote) names a real
    person: non-empty, not the resolver's "Unknown" sentinel, not phone/shortcode-shaped."""
    n = _s(sender).strip()
    return bool(n) and n != "Unknown" and not _looks_like_phone_number(n)


def _counts_toward_threshold(entry: dict) -> bool:
    return (_s(entry.get("verdict_class")) in COUNT_CLASSES
            and _sender_known(entry.get("sender")))


def _band(entry: dict) -> int:
    """Item priority band: 0 = known deferred-actionable, 1 = known ambient, 2 = the rest
    (unknown/group/error/…) — below the fold."""
    if _sender_known(entry.get("sender")):
        cls = _s(entry.get("verdict_class"))
        if cls in ACTIONABLE_CLASSES:
            return 0
        if cls == "ambient":
            return 1
    return 2


def check(entries: list, min_n: int | None = None) -> dict:
    """Pure decision: deliver iff the KNOWN-sender signal pile (see module docstring) is heavy
    enough. Items are deduped by sender (newest per sender wins), ordered known-actionable →
    known-ambient → everything else, newest first within each band, capped at ITEM_CAP."""
    min_n = _int_env("SOTTO_DIGEST_MIN", 8) if min_n is None else min_n
    candidates = [e for e in entries if _s(e.get("verdict_class")) != "signal"]
    counted = [e for e in candidates if _counts_toward_threshold(e)]
    if len(counted) < min_n:
        return {"deliver": False}
    # queue.jsonl is append-ordered → iterate newest-first and keep the first (= newest) per sender.
    by_sender: dict = {}
    for entry in reversed(candidates):
        it = _item(entry)
        key = it["sender"].strip().lower()
        if key not in by_sender:
            by_sender[key] = (_band(entry), it)
    ranked = list(by_sender.values())          # newest-first already; stable sort keeps that
    ranked.sort(key=lambda pair: pair[0])
    items = [it for _band_, it in ranked[:ITEM_CAP]]
    return {"deliver": True, "items": items}


def run_check(now: datetime) -> dict:
    """One check run: decide off the current window, then ALWAYS advance the window stamp — a
    silent run must not let 'since last digest' grow unbounded (Sprint 0 §2b). Anything the silent
    window skipped wasn't digest-worthy then; the brief remains the backstop."""
    result = check(entries_since(read_stamp()))
    write_stamp(now)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", action="store_true",
                    help="record this digest's time (run AFTER delivering)")
    ap.add_argument("--now", help="ISO timestamp override (tests)")
    a = ap.parse_args()
    now = _parse_iso(a.now) or datetime.now(timezone.utc)
    if a.stamp:
        write_stamp(now)
        print("stamped")
        return
    print(json.dumps(run_check(now)))


if __name__ == "__main__":
    main()
