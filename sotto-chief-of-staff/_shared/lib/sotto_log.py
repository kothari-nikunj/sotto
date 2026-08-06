"""Shared brief diagnostics → stderr + $SOTTO_DATA/logs/compose_brief.log.

execute_code captures a script's stderr and hands it to the agent, NOT to Railway's container logs.
So scripts in the brief pipeline persist their diagnostics to a log file on the /data volume, which the
receiver serves at GET /debug/brief-log. Best-effort; never raises."""
from __future__ import annotations

import datetime
import os
import sys


_MAX_BYTES = 4 * 1024 * 1024   # rotate the brief log so it can't grow unbounded on the /data volume
_KEEP_LINES = 1500             # ~last few weeks of briefs; plenty for /debug/brief-log


def bounded_append(path: str, line: str, max_bytes: int, keep_lines: int) -> None:
    """Append one `line` to `path`, first rotating to the last `keep_lines` once the file passes
    `max_bytes` — bounded disk, recent history preserved. Best-effort; makes the parent dir. Shared by
    diag() (compose_brief.log) and metrics.py's jsonl writer (identical rotate-then-append); each caller
    keeps its OWN bound constants."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Append-only would grow forever on a long-running cloud box. Truncate to the tail once oversized.
    try:
        if os.path.getsize(path) > max_bytes:
            with open(path, encoding="utf-8") as f:
                tail = f.readlines()[-keep_lines:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(tail)
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(line if line.endswith("\n") else line + "\n")


def diag(msg: str) -> None:
    print(msg, file=sys.stderr)
    try:
        path = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "logs", "compose_brief.log")
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        bounded_append(path, f"{ts} {msg}", _MAX_BYTES, _KEEP_LINES)
    except Exception:
        pass
