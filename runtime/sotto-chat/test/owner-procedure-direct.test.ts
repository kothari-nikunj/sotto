import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {OperationalJournal} from '../src/journal/index.js';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createOwnerPreferenceToolClient} from '../src/tools/owner-preference-tool-client.js';
import {createOwnerPreferenceExecution} from '../src/tools/owner-preference-execution.js';
import {createPreferenceCommand} from '../src/tools/preference-command.js';
import {procedureRuleSchema,procedureReviewHash,procedureReviewText} from '../src/tools/owner-preference-tool-contract.js';
const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:'a'.repeat(64)};
const policy={channel:'photon' as const,accountId:'account',ownerId:'owner'};
const route={threadId:'thread',destination:'owner',lineKey:'line'};
const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId,text});
const sources={read:async()=>({version:2 as const,complete:false,revision:null,bridge_sources:[],google_sources:[]})};
const rule='Never book me Fridays.';
for(const scenario of ['early','undelivered','wrong_reply','intervening','quoted','cross_kind','nonaffirmative','wrong_thread','cross_confirm','context_section'] as const)test(`direct Procedures admission refuses ${scenario} without append`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'direct-rule-')),journal=new OperationalJournal(join(folder,'journal.db'));let previews=0,applies=0,refused=0;
 const client=createOwnerPreferenceToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'b'.repeat(64),fetch:async(_input,init)=>{
  const body=JSON.parse(String(init!.body));assert.equal(body.operation,'preview');previews++;
  const review=scenario==='context_section'?{operation:'append-context' as const,section:'About' as const,text:body.args.text,review_hash:''}:{operation:'append-procedure' as const,section:'Procedures' as const,rule:body.args.rule,review_hash:''};review.review_hash=procedureReviewHash(review);
  return new Response(JSON.stringify({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review}));
 }});
 const handler=createOwnerPreferenceExecution({client,journal:()=>journal,sourceAuthority:sources}),command=createPreferenceCommand({execution:handler});
 const c=new ConversationCoordinator({journal,ownerPreferenceTool:client,ownerPolicy:policy,model:{generate:async request=>{
  if(!['cross_confirm','context_section'].includes(scenario)||request.text!=='yes')assert.equal(request.acceptedProcedureReview,undefined,'ineligible review is not projected');
  else assert.ok(request.acceptedProcedureReview,'valid Procedures review reaches the wrong-wrapper test');
  const context={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerPreference:request.allowOwnerPreference,sourceRevision:undefined,signal:request.signal};
  if(request.text.startsWith('Remember')){
   if(scenario==='cross_kind'){
    journal.prepareOwnerTaskToolAction({runId:request.runId,accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon',scopeHash:client.scopeHash},request.ownerRequestKey!,{operation:'add',text:'Never book me Fridays.'});
    await assert.rejects(command({id:'master-file',args:['append','--section','Procedures','--text',rule]},context),/another private mutation/);refused++;
   }else await command({id:'master-file',args:['append','--section',scenario==='context_section'?'About':'Procedures','--text',rule]},context);
  }else if(request.text==='yes'){await assert.rejects(command({id:'master-file',args:scenario==='context_section'?['confirm-context','--section','People']:[scenario==='cross_confirm'?'confirm-priorities':'confirm-procedure']},context));refused++;}
  else if(scenario==='nonaffirmative'||scenario==='wrong_thread'){refused++;}
  else if(scenario==='quoted'){await assert.rejects(command({id:'master-file',args:['append','--section','Procedures','--text',rule]},context),/exact owner span/);refused++;}
  return {text:'Please clarify.',sourceRevision:'owner-only-context-v1'};
 }},postPrepared:async req=>({acceptance:scenario==='undelivered'&&req.text.startsWith('Add to your')?'rejected':'accepted',messageId:'review-message'})});
 try{
  if(scenario==='early')await c.receive(inbound('early','yes'),route);
  else if(scenario==='quoted')await c.receive({...inbound('quote','Explain that'),replyBound:true,replyToMessageId:'quoted-rule'},route);
  else{
   await c.receive(inbound('proposal','Remember '+rule),route);
   if(scenario==='intervening')await c.receive(inbound('other','Another question'),route);
   if(scenario!=='cross_kind')await c.receive({...inbound('yes',scenario==='nonaffirmative'?'Maybe later':'yes'),...(scenario==='wrong_reply'?{replyBound:true,replyToMessageId:'different-review'}:{})},scenario==='wrong_thread'?{...route,threadId:'other-thread'}:route);
  }
  if(scenario==='undelivered'){
   const event=journal.db.prepare("SELECT id FROM inbound_events WHERE source_id='yes'").get()!;
   journal.startRun({runId:'undelivered-confirmation',eventId:event.id as string});
   assert.throws(()=>journal.selectOwnerProcedureProposal({runId:'undelivered-confirmation',accountId:'account',ownerId:'owner',channel:'photon',lineKey:'line',scopeHash:client.scopeHash},event.id as string),/Accepted standing-rule review unavailable|current accepted question/);refused++;
  }
  assert.equal(refused,1);assert.equal(applies,0);assert.equal(previews,['early','quoted','cross_kind'].includes(scenario)?0:1);
  assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_preference_tool' AND json_extract(result_json,'$.args.operation') IN ('append-procedure','append-context')").get()!.n,0);
 }finally{await c.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('direct rule bounds preserve CJK/mixed scalars, closed section and full review',async()=>{
 const near='規'.repeat(3800)+'😀'.repeat(20);assert.ok(procedureRuleSchema.safeParse(near).success);
 for(const text of [' inferred ',rule+'\nnext',rule+'\u0085next','## Procedures','x'.repeat(4000),'😀'.repeat(2100)])assert.equal(procedureRuleSchema.safeParse(text).success,false);
 const review={operation:'append-procedure' as const,section:'Procedures' as const,rule:near,review_hash:''};review.review_hash=procedureReviewHash(review);
 let contact=0;const client=createOwnerPreferenceToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'b'.repeat(64),fetch:async(_url,init)=>{
  contact++;const body=JSON.parse(String(init!.body));const result=JSON.stringify({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review}).replace(/規/g,'\\u898f');assert.ok(Buffer.byteLength(result)>8192);return new Response(result);
 }});
 const {stableActionIdentity}=await import('../src/tools/action-gate.js');const event='a'.repeat(64),args={operation:'propose-procedure' as const,rule:near};const binding=stableActionIdentity(event,'owner_preference_tool',args);
 const result=await client.call('preview',{actionId:binding.actionId,eventId:event,payloadHash:event,text:near,admittedAtMs:Date.now(),providerThreadId:'thread',args});assert.equal(result.status,'preview');assert.equal(contact,1);assert.ok(procedureReviewText(review).length<=4000);
});

test('coalesced owner messages do not receive a standing-rule confirmation context',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'procedure-batch-')),journal=new OperationalJournal(join(folder,'journal.db'));
 let release!:()=>void,entered!:()=>void;const gate=new Promise<void>(done=>{release=done;});const waiting=new Promise<void>(done=>{entered=done;});
 const seen:Array<{messages:unknown[];acceptedProcedureReview?:unknown}>=[];
 const client=createOwnerPreferenceToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'b'.repeat(64),fetch:async(_url,init)=>{
  const body=JSON.parse(String(init!.body));assert.equal(body.operation,'preview');
  const review={operation:'append-procedure' as const,section:'Procedures' as const,rule,review_hash:''};review.review_hash=procedureReviewHash(review);
  return new Response(JSON.stringify({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review}));
 }});
 const command=createPreferenceCommand({execution:createOwnerPreferenceExecution({client,journal:()=>journal,sourceAuthority:sources})});
 const c=new ConversationCoordinator({journal,ownerPreferenceTool:client,ownerPolicy:policy,model:{generate:async request=>{
  seen.push(request);
  if(request.text.startsWith('Remember'))await command({id:'master-file',args:['append','--section','Procedures','--text',rule]},
   {mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerPreference:request.allowOwnerPreference,signal:request.signal});
  else if(request.messages.length===1){assert.ok(request.acceptedProcedureReview);entered();await gate;}
  return {text:'Please clarify.',sourceRevision:'owner-only-context-v1'};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'review-message'})});
 try{
  await c.receive(inbound('proposal','Remember '+rule),route);
  const yes=c.receive(inbound('yes','yes'),route);await waiting;const more=c.receive(inbound('more','Explain that too'),route);release();await Promise.all([yes,more]);
  assert.equal(seen.at(-1)!.messages.length,2);assert.equal(seen.at(-1)!.acceptedProcedureReview,undefined);
  assert.equal(journal.db.prepare("SELECT count(*) n FROM actions WHERE kind='owner_preference_tool'").get()!.n,1);
 }finally{release();await c.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('accepted Procedures context remains fenced across an asynchronous authority change',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'procedure-authority-'));let changed=false,calls=0;
 const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Answer safely',
  sourceAuthority:{read:async()=>changed?{version:2,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:[]}:{version:2,complete:false,revision:null,bridge_sources:[],google_sources:[]}},
  readMasterContext:async()=>{changed=true;return {text:'',chars:0,cap:8000,prompt:''};},
  model:new MockLanguageModelV4({doGenerate:async()=>{calls++;throw Error('model must not be contacted');}})});
 try{
  await assert.rejects(runtime.generateChat({text:'yes',threadId:'thread',ownerId:'owner',acceptedProcedureReview:{text:procedureReviewText({rule}),sourceRevision:'owner-only-context-v1'}}));assert.equal(calls,0);
 }finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});

test('Priorities closed complete set and review hash preserve exact owner words',async()=>{
 const {prioritiesTextSchema,prioritiesReviewSchema,procedurePreviewSchema,standingPreviewMatches,ownerPreferenceToolArgsSchema,preferenceReceiptMatches}=await import('../src/tools/owner-preference-tool-contract.js');
 for(const text of ['Fundraising\nHiring','One','One\nTwo\nThree'])assert.ok(prioritiesTextSchema.safeParse(text).success);
 for(const text of ['One\nTwo\nThree\nFour','One\n\nTwo','One\n## About',' leading','bad\tcontrol','bad\u2028break'])assert.equal(prioritiesTextSchema.safeParse(text).success,false);
 const review={operation:'replace-priorities' as const,section:'Priorities' as const,text:'Fundraising\nHiring',previous_text:'Previous priority',review_hash:''};review.review_hash=procedureReviewHash(review);
 assert.equal(procedureReviewText(review),'Replace your current priorities (complete set):\n\nCurrent:\nPrevious priority\n\nNew:\nFundraising\nHiring\n\nReplace these priorities?');
 const preview=procedurePreviewSchema.parse({version:1,status:'preview',event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),review});
 assert.equal(standingPreviewMatches(preview,{operation:'propose-priorities',text:review.text}),true);
 assert.equal(standingPreviewMatches(preview,{operation:'propose-priorities',text:'Invented'}),false);
 assert.equal(standingPreviewMatches(preview,{operation:'propose-procedure',rule:'Fundraising'}),false);
 const proposal={event_id:'a'.repeat(64),action_id:'c'.repeat(64),args_hash:'d'.repeat(64),accepted_dispatch_id:'accepted',source_revision:'owner-only-context-v1' as const};
 const args=ownerPreferenceToolArgsSchema.parse({operation:'replace-priorities',review,proposal});
 const receipt={version:1 as const,status:'committed' as const,action_id:'e'.repeat(64),event_id:'f'.repeat(64),payload_hash:'b'.repeat(64),args_hash:'a'.repeat(64),outcome:{...review,status:'saved' as const}};
 assert.equal(preferenceReceiptMatches(receipt,args),true);
 assert.equal(preferenceReceiptMatches({...receipt,outcome:{...receipt.outcome,previous_text:'Different'}},args),false);
 assert.ok(prioritiesReviewSchema.safeParse(review).success);
});


test('About/People closed review binds section and preserves full text',async()=>{
 const {ownerContextSectionSchema,ownerContextTextSchema,procedurePreviewSchema,standingPreviewMatches,ownerPreferenceToolArgsSchema,preferenceReceiptMatches}=await import('../src/tools/owner-preference-tool-contract.js');
 assert.equal(ownerContextSectionSchema.safeParse('Priorities').success,false);
 for(const text of ['first\nsecond','## About',' leading','bad\u2028break'])assert.equal(ownerContextTextSchema.safeParse(text).success,false);
 for(const section of ['About','People'] as const){
  const review={operation:'append-context' as const,section,text:'Dana is my cofounder.',review_hash:''};review.review_hash=procedureReviewHash(review);
  assert.equal(procedureReviewText(review),`Add to your standing context (${section}):\n\nDana is my cofounder.\n\nSave this context?`);
  const preview=procedurePreviewSchema.parse({version:1,status:'preview',event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),review});
  assert.ok(standingPreviewMatches(preview,{operation:'propose-context',section,text:review.text}));
  assert.equal(standingPreviewMatches(preview,{operation:'propose-context',section:section==='About'?'People':'About',text:review.text}),false);
  const proposal={event_id:'a'.repeat(64),action_id:'c'.repeat(64),args_hash:'d'.repeat(64),accepted_dispatch_id:'accepted',source_revision:'owner-only-context-v1' as const};
  const args=ownerPreferenceToolArgsSchema.parse({operation:'append-context',review,proposal});
  const receipt={version:1 as const,status:'committed' as const,action_id:'e'.repeat(64),event_id:'f'.repeat(64),payload_hash:'b'.repeat(64),args_hash:'a'.repeat(64),outcome:{...review,status:'saved' as const}};
  assert.ok(preferenceReceiptMatches(receipt,args));
  assert.equal(preferenceReceiptMatches({...receipt,outcome:{...receipt.outcome,section:section==='About'?'People':'About'}},args),false);
 }
});

test('exact weekly prioritize CTA accepts only whole current owner command',async()=>{
 const {priorityCtaMatches,ownerPreferenceToolArgsSchema,preferenceReceiptMatches}=await import('../src/tools/owner-preference-tool-contract.js');
 assert.ok(priorityCtaMatches('prioritize staying in touch with Maya','Maya'));
 assert.ok(priorityCtaMatches(' PRIORITIZE staying in touch with Maya ','Maya'));
 for(const text of ['yes','Please prioritize staying in touch with Maya','"prioritize staying in touch with Maya"','Someone said prioritize staying in touch with Maya','prioritize staying in touch with Dana','prioritize staying in touch with Maya\nand Dana'])assert.equal(priorityCtaMatches(text,'Maya'),false);
 const args=ownerPreferenceToolArgsSchema.parse({operation:'prioritize',name:'Maya'});
 const receipt={version:1 as const,status:'committed' as const,action_id:'e'.repeat(64),event_id:'f'.repeat(64),payload_hash:'b'.repeat(64),args_hash:'a'.repeat(64),outcome:{operation:'prioritize' as const,section:'Priorities' as const,name:'Maya',text:'Staying in touch with Maya',status:'saved' as const}};
 assert.ok(preferenceReceiptMatches(receipt,args));
 assert.equal(preferenceReceiptMatches({...receipt,outcome:{...receipt.outcome,name:'Dana',text:'Staying in touch with Dana'}},args),false);
 const execution=createOwnerPreferenceExecution({client:createOwnerPreferenceToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'b'.repeat(64)}),journal:()=>undefined,sourceAuthority:sources});
 const command=createPreferenceCommand({execution});
 await assert.rejects(command({id:'master-file',args:['prioritize','--text','Fundraising']},{}),/Unsupported/);
});
