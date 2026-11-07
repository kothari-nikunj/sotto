import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { RequestContext } from '@mastra/core/request-context';
import { MockLanguageModelV4 } from 'ai/test';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator, type ModelRequest } from '../src/conversation.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createOwnerReminderToolClient } from '../src/tools/owner-reminder-tool-client.js';
import { createOwnerReminderTools } from '../src/tools/owner-reminder-tool.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { ownerReminderToolKind } from '../src/tools/owner-reminder-tool-contract.js';

const identity = {tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64)};
const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: 'owner'};
const context = {threadId: 'thread', destination: 'owner', lineKey: 'line'};
const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: 'owner',
  isDirect: true, eventId, text});
const args = {due: new Date(Date.now() + 3600000).toISOString(), action: 'Review the cedar packet 🌲'};
const authority = {version: 2 as const, complete: true, revision: 'd'.repeat(64), bridge_sources: [], google_sources: []};
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status});
const usage = {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}};
function fixture(folder: string) {
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  const calls: Record<string, unknown>[] = [], receipts = new Map<string, Record<string, unknown>>();
  let loseCreate = false, hideInspect = false, state = authority;
  let loopReads = 0;
  const loop = {anchor_key: 'waiting-source', loop_version: 'd'.repeat(64), loop_identity: 'e'.repeat(64)};
  let waiting = [loop];
  const client = createOwnerReminderToolClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string); calls.push(body);
      assert.equal(body.text_hash, createHash('sha256').update(body.text).digest('hex')); assert.ok(!body.text.startsWith('/reminder'));
      if (body.operation === 'list') return reply({version: 1, status: 'listed', event_id: body.event_id,
        payload_hash: body.payload_hash, zone: 'America/Los_Angeles', items: []});
      if (body.operation === 'context') return reply({version: 1, status: 'context', event_id: body.event_id,
        payload_hash: body.payload_hash, zone: 'America/Los_Angeles', now: new Date().toISOString()});
      if (body.operation === 'inspect') return reply(!hideInspect && receipts.get(body.action_id) || {version: 1, status: 'not_found'});
      const binding = stableActionIdentity(body.event_id, ownerReminderToolKind, body.args);
      const receipt = {version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
        payload_hash: body.payload_hash, args_hash: binding.payloadHash, reminder_id: `int_${'b'.repeat(32)}`,
        reminder_status: 'scheduled', due: body.args.due, action: body.args.action,
        ...(body.args.operation === 'conditional' ? {operation: 'conditional', anchor_key: body.args.anchor_key} : {})};
      receipts.set(body.action_id, receipt); if (loseCreate) throw new Error('lost after commit'); return reply(receipt);
    }});
  const sourceAuthority = {read: async () => state};
  const tools = createOwnerReminderTools({client, journal: () => journal, sourceAuthority, readLoops: async () => {
    loopReads++; return {you_owe: [], waiting_on_them: waiting, counts: {you_owe: 0, waiting_on_them: waiting.length},
      coverage: 'currently_permitted_canonical_loops'} as never;
  }});
  const callTool = async (name: keyof typeof tools, input: unknown, request: ModelRequest, overrides: Record<string, unknown> = {}) => {
    const requestContext = new RequestContext();
    for (const [key, value] of Object.entries({mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey,
      sourceRevision: state.revision, allowOwnerReminder: request.allowOwnerReminder, ...overrides})) requestContext.set(key, value);
    return tools[name].execute!(input as never, {requestContext, abortSignal: request.signal} as never);
  };
  return {get journal() {return journal;}, client, calls, tools, sourceAuthority, callTool, loop,
    get loopReads() {return loopReads;}, clearLoops() {waiting = [];},
    terminal(status: 'canceled' | 'fired') {for (const receipt of receipts.values()) receipt.reminder_status = status;},
    set loseCreate(value: boolean) {loseCreate = value;}, set hideInspect(value: boolean) {hideInspect = value;},
    revoke() {state = {...authority, revision: 'e'.repeat(64)};},
    reopen() {journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();}};
}

test('real Mastra tool turn binds owner request; minimal receipt ACK survives source revocation and restart without replay', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-natural-reminder-')), f = fixture(folder); let step = 0; const sent: string[] = [];
  const model = new MockLanguageModelV4({doGenerate: async () => {
    step++;
    if (step <= 2) return {content: [{type: 'tool-call', toolCallId: `call-${step}`,
      toolName: step === 1 ? 'owner_reminder_context' : 'owner_reminder_create', input: JSON.stringify(step === 1 ? {} : args)}],
      finishReason: {unified: 'tool-calls', raw: 'tool_calls'}, usage, warnings: []};
    f.revoke(); return {content: [{type: 'text', text: 'Never send source-derived action text'}], finishReason: {unified: 'stop', raw: 'STOP'}, usage, warnings: []};
  }});
  const runtime = createSottoAgentRuntime({model, storagePath: join(folder, 'memory.db'), instructions: 'Use the reminder tools.',
    sourceAuthority: f.sourceAuthority, chatTools: async () => f.tools});
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client,
    model: {generate: request => runtime.generateChat(request)}, canPostPrepared: proof => runtime.canDeliverReplySource(proof),
    postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: 'ack'};}});
  try {
    await coordinator.receive(inbound('natural', 'Remind me in an hour to review the cedar packet.'), context);
    assert.equal(step, 3); assert.deepEqual(f.calls.map(row => row.operation), ['context', 'create']);
    assert.equal(sent.length, 1); assert.match(sent[0]!, /Reminder set\./);
    assert.ok(!sent[0]!.includes(args.action)); assert.ok(!sent[0]!.includes('source-derived'));
    const action = f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_reminder_tool'").get()!;
    assert.equal(action.status, 'succeeded'); assert.equal(action.source_event_id, f.calls[1]!.event_id);
    assert.deepEqual(JSON.parse(action.result_json as string).args, args);
    assert.equal((await coordinator.receive(inbound('natural', 'Remind me in an hour to review the cedar packet.'), context)).status, 'duplicate');
    assert.equal(f.calls.filter(row => row.operation === 'create').length, 1);
    await coordinator.close(); await runtime.close(); f.reopen();
    const recovered = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client,
      model: {generate: async () => {throw new Error('completed effect regenerated');}}, postPrepared: async () => {throw new Error('second ack');}});
    assert.deepEqual(await recovered.recover(), []); f.journal.pruneTerminal(Date.now() + 1000);
    assert.equal(f.journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_reminder_tool'").get()!.n, 1);
    assert.ok(!JSON.stringify(model.doGenerateCalls).includes('c'.repeat(64)));
  } finally {await runtime.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('lost receipt partitions only its owner event; restart and safe chat inspect saved arguments without recreate', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-natural-unknown-')), f = fixture(folder), sent: string[] = [];
  f.loseCreate = true; f.hideInspect = true;
  const first = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client,
    model: {generate: async request => {await f.callTool('owner_reminder_create', args, request); return 'not acknowledged';}},
    postPrepared: async request => {assert.match(request.text, /^I couldn’t confirm whether the reminder change happened\./); sent.push(request.text); return {acceptance: 'accepted', messageId: 'original-notice'};}});
  try {
    assert.equal((await first.receive(inbound('unknown', 'Remind me in one hour to review the packet.'), context)).status, 'held');
    const action = f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_reminder_tool'").get()!;
    assert.equal(action.status, 'unknown'); assert.deepEqual(JSON.parse(action.result_json as string).args, args);
    await first.close(); f.reopen(); const generated: string[] = [];
    const second = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client,
      model: {generate: async request => {generated.push(request.text); return 'safe answer';}},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    assert.equal((await second.recover())[0]?.status, 'held');
    assert.equal((await second.receive(inbound('safe', 'What is next?'), context)).status, 'accepted');
    assert.deepEqual(generated, ['What is next?']);
    assert.match(sent[0]!, /couldn’t confirm whether the reminder change happened/);
    assert.equal(sent[1], 'safe answer'); f.hideInspect = false;
    f.journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.id);
    assert.equal((await second.recover())[0]?.status, 'accepted'); assert.equal(f.journal.getAction(action.id as string)?.status, 'succeeded');
    assert.equal(f.calls.filter(row => row.operation === 'create').length, 1);
    for (const request of f.calls.filter(row => row.operation === 'inspect')) assert.deepEqual(request.args, args);
    assert.match(sent[2]!, /Reminder set\./);
  } finally {f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('trusted tool refuses unattended, stale, spoofed event, extra arguments and changed retry before contact', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-natural-authority-')), f = fixture(folder); const late: string[] = []; let generationError: unknown;
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client,
    model: {generate: async request => {
      try {
      for (const overrides of [{mode: 'unattended'}, {runId: 'spoof'}, {ownerRequestKey: 'spoof'}, {allowOwnerReminder: false}])
        await assert.rejects(f.callTool('owner_reminder_create', args, request, overrides));
      const invalid = await f.callTool('owner_reminder_create', {...args, event_id: 'spoof'}, request);
      assert.match(JSON.stringify(invalid), /validation|unrecognized|invalid/i);
      assert.equal(f.calls.length, 0);
      await f.callTool('owner_reminder_create', args, request);
      await assert.rejects(f.callTool('owner_reminder_create', {...args, action: 'changed'}, request), /conflicts/);
      f.journal.cancelRun(request.runId); await assert.rejects(f.callTool('owner_reminder_create', args, request)); return 'cancelled';
      } catch (error) {generationError = error; throw error;}
    }}, postPrepared: async request => {late.push(request.text); return {acceptance: 'accepted', messageId: 'late'};}});
  try {
    await coordinator.receive(inbound('authority', 'Remind me in one hour.'), context);
    assert.equal(generationError, undefined);
    assert.equal(f.calls.length, 1); assert.deepEqual(late, []);
    assert.equal((await coordinator.receive({...inbound('foreign', 'remind me'), senderId: 'other'}, context)).status, 'denied');
    assert.equal(f.calls.length, 1);
  } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('ordinary coalesced input cannot attribute a model reminder to the first owner event', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-natural-batch-')), f = fixture(folder);
  let enter!: () => void, release!: () => void, turns = 0;
  const entered = new Promise<void>(resolve => {enter = resolve;}), gate = new Promise<void>(resolve => {release = resolve;});
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client,
    model: {generate: async request => {
      if (++turns === 1) {enter(); await gate; return 'cancelled first';}
      assert.equal(request.allowOwnerReminder, false); await assert.rejects(f.callTool('owner_reminder_create', args, request));
      return 'Please repeat the reminder as one owner message.';
    }}, postPrepared: async () => ({acceptance: 'accepted', messageId: 'clarify'})});
  try {
    const first = coordinator.receive(inbound('first', 'Hello'), context); await entered;
    const second = coordinator.receive(inbound('second', 'Remind me in one hour'), context); release();
    await Promise.all([first, second]); assert.equal(f.calls.length, 0);
  } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('a final source-authority change before contact blocks the write without admitting an effect', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-natural-source-fence-')), f = fixture(folder); let checks = 0;
  f.sourceAuthority.read = async () => ++checks === 1 ? authority : {...authority, revision: 'e'.repeat(64)};
  let executed = false;
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client,
    model: {generate: async request => {
      await assert.rejects(f.callTool('owner_reminder_create', args, request), /withheld/); executed = true;
      return 'Source access changed; please retry your reminder request.';
    }}, postPrepared: async () => ({acceptance: 'accepted', messageId: 'clarify'})});
  try {
    await coordinator.receive(inbound('source-fence', 'Remind me in one hour.'), context);
    assert.ok(executed); assert.equal(checks, 2); assert.equal(f.calls.length, 0);
    assert.equal(f.journal.db.prepare('SELECT COUNT(*) AS n FROM actions').get()!.n, 0);
  } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('an unrelated single owner reminder can proceed beside an unknown prior reminder', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-natural-independent-')), f = fixture(folder);
  f.loseCreate = true; f.hideInspect = true;
  const sent: string[] = [], contexts: boolean[] = [];
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client,
    model: {generate: async request => {contexts.push(request.allowOwnerReminder);
      await f.callTool('owner_reminder_create', {...args, action: request.text}, request); return 'tool completion';}},
    postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
  try {
    assert.equal((await coordinator.receive(inbound('held-first', 'Remind me to review the first packet.'), context)).status, 'held');
    f.loseCreate = false;
    await coordinator.receive(inbound('fresh-second', 'Remind me to review the second packet.'), context);
    assert.deepEqual(contexts, [true, true]); assert.equal(f.calls.filter(row => row.operation === 'create').length, 2);
    assert.equal(sent.length, 2);
    assert.match(sent[0]!, /^I couldn’t confirm whether the reminder change happened\./,
      'the original uncertain request is acknowledged before another owner turn');
    assert.match(sent[1]!, /Reminder set\./);
    const actions = f.journal.db.prepare("SELECT status FROM actions WHERE kind='owner_reminder_tool' ORDER BY created_at,id").all();
    assert.deepEqual(actions.map(row => row.status), ['unknown', 'succeeded']);
  } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('client binds canonical receipt fields including microseconds and accepts equivalent normalized offsets only', async () => {
  const eventId = 'a'.repeat(64), payloadHash = 'b'.repeat(64);
  const precise = {due: '2026-11-01T01:30:00.123456-07:00', action: 'Review'};
  const binding = stableActionIdentity(eventId, ownerReminderToolKind, precise);
  const request = {actionId: binding.actionId, eventId, payloadHash, text: 'Remind me to review.',
    admittedAtMs: 1791540000000, providerThreadId: 'thread', args: precise};
  let changed: Record<string, unknown> = {};
  const client = createOwnerReminderToolClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async () => reply({version: 1, status: 'committed', action_id: binding.actionId, event_id: eventId,
      payload_hash: payloadHash, args_hash: binding.payloadHash, reminder_id: `int_${'f'.repeat(32)}`,
      reminder_status: 'scheduled', due: '2026-11-01T08:30:00.123456Z', action: precise.action, ...changed})});
  assert.equal((await client.call('create', request)).status, 'committed');
  for (const patch of [{due: '2026-11-01T08:30:00.123999Z'}, {due: '2026-11-01T08:30:00.123456'},
    {due: '2026-02-30T08:30:00.123456Z'}, {action: 'Altered'}, {args_hash: '0'.repeat(64)},
    {event_id: '0'.repeat(64)}, {payload_hash: '0'.repeat(64)}, {action_id: '0'.repeat(64)}]) {
    changed = patch; await assert.rejects(client.call('create', request), /unavailable/);
  }
});


test('read-only list needs current owner admission, not a mutation grant; final authority await cannot return cancelled data', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-reminder-list-current-')), f = fixture(folder);
  let reads = 0, runId = '', result: unknown;
  f.sourceAuthority.read = async () => {
    reads++;
    if (reads === 2) f.journal.cancelRun(runId);
    return authority;
  };
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy,
    model: {generate: async request => {
      runId = request.runId;
      await assert.rejects(f.callTool('owner_reminder_list', {}, request, {mode: 'unattended'}));
      await assert.rejects(f.callTool('owner_reminder_list', {}, request, {ownerRequestKey: 'foreign'}));
      reads = 0;
      await assert.rejects(f.callTool('owner_reminder_list', {}, request, {allowOwnerReminder: false}), /unavailable/);
      result = 'cancelled'; return 'must not post';
    }}, postPrepared: async () => {assert.fail('cancelled list cannot post');}});
  try {
    await coordinator.receive(inbound('list-current', 'Which reminders do I have?'), context);
    assert.equal(result, 'cancelled');
    assert.equal(f.calls.length, 1); assert.equal(f.calls[0]!.operation, 'list');
    assert.equal(f.journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 0);
  } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('list client binds original event and validates scheduled rows, including bounded Unicode output', async () => {
  const eventId = 'a'.repeat(64), payloadHash = 'b'.repeat(64);
  const input = {actionId: stableActionIdentity(eventId, ownerReminderToolKind, null).actionId,
    eventId, payloadHash, text: 'List my reminders', admittedAtMs: Date.now(), providerThreadId: 'thread', args: null};
  const item = {id: `int_${'c'.repeat(32)}`, due: '2026-10-05T12:00:00Z', action: '🌲'.repeat(500), status: 'scheduled'};
  const valid = {version: 1, status: 'listed', event_id: eventId, payload_hash: payloadHash,
    zone: 'America/Los_Angeles', items: Array.from({length: 50}, (_, i) => ({...item, id: `int_${i.toString(16).padStart(32, '0')}`}))};
  let response: unknown = valid;
  const client = createOwnerReminderToolClient({identity, bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64),
    fetch: async () => new Response(JSON.stringify(response).replace(/[\uD800-\uDFFF]/g, char => `\\u${char.charCodeAt(0).toString(16)}`))});
  assert.equal((await client.call('list', input)).status, 'listed');
  for (const invalid of [{...valid, event_id: 'd'.repeat(64)}, {...valid, items: [{...item, status: 'fired'}]},
    {...valid, items: [{...item, due: '2026-02-30T12:00:00Z'}]}, {...valid, items: Array(51).fill(item)},
    {...valid, items: [{...item, unexpected: true}]}]) {
    response = invalid; await assert.rejects(client.call('list', input), /unavailable/);
  }
});


test('cancel receipt requires terminal status; late accepted delivery may truthfully be fired', async () => {
  const args = {operation: 'cancel' as const, reminder_id: `int_${'c'.repeat(32)}`, cancellation: 'Cancel cedar'};
  const eventId = 'a'.repeat(64), binding = stableActionIdentity(eventId, ownerReminderToolKind, args);
  const request = {actionId: binding.actionId, eventId, payloadHash: 'b'.repeat(64), text: 'Cancel cedar',
    admittedAtMs: Date.now(), providerThreadId: 'thread', args};
  let status = 'scheduled';
  const client = createOwnerReminderToolClient({identity, bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64),
    fetch: async () => reply({version: 1, status: 'committed', operation: 'cancel', action_id: binding.actionId,
      event_id: eventId, payload_hash: request.payloadHash, args_hash: binding.payloadHash,
      reminder_id: args.reminder_id, reminder_status: status, due: '2026-10-05T12:00:00Z', action: 'Review cedar'})});
  await assert.rejects(client.call('cancel', request), /unavailable/);
  for (const terminal of ['canceled', 'fired']) {status = terminal; assert.equal((await client.call('inspect', request)).status, 'committed');}
});

test('conditional reminder selects exact waiting-on proof; original succeeded action bypasses changed loop lookup', async () => {
 const folder = mkdtempSync(join(tmpdir(), 'sotto-conditional-proof-')), f = fixture(folder);
 let generated = 0;
 const args = {operation: 'conditional' as const, due: new Date(Date.now() + 3600000).toISOString(),
  action: 'Check Dana reply', condition: 'if Dana has not replied', ...f.loop};
 const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy,
  model: {generate: async request => {
   generated++;
   for (const bad of [{...args, loop_version: 'f'.repeat(64)}, {...args, anchor_key: 'foreign'}, {...args, condition: 'invented condition'}])
    await assert.rejects(f.callTool('owner_reminder_conditional', bad, request));
   assert.equal(f.calls.length, 0); assert.equal(f.journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 0);
   const recorded = await f.callTool('owner_reminder_conditional', args, request);
   assert.equal((recorded as {status: string}).status, 'recorded');
   const reads = f.loopReads; f.clearLoops(); f.revoke();
   await f.callTool('owner_reminder_conditional', args, request);
   assert.equal(f.loopReads, reads, 'immutable receipt is not hidden by a terminalized loop');
   assert.equal(f.calls.length, 1); return 'not guessed acknowledgment';
  }}, postPrepared: async request => {assert.match(request.text, /Conditional reminder set\./); return {acceptance: 'accepted', messageId: 'conditional-ack'};}});
 try {
  await coordinator.receive(inbound('conditional-proof', 'Remind me tomorrow if Dana has not replied'), context);
  assert.equal(generated, 1);
  const body = f.calls[0]! as {args: {source_revision: string}};
  assert.equal(body.args.source_revision, authority.revision);
 } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

for (const terminal of ['canceled', 'fired'] as const) {
 test(`recovered reminder acknowledgment uses actual ${terminal} receipt without technical metadata`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-reminder-terminal-')), f = fixture(folder), sent: string[] = [];
  f.loseCreate = true; f.hideInspect = true;
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client,
   model: {generate: async request => {
    await f.callTool('owner_reminder_create', {due: new Date(Date.now() + 3600000).toISOString(), action: 'Call Dana'}, request);
    return 'unused';
   }}, postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: 'terminal-ack'};}});
  try {
   assert.equal((await coordinator.receive(inbound('terminal-' + terminal, 'Remind me to call Dana'), context)).status, 'held');
   assert.match(sent[0]!, /^I couldn’t confirm whether the reminder change happened\./);
   f.terminal(terminal); f.hideInspect = false;
   f.journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_reminder_tool' AND status='unknown'").run();
   assert.equal((await coordinator.recover())[0]?.status, 'accepted');
   assert.equal(sent.length, 2);
   assert.equal(sent[1], terminal === 'fired' ? 'That reminder was already sent.' : 'That reminder is canceled.');
   assert.equal(f.calls.filter(call => call.operation === 'create').length, 1);
  } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
 });
}

test('/stop after an unknown reminder acknowledges the control once; later safe chat and late proof do not ACK the action', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-natural-stopped-')), f = fixture(folder), sent: string[] = [];
  f.loseCreate = true; f.hideInspect = true;
  const generated: string[] = [];
  const model = {generate: async (request: ModelRequest) => {
    if (request.text.startsWith('Remind')) {await f.callTool('owner_reminder_create', args, request); return 'not acknowledged';}
    generated.push(request.text); return 'safe answer';}};
  const postPrepared = async (request: {text: string}) => {sent.push(request.text); return {acceptance: 'accepted' as const, messageId: `ack-${sent.length}`};};
  let coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client, model, postPrepared});
  try {
    assert.equal((await coordinator.receive(inbound('unknown', 'Remind me in one hour to review the packet.'), context)).status, 'held');
    assert.equal((await coordinator.receive(inbound('stop', '/stop'), context)).status, 'stopped');
    const action = f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_reminder_tool'").get()!;
    const inspects = () => f.calls.filter(row => row.operation === 'inspect').length;
    const originalInspects = inspects();
    assert.equal(originalInspects, 1, 'the original uncertain request inspects proof before its notice');
    // Proof is still unavailable: the unknown effect stays recorded, but it cannot wedge unrelated owner chat.
    f.journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.id);
    assert.equal((await coordinator.receive(inbound('safe-1', 'What is next?'), context)).status, 'accepted');
    assert.equal(f.journal.getAction(action.id as string)?.status, 'unknown');
    assert.equal(inspects(), originalInspects + 1, 'the parked action is still inspected');
    assert.equal((await coordinator.receive(inbound('safe-2', 'Hello?'), context)).status, 'accepted');
    assert.equal(inspects(), originalInspects + 1, 'a parked action is probed at most once a minute, not on every turn');
    await coordinator.close(); f.reopen();
    coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerReminderTool: f.client, model, postPrepared});
    assert.deepEqual(await coordinator.recover(), []);
    // A late receiver proof is still reconciled, but the stopped request is never acknowledged or recreated.
    f.hideInspect = false;
    f.journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.id);
    assert.equal((await coordinator.receive(inbound('safe-3', 'Anything else?'), context)).status, 'accepted');
    assert.equal(f.journal.getAction(action.id as string)?.status, 'succeeded');
    assert.equal(inspects(), originalInspects + 2);
    assert.equal((await coordinator.receive(inbound('safe-4', 'Thanks'), context)).status, 'accepted');
    assert.equal(inspects(), originalInspects + 2, 'a settled action is never inspected again');
    assert.deepEqual(generated, ['What is next?', 'Hello?', 'Anything else?', 'Thanks']);
    assert.equal(sent.filter(text => text.startsWith('I stopped working on your earlier request.')).length, 1);
    assert.deepEqual(sent.filter(text => text === 'safe answer'), ['safe answer', 'safe answer', 'safe answer', 'safe answer']);
    assert.equal(sent.filter(text => text.startsWith('I couldn’t confirm whether the reminder change happened.')).length, 1);
    assert.equal(sent.length, 6, 'the original notice is retained, but late stopped-effect proof has no separate acknowledgement');
    assert.equal(f.calls.filter(row => row.operation === 'create').length, 1);
    assert.equal(f.journal.getInbound(action.source_event_id as string)?.outcome, 'cancelled');
  } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('acknowledgement exception logs no owner or error content', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-ack-diagnostic-')), f=fixture(folder);
  const lines:string[]=[];
  const original=console.error;
  console.error=(...parts:unknown[])=>{lines.push(parts.map(String).join(' '));};
  const coordinator=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerReminderTool:f.client,
    model:{generate:async request=>{
      await f.callTool('owner_reminder_create',args,request);
      f.journal.readOwnerReminderToolAction=(()=>{throw Error('private prompt and provider body');}) as typeof f.journal.readOwnerReminderToolAction;
      return 'unused';
    }},postPrepared:async()=>{throw Error('must not send');}});
  try {
    assert.equal((await coordinator.receive(inbound('ack-diagnostic','Remind me to call Dana'),context)).status,'held');
    assert.deepEqual(lines,['sotto owner acknowledgement held by exception']);
    assert.doesNotMatch(lines.join(' '),/private prompt|provider body|Remind me/);
  } finally {console.error=original;await coordinator.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('acknowledgement failure gives one bounded notice and later chat continues', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-ack-notice-')),f=fixture(folder),sent:string[]=[];
  const coordinator=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerReminderTool:f.client,
    model:{generate:async request=>{
      if(request.text.startsWith('Remind')) {
        await f.callTool('owner_reminder_create',args,request);
        f.journal.readOwnerReminderToolAction=(()=>{throw Error('private prompt');}) as typeof f.journal.readOwnerReminderToolAction;
        return 'unused';
      }
      return 'safe answer';
    }},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`sent-${sent.length}`};}});
  const original=console.error; console.error=()=>{};
  try {
    assert.equal((await coordinator.receive(inbound('ack-failure','Remind me to call Dana'),context)).status,'accepted');
    assert.match(sent[0]!,/couldn’t confirm the result of that request/);
    assert.equal((await coordinator.receive(inbound('safe-after-ack','What is next?'),context)).status,'accepted');
    assert.deepEqual(sent,[sent[0]!,'safe answer']);
    await coordinator.recover();
    assert.equal(sent.length,2);
  } finally {console.error=original;await coordinator.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('prepared acknowledgement-exception notice resumes its original dispatch', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-ack-notice-restart-')),f=fixture(folder);
  const original=console.error;const lines:string[]=[];console.error=(...v:unknown[])=>{lines.push(v.map(String).join(' '));};
  let sends=0;
  const first=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerReminderTool:f.client,
    model:{generate:async request=>{
      await f.callTool('owner_reminder_create',args,request);
      f.journal.readOwnerReminderToolAction=(()=>{throw Error('private error');}) as typeof f.journal.readOwnerReminderToolAction;
      return 'unused';
    }},canPostPrepared:async()=>false,postPrepared:async()=>{sends++;throw Error('early notice send');}});
  try {
    assert.equal((await first.receive(inbound('ack-prepared','Remind me to call Dana'),context)).status,'held');
    const action=f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_reminder_tool'").get()!;
    const source=action.source_event_id as string;
    const prior=f.journal.getReplyForEvent(source)!;
    assert.ok(prior);
    assert.equal(prior.dispatch_status,'prepared');
    await first.close();f.reopen();
    f.journal.readOwnerReminderToolAction=(()=>{throw Error('private error');}) as typeof f.journal.readOwnerReminderToolAction;
    const second=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerReminderTool:f.client,
      model:{generate:async()=>{throw Error('model must not run');}},
      postPrepared:async post=>{sends++;assert.equal(post.dispatchId,prior.dispatch_id);
        return {acceptance:'accepted',messageId:'notice'};}});
    try {
      assert.equal((await second.recover())[0]?.status,'accepted');
      assert.equal(sends,1);
      assert.equal(f.journal.listRepliesForThread(source).length,1);
      await second.recover();assert.equal(sends,1);
    } finally {await second.close();}
  } finally {console.error=original;await first.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
