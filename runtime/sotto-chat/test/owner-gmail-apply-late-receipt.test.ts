import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { executeTool } from './helpers/execute-tool.js';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator, type ModelRequest } from '../src/conversation.js';
import { createOwnerGmailDraftClient } from '../src/tools/owner-gmail-draft-client.js';
import { createOwnerGmailDraftTools } from '../src/tools/owner-gmail-draft-tool.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { ownerGmailDraftKind, gmailDraftReviewHash } from '../src/tools/owner-gmail-draft-contract.js';

const identity = { tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64) };
const ownerPolicy = { channel: 'photon' as const, accountId: 'account', ownerId: 'owner' };
const context = { threadId: 'thread', destination: 'owner', lineKey: 'line' };
const inbound = (eventId: string, text: string) => ({ channel: 'photon' as const, accountId: 'account', senderId: 'owner', isDirect: true, eventId, text });
const authority = { version: 2 as const, complete: true, revision: 'd'.repeat(64), bridge_sources: [], google_sources: ['gmail' as const] };
const reply = (value: unknown) => Response.json(value);

async function lateApply(t: test.TestContext, commitAt: 'immediate-inspect' | 'timer' | 'timer-unknown' | 'never') {
  const timer = commitAt === 'timer' || commitAt === 'timer-unknown';
  if (timer) t.mock.timers.enable({apis: ['Date', 'setTimeout'], now: Date.parse('2026-11-13T20:00:00Z')});
  const timeout = AbortSignal.timeout.bind(AbortSignal);
  t.mock.method(AbortSignal, 'timeout', (ms: number) => timeout(ms === 15_000 ? 30 : ms));
  const folder = mkdtempSync(join(tmpdir(), 'sotto-gmail-late-')), journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  const calls: string[] = [], receipts = new Map<string, unknown>(), sent: string[] = [];
  let commitDraft: (() => void) | undefined, sentBeforeTimer = 0;
  const client = createOwnerGmailDraftClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      calls.push(body.operation);
      if (body.operation === 'preview') {
        const review = { message_id: body.args.message_id, thread_id: 'thread-1', to: 'sender@example.test', subject: 'Re: Notes',
          body: body.args.body, reply_message_id: '<message@example.test>', source_revision: authority.revision, review_hash: '' };
        review.review_hash = gmailDraftReviewHash(review);
        return reply({ version: 1, status: 'preview', event_id: body.event_id, payload_hash: body.payload_hash, review });
      }
      if (body.operation === 'inspect')
        return reply(receipts.get(body.action_id) ?? { version: 1, status: 'unknown' });
      const alive = setInterval(() => {}, 1_000); // AbortSignal.timeout does not hold the event loop open
      await new Promise(resolve => init!.signal!.addEventListener('abort', resolve, { once: true }));
      clearInterval(alive);
      commitDraft = () => receipts.set(body.action_id, { version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
        payload_hash: body.payload_hash, args_hash: stableActionIdentity(body.event_id, ownerGmailDraftKind, body.args).payloadHash,
        outcome: { operation: 'save', draft_id: 'draft-1', thread_id: 'thread-1', review_hash: body.args.review.review_hash, status: 'draft_saved' } });
      if (commitAt === 'immediate-inspect') commitDraft();
      throw new Error('client deadline reached');
    } });
  const tools = createOwnerGmailDraftTools({ client, journal: () => journal, sourceAuthority: { read: async () => authority } });
  const callTool = (name: keyof typeof tools, input: unknown, request: ModelRequest) => {
    return executeTool(tools[name], input, request.signal, { mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey,
      sourceRevision: authority.revision, allowOwnerGmailDraft: request.allowOwnerGmailDraft });
  };
  const c = new ConversationCoordinator({ journal, ownerPolicy, ownerGmailDraft: client, model: { generate: async (r) => {
    if (r.text.startsWith('Draft')) await callTool('gmail_draft_propose', { message_id: 'message-1', body: 'Thanks, Robin will send the notes.' }, r);
    else await callTool('gmail_draft_save', {}, r);
    return { text: 'safe chat', sourceRevision: authority.revision };
  } }, postPrepared: async (r) => { sent.push(r.text); return { acceptance: 'accepted', messageId: `ack-${sent.length}` }; } });
  try {
    await c.receive(inbound('proposal', 'Draft a reply to Robin'), context);
    const status = (await c.receive(inbound('yes', 'Yes.'), context)).status;
    if (timer) {
      await c.drain();
      assert.deepEqual(calls.filter(op => op !== 'preview'), ['apply', 'inspect']);
      assert.match(sent.at(-1)!, /^I couldn’t confirm whether the Gmail action happened\./);
      assert.ok(commitDraft);
      sentBeforeTimer = sent.length;
      if (commitAt === 'timer') commitDraft();
      t.mock.timers.tick(59_999);
      assert.deepEqual(calls.filter(op => op !== 'preview'), ['apply', 'inspect']);
      t.mock.timers.tick(1);
      await c.drain();
      await Promise.resolve();
      await c.drain();
    }
    return { status, sent, calls, sentBeforeTimer };
  } finally {
    await c.close();
    journal.close();
    rmSync(folder, { recursive: true, force: true });
  }
}

// The held turn's one immediate delayed inspection (conversation.ts mayInspectDelayed) already
// covers a receipt that lands after the client deadline; these pin that no second apply happens.
test('apply deadline then one inspect showing the committed draft reports it saved, never re-applies', async (t) => {
  const { status, sent, calls } = await lateApply(t, 'immediate-inspect');
  assert.equal(status, 'held');
  assert.equal(sent.at(-1), 'Saved your Gmail draft. Nothing was sent.');
  assert.ok(!sent.some(text => text.startsWith('I couldn’t confirm')));
  assert.deepEqual(calls.filter(op => op !== 'preview'), ['apply', 'inspect']);
});

test('apply deadline then an inconclusive inspect still reports the failure, never re-applies', async (t) => {
  const { status, sent, calls } = await lateApply(t, 'never');
  assert.equal(status, 'held');
  assert.match(sent.at(-1)!, /^I couldn’t confirm whether the Gmail action happened\./);
  assert.ok(!sent.includes('Saved your Gmail draft. Nothing was sent.'));
  assert.deepEqual(calls.filter(op => op !== 'preview'), ['apply', 'inspect']);
});

test('Gmail commits after first inspect and wakes again without an owner message', async (t) => {
  const { status, sent, calls } = await lateApply(t, 'timer');
  assert.equal(status, 'held');
  assert.deepEqual(calls.filter(op => op !== 'preview'), ['apply', 'inspect', 'inspect']);
  assert.equal(sent.at(-1), 'Saved your Gmail draft. Nothing was sent.');
});

test('Gmail still unknown at the scheduled inspect stays silent, never re-applies', async (t) => {
  const { status, sent, calls, sentBeforeTimer } = await lateApply(t, 'timer-unknown');
  assert.equal(status, 'held');
  assert.deepEqual(calls.filter(op => op !== 'preview'), ['apply', 'inspect', 'inspect']);
  assert.equal(sent.length, sentBeforeTimer);
  assert.equal(sent.filter(text => text.startsWith('I couldn’t confirm')).length, 1);
  assert.ok(!sent.includes('Saved your Gmail draft. Nothing was sent.'));
});
