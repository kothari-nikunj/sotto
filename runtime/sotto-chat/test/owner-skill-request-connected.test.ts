import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync} from 'node:fs';
import {once} from 'node:events';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
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
      SOTTO_TEST_MEETING_PREP: '1', SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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
const sourceAuthority = {read: async () => ({version: 2 as const, revision: 'a'.repeat(64), complete: true, bridge_sources: [], google_sources: ['calendar' as const]})};

for (const skill of ['meeting-prep', 'ask-research'] as const) test(`native ${skill} skill queues actual Python work; lost admission reopens and inspects without another job`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-shared-skill-http-'));
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  let runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
  let coordinator: ConversationCoordinator | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver'));
    let lose = false, blocked = false, admissions = 0;
    const client = createOwnerSkillRequestClient({...receiver, fetch: async (input, init) => {
      const body = JSON.parse(init!.body as string);
      if (body.operation === 'inspect' && blocked) throw new Error('inspect unavailable');
      const response = await fetch(input, init);
      if (body.operation === 'admit') {
        admissions++; assert.equal(response.status, 200);
        if (lose) {await response.body?.cancel(); throw new Error('lost committed response');}
      }
      return response;
    }});
    const command = createSkillCommand(createOwnerSkillExecution({client, journal: () => journal, sourceAuthority}));
    const tool = createRunScriptTool({mode: 'chat', executeCommand: command,
      commandContext: (ctx, signal) => ({mode: ctx?.get('mode'), runId: ctx?.get('runId'), ownerRequestKey: ctx?.get('ownerRequestKey'), allowOwnerSkill: ctx?.get('allowOwnerSkill'), sourceRevision: ctx?.get('sourceRevision'), signal})});
    let step = 0;
    const calls = [{name: 'skill', args: {name: skill === 'ask-research' ? 'sotto-ask' : 'sotto-meeting-prep'}},
      {name: 'run_sotto_script', args: {id: skill, args: skill === 'ask-research' ? ['--person', 'Dana'] : []}}, null,
      {name: 'run_sotto_script', args: {id: skill, args: [skill === 'ask-research' ? '--person' : '--focus', 'Spencer Kim']}}, null];
    const model = new MockLanguageModelV4({doGenerate: async request => {
      if (step === 1) assert.ok(JSON.stringify(request.prompt).includes(skill === 'ask-research' ? 'Answer questions over' : 'Two modes, one pipeline'));
      const call = calls[step++];
      return {content: call ? [{type: 'tool-call' as const, toolCallId: `step-${step}`, toolName: call.name, input: JSON.stringify(call.args)}] : [{type: 'text' as const, text: 'invented completed prep'}], finishReason: {unified: call ? 'tool-calls' as const : 'stop' as const, raw: 'STOP'}, usage, warnings: []};
    }});
    runtime = createSottoAgentRuntime({model, storagePath: join(folder, 'memory.db'), instructions: SHARED_SKILL_EXECUTION_GUIDE,
      skillPaths: (await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry => entry.path), sourceAuthority, chatTools: () => ({run_sotto_script: tool})});
    const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id};
    const context = {threadId: receiver.identity.route_id, destination: 'photon', lineKey: 'line'};
    const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: ownerPolicy.ownerId, isDirect: true, eventId, text});
    const sent: string[] = [];
    coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerSkillRequest: client,
      model: {generate: request => runtime!.generateChat({...request, threadId: 'memory', ownerId: 'owner'})},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    assert.equal((await coordinator.receive(inbound('sweep', skill === 'ask-research' ? 'Research Dana' : 'Prep my meetings'), context)).status, 'accepted');
    assert.match(sent[0]!, /queued/); assert.ok(!sent[0]!.includes('invented'));
    lose = true; blocked = true;
    assert.equal((await coordinator.receive(inbound('focus', skill === 'ask-research' ? 'Research Spencer Kim' : 'Prep Spencer Kim'), context)).status, 'held');
    assert.match(sent[1]!, /I couldn’t confirm whether the skill request/);
    const before = journal.db.prepare('SELECT * FROM actions WHERE kind=? ORDER BY created_at').all(ownerSkillRequestKind);
    assert.deepEqual(before.map(row => row.status), ['succeeded', 'unknown']);
    await coordinator.close(); await runtime.close(); runtime = undefined;
    await stopReceiver(receiver.child); receiver = await startReceiver(join(folder, 'receiver'));
    journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();
    coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerSkillRequest: client,
      model: {generate: async request => {assert.equal(request.text, 'Hello safely'); return 'safe answer';}},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    // Reopened receiver uses a new loopback port; retain only the same durable identity/proofs.
    const reopenedClient = createOwnerSkillRequestClient(receiver);
    client.call = async (operation, input, signal) => {
      if (blocked && operation === 'inspect') throw new Error('inspect unavailable');
      assert.equal(operation, 'inspect'); return reopenedClient.call(operation, input, signal);
    };
    assert.equal((await coordinator.recover())[0]?.status, 'held');
    assert.equal((await coordinator.receive(inbound('safe', 'Hello safely'), context)).status, 'accepted');
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_skill_request' AND status='unknown'").run();
    blocked = false;
    assert.equal((await coordinator.recover())[0]?.status, 'accepted');
    assert.equal(admissions, 2);
    assert.equal((await coordinator.receive(inbound('focus', skill === 'ask-research' ? 'Research Spencer Kim' : 'Prep Spencer Kim'), context)).status, 'duplicate');
    const queue = new DatabaseSync(join(folder, 'receiver/events/work.sqlite3'), {readOnly: true});
    try {
      const jobs = queue.prepare('SELECT id,admission,payload FROM jobs WHERE admission IS NOT NULL').all().filter(row => JSON.parse(row.payload as string).label.startsWith('skill:'));
      assert.equal(jobs.length, 2);
      const actions = journal.db.prepare('SELECT * FROM actions WHERE kind=?').all(ownerSkillRequestKind);
      for (const action of actions) {
        const saved = JSON.parse(action.result_json as string);
        const job = jobs.find(row => row.id === saved.receipt.outcome.request_id)!;
        const payload = JSON.parse(job.payload as string);
        assert.equal(payload.label, `skill:${action.source_event_id}:${skill}`);
        assert.equal(JSON.parse(payload.prompt).work_key, `skill:${action.source_event_id}`);
        const admission = JSON.parse(job.admission as string);
        assert.deepEqual(admission.receipt, saved.receipt);
      }
    } finally {queue.close();}
    assert.equal(sent.length, 4);
    assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether')).length,1);
  } finally {await coordinator?.close(); await runtime?.close(); journal.close(); if (receiver) await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});}
});

test('native forced proactive greeting refusal continues model to an ordinary reply without admission',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'native-proactive-refusal-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));let contacts=0,callbacks=0,step=0;
 const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:'a'.repeat(64)};
 const client=createOwnerSkillRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async()=>{contacts++;throw Error('must not contact');}});
 const command=createSkillCommand(createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority}));
 const tool=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerSkill:ctx?.get('allowOwnerSkill'),sourceRevision:ctx?.get('sourceRevision'),signal})});
 const model=new MockLanguageModelV4({doGenerate:async request=>{
  const call=step++===0;
  if(!call)assert.match(JSON.stringify(request.prompt),/needs_context/);
  return {content:call?[{type:'tool-call' as const,toolCallId:'forced-proactive',toolName:'run_sotto_script',input:JSON.stringify({id:'proactive',args:[]})}]:[{type:'text' as const,text:'Hello! How can I help?'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
 }});
 const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:SHARED_SKILL_EXECUTION_GUIDE,skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,chatTools:()=>({run_sotto_script:tool})});
 const sent:string[]=[];
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerSkillRequest:client,onOwnerSilentWorkAccepted:()=>{callbacks++;},model:{generate:request=>runtime.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'hello-reply'};}});
 try {assert.equal((await coordinator.receive({eventId:'hello',channel:'photon',accountId:'account',senderId:'owner',isDirect:true,text:'Hello, Sotto.'},{threadId:'thread',destination:'photon',lineKey:'line'})).status,'accepted');assert.deepEqual(sent,['Hello! How can I help?']);assert.equal(contacts,0);assert.equal(callbacks,0);assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);assert.equal(step,2);}
 finally {await coordinator.close();await runtime.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});


test('contextual prep selection reaches the actual receiver without replacing it with a pronoun', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-prep-literal-connected-'));
  const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver'));
    const client = createOwnerSkillRequestClient(receiver);
    const execution = createOwnerSkillExecution({client, journal: () => journal, sourceAuthority});
    for (const [i, text] of ['prep me for that pls', 'full prep on Tanya'].entries()) {
      const selector = 'Tanya Reed', runId = 'context-' + i;
      const inbound = {channel: 'photon', accountId: 'account', senderId: receiver.identity.owner_id, isDirect: true, eventId: runId, text};
      const context = {threadId: receiver.identity.route_id, destination: 'photon', lineKey: 'line'};
      const eventId = journal.admitInbound({eventId: runId, accountKey: 'account', lineKey: 'line', threadKey: context.threadId,
        channel: 'photon', sender: receiver.identity.owner_id, payload: JSON.stringify({inbound, context})}).event.id as string;
      journal.startRun({runId, eventId});
      const result = await execution.change({skill: 'meeting-prep', selection: {mode: 'focus', focus: selector}},
        {mode: 'chat', runId, ownerRequestKey: eventId, allowOwnerSkill: true, sourceRevision: 'a'.repeat(64)});
      assert.equal(result.status, 'queued');
      journal.completeRun(runId);
    }
    const queue = new DatabaseSync(join(folder, 'receiver/events/work.sqlite3'), {readOnly: true});
    try {
      const jobs = queue.prepare('SELECT payload FROM jobs WHERE admission IS NOT NULL').all();
      assert.equal(jobs.length, 2);
      for (const job of jobs) assert.deepEqual(JSON.parse(JSON.parse(job.payload as string).prompt).selection,
        {mode: 'focus', focus: 'Tanya Reed'});
    } finally {queue.close();}
  } finally {journal.close(); if (receiver) await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});}
});

test('a homepage answer binds the accepted owner request through the real Python receiver',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'research-clarify-http-'));
 const journal=new OperationalJournal(join(folder,'operations.sqlite'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,coordinator:ConversationCoordinator|undefined;
 try{
  receiver=await startReceiver(join(folder,'receiver'));
  const client=createOwnerSkillRequestClient(receiver);
  const execution=createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority});
  const context={threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'};
  const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:receiver.identity.owner_id};
  const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:ownerPolicy.ownerId,isDirect:true,eventId,text});
  coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerSkillRequest:client,model:{generate:async request=>{
   if(request.text.startsWith('Queue'))return {text:'Which website?',sourceRevision:'a'.repeat(64)};
   await execution.change({skill:'ask-research',selection:{mode:'company',company:'spurtest.example'}},
    {mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:'a'.repeat(64)});
   return {text:'Queued',sourceRevision:'a'.repeat(64)};
  }},postPrepared:async()=>({acceptance:'accepted',messageId:'clarification'})});
  await coordinator.receive(inbound('request','Queue research for Spur?'),context);
  await coordinator.receive(inbound('answer','https://spurtest.example/'),context);
  const queue=new DatabaseSync(join(folder,'receiver/events/work.sqlite3'),{readOnly:true});
  try{
   const jobs=queue.prepare('SELECT payload FROM jobs WHERE admission IS NOT NULL').all();
   assert.equal(jobs.length,1);
   const question=JSON.parse(JSON.parse(jobs[0]!.payload as string).prompt);
   assert.equal(question.selection.company,'spurtest.example');
   assert.match(question.question,/Queue research for Spur/);
   assert.match(question.question,/Owner-supplied lookup identity: spurtest.example/);
  }finally{queue.close();}
 }finally{await coordinator?.close();journal.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
