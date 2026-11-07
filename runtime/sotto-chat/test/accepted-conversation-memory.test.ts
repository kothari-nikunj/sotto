import {createOwnerTaskToolClient} from '../src/tools/owner-task-tool-client.js';
import {createOwnerTaskTools} from '../src/tools/owner-task-tool.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';
import {ownerTaskToolKind} from '../src/tools/owner-task-tool-contract.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {startApplication} from '../src/application.js';
import type {OperationalJournal} from '../src/journal/index.js';

const usage = {inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
const inbound = (eventId:string,text:string) => ({channel:'telegram' as const,eventId,accountId:'bot',senderId:'42',isDirect:true,text});

async function fixture() {
  const directory = await mkdtemp(join(tmpdir(),'accepted-memory-'));
  let answer = 'A distinctive offer that has not arrived.';
  let acceptance: 'accepted' | 'rejected' | 'unknown' = 'accepted';
  let failProjection = false;
  let failAfterSave = false;
  let journal!: OperationalJournal;
  const prompts: string[] = [];
  const posts: string[] = [];
  let revision = 'a'.repeat(64);
  let complete = true;
  let task: {name:string;input:unknown} | undefined;
  let loops: import('../src/agent/owner-loops.js').OwnerLoops['you_owe'] = [];
  let hold: (()=>Promise<void>) | undefined;
  const sourceAuthority={read:async()=>({version:2 as const,revision,complete,bridge_sources:[],google_sources:[],connector_sources:[]})};
  const client=createOwnerTaskToolClient({bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),readBearer:'c'.repeat(64),sourceAuthority,
    identity:{tenant_id:'fixture',channel:'telegram',account_id:'bot',owner_id:'42',line_key:'bot',target:'telegram',
      route_mode:'fixed',route_id:'telegram:42',bot_fingerprint:'a'.repeat(64)},fetch:async(_url,init)=>{
      const body=JSON.parse(String(init?.body));
      if(String(_url).endsWith('/runtime/knowledge-read'))return new Response(JSON.stringify({coverage:'currently_permitted_canonical_loops',
        you_owe:loops,waiting_on_them:[],counts:{you_owe:loops.length,waiting_on_them:0}}),{status:200});
      const base={version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,
        args_hash:stableActionIdentity(body.event_id,ownerTaskToolKind,body.args).payloadHash,operation:body.args.operation};
      const receipt=body.args.operation==='resolve_many'?{...base,outcome:'complete',items:body.args.items.map((item:{anchor_key:string},i:number)=>
        ({index:i+1,action:'resolve',anchor_key:item.anchor_key,status:'applied',post_digest:'b'.repeat(64),loop_status:'resolved'}))}:
        {...base,anchor_key:'owner:test',post_digest:'b'.repeat(64),loop_status:'open'};
      loops=[];return new Response(JSON.stringify(receipt),{status:200});
    }});

  const model = new MockLanguageModelV4({doGenerate:async request => {
    prompts.push(JSON.stringify(request.prompt));
    if(hold){const wait=hold;hold=undefined;await wait();}
    if(task){const call=task;task=undefined;return {content:[{type:'tool-call',toolCallId:'fixture-tool',toolName:call.name,input:JSON.stringify(call.input)}],finishReason:{unified:'tool-calls',raw:'STOP'},usage,warnings:[]};}

    return {content:[{type:'text',text:answer}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};
  }});
  const start = async () => {
    const runtime = createSottoAgentRuntime({model,storagePath:join(directory,'memory.db'),instructions:'Answer briefly.',
      sourceAuthority,chatTools:()=>createOwnerTaskTools({client,journal:()=>journal,sourceAuthority})});
    const app = await startApplication({directory,mediaRoot:directory,bearerToken:'fixture-only-bearer-for-local-tests-000',
      owner:{channel:'telegram',accountId:'bot',ownerId:'42'},lineKey:'bot',target:'owner',providerThreadId:'telegram:42',deferListen:true}, {
      agent:{...runtime,projectConversation:async input => {
        if (failProjection && input.messages.some(message=>message.role==='assistant')) throw new Error('projection interrupted');
        const result = await runtime.projectConversation(input);
        if(failAfterSave&&input.messages.some(message=>message.role==='assistant'))throw new Error('checkpoint interrupted');
        return result;
      }},
      transport:{send:async request => {posts.push(request.text ?? '');return {acceptance,providerMessageIds:acceptance==='accepted'?[`p${posts.length}`]:acceptance==='unknown'?['partial-id']:[]};}},
      onJournalReady:value=>{journal=value;},ownerTaskTool:client,
    });
    return {app,runtime,close:async()=>{await app.close();await runtime.close();}};
  };
  return {directory,start,prompts,posts,get journal(){return journal;},set answer(value:string){answer=value;},
    set acceptance(value:typeof acceptance){acceptance=value;},set failProjection(value:boolean){failProjection=value;},set failAfterSave(value:boolean){failAfterSave=value;},
    set task(value:{name:string;input:unknown}){task=value;
      if(value.name==='owner_task_resolve_many')loops=(value.input as {items:Array<{name:string;anchor_key:string;loop_version:string;loop_identity:string}>}).items.map(item=>({
        name:item.name,anchor_key:item.anchor_key,loop_version:item.loop_version,loop_identity:item.loop_identity,what:'Fixture obligation',channel:'imessage',identifier:'fixture',thread_id:'fixture',group_id:'',action_type:'reply',age_days:1,deadline:null,
        overdue:false,chased_count:0,chase_pending:null,last_chased_at:null,chase_after:null,chased_out:false,handoff_asked_at:null}));
    },set hold(value:()=>Promise<void>){hold=value;},
    set revision(value:string){revision=value;},set complete(value:boolean){complete=value;},
    cleanup:()=>rm(directory,{recursive:true,force:true})};
}

for (const acceptance of ['rejected','unknown'] as const) test(`${acceptance} delivery never becomes assistant history`, async()=>{
  const f = await fixture(); const run = await f.start();
  try {
    f.acceptance=acceptance;
    assert.equal((await run.app.receive(inbound('1','Give me an option.'))).status,'held');
    const threads=await run.runtime.memory.listThreads({perPage:false});
    const recalled=await run.runtime.memory.recall({threadId:threads.threads[0]!.id,resourceId:threads.threads[0]!.resourceId});
    assert.ok(JSON.stringify(recalled.messages).includes('Give me an option.'));
    assert.equal(JSON.stringify(recalled.messages).includes('distinctive offer'),false);
    f.acceptance='accepted'; f.answer='Second answer.';
    await run.app.receive(inbound('2','What next?'));
    assert.equal(f.prompts.at(-1)!.includes('distinctive offer'),false);
    assert.equal(f.prompts.at(-1)!.split('What next?').length-1,1,'current owner input is not duplicated by recall');
  } finally {await run.close();await f.cleanup();}
});

test('accepted projection repairs after restart without another send or duplicate native messages',async()=>{
  const f=await fixture();let run=await f.start();
  try {
    f.failProjection=true;
    assert.equal((await run.app.receive(inbound('1','Give me an option.'))).status,'accepted');
    await run.close();f.failProjection=false;run=await f.start();
    assert.equal(f.posts.length,1);
    f.answer='Second answer.';
    await run.app.receive(inbound('2','Yes, that one.'));
    assert.ok(f.prompts.at(-1)!.includes('distinctive offer'));
    assert.equal(f.prompts.at(-1)!.split('distinctive offer').length-1,1);
  } finally {await run.close();await f.cleanup();}
});

test('many accepted pasted turns keep a bounded recalled prompt and the preceding owner-answer pair',async()=>{
  const f=await fixture();const run=await f.start();
  try{
    for(let i=0;i<10;i++){
      f.answer=i===9?`Accepted answer 9: ${'y'.repeat(27_000)} END-OF-LATEST-ANSWER`:`Accepted answer ${i}.`;
      await run.app.receive(inbound(`paste-${i}`,`Pasted material ${i}: `+'x'.repeat(27_000)));
    }
    f.answer='Short follow-up answer.';
    await run.app.receive(inbound('followup','Can you make that shorter?'));
    const prompt=f.prompts.at(-1)!;
    assert.ok(prompt.length<100_000,`recalled prompt should be bounded, got ${prompt.length}`);
    assert.ok(prompt.includes('Pasted material 9:')&&prompt.includes('Accepted answer 9:'),
      'the immediately preceding owner-answer pair must remain available');
    assert.ok(prompt.includes('END-OF-LATEST-ANSWER'),'the recent answer ending must remain available');
    assert.ok(prompt.includes('Earlier accepted conversation was omitted'));
    assert.equal(prompt.includes('Pasted material 0:'),false);
    assert.equal(f.journal.db.prepare('SELECT COUNT(*) AS n FROM inbound_events').get()!.n,11,
      'prompt trimming never prunes the durable journal');
  }finally{await run.close();await f.cleanup();}
});

test('legacy oversized accepted history is bounded only in the recalled prompt',async()=>{
  const f=await fixture();const run=await f.start();
  try{
    await run.app.receive(inbound('initial','Hello.'));
    const {threads}=await run.runtime.memory.listThreads({perPage:false});
    const thread=threads[0]!;
    const oldOwner='LEGACY-OWNER '+ 'a'.repeat(90_000)+' OWNER-END';
    const oldReply='LEGACY-REPLY '+ 'b'.repeat(90_000)+' REPLY-END';
    await run.runtime.memory.saveMessages({messages:[
      {id:'legacy-oversized-owner',threadId:thread.id,resourceId:thread.resourceId,role:'user',createdAt:new Date(),content:{format:2,parts:[{type:'text',text:oldOwner}]}},
      {id:'legacy-oversized-reply',threadId:thread.id,resourceId:thread.resourceId,role:'assistant',createdAt:new Date(),content:{format:2,parts:[{type:'text',text:oldReply}]}}
    ]});
    await run.app.receive(inbound('followup','What did we just discuss?'));
    const prompt=f.prompts.at(-1)!;
    assert.ok(prompt.length<100_000,`legacy recall exceeded the bound: ${prompt.length}`);
    assert.ok(prompt.includes('LEGACY-OWNER')&&prompt.includes('LEGACY-REPLY'));
    assert.ok(prompt.includes('Earlier message truncated to the conversation history limit.'));
    const saved=await run.runtime.memory.recall({threadId:thread.id,resourceId:thread.resourceId});
    assert.ok(JSON.stringify(saved.messages).includes('OWNER-END'));
    assert.ok(JSON.stringify(saved.messages).includes('REPLY-END'));
  }finally{await run.close();await f.cleanup();}
});

test('first upgrade discards legacy undelivered prose and rebuilds only accepted journal history', async()=>{
  const f=await fixture();let run=await f.start();
  try {
    f.answer='The accepted option.';
    await run.app.receive(inbound('1','Give me an option.'));
    const {threads}=await run.runtime.memory.listThreads({perPage:false});
    const thread=threads[0]!;
    // Emulate the old writable Mastra transcript beside an existing delivery journal.
    await run.runtime.memory.saveMessages({messages:[{id:'legacy-undelivered',threadId:thread.id,
      resourceId:thread.resourceId,role:'assistant',createdAt:new Date(),
      content:{format:2,parts:[{type:'text',text:'Legacy promise that never reached the owner.'}]}}]});
    f.journal.db.exec('DELETE FROM conversation_memory_done; DELETE FROM conversation_memory_sessions');
    await run.close();run=await f.start();
    const recall=await run.runtime.memory.recall({threadId:thread.id,resourceId:thread.resourceId});
    const history=JSON.stringify(recall.messages);
    assert.ok(history.includes('The accepted option.'));
    assert.equal(history.includes('Legacy promise'),false);
    assert.equal(f.posts.length,1,'upgrading history must not resend an accepted reply');
    f.answer='Current answer.';
    await run.app.receive(inbound('2','Tell me more.'));
    assert.ok(f.prompts.at(-1)!.includes('The accepted option.'));
    assert.equal(f.prompts.at(-1)!.includes('Legacy promise'),false);
  } finally {await run.close();await f.cleanup();}
});

// The previous binary had no projection tables; dropping them emulates its journal.
const previousBinary='DROP TABLE conversation_memory_done; DROP TABLE conversation_memory_sessions; DROP TABLE conversation_memory_excluded';

test('first upgrade rebuilds only the current session after an earlier /new', async()=>{
  const f=await fixture();let run=await f.start();
  try {
    f.answer='Earlier-session reply BRAVO.';
    await run.app.receive(inbound('1','Earlier-session detail ALPHA.'));
    await run.app.receive(inbound('2','/new'));
    f.answer='Current-session reply CHARLIE.';
    await run.app.receive(inbound('3','First message after new.'));
    f.journal.db.exec(previousBinary);
    const sent=f.posts.length;
    await run.close();run=await f.start();
    assert.equal(f.posts.length,sent,'upgrading history must not resend');
    f.answer='After upgrade.';
    await run.app.receive(inbound('4','What next?'));
    const prompt=f.prompts.at(-1)!;
    assert.equal(prompt.includes('ALPHA'),false,'input from before /new stays out of the rebuilt session');
    assert.equal(prompt.includes('BRAVO'),false,'replies from before /new stay out of the rebuilt session');
    assert.ok(prompt.includes('First message after new.'));
    assert.ok(prompt.includes('CHARLIE'));
  } finally {await run.close();await f.cleanup();}
});

test('first upgrade rebuilds nothing for a thread whose session was already rotated', async()=>{
  const f=await fixture();let run=await f.start();
  try {
    f.answer='Yesterday reply BRAVO.';
    await run.app.receive(inbound('1','Yesterday detail ALPHA.'));
    await run.app.rotateSessions();
    // Runs older than the recorded admission boundary are bounded by their own event.
    f.journal.db.exec('UPDATE runs SET admitted_through=NULL');
    f.journal.db.exec(previousBinary);
    await run.close();run=await f.start();
    f.answer='Today.';
    await run.app.receive(inbound('2','Good morning.'));
    assert.equal(f.prompts.at(-1)!.includes('ALPHA'),false);
    assert.equal(f.prompts.at(-1)!.includes('BRAVO'),false);
    f.answer='Later.';
    await run.app.receive(inbound('3','And then?'));
    assert.ok(f.prompts.at(-1)!.includes('Good morning.'),'the new session accumulates normally');
    assert.ok(f.prompts.at(-1)!.includes('Today.'));
  } finally {await run.close();await f.cleanup();}
});

test('late acceptance repairs history without resending; /new prevents old-session replay',async()=>{
  const f=await fixture();let run=await f.start();
  try {
    f.acceptance='unknown';await run.app.receive(inbound('1','Give me an option.'));
    const dispatch=f.journal.db.prepare("SELECT * FROM dispatches WHERE status='unknown'").get()!;
    f.journal.settleDispatch(String(dispatch.id),Number(dispatch.current_attempt),'accepted','late-proof');
    await run.close();f.acceptance='accepted';run=await f.start();
    assert.equal(f.posts.length,1);
    f.answer='Second answer.';await run.app.receive(inbound('2','Tell me more.'));
    assert.ok(f.prompts.at(-1)!.includes('distinctive offer'));
    await run.app.receive(inbound('3','/new'));
    await run.app.receive(inbound('4','Hello again.'));
    assert.equal(f.prompts.at(-1)!.includes('distinctive offer'),false);
    assert.equal(f.prompts.at(-1)!.includes('Give me an option.'),false);
    assert.ok(f.prompts.at(-1)!.includes('I started a new conversation.'));
  } finally {await run.close();await f.cleanup();}
});

test('source withdrawal purges projected history and does not replay completed journal records',async()=>{
  const f=await fixture();const run=await f.start();
  try {
    await run.app.receive(inbound('1','Give me an option.'));
    f.revision='b'.repeat(64);f.answer='Current reply.';
    await run.app.receive(inbound('2','What next?'));
    assert.equal(f.prompts.at(-1)!.includes('distinctive offer'),false);
    assert.equal(f.prompts.at(-1)!.includes('Give me an option.'),false);
  } finally {await run.close();await f.cleanup();}
});


test('canonical task acknowledgment replaces discarded model prose in the next request',async()=>{
  const f=await fixture();const run=await f.start();
  try{
    f.task={name:'owner_task_add',input:{text:'Review the cedar packet'}};
    f.answer='Discarded invented model acknowledgment.';
    assert.equal((await run.app.receive(inbound('1','Add this task: Review the cedar packet'))).status,'accepted');
    assert.equal(f.posts[0],'Added: Review the cedar packet');
    f.answer='Next answer.';await run.app.receive(inbound('2','What next?'));
    assert.ok(f.prompts.at(-1)!.includes('Added: Review the cedar packet'));
    assert.equal(f.prompts.at(-1)!.includes('Discarded invented'),false);
  }finally{await run.close();await f.cleanup();}
});

test('/stop cancels generation and preserves only its accepted control acknowledgment',async()=>{
  const f=await fixture();const run=await f.start();
  let entered!:()=>void,release!:()=>void;
  const ready=new Promise<void>(resolve=>{entered=resolve;});const gate=new Promise<void>(resolve=>{release=resolve;});
  try{
    f.hold=async()=>{entered();await gate;};
    const first=run.app.receive(inbound('1','Give me an option.'));await ready;
    const stopped=run.app.receive(inbound('2','/stop'));release();await first;await stopped;
    f.answer='Next answer.';await run.app.receive(inbound('3','Hello.'));
    assert.equal(f.prompts.at(-1)!.includes('distinctive offer'),false);
    assert.ok(f.posts.some(text=>/stop/i.test(text)));
    assert.ok(f.prompts.at(-1)!.includes(f.posts[0]!));
  }finally{release();await run.close();await f.cleanup();}
});

// Scripted model/provider edges test the conversation boundary, not model reasoning quality.
for(const sample of [
  {name:'terse follow-up',first:'Tell me about Cedar.',reply:'Cedar has a documentation site.',followup:'Their website too?',expected:'Cedar has a documentation site.'},
  {name:'ambiguous identity',first:'Close Alex’s loop.',reply:'I found Alex Chen and Alex Patel. Which Alex?',followup:'Which one?',expected:'Alex Chen and Alex Patel'},
])test(`natural-text fixture: ${sample.name}`,async()=>{
  const f=await fixture();const run=await f.start();
  try{
    f.answer=sample.reply;await run.app.receive(inbound('1',sample.first));
    f.answer='Fixture follow-up.';await run.app.receive(inbound('2',sample.followup));
    assert.ok(f.prompts.at(-1)!.includes(sample.expected));
    assert.equal(f.journal.db.prepare('SELECT COUNT(*) AS n FROM actions').get()!.n,0);
  }finally{await run.close();await f.cleanup();}
});

test('natural-text fixture: several loop closures retain per-item canonical receipts',async()=>{
  const f=await fixture();const run=await f.start();
  try{
    const items=['Tina','Hari','Dana'].map((name,index)=>({name,anchor_key:`loop-${index}`,loop_version:'a'.repeat(64),loop_identity:'b'.repeat(64),correction:name}));
    f.task={name:'owner_task_resolve_many',input:{selection_mode:'named_people',items}};
    f.answer='Discarded blanket completion claim.';
    assert.equal((await run.app.receive(inbound('1','Tina, Hari, and Dana are sorted. What else is open?'))).status,'accepted');
    for(const item of items)assert.ok(f.posts[0]!.includes(`${item.name}: Marked done.`),f.posts[0]);
    f.answer='Next answer.';await run.app.receive(inbound('2','What’s left?'));
    for(const item of items)assert.ok(f.prompts.at(-1)!.includes(`${item.name}: Marked done.`));
    assert.equal(f.prompts.at(-1)!.includes('Discarded blanket'),false);
  }finally{await run.close();await f.cleanup();}
});


test('native save replay is idempotent when the checkpoint fails, and a revoked pending reply is dropped',async()=>{
  const f=await fixture();let run=await f.start();
  try{
    f.failAfterSave=true;await run.app.receive(inbound('1','First request.'));
    await run.close();f.failAfterSave=false;run=await f.start();
    f.answer='Second answer.';await run.app.receive(inbound('2','Next?'));
    assert.equal(f.prompts.at(-1)!.split('distinctive offer').length-1,1);
    f.failProjection=true;f.answer='Accepted under the old source revision.';
    await run.app.receive(inbound('3','Another option.'));
    await run.close();f.failProjection=false;f.revision='b'.repeat(64);run=await f.start();
    f.answer='Current reply.';await run.app.receive(inbound('4','Now?'));
    assert.equal(f.prompts.at(-1)!.includes('Accepted under the old'),false);
    assert.equal(f.posts.length,4,'repair never resends');
  }finally{await run.close();await f.cleanup();}
});

test('journal compaction after /new does not exclude a later reused rowid',async()=>{
  const f=await fixture();let run=await f.start();
  try{
    await run.app.receive(inbound('1','Old request.'));
    await run.app.receive(inbound('2','/new'));
    f.journal.pruneTerminal(Date.now()+1000);
    f.answer='A fresh retained answer.';await run.app.receive(inbound('3','Fresh request.'));
    await run.close();run=await f.start();
    f.answer='Follow-up answer.';await run.app.receive(inbound('4','More?'));
    assert.ok(f.prompts.at(-1)!.includes('A fresh retained answer.'));
    assert.equal(f.prompts.at(-1)!.includes('Old request.'),false);
  }finally{await run.close();await f.cleanup();}
});

test('unavailable source authority defers pending accepted projection until the receiver is ready',async()=>{
  const f=await fixture();let run=await f.start();
  try{
    f.failProjection=true;await run.app.receive(inbound('1','First request.'));
    await run.close();f.failProjection=false;f.complete=false;run=await f.start();
    assert.equal(f.posts.length,1);
    f.complete=true;f.answer='Second answer.';await run.app.receive(inbound('2','Next?'));
    assert.ok(f.prompts.at(-1)!.includes('distinctive offer'));
    assert.equal(f.posts.length,2);
  }finally{await run.close();await f.cleanup();}
});

test('quoted reply evidence stays transient while admitted owner text is saved verbatim',async()=>{
  const f=await fixture();const run=await f.start();
  try{
    await run.app.receive(inbound('1','First request.'));
    f.answer='Second answer.';
    await run.app.receive({...inbound('2','That one.'),replyBound:true,replyToMessageId:'p1'});
    assert.ok(f.prompts.at(-1)!.includes('Exact locally accepted reply quoted by the owner'));
    const threads=await run.runtime.memory.listThreads({perPage:false});
    const stored=await run.runtime.memory.recall({threadId:threads.threads[0]!.id,resourceId:threads.threads[0]!.resourceId});
    const owner=stored.messages.filter(message=>message.role==='user');
    assert.ok(JSON.stringify(owner).includes('That one.'));
    assert.equal(JSON.stringify(owner).includes('Exact locally accepted'),false);
    assert.equal(JSON.stringify(owner).includes('distinctive offer'),false);
  }finally{await run.close();await f.cleanup();}
});

for(const boundary of ['same','restart','new','nightly','retention','changed'] as const)
test(`authority outage suspends actual model recall and respects recovery boundary: ${boundary}`,async()=>{
  const f=await fixture();let run=await f.start();
  try {
    f.answer='PRIVATE ACCEPTED COMPANY BLURB';
    await run.app.receive(inbound('outage-company','Private company request'));
    const threads=await run.runtime.memory.listThreads({perPage:false});
    const thread=threads.threads[0]!;
    f.complete=false;f.answer='Fresh owner-only reply.';
    await run.app.receive(inbound('outage-followup','shorter pls'));
    assert.equal(f.prompts.at(-1)!.includes('PRIVATE ACCEPTED COMPANY BLURB'),false);
    assert.equal(f.prompts.at(-1)!.includes('Private company request'),false);
    assert.ok(JSON.stringify((await run.runtime.memory.recall({threadId:thread.id,resourceId:thread.resourceId})).messages).includes('PRIVATE ACCEPTED COMPANY BLURB'),
      'outage suspends model access without erasing the already verified cache');
    if(boundary==='restart'){await run.close();run=await f.start();}
    if(boundary==='new')await run.app.receive(inbound('outage-reset','/new'));
    if(boundary==='nightly')await run.app.rotateSessions();
    if(boundary==='retention')await run.runtime.purgeThreadsBefore(new Date(Date.now()+60_000));
    if(boundary==='changed')f.revision='b'.repeat(64);
    f.complete=true;f.answer='Current answer.';
    await run.app.receive(inbound('recovered-followup','Can you shorten the blurb?'));
    assert.equal(f.prompts.at(-1)!.includes('PRIVATE ACCEPTED COMPANY BLURB'),['same','restart'].includes(boundary));
    assert.equal(f.posts.filter(text=>text==='PRIVATE ACCEPTED COMPANY BLURB').length,1,'no replayed delivery');
  } finally {await run.close();await f.cleanup();}
});

test('source becoming unavailable during generation withholds reply without replay or source exposure on the next turn',async()=>{
 const f=await fixture();const run=await f.start();
 try {
  f.answer='PRIVATE ACCEPTED COMPANY BLURB';await run.app.receive(inbound('stable-company','Private company request'));
  f.hold=async()=>{f.complete=false;};f.answer='Must be withheld';
  await run.app.receive(inbound('uncertain-followup','shorter pls'));
  assert.equal(f.posts.includes('Must be withheld'),false);
  f.answer='Owner-only fresh answer';await run.app.receive(inbound('still-offline','hello'));
  assert.equal(f.prompts.at(-1)!.includes('PRIVATE ACCEPTED COMPANY BLURB'),false);
  f.complete=true;f.answer='Recovered answer';await run.app.receive(inbound('now-online','shorten the prior blurb'));
  assert.equal(f.prompts.at(-1)!.includes('PRIVATE ACCEPTED COMPANY BLURB'),true);
  assert.equal(f.posts.includes('Must be withheld'),false);
 }finally{await run.close();await f.cleanup();}
});
