import {createOwnerBriefTools} from '../src/tools/owner-brief-tool.js';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {once} from 'node:events';
import {existsSync, readFileSync, writeFileSync} from 'node:fs';
import {DatabaseSync} from 'node:sqlite';
import {resolve} from 'node:path';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {ConversationCoordinator} from '../src/conversation.js';
import {OperationalJournal} from '../src/journal/index.js';
import {createOwnerBriefRequestClient} from '../src/tools/owner-brief-request-client.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';
const identity = {tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner', line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64)};
const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: 'owner'};
const context = {threadId: 'thread', destination: 'owner-destination', lineKey: 'line'};
const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: 'owner', isDirect: true, eventId, text});
for (const lost of [false, true]) {
  test(`brief original admission and safe mixed projection with recovery=${lost}`, async () => {
    const folder = mkdtempSync(join(tmpdir(), 'sotto-brief-request-'));
    const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
    let held = lost;
    const calls: string[] = [], model: string[] = [], sends: string[] = [];
    const client = createOwnerBriefRequestClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
      fetch: async (_url, init) => {
        const body = JSON.parse(init!.body as string);
        calls.push(body.operation);
        assert.equal(body.args.provider_message_id, 'provider-original');
        if (held && body.operation === 'admit') throw new Error('lost response');
        if (held) return Response.json({version: 1, status: 'not_found'});
        return Response.json({version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
          payload_hash: body.payload_hash, args_hash: stableActionIdentity(body.event_id, 'owner_brief_request', body.args).payloadHash,
          outcome: {operation: 'admit', request_id: 'b'.repeat(32), kind: 'evening', refresh: false, status: 'queued'}});
      }});
    const service = new ConversationCoordinator({journal, ownerPolicy, ownerBriefRequest: client,
      model: {generate: async turn => {model.push(turn.text); return {text: 'Calendar read answer', sourceRevision: 'owner-only-context-v1'};}},
      postPrepared: async request => {sends.push(request.text); return {acceptance: 'accepted', messageId: `send-${sends.length}`};}});
    try {
      const text = lost ? 'Give me my evening brief and tell me  what is on my calendar'
        : 'evening brief pls and tell me  what is on my calendar';
      const first = await service.receive(inbound('provider-original', text), context);
      if (lost) {
        assert.equal(first.status, 'held');
        assert.match(sends[0]!, /I couldn’t confirm whether the brief request/);
        assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_brief_request'").get()!.status, 'unknown');
        await service.receive(inbound('safe', 'hello'), context);
        assert.deepEqual(model, ['hello']);
        // A later exact receipt is inspected only after the persisted cadence is due.
        journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_brief_request' AND status='unknown'").run();
        held = false;
        await service.recover();
      }
      assert.deepEqual(model.filter(x => x !== 'hello'), ['tell me  what is on my calendar']);
      assert.match(sends.at(-1)!, /^Your brief request is queued/);
      assert.equal(calls.filter(x => x === 'admit').length, 1);
      const event = journal.db.prepare("SELECT * FROM inbound_events WHERE source_id='provider-original'").get()!;
      assert.equal(JSON.parse(event.payload as string).inbound.text, text);
      assert.equal((await service.receive(inbound('provider-original', text), context)).status, 'duplicate');
    } finally {await service.close(); journal.close(); rmSync(folder, {recursive: true, force: true});}
  });
}
for (const answer of ['Your brief request is queued.', 'Your brief request is queued.\n\nCalendar read answer', 'Calendar read answer']) {
  test(`brief acknowledgement is not prepended twice: ${answer}`, async () => {
    const folder = mkdtempSync(join(tmpdir(), 'sotto-brief-ack-'));
    const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
    const sends: string[] = [];
    let admissions = 0;
    const client = createOwnerBriefRequestClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
      fetch: async (_url, init) => {
        const body = JSON.parse(init!.body as string);
        admissions++;
        return Response.json({version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
          payload_hash: body.payload_hash, args_hash: stableActionIdentity(body.event_id, 'owner_brief_request', body.args).payloadHash,
          outcome: {operation: 'admit', request_id: 'b'.repeat(32), kind: 'evening', refresh: false, status: 'queued'}});
      }});
    const service = new ConversationCoordinator({journal, ownerPolicy, ownerBriefRequest: client,
      model: {generate: async () => ({text: answer, sourceRevision: 'owner-only-context-v1'})},
      postPrepared: async request => {sends.push(request.text); return {acceptance: 'accepted', messageId: 'single-ack'};}});
    try {
      await service.receive(inbound('brief-ack', 'evening brief pls and tell me what is on my calendar'), context);
      assert.equal(admissions, 1);
      assert.deepEqual(sends, [answer.startsWith('Your brief request is queued.') ? answer : 'Your brief request is queued.\n\n' + answer]);
      assert.equal(sends[0]!.split('Your brief request is queued.').length - 1, 1);
    } finally {await service.close(); journal.close(); rmSync(folder, {recursive: true, force: true});}
  });
}

async function startReceiver(folder: string, channel: 'photon' | 'telegram' = 'photon', port = 0) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_RECEIVER_PORT: String(port), SOTTO_TEST_TELEGRAM_ROUTINE: channel === 'telegram' ? '1' : '0', SOTTO_TEST_BRIEF_REQUEST: '1',
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
for (const channel of ['photon', 'telegram'] as const) for (const variant of ['ordinary','lost','fresh','refresh-polite','updated','article','tool','tool-repeat','tool-lost','tool-modifier'] as const) test(`actual scoped HTTP receiver commits original queue receipt channel=${channel} variant=${variant}`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-brief-http-'));
  let receiver = await startReceiver(folder, channel);
  let journal = new OperationalJournal(join(folder, 'node.sqlite'));
  const sent: string[] = [];
  const loseResponse=variant==='lost'||variant==='tool-lost';
  const modelTool=variant==='tool'||variant==='tool-repeat'||variant==='tool-lost'||variant==='tool-modifier';
  // A settled brief keeps generating: the tool-only step(s) are followed by the independent answer.
  const toolSteps=variant==='tool-repeat'?2:variant==='tool'||variant==='tool-modifier'?1:0;
  const independentAnswer = variant==='tool-modifier' ? 'The full brief will arrive separately.' : 'Your dentist is at 2 PM.';
  let held = loseResponse;
  const contacts: string[] = [];
  const client = createOwnerBriefRequestClient({...receiver, fetch: async (url, init) => {
    const body = JSON.parse(init!.body as string);
    contacts.push(body.operation);
    if (held && body.operation === 'inspect') return Response.json({version: 1, status: 'not_found'});
    const result = await fetch(url, init);
    if (loseResponse && body.operation === 'admit' && result.status === 200) {
      await result.body?.cancel();
      throw new Error('Synthetic lost response after actual queue commit');
    }
    return result;
  }});
  let modelCalls = 0;
  const runtime = createSottoAgentRuntime({storagePath: join(folder, 'memory.sqlite'), instructions: 'Answer the read-only owner question.',
    sourceAuthority: {read: async () => ({version: 2, complete: true, revision: 'a'.repeat(64), bridge_sources: [], google_sources: []})},
    chatTools: () => createOwnerBriefTools({client, journal: () => journal, sourceAuthority: {read: async () => ({version: 2, complete: true, revision: 'a'.repeat(64), bridge_sources: [], google_sources: []})}}),
    model: new MockLanguageModelV4({doGenerate: async () => {
      modelCalls++;
      const toolCall = {type: 'tool-call' as const, toolCallId: `brief-call-${modelCalls}`, toolName: 'owner_brief_request', input: JSON.stringify({kind: 'evening', refresh: true})};
      if (variant === 'tool-lost' && modelCalls === 1) return {content: [{type: 'text', text: 'Your dentist is at 2 PM.'}, toolCall], finishReason: {unified: 'tool-calls', raw: 'TOOL_CALLS'}, usage: {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}}, warnings: []};
      if (modelCalls <= toolSteps) return {content: [toolCall], finishReason: {unified: 'tool-calls', raw: 'TOOL_CALLS'}, usage: {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}}, warnings: []};
      return {content: [{type: 'text', text: toolSteps ? independentAnswer : 'Safe independent answer'}], finishReason: {unified: 'stop', raw: 'STOP'},
        usage: {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}}, warnings: []};
    }})});
  const createCoordinator = () => new ConversationCoordinator({journal,
    ownerPolicy: {channel, accountId: receiver.identity.account_id, ownerId: receiver.identity.owner_id},
    ownerBriefRequest: client, model: {generate: request => runtime.generateChat({...request, threadId: 'memory', ownerId: 'owner'})},
    canPostPrepared: request => runtime.canDeliverReplySource(request),
    postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: 'synthetic-ack'};}});
  let service = createCoordinator();
  try {
    const input = {...inbound('http-provider-original', variant==='tool-modifier' ? 'Give me a fresh evening brief, including anything worth preparing for over the next few days. Keep the help offer focused on meeting prep this time.' : modelTool ? 'Thanks. try a fresh evening brief again pls and what time is my dentist?' : variant==='refresh-polite' ? 'refresh my evening brief please' : variant==='updated' ? 'send me an updated brief' : variant==='article' ? 'send me an evening brief' : variant==='fresh' ? 'Please send me a fresh evening brief.' : 'Give me my evening brief' + (loseResponse ? ' and tell me what is on my calendar' : '')), senderId: receiver.identity.owner_id, channel, accountId: receiver.identity.account_id};
    const result = await service.receive(input, {...context, lineKey: receiver.identity.line_key, threadId: receiver.identity.route_id});
    if (loseResponse) {
      assert.equal(result.status, 'held');
      assert.match(sent[0]!, /I couldn’t confirm whether the brief request/);
      assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_brief_request'").get()!.status, 'unknown');
      assert.equal(modelCalls, modelTool ? 1 : 0);
      await service.close();
      journal.close();
      await stopReceiver(receiver.child);
      receiver = await startReceiver(folder, channel, receiver.port);
      journal = new OperationalJournal(join(folder, 'node.sqlite'));
      journal.recoverInterrupted();
      service = createCoordinator();
      await service.receive({...inbound('safe-http', 'hello while my brief outcome is held'), senderId: receiver.identity.owner_id, channel, accountId: receiver.identity.account_id}, {...context, lineKey: receiver.identity.line_key, threadId: receiver.identity.route_id});
      assert.equal(modelCalls, modelTool ? 2 : 1);
      // A later exact receipt is inspected only after the persisted cadence is due.
      journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_brief_request' AND status='unknown'").run();
      held = false;
      await service.recover();
      assert.equal(modelCalls, 2);
      assert.match(sent.at(-1)!, /^Your brief request is queued/);
    } else {
      assert.ok(['accepted', 'silent'].includes(result.status));
      assert.equal(modelCalls, toolSteps ? toolSteps + 1 : 0);
      assert.deepEqual(sent, [modelTool ? 'Your brief request is queued.\n\n'+independentAnswer : 'Your brief request is queued.']);
    }
    assert.equal(contacts.filter(operation => operation === 'admit').length, 1);
    const action = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_brief_request'").get()!;
    assert.equal(action.status, 'succeeded');
    const metadata = JSON.parse(action.result_json as string);
    assert.equal(metadata.args.provider_message_id, input.eventId);
    assert.equal(metadata.args.kind,variant==='updated'?'auto':'evening');assert.equal(metadata.args.refresh,variant==='fresh'||variant==='updated'||variant==='refresh-polite'||modelTool);
    const saved = journal.readOwnerBriefRequestAction(action.id as string, {accountId: receiver.identity.account_id, ownerId: receiver.identity.owner_id,
      channel, lineKey: receiver.identity.line_key, scopeHash: client.scopeHash});
    assert.deepEqual(await client.call('inspect', saved.request), metadata.receipt);
    const changedArgs = {...saved.request.args, refresh: !saved.request.args.refresh};
    const changed = {...saved.request, args: changedArgs, actionId: stableActionIdentity(saved.request.eventId, 'owner_brief_request', changedArgs).actionId};
    assert.equal((await client.call('admit', changed)).status, 'rejected');
    assert.deepEqual(await client.call('inspect', saved.request), metadata.receipt);
    assert.equal((await service.receive(input, {...context, lineKey: receiver.identity.line_key, threadId: receiver.identity.route_id})).status, 'duplicate');
    if (receiver.identity.channel === 'telegram') {
      const work = new DatabaseSync(join(folder, 'events/work.sqlite3'), {readOnly: true});
      try {
        const rows = work.prepare('SELECT admission FROM jobs').all();
        assert.equal(rows.length, 1, 'restart and inspect never enqueue another job');
        const binding = JSON.parse(rows[0]!.admission as string).telegram_binding;
        assert.equal(binding.owner_id, receiver.identity.owner_id);
        assert.equal(binding.bot_fingerprint, receiver.identity.bot_fingerprint);
        assert.equal(binding.source_revision, 'a'.repeat(64));
      } finally { work.close(); }
      const linkPath = join(folder, 'telegram-link.json');
      const original = readFileSync(linkPath, 'utf8');
      writeFileSync(linkPath, JSON.stringify({...JSON.parse(original), allowed_user: 999}));
      await assert.rejects(client.call('inspect', saved.request), /Owner tool unavailable/);
      writeFileSync(linkPath, original);
      writeFileSync(join(folder, 'source-revoked'), 'revoked');
      await assert.rejects(client.call('inspect', saved.request), /Owner tool unavailable/);
      rmSync(join(folder, 'source-revoked'));
      assert.deepEqual(await client.call('inspect', saved.request), metadata.receipt);
    }

  } finally {await service.close(); await runtime.close(); journal.close(); await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});}
});

test('queued brief plus modal mutation asks separately without a model turn', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-brief-mutation-'));
  const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
  const sent: string[] = [];
  const client = createOwnerBriefRequestClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      return Response.json({version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
        payload_hash: body.payload_hash, args_hash: stableActionIdentity(body.event_id, 'owner_brief_request', body.args).payloadHash,
        outcome: {operation: 'admit', request_id: 'b'.repeat(32), kind: 'evening', refresh: false, status: 'queued'}});
    }});
  const service = new ConversationCoordinator({journal, ownerPolicy, ownerBriefRequest: client,
    model: {generate: async () => {throw new Error('Mutation remainder reached model');}},
    postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: 'brief-ack'};}});
  try {
    await service.receive(inbound('modal-mutation', 'Give me my brief and can you remind me tomorrow'), context);
    assert.deepEqual(sent, ['Your brief request is queued. Please ask the other part in a separate message.']);
  } finally {await service.close(); journal.close(); rmSync(folder, {recursive: true, force: true});}
});

for (const [text, remainder] of [['send my brief and tell me which tasks are not done', 'tell me which tasks are not done'],
  ['give me my brief and check if Dana can stop by', 'check if Dana can stop by']] as const)
  test(`fast-path brief with negation only in its remainder queues and answers instead of holding: ${text}`, async () => {
    const folder = mkdtempSync(join(tmpdir(), 'sotto-brief-remainder-'));
    const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
    const calls: string[] = [], model: string[] = [], sends: string[] = [];
    const client = createOwnerBriefRequestClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
      fetch: async (_url, init) => {
        const body = JSON.parse(init!.body as string);
        calls.push(body.operation);
        return Response.json({version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
          payload_hash: body.payload_hash, args_hash: stableActionIdentity(body.event_id, 'owner_brief_request', body.args).payloadHash,
          outcome: {operation: 'admit', request_id: 'b'.repeat(32), kind: 'evening', refresh: false, status: 'queued'}});
      }});
    const service = new ConversationCoordinator({journal, ownerPolicy, ownerBriefRequest: client,
      model: {generate: async turn => {model.push(turn.text); return {text: 'answer', sourceRevision: 'owner-only-context-v1'};}},
      postPrepared: async request => {sends.push(request.text); return {acceptance: 'accepted', messageId: `send-${sends.length}`};}});
    try {
      assert.ok(['accepted', 'silent'].includes((await service.receive(inbound('remainder', text), context)).status));
      assert.deepEqual(calls, ['admit']);
      assert.deepEqual(model, [remainder]);
      assert.deepEqual(sends, ['Your brief request is queued.\n\nanswer']);
      assert.ok(['accepted', 'silent'].includes((await service.receive(inbound('next', 'hello'), context)).status));
      assert.deepEqual(model, [remainder, 'hello']);
    } finally {await service.close(); journal.close(); rmSync(folder, {recursive: true, force: true});}
  });

test('a refused brief admission answers the owner instead of wedging the thread', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-brief-refused-'));
  const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
  let contacts = 0;
  const model: string[] = [], sends: string[] = [];
  const client = createOwnerBriefRequestClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async () => {contacts++; throw new Error('must not admit');}});
  journal.prepareOwnerBriefRequestAction = () => {throw new Error('Brief requires a current affirmative owner brief request');};
  const service = new ConversationCoordinator({journal, ownerPolicy, ownerBriefRequest: client,
    model: {generate: async turn => {model.push(turn.text); return {text: 'answer', sourceRevision: 'owner-only-context-v1'};}},
    postPrepared: async request => {sends.push(request.text); return {acceptance: 'accepted', messageId: `send-${sends.length}`};}});
  try {
    assert.ok(['accepted', 'silent'].includes((await service.receive(inbound('refused', 'Give me my brief and tell me what is on my calendar'), context)).status));
    assert.deepEqual(sends, ['I could not queue your brief request. The other part was not processed; please ask it separately.']);
    assert.equal(journal.db.prepare("SELECT outcome FROM inbound_events WHERE source_id='refused'").get()!.outcome, 'resolved');
    assert.ok(['accepted', 'silent'].includes((await service.receive(inbound('after-refused', 'hello'), context)).status));
    assert.deepEqual(model, ['hello']);
    assert.equal(contacts, 0);
  } finally {await service.close(); journal.close(); rmSync(folder, {recursive: true, force: true});}
});

for(const code of ['pending_delivery','needs_context','unavailable','event_conflict'] as const) test(`brief rejection ${code} gives truthful reply without another request or model call`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'brief-rejection-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));let contacts=0,modelCalls=0;const sent:string[]=[],feedback:string[]=[];
 const client=createOwnerBriefRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async()=>{contacts++;return Response.json({version:1,status:'rejected',code},{status:409});}});
 const coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerBriefRequest:client,model:{generate:async()=>{modelCalls++;throw Error('must not model');}},onOwnerReplyAccepted:message=>{feedback.push(message.eventId);},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'rejection'};}});
 const input=inbound('rejected','Please send me a fresh evening brief.');
 try {
  assert.ok(['accepted','silent'].includes((await coordinator.receive(input,context)).status));
  assert.equal(contacts,1);assert.equal(modelCalls,0);assert.equal(sent.length,1);assert.doesNotMatch(sent[0]!,/try again/i);
  if(code==='pending_delivery')assert.match(sent[0]!,/earlier brief.*unconfirmed.*on hold.*twice/);
  assert.deepEqual(feedback,code==='pending_delivery'?[]:['rejected'],'an unconfirmed earlier delivery gives no completion feedback');
  if(code==='needs_context')assert.match(sent[0]!,/context is not ready/);
  assert.equal((await coordinator.receive(input,context)).status,'duplicate');assert.equal(contacts,1);
  const action=journal.db.prepare("SELECT * FROM actions WHERE kind='owner_brief_request'").get()!;
  assert.equal(action.status,'rejected');assert.equal(JSON.parse(action.result_json as string).rejection,code);
 }finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});


test('morning brief pls admits once without asking the owner to rephrase or invoking chat', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-brief-fragment-'));
  const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
  let admissions = 0;
  const sends: string[] = [];
  const client = createOwnerBriefRequestClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      admissions++;
      assert.equal(body.text, 'morning brief pls');
      assert.equal(body.args.kind, 'morning');
      assert.equal(body.args.refresh, false);
      return Response.json({version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
        payload_hash: body.payload_hash, args_hash: stableActionIdentity(body.event_id, 'owner_brief_request', body.args).payloadHash,
        outcome: {operation: 'admit', request_id: 'b'.repeat(32), kind: 'morning', refresh: false, status: 'queued'}});
    }});
  const service = new ConversationCoordinator({journal, ownerPolicy, ownerBriefRequest: client,
    model: {generate: async () => {throw new Error('ordinary chat must not handle this explicit brief');}},
    postPrepared: async request => {sends.push(request.text); return {acceptance: 'accepted', messageId: 'brief-fragment-ack'};}});
  try {
    await service.receive(inbound('morning-fragment', 'morning brief pls'), context);
    assert.deepEqual(sends, ['Your brief request is queued.']);
    assert.equal(admissions, 1);
    assert.equal((await service.receive(inbound('morning-fragment', 'morning brief pls'), context)).status, 'duplicate');
    assert.equal(admissions, 1);
  } finally {await service.close(); journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('brief model tool retains owner, source revision, cancellation and immutable event boundaries', async () => {
  const {RequestContext} = await import('@mastra/core/request-context');
  const folder = mkdtempSync(join(tmpdir(), 'sotto-brief-boundaries-'));
  const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
  let contacts = 0;
  const client = createOwnerBriefRequestClient({bridgeUrl:'http://127.0.0.1:8080/mcp', bearer:'c'.repeat(64), identity,
    fetch:async()=>{contacts++;throw Error('must not contact');}});
  const tools = createOwnerBriefTools({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:[]})}});
  const service = new ConversationCoordinator({journal,ownerPolicy,ownerBriefRequest:client,
    model:{generate:async turn=>{
      const base={mode:'chat',runId:turn.runId,ownerRequestKey:turn.ownerRequestKey,allowOwnerSkill:true,sourceRevision:'a'.repeat(64)};
      for(const overrides of [{mode:'unattended'},{allowOwnerSkill:false},{sourceRevision:'b'.repeat(64)},{runId:'not-current'}]) {
        const requestContext = new RequestContext();
        for(const [key,value] of Object.entries({...base,...overrides})) requestContext.set(key,value);
        await assert.rejects(tools.owner_brief_request.execute!({kind:'morning',refresh:true},{requestContext,abortSignal:turn.signal} as never));
      }
      const requestContext = new RequestContext();
      for(const [key,value] of Object.entries(base)) requestContext.set(key,value);
      await assert.rejects(tools.owner_brief_request.execute!({kind:'morning',refresh:true},{requestContext,abortSignal:AbortSignal.abort()} as never));
      const authority={...ownerPolicy,runId:turn.runId,lineKey:'line',scopeHash:client.scopeHash};
      assert.throws(()=>journal.prepareOwnerBriefRequestAction(authority,turn.ownerRequestKey,{kind:'morning',refresh:true,remainder:null,provider_message_id:'forged'}));
      assert.equal(journal.db.prepare('SELECT COUNT(*) AS n FROM actions').get()!.n,0);
      return 'Nothing was queued.';
    }},postPrepared:async()=>({acceptance:'accepted',messageId:'safe'})});
  try {
    await service.receive(inbound('natural-request','could you try that morning overview again?'),context);
    assert.equal(contacts,0);
  } finally {await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('journal rejects off-request brief admission before creating an action', () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-brief-intent-'));
  const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
  try {
    for (const [i, text] of ['thanks', 'ok', 'what time is my dentist?', 'Do not send my brief', 'She said send my brief', '"send my brief"'].entries()) {
      const event = journal.admitInbound({eventId:'intent-'+i, accountKey:'account', lineKey:'line', threadKey:'thread',
        channel:'photon', sender:'owner', payload:JSON.stringify({inbound:inbound('intent-'+i,text),context})}).event.id as string;
      const authority = {runId:'run-'+i, accountId:'account', ownerId:'owner', lineKey:'line', channel:'photon' as const, scopeHash:'c'.repeat(64)};
      journal.startRun({runId:authority.runId,eventId:event});
      assert.throws(() => journal.prepareOwnerBriefRequestAction(authority,event,{kind:'auto',refresh:false,remainder:null,provider_message_id:'intent-'+i}), /current affirmative/);
      assert.equal(journal.listOwnerBriefRequestActionsForThread(event).length,0);
      journal.completeRun(authority.runId);
    }
  } finally {journal.close();rmSync(folder,{recursive:true,force:true});}
});
