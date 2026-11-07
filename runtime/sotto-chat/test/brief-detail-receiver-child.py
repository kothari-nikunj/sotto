"""Credential-free accepted-artifact receiver for the cross-language read contract."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
from datetime import datetime
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from zoneinfo import ZoneInfo

source = Path(sys.argv[1]).resolve()
fixture = Path(sys.argv[2]).resolve()
data = fixture / 'data'
sys.path.insert(0, str(source / 'runtime/trigger-receiver'))
sys.path.insert(0, str(source / 'sotto-chief-of-staff/_shared/lib'))
os.environ['SOTTO_MCP_TOKEN'] = 'synthetic-receiver-root-bearer-for-offline-contract'
os.environ['SOTTO_DATA'] = str(data)
os.environ['SOTTO_TIMEZONE'] = 'UTC'
import receiver as rec  # noqa: E402
from retained_brief_proof import gallery_payload_hash, route_digest, stamp  # noqa: E402

rec.DATA = str(data)
revision = 'a' * 64
telegram = len(sys.argv) > 3 and sys.argv[3] == 'telegram'
if telegram:
    # Real protected link + expectation + credential-HMAC verifier. Only the
    # loaded Node identity HTTP response is synthetic, never the route verifier.
    from test_telegram_channel_proof import _fixture, ZONE
    _, _, _, channel_client, _, policy = _fixture(fixture)
    os.environ.update(policy)
    os.environ['SOTTO_TIMEZONE'] = ZONE
    rec._runtime_adapter = lambda: SimpleNamespace(_client=lambda **_: channel_client)
    route = rec._candidate_retained_route_digest()
    assert route is not None
else:
    route = route_digest('photon', '', 'b' * 64, '+15555550123', 'account-1', 'line-1',
                         'imessage:iMessage;-;+15555550123~+15555550999')
artifact = 'c' * 24
folder = data / 'cache/visual-briefs' / artifact
folder.mkdir(parents=True)
folder.chmod(0o700)
images = [b'\x89PNG\r\n\x1a\nsynthetic-image-' + str(i).encode() for i in range(4)]
image_hashes = []
for index, image in enumerate(images, 1):
    image_path = folder / f'{index:02d}.png'
    image_path.write_bytes(image)
    image_path.chmod(0o600)
    image_hashes.append(hashlib.sha256(image).hexdigest())
manifest = {'id': artifact, 'kind': 'brief', 'preview': False, 'title': 'Accepted morning brief',
            'summary': 'Résumé 📨 / 東京', 'full_text': 'Accepted owner detail. Source secret withheld. Cedar procurement saved briefing detail.'}
proof = stamp(manifest, revision, route, image_hashes)
manifest['retained_text_proof'] = proof
manifest_path = folder / 'manifest.json'
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))
manifest_path.chmod(0o600)
events = data / 'events'
events.mkdir()
events.chmod(0o700)
receipt = {'visual_artifact_id': artifact, 'message_id': 'synthetic-parent',
           'accepted_at': time.time(), 'route_proof_version': 1, 'route_digest': route,
           'payload_hash': gallery_payload_hash(manifest['summary'], image_hashes),
           'retained_text_proof': proof}
long_artifact = 'd' * 24
long_folder = data / 'cache/visual-briefs' / long_artifact
long_folder.mkdir()
long_folder.chmod(0o700)
for index, image in enumerate(images, 1):
    path = long_folder / f'{index:02d}.png'
    path.write_bytes(image)
    path.chmod(0o600)
long_manifest = {'id': long_artifact, 'kind': 'brief', 'preview': False,
                 'title': 'Long accepted brief', 'summary': 'Long detail',
                 'full_text': '😀' * 4000}
long_proof = stamp(long_manifest, revision, route, image_hashes)
long_manifest['retained_text_proof'] = long_proof
path = long_folder / 'manifest.json'
path.write_text(json.dumps(long_manifest, ensure_ascii=False))
path.chmod(0o600)
long_receipt = {'visual_artifact_id': long_artifact, 'message_id': 'synthetic-long-parent',
                'accepted_at': receipt['accepted_at'] - 1, 'route_proof_version': 1,
                'route_digest': route,
                'payload_hash': gallery_payload_hash(long_manifest['summary'], image_hashes),
                'retained_text_proof': long_proof}
# Real accepted retained-text proofs; unproved content is never a discovery result.
extra_receipts = []
for extra_id, extra_title, extra_text, extra_revision in (
    ('e'*24, 'Accepted evening Cedar brief', 'Another Cedar procurement gallery.', revision),
    ('f'*24, 'Withheld secret Cedar title', 'Cedar secret text must not appear.', '0'*64),
):
    extra_folder = data / 'cache/visual-briefs' / extra_id
    extra_folder.mkdir()
    extra_folder.chmod(0o700)
    for index, image in enumerate(images, 1):
        image_path = extra_folder / f'{index:02d}.png'
        image_path.write_bytes(image)
        image_path.chmod(0o600)
    extra_manifest = {'id':extra_id,'kind':'brief','preview':False,'title':extra_title,
                      'summary':'Saved Cedar brief','full_text':extra_text}
    extra_proof = stamp(extra_manifest, extra_revision, route, image_hashes)
    extra_manifest['retained_text_proof'] = extra_proof
    extra_path = extra_folder / 'manifest.json'
    extra_path.write_text(json.dumps(extra_manifest))
    extra_path.chmod(0o600)
    extra_receipts.append({'visual_artifact_id':extra_id,'message_id':'synthetic-'+extra_id,
        'accepted_at':receipt['accepted_at']-2,'route_proof_version':1,'route_digest':route,
        'payload_hash':gallery_payload_hash(extra_manifest['summary'],image_hashes),
        'retained_text_proof':extra_proof})
# Use the canonical archive producer too; discovery still requires accepted gallery proof.
sys.path.insert(0, str(source / 'sotto-chief-of-staff/_shared/scripts'))
import compose_brief  # noqa: E402
compose_brief._archive_brief({'brief_markdown':manifest['full_text'],'type':'morning'},'morning')
for path in (data/'briefs').glob('*.json'):
    path.chmod(0o600)

outbox = events / 'outbox.json'
outbox.write_text(json.dumps({'rows': [{'status': 'delivered', 'acceptance': 'accepted',
                                         'receipt': receipt},
                                        {'status': 'delivered', 'acceptance': 'accepted',
                                         'receipt': long_receipt}]+[{'status':'delivered','acceptance':'accepted','receipt':item} for item in extra_receipts]}))
outbox.chmod(0o600)

rec._runtime_source_authority = lambda: {
    'version': 2, 'complete': True,
    'revision': 'd' * 64 if (fixture / 'revoked').exists() else revision,
    'bridge_sources': [], 'google_sources': [],
}
if not telegram:
    rec._candidate_retained_route_digest = lambda: route
server = ThreadingHTTPServer(('127.0.0.1', 0), rec.Handler)
print(json.dumps({'ready': True, 'url': f'http://127.0.0.1:{server.server_port}/mcp',
                  'bearer': rec.MCP_TOKEN, 'artifact': artifact,
                  'long_artifact': long_artifact,
                  'revision': revision, 'date':datetime.fromtimestamp(receipt['accepted_at'], ZoneInfo(os.environ['SOTTO_TIMEZONE'])).date().isoformat()}), flush=True)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    server.serve_forever()
finally:
    server.server_close()
