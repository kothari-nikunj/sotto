"""Bounded future-only retained accepted artifact projection; no HTTP or grant writer.

The receiver must supply a live complete source revision and an independently
verified current exact Photon route digest. This module never treats an old
manifest, a sidecar, or a symbolic target hash as acceptance proof.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import re
import stat
import time
import sys
from datetime import datetime, timezone
from pathlib import Path

_LIBRARY = Path(__file__).resolve().parent
if str(_LIBRARY) not in sys.path:
    sys.path.insert(0, str(_LIBRARY))
from brief_detail_selection import accepted_day, MAX_MATCHES  # noqa: E402
from retained_brief_proof import gallery_payload_hash, valid as valid_proof  # noqa: E402

MAX_OUTBOX = 4 * 1024 * 1024
MAX_MANIFEST = 128 * 1024
MAX_ROWS = 1024
MAX_PAGE = 4000
MAX_DISCOVERY_ARTIFACTS = 32
MAX_DISCOVERY_BYTES = 64 * 1024 * 1024
_HEX24 = re.compile(r'\A[a-f0-9]{24}\Z')
_HEX64 = re.compile(r'\A[a-f0-9]{64}\Z')
_KNOWLEDGE = str(_LIBRARY.parent / 'knowledge')
if _KNOWLEDGE not in sys.path:
    sys.path.insert(0, _KNOWLEDGE)


class UnavailableError(ValueError):
    pass


def _unique(pairs):
    row = {}
    for key, value in pairs:
        if key in row:
            raise UnavailableError('duplicate JSON key')
        row[key] = value
    return row


def _nonfinite(_value):
    raise UnavailableError('nonfinite JSON number')


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise UnavailableError('nonfinite JSON number')
    return parsed


def _private_bytes(root, parts, limit, budget=None):
    handles = []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        handles.append(fd)
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            handles.append(fd)
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
        handles.append(leaf)
        info = os.fstat(leaf)
        if (not stat.S_ISREG(info.st_mode) or info.st_size < 1 or info.st_size > limit
                or info.st_mode & 0o077 or info.st_uid not in (os.geteuid(), 0)):
            raise UnavailableError('private document invalid')
        with os.fdopen(os.dup(leaf), 'rb') as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise UnavailableError('private document too large')
        if budget is not None:
            budget[0] -= len(raw)
            if budget[0] < 0:
                raise UnavailableError('discovery exceeds byte bound')
        return raw
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise UnavailableError('private document unavailable') from error
    finally:
        for fd in reversed(handles):
            os.close(fd)


def _private_json(root, parts, limit, budget=None):
    try:
        return json.loads(_private_bytes(root, parts, limit, budget).decode('utf-8'),
                          object_pairs_hook=_unique, parse_constant=_nonfinite,
                          parse_float=_finite_float)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise UnavailableError('private JSON unavailable') from error


def outbox_rows(data_root):
    outbox = _private_json(data_root, ('events', 'outbox.json'), MAX_OUTBOX)
    rows = outbox.get('rows') if isinstance(outbox, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
        raise UnavailableError('outbox rows unavailable')
    return rows


def retained_candidate(payload):
    presentation = payload.get('presentation') if isinstance(payload, dict) else None
    text_artifact = payload.get('retained_text_artifact') if isinstance(payload, dict) else None
    proof = (presentation.get('retained_text_proof') if isinstance(presentation, dict)
             else text_artifact.get('retained_text_proof') if isinstance(text_artifact, dict) else None)
    expected_versions = (1, 2) if isinstance(presentation, dict) else (3,)
    if (not isinstance(proof, dict) or set(proof) != {
            'version', 'source_revision', 'route_digest', 'content_sha256',
            'gallery_payload_sha256', 'image_sha256s'}
            or type(proof['version']) is not int or proof['version'] not in expected_versions
            or any(not isinstance(proof[key], str) or re.fullmatch(r'[a-f0-9]{64}', proof[key]) is None
                   for key in ('source_revision', 'route_digest', 'content_sha256',
                               'gallery_payload_sha256'))
            or not isinstance(proof['image_sha256s'], list)
            or len(proof['image_sha256s']) != (0 if proof['version'] == 3 else 4)
            or any(not isinstance(item, str) or re.fullmatch(r'[a-f0-9]{64}', item) is None
                   for item in proof['image_sha256s'])):
        return None
    return proof


def contact_scope(payload):
    return retained_candidate(payload) or notification_candidate(payload) or payload.get('task_snapshot')


def notification_label(label):
    return label == 'event'


def notification_candidate(payload):
    if not notification_label(payload.get('label')) or payload.get('presentation'):
        return None
    decisions = payload.get('decision_ids') or []
    if not isinstance(decisions, list) or len(decisions) != 1 or not isinstance(decisions[0], str) or not re.fullmatch(r'(?:[a-f0-9]{16}|[a-f0-9]{64})', decisions[0]):
        return None
    references = [effect.get('request_reference') for effect in payload.get('effects') or []
                  if effect.get('notification_id') == decisions[0] and effect.get('kind') == 'eligibility']
    if len(references) != 1 or not isinstance(references[0], dict):
        return None
    # A Gmail message ID identifies immutable message contents. Editable local messages
    # need their own retained content version before they can use this rating path.
    source, ident = references[0].get('source'), references[0].get('id')
    scope = payload.get('notification_feedback_scope')
    if not isinstance(scope, dict):
        return None
    revision, route = scope.get('source_revision'), scope.get('route_digest')
    if (source != 'gmail' or not isinstance(ident, str) or not re.fullmatch(r'[^\x00-\x1f\x7f\ud800-\udfff]{1,512}', ident)
            or not all(isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value)
                       for value in (revision, route))):
        return None
    return {'source_revision': revision, 'route_digest': route, 'decision_id': decisions[0],
            'source': source, 'source_ref': hashlib.sha256(f'{source}:{ident}'.encode()).hexdigest()}


def accepted_notification(payload, receipt, retained_at_contact, retained_now):
    candidate = notification_candidate(payload)
    if (not candidate or not retained_at_contact or not retained_now or not isinstance(receipt, dict)
            or receipt.get('acceptance') != 'accepted' or type(receipt.get('route_proof_version')) is not int
            or receipt['route_proof_version'] != 1 or receipt.get('route_digest') != candidate['route_digest']
            or not isinstance(receipt.get('message_id'), str) or not 0 < len(receipt['message_id']) <= 512
            or receipt.get('message_ids') != [receipt['message_id']]):
        return {}
    body = json.dumps({'text': payload['body']}, ensure_ascii=False, separators=(',', ':')).encode()
    return {'notification_feedback': candidate} if receipt.get('payload_hash') == hashlib.sha256(body).hexdigest() else {}


def _notification_binding(row, scope):
    receipt = row.get('receipt')
    if not isinstance(receipt, dict):
        return None
    proof = receipt.get('notification_feedback')
    accepted_at = receipt.get('accepted_at')
    if (row.get('status') != 'delivered' or row.get('acceptance') != 'accepted'
            or receipt.get('acceptance') != 'accepted' or type(receipt.get('route_proof_version')) is not int
            or receipt['route_proof_version'] != 1
            or not isinstance(proof, dict)
            or proof.get('source') != 'gmail'
            or not re.fullmatch(r'(?:[a-f0-9]{16}|[a-f0-9]{64})', str(proof.get('decision_id') or ''))
            or not _HEX64.fullmatch(str(proof.get('source_ref') or ''))
            or any(not _HEX64.fullmatch(str(scope.get(k) or '')) or proof.get(k) != scope[k]
                   for k in ('source_revision', 'route_digest'))
            or receipt.get('route_digest') != scope['route_digest']
            or not _HEX64.fullmatch(str(receipt.get('payload_hash') or ''))
            or not isinstance(receipt.get('message_id'), str) or not receipt['message_id']
            or receipt.get('message_ids') != [receipt['message_id']]
            or type(accepted_at) not in (int, float) or not math.isfinite(accepted_at)):
        return None
    identity = [row['id'], accepted_at, receipt['message_id'], receipt['payload_hash'], proof]
    version = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {**proof, 'archive_version': version}


def notification_bindings(data_root, scope, references):
    """Index current accepted notification receipts once. Ambiguous dispatches stay unavailable."""
    try:
        rows = outbox_rows(data_root)
    except UnavailableError:
        return {}
    result = {}
    for row in rows:
        dispatch = row.get('id')
        if isinstance(dispatch, str) and (ref := 'notification:' + dispatch) in references:
            result[ref] = None if ref in result else _notification_binding(row, scope)
    return result


def notification_feedback_receipt(data_root, args, admitted_at_ms, window_ms):
    """Project one accepted notification's content-free proof from the existing outbox."""
    try:
        matches = [row for row in outbox_rows(data_root) if row.get('id') == args['dispatch_id']]
    except UnavailableError:
        return None
    if len(matches) != 1:
        return None
    proof = _notification_binding(matches[0], args)
    receipt = matches[0].get('receipt') or {}
    if (not proof or receipt.get('payload_hash') != args['payload_hash']
            or receipt.get('message_id') != args['provider_message_id']
            or not 0 <= admitted_at_ms - receipt['accepted_at'] * 1000 <= window_ms):
        return None
    return proof


def validate_selector(selector):
    if type(selector) is not dict or type(selector.get('offset')) is not int or type(selector.get('limit')) is not int:
        raise ValueError('invalid selector')
    if (not 0 <= selector['offset'] <= 65536 or not 1 <= selector['limit'] <= MAX_PAGE
            or selector['offset'] + selector['limit'] > 65536):
        raise ValueError('invalid page')
    keys = set(selector)
    if keys == {'artifact_id', 'offset', 'limit'}:
        if type(selector['artifact_id']) is not str or _HEX24.fullmatch(selector['artifact_id']) is None:
            raise ValueError('invalid artifact')
        return 'exact'
    if keys == {'gallery_receipt', 'offset', 'limit'}:
        proof = selector['gallery_receipt']
        if (type(proof) is not dict or set(proof) != {'dispatch_id', 'message_id', 'payload_hash'}
                or not all(isinstance(value, str) for value in proof.values())
                or not re.fullmatch(r'[a-f0-9]{16,64}', proof['dispatch_id'])
                or not 0 < len(proof['message_id']) <= 512
                or not _HEX64.fullmatch(proof['payload_hash'])):
            raise ValueError('invalid gallery receipt')
        return 'gallery'
    if keys == {'latest', 'offset', 'limit'}:
        if selector['latest'] is not True:
            raise ValueError('invalid latest')
        return 'latest'
    if not keys <= {'date', 'query', 'kind', 'offset', 'limit'} or not keys & {'date', 'query', 'kind'} or selector['offset'] != 0:
        raise ValueError('invalid discovery')
    if 'date' in selector:
        day = selector['date']
        if type(day) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) is None:
            raise ValueError('invalid date')
        datetime.strptime(day, '%Y-%m-%d')
    if 'kind' in selector and selector['kind'] not in ('brief', 'prep'):
        raise ValueError('invalid kind')
    if 'query' in selector:
        query = selector['query']
        if (type(query) is not str or not query or query.strip() != query or len(query) > 200
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in query)):
            raise ValueError('invalid query')
    return 'discovery'


def _verified_artifact(data_root, artifact, rows, source_revision, route_digest, budget=None):
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            raise UnavailableError('outbox row invalid')
        receipt = row.get('receipt')
        if isinstance(receipt, dict) and (receipt.get('visual_artifact_id') or receipt.get('text_artifact_id')) == artifact:
            matches.append((row, receipt))
    if not matches:
        return None
    if len(matches) != 1:
        raise UnavailableError('artifact acceptance ambiguous')
    row, receipt = matches[0]
    proof = receipt.get('retained_text_proof')
    if (row.get('status') != 'delivered' or row.get('acceptance') != 'accepted'
            or not isinstance(proof, dict)
            or proof.get('source_revision') != source_revision
            or proof.get('route_digest') != route_digest
            or type(receipt.get('route_proof_version')) is not int
            or receipt['route_proof_version'] != 1
            or receipt.get('route_digest') != route_digest
            or receipt.get('payload_hash') != proof.get('gallery_payload_sha256')
            or not isinstance(receipt.get('message_id'), str)
            or not 0 < len(receipt['message_id']) <= 512
            or type(receipt.get('accepted_at')) not in (int, float)
            or not math.isfinite(receipt['accepted_at'])):
        return None
    if (proof.get('version') == 3) != (receipt.get('text_artifact_id') == artifact):
        return None
    manifest = _private_json(data_root, ('cache', 'visual-briefs', artifact,
                                          'manifest.json'), MAX_MANIFEST, budget)
    if (not isinstance(manifest, dict) or manifest.get('id') != artifact
            or manifest.get('retained_text_proof') != proof
            or not valid_proof(manifest, proof)):
        return None
    if proof['version'] == 3:
        if receipt.get('text_artifact_id') != artifact or receipt.get('visual_artifact_id') is not None:
            return None
        return manifest, receipt
    image_hashes = []
    for index in range(1, 5):
        image = _private_bytes(data_root, ('cache', 'visual-briefs', artifact,
                                           f'{index:02d}.png'), 5_000_000, budget)
        image_hashes.append(hashlib.sha256(image).hexdigest())
    if (image_hashes != proof['image_sha256s']
            or gallery_payload_hash(manifest['summary'], image_hashes)
                != receipt['payload_hash']):
        return None
    return manifest, receipt


def research_company_domain(evidence):
    from textutil import FREEMAIL_DOMAINS
    from research_contract import company_domain
    import knowledge_update as ku
    records = [record for record in evidence.get('records', []) if record.get('stage') == 'focus']
    if evidence.get('status') != 'retained' or evidence.get('omitted_records', 0) or len(records) != 1:
        return None
    subjects = records[0].get('subjects', [])
    if len(subjects) != 1 or len(subjects[0]) >= 200 or re.fullmatch(r'[^\s@]+@[^\s@]+', subjects[0]) is None:
        return None
    local, domain = subjects[0].lower().split('@')
    domain = company_domain(domain)
    if (not domain or domain in FREEMAIL_DOMAINS
            or domain.endswith(('.localhost', '.local', '.internal', '.home.arpa', '.onion'))
            or local in {'info', 'contact', 'support', 'sales', 'hello', 'team', 'admin', 'office', 'noreply', 'no-reply'}):
        return None
    try:
        company = ku.company_knowledge(company_name=domain, domain=domain, unique=True)
    except ku.CompanyLookupError:
        return None
    return domain if isinstance(company, dict) and company.get('domain', '').lower() == domain else None


def accepted_research_company(data_root, message_id, *, source_revision, route_digest, admitted_at_ms, context_window_ms):
    outbox = _private_json(data_root, ('events', 'outbox.json'), MAX_OUTBOX)
    rows = outbox.get('rows', []) if isinstance(outbox, dict) else None
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        return None
    matches = [row for row in rows if isinstance(row, dict)
               and isinstance(row.get('receipt'), dict) and row['receipt'].get('message_id') == message_id]
    if len(matches) != 1:
        return None
    receipt = matches[0]['receipt']
    artifact = receipt.get('text_artifact_id')
    accepted_at = receipt.get('accepted_at')
    if (not isinstance(artifact, str) or not _HEX24.fullmatch(artifact)
            or type(accepted_at) not in (int, float) or not math.isfinite(accepted_at)
            or not 0 <= admitted_at_ms - accepted_at * 1000 <= context_window_ms):
        return None
    verified = _verified_artifact(data_root, artifact, rows, source_revision, route_digest)
    if not verified or verified[0].get('kind') != 'prep':
        return None
    return research_company_domain(verified[0].get('research_evidence', {}))


def _page(artifact, manifest, selector):
    full = manifest['full_text']
    offset = selector['offset']
    end = min(len(full), offset + selector['limit'])
    domain = (research_company_domain(manifest.get('research_evidence', {}))
              if manifest.get('kind') == 'prep' and manifest.get('retained_text_proof', {}).get('version') == 3 else None)
    return {'status': 'found', 'coverage': 'accepted_artifacts_only', 'id': artifact,
            'kind': manifest['kind'], 'title': manifest['title'], 'text': full[offset:end],
            'offset': offset, 'next_offset': end if end < len(full) else None,
            'research_evidence': (manifest['research_evidence']
                if manifest.get('retained_text_proof', {}).get('version') in (2, 3)
                else {'status': 'unavailable'}),
            **({'research_company_domain': domain} if domain else {})}


def read(data_root, selector, *, source_revision, route_digest, zone=timezone.utc, now=None):
    """Select only accepted artifacts whose full content proof is currently eligible."""
    try:
        mode = validate_selector(selector)
    except (ValueError, TypeError):
        return {'status': 'invalid_selector'}
    if (not isinstance(source_revision, str) or _HEX64.fullmatch(source_revision) is None
            or not isinstance(route_digest, str) or _HEX64.fullmatch(route_digest) is None):
        return {'status': 'unavailable'}
    try:
        rows = outbox_rows(data_root)
        if mode == 'gallery':
            proof = selector['gallery_receipt']
            matches = [row for row in rows if row.get('id') == proof['dispatch_id']]
            receipt = matches[0].get('receipt') if len(matches) == 1 else None
            artifact = receipt.get('visual_artifact_id') if isinstance(receipt, dict) else None
            if (not isinstance(artifact, str) or not _HEX24.fullmatch(artifact)
                    or receipt.get('message_id') != proof['message_id']
                    or receipt.get('message_ids') != [proof['message_id']]
                    or receipt.get('payload_hash') != proof['payload_hash']):
                return {'status': 'withheld', 'coverage': 'accepted_artifacts_only'}
            verified = _verified_artifact(data_root, artifact, rows, source_revision, route_digest)
            if not verified or verified[0].get('kind') != 'brief':
                return {'status': 'withheld', 'coverage': 'accepted_artifacts_only'}
            return _page(artifact, verified[0], selector)
        if mode == 'exact':
            artifact = selector['artifact_id']
            if not any(isinstance(row.get('receipt'), dict) and (row['receipt'].get('visual_artifact_id') or row['receipt'].get('text_artifact_id')) == artifact for row in rows):
                return {'status': 'not_found', 'coverage': 'accepted_artifacts_only'}
            verified = _verified_artifact(data_root, artifact, rows, source_revision, route_digest)
            return _page(artifact, verified[0], selector) if verified else {'status': 'withheld', 'coverage': 'accepted_artifacts_only'}
        clock = time.time() if now is None else now
        candidates = []
        withheld = False
        for row in rows:
            receipt = row.get('receipt')
            if row.get('status') != 'delivered' or row.get('acceptance') != 'accepted' or not isinstance(receipt, dict):
                continue
            proof, artifact, stamp = receipt.get('retained_text_proof'), (receipt.get('visual_artifact_id') or receipt.get('text_artifact_id')), receipt.get('accepted_at')
            day = accepted_day(stamp, clock, zone) if mode == 'discovery' else None
            if mode == 'discovery' and (day is None or 'date' in selector and selector['date'] != day):
                continue  # the owner-local date needs only the receipt, so it filters before the bound
            if (not isinstance(proof, dict) or proof.get('source_revision') != source_revision
                    or proof.get('route_digest') != route_digest or type(artifact) is not str or not _HEX24.fullmatch(artifact)
                    or type(stamp) not in (int, float) or not math.isfinite(stamp)):
                withheld |= isinstance(proof, dict)
                continue
            candidates.append((stamp, artifact))
        candidates.sort(reverse=True)
        if mode == 'latest':
            if not candidates:
                return {'status': 'withheld' if withheld else 'not_found', 'coverage': 'accepted_artifacts_only'}
            artifact = candidates[0][1]
            verified = _verified_artifact(data_root, artifact, rows, source_revision, route_digest)
            return _page(artifact, verified[0], selector) if verified else {'status': 'withheld', 'coverage': 'accepted_artifacts_only'}
        # A busy week is bounded, never fatal: scan the newest artifacts within the artifact and byte
        # bounds, and count the older unscanned ones as omitted rather than claiming they do not match.
        omitted = max(0, len(candidates) - MAX_DISCOVERY_ARTIFACTS)
        candidates = candidates[:MAX_DISCOVERY_ARTIFACTS]
        budget = [MAX_DISCOVERY_BYTES]
        matches = []
        for index, (stamp, artifact) in enumerate(candidates):
            try:
                verified = _verified_artifact(data_root, artifact, rows, source_revision, route_digest, budget)
            except UnavailableError:
                if budget[0] >= 0:
                    verified = None  # one unreadable artifact is withheld; it never hides the others
                elif index == 0:
                    raise
                else:
                    omitted += len(candidates) - index
                    break
            if verified is None:
                withheld = True
                continue
            manifest, _receipt = verified
            day = accepted_day(stamp, clock, zone)
            if (('date' in selector and selector['date'] != day)
                    or ('kind' in selector and selector['kind'] != manifest['kind'])
                    or ('query' in selector and selector['query'].casefold() not in manifest['full_text'].casefold())):
                continue
            matches.append((artifact, day, manifest))
        if not matches:
            # Unscanned older artifacts are no evidence of absence.
            return {'status': 'withheld' if withheld or omitted else 'not_found', 'coverage': 'accepted_artifacts_only'}
        if len(matches) > 1 and ('date' in selector or 'query' in selector):
            return {'status': 'ambiguous', 'coverage': 'accepted_artifacts_only', 'matches': [
                {'id': artifact, 'date': day, 'title': manifest['title'], 'kind': manifest['kind']}
                for artifact, day, manifest in matches[:MAX_MATCHES]],
                'omitted_matches': max(0, len(matches) - MAX_MATCHES) + omitted}
        artifact, day, manifest = matches[0]
        return {**_page(artifact, manifest, selector), 'date': day}
    except (UnavailableError, OSError, ValueError, TypeError, OverflowError):
        return {'status': 'unavailable'}
