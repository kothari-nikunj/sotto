#!/usr/bin/env python3
"""
ledger_io.py — shared READ helpers for the continuity ledger ($SOTTO_DATA/knowledge/continuity/*.md).

One place for the load/parse/age logic that used to be copy-pasted across retune_scan.py,
loops_query.py, and continuity_resolve.py (drift between the copies = loops silently disagreeing
about what's open). Read-only: writing/resolution stays in continuity_resolve.py (`_persist`).

Exports:
  ACTIVE / TERMINAL         — the status sets (continuity.rs:227/230)
  normalize_action_type(t)  — the ONE spelling normalizer (variants → canonical types)
  WAITING_ON_TYPES          — the ONE direction predicate: what the user is OWED
  MEETING_TYPES / SCHEDULING_TYPES — the rest of the CLOSED family vocabulary (continuity_resolve's
                              `action_family` maps everything else onto the owed-by-you family)
  CHASE_MAX                 — how many chases a waiting_on gets before it hands off to sotto-loops
  ledger_dir()              — the ledger directory under $SOTTO_DATA
  parse_frontmatter(content)— YAML frontmatter → dict; None when there is no frontmatter block OR
                              the block is malformed (load_entries tells the two apart and flags
                              malformed entries so writers never persist over them)
  load_entries(...)         — every ledger file's frontmatter, sorted by path (deterministic)
  load_active()             — the entries the read views surface: ACTIVE status, not snoozed
  CHASE_STATE_FIELDS        — everything the chase lane has spent on a loop, cleared as one
  age_days(created_at, today) — whole days old vs an aware "today" (naive created_at → UTC)
"""
from __future__ import annotations

import glob
import os
import sys
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from textutil import _s  # noqa: E402
from timeutil import _now_local, _parse_ts, configured_tz  # noqa: E402  (ages match the brief)

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

ACTIVE = {"open", "waiting", "failed", "blocked"}      # continuity.rs:227
TERMINAL = {"resolved", "dismissed", "expired"}        # continuity.rs:230

# ── Direction, defined ONCE ──────────────────────────────────────────────────────────────────────
# One sentence: `waiting_on` is the only type where the OTHER side owes the user, so it is the only
# one that is chased instead of age-expired — everything else (reply / follow_up / follow_up_stale /
# call_back / …) is a debt the USER owes. `follow_up_stale` reads like "they went quiet" but its
# semantics are "you owe them a nudge": it resolves on the user's OUTGOING message and expires on
# the user's clock, so it lives in you_owe, here and in every read view.
WAITING_ON_TYPES = {"waiting_on"}
CHASE_MAX = 2                                          # then it hands off to sotto-loops

# ── The CLOSED family vocabulary (the anchor's middle component) ─────────────────────────────────
# One sentence: a debt belongs to one of four families — owed-to-you, a calendar shadow, a
# scheduling ask, or owed-by-you — and anything the extractor invents falls into owed-by-you rather
# than minting a family of its own.
#
# The brief's `type` is a free string in the responseSchema, and a real volume showed ~19 distinct
# values (`action`, `task`, `review`, `read`, `info`, `reminder`, `document_mention`,
# `action_required`, `email_follow`, …). `action_family` used to return an unrecognized type
# VERBATIM, so every new word the model reached for forked a fresh anchor and the same debt opened
# a second row. These two sets plus WAITING_ON_TYPES are the whole vocabulary; the default closes it.
MEETING_TYPES = {"meeting_prep", "meeting_info", "meeting", "calendar"}
SCHEDULING_TYPES = {"schedule", "reschedule", "propose_times", "rsvp"}

# Everything the chase lane has SPENT on one loop — the two nudges, the clock between them, the
# undelivered proposal, and the hand-off question that ends the lane. Named once because they are
# cleared together or not at all: a re-opened anchor and the user's "keep waiting" both mean "start
# this loop's chase story over", and a list that is clipped in one caller and not the other leaves a
# loop that can never be chased again or one that is never asked about twice.
CHASE_STATE_FIELDS = ("chased_count", "chase_after", "last_chased_at", "chase_pending",
                      "handoff_asked_at")

# The OTHER row-level predicate lives one directory over, in `_shared/lib/brief_validate.is_urgent`
# (overdue / due within 24h / already chased) — it belongs to the BRIEF's contract about which loops
# earn a line, it is pure and stdlib-only there, and both the validator and the composer already
# import that module. Named here so nobody writes a second copy looking for it in the ledger's own
# helpers. One concept, one implementation.


def normalize_action_type(t) -> str:
    """Collapse model-emitted VARIANTS of the FLEX action-type vocabulary onto the canonical types.
    The brief's responseSchema leaves `type` a free string, so Gemini emits channel-suffixed reply
    forms (reply_message, reply_email — seen in real runs) alongside the documented "reply", plus
    the occasional hyphen/space spelling of follow_up / call_back. Anchor-keying, cross-channel
    resolution AND every read view key off this: a variant normalized in the writer but not in a
    reader is an item chased by one lane and filed under the wrong direction by the other."""
    t = (t if isinstance(t, str) else ("" if t is None else str(t)))
    t = t.lower().strip().replace("-", "_").replace(" ", "_")
    if t.startswith("reply"):
        return "reply"              # reply_message / reply_email / reply_imessage → reply
    if t in ("followup", "followup_stale"):
        return t.replace("followup", "follow_up")
    if t in ("callback", "call"):
        # A bare "call" is the same debt as "call_back" — and naming it so is what routes it
        # through the call-history resolver instead of leaving it to age out unanswered.
        return "call_back"
    if t in ("follow_up_stalled", "followup_stalled"):
        return "follow_up_stale"
    if t == "propose_time":
        return "propose_times"
    return t


def is_waiting_on(t) -> bool:
    """True when the OTHER side owes the user — the one direction that is chased, never age-expired."""
    return normalize_action_type(t) in WAITING_ON_TYPES


def ledger_dir() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "knowledge", "continuity")


def parse_frontmatter(content: str):
    """The YAML frontmatter of one ledger file. Returns a dict only when the frontmatter parses to
    a mapping (a valid empty mapping `{}` included). Returns None both for a bare file (no
    frontmatter block at all) and for a MALFORMED one (unclosed fence, YAML error, non-mapping):
    malformed metadata must never masquerade as an empty-but-valid entry — continuity_resolve used
    to treat it as a status-less open item and rewrite the file as '---\\n{}\\n---', destroying the
    user's content. load_entries distinguishes the two shapes via the opening fence."""
    if not isinstance(content, str) or not content.startswith("---"):
        return None
    end = content.find("\n---", 3)
    if end == -1:
        return None        # opening fence but no closing one — malformed, not a bare file
    try:
        fm = yaml.safe_load(content[4:end])
    except Exception:
        return None
    return fm if isinstance(fm, dict) else None


def load_entries(with_path: bool = False, include_bare: bool = False) -> list:
    """Frontmatter dicts for every *.md in the ledger, sorted by path. Unreadable files are
    skipped. Files with no valid frontmatter are skipped unless include_bare=True
    (continuity_resolve historically keys bare files by filename; the read views ignore them).
    MALFORMED files (a fence that doesn't parse to a mapping) are likewise only surfaced with
    include_bare=True, and carry {"_malformed": True} so writers (continuity_resolve) can skip
    them instead of persisting over the broken file. with_path=True adds the source path under
    "_path" (continuity_resolve persists back to it)."""
    if yaml is None:  # pragma: no cover — the read views degrade to empty without PyYAML
        return []
    out = []
    for path in sorted(glob.glob(os.path.join(ledger_dir(), "*.md"))):
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        fm = parse_frontmatter(content)
        if fm is None:
            if not include_bare:
                continue
            # A fence that didn't parse to a mapping = malformed; no fence at all = bare.
            fm = {"_malformed": True} if content.startswith("---") else {}
        if with_path:
            fm["_path"] = path
        out.append(fm)
    return out


def load_active() -> list:
    """The entries the read views surface: ACTIVE status AND not snoozed past today. Missing status
    counts as open (matching continuity_resolve's default).

    The SNOOZE lives here, not in each caller. The brief, `sotto-loops` and the cleanup scan are
    three views of one ledger, and the brief's used to be the one that ignored `snoozed_until` — so
    a loop the user had explicitly parked was named as urgent in the brief and counted in the line
    that says how many are open, while the dashboard the line points at showed neither. One concept,
    one implementation: hidden is hidden, in every view."""
    today = _now_local(configured_tz() or "+00:00").strftime("%Y-%m-%d")
    return [fm for fm in load_entries()
            if fm.get("status", "open") in ACTIVE
            and not (_s(fm.get("snoozed_until"))[:10] > today)]


def age_days(created_at, today):
    """Whole days between created_at and `today` (an AWARE datetime, i.e. _now_local(...)).
    A naive created_at is treated as UTC. None when created_at doesn't parse."""
    d = _parse_ts(_s(created_at))
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return max(0, (today - d).days)
