import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { OperationalJournal } from '../src/journal/index.js';
import { ReceiptedDelivery } from '../src/delivery.js';

const root = resolve(fileURLToPath(new URL('../../..', import.meta.url)));
const pythonSource = String.raw`
import json, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
from client import _clean_receipt
from retained_brief_proof import route_digest, gallery_payload_hash
request = json.load(sys.stdin)
identity = request['identity']
print(json.dumps({
  'receipt': _clean_receipt(request['receipt']),
  'route_digest': route_digest(identity['channel'], identity['tenantId'],
    identity['projectFingerprint'], identity['ownerId'], identity['accountId'],
    identity['lineKey'], request['thread']),
  'payload_hash': gallery_payload_hash(request['summary'], request['image_hashes'])
}))
`;

function pythonContract(input: unknown): Record<string, unknown> {
  const child = spawnSync(process.env.PYTHON ?? 'python3', ['-c', pythonSource,
    join(root, 'adapters/sotto'), join(root, 'sotto-chief-of-staff/_shared/lib')], {
    input: JSON.stringify(input), encoding: 'utf8', timeout: 15_000, maxBuffer: 64 * 1024,
    env: { PATH: process.env.PATH ?? '', PYTHONIOENCODING: 'utf-8' },
  });
  if (child.error || child.status !== 0) {
    throw new Error(`Python contract failed: ${child.error?.name ?? child.status}`);
  }
  return JSON.parse(child.stdout) as Record<string, unknown>;
}

test('actual Node gallery receipt projects to strict Python proof; unknown and legacy do not', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'sotto-retained-proof-'));
  const journal = new OperationalJournal(join(dir, 'journal.sqlite'));
  let acceptance: 'accepted' | 'unknown' = 'accepted';
  const identity = { channel: 'photon' as const, tenantId: 'tenant-1',
    projectFingerprint: 'a'.repeat(64), ownerId: 'owner-é', accountId: 'account-1', lineKey: 'line-1' };
  const thread = 'imessage:iMessage;-;+15555550123~+15555550999';
  const delivery = new ReceiptedDelivery(journal, new Map([['photon', {
    threadId: thread, transport: { send: async () => ({ acceptance,
      providerMessageIds: acceptance === 'accepted' ? ['parent-id'] : [] }) },
  }]]), identity);
  try {
    const paths = [0, 1, 2, 3].map(index => {
      const path = join(dir, `image-${index}.png`);
      writeFileSync(path, `image-${index}`);
      return path;
    });
    const summary = 'Résumé 📨 / 東京';
    const imageHashes = [0, 1, 2, 3].map(index =>
      createHash('sha256').update(`image-${index}`).digest('hex'));
    const request = { dispatch_id: 'protected-gallery', target: 'photon', summary, paths };
    const accepted = await delivery.sendGallery(request);
    const projected = pythonContract({ identity, thread, summary, image_hashes: imageHashes, receipt: accepted });
    assert.deepEqual(projected.receipt, accepted);
    assert.equal(projected.route_digest, accepted.route_digest);
    assert.equal(projected.payload_hash, accepted.payload_hash);

    acceptance = 'unknown';
    const unknown = await delivery.sendGallery({ ...request, dispatch_id: 'unknown-gallery' });
    assert.deepEqual(pythonContract({ identity, thread, summary,
      image_hashes: imageHashes, receipt: unknown }).receipt, { acceptance: 'unknown' });

    journal.db.prepare('UPDATE dispatches SET route_digest=NULL WHERE id=?').run(request.dispatch_id);
    const legacy = delivery.getReceipt(request.dispatch_id).receipt;
    assert.deepEqual(pythonContract({ identity, thread, summary,
      image_hashes: imageHashes, receipt: legacy }).receipt,
    { acceptance: 'accepted', message_id: 'parent-id', message_ids: ['parent-id'] });
  } finally { journal.close(); rmSync(dir, { recursive: true, force: true }); }
});
