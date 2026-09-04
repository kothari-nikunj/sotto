#!/usr/bin/env python3
"""
learn_step.py — the brief's Learn step as ONE command that leaves a receipt.

In one sentence: a brief that delivered without writing its memory is a failed brief, and says so.

The six Learn writers (knowledge, continuity, preferences, style, meeting attendance, contacts) used
to be six separate instructions in the skill — "run ALL SIX" — with nothing verifying that any of
them ran. The knowledge, preferences and style loops all rest on that step, and a run that skipped
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
  preferences  learn_preferences.py                       drafts matched to sends → outcomes → rules
  style        style_extract.py <local> --gmail <gmail>   what you actually sent
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

STEP_TIMEOUT_SECS = 300      # one writer, generously: knowledge_update on a big day is seconds

# name → (script path relative to the pack, argv builder taking the parsed args)
STEPS = (
    ("knowledge", ("_shared", "knowledge", "knowledge_update.py"),
     lambda a: [a.knowledge_out] if a.knowledge_out else None),
    ("continuity", ("morning-brief", "scripts", "continuity_resolve.py"),
     lambda a: ["--merge-only", a.continuity] if a.continuity else None),
    ("preferences", ("approval-tiers", "scripts", "learn_preferences.py"),
     lambda a: []),
    # --gmail is OPTIONAL to style: a missing sent-mail file (a Gmail-less day, a failed gather)
    # must not skip the whole voice writer — style_extract itself tolerates the absent file.
    ("style", ("_shared", "scripts", "style_extract.py"),
     lambda a: ([a.local] + (["--gmail", a.gmail] if _present(a.gmail) else [])) if a.local else None),
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
        r = run([sys.executable or "python3", script, *argv], capture_output=True, text=True,
                timeout=STEP_TIMEOUT_SECS, env=os.environ)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "detail": f"timed out after {STEP_TIMEOUT_SECS}s"}
    except Exception as e:  # noqa: BLE001 — a missing interpreter is a failed step, not a crash
        return {"status": "failed", "detail": f"{type(e).__name__}: {e}"}
    tail = (r.stderr or r.stdout or "").strip().splitlines()
    detail = tail[-1][:300] if tail else ""
    return {"status": "ok" if r.returncode == 0 else "failed", "exit": r.returncode, "detail": detail}


def learn(args, run=subprocess.run, now=None) -> dict:
    """Run every step; return the receipt (also written to the volume)."""
    day = args.day or _user_local_date(configured_tz())
    steps = {}
    for name, rel, build in STEPS:
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
    _write_receipt(receipt)
    return receipt


def _write_receipt(receipt: dict) -> None:
    """tmp + os.replace like every other writer on the volume. Best-effort: a receipt that could
    not be written is exactly what the receiver's check reports, so nothing is hidden by raising."""
    path = receipt_path(receipt["day"], receipt["kind"])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(receipt, f)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[learn_step] receipt not written: {e}", file=sys.stderr, flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--type", default="morning", choices=("morning", "evening"))
    ap.add_argument("--local", default="", help="the read_local snapshot (style)")
    ap.add_argument("--gmail", default="", help="the gather's Gmail file (style's sent-mail lane)")
    ap.add_argument("--granola", default="", help="gather_granola output (meeting attendance)")
    ap.add_argument("--knowledge-out", dest="knowledge_out", default="",
                    help="the brief's extracted_knowledge payload (knowledge)")
    ap.add_argument("--continuity", default="", help="the --merge-only payload (continuity)")
    ap.add_argument("--day", default="", help=argparse.SUPPRESS)   # tests pin the day
    args = ap.parse_args(argv)
    receipt = learn(args)
    print(json.dumps(receipt))
    return 0 if receipt["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
