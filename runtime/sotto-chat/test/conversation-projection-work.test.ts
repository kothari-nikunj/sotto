import test from 'node:test';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {OperationalJournal} from '../src/journal/index.js';
import {AcceptedConversationMemory,type ConversationMemoryProjection} from '../src/conversation-memory.js';

// Count materialized payloads through the real journal, without a timing threshold.
test('settled conversation projection reads no old payloads and still retries deferred input',async()=>{
  const directory=mkdtempSync(join(tmpdir(),'projection-work-'));
  const journal=new OperationalJournal(join(directory,'journal.db'));
  const batches:ConversationMemoryProjection[]=[];
  let deferred=false,payloads=0,backgroundChecks=0;
  const prepare=journal.db.prepare.bind(journal.db);
  journal.db.prepare=(sql)=>{
    const statement=prepare(sql),all=statement.all.bind(statement);
    statement.all=(...params)=>{
      const rows=Reflect.apply(all,statement,params) as ReturnType<typeof statement.all>;
      payloads+=rows.filter(row=>Object.hasOwn(row,'payload')).length;
      return rows;
    };
    return statement;
  };
  const background=journal.acceptedBackgroundMessage.bind(journal);
  journal.acceptedBackgroundMessage=(...args)=>{backgroundChecks++;return background(...args);};
  const memory=new AcceptedConversationMemory({journal,ownerId:'owner',threadFor:()=> 'native',scopeThread:t=>createHash('sha256').update(JSON.stringify(['account','line','telegram',t])).digest('hex'),
    sessionStartedAt:()=>0,reset:async()=>{},project:async input=>{
      batches.push(input);return deferred?'deferred':'settled';
    }});
  const admit=(eventId:string)=>journal.admitInbound({eventId,accountKey:'account',lineKey:'line',threadKey:'thread',
    channel:'telegram',sender:'owner',payload:JSON.stringify({inbound:{text:`Owner ${eventId}`},context:{threadId:'thread'}})});
  try{
    for(let i=0;i<200;i++)admit(String(i));
    await memory.sync('thread');
    assert.equal(batches.reduce((n,b)=>n+b.messages.length,0),200);
    payloads=0;backgroundChecks=0;batches.length=0;
    await memory.sync('thread');
    assert.equal(payloads,0,'completed payloads must be filtered before materialization');
    assert.equal(backgroundChecks,0,'plain owner messages have no background receipt to validate');
    assert.equal(batches.length,0);
    deferred=true;admit('later');await memory.sync('thread');
    assert.equal(batches.at(-1)!.messages[0]!.text,'Owner later');
    deferred=false;await memory.sync('thread');
    assert.equal(batches.at(-1)!.messages[0]!.text,'Owner later');
    batches.length=0;await memory.sync('thread');assert.equal(batches.length,0);
  }finally{journal.close();rmSync(directory,{recursive:true,force:true});}
});

test('old frozen dispatches avoid journal rereads while failed receipts can recover',async()=>{
  const directory=mkdtempSync(join(tmpdir(),'projection-background-'));
  const journal=new OperationalJournal(join(directory,'journal.db'));
  const batches:ConversationMemoryProjection[]=[];
  let sessionStartedAt=100,checks=0,reads=0;
  const background=journal.acceptedBackgroundMessage.bind(journal),getInbound=journal.getInbound.bind(journal);
  journal.acceptedBackgroundMessage=(...args)=>{checks++;return background(...args);};
  journal.getInbound=(...args)=>{reads++;return getInbound(...args);};
  const scope=(t:string)=>createHash('sha256').update(JSON.stringify(['account','line','telegram',t])).digest('hex');
  const memory=new AcceptedConversationMemory({journal,ownerId:'owner',threadFor:()=>String(sessionStartedAt),scopeThread:scope,
    routeDigestForThread:()=> 'b'.repeat(64),sessionStartedAt:()=>sessionStartedAt,reset:async()=>{},
    project:async input=>{batches.push(input);return 'settled';}});
  const seed=(id:string,createdAt:number,status='accepted')=>{
    journal.prepareDispatch({dispatchId:id,kind:'scheduled_text',destination:'owner',payload:JSON.stringify({text:`Background ${id}`}),routeDigest:'b'.repeat(64),sourceRevision:'a'.repeat(64)});
    journal.db.prepare('UPDATE dispatches SET created_at=?,updated_at=200,status=?,provider_message_id=?,provider_message_ids=? WHERE id=?')
      .run(createdAt,status,id,JSON.stringify([id]),id);
    const admitted=journal.admitInbound({eventId:id,accountKey:'account',lineKey:'line',threadKey:'thread',channel:'telegram',sender:'owner',
      payload:JSON.stringify({inbound:{text:`Owner ${id}`,replyBound:true,replyToMessageId:id}})});
    journal.db.prepare('UPDATE inbound_events SET received_at=300,continuation_json=? WHERE id=?')
      .run(JSON.stringify({quoted_context:true,dispatch_id:id,provider_message_id:id,route_digest:'b'.repeat(64)}),String(admitted.event.id));
  };
  try{
    for(let i=0;i<200;i++)seed(`old-${i}`,99);
    seed('boundary',100);seed('fresh',101);seed('recover',101,'unknown');seed('invalid',101);
    journal.db.prepare("UPDATE dispatches SET payload_hash='invalid' WHERE id='invalid'").run();
    await memory.sync('thread');
    assert.equal(checks,3,'dispatch creation, not newer owner receipt time, bounds rereads');assert.equal(reads,3);
    assert.equal(batches.flatMap(b=>b.messages).filter(m=>m.text.includes('Background')).length,1);
    assert.equal(batches.flatMap(b=>b.messages).some(m=>m.text.includes('Background boundary')),false,'equal-time dispatches remain withheld before receipt hydration');
    assert.equal(batches.flatMap(b=>b.messages).some(m=>m.text.includes('Background fresh')),true);
    checks=0;reads=0;batches.length=0;
    await memory.sync('thread');assert.equal(checks,2);assert.equal(reads,2);
    journal.db.prepare("UPDATE dispatches SET status='accepted' WHERE id='recover'").run();
    journal.db.prepare("UPDATE dispatches SET payload_hash=? WHERE id='invalid'")
      .run(createHash('sha256').update(String(journal.getDispatch('invalid')!.payload)).digest('hex'));
    await memory.sync('thread');
    assert.equal(batches.flatMap(b=>b.messages).filter(m=>m.text.includes('Background recover')).length,1);
    assert.equal(batches.flatMap(b=>b.messages).filter(m=>m.text.includes('Background invalid')).length,1);
    checks=0;reads=0;await memory.sync('thread');assert.equal(checks,0);assert.equal(reads,0);
    sessionStartedAt=102;checks=0;reads=0;batches.length=0;
    await memory.sync('thread');assert.equal(checks,0);assert.equal(reads,0);
    assert.equal(batches.flatMap(b=>b.messages).filter(m=>m.text.includes('Background')).length,0);
  }finally{journal.close();rmSync(directory,{recursive:true,force:true});}
});
