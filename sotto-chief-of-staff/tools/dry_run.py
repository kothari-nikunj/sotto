#!/usr/bin/env python3
"""
dry_run.py — exercise the full brief loop offline (no live LLM / Hermes / Mac).

Simulates what the morning-brief skill does after extraction: takes a recorded
"extraction result" fixture (what Gemini would return over {google, granola, local}),
applies it to the knowledge graph + continuity ledger, then renders a brief from the
actions and prints what the user would receive. Proves the deterministic core end-to-end.

Usage: SOTTO_DATA=/tmp/exhaust python3 tools/dry_run.py [fixtures/brief_bundle.json]
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "_shared", "lib"))


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render_brief(actions: list, resolved: dict) -> str:
    sections = {"needs_attention": [], "should_handle": [], "already_handled": [], "fyi": []}
    for a in actions:
        sections.get(a.get("section", "fyi"), sections["fyi"]).append(a)
    out = ["# Your brief\n"]
    titles = {"needs_attention": "Needs attention", "should_handle": "I can handle",
              "already_handled": "Already handled", "fyi": "FYI"}
    for key, title in titles.items():
        items = sections[key]
        if key == "already_handled":
            items = items + [{"summary": f"{r['contact_name']}: {r['resolution']}"} for r in resolved.get("resolved", [])]
        if not items:
            continue
        out.append(f"\n## {title}")
        for it in items:
            out.append(f"- {it.get('summary','')}" + (f" ({it['channel']})" if it.get("channel") else ""))
    return "\n".join(out)


def run(bundle: dict) -> dict:
    ku = _load("morning-brief/scripts/knowledge_update.py", "knowledge_update")
    cr = _load("morning-brief/scripts/continuity_resolve.py", "continuity_resolve")

    applied = ku.apply(bundle.get("extracted_knowledge", {}))
    resolved = cr.resolve({
        "today": bundle.get("today", "2026-06-23"),
        "signals": bundle.get("signals", {}),
        "new_actions": bundle.get("actions", []),
    })
    brief = render_brief(bundle.get("actions", []), resolved)
    return {"brief_markdown": brief, "knowledge_applied": applied,
            "continuity": {k: len(v) for k, v in resolved.items()}}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "fixtures", "brief_bundle.json")
    with open(path) as f:
        bundle = json.load(f)
    result = run(bundle)
    print(result["brief_markdown"])
    print("\n---\nknowledge:", json.dumps(result["knowledge_applied"]["applied"]))
    print("continuity:", json.dumps(result["continuity"]))


if __name__ == "__main__":
    main()
