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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
from personal_context import render_feedback  # noqa: E402
import preferences  # noqa: E402
from tzchain import data_root as _root  # noqa: E402


def _bucket(channel: str, person_context: str | None) -> str:
    if channel in ("email", "gmail", "apple_mail"):
        return "work_email"               # personal voice on a work email is a disaster (ts:838)
    return "personal_message" if person_context == "personal" else "work_message"


def _quote(samples: list, limit: int, header: str, seen: set[str]) -> list:
    lines, shown = [], 0
    for s in samples:
        t = (s.get("text") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        lines.append(f"> {t}")
        shown += 1
        if shown >= limit:
            break
    return ([f"\n### {header}"] + lines) if lines else []


def _project_style(style, permitted):
    """Read-only admitted view: never reuse aggregates from withheld samples."""
    import style_extract  # noqa: PLC0415
    from source_context import allowed  # noqa: PLC0415
    sources = set(style_extract.NORMALIZED_CHANNEL_SOURCES.values()) | {'contacts', 'calendar'}
    original = {source: bool(allowed(source)) for source in sources}
    grants = {source: original[source] and bool(permitted(source)) for source in sources}
    attribution = grants['contacts'] and grants['calendar']
    def samples(rows):
        kept = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            channel = row.get('channel')
            source = style_extract.NORMALIZED_CHANNEL_SOURCES.get(channel)
            if not source or not grants[source] or not isinstance(row.get('text'), str):
                continue
            email = channel in ('email', 'gmail', 'apple_mail')
            if not email and not attribution:
                continue
            item = {key: row[key] for key in ('text', 'channel', 'quality', 'date', 'added_at',
                    'confirmed_at', 'source') if key in row}
            item['context'] = 'work' if email else row.get('context', 'unknown')
            if attribution:
                for key in ('canonical_id', 'recipient'):
                    if isinstance(row.get(key), str):
                        item[key] = row[key]
            item['bucket'] = style_extract.bucket_for_sample(item)
            kept.append(item)
        return kept
    canonical = {bucket: [] for bucket in style_extract.ALL_BUCKETS}
    for rows in (style.get('canonical') or {}).values():
        for sample in samples(rows):
            canonical[sample['bucket']].append(sample)
    recent, confirmed = samples(style.get('recent') or []), samples(style.get('confirmed') or [])
    pool = [sample for rows in canonical.values() for sample in rows]
    projected = {'canonical': canonical, 'recent': recent, 'confirmed': confirmed,
        'per_person': style_extract._rebuild_per_person(pool + recent) if attribution else {},
        'master_by_context': {bucket: style_extract.analyze_master_style([sample['text'] for sample in rows]) if rows else {}
                              for bucket, rows in canonical.items()}}
    def check():
        if any(bool(allowed(source)) != value for source, value in original.items()):
            raise ValueError('style permission changed')
    return projected, check


def apply(req: dict, *, permitted=None) -> dict:
    path = os.path.join(_root(), "style.json")
    style = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            style = json.load(f)

    check = None
    if permitted is not None:
        style, check = _project_style(style, permitted)
    channel = req.get("channel", "imessage")
    recipient = (req.get("recipient") or "").lower()
    cid = req.get("canonical_id")
    per_person = style.get("per_person") or {}
    person = per_person.get(cid) or per_person.get(f"name:{recipient}") or \
        next((p for p in per_person.values() if (p.get("name") or "").lower() == recipient), None)

    if permitted is not None:
        if cid:
            person = per_person.get(cid)
        else:
            matches = [p for p in per_person.values() if (p.get("name") or "").lower() == recipient]
            person = matches[0] if len(matches) == 1 else None

    bucket = _bucket(channel, person.get("context") if person else req.get("context"))
    canonical = (style.get("canonical") or {}).get(bucket, [])
    recent = [s for s in (style.get("recent") or []) if s.get("bucket") == bucket]
    confirmed = [s for s in (style.get("confirmed") or []) if s.get("bucket") == bucket]
    master = (style.get("master_by_context") or {}).get(bucket) or style.get("master") or {}

    parts = ["## How to write this — match the user's actual voice"]
    source = "bucket"
    quoted_texts = set()
    if person and person.get("samples"):
        parts += _quote(person["samples"], 8,
                        f"How the user actually writes to {person.get('name') or recipient} — match this voice, length, punctuation, and tone exactly", quoted_texts)
        if person.get("notes"):
            parts.append("(" + "; ".join(person["notes"]) + ")")
        source = "per_person"

    if confirmed:
        # Newest first: `confirmed` is appended to, so list order is oldest-first and the four quoted
        # would otherwise freeze on the first four ever confirmed.
        confirmed.sort(key=lambda s: str(s.get("confirmed_at") or ""), reverse=True)
        parts += _quote(confirmed, 4, "Past shipped drafts (strong voice examples, not current facts)", quoted_texts)
        if source == "bucket":
            source = "confirmed"

    has_person = bool(person and person.get("samples"))
    can_budget, rec_budget = (4, 2) if has_person else (7, 3)
    canon_fresh = sorted(canonical, key=lambda s: s.get("quality", 0), reverse=True)
    if canon_fresh:
        parts += _quote(canon_fresh, can_budget, f"How the user writes {bucket.replace('_', ' ')}s", quoted_texts)
        if source == "bucket":
            source = "canonical"
    if recent:
        parts += _quote(recent, rec_budget, "Recent messages (freshest voice)", quoted_texts)
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

    # A stated tone instruction is authority, not a style inference. Brief composition already
    # honors this same explicit list; reply drafting must receive it too or "keep it terse" applies
    # to the digest while the proposed reply ignores it.
    tone_notes = preferences.load_explicit().get("tone_notes") or []
    if tone_notes:
        parts.append("\n### Explicit tone instructions")
        parts += [f"- {note}" for note in tone_notes]

    # (style.json's `preferences` list — "learned preferences from past edits" — is carried forward
    # by the extractor but has NO writer; the edit-diff arc is still open. Nothing is rendered from
    # it here: a prompt section with no producer is dead real estate on the drafter's hottest path.)

    if source == "bucket" and (permitted is not None or len(parts) == 1):
        # Nothing learned yet — give a minimal honest instruction.
        parts.append("(No writing samples yet — write concise and natural; mirror the incoming message's register.)")
    parts.append(render_feedback() if permitted is None else render_feedback(permitted=permitted))
    parts.append('\nWrite as if you ARE this person. The samples above guide style only, not current facts, recipients, or completed owner actions. Use the actual current thread and explicit owner statements for factual claims, preserving their qualifiers.')

    if check is not None:
        check()
    return {"guidance": "\n".join(parts), "bucket": bucket, "source": source,
            "master": master, "has_person": has_person, "tone_notes": tone_notes}


if __name__ == "__main__":
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    print(json.dumps(apply(json.loads(raw))))
