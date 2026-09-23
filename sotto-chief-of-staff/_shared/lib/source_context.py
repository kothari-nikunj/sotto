"""The authenticated Bridge read seam used by background gather and first-day seeding."""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import math
import os
import sys
from contextlib import contextmanager
from pathlib import Path
import urllib.request
from datetime import datetime, timezone

from textutil import unwrap_tool_result
import jsonstore
from source_catalog import SOURCE_FIELDS, SOURCE_LABELS, LOCAL_SNAPSHOT_TTL_HOURS


SOURCE_STATUSES = frozenset({'ok', 'partial', 'unavailable', 'disabled', 'skipped'})
BRIDGE_STATUSES = frozenset({'ok', 'disabled', 'unavailable', 'degraded', 'needs_fda', 'partial'})
CONTEXT_SOURCES = tuple(SOURCE_FIELDS) + ('gmail', 'calendar', 'granola', 'x', 'x_bookmarks')


class HistoryProtocolError(ValueError):
    """An authenticated history response violates the paging contract."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class HistoryContinuationError(HistoryProtocolError):
    """Only the continuation token this window is holding is dead; the window itself is valid.

    Carries a code and nothing else: a provider reason phrase must never reach a state file.
    """

    def __init__(self, code='expired_continuation_token'):
        super().__init__(code)


def permission_fingerprint():
    """Stable work identity: a source toggle starts a new brief instead of reusing old text."""
    permissions = {source: allowed(source) for source in CONTEXT_SOURCES}
    return hashlib.sha256(json.dumps(permissions, sort_keys=True).encode()).hexdigest()[:24]


def used_sources(local=None, gmail=None, calendar=None, granola=None, x_context=None):
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
    x_rows = project_x(x_context).get('attendees', [])
    if x_rows:
        sources.add('x')
        if any(row.get('bookmarks') for row in x_rows):
            sources.add('x_bookmarks')
    return sorted(sources)


def project_x(payload):
    """Prepared X material follows the owner's current credential opt-in, including bookmarks."""
    if not allowed('x'):
        return {'attendees': [], 'connected': False}
    result = dict(payload) if isinstance(payload, dict) else {'attendees': payload or []}
    rows = result.get('attendees')
    bookmarks_allowed = allowed('x_bookmarks')
    result['attendees'] = [{**row, 'bookmarks': row.get('bookmarks', []) if bookmarks_allowed else []}
                           for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return result


def x_availability(payload):
    """A failed optional endpoint must not tell the writer to ignore successful X reads."""
    result = project_x(payload)
    if result.get('warnings'):
        status = result.get('request_status', {})
        return 'partial' if isinstance(status, dict) and status.get('succeeded') is True else 'unavailable'
    return None


def _metadata_writer(func):
    """A corrupt consent receipt is refused, never repaired and never fatal: the file stays as it
    is, `allowed()` fails closed on it, and the brief still composes without local sources."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except jsonstore.Unreadable as error:
            print(f'[sotto] source receipt left untouched: {error}', file=sys.stderr, flush=True)
    return wrapper


def consent_receipt_readable(data_root=None):
    """Whether self-host consent can be read at all; managed mode keeps consent elsewhere."""
    if os.environ.get('SOTTO_DEPLOYMENT_MODE') == 'managed':
        return True
    try:
        _read_source_state(_state_path(data_root))
    except (OSError, ValueError, TypeError, AttributeError, jsonstore.Unreadable):
        return False
    return True


@_metadata_writer
def record_x_status(payload):
    """Store connection evidence alongside Bridge receipts, never profiles or Posts."""
    request = payload.get('request_status', {}) if isinstance(payload, dict) else {}
    status = request.get('status')
    if status not in ('unconfigured', 'unverified', 'ok', 'degraded'):
        return
    now = datetime.now(timezone.utc)
    errors = {'authentication_failed', 'permission_denied', 'credits_exhausted',
              'rate_limited', 'service_unavailable', 'transport_unavailable', 'invalid_response'}
    with _source_transaction(_state_path()) as state:
        sources = _source_rows(state)
        old = sources.get('x', {})
        old = old if isinstance(old, dict) else {}
        # A run with no candidates makes no API request. It cannot clear a failed request
        # or advance the last success timestamp simply because credentials still exist.
        if status == 'unverified' and old.get('status') in ('ok', 'degraded'):
            return
        row = {'status': status, 'observed_at': now.isoformat(), 'observed_epoch': now.timestamp(),
               'error_codes': sorted({code for code in request.get('error_codes', [])
                                      if isinstance(code, str) and code in errors})}
        if request.get('succeeded') is True:
            row['last_success_at'] = now.isoformat()
        elif old.get('last_success_at'):
            row['last_success_at'] = old['last_success_at']
        sources['x'] = row


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


def _source_rows(state):
    """Never repair an unreadable consent receipt by silently dropping its local disables."""
    if (not isinstance(state, dict) or not isinstance(state.get('sources', {}), dict)
            or any(not isinstance(row, dict) for row in state.get('sources', {}).values())):
        raise jsonstore.Unreadable('invalid source receipt')
    for source, row in state.get('sources', {}).items():
        if source in SOURCE_FIELDS and (not isinstance(row.get('status'), str)
                                       or row['status'] not in BRIDGE_STATUSES):
            raise jsonstore.Unreadable('invalid source permission status')
    return state.setdefault('sources', {})


def _read_source_state(path):
    state = jsonstore.read(path, default=None, strict=True)
    if state is None:
        if Path(path).exists():
            raise jsonstore.Unreadable('null source receipt')
        state = {}
    _source_rows(state)
    return state


@contextmanager
def _source_transaction(path):
    # The general JSON store treats null as its default; a present null consent receipt is
    # corrupt, not a new installation. Validate it under the same shared writer lock.
    with jsonstore.lock(path):
        state = _read_source_state(path)
        yield state
        jsonstore.write_atomic(path, state)


def _local_statuses(statuses):
    """Both message readers contribute to the parent's coverage, including older Bridges."""
    statuses = statuses if isinstance(statuses, dict) else {}
    result = {s: v for s, v in statuses.items() if s in SOURCE_FIELDS and isinstance(v, str)}
    for source in ('imessage', 'whatsapp'):
        deferred = statuses.get('deferred_unread_' + source)
        if deferred in ('degraded', 'partial', 'needs_fda', 'unavailable'):
            if result.get(source) in (None, 'ok', 'partial', 'degraded'):
                result[source] = 'partial'
    return result


def bridge_request_epoch(payload):
    """Server-authored request time, retained across replay; never infer it from arrival."""
    value = payload.get('_bridge_request_started_at') if isinstance(payload, dict) else None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None
        epoch = stamp.timestamp()
        return epoch if 0 < epoch <= datetime.now(timezone.utc).timestamp() else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


@_metadata_writer
def record_bridge_status(payload, data_root=None):
    """Remember authenticated Bridge read/health metadata, including self-host source revocation.

    A failed read is availability, not revoked permission. Never store message content here.
    Consent is asymmetric: a disable reported by a probe takes effect when it arrives, whatever
    either clock says, while only a strictly later probe restores access. A completed read never
    restores it, because it may have started before the toggle and no clock can prove otherwise.
    """
    if not isinstance(payload, dict):
        return
    statuses = payload.get('source_status', payload.get('sources'))
    if not isinstance(statuses, dict):
        return
    now = datetime.now(timezone.utc)
    observed = payload.get('generated_at') or payload.get('last_seen') or now.isoformat()
    try:
        stamp = datetime.fromisoformat(observed.replace('Z', '+00:00'))
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        # Mac time remains the fallback for unsolicited wake snapshots. Relayed observations
        # also carry the server's request-start time, independent of Mac clock corrections.
        received_stamp = min(stamp, now)
    except (ValueError, TypeError, AttributeError):
        return
    values = {s: v for s, v in _local_statuses(statuses).items()
              if v in BRIDGE_STATUSES}
    if not values:
        return

    def epoch(row):
        try:
            value = float(row.get('order_epoch', row.get('observed_epoch', 0)))
            return value if math.isfinite(value) else 0
        except (ValueError, TypeError, AttributeError):
            return 0

    requested = bridge_request_epoch(payload)

    def newer(row, *, strict=False):
        prior_request = row.get('request_epoch')
        if (requested is not None and type(prior_request) in (int, float)
                and math.isfinite(prior_request) and prior_request > 0):
            return requested > prior_request if strict else requested >= prior_request
        # The first server-ordered observation can replace a legacy future watermark for
        # availability. Consent restoration still requires the existing strict probe check.
        if requested is not None and not strict:
            return True
        observed_epoch = row.get('observed_epoch')
        if (requested is not None and strict and type(observed_epoch) in (int, float)
                and math.isfinite(observed_epoch) and 0 < observed_epoch < epoch(row)):
            # A future Mac stamp was capped to arrival, so this is a proven server receipt.
            # An ordinary (possibly behind-clock) observed_epoch does not prove arrival.
            return requested > observed_epoch
        return stamp.timestamp() > epoch(row) if strict else stamp.timestamp() >= epoch(row)

    is_read = 'source_status' in payload
    with _source_transaction(_state_path(data_root)) as state:
        sources = _source_rows(state)
        for source, status in values.items():
            previous = sources.get(source, {})
            previous = previous if isinstance(previous, dict) else {}
            if status == 'disabled' and not is_read:
                # No stored watermark, from either clock, may hold a disable back.
                applied = True
            elif previous.get('status') == 'disabled' and status != 'disabled':
                # Only a strictly newer probe restores consent, never a completed read.
                applied = not is_read and newer(previous, strict=True)
            else:
                applied = newer(previous)
            current = dict(previous)
            if applied:
                current.update(status=status, observed_at=received_stamp.isoformat(),
                               observed_epoch=received_stamp.timestamp(), order_epoch=stamp.timestamp())
                current.pop('request_epoch', None)
                if requested is not None:
                    current['request_epoch'] = requested
                if status == 'ok':
                    current['last_available_at'] = received_stamp.isoformat()
                elif previous.get('status') == 'ok':
                    current.setdefault('last_available_at', previous.get('observed_at'))
            # A health probe proves access, not extraction. Keep the last real read separately
            # and order it by its own timestamp: a probe can arrive while a wake read is running.
            # A newer disable still wins; a late read cannot restore permission or activity counts.
            previous_read = previous.get('read', {})
            read_allowed = applied or (current.get('status') != 'disabled' and status != 'disabled')
            if (is_read and read_allowed
                    and newer(previous_read)):
                counts = {}
                invalid = any(field not in payload for field in SOURCE_FIELDS[source])
                for field in SOURCE_FIELDS[source]:
                    if field not in payload:
                        continue
                    value = payload[field]
                    if field == 'contacts_total':
                        if type(value) is int and value >= 0:
                            counts[field] = value
                        else:
                            invalid = True
                    elif field == 'screen_time':
                        if isinstance(value, dict) and isinstance(value.get('top_apps'), list):
                            counts[field] = len(value['top_apps'])
                        else:
                            invalid = True
                    elif isinstance(value, list):
                        counts[field] = len(value)
                    else:
                        invalid = True
                read_status = status
                if status == 'ok' and (invalid or not counts):
                    read_status = 'partial'
                current['read'] = {'status': read_status, 'observed_at': received_stamp.isoformat(),
                                   'observed_epoch': received_stamp.timestamp(), 'order_epoch': stamp.timestamp(), 'field_counts': counts,
                                   'has_data': any(counts.values()),
                                   'payload_valid': not invalid and bool(counts)}
                if requested is not None:
                    current['read']['request_epoch'] = requested
                if status == 'disabled':
                    current['read'].update(field_counts={}, has_data=False)
                elif current['read']['has_data']:
                    current['last_available_at'] = received_stamp.isoformat()
            if current:  # never save an empty row the validator would refuse
                sources[source] = current


def source_health(data_root=None, now=None):
    """Authenticated diagnostics from existing receipts; no source content or live work."""
    now = now or datetime.now(timezone.utc)
    try:
        state = _read_source_state(_state_path(data_root))
        rows = _source_rows(state)
    except (OSError, ValueError, TypeError, AttributeError, jsonstore.Unreadable):
        return {'status': 'unavailable', 'sources': []}
    result = []
    for source in SOURCE_FIELDS:
        row = rows.get(source, {})
        row = row if isinstance(row, dict) else {}
        read = row.get('read', {})
        read = read if isinstance(read, dict) else {}
        permitted = allowed(source)
        try:
            age = now.timestamp() - float(read['observed_epoch'])
            if not math.isfinite(age) or age < 0:
                age = None
        except (KeyError, TypeError, ValueError):
            age = None
        status = 'disabled' if not permitted or row.get('status') == 'disabled' else (
            'unverified' if not read else read.get('status', 'unverified'))
        if permitted and row.get('status') != 'disabled' and status == 'disabled':
            status = 'unverified'  # Re-enabled access is not a new extraction.
        # A new access failure matters immediately; a successful probe still cannot
        # erase an extraction failure or make the previous data younger.
        if status != 'disabled' and row.get('status') in ('unavailable', 'needs_fda', 'degraded', 'partial'):
            status = row['status']
        if (source in ('chrome', 'whatsapp', 'whatsapp_calls') and status == 'unavailable'
                and not row.get('last_available_at') and not read.get('has_data')):
            status = 'not_present'  # No evidence this optional source was ever available.
        if status == 'ok':
            if age is None or age > LOCAL_SNAPSHOT_TTL_HOURS * 3600:
                status = 'stale'
            elif not read.get('has_data'):
                status = 'empty'
        item = {'source': source, 'label': SOURCE_LABELS[source], 'status': status}
        if status != 'disabled':
            counts = read.get('field_counts', {})
            counts = counts if isinstance(counts, dict) else {}
            item.update(observed_at=read.get('observed_at'), age_seconds=age,
                        field_counts={key: value for key, value in counts.items()
                                      if key in SOURCE_FIELDS[source] and type(value) is int and value >= 0})
        result.append(item)
    x = rows.get('x', {})
    x = x if isinstance(x, dict) else {}
    x_status = 'unconfigured' if not allowed('x') else x.get('status', 'unverified')
    if x_status == 'unconfigured' and allowed('x'):
        x_status = 'unverified'
    result.append({'source': 'x', 'label': SOURCE_LABELS['x'], 'status': x_status,
                   'observed_at': x.get('observed_at'), 'last_success_at': x.get('last_success_at'),
                   'error_codes': x.get('error_codes', []) if x_status != 'unconfigured' else [],
                   'bookmarks_configured': allowed('x_bookmarks')})
    return {'status': 'ok', 'sources': result}


def project_local(local):
    """Apply current permission to both live and cached payloads before any consumer sees them."""
    local = local if isinstance(local, dict) else {}
    result = dict(local)
    statuses = _local_statuses(result.get('source_status'))
    availability = dict(result.get('_source_availability') or {})
    result.pop('_consent_unreadable', None)
    if not consent_receipt_readable():
        result['_consent_unreadable'] = True  # every local source below fails closed via allowed()
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
    if source in ('x', 'x_bookmarks'):
        # X is an optional operator-configured integration in both hosting modes. It has
        # no Bridge switch or cloud OAuth setup; explicitly supplied owner credentials
        # are its opt-in. Check again when cached context is consumed and delivered.
        if os.environ.get('SOTTO_X_STUB'):
            return True
        if source == 'x_bookmarks':
            return bool(os.environ.get('X_USER_ACCESS_TOKEN', '').strip()
                        and os.environ.get('X_OWNER_USER_ID', '').strip())
        return bool(os.environ.get('X_BEARER_TOKEN', '').strip()
                    or os.environ.get('X_USER_ACCESS_TOKEN', '').strip())
    if os.environ.get('SOTTO_DEPLOYMENT_MODE') != 'managed':
        if source in SOURCE_FIELDS:
            try:
                state = _read_source_state(_state_path())
                row = _source_rows(state).get(source, {})
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
    except (OSError, ValueError, RuntimeError, jsonstore.Unreadable):
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
        raise HistoryProtocolError('invalid_history_page')
    cursor = result.get('next_cursor')
    if result.get('complete') is not True and (not isinstance(cursor, int) or isinstance(cursor, bool)
                                              or cursor <= 0 or (before and cursor >= before)):
        raise HistoryProtocolError('history_cursor_did_not_advance')
    return result
