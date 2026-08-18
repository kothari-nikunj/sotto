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

Prints JSON: { followup_markdown, commitments[], drafts[], ledger[, open_loops] }
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
_SHARED_LIB = os.path.join(os.path.dirname(__file__), "..", "..", "_shared", "lib")
sys.path.insert(0, _SHARED)
if _SHARED_LIB not in sys.path:
    sys.path.insert(0, _SHARED_LIB)

from textutil import _arr, _obj, _s, unwrap_tool_result  # noqa: E402
from timeutil import (  # noqa: E402
    _now_local, _parse_ts, _user_local_date, _user_tz_offset, configured_tz,
    configured_user_email,
)
from gemini import call_gemini  # noqa: E402
from render_local import resolve_contact_names  # noqa: E402
from chatfmt import to_chat  # noqa: E402

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "references", "followup-prompt.md")

# A transcript is included WHOLE up to this cap — ~2.5 hours of speech, so virtually every real
# meeting fits entire. The old cap was 6,000 chars (~7 minutes): it cut every meeting off before
# the part where people say what they'll do, which is the one thing this extraction exists for.
# Over the cap, the TAIL is kept — commitments cluster at the end. Budget: 150K chars ≈ 37K tokens,
# and the pipeline requires a 1M-context model precisely so meetings never get skimmed.
TRANSCRIPT_CHAR_CAP = 150_000
NOTES_CHAR_CAP = 4_000


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
        body = _s(m.get("transcript")) or _s(m.get("ai_summary") or m.get("your_notes"))
        if not body:
            continue
        # Prefer an explicit end instant. Granola often only supplies start; when end exists an
        # in-progress meeting cannot leak into commitment capture.
        stamp = _s(m.get("end") or m.get("start") or m.get("created_at"))
        if not stamp:
            date, clock = _s(m.get("date")), _s(m.get("time"))
            stamp = f"{date}T{clock}:00" if date and clock else date
        ts = _parse_ts(stamp)
        if ts is not None:
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            hours_ago = (now - ts.astimezone(timezone.utc)).total_seconds() / 3600.0
            if hours_ago < 0 or hours_ago > since_hours:    # future, or too old
                continue
        out.append(m)
    return out


def build_context(inputs: dict, since_hours: int) -> tuple[str, list]:
    local = resolve_contact_names(_obj(inputs, "local"))
    granola = _arr(inputs, "granola") or _arr(local, "granola_meetings")
    now = _now_local("+00:00").astimezone(timezone.utc)
    ended = _recent_ended(granola, since_hours, now)
    if not ended:
        return "", []
    blocks = []
    for m in ended:
        title = _s(m.get("title")) or "Meeting"
        when = _s(m.get("start") or m.get("date"))
        meeting_id = _s(m.get("meeting_id") or m.get("id"))
        emails = [_s(e) for e in (m.get("attendee_emails") or []) if _s(e)]
        lines = [f"### {title} — {when}"]
        if meeting_id:
            lines.append(f"meeting_id: {meeting_id}")
        if emails:
            lines.append("attendees: " + ", ".join(emails))
        # Distilled first — the transcript used to REPLACE the notes and summary, so the two most
        # concentrated signals (what the user themselves typed; Granola's summary, which usually
        # already lists the action items) vanished exactly when a transcript existed. All three
        # layers now render, most-distilled first; the transcript is the evidence record, not the
        # headline. The prompt's "distilled first, verified always" rule is written against these
        # exact three markers.
        notes = _s(m.get("your_notes")).strip()
        summary = _s(m.get("ai_summary")).strip()
        transcript = _s(m.get("transcript"))
        if notes:
            lines.append(f"[your notes]: {notes[:NOTES_CHAR_CAP]}")
        if summary:
            lines.append(f"[summary]: {summary[:NOTES_CHAR_CAP]}")
        if transcript:
            lines.append(f"[transcript]: {transcript[-TRANSCRIPT_CHAR_CAP:]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), ended


def _open_loops_context(inputs: dict) -> str:
    """The bounded choice set the model may reconcile against. It may select an exact anchor from
    here; apply_commitments validates that choice against the live ledger before mutating anything."""
    snapshot = inputs.get("open_loops")
    if not isinstance(snapshot, dict):
        try:
            snapshot = _query_ledger()
        except Exception:  # noqa: BLE001 — reconciliation is optional, capture is not
            snapshot = {}
    rows = []
    for direction, key in (("you_owe", "you_owe"), ("waiting_on_them", "waiting_on_them")):
        for item in (snapshot.get(key) or [])[:40]:
            if not isinstance(item, dict):
                continue
            anchor = _s(item.get("anchor_key"))
            what = _s(item.get("what") or item.get("summary") or item.get("ask"))
            if anchor and what:
                rows.append(f"- {direction} | {anchor} | {what[:300]}")
    return "\n".join(rows) or "(none)"


def build_prompt(template: str, inputs: dict, since_hours: int) -> tuple[str, list]:
    google = _obj(inputs, "google")
    events = _arr(google, "events")
    tz = _s(inputs.get("user_timezone")) or _s(google.get("userTimezone")) or configured_tz() or _user_tz_offset(events)
    context, ended = build_context(inputs, since_hours)
    fields = {
        "meetings_context": context or "(no recently-ended meetings with notes)",
        "user_name": os.environ.get("SOTTO_USER_NAME", "") or "(unknown)",
        "user_email": _s(inputs.get("user_email")) or _s(google.get("userEmail")) or "(unknown)",
        "user_timezone": tz or "(unknown)",
        "user_today": _user_local_date(tz),
        "open_loops_context": _open_loops_context(inputs),
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
    out.setdefault("procedural_candidates", [])
    # The SKILL delivers followup_markdown VERBATIM on chat channels — route it through the shared
    # chat pipeline so **bold**/<!--…--> never reach WhatsApp literally. to_chat is idempotent, so
    # the evening merge (compose_brief embeds this in its prompt, then chat-formats its own output)
    # is unaffected by the double pass.
    out["followup_markdown"] = to_chat(out["followup_markdown"])
    return out


def _commitment_module():
    """Load the sibling by path without making script execution depend on package layout."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apply_commitments.py")
    spec = importlib.util.spec_from_file_location("sotto_apply_followup_commitments", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ground_output(out: dict, ended: list, inputs: dict) -> dict:
    """Remove model claims that cannot pass the ledger's source and ownership gate."""
    google = _obj(inputs, "google")
    user_email = (_s(inputs.get("user_email")) or _s(google.get("userEmail"))
                  or configured_user_email())
    grounded, stats = _commitment_module().ground_commitments(
        out, ended, user_email, os.environ.get("SOTTO_USER_NAME", ""))
    # Kept private-looking but visible in JSON for diagnosis: silence is safe only when operators
    # can tell whether a quiet result meant "no commitments" or "claims failed grounding".
    grounded["commitment_grounding"] = stats
    return grounded


def compose(inputs: dict, since_hours: int = 36, llm=None) -> dict:
    llm = llm or call_gemini
    prompt, ended = build_prompt(_load_prompt(), inputs, since_hours)
    if not ended:
        return {"followup_markdown": "Nothing to follow up on from your recent meetings.",
                "commitments": [], "drafts": []}
    return _ground_output(_normalize(json.loads(llm(prompt, inputs))), ended, inputs)


def compose_for_brief(inputs: dict, since_hours: int = 12, llm=None) -> dict:
    """The evening-brief merge entry point (roadmap Sprint 0 #4): the SAME extraction as compose(),
    but shaped for embedding — returns {} when no recently-ended meeting has a transcript/notes (so
    the brief renders no followup block at all, instead of the human-facing 'Nothing to follow up'
    line), and NEVER delivers anything itself. compose_brief calls this guarded; any exception there
    degrades to an empty context."""
    _, ended = build_context(inputs, since_hours)
    if not ended:
        return {}
    return compose(inputs, since_hours=since_hours, llm=llm)


def _apply_ledger(out: dict, user_email: str, source_meetings: list) -> dict:
    """Persist this extraction before its on-demand result is shown.

    The evening brief already does this through compose_brief._apply_followup_commitments. The CLI
    used to leave it to a later agent step, behind an unnecessary "confirm the summary" gate; users
    naturally did not reply merely to confirm an accurate read-only summary, so correct Granola
    commitments vanished. Loading by path keeps compose_followup importable in tests and as a module.
    """
    return _commitment_module().apply(out, user_email, source_meetings=source_meetings)


def _query_ledger() -> dict:
    """Read back the canonical loop view after an on-demand backlog reconciliation."""
    import importlib.util
    path = os.path.join(_SHARED, "loops_query.py")
    spec = importlib.util.spec_from_file_location("sotto_followup_loops_query", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.query()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--granola")
    ap.add_argument("--local")
    ap.add_argument("--calendar")
    ap.add_argument("--user-email", dest="user_email")
    ap.add_argument("--user-timezone", dest="user_timezone")
    ap.add_argument("--since-hours", dest="since_hours", type=int, default=36)
    ap.add_argument("--reconcile-open-loops", action="store_true",
                    help="include a canonical post-write open-loop snapshot")
    ap.add_argument("--no-apply-commitments", action="store_true",
                    help="debug only: compose without writing the continuity ledger")
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
        "local": unwrap_tool_result(load(a.local, {})),
        "google": {"events": pick_list(load(a.calendar, []), "events", "items"),
                   "userEmail": a.user_email or ""},
        "user_email": a.user_email or "",
        "user_timezone": a.user_timezone or "",
    }
    out = compose(inputs, a.since_hours)
    if not a.no_apply_commitments:
        # No output is emitted until this succeeds: a successful command is therefore a truthful
        # guarantee that every extracted commitment was written/deduped or already terminal.
        user_email = (a.user_email or _s(inputs["google"].get("userEmail"))
                      or configured_user_email())
        _, ended = build_context(inputs, a.since_hours)
        out["ledger"] = _apply_ledger(out, user_email, ended)
        if a.reconcile_open_loops:
            # The backlog command is one transaction-shaped unit from the agent's perspective:
            # extract → persist → read back. It cannot mistake intended writes for observed state.
            out["open_loops"] = _query_ledger()
    print(json.dumps(out))


if __name__ == "__main__":
    main()
