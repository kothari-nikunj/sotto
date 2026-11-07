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
import {createOwnerPreferenceToolClient} from '../src/tools/owner-preference-tool-client.js';
import {createOwnerPreferenceExecution,type OwnerPreferenceExecutionContext} from '../src/tools/owner-preference-execution.js';
import {createPreferenceCommand,USEFULNESS_EXECUTION_GUIDE} from '../src/tools/preference-command.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_USEFULNESS: '1',
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
const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['gmail' as const],connector_sources:[]})};
for(const syncFault of [false,true])test(`original Feedback rates an actual delivered item; ${syncFault?'post-write unknown held':'loss/reopen receipt recovered'}; original next brief receives narrow feedback`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-usefulness-'));
 let journal=new OperationalJournal(join(folder,'operations.sqlite'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,runtime:ReturnType<typeof createSottoAgentRuntime>|undefined,coordinator:ConversationCoordinator|undefined;
 let step=0,phase='rate',reference='',applies=0,hold=true,lose=!syncFault;const sent:string[]=[];
 const excerpt='School deadline: return the form by Friday.';
 const mount=async()=>{
  const client=createOwnerPreferenceToolClient({...receiver!,fetch:async(input,init)=>{
   const body=JSON.parse(String(init!.body));if(body.operation==='inspect'&&hold)throw Error('inspection held');
   const response=await fetch(input,init);assert.equal(response.status,200,await response.clone().text());
   if(body.operation==='apply'){applies++;if(syncFault)assert.equal((await response.clone().json()).status,'unknown');if(lose){await response.body?.cancel();throw Error('lost committed response');}}
   return response;
  }});
  const command=createPreferenceCommand({execution:createOwnerPreferenceExecution({client,journal:()=>journal,sourceAuthority})});
  const tool=createRunScriptTool({mode:'chat',commandGuide:'usefulness-feedback: list | useful/not_useful --reference REF [--excerpt TEXT] [--reason TEXT]',executeCommand:async(invocation,context)=>{
   const result=await command(invocation,context as OwnerPreferenceExecutionContext);
   if(invocation.args[0]==='list'){
    assert.equal(journal.db.prepare("SELECT count(*) n FROM actions WHERE kind='owner_preference_tool'").get()!.n,0);
    const listed=result.result as {status:string,items:Array<{reference:string,text:string}>};assert.equal(listed.status,'usefulness_list');assert.equal(listed.items.length,1);reference=listed.items[0]!.reference;assert.ok(listed.items[0]!.text.includes(excerpt));
   }
   return result;
  },commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerPreference:ctx?.get('allowOwnerPreference'),sourceRevision:ctx?.get('sourceRevision'),usefulnessList:ctx?.get('usefulnessList'),setUsefulnessList:(value:unknown)=>ctx?.set('usefulnessList',value),signal})});
  const model=new MockLanguageModelV4({doGenerate:async request=>{
   if(phase==='safe')return {content:[{type:'text' as const,text:'Hello safely.'}],finishReason:{unified:'stop' as const,raw:'STOP'},usage,warnings:[]};
   const prompt=JSON.stringify(request.prompt);
   const calls=[{name:'skill',input:{name:'sotto-feedback'}},{name:'run_sotto_script',input:{id:'usefulness-feedback',args:['list']}},{name:'run_sotto_script',input:{id:'usefulness-feedback',args:['useful','--reference',reference,'--excerpt',excerpt,'--reason','This saved me time.']}},null];
   const call=calls[step++];
   if(step===2)assert.ok(prompt.includes('run_sotto_script usefulness-feedback list'),'actual original Feedback instructions');
   if(step===3){assert.ok(reference);assert.ok(prompt.includes(excerpt),'actual list result reached attending model');}
   return {content:call?[{type:'tool-call' as const,toolCallId:'step-'+step,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text' as const,text:'normal completion'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory-'+Math.random()+'.sqlite'),instructions:USEFULNESS_EXECUTION_GUIDE,skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(item=>item.path),sourceAuthority,chatTools:()=>({run_sotto_script:tool})});
  coordinator=new ConversationCoordinator({journal,ownerPreferenceTool:client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver!.identity.owner_id},model:{generate:req=>runtime!.generateChat({...req,threadId:'native',ownerId:'owner'})},postPrepared:async post=>{sent.push(post.text);return {acceptance:'accepted',messageId:'sent-'+sent.length};}});
 };
 const receive=(id:string,text:string)=>coordinator!.receive({channel:'photon',accountId:'account',senderId:receiver!.identity.owner_id,isDirect:true,eventId:id,text},{threadId:receiver!.identity.route_id,destination:'photon',lineKey:'line'});
 try{
  receiver=await startReceiver(join(folder,'receiver'));
  if(syncFault)writeFileSync(join(folder,'receiver/usefulness-sync-fail'),'1');
  await mount();
  assert.equal((await receive('rating','The school deadline item was useful.')).status,'held');assert.equal(applies,1);
  assert.match(sent.at(-1)!,/^I couldn’t confirm whether the preference change happened\./,'uncertainty is reported without fresh owner input');
  assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_preference_tool'").get()!.status,'unknown','an accepted notice does not prove the write');
  const outcomes=()=>readFileSync(join(folder,'receiver/outcomes.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));
  assert.equal(outcomes().length,1);assert.equal(outcomes()[0].reference,reference);assert.equal(outcomes()[0].outcome,'useful');assert.match(outcomes()[0].item_locator,/^excerpt:/);
  const stored=JSON.stringify(outcomes());assert.ok(!stored.includes(excerpt));assert.ok(!stored.includes('This saved me time.'));
  phase='safe';assert.equal((await receive('hello','Hello')).status,'accepted');assert.equal(sent.at(-1),'Hello safely.');assert.match(sent.at(-2)!,/^I couldn’t confirm whether the preference change happened\./,'the notice precedes fresh safe chat');
  await coordinator!.close();await runtime!.close();await stopReceiver(receiver.child);journal.close();
  journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_preference_tool' AND status='unknown'").run();receiver=await startReceiver(join(folder,'receiver'));hold=false;lose=false;await mount();
  await coordinator!.recover();assert.equal(applies,1);assert.equal(outcomes().length,1);assert.equal(sent.at(-1),syncFault
    ? 'Hello safely.'
    : 'Recorded that as useful.');
  assert.equal(journal.db.prepare("SELECT count(*) n FROM actions WHERE kind='owner_preference_tool'").get()!.n,1);
  assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_preference_tool'").get()!.status,syncFault?'unknown':'succeeded');
  assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the preference change happened.')).length,1,'the accepted notice is never repeated');
  const afterProof=sent.length;await coordinator!.recover();assert.equal(sent.length,afterProof,'late committed proof is acknowledged once');
  const prompt=await new Promise<{prompt:string}>((done,fail)=>{let text='';const timer=setTimeout(()=>fail(Error('prompt read timeout')),5000);const onData=(chunk:Buffer)=>{text+=String(chunk);if(text.includes('\n')){clearTimeout(timer);receiver!.child.stdout.off('data',onData);done(JSON.parse(text.split('\n')[0]!));}};receiver!.child.stdout.on('data',onData);receiver!.child.stdin.write('usefulness-brief-prompt\n');});
  assert.ok(prompt.prompt.includes('USER FEEDBACK ON SPECIFIC PAST OUTPUTS'));assert.ok(prompt.prompt.includes(excerpt));assert.ok(!prompt.prompt.includes('Donation item: community fundraiser'));assert.ok(prompt.prompt.includes('not a sender mute'));
  assert.equal(outcomes().length,1);
 }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});
