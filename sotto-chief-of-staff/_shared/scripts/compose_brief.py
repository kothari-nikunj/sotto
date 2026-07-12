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

This file used to be a 2,400-line monolith that doubled as the pack's utility library (~10
sibling scripts do `import compose_brief as cb`). The pure-utility layers were split into
_shared/lib/{textutil,timeutil,gemini,render_local}.py; this file keeps the ORCHESTRATION
(compose / build_prompt), the extraction/critic/revise flow, signal correlation, preference
application, tap-link building and the coverage/first-run lines. Every moved helper is re-imported
below at its old location (see the COMPAT SURFACE block) so `cb.<helper>` keeps resolving.

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

Env: GOOGLE_AI_API_KEY (host's native Gemini key), SOTTO_GEMINI_MODEL (default gemini-3-flash-preview).
Test mode: set SOTTO_LLM_STUB=/path/to/response.json to bypass the network and return that file.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

# ─── COMPAT SURFACE — do not remove ───────────────────────────────────────────────────────────────
# The 2,400-line monolith was split into focused modules under _shared/lib/. External scripts still
# do `import compose_brief as cb` and reach these helpers as `cb.<name>` (ledger_io, correlate_signals,
# relationship_pulse, retune_scan/retune_apply, loops_query, proactive_scan, prewarm_graph,
# research_attendees, triage_queue, brief_marker, compose_followup, meeting-prep, select_attendees).
# These re-imports keep every moved helper resolvable at its OLD location. test_compat_surface.py
# locks this list — do NOT drop an alias without updating that test.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from textutil import (  # noqa: E402,F401
    _arr, _obj, _s, _digits, normalize_phone_for_comparison, _format_phone_for_display,
    _normalize_identifier, _looks_like_phone_number, _normalize_name_key, _names_match,
    _is_likely_automated, _HOSTING_DOMAINS, _CONSUMER_DOMAINS, _is_excluded_domain,
    _base_domain, _sender_addr, _extract_sender_name,
)
from timeutil import (  # noqa: E402,F401
    _date_only, _parse_ts, _tz_offset_minutes, _env_tz, _settings_path, load_settings,
    configured_tz, _resolve_tz, _now_local, _user_tz_offset, _user_local_date, _time_frame,
)
from gemini import (  # noqa: E402,F401
    _diag, _gemini_once, _is_retryable,
)
import metrics  # noqa: E402  (cost/latency observability — best-effort, never blocks a brief)
from render_local import (  # noqa: E402,F401
    DEFERRED_UNREAD_PROMPT_CAP, EMAIL_BODY_MAX, build_contact_lookup, resolve_imessage_name,
    resolve_whatsapp_name, resolve_call_name, build_canonical_resolver, _build_connected_dict,
    _process_missed_calls, _process_wa_missed_calls, _process_recent_calls,
    resolve_contact_names, _norm_escalation_tone, _action_age, _SYSTEM_MESSAGE_PATTERNS,
    _ASK_OR_COMMITMENT_PATTERN, _SHORT_ACK_MAX_CHARS, _is_system_message,
    _compute_last_unreplied_ask, _group_messages_into_threads, _thread_last_is_user,
    _thread_needs_response, _thread_is_known_person, _format_thread_as_text,
    _format_threads_as_text, _is_sent_email, _trim_email, _format_emails, _format_calendar,
    MAX_ATTENDEES_TO_RESEARCH, RESEARCH_HORIZON_HOURS, _LOW_QUALITY_PROFILE_PHRASES,
    _is_high_quality_profile, _known_identities, _format_attendee_research, _format_reminders,
    _format_birthdays, _format_missed_calls, _format_recent_calls, _stale_local_note,
    _format_source_availability, _format_deferred_unread, _format_stale_threads,
    _format_past_commitments, _format_action_ledger, _format_attention_queue,
    _format_relationship_insights, _format_knowledge_section, _format_contact_notes,
    _format_apple_notes, _format_granola_meetings, _format_top_browsing_domains,
    _format_search_queries, _format_screen_time, _format_recent_files, _format_meeting_archive,
    _format_reconciliation, _format_signal_scores, _format_granola_context,
    _format_file_matches, _format_domain_research, _format_escalation_signals,
)

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "morning-brief", "references", "extraction-prompt.md")

# How long an offline-Bridge local snapshot stays usable. The cache is a BACKUP, not the default
# path — a live read_local always wins. Past this, we'd rather brief with no local than re-surface
# day(s)-old "needs reply" threads as if they're fresh, so an expired snapshot is dropped.
LOCAL_SNAPSHOT_TTL_HOURS = 24


def _load_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()




def select_attendees_for_research(inputs: dict) -> list:
    """Deterministically pick the external attendees of upcoming meetings who warrant research.
    Mirrors the Mac backend: within RESEARCH_HORIZON_HOURS, an attendee needs research unless they
    are the user, share the user's email domain, or are already a known contact / in the graph.
    Returns [{name, email, meeting_title, meeting_start}], deduped by email, capped at the max."""
    google = _obj(inputs, "google")
    local = resolve_contact_names(_obj(inputs, "local"))
    events = _arr(google, "events")
    user_email = _s(google.get("userEmail")).lower()
    user_domain = user_email.split("@")[1] if "@" in user_email else ""
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




def _coverage_line(local: dict, sa: dict, events, emails) -> str:
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
    return note.strip()




def _first_run_note(inputs: dict, local: dict, sa: dict, events, emails) -> str:
    if not _is_first_run(inputs, local):
        return ""
    coverage = _coverage_line(local, sa, events, emails)
    return (
        "## FIRST BRIEF (one-time onboarding — overrides the no-intro rule, JUST for this brief)\n"
        "This is the user's very first Sotto brief. Open with ONE short, warm sentence introducing "
        "yourself as Sotto and what you do, then deliver the normal brief. After it, add ONE line on "
        "what they can ask next — e.g. \"reply to a message for you\", \"tell you about someone you're "
        "meeting\", or \"what you're waiting on\". Keep each to a single sentence; never repeat this on "
        "later briefs."
        + (f" Also include this coverage note once: \"{coverage}\"" if coverage else "")
        + "\n\n")




# ---------------------------------------------------------------------------
# Pre-computed signal renderers (from the pipeline; passed in `inputs["signals"]`)
# ---------------------------------------------------------------------------

def _sig(inputs, key):
    return _arr(_obj(inputs, "signals"), key)




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
        contact_map.setdefault(name.lower().strip(), []).append({"channel": channel, "timestamp": _s(ts), "_dt": d})

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
        results.append({"name": key, "narrative": narrative,
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




def _unwrap_local(obj):
    """Tolerate the agent dumping a RAW MCP tool-result wrapper into --local instead of clean LocalData,
    so it never needs a brittle `python3 -c` to unwrap (which also trips the dangerous-command gate and
    silently kills headless/cron briefs). Peels {result}/{structuredContent}/{content:[{text}]} until it
    finds the LocalData object."""
    for _ in range(5):
        if not isinstance(obj, dict):
            return {}
        if any(k in obj for k in ("imessage", "source_status", "generated_at", "whatsapp", "contacts")):
            return obj  # already LocalData
        if isinstance(obj.get("structuredContent"), dict):
            obj = obj["structuredContent"]
        elif isinstance(obj.get("result"), (dict, list)):
            obj = obj["result"]
        elif isinstance(obj.get("content"), list) and obj["content"] and isinstance(obj["content"][0], dict) and obj["content"][0].get("text"):
            try:
                obj = json.loads(obj["content"][0]["text"])
            except Exception:
                return {}
        else:
            return obj
    return obj if isinstance(obj, dict) else {}




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
    return local




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

    # Timezone priority: explicit userTimezone → SOTTO_TIMEZONE env (the authoritative IANA zone set
    # on Railway, DST-correct) → an offset sniffed from a calendar event. The env fallback is what
    # keeps headless cron briefs on the user's local day instead of UTC (the off-by-one date bug).
    tz = _s(google.get("userTimezone")) or configured_tz() or _user_tz_offset(events)
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

    # Cross-channel escalation: compute it (port of detectEscalation) if the caller didn't supply it.
    escalation = _sig(inputs, "escalation_signals") or _detect_escalation(local, trimmed_emails)
    # Context-signal correlation — compute it HERE (deterministic, always runs) rather than relying on
    # a separate step the agent skips. This is what connects "you researched their company / downloaded
    # their deck / met them last week" to the actual email senders in the brief.
    corr = _correlate_signals(local, trimmed_emails, _arr(local, "granola_meetings"))

    cross_source = _build_cross_source_index(im_needs, im_handled, wa_needs, wa_handled,
                                             trimmed_emails, events, missed)

    def opt(text):
        return (text + "\n") if text else ""

    fields = {
        "brief_type": brief_type,
        "user_today": user_today,
        "time_frame": time_frame,
        "source_availability": _stale_local_note(local) + _format_source_availability(sa),
        "first_run_note": _first_run_note(inputs, local, sa, events, trimmed_emails),
        "user_preferences": opt(_format_user_preferences(prefs)),
        "signal_scores": _format_signal_scores(_sig(inputs, "signal_scores") or corr["signal_scores"]),
        "granola_context": _format_granola_context(_sig(inputs, "granola_context") or corr["granola_context"]),
        "file_matches": _format_file_matches(_sig(inputs, "file_matches") or corr["file_matches"]),
        "domain_research_matches": _format_domain_research(_sig(inputs, "signal_boosts") or corr["signal_boosts"], local),
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
        "knowledge_section": opt(_format_knowledge_section(local)),
        "action_ledger": opt(_format_action_ledger(local)),
        "attention_queue": opt(_format_attention_queue(local)),
        "relationship_insights": opt(_format_relationship_insights(local)),
        "reconciliation": opt(_format_reconciliation(local, brief_type)),
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




def call_gemini(prompt: str, inputs: dict) -> str:
    """Structured Gemini call, returns the model's JSON text. Honors SOTTO_LLM_STUB for tests.
    OPTIONAL fallback: if the user supplies a backup 1M-context model and/or a second API key
    (SOTTO_FALLBACK_MODEL / SOTTO_FALLBACK_API_KEY), a 429/5xx/timeout on the primary retries on the
    backup — so a Gemini quota blip (the 429 storm we hit) no longer fails the whole brief. The
    backup MUST be 1M-context: the brief prompt runs 100K–140K chars. A second key alone (same model,
    different project) is enough to dodge per-project quota; a different model covers a model outage."""
    # Cost/latency: tag the phase of the coming call from the inputs sentinel the critic/revise pass
    # sets (default extraction), so _gemini_once records under the right phase. Best-effort.
    phase = ("critic" if (isinstance(inputs, dict) and inputs.get("_critic"))
             else "revise" if (isinstance(inputs, dict) and inputs.get("_revise"))
             else "extraction")
    try:
        metrics.set_phase(phase)
    except Exception:  # noqa: BLE001
        pass
    stub = os.environ.get("SOTTO_LLM_STUB")
    if stub:
        import time as _time
        t0 = _time.monotonic()
        with open(stub, encoding="utf-8") as f:
            content = f.read()
        try:                                   # stub: real wall, tokens 0, unpriced model → est n/a
            metrics.record(phase, _time.monotonic() - t0, 0, 0, "")
        except Exception:  # noqa: BLE001
            pass
        return content
    key = os.environ.get("GOOGLE_AI_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_AI_API_KEY not set (or use SOTTO_LLM_STUB for offline)")
    model = os.environ.get("SOTTO_GEMINI_MODEL", "gemini-3-flash-preview")
    fb_model = (os.environ.get("SOTTO_FALLBACK_MODEL") or "").strip()
    fb_key = (os.environ.get("SOTTO_FALLBACK_API_KEY") or "").strip()
    try:
        return _gemini_once(model, key, prompt)
    except Exception as e:  # noqa: BLE001
        if (fb_model or fb_key) and _is_retryable(e):
            _diag(f"[compose_brief] primary {model} failed ({type(e).__name__}) — falling back to "
                  f"{fb_model or model}{' (backup key)' if fb_key else ''}")
            return _gemini_once(fb_model or model, fb_key or key, prompt, label=" [fallback]")
        raise




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



CRITIC_SYSTEM = """You are a brief quality critic. Compare a generated communication brief against the raw data manifest and identify errors, omissions, and misattributions. Be strict but fair.

You will receive: (1) a DATA MANIFEST — a compact summary of all raw data available to generate the brief; (2) the GENERATED BRIEF markdown; (3) the ACTION ITEMS extracted alongside it.

Check for these issues:
- MISSED THREADS: Important email/message threads in the manifest the brief doesn't mention at all. Only flag genuinely important ones (not newsletters, automated notifications, or marketing).
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




def run_critic(brief_markdown: str, actions: list, manifest: dict, llm=call_gemini) -> dict:
    """Port of brief-critic.ts runCritic. Returns {patches, score, summary}; on any failure returns
    an empty (passing) result so the critic never blocks delivery."""
    actions_summary = [{"id": a.get("id"), "type": a.get("type") or a.get("action_type"),
                        "channel": a.get("channel"),
                        "contact": a.get("contactName") or a.get("contact_name") or "(unknown)",
                        "context": _s(a.get("contextSummary") or a.get("summary"))[:100]}
                       for a in (actions or [])[:30]]
    user_prompt = (CRITIC_SYSTEM + "\n\n## DATA MANIFEST\n" + json.dumps(manifest, indent=2)
                   + "\n\n## GENERATED BRIEF\n" + _s(brief_markdown)
                   + f"\n\n## ACTION ITEMS ({len(actions or [])} total)\n" + json.dumps(actions_summary, indent=2)
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

Return JSON: {"brief_markdown":"<corrected brief>","actions":<the same actions[], with any added/fixed/removed per the patches>}."""




def critique_and_revise(out: dict, inputs: dict, llm=call_gemini) -> dict:
    """Run the critic; if it finds critical/moderate issues, revise the brief to fix them. Best-effort:
    any failure returns the original brief unchanged. Stamps out['_critic'] for observability."""
    try:
        manifest = build_data_manifest(inputs)
        critic = run_critic(out.get("brief_markdown", ""), out.get("actions", []), manifest, llm)
        actionable = [p for p in critic["patches"] if p["severity"] in ("critical", "moderate")]
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
    default_cal = _s(google.get("userEmail")) or _s(os.environ.get("SOTTO_USER_EMAIL"))
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
    """Build a CHAT-tappable link for an action (port of actionSchemas.tsx buildUrl, but using
    web/universal schemes that render as tappable links in WhatsApp/Telegram/SMS — wa.me, mailto:,
    tel:, sms:, the Gmail web URL, or the meeting link — not the Mac app's imessage://). Returns ''
    when there's no routable identifier."""
    import urllib.parse as _u
    ch = _s(action.get("channel")).lower()
    a_type = _s(action.get("type") or action.get("action_type")).lower()
    ident = _s(action.get("contactIdentifier") or action.get("contact_identifier"))
    # WhatsApp/group JIDs contain '@' (…@s.whatsapp.net, …@g.us, …@lid, …@c.us) but are NOT emails —
    # treating them as email is how a WhatsApp action wrongly got a mailto: link. Strip the JID to its
    # phone, and only count a '@' as email when it is NOT a JID.
    is_jid = any(ident.endswith(s) for s in ("@s.whatsapp.net", "@g.us", "@lid", "@c.us"))
    phone_digits = re.sub(r"\D", "", ident.split("@")[0] if is_jid else ident)
    email = _s(action.get("emailReplyTo")) or (ident if ("@" in ident and not is_jid) else "")

    def _mailto():
        subj = _s(action.get("emailSubject"))
        q = ("?subject=" + _u.quote("Re: " + subj)) if subj else ""
        return f"mailto:{email}{q}"

    # Channel is AUTHORITATIVE — a message action never routes to mailto just because its id has '@'.
    if ch in ("email", "gmail", "apple_mail"):
        if email:
            return _mailto()
        tid = _s(action.get("emailThreadId"))
        return f"https://mail.google.com/mail/u/0/#inbox/{tid}" if tid else ""
    if ch in ("whatsapp", "whatsapp_call"):
        return f"https://wa.me/{phone_digits}" if len(phone_digits) >= 7 else ""
    if ch in ("phone",) or a_type == "call_back":
        return f"tel:+{phone_digits}" if len(phone_digits) >= 7 else ""
    if ch in ("imessage", "sms"):
        # Routable guard (port of actionSchemas isRoutableIdentifier): only a real phone (>=7 digits)
        # gets an sms: link. NEVER fall back to sms:<ident> — that's how name slugs ("arnav_sahu") and
        # group ids ("group_jake_ts") leaked as fake deep links. No phone → no link.
        return f"sms:+{phone_digits}" if len(phone_digits) >= 7 else ""
    if ch in ("calendar",) or a_type in ("meeting_prep", "meeting_info"):
        # Prefer a link on the action; else resolve the event id (carried in contactIdentifier or
        # eventId) back to the gathered event's meeting/html link. This is what makes calendar actions
        # one-tap — the LLM usually emits the event id but not the link.
        link = _s(action.get("meetingLink"))
        if link:
            return link
        eid = _s(action.get("eventId") or action.get("event_id")) or ident
        return _s((event_links or {}).get(eid))
    # No explicit channel → infer from the identifier shape (a real email, else a phone).
    if email:
        return _mailto()
    if len(phone_digits) >= 7:
        return f"sms:+{phone_digits}"
    return ""




_MARKER_RE = re.compile(r"<!--.*?-->", re.S)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$", re.M)
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_HRULE_RE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$", re.M)


def render_chat_text(markdown: str) -> str:
    """brief_markdown → the text actually SENT on a chat channel (WhatsApp/Telegram/SMS).

    The markdown carries Mac-app plumbing (<!--id:…--> / <!--meeting:…--> markers) and CommonMark
    formatting (## headings, **bold**) that chat channels show as literal clutter — the exact
    'Sarah Chen<!--id:…|ch:email-->' mess. Delivery instructions used to tell the AGENT
    to sed the markers out; agents skip instructions, so this is now deterministic:
      - every <!--…--> marker is stripped (id, ch, meeting — all of them);
      - '## Heading' → '*Heading*' (WhatsApp bold);
      - '**bold**' → '*bold*' (WhatsApp bold syntax is single asterisks);
      - horizontal rules dropped; runs of blank lines collapsed.
    brief_markdown stays untouched in the output for records/critic/actions."""
    text = _s(markdown)
    text = _MARKER_RE.sub("", text)
    text = _HEADING_RE.sub(lambda m: f"*{m.group(1)}*", text)
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _HRULE_RE.sub("", text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.M)     # trailing space the marker strip leaves
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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


def compose(inputs: dict, llm=call_gemini, critic: bool = False) -> dict:
    """Run the FULL FLEX extraction. `llm(prompt, inputs)` receives the fully-rendered prompt
    plus the original inputs dict (so a host model / test stub still sees the structured payload).
    With critic=True, the second-pass critic+revise quality gate runs per SOTTO_CRITIC
    ("auto" default / "always" / "off" — see _critic_decision); critic=False never runs it."""
    try:
        metrics.start_run()                                # cost/latency accumulator for THIS run
    except Exception:  # noqa: BLE001
        pass
    template = _load_prompt()
    prompt = build_prompt(template, inputs)
    raw = llm(prompt, inputs)
    out = _normalize_output(json.loads(raw))
    if critic:
        payload_chars = max(0, len(prompt) - len(template))   # the rendered source data, sans template
        run, reason = _critic_decision(_critic_mode(), payload_chars, len(out.get("actions") or []))
        _diag(f"[compose_brief] critic {'ran' if run else 'skipped'} ({reason})")
        if run:
            out = critique_and_revise(out, inputs, llm)
        else:
            out["_critic"] = {"skipped": True, "reason": reason}
    # chat-tappable wa.me/mailto:/tel:/sms: link per action; calendar actions resolve via the event map
    result = _attach_tap_links(out, _event_link_map(inputs))
    # The chat-deliverable text: markers stripped, WhatsApp-safe formatting. Deterministic here so
    # delivery never depends on the agent remembering to sed the markers out.
    result["brief_text"] = render_chat_text(result.get("brief_markdown"))
    _emit_metrics(inputs)                                  # one [brief-cost] line — success + degraded
    return result




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
    args = ap.parse_args()

    # Critic on by default for real runs; auto-off under the test stub (it can't return critic JSON).
    use_critic = not args.no_critic and not os.environ.get("SOTTO_LLM_STUB")

    using_files = any([args.local, args.gmail, args.calendar, args.granola, args.knowledge, args.attendee_research])
    if not using_files:
        # Back-compat: a single assembled inputs object from a file arg or stdin.
        raw = open(args.inputs).read() if args.inputs else sys.stdin.read()
        print(json.dumps(compose(json.loads(raw), critic=use_critic)))
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

    google = {
        "emails": pick_list(load(args.gmail, []), "emails", "messages"),
        "events": pick_list(load(args.calendar, []), "events", "items"),
    }
    if args.user_email:
        google["userEmail"] = args.user_email
    if args.user_timezone:
        google["userTimezone"] = args.user_timezone
    local = _unwrap_local(load(args.local, {}))   # accept raw read_local tool-result wrappers
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
        "attendee_research": load(args.attendee_research, []),
    }
    print(json.dumps(compose(inputs, critic=use_critic)))




if __name__ == "__main__":
    main()
