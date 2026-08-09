#!/usr/bin/env python3
"""triage_queue.py — the cross-channel "needs you" queue for sotto-triage.

Lists the threads waiting on a reply across email + iMessage + WhatsApp, name-resolved and deduped, so
the agent can walk them one at a time (draft / archive / label / mark-handled). Reuses compose_brief's
thread-processing + contact resolution, so the queue matches the morning brief exactly.

Inputs (the same temp files the brief uses):
  --local /tmp/sotto_local.json   (read_local: imessage/whatsapp/contacts)
  --gmail /tmp/sotto_gmail.json    (gather_google output: array of emails)
Prints JSON: { "email":[...], "imessage":[...], "whatsapp":[...], "counts":{...} }
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
# The brief's own thread-processing + contact resolution, from the modules that OWN them.
from textutil import _arr, _looks_like_phone_number, _s, unwrap_tool_result  # noqa: E402
from render_local import (  # noqa: E402
    _group_messages_into_threads, _is_sent_email, _thread_needs_response, build_contact_lookup,
)

PROMO_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_UPDATES", "SPAM"}


def _load(path, default):
    if not path:
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def local_queue(local, channel):
    """Needs-a-reply iMessage/WhatsApp threads, name-resolved, unknown phone-only senders dropped."""
    lookup = build_contact_lookup(_arr(local, "contacts"))
    out = []
    for t in _group_messages_into_threads(_arr(local, channel), channel, lookup):
        if not _thread_needs_response(t):
            continue
        name = _s(t.get("name"))
        if not name or name == "Unknown":
            continue  # unresolved sender (e.g. @lid) — drafting to an unidentified human is unsafe
        if _looks_like_phone_number(name):
            continue  # unknown phone-only / shortcode / OTP sender — not worth triaging
        msgs = t.get("messages") or []
        last_in = next((m for m in reversed(msgs) if not m.get("is_from_me")), msgs[-1] if msgs else {})
        out.append({
            "name": name,
            "identifier": "" if t.get("is_group_chat") else _s(t.get("identifier")),
            "channel": channel,
            "is_group_chat": bool(t.get("is_group_chat")),
            "last_snippet": _s(last_in.get("text"))[:240],
            "message_count": len(msgs),
        })
    return out


def email_queue(emails):
    """Email threads whose latest message is inbound (user hasn't replied) and not promotional."""
    by_thread: dict = {}
    for e in emails:
        tid = _s(e.get("threadId")) or _s(e.get("id"))
        by_thread.setdefault(tid, []).append(e)
    out = []
    for tid, msgs in by_thread.items():
        msgs.sort(key=lambda m: _s(m.get("date")))
        last = msgs[-1]
        if _is_sent_email(last):
            continue  # user replied last → already handled
        labels = set(last.get("labelIds") or last.get("labels") or [])
        if labels & PROMO_LABELS:
            continue
        out.append({
            "from": _s(last.get("from")),
            "subject": _s(last.get("subject")),
            "threadId": tid,
            "channel": "email",
            "last_snippet": (_s(last.get("snippet")) or _s(last.get("body")))[:240],
            "important": "IMPORTANT" in labels,
            "unread": "UNREAD" in labels,
        })
    out.sort(key=lambda x: (not x["important"], not x["unread"]))   # important + unread first
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", default="/tmp/sotto_local.json")
    ap.add_argument("--gmail", default="/tmp/sotto_gmail.json")
    a = ap.parse_args()
    local = unwrap_tool_result(_load(a.local, {}))   # accept raw read_local tool-result wrappers
    emails = _load(a.gmail, [])
    if isinstance(emails, dict):
        emails = emails.get("emails") or emails.get("messages") or []
    q = {
        "email": email_queue(emails),
        "imessage": local_queue(local, "imessage"),
        "whatsapp": local_queue(local, "whatsapp"),
    }
    q["counts"] = {k: len(v) for k, v in q.items() if k != "counts"}
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
        from sotto_log import diag
        c = q["counts"]
        diag(f"[triage_queue] need a reply: {c['email']} email, {c['imessage']} imessage, {c['whatsapp']} whatsapp")
    except Exception:
        pass
    print(json.dumps(q))


if __name__ == "__main__":
    main()
