import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {RequestContext} from '@mastra/core/request-context';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createOwnerTaskToolClient} from '../src/tools/owner-task-tool-client.js';
import {createOwnerTaskTools} from '../src/tools/owner-task-tool.js';
import {cleanupSelectionsMatch,cleanupItemsSchema,ownerTaskToolArgsSchema,explicitLoopThreadSelection} from '../src/tools/owner-task-tool-contract.js';

const mixed=[{index:2,action:'keep' as const,correction:'keep 2'},{index:5,action:'resolve' as const,correction:'5'}];
test('new mixed resolves are refused while legacy stored argument shapes remain readable',()=>{
 assert.ok(cleanupItemsSchema.safeParse(mixed).success,'retained wire shape remains valid');
 assert.equal(cleanupSelectionsMatch(mixed,'keep 2 and 5'),false);
 assert.equal(cleanupSelectionsMatch(mixed,'keep 2 and 5',true),true,'only saved action reads opt into the old grammar');
 for(const [action,correction,days]of [['dismiss','dismiss 2',undefined],['snooze','snooze 2 for 7 days',7]] as const){
  assert.equal(cleanupSelectionsMatch([{index:2,action,correction,...(days?{days}:{})},mixed[1]!],correction+' and 5'),false);
 }
 assert.equal(cleanupSelectionsMatch([{index:2,action:'keep',correction:'keep 2'},{index:5,action:'keep',correction:'keep 5'}],'keep 2 and keep 5'),true);
 assert.equal(cleanupSelectionsMatch([{index:2,action:'dismiss',correction:'dismiss 2'},{index:5,action:'snooze',correction:'snooze 5 for 7 days',days:7}],'dismiss 2 and snooze 5 for 7 days'),true);
 assert.equal(ownerTaskToolArgsSchema.safeParse({operation:'resolve_many',source_revision:'a'.repeat(64),selection_mode:'named_people',items:[{name:'Flash text thread',correction:'Flash text thread'}]}).success,true);
});

test('stored all-resolve compatibility retains full-text negation and exact-set guards',()=>{
 const items=[{index:1,action:'resolve' as const,correction:'1'},{index:3,action:'resolve' as const,correction:'3'}];
 for(const text of ['1 and 3 are not done, 5 is still open','1 and 3 are done. Close those 3.','1 and 3 are done, 3 is still open','1 and 3 are done. Cancel that.'])assert.equal(cleanupSelectionsMatch(items,text,true),false,text);
});

test('explicit thread labels retain their full-label selection requirement',()=>{
 for(const phrase of ['Flash text thread','Flash chat thread','Flash thread',"Flash text thread's reply"])assert.ok(explicitLoopThreadSelection(phrase),phrase);
 for(const phrase of ['Flash','Flash Thompson',"Flash's reply"])assert.equal(explicitLoopThreadSelection(phrase),false,phrase);
});

for(const [phrase,rowName,expected]of [['Flash text thread','Flash Thompson',false],['Flash chat thread','Flash Thompson',false],['Flash thread','Flash Thompson',false],['Flash text thread','FLASH‼',true],['Flash','Flash Thompson',true]] as const)test(`runtime forced anchor: ${phrase} selects ${rowName}: ${expected}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'task-thread-admission-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));
 const revision='a'.repeat(64),proof='b'.repeat(64);const calls:Record<string,any>[]=[];
 const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:revision};
 const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async(_url,init)=>{const body=JSON.parse(String(init?.body));calls.push(body);const item=body.args.items[0];assert.equal(Boolean(item.anchor_key),expected);return new Response(JSON.stringify({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:journal.getAction(body.action_id)!.payload_hash,operation:'resolve_many',outcome:expected?'complete':'partial',items:[expected?{index:1,action:'resolve',anchor_key:'person',status:'applied',post_digest:proof,loop_status:'resolved'}:{index:1,action:'resolve',anchor_key:'',status:'refused',reason:'target_unavailable'}]}));}});
 const row={anchor_key:'person',loop_version:proof,loop_identity:proof,name:rowName,what:'Return the deck',channel:'email',identifier:'flash@example.com',thread_id:'thread',group_id:'',action_type:'follow_up',age_days:1,deadline:null,overdue:false,chased_count:0,chase_pending:null,last_chased_at:null,chase_after:null,chased_out:false,handoff_asked_at:null};
 client.readLoops=async()=>({coverage:'currently_permitted_canonical_loops',you_owe:[row],waiting_on_them:[],counts:{you_owe:1,waiting_on_them:0}});
 client.readback=async()=>({you_owe:expected?0:1,waiting_on_them:0,source_revision:revision,remaining:expected?[]:[row]});
 const tools=createOwnerTaskTools({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision,bridge_sources:[],google_sources:['gmail']})}});
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerTaskTool:client,model:{generate:async request=>{const requestContext=new RequestContext();for(const [key,value]of Object.entries({mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerTask:request.allowOwnerTask,sourceRevision:revision}))requestContext.set(key,value);await tools.owner_task_resolve_many.execute!({selection_mode:'named_people',items:[{name:phrase,correction:phrase,anchor_key:'person',loop_version:proof,loop_identity:proof}]},{requestContext,abortSignal:request.signal} as never);return 'safe';}},postPrepared:async()=>({acceptance:'accepted',messageId:'ack'})});
 try{await coordinator.receive({channel:'photon',accountId:'account',senderId:'owner',isDirect:true,eventId:'completion',text:phrase+' is handled'},{threadId:'thread',destination:'photon',lineKey:'line'});assert.equal(calls.length,1);}finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

import {stableActionIdentity} from '../src/tools/action-gate.js';
import {cleanupListHash,cleanupListText,ownerTaskToolKind,type CleanupList,type OwnerTaskToolArgs} from '../src/tools/owner-task-tool-contract.js';

const historicalSelections=[
 {label:'mixed',text:'keep 2 and 5',items:mixed},
 {label:'all-resolve carve-out',text:'1 and 3 are done, 5 is still open',items:[{index:1,action:'resolve' as const,correction:'1'},{index:3,action:'resolve' as const,correction:'3'}]},
 {label:'all-resolve counted restatement',text:'1 and 3 are done. Close those 2.',items:[{index:1,action:'resolve' as const,correction:'1'},{index:3,action:'resolve' as const,correction:'3'}]},
];
for(const {label,text,items}of historicalSelections)for(const historicalStatus of ['succeeded','unknown'] as const)test(`historical ${historicalStatus} ${label} action remains readable but cannot be newly admitted`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'legacy-mixed-task-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));
 const revision='a'.repeat(64),proof='b'.repeat(64);const calls:Record<string,any>[]=[];let phase='list';let inspected=false;
 const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:revision};
 const list:CleanupList={operation:'cleanup_list',source_revision:revision,total:5,remaining:0,review_hash:'',rows:Array.from({length:5},(_,i)=>({index:i+1,anchor_key:'task-'+(i+1),name:'Owner',what:'Task '+(i+1),direction:'you_owe',age_days:10,times_surfaced:0,surface_count_provenance:'accepted_delivery',overdue:false,deadline:null,chased_count:0,chased_out:false,suggestion:'do it or dismiss',loop_version:proof,loop_identity:proof}))};list.review_hash=cleanupListHash(list);
 const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async(_url,init)=>{const body=JSON.parse(String(init?.body));calls.push(body);assert.notEqual(body.operation,'apply','saved action must never apply again');if(body.operation==='inspect')return new Response(JSON.stringify({version:1,status:'not_found'}));return new Response(JSON.stringify({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review:list}));}});
 client.readback=async()=>({you_owe:4,waiting_on_them:0,source_revision:revision});
 const tools=createOwnerTaskTools({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision,bridge_sources:[],google_sources:[]})}});
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerTaskTool:client,model:{generate:async request=>{
  if(phase==='list'){const requestContext=new RequestContext();for(const [key,value]of Object.entries({mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerTask:request.allowOwnerTask,sourceRevision:revision}))requestContext.set(key,value);await tools.owner_task_cleanup_list.execute!({},{requestContext,abortSignal:request.signal} as never);return cleanupListText(list);}
  const authority={runId:request.runId!,accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon' as const,scopeHash:client.scopeHash};
  const eventId=request.ownerRequestKey!;const parent=journal.getAction(calls[0]!.action_id)!;const reply=journal.getReplyForEvent(calls[0]!.event_id)!;
  const args=ownerTaskToolArgsSchema.parse({operation:'cleanup',list,items,proposal:{event_id:calls[0]!.event_id,action_id:parent.id,args_hash:parent.payload_hash,accepted_dispatch_id:reply.dispatch_id,source_revision:revision}}) as Extract<OwnerTaskToolArgs,{operation:'cleanup'}>;
  // Recreate the exact durable shape written by the old admission code, without invoking an effect.
  assert.equal(cleanupSelectionsMatch(items,text),false,'new admission remains strict');
  const seed=journal.prepareOwnerTaskToolAction(authority,eventId,{operation:'add',text:items[0]!.correction});
  const metadata=JSON.parse(journal.getAction(seed.actionId)!.result_json as string);const binding=stableActionIdentity(eventId,ownerTaskToolKind,args);
  const receipt={version:1,status:'committed',action_id:binding.actionId,event_id:eventId,payload_hash:seed.payloadHash,args_hash:binding.payloadHash,operation:'cleanup',review_hash:list.review_hash,outcome:'complete',items:items.map(item=>({index:item.index,action:item.action,anchor_key:'task-'+item.index,status:'applied',post_digest:proof,loop_status:item.action==='resolve'?'resolved':'open'}))};
  journal.db.prepare('UPDATE actions SET id=?,payload_hash=?,status=?,result_json=? WHERE id=?').run(binding.actionId,binding.payloadHash,historicalStatus,JSON.stringify({...metadata,args,...(historicalStatus==='succeeded'?{receipt}:{})}),seed.actionId);
  const before=journal.db.prepare('SELECT * FROM actions ORDER BY id').all();
  const saved=journal.readOwnerTaskToolAction(binding.actionId,authority);assert.equal(saved.status,historicalStatus);assert.deepEqual(saved.request.args,args);assert.deepEqual(saved.receipt,historicalStatus==='succeeded'?receipt:null);
  assert.deepEqual(journal.db.prepare('SELECT * FROM actions ORDER BY id').all(),before,'stored reader cannot write');
  assert.throws(()=>journal.prepareOwnerTaskToolAction(authority,eventId,args),/Cleanup selection unavailable/);
  inspected=true;return 'Retained outcome inspected.';
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'ack'})});
 const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId,text});const context={threadId:'thread',destination:'photon',lineKey:'line'};
 try{await coordinator.receive(inbound('list','Show stale loops'),context);phase='stored';await coordinator.receive(inbound('choice',text),context);assert.ok(inspected,'stored reader assertions must finish despite coordinator exception handling');assert.equal(calls.filter(call=>call.operation==='preview').length,1);assert.equal(calls.filter(call=>call.operation==='apply').length,0);}finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
