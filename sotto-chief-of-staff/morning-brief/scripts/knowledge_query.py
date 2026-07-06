#!/usr/bin/env python3
"""
knowledge_query.py — pack person/company knowledge for the LLM, or answer "what do I know about X".

PORT SOURCE: knowledge_files.rs::get_person_knowledge_for_llm / pack_person_compact (line 900-1075)
Used by morning-brief (load prior knowledge) and the ask/people skills.

Usage:
    knowledge_query.py --person "Sarah Chen"        # one person, expanded
    knowledge_query.py --relevant-days 7            # everyone updated in last N days, compact
Prints JSON { "<slug>": "<packed string>", ... }
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib"))
import knowledge as kg  # noqa: E402


LOW_CONFIDENCE = 0.6   # below this a fact must be visibly labeled in the packed context


def _fact_text(f: kg.FactMeta, now: datetime) -> str:
    """Research facts (prewarm_graph / persist_prep, conf 0.55) bake a "Per web search:" prefix into
    the text, so they arrive pre-labeled. Any OTHER fact that is low-confidence — an LLM extraction
    scored 0.5-0.69, or one whose confidence decayed — would otherwise pack as a bare assertion;
    label it so the brief never presents an unverified fact as ground truth."""
    if kg.effective_confidence(f, now) < LOW_CONFIDENCE and not f.text.startswith("Per web search"):
        return f.text + " (unverified)"
    return f.text


def pack_person(p: kg.PersonFile, expanded: bool, now: datetime) -> str:
    lines = []
    identity = f"{p.name} ({p.canonical_id})"
    if p.title and p.company:
        identity += f" | {p.title} @ {p.company}"
    elif p.title:
        identity += f" | {p.title}"
    elif p.company:
        identity += f" | @ {p.company}"
    email = next((i for i in p.identifiers if "@" in i), None)
    if email:
        identity += f" | {email}"
    lines.append(identity)

    active = kg.sorted_active_facts(p.facts, now)
    limit = kg.MAX_FACTS_FOR_LLM if expanded else kg.MAX_FACTS_COMPACT
    seven_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    facts, included = [], set()
    for fid, f in active[:limit]:
        facts.append(_fact_text(f, now)); included.add(fid)
    if not expanded:
        for fid, f in active:
            if fid not in included and f.last >= seven_ago:
                facts.append(_fact_text(f, now))
    if facts:
        lines.append("= " + "; ".join(facts))
    if p.talking_points:
        tp = p.talking_points if expanded else p.talking_points[:kg.MAX_TALKING_POINTS_FOR_LLM]
        lines.append("> " + "; ".join(tp))
    if p.recent_activity:
        ra = p.recent_activity if expanded else p.recent_activity[:kg.MAX_RECENT_ACTIVITY_FOR_LLM]
        lines.append("~ " + "; ".join(ra))
    if expanded and p.notes:
        excerpt = p.notes[:kg.NOTES_EXCERPT_CHARS] + ("..." if len(p.notes) > kg.NOTES_EXCERPT_CHARS else "")
        lines.append("# " + excerpt)
    return "\n".join(lines)


def _load(path):
    with open(path, encoding="utf-8") as f:
        return kg.parse_person_file(f.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--person")
    ap.add_argument("--relevant-days", type=int, default=7)
    args = ap.parse_args()
    now = datetime.now()
    out = {}

    if args.person:
        target = kg.slugify(args.person)
        path = os.path.join(kg.people_dir(), f"{target}.md")
        if os.path.exists(path):
            out[target] = pack_person(_load(path), True, now)
    else:
        cutoff = now - timedelta(days=args.relevant_days)
        for path in glob.glob(os.path.join(kg.people_dir(), "*.md")):
            if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                continue
            p = _load(path)
            out[kg.slugify(p.name)] = pack_person(p, False, now)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
