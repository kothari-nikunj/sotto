"""Read-only Google source lineage for typed owner reads; never issues OAuth consent.

A token proves neither a successful provider read nor a managed source grant. Managed
read access additionally requires a current tenant ledger and an applied broker
bootstrap that minted this token. Legacy Cloud tokens have no such lineage and
remain unavailable to the new typed surface until an explicit modern reconnect.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote

_TOKEN_LIMIT = 64 * 1024
_GENERATION = re.compile(r'[a-f0-9]{32}\Z')
_REQUEST_ID = re.compile(r'[a-f0-9]{64}\Z')
_GMAIL = {'https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/gmail.modify'}
_CALENDAR = {'https://www.googleapis.com/auth/calendar.readonly',
             'https://www.googleapis.com/auth/calendar',
             'https://www.googleapis.com/auth/calendar.events',
             'https://www.googleapis.com/auth/calendar.events.readonly'}
_CONTACTS = {'https://www.googleapis.com/auth/contacts.readonly',
             'https://www.googleapis.com/auth/contacts'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate token field')
        result[key] = value
    return result


def _invalid_number(_value):
    raise ValueError('invalid token number')


def _token(path):
    """Return a bounded metadata-only snapshot, disconnected, or unknown."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= _TOKEN_LIMIT
                or before.st_mode & 0o077 or before.st_uid != os.geteuid()):
            return None
        chunks = []
        remaining = _TOKEN_LIMIT + 1
        while remaining:
            piece = os.read(fd, min(8192, remaining))
            if not piece:
                break
            chunks.append(piece)
            remaining -= len(piece)
        raw = b''.join(chunks)
        after = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        if (len(raw) > _TOKEN_LIMIT or len(raw) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)):
            return None
        record = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                            parse_constant=_invalid_number)
        if (not isinstance(record, dict) or record.get('token_uri') != 'https://oauth2.googleapis.com/token'
                or any(not isinstance(record.get(key), str) or not record[key]
                       for key in ('refresh_token', 'client_id', 'client_secret'))
                or not isinstance(record.get('scopes'), list)
                or any(not isinstance(scope, str) for scope in record['scopes'])
                or len(record['scopes']) > 16):
            return None
        generation = record.get('sotto_source_generation')
        if not isinstance(generation, str) or _GENERATION.fullmatch(generation) is None:
            return None
        request = record.get('sotto_cloud_request_id')
        if request is not None and (not isinstance(request, str)
                                    or _REQUEST_ID.fullmatch(request) is None):
            return None
        sources = sorted(source for source, accepted in (
            ('gmail', _GMAIL), ('calendar', _CALENDAR), ('google_contacts', _CONTACTS))
            if accepted.intersection(record['scopes']))
        # The body hash detects replacement during a provider read without exposing tokens.
        return {'generation': generation, 'request_id': request,
                'sources': sources, 'body_digest': hashlib.sha256(raw).hexdigest(),
                # Access token and expiry rotate within the same grant. Keep every
                # other credential/lineage field in the snapshot replacement fence.
                'grant_digest': hashlib.sha256(json.dumps(
                    {key: value for key, value in record.items() if key not in ('token', 'expiry')},
                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    except FileNotFoundError:
        return {'generation': 'disconnected', 'request_id': None,
                'sources': [], 'body_digest': 'disconnected'}
    except (OSError, UnicodeError, TypeError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _applied_cloud_origin(data_root, tenant, token):
    if not token['request_id'] or token['generation'] == 'disconnected':
        return False
    path = Path(data_root) / 'config/cloud-pairing.sqlite'
    try:
        metadata = path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077):
            return False
        with sqlite3.connect('file:' + quote(str(path)) + '?mode=ro', uri=True, timeout=1) as db:
            row = db.execute('SELECT generation, applied FROM bootstraps WHERE id=?',
                             (token['request_id'],)).fetchone()
            newest_applied = db.execute('SELECT MAX(generation) FROM bootstraps WHERE applied=1').fetchone()
        if not row or type(row[0]) is not int or row[0] <= 0 or row[1] != 1:
            return False
        if not newest_applied or newest_applied[0] != row[0]:
            return False
        stamp = ('sotto-cloud-google-v1\0' + tenant + '\0' +
                 token['request_id'] + '\0' + str(row[0]))
        return hashlib.sha256(stamp.encode()).hexdigest()[:32] == token['generation']
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return False


def state(data_root, token_path, *, managed_mode, tenant='', managed_ledger=None):
    """Return stable granted sources and token lineage, or None when uncertain.

    Missing token is a known disconnect. It grants nothing, while still allowing
    owner-only chat and an authority revision that changes on a future reconnect.
    """
    token = _token(token_path)
    if token is None:
        return None
    if token['generation'] == 'disconnected':
        return {'sources': [], 'generation': 'disconnected', 'body_digest': 'disconnected'}
    if managed_mode:
        if (not isinstance(tenant, str) or not tenant or not isinstance(managed_ledger, dict)
                or not isinstance(managed_ledger.get('google_sources'), list)
                or not _applied_cloud_origin(data_root, tenant, token)):
            return None
        sources = sorted(set(token['sources']) & set(managed_ledger['google_sources']))
    elif token['request_id'] is not None:
        # A Cloud-bound token cannot silently become self-host consent.
        return None
    else:
        sources = token['sources']
    return {'sources': sources, 'generation': token['generation'],
            'body_digest': token['body_digest'], 'grant_digest': token['grant_digest']}
