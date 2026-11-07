#!/usr/bin/env python3
"""
retune_scan.py — the read-only scan behind `sotto-loops` §B: what to clean up and what to retune.

A periodic "tune-up" surfaces three things, all deterministic from the volume:
  - stale_loops    : ACTIVE continuity loops that are getting old, overdue, or were actually delivered
                     by name again and again
                     without resolution: candidates the user can dismiss / snooze / keep.
                     Legacy capture counts are untrusted. Inferred debts park only after the existing
                     warning/quiet-age rules; this scan never expires or resolves a task.
  - mute_suggestions: empty until explicit feedback can be distinguished from inferred draft
                     non-use. Existing deprioritization_hints are not evidence to mute a person.
  - current        : the settings a retune might change (timezone + the explicit mutes/tone in effect).

Read-only. `retune_apply.py` performs the chosen dismiss/snooze/keep; `preferences.py` performs mutes.

Usage: retune_scan.py        → {stale_loops, mute_suggestions, current, counts}
Env: SOTTO_DATA, SOTTO_TIMEZONE. Named constants, not knobs (defaults matter — see CLAUDE.md):
STALE_AGE_DAYS, STALE_SURFACED.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from textutil import _s  # noqa: E402
from timeutil import _now_local, _calendar_day, configured_tz  # noqa: E402
import ledger_io  # noqa: E402
import preferences as pref  # noqa: E402
from delivery_effects import delivered_surface_count  # noqa: E402
from ledger_provenance import project_entries  # noqa: E402

# Direction comes from ledger_io (one predicate for the resolver and every read view): `waiting_on`
# is what THEY owe you; `follow_up_stale` is a nudge YOU owe them, so it sits in you_owe.
STALE_AGE_DAYS = 4    # a loop this many days old is stale — long enough that it isn't just "today"
STALE_SURFACED = 3    # …or surfaced this many times without moving, which says the same thing
REMINDER_HANDOFF = "reminded you twice — draft a follow-up or drop"
LEGACY_CHASE_HANDOFF = "chased twice — nudge again or drop"


def scan(now=None, *, rows=None) -> dict:
    """Return every stale active obligation, using an injectable clock for brief rendering/tests."""
    today = now or _now_local(configured_tz() or "+00:00")
    return _scan_rows(ledger_io.load_active(now=today) if rows is None else rows, today)



def scan_permitted_rows(data_root: str, permitted: list[str], *, now: datetime | None = None) -> dict:
    """Original stale evidence over the caller's protected current-source view."""
    from loops_query import permitted_rows
    today = now or _now_local(configured_tz() or '+00:00')
    if not isinstance(today, datetime) or today.tzinfo is None or today.utcoffset() is None:
        raise ValueError('protected scan requires timezone-aware now')
    if type(permitted) is not list or any(type(source) is not str for source in permitted):
        raise ValueError('protected scan sources invalid')
    rows = permitted_rows(data_root, permitted)
    out = _scan_rows(ledger_io.active_entries(rows, today), today)
    out['coverage'] = 'currently_permitted_canonical_loops'
    return out

def scan_owner_rows(active_rows: list[dict], *, now: datetime) -> dict:
    """Owner-only view over caller-bounded, canonical active rows only.

    No disk loading or agent exposure is added here. The caller must enforce file
    type, symlink, count and byte limits before parsing ledger frontmatter.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("protected scan requires timezone-aware now")
    out = _scan_rows(project_entries(active_rows, lambda _source: False), now,
                     current={})
    out["coverage"] = "owner_authored_only"
    return out


def _scan_rows(rows: list[dict], now=None, *, current: dict | None = None, include_all: bool = False) -> dict:
    today = now or _now_local(configured_tz() or "+00:00")
    if today.tzinfo is None:
        today = today.replace(tzinfo=_now_local(configured_tz() or "+00:00").tzinfo)
    today_str = today.strftime("%Y-%m-%d")

    stale = []
    for it in rows:   # ACTIVE and not snoozed — caller/ledger_io owns both rules
        age = ledger_io.age_days(it.get("created_at"), today)
        surfaced = delivered_surface_count(it)
        deadline = _calendar_day(it.get("deadline"))
        overdue = bool(deadline and deadline < today_str)
        is_stale = overdue or (age is not None and age >= STALE_AGE_DAYS) or surfaced >= STALE_SURFACED
        if not is_stale and not include_all:
            continue
        direction = "waiting_on_them" if ledger_io.is_waiting_on(it.get("action_type")) else "you_owe"
        # The hand-off counts reminders delivered to the owner, never messages to the counterpart.
        # continuity_resolve stops at CHASE_MAX; a draft still needs a separate send approval.
        chased = int(it.get("chased_count") or 0)
        suggestion = "do it or dismiss" if direction == "you_owe" else "nudge or drop"
        if direction == "waiting_on_them" and chased >= ledger_io.CHASE_MAX:
            suggestion = REMINDER_HANDOFF
        stale.append({
            "anchor_key": _s(it.get("anchor_key")),
            "name": ledger_io.contact_label(it),
            "what": ledger_io.task_label(it)[:200],
            "direction": direction,
            "age_days": age,
            "times_surfaced": surfaced,
            "surface_count_provenance": ("accepted_delivery" if isinstance(it.get("delivery_surface"), dict)
                                         and it["delivery_surface"].get("schema") == 1
                                         else "unknown_legacy"),
            "overdue": overdue,
            "deadline": deadline or None,
            "chased_count": chased,
            "chased_out": chased >= ledger_io.CHASE_MAX,
            # what a chief of staff would propose: chase what you owe, nudge-or-drop what you're awaiting
            "suggestion": suggestion,
        })
    # worst first: overdue, then most-surfaced, then oldest
    stale.sort(key=lambda e: (not e["overdue"], -e["times_surfaced"], -(e["age_days"] or 0)))

    if current is None:
        current = {"timezone": configured_tz(), **pref.load_explicit()}

    # Fail closed at the shared consumer (evening brief AND tune-up), including old preferences.
    # The matcher records unmatched drafts as "dismissed" and historical outcomes have no
    # provenance separating that inference from a deliberate rejection. Even rejecting a draft
    # does not mean the person should be muted. Keep the response shape; explicit mute commands
    # remain available. Restore suggestions only with the designed feedback/evidence contract.
    suggestions = []

    return {
        "stale_loops": stale,
        "mute_suggestions": suggestions,
        "current": current,
        "counts": {"stale": len(stale), "mute_suggestions": len(suggestions)},
    }


if __name__ == "__main__":
    print(json.dumps(scan()))
