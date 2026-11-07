import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { OperationalJournal } from '/app/runtime/sotto-chat/dist/journal/index.js';
import { ReceiptedDelivery } from '/app/runtime/sotto-chat/dist/delivery.js';
import { createChatHttpServer, listenLoopback } from '/app/runtime/sotto-chat/dist/http.js';

const [stateDir, forbiddenPath, expectedPath] = process.argv.slice(2);
if (!stateDir || !forbiddenPath || !expectedPath) throw new Error('fixture arguments required');
const expected = JSON.parse(await readFile(expectedPath, 'utf8'));
let directReadDenied = false;
try { await readFile(forbiddenPath); } catch (error) { directReadDenied = error?.code === 'EACCES' || error?.code === 'EPERM'; }
const calls = [];
const journal = new OperationalJournal(`${stateDir}/journal.sqlite`);
const transport = { send: async request => {
  calls.push({
    text: request.text,
    media: (request.media ?? []).map(item => ({
      filename: item.filename,
      size: item.data?.byteLength,
      sha256: createHash('sha256').update(item.data).digest('hex'),
    })),
  });
  return { acceptance: 'accepted', providerMessageIds: ['fake-provider-gallery-id'] };
} };
const delivery = new ReceiptedDelivery(journal, new Map([['owner', { threadId: 'fake-owner-thread', transport }]]), {
  channel: 'photon', tenantId: 'fixture-tenant', projectFingerprint: '',
  ownerId: 'fixture-owner', accountId: 'fixture-account', lineKey: 'fixture-line',
});
const server = createChatHttpServer({
  send: async () => ({ acceptance: 'not_attempted' }),
  sendGallery: async () => ({ acceptance: 'not_attempted' }),
  sendGalleryBytes: request => delivery.sendGalleryBytes(request),
  receipt: async id => delivery.getReceipt(id),
  run: async () => ({ text: '' }),
  sessions: async () => ({}),
  health: async () => {
    const row = journal.getDispatch(expected.dispatch_id);
    return { directReadDenied, calls, row: row ? { status: row.status, payload_hash: row.payload_hash,
      provider_message_id: row.provider_message_id } : null };
  },
}, { bearerToken: expected.bearer, allowedTargets: new Set(['owner']), mediaRoot: stateDir });
const port = await listenLoopback(server, 0);
console.log(JSON.stringify({ ready: true, port }));
let closing = false;
process.on('SIGTERM', () => {
  if (closing) return;
  closing = true;
  server.close(() => { journal.close(); process.exit(0); });
});
