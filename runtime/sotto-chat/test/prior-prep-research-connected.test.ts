import {startReceiver,stopReceiver} from './research-receiver-fixture.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync, rmSync, readFileSync} from 'node:fs';
import {DatabaseSync} from 'node:sqlite';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {OperationalJournal} from '../src/journal/index.js';
import {createSkillCommand} from '../src/tools/skill-command.js';
import {createOwnerSkillExecution} from '../src/tools/owner-skill-execution.js';
import {createOwnerSkillRequestClient} from '../src/tools/owner-skill-request-client.js';
test('accepted prep binds Node prior proof to receiver research worker and outbox',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-prior-prep-connected-'));
 const journal=new OperationalJournal(join(folder,'journal.sqlite'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 try{
  receiver=await startReceiver(join(folder,'receiver'),{SOTTO_TEST_MEETING_PREP:'1',SOTTO_TEST_ASK_COMPANY:'1',SOTTO_TEST_PRIOR_PREP:'1'});
  const prep=JSON.parse(readFileSync(join(folder,'receiver/synthetic-prior-prep.json'),'utf8'));
  const stamp=Date.now(),revision='a'.repeat(64),thread=receiver.identity.route_id;
  journal.db.exec('CREATE TABLE memory_sessions(provider_thread TEXT PRIMARY KEY,memory_thread TEXT,created_at INTEGER)');
  journal.db.prepare('INSERT INTO memory_sessions VALUES(?,?,?)').run(thread,'session',stamp-1000);
  journal.prepareDispatch({dispatchId:'prep',kind:'scheduled_text',destination:'photon',payload:JSON.stringify({text:prep.body}),routeDigest:prep.route,sourceRevision:revision});
  journal.beginDispatch('prep');journal.settleDispatch('prep',1,'accepted',prep.message_id,[prep.message_id],stamp);
  journal.db.prepare('UPDATE dispatches SET created_at=?,updated_at=? WHERE id=?').run(stamp-500,stamp,'prep');
  const inbound={eventId:'competitors',text:'Who are his competitors?',channel:'photon',accountId:'account',senderId:receiver.identity.owner_id,isDirect:true};
  const context={threadId:thread,destination:'photon',lineKey:'line'};
  const event=journal.admitInbound({eventId:'competitors',accountKey:'account',lineKey:'line',threadKey:thread,channel:'photon',sender:receiver.identity.owner_id,payload:JSON.stringify({inbound,context}),continuation:{routeDigest:prep.route,providerSentAtMs:stamp+100}}).event.id as string;
  journal.startRun({runId:'research',eventId:event});
  let submitted: {args:{prior_prep:{provider_message_id:string;route_digest:string}}}|undefined;
  const client=createOwnerSkillRequestClient({...receiver,fetch:async(input,init)=>{submitted=JSON.parse(String(init?.body));return fetch(input,init);}});
  const command=createSkillCommand(createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision,bridge_sources:[],google_sources:[]})}}));
  const result=await command({id:'ask-research',args:['--company','cedar.example']},{mode:'chat',runId:'research',ownerRequestKey:event,allowOwnerSkill:true,sourceRevision:revision});
  assert.equal(result.result.status,'queued',JSON.stringify(result));
  assert.equal(submitted!.args.prior_prep.provider_message_id,prep.message_id);
  assert.equal(submitted!.args.prior_prep.route_digest,prep.route);
  const queue=new DatabaseSync(join(folder,'receiver/events/work.sqlite3'),{readOnly:true});
  try{const jobs=queue.prepare('SELECT payload FROM jobs').all();assert.equal(jobs.length,1);const procedure=JSON.parse(JSON.parse(String(jobs[0]!.payload)).prompt);assert.equal(procedure.selection.company,'cedar.example');assert.match(procedure.question,/Who are his competitors/);}finally{queue.close();}
  const completed=new Promise<{executed:boolean;jobs:{status:string}|null}>((done,fail)=>{let text='';const listener=(chunk:Buffer)=>{text+=String(chunk);if(text.includes('\n')){receiver!.child.stdout.off('data',listener);clearTimeout(timer);done(JSON.parse(text.split('\n')[0]!));}};const timer=setTimeout(()=>fail(Error('worker timeout')),15000);receiver!.child.stdout.on('data',listener);});
  receiver.child.stdin.write('execute-ask-research\n');const execution=await completed;
  assert.equal(execution.executed,true);assert.equal(execution.jobs?.status,'done',JSON.stringify(execution));
  const sends=readFileSync(join(folder,'receiver/synthetic-sends.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));
  assert.equal(sends.length,2);assert.match(sends[1].text,/Juniper Systems/);assert.match(sends[1].text,/https:\/\/cedar.example\/market/);
  assert.equal(readFileSync(join(folder,'receiver/synthetic-company-calls.jsonl'),'utf8').trim().split('\n').length,1);
 }finally{journal.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
