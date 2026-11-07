import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
const usage={inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}};
test('quote lookup is exact accepted same-thread and unavailable for conflicting, foreign, unaccepted, expired or ambiguous targets',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'quote-journal-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));const requests:any[]=[];let serial=0;
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{requests.push(request);if(request.quotedReply?.status==='found'){const trusted={runId:request.runId,channel:'photon' as const,accountId:'account',ownerId:'owner',lineKey:'line',scopeHash:'a'.repeat(64)};assert.equal(journal.ownerReminderToolAdmission(trusted,request.ownerRequestKey,true).text,'2');assert.throws(()=>journal.prepareOwnerTaskToolAction(trusted,request.ownerRequestKey,{operation:'add',text:request.quotedReply!.status==='found'?request.quotedReply!.text:'never'}));}return {text:'menu-'+requests.length,sourceRevision:'a'.repeat(64)};}},postPrepared:async()=>({acceptance:'accepted',messageId:'sent-'+(++serial)})});
 const receive=(id:string,thread='thread',bound=false,anchor?:string)=>coordinator.receive({channel:'photon',accountId:'account',senderId:'owner',isDirect:true,eventId:id,text:'2',...(bound?{replyBound:true,...(anchor?{replyToMessageId:anchor}:{})}:{})},{threadId:thread,destination:'owner',lineKey:'line'});
 try {
  await receive('first');await receive('newest');await receive('choose-old','thread',true,'sent-1');
  assert.equal(requests.at(-1).text,'2');assert.equal(requests.at(-1).messages[0].text,'2');assert.equal(requests.at(-1).quotedReply.text,'menu-1');
  for(const [id,thread,anchor] of [['foreign','foreign','sent-1'],['missing','thread','missing'],['conflict','thread',undefined]] as const){await receive(id,thread,true,anchor);assert.deepEqual(requests.at(-1).quotedReply,{status:'unavailable'});}
  journal.db.prepare("UPDATE dispatches SET status='unknown' WHERE provider_message_id='sent-1'").run();await receive('unknown','thread',true,'sent-1');assert.deepEqual(requests.at(-1).quotedReply,{status:'unavailable'});
  journal.db.prepare("UPDATE dispatches SET status='accepted',provider_message_ids='not json' WHERE provider_message_id='sent-1'").run();await receive('malformed','thread',true,'sent-1');assert.deepEqual(requests.at(-1).quotedReply,{status:'unavailable'});
  journal.db.prepare("UPDATE dispatches SET provider_message_ids=NULL WHERE provider_message_id='sent-1'").run();journal.db.prepare("UPDATE prepared_replies SET payload='' WHERE dispatch_id IN (SELECT id FROM dispatches WHERE provider_message_id='sent-1')").run();await receive('expired','thread',true,'sent-1');assert.deepEqual(requests.at(-1).quotedReply,{status:'unavailable'});
  journal.db.prepare("UPDATE dispatches SET provider_message_ids='[\"sent-2\"]' WHERE provider_message_id='sent-3'").run();await receive('ambiguous','thread',true,'sent-2');assert.deepEqual(requests.at(-1).quotedReply,{status:'unavailable'});
 }finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
for(const mode of ['revoked','legacy','race','owner-only'] as const)test(`runtime quoted-data authority ${mode}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'quote-runtime-'));let revision='a'.repeat(64),calls=0;
 const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Answer safely',sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:[]})},readMasterContext:async()=>{if(mode==='race')revision='b'.repeat(64);return {text:'',chars:0,cap:8000,prompt:''};},model:new MockLanguageModelV4({doGenerate:async request=>{calls++;const prompt=JSON.stringify(request.prompt);if(mode==='owner-only')assert.ok(prompt.includes('PRIVATE QUOTE'));else{assert.ok(!prompt.includes('PRIVATE QUOTE'));assert.ok(prompt.includes('exact quoted reply is unavailable'));}return {content:[{type:'text',text:'Please restate the choice'}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};}})});
 try {const generation=runtime.generateChat({text:'2',threadId:'fresh',ownerId:'owner',quotedReply:{status:'found',messageId:'id',text:'PRIVATE QUOTE',sourceRevision:mode==='owner-only'?'owner-only-context-v1':mode==='race'?revision:mode==='legacy'?'':'b'.repeat(64)}});if(mode==='race'){await assert.rejects(generation);assert.equal(calls,0);}else{await generation;assert.equal(calls,1);}}
 finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});
test('coalesced anchored inputs expose unavailable rather than latest quote',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'quote-batch-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));let release!:()=>void,entered!:()=>void;
 const waiting=new Promise<void>(done=>{entered=done;});const gate=new Promise<void>(done=>{release=done;});let calls=0;const seen:any[]=[];
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{seen.push(request);if(calls++===0){entered();await gate;}return 'clarify';}},postPrepared:async()=>({acceptance:'accepted',messageId:'ack'})});
 const inbound=(id:string,bound=false)=>({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId:id,text:'2',...(bound?{replyBound:true,replyToMessageId:'old'}:{})});
 const context={threadId:'thread',destination:'owner',lineKey:'line'};
 try {const first=coordinator.receive(inbound('first',true),context);await waiting;const second=coordinator.receive(inbound('second'),context);release();await Promise.all([first,second]);assert.deepEqual(seen.at(-1).quotedReply,{status:'unavailable'});assert.equal(seen.at(-1).messages.length,2);}
 finally{release();await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
