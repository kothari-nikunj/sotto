import {createOwnerOfferReplyClient} from '../src/tools/owner-offer-reply-client.js';
import {createOwnerMeetingPrepClient} from '../src/tools/owner-meeting-prep-client.js';
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
import { createOwnerRoutineToolClient } from '../src/tools/owner-routine-tool-client.js';
import { createOwnerRoutineTools } from '../src/tools/owner-routine-tool.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { ownerRoutineToolKind, routineDefinitionHash,routineProposalText,routineScheduleSchema,routinePreviewMatches } from '../src/tools/owner-routine-tool-contract.js';
const identity = { tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64) };
const forbiddenMeetingPrep = createOwnerMeetingPrepClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
  fetch: async () => {assert.fail('Accepted local review must not be stolen by meeting prep');}});
const ownerPolicy = { channel: 'photon' as const, accountId: 'account', ownerId: 'owner' };
const context = { threadId: 'thread', destination: 'owner', lineKey: 'line' };
const inbound = (eventId: string, text: string) => ({ channel: 'photon' as const, accountId: 'account', senderId: 'owner',
  isDirect: true, eventId, text });
const args = { operation: 'propose' as const, name: 'user-friday-loops', schedule: { hour: 16, minute: 0, weekday: 5 }, prompt: 'Summarize my owner-authored open loops by person.' };
const authority = { version: 2 as const, complete: true, revision: 'd'.repeat(64), bridge_sources: [], google_sources: [] };
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const usage = { inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 2, text: 2, reasoning: 0 } };
test('canonical weekday sets render exact create and adjustment reviews without changing legacy schedule hashes', () => {
  const schedule={hour:9,minute:0,weekday:[1,3,5]};
  assert.equal(routineScheduleSchema.safeParse(schedule).success,true);
  for(const weekday of [[],[1,1],[3,1],[0,7],[false,1]])
    assert.equal(routineScheduleSchema.safeParse({hour:9,minute:0,weekday}).success,false);
  const definition={name:args.name,schedule,prompt:args.prompt,zone:'UTC',destination:'this Photon conversation' as const};
  const hash=routineDefinitionHash(definition);
  assert.match(routineProposalText({version:1,status:'preview',event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),definition,definition_hash:hash}),
    /Every Monday, Wednesday, and Friday at 9am your time/);
  const updated={...definition,schedule:{hour:10,minute:0,weekday:[1,3,5]}};
  assert.match(routineProposalText({version:1,status:'preview',event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),operation:'adjust',routine_id:'c'.repeat(64),previous_definition:definition,previous_definition_hash:hash,definition:updated,definition_hash:routineDefinitionHash(updated)}),
    /From: Every Monday, Wednesday, and Friday at 9am UTC\nTo: Every Monday, Wednesday, and Friday at 10am UTC/);
  const legacy={...definition,schedule:{hour:9,minute:0,weekday:1}};
  assert.equal(routineDefinitionHash(legacy),'475a04f8f1e684dd7d1b6a2a207f4b2d6217e1cbedbca534beaac98c02afe347');
});
test('an adjustment preview between weekday-set aliases never matches as a schedule change', () => {
  const base={name:args.name,prompt:args.prompt,zone:'UTC',destination:'this Photon conversation' as const};
  for(const [from,to] of [['weekdays',[1,2,3,4,5]],[3,[3]],[null,[0,1,2,3,4,5,6]],[[1,3,5],[1,3,5]]] as const){
    const previous={...base,schedule:{hour:9,minute:0,weekday:from as never}};
    for(const [hour,changed] of [[9,false],[10,true]] as const){
      const definition={...previous,schedule:{hour,minute:0,weekday:to as never}};
      const preview={version:1 as const,status:'preview' as const,event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),operation:'adjust' as const,
        routine_id:'c'.repeat(64),previous_definition:previous,previous_definition_hash:routineDefinitionHash(previous),definition,definition_hash:routineDefinitionHash(definition)};
      assert.equal(routinePreviewMatches(preview,{operation:'propose_adjust',routine_id:'c'.repeat(64),schedule:definition.schedule}),changed);
    }
  }
});
function fixture(folder: string) {
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  const calls: Record<string, unknown>[] = [], receipts = new Map<string, Record<string, unknown>>();
  let loseCreate = false, hideInspect = false, state = authority;
  let previewRefusal: 'routine_limit' | 'routine_conflict' | 'unsupported_definition' | undefined;
  const client = createOwnerRoutineToolClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      calls.push(body);
      assert.equal(body.text_hash, createHash('sha256').update(body.text).digest('hex'));
      assert.ok(!body.text.startsWith('/loop'));
      if (body.operation === 'list')
        return reply({ version: 1, status: 'list', event_id: body.event_id, payload_hash: body.payload_hash, coverage: 'fresh_only', routines: [] });
      if (body.operation === 'preview') {
        if (previewRefusal) return reply({version:1,status:'rejected',code:previewRefusal},409);
        const definition = { name: body.args.name, schedule: body.args.schedule, prompt: body.args.prompt, zone: 'UTC', destination: 'this Photon conversation' as const };
        if (body.args.operation === 'propose_adjust') {
          const previous = {name:args.name,schedule:args.schedule,prompt:args.prompt,zone:'UTC',destination:'this Photon conversation' as const};
          const updated = {...previous,schedule:body.args.schedule};
          return reply({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,
            operation:'adjust',routine_id:body.args.routine_id,previous_definition:previous,
            previous_definition_hash:routineDefinitionHash(previous),definition:updated,definition_hash:routineDefinitionHash(updated)});
        }
        return reply({ version: 1, status: 'preview', event_id: body.event_id, payload_hash: body.payload_hash, definition, definition_hash: routineDefinitionHash(definition) });
      }
      if (body.operation === 'inspect')
        return reply(!hideInspect && receipts.get(body.action_id) || { version: 1, status: 'not_found' });
      const binding = stableActionIdentity(body.event_id, ownerRoutineToolKind, body.args);
      const adjustedId = createHash('sha256').update(JSON.stringify(['sotto.fresh-routine.v1',body.action_id])).digest('hex');
      const receipt = { version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id, payload_hash: body.payload_hash, args_hash: binding.payloadHash,
        outcome: body.args.operation === 'adjust'
          ? {operation:'adjust',routine_id:adjustedId,definition_hash:routineDefinitionHash(body.args.definition),status:'active',previous_routine_id:body.args.routine_id,previous_definition_hash:body.args.previous_definition_hash}
          : { operation: body.args.operation, routine_id: 'b'.repeat(64), definition_hash: body.args.operation === 'create' ? routineDefinitionHash(body.args.definition) : 'a'.repeat(64), status: body.args.operation === 'create' ? 'active' : 'cancelled' } };
      receipts.set(body.action_id, receipt);
      if (loseCreate)
        throw new Error('lost after commit');
      return reply(receipt);
    } });
  const sourceAuthority = { read: async () => state };
  const tools = createOwnerRoutineTools({ client, journal: () => journal, sourceAuthority });
  const callTool = async (name: keyof typeof tools, input: unknown, request: ModelRequest, overrides: Record<string, unknown> = {}) => {
    const requestContext = new RequestContext();
    for (const [key, value] of Object.entries({ mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey,
      sourceRevision: state.revision, allowOwnerRoutine: request.allowOwnerRoutine, ...overrides }))
      requestContext.set(key, value);
    return tools[name].execute!(input as never, { requestContext, abortSignal: request.signal } as never);
  };
  return { get journal() { return journal; }, client, calls, tools, sourceAuthority, callTool,
    set previewRefusal(value: 'routine_limit' | 'routine_conflict' | 'unsupported_definition') {previewRefusal=value;},
    set loseCreate(value: boolean) { loseCreate = value; }, set hideInspect(value: boolean) { hideInspect = value; },
    revoke() { state = { ...authority, revision: 'e'.repeat(64) }; },
    reopen() { journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted(); } };
}
test('exact routine swipe confirms through the mounted tool without asking the model; old and declined targets have no effect', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-swipe-')), f = fixture(folder), sent:string[] = [];
  let modelCalls = 0, confirmCalls = 0;
  const c = new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerRoutineTool:f.client,
    model:{generate:async request => {modelCalls++;
      if (request.text.startsWith('Every')) await f.callTool('owner_routine_propose',
        {name:args.name,schedule:args.schedule,prompt:args.prompt},request);
      return {text:'ordinary answer',sourceRevision:authority.revision};}},
    confirmSwipeRoutine:async request => {confirmCalls++;
      await f.callTool(request.operation === 'adjust' ? 'owner_routine_adjust_confirm' : 'owner_routine_create',{},
        {runId:request.runId,ownerRequestKey:request.eventId,allowOwnerRoutine:true,signal:request.signal} as ModelRequest,
        {sourceRevision:request.sourceRevision});},
    postPrepared:async request => {sent.push(request.text);return {acceptance:'accepted',messageId:`ack-${sent.length}`};}});
  try {
    await c.receive(inbound('offer','Every Friday summarize my loops'),context);
    assert.match(sent[0]!,/Set this up/);
    assert.equal((await c.receive({...inbound('confirm','Yes, please.'),replyBound:true,replyToMessageId:'ack-1'},context)).status,'accepted');
    assert.equal(sent[1],'Set up. Results will arrive here.');
    assert.equal(modelCalls,1);
    assert.equal(confirmCalls,1);
    assert.equal(f.calls.filter(call => call.operation === 'apply').length,1);
    await c.receive({...inbound('old','Yes.'),replyBound:true,replyToMessageId:'ack-1'},context);
    assert.match(sent[2]!,/could not confirm that exact proposal/);
    assert.equal(modelCalls,1);
    assert.equal(f.calls.filter(call => call.operation === 'apply').length,1);
  } finally {await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('an unavailable explicit routine swipe target never borrows the latest accepted proposal', async () => {
  for (const target of ['foreign-message', undefined]) {
    const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-wrong-swipe-')), f = fixture(folder);
    let confirmationAttempts = 0;
    const c = new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerRoutineTool:f.client,
      model:{generate:async request => {
        if (request.text.startsWith('Every')) await f.callTool('owner_routine_propose',
          {name:args.name,schedule:args.schedule,prompt:args.prompt},request);
        else if (request.text === 'Yes.') await assert.rejects(f.callTool('owner_routine_create',{},request));
        return {text:'Please reply to the current review.',sourceRevision:authority.revision};}},
      confirmSwipeRoutine:async () => {confirmationAttempts++;assert.fail('Wrong target cannot use the direct tool path');},
      postPrepared:async () => ({acceptance:'accepted',messageId:'accepted-review'})});
    try {
      await c.receive(inbound('offer','Every Friday summarize my loops'),context);
      await c.receive({...inbound('wrong','Yes.'),replyBound:true,...(target ? {replyToMessageId:target} : {})},context);
      assert.equal(confirmationAttempts,0);
      assert.equal(f.calls.filter(call => call.operation === 'apply').length,0);
    } finally {await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
  }
});
test('an expired routine reply proof is refused before its otherwise adjacent writer selector', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-routine-swiped-expired-')),f=fixture(folder),sent:string[]=[];
  let modelCalls=0,confirmCalls=0;
  const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerRoutineTool:f.client,
    model:{generate:async request=>{modelCalls++;
      await f.callTool('owner_routine_propose',{name:args.name,schedule:args.schedule,prompt:args.prompt},request);
      return {text:'review',sourceRevision:authority.revision};}},
    confirmSwipeRoutine:async()=>{confirmCalls++;assert.fail('Expired proof cannot reach the writer');},
    postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`ack-${sent.length}`};}});
  try {
    await c.receive(inbound('offer','Every Friday summarize my loops'),context);
    const reply=f.journal.db.prepare('SELECT dispatch_id FROM prepared_replies LIMIT 1').get()!;
    f.journal.db.prepare('UPDATE dispatches SET updated_at=? WHERE id=?').run(Date.now()+60_000,reply.dispatch_id as string);
    await c.receive({...inbound('yes','Yes.'),replyBound:true,replyToMessageId:'ack-1'},context);
    assert.match(sent.at(-1)!,/could not confirm that exact proposal/);
    assert.equal(confirmCalls,0);
    assert.equal(modelCalls,1);
    assert.equal(f.calls.filter(call=>call.operation==='apply').length,0);
  }finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('routine swipe rechecks live source authority before writer contact', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-swiped-revoke-')), f = fixture(folder), sent:string[] = [];
  let modelCalls = 0;
  const c = new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerRoutineTool:f.client,
    model:{generate:async request => {modelCalls++;
      await f.callTool('owner_routine_propose',{name:args.name,schedule:args.schedule,prompt:args.prompt},request);
      return {text:'review',sourceRevision:authority.revision};}},
    confirmSwipeRoutine:async request => {
      await f.callTool('owner_routine_create',{},
        {runId:request.runId,ownerRequestKey:request.eventId,allowOwnerRoutine:true,signal:request.signal} as ModelRequest,
        {sourceRevision:request.sourceRevision});},
    postPrepared:async request => {sent.push(request.text);return {acceptance:'accepted',messageId:`ack-${sent.length}`};}});
  try {
    await c.receive(inbound('offer','Every Friday summarize my loops'),context);
    f.revoke();
    await c.receive({...inbound('yes','Yes.'),replyBound:true,replyToMessageId:'ack-1'},context);
    assert.match(sent[1]!,/could not confirm that exact proposal/);
    assert.equal(modelCalls,1);
    assert.equal(f.calls.filter(call => call.operation === 'apply').length,0);
  } finally {await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('exact routine adjustment swipe selects the adjust wrapper and keeps source-loop offers out', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-adjust-swipe-')), f = fixture(folder), sent:string[]=[];
  let modelCalls=0;
  const sourceOffer = createOwnerOfferReplyClient({bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),identity,
    fetch:async () => {assert.fail('A local adjustment must not become a source offer');}});
  const c = new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerRoutineTool:f.client,
    ownerOfferReply:sourceOffer,
    model:{generate:async request => {modelCalls++;
      await f.callTool('owner_routine_adjust_propose',
        {routine_id:'f'.repeat(64),schedule:{hour:17,minute:0,weekday:5}},request);
      return {text:'review',sourceRevision:authority.revision};}},
    confirmSwipeRoutine:async request => {assert.equal(request.operation,'adjust');
      await f.callTool('owner_routine_adjust_confirm',{},
        {runId:request.runId,ownerRequestKey:request.eventId,allowOwnerRoutine:true,signal:request.signal} as ModelRequest,
        {sourceRevision:request.sourceRevision});},
    postPrepared:async request => {sent.push(request.text);return {acceptance:'accepted',messageId:`ack-${sent.length}`};}});
  try {
    await c.receive(inbound('offer','Move my Friday routine to 5pm'),context);
    assert.match(sent[0]!,/Update it/);
    assert.equal((await c.receive({...inbound('yes','Yes.'),replyBound:true,replyToMessageId:'ack-1'},context)).status,'accepted');
    assert.match(sent[1]!,/Updated\./);
    assert.equal(modelCalls,1);
    assert.equal(f.calls.filter(call => call.operation === 'apply').length,1);
  } finally {await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('lost routine swipe apply is held and never retried as a new effect', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-routine-swiped-held-')),f=fixture(folder),sent:string[]=[];
  const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerRoutineTool:f.client,
    model:{generate:async request=>{if(request.text.startsWith('Every'))
      await f.callTool('owner_routine_propose',{name:args.name,schedule:args.schedule,prompt:args.prompt},request);
      return {text:'safe answer',sourceRevision:authority.revision};}},
    confirmSwipeRoutine:async request=>{await f.callTool('owner_routine_create',{},
      {runId:request.runId,ownerRequestKey:request.eventId,allowOwnerRoutine:true,signal:request.signal} as ModelRequest,
      {sourceRevision:request.sourceRevision});},
    postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`ack-${sent.length}`};}});
  try {
    await c.receive(inbound('offer','Every Friday summarize my loops'),context);
    f.loseCreate=true;f.hideInspect=true;
    assert.equal((await c.receive({...inbound('yes','Yes.'),replyBound:true,replyToMessageId:'ack-1'},context)).status,'held');
    assert.equal(f.calls.filter(call=>call.operation==='apply').length,1);
    await c.receive(inbound('later','Hello'),context);
    assert.equal(sent.at(-1),'safe answer');
    assert.equal(f.calls.filter(call=>call.operation==='apply').length,1);
  }finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('/stop aborts a swiped routine before tool contact and only acknowledges the control', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-routine-swiped-stop-')),f=fixture(folder),sent:string[]=[];
  let entered!:()=>void,resume!:()=>void;
  const atGate=new Promise<void>(resolve=>{entered=resolve;});
  const held=new Promise<void>(resolve=>{resume=resolve;});
  const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerRoutineTool:f.client,
    model:{generate:async request=>{await f.callTool('owner_routine_propose',
      {name:args.name,schedule:args.schedule,prompt:args.prompt},request);
      return {text:'review',sourceRevision:authority.revision};}},
    confirmSwipeRoutine:async request=>{entered();await held;
      assert.equal(request.signal.aborted,true);
      // The mounted wrapper checks the aborted trusted signal before any contact.
      await assert.rejects(f.callTool('owner_routine_create',{},
        {runId:request.runId,ownerRequestKey:request.eventId,allowOwnerRoutine:true,signal:request.signal} as ModelRequest,
        {sourceRevision:request.sourceRevision}));},
    postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`ack-${sent.length}`};}});
  try {
    await c.receive(inbound('offer','Every Friday summarize my loops'),context);
    const pending=c.receive({...inbound('yes','Yes.'),replyBound:true,replyToMessageId:'ack-1'},context);
    await atGate;
    assert.equal((await c.receive(inbound('stop','/stop'),context)).status,'stopped');
    resume();await pending;
    await c.recover();
    assert.equal(f.calls.filter(call=>call.operation==='apply').length,0);
    assert.equal(sent.at(-1),'I stopped working on your earlier request. An action already sent may still have happened; please check before retrying.');
  }finally{resume?.();await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('/stop releases a hung swiped decline delivery without a late reply or routine effect', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-routine-swiped-decline-')),f=fixture(folder),sent:string[]=[];
  let entered!:()=>void,release!:()=>void;
  const atSend=new Promise<void>(resolve=>{entered=resolve;});
  const hung=new Promise<{acceptance:'accepted';messageId:string}>(resolve=>{release=()=>resolve({acceptance:'accepted',messageId:'late-decline'});});
  const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerRoutineTool:f.client,
    model:{generate:async request=>{if(request.text.startsWith('Every'))
      await f.callTool('owner_routine_propose',{name:args.name,schedule:args.schedule,prompt:args.prompt},request);
      return {text:'safe answer',sourceRevision:authority.revision};}},
    confirmSwipeRoutine:async()=>{assert.fail('Decline cannot invoke a writer');},
    postPrepared:async request=>{sent.push(request.text);
      if(request.text.startsWith('Okay, I did not')){entered();return hung;}
      return {acceptance:'accepted',messageId:`ack-${sent.length}`};}});
  try {
    await c.receive(inbound('offer','Every Friday summarize my loops'),context);
    const declining=c.receive({...inbound('no','No.'),replyBound:true,replyToMessageId:'ack-1'},context);
    await atSend;
    assert.equal((await c.receive(inbound('stop','/stop'),context)).status,'stopped');
    assert.equal((await declining).status,'held');
    assert.equal((await c.receive(inbound('safe','Hello'),context)).status,'accepted');
    release();await Promise.resolve();
    assert.equal(sent.filter(text=>text.startsWith('Okay, I did not')).length,1);
    assert.equal(sent.at(-1),'safe answer');
    assert.equal(f.calls.filter(call=>call.operation==='apply').length,0);
  }finally{release?.();await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('actual Mastra proposal -> accepted question -> separate yes -> exact create; replay and cancel use durable receipt ACKs', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-')), f = fixture(folder), sent: string[] = [];
  let step = 0;
  const calls = [{ name: 'owner_routine_propose', input: { name: args.name, schedule: args.schedule, prompt: args.prompt } }, null, { name: 'owner_routine_create', input: {} }, null, { name: 'owner_routines', input: {} }, null, { name: 'owner_routine_cancel', input: { routine_id: 'b'.repeat(64) } }, null];
  const model = new MockLanguageModelV4({ doGenerate: async () => { const call = calls[step++]; return { content: call ? [{ type: 'tool-call' as const, toolCallId: `routine-${step}`, toolName: call.name, input: JSON.stringify(call.input) }] : [{ type: 'text' as const, text: 'model completion' }], finishReason: { unified: call ? 'tool-calls' : 'stop', raw: 'STOP' }, usage, warnings: [] }; } });
  const runtime = createSottoAgentRuntime({ model, storagePath: join(folder, 'memory.db'), instructions: 'Use owner routines', sourceAuthority: f.sourceAuthority, chatTools: async () => f.tools });
  const c = new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerRoutineTool: f.client, ownerMeetingPrep: forbiddenMeetingPrep, model: { generate: r => runtime.generateChat({ ...r, threadId: 'memory', ownerId: 'owner' }) }, postPrepared: async (r) => { sent.push(r.text); return { acceptance: 'accepted', messageId: `ack-${sent.length}` }; }, canPostPrepared: r => runtime.canDeliverReplySource(r) });
  try {
    assert.equal((await c.receive(inbound('proposal', 'Every Friday at 4pm summarize my open loops'), context)).status, 'accepted');
    assert.match(sent[0]!, /Every Friday at 4pm your time/);
    assert.match(sent[0]!, /Set this up/);
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 0);
    assert.equal((await c.receive({...inbound('yes', 'Yes, please.'), replyBound: true, replyToMessageId: 'ack-1', providerSentAtMs: 946684800000}, context)).status, 'accepted');
    assert.equal(sent[1], 'Set up. Results will arrive here.');
    const create = f.calls.find(x => x.operation === 'apply')!;
    assert.equal(create.text, 'Yes, please.');
    assert.deepEqual((create.args as any).definition.prompt, args.prompt);
    assert.notEqual((create.args as any).proposal.event_id, create.event_id);
    assert.equal((await c.receive(inbound('list', 'What routines do I have?'), context)).status, 'accepted');
    assert.equal((await c.receive(inbound('cancel', 'Stop the Friday summary'), context)).status, 'accepted');
    assert.equal(sent[3], 'Stopped that routine.');
    const actions = f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_routine_tool' ORDER BY id").all();
    await c.close();
    await runtime.close();
    f.journal.close();
    const reopened = new OperationalJournal(join(folder, 'operations.sqlite'));
    reopened.recoverInterrupted();
    const recovery = new ConversationCoordinator({ journal: reopened, ownerPolicy, ownerRoutineTool: f.client, model: { generate: async () => { throw new Error('replay'); } }, postPrepared: async () => { throw new Error('resend'); } });
    assert.deepEqual(await recovery.recover(), []);
    assert.equal((await recovery.receive({...inbound('yes', 'Yes, please.'), replyBound: true, replyToMessageId: 'ack-1', providerSentAtMs: 946684800000}, context)).status, 'duplicate');
    assert.deepEqual(reopened.db.prepare("SELECT * FROM actions WHERE kind='owner_routine_tool' ORDER BY id").all(), actions);
    reopened.close();
  }
  finally {
    await runtime.close();
    try {
      f.journal.close();
    }
    catch { }
    rmSync(folder, { recursive: true, force: true });
  }
});
test('unknown create recovers by saved inspection while safe chat proceeds, never creates again', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-held-')), f = fixture(folder), sent: string[] = [];
  const make = () => new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerRoutineTool: f.client, model: { generate: async (r) => {
        if (r.text.startsWith('Every'))
          await f.callTool('owner_routine_propose', { name: args.name, schedule: args.schedule, prompt: args.prompt }, r);
        else if (r.text === 'Yes.')
          await f.callTool('owner_routine_create', {}, r);
        return { text: 'safe chat', sourceRevision: authority.revision };
      } }, postPrepared: async (r) => { sent.push(r.text); return { acceptance: 'accepted', messageId: `ack-${sent.length}` }; } });
  let c = make();
  try {
    await c.receive(inbound('proposal', 'Every Friday at4 summarize my loops'), context);
    f.loseCreate = true;
    f.hideInspect = true;
    assert.equal((await c.receive(inbound('yes', 'Yes.'), context)).status, 'held');
    await c.close();
    f.journal.recoverInterrupted();
    c = make();
    assert.equal((await c.receive(inbound('safe', 'Tell me a joke'), context)).status, 'accepted');
    assert.equal(sent.at(-1), 'safe chat');
    f.hideInspect = false;
    f.journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE status='unknown'").run();
    assert.equal((await c.recover())[0]?.status, 'accepted');
    assert.equal(sent.at(-1), 'Set up. Results will arrive here.');
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 1);
    assert.ok(f.calls.filter(x => x.operation === 'inspect').length > 0);
  }
  finally {
    await c.close();
    f.journal.close();
    rmSync(folder, { recursive: true, force: true });
  }
});
test('confirmation rejects tweaks, intervening owner input, rotations, stale source and early yes; all remain effect-free', async () => {
  for (const scenario of ['ambiguous', 'intervening', 'new', 'stop', 'stale', 'unknown-send', 'wrong-reply', 'missing-reply-id'] as const) {
    const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-confirm-')), f = fixture(folder);
    let rejection = 0;
    const c = new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerRoutineTool: f.client, ownerMeetingPrep: scenario === 'stale' ? forbiddenMeetingPrep : undefined,
      model: { generate: async (r) => {
          if (r.text.startsWith('Every'))
            await f.callTool('owner_routine_propose', { name: args.name, schedule: args.schedule, prompt: args.prompt }, r);
          else if (r.text.startsWith('Yes')) {
            await assert.rejects(f.callTool('owner_routine_create', {}, r));
            rejection++;
          }
          return { text: 'Please clarify.', sourceRevision: (await f.sourceAuthority.read()).revision };
        } },
      rotateSession: async () => { }, postPrepared: async () => scenario === 'unknown-send' && f.calls.length === 1 ? { acceptance: 'unknown' } : { acceptance: 'accepted', messageId: 'ack' } });
    try {
      await c.receive(inbound('proposal', 'Every Friday summarize my loops'), context);
      if (scenario === 'intervening')
        await c.receive(inbound('other', 'Tell me a joke'), context);
      if (scenario === 'new')
        await c.receive(inbound('rotate', '/new'), context);
      if (scenario === 'stop')
        await c.receive(inbound('stop', '/stop'), context);
      if (scenario === 'stale')
        f.revoke();
      await c.receive({...inbound('yes', scenario === 'ambiguous' ? 'Yes, but make it five.' : 'Yes.'),
        ...(scenario === 'stale' ? {replyBound: true, replyToMessageId: 'ack'} : {}),
        ...(scenario === 'wrong-reply' ? {replyBound: true, replyToMessageId: 'other-question'} : {}),
        ...(scenario === 'missing-reply-id' ? {replyBound: true} : {}),
      }, context);
      assert.equal(rejection, 1, scenario);
      assert.equal(f.calls.filter(x => x.operation === 'apply').length, 0, scenario);
    }
    finally {
      await c.close();
      f.journal.close();
      rmSync(folder, { recursive: true, force: true });
    }
  }
});
test('routine authority and composite mutation fences reject before any writer contact', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-fence-')), f = fixture(folder);
  const c = new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerRoutineTool: f.client, model: { generate: async (r) => {
        for (const overrides of [{ mode: 'unattended' }, { runId: 'spoof' }, { ownerRequestKey: 'spoof' }, { allowOwnerRoutine: false }, { sourceRevision: 'e'.repeat(64) }])
          await assert.rejects(f.callTool('owner_routine_propose', { name: args.name, schedule: args.schedule, prompt: args.prompt }, r, overrides));
        assert.equal(f.calls.length, 0);
        await f.callTool('owner_routine_propose', { name: args.name, schedule: args.schedule, prompt: args.prompt }, r);
        const authority = { runId: r.runId, accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon' as const, scopeHash: f.client.scopeHash };
        assert.throws(() => f.journal.prepareOwnerPreferenceToolAction(authority, r.ownerRequestKey, { operation: 'clear-tone' }));
        assert.throws(() => f.journal.prepareOwnerTaskToolAction(authority, r.ownerRequestKey, { operation: 'add', text: 'Friday' }));
        assert.throws(() => f.journal.prepareOwnerReminderToolAction(authority, r.ownerRequestKey, { due: '2026-10-09T08:15:00Z', action: 'Friday' }));
        return { text: 'unused', sourceRevision: authority.scopeHash };
      } }, postPrepared: async () => ({ acceptance: 'accepted', messageId: 'ack' }) });
  try {
    await c.receive(inbound('proposal', 'Every Friday summarize loops'), context);
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 0);
  }
  finally {
    await c.close();
    f.journal.close();
    rmSync(folder, { recursive: true, force: true });
  }
});


test('receiver preview refusal records a factual refusal without a question or create contact', async () => {
  for (const code of ['routine_limit','routine_conflict','unsupported_definition'] as const) {
    const folder = mkdtempSync(join(tmpdir(),'sotto-routine-refusal-'));
    const f = fixture(folder);
    const sent: string[] = [];
    f.previewRefusal = code;
    const coordinator = new ConversationCoordinator({
      journal: f.journal, ownerPolicy, ownerRoutineTool: f.client,
      model: {generate: async request => {
        assert.deepEqual(await f.callTool('owner_routine_propose',{name:args.name,schedule:args.schedule,prompt:args.prompt},request), {status:'rejected',code});
        return 'Should I create this routine?';
      }},
      postPrepared: async request => {sent.push(request.text);return {acceptance:'accepted',messageId:'refused'};},
    });
    try {
      assert.equal((await coordinator.receive(inbound('proposal','Every Friday summarize my loops'),context)).status,'accepted');
      assert.ok(!sent[0]!.includes('Should I create'));
      assert.equal(f.calls.filter(row=>row.operation==='apply').length,0);
      const action=f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_routine_tool'").get()!;
      assert.equal(action.status,'rejected');
    } finally {await coordinator.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
  }
});

for (const stale of [false, true]) test(`exact no to local routine keeps local precedence, stale=${stale}`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-local-offer-priority-')), f = fixture(folder);
  const replies: string[] = [];
  const sourceOffer = createOwnerOfferReplyClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async () => {assert.fail('Local proposal cannot be consumed as a source-loop offer');}});
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerRoutineTool: f.client,
    ownerOfferReply: sourceOffer, model: {generate: async request => {
      if (request.text === 'Every Friday summarize my open loops') await f.callTool('owner_routine_propose', {name: args.name, schedule: args.schedule, prompt: args.prompt}, request);
      else assert.equal(request.text, 'No.');
      return {text: 'I will not create it.', sourceRevision: (await f.sourceAuthority.read()).revision};
    }}, postPrepared: async request => {replies.push(request.text); return {acceptance: 'accepted', messageId: `local-${replies.length}`};}});
  try {
    await coordinator.receive(inbound('local-proposal', 'Every Friday summarize my open loops'), context);
    if (stale) f.revoke();
    await coordinator.receive({...inbound('local-no', 'No.'), replyBound: true, replyToMessageId: 'local-1'}, context);
    assert.equal(replies.at(-1), 'I will not create it.');
    assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_offer_reply'").get()!.n, 0);
    assert.equal(f.calls.filter(call => call.operation === 'apply').length, 0);
  } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

 test('reopened prepared approval review retains verbatim delivery exemption', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-verbatim-')), f = fixture(folder);
  let step = 0;
  const model = new MockLanguageModelV4({doGenerate: async () => ({content: step++ === 0
    ? [{type:'tool-call' as const, toolCallId:'review',toolName:'owner_routine_propose',input:JSON.stringify({name: args.name, schedule: args.schedule, prompt: '**Exact prompt** — preserve'})}]
    : [{type:'text' as const,text:'finished'}],finishReason:{unified:step === 1 ? 'tool-calls' : 'stop',raw:'STOP'},usage,warnings:[]})});
  const runtime = createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Review',sourceAuthority:f.sourceAuthority,chatTools:async()=>f.tools});
  const c = new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerRoutineTool:f.client,model:{generate:r=>runtime.generateChat({...r,threadId:'memory',ownerId:'owner'})},canPostPrepared:async()=>false,postPrepared:async()=>{throw Error('blocked');}});
  try {
    assert.equal((await c.receive(inbound('review','Prepare my review'),context)).status,'held');
    await c.close(); await runtime.close(); f.journal.close();
    const reopened = new OperationalJournal(join(folder,'operations.sqlite')); reopened.recoverInterrupted();
    let delivered = false;
    const recovery = new ConversationCoordinator({journal:reopened,ownerPolicy,ownerRoutineTool:f.client,model:{generate:async()=>{throw Error('no model');}},canPostPrepared:async()=>true,postPrepared:async r=>{
      assert.equal(r.verbatim,true); assert.match(r.text,/\*\*Exact/); assert.match(r.text,/— preserve/); delivered=true; return {acceptance:'accepted',messageId:'review-message'};
    }});
    await recovery.recover(); assert.equal(delivered,true); await recovery.close(); reopened.close();
  } finally {await runtime.close(); try {f.journal.close();} catch {} rmSync(folder,{recursive:true,force:true});}
});

 test('weekday literal adds exact review while daily and weekly serialized hashes remain stable',()=>{
  const definition={destination:'this Photon conversation' as const,name:'user-test',prompt:'Read my loops.',schedule:{hour:9,minute:0,weekday:null as null|number|'weekdays'},zone:'America/Los_Angeles'};
  for(const [weekday,expected] of [[null,'aa240ca6882e7b8fde19ae1aa041d7eaf1c8130a07707fab64c5b51005f19724'],[1,'90904580251ccd1def07935e49582aa6e8a1519af133133c61e088ecef34e194']] as const){const old={...definition,schedule:{hour:9,minute:0,weekday}};assert.equal(routineDefinitionHash(old),expected);}
  const next={...definition,schedule:{hour:9,minute:0,weekday:'weekdays' as const}};
  assert.match(routineProposalText({version:1,status:'preview',event_id:'a'.repeat(64),payload_hash:'a'.repeat(64),definition:next,definition_hash:routineDefinitionHash(next)}),/^Every weekday \(Monday–Friday\) at 9am/);
  assert.equal(routineScheduleSchema.safeParse({...next.schedule,weekday:'1-5'}).success,false);
 });

test('adjust preview preserves exact task and zone; old create preview cannot authorize adjustment',async()=>{
  const {routinePreviewMatches,ownerRoutineToolReceiptSchema,routineReceiptMatches}=await import('../src/tools/owner-routine-tool-contract.js');
  const previous={name:args.name,schedule:args.schedule,prompt:args.prompt,zone:'UTC',destination:'this Photon conversation' as const};
  const definition={...previous,schedule:{hour:17,minute:0,weekday:5}};
  const request={operation:'propose_adjust' as const,routine_id:'f'.repeat(64),schedule:definition.schedule};
  const preview={version:1 as const,status:'preview' as const,event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),operation:'adjust' as const,routine_id:request.routine_id,previous_definition:previous,previous_definition_hash:routineDefinitionHash(previous),definition,definition_hash:routineDefinitionHash(definition)};
  assert.equal(routinePreviewMatches(preview,request),true);
  assert.equal(routinePreviewMatches({...preview,definition:{...definition,prompt:'Changed task'},definition_hash:routineDefinitionHash({...definition,prompt:'Changed task'})},request),false);
  assert.equal(routinePreviewMatches({version:1,status:'preview',event_id:preview.event_id,payload_hash:preview.payload_hash,definition,definition_hash:preview.definition_hash},request),false);
  assert.match(routineProposalText(preview),/From: Every Friday at 4pm UTC\nTo: Every Friday at 5pm UTC/);
  const receipt=ownerRoutineToolReceiptSchema.parse({version:1,status:'committed',action_id:'a'.repeat(64),event_id:'b'.repeat(64),payload_hash:'c'.repeat(64),args_hash:'d'.repeat(64),outcome:{operation:'adjust',routine_id:createHash('sha256').update(JSON.stringify(['sotto.fresh-routine.v1','a'.repeat(64)])).digest('hex'),definition_hash:preview.definition_hash,status:'active',previous_routine_id:request.routine_id,previous_definition_hash:preview.previous_definition_hash}});
  const apply={operation:'adjust' as const,routine_id:request.routine_id,previous_definition_hash:preview.previous_definition_hash,definition,proposal:{event_id:'a'.repeat(64),action_id:'b'.repeat(64),args_hash:'c'.repeat(64),accepted_dispatch_id:'accepted',source_revision:'d'.repeat(64)}};
  assert.equal(routineReceiptMatches(receipt,apply),true);assert.equal(routineReceiptMatches({...receipt,outcome:{...receipt.outcome,routine_id:'e'.repeat(64)}},apply),false);assert.equal(routineReceiptMatches(receipt,{...apply,routine_id:'0'.repeat(64)}),false);
});

test('adjust preview supports escaped full valid task under scoped64KiB response bound',async()=>{
  const previous={name:args.name,schedule:args.schedule,prompt:'😀'.repeat(2000),zone:'UTC',destination:'this Photon conversation' as const};
  const definition={...previous,schedule:{hour:17,minute:0,weekday:5}};
  const input={operation:'propose_adjust' as const,routine_id:'f'.repeat(64),schedule:definition.schedule};
  const eventId='a'.repeat(64),binding=stableActionIdentity(eventId,ownerRoutineToolKind,input);
  const client=createOwnerRoutineToolClient({bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),identity,fetch:async()=>{
    const text=JSON.stringify({version:1,status:'preview',event_id:eventId,payload_hash:'b'.repeat(64),operation:'adjust',routine_id:input.routine_id,previous_definition:previous,previous_definition_hash:routineDefinitionHash(previous),definition,definition_hash:routineDefinitionHash(definition)}).replace(/[^\x00-\x7f]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
    assert.ok(Buffer.byteLength(text)>16384&&Buffer.byteLength(text)<65536);return new Response(text);
  }});
  assert.equal((await client.call('preview',{actionId:binding.actionId,eventId,payloadHash:'b'.repeat(64),text:'Move the routine to5pm',admittedAtMs:Date.now(),providerThreadId:'thread',args:input})).status,'preview');
});

 test('first-of-month literal preserves old hashes and full adjustment review',async()=>{
 const {routinePreviewMatches}=await import('../src/tools/owner-routine-tool-contract.js');
 const previous={destination:'this Photon conversation' as const,name:'user-test',prompt:'Read my loops.',schedule:{hour:9,minute:0,weekday:null},zone:'America/Los_Angeles'};
 assert.equal(routineDefinitionHash(previous),'aa240ca6882e7b8fde19ae1aa041d7eaf1c8130a07707fab64c5b51005f19724');
 const definition={...previous,schedule:{hour:9,minute:0,weekday:'first_of_month' as const}};
 assert.equal(routineScheduleSchema.safeParse(definition.schedule).success,true);
 for(const weekday of ['monthly','1','day_2','0 9 1 * *'])assert.equal(routineScheduleSchema.safeParse({...definition.schedule,weekday}).success,false);
 assert.equal(routineScheduleSchema.safeParse({...definition.schedule,day:1}).success,false);
 const preview={version:1 as const,status:'preview' as const,event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),operation:'adjust' as const,routine_id:'c'.repeat(64),previous_definition:previous,previous_definition_hash:routineDefinitionHash(previous),definition,definition_hash:routineDefinitionHash(definition)};
 assert.equal(routinePreviewMatches(preview,{operation:'propose_adjust',routine_id:preview.routine_id,schedule:definition.schedule}),true);
 assert.match(routineProposalText(preview),/From: Every day at 9am Pacific Time\nTo: On the first day of every month at 9am Pacific Time/);
 assert.match(routineProposalText({version:1,status:'preview',event_id:preview.event_id,payload_hash:preview.payload_hash,definition,definition_hash:preview.definition_hash}),/^On the first day of every month at 9am/);
 });

test('hourly schedule preserves legacy hashes and renders local minute precisely',()=>{
 const previous={destination:'this Photon conversation' as const,name:'user-test',prompt:'Read my loops.',schedule:{hour:9,minute:0,weekday:null},zone:'America/Los_Angeles'};
 assert.equal(routineDefinitionHash(previous),'aa240ca6882e7b8fde19ae1aa041d7eaf1c8130a07707fab64c5b51005f19724');
 const definition={...previous,schedule:{hour:null,minute:15,weekday:null}};
 assert.equal(routineScheduleSchema.safeParse(definition.schedule).success,true);
 for(const weekday of [0,6,'weekdays','first_of_month'])assert.equal(routineScheduleSchema.safeParse({...definition.schedule,weekday}).success,false);
 for(const schedule of [{hour:null,weekday:null},{hour:null,minute:60,weekday:null},{hour:null,minute:-1,weekday:null}])assert.equal(routineScheduleSchema.safeParse(schedule).success,false);
 const preview={version:1 as const,status:'preview' as const,event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),definition,definition_hash:routineDefinitionHash(definition)};
 assert.match(routineProposalText(preview),/^Every hour at 15 minutes past your time:\nRead my loops\./);
 assert.match(routineProposalText({...preview,operation:'adjust',routine_id:'c'.repeat(64),previous_definition:previous,previous_definition_hash:routineDefinitionHash(previous)}),/From: Every day at 9am Pacific Time\nTo: Every hour at 15 minutes past Pacific Time/);
});


test('routine previews use plain local times without exposing transport names or schedule IDs',()=>{
 for(const [hour,minute,clock] of [[0,0,'12am'],[12,0,'12pm'],[16,5,'4:05pm']] as const){
  const definition={name:'user-private-label',schedule:{hour,minute,weekday:5},prompt:'Check the test results.',zone:'America/Los_Angeles',destination:'this Photon conversation' as const};
  const before=JSON.stringify(definition);
  const text=routineProposalText({version:1,status:'preview',event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),definition,definition_hash:routineDefinitionHash(definition)});
  assert.equal(text,`Every Friday at ${clock} your time:\nCheck the test results.\n\nI'll send the result here. Set this up?`);
  assert.doesNotMatch(text,/Photon|Telegram|user-private-label|America\//);
  assert.equal(JSON.stringify(definition),before,'presentation never rewrites the reviewed definition');
 }
});
