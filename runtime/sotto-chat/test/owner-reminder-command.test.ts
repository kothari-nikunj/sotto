import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { ConversationCoordinator } from '../src/conversation.js';
import { OperationalJournal, parseOwnerReminderCommand } from '../src/journal/index.js';
import { createOwnerReminderClient } from '../src/tools/owner-reminder-client.js';

const identity = { tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread',
  project_fingerprint: 'a'.repeat(64) };
const ownerPolicy = { channel: 'photon' as const, accountId: 'account', ownerId: 'owner' };
const context = { threadId: 'thread', destination: 'owner-destination', lineKey: 'line' };
const reminderId = `int_${'b'.repeat(32)}`;
const atCommand = '/reminder at 2026-10-09T08:15:00Z Review the cedar packet';
const inbound = (eventId: string, text: string) => ({ channel: 'photon' as const,
  accountId: 'account', senderId: 'owner', isDirect: true, eventId, text });
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });

test('literal create, list, and cancel are private owner commands with exact receipts and no model turn', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-owner-reminder-'));
  const journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  try {
    const operations: string[] = [];
    const admittedFeedback: string[] = [], acceptedFeedback: string[] = [];
    const client = createOwnerReminderClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp',
      bearer: 'c'.repeat(64), identity, fetch: async (_url, init) => {
        const body = JSON.parse(init!.body as string);
        operations.push(body.operation);
        assert.equal(body.identity.owner_id, 'owner');
        assert.equal(body.text_hash.length, 64);
        if (body.operation === 'list') return reply({ version: 1, status: 'listed', items: [
          { id: reminderId, due: '2026-10-09T08:15:00Z', action: 'Review the cedar packet', status: 'scheduled' }] });
        return reply({ version: 1, status: 'committed', operation: body.operation,
          action_id: body.action_id, event_id: body.event_id, payload_hash: body.payload_hash,
          reminder_id: reminderId, reminder_status: body.operation === 'create' ? 'scheduled' : 'canceled' });
      } });
    const sent: string[] = [];
    const service = new ConversationCoordinator({ journal, ownerPolicy, ownerReminder: client,
      onOwnerAdmission: input => {admittedFeedback.push(input.eventId);},
      onOwnerReplyAccepted: input => {acceptedFeedback.push(input.eventId);},
      model: { generate: async () => { throw new Error('literal command reached model'); } },
      postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: `send-${sent.length}` }; } });
    await service.receive(inbound('create', atCommand), context);
    await service.receive(inbound('list', '/reminder list'), context);
    await service.receive(inbound('cancel', `/reminder cancel ${reminderId}`), context);
    assert.deepEqual(operations, ['create', 'list', 'cancel']);
    assert.deepEqual(admittedFeedback, ['create', 'list', 'cancel']);
    assert.deepEqual(acceptedFeedback, ['create', 'list', 'cancel']);
    assert.match(sent[0]!, new RegExp(reminderId));
    assert.match(sent[1]!, /Review the cedar packet/);
    assert.match(sent[2]!, /already in progress may still arrive/);
    assert.equal((await service.receive(inbound('create', atCommand), context)).status, 'duplicate');
    assert.deepEqual(operations, ['create', 'list', 'cancel']);
    const actions = journal.db.prepare("SELECT kind,status FROM actions WHERE kind='owner_reminder_admitted'").all();
    assert.deepEqual(actions.map(row => [row.kind, row.status]), [
      ['owner_reminder_admitted', 'succeeded'], ['owner_reminder_admitted', 'succeeded']]);
    assert.throws(() => journal.recordActionIntent({ actionId: 'model', runId: 'model',
      kind: 'owner_reminder_admitted', payloadHash: 'model' }), /admitted-event proof/);
    journal.pruneTerminal(Date.now() + 1000);
    assert.equal(journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_reminder_admitted'").get()!.n, 2);
  } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
});

test('lost create receipt remains unknown; safe chat progresses; exact inspect alone releases original ack', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-owner-reminder-recover-'));
  const path = join(folder, 'operations.sqlite');
  let journal = new OperationalJournal(path);
  let createContacts = 0;
  let inspectProved = false;
  const requests: string[] = [];
  const client = createOwnerReminderClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp',
    bearer: 'c'.repeat(64), identity, fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      requests.push(body.operation);
      if (body.operation === 'create') { createContacts++; throw new Error('response lost after effect'); }
      if (!inspectProved) return reply({ version: 1, status: 'not_found' });
      return reply({ version: 1, status: 'committed', operation: 'create',
        action_id: body.action_id, event_id: body.event_id, payload_hash: body.payload_hash,
        reminder_id: reminderId, reminder_status: 'canceled' });
    } });
  try {
    const original = new ConversationCoordinator({ journal, ownerPolicy, ownerReminder: client,
      model: { generate: async () => { throw new Error('command reached model'); } },
      postPrepared: async () => { throw new Error('unknown command was acknowledged'); } });
    assert.equal((await original.receive(inbound('create-unknown', atCommand), context)).status, 'held');
    const action = journal.db.prepare("SELECT id,status FROM actions WHERE kind='owner_reminder_admitted'").get()!;
    assert.equal(action.status, 'unknown');
    journal.close();
    journal = new OperationalJournal(path);
    journal.recoverInterrupted();
    const generated: string[] = [];
    const sent: string[] = [];
    const recovered = new ConversationCoordinator({ journal, ownerPolicy, ownerReminder: client,
      model: { generate: async request => { generated.push(request.text); return 'safe answer'; } },
      postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: `send-${sent.length}` }; } });
    assert.equal((await recovered.recover())[0]?.status, 'held');
    assert.equal((await recovered.receive(inbound('safe', 'What is next?'), context)).status, 'accepted');
    assert.deepEqual(generated, ['What is next?']);
    assert.deepEqual(sent, ['safe answer']);
    assert.equal(journal.getAction(action.id as string)?.status, 'unknown');
    inspectProved = true;
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE status='unknown'").run();
    assert.equal((await recovered.recover())[0]?.status, 'accepted');
    assert.equal(journal.getAction(action.id as string)?.status, 'succeeded');
    assert.match(sent[1]!, /was created and has since been canceled/);
    assert.match(sent[1]!, new RegExp(reminderId));
    assert.equal(createContacts, 1);
    assert.deepEqual(requests, ['create', 'inspect', 'inspect']);
  } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
});

test('invalid reminder calendar and target syntax never mint an effect', () => {
  assert.equal(parseOwnerReminderCommand('/reminder at 2026-02-30T08:15:00Z invalid'), null);
  assert.equal(parseOwnerReminderCommand('/reminder at 2026-10-09T08:15:00 Review'), null);
  assert.equal(parseOwnerReminderCommand('/reminder cancel int_bad'), null);
  assert.deepEqual(parseOwnerReminderCommand(atCommand), { operation: 'create' });
});

test('unknown cancel inspects exact proof once, never contacts cancellation again', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-owner-reminder-cancel-'));
  const journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let cancelContacts = 0;
  const client = createOwnerReminderClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp',
    bearer: 'c'.repeat(64), identity, fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      if (body.operation === 'cancel') { cancelContacts++; throw new Error('lost cancel response'); }
      return reply({ version: 1, status: 'committed', operation: 'cancel',
        action_id: body.action_id, event_id: body.event_id, payload_hash: body.payload_hash,
        reminder_id: reminderId, reminder_status: 'fired' });
    } });
  try {
    const sent: string[] = [];
    const service = new ConversationCoordinator({ journal, ownerPolicy, ownerReminder: client,
      model: { generate: async () => { throw new Error('cancel reached model'); } },
      postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: 'ack' }; } });
    assert.equal((await service.receive(inbound('cancel-unknown', `/reminder cancel ${reminderId}`), context)).status, 'held');
    assert.deepEqual(await service.recover(), [], 'the first immediate inspect already acknowledged the exact cancellation');
    assert.equal(cancelContacts, 1);
    assert.equal(sent.length, 1);
    assert.match(sent[0]!, /has already fired/);
    assert.equal((await service.receive(inbound('cancel-unknown', `/reminder cancel ${reminderId}`), context)).status, 'duplicate');
    assert.equal(cancelContacts, 1);
  } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
});

test('reminder client rejects a successful-looking receipt with wrong effect binding', async () => {
  const client = createOwnerReminderClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp',
    bearer: 'c'.repeat(64), identity, fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      return reply({ version: 1, status: 'committed', operation: 'create',
        action_id: body.action_id, event_id: body.event_id, payload_hash: 'f'.repeat(64),
        reminder_id: reminderId, reminder_status: 'scheduled' });
    } });
  await assert.rejects(client.call('create', { actionId: 'a'.repeat(64), eventId: 'b'.repeat(64),
    payloadHash: 'd'.repeat(64), text: atCommand, admittedAtMs: Date.now(), providerThreadId: 'thread' }),
  /unavailable/);
});

test('invalid unknown reminder authority isolates only its intact command', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-owner-reminder-scope-'));
  const journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  try {
    const originalClient = createOwnerReminderClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp',
      bearer: 'c'.repeat(64), identity, fetch: async () => { throw new Error('lost effect response'); } });
    const first = new ConversationCoordinator({ journal, ownerPolicy, ownerReminder: originalClient,
      model: { generate: async () => { throw new Error('command reached model'); } },
      postPrepared: async () => { throw new Error('unknown effect acknowledged'); } });
    assert.equal((await first.receive(inbound('wrong-scope', atCommand), context)).status, 'held');
    let receiverContacts = 0;
    const wrongScopeClient = createOwnerReminderClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp',
      bearer: 'd'.repeat(64), identity, fetch: async () => { receiverContacts++; throw new Error('invalid inspect'); } });
    const sent: string[] = [];
    const recovered = new ConversationCoordinator({ journal, ownerPolicy, ownerReminder: wrongScopeClient,
      model: { generate: async request => {
        assert.equal(request.text, 'safe independent chat');
        return 'safe answer';
      } },
      postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: 'safe' }; } });
    assert.equal((await recovered.recover())[0]?.status, 'held');
    assert.equal((await recovered.receive(inbound('safe-after-wrong-scope', 'safe independent chat'), context)).status, 'accepted');
    assert.deepEqual(sent, ['safe answer']);
    assert.equal(receiverContacts, 0);
    assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_reminder_admitted'").get()!.status, 'unknown');
  } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
});

test('a /stop after an unknown command with invalid authority parks it; later chat is never wedged, even after restart', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-owner-reminder-stopped-'));
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  try {
    const originalClient = createOwnerReminderClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp',
      bearer: 'c'.repeat(64), identity, fetch: async () => { throw new Error('lost effect response'); } });
    const first = new ConversationCoordinator({ journal, ownerPolicy, ownerReminder: originalClient,
      model: { generate: async () => { throw new Error('command reached model'); } },
      postPrepared: async () => { throw new Error('unknown effect acknowledged'); } });
    assert.equal((await first.receive(inbound('stopped-command', atCommand), context)).status, 'held');
    assert.equal((await first.receive(inbound('stop', '/stop'), context)).status, 'stopped');
    await first.close(); journal.close();
    journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();
    let receiverContacts = 0;
    const wrongScopeClient = createOwnerReminderClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp',
      bearer: 'd'.repeat(64), identity, fetch: async () => { receiverContacts++; throw new Error('invalid inspect'); } });
    const sent: string[] = [];
    const recovered = new ConversationCoordinator({ journal, ownerPolicy, ownerReminder: wrongScopeClient,
      model: { generate: async request => `answer: ${request.text}` },
      postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: `m${sent.length}` }; } });
    assert.deepEqual(await recovered.recover(), [{ status: 'stopped' }]);
    assert.equal((await recovered.receive(inbound('after-stop-1', 'first safe chat'), context)).status, 'accepted');
    assert.equal((await recovered.receive(inbound('after-stop-2', 'second safe chat'), context)).status, 'accepted');
    assert.deepEqual(sent, [
      'I stopped working on your earlier request. An action already sent may still have happened; please check before retrying.',
      'answer: first safe chat', 'answer: second safe chat',
    ]);
    assert.equal(receiverContacts, 0);
    assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_reminder_admitted'").get()!.status, 'unknown');
    assert.equal(journal.db.prepare("SELECT outcome FROM inbound_events WHERE source_id='stopped-command'").get()!.outcome, 'cancelled');
    await recovered.close();
  } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
});
