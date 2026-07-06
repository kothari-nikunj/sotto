#!/usr/bin/env python3
"""
persist_prep.py — stop throwing away meeting-prep attendee research.

Two jobs, both deterministic:

  1. PERSIST (default): write the grounded output of research_attendees.py into the knowledge graph
     so the same people aren't re-researched every prep run. Facts come ONLY from the research JSON
     (title/company/summary) — never invented — and mirror prewarm_graph.py's pattern exactly:
     clearly-sourced "Per web search: …" facts at LOW confidence (0.55) that decay, applied through
     knowledge_update.apply() (dedupe/bump/supersede + decay all included). They are never written
     as authoritative identity fields; the brief's Learn step promotes them as they're confirmed.

       persist_prep.py --research /tmp/sotto_research.json [--attendees /tmp/sotto_research_in.json]

     (--attendees maps email → display name for nicer graph filenames; research output has no name.)

  2. FILTER (--filter-fresh): rewrite the select_attendees output IN PLACE, dropping attendees whose
     graph profile is already FRESH — `last_researched` (stamped by persist below) < 30 days old —
     so step 2 of the SKILL skips re-researching them.

       persist_prep.py --filter-fresh /tmp/sotto_research_in.json

Prints JSON either way. Degrades to a no-op (exit 0) on missing/empty inputs.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "_shared", "lib"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "_shared", "scripts"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "morning-brief", "scripts"))
import compose_brief as cb  # noqa: E402
import knowledge as kg  # noqa: E402
import knowledge_update as ku  # noqa: E402

FRESH_DAYS = 30                 # matches the Mac attendee-cache TTL
RESEARCH_CONFIDENCE = 0.55      # prewarm_graph.py — low-confidence research facts that decay
SOURCE_PREFIX = "meeting-prep-research"

_s = cb._s   # the shared string coercion, not a private copy


def _find_profile(name: str, email: str):
    """Locate a person's graph file by name slug, else by email in identifiers. Returns path or None."""
    slug = kg.safe_slug(name or "")
    if slug:
        try:
            path = kg.safe_path(kg.people_dir(), slug)
            if os.path.exists(path):
                return path
        except ValueError:
            pass
    email = (email or "").strip().lower()
    if not email:
        return None
    for path in glob.glob(os.path.join(kg.people_dir(), "*.md")):
        try:
            with open(path, encoding="utf-8") as f:
                p = kg.parse_person_file(f.read())
            if any(_s(i).strip().lower() == email for i in p.identifiers):
                return path
        except Exception:  # noqa: BLE001 — one unreadable file must not kill the filter
            continue
    return None


def profile_is_fresh(name: str, email: str, now: datetime | None = None) -> bool:
    """True ONLY when this person was actually RESEARCHED within FRESH_DAYS, per the profile's
    `last_researched` stamp (written by persist() below and prewarm_graph's research path). File
    mtime is deliberately NOT used — every brief rewrite bumps it, which made stale research look
    permanently fresh — and a mere company/title doesn't count either. A profile with no parseable
    `last_researched` (incl. every legacy profile) is not fresh: one re-research is correct."""
    path = _find_profile(name, email)
    if not path:
        return False
    now = now or datetime.now(timezone.utc)
    try:
        with open(path, encoding="utf-8") as f:
            p = kg.parse_person_file(f.read())
    except Exception:  # noqa: BLE001
        return False
    try:
        researched = datetime.strptime(_s(p.last_researched).strip()[:10], "%Y-%m-%d")
    except ValueError:
        return False
    age_days = (now - researched.replace(tzinfo=timezone.utc)).total_seconds() / 86400.0
    return age_days < FRESH_DAYS


def filter_fresh(attendees: list, now: datetime | None = None) -> tuple[list, list]:
    """(kept, skipped_names) — drop attendees whose graph profile is fresh."""
    kept, skipped = [], []
    for a in attendees:
        if not isinstance(a, dict):
            continue
        name, email = _s(a.get("name")), _s(a.get("email")).lower()
        if profile_is_fresh(name, email, now):
            skipped.append(name or email)
        else:
            kept.append(a)
    return kept, skipped


def persist(research: dict, attendees_in: list | None = None, now: datetime | None = None) -> dict:
    """Write the research output's grounded bits into the graph via knowledge_update. Attendees with
    NO grounded content (no title/company/summary) are skipped entirely — nothing is invented."""
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    names_by_email = {}
    for a in attendees_in or []:
        if isinstance(a, dict) and _s(a.get("email")):
            names_by_email[_s(a.get("email")).strip().lower()] = _s(a.get("name")).strip()

    updates = []
    for a in (research or {}).get("attendees", []):
        if not isinstance(a, dict):
            continue
        email = _s(a.get("email")).strip().lower()
        name = names_by_email.get(email) or _s(a.get("name")).strip() \
            or (email.split("@")[0] if email else "")
        if not name and not email:
            continue
        # Grounded bits ONLY — from the research output, clearly sourced (prewarm_graph pattern).
        # ONE combined fact per attendee: the shared "Per web search" prefix would otherwise make
        # separate title/summary facts dedupe-collide inside knowledge_update.
        bits = []
        title, company = _s(a.get("title")).strip(), _s(a.get("company")).strip()
        if title or company:
            bits.append(" at ".join(x for x in (title, company) if x))
        if _s(a.get("summary")).strip():
            bits.append(_s(a.get("summary")).strip())
        if not bits:
            continue   # nothing grounded → nothing to persist
        updates.append({
            "person_name": name or email,
            "identifier": email,
            "last_researched": today,   # freshness stamp profile_is_fresh keys off (not mtime)
            "facts": [{"fact": "Per web search: " + " — ".join(bits),
                       "confidence": RESEARCH_CONFIDENCE, "memory_type": "context",
                       "source_ref": f"{SOURCE_PREFIX}:{today}"}],
        })

    if updates:
        ku.apply({"person_updates": updates})
    return {"persisted": len(updates), "people": [u["person_name"] for u in updates]}


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        return json.loads(raw) if raw.strip() else None
    except (OSError, json.JSONDecodeError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--research", help="research_attendees.py output ({attendees:[…]})")
    ap.add_argument("--attendees", help="select_attendees.py output (email → name mapping)")
    ap.add_argument("--filter-fresh", dest="filter_fresh",
                    help="select_attendees output file; rewritten IN PLACE minus fresh profiles")
    a = ap.parse_args()

    if a.filter_fresh:
        data = _load(a.filter_fresh)
        attendees = data if isinstance(data, list) else []
        kept, skipped = filter_fresh(attendees)
        if skipped:   # only rewrite when something changed
            with open(a.filter_fresh, "w", encoding="utf-8") as f:
                json.dump(kept, f)
        print(json.dumps({"kept": len(kept), "skipped_fresh": skipped}))
        return

    research = _load(a.research) if a.research else None
    if not isinstance(research, dict):
        research = {"attendees": research} if isinstance(research, list) else {"attendees": []}
    attendees_in = _load(a.attendees) if a.attendees else None
    if not isinstance(attendees_in, list):
        attendees_in = []
    print(json.dumps(persist(research, attendees_in)))


if __name__ == "__main__":
    main()
