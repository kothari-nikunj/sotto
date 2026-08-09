#!/usr/bin/env python3
"""
learn_preferences.py — derive behavioral rules from outcomes (parity C1).

PORT SOURCE: api/src/services/preference-learner.ts + feedback.ts
Reads $SOTTO_DATA/outcomes.jsonl, writes $SOTTO_DATA/preferences.json. The morning brief's Learn
step runs this after every brief (fast + idempotent; a missing/empty outcomes.jsonl is a no-op).
The skills read preferences.json to adjust approval tiers and style.

Signals captured (mirrors FeedbackSummary):
  - deprioritization: contacts/action_types frequently dismissed
  - approval_defaults: per (contact, action_type) tier the user actually accepts at — emitted only
    with enough signal (>= MIN_ACCEPTED_FOR_DEFAULT accepted outcomes at >= MIN_ACCEPT_RATE
    acceptance). A learned default may relax review → one_tap for that exact contact+action_type;
    it is clamped so it can NEVER grant `auto` and never emits `forbidden`. Explicit user
    preferences (the reserved `explicit` block) always win and are carried forward untouched.
    NEVER_RELAX classes (a decline) are pinned to `review` no matter what the user accepted.
  - style_feedback: counts of edited_and_sent (drives style refinement)
  - analytics: completion_rate, counts

A rule you delete stays deleted. The three lists above are REBUILT from scratch on every run, so a
rule the user removed in the dashboard (/api/prefs) would otherwise reappear the next morning. The
dashboard records each such delete in the top-level `suppressed` list — [{list, value}, …] — and
this learner filters those values back out, then carries the list forward untouched.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict

# approval_defaults thresholds: enough accepted signal, and the user almost always accepts.
MIN_ACCEPTED_FOR_DEFAULT = 3
MIN_ACCEPT_RATE = 0.8
# Tiers a learned default may record. "auto" is clamped to "one_tap" (behavior alone never earns
# full autonomy); "forbidden" is never learnable.
LEARNABLE_TIERS = {"one_tap", "review"}

# ── The never-relax guard ────────────────────────────────────────────────────────────────────────
# Some action classes are `review` FOREVER: however many the user accepts, habit must never earn
# them a one-tap. A DECLINE is the first — sending a "no" you didn't read is unrecoverable in a way
# sending a "yes" isn't (_shared/references/approval-tiers.md; sotto-draft-reply's no-draft rule).
# Keying: outcomes are keyed `contact|action_type`, so the guard keys on the action_type half — the
# smallest true mechanism, no new state. Drafters type a no by logging `action_type: "decline"`;
# matching is per word, so decline / decline_reply / reply_decline all count.
NEVER_RELAX = {"decline"}
NEVER_RELAX_TIER = "review"
_WORDS = re.compile(r"[^a-z0-9]+")


def _never_relax(action_type) -> bool:
    """True when this action_type names a class learning may never relax."""
    return bool(NEVER_RELAX & set(_WORDS.split(str(action_type or "").lower())))


def _root():
    return os.environ.get("SOTTO_DATA", "/data")


def _suppressed(existing: dict) -> dict:
    """{list name → {values the user deleted}} from preferences.json's top-level `suppressed`.
    Malformed entries are ignored rather than fatal — a tombstone list is a convenience, never a
    reason to abort a Learn run."""
    out = {}
    for item in (existing.get("suppressed") if isinstance(existing, dict) else None) or []:
        if isinstance(item, dict) and isinstance(item.get("list"), str) and item.get("value") is not None:
            out.setdefault(item["list"], set()).add(str(item["value"]))
    return out


def learn() -> dict:
    path = os.path.join(_root(), "outcomes.jsonl")
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:  # skip a corrupt/partial appended line rather than crash all learning
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    path = os.path.join(_root(), "preferences.json")

    # NO-OP on a missing/empty outcomes log: never rewrite preferences.json from zero rows (that
    # would wipe previously-learned fields, e.g. after a log rotation). Safe for the brief's Learn
    # step to run unconditionally.
    if not rows:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f) or {}
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    dismissed = defaultdict(int)
    accepted = defaultdict(int)
    edits = defaultdict(int)
    accepted_tiers = defaultdict(Counter)   # (contact|action_type) → tiers the user accepted at
    total = len(rows)
    completed = 0

    for r in rows:
        key = f"{r.get('contact','')}|{r.get('action_type','')}"
        oc = r.get("outcome")
        if oc == "dismissed":
            dismissed[key] += 1
        elif oc in ("executed", "edited_and_sent"):
            accepted[key] += 1
            completed += 1
            if oc == "edited_and_sent":
                edits[key] += 1
            tier = str(r.get("tier") or "").strip().lower()
            if tier:
                accepted_tiers[key][tier] += 1

    # Deprioritize anything dismissed >=3x and rarely accepted
    deprioritize = [k for k, c in dismissed.items() if c >= 3 and accepted.get(k, 0) <= c // 2]

    # approval_defaults: per (contact, action_type), the tier the user actually accepts at — only
    # with enough signal (>=3 accepts, >=80% acceptance among decided outcomes). Clamped: "auto" is
    # recorded as "one_tap" (learning never grants full autonomy) and "forbidden" is never learned.
    approval_defaults = {}
    for key, acc in accepted.items():
        decided = acc + dismissed.get(key, 0)
        if acc < MIN_ACCEPTED_FOR_DEFAULT or acc / decided < MIN_ACCEPT_RATE:
            continue
        if not accepted_tiers[key]:
            continue   # no tier recorded on the accepted outcomes → nothing to default to
        tier = accepted_tiers[key].most_common(1)[0][0]
        if tier == "auto":
            tier = "one_tap"
        if _never_relax(key.rsplit("|", 1)[-1]):
            tier = NEVER_RELAX_TIER   # a decline stays `review` however many the user accepted
        if tier in LEARNABLE_TIERS:
            approval_defaults[key] = tier

    # Read the existing file BEFORE building the new one: it carries both the user's EXPLICIT block
    # and the `suppressed` tombstones the rebuilt lists must respect. If it's present but unreadable
    # (truncated/corrupt), ABORT without writing — minting a fresh file here would silently drop the
    # user's explicit block. A corrupt file must be repaired (or explicitly rewritten via
    # sotto-feedback), never papered over by this writer.
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f) or {}
        except (OSError, json.JSONDecodeError, ValueError):
            print("[learn_preferences] preferences.json exists but is unreadable — "
                  "aborting without writing (the explicit block would be lost)", file=sys.stderr)
            return {}

    # A rule you delete stays deleted: drop every value the dashboard tombstoned, per list.
    sup = _suppressed(existing)
    prefs = {
        "deprioritization_hints": [k for k in deprioritize
                                   if k not in sup.get("deprioritization_hints", ())],
        "approval_defaults": {k: v for k, v in approval_defaults.items()
                              if k not in sup.get("approval_defaults", ())},
        "edit_heavy": [k for k, c in edits.items()
                       if c >= 3 and k not in sup.get("edit_heavy", ())],
        "analytics": {
            "total_outcomes": total,
            "completion_rate": round(completed / total, 3) if total else 0.0,
        },
        "version": 1,
    }
    # Carry forward the EXPLICIT preferences (mutes / tone the user stated via sotto-feedback) and
    # the tombstones themselves. This learner rewrites preferences.json wholesale from behavior;
    # neither may be wiped.
    if isinstance(existing.get("explicit"), dict):
        prefs["explicit"] = existing["explicit"]
    if isinstance(existing.get("suppressed"), list):
        prefs["suppressed"] = existing["suppressed"]
    os.makedirs(_root(), exist_ok=True)
    tmp = path + ".tmp"   # tmp + atomic rename (preferences.py _save pattern): a crash mid-write
    with open(tmp, "w", encoding="utf-8") as f:   # must never leave a truncated preferences.json
        json.dump(prefs, f, indent=2)
    os.replace(tmp, path)
    return prefs


if __name__ == "__main__":
    print(json.dumps(learn()))
