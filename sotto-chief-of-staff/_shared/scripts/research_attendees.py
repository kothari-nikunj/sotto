#!/usr/bin/env python3
"""
research_attendees.py — batched TWO-PASS attendee research via Gemini Search Grounding.

FAITHFUL PORT of the desktop app's api/src/services/gemini-research.ts (researchBatch /
researchAttendees), upgraded with a recency sweep. Learnings ported verbatim:
  • BATCH attendees per grounded Gemini call (25 people → a handful of calls) — the big efficiency win
  • grounding + STRUCTURED output together: tools=[google_search] + responseSchema → clean attendee objects
  • dedupe by email, cap at 25, run batches CONCURRENTLY, per-batch timeout (one failure ≠ all fail)
  • feed a meeting-context summary so `relevance` reflects the actual agenda
  • uses the GOOGLE_AI_API_KEY we already have — no Firecrawl/Tavily/Parallel key

TWO PASSES per attendee (both grounded, both batched, run concurrently):
  Pass A — PROFILE (the original port): who they are, title, company, relevance, bio.
  Pass B — RECENCY & TEXTURE sweep (the quality upgrade): a separate grounded call that hunts the
    last ~DEFAULT_RECENCY_DAYS (90) days — new blog posts / Substack / podcast or
    talk appearances / X-LinkedIn threads with traction / launches, funding, job changes, press —
    plus PUBLIC personal texture (marathon, hobby post, moved cities). Each person's EXISTING graph
    facts are injected with "find what is NEW relative to this" so the sweep returns genuinely novel
    material, not a re-run of the resume. Everything must be dated + source-URLed; a deterministic
    post-filter here (not just the prompt) drops any activity item without a URL and any personal
    item without an explicit public source. Yields recent_activity[{when,what,source_url}] (≤4),
    personal[] (≤2), conversation_hooks[] (≤2) merged onto the Pass-A entries by email.

FOCUS PASS (--focus <name-or-email>, ONE person only): when the user names a person ("prep Spencer")
  the prep is a deep dive, so that ONE attendee gets one additional grounded call covering their
  company and space — what it builds, the founder's background/origin story, traction signals, and
  the market landscape / why now. Returns company_deep{company,builds[],founder_story,traction[],
  market} merged onto that attendee's entry. Depth follows focus: it never fans out, so the cost is
  exactly one extra call, and only when the user asked about one person.

This replaces the per-attendee sub-agent fan-out for research: fewer calls, deterministic, structured.
(web_research.py remains for ad-hoc one-off grounded lookups.)

Input (argv files or stdin JSON):
  --attendees /tmp/sotto_research_in.json   [{name,email}, …]  (from select_attendees.py; entries may
                                            carry a "known" packed-facts string — else the graph is read)
  --focus     "Spencer" | spencer@co.com    ONE attendee gets the extra company/space deep dive
  --context   /tmp/sotto_cal.json           calendar/events for the meeting-context summary (optional)
  --comms     /tmp/sotto_attendee_comms.json (or /tmp/sotto_gmail.json) — already-gathered emails,
                                            per-attendee relationship context (disambiguation +
                                            relevance), no new gather. Accepts the per-attendee
                                            {email:[{date,subject,snippet,from_me}]} shape or the
                                            raw gmail file
  --out       /tmp/sotto_research.json      output file (default) — ALWAYS truncated+rewritten, even
                                            to {"attendees":[]}, so a later run never reuses a stale file
Output (--out file + stdout): {"attendees":[{email,title,company,relevance[],summary,company_summary,
                                recent_activity[],personal[],conversation_hooks[]}, …]}
Degradation ladder per attendee: person profile → company profile (corporate domains always get a
company_summary attempt) → truly nothing (freemail + unsearchable name only).
Env: GOOGLE_AI_API_KEY (required), SOTTO_GEMINI_MODEL (default gemini-3.6-flash),
     SOTTO_RESEARCH_DEEP=0 disables Pass B AND the focus pass; the recency window is the named
     constant DEFAULT_RECENCY_DAYS (90), not a knob.
Test: SOTTO_LLM_STUB=/path/to/{"attendees":[...]}.json bypasses the network.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Hoist the _shared/lib path onto sys.path ONCE at import (guarded), not per call. research() fans
# batches across a ThreadPool; a per-call sys.path.insert inside _metrics()/_diag() would have workers
# mutating sys.path concurrently every batch. A single guarded module-level insert is thread-safe.
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
from textutil import _s  # noqa: E402  (shared helpers, from the modules that own them)
from timeutil import _now_local, _parse_ts, _user_local_date, configured_tz  # noqa: E402
from render_local import RESEARCH_HORIZON_HOURS  # noqa: E402

MODEL = os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3.6-flash")
MAX_ATTENDEES = 25
BATCH_SIZE = 5
PER_BATCH_TIMEOUT = 60
MAX_CONCURRENCY = 5
MAX_OUTPUT_TOKENS = 8192

# Pass B (recency sweep) spend controls: smaller batches (each person gets MULTIPLE distinct
# searches, so 5-wide batches starve the sweep), a longer timeout, and its own token budget.
DEEP_BATCH_SIZE = 3
DEEP_PER_BATCH_TIMEOUT = 90
DEEP_MAX_OUTPUT_TOKENS = 8192
MAX_RECENT_ACTIVITY = 4
MAX_PERSONAL = 2
MAX_HOOKS = 2
DEFAULT_RECENCY_DAYS = 90

# Focus pass (--focus) spend controls: ONE person, ONE call, several searches inside it.
FOCUS_TIMEOUT = 90
FOCUS_MAX_OUTPUT_TOKENS = 8192
MAX_BUILDS = 5
MAX_TRACTION = 4


def _deep_enabled() -> bool:
    return os.environ.get("SOTTO_RESEARCH_DEEP", "1").strip() != "0"


def _recency_days() -> int:
    return DEFAULT_RECENCY_DAYS


# Freemail domains: no company behind the @ — the person→company fallback can't help here, so
# these are the only addresses where "No public profile found." with no company is acceptable.
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "pm.me", "hey.com", "fastmail.com", "gmx.com", "zoho.com", "mail.com",
}

# Generic-filler talking points/hooks ("Ask for introductions…", "Understand the agenda…") are the
# fabrication the user called out — a deterministic post-filter, not just a prompt rule.
_FILLER_STARTS = (
    "ask about", "ask for", "ask them", "ask what", "ask how", "understand ", "explore ",
    "discuss potential", "build rapport", "get to know", "learn about", "learn more about",
    "introduce yourself", "establish ", "gauge ", "see if there",
)
_FILLER_PHRASES = (
    "build rapport", "understand the agenda", "understand what prompted", "ask for introductions",
    "explore potential overlap", "potential synergies", "areas of mutual interest",
    "get to know each other",
)


def _has_concrete_anchor(text: str) -> bool:
    """A concrete named entity beyond generic process language: a URL, a digit (date, round size),
    or a capitalized token past the first word (a name, company, product)."""
    if re.search(r"https?://", text) or re.search(r"\d", text):
        return True
    words = re.findall(r"[A-Za-z][\w'&.-]*", text)
    return any(w[0].isupper() and w not in ("I",) for w in words[1:])


def is_filler_point(text: str) -> bool:
    """True for generic process filler ('ask for introductions', 'understand the agenda') that
    isn't anchored to anything concrete. Shared by the hook post-filter here and the meeting-prep
    talking-points post-filter."""
    t = (text or "").strip()
    low = t.lower().lstrip("-·* ").strip()
    if not low:
        return True
    if any(p in low for p in _FILLER_PHRASES):
        return True
    return low.startswith(_FILLER_STARTS) and not _has_concrete_anchor(t)


def matches_focus(needle: str, name: str, email: str) -> bool:
    """ONE rule for every focus lookup (--focus here, meeting selection in compose_meeting_prep.py,
    --keep in persist_prep.py): an argument containing '@' must equal the email exactly; anything
    else is a case-insensitive substring of the display name or of the email's local part."""
    n = _s(needle).strip().lower()
    if not n:
        return False
    em = _s(email).strip().lower()
    if "@" in n:
        return bool(em) and n == em
    return n in _s(name).strip().lower() or (bool(em) and n in em.split("@")[0])


# Grounding discipline — the ported original's rule (references/research-prompt.md), VERBATIM.
# It is stronger than "don't guess": a figure is allowed only when a citation backs it, because
# these facts get persisted into the knowledge graph.
NO_UNSOURCED_NUMBERS = (
    "No unsourced numbers. Do NOT state specific dollar figures — ARR, valuation, raise size, "
    "acquisition/exit price — unless they appear in the grounded result's citations. A named stage "
    "(\"raised a Series A\") is fine if reported; \"$2.4M ARR\" or \"a $115M exit\" is allowed ONLY "
    "if a citation backs it — otherwise omit the number (write \"acquired by X\", not \"acquired by "
    "X for $Y\"). These figures get written into the user's permanent knowledge graph, so an "
    "invented one persists — when unsure, leave it out."
)

# responseSchema (REST form) — same fields as the desktop BATCH_ATTENDEE_SCHEMA.
SCHEMA = {
    "type": "object",
    "properties": {
        "attendees": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "title": {"type": "string", "nullable": True},
                    "company": {"type": "string"},
                    "relevance": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                    "company_summary": {"type": "string", "nullable": True},
                },
                "required": ["email", "company", "summary"],
            },
        }
    },
    "required": ["attendees"],
}

# Pass B responseSchema — the recency-sweep additions, merged onto Pass A entries by email.
DEEP_SCHEMA = {
    "type": "object",
    "properties": {
        "attendees": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "recent_activity": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "when": {"type": "string"},
                                "what": {"type": "string"},
                                "source_url": {"type": "string"},
                            },
                            "required": ["what", "source_url"],
                        },
                    },
                    "personal": {"type": "array", "items": {"type": "string"}},
                    "conversation_hooks": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["email"],
            },
        }
    },
    "required": ["attendees"],
}


# Focus-pass responseSchema — one attendee's company/space deep dive, merged onto their Pass-A entry.
FOCUS_SCHEMA = {
    "type": "object",
    "properties": {
        "attendees": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "company_deep": {
                        "type": "object",
                        "properties": {
                            "company": {"type": "string"},
                            "builds": {"type": "array", "items": {"type": "string"}},
                            "founder_story": {"type": "string"},
                            "traction": {"type": "array", "items": {"type": "string"}},
                            "market": {"type": "string"},
                        },
                    },
                },
                "required": ["email"],
            },
        }
    },
    "required": ["attendees"],
}


def _diag(msg: str) -> None:
    try:
        from sotto_log import diag  # _LIB already on sys.path (module-level, thread-safe)
        diag(msg)
    except Exception:
        print(msg, file=sys.stderr)


def _metrics():
    """Lazy, best-effort handle on the shared cost/latency accumulator. Swallowed on failure so
    research observability never blocks the brief."""
    try:
        import metrics  # _LIB already on sys.path (module-level, thread-safe)
        return metrics
    except Exception:
        return None


def _domain_of(email: str) -> str:
    email = _s(email)
    return email.split("@")[1].lower().strip() if "@" in email else ""


def _comms_by_email(comms, attendee_emails: set) -> dict:
    """Per-attendee comms context from the user's ALREADY-GATHERED email data (no new gather):
    accepts the raw gmail file ({emails|messages:[…]} or a bare list), a full inputs dict
    ({google:{emails:[…]}}), or gather_google.py --attendee-comms output
    ({email:[{date,subject,snippet,from_me}]} — 30 days of per-attendee threads, a strictly
    better signal than the 24h global gmail file). Returns
    {email: ["email <date> <subject>: <snippet>", …]} (≤3 each) — the real relationship, used to
    disambiguate WHICH person to research and to sharpen relevance."""
    if isinstance(comms, dict) and comms and all(
            isinstance(k, str) and "@" in k and isinstance(v, list) for k, v in comms.items()):
        # gather_google --attendee-comms shape: already keyed per attendee — just format lines.
        out = {}
        for em, rows in comms.items():
            em = em.lower().strip()
            if em not in attendee_emails:
                continue
            lines = []
            for r in rows[:3]:
                if not isinstance(r, dict):
                    continue
                line = "email"
                date = _s(r.get("date")).strip()
                subject = _s(r.get("subject")).strip()
                snippet = _s(r.get("snippet")).strip()[:120]
                if date:
                    line += f" {date}"
                if subject:
                    line += f' "{subject}"'
                line += " (you→them)" if r.get("from_me") else " (them→you)"
                if snippet:
                    line += f": {snippet}"
                lines.append(line)
            if lines:
                out[em] = lines
        return out
    if isinstance(comms, dict):
        emails = comms.get("emails") or comms.get("messages") \
            or (comms.get("google") or {}).get("emails") or []
    elif isinstance(comms, list):
        emails = comms
    else:
        emails = []
    out: dict = {}
    for e in emails:
        if not isinstance(e, dict):
            continue
        headers = e.get("headers") or {}
        addrs = " ".join(_s(headers.get(k) or e.get(k)) for k in ("from", "to", "cc")).lower()
        subject = _s(headers.get("subject") or e.get("subject")).strip()
        snippet = _s(e.get("snippet") or e.get("body")).strip()[:120]
        date = _s(headers.get("date") or e.get("date")).strip()
        for em in attendee_emails:
            if em and em in addrs and len(out.setdefault(em, [])) < 3:
                line = "email"
                if date:
                    line += f" {date}"
                if subject:
                    line += f' "{subject}"'
                if snippet:
                    line += f": {snippet}"
                out[em].append(line)
    return out


def _build_prompt(batch: list, context_summary: str, comms_by_email: dict | None = None,
                  known_by_email: dict | None = None) -> str:
    comms_by_email = comms_by_email or {}
    known_by_email = known_by_email or {}
    lines = []
    for a in batch:
        name = _s(a.get("name")) or _s(a.get("email")).split("@")[0]
        email = _s(a.get("email"))
        domain = _domain_of(email)
        corporate = bool(domain) and domain not in FREEMAIL_DOMAINS
        has_last = len(name.split()) >= 2
        hint = ""
        if corporate:
            hint = (f" (corporate domain {domain} — research the company too)" if has_last
                    else f" (use domain {domain} to identify company)")
        elif not has_last:
            hint = f" (use domain {domain} to identify company)" if domain else ""
        lines.append(f"- {name} <{email}>{hint}")
        known = _s(known_by_email.get(email.lower())).strip()
        if known:
            lines.append(f"  What the user already knows: {known[:400]}")
        for c in (comms_by_email.get(email.lower()) or []):
            lines.append(f"  Recent comms with the user: {c}")
    return (
        f"## Meeting Context\n{context_summary or '(none)'}\n\n"
        f"## Attendees to Research ({len(batch)} people)\n" + "\n".join(lines) + "\n\n"
        "## Task\nFor EACH attendee, search using \"[Name] [Company/Domain] LinkedIn\" and "
        "\"[Company name] product\". Use any \"already knows\"/\"recent comms\" lines to "
        "disambiguate WHICH person this is (right company, right city) and to sharpen relevance — "
        "never as facts to restate. Return one entry per attendee containing:\n"
        "- email: exactly as listed above\n- title: current job title (null if not found)\n"
        "- company: full company name\n- relevance: 1-2 bullets on relevance to the meeting context\n"
        "- summary: 3-4 sentence professional bio — current focus, what they do, 2-3 past roles. "
        "Do NOT include email context or how the user knows them.\n"
        "- company_summary: 1-2 sentences on what their company does (product, customers, stage), "
        "from searching the company name / email domain. ALWAYS attempt this for a corporate "
        "domain — even when the person themselves has no public profile, the company almost always "
        "does. Null only for freemail addresses or when the company search also finds nothing.\n"
        "Degrade person → company → nothing: a found company with an unfindable person is "
        "summary=\"No public profile found.\" PLUS a filled company/company_summary — reserve a "
        "fully empty entry for freemail addresses with unsearchable names.\n"
        "Stay factual — never guess a title, employer, or funding stage. "
        + NO_UNSOURCED_NUMBERS + "\n"
        "If nothing is found: title=null, summary=\"No public profile found.\", relevance=[]."
    )


def _known_facts(a: dict) -> str:
    """The person's EXISTING packed graph facts, for the novelty instruction in Pass B ("find what
    is NEW relative to this"). An explicit `known` string on the attendee (e.g. the knowledge_query
    --person pack the caller already fetched) wins; otherwise the graph profile is read directly.
    Best-effort — an unreadable graph never blocks the sweep, it just loses the novelty anchor."""
    k = _s(a.get("known")).strip()
    if k:
        return k
    try:
        import knowledge as kg  # _LIB already on sys.path (module-level, thread-safe)
        path = kg.find_person_file(name=_s(a.get("name")),
                                   identifier=_s(a.get("email")).strip().lower())
        if not path or not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            p = kg.parse_person_file(f.read())
        bits = []
        head = " at ".join(x for x in (_s(p.title).strip(), _s(p.company).strip()) if x)
        if head:
            bits.append(head)
        bits.extend(f.text for _fid, f in kg.sorted_active_facts(p.facts)[:8])
        return "; ".join(b for b in bits if b)
    except Exception:  # noqa: BLE001
        return ""


def _build_deep_prompt(batch: list, context_summary: str, known_by_email: dict, days: int) -> str:
    year = datetime.now(timezone.utc).year
    lines = []
    for a in batch:
        name = _s(a.get("name")) or _s(a.get("email")).split("@")[0]
        email = _s(a.get("email"))
        lines.append(f"- {name} <{email}>")
        known = _s(known_by_email.get(email.lower())).strip()
        lines.append("  Already known (do NOT repeat any of this): "
                     + (known if known else "(nothing on file — everything genuinely recent is new)"))
    return (
        f"## Meeting Context\n{context_summary or '(none)'}\n\n"
        f"## People to Sweep ({len(batch)} people)\n" + "\n".join(lines) + "\n\n"
        f"## Task — recency & texture sweep (last ~{days} days)\n"
        "Each person's baseline bio is already covered elsewhere. Your ONLY job is what is NEW and "
        f"genuinely theirs from the last ~{days} days. For EACH person, run MULTIPLE distinct "
        "searches — at minimum: \"[Name] blog\", \"[Name] podcast OR talk\", "
        "\"[Name] Twitter OR X OR LinkedIn\", and \"[Name] [Company] " + str(year) + "\". Hunt for:\n"
        "- a new blog post, Substack, or Medium piece they WROTE\n"
        "- a podcast episode, conference talk, or interview they APPEARED on\n"
        "- X/Twitter/LinkedIn posts or threads of theirs that got real traction\n"
        "- launches, funding, job changes, or press about them or their company\n"
        "- PUBLIC personal texture they shared themselves — ran a marathon, wrote about a hobby or "
        "their kids, moved cities. Public and tasteful only: nothing about health, politics, or "
        "family members who didn't post it themselves.\n\n"
        "Return one entry per person:\n"
        f"- email: exactly as listed above (join key — do not alter it)\n"
        f"- recent_activity: up to {MAX_RECENT_ACTIVITY} items {{when, what, source_url}}. `when` is "
        "the publish/event date — approximate is fine (\"late July 2026\"). `what` is one specific "
        "sentence. `source_url` is the page that shows it — REQUIRED; items without a real URL are "
        "discarded.\n"
        f"- personal: up to {MAX_PERSONAL} public personal-texture strings, each ENDING with its "
        "source URL in parentheses — an item without a URL is discarded.\n"
        f"- conversation_hooks: up to {MAX_HOOKS} one-line openers grounded ONLY in what you found "
        "above, e.g. \"He published a piece on agent memory last week — strong hook given your "
        "roadmap.\" Tie to the meeting context when it genuinely fits; never invent a connection.\n\n"
        "Rules:\n"
        "- NOVELTY IS THE POINT: never repeat anything from a person's \"Already known\" line — "
        "return only what is new relative to it.\n"
        "- Everything dated and source-URLed. No engagement-bait speculation.\n"
        "- " + NO_UNSOURCED_NUMBERS + "\n"
        "- If you found nothing concrete about this person or their company, return an empty "
        "conversation_hooks list. NEVER write generic process points like \"ask for "
        "introductions\", \"understand the agenda\", \"explore potential overlaps\", \"build "
        "rapport\" — a hook exists ONLY to point at a specific thing you actually found.\n"
        "- If nothing genuinely recent exists for a person, return empty lists — an empty list "
        "beats a stretch."
    )


def _build_focus_prompt(a: dict, context_summary: str, known: str) -> str:
    """The focus pass: one person, one company, in depth. Same grounding + honesty discipline as
    Pass B — everything traced to a grounded result, "not public" beats a guess."""
    name = _s(a.get("name")) or _s(a.get("email")).split("@")[0]
    email = _s(a.get("email"))
    domain = _domain_of(email)
    corporate = bool(domain) and domain not in FREEMAIL_DOMAINS
    return (
        f"## Meeting Context\n{context_summary or '(none)'}\n\n"
        f"## Focus Person (the ONLY person to research here)\n- {name} <{email}>"
        + (f" (company domain {domain} — that is the company to research)" if corporate else "")
        + "\n  Already known: " + (_s(known).strip() or "(nothing on file)") + "\n\n"
        "## Task — the company and the space, in depth\n"
        "The user is meeting this person and asked for a deep dive, so go past the bio: research "
        "their COMPANY and the market it sits in. Run MULTIPLE distinct searches — at minimum: "
        f"\"[Company] product\", \"[Company] customers OR pricing\", \"{name} founder interview OR "
        "podcast\", \"[Company] funding OR raise\", \"[Company] competitors OR alternatives\".\n\n"
        "Return exactly ONE entry:\n"
        "- email: exactly as listed above (join key — do not alter it)\n"
        "- company_deep:\n"
        "  - company: the company name exactly as it is written publicly — never append a "
        "descriptor you did not read\n"
        f"  - builds: up to {MAX_BUILDS} product areas, one concrete line each — what the product "
        "does and for whom, in plain terms. No marketing copy, no adjectives you can't source.\n"
        "  - founder_story: 2-4 sentences — who founded it, where they came from, and the origin "
        "story (the problem they hit that made them build this). Only what a source states.\n"
        f"  - traction: up to {MAX_TRACTION} signals, each ENDING with its source URL in "
        "parentheses — funding round and date, customer counts, launches, partnerships, press. An "
        "item without a real URL is discarded.\n"
        "  - market: 3-5 sentences on the landscape and why now — who they displace, the adjacent "
        "players BY NAME, and the shift that makes this fundable or relevant now. Attribute a "
        "contested claim to who said it.\n\n"
        "Rules:\n"
        "- \"Not public\" beats a guess: leave a field empty rather than inferring it. Never invent "
        "a founder, a date, a customer, or a competitor — a missing field is the honest answer.\n"
        "- " + NO_UNSOURCED_NUMBERS + "\n"
        "- Everything comes from the grounded results. Do not reason from the company's name, and "
        "do not fill a gap from memory.\n"
        "- If the company itself cannot be identified from the domain or the person, return "
        "company_deep with empty fields — that is a valid answer."
    )


def _gemini_grounded(prompt: str, key: str, use_schema: bool, schema: dict = SCHEMA,
                     timeout: int = PER_BATCH_TIMEOUT, max_tokens: int = MAX_OUTPUT_TOKENS) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={key}"
    gen = {"maxOutputTokens": max_tokens}
    if use_schema:
        gen["responseMimeType"] = "application/json"
        gen["responseSchema"] = schema
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}], "generationConfig": gen}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    import time as _time
    t0 = _time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    m = _metrics()                        # tag this grounded batch as the 'research' phase (thread-safe)
    if m is not None:
        # Guard the CALL itself (not just metrics' internals): a foreign 'metrics' shadow via sys.modules
        # could raise here AFTER the billed batch succeeded and fail research for the whole brief.
        try:
            um = data.get("usageMetadata") or {}
            m.record("research", _time.monotonic() - t0,
                     um.get("promptTokenCount"), um.get("candidatesTokenCount"), MODEL)
        except Exception:
            pass
    cand = (data.get("candidates") or [{}])[0]
    return "".join(p.get("text", "") for p in (cand.get("content", {}).get("parts") or []))


def _grounded_json(prompt: str, key: str, schema: dict, timeout: int, max_tokens: int,
                   fallback_shape: str, tag: str) -> list:
    # Some Gemini versions reject google_search + responseSchema together; if so, retry grounding-only
    # and parse the JSON the prompt asked for. Either way we degrade to [] on failure (never invent).
    for use_schema in (True, False):
        try:
            raw = _gemini_grounded(prompt + ("" if use_schema
                                             else "\n\nReturn ONLY JSON: " + fallback_shape),
                                   key, use_schema, schema=schema, timeout=timeout,
                                   max_tokens=max_tokens)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1].lstrip("json").strip() if "```" in raw else raw
            return (json.loads(raw) or {}).get("attendees", []) or []
        except Exception as e:  # noqa: BLE001
            if use_schema:
                continue  # try grounding-only
            _diag(f"[research_attendees] {tag} batch failed ({type(e).__name__}: {str(e)[:100]})")
            return []
    return []


def _research_batch(batch: list, context_summary: str, key: str,
                    comms_by_email: dict | None = None,
                    known_by_email: dict | None = None) -> list:
    return _grounded_json(_build_prompt(batch, context_summary, comms_by_email, known_by_email),
                          key, SCHEMA, PER_BATCH_TIMEOUT, MAX_OUTPUT_TOKENS,
                          "{\"attendees\":[{email,title,company,relevance,summary,"
                          "company_summary}]}", "profile")


def _deep_batch(batch: list, context_summary: str, key: str, known_by_email: dict) -> list:
    return _grounded_json(_build_deep_prompt(batch, context_summary, known_by_email, _recency_days()),
                          key, DEEP_SCHEMA, DEEP_PER_BATCH_TIMEOUT, DEEP_MAX_OUTPUT_TOKENS,
                          "{\"attendees\":[{email,recent_activity:[{when,what,source_url}],"
                          "personal,conversation_hooks}]}", "recency")


def _focus_batch(attendee: dict, context_summary: str, key: str, known: str) -> list:
    return _grounded_json(_build_focus_prompt(attendee, context_summary, known),
                          key, FOCUS_SCHEMA, FOCUS_TIMEOUT, FOCUS_MAX_OUTPUT_TOKENS,
                          "{\"attendees\":[{email,company_deep:{company,builds,founder_story,"
                          "traction,market}}]}", "focus")


def _postfilter_focus(entry: dict) -> dict:
    """Deterministic grounding discipline for the focus pass (in code, not just the prompt):
      • every traction item MUST carry its source URL in the string — url-less items are dropped
      • empty strings/lists are dropped entirely, so an empty field can never render as a heading
      • hard caps: 5 builds / 4 traction."""
    cd = entry.get("company_deep")
    if not isinstance(cd, dict):
        return {}
    builds = [_s(s).strip() for s in (cd.get("builds") or []) if isinstance(s, str)]
    traction = [_s(s).strip() for s in (cd.get("traction") or []) if isinstance(s, str)]
    traction = [t for t in traction if t and re.search(r"https?://\S", t)]
    out = {}
    for k, v in (("company", _s(cd.get("company")).strip()),
                 ("builds", [b for b in builds if b][:MAX_BUILDS]),
                 ("founder_story", _s(cd.get("founder_story")).strip()),
                 ("traction", traction[:MAX_TRACTION]),
                 ("market", _s(cd.get("market")).strip())):
        if v:
            out[k] = v
    # A company name alone is not a deep dive — it's already on the Pass-A entry.
    return {"company_deep": out} if set(out) - {"company"} else {}


def _postfilter_deep(entry: dict) -> dict:
    """Deterministic grounding discipline for Pass B (in code, not just the prompt):
      • every recent_activity item MUST carry a real source_url — url-less items are dropped
      • personal items MUST carry an explicit public source (an http(s) URL) in the string — dropped
        otherwise
      • hard caps: 4 activity / 2 personal / 2 hooks."""
    ra = []
    for it in entry.get("recent_activity") or []:
        if not isinstance(it, dict):
            continue
        what = _s(it.get("what")).strip()
        url = _s(it.get("source_url")).strip()
        if what and url.lower().startswith(("http://", "https://")):
            ra.append({"when": _s(it.get("when")).strip(), "what": what, "source_url": url})
    personal = [_s(s).strip() for s in (entry.get("personal") or []) if isinstance(s, str)]
    personal = [s for s in personal if s and re.search(r"https?://\S", s)]
    hooks = [_s(s).strip() for s in (entry.get("conversation_hooks") or [])
             if isinstance(s, str) and _s(s).strip()]
    hooks = [h for h in hooks if not is_filler_point(h)]  # no "build rapport" fabrication
    return {"recent_activity": ra[:MAX_RECENT_ACTIVITY], "personal": personal[:MAX_PERSONAL],
            "conversation_hooks": hooks[:MAX_HOOKS]}


def _within_research_horizon(a: dict) -> bool:
    """Tiered-spend gate for Pass B: select_attendees already limits selection to meetings within
    RESEARCH_HORIZON_HOURS (72h), so normally everyone here qualifies. This re-check only matters
    for direct callers passing arbitrary lists — an unparseable/absent meeting_start trusts the
    selector and stays in."""
    st = _parse_ts(_s(a.get("meeting_start")))
    if st is None:
        return True
    if st.tzinfo is None:
        st = st.replace(tzinfo=timezone.utc)
    hours_away = (st - datetime.now(timezone.utc)).total_seconds() / 3600.0
    return -1 <= hours_away <= RESEARCH_HORIZON_HOURS


def research(attendees: list, context_summary: str, comms=None, focus: str = "") -> dict:
    m = _metrics()
    if m is not None:
        try:
            m.start_run()
        except Exception:
            pass

    def _fin(res: dict) -> dict:
        # Emit the research phase's own [brief-cost] line + jsonl record (research runs as its own
        # process, ahead of compose, so it can't share compose's in-memory accumulator).
        if m is not None:
            try:
                import datetime as _dt
                m.emit(_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"), "research")
            except Exception:
                pass
        return res

    stub = os.environ.get("SOTTO_LLM_STUB")
    if stub:
        try:
            with open(stub, encoding="utf-8") as f:
                return _fin({"attendees": (json.load(f) or {}).get("attendees", [])})
        except Exception:
            return _fin({"attendees": []})
    key = os.environ.get("GOOGLE_AI_API_KEY")
    if not key or not attendees:
        if attendees and not key:   # honest degrade — never a silent {"attendees":[]}
            _diag("[research] skipped — GOOGLE_AI_API_KEY not set")
        return _fin({"attendees": []})
    # dedupe by email (lowercase), cap at 25
    seen, unique = set(), []
    for a in attendees:
        em = _s(a.get("email")).lower()
        if em and em not in seen:
            seen.add(em); unique.append(a)
    capped = unique[:MAX_ATTENDEES]
    if len(unique) > MAX_ATTENDEES:
        _diag(f"[research_attendees] capping {len(unique)} → {MAX_ATTENDEES}")
    batches = [capped[i:i + BATCH_SIZE] for i in range(0, len(capped), BATCH_SIZE)]

    # Per-attendee grounding context, shared by both passes: what the user already knows (graph
    # facts — the Pass-B novelty anchor, the Pass-A disambiguator) + real comms with the user.
    known_by_email = {_s(a.get("email")).lower(): _known_facts(a) for a in capped}
    comms_by_email = _comms_by_email(comms, set(known_by_email))

    # Pass B (recency sweep) fan-out: same pool, its own smaller batches; horizon-gated + env knob.
    deep_targets = [a for a in capped if _within_research_horizon(a)] if _deep_enabled() else []
    deep_batches = [deep_targets[i:i + DEEP_BATCH_SIZE]
                    for i in range(0, len(deep_targets), DEEP_BATCH_SIZE)]

    # Focus pass: ONE extra grounded call for the ONE person the user named (depth follows focus).
    focus_target = next((a for a in capped
                         if matches_focus(focus, _s(a.get("name")), _s(a.get("email")))),
                        None) if (focus and _deep_enabled()) else None
    if focus and not focus_target:
        _diag(f"[research_attendees] --focus {focus!r} matched no attendee — sweep research only")

    profile_rows, deep_rows, focus_rows = [], [], []
    n_futs = len(batches) + len(deep_batches) + (1 if focus_target else 0)
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENCY, n_futs or 1)) as ex:
        futs = {ex.submit(_research_batch, b, context_summary, key, comms_by_email,
                          known_by_email): "profile" for b in batches}
        futs.update({ex.submit(_deep_batch, b, context_summary, key, known_by_email): "deep"
                     for b in deep_batches})
        if focus_target:
            futs[ex.submit(_focus_batch, focus_target, context_summary, key,
                           known_by_email.get(_s(focus_target.get("email")).lower(), ""))] = "focus"
        for fut in as_completed(futs):
            try:
                rows = fut.result() or []
            except Exception as e:  # noqa: BLE001 — one failed batch never fails the others
                _diag(f"[research_attendees] {futs[fut]} batch crashed "
                      f"({type(e).__name__}: {str(e)[:100]})")
                rows = []
            {"profile": profile_rows, "deep": deep_rows, "focus": focus_rows}[futs[fut]].extend(rows)

    # Merge: profile entries keyed by email, in input order where possible; recency-sweep fields
    # post-filtered then attached. A sweep hit with no Pass-A profile still surfaces (thin entry).
    by_email = {}
    for r in profile_rows:
        em = _s(r.get("email")).lower().strip()
        if em:
            by_email.setdefault(em, r)
    for d in deep_rows:
        em = _s(d.get("email")).lower().strip()
        if not em:
            continue
        clean = _postfilter_deep(d)
        if not any(clean.values()):
            continue  # an empty list beats a stretch — nothing to attach
        entry = by_email.setdefault(em, {"email": em, "title": None, "company": "",
                                         "relevance": [], "summary": ""})
        entry.update(clean)
    if focus_target:
        # The focus call asked about exactly ONE person, so it joins on the TARGET's email, not on
        # whatever the model echoed back — the identity can't drift onto a different attendee.
        em = _s(focus_target.get("email")).lower().strip()
        for f in focus_rows:
            clean = _postfilter_focus(f) if isinstance(f, dict) else {}
            if not clean:
                continue
            entry = by_email.setdefault(em, {"email": em, "title": None, "company": "",
                                             "relevance": [], "summary": ""})
            entry.update(clean)
            break
    order = {_s(a.get("email")).lower(): i for i, a in enumerate(capped)}
    out = sorted(by_email.values(),
                 key=lambda r: order.get(_s(r.get("email")).lower(), len(order)))
    n_deep = sum(1 for r in out if r.get("recent_activity") or r.get("personal")
                 or r.get("conversation_hooks"))
    _diag(f"[research_attendees] {len(out)}/{len(capped)} researched in {len(batches)} profile + "
          f"{len(deep_batches)} recency" + (" + 1 focus" if focus_target else "")
          + f" grounded call(s); {n_deep} with recent activity")
    return _fin({"attendees": out})


CACHE_KEEP_DAYS = 7   # dashboard research-card cache retention ($SOTTO_DATA/cache/research_*.json)


def _persist_research_cache(result: dict) -> None:
    """The Window's persistence hook (docs/plans/web-dashboard-the-window.md): the --out file is
    transient (/tmp/sotto_research.json), so after a SUCCESSFUL run ALSO write the research output
    verbatim (+ written_at) to $SOTTO_DATA/cache/research_<local-date>.json — the dashboard's
    meeting-research cards — pruning siblings older than CACHE_KEEP_DAYS. Same best-effort
    discipline as compose_brief._archive_brief: a cache failure never fails research. Runs that
    produced NO attendees skip entirely, so a skipped/failed afternoon re-run can't clobber the
    morning's cache with nothing (unlike --out, which is deliberately always truncated)."""
    try:
        if not (isinstance(result, dict) and result.get("attendees")):
            return
        tz = configured_tz()
        date = _user_local_date(tz)
        d = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "cache")
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".research_{date}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({**result, "written_at":
                       datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f)
        os.replace(tmp, os.path.join(d, f"research_{date}.json"))
        cutoff = (_now_local(tz) - timedelta(days=CACHE_KEEP_DAYS)).strftime("%Y-%m-%d")
        for n in os.listdir(d):
            m = re.match(r"\Aresearch_(\d{4}-\d{2}-\d{2})\.json\Z", n)
            if m and m.group(1) < cutoff:
                try:
                    os.remove(os.path.join(d, n))
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 — best-effort: an unwritable volume never fails research
        pass


def _context_summary(calendar) -> str:
    events = calendar.get("events") if isinstance(calendar, dict) else calendar
    if not isinstance(events, list):
        return ""
    rows = []
    for e in sorted(events, key=lambda x: _s(x.get("start"))):
        t = _s(e.get("summary") or e.get("title"))
        desc = _s(e.get("description"))[:500]
        if t:
            rows.append(f'Meeting "{t}" at {_s(e.get("start"))}' + (f": {desc}" if desc else ""))
    return "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--attendees")
    ap.add_argument("--context")
    ap.add_argument("--comms", help="already-gathered email JSON — /tmp/sotto_attendee_comms.json "
                                    "({email:[{date,subject,snippet,from_me}]}) or the raw gmail "
                                    "file ({emails:[…]}, a bare list, or a full inputs dict); "
                    "optional, gives the researcher the real relationship context per attendee")
    ap.add_argument("--focus", default="",
                    help="ONE attendee (name substring or exact email) gets an extra grounded call "
                         "covering their company and space (company_deep) — use it when the user "
                         "named a person; no match degrades to ordinary sweep research")
    ap.add_argument("--out", default="/tmp/sotto_research.json",
                    help="output file — always truncated+rewritten (even to {\"attendees\":[]}), so "
                         "a skipped/failed research step can't leave the MORNING's file for an "
                         "afternoon prep to reuse as stale bios")
    a = ap.parse_args()

    def load(p, d):
        if not p:
            return d
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return d

    attendees = load(a.attendees, None)
    if attendees is None and not sys.stdin.isatty():
        try:
            attendees = json.loads(sys.stdin.read() or "[]")
        except Exception:
            attendees = []
    attendees = attendees if isinstance(attendees, list) else (attendees or {}).get("attendees", []) if isinstance(attendees, dict) else []
    result = research(attendees, _context_summary(load(a.context, [])),
                      comms=load(a.comms, None), focus=a.focus)
    payload = json.dumps(result)
    try:
        with open(a.out, "w", encoding="utf-8") as f:   # own the file; no shell redirect needed
            f.write(payload)
    except OSError as e:
        _diag(f"[research_attendees] could not write {a.out}: {e}")
    _persist_research_cache(result)   # dashboard research cards (best-effort; empty runs skip)
    print(payload)   # stdout kept for compatibility with redirect-style invocations


if __name__ == "__main__":
    main()
