import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import {startApplication} from '../src/application.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import type {OperationalJournal} from '../src/journal/index.js';
const usage={inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}};
for(const mode of ['valid','restart','rotated','revoked'] as const)test(`accepted background pulse HTTP to natural yes: ${mode}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'background-app-'));let revision='a'.repeat(64),calls=0,posts=0;const sent:string[]=[];let journal!:OperationalJournal;
 const pulse='Jackson Stokes needs a telecom introduction. I can draft a reply to Jackson Stokes.';
 const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Draft inline; never send.',sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['gmail']})},model:new MockLanguageModelV4({doGenerate:async request=>{
  if(calls++>0){
   const prompt=JSON.stringify(request.prompt),permitted=mode==='valid'||mode==='restart';
   assert.equal(prompt.includes(pulse),permitted);
   assert.equal(request.prompt.filter(message=>message.role==='assistant'&&JSON.stringify(message.content).includes(pulse)).length,permitted?1:0,
    'accepted pulse is remembered once as assistant output, never an owner instruction');
   assert.equal(request.prompt.some(message=>message.role==='user'&&JSON.stringify(message.content).includes(pulse)),false);
   if(permitted)assert.ok(request.prompt.some(message=>message.role==='assistant'&&JSON.stringify(message.content).includes('never approval to save a draft')),
    'remembered background text keeps its not-instructions, not-approval provenance');
  }
  return {content:[{type:'text' as const,text:calls===1?'Hello.':'Jackson, I will check our telecom contacts.'}],finishReason:{unified:'stop' as const,raw:'STOP'},usage,warnings:[]};
 }})});
 const options={directory:join(folder,'state'),mediaRoot:folder,bearerToken:'fixture-only-local-bearer-0000000',owner:{channel:'photon' as const,accountId:'account',ownerId:'owner'},lineKey:'line',target:'owner',providerThreadId:'thread'};
 const deps={agent:{...runtime,generateChat:async(input:Parameters<typeof runtime.generateChat>[0])=>{
  assert.notEqual(input.allowSend,true,'background context never grants send approval');
  assert.equal(input.acceptedProcedureReview,undefined,'an inline offer is not an accepted action review');
  return runtime.generateChat(input);
 }},onJournalReady:(value:OperationalJournal)=>{journal=value;},transport:{send:async(input:{text?:string})=>{sent.push(input.text??'');return {acceptance:'accepted' as const,providerMessageIds:['message-'+(++posts)],providerSentAtMs:Date.now()};}}};
 let app=await startApplication(options,deps);
 const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId,text});
 try{
  await app.receive(inbound('hello','hello'),{threadId:'thread',sentAtMs:Date.now()});
  await new Promise(r=>setTimeout(r,5));
  const response=await fetch(`http://127.0.0.1:${app.port}/v1/send`,{method:'POST',headers:{authorization:`Bearer ${options.bearerToken}`,'content-type':'application/json'},body:JSON.stringify({dispatch_id:'pulse',target:'owner',text:pulse,source_revision:revision})});
  assert.equal(response.status,200);assert.equal((await response.json()).acceptance,'accepted');
  if(mode==='restart'){await app.close();app=await startApplication(options,deps);}
  if(mode==='rotated')await app.rotateSessions();
  if(mode==='revoked')revision='b'.repeat(64);
  await new Promise(r=>setTimeout(r,5));
  assert.equal((await app.receive(inbound('yes','yes'),{threadId:'thread',sentAtMs:Date.now()})).status,'accepted');
  assert.equal(sent.at(-1),'Jackson, I will check our telecom contacts.');assert.equal(calls,2);assert.equal(posts,3,'one pulse and two inline replies, no external send');
  assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0,'no draft save, send, or other owner action was admitted');
 }finally{await app.close();await runtime.close();rmSync(folder,{recursive:true,force:true});}
});
