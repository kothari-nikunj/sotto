#!/usr/bin/env python3
"""
retune_scan.py — the read-only scan behind `sotto-retune`: what to clean up and what to retune.

A periodic "tune-up" surfaces three things, all deterministic from the volume:
  - stale_loops    : ACTIVE continuity loops that are getting old, overdue, or surfaced again and again
                     without resolution — the candidates the user can dismiss / snooze / keep. (The
                     brief auto-expires loops at 7 days; this catches the 3–7d window + repeat-offenders
                     BEFORE they clutter another brief, and gives a user-driven exit the auto-sweep lacks.)
  - mute_suggestions: contacts the user keeps dismissing (from the BEHAVIORAL learner's
                     deprioritization_hints) who aren't muted yet — the bridge from "learned" to an
                     explicit mute the brief honors.
  - current        : the settings a retune might change (timezone + the explicit mutes/tone in effect).

Read-only. `retune_apply.py` performs the chosen dismiss/snooze/keep; `preferences.py` performs mutes.

Usage: retune_scan.py        → {stale_loops, mute_suggestions, current, counts}
Env: SOTTO_DATA, SOTTO_TIMEZONE. Tunables: SOTTO_STALE_AGE_DAYS (4), SOTTO_STALE_SURFACED (3).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose_brief as cb  # noqa: E402
import ledger_io  # noqa: E402
import preferences as pref  # noqa: E402

WAITING_TYPES = {"waiting_on", "follow_up_stale"}
STALE_AGE_DAYS = int(os.environ.get("SOTTO_STALE_AGE_DAYS", "4"))
STALE_SURFACED = int(os.environ.get("SOTTO_STALE_SURFACED", "3"))


def scan() -> dict:
    today = cb._now_local(cb.configured_tz() or "+00:00")
    today_str = today.strftime("%Y-%m-%d")

    stale = []
    for it in ledger_io.load_active():
        if cb._s(it.get("snoozed_until"))[:10] > today_str:    # already snoozed — leave it be
            continue
        age = ledger_io.age_days(it.get("created_at"), today)
        surfaced = int(it.get("times_surfaced", 1) or 1)
        deadline = cb._s(it.get("deadline"))[:10]
        overdue = bool(deadline and deadline < today_str)
        is_stale = overdue or (age is not None and age >= STALE_AGE_DAYS) or surfaced >= STALE_SURFACED
        if not is_stale:
            continue
        direction = "waiting_on_them" if cb._s(it.get("action_type")).lower() in WAITING_TYPES else "you_owe"
        stale.append({
            "anchor_key": cb._s(it.get("anchor_key")),
            "name": cb._s(it.get("contact_name")) or "(unknown)",
            "what": (cb._s(it.get("summary") or it.get("ask"))
                     or cb._s(it.get("action_type")).replace("_", " "))[:200],
            "direction": direction,
            "age_days": age,
            "times_surfaced": surfaced,
            "overdue": overdue,
            "deadline": deadline or None,
            # what a chief of staff would propose: chase what you owe, nudge-or-drop what you're awaiting
            "suggestion": "do it or dismiss" if direction == "you_owe" else "nudge or drop",
        })
    # worst first: overdue, then most-surfaced, then oldest
    stale.sort(key=lambda e: (not e["overdue"], -e["times_surfaced"], -(e["age_days"] or 0)))

    current = {
        "timezone": cb.configured_tz(),
        **pref.load_explicit(),
    }

    # Behavioral → explicit bridge: contacts the learner flagged as repeatedly-dismissed, not yet muted.
    muted_people_lc = {p.lower() for p in current.get("mute_people", [])}
    suggestions = []
    try:
        with open(os.path.join(os.environ.get("SOTTO_DATA", "/data"), "preferences.json"), encoding="utf-8") as f:
            hints = (json.load(f) or {}).get("deprioritization_hints", []) or []
    except (OSError, json.JSONDecodeError, ValueError):
        hints = []
    seen = set()
    for h in hints:
        contact = cb._s(h).split("|", 1)[0].strip()
        if not contact or contact.lower() in muted_people_lc or contact.lower() in seen:
            continue
        seen.add(contact.lower())
        suggestions.append({"name": contact, "reason": "you keep dismissing their items"})

    return {
        "stale_loops": stale,
        "mute_suggestions": suggestions,
        "current": current,
        "counts": {"stale": len(stale), "mute_suggestions": len(suggestions)},
    }


if __name__ == "__main__":
    print(json.dumps(scan()))
