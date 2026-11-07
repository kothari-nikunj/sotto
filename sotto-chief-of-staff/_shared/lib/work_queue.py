"""Durable ownership of accepted work, before the delivery outbox owns its result.

Only this module writes events/work.sqlite3. Knowledge and delivery keep their own
domain stores. A completed result survives a worker crash without another model call.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.error import HTTPError

LEASE_SECONDS = 120
MAX_ATTEMPTS = 3
# Known provider refusals get a separate, finite recovery allowance. Unknown outcomes still
# spend fault attempts. Shared with model_work so a queue retry can actually dispatch a call.
MAX_PROVIDER_RECOVERIES = 4
PROVIDER_RETRY_EXIT = 76
MODEL_BUDGET_EXIT = 77
MODEL_ADMISSION_BUSY_EXIT = 78
MODEL_OUTCOME_UNCERTAIN_EXIT = 79
MODEL_BUDGET_EXHAUSTED = 'model_budget_exhausted'
RETRYABLE_PROVIDER_CODES = frozenset({429, 500, 502, 503, 504})


def retryable_provider_error(error):
    return isinstance(error, HTTPError) and error.code in RETRYABLE_PROVIDER_CODES

RETENTION_SECONDS = 7 * 86400
MAX_WORKERS = 2
BACKGROUND_PRIORITY = 50
BACKGROUND_MAX_WAIT_SECONDS = 30 * 60


@contextmanager
def _db(root):
    directory = Path(root) / 'events'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'work.sqlite3'
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    os.chmod(path, 0o600)
    try:
        # Preserve the database's journal mode. Concurrent first opens can collide while switching
        # to WAL before BEGIN IMMEDIATE acquires the write reservation. New stores use SQLite's
        # rollback journal; existing WAL stores stay WAL. Both retain FULL durability below.
        db.execute('PRAGMA synchronous=FULL')
        db.row_factory = sqlite3.Row
        # Schema creation and migrations share the same write reservation as queue mutations. Two
        # first opens of a legacy volume must not both observe a missing column and race ALTER.
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL, due REAL NOT NULL,
            valid_until REAL, priority INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            owner TEXT, lease_until REAL, result TEXT, error TEXT, finished REAL,
            handoff_attempts INTEGER NOT NULL DEFAULT 0)''')
        columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
        if 'handoff_attempts' not in columns:
            db.execute('ALTER TABLE jobs ADD COLUMN handoff_attempts INTEGER NOT NULL DEFAULT 0')
        if 'provider_recoveries' not in columns:
            db.execute('ALTER TABLE jobs ADD COLUMN provider_recoveries INTEGER NOT NULL DEFAULT 0')
        if 'admission' not in columns:
            db.execute('ALTER TABLE jobs ADD COLUMN admission TEXT')
        if 'feedback' not in columns:
            db.execute('ALTER TABLE jobs ADD COLUMN feedback TEXT')
        db.execute('''CREATE TABLE IF NOT EXISTS work_items (
            kind TEXT NOT NULL, item_key TEXT NOT NULL, job_id TEXT NOT NULL,
            PRIMARY KEY(kind,item_key))''')
        # Permanent admission evidence. Normal work rows and outbox receipts have shorter
        # operational retention, so they cannot fence a weekly slot across rollback.
        # Only hashes/ids/status survive here; never a prompt, source store, or route.
        db.execute('''CREATE TABLE IF NOT EXISTS routine_slots (
            slot_hash TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE,
            request_digest TEXT NOT NULL, admitted REAL NOT NULL,
            last_work_status TEXT NOT NULL DEFAULT 'admitted',
            routine_hash TEXT)''')
        # Fixed receiver schedules have different authority from migrated personal routines.
        # This permanent fence contains no prompt, result, or provider recipient. It survives
        # the seven-day work row sweep and is written with the job in this same transaction.
        # authority_digest is the tenant/profile identity that owns the slot history; the
        # schedule digest (zone, crons.json, owner route) is rebound in place when it changes.
        db.execute('''CREATE TABLE IF NOT EXISTS fixed_schedule_authority (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            authority_digest TEXT NOT NULL, schedule_digest TEXT)''')
        if 'schedule_digest' not in {r[1] for r in db.execute(
                'PRAGMA table_info(fixed_schedule_authority)')}:
            # A NULL schedule digest marks a legacy row whose authority_digest is the old
            # combined identity+schedule transcript (adopted on the next admission).
            db.execute('ALTER TABLE fixed_schedule_authority ADD COLUMN schedule_digest TEXT')
        db.execute('''CREATE TABLE IF NOT EXISTS fixed_slots (
            slot_hash TEXT PRIMARY KEY, name TEXT NOT NULL,
            job_id TEXT UNIQUE, work_key_digest TEXT,
            admitted REAL NOT NULL, last_work_status TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0)''')
        # An explicit Bridge wake may reach the same brief key before its cron tick.
        # Keep its content-free identity after the ordinary job is scrubbed/pruned,
        # so the later cron can adopt or hold it without minting a second composition.
        db.execute('''CREATE TABLE IF NOT EXISTS fixed_brief_wakes (
            job_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            admitted REAL NOT NULL, last_work_status TEXT NOT NULL)''')
        slot_columns = {r[1] for r in db.execute('PRAGMA table_info(routine_slots)')}
        for column in ('completion_utc', 'handoff_kind', 'handoff_digest'):
            if column not in slot_columns:
                db.execute(f'ALTER TABLE routine_slots ADD COLUMN {column} TEXT')
        if 'routine_hash' not in {r[1] for r in db.execute('PRAGMA table_info(routine_slots)')}:
            db.execute('ALTER TABLE routine_slots ADD COLUMN routine_hash TEXT')
        db.execute('''CREATE TABLE IF NOT EXISTS routine_cursors (
            cursor_hash TEXT PRIMARY KEY, binding_digest TEXT NOT NULL,
            initial_utc TEXT NOT NULL, current_utc TEXT NOT NULL,
            pending_slot_utc TEXT, pending_job_id TEXT,
            pending_provisional_utc TEXT, generation INTEGER NOT NULL DEFAULT 0,
            schedule_expr TEXT, schedule_zone TEXT, completed_count INTEGER,
            last_run_utc TEXT, initial_completed_count INTEGER,
            initial_last_run_utc TEXT, last_evidence_digest TEXT,
            last_reconciled_job_id TEXT)''')
        db.execute('''CREATE TABLE IF NOT EXISTS routine_reconcile_scan (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            after_cursor_hash TEXT NOT NULL)''')
        db.execute("INSERT OR IGNORE INTO routine_reconcile_scan VALUES (1,'')")
        cursor_columns = {r[1] for r in db.execute('PRAGMA table_info(routine_cursors)')}
        for column, definition in (
                ('schedule_expr', 'TEXT'), ('schedule_zone', 'TEXT'),
                ('completed_count', 'INTEGER'), ('last_run_utc', 'TEXT'),
                ('initial_completed_count', 'INTEGER'), ('initial_last_run_utc', 'TEXT'),
                ('last_evidence_digest', 'TEXT'), ('last_reconciled_job_id', 'TEXT')):
            if column not in cursor_columns:
                db.execute(f'ALTER TABLE routine_cursors ADD COLUMN {column} {definition}')
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def job_id_for(kind, key) -> str:
    """The durable identity of an admitted request: what `enqueue` dedupes on, and what a caller
    asking "is THIS request already admitted?" compares against."""
    return hashlib.sha256(f'{kind}:{key}'.encode()).hexdigest()[:32]


def _routine_text(value, label):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 160:
        raise ValueError(f'invalid routine {label}')
    return value


def _routine_slot_key(owner_scope, profile_id, legacy_id, scheduled_at_utc):
    """One native scheduled instant, independent of expression/revision/activation epoch."""
    identity = [_routine_text(owner_scope, 'owner scope'),
                _routine_text(profile_id, 'profile ID'),
                _routine_text(legacy_id, 'legacy ID'), scheduled_at_utc]
    encoded = json.dumps(identity, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return 'routine-slot-v1:' + hashlib.sha256(encoded).hexdigest()


def _routine_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)


def _routine_bound(value, label):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'invalid routine {label}')
    return value


def _routine_utc(value, label):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f'routine {label} must be timezone-aware')
    return value.astimezone(timezone.utc).isoformat(
        timespec='microseconds').replace('+00:00', 'Z')


def _routine_hash(owner_scope, profile_id, legacy_id):
    parts = [_routine_text(owner_scope, 'owner scope'),
             _routine_text(profile_id, 'profile ID'),
             _routine_text(legacy_id, 'legacy ID')]
    return hashlib.sha256(_routine_json(parts).encode('utf-8')).hexdigest()


def _native_cursor_module():
    path = Path(__file__).with_name('routine_cursor.py')
    spec = importlib.util.spec_from_file_location('sotto_routine_cursor', path)
    if spec is None or spec.loader is None:
        raise RuntimeError('native routine cursor is unavailable')
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _captured_schedule(expr, zone, completed, last_run_at):
    if expr is None and zone is None and completed is None and last_run_at is None:
        return None
    if (not isinstance(expr, str) or not isinstance(zone, str)
            or type(completed) is not int or completed < 0):
        raise ValueError('invalid captured routine schedule')
    cursor = _native_cursor_module()
    # Captured metadata is not native planning authority. Fresh hourly and exact
    # weekday-set routines supply their own wall-time successor; the native
    # planner remains fixed-only.
    hourly = re.fullmatch(r'([0-9]{1,2}) \* \* \* \*', expr)
    weekday_set = re.fullmatch(r'([0-9]{1,2}) ([0-9]{1,2}) \* \* ([0-6](?:,[0-6]){1,6})', expr)
    canonical_set = (weekday_set is not None and int(weekday_set[1]) <= 59 and int(weekday_set[2]) <= 23
                     and (parts := [int(part) for part in weekday_set[3].split(',')])
                     and all(left < right for left, right in zip(parts, parts[1:])))
    if (hourly is None or int(hourly[1]) > 59) and not canonical_set:
        cursor._fixed_expr(expr)
    cursor._zone(zone)
    if (completed > 0) != (last_run_at is not None):
        raise ValueError('invalid captured routine completion anchor')
    last_utc = None
    if last_run_at is not None:
        if not isinstance(last_run_at, str):
            raise ValueError('invalid captured routine completion anchor')
        last_utc = _routine_utc(datetime.fromisoformat(last_run_at), 'last run')
    return expr, zone, completed, last_utc


def _routine_binding(activation_epoch, source_revision, owner_route):
    _routine_text(activation_epoch, 'activation epoch')
    if not isinstance(source_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', source_revision):
        raise ValueError('invalid routine source revision')
    try:
        route = json.loads(_routine_json(owner_route))
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError('invalid routine owner route JSON') from error
    if (not isinstance(route, dict) or set(route) != {'platform', 'chat_id', 'thread_id'}
            or route['platform'] not in ('telegram', 'imessage')
            or not isinstance(route['chat_id'], str) or not route['chat_id']
            or len(route['chat_id']) > 512
            or (route['thread_id'] is not None and (
                not isinstance(route['thread_id'], str)
                or not route['thread_id'] or len(route['thread_id']) > 512))):
        raise ValueError('invalid routine owner route')
    return hashlib.sha256(_routine_json({
        'activation_epoch': activation_epoch, 'source_revision': source_revision,
        'owner_route': route,
    }).encode('utf-8')).hexdigest()


def _prepare_routine_slot(owner_scope, profile_id, legacy_id, scheduled_at_utc,
                          activation_epoch, source_revision, owner_route, payload,
                          not_before, valid_until, priority, cursor_successor=None):
    if not isinstance(scheduled_at_utc, datetime) or scheduled_at_utc.utcoffset() is None:
        raise ValueError('routine scheduled instant must be timezone-aware')
    scheduled = scheduled_at_utc.astimezone(timezone.utc).isoformat(
        timespec='microseconds').replace('+00:00', 'Z')
    _routine_text(activation_epoch, 'activation epoch')
    if not isinstance(source_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', source_revision):
        raise ValueError('invalid routine source revision')
    # Freeze mutable caller containers once, then validate and hash exactly the
    # same values that the normal WorkQueue row will store.
    try:
        owner_route = json.loads(_routine_json(owner_route))
        payload = json.loads(_routine_json(payload))
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError('invalid routine work or route JSON') from error
    if (not isinstance(owner_route, dict) or set(owner_route) != {'platform', 'chat_id', 'thread_id'}
            or owner_route['platform'] not in ('telegram', 'imessage')
            or not isinstance(owner_route['chat_id'], str) or not owner_route['chat_id']
            or len(owner_route['chat_id']) > 512
            or (owner_route['thread_id'] is not None and (
                not isinstance(owner_route['thread_id'], str)
                or not owner_route['thread_id'] or len(owner_route['thread_id']) > 512))):
        raise ValueError('invalid routine owner route')
    if (not isinstance(payload, dict) or set(payload) != {'runner', 'prompt', 'label', 'decision_ids'}
            or not isinstance(payload['runner'], list) or len(payload['runner']) != 2
            or not all(isinstance(part, str) and part and os.path.isabs(part)
                       for part in payload['runner'])
            or not isinstance(payload['prompt'], str) or not payload['prompt']
            or len(payload['prompt']) > 100_000
            or not isinstance(payload['label'], str) or not payload['label']
            or len(payload['label']) > 160
            or not isinstance(payload['decision_ids'], list)
            or not all(isinstance(item, str) for item in payload['decision_ids'])):
        raise ValueError('invalid fixed routine work payload')
    not_before = _routine_bound(not_before, 'not_before')
    scheduled_due = datetime.fromisoformat(scheduled.replace('Z', '+00:00')).timestamp()
    if not_before is not None and not_before < scheduled_due:
        raise ValueError('routine not_before precedes scheduled occurrence')
    if not_before is None:
        not_before = scheduled_due
    valid_until = _routine_bound(valid_until, 'valid_until')
    if type(priority) is not int or not 0 <= priority <= 10000:
        raise ValueError('invalid routine priority')
    key = _routine_slot_key(owner_scope, profile_id, legacy_id, scheduled)
    slot_hash = key.rsplit(':', 1)[1]
    job_id = job_id_for('run', key)
    encoded = _routine_json(payload)
    request_fields = {
        'activation_epoch': activation_epoch, 'source_revision': source_revision,
        'owner_route_digest': hashlib.sha256(_routine_json(owner_route).encode('utf-8')).hexdigest(),
        'payload_digest': hashlib.sha256(encoded.encode('utf-8')).hexdigest(),
        'not_before': not_before, 'valid_until': valid_until, 'priority': priority,
    }
    if cursor_successor is not None:
        request_fields['cursor_successor'] = cursor_successor
    request = _routine_json(request_fields)
    request_digest = hashlib.sha256(request.encode('utf-8')).hexdigest()
    return {'slot_hash': slot_hash, 'job_id': job_id, 'encoded': encoded,
            'request_digest': request_digest, 'routine_hash': _routine_hash(
                owner_scope, profile_id, legacy_id), 'scheduled': scheduled,
            'not_before': not_before, 'valid_until': valid_until, 'priority': priority}


def _admit_routine_slot_in_tx(db, prepared):
    slot_hash = prepared['slot_hash']
    job_id = prepared['job_id']
    existing = db.execute('SELECT * FROM routine_slots WHERE slot_hash=?',
                          (slot_hash,)).fetchone()
    if existing is not None:
        if (existing['job_id'] != job_id
                or existing['request_digest'] != prepared['request_digest']
                or existing['routine_hash'] not in (None, prepared['routine_hash'])):
            raise RuntimeError('routine slot conflicts with prior admission')
        work = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        work_status = work['status'] if work else existing['last_work_status']
        admission = ('held' if work is None else
                     'terminal' if work_status in ('done', 'failed', 'expired') else 'existing')
        return {'job_id': job_id, 'admission': admission, 'work_status': work_status}
    if db.execute('SELECT 1 FROM jobs WHERE id=?', (job_id,)).fetchone():
        raise RuntimeError('routine work identity is already occupied')
    now = time.time()
    db.execute('''INSERT INTO routine_slots
        (slot_hash,job_id,request_digest,admitted,routine_hash)
        VALUES (?,?,?,?,?)''', (slot_hash, job_id, prepared['request_digest'],
                              now, prepared['routine_hash']))
    db.execute('''INSERT INTO jobs
        (id,kind,payload,status,created,due,valid_until,priority)
        VALUES (?, 'run', ?, 'pending', ?, ?, ?, ?)''',
        (job_id, prepared['encoded'], now,
         now if prepared['not_before'] is None else prepared['not_before'],
         prepared['valid_until'], prepared['priority']))
    return {'job_id': job_id, 'admission': 'new', 'work_status': 'pending'}


def admit_routine_slot(root, *, owner_scope, profile_id, legacy_id,
                       scheduled_at_utc, activation_epoch, source_revision,
                       owner_route, payload, not_before=None, valid_until=None,
                       priority=20):
    # Unmounted trusted storage API. The eventual worker must revalidate route,
    # activation epoch and migration state before execution.
    prepared = _prepare_routine_slot(
        owner_scope, profile_id, legacy_id, scheduled_at_utc, activation_epoch,
        source_revision, owner_route, payload, not_before, valid_until, priority)
    with _db(root) as db:
        if db.execute('SELECT 1 FROM routine_cursors WHERE cursor_hash=?',
                      (prepared['routine_hash'],)).fetchone():
            raise RuntimeError('routine cursor requires protected CAS admission')
        return _admit_routine_slot_in_tx(db, prepared)


def _cursor_state(row):
    return {'cursor_hash': row['cursor_hash'], 'current_utc': row['current_utc'],
            'pending_job_id': row['pending_job_id'],
            'pending_slot_utc': row['pending_slot_utc'],
            'generation': row['generation'],
            'completed_count': row['completed_count'],
            'last_run_utc': row['last_run_utc']}


def bind_routine_cursor(root, *, owner_scope, profile_id, legacy_id,
                        recorded_at_utc, activation_epoch, source_revision,
                        owner_route, schedule_expr=None, schedule_zone=None,
                        completed_count=None, last_run_at=None):
    # Binding is storage evidence only; no quiescence, execution or grant claim.
    cursor_hash = _routine_hash(owner_scope, profile_id, legacy_id)
    recorded = _routine_utc(recorded_at_utc, 'recorded cursor')
    binding = _routine_binding(activation_epoch, source_revision, owner_route)
    captured = _captured_schedule(schedule_expr, schedule_zone, completed_count, last_run_at)
    if captured is not None and captured[3] is not None and captured[3] >= recorded:
        raise ValueError('captured routine last run must precede recorded cursor')
    with _db(root) as db:
        existing = db.execute('SELECT * FROM routine_cursors WHERE cursor_hash=?',
                              (cursor_hash,)).fetchone()
        if existing:
            if (existing['binding_digest'] != binding or existing['initial_utc'] != recorded
                    or (existing['schedule_expr'], existing['schedule_zone'],
                        existing['initial_completed_count'], existing['initial_last_run_utc']) !=
                       (captured if captured is not None else (None, None, None, None))):
                raise RuntimeError('routine cursor conflicts with prior binding')
            return _cursor_state(existing)
        # v17 fences lack a routine identity. Any such evidence holds all bindings;
        # do not infer identity from a job label, prompt or slot hash.
        if db.execute('SELECT 1 FROM routine_slots WHERE routine_hash IS NULL OR routine_hash=? LIMIT 1',
                      (cursor_hash,)).fetchone():
            raise RuntimeError('routine slot history requires reconciliation before cursor binding')
        db.execute('''INSERT INTO routine_cursors
            (cursor_hash,binding_digest,initial_utc,current_utc,schedule_expr,
             schedule_zone,completed_count,last_run_utc,initial_completed_count,
             initial_last_run_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?)''', (cursor_hash, binding, recorded, recorded,
                *(captured if captured is not None else (None, None, None, None)),
                *((captured[2], captured[3]) if captured is not None else (None, None))))
        return {'cursor_hash': cursor_hash, 'current_utc': recorded,
                'pending_job_id': None, 'pending_slot_utc': None, 'generation': 0,
                'completed_count': captured[2] if captured is not None else None,
                'last_run_utc': captured[3] if captured is not None else None}


def admit_routine_cursor_slot(root, *, owner_scope, profile_id, legacy_id,
                              expected_recorded_at_utc, next_provisional_at_utc,
                              activation_epoch, source_revision, owner_route,
                              payload, not_before=None, valid_until=None,
                              priority=20):
    # Prior pending work holds later slots, even when normal WorkQueue rows prune.
    # Reanchor requires a future explicit trusted execution/outbox reconciliation.
    expected = _routine_utc(expected_recorded_at_utc, 'recorded cursor')
    successor = _routine_utc(next_provisional_at_utc, 'provisional successor')
    if successor <= expected:
        raise ValueError('routine provisional successor must follow recorded cursor')
    # Freeze this caller-owned container once so the cursor binding and slot
    # request cannot describe different recipients if another thread mutates it.
    try:
        owner_route = json.loads(_routine_json(owner_route))
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError('invalid routine owner route JSON') from error
    binding = _routine_binding(activation_epoch, source_revision, owner_route)
    prepared = _prepare_routine_slot(
        owner_scope, profile_id, legacy_id, expected_recorded_at_utc,
        activation_epoch, source_revision, owner_route, payload,
        not_before, valid_until, priority, cursor_successor=successor)
    cursor_hash = prepared['routine_hash']
    with _db(root) as db:
        row = db.execute('SELECT * FROM routine_cursors WHERE cursor_hash=?',
                         (cursor_hash,)).fetchone()
        if row is None:
            raise RuntimeError('routine cursor is not bound')
        if row['binding_digest'] != binding:
            raise RuntimeError('routine cursor binding changed')
        if row['pending_job_id'] is not None:
            if (row['pending_job_id'] != prepared['job_id']
                    or row['pending_slot_utc'] != expected
                    or row['pending_provisional_utc'] != successor):
                raise RuntimeError('routine cursor has unresolved pending job')
            receipt = _admit_routine_slot_in_tx(db, prepared)
            if receipt['admission'] == 'new':
                raise RuntimeError('routine cursor missing its slot fence')
            return {**receipt, 'cursor': _cursor_state(row)}
        if row['current_utc'] != expected:
            raise RuntimeError('routine cursor CAS failed')
        receipt = _admit_routine_slot_in_tx(db, prepared)
        if receipt['admission'] != 'new':
            raise RuntimeError('routine slot predates cursor admission')
        updated = db.execute('''UPDATE routine_cursors SET current_utc=?,
            pending_slot_utc=?,pending_job_id=?,pending_provisional_utc=?,
            generation=generation+1
            WHERE cursor_hash=? AND current_utc=? AND pending_job_id IS NULL''',
            (successor, expected, prepared['job_id'], successor,
             cursor_hash, expected))
        if updated.rowcount != 1:
            raise RuntimeError('routine cursor CAS failed')
        row = db.execute('SELECT * FROM routine_cursors WHERE cursor_hash=?',
                         (cursor_hash,)).fetchone()
        return {**receipt, 'cursor': _cursor_state(row)}


def reconcile_routine_cursor(root, *, owner_scope, profile_id, legacy_id,
                             activation_epoch, source_revision, owner_route,
                             schedule_expr, schedule_zone, expected_generation,
                             pending_job_id, evidence, successor_after_completion=None):
    """Trusted receiver CAS after explicit silent or terminal outbox proof.

    Caller must independently revalidate current routine authority and obtain an
    outbox-owned receipt. The trusted receiver may supply a fresh wall-date
    successor; the default preserves native captured schedule semantics. This
    callable never comes from work payloads or model arguments. WorkQueue cannot
    atomically read another JSON store.
    Missing/unknown/failed outbox evidence holds; `jobs.done` alone proves no
    channel acceptance. This never starts a clock or authorizes a routine.
    Terminal outbox evidence currently prunes after seven days. If authority
    remains unavailable until its receipt prunes, this cursor stays held for
    separately authenticated manual reconciliation; slot hashes cannot recover
    delivery or effects proof and must never cause an automatic resend.
    """
    cursor_hash = _routine_hash(owner_scope, profile_id, legacy_id)
    binding = _routine_binding(activation_epoch, source_revision, owner_route)
    if (type(expected_generation) is not int or expected_generation < 1
            or not isinstance(pending_job_id, str) or not pending_job_id):
        raise ValueError('invalid routine reconciliation identity')
    if not isinstance(evidence, dict):
        raise ValueError('invalid routine reconciliation evidence')
    try:
        evidence_digest = hashlib.sha256(_routine_json(evidence).encode('utf-8')).hexdigest()
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError('invalid routine reconciliation evidence') from error
    with _db(root) as db:
        row = db.execute('SELECT * FROM routine_cursors WHERE cursor_hash=?',
                         (cursor_hash,)).fetchone()
        if (row is None or row['binding_digest'] != binding or
                row['schedule_expr'] != schedule_expr or row['schedule_zone'] != schedule_zone):
            raise RuntimeError('routine reconciliation binding changed')
        if row['pending_job_id'] is None:
            if (row['generation'] == expected_generation and
                    row['last_reconciled_job_id'] == pending_job_id and
                    row['last_evidence_digest'] == evidence_digest):
                return {'status': 'existing', 'next_cursor': row['current_utc']}
            raise RuntimeError('routine reconciliation has no matching pending job')
        if row['generation'] != expected_generation or row['pending_job_id'] != pending_job_id:
            raise RuntimeError('routine reconciliation CAS failed')
        slot = db.execute('SELECT * FROM routine_slots WHERE job_id=? AND routine_hash=?',
                          (pending_job_id, cursor_hash)).fetchone()
        if (slot is None or slot['last_work_status'] != 'done' or
                not isinstance(slot['completion_utc'], str)):
            raise RuntimeError('routine completion evidence is unavailable')
        job = db.execute('SELECT status FROM jobs WHERE id=?', (pending_job_id,)).fetchone()
        if job is not None and job['status'] != 'done':
            raise RuntimeError('routine work is not finished')
        if slot['handoff_kind'] == 'silent':
            if evidence != {'kind': 'silent'} or slot['handoff_digest'] is not None:
                raise RuntimeError('routine silent evidence mismatch')
        elif slot['handoff_kind'] == 'outbox':
            if (set(evidence) != {'kind', 'run_id', 'digest', 'status', 'acceptance',
                                  'acceptance_uncertain', 'effects_pending', 'effects_status',
                                  'accepted_at'} or
                    evidence['kind'] != 'outbox' or evidence['run_id'] != pending_job_id or
                    evidence['digest'] != slot['handoff_digest'] or
                    evidence['status'] != 'delivered' or evidence['acceptance'] != 'accepted' or
                    evidence['acceptance_uncertain'] is not False or
                    evidence['effects_pending'] is not False or
                    evidence['effects_status'] != 'applied' or
                    type(evidence['accepted_at']) not in (int, float) or
                    not math.isfinite(evidence['accepted_at'])):
                raise RuntimeError('routine outbox is not authoritatively settled')
        else:
            raise RuntimeError('routine handoff evidence is unavailable')
        completed_at = datetime.fromisoformat(slot['completion_utc'].replace('Z', '+00:00'))
        if (completed_at.astimezone(timezone.utc) <
                datetime.fromisoformat(row['pending_slot_utc'].replace('Z', '+00:00')).astimezone(timezone.utc)):
            raise RuntimeError('routine completion precedes its admitted slot')
        if row['last_run_utc'] is not None and slot['completion_utc'] <= row['last_run_utc']:
            raise RuntimeError('routine completion did not advance')
        successor_fn = successor_after_completion or _native_cursor_module().successor_after_native_completion
        next_local = successor_fn(expr=row['schedule_expr'], completed_at=completed_at, zone=row['schedule_zone'])
        successor = _routine_utc(datetime.fromisoformat(next_local), 'completion successor')
        if successor <= row['pending_slot_utc']:
            raise RuntimeError('routine completion successor is not forward')
        db.execute('''UPDATE routine_cursors SET current_utc=?,pending_slot_utc=NULL,
            pending_job_id=NULL,pending_provisional_utc=NULL,
            completed_count=completed_count+1,last_run_utc=?,
            last_evidence_digest=?,last_reconciled_job_id=? WHERE cursor_hash=?''',
            (successor, slot['completion_utc'], evidence_digest, pending_job_id, cursor_hash))
        return {'status': 'reconciled', 'next_cursor': successor}

def event_item_key(event) -> str:
    """Canonical durable identity for one provider event, independent of batch grouping."""
    stable = {k: event[k] for k in ('source', 'rowid', 'source_id', 'id', 'timestamp') if k in event}
    if len(stable) < 2:
        stable = event
    return 'event:' + hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def enqueue(root, kind, payload, key=None, not_before=None, valid_until=None, priority=20,
            item_keys=None, retry_terminal=False, shared_fixed_brief_wake=False, feedback=None, admission=None):
    """Commit before acknowledging ingress/removing a held item. Replays keep one ID."""
    wake_name = None
    if shared_fixed_brief_wake:
        label = payload.get('label') if isinstance(payload, dict) else None
        if (kind != 'run' or item_keys is not None or not isinstance(label, str)
                or not label.startswith('brief:')):
            raise ValueError('invalid fixed brief wake admission')
        wake_name = _fixed_identity(label[6:], 'brief wake name')
    admission_json = (json.dumps(admission, sort_keys=True, separators=(',', ':'), allow_nan=False)
                      if admission is not None else None)
    if admission_json is not None and len(admission_json.encode()) > 2048:
        raise ValueError('work admission exceeds bound')
    now = time.time()
    identity = str(key) if key is not None else secrets.token_hex(16)
    job_id = job_id_for(kind, identity)
    encoded = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    with _db(root) as db:
        if admission_json is not None:
            prior_event = db.execute("SELECT id,admission FROM jobs WHERE admission IS NOT NULL AND "
                                     "json_extract(admission,'$.event_id')=?",
                                     (admission['event_id'],)).fetchone()
            if prior_event is not None and (prior_event['id'] != job_id
                                            or prior_event['admission'] != admission_json):
                raise ValueError('work admission conflict')
            prior = db.execute('SELECT admission FROM jobs WHERE id=?', (job_id,)).fetchone()
            if prior is not None:
                if prior['admission'] != admission_json:
                    raise ValueError('work admission conflict')
                return job_id
        if db.execute('SELECT 1 FROM routine_slots WHERE job_id=?', (job_id,)).fetchone():
            raise RuntimeError('routine slot requires protected admission')
        fixed = db.execute('SELECT name,last_work_status FROM fixed_slots WHERE job_id=?',
                           (job_id,)).fetchone()
        wake = db.execute('SELECT name,last_work_status FROM fixed_brief_wakes WHERE job_id=?',
                          (job_id,)).fetchone()
        if wake and wake['name'] != wake_name:
            raise RuntimeError('fixed brief wake identity changed')
        if fixed and wake_name != fixed['name']:
            raise RuntimeError('fixed slot requires protected admission')
        # Terminal rows are evidence that this admitted request exhausted its bounded attempt
        # budget (or deadline). A scheduler replay must not silently mint another budget forever.
        # An ingress path with a real repeated request can opt in explicitly; that atomically starts
        # a fresh admission while successful and in-flight identities remain deduplicated.
        terminal = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if (fixed or wake) and terminal is None:
            prior_status = fixed['last_work_status'] if fixed else wake['last_work_status']
            if prior_status == 'done':
                return job_id  # The pruned successful occurrence remains fenced forever.
            raise RuntimeError('fixed work outcome requires reconciliation')
        if terminal and terminal['status'] in ('failed', 'expired'):
            if not retry_terminal:
                raise RuntimeError(f"work request is terminal ({terminal['status']})")
            db.execute("DELETE FROM work_items WHERE job_id=?", (job_id,))
        if item_keys is not None:
            keys = list(map(str, item_keys))
            events = payload.get('bundle', {}).get('events', [])
            if len(keys) != len(events) or len(set(keys)) != len(keys) or not keys:
                raise ValueError('work input keys must match distinct bundle events')
            ownership = {r['item_key']: (r['job_id'], r['status']) for r in db.execute(
                '''SELECT w.item_key,w.job_id,j.status FROM work_items w JOIN jobs j ON j.id=w.job_id
                   WHERE w.kind=?''', (kind,))}
            terminal_keys = [k for k in keys if k in ownership and ownership[k][1] in ('failed', 'expired')]
            if terminal_keys and not retry_terminal:
                raise RuntimeError(f"work request is terminal ({ownership[terminal_keys[0]][1]})")
            if retry_terminal:
                db.executemany('DELETE FROM work_items WHERE kind=? AND item_key=?',
                               [(kind, key) for key in terminal_keys])
                ownership = {k: v for k, v in ownership.items() if v[1] not in ('failed', 'expired')}
            owned = {k: v[0] for k, v in ownership.items()}
            fresh = [i for i, k in enumerate(keys) if k not in owned]
            if not fresh:
                return owned[keys[0]]
            payload = json.loads(encoded)
            payload['bundle']['events'] = [events[i] for i in fresh]
            item_keys = [keys[i] for i in fresh]
            job_id = hashlib.sha256((kind + ':items:' + '\n'.join(sorted(item_keys))).encode()).hexdigest()[:32]
            encoded = json.dumps(payload, separators=(',', ':'), sort_keys=True)
            if db.execute('SELECT 1 FROM routine_slots WHERE job_id=?', (job_id,)).fetchone():
                raise RuntimeError('routine slot requires protected admission')
            if db.execute('SELECT 1 FROM fixed_slots WHERE job_id=?', (job_id,)).fetchone():
                raise RuntimeError('fixed slot requires protected admission')
            terminal = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if terminal and terminal['status'] in ('failed', 'expired'):
                if not retry_terminal:
                    raise RuntimeError(f"work request is terminal ({terminal['status']})")
                db.execute("DELETE FROM work_items WHERE job_id=?", (job_id,))
        db.execute('''INSERT OR IGNORE INTO jobs
            (id,kind,payload,status,created,due,valid_until,priority,feedback,admission)
            VALUES (?,?,?,'pending',?,?,?,?,?,?)''',
            (job_id, kind, encoded, now, now if not_before is None else not_before,
             valid_until, priority, json.dumps(feedback) if feedback else None, admission_json))
        if terminal and terminal['status'] in ('failed', 'expired'):
            db.execute('''UPDATE jobs SET kind=?,payload=?,status='pending',created=?,due=?,
                       valid_until=?,priority=?,attempts=0,owner=NULL,lease_until=NULL,result=NULL,
                       handoff_attempts=0,provider_recoveries=0,error=NULL,finished=NULL,feedback=? WHERE id=?''',
                       (kind, encoded, now, now if not_before is None else not_before,
                        valid_until, priority, json.dumps(feedback) if feedback else None, job_id))
            if fixed:
                db.execute("UPDATE fixed_slots SET last_work_status='admitted' WHERE job_id=?",
                           (job_id,))
            if wake:
                db.execute("UPDATE fixed_brief_wakes SET last_work_status='admitted' WHERE job_id=?",
                           (job_id,))
        if wake_name is not None and wake is None:
            db.execute('''INSERT INTO fixed_brief_wakes
                (job_id,name,admitted,last_work_status) VALUES (?,?,?,?)''',
                (job_id, wake_name, now,
                 'admitted' if terminal is None or terminal['status'] in ('failed', 'expired')
                 else terminal['status']))
        if item_keys is not None:
            db.executemany('INSERT OR IGNORE INTO work_items(kind,item_key,job_id) VALUES (?,?,?)',
                           [(kind, k, job_id) for k in item_keys])
    return job_id


def _fixed_identity(value, label):
    if not isinstance(value, str) or not value or len(value) > 160 or value != value.strip():
        raise ValueError(f'invalid fixed {label}')
    return value


def _fixed_hash(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _fixed_digest(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError(f'invalid fixed {label}')
    return value


def _check_fixed_authority(db, authority_digest, schedule_digest=None):
    """One tenant/profile owns the slot history; a schedule change rebinds without losing it.

    `authority_digest` is the identity (tenant + profile) and never rebinds: a different one
    means another installation's history, so admission stops for reconciliation. The schedule
    digest (zone, crons.json jobs, owner route) is configuration: a change is one logged UPDATE
    under this write transaction, and the existing slot rows keep fencing their wall slots, so
    an already admitted slot is never re-run and no future slot is withheld. With no
    `schedule_digest` (a read) the identity is only checked and nothing is written.
    """
    authority_digest = _fixed_digest(authority_digest, 'schedule authority')
    if schedule_digest is not None:
        schedule_digest = _fixed_digest(schedule_digest, 'schedule digest')
    row = db.execute('SELECT authority_digest,schedule_digest FROM fixed_schedule_authority '
                     'WHERE singleton=1').fetchone()
    if row is None:
        if db.execute('SELECT 1 FROM fixed_slots LIMIT 1').fetchone():
            raise RuntimeError('fixed schedule authority evidence is missing')
        if schedule_digest is not None:
            db.execute('INSERT INTO fixed_schedule_authority VALUES (1,?,?)',
                       (authority_digest, schedule_digest))
        return
    if row['schedule_digest'] is None:
        # Legacy pre-release row: its digest mixed identity with schedule, so identity cannot
        # be recovered from it, and refusing would stop every fixed procedure for good. Adopt
        # the current identity once (the supervisor's epoch fences tenant and profile before a
        # supervised receiver starts) and keep the slot history that prevents re-runs.
        if schedule_digest is not None:
            db.execute('UPDATE fixed_schedule_authority SET authority_digest=?,schedule_digest=? '
                       'WHERE singleton=1', (authority_digest, schedule_digest))
            print('[sotto] fixed schedule authority: adopted legacy binding '
                  f"({'same' if row['authority_digest'] == schedule_digest else 'changed'} "
                  'schedule); slot history kept', flush=True)
        return
    if row['authority_digest'] != authority_digest:
        raise RuntimeError('fixed schedule authority changed; reconciliation required')
    if schedule_digest is not None and row['schedule_digest'] != schedule_digest:
        db.execute('UPDATE fixed_schedule_authority SET schedule_digest=? WHERE singleton=1',
                   (schedule_digest,))
        print('[sotto] fixed schedule authority: zone, crons or route changed; rebound, '
              'slot history kept', flush=True)


def fixed_slot(root, *, authority_digest, name, wall_slot):
    """Read content-free fixed admission evidence; absence is not proof of a prior delivery."""
    slot_hash = _fixed_hash(_fixed_identity(name, 'name'), _fixed_identity(wall_slot, 'wall slot'))
    _fixed_digest(authority_digest, 'schedule authority')
    with _db(root) as db:
        _check_fixed_authority(db, authority_digest)
        row = db.execute('SELECT * FROM fixed_slots WHERE slot_hash=?', (slot_hash,)).fetchone()
        return dict(row) if row else None


def skip_fixed_slot(root, *, authority_digest, schedule_digest, name, wall_slot):
    """Spend an unavailable interval lane once. Daily/weekly slots must not call this."""
    slot_hash = _fixed_hash(_fixed_identity(name, 'name'), _fixed_identity(wall_slot, 'wall slot'))
    with _db(root) as db:
        _check_fixed_authority(db, authority_digest, schedule_digest)
        db.execute('''INSERT OR IGNORE INTO fixed_slots
            (slot_hash,name,job_id,work_key_digest,admitted,last_work_status)
            VALUES (?,?,NULL,NULL,?,'skipped_unlinked')''', (slot_hash, name, time.time()))
        row = db.execute('SELECT last_work_status FROM fixed_slots WHERE slot_hash=?',
                         (slot_hash,)).fetchone()
        return row['last_work_status'] == 'skipped_unlinked'


def enqueue_fixed_slot(root, payload, *, key, authority_digest, schedule_digest, name, wall_slot,
                       not_before=None, valid_until=None, priority=20,
                       retry_terminal=False, replace_pending=False, recovery_from=None):
    """Atomically admit one fixed wall slot and its ordinary run job.

    A caller may replace an unstarted revision, or supply an already verified
    unaccepted handoff's exact old job ID. This API does not verify outbox proof
    and must only be called by the receiver's trusted cron path.
    """
    name = _fixed_identity(name, 'name')
    wall_slot = _fixed_identity(wall_slot, 'wall slot')
    key = _fixed_identity(key, 'work key')
    if recovery_from is not None and (
            not isinstance(recovery_from, str) or re.fullmatch(r'[0-9a-f]{32}', recovery_from) is None):
        raise ValueError('invalid fixed recovery job')
    if not isinstance(payload, dict) or payload.get('label') != f'cron:{name}':
        raise ValueError('fixed job label does not match the schedule')
    encoded = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    job_id = job_id_for('run', key)
    slot_hash = _fixed_hash(name, wall_slot)
    key_digest = _fixed_hash(key)
    now = time.time()
    with _db(root) as db:
        _check_fixed_authority(db, authority_digest, schedule_digest)
        row = db.execute('SELECT * FROM fixed_slots WHERE slot_hash=?', (slot_hash,)).fetchone()
        old_job = db.execute('SELECT status,result FROM jobs WHERE id=?',
                             (row['job_id'],)).fetchone() if row and row['job_id'] else None
        current = db.execute('SELECT kind,status,payload FROM jobs WHERE id=?', (job_id,)).fetchone()
        wake = db.execute('SELECT name,last_work_status FROM fixed_brief_wakes WHERE job_id=?',
                          (job_id,)).fetchone()
        if row is None and (current is not None or wake is not None):
            if wake is None or wake['name'] != name:
                raise RuntimeError('existing fixed work identity is unrelated')
            if current is None:
                if wake['last_work_status'] != 'done':
                    raise RuntimeError('fixed brief wake outcome requires reconciliation')
                # The wake completed before this scheduled slot and its operational row
                # has pruned. Adopt its permanent ID without reconstructing work.
                db.execute('''INSERT INTO fixed_slots
                    (slot_hash,name,job_id,work_key_digest,admitted,last_work_status)
                    VALUES (?,?,?,?,?,'done')''',
                    (slot_hash, name, job_id, key_digest, now))
                return job_id
            if current['kind'] != 'run':
                raise RuntimeError('existing fixed work identity is unrelated')
            if current['status'] in ('pending', 'ready', 'leased'):
                try:
                    existing_label = json.loads(current['payload'])['label']
                except (TypeError, ValueError, KeyError) as error:
                    raise RuntimeError('existing fixed work identity is unavailable') from error
                if existing_label != f'brief:{name}':
                    raise RuntimeError('existing fixed work identity is unrelated')
        if row and row['job_id'] is None:
            return None  # A deliberately skipped interval cannot mint later work.
        if row and row['job_id'] == job_id and (
                current is None or current['status'] not in ('failed', 'expired')):
            # The permanent fence remains authoritative after ordinary work pruning.
            # Never reconstruct a pruned job or rewrite a completed disposition on replay.
            return job_id
        if (row and row['job_id'] != job_id and current
                and current['status'] not in ('failed', 'expired')):
            return row['job_id']  # Do not rebind a slot to an older live/completed revision.
        if row and row['job_id'] != job_id:
            old_status = old_job['status'] if old_job else row['last_work_status']
            if replace_pending and old_job and old_status == 'pending' and old_job['result'] is None:
                db.execute('DELETE FROM jobs WHERE id=? AND status=?', (row['job_id'], 'pending'))
            elif recovery_from == row['job_id'] and old_status in ('done', 'failed', 'expired'):
                pass  # The receiver verified exact unaccepted outbox evidence before this call.
            elif old_status in ('failed', 'expired'):
                pass  # A changed context revision may receive its own bounded work budget.
            else:
                return row['job_id']  # Preserve a leased/saved/completed result.
        if current and current['status'] in ('failed', 'expired') and not retry_terminal:
            raise RuntimeError(f"work request is terminal ({current['status']})")
        if current and current['status'] in ('failed', 'expired'):
            db.execute('''UPDATE jobs SET kind='run',payload=?,status='pending',created=?,due=?,
                       valid_until=?,priority=?,attempts=0,owner=NULL,lease_until=NULL,result=NULL,
                       handoff_attempts=0,provider_recoveries=0,error=NULL,finished=NULL WHERE id=?''',
                       (encoded, now, now if not_before is None else not_before,
                        valid_until, priority, job_id))
            db.execute("UPDATE fixed_brief_wakes SET last_work_status='admitted' WHERE job_id=?",
                       (job_id,))
        else:
            db.execute('''INSERT OR IGNORE INTO jobs
                (id,kind,payload,status,created,due,valid_until,priority)
                VALUES (?,'run',?,'pending',?,?,?,?)''',
                (job_id, encoded, now, now if not_before is None else not_before,
                 valid_until, priority))
        if row:
            db.execute('''UPDATE fixed_slots SET job_id=?,work_key_digest=?,admitted=?,
                         last_work_status='admitted',revision=revision+1 WHERE slot_hash=?''',
                       (job_id, key_digest, now, slot_hash))
        else:
            db.execute('''INSERT INTO fixed_slots
                (slot_hash,name,job_id,work_key_digest,admitted,last_work_status)
                VALUES (?,?,?,?,?,?)''',
                (slot_hash, name, job_id, key_digest, now,
                 current['status'] if current is not None else 'admitted'))
        return job_id


def owned_items(root, kind, item_keys):
    """Accepted queue identities survive payload cleanup and alternate batch grouping."""
    wanted = set(map(str, item_keys))
    with _db(root) as db:
        return {r['item_key']: r['job_id'] for r in db.execute(
            'SELECT item_key,job_id FROM work_items WHERE kind=?', (kind,)) if r['item_key'] in wanted}


def _decode(row):
    if row is None:
        return None
    out = dict(row)
    out['payload'] = json.loads(out['payload'])
    out['result'] = json.loads(out['result']) if out['result'] else None
    return out


def claim(root, owner, now=None):
    """At most two workers and one maintenance worker; aged maintenance gets the next free slot."""
    now = time.time() if now is None else now
    with _db(root) as db:
        # Preserve only the last WorkQueue disposition when its seven-day
        # operational row disappears. It is not a channel acceptance receipt.
        db.execute('''UPDATE routine_slots SET last_work_status=(
                       SELECT j.status FROM jobs j WHERE j.id=routine_slots.job_id)
                     WHERE job_id IN (SELECT id FROM jobs WHERE finished IS NOT NULL
                                      AND finished < ?)''', (now - RETENTION_SECONDS,))
        db.execute('''UPDATE fixed_slots SET last_work_status=(
                       SELECT j.status FROM jobs j WHERE j.id=fixed_slots.job_id)
                     WHERE job_id IN (SELECT id FROM jobs WHERE finished IS NOT NULL
                                      AND finished < ?)''', (now - RETENTION_SECONDS,))
        db.execute('''UPDATE fixed_brief_wakes SET last_work_status=(
                       SELECT j.status FROM jobs j WHERE j.id=fixed_brief_wakes.job_id)
                     WHERE job_id IN (SELECT id FROM jobs WHERE finished IS NOT NULL
                                      AND finished < ?)''', (now - RETENTION_SECONDS,))
        db.execute("DELETE FROM work_items WHERE job_id IN (SELECT id FROM jobs WHERE finished IS NOT NULL AND finished < ?)",
                   (now - RETENTION_SECONDS,))
        db.execute("DELETE FROM jobs WHERE finished IS NOT NULL AND finished < ?", (now - RETENTION_SECONDS,))
        db.execute("""UPDATE jobs SET status=CASE WHEN result IS NULL THEN 'pending' ELSE 'ready' END,
                   owner=NULL WHERE status='leased' AND lease_until<=?""", (now,))
        db.execute('''UPDATE fixed_slots SET last_work_status=(
                       SELECT j.status FROM jobs j WHERE j.id=fixed_slots.job_id)
                     WHERE job_id IN (SELECT id FROM jobs WHERE owner IS NULL
                                      AND status IN ('pending','ready'))
                       AND last_work_status IN ('leased','result_saved')''')
        db.execute('''UPDATE fixed_brief_wakes SET last_work_status=(
                       SELECT j.status FROM jobs j WHERE j.id=fixed_brief_wakes.job_id)
                     WHERE job_id IN (SELECT id FROM jobs WHERE owner IS NULL
                                      AND status IN ('pending','ready'))
                       AND last_work_status IN ('leased','result_saved')''')
        db.execute("""UPDATE jobs SET status='expired',payload='{}',result=NULL,
                   finished=?,owner=NULL WHERE status IN ('pending','ready')
                   AND valid_until IS NOT NULL AND valid_until<=?""", (now, now))
        db.execute("""UPDATE fixed_slots SET last_work_status='expired'
                   WHERE job_id IN (SELECT id FROM jobs WHERE status='expired')""")
        db.execute("""UPDATE fixed_brief_wakes SET last_work_status='expired'
                   WHERE job_id IN (SELECT id FROM jobs WHERE status='expired')""")
        active = db.execute("SELECT priority FROM jobs WHERE status='leased'").fetchall()
        if len(active) >= MAX_WORKERS:
            return None
        ceiling = BACKGROUND_PRIORITY if any(r['priority'] >= BACKGROUND_PRIORITY for r in active) else 10000
        # Continuous event traffic otherwise starves memory indefinitely. Once maintenance has
        # waited 30 minutes since it became runnable, admit the oldest such job before another
        # foreground job. This never preempts work or fills the foreground's reserved slot.
        while True:
            row = None
            if ceiling > BACKGROUND_PRIORITY:
                row = db.execute("""SELECT * FROM jobs WHERE status IN ('pending','ready') AND due<=?
                                  AND priority>=? AND max(created,due)<=?
                                  ORDER BY max(created,due),created LIMIT 1""",
                                 (now, BACKGROUND_PRIORITY, now - BACKGROUND_MAX_WAIT_SECONDS)).fetchone()
            if row is None:
                row = db.execute("""SELECT * FROM jobs WHERE status IN ('pending','ready') AND due<=?
                                  AND priority<? ORDER BY priority,due,created LIMIT 1""", (now, ceiling)).fetchone()
            if row is None:
                return None
            # Composition and its delivery handoff have independent bounded budgets: a saved result
            # never recomposes, but it also cannot retry a broken handoff forever.
            exhausted = (row['attempts'] - row['provider_recoveries'] >= MAX_ATTEMPTS if row['result'] is None
                         else row['handoff_attempts'] >= MAX_ATTEMPTS)
            if not exhausted:
                break
            diagnostic = ('composition attempts exhausted' if row['result'] is None
                          else 'delivery handoff attempts exhausted')
            db.execute("""UPDATE jobs SET status='failed',payload='{}',result=NULL,error=?,
                       finished=?,owner=NULL,lease_until=NULL WHERE id=?""",
                       (diagnostic, now, row['id']))
            db.execute("UPDATE fixed_slots SET last_work_status='failed' WHERE job_id=?",
                       (row['id'],))
            db.execute("UPDATE fixed_brief_wakes SET last_work_status='failed' WHERE job_id=?",
                       (row['id'],))
        db.execute("""UPDATE jobs SET status='leased',owner=?,lease_until=?,
                   attempts=attempts+?,handoff_attempts=handoff_attempts+? WHERE id=?""",
                   (owner, now + LEASE_SECONDS, int(row['result'] is None),
                    int(row['result'] is not None), row['id']))
        db.execute("UPDATE fixed_slots SET last_work_status=? WHERE job_id=?",
                   ('result_saved' if row['result'] is not None else 'leased', row['id']))
        db.execute("UPDATE fixed_brief_wakes SET last_work_status=? WHERE job_id=?",
                   ('result_saved' if row['result'] is not None else 'leased', row['id']))
        return _decode(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())


def renew(root, job_id, owner, now=None):
    with _db(root) as db:
        return db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND owner=? AND status='leased'",
                          ((time.time() if now is None else now) + LEASE_SECONDS, job_id, owner)).rowcount == 1


def save_result(root, job_id, owner, result):
    """Keep the lease through the first handoff; the exact output is recoverable on expiry."""
    with _db(root) as db:
        if not db.execute("""UPDATE jobs SET result=?,handoff_attempts=handoff_attempts+1
                          WHERE id=? AND owner=? AND status='leased' AND result IS NULL""",
                          (json.dumps(result), job_id, owner)).rowcount:
            raise RuntimeError('work lease lost before result commit')
        db.execute("UPDATE fixed_slots SET last_work_status='result_saved' WHERE job_id=?",
                   (job_id,))
        db.execute("UPDATE fixed_brief_wakes SET last_work_status='result_saved' WHERE job_id=?",
                   (job_id,))


def routine_slot_for_job(root, job_id):
    with _db(root) as db:
        row = db.execute('''SELECT slot_hash,routine_hash,completion_utc,
                            handoff_kind,handoff_digest FROM routine_slots WHERE job_id=?''',
                         (job_id,)).fetchone()
        return dict(row) if row else None


def routine_job_matches_authority(root, *, owner_scope, profile_id, legacy_id,
                                  activation_epoch, source_revision, owner_route,
                                  schedule_expr, schedule_zone, expected_generation,
                                  pending_job_id):
    """Read current slot/cursor binding before a routine worker or outbox effect.

    This storage check does not establish the caller's authority: the receiver
    must obtain these fields from authenticated current migration/route state.
    """
    if type(expected_generation) is not int or expected_generation < 1:
        return False
    cursor_hash = _routine_hash(owner_scope, profile_id, legacy_id)
    binding = _routine_binding(activation_epoch, source_revision, owner_route)
    with _db(root) as db:
        row = db.execute('''SELECT binding_digest,schedule_expr,schedule_zone,
            pending_job_id,pending_slot_utc,generation FROM routine_cursors WHERE cursor_hash=?''',
            (cursor_hash,)).fetchone()
        if (row is None or row['binding_digest'] != binding or
                row['schedule_expr'] != schedule_expr or row['schedule_zone'] != schedule_zone or
                row['pending_job_id'] != pending_job_id or
                row['generation'] != expected_generation):
            return False
        try:
            scheduled = datetime.fromisoformat(row['pending_slot_utc'].replace('Z', '+00:00'))
            if scheduled.astimezone(timezone.utc) > datetime.now(timezone.utc):
                return False
        except (AttributeError, TypeError, ValueError):
            return False
        return db.execute('''SELECT 1 FROM routine_slots
            WHERE routine_hash=? AND job_id=?''',
            (cursor_hash, pending_job_id)).fetchone() is not None


def pending_routine_job_ids(root, limit=32):
    """Return a durable rotating page so permanently held jobs cannot starve others.

    The existing work heartbeat calls this once per pass. Selection advances in
    the same SQLite transaction; a crash may repeat a page, which is safe because
    reconciliation is idempotent, but cannot permanently skip a pending job.
    """
    if type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError('invalid routine reconciliation limit')
    with _db(root) as db:
        after = db.execute('SELECT after_cursor_hash FROM routine_reconcile_scan WHERE singleton=1').fetchone()[0]
        rows = list(db.execute('''SELECT cursor_hash,pending_job_id FROM routine_cursors
            WHERE pending_job_id IS NOT NULL AND cursor_hash>? ORDER BY cursor_hash LIMIT ?''',
            (after, limit)))
        if len(rows) < limit:
            rows.extend(db.execute('''SELECT cursor_hash,pending_job_id FROM routine_cursors
                WHERE pending_job_id IS NOT NULL AND cursor_hash<=? ORDER BY cursor_hash LIMIT ?''',
                (after, limit-len(rows))))
        if rows:
            db.execute('UPDATE routine_reconcile_scan SET after_cursor_hash=? WHERE singleton=1',
                       (rows[-1]['cursor_hash'],))
        return [row['pending_job_id'] for row in rows]


def finish(root, job_id, owner, *, routine_handoff=None):
    """Finish ordinary work; routine work also stores typed handoff evidence.

    `completion_utc` comes from the previously saved execution result, not from
    provider retry or cursor reconciliation time. It is not channel acceptance.
    """
    with _db(root) as db:
        slot = db.execute('SELECT * FROM routine_slots WHERE job_id=?', (job_id,)).fetchone()
        job = db.execute("SELECT result FROM jobs WHERE id=? AND owner=? AND status='leased'",
                         (job_id, owner)).fetchone()
        if job is None:
            return False
        completion_utc = handoff_kind = handoff_digest = None
        if slot:
            if not isinstance(routine_handoff, dict) or set(routine_handoff) not in (
                    {'kind'}, {'kind', 'digest'}):
                raise RuntimeError('routine handoff evidence is unavailable')
            handoff_kind = routine_handoff['kind']
            handoff_digest = routine_handoff.get('digest')
            if not ((handoff_kind == 'silent' and handoff_digest is None) or
                    (handoff_kind == 'outbox' and isinstance(handoff_digest, str) and
                     re.fullmatch(r'[0-9a-f]{64}', handoff_digest))):
                raise RuntimeError('routine handoff evidence is invalid')
            try:
                saved = json.loads(job['result'])
                completion_utc = _routine_utc(
                    datetime.fromisoformat(saved['_routine_completed_at']), 'completion')
            except (TypeError, ValueError, KeyError) as error:
                raise RuntimeError('routine completion anchor is unavailable') from error
            if slot['completion_utc'] is not None:
                raise RuntimeError('routine completion already recorded')
        elif routine_handoff is not None:
            raise RuntimeError('routine handoff has no admitted slot')
        finished = time.time()
        changed = db.execute("""UPDATE jobs SET status='done',payload='{}',result=NULL,owner=NULL,
                              finished=? WHERE id=? AND owner=? AND status='leased'""",
                             (finished, job_id, owner)).rowcount
        if slot and changed:
            db.execute('''UPDATE routine_slots SET completion_utc=?,handoff_kind=?,
                          handoff_digest=?,last_work_status='done' WHERE job_id=?''',
                       (completion_utc, handoff_kind, handoff_digest, job_id))
        if changed:
            db.execute("UPDATE fixed_slots SET last_work_status='done' WHERE job_id=?", (job_id,))
            db.execute("UPDATE fixed_brief_wakes SET last_work_status='done' WHERE job_id=?",
                       (job_id,))
        return changed == 1


def fail(root, job_id, owner, error, now=None, *, retry_provider=False, model_budget_exhausted=False, model_outcome_uncertain=False):
    """Return the committed disposition, or None when this worker no longer owns the job."""
    now = time.time() if now is None else now
    with _db(root) as db:
        row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=? AND status='leased'", (job_id, owner)).fetchone()
        if row is None:
            return None
        provider_failure = retry_provider and row['kind'] == 'event' and row['result'] is None
        budget_failure = model_budget_exhausted and row['kind'] == 'run' and row['result'] is None
        uncertain_failure = model_outcome_uncertain and row['kind'] == 'run' and row['result'] is None
        recoveries = row['provider_recoveries']
        retired = row['kind'] == 'source_correction'
        if uncertain_failure or budget_failure or retired:
            terminal = True
        elif provider_failure:
            terminal = recoveries >= MAX_PROVIDER_RECOVERIES
            recoveries += int(not terminal)
        else:
            terminal = (row['attempts'] - recoveries >= MAX_ATTEMPTS if row['result'] is None
                        else row['handoff_attempts'] >= MAX_ATTEMPTS)
        due = now + min(900, 60 * 2 ** max(0, max(row['attempts'], row['handoff_attempts']) - 1))
        expired = row['valid_until'] is not None and due >= row['valid_until']
        status = 'failed' if terminal else 'expired' if expired else 'ready' if row['result'] else 'pending'
        final = status in ('failed', 'expired')
        diagnostic = ('model_outcome_uncertain' if uncertain_failure
                      else MODEL_BUDGET_EXHAUSTED if budget_failure
                      else 'delivery handoff attempts exhausted' if terminal and row['result'] is not None
                      else 'provider recovery attempts exhausted' if terminal and provider_failure
                      else 'retry would exceed relevance deadline' if expired and not terminal
                      else str(error)[:120])
        db.execute("""UPDATE jobs SET status=?,due=?,owner=NULL,lease_until=NULL,error=?,finished=?,
                   payload=?,result=?,provider_recoveries=? WHERE id=?""",
                   (status, due, diagnostic, now if final else None, '{}' if final and not retired else row['payload'],
                    None if final else row['result'], recoveries, job_id))
        db.execute('UPDATE fixed_slots SET last_work_status=? WHERE job_id=?', (status, job_id))
        db.execute('UPDATE fixed_brief_wakes SET last_work_status=? WHERE job_id=?',
                   (status, job_id))
        return status


def release(root, job_id, owner, now=None):
    """Hand a leased job back UNCHARGED: the worker was told to stop (a redeploy's SIGTERM), not the
    job failing. `fail` counts every ending against MAX_ATTEMPTS because a crash mid-model-call is
    indistinguishable from a bad job — but a graceful stop is distinguishable, and a platform that
    redeploys on every push would otherwise make a day's brief terminal in three pushes."""
    now = time.time() if now is None else now
    with _db(root) as db:
        changed = db.execute('''UPDATE jobs SET status=CASE WHEN result IS NULL THEN 'pending' ELSE 'ready' END,
                          attempts=max(0, attempts - CASE WHEN result IS NULL THEN 1 ELSE 0 END),
                          handoff_attempts=max(0, handoff_attempts - CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END),
                          owner=NULL, lease_until=NULL, due=? WHERE id=? AND owner=? AND status='leased'
                          ''', (now, job_id, owner)).rowcount == 1
        if changed:
            db.execute('''UPDATE fixed_slots SET last_work_status=(
                          SELECT status FROM jobs WHERE id=?) WHERE job_id=?''',
                       (job_id, job_id))
            db.execute('''UPDATE fixed_brief_wakes SET last_work_status=(
                          SELECT status FROM jobs WHERE id=?) WHERE job_id=?''',
                       (job_id, job_id))
        return changed


def defer(root, job_id, owner, *, delay_seconds=60, max_age_seconds=None, now=None):
    """Release known unbilled contention with a bounded due date, without spending a fault attempt.

    Return pending/ready/expired or None for a stale lease. The original relevance deadline is
    never extended. Callers can put a finite age on work admitted without a deadline.
    """
    now = time.time() if now is None else now
    due = now + max(1, delay_seconds)
    with _db(root) as db:
        row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=? AND status='leased'",
                         (job_id, owner)).fetchone()
        if row is None:
            return None
        deadline = row['valid_until']
        if deadline is None and max_age_seconds is not None:
            deadline = row['created'] + max_age_seconds
        if deadline is not None and due >= deadline:
            db.execute("""UPDATE jobs SET status='expired',payload='{}',result=NULL,
                       owner=NULL,lease_until=NULL,error='retry would exceed relevance deadline',
                       finished=? WHERE id=?""", (now, job_id))
            return 'expired'
        status = 'ready' if row['result'] is not None else 'pending'
        db.execute("""UPDATE jobs SET status=?,
                   attempts=max(0,attempts-?),handoff_attempts=max(0,handoff_attempts-?),
                   owner=NULL,lease_until=NULL,due=? WHERE id=?""",
                   (status, int(row['result'] is None), int(row['result'] is not None), due, job_id))
        return status


def active_job(root, kind, label):
    """The one non-terminal job carrying this label (its payload's `label`), or None."""
    with _db(root) as db:
        for row in db.execute("""SELECT * FROM jobs WHERE kind=? AND status IN ('pending','leased','ready')
                              ORDER BY created""", (kind,)):
            try:
                payload = json.loads(row['payload'])
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get('label') == label:
                return _decode(row)
    return None


def discard_pending(root, job_id):
    """Drop a job nobody has started, without a trace. Deliberately NOT a terminal status: a key
    that later comes back (a calendar edit reverted) must be admittable again, and `enqueue`
    refuses terminal keys by design."""
    with _db(root) as db:
        db.execute("DELETE FROM work_items WHERE job_id IN (SELECT id FROM jobs WHERE id=? AND status='pending')", (job_id,))
        return db.execute("DELETE FROM jobs WHERE id=? AND status='pending'", (job_id,)).rowcount == 1


DIAGNOSTIC_NAMES = re.compile(r'\b(?:ModuleNotFoundError|ImportError|PermissionError|FileNotFoundError|'
                              r'TimeoutError|HTTPError|ConnectionError|JSONDecodeError|ValueError|'
                              r'TypeError|KeyError|OSError|RuntimeError|AskResearchUnconfirmed|'
                              r'AskPersistenceUnconfirmed|AskAnswerUnconfirmed)\b')


def diagnostic_category(stderr) -> str:
    """The ONE sanitizer for a worker's stderr: an allowlisted exception name, or a presence flag.
    Nothing else crosses into the persisted error column or a receipt — a subprocess's stderr can
    carry a message body, an address, or a provider's error page. The receiver and the runners it
    spawns share this function so the two boundaries can't disagree about what may cross."""
    names = DIAGNOSTIC_NAMES.findall(str(stderr or ''))
    return names[-1] if names else ('stderr_present' if str(stderr or '').strip() else 'no_stderr')


def status(root):
    """Metadata only, suitable for health. Never expose prompts or results."""
    path = Path(root) / 'events' / 'work.sqlite3'
    if not path.exists():
        return {'counts': {}, 'oldest_due': None}
    try:
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            counts = {r[0]: r[1] for r in db.execute('SELECT status,count(*) FROM jobs GROUP BY status')}
            oldest = db.execute("SELECT min(due) FROM jobs WHERE status IN ('pending','ready')").fetchone()[0]
            return {'counts': counts, 'oldest_due': oldest}
    except (OSError, sqlite3.Error, ValueError) as error:
        return {'error': type(error).__name__}


def get(root, job_id):
    with _db(root) as db:
        return _decode(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())


def feedback_jobs(root):
    """Terminal work with opt-in, content-free delivery feedback; retained with its queue row."""
    with _db(root) as db:
        rows = db.execute("SELECT id,status,created,finished,feedback FROM jobs "
                          "WHERE feedback IS NOT NULL AND status IN ('done','failed','expired')").fetchall()
        return [{**dict(row), 'feedback': json.loads(row['feedback'])} for row in rows]


def clear_feedback(root, job_id):
    """Only after the outbox durably owns the notice, or delivery needs no notice."""
    with _db(root) as db:
        db.execute('UPDATE jobs SET feedback=NULL WHERE id=?', (job_id,))


def admission_for_event(root, event_id):
    """Bounded immutable admission metadata retained with an existing job, never source text."""
    with _db(root) as db:
        row = db.execute("SELECT admission FROM jobs WHERE admission IS NOT NULL AND "
                         "json_extract(admission,'$.event_id')=? LIMIT 1", (event_id,)).fetchone()
        return json.loads(row['admission']) if row else None
