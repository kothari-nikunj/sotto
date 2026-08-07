#!/usr/bin/env python3
"""
ledger_io.py — shared READ helpers for the continuity ledger ($SOTTO_DATA/knowledge/continuity/*.md).

One place for the load/parse/age logic that used to be copy-pasted across retune_scan.py,
loops_query.py, and continuity_resolve.py (drift between the copies = loops silently disagreeing
about what's open). Read-only: writing/resolution stays in continuity_resolve.py (`_persist`).

Exports:
  ACTIVE / TERMINAL         — the status sets (continuity.rs:227/230)
  ledger_dir()              — the ledger directory under $SOTTO_DATA
  parse_frontmatter(content)— YAML frontmatter → dict; None when there is no frontmatter block OR
                              the block is malformed (load_entries tells the two apart and flags
                              malformed entries so writers never persist over them)
  load_entries(...)         — every ledger file's frontmatter, sorted by path (deterministic)
  load_active()             — only entries whose status is ACTIVE (what the read views surface)
  age_days(created_at, today) — whole days old vs an aware "today" (naive created_at → UTC)
"""
from __future__ import annotations

import glob
import os
import sys
from datetime import timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose_brief as cb  # noqa: E402  (shared _parse_ts/_s so ages match the brief)

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

ACTIVE = {"open", "waiting", "failed", "blocked"}      # continuity.rs:227
TERMINAL = {"resolved", "dismissed", "expired"}        # continuity.rs:230


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
    """The ACTIVE entries — what loops_query/retune_scan surface. Missing status counts as open
    (matching continuity_resolve's default)."""
    return [fm for fm in load_entries() if fm.get("status", "open") in ACTIVE]


def age_days(created_at, today):
    """Whole days between created_at and `today` (an AWARE datetime, i.e. cb._now_local(...)).
    A naive created_at is treated as UTC. None when created_at doesn't parse."""
    d = cb._parse_ts(cb._s(created_at))
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return max(0, (today - d).days)
