import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync, readFileSync, readdirSync} from 'node:fs';
import {once} from 'node:events';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {readSkillIndex} from '../src/tools/skills.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import {createSkillCommand, SHARED_SKILL_EXECUTION_GUIDE} from '../src/tools/skill-command.js';
import {createOwnerSkillExecution} from '../src/tools/owner-skill-execution.js';
import {createOwnerSkillRequestClient, ownerSkillRequestKind} from '../src/tools/owner-skill-request-client.js';
async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_RELATIONSHIP_PULSE: '1', SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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




const usage = {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}};

const sourceAuthority = {read: async () => ({version: 2 as const, complete: true, revision: 'a'.repeat(64), bridge_sources: ['imessage' as const, 'contacts' as const], google_sources: ['gmail' as const], connector_sources: []})};
async function execute(child: ChildProcessWithoutNullStreams) {
  return new Promise<Record<string, unknown>>((done, fail) => {
    let output = '';
    const timer = setTimeout(() => fail(new Error('pulse worker timeout')), 30000);
    const listener = (chunk: Buffer) => {
      output += String(chunk);
      for (const line of output.split('\n')) {
        try {
          const result = JSON.parse(line);
          if ('executed' in result) {clearTimeout(timer); child.stdout.off('data', listener); done(result);}
        } catch {/* child diagnostics are not receipts */}
      }
    };
    child.stdout.on('data', listener);
    child.stdin.write('execute-relationship-pulse\n');
  });
}
test('original relationship pulse native admission, real gather/compute/persist/outbox and lost receipt reopen once', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-pulse-http-')), data = join(folder, 'receiver');
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  let runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
  let coordinator: ConversationCoordinator | undefined;
  try {
    receiver = await startReceiver(data);
    let blocked = true, admissions = 0;
    const client = createOwnerSkillRequestClient({...receiver, fetch: async (input, init) => {
      const body = JSON.parse(init!.body as string);
      if (body.operation === 'inspect' && blocked) throw new Error('inspection unavailable');
      const response = await fetch(input, init);
      if (body.operation === 'admit') {admissions++; assert.equal(response.status, 200); await response.body?.cancel(); throw new Error('committed receipt lost');}
      return response;
    }});
    const command = createSkillCommand(createOwnerSkillExecution({client, journal: () => journal, sourceAuthority}));
    const script = createRunScriptTool({mode: 'chat', executeCommand: command, commandContext: (ctx, signal) => ({mode: ctx?.get('mode'), runId: ctx?.get('runId'), ownerRequestKey: ctx?.get('ownerRequestKey'), allowOwnerSkill: ctx?.get('allowOwnerSkill'), sourceRevision: ctx?.get('sourceRevision'), signal})});
    let step = 0;
    const model = new MockLanguageModelV4({doGenerate: async request => {
      if (step === 1) assert.ok(JSON.stringify(request.prompt).includes('run_sotto_script relationship-pulse'));
      const call = step++ === 0 ? {name: 'skill', input: {name: 'sotto-relationship-pulse'}} : {name: 'run_sotto_script', input: {id: 'relationship-pulse', args: []}};
      return {content: [{type: 'tool-call' as const, toolCallId: 'pulse-' + step, toolName: call.name, input: JSON.stringify(call.input)}], finishReason: {unified: 'tool-calls' as const, raw: 'STOP'}, usage, warnings: []};
    }});
    runtime = createSottoAgentRuntime({model, storagePath: join(folder, 'memory.db'), instructions: SHARED_SKILL_EXECUTION_GUIDE, skillPaths: (await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(x => x.path), sourceAuthority, chatTools: () => ({run_sotto_script: script})});
    const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id};
    const context = {threadId: receiver.identity.route_id, destination: 'photon', lineKey: 'line'};
    const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: ownerPolicy.ownerId, isDirect: true, eventId, text});
    const sent: string[] = [];
    const make = (safe = false) => new ConversationCoordinator({journal, ownerPolicy, ownerSkillRequest: client, model: {generate: request => safe ? Promise.resolve('Safe hello') : runtime!.generateChat({...request, threadId: 'pulse-memory', ownerId: 'owner'})}, postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: 'pulse-ack-' + sent.length};}});
    coordinator = make();
    assert.equal((await coordinator.receive(inbound('pulse', 'Show my relationship pulse'), context)).status, 'held');
    assert.match(sent[0]!, /I couldn’t confirm whether the skill request/);
    assert.equal(admissions, 1);
    await coordinator.close(); await runtime.close(); runtime = undefined;
    await stopReceiver(receiver.child); receiver = await startReceiver(data);
    journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();
    const reopened = createOwnerSkillRequestClient(receiver);
    client.call = async (operation, input, signal) => {assert.equal(operation, 'inspect'); if (blocked) throw new Error('held'); return reopened.call(operation, input, signal);};
    coordinator = make(true); await coordinator.recover();
    await coordinator.receive(inbound('hello', 'Hello safely'), context); assert.ok(sent.includes('Safe hello'));
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_skill_request' AND status='unknown'").run();
    blocked = false; await coordinator.recover();
    assert.ok(sent.includes('Your relationship pulse is queued. It will arrive separately.'));
    const completed = await execute(receiver.child);
    assert.equal((completed.jobs as {status: string}).status, 'done', JSON.stringify(completed));
    const statePath = join(data, 'knowledge/relationship_state.json');
    const state = readFileSync(statePath, 'utf8');
    assert.ok(state.includes('Dana'), state);
    const sends = readFileSync(join(data, 'synthetic-sends.jsonl'), 'utf8');
    assert.equal(sends.trim().split('\n').length, 1);
    assert.ok(sends.includes('Dana'), sends);
    const checkpointNames = readdirSync(join(data, 'events/research-artifacts')).filter(name => /^pulse-.*\.json$/.test(name));
    assert.equal(checkpointNames.length, 1);
    const checkpoint = JSON.parse(readFileSync(join(data, 'events/research-artifacts', checkpointNames[0]!), 'utf8'));
    const delivered = JSON.parse(sends.trim()).text as string;
    assert.ok(delivered.endsWith(checkpoint.compute.result.pulse_markdown), delivered);
    assert.ok(delivered.includes('Cedar deck') && delivered.includes('review and reply if still relevant'), delivered);
    assert.ok(delivered.includes('No later response found in the available history'), delivered);
    assert.ok(!delivered.includes('Unanswered') && !delivered.includes('Waiting on you'), delivered);
    assert.ok(checkpoint.compute.result.attention_queue.length <= 5);
    assert.equal(checkpoint.persist.status, 'completed');
    const google = readFileSync(join(data, 'pulse-google-calls.jsonl'), 'utf8');
    // Bounded refresh: one both-direction metadata search since the last observation (no separate
    // 42-day sent lane), and a Bridge read bounded to the snapshot TTL rather than 1008 hours.
    assert.ok(google.includes('after:') && google.includes('before:') && google.includes('/messages/pulse-inbound') && !google.includes('in:sent'), google);
    assert.equal(google.split('\n').filter(line => line.includes('"/gmail/v1/users/me/messages"')).length, 1, google);
    assert.ok(readFileSync(join(data, 'pulse-bridge-calls.jsonl'), 'utf8').includes('"since_hours": 25'));
    await execute(receiver.child);
    await coordinator.close(); await stopReceiver(receiver.child); receiver = await startReceiver(data);
    await execute(receiver.child);
    assert.equal(readFileSync(statePath, 'utf8'), state);
    assert.equal(readFileSync(join(data, 'synthetic-sends.jsonl'), 'utf8'), sends);
    assert.equal(readFileSync(join(data, 'pulse-google-calls.jsonl'), 'utf8'), google);
    assert.equal(admissions, 1);
    assert.equal(journal.db.prepare('SELECT COUNT(*) AS n FROM actions WHERE kind=?').get(ownerSkillRequestKind)!.n, 1);
  } finally {
    await coordinator?.close(); await runtime?.close(); journal.close();
    if (receiver) await stopReceiver(receiver.child);
    rmSync(folder, {recursive: true, force: true});
  }
});
