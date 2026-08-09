#!/usr/bin/env python3
"""
compose_brief.py — the brief extraction, as a script that calls Gemini DIRECTLY.

This is a faithful Python port of the Mac backend's `extractFlexBriefData`
(api/src/services/gemini-flex.ts). It (a) loads the FULL FLEX extraction prompt from
`morning-brief/references/extraction-prompt.md`, (b) renders every LocalData / Google /
Granola source into the prompt's data section exactly like the backend's
`formatSourceForLLM` helpers — deferred-unread items capped at DEFERRED_UNREAD_PROMPT_CAP,
contact names resolved, phones/emails/JIDs normalized — and (c) returns the same normalized
brief contract the rest of the skill expects.

This file used to be a 2,400-line monolith that doubled as the pack's utility library. The
pure-utility layers were split into _shared/lib/{textutil,timeutil,gemini,chatfmt,render_local}.py
and this file is now a LEAF: it keeps the ORCHESTRATION (compose / build_prompt), the
extraction/critic/revise flow, signal correlation, preference application, tap-link building and
the coverage/first-run lines, and nothing else in the pack imports it. Siblings that need a shared
helper import the owning lib module directly — loading the brief engine (and its 566-line prompt)
to reach `_s()` is exactly the edge that was removed.

Two ways to run the extraction:
  1. NATIVE (default, simplest): the host's own model runs the FLEX prompt — the host (Hermes/OpenClaw)
     already manages the provider + key. **Requires the host model to be 1M-context (use Gemini).**
     No script key needed; the skill just instructs the agent.
  2. THIS SCRIPT (deterministic / host-model-independent): a single structured Gemini call. Use it
     when the host's global model isn't 1M (so the brief still works without clobbering their model).
     It reads `GOOGLE_AI_API_KEY` — the SAME key the host stores natively and passes to execute_code,
     NOT a second key store. This is the "processing core" reduced to one stdlib script.

Input  (stdin or argv[1], JSON): { type, window_hours, google, granola, local, prior_knowledge?, first_run? }
  google: { emails[], events[], userEmail?, userTimezone? }
  local:  the LocalData payload (the 16 sources + intelligence context). Missing fields are treated as empty.
Output (stdout, JSON): { brief_markdown, actions[], meetings_needing_prep[], extracted_knowledge }

Env: GOOGLE_AI_API_KEY (host's native Gemini key), SOTTO_GEMINI_MODEL (default gemini-3.6-flash).
Test mode: set SOTTO_LLM_STUB=/path/to/response.json to bypass the network and return that file.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# The pure-utility layers this file uses, imported from their OWNERS in _shared/lib. This is a
# plain dependency list, not a re-export surface: siblings that want `_s` or `_now_local` import
# textutil / timeutil themselves, so nothing loads the brief engine to reach a string helper.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from textutil import (  # noqa: E402
    _arr, _obj, _s, _names_match, _is_excluded_domain,
    _base_domain, _sender_addr, _extract_sender_name, unwrap_tool_result,
)
from timeutil import (  # noqa: E402
    _date_only, _parse_ts, _tz_offset_minutes,
    configured_tz, configured_user_email, _resolve_tz, _user_tz_offset,
    _user_local_date, _time_frame,
)
from gemini import _diag, call_gemini  # noqa: E402
from chatfmt import to_chat  # noqa: E402  (the ONE markdown→chat transformation)
import metrics  # noqa: E402  (cost/latency observability — best-effort, never blocks a brief)
from render_local import (  # noqa: E402
    build_contact_lookup,
    resolve_contact_names, _action_age,
    _group_messages_into_threads,
    _thread_needs_response, _thread_is_known_person,
    _format_threads_as_text, _trim_email, _format_emails, _format_calendar,
    MAX_ATTENDEES_TO_RESEARCH, RESEARCH_HORIZON_HOURS,
    _known_identities, _format_attendee_research, _format_reminders,
    _format_birthdays, _format_missed_calls, _format_recent_calls, _stale_local_note,
    _format_source_availability, _format_deferred_unread, _format_stale_threads,
    _format_past_commitments, _format_action_ledger, _format_attention_queue,
    _format_relationship_insights, _format_knowledge_section, _format_contact_notes,
    _format_apple_notes, _format_granola_meetings, _format_top_browsing_domains,
    _format_search_queries, _format_screen_time, _format_recent_files, _format_meeting_archive,
    _format_reconciliation, _format_signal_scores, _format_granola_context,
    _format_file_matches, _format_domain_research, _format_escalation_signals,
)
# The ONE link builder (sibling script): _action_tap_link routes an action to a channel, link_for
# builds every URL — one place a link scheme can be wrong, one place a draft can be recorded.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from action_links import link_for  # noqa: E402

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "morning-brief", "references", "extraction-prompt.md")

# How long an offline-Bridge local snapshot stays usable. The cache is a BACKUP, not the default
# path — a live read_local always wins. Past this, we'd rather brief with no local than re-surface
# day(s)-old "needs reply" threads as if they're fresh, so an expired snapshot is dropped.
LOCAL_SNAPSHOT_TTL_HOURS = 24

# Ledger entries the brief never renders: a meeting-prep/meeting-info loop is a calendar shadow, not
# an ask, and the docket already covers it. Same rule (and same two names) the dashboard's
# /api/loops and the overview apply — legacy hyphen spellings normalize onto these.
MEETING_LEDGER_ACTION_TYPES = frozenset({"meeting_prep", "meeting_info"})


# The documented seam between the static policy (Gemini systemInstruction) and the per-brief data
# template (the user turn). Everything above it is policy; everything below is rendered per brief.
_SPLIT_SEAM = "<!-- SYSTEM/USER SPLIT -->"
_DATA_SEAM_RE = re.compile(r"(?m)^## DATA PROMPT[ \t]*$")   # legacy seam (pre-contract templates)

# Comment prefixes (after "<!--") that are NOT maintainer prose and must survive the strip: the
# model-facing tap-marker syntax the model is taught to emit, and the split seam token.
_KEEP_COMMENT_PREFIXES = ("id:", "meeting:", " SYSTEM/USER SPLIT")

_STANDALONE_CLOSE_RE = re.compile(r"(?m)^[ \t]*-->[ \t]*$")


def _strip_maintainer_comments(text: str) -> str:
    """Remove every HTML comment from the template EXCEPT tap-marker syntax and the split seam.

    Maintainer-facing prose lives in comments (the template's LOADER CONTRACT marks them
    "<!-- MAINTAINER: ... -->", but we strip ALL non-marker comments defensively — repo notes must
    never cost tokens or leak to the model). A plain regex can't do this: the maintainer block
    QUOTES comment syntax (`-->` mid-prose), so a non-greedy match mis-terminates. Rule used here:
    an inline comment closes at the first `-->` on its opening line; a block comment closes at the
    next standalone `-->` line. Deterministic → the loaded template stays byte-stable."""
    out, i, n = [], 0, len(text)
    while True:
        j = text.find("<!--", i)
        if j < 0:
            out.append(text[i:])
            break
        if text[j + 4:].startswith(_KEEP_COMMENT_PREFIXES):
            k = text.find("-->", j)
            end = (k + 3) if k >= 0 else n
            out.append(text[i:end])
            i = end
            continue
        out.append(text[i:j])
        line_end = text.find("\n", j)
        line_end = n if line_end < 0 else line_end
        close_inline = text.find("-->", j, line_end)
        if close_inline >= 0:
            i = close_inline + 3
        else:
            m = _STANDALONE_CLOSE_RE.search(text, line_end)
            i = m.end() if m else n
        # If the strip started at a line begin and left a dangling newline, drop it too.
        if (j == 0 or text[j - 1] == "\n") and i < n and text[i] == "\n":
            i += 1
    return "".join(out)


def _load_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return _strip_maintainer_comments(f.read())


def _split_prompt(template: str) -> tuple[str, str]:
    """Split the loaded template at the documented seam into (system_instruction, user_template).

    Seam (per the template's LOADER CONTRACT): the single `<!-- SYSTEM/USER SPLIT -->` comment —
    everything above it (from the `## SYSTEM INSTRUCTION` heading down) is the static policy sent as
    Gemini's systemInstruction; everything below is the per-brief user turn that build_prompt
    renders. A legacy `## DATA PROMPT` heading works as fallback seam.

    The system side is BYTE-STABLE across runs — Gemini's implicit prefix caching then applies
    automatically (cached input is ~10× cheaper). We intentionally do NOT register explicit
    cachedContents: two brief calls a day against a 1h cache TTL would never hit. A template without
    any seam degrades to the old single-prompt behavior (empty system, full template as user)."""
    idx = template.find(_SPLIT_SEAM)
    if idx >= 0:
        system, user = template[:idx], template[idx + len(_SPLIT_SEAM):]
    else:
        m_legacy = _DATA_SEAM_RE.search(template)
        if m_legacy is None:
            return "", template
        system, user = template[:m_legacy.start()], template[m_legacy.end():]
    m = system.find("## SYSTEM INSTRUCTION")
    if m >= 0:
        system = system[m + len("## SYSTEM INSTRUCTION"):]
    system = re.sub(r"\n-{3,}\s*$", "", system.rstrip()).strip()
    return system, user.lstrip("\n")


# responseSchema for the three-field brief contract (generationConfig.responseSchema — the same
# approach research_attendees.py already uses). Gemini's schema dialect is an OpenAPI subset with no
# additionalProperties, so "permissive" here means DECLARING the full documented field set (a field
# not declared cannot be emitted under controlled generation) while requiring almost nothing at the
# item level — rich optional fields still pass, and _normalize_output's aliases keep working.
_ACTION_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "type": {"type": "string"},
        "channel": {"type": "string"},
        "sectionType": {"type": "string"},
        "contactName": {"type": "string"},
        "contactIdentifier": {"type": "string"},
        "contextSummary": {"type": "string"},
        "contextAsk": {"type": "string"},
        "contextDeadline": {"type": "string"},
        "deadlineDate": {"type": "string"},
        "contextUrgencyReason": {"type": "string"},
        "prose": {"type": "string"},
        "confidence": {"type": "number"},
        "messageCount": {"type": "integer"},
        "sourceLinks": {"type": "array", "items": {"type": "string"}},
        "externalContext": {"type": "array", "items": {"type": "string"}},
        "internalContext": {"type": "array", "items": {"type": "string"}},
        "background": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "object", "properties": {
            "sourceType": {"type": "string"}, "sourceId": {"type": "string"},
            "snippet": {"type": "string"}}}},
        "deduplication": {"type": "object", "properties": {
            "relatedChannels": {"type": "array", "items": {"type": "string"}}}},
        "threadSnippet": {"type": "string"},
        "userStyleExamples": {"type": "array", "items": {"type": "string"}},
        "emailReplyTo": {"type": "string"},
        "emailSubject": {"type": "string"},
        "emailThreadId": {"type": "string"},
        "emailMessageId": {"type": "string"},
        "emailReferences": {"type": "string"},
        "eventId": {"type": "string"},
        "meetingTime": {"type": "string"},
        "meetingLocation": {"type": "string"},
        "meetingLink": {"type": "string"},
        "crossChannelContext": {"type": "string"},
    },
    "required": ["channel", "contextSummary"],
}

BRIEF_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "markdown": {"type": "string"},
        "actionItems": {"type": "array", "items": _ACTION_ITEM_SCHEMA},
        "extractedKnowledge": {"type": "object", "properties": {
            "person_updates": {"type": "array", "items": {"type": "object", "properties": {
                "canonical_id": {"type": "string"}, "person_name": {"type": "string"},
                "identifier": {"type": "string"},
                "facts": {"type": "array", "items": {"type": "object", "properties": {
                    "fact": {"type": "string"}, "memory_type": {"type": "string"},
                    "confidence": {"type": "number"}, "change_type": {"type": "string"}}}},
                "profile_patch": {"type": "object", "properties": {
                    "title": {"type": "string"}, "company": {"type": "string"}}},
                # Typed edges between two people — closed vocabulary, enforced by
                # knowledge_update (kg.RELATION_INVERSE); an unknown `type` is dropped there.
                "relations": {"type": "array", "items": {"type": "object", "properties": {
                    "type": {"type": "string"}, "other_person_name": {"type": "string"},
                    "other_identifier": {"type": "string"}, "date": {"type": "string"},
                    "confidence": {"type": "number"}}}}}}},
            "company_updates": {"type": "array", "items": {"type": "object", "properties": {
                "company_name": {"type": "string"},
                "news": {"type": "array", "items": {"type": "object", "properties": {
                    "text": {"type": "string"}, "date": {"type": "string"}}}},
                "context_updates": {"type": "array", "items": {"type": "string"}}}}},
        }},
    },
    "required": ["markdown", "actionItems", "extractedKnowledge"],
    # actionItems FIRST — the prompt's documented generation order (details before narrative).
    "propertyOrdering": ["actionItems", "markdown", "extractedKnowledge"],
}




def _freemail_domains() -> set:
    """research_attendees.FREEMAIL_DOMAINS — imported lazily (research_attendees imports THIS module
    at its top level, so a module-level import here would be circular) rather than mirrored as a
    second list. Best-effort: an import failure just means the colleague-domain skip stays on."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import research_attendees as _ra  # noqa: PLC0415
        return _ra.FREEMAIL_DOMAINS
    except Exception:
        return set()


def select_attendees_for_research(inputs: dict) -> list:
    """Deterministically pick the external attendees of upcoming meetings who warrant research.
    Mirrors the Mac backend: within RESEARCH_HORIZON_HOURS, an attendee needs research unless they
    are the user, share the user's email domain, or are already a known contact / in the graph.
    Returns [{name, email, meeting_title, meeting_start}], deduped by email, capped at the max."""
    google = _obj(inputs, "google")
    local = resolve_contact_names(_obj(inputs, "local"))
    events = _arr(google, "events")
    user_email = (_s(google.get("userEmail")) or configured_user_email()).lower()
    user_domain = user_email.split("@")[1] if "@" in user_email else ""
    # The same-domain skip means "colleagues don't need research" — that only holds for a CORPORATE
    # domain. For a freemail user (gmail.com etc.) sharing a domain proves nothing, and the skip
    # would silently exclude EVERY freemail attendee from research forever.
    if user_domain in _freemail_domains():
        user_domain = ""
    # Research-quality gate: a thin/stale graph profile doesn't count as "known" here, so the
    # attendee gets re-researched (port of the Mac's re-research-on-low-quality cache behavior).
    known_emails, known_names = _known_identities(local, research_quality=True)
    now = datetime.now(timezone.utc)

    picked, seen = [], set()
    for e in events:
        start = _s(e.get("start"))
        st = _parse_ts(start)
        if st is not None:
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            hours_away = (st - now).total_seconds() / 3600.0
            if hours_away < -1 or hours_away > RESEARCH_HORIZON_HOURS:
                continue  # past meeting or beyond the research horizon
        for a in _arr(e, "attendees"):
            email = _s(a.get("email")).lower().strip()
            if not email or email in seen:
                continue
            name = _s(a.get("displayName")) or (email.split("@")[0])
            if email == user_email:
                continue
            if user_domain and email.endswith("@" + user_domain):
                continue
            if email in known_emails or any(_names_match(name, kn) for kn in known_names):
                continue
            seen.add(email)
            picked.append({"name": name, "email": email,
                           "meeting_title": _s(e.get("summary")), "meeting_start": start})
            if len(picked) >= MAX_ATTENDEES_TO_RESEARCH:
                return picked
    return picked




def _is_first_run(inputs: dict, local: dict) -> bool:
    """The first brief a new user ever gets — the one they judge Sotto on. Driven by an explicit
    `first_run` flag (the setup skill sets it) OR auto-detected: no brief has been delivered yet.
    Auto-detection means even a plain cron first-brief gets the welcome. (We key on the delivered
    marker, NOT graph contents, so pre-warming the graph at setup doesn't suppress the welcome.)"""
    if (inputs or {}).get("first_run") is not None:
        return bool(inputs["first_run"])
    try:
        briefs = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "briefs")
        if any(f.endswith(".delivered") for f in os.listdir(briefs)):
            return False
    except OSError:
        pass  # no briefs dir yet → first run
    return True




def _email_truncation_note(google: dict) -> str:
    """Email-window honesty (roadmap Sprint 0 #5): when gather_google hit its --max cap, the inbox
    window was silently cut. Surface that as one honest parenthetical instead of letting the brief
    imply full coverage. Empty string when nothing was truncated."""
    n = (google or {}).get("emailsTruncatedAt") or (google or {}).get("emails_truncated_at")
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    return f"(inbox window truncated at {n} — more arrived)" if n > 0 else ""


def _coverage_line(local: dict, sa: dict, events, emails, truncation_note: str = "") -> str:
    """One honest line on what Sotto can see right now vs. what's still to connect — set on the first
    brief so a new user knows why a thin brief is thin (and what to link), without nagging daily."""
    seeing, missing = [], []
    if emails and events:
        seeing.append("your email and calendar")
    elif emails:
        seeing.append("your email")
    elif events:
        seeing.append("your calendar")
    else:
        missing.append("Gmail + Calendar")
    pairs = [("imessage", "iMessage"), ("whatsapp", "WhatsApp")]
    for sid, label in pairs:
        st = _s((sa or {}).get(sid))
        if _arr(local, sid):
            seeing.append(label)
        elif st and st != "available":
            missing.append(label)
    if _arr(local, "granola_meetings"):
        seeing.append("your Granola meeting notes")
    else:
        missing.append("Granola (optional, for meeting notes)")
    note = ""
    if seeing:
        note = "Right now I can see " + ", ".join(dict.fromkeys(seeing)) + "."
    if missing:
        note += (" " if note else "") + "Link " + ", ".join(dict.fromkeys(missing)) + " for the full picture."
    if truncation_note:
        note += (" " if note else "") + truncation_note
    return note.strip()




def _first_run_note(inputs: dict, local: dict, sa: dict, events, emails) -> str:
    if not _is_first_run(inputs, local):
        return ""
    coverage = _coverage_line(local, sa, events, emails, _email_truncation_note(_obj(inputs, "google")))
    return (
        "## FIRST BRIEF (one-time onboarding — overrides the no-intro rule, JUST for this brief)\n"
        "This is the user's very first Sotto brief. Open with ONE short, warm sentence introducing "
        "yourself as Sotto and what you do, then deliver the normal brief. After it, add ONE line on "
        "what they can ask next — e.g. \"reply to a message for you\", \"tell you about someone you're "
        "meeting\", or \"what you're waiting on\". Keep each to a single sentence; never repeat this on "
        "later briefs."
        + (f" Also include this coverage note once: \"{coverage}\"" if coverage else "")
        + "\n\n")




def _correlate_signals(local, emails, granola) -> dict:
    """Returns {signal_boosts, file_matches, granola_context, signal_scores} connecting browsing/files/
    meeting-notes to email senders. Empty lists when nothing correlates."""
    senders = {}                                  # addr -> display name
    by_domain: dict = {}                          # base domain -> [(addr, name)]
    for e in emails or []:
        addr = _sender_addr(e.get("from"))
        if not addr or addr in senders:
            continue
        name = _extract_sender_name(_s(e.get("from"))) or addr
        senders[addr] = name
        d = _base_domain(addr)
        if not _is_excluded_domain(d):
            by_domain.setdefault(d, []).append((addr, name))

    # 1) domain → email sender: researched their company (≥2 visits, filtered) and they emailed.
    researched = {}
    for h in _arr(local, "chrome_history") + _arr(local, "safari_history"):
        d = _base_domain(_s(h.get("domain")))
        if not _is_excluded_domain(d) and int(h.get("visit_count") or 0) >= 2:
            researched[d] = True
    boosts = [{"person": name, "email": addr, "domain": d}
              for d in researched for addr, name in by_domain.get(d, [])]

    # 2) file → email sender: downloaded from their domain (ONE-TO-ONE only — reject shared domains).
    file_matches = []
    for f in _arr(local, "recent_files"):
        src = _base_domain(_s(f.get("source_url")))
        people = by_domain.get(src, []) if not _is_excluded_domain(src) else []
        if len(people) == 1:
            _, name = people[0]
            file_matches.append({"filename": _s(f.get("filename")), "event": name,
                                 "status": _s(f.get("status")), "confidence": "high", "keywords": [src]})

    # 3) granola → email sender: met them recently, now they emailed (most-recent context per person).
    granola_context, seen = [], set()
    for g in sorted(granola or [], key=lambda m: _s(m.get("date")), reverse=True):
        for p in (g.get("attendee_emails") or []):
            pa = _s(p).lower()
            if pa in senders and pa not in seen:
                seen.add(pa)
                granola_context.append({"meeting_title": _s(g.get("title")), "last_meeting": _s(g.get("date")),
                                        "person": senders[pa], "summary": _s(g.get("ai_summary") or g.get("your_notes"))})

    # Per-person score = weighted sum of contributing signals (researched 2, file 2, met-recently 3).
    score: dict = {}
    for b in boosts:
        score[b["person"]] = score.get(b["person"], 0) + 2
    for f in file_matches:
        score[f["event"]] = score.get(f["event"], 0) + 2
    for g in granola_context:
        score[g["person"]] = score.get(g["person"], 0) + 3
    signal_scores = [{"event": n, "score": s, "signals": ["context"]}
                     for n, s in sorted(score.items(), key=lambda x: -x[1]) if s]

    return {"signal_boosts": boosts, "file_matches": file_matches,
            "granola_context": granola_context, "signal_scores": signal_scores}




def _detect_escalation(local: dict, emails: list, now: datetime | None = None) -> list:
    """Port of pipeline/generate.ts detectEscalation: a contact who reached out across 2+ distinct
    channels within 48h is escalating — the strongest priority signal in a Sotto brief. Local is the
    resolved local data (resolved_name/missed_calls); emails are the trimmed emails."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=48)
    contact_map: dict = {}
    display_names: dict = {}   # lowercased dedup key → display-cased name (first form seen)

    def add(name, channel, ts):
        name = _s(name).strip()
        if not name:
            return
        d = _parse_ts(ts)
        if d is None:
            return
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if d < cutoff:
            return
        key = name.lower().strip()
        display_names.setdefault(key, name)
        contact_map.setdefault(key, []).append({"channel": channel, "timestamp": _s(ts), "_dt": d})

    def latest_by_person(messages, name_of):
        by = {}
        for m in messages:
            if m.get("is_group_chat") or m.get("is_from_me"):
                continue
            name = _s(name_of(m))
            key = name.lower().strip()
            if not key:
                continue
            if key not in by or _s(m.get("timestamp")) > by[key][1]:
                by[key] = (name, _s(m.get("timestamp")))
        return by.values()

    for name, ts in latest_by_person(_arr(local, "imessage"), lambda m: m.get("resolved_name") or m.get("handle")):
        add(name, "iMessage", ts)
    for name, ts in latest_by_person(_arr(local, "whatsapp"), lambda m: m.get("resolved_name") or m.get("partner_name")):
        add(name, "WhatsApp", ts)
    for c in _arr(local, "missed_calls"):
        add(c.get("name"), "phone", c.get("timestamp"))
    email_by_person = {}
    for e in emails:
        # Contacts-reconciled name first — keys the email touch to the SAME person as their
        # iMessage/WhatsApp touches, so 2-channel escalation actually fires across channels.
        name = _s(e.get("resolvedName")) or _extract_sender_name(e.get("from"))
        if not name:
            continue
        ts = _s(e.get("date"))
        key = name.lower().strip()
        cur = email_by_person.get(key)
        d = _parse_ts(ts)
        if cur is None or (d is not None and (cur[2] is None or d > cur[2])):
            email_by_person[key] = (name, ts, d)
    for name, ts, _d in email_by_person.values():
        add(name, "email", ts)

    def fmt_day(dt):
        return f"{dt.strftime('%a')} {dt.hour % 12 or 12}{'pm' if dt.hour >= 12 else 'am'}"

    results = []
    for key, entries in contact_map.items():
        distinct = list({e["channel"] for e in entries})
        if len(distinct) < 2:
            continue
        entries.sort(key=lambda e: e["_dt"])
        seen, deduped = set(), []
        for e in entries:               # keep earliest per channel
            if e["channel"] not in seen:
                seen.add(e["channel"])
                deduped.append(e)
        narrative = " → ".join(f"{e['channel']} ({fmt_day(e['_dt'])})" for e in deduped)
        # display-cased name — escalation is the prompt's highest-trust section, and the lowercased
        # dedup key would render "sarah chen" verbatim in the brief.
        results.append({"name": display_names.get(key, key), "narrative": narrative,
                        "escalation_level": len(distinct),
                        "channels": [{"channel": e["channel"], "timestamp": e["timestamp"]} for e in deduped]})
    results.sort(key=lambda r: r["escalation_level"], reverse=True)
    return results[:10]




def explicit_prefs() -> dict:
    """The user's EXPLICIT preferences (mutes / tone) from preferences.json, written via the
    sotto-feedback skill. Honored deterministically here so "stop surfacing X" / "don't flag Bob" /
    "keep it terse" actually stick. Best-effort; always returns the full shape."""
    shape = {"mute_senders": [], "mute_people": [], "mute_sections": [], "tone_notes": []}
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import preferences as _p  # noqa: PLC0415
        ex = _p.load_explicit()
        return {k: ex.get(k, shape[k]) for k in shape}
    except Exception:
        return shape




def _sender_is_muted(addr: str, muted: list) -> bool:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import preferences as _p  # noqa: PLC0415
        return _p.sender_is_muted(addr, muted)
    except Exception:
        return False




def _name_muted(name: str, muted: list) -> bool:
    n = _s(name).strip().lower()
    return bool(n) and any(n == _s(m).strip().lower() for m in (muted or []))




def _format_user_preferences(ex: dict) -> str:
    """Prompt block telling the model to honor the user's stated preferences. Senders are dropped
    deterministically before this; people are also filtered, but we restate them so the model never
    re-introduces a muted person via another source."""
    people = ex.get("mute_people") or []
    sections = ex.get("mute_sections") or []
    tone = ex.get("tone_notes") or []
    if not (people or sections or tone):
        return ""
    lines = ["## User Preferences (HONOR THESE)"]
    if people:
        lines.append(f"Do NOT surface or flag these people anywhere in the brief: {', '.join(people)}.")
    if sections:
        lines.append(f"Omit these sections entirely: {', '.join(sections)}.")
    if tone:
        lines.append("Tone/format the user has asked for: " + "; ".join(tone) + ".")
    return "\n".join(lines)




# ---------------------------------------------------------------------------
# Cross-source index (simplified port of buildCrossSourceIndex)
# ---------------------------------------------------------------------------

def _build_cross_source_index(im_needs, im_handled, wa_needs, wa_handled, emails, events, missed) -> str:
    people = {}

    def get(name):
        key = name.lower().strip()
        return people.setdefault(key, {"name": name, "sources": [], "details": []})

    for t in im_needs:
        if t.get("name") and not t.get("is_group_chat"):
            e = get(t["name"]); e["sources"].append("iMessage"); e["details"].append("iMessage: waiting for response")
    for t in im_handled:
        if t.get("name") and not t.get("is_group_chat"):
            e = get(t["name"])
            if "iMessage" not in e["sources"]:
                e["sources"].append("iMessage"); e["details"].append("iMessage: already responded")
    for t in wa_needs:
        if t.get("name") and not t.get("is_group_chat"):
            e = get(t["name"]); e["sources"].append("WhatsApp"); e["details"].append("WhatsApp: waiting for response")
    for t in wa_handled:
        if t.get("name") and not t.get("is_group_chat"):
            e = get(t["name"])
            if "WhatsApp" not in e["sources"]:
                e["sources"].append("WhatsApp"); e["details"].append("WhatsApp: already responded")
    for em in emails:
        from_ = _s(em.get("from"))
        # Contacts-reconciled name first so the email row lands under the same person as their
        # iMessage/WhatsApp rows (otherwise one human splits into two index entries).
        name = _s(em.get("resolvedName")) or re.sub(r"<.*?>", "", from_).strip().strip('"') or from_
        if not name:
            continue
        e = get(name)
        if "Email" not in e["sources"]:
            e["sources"].append("Email"); e["details"].append(f"Email: \"{_s(em.get('subject'))[:150]}\"")
    for ev in events:
        for a in (ev.get("attendees") or []):
            name = a.get("displayName") or (_s(a.get("email")).split("@")[0]) or ""
            if not name:
                continue
            e = get(name)
            if "Calendar" not in e["sources"]:
                e["sources"].append("Calendar"); e["details"].append(f"Calendar: {ev.get('summary') or 'event'}")
    for c in missed:
        if c.get("name"):
            e = get(c["name"]); e["sources"].append("MissedCall"); e["details"].append("Missed call")

    lines = []
    for e in people.values():
        uniq = list(dict.fromkeys(e["sources"]))
        if len(uniq) < 2:
            continue
        lines.append(f"- {e['name']}: {', '.join(uniq)}\n  " + "\n  ".join(e["details"]))
    if not lines:
        return "No cross-source connections detected."
    return "Cross-source (no resolution detected):\n" + "\n".join(lines)




# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------

# ── No-Bridge fallback: cache the last good read_local so an asleep Mac degrades to yesterday's ──
# local data instead of a Google-only brief. The snapshot is the raw read_local payload + a stamp.
_LOCAL_SOURCE_KEYS = ("imessage", "whatsapp", "missed_calls", "calls", "whatsapp_calls", "reminders",
                      "recent_files", "apple_notes", "contacts", "deferred_unread_imessage",
                      "deferred_unread_whatsapp")




def _local_has_data(local: dict) -> bool:
    return any(_arr(local, k) for k in _LOCAL_SOURCE_KEYS)




def _snapshot_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "knowledge", "last_local_snapshot.json")




def _snapshot_age_hours(captured_at: str):
    """Hours since the snapshot was captured, or None if the stamp can't be parsed. Handles the ISO
    `generated_at` form and the naive 'YYYY-MM-DD HH:MM:SS' fallback (treated as UTC)."""
    ts = _parse_ts(captured_at)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0




def _save_local_snapshot(local: dict):
    try:
        path = _snapshot_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stamp = local.get("generated_at") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        local = dict(local)
        # Contacts carry-forward: contacts (the identity/name-resolution layer) change slowly and a
        # pull can come back thin. If THIS pull has no contacts but the prior snapshot did, keep the
        # old ones so a contacts-less refresh doesn't wipe name resolution (the "raw phone numbers in
        # the brief" symptom). Everything else is plain last-write-wins.
        if not _arr(local, "contacts"):
            try:
                with open(path, encoding="utf-8") as f:
                    prev = (json.load(f).get("local") or {})
                if _arr(prev, "contacts"):
                    local["contacts"] = prev["contacts"]
            except Exception:
                pass
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"captured_at": stamp, "local": local}, f)
    except Exception:
        pass




def _local_fallback(local: dict) -> dict:
    """Bridge unreachable (empty local): fall back to the last cached snapshot, tagged stale so the
    brief says 'local context from Mac last seen …' rather than silently dropping to Google-only.
    Snapshots older than LOCAL_SNAPSHOT_TTL_HOURS are dropped — better no local than stale loops."""
    try:
        path = _snapshot_path()
        if not os.path.exists(path):
            return local
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
        cached = snap.get("local") or {}
        if not _local_has_data(cached):
            return local
        age = _snapshot_age_hours(snap.get("captured_at"))
        if age is not None and age > LOCAL_SNAPSHOT_TTL_HOURS:
            return local  # expired — don't replay day(s)-old messages as if they're current
        cached = dict(cached)
        cached["_local_stale_since"] = snap.get("captured_at")
        # Preserve any availability/knowledge the caller did pass alongside the empty local.
        for k, v in (local or {}).items():
            if v and k not in cached:
                cached[k] = v
        return cached
    except Exception:
        return local




def _normalize_local(inputs: dict) -> dict:
    """Fold the brief inputs the SKILL passes at the TOP LEVEL into `local`, where the source
    renderers actually look. Keeps the documented skill contract — {type, google, granola, local,
    prior_knowledge, …} — working even though every renderer reads from `local`. Without this,
    Granola notes and the people/company knowledge graph are silently dropped from the brief.

    - prior_knowledge (knowledge_query.py output) → local.{person_knowledge, company_knowledge,
      contact_index, journal_context}.
    - granola (Hermes MCP) → local.granola_meetings (accepts {meetings:[…]} or a bare list).
    - the Bridge's source_status → the consumer's _source_availability (ok→available, else→unavailable),
      so the prompt still warns when a local source is missing and the model won't invent actions for it.
    Values already present in `local` win (an explicit override is never clobbered)."""
    local = dict(_obj(inputs, "local"))

    pk = _obj(inputs, "prior_knowledge")
    named = ("person_knowledge", "company_knowledge", "contact_index", "journal_context")
    if pk and not any(k in pk for k in named):
        # Bare knowledge_query.py output ({slug: packed_string}) → treat as person_knowledge.
        pk = {"person_knowledge": pk}
    for k in named:
        if pk.get(k) and not local.get(k):
            local[k] = pk[k]

    if not local.get("granola_meetings"):
        g = inputs.get("granola")
        if isinstance(g, dict) and isinstance(g.get("meetings"), list):
            local["granola_meetings"] = g["meetings"]
        elif isinstance(g, list):
            local["granola_meetings"] = g

    if not local.get("_source_availability") and isinstance(local.get("source_status"), dict):
        local["_source_availability"] = {
            sid: ("available" if _s(st) == "ok" else "unavailable")
            for sid, st in local["source_status"].items()
        }

    # A granola input carrying gather warnings (gather_granola.py embeds them on any failure) means
    # the meeting list is broken/empty, not a genuinely quiet week — mark the source unavailable so
    # the Data Source Availability section says so instead of the brief silently losing meeting notes.
    g = inputs.get("granola")
    if isinstance(g, dict) and g.get("warnings"):
        avail = local.setdefault("_source_availability", {})
        if isinstance(avail, dict):
            avail.setdefault("granola", "unavailable")

    # Surface the weekly relationship pulse (relationship_pulse.py writes it to the volume) so the
    # daily brief's attention-queue / relationship-insights sections aren't inert.
    if not local.get("attention_queue") and not local.get("relationship_insights"):
        try:
            state_path = os.path.join(os.environ.get("SOTTO_DATA", "/data"),
                                      "knowledge", "relationship_state.json")
            if os.path.exists(state_path):
                with open(state_path, encoding="utf-8") as f:
                    state = json.load(f)
                if state.get("attention_queue"):
                    local["attention_queue"] = state["attention_queue"]
                if state.get("relationship_insights"):
                    local["relationship_insights"] = state["relationship_insights"]
        except Exception:
            pass

    # Surface the continuity ledger as the action_ledger (same pattern as the pulse merge above):
    # nothing else populates it, so without this the Open Commitments / Evening Accountability /
    # TRACKED OPEN LOOPS sections render empty every day. Read-only via ledger_io — resolution
    # still happens only in continuity_resolve.
    # Meeting-prep/meeting-info entries are calendar shadows, not asks: the docket is their surface,
    # so the brief drops them exactly like /api/loops and the overview do. The type spelling is
    # normalized by ledger_io — the same normalizer the resolver and the read views use, so a
    # `Meeting-Prep ` is dropped here exactly as it is there.
    if not local.get("action_ledger"):
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import ledger_io  # noqa: PLC0415
            entries = [e for e in ledger_io.load_active()
                       if ledger_io.normalize_action_type(e.get("action_type"))
                       not in MEETING_LEDGER_ACTION_TYPES]
            if entries:
                local["action_ledger"] = entries
        except Exception:
            pass
    return local




# ── Entity-dedup lite, the brief-side halves (Editor Step 2 item 6) ───────────────────────────────
# Both ride the {{knowledge_section}} placeholder as extra prompt CONTEXT — deliberately, because
# context cannot break a brief: no new template placeholder to keep in sync, no post-hoc edit of the
# validated markdown, and a failure to read either file degrades to saying nothing.

KNOWN_COMPANIES_MAX = 40        # most-recently-touched company files fed back to the model
MERGE_SUGGESTIONS_SHOWN = 3     # the brief mentions at most a couple; the dashboard has the list


def _knowledge_dir() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "knowledge")


def _known_companies() -> list:
    """[(display_name, slug)] for the company files the graph already has, most recent first.
    Reads the frontmatter `aliases`/`normalized` that knowledge_update writes."""
    import glob as _glob
    out = []
    try:
        paths = _glob.glob(os.path.join(_knowledge_dir(), "companies", "*.md"))
        paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    except OSError:
        return out
    import yaml as _y
    for path in paths[:KNOWN_COMPANIES_MAX]:
        slug = os.path.splitext(os.path.basename(path))[0]
        name = ""
        try:
            with open(path, encoding="utf-8") as f:
                yaml_str, _body = _split_frontmatter(f.read())
            fm = (_y.safe_load(yaml_str) if yaml_str else {}) or {}
            aliases = [a for a in (fm.get("aliases") or []) if _s(a).strip()]
            name = _s(aliases[0]).strip() if aliases else ""
        except Exception:  # noqa: BLE001 — an unreadable company file just contributes its slug
            pass
        out.append((name or slug.replace("-", " ").title(), slug))
    return out


def _split_frontmatter(content: str):
    """kg.split_frontmatter_body without importing the whole knowledge lib into the brief path."""
    if not content.startswith("---\n"):
        return (None, content)
    after = content[4:]
    idx = after.find("\n---\n")
    return (after[:idx], after[idx + 5:]) if idx != -1 else (None, content)


def _format_known_companies() -> str:
    """The prompt-side half of company dedup: PREVENTION. The model free-forms "YC" one day and
    "Y Combinator" the next, and each spelling mints its own file — so show it the names the graph
    already uses and ask it to reuse them verbatim."""
    companies = _known_companies()
    if not companies:
        return ""
    lines = "\n".join(f"- {name}" for name, _slug in companies)
    return ("### Companies Already in the Knowledge Graph\n"
            "These are the company names Sotto's memory already uses. When one of them appears in "
            "today's data, write it EXACTLY this way — in `extractedKnowledge.company_updates[].company_name` "
            "and in the brief's prose. Do not invent a variant or an abbreviation of a name on this "
            "list (\"YC\" for a company stored as \"Y Combinator\"): a variant creates a second, "
            "half-empty memory of the same company. A company genuinely NOT on this list is new — "
            "name it in full, the way it names itself.\n"
            f"{lines}\n")


def _merge_suggestions() -> list:
    """knowledge/merge_suggestions.json (written by knowledge_update's Learn step; see its module
    docstring). Missing/corrupt → no suggestions, silently."""
    try:
        with open(os.path.join(_knowledge_dir(), "merge_suggestions.json"), encoding="utf-8") as f:
            data = json.load(f) or {}
        items = data.get("suggestions")
        return [s for s in items if isinstance(s, dict)] if isinstance(items, list) else []
    except Exception:  # noqa: BLE001
        return []


def _format_merge_suggestions() -> str:
    """The user-facing half of person dedup: name-similarity pairs are never merged automatically,
    so SOMETHING has to ask. One optional line, offered as context — never a required section."""
    items = _merge_suggestions()[:MERGE_SUGGESTIONS_SHOWN]
    if not items:
        return ""
    pairs = "\n".join(
        f"- {_s(s.get('from_name')) or _s(s.get('from'))} and "
        f"{_s(s.get('into_name')) or _s(s.get('into'))} ({_s(s.get('reason'))})"
        for s in items)
    return ("### Memory Maintenance — possible duplicate people (unconfirmed)\n"
            "Sotto's memory holds these pairs of person files that MIGHT be the same human. They "
            "were flagged by name similarity ONLY and were deliberately not merged — merging two "
            "different people is worse than holding two files for one. You may mention this in AT "
            "MOST ONE short closing line, and only when the brief has room: name the pair and offer "
            "to merge (\"Ben and Ben Butler look like the same person — say merge them and I'll "
            "fix it\"). Never lead with it, never spend an entry on it, and drop it entirely on a "
            "busy day. It is housekeeping, not news.\n"
            f"{pairs}\n")


def _brief_now(inputs: dict):
    """The instant this brief reasons about: `inputs["now"]` when a caller pins it (the golden
    replay reads a day six weeks back), else None — meaning the wall clock. One optional key so
    ledger ages in the prompt belong to the day being composed."""
    return _parse_ts(_s(inputs.get("now"))) if inputs.get("now") else None


def _brief_tz(inputs: dict) -> str:
    """The user's zone for THIS brief, resolved once: explicit userTimezone → SOTTO_TIMEZONE env
    (the authoritative IANA zone set on Railway, DST-correct) → an offset sniffed from a calendar
    event. The env fallback is what keeps headless cron briefs on the user's local day instead of
    UTC (the off-by-one date bug)."""
    google = _obj(inputs, "google")
    return (_s(google.get("userTimezone")) or configured_tz()
            or _user_tz_offset(_arr(google, "events")))


def _brief_day(tz: str, now=None) -> str:
    """The user-local date this brief reasons about — the wall clock normally, the INJECTED instant
    when a caller pinned one. Same discipline as _action_age: a corpus replay of a day six weeks
    back must read that day's record, not today's."""
    if now is None:
        return _user_local_date(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    tzinfo = _resolve_tz(tz)
    local = (now.astimezone(tzinfo) if tzinfo is not None
             else now + timedelta(minutes=_tz_offset_minutes(tz)))
    return local.strftime("%Y-%m-%d")


def build_prompt(template: str, inputs: dict) -> str:
    brief_type = _s(inputs.get("type")) or "morning"
    google = _obj(inputs, "google")
    events = _arr(google, "events")
    emails_raw = _arr(google, "emails")

    local = resolve_contact_names(_normalize_local(inputs))
    sa = _obj(local, "_source_availability")

    # Explicit user preferences (the sotto-feedback channel): suppress muted people from the
    # relationship/attention surfaces deterministically, so "don't flag Bob" actually sticks. Muted
    # senders are dropped from email below; muted sections + tone are passed to the prompt.
    prefs = explicit_prefs()
    if prefs["mute_people"]:
        for key in ("attention_queue", "relationship_insights"):
            if isinstance(local.get(key), list):
                local[key] = [q for q in local[key]
                              if not _name_muted(q.get("display_name"), prefs["mute_people"])]

    tz = _brief_tz(inputs)
    user_today = _user_local_date(tz)
    time_frame = _time_frame(tz)

    # Message threads. Drop threads from unknown senders (raw phone numbers / shortcodes / OTP
    # spam) before they reach the FLEX prompt — same as the Mac pipeline, which only keeps
    # is_known_contact threads. Known = Contacts resolved a real name, OR the sender matches a
    # calendar attendee / graph person / saved contact.
    known_emails, known_names = _known_identities(local)
    for e in events:
        for a in _arr(e, "attendees"):
            em = _s(a.get("email")).lower().strip()
            if em:
                known_emails.add(em)
            nm = _s(a.get("displayName")).strip()
            if nm:
                known_names.append(nm)
    # Canonical ids the Mac isKnownPerson rescue trusts: calendar attendees we've actually met,
    # and anyone already in the attention queue / relationship insights / action ledger.
    known_canonical_ids = set()
    for item in _arr(local, "cached_calendar_attendees"):
        if (item.get("meeting_count") or 0) > 0 and _s(item.get("canonical_id")):
            known_canonical_ids.add(_s(item.get("canonical_id")))
        em = _s(item.get("email")).lower().strip()
        if em and (item.get("meeting_count") or 0) > 0:
            known_emails.add(em)
    for key in ("attention_queue", "relationship_insights", "action_ledger"):
        for item in _arr(local, key):
            if _s(item.get("canonical_id")):
                known_canonical_ids.add(_s(item.get("canonical_id")))

    def _known_threads(threads):
        return [t for t in threads if _thread_is_known_person(t, known_emails, known_names, known_canonical_ids)]

    contact_lookup = build_contact_lookup(_arr(local, "contacts"))
    im_threads = _known_threads(_group_messages_into_threads(_arr(local, "imessage"), "imessage", contact_lookup))
    wa_threads = _known_threads(_group_messages_into_threads(_arr(local, "whatsapp"), "whatsapp", contact_lookup))
    im_needs = [t for t in im_threads if _thread_needs_response(t)]
    im_handled = [t for t in im_threads if not _thread_needs_response(t)]
    wa_needs = [t for t in wa_threads if _thread_needs_response(t)]
    wa_handled = [t for t in wa_threads if not _thread_needs_response(t)]

    # Trim with the contact lookup so senders the user has in Contacts surface under the SAME name
    # they carry on iMessage/WhatsApp/calendar (the cross-channel identity reconciliation).
    trimmed_emails = [_trim_email(e, contact_lookup) for e in emails_raw]
    if prefs["mute_senders"]:
        trimmed_emails = [e for e in trimmed_emails
                          if not _sender_is_muted(_sender_addr(e.get("from")), prefs["mute_senders"])]
    missed = _arr(local, "missed_calls")

    # Cross-channel escalation: computed HERE, always (port of detectEscalation). There is no
    # caller-supplied override — the old `inputs["signals"]` escape hatch had no producer anywhere in
    # the pack, and its name collided with continuity's differently-shaped "signals" key.
    escalation = _detect_escalation(local, trimmed_emails)
    # Context-signal correlation — compute it HERE (deterministic, always runs) rather than relying on
    # a separate step the agent skips. This is what connects "you researched their company / downloaded
    # their deck / met them last week" to the actual email senders in the brief.
    corr = _correlate_signals(local, trimmed_emails, _arr(local, "granola_meetings"))

    cross_source = _build_cross_source_index(im_needs, im_handled, wa_needs, wa_handled,
                                             trimmed_emails, events, missed)

    def opt(text):
        return (text + "\n") if text else ""

    # Email-window honesty: the gather hit its cap, so the Gmail section below is NOT the full window.
    trunc = _email_truncation_note(google)
    trunc_block = (f"NOTE — email coverage: {trunc} The Gmail section below is not the complete "
                   "inbox window; do not present it as exhaustive.\n") if trunc else ""

    # Evening followup merge (roadmap Sprint 0 #4): compose() pre-computes the followup extraction and
    # passes the rendered block in `_followup_context`. It renders through the {{followup_context}}
    # placeholder when the template has one; otherwise it rides the existing evening path by being
    # appended to the reconciliation/evening-accountability block (so the merge works either way,
    # never twice).
    followup_context = _s(inputs.get("_followup_context")) if brief_type == "evening" else ""
    reconciliation = opt(_format_reconciliation(local, brief_type))
    if followup_context and "{{followup_context}}" not in template:
        reconciliation += opt(followup_context)

    # Today's delivered nudges (evening only). Same placeholder-or-reconciliation fallback as the
    # followup block, so an older template still gets it exactly once.
    already_nudged = _already_nudged_block(tz)
    if already_nudged and "{{already_nudged}}" not in template:
        reconciliation += opt(already_nudged)

    fields = {
        "brief_type": brief_type,
        "user_today": user_today,
        "time_frame": time_frame,
        "followup_context": opt(followup_context),
        "already_nudged": opt(already_nudged),
        "source_availability": _stale_local_note(local) + _format_source_availability(sa) + trunc_block,
        "first_run_note": _first_run_note(inputs, local, sa, events, trimmed_emails),
        "user_preferences": opt(_format_user_preferences(prefs)),
        "signal_scores": _format_signal_scores(corr["signal_scores"]),
        "granola_context": _format_granola_context(corr["granola_context"]),
        "file_matches": _format_file_matches(corr["file_matches"]),
        "domain_research_matches": _format_domain_research(corr["signal_boosts"], local),
        "top_browsing_domains": _format_top_browsing_domains(local),
        "recent_search_queries": opt(_format_search_queries(local)),
        "screen_time": _format_screen_time(local),
        "recent_files": _format_recent_files(local),
        "apple_notes": opt(_format_apple_notes(local)),
        "granola_meetings": _format_granola_meetings(local),
        "meeting_archive_context": _format_meeting_archive(local),
        "stale_threads": _format_stale_threads(local),
        "deferred_unread": _format_deferred_unread(local),
        "past_commitments": _format_past_commitments(local),
        "cross_source_index": cross_source,
        "escalation_signals": opt(_format_escalation_signals(escalation)),
        "contact_notes": opt(_format_contact_notes(local)),
        # The graph's own state rides in beside the packed knowledge: the company names it already
        # uses (so the model stops free-forming variants) and any unconfirmed duplicate people.
        "knowledge_section": (opt(_format_knowledge_section(local))
                              + opt(_format_known_companies())
                              + opt(_format_merge_suggestions())),
        "action_ledger": opt(_format_action_ledger(local, _brief_now(inputs))),
        "attention_queue": opt(_format_attention_queue(local)),
        "relationship_insights": opt(_format_relationship_insights(local)),
        "reconciliation": reconciliation,
        "imessage_needs_response": _format_threads_as_text(im_needs, "imessage", sa.get("imessage")),
        "whatsapp_needs_response": _format_threads_as_text(wa_needs, "whatsapp", sa.get("whatsapp")),
        "imessage_handled": _format_threads_as_text(im_handled, "imessage", sa.get("imessage")),
        "whatsapp_handled": _format_threads_as_text(wa_handled, "whatsapp", sa.get("whatsapp")),
        "gmail": _format_emails(trimmed_emails),
        "attendee_research": opt(_format_attendee_research(inputs)),
        "calendar": _format_calendar(events, contact_lookup),
        "reminders": _format_reminders(_arr(local, "reminders"), sa.get("reminders")),
        "birthdays": opt(_format_birthdays(local)),
        "missed_calls": _format_missed_calls(missed, sa.get("calls")),
        "recent_calls": _format_recent_calls(_arr(local, "recent_calls")),
    }

    def repl(m):
        return fields.get(m.group(1), m.group(0))

    return re.sub(r"\{\{(\w+)\}\}", repl, template)




def _invoke_llm(llm, prompt: str, inputs: dict, system: str | None = None, schema: dict | None = None) -> str:
    """Call `llm` with the system/schema kwargs when it can take them, else the legacy 2-arg form.
    Keeps every existing llm stub (`def fake_llm(prompt, inputs)`) working while the real
    call_gemini gets the true system/user split + responseSchema."""
    if system or schema is not None:
        import inspect
        try:
            params = inspect.signature(llm).parameters
            takes_kwargs = (any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
                            or ("system" in params and "schema" in params))
        except (TypeError, ValueError):
            takes_kwargs = False
        if takes_kwargs:
            return llm(prompt, inputs, system=system, schema=schema)
    return llm(prompt, inputs)




def _normalize_output(parsed: dict) -> dict:
    """Map the FLEX field names (markdown/actionItems/extractedKnowledge) onto the skill's
    normalized brief contract, accepting either naming so native and script paths agree."""
    out = dict(parsed) if isinstance(parsed, dict) else {}
    # brief_markdown <- markdown
    if "brief_markdown" not in out and "markdown" in out:
        out["brief_markdown"] = out.get("markdown")
    # actions <- actionItems
    if "actions" not in out and "actionItems" in out:
        out["actions"] = out.get("actionItems")
    # extracted_knowledge <- extractedKnowledge
    if "extracted_knowledge" not in out and "extractedKnowledge" in out:
        out["extracted_knowledge"] = out.get("extractedKnowledge")

    out.setdefault("brief_markdown", "")
    out.setdefault("actions", [])
    out.setdefault("meetings_needing_prep", [])
    ek = out.setdefault("extracted_knowledge", {})
    if not isinstance(ek, dict):
        ek = {}
        out["extracted_knowledge"] = ek
    ek.setdefault("person_updates", [])
    ek.setdefault("company_updates", [])
    return out




# ---------------------------------------------------------------------------
# Brief critic — second pass (port of api/src/services/brief-critic.ts +
# pipeline/generate.ts applyCriticPass). The Mac ran the draft brief through a
# critic LLM, then let Claude polish integrate the patches. The cloud has no
# polish stage, so here the critic's actionable patches drive a REVISE pass that
# rewrites the brief to fix them — the only automated quality gate the port has.
#
# SOTTO_CRITIC gates it: "auto" (default) skips the critic+revise pass (2 extra
# sequential 100K+-char Gemini calls) on a small/low-risk brief; "always" =
# every brief (the old behavior); "off" = never. Deterministic + logged.
# ---------------------------------------------------------------------------

# "auto" skips ONLY when BOTH hold: the rendered source payload (prompt minus the fixed template —
# i.e. the actual emails/messages/events fed in) is under this, AND few actions were extracted.
# A full real brief runs 100K+ payload chars; a quiet-day brief with a handful of items has little
# for a critic to catch, and the two extra calls cost more latency than they buy quality.
CRITIC_AUTO_MIN_PAYLOAD_CHARS = 15000


CRITIC_AUTO_MIN_ACTIONS = 5




def _critic_mode() -> str:
    m = (os.environ.get("SOTTO_CRITIC") or "auto").strip().lower()
    return m if m in ("auto", "always", "off") else "auto"




def _critic_decision(mode: str, payload_chars: int, n_actions: int):
    """(run, reason) — deterministic so it's testable and the brief log explains every skip."""
    if mode == "off":
        return False, "SOTTO_CRITIC=off"
    if mode == "always":
        return True, "SOTTO_CRITIC=always"
    if payload_chars < CRITIC_AUTO_MIN_PAYLOAD_CHARS and n_actions <= CRITIC_AUTO_MIN_ACTIONS:
        return False, (f"auto: small brief — payload {payload_chars} < {CRITIC_AUTO_MIN_PAYLOAD_CHARS} chars "
                       f"and {n_actions} actions ≤ {CRITIC_AUTO_MIN_ACTIONS}")
    return True, f"auto: payload {payload_chars} chars, {n_actions} actions"



# The triage policy the brief was WRITTEN under (extraction-prompt.md's Triage Discipline + Priority
# Levels). The critic judges against THIS bar — without it, the last model to touch every brief
# re-inflated what the extraction deliberately pruned (the additive-critic bug).
#
# ONE FILE OWNS THE BAR: the sections are EXTRACTED from the loaded prompt, not re-typed here. The
# hand-copied version drifted (it had lost "At most 5 items — fewer is better" and grown a line the
# prompt never contained), so the critic was judging against a policy the brief was never written
# under. The constant below survives only as the fail-loud fallback for an unparseable template.
_CRITIC_POLICY_SECTIONS = ("## Triage Discipline", "## Priority Levels")

_CRITIC_POLICY_HEADER = ("## Triage Policy (the SAME policy the brief was written under — judge "
                         "against this bar, NOT against completeness)\n\n")

_CRITIC_TRIAGE_POLICY_FALLBACK = """Triage Discipline — before including each entry, the brief asks: "Would a great chief of staff interrupt for this?"
- YES: Real stakes, real deadline, real relationship at risk, or a real opportunity window closing
- MAYBE: Worth mentioning but not interrupting for — put it lower or fold it into another entry
- NO: Social threads with no ask, FYI messages that don't change today's decisions, low-stakes scheduling, casual banter that's wrapped up → Already Handled at most, or skip entirely
- NEVER: System-generated emails where no human is personally waiting for a response. These are not communication — they are system output. Skip entirely.
A brief with 5 excellent entries beats one with 12 mediocre ones.

Priority Levels:
- Needs Attention Now: stakes are real AND timing matters. Must meet AT LEAST ONE:
  - Explicit deadline (today, overdue, promised by date)
  - Multi-channel escalation (same person, same topic, 2+ channels — they're clearly waiting)
  - Missed calls (someone tried to reach you live)
  - Time-sensitive decisions (offers expiring, invitations with deadlines)
  - Commitments you made that are due
  Items that are just "waiting for a response" without urgency belong in Should Handle Today or are omitted. A bare unread/unanswered message is NOT enough on its own.
- Should Handle Today: genuinely worth acting on today — a clear next step and a reason to do it today. The test: a real human wrote something that expects a real human response. Automated emails, system notifications, and informational digests never qualify. If there is no clear ask, it is omitted unless it is a true stale loop (3+ days), cross-channel escalation, or a relationship the user explicitly keeps warm."""


def _extract_prompt_sections(template: str, headings) -> str:
    """The named `## ` sections of the loaded template, VERBATIM and in order, joined by a blank line.
    A section runs from its heading line to the next `## ` heading (or EOF). Returns "" when ANY
    heading is missing or empty — a partial policy IS a drifted policy, so the caller falls back
    whole rather than shipping half a bar."""
    out = []
    for h in headings:
        m = re.search(r"(?m)^" + re.escape(h) + r"[ \t]*$", template)
        if m is None:
            return ""
        rest = template[m.end():]
        nxt = re.search(r"(?m)^##[ \t]", rest)
        body = (rest[:nxt.start()] if nxt else rest).strip()
        if not body:
            return ""
        out.append(f"{h}\n{body}")
    return "\n\n".join(out)


def _critic_triage_policy() -> str:
    """The critic's bar, read out of extraction-prompt.md at load. Any parse/read failure falls back
    to the embedded copy and says so loudly — a critic with no bar re-inflates every brief."""
    try:
        extracted = _extract_prompt_sections(_load_prompt(), _CRITIC_POLICY_SECTIONS)
    except Exception as e:  # noqa: BLE001
        extracted = ""
        _diag(f"[compose_brief] critic policy: prompt unreadable ({type(e).__name__}) — using the "
              "embedded fallback (it may have drifted from extraction-prompt.md)")
    if extracted:
        return _CRITIC_POLICY_HEADER + extracted
    _diag("[compose_brief] critic policy: could not extract "
          + " + ".join(_CRITIC_POLICY_SECTIONS)
          + " from extraction-prompt.md — using the embedded fallback (it may have drifted)")
    return _CRITIC_POLICY_HEADER + _CRITIC_TRIAGE_POLICY_FALLBACK


# Resolved once per process (the prompt is a repo file, and CRITIC_SYSTEM below is a constant).
_CRITIC_TRIAGE_POLICY = _critic_triage_policy()


CRITIC_SYSTEM = """You are a brief quality critic. Compare a generated communication brief against the raw data manifest and identify errors, omissions, and misattributions. Be strict but fair.

""" + _CRITIC_TRIAGE_POLICY + """

A brief that omits a low-stakes thread is CORRECT, not incomplete. Never patch for coverage. If your only complaint is that a thread is unmentioned and it does not meet the bar, that is not an issue.

You will receive: (1) a DATA MANIFEST — a compact summary of all raw data available to generate the brief; (2) the GENERATED BRIEF markdown; (3) the ACTION ITEMS extracted alongside it.

Check for these issues:
- MISSED THREADS: threads that meet the Needs Attention Now bar (explicit deadline / multi-channel escalation / missed call / expiring decision / due commitment) AND are absent from the brief. Nothing below that bar counts as missed — omission of low-stakes, no-ask, newsletter, automated, or marketing threads is the triage policy working, not an error.
- ATTRIBUTION ERRORS: Names/contacts mismatched between manifest and brief (wrong person credited, name misspelled differently than source).
- IDENTITY ERRORS: Two different identifiers/senders presented as the same person without the manifest linking them; one person split into duplicate entries; a name in the brief that appears NOWHERE in the manifest (invented or "expanded" name); a group-chat statement attributed to a specific member the data doesn't name.
- FABRICATED URGENCY: Deadlines, "waiting N days", call counts, or escalation claims with no supporting evidence in the manifest.
- PRIORITY ORDERING: High-signal items (missed calls, multi-channel contacts, urgent/deadline emails) buried below low-signal ones.
- ALREADY HANDLED: Items the user clearly already acted on (last_from_me, replied threads) that belong in "Already Handled" but aren't, or items wrongly marked handled.
- PROACTIVE ACTIONS: "follow_up_stale"/"waiting_on" actions are valid if they correspond to stale_threads or past_commitments in the manifest. Do NOT flag these as hallucinations.
- ACTION COVERAGE: Every bold **Name** in Needs Attention Now / Should Handle Today MUST have a matching action item. Flag any name in the narrative with no matching action.
- SYNTHESIS: Did the brief weave available Granola/file/browsing signals into the relevant person's entry, rather than merely listing communications? Flag a person whose entry ignores a clearly-relevant cross-channel signal in the manifest.

Return JSON: {"patches":[{"type":"add_item|fix_attribution|reorder|mark_handled|remove_item","target":"optional","detail":"...","severity":"critical|moderate|minor"}],"score":0-100,"summary":"one line"}

If the brief is good, return an empty patches array and a high score. Do NOT invent issues."""




def build_data_manifest(inputs: dict) -> dict:
    """Port of brief-critic.ts buildDataManifest — a compact summary of the raw data the brief had."""
    google = _obj(inputs, "google")
    local = resolve_contact_names(_normalize_local(inputs))
    lookup = build_contact_lookup(_arr(local, "contacts"))
    emails = [_trim_email(e, lookup) for e in _arr(google, "emails")]
    events = _arr(google, "events")

    seen, threads = set(), []
    for e in emails:
        tid = _s(e.get("threadId"))
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        from_ = (f"{e['resolvedName']} <{e.get('senderEmail', '')}>" if e.get("resolvedName")
                 else e.get("from"))  # same reconciled name the brief prompt saw
        threads.append({"subject": e.get("subject") or "(no subject)", "from": from_,
                        "thread_id": tid or None, "snippet": (_s(e.get("body"))[:120]) or None})

    def contacts(arr, name_key):
        out = []
        for m in arr:
            if m.get("is_group_chat") or m.get("is_from_me"):
                continue
            nm = _s(m.get("resolved_name")) or _s(m.get(name_key))
            if nm and nm not in out:
                out.append(nm)
        return out

    missed = [c.get("name") for c in _arr(local, "missed_calls") if c.get("name")]
    return {
        "email_count": len(emails),
        "email_threads": threads[:50],
        "imessage_contacts": contacts(_arr(local, "imessage"), "handle"),
        "whatsapp_contacts": contacts(_arr(local, "whatsapp"), "partner_name"),
        "calendar_event_count": len(events),
        "calendar_events": [{"title": _s(e.get("summary")) or "(untitled)", "time": _s(e.get("start")),
                             "attendee_count": len(_arr(e, "attendees"))} for e in events][:30],
        "missed_call_names": list(dict.fromkeys(missed)),
        "reminder_count": len(_arr(local, "reminders")),
        "action_ledger_open": len([a for a in _arr(local, "action_ledger") if a.get("status") in ("open", "waiting")]),
        "stale_threads": [{"subject": _s(t.get("subject")), "thread_id": _s(t.get("threadId")),
                           "days_since_sent": t.get("daysSinceSent")} for t in _arr(local, "stale_threads")] or None,
        "past_commitments": [{"contact": _s(c.get("contactName")), "summary": _s(c.get("summary")),
                              "type": _s(c.get("type"))} for c in _arr(local, "past_commitments")] or None,
    }




def run_critic(brief_markdown: str, actions: list, manifest: dict, llm=call_gemini,
               violations: list | None = None) -> dict:
    """Port of brief-critic.ts runCritic. Returns {patches, score, summary}; on any failure returns
    an empty (passing) result so the critic never blocks delivery. `violations` (optional) is the
    deterministic brief_validate output — appended to the critic's user content so the revise pass
    fixes them alongside the critic's own findings."""
    actions_summary = [{"id": a.get("id"), "type": a.get("type") or a.get("action_type"),
                        "channel": a.get("channel"),
                        "contact": a.get("contactName") or a.get("contact_name") or "(unknown)",
                        "context": _s(a.get("contextSummary") or a.get("summary"))[:100]}
                       for a in (actions or [])[:30]]
    violations_block = ""
    if violations:
        violations_block = ("\n\n## AUTOMATED VALIDATOR VIOLATIONS (deterministic checks — every one "
                            "is a real defect; include each as a patch)\n"
                            + "\n".join(f"- {v}" for v in violations))
    user_prompt = (CRITIC_SYSTEM + "\n\n## DATA MANIFEST\n" + json.dumps(manifest, indent=2)
                   + "\n\n## GENERATED BRIEF\n" + _s(brief_markdown)
                   + f"\n\n## ACTION ITEMS ({len(actions or [])} total)\n" + json.dumps(actions_summary, indent=2)
                   + violations_block
                   + "\n\nAnalyze the brief against the manifest. Return JSON only.")
    try:
        parsed = json.loads(llm(user_prompt, {"_critic": True}))
    except Exception as e:  # noqa: BLE001
        _diag(f"[compose_brief] critic pass failed ({type(e).__name__}: {str(e)[:120]}) — brief unrevised")
        return {"patches": [], "score": -1, "summary": "critic unavailable"}
    patches = [{"type": p.get("type") or "add_item", "target": p.get("target"),
                "detail": _s(p.get("detail")),
                "severity": p.get("severity") if p.get("severity") in ("critical", "moderate", "minor") else "minor"}
               for p in (parsed.get("patches") or [])]
    return {"patches": patches, "score": parsed.get("score", -1), "summary": _s(parsed.get("summary"))}




REVISE_SYSTEM = """You wrote the communication brief below. A quality critic found issues. Produce a CORRECTED brief that fixes every actionable issue while keeping Sotto's voice, the section structure (the communication sections + the short Coming Up schedule), the markers, and everything the critic did NOT flag exactly as-is. KEEP the short Coming Up section if present. Do not expand it into a full meeting-by-meeting agenda, and do not add meta-commentary about the revision.

A brief that omits a low-stakes thread is CORRECT, not incomplete. Never patch for coverage — fix the flagged issues without adding entries that don't meet the Needs Attention Now bar.

Return JSON: {"brief_markdown":"<corrected brief>","actions":<the same actions[], with any added/fixed/removed per the patches>}."""




def critique_and_revise(out: dict, inputs: dict, llm=call_gemini, violations: list | None = None) -> dict:
    """Run the critic; if it finds critical/moderate issues, revise the brief to fix them. Best-effort:
    any failure returns the original brief unchanged. Stamps out['_critic'] for observability.
    Deterministic brief_validate `violations` ride along: they're shown to the critic AND merged as
    moderate patches so the revise pass fixes them even when the critic itself finds nothing."""
    try:
        manifest = build_data_manifest(inputs)
        critic = run_critic(out.get("brief_markdown", ""), out.get("actions", []), manifest, llm,
                            violations=violations)
        actionable = [p for p in critic["patches"] if p["severity"] in ("critical", "moderate")]
        for v in violations or []:
            if not any(p["detail"] == v for p in actionable):
                actionable.append({"type": "validator", "target": None, "detail": v, "severity": "moderate"})
        out["_critic"] = {"score": critic["score"], "summary": critic["summary"],
                          "patches": len(critic["patches"]), "actionable": len(actionable)}
        if not actionable:
            return out
        patch_lines = "\n".join(f"- [{p['severity']}] {p['type']}: {p['detail']}" for p in actionable)
        revise_prompt = (REVISE_SYSTEM + "\n\n## CRITIC ISSUES TO FIX\n" + patch_lines
                         + "\n\n## CURRENT BRIEF\n" + _s(out.get("brief_markdown"))
                         + "\n\n## CURRENT ACTIONS\n" + json.dumps(out.get("actions", []))[:8000]
                         + "\n\nReturn the corrected JSON only.")
        revised = _normalize_output(json.loads(llm(revise_prompt, {"_revise": True})))
        if revised.get("brief_markdown"):
            revised["_critic"] = out["_critic"]
            revised["extracted_knowledge"] = out.get("extracted_knowledge", revised.get("extracted_knowledge"))
            revised.setdefault("meetings_needing_prep", out.get("meetings_needing_prep", []))
            return revised
    except Exception as e:  # noqa: BLE001
        _diag(f"[compose_brief] critic/revise failed ({type(e).__name__}: {str(e)[:120]}) — delivering draft")
    return out




def _gcal_eid_link(event_id: str, cal_id: str) -> str:
    """Canonical Google Calendar event URL: base64("<eventId> <calendarId>") with padding stripped — the
    same scheme Google's own 'open event' links use. cal_id is the calendar's owner email (the user's).
    Lets a calendar action be one-tap even when the gathered event carried no link."""
    import base64
    if not event_id or not cal_id:
        return ""
    eid = base64.b64encode(f"{event_id} {cal_id}".encode()).decode().rstrip("=")
    return f"https://www.google.com/calendar/event?eid={eid}"




def _self_attendee_email(event: dict) -> str:
    """The user's own address on an event (attendee flagged self:true, else organizer) — used as the
    calendar id when building an eid link, so it works zero-config even if userEmail wasn't passed."""
    for a in _arr(event, "attendees"):
        if a.get("self") and _s(a.get("email")):
            return _s(a.get("email")).lower()
    org = event.get("organizer")
    if isinstance(org, dict) and org.get("self") and _s(org.get("email")):
        return _s(org.get("email")).lower()
    return ""




def _event_link_map(inputs: dict) -> dict:
    """event_id → tappable link, from the gathered calendar. Prefer the join/HTML link (gather_google
    folds hangoutLink/htmlLink into `meetingLink`); else build the canonical Google event URL from the
    event id + the user's calendar email. So a calendar ACTION (which carries only the event id) is
    one-tap even when the LLM didn't copy a link AND google_api.py didn't return htmlLink."""
    google = _obj(inputs, "google")
    default_cal = _s(google.get("userEmail")) or configured_user_email()
    out = {}
    for e in _arr(google, "events"):
        eid = _s(e.get("id"))
        if not eid:
            continue
        cal_id = default_cal or _self_attendee_email(e)
        link = _s(e.get("meetingLink")) or _s(e.get("htmlLink")) or _gcal_eid_link(eid, cal_id)
        if link:
            out[eid] = link
    return out




def _action_tap_link(action: dict, event_links: dict | None = None) -> str:
    """Pick the CHANNEL and IDENTIFIER for a brief action, then hand the URL to the one link builder
    (action_links.link_for). What lives here is the
    routing the raw builder can't do: JID stripping, the routable-phone guard, resolving a calendar
    event id against the gathered events, and inferring a channel the model omitted. Chat-tappable
    schemes throughout (wa.me/mailto:/tel:/sms:, not the Mac app's imessage://) because the brief is
    delivered in chat. Returns '' when there's no routable identifier."""
    ch = _s(action.get("channel")).lower()
    a_type = _s(action.get("type") or action.get("action_type")).lower()
    ident = _s(action.get("contactIdentifier") or action.get("contact_identifier"))
    # WhatsApp/group JIDs contain '@' (…@s.whatsapp.net, …@g.us, …@lid, …@c.us) but are NOT emails —
    # treating them as email is how a WhatsApp action wrongly got a mailto: link. Strip the JID to its
    # phone, and only count a '@' as email when it is NOT a JID.
    is_jid = any(ident.endswith(s) for s in ("@s.whatsapp.net", "@g.us", "@lid", "@c.us"))
    phone_digits = re.sub(r"\D", "", ident.split("@")[0] if is_jid else ident)
    email = _s(action.get("emailReplyTo")) or (ident if ("@" in ident and not is_jid) else "")
    subject = _s(action.get("emailSubject"))

    def _link(channel: str, identifier: str) -> str:
        # No message: a brief's tap link opens the thread, it never prefills a draft.
        return link_for(channel, identifier, "", ("Re: " + subject) if subject else "") if identifier else ""

    # Routable guard (port of actionSchemas isRoutableIdentifier): only a real phone (>=7 digits) gets
    # a phone-shaped link. NEVER fall back to sms:<ident> — that's how name slugs ("arnav_sahu") and
    # group ids ("group_jake_ts") leaked as fake deep links. No phone → no link.
    phone = ("+" + phone_digits) if len(phone_digits) >= 7 else ""

    # Channel is AUTHORITATIVE — a message action never routes to mailto just because its id has '@'.
    if ch in ("email", "gmail", "apple_mail"):
        return _link("email", email) if email else _link("gmail_thread", _s(action.get("emailThreadId")))
    if ch in ("whatsapp", "whatsapp_call"):
        return _link("whatsapp", phone_digits) if phone else ""   # wa.me carries no '+'
    if ch in ("phone",) or a_type == "call_back":
        return _link("phone", phone)
    if ch in ("imessage", "sms"):
        return _link("sms", phone)
    if ch in ("calendar",) or a_type in ("meeting_prep", "meeting_info"):
        # Prefer a link on the action; else resolve the event id (carried in contactIdentifier or
        # eventId) back to the gathered event's meeting/html link. This is what makes calendar actions
        # one-tap — the LLM usually emits the event id but not the link.
        link = _s(action.get("meetingLink")) or _s((event_links or {}).get(
            _s(action.get("eventId") or action.get("event_id")) or ident))
        return _link("calendar", link)
    # No explicit channel → infer from the identifier shape (a real email, else a phone).
    return _link("email", email) if email else _link("sms", phone)




def _attach_tap_links(out: dict, event_links: dict | None = None) -> dict:
    actions = out.get("actions") or []
    for a in actions:
        if isinstance(a, dict) and not a.get("tap_link"):
            link = _action_tap_link(a, event_links)
            if link:
                a["tap_link"] = link
    linked = sum(1 for a in actions if isinstance(a, dict) and a.get("tap_link"))
    dropped = [f"{_s(a.get('channel'))}:{_s(a.get('contactIdentifier') or a.get('contact_identifier'))}"
               for a in actions if isinstance(a, dict) and not a.get("tap_link")]
    _diag(f"[compose_brief] tap_links: {linked}/{len(actions)} actions linked"
          + (f"; no link for {dropped}" if dropped else ""))
    return out




def _emit_metrics(inputs: dict) -> None:
    """Emit the run's [brief-cost] summary. Reports the phases skipped this run (critic/revise that
    produced no LLM call) alongside the ones that ran. Fully swallowed — observability never blocks."""
    try:
        kind = _s(inputs.get("type")) or "morning"
        try:
            google = _obj(inputs, "google")
            tz = _s(google.get("userTimezone")) or configured_tz()
            date = _user_local_date(tz)
        except Exception:  # noqa: BLE001
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ran = set(metrics.summary().get("phases", {}))
        skipped = [p for p in ("critic", "revise") if p not in ran]
        metrics.emit(date, kind, skipped)
    except Exception:  # noqa: BLE001
        pass


# ── Evening followup merge (roadmap Sprint 0 #4) ──────────────────────────────────────────────────
# The 16:45 standalone followup and the 17:30 evening brief said the same thing 45 minutes apart.
# `--type evening` now runs the followup extraction (compose_followup's importable core) inside the
# brief pipeline, renders it as prompt context, and writes its commitments to the continuity ledger
# via the existing apply path. Guarded end-to-end: ANY failure → empty context, never a lost brief.

FOLLOWUP_MERGE_SINCE_HOURS = 12    # today's meetings only — the standalone 36h window would re-surface yesterday's


def _followup_scripts_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "followup", "scripts")


def _followup_evening_context(inputs: dict, llm) -> dict:
    """Run compose_followup's importable core against the SAME gathered inputs the evening brief has.
    Returns {followup_markdown, commitments[], drafts[]} or {} (nothing ended / any failure)."""
    try:
        path = _followup_scripts_path()
        if path not in sys.path:
            sys.path.insert(0, path)
        import compose_followup as _cf  # noqa: PLC0415
        google = _obj(inputs, "google")
        f_inputs = {
            "granola": [],                                   # granola rides in local.granola_meetings
            "local": _normalize_local(inputs),
            "google": google,
            "user_email": _s(google.get("userEmail")) or configured_user_email(),
            "user_timezone": _s(google.get("userTimezone")) or configured_tz(),
            "_followup": True,                               # lets an injected llm stub route the call
        }
        return _cf.compose_for_brief(f_inputs, since_hours=FOLLOWUP_MERGE_SINCE_HOURS, llm=llm) or {}
    except Exception as e:  # noqa: BLE001
        _diag(f"[compose_brief] followup merge failed ({type(e).__name__}: {str(e)[:120]}) — evening "
              "brief continues without followup context")
        return {}


def _apply_followup_commitments(fu: dict, inputs: dict) -> None:
    """Deterministic ledger write for the merged followup's commitments — the SAME apply path the
    standalone skill uses (apply_commitments.apply). Best-effort; never blocks the brief."""
    if not (isinstance(fu, dict) and fu.get("commitments")):
        return
    try:
        path = _followup_scripts_path()
        if path not in sys.path:
            sys.path.insert(0, path)
        import apply_commitments as _ac  # noqa: PLC0415
        user_email = _s(_obj(inputs, "google").get("userEmail")) or configured_user_email()
        res = _ac.apply(fu, user_email)
        _diag(f"[compose_brief] followup commitments → ledger: {res.get('written', 0)} written, "
              f"{res.get('deduped', 0)} deduped")
    except Exception as e:  # noqa: BLE001
        _diag(f"[compose_brief] followup ledger apply failed ({type(e).__name__}: {str(e)[:120]})")


def _render_followup_context(fu: dict) -> str:
    """Deterministically render the merged followup result as an evening-brief prompt block. Empty
    string when there's nothing worth saying (no markdown, no commitments, no drafts)."""
    if not isinstance(fu, dict):
        return ""
    md = _s(fu.get("followup_markdown")).strip()
    commitments = [c for c in (fu.get("commitments") or []) if isinstance(c, dict)]
    drafts = [d for d in (fu.get("drafts") or []) if isinstance(d, dict)]
    if not (commitments or drafts or md):
        return ""
    lines = ["## Today's Meeting Follow-Ups (PRE-COMPUTED — evening merge of the followup extraction)",
             "From meetings that ended today (transcripts/notes). Weave these into the evening brief: "
             "commitments belong in the accountability framing; ready drafts get ONE mention ('drafts are "
             "ready — ask and I'll share them'). Do not re-derive or duplicate them."]
    if md:
        lines += ["", md]
    if commitments:
        lines += ["", "Commitments detected (ALREADY recorded in the action ledger — do not double-count):"]
        for c in commitments:
            owner = _s(c.get("owner")) or "you"
            due = _s(c.get("due"))
            lines.append(f"- {owner}: {_s(c.get('what'))}" + (f" (due {due})" if due else ""))
    if drafts:
        lines += ["", f"Ready-to-send drafts prepared: {len(drafts)} "
                      "(" + ", ".join(_s(d.get('to_name')) or _s(d.get('to_email')) or "draft" for d in drafts[:5]) + ")"]
    return "\n".join(lines)


# ── Already-nudged today (the surfaced ledger's brief-side reader) ────────────────────────────────
# events/surfaced.jsonl records EVERY funnel verdict at verdict time (triage_event._record_surfaced:
# {ts, sender, channel, verdict, reason, class}). Nothing read it, so a 3pm ask could tap the user at
# 3:05 and then get re-derived and re-told at 5:30 as if it were news — the double-tell. The evening
# brief now reads today's DELIVERED nudges (verdict "agent") and compresses them to one line each.
# Read-only, bounded, and fail-quiet: a missing or corrupt ledger renders nothing.

SURFACED_TAIL_LINES = 800     # the writer bounds the file at 4000; a day of verdicts is far less
SURFACED_NUDGE_MAX = 10       # rows rendered — a brief that lists 30 nudges is a second brief


def _surfaced_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "events", "surfaced.jsonl")


def _surfaced_rows_today(today: str, tzinfo=None) -> list:
    """Every verdict row stamped on the user's local `today`, oldest first, as
    (row, local_datetime). `today` is the user's local date and `tzinfo` their zone (rows are
    stamped in UTC), so an evening read never picks up yesterday's verdicts. ONE reader for the
    file — the already-nudged block and the receipts block filter this, they don't re-open it.
    Bounded tail; unparseable rows are skipped, never fatal."""
    try:
        with open(_surfaced_path(), encoding="utf-8") as f:
            lines = f.readlines()[-SURFACED_TAIL_LINES:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        ts = _parse_ts(_s(row.get("ts")))
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        local = ts.astimezone(tzinfo) if tzinfo is not None else ts
        if local.strftime("%Y-%m-%d") != today:
            continue
        out.append((row, local))
    out.sort(key=lambda r: r[1])
    return out


def _load_surfaced_nudges(today: str, tzinfo=None) -> list:
    """Today's DELIVERED nudges, oldest first: [{time, sender, class, reason}]."""
    out = []
    for row, local in _surfaced_rows_today(today, tzinfo):
        if _s(row.get("verdict")) != "agent":
            continue
        out.append({"time": f"{local.hour % 12 or 12}:{local.minute:02d}"
                            + ("pm" if local.hour >= 12 else "am"),
                    "sender": _s(row.get("sender")), "cls": _s(row.get("class")),
                    "reason": _s(row.get("reason"))[:160]})
    return out[-SURFACED_NUDGE_MAX:]


def _format_already_nudged(rows: list) -> str:
    """The prompt block. Empty string when nothing was nudged today — the common case, and the one
    where an empty section would only invite the model to say "no nudges today"."""
    if not rows:
        return ""
    lines = ["## Already Nudged Today (Sotto ALREADY interrupted the user about these)",
             "Each line is a nudge the user received on their phone hours ago — they have seen it. "
             "Do NOT re-tell it as news and never lead the brief with one: if it is still open, it "
             "compresses to ONE line in the same matter-of-fact register as Already Handled "
             "(\"nudged you at 3:05 — Sarah's ask; still open unless you handled it\"), and if "
             "today's data shows it resolved it belongs in ✅ Already Handled instead. An item NOT "
             "listed here was never nudged — treat it normally."]
    for r in rows:
        who = r["sender"] or "unknown sender"
        cls = f" [{r['cls']}]" if r["cls"] else ""
        why = f" — {r['reason']}" if r["reason"] else ""
        lines.append(f"- {r['time']}: {who}{cls}{why}")
    return "\n".join(lines) + "\n"


def _already_nudged_block(tz: str) -> str:
    """Read whenever today already has delivered nudges — regardless of brief type. The 06:30 cron
    morning brief simply finds none; a wake-path morning brief at 9am finds the 7:15 chase and
    stops re-telling it as news. Any failure renders nothing."""
    try:
        return _format_already_nudged(_load_surfaced_nudges(_user_local_date(tz), _resolve_tz(tz)))
    except Exception as e:  # noqa: BLE001
        _diag(f"[compose_brief] already-nudged read failed ({type(e).__name__}) — section omitted")
        return ""


def _normalize_attendee_research(v) -> list:
    """Accept BOTH attendee-research shapes: the documented bare list, and the {"attendees":[…]}
    envelope research_attendees.py --out actually writes (which the skill passes straight through
    as --attendee-research). Without this, the dict shape read as [] in the renderer and the whole
    research block silently vanished from the prompt. Anything else (None/garbage) → []."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict) and isinstance(v.get("attendees"), list):
        return v["attendees"]
    return []


# ── The open-items contract ───────────────────────────────────────────────────────────────────────
# One sentence: every open ledger item appears in the brief exactly once — as an ask with its age, or
# in Already Handled when today's data shows it resolved, never silently dropped. The prompt states it,
# brief_validate rule (g) measures it, the critic retry fixes it — and this backstop guarantees it.

_FILTERED_HEADING_RE = re.compile(r"(?im)^#{1,6}[ \t]+.*filtered.*$")


def _open_ledger_entries(inputs: dict) -> list:
    """The open/waiting ledger entries this brief was built from — the same ones _format_action_ledger
    rendered into the prompt (meeting shadows already dropped by _normalize_local)."""
    try:
        return [e for e in _arr(_normalize_local(inputs), "action_ledger")
                if isinstance(e, dict) and e.get("status") in ("open", "waiting")]
    except Exception:  # noqa: BLE001
        return []


def _insert_before_filtered(md: str, block: str) -> str:
    """THE seam every deterministic markdown appendix lands on: insert `block` just before the
    `## Filtered` heading (the last section), or append it at the end when there is none. Both
    post-LLM appendices — Still open and What moved today — are READ from the record rather than
    authored by a model, so they share one insertion rule and cannot drift apart."""
    m = _FILTERED_HEADING_RE.search(md)
    return (md[:m.start()] + block + md[m.start():]) if m else (md.rstrip() + "\n\n" + block)


def _append_still_open(out: dict, open_ledger: list, now=None) -> dict:
    """Append one plain line per open ledger item the brief never named, just before the Filtered
    section (the last one). The brief is never suppressed or delayed over this — a terse line about a
    loop the narrative forgot beats a brief that tells the user they have nothing to do."""
    try:
        import brief_validate  # noqa: PLC0415  (_shared/lib is on sys.path)
        md = _s(out.get("brief_markdown"))
        missed = brief_validate.missing_open_loops(md, open_ledger) if md else []
        if not missed:
            return out
        lines = []
        for e in missed:
            age = _action_age(_s(e.get("created_at")), now)
            lines.append(f"- **{_s(e.get('contact_name')) or 'Open loop'}** — {_s(e.get('summary'))}"
                         + (f" ({age})" if age != "unknown" else ""))
        block = "## Still open\n" + "\n".join(lines) + "\n\n"
        out["brief_markdown"] = _insert_before_filtered(md, block)
        _diag(f"[compose_brief] still-open backstop: {len(missed)} open ledger item(s) the brief dropped")
    except Exception:  # noqa: BLE001
        pass
    return out


# ── "What moved today" — the evening brief's outcome receipts ─────────────────────────────────────
# One sentence: the evening brief proves the quiet was work — what moved, who moved it, nothing else.
#
# The owner's standing bar governs every line here: a chief of staff cares about OUTCOMES and never
# performs activity, so this block reports what MOVED — chased, closed, held, prepped, offered — and
# never throughput. "63 emails processed" is exactly the line it refuses to write.
#
# Deterministic and appended POST-LLM, the _append_still_open pattern: a receipt is a claim about the
# record, so it is READ from the record rather than authored by a model, and any failure renders
# nothing instead of costing the user their brief. Every read is bounded and scoped to the user's
# local day (injected-now aware, so a corpus replay reports the day it is replaying). A day where
# nothing moved gets NO block at all — an empty receipts block would be activity theater about
# inactivity, and the quiet-day line already says it better.

RECEIPT_NAMES_MAX = 3        # past this a line groups by what happened; naming is the point, listing isn't
RECEIPT_WHAT_MAX = 60        # a loop's summary rendered as a clause, not a paragraph

# ledger_io.CHASE_MAX is 2, so there is no third ask — an out-of-range count just drops the ordinal.
_CHASE_ORDINALS = {1: "first", 2: "second"}

# resolution → (clause when the ledger entry carries a name, clause when it doesn't). Plain verbs
# only: the vocabulary is continuity_resolve's (_terminate), the English is the user's.
_RESOLUTION_CLAUSES = {
    "delivered":         ("{name} delivered {what}",           "{what} came through"),
    "replied":           ("you got back to {name}",            "{what} got an answer"),
    "called":            ("you called {name} back",            "{what} got a call back"),
    "scheduled_meeting": ("{name} is on the calendar",         "{what} is on the calendar"),
    "brief_handled":     ("you handled {name}",                "{what} is handled"),
    "meeting_passed":    ("your meeting with {name} happened", "{what} happened"),
    "user_resolved":     ("you closed {name} out yourself",    "you closed {what} yourself"),
}
_RESOLUTION_FALLBACK = ("{name} — {what}", "{what} resolved")

# The same vocabulary, coarsened to WHO moved it — used only on a day with more closures than we
# would name. Anything unmapped is "closed out".
_RESOLUTION_BUCKETS = {"delivered": "they delivered", "replied": "you answered",
                       "called": "you answered", "scheduled_meeting": "you answered",
                       "brief_handled": "you answered"}
_BUCKET_FALLBACK = "closed out"

# Queue classes that mean "this would have interrupted you, and Sotto chose a better moment":
# triage_event's meeting hold, thread cooldown, daily interrupt budget, and the reconnect-grace
# staleness gate. Every one of them is a deliberate hold, and each already has its detail in the
# waiting room — this block only says how many.
_HELD_QUEUE_CLASSES = frozenset({"meeting_hold", "cooldown", "budget", "stale"})


def _receipt_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _receipt_what(entry: dict) -> str:
    """The loop's own words as a clause. Ledger summaries are already one line; this only trims."""
    what = _s(entry.get("summary")).strip().rstrip(".")
    return (what[:RECEIPT_WHAT_MAX].rstrip() + "…") if len(what) > RECEIPT_WHAT_MAX else what


def _chase_clause(e: dict) -> str:
    """One delivered chase, in plain English: 'Gave Maya a nudge about the contract (second ask).'"""
    name, what = _s(e.get("contact_name")).strip(), _receipt_what(e)
    if name and what:
        s = f"Gave {name} a nudge about {what}"
    elif name:
        s = f"Gave {name} a nudge"
    elif what:
        s = f"Sent a nudge about {what}"
    else:
        return ""
    ordinal = _CHASE_ORDINALS.get(_receipt_int(e.get("chased_count")))
    return s + (f" ({ordinal} ask)." if ordinal else ".")


def _closed_clause(e: dict) -> str:
    """One closed loop, named where the ledger has a name: 'Ron delivered the deck'."""
    name, what = _s(e.get("contact_name")).strip(), _receipt_what(e)
    if not (name or what):
        return ""
    named, bare = _RESOLUTION_CLAUSES.get(_s(e.get("resolution")).strip().lower(),
                                          _RESOLUTION_FALLBACK)
    return named.format(name=name, what=what or "it") if name else bare.format(what=what)


def _closed_grouping(entries: list) -> str:
    """'3 they delivered, 2 you answered' — the too-many-to-name form, biggest group first."""
    counts: dict = {}
    for e in entries:
        b = _RESOLUTION_BUCKETS.get(_s(e.get("resolution")).strip().lower(), _BUCKET_FALLBACK)
        counts[b] = counts.get(b, 0) + 1
    return ", ".join(f"{n} {b}" for b, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _ledger_receipts(day: str) -> tuple:
    """(chases delivered today, loops closed today) read straight from the continuity ledger.
    Read-only — continuity_resolve stays the ledger's one writer. Unlike every other brief-side
    ledger read this one loads TERMINAL entries too: a loop that closed today is precisely what
    this block reports, and load_active() drops exactly those. A loop that merely aged out is NOT
    a closure (status expired/dismissed) — nothing moved, so nothing is claimed."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import ledger_io  # noqa: PLC0415
        entries = ledger_io.load_entries()
    except Exception:  # noqa: BLE001
        return [], []
    chased = [e for e in entries if _s(e.get("last_chased_at"))[:10] == day]
    closed = [e for e in entries if _s(e.get("status")) == "resolved"
              and _s(e.get("resolved_at"))[:10] == day]
    return chased, closed


def _held_count(day: str, tz: str) -> int:
    """How many interruptions Sotto held back today — surfaced.jsonl rows whose verdict was `queue`
    with a deliberate hold class. Count only: the waiting room already carries every detail, and a
    second list of them here would be the double-tell the Already Nudged block exists to prevent."""
    try:
        return sum(1 for row, _dt in _surfaced_rows_today(day, _resolve_tz(tz))
                   if _s(row.get("verdict")) == "queue" and _s(row.get("class")) in _HELD_QUEUE_CLASSES)
    except Exception:  # noqa: BLE001
        return 0


def _cache_json(name: str):
    """One bounded read of a $SOTTO_DATA/cache file. Missing/corrupt → None, silently."""
    try:
        with open(os.path.join(os.environ.get("SOTTO_DATA", "/data"), "cache", name),
                  encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _prepped_count(day: str) -> int:
    """People researched for upcoming meetings today — research_attendees' own dated cache
    ($SOTTO_DATA/cache/research_<day>.json), which it writes only after a run that found someone."""
    data = _cache_json(f"research_{day}.json")
    return len(_arr(data if isinstance(data, dict) else {}, "attendees"))


def _taps_count(day: str) -> int:
    """Post-meeting taps that actually fired today — calcache's date-keyed meeting_taps.json. A
    stamp from another date reads as zero, which IS the day rollover."""
    st = _cache_json("meeting_taps.json")
    if not isinstance(st, dict) or _s(st.get("date")) != day:
        return 0
    return len([k for k in _arr(st, "fired") if isinstance(k, str)])


def _has_meetings_on(events: list, day: str, tz: str) -> bool:
    """Does the calendar hold an event on this user-local day? Offsets are honored; an event whose
    start won't parse falls back to its literal date part."""
    tzinfo = _resolve_tz(tz)
    for e in events:
        start = _s(e.get("start"))
        st = _parse_ts(start)
        if st is None:
            if start and _date_only(start) == day:
                return True
            continue
        if st.tzinfo is not None and tzinfo is not None:
            st = st.astimezone(tzinfo)
        if st.strftime("%Y-%m-%d") == day:
            return True
    return False


def _receipt_lines(inputs: dict, day: str, tz: str) -> list:
    """The block's lines, each rendered ONLY when its number is nonzero. Deliberately NOT here: a
    'Sotto nudged you N times' line. Today's delivered nudges already reach the user twice — on
    their phone at the time, and compressed into the brief by the Already Nudged Today block — so a
    count of them adds no outcome, only throughput."""
    lines = []
    chased, closed = _ledger_receipts(day)

    chase_clauses = [c for c in (_chase_clause(e) for e in chased) if c]
    if chase_clauses:
        lines += (chase_clauses if len(chase_clauses) <= RECEIPT_NAMES_MAX
                  else [f"Nudged {len(chase_clauses)} people about what they owe you."])

    closed_clauses = [c for c in (_closed_clause(e) for e in closed) if c]
    if closed_clauses:
        n = len(closed_clauses)
        detail = ("; ".join(closed_clauses) if n <= RECEIPT_NAMES_MAX else _closed_grouping(closed))
        lines.append(f"Closed {n} loop{'' if n == 1 else 's'} — {detail}.")

    held = _held_count(day, tz)
    if held:
        lines.append(f"Held {held} interruption{'' if held == 1 else 's'} until a better moment.")

    # Prep is only a receipt when there is something it prepped FOR: research runs on a horizon, so
    # a count with no meetings tomorrow would be a claim about work nobody is about to need.
    tomorrow = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    if _has_meetings_on(_arr(_obj(inputs, "google"), "events"), tomorrow, tz):
        prepped = _prepped_count(day)
        if prepped:
            lines.append(f"Prepped {prepped} {'person' if prepped == 1 else 'people'} for tomorrow.")

    taps = _taps_count(day)
    if taps:
        lines.append(f"Offered follow-ups after {taps} meeting{'' if taps == 1 else 's'}.")
    return lines


def _append_receipts(out: dict, inputs: dict, now=None) -> dict:
    """Append `## What moved today` to the EVENING brief, just before the Filtered section (after
    Still open, which lands at the same seam first). Never suppresses or delays the brief: any
    failure — unreadable ledger, missing cache, malformed day — renders nothing at all."""
    try:
        if (_s(inputs.get("type")) or "morning") != "evening":
            return out
        md = _s(out.get("brief_markdown"))
        if not md:
            return out
        tz = _brief_tz(inputs)
        lines = _receipt_lines(inputs, _brief_day(tz, now), tz)
        if not lines:
            return out                      # nothing moved → no block (see the note above)
        block = "## What moved today\n" + "\n".join(f"- {ln}" for ln in lines) + "\n\n"
        out["brief_markdown"] = _insert_before_filtered(md, block)
        _diag(f"[compose_brief] what-moved receipts: {len(lines)} line(s)")
    except Exception:  # noqa: BLE001
        pass
    return out


# ── "A newer Sotto is published" — one line, once, in the next brief ──────────────────────────────
# One sentence: a new version gets ONE quiet line in whichever brief comes next and a line on the
# dashboard, never an interruption of its own.
#
# It is housekeeping, so it spends no interrupt budget, is never a standalone push, and never
# repeats: the marker below records the version the moment the line is written, and a version
# already recorded is never mentioned again. The two facts come from the receiver's daily update
# check (receiver.check_for_update, the ONE writer of cache/update_check.json) — this process can't
# see /app/VERSION, which is why `current` lives in that file. No check, no volume, no newer
# version, SOTTO_UPDATE_CHECK=0: the file is absent or says nothing, and so does the brief.

UPDATE_CHECK_CACHE = "update_check.json"     # written by the receiver's daily check
UPDATE_NOTICE_MARKER = "update_notice.json"  # {noticed_version} — written here, once per version


def _update_notice_version() -> str:
    """The published version this brief should mention, or "" — which is every case but one: no
    cache, no stamp on this build, nothing newer, or a version already mentioned in an earlier
    brief. Reads only — recording the version is the caller's job, and only when it writes a line."""
    cache = _cache_json(UPDATE_CHECK_CACHE)
    if not isinstance(cache, dict):
        return ""
    latest, current = _s(cache.get("latest")).strip(), _s(cache.get("current")).strip()
    if not latest or not current or latest == current:
        return ""
    marker = _cache_json(UPDATE_NOTICE_MARKER)
    noticed = _s(marker.get("noticed_version")).strip() if isinstance(marker, dict) else ""
    return "" if noticed == latest else latest


def _write_update_notice_marker(version: str) -> None:
    """Record the version this brief is about to mention (atomic, the _archive_brief pattern). On
    COMPOSE, like the archive next to it: a brief that composed and then failed to deliver spends
    the notice, which is the right way round for housekeeping."""
    d = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "cache")
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{UPDATE_NOTICE_MARKER}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"noticed_version": version}, f)
    os.replace(tmp, os.path.join(d, UPDATE_NOTICE_MARKER))


def _append_update_notice(out: dict) -> dict:
    """Append the single update line at the same seam the other two appendices use, morning or
    evening, whichever brief comes first. Same shape as its neighbours: any failure — unreadable
    cache, read-only volume, no markdown — renders nothing at all and costs the user nothing."""
    try:
        md = _s(out.get("brief_markdown"))
        if not md:
            return out
        version = _update_notice_version()
        if not version:
            return out
        block = (f"Sotto {version} is ready — merge the Railway update PR (or Sync fork on GitHub); "
                 "the redeploy is the update.\n\n")
        # Marker BEFORE the insert, deliberately: a volume that can't record "said it" would
        # otherwise repeat the line in every brief forever, and the house rule is fail-to-silence.
        _write_update_notice_marker(version)
        out["brief_markdown"] = _insert_before_filtered(md, block)
        _diag(f"[compose_brief] update notice: {version}")
    except Exception:  # noqa: BLE001
        pass
    return out


def compose(inputs: dict, llm=call_gemini, critic: bool = False) -> dict:
    """Run the FULL FLEX extraction. The static policy half of the template goes out as Gemini's
    systemInstruction (byte-stable → implicit prefix caching) with the three-field responseSchema
    attached; the rendered data is the user turn. `llm(prompt, inputs)` still receives the rendered
    user prompt plus the original inputs dict; an llm that accepts system=/schema= kwargs (the real
    call_gemini) also gets those. With critic=True, the second-pass critic+revise quality gate runs
    per SOTTO_CRITIC ("auto" default / "always" / "off" — see _critic_decision); critic=False never
    runs it. brief_validate runs post-extraction on every path (log-only; feeds the critic)."""
    try:
        metrics.start_run()                                # cost/latency accumulator for THIS run
    except Exception:  # noqa: BLE001
        pass
    template = _load_prompt()
    system_text, user_template = _split_prompt(template)

    # Normalize once, early: every path into compose (CLI files, back-compat stdin, direct import)
    # gets the same attendee-research shape tolerance.
    if inputs.get("attendee_research") is not None and not isinstance(inputs.get("attendee_research"), list):
        inputs = dict(inputs)
        inputs["attendee_research"] = _normalize_attendee_research(inputs["attendee_research"])

    # Evening merge: pre-compute the followup context so build_prompt can render it (Sprint 0 #4).
    if (_s(inputs.get("type")) or "morning") == "evening" and not inputs.get("_followup_context"):
        fu = _followup_evening_context(inputs, llm)
        fu_text = _render_followup_context(fu)
        if fu_text:
            _apply_followup_commitments(fu, inputs)        # deterministic ledger write (existing path)
            inputs = dict(inputs)
            inputs["_followup_context"] = fu_text

    prompt = build_prompt(user_template, inputs)
    raw = _invoke_llm(llm, prompt, inputs, system=system_text, schema=BRIEF_RESPONSE_SCHEMA)
    out = _normalize_output(json.loads(raw))

    # Deterministic post-hoc validator (Sprint 1 #6): log-only, never blocks delivery; the violation
    # list is handed to the critic so the revise pass fixes what code could measure.
    open_ledger = _open_ledger_entries(inputs)
    violations = []
    try:
        import brief_validate  # noqa: PLC0415  (_shared/lib is on sys.path)
        # first_run: the one-time onboarding note MANDATES a trailing offer line after the brief —
        # the validator must not count it as Coming Up overflow (which would make the critic delete
        # a schedule line or the offer on the single most-judged brief).
        violations = brief_validate.validate(out.get("brief_markdown", ""), out.get("actions") or [], prompt,
                                             first_run=_is_first_run(inputs, {}),
                                             action_ledger=open_ledger)
        if violations:
            _diag(f"[brief-validate] {len(violations)} violation(s): " + " | ".join(violations[:12]))
    except Exception:  # noqa: BLE001
        pass

    if critic:
        payload_chars = max(0, len(prompt) - len(user_template))  # the rendered source data, sans template
        run, reason = _critic_decision(_critic_mode(), payload_chars, len(out.get("actions") or []))
        _diag(f"[compose_brief] critic {'ran' if run else 'skipped'} ({reason})")
        if run:
            out = critique_and_revise(out, inputs, llm, violations=violations)
        else:
            out["_critic"] = {"skipped": True, "reason": reason}
    # Last line of defence for the open-items contract (runs after the critic's retry had its chance)
    out = _append_still_open(out, open_ledger, _brief_now(inputs))
    # …and then the evening's outcome receipts, read from the record rather than written by a model.
    out = _append_receipts(out, inputs, _brief_now(inputs))
    # …and last, the one-line "a newer Sotto is published" notice — housekeeping, once per version.
    out = _append_update_notice(out)
    # chat-tappable wa.me/mailto:/tel:/sms: link per action; calendar actions resolve via the event map
    result = _attach_tap_links(out, _event_link_map(inputs))
    # The chat-deliverable text: markers stripped, WhatsApp-safe formatting (chatfmt.to_chat is the
    # ONE such transformation). Deterministic here so delivery never depends on the agent
    # remembering to sed the markers out. brief_markdown stays untouched for records/critic/actions.
    result["brief_text"] = to_chat(result.get("brief_markdown"))
    _emit_metrics(inputs)                                  # one [brief-cost] line — success + degraded
    return result




def _archive_brief(out: dict, brief_type: str) -> None:
    """Delivered-brief archive (contracts/exhaust-schema.md: briefs/<date>_<type>.json) — read by
    the dashboard's Briefs tab today and the Knowledge phase's delta briefs later. Best-effort:
    an archive failure must never cost the user the brief itself. Same-day re-runs overwrite —
    the archive holds the brief the user actually got last.

    Composing is NOT delivering: the midday-digest window is advanced by brief_marker.claim()
    instead (Sprint 0 §2c), so an on-demand 9am compose that loses the deliver-once claim can't
    swallow the 6:30–9:00 digest window."""
    try:
        date = _user_local_date(configured_tz())
        d = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "briefs")
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f".{date}_{brief_type}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f)
        os.replace(tmp, os.path.join(d, f"{date}_{brief_type}.json"))
    except Exception:
        pass


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Render + run the Sotto FLEX brief extraction.")
    ap.add_argument("inputs", nargs="?", help="a single assembled inputs JSON file (back-compat; or stdin)")
    ap.add_argument("--type", choices=["morning", "evening"], default="morning")
    ap.add_argument("--local", help="read_local output JSON (the 16 local sources) — REQUIRED for a real brief")
    ap.add_argument("--gmail", help="Gmail JSON: an array, or {emails:[...]} / {messages:[...]}")
    ap.add_argument("--calendar", help="Calendar JSON: an array, or {events:[...]} / {items:[...]}")
    ap.add_argument("--granola", help="Granola JSON: an array, or {meetings:[...]}")
    ap.add_argument("--knowledge", help="prior knowledge JSON (knowledge_query.py output)")
    ap.add_argument("--attendee-research", dest="attendee_research", help="attendee research JSON array")
    ap.add_argument("--user-email", dest="user_email")
    ap.add_argument("--user-timezone", dest="user_timezone")
    ap.add_argument("--window-hours", dest="window_hours", type=int, default=24)
    ap.add_argument("--no-critic", dest="no_critic", action="store_true",
                    help="skip the second-pass critic+revise quality gate "
                         "(env SOTTO_CRITIC=auto|always|off tunes it when not skipped)")
    ap.add_argument("--seed-snapshot", dest="seed_snapshot", metavar="SEED.json",
                    help="write the local snapshot from a read_local seed file, print a count, exit "
                         "(setup step 3) — no brief is composed")
    args = ap.parse_args()

    # ── Seed the local snapshot and stop ──────────────────────────────────────────────────────────
    # The event funnel resolves EVERY sender's name from this snapshot (triage_event._load_snapshot_
    # local). Until this file exists it knows nobody, so day-one nudges are nameless — and the only
    # other writer is a delivered brief. Setup writes it once, straight from the same seed the style
    # /graph/pulse seeders already read. Same writer as the brief (_save_local_snapshot), so the
    # contacts carry-forward and file shape can't diverge.
    if args.seed_snapshot:
        try:
            with open(args.seed_snapshot, encoding="utf-8") as f:
                seed = json.load(f)
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"seeded": False, "reason": f"unreadable seed ({type(e).__name__})"}))
            return
        local = unwrap_tool_result(seed)     # tolerate a raw MCP tool-result wrapper, like --local
        if not _local_has_data(local):
            print(json.dumps({"seeded": False, "reason": "seed carried no local data"}))
            return
        _save_local_snapshot(local)
        counts = {k: len(_arr(local, k)) for k in _LOCAL_SOURCE_KEYS if _arr(local, k)}
        print(json.dumps({"seeded": True, "path": _snapshot_path(),
                          "contacts": len(_arr(local, "contacts")), "counts": counts}))
        return

    # Critic on by default for real runs; auto-off under the test stub (it can't return critic JSON).
    use_critic = not args.no_critic and not os.environ.get("SOTTO_LLM_STUB")

    using_files = any([args.local, args.gmail, args.calendar, args.granola, args.knowledge, args.attendee_research])
    if not using_files:
        # Back-compat: a single assembled inputs object from a file arg or stdin.
        raw = open(args.inputs).read() if args.inputs else sys.stdin.read()
        parsed = json.loads(raw)
        out = compose(parsed, critic=use_critic)
        _archive_brief(out, parsed.get("type") or args.type)
        print(json.dumps(out))
        return

    # Friendly mode: one file per source — no hand-assembled JSON. Missing/unreadable files → empty.
    def load(path, default):
        if not path:
            return default
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def pick_list(v, *keys):
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k in keys:
                if isinstance(v.get(k), list):
                    return v[k]
        return []

    gmail_raw = load(args.gmail, [])
    google = {
        "emails": pick_list(gmail_raw, "emails", "messages"),
        "events": pick_list(load(args.calendar, []), "events", "items"),
    }
    # gather_google marks a capped inbox window ({"emails": [...], "truncated_at": N}) — carry the
    # honesty note through so the brief never presents a truncated window as the full inbox.
    if isinstance(gmail_raw, dict) and gmail_raw.get("truncated_at"):
        google["emailsTruncatedAt"] = gmail_raw["truncated_at"]
    if args.user_email:
        google["userEmail"] = args.user_email
    if args.user_timezone:
        google["userTimezone"] = args.user_timezone
    local = unwrap_tool_result(load(args.local, {}))   # accept raw read_local tool-result wrappers
    if _local_has_data(local):
        _save_local_snapshot(local)          # fresh data → remember it for a future Bridge outage
    else:
        local = _local_fallback(local)        # Bridge unreachable → degrade to the last good snapshot
    # Visibility for "the brief didn't marry Gmail + local": log what each side actually contributed,
    # so the logs distinguish "Gmail wasn't gathered" from "the user had no email". Marrying the two is
    # the whole point — a local-only or Google-only brief is a degraded brief.
    n_local_msgs = len(_arr(local, "imessage")) + len(_arr(local, "whatsapp"))
    n_contacts = len(_arr(local, "contacts"))
    # Full per-source breakdown so every source can be verified on a real run (0 = not flowing).
    def _n(k):
        return len(_arr(local, k))
    st_apps = len(_obj(local, "screen_time").get("top_apps") or [])
    _diag("[compose_brief] inputs: "
          f"{len(google['emails'])} emails, {len(google['events'])} events | imsg {_n('imessage')}, "
          f"wa {_n('whatsapp')}, calls {_n('calls')}, wa_calls {_n('whatsapp_calls')}, "
          f"reminders {_n('reminders')}, notes {_n('apple_notes')}, chrome {_n('chrome_history')}, "
          f"safari {_n('safari_history')}, files {_n('recent_files')}, screen_time {st_apps} apps, "
          f"contacts {n_contacts}")
    if local.get("imessage") and not google["emails"]:
        _diag("[compose_brief] WARNING: local messages present but 0 Gmail — brief will be local-only. "
              "If Google is connected, the agent did NOT gather Gmail before composing.")
    if n_local_msgs and not n_contacts:
        _diag("[compose_brief] WARNING: messages present but 0 contacts — names won't resolve. "
              "Ensure read_local returned the contacts array.")
    inputs = {
        "type": args.type,
        "window_hours": args.window_hours,
        "google": google,
        "granola": load(args.granola, {}),
        "local": local,
        "prior_knowledge": load(args.knowledge, {}),
        # research_attendees.py --out writes {"attendees":[…]} — accept it AND the bare list
        # (same pick_list tolerance as gmail), so the research block never silently vanishes.
        "attendee_research": _normalize_attendee_research(load(args.attendee_research, [])),
    }
    out = compose(inputs, critic=use_critic)
    _archive_brief(out, args.type)
    print(json.dumps(out))




if __name__ == "__main__":
    main()
