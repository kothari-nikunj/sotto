"""Read-only compatibility for bounded receipts in the retired retraction ledger.

Only positively observed native revisions may invalidate exact version-bound evidence.
Automatic admission and projection workers are retired; legacy ledgers are never authority.
Retained files are never rewritten or deleted by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from datetime import datetime

ENABLED = False
MAX_RECEIPTS = 256
MAX_BYTES = 512 * 1024
REF = re.compile(r'msg:v2:(imessage|whatsapp):[a-f0-9]{64}')
HEX = re.compile(r'[a-f0-9]{64}')


def enabled():
    return ENABLED


def _path(data_dir=None):
    return Path(data_dir or os.environ.get('SOTTO_DATA', '/data')) / 'knowledge/source-retractions.json'


def validate(candidate):
    if not isinstance(candidate, dict) or set(candidate) != {
            'source_ref', 'previous_content_version', 'content_version',
            'source_revision_evidence', 'store_generation'}:
        raise ValueError('invalid source correction')
    ref = candidate['source_ref']
    if not isinstance(ref, str) or not REF.fullmatch(ref) or not ref.startswith('msg:v2:imessage:'):
        raise ValueError('unsupported correction source')
    for field in ('previous_content_version', 'content_version', 'store_generation'):
        if not isinstance(candidate[field], str) or not HEX.fullmatch(candidate[field]):
            raise ValueError('invalid correction version')
    if candidate['previous_content_version'] == candidate['content_version']:
        raise ValueError('unchanged correction version')
    evidence = candidate['source_revision_evidence']
    if (not isinstance(evidence, dict) or set(evidence) != {'kind', 'marker', 'value'}
            or (evidence.get('kind'), evidence.get('marker')) not in {
                ('edit', 'date_edited'), ('unsend', 'date_retracted')}
            or not isinstance(evidence.get('value'), str)
            or not re.fullmatch(r'[1-9][0-9]{0,18}', evidence['value'])
            or int(evidence['value']) > 2**63 - 1):
        raise ValueError('unsupported positive revision evidence')
    return json.loads(json.dumps(candidate))


def evidence_ref(candidate):
    return candidate['source_ref'] + ':v:' + candidate['previous_content_version']


def _identity(candidate):
    return hashlib.sha256(json.dumps(candidate, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _load(data_dir=None):
    path = _path(data_dir)
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {'schema_version': 2, 'receipts': {}}
    if len(raw) > MAX_BYTES:
        raise ValueError('source correction state too large')
    value = json.loads(raw)
    # Explicit valid legacy shapes remain inert. Malformed active state fails closed.
    if isinstance(value, list) or (isinstance(value, dict) and value.get('schema_version') == 1):
        return {'schema_version': 2, 'receipts': {}}
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'receipts'}
            or value['schema_version'] != 2 or not isinstance(value['receipts'], dict)
            or len(value['receipts']) > MAX_RECEIPTS):
        raise ValueError('invalid source correction state')
    for key, row in value['receipts'].items():
        if (not isinstance(row, dict) or set(row) not in ({'evidence', 'status', 'after', 'accepted_at', 'unreadable'},
                                                       {'evidence', 'status', 'after', 'accepted_at', 'unreadable', 'unsupported'})
                or row['status'] not in ('pending', 'applied', 'partial', 'held') or not isinstance(row['after'], str)
                or len(row['after']) > 256 or '/' in row['after'] or '\\' in row['after']
                or not isinstance(row['accepted_at'], str)
                or type(row['unreadable']) is not int or not 0 <= row['unreadable'] <= 10**9):
            raise ValueError('invalid source correction receipt')
        if type(row.get('unsupported', 0)) is not int or not 0 <= row.get('unsupported', 0) <= 10**9:
            raise ValueError('invalid unsupported count')
        candidate = validate(row['evidence'])
        if key != _identity(candidate):
            raise ValueError('source correction identity mismatch')
        if datetime.fromisoformat(row['accepted_at']).tzinfo is None:
            raise ValueError('invalid correction acceptance time')
    return value


def invalidated_refs(data_dir=None):
    if not enabled():
        return set()
    return {evidence_ref(row['evidence']) for row in _load(data_dir)['receipts'].values()}


def revision(data_dir=None):
    """Fence already-materialized context when a new correction is accepted."""
    if not enabled():
        return ''
    keys = sorted(_load(data_dir)['receipts'])
    return hashlib.sha256(json.dumps(keys, separators=(',', ':')).encode()).hexdigest()
