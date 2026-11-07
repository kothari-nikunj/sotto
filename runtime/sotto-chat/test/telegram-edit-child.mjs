// Isolated pinned-SDK probe: an owner edit (edited_message, same message_id) must be denied and
// acknowledged, never admitted as a conflicting event or kept in the persisted retry list.
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';

const dist = process.env.SOTTO_CHAT_DIST ?? fileURLToPath(new URL('../src/', import.meta.url));
const { startOwnerGateway } = await import(pathToFileURL(join(dist, 'channels/gateway.js')));
const { OperationalJournal } = await import(pathToFileURL(join(dist, 'journal/index.js')));
const root = await mkdtemp(join(tmpdir(), 'telegram-edit-contract-'));
const originalFetch = globalThis.fetch;
const token = '123456:offline-fixture';
const manifest = { channel: 'telegram', accountId: 'fixture-bot', ownerId: '42', lineKey: 'primary',
  providerThreadId: 'telegram:42', credentials: { telegramToken: token, telegramLinkFile: { bot_token: token, allowed_user: 42 } } };
const original = { message_id: 23, date: 1790726400, from: { id: 42, is_bot: false, first_name: 'Fixture' },
  chat: { id: 42, type: 'private', first_name: 'Fixture' }, text: 'Owner request' };
const updates = [{ update_id: 100, message: original },
  { update_id: 101, edited_message: { ...original, text: 'Owner request (typo fixed)', edit_date: 1790726460 } }];
let emitted = false, receives = 0, admissionErrors = 0, drained;
const confirmed = new Promise(resolve => { drained = resolve; });
globalThis.fetch = async (input, init) => {
  const request = new Request(input, init);
  const method = new URL(request.url).pathname.split('/').at(-1);
  const answer = result => Response.json({ ok: true, result });
  if (method === 'getMe') return answer({ id: 123456, is_bot: true, first_name: 'Sotto', username: 'sotto_bot' });
  if (method !== 'getUpdates') throw new Error(`unexpected Telegram method ${method}`);
  const body = await request.clone().text();
  const offset = Number(new URLSearchParams(body).get('offset') ?? JSON.parse(body || '{}').offset ?? new URL(request.url).searchParams.get('offset'));
  if (!emitted) { emitted = true; return answer(updates); }
  if (offset >= 102) drained();
  await new Promise(resolve => setTimeout(resolve, 100));
  return answer([]);
};
let journal, gateway;
try {
  journal = new OperationalJournal(join(root, 'journal.sqlite3'));
  gateway = await startOwnerGateway({ manifest, database: journal.db, application: {
    receive(event) {
      receives++;
      try {
        return Promise.resolve(journal.admitInbound({ eventId: event.eventId, accountKey: event.accountId, lineKey: 'primary',
          threadKey: 'telegram:42', channel: event.channel, sender: event.senderId, payload: JSON.stringify(event) }));
      } catch (error) { admissionErrors++; throw error; }
    }, async close() {} } });
  const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('offset never advanced')), 8000));
  await Promise.race([confirmed, timeout]).catch(() => undefined);
  // Allow the adapter's checkpoint write for the batch to settle.
  await new Promise(resolve => setTimeout(resolve, 300));
  const checkpoint = journal.db.prepare("SELECT value FROM sdk_kv WHERE key LIKE 'telegram:polling:%'").get();
  const pending = checkpoint ? JSON.parse(checkpoint.value).pending ?? [] : [];
  console.log(`receives=${receives} admissionErrors=${admissionErrors} admitted=${journal.listPendingInbound().length} pending=${JSON.stringify(pending.map(item => item.update.update_id))}`);
  assert.equal(JSON.parse(journal.listPendingInbound()[0].payload).text, 'Owner request');
} finally {
  try { if (gateway) await gateway.close(); if (journal) journal.close(); }
  finally { globalThis.fetch = originalFetch; await rm(root, { recursive: true, force: true }); }
}
