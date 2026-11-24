"""The brief's read window: 24 hours, reaching back to the last successful read after a missed run.

One rule: each source is read from its last successful brief read when that is older than 24 hours,
never further back than CATCH_UP_MAX_HOURS. Stamps are per source ('gmail' and each Mac source) in
`cache/brief-window.json` and advance only when that source was read successfully; a missing or
unreadable file means the ordinary 24-hour window.
"""
import math
import os
from datetime import datetime, timezone

import jsonstore

BRIEF_WINDOW_HOURS = 24
CATCH_UP_MAX_HOURS = 72


def _path():
    return os.path.join(os.environ.get('SOTTO_DATA', '/data'), 'cache', 'brief-window.json')


def _stamps():
    try:
        state = jsonstore.read(_path(), default={})
    except jsonstore.Unreadable:
        return {}
    return state if isinstance(state, dict) else {}


def hours(source):
    """Hours to read for `source` ('gmail' or a Mac source such as 'imessage') in this brief."""
    try:
        stamp = datetime.fromisoformat(_stamps().get(source))
        gap = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600
    except (ValueError, TypeError, AttributeError, jsonstore.Unreadable):
        return BRIEF_WINDOW_HOURS
    return min(max(BRIEF_WINDOW_HOURS, math.ceil(gap)), CATCH_UP_MAX_HOURS)


def local_hours():
    """One Bridge read covers every Mac source, so it reaches back for the one read longest ago."""
    return max([hours(source) for source in _stamps() if source != 'gmail'] or [BRIEF_WINDOW_HOURS])


def days(source):
    return math.ceil(hours(source) / 24)


def record(sources, at):
    """Mark `sources` as read through `at` (the run's start), after the brief composed."""
    if sources:
        with jsonstore.transaction(_path(), default={}) as state:
            state.update({source: at for source in sources})
