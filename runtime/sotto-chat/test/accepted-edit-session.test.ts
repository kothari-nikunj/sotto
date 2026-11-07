import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {startApplication} from '../src/application.js';
import type {OperationalJournal} from '../src/journal/index.js';

for(const mode of ['current','reset','new-command','other-thread','fresh-after-reset'] as const)test(`accepted edit context respects exact session exclusions: ${mode}`,async t=>{
 t.mock.timers.enable({apis:['Date'],now:Date.UTC(2026,10,3,18)});
 const directory=mkdtempSync(join(tmpdir(),'edit-session-boundary-'));
 const revision='a'.repeat(64),body='Previous session: Dana confirmed Thursday at 2 PM.',freshBody='Current session: Cedar confirmed Friday at 3 PM.';
 let journal!:OperationalJournal,calls=0,sends=0,visible=false,freshVisible=false;
 const runtime=createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer naturally.',
  sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:[]})},
  model:new MockLanguageModelV4({doGenerate:async request=>{
   calls++;if(calls>1){const prompt=JSON.stringify(request.prompt);visible=prompt.includes(body);freshVisible=prompt.includes(freshBody);}
   return {content:[{type:'text',text:calls===1?body:mode==='fresh-after-reset'&&calls===2?freshBody:'Please paste the text.'}],finishReason:{unified:'stop',raw:'STOP'},warnings:[],
    usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}}};
  }})});
 const app=await startApplication({directory,mediaRoot:directory,bearerToken:'local-edit-session-probe-token-000000',
  owner:{channel:'photon',accountId:'account',ownerId:'owner'},lineKey:'line',target:'owner',providerThreadId:'thread'},
  {agent:runtime,onJournalReady:value=>{journal=value;},transport:{send:async()=>({acceptance:'accepted',providerMessageIds:['sent-'+(++sends)],providerSentAtMs:Date.now()})}});
 const inbound={channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true},context={threadId:'thread'};
 try{
  await app.receive({...inbound,eventId:'initial',text:'When is the meeting?'},context);
  if(mode==='reset'||mode==='fresh-after-reset')await app.rotateSessions();
  if(mode==='fresh-after-reset')await app.receive({...inbound,eventId:'fresh',text:'When is the current meeting?'},context);
  if(mode==='other-thread'){
   const first=journal.db.prepare("SELECT id FROM inbound_events WHERE source_id='initial'").get()!;
   journal.db.prepare('INSERT INTO conversation_memory_excluded VALUES(?,?)').run('another-thread',first.id);
  }
  if(mode==='new-command')await app.receive({...inbound,eventId:'new',text:'/new'},context);
  await app.receive({...inbound,eventId:'shorten',text:'shorter pls'},context);
  const initial=journal.db.prepare("SELECT id FROM inbound_events WHERE source_id='initial'").get()!;
  const prior=journal.getReplyForEvent(String(initial.id))!;
  const session=journal.db.prepare('SELECT created_at FROM memory_sessions').get()!;
  const excluded=journal.db.prepare('SELECT * FROM conversation_memory_excluded WHERE event_id=?').all(initial.id);
  assert.equal(prior.prepared_at,session.created_at,'timestamp equality alone does not identify the session');
  assert.equal(excluded.length,mode==='current'?0:1);
  assert.equal(calls,mode==='fresh-after-reset'?3:2);
  assert.equal(visible,mode==='current'||mode==='other-thread');
  assert.equal(freshVisible,mode==='fresh-after-reset');
 }finally{await app.close();await runtime.close();rmSync(directory,{recursive:true,force:true});}
});
