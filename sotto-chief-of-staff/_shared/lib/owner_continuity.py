"""Bounded owner-only projections; receiver routes and model tools are separate policy layers."""
from __future__ import annotations

from datetime import datetime
import json

import ledger_io
from ledger_provenance import project_entries
from protected_ledger_io import ProtectedLedgerUnavailableError, load_protected_entries

MAX_RESPONSE_BYTES = 64 * 1024


def read_owner_continuity(data_root: str, *, now: datetime, view: str = 'loops',
                          include_parked: bool = False) -> dict:
    """Read only fresh owner-origin rows, explicitly labeling partial coverage.

    The caller owns the data root and clock; neither is read from process configuration.
    Legacy and source-backed rows stay withheld even when a source is currently granted.
    This callable grants no source access and does not itself expose a route or tool.
    """
    if (not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
            or view not in ('loops', 'retune') or type(include_parked) is not bool
            or (view == 'retune' and include_parked)):
        raise ProtectedLedgerUnavailableError('invalid protected read request')
    try:
        # Provenance projection precedes status selection, rendering and counts.
        rows = project_entries(load_protected_entries(data_root), lambda _source: False)
        active = ledger_io.active_entries(rows, now)
        if view == 'loops':
            from loops_query import query_owner_rows
            result = query_owner_rows(active, parked_rows=rows if include_parked else None, now=now)
        else:
            from retune_scan import scan_owner_rows
            result = scan_owner_rows(active, now=now)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise ProtectedLedgerUnavailableError('protected read exceeds its response bound')
        return result
    except ProtectedLedgerUnavailableError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        raise ProtectedLedgerUnavailableError('protected read is unavailable') from None
