import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { MockLanguageModelV4 } from 'ai/test';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator } from '../src/conversation.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createOwnerPreferenceToolClient } from '../src/tools/owner-preference-tool-client.js';
import { createOwnerPreferenceTools } from '../src/tools/owner-preference-tool.js';
import { createRunScriptTool } from '../src/tools/run-script-tool.js';
import { createPreferenceCommand, SKILL_COMMAND_EXECUTION_GUIDE } from '../src/tools/preference-command.js';
import { createOwnerPreferenceExecution } from '../src/tools/owner-preference-execution.js';
import { readSkillIndex } from '../src/tools/skills.js';
import type { OwnerPreferenceToolArgs } from '../src/tools/owner-preference-tool-contract.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';

async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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
const sourceAuthority = {read: async () => ({version: 2 as const, revision: 'a'.repeat(64), complete: true, bridge_sources: [], google_sources: []})};

for(const [ownerText,commandSpec] of [['Please quieter today.','tomorrow'],['quiet for 10 minutes please','quiet for 10 minutes please']])
test(`original Feedback quiet command ${ownerText} commits once despite lost response; state-bound resume preserves preferences`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-quiet-native-'));
 let journal=new OperationalJournal(join(folder,'operations.sqlite'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
 try {
  receiver=await startReceiver(join(folder,'receiver'));
  const preferencesPath=join(folder,'receiver/preferences.json');
  writeFileSync(preferencesPath,JSON.stringify({explicit:{nudge_budget:'4',retired_scalar:'morning'},unrelated:{keep:true}}),{mode:0o600});
  let apply=0,blocked=true,lose=true;
  const client=createOwnerPreferenceToolClient({...receiver,fetch:async(input,init)=>{
   const body=JSON.parse(init!.body as string);
   if(body.operation==='inspect'&&blocked)return new Response('{}',{status:503});
   const response=await fetch(input,init);
   if(body.operation==='apply'){apply++;if(lose){lose=false;assert.equal(response.status,200);await response.body?.cancel();throw new Error('lost committed response');}}
   return response;
  }});
  const execution=createOwnerPreferenceExecution({client,journal:()=>journal,sourceAuthority});
  const command=createRunScriptTool({mode:'chat',commandContext:(context,signal)=>({mode:context?.get('mode'),runId:context?.get('runId'),ownerRequestKey:context?.get('ownerRequestKey'),allowOwnerPreference:context?.get('allowOwnerPreference'),sourceRevision:context?.get('sourceRevision'),signal}),executeCommand:createPreferenceCommand({execution})});
  let step=0;
  const model=new MockLanguageModelV4({doGenerate:async request=>{
   const calls=[{name:'skill',input:{name:'sotto-feedback'}},{name:'run_sotto_script',input:{id:'preferences',args:['snooze-nudges',commandSpec]}}];
   if(step===1)assert.ok(JSON.stringify(request.prompt).includes('Sotto — Feedback & corrections'));
   const call=calls[step++];
   return {content:call?[{type:'tool-call' as const,toolCallId:'quiet-'+step,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text' as const,text:'unused'}],finishReason:{unified:call?'tool-calls':'stop',raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:SKILL_COMMAND_EXECUTION_GUIDE,skillPaths:(await readSkillIndex(resolve('../../sotto-chief-of-staff'))).map(skill=>skill.path),sourceAuthority,chatTools:async()=>({run_sotto_script:command})});
  const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:receiver.identity.owner_id};
  const context={threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'};
  const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:ownerPolicy.ownerId,isDirect:true,eventId,text});
  const noticeAttempts:string[]=[];
  const first=new ConversationCoordinator({journal,ownerPolicy,ownerPreferenceTool:client,model:{generate:request=>runtime!.generateChat({...request,threadId:'quiet-memory',ownerId:'owner-memory'})},postPrepared:async request=>{noticeAttempts.push(request.text);throw new Error('no uncertain ack');}});
  assert.equal((await first.receive(inbound('quiet',ownerText!),context)).status,'held');
  assert.equal(noticeAttempts.length,1);assert.match(noticeAttempts[0]!,/^I couldn’t confirm whether the preference change happened\./);
  assert.equal(journal.db.prepare("SELECT COUNT(*) AS n FROM prepared_replies p JOIN dispatches d ON d.id=p.dispatch_id WHERE p.purpose='outcome_pending' AND d.status='unknown'").get()!.n,1,'a contacted failed notice stays unknown');
  const canonical=JSON.parse(readFileSync(preferencesPath,'utf8'));
  assert.match(canonical.explicit.nudge_snooze_until,/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
  if(ownerText!.includes('10 minutes')){
   const admitted=journal.db.prepare('SELECT received_at FROM inbound_events ORDER BY rowid LIMIT 1').get()!;
   const expected=new Date(Number(admitted.received_at)+10*60_000);
   const parts=new Intl.DateTimeFormat('sv-SE',{timeZone:'America/Los_Angeles',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(expected).replace(' ','T');
   assert.equal(canonical.explicit.nudge_snooze_until,parts);
  }
  assert.equal(canonical.explicit.nudge_budget,'4');assert.equal(canonical.explicit.retired_scalar,'morning');assert.deepEqual(canonical.unrelated,{keep:true});
  await first.close();await runtime.close();runtime=undefined;journal.close();journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();
  const sent:string[]=[];
  const recovered=new ConversationCoordinator({journal,ownerPolicy,ownerPreferenceTool:client,model:{generate:async request=>{
   if(request.text==='Hello safely')return 'safe answer';
   await createPreferenceCommand({execution:createOwnerPreferenceExecution({client,journal:()=>journal,sourceAuthority})})({id:'preferences',args:['unsnooze-nudges']},{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerPreference:request.allowOwnerPreference,sourceRevision:'a'.repeat(64),signal:request.signal});return 'unused';
  }},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'ack-'+sent.length};}});
  assert.equal((await recovered.recover())[0]?.status,'held');
  assert.equal((await recovered.receive(inbound('safe','Hello safely'),context)).status,'accepted');
  blocked=false;
  journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_preference_tool' AND status='unknown'").run();
  assert.equal((await recovered.recover())[0]?.status,'accepted');assert.equal(apply,1);
  assert.match(sent.at(-1)!,/^Nudges paused until/);
  assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the preference change happened.')).length,0,'the contacted unknown notice is not replayed after restart');assert.equal(noticeAttempts.length,1);
  assert.equal((await recovered.receive(inbound('resume','Back to normal.'),context)).status,'accepted');assert.equal(apply,2);
  const resumed=JSON.parse(readFileSync(preferencesPath,'utf8'));assert.equal(resumed.explicit.nudge_snooze_until,'');assert.equal(resumed.explicit.nudge_budget,'4');assert.deepEqual(resumed.unrelated,{keep:true});
  assert.equal((await recovered.receive(inbound('quiet',ownerText!),context)).status,'duplicate');await recovered.close();
 }finally{await runtime?.close();journal.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
