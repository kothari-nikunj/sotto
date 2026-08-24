#!/usr/bin/env python3
"""One-shot intentions serviced by Sotto's existing 15-minute heartbeat.

The append-only log is the audit trail; folding the latest row per id yields current state. Creating
the same action at the same due time is idempotent, so a retried agent turn cannot schedule twice.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone

MAX_ROWS = 1_000
VALID_STATUS = {"scheduled", "fired", "canceled"}


def log_path() -> str:
    return os.path.join(os.environ.get("SOTTO_DATA", "/data"), "intentions.jsonl")


@contextmanager
def _lock():
    path = log_path() + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _rows() -> list[dict]:
    try:
        with open(log_path(), encoding="utf-8") as handle:
            rows = []
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"invalid intention row {number}")
                rows.append(row)
            return rows
    except OSError:
        return []


def current() -> list[dict]:
    latest = {}
    for row in _rows():
        ident = str(row.get("id") or "").strip()
        if ident:
            latest[ident] = row
    return sorted(latest.values(), key=lambda row: (str(row.get("due") or ""), row["id"]))


def _write_row(row: dict) -> None:
    path = log_path()
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    # A corrupt log must never be compacted into an apparently valid partial view. Normal creates
    # and transitions read the log before writing, so this is only a last-resort guard against a
    # concurrent external edit after that validation.
    try:
        rows = _rows()
    except (ValueError, TypeError):
        return
    if len(rows) <= MAX_ROWS:
        return
    latest = {row["id"]: row for row in rows if row.get("id")}
    tmp = path + f".{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for item in sorted(latest.values(), key=lambda value: value.get("updated_at", "")):
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _parse_time(value: str, default_tz=None) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if default_tz is None:
            raise ValueError("time must include a timezone offset")
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed


def create(due: str, action: str, context: str = "", created_by: str = "user",
           anchor_key: str = "") -> dict:
    due_dt = _parse_time(due)
    due_iso = due_dt.isoformat()
    action = " ".join(action.split())[:500]
    context = " ".join(context.split())[:1_000]
    anchor_key = anchor_key.strip()[:200]
    if not action:
        raise ValueError("action is required")
    with _lock():
        for row in current():
            if (row.get("status") == "scheduled" and row.get("due") == due_iso
                    and str(row.get("action") or "").casefold() == action.casefold()
                    and str(row.get("anchor_key") or "") == anchor_key):
                return dict(row, duplicate=True)
        now = datetime.now(timezone.utc).isoformat()
        row = {"id": f"int_{secrets.token_hex(8)}", "due": due_iso, "action": action,
               "context": context, "created_by": created_by[:80] or "user",
               "anchor_key": anchor_key,
               "status": "scheduled", "created_at": now, "updated_at": now}
        _write_row(row)
        return row


def transition(ident: str, status: str) -> dict:
    if status not in VALID_STATUS - {"scheduled"}:
        raise ValueError("status must be fired or canceled")
    with _lock():
        found = next((row for row in current() if row.get("id") == ident), None)
        if not found:
            raise KeyError(ident)
        if found.get("status") != "scheduled":
            return found
        row = dict(found, status=status, updated_at=datetime.now(timezone.utc).isoformat())
        _write_row(row)
        return row


def due(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    out = []
    for row in current():
        if row.get("status") != "scheduled":
            continue
        try:
            when = _parse_time(str(row.get("due") or ""), now.tzinfo)
        except ValueError:
            continue
        if when.astimezone(timezone.utc) <= now.astimezone(timezone.utc):
            out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Create, list, cancel, and fire one-shot intentions.")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("create")
    make.add_argument("--due", required=True)
    make.add_argument("--action", required=True)
    make.add_argument("--context", default="")
    make.add_argument("--created-by", default="user")
    make.add_argument("--anchor-key", default="",
                      help="optional open-loop anchor; auto-cancel when that loop closes")
    show = sub.add_parser("list")
    show.add_argument("--all", action="store_true")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("id")
    fire = sub.add_parser("fire", help=argparse.SUPPRESS)
    fire.add_argument("id")
    due_cmd = sub.add_parser("due", help=argparse.SUPPRESS)
    due_cmd.add_argument("--now")
    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create(args.due, args.action, args.context, args.created_by, args.anchor_key)
        elif args.command == "list":
            result = current() if args.all else [r for r in current() if r.get("status") == "scheduled"]
        elif args.command == "cancel":
            result = transition(args.id, "canceled")
        elif args.command == "fire":
            result = transition(args.id, "fired")
        else:
            result = due(_parse_time(args.now) if args.now else None)
    except (ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
