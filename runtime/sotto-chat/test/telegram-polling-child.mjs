// Isolated pinned-SDK contract probe. Every fetch is synthetic and unexpected methods fail.
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';

const dist = process.env.SOTTO_CHAT_DIST ?? fileURLToPath(new URL('../src/', import.meta.url));
const { startOwnerGateway } = await import(pathToFileURL(join(dist, 'channels/gateway.js')));
const { OperationalJournal } = await import(pathToFileURL(join(dist, 'journal/index.js')));
const root = await mkdtemp(join(tmpdir(), 'telegram-polling-contract-'));
const originalFetch = globalThis.fetch;
const token = '123456:offline-fixture';
const manifest = { channel: 'telegram', accountId: 'fixture-bot', ownerId: '42', lineKey: 'primary',
  providerThreadId: 'telegram:42', credentials: { telegramToken: token,
    telegramLinkFile: { bot_token: token, allowed_user: 42 } } };
const update = { update_id: 100, message: { message_id: 23, date: 1790726400,
  from: { id: 42, is_bot: false, first_name: 'Fixture' },
  chat: { id: 42, type: 'private', first_name: 'Fixture' }, text: 'Owner fixture request' } };
let journal;
let gateway;
let admitted = [];
let rejectAdmission = false;
let emitted = false;
let advanced;
let advanceResolve;
let pollMode = 'emit';
function resetAdvance() { advanced = new Promise(resolve => { advanceResolve = resolve; }); }
const deadline = async promise => {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('offline polling contract timed out')), 15000); })]); }
  finally { clearTimeout(timer); }
};
globalThis.fetch = async (input, init) => {
  const request = new Request(input, init);
  const url = new URL(request.url);
  assert.equal(url.origin, 'https://api.telegram.org');
  assert.ok(url.pathname.startsWith(`/bot${token}/`));
  const method = url.pathname.split('/').at(-1);
  const body = request.method === 'POST' ? await request.json() : {};
  const answer = result => Response.json({ ok: true, result });
  if (method === 'getMe') return answer({ id: 123456, is_bot: true, first_name: 'Fixture bot', username: 'fixture_bot' });
  assert.equal(method, 'getUpdates', 'no send, typing, webhook mutation or unexpected network method');
  if (pollMode === 'emit' && !emitted) { emitted = true; return answer([update]); }
  if (body.offset === 101) {
    assert.equal(journal.listPendingInbound().length, rejectAdmission ? 0 : 1,
      'admission state must already be durable when the next offset is submitted');
    if (rejectAdmission) {
      const checkpoint = journal.db.prepare("SELECT value FROM sdk_kv WHERE key LIKE 'telegram:polling:%'").get();
      assert.equal(JSON.parse(checkpoint.value).pending[0].update.update_id, 100,
        'failed admission must be stored before submitting the next offset');
    }
    advanceResolve();
  }
  return new Promise((_, reject) => {
    const abort = () => reject(new DOMException('Aborted', 'AbortError'));
    if (request.signal.aborted) abort();
    else request.signal.addEventListener('abort', abort, { once: true });
  });
};
async function open(path) {
  journal = new OperationalJournal(path);
  gateway = await startOwnerGateway({ manifest, database: journal.db, application: {
    receive(event) {
      if (rejectAdmission) throw new Error('synthetic admission unavailable');
      const result = journal.admitInbound({ eventId: event.eventId, accountKey: event.accountId,
        lineKey: manifest.lineKey, threadKey: manifest.providerThreadId, channel: event.channel,
        sender: event.senderId, payload: JSON.stringify(event) });
      admitted.push(result.admitted);
      return Promise.resolve(result);
    },
    async close() {},
  } });
}
async function close() {
  if (gateway) { await gateway.close(); assert.equal(gateway.providerStopped, true); gateway = undefined; }
  if (journal) { journal.close(); journal = undefined; }
}
try {
  const firstPath = join(root, 'admitted.sqlite3');
  resetAdvance();
  await open(firstPath);
  await deadline(advanced);
  assert.deepEqual(admitted, [true]);
  assert.equal(journal.listPendingInbound().length, 1);
  await close();
  // Inject a duplicate after restart to test journal deduplication. This is not a claim
  // that Telegram naturally replays an update already confirmed by offset 101.
  emitted = false;
  resetAdvance();
  await open(firstPath);
  await deadline(advanced);
  assert.deepEqual(admitted, [true, false]);
  assert.equal(journal.listPendingInbound().length, 1);
  await close();

  const failedPath = join(root, 'failed.sqlite3');
  rejectAdmission = true;
  emitted = false;
  resetAdvance();
  await open(failedPath);
  await deadline(advanced);
  assert.equal(journal.listPendingInbound().length, 0);
  const checkpoint = journal.db.prepare("SELECT value FROM sdk_kv WHERE key LIKE 'telegram:polling:%'").get();
  assert.ok(checkpoint, 'failed admission must be durable before advancing the provider offset');
  const saved = JSON.parse(checkpoint.value);
  assert.equal(saved.offset, 101);
  assert.equal(saved.pending[0].update.update_id, 100);
  await close();
  // Wait for the pinned adapter's recorded retry deadline, without changing its clock.
  const delay = Math.max(0, saved.pending[0].retryAt - Date.now() + 30);
  assert.ok(delay <= 3000);
  await new Promise(resolve => setTimeout(resolve, delay));
  rejectAdmission = false;
  pollMode = 'block';
  resetAdvance();
  await open(failedPath);
  await deadline(advanced);
  assert.equal(journal.listPendingInbound().length, 1);
  assert.deepEqual(admitted, [true, false, true]);
  assert.equal(journal.db.prepare("SELECT COUNT(*) AS n FROM sdk_kv WHERE key LIKE 'telegram:polling:%'").get().n, 0);
  await close();
  console.log('PASS pinned Telegram polling: admission before offset, injected duplicate dedup, durable failed-admission retry across restart; synthetic fetch only');
} finally {
  try { await close(); }
  finally { globalThis.fetch = originalFetch; await rm(root, { recursive: true, force: true }); }
}
