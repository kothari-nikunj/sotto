#!/usr/bin/env python3
"""sessions.py — archive the Hermes chat sessions, the ONE implementation both callers share.

Two callers: `start.sh` at boot (fresh-per-deploy, since Aug 26) and the receiver's nightly
housekeeping (`_session_archive_tick`, Sep 3). Both used to find session ids with a hex/uuid
regex — and Hermes' ids are neither: `20260903_033026_65967d` for a chat session,
`cron_964b334424a8_20260902_203042` for a cron run. The regex matched NOTHING, so "archived N
session(s)" was 0 at every boot for a week, no session ever reset, and interactive replies grew a
week-long transcript to copy from (the Sep 1–2 incidents). This module reads the ID column instead.

Cron sessions are left alone: a `cron_*` session is a finished one-shot, there are ~100 of them a
day, and archiving them is 100 subprocess calls that change nothing the gateway reads.

    python3 sessions.py            # archive every non-cron session; prints the count; exit 0
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys


def _adapter():
    path = Path('/app/adapters/hermes/runtime_api.py')
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / 'adapters/hermes/runtime_api.py'
    spec = importlib.util.spec_from_file_location('sotto_hermes_session_api', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def session_ids(listing: str) -> list[str]:
    return _adapter().session_ids(listing)


def archive_all(run=None) -> int | None:
    return _adapter().archive_sessions(run or subprocess.run)


def main() -> int:
    n = archive_all()
    if n is None:
        print("[sotto] session archive: sessions CLI unavailable — persona updates reach chat "
              "only after /new", flush=True)
        return 0
    print(f"[sotto] session archive: archived {n} chat session(s) — the next message starts a "
          "fresh session (silently; /resume can reopen old transcripts)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
