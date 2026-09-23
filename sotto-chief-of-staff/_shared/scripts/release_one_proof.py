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
import re
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


def _observed_correlations(root, rows, since, until):
    """Join retained observations, never infer unseen transitions or a user's reason."""
    events = {}
    malformed = 0

    def retain(rec):
        nonlocal malformed
        if not isinstance(rec, dict):
            malformed += 1
            return
        if rec.get("outcome") not in ("loop_transition", "loop_proposal"):
            return
        stamp = _dt(rec.get("ts"))
        ident = rec.get("event_id")
        if (rec.get("schema") != 1 or stamp is None or
                not isinstance(ident, str) or not re.fullmatch(r"[a-f0-9]{64}", ident)):
            malformed += 1
            return
        if any(value is not None and (not isinstance(value, str)
               or not re.fullmatch(r"[a-f0-9]{64}", value))
               for value in (rec.get("loop_identity"), rec.get("loop_key"))):
            malformed += 1
            return
        if rec["outcome"] == "loop_transition" and (
                not isinstance(rec.get("from_status"), str)
                or not isinstance(rec.get("to_status"), str)
                or rec.get("from_status") not in ledger_io.ACTIVE | ledger_io.TERMINAL | ledger_io.PARKED | {"absent"}
                or rec.get("to_status") not in ledger_io.ACTIVE | ledger_io.TERMINAL | ledger_io.PARKED
                or rec.get("actor") not in ("user", "system")):
            malformed += 1
            return
        if stamp <= until:
            events[ident] = rec

    exhaust = os.path.join(root, "outcomes.jsonl")
    try:
        with open(exhaust, encoding="utf-8") as handle:
            for line in handle:
                try:
                    retain(json.loads(line))
                except ValueError:
                    malformed += 1
    except FileNotFoundError:
        pass
    except OSError:
        malformed += 1
    # A saved correction is real even if the diagnostics append failed. Deduping the canonical
    # last transition and exhaust by event_id also makes ordinary retries invisible to counts.
    for row in rows:
        if isinstance(row.get("proof_transition"), dict):
            retain(row["proof_transition"])
    ordered = sorted(events.values(), key=lambda rec: (_dt(rec["ts"]), rec["event_id"]))
    transitions = [r for r in ordered if r["outcome"] == "loop_transition"]
    in_window = [r for r in ordered if since <= _dt(r["ts"]) <= until]
    if not in_window:
        return None, {"status": "unavailable", "retained_events_in_window": 0,
                      "malformed_or_unreadable": malformed,
                      "reason": "no identity-bearing observations retained in window"}

    proposals = [r for r in in_window if r["outcome"] == "loop_proposal"
                 and r.get("result") == "rejected" and r.get("proposed_status") == "resolved"
                 and r.get("reason") in ("unsupported_evidence", "stale_revision")
                 and r.get("loop_identity")]
    local_proposals = [r for r in in_window if r["outcome"] == "loop_proposal"
                       and r.get("observation_identity") != "durable_run"]
    manual = [r for r in transitions if r.get("actor") == "user"
              and r.get("to_status") == "resolved" and since <= _dt(r["ts"]) <= until]
    matched = {r["loop_identity"] for r in manual if any(
        p["loop_identity"] == r.get("loop_identity") and _dt(p["ts"]) <= _dt(r["ts"])
        for p in proposals)}
    dismissals = [r for r in transitions if r.get("actor") == "user"
                  and r.get("to_status") == "dismissed" and since <= _dt(r["ts"]) <= until]
    quick = {r["loop_identity"] for r in dismissals if r.get("loop_identity") and any(
        c.get("loop_identity") == r["loop_identity"] and c.get("from_status") == "absent"
        and c.get("capture_origin") == "automatic"
        and timedelta(0) <= _dt(r["ts"]) - _dt(c["ts"]) <= timedelta(hours=24)
        for c in transitions)}
    reopened = [r for r in transitions if r.get("from_status") in ledger_io.TERMINAL | ledger_io.PARKED
                and r.get("to_status") in ledger_io.ACTIVE]
    resurfaced = {r["loop_identity"] for r in dismissals if r.get("loop_identity") and any(
        c.get("loop_identity") == r["loop_identity"] and _dt(c["ts"]) <= _dt(r["ts"])
        for c in reopened)}
    local_note = ("; local or legacy proposal observations may repeat across invocations"
                  if local_proposals else "")
    note = ("observed review candidates only; user intent and missing historical events are unknown"
            + local_note)
    proposal_status = "candidate_observations" if local_proposals else "observed"
    return {
        "proposal_to_later_manual_resolution": {
            "status": proposal_status, "rejected_completion_proposals": len(proposals),
            "loops_later_manually_resolved": len(matched), "error_rate": None, "interpretation": note},
        "quick_dismissal_to_false_capture": {
            "status": "observed", "loops_dismissed_within_24h_of_observed_capture": len(quick),
            "error_rate": None, "interpretation": note},
        "resurfacing_after_terminal_or_parked_state": {
            "status": "observed", "reopened_loops_later_dismissed": len(resurfaced),
            "error_rate": None, "interpretation": note},
    }, {
        "status": "observed_only", "retained_events_in_window": len(in_window),
        "malformed_or_unreadable": malformed,
        "local_or_legacy_proposal_observations": len(local_proposals),
        "proposal_dedupe": ("partial_local_candidates" if local_proposals else "durable_run_identity"),
        "first_observed_at": in_window[0]["ts"], "last_observed_at": in_window[-1]["ts"],
        "coverage_note": "retained observations do not establish complete event coverage or a quality denominator",
    }


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
    invalid_step_metadata = 0
    for receipt in retained:
        steps = receipt.get("steps") or {}
        continuity = steps.get("continuity") or {} if isinstance(steps, dict) else None
        if not isinstance(continuity, dict):
            invalid_step_metadata += 1
            continue
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

    denominator_known = (successful_continuity > 0 and exact_receipts == successful_continuity
                         and invalid_step_metadata == 0)
    if denominator_known:
        proposal_outcomes = {"status": "available", "proposed": proposed,
                             "accepted": accepted, "rejected": rejected,
                             "rejected_by_reason": dict(sorted(rejection_reasons.items()))}
    elif successful_continuity == 0:
        proposal_outcomes = {"status": "unavailable", "reason": (
            "invalid continuity metadata prevents determining receipt coverage" if invalid_step_metadata else
            "no successful continuity receipts retained in window")}
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
                             "missing_receipts": successful_continuity - exact_receipts + invalid_step_metadata}
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
    observed, observation_coverage = _observed_correlations(root, rows, since, until)
    if observed is not None:
        correlations = observed
    return {
        "schema": 1,
        "mode": "read_only_existing_volume",
        "window": {"days": days, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "until": until.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "learn_receipts": {
            "retained_in_window": len(retained), "unreadable": unreadable,
            "invalid_timestamp": invalid_timestamp,
            "invalid_step_metadata": invalid_step_metadata,
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
        "correlation_observations": observation_coverage,
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
