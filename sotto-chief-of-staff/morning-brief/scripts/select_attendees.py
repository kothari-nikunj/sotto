#!/usr/bin/env python3
"""
select_attendees.py — deterministically pick the EXTERNAL attendees of today's meetings who
warrant research, so the agent researches the right people (and only them) before composing.

Only external attendees lacking useful known profiles are selected within the next 72h.

Usage (execute_code): pipe the SAME inputs dict the brief uses (or at least {google, local}):
    select_attendees.py < inputs.json
Prints JSON: [{"name","email","meeting_title","meeting_start"}, ...]  (deduped, capped at 25)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib"))
from prep_selection import select_attendees_for_research  # noqa: E402


def main():
    # Degrade gracefully: a missing/unreadable input file (e.g. the agent ran this before gathering)
    # means "no attendees to research", NOT a traceback. An empty list is the SKILL's skip signal.
    try:
        raw = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
        inputs = json.loads(raw) if raw.strip() else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        print("[]")
        return
    print(json.dumps(select_attendees_for_research(inputs)))


if __name__ == "__main__":
    main()
