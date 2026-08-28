#!/usr/bin/env python3
"""
knowledge_query.py — pack person/company knowledge for the LLM, or answer "what do I know about X".

PORT SOURCE: knowledge_files.rs::get_person_knowledge_for_llm / pack_person_compact (line 900-1075)
Used by morning-brief (load prior knowledge) and the ask/people skills.

Usage:
    knowledge_query.py --person "Sarah Chen"        # one person (name, email, or phone), expanded
    knowledge_query.py --calendar /tmp/sotto_cal.json --gmail /tmp/sotto_gmail.json   # today's cast
    knowledge_query.py --relevant-days 7            # fallback: everyone updated in last N days
--person prints JSON { "<canonical_id>": "<packed string>" }.
The default mode prints the NAMED form compose_brief._normalize_local consumes directly:
    { "person_knowledge":  { "<canonical_id>": "<packed>" },
      "company_knowledge": { "<company_slug>": "<packed>" },     # omitted when nothing is on file
      "contact_index":     [ {canonical_id, display_name, identifiers, confidence} ] }
contact_index covers EVERY person file (not just recently-updated ones) — it is the identity map
that lets the brief resolve a phone and an email to the SAME person (the phone↔email bridge).

WHO PACKS, in one sentence: a person packs when they appear in today's inputs (--gmail) or on
today's calendar (--calendar), and the --relevant-days file-mtime window is the fallback cohort
used only when no inputs are supplied (no --gmail, or a gather that produced no addresses). mtime
says when a file was last REWRITTEN, which is not the same question as "does this person matter
today" (persist_prep.py documents the same lesson) — so the person who emailed you this morning
used to pack nothing and the model re-derived what the graph already knew.

company_knowledge answers the other half — the company files the Learn step has been writing since
the Rust port and nothing ever read back. Companies come from today's attendees' email domains and
from the packed people's `company` field, resolved through knowledge_update's one company reader.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # knowledge.py, its sibling
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import knowledge as kg  # noqa: E402


LOW_CONFIDENCE = 0.6   # below this a fact must be visibly labeled in the packed context

# Company context is a garnish on the brief, not a second brief: a handful of the companies in
# today's cast, each a couple of sentences. Same spirit as the per-person compact caps above.
MAX_COMPANIES_PACKED = 5
MAX_COMPANY_ENTRY_CHARS = 600
MAX_COMPANY_NEWS_LINES = 3   # the file stores news newest-first (knowledge_update inserts at 0)

EMAIL_RE = re.compile(r"[\w.+\-']+@[\w\-]+\.[\w.\-]+")


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

    # Relations, as sentences, right under the identity line — so every consumer of a packed person
    # block (the brief, meeting prep's "Your thread", Ask, the event funnel's "who is this")
    # inherits "Introduced to you by Vishnu Sharma (May 2026)" with no further wiring.
    sentences = [s for s in (r.sentence() for r in p.relations) if s][:kg.MAX_RELATIONS_FOR_LLM]
    if sentences:
        lines.append("& " + "; ".join(sentences))

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


def _calendar_attendee_emails(path: str | None) -> set:
    """Lowercased attendee emails from a gathered calendar file (gather_google.py's
    /tmp/sotto_cal.json — a list of events, or {events:[…]}; attendees are dicts or bare strings).
    Missing/unreadable file → empty set (the exemption just doesn't apply)."""
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return set()
    events = data.get("events") if isinstance(data, dict) else data
    emails = set()
    for e in events if isinstance(events, list) else []:
        if not isinstance(e, dict):
            continue
        for a in e.get("attendees") or []:
            em = a.get("email") if isinstance(a, dict) else a
            if isinstance(em, str) and "@" in em:
                emails.add(em.strip().lower())
    return emails


def _gmail_addresses(path: str | None) -> set:
    """Lowercased From/To addresses from a gathered gmail file (gather_google.py's
    /tmp/sotto_gmail.json — a list of emails, or {emails:[…]}). The fields are display strings
    ("Sarah Chen <sarah@acme.com>", or a comma-joined To), so the addresses are pulled out with a
    regex rather than parsed. Missing/unreadable file → empty set (the gate falls back to mtime)."""
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return set()
    rows = data.get("emails") if isinstance(data, dict) else data
    out = set()
    for e in rows if isinstance(rows, list) else []:
        if not isinstance(e, dict):
            continue
        for field in ("from", "to", "cc"):
            v = e.get(field)
            if isinstance(v, str):
                out.update(m.group(0).lower() for m in EMAIL_RE.finditer(v))
    return out


def _company_entry(known: dict) -> str:
    """One company file → one packed block: what it is, then its newest news. Capped."""
    lines = []
    name = (known.get("name") or known.get("slug") or "").strip()
    about = " ".join((known.get("about") or "").split())
    lines.append(f"{name} — {about}" if about else name)
    for n in (known.get("news") or [])[:MAX_COMPANY_NEWS_LINES]:
        text = " ".join(str(n).split())
        if text:
            lines.append("- " + text)
    return "\n".join(lines)[:MAX_COMPANY_ENTRY_CHARS]


def pack_companies(domains, companies) -> dict:
    """{slug: packed block} for today's companies — the read side of the company files the Learn
    step writes. Sources, in order: today's attendees' email domains, then the packed people's
    `company` field. Resolution and dedup are knowledge_update's (`company_knowledge` runs the same
    slug → alias → domain ladder the writer does), so a domain and a name that mean one company
    pack once. Best-effort: no companies dir, or an unreadable one, packs nothing."""
    try:
        import knowledge_update as ku  # noqa: PLC0415 — lazy: reading companies must never break a query
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for name in list(domains) + list(companies):
        if len(out) >= MAX_COMPANIES_PACKED:
            break
        if not str(name).strip():
            continue
        try:
            known = ku.company_knowledge(company_name=str(name))
        except Exception:  # noqa: BLE001
            continue
        slug = known.get("slug") if known else ""
        if not slug or slug in out:
            continue
        entry = _company_entry(known)
        if entry.strip():
            out[slug] = entry
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--person")
    ap.add_argument("--relevant-days", type=int, default=7)
    ap.add_argument("--calendar", help="gathered calendar JSON; today's attendees pack, and their "
                                       "email domains pull in company context")
    ap.add_argument("--gmail", help="gathered gmail JSON; everyone in today's From/To packs. "
                                    "Supplying it REPLACES the --relevant-days mtime window")
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
    cal_emails = _calendar_attendee_emails(args.calendar)
    gmail_emails = _gmail_addresses(args.gmail)
    today_emails = cal_emails | gmail_emails
    # THE GATE, in one sentence: a person packs when they appear in today's inputs or on today's
    # calendar; the mtime window is the fallback cohort, used only when no inputs were supplied.
    # mtime only says when a file was last REWRITTEN, so under it someone who emailed you this
    # morning packs zero context and the model re-derives what the graph already knows. An empty or
    # unreadable --gmail is "no inputs", not "nobody" — a broken gather degrades to the old cohort.
    inputs_gate = bool(gmail_emails)
    person_knowledge, contact_index, companies = {}, [], []
    for path in sorted(glob.glob(os.path.join(kg.people_dir(), "*.md"))):
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
        if not any(i.strip().lower() in today_emails for i in identifiers):
            if inputs_gate:
                continue
            try:
                if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff:
                    continue
            except OSError:
                continue
        person_knowledge[p.canonical_id or kg.slugify(p.name)] = pack_person(p, False, now)
        if p.company:
            companies.append(p.company)

    out = {"person_knowledge": person_knowledge, "contact_index": contact_index}
    domains = sorted({e.split("@", 1)[1] for e in cal_emails if "@" in e})
    company_knowledge = pack_companies(domains, companies)
    if company_knowledge:   # absent, not empty, when nothing is on file — the brief renders neither
        out["company_knowledge"] = company_knowledge
    print(json.dumps(out))


if __name__ == "__main__":
    main()
