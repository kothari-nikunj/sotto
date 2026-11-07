import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn,type ChildProcessWithoutNullStreams} from 'node:child_process';
import {once} from 'node:events';
import {existsSync,readFileSync,mkdtempSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {MockLanguageModelV4} from 'ai/test';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {readSkillIndex} from '../src/tools/skills.js';
import {createOwnerGmailDraftClient} from '../src/tools/owner-gmail-draft-client.js';
import {createOwnerGmailDraftTools,GMAIL_DRAFT_EXECUTION_GUIDE} from '../src/tools/owner-gmail-draft-tool.js';
import {createOwnerMeetingPrepClient} from '../src/tools/owner-meeting-prep-client.js';
import {createKnowledgeCommand,MASTER_CONTEXT_EXECUTION_GUIDE} from '../src/tools/knowledge-command.js';
import {createOwnerPreferenceToolClient} from '../src/tools/owner-preference-tool-client.js';
import {createOwnerPreferenceExecution} from '../src/tools/owner-preference-execution.js';
import {createPreferenceCommand,PROCEDURE_EXECUTION_GUIDE} from '../src/tools/preference-command.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
async function startReceiver(folder: string, offerKind: 'loop' | 'meeting_prep' = 'loop') {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-offer-reply-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_PROCEDURE_OFFER: '1', SOTTO_TEST_OFFER_REPLY: '1', SOTTO_TEST_OFFER_KIND: offerKind,
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
      offerMessageId: string;
      providerThreadId: string;
      identity: OwnerToolIdentity;
      bearer: string;
      readBearer: string;
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
const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['calendar' as const,'gmail' as const]})};
for (const unthreaded of [false,true]) test(`native evening skill accepts actual composer procedure offer once; loss/reopen inspect; unthreaded=${unthreaded}`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-procedure-offer-'));
  let journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
  let coordinator:ConversationCoordinator|undefined;
  try {
    receiver=await startReceiver(join(folder,'receiver'));
    let apply=0, blocked=true;
    const client=createOwnerPreferenceToolClient({...receiver,fetch:async(input,init)=>{
      const body=JSON.parse(String(init!.body));
      if(body.operation==='inspect'&&blocked)throw Error('held inspection');
      const response=await fetch(input,init);
      if(body.operation==='apply'){apply++;assert.equal(response.status,200);await response.body?.cancel();throw Error('lost saved receipt');}
      return response;
    }});
    const command=createPreferenceCommand({execution:createOwnerPreferenceExecution({client,journal:()=>journal,sourceAuthority})});
    const tool=createRunScriptTool({mode:'chat',commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerPreference:ctx?.get('allowOwnerPreference'),sourceRevision:ctx?.get('sourceRevision'),signal}),executeCommand:command});
    let step=0;
    const calls=[{name:'skill',input:{name:'sotto-evening-brief'}},{name:'run_sotto_script',input:{id:'master-file',args:['accept-offer']}},null];
    const model=new MockLanguageModelV4({doGenerate:async request=>{
      if(step===1)assert.ok(JSON.stringify(request.prompt).includes('run_sotto_script master-file accept-offer'),'actual original evening skill loaded');
      const call=calls[step++];return {content:call?[{type:'tool-call' as const,toolCallId:`procedure-${step}`,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text' as const,text:'done'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:PROCEDURE_EXECUTION_GUIDE,skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,chatTools:()=>({run_sotto_script:tool})});
    const sent:string[]=[];
    const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:receiver.identity.owner_id};
    const context={threadId:receiver.providerThreadId,destination:'photon',lineKey:'line'};
    const inbound=(id:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:ownerPolicy.ownerId,isDirect:true,eventId:id,text});
    const digest='b'.repeat(64);
    if(unthreaded){journal.prepareDispatch({dispatchId:'accepted-procedure',kind:'scheduled_text',destination:'photon',payload:'Make this a standing rule?',routeDigest:digest});journal.beginDispatch('accepted-procedure');journal.settleDispatch('accepted-procedure',1,'accepted',receiver.offerMessageId,[receiver.offerMessageId],Date.now()-1000);}
    coordinator=new ConversationCoordinator({journal,ownerPolicy,routeDigestForThread:()=>digest,ownerPreferenceTool:client,ownerMeetingPrep:createOwnerMeetingPrepClient(receiver),model:{generate:request=>runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`reply-${sent.length}`};}});
    const yes=unthreaded?inbound('procedure-yes','Yes'):{...inbound('procedure-yes','Yes'),replyBound:true,replyToMessageId:receiver.offerMessageId};
    assert.equal((await coordinator.receive(yes,context,unthreaded?Date.now():undefined)).status,'held');
    assert.match(sent[0]!,/^I couldn’t confirm whether the preference change happened\./,'original request sends an honest notice before another owner turn');
    assert.equal(apply,1);
    const rule='Never book me Fridays.\nUse mornings instead.';
    const masterPath=join(folder,'receiver/knowledge/master.md');
    const master=readFileSync(masterPath,'utf8');assert.equal(master.split(rule).length-1,1);
    const contextResponse = new Promise<string>(resolveLine => {
      let buffer = '';
      const listener = (chunk: Buffer) => {buffer += String(chunk); const end = buffer.indexOf('\n');
        if (end >= 0) {receiver!.child.stdout.off('data',listener);resolveLine(buffer.slice(0,end));}};
      receiver!.child.stdout.on('data',listener);
    });
    receiver.child.stdin.write('master-context\n');
    assert.ok(JSON.parse(await contextResponse).master_context.includes(rule), 'next actual shared composer prompt includes saved rule');
    await coordinator.close();await runtime.close();runtime=undefined;await stopReceiver(receiver.child);journal.close();
    receiver=await startReceiver(join(folder,'receiver'));journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();
    const reopened=createOwnerPreferenceToolClient(receiver);
    client.call=async(operation,input,signal)=>{assert.equal(operation,'inspect');if(blocked)throw Error('held');return reopened.call(operation,input,signal);};
    coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerPreferenceTool:client,model:{generate:async request=>{assert.equal(request.text,'Hello safely');return 'safe chat';}},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`reply-${sent.length}`};}});
    await coordinator.receive(inbound('safe','Hello safely'),context);assert.equal(sent.at(-1),'safe chat');assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether')).length,1,'reopen and safe chat do not duplicate the original notice');
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE status='unknown'").run();blocked=false;await coordinator.recover();assert.equal(sent.filter(text=>text===`Standing rule saved: ${rule}`).length,1);
    assert.equal(readFileSync(masterPath,'utf8'),master);assert.equal(apply,1);
    assert.equal((await coordinator.receive(yes,context,unthreaded?Date.now():undefined)).status,'duplicate');
    await coordinator.close();
    let reads = 0;
    const knowledge = createKnowledgeCommand({...receiver,readBearer:receiver.readBearer,sourceAuthority,
      isCurrent:(runId,eventId)=>journal.isRunCurrent(runId) && journal.getRun(runId)?.event_id === eventId,
      fetch:async(input,init)=>{reads++;return fetch(input,init);}});
    const getTool = createRunScriptTool({mode:'chat',commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),sourceRevision:ctx?.get('sourceRevision'),standingContext:ctx?.get('standingContext'),signal}),executeCommand:knowledge});
    let freshStep=0;
    const freshCalls=[{name:'skill',input:{name:'sotto-ask'}},{name:'run_sotto_script',input:{id:'master-file',args:['get']}},null];
    const freshModel = new MockLanguageModelV4({doGenerate:async request=>{
      assert.ok(JSON.stringify(request.prompt).includes(rule.replace('\n','\\n')) || JSON.stringify(request.prompt).includes('Never book me Fridays.'),'fresh generation already has actual standing rule');
      const call=freshCalls[freshStep++];
      return {content:call?[{type:'tool-call' as const,toolCallId:`fresh-${freshStep}`,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text' as const,text:'Friday is excluded by your standing rule; use a morning instead.'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model:freshModel,storagePath:join(folder,'brand-new-memory.db'),instructions:MASTER_CONTEXT_EXECUTION_GUIDE,
      skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,
      readMasterContext:async ctx=>(await knowledge({id:'master-file',args:['get'],mode:'chat'},ctx)).result as {text:string;chars:number;cap:number;prompt:string},
      chatTools:()=>({run_sotto_script:getTool})});
    coordinator=new ConversationCoordinator({journal,ownerPolicy,model:{generate:request=>runtime!.generateChat({...request,threadId:'fresh-memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`fresh-${sent.length}`};}});
    await coordinator.receive(inbound('fresh-question','Would Friday be okay?'),context);
    assert.equal(sent.at(-1),'Friday is excluded by your standing rule; use a morning instead.');
    assert.equal(reads,1,'automatic standing read and same-turn explicit get share one snapshot');

  } finally {await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});
