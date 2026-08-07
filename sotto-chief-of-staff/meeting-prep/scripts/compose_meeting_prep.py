#!/usr/bin/env python3
"""
compose_meeting_prep.py — assemble the calendar ahead into ONE meeting-prep message: who the
external people are, the context that matters, and concrete talking points.

PORT SOURCE: api/src/agents/registry.ts (MEETING_PREP_PROMPT) + api/src/services/claude-flex.ts
(buildMeetingResearch / performAttendeeResearch). The Mac app prepped one meeting at a time inside
worker dispatch, joining external attendees → web research → knowledge graph → past Granola notes.
This script does the SAME join for every upcoming meeting (next 72h, external attendees only — the
exact window/filter select_attendees.py uses) and renders one prep brief deterministically, then a
single Gemini call (the same model + plumbing as compose_brief.py) writes the talking points.

The agent supplies the inputs the brief skill already gathers:
  --calendar          Calendar JSON (array, or {events:[...]}/{items:[...]})         [REQUIRED]
  --local             read_local JSON (for contacts + knowledge-graph person notes)
  --attendee-research research_attendees.py results [{email,title,company,relevance,summary,
                      recent_activity,personal,conversation_hooks}] (research-prompt.md)
  --attendee-comms    gather_google.py --attendee-comms output ({email:[{date,subject,snippet,
                      from_me}]}) — the user's OWN recent email threads with each attendee. This
                      is what marries web research with private context: per attendee the context
                      block gains `thread:` lines (their real email exchange), `text:` lines
                      (iMessage/WhatsApp mined from --local via the contact_index), a `loop:`
                      line (the continuity ledger's open item with that person), and a `granola:`
                      line (most recent past meeting with THIS person — signals.ts
                      matchGranolaToCalendar's per-attendee, most-recent-only match).
  --knowledge         knowledge_query.py output ({slug: packed} or {person_knowledge:{...}})
  --granola           Granola JSON (array, or {meetings:[...]}) — past meeting history
  --user-email / --user-timezone

Prints JSON: { "prep_markdown": "...", "meetings": [ {event_id,title,start,attendees[],talking_points[]} ] }
Test mode: SOTTO_LLM_STUB=/path/to/response.json bypasses the network (same as compose_brief.py).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

_SHARED = os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "scripts")
_SHARED_LIB = os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib")
sys.path.insert(0, _SHARED)
if _SHARED_LIB not in sys.path:
    sys.path.insert(0, _SHARED_LIB)

import compose_brief as cb  # noqa: E402
import ledger_io  # noqa: E402  (continuity ledger read — per-attendee open loops)
import research_attendees as ra  # noqa: E402  (is_filler_point — shared anti-fabrication filter)
from chatfmt import to_chat  # noqa: E402

_HERE = os.path.dirname(__file__)
PROMPT_PATH = os.path.join(_HERE, "..", "references", "meeting-prep-prompt.md")

# Private-context caps per attendee (token budget: research lines already run ~6-8 per person).
MAX_THREAD_LINES = 3     # email exchanges from --attendee-comms
MAX_TEXT_LINES = 2       # iMessage/WhatsApp messages mined from --local
MAX_PRIVATE_LINES = 6    # hard cap on thread: + text: + loop: lines combined


def _load_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _knowledge_lookup(prior_knowledge: dict) -> tuple[dict, list]:
    """Index packed knowledge strings by email and by lowercased name so attendees can be matched.
    Accepts {person_knowledge:{slug:packed}} or a bare {slug:packed} map (knowledge_query.py emits
    the bare form). Packed head line is "Name (slug) | role @ company | email"."""
    people = {}
    if isinstance(prior_knowledge, dict):
        pk = prior_knowledge.get("person_knowledge")
        people = pk if isinstance(pk, dict) else prior_knowledge
    by_email, by_name = {}, []
    for packed in (people or {}).values():
        head = cb._s(packed).split("\n", 1)[0]
        name = head.split("(")[0].strip()
        m = re.search(r"\|\s*([^\s|]+@[^\s|]+)", head)
        email = m.group(1).lower().strip() if m else ""
        if email:
            by_email[email] = packed
        if name:
            by_name.append((name, packed))
    return by_email, by_name


def _granola_for_emails(granola_meetings: list, emails: set) -> list:
    """Past meetings whose attendees overlap this meeting's external attendees (port of the
    get_granola_notes join in MEETING_PREP_PROMPT)."""
    hits = []
    for m in granola_meetings:
        ae = {cb._s(e).lower().strip() for e in (m.get("attendee_emails") or [])}
        if ae & emails:
            # Prefer the full TRANSCRIPT (richer — carries what was decided/committed) when the skill
            # fetched it; fall back to the AI summary / your notes. Larger cap for transcripts.
            transcript = cb._s(m.get("transcript"))
            body = transcript[:3500] if transcript else cb._s(m.get("ai_summary") or m.get("your_notes"))[:1200]
            if body:
                src = "transcript" if transcript else "notes"
                hits.append(f"- {cb._s(m.get('title'))} ({cb._s(m.get('date'))}) [{src}]: {body}")
    return hits[:3]


def _external_attendees(event: dict, user_email: str, user_domain: str) -> list:
    """All EXTERNAL attendees of a meeting (port of the meeting-prep set: every attendee except the
    user and same-domain colleagues). Unlike select_attendees_for_research this keeps KNOWN people
    too — prep wants them, attached to their knowledge-graph context. Deduped by email."""
    out, seen = [], set()
    for a in cb._arr(event, "attendees"):
        email = cb._s(a.get("email")).lower().strip()
        if not email or email in seen:
            continue
        if email == user_email:
            continue
        if user_domain and email.endswith("@" + user_domain):
            continue
        seen.add(email)
        out.append({"name": cb._s(a.get("displayName")) or email.split("@")[0], "email": email})
    return out


def _comms_lookup(inputs: dict) -> dict:
    """{email(lower): [{date,subject,snippet,from_me}]} from the --attendee-comms file
    (gather_google.py --attendee-comms output). Tolerates a missing/malformed file → {}."""
    out = {}
    for em, rows in (cb._obj(inputs, "attendee_comms") or {}).items():
        if isinstance(em, str) and "@" in em and isinstance(rows, list):
            out[em.lower().strip()] = [r for r in rows if isinstance(r, dict)]
    return out


def _contact_identity(email: str, name: str, contact_index: list):
    """The contact_index entry (knowledge_query's identity map: {canonical_id, display_name,
    identifiers[]}) for this attendee. Conservative matching: an EXACT email hit in identifiers
    wins; otherwise a display-name match counts ONLY when exactly one entry matches (a shared
    first name is not the same person). None when unmatched — callers degrade silently."""
    email = cb._s(email).lower().strip()
    for entry in contact_index:
        if any(cb._s(i).strip().lower() == email for i in cb._arr(entry, "identifiers")):
            return entry
    named = [e for e in contact_index if cb._names_match(cb._s(name), cb._s(e.get("display_name")))]
    return named[0] if len(named) == 1 else None


def _identity_keys(entry) -> set:
    """Normalized identifier keys (emails lowercased, phones → last-10 digits) for one
    contact_index entry — the email→phone bridge that lets an attendee's email match their
    iMessage/WhatsApp handle."""
    keys = set()
    for i in cb._arr(entry or {}, "identifiers"):
        k = cb._normalize_identifier(cb._s(i))
        if k:
            keys.add(k)
    return keys


def _thread_lines(rows: list) -> list:
    """`thread:` context lines (≤MAX_THREAD_LINES) from the attendee's Gmail exchange with the
    user — email <date> "<subject>" (you→them|them→you): <snippet≤140>."""
    lines = []
    for row in rows[:MAX_THREAD_LINES]:
        direction = "you→them" if row.get("from_me") else "them→you"
        date = cb._s(row.get("date")).strip()[:31]
        subject = cb._s(row.get("subject")).strip()[:80]
        snippet = cb._s(row.get("snippet")).strip()[:140]
        line = "    thread: email"
        if date:
            line += f" {date}"
        if subject:
            line += f' "{subject}"'
        line += f" ({direction})"
        if snippet:
            line += f": {snippet}"
        lines.append(line)
    return lines


def _text_lines(local: dict, identity) -> list:
    """`text:` context lines (≤MAX_TEXT_LINES): the attendee's most recent 1:1 iMessage/WhatsApp
    messages, matched via the identity's normalized identifiers ONLY (exact email/phone — never a
    guessed name). Group chats and @lid JIDs are skipped; no identity match → [] (silent)."""
    keys = _identity_keys(identity)
    if not keys:
        return []
    hits = []
    for channel, ident_key in (("imessage", "handle"), ("whatsapp", "contact_jid")):
        for m in cb._arr(local, channel):
            ident = cb._s(m.get(ident_key))
            if (not ident or ident.endswith("@lid") or m.get("is_group_chat")
                    or cb._s(m.get("chat_guid"))):
                continue
            if cb._normalize_identifier(ident) not in keys:
                continue
            text = cb._s(m.get("text")).strip()
            if text:
                hits.append((cb._s(m.get("timestamp")), channel, bool(m.get("is_from_me")), text))
    hits.sort(key=lambda h: h[0], reverse=True)
    lines = []
    for ts, channel, from_me, text in hits[:MAX_TEXT_LINES]:
        direction = "you→them" if from_me else "them→you"
        date = ts.split("T")[0] if ts else ""
        lines.append(f"    text: {channel}" + (f" {date}" if date else "")
                     + f" ({direction}): {text[:140]}")
    return lines


def _granola_line(email: str, granola_meetings: list) -> str:
    """`granola:` context line — THE MOST RECENT past meeting whose attendees include this
    attendee (port of signals.ts matchGranolaToCalendar: per-person, most-recent-only,
    deterministic — not a pile of meetings for the LLM to sort out). The per-meeting
    "past meetings" block still carries the deep transcript; this line pins the freshest
    context to the PERSON."""
    email = cb._s(email).lower().strip()
    best = None
    for m in granola_meetings:
        ae = {cb._s(e).lower().strip() for e in (m.get("attendee_emails") or [])}
        if email not in ae:
            continue
        summ = cb._s(m.get("ai_summary") or m.get("your_notes")).strip()
        if not summ:
            continue
        date = cb._s(m.get("date"))
        if best is None or date > best[0]:
            best = (date, summ)
    if not best:
        return ""
    return f"    granola: last met {best[0] or '(undated)'}: {best[1][:140]}"


def _active_loops() -> list:
    """ACTIVE continuity-ledger entries (open/waiting/failed/blocked) — best-effort: an unreadable
    ledger never blocks the prep."""
    try:
        return ledger_io.load_active()
    except Exception:  # noqa: BLE001
        return []


def _loop_line(email: str, identity, name: str, loops: list) -> str:
    """One `loop:` context line — the continuity ledger's open item with this attendee. Matching
    mirrors _contact_identity's conservatism: contact_identifier must hit the attendee's normalized
    email/phone keys; a contact_name match counts only when it is unique across active loops."""
    keys = _identity_keys(identity) | {cb._normalize_identifier(cb._s(email))}
    keys.discard("")
    matches = [it for it in loops
               if cb._normalize_identifier(cb._s(it.get("contact_identifier"))) in keys]
    if not matches:
        display = cb._s((identity or {}).get("display_name")) or cb._s(name)
        named = [it for it in loops
                 if cb._s(it.get("contact_name")) and cb._names_match(display, cb._s(it.get("contact_name")))]
        matches = named if len(named) == 1 else []
    if not matches:
        return ""
    it = matches[0]
    what = (cb._s(it.get("summary") or it.get("ask"))
            or cb._s(it.get("action_type")).replace("_", " ")).strip()[:140]
    if not what:
        return ""
    status = cb._s(it.get("status")) or "open"
    line = f"    loop: {status} — {what}"
    try:
        n = int(it.get("times_surfaced") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 1:
        line += f" (surfaced {n}x)"
    return line


def build_context(inputs: dict) -> tuple[str, list]:
    """Assemble the per-meeting context block + a structured meeting list (event_id/title/start/
    attendees). Returns ("", []) when there are no upcoming external meetings."""
    google = cb._obj(inputs, "google")
    local = cb.resolve_contact_names(cb._obj(inputs, "local"))
    events = cb._arr(google, "events")
    user_email = cb._s(google.get("userEmail")).lower()
    user_domain = user_email.split("@")[1] if "@" in user_email else ""

    research_by_email = {cb._s(r.get("email")).lower(): r for r in cb._arr(inputs, "attendee_research")}
    pk = cb._obj(inputs, "prior_knowledge") or cb._obj(inputs, "knowledge")
    kg_by_email, kg_by_name = _knowledge_lookup(pk)
    granola_meetings = cb._arr(local, "granola_meetings")
    # Private-context sources: the attendee-comms gather, the identity map (knowledge_query's
    # contact_index — email→name/phone bridge), and the continuity ledger's active loops.
    comms_by_email = _comms_lookup(inputs)
    contact_index = cb._arr(pk, "contact_index") + cb._arr(local, "contact_index")
    loops = _active_loops()

    now = datetime.now(timezone.utc)
    upcoming = []
    for e in events:
        start = cb._s(e.get("start"))
        st = cb._parse_ts(start)
        if st is not None:
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            hours_away = (st - now).total_seconds() / 3600.0
            if hours_away < -1 or hours_away > cb.RESEARCH_HORIZON_HOURS:
                continue
        title = cb._s(e.get("summary"))
        attendees = _external_attendees(e, user_email, user_domain)
        if not attendees:
            continue
        upcoming.append((st or now, e, title, start, attendees))

    if not upcoming:
        return "", []

    upcoming.sort(key=lambda x: x[0])

    blocks, meetings_out = [], []
    for _st, e, title, start, attendees in upcoming:
        emails = {a["email"] for a in attendees}
        lines = [f"### {title} — {start}"]
        if e.get("meetingLink"):
            lines.append(f"link: {e.get('meetingLink')}")
        if e.get("location"):
            lines.append(f"location: {cb._s(e.get('location'))}")
        att_struct = []
        for a in attendees:
            email = a["email"]
            name = a["name"]
            r = research_by_email.get(email)
            role = cb._s(r.get("title")) if r else ""
            company = cb._s(r.get("company")) if r else ""
            att_struct.append({"name": name, "role": role or None, "company": company or None})
            hdr = f"- {name} <{email}>"
            if role and company:
                hdr += f" — {role} at {company}"
            elif company:
                hdr += f" — {company}"
            lines.append(hdr)
            if r:
                summ = cb._s(r.get("summary")).strip()
                if summ and summ.lower() != "no public profile found.":
                    lines.append(f"    research: {summ}")
                comp = cb._s(r.get("company_summary")).strip()
                if comp:   # person → company fallback (corporate domains research the company
                    lines.append(f"    company: {comp}")   # even when the person has no profile)
                for b in cb._arr(r, "relevance"):
                    b = cb._s(b).strip()
                    if b:
                        lines.append(f"    · {b}")
                # Recency-sweep fields (research_attendees.py Pass B) — dated, source-URLed,
                # already post-filtered there; the freshest material for talking points.
                for it in cb._arr(r, "recent_activity"):
                    if not isinstance(it, dict):
                        continue
                    what = cb._s(it.get("what")).strip()
                    url = cb._s(it.get("source_url")).strip()
                    when = cb._s(it.get("when")).strip()
                    if what and url:
                        lines.append("    recent: " + what + (f" ({when})" if when else "") + f" — {url}")
                for p in cb._arr(r, "personal"):
                    p = cb._s(p).strip()
                    if p:
                        lines.append(f"    personal: {p}")
                for h in cb._arr(r, "conversation_hooks"):
                    h = cb._s(h).strip()
                    if h:
                        lines.append(f"    hook: {h}")
            # Knowledge graph: what the user already knows about this person.
            packed = kg_by_email.get(email) or next(
                (pk for nm, pk in kg_by_name if cb._names_match(name, nm)), None)
            if packed:
                lines.append("    known: " + cb._s(packed).replace("\n", " | "))
            if not r and not packed:
                lines.append("    (no public profile or prior knowledge found)")
            # Private context — the user's OWN relationship with this attendee: recent email
            # threads (attendee-comms gather), the continuity ledger's open loop, the most
            # recent Granola meeting with THIS person, and 1:1 texts (matched via contact_index
            # — exact identifiers only). Each source degrades silently; the combined block is
            # hard-capped (highest-signal lines first) so prep context stays budgeted.
            private = _thread_lines(comms_by_email.get(email) or [])
            identity = _contact_identity(email, name, contact_index)
            loop = _loop_line(email, identity, name, loops)
            if loop:
                private.append(loop)
            granola = _granola_line(email, granola_meetings)
            if granola:
                private.append(granola)
            private += _text_lines(local, identity)
            lines.extend(private[:MAX_PRIVATE_LINES])
        past = _granola_for_emails(granola_meetings, emails)
        if past:
            lines.append("  past meetings:")
            lines.extend("  " + p for p in past)
        blocks.append("\n".join(lines))
        meetings_out.append({"event_id": cb._s(e.get("id")), "title": title,
                             "start": start, "attendees": att_struct, "talking_points": []})

    return "\n\n".join(blocks), meetings_out


def build_prompt(template: str, inputs: dict) -> tuple[str, list]:
    google = cb._obj(inputs, "google")
    events = cb._arr(google, "events")
    tz = cb._s(google.get("userTimezone")) or cb._user_tz_offset(events)
    context, meetings = build_context(inputs)
    fields = {
        "meetings_context": context or "(no upcoming meetings with external attendees)",
        "user_timezone": tz or "(unknown)",
        "user_today": cb._user_local_date(tz),
    }
    rendered = re.sub(r"\{\{(\w+)\}\}", lambda m: fields.get(m.group(1), m.group(0)), template)
    return rendered, meetings


def _normalize(parsed: dict, meetings: list) -> dict:
    out = dict(parsed) if isinstance(parsed, dict) else {}
    if "prep_markdown" not in out and "markdown" in out:
        out["prep_markdown"] = out.get("markdown")
    out.setdefault("prep_markdown", "")
    if not isinstance(out.get("meetings"), list) or not out["meetings"]:
        out["meetings"] = meetings
    # Anti-fabrication (deterministic, not just the prompt): drop generic-process talking points —
    # "ask for introductions", "understand the agenda", "build rapport" — an EMPTY list is the
    # correct output when research found nothing concrete.
    for m in out["meetings"]:
        if isinstance(m, dict) and isinstance(m.get("talking_points"), list):
            m["talking_points"] = [t for t in m["talking_points"]
                                   if isinstance(t, str) and not ra.is_filler_point(t)]
    return out


def compose(inputs: dict, llm=None) -> dict:
    """prep_markdown is chat-ready on the way out (one formatting pipeline — Sprint 0 §3): the
    LLM's markdown (## / **bold** / any markers) goes through chatfmt.to_chat so the skill's
    verbatim delivery never leaks raw CommonMark into WhatsApp. JSON keys are unchanged."""
    llm = llm or cb.call_gemini
    prompt, meetings = build_prompt(_load_prompt(), inputs)
    if not meetings:
        # Nothing to prep — skip the model call entirely.
        return {"prep_markdown": "No meetings with outside people in the next 3 days — "
                "your calendar's internal.", "meetings": []}
    raw = llm(prompt, inputs)
    out = _normalize(json.loads(raw), meetings)
    out["prep_markdown"] = to_chat(out.get("prep_markdown"))
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Assemble the calendar ahead into one meeting-prep message.")
    ap.add_argument("inputs", nargs="?", help="a single assembled inputs JSON file (back-compat; or stdin)")
    ap.add_argument("--calendar", help="Calendar JSON: an array, or {events:[...]}/{items:[...]}")
    ap.add_argument("--local", help="read_local output JSON (contacts + granola + knowledge-graph notes)")
    ap.add_argument("--attendee-research", dest="attendee_research", help="attendee research JSON array")
    ap.add_argument("--attendee-comms", dest="attendee_comms",
                    help="per-attendee Gmail threads JSON ({email:[{date,subject,snippet,from_me}]}"
                         " — gather_google.py --attendee-comms output)")
    ap.add_argument("--knowledge", help="prior knowledge JSON (knowledge_query.py output)")
    ap.add_argument("--granola", help="Granola JSON: an array, or {meetings:[...]}")
    ap.add_argument("--user-email", dest="user_email")
    ap.add_argument("--user-timezone", dest="user_timezone")
    args = ap.parse_args()

    using_files = any([args.calendar, args.local, args.attendee_research, args.attendee_comms,
                       args.knowledge, args.granola])
    if not using_files:
        raw = open(args.inputs).read() if args.inputs else sys.stdin.read()
        print(json.dumps(compose(json.loads(raw) if raw.strip() else {})))
        return

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

    google = {"events": pick_list(load(args.calendar, []), "events", "items")}
    if args.user_email:
        google["userEmail"] = args.user_email
    if args.user_timezone:
        google["userTimezone"] = args.user_timezone
    local = cb._unwrap_local(load(args.local, {}))   # accept raw read_local tool-result wrappers
    granola = load(args.granola, None)
    if granola is not None and not cb._arr(local, "granola_meetings"):
        local = dict(local)
        local["granola_meetings"] = pick_list(granola, "meetings", "items")
    inputs = {
        "google": google,
        "local": local,
        "attendee_research": load(args.attendee_research, []),
        "attendee_comms": load(args.attendee_comms, {}),
        "prior_knowledge": load(args.knowledge, {}),
    }
    print(json.dumps(compose(inputs)))


if __name__ == "__main__":
    main()
