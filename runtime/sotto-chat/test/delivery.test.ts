import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { OperationalJournal } from '../src/journal/index.js';
import { ReceiptedDelivery } from '../src/delivery.js';
import type { Receipt, Send } from '../src/channels/types.js';
import type { RouteIdentity } from '../src/route-proof.js';
import { routeDigest } from '../src/route-proof.js';
import { createTelegramTransport } from '../src/channels/transport.js';

const routeIdentity: RouteIdentity = { channel: 'photon', tenantId: 'fixture-tenant',
  projectFingerprint: 'a'.repeat(64), ownerId: 'owner-1', accountId: 'account-1', lineKey: 'line-1' };

function fixture(send: (payload: Send) => Promise<Receipt>, identity: RouteIdentity = routeIdentity) {
  const dir = mkdtempSync(join(tmpdir(), 'sotto-delivery-'));
  const db = join(dir, 'journal.sqlite');
  const journal = new OperationalJournal(db);
  const targets = new Map([['owner', { threadId: 'owner-dm', transport: { send } }]]);
  return { dir, db, journal, targets, delivery: new ReceiptedDelivery(journal, targets, identity), close: () => { journal.close(); rmSync(dir, { recursive: true, force: true }); } };
}
const request = { dispatch_id: 'dispatch-1', target: 'owner', text: 'hello', attempt: 1 };

test('Telegram scheduled text commits one immutable payload across the transport boundary', async () => {
  const sent: Send[] = [];
  const telegramIdentity: RouteIdentity = { ...routeIdentity, channel: 'telegram', projectFingerprint: '' };
  const f = fixture(async payload => { sent.push(payload); return { acceptance: 'accepted', providerMessageIds: ['one-provider-message'] }; }, telegramIdentity);
  try {
    const longText = 'A *formatted* update with an emoji 🧭.\n'.repeat(130);
    assert.equal((await f.delivery.sendText({ ...request, text: longText })).acceptance, 'accepted');
    assert.deepEqual(sent, [{ threadId: 'owner-dm', text: longText }]);
  } finally { f.close(); }
});

test('accepted Telegram chunks recover all receipts after reopen without another provider post', async () => {
  const posts: string[] = [];
  const transport = createTelegramTransport({postMessage: async (_thread, message) => {
    posts.push(String((message as {raw: string}).raw));
    return {id: `telegram-part-${posts.length}`} as never;
  }});
  const identity: RouteIdentity = {...routeIdentity, channel: 'telegram', projectFingerprint: ''};
  const f = fixture(transport.send, identity);
  const input = {...request, text: 'Unicode 🧭 *literal*\n'.repeat(700)};
  let reopened: OperationalJournal | undefined;
  try {
    const receipt = await f.delivery.sendText(input);
    assert.equal(receipt.acceptance, 'accepted');
    assert.ok(posts.length > 1);
    assert.equal(posts.join(''), input.text);
    assert.equal(receipt.payload_hash, createHash('sha256').update(JSON.stringify({text: input.text})).digest('hex'));
    assert.equal(receipt.route_proof_version, 1);
    assert.equal(receipt.route_digest, routeDigest(identity, 'owner-dm'));
    assert.deepEqual(receipt.message_ids, posts.map((_, i) => `telegram-part-${i + 1}`));
    const count = posts.length;
    f.journal.close();
    reopened = new OperationalJournal(f.db);
    const delivery = new ReceiptedDelivery(reopened, f.targets, identity);
    assert.deepEqual(await delivery.sendText(input), receipt);
    assert.deepEqual(delivery.getReceipt(input.dispatch_id), {found: true, receipt});
    assert.equal(posts.length, count);
  } finally { reopened?.close(); rmSync(f.dir, {recursive: true, force: true}); }
});

test('partially accepted Telegram dispatch stays unknown after restart and never resends its prefix', async () => {
  let calls = 0;
  const transport = createTelegramTransport({postMessage: async () => {
    if (++calls === 1) return {id: 'accepted-prefix'} as never;
    throw new Error('next chunk response lost');
  }});
  const identity: RouteIdentity = {...routeIdentity, channel: 'telegram', projectFingerprint: ''};
  const f = fixture(transport.send, identity);
  const input = {...request, text: 'x'.repeat(9000)};
  let reopened: OperationalJournal | undefined;
  try {
    const partial = {acceptance: 'unknown', message_ids: ['accepted-prefix']};
    assert.deepEqual(await f.delivery.sendText(input), partial);
    assert.equal(calls, 2);
    assert.equal(f.journal.getDispatch(input.dispatch_id)?.provider_message_id, null);
    assert.equal(f.journal.getDispatch(input.dispatch_id)?.provider_message_ids, '["accepted-prefix"]');
    f.journal.close();
    reopened = new OperationalJournal(f.db);
    const delivery = new ReceiptedDelivery(reopened, f.targets, identity);
    assert.deepEqual(await delivery.sendText({...input, attempt: 2}), partial);
    assert.deepEqual(delivery.getReceipt(input.dispatch_id), {found: true, receipt: partial});
    assert.equal(reopened.getDispatch(input.dispatch_id)?.status, 'unknown');
    assert.equal(reopened.getDispatch(input.dispatch_id)?.provider_message_ids, '["accepted-prefix"]');
    assert.throws(() => reopened!.retryScheduledText(input.dispatch_id), /earlier provider outcome is unresolved/);
    assert.equal(calls, 2);
    assert.deepEqual(reopened.getAttempts(input.dispatch_id).map(attempt => attempt.number), [1]);
  } finally { reopened?.close(); rmSync(f.dir, {recursive: true, force: true}); }
});

test('duplicate in-flight and completed requests do not send twice', async () => {
  let calls = 0;
  let resolve!: (receipt: Receipt) => void;
  const f = fixture(() => { calls++; return new Promise(r => { resolve = r; }); });
  try {
    const pending = f.delivery.sendText(request);
    assert.deepEqual(await f.delivery.sendText(request), { acceptance: 'unknown' });
    assert.equal(calls, 1);
    resolve({ acceptance: 'accepted', providerMessageIds: ['p1'] });
    assert.equal((await pending).acceptance, 'accepted');
    assert.equal((await f.delivery.sendText(request)).message_id, 'p1');
    assert.equal(calls, 1);
    await assert.rejects(() => f.delivery.sendText({ ...request, text: 'different content' }), /identity conflict/);
  } finally { f.close(); }
});

test('unknown text stays held across a higher attempt and journal reopen', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return calls === 1 ? { acceptance: 'unknown', providerMessageIds: [] } : { acceptance: 'accepted', providerMessageIds: ['p2'] }; });
  let reopened: OperationalJournal | undefined;
  try {
    assert.equal((await f.delivery.sendText(request)).acceptance, 'unknown');
    assert.equal((await f.delivery.sendText(request)).acceptance, 'unknown');
    assert.equal(calls, 1);
    f.journal.close();
    reopened = new OperationalJournal(f.db);
    const delivery = new ReceiptedDelivery(reopened, f.targets, routeIdentity);
    assert.equal((await delivery.sendText({ ...request, attempt: 2 })).acceptance, 'unknown');
    assert.equal(calls, 1);
    assert.deepEqual(reopened.getAttempts(request.dispatch_id).map(row => row.number), [1]);
  } finally { reopened?.close(); rmSync(f.dir, { recursive: true, force: true }); }
});

test('receiver attempt gaps survive crashes before HTTP contact without fabricating earlier sends', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return { acceptance: 'rejected', providerMessageIds: [] }; });
  try {
    await f.delivery.sendText({ ...request, attempt: 2 }); // Receiver charged attempt 1, then crashed.
    await f.delivery.sendText({ ...request, attempt: 2 }); // Same request is idempotent.
    await f.delivery.sendText({ ...request, attempt: 4 }); // Another charge happened before HTTP.
    await f.delivery.sendText({ ...request, attempt: 3 }); // A delayed older request cannot resend.
    assert.equal(calls, 2);
    assert.deepEqual(f.journal.getAttempts(request.dispatch_id).map(row => row.number), [2, 4]);
    await assert.rejects(f.delivery.sendText({ ...request, attempt: 97 }), /attempt number/);
    assert.equal(calls, 2);
  } finally { f.close(); }
});

test('historical unknown then rejected text remains held after reopen', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return { acceptance: 'rejected', providerMessageIds: [] }; });
  let reopened: OperationalJournal | undefined;
  try {
    assert.equal((await f.delivery.sendText(request)).acceptance, 'rejected');
    assert.equal((await f.delivery.sendText({ ...request, attempt: 2 })).acceptance, 'rejected');
    assert.equal(calls, 2);
    f.journal.close();
    // Reproduce a pre-fix journal: the latest attempt was definitively rejected,
    // but the earlier contacted attempt still has an unknown provider outcome.
    const db = new DatabaseSync(f.db);
    try { db.prepare("UPDATE dispatch_attempts SET status='unknown' WHERE dispatch_id=? AND number=1")
      .run(request.dispatch_id); } finally { db.close(); }
    reopened = new OperationalJournal(f.db);
    const delivery = new ReceiptedDelivery(reopened, f.targets, routeIdentity);
    assert.deepEqual(delivery.getReceipt(request.dispatch_id),
      { found: true, receipt: { acceptance: 'unknown' } });
    assert.equal((await delivery.sendText({ ...request, attempt: 3 })).acceptance, 'unknown');
    assert.throws(() => reopened!.retryScheduledText(request.dispatch_id), /earlier provider outcome is unresolved/);
    assert.equal(calls, 2);
    assert.deepEqual(reopened.getAttempts(request.dispatch_id).map(row => row.number), [1, 2]);
  } finally { reopened?.close(); rmSync(f.dir, { recursive: true, force: true }); }
});

test('gallery ambiguity survives restart without a second transport send', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; throw new Error('response lost after contact'); });
  const paths = [0, 1, 2, 3].map(i => { const path = join(f.dir, `${i}.png`); writeFileSync(path, 'image fixture'); return path; });
  const input = { dispatch_id: 'gallery-1', target: 'owner', summary: 'brief', paths };
  let reopened: OperationalJournal | undefined;
  try {
    assert.equal((await f.delivery.sendGallery(input)).acceptance, 'unknown');
    f.journal.close();
    reopened = new OperationalJournal(f.db);
    const delivery = new ReceiptedDelivery(reopened, f.targets, routeIdentity);
    assert.equal((await delivery.sendGallery(input)).acceptance, 'unknown');
    assert.deepEqual(delivery.getReceipt('gallery-1'), { found: true, receipt: { acceptance: 'unknown' } });
    assert.equal(calls, 1);
  } finally { reopened?.close(); rmSync(f.dir, { recursive: true, force: true }); }
});

test('byte gallery commits the exact ordered image bytes and Unicode summary', async () => {
  const f = fixture(async () => ({ acceptance: 'accepted', providerMessageIds: ['group-1'] }));
  const images = [1, 2, 3, 4].map(index => ({ name: `0${index}.png`,
    bytes: Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, index]) }));
  const summary = 'Résumé 📨 / 東京';
  try {
    assert.equal((await f.delivery.sendGalleryBytes({ dispatch_id: 'byte-gallery', target: 'owner', summary, images })).acceptance, 'accepted');
    const expected = JSON.stringify({ summary, images: images.map(image => createHash('sha256').update(image.bytes).digest('hex')) });
    assert.equal(f.journal.getDispatch('byte-gallery')?.payload_hash,
      createHash('sha256').update(expected).digest('hex'));
    assert.equal((await f.delivery.sendGalleryBytes({ dispatch_id: 'byte-gallery', target: 'owner', summary, images })).acceptance, 'accepted');
    await assert.rejects(f.delivery.sendGalleryBytes({ dispatch_id: 'byte-gallery', target: 'owner', summary,
      images: [{ ...images[0], bytes: Buffer.from('changed') }, ...images.slice(1)] }), /identity conflict/);
  } finally { f.close(); }
});

test('byte gallery freezes caller buffers before journal proof and provider contact', async () => {
  let sentBytes: Buffer | undefined;
  let resolve!: (receipt: Receipt) => void;
  const f = fixture(async payload => {
    sentBytes = payload.media?.[0]?.data as Buffer | undefined;
    return new Promise<Receipt>(r => { resolve = r; });
  });
  const images = [1, 2, 3, 4].map(index => ({ name: `0${index}.png`,
    bytes: Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, index]) }));
  const original = Buffer.from(images[0].bytes);
  try {
    const pending = f.delivery.sendGalleryBytes({ dispatch_id: 'frozen-gallery', target: 'owner', summary: 'brief', images });
    images[0].bytes[8] = 99;
    assert.deepEqual(sentBytes, original);
    resolve({ acceptance: 'accepted', providerMessageIds: ['group-1'] });
    assert.equal((await pending).acceptance, 'accepted');
    const expected = JSON.stringify({ summary: 'brief', images: [original, ...images.slice(1).map(image => image.bytes)]
      .map(bytes => createHash('sha256').update(bytes).digest('hex')) });
    assert.equal(f.journal.getDispatch('frozen-gallery')?.payload_hash, createHash('sha256').update(expected).digest('hex'));
  } finally { f.close(); }
});

test('forbidden destination and missing provider ID cannot claim acceptance', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return { acceptance: 'accepted', providerMessageIds: [] }; });
  try {
    assert.equal((await f.delivery.sendText({ ...request, target: 'outsider' })).acceptance, 'not_attempted');
    assert.equal(calls, 0);
    assert.equal((await f.delivery.sendText(request)).acceptance, 'unknown');
    assert.equal(calls, 1);
  } finally { f.close(); }
});

test('accepted route proof survives restart and a changed destination cannot reuse the dispatch', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return { acceptance: 'accepted', providerMessageIds: ['provider-1'] }; });
  let reopened: OperationalJournal | undefined;
  try {
    const accepted = await f.delivery.sendText(request);
    const originalDigest = routeDigest(routeIdentity, 'owner-dm');
    assert.equal(accepted.route_proof_version, 1);
    assert.equal(accepted.route_digest, originalDigest);
    assert.equal(accepted.payload_hash, createHash('sha256').update(JSON.stringify({ text: request.text })).digest('hex'));
    assert.equal(f.delivery.getReceipt(request.dispatch_id).receipt?.route_digest, originalDigest);
    f.journal.close();
    reopened = new OperationalJournal(f.db);
    const changed = new Map([['owner', { threadId: 'different-owner-dm',
      transport: { send: async () => { calls++; return { acceptance: 'accepted' as const, providerMessageIds: ['wrong'] }; } } }]]);
    const delivery = new ReceiptedDelivery(reopened, changed, routeIdentity);
    assert.equal(delivery.getReceipt(request.dispatch_id).receipt?.route_digest, originalDigest);
    await assert.rejects(delivery.sendText(request), /route identity conflict/);
    assert.equal(calls, 1);
  } finally { reopened?.close(); rmSync(f.dir, { recursive: true, force: true }); }
});

test('accepted gallery proof binds exact image bytes and Unicode summary through retention', async () => {
  const f = fixture(async () => ({ acceptance: 'accepted', providerMessageIds: ['gallery-parent', 'image-1'] }));
  try {
    const paths = [0, 1, 2, 3].map(index => {
      const path = join(f.dir, `gallery-${index}.png`);
      writeFileSync(path, `image-${index}`);
      return path;
    });
    const request = { dispatch_id: 'unicode-gallery', target: 'owner',
      summary: 'Résumé 📨 / 東京', paths };
    const accepted = await f.delivery.sendGallery(request);
    assert.equal(accepted.route_digest, routeDigest(routeIdentity, 'owner-dm'));
    // Independently computed with Python's UTF-8 compact JSON over the four file hashes.
    assert.equal(accepted.payload_hash, '912ec2e24f8a0af4bcf90eb36fe264712422bfe0475abfbf1f418b14ea809210');
    f.journal.pruneTerminal(Date.now() + 365 * 24 * 60 * 60 * 1000);
    assert.equal(f.delivery.getReceipt('unicode-gallery').receipt?.payload_hash, accepted.payload_hash);
  } finally { f.close(); }
});

test('route is captured before contact even if the destination map mutates during send', async () => {
  let release!: (value: Receipt) => void;
  const f = fixture(() => new Promise(resolve => { release = resolve; }));
  try {
    const inFlight = f.delivery.sendText(request);
    f.targets.set('owner', { threadId: 'new-owner-dm', transport: { send: async () =>
      ({ acceptance: 'accepted', providerMessageIds: ['wrong'] }) } });
    release({ acceptance: 'accepted', providerMessageIds: ['old-thread-id'] });
    assert.equal((await inFlight).route_digest, routeDigest(routeIdentity, 'owner-dm'));
    await assert.rejects(f.delivery.sendText(request), /route identity conflict/);
  } finally { f.close(); }
});

test('unknown route is persisted but never exposed as accepted proof or retried after route change', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return { acceptance: 'unknown', providerMessageIds: [] }; });
  try {
    assert.deepEqual(await f.delivery.sendText(request), { acceptance: 'unknown' });
    assert.equal(f.journal.getDispatch(request.dispatch_id)?.route_digest, routeDigest(routeIdentity, 'owner-dm'));
    f.targets.set('owner', { threadId: 'changed', transport: { send: async () => {
      calls++; return { acceptance: 'accepted', providerMessageIds: ['wrong'] };
    } } });
    await assert.rejects(f.delivery.sendText({ ...request, attempt: 2 }), /route identity conflict/);
    assert.equal(calls, 1);
  } finally { f.close(); }
});

test('legacy and corrupt route rows never acquire proof from the current map', async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return { acceptance: 'accepted', providerMessageIds: ['provider-1'] }; });
  try {
    await f.delivery.sendText(request);
    f.journal.db.prepare('UPDATE dispatches SET route_digest=NULL WHERE id=?').run(request.dispatch_id);
    assert.deepEqual(f.delivery.getReceipt(request.dispatch_id).receipt,
      { acceptance: 'accepted', message_id: 'provider-1', message_ids: ['provider-1'] });
    assert.equal((await f.delivery.sendText(request)).route_digest, undefined);
    assert.equal(calls, 1);
    f.journal.db.prepare('UPDATE dispatches SET route_digest=? WHERE id=?').run('invalid', request.dispatch_id);
    assert.throws(() => f.delivery.getReceipt(request.dispatch_id), /route proof is invalid/);

    f.journal.prepareDispatch({ dispatchId: 'legacy-prepared', kind: 'scheduled_text',
      destination: 'owner', payload: JSON.stringify({ text: 'old' }) });
    await assert.rejects(f.delivery.sendText({ dispatch_id: 'legacy-prepared', target: 'owner',
      text: 'old', attempt: 1 }), /legacy dispatch route proof unavailable/);
    assert.equal(calls, 1);
    f.journal.prepareDispatch({ dispatchId: 'legacy-unknown', kind: 'scheduled_text',
      destination: 'owner', payload: JSON.stringify({ text: 'unknown' }) });
    f.journal.beginDispatch('legacy-unknown');
    f.journal.settleDispatch('legacy-unknown', 1, 'unknown');
    assert.equal((await f.delivery.sendText({ dispatch_id: 'legacy-unknown', target: 'owner',
      text: 'unknown', attempt: 2 })).route_digest, undefined);
    assert.equal(calls, 1);
  } finally { f.close(); }
});
