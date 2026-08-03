#!/usr/bin/env python3
"""
select_attendees.py — deterministically pick the EXTERNAL attendees of today's meetings who
warrant research, so the agent researches the right people (and only them) before composing.

PORT SOURCE: gemini-flex.ts::processCalendarEvents (the `_needs_research = !isKnown` filter) +
gemini-research.ts (MAX_ATTENDEES_TO_RESEARCH=25). An attendee needs research unless they are the
user, share the user's email domain, or are already a known contact / in the knowledge graph. Only
meetings within the next 72h are considered. The actual research is host-native (the agent's web
search, per references/research-prompt.md) — this script just decides WHO.

Usage (execute_code): pipe the SAME inputs dict the brief uses (or at least {google, local}):
    select_attendees.py < inputs.json
Prints JSON: [{"name","email","meeting_title","meeting_start"}, ...]  (deduped, capped at 25)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "scripts"))
from compose_brief import select_attendees_for_research  # noqa: E402


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
