#!/usr/bin/env python3
"""
digest_check.py — the adaptive midday catch-up gate (Phase 2 §3).

Reads $SOTTO_DATA/events/queue.jsonl entries queued SINCE the last digest stamp
($SOTTO_DATA/events/last_digest.txt) and decides deterministically whether a midday digest is worth
delivering. Heavy day (ambient count >= SOTTO_DIGEST_MIN, default 8) →
  {"deliver": true, "items": [{sender, class, preview, ts}, …]}
(one item per sender — the NEWEST entry per sender wins, newest first, hard cap 12); a quiet day →
  {"deliver": false}
so the sotto-event skill's digest mode stays silent.

"Ambient" = every queued class EXCEPT "signal": the user's own outbound messages are ledger fodder
for deterministic loop resolution, never digest content.

  digest_check.py              → the decision JSON (the skill consumes it verbatim)
  digest_check.py --stamp      → records now to last_digest.txt (run AFTER delivering)
  digest_check.py --now ISO    → clock override (tests)

Mirrors followup_cron.py's marker style (--silent-check/--stamp): the decision is pure and testable;
the skill only composes and delivers what this prints.

Env: SOTTO_DATA (state dir), SOTTO_DIGEST_MIN (ambient threshold, default 8).
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
from textutil import _s  # noqa: E402
from timeutil import _parse_ts  # noqa: E402

ITEM_CAP = 12


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


def check(entries: list, min_n: int | None = None) -> dict:
    """Pure decision: deliver iff the ambient pile is heavy enough. Items are deduped by sender
    (newest per sender wins), ordered newest first, capped at ITEM_CAP."""
    min_n = _int_env("SOTTO_DIGEST_MIN", 8) if min_n is None else min_n
    ambient = [e for e in entries if _s(e.get("verdict_class")) != "signal"]
    if len(ambient) < min_n:
        return {"deliver": False}
    # queue.jsonl is append-ordered → iterate newest-first and keep the first (= newest) per sender.
    by_sender: dict = {}
    for entry in reversed(ambient):
        it = _item(entry)
        key = it["sender"].strip().lower()
        if key not in by_sender:
            by_sender[key] = it
    items = list(by_sender.values())[:ITEM_CAP]
    return {"deliver": True, "items": items}


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
    print(json.dumps(check(entries_since(read_stamp()))))


if __name__ == "__main__":
    main()
