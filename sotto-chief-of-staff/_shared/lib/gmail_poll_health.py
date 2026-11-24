"""Repeated Gmail poll failures, as one receipt row the brief's source note reads.

The receiver's poll loop marks the row after GMAIL_FAIL_ALERT_AFTER consecutive failures and clears
it on the next success. It lives in `config/source-state.json` beside the other source receipts and
never stores the error itself.
"""
from datetime import datetime, timezone

import jsonstore
from source_context import _metadata_writer, _read_source_state, _source_rows, _source_transaction, _state_path


@_metadata_writer
def record(paused):
    with _source_transaction(_state_path()) as state:
        sources = _source_rows(state)
        if not paused:
            sources.pop('gmail_poll', None)
        elif 'gmail_poll' not in sources:
            sources['gmail_poll'] = {'status': 'paused', 'since': datetime.now(timezone.utc).isoformat()}


def paused_since():
    """When Gmail polling paused (ISO time), or None while it is working or unknown."""
    try:
        row = _source_rows(_read_source_state(_state_path())).get('gmail_poll', {})
    except (OSError, ValueError, TypeError, AttributeError, jsonstore.Unreadable):
        return None
    return row.get('since') if row.get('status') == 'paused' else None
