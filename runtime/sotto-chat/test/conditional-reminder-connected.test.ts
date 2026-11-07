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
import { createOwnerTaskToolClient } from '../src/tools/owner-task-tool-client.js';
import { createKnowledgeCommand, LOOPS_EXECUTION_GUIDE } from '../src/tools/knowledge-command.js';
import { createRunScriptTool } from '../src/tools/run-script-tool.js';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';

async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
      SOTTO_TEST_CONDITIONAL_REMINDER: '1', SOTTO_DATA: folder, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'tenant', SOTTO_ACCOUNT_ID: 'account',
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
    const parsed = JSON.parse(line) as {port: number; identity: OwnerToolIdentity; bearer: string; readBearer: string};
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


async function childCommand(child: ChildProcessWithoutNullStreams, command: string): Promise<any> {
  return new Promise((done, fail) => {
    let output = '';
    const timer = setTimeout(() => {child.stdout.off('data', read); fail(new Error('child command timeout'));}, 10000);
    const read = (chunk: Buffer) => {
      output += String(chunk);
      for (const line of output.split('\n')) {
        if (!line.startsWith('{')) continue;
        try {const value = JSON.parse(line); clearTimeout(timer); child.stdout.off('data', read); done(value); return;} catch { /* partial fixture frame */ }
      }
    };
    child.stdout.on('data', read); child.stdin.write(command + '\n');
  });
}

test('original Intentions exact pending loop -> conditional reminder -> real due scan/outbox once; resolved and revoked hold; loss/reopen inspect-only', async () => {
 const folder = mkdtempSync(join(tmpdir(), 'sotto-conditional-connected-'));
 let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
 let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
 let runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
 let coordinator: ConversationCoordinator | undefined;
 let step = 0, selected = 'waiting-dana', lose = true;
 const clock = new Date(Date.now() + 86400000); clock.setUTCHours(20, 0, 0, 0);
 const due = clock.toISOString();
 const calls: string[] = [], sent: string[] = [];
 try {
  receiver = await startReceiver(join(folder, 'receiver'));
  const sourceAuthority = {read: async () => ({version: 2 as const, complete: true, revision: 'a'.repeat(64),
   bridge_sources: [], google_sources: ['gmail' as const]})};
  let rows: Awaited<ReturnType<ReturnType<typeof createOwnerTaskToolClient>['readLoops']>>;
  const makeClient = () => createOwnerReminderToolClient({...receiver!, fetch: async (input, init) => {
   const body = JSON.parse(init!.body as string); calls.push(body.operation);
   const response = await fetch(input, init);
   if (lose && body.operation === 'create' && response.status === 200) {lose = false; await response.text(); throw Error('synthetic response loss');}
   return response;
  }});
  let client = makeClient();
  let loops = createOwnerTaskToolClient({...receiver, sourceAuthority});
  const makeCommand = () => createKnowledgeCommand({...receiver!, sourceAuthority,
   isCurrent: (runId, eventId) => journal.isRunCurrent(runId) && journal.getRun(runId)?.event_id === eventId,
   fetch: async (input, init) => {const response = await fetch(input, init); rows = await response.clone().json(); return response;}});
  let command = makeCommand();
  const makeRuntime = () => createSottoAgentRuntime({storagePath: join(folder, 'memory.db'), sourceAuthority,
   instructions: LOOPS_EXECUTION_GUIDE + ' Use original Intentions. Select one exact waiting-on loop; ask about ambiguity.',
   skillPaths: skills.map(skill => skill.path),
   chatTools: async () => ({...createOwnerReminderTools({client, journal: () => journal, sourceAuthority, readLoops: loops.readLoops}),
    run_sotto_script: createRunScriptTool({mode: 'chat', executeCommand: command,
     commandContext: (ctx, signal) => ({mode: ctx?.get('mode'), runId: ctx?.get('runId'), ownerRequestKey: ctx?.get('ownerRequestKey'),
      sourceRevision: ctx?.get('sourceRevision'), signal})})}),
   model: new MockLanguageModelV4({doGenerate: async options => {
    step++;
    const row = rows?.waiting_on_them.find(row => row.anchor_key === selected);
    const names = ['skill', 'run_sotto_script', 'owner_reminder_context', 'owner_reminder_conditional'];
    const args = step === 1 ? {name: 'sotto-intentions'} : step === 2 ? {id: 'loops-query', args: []} : step === 3 ? {} : {
     operation: 'conditional', due, action: 'Check Dana reply', condition: 'if Dana has not replied',
     anchor_key: row!.anchor_key, loop_version: row!.loop_version, loop_identity: row!.loop_identity};
    if (step === 2) assert.match(JSON.stringify(options.prompt), /owner_reminder_conditional/);
    if (step === 4) {assert.ok(row); assert.match(JSON.stringify(options.prompt), /waiting-dana/);}
    return {content: step <= 4 ? [{type: 'tool-call' as const, toolCallId: `${selected}-${step}`, toolName: names[step - 1]!, input: JSON.stringify(args)}]
     : [{type: 'text' as const, text: 'completion'}], finishReason: {unified: step <= 4 ? 'tool-calls' : 'stop', raw: 'STOP'},
     usage: {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}}, warnings: []};
   }})});
  const skills = await readSkillIndex(resolve('../../sotto-chief-of-staff'));
  const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id};
  const context = {threadId: receiver.identity.route_id, destination: 'photon', lineKey: 'line'};
  const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: ownerPolicy.ownerId,
   isDirect: true, eventId, text});
  runtime = makeRuntime();
  const makeCoordinator = () => new ConversationCoordinator({journal, ownerPolicy, ownerReminderTool: client,
   model: {generate: request => runtime!.generateChat({...request, threadId: 'conditional-memory', ownerId: 'owner'})},
   postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
  coordinator = makeCoordinator();
  const text = 'Remind me tomorrow if Dana has not replied';
  assert.equal((await coordinator.receive(inbound('conditional-lost', text), context)).status, 'held');
  assert.equal(calls.filter(op => op === 'create').length, 1);
  assert.equal(sent.length, 1, 'the immediate receipt inspection produces one confirmation without another owner input');
  assert.match(sent[0]!, /Conditional reminder set\./);
  const action = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_reminder_tool'").get()!;
  assert.equal(action.status, 'succeeded', 'only the committed receipt settles reminder creation');
  assert.ok(calls.includes('inspect'));
  await coordinator.close(); await runtime.close(); journal.close(); await stopReceiver(receiver.child);
  receiver = await startReceiver(join(folder, 'receiver'));
  journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();
  client = makeClient(); loops = createOwnerTaskToolClient({...receiver, sourceAuthority}); command = makeCommand();
  runtime = makeRuntime(); coordinator = makeCoordinator();
  const prior = step;
  assert.deepEqual(await coordinator.recover(), [], 'the already accepted confirmation does not replay on restart');
  assert.equal((await coordinator.receive(inbound('conditional-lost', text), context)).status, 'duplicate');
  assert.equal(step, prior); assert.equal(calls.filter(op => op === 'create').length, 1);
  assert.ok(calls.includes('inspect')); assert.match(sent.at(-1)!, /Conditional reminder set\./);
  assert.equal(journal.getAction(action.id as string)!.status, 'succeeded');
  assert.equal(sent.length, 1, 'restart and duplicate owner input do not send another confirmation');
  const afterProof = sent.length; await coordinator.recover();
  assert.equal(sent.length, afterProof, 'accepted late confirmation is not repeated');
  for (selected of ['waiting-resolved', 'waiting-held']) {
   step = 0; await coordinator.receive(inbound('create-' + selected, text), context);
  }
  await childCommand(receiver.child, 'conditional-state=waiting-resolved:resolved');
  await childCommand(receiver.child, 'conditional-state=waiting-held:snoozed');
  const now = new Date(Date.parse(due) + 1000).toISOString();
  const dueResult = await childCommand(receiver.child, 'conditional-due=' + now);
  assert.equal(dueResult.sends.length, 1, JSON.stringify(dueResult));
  assert.equal(dueResult.funnel.bundle.events[0].class, 'intention');
  assert.equal(dueResult.attempts[0].valid, true); assert.equal(dueResult.attempts[0].attempted, true);
  assert.equal(dueResult.sends[0], 'Check Dana reply');
  assert.equal(dueResult.rows.find((row: any) => row.anchor_key === 'waiting-held').status, 'scheduled');
  assert.equal(dueResult.rows.find((row: any) => row.anchor_key === 'waiting-resolved').status, 'canceled');
  await childCommand(receiver.child, 'conditional-state=waiting-held:open');
  await childCommand(receiver.child, 'conditional-revoke');
  const repeated = await childCommand(receiver.child, 'conditional-due=' + now);
  assert.equal(repeated.sends.length, 0);
  assert.equal(repeated.rows.find((row: any) => row.anchor_key === 'waiting-held').status, 'scheduled');
 } finally {
  await coordinator?.close(); await runtime?.close(); journal.close(); if (receiver) await stopReceiver(receiver.child);
  rmSync(folder, {recursive: true, force: true});
 }
});
