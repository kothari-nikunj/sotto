"""Integrity binding for future retained text; never grants source or route access."""
from __future__ import annotations

import hashlib
import json
import re
import struct

HEX64 = re.compile(r'\A[a-f0-9]{64}\Z')
HEX24 = re.compile(r'\A[a-f0-9]{24}\Z')
MAX_TEXT = 64 * 1024
MAX_TITLE = 512
MAX_SUMMARY = 1000
MAX_RESEARCH_EVIDENCE = 16 * 1024
MAX_RESEARCH_RECORDS = 12
MAX_OMITTED_RESEARCH_RECORDS = 1000


def validate_research_evidence(value):
    """One bounded prep attachment, not a source grant or truth assertion."""
    if value == {'status': 'unavailable'}:
        return value
    if (type(value) is not dict or set(value) != {'status', 'input_sha256', 'records', 'omitted_records'}
            or value['status'] != 'retained' or not _hex64(value['input_sha256'])
            or type(value['omitted_records']) is not int or not 0 <= value['omitted_records'] <= MAX_OMITTED_RESEARCH_RECORDS
            or type(value['records']) is not list or not 1 <= len(value['records']) <= MAX_RESEARCH_RECORDS):
        raise ValueError('invalid research evidence')
    for record in value['records']:
        if (type(record) is not dict or set(record) != {'provider', 'stage', 'subjects', 'response_json',
                'response_sha256', 'grounding_json', 'truncated'}
                or record['provider'] not in ('gemini', 'exa', 'parallel')
                or record['stage'] not in ('profile', 'recency', 'focus', 'company')
                or type(record['subjects']) is not list or len(record['subjects']) > 25
                or any(type(subject) is not str or len(subject) > 200 for subject in record['subjects'])
                or type(record['response_json']) is not str or len(record['response_json']) > 2000
                or not _hex64(record['response_sha256'])
                or type(record['grounding_json']) is not str or not 0 < len(record['grounding_json']) <= 3000
                or type(record['truncated']) is not bool):
            raise ValueError('invalid research evidence record')
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    if len(raw.encode('utf-8')) > MAX_RESEARCH_EVIDENCE:
        raise ValueError('research evidence exceeds bound')
    return value


def route_digest(channel, tenant_id, project_fingerprint, owner_id, account_id, line_key,
                 provider_thread_id):
    """Match the Node accepted-dispatch route transcript, not an activation event ID."""
    parts = [channel, tenant_id, project_fingerprint, owner_id, account_id, line_key,
             provider_thread_id]
    if (channel not in ('photon', 'telegram')
            or any(not isinstance(part, str) for part in parts)
            or any(not part for part in parts[3:])
            or (project_fingerprint and not _hex64(project_fingerprint))):
        raise ValueError('invalid route identity')
    encoded = [part.encode('utf-8') for part in parts]
    if any(len(part) > 4096 for part in encoded) or any(
            len(part.encode('utf-16-le')) // 2 > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in part)
            for part in parts):
        raise ValueError('route identity is too long')
    raw = b'sotto-dispatch-route-v1\0' + struct.pack('>I', len(encoded))
    raw += b''.join(struct.pack('>I', len(part)) + part for part in encoded)
    return hashlib.sha256(raw).hexdigest()


def _hex64(value):
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def gallery_payload_hash(summary, image_hashes):
    """Match Node's JSON.stringify({summary,images}) and SHA-256 of exact PNG bytes.

    Only strings with valid Unicode scalars enter this protected path. The
    ordered two-key compact JSON representation has a cross-language fixture.
    """
    if (not isinstance(summary, str) or not 0 < len(summary)
            or len(summary.encode('utf-16-le')) // 2 > MAX_SUMMARY
            or not isinstance(image_hashes, list) or len(image_hashes) != 4
            or any(not _hex64(item) for item in image_hashes)):
        raise ValueError('invalid gallery payload')
    raw = json.dumps({'summary': summary, 'images': image_hashes}, ensure_ascii=False,
                     separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def text_payload_hash(text):
    """Match the scheduled-text transport's exact JSON payload."""
    return hashlib.sha256(json.dumps({'text': text}, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def payload_hash(manifest, image_hashes, version):
    if version == 3:
        if image_hashes != []:
            raise ValueError('text proof cannot contain images')
        return text_payload_hash(manifest['full_text'])
    return gallery_payload_hash(manifest['summary'], image_hashes)


def content_digest(manifest, source_revision, route_digest, image_hashes, *, version=1):
    """Hash exactly the source-bearing fields exposed by the protected reader."""
    if (not isinstance(manifest, dict) or not _hex64(source_revision)
            or not _hex64(route_digest) or not isinstance(manifest.get('id'), str)
            or HEX24.fullmatch(manifest['id']) is None
            or manifest.get('kind') not in ('brief', 'prep')
            or type(manifest.get('preview')) is not bool
            or manifest['preview'] is not False
            or not isinstance(manifest.get('title'), str)
            or len(manifest['title']) > MAX_TITLE
            or not isinstance(manifest.get('summary'), str)
            or not isinstance(manifest.get('full_text'), str)
            or len(manifest['full_text'].encode('utf-8')) > MAX_TEXT):
        raise ValueError('invalid retained text fields')
    gallery_hash = payload_hash(manifest, image_hashes, version)
    if version not in (1, 2, 3) or (version == 1 and 'research_evidence' in manifest):
        raise ValueError('research evidence requires versioned proof')
    fields = [version, manifest['id'], manifest['kind'], manifest['title'], manifest['full_text'],
              manifest['preview'], source_revision, route_digest, gallery_hash]
    if version in (2, 3):
        fields.append(validate_research_evidence(manifest.get('research_evidence')))
    raw = json.dumps(fields, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(f'sotto-retained-brief-v{version}\0'.encode() + raw).hexdigest()


def stamp(manifest, source_revision, route_digest, image_hashes, *, text=False):
    version = 3 if text else 2 if 'research_evidence' in manifest else 1
    # Keep the receipt field name for compatibility; v3 binds the scheduled-text payload.
    return {'version': version, 'source_revision': source_revision,
            'route_digest': route_digest,
            'image_sha256s': image_hashes,
            'gallery_payload_sha256': payload_hash(manifest, image_hashes, version),
            'content_sha256': content_digest(manifest, source_revision, route_digest, image_hashes, version=version)}


def valid(manifest, proof):
    if (not isinstance(proof, dict) or set(proof) != {
            'version', 'source_revision', 'route_digest', 'content_sha256',
            'image_sha256s', 'gallery_payload_sha256'}
            or type(proof['version']) is not int or proof['version'] not in (1, 2, 3)
            or not _hex64(proof['content_sha256'])
            or not _hex64(proof['gallery_payload_sha256'])):
        return False
    try:
        return (content_digest(manifest, proof['source_revision'], proof['route_digest'],
                               proof['image_sha256s'], version=proof['version']) == proof['content_sha256']
                and payload_hash(manifest, proof['image_sha256s'], proof['version'])
                    == proof['gallery_payload_sha256'])
    except (ValueError, TypeError, UnicodeError):
        return False
