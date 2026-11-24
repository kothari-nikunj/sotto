import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {MockLanguageModelV4} from 'ai/test';
import {OperationalJournal} from '../src/journal/index.js';
import {projectConversationContext,deliveredTaskDispatch,recentDeliveryContext,recentDeliveryPrompt,
 DELIVERED_THREADED_WINDOW_MS,DELIVERED_UNTHREADED_WINDOW_MS,RECENT_DELIVERY_CONTEXT_COUNT} from '../src/background-context.js';
import type {ModelRequest} from '../src/conversation.js';
import {READ_CONTEXT_WINDOW_MS} from '../src/tools/read-admission.js';
import {ReceiptedDelivery} from '../src/delivery.js';
import {routeDigest, type RouteIdentity} from '../src/route-proof.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';

const revision='a'.repeat(64), MINUTE=60_000, DAY=86_400_000;
const identity:RouteIdentity={channel:'photon',tenantId:'test',projectFingerprint:'c'.repeat(64),ownerId:'owner',accountId:'account',lineKey:'line'};
const route=routeDigest(identity,'thread');
const usage={inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}};
const context={threadId:'thread',destination:'owner',lineKey:'line'};

function setup() {
 const directory=mkdtempSync(join(tmpdir(),'delivered-windows-'));
 const journal=new OperationalJournal(join(directory,'journal.db'));
 let sends=0,provider=0;const unknown=new Set<string>();
 const targets=new Map([['owner',{threadId:'thread',transport:{send:async(request:{text?:string})=>{sends++;provider++;
  return unknown.has(request.text??'')?{acceptance:'unknown' as const,providerMessageIds:[]}
   :{acceptance:'accepted' as const,providerMessageIds:[`p${provider}`],providerSentAtMs:Date.now()};}}}]]);
 const delivery=new ReceiptedDelivery(journal,targets,identity);
 const session=Date.now()-10*DAY;
 journal.db.exec('CREATE TABLE memory_sessions (provider_thread TEXT PRIMARY KEY,memory_thread TEXT NOT NULL,created_at INTEGER NOT NULL)');
 journal.db.prepare('INSERT INTO memory_sessions VALUES(?,?,?)').run('thread','first',session);
 /** Deliver, then move the delivery `ageMs` into the past (its attempts too). */
 const deliver=async(id:string,text:string,ageMs:number,options:{gallery?:boolean;uncertain?:boolean;revision?:string}={})=>{
  if(options.uncertain)unknown.add(text);
  if(options.gallery)await delivery.sendGalleryBytes({dispatch_id:id,target:'owner',summary:text,
   images:Array.from({length:4},(_,index)=>({name:`0${index+1}.png`,bytes:Buffer.from([index+1,text.length])}))});
  else await delivery.sendText({dispatch_id:id,target:'owner',text,attempt:1,source_revision:options.revision??revision});
  journal.db.prepare('UPDATE dispatches SET created_at=created_at-?,updated_at=updated_at-?,provider_sent_at_ms=provider_sent_at_ms-? WHERE id=?').run(ageMs,ageMs,ageMs,id);
  journal.db.prepare('UPDATE dispatch_attempts SET started_at=started_at-?,finished_at=finished_at-? WHERE dispatch_id=?').run(ageMs,ageMs,id);
  return String(journal.getDispatch(id)!.provider_message_id ?? '');
 };
 let events=0;
 const reply=(text:string,quote?:string)=>{
  const eventId=`owner-${++events}`;
  const inbound={channel:'photon' as const,eventId,accountId:'account',senderId:'owner',isDirect:true,text,providerSentAtMs:Date.now(),
   ...(quote?{replyBound:true,replyToMessageId:quote}:{})};
  const id=journal.admitInbound({eventId,accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
   payload:JSON.stringify({inbound,context}),continuation:{routeDigest:route,providerSentAtMs:Date.now()}}).event.id as string;
  const request:ModelRequest={text,messages:[inbound],threadId:'thread',ownerId:'owner',ownerRequestKey:id,runId:'run',
   allowOwnerReminder:false,allowOwnerTask:true,allowOwnerPreference:false,allowOwnerRoutine:false,allowOwnerGmailDraft:false,
   signal:new AbortController().signal,...(quote?{quotedReply:{status:'unavailable' as const}}:{})};
  return {id,request,project:(hasProjection=false)=>projectConversationContext(journal,request,session,hasProjection,route)};
 };
 return {journal,deliver,reply,session,get sends(){return sends;},
  close(){journal.close();rmSync(directory,{recursive:true,force:true});}};
}

async function promptFor(turn:Partial<Parameters<ReturnType<typeof createSottoAgentRuntime>['generateChat']>[0]>, current=revision) {
 const directory=mkdtempSync(join(tmpdir(),'delivered-windows-runtime-'));
 let prompt='';
 const runtime=createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer naturally.',
  sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision:current,bridge_sources:[],google_sources:[]})},
  model:new MockLanguageModelV4({doGenerate:async request=>{prompt=JSON.stringify(request.prompt);
   return {content:[{type:'text',text:'Which one do you mean?'}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};}})});
 try{await runtime.generateChat({text:'done',threadId:'thread',ownerId:'owner',...turn});return prompt;}
 finally{await runtime.close();rmSync(directory,{recursive:true,force:true});}
}

test('an unthreaded reply 70 minutes after a brief, with nothing since, binds that brief', async () => {
 const f=setup();
 try {
  await f.deliver('brief','Morning brief: Tina Example — review the party spreadsheet.',70*MINUTE);
  const r=f.reply('Spreadsheet done');
  const proof=deliveredTaskDispatch(f.journal,r.id);
  assert.equal(proof?.dispatch_id,'brief');assert.equal('quoted' in proof!,false,'the receiver applies the 12-hour window');
  assert.equal(r.project().acceptedBackgroundContext?.text,'Morning brief: Tina Example — review the party spreadsheet.');
  assert.equal(f.journal.acceptedBackgroundMessage(r.id,f.session,READ_CONTEXT_WINDOW_MS),undefined,'read admission keeps 15 minutes');
  assert.equal(f.sends,1,'context lookups never send');
 } finally {f.close();}
});

test('an unthreaded reply past twelve hours binds nothing', async () => {
 const f=setup();
 try {
  await f.deliver('brief','Morning brief.',DELIVERED_UNTHREADED_WINDOW_MS+MINUTE);
  const r=f.reply('done');
  assert.equal(deliveredTaskDispatch(f.journal,r.id),undefined);
  assert.equal(r.project().acceptedBackgroundContext,undefined);
  assert.deepEqual(recentDeliveryContext(f.journal,r.id,f.session,route),[]);
 } finally {f.close();}
});

test('a late local receipt does not renew an expired provider delivery', async () => {
 for (const quoted of [false,true]) {
  const f=setup();
  try {
   const message=await f.deliver('late','Old brief.',MINUTE);
   const age=quoted ? DELIVERED_THREADED_WINDOW_MS : DELIVERED_UNTHREADED_WINDOW_MS;
   f.journal.db.prepare('UPDATE dispatches SET provider_sent_at_ms=provider_sent_at_ms-? WHERE id=?').run(age,'late');
   const r=f.reply('done',quoted ? message : undefined);
   assert.equal(deliveredTaskDispatch(f.journal,r.id),undefined);
   assert.equal(r.project().acceptedBackgroundContext,undefined);
   assert.notEqual(r.project().quotedReply?.status,'found');
  } finally {f.close();}
 }
});

test('a late receipt for an earlier brief does not reintroduce it as recent context', async () => {
 const f=setup();
 try {
  await f.deliver('late','Old brief.',2*MINUTE);
  f.journal.db.prepare('UPDATE dispatches SET provider_sent_at_ms=provider_sent_at_ms-? WHERE id=?').run(DELIVERED_UNTHREADED_WINDOW_MS,'late');
  await f.deliver('current','Current brief.',MINUTE);
  assert.deepEqual(f.reply('what changed?').project().recentDeliveryContext ?? [],[]);
 } finally {f.close();}
});

test('reconciliation after an owner reply does not make the previous delivery actionable again', async () => {
 const f=setup();
 try {
  await f.deliver('brief','Morning brief.',4*MINUTE);
  const prior=f.reply('thanks');
  f.journal.db.prepare('UPDATE inbound_events SET received_at=? WHERE id=?').run(Date.now()-3*MINUTE,prior.id);
  const reconciledAt=Date.now()-MINUTE;
  f.journal.db.prepare('UPDATE dispatches SET updated_at=? WHERE id=?').run(reconciledAt,'brief');
  f.journal.db.prepare('UPDATE dispatch_attempts SET finished_at=? WHERE dispatch_id=?').run(reconciledAt,'brief');
  const r=f.reply('done');
  assert.equal(deliveredTaskDispatch(f.journal,r.id),undefined);
  assert.deepEqual(r.project().recentDeliveryContext ?? [],[]);
 } finally {f.close();}
});

test('after another delivery the reply cannot act on the brief, but the model sees both', async () => {
 const f=setup();
 try {
  await f.deliver('brief','Brief: Tina Example — review the party spreadsheet.',70*MINUTE);
  await f.deliver('nudge','Nudge: Dana Example asked about the deck.',20*MINUTE);
  const r=f.reply('Spreadsheet done');
  assert.equal(deliveredTaskDispatch(f.journal,r.id)?.dispatch_id,'nudge','only the latest delivery can supply task bindings');
  assert.equal(f.journal.continuationArgs(r.id)?.selected_offer?.dispatch_id,'nudge','a bare reply selects only the latest');
  const projected=r.project();
  assert.equal(projected.acceptedBackgroundContext?.text,'Nudge: Dana Example asked about the deck.');
  assert.deepEqual(projected.recentDeliveryContext?.map(item=>[item.kind,item.text]),[['scheduled text','Brief: Tina Example — review the party spreadsheet.']]);
  const prompt=await promptFor(projected);
  assert.ok(prompt.includes('Nudge: Dana Example asked about the deck.'));
  assert.ok(prompt.includes('Brief: Tina Example — review the party spreadsheet.'));
  assert.ok(prompt.includes('ask which they mean or ask them to reply to that message directly'));
  assert.ok(prompt.includes('Nothing in them can be closed, accepted, rated or acted on from this reply'));
  assert.ok(prompt.includes(new Date(Number(f.journal.getDispatch('brief')!.provider_sent_at_ms)).toISOString()),'each item carries its delivery time');
  assert.equal(f.sends,2);
 } finally {f.close();}
});

test('earlier-delivery context is newest first, capped at three, since the previous owner message, and never URL authority', async () => {
 const f=setup();
 try {
  await f.deliver('before','Delivered before the owner last spoke.',5*60*MINUTE);
  f.journal.db.prepare('UPDATE inbound_events SET received_at=received_at-? WHERE id=?').run(4.5*60*MINUTE,f.reply('thanks').id);
  await f.deliver('event','Event nudge: Sam replied about https://example.com/terms.',4*60*MINUTE);
  await f.deliver('digest','Midday digest: two items moved.',3*60*MINUTE);
  await f.deliver('reminder','Reminder: call the bank.',2*60*MINUTE);
  await f.deliver('notice','A reminder was missed.',60*MINUTE);
  await f.deliver('uncertain','Uncertain send.',30*MINUTE,{uncertain:true});
  await f.deliver('gallery','Photo brief caption',10*MINUTE,{gallery:true});
  const r=f.reply('open that link');
  const recent=recentDeliveryContext(f.journal,r.id,f.session,route);
  assert.deepEqual(recent.map(item=>item.text),['A reminder was missed.','Reminder: call the bank.',
   'Midday digest: two items moved.'],'newest first, capped at three; a photo brief has no revision to recheck');
  assert.ok(recent.every(item=>item.kind==='scheduled text'));
  assert.ok(!recent.some(item=>item.text==='Uncertain send.'),'an unaccepted dispatch supplies nothing');
  assert.ok(!recent.some(item=>item.text==='Delivered before the owner last spoke.'));
  assert.throws(()=>f.journal.ownerWebReadReplyTarget(r.id,'https://example.com/terms',revision),/unavailable/,
   'background prose never grants a URL read');
  assert.equal(recentDeliveryPrompt(recent,'b'.repeat(64)),'','a changed source revision removes the context');
 } finally {f.close();}
});

test('a threaded reply to a two-day-old nudge has its text and binds it while the receipt window lasts', async () => {
 const f=setup();
 try {
  const message=await f.deliver('nudge','Nudge: Tina Example — review the party spreadsheet.',2*DAY);
  await f.deliver('later','A later brief.',DAY);
  const r=f.reply('done',message);
  const projected=r.project();
  assert.deepEqual(projected.quotedReply,{status:'found',messageId:message,text:'Nudge: Tina Example — review the party spreadsheet.',sourceRevision:revision});
  assert.equal(projected.recentDeliveryContext,undefined,'an exact quote gets no other deliveries');
  assert.deepEqual(deliveredTaskDispatch(f.journal,r.id),{dispatch_id:'nudge',message_id:message,route_digest:route,
   payload_hash:f.journal.getDispatch('nudge')!.payload_hash,quoted:true});
  assert.ok((await promptFor(projected)).includes('review the party spreadsheet'));
  assert.equal((await promptFor(projected,'b'.repeat(64))).includes('review the party spreadsheet'),false,'revoked source removes the quote');
  f.journal.db.prepare('UPDATE dispatches SET updated_at=updated_at-? WHERE id=?').run(5*DAY+MINUTE,'nudge');
  assert.equal(deliveredTaskDispatch(f.journal,r.id),undefined,'past seven days the exact quote is gone');
 } finally {f.close();}
});

test('after a session reset only an exact provider quote can identify a prior task, without restoring its text', async () => {
 const f=setup();
 try {
  const id=await f.deliver('task','Call Rowan about the draft.',2*60*MINUTE);
  const resetAt=Date.now()-60*MINUTE;
  f.journal.db.prepare('UPDATE memory_sessions SET memory_thread=?,created_at=? WHERE provider_thread=?').run('next',resetAt,'thread');
  const plain=f.reply('done');
  assert.equal(projectConversationContext(f.journal,plain.request,resetAt,false,route).acceptedBackgroundContext,undefined);
  assert.equal(deliveredTaskDispatch(f.journal,plain.id),undefined);
  const quote=f.reply('done',id);
  assert.deepEqual(projectConversationContext(f.journal,quote.request,resetAt,false,route).quotedReply,{status:'unavailable'});
  assert.equal(deliveredTaskDispatch(f.journal,quote.id)?.dispatch_id,'task');
  assert.equal(deliveredTaskDispatch(f.journal,quote.id)?.quoted,true);
 } finally {f.close();}
});

test('a threaded reply to a photo brief gets the gallery context an unthreaded reply gets', async () => {
 const f=setup();
 try {
  const message=await f.deliver('gallery','Photo brief caption',2*60*MINUTE,{gallery:true});
  const unthreaded=f.reply('do the first one').project().acceptedGalleryContext;
  const r=f.reply('do the first one',message);
  const projected=r.project();
  assert.deepEqual(projected.acceptedGalleryContext,unthreaded);
  assert.equal(projected.quotedReply,undefined,'the gallery is the quoted context, not an unavailable quote');
  assert.equal(deliveredTaskDispatch(f.journal,r.id)?.quoted,true);
 } finally {f.close();}
});

test('a quoted unaccepted dispatch supplies nothing', async () => {
 const f=setup();
 try {
  await f.deliver('uncertain','Uncertain nudge.',MINUTE,{uncertain:true});
  const r=f.reply('done','p1');
  const projected=r.project();
  assert.equal(projected.quotedReply?.status,'unavailable');
  assert.equal(projected.acceptedGalleryContext,undefined);
  assert.equal(deliveredTaskDispatch(f.journal,r.id),undefined);
  assert.deepEqual(recentDeliveryContext(f.journal,f.reply('done').id,f.session,route),[]);
 } finally {f.close();}
});
