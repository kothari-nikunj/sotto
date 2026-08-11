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

(This module used to also append every built link to $SOTTO_DATA/events/drafts.jsonl, the left half
of Step 3's draft-diff matcher. Nothing ever read it, so it was deleted — see ROADMAP; re-add it
with the matcher, when there is a consumer.)
"""
from __future__ import annotations

import json
import re
import sys
from urllib.parse import quote

# The one action_type with a rule of its own: a decline is `review` tier forever, so it is presented
# as text and never pre-linked.
DECLINE_ACTION = "decline"


def _digits(identifier: str) -> str:
    d = re.sub(r"[^0-9+]", "", identifier or "")
    return d


def normalized_identifier(channel: str, identifier: str) -> str:
    """The identifier EXACTLY as this module's link builders embed it — one per-channel
    normalization, so every builder below (and anything that later has to match a link against a
    real conversation) works from the same string instead of re-deriving it."""
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
    # The decline is built (so the channel is still validated) and then withheld. Enforcing it
    # here means the rule holds even if a prompt forgets it.
    return "" if (action_type or "").strip().lower() == DECLINE_ACTION else url


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    req = json.loads(raw)
    action_type = req.get("action_type", "")
    url = link_for(req.get("channel", ""), req.get("identifier", ""),
                   req.get("message", ""), req.get("subject", ""), action_type)
    label = (f"Open {req.get('channel')} to {req.get('identifier')}" if url
             else "a decline is never pre-linked — present it as text")
    print(json.dumps({"url": url, "label": label}))


if __name__ == "__main__":
    main()
