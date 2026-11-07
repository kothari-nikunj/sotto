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
       • the profile bio (title/company/summary) — LOW confidence (0.55) that decays; a later run
         whose bio reads differently supersedes it, the same words corroborate it
       • recent_activity items (Pass B) — dated + source-URLed, confidence 0.6,
         source_ref = the item's source_url
       • personal items (Pass B) — public texture carrying its own source URL, confidence 0.6
     Typed title/company retain their supporting fact id; correcting that fact withdraws the
     typed projection until the owner supplies structured identity.

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
import hashlib
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
from textutil import FREEMAIL_DOMAINS  # noqa: E402
import research_contract as ra  # noqa: E402

FRESH_DAYS = 30                 # matches the Mac attendee-cache TTL
RESEARCH_CONFIDENCE = 0.55      # prewarm_graph.py — low-confidence research facts that decay
RECENT_CONFIDENCE = 0.6         # dated + source-URLed recency-sweep items (Pass B)
SOURCE_PREFIX = "meeting-prep-research"
FACT_SOURCE = "web_research"    # provenance label for ALL research writes (grounded web search —
                                # NOT knowledge_update's default "brief_extraction" label)
MAX_RECENT_PERSISTED = ra.MAX_RECENT_ACTIVITY
MAX_PERSONAL_PERSISTED = ra.MAX_PERSONAL
MAX_TRACTION_PERSISTED = ra.MAX_TRACTION


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
    return "" if not dom or dom in FREEMAIL_DOMAINS else dom


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


def company_updates(research: dict, today: str, *, companies_only: bool = False) -> list:
    """The `company_updates` half of the research write: what we learned about ORGANIZATIONS,
    keyed to the company rather than to whichever human we happened to research.

    One update per company (two attendees from the same employer merge into one), and the About is
    only ever written when it is an UPGRADE on what is on file — a deep dive beats a sweep
    one-liner, and a `user_edit` About beats everything."""
    by_key: dict = {}
    for a in (research or {}).get("companies" if companies_only else "attendees", []):
        if not isinstance(a, dict):
            continue
        cd = a.get("company_deep") if isinstance(a.get("company_deep"), dict) else {}
        name = _s(cd.get("company")).strip() or _s(a.get("company")).strip()
        if not name:
            continue    # no company identified — nothing to key a file by, and none is invented
        domain = ra.company_domain(_s(a.get("domain"))) if companies_only else _domain_of(a.get("email"))
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


def research_bio(a: dict) -> str:
    """The attendee's ONE combined "Per web search: <title> at <company> — <summary>" bio, or "".
    One fact, because the shared prefix would make separate title/summary facts dedupe-collide
    inside knowledge_update; prewarm_graph writes the same bio through here. The sentinel summary
    is a research MISS, and company_summary describes the employer: it seeds the company's About
    in company_updates, so it never resurfaces in a later prep as something about this person."""
    bits = []
    title, company = _s(a.get("title")).strip(), _s(a.get("company")).strip()
    if title or company:
        bits.append(" at ".join(x for x in (title, company) if x))
    summary = _profile_summary(a)
    if summary:
        bits.append(summary)
    return "Per web search: " + " — ".join(bits) if bits else ""


def _profile_summary(a: dict) -> str:
    summary = _s(a.get("summary")).strip()
    return "" if summary.lower() == "no public profile found." else summary


def _replaces_earlier_bio(name: str, email: str, bio: str) -> bool:
    """True when `bio` would BUMP an earlier research bio that reads differently. A bump keeps the
    old words and strengthens them — including the company summary earlier runs folded in — so
    the new bio replaces it through knowledge_update's correction path instead. Same words still
    corroborate."""
    path = _find_profile(name, email)
    if not path:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            facts = kg.parse_person_file(f.read()).facts
    except Exception:  # noqa: BLE001 — an unreadable profile just keeps the plain bump
        return False
    action, fid = kg.find_similar_fact(facts, bio, "context", False)
    old = facts[fid] if action == kg.BUMP else None
    return bool(old and old.source == FACT_SOURCE and old.type == "context"
                and old.text.startswith("Per web search: ") and old.text != bio)


def persist(research: dict, attendees_in: list | None = None, now: datetime | None = None,
            *, previous_receipt: dict | None = None) -> dict:
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
        # Grounded bits ONLY — from the research output, clearly sourced.
        facts = []
        title, company = _s(a.get("title")).strip(), _s(a.get("company")).strip()
        bio = research_bio(a)
        if bio:
            fact = {"fact": bio, "confidence": RESEARCH_CONFIDENCE, "memory_type": "context",
                    "source": FACT_SOURCE, "source_ref": f"{SOURCE_PREFIX}:{today}"}
            # Only a run that found the person retires the earlier bio; a miss must not thin it
            # down to "title at company".
            if _profile_summary(a) and _replaces_earlier_bio(name, email, bio):
                fact["change_type"] = "correction"
            facts.append(fact)
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
        update = {
            "person_name": name or email,
            "identifier": email,
            "updated_by": FACT_SOURCE,
            "last_researched": today,   # freshness stamp profile_is_fresh keys off (not mtime)
            "facts": facts,
        }
        # Research hands us title/company as TYPED fields — they land as typed fields, not only
        # inside the prose fact above. Every consumer of the graph's identity line (the pack's
        # "Title at Company", the pulse's weighting, the dashboard, the People filter) reads the
        # typed pair; without this, a fully-researched person still looked blank there.
        if title or company:
            update["profile_patch"] = {k: v for k, v in (("title", title), ("company", company)) if v}
            update["profile_support_fact_index"] = 0
        updates.append(update)

    return _persist_updates(research, today, updates, previous_receipt=previous_receipt)


def _persist_updates(research: dict, today: str, updates: list, *, companies_only=False,
                     previous_receipt: dict | None = None) -> dict:
    digest = hashlib.sha256(json.dumps({'research': research,
        'people': [(u['person_name'], u.get('identifier', '')) for u in updates]},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    out = {"persisted": 0, "people": [], "companies": [], "complete": True,
           "input_digest": digest, "outcomes": {"people": [], "companies": []}}
    errors = []
    previous = ((previous_receipt or {}).get('outcomes', {})
                if (previous_receipt or {}).get('input_digest') == digest else {})
    prior_companies = {item.get('name'): item for item in previous.get('companies', [])
                       if item.get('status') == 'applied'}
    prior_people = {(item.get('identifier') or item.get('name')): item
                    for item in previous.get('people', [])
                    if item.get('fresh') or (item.get('status') == 'rejected'
                        and not all(ku.fact_establishes_research_coverage(outcome)
                                    for outcome in item.get('fact_outcomes', {}).get('items', [])))}
    try:
        companies = company_updates(research, today, companies_only=companies_only)
    except Exception as error:
        companies = []
        errors.append(f"{type(error).__name__}: {str(error)[:200]}")
        out['outcomes']['companies'].append({'status': 'failed', 'error': errors[-1]})
    for company in companies:
        item = {'name': company['company_name']}
        if company['company_name'] in prior_companies:
            out['companies'].append(company['company_name'])
            out['outcomes']['companies'].append(prior_companies[company['company_name']])
            continue
        try:
            result = ku.apply({'person_updates': [], 'company_updates': [company]})
            item.update(status='applied' if result['company_files'] else 'rejected')
            if result['company_files']:
                out['companies'].append(company['company_name'])
        except Exception as error:
            errors.append(f"{type(error).__name__}: {str(error)[:200]}")
            item.update(status='failed', error=errors[-1])
        out['outcomes']['companies'].append(item)
    for update in updates:
        if errors:
            update.pop('last_researched', None)
        item = {'name': update['person_name'], 'identifier': update.get('identifier', '')}
        prior = prior_people.get(update.get('identifier') or update['person_name'])
        if prior:
            out['outcomes']['people'].append(prior)
            if prior['status'] == 'applied':
                out['persisted'] += 1
                out['people'].append(update['person_name'])
            continue
        try:
            result = ku.apply({'person_updates': [update], 'company_updates': []})
            facts = result['fact_outcomes']
            accepted = any(key in ku.ACCEPTED_FACT_DISPOSITIONS and count
                           for key, count in facts['counts'].items())
            item.update(status='applied' if accepted else 'rejected', fact_outcomes=facts,
                        fresh=bool(update.get('last_researched') and facts['total'] and not facts['truncated']
                                   and all(ku.fact_establishes_research_coverage(outcome)
                                           for outcome in facts['items'])))
            if accepted:
                out['persisted'] += 1
                out['people'].append(update['person_name'])
        except Exception as error:
            errors.append(f"{type(error).__name__}: {str(error)[:200]}")
            item.update(status='failed', error=errors[-1])
        out['outcomes']['people'].append(item)
    if errors:
        out.update(complete=False, error='; '.join(errors)[:600])
    return out


def persist_company(research: dict, now: datetime | None = None, *,
                    previous_receipt: dict | None = None) -> dict:
    """Persist only original grounded company fields; never generate a person update."""
    out = {"persisted": 0, "people": [], "companies": []}
    rows = research.get("companies", []) if isinstance(research, dict) else []
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        return out
    filtered = ra.postfilter_company(rows[0])
    if not filtered or not _s(filtered["company_deep"].get("company")).strip():
        return out
    row = {**filtered, "domain": ra.company_domain(_s(rows[0].get("domain")))}
    return _persist_updates({"companies": [row]},
                            (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d"),
                            [], companies_only=True, previous_receipt=previous_receipt)


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        return json.loads(raw) if raw.strip() else None
    except (OSError, json.JSONDecodeError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company-only", action="store_true", help="persist companies only, never people")
    ap.add_argument("--previous-receipt", help="receipt for retrying the same retained research")
    ap.add_argument("--research", help="research_attendees.py output ({attendees:[…]})")
    ap.add_argument("--attendees", help="select_attendees.py output (email → name mapping)")
    ap.add_argument("--filter-fresh", dest="filter_fresh",
                    help="select_attendees output file; rewritten IN PLACE minus fresh profiles")
    ap.add_argument("--keep", default="",
                    help="with --filter-fresh: never drop this person (name substring or exact "
                         "email) — the focused person, whose deep dive freshness must not block")
    a = ap.parse_args()

    if a.company_only and (a.attendees or a.filter_fresh or a.keep):
        ap.error("--company-only cannot be combined with person selection")
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
    if a.company_only:
        print(json.dumps(persist_company(research, previous_receipt=_load(a.previous_receipt)
                                         if a.previous_receipt else None)))
        return
    attendees_in = _load(a.attendees) if a.attendees else None
    if not isinstance(attendees_in, list):
        attendees_in = []
    print(json.dumps(persist(research, attendees_in, previous_receipt=_load(a.previous_receipt)
                             if a.previous_receipt else None)))


if __name__ == "__main__":
    main()
