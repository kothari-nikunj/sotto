import {readSkillIndex} from '../src/tools/skills.js';
import {createOwnerTaskExecution} from '../src/tools/owner-task-execution.js';
import {createTaskCommand,TASK_CORRECTION_EXECUTION_GUIDE} from '../src/tools/task-command.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import {createKnowledgeCommand,RETUNE_EXECUTION_GUIDE} from '../src/tools/knowledge-command.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { MockLanguageModelV4 } from 'ai/test';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator } from '../src/conversation.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createOwnerTaskToolClient } from '../src/tools/owner-task-tool-client.js';
import { createHmac } from 'node:crypto';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';

async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_LOOP_KEEP:'1', SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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
const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['gmail' as const]})};
for(const target of ['keep-you','keep-waiting'] as const)test(`original Loops protected keep ${target}; lost response restart inspects once and safe chat continues`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-loop-snooze-'));let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 let journal=new OperationalJournal(join(folder,'operations.sqlite'));let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
 let coordinator:ConversationCoordinator|undefined;let held=true,applies=0;const sent:string[]=[];let phase=0,safe=false,scanning=false;
 let row:{anchor_key:string;loop_version:string;loop_identity:string}|undefined;
 const correction=target==='keep-you'?'I still care about sending the Cedar deck, keep it':'I still care about the Cedar notes, keep waiting';
 const mount=()=>{
  const readBearer=createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex');
  const client=createOwnerTaskToolClient({...receiver!,readBearer,sourceAuthority,fetch:async(input,init)=>{
   const body=JSON.parse(String(init?.body));
   if(String(input).endsWith('/owner-loop-tool')&&body.operation==='inspect'&&held)throw new Error('inspection held');
   const response=await fetch(input,init);
   if(String(input).endsWith('/owner-loop-tool')&&body.operation==='apply'){applies++;assert.equal(response.status,200,await response.clone().text());await response.body?.cancel();throw new Error('committed response lost');}
   return response;
  }});
  const command=createTaskCommand(createOwnerTaskExecution({client,journal:()=>journal,sourceAuthority}));
  const read=createKnowledgeCommand({...receiver!,readBearer,sourceAuthority,isCurrent:(run,event)=>journal.isRunCurrent(run)&&journal.getRun(run)?.event_id===event});
  const script=createRunScriptTool({mode:'chat',commandContext:(c,signal)=>({mode:c?.get('mode'),runId:c?.get('runId'),ownerRequestKey:c?.get('ownerRequestKey'),allowOwnerTask:c?.get('allowOwnerTask'),sourceRevision:c?.get('sourceRevision'),signal}),executeCommand:async(request,context)=>{
   if(request.id==='retune-scan')return read(request,context);
   if(request.id==='loops-query'){const result=await read(request,context);const rows=result.result as {you_owe:typeof row[];waiting_on_them:typeof row[];parked:typeof row[]};row=[...rows.you_owe,...rows.waiting_on_them,...rows.parked].find(item=>item?.anchor_key===target);return result;}return command(request,context);
  }});
  const model=new MockLanguageModelV4({doGenerate:async options=>{
   if(safe)return {content:[{type:'text',text:'Hello safely'}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};
   const current=phase++;
   if(scanning){
    if(current===2){const prompt=JSON.stringify(options.prompt);assert.match(prompt,/currently_permitted_canonical_loops/);assert.match(prompt,/keep-waiting/);return {content:[{type:'text',text:'The Cedar notes are a stale waiting obligation. Would you like to keep, snooze or dismiss it?'}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};}
    const call=current===0?{name:'skill',args:{name:'sotto-loops'}}:{name:'run_sotto_script',args:{id:'retune-scan',args:[]}};return {content:[{type:'tool-call',toolCallId:'scan-'+current,toolName:call.name,input:JSON.stringify(call.args)}],finishReason:{unified:'tool-calls',raw:'STOP'},usage,warnings:[]};
   }
   let call:{name:string;args:unknown};
   if(current===0)call={name:'skill',args:{name:'sotto-loops'}};
   else if(current===1){assert.match(JSON.stringify(options.prompt),/Applying explicit corrections/);call={name:'run_sotto_script',args:{id:'loops-query',args:['--parked']}};}
   else {assert.ok(row);call={name:'run_sotto_script',args:{id:'retune-apply',args:['keep','--anchor',row.anchor_key,'--loop-version',row.loop_version,'--loop-identity',row.loop_identity,'--correction',correction]}};}
   return {content:[{type:'tool-call',toolCallId:'snooze-'+current,toolName:call.name,input:JSON.stringify(call.args)}],finishReason:{unified:'tool-calls',raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:TASK_CORRECTION_EXECUTION_GUIDE+RETUNE_EXECUTION_GUIDE,skillPaths:(readSkills),sourceAuthority,chatTools:()=>({run_sotto_script:script})});
  coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'+15555550123'},ownerTaskTool:client,model:{generate:request=>runtime!.generateChat({...request,threadId:'snooze-memory',ownerId:'owner'})},canPostPrepared:revision=>runtime!.canDeliverReplySource(revision),postPrepared:async post=>{sent.push(post.text);return {acceptance:'accepted',messageId:'snooze-ack-'+sent.length};}});
  return client;
 };
 const readSkills=(await readSkillIndex(process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'))).map(item=>item.path);
 const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:'+15555550123',isDirect:true,eventId,text});
 try{
  receiver=await startReceiver(join(folder,'receiver'));let client=mount();const context={threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'};
  if(target==='keep-waiting'){
   const dir=join(folder,'receiver/knowledge/continuity');const beforeScan=readdirSync(dir).map(name=>[name,readFileSync(join(dir,name),'utf8')] as const);
   scanning=true;assert.equal((await coordinator!.receive(inbound('scan','Help me tune up my stale loops'),context)).status,'accepted');
   assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);assert.equal(applies,0);
   for(const[name,text]of beforeScan)assert.equal(readFileSync(join(dir,name),'utf8'),text);
   scanning=false;phase=0;
  }
  assert.equal((await coordinator!.receive(inbound('snooze',correction),context)).status,'held');assert.equal(applies,1);
  const finalCurrent=await client.readLoops('a'.repeat(64),undefined,true);assert.ok([...finalCurrent.you_owe,...finalCurrent.waiting_on_them].some(item=>item.anchor_key===target));assert.ok(!finalCurrent.parked?.some(item=>item.anchor_key===target));
  safe=true;await coordinator!.receive(inbound('hello','Unrelated hello'),context);assert.match(sent.at(-2)!,/^I couldn’t confirm whether /,'the first uncertain command gets one immediate notice');assert.equal(sent.at(-1),'Hello safely');
  const directory=join(folder,'receiver/knowledge/continuity');const before=readdirSync(directory).map(name=>[name,readFileSync(join(directory,name),'utf8')] as const);
  const kept=before.find(([,text])=>text.includes('anchor_key: '+target))?.[1];assert.ok(kept);assert.ok(kept.includes('created_at:'));if(target==='keep-you')assert.ok(kept.includes('reopened_at:'));else for(const field of ['chased_count','chase_after','last_chased_at','chase_pending','handoff_asked_at','chase_stalls','last_heard_at'])assert.ok(!kept.includes(field+':'));
  await coordinator!.close();await runtime!.close();await stopReceiver(receiver.child);journal.close();
  journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE status='unknown'").run();receiver=await startReceiver(join(folder,'receiver'));client=mount();held=false;await coordinator!.recover();
  assert.equal(applies,1);assert.match(sent.at(-1)!,/^Kept this obligation\. Your currently available list/);assert.ok(!sent.at(-1)!.includes('Marked done'));
  for(const[name,text]of before)assert.equal(readFileSync(join(directory,name),'utf8'),text);
  const current=await client.readLoops('a'.repeat(64),undefined,true);assert.ok([...current.you_owe,...current.waiting_on_them].some(item=>item.anchor_key===target));assert.ok(!current.parked?.some(item=>item.anchor_key===target));
 }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});
