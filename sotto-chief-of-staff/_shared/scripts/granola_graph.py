#!/usr/bin/env python3
"""
granola_graph.py — the meetings you actually sat in become the relationship graph.

Granola already tells us who was in the room; until now that attendance was read once, rendered into
one brief, and thrown away. This is the writer that keeps it: every small meeting becomes a dated,
sourced `Met on …` fact on each attendee's person file, so a brief three months from now can say
"you last sat with Priya on June 25 about the Harbor term sheet" instead of meeting her cold.

What it writes, and nothing more:
  • one fact per attendee per meeting — "Met on <date>: <title>" plus one clause of the AI summary
    when there is one, capped at MAX_FACT_CHARS, `source: granola`, `source_ref` the meeting id
  • a `company` profile patch when the attendee's email domain is a corporate one AND the graph has
    no company for them yet — the dumbest possible inference ("acmecorp.com" → "Acmecorp"), applied
    only into a blank

What it refuses to write, because it would be noise:
  • meetings with more than MAX_ATTENDEES_FOR_GRAPH attendees — a 40-person webinar is not a
    relationship, and 40 identical facts is not memory
  • meetings with no attendee emails at all, the user's own address, and automated senders
  • a company guess for a freemail address (gmail.com says nothing about where someone works)

Idempotent by construction: the fact text for a given meeting is identical on every run, so
knowledge_update's find_similar_fact BUMPs it (seen+1, last=today) instead of duplicating. A
RECURRING meeting with the same title bumps the same fact too — that is the intent: a weekly sync is
one strengthening relationship signal, not fifty facts.

Usage (execute_code):  granola_graph.py [--granola /tmp/sotto_granola.json]
Prints JSON: {"meetings": N, "people": M, "facts": K, "skipped_large": L}.
Missing/empty/unparseable input is a no-op that exits 0 (fail toward silence).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "lib"))
sys.path.insert(0, os.path.join(_HERE, "..", "knowledge"))
import knowledge as kg  # noqa: E402
import knowledge_update as ku  # noqa: E402
from textutil import _arr, _is_excluded_domain, _is_likely_automated, _base_domain, _s  # noqa: E402
from timeutil import configured_user_email  # noqa: E402

DEFAULT_GRANOLA = "/tmp/sotto_granola.json"
MAX_ATTENDEES_FOR_GRAPH = 8   # above this it is an event, not a meeting — attendance proves nothing
MAX_FACT_CHARS = 200          # one line in a person file; the summary clause is texture, not a copy
CONFIDENCE = 0.7              # observed attendance is solid, but the title is the model's, not gospel
SOURCE = "granola"


def _name_from_email(email: str) -> str:
    """'sarah.chen@acme.com' → 'Sarah Chen'. Only a placeholder: knowledge_update never lets a
    derived name overwrite a real one, and the identifier is what actually keys the file."""
    local = re.split(r"[+@]", _s(email).strip().lower())[0]
    parts = [p for p in re.split(r"[._\-]+", local) if p]
    return " ".join(p.capitalize() for p in parts) or _s(email)


def _company_from_email(email: str) -> str:
    """'sarah@acmecorp.com' → 'Acmecorp'; freemail/hosting → ''. Deliberately dumb: the registrable
    label capitalized, never a lookup and never a guess at the legal name."""
    base = _base_domain(email)
    if not base or _is_excluded_domain(base):
        return ""
    return base.split(".")[0].capitalize()


def _first_clause(text: str) -> str:
    """The first sentence of an AI summary, list bullets and markdown stripped."""
    t = re.sub(r"\s+", " ", _s(text)).strip()
    t = re.sub(r"^[#>*\-\s]+", "", t)
    if not t:
        return ""
    return re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0].strip()


def _fact_text(meeting: dict) -> str:
    date = _s(meeting.get("date")).strip() or _s(meeting.get("start"))[:10]
    title = _s(meeting.get("title")).strip() or "Untitled Meeting"
    text = f"Met on {date}: {title}" if date else f"Met: {title}"
    clause = _first_clause(meeting.get("ai_summary"))
    if clause:
        text = f"{text} — {clause}"
    if len(text) > MAX_FACT_CHARS:
        text = text[:MAX_FACT_CHARS - 1].rstrip() + "…"
    return text


def _source_ref(meeting: dict) -> str:
    mid = _s(meeting.get("meeting_id")) or _s(meeting.get("id"))
    if mid:
        return mid
    return f"{_s(meeting.get('title')).strip()}:{_s(meeting.get('date')).strip()}"


def _known_company(email: str, name: str, index: dict | None = None) -> str:
    """The company already on file for this person, or '' — the guard that keeps the domain guess
    out of a file that knows better. An unreadable graph reads as 'no company' (best-effort)."""
    try:
        path = kg.find_person_file(name=name, identifier=email, index=index)
        if not path or not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return _s(kg.parse_person_file(f.read()).company).strip()
    except Exception:  # noqa: BLE001
        return ""


def build_updates(meetings: list, user_email: str = "") -> tuple:
    """(person_updates, skipped_large) for a list of gather_granola meetings."""
    user_email = _s(user_email).strip().lower() or configured_user_email()
    try:   # ONE index for the whole batch — the company guard reads it, it never writes
        index = kg.build_people_index()
    except Exception:  # noqa: BLE001
        index = None
    updates: list = []
    skipped_large = 0
    for m in meetings:
        if not isinstance(m, dict):
            continue
        emails = []
        for e in _arr(m, "attendee_emails"):
            addr = _s(e).strip().lower()
            if "@" not in addr or addr == user_email or _is_likely_automated(addr):
                continue
            if addr not in emails:
                emails.append(addr)
        # The cap counts the meeting's OWN guest list, not what survived the filters: a 40-person
        # webinar stays a webinar even when 32 of the addresses are automated.
        if len(_arr(m, "attendee_emails")) > MAX_ATTENDEES_FOR_GRAPH:
            skipped_large += 1
            continue
        if not emails:
            continue
        fact = {"fact": _fact_text(m), "memory_type": "milestone", "confidence": CONFIDENCE,
                "source": SOURCE, "source_ref": _source_ref(m)}
        for addr in emails:
            name = _name_from_email(addr)
            upd = {"person_name": name, "identifier": addr, "updated_by": SOURCE,
                   "facts": [dict(fact)]}
            company = _company_from_email(addr)
            if company and not _known_company(addr, name, index):
                upd["profile_patch"] = {"company": company}
            updates.append(upd)
    return updates, skipped_large


def run(granola: dict) -> dict:
    meetings = _arr(granola, "meetings")
    updates, skipped_large = build_updates(meetings)
    if updates:
        ku.apply({"person_updates": updates})
    return {"meetings": len(meetings), "people": len({u["identifier"] for u in updates}),
            "facts": len(updates), "skipped_large": skipped_large}


def main() -> None:
    ap = argparse.ArgumentParser(description="Persist Granola meeting attendance into the graph.")
    ap.add_argument("--granola", default=DEFAULT_GRANOLA, help=f"gather_granola output ({DEFAULT_GRANOLA})")
    args = ap.parse_args()
    try:
        with open(args.granola, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        print(json.dumps({"meetings": 0, "people": 0, "facts": 0, "skipped_large": 0}))
        return
    print(json.dumps(run(data if isinstance(data, dict) else {})))


if __name__ == "__main__":
    main()
