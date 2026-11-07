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
import {relationReviewHash,relationReviewText,taskReceiptMatches,ownerTaskToolArgsSchema, type RelationPreview} from '../src/tools/owner-task-tool-contract.js';
const revision='a'.repeat(64);
const proposal={operation:'propose_relation' as const,mode:'add' as const,canonical_id:'c_abcdef',other_canonical_id:'c_123456',relation_type:'works_with' as const,date:'',source_revision:revision};
const {operation:ignoredOperation,...proposalFields}=proposal;
const review={...proposalFields,name:'Dana Cedar',other_name:'Evan Birch',present:false,relation_version:'b'.repeat(64),review_hash:''};const trustedReview={...review,operation:'relation' as const};trustedReview.review_hash=relationReviewHash(trustedReview);
const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:revision};
const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:'owner'};
const context={threadId:'thread',destination:'photon',lineKey:'line'};
const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId,text});
function fixture(){
 const folder=mkdtempSync(join(tmpdir(),'relation-review-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));let previews=0,applies=0,lose=false,phase='preview';const sent:string[]=[];
 const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async(_url,init)=>{const body=JSON.parse(String(init?.body));if(body.operation==='preview'){previews++;if(lose)throw Error('synthetic lost read');return new Response(JSON.stringify({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review:trustedReview}));}applies++;return new Response(JSON.stringify({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:journal.getAction(body.action_id)!.payload_hash,operation:'relation',mode:'add',canonical_id:proposal.canonical_id,other_canonical_id:proposal.other_canonical_id,relation_type:'works_with',date:'',review_hash:trustedReview.review_hash,changed:true,post_digest:'c'.repeat(64)}));}});
 const execution=createOwnerTaskExecution({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:[]})}});
 const command=createTaskCommand(execution);
 const coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerTaskTool:client,model:{generate:async request=>{const authority={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerTask:request.allowOwnerTask,sourceRevision:revision,signal:request.signal};if(phase==='preview'){const first=await execution.execute(proposal,authority),replayed=await execution.execute(proposal,authority);assert.deepEqual(replayed,first);if('review_text' in first){assert.equal(first.review_text,relationReviewText(trustedReview));assert.equal(first.needs_owner_confirmation,true);}else assert.fail('canonical preview missing');}else if(phase==='cross-kind'){const admission={runId:request.runId,channel:'photon' as const,accountId:'account',ownerId:'owner',lineKey:'line',scopeHash:client.scopeHash};journal.prepareOwnerGmailDraftAction(admission,request.ownerRequestKey!,{operation:'propose',message_id:'message-1',body:'Exact separate draft'},revision);await assert.rejects(command({id:'knowledge-edit',args:['--op','confirm-relation']},authority),/another private mutation/);}else if(phase==='wrong-family')await assert.rejects(command({id:'knowledge-edit',args:['--op','confirm-identifier']},authority),/review unavailable/);else if(phase==='confirm')await command({id:'knowledge-edit',args:['--op','confirm-relation']},authority);return 'Model prose';}},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'accepted-'+sent.length};}});
 return {journal,coordinator,execution,client,sent,get previews(){return previews;},get applies(){return applies;},set phase(value:string){phase=value;},set lose(value:boolean){lose=value;},close:async()=>{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}};
}
test('relation preview preserves accepted review, separate yes and no repeated mutation',async()=>{const f=fixture();try{assert.equal((await f.coordinator.receive(inbound('proposal','Dana works with Evan'),context)).status,'accepted');assert.deepEqual(f.sent,[relationReviewText(trustedReview)]);assert.equal(f.applies,0);f.phase='confirm';const confirmed=await f.coordinator.receive(inbound('yes','Yes.'),context);assert.equal(confirmed.status,'accepted',JSON.stringify(confirmed));assert.equal(f.applies,1);assert.equal(f.sent.at(-1),'Relationship added.');assert.equal((await f.coordinator.receive(inbound('yes','Yes.'),context)).status,'duplicate');assert.equal(f.applies,1);}finally{await f.close();}});
test('intervening input and wrong explicit reply cannot consume relationship review',async()=>{for(const wrongReply of [false,true]){const f=fixture();try{await f.coordinator.receive(inbound('proposal','Dana works with Evan'),context);if(!wrongReply){f.phase='safe';await f.coordinator.receive(inbound('between','Hello'),context);}f.phase='confirm';await f.coordinator.receive({...inbound('yes','Yes.'),...(wrongReply?{replyBound:true,replyToMessageId:'wrong'}:{})},context);assert.equal(f.applies,0);}finally{await f.close();}}});
test('lost read-only preview stays pending, not uncertain mutation',async()=>{const f=fixture();try{f.lose=true;await f.coordinator.receive(inbound('proposal','Dana works with Evan'),context);assert.equal(f.applies,0);assert.deepEqual(f.journal.db.prepare('SELECT status FROM actions').all().map(row=>row.status),['pending']);}finally{await f.close();}});
test('closed original command and changed/no-op receipt binding',()=>{
 assert.deepEqual(parseTaskCorrectionCommand(['--op','relation-add','--slug','c_abcdef','--other-slug','c_123456','--type','works_with'],revision),proposal);
 assert.throws(()=>parseTaskCorrectionCommand(['--op','relation-remove','--slug','c_abcdef','--other-slug','c_123456','--type','works_with','--date','2026-10-10'],revision));
 assert.equal(ownerTaskToolArgsSchema.safeParse({...proposal,date:'2026-02-30'}).success,false);
 const args={operation:'relation' as const,review:trustedReview,proposal:{event_id:revision,action_id:revision,args_hash:revision,accepted_dispatch_id:'dispatch',source_revision:revision}};
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,operation:'relation' as const,mode:'add' as const,canonical_id:proposal.canonical_id,other_canonical_id:proposal.other_canonical_id,relation_type:'works_with' as const,date:'',review_hash:trustedReview.review_hash,changed:true,post_digest:revision};assert.ok(taskReceiptMatches(receipt,args));assert.equal(taskReceiptMatches({...receipt,changed:false},args),false);
});


test('shared Unicode hash vector and full escaped Unicode preview remain exact',async()=>{
 const vector={operation:'relation' as const,mode:'add' as const,canonical_id:'c_a1b2c3',other_canonical_id:'c_123456',name:'Dana 李',other_name:'Vishnu é',relation_type:'introduced_by' as const,date:'2026-01-02',present:false,relation_version:'0'.repeat(64),source_revision:revision,review_hash:''};
 assert.equal(relationReviewHash(vector),'dec620dd77289f1a438680bd1ca851d89ecfdfaa3071d04d0ca84f1086fda30e');
 const large={...trustedReview,name:'😀'.repeat(500),other_name:'𐀀'.repeat(500)};large.review_hash=relationReviewHash(large);
 const {stableActionIdentity}=await import('../src/tools/action-gate.js');const {ownerTaskToolKind}=await import('../src/tools/owner-task-tool-contract.js');const binding=stableActionIdentity(revision,ownerTaskToolKind,proposal);
 const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async()=>{const encoded=JSON.stringify({version:1,status:'preview',event_id:revision,payload_hash:revision,review:large}).replace(/[^\x00-\x7f]/g,char=>'\\u'+char.charCodeAt(0).toString(16).padStart(4,'0'));assert.ok(Buffer.byteLength(encoded)>8192);return new Response(encoded);}});
 const result=await client.call('preview',{actionId:binding.actionId,eventId:revision,payloadHash:revision,text:'Add this exact relationship',admittedAtMs:Date.now(),providerThreadId:'thread',args:proposal});assert.equal(result.status,'preview');if(result.status==='preview'&&result.review.operation==='relation')assert.equal(relationReviewText(result.review).includes(large.name),true);
});

test('a same-event admitted Gmail action prevents relationship confirmation contact',async()=>{const f=fixture();try{await f.coordinator.receive(inbound('proposal','Dana works with Evan'),context);f.phase='cross-kind';await f.coordinator.receive(inbound('yes','Yes.'),context);assert.equal(f.applies,0);assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_gmail_draft_tool'").get()!.n,1);}finally{await f.close();}});

test('relationship review cannot authorize identifier confirmation',async()=>{const f=fixture();try{await f.coordinator.receive(inbound('proposal','Dana works with Evan'),context);f.phase='wrong-family';await f.coordinator.receive(inbound('yes','Yes.'),context);assert.equal(f.applies,0);}finally{await f.close();}});
