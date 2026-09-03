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

import subprocess
import sys

CLI_TIMEOUT_SECS = 30
CRON_PREFIX = "cron_"


def session_ids(listing: str) -> list[str]:
    """The ID column of `hermes sessions list`, cron sessions excluded, in listing order.

    The table is `Title  Workspace  Last Active  ID` with a rule under the header; the id is the
    LAST whitespace-separated token of a data row and always carries a digit. Titles can contain
    anything, so the last column — not a pattern over the whole line — is what is read."""
    out: list[str] = []
    for line in (listing or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        sid = parts[-1]
        if sid == "ID" or not any(ch.isdigit() for ch in sid) or set(sid) <= set("─-="):
            continue
        if sid.startswith(CRON_PREFIX) or sid in out:
            continue
        out.append(sid)
    return out


def archive_all(run=None) -> int | None:
    """Archive every non-cron session. Returns how many `hermes sessions archive` calls exited 0,
    or None when the sessions CLI is not available here. `archive` keeps the transcript (`/resume`
    reopens it) — this is "start fresh", never "destroy history". Never raises."""
    run = run or subprocess.run     # resolved at call time, so a patched subprocess.run is honoured
    try:
        listed = run(["hermes", "sessions", "list"], capture_output=True, text=True,
                     timeout=CLI_TIMEOUT_SECS)
    except (OSError, subprocess.SubprocessError):
        return None
    if listed.returncode != 0:
        return None
    archived = 0
    for sid in session_ids(listed.stdout or ""):
        try:
            done = run(["hermes", "sessions", "archive", sid], capture_output=True, text=True,
                       timeout=CLI_TIMEOUT_SECS)
        except (OSError, subprocess.SubprocessError):
            continue
        archived += int(done.returncode == 0)
    return archived


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
