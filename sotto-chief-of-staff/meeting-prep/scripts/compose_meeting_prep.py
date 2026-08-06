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
  --attendee-research host-web-search results [{email,title,company,relevance,summary}] (research-prompt.md)
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
sys.path.insert(0, _SHARED)

import compose_brief as cb  # noqa: E402

_HERE = os.path.dirname(__file__)
PROMPT_PATH = os.path.join(_HERE, "..", "references", "meeting-prep-prompt.md")


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


def build_context(inputs: dict) -> tuple[str, list]:
    """Assemble the per-meeting context block + a structured meeting list (event_id/title/start/
    attendees). Returns ("", []) when there are no upcoming external meetings."""
    google = cb._obj(inputs, "google")
    local = cb.resolve_contact_names(cb._obj(inputs, "local"))
    events = cb._arr(google, "events")
    user_email = cb._s(google.get("userEmail")).lower()
    user_domain = user_email.split("@")[1] if "@" in user_email else ""

    research_by_email = {cb._s(r.get("email")).lower(): r for r in cb._arr(inputs, "attendee_research")}
    kg_by_email, kg_by_name = _knowledge_lookup(cb._obj(inputs, "prior_knowledge") or cb._obj(inputs, "knowledge"))
    granola_meetings = cb._arr(local, "granola_meetings")

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
                for b in cb._arr(r, "relevance"):
                    b = cb._s(b).strip()
                    if b:
                        lines.append(f"    · {b}")
            # Knowledge graph: what the user already knows about this person.
            packed = kg_by_email.get(email) or next(
                (pk for nm, pk in kg_by_name if cb._names_match(name, nm)), None)
            if packed:
                lines.append("    known: " + cb._s(packed).replace("\n", " | "))
            if not r and not packed:
                lines.append("    (no public profile or prior knowledge found)")
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
    return out


def compose(inputs: dict, llm=None) -> dict:
    llm = llm or cb.call_gemini
    prompt, meetings = build_prompt(_load_prompt(), inputs)
    if not meetings:
        # Nothing to prep — skip the model call entirely.
        return {"prep_markdown": "No meetings with outside people in the next 3 days — "
                "your calendar's internal.", "meetings": []}
    raw = llm(prompt, inputs)
    return _normalize(json.loads(raw), meetings)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Assemble the calendar ahead into one meeting-prep message.")
    ap.add_argument("inputs", nargs="?", help="a single assembled inputs JSON file (back-compat; or stdin)")
    ap.add_argument("--calendar", help="Calendar JSON: an array, or {events:[...]}/{items:[...]}")
    ap.add_argument("--local", help="read_local output JSON (contacts + granola + knowledge-graph notes)")
    ap.add_argument("--attendee-research", dest="attendee_research", help="attendee research JSON array")
    ap.add_argument("--knowledge", help="prior knowledge JSON (knowledge_query.py output)")
    ap.add_argument("--granola", help="Granola JSON: an array, or {meetings:[...]}")
    ap.add_argument("--user-email", dest="user_email")
    ap.add_argument("--user-timezone", dest="user_timezone")
    args = ap.parse_args()

    using_files = any([args.calendar, args.local, args.attendee_research, args.knowledge, args.granola])
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
        "prior_knowledge": load(args.knowledge, {}),
    }
    print(json.dumps(compose(inputs)))


if __name__ == "__main__":
    main()
