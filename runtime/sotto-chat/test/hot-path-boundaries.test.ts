import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {OperationalJournal} from '../src/journal/index.js';
import {AcceptedConversationMemory} from '../src/conversation-memory.js';
import {ConversationSourceGuard} from '../src/agent/source-authority.js';
import {ownerContinuityEndpoint, readOwnerContinuityJson} from '../src/agent/owner-continuity-client.js';
import {ownerReadRequest, readCancelled} from '../src/tools/read-admission.js';

test('independent authority and baseline reads overlap; purge waits for both', async () => {
  let releaseAuthority!: (value: any) => void, releaseBaseline!: (value: string) => void;
  let baselineStarted = false, purges = 0;
  const guard = new ConversationSourceGuard({baselinePath:'/unused',
    reader:{read:()=>new Promise(resolve=>{releaseAuthority=resolve;})},purge:async()=>{purges++;}});
  Object.defineProperty(guard,'baseline',{value:()=>{baselineStarted=true;return new Promise(resolve=>{releaseBaseline=resolve;});}});
  const pending=guard.begin();
  assert.equal(baselineStarted,true);
  assert.equal(purges,0);
  const authority={version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:[],connector_sources:[]};
  releaseAuthority(authority);await Promise.resolve();assert.equal(purges,0);
  releaseBaseline(authority.revision);
  assert.deepEqual(await pending,authority);assert.equal(purges,0);
});

test('cached private endpoint still validates changed configuration and credentials', async () => {
  const urls:string[]=[];
  const options={bridgeUrl:'http://127.0.0.1:43210/mcp',readBearer:'first',fetch:async(input:any)=>{
    urls.push(String(input));return new Response('{}',{headers:{'content-type':'application/json'}});
  }};
  await readOwnerContinuityJson(options,'owner-loops',{});
  const external=ownerContinuityEndpoint(options,'knowledge-read');external.hostname='attacker.example';
  options.readBearer='second';await readOwnerContinuityJson(options,'knowledge-read',{});
  assert.deepEqual(urls,['http://127.0.0.1:43210/runtime/owner-loops','http://127.0.0.1:43210/runtime/knowledge-read']);
  for (const route of ['knowledge-read?other=1', '../other', 'google-read#fragment'])
    await assert.rejects(readOwnerContinuityJson(options,route as never,{}));
  assert.equal(urls.length,2);
  options.readBearer='bad\r\nvalue';await assert.rejects(readOwnerContinuityJson(options,'knowledge-read',{}));
  options.readBearer='second';options.bridgeUrl='https://attacker.example/mcp';
  await assert.rejects(readOwnerContinuityJson(options,'knowledge-read',{}));assert.equal(urls.length,2);
  options.bridgeUrl='http://[::1]:43211/mcp';await readOwnerContinuityJson(options,'owner-loops',{});
  assert.equal(urls[2],'http://[::1]:43211/runtime/owner-loops');
});

test('reused policy expressions preserve repeated courtesy, refusal and Unicode decisions', () => {
  for(let i=0;i<20;i++){
    assert.equal(ownerReadRequest('please research Noé Dupont'),true);
    assert.equal(readCancelled('please don’t look up Dana Reed'),true);
    assert.equal(ownerReadRequest('look up Dana Reed. nvm'),false);
  }
});


test('memory batch checkpoints roll back together and retry the same native IDs', async () => {
  const directory=mkdtempSync(join(tmpdir(),'sotto-memory-batch-'));
  const journal=new OperationalJournal(join(directory,'journal.sqlite'));
  const batches:string[][]=[];
  const memory=new AcceptedConversationMemory({journal,ownerId:'owner',threadFor:()=> 'native-thread',
    sessionStartedAt:()=>0,scopeThread:thread=>createHash('sha256').update(JSON.stringify(['account','line','telegram',thread])).digest('hex'),reset:async()=>{},
    project:async input=>{batches.push(input.messages.map(message=>message.id));return 'settled';}});
  try {
    for(let i=0;i<3;i++) journal.admitInbound({eventId:`synthetic-${i}`,accountKey:'account',lineKey:'line',
      threadKey:'thread',channel:'telegram',sender:'owner',payload:JSON.stringify({inbound:{text:`Synthetic ${i}`},context:{threadId:'thread'}})});
    journal.db.exec(`CREATE TRIGGER fail_checkpoint BEFORE INSERT ON conversation_memory_done
      WHEN (SELECT COUNT(*) FROM conversation_memory_done)>0 BEGIN SELECT RAISE(ABORT,'synthetic checkpoint failure'); END;`);
    await assert.rejects(memory.sync('thread'),/synthetic checkpoint failure/);
    assert.equal(journal.db.prepare('SELECT COUNT(*) AS n FROM conversation_memory_done').get()!.n,0);
    journal.db.exec('DROP TRIGGER fail_checkpoint');
    await memory.sync('thread');
    assert.deepEqual(batches[1],batches[0]);assert.equal(batches[0]!.length,3);
    assert.equal(journal.db.prepare('SELECT COUNT(*) AS n FROM conversation_memory_done').get()!.n,3);
    await memory.sync('thread');assert.equal(batches.length,2);
  } finally {await memory.drain();journal.close();rmSync(directory,{recursive:true,force:true});}
});
