import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createOwnerTaskToolClient} from '../src/tools/owner-task-tool-client.js';
import {createOwnerTaskExecution} from '../src/tools/owner-task-execution.js';
import {createTaskCommand,parseTaskCorrectionCommand} from '../src/tools/task-command.js';
import {identifierReviewHash,taskReviewText,taskReceiptMatches,ownerTaskToolArgsSchema, type TaskPreview} from '../src/tools/owner-task-tool-contract.js';
const revision='a'.repeat(64);
const proposal={operation:'propose_identifier' as const,canonical_id:'c_abcdef',identifier:'Dana.New@Example.com',source_revision:revision};
const {operation:ignoredOperation,...proposalFields}=proposal;
const review={...proposalFields,name:'Dana Cedar',present:false,normalized_identifier:'dana.new@example.com',review_hash:''};const trustedReview={...review,operation:'identifier' as const};trustedReview.review_hash=identifierReviewHash(trustedReview);
const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:revision};
const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:'owner'};
const context={threadId:'thread',destination:'photon',lineKey:'line'};
const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId,text});
function fixture(){
 const folder=mkdtempSync(join(tmpdir(),'identifier-review-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));let previews=0,applies=0,lose=false,phase='preview';const sent:string[]=[];
 const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async(_url,init)=>{const body=JSON.parse(String(init?.body));if(body.operation==='preview'){previews++;if(lose)throw Error('synthetic lost read');return new Response(JSON.stringify({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review:trustedReview}));}applies++;return new Response(JSON.stringify({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:journal.getAction(body.action_id)!.payload_hash,operation:'identifier',canonical_id:proposal.canonical_id,identifier:proposal.identifier,review_hash:trustedReview.review_hash,added:true,post_digest:'c'.repeat(64)}));}});
 const execution=createOwnerTaskExecution({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:[]})}});
 const command=createTaskCommand(execution);
 const coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerTaskTool:client,model:{generate:async request=>{const authority={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerTask:request.allowOwnerTask,sourceRevision:revision,signal:request.signal};if(phase==='preview'){const first=await execution.execute(proposal,authority),replayed=await execution.execute(proposal,authority);assert.deepEqual(replayed,first);assert.equal(first.status,'proposed');if('review_text' in first){assert.equal(first.review_text,taskReviewText(trustedReview));assert.equal(first.needs_owner_confirmation,true);assert.match(first.instruction,/assistant response containing review_text exactly, without more tools/);}else assert.fail('canonical preview missing');}else if(phase==='cross-kind'){const admission={runId:request.runId,channel:'photon' as const,accountId:'account',ownerId:'owner',lineKey:'line',scopeHash:client.scopeHash};journal.prepareOwnerGmailDraftAction(admission,request.ownerRequestKey!,{operation:'propose',message_id:'message-1',body:'Exact separate draft'},revision);await assert.rejects(command({id:'knowledge-edit',args:['--op','confirm-identifier']},authority),/another private mutation/);}else if(phase==='wrong-family')await assert.rejects(command({id:'knowledge-edit',args:['--op','confirm-relation']},authority),/review unavailable/);else if(phase==='confirm')await command({id:'knowledge-edit',args:['--op','confirm-identifier']},authority);return 'Model prose';}},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'accepted-'+sent.length};}});
 return {journal,coordinator,execution,client,sent,folder,get previews(){return previews;},get applies(){return applies;},set phase(value:string){phase=value;},set lose(value:boolean){lose=value;},close:async()=>{await coordinator.close();try{journal.close();}catch{}rmSync(folder,{recursive:true,force:true});}};
}
test('identifier preview preserves accepted review, separate yes and no repeated mutation',async()=>{const f=fixture();try{assert.equal((await f.coordinator.receive(inbound('proposal','Dana uses Dana.New@Example.com'),context)).status,'accepted');assert.deepEqual(f.sent,[taskReviewText(trustedReview)]);assert.equal(f.applies,0);assert.equal(f.previews,1);f.phase='confirm';const confirmed=await f.coordinator.receive(inbound('yes','Yes.'),context);assert.equal(confirmed.status,'accepted',JSON.stringify(confirmed));assert.equal(f.applies,1);assert.equal(f.sent.at(-1),'Identifier added.');assert.equal((await f.coordinator.receive(inbound('yes','Yes.'),context)).status,'duplicate');assert.equal(f.applies,1);}finally{await f.close();}});
test('intervening input and wrong explicit reply cannot consume identifier review',async()=>{for(const wrongReply of [false,true]){const f=fixture();try{await f.coordinator.receive(inbound('proposal','Dana uses Dana.New@Example.com'),context);if(!wrongReply){f.phase='safe';await f.coordinator.receive(inbound('between','Hello'),context);}f.phase='confirm';await f.coordinator.receive({...inbound('yes','Yes.'),...(wrongReply?{replyBound:true,replyToMessageId:'wrong'}:{})},context);assert.equal(f.applies,0);}finally{await f.close();}}});
test('lost read-only preview stays pending, not uncertain mutation',async()=>{const f=fixture();try{f.lose=true;await f.coordinator.receive(inbound('proposal','Dana uses Dana.New@Example.com'),context);assert.equal(f.applies,0);assert.deepEqual(f.journal.db.prepare('SELECT status FROM actions').all().map(row=>row.status),['pending']);}finally{await f.close();}});

test('identifier review cannot authorize relationship confirmation',async()=>{const f=fixture();try{await f.coordinator.receive(inbound('proposal','Dana uses Dana.New@Example.com'),context);f.phase='wrong-family';await f.coordinator.receive(inbound('yes','Yes.'),context);assert.equal(f.applies,0);}finally{await f.close();}});
test('shared Unicode review hash binds raw and normalized identifiers and duplicate status',()=>{
 const vector={operation:'identifier' as const,canonical_id:'c_a1b2c3',name:'Dana 李',identifier:'DANA@example.com',normalized_identifier:'dana@example.com',present:false,source_revision:revision,review_hash:''};
 assert.equal(identifierReviewHash(vector),'1f079c31817e13bd58a62b4023c540baaa69434b98d660cedf6131dfead4536d');
 const args={operation:'identifier' as const,review:trustedReview,proposal:{event_id:revision,action_id:revision,args_hash:revision,accepted_dispatch_id:'dispatch',source_revision:revision}};
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,operation:'identifier' as const,canonical_id:proposal.canonical_id,identifier:proposal.identifier,added:true,review_hash:trustedReview.review_hash,post_digest:revision};
 assert.ok(taskReceiptMatches(receipt,args));assert.equal(taskReceiptMatches({...receipt,added:false},args),false);assert.equal(taskReceiptMatches({...receipt,identifier:trustedReview.normalized_identifier},args),false);
});
test('historical legacy committed/contacting records recover across restart; absent pending never applies',async()=>{
 const {stableActionIdentity}=await import('../src/tools/action-gate.js');const {ownerTaskToolKind}=await import('../src/tools/owner-task-tool-contract.js');
 for(const status of ['succeeded','unknown','pending']){const f=fixture();let reopened:OperationalJournal|undefined,recovery:ConversationCoordinator|undefined;try{
  await f.coordinator.receive(inbound('historical','Dana uses Dana.New@Example.com'),context);
  const row=f.journal.db.prepare('SELECT * FROM actions').get()!;
  const legacy={operation:'add_identifier' as const,canonical_id:proposal.canonical_id,identifier:proposal.identifier,source_revision:revision};
  const binding=stableActionIdentity(row.source_event_id as string,ownerTaskToolKind,legacy);
  const receipt={version:1 as const,status:'committed' as const,action_id:binding.actionId,event_id:row.source_event_id as string,payload_hash:row.source_payload_hash as string,args_hash:binding.payloadHash,operation:'add_identifier' as const,canonical_id:legacy.canonical_id,identifier:legacy.identifier,added:true,post_digest:revision};
  // Retained v64 bytes at the journal boundary; no fresh-admission bypass.
  f.journal.db.prepare("UPDATE inbound_events SET outcome='pending' WHERE id=?").run(row.source_event_id);
  f.journal.db.prepare('UPDATE actions SET id=?,payload_hash=?,status=?,result_json=? WHERE id=?').run(binding.actionId,binding.payloadHash,status,JSON.stringify({args:legacy,scope_hash:f.client.scopeHash,...(status==='succeeded'?{receipt}:{})}),row.id);
  f.journal.db.prepare('DELETE FROM prepared_replies WHERE event_id=?').run(row.source_event_id);
  await f.coordinator.close();f.journal.close();reopened=new OperationalJournal(join(f.folder,'journal.sqlite'));reopened.recoverInterrupted();
  let inspections=0,applies=0,modelAttempts=0;const replies:string[]=[];
  const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async(_url,init)=>{const body=JSON.parse(String(init?.body));if(body.operation==='apply'){applies++;throw Error('Legacy apply forbidden');}assert.equal(body.operation,'inspect');inspections++;return new Response(JSON.stringify(status==='pending'?{version:1,status:'not_found'}:receipt));}});
  const authority={channel:'photon' as const,accountId:'account',ownerId:'owner',lineKey:'line',scopeHash:client.scopeHash};
  const saved=reopened.readOwnerTaskToolAction(binding.actionId,authority);assert.equal(saved.status,status);assert.deepEqual(saved.receipt,status==='succeeded'?receipt:null);
  const execution=createOwnerTaskExecution({client,journal:()=>reopened,sourceAuthority:{read:async()=>({version:2,complete:false,revision:null,bridge_sources:[],google_sources:[]})}});
  await assert.rejects(execution.execute(legacy,{mode:'chat',runId:row.run_id,ownerRequestKey:row.source_event_id,allowOwnerTask:true}),/accepted review/);
  recovery=new ConversationCoordinator({journal:reopened,ownerPolicy,ownerTaskTool:client,model:{generate:async()=>{modelAttempts++;throw Error('No model replay');}},postPrepared:async r=>{replies.push(r.text);return {acceptance:'accepted',messageId:'historical-ack'};}});
  if(status==='pending'){
    assert.equal((await client.call('inspect',saved.request)).status,'not_found');
    await recovery.recover();
    assert.equal(reopened.getAction(binding.actionId)!.status,'pending');
    assert.equal(modelAttempts,1,'only the pending case reaches ordinary model handling');
    assert.equal(replies.length,1);
    assert.match(replies[0]!,/model request failed or took too long/);
    assert.doesNotMatch(replies[0]!,/No model replay|Dana.New|Identifier added/);
    await recovery.recover();
    assert.equal(modelAttempts,1,'accepted fixed notice is not regenerated');
    assert.equal(replies.length,1);
  }
  else {await recovery.recover();assert.equal(reopened.getAction(binding.actionId)!.status,'succeeded');assert.deepEqual(replies,['Identifier added.']);assert.equal(inspections,status==='unknown'?1:0);assert.equal(modelAttempts,0);}
  assert.equal(applies,0);
 }finally{await recovery?.close();reopened?.close();await f.close();}}
});
