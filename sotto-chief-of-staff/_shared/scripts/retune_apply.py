#!/usr/bin/env python3
"""
retune_apply.py — the WRITE side of sotto-loops §B: clear / defer a stale continuity loop.

Gives the user the exit the auto-sweep never had. Operates on one loop by its anchor_key (from
retune_scan.py), mutating the continuity .md in place via continuity_resolve's own loader/persister
so the file stays schema-compatible with the brief.

  retune_apply.py dismiss <anchor_key>          # done with it — terminal "dismissed" (won't resurface)
  retune_apply.py snooze  <anchor_key> <days>    # hide it for N days, then surface again
  retune_apply.py keep    <anchor_key>           # "I still care": resets the clock that governs this
                                                 # direction — the 7d age expiry on what you owe,
                                                 # the chase count on what you're owed

Prints {"ok": bool, "action": ..., "anchor_key": ..., "detail": ...}.
Mutes / tone live in preferences.py — this only touches the continuity ledger.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "lib"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "morning-brief", "scripts"))
from textutil import _s  # noqa: E402
from timeutil import _now_local, configured_tz  # noqa: E402
import continuity_resolve as cr  # noqa: E402
import ledger_io  # noqa: E402


def _today() -> str:
    return _now_local(configured_tz() or "+00:00").strftime("%Y-%m-%d")


def _find(anchor_key: str):
    """Locate the loop by anchor_key (the file's frontmatter carries it; the dict key is anchor_key
    or basename). Returns the item dict (with _path) or None."""
    items = cr._load_items()
    if anchor_key in items:
        return items[anchor_key]
    for it in items.values():
        if _s(it.get("anchor_key")) == anchor_key:
            return it
    return None


def _apply_unlocked(action: str, anchor_key: str, days: int = 7) -> dict:
    anchor_key = (anchor_key or "").strip()
    if not anchor_key:
        return {"ok": False, "detail": "missing anchor_key"}
    it = _find(anchor_key)
    if it is None:
        return {"ok": False, "action": action, "anchor_key": anchor_key,
                "detail": "no open loop with that key (already resolved or cleared?)"}
    today = _today()
    if action == "dismiss":
        cr._terminate(it, "dismissed", "user_dismissed", today)
        cr._persist(it)
        return {"ok": True, "action": "dismiss", "anchor_key": anchor_key, "detail": "dismissed"}
    # DIRECTION decides which clock a verb may touch. A `waiting_on` has no 7-day age expiry to
    # reset, so resetting `created_at` on one only pushes its chase out and hides it from
    # retune_scan — the opposite of what the user just asked for.
    waiting = ledger_io.is_waiting_on(it.get("action_type"))
    if action == "snooze":
        until = (datetime.now(timezone.utc) + timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
        it["snoozed_until"] = until
        if not waiting:
            it["created_at"] = today      # reset the aging clock so it isn't auto-expired while hidden
        cr._persist(it)
        return {"ok": True, "action": "snooze", "anchor_key": anchor_key,
                "detail": f"hidden until {until}"}
    if action == "keep":
        if waiting:
            # "I still care" on a debt someone owes you = a fresh chase clock. Without this, an
            # item chased to its quota is kept and then never chased again — the verb would mean
            # nothing on the one direction where it is most often offered. The field list is
            # ledger_io's (CHASE_STATE_FIELDS): it includes the hand-off stamp, so answering
            # "keep waiting" both restarts the chases AND makes the loop askable-about again.
            for k in ledger_io.CHASE_STATE_FIELDS:
                it.pop(k, None)
            cr._persist(it)
            return {"ok": True, "action": "keep", "anchor_key": anchor_key,
                    "detail": "kept (chase clock reset)"}
        it["created_at"] = today          # fresh 7-day window; user intends to handle it
        cr._persist(it)
        return {"ok": True, "action": "keep", "anchor_key": anchor_key, "detail": "kept (clock reset)"}
    return {"ok": False, "detail": f"unknown action: {action}"}


def apply(action: str, anchor_key: str, days: int = 7) -> dict:
    with cr._ledger_lock():
        return _apply_unlocked(action, anchor_key, days)


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "detail": "usage: retune_apply.py dismiss|snooze|keep <anchor_key> [days]"}))
        sys.exit(2)
    action, anchor_key = sys.argv[1], sys.argv[2]
    days = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 7
    print(json.dumps(apply(action, anchor_key, days)))


if __name__ == "__main__":
    main()
