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
import { createOwnerLoopClient } from '../src/tools/owner-loop-client.js';
import { createOwnerTaskTools } from '../src/tools/owner-task-tool.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { createHmac } from 'node:crypto';
import { RequestContext } from '@mastra/core/request-context';
import { createOwnerLoopsTool, readOwnerLoops } from '../src/agent/owner-loops.js';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';

async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_LOOP_CORRECTION:'1', SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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
test('original loops skill corrects source→them→you→resolved and dismissed; actual readback and lost response reopen once', {timeout:90000}, async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-source-loop-'));let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 let journal=new OperationalJournal(join(folder,'operations.sqlite'));let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
 let coordinator:ConversationCoordinator|undefined;let loss=false,held=false,applies=0;const sent:string[]=[];
 const state={safe:false,phase:0,target:'source-loop',op:'retarget',direction:'them',correction:'Dana will send the deck',to:'resolved'};
 let row:any; const skills=(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(x=>x.path);
 const mount=()=>{
  const readBearer=createHmac('sha256','0123456789abcdef0123456789abcdef').update('sotto-mcp').digest('hex');
  const client=createOwnerTaskToolClient({...receiver!,readBearer,sourceAuthority,fetch:async(input,init)=>{
   const body=JSON.parse(String(init?.body));
   if(String(input).endsWith('/owner-loop-tool') && body.operation==='inspect' && held)return new Response('{}',{status:503});
   const result=await fetch(input,init);
   if(String(input).endsWith('/owner-loop-tool') && body.operation==='apply'){applies++;if(loss){loss=false;held=true;await result.body?.cancel();throw new Error('synthetic committed response lost');}}
   return result;
  }});
  const execution=createOwnerTaskExecution({client,journal:()=>journal,sourceAuthority});const command=createTaskCommand(execution);
  const read=createKnowledgeCommand({...receiver!,readBearer,sourceAuthority,isCurrent:(run,event)=>journal.isRunCurrent(run) && journal.getRun(run)?.event_id===event});
  const script=createRunScriptTool({mode:'chat',commandContext:(c,signal)=>({mode:c?.get('mode'),runId:c?.get('runId'),ownerRequestKey:c?.get('ownerRequestKey'),allowOwnerTask:c?.get('allowOwnerTask'),sourceRevision:c?.get('sourceRevision'),signal}),
   executeCommand:async(request,context)=>{
    if(request.id==='loops-query'){const result=await read(request,context);row=(result.result as any).you_owe.concat((result.result as any).waiting_on_them).find((r:any)=>r.anchor_key===state.target);return result;}
    return command(request,context);
   }});
  const model=new MockLanguageModelV4({doGenerate:async(options)=>{
   if(state.safe)return {content:[{type:'text',text:'Hello safely'}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};
   const phase=state.phase++;let call:{name:string;args:unknown}|undefined;
   if(phase===0)call={name:'skill',args:{name:'sotto-loops'}};
   if(phase===1){assert.match(JSON.stringify(options.prompt),/Applying explicit corrections/);call={name:'run_sotto_script',args:{id:'loops-query',args:[]}};}
   if(phase===2){assert.ok(row);assert.match(JSON.stringify(options.prompt),new RegExp(row.loop_identity));
    const args=['--op',state.op==='retarget'?'loop-retarget':'loop','--anchor',row.anchor_key,'--loop-version',row.loop_version,'--loop-identity',row.loop_identity,'--correction',state.correction,...(state.op==='retarget'?['--direction',state.direction]:['--to',state.to])];
    call={name:'run_sotto_script',args:{id:'knowledge-edit',args}};
   }
   return {content:call?[{type:'tool-call',toolCallId:'correction-'+state.correction+'-'+phase,toolName:call.name,input:JSON.stringify(call.args)}]:[{type:'text',text:'Tool result received'}],finishReason:{unified:call?'tool-calls':'stop',raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:TASK_CORRECTION_EXECUTION_GUIDE,skillPaths:skills,sourceAuthority,chatTools:()=>({run_sotto_script:script})});
  coordinator=new ConversationCoordinator({journal,model:{generate:request=>runtime!.generateChat({...request,threadId:'memory',ownerId:'memory-owner'})},ownerPolicy:{channel:'photon',accountId:'account',ownerId:'+15555550123'},ownerTaskTool:client,
   postPrepared:async post=>{sent.push(post.text);return {acceptance:'accepted',messageId:'synthetic-'+sent.length};},canPostPrepared:revision=>runtime!.canDeliverReplySource(revision)});
  return client;
 };
 const inbound=(id:string,text:string)=>({channel:'photon' as const,accountId:'account',ownerId:'+15555550123',eventId:id,text,threadId:receiver!.identity.route_id,senderId:'+15555550123',isDirect:true,isFromMe:false,isReceipt:false});
 try{
  receiver=await startReceiver(join(folder,'receiver'));let client=mount();
  for(const [id,direction,correction] of [['them','them','Dana will send the deck'],['you','you','I will send the deck']] as const){
   state.phase=0;state.direction=direction;state.correction=correction;
   const result=await coordinator!.receive(inbound(id,correction),{threadId:receiver!.identity.route_id,destination:'photon',lineKey:'line'});assert.equal(result.status,'accepted',JSON.stringify({result,sent}));
   assert.match(sent.at(-1)!,/currently available list/);const next=await client.readLoops('a'.repeat(64));
   const lane=direction==='you'?next.you_owe:next.waiting_on_them;const replacement=lane.find(r=>r.what==='send the deck');assert.ok(replacement);state.target=replacement.anchor_key;
  }
  state.phase=0;state.op='correct';state.to='resolved';state.correction='I already handled sending the deck';loss=true;
  assert.equal((await coordinator!.receive(inbound('resolved',state.correction),{threadId:receiver!.identity.route_id,destination:'photon',lineKey:'line'})).status,'held');assert.equal(applies,3);
  state.safe=true;await coordinator!.receive(inbound('safe','independent hello'),{threadId:receiver!.identity.route_id,destination:'photon',lineKey:'line'});assert.match(sent.at(-2)!,/^I couldn’t confirm whether /,'the first uncertain command gets one immediate notice');assert.match(sent.at(-1)!,/Hello safely/);state.safe=false;
  await coordinator!.close();await runtime!.close();await stopReceiver(receiver.child);journal.close();
  journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE status='unknown'").run();receiver=await startReceiver(join(folder,'receiver'));client=mount();held=false;
  await coordinator!.recover();assert.equal(applies,3);assert.match(sent.at(-1)!,/Marked done/);
  assert.ok(!(await client.readLoops('a'.repeat(64))).you_owe.some(r=>r.what==='send the deck'));
  state.phase=0;state.target='dismiss-loop';state.to='dismissed';state.correction='The packet is not a real obligation';
  assert.equal((await coordinator!.receive(inbound('dismissed',state.correction),{threadId:receiver!.identity.route_id,destination:'photon',lineKey:'line'})).status,'accepted');assert.match(sent.at(-1)!,/Obligation dismissed/);assert.equal(applies,4);
  assert.deepEqual((await client.readLoops('a'.repeat(64))).counts,{you_owe:0,waiting_on_them:0});
 }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});
