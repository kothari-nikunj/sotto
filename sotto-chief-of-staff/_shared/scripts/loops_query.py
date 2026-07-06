#!/usr/bin/env python3
"""
loops_query.py — the open-loops / action-ledger view for the `sotto-loops` skill.

Reads the continuity ledger ($SOTTO_DATA/knowledge/continuity/*.md — the same files the brief's
continuity_resolve.py maintains) and splits the ACTIVE loops into two directions a chief of staff cares
about, oldest/most-overdue first:
  - you_owe        — things YOU need to do (reply / follow up / call back / draft / prep)
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
import compose_brief as cb  # noqa: E402
import ledger_io  # noqa: E402

WAITING_TYPES = {"waiting_on", "follow_up_stale"}          # you've acted; awaiting their side


def _entry(it: dict, today: datetime, today_str: str) -> dict:
    name = cb._s(it.get("contact_name")) or "(unknown)"
    label = cb._s(it.get("summary") or it.get("ask")) or cb._s(it.get("action_type")).replace("_", " ")
    deadline = cb._s(it.get("deadline"))[:10]
    age = ledger_io.age_days(it.get("created_at"), today)
    return {
        "name": name,
        "what": label[:200],
        "channel": cb._s(it.get("channel")),
        "identifier": cb._s(it.get("contact_identifier")),
        "action_type": cb._s(it.get("action_type")),
        "age_days": age,
        "deadline": deadline or None,
        "overdue": bool(deadline and deadline < today_str),
    }


def query() -> dict:
    today = cb._now_local(cb._env_tz() or "+00:00")
    today_str = today.strftime("%Y-%m-%d")
    you_owe, waiting = [], []
    for it in ledger_io.load_active():
        if cb._s(it.get("snoozed_until"))[:10] > today_str:   # user-snoozed via sotto-retune
            continue
        e = _entry(it, today, today_str)
        (waiting if cb._s(it.get("action_type")).lower() in WAITING_TYPES else you_owe).append(e)
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
