#!/usr/bin/env python3
"""
style_apply.py — build the drafter's writing-style guidance for a recipient+channel (style.json v2).

PORT SOURCE: api/src/services/style-profile.ts formatStyleForWorker (818-963).
The real signal is VERBATIM sample messages quoted into the prompt — per-person first, then the
context bucket — plus master-style guardrails (capitalization, exclamation habit, openers/closings).
The drafter (draft-reply skill) reads this and writes in the user's voice.

Usage: style_apply.py '{"recipient":"sarah@acme.com","channel":"email","canonical_id":"c_..","work":true}'
Prints { "guidance": "<markdown>", "bucket": "...", "source": "per_person|canonical|recent|bucket" }
"""
from __future__ import annotations

import json
import os
import sys


def _root():
    return os.environ.get("SOTTO_DATA", "/data")


def _bucket(channel: str, person_context: str | None) -> str:
    if channel in ("email", "gmail", "apple_mail"):
        return "work_email"               # personal voice on a work email is a disaster (ts:838)
    return "personal_message" if person_context == "personal" else "work_message"


def _quote(samples: list, limit: int, header: str) -> list:
    lines, shown = [], 0
    for s in samples:
        t = (s.get("text") or "").strip()
        if not t:
            continue
        lines.append(f"> {t}")
        shown += 1
        if shown >= limit:
            break
    return ([f"\n### {header}"] + lines) if lines else []


def apply(req: dict) -> dict:
    path = os.path.join(_root(), "style.json")
    style = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            style = json.load(f)

    channel = req.get("channel", "imessage")
    recipient = (req.get("recipient") or "").lower()
    cid = req.get("canonical_id")
    per_person = style.get("per_person") or {}
    person = per_person.get(cid) or per_person.get(f"name:{recipient}") or \
        next((p for p in per_person.values() if (p.get("name") or "").lower() == recipient), None)

    bucket = _bucket(channel, person.get("context") if person else None)
    canonical = (style.get("canonical") or {}).get(bucket, [])
    recent = [s for s in (style.get("recent") or []) if s.get("bucket") == bucket]
    confirmed = [s for s in (style.get("confirmed") or []) if s.get("bucket") == bucket]
    master = (style.get("master_by_context") or {}).get(bucket) or style.get("master") or {}

    parts = ["## How to write this — match the user's actual voice"]
    source = "bucket"
    person_texts = set()
    if person and person.get("samples"):
        parts += _quote(person["samples"], 8,
                        f"How the user actually writes to {person.get('name') or recipient} — match this voice, length, punctuation, and tone exactly")
        person_texts = {(s.get("text") or "")[:80] for s in person["samples"]}
        if person.get("notes"):
            parts.append("(" + "; ".join(person["notes"]) + ")")
        source = "per_person"

    if confirmed:
        parts += _quote(confirmed, 4, "Drafts the user has shipped before (highest signal)")
        if source == "bucket":
            source = "confirmed"

    has_person = bool(person and person.get("samples"))
    can_budget, rec_budget = (4, 2) if has_person else (7, 3)
    canon_fresh = [s for s in sorted(canonical, key=lambda s: s.get("quality", 0), reverse=True)
                   if (s.get("text") or "")[:80] not in person_texts]
    if canon_fresh:
        parts += _quote(canon_fresh, can_budget, f"How the user writes {bucket.replace('_', ' ')}s")
        if source == "bucket":
            source = "canonical"
    if recent:
        parts += _quote(recent, rec_budget, "Recent messages (freshest voice)")
        if source == "bucket":
            source = "recent"

    # Guardrails from the bucket's master style.
    g = []
    if master.get("capitalization") == "lowercase":
        g.append("- Starts messages lowercase — do the same.")
    if master.get("uses_exclamation_marks") is False:
        g.append("- Rarely uses exclamation marks — don't add them.")
    elif master.get("uses_exclamation_marks") is True:
        g.append("- Uses exclamation marks naturally — include them.")
    if master.get("uses_em_dashes"):
        g.append("- Uses em dashes.")
    if master.get("greetings"):
        g.append("- Typical openers: " + ", ".join(master["greetings"]) + ".")
    if master.get("signoffs"):
        g.append("- Typical closings: " + ", ".join(master["signoffs"]) + ".")
    if g:
        parts.append("\n### Voice guardrails")
        parts += g

    prefs = style.get("preferences") or []
    if prefs:
        parts.append("\n### Learned preferences (from past edits)")
        parts += [f"- {p}" for p in prefs]

    if source == "bucket" and len(parts) == 1:
        # Nothing learned yet — give a minimal honest instruction.
        parts.append("(No writing samples yet — write concise and natural; mirror the incoming message's register.)")
    parts.append('\nWrite as if you ARE this person. The samples above are the ground truth — if in doubt, re-read them.')

    return {"guidance": "\n".join(parts), "bucket": bucket, "source": source,
            "master": master, "has_person": has_person}


if __name__ == "__main__":
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    print(json.dumps(apply(json.loads(raw))))
