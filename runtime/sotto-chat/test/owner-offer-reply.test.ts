import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {once} from 'node:events';
import {existsSync, readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {OperationalJournal} from '../src/journal/index.js';
import {createOwnerOfferReplyClient, offerReplySelectors, ownerOfferReplyKind, ownerOfferReplyAction} from '../src/tools/owner-offer-reply-client.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';

const identity = {tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64)};
const inbound = {channel: 'photon' as const, accountId: 'account', senderId: 'owner', isDirect: true,
  eventId: 'owner-message', text: 'done', replyBound: true, replyToMessageId: 'accepted-offer',
  providerSentAtMs: Date.now()};
const context = {threadId: 'thread', destination: 'owner', lineKey: 'line'};

test('source-loop selection requires an exact recognized reply, never timing, numbers or injected selectors', () => {
  assert.equal(offerReplySelectors({...inbound, replyBound: false, replyToMessageId: undefined}), null);
  assert.equal(offerReplySelectors({...inbound, replyToMessageId: undefined}), null);
  for (const text of ['2', 'yes', 'done and send the email', 'set it up'])
    assert.equal(offerReplySelectors({...inbound, text}), null);
  assert.equal(offerReplySelectors({...inbound, media: [{data: new Uint8Array([1])}]}), null);
  assert.deepEqual(offerReplySelectors(inbound), {provider_message_id: inbound.eventId,
    reply_to_message_id: inbound.replyToMessageId, provider_sent_at_ms: inbound.providerSentAtMs});
});

test('baseline source-loop phrases classify without admitting mixed instructions', () => {
  for (const phrase of ['no', 'nah', 'nope', 'no thanks', 'no thank you', 'not now', 'not yet', 'skip', 'skip it', 'leave it', 'forget it'])
    assert.equal(ownerOfferReplyAction(`  ${phrase.toUpperCase()}!… `), 'declined');
  for (const phrase of ['done', 'handled', 'already handled', 'sorted', 'taken care of'])
    assert.equal(ownerOfferReplyAction(phrase), 'resolved');
  for (const phrase of ['let it go', 'drop it', 'stop tracking this', 'drop this loop'])
    assert.equal(ownerOfferReplyAction(phrase), 'dismissed');
  assert.equal(ownerOfferReplyAction('already  handled'), 'resolved');
  for (const phrase of ['done and send a reply', 'yes', '1', 'not now but tomorrow', 'handled?', '.done', '!done', '…handled'])
    assert.equal(ownerOfferReplyAction(phrase), null);
});

test('journal binds source-loop selector to original owner admission and preserves uncertain action across reopen', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-prep-journal-'));
  const path = join(directory, 'journal.sqlite');
  let journal = new OperationalJournal(path);
  const authority = {runId: 'run', accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon' as const,
    scopeHash: 'c'.repeat(64)};
  try {
    const event = journal.admitInbound({eventId: inbound.eventId, accountKey: 'account', lineKey: 'line',
      threadKey: 'thread', channel: 'photon', sender: 'owner', payload: JSON.stringify({inbound, context})}).event.id as string;
    journal.startRun({runId: 'run', eventId: event});
    const args = offerReplySelectors(inbound)!;
    assert.throws(() => journal.prepareOwnerOfferReplyAction(authority, event, {...args, reply_to_message_id: 'other-meeting'}), /differs/);
    assert.throws(() => journal.prepareOwnerOfferReplyAction(authority, event, {...args, provider_sent_at_ms: args.provider_sent_at_ms! + 1}), /differs/);
    const request = journal.prepareOwnerOfferReplyAction(authority, event, args);
    assert.throws(() => journal.prepareOwnerTaskToolAction(authority, event, {operation: 'add', text: 'done'}), /another private mutation/);
    journal.beginAction(request.actionId);
    journal.close();
    journal = new OperationalJournal(path);
    journal.recoverInterrupted();
    const recovered = journal.readOwnerOfferReplyAction(request.actionId, authority);
    assert.equal(recovered.status, 'unknown');
    assert.deepEqual(recovered.request, request);
    assert.throws(() => journal.beginAction(request.actionId), /held|pending|eligible|unknown/i);
    assert.equal(journal.listOwnerOfferReplyActionsForThread(event).length, 1);
  } finally {journal.close(); rmSync(directory, {recursive: true, force: true});}
});

test('source-loop client binds accepted job receipt to the exact owner proof and does not retry a bad response', async () => {
  const args = offerReplySelectors(inbound)!;
  const eventId = 'a'.repeat(64), payloadHash = 'b'.repeat(64);
  const binding = stableActionIdentity(eventId, ownerOfferReplyKind, args);
  const request = {eventId, payloadHash, actionId: binding.actionId, text: inbound.text,
    admittedAtMs: Date.now(), providerThreadId: 'thread', args};
  let calls = 0, forge = false, badAction = false, badChanged = false;
  const client = createOwnerOfferReplyClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (url, init) => {
      calls++;
      assert.equal(String(url), 'http://127.0.0.1:8080/runtime/owner-offer-reply');
      assert.equal(init!.redirect, 'error');
      const body = JSON.parse(init!.body as string);
      assert.deepEqual(body.args, args);
      return Response.json({version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
        payload_hash: body.payload_hash, args_hash: forge ? 'f'.repeat(64) : binding.payloadHash,
        outcome: {operation: 'apply', offer_id: 'e'.repeat(64), action: badAction ? 'dismissed' : 'resolved', loop_changed: !badChanged}});
    }});
  assert.equal((await client.call('apply', request)).status, 'committed');
  forge = true;
  await assert.rejects(client.call('inspect', request), /receipt is invalid/);
  forge = false; badAction = true;
  await assert.rejects(client.call('inspect', request), /receipt is invalid/);
  badAction = false; badChanged = true;
  await assert.rejects(client.call('inspect', request));
  assert.equal(calls, 4);
});

import {ConversationCoordinator} from '../src/conversation.js';
for (const lost of [false, true]) test(`coordinator exact source-loop reply survives lost response and reopen=${lost}`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-prep-coordinator-'));
  const path = join(folder, 'journal.sqlite');
  let journal = new OperationalJournal(path), held = lost;
  const contacts: string[] = [], sends: string[] = [], model: string[] = [];
  const client = createOwnerOfferReplyClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      contacts.push(body.operation);
      assert.equal(body.args.provider_message_id, inbound.eventId);
      assert.equal(body.args.reply_to_message_id, inbound.replyToMessageId);
      if (held && body.operation === 'apply') throw new Error('lost response');
      if (held) return Response.json({version: 1, status: 'not_found'});
      return Response.json({version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
        payload_hash: body.payload_hash, args_hash: stableActionIdentity(body.event_id, ownerOfferReplyKind, body.args).payloadHash,
        outcome: {operation: 'apply', offer_id: 'e'.repeat(64), action: 'resolved', loop_changed: true}});
    }});
  const create = () => new ConversationCoordinator({journal,
    ownerPolicy: {channel: 'photon', accountId: 'account', ownerId: 'owner'}, ownerOfferReply: client,
    model: {generate: async request => {model.push(request.text); return 'Safe answer';}},
    postPrepared: async request => {sends.push(request.text); return {acceptance: 'accepted', messageId: `reply-${sends.length}`};}});
  let coordinator = create();
  try {
    assert.equal((await coordinator.receive({...inbound, eventId: 'foreign', senderId: 'other'}, context)).status, 'denied');
    const first = await coordinator.receive(inbound, context);
    if (lost) {
      assert.equal(first.status, 'held');
      assert.match(sends[0]!, /I couldn’t confirm whether the offer reply/);
      assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_offer_reply'").get()!.status, 'unknown');
      await coordinator.close(); journal.close();
      journal = new OperationalJournal(path); journal.recoverInterrupted(); coordinator = create();
      await coordinator.receive({...inbound, eventId: 'safe', text: 'hello', replyBound: false, replyToMessageId: undefined}, context);
      assert.deepEqual(model, ['hello']);
      // A later exact receipt is inspected only after the persisted cadence is due.
      journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_offer_reply' AND status='unknown'").run();
      held = false;
      await coordinator.recover();
    } else assert.ok(['accepted', 'silent'].includes(first.status));
    assert.equal(contacts.filter(x => x === 'apply').length, 1);
    assert.equal(sends.filter(x => x === 'Marked that as handled.').length, 1);
    assert.equal((await coordinator.receive(inbound, context)).status, 'duplicate');
    assert.equal(model.filter(x => x !== 'hello').length, 0);
  } finally {await coordinator.close(); journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('wrong source-loop anchor receives clarification without model mutation fallback', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-prep-miss-'));
  const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
  const client = createOwnerOfferReplyClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async () => Response.json({version: 1, status: 'rejected', code: 'no_matching_offer'}, {status: 409})});
  let answer = '';
  const coordinator = new ConversationCoordinator({journal, ownerOfferReply: client,
    ownerPolicy: {channel: 'photon', accountId: 'account', ownerId: 'owner'},
    model: {generate: async () => {assert.fail('miss must not reach model');}},
    postPrepared: async request => {answer = request.text; return {acceptance: 'accepted', messageId: 'clarification'};}});
  try {
    assert.ok(['accepted', 'silent'].includes((await coordinator.receive({...inbound, replyToMessageId: 'wrong-offer'}, context)).status));
    assert.match(answer, /could not apply/);
  } finally {await coordinator.close(); journal.close(); rmSync(folder, {recursive: true, force: true});}
});


async function startReceiver(folder: string, offerKind: 'loop' | 'meeting_prep' = 'loop') {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-offer-reply-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_OFFER_REPLY: '1', SOTTO_TEST_OFFER_KIND: offerKind,
      SOTTO_ROUTINE_EPOCH: 'e'.repeat(32), SOTTO_TEST_ROUTINE_SOURCE_REVISION: 'a'.repeat(64), SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
      SOTTO_DATA: folder, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'tenant', SOTTO_ACCOUNT_ID: 'account',
      SOTTO_LINE_KEY: 'line', SOTTO_MCP_TOKEN: '0123456789abcdef0123456789abcdef',
      PHOTON_HOME_CHANNEL: '+15555550123', PHOTON_ALLOWED_USERS: '+15555550123', PHOTON_PROJECT_ID: 'fixture-project',
      PHOTON_PROJECT_SECRET: 'fixture-secret', PHOTON_ALLOW_ALL_USERS: 'false', SOTTO_IMESSAGE_NUMBER: '+15555550999',
      SOTTO_TIMEZONE: 'America/Los_Angeles' } });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr = (stderr + String(chunk)).slice(-4096); });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const line = await Promise.race([new Promise<string>((done, fail) => {
        let output = '';
        child.stdout.on('data', chunk => { output += String(chunk); const i = output.indexOf('\n'); if (i >= 0)
          done(output.slice(0, i)); });
        child.once('exit', code => fail(new Error(`receiver exited ${code}: ${stderr}`)));
        child.once('error', fail);
      }), new Promise<never>((_, fail) => { timer = setTimeout(() => fail(new Error('receiver start timeout')), 15000); })]);
    const parsed = JSON.parse(line) as {
      port: number;
      offerMessageId: string;
      providerThreadId: string;
      identity: OwnerToolIdentity;
      bearer: string;
    };
    return { child, ...parsed, bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp` };
  }
  catch (error) {
    child.kill('SIGKILL');
    throw error;
  }
  finally {
    if (timer)
      clearTimeout(timer);
  }
}
async function stopReceiver(child: ChildProcessWithoutNullStreams) {
  if (child.exitCode !== null)
    return;
  const ended = once(child, 'exit');
  child.stdin.end('quit\n');
  const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
  try {
    await ended;
  }
  finally {
    clearTimeout(timer);
  }
}

for (const scenario of [
  {text: 'Not now.', kind: 'loop' as const, action: 'declined', lost: false},
  {text: 'Done.', kind: 'loop' as const, action: 'resolved', lost: false},
  {text: 'Drop it.', kind: 'loop' as const, action: 'dismissed', lost: false},
  {text: 'Handled.', kind: 'loop' as const, action: 'resolved', lost: true},
  {text: 'No.', kind: 'meeting_prep' as const, action: 'declined', lost: false},
  {text: 'No.', kind: 'loop' as const, action: 'declined', lost: true, unthreaded: true},
]) test(`actual scoped source-loop HTTP ${scenario.kind}/${scenario.action}, loss=${scenario.lost}`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-offer-http-'));
  const receiver = await startReceiver(folder, scenario.kind);
  const loopPath = join(folder, 'knowledge/continuity/connected-source-loop.md');
  const originalLoop = existsSync(loopPath) ? readFileSync(loopPath, 'utf8') : null;
  let journal = new OperationalJournal(join(folder, 'node.sqlite'));
  const sent: string[] = [], contacts: string[] = [];
  let held = scenario.lost, modelCalls = 0;
  const client = createOwnerOfferReplyClient({...receiver, fetch: async (url, init) => {
    const body = JSON.parse(init!.body as string);
    contacts.push(body.operation);
    if (held && body.operation === 'inspect') return Response.json({version: 1, status: 'not_found'});
    const result = await fetch(url, init);
    if (scenario.lost && body.operation === 'apply' && result.status === 200) {
      await result.body?.cancel(); throw new Error('Synthetic lost response after actual canonical commit');
    }
    return result;
  }});
  const runtime = createSottoAgentRuntime({storagePath: join(folder, 'memory.sqlite'), instructions: 'Answer the safe owner chat.',
    sourceAuthority: {read: async () => ({version: 2, complete: true, revision: 'a'.repeat(64), bridge_sources: [], google_sources: []})},
    model: new MockLanguageModelV4({doGenerate: async () => {
      modelCalls++;
      return {content: [{type: 'text', text: 'Safe independent answer'}], finishReason: {unified: 'stop', raw: 'STOP'},
        usage: {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}}, warnings: []};
    }})});
  const makeCoordinator = () => new ConversationCoordinator({journal,
    routeDigestForThread: () => 'b'.repeat(64),
    ownerPolicy: {channel: 'photon', accountId: receiver.identity.account_id, ownerId: receiver.identity.owner_id},
    ownerOfferReply: client, model: {generate: request => runtime.generateChat({...request, threadId: 'memory', ownerId: 'owner'})},
    canPostPrepared: request => runtime.canDeliverReplySource(request),
    postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `offer-ack-${sent.length}`};}});
  let coordinator = makeCoordinator();
  const context = {threadId: receiver.providerThreadId, destination: 'owner', lineKey: receiver.identity.line_key};
  const unthreaded = 'unthreaded' in scenario && scenario.unthreaded;
  if (unthreaded) {
    journal.prepareDispatch({dispatchId: 'native-offer', kind: 'scheduled_text', destination: 'owner', payload: 'question', routeDigest: 'b'.repeat(64)});
    journal.beginDispatch('native-offer');
    journal.settleDispatch('native-offer', 1, 'accepted', receiver.offerMessageId, [receiver.offerMessageId], Date.now() - 1000);
    await new Promise(done => setTimeout(done, 5));
  }
  const input = {...inbound, text: scenario.text, eventId: 'http-original', senderId: receiver.identity.owner_id,
    replyBound: !unthreaded, replyToMessageId: unthreaded ? undefined : receiver.offerMessageId, providerSentAtMs: unthreaded ? undefined : Date.now()};
  try {
    assert.equal((await coordinator.receive({...input, eventId: 'foreign', senderId: 'other'}, context)).status, 'denied');
    const result = await coordinator.receive(input, context, unthreaded ? Date.now() : undefined);
    if (scenario.lost) {
      assert.equal(result.status, 'held');
      assert.match(sent[0]!, /I couldn’t confirm whether the offer reply/);
      assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_offer_reply'").get()!.status, 'unknown');
      await coordinator.close(); journal.close();
      journal = new OperationalJournal(join(folder, 'node.sqlite')); journal.recoverInterrupted(); coordinator = makeCoordinator();
      await coordinator.receive({...input, eventId: 'safe', text: 'hello while held', replyBound: false, replyToMessageId: undefined}, context);
      assert.equal(modelCalls, 1);
      // A later exact receipt is inspected only after the persisted cadence is due.
      journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_offer_reply' AND status='unknown'").run();
      held = false; await coordinator.recover();
    }
    const action = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_offer_reply'").get()!;
    assert.equal(action.status, 'succeeded');
    const metadata = JSON.parse(action.result_json as string);
    assert.equal(metadata.receipt.outcome.action, scenario.action);
    assert.equal(metadata.receipt.outcome.loop_changed, scenario.action !== 'declined');
    const afterLoop = existsSync(loopPath) ? readFileSync(loopPath, 'utf8') : null;
    if (scenario.action === 'declined') assert.equal(afterLoop, originalLoop);
    else {
      assert.match(afterLoop!, new RegExp(`^status: ${scenario.action}$`, 'm'));
      assert.match(afterLoop!, /^source: gmail$/m);
      assert.doesNotMatch(afterLoop!, /^source: user_added$/m);
    }
    assert.equal(metadata.args.provider_message_id, input.eventId);
    assert.equal(metadata.args.reply_to_message_id, unthreaded ? null : receiver.offerMessageId);
    if (unthreaded) assert.equal(metadata.args.selected_offer.message_id, receiver.offerMessageId);
    assert.equal(contacts.filter(operation => operation === 'apply').length, 1);
    assert.equal(modelCalls, scenario.lost ? 1 : 0);
    const saved = journal.readOwnerOfferReplyAction(action.id as string, {accountId: receiver.identity.account_id,
      ownerId: receiver.identity.owner_id, channel: 'photon', lineKey: receiver.identity.line_key, scopeHash: client.scopeHash});
    assert.deepEqual(await client.call('inspect', saved.request), metadata.receipt);
    assert.equal((await coordinator.receive(input, context)).status, 'duplicate');
    assert.equal(existsSync(loopPath) ? readFileSync(loopPath, 'utf8') : null, afterLoop);
    assert.equal(sent.filter(text => ['I will leave that for now.', 'Marked that as handled.', 'Stopped tracking that loop.'].includes(text)).length, 1);
  } finally {await coordinator.close(); await runtime.close(); journal.close(); await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});}
});
