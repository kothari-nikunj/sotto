#!/usr/bin/env python3
"""Live judgment probe on invented regressions, never a private inbox export or a stub accuracy score.

Uses the real event classifier, digest review/selection, and native brief extraction (same loaded
system, renderer and response schema). No learning, writes to user state, or message delivery.
The later-reply fixtures supply full thread context explicitly to the event classifier; this
measures judgment given evidence, not the completeness of the realtime context gather.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
import os
import re
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
for part in ("lib", "scripts", "knowledge"):
    sys.path.insert(0, str(ROOT / "_shared" / part))

import compose_brief as cb  # noqa: E402
import gemini  # noqa: E402
import relevance  # noqa: E402


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cases():
    return json.loads((ROOT / "evals/fixtures/relevance_cases.json").read_text())


def run():
    # The shared classifier and brief must reason about the same frozen fixture clock.
    clock = relevance._now_local
    previous = os.environ.get('SOTTO_DATA')
    try:
        relevance._now_local = lambda tz: datetime.fromisoformat("2026-09-07T15:30:00-07:00")
        with tempfile.TemporaryDirectory(prefix='sotto-relevance-eval-') as tmp:
            os.environ['SOTTO_DATA'] = tmp
            # Exercise production consent projection with invented, explicitly enabled sources.
            # Never copy the live tenant's permissions or write a receipt to its data directory.
            config = Path(tmp) / 'config'
            config.mkdir()
            (config / 'managed-capabilities.json').write_text(json.dumps({
                'tenant_id': os.environ.get('SOTTO_TENANT_ID'),
                'sources': {s: {'consented': True, 'connected': True}
                            for s in ('imessage', 'contacts')}}))
            return _run()
    finally:
        relevance._now_local = clock
        if previous is None:
            os.environ.pop('SOTTO_DATA', None)
        else:
            os.environ['SOTTO_DATA'] = previous


def _run():
    data = cases()
    te = load_script("eval_triage", "event-triage/scripts/triage_event.py")
    dc = load_script("eval_digest", "event-triage/scripts/digest_check.py")
    results = []
    entries, contacts, imessages = [], [], []
    for i, case in enumerate(data):
        phone = f"+120255501{i:02d}"
        contacts.append({"name": case["sender"], "phones": [phone]})
        thread = []
        for j, msg in enumerate(case["messages"]):
            outgoing = bool(msg.get("is_from_me"))
            # Fixed historic clock and invented numbers, with real chronological ordering.
            ts = f"2026-09-07T21:{10+j:02d}:00Z"
            text = ("Background: " + case["context"] + "\n" if j == 0 else "") + msg["text"]
            ev = {"source": "imessage", "handle": phone, "text": text,
                  "is_from_me": outgoing, "timestamp": ts, "rowid": i * 10 + j,
                  "is_group_chat": False}
            imessages.append(ev)
            entries.append({"ts": ts, "sender": case["sender"],
                            "verdict_class": "signal" if outgoing else "ambient", "event": ev})
            thread.append(("USER: " if outgoing else "THEY: ") + msg["text"])
        ev = {"source": "imessage", "text": "\n".join(thread)}
        _, cls, _ = te._classify_tier1(ev, case["context"])
        results.append({"case": case["id"], "surface": "nudge", "observed": cls,
                        "expected": case["expected"], "pass": cls in case["expected"]})

    review = dc.review_conversations
    def recorded(conversations):
        judgments = review(conversations)
        by_id = {c["id"]: c["sender"] for c in conversations}
        by_sender = {c["sender"]: c for c in data}
        for j in judgments:
            case = by_sender[by_id[j["id"]]]
            results.append({"case": case["id"], "surface": "digest", "observed": j["class"],
                            "expected": case["expected"], "pass": j["class"] in case["expected"]})
        return judgments
    dc.review_conversations = recorded
    out = dc.check(sorted(entries, key=lambda e: e["ts"]), min_n=1)
    expected_senders = {c["sender"] for c in data if c["brief_action"]}
    results.append({"case": "selected_items", "surface": "digest",
                    "pass": {i["sender"] for i in out.get("items", [])} == expected_senders})

    inputs = {"type": "morning", "first_run": False, "now": "2026-09-07T22:30:00Z",
              "userTimezone": "America/Los_Angeles", "google": {"emails": [], "events": []},
              "local": {"contacts": contacts, "imessage": imessages}}
    system, template = cb._split_prompt(cb._load_prompt())
    prompt = cb.build_prompt(template, inputs)
    brief = json.loads(gemini.call_gemini(prompt, inputs, system=system, schema=cb.BRIEF_RESPONSE_SCHEMA))
    actions = brief.get("actionItems", [])
    # Judge what the user actually sees, not optional metadata fields the schema does not require.
    markdown = brief.get("markdown", "")
    attention = "\n".join(re.findall(
        r"(?ms)^## (?:Needs Attention Now|Should Handle Today)[ \t]*\n(.*?)(?=^## |\Z)", markdown))
    for i, case in enumerate(data):
        included = case["sender"] in attention or f"id:+120255501{i:02d}|" in attention
        results.append({"case": case["id"], "surface": "brief", "observed": included,
                        "expected": case["brief_action"], "pass": included == case["brief_action"]})
    return {"live": True, "private_data": False, "messages_sent": 0,
            "brief_actions": [{k: a.get(k) for k in ("contactName", "contactIdentifier", "sectionType", "contextAsk")} for a in actions],
            "brief_markdown": brief.get("markdown", ""),
            "passed": sum(r["pass"] for r in results), "total": len(results), "results": results}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="make paid native model calls")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    if not args.live:
        print(json.dumps({"live": False, "cases": [c["id"] for c in cases()],
                          "note": "No judgment evaluated; pass --live to measure model behavior."}))
        return 0
    # The renderer may read knowledge/prefs. Isolate it from the user's actual volume.
    report = run()
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)
    return int(report["passed"] != report["total"])


if __name__ == "__main__":
    raise SystemExit(main())
