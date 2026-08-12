#!/usr/bin/env python3
"""
persist_prep.py — stop throwing away meeting-prep attendee research.

Two jobs, both deterministic:

  1. PERSIST (default): write the grounded output of research_attendees.py into the knowledge graph
     so the same people aren't re-researched every prep run. Facts come ONLY from the research JSON
     — never invented — and mirror prewarm_graph.py's pattern: clearly-sourced "Per web search: …"
     facts applied through knowledge_update.apply() (dedupe/bump/supersede + decay all included).
     Three kinds, all labeled source="web_research" (they came from grounded search, NOT from the
     brief's email/message extraction):
       • the profile bio (title/company/summary) — LOW confidence (0.55) that decays
       • recent_activity items (Pass B) — dated + source-URLed, confidence 0.6,
         source_ref = the item's source_url
       • personal items (Pass B) — public texture carrying its own source URL, confidence 0.6
     Facts are never written as authoritative identity fields; the brief's Learn step promotes
     them as confirmed.

     WHAT WE LEARNED ABOUT A COMPANY GOES TO THE COMPANY. The same apply() call carries
     `company_updates`, because the focus pass's `company_deep` (what it builds, the founder story,
     the market, traction) is knowledge about an ORGANIZATION — parking it on whichever human
     happened to be researched means the next person from that company arrives cold. It rides
     knowledge_update's existing company writer (one concept, one writer): builds/founder/market
     become the company's `## About`, each source-URLed traction signal becomes a `## News` line,
     and `updated_by: web_research` + `last_researched` are the provenance stamps.

     The bar for writing anything: **would a brief three months from now, about a DIFFERENT person
     from this company, still want this line?** What it builds, who founded it and why, the market
     it sits in, the round it raised — yes. An opener, an angle, a "great time to reach out" — no,
     and the research no longer even asks for those.

     The one-line `company_summary` from Pass A seeds About only when the file has none: a sweep
     one-liner must never overwrite a deep dive, and a `user_edit` About is never overwritten at
     all (a correction you made stays made).

       persist_prep.py --research /tmp/sotto_research.json [--attendees /tmp/sotto_research_in.json]

     (--attendees maps email → display name for nicer graph filenames; research output has no name.)

  2. FILTER (--filter-fresh): rewrite the select_attendees output IN PLACE, dropping attendees whose
     graph profile is already FRESH — `last_researched` (stamped by persist below) < 30 days old —
     so step 2 of the SKILL skips re-researching them.

       persist_prep.py --filter-fresh /tmp/sotto_research_in.json [--keep "Spencer"]

     `--keep <name-or-email>` is the focused-prep exception: the person the user named survives the
     filter even when fresh, because the focus pass (research_attendees.py --focus) produces a
     company/space deep dive no earlier sweep run ever wrote.

Prints JSON either way. Degrades to a no-op (exit 0) on missing/empty inputs.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "_shared", "lib"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "_shared", "scripts"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "_shared", "knowledge"))
from textutil import _s  # noqa: E402  (the shared string coercion, not a private copy)
import knowledge as kg  # noqa: E402
import knowledge_update as ku  # noqa: E402
import research_attendees as ra  # noqa: E402  (matches_focus — the one focus/keep matching rule)

FRESH_DAYS = 30                 # matches the Mac attendee-cache TTL
RESEARCH_CONFIDENCE = 0.55      # prewarm_graph.py — low-confidence research facts that decay
RECENT_CONFIDENCE = 0.6         # dated + source-URLed recency-sweep items (Pass B)
SOURCE_PREFIX = "meeting-prep-research"
FACT_SOURCE = "web_research"    # provenance label for ALL research writes (grounded web search —
                                # NOT knowledge_update's default "brief_extraction" label)
MAX_RECENT_PERSISTED = 4        # mirror research_attendees.py's post-filter caps, defensively
MAX_PERSONAL_PERSISTED = 2
MAX_TRACTION_PERSISTED = 4      # company_deep's own cap (research_attendees.MAX_TRACTION)


def _find_profile(name: str, email: str):
    """Locate a person's graph file via the canonical-id store — identifier match first (an email
    is a stronger key than a name form that can differ by channel). Returns path or None."""
    try:
        return kg.find_person_file(name=name or "", identifier=(email or "").strip().lower())
    except Exception:  # noqa: BLE001 — an unreadable store must not kill the filter
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


def filter_fresh(attendees: list, now: datetime | None = None,
                 keep: str = "") -> tuple[list, list]:
    """(kept, skipped_names) — drop attendees whose graph profile is fresh, EXCEPT the person named
    by `keep` (--keep, the focused person): a focused prep needs the company/space deep dive, and
    yesterday's sweep research never produced one, so freshness must not block it."""
    kept, skipped = [], []
    for a in attendees:
        if not isinstance(a, dict):
            continue
        name, email = _s(a.get("name")), _s(a.get("email")).lower()
        if keep and ra.matches_focus(keep, name, email):
            kept.append(a)
        elif profile_is_fresh(name, email, now):
            skipped.append(name or email)
        else:
            kept.append(a)
    return kept, skipped


def _domain_of(email: str) -> str:
    """The attendee's corporate domain, or "" for freemail/none. It is the company file's second
    resolution key (knowledge_update.resolve_company_path consults alias-slug THEN domain), which
    is what stops "Commenda" and "Commenda Inc." forking two files."""
    email = _s(email).strip().lower()
    dom = email.split("@")[1] if "@" in email else ""
    return "" if not dom or dom in ra.FREEMAIL_DOMAINS else dom


def _about_from_deep(cd: dict) -> str:
    """company_deep → the company's durable identity paragraph. Only the three DURABLE fields:
    what it builds, who founded it and why, and the market it sits in. Traction is dated and
    URL-bearing, so it belongs in `## News`, not here."""
    if not isinstance(cd, dict):
        return ""
    parts = []
    builds = [_s(b).strip() for b in (cd.get("builds") or []) if _s(b).strip()]
    if builds:
        parts.append("Builds: " + "; ".join(builds) + ".")
    story = _s(cd.get("founder_story")).strip()
    if story:
        parts.append(story)
    market = _s(cd.get("market")).strip()
    if market:
        parts.append(market)
    return "\n\n".join(parts)


def _news_from_traction(cd: dict) -> list:
    """company_deep.traction → knowledge_update `news` items. Each traction string already ends
    with its source URL (research_attendees._postfilter_focus drops any that doesn't); split it out
    so the company writer's URL dedupe works and a re-run of the same research adds nothing."""
    if not isinstance(cd, dict):
        return []
    out = []
    for t in (cd.get("traction") or [])[:MAX_TRACTION_PERSISTED]:
        t = _s(t).strip()
        m = re.search(r"https?://\S+", t)
        if not t or not m:
            continue   # no source → not a fact we keep, same bar as `personal`
        url = m.group(0).rstrip(").,")
        text = t.replace(m.group(0), "").strip().strip("()").strip(" —-·").strip()
        if text:
            out.append({"text": text, "url": url})
    return out


def _company_updates(research: dict, today: str) -> list:
    """The `company_updates` half of the research write: what we learned about ORGANIZATIONS,
    keyed to the company rather than to whichever human we happened to research.

    One update per company (two attendees from the same employer merge into one), and the About is
    only ever written when it is an UPGRADE on what is on file — a deep dive beats a sweep
    one-liner, and a `user_edit` About beats everything."""
    by_key: dict = {}
    for a in (research or {}).get("attendees", []):
        if not isinstance(a, dict):
            continue
        cd = a.get("company_deep") if isinstance(a.get("company_deep"), dict) else {}
        name = _s(cd.get("company")).strip() or _s(a.get("company")).strip()
        if not name:
            continue    # no company identified — nothing to key a file by, and none is invented
        domain = _domain_of(a.get("email"))
        key = kg.company_slug(name) or domain
        if not key:
            continue
        upd = by_key.setdefault(key, {"company_name": name, "news": []})
        if domain and not upd.get("domain"):
            upd["domain"] = domain
        deep_about = _about_from_deep(cd)
        if deep_about:
            upd["_deep_about"] = deep_about
        summary = _s(a.get("company_summary")).strip()
        if summary and not upd.get("_thin_about"):
            upd["_thin_about"] = summary
        for item in _news_from_traction(cd):
            if not any(n["url"] == item["url"] for n in upd["news"]):
                upd["news"].append(item)

    out = []
    for upd in by_key.values():
        deep_about = upd.pop("_deep_about", "")
        thin_about = upd.pop("_thin_about", "")
        known = ku.company_knowledge(company_name=upd["company_name"],
                                     domain=upd.get("domain", ""))
        if _s(known.get("updated_by")) != "user_edit":
            # A correction the user made outranks anything a search returns; otherwise the deep
            # dive always writes, and the sweep one-liner only fills an empty About.
            about = deep_about or (thin_about if not _s(known.get("about")).strip() else "")
            if about:
                upd["about"] = about
        if not (upd.get("about") or upd["news"]):
            continue    # nothing new to say about this company — no empty rewrite
        upd["updated_by"] = FACT_SOURCE
        upd["last_researched"] = today
        out.append(upd)
    return out


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
        # ONE combined bio fact per attendee: the shared "Per web search" prefix would otherwise
        # make separate title/summary facts dedupe-collide inside knowledge_update.
        facts = []
        bits = []
        title, company = _s(a.get("title")).strip(), _s(a.get("company")).strip()
        if title or company:
            bits.append(" at ".join(x for x in (title, company) if x))
        summary = _s(a.get("summary")).strip()
        if summary and summary.lower() != "no public profile found.":
            bits.append(summary)   # the sentinel is a research MISS, not a fact about the person
        company_summary = _s(a.get("company_summary")).strip()
        if company_summary:        # person → company fallback: the company profile still persists
            bits.append(company_summary)
        if bits:
            facts.append({"fact": "Per web search: " + " — ".join(bits),
                          "confidence": RESEARCH_CONFIDENCE, "memory_type": "context",
                          "source": FACT_SOURCE, "source_ref": f"{SOURCE_PREFIX}:{today}"})
        # Recency-sweep items (Pass B) — persisted ONLY when dated + source-URLed (the research
        # script post-filters, but re-check here: this is the graph's last line of defense).
        # source_ref = the actual page, so every graph fact can be traced to its source.
        for it in (a.get("recent_activity") or [])[:MAX_RECENT_PERSISTED]:
            if not isinstance(it, dict):
                continue
            what = _s(it.get("what")).strip()
            url = _s(it.get("source_url")).strip()
            when = _s(it.get("when")).strip()
            if not what or not url.lower().startswith(("http://", "https://")):
                continue
            facts.append({"fact": f"Per web search ({when or today}): {what}",
                          "confidence": RECENT_CONFIDENCE, "memory_type": "context",
                          "source": FACT_SOURCE, "source_ref": url})
        for p in (a.get("personal") or [])[:MAX_PERSONAL_PERSISTED]:
            p = _s(p).strip()
            m = re.search(r"https?://\S+", p)
            if not p or not m:
                continue   # a personal item without an explicit public source is never persisted
            facts.append({"fact": f"Per web search: {p}",
                          "confidence": RECENT_CONFIDENCE, "memory_type": "personal",
                          "source": FACT_SOURCE, "source_ref": m.group(0).rstrip(").,")})
        if not facts:
            continue   # nothing grounded → nothing to persist
        updates.append({
            "person_name": name or email,
            "identifier": email,
            "last_researched": today,   # freshness stamp profile_is_fresh keys off (not mtime)
            "facts": facts,
        })

    try:
        companies = _company_updates(research, today)
    except Exception:  # noqa: BLE001 — reading the company graph must never cost the person writes
        companies = []
    out = {"persisted": len(updates), "people": [u["person_name"] for u in updates],
           "companies": [c["company_name"] for c in companies]}
    if not (updates or companies):
        return out
    try:
        # ONE apply() for the whole research run: people and companies land together, so a person
        # and their employer can never disagree about when they were last researched, and the run
        # can't half-write.
        ku.apply({"person_updates": updates, "company_updates": companies})
    except Exception as e:  # noqa: BLE001
        # FAIL TOWARD SILENCE (CLAUDE.md): persistence is memory, not the deliverable. An
        # unwritable volume loses one run's learning; it must never cost the prep or the brief the
        # research was gathered for. Said out loud in the output, never swallowed.
        out.update({"persisted": 0, "people": [], "companies": [],
                    "error": f"{type(e).__name__}: {str(e)[:200]}"})
    return out


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
    ap.add_argument("--keep", default="",
                    help="with --filter-fresh: never drop this person (name substring or exact "
                         "email) — the focused person, whose deep dive freshness must not block")
    a = ap.parse_args()

    if a.filter_fresh:
        data = _load(a.filter_fresh)
        attendees = data if isinstance(data, list) else []
        kept, skipped = filter_fresh(attendees, keep=a.keep)
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
