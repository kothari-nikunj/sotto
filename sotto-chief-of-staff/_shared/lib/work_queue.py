"""Durable ownership of accepted work, before the delivery outbox owns its result.

Only this module writes events/work.sqlite3. Knowledge and delivery keep their own
domain stores. A completed result survives a worker crash without another model call.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

LEASE_SECONDS = 120
MAX_ATTEMPTS = 3
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
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL, created REAL NOT NULL, due REAL NOT NULL,
            valid_until REAL, priority INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            owner TEXT, lease_until REAL, result TEXT, error TEXT, finished REAL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS work_items (
            kind TEXT NOT NULL, item_key TEXT NOT NULL, job_id TEXT NOT NULL,
            PRIMARY KEY(kind,item_key))''')
        db.execute('BEGIN IMMEDIATE')
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


def enqueue(root, kind, payload, key=None, not_before=None, valid_until=None, priority=20,
            item_keys=None, retry_terminal=False):
    """Commit before acknowledging ingress/removing a held item. Replays keep one ID."""
    now = time.time()
    identity = str(key) if key is not None else secrets.token_hex(16)
    job_id = job_id_for(kind, identity)
    encoded = json.dumps(payload, separators=(',', ':'), sort_keys=True)
    with _db(root) as db:
        # Terminal rows are evidence that this admitted request exhausted its bounded attempt
        # budget (or deadline). A scheduler replay must not silently mint another budget forever.
        # An ingress path with a real repeated request can opt in explicitly; that atomically starts
        # a fresh admission while successful and in-flight identities remain deduplicated.
        terminal = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
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
                db.execute("""DELETE FROM work_items WHERE kind=? AND job_id IN
                           (SELECT id FROM jobs WHERE status IN ('failed','expired'))""", (kind,))
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
            terminal = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if terminal and terminal['status'] in ('failed', 'expired'):
                if not retry_terminal:
                    raise RuntimeError(f"work request is terminal ({terminal['status']})")
                db.execute("DELETE FROM work_items WHERE job_id=?", (job_id,))
        db.execute('''INSERT OR IGNORE INTO jobs
            (id,kind,payload,status,created,due,valid_until,priority)
            VALUES (?,?,?,'pending',?,?,?,?)''',
            (job_id, kind, encoded, now, now if not_before is None else not_before,
             valid_until, priority))
        if terminal and terminal['status'] in ('failed', 'expired'):
            db.execute('''UPDATE jobs SET kind=?,payload=?,status='pending',created=?,due=?,
                       valid_until=?,priority=?,attempts=0,owner=NULL,lease_until=NULL,result=NULL,
                       error=NULL,finished=NULL WHERE id=?''',
                       (kind, encoded, now, now if not_before is None else not_before,
                        valid_until, priority, job_id))
        if item_keys is not None:
            db.executemany('INSERT OR IGNORE INTO work_items(kind,item_key,job_id) VALUES (?,?,?)',
                           [(kind, k, job_id) for k in item_keys])
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
        db.execute("DELETE FROM work_items WHERE job_id IN (SELECT id FROM jobs WHERE finished IS NOT NULL AND finished < ?)",
                   (now - RETENTION_SECONDS,))
        db.execute("DELETE FROM jobs WHERE finished IS NOT NULL AND finished < ?", (now - RETENTION_SECONDS,))
        db.execute("""UPDATE jobs SET status=CASE WHEN result IS NULL THEN 'pending' ELSE 'ready' END,
                   owner=NULL WHERE status='leased' AND lease_until<=?""", (now,))
        db.execute("""UPDATE jobs SET status='expired',payload='{}',result=NULL,
                   finished=?,owner=NULL WHERE status IN ('pending','ready')
                   AND valid_until IS NOT NULL AND valid_until<=?""", (now, now))
        active = db.execute("SELECT priority FROM jobs WHERE status='leased'").fetchall()
        if len(active) >= MAX_WORKERS:
            return None
        ceiling = BACKGROUND_PRIORITY if any(r['priority'] >= BACKGROUND_PRIORITY for r in active) else 10000
        # Continuous event traffic otherwise starves memory indefinitely. Once maintenance has
        # waited 30 minutes since it became runnable, admit the oldest such job before another
        # foreground job. This never preempts work or fills the foreground's reserved slot.
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
        # Crashes count as attempts too; a repeatedly killed model call cannot run forever.
        if row['result'] is None and row['attempts'] >= MAX_ATTEMPTS:
            db.execute("UPDATE jobs SET status='failed',payload='{}',finished=? WHERE id=?", (now, row['id']))
            return None
        db.execute("""UPDATE jobs SET status='leased',owner=?,lease_until=?,
                   attempts=attempts+? WHERE id=?""",
                   (owner, now + LEASE_SECONDS, int(row['result'] is None), row['id']))
        return _decode(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())


def renew(root, job_id, owner, now=None):
    with _db(root) as db:
        return db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND owner=? AND status='leased'",
                          ((time.time() if now is None else now) + LEASE_SECONDS, job_id, owner)).rowcount == 1


def save_result(root, job_id, owner, result):
    """Keep the lease through the handoff; the exact output is recoverable on expiry."""
    with _db(root) as db:
        if not db.execute("UPDATE jobs SET result=? WHERE id=? AND owner=? AND status='leased'",
                          (json.dumps(result), job_id, owner)).rowcount:
            raise RuntimeError('work lease lost before result commit')


def finish(root, job_id, owner):
    with _db(root) as db:
        return db.execute("""UPDATE jobs SET status='done',payload='{}',result=NULL,owner=NULL,
                          finished=? WHERE id=? AND owner=? AND status='leased'""",
                          (time.time(), job_id, owner)).rowcount == 1


def fail(root, job_id, owner, error, now=None):
    now = time.time() if now is None else now
    with _db(root) as db:
        row = db.execute("SELECT * FROM jobs WHERE id=? AND owner=? AND status='leased'", (job_id, owner)).fetchone()
        if row is None:
            return
        terminal = row['result'] is None and row['attempts'] >= MAX_ATTEMPTS
        db.execute('''UPDATE jobs SET status=?,due=?,owner=NULL,error=?,finished=?,payload=? WHERE id=?''',
                   ('failed' if terminal else ('ready' if row['result'] else 'pending'),
                    now + min(900, 60 * 2 ** max(0, row['attempts'] - 1)), str(error)[:120],
                    now if terminal else None, '{}' if terminal else row['payload'], job_id))


def release(root, job_id, owner, now=None):
    """Hand a leased job back UNCHARGED: the worker was told to stop (a redeploy's SIGTERM), not the
    job failing. `fail` counts every ending against MAX_ATTEMPTS because a crash mid-model-call is
    indistinguishable from a bad job — but a graceful stop is distinguishable, and a platform that
    redeploys on every push would otherwise make a day's brief terminal in three pushes."""
    now = time.time() if now is None else now
    with _db(root) as db:
        return db.execute('''UPDATE jobs SET status=CASE WHEN result IS NULL THEN 'pending' ELSE 'ready' END,
                          attempts=max(0, attempts - CASE WHEN result IS NULL THEN 1 ELSE 0 END),
                          owner=NULL, lease_until=NULL, due=? WHERE id=? AND owner=? AND status='leased'
                          ''', (now, job_id, owner)).rowcount == 1


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
                              r'TypeError|KeyError|OSError|RuntimeError)\b')


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
