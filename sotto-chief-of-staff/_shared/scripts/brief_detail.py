#!/usr/bin/env python3
"""Retrieve retained, accepted photo-brief text for the configured owner. Read-only, no model."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from source_context import permission_fingerprint  # noqa: E402
import tzchain  # noqa: E402

RETENTION_DAYS = 7
PAGE_CHARS = 12000
MAX_MATCHES = 20


def _read(path):
    try:
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def query(*, artifact='', day='', kind='', text='', offset=0, data_root=None, now=None):
    root = Path(data_root or os.environ.get('SOTTO_DATA', '/data'))
    clock = time.time() if now is None else now
    home = os.environ.get('PHOTON_HOME_CHANNEL', '')
    if not home:
        return {'status': 'unavailable', 'reason': 'The owner photo channel is not configured.'}
    if (artifact and not re.fullmatch(r'[a-f0-9]{24}', artifact)) or offset < 0:
        return {'status': 'invalid_selector'}
    if day:
        try:
            datetime.strptime(day, '%Y-%m-%d')
        except ValueError:
            return {'status': 'invalid_selector'}
    owner_hashes = {hashlib.sha256(t.encode()).hexdigest() for t in ('photon', 'photon:' + home)}
    accepted = {}
    rows = _read(root / 'events/outbox.json').get('rows')
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get('status') != 'delivered':
            continue
        receipt = row.get('receipt') or {}
        if (isinstance(receipt, dict) and receipt.get('target_hash') in owner_hashes
                and receipt.get('message_id') and isinstance(receipt.get('visual_artifact_id'), str)
                and type(receipt.get('accepted_at')) in (int, float)):
            accepted[receipt.get('visual_artifact_id')] = receipt.get('accepted_at')
    matches = []
    withheld = False
    zone = tzchain.resolve(tzchain.configured_tz_name(str(root))) or timezone.utc
    fingerprint = permission_fingerprint()
    for path in (root / 'cache/visual-briefs').glob('*/manifest.json'):
        if path.parent.is_symlink() or not re.fullmatch(r'[a-f0-9]{24}', path.parent.name):
            continue
        if artifact and path.parent.name != artifact:
            continue
        manifest = _read(path)
        # A bare `photon` receipt is a default-channel alias, not enduring recipient evidence.
        # Bind the retained text to the owner at render time as well as the accepted target.
        if manifest.get('owner_channel_hash') != hashlib.sha256(home.encode()).hexdigest():
            continue
        if manifest.get('preview') or manifest.get('kind') not in ('brief', 'prep'):
            continue
        if kind and manifest.get('kind') != kind:
            continue
        stamp = accepted.get(path.parent.name)
        for receipt_path in path.parent.glob('delivery-*.json'):
            r = _read(receipt_path)
            if r.get('acceptance') == 'accepted' and r.get('target_hash') in owner_hashes and r.get('message_id'):
                candidate = r.get('recorded_at')
                if type(candidate) in (int, float) and (stamp is None or candidate > stamp):
                    stamp = candidate
        if type(stamp) not in (int, float) or not 0 <= clock - stamp <= RETENTION_DAYS * 86400:
            continue
        date = datetime.fromtimestamp(stamp, timezone.utc).astimezone(zone).date().isoformat()
        if day and date != day:
            continue
        # Old manifests lack a permission receipt. Do not make archived source text a revocation
        # bypass; ask for a fresh source read instead. Changes in either direction require refresh.
        if manifest.get('source_permission_fingerprint') != fingerprint:
            withheld = True
            continue
        full = manifest.get('full_text')
        if not isinstance(full, str) or len(full) > 100_000:
            continue
        if text and text.casefold() not in full.casefold():
            continue
        matches.append((stamp, date, path.parent.name, manifest))
    matches.sort(key=lambda m: (m[0], m[2]), reverse=True)
    if not matches:
        return {'status': 'withheld' if withheld else 'not_found',
                'reason': ('Saved source permissions changed or are unknown. Read the connected sources afresh.' if withheld else
                           'No matching accepted photo text was found in the seven-day retained history.'),
                'coverage': 'Missing or expired photo text does not mean there was no prior conversation.'}
    if len(matches) > 1 and (day or text) and not artifact:
        return {'status': 'ambiguous', 'matches': [
            {'id': m[2], 'date': m[1], 'title': m[3].get('title'), 'kind': m[3]['kind']}
            for m in matches[:MAX_MATCHES]], 'omitted_matches': max(0, len(matches) - MAX_MATCHES)}
    stamp, date, ident, manifest = matches[0]
    full = manifest['full_text']
    end = min(offset + PAGE_CHARS, len(full))
    return {'status': 'found', 'id': ident, 'date': date, 'title': manifest.get('title'),
            'kind': manifest['kind'], 'text': full[offset:end], 'offset': offset,
            'next_offset': end if end < len(full) else None, 'total_chars': len(full),
            'coverage': 'Exact composed text behind an accepted photo gallery; not a full source transcript or proof of device display.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', default='')
    parser.add_argument('--date', default='')
    parser.add_argument('--kind', choices=('brief', 'prep'), default='')
    parser.add_argument('--query', default='')
    parser.add_argument('--offset', type=int, default=0)
    args = parser.parse_args()
    print(json.dumps(query(artifact=args.id, day=args.date, kind=args.kind, text=args.query, offset=args.offset)))


if __name__ == '__main__':
    main()
