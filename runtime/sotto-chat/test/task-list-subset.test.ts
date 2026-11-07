import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {RequestContext} from '@mastra/core/request-context';
import {MockLanguageModelV4} from 'ai/test';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator,type ModelRequest} from '../src/conversation.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {createOwnerTaskToolClient} from '../src/tools/owner-task-tool-client.js';
import {createOwnerTaskTools} from '../src/tools/owner-task-tool.js';
import {OWNER_TASK_READBACK_GUIDE} from '../src/tools/owner-task-readback.js';
import {createOwnerTaskExecution} from '../src/tools/owner-task-execution.js';
import {createTaskCommand} from '../src/tools/task-command.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import {cleanupListSchema,cleanupListHash,cleanupListText,ownerTaskToolArgsSchema,taskPreviewMatches,type CleanupList} from '../src/tools/owner-task-tool-contract.js';
const revision='a'.repeat(64);
const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:revision};
const context={threadId:'thread',destination:'photon',lineKey:'line'};
const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId,text});
const anchors=['cedar','amber','birch'];
const list:CleanupList={operation:'cleanup_list',scope:'open',anchor_keys:anchors,source_revision:revision,total:4,remaining:1,review_hash:'',rows:anchors.map((name,i)=>({index:i+1,anchor_key:name,name:'Task',what:name,direction:'you_owe',age_days:0,times_surfaced:0,surface_count_provenance:'accepted_delivery',overdue:false,deadline:null,chased_count:0,chased_out:false,suggestion:'do it or dismiss',loop_version:'b'.repeat(64),loop_identity:'c'.repeat(64)}))};
list.review_hash=cleanupListHash(list);
const {scope:_scope,anchor_keys:_anchors,...staleList}=list;staleList.review_hash=cleanupListHash(staleList);
const choices=[{index:1,action:'resolve' as const,correction:'1'}];
function fixture(cleanupInput:unknown={items:choices},display:CleanupList=list,unfiltered=false){
 const list=display,anchors=list.anchor_keys??list.rows.map(row=>row.anchor_key);
 const folder=mkdtempSync(join(tmpdir(),'task-subset-'));let journal=new OperationalJournal(join(folder,'journal.sqlite'));let phase='list',stateRevision=revision,complete=true;const calls:Record<string,any>[]=[];const sent:string[]=[];const contexts:RequestContext[]=[];
 const sourceAuthority={read:async()=>({version:2 as const,complete,revision:stateRevision,bridge_sources:[],google_sources:[]})};
 const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async(_url,init)=>{const body=JSON.parse(String(init?.body));calls.push(body);if(body.operation==='preview'){const {anchor_keys:_keys,...full}=list;const current=unfiltered?full:{...full,total:list.rows.length,remaining:0};current.review_hash=cleanupListHash(current);return new Response(JSON.stringify({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review:body.args.scope==='open'?(body.args.anchor_keys?list:current):staleList}));}return new Response(JSON.stringify({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:journal.getAction(body.action_id)!.payload_hash,operation:'cleanup',review_hash:list.review_hash,outcome:'complete',items:body.args.items.map((item:{index:number;action:string})=>({index:item.index,action:item.action,anchor_key:list.rows[item.index-1]!.anchor_key,status:'applied',post_digest:revision,loop_status:'resolved'}))}));}});
 let projections=0,readbacks=0;
 client.readLoops=async()=>{projections++;return {coverage:'currently_permitted_canonical_loops',you_owe:[],waiting_on_them:[],parked:[],counts:{you_owe:0,waiting_on_them:0,parked:0}};};
 client.readback=async()=>{readbacks++;return {you_owe:3,waiting_on_them:0,source_revision:stateRevision};};
 const tools=createOwnerTaskTools({client,journal:()=>journal,sourceAuthority});
 const script=createRunScriptTool({mode:'chat',executeCommand:createTaskCommand(createOwnerTaskExecution({client,journal:()=>journal,sourceAuthority})),commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerTask:ctx?.get('allowOwnerTask'),sourceRevision:ctx?.get('sourceRevision'),signal})});
 const callTool=async(name:'owner_task_list'|'owner_task_cleanup',input:unknown,request:ModelRequest)=>{const requestContext=new RequestContext();for(const [key,value]of Object.entries({mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerTask:request.allowOwnerTask,sourceRevision:stateRevision}))requestContext.set(key,value);contexts.push(requestContext);return tools[name].execute!(input as never,{requestContext,abortSignal:request.signal} as never);};
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerTaskTool:client,model:{generate:async request=>{if(phase==='list')await callTool('owner_task_list',unfiltered?{}:{anchor_keys:anchors},request);if(phase==='cleanup')await callTool('owner_task_cleanup',cleanupInput,request);return {text:String(contexts.at(-1)?.get('ownerTaskClarification')??'arbitrary model prose with the wrong numbering'),sourceRevision:stateRevision};}},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'reply-'+sent.length};}});
 return {folder,get projections(){return projections;},get readbacks(){return readbacks;},get journal(){return journal;},reopen(){journal.close();journal=new OperationalJournal(join(folder,'journal.sqlite'));journal.recoverInterrupted();},client,sourceAuthority,tools,script,callTool,coordinator,calls,sent,contexts,set phase(value:string){phase=value;},revoke(){stateRevision='d'.repeat(64);},incomplete(){complete=false;},close:async()=>{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}};
}

test('subset preview binds requested order and rejects duplicate or altered identity maps',()=>{
 const args=ownerTaskToolArgsSchema.parse({operation:'propose_cleanup',scope:'open',source_revision:revision,anchor_keys:anchors});
 const preview={version:1 as const,status:'preview' as const,event_id:revision,payload_hash:revision,review:list};
 assert.ok(cleanupListSchema.safeParse(list).success);assert.ok(taskPreviewMatches(preview,args));
 assert.equal(taskPreviewMatches(preview,{...args,anchor_keys:[...anchors].reverse()} as typeof args),false);
 assert.equal(cleanupListSchema.safeParse({...list,anchor_keys:[...anchors].reverse()}).success,false);
 for(const anchor_keys of [[],['amber','amber'],Array(51).fill('amber')])assert.equal(ownerTaskToolArgsSchema.safeParse({operation:'propose_cleanup',scope:'open',source_revision:revision,anchor_keys}).success,false);
 assert.equal(ownerTaskToolArgsSchema.safeParse({operation:'propose_cleanup',source_revision:revision,anchor_keys:anchors}).success,false);
});

test('task inventory guide distinguishes factual inspection from terminal numbered review',()=>{
 assert.match(OWNER_TASK_READBACK_GUIDE,/Factual status, completion evidence and provenance questions are targeted inspection/);
 assert.match(OWNER_TASK_READBACK_GUIDE,/knowledge_query obligation_history or loops_query/);
 assert.match(OWNER_TASK_READBACK_GUIDE,/terminal selectable review/);
 assert.match(OWNER_TASK_READBACK_GUIDE,/Deliver its exact review_text without renumbering or paraphrasing/);
});

test('coordinator publishes exact canonical subset and injects its proof for numeric completion',async()=>{const f=fixture();try{
 assert.equal((await f.coordinator.receive(inbound('list','Show those three tasks'),context)).status,'accepted');
 assert.equal(f.sent[0],cleanupListText(list));assert.deepEqual(f.calls[0]!.args.anchor_keys,anchors);
 f.phase='cleanup';assert.equal((await f.coordinator.receive(inbound('choice','1 is done'),context)).status,'accepted');
 assert.equal(f.calls.length,2);assert.deepEqual(f.calls[1]!.args.list.anchor_keys,anchors);assert.equal(f.calls[1]!.args.list.rows[0].anchor_key,'cedar');assert.equal(f.calls[1]!.args.proposal.action_id,f.calls[0]!.action_id);
 assert.match(f.sent[1]!,/1\. Marked done/);
 assert.equal(f.projections,0,'accepted proof needs no discarded pre-apply projection');assert.equal(f.readbacks,1,'fresh post-effect readback remains');
 }finally{await f.close();}});

for(const path of ['native','script'] as const)for(const phase of ['list','cleanup'] as const)test(`settled ${path} ${phase} delivers canonical text after one model step`,async()=>{
 const f=fixture();let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;let coordinator:ConversationCoordinator|undefined;let steps=0;
 try{
  if(phase==='cleanup')await f.coordinator.receive(inbound('prior','Show those three tasks'),context);
  const usage={inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
  const toolName=path==='script'?'run_sotto_script':phase==='list'?'owner_task_list':'owner_task_cleanup';
  const input=path==='script'?{id:'retune-apply',args:phase==='list'?['list']:['cleanup',JSON.stringify({items:choices})]}:phase==='list'?{anchor_keys:anchors}:{items:choices};
  const model=new MockLanguageModelV4({doGenerate:async()=>{assert.equal(++steps,1,'no discarded model continuation');return {content:[{type:'tool-call',toolCallId:'task',toolName,input:JSON.stringify(input)}],finishReason:{unified:'tool-calls',raw:'tool_calls'},usage,warnings:[]};}});
  runtime=createSottoAgentRuntime({model,storagePath:join(f.folder,'memory.db'),instructions:'Use task tools',sourceAuthority:f.sourceAuthority,chatTools:async()=>({...f.tools,run_sotto_script:f.script})});
  coordinator=new ConversationCoordinator({journal:f.journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerTaskTool:f.client,model:{generate:request=>runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{f.sent.push(request.text);return {acceptance:'accepted',messageId:'ack'};}});
  assert.equal((await coordinator.receive(inbound('settle',phase==='list'?'Show stale tasks':'1 is done'),context)).status,'accepted');assert.equal(steps,1);
  if(phase==='list')assert.equal(f.sent.at(-1),cleanupListText(path==='native'?list:staleList));else{assert.match(f.sent.at(-1)!,/1\. Marked done/);assert.equal(f.projections,0);assert.equal(f.readbacks,1);}
 }finally{await coordinator?.close();await runtime?.close();await f.close();}
});

for(const scenario of ['missing','intervening','source','ambiguous'] as const)test(`${scenario} list immediately clarifies without reserving or contacting a mutation`,async()=>{const f=fixture();try{
 if(scenario!=='missing')await f.coordinator.receive(inbound('list','Show those three tasks'),context);
 if(scenario==='intervening'){f.phase='safe';await f.coordinator.receive(inbound('between','hello'),context);}
 if(scenario==='source')f.revoke();
 f.phase='cleanup';await f.coordinator.receive(inbound('choice',scenario==='ambiguous'?'1 might be done':'1 is done'),context);
 assert.match(String(f.contexts.at(-1)!.get('ownerTaskClarification')),/fresh numbered list/);
 assert.equal(f.calls.filter(row=>row.operation==='apply').length,0);
 assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE source_event_id=(SELECT id FROM inbound_events WHERE source_id='choice')").get()!.n,0);
 }finally{await f.close();}});

for(const path of ['native','script'] as const)for(const scenario of ['missing','ambiguous'] as const)test(`real model loop stops on ${path} ${scenario} list clarification after its first tool call`,async()=>{const f=fixture();let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;let coordinator:ConversationCoordinator|undefined;let steps=0;try{
 if(scenario==='ambiguous')await f.coordinator.receive(inbound('list','Show those three tasks'),context);
 const callsBefore=f.calls.length;
 const usage={inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
 const model=new MockLanguageModelV4({doGenerate:async()=>{steps++;return {content:[{type:'tool-call',toolCallId:'cleanup',toolName:path==='native'?'owner_task_cleanup':'run_sotto_script',input:JSON.stringify(path==='native'?{items:choices}:{id:'retune-apply',args:['cleanup',JSON.stringify({items:choices})]})}],finishReason:{unified:'tool-calls',raw:'tool_calls'},usage,warnings:[]};}});
 runtime=createSottoAgentRuntime({model,storagePath:join(f.folder,'memory.db'),instructions:'Use task tools',sourceAuthority:f.sourceAuthority,chatTools:async()=>({...f.tools,run_sotto_script:f.script})});
 coordinator=new ConversationCoordinator({journal:f.journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerTaskTool:f.client,model:{generate:request=>runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{f.sent.push(request.text);return {acceptance:'accepted',messageId:'clarification'};}});
 assert.equal((await coordinator.receive(inbound('missing',scenario==='ambiguous'?'1 might be done':'1 is done'),context)).status,'accepted');
 assert.equal(steps,1);assert.equal(f.calls.length,callsBefore);assert.match(f.sent.at(-1)!,/fresh numbered list/);
 }finally{await coordinator?.close();await runtime?.close();await f.close();}});


test('legacy cleanup preserves trusted admission failures without marking them as list clarification',async()=>{const f=fixture();try{
 const requestContext=new RequestContext();for(const [key,value]of Object.entries({mode:'chat',runId:'untrusted-run',allowOwnerTask:true,sourceRevision:revision}))requestContext.set(key,value);
 await assert.rejects(()=>f.script.execute!({id:'retune-apply',args:['cleanup',JSON.stringify({items:choices})]},{requestContext} as never),/Owner task authority is unavailable/);
 assert.equal(requestContext.get('ownerTaskClarification'),undefined);assert.equal(f.calls.length,0);
 }finally{await f.close();}});


test('accepted subset survives journal restart with its original order and target proofs',async()=>{const f=fixture();let restarted:ConversationCoordinator|undefined;try{
 await f.coordinator.receive(inbound('list','Show those three tasks'),context);
 await f.coordinator.close();f.reopen();
 restarted=new ConversationCoordinator({journal:f.journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerTaskTool:f.client,model:{generate:async request=>{await f.callTool('owner_task_cleanup',{items:[{index:3,action:'resolve',correction:'3'}]},request);return 'model prose';}},postPrepared:async request=>{f.sent.push(request.text);return {acceptance:'accepted',messageId:'restarted-ack'};}});
 assert.equal((await restarted.receive(inbound('after-restart','3 is done'),context)).status,'accepted');
 assert.deepEqual(f.calls[1]!.args.list.anchor_keys,anchors);assert.equal(f.calls[1]!.args.list.rows[2].anchor_key,'birch');
 assert.equal(f.calls[1]!.args.proposal.action_id,f.calls[0]!.action_id);assert.match(f.sent.at(-1)!,/3\. Marked done/);
 }finally{await restarted?.close();await f.close();}});

for(const status of ['unknown','rejected'] as const)test(`subset with ${status} delivery cannot authorize completion`,async()=>{const f=fixture();let changed:ConversationCoordinator|undefined;try{
 await f.coordinator.receive(inbound('list','Show those three tasks'),context);
 changed=new ConversationCoordinator({journal:f.journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerTaskTool:f.client,model:{generate:async request=>{
   f.journal.db.prepare("UPDATE dispatches SET status=? WHERE id IN (SELECT dispatch_id FROM prepared_replies WHERE event_id=(SELECT id FROM inbound_events WHERE source_id='list'))").run(status);
   const result=await f.callTool('owner_task_cleanup',{items:choices},request);
   assert.equal((result as {status:string}).status,'needs_clarification');return 'No current accepted list. I can show you a fresh numbered list.';
 }},postPrepared:async request=>{f.sent.push(request.text);return {acceptance:'accepted',messageId:'clarification-ack'};}});
 await changed.receive(inbound('choice','1 is done'),context);
 assert.equal(f.calls.filter(call=>call.operation==='apply').length,0);assert.match(String(f.contexts.at(-1)!.get('ownerTaskClarification')),/fresh numbered list/);
 }finally{await changed?.close();await f.close();}});

test('incomplete authority cannot apply a saved numbered list without the discarded projection',async()=>{const f=fixture();try{
 await f.coordinator.receive(inbound('list','Show those three tasks'),context);f.phase='cleanup';f.incomplete();
 await f.coordinator.receive(inbound('choice','1 is done'),context);
 assert.equal(f.calls.filter(row=>row.operation==='apply').length,0);assert.equal(f.projections,0);assert.equal(f.readbacks,0);
 assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE source_event_id=(SELECT id FROM inbound_events WHERE source_id='choice')").get()!.n,0);
 }finally{await f.close();}});

const rangeSelection={mode:'explicit',correction:'Close 1-2',clauses:[{action:'resolve',correction:'Close 1-2',references:[{first:1,last:2,correction:'1-2'}]}]};
for(const scenario of ['valid','restart','missing','intervening','source','wrong-reply','wrong-route','unknown-delivery','forged-order'] as const)test(`typed selection retains accepted-list proof: ${scenario}`,async()=>{
 const f=fixture({selection:rangeSelection});let changed:ConversationCoordinator|undefined;try{
  if(scenario!=='missing')await f.coordinator.receive(inbound('list','Show these tasks'),context);
  if(scenario==='restart'){await f.coordinator.close();f.reopen();}
  if(scenario==='intervening'){f.phase='safe';await f.coordinator.receive(inbound('between','Hello'),context);}
  if(scenario==='source')f.revoke();
  if(scenario==='forged-order'){
   const row=f.journal.db.prepare("SELECT id,result_json FROM actions WHERE json_extract(result_json,'$.args.operation')='propose_cleanup'").get()!;
   const metadata=JSON.parse(String(row.result_json));metadata.preview.review.rows.reverse();
   f.journal.db.prepare('UPDATE actions SET result_json=? WHERE id=?').run(JSON.stringify(metadata),String(row.id));
  }
  changed=new ConversationCoordinator({journal:f.journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerTaskTool:f.client,model:{generate:async request=>{
   if(scenario==='unknown-delivery')f.journal.db.prepare("UPDATE dispatches SET status='unknown' WHERE id IN (SELECT dispatch_id FROM prepared_replies WHERE event_id=(SELECT id FROM inbound_events WHERE source_id='list'))").run();
   await f.callTool('owner_task_cleanup',{selection:rangeSelection},request);return 'A task response';
  }},postPrepared:async request=>{f.sent.push(request.text);return {acceptance:'accepted',messageId:'typed-ack'};}});
  await changed.receive({...inbound('choice',rangeSelection.correction),...(scenario==='wrong-reply'?{replyToMessageId:'another-question'}:{})},{...context,...(scenario==='wrong-route'?{destination:'other'}:{})});
  const calls=f.calls.filter(row=>row.operation==='apply');
  if(scenario==='valid'||scenario==='restart'){assert.equal(calls.length,1);assert.deepEqual(calls[0]!.args.selection,rangeSelection);assert.deepEqual(calls[0]!.args.items.map((item:any)=>item.index),[1,2]);}
  else assert.equal(calls.length,0,'invalid list must never contact a mutation');
 }finally{await changed?.close();await f.close();}
});

test('all-except uses only the complete requested subset despite outside global rows',async()=>{
 const selection={mode:'all_except',correction:'All done except 1',excluded:[{first:1,last:1,correction:'1'}]};
 const f=fixture({selection});try{
  await f.coordinator.receive(inbound('list','Show those three tasks'),context);
  f.phase='cleanup';await f.coordinator.receive(inbound('choice',selection.correction),context);
  const apply=f.calls.find(row=>row.operation==='apply');assert.ok(apply);
  assert.deepEqual(apply.args.list.anchor_keys,anchors);assert.equal(apply.args.list.remaining,1);
  assert.deepEqual(apply.args.items.map((item:any)=>item.index),[2,3]);
  assert.deepEqual(apply.args.items.map((item:any)=>apply.args.list.rows[item.index-1].anchor_key),['amber','birch']);
 }finally{await f.close();}
});

for(const [label,suffix]of [['BOM','\ufeff'],['NEL','\u0085'],['C0','\u001c']] as const)test(`original owner ${label} follows existing admission normalization`,async()=>{
 const selection={mode:'all_except',correction:'All done except 1',excluded:[{first:1,last:1,correction:'1'}]};
 const f=fixture({selection});try{
  await f.coordinator.receive(inbound('list','Show those tasks'),context);f.phase='cleanup';
  await f.coordinator.receive(inbound('choice',selection.correction+suffix),context);
  const applies=f.calls.filter(row=>row.operation==='apply');
  if(label==='BOM'){
   assert.equal(applies.length,1);assert.equal(applies[0]!.text,selection.correction);
   const admitted=f.journal.db.prepare("SELECT payload FROM inbound_events WHERE source_id='choice'").get()!;
   assert.equal(JSON.parse(String(admitted.payload)).inbound.text,selection.correction);
   return;
  }
  assert.equal(applies.length,0);
  assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE source_event_id=(SELECT id FROM inbound_events WHERE source_id='choice')").get()!.n,0);
 }finally{await f.close();}
});

const singletonList:CleanupList={...list,anchor_keys:['amber'],rows:[{...list.rows[1]!,index:1}],remaining:3};
singletonList.review_hash=cleanupListHash(singletonList);
const singletonSelection={selection:{mode:'singleton',correction:'That one is done too.',reference:'That one'}};
test('accepted singleton remaining-task list supplies target authority for next explicit completion',async()=>{const f=fixture(singletonSelection,singletonList);try{
 await f.coordinator.receive(inbound('remaining','What is left of those three test tasks?'),context);
 assert.equal(f.sent[0],cleanupListText(singletonList));
 f.phase='cleanup';await f.coordinator.receive(inbound('done','That one is done too.'),context);
 const apply=f.calls.find(row=>row.operation==='apply')!;
 assert.ok(apply);assert.deepEqual(apply.args.items,[{index:1,action:'resolve',correction:'That one is done too.'}]);
 assert.equal(apply.args.list.rows[0].anchor_key,'amber');assert.equal(apply.args.proposal.action_id,f.calls[0]!.action_id);
 assert.match(f.sent.at(-1)!,/Marked done/);
 }finally{await f.close();}});
for(const scenario of ['multirow','stale','source','negative','thanks','assistant-prose','injected-label','mismatched-owner'] as const)test(`singleton refuses ${scenario} without contacting a mutation`,async()=>{
 const text=scenario==='negative'?'That one is not done.':scenario==='thanks'?'Thanks for that one.':scenario==='injected-label'?'Dana is done.':'That one is done too.';
 const f=fixture({selection:{mode:'singleton',correction:text,reference:scenario==='thanks'?'that one':scenario==='injected-label'?'Dana':'That one'}},scenario==='multirow'?list:singletonList);try{
 if(scenario==='assistant-prose')f.phase='safe';
 await f.coordinator.receive(inbound('remaining','What remains?'),context);
 if(scenario==='stale'){f.phase='safe';await f.coordinator.receive(inbound('between','Hello'),context);}
 if(scenario==='source')f.revoke();
 f.phase='cleanup';await f.coordinator.receive(inbound('done',scenario==='mismatched-owner'?'Thanks':text),context);
 assert.equal(f.calls.filter(row=>row.operation==='apply').length,0);assert.match(f.sent.at(-1)!,/fresh numbered list/);
 }finally{await f.close();}
});


for(const scenario of ['valid','restart','unrelated','unknown-repair','repair-source','repair-route','new-list','source','yes','thanks','another-owner','late-proof'] as const)test(`typed selection repair retains only its bound list: ${scenario}`,async()=>{
 const selection={...rangeSelection,correction:'Close 1-99',clauses:[{action:'resolve',correction:'Close 1-99',references:[{first:1,last:50,correction:'1-99'}]}]};
 const f=fixture({selection});let changed:ConversationCoordinator|undefined;try{
  await f.coordinator.receive(inbound('list','Show these tasks'),context);
  f.phase='cleanup';await f.coordinator.receive(inbound('repair',selection.correction),context);
  assert.equal(f.calls.filter(row=>row.operation==='apply').length,0);
  if(scenario==='restart'){await f.coordinator.close();f.reopen();}
  if(scenario==='unrelated'){f.phase='safe';await f.coordinator.receive(inbound('between','Hello'),context);}
  if(scenario==='source')f.revoke();
  if(scenario==='new-list'){f.phase='list';await f.coordinator.receive(inbound('new-list','Show current tasks'),context);}
  if(scenario==='repair-source')f.journal.db.prepare("UPDATE prepared_replies SET source_revision=? WHERE event_id=(SELECT id FROM inbound_events WHERE source_id='repair')").run('d'.repeat(64));
  if(scenario==='repair-route')f.journal.db.prepare("UPDATE dispatches SET destination='elsewhere' WHERE id IN (SELECT dispatch_id FROM prepared_replies WHERE event_id=(SELECT id FROM inbound_events WHERE source_id='repair'))").run();
  if(scenario==='unknown-repair'||scenario==='late-proof')f.journal.db.prepare("UPDATE dispatches SET status='unknown' WHERE id IN (SELECT dispatch_id FROM prepared_replies WHERE event_id=(SELECT id FROM inbound_events WHERE source_id='repair'))").run();
  changed=new ConversationCoordinator({journal:f.journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerTaskTool:f.client,model:{generate:async request=>{if(scenario==='late-proof')f.journal.db.prepare("UPDATE dispatches SET status='accepted' WHERE id IN (SELECT dispatch_id FROM prepared_replies WHERE event_id=(SELECT id FROM inbound_events WHERE source_id='repair'))").run();await f.callTool('owner_task_cleanup',{selection:rangeSelection},request);return 'response';}},postPrepared:async request=>{f.sent.push(request.text);return {acceptance:'accepted',messageId:'repaired'};}});
  await changed.receive({...inbound('choice',scenario==='yes'?'yes':scenario==='thanks'?'thanks':rangeSelection.correction),...(scenario==='another-owner'?{senderId:'someone-else'}:{})},context);
  const applied=f.calls.filter(row=>row.operation==='apply');assert.equal(applied.length,scenario==='valid'||scenario==='restart'||scenario==='new-list'?1:0);
  if(scenario==='new-list')assert.equal(applied[0]!.args.proposal.action_id,f.calls.filter(row=>row.operation==='preview').at(-1)!.action_id,'new accepted list supersedes repair snapshot');
 }finally{await changed?.close();await f.close();}
});

for(const scenario of ['valid','question','stale','negation','extra','unknown-delivery'] as const)test(`short numeric done answer requires trusted open-list question: ${scenario}`,async()=>{
 const correction=scenario==='question'?'All? 1–3?':scenario==='negation'?'not 1-3':scenario==='extra'?'1-3 tomorrow':'1-3';
 const token=scenario==='question'?'1–3':'1-3';
 const selection={mode:'explicit',correction,clauses:[{action:'resolve',correction,references:[{first:1,last:3,correction:token}]}]};
 const display=scenario==='stale'?staleList:list;
 const f=fixture({selection},display);try{
  await f.coordinator.receive(inbound('list','Show tasks'),context);
  if(scenario==='unknown-delivery')f.journal.db.prepare("UPDATE dispatches SET status='unknown'").run();
  f.phase='cleanup';await f.coordinator.receive(inbound('choice',correction),context);
  const applied=f.calls.filter(row=>row.operation==='apply');
  assert.equal(applied.length,scenario==='valid'||scenario==='question'?1:0);
  if(applied.length)assert.deepEqual(applied[0]!.args.items.map((item:any)=>item.index),[1,2,3]);
 }finally{await f.close();}
});

test('screenshot numeric answer resolves twenty displayed tasks and no unseen task',async()=>{
 const correction='All? 1–20?';const selection={mode:'explicit',correction,clauses:[{action:'resolve',correction,references:[{first:1,last:20,correction:'1–20'}]}]};
 const display={...list,total:21,remaining:1,anchor_keys:Array.from({length:20},(_,i)=>'shown-'+i),rows:Array.from({length:20},(_,i)=>({...list.rows[0]!,index:i+1,anchor_key:'shown-'+i}))};display.review_hash=cleanupListHash(display);
 const f=fixture({selection},display);try{
  await f.coordinator.receive(inbound('list','Show those twenty tasks'),context);
  f.phase='cleanup';await f.coordinator.receive(inbound('choice',correction),context);
  const applied=f.calls.filter(row=>row.operation==='apply');assert.equal(applied.length,1);assert.equal(applied[0]!.args.items.length,20);assert.deepEqual(applied[0]!.args.list.anchor_keys,display.anchor_keys);assert.equal(applied[0]!.args.list.remaining,1);
 }finally{await f.close();}
});

test('a short numeric question does not mark a task done',async()=>{
 const correction='3?';const selection={mode:'explicit',correction,clauses:[{action:'resolve',correction,references:[{first:3,last:3,correction:'3'}]}]};
 const f=fixture({selection});try{
  await f.coordinator.receive(inbound('list','Show tasks'),context);
  f.phase='cleanup';await f.coordinator.receive(inbound('choice',correction),context);
  assert.equal(f.calls.filter(row=>row.operation==='apply').length,0);
 }finally{await f.close();}
});

const collectiveCases=[
 ...['valid','missing','intervening','source','unknown-delivery','partial'].map(scenario=>({scenario,text:'Close all of them',reference:'all of them',page:false})),
 ...['all of this is done','all of that is done','all of this is not done','all of this might be done','all of this is done, cancel','all of this project is done'].map(text=>({scenario:text,text,reference:text.slice(0,11),page:false})),
 ...['this','that','these','those','bare','them','source','intervening'].map(scenario=>{const reference=scenario==='bare'?'all':`all of ${['source','intervening'].includes(scenario)?'this':scenario}`;return {scenario,text:scenario==='them'?'Close all of them':`${reference} is done`,reference,page:true};})
];
for(const {scenario,text,reference,page} of collectiveCases)test(`collective ${page?'page':'subset'}: ${scenario}`,async()=>{
 const {anchor_keys,...base}=list;
 const display=page?{...base,total:51,remaining:1,rows:Array.from({length:50},(_,i)=>({...list.rows[0]!,index:i+1,anchor_key:`shown-${i}`}))}:scenario==='partial'?base:list;display.review_hash=cleanupListHash(display);
 const f=fixture({selection:{mode:'all',correction:text,reference}},display,page);try{
 if(scenario!=='missing')await f.coordinator.receive(inbound('list','Give me all the open items?'),context);
 if(page){assert.equal(f.calls[0]!.args.anchor_keys,undefined);}
 if(scenario==='source')f.revoke();
 if(scenario==='intervening'){f.phase='safe';await f.coordinator.receive(inbound('between','Hello'),context);}
 if(scenario==='unknown-delivery')f.journal.db.prepare("UPDATE dispatches SET status='unknown'").run();
 f.phase='cleanup';await f.coordinator.receive(inbound('done',text),context);
 const applied=f.calls.filter(row=>row.operation==='apply');
 const valid=page?['this','that','these','those','them'].includes(scenario):scenario==='valid'||/^all of (?:this|that) is done$/.test(text);
 assert.equal(applied.length,valid?1:0);
 if(valid){assert.deepEqual(applied[0]!.args.items.map((item:any)=>item.index),display.rows.map(row=>row.index));assert.deepEqual(applied[0]!.args.list,display);}
 else if(['missing','intervening','unknown-delivery'].includes(scenario))assert.match(f.sent.at(-1)!,/^Open tasks:/);
 }finally{await f.close();}
});
