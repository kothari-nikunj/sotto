import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync, readFileSync, readdirSync} from 'node:fs';
import {once} from 'node:events';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
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
      SOTTO_TEST_FOLLOWUP: '1', SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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

import {createOwnerGmailDraftClient} from '../src/tools/owner-gmail-draft-client.js';
import {createOwnerGmailDraftTools,GMAIL_DRAFT_EXECUTION_GUIDE} from '../src/tools/owner-gmail-draft-tool.js';
const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['gmail' as const],connector_sources:['granola' as const]})};
async function execute(child:ChildProcessWithoutNullStreams){
  return new Promise<Record<string,unknown>>((done,fail)=>{
    let output='';const timer=setTimeout(()=>fail(new Error('followup worker timeout')),25000);
    child.once('exit',code=>{clearTimeout(timer);fail(new Error('fixture exited '+code));});
    const listener=(chunk:Buffer)=>{output+=String(chunk);for(const line of output.split('\n')){try{const result=JSON.parse(line);if('executed'in result){clearTimeout(timer);child.stdout.off('data',listener);done(result);}}catch{/* bounded fixture diagnostics are not JSON receipts */}}};
    child.stdout.on('data',listener);child.stdin.write('execute-followup\n');
  });
}
test('original followup queued pipeline captures loops and accepted draft; full new-email review, separate save, lost response reopen once',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-followup-http-')),data=join(folder,'receiver');
 let journal=new OperationalJournal(join(folder,'operations.sqlite'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,runtime:ReturnType<typeof createSottoAgentRuntime>|undefined,coordinator:ConversationCoordinator|undefined;
 try{
  receiver=await startReceiver(data);
  const skillClient=createOwnerSkillRequestClient(receiver);
  let lost=false,blocked=false,applies=0;let lastPreview:unknown;let offerId='';
  let gmailClient=createOwnerGmailDraftClient({...receiver,fetch:async(input,init)=>{
    const body=JSON.parse(init!.body as string);
    if(body.operation==='inspect'&&blocked)throw new Error('inspect unavailable');
    const response=await fetch(input,init);
    if(body.operation==='preview'){lastPreview=await response.clone().json();if((lastPreview as {status:string}).status==='offers'){assert.equal((lastPreview as {offers:unknown[]}).offers.length,1);offerId=(lastPreview as {offers:{offer_id:string}[]}).offers[0]!.offer_id;}}
    if(body.operation==='apply'){applies++;assert.equal(response.status,200);if(lost){await response.body?.cancel();throw new Error('committed answer lost');}}
    return response;
  }});
  const command=createSkillCommand(createOwnerSkillExecution({client:skillClient,journal:()=>journal,sourceAuthority}));
  const script=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerSkill:ctx?.get('allowOwnerSkill'),sourceRevision:ctx?.get('sourceRevision'),signal})});
  let step=0;const calls:({name:string;args:Record<string,unknown>}|null)[]=[{name:'skill',args:{name:'sotto-followup'}},{name:'run_sotto_script',args:{id:'followup',args:[]}},null,{name:'skill',args:{name:'sotto-draft-reply'}},{name:'gmail_draft_followup_list',args:{}},{name:'gmail_draft_followup_review',args:{}},null,{name:'gmail_draft_save',args:{}},null];
  const model=new MockLanguageModelV4({doGenerate:async request=>{
    const prompt=JSON.stringify(request.prompt);
    if(step===1)assert.ok(prompt.includes('Post-meeting follow-up'));
    const call=calls[step++];if(call?.name==='gmail_draft_followup_review')call.args={offer_id:offerId};
    return {content:call?[{type:'tool-call' as const,toolCallId:'followup-'+step,toolName:call.name,input:JSON.stringify(call.args)}]:[{type:'text' as const,text:'ordinary completion'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
  }});
  const tools=()=>{const mounted=createOwnerGmailDraftTools({client:gmailClient,journal:()=>journal,sourceAuthority});const original=mounted.gmail_draft_followup_list.execute!;mounted.gmail_draft_followup_list.execute=async(input,context)=>{const result=await original(input,context);if(result && typeof result==='object' && 'offers' in result)offerId=(result as {offers:{offer_id:string}[]}).offers[0]!.offer_id;return result;};return mounted;};
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:SHARED_SKILL_EXECUTION_GUIDE+GMAIL_DRAFT_EXECUTION_GUIDE,skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(x=>x.path),sourceAuthority,chatTools:()=>({run_sotto_script:script,...tools()})});
  const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:receiver.identity.owner_id},context={threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'};
  const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:ownerPolicy.ownerId,isDirect:true,eventId,text});
  const sent:string[]=[];
  const make=(safe=false)=>new ConversationCoordinator({journal,ownerPolicy,ownerSkillRequest:skillClient,ownerGmailDraft:gmailClient,model:{generate:request=>safe?Promise.resolve('Safe hello'):runtime!.generateChat({...request,threadId:'followup-memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'ack-'+sent.length};}});
  coordinator=make();assert.equal((await coordinator.receive(inbound('followup','Follow up on my meetings'),context)).status,'accepted');assert.equal(sent[0],'Your follow-up is queued. It will arrive separately.',JSON.stringify(journal.db.prepare('SELECT result_json FROM actions').all()));
  const executed=await execute(receiver.child);assert.equal((executed.jobs as {status:string})?.status,'done',JSON.stringify(executed));
  assert.ok(existsSync(join(data,'synthetic-sends.jsonl')),JSON.stringify(executed));
  const sends=readFileSync(join(data,'synthetic-sends.jsonl'),'utf8');assert.ok(sends.includes('Here is the deck as promised.'));
  const modelBefore=readFileSync(join(data,'synthetic-model-calls.jsonl'),'utf8');assert.equal(modelBefore.trim().split('\n').length,1);
  const continuity=join(data,'knowledge/continuity');const files=readdirSync(continuity).map(name=>[name,readFileSync(join(continuity,name),'utf8')] as const);assert.ok(files.some(([,text])=>text.includes('send the deck')),JSON.stringify(readdirSync(join(data,'events/research-artifacts')).filter(x=>x.startsWith('followup-')).map(x=>JSON.parse(readFileSync(join(data,'events/research-artifacts',x),'utf8')))));
  step=3;assert.equal((await coordinator.receive(inbound('review','Show me the follow-up draft'),context)).status,'accepted');assert.ok(sent[1]!.includes('To: dana@example.com\nSubject: Deck follow-up\n\nHere is the deck as promised.'),JSON.stringify({sent,offerId,lastPreview,actions:journal.db.prepare('SELECT result_json FROM actions').all()}));
  step=7;lost=true;blocked=true;assert.equal((await coordinator.receive(inbound('yes','Yes'),context)).status,'held');assert.equal(applies,1);
  await coordinator.close();await runtime.close();runtime=undefined;await stopReceiver(receiver.child);receiver=await startReceiver(data);
  journal.close();journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();
  const reopened=createOwnerGmailDraftClient(receiver);gmailClient.call=async(operation,input,signal)=>{assert.equal(operation,'inspect');if(blocked)throw new Error('held');return reopened.call(operation,input,signal);};
  coordinator=make(true);await coordinator.recover();await coordinator.receive(inbound('hello','Hello safely'),context);assert.ok(sent.includes('Safe hello'));blocked=false;await coordinator.recover();
  await execute(receiver.child);assert.equal(readFileSync(join(data,'synthetic-sends.jsonl'),'utf8'),sends);assert.equal(readFileSync(join(data,'synthetic-model-calls.jsonl'),'utf8'),modelBefore);
  for(const[name,text]of files)assert.equal(readFileSync(join(continuity,name),'utf8'),text);
  const provider=readFileSync(join(data,'provider-calls.jsonl'),'utf8').trim().split('\n').map(x=>JSON.parse(x));assert.equal(provider.length,1);
  const raw=provider[0].args.body.message.raw;const mime=Buffer.from(raw,'base64url').toString();assert.match(mime,/To: dana@example.com/);assert.match(mime,/Subject: Deck follow-up/);assert.ok(mime.includes('Here is the deck as promised.'));assert.equal(provider[0].args.body.message.threadId,undefined);assert.ok(!mime.includes('In-Reply-To:')&&!mime.includes('References:'));assert.equal(applies,1);
 }finally{await coordinator?.close();await runtime?.close();journal.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
