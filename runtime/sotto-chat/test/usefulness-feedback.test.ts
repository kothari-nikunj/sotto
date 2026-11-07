import assert from 'node:assert/strict';
import test from 'node:test';
import {createHash} from 'node:crypto';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator,type ModelRequest} from '../src/conversation.js';
import {createOwnerPreferenceExecution,type OwnerPreferenceExecutionContext} from '../src/tools/owner-preference-execution.js';
import {createPreferenceCommand,parseUsefulnessCommand} from '../src/tools/preference-command.js';
import {createOwnerPreferenceToolClient} from '../src/tools/owner-preference-tool-client.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';
import {ownerPreferenceToolKind,preferenceReceiptMatches,preferenceAcknowledgement} from '../src/tools/owner-preference-tool-contract.js';
const revision='a'.repeat(64),reference='2026-10-05_morning';
const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:revision};
const listArgs={operation:'usefulness-list' as const,source_revision:revision};
const binding=stableActionIdentity(revision,ownerPreferenceToolKind,listArgs);
const request={actionId:binding.actionId,eventId:revision,payloadHash:revision,text:'That item was useful.',admittedAtMs:Date.now(),providerThreadId:'thread',args:listArgs};
const listed={version:1,status:'usefulness_list',event_id:revision,payload_hash:revision,source_revision:revision,items:[{reference,text:'School deadline. Donation item.',archive_version:'b'.repeat(64)}]};
test('original usefulness argv is closed; whole output and excerpt ratings preserve reason discard',async()=>{
 assert.equal(parseUsefulnessCommand(['list']),null);
 assert.deepEqual(parseUsefulnessCommand(['not_useful','--reference',reference]),{rating:'not_useful',reference,excerpt:''});
 assert.deepEqual(parseUsefulnessCommand(['useful','--reference',reference,'--excerpt',' School deadline. ','--reason','why']),{rating:'useful',reference,excerpt:'School deadline.'});
 for(const argv of [[],['list','extra'],['useful'],['useful','--reference',reference,'--version',revision],['useful','--reference',reference,'--reference',reference],['useful','--reference',reference,'--excerpt','😀'.repeat(321)],['useful','--reference',reference,'--reason','x'.repeat(901)]])assert.throws(()=>parseUsefulnessCommand(argv));
 let received:unknown;
 const command=createPreferenceCommand({execution:{rateUsefulness:async(input:unknown)=>{received=input;return {status:'recorded'};}} as never});
 await command({id:'usefulness-feedback',args:['useful','--reference',reference,'--reason','private reason']},{});
 assert.deepEqual(received,{rating:'useful',reference,excerpt:''});
});
test('authenticated usefulness list accepts its scoped 1MiB bound and rejects changed/malformed proof',async()=>{
 const call=async(value:unknown)=>createOwnerPreferenceToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async()=>new Response(JSON.stringify(value))}).call('read',request);
 assert.equal((await call({...listed,items:[{...listed.items[0],text:'x'.repeat(600*1024)}]})).status,'usefulness_list');
 for(const bad of [{...listed,source_revision:'c'.repeat(64)},{...listed,event_id:'c'.repeat(64)},{...listed,items:[...listed.items,...listed.items]},{...listed,items:[{...listed.items[0],archive_version:'invalid'}]},{...listed,items:[{...listed.items[0],text:'x'.repeat(1100*1024)}]}])await assert.rejects(call(bad));
});
test('rating receipt binds reference, decision and original excerpt digest; whole-brief feedback remains supported',()=>{
 const excerpt='School deadline.';
 const args={operation:'usefulness' as const,rating:'useful' as const,reference,archive_version:revision,excerpt,source_revision:revision};
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,outcome:{operation:'usefulness' as const,reference,item_locator:`excerpt:0:${excerpt.length}:${createHash('sha256').update(excerpt).digest('hex').slice(0,20)}`,rating:'useful' as const,status:'recorded' as const}};
 assert.ok(preferenceReceiptMatches(receipt,args));
 for(const changed of [{reference:'2026-10-04_evening'},{rating:'not_useful' as const},{item_locator:'output'},{item_locator:'excerpt:0:2:'+'b'.repeat(20)}])assert.equal(preferenceReceiptMatches({...receipt,outcome:{...receipt.outcome,...changed}},args),false);
 assert.ok(preferenceReceiptMatches({...receipt,outcome:{...receipt.outcome,item_locator:'output'}},{...args,excerpt:''}));
 assert.equal(preferenceAcknowledgement(receipt),'Recorded that as useful.');
});
for(const scenario of ['no_list','malformed_list','foreign_list','changed_revision','grouped','cancel_final_source','saved_unknown'] as const)test(`trusted usefulness execution fence: ${scenario}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'usefulness-proof-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
 let contacts=0,applies=0,revisionReads=0,runId='',refused=0;
 const client=createOwnerPreferenceToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async(_url,init)=>{
  contacts++;const body=JSON.parse(String(init!.body));
  if(body.operation==='read')return new Response(JSON.stringify({...listed,event_id:body.event_id,payload_hash:body.payload_hash}));
  if(body.operation==='inspect')return new Response(JSON.stringify({version:1,status:'unknown'}));
  applies++;return new Response(JSON.stringify({version:1,status:'unknown'}));
 }});
 const handler=createOwnerPreferenceExecution({client,journal:()=>journal,sourceAuthority:{read:async()=>{
  revisionReads++;if(scenario==='cancel_final_source'&&revisionReads===2)journal.cancelRun(runId);
  return {version:2,complete:true,revision,bridge_sources:[],google_sources:['gmail']};
 }}});
 const command=createPreferenceCommand({execution:handler});
 const c=new ConversationCoordinator({journal,ownerPreferenceTool:client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async(req:ModelRequest)=>{
  runId=req.runId;const ctx:OwnerPreferenceExecutionContext={mode:'chat',runId,ownerRequestKey:req.ownerRequestKey,allowOwnerPreference:scenario!=='grouped',sourceRevision:revision,setUsefulnessList:value=>{ctx.usefulnessList=value;}};
  if(scenario==='cancel_final_source'){await assert.rejects(command({id:'usefulness-feedback',args:['list']},ctx));refused++;}
  else {
   if(scenario!=='no_list'&&scenario!=='malformed_list')await command({id:'usefulness-feedback',args:['list']},ctx);
   if(scenario==='malformed_list')ctx.usefulnessList={status:'usefulness_list'};
   if(scenario==='foreign_list')ctx.usefulnessList={...(ctx.usefulnessList as object),event_id:'c'.repeat(64)};
   if(scenario==='changed_revision')ctx.sourceRevision='c'.repeat(64);
   const rating=command({id:'usefulness-feedback',args:['useful','--reference',reference,'--excerpt','School deadline.']},ctx);
   if(['no_list','malformed_list','foreign_list','changed_revision'].includes(scenario))
    await assert.rejects(rating,{message:'Read the current usefulness list before rating: run_sotto_script id "usefulness-feedback" with args ["list"] in this owner turn.'});
   else await assert.rejects(rating);
   refused++;
   if(scenario==='saved_unknown'){
    const prior=revisionReads;ctx.sourceRevision='c'.repeat(64);ctx.usefulnessList=undefined;
    await assert.rejects(command({id:'usefulness-feedback',args:['useful','--reference',reference,'--excerpt','School deadline.']},ctx));assert.equal(revisionReads,prior);assert.equal(applies,1);
   }
  }
  return {text:'Clarify the item.',sourceRevision:revision};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});
 try{
  await c.receive({channel:'photon',accountId:'account',senderId:'owner',isDirect:true,eventId:'rating',text:'The school deadline was useful.'},{threadId:'thread',lineKey:'line',destination:'owner'});
  assert.equal(refused,1);assert.equal(applies,scenario==='saved_unknown'?1:0);
  if(scenario==='no_list'||scenario==='malformed_list')assert.equal(contacts,0);
  assert.equal(journal.db.prepare("SELECT count(*) n FROM actions WHERE kind='owner_preference_tool'").get()!.n,scenario==='saved_unknown'?1:0);
 }finally{await c.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
