#!/usr/bin/env python3
"""
prewarm_graph.py — seed the knowledge graph at SETUP so brief #1 isn't cold.

The first brief is the one a new user judges Sotto on, yet today the graph fills in only AFTER it (the
Learn step), so brief #1 is the weakest one. This pre-warm runs once during setup over the 6-week seed
read: it creates identity STUBS for the people the user actually talks to most (name + identifiers,
NO invented facts), so day-1 briefs recognize them, relationship-pulse can weight them, and name
resolution is robust even when Bridge contacts come back thin.

By default the emailed contacts are ALSO enriched with grounded research (through the shared search
seam — Parallel → Exa → Gemini grounding, whichever key is set), written as
clearly-sourced, LOW-confidence "Per web search: …" facts that decay — never authoritative identity
fields (the guessed-role burn — "Peyton the founder", "Alive Ventures" — came from UNsourced
authoritative claims, which this never writes). Name-only stubs made week-one briefs read thin.
Fully guarded: no research provider / network failure / import error degrades silently to the plain
stubs. Set SOTTO_PREWARM_RESEARCH=0 to skip enrichment entirely. The user's normal Learn step +
on-demand meeting research promote facts over time as they're confirmed.

WHAT A CONTACTS CARD CONTRIBUTES (both modes): every email AND phone becomes an identifier on the
person file — so a text from their second number resolves to the same human instead of forking a
file — plus the two durable things the user typed themselves: the card's `notes` as ONE sourced fact
and their `birthday`. Nothing else: an address book is a directory, not knowledge.

Usage (execute_code):
  prewarm_graph.py /tmp/sotto_seed.json   — SETUP: top-contact stubs (+ opt-in research), or stdin
  prewarm_graph.py --sync-contacts        — EVERY RUN (the brief's Learn step): re-sync identifiers,
      notes and birthdays from `$SOTTO_DATA/knowledge/last_local_snapshot.json`, no research. This
      is why the graph no longer depends on a live Contacts read at brief time.
Prints JSON: {"stubs": N, "researched": M, "people": [...]} / {"contacts": N, "synced": M, "facts": K}.
Env: SOTTO_DATA (graph location), SOTTO_PREWARM_RESEARCH=0 (skip enrichment), one of
EXA_API_KEY / PARALLEL_API_KEY / GOOGLE_AI_API_KEY (enrichment silently skips without any of them).
How many people is the named constant MAX_PREWARM (12).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "lib"))
sys.path.insert(0, os.path.join(_HERE, "..", "knowledge"))
import knowledge as kg  # noqa: E402
import knowledge_update as ku  # noqa: E402
from textutil import _arr, _looks_like_phone_number, _s, unwrap_tool_result  # noqa: E402
from render_local import resolve_contact_names  # noqa: E402

MAX_PREWARM = 12       # top contacts the first-run seed pre-warms — a dozen covers the people you
#                        actually talk to without turning setup into a research job
MIN_INTERACTIONS = 3   # don't stub a one-off; "people you talk to" means a few touches at least
CONTACTS_SOURCE = "apple_contacts"    # provenance for anything read off a Contacts card
CONTACTS_CONFIDENCE = 0.9             # the user typed it themselves — high, but still correctable
MAX_NOTE_CHARS = 600                  # one note, one fact; the same cap the research facts use


def _contact_field(c: dict, *keys) -> list:
    """The values under the first field name a Bridge/legacy contact actually uses (emails/email)."""
    for k in keys:
        v = c.get(k)
        if isinstance(v, str):
            v = [v]
        if isinstance(v, list):
            vals = [_s(x).strip() for x in v if _s(x).strip()]
            if vals:
                return vals
    return []


def _contact_cards(local: dict) -> dict:
    """name (lowercased) → the contact card, from the Bridge contacts list."""
    out: dict = {}
    for c in _arr(local, "contacts"):
        nm = _s(c.get("name")).strip().lower()
        if nm:
            out.setdefault(nm, c)
    return out


def contact_update(c: dict) -> dict:
    """One Contacts card → one person_update: all identifiers, the notes fact, the birthday fact.
    Returns {} for a card with no name AND no email — a phone-only nameless row is noise, not a
    person. NO research and nothing inferred: every field here was typed by the user."""
    name = _s(c.get("name")).strip()
    emails = [e.lower() for e in _contact_field(c, "emails", "email") if "@" in e]
    phones = _contact_field(c, "phones", "phone")
    if not name and not emails:
        return {}
    facts = []
    notes = _s(c.get("notes")).strip()
    if notes:
        facts.append({"fact": notes[:MAX_NOTE_CHARS], "memory_type": "context",
                      "confidence": CONTACTS_CONFIDENCE, "source": CONTACTS_SOURCE,
                      "source_ref": "contacts"})
    birthday = _s(c.get("birthday")).strip()
    if birthday:
        facts.append({"fact": f"Birthday: {birthday} (Apple Contacts)", "memory_type": "personal",
                      "confidence": CONTACTS_CONFIDENCE, "source": CONTACTS_SOURCE,
                      "source_ref": "contacts"})
    identifiers = emails + phones
    # The email leads: research and the brief's own resolution both key off an addressable
    # identifier, and `identifiers` carries the rest onto the same file.
    return {"person_name": name, "identifier": identifiers[0] if identifiers else "",
            "identifiers": identifiers, "facts": facts}


def sync_contacts(local: dict) -> dict:
    """Re-apply Contacts identity onto the graph — the Learn-step pass that makes a live Contacts
    read unnecessary. Deliberately NOT every card: an address book of 2,000 rows is a directory, and
    minting 2,000 person files would bury the graph. A card syncs when Sotto ALREADY knows the
    person, or when the user wrote a note or a birthday on it — the only two things a brief three
    months from now could use. Idempotent: identical note text BUMPs, it never duplicates."""
    cards = _arr(local, "contacts")
    try:
        index = kg.build_people_index()
    except Exception:  # noqa: BLE001 — an unreadable graph syncs the noted cards and nothing else
        index = None
    updates = []
    for c in cards:
        upd = contact_update(c)
        if not upd:
            continue
        known = index is not None and any(
            kg.find_person_file(name=upd["person_name"], identifier=i, index=index)
            for i in (upd["identifiers"] or [""]))
        if not (upd["facts"] or known):
            continue
        upd["updated_by"] = CONTACTS_SOURCE
        updates.append(upd)
    if updates:
        ku.apply({"person_updates": updates})
    return {"contacts": len(cards), "synced": len(updates),
            "facts": sum(len(u["facts"]) for u in updates)}


def _top_contacts(local: dict) -> list:
    """Count message/call touches per RESOLVED known contact (same is_known gate as the brief: skip
    group chats and raw-phone-named senders), return the most-frequent first."""
    local = resolve_contact_names(local)
    counts: dict = {}

    def add(name):
        nm = _s(name).strip()
        if not nm or _looks_like_phone_number(nm):
            return
        counts[nm] = counts.get(nm, 0) + 1

    for m in _arr(local, "imessage"):
        if not m.get("is_group_chat"):
            add(m.get("resolved_name"))
    for m in _arr(local, "whatsapp"):
        if not m.get("is_group_chat"):
            add(_s(m.get("resolved_name")) or _s(m.get("partner_name")))
    for c in _arr(local, "missed_calls") + _arr(local, "recent_calls"):
        add(c.get("name"))

    ranked = sorted(((n, c) for n, c in counts.items() if c >= MIN_INTERACTIONS),
                    key=lambda kv: kv[1], reverse=True)
    return ranked[:MAX_PREWARM]


def _research_facts(updates: list) -> int:
    """Opt-in: enrich the emailed stubs with grounded research, stored as LOW-confidence, clearly
    'per web search' facts (kept since >=0.5, but they decay and never become identity fields). Returns
    how many people got at least one research fact. Best-effort — any failure leaves the stubs intact."""
    attendees = [{"name": u["person_name"], "email": u["identifier"]}
                 for u in updates if "@" in u.get("identifier", "")]
    if not attendees:
        return 0
    try:
        import research_attendees as ra  # noqa: PLC0415
        # Profiles only (Pass A): prewarm persists a single bio fact per person, so Pass B's
        # recency output — roughly half the grounded calls — would be entirely discarded.
        # Env-scoped to this call; the user's own SOTTO_RESEARCH_DEEP setting is restored.
        prev_deep = os.environ.get("SOTTO_RESEARCH_DEEP")
        os.environ["SOTTO_RESEARCH_DEEP"] = "0"
        try:
            res = ra.research(attendees, "")   # no meeting context at setup — just background research
        finally:
            if prev_deep is None:
                os.environ.pop("SOTTO_RESEARCH_DEEP", None)
            else:
                os.environ["SOTTO_RESEARCH_DEEP"] = prev_deep
    except Exception:  # noqa: BLE001  (no key, network, import) — pre-warm degrades to stubs
        return 0
    by_email = {_s(a.get("email")).lower(): a for a in (res or {}).get("attendees", [])}
    n = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for u in updates:
        a = by_email.get(u["identifier"])
        if not a:
            continue
        # ONE combined fact (persist_prep.py pattern): two separate "Per web search:" facts overlap
        # >0.5 in knowledge_update's find_similar_fact, so the second would BUMP-swallow the richer
        # summary. The "No public profile found." sentinel is a research MISS, not a fact about the
        # person — mirror persist_prep's filter so it never persists into the graph.
        bits = []
        title, company = _s(a.get("title")).strip(), _s(a.get("company")).strip()
        if title or company:
            bits.append(" at ".join(x for x in (title, company) if x))
        summary = _s(a.get("summary")).strip()
        if summary and summary.lower() != "no public profile found.":
            bits.append(summary)
        company_summary = _s(a.get("company_summary")).strip()
        if company_summary:   # person → company fallback: the company is researchable even
            bits.append(company_summary)   # when the person isn't
        if not bits:
            continue   # nothing grounded → the plain identity stub stands
        # APPEND — the card's own note and birthday were already on this update and are the
        # user's words; a research sweep adds to them, it never replaces them.
        u.setdefault("facts", []).append(
            {"fact": ("Per web search: " + " — ".join(bits))[:600],
             "confidence": 0.55, "memory_type": "context",
             "source": "web_research",   # without it the updater stamps brief_extraction
             "source_ref": f"prewarm-research:{today}"})
        u["updated_by"] = "web_research"
        u["last_researched"] = today   # persist_prep.profile_is_fresh keys off this stamp
        n += 1
    return n


def prewarm(local: dict) -> dict:
    cards = _contact_cards(local)
    top = _top_contacts(local)
    updates = []
    for name, _count in top:
        # The card carries the identity: all emails + phones, the user's own note, their birthday.
        # No card → identity-only stub by name, exactly as before.
        upd = contact_update(cards.get(name.lower(), {})) or {}
        upd.update({"person_name": name, "identifier": upd.get("identifier", "")})
        upd.setdefault("identifiers", [])
        upd.setdefault("facts", [])
        if upd["facts"]:
            upd["updated_by"] = CONTACTS_SOURCE   # a note/birthday write is not a brief extraction
        updates.append(upd)

    researched = 0
    if os.environ.get("SOTTO_PREWARM_RESEARCH", "1") != "0" and updates:   # default ON; =0 skips
        researched = _research_facts(updates)

    if updates:
        ku.apply({"person_updates": updates})
    return {"stubs": len(updates), "researched": researched,
            "people": [u["person_name"] for u in updates]}


def _snapshot_local() -> dict:
    """`local` out of the brief's own cached snapshot — the file the Learn step syncs from, so no
    mode of this script ever needs a live Contacts read. Missing/unreadable → {}."""
    path = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "knowledge", "last_local_snapshot.json")
    try:
        with open(path, encoding="utf-8") as f:
            local = (json.load(f) or {}).get("local") or {}
        return local if isinstance(local, dict) else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError, AttributeError):
        return {}


def main():
    if "--sync-contacts" in sys.argv[1:]:
        print(json.dumps(sync_contacts(_snapshot_local())))
        return
    try:
        raw = open(sys.argv[1]).read() if len(sys.argv) > 1 else sys.stdin.read()
        # The setup seed may be the raw MCP tool-result wrapper (saved AS-IS) — unwrap it the same
        # way compose_brief does, or the prewarm silently builds 0 stubs from a healthy snapshot.
        local = unwrap_tool_result(json.loads(raw)) if raw.strip() else {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        print(json.dumps({"stubs": 0, "researched": 0, "people": []}))
        return
    print(json.dumps(prewarm(local)))


if __name__ == "__main__":
    main()
