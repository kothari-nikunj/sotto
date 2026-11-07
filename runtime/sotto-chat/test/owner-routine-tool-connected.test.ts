import {readSkillIndex} from '../src/tools/skills.js';
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
import { createOwnerRoutineToolClient } from '../src/tools/owner-routine-tool-client.js';
import { createOwnerRoutineTools } from '../src/tools/owner-routine-tool.js';
import type { OwnerRoutineToolArgs } from '../src/tools/owner-routine-tool-contract.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { RequestContext } from '@mastra/core/request-context';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';
async function startReceiver(folder: string, channel: 'photon' | 'telegram' = 'photon') {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_TELEGRAM_ROUTINE: channel === 'telegram' ? '1' : '0',
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
const usage = { inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 2, text: 2, reasoning: 0 } };
const sourceAuthority = { read: async () => ({ version: 2 as const, revision: 'a'.repeat(64), complete: true, bridge_sources: [], google_sources: [] }) };
for (const [channel,weekday,hourly] of [['photon',null,false],['telegram',null,false],['photon','weekdays',false],['photon',[1,3,5],false],['photon','first_of_month',false],['photon',null,true]] as const) test(`${channel} ${hourly?'hourly':weekday??'daily'}: actual Mastra proposal and separate yes create through real receiver; list/cancel preserve original receipt across restart`, async () => {
  const advanced=weekday!==null||hourly;
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-connected-'));
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined, runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
  try {
    receiver = await startReceiver(join(folder, channel === 'telegram' ? 'data' : 'receiver'), channel);
    const client = createOwnerRoutineToolClient(receiver), tools = createOwnerRoutineTools({ client, journal: () => journal, sourceAuthority });
    const ownerPolicy = { channel, accountId: receiver.identity.account_id, ownerId: receiver.identity.owner_id };
    const context = { threadId: receiver.identity.route_id, destination: receiver.identity.owner_id, lineKey: receiver.identity.line_key };
    const inbound = (eventId: string, text: string) => ({ channel, accountId: receiver!.identity.account_id, senderId: receiver!.identity.owner_id, isDirect: true, eventId, text });
    let step = 0, id = '';
    const calls = [{ name: 'owner_routine_propose', args: { name: 'user-cedar-summary', schedule: { hour: hourly?null:17, minute: hourly?0:45, weekday }, prompt: 'Summarize my owner-authored open loops grouped by person, under ten lines.' } }, null, { name: 'owner_routine_create', args: {} }, null, { name: 'owner_routines', args: {} }, null, { name: 'owner_routine_cancel', args: () => ({ routine_id: id }) }, null];
    if(advanced)calls.splice(6,0,{name:'owner_routine_adjust_propose',args:()=>({routine_id:id,schedule:{hour:hourly?null:18,minute:hourly?15:0,weekday}})} as never,null,{name:'owner_routine_create',args:{}} as never,{name:'owner_routine_adjust_confirm',args:{}} as never,null);
    if(advanced)calls.unshift({name:'skill',args:{name:'sotto-routines'}} as never);
    const model = new MockLanguageModelV4({ doGenerate: async (options) => { const call = calls[step++]; if(advanced&&step===2)assert.ok(JSON.stringify(options.prompt).includes('Sotto — Personal routines')); if (step === (advanced?7:6)) {
        const text = JSON.stringify(options.prompt);
        assert.ok(text.includes('fresh_only') && text.includes(id));
      } return { content: call ? [{ type: 'tool-call' as const, toolCallId: `routine-connected-${step}`, toolName: call.name, input: JSON.stringify(typeof call.args === 'function' ? call.args() : call.args) }] : [{ type: 'text' as const, text: 'model completed' }], finishReason: { unified: call ? 'tool-calls' : 'stop', raw: 'STOP' }, usage, warnings: [] }; } });
    runtime = createSottoAgentRuntime({ model, storagePath: join(folder, 'memory.db'), instructions: 'Use personal routine tools', skillPaths:advanced?(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(skill=>skill.path):[], sourceAuthority, chatTools: async () => tools });
    const sent: string[] = [];
    const c = new ConversationCoordinator({ journal, ownerPolicy, ownerRoutineTool: client, model: { generate: r => runtime!.generateChat({ ...r, threadId: 'memory', ownerId: 'owner' }) }, canPostPrepared: r => runtime!.canDeliverReplySource(r), postPrepared: async (r) => { sent.push(r.text); return { acceptance: 'accepted', messageId: `ack-${sent.length}` }; } });
    assert.equal((await c.receive(inbound('propose', hourly?'Every hour summarize my owner loops':advanced?(weekday==='first_of_month'?'On the first of every month at17:45 summarize my owner loops':Array.isArray(weekday)?'Every Monday Wednesday and Friday at17:45 summarize my owner loops':'Every weekday at17:45 summarize my owner loops'):'Every day at17:45 summarize my owner loops'), context)).status, 'accepted');
    assert.match(sent[0]!, hourly?/Every hour on the hour your time/:advanced?(weekday==='first_of_month'?/On the first day of every month at 5:45pm your time/:Array.isArray(weekday)?/Every Monday, Wednesday, and Friday at 5:45pm your time/:/Every weekday \(Monday–Friday\) at 5:45pm your time/):/Every day at 5:45pm your time/);
    assert.match(sent[0]!, /Set this up/);
    const path = join(folder, channel === 'telegram' ? 'data/runtime/owner-routines.json' : 'receiver/runtime/owner-routines.json');
    assert.ok(!existsSync(path), 'preview creates no schedule');
    assert.equal((await c.receive(inbound('yes', 'Yes, please.'), context)).status, 'accepted');
    assert.equal(sent[1], 'Set up. Results will arrive here.');
    const store = JSON.parse(readFileSync(path, 'utf8'));
    assert.equal(Object.keys(store.routines).length, 1);
    id = Object.keys(store.routines)[0]!;
    assert.deepEqual(store.routines[id].definition.schedule.weekday,weekday);
    const action = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_routine_tool' AND source_event_id=(SELECT id FROM inbound_events WHERE source_id='yes')").get()!;
    const event = journal.getInbound(action.source_event_id as string)!;
    const request = { actionId: action.id as string, eventId: event.id as string, payloadHash: event.payload_hash as string, text: 'Yes, please.', admittedAtMs: event.received_at as number, providerThreadId: context.threadId, args: JSON.parse(action.result_json as string).args as OwnerRoutineToolArgs };
    const original = await client.call('inspect', request);
    assert.equal(original.status, 'committed');
    assert.equal((await c.receive(inbound('list', 'List my routines'), context)).status, 'accepted');
    if(advanced) {
      const oldId=id,oldDefinition=store.routines[id].definition;
      assert.equal((await c.receive(inbound('adjust',hourly?'Run my summary at minute15 each hour':'Move my summary to6pm'),context)).status,'accepted');
      assert.match(sent[3]!,(hourly?/From: Every hour on the hour/ :weekday==='first_of_month'?/From: On the first day of every month.*5:45pm/:Array.isArray(weekday)?/From: Every Monday, Wednesday, and Friday.*5:45pm/:/From: Every weekday.*5:45pm/));assert.match(sent[3]!,(hourly?/To: Every hour at 15 minutes past/ :weekday==='first_of_month'?/To: On the first day of every month.*6pm/:Array.isArray(weekday)?/To: Every Monday, Wednesday, and Friday.*6pm/:/To: Every weekday.*6pm/));
      assert.equal(JSON.parse(readFileSync(path,'utf8')).routines[oldId].status,'active');
      assert.equal((await c.receive(inbound('adjust-yes','Yes.'),context)).status,'accepted');
      assert.equal(sent[4],hourly?'Updated. Every hour at 15 minutes past Pacific Time. Results will arrive here.':`Updated. ${weekday==='first_of_month'?'On the first day of every month':Array.isArray(weekday)?'Every Monday, Wednesday, and Friday':'Every weekday (Monday–Friday)'} at 6pm Pacific Time. Results will arrive here.`);
      const updated=JSON.parse(readFileSync(path,'utf8'));assert.equal(updated.routines[oldId].status,'cancelled');assert.deepEqual(updated.routines[oldId].definition,oldDefinition);
      id=Object.keys(updated.routines).find(key=>updated.routines[key].status==='active')!;assert.notEqual(id,oldId);assert.equal(updated.routines[id].definition.schedule.hour,hourly?null:18);
      assert.deepEqual(await client.call('inspect',request),original);
    }
    assert.equal((await c.receive(inbound('cancel', 'Cancel the cedar summary'), context)).status, 'accepted');
    assert.equal(sent[advanced?5:3], 'Stopped that routine.');
    assert.deepEqual(await client.call('inspect', request), original);
    assert.equal(JSON.parse(readFileSync(path, 'utf8')).routines[id].status, 'cancelled');
    const before = readFileSync(path, 'utf8');
    await c.close();
    await runtime.close();
    journal.close();
    journal = new OperationalJournal(join(folder, 'operations.sqlite'));
    journal.recoverInterrupted();
    const recovery = new ConversationCoordinator({ journal, ownerPolicy, ownerRoutineTool: client, model: { generate: async () => { throw new Error('replayed'); } }, postPrepared: async () => { throw new Error('resent'); } });
    assert.deepEqual(await recovery.recover(), []);
    assert.equal((await recovery.receive(inbound('yes', 'Yes, please.'), context)).status, 'duplicate');
    assert.equal(readFileSync(path, 'utf8'), before);
  }
  finally {
    await runtime?.close();
    journal.close();
    if (receiver)
      await stopReceiver(receiver.child);
    rmSync(folder, { recursive: true, force: true });
  }
});
test('real committed routine with lost response reopens and allows safe chat; recovery only inspects saved create arguments', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-loss-'));
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver'));
    let hold = true, applies = 0;
    const contacts: any[] = [];
    const client = createOwnerRoutineToolClient({ ...receiver, fetch: async (url, init) => {
        const body = JSON.parse(init!.body as string);
        contacts.push(body);
        if (body.operation === 'inspect' && hold)
          return new Response('{}', { status: 503 });
        const response = await fetch(url, init);
        if (body.operation === 'apply' && ++applies === 1) {
          assert.equal(response.status, 200);
          await response.text();
          throw new Error('receipt lost after real commit');
        }
        return response;
      } });
    const tools = createOwnerRoutineTools({ client, journal: () => journal, sourceAuthority });
    const ownerPolicy = { channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id };
    const context = { threadId: receiver.identity.route_id, destination: receiver.identity.owner_id, lineKey: 'line' };
    const inbound = (eventId: string, text: string) => ({ channel: 'photon' as const, accountId: 'account', senderId: receiver!.identity.owner_id, isDirect: true, eventId, text });
    const sent: string[] = [];
    const make = () => new ConversationCoordinator({ journal, ownerPolicy, ownerRoutineTool: client, model: { generate: async (request) => {
          const ctx = new RequestContext();
          for (const [key, value] of Object.entries({ mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey, allowOwnerRoutine: request.allowOwnerRoutine, sourceRevision: 'a'.repeat(64) }))
            ctx.set(key, value);
          if (request.text.startsWith('Every'))
            await tools.owner_routine_propose.execute!({ name: 'user-loss-summary', schedule: { hour: 17, minute: 45, weekday: null }, prompt: 'Summarize my owner-authored open loops.' }, { requestContext: ctx, abortSignal: request.signal } as never);
          if (request.text === 'Yes.')
            await tools.owner_routine_create.execute!({}, { requestContext: ctx, abortSignal: request.signal } as never);
          return { text: 'safe answer', sourceRevision: 'a'.repeat(64) };
        } }, postPrepared: async (r) => { sent.push(r.text); return { acceptance: 'accepted', messageId: `ack-${sent.length}` }; } });
    let c = make();
    await c.receive(inbound('proposal', 'Every day summarize my loops'), context);
    assert.equal((await c.receive(inbound('yes', 'Yes.'), context)).status, 'held');
    const path = join(folder, 'receiver/runtime/owner-routines.json');
    assert.equal(Object.keys(JSON.parse(readFileSync(path, 'utf8')).routines).length, 1);
    const before = readFileSync(path, 'utf8');
    await c.close();
    journal.close();
    journal = new OperationalJournal(join(folder, 'operations.sqlite'));
    journal.recoverInterrupted();
    c = make();
    assert.equal((await c.receive(inbound('safe', 'Tell me a joke'), context)).status, 'accepted');
    assert.equal(sent.at(-1), 'safe answer');
    hold = false;
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_routine_tool' AND status='unknown'").run();
    assert.equal((await c.recover())[0]?.status, 'accepted');
    assert.equal(sent.at(-1), 'Set up. Results will arrive here.');
    assert.equal(applies, 1);
    assert.equal(readFileSync(path, 'utf8'), before);
    const original = contacts.find(x => x.operation === 'apply');
    for (const inspect of contacts.filter(x => x.operation === 'inspect'))
      assert.deepEqual(inspect.args, original.args);
    await c.close();
  }
  finally {
    journal.close();
    if (receiver)
      await stopReceiver(receiver.child);
    rmSync(folder, { recursive: true, force: true });
  }
});
