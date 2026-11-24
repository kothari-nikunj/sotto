"""Possibly-handled notes for open tasks ($SOTTO_DATA/events/loop-checks.json).

One rule: an open task you owe gets a check note when, after the ask, you messaged or called that
person, wrote to the person the task is for, met them (Granola), or (a scheduling ask only) a meeting
with them is on the calendar. A scheduling ask also keeps your message's note when their later
messages ask nothing (`replied_since`), and gets a `dates_passed` note once its deadline is before
today and a complete current Calendar read found no meeting that matched the ask. A WhatsApp call notes only a callback ask and is never worded as a
conversation: its row cannot prove you spoke. Informational iMessage read and reaction notes can
also appear on waiting-on tasks. A note never closes or changes the task; the owner confirms.
Notes are keyed by the exact loop version, so any change to the task drops its note.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

import jsonstore
from timeutil import parse_observed_time


INTERACTION_BASES = {"read_no_reply", "reacted"}
REACTION_KINDS = {"love", "like", "dislike", "laugh", "emphasize", "question", "emoji", "sticker"}

_brief_checks: ContextVar[dict | None] = ContextVar("brief_loop_checks", default=None)


@contextmanager
def brief_snapshot():
    """Use one lazily read note snapshot per brief, for rendering and source accounting."""
    token = _brief_checks.set({})
    try:
        yield
    finally:
        _brief_checks.reset(token)


def path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "events", "loop-checks.json")


def read(*, include_hidden_replies=False, permitted=None) -> dict:
    """loop_version → {basis, at, source}. A note from a source no longer allowed is not shown.
    Notes carry no message text or titles."""
    snapshot = None if include_hidden_replies or permitted is not None else _brief_checks.get()
    if snapshot is not None:
        if "checks" not in snapshot:
            snapshot["checks"] = _permitted_checks(jsonstore.read(path(), default={}))
        return snapshot["checks"]
    return _permitted_checks(jsonstore.read(path(), default={}),
                             include_hidden_replies=include_hidden_replies, permitted=permitted)


def _permitted_checks(value: dict, *, include_hidden_replies=False, permitted=None) -> dict:
    from source_context import allowed
    checks = value.get("checks") if isinstance(value, dict) else None
    if not isinstance(checks, dict):
        return {}
    def visible(source):
        name = str(source or "")
        return (permitted is None or name in permitted) and allowed(name)
    return {version: note for version, note in checks.items()
            if isinstance(note, dict) and visible(note.get("source"))
            and (include_hidden_replies or not note.get("replied_since")
                 or (note.get("replied_source") and visible(note["replied_source"])))}


def _newer(note: dict, existing: dict | None) -> bool:
    stamp = parse_observed_time(note.get("message_at") or note.get("at"))
    prior = parse_observed_time(existing.get("message_at") or existing.get("at")) if isinstance(existing, dict) else None
    if stamp is not None and (prior is None or stamp > prior):
        return True
    if stamp is None or stamp != prior or note.get("source") != existing.get("source") or note.get("basis") != existing.get("basis"):
        return False
    reply = parse_observed_time(note.get("replied_since"))
    older = parse_observed_time(existing.get("replied_since"))
    return reply is not None and (older is None or reply > older)


def record(checks: dict, *, previous: dict | None = None) -> None:
    """Replace the notes while honoring additions and removals during the Learn pass."""
    target = path()
    with jsonstore.lock(target):
        result = dict(checks)
        if previous is not None:
            current = _permitted_checks(jsonstore.read(target, default={}), include_hidden_replies=True)
            # An inbound event may have cleared the saved note after this pass read it.
            for version in previous.keys() - current.keys():
                result.pop(version, None)
            for version, note in current.items():
                if note != previous.get(version) and _newer(note, result.get(version)):
                    result[version] = note
        jsonstore.write_atomic(target, {"checks": result})


def add(version: str, note: dict) -> None:
    """Set one task's note between passes (closure_pass, when the owner sends a message)."""
    with jsonstore.transaction(path(), default={}) as value:
        checks = value.get("checks") if isinstance(value.get("checks"), dict) else {}
        if _newer(note, checks.get(version)):
            value["checks"] = {**checks, version: note}


def refresh(version: str, note: dict, update: dict | None) -> None:
    """Update or clear a note between passes only while its observed value still matches."""
    with jsonstore.transaction(path(), default={}) as value:
        checks = value.get("checks") if isinstance(value.get("checks"), dict) else {}
        if checks.get(version) == note:
            if update is None:
                checks.pop(version)
            else:
                checks[version] = update
            value["checks"] = checks


def note_for(row: dict, checks: dict) -> str:
    """One owner-facing phrase for a row's note, or "" when it has none."""
    from delivery_effects import loop_version
    note = checks.get(loop_version(row))
    if not note:
        return ""
    day = str(note.get("at") or "")[:10]
    if note.get("basis") == "meeting_cancelled":
        return "open again: the meeting that closed this was cancelled or declined"
    if note.get("basis") == "dates_passed":
        return f"date passed: {day}; I found no upcoming meeting I could match to this request"
    if note.get("basis") == "read_no_reply":
        return f"they read your message on {day}"
    if note.get("basis") == "reacted":
        return f"they reacted ({note['kind']}) to your message on {day}"
    phrase = {"meeting": f"a meeting with them is on your calendar ({day})",
              "call": (f"there was a WhatsApp call with them on {day}" if note.get("source") == "whatsapp_calls"
                       else f"you spoke by phone on {day}"),
              "sent_to_them": f"you wrote to the person this was for on {day}",
              "met": f"you met them on {day} (Granola notes)"}
    replied = " and they replied since" if note.get("replied_since") else ""
    return "maybe done: " + phrase.get(note.get("basis"), f"you messaged them on {day}{replied}")
