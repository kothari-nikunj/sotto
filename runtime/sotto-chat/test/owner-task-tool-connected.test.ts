import {startConfiguredRuntime} from '../src/bootstrap/main.js';
import {DatabaseSync} from 'node:sqlite';
import {Message} from 'chat';
import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, readdirSync, renameSync, rmSync, mkdirSync, writeFileSync } from 'node:fs';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { MockLanguageModelV4 } from 'ai/test';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator } from '../src/conversation.js';
import { PhotonOwnerFeedback } from '../src/channels/presence.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createOwnerTaskToolClient } from '../src/tools/owner-task-tool-client.js';
import { createOwnerLoopClient } from '../src/tools/owner-loop-client.js';
import { createOwnerTaskTools } from '../src/tools/owner-task-tool.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { createHmac } from 'node:crypto';
import { RequestContext } from '@mastra/core/request-context';
import { createOwnerLoopsTool, readOwnerLoops } from '../src/agent/owner-loops.js';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';

async function startReceiver(folder: string, multi = false, legacyDates = false, samePersonTasks = false, taskPeer = '') {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_KNOWLEDGE_READ:'1', SOTTO_TEST_TASK_PEER:taskPeer, SOTTO_TEST_SAME_PERSON_TASKS:samePersonTasks?'1':undefined, SOTTO_TEST_LOOP_RESOLVE_MANY:multi?'1':undefined, SOTTO_TEST_LOOP_LEGACY_DATES:legacyDates?'1':undefined, SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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

function unrelatedLedgerBytes(folder:string) {
  const directory=join(folder,'receiver/knowledge/continuity');
  const row=readdirSync(directory).filter(name=>name.endsWith('.md')).map(name=>readFileSync(join(directory,name),'utf8')).find(text=>text.includes('resolve-many-4'));
  assert.ok(row);return row;
}

async function runThreePersonClosure(message:string,asksRemaining:boolean,incomplete:false|'unknown'|'refused'|'rejected'|'lost'=false,legacyDates=false) {
  const folder=mkdtempSync(join(tmpdir(),'sotto-multi-resolve-connected-'));
  const journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
  let coordinator:ConversationCoordinator|undefined;
  let feedback:PhotonOwnerFeedback|undefined;
  try {
    receiver=await startReceiver(join(folder,'receiver'),true,legacyDates);
    const authority={read:async()=>({version:2 as const,revision:'a'.repeat(64),complete:true,bridge_sources:[],google_sources:['gmail' as const]})};
    const readBearer=createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex');
    let applies=0, blockInspect=true;
    const client=createOwnerTaskToolClient({...receiver,readBearer,sourceAuthority:authority,fetch:async(input,init)=>{
      const body=JSON.parse(String(init?.body??'{}'));
      if(incomplete==='lost'&&body.operation==='inspect'&&blockInspect)return new Response('{}',{status:503});
      let withheld:string|undefined;
      if(incomplete==='rejected'&&body.operation==='apply'&&body.args?.operation==='resolve_many'){
        // Make one selected target temporarily unavailable at the real writer.
        // Restore the fixture after its refusal; independent selections may still close.
        const directory=join(folder,'receiver/knowledge/continuity');
        withheld=readdirSync(directory).filter(name=>name.endsWith('.md')).map(name=>join(directory,name))
          .find(path=>readFileSync(path,'utf8').includes('resolve-many-1'));
        assert.ok(withheld);renameSync(withheld,join(folder,'withheld.md'));
      }
      let response:Response;
      try{response=await fetch(input,init);}
      finally{if(withheld)renameSync(join(folder,'withheld.md'),withheld);}
      if(body.operation==='apply'&&body.args?.operation==='resolve_many'){
        applies++;
        if(incomplete==='lost'){await response.body?.cancel();throw new Error('lost committed aggregate receipt');}
        if(incomplete==='unknown'||incomplete==='refused'){
          // The real receiver/writer runs; inject only uncertainty in its transport receipt.
          const receipt=await response.json();
          receipt.outcome='partial';
          receipt.items=receipt.items.map((item:any,index:number)=>index===0?item:
            {index:item.index,action:item.action,anchor_key:item.anchor_key,status:index===1?incomplete:'skipped',
              ...(index===1&&incomplete==='refused'?{reason:'target_changed'}:{})});
          return new Response(JSON.stringify(receipt),{status:response.status});
        }
      }
      return response;
    }});
    const prior=await client.readLoops('a'.repeat(64));
    const current=[...prior.you_owe,...prior.waiting_on_them];
    const unrelated=current.find(row=>row.anchor_key==='resolve-many-4')!;
    const unrelatedBytes=unrelatedLedgerBytes(folder);
    if(legacyDates){assert.equal(unrelated.deadline,null);assert.equal(unrelated.overdue,false);assert.equal(unrelated.chase_pending,null);assert.equal(unrelated.last_chased_at,null);assert.equal(unrelated.chase_after,null);assert.equal(unrelated.handoff_asked_at,null);}
    const names=['Tina','shimolee','Hari'];
    const items=names.map(name=>{
      const matches=current.filter(row=>row.name.toLowerCase().split(' ')[0]===name.toLowerCase());
      assert.equal(matches.length,1,JSON.stringify(current.map(row=>row.name)));const row=matches[0]!;
      return {name,correction:name,anchor_key:row.anchor_key,loop_version:row.loop_version!,loop_identity:row.loop_identity!};
    });
    let calls=0;
    const model=new MockLanguageModelV4({doGenerate:async()=>{
      const call=calls++===0;
      return {content:call?[{type:'tool-call' as const,toolCallId:'multi-resolve',toolName:'owner_task_resolve_many',
        input:JSON.stringify({selection_mode:'named_people',items})}]:[{type:'text' as const,text:calls>1?'Hello safely.':'Model closure claim'}],
        finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Use exact current loop proofs.',sourceAuthority:authority,
      chatTools:()=>createOwnerTaskTools({client,journal:()=>journal,sourceAuthority:authority})});
    const sent:string[]=[];
    const reactions:string[]=[];
    feedback=new PhotonOwnerFeedback({markRead:async()=>{},addReaction:async(_thread,_event,emoji)=>{reactions.push(emoji);}});
    coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},ownerTaskTool:client,
      onOwnerAdmission:feedback.admitted.bind(feedback),onOwnerReplyAccepted:feedback.accepted.bind(feedback),
      model:{generate:request=>runtime!.generateChat({...request,threadId:'multi-memory',ownerId:'owner'})},
      postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'multi-accepted'};}});
    const inbound={channel:'photon' as const,accountId:'account',senderId:receiver.identity.owner_id,isDirect:true,eventId:'multi-source',text:message};
    const context={threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'};
    const result=await coordinator.receive(inbound,context);
    if(incomplete==='lost'){
      assert.equal(result.status,'held');
      assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_loop_tool'").get()!.status,'unknown');
      const beforeRecovery=calls;
      blockInspect=false;
      journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_loop_tool' AND status='unknown'").run();
      await coordinator.recover();
      assert.equal(applies,1,'receipt recovery must not reapply the aggregate');
      assert.equal(calls,beforeRecovery,'receipt recovery must not start a model turn');
      assert.ok(sent.some(text=>text.includes('Tina: Marked done')));
      assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_loop_tool'").get()!.status,'succeeded');
      return;
    }
    assert.equal(result.status,'accepted');
    assert.equal(calls,1,'durable aggregate outcomes must not buy a discarded model continuation');
    const after=await client.readLoops('a'.repeat(64));
    if(incomplete==='rejected'){
      assert.deepEqual(after.you_owe.map(row=>row.anchor_key),prior.you_owe.filter(row=>['resolve-many-1','resolve-many-4'].includes(row.anchor_key)).map(row=>row.anchor_key));
      assert.match(sent[0]!,/Tina: I couldn’t tell which one you meant/);
      assert.match(sent[0]!,/shimolee: Marked done/);assert.match(sent[0]!,/Hari: Marked done/);
      assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_loop_tool'").get()!.status,'succeeded');
    }else {
      assert.equal(after.you_owe.length+after.waiting_on_them.length,1);
      assert.equal([...after.you_owe,...after.waiting_on_them][0]!.name,'Other Person');
      assert.deepEqual([...after.you_owe,...after.waiting_on_them][0],unrelated,'unrelated task and raw-row proofs stay unchanged across apply/readback');
      assert.equal(unrelatedLedgerBytes(folder),unrelatedBytes);
      assert.match(sent[0]!,/Tina: Marked done/);
      if(incomplete){
        if(incomplete==='unknown')assert.match(sent[0]!,/shimolee: I couldn’t confirm whether this was marked done/);
        else assert.match(sent[0]!,/shimolee: This one changed since I checked/);
        assert.match(sent[0]!,/Hari: I haven’t changed this one/);
        assert.doesNotMatch(sent[0]!,/shimolee: Marked done|Hari: Marked done/);
      }else {assert.match(sent[0]!,/shimolee: Marked done/);assert.match(sent[0]!,/Hari: Marked done/);}
      if(asksRemaining)assert.match(sent[0]!,/Still open:\n• Other Person: /);
      else assert.doesNotMatch(sent[0]!,/Still open:|currently available list|Other Person/);
      assert.doesNotMatch(sent[0]!,/Model closure claim/);
      const actions=journal.db.prepare("SELECT result_json FROM actions WHERE kind='owner_loop_tool'").all();
      assert.equal(actions.length,1);
      const saved=JSON.parse(String(actions[0]!.result_json));
      assert.equal(saved.receipt.operation,'resolve_many');
      assert.deepEqual(saved.receipt.items.map((item:{status:string})=>item.status),incomplete?['applied',incomplete,'skipped']:['applied','applied','applied']);
    }
    await new Promise<void>(resolve=>setImmediate(resolve));
    assert.equal(reactions.includes('✅'),!incomplete,'incomplete task outcomes must not receive a completion tapback');
    if(incomplete){
      const event=journal.db.prepare("SELECT id,outcome FROM inbound_events WHERE source_id='multi-source'").get()!;
      assert.equal(event.outcome,'resolved','the honest acknowledgement preserves the original event lifecycle');
      assert.equal(journal.getReplyForEvent(String(event.id))?.purpose,null);
      const modelCalls=calls;
      await coordinator.recover();
      assert.equal(sent.length,1,'accepted pending acknowledgement is not resent');
      assert.equal(applies,1,'recovery must not reapply the aggregate');
      assert.equal(calls,modelCalls,'recovery must not create another model turn');
      assert.equal(reactions.includes('✅'),false);
      assert.equal((await coordinator.receive({...inbound,eventId:'safe-source',text:'Hello'},context)).status,'accepted');
      assert.equal(sent.at(-1),'Hello safely.','uncertain item does not block unrelated safe chat');
      assert.equal(applies,1);
      await new Promise<void>(resolve=>setImmediate(resolve));
      assert.equal(reactions.filter(emoji=>emoji==='✅').length,1,'only the later safe chat receives completion feedback');
    }
    assert.equal((await coordinator.receive(inbound,context)).status,'duplicate');
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_loop_tool'").get()!.n,1);
  } finally {await coordinator?.close();await feedback?.close();await runtime?.close();journal.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
}

test('one explicit three-person closure uses one admitted action and three exact per-item receipts',
  () => runThreePersonClosure('Tina shimolee Hari loops are closed. What else is open?',true));
test('a simple multi-loop completion acknowledges only those items after readback',
  () => runThreePersonClosure('Tina shimolee Hari loops are closed.',false));
test('lost aggregate receipt remains held and recovers without another mutation or model turn',
  () => runThreePersonClosure('Tina shimolee Hari loops are closed.',false,'lost'));
test('partial multi-loop receipt keeps honest reply without completion tapback or recovery reapply',
  () => runThreePersonClosure('Tina shimolee Hari loops are closed.',false,'unknown'));
test('one unavailable selection does not block clear peers or falsely receive a completion tapback',
  () => runThreePersonClosure('Tina shimolee Hari loops are closed.',false,'rejected'));
test('definite partial multi-loop refusal gets no completion tapback while later safe chat does',
  () => runThreePersonClosure('Tina shimolee Hari loops are closed.',false,'refused'));
test('real Python legacy dates do not block named-person aggregate preflight or post-write readback',
  () => runThreePersonClosure('Tina shimolee Hari loops are closed. What else is open?',true,false,true));

async function runOwnerTaskSelection(message:string,multiple:boolean,repair:'first-only'|'missing'|'repeat'|'terminal'|'parallel'='first-only',legacyDates=false,noRemainingOwnerTasks=false) {
  const folder=mkdtempSync(join(tmpdir(),'sotto-owner-selection-connected-'));
  const journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
  let coordinator:ConversationCoordinator|undefined;
  try {
    receiver=await startReceiver(join(folder,'receiver'),true,legacyDates);
    const authority={read:async()=>({version:2 as const,revision:'a'.repeat(64),complete:true,bridge_sources:[],google_sources:['gmail' as const]})};
    const readBearer=createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex');
    const writes:string[]=[];
    const client=createOwnerTaskToolClient({...receiver,readBearer,sourceAuthority:authority,fetch:async(input,init)=>{
      if(init?.body){const body=JSON.parse(String(init.body));if(body.operation==='apply')writes.push(body.args.operation);}
      return fetch(input,init);
    }});
    const titles=['Review alpha checklist','Review beta checklist',...(noRemainingOwnerTasks?[]:['Review gamma checklist'])];
    for(const [index,title] of titles.entries()){
      const eventId=String(index+1).repeat(64),args={operation:'add' as const,text:title};
      const binding=stableActionIdentity(eventId,'owner_loop_tool',args);
      assert.equal((await client.call('apply',{actionId:binding.actionId,eventId,payloadHash:'b'.repeat(64),text:`Add ${title}`,
        admittedAtMs:Date.now(),providerThreadId:receiver.identity.route_id,args})).status,'committed');
    }
    writes.length=0;
    const before=await client.readLoops('a'.repeat(64));
    const unrelated=before.you_owe.find(row=>row.anchor_key==='resolve-many-4')!;
    const unrelatedBytes=unrelatedLedgerBytes(folder);
    const selection=message.startsWith('gamma')?'gamma':'alpha';
    const selected=[selection,...(multiple?['beta']:[])].map(name=>{
      const row=before.you_owe.find(row=>row.what===`Review ${name} checklist`)!;
      assert.ok(row);return {name,correction:name,anchor_key:row.anchor_key,loop_version:row.loop_version!,loop_identity:row.loop_identity!};
    });
    let step=0;
    const model=new MockLanguageModelV4({doGenerate:async()=>{
      step++;
      // One rejected single selection can repair to aggregate; terminal/repeated refusal cannot loop.
      if(multiple&&step<=2)assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_loop_tool'").get()!.n,0);
      const call=multiple&&(step===1&&repair!=='terminal'||step===2&&repair==='repeat')
        ?{name:'owner_task_close',args:{anchor:selected[0]!.anchor_key,...(repair==='missing'?{}:{correction:'alpha'})}}:
        step===(multiple&&repair!=='terminal'?2:1)?multiple?{name:'owner_task_resolve_many',args:{selection_mode:'owner_tasks',items:selected}}:
          {name:'owner_task_close',args:{anchor:selected[0]!.anchor_key,correction:selection}}:null;
      const toolCalls=call?[{type:'tool-call' as const,toolCallId:`selection-${step}`,toolName:call.name,input:JSON.stringify(call.args)}]:[];
      if(repair==='parallel'&&step===1){
        const items=['alpha','beta'].map(name=>{const row=before.you_owe.find(row=>row.what===`Review ${name} checklist`)!;return {name,correction:name,anchor_key:row.anchor_key,loop_version:row.loop_version!,loop_identity:row.loop_identity!};});
        toolCalls.push({type:'tool-call',toolCallId:'parallel-invalid-selection',toolName:'owner_task_resolve_many',input:JSON.stringify({selection_mode:'owner_tasks',items})});
      }
      return {content:call?toolCalls:
        [{type:'text' as const,text:repair==='repeat'||repair==='terminal'?'Hello.':'all tasks completed'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Use task tools.',sourceAuthority:authority,
      chatTools:()=>createOwnerTaskTools({client,journal:()=>journal,sourceAuthority:authority})});
    const sent:string[]=[];
    coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},ownerTaskTool:client,
      model:{generate:request=>runtime!.generateChat({...request,threadId:'selection-memory',ownerId:'owner'})},
      postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'selection-ack'};}});
    const inbound={channel:'photon' as const,accountId:'account',senderId:receiver.identity.owner_id,isDirect:true,eventId:'selection',text:message};
    assert.equal((await coordinator.receive(inbound,{threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'})).status,'accepted');
    if(repair==='repeat'||repair==='terminal'){
      assert.equal(step,repair==='terminal'?1:2,'unrepairable/repeated selection stops before another model call');
      assert.deepEqual(writes,[]);
      assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_loop_tool'").get()!.n,0);
      const after=await client.readLoops('a'.repeat(64));
      assert.deepEqual(after.you_owe.filter(row=>row.channel==='manual').map(row=>row.what).sort(),titles);
      assert.equal(sent.length,1);assert.match(sent[0]!,/fresh numbered list/);assert.doesNotMatch(sent[0]!,/all tasks completed/);
      const beforeSafe=step;
      assert.equal((await coordinator.receive({...inbound,eventId:'safe-next',text:'Hello'},{threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'})).status,'accepted');
      assert.equal(step,beforeSafe+1);assert.equal(sent[1],'Hello.','clarification state does not leak into a fresh turn');
      return;
    }
    assert.equal(step,repair==='parallel'||!multiple?1:2,'settled receipts stop; aggregate repair retains its one repair step');
    if(multiple)assert.match(JSON.stringify(model.doGenerateCalls[1]),/needs_selection/);
    assert.deepEqual(writes,[multiple?'resolve_many':'close']);
    const after=await client.readLoops('a'.repeat(64));
    assert.deepEqual(after.you_owe.find(row=>row.anchor_key==='resolve-many-4'),unrelated);
    assert.equal(unrelatedLedgerBytes(folder),unrelatedBytes);
    const remaining=after.you_owe.filter(row=>row.channel==='manual').map(row=>row.what).sort();
    assert.deepEqual(remaining,multiple?titles.slice(2):selection==='gamma'?[titles[0],titles[1]]:[titles[1],titles[2]]);
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_loop_tool'").get()!.n,1);
    if(multiple){assert.match(sent[0]!,/alpha: Marked done/);assert.match(sent[0]!,/beta: Marked done/);}
    if(message.endsWith('?')){
      assert.match(sent[0]!,/Still open:|Here’s the full remaining list:/);
      if(!noRemainingOwnerTasks)assert.match(sent[0]!,/Review gamma checklist/);
      assert.match(sent[0]!,/Other Person/,'qualified readbacks must retain unrelated source obligations');
      assert.doesNotMatch(sent[0]!,/No other tasks you added|Nothing else is open/);
    }
    if(!multiple)assert.equal(sent[0],'Marked that task complete.');
    assert.doesNotMatch(sent[0]!,/all tasks completed|gamma: Marked done/);
  }finally{await coordinator?.close();await runtime?.close();journal.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
}

test('same-step clarification cannot replace a successful canonical task receipt',()=>
  runOwnerTaskSelection('alpha done',false,'parallel'));
test('missing single selection gets one aggregate repair opportunity',()=>
  runOwnerTaskSelection('alpha and beta done, gamma still open',true,'missing'));
test('repeated single selection ends with honest clarification and no writes',()=>
  runOwnerTaskSelection('alpha and beta done, gamma still open',true,'repeat'));
test('unrepairable aggregate clause ends after one model call with no writes',()=>
  runOwnerTaskSelection('alpha and beta might be done',true,'terminal'));
for(const suffix of ['any test tasks left?','any Sotto pilot test tasks left?','any onboarding tasks left?','any urgent tasks left?','any email tasks left?','any of my tasks left?','anything else open?','what else?'])
  test(`natural readback suffix preserves aggregate repair: ${suffix}`,()=>
    runOwnerTaskSelection(`alpha and beta are done. ${suffix}`,true));

test('wrong single-close calls repair to one aggregate on the same original owner event without losing Beta',()=>
  runOwnerTaskSelection('alpha and beta done, gamma still open',true));
test('real Python unrelated legacy dates leave owner aggregate and remaining-task readback usable',()=>
  runOwnerTaskSelection('alpha and beta are done. any test tasks left?',true,'first-only',true));
test('qualified readback after completing added tasks still discloses source obligations',()=>
  runOwnerTaskSelection('alpha and beta are done. any test tasks left?',true,'first-only',false,true));
test('an explicit global open-list inquiry retains source obligations after owner-task completion',()=>
  runOwnerTaskSelection('alpha and beta are done. what else is open?',true));
for(const message of ['alpha done','alpha done, beta still open','alpha done beta still open','gamma is done too','gamma is done now'])
  test(`single close accounts for the full owner selection: ${message}`,()=>runOwnerTaskSelection(message,false));

test('real Mastra task add, authenticated owner list and exact close -> receiver canonical ledger -> durable receipt ACK; literals remain compatible', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-task-connected-'));
  const journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined, runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver'));
    const literal = createOwnerLoopClient(receiver);
    const readOptions = {...receiver, readBearer: createHmac('sha256', '0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex')};
    const client = createOwnerTaskToolClient(readOptions);
    const tools = {...createOwnerTaskTools({client, journal: () => journal, sourceAuthority}), owner_open_loops: createOwnerLoopsTool(readOptions)};
    let step = 0, anchor = ''; const text = 'Review the cedar packet 🌲';
    const model = new MockLanguageModelV4({doGenerate: async () => {
      step++;
      const call = step === 1 ? {name: 'owner_task_add', args: {text}} : step === 2 ? {name: 'owner_open_loops', args: {include_parked: false}} :
        step === 3 ? {name: 'owner_task_close', args: {anchor,correction:'the cedar packet'}} : null;
      return {content: call ? [{type: 'tool-call' as const, toolCallId: `call-${step}`, toolName: call.name, input: JSON.stringify(call.args)}]
        : [{type: 'text' as const, text: 'model completion'}], finishReason: {unified: call ? 'tool-calls' : 'stop', raw: 'STOP'}, usage, warnings: []};
    }});
    runtime = createSottoAgentRuntime({model, storagePath: join(folder, 'memory.db'), instructions: 'Use task tools', sourceAuthority, chatTools: async () => tools});
    const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id};
    const context = {threadId: receiver.identity.route_id, destination: 'photon', lineKey: 'line'};
    const inbound = (eventId: string, value: string) => ({channel: 'photon' as const, accountId: 'account', senderId: ownerPolicy.ownerId, isDirect: true, eventId, text: value});
    const sent: string[] = [];
    const coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerTaskTool: client, ownerLoop: literal,
      model: {generate: request => runtime!.generateChat({...request, threadId: 'memory', ownerId: 'memory-owner'})},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    const sentence = `Add this task: ${text}`;
    assert.equal((await coordinator.receive(inbound('natural-add', sentence), context)).status, 'accepted');
    const projected = await readOwnerLoops(readOptions, {include_parked: false});
    assert.equal(projected.counts.you_owe, 1); assert.equal(projected.you_owe[0]!.what, text); anchor = projected.you_owe[0]!.anchor_key;
    assert.equal(sent[0],`Added: ${text}`);
    const files = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_loop_tool'").all(); assert.equal(files.length, 1);
    const stored = files[0]!, meta = JSON.parse(stored.result_json as string), event = journal.getInbound(stored.source_event_id as string)!;
    const original = {actionId: stored.id as string, eventId: stored.source_event_id as string, payloadHash: stored.source_payload_hash as string,
      text: sentence, admittedAtMs: event.received_at as number, providerThreadId: context.threadId, args: meta.args};
    const changed = {operation: 'add' as const, text: 'this task'};
    const changedIdentity = stableActionIdentity(original.eventId, 'owner_loop_tool', changed);
    assert.deepEqual(await client.call('apply', {...original, args: changed, actionId: changedIdentity.actionId}), {version: 1, status: 'rejected', code: 'event_conflict'});
    assert.equal((await coordinator.receive(inbound('natural-close', 'I finished the cedar packet.'), context)).status, 'accepted');
    assert.equal(step, 3); assert.equal(sent[1],'Marked that task complete.');
    assert.equal((await readOwnerLoops(readOptions, {include_parked: false})).counts.you_owe, 0);
    const inspected = await client.call('inspect', original); assert.equal(inspected.status, 'committed');
    if (inspected.status === 'committed' && inspected.operation !== 'resolve_many' && inspected.operation !== 'cleanup' && inspected.operation !== 'identifier' && inspected.operation !== 'relation' && inspected.operation !== 'fact_add' && inspected.operation !== 'fact_correct' && inspected.operation !== 'add_identifier') {assert.equal(inspected.anchor_key, anchor); assert.equal(inspected.loop_status, 'resolved');}
    const ledger = readFileSync(join(folder, 'receiver/knowledge/continuity', (await import('node:fs')).readdirSync(join(folder, 'receiver/knowledge/continuity')).find(name => name.endsWith('.md'))!), 'utf8');
    assert.ok(ledger.includes('sotto.owner-loop-tool.v1')); assert.ok(ledger.includes('owner_loop_tool_create') && ledger.includes('owner_loop_tool_close'));
    assert.equal((await coordinator.receive(inbound('natural-add', sentence), context)).status, 'duplicate');
    assert.equal((await coordinator.receive(inbound('natural-close', 'I finished the cedar packet.'), context)).status, 'duplicate'); assert.equal(step, 3);
    await coordinator.receive(inbound('literal-add', '/loop add Review the literal spruce packet'), context); assert.equal(step, 3);
    const literalProjection = await readOwnerLoops(readOptions, {include_parked: false});
    assert.equal(literalProjection.counts.you_owe, 1);
    await coordinator.receive(inbound('literal-close', `/loop close ${literalProjection.you_owe[0]!.anchor_key}`), context); assert.equal(step, 3);
    assert.equal((await readOwnerLoops(readOptions, {include_parked: false})).counts.you_owe, 0);
    await coordinator.close();
  } finally {await runtime?.close(); journal.close(); if (receiver) await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});}
});

test('actual receiver commits before lost response; reopen preserves exact args, safe chat, inspect-only recovery and no second effect', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-task-connected-loss-'));
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver')); let apply = 0, blocked = true; const inspected: unknown[] = [];
    const client = createOwnerTaskToolClient({...receiver, sourceAuthority,readBearer:createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex'), fetch: async (input, init) => {
      const body = JSON.parse(init!.body as string);
      if (body.operation === 'inspect') {inspected.push(body.args); if (blocked) return new Response('{}', {status: 503});}
      const result = await fetch(input, init);
      if (body.operation === 'apply') {apply++; assert.equal(result.status, 200); await result.body?.cancel(); throw new Error('lost committed receipt');}
      return result;
    }});
    const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id};
    const context = {threadId: receiver.identity.route_id, destination: 'photon', lineKey: 'line'};
    const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: ownerPolicy.ownerId, isDirect: true, eventId, text});
    const tools = createOwnerTaskTools({client, journal: () => journal, sourceAuthority});
    const sent: string[] = [];
    const first = new ConversationCoordinator({journal, ownerPolicy, ownerTaskTool: client,
      model: {generate: async request => {
        const requestContext = new RequestContext();
        for (const [key, value] of Object.entries({mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey,
          sourceRevision: 'a'.repeat(64), allowOwnerTask: request.allowOwnerTask})) requestContext.set(key, value);
        await tools.owner_task_add.execute!({text: 'Review the held cedar packet',direction:'them',contact:'Dana',identifier:'dana@example.com'}, {requestContext, abortSignal: request.signal} as never); return 'no';
      }}, postPrepared: async request => {assert.match(request.text, /^I couldn’t confirm whether the task change happened\./); sent.push(request.text); return {acceptance: 'accepted', messageId: 'original-task-notice'};}});
    const sentence = 'Dana dana@example.com owes me: Review the held cedar packet';
    assert.equal((await first.receive(inbound('held', sentence), context)).status, 'held');
    const action = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_loop_tool'").get()!; assert.equal(action.status, 'unknown');
    await first.close(); journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();
    const recovered = new ConversationCoordinator({journal, ownerPolicy, ownerTaskTool: client,
      model: {generate: async request => {assert.equal(request.text, 'Hello safely'); return 'safe answer';}},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    assert.equal((await recovered.recover())[0]?.status, 'held');
    assert.equal((await recovered.receive(inbound('safe', 'Hello safely'), context)).status, 'accepted');
    assert.equal(sent.at(-1), 'safe answer');
    assert.equal(sent.filter(text => text.startsWith('I couldn’t confirm whether the task change happened.')).length, 1);
    blocked = false;
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_loop_tool' AND status='unknown'").run();
    assert.equal((await recovered.recover())[0]?.status, 'accepted');
    assert.equal(apply, 1); assert.match(sent.at(-1)!, /1 waiting on others/);
    const listed=await readOwnerLoops({...receiver,readBearer:createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex')},{include_parked:false});assert.equal(listed.counts.you_owe,0);assert.equal(listed.waiting_on_them[0]?.what,'Review the held cedar packet');
    for (const args of inspected) assert.deepEqual(args, {operation: 'add', text: 'Review the held cedar packet',direction:'them',contact:'Dana',identifier:'dana@example.com'});
    assert.equal((await recovered.receive(inbound('held', sentence), context)).status, 'duplicate');
    await recovered.close();
  } finally {journal.close(); if (receiver) await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});}
});

test('configured default agent loads original Loops and common directed add reaches original writer',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'directed-task-default-'));let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined;const sent:string[]=[];const feedbackEffects:{id:string;effect:string}[]=[];let calls=0;
 try{receiver=await startReceiver(join(folder,'receiver'));const thread=receiver.identity.route_id;const media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const config={version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-urgent-handoff-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use the original skills and enabled commands.',skillPackRoot:process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver.bridgeUrl,readBearer:createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex')},ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}};
  const configPath=join(folder,'runtime.json');writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async(_thread:string,id:string)=>({id,threadId:thread,author:{isMe:false},raw:{read:async()=>{feedbackEffects.push({id,effect:'read'});},react:async(emoji:string)=>{feedbackEffects.push({id,effect:emoji});}}}),addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};

 const external:typeof fetch=async(input,init)=>{const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);if(url.origin!=='https://proxy.example'){assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin);return fetch(input,init);}calls++;assert.ok(calls<=3);const call=calls===1?{name:'skill',args:{name:'sotto-loops'}}:calls===2?{name:'run_sotto_script',args:{id:'knowledge-edit',args:['--op','loop-add','--text','Send the Cedar deck','--direction','them','--contact','Dana','--identifier','dana@example.com']}}:undefined;
 if(calls===2)assert.ok(JSON.stringify(JSON.parse(String(init?.body)).messages).includes('SKILL'));
 return new Response(JSON.stringify({id:'task-'+calls,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'tool-'+calls,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:'Done.'},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});};
 app=await startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['task-ack']};}}),openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
 const message=new Message({id:'directed-default',threadId:thread,text:'Dana dana@example.com owes me: Send the Cedar deck',formatted:{type:'root',children:[]},raw:{id:'directed-default',timestamp:new Date()},author:{userId:receiver.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
 await app.gateway.chat.processMessage(adapter as never,thread,message);const until=Date.now()+15000;while(sent.length===0){if(Date.now()>until)throw Error('directed default task timeout');await new Promise(resolve=>setTimeout(resolve,10));}
 assert.equal(calls,2,'skill load plus admitted add, no discarded model continuation');assert.match(sent[0]!,/1 waiting on others/);const read=await readOwnerLoops({...receiver,readBearer:createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex')},{include_parked:false});assert.equal(read.waiting_on_them[0]?.what,'Send the Cedar deck');assert.equal(read.counts.you_owe,0);
 }finally{await app?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});

for(const variant of ['valid','wrong-description','ambiguous','same-person','bare-name','different-identity','same-ask','hidden-matching','wrong-anchor','empty-description'] as const)
test(`queried tasks close from possessive owner wording with current row proofs: ${variant}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-possessive-close-'));
 const journal=new OperationalJournal(join(folder,'operations.sqlite'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
 let coordinator:ConversationCoordinator|undefined;
 try{
  receiver=await startReceiver(join(folder,'receiver'),true,false,['same-person','bare-name','different-identity','same-ask','hidden-matching','wrong-anchor','empty-description'].includes(variant),variant);
  const authority={read:async()=>({version:2 as const,revision:'a'.repeat(64),complete:true,bridge_sources:[],google_sources:['gmail' as const]})};
  const readBearer=createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex');
  let writes=0;
  const client=createOwnerTaskToolClient({...receiver,readBearer,sourceAuthority:authority,fetch:async(input,init)=>{
   const body=JSON.parse(String(init?.body??'{}'));if(body.operation==='apply')writes++;
   const response=await fetch(input,init);
   if(variant==='ambiguous'&&body.operation==='loops_query'){
    const payload=await response.json();const row=payload.you_owe.find((r:{name:string})=>r.name==='Tina Jiang');
    payload.you_owe.push({...row,name:'Tina Smith',anchor_key:'other-tina'});payload.counts.you_owe++;
    return new Response(JSON.stringify(payload),{status:response.status});
   }
   return response;
  }});
  const prior=await client.readLoops('a'.repeat(64));
  assert.match(prior.you_owe.find(row=>row.name==='Tina Jiang')!.ask!,/check-in/);
  const phrases=[variant==='wrong-description'?'Tina’s school application':'Tina’s offer check-in',variant==='bare-name'?'Shimolee':variant==='empty-description'?'Shimolee’s tasks':'Shimolee’s family photo plans'];
  const items=phrases.map((name,index)=>{const row=prior.you_owe.find(row=>variant==='wrong-anchor'&&index===1?row.anchor_key==='resolve-many-extra':row.anchor_key===`resolve-many-${index+1}`)!;
   return {name,correction:name,anchor_key:row.anchor_key,loop_version:row.loop_version!,loop_identity:row.loop_identity!};});
  let calls=0;
  runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Use current task proofs.',sourceAuthority:authority,
   chatTools:()=>createOwnerTaskTools({client,journal:()=>journal,sourceAuthority:authority}),
   model:new MockLanguageModelV4({doGenerate:async()=>{
    const step=calls++;
    return {content:step===1?[{type:'tool-call',toolCallId:'close-pair',toolName:'owner_task_resolve_many',input:JSON.stringify({selection_mode:'named_people',items})}]:[{type:'text',text:step===0?'Tina: offer check-in. Shimolee: family photo plans.':'Closure result.'}],
     finishReason:{unified:step===1?'tool-calls':'stop',raw:'STOP'},usage,warnings:[]};
   }})});
  const sent:string[]=[];
  coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},ownerTaskTool:client,
   model:{generate:request=>runtime!.generateChat({...request,threadId:'possessive-memory',ownerId:'owner'})},
   postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`accepted-${sent.length}`};}});
  const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:receiver!.identity.owner_id,isDirect:true,eventId,text});
  const context={threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'};
  await coordinator.receive(inbound('query','What is open with Tina and Shimolee?'),context);
  const close=inbound('close',phrases.join(' and ')+' are both done. Close those two.');
  await coordinator.receive(close,context);
  const after=await client.readLoops('a'.repeat(64));
  if(variant==='valid'||variant==='same-person'||variant==='different-identity'){
   assert.equal(writes,1);assert.equal(after.you_owe.length,variant==='valid'?2:3);
   assert.ok(after.you_owe.every(row=>!items.some(item=>item.anchor_key===row.anchor_key)));
   assert.equal(calls,2,'query plus mutation step only; no discarded completion');
   if(variant!=='valid')assert.match(after.you_owe.find(row=>row.anchor_key==='resolve-many-extra')!.what,/catering/);
   assert.match(sent.at(-1)!,/Tina’s offer check-in: Marked done/);assert.match(sent.at(-1)!,/Shimolee’s family photo plans: Marked done/);
   await coordinator.receive(close,context);await coordinator.recover();assert.equal(writes,1);
  }else{
   assert.equal(writes,1);
   const heldIndex=variant==='wrong-description'||variant==='ambiguous'?0:1;
   const held=items[heldIndex]!,clear=items[1-heldIndex]!;
   assert.ok(after.you_owe.some(row=>row.anchor_key===held.anchor_key),'unresolved target stays open');
   assert.ok(!after.you_owe.some(row=>row.anchor_key===clear.anchor_key),'independent clear selection closes');
   for(const row of prior.you_owe.filter(row=>row.anchor_key!==clear.anchor_key))assert.deepEqual(after.you_owe.find(value=>value.anchor_key===row.anchor_key),row);
   assert.ok(sent.at(-1)!.includes(clear.name+': Marked done'));
   assert.ok(sent.at(-1)!.includes(held.name+': I couldn’t tell which one you meant'));
   await coordinator.receive(close,context);await coordinator.recover();assert.equal(writes,1);
  }
 }finally{await coordinator?.close();await runtime?.close();journal.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
