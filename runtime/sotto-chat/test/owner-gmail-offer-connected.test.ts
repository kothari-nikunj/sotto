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
import {createOwnerPreferenceToolClient} from '../src/tools/owner-preference-tool-client.js';
import {createOwnerPreferenceExecution} from '../src/tools/owner-preference-execution.js';
import {createPreferenceCommand,PROCEDURE_EXECUTION_GUIDE} from '../src/tools/preference-command.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
async function startReceiver(folder: string, offerKind: 'loop' | 'meeting_prep' = 'loop') {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-offer-reply-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_GMAIL_OFFER: '1', SOTTO_TEST_OFFER_REPLY: '1', SOTTO_TEST_OFFER_KIND: offerKind,
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
for(const unthreaded of [false,true])test(`native accepted Gmail offer full review/save; loss/reopen inspect only; unthreaded=${unthreaded}`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-gmail-offer-http-'));
  let journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
  let coordinator:ConversationCoordinator|undefined;
  try{
    receiver=await startReceiver(join(folder,'receiver'));
    let lose=true,blocked=false,applies=0,previews=0; const diagnostic:unknown[]=[];
    const client=createOwnerGmailDraftClient({...receiver,fetch:async(input,init)=>{
      const body=JSON.parse(String(init!.body));
      if(body.operation==='inspect'&&blocked)throw Error('inspection held');
      const response=await fetch(input,init);
      if(body.operation==='preview'){previews++;diagnostic.push({status:response.status,value:await response.clone().json()});}
      if(body.operation==='apply'){applies++;assert.equal(response.status,200);if(lose){await response.body?.cancel();throw Error('lost committed response');}}
      return response;
    }});
    const prep=createOwnerMeetingPrepClient(receiver);
    const preferenceClient=createOwnerPreferenceToolClient(receiver);
    const command=createPreferenceCommand({execution:createOwnerPreferenceExecution({client:preferenceClient,journal:()=>journal,sourceAuthority})});
    const facade=createRunScriptTool({mode:'chat',commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerPreference:ctx?.get('allowOwnerPreference'),sourceRevision:ctx?.get('sourceRevision'),signal}),executeCommand:command});
    const tools=createOwnerGmailDraftTools({client,journal:()=>journal,sourceAuthority});
    let step=0;
    const calls=[{name:'skill',input:{name:'sotto-draft-reply'}},{name:'run_sotto_script',input:{id:'master-file',args:['accept-offer']}},{name:'gmail_draft_offer',input:{}},null,{name:'gmail_draft_save',input:{}},null];
    const model=new MockLanguageModelV4({doGenerate:async request=>{
      if(step===1)assert.ok(JSON.stringify(request.prompt).includes('A bare'));
      const call=calls[step++];return {content:call?[{type:'tool-call' as const,toolCallId:`offer-${step}`,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text' as const,text:'completed model turn'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
    }});
    runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:GMAIL_DRAFT_EXECUTION_GUIDE+'\n'+PROCEDURE_EXECUTION_GUIDE,
      skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,chatTools:()=>({...tools,run_sotto_script:facade})});
    const sent:string[]=[];
    const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:receiver.identity.owner_id};
    const context={threadId:receiver.providerThreadId,destination:'photon',lineKey:'line'};
    const inbound=(id:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:ownerPolicy.ownerId,isDirect:true,eventId:id,text});
    const digest='b'.repeat(64);
    if(unthreaded){journal.prepareDispatch({dispatchId:'accepted-offer',kind:'scheduled_text',destination:'photon',payload:'Want this in your Gmail drafts?',routeDigest:digest});journal.beginDispatch('accepted-offer');journal.settleDispatch('accepted-offer',1,'accepted',receiver.offerMessageId,[receiver.offerMessageId],Date.now()-1000);}
    coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerGmailDraft:client,ownerMeetingPrep:prep,ownerPreferenceTool:preferenceClient,routeDigestForThread:()=>digest,
      model:{generate:request=>runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);if(sent.length===1)assert.equal(request.verbatim,true);return {acceptance:'accepted',messageId:`review-${sent.length}`};}});
    const yes=unthreaded?inbound('offer-yes','Yes'):{...inbound('offer-yes','Yes'),replyBound:true,replyToMessageId:receiver.offerMessageId};
    const first=await coordinator.receive(yes,context,unthreaded?Date.now():undefined);
    assert.ok(['accepted','silent'].includes(first.status),JSON.stringify({first,step,diagnostic}));assert.match(sent[0]!,/Any word on the Cedar review/);assert.match(sent[0]!,/Save as a Gmail draft/);
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_preference_tool'").get()!.n,0);
    assert.equal(applies,0);assert.equal(previews,1);assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_meeting_prep_request'").get()!.n,0);
    blocked=true;
    const save={...inbound('save-yes','Yes'),replyBound:true,replyToMessageId:'review-1'};
    assert.equal((await coordinator.receive(save,context)).status,'held');
    assert.match(sent.at(-1)!,/^I couldn’t confirm whether the Gmail action happened\./,'uncertainty is reported before fresh owner input');
    const action=journal.db.prepare("SELECT * FROM actions WHERE kind='owner_gmail_draft_tool' AND status='unknown'").get()!;
    const original=JSON.parse(String(action.result_json)).args.proposal.origin;
    assert.ok(original.offer_id);assert.equal(original.selection.provider_message_id,'offer-yes');
    await coordinator.close();await runtime.close();runtime=undefined;await stopReceiver(receiver.child);
    receiver=await startReceiver(join(folder,'receiver'));journal.close();journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();
    const reopened=createOwnerGmailDraftClient(receiver);
    client.call=async(operation,input,signal)=>{assert.equal(operation,'inspect');if(blocked)throw Error('held');return reopened.call(operation,input,signal);};
    coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerGmailDraft:client,ownerMeetingPrep:createOwnerMeetingPrepClient(receiver),model:{generate:async request=>{assert.equal(request.text,'Hello safely');return 'safe unrelated chat';}},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`reply-${sent.length}`};}});
    await coordinator.receive(inbound('safe','Hello safely'),context);assert.equal(sent.at(-1),'safe unrelated chat');
    assert.match(sent.at(-2)!,/^I couldn’t confirm whether the Gmail action happened\./,'the accepted notice is not repeated after restart');
    assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the Gmail action happened.')).length,1);
    blocked=false;lose=false;
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE id=? AND status='unknown'").run(action.id);
    await coordinator.recover();
    assert.equal(sent.filter(text=>text==='Saved your Gmail draft. Nothing was sent.').length,1,
      'late exact proof acknowledges once without a second provider write');
    assert.equal(applies,1);const provider=readFileSync(join(folder,'receiver/provider-calls.jsonl'),'utf8').trim().split('\n');assert.equal(provider.length,1);
    const create=JSON.parse(provider[0]!);assert.equal(create.args.body.message.threadId,'connected-source-thread');
    const mime=Buffer.from(create.args.body.message.raw,'base64url').toString('utf8');assert.match(mime,/To: dana@outside.com/);assert.match(mime,/Any word on the Cedar review/);
    assert.equal((await coordinator.receive(save,context)).status,'duplicate');
  }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});
