#!/usr/bin/env python3
"""
keys.py — the stable ids two processes must compute IDENTICALLY.

The skills tree and the receiver image are separate runtimes: the receiver must render the Cadence
waiting room and the Voice card even when the skills tree isn't on the box at all, so it cannot
import this file. It VENDORS it instead — `runtime/trigger-receiver/keys.py` is a byte-identical
copy, and `tests/test_docs_drift.py` fails the suite if the two files diverge. Copy-plus-guard, not
hand-synced logic: change this file, copy it across, in the same commit.

Stdlib only (no PyYAML, no sibling imports) — that is what makes the vendoring safe.

  queue_key(line)  — the id of ONE queue.jsonl line (triage_event --promote ⇄ the dashboard's
                     "nudge me now": the dashboard hashes the exact bytes it rendered, so an entry
                     that already left the queue simply stops matching).
  sample_key(s)    — the dedup key of ONE style sample (channel|date|recipient|text[:120]).
  sample_hash(s)   — the short id of that sample (style_extract --confirm ⇄ the Voice card's
                     "confirm this one": both hash the same style.json fields).
"""
from __future__ import annotations

import hashlib


def queue_key(line: str) -> str:
    """sha256 of the raw queue.jsonl line, truncated to 16 hex chars."""
    return hashlib.sha256(line.strip().encode("utf-8")).hexdigest()[:16]


def _str(v) -> str:
    """A non-string field is no field. (Both callers were already defensive here, differently.)"""
    return v if isinstance(v, str) else ""


def sample_key(s: dict) -> str:
    """The fields that make two style samples the same sample."""
    return "|".join([_str(s.get("channel")), _str(s.get("date"))[:10],
                     _str(s.get("recipient")).lower(), _str(s.get("text"))[:120].lower()])


def sample_hash(s: dict) -> str:
    """sha256 of sample_key(s), truncated to 16 hex chars."""
    return hashlib.sha256(sample_key(s).encode("utf-8")).hexdigest()[:16]
