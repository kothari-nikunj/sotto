import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {OperationalJournal} from '../src/journal/index.js';
import {parseSkillCommand} from '../src/tools/skill-command.js';
const inbound=(eventId:string,text:string)=>({eventId,text,channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true});
const context={threadId:'thread',destination:'photon',lineKey:'line'};

for(const scenario of ['valid','source-change','reset','no-session','no-reference','website','forged-proof','saved-only','rewrite','cancel','subject-switch'] as const)
test(`accepted prep public-company continuation: ${scenario}`,()=>{
 const dir=mkdtempSync(join(tmpdir(),'prep-research-'));const journal=new OperationalJournal(join(dir,'journal.sqlite'));
 const revision='a'.repeat(64),route='b'.repeat(64),stamp=Date.now();
 try{
  if(scenario!=='no-session'){
   journal.db.exec('CREATE TABLE memory_sessions(provider_thread TEXT PRIMARY KEY,memory_thread TEXT,created_at INTEGER)');
   journal.db.prepare('INSERT INTO memory_sessions VALUES(?,?,?)').run('thread','session',scenario==='reset'?stamp+1:stamp-1000);
  }
  journal.prepareDispatch({dispatchId:'prep',kind:'scheduled_text',destination:'photon',payload:JSON.stringify({text:'Prep for Dana. Cedar Labs builds clinic tools.'}),routeDigest:route,sourceRevision:revision});
  journal.beginDispatch('prep');journal.settleDispatch('prep',1,'accepted','prep-native',['prep-native'],stamp);
  journal.db.prepare('UPDATE dispatches SET created_at=?,updated_at=? WHERE id=?').run(stamp-500,stamp,'prep');
  if(scenario==='cancel'||scenario==='subject-switch')journal.admitInbound({eventId:'intervening',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:inbound('intervening',scenario==='cancel'?'Never mind':'Research Other Labs'),context})});
  const text=scenario==='no-reference'?'What is the landscape like?':scenario==='saved-only'?'Who are his competitors, using only the prep?':scenario==='rewrite'?'Rewrite his competitor summary':'Who are his competitors?';
  const event=journal.admitInbound({eventId:'competitors',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:inbound('competitors',text),context}),continuation:{routeDigest:route,providerSentAtMs:stamp+100}}).event.id as string;
  journal.startRun({runId:'prep-followup',eventId:event});
  const authority={runId:'prep-followup',accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon' as const,scopeHash:'c'.repeat(64)};
  const input=parseSkillCommand(['--company','cedar.example',...(scenario==='website'?['--website']:[])],'ask-research');
  if(scenario==='forged-proof')Object.assign(input,{prior_prep:{provider_message_id:'forged',source_revision:revision,route_digest:route}});
  const resolve=()=>journal.resolveOwnerResearchSelection(authority,event,input,scenario==='source-change'?'d'.repeat(64):revision);
  if(scenario==='valid'||scenario==='forged-proof'){
   const args=resolve();assert.equal(args.skill,'ask-research');
   assert.deepEqual((args as any).prior_prep,{provider_message_id:'prep-native',source_revision:revision,route_digest:route});
   assert.doesNotThrow(()=>journal.prepareOwnerSkillRequestAction(authority,event,args));
  }else assert.throws(resolve);
 }finally{journal.close();rmSync(dir,{recursive:true,force:true});}
});
