import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { RequestContext } from '@mastra/core/request-context';
import { MockLanguageModelV4 } from 'ai/test';
import {createTool} from '@mastra/core/tools';
import {z} from 'zod';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator, type ModelRequest, type ConversationDependencies } from '../src/conversation.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createOwnerTaskToolClient } from '../src/tools/owner-task-tool-client.js';
import {createOwnerTaskExecution} from '../src/tools/owner-task-execution.js';
import { createOwnerTaskTools } from '../src/tools/owner-task-tool.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { ownerTaskToolKind } from '../src/tools/owner-task-tool-contract.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import {createTaskCommand} from '../src/tools/task-command.js';

const identity = {tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64)};
const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: 'owner'};
const context = {threadId: 'thread', destination: 'owner', lineKey: 'line'};
const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: 'owner',
  isDirect: true, eventId, text});
const args = {operation: 'add' as const, text: 'Review the cedar packet'};
const authority = {version: 2 as const, complete: true, revision: 'd'.repeat(64), bridge_sources: [], google_sources: []};
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status});
const usage = {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}};
function fixture(folder: string) {
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  const calls: Record<string, unknown>[] = [], receipts = new Map<string, Record<string, unknown>>();
  let loseCreate = false, hideInspect = false, state = authority;
  const client = createOwnerTaskToolClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string); calls.push(body);
      assert.equal(body.text_hash, createHash('sha256').update(body.text).digest('hex')); assert.ok(!body.text.startsWith('/loop'));
      if (body.operation === 'inspect') return reply(!hideInspect && receipts.get(body.action_id) || {version: 1, status: 'not_found'});
      const binding = stableActionIdentity(body.event_id, ownerTaskToolKind, body.args);
      const receipt = {version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
        payload_hash: body.payload_hash, args_hash: binding.payloadHash, operation: body.args.operation, anchor_key: body.args.anchor ?? 'owner:test', post_digest: 'b'.repeat(64), loop_status: body.args.operation === 'close' ? 'resolved' : 'open'};
      receipts.set(body.action_id, receipt); if (loseCreate) throw new Error('lost after commit'); return reply(receipt);
    }});
  const sourceAuthority = {read: async () => state};
  const tools = createOwnerTaskTools({client, journal: () => journal, sourceAuthority});
  const callTool = async (name: keyof typeof tools, input: unknown, request: ModelRequest, overrides: Record<string, unknown> = {}) => {
    const requestContext = new RequestContext();
    for (const [key, value] of Object.entries({mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey,
      sourceRevision: state.revision, allowOwnerTask: request.allowOwnerTask, ...overrides})) requestContext.set(key, value);
    return tools[name].execute!(input as never, {requestContext, abortSignal: request.signal} as never);
  };
  return {get journal() {return journal;}, client, calls, tools, sourceAuthority, callTool,
    set loseCreate(value: boolean) {loseCreate = value;}, set hideInspect(value: boolean) {hideInspect = value;},
    revoke() {state = {...authority, revision: 'e'.repeat(64)};},
    reopen() {journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();}};
}

for(const mode of ['repair','repeat','zero-width','question','honest','retrospective','valid-combined','invalid-combined','step-limit','uncertain','source','cancel'] as const)test(`reserved task receipt guard: ${mode}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'task-receipt-guard-')),f=fixture(folder),sent:string[]=[];
 let steps=0,nextTurn=false,nextSteps=0;
 const text=(value:string)=>({type:'text' as const,text:value});
 const call=(input:unknown)=>({type:'tool-call' as const,toolCallId:'add-'+steps,toolName:'owner_task_add',input:JSON.stringify(input)});
 if(mode==='uncertain'){f.loseCreate=true;f.hideInspect=true;}
 const model=new MockLanguageModelV4({doGenerate:async()=>{
  steps++;
  let content:Array<ReturnType<typeof text>|ReturnType<typeof call>>;
  if(nextTurn)content=++nextSteps===1?[text('Added: Second task')]:[call({text:'Second task'})];
  else if(mode==='step-limit')content=[text(`Added: ${args.text}`),call({wrong:args.text})];
  else if(steps===1){
   if(mode==='source')f.revoke();if(mode==='cancel')void coordinator.close();
   content=mode==='uncertain'?[call({text:args.text})]:mode==='valid-combined'?[text(`Added: ${args.text}`),call({text:args.text})]:mode==='invalid-combined'?[text(`Added: ${args.text}`),call({wrong:args.text})]:[text(`${mode==='zero-width'?'\u200B\u2060':''}Added: ${args.text}`)];
  }else if((mode==='repair'||mode==='source')&&steps===2)content=[call({text:args.text})];
  else if(mode==='retrospective')content=[text(`Earlier, I said “Added: ${args.text}”. That does not prove a new addition.`)];
  else if(mode==='honest'||mode==='question')content=[text('I have not added a task.')];
  else content=[text(`Added: ${args.text}`)];
  return {content,finishReason:{unified:content.some(part=>part.type==='tool-call')?'tool-calls':'stop',raw:'STOP'},usage,warnings:[]};
 }});
 const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Use task tools only for the current owner request.',sourceAuthority:f.sourceAuthority,chatTools:async()=>f.tools});
 const coordinator=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerTaskTool:f.client,model:{generate:request=>runtime.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'ack-'+sent.length};}});
 try{
  await coordinator.receive(inbound('receipt-guard',mode==='retrospective'?`What did your earlier acknowledgement about ${args.text} say?`:mode==='question'?`Should I add ${args.text}?`:`Add ${args.text}`),context);
  const actions=f.journal.db.prepare("SELECT status FROM actions WHERE kind='owner_loop_tool'").all();
  if(mode==='repair'||mode==='valid-combined'){
   assert.equal(steps,mode==='repair'?2:1);assert.equal(f.calls.filter(row=>row.operation==='apply').length,1);assert.equal(actions.length,1);assert.equal(actions[0]!.status,'succeeded');assert.deepEqual(sent,[`Added: ${args.text}`]);
  }else if(mode==='uncertain'){
   assert.equal(steps,2,'uncertain mutation cannot buy a corrective retry');assert.equal(f.calls.filter(row=>row.operation==='apply').length,1);assert.equal(actions[0]!.status,'unknown');assert.ok(sent.every(value=>!value.startsWith('Added:')));
  }else{
   assert.equal(f.calls.length,0);assert.equal(actions.length,0);assert.ok(sent.every(value=>!value.startsWith('Added:')));
   if(mode==='cancel')assert.equal(sent.length,0);
   else if(mode==='repeat'||mode==='zero-width')assert.deepEqual(sent,['I couldn’t record that task.']);
   else if(mode==='invalid-combined'||mode==='step-limit')assert.deepEqual(sent,['I couldn’t confirm whether that task was added.']);
   if(mode==='retrospective')assert.match(sent[0]!,/^Earlier, I said/);
   if(mode!=='source'&&mode!=='cancel')assert.equal(steps,mode==='step-limit'?60:2);
  }
  if(mode==='repeat'){
   nextTurn=true;await coordinator.receive(inbound('receipt-guard-next','Add Second task'),context);
   assert.equal(nextSteps,2,'a fresh turn gets exactly one fresh correction');assert.equal(f.calls.filter(row=>row.operation==='apply').length,1);assert.equal(sent.at(-1),'Added: Second task');
  }
 }finally{await coordinator.close();await runtime.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

for(const value of ['> Added: prior task','\u200B> Added: prior task','“Added: prior task”','```text\nAdded: prior task\n```'] as const)test(`quoted task receipt remains ordinary text: ${JSON.stringify(value)}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'quoted-task-receipt-'));let calls=0;
 const runtime=createSottoAgentRuntime({model:new MockLanguageModelV4({doGenerate:async()=>{calls++;return {content:[{type:'text',text:value}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};}}),storagePath:join(folder,'memory.db'),instructions:'Quote the prior wording.'});
 try{assert.equal((await runtime.generateChat({text:'Quote the earlier acknowledgement.',threadId:'memory',ownerId:'owner',allowOwnerTask:true})).text,value);assert.equal(calls,1);}
 finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});

test('an unknown non-task tool attempt cannot buy a task correction and its coordinator outcome remains held',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'other-effect-receipt-')),f=fixture(folder);let calls=0,effects=0;const replies:string[]=[];
 const opaque=createTool({id:'opaque_tool',description:'Synthetic unknown effect',inputSchema:z.object({}),execute:async(_input,ctx)=>{
  effects++;
  f.journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,'unknown',?,?,?)")
   .run('synthetic-other-effect',ctx.requestContext!.get('runId'),'owner_calendar_tool','a'.repeat(64),ctx.requestContext!.get('ownerRequestKey'),'{}',Date.now(),Date.now());
  return {status:'unknown'};
 }});
 const runtime=createSottoAgentRuntime({model:new MockLanguageModelV4({doGenerate:async()=>({content:++calls===1?[{type:'tool-call',toolCallId:'opaque',toolName:'opaque_tool',input:'{}'}]:[{type:'text',text:`Added: ${args.text}`}],finishReason:{unified:calls===1?'tool-calls':'stop',raw:'STOP'},usage,warnings:[]})}),storagePath:join(folder,'memory.db'),instructions:'Synthetic fixture',sourceAuthority:f.sourceAuthority,chatTools:()=>({...f.tools,opaque_tool:opaque})});
 const coordinator=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerTaskTool:f.client,ownerCalendar:f.client as never,model:{generate:request=>runtime.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async post=>{replies.push(post.text);return {acceptance:'accepted',messageId:'held-notice'};}});
 try{
  assert.equal((await coordinator.receive(inbound('other-effect','Synthetic current request'),context)).status,'held');
  assert.equal(calls,2);assert.equal(effects,1);assert.equal(replies.length,1);assert.match(replies[0]!,/^I couldn’t confirm whether/);assert.ok(!replies[0]!.startsWith('Added:'));assert.equal(f.calls.length,0);
  assert.equal(f.journal.getAction('synthetic-other-effect')!.status,'unknown');
 }finally{await coordinator.close();await runtime.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('presentation-only shortening can reproduce quoted receipt wording without tools or repair',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'short-task-receipt-'));let calls=0;
 const runtime=createSottoAgentRuntime({model:new MockLanguageModelV4({doGenerate:async()=>{calls++;return {content:[{type:'text',text:'Added: prior task'}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};}}),storagePath:join(folder,'memory.db'),instructions:'Preserve prior wording.',answerOnlyInstructions:'Shorten accepted wording only.',chatTools:()=>{throw Error('presentation must not discover tools');}});
 try{assert.equal((await runtime.generateChat({text:'shorter pls',acceptedEditContext:{text:'Added: prior task',sourceRevision:'owner-only-context-v1'},threadId:'memory',ownerId:'owner',allowOwnerTask:true})).text,'Added: prior task');assert.equal(calls,1);}
 finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});


test('real Mastra add tool records one action; source revocation after effect produces owner-only receipt ACK and no replay', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-natural-task-')), f = fixture(folder), sent: string[] = []; let step = 0;
  const timings:Array<Parameters<NonNullable<ConversationDependencies['onOwnerTaskTiming']>>[0]>=[];
  const call=f.client.call;
  f.client.call=async(...request)=>{const result=await call(...request);f.revoke();return result;};
  const model = new MockLanguageModelV4({doGenerate: async () => {
    step++;
    if (step === 1) return {content: [{type: 'tool-call', toolCallId: 'add', toolName: 'owner_task_add', input: JSON.stringify({text: args.text})}],
      finishReason: {unified: 'tool-calls', raw: 'tool_calls'}, usage, warnings: []};
    throw new Error('settled add must not buy a discarded model continuation');
  }});
  const runtime = createSottoAgentRuntime({model, storagePath: join(folder, 'memory.db'), instructions: 'Use task tools',
    sourceAuthority: f.sourceAuthority, chatTools: async () => f.tools});
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerTaskTool: f.client,
    model: {generate: request => runtime.generateChat({...request, threadId: 'memory', ownerId: 'owner'})},
    onOwnerTaskTiming:timing=>{timings.push(timing);throw new Error('diagnostic sink unavailable');},
    postPrepared: async request => {await new Promise(resolve=>setTimeout(resolve,12));sent.push(request.text); return {acceptance: 'accepted', messageId: 'ack'};}});
  try {
    assert.equal((await coordinator.receive(inbound('add', `Add this task: ${args.text}`), context)).status, 'accepted');
    assert.equal(step, 1); assert.deepEqual(f.calls.map(row => row.operation), ['apply']);
    assert.equal(sent[0],`Added: ${args.text}`);
    assert.equal(timings.length,1);const timing=timings[0]!;
    assert.deepEqual(Object.keys(timing).sort(),['acknowledgementMs','dispatchMs','receiptToDispatchMs','admissionToDispatchMs','outcome'].sort());
    assert.equal(timing.outcome,'accepted');assert.ok(timing.dispatchMs>=8);
    for(const [key,value]of Object.entries(timing))if(key!=='outcome')assert.ok(Number.isSafeInteger(value)&&Number(value)>=0);
    assert.ok(timing.admissionToDispatchMs>=timing.receiptToDispatchMs);assert.ok(!JSON.stringify(timing).includes(args.text));
    assert.equal((await coordinator.receive(inbound('add', `Add this task: ${args.text}`), context)).status, 'duplicate');
    await coordinator.close(); await runtime.close(); f.reopen();
    const recovered = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerTaskTool: f.client,
      model: {generate: async () => {throw new Error('regenerated');}}, postPrepared: async () => {throw new Error('resent');}});
    assert.deepEqual(await recovered.recover(), []); f.journal.pruneTerminal(Date.now() + 1000);
    assert.equal(f.journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_loop_tool'").get()!.n, 1);
    assert.ok(!JSON.stringify(model.doGenerateCalls).includes('c'.repeat(64)));
  } finally {await runtime.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('admitted shared add hands its settled receipt to the coordinator without another model step',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'task-script-handoff-')),f=fixture(folder),sent:string[]=[];let steps=0;
 const script=createRunScriptTool({mode:'chat',executeCommand:createTaskCommand(createOwnerTaskExecution({client:f.client,journal:()=>f.journal,sourceAuthority:f.sourceAuthority})),commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerTask:ctx?.get('allowOwnerTask'),sourceRevision:ctx?.get('sourceRevision'),signal})});
 const model=new MockLanguageModelV4({doGenerate:async()=>{assert.equal(++steps,1);return {content:[{type:'tool-call',toolCallId:'add',toolName:'run_sotto_script',input:JSON.stringify({id:'knowledge-edit',args:['--op','loop-add','--text',args.text]})}],finishReason:{unified:'tool-calls',raw:'tool_calls'},usage,warnings:[]};}});
 const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Use task command',sourceAuthority:f.sourceAuthority,chatTools:async()=>({run_sotto_script:script})});
 const coordinator=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerTaskTool:f.client,model:{generate:request=>runtime.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'ack'};}});
 try{assert.equal((await coordinator.receive(inbound('add',`Add this task: ${args.text}`),context)).status,'accepted');assert.equal(steps,1);assert.deepEqual(f.calls.map(row=>row.operation),['apply']);assert.deepEqual(sent,[`Added: ${args.text}`]);}
 finally{await coordinator.close();await runtime.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('unknown task survives restart; safe unrelated chat and new single task proceed; inspect alone recovers old effect', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-task-held-')), f = fixture(folder), sent: string[] = [];
  f.loseCreate = true; f.hideInspect = true;
  const first = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerTaskTool: f.client,
    model: {generate: async request => {await f.callTool('owner_task_add', {text: args.text}, request); return 'no';}},
    postPrepared: async () => {throw new Error('uncertain ack');}});
  try {
    assert.equal((await first.receive(inbound('held', `Add task ${args.text}`), context)).status, 'held');
    const saved = f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_loop_tool'").get()!;
    await first.close(); f.reopen(); f.loseCreate = false;
    const second = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerTaskTool: f.client,
      model: {generate: async request => {if (request.text.includes('Second task')) await f.callTool('owner_task_add', {text: 'Second task'}, request); return 'safe answer';}},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    assert.equal((await second.recover())[0]?.status, 'held');
    assert.equal((await second.receive(inbound('safe', 'What is next?'), context)).status, 'accepted');
    assert.equal((await second.receive(inbound('second', 'Add Second task'), context)).status, 'accepted');
    assert.equal(f.journal.getAction(saved.id as string)?.status, 'unknown');
    f.hideInspect = false;
    f.journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(saved.id);
    assert.equal((await second.recover())[0]?.status, 'accepted');
    assert.equal(f.calls.filter(row => row.operation === 'apply').length, 2);
    for (const request of f.calls.filter(row => row.operation === 'inspect')) assert.deepEqual(request.args, args);
  } finally {f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('trusted task tool denies bad authority, nonowner span, stale revision, changed args and task/reminder composite before contact', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-task-authority-')), f = fixture(folder);
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerTaskTool: f.client,
    model: {generate: async request => {
      for (const overrides of [{mode: 'unattended'}, {runId: 'spoof'}, {ownerRequestKey: 'spoof'}, {allowOwnerTask: false}])
        await assert.rejects(f.callTool('owner_task_add', {text: args.text}, request, overrides));
      await assert.rejects(f.callTool('owner_task_add', {text: 'Paraphrased task'}, request));
      await assert.rejects(f.callTool('owner_task_add', {text: args.text}, request, {sourceRevision: 'f'.repeat(64)}));
      assert.equal(f.calls.length, 0);
      await f.callTool('owner_task_add', {text: args.text}, request);
      await assert.rejects(f.callTool('owner_task_close', {anchor: 'owner:test'}, request));
      const auth = {runId: request.runId, accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon' as const, scopeHash: f.client.scopeHash};
      assert.throws(() => f.journal.prepareOwnerReminderToolAction(auth, request.ownerRequestKey, {due: '2026-10-09T08:15:00Z', action: 'same event'}));
      assert.equal(f.calls.length, 1); return 'not used';
    }}, postPrepared: async () => ({acceptance: 'accepted', messageId: 'ack'})});
  try {assert.equal((await coordinator.receive(inbound('authority', `Add ${args.text}`), context)).status, 'accepted');}
  finally {f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('batch ambiguity never attributes a write to first event', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-task-batch-')), f = fixture(folder);
  let first = true;
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerTaskTool: f.client,
    model: {generate: async request => {
      if (first) {first = false; throw new Error('first turn held');}
      assert.equal(request.allowOwnerTask, false);
      await assert.rejects(f.callTool('owner_task_add', {text: args.text}, request)); return 'Please send one task request.';
    }}, postPrepared: async () => ({acceptance: 'accepted', messageId: 'ack'})});
  try {
    await coordinator.receive(inbound('first', `Add ${args.text}`), context);
    assert.equal((await coordinator.receive(inbound('second', 'And close another task'), context)).status, 'accepted');
    assert.equal(f.calls.length, 0);
  } finally {f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

for (const closeDuringRead of [false, true, 'preflight'] as const) test(`correction receipt readback ${closeDuringReadOf(closeDuringRead)}`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-correction-readback-')), f = fixture(folder);
  let release!: () => void, entered!: () => void;
  const waiting = new Promise<void>(done => {entered = done;});
  const gate = new Promise<void>(done => {release = done;});
  const sent: string[] = [];
  const correction = {operation:'correct' as const,anchor:'source-loop',loop_version:'a'.repeat(64),loop_identity:'b'.repeat(64),source_revision:authority.revision,correction:'I handled the packet',to:'resolved' as const};
  const client = {...f.client,readback:async (_args:unknown,_receipt:unknown,signal?:AbortSignal) => {
    if(closeDuringRead!=='preflight')entered(); if(closeDuringRead===true)await gate;
    if(closeDuringRead==='preflight')return {you_owe:0,waiting_on_them:0,source_revision:authority.revision};
    if(signal?.aborted)throw new Error('cancelled');
    throw new Error('source revoked');
  }};
  const coordinator = new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerTaskTool:client,
    model:{generate:async request=>{
      const requestAuthority={runId:request.runId,channel:identity.channel,accountId:identity.account_id,ownerId:identity.owner_id,lineKey:identity.line_key,scopeHash:client.scopeHash};
      const prepared=f.journal.prepareOwnerTaskToolAction(requestAuthority,request.ownerRequestKey!,correction);
      f.journal.beginAction(prepared.actionId);
      f.journal.settleOwnerTaskToolAction(prepared.actionId,requestAuthority,{version:1,status:'committed',action_id:prepared.actionId,event_id:prepared.eventId,payload_hash:prepared.payloadHash,args_hash:stableActionIdentity(prepared.eventId,ownerTaskToolKind,correction).payloadHash,operation:'correct',anchor_key:'source-loop',post_digest:'c'.repeat(64),loop_status:'resolved'});
      return 'untrusted model success';
    }},canPostPrepared:async()=>{if(closeDuringRead==='preflight'){entered();await gate;}return true;},postPrepared:async post=>{sent.push(post.text);return {acceptance:'accepted',messageId:'ack'};}});
  try {
    const result=coordinator.receive(inbound('correction',correction.correction),context);
    await waiting;
    if(closeDuringRead){const closing=coordinator.close();release();await closing;assert.ok(['cancelled','held'].includes((await result).status));assert.equal(sent.length,0);}
    else {assert.equal((await result).status,'accepted');assert.match(sent[0]!,/recorded, but I couldn’t verify/);assert.ok(!sent[0]!.includes('currently available list'));}
    assert.equal(f.journal.db.prepare("SELECT status FROM actions WHERE kind='owner_loop_tool'").get()!.status,'succeeded');
  } finally {release();await coordinator.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
function closeDuringReadOf(value:boolean|'preflight'){return value==='preflight'?'stops during delivery preflight':value?'stops after shutdown':'reports unavailable honestly';}


test('person correction cannot borrow quoted replacement, a stale source revision or a grouped owner turn', async () => {
 const folder=mkdtempSync(join(tmpdir(),'sotto-person-authority-')),f=fixture(folder);
 const execution=createOwnerTaskExecution({client:f.client,journal:()=>f.journal,sourceAuthority:f.sourceAuthority});
 const target={operation:'fact_correct' as const,canonical_id:'c_123abc',fact_id:'fact_1',fact_version:'a'.repeat(64),source_revision:authority.revision,text:'Dana is the COO.'};
 let checked=false;
 const coordinator=new ConversationCoordinator({journal:f.journal,ownerPolicy,
  model:{generate:async request=>{
   const trusted={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerTask:true,sourceRevision:authority.revision,signal:request.signal};
   await assert.rejects(execution.execute(target,{...trusted,allowOwnerTask:false}));
   await assert.rejects(execution.execute({...target,source_revision:'e'.repeat(64)},trusted));
   await assert.rejects(execution.execute({...target,text:'Invented replacement'},trusted),/exact owner span/);
   assert.equal(f.calls.length,0);assert.equal(f.journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
   checked=true;return 'Please clarify the person and fact.';
  }},postPrepared:async()=>({acceptance:'accepted',messageId:'clarification'})});
 try{await coordinator.receive(inbound('person-authority',target.text),context);assert.equal(checked,true);}
 finally{await coordinator.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('fresh legacy identifier admission and reviewed borrowed span/stale source refuse before contact',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-identifier-refusal-')),f=fixture(folder);
 const execution=createOwnerTaskExecution({client:f.client,journal:()=>f.journal,sourceAuthority:f.sourceAuthority});
 let stale=false;
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerTaskTool:f.client,model:{generate:async request=>{
  await assert.rejects(execution.execute({operation:'add_identifier',canonical_id:'c_123abc',identifier:'borrowed@example.com',source_revision:stale?'a'.repeat(64):authority.revision},{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerTask:request.allowOwnerTask,sourceRevision:authority.revision,signal:request.signal}),/accepted review/);
  assert.throws(()=>f.journal.prepareOwnerTaskToolAction({runId:request.runId,channel:'photon',accountId:'account',ownerId:'owner',lineKey:'line',scopeHash:f.client.scopeHash},request.ownerRequestKey!,{operation:'add_identifier',canonical_id:'c_123abc',identifier:'borrowed@example.com',source_revision:authority.revision}),/accepted review/);
  await assert.rejects(execution.execute({operation:'propose_identifier',canonical_id:'c_123abc',identifier:'borrowed@example.com',source_revision:stale?'a'.repeat(64):authority.revision},{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerTask:request.allowOwnerTask,sourceRevision:authority.revision,signal:request.signal}),stale?/Source authority/:/exact owner span/);
  return {text:'Please provide the exact identifier.'};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'safe'})});
 try{await c.receive(inbound('missing','Dana has another email address'),context);stale=true;await c.receive(inbound('stale','Dana uses borrowed@example.com'),context);assert.equal(f.calls.length,0);assert.equal(f.journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);}finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('task add refuses invented contact and identifier before action or receiver contact',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'task-owner-spans-')),f=fixture(folder);
 try{
 for(const [index,detail] of [{contact:'Invented'}, {identifier:'invented@example.com'}].entries()){
 const coordinator=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerTaskTool:f.client,
 model:{generate:async request=>{await assert.rejects(()=>f.callTool('owner_task_add',{text:args.text,direction:'them',...detail},request),/exact owner span/);return 'Please clarify';}},
 postPrepared:async()=>({acceptance:'accepted',messageId:`reply-${index}`})});
 await coordinator.receive(inbound(`span-${index}`,`Dana owes me ${args.text}`),context);await coordinator.close();
 }
 assert.equal(f.calls.length,0);assert.equal(f.journal.db.prepare("SELECT COUNT(*) n FROM actions WHERE kind='owner_loop_tool'").get()!.n,0);
 }finally{f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

import {resolveManyOwnerTextMatches,namedLoopDescriptionMatches,namedLoopSelection} from '../src/tools/owner-task-tool-contract.js';
test('possessive completion phrases retain exact asks and reject contradictions or mismatched counts',()=>{
 const items=['Tina’s offer check-in','Shimolee’s family photo plans'].map(name=>({name,correction:name}));
 assert.equal(resolveManyOwnerTextMatches(items,items.map(item=>item.name).join(' and ')+' are both done. Close those two.','named_people'),true);
 for(const ending of ['are not done. Close those two.','are both done. Close those three.','might be done. Close those two.','are both done. Leave those two open.'])
  assert.equal(resolveManyOwnerTextMatches(items,items.map(item=>item.name).join(' and ')+' '+ending,'named_people'),false,ending);
 assert.deepEqual(namedLoopSelection(items[0]!.name),{person:'tina',words:['offer','check','in']});
 assert.equal(namedLoopDescriptionMatches(items[0]!.name,'Discuss her offer','Schedule the check-in'),true);
 assert.equal(namedLoopDescriptionMatches(items[1]!.name,'Choose dates for family photos'),true);
 assert.equal(namedLoopDescriptionMatches('Tina’s school application','Discuss her offer','Schedule the check-in'),false);
 assert.equal(namedLoopDescriptionMatches('Tina’s plans','Discuss her offer'),false);
 assert.equal(resolveManyOwnerTextMatches(items,'Those two are done','named_people'),false);
});


test('a completion question stays a question before an open-task readback', () => {
 const items=[{name:'alpha',correction:'alpha'},{name:'beta',correction:'beta'}];
 for(const suffix of ['any Sotto pilot test tasks left?', 'what else is open?']) {
  assert.equal(resolveManyOwnerTextMatches(items,`alpha and beta done? ${suffix}`,'owner_tasks'),false);
  assert.equal(resolveManyOwnerTextMatches(items,`alpha and beta are not done. ${suffix}`,'owner_tasks'),false);
  assert.equal(resolveManyOwnerTextMatches(items,`alpha and beta are done. ${suffix}`,'owner_tasks'),true);
 }
});
