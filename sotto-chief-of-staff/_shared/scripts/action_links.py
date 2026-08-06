#!/usr/bin/env python3
"""
action_links.py — build tappable deep links for sending (no AppleScript needed).

PORT SOURCE: app/src/lib/actionSchemas.tsx (the urlScheme/buildUrl builders).
A draft becomes a deep link delivered in chat (Telegram/WhatsApp). The user taps it ON THEIR PHONE
and the native app (Messages/WhatsApp/Mail) opens with the recipient + draft PREFILLED — exactly
like the Mac/iOS app. The recipient + draft are known server-side, so the link is fully built here.

Usage: action_links.py '{"channel":"imessage","identifier":"+15551234567","message":"On my way","subject":""}'
Prints { "url": "...", "label": "Reply to +1555…" }
"""
from __future__ import annotations

import json
import re
import sys
from urllib.parse import quote


def _digits(identifier: str) -> str:
    d = re.sub(r"[^0-9+]", "", identifier or "")
    return d


def imessage(identifier: str, message: str = "") -> str:
    base = f"imessage://{_digits(identifier) or identifier}"
    return f"{base}?body={quote(message)}" if message else base


def sms(identifier: str, message: str = "") -> str:
    # Messages routes iMessage vs SMS automatically; most chat clients linkify sms: reliably.
    base = f"sms:{_digits(identifier) or identifier}"
    return f"{base}&body={quote(message)}" if message else base


def whatsapp(identifier: str, message: str = "") -> str:
    phone = _digits(identifier).lstrip("+")
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
    return f"tel:{_digits(identifier) or identifier}"


def link_for(channel: str, identifier: str, message: str = "", subject: str = "") -> str:
    ch = (channel or "").lower()
    if ch == "imessage":
        return imessage(identifier, message)
    if ch == "sms":
        return sms(identifier, message)
    if ch == "whatsapp":
        return whatsapp(identifier, message)
    if ch in ("email", "gmail", "apple_mail", "mail"):
        return mailto(identifier, message, subject)
    if ch in ("phone", "facetime", "tel"):
        return tel(identifier)
    raise ValueError(f"unknown channel: {channel}")


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    req = json.loads(raw)
    url = link_for(req.get("channel", ""), req.get("identifier", ""),
                   req.get("message", ""), req.get("subject", ""))
    print(json.dumps({"url": url, "label": f"Open {req.get('channel')} to {req.get('identifier')}"}))


if __name__ == "__main__":
    main()
