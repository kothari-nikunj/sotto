#!/usr/bin/env python3
"""
jsonstore.py — read-modify-write a JSON file on the volume without losing somebody else's write.

THE BUG THIS EXISTS FOR (Aug 2026, found by an external reviewer and then reproduced): three
processes update `preferences.json` — `preferences.py` (your stated rules), `learn_preferences.py`
(the rebuilt behavioural lists, which runs in EVERY brief), and the dashboard's learned-rule delete.
All three did `read → modify → write` with no lock, and all three wrote through the same FIXED
temp path (`preferences.json.tmp`). Two of them at once produced three distinct failures, measured
over 100 trials of the mildest version (two concurrent mutes):

    7%  a preference silently vanished   — B read before A wrote, then B's write won
    7%  a writer exited non-zero         — its temp file was renamed away by the other process
    2%  preferences.json left unreadable — both wrote the same temp, one read it half-written

The real collision is wider than that test: it is you muting someone at 6:31am while the morning
brief's Learn step rebuilds the lists, and the learner's window spans reading all of
`outcomes.jsonl`. `os.replace` makes the final rename atomic; it does nothing about the read that
happened before it, which is where the update is lost.

So the unit is a TRANSACTION, not a write:

    with jsonstore.transaction(path) as data:      # flock held from here …
        data["explicit"] = new_explicit
                                                   # … to here, then written atomically

Some critical sections are not one JSON file: `knowledge_update.apply()` rewrites a whole directory
of person and company markdown, and briefs, research, prewarm and dashboard edits all call it at
once — the same race, one level up. For those there is `lock(path)`, the transaction's lock half
without the read or the write:

    with jsonstore.lock(f"{data}/knowledge/.apply"):   # one lock, whole body
        ...

Locking is advisory `fcntl.flock` on a sidecar `<path>.lock`, held across the read and the write.
The receiver image cannot import this module (no skills tree on the box), so `connectors.py`
carries a matching implementation and `tests/test_docs_drift.py` asserts the two agree on the lock
path — flock is an OS primitive, so two implementations interoperate correctly as long as they
name the same file. Same copy-plus-guard posture as `keys.py`.

A crash inside the block writes nothing: the file is only replaced on a clean exit.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from contextlib import contextmanager

LOCK_SUFFIX = ".lock"          # the shared convention — connectors.py must match
LOCK_TIMEOUT_SECS = 10         # a preference write is milliseconds; 10s means something is wrong


class Unreadable(Exception):
    """The file exists but does not parse. Raised INSTEAD of silently returning {} so a caller can
    decide — `learn_preferences.py` aborts rather than mint a fresh file over a corrupt one, which
    would drop the user's explicit block and every `suppressed` tombstone with it."""


def lock_path(path: str) -> str:
    """The sidecar lock for `path`. One function so the name can never drift between callers."""
    return path + LOCK_SUFFIX


def _tmp_path(path: str) -> str:
    """A temp name unique to THIS process. The old fixed `<path>.tmp` was itself a shared mutable
    resource: two writers opened the same file, and whoever renamed first pulled it out from under
    the other."""
    return f"{path}.tmp.{os.getpid()}"


def read(path: str, default=None, strict: bool = False):
    """Parse `path`, or `default` when it is absent. A present-but-corrupt file raises Unreadable
    when `strict`, else returns `default`."""
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError, ValueError) as e:
        if strict:
            raise Unreadable(f"{path}: {e}") from e
        return default
    return v if v is not None else default


def write_atomic(path: str, obj, mode: int = 0o600, indent: int | None = None) -> None:
    """Write `obj` to `path` via a process-unique temp + `os.replace`. Not a substitute for
    `transaction` — it makes the WRITE atomic, not the read-modify-write."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = _tmp_path(path)
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def lock(path: str):
    """Hold `path`'s exclusive sidecar lock (`<path>.lock`) for the body of the block — nothing is
    read and nothing is written.

    THE lock primitive: `transaction` is this plus a read and an atomic write, and a caller whose
    critical section is a whole function body (knowledge_update.apply, which rewrites a directory of
    markdown rather than one JSON file) takes it directly. Same suffix, same timeout, one
    implementation — two writers that lock differently are two writers that don't lock.

    Reentrant WITHIN this process, by a depth counter: flock would block a second exclusive open of
    the same lock file even from the process that holds it, and the tree genuinely nests —
    `knowledge_edit --op merge` takes the apply lock so a human-confirmed merge can't race a brief's
    Learn step, and `apply()` itself calls the same merge machinery from inside its own locked body.
    Cross-PROCESS exclusion (the point of the lock) is untouched; these scripts are single-threaded
    one-shot CLIs, so a process-level counter is the whole story."""
    return _ReentrantLock(path)


class _ReentrantLock:
    _depth: dict = {}          # lock path → how many times THIS process currently holds it

    def __init__(self, path: str):
        self._path = path
        self._lf = None

    def __enter__(self):
        lk = lock_path(self._path)
        if _ReentrantLock._depth.get(lk, 0) == 0:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self._lf = os.open(lk, os.O_CREAT | os.O_RDWR, 0o600)
            _flock_with_timeout(self._lf, lk)
        _ReentrantLock._depth[lk] = _ReentrantLock._depth.get(lk, 0) + 1
        return self

    def __exit__(self, *exc):
        lk = lock_path(self._path)
        _ReentrantLock._depth[lk] -= 1
        if _ReentrantLock._depth[lk] == 0 and self._lf is not None:
            try:
                fcntl.flock(self._lf, fcntl.LOCK_UN)
            finally:
                os.close(self._lf)
                self._lf = None
        return False


def _flock_with_timeout(fd: int, lock: str) -> None:
    """Block for the lock, but never forever: a stuck holder must surface as an error a human can
    read, not as a brief that hangs until the platform kills it."""
    import time
    deadline = time.monotonic() + LOCK_TIMEOUT_SECS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"could not lock {lock} within {LOCK_TIMEOUT_SECS}s — another writer is stuck"
                ) from e
            time.sleep(0.02)


@contextmanager
def transaction(path: str, default=None, mode: int = 0o600, indent: int | None = None,
                strict: bool = False):
    """Hold the exclusive lock across read AND write.

    Yields the parsed value (or `default`). Mutate it in place; on a clean exit it is written back
    atomically, under the same lock that produced the read — which is what makes the update safe.
    To abort without touching the file, raise. `strict=True` turns a corrupt file into `Unreadable`
    BEFORE the block runs, which is how learn_preferences refuses to mint a fresh file over one it
    couldn't parse. Built ON `lock()` — one flock implementation, and the reentrancy comes along."""
    with lock(path):
        data = read(path, default, strict=strict)
        yield data
        write_atomic(path, data, mode=mode, indent=indent)
