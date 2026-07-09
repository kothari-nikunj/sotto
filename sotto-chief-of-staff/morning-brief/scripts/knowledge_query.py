#!/usr/bin/env python3
"""
knowledge_query.py — pack person/company knowledge for the LLM, or answer "what do I know about X".

PORT SOURCE: knowledge_files.rs::get_person_knowledge_for_llm / pack_person_compact (line 900-1075)
Used by morning-brief (load prior knowledge) and the ask/people skills.

Usage:
    knowledge_query.py --person "Sarah Chen"        # one person (name, email, or phone), expanded
    knowledge_query.py --relevant-days 7            # everyone updated in last N days, compact
--person prints JSON { "<canonical_id>": "<packed string>" }.
--relevant-days prints the NAMED form compose_brief._normalize_local consumes directly:
    { "person_knowledge": { "<canonical_id>": "<packed>" },
      "contact_index":    [ {canonical_id, display_name, identifiers, confidence} ] }
contact_index covers EVERY person file (not just recently-updated ones) — it is the identity map
that lets the brief resolve a phone and an email to the SAME person (the phone↔email bridge).
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

    try:  # legacy name-slug files → canonical_id keying (idempotent; reads work pre-first-update)
        kg.migrate_people_dir(now)
    except Exception:  # noqa: BLE001
        pass

    if args.person:
        # Accept a name, an email, or a phone — resolved through the same identity map as writes.
        path = kg.find_person_file(name=args.person, identifier=args.person)
        out = {}
        if path and os.path.exists(path):
            p = _load(path)
            out[p.canonical_id or kg.slugify(p.name)] = pack_person(p, True, now)
        print(json.dumps(out))
        return

    cutoff = now - timedelta(days=args.relevant_days)
    person_knowledge, contact_index = {}, []
    for path in glob.glob(os.path.join(kg.people_dir(), "*.md")):
        try:
            p = _load(path)
        except Exception:  # noqa: BLE001 — one unreadable file must not kill the brief's knowledge
            continue
        identifiers = [str(i) for i in p.identifiers if str(i).strip()]
        if p.canonical_id and identifiers:
            # confidence "medium": graph identifiers unify identity (canonical_id attach, known-person
            # rescue) but never override a name Apple Contacts resolved (those seed as "high").
            contact_index.append({"canonical_id": p.canonical_id, "display_name": p.name,
                                  "identifiers": identifiers, "confidence": "medium"})
        try:
            if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                continue
        except OSError:
            continue
        person_knowledge[p.canonical_id or kg.slugify(p.name)] = pack_person(p, False, now)
    print(json.dumps({"person_knowledge": person_knowledge, "contact_index": contact_index}))


if __name__ == "__main__":
    main()
