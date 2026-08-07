#!/usr/bin/env python3
"""
action_links.py — build tappable deep links for sending (no AppleScript needed).

PORT SOURCE: app/src/lib/actionSchemas.tsx (the urlScheme/buildUrl builders).
A draft becomes a deep link delivered in chat (Telegram/WhatsApp). The user taps it ON THEIR PHONE
and the native app (Messages/WhatsApp/Mail) opens with the recipient + draft PREFILLED — exactly
like the Mac/iOS app. The recipient + draft are known server-side, so the link is fully built here.

Usage: action_links.py '{"channel":"imessage","identifier":"+15551234567","message":"On my way","subject":""}'
Prints { "url": "...", "label": "Reply to +1555…" }

RECORDING (roadmap Step 2 item 0): the `message` handed to this script IS the final drafted reply —
by the time a link is built the agent has finished composing. So every link build appends one
{ts, channel, identifier, message} line to $SOTTO_DATA/events/drafts.jsonl. That makes offered
drafts a persisted artifact (they used to be composed in the agent turn and stored NOWHERE), which
is the missing left-hand side of Step 3's draft-diff matcher: drafts.jsonl × the outgoing signals
already landing in events/queue.jsonl → "what did he change about what I wrote". Deterministic —
no agent compliance needed — and strictly best-effort: recording must never break link building.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
from urllib.parse import quote

_HERE = os.path.dirname(os.path.abspath(__file__))
_SHARED_LIB = os.path.join(_HERE, "..", "lib")

# Same bound as triage_event's events/queue.jsonl — drafts.jsonl is its join partner and lives on the
# same /data volume, so it gets the same rotate-keeping-tail ceiling (a matcher only needs recent days).
DRAFTS_MAX_BYTES = 4 * 1024 * 1024
DRAFTS_KEEP_LINES = 4000


def _digits(identifier: str) -> str:
    d = re.sub(r"[^0-9+]", "", identifier or "")
    return d


def normalized_identifier(channel: str, identifier: str) -> str:
    """The identifier EXACTLY as this module's link builders embed it. Single source of truth for
    both the URL and the drafts.jsonl join key — a recorded draft must be matchable against an
    outgoing signal without re-deriving (or re-guessing) the per-channel format."""
    ch = (channel or "").lower()
    if ch in ("imessage", "sms", "phone", "facetime", "tel"):
        return _digits(identifier) or (identifier or "")
    if ch == "whatsapp":
        return _digits(identifier).lstrip("+")
    return (identifier or "").strip()   # email/gmail/apple_mail/mail — mailto: uses it verbatim


def imessage(identifier: str, message: str = "") -> str:
    base = f"imessage://{normalized_identifier('imessage', identifier)}"
    return f"{base}?body={quote(message)}" if message else base


def sms(identifier: str, message: str = "") -> str:
    # Messages routes iMessage vs SMS automatically; most chat clients linkify sms: reliably.
    base = f"sms:{normalized_identifier('sms', identifier)}"
    return f"{base}&body={quote(message)}" if message else base


def whatsapp(identifier: str, message: str = "") -> str:
    phone = normalized_identifier("whatsapp", identifier)
    base = f"https://wa.me/{phone}"  # universal https click-to-chat (reliable in chat clients)
    return f"{base}?text={quote(message)}" if message else base


def mailto(email: str, message: str = "", subject: str = "") -> str:
    parts = []
    if subject:
        parts.append(f"subject={quote(subject)}")
    if message:
        parts.append(f"body={quote(message)}")
    return f"mailto:{email}" + (("?" + "&".join(parts)) if parts else "")


def tel(identifier: str) -> str:
    return f"tel:{normalized_identifier('tel', identifier)}"


def gmail_thread(thread_id: str) -> str:
    """Gmail's web thread URL — the fallback for an email action that has a thread but no address."""
    return f"https://mail.google.com/mail/u/0/#inbox/{thread_id}" if thread_id else ""


def drafts_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "events", "drafts.jsonl")


def record_draft(channel: str, identifier: str, message: str, subject: str = "") -> None:
    """Append one {ts, channel, identifier, message} line to events/drafts.jsonl — the offered-draft
    ledger. WRITE-ONLY today, deliberately: the reader is the draft-diff matcher that lands with the
    learning loop (ROADMAP Step 3), and recording starts early because training data only accrues from
    the day recording starts. ts is the same ISO-Z the queue/surfaced ledgers use, so that future join
    is a plain string/time comparison. Bounded via sotto_log.bounded_append (rotate-keeping-tail), the
    same retention the other jsonl ledgers use, so it can't grow forever on the /data volume.

    Messageless links (tel:/facetime, or a bare open-the-thread link) carry no draft — nothing to
    record. ANY failure is swallowed: the link is the user-facing product, recording is exhaust."""
    try:
        if not message or not str(message).strip():
            return
        if _SHARED_LIB not in sys.path:
            sys.path.insert(0, _SHARED_LIB)
        from sotto_log import bounded_append  # noqa: PLC0415
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "channel": (channel or "").lower(),
            "identifier": normalized_identifier(channel, identifier),
            "message": str(message),
        }
        if subject:
            entry["subject"] = str(subject)
        bounded_append(drafts_path(), json.dumps(entry), DRAFTS_MAX_BYTES, DRAFTS_KEEP_LINES)
    except Exception:  # noqa: BLE001
        pass


def link_for(channel: str, identifier: str, message: str = "", subject: str = "") -> str:
    """THE link builder — every tappable link in the product is built here (compose_brief's
    _action_tap_link picks the channel + identifier for a brief action, then delegates), which is also
    what makes this the one place drafts.jsonl can record from."""
    ch = (channel or "").lower()
    if ch == "imessage":
        url = imessage(identifier, message)
    elif ch == "sms":
        url = sms(identifier, message)
    elif ch in ("whatsapp", "whatsapp_call"):
        url = whatsapp(identifier, message)
    elif ch in ("email", "gmail", "apple_mail", "mail"):
        url = mailto(identifier, message, subject)
    elif ch == "gmail_thread":
        url = gmail_thread(identifier)
    elif ch in ("phone", "facetime", "tel"):
        url = tel(identifier)
    elif ch == "calendar":
        url = (identifier or "").strip()   # a meeting/event link is ALREADY a URL — the caller resolved it
    else:
        raise ValueError(f"unknown channel: {channel}")
    # Record AFTER the link is built: an unknown channel raises above and never pollutes the ledger.
    record_draft(channel, identifier, message, subject)
    return url


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    req = json.loads(raw)
    url = link_for(req.get("channel", ""), req.get("identifier", ""),
                   req.get("message", ""), req.get("subject", ""))
    print(json.dumps({"url": url, "label": f"Open {req.get('channel')} to {req.get('identifier')}"}))


if __name__ == "__main__":
    main()
