import {readSkillIndex} from '../src/tools/skills.js';
import {createOwnerTaskExecution} from '../src/tools/owner-task-execution.js';
import {createTaskCommand,TASK_CORRECTION_EXECUTION_GUIDE} from '../src/tools/task-command.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import {createKnowledgeCommand} from '../src/tools/knowledge-command.js';
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
import { createOwnerTaskToolClient } from '../src/tools/owner-task-tool-client.js';
import { createHash, createHmac } from 'node:crypto';
import { AcceptedConversationMemory } from '../src/conversation-memory.js';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';

async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_PERSON_CORRECTION:'1', SOTTO_TEST_KNOWLEDGE_READ:'1', SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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



const usage = {inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}};
const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:[]})};

test('original Feedback corrects one exact PERSON fact; response loss inspects once, and fresh Ask reads corrected truth', {timeout:90000}, async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-person-correction-'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 let journal=new OperationalJournal(join(folder,'operations.sqlite'));
 let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined,coordinator:ConversationCoordinator|undefined;
 let turn='ambiguous';
 let lose=true,hide=true,applies=0,phase=0,safe=false,asking=false,ambiguous=false,stale=false;
 let selected:{canonical_id:string;fact:{id:string;text:string;fact_version:string}}|undefined;
 const replacement='Dana is the COO of Cedar.';
 const sent:string[]=[];
 const skills=(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path);
 const mount=()=>{
  const readBearer=createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex');
  const client=createOwnerTaskToolClient({...receiver!,readBearer,sourceAuthority,fetch:async(input,init)=>{
   const body=JSON.parse(String(init?.body));
   if(String(input).endsWith('/owner-loop-tool') && body.operation==='inspect' && hide)return new Response('{}',{status:503});
   const response=await fetch(input,init);
   if(String(input).endsWith('/owner-loop-tool') && body.operation==='apply'){
    applies++;if(lose){lose=false;await response.body?.cancel();throw new Error('synthetic lost committed response');}
   }
   return response;
  }});
  const command=createTaskCommand(createOwnerTaskExecution({client,journal:()=>journal,sourceAuthority}));
  const read=createKnowledgeCommand({...receiver!,readBearer,sourceAuthority,isCurrent:(run,event)=>journal.isRunCurrent(run)&&journal.getRun(run)?.event_id===event});
  const script=createRunScriptTool({mode:'chat',commandContext:(context,signal)=>({mode:context?.get('mode'),runId:context?.get('runId'),ownerRequestKey:context?.get('ownerRequestKey'),allowOwnerTask:context?.get('allowOwnerTask'),sourceRevision:context?.get('sourceRevision'),signal}),
   executeCommand:async(request,context)=>{
    if(request.id==='knowledge-query'){
     const result=await read(request,context);
     if(!asking){const rows=Object.values(result.result as Record<string,any>);assert.equal(rows.length,1);assert.equal(rows[0].facts.length,2);if(!ambiguous)selected={canonical_id:rows[0].canonical_id,fact:rows[0].facts.find((fact:any)=>fact.text.startsWith('In 2020'))};}
     return result;
    }
    return command(request,context);
   }});
  const model=new MockLanguageModelV4({doGenerate:async(options)=>{
   if(safe)return {content:[{type:'text',text:'Hello safely'}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};
   const n=phase++;let call:{name:string;input:unknown}|undefined;
   if(n===0)call={name:'skill',input:{name:asking?'sotto-ask':'sotto-feedback'}};
   if(n===1 && stale){assert.ok(selected);call={name:'run_sotto_script',input:{id:'knowledge-edit',args:['--op','correct','--slug',selected.canonical_id,'--fact-id',selected.fact.id,'--fact-version',selected.fact.fact_version,'--text','Dana is the CFO of Cedar.']}};}
   if(n===1&&!stale){assert.match(JSON.stringify(options.prompt),asking?/Where to look/:/Match the correction/);call={name:'run_sotto_script',input:{id:'knowledge-query',args:['--person','Dana Cedar',...(asking?[]:['--editable-person','--topic','pediatric clinic procurement'])]}};}
   if(n===2&&ambiguous){assert.match(JSON.stringify(options.prompt),/Dana enjoys hiking/);assert.match(JSON.stringify(options.prompt),/In 2020 Dana discussed/);}
   if(n===2&&!asking&&!ambiguous){assert.ok(selected?.fact.fact_version);call={name:'run_sotto_script',input:{id:'knowledge-edit',args:['--op','correct','--slug',selected.canonical_id,'--fact-id',selected.fact.id,'--fact-version',selected.fact.fact_version,'--text',stale?'Dana is the CFO of Cedar.':replacement]}};}
   if(n===2&&asking){assert.ok(JSON.stringify(options.prompt).includes(replacement));assert.doesNotMatch(JSON.stringify(options.prompt),/In 2020 Dana discussed pediatric clinic procurement with Cedar/);}
   return {content:call?[{type:'tool-call',toolCallId:`person-${turn}-${asking}-${n}`,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text',text:ambiguous?'Which Dana fact do you mean?':replacement}],finishReason:{unified:call?'tool-calls':'stop',raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,asking?'fresh-memory.db':'memory.db'),instructions:TASK_CORRECTION_EXECUTION_GUIDE,skillPaths:skills,sourceAuthority,chatTools:()=>({run_sotto_script:script})});
  coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'+15555550123'},ownerTaskTool:client,
   model:{generate:request=>runtime!.generateChat({...request,threadId:asking?'fresh-person':'person',ownerId:'owner'})},
   postPrepared:async post=>{sent.push(post.text);return {acceptance:'accepted',messageId:`synthetic-${sent.length}`};},canPostPrepared:revision=>runtime!.canDeliverReplySource(revision)});
  return client;
 };
 const inbound=(eventId:string,text:string,senderId='+15555550123')=>({channel:'photon' as const,accountId:'account',eventId,text,senderId,isDirect:true});
 const context=()=>({threadId:receiver!.identity.route_id,destination:'photon',lineKey:'line'});
 try{
  receiver=await startReceiver(join(folder,'receiver'));mount();
  assert.equal((await coordinator!.receive(inbound('foreign',replacement,'+15555550124'),context())).status,'denied');assert.equal(applies,0);
  ambiguous=true;
  assert.equal((await coordinator!.receive(inbound('ambiguous','That Dana Cedar fact is wrong.'),context())).status,'accepted');
  assert.equal(sent.at(-1),'Which Dana fact do you mean?');assert.equal(applies,0);
  ambiguous=false;phase=0;sent.length=0;turn='correct';
  assert.equal((await coordinator!.receive(inbound('correct',replacement),context())).status,'held');assert.equal(applies,1);assert.equal(sent.length,1);assert.match(sent[0]!,/^I couldn’t confirm whether the task change happened\./,'the pending-effect notice arrives without more owner input');
  const action=journal.db.prepare("SELECT * FROM actions WHERE kind='owner_loop_tool'").get()!;assert.equal(action.status,'unknown');
  safe=true;await coordinator!.receive(inbound('hello','Hello independently'),context());assert.equal(sent.length,2);assert.match(sent[0]!,/^I couldn’t confirm whether the task change happened\./);assert.equal(sent.at(-1),'Hello safely');assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether ')).length,1,'safe chat does not repeat the pending-effect notice');safe=false;
  await coordinator!.close();await runtime!.close();await stopReceiver(receiver.child);journal.close();
  journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE status='unknown'").run();receiver=await startReceiver(join(folder,'receiver'));hide=false;mount();
  await coordinator!.recover();assert.equal(applies,1);assert.equal(sent.at(-1),'Correction recorded.');
  assert.equal(journal.getAction(action.id as string)!.status,'succeeded');
  assert.equal((await coordinator!.receive(inbound('correct',replacement),context())).status,'duplicate');assert.equal(applies,1);
  stale=true;phase=0;turn='stale';
  const beforeStale=readFileSync(join(folder,'receiver/knowledge/people',selected!.canonical_id+'.md'),'utf8');
  assert.equal((await coordinator!.receive(inbound('stale','Dana is the CFO of Cedar.'),context())).status,'accepted');
  assert.equal(readFileSync(join(folder,'receiver/knowledge/people',selected!.canonical_id+'.md'),'utf8'),beforeStale);
  assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE status='rejected'").get()!.n,1);
  stale=false;
  await coordinator!.close();await runtime!.close();asking=true;phase=0;turn='ask';mount();
  const before=readFileSync(join(folder,'receiver/knowledge/people',selected!.canonical_id+'.md'),'utf8');
  assert.equal((await coordinator!.receive(inbound('ask','What is Dana Cedar’s role?'),context())).status,'accepted');assert.equal(sent.at(-1),replacement);
  assert.equal(readFileSync(join(folder,'receiver/knowledge/people',selected!.canonical_id+'.md'),'utf8'),before);
  assert.equal(applies,2,'one successful apply and one explicit stale refusal; recovery never applies');assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,2);
 }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('original Feedback teaches identifier; lost receipt reopens inspect-only and fresh Ask resolves it',{timeout:90000},async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-person-identifier-'));let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 let journal=new OperationalJournal(join(folder,'operations.sqlite'));let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined,c:ConversationCoordinator|undefined;
 const identifier='Dana.New@Example.com';let canonicalReview='',canonical='',phase=0,mode='teach',lose=true,hide=true,applies=0;const sent:string[]=[];
 const skills=(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(s=>s.path);
 const mount=()=>{
  const readBearer=createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex');
  const client=createOwnerTaskToolClient({...receiver!,readBearer,sourceAuthority,fetch:async(url,init)=>{const body=JSON.parse(String(init?.body));if(String(url).endsWith('/owner-loop-tool')&&body.operation==='inspect'&&hide)return new Response('{}',{status:503});const response=await fetch(url,init);if(String(url).endsWith('/owner-loop-tool')&&body.operation==='apply'){applies++;if(lose){lose=false;await response.body?.cancel();throw new Error('lost committed identifier response');}}return response;}});
  const read=createKnowledgeCommand({...receiver!,readBearer,sourceAuthority,isCurrent:(run,event)=>journal.isRunCurrent(run)&&journal.getRun(run)?.event_id===event});
  const command=createTaskCommand(createOwnerTaskExecution({client,journal:()=>journal,sourceAuthority}));
  const script=createRunScriptTool({mode:'chat',commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerTask:ctx?.get('allowOwnerTask'),sourceRevision:ctx?.get('sourceRevision'),signal}),executeCommand:async(req,ctx)=>{if(req.id==='knowledge-query'){const result=await read(req,ctx);const rows=Object.entries(result.result as Record<string,any>);assert.equal(rows.length,1);if(mode==='ask')assert.equal(rows[0]![0],canonical);else canonical=rows[0]![0];return result;}const result=await command(req,ctx);if(result.result.status==='proposed'&&'review_text' in result.result)canonicalReview=result.result.review_text;return result;}});
  const model=new MockLanguageModelV4({doGenerate:async(options)=>{const n=phase++;if(mode==='duplicate'&&n===0)assert.ok(canonicalReview.length>0&&JSON.stringify(options.prompt).includes(JSON.stringify(canonicalReview).slice(1,-1)),'normal final review survives same-folder reopen');if(mode==='teach'&&n===3){const prompt=JSON.stringify(options.prompt);assert.ok(prompt.includes('needs_owner_confirmation'));assert.ok(prompt.includes('Save identifier:'));assert.ok(prompt.includes('assistant response containing review_text exactly, without more tools'));}let call:{name:string;input:unknown}|undefined;if(mode==='confirm'){if(n===0){assert.ok(canonicalReview.length>0&&JSON.stringify(options.prompt).includes(JSON.stringify(canonicalReview).slice(1,-1)));}if(n===0)call={name:'run_sotto_script',input:{id:'knowledge-edit',args:['--op','confirm-identifier']}};}else if(mode!=='safe'){if(n===0)call={name:'skill',input:{name:mode==='ask'?'sotto-ask':'sotto-feedback'}};if(n===1){assert.match(JSON.stringify(options.prompt),mode==='ask'?/Where to look/:/Match the correction/);call={name:'run_sotto_script',input:{id:'knowledge-query',args:['--person',mode==='ask'?identifier:'Dana Cedar']}};}if(n===2&&mode!=='ask'){assert.ok(canonical);call={name:'run_sotto_script',input:{id:'knowledge-edit',args:['--op','add-identifier','--slug',canonical,'--identifier',identifier]}};}}return {content:call?[{type:'tool-call',toolCallId:`identifier-${mode}-${n}`,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text',text:mode==='ask'?'Dana resolved.':(mode==='teach'||mode==='duplicate')?canonicalReview:'Hello safely'}],finishReason:{unified:call?'tool-calls':'stop',raw:'STOP'},usage,warnings:[]};}});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,mode==='ask'?'fresh-memory.db':'memory.db'),instructions:TASK_CORRECTION_EXECUTION_GUIDE,skillPaths:skills,sourceAuthority,chatTools:()=>({run_sotto_script:script})});
  // This fixture mounts the coordinator directly; wire the same accepted-history
  // projection as the application instead of relying on generation to save prose.
  const projection=new AcceptedConversationMemory({journal,ownerId:'owner',threadFor:()=> 'identifier',sessionStartedAt:()=>undefined,
   scopeThread:thread=>createHash('sha256').update(JSON.stringify(['account','line','photon',thread])).digest('hex'),
   reset:thread=>runtime!.rotateThread(thread),project:input=>runtime!.projectConversation(input)});
  c=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'+15555550123'},ownerTaskTool:client,
   projectConversation:()=>projection.sync(receiver!.identity.route_id),
   model:{generate:async r=>{await projection.sync(r.threadId);return runtime!.generateChat({...r,threadId:'identifier',ownerId:'owner',
    historyInputIds:projection.currentInputIds(r.threadId,r.messages.map(message=>message.eventId))});}},postPrepared:async r=>{sent.push(r.text);return {acceptance:'accepted',messageId:`ack-${sent.length}`};},canPostPrepared:r=>runtime!.canDeliverReplySource(r)});
 };
 const inbound=(eventId:string,text:string,senderId='+15555550123')=>({channel:'photon' as const,accountId:'account',senderId,isDirect:true,eventId,text});
 const context=()=>({threadId:receiver!.identity.route_id,destination:'photon',lineKey:'line'});
 try{
  receiver=await startReceiver(join(folder,'receiver'));mount();
  assert.equal((await c!.receive(inbound('foreign',`Dana Cedar uses ${identifier}`,'+15555550124'),context())).status,'denied');assert.equal(applies,0);
  mode='lookup';assert.equal((await c!.receive(inbound('lookup',`Look up ${identifier}. Who does it belong to?`),context())).status,'accepted');assert.equal(applies,0);
  mode='teach';phase=0;assert.equal((await c!.receive(inbound('teach',`Dana Cedar uses ${identifier}`),context())).status,'accepted');assert.match(sent.at(-1)!,/Save identifier:/);assert.equal(applies,0);
  mode='confirm';phase=0;assert.equal((await c!.receive(inbound('yes','Yes.'),context())).status,'held');assert.equal(applies,1);
  const action=journal.db.prepare("SELECT * FROM actions WHERE kind='owner_loop_tool' AND json_extract(result_json,'$.args.operation')='identifier'").get()!;assert.equal(action.status,'unknown');
  assert.match(sent.at(-1)!,/^I couldn’t confirm whether the task change happened\./,'the pending-effect notice arrives without more owner input');
  mode='safe';await c!.receive(inbound('safe','Hello independently'),context());assert.match(sent.at(-2)!,/^I couldn’t confirm whether the task change happened\./);assert.equal(sent.at(-1),'Hello safely');assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether ')).length,1,'safe chat does not repeat the pending-effect notice');
  await c!.close();await runtime!.close();await stopReceiver(receiver.child);journal.close();
  journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE status='unknown'").run();receiver=await startReceiver(join(folder,'receiver'));hide=false;phase=0;mount();await c!.recover();assert.equal(applies,1);assert.equal(sent.at(-1),'Identifier added.');
  assert.equal((await c!.receive(inbound('teach',`Dana Cedar uses ${identifier}`),context())).status,'duplicate');
  const path=join(folder,'receiver/knowledge/people',canonical+'.md'),before=readFileSync(path,'utf8');
  mode='duplicate';phase=0;assert.equal((await c!.receive(inbound('again',`Dana Cedar uses ${identifier}`),context())).status,'accepted');assert.match(sent.at(-1)!,/Already recorded/);assert.equal(applies,1);mode='confirm';phase=0;assert.equal((await c!.receive(inbound('again-yes','Yes.'),context())).status,'accepted');assert.equal(sent.at(-1),'That identifier is already known.');assert.equal(readFileSync(path,'utf8'),before);
  await c!.close();await runtime!.close();mode='ask';phase=0;mount();assert.equal((await c!.receive(inbound('ask',`Who is ${identifier}?`),context())).status,'accepted');assert.equal(sent.at(-1),'Dana resolved.');assert.equal(readFileSync(path,'utf8'),before);assert.equal(applies,2);
 }finally{await c?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});
