#!/usr/bin/env python3
"""
prewarm_graph.py — seed the knowledge graph at SETUP so brief #1 isn't cold.

The first brief is the one a new user judges Sotto on, yet today the graph fills in only AFTER it (the
Learn step), so brief #1 is the weakest one. This pre-warm runs once during setup over the 6-week seed
read: it creates identity STUBS for the people the user actually talks to most (name + identifiers,
NO invented facts), so day-1 briefs recognize them, relationship-pulse can weight them, and name
resolution is robust even when Bridge contacts come back thin.

By default the emailed contacts are ALSO enriched with grounded Gemini research, written as
clearly-sourced, LOW-confidence "Per web search: …" facts that decay — never authoritative identity
fields (the guessed-role burn — "Peyton the founder", "Alive Ventures" — came from UNsourced
authoritative claims, which this never writes). Name-only stubs made week-one briefs read thin.
Fully guarded: no GOOGLE_AI_API_KEY / network failure / import error degrades silently to the plain
stubs. Set SOTTO_PREWARM_RESEARCH=0 to skip enrichment entirely. The user's normal Learn step +
on-demand meeting research promote facts over time as they're confirmed.

Usage (execute_code): prewarm_graph.py /tmp/sotto_seed.json     (the setup read_local snapshot; or stdin)
Prints JSON: {"stubs": N, "researched": M, "people": [...]}.
Env: SOTTO_DATA (graph location), SOTTO_PREWARM_RESEARCH=0 (skip enrichment), GOOGLE_AI_API_KEY
(enrichment silently skips without it), SOTTO_PREWARM_MAX (default 12).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "lib"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "morning-brief", "scripts"))
import compose_brief as cb  # noqa: E402
import knowledge_update as ku  # noqa: E402
from textutil import unwrap_tool_result  # noqa: E402  (shared MCP tool-result unwrap)

MAX_PREWARM = int(os.environ.get("SOTTO_PREWARM_MAX", "12"))
MIN_INTERACTIONS = 3   # don't stub a one-off; "people you talk to" means a few touches at least


def _contact_emails(local: dict) -> dict:
    """name (lowercased) → first email, from the Bridge contacts list (tolerant of field names)."""
    out: dict = {}
    for c in cb._arr(local, "contacts"):
        nm = cb._s(c.get("name")).strip().lower()
        if not nm:
            continue
        emails = c.get("emails") or c.get("email") or []
        if isinstance(emails, str):
            emails = [emails]
        em = next((cb._s(e).strip() for e in emails if cb._s(e).strip()), "")
        if em:
            out[nm] = em.lower()
    return out


def _top_contacts(local: dict) -> list:
    """Count message/call touches per RESOLVED known contact (same is_known gate as the brief: skip
    group chats and raw-phone-named senders), return the most-frequent first."""
    local = cb.resolve_contact_names(local)
    counts: dict = {}

    def add(name):
        nm = cb._s(name).strip()
        if not nm or cb._looks_like_phone_number(nm):
            return
        counts[nm] = counts.get(nm, 0) + 1

    for m in cb._arr(local, "imessage"):
        if not m.get("is_group_chat"):
            add(m.get("resolved_name"))
    for m in cb._arr(local, "whatsapp"):
        if not m.get("is_group_chat"):
            add(cb._s(m.get("resolved_name")) or cb._s(m.get("partner_name")))
    for c in cb._arr(local, "missed_calls") + cb._arr(local, "recent_calls"):
        add(c.get("name"))

    ranked = sorted(((n, c) for n, c in counts.items() if c >= MIN_INTERACTIONS),
                    key=lambda kv: kv[1], reverse=True)
    return ranked[:MAX_PREWARM]


def _research_facts(updates: list, emails: dict) -> int:
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
        # recency/hooks output — roughly half the grounded calls — would be entirely discarded.
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
    by_email = {cb._s(a.get("email")).lower(): a for a in (res or {}).get("attendees", [])}
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
        title, company = cb._s(a.get("title")).strip(), cb._s(a.get("company")).strip()
        if title or company:
            bits.append(" at ".join(x for x in (title, company) if x))
        summary = cb._s(a.get("summary")).strip()
        if summary and summary.lower() != "no public profile found.":
            bits.append(summary)
        company_summary = cb._s(a.get("company_summary")).strip()
        if company_summary:   # person → company fallback: the company is researchable even
            bits.append(company_summary)   # when the person isn't
        if not bits:
            continue   # nothing grounded → the plain identity stub stands
        u["facts"] = [{"fact": ("Per web search: " + " — ".join(bits))[:600],
                       "confidence": 0.55, "memory_type": "context",
                       "source_ref": f"prewarm-research:{today}"}]
        u["last_researched"] = today   # persist_prep.profile_is_fresh keys off this stamp
        n += 1
    return n


def prewarm(local: dict) -> dict:
    emails = _contact_emails(local)
    top = _top_contacts(local)
    updates = []
    for name, _count in top:
        ident = emails.get(name.lower(), "")   # email if we have one; else identity-only stub by name
        updates.append({"person_name": name, "identifier": ident})

    researched = 0
    if os.environ.get("SOTTO_PREWARM_RESEARCH", "1") != "0" and updates:   # default ON; =0 skips
        researched = _research_facts(updates, emails)

    if updates:
        ku.apply({"person_updates": updates})
    return {"stubs": len(updates), "researched": researched,
            "people": [u["person_name"] for u in updates]}


def main():
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
