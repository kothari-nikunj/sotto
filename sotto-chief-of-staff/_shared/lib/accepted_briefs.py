"""Join retained brief artifacts to the outbox runs that accepted them."""

import hashlib
import json
import math
from pathlib import Path
import re

_LABEL = re.compile(r'.+:sotto-(morning|evening|welcome)-brief')
_DAY = re.compile(r'\d{4}-\d{2}-\d{2}')


def _brief_kind(label):
    match = _LABEL.fullmatch(label) if isinstance(label, str) else None
    return match[1] if match else None


def body_proof(payload):
    """Retain a digest when an outbox brief settles as accepted."""
    body = payload.get('body')
    if _brief_kind(payload.get('label')) and isinstance(body, str):
        return {'brief_body_sha256': hashlib.sha256(body.encode()).hexdigest()}
    return {}


def _accepted_runs(root, exact):
    try:
        rows = json.loads((root / 'events/outbox.json').read_text())['rows']
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(rows, list):
        return {}
    accepted = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('status') != 'delivered':
            continue
        run, day = row.get('id'), row.get('day')
        payload, receipt = row.get('payload'), row.get('receipt')
        label = payload.get('label') if isinstance(payload, dict) else None
        kind = _brief_kind(label)
        if not (kind and isinstance(day, str) and _DAY.fullmatch(day)
                and isinstance(run, str) and run):
            continue
        contacted = row.get('contacted_at')
        if exact and (row.get('acceptance') != 'accepted' or row.get('acceptance_uncertain') is not False
                      or not isinstance(receipt, dict) or type(contacted) not in (int, float)
                      or not 0 < contacted < math.inf):
            continue
        accepted[run] = (f'{day}_{kind}', receipt if isinstance(receipt, dict) else {}, contacted)
    return accepted


def bindings(root, *, delivery_body, exact=False):
    """Yield edition, artifact and accepted run from one outbox snapshot.

    Exact consumers require the body digest. Replay can still use receipts from before
    the digest was introduced, with the existing run binding and its own exclusions.
    """
    root = Path(root)
    accepted = _accepted_runs(root, exact)
    if not accepted:
        return

    for path in (root / 'events/work-inputs').glob('brief-*/artifact.json'):
        try:
            artifact = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(artifact, dict):
            continue
        text, runs = artifact.get('brief_text'), artifact.get('_delivery_run_ids')
        if not isinstance(text, str) or not text.strip() or not isinstance(runs, list):
            continue
        for run in runs:
            if not isinstance(run, str) or run not in accepted:
                continue
            reference, receipt, contacted = accepted[run]
            stamp = receipt.get('accepted_at')
            if exact and (type(stamp) not in (int, float) or not 0 < stamp < math.inf
                          or 'brief_body_sha256' not in receipt):
                continue
            if 'brief_body_sha256' in receipt:
                body = delivery_body(text)
                try:
                    digest = hashlib.sha256(body.encode()).hexdigest()
                except UnicodeEncodeError:
                    continue
                if not body or digest != receipt['brief_body_sha256']:
                    continue
            yield reference, artifact, run, receipt, contacted


def permitted_sources(artifact, allowed):
    sources = artifact.get('_source_permissions')
    return isinstance(sources, list) and all(isinstance(source, str) and allowed(source) for source in sources)
