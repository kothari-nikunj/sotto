"""Bounded, content-free owner audit for authenticated MCP tool calls."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import html
import json
import os
import re
import stat
import threading

from client_mcp_catalog import MAC_SOURCES, TOOLS

DAYS = 30
DAY_BYTES = 1024 * 1024
ROW_LIMIT = 100
SOURCE_IDS = {*MAC_SOURCES, 'gmail', 'calendar', 'google_contacts', 'granola', 'saved_knowledge'}
FINGERPRINT = re.compile(r'[0-9a-f]{12}\Z')
READ_STATUSES = frozenset({'ok', 'complete', 'empty', 'partial', 'unavailable', 'missing', 'ambiguous', 'changed',
                           'disabled', 'not_found', 'locked', 'degraded', 'not_configured', 'needs_fda', 'failed', 'unread'})
OUTCOMES = {'ok', 'unavailable', 'invalid_request', 'notification', 'rejected', 'busy', 'internal_error', 'unauthorized'}
_TOUCHED = ContextVar('mcp_audit_touched', default=None)
_WRITE_LOCK = threading.Lock()
STAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00\Z')


@contextmanager
def capture_sources():
    touched = set()
    token = _TOUCHED.set(touched)
    try:
        yield touched
    finally:
        _TOUCHED.reset(token)


def touch(*sources):
    touched = _TOUCHED.get()
    if touched is not None:
        touched.update(source for source in sources if source in SOURCE_IDS)


@contextmanager
def _open_day(root, day, flags):
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if flags & os.O_CREAT:
            try:
                os.mkdir('logs', 0o700, dir_fd=directory)
            except FileExistsError:
                pass
        logs = os.open('logs', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            fd = os.open(f'mcp-access-{day}.jsonl', flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=logs)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_uid != os.geteuid():
                    raise OSError('unsafe MCP audit file')
                yield fd, info.st_size
            finally:
                os.close(fd)
        finally:
            os.close(logs)
    finally:
        os.close(directory)


def read_status(value, tool):
    """Summarize only producer-owned status fields; never inspect a returned source row."""
    if not isinstance(value, dict):
        return None
    if tool == 'sotto_contacts':
        statuses = [read_status(value.get(key), None) for key in ('mac_contacts', 'google_contacts')]
    else:
        source_status = value.get('source_status')
        statuses = list(source_status.values()) if isinstance(source_status, dict) else []
        coverage = value.get('coverage') if tool == 'sotto_search_local' else value.get('source_coverage')
        if isinstance(coverage, dict):
            statuses.extend(row.get('status') for row in coverage.values() if isinstance(row, dict))
        observation = value.get('observation')
        if isinstance(observation, dict):
            statuses.append(observation.get('status'))
        if value.get('not_permitted'):
            statuses.append('disabled')
        if not statuses or value.get('status') not in (None, 'ok'):
            statuses.append(value.get('status'))
    known = {status for status in statuses if isinstance(status, str) and status in READ_STATUSES}
    if len(known) == 1:
        return known.pop()
    if known:
        return 'ok' if known <= {'ok', 'complete', 'empty'} else 'partial'
    return None


def append(root, audit):
    if not audit.get('client') or not audit.get('tool'):
        return
    stamp = datetime.fromtimestamp(audit['at'], timezone.utc).isoformat(timespec='seconds')
    row = {key: audit[key] for key in ('client', 'tool', 'outcome', 'bytes', 'sources')}
    if audit.get('read_status') in READ_STATUSES:
        row['read_status'] = audit['read_status']
    row['ts'] = stamp
    encoded = json.dumps(row, separators=(',', ':')).encode() + b'\n'
    with _WRITE_LOCK, _open_day(root, stamp[:10], os.O_WRONLY | os.O_APPEND | os.O_CREAT) as (fd, size):
        if size + len(encoded) <= DAY_BYTES:
            os.write(fd, encoded)


def _valid(row, day):
    return (type(row) is dict and set(row) - {'read_status'} == {'ts', 'client', 'tool', 'sources', 'outcome', 'bytes'}
            and ('read_status' not in row or type(row['read_status']) is str and row['read_status'] in READ_STATUSES)
            and type(row['ts']) is str and row['ts'].startswith(day) and STAMP.fullmatch(row['ts'])
            and type(row['client']) is str and FINGERPRINT.fullmatch(row['client'])
            and type(row['tool']) is str and row['tool'] in TOOLS
            and type(row['outcome']) is str and row['outcome'] in OUTCOMES
            and type(row['bytes']) is int and 0 <= row['bytes'] <= 1024 * 1024
            and type(row['sources']) is list and len(row['sources']) <= len(SOURCE_IDS)
            and all(type(source) is str and source in SOURCE_IDS for source in row['sources']))


def recent(root, now):
    rows = []
    today = datetime.fromtimestamp(now, timezone.utc).date()
    for offset in range(DAYS):
        day = (today - timedelta(days=offset)).isoformat()
        try:
            with _open_day(root, day, os.O_RDONLY) as (fd, size):
                # One bounded read. Ignore a trailing line from a writer still in progress.
                os.lseek(fd, max(0, size - 64 * 1024), os.SEEK_SET)
                lines = os.read(fd, 64 * 1024).split(b'\n')[:-1]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                continue
            if _valid(row, day):
                rows.append(row)
                if len(rows) == ROW_LIMIT:
                    return rows
    return rows


def markup(root, now):
    rows = recent(root, now)
    text = '<h2>Recent assistant reads</h2>'
    if not rows:
        return text + '<p>No reads available in the retained log.</p>'
    cells = ''.join('<tr>' + ''.join(f'<td>{html.escape(str(value))}</td>' for value in (
        row['ts'], row['client'], row['tool'], ', '.join(row['sources']), row['outcome'], row.get('read_status', '—'), row['bytes']))
        + '</tr>' for row in rows)
    return (text + '<p>Last 100 authenticated tool calls in the past 30 days. Sources are attempts; '
            'bytes describe the returned structured result.</p><table><thead><tr><th>Time (UTC)</th>'
            '<th>Connection</th><th>Tool</th><th>Sources</th><th>Call outcome</th><th>Read status</th><th>Bytes</th>'
            '</tr></thead><tbody>' + cells + '</tbody></table>')
