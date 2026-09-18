#!/usr/bin/env python3
"""Read-only release-one proof counters over an existing Sotto volume.

The report separates exact retained event counters from current-state proxies. It never treats an
absent historical artifact as zero and never attributes a quick dismissal to a false capture or an
explicit user resolution to missed automatic recognition.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import ledger_io  # noqa: E402
from timeutil import _parse_ts  # noqa: E402


def _dt(value):
    parsed = _parse_ts(str(value or ""))
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _in_window(value, since, until):
    parsed = _dt(value)
    return parsed is not None and since <= parsed <= until


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def build_report(data_root=None, days=30, now=None):
    root = os.path.abspath(data_root or os.environ.get("SOTTO_DATA", "/data"))
    until = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = until - timedelta(days=days)

    receipt_paths = sorted(glob.glob(os.path.join(root, "briefs", "*.learned.json")))
    retained, unreadable, invalid_timestamp, outside = [], 0, 0, 0
    for path in receipt_paths:
        receipt = _read_json(path)
        if receipt is None:
            unreadable += 1
            continue
        timestamp = _dt(receipt.get("ts"))
        if timestamp is None:
            invalid_timestamp += 1
        elif since <= timestamp <= until:
            retained.append(receipt)
        else:
            outside += 1

    proposed = accepted = rejected = 0
    rejection_reasons = Counter()
    exact_receipts = 0
    successful_continuity = 0
    for receipt in retained:
        continuity = (receipt.get("steps") or {}).get("continuity") or {}
        if continuity.get("status") == "ok":
            successful_continuity += 1
        proof_envelope = continuity.get("proof") or {}
        proof = proof_envelope.get("loop_updates") if isinstance(proof_envelope, dict) else None
        if continuity.get("status") != "ok":
            continue
        if (not isinstance(proof_envelope, dict)
                or proof_envelope.get("coverage") != "exact_resolver_outcomes"
                or not isinstance(proof, dict)):
            continue
        values = [proof.get(key) for key in ("total", "accepted", "rejected")]
        reasons = proof.get("rejected_by_reason", {})
        if (not all(type(value) is int and value >= 0 for value in values)
                or values[0] != values[1] + values[2] or not isinstance(reasons, dict)
                or not all(isinstance(key, str) and type(value) is int and value >= 0
                           for key, value in reasons.items())
                or sum(reasons.values()) != values[2]):
            continue
        exact_receipts += 1
        proposed += proof.get("total", 0)
        accepted += proof.get("accepted", 0)
        rejected += proof.get("rejected", 0)
        rejection_reasons.update(reasons)

    # Read the requested root directly. `ledger_io.load_entries()` follows process SOTTO_DATA;
    # changing that global just to inspect another volume would violate this command's read-only,
    # side-effect-free contract.
    rows = []
    for path in sorted(glob.glob(os.path.join(root, "knowledge", "continuity", "*.md"))):
        try:
            with open(path, encoding="utf-8") as handle:
                row = ledger_io.parse_frontmatter(handle.read())
        except OSError:
            row = None
        if isinstance(row, dict):
            rows.append(row)
    current_status = Counter(str(row.get("status") or "open") for row in rows)
    recent_terminal = [row for row in rows
                       if row.get("status") in ledger_io.TERMINAL
                       and _in_window(row.get("resolved_at") or row.get("closed_at"), since, until)]
    explicit_mode = [row for row in recent_terminal if row.get("resolution_mode") == "explicit"]
    user_resolution = [row for row in recent_terminal
                       if str(row.get("resolution") or "").startswith("user_")]
    quick = []
    for row in recent_terminal:
        created = _dt(row.get("created_at"))
        closed = _dt(row.get("resolved_at") or row.get("closed_at"))
        if created and closed and 0 <= (closed.date() - created.date()).days <= 1:
            quick.append(row)

    denominator_known = successful_continuity > 0 and exact_receipts == successful_continuity
    if denominator_known:
        proposal_outcomes = {"status": "available", "proposed": proposed,
                             "accepted": accepted, "rejected": rejected,
                             "rejected_by_reason": dict(sorted(rejection_reasons.items()))}
    elif successful_continuity == 0:
        proposal_outcomes = {"status": "unavailable", "reason": "no successful continuity receipts retained in window"}
    elif exact_receipts == 0:
        proposal_outcomes = {"status": "unavailable",
                             "reason": "retained continuity receipts predate exact proposal counters",
                             "proposed": None, "accepted": None, "rejected": None,
                             "rejected_by_reason": None,
                             "missing_receipts": successful_continuity}
    else:
        proposal_outcomes = {"status": "partial", "proposed": proposed,
                             "accepted": accepted, "rejected": rejected,
                             "rejected_by_reason": dict(sorted(rejection_reasons.items())),
                             "missing_receipts": successful_continuity - exact_receipts}
    correlations = {
        "proposal_to_later_manual_resolution": {
            "status": "unavailable",
            "reason": "ledger transitions overwrite prior state and retained rows do not identify proposal attempts",
        },
        "quick_dismissal_to_false_capture": {
            "status": "unavailable",
            "reason": "elapsed time is observable but user intent is not recorded",
        },
        "resurfacing_after_terminal_or_parked_state": {
            "status": "unavailable",
            "reason": "current ledger rows overwrite earlier status transitions",
        },
    }
    return {
        "schema": 1,
        "mode": "read_only_existing_volume",
        "window": {"days": days, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "until": until.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "learn_receipts": {
            "retained_in_window": len(retained), "unreadable": unreadable,
            "invalid_timestamp": invalid_timestamp,
            "retained_outside_window": outside,
            "first_retained_at": min((str(r.get("ts")) for r in retained), default=None),
            "last_retained_at": max((str(r.get("ts")) for r in retained), default=None),
            "retained_observation_days": sorted({str(r.get("ts"))[:10] for r in retained}),
            "coverage_note": "retained receipt dates show available observations, not proof every scheduled brief ran",
            "continuity_ok": successful_continuity,
            "with_exact_proposal_outcomes": exact_receipts,
            "proposal_outcomes": proposal_outcomes,
        },
        "ledger_snapshot_proxies": {
            "coverage": "current_rows_only_transitions_not_retained",
            "rows": len(rows), "status": dict(sorted(current_status.items())),
            "terminal_rows_closed_in_window": len(recent_terminal),
            "explicit_mode_terminal_rows": len(explicit_mode),
            "user_resolution_rows": len(user_resolution),
            "closed_within_one_calendar_day_of_created_rows": len(quick),
            "interpretation": "observable proxies only; neither count establishes model error or user intent",
        },
        "correlations": correlations,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="", help="existing Sotto volume (default: SOTTO_DATA or /data)")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--now", default="", help="UTC ISO timestamp for deterministic probes")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be positive")
    now = _dt(args.now) if args.now else None
    if args.now and now is None:
        parser.error("--now must be an ISO timestamp")
    print(json.dumps(build_report(args.data_root or None, args.days, now), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
