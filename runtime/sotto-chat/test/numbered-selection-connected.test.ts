import {projectTaskToolDiagnostics,type TaskToolDiagnosticBatch} from '../src/agent/tool-diagnostics.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtempSync,rmSync,readFileSync,readdirSync,writeFileSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {MockLanguageModelV4} from 'ai/test';
import {stableActionIdentity} from '../src/tools/action-gate.js';
import {configuredChannelIdentityForConfig} from '../src/configured-identity.js';
import {activationDiscoverySeedForConfig} from '../src/bootstrap/main.js';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {createOwnerTaskTools} from '../src/tools/owner-task-tool.js';
import {createOwnerTaskToolClient,type OwnerTaskToolRequest} from '../src/tools/owner-task-tool-client.js';
import {startApplication} from '../src/application.js';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';

for(const mode of ['complete','complete-exception-first','manual-subset-exception-first','manual-subset-add-list-diagnostics','lost_receipt','writer_error','manual-subset','manual-subset-shared','manual-subset-legacy','manual-subset-malformed'] as const)test(`real runtime/receiver all-except ${mode}`,async()=>{
 const loseReceipt=mode==='lost_receipt',writerError=mode==='writer_error',manualSubset=mode.startsWith('manual-subset'),preEffectFailure=mode==='manual-subset-legacy'||mode==='manual-subset-malformed';
 const shared=mode==='manual-subset-shared';
 let activationApp:Awaited<ReturnType<typeof startApplication>>|undefined;
 const nodeBearer='synthetic-private-shared-project-runtime-bearer';
 const sharedThread='imessage:iMessage;-;+15555550123~shared';
 const folder=mkdtempSync(join(tmpdir(),'numbered-45-'));
 // Reuse the real Node activation service as the receiver's independent channel witness.
 // Task execution below still uses its existing real coordinator/journal/receiver path.
 if(shared){
  const config={version:1,tenantId:'tenant',channel:'photon',owner:{accountId:'account',ownerId:'+15555550123'},lineKey:'line',target:'photon',bearerToken:nodeBearer,firstContact:{mode:'owner_dm_shared_project'},credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'}};
  activationApp=await startApplication({directory:join(folder,'activation'),mediaRoot:folder,bearerToken:nodeBearer,owner:{...config.owner,channel:'photon'},lineKey:'line',target:'photon',port:0,configuredChannelIdentity:configuredChannelIdentityForConfig(config as never),discoverySeed:activationDiscoverySeedForConfig(config as never)},{agent:{generateChat:async()=>({text:''}),generateUnattended:async()=>({text:''}),rotateThread:async()=>{},purgeThreadsBefore:async()=>0},transport:{send:async()=>({acceptance:'not_attempted',providerMessageIds:[]})}});
  assert.equal((await activationApp.receive({channel:'photon',eventId:'shared-activation',accountId:'account',senderId:'+15555550123',isDirect:true,text:'Hello'},{threadId:sharedThread})).status,'silent');
  const url=`http://127.0.0.1:${activationApp.port}`;const headers={authorization:`Bearer ${nodeBearer}`,'content-type':'application/json'};
  const state=await (await fetch(url+'/v1/activation-state',{method:'POST',headers,body:'{}'})).json() as any;
  assert.equal(state.activation.status,'pending');
  const ack=await fetch(url+'/v1/activation-ack',{method:'POST',headers,body:JSON.stringify({event_id:state.activation.event_id})});assert.equal(ack.status,200);assert.deepEqual(await ack.json(),{acknowledged:true});
 }
 const child=spawn(process.env.SOTTO_PYTHON??'python3',[resolve('test/owner-loop-receiver-child.py')],{stdio:['pipe','pipe','pipe'],env:{PATH:process.env.PATH,PYTHONDONTWRITEBYTECODE:'1',SOTTO_TEST_LOOP_CLEANUP:'1',SOTTO_TEST_CLEANUP_WRITER_ERROR:writerError?'1':undefined,SOTTO_TEST_NUMBERED_SHARED:shared?'1':'0',...(shared?{SOTTO_TEST_NODE_PORT:String(activationApp!.port),SOTTO_CHAT_RUNTIME_URL:`http://127.0.0.1:${activationApp!.port}`,SOTTO_CHAT_RUNTIME_TOKEN:nodeBearer}:{}),SOTTO_TEST_NUMBERED_SELECTION:manualSubset?'0':'1',SOTTO_TEST_NUMBERED_MANUAL_SUBSET:manualSubset?'1':'0',SOTTO_RUNTIME_PROFILE:'managed-photon-fixed-five-v1',SOTTO_DEPLOYMENT_MODE:'managed',SOTTO_DATA:join(folder,'receiver'),SOTTO_CRON_DELIVER:'photon',SOTTO_TENANT_ID:'tenant',SOTTO_ACCOUNT_ID:'account',SOTTO_LINE_KEY:'line',SOTTO_MCP_TOKEN:'0123456789abcdef0123456789abcdef',PHOTON_HOME_CHANNEL:'+15555550123',PHOTON_ALLOWED_USERS:'+15555550123',PHOTON_PROJECT_ID:'fixture-project',PHOTON_PROJECT_SECRET:'fixture-secret',PHOTON_ALLOW_ALL_USERS:'false',SOTTO_IMESSAGE_NUMBER:'+15555550999',SOTTO_TIMEZONE:'America/Los_Angeles'}});
 let stderr='',output='';child.stderr.on('data',chunk=>stderr=(stderr+String(chunk)).slice(-4096));
 let timer:ReturnType<typeof setTimeout>|undefined;
 let healthApp:Awaited<ReturnType<typeof startApplication>>|undefined;let batch:TaskToolDiagnosticBatch|undefined;
 let journal:OperationalJournal|undefined,runtime:ReturnType<typeof createSottoAgentRuntime>|undefined,coordinator:ConversationCoordinator|undefined;
 try{
  const line=await new Promise<string>((done,fail)=>{timer=setTimeout(()=>fail(new Error('Receiver timeout '+stderr)),15000);child.once('error',fail);child.once('exit',code=>fail(new Error('Receiver exit '+code+' '+stderr)));child.stdout.on('data',chunk=>{output+=String(chunk);if(output.includes('\n'))done(output.slice(0,output.indexOf('\n')));});});clearTimeout(timer);
  const info=JSON.parse(line) as {port:number;identity:OwnerToolIdentity;bearer:string;readBearer:string};
  const providerThread=shared?sharedThread:info.identity.route_id!;
  if(shared)assert.equal(info.identity.route_mode,'first_contact_shared_project');
  const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['gmail' as const]})};
  let applyBody:any,preview:any,applies=0,addApplies=0,hideInspect=loseReceipt;const cleanupStatuses:number[]=[];
  const client=createOwnerTaskToolClient({...info,bridgeUrl:`http://127.0.0.1:${info.port}/mcp`,sourceAuthority,fetch:async(input,init)=>{
   assert.equal(new URL(String(input)).hostname,'127.0.0.1');const body=JSON.parse(String(init?.body));if(body.operation==='apply'&&body.args?.operation==='add')addApplies++;if(hideInspect&&body.operation==='inspect'&&body.args?.operation==='cleanup')return new Response('{}',{status:503});const response=await fetch(input,init);
   if(body.operation==='preview')preview=await response.clone().json();
   if(body.operation==='apply'&&body.args?.operation==='cleanup'){applyBody=body;applies++;cleanupStatuses.push(response.status);const raw=await response.clone().text();assert.ok(!raw.includes('SYNTHETIC-PRIVATE-WRITER-ERROR'));if(!preEffectFailure)assert.equal(JSON.parse(raw).status,writerError?'unknown':'committed');if(loseReceipt)throw Error('synthetic lost receipt');}
   return response;
  }});
  const anchors:string[]=[];
  if(manualSubset)for(const [index,name] of ['amber','birch','cedar','fourth'].entries()){
   const args={operation:'add' as const,text:'Synthetic '+name};const eventId=String(index+1).repeat(64);
   const binding=stableActionIdentity(eventId,'owner_loop_tool',args);
   const result=await client.call('apply',{eventId,payloadHash:'b'.repeat(64),actionId:binding.actionId,text:'Add Synthetic '+name,admittedAtMs:Date.now(),providerThreadId:providerThread,args});
   assert.equal(result.status,'committed');assert.ok('anchor_key' in result);anchors.push(result.anchor_key as string);
  }
  if(mode==='manual-subset-legacy')writeFileSync(join(folder,'receiver/preferences.json'),JSON.stringify({owner_preference_receipts:{legacy:{tag:'loop_cleanup',args:null}}}));
  if(mode==='manual-subset-malformed')writeFileSync(join(folder,'receiver/knowledge/continuity/unrelated-invalid.md'),'Retained unrelated malformed historical file\n');
  journal=new OperationalJournal(join(folder,'journal.sqlite'));
  const tools=createOwnerTaskTools({client,journal:()=>journal!,sourceAuthority});let phase:'list'|'selection'|'add'='list',steps=0;let diagnosticAnchor:string|undefined,addPrelude=false;const diagnosticTitle='Synthetic diagnostic private addition';
  const selection=manualSubset?{mode:'all_except',correction:mode==='manual-subset-exception-first'?'All except 2 are done.':'All done except 2',excluded:[{first:2,last:2,correction:'2'}]}:{mode:'all_except',correction:mode==='complete-exception-first'?'All except 7 and 14 are done.':'All done. Except 7 and 14',excluded:[{first:7,last:7,correction:'7'},{first:14,last:14,correction:'14'}]};
  const model=new MockLanguageModelV4({doGenerate:async()=>{steps++;if(phase==='add'&&!addPrelude){addPrelude=true;return {content:[{type:'text',text:'Added: '+diagnosticTitle}],finishReason:{unified:'stop',raw:'STOP'},usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]};}if(writerError&&phase==='selection'&&steps>2)return {content:[{type:'text',text:'Cleanup diagnostic: repeated by synthetic model'}],finishReason:{unified:'stop',raw:'stop'},usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]};return {content:[{type:'tool-call',toolCallId:'selection-'+steps,toolName:phase==='add'?'owner_task_add':phase==='list'?'owner_task_list':'owner_task_cleanup',input:JSON.stringify(phase==='add'?{text:diagnosticTitle}:phase==='list'?(manualSubset?{anchor_keys:diagnosticAnchor?[diagnosticAnchor]:anchors.slice(0,3)}:{}):{selection})}],finishReason:{unified:'tool-calls',raw:'tool_calls'},usage:{inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}},warnings:[]};}});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.sqlite'),instructions:'Use task tools.',sourceAuthority,chatTools:async()=>tools,onTaskToolDiagnostics:events=>{batch=projectTaskToolDiagnostics({events:[...events],completedAtMs:1});}});
  const sent:string[]=[],modelErrors:string[]=[];
  coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:info.identity.account_id,ownerId:info.identity.owner_id},ownerTaskTool:client,model:{generate:async request=>{try{return await runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'});}catch(error){modelErrors.push(String(error));throw error;}}},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'accepted-'+sent.length};}});
  const event=(eventId:string,text:string)=>({channel:'photon' as const,accountId:info.identity.account_id,senderId:info.identity.owner_id,isDirect:true,eventId,text});const context={threadId:providerThread,destination:'photon',lineKey:'line'};
  const files=()=>{const dir=join(folder,'receiver/knowledge/continuity');return Object.fromEntries(readdirSync(dir).filter(name=>name.endsWith('.md')).map(name=>[name,readFileSync(join(dir,name),'utf8')]));};
  const before=files();assert.equal(Object.keys(before).length,manualSubset?(mode==='manual-subset-malformed'?52:51):45);
  if(manualSubset)for(const anchor of anchors){const raw=Object.values(before).find(raw=>raw.includes('anchor_key: '+anchor+'\n'));assert.ok(raw);assert.match(raw,/channel: manual/);assert.match(raw,/source: user_added/);}
  await coordinator.receive(event('list','Show my numbered tasks'),context);
  assert.ok(preview,JSON.stringify({sent,steps,modelErrors,runs:journal.db.prepare('SELECT status FROM runs').all()}));assert.equal(preview.review.rows.length,manualSubset?3:45);if(manualSubset){assert.equal(preview.review.total,50);assert.equal(preview.review.remaining,47);assert.deepEqual(preview.review.anchor_keys,anchors.slice(0,3));}assert.match(sent[0]!,/^Open tasks:/);assert.deepEqual(files(),before);
  phase='selection';await coordinator.receive(event('selection',selection.correction),context);
  assert.equal(applies,1);assert.equal(applyBody.args.items.length,manualSubset?2:43);assert.deepEqual(applyBody.args.selection,selection);
  assert.deepEqual(applyBody.args.items.map((item:any)=>item.index),manualSubset?[1,3]:Array.from({length:45},(_,i)=>i+1).filter(i=>i!==7&&i!==14));
  if(writerError){
   assert.equal(steps,3,'list and cleanup tool calls plus the simulated diagnostic-leak text were generated');
   assert.equal(modelErrors.length,0,'the model returned its simulated diagnostic-leak text normally');
   const expected=[{tool:'owner_task_cleanup',status:'unknown',error:'tool_error',cleanup_stage:'writer',cleanup_category:'io',cleanup_http_status:200}];
   const healthBearer='synthetic-cleanup-health-bearer-at-least-32';
   healthApp=await startApplication({directory:join(folder,'health'),mediaRoot:folder,bearerToken:healthBearer,owner:{channel:'telegram',accountId:'bot',ownerId:'42'},lineKey:'bot',target:'owner',providerThreadId:'telegram:42'},
    {agent:{generateChat:async()=>({text:''}),generateUnattended:async()=>({text:''}),rotateThread:async()=>{},purgeThreadsBefore:async()=>0},transport:{send:async()=>({acceptance:'accepted',providerMessageIds:['fixture']})},taskToolDiagnostics:()=>batch});
   const health=await(await fetch(`http://127.0.0.1:${healthApp.port}/v1/health`,{headers:{authorization:`Bearer ${healthBearer}`}})).json();
   assert.deepEqual(health.taskToolDiagnostics,{events:expected,completedAtMs:1});
   assert.ok(!JSON.stringify(health).includes('SYNTHETIC-PRIVATE-WRITER-ERROR'));
   assert.deepEqual(files(),before,'writer failure has no canonical effect');
   assert.equal(readFileSync(join(folder,'receiver/cleanup-failure-attempts.jsonl'),'utf8').trim().split('\n').length,1);
   assert.equal(journal.getAction(applyBody.action_id)!.status,'unknown');
   assert.equal(sent.length,2);assert.match(sent[1]!,/^I couldn’t confirm whether the task change happened\./);
   assert.ok(!JSON.stringify(sent).includes('Cleanup diagnostic:'));
   assert.ok(!JSON.stringify(journal.db.prepare('SELECT payload FROM prepared_replies').all()).includes('Cleanup diagnostic:'));
   const authority={accountId:info.identity.account_id,ownerId:info.identity.owner_id,lineKey:'line',channel:'photon' as const,scopeHash:client.scopeHash};
   const saved=journal.readOwnerTaskToolAction(applyBody.action_id,authority);
   assert.deepEqual(await client.call('inspect',saved.request),{version:1,status:'not_found'});
   assert.equal(applies,1);assert.equal(journal.getAction(applyBody.action_id)!.status,'unknown');
   assert.equal(readFileSync(join(folder,'receiver/cleanup-failure-attempts.jsonl'),'utf8').trim().split('\n').length,1,'inspection never retries writer');
   assert.ok(!stderr.includes('SYNTHETIC-PRIVATE-WRITER-ERROR'));
   for(const name of readdirSync(folder).filter(name=>/^(journal|memory)\.sqlite(?:-|$)/.test(name))){
    assert.ok(!readFileSync(join(folder,name)).includes(Buffer.from('SYNTHETIC-PRIVATE-WRITER-ERROR')),`raw error absent from synthetic ${name}`);
   }
   return;
  }
  if(preEffectFailure){assert.deepEqual(cleanupStatuses,[503]);assert.match(sent.at(-1)!,/couldn’t confirm/);assert.deepEqual(files(),before);assert.equal(readFileSync(join(folder,'receiver/cleanup-writes.jsonl'),'utf8').trim().split('\n').length,4);return;}
  const after=files();assert.equal(Object.values(after).filter(text=>/^status: resolved$/m.test(text)).length,manualSubset?2:43);
  if(manualSubset){const changed=new Set([anchors[0],anchors[2]]);for(const [name,raw] of Object.entries(before))if(![...changed].some(anchor=>raw.includes('anchor_key: '+anchor+'\n')))assert.equal(after[name],raw);}
  for(const index of manualSubset?[2]:[7,14]){const anchor=preview.review.rows[index-1].anchor_key;const name=Object.keys(before).find(name=>before[name]!.includes('anchor_key: '+anchor+'\n'));assert.ok(name);assert.equal(after[name],before[name]);}
  const writes=readFileSync(join(folder,'receiver/cleanup-writes.jsonl'),'utf8');assert.equal(writes.trim().split('\n').length,manualSubset?6:43);
  if(loseReceipt){
   assert.match(sent.at(-1)!,/couldn’t confirm/);
   await coordinator.close();coordinator=undefined;await runtime.close();runtime=undefined;journal.close();journal=new OperationalJournal(join(folder,'journal.sqlite'));journal.recoverInterrupted();hideInspect=false;
   const authority={accountId:info.identity.account_id,ownerId:info.identity.owner_id,lineKey:'line',channel:'photon' as const,scopeHash:client.scopeHash};
   const saved=journal.readOwnerTaskToolAction(applyBody.action_id,authority);
   assert.deepEqual(saved.request.args,applyBody.args);
   const receipt=await client.call('inspect',saved.request as OwnerTaskToolRequest);assert.equal(receipt.status,'committed');
   journal.settleOwnerTaskToolAction(applyBody.action_id,authority,receipt as never,true);
   assert.equal(journal.getAction(applyBody.action_id)!.status,'succeeded');
  }else assert.equal(steps,2,'one model step per settled list/selection');
  if(shared){
   const state=await (await fetch(`http://127.0.0.1:${activationApp!.port}/v1/activation-state`,{method:'POST',headers:{authorization:`Bearer ${nodeBearer}`,'content-type':'application/json'},body:'{}'})).json() as any;
   assert.equal(state.activation.status,'acknowledged');assert.equal(state.configured.identity.route.mode,'first_contact_shared_project');
   await activationApp!.close();activationApp=undefined;
   await assert.rejects(()=>client.call('inspect',{actionId:applyBody.action_id,eventId:applyBody.event_id,payloadHash:applyBody.payload_hash,text:applyBody.text,admittedAtMs:applyBody.admitted_at_ms,providerThreadId:applyBody.provider_thread_id,args:applyBody.args}),/Owner tool unavailable/);
  }
  assert.deepEqual(files(),after);assert.equal(readFileSync(join(folder,'receiver/cleanup-writes.jsonl'),'utf8'),writes);
  if(mode==='manual-subset-add-list-diagnostics'){
   const priorAdds=addApplies,priorSteps=steps;phase='add';await coordinator!.receive(event('diagnostic-add','Add '+diagnosticTitle),context);
   assert.equal(steps,priorSteps+2,'unproved acknowledgement buys one corrective model call');assert.equal(addApplies,priorAdds+1,'corrected real add applies exactly once');
   assert.deepEqual(batch?.events,[{tool:'owner_task_add',status:'recorded',error:'none'}]);
   assert.ok(!JSON.stringify(batch).includes(diagnosticTitle));
   const added=files();const newFiles=Object.keys(added).filter(name=>!Object.hasOwn(after,name));assert.equal(newFiles.length,1);
   assert.ok(added[newFiles[0]!]!.includes('summary: '+diagnosticTitle+'\n'));
   diagnosticAnchor=/^anchor_key: (.+)$/m.exec(added[newFiles[0]!]!)?.[1];assert.ok(diagnosticAnchor);
   phase='list';await coordinator!.receive(event('diagnostic-list','Show my numbered task'),context);
   assert.deepEqual(batch?.events,[{tool:'owner_task_list',status:'proposed',error:'none'}]);
   assert.ok(!JSON.stringify(batch).includes(diagnosticTitle));assert.deepEqual(files(),added);
   assert.deepEqual(preview.review.anchor_keys,[diagnosticAnchor]);assert.equal(preview.review.rows[0].what,diagnosticTitle);
   assert.match(sent.at(-1)!,/^Open tasks:/);
  }
 }finally{await healthApp?.close();if(timer)clearTimeout(timer);await coordinator?.close();await runtime?.close();journal?.close();if(child.exitCode===null){const ended=once(child,'exit');child.stdin.end('quit\n');const kill=setTimeout(()=>child.kill('SIGKILL'),5000);await ended;clearTimeout(kill);}await activationApp?.close();rmSync(folder,{recursive:true,force:true});}
});
