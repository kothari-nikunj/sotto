"""Shared brief diagnostics → stderr + $SOTTO_DATA/logs/compose_brief.log.

execute_code captures a script's stderr and hands it to the agent, NOT to Railway's container logs.
So scripts in the brief pipeline persist their diagnostics to a log file on the /data volume, which the
receiver serves at GET /debug/brief-log. Best-effort; never raises."""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import os
import sys

# Hoist this module's own dir onto sys.path (guarded, like metrics.py's) rather than relying on the
# caller's sys.path: sotto_log is loaded directly (importlib spec_from_file_location) by test files
# outside the skills tree's own conftest.py — e.g. runtime/trigger-receiver's — that never put
# _shared/lib on sys.path themselves.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import jsonstore  # noqa: E402


_MAX_BYTES = 4 * 1024 * 1024   # rotate the brief log so it can't grow unbounded on the /data volume
_KEEP_LINES = 1500             # ~last few weeks of briefs; plenty for /debug/brief-log


@contextlib.contextmanager
def _log_lock(path: str):
    """Coordinate append/rotation with the receiver retention sweep via jsonstore's own sidecar
    flock (`<path>.lock`, `jsonstore.lock_path` / `jsonstore._flock_with_timeout` — same file, same
    timeout, one retry loop instead of a hand-rolled copy of it).

    Takes that primitive directly rather than the reentrant `jsonstore.lock()` wrapper: that
    wrapper's depth counter is documented as correct for one thread per process per lock path, and
    diag()/bounded_append run off a ThreadPool (research_attendees) — going through it here would
    let a second thread believe it already owns a lock a sibling thread is genuinely holding."""
    lock_fd = os.open(jsonstore.lock_path(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        jsonstore._flock_with_timeout(lock_fd, jsonstore.lock_path(path))
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def bounded_append(path: str, line: str, max_bytes: int, keep_lines: int, *, already_locked=False) -> None:
    """Append one `line` to `path`, first rotating to the last `keep_lines` once the file passes
    `max_bytes` — bounded disk, recent history preserved. Best-effort; makes the parent dir. Shared by
    diag() (compose_brief.log) and metrics.py's jsonl writer (identical rotate-then-append); each caller
    keeps its OWN bound constants. A caller performing a larger read-modify-write transaction may
    pass `already_locked=True` only while it holds this path's same sidecar flock."""
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def rotate_and_append():
        """The file operation; either this helper or the caller owns the sidecar lock."""
        try:
            if os.path.getsize(path) > max_bytes:
                with open(path, encoding="utf-8") as f:
                    tail = f.readlines()[-keep_lines:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(tail)
        except OSError:
            pass
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(line if line.endswith("\n") else line + "\n")

    # Rotation and append are one transaction. Retention uses the same sidecar lock when it cuts
    # this file in place, including while these helpers run in separate brief child processes.
    if already_locked:
        rotate_and_append()
        return
    with _log_lock(path):
        rotate_and_append()


def diag(msg: str) -> None:
    print(msg, file=sys.stderr)
    try:
        path = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "logs", "compose_brief.log")
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        bounded_append(path, f"{ts} {msg}", _MAX_BYTES, _KEEP_LINES)
    except Exception:
        pass
