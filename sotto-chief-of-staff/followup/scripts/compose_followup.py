#!/usr/bin/env python3
"""
compose_followup.py — turn the meetings that JUST ended into commitments + ready-to-send follow-up drafts.

PORT SOURCE: api/src/agents/registry.ts (worker-dispatch drafting/work-log) + continuity.ts. Mirrors
compose_meeting_prep.py's shape exactly, but looks BACKWARD (meetings that ended in the last ~36h, with a
Granola transcript) instead of forward. Deterministic assembly here; one Gemini call (same plumbing as
compose_brief.py) writes the follow-up. Drafts only — never sends.

Inputs (the skill already gathers these):
  --granola      Granola JSON (array or {meetings:[...]}) — REQUIRED (needs transcripts)
  --local        read_local JSON (contacts, for name/email resolution)         [optional]
  --calendar     Calendar JSON — to match a meeting's attendees/emails          [optional]
  --user-email / --user-timezone
  --since-hours  how far back to look for ended meetings (default 36)

Prints JSON: { followup_markdown, commitments[], drafts[] }
Test mode: SOTTO_LLM_STUB=/path/to/response.json bypasses the network (same as compose_brief.py).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import timezone, timedelta

_SHARED = os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "scripts")
sys.path.insert(0, _SHARED)
import compose_brief as cb  # noqa: E402

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "references", "followup-prompt.md")


def _load_prompt() -> str:
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def _recent_ended(meetings: list, since_hours: int, now) -> list:
    """Meetings whose date is within the last `since_hours` (and not in the future) AND have a transcript
    or notes — those are the ones worth a follow-up."""
    out = []
    for m in meetings:
        if not isinstance(m, dict):
            continue
        body = cb._s(m.get("transcript")) or cb._s(m.get("ai_summary") or m.get("your_notes"))
        if not body:
            continue
        ts = cb._parse_ts(cb._s(m.get("date") or m.get("start") or m.get("created_at")))
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            hours_ago = (now - ts.astimezone(timezone.utc)).total_seconds() / 3600.0
            if hours_ago < -1 or hours_ago > since_hours:   # future, or too old
                continue
        out.append(m)
    return out


def build_context(inputs: dict, since_hours: int) -> tuple[str, list]:
    local = cb.resolve_contact_names(cb._obj(inputs, "local"))
    granola = cb._arr(inputs, "granola") or cb._arr(local, "granola_meetings")
    now = cb._now_local("+00:00").astimezone(timezone.utc)
    ended = _recent_ended(granola, since_hours, now)
    if not ended:
        return "", []
    blocks = []
    for m in ended:
        title = cb._s(m.get("title")) or "Meeting"
        when = cb._s(m.get("date") or m.get("start"))
        emails = [cb._s(e) for e in (m.get("attendee_emails") or []) if cb._s(e)]
        transcript = cb._s(m.get("transcript"))
        body = transcript[:6000] if transcript else cb._s(m.get("ai_summary") or m.get("your_notes"))[:1500]
        src = "transcript" if transcript else "notes"
        lines = [f"### {title} — {when}"]
        if emails:
            lines.append("attendees: " + ", ".join(emails))
        lines.append(f"[{src}]: {body}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), ended


def build_prompt(template: str, inputs: dict, since_hours: int) -> tuple[str, list]:
    google = cb._obj(inputs, "google")
    events = cb._arr(google, "events")
    tz = cb._s(inputs.get("user_timezone")) or cb._s(google.get("userTimezone")) or cb.configured_tz() or cb._user_tz_offset(events)
    context, ended = build_context(inputs, since_hours)
    fields = {
        "meetings_context": context or "(no recently-ended meetings with notes)",
        "user_email": cb._s(inputs.get("user_email")) or cb._s(google.get("userEmail")) or "(unknown)",
        "user_timezone": tz or "(unknown)",
        "user_today": cb._user_local_date(tz),
    }
    rendered = re.sub(r"\{\{(\w+)\}\}", lambda m: fields.get(m.group(1), m.group(0)), template)
    return rendered, ended


def _normalize(parsed: dict) -> dict:
    out = dict(parsed) if isinstance(parsed, dict) else {}
    if "followup_markdown" not in out and "markdown" in out:
        out["followup_markdown"] = out.get("markdown")
    out.setdefault("followup_markdown", "")
    out.setdefault("commitments", [])
    out.setdefault("drafts", [])
    return out


def compose(inputs: dict, since_hours: int = 36, llm=None) -> dict:
    llm = llm or cb.call_gemini
    prompt, ended = build_prompt(_load_prompt(), inputs, since_hours)
    if not ended:
        return {"followup_markdown": "Nothing to follow up on from your recent meetings.",
                "commitments": [], "drafts": []}
    return _normalize(json.loads(llm(prompt, inputs)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--granola")
    ap.add_argument("--local")
    ap.add_argument("--calendar")
    ap.add_argument("--user-email", dest="user_email")
    ap.add_argument("--user-timezone", dest="user_timezone")
    ap.add_argument("--since-hours", dest="since_hours", type=int, default=36)
    a = ap.parse_args()

    def load(p, d):
        if not p:
            return d
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return d

    def pick_list(v, *keys):
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k in keys:
                if isinstance(v.get(k), list):
                    return v[k]
        return []

    inputs = {
        "granola": pick_list(load(a.granola, []), "meetings", "items"),
        "local": cb._unwrap_local(load(a.local, {})),
        "google": {"events": pick_list(load(a.calendar, []), "events", "items"),
                   "userEmail": a.user_email or ""},
        "user_email": a.user_email or "",
        "user_timezone": a.user_timezone or "",
    }
    print(json.dumps(compose(inputs, a.since_hours)))


if __name__ == "__main__":
    main()
