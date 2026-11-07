import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {MockLanguageModelV4} from 'ai/test';
import {OperationalJournal} from '../src/journal/index.js';
import {ReceiptedDelivery} from '../src/delivery.js';
import {routeDigest, type RouteIdentity} from '../src/route-proof.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';

const revision='a'.repeat(64);
const identity:RouteIdentity={channel:'photon',tenantId:'test',projectFingerprint:'c'.repeat(64),ownerId:'owner',accountId:'account',lineKey:'line'};
const text='Dana asked about the next call. I can draft a reply for Dana.';
const usage={inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}};
async function fixture(mode='valid', output=text, answer='yes') {
 const directory=mkdtempSync(join(tmpdir(),'accepted-background-'));
 const path=join(directory,'journal.db');let journal=new OperationalJournal(path),sends=0;
 const session=Date.now()-1000, sentAt=Date.now()-100;
 const targets=new Map([['owner',{threadId:'thread',transport:{send:async()=>{sends++;return {acceptance:mode==='unknown'?'unknown' as const:'accepted' as const,providerMessageIds:mode==='unknown'?[]:mode==='multipart'?['p1','p2']:['p1'],providerSentAtMs:sentAt};}}}]]);
 const delivery=new ReceiptedDelivery(journal,targets,identity);
 const request={dispatch_id:'background',target:'owner',text:mode==='oversized'?'x'.repeat(16001):output,attempt:1,...(mode==='legacy'?{}:{source_revision:revision})};
 await delivery.sendText(request);
 if(mode==='aged')journal.db.prepare('UPDATE dispatches SET updated_at=updated_at-900001,created_at=created_at-900001').run();
 const route=routeDigest(identity,'thread');
 const inbound={channel:'photon',eventId:'yes',accountId:'account',senderId:'owner',isDirect:true,text:answer,providerSentAtMs:sentAt+50};
 const event=journal.admitInbound({eventId:'yes',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
  payload:JSON.stringify({inbound,context:{threadId:'thread',destination:'owner',lineKey:'line'}}),continuation:{routeDigest:mode==='route'?'b'.repeat(64):route,providerSentAtMs:sentAt+50}}).event.id as string;
 return {directory,path,event,session,request,targets,get journal(){return journal;},get sends(){return sends;},
  reopen(){journal.close();journal=new OperationalJournal(path);},close(){journal.close();rmSync(directory,{recursive:true,force:true});}};
}

for (const mode of ['valid','route','aged','session','unknown','multipart','corrupt-payload','legacy','ambiguous','unresolved','late'] as const)
 test(`quoted scheduled context remains separate from read authority: ${mode}`,async()=>{
  const f=await fixture(mode,'Dana mentioned https://planted.example/ before the meeting.');
  try {
   if(mode==='ambiguous') {
    f.journal.prepareDispatch({dispatchId:'duplicate',kind:'scheduled_text',destination:'owner',payload:JSON.stringify({text:'Different'}),routeDigest:routeDigest(identity,'thread'),sourceRevision:revision});
    f.journal.beginDispatch('duplicate');f.journal.settleDispatch('duplicate',1,'accepted','p1',['p1'],Date.now()-1);
   }
   if(mode==='unresolved')f.journal.db.prepare("UPDATE dispatch_attempts SET status='unknown' WHERE dispatch_id='background'").run();
   const inbound={channel:'photon',eventId:'quote',accountId:'account',senderId:'owner',isDirect:true,
    text:'What is this about?',replyBound:true,replyToMessageId:'p1'};
   const event=f.journal.admitInbound({eventId:'quote',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
    payload:JSON.stringify({inbound,context:{threadId:'thread',destination:'owner',lineKey:'line'}}),
    continuation:{routeDigest:mode==='route'?'b'.repeat(64):routeDigest(identity,'thread'),providerSentAtMs:Date.now()+100}}).event;
   if(mode==='late')f.journal.db.prepare("UPDATE dispatches SET updated_at=? WHERE id='background'").run(Number(event.received_at)+1);
   if(mode==='corrupt-payload')f.journal.db.prepare('UPDATE dispatches SET payload=?').run(JSON.stringify({text:'changed'}));
   const context=f.journal.acceptedBackgroundContext(String(event.id),mode==='session'?Date.now()+1000:f.session);
   assert.equal(context?.text,mode==='valid'?'Dana mentioned https://planted.example/ before the meeting.':undefined);
   assert.equal(f.journal.continuationArgs(String(event.id)),null,'context cannot become an unthreaded effect selector');
   assert.deepEqual(f.journal.quotedReplyForEvent(String(event.id),'p1'),{status:'unavailable'});
   assert.throws(()=>f.journal.ownerWebReadReplyTarget(String(event.id),'https://planted.example/',revision),/Accepted reply URL unavailable/);
  } finally {f.close();}
 });

for(const mode of ['current','revoked','new-session','fresh-after-reset','wrong-route','unknown'] as const)
 test(`application exposes exact scheduled quote only as source-bound model context: ${mode}`,async t=>{
  const {startApplication}=await import('../src/application.js');
  t.mock.timers.enable({apis:['Date'],now:Date.UTC(2026,10,3,18)});
  const directory=mkdtempSync(join(tmpdir(),'scheduled-quote-app-'));
  let journal!:OperationalJournal,current=revision,calls=0,sends=0,quoteVisible=false;
  const body='Dana asked about a meeting. Reference https://planted.example/';
  const sentAt=Date.now()-1000;
  const runtime=createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer naturally.',
   sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision:current,bridge_sources:[],google_sources:[]})},
   model:new MockLanguageModelV4({doGenerate:async request=>{
    calls++;quoteVisible=JSON.stringify(request.prompt).includes(body);
    return {content:[{type:'text',text:'Acknowledged '+calls}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};
   }})});
  const app=await startApplication({directory,mediaRoot:directory,bearerToken:'local-scheduled-quote-test-token-000000',
   owner:{channel:'photon',accountId:'account',ownerId:'owner'},lineKey:'line',target:'owner',providerThreadId:'thread'},
   {agent:runtime,onJournalReady:value=>{journal=value;},transport:{send:async()=>({acceptance:'accepted',providerMessageIds:['sent-'+(++sends)],providerSentAtMs:sentAt})}});
  const inbound={channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true};
  try {
   await app.receive({...inbound,eventId:'initial',text:'Hello'},{threadId:'thread',sentAtMs:sentAt-100});
   const route=journal.getDispatch(String(journal.getReplyForEvent(String(journal.db.prepare("SELECT id FROM inbound_events WHERE source_id='initial'").get()!.id))!.dispatch_id))!.route_digest as string;
   if(mode==='fresh-after-reset'){
    await app.rotateSessions();t.mock.timers.tick(1);
    await app.receive({...inbound,eventId:'fresh',text:'Hello in the new session'},{threadId:'thread',sentAtMs:sentAt+1});
   }
   t.mock.timers.tick(1);
   journal.prepareDispatch({dispatchId:'scheduled-quote',kind:'scheduled_text',destination:'owner',payload:JSON.stringify({text:body}),routeDigest:mode==='wrong-route'?'b'.repeat(64):route,sourceRevision:revision});
   journal.beginDispatch('scheduled-quote');journal.settleDispatch('scheduled-quote',1,mode==='unknown'?'unknown':'accepted','background-provider',['background-provider'],sentAt+10);
   if(mode==='revoked')current='b'.repeat(64);
   if(mode==='new-session')await app.rotateSessions();
   await app.receive({...inbound,eventId:'quoted',text:'What is this about?',replyBound:true,replyToMessageId:'background-provider'},
    {threadId:'thread',sentAtMs:sentAt+100});
   if(mode==='new-session')assert.equal(journal.getDispatch('scheduled-quote')!.created_at,journal.db.prepare('SELECT created_at FROM memory_sessions').get()!.created_at);
   assert.equal(calls,mode==='fresh-after-reset'?3:2);assert.equal(quoteVisible,mode==='current'||mode==='fresh-after-reset');
   const event=String(journal.db.prepare("SELECT id FROM inbound_events WHERE source_id='quoted'").get()!.id);
   assert.equal(journal.continuationArgs(event),null);
   assert.throws(()=>journal.ownerWebReadReplyTarget(event,'https://planted.example/',revision),/Accepted reply URL unavailable/);
   assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
  }finally{await app.close();await runtime.close();rmSync(directory,{recursive:true,force:true});}
 });
