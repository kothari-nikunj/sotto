import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { MockLanguageModelV4 } from 'ai/test';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator } from '../src/conversation.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createOwnerPreferenceToolClient } from '../src/tools/owner-preference-tool-client.js';
import { createOwnerPreferenceTools } from '../src/tools/owner-preference-tool.js';
import { createRunScriptTool } from '../src/tools/run-script-tool.js';
import { createPreferenceCommand, SKILL_COMMAND_EXECUTION_GUIDE } from '../src/tools/preference-command.js';
import { createOwnerPreferenceExecution } from '../src/tools/owner-preference-execution.js';
import { readSkillIndex } from '../src/tools/skills.js';
import type { OwnerPreferenceToolArgs } from '../src/tools/owner-preference-tool-contract.js';
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



const usage = {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}};
const sourceAuthority = {read: async () => ({version: 2 as const, revision: 'a'.repeat(64), complete: true, bridge_sources: [], google_sources: []})};

for (const commandAdapter of [false, true]) test(`actual Mastra ${commandAdapter ? 'loaded feedback skill command' : 'typed preferences'} read/add/remove/budget -> locked receiver store; explicit-only projection and original receipts survive later edits and restart`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-pref-connected-'));
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined, runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver'));
    const preferencesPath = join(folder, 'receiver/preferences.json');
    writeFileSync(preferencesPath, JSON.stringify({learned: {tone: 'secret inferred value'}, unrelated: {keep: true},
      owner_quiet_receipts: {prior: {snooze_until: ''}}, explicit: {retired_scalar: 'morning', nudge_snooze_until: '', tone_notes: ['Keep it terse']}}), {mode: 0o600});
    const client = createOwnerPreferenceToolClient(receiver), tools = createOwnerPreferenceTools({client, journal: () => journal, sourceAuthority});
    const command = createRunScriptTool({mode: 'chat',
      commandContext: (context, signal) => ({mode: context?.get('mode'), runId: context?.get('runId'), ownerRequestKey: context?.get('ownerRequestKey'), allowOwnerPreference: context?.get('allowOwnerPreference'), sourceRevision: context?.get('sourceRevision'), signal}),
      executeCommand: createPreferenceCommand({execution: createOwnerPreferenceExecution({client, journal: () => journal, sourceAuthority})}),
    });
    let step = 0;
    const calls = [
      {name: 'owner_preferences', args: {}}, {name: 'owner_preference_change', args: {operation: 'add', field: 'mute_senders', value: 'News@Example.COM'}}, null,
      {name: 'owner_preferences', args: {}}, {name: 'owner_preference_change', args: {operation: 'remove', field: 'mute_senders', value: 'news@example.com'}}, null,
      {name: 'owner_preference_change', args: {operation: 'nudge-budget', direction: 'no'}}, null,
      {name: 'owner_preferences', args: {}}, {name: 'owner_preference_change', args: {operation: 'remove', field: 'tone_notes', value: 'Keep it terse'}}, null,
    ];
    if (commandAdapter) {
      for (const call of calls) if (call) {
        const args = call.args as Record<string, string>;
        const argv = call.name === 'owner_preferences' ? ['show'] : args.operation === 'nudge-budget' ? ['nudge-budget', args.direction!] : args.field === 'tone_notes' ? ['remove-tone', args.value!] : [args.operation === 'add' ? 'mute-sender' : 'unmute-sender', args.value!];
        call.name = 'run_sotto_script'; call.args = {id: 'preferences', args: argv} as unknown as typeof call.args;
      }
      calls.unshift({name: 'skill', args: {name: 'sotto-feedback'} as unknown as Record<string, unknown>},
        {name: 'skill_read', args: {skillName: 'sotto-feedback', path: 'references/_shared/references/approval-tiers.md'} as unknown as Record<string, unknown>});
    }
    const model = new MockLanguageModelV4({doGenerate: async request => {
      if (commandAdapter && step === 0) {
        const names = request.tools?.map(tool => tool.name) ?? [];
        for (const name of ['skill', 'skill_read', 'run_sotto_script', 'owner_preferences', 'owner_preference_change']) assert.ok(names.includes(name), name);
      }
      if (commandAdapter && step === 1) assert.ok(JSON.stringify(request.prompt).includes('Sotto — Feedback & corrections'), 'native skill must expose original skill body');
      if (commandAdapter && step === 2) assert.ok(JSON.stringify(request.prompt).includes('A Gmail draft is not a send'), 'native skill_read must return shared reference');
      const call = calls[step++];
      return {content: call ? [{type: 'tool-call' as const, toolCallId: `call-${step}`, toolName: call.name, input: JSON.stringify(call.args)}]
        : [{type: 'text' as const, text: 'model completion'}], finishReason: {unified: call ? 'tool-calls' : 'stop', raw: 'STOP'}, usage, warnings: []};
    }});
    runtime = createSottoAgentRuntime({model, storagePath: join(folder, 'memory.db'), instructions: 'Use explicit preferences\n' + SKILL_COMMAND_EXECUTION_GUIDE, skillPaths: commandAdapter ? (await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(skill => skill.path) : [], sourceAuthority, chatTools: async () => ({...tools, ...(commandAdapter ? {run_sotto_script: command} : {})})});
    const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id};
    const context = {threadId: receiver.identity.route_id, destination: 'photon', lineKey: 'line'};
    const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: ownerPolicy.ownerId, isDirect: true, eventId, text});
    const sent: string[] = [];
    const coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerPreferenceTool: client,
      model: {generate: request => runtime!.generateChat({...request, threadId: 'memory', ownerId: 'memory-owner'})},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    const sentence = 'Mute sender News@Example.COM';
    assert.equal((await coordinator.receive(inbound('mute', sentence), context)).status, 'accepted');
    assert.deepEqual(JSON.parse(readFileSync(preferencesPath, 'utf8')).explicit.mute_senders, ['news@example.com']);
    const saved = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_preference_tool'").get()!;
    const event = journal.getInbound(saved.source_event_id as string)!;
    const original = {actionId: saved.id as string, eventId: saved.source_event_id as string, payloadHash: saved.source_payload_hash as string,
      text: sentence, admittedAtMs: event.received_at as number, providerThreadId: context.threadId,
      args: JSON.parse(saved.result_json as string).args as OwnerPreferenceToolArgs};
    const firstReceipt = await client.call('inspect', original); assert.equal(firstReceipt.status, 'committed');
    const projection = await client.call('read', {...original, args: null,
      actionId: stableActionIdentity(original.eventId, 'owner_preference_tool', null).actionId});
    assert.equal(projection.status, 'read'); assert.ok(!JSON.stringify(projection).includes('learned') && !JSON.stringify(projection).includes('retired_scalar') && !JSON.stringify(projection).includes('owner_quiet_receipts'));
    assert.equal((await coordinator.receive(inbound('unmute', 'Unmute that sender'), context)).status, 'accepted');
    assert.deepEqual(JSON.parse(readFileSync(preferencesPath, 'utf8')).explicit.mute_senders, []);
    assert.deepEqual(await client.call('inspect', original), firstReceipt, 'original committed outcome retained after later remove');
    assert.equal((await coordinator.receive(inbound('no-nudges', 'No unsolicited nudges'), context)).status, 'accepted');
    assert.match(sent[2]!, /nudge limit: 0/); assert.match(sent[2]!, /Scheduled briefs and digests continue/);
    assert.equal((await coordinator.receive(inbound('remove-tone', 'Remove that tone note'), context)).status, 'accepted');
    const final = JSON.parse(readFileSync(preferencesPath, 'utf8'));
    assert.deepEqual(final.explicit.tone_notes, []); assert.equal(final.explicit.nudge_budget, '0'); assert.equal(final.explicit.retired_scalar, 'morning');
    assert.deepEqual(final.learned, {tone: 'secret inferred value'}); assert.deepEqual(final.owner_quiet_receipts, {prior: {snooze_until: ''}}); assert.deepEqual(final.unrelated, {keep: true});
    const changed: OwnerPreferenceToolArgs = {operation: 'clear-tone'};
    assert.deepEqual(await client.call('apply', {...original, args: changed,
      actionId: stableActionIdentity(original.eventId, 'owner_preference_tool', changed).actionId}), {version: 1, status: 'rejected', code: 'event_conflict'});
    assert.deepEqual(sent.slice(0, 2), ['Recorded your explicit sender mute.', 'Removed your explicit sender mute.']);
    assert.equal(step, calls.length);
    assert.ok(!JSON.stringify(model.doGenerateCalls).includes('secret inferred value'));
    const terminal = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_preference_tool' ORDER BY id").all();
    await coordinator.close(); await runtime.close(); journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();
    const recovered = new ConversationCoordinator({journal, ownerPolicy, ownerPreferenceTool: client, model: {generate: async () => {throw new Error('regenerated');}}, postPrepared: async () => {throw new Error('resent');}});
    assert.deepEqual(await recovered.recover(), []); assert.equal((await recovered.receive(inbound('mute', sentence), context)).status, 'duplicate');
    assert.deepEqual(journal.db.prepare("SELECT * FROM actions WHERE kind='owner_preference_tool' ORDER BY id").all(), terminal);
  } finally {await runtime?.close(); journal.close(); if (receiver) await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});}
});

test('real preference commit with lost response reopens, permits safe chat and recovers by exact inspection only', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-pref-loss-'));
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver')); let apply = 0, blocked = true; const inspected: unknown[] = [];
    const client = createOwnerPreferenceToolClient({...receiver, fetch: async (input, init) => {
      const body = JSON.parse(init!.body as string);
      if (body.operation === 'inspect') {inspected.push(body.args); if (blocked) return new Response('{}', {status: 503});}
      const result = await fetch(input, init);
      if (body.operation === 'apply') {apply++; assert.equal(result.status, 200); await result.body?.cancel(); throw new Error('lost after commit');}
      return result;
    }});
    const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id};
    const context = {threadId: receiver.identity.route_id, destination: 'photon', lineKey: 'line'};
    const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: ownerPolicy.ownerId, isDirect: true, eventId, text});
    const executeCommand = createPreferenceCommand({execution: createOwnerPreferenceExecution({client, journal: () => journal, sourceAuthority})});
    const noticeAttempts: string[] = [];
    const first = new ConversationCoordinator({journal, ownerPolicy, ownerPreferenceTool: client, model: {generate: async request => {
      await executeCommand({id: 'preferences', args: ['tone', 'Keep it terse']}, {mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey,
        sourceRevision: 'a'.repeat(64), allowOwnerPreference: request.allowOwnerPreference, signal: request.signal}); return 'no';
    }}, postPrepared: async request => {noticeAttempts.push(request.text);throw new Error('uncertain ack');}});
    const sentence = 'Tone: Keep it terse'; assert.equal((await first.receive(inbound('held', sentence), context)).status, 'held');
    assert.equal(noticeAttempts.length,1);assert.match(noticeAttempts[0]!,/^I couldn’t confirm whether the preference change happened\./);
  assert.equal(journal.db.prepare("SELECT COUNT(*) AS n FROM prepared_replies p JOIN dispatches d ON d.id=p.dispatch_id WHERE p.purpose='outcome_pending' AND d.status='unknown'").get()!.n,1,'a contacted failed notice stays unknown');
    const saved = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_preference_tool'").get()!; assert.equal(saved.status, 'unknown');
    await first.close(); journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();
    const sent: string[] = [];
    const recovered = new ConversationCoordinator({journal, ownerPolicy, ownerPreferenceTool: client, model: {generate: async request => {assert.equal(request.text, 'Hello safely'); return 'safe answer';}},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    assert.equal((await recovered.recover())[0]?.status, 'held'); assert.equal((await recovered.receive(inbound('safe', 'Hello safely'), context)).status, 'accepted');
    blocked = false;
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_preference_tool' AND status='unknown'").run();
    assert.equal((await recovered.recover())[0]?.status, 'accepted'); assert.equal(apply, 1);
    assert.equal(sent.at(-1), 'Recorded your explicit tone note.');
    assert.equal(sent.filter(text => text.startsWith('I couldn’t confirm whether the preference change happened.')).length, 0, 'the contacted unknown notice is not replayed after restart');
    assert.equal(noticeAttempts.length,1);
    for (const args of inspected) assert.deepEqual(args, {operation: 'add', field: 'tone_notes', value: 'Keep it terse'});
    assert.equal((await recovered.receive(inbound('held', sentence), context)).status, 'duplicate');
    await recovered.close();
  } finally {journal.close(); if (receiver) await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});}
});
