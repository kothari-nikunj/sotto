#!/usr/bin/env python3
"""chatfmt.py — the ONE markdown→chat-text transformation every surface shares (Sprint 0 §3).

The text actually SENT on a chat channel
(WhatsApp/Telegram/SMS) must not carry Mac-app plumbing (<!--id:…--> / <!--meeting:…--> markers) or
CommonMark formatting (## headings, **bold**) that chat clients show as literal clutter. Delivery
instructions used to tell the AGENT to clean this up; agents skip instructions, so the conversion is
deterministic and shared here — pulse, meeting-prep, followup (and eventually the brief itself) all
emit chat-ready text through this single pipeline instead of five diverging copies.

  to_chat(text) -> str
    - every <!--…--> marker is stripped (id, ch, meeting — all of them);
    - '## Heading' → '*Heading*' (WhatsApp bold);
    - '**bold**' → '*bold*' (WhatsApp bold syntax is single asterisks);
    - horizontal rules dropped; trailing spaces and runs of blank lines collapsed;
    - deep-link lines (plain URLs / sms:/mailto: lines) pass through untouched.

IDEMPOTENT: to_chat(to_chat(x)) == to_chat(x) — already-converted *single-asterisk* text has no
'**', no '#' headings and no markers left, so a second pass changes nothing. Producers can safely
route text through this even when an upstream step already did.

No dependencies (stdlib re only) so any script can `from chatfmt import to_chat` with just
_shared/lib on sys.path.
"""
from __future__ import annotations

import re

_MARKER_RE = re.compile(r"<!--.*?-->", re.S)
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$", re.M)
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_HRULE_RE = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$", re.M)


def _s(v) -> str:
    return v if isinstance(v, str) else ("" if v is None else str(v))


def to_chat(text) -> str:
    """markdown-ish text → the chat-deliverable form (see module docstring). None/non-str → ''."""
    t = _s(text)
    t = _MARKER_RE.sub("", t)
    t = _HEADING_RE.sub(lambda m: f"*{m.group(1)}*", t)
    t = _BOLD_RE.sub(r"*\1*", t)
    t = _HRULE_RE.sub("", t)
    t = re.sub(r"[ \t]+$", "", t, flags=re.M)     # trailing space the marker strip leaves
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()
