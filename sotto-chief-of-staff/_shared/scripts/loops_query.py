#!/usr/bin/env python3
"""
loops_query.py — the open-loops / action-ledger view for the `sotto-loops` skill.

Reads the continuity ledger ($SOTTO_DATA/knowledge/continuity/*.md — the same files the brief's
continuity_resolve.py maintains) and splits the ACTIVE loops into two directions a chief of staff cares
about, oldest/most-overdue first (the split itself is `ledger_io.WAITING_ON_TYPES` — one predicate,
shared with the resolver and retune_scan):
  - you_owe        — things YOU need to do (reply / follow up / follow up on a stale thread / call back)
  - waiting_on_them — things you've handed off and are AWAITING the other side on

This is read-only (resolution happens in the brief's Learn step); it just surfaces what's open so the
user can ask "what am I waiting on?" / "what are my open loops?" without running a whole brief.

Usage: loops_query.py            → {you_owe:[...], waiting_on_them:[...], counts:{...}}
Env: SOTTO_DATA (ledger root), SOTTO_TIMEZONE (for "today"/age).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

# Shared tz helpers so "today"/age match the brief; shared ledger loader so all the
# read views agree on what's open (ledger_io).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from textutil import _s  # noqa: E402
from timeutil import _now_local, configured_tz  # noqa: E402
import ledger_io  # noqa: E402

# Direction is defined ONCE, in ledger_io: `waiting_on` is what THEY owe you; everything else
# active — `follow_up_stale` included, since it resolves on your outgoing message and expires on
# your clock — is what you owe. A second copy here is how a chased item got filed under you_owe.


def _entry(it: dict, today: datetime, today_str: str) -> dict:
    name = _s(it.get("contact_name")) or "(unknown)"
    label = _s(it.get("summary") or it.get("ask")) or _s(it.get("action_type")).replace("_", " ")
    deadline = _s(it.get("deadline"))[:10]
    age = ledger_io.age_days(it.get("created_at"), today)
    return {
        "name": name,
        "what": label[:200],
        "channel": _s(it.get("channel")),
        "identifier": _s(it.get("contact_identifier")),
        "action_type": _s(it.get("action_type")),
        "age_days": age,
        "deadline": deadline or None,
        "overdue": bool(deadline and deadline < today_str),
        # Chase state (written ONLY by continuity_resolve — see its `_stamp_chase`). Surfaced so
        # "what am I waiting on" can name it ("chased once, Tuesday") and so proactive_scan can
        # read today's chase-due item without a second ledger reader.
        # `chase_pending` is the PROPOSED chase for that day: proactive_scan delivers it and calls
        # `continuity_resolve.py --finalize-chase <anchor_key>`, which is what makes it count.
        "anchor_key": _s(it.get("anchor_key")),
        "chased_count": int(it.get("chased_count") or 0),
        "chase_pending": _s(it.get("chase_pending"))[:10] or None,
        "last_chased_at": _s(it.get("last_chased_at"))[:10] or None,
        "chase_after": _s(it.get("chase_after"))[:10] or None,
        # Chased its full quota: the automatic chase has stopped and the decision is the user's.
        # A hand-off is something the system OWES them, so lanes may surface it without waiting
        # for the tidy-up threshold.
        "chased_out": int(it.get("chased_count") or 0) >= ledger_io.CHASE_MAX,
        # …and the day that question was actually DELIVERED (continuity_resolve's
        # `--finalize-handoff`, the hand-off's half of the chase's two-phase rule). Once it is set
        # the user has been asked, so the question is not repeated and the brief stops giving the
        # loop a named line — it waits here, on this list, for their answer.
        "handoff_asked_at": _s(it.get("handoff_asked_at"))[:10] or None,
    }


def query() -> dict:
    today = _now_local(configured_tz() or "+00:00")
    today_str = today.strftime("%Y-%m-%d")
    you_owe, waiting = [], []
    for it in ledger_io.load_active():   # ACTIVE and not user-snoozed — ledger_io owns both rules
        # Meeting prep/info are calendar shadows, not communication debts — the docket and the
        # brief's schedule section own them. (Legacy entries; new ones are no longer created.)
        if ledger_io.normalize_action_type(it.get("action_type")) in ("meeting_prep", "meeting_info"):
            continue
        e = _entry(it, today, today_str)
        (waiting if ledger_io.is_waiting_on(it.get("action_type")) else you_owe).append(e)
    # Oldest / most-overdue first (overdue, then by age desc).
    sort_key = lambda e: (not e["overdue"], -(e["age_days"] or 0))
    you_owe.sort(key=sort_key)
    waiting.sort(key=sort_key)
    out = {"you_owe": you_owe, "waiting_on_them": waiting,
           "counts": {"you_owe": len(you_owe), "waiting_on_them": len(waiting)}}
    try:
        from sotto_log import diag
        diag(f"[loops_query] {out['counts']['you_owe']} you-owe, {out['counts']['waiting_on_them']} waiting-on")
    except Exception:
        pass
    return out


if __name__ == "__main__":
    print(json.dumps(query()))
