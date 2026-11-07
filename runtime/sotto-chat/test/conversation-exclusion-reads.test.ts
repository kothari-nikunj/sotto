import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {OperationalJournal} from '../src/journal/index.js';

for(const present of [true,false])test(`context exclusion table presence is read once per request, present=${present}`,()=>{
 const directory=mkdtempSync(join(tmpdir(),'context-exclusion-reads-'));
 const journal=new OperationalJournal(join(directory,'operations.sqlite'));
 try{
  if(present)journal.db.exec('CREATE TABLE conversation_memory_excluded(provider_thread TEXT,event_id TEXT)');
  const admit=(id:string)=>journal.admitInbound({eventId:id,accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
   payload:JSON.stringify({inbound:{text:'Read that source',channel:'photon',accountId:'account',senderId:'owner'},context:{threadId:'thread',destination:'owner',lineKey:'line'}})}).event.id as string;
  const prior=Array.from({length:8},(_,i)=>admit('prior-'+i));
  journal.db.prepare("UPDATE inbound_events SET outcome='resolved'").run();
  const current=admit('current');
  let presenceReads=0,eventReads=0;
  const prepare=journal.db.prepare.bind(journal.db);
  journal.db.prepare=sql=>{
   if(sql.includes('sqlite_master')&&sql.includes('conversation_memory_excluded'))presenceReads++;
   if(sql.includes('FROM conversation_memory_excluded WHERE provider_thread='))eventReads++;
   return prepare(sql);
  };
  for(let request=1;request<=2;request++){
   assert.throws(()=>journal.ownerWebReadReplyTarget(current,'https://example.com/','a'.repeat(64)),/Accepted reply URL unavailable/);
   assert.equal(presenceReads,request,'one metadata lookup per request, without a cross-request cache');
   assert.equal(eventReads,present?prior.length*request:0);
  }
 }finally{journal.close();rmSync(directory,{recursive:true,force:true});}
});
