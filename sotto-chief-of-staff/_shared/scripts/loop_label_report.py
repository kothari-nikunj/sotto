#!/usr/bin/env python3
"""loop_label_report.py — how often each "maybe done" hint was right, from your own decisions.

Usage: loop_label_report.py [--days N]   (read-only; default 90, the outcomes retention window)

Reads the content-free `loop_label` rows in outcomes.jsonl. For each hint basis it counts the
loops you then closed, kept open (keep or snooze) or dismissed; precision is closed / (closed +
kept open). It also counts closes with no hint (a signal Sotto missed) and loops you added
yourself (an ask Sotto never captured). A hint earns automatic closing only on this evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import log_outcome  # noqa: E402
import source_context  # noqa: E402

KEPT = ("keep", "snooze")


def _rows(path):
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("outcome") == "loop_label":
            out.append(row)
    return out


def _permitted(row) -> bool:
    allowed = source_context.allowed
    hint = row.get("hint") or {}
    channel = str(row.get("channel") or "")
    return ((channel in ("manual", "other") or allowed(channel))
            and (not hint or allowed(str(hint.get("source") or ""))))


def build(now: datetime | None = None, days: int = 90) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    rows = [row for row in _rows(log_outcome._path())
            if (log_outcome._dt(row.get("ts")) or since) >= since and _permitted(row)]
    hints, unsignalled, added, undone = {}, 0, 0, {}
    for row in rows:
        decision = row.get("decision")
        basis = (row.get("hint") or {}).get("basis")
        if decision == "added":
            added += 1
        elif decision == "reopened":
            undone[basis or "unknown"] = undone.get(basis or "unknown", 0) + 1
        elif basis:
            counts = hints.setdefault(basis, {"closed": 0, "kept_open": 0, "dismissed": 0})
            if decision == "resolved":
                counts["closed"] += 1
            elif decision in KEPT:
                counts["kept_open"] += 1
            elif decision == "dismissed":
                counts["dismissed"] += 1
        elif decision == "resolved":
            unsignalled += 1
    for counts in hints.values():
        judged = counts["closed"] + counts["kept_open"]
        counts["precision"] = round(counts["closed"] / judged, 2) if judged else None
    return {"days": days, "decisions": len(rows), "hints": hints,
            "closed_without_hint": unsignalled, "added_by_you": added, "auto_closes_undone": undone}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    print(json.dumps(build(days=parser.parse_args().days), indent=2))


if __name__ == "__main__":
    main()
