#!/usr/bin/env python3
"""
log_outcome.py — record action outcomes + analytics to the exhaust (parity C2).

PORT SOURCE: app/src/hooks/useActionExecution.ts outcomes + api/src/services/execution-ledger.ts
Appends to $SOTTO_DATA/outcomes.jsonl. Feeds learn_preferences.py.

Usage: log_outcome.py '{"action_id":"...","outcome":"edited_and_sent","channel":"imessage",
                        "contact":"sarah","action_type":"reply","tier":"one_tap","edits":"..."}'
Outcomes: draft_created|opened|copied|dismissed|executed|viewed|edited_and_sent
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

VALID = {"draft_created", "opened", "copied", "dismissed", "executed", "viewed", "edited_and_sent"}


def _path():
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "outcomes.jsonl")


def log(rec: dict) -> dict:
    rec = dict(rec)
    rec["ts"] = rec.get("ts") or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    if rec.get("outcome") not in VALID:
        raise ValueError(f"invalid outcome: {rec.get('outcome')}")
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    with open(_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return {"logged": True, "outcome": rec["outcome"]}


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    print(json.dumps(log(json.loads(raw))))


if __name__ == "__main__":
    main()
