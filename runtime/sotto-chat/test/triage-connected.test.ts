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
import { createOwnerGmailDraftClient } from '../src/tools/owner-gmail-draft-client.js';
import { createOwnerGmailDraftTools } from '../src/tools/owner-gmail-draft-tool.js';
import { createGoogleReadTools } from '../src/agent/google-read.js';
import { DatabaseSync } from 'node:sqlite';
import {createKnowledgeCommand,TRIAGE_EXECUTION_GUIDE} from '../src/tools/knowledge-command.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import {readSkillIndex} from '../src/tools/skills.js';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';
async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_TRIAGE: '1', SOTTO_HOME: join(folder, 'sotto-home'), SOTTO_ROUTINE_EPOCH: 'e'.repeat(32), SOTTO_TEST_ROUTINE_SOURCE_REVISION: 'a'.repeat(64), SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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
      readBearer: string;
      identity: OwnerToolIdentity;
      bearer: string;
    };
    return { child, ...parsed, bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp` };
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
const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),
  bridge_sources:['imessage' as const,'whatsapp' as const,'contacts' as const],google_sources:['gmail' as const]})};

test('original triage gathers real queue; selected exact Gmail review saves once and fresh queue remains pending',async(t)=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-triage-connected-'));
  let journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
  let coordinator:ConversationCoordinator|undefined;
  let phase:'queue'|'review'|'save'|'safe'|'remaining'='queue',step=0;
  let loseResponse=true,holdInspect=true,applies=0;
  const sent:string[]=[];
  const body='I reviewed the cedar résumé. Thank you.';
  const capturedQueues:Record<string,any>[]=[];
  const http:Record<string,any>[]=[];
  try{
    receiver=await startReceiver(join(folder,'receiver'));
    const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:receiver.identity.owner_id};
    const context={threadId:receiver.identity.route_id,destination:receiver.identity.owner_id,lineKey:'line'};
    const incoming=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:receiver!.identity.owner_id,isDirect:true,eventId,text});
    const clientOptions=()=>({...receiver!,fetch:async(input:Parameters<typeof fetch>[0],init?:RequestInit)=>{
      const request=JSON.parse(init!.body as string);http.push({path:new URL(String(input)).pathname,...request});
      if(request.operation==='inspect'&&holdInspect)return new Response('{}',{status:503});
      const response=await fetch(input,init);
      if(request.operation==='apply'){
        applies++;assert.equal(response.status,200);
        if(loseResponse){await response.text();throw Error('synthetic response loss after committed save');}
      }
      return response;
    }});
    const make=async()=>{
      const client=createOwnerGmailDraftClient(clientOptions());
      const drafts=createOwnerGmailDraftTools({client,journal:()=>journal,sourceAuthority});
      const reads=createGoogleReadTools({...receiver!,sourceAuthority,fetch:async(input,init)=>{
        http.push(JSON.parse(String(init?.body)));return fetch(input,init);
      }});
      const command=createKnowledgeCommand({...receiver!,sourceAuthority,
        isCurrent:(runId,eventId)=>journal.isRunCurrent(runId)&&journal.getRun(runId)?.event_id===eventId});
      const script=createRunScriptTool({mode:'chat',commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),
        ownerRequestKey:ctx?.get('ownerRequestKey'),sourceRevision:ctx?.get('sourceRevision'),signal}),
        executeCommand:async(invocation,ctx)=>{const result=await command(invocation,ctx);capturedQueues.push(result.result as Record<string,any>);return result;}});
      const model=new MockLanguageModelV4({doGenerate:async request=>{
        const prompt=JSON.stringify(request.prompt);
        const phases={queue:[{name:'skill',args:{name:'sotto-triage'}},{name:'run_sotto_script',args:{id:'triage-queue',args:[]}},null],
          review:[{name:'skill',args:{name:'sotto-draft-reply'}},
            {name:'gmail_get',args:{message_id:phase==='review'?capturedQueues[0]!.email[0].message_id:''}},{name:'gmail_draft_propose',args:{message_id:'message-123',body}},null],
          save:[{name:'gmail_draft_save',args:{}},null],safe:[null],remaining:[{name:'run_sotto_script',args:{id:'triage-queue',args:[]}},null]};
        if(phase==='queue'&&step===1){
          assert.ok(prompt.includes('run_sotto_script triage-queue'));
          const system=JSON.stringify(request.prompt.filter(message=>message.role==='system'));
          const skillResult=JSON.stringify(request.prompt.flatMap(message=>message.role==='tool'?message.content:[])
            .filter(part=>part.type==='tool-result'&&part.toolName==='skill'));
          for(const delivered of [system,skillResult]){
            assert.ok(delivered.includes('In the first item, introduce this as a recent-message pass.'));
            assert.ok(delivered.includes('not the total inbox'));
            assert.ok(delivered.includes('sources reported partial or unavailable in the returned coverage'));
            assert.ok(delivered.includes('Before drafting for every next item, read that exact item'));
            assert.ok(delivered.includes('if the next item’s exact queue identity/context is unavailable this turn, rerun triage-queue'));
            assert.ok(delivered.includes('clarify if that selection is ambiguous'));
            assert.ok(delivered.includes('Queue snippets and the previous item’s draft are not enough'));
            assert.ok(delivered.includes('unless the actual material is available and included'));
            assert.ok(delivered.includes('otherwise briefly ask for it or offer to prepare it'));
          }
        }
        if(phase==='queue'&&step===2){assert.ok(prompt.includes('Planning group'));assert.ok(prompt.includes('Cedar review'));}
        if(phase==='review'&&step===1)assert.ok(prompt.includes('run_sotto_script style-apply'));
        if(phase==='review'&&step===2){
          const toolResult=request.prompt.flatMap(message=>message.role==='tool'?message.content:[])
            .find(part=>part.type==='tool-result'&&part.toolName==='gmail_get');
          assert.ok(toolResult);const fetched=JSON.parse(JSON.stringify(toolResult));
          assert.ok(JSON.stringify(fetched).includes(capturedQueues[0]!.email[0].threadId));
          assert.ok(prompt.includes('Please review the cedar packet.'));
        }
        const call=phases[phase][step++];
        return {content:call?[{type:'tool-call' as const,toolCallId:`triage-${phase}-${step}`,toolName:call.name,input:JSON.stringify(call.args)}]
          :[{type:'text' as const,text:phase==='queue'?'Alice’s Cedar review needs you first. There is also a planning group; reply in the app. Which item should we draft?'
            :phase==='remaining'?'Your Gmail draft was saved, not sent. Alice’s thread still needs a reply in the current queue.'
              :phase==='safe'?'Safe unrelated chat.':'model completed'}],
          finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
      }});
      runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:TRIAGE_EXECUTION_GUIDE,
        skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,
        chatTools:()=>({run_sotto_script:script,...reads,...drafts})});
      coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerGmailDraft:client,
        model:{generate:r=>runtime!.generateChat({...r,threadId:'memory',ownerId:'owner'})},canPostPrepared:r=>runtime!.canDeliverReplySource(r),
        postPrepared:async r=>{sent.push(r.text);return {acceptance:'accepted',messageId:`triage-answer-${sent.length}`};}});
      return client;
    };
    let client=await make();
    assert.equal((await coordinator!.receive(incoming('triage','Triage my inbox and messages.'),context)).status,'accepted');
    assert.equal(capturedQueues.length,1);
    const queue=capturedQueues[0]!;
    assert.deepEqual(queue.email.map((row:Record<string,any>)=>row.threadId),['thread-123','thread-456']);
    assert.equal(queue.email[0].message_id,'message-123');
    assert.equal(queue.email[0].important,true);assert.equal(queue.email[0].unread,true);
    assert.ok(queue.imessage.some((row:Record<string,any>)=>row.name==='Jake Rosen'));
    const group=queue.imessage.find((row:Record<string,any>)=>row.is_group_chat);assert.ok(group);assert.equal(group.identifier,'');
    assert.ok(!JSON.stringify(queue).includes('8885551234'));assert.equal(queue.whatsapp.length,1);
    assert.equal(queue.coverage.gmail_query,'newer_than:1d');assert.equal(queue.coverage.local_since_hours,72);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
    phase='review';step=0;
    assert.equal((await coordinator!.receive(incoming('review','Draft a reply to Alice about the Cedar packet.'),context)).status,'accepted');
    assert.equal(sent.at(-1),`To: alice@example.com\nSubject: Re: Cedar review\n\n${body}\n\nSave as a Gmail draft? Nothing will be sent.`);
    const provider=join(folder,'receiver/provider-calls.jsonl');assert.equal(existsSync(provider),false);
    assert.equal(http.filter(row=>row.operation==='gmail_search').length,0,'selected queue message needs no model search round');
    assert.equal(http.filter(row=>row.operation==='gmail_get').length,1);
    phase='save';step=0;
    assert.equal((await coordinator!.receive(incoming('save','Yes, please.'),context)).status,'held');
    assert.match(sent.at(-1)!,/^I couldn’t confirm whether the Gmail action happened\./,'original save request sends an honest notice before another owner turn');
    assert.equal(applies,1);
    const call=JSON.parse(readFileSync(provider,'utf8').trim());assert.equal(call.operation,'drafts.create');
    assert.equal(call.args.body.message.threadId,'thread-123');
    const mime=Buffer.from(call.args.body.message.raw,'base64url').toString();assert.match(mime,/To: alice@example.com/);
    assert.match(mime,/In-Reply-To: <cedar@example.com>/);assert.equal(Buffer.from(mime.split('\n\n')[1]!.replace(/\s/g,''),'base64').toString(),body);
    const ledger=join(folder,'receiver/events/owner-gmail-drafts.json');const savedBytes=readFileSync(ledger,'utf8');
    await coordinator!.close();await runtime!.close();journal.close();await stopReceiver(receiver.child);
    receiver=await startReceiver(join(folder,'receiver'));journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();
    loseResponse=false;phase='safe';step=0;client=await make();
    assert.equal((await coordinator!.receive(incoming('safe','Tell me a joke.'),context)).status,'accepted');
    assert.equal(sent.at(-1),'Safe unrelated chat.','unrelated chat continues after the original uncertainty notice');
    assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the Gmail action happened.')).length,1);
    holdInspect=false;const resumedAt=Date.now()+61_000;t.mock.method(Date,'now',()=>resumedAt);
    const beforeReceipt=sent.length;await coordinator!.recover();
    assert.equal(sent.length,beforeReceipt+1);assert.equal(sent.at(-1),'Saved your Gmail draft. Nothing was sent.');
    await coordinator!.recover();assert.equal(sent.length,beforeReceipt+1,'late draft receipt acknowledged once');t.mock.restoreAll();assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the Gmail action happened.')).length,1);
    assert.equal((await coordinator!.receive(incoming('save','Yes, please.'),context)).status,'duplicate');assert.equal(applies,1);
    assert.equal(readFileSync(provider,'utf8').trim().split('\n').length,1);assert.equal(readFileSync(ledger,'utf8'),savedBytes);
    phase='remaining';step=0;
    assert.equal((await coordinator!.receive(incoming('remaining','What is still waiting on a reply?'),context)).status,'accepted');
    assert.equal(capturedQueues.length,2);assert.deepEqual(capturedQueues[1]!.email,queue.email);
    assert.match(sent.at(-1)!,/saved, not sent/);assert.equal(applies,1);
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind!='owner_gmail_draft_tool'").get()!.n,0);
    const work=join(folder,'receiver/events/work.sqlite3');if(existsSync(work)){const db=new DatabaseSync(work,{readOnly:true});try{assert.equal(db.prepare('SELECT count(*) AS n FROM jobs').get()!.n,0);}finally{db.close();}}
    const external=readFileSync(join(folder,'receiver/triage-http.jsonl'),'utf8');assert.ok(external.includes('newer_than:1d'));assert.ok(!external.includes('/attachments/'));
    assert.equal(readFileSync(join(folder,'receiver/triage-bridge.jsonl'),'utf8').trim().split('\n').length,2);
    assert.ok(http.some(row=>row.operation==='inspect'));assert.equal(client.identity.owner_id,ownerPolicy.ownerId);
  }finally{await coordinator?.close();await runtime?.close();journal.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
