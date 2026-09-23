#!/usr/bin/env python3
"""
log_outcome.py — record action outcomes + analytics to the exhaust (parity C2).

PORT SOURCE: app/src/hooks/useActionExecution.ts outcomes + api/src/services/execution-ledger.ts
Appends to $SOTTO_DATA/outcomes.jsonl. Read by draft grading and outcome inspection.

Usage: log_outcome.py '{"action_id":"...","outcome":"edited_and_sent","channel":"imessage",
                        "contact":"sarah","action_type":"reply","tier":"one_tap","edits":"..."}'
Outcomes: draft_created|opened|copied|dismissed|executed|viewed|edited_and_sent|useful|not_useful
"""
from __future__ import annotations

import json
import hashlib
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
import jsonstore  # noqa: E402

VALID = {"draft_created", "opened", "copied", "dismissed", "executed", "viewed", "edited_and_sent",
         "useful", "not_useful", "loop_transition", "loop_proposal"}


def _path():
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "outcomes.jsonl")


def log(rec: dict) -> dict:
    rec = dict(rec)
    rec["ts"] = rec.get("ts") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if rec.get("outcome") not in VALID:
        raise ValueError(f"invalid outcome: {rec.get('outcome')}")
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    def append():
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    # jsonstore.lock is process-reentrant, so callers that already hold this ledger's lock take
    # the same path as every other writer.  Keeping a lock-bypass flag would let a future caller
    # append without exclusion merely by asserting that it had locked correctly.
    with jsonstore.lock(_path()):
        # Only durable, replayable producers supply an event_id. Ordinary action feedback keeps
        # its existing append semantics. Retention bounds this existing exhaust to ninety days.
        if rec.get("event_id") and os.path.exists(_path()):
            with open(_path(), encoding="utf-8") as handle:
                for line in handle:
                    try:
                        old = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(old, dict) and old.get("event_id") == rec["event_id"]:
                        return {"logged": True, "outcome": rec["outcome"], "duplicate": True}
        append()
    return {"logged": True, "outcome": rec["outcome"]}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _utc_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_utc_z(value) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return _utc_z()


def loop_transition(previous: dict | None, row: dict) -> dict | None:
    """Content-free transition metadata stored atomically with the canonical ledger row.

    The last transition also survives an exhaust append failure. The report can recover it from
    that row; earlier missing history stays unknown. Observation time is an exact UTC timestamp,
    never the day-only resolution date or a claimed source timestamp.
    """
    from delivery_effects import loop_identity

    previous = previous or {}
    old_status = previous.get("status", "open") if previous else "absent"
    new_status = row.get("status", "open")
    if previous and old_status == new_status and loop_identity(previous) == loop_identity(row):
        return previous.get("proof_transition")
    prior_event = previous.get("proof_transition") or {}
    rec = {
        "schema": 1, "outcome": "loop_transition", "source": "continuity_ledger",
        "ts": _utc_z(),
        "loop_key": _digest(row.get("anchor_key")), "loop_identity": loop_identity(row),
        "previous_identity": loop_identity(previous) if previous else None,
        "from_status": old_status, "to_status": new_status,
        "actor": "user" if str(row.get("resolution", "")).startswith("user_") else "system",
        "capture_origin": "manual" if row.get("channel") == "manual" else "automatic",
    }
    # The previous event breaks repeated open/parked/open cycles even when their dates match.
    rec["event_id"] = _digest([rec, prior_event.get("event_id")])
    return rec


def record_loop_event(rec: dict | None) -> bool:
    """Diagnostics cannot turn a successfully saved correction into a failed user action."""
    if not rec:
        return True
    try:
        log(rec)
        return True
    except (OSError, ValueError, TypeError):
        print("[continuity_proof] outcome recording failed; historical coverage is incomplete",
              file=sys.stderr)
        return False


def record_loop_proposal(update, row, result, reason, timestamp):
    """Retain the proposal identity and decision, never its message or evidence text."""
    from delivery_effects import loop_identity, loop_version, run_id

    durable_run = run_id()
    timestamp = _as_utc_z(timestamp)
    rec = {
        "schema": 1, "outcome": "loop_proposal", "source": "continuity_resolver",
        "ts": timestamp, "loop_identity": loop_identity(row) if row else None,
        "loop_key": _digest(row.get("anchor_key")) if row else None,
        "loop_version": loop_version(row) if row else None,
        "result": result, "reason": reason,
        "proposed_status": (update.get("status") if isinstance(update, dict)
                            and update.get("status") in ("resolved", "waiting") else "unknown"),
        "observation_identity": "durable_run" if durable_run else "local_invocation",
    }
    # A durable run retry is the same proposal observation. Distinct invocations without a run
    # identity remain distinct observations, not a fabricated global proposal denominator.
    rec["event_id"] = _digest([durable_run or timestamp, _digest(update), rec["loop_identity"],
                                result, reason])
    return record_loop_event(rec)


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    print(json.dumps(log(json.loads(raw))))


if __name__ == "__main__":
    main()
