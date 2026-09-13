"""The authenticated Bridge read seam used by background gather and first-day seeding."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import urllib.request
from datetime import datetime, timezone

from textutil import unwrap_tool_result
import jsonstore
from source_catalog import SOURCE_FIELDS


SOURCE_STATUSES = frozenset({'ok', 'partial', 'unavailable', 'disabled', 'skipped'})
CONTEXT_SOURCES = tuple(SOURCE_FIELDS) + ('gmail', 'calendar', 'granola')


def permission_fingerprint():
    """Stable work identity: a source toggle starts a new brief instead of reusing old text."""
    permissions = {source: allowed(source) for source in CONTEXT_SOURCES}
    return hashlib.sha256(json.dumps(permissions, sort_keys=True).encode()).hexdigest()[:24]


def used_sources(local=None, gmail=None, calendar=None, granola=None):
    """Minimal source IDs for a delivery-time permission check; never copies source content."""
    local = project_local(local or {})
    sources = {source for source, fields in SOURCE_FIELDS.items()
               if any(local.get(field) for field in fields)}
    for source, rows, key in (('gmail', gmail, 'emails'), ('calendar', calendar, 'events'),
                              ('granola', granola, 'meetings')):
        if isinstance(rows, dict):
            rows = (rows.get(key) or rows.get('stale_threads') or rows.get('staleThreads') or [])
        if rows and allowed(source):
            sources.add(source)
    return sorted(sources)


def source_result(status, *, observed_at=None, complete=False, since=None, until=None, error=None):
    """Metadata for one observation; legacy data arrays can travel beside this receipt."""
    if status not in SOURCE_STATUSES:
        raise ValueError('invalid source status')
    result = {'status': status, 'observed_at': observed_at or datetime.now(timezone.utc).isoformat(),
              'complete': status == 'ok' and complete is True,
              'coverage': {'since': since, 'until': until}}
    if error:
        result['error'] = str(error)[:80]  # callers pass exception classes, never provider bodies
    return result


def _state_path(data_root=None):
    return str(Path(data_root or os.environ.get('SOTTO_DATA', '/data')) / 'config/source-state.json')


def record_bridge_status(payload, data_root=None):
    """Remember authenticated Bridge read/health metadata, including self-host source revocation.

    A failed read is availability, not revoked permission. Never store message content here.
    Older in-flight reads cannot overwrite a newer source-toggle observation.
    """
    if not isinstance(payload, dict):
        return
    statuses = payload.get('source_status', payload.get('sources'))
    if not isinstance(statuses, dict):
        return
    observed = payload.get('generated_at') or datetime.now(timezone.utc).isoformat()
    try:
        stamp = datetime.fromisoformat(observed.replace('Z', '+00:00'))
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    except (ValueError, TypeError, AttributeError):
        return
    values = {s: v for s, v in statuses.items() if s in SOURCE_FIELDS
              and v in ('ok', 'disabled', 'unavailable', 'degraded', 'needs_fda', 'partial')}
    if not values:
        return
    with jsonstore.transaction(_state_path(data_root), default={}) as state:
        sources = state.setdefault('sources', {})
        for source, status in values.items():
            previous = sources.get(source, {})
            try:
                previous_epoch = float(previous.get('observed_epoch', 0))
            except (ValueError, TypeError, AttributeError):
                previous_epoch = 0
            if previous_epoch > stamp.timestamp():
                continue
            sources[source] = {'status': status, 'observed_at': stamp.isoformat(),
                               'observed_epoch': stamp.timestamp()}


def project_local(local):
    """Apply current permission to both live and cached payloads before any consumer sees them."""
    local = local if isinstance(local, dict) else {}
    result = dict(local)
    statuses = dict(result.get('source_status') or {})
    availability = dict(result.get('_source_availability') or {})
    for source, fields in SOURCE_FIELDS.items():
        if statuses.get(source) == 'disabled' or not allowed(source):
            for field in fields:
                result.pop(field, None)
            # Do not create noise about sources that have never been configured.
            if source in statuses or any(field in local for field in fields):
                statuses[source] = 'disabled'
                availability[source] = 'disabled'
    if statuses:
        result['source_status'] = statuses
    if availability:
        result['_source_availability'] = availability
    return result


def allowed(source):
    if os.environ.get('SOTTO_DEPLOYMENT_MODE') != 'managed':
        if source in SOURCE_FIELDS:
            try:
                state = jsonstore.read(_state_path(), default={}, strict=True)
                row = state.get('sources', {}).get(source, {})
                if row:
                    return row.get('status') != 'disabled'
                # Upgrade compatibility before the first fresh Bridge status arrives.
                snapshot = jsonstore.read(str(Path(os.environ.get('SOTTO_DATA', '/data')) /
                                              'knowledge/last_local_snapshot.json'), default={})
                return snapshot.get('local', {}).get('source_status', {}).get(source) != 'disabled'
            except (OSError, ValueError, AttributeError, TypeError, jsonstore.Unreadable):
                return False
        return True
    try:
        path = Path(os.environ.get('SOTTO_DATA', '/data')) / 'config/managed-capabilities.json'
        value = json.loads(path.read_text())
        row = value['sources'][source]
        return (value.get('tenant_id') == os.environ.get('SOTTO_TENANT_ID')
                and row.get('consented') is True and row.get('connected') is True)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def bridge_call(name, arguments=None):
    key = os.environ.get('BRIDGE_TOKEN') or os.environ.get('SOTTO_MCP_TOKEN') or os.environ.get('SOTTO_TRIGGER_TOKEN')
    if not key:
        raise RuntimeError('Bridge is not configured')
    token = hmac.new(key.encode(), b'sotto-mcp', hashlib.sha256).hexdigest()
    rpc = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
           'params': {'name': name, 'arguments': arguments or {}}}
    req = urllib.request.Request('http://127.0.0.1:' + os.environ.get('PORT', os.environ.get('SOTTO_TRIGGER_PORT', '8787')) + '/mcp',
        data=json.dumps(rpc).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=100) as response:
        reply = json.load(response)
    result = reply.get('result', {})
    if reply.get('error') or result.get('isError'):
        raise RuntimeError('Bridge read unavailable')
    payload = unwrap_tool_result(result)
    if name in ('read_local', 'health'):
        record_bridge_status(payload)
    return payload


def read_local(hours=24):
    try:
        return project_local(bridge_call('read_local', {'since_hours': hours}))
    except (OSError, ValueError, RuntimeError):
        return {}  # The brief owns the last-snapshot fallback and freshness disclosure.


def history_page(source, since, until, before=0, limit=500):
    if not allowed(source):
        return {'source': source, 'status': 'disabled', 'rows': [], 'complete': False}
    result = bridge_call('read_history', {'source': source, 'since': since, 'until': until,
                                        'before': before, 'limit': limit})
    if not allowed(source):
        raise RuntimeError('source consent changed during history read')
    if (not isinstance(result, dict) or result.get('source') != source
            or result.get('status') != 'ok' or not isinstance(result.get('rows'), list)):
        raise RuntimeError('history requires a connected source and an updated Bridge')
    cursor = result.get('next_cursor')
    if result.get('complete') is not True and (not isinstance(cursor, int) or isinstance(cursor, bool)
                                              or cursor <= 0 or (before and cursor >= before)):
        raise ValueError('history cursor did not advance')
    return result
