#!/usr/bin/env python3
"""
action_links.py — build tappable deep links for sending (no AppleScript needed).

PORT SOURCE: app/src/lib/actionSchemas.tsx (the urlScheme/buildUrl builders).
A draft becomes a deep link delivered in chat (Telegram/WhatsApp). The user taps it ON THEIR PHONE
and the native app (Messages/WhatsApp/Mail) opens with the recipient + draft PREFILLED — exactly
like the Mac/iOS app. The recipient + draft are known server-side, so the link is fully built here.

Usage: action_links.py '{"channel":"imessage","identifier":"+15551234567","message":"On my way","subject":""}'
Prints { "url": "...", "label": "Reply to +1555…" }

`action_type` names the variant of the draft ("reply", "decline", "send", …). It exists for ONE
reason now: A DECLINE IS NEVER LINKED. That is the never-pre-link-a-decline rule (sotto-draft-reply)
moved out of prose and into code — pass `action_type: "decline"` and this module hands back an empty
URL, whatever the channel, so a decline is presented as text and approved by a human every time.

Every built link also leaves one row in $SOTTO_DATA/events/drafts.jsonl — the offered-drafts
ledger, the left half of the draft→outcome matcher (_shared/scripts/draft_outcomes.py, which
runs directly in every brief's Learn step). This module is the ONE place a draft
becomes tappable, which makes it the one honest place to record that a draft was offered. The
ledger row is best-effort: a failed append never costs the link.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
from message_targets import normalized_target  # noqa: E402

# The one action_type with a rule of its own: a decline is `review` tier forever, so it is presented
# as text and never pre-linked.
DECLINE_ACTION = "decline"

# Offered-drafts ledger bounds (rotate-keeping-tail, same mechanism as the event queue).
DRAFTS_MAX_BYTES = 2 * 1024 * 1024
DRAFTS_KEEP_LINES = 2000


def drafts_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "events", "drafts.jsonl")


def record_draft(channel: str, identifier: str, message: str, action_type: str = "") -> None:
    """One line per offered draft, including declines — they're presented as text, and the matcher
    still wants to know whether the user sent one. A draft with no supported recipient (see
    normalized_identifier) leaves no row: the outcome matcher could never match it back to a real
    conversation, so recording it would only be a receipt nothing can reconcile against. Empty-
    message links (a bare "open the thread" tap) are likewise not drafts and leave no row."""
    if not (message or "").strip() or not normalized_identifier(channel, identifier):
        return
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
        from sotto_log import bounded_append  # noqa: PLC0415
        row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "channel": (channel or "").lower(),
               "identifier": normalized_identifier(channel, identifier),
               "text": message,
               "action_type": (action_type or "").strip().lower()}
        bounded_append(drafts_path(), json.dumps(row), DRAFTS_MAX_BYTES, DRAFTS_KEEP_LINES)
    except Exception:  # noqa: BLE001
        pass


def normalized_identifier(channel: str, identifier: str) -> str:
    """The identifier EXACTLY as this module's link builders embed it — one per-channel
    normalization, so every builder below (and anything that later has to match a link against a
    real conversation) works from the same string instead of re-deriving it."""
    ch = (channel or "").lower()
    if ch in ("imessage", "sms", "phone", "facetime", "tel", "whatsapp", "whatsapp_call",
              "email", "gmail", "apple_mail", "mail"):
        return normalized_target(ch, identifier)
    return (identifier or "").strip()


def imessage(identifier: str, message: str = "") -> str:
    target = normalized_identifier('imessage', identifier)
    if not target:
        return ''
    base = f"imessage://{quote(target, safe='@+')}"
    return f"{base}?body={quote(message)}" if message else base


def sms(identifier: str, message: str = "") -> str:
    # Messages routes iMessage vs SMS automatically; most chat clients linkify sms: reliably.
    target = normalized_identifier('sms', identifier)
    if not target:
        return ''
    base = f"sms:{target}"
    return f"{base}&body={quote(message)}" if message else base


def whatsapp(identifier: str, message: str = "") -> str:
    phone = normalized_identifier("whatsapp", identifier)
    if not phone:
        return ''
    base = f"https://wa.me/{phone}"  # universal https click-to-chat (reliable in chat clients)
    return f"{base}?text={quote(message)}" if message else base


def mailto(email: str, message: str = "", subject: str = "") -> str:
    target = normalized_identifier('email', email)
    if not target:
        return ''
    parts = []
    if subject:
        parts.append(f"subject={quote(subject)}")
    if message:
        parts.append(f"body={quote(message)}")
    return f"mailto:{quote(target, safe='@+')}" + (("?" + "&".join(parts)) if parts else "")


def tel(identifier: str) -> str:
    target = normalized_identifier('tel', identifier)
    return f"tel:{target}" if target else ''


def gmail_thread(thread_id: str) -> str:
    """Gmail's web thread URL — the fallback for an email action that has a thread but no address."""
    return f"https://mail.google.com/mail/u/0/#inbox/{thread_id}" if thread_id else ""


def link_for(channel: str, identifier: str, message: str = "", subject: str = "",
             action_type: str = "") -> str:
    """THE link builder — every tappable link in the product is built here (compose_brief's
    _action_tap_link picks the channel + identifier for a brief action, then delegates).

    One channel, one exception: `action_type: "decline"` returns "" — a decline is `review` tier
    forever and is never pre-linked."""
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
    record_draft(channel, identifier, message, action_type)
    # The decline is built (so the channel is still validated) and then withheld. Enforcing it
    # here means the rule holds even if a prompt forgets it.
    return "" if (action_type or "").strip().lower() == DECLINE_ACTION else url


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    req = json.loads(raw)
    action_type = req.get("action_type", "")
    url = link_for(req.get("channel", ""), req.get("identifier", ""),
                   req.get("message", ""), req.get("subject", ""), action_type)
    declined = action_type.strip().lower() == DECLINE_ACTION
    label = (f"Open {req.get('channel')} to {req.get('identifier')}" if url else
             "A decline is never pre-linked. Present it as text." if declined else
             "No supported recipient address. Show the draft without a link.")
    print(json.dumps({"url": url, "label": label,
                      "status": 'ready' if url else 'review' if declined else 'needs_recipient'}))


if __name__ == "__main__":
    main()
