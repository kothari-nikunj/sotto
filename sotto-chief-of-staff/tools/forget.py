#!/usr/bin/env python3
"""
forget.py — delete Sotto's exhaust from `$SOTTO_DATA`, one named category at a time.

`docs/DATA-FLOW.md` promises the reader they can delete what Sotto keeps. This is the tool that
promise points at. It lives here, in `sotto-chief-of-staff/tools/`, and not in the repo-root
`tools/` — the root one is build machinery that the publish generator strips, and a retention tool
nobody who installed Sotto can run is not a retention tool.

    SOTTO_DATA=~/SottoData python3 tools/forget.py --snapshot
    SOTTO_DATA=~/SottoData python3 tools/forget.py --all

Verbs (combine freely; `--all` is every one of them):

    --snapshot    delete knowledge/last_local_snapshot.json — the RAW Bridge payload, the widest
                  file Sotto keeps and the one DATA-FLOW.md singles out
    --caches      delete cache/research_*.json and cache/calendar_today.json — render caches, both
                  rebuilt on the next run
    --logs        TRUNCATE logs/compose_brief.log — truncate, not delete: a running receiver or a
                  mid-flight brief holds the file open, and unlinking it under them would send
                  every later line to a file nobody can read
    --receipts    delete events/delivery.jsonl and events/sends.jsonl — the "did it land" and
                  "what did Sotto send" ledgers

Prints ONE JSON object saying exactly what went, with byte counts, and exits 0 — including when
there was nothing to do, because "already clean" is a success, not an error.

WHAT IT WILL NOT TOUCH, ever: `knowledge/people/`, `knowledge/companies/`, `knowledge/continuity/`.
That is the memory — who someone is, what a company builds, what you still owe whom. Deleting it is
a decision a person makes about their own graph, not hygiene a script performs, so it belongs to
`knowledge_edit.py` and the dashboard, not here. The guard below enforces it on every path.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

# Never a target, whatever a future verb asks for. Checked per path, not per verb, so a typo'd
# glob can't reach the graph either.
PROTECTED = ("knowledge/people", "knowledge/companies", "knowledge/continuity")

VERBS = ("snapshot", "caches", "logs", "receipts")


def _root() -> str:
    return os.environ.get("SOTTO_DATA", "/data")


def _protected(rel: str) -> bool:
    rel = rel.replace(os.sep, "/")
    return any(rel == p or rel.startswith(p + "/") for p in PROTECTED)


def _targets(verbs: set) -> list:
    """[(relative path, "deleted"|"truncated")] for the selected verbs — globs expanded against the
    real volume, so what the caller sees named in the summary is what existed."""
    root = _root()
    out = []

    def add(pattern: str, action: str) -> None:
        for path in sorted(glob.glob(os.path.join(root, *pattern.split("/")))):
            if os.path.isfile(path):
                out.append((os.path.relpath(path, root).replace(os.sep, "/"), action))

    if "snapshot" in verbs:
        add("knowledge/last_local_snapshot.json", "deleted")
    if "caches" in verbs:
        add("cache/research_*.json", "deleted")
        add("cache/calendar_today.json", "deleted")
    if "logs" in verbs:
        add("logs/compose_brief.log", "truncated")
    if "receipts" in verbs:
        add("events/delivery.jsonl", "deleted")
        add("events/sends.jsonl", "deleted")
    return out


def forget(verbs) -> dict:
    """Apply the verbs; return the summary dict that main() prints. Every removal is independent —
    one unwritable file is reported and the rest still go."""
    root = _root()
    verbs = set(verbs)
    unknown = verbs - set(VERBS)
    if unknown:
        raise ValueError(f"unknown verb(s): {sorted(unknown)}")

    removed, errors = [], []
    for rel, action in _targets(verbs):
        if _protected(rel):   # unreachable by construction; the guard is the point
            errors.append({"path": rel, "error": "protected: this is memory, not exhaust"})
            continue
        path = os.path.join(root, *rel.split("/"))
        try:
            size = os.path.getsize(path)
            if action == "truncated":
                with open(path, "w"):
                    pass
            else:
                os.remove(path)
        except OSError as e:
            errors.append({"path": rel, "error": f"{type(e).__name__}: {e}"})
            continue
        removed.append({"path": rel, "bytes": size, "action": action})

    return {
        "data_root": root,
        "verbs": sorted(verbs),
        "removed": removed,
        "count": len(removed),
        "bytes": sum(r["bytes"] for r in removed),
        "errors": errors,
        "kept": list(PROTECTED),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="forget.py",
        description="Delete Sotto's exhaust from $SOTTO_DATA. Never touches the knowledge graph.")
    p.add_argument("--snapshot", action="store_true", help="knowledge/last_local_snapshot.json")
    p.add_argument("--caches", action="store_true",
                   help="cache/research_*.json, cache/calendar_today.json")
    p.add_argument("--logs", action="store_true", help="truncate logs/compose_brief.log")
    p.add_argument("--receipts", action="store_true",
                   help="events/delivery.jsonl, events/sends.jsonl")
    p.add_argument("--all", action="store_true", help="every verb above")
    args = p.parse_args(argv)

    verbs = set(VERBS) if args.all else {v for v in VERBS if getattr(args, v)}
    if not verbs:
        # Nothing named is not "delete nothing" — it is a caller who meant something. Say so
        # rather than print an empty success a script would read as done.
        p.print_usage(sys.stderr)
        print("forget.py: name at least one verb (--snapshot/--caches/--logs/--receipts/--all)",
              file=sys.stderr)
        return 2

    summary = forget(verbs)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
