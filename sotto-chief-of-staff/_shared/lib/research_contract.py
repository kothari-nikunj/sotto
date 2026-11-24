"""Pure research/prep qualification and retained-result limits; no provider or source reads."""
import json
import re
from urllib.parse import urlsplit

from textutil import FREEMAIL_DOMAINS, _s

MAX_ATTENDEES = 25
MAX_ATTENDEES_TO_RESEARCH = MAX_ATTENDEES
RESEARCH_HORIZON_HOURS = 72
MAX_RECENT_ACTIVITY = 4
MAX_PERSONAL = 2
MAX_BUILDS = 5
MAX_TRACTION = 4
CACHE_KEEP_DAYS = 7
MAX_DOMAIN_CHARS = 253

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
    isn't anchored to anything concrete. Applied by compose_meeting_prep's talking-points
    post-filter — the one place this tree still emits "what to say"."""
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


# Shared by grounded research and local prep composition.
SOURCE_FIDELITY = (
    "Preserve what each source actually establishes and its qualifications. For example, "
    "membership does not establish certification, and an organization footer does not "
    "establish a listed company's products or capabilities. Attribute self-reported claims "
    "and retain uncertainty; omit unsupported conclusions. "
    "Copy source URLs exactly from the supplied "
    "evidence or grounded results; never reconstruct paths or substitute a guessed link. "
    "An inaccessible or mismatched source leaves its claim unverified."
)

NO_UNSOURCED_NUMBERS = (
    "No unsourced numbers. Do NOT state specific dollar figures — ARR, valuation, raise size, "
    "acquisition/exit price — unless they appear in the grounded result's citations. A named stage "
    "(\"raised a Series A\") is fine if reported; \"$2.4M ARR\" or \"a $115M exit\" is allowed ONLY "
    "if a citation backs it — otherwise omit the number (write \"acquired by X\", not \"acquired by "
    "X for $Y\"). These figures get written into the user's permanent knowledge graph, so an "
    "invented one persists — when unsure, leave it out. "
    + SOURCE_FIDELITY
)

def attendee_company_domain(email: str) -> str:
    address, separator, domain = _s(email).strip().partition("@")
    domain = company_domain(domain) if address and separator else ""
    return "" if domain in FREEMAIL_DOMAINS else domain


TRACTION_SOURCE_RULE = (
    "Use one complete verbatim source sentence per traction item, including the company name, "
    "followed by the exact source URL in parentheses. Use the official company site or a source "
    "whose same sentence also names the company's exact domain. Keep numbers, funding, "
    "partnerships, and certification in traction, with the source's original scope. "
    "Leave traction empty when no such sentence is available."
)


_CERTIFICATION_TERMS = re.compile(
    r"\b(?:certif(?:ied|ies|y|icates?|ications?)|accredit(?:ed|s|ations?))\b", re.IGNORECASE
)


def company_description(value: str) -> str:
    """Hold certification terms in unchecked public research prose."""
    text = _s(value).strip()
    return "" if _CERTIFICATION_TERMS.search(text) else text


def _traction_parts(item):
    match = re.fullmatch(r"(.+?[.!?]) \((https?://\S+)\)", item)
    if not match:
        return "", ""
    claim, url = match.groups()
    # An entire source sentence keeps qualifications such as "not certified".
    if re.search(r"[.!?]\s", claim):
        return "", ""
    return claim, url


def project_traction_sources(entry, grounding):
    """Retain only matching source sentences from provider transport, never model JSON."""
    deep = entry.get("company_deep")
    items = deep.get("traction") if isinstance(deep, dict) else None
    if not isinstance(items, list) or not isinstance(grounding, list):
        return {}
    claims = {_traction_parts(item.strip()) for item in items if isinstance(item, str)}
    sources = {}
    for basis in grounding:
        citations = basis.get("citations") if isinstance(basis, dict) else None
        for citation in citations if isinstance(citations, list) else ():
            excerpts = citation.get("excerpts") if isinstance(citation, dict) else None
            url = citation.get("url") if isinstance(citation, dict) else None
            if not isinstance(url, str) or not isinstance(excerpts, list):
                continue
            for excerpt in excerpts:
                if not isinstance(excerpt, str):
                    continue
                sentences = re.split(r"(?<=[.!?])\s+", " ".join(excerpt.split()))
                for sentence in sentences:
                    if (sentence, url) in claims and sentence not in sources.get(url, ()):
                        sources.setdefault(url, []).append(sentence)
    return sources


def grounded_traction(entry, domain):
    """Yield exact source sentences that establish both the claim and the company identity."""
    deep = entry.get("company_deep")
    sources = entry.get("traction_sources")
    if not (domain and isinstance(deep, dict) and isinstance(sources, dict)):
        return
    name = _s(deep.get("company")).strip()
    items = deep.get("traction")
    if not name or not isinstance(items, list):
        return
    domain_pattern = rf"(?<![\w.-]){re.escape(domain)}(?![\w-]|\.[\w-])"
    name_pattern = rf"(?<!\w){re.escape(name)}(?!\w)"
    for item in items:
        claim, url = _traction_parts(item.strip()) if isinstance(item, str) else ("", "")
        excerpts = sources.get(url)
        if not claim or not isinstance(excerpts, list) or claim not in excerpts:
            continue
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            continue
        if host and re.search(name_pattern, claim, re.IGNORECASE) and (
                host.removeprefix("www.") == domain.removeprefix("www.")
                or re.search(domain_pattern, claim, re.IGNORECASE)):
            yield claim, url


def postfilter_focus(entry: dict, *, domain: str = "") -> dict:
    """Drop empty fields and traction without retained source and company evidence."""
    cd = entry.get("company_deep")
    if not isinstance(cd, dict):
        return {}
    builds = [company_description(s) for s in (cd.get("builds") or []) if isinstance(s, str)]
    supported = list(grounded_traction(entry, domain))[:MAX_TRACTION]
    traction = [f"{claim} ({url})" for claim, url in supported]
    out = {}
    for k, v in (("company", _s(cd.get("company")).strip()),
                 ("builds", [b for b in builds if b][:MAX_BUILDS]),
                 ("founder_story", company_description(cd.get("founder_story"))),
                 ("traction", traction[:MAX_TRACTION]),
                 ("market", company_description(cd.get("market")))):
        if v:
            out[k] = v
    # A company name alone is not a deep dive — it's already on the Pass-A entry.
    if not set(out) - {"company"}:
        return {}
    filtered = {"company_deep": out}
    if supported:
        filtered["traction_sources"] = {}
        for claim, url in supported:
            filtered["traction_sources"].setdefault(url, []).append(claim)
    return filtered


def postfilter_company(entry: dict, *, domain: str = "") -> dict:
    # Grounding-only fallbacks may not enforce the provider schema. Do not turn
    # malformed list fields into individual characters or coerced facts.
    deep = entry.get("company_deep")
    if not isinstance(deep, dict) or not isinstance(deep.get("company"), str):
        return {}
    if any(key in deep and not isinstance(deep[key], str)
           for key in ("company", "founder_story", "market")):
        return {}
    if any(key in deep and (not isinstance(deep[key], list)
           or any(not isinstance(item, str) for item in deep[key]))
           for key in ("builds", "traction")):
        return {}
    return postfilter_focus(entry, domain=domain)


def project_research_for_answer(research, *, company_selector="", attendee_email=""):
    """Render checked research without exposing raw provider output or changing receipts."""
    result = {key: value for key, value in research.items() if key != "research_evidence"}
    key = "companies" if company_selector else "attendees"
    rows = result.get(key)
    if not isinstance(rows, list):
        return result
    result[key] = []
    email = _s(attendee_email).strip().lower()
    domain = company_domain(company_selector) if company_selector else attendee_company_domain(email)
    for row in rows:
        if not isinstance(row, dict):
            continue
        if company_selector:
            filtered = postfilter_company(row, domain=domain)
        else:
            row_domain = domain if _s(row.get("email")).strip().lower() == email else ""
            filtered = postfilter_focus(row, domain=row_domain)
        projected = {field: value for field, value in row.items()
                     if field not in {"company_deep", "traction_sources", "summary", "company_summary", "relevance", "domain"}}
        for field in ("summary", "company_summary"):
            if description := company_description(row.get(field)):
                projected[field] = description
        if isinstance(row.get("relevance"), list):
            projected["relevance"] = list(filter(None, map(company_description, row["relevance"])))
        projected.update({field: value for field, value in postfilter_deep(row).items() if field in row})
        if company_selector and domain and row.get("domain") == domain:
            projected["domain"] = domain
        if "company_deep" in filtered:
            projected["company_deep"] = filtered["company_deep"]
        result[key].append(projected)
    return result


def postfilter_deep(entry: dict) -> dict:
    """Deterministic grounding discipline for Pass B (in code, not just the prompt):
      • every recent_activity item MUST carry a real source_url — url-less items are dropped
      • personal items MUST carry an explicit public source (an http(s) URL) in the string — dropped
        otherwise
      • hard caps: 4 activity / 2 personal."""
    ra = []
    for it in entry.get("recent_activity") or []:
        if not isinstance(it, dict):
            continue
        what = company_description(it.get("what"))
        url = _s(it.get("source_url")).strip()
        if what and url.lower().startswith(("http://", "https://")):
            ra.append({"when": _s(it.get("when")).strip(), "what": what, "source_url": url})
    personal = [company_description(s) for s in (entry.get("personal") or []) if isinstance(s, str)]
    personal = [s for s in personal if s and re.search(r"https?://\S", s)]
    return {"recent_activity": ra[:MAX_RECENT_ACTIVITY], "personal": personal[:MAX_PERSONAL]}



def company_domain(selector: str) -> str:
    """Only a bare domain supplied by the owner, never one inferred by a research model."""
    if (len(selector) <= MAX_DOMAIN_CHARS and re.fullmatch(
            r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}", selector)):
        return selector.lower()
    return ""


def evidence_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def prep_research_evidence(research, records=None):
    """Bound retained provider reports; absence and clipping never imply verification."""
    import hashlib
    from retained_brief_proof import (validate_research_evidence, MAX_RESEARCH_EVIDENCE,
                                      MAX_RESEARCH_RECORDS, MAX_OMITTED_RESEARCH_RECORDS)
    source = research if isinstance(research, dict) else {}
    evidence = source.get('research_evidence') or {}
    candidates = records if records is not None else evidence.get('records', [])
    if not candidates:
        return {'status': 'unavailable'}
    # This is the exact post-filtered research input to local prep composition, excluding its
    # metadata. Original provider responses below remain separately identified, never conflated.
    used = {key: value for key, value in source.items() if key != 'research_evidence'}
    result = {'status': 'retained', 'input_sha256': hashlib.sha256(evidence_json(used).encode()).hexdigest(),
              'records': [], 'omitted_records': int(evidence.get('omitted_records', 0))}
    for record in sorted(candidates, key=lambda item: evidence_json(item)):
        trial = {**result, 'records': [*result['records'], record]}
        # Reserve digits for later omission counts before deciding this record fits.
        bounded_trial = {**trial, 'omitted_records': MAX_OMITTED_RESEARCH_RECORDS}
        if len(trial['records']) <= MAX_RESEARCH_RECORDS and len(evidence_json(bounded_trial).encode()) <= MAX_RESEARCH_EVIDENCE:
            validate_research_evidence(trial)
            result = trial
        else:
            result['omitted_records'] += 1
    return result if result['records'] else {'status': 'unavailable'}

