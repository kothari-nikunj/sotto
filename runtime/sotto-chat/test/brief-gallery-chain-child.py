"""Offline real receiver composition/outbox/HTTP fixture; fake model and source observation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import sys
from http.server import ThreadingHTTPServer

source = Path(sys.argv[1]).resolve()
data = Path(sys.argv[2]).resolve()
config = json.loads(Path(sys.argv[3]).read_text())
sys.path.insert(0, str(source / 'runtime/trigger-receiver'))
sys.path.insert(0, str(source / 'sotto-chief-of-staff/_shared/lib'))
sys.path.insert(0, str(source / 'adapters/sotto'))
os.environ.update({
    'SOTTO_DATA': str(data), 'SOTTO_CRON_DELIVER': 'photon',
    'SOTTO_VISUAL_BRIEFS': '1', 'SOTTO_DEPLOYMENT_MODE': 'managed',
    'SOTTO_TENANT_ID': config['tenantId'],
    'SOTTO_CHAT_RUNTIME_URL': f"http://127.0.0.1:{config['port']}",
    'SOTTO_CHAT_RUNTIME_TOKEN': config['bearerToken'],
    'SOTTO_MCP_TOKEN': 'offline-receiver-read-bearer-0000000000000000',
    'PHOTON_PROJECT_ID': config['credentials']['photonProjectId'],
    'PHOTON_PROJECT_SECRET': config['credentials']['photonProjectSecret'],
    'PHOTON_HOME_CHANNEL': config['owner']['ownerId'],
    'PHOTON_ALLOWED_USERS': config['owner']['ownerId'],
    'PHOTON_ALLOW_ALL_USERS': 'false',
})
import receiver as rec  # noqa: E402
import runtime_activation  # noqa: E402
import startup_config  # noqa: E402

rec.DATA = str(data)
data.mkdir(mode=0o700, parents=True, exist_ok=True)
data.chmod(0o700)
rec._configured_tz_name = lambda: 'UTC'
rec._runtime_source_authority = lambda: {
    'version': 2, 'complete': True,
    'revision': 'b' * 64 if (data.parent / 'revoked').exists() else 'a' * 64,
    'bridge_sources': [], 'google_sources': [],
}
rec._composed_brief_recently = lambda *_: True
rec._read_delivery_effects = lambda *_: {}
rec.OUTBOX.HOOKS['brief_gate'] = lambda *_: 'send'
rec.OUTBOX.HOOKS['valid'] = lambda *_: True
rec.OUTBOX.HOOKS['on_delivered'] = lambda *_: None
rec.OUTBOX.HOOKS['on_invalid'] = lambda *_: None

client = rec._runtime_adapter()._client(timeout=5)
pending = client.activation_evidence()
assert pending is not None
photon = rec._sotto_adapter('photon_owner')
assert runtime_activation.consume(
    pending, tenant_id=config['tenantId'], owner_id=config['owner']['ownerId'],
    project_fingerprint=photon.project_fingerprint(config['credentials']['photonProjectId']),
    writer=photon.record_owner_activation, ack=client.activation_ack)
startup_config.stage_receiver_channel_expectation(
    data / 'runtime', startup_config.receiver_channel_expectation(config))
route = rec._candidate_retained_route_digest()
assert isinstance(route, str) and len(route) == 64, 'current Node ACK/receiver receipt did not prove route'

BRIEF = '''Good morning — Monday, September 14
Needs Attention Now
Alex — Complete the preschool waiver by tomorrow; the form is still outstanding.
Should Handle Today
Jordan — Reply to the lunch invitation for Thursday.
Coming Up
10:00 AM — Team meeting
12:00 PM — Lunch with Alex
Already Handled
Sam — Replied yesterday.
Filtered
Donation blast and social chatter.
'''

class FakeModelProcess:
    returncode = 0
    pid = 12345
    def communicate(self, timeout):
        return BRIEF, ''
    def poll(self):
        return 0

rec.subprocess.Popen = lambda *_args, **_kwargs: FakeModelProcess()
rec._require_sotto_procedure = lambda *_args: None
adapter = rec._runtime_adapter()
adapter.run_argv = lambda *_args: ['/fixture-model']
rec._record_delivery = lambda *_args, **_kwargs: None
job = {'id': 'offline-brief', 'kind': 'run', 'owner': 'worker', 'payload': {
    'runner': ['/fixture-model'], 'prompt': '{}', 'label': 'cron:sotto-morning-brief'}}
result = rec._execute_work(job)
assert result['route_digest_at_composition'] == route
assert result['source_revision_at_composition'] == 'a' * 64
rec.WORK_QUEUE.enqueue(str(data), 'run', job['payload'], key=job['id'])
leased = rec.WORK_QUEUE.claim(str(data), 'worker')
rec.WORK_QUEUE.save_result(str(data), leased['id'], 'worker', result)
saved = rec.WORK_QUEUE.get(str(data), leased['id'])['result']
assert rec._deliver_text(saved['text'], saved['label'], run_id=leased['id'],
    source_revision_at_composition=saved['source_revision_at_composition'],
    route_digest_at_composition=saved['route_digest_at_composition'],
    source_text_sha256=saved['source_text_sha256'])
rows = json.loads((data / 'events/outbox.json').read_text())['rows']
receipt = rows[0]['receipt']
assert receipt['route_digest'] == route and 'retained_text_proof' in receipt
artifact = receipt['visual_artifact_id']
folder = data / 'cache/visual-briefs' / artifact
image_hashes = [hashlib.sha256((folder / f'{i:02d}.png').read_bytes()).hexdigest()
                for i in range(1, 5)]
server = ThreadingHTTPServer(('127.0.0.1', 0), rec.Handler)
print(json.dumps({'ready': True, 'url': f'http://127.0.0.1:{server.server_port}/mcp',
                  'bearer': rec.MCP_TOKEN, 'artifact': artifact, 'route_digest': route,
                  'payload_hash': receipt['payload_hash'], 'image_hashes': image_hashes,
                  'revision': 'a' * 64}), flush=True)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    server.serve_forever()
finally:
    server.server_close()
