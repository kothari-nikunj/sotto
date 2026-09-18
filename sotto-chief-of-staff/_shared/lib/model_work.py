"""Finite model operations across worker retries; no daily or monetary quota.

Only opaque request revisions and attempt state live here. Successful artifacts belong to the
owning procedure, not this accounting store. ContextVars preserve attribution in nested calls.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from urllib.error import HTTPError


@dataclass(frozen=True)
class Policy:
    attempts: int
    output: int
    thinking: str | None = 'low'


POLICIES = {
    'notification': Policy(2, 4096), 'triage': Policy(2, 2048), 'digest': Policy(2, 4096),
    'memory_extract': Policy(2, 16384), 'memory_curate': Policy(2, 8192),
    'brief': Policy(12, 65536, None),
    'brief_extract': Policy(6, 65536, None), 'brief_critic': Policy(3, 65536, None),
    'brief_revise': Policy(3, 65536, None), 'followup': Policy(2, 8192),
    'research': Policy(4, 8192),
}
RETENTION_DAYS = 90
ATTEMPT_LEASE_SECONDS = 900
MAX_INTERRUPTION_RECOVERIES = 2
ARTIFACT_LOCK_SHARDS = 256
# Only models whose request contract is exercised by transport tests get an override.
LOW_THINKING_MODELS = frozenset({'gemini-3.8-flash', 'gemini-3.7-flash',
                               'gemini-3-flash-preview', 'gemini-3.5-flash-lite'})
_CURRENT = ContextVar('sotto_model_operation', default=None)


class ModelWorkHeldError(RuntimeError):
    """This unchanged operation has no remaining attempts; retain source coverage."""


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def current():
    return _CURRENT.get()


@lru_cache(maxsize=1)
def implementation_revision():
    """A repaired native client must release work parked under its old request contract."""
    root = Path(__file__).resolve().parent
    return revision([(root / name).read_text() for name in
                     ('model_work.py', 'gemini.py', 'gemini_transport.py')])


@contextmanager
def scope(task, evidence, *, owner=None, occurrence=False):
    parent = current()
    # Evidence and request revisions release parked contracts; queue occurrence IDs only reset attempt totals.
    ident = revision({'task': task, 'evidence': evidence, 'policy': vars(POLICIES[task]),
                      'implementation': implementation_revision(),
                      'route': [os.environ.get(k) for k in ('SOTTO_MODEL_PROXY_URL', 'SOTTO_OPENAI_BASE_URL',
                                 'SOTTO_DEPLOYMENT_MODE', 'SOTTO_FALLBACK_MODEL')],
                      'owner': owner or os.environ.get('SOTTO_TENANT_ID', 'local')})
    contract_id = ident
    if occurrence:
        # Receiver job identity survives retries but changes for the next scheduled occurrence.
        occurrence_id = occurrence if isinstance(occurrence, str) else (
            os.environ.get('SOTTO_DELIVERY_RUN_ID') or uuid.uuid4().hex)
        ident = revision([ident, occurrence_id])
    value = {'task': task, 'id': ident, 'contract_id': contract_id, 'parent': parent['id'] if parent else '',
             'run_id': os.environ.get('SOTTO_DELIVERY_RUN_ID', ''), 'attempt': 0}
    token = _CURRENT.set(value)
    try:
        yield value
    finally:
        _CURRENT.reset(token)


PRUNE_INTERVAL_SECONDS = 3600   # retention is a day-scale rule; sweeping on every open is write amplification
_LAST_PRUNE = [0.0]


@contextmanager
def _db():
    root = Path(os.environ.get('SOTTO_DATA', '/data')) / 'events'
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'model-work.sqlite3'
    db = sqlite3.connect(path, timeout=15)
    try:
        os.chmod(path, 0o600)
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, revision TEXT, '
                       'attempts INTEGER NOT NULL, parked INTEGER NOT NULL, updated REAL NOT NULL)')
            # parked is a legacy column; parked_requests is the only request-shape latch.
            db.execute('CREATE INDEX IF NOT EXISTS operations_updated ON operations(updated)')
            db.execute('CREATE TABLE IF NOT EXISTS attempts (operation TEXT, attempt INTEGER, metadata TEXT, '
                       'status TEXT, usage TEXT, created REAL, PRIMARY KEY(operation,attempt))')
            db.execute('CREATE INDEX IF NOT EXISTS attempts_created ON attempts(created)')
            db.execute('CREATE TABLE IF NOT EXISTS parked_requests (operation TEXT, revision TEXT, '
                       'created REAL, PRIMARY KEY(operation,revision))')
            db.execute('CREATE INDEX IF NOT EXISTS parked_created ON parked_requests(created)')
            if time.time() - _LAST_PRUNE[0] >= PRUNE_INTERVAL_SECONDS:   # once an hour per process
                _LAST_PRUNE[0] = time.time()
                db.execute('DELETE FROM parked_requests WHERE created < ?', (time.time() - RETENTION_DAYS * 86400,))
                db.execute('DELETE FROM operations WHERE updated < ?', (time.time() - RETENTION_DAYS * 86400,))
                db.execute('DELETE FROM attempts WHERE created < ?', (time.time() - RETENTION_DAYS * 86400,))
        with db:
            yield db
    finally:
        db.close()


def _abandoned(status, created, metadata):
    if status != 'started' or created < time.time() - ATTEMPT_LEASE_SECONDS:
        return True
    pid = metadata.get('worker_pid')
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
    return False


@contextmanager
def attempt(provider, model, prompt, system=None, schema=None):
    value = current()
    if not value:
        yield
        return
    # Attempt totals survive worker retries. A 400 parks the request contract across occurrences.
    request_revision = revision([provider, model, system, schema, POLICIES[value['task']].output])
    value.pop('usage', None)
    with _db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT revision,attempts FROM operations WHERE id=?', (value['id'],)).fetchone()
        attempts = row[1] if row else 0
        # A dead worker may have paid for its request: retain it as unknown, never refund usage.
        # Allow only two replacement claims after the provider/worker timeout, then hold visibly.
        recovered = db.execute("SELECT COUNT(*) FROM attempts WHERE operation=? AND status='interrupted_unknown'",
                               (value['id'],)).fetchone()[0]
        pending = db.execute("SELECT attempt,status,created,metadata FROM attempts WHERE operation=? AND "
                             "status IN ('started','KeyboardInterrupt','SystemExit') ORDER BY attempt",
                             (value['id'],)).fetchall()
        stale = [(number,) for number, status, created, metadata in pending
                 if _abandoned(status, created, json.loads(metadata))]
        for (number,) in stale[:max(0, MAX_INTERRUPTION_RECOVERIES - recovered)]:
            db.execute("UPDATE attempts SET status='interrupted_unknown' WHERE operation=? AND attempt=?",
                       (value['id'], number))
            recovered += 1
        parked = db.execute('SELECT 1 FROM parked_requests WHERE operation=? AND revision=?',
                            (value['contract_id'], request_revision)).fetchone()
        if attempts - recovered >= POLICIES[value['task']].attempts or parked:
            raise ModelWorkHeldError('unchanged model operation exhausted')
        value['attempt'] = attempts + 1
        db.execute('INSERT OR REPLACE INTO operations VALUES(?,?,?,?,?)',
                   (value['id'], request_revision, attempts + 1, 0, time.time()))
        metadata = {k: value[k] for k in ('task', 'id', 'parent', 'run_id')}
        metadata.update(worker_pid=os.getpid(), provider=provider, model=model, request_revision=request_revision,
                        context_chars={'evidence': len(prompt), 'instructions': len(system or ''),
                                       'schema': len(json.dumps(schema))})
        db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?)',
                   (value['id'], attempts + 1, json.dumps(metadata), 'started', None, time.time()))
    status = 'succeeded'
    try:
        yield
    except HTTPError as error:
        status = 'http_' + str(error.code)
        if error.code in (400, 422):
            with _db() as db:
                db.execute('INSERT OR REPLACE INTO parked_requests VALUES(?,?,?)',
                           (value['contract_id'], request_revision, time.time()))
        raise
    except BaseException as error:
        status = type(error).__name__
        raise
    finally:
        # Accounting must not mask the provider outcome. The pre-dispatch claim above is strict.
        try:
            with _db() as db:
                db.execute('UPDATE attempts SET status=?,usage=? WHERE operation=? AND attempt=?',
                           (status, json.dumps(value.get('usage')), value['id'], value['attempt']))
        except (OSError, sqlite3.Error):
            pass


def note_usage(model, usage):
    value = current()
    if value:
        from usage_accounting import normalize, estimate, estimate_range, PRICING_VERSION
        counts = normalize(usage)
        value['usage'] = counts | {'estimated_token_cost': estimate(model, counts),
                                   'token_cost_range': estimate_range(model, counts),
                                   'pricing_version': PRICING_VERSION, 'provider_fee_status': 'not_included'}


def generation_config(model):
    value = current()
    if not value:
        return {}
    policy = POLICIES[value['task']]
    config = {'maxOutputTokens': policy.output}
    if policy.thinking and model in LOW_THINKING_MODELS:
        config['thinkingConfig'] = {'thinkingLevel': policy.thinking}
    return config


def headers():
    value = current()
    if not value:
        return {}
    return {'X-Sotto-Workload': value['task'], 'X-Sotto-Operation': value['id'],
            'X-Sotto-Parent-Operation': value['parent'], 'X-Sotto-Run': value['run_id'],
            'X-Sotto-Attempt': str(value['attempt'])}


def call(task, prompt, inputs, *, system=None, schema=None, llm=None, contract_revision=None):
    import gemini
    from gemini_transport import effective_compose_model
    with scope(task, [prompt, system, schema, effective_compose_model(), contract_revision], occurrence=True):
        return (llm or gemini.call_gemini)(prompt, inputs, system=system, schema=schema)


def procedure(task):
    """Attach a durable operation to a procedure's actual input, not its queue retry number."""
    from functools import wraps
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            evidence = [args[0] if args else kwargs.get('inputs'),
                        {k: v for k, v in kwargs.items() if k not in ('llm', 'inputs', 'now')}]
            from gemini_transport import effective_compose_model
            contract = fn.__globals__.get('request_revision')
            contract = contract() if callable(contract) else revision(Path(fn.__code__.co_filename).read_text())
            with scope(task, [evidence, effective_compose_model(), contract], occurrence=True):
                return fn(*args, **kwargs)
        return run
    return decorate


@contextmanager
def brief_stage(name):
    """Each known brief stage owns its retry ladder; extraction cannot consume the critic's calls."""
    parent = current()
    if parent and parent['task'] == 'brief':
        with scope('brief_' + name, parent['contract_id'], occurrence=parent['id']):
            yield
    else:
        yield


def artifact_lock_path(root, identity):
    return str(Path(root) / f'lock-{int(revision(identity), 16) % ARTIFACT_LOCK_SHARDS:02x}')
