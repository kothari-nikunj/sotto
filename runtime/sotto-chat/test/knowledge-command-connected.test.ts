import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync, readdirSync, readFileSync, writeFileSync} from 'node:fs';
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
import {createSourceAuthorityReader,type SourceAuthorityReader} from '../src/agent/source-authority.js';
import {createKnowledgeCommand, KNOWLEDGE_EXECUTION_GUIDE, LOOPS_EXECUTION_GUIDE, GRANOLA_HISTORY_EXECUTION_GUIDE} from '../src/tools/knowledge-command.js';
import {createOwnerLoopsTool} from '../src/agent/owner-loops.js';
async function startReceiver(folder: string, loops = false, granola = false, history = false) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_KNOWLEDGE_READ: loops || granola || history ? undefined : '1', SOTTO_TEST_LOOPS_READ: loops ? '1' : undefined, SOTTO_TEST_GRANOLA_READ:granola?'1':undefined, SOTTO_TEST_GRANOLA_HISTORY:history?'1':undefined, SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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




const usage = {inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:[]})};
test('mixed three-loop closure and open-list question reads both partial views without mutating or queuing', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-mixed-loop-read-'));
  const journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
  let coordinator:ConversationCoordinator|undefined;
  try {
    receiver=await startReceiver(join(folder,'receiver'),true);
    const authority:SourceAuthorityReader={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['gmail' as const]})};
    const directory=join(folder,'receiver/knowledge/continuity');
    const before=readdirSync(directory).sort().map(name=>[name,readFileSync(join(directory,name),'utf8')]);
    let sourceReads=0,ownerReads=0,step=0;
    const command=createKnowledgeCommand({...receiver,sourceAuthority:authority,
      isCurrent:(run,event)=>journal.isRunCurrent(run)&&journal.getRun(run)?.event_id===event,
      fetch:async(input,init)=>{sourceReads++;return fetch(input,init);}});
    const script=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({
      mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),sourceRevision:ctx?.get('sourceRevision'),signal})});
    const owner=createOwnerLoopsTool({...receiver,fetch:async(input,init)=>{ownerReads++;return fetch(input,init);}});
    const calls=[{name:'skill',args:{name:'sotto-loops'}},{name:'run_sotto_script',args:{id:'loops-query',args:[]}},{name:'owner_open_loops',args:{include_parked:false}},null];
    const model=new MockLanguageModelV4({doGenerate:async request=>{
      const prompt=JSON.stringify(request.prompt);
      if(step===1)assert.match(prompt,/request for the current open list is not an invocation of the followup background skill/);
      if(step===3){assert.match(prompt,/currently_permitted_canonical_loops/);assert.match(prompt,/owner_authored_only/);}
      const call=calls[step++];
      return {content:call?[{type:'tool-call' as const,toolCallId:`mixed-${step}`,toolName:call.name,input:JSON.stringify(call.args)}]:[{type:'text' as const,text:'I can show the currently permitted open loops, but I have not marked Dana, Morgan, or Ravi done. Which exact loop should I correct first?'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:LOOPS_EXECUTION_GUIDE,
      skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority:authority,
      chatTools:()=>({run_sotto_script:script,owner_open_loops:owner})});
    const sent:string[]=[];
    coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},
      model:{generate:request=>runtime!.generateChat({...request,threadId:'mixed-memory',ownerId:'owner'})},
      postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'mixed-answer'};}});
    assert.equal((await coordinator.receive({channel:'photon',accountId:'account',senderId:receiver.identity.owner_id,isDirect:true,eventId:'mixed-loop',text:'Dana, Morgan, and Ravi loops are closed. What else is open?'},
      {threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'})).status,'accepted');
    assert.equal(step,4);assert.equal(sourceReads,1);assert.equal(ownerReads,1);
    assert.match(sent[0]!,/have not marked/);assert.match(sent[0]!,/Which exact loop/);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
    assert.deepEqual(readdirSync(directory).sort().map(name=>[name,readFileSync(join(directory,name),'utf8')]),before);
  } finally {await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('native Ask loads original skill, reads actual canonical older topic fact, and answers in attending loop',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-knowledge-http-'));
  const journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
  let coordinator:ConversationCoordinator|undefined;
  try {
    receiver=await startReceiver(join(folder,'receiver'));
    const reads:string[]=[];
    const command=createKnowledgeCommand({...receiver,sourceAuthority,
      isCurrent:(runId,eventId)=>journal.isRunCurrent(runId)&&journal.getRun(runId)?.event_id===eventId,
      fetch:async(input,init)=>{reads.push(new URL(String(input)).pathname);return fetch(input,init);}});
    const tool=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({
      mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),sourceRevision:ctx?.get('sourceRevision'),signal})});
    let step=0;
    const calls=[{name:'skill',args:{name:'sotto-ask'}},{name:'run_sotto_script',args:{id:'knowledge-query',args:['--person','Dana Cedar','--person-coverage','--topic','pediatric']}},null];
    const model=new MockLanguageModelV4({doGenerate:async request=>{
      const prompt=JSON.stringify(request.prompt);
      if(step===1)assert.ok(prompt.includes('run_sotto_script knowledge-query --person'));
      if(step===2)assert.ok(prompt.includes('pediatric clinic procurement'));
      const call=calls[step++];
      return {content:call?[{type:'tool-call' as const,toolCallId:`knowledge-${step}`,toolName:call.name,input:JSON.stringify(call.args)}]:[{type:'text' as const,text:'Dana previously discussed pediatric clinic procurement; this is sourced memory, not a complete transcript.'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:KNOWLEDGE_EXECUTION_GUIDE,
      skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,chatTools:()=>({run_sotto_script:tool})});
    const sent:string[]=[];
    coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},
      model:{generate:request=>runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'knowledge-answer'};}});
    assert.equal((await coordinator.receive({channel:'photon',accountId:'account',senderId:receiver.identity.owner_id,isDirect:true,eventId:'knowledge-question',text:'What do I know about Dana Cedar and pediatric clinics?'},
      {threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'})).status,'accepted');
    assert.match(sent[0]!,/pediatric clinic procurement/);assert.equal(step,3);assert.deepEqual(reads,['/runtime/knowledge-read']);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
    const queuePath=join(folder,'receiver/events/work.sqlite3');
    if(existsSync(queuePath)){const queue=new DatabaseSync(queuePath,{readOnly:true});try{assert.equal(queue.prepare('SELECT count(*) AS n FROM jobs').get()!.n,0);}finally{queue.close();}}
  }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('native Ask reads original writer-seeded saved company name and domain, reports no match, and never mutates',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-knowledge-http-'));
  const journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
  let coordinator:ConversationCoordinator|undefined;
  try {
    receiver=await startReceiver(join(folder,'receiver'));
    const companyDirectory=join(folder,'receiver/knowledge/companies');const before=readdirSync(companyDirectory).map(name=>[name,readFileSync(join(companyDirectory,name),'utf8')]);
    const resultBodies:Array<{status:string;company_knowledge:Record<string,string>;truncated:boolean}>=[];const reads:string[]=[];
    const command=createKnowledgeCommand({...receiver,sourceAuthority,
      isCurrent:(runId,eventId)=>journal.isRunCurrent(runId)&&journal.getRun(runId)?.event_id===eventId,
      fetch:async(input,init)=>{reads.push(new URL(String(input)).pathname);const response=await fetch(input,init);resultBodies.push(await response.clone().json());return response;}});
    const tool=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({
      mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),sourceRevision:ctx?.get('sourceRevision'),signal})});
    let step=0;
    const calls=[{name:'skill',args:{name:'sotto-ask'}},...['Cedar Labs','cedar.example','Unknown Company'].map(company=>({name:'run_sotto_script',args:{id:'knowledge-query',args:['--company',company]}})),null];
    const model=new MockLanguageModelV4({doGenerate:async request=>{
      const prompt=JSON.stringify(request.prompt);
      if(step===1)assert.ok(prompt.includes('run_sotto_script knowledge-query --company'));
      if(step===2||step===3)assert.ok(prompt.includes('Saved owner meeting context.'));
      if(step===4)assert.ok(prompt.includes('missing'));
      const call=calls[step++];
      return {content:call?[{type:'tool-call' as const,toolCallId:`knowledge-${step}`,toolName:call.name,input:JSON.stringify(call.args)}]:[{type:'text' as const,text:'Saved Cedar Labs knowledge says it builds clinic tools. Unknown Company has no saved match; this is not fresh research.'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:KNOWLEDGE_EXECUTION_GUIDE,
      skillPaths:(await readSkillIndex(process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,chatTools:()=>({run_sotto_script:tool})});
    const sent:string[]=[];
    coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},
      model:{generate:request=>runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'knowledge-answer'};}});
    assert.equal((await coordinator.receive({channel:'photon',accountId:'account',senderId:receiver.identity.owner_id,isDirect:true,eventId:'knowledge-question',text:'What saved knowledge do I have about Cedar Labs, cedar.example, and Unknown Company?'},
      {threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'})).status,'accepted');
    assert.match(sent[0]!,/builds clinic tools/);assert.equal(step,5);assert.deepEqual(resultBodies.map(value=>value.status),['saved','saved','missing']);assert.deepEqual(resultBodies[0]!.company_knowledge,resultBodies[1]!.company_knowledge);assert.equal(resultBodies[0]!.truncated,false,'the complete short record includes saved Context');assert.equal(resultBodies[2]!.truncated,false);assert.deepEqual(reads,Array(3).fill('/runtime/knowledge-read'));assert.deepEqual(readdirSync(companyDirectory).map(name=>[name,readFileSync(join(companyDirectory,name),'utf8')]),before);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
    const queuePath=join(folder,'receiver/events/work.sqlite3');
    if(existsSync(queuePath)){const queue=new DatabaseSync(queuePath,{readOnly:true});try{assert.equal(queue.prepare('SELECT count(*) AS n FROM jobs').get()!.n,0);}finally{queue.close();}}
  }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});

for (const [skillName,granola] of [['sotto-ask',false],['sotto-people',false],['sotto-ask',true]] as const) test(`native ${skillName} granola=${granola} reads currently permitted canonical source loops without writes`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-loops-http-'));
  const journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
  let coordinator:ConversationCoordinator|undefined;
  try {
    receiver=await startReceiver(join(folder,'receiver'),!granola,granola);
    const authority:SourceAuthorityReader=granola?createSourceAuthorityReader({bridgeUrl:receiver.bridgeUrl,bearer:receiver.readBearer}):{read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['gmail' as const]})};
    if(granola){const actual=await authority.read();assert.equal(actual.complete,true);assert.deepEqual(actual.connector_sources,['granola']);assert.deepEqual(actual.google_sources,[]);}
    const directory=join(folder,'receiver/knowledge/continuity');
    const snapshot=()=>readdirSync(directory).sort().map(name=>[name,readFileSync(join(directory,name),'utf8')]);
    const before=snapshot();let reads=0;
    const command=createKnowledgeCommand({...receiver,sourceAuthority:authority,
      isCurrent:(runId,eventId)=>journal.isRunCurrent(runId)&&journal.getRun(runId)?.event_id===eventId,
      fetch:async(input,init)=>{reads++;const response=await fetch(input,init);const value=await response.clone().json();
        assert.equal(response.status,200);assert.equal(value.coverage,'currently_permitted_canonical_loops');
        assert.equal(value.counts.you_owe,1);assert.equal(value.counts.waiting_on_them,granola?0:1);
        assert.equal(value.you_owe[0].what,granola?'Send Dana the Granola Cedar commitment':'Send Dana the Cedar procurement review');
        if(!granola)assert.equal(value.waiting_on_them[0].what,'Waiting for Eli Cedar to approve procurement');
        assert.ok(!JSON.stringify(value).includes('Denied'));return response;}});
    const tool=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),sourceRevision:ctx?.get('sourceRevision'),signal})});
    let step=0;const argv=skillName==='sotto-people'?['--parked']:[];
    const calls=[{name:'skill',args:{name:skillName}},{name:'run_sotto_script',args:{id:'loops-query',args:argv}},null];
    const model=new MockLanguageModelV4({doGenerate:async request=>{
      const prompt=JSON.stringify(request.prompt);
      if(step===1)assert.ok(prompt.includes('run_sotto_script loops-query'),'original skill instructs canonical ledger read');
      if(step===2){assert.ok(prompt.includes(granola?'Granola Cedar commitment':'Cedar procurement review'));assert.ok(!prompt.includes('Denied private Mac'));}
      const call=calls[step++];return {content:call?[{type:'tool-call' as const,toolCallId:`loops-${step}`,toolName:call.name,input:JSON.stringify(call.args)}]:[{type:'text' as const,text:granola?'You owe Dana the Granola Cedar commitment; no waiting-on rows are currently permitted.':'You owe Dana the Cedar procurement review and are waiting for Eli’s approval. These are the currently permitted canonical loops.'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:LOOPS_EXECUTION_GUIDE,
      skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority:authority,chatTools:()=>({run_sotto_script:tool})});
    const sent:string[]=[];
    coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},model:{generate:request=>runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'loops-answer'};}});
    assert.equal((await coordinator.receive({channel:'photon',accountId:'account',senderId:receiver.identity.owner_id,isDirect:true,eventId:'loops-question',text:'Who do I owe, and what am I waiting for?'},{threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'})).status,'accepted');
    assert.match(sent[0]!,/currently permitted/);if(granola){assert.match(sent[0]!,/Granola Cedar commitment/);assert.ok(!sent[0]!.includes('Eli'));}assert.equal(reads,1);assert.equal(step,3);assert.deepEqual(snapshot(),before);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
    const queuePath=join(folder,'receiver/events/work.sqlite3');
    if(existsSync(queuePath)){const queue=new DatabaseSync(queuePath,{readOnly:true});try{assert.equal(queue.prepare('SELECT count(*) AS n FROM jobs').get()!.n,0);}finally{queue.close();}}
  }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('native Ask discovers Granola metadata, asks owner which meeting, then reads exact notes/transcript/follow-through in same conversation',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-granola-history-'));
  const journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,runtime:ReturnType<typeof createSottoAgentRuntime>|undefined,coordinator:ConversationCoordinator|undefined;
  try {
    receiver=await startReceiver(join(folder,'receiver'),false,false,true);
    const authority=createSourceAuthorityReader({bridgeUrl:receiver.bridgeUrl,bearer:receiver.readBearer});
    assert.deepEqual((await authority.read()).connector_sources,['granola']);
    const directory=join(folder,'receiver/knowledge/continuity');
    const before=readdirSync(directory).sort().map(name=>[name,readFileSync(join(directory,name),'utf8')]);
    const results:Record<string,any>[]=[];
    const command=createKnowledgeCommand({...receiver,sourceAuthority:authority,
      isCurrent:(run,event)=>journal.isRunCurrent(run)&&journal.getRun(run)?.event_id===event,
      fetch:async(input,init)=>{const response=await fetch(input,init);assert.equal(response.status,200);results.push(await response.clone().json());return response;}});
    const tool=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),sourceRevision:ctx?.get('sourceRevision'),signal})});
    let step=0;
    const calls=[{name:'skill',args:{name:'sotto-ask'}},{name:'run_sotto_script',args:{id:'granola-history',args:['--days','14']}},null,
      {name:'run_sotto_script',args:{id:'granola-history',args:['--days','14','--meeting-id','synthetic-meeting']}},null];
    const model=new MockLanguageModelV4({doGenerate:async request=>{
      const prompt=JSON.stringify(request.prompt);
      if(step===1)assert.ok(prompt.includes('run_sotto_script granola-history --days'));
      if(step===2){assert.equal(results[0]!.meetings.length,2);assert.ok(!prompt.includes('Decision: use'));assert.ok(!prompt.includes('Dana: We agreed'));}
      if(step===4){assert.ok(prompt.includes('pediatric clinic pilot'));assert.ok(prompt.includes('Granola Cedar commitment'));assert.ok(prompt.includes('Dana: We agreed'));}
      const call=calls[step++];return {content:call?[{type:'tool-call' as const,toolCallId:`history-${step}`,toolName:call.name,input:JSON.stringify(call.args)}]:[{type:'text' as const,text:step===3?'Two Cedar reviews match. Which meeting do you mean?':'The selected meeting agreed to the pediatric clinic pilot. Dana said she would send the commitment; the ledger records your Cedar follow-through.'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:GRANOLA_HISTORY_EXECUTION_GUIDE,
      skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority:authority,chatTools:()=>({run_sotto_script:tool})});
    const sent:string[]=[];
    coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},model:{generate:request=>runtime!.generateChat({...request,threadId:'history-memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`history-answer-${sent.length}`};}});
    const receive=(eventId:string,text:string)=>coordinator!.receive({channel:'photon',accountId:'account',senderId:receiver!.identity.owner_id,isDirect:true,eventId,text},{threadId:receiver!.identity.route_id,destination:'photon',lineKey:'line'});
    assert.equal((await receive('history-discover','What did we decide in the Cedar review?')).status,'accepted');
    assert.equal(results.length,1);assert.match(sent[0]!,/Which meeting/);
    assert.equal((await receive('history-select','The meeting synthetic-meeting, please.')).status,'accepted');
    assert.equal(results.length,2);assert.equal(results[1]!.meeting_id,'synthetic-meeting');assert.equal(results[1]!.transcript_available,true);
    assert.equal(results[1]!.obligations.length,1);assert.match(sent[1]!,/pediatric clinic pilot/);
    assert.deepEqual(readdirSync(directory).sort().map(name=>[name,readFileSync(join(directory,name),'utf8')]),before);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
    const queue=join(folder,'receiver/events/work.sqlite3');if(existsSync(queue)){const db=new DatabaseSync(queue,{readOnly:true});try{assert.equal(db.prepare('SELECT count(*) AS n FROM jobs').get()!.n,0);}finally{db.close();}}
    // Abort an actual in-flight receiver/subprocess read, not only its returned answer.
    const callsPath=join(folder,'receiver/synthetic-mcp-calls.jsonl');
    const callCount=readFileSync(callsPath,'utf8').trim().split('\n').length;
    writeFileSync(join(folder,'receiver/synthetic-mcp-block'),'block');
    const abort=new AbortController();
    const cancellationCommand=createKnowledgeCommand({...receiver,sourceAuthority:authority,isCurrent:()=>true});
    const cancelled=cancellationCommand({id:'granola-history',args:['--meeting-id','synthetic-meeting'],mode:'chat'},
      {mode:'chat',runId:'cancel-run',ownerRequestKey:'cancel-event',sourceRevision:(await authority.read()).revision,signal:abort.signal});
    const rejection=assert.rejects(cancelled);
    // Allow isolated Python/TLS startup under the emulated production image.
    for(let wait=0;wait<500&&!existsSync(join(folder,'receiver/synthetic-mcp-blocked'));wait++)await new Promise(resolve=>setTimeout(resolve,20));
    assert.ok(existsSync(join(folder,'receiver/synthetic-mcp-blocked')));abort.abort();await rejection;
    // Wait for the provider's disconnect observation, including its two-second
    // blocked response, without assuming a 300ms scheduling margin under load.
    const disconnected=join(folder,'receiver/synthetic-mcp-disconnected');
    for(let wait=0;wait<500&&!existsSync(disconnected);wait++)await new Promise(resolve=>setTimeout(resolve,20));
    const afterAbort=readFileSync(callsPath,'utf8').trim().split('\n').slice(callCount).map(line=>JSON.parse(line));
    assert.ok(!afterAbort.some(row=>['get_meetings','get_meeting_transcript'].includes(row.params?.name)));
    assert.ok(existsSync(disconnected),'isolated worker connection closed before blocked provider response');
    const mcp=readFileSync(join(folder,'receiver/synthetic-mcp-calls.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));
    assert.equal(mcp.filter(row=>row.params?.name==='get_meeting_transcript').length,1);
    assert.equal(mcp.filter(row=>row.params?.name==='get_meetings').length,1);
  }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});


test('actual unattended routine reads consent-filtered canonical tasks through receiver without owner mutation authority', async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-routine-canonical-'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
 try {
  receiver=await startReceiver(join(folder,'receiver'),true);
  let complete=true;
  const authority:SourceAuthorityReader={read:async()=>({version:2 as const,complete,revision:complete?'a'.repeat(64):null,bridge_sources:[],google_sources:['gmail' as const]})};
  const directory=join(folder,'receiver/knowledge/continuity');
  const before=readdirSync(directory).sort().map(name=>[name,readFileSync(join(directory,name),'utf8')]);
  let contacts=0,step=0;
  const command=createKnowledgeCommand({...receiver,mode:'unattended',sourceAuthority:authority,
   fetch:async(input,init)=>{contacts++;return fetch(input,init);}});
  const script=createRunScriptTool({mode:'unattended',executeCommand:command,commandContext:(ctx,signal)=>({
   mode:ctx?.get('mode'),runId:ctx?.get('runId'),sourceRevision:ctx?.get('sourceRevision'),signal})});
  const model=new MockLanguageModelV4({doGenerate:async request=>{
   assert.deepEqual(request.tools?.map(tool=>tool.name),['run_sotto_script']);
   if(step++===0)return {content:[{type:'tool-call' as const,toolCallId:'routine-loops',toolName:'run_sotto_script',input:JSON.stringify({id:'loops-query',args:[]})}],finishReason:{unified:'tool-calls' as const,raw:'STOP'},usage,warnings:[]};
   const prompt=JSON.stringify(request.prompt);
   assert.match(prompt,/Send Dana the Cedar procurement review/);
   assert.match(prompt,/Waiting for Eli Cedar to approve procurement/);
   assert.match(prompt,/currently_permitted_canonical_loops/);
   assert.doesNotMatch(prompt,/Denied private|Denied mixed|Denied ambiguous/);
   return {content:[{type:'text' as const,text:'Two permitted current obligations.'}],finishReason:{unified:'stop' as const,raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Summarize current source-backed obligations read-only.',
   sourceAuthority:authority,unattendedTools:()=>({run_sotto_script:script})});
  const result=await runtime.generateUnattended({runId:'scheduled-routine',text:'What are my open loops?'});
  assert.equal(result.text,'Two permitted current obligations.');assert.equal(step,2);assert.equal(contacts,1);
  assert.deepEqual(readdirSync(directory).sort().map(name=>[name,readFileSync(join(directory,name),'utf8')]),before);
  const queuePath=join(folder,'receiver/events/work.sqlite3');
  if(existsSync(queuePath)){const queue=new DatabaseSync(queuePath,{readOnly:true});try{assert.equal(queue.prepare('SELECT count(*) AS n FROM jobs').get()!.n,0);}finally{queue.close();}}
  complete=false;
  await assert.rejects(runtime.generateUnattended({runId:'incomplete-routine',text:'What are my open loops?'}));
  assert.equal(step,2,'incomplete source authority prevents model contact');assert.equal(contacts,1);
  complete=true;
  const abort=new AbortController();abort.abort();
  await assert.rejects(runtime.generateUnattended({runId:'cancelled-routine',text:'What are my open loops?',signal:abort.signal}));
  assert.equal(step,2);assert.equal(contacts,1);
 } finally {await runtime?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});


test('topic-only outcome lookup traverses the protected receiver once and leaves ledger and jobs unchanged', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-topic-outcome-'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver'), true);
    const directory = join(folder, 'receiver/knowledge/continuity');
    const row = {anchor_key:'orion-approval',canonical_id:'c_abc111',contact_name:'Alex Reed',
      contact_identifier:'alex@example.test',status:'resolved',action_type:'follow_up',
      source:'gmail',channel:'gmail',source_thread_id:'original-thread',created_at:'2026-10-18',
      ask:'Orion approval follow-up',resolution:'replied',resolution_evidence:'Owner approved; document sent.',
      source_refs:[{sourceType:'gmail',sourceId:'original',snippet:'Orion approval requested.'}],
      resolution_source_refs:[{sourceType:'gmail',sourceId:'sent',snippet:'The approved document was sent.'}]};
    writeFileSync(join(directory, 'orion.md'), '---\n' + JSON.stringify(row) + '\n---\n');
    const before = readdirSync(directory).sort().map(name => [name,readFileSync(join(directory,name),'utf8')]);
    let reads = 0, sourceCurrent = true;
    const command = createKnowledgeCommand({...receiver,isCurrent:()=>true,
      sourceAuthority:{read:async()=>({version:2 as const,complete:sourceCurrent,revision:'a'.repeat(64),bridge_sources:[],google_sources:['gmail' as const]})},
      fetch:async(input,init)=>{reads++;return fetch(input,init);}});
    const invocation = {id:'knowledge-query',args:['--topic','Orion'],mode:'chat' as const};
    const context = {mode:'chat',runId:'read-only-run',ownerRequestKey:'owner-event',sourceRevision:'a'.repeat(64)};
    const value = (await command(invocation,context)).result as {person_knowledge:unknown;contact_index:unknown[];
      obligation_history:{status:string;absence_is_authoritative:boolean;items:Array<{anchor_key:string;completion_basis:string;source_refs:Array<{sourceId:string}>;resolution_source_refs:Array<{sourceId:string}>}>}};
    assert.equal(reads,1);
    assert.deepEqual(value.person_knowledge,{});
    assert.deepEqual(value.contact_index,[]);
    assert.equal(value.obligation_history.status,'matched');
    assert.equal(value.obligation_history.absence_is_authoritative,false);
    assert.equal(value.obligation_history.items[0]!.completion_basis,'source_evidence');
    assert.equal(value.obligation_history.items[0]!.source_refs[0]!.sourceId,'original');
    assert.equal(value.obligation_history.items[0]!.resolution_source_refs[0]!.sourceId,'sent');
    assert.deepEqual(readdirSync(directory).sort().map(name => [name,readFileSync(join(directory,name),'utf8')]),before);
    const queuePath = join(folder,'receiver/events/work.sqlite3');
    if(existsSync(queuePath)){const queue=new DatabaseSync(queuePath,{readOnly:true});try{assert.equal(queue.prepare('SELECT count(*) AS n FROM jobs').get()!.n,0);}finally{queue.close();}}
    sourceCurrent = false;
    await assert.rejects(command(invocation,context));
    assert.equal(reads,1,'incomplete current authority blocks another receiver read');
  } finally {if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
