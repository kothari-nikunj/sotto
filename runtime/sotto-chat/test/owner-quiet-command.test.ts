import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { ConversationCoordinator } from '../src/conversation.js';
import { OperationalJournal, parseOwnerQuietCommand } from '../src/journal/index.js';
import { createOwnerQuietClient } from '../src/tools/owner-quiet-client.js';

const identity = { tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread',
  project_fingerprint: 'a'.repeat(64) };
const ownerPolicy = { channel: 'photon' as const, accountId: 'account', ownerId: 'owner' };
const context = { threadId: 'thread', destination: 'owner-destination', lineKey: 'line' };
const inbound = (eventId: string, text: string) => ({ channel: 'photon' as const,
  accountId: 'account', senderId: 'owner', isDirect: true, eventId, text });
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const endpoint = 'http://127.0.0.1:8080/mcp';

test('literal quiet parser rejects ambiguous and out-of-bounds commands before any effect', () => {
  assert.deepEqual(parseOwnerQuietCommand('/quiet for 24h'), { mode: 'for' });
  assert.deepEqual(parseOwnerQuietCommand('/quiet for 1440m'), { mode: 'for' });
  assert.deepEqual(parseOwnerQuietCommand('/quiet off'), { mode: 'off' });
  for (const text of ['/quiet for 0h', '/quiet for 25h', '/quiet for 1441m', '/quiet for 01h',
    '/quiet for 1d', '/quiet for 1 h', '/quiet off ', '/quiet on']) {
    assert.equal(parseOwnerQuietCommand(text), null);
  }
});

test('one admitted quiet pause and off write through private receiver with exact saved receipts', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-owner-quiet-'));
  const journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  try {
    const operations: string[] = [];
    const client = createOwnerQuietClient({ bridgeUrl: endpoint, bearer: 'c'.repeat(64), identity,
      fetch: async (_url, init) => {
        const body = JSON.parse(init!.body as string);
        operations.push(body.operation);
        assert.equal(body.identity.owner_id, 'owner');
        assert.equal(body.text_hash.length, 64);
        if (body.operation === 'prepare') {
          assert.equal(body.zone, null);
          return reply({ version: 1, status: 'prepared', zone: 'America/Los_Angeles' });
        }
        assert.equal(body.zone, 'America/Los_Angeles');
        return reply({ version: 1, status: 'committed', action_id: body.action_id,
          event_id: body.event_id, payload_hash: body.payload_hash, zone: body.zone,
          snooze_until: body.text === '/quiet off' ? '' : '2026-10-09T09:00:00.000+00:00' });
      } });
    const sent: string[] = [];
    const service = new ConversationCoordinator({ journal, ownerPolicy, ownerQuiet: client,
      model: { generate: async () => { throw new Error('literal quiet command reached model'); } },
      postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: `send-${sent.length}` }; } });
    await service.receive(inbound('pause', '/quiet for 2h'), context);
    await service.receive(inbound('off', '/quiet off'), context);
    assert.equal((await service.receive({ ...inbound('forged', '/quiet for 1h'), senderId: 'other' }, context)).status,
      'denied');
    await service.receive(inbound('malformed', '/quiet for 25h'), context);
    assert.deepEqual(operations, ['prepare', 'write', 'prepare', 'write']);
    assert.match(sent[0]!, /Recorded your nudge pause until 2026-10-09T09:00:00\.000\+00:00 \(America\/Los_Angeles\)/);
    assert.match(sent[1]!, /Recorded your request to resume nudges. Ordinary quiet hours remain in effect/);
    assert.match(sent[2]!, /Use \/quiet for/);
    assert.equal((await service.receive(inbound('pause', '/quiet for 2h'), context)).status, 'duplicate');
    assert.equal(journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_quiet_admitted' AND status='succeeded'").get()!.n, 2);
    assert.throws(() => journal.recordActionIntent({ actionId: 'model', runId: 'model',
      kind: 'owner_quiet_admitted', payloadHash: 'model' }), /admitted-event proof/);
    journal.pruneTerminal(Date.now() + 1000);
    assert.equal(journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_quiet_admitted'").get()!.n, 2);
  } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
});

test('lost quiet receipt stays unknown while safe chat proceeds, then exact inspect alone acknowledges it', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-owner-quiet-recover-'));
  const path = join(folder, 'operations.sqlite');
  let journal = new OperationalJournal(path);
  let writes = 0;
  let proved = false;
  const operations: string[] = [];
  const client = createOwnerQuietClient({ bridgeUrl: endpoint, bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      operations.push(body.operation);
      if (body.operation === 'prepare') return reply({ version: 1, status: 'prepared', zone: 'UTC' });
      if (body.operation === 'write') { writes++; throw new Error('response lost after effect'); }
      if (!proved) return reply({ version: 1, status: 'not_found' });
      return reply({ version: 1, status: 'committed', action_id: body.action_id,
        event_id: body.event_id, payload_hash: body.payload_hash,
        zone: body.zone, snooze_until: '2026-10-09T09:00:00.000+00:00' });
    } });
  try {
    const noticeAttempts: string[] = [];
    const original = new ConversationCoordinator({ journal, ownerPolicy, ownerQuiet: client,
      model: { generate: async () => { throw new Error('unknown command reached model'); } },
      postPrepared: async request => {noticeAttempts.push(request.text); throw new Error('unknown command was acknowledged'); } });
    assert.equal((await original.receive(inbound('pause-unknown', '/quiet for 2h'), context)).status, 'held');
    const action = journal.db.prepare("SELECT id,status FROM actions WHERE kind='owner_quiet_admitted'").get()!;
    assert.equal(action.status, 'unknown');
    assert.equal(noticeAttempts.length, 1);
    assert.match(noticeAttempts[0]!, /^I couldn’t confirm whether the quiet setting happened\./);
    await original.close();
    journal.close();
    journal = new OperationalJournal(path);
    journal.recoverInterrupted();
    const sent: string[] = [];
    const generated: string[] = [];
    const recovered = new ConversationCoordinator({ journal, ownerPolicy, ownerQuiet: client,
      model: { generate: async request => { generated.push(request.text); return 'safe answer'; } },
      postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: `send-${sent.length}` }; } });
    assert.equal((await recovered.recover())[0]?.status, 'held');
    assert.equal((await recovered.receive(inbound('safe', 'What is next?'), context)).status, 'accepted');
    assert.deepEqual(generated, ['What is next?']);
    assert.deepEqual(sent, ['safe answer']);
    assert.equal(journal.getAction(action.id as string)?.status, 'unknown');
    proved = true;
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE id=? AND status='unknown'").run(action.id);
    assert.equal((await recovered.recover())[0]?.status, 'accepted');
    assert.equal(journal.getAction(action.id as string)?.status, 'succeeded');
    assert.match(sent[1]!, /Recorded your nudge pause until 2026-10-09T09:00:00\.000\+00:00 \(UTC\)/);
    assert.equal(writes, 1);
    assert.deepEqual(operations, ['prepare', 'write', 'inspect', 'inspect']);
    assert.equal(noticeAttempts.length, 1, 'the contacted unknown notice is never replayed');
    const afterProof = sent.length; await recovered.recover(); assert.equal(sent.length, afterProof);
    await recovered.close();
  } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
});

test('a later quiet off waits behind an unknown pause while unrelated chat remains available', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-owner-quiet-order-'));
  const journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let proved = false;
  const writes: string[] = [];
  const client = createOwnerQuietClient({ bridgeUrl: endpoint, bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      if (body.operation === 'prepare') return reply({ version: 1, status: 'prepared', zone: 'UTC' });
      if (body.operation === 'inspect') return proved
        ? reply({ version: 1, status: 'committed', action_id: body.action_id,
          event_id: body.event_id, payload_hash: body.payload_hash, zone: body.zone,
          snooze_until: '2026-10-09T09:00:00.000+00:00' })
        : reply({ version: 1, status: 'not_found' });
      writes.push(body.text);
      if (body.text !== '/quiet off') throw new Error('first response lost after effect');
      return reply({ version: 1, status: 'committed', action_id: body.action_id,
        event_id: body.event_id, payload_hash: body.payload_hash, zone: body.zone,
        snooze_until: '' });
    } });
  try {
    const sent: string[] = [];
    const generated: string[] = [];
    const service = new ConversationCoordinator({ journal, ownerPolicy, ownerQuiet: client,
      model: { generate: async request => { generated.push(request.text); return 'safe answer'; } },
      postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: `send-${sent.length}` }; } });
    assert.equal((await service.receive(inbound('pause', '/quiet for 2h'), context)).status, 'held');
    await service.receive(inbound('off', '/quiet off'), context);
    assert.deepEqual(writes, ['/quiet for 2h']);
    assert.equal(journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_quiet_admitted'").get()!.n, 1);
    await service.receive(inbound('safe', 'What is next?'), context);
    assert.deepEqual(generated, ['What is next?']);
    assert.equal(sent.length, 2);
    assert.match(sent[0]!, /^I couldn’t confirm whether the quiet setting happened\./, 'one immediate notice precedes fresh safe chat');
    assert.equal(sent[1], 'safe answer');
    assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_quiet_admitted'").get()!.status, 'unknown');
    proved = true;
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_quiet_admitted' AND status='unknown'").run();
    await service.recover();
    assert.deepEqual(writes, ['/quiet for 2h', '/quiet off']);
    assert.equal(journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_quiet_admitted' AND status='succeeded'").get()!.n, 2);
    assert.match(sent[2]!, /Recorded your nudge pause/);
    assert.match(sent[3]!, /Recorded your request to resume nudges/);
    assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the quiet setting happened.')).length, 1);
    const afterProof = sent.length; await service.recover(); assert.equal(sent.length, afterProof);
    await service.close();
  } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
});

test('quiet client rejects a committed response with different saved action or zone', async () => {
  const client = createOwnerQuietClient({ bridgeUrl: endpoint, bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      return reply({ version: 1, status: 'committed', action_id: '0'.repeat(64),
        event_id: body.event_id, payload_hash: body.payload_hash,
        zone: 'UTC', snooze_until: '2026-10-09T09:00:00.000+00:00' });
    } });
  await assert.rejects(client.call('write', { actionId: 'a'.repeat(64), eventId: 'b'.repeat(64),
    payloadHash: 'c'.repeat(64), text: '/quiet for 2h', admittedAtMs: 1791540000000,
    providerThreadId: 'thread', zone: 'UTC' }), /unavailable/);
});

test('quiet client rejects naive, malformed calendar, precision, and off-mismatched receipts', async () => {
  const request = { actionId: 'a'.repeat(64), eventId: 'b'.repeat(64), payloadHash: 'c'.repeat(64),
    text: '/quiet for 2h', admittedAtMs: 1791540000000, providerThreadId: 'thread', zone: 'UTC' };
  const invalid = ['2026-10-09T09:00', '2026-10-09T09:00:00.000',
    '2026-02-29T09:00:00.000+00:00', '2026-04-31T09:00:00.000+00:00',
    '2026-13-01T09:00:00.000+00:00', '0000-01-01T09:00:00.000+00:00',
    '2026-10-09T24:00:00.000+00:00', '2026-10-09T09:00:60.000+00:00',
    '2026-10-09T09:00:00.000+24:00', '2026-10-09T09:00:00.000+00:60',
    '2026-10-09T09:00:00+00:00', '2026-10-09T09:00:00.0000+00:00', '2026-10-09T09:00:00.000Z', ''];
  for (const [until, text] of [...invalid.map(until => [until, request.text]),
    ['2026-10-09T09:00:00.000+00:00', '/quiet off']]) {
    const client = createOwnerQuietClient({ bridgeUrl: endpoint, bearer: 'c'.repeat(64), identity,
      fetch: async () => reply({ version: 1, status: 'committed', action_id: request.actionId,
        event_id: request.eventId, payload_hash: request.payloadHash, zone: request.zone, snooze_until: until }) });
    await assert.rejects(client.call('write', { ...request, text: text! }), /unavailable/, `${text}: ${until}`);
  }
});

test('both fall-back instants retain exact offsets, milliseconds, and receipt bindings across journal reopen', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-owner-quiet-fold-'));
  const path = join(folder, 'operations.sqlite');
  let journal = new OperationalJournal(path);
  const untils = ['2026-11-01T01:30:00.123-07:00', '2026-11-01T01:30:00.123-08:00'];
  let writes = 0;
  const receipts: Record<string, unknown>[] = [];
  const sent: string[] = [];
  const client = createOwnerQuietClient({ bridgeUrl: endpoint, bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      if (body.operation === 'prepare') return reply({ version: 1, status: 'prepared', zone: 'America/Los_Angeles' });
      const receipt = { version: 1, status: 'committed', action_id: body.action_id,
        event_id: body.event_id, payload_hash: body.payload_hash, zone: body.zone, snooze_until: untils[writes++] };
      receipts.push(receipt);
      return reply(receipt);
    } });
  try {
    const service = new ConversationCoordinator({ journal, ownerPolicy, ownerQuiet: client,
      model: { generate: async () => { throw new Error('quiet command reached model'); } },
      postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: `send-${sent.length}` }; } });
    for (let i = 0; i < untils.length; i++) {
      await service.receive(inbound(`fold-${i}`, `/quiet for ${i + 1}h`), context);
      assert.ok(sent[i]!.includes(`Recorded your nudge pause until ${untils[i]} (America/Los_Angeles)`));
    }
    journal.close();
    journal = new OperationalJournal(path);
    const { createHash } = await import('node:crypto');
    for (const receipt of receipts) {
      const action = journal.getAction(receipt.action_id as string)!;
      const metadata = JSON.parse(action.result_json as string);
      assert.equal(metadata.snooze_until, receipt.snooze_until);
      assert.equal(metadata.owner_zone, receipt.zone);
      assert.equal(action.source_event_id, receipt.event_id);
      assert.equal(action.source_payload_hash, receipt.payload_hash);
      assert.equal(metadata.receipt_hash, createHash('sha256').update(JSON.stringify([
        'sotto.owner-quiet-receipt.v1', receipt.action_id, receipt.event_id, receipt.payload_hash,
        receipt.zone, receipt.snooze_until])).digest('hex'));
      assert.throws(() => journal.settleExplicitOwnerQuietIntent(action.id as string, 'a'.repeat(64),
        receipt.zone as string, '2026-02-29T01:30:00.000-08:00'), /receipt is invalid/);
    }
    assert.equal(Date.parse(untils[1]!) - Date.parse(untils[0]!), 60 * 60 * 1000);
  } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
});
