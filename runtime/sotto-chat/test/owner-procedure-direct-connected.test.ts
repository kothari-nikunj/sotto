import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn,type ChildProcessWithoutNullStreams} from 'node:child_process';
import {once} from 'node:events';
import {existsSync,readFileSync,writeFileSync,mkdtempSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {MockLanguageModelV4} from 'ai/test';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {readSkillIndex} from '../src/tools/skills.js';
import {createKnowledgeCommand,MASTER_CONTEXT_EXECUTION_GUIDE} from '../src/tools/knowledge-command.js';
import {createOwnerPreferenceToolClient} from '../src/tools/owner-preference-tool-client.js';
import {createOwnerPreferenceExecution} from '../src/tools/owner-preference-execution.js';
import {createPreferenceCommand,PROCEDURE_EXECUTION_GUIDE} from '../src/tools/preference-command.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_DIRECT_PROCEDURE: '1',
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
      readBearer: string;
    };
    return { child, ...parsed, providerThreadId:parsed.identity.route_id, bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp` };
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
const usage={inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
const sourceAuthority={read:async()=>({version:2 as const,complete:false,revision:null,bridge_sources:[],google_sources:[]})};
for(const {syncFault,priorities,contextSection} of [{syncFault:false,priorities:false,contextSection:null},{syncFault:true,priorities:false,contextSection:null},{syncFault:false,priorities:true,contextSection:null},{syncFault:false,priorities:false,contextSection:'About' as const},{syncFault:false,priorities:false,contextSection:'People' as const}])test(`original Feedback ${contextSection?contextSection+' context':priorities?'Priorities replacement':'direct rule'} review/yes appends once; ${syncFault?'post-write sync fault held':'lost receipt recovered'}; fresh Ask uses actual owner-only master context`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-direct-procedure-'));
 let journal=new OperationalJournal(join(folder,'operations.sqlite'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,runtime:ReturnType<typeof createSottoAgentRuntime>|undefined,coordinator:ConversationCoordinator|undefined;
 let phase='review',step=0,applies=0,hold=true,lose=!syncFault;const sent:string[]=[];const rule=contextSection==='About'?'I run Cedar.':contextSection==='People'?'Dana is my cofounder.':priorities?'Fundraising\nHiring':'Never book me Fridays.';
 const reviewText=contextSection?`Add to your standing context (${contextSection}):\n\n${rule}\n\nSave this context?`:priorities?`Replace your current priorities (complete set):\n\nCurrent:\nNone\n\nNew:\n${rule}\n\nReplace these priorities?`:`Add to your standing rules (Procedures):\n\n${rule}\n\nSave this rule?`;
 const acknowledgement=contextSection?`Standing context saved (${contextSection}): ${rule}`:priorities?'Current priorities saved.':`Standing rule saved: ${rule}`;
 const mount=async()=>{
  const client=createOwnerPreferenceToolClient({...receiver!,fetch:async(input,init)=>{
   const body=JSON.parse(String(init!.body));if(body.operation==='inspect'&&hold)throw Error('inspection held');
   const response=await fetch(input,init);
   assert.equal(response.status,200,await response.clone().text());
   if(body.operation==='apply'){applies++;if(syncFault)assert.equal((await response.clone().json()).status,'unknown');if(lose){await response.body?.cancel();throw Error('lost committed response');}}
   return response;
  }});
  const command=createPreferenceCommand({execution:createOwnerPreferenceExecution({client,journal:()=>journal,sourceAuthority})});
  const tool=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerPreference:ctx?.get('allowOwnerPreference'),sourceRevision:ctx?.get('sourceRevision'),signal})});
  const model=new MockLanguageModelV4({doGenerate:async request=>{
   if(phase==='safe')return {content:[{type:'text' as const,text:'Hello safely.'}],finishReason:{unified:'stop' as const,raw:'STOP'},usage,warnings:[]};
   const calls=phase==='review'?[{name:'skill',input:{name:'sotto-feedback'}},{name:'run_sotto_script',input:{id:'master-file',args:[priorities?'set':'append','--section',contextSection??(priorities?'Priorities':'Procedures'),'--text',rule]}},null]:[{name:'run_sotto_script',input:{id:'master-file',args:contextSection?['confirm-context','--section',contextSection]:[priorities?'confirm-priorities':'confirm-procedure']}},null];
   const call=calls[step++];
   if(phase==='confirm'&&step===1){const prompt=JSON.stringify(request.prompt);assert.ok(prompt.includes('Accepted standing-rule review for this owner confirmation'),'exact accepted review survives unavailable-source history purge');assert.ok(rule.split('\n').every(line=>prompt.includes(line)));assert.ok(!prompt.includes(JSON.stringify('Remember: '+rule).slice(1,-1)),'old owner history was purged rather than retained as authority');}
   if(phase==='review'&&step===2)assert.ok(JSON.stringify(request.prompt).includes('Adding to your standing file'),'original Feedback instructions loaded');
   return {content:call?[{type:'tool-call' as const,toolCallId:`direct-${phase}-${step}`,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text' as const,text:'done'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory-'+Math.random()+'.db'),instructions:PROCEDURE_EXECUTION_GUIDE,skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,chatTools:()=>({run_sotto_script:tool})});
  coordinator=new ConversationCoordinator({journal,ownerPreferenceTool:client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver!.identity.owner_id},model:{generate:request=>{if(phase==='confirm')assert.equal(request.acceptedProcedureReview?.text,reviewText);if(phase==='safe')assert.equal(request.acceptedProcedureReview,undefined);return runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'});}},postPrepared:async request=>{sent.push(request.text);if(request.text===reviewText)assert.equal(request.verbatim,true);return {acceptance:'accepted',messageId:`reply-${sent.length}`};}});
 };
 const receive=(id:string,text:string)=>coordinator!.receive({channel:'photon',accountId:'account',senderId:receiver!.identity.owner_id,isDirect:true,eventId:id,text},{threadId:receiver!.providerThreadId,destination:'photon',lineKey:'line'});
 try{
  receiver=await startReceiver(join(folder,'receiver'));await mount();
  assert.equal((await receive('rule','Remember: '+rule)).status,'accepted');
  assert.equal(sent[0],reviewText);assert.equal(applies,0);assert.equal(existsSync(join(folder,'receiver/knowledge/master.md')),false);
  if(syncFault)writeFileSync(join(folder,'receiver/direct-master-sync-fail'),'1');
  phase='confirm';step=0;assert.equal((await receive('yes','yes')).status,'held');assert.equal(applies,1);
  assert.match(sent.at(-1)!,/^I couldn’t confirm whether the preference change happened\./,'the unknown preference gets an immediate notice');
  assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_preference_tool' ORDER BY rowid DESC LIMIT 1").get()!.status,'unknown','notice acceptance does not settle the effect');
  const masterPath=join(folder,'receiver/knowledge/master.md'),saved=readFileSync(masterPath,'utf8');assert.equal(saved.split(rule).length-1,1);
  phase='safe';assert.equal((await receive('hello','Hello safely')).status,'accepted');assert.equal(sent.at(-1),'Hello safely.');assert.match(sent.at(-2)!,/^I couldn’t confirm whether the preference change happened\./,'the notice arrived before fresh safe chat');assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the preference change happened.')).length,1);
  await coordinator!.close();await runtime!.close();runtime=undefined;await stopReceiver(receiver.child);journal.close();
  journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_preference_tool' AND status='unknown'").run();receiver=await startReceiver(join(folder,'receiver'));hold=false;lose=false;await mount();await coordinator!.recover();
  assert.equal(sent.at(-1),syncFault ? 'Hello safely.' : acknowledgement);
  assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_preference_tool' ORDER BY rowid DESC LIMIT 1").get()!.status,syncFault?'unknown':'succeeded','only an exact committed receipt settles the preference');
  assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the preference change happened.')).length,1,'an unresolved sync fault does not repeat the accepted notice');assert.equal(applies,1);assert.equal(readFileSync(masterPath,'utf8'),saved);
  await receive('consumed-confirmation','yes');assert.equal(applies,1);assert.equal(readFileSync(masterPath,'utf8'),saved);
  await coordinator!.close();await runtime!.close();runtime=undefined;
  let reads=0;const knowledge=createKnowledgeCommand({...receiver,sourceAuthority,isCurrent:(runId,eventId)=>journal.isRunCurrent(runId)&&journal.getRun(runId)?.event_id===eventId,fetch:async(input,init)=>{reads++;return fetch(input,init);}});
  let freshStep=0;const freshModel=new MockLanguageModelV4({doGenerate:async request=>{
   assert.ok(rule.split('\n').every(line=>JSON.stringify(request.prompt).includes(line)),'actual uncached master reader injects rule in brand-new memory');
   assert.match(JSON.stringify(request.prompt), /Current (?:local date and time: [A-Za-z]+, |UTC instant: )\d{4}-\d{2}-\d{2}T/,'actual receiver injects fresh clock into model instructions');
   const call=freshStep++===0?{name:'skill',input:{name:'sotto-ask'}}:null;
   return {content:call?[{type:'tool-call' as const,toolCallId:'fresh-ask',toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text' as const,text:'Friday is excluded by your saved standing rule.'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model:freshModel,storagePath:join(folder,'brand-new-memory.db'),instructions:MASTER_CONTEXT_EXECUTION_GUIDE,skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,readMasterContext:async ctx=>(await knowledge({id:'master-file',args:['get'],mode:'chat'},ctx)).result as {text:string;chars:number;cap:number;prompt:string}});
  coordinator=new ConversationCoordinator({journal,ownerPreferenceTool:createOwnerPreferenceToolClient(receiver),ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},model:{generate:req=>runtime!.generateChat({...req,threadId:'brand-new',ownerId:'owner'})},postPrepared:async req=>{sent.push(req.text);return {acceptance:'accepted',messageId:'fresh-answer'};}});
  assert.equal((await receive('fresh','Would Friday work?')).status,'accepted');assert.equal(reads,1);assert.equal(sent.at(-1),'Friday is excluded by your saved standing rule.');assert.equal(readFileSync(masterPath,'utf8'),saved);assert.equal(applies,1);
 }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('original weekly priority CTA adds once without review; lost receipt reopens inspect-only and fresh context reads it',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-priority-cta-'));
 let journal=new OperationalJournal(join(folder,'operations.sqlite'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,runtime:ReturnType<typeof createSottoAgentRuntime>|undefined,coordinator:ConversationCoordinator|undefined;
 let phase='cta',step=0,applies=0,previews=0,hold=true,lose=true,reads=0;const sent:string[]=[];
 const text='Staying in touch with Maya';
 const mount=async()=>{
  const client=createOwnerPreferenceToolClient({...receiver!,fetch:async(input,init)=>{
   const body=JSON.parse(String(init!.body));if(body.operation==='preview')previews++;
   if(body.operation==='inspect'&&hold)throw Error('inspection held');
   const response=await fetch(input,init);assert.equal(response.status,200,await response.clone().text());
   if(body.operation==='apply'){applies++;if(lose){await response.body?.cancel();throw Error('lost committed response');}}
   return response;
  }});
  const command=createPreferenceCommand({execution:createOwnerPreferenceExecution({client,journal:()=>journal,sourceAuthority})});
  const tool=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerPreference:ctx?.get('allowOwnerPreference'),sourceRevision:ctx?.get('sourceRevision'),signal})});
  const knowledge=createKnowledgeCommand({...receiver!,sourceAuthority,isCurrent:(runId,eventId)=>journal.isRunCurrent(runId)&&journal.getRun(runId)?.event_id===eventId,fetch:async(input,init)=>{reads++;return fetch(input,init);}});
  const model=new MockLanguageModelV4({doGenerate:async request=>{
   if(phase==='fresh'){assert.ok(JSON.stringify(request.prompt).includes(text));return {content:[{type:'text' as const,text:'Staying in touch with Maya is a current priority.'}],finishReason:{unified:'stop' as const,raw:'STOP'},usage,warnings:[]};}
   if(phase==='safe')return {content:[{type:'text' as const,text:'Hello safely.'}],finishReason:{unified:'stop' as const,raw:'STOP'},usage,warnings:[]};
   const calls=[{name:'skill',input:{name:'sotto-feedback'}},{name:'run_sotto_script',input:{id:'master-file',args:['prioritize','--text',text]}},null];const call=calls[step++];
   return {content:call?[{type:'tool-call' as const,toolCallId:`cta-${step}`,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text' as const,text:'done'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory-'+Math.random()+'.db'),instructions:PROCEDURE_EXECUTION_GUIDE,skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,chatTools:()=>({run_sotto_script:tool}),...(phase==='fresh'?{readMasterContext:async(ctx:Parameters<typeof knowledge>[1])=>(await knowledge({id:'master-file',args:['get'],mode:'chat'},ctx)).result as {text:string;chars:number;cap:number;prompt:string}}:{})});
  coordinator=new ConversationCoordinator({journal,ownerPreferenceTool:client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver!.identity.owner_id},model:{generate:req=>runtime!.generateChat({...req,threadId:'memory',ownerId:'owner'})},postPrepared:async req=>{sent.push(req.text);return {acceptance:'accepted',messageId:'cta-reply-'+sent.length};}});
 };
 const receive=(id:string,ownerText:string)=>coordinator!.receive({channel:'photon',accountId:'account',senderId:receiver!.identity.owner_id,isDirect:true,eventId:id,text:ownerText},{threadId:receiver!.providerThreadId,destination:'photon',lineKey:'line'});
 try{
  receiver=await startReceiver(join(folder,'receiver'));await mount();
  assert.equal((await receive('cta','prioritize staying in touch with Maya')).status,'held');
  assert.equal(previews,0);assert.equal(applies,1);
  assert.match(sent.at(-1)!,/^I couldn’t confirm whether the preference change happened\./,'the unknown preference gets an immediate notice');
  assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_preference_tool' ORDER BY rowid DESC LIMIT 1").get()!.status,'unknown','notice acceptance does not settle the effect');
  const masterPath=join(folder,'receiver/knowledge/master.md'),master=readFileSync(masterPath,'utf8');assert.equal(master.split(text).length-1,1);
  phase='safe';assert.equal((await receive('safe','Hello safely')).status,'accepted');assert.equal(sent.at(-1),'Hello safely.');assert.match(sent.at(-2)!,/^I couldn’t confirm whether the preference change happened\./,'the notice arrived before fresh safe chat');assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the preference change happened.')).length,1);
  await coordinator!.close();await runtime!.close();runtime=undefined;await stopReceiver(receiver.child);journal.close();
  journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_preference_tool' AND status='unknown'").run();receiver=await startReceiver(join(folder,'receiver'));hold=false;lose=false;await mount();await coordinator!.recover();
  assert.equal(sent.at(-1),'Prioritized staying in touch with Maya.');assert.equal(applies,1);assert.equal(previews,0);assert.equal(readFileSync(masterPath,'utf8'),master);
  await coordinator!.close();await runtime!.close();runtime=undefined;phase='fresh';await mount();
  assert.equal((await receive('fresh','What are my current priorities?')).status,'accepted');assert.equal(reads,1);assert.equal(applies,1);assert.equal(readFileSync(masterPath,'utf8'),master);
 }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});
