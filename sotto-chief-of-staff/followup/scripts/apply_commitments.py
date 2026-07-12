#!/usr/bin/env python3
"""
apply_commitments.py — write a followup's extracted commitments STRAIGHT into the continuity ledger.

Before this, compose_followup's `commitments[]` only reached the ledger if the NEXT brief happened to
carry them through its continuity payload — a day of lag and an easy drop. This is the deterministic
write: each commitment becomes a ledger item under $SOTTO_DATA/knowledge/continuity/ in the SAME
markdown+YAML-frontmatter shape continuity_resolve.py maintains (we reuse its loader/persister and
anchor_key machinery, so the brief's resolution sweep and sotto-loops read them natively).

Anchoring / dedupe:
  - commitment with a recipient email → the contact anchor plus a content hash,
    `email:<family>:id:<email>:c:<sha256(owner|what)[:12]>` — two DISTINCT commitments to the same
    person get distinct anchors (a bare contact anchor collapsed them and silently dropped the
    second), while re-running the same payload still hashes to the same anchor and dedupes/bumps.
    This intentionally trades cross-source dedupe against a brief-created email anchor for never
    losing a commitment.
  - commitment with no recipient → a stable synthetic thread anchor
    `thread:commitment:<sha256(meeting|owner|what)[:12]>` — distinct commitments never collapse, and
    re-running the apply (or a later brief run) can't duplicate them.
  - an existing ACTIVE item just gets its times_surfaced bumped; a TERMINAL (resolved/dismissed)
    item is left alone — we never resurrect something the user already closed.

Direction: the user's own commitments become `follow_up` (you owe); another attendee's become
`waiting_on` (they owe you) — exactly the split loops_query.py surfaces. Writes files only —
NEVER sends anything.

Usage:
    apply_commitments.py /tmp/sotto_followup.json --user-email you@example.com
    (the file is compose_followup.py's full output, or any {"commitments":[...]} / bare array)
Prints {"written": N, "deduped": M, "skipped_terminal": K, "anchor_keys": [...]}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "morning-brief", "scripts"))
import continuity_resolve as cr  # noqa: E402

_USER_ALIASES = {"user", "the user", "me", "you", "i", "myself", "self"}

_s = cr._s   # the shared string coercion (also ISO-stringifies YAML dates), not a private copy


def _owner_is_user(owner: str, user_email: str) -> bool:
    o = _s(owner).strip().lower()
    if not o:
        return True   # unowned → treat as the user's (they're the one following up)
    if o in _USER_ALIASES:
        return True
    ue = _s(user_email).strip().lower()
    if ue and (o == ue or o == ue.split("@")[0]):
        return True
    return False


def _action_for(c: dict, user_email: str, today: str):
    """Map one commitment {meeting, owner, what, due, to_email} → a snake_case ledger action."""
    what = _s(c.get("what")).strip()
    if not what:
        return None
    meeting = _s(c.get("meeting")).strip()
    owner = _s(c.get("owner")).strip()
    is_user = _owner_is_user(owner, user_email)
    to_email = _s(c.get("to_email")).strip().lower()
    due = _s(c.get("due")).strip()
    # Only an ISO-ish date becomes a hard deadline (continuity's expiry compares date strings);
    # a fuzzy "Friday" stays in the summary instead of mis-expiring the loop.
    deadline = due if (due.startswith("20") and len(due) >= 10) else None

    summary = f"You committed to: {what}" if is_user else f"{owner or 'They'} owes: {what}"
    if meeting:
        summary += f' (from "{meeting}")'
    if due and not deadline:
        summary += f" — due {due}"

    action = {
        "action_type": "follow_up" if is_user else "waiting_on",
        "channel": "email" if to_email else "followup",
        "contact_identifier": to_email or None,
        "contact_name": (owner if not is_user else "") or (to_email.split("@")[0] if to_email else meeting),
        "summary": summary,
        "deadline": deadline,
        "created_at": today,
    }
    if not to_email:
        # Stable synthetic anchor: distinct no-recipient commitments never collapse, re-runs dedupe.
        h = hashlib.sha256(f"{meeting}|{owner}|{what}".encode()).hexdigest()[:12]
        action["source_thread_id"] = f"commitment:{h}"
    else:
        # Content-aware suffix for recipient commitments: the bare contact anchor made every
        # commitment sharing a to_email collapse onto ONE key (follow_up and waiting_on share the
        # follow_up family), silently dropping all but the first. Same payload → same hash → still
        # dedupes/bumps on re-apply. Intentional trade: this anchor no longer merges with a
        # brief-created `email:follow_up:id:<email>` loop — never losing a commitment wins.
        action["_anchor_suffix"] = ":c:" + hashlib.sha256(f"{owner}|{what}".encode()).hexdigest()[:12]
    return action


def apply(payload, user_email: str = "", now: datetime | None = None) -> dict:
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    commitments = payload.get("commitments", []) if isinstance(payload, dict) else (payload or [])
    if not commitments:   # nothing to write — don't even load the ledger
        return {"written": 0, "deduped": 0, "skipped_terminal": 0, "anchor_keys": []}

    items = cr._load_items()
    written, deduped, skipped_terminal, anchors = 0, 0, 0, []
    for c in commitments:
        if not isinstance(c, dict):
            continue
        raw = _action_for(c, user_email, today)
        if raw is None:
            continue
        a = cr._normalize_action(raw)
        ak = cr.compute_anchor_key(a) + raw.get("_anchor_suffix", "")
        anchors.append(ak)
        existing = items.get(ak)
        if existing is not None:
            if _s(existing.get("status", "open")) in cr.TERMINAL:
                skipped_terminal += 1    # the user already closed this — never resurrect it
                continue
            existing["times_surfaced"] = int(existing.get("times_surfaced", 1)) + 1
            cr._persist(existing)
            deduped += 1
            continue
        it = {
            "anchor_key": ak, "action_type": a.get("action_type"), "channel": a.get("channel"),
            "contact_name": a.get("contact_name"), "contact_identifier": a.get("contact_identifier"),
            "canonical_id": a.get("canonical_id"), "status": "open",
            "created_at": a.get("created_at") or today, "times_surfaced": 1,
            "summary": a.get("summary", ""), "ask": a.get("ask"),
            "meeting_time": a.get("meeting_time"), "deadline": a.get("deadline"),
            "source_thread_id": a.get("source_thread_id"),
            "source": "followup_commitment",
        }
        items[ak] = it
        cr._persist(it)
        written += 1

    return {"written": written, "deduped": deduped,
            "skipped_terminal": skipped_terminal, "anchor_keys": anchors}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("payload", nargs="?", help="compose_followup output JSON (default: stdin)")
    ap.add_argument("--user-email", dest="user_email", default="")
    a = ap.parse_args()
    try:
        raw = open(a.payload, encoding="utf-8").read() if a.payload else sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    print(json.dumps(apply(payload, a.user_email)))


if __name__ == "__main__":
    main()
