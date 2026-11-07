"""Read source-generation evidence from the existing self-host credential records.

This module does not grant access or read source content. A present legacy or malformed
credential is unknown, never an invitation to stamp old context with a fresh revision.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

GENERATION_KEY = 'sotto_source_generation'
_GENERATION = re.compile(r'\A[a-f0-9]{32}\Z')
_MAX_CREDENTIAL_BYTES = 64 * 1024
DISCONNECTED = 'disconnected'


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate credential field')
        value[key] = item
    return value


def _nonfinite(value):
    raise ValueError('nonfinite credential field')


def _generation(path: str | Path, *, service: str) -> str | None:
    """Return opaque generation, disconnected sentinel, or unknown (None)."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_CREDENTIAL_BYTES
                or metadata.st_size == 0 or metadata.st_mode & 0o077
                or metadata.st_uid not in (os.geteuid(), 0)):
            return None
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            raw = stream.read(_MAX_CREDENTIAL_BYTES + 1)
        if len(raw) > _MAX_CREDENTIAL_BYTES:
            return None
        row = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                         parse_constant=_nonfinite)
        if not isinstance(row, dict):
            return None
        generation = row.get(GENERATION_KEY)
        if not isinstance(generation, str) or _GENERATION.fullmatch(generation) is None:
            return None
        if service == 'google':
            if not isinstance(row.get('refresh_token'), str) or not row['refresh_token']:
                return None
        elif service == 'granola':
            if (row.get('service') != 'granola' or not isinstance(row.get('access_token'), str)
                    or not row['access_token']):
                return None
        else:
            return None
        return generation
    except FileNotFoundError:
        return DISCONNECTED
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def generations(google_token: str | Path, granola_token: str | Path) -> tuple[str, str] | None:
    """Both sources must be known; a missing token is a known disconnected state."""
    google = _generation(google_token, service='google')
    granola = _generation(granola_token, service='granola')
    return (google, granola) if google is not None and granola is not None else None


def granola_generation(path: str | Path) -> str | None:
    """Independent protected connector evidence; no Google credential dependency."""
    return _generation(path, service='granola')
