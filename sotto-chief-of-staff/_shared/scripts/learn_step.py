#!/usr/bin/env python3
"""
learn_step.py — the brief's Learn step as ONE command that leaves a receipt.

In one sentence: a brief that delivered without writing its memory is a failed brief, and says so.

The seven Learn writers (knowledge, continuity, draft outcomes, style, meeting commitments,
meeting attendance, contacts) used
to be six separate instructions in the skill — "run ALL SIX" — with nothing verifying that any of
them ran. The knowledge, draft outcomes and style loops all rest on that step, and a run that skipped
it (the way runs skipped the deliver-once claim, four times in one week) left the graph a day
colder with every page still reading green. This runner is the machinery: the agent runs ONE
command, every writer runs in order whether or not the agent remembered it, and the receipt
`$SOTTO_DATA/briefs/<day>.<kind>.learned.json` records what ran and what failed. The receiver reads
the receipt when the brief's send is acknowledged and logs, loudly, a brief that delivered without
one.

Usage:
  learn_step.py --type morning|evening --local /tmp/sotto_local.json
                [--gmail /tmp/sotto_gmail.json] [--granola /tmp/sotto_granola.json]
                [--knowledge-out /tmp/sotto_know_out.json] [--continuity /tmp/sotto_cont.json]

Steps, in order (a step whose input file is absent is recorded `skipped`, never `failed`):
  knowledge    knowledge_update.py <knowledge-out>        the brief's extracted_knowledge
  continuity   continuity_resolve.py --merge-only <cont>  the brief's actions[] → the ledger
  style        style_extract.py <local> --gmail <gmail>   what you actually sent
  drafts       draft_outcomes.py                          drafts matched to sends → outcomes + voice confirms
  commitments  capture_commitments.py --granola <granola> new/changed notes → canonical obligations
  granola      granola_graph.py --granola <granola>       who you actually sat with
  contacts     prewarm_graph.py --sync-contacts           identifiers, notes, birthdays

Prints the receipt as JSON. Exit 0 when every step that had input ran clean; exit 1 when any step
failed — the receipt is written either way, so a failure is on the volume, not just in a log line.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.abspath(os.path.join(HERE, "..", ".."))            # sotto-chief-of-staff/
sys.path.insert(0, os.path.join(PACK, "_shared", "lib"))
from timeutil import configured_tz, _user_local_date  # noqa: E402

ESSENTIAL_STEPS = frozenset({"knowledge", "continuity"})

STEP_TIMEOUT_SECS = 300      # one writer, generously: knowledge_update on a big day is seconds
COMMITMENTS_TIMEOUT_SECS = 540  # fit inside brief_runner's 600s ancillary-process ceiling

# name → (script path relative to the pack, argv builder taking the parsed args)
STEPS = (
    ("knowledge", ("_shared", "knowledge", "knowledge_update.py"),
     lambda a: [a.knowledge_out] if a.knowledge_out else None),
    ("continuity", ("morning-brief", "scripts", "continuity_resolve.py"),
     lambda a: ["--merge-only", a.continuity] if a.continuity else None),
    # --gmail is OPTIONAL to style: a missing sent-mail file (a Gmail-less day, a failed gather)
    # must not skip the whole voice writer — style_extract itself tolerates the absent file.
    ("style", ("_shared", "scripts", "style_extract.py"),
     lambda a: ([a.local] + (["--gmail", a.gmail] if _present(a.gmail) else [])) if a.local else None),
    # Grade only after extraction: a newly observed verbatim send can be confirmed in this pass.
    ("drafts", ("_shared", "scripts", "draft_outcomes.py"),
     lambda a: []),
    ("commitments", ("followup", "scripts", "capture_commitments.py"),
     lambda a: ["--granola", a.granola] if a.granola else None),
    ("granola", ("_shared", "scripts", "granola_graph.py"),
     lambda a: ["--granola", a.granola] if a.granola else None),
    ("contacts", ("_shared", "scripts", "prewarm_graph.py"),
     lambda a: ["--sync-contacts"]),
)


def _present(path: str) -> bool:
    return bool(path) and os.path.exists(path)


def receipt_path(day: str, kind: str) -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "briefs", f"{day}.{kind}.learned.json")


def _run_one(script: str, argv: list, run=subprocess.run) -> dict:
    try:
        timeout = (COMMITMENTS_TIMEOUT_SECS if os.path.basename(script) == 'capture_commitments.py'
                   else STEP_TIMEOUT_SECS)
        r = run([sys.executable or "python3", script, *argv], capture_output=True, text=True,
                timeout=timeout, env=os.environ)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "detail": f"timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001 — a missing interpreter is a failed step, not a crash
        return {"status": "failed", "detail": f"{type(e).__name__}: {e}"}
    tail = (r.stderr or r.stdout or "").strip().splitlines()
    detail = tail[-1][:300] if tail else ""
    proof = None
    if r.returncode == 0 and os.path.basename(script) == "continuity_resolve.py":
        # The CLI ends with a generic open/resolved count. Preserve the preceding content-free
        # rejection summary in the existing receipt so a successful run cannot hide refused work.
        detail = next((line[:300] for line in reversed(tail)
                       if line.startswith("[continuity_resolve] rejected loop updates: ")), detail)
        prefix = "[continuity_resolve] loop update outcomes: "
        summary = next((line[len(prefix):] for line in reversed(tail) if line.startswith(prefix)), "")
        if summary:
            try:
                parsed = json.loads(summary)
                if (isinstance(parsed, dict)
                        and all(isinstance(parsed.get(k), int) and parsed[k] >= 0
                                for k in ("total", "accepted", "rejected"))
                        and parsed["total"] == parsed["accepted"] + parsed["rejected"]
                        and isinstance(parsed.get("rejected_by_reason", {}), dict)):
                    proof = {"loop_updates": parsed, "coverage": "exact_resolver_outcomes"}
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    if os.path.basename(script) == "capture_commitments.py":
        try:
            parsed = json.loads(r.stdout.strip().splitlines()[-1])
            revisions = parsed.get('meeting_revisions')
            stable_revisions = parsed.get('stable_meeting_revisions', [])
            failed_revisions = parsed.get('failed_meeting_revisions', [])
            if (isinstance(revisions, list) and all(isinstance(value, str) for value in revisions)
                    and isinstance(failed_revisions, list)
                    and isinstance(stable_revisions, list)
                    and all(isinstance(value, str) for value in stable_revisions)
                    and all(isinstance(value, str) for value in failed_revisions)
                    and all(isinstance(parsed.get(key), int) and parsed[key] >= 0
                            for key in ('examined', 'written', 'deduped', 'skipped_terminal'))):
                proof = {'coverage': ('successful_meeting_revisions' if r.returncode == 0
                                      else 'partial_successful_meeting_revisions'),
                         'meeting_revisions': revisions,
                         'stable_meeting_revisions': stable_revisions,
                         'failed_meeting_revisions': failed_revisions,
                         'examined': parsed['examined'], 'written': parsed['written'],
                         'deduped': parsed['deduped'],
                         'skipped_terminal': parsed['skipped_terminal']}
                # Do not let the last successful model timing line hide a later writer failure.
                # Only the class and opaque revision survive; no exception text or source notes.
                failures = parsed.get('failures', [])
                proof['failures'] = [
                    {'meeting_revision': row['meeting_revision'], 'error': row['error']}
                    for row in failures if isinstance(row, dict)
                    and row.get('meeting_revision') in failed_revisions
                    and isinstance(row.get('error'), str)
                    and row['error'].isidentifier() and len(row['error']) <= 80
                ] if isinstance(failures, list) else []
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            pass
    result = {"status": "ok" if r.returncode == 0 else "failed", "exit": r.returncode,
              "detail": detail}
    if proof is not None:
        result["proof"] = proof
    return result


def learn(args, run=subprocess.run, now=None) -> dict:
    """Run every step; return the receipt (also written to the volume)."""
    day = args.day or _user_local_date(configured_tz())
    phase = getattr(args, "phase", "all")
    steps = {}
    for name, rel, build in STEPS:
        if phase == "essential" and name not in ESSENTIAL_STEPS:
            steps[name] = {"status": "queued", "detail": "durable follow-up after composition"}
            continue
        if phase == "ancillary" and name in ESSENTIAL_STEPS:
            continue
        argv = build(args)
        inputs = [a for a in (argv or []) if not a.startswith("--")]
        if argv is None or any(not _present(p) for p in inputs):
            steps[name] = {"status": "skipped", "detail": "no input file"}
            continue
        script = os.path.join(PACK, *rel)
        if not os.path.exists(script):
            steps[name] = {"status": "failed", "detail": f"{os.path.join(*rel)} not found"}
            continue
        steps[name] = _run_one(script, argv, run)
        print(f"[learn_step] {name}: {steps[name]['status']}"
              + (f" — {steps[name]['detail']}" if steps[name]["status"] != "ok" and steps[name]["detail"] else ""),
              file=sys.stderr, flush=True)
    receipt = {"day": day, "kind": args.type,
               "ts": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "steps": steps,
               "ok": all(s["status"] != "failed" for s in steps.values())}
    if phase != "all":
        receipt.update(phase=phase, run_key=getattr(args, "run_key", ""),
                       required_ok=phase == "essential" and receipt["ok"])
    _write_receipt(receipt)
    return receipt


def _write_receipt(receipt: dict) -> None:
    """Merge follow-up results into the same run's durable receipt through one locked writer."""
    sys.path.insert(0, os.path.join(PACK, "_shared", "lib"))
    import jsonstore
    path = receipt_path(receipt["day"], receipt["kind"])
    try:
        with jsonstore.transaction(path, default={}) as state:
            if receipt.get("phase") == "ancillary":
                if state.get("run_key") != receipt.get("run_key"):
                    # A later brief owns today's display receipt; this old job must not replace it.
                    return
                receipt["steps"] = {**state.get("steps", {}), **receipt["steps"]}
                receipt["required_ok"] = state.get("required_ok") is True
                receipt["ok"] = receipt["required_ok"] and all(
                    step.get("status") != "failed" for step in receipt["steps"].values())
            state.clear()
            state.update(receipt)
    except (OSError, jsonstore.Unreadable) as error:
        print(f"[learn_step] receipt not written: {type(error).__name__}", file=sys.stderr, flush=True)
        if receipt.get('phase') in ('essential', 'ancillary'):
            # A durable runner must retry when its completion receipt did not reach the volume.
            # The legacy all-writers command retains its historical best-effort log behavior.
            raise



def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--type", default="morning", choices=("morning", "evening", "welcome"))
    ap.add_argument("--local", default="", help="the read_local snapshot (style)")
    ap.add_argument("--gmail", default="", help="the gather's Gmail file (style's sent-mail lane)")
    ap.add_argument("--granola", default="", help="gather_granola output (meeting attendance)")
    ap.add_argument("--knowledge-out", dest="knowledge_out", default="",
                    help="the brief's extracted_knowledge payload (knowledge)")
    ap.add_argument("--continuity", default="", help="the --merge-only payload (continuity)")
    ap.add_argument("--phase", choices=("all", "essential", "ancillary"), default="all",
                    help="essential state gates delivery; ancillary learning runs as durable follow-up")
    ap.add_argument("--run-key", default="", help=argparse.SUPPRESS)
    ap.add_argument("--day", default="", help=argparse.SUPPRESS)   # tests pin the day
    args = ap.parse_args(argv)
    receipt = learn(args)
    print(json.dumps(receipt))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
