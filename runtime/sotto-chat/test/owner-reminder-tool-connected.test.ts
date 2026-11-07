import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { MockLanguageModelV4 } from 'ai/test';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator } from '../src/conversation.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createOwnerReminderToolClient } from '../src/tools/owner-reminder-tool-client.js';
import { createOwnerReminderClient } from '../src/tools/owner-reminder-client.js';
import { createOwnerReminderTools } from '../src/tools/owner-reminder-tool.js';
import { readSkillIndex } from '../src/tools/skills.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';

async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
      SOTTO_DATA: folder, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'tenant', SOTTO_ACCOUNT_ID: 'account',
      SOTTO_LINE_KEY: 'line', SOTTO_MCP_TOKEN: '0123456789abcdef0123456789abcdef',
      PHOTON_HOME_CHANNEL: '+15555550123', PHOTON_ALLOWED_USERS: '+15555550123', PHOTON_PROJECT_ID: 'fixture-project',
      PHOTON_PROJECT_SECRET: 'fixture-secret', PHOTON_ALLOW_ALL_USERS: 'false', SOTTO_IMESSAGE_NUMBER: '+15555550999',
      SOTTO_TIMEZONE: 'America/Los_Angeles'}});
  let stderr = ''; child.stderr.on('data', chunk => {stderr = (stderr + String(chunk)).slice(-4096);});
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const line = await Promise.race([new Promise<string>((done, fail) => {
      let output = ''; child.stdout.on('data', chunk => {output += String(chunk); const i = output.indexOf('\n'); if (i >= 0) done(output.slice(0, i));});
      child.once('exit', code => fail(new Error(`receiver exited ${code}: ${stderr}`))); child.once('error', fail);
    }), new Promise<never>((_, fail) => {timer = setTimeout(() => fail(new Error('receiver start timeout')), 15000);})]);
    const parsed = JSON.parse(line) as {port: number; identity: OwnerToolIdentity; bearer: string};
    return {child, ...parsed, bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp`};
  } catch (error) {child.kill('SIGKILL'); throw error;}
  finally {if (timer) clearTimeout(timer);}
}
async function stopReceiver(child: ChildProcessWithoutNullStreams) {
  if (child.exitCode !== null) return;
  const ended = once(child, 'exit'); child.stdin.end('quit\n');
  const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
  try {await ended;} finally {clearTimeout(timer);}
}

test('original Intentions -> real reminder list across both creation lanes and reopen; scheduled owner items only, no read effects', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-natural-connected-'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver'));
    const client = createOwnerReminderToolClient(receiver), literal = createOwnerReminderClient(receiver);
    const sourceAuthority = {read: async () => ({version: 2 as const, revision: 'a'.repeat(64), complete: true, bridge_sources: [], google_sources: []})};
    const tools = createOwnerReminderTools({client, journal: () => journal, sourceAuthority});
    const due = new Date(Date.now() + 3600000).toISOString(), action = 'Review the cedar packet 🌲'; let step = 0, listing = false, listStep = 0;
    let listEvidence = '';
    const model = new MockLanguageModelV4({doGenerate: async options => {
      if (listing) {
        listStep++;
        listEvidence = JSON.stringify(options.prompt);
        const content = listStep === 1 ? [{type: 'tool-call' as const, toolCallId: `skill-list-${step}`,
          toolName: 'skill', input: JSON.stringify({name: 'sotto-intentions'})}]
          : listStep === 2 ? [{type: 'tool-call' as const, toolCallId: `read-list-${step}`,
            toolName: 'owner_reminder_list', input: '{}'}]
          : [{type: 'text' as const, text: `You have two scheduled reminders: review the cedar packet at ${new Intl.DateTimeFormat('en-US', {timeZone: 'America/Los_Angeles', hour: 'numeric', minute: '2-digit'}).format(new Date(due))}, and call Dana. Times are in America/Los_Angeles.`}];
        if (listStep === 3) {
          assert.match(listEvidence, /same canonical store/);
          assert.match(listEvidence, /owner_reminder_list/);
          assert.ok(listEvidence.includes(due.replace(/(\.\d{3})Z$/, '$1000Z')) || listEvidence.includes(due.replace('.000Z', 'Z')) || listEvidence.includes(due));
          assert.match(listEvidence, /Review the cedar packet/);
          assert.match(listEvidence, /Call Dana/);
          assert.match(listEvidence, /America\/Los_Angeles/);
          assert.doesNotMatch(listEvidence, /foreign fixture reminder|fired fixture reminder/);
        }
        return {content, finishReason: {unified: listStep < 3 ? 'tool-calls' : 'stop', raw: 'STOP'},
          usage: {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}}, warnings: []};
      }
      step++;
      return {content: step <= 2 ? [{type: 'tool-call' as const, toolCallId: `tool-${step}`,
        toolName: step === 1 ? 'owner_reminder_context' : 'owner_reminder_create', input: JSON.stringify(step === 1 ? {} : {due, action})}]
        : [{type: 'text' as const, text: 'model completion'}], finishReason: {unified: step <= 2 ? 'tool-calls' : 'stop', raw: 'STOP'},
        usage: {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}}, warnings: []};
    }});
    runtime = createSottoAgentRuntime({model, storagePath: join(folder, 'memory.db'), instructions: 'Use the original Intentions skill and reminder tools. Dates and times use the returned owner zone.',
      skillPaths: (await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(skill => skill.path), sourceAuthority,
      chatTools: async () => tools});
    const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id};
    const context = {threadId: receiver.identity.route_id, destination: 'photon', lineKey: 'line'};
    const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account',
      senderId: ownerPolicy.ownerId, isDirect: true, eventId, text});
    const sent: string[] = [];
    let coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerReminderTool: client, ownerReminder: literal,
      model: {generate: request => runtime!.generateChat({...request, threadId: 'memory-thread', ownerId: 'memory-owner'})},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    const sentence = 'Remind me in an hour to review the cedar packet.';
    await coordinator.receive(inbound('create-natural', sentence), context);
    assert.equal(step, 3); assert.equal(sent.length, 1); assert.match(sent[0]!, /Reminder set\./);
    const rows = readFileSync(join(folder, 'receiver/intentions.jsonl'), 'utf8').trim().split('\n').map(line => JSON.parse(line));
    assert.equal(rows.length, 1); assert.equal(rows[0].created_by, 'owner_tool'); assert.equal(rows[0].action, action);
    assert.equal(rows[0].owner_create.text_hash, journal.db.prepare("SELECT source_text_hash FROM actions WHERE kind='owner_reminder_tool'").get()!.source_text_hash);
    assert.equal(rows[0].owner_create.domain, 'sotto.owner-reminder-tool.v1'); assert.ok(!('command' in rows[0].owner_create));
    await coordinator.receive(inbound('list-natural', '/reminder list'), context); assert.ok(sent[1]!.includes(action));
    await coordinator.receive(inbound('cancel-natural', `/reminder cancel ${rows[0].id}`), context); assert.match(sent[2]!, /Cancellation recorded/);
    assert.equal(step, 3, 'literal list/cancel did not reach model');
    assert.equal((await coordinator.receive(inbound('create-natural', sentence), context)).status, 'duplicate');
    assert.equal(step, 3);
    const saved = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_reminder_tool'").get()!;
    assert.equal(saved.status, 'succeeded');
    const metadata = JSON.parse(saved.result_json as string);
    const event = journal.getInbound(saved.source_event_id as string)!;
    const original = {actionId: saved.id as string, eventId: saved.source_event_id as string,
      payloadHash: saved.source_payload_hash as string, text: sentence, admittedAtMs: event.received_at as number,
      providerThreadId: context.threadId, args: metadata.args};
    const inspected = await client.call('inspect', original);
    assert.equal(inspected.status, 'committed');
    if (inspected.status === 'committed') assert.equal(inspected.reminder_status, 'canceled');
    assert.deepEqual(await client.call('create', original), inspected, 'receipt replay does not create again');
    const changed = {...metadata.args, action: 'Changed request'};
    const changedId = stableActionIdentity(original.eventId, 'owner_reminder_tool', changed).actionId;
    assert.deepEqual(await client.call('create', {...original, actionId: changedId, args: changed}),
      {version: 1, status: 'rejected', code: 'event_conflict'});
    step = 0;
    await coordinator.receive(inbound('create-natural-again', sentence), context);
    await coordinator.receive(inbound('create-slash', `/reminder at ${new Date(Date.now() + 7200000).toISOString()} Call Dana`), context);
    const seeded = new Promise<void>(done => receiver!.child.stdout.once('data', () => done()));
    receiver.child.stdin.write('seed-reminder-list-exclusions\n'); await seeded;
    const beforeList = readFileSync(join(folder, 'receiver/intentions.jsonl'), 'utf8');
    const actionCount = journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n;
    listing = true;
    await coordinator.receive(inbound('list-in-words', 'What reminders do I have?'), context);
    assert.equal(listStep, 3);
    assert.match(sent.at(-1)!, /two scheduled reminders/);
    assert.ok(sent.at(-1)!.includes(new Intl.DateTimeFormat('en-US', {timeZone: 'America/Los_Angeles', hour: 'numeric', minute: '2-digit'}).format(new Date(due))));
    assert.equal(readFileSync(join(folder, 'receiver/intentions.jsonl'), 'utf8'), beforeList);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, actionCount);
    await coordinator.close();
    journal.close();
    journal = new OperationalJournal(join(folder, 'operations.sqlite'));
    journal.recoverInterrupted();
    await stopReceiver(receiver.child);
    receiver = await startReceiver(join(folder, 'receiver'));
    // The client endpoint is tied to the old port, so reopen through a fresh attending runtime below.
    const reopenedClient = createOwnerReminderToolClient(receiver);
    const reopenedTools = createOwnerReminderTools({client: reopenedClient, journal: () => journal, sourceAuthority});
    await runtime.close();
    listStep = 0;
    runtime = createSottoAgentRuntime({model, storagePath: join(folder, 'reopened-memory.db'),
      instructions: 'Use the original Intentions skill to list reminders in the returned timezone.',
      skillPaths: (await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(skill => skill.path),
      sourceAuthority, chatTools: () => reopenedTools});
    coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerReminderTool: reopenedClient,
      ownerReminder: createOwnerReminderClient(receiver),
      model: {generate: request => runtime!.generateChat({...request, threadId: 'fresh-memory-thread', ownerId: 'memory-owner'})},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    await coordinator.receive(inbound('list-after-reopen', 'Which reminders are scheduled?'), context);
    assert.equal(listStep, 3);
    assert.equal(readFileSync(join(folder, 'receiver/intentions.jsonl'), 'utf8'), beforeList);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, actionCount);
    listing = false;
    await coordinator.close();
  } finally {await runtime?.close(); journal.close(); if (receiver) await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});}
});

test('original Intentions list -> natural cancel -> absent; lost real cancellation response reopens inspect-only', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-cancel-connected-'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
  let coordinator: ConversationCoordinator | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver'));
    const requests: string[] = [], sent: string[] = [];
    let lose = true;
    const makeClient = () => createOwnerReminderToolClient({...receiver!, fetch: async (url, init) => {
      const body = JSON.parse(init!.body as string); requests.push(body.operation);
      const result = await fetch(url, init);
      if (body.operation === 'cancel' && lose) {lose = false; await result.text(); throw new Error('lost response');}
      return result;
    }});
    let client = makeClient();
    const sourceAuthority = {read: async () => ({version: 2 as const, revision: 'a'.repeat(64), complete: true, bridge_sources: [], google_sources: []})};
    const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id};
    const context = {threadId: receiver.identity.route_id, destination: 'photon', lineKey: 'line'};
    const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: ownerPolicy.ownerId, isDirect: true, eventId, text});
    const text = 'Cancel my cedar reminder.';
    let step = 0, target = '', ambiguous = true;
    const usage = {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}};
    const model = new MockLanguageModelV4({doGenerate: async options => {
      step++;
      if (step === 3) {
        assert.match(JSON.stringify(options.prompt), /Review cedar/);
        assert.match(JSON.stringify(options.prompt), /if several reminders fit/i);
      }
      if (ambiguous && step === 3) return {content: [{type: 'text' as const, text: 'Which reminder do you mean: Review cedar or Call Dana?'}],
        finishReason: {unified: 'stop', raw: 'STOP'}, usage, warnings: []};
      const toolName = step === 1 ? 'skill' : step === 2 ? 'owner_reminder_list' : 'owner_reminder_cancel';
      const input = step === 1 ? {name: 'sotto-intentions'} : step === 2 ? {} : {operation: 'cancel', reminder_id: target, cancellation: text};
      return {content: step <= 3 ? [{type: 'tool-call' as const, toolCallId: `cancel-${step}`, toolName, input: JSON.stringify(input)}]
        : [{type: 'text' as const, text: 'Cancellation completed.'}], finishReason: {unified: step <= 3 ? 'tool-calls' : 'stop', raw: 'STOP'}, usage, warnings: []};
    }});
    const build = async () => {
      const tools = createOwnerReminderTools({client, journal: () => journal, sourceAuthority});
      runtime = createSottoAgentRuntime({model, storagePath: join(folder, `memory-${requests.length}.db`), sourceAuthority,
        skillPaths: (await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(skill => skill.path),
        instructions: 'Use the original Intentions skill. Never choose an ambiguous reminder.', chatTools: () => tools});
      coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerReminderTool: client,
        ownerReminder: createOwnerReminderClient(receiver!), model: {generate: request => runtime!.generateChat({...request, threadId: 'cancel-memory', ownerId: 'owner'})},
        postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${requests.length}`};}});
    };
    await build();
    await coordinator!.receive(inbound('create-cancel-target', `/reminder at ${new Date(Date.now() + 3600000).toISOString()} Review cedar`), context);
    target = JSON.parse(readFileSync(join(folder, 'receiver/intentions.jsonl'), 'utf8').trim()).id;
    await coordinator!.receive(inbound('create-second-target', `/reminder at ${new Date(Date.now() + 7200000).toISOString()} Call Dana`), context);
    const beforeAmbiguous = readFileSync(join(folder, 'receiver/intentions.jsonl'), 'utf8');
    await coordinator!.receive(inbound('ambiguous-cancel', 'Cancel that reminder.'), context);
    assert.equal(requests.filter(op => op === 'cancel').length, 0);
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_reminder_tool'").get()!.n, 0);
    assert.equal(readFileSync(join(folder, 'receiver/intentions.jsonl'), 'utf8'), beforeAmbiguous);
    ambiguous = false; step = 0;
    assert.equal((await coordinator!.receive(inbound('cancel-in-words', text), context)).status, 'held');
    const after = readFileSync(join(folder, 'receiver/intentions.jsonl'), 'utf8');
    assert.equal(JSON.parse(after.trim().split('\n').at(-1)!).status, 'canceled');
    assert.equal(requests.filter(op => op === 'cancel').length, 1);
    assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_reminder_tool'").get()!.status, 'succeeded');
    assert.equal(sent.filter(text => text === 'That reminder is canceled.').length, 1,
      'the exact available receipt is acknowledged without another owner turn');
    await coordinator!.close(); await runtime!.close(); journal.close();
    await stopReceiver(receiver.child); receiver = await startReceiver(join(folder, 'receiver'));
    journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();
    client = makeClient(); await build();
    const priorSteps = step;
    assert.deepEqual(await coordinator!.recover(), []);
    assert.equal((await coordinator!.receive(inbound('cancel-in-words', text), context)).status, 'duplicate');
    assert.equal(step, priorSteps, 'unknown recovery never reruns the model or cancellation');
    assert.equal(requests.filter(op => op === 'cancel').length, 1);
    assert.equal(requests.at(-1), 'inspect');
    assert.equal(sent.at(-1), 'That reminder is canceled.');
    assert.equal(readFileSync(join(folder, 'receiver/intentions.jsonl'), 'utf8'), after);
    const action = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_reminder_tool'").get()!;
    const metadata = JSON.parse(action.result_json as string);
    const event = journal.getInbound(action.source_event_id as string)!;
    const original = {request: {actionId: action.id as string, eventId: action.source_event_id as string,
      payloadHash: action.source_payload_hash as string, text, admittedAtMs: event.received_at as number,
      providerThreadId: context.threadId, args: metadata.args}};
    assert.equal(metadata.receipt.operation, 'cancel');
    assert.equal((await client.call('list', {...original.request, args: null,
      actionId: stableActionIdentity(original.request.eventId, 'owner_reminder_tool', null).actionId})).status, 'listed');
    const listed = await client.call('list', {...original.request, args: null,
      actionId: stableActionIdentity(original.request.eventId, 'owner_reminder_tool', null).actionId});
    if (listed.status === 'listed') {assert.equal(listed.items.length, 1); assert.equal(listed.items[0]!.action, 'Call Dana'); assert.ok(!listed.items.some(item => item.id === target));}
  } finally {
    await coordinator?.close(); await runtime?.close(); journal.close();
    if (receiver) await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});
  }
});
