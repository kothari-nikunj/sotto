#!/usr/bin/env python3
"""One-shot intentions serviced by Sotto's existing receiver heartbeat.

Proved unconditional owner-tool timers use its 60-second tick. Conditional and
legacy intentions retain the 15-minute proactive scan and its attention gates.

The append-only log is the audit trail; folding the latest row per id yields current state. Creating
the same action at the same due time is idempotent, so a retried agent turn cannot schedule twice.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
sys.path.insert(0, str(Path(__file__).resolve().parent))

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


def _rows(*, strict: bool = False) -> list[dict]:
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
    except FileNotFoundError:
        return []
    except OSError:
        if strict:
            raise
        return []


def current(*, strict: bool = False) -> list[dict]:
    latest = {}
    for row in _rows(strict=strict):
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


def _admitted_conditional(row):
    """Recognize only the new admitted condition binding for late accepted truth."""
    proof, binding = row.get('owner_create'), row.get('condition_binding')
    if (row.get('created_by') != 'owner_tool' or not isinstance(proof, dict)
            or proof.get('domain') != 'sotto.owner-reminder-tool.v1'
            or not isinstance(binding, dict)
            or set(binding) != {'anchor_key', 'loop_identity', 'sources'}):
        return False
    args = proof.get('args')
    if (not isinstance(args, dict) or set(args) != {'operation', 'due', 'action', 'condition',
            'anchor_key', 'loop_version', 'loop_identity', 'source_revision'}
            or args.get('operation') != 'conditional' or args.get('action') != row.get('action')
            or args.get('anchor_key') != row.get('anchor_key')
            or binding.get('anchor_key') != args.get('anchor_key')
            or binding.get('loop_identity') != args.get('loop_identity')
            or not isinstance(binding.get('sources'), list)
            or any(source not in ('gmail', 'calendar', 'granola', 'imessage', 'whatsapp') for source in binding['sources'])):
        return False
    try:
        canonical = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return (proof.get('args_hash') == hashlib.sha256(canonical.encode()).hexdigest()
                and _parse_time(args['due']) == _parse_time(row['due']))
    except (ValueError, TypeError, KeyError):
        return False


def mark_accepted(ident: str, accepted_at) -> dict:
    """Record confirmed delivery even if cancellation raced an in-flight send.

    Only the receiver's accepted-delivery effect calls this. It does not retry,
    contact a provider, or make an unknown effect look accepted.
    """
    if not accepted_at:
        raise ValueError('provider acceptance is required')
    with _lock():
        found = next((row for row in current(strict=True) if row.get('id') == ident), None)
        if not found:
            raise KeyError(ident)
        if found.get('status') == 'fired':
            return found
        if found.get('status') not in ('scheduled', 'canceled'):
            raise ValueError('intention status is invalid')
        if found.get('created_by') not in ('owner_command', 'owner_tool') or not isinstance(found.get('owner_create'), dict):
            if found['status'] == 'canceled':
                return found  # preserve the legacy canceled-intention transition
            row = dict(found, status='fired', updated_at=datetime.now(timezone.utc).isoformat())
            _write_row(row)
            return row
        if (found['status'] == 'canceled' and not isinstance(found.get('owner_cancel'), dict)
                and not _admitted_conditional(found)):
            return found
        row = dict(found, status='fired', updated_at=datetime.now(timezone.utc).isoformat(),
                   accepted_at=accepted_at)
        _write_row(row)
        return row


def mark_undelivered(ident: str, outcome: str) -> dict:
    """End a timer only from a definitive terminal outbox outcome, never an unknown send."""
    if outcome not in ('superseded', 'expired', 'failed'):
        raise ValueError('terminal non-delivery outcome required')
    with _lock():
        found = next((row for row in current(strict=True) if row.get('id') == ident), None)
        if not found:
            raise KeyError(ident)
        if not owner_timer(found) or found.get('status') != 'scheduled':
            return found
        row = dict(found, status='canceled', delivery_outcome=outcome,
                   updated_at=datetime.now(timezone.utc).isoformat())
        _write_row(row)
        return row


def mark_delivery_notice(ident: str, outcome: str) -> dict:
    """Persist the terminal notice receipt so pruning cannot recreate feedback."""
    if outcome not in ('delivered', 'superseded', 'expired', 'failed'):
        raise ValueError('terminal notice outcome required')
    with _lock():
        found = next((row for row in current(strict=True) if row.get('id') == ident), None)
        if not found:
            raise KeyError(ident)
        if found.get('delivery_outcome') and not found.get('delivery_notice_outcome'):
            found = dict(found, delivery_notice_outcome=outcome,
                         updated_at=datetime.now(timezone.utc).isoformat())
            _write_row(found)
        return found


def owner_timer(row):
    """Exclusive lane, not authorization: invalid owner-tool timers must not become nudges."""
    return (isinstance(row, dict) and row.get('created_by') == 'owner_tool'
            and not _admitted_conditional(row))


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


def _owner_match(row: dict, proof: dict) -> bool:
    return row == proof


def _owner_event(rows: list[dict], event_id: str) -> tuple[dict, dict] | None:
    matches = [(row, proof) for row in rows
               for proof in (row.get('owner_create'), row.get('owner_cancel'))
               if isinstance(proof, dict) and proof.get('event_id') == event_id]
    if len(matches) > 1:
        raise ValueError('owner reminder event is ambiguous')
    return matches[0] if matches else None


def create_admitted(due_at: str, action: str, proof: dict) -> tuple[str, dict | None]:
    """Persist one owner event, retaining its receipt after fire/cancel/compaction.

    Unlike the legacy CLI's due/action dedupe, two distinct owner events may ask for
    identical reminders. The trusted caller has already validated route and command.
    """
    return _create_owner(due_at, action, proof, 'owner_command')


def create_tool_admitted(due_at: str, action: str, proof: dict, *, condition_binding=None) -> tuple[str, dict | None]:
    """Persist honest tool provenance under the same original owner-event effect fence."""
    return _create_owner(due_at, action, proof, 'owner_tool', condition_binding=condition_binding)


def _create_owner(due_at: str, action: str, proof: dict, created_by: str, *, condition_binding=None) -> tuple[str, dict | None]:
    due_iso = _parse_time(due_at).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    if not action or len(action) > 500 or action.strip() != action:
        raise ValueError('owner reminder action is invalid')
    with _lock():
        rows = current(strict=True)
        prior = _owner_event(rows, proof['event_id'])
        if prior:
            row, saved = prior
            return ('committed', row) if _owner_match(saved, proof) and saved is row.get('owner_create') else ('event_conflict', None)
        now = datetime.now(timezone.utc).isoformat()
        row = {'id': f'int_{secrets.token_hex(16)}', 'due': due_iso, 'action': action,
               'context': '', 'created_by': created_by, 'anchor_key': '',
               'status': 'scheduled', 'created_at': now, 'updated_at': now,
               'owner_create': proof}
        if condition_binding is not None:
            row.update(anchor_key=condition_binding['anchor_key'], condition_binding=condition_binding)
        _write_row(row)
        return 'committed', row


def conditional_loop(binding, *, now, permitted, version=None, rows=None):
    """Exact protected debt identity; missing or unproved context never means closed.

    The default read is scoped to the bound anchor, so an unrelated unreadable ledger file or a
    long-grown ledger cannot hold this one condition (`protected_ledger_io.load_protected_scope`).
    """
    from delivery_effects import loop_identity, loop_version
    from ledger_provenance import classify_ledger_sources, source_loop_sources
    from protected_ledger_io import load_protected_scope
    import ledger_io
    if (type(binding) is not dict or set(binding) != {'anchor_key', 'loop_identity', 'sources'}
            or type(binding['sources']) is not list
            or type(binding['anchor_key']) is not str or not binding['anchor_key']):
        raise ValueError('conditional binding unavailable')
    if rows is None:
        rows, _complete = load_protected_scope(os.environ.get('SOTTO_DATA', '/data'), [binding['anchor_key']])
    selected = [row for row in rows if row.get('anchor_key') == binding['anchor_key']]
    if len(selected) != 1:
        raise ValueError('conditional target unavailable')
    row = selected[0]
    if loop_identity(row) != binding['loop_identity'] or (version and loop_version(row) != version):
        raise ValueError('conditional target changed')
    if row.get('content_provenance') is not None:
        kind, sources = classify_ledger_sources(row)
        if kind != 'owner':
            sources = source_loop_sources(row)
    else:
        sources = source_loop_sources(row)
    union = set(sources) | set(binding['sources'])
    if any(permitted(source) is not True for source in union):
        raise PermissionError('conditional source unavailable')
    status = row.get('status', 'open')
    if status in ledger_io.TERMINAL:
        return 'terminal', row, sorted(union)
    from timeutil import _resolve_tz
    zone = _resolve_tz(ledger_io.configured_tz() or '+00:00') or timezone.utc
    if not ledger_io.active_entries([row], now.astimezone(zone)):
        return 'held', row, sorted(union)
    return 'active', row, sorted(union)


def conditional_current_state(binding, *, now, rows=None):
    """New admitted conditions require actual current connector/Google/Bridge grants."""
    from ask_research_pipeline import current_authority
    authority = current_authority()
    if not isinstance(authority, dict) or authority.get('complete') is not True:
        raise PermissionError('conditional source authority unavailable')
    granted = set(authority.get('bridge_sources', [])) | set(authority.get('google_sources', [])) | set(authority.get('connector_sources', []))
    result = conditional_loop(binding, now=now, permitted=lambda source: source in granted, rows=rows)
    if current_authority() != authority:
        raise PermissionError('conditional source authority changed')
    return result


def cancel_admitted(ident: str, proof: dict) -> tuple[str, dict | None]:
    with _lock():
        rows = current(strict=True)
        prior = _owner_event(rows, proof['event_id'])
        if prior:
            row, saved = prior
            return ('committed', row) if _owner_match(saved, proof) and saved is row.get('owner_cancel') else ('event_conflict', None)
        row = next((item for item in rows if item.get('id') == ident), None)
        if not row:
            return 'target_unavailable', None
        if row.get('created_by') not in ('owner_command', 'owner_tool') or not isinstance(row.get('owner_create'), dict):
            return 'target_not_owner', None
        if row['owner_create'].get('identity_digest') != proof['identity_digest']:
            return 'target_not_owner', None
        if row.get('status') != 'scheduled':
            return 'target_unavailable', None
        changed = dict(row, status='canceled', updated_at=datetime.now(timezone.utc).isoformat(),
                       owner_cancel=proof)
        _write_row(changed)
        return 'committed', changed


def inspect_admitted(proof: dict, *, cancel: bool) -> dict | None:
    with _lock():
        prior = _owner_event(current(strict=True), proof['event_id'])
        if not prior:
            return None
        row, saved = prior
        expected = row.get('owner_cancel') if cancel else row.get('owner_create')
        if saved is not expected or not _owner_match(saved, proof):
            raise ValueError('owner reminder event conflicts with saved effect')
        return row


def list_admitted(identity_digest: str, limit: int = 50) -> list[dict]:
    with _lock():
        rows = [row for row in current(strict=True) if row.get('created_by') in ('owner_command', 'owner_tool')
                and row.get('status') == 'scheduled'
                and isinstance(row.get('owner_create'), dict)
                and row['owner_create'].get('identity_digest') == identity_digest]
        return sorted(rows, key=lambda row: (row['due'], row['id']))[:limit]


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
