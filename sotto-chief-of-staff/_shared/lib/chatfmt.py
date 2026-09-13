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


def to_imessage(text) -> str:
    """Plain text for Messages, for both interactive and scheduled deliveries.

    Preserve words and web destinations; remove presentation syntax and encoded email
    actions that Messages cannot render usefully. Canonical archives stay untouched.
    Idempotent because Hermes formats both before retries and at the send boundary.
    """
    t = _MARKER_RE.sub("", _s(text))
    # Balanced parentheses occur in real web links (e.g. Wikipedia article names).
    link = re.compile(r"!?\[([^\]\n]+)\]\(")
    cursor, parts = 0, []
    while match := link.search(t, cursor):
        end, depth = match.end(), 1
        while end < len(t) and depth and t[end] != "\n":
            depth += (t[end] == "(") - (t[end] == ")")
            end += 1
        if depth:
            parts.append(t[cursor:match.end()])
            cursor = match.end()
            continue
        label, url = match.group(1), t[match.end():end - 1]
        if url.lower().startswith("mailto:"):
            replacement = "(Copy the draft into Gmail to review and send.)"
        else:
            replacement = url if label == url else f"{label}: {url}"
        parts.extend((t[cursor:match.start()], replacement))
        cursor = end
    parts.append(t[cursor:])
    t = "".join(parts)
    t = re.sub(r"mailto:[^\s<>]+", "(Copy the draft into Gmail to review and send.)", t, flags=re.I)
    t = _HEADING_RE.sub(r"\1", t)
    t = _HRULE_RE.sub("", t)
    t = re.sub(r"^[ \t]*```[^\n]*$", "", t, flags=re.M)
    t = re.sub(r"^[ \t]*(?:>[ \t]*)+", "", t, flags=re.M)
    # Boundary checks keep URLs, snake_case identifiers and arithmetic intact.
    for marker in (r"\*\*", r"__", r"\*", r"_", r"~~", r"`"):
        t = re.sub(rf"(?<![\w/]){marker}(\S(?:[^\n]*?\S)?){marker}(?!\w)", r"\1", t)
    t = re.sub(r"^[ \t]*[-*+]\s+", "• ", t, flags=re.M)
    t = re.sub(r"[ \t]+$", "", t, flags=re.M)
    # Consecutive short list entries need one line each, not separate paragraphs.
    t = re.sub(r"(?<=\n)\n+(?=• )", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def compact_handled(text) -> str:
    """Keep the complete handled recap but display its entries as a compact list."""
    match = re.search(r"^\*?(?:✅ )?Already Handled\*?[ \t]*$", text, flags=re.M)
    if not match:
        return text
    end = re.search(r"^\*?(?:Filtered|Coming Up|Needs Attention Now|Should Handle Today)\*?[ \t]*$",
                    text[match.end():], flags=re.M)
    # Only recognized brief sections; a free-form chat answer is not a brief.
    if not end:
        return text
    stop = match.end() + end.start()
    entries = [line.strip().lstrip("• ") for line in text[match.end():stop].splitlines() if line.strip()]
    return text[:match.end()] + "\n" + "\n".join("• " + entry for entry in entries) + "\n\n" + text[stop:]
