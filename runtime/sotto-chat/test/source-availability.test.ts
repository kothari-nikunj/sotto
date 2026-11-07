import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm, readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import {ConversationSourceGuard, type SourceAuthority, unavailableAuthority} from '../src/agent/source-authority.js';
import {guardedSourceCall} from '../src/agent/source-tools.js';
import {readOwnerGoogle} from '../src/agent/google-read.js';
import {createKnowledgeCommand} from '../src/tools/knowledge-command.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {acceptedFixture} from './helpers/accepted-memory.js';

const revision='a'.repeat(64);
const authority=(online:boolean):SourceAuthority=>({version:2,complete:true,revision,
  bridge_sources:online?['imessage']:[],google_sources:['gmail'],connector_sources:[]});

for(const online of [false,true]) {
  test(`Bridge availability ${online?'loss':'recovery'} keeps Google and company results authorized`,async()=>{
    let state=authority(online),calls=0;
    const sourceAuthority={read:async()=>state};
    const result={version:1,source:'gmail',operation:'gmail_search',items:[],observation:{
      status:'ok',complete:true,observed_at:'2026-10-13T00:00:00Z',coverage:{since:null,until:null}}};
    assert.deepEqual(await readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read',sourceAuthority,
      fetch:async()=>{calls++;state=authority(!online);return Response.json(result);}},
      'gmail_search',{query:'company',max:4},revision),result);
    state=authority(online);
    const company={status:'saved',company_knowledge:{cedar:'Cedar builds clinic tools.'},company_identity:{slug:'cedar',name:'Cedar',domain:'',last_researched:''},coverage:'Saved company record only; original sources were not searched.',next_cursor:null,truncated:false};
    const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read',sourceAuthority,
      isCurrent:()=>true,fetch:async()=>{calls++;state=authority(!online);return Response.json(company);}});
    assert.deepEqual((await command({id:'knowledge-query',args:['--company','Cedar'],mode:'chat'},
      {mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision})).result,company);
    assert.equal(calls,2,'one original read each, no retry');
  });
  test(`Bridge availability ${online?'loss':'recovery'} does not discard a reply or accepted history`,async()=>{
    const directory=await mkdtemp(join(tmpdir(),'sotto-availability-history-'));
    let state=authority(online),change=false;
    const model=new MockLanguageModelV4({doGenerate:async()=>{
      if(change){state=authority(!online);change=false;}
      return {content:[{type:'text',text:'Saved company answer'}],finishReason:{unified:'stop',raw:'STOP'},
        usage:{inputTokens:{total:3,noCache:3,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}},warnings:[]};
    }});
    const runtime=createSottoAgentRuntime({model,storagePath:join(directory,'memory.db'),instructions:'Offline test.',sourceAuthority:{read:async()=>state}});
    try {
      await acceptedFixture(runtime,{text:'EARLIER ACCEPTED OWNER CONTEXT',threadId:'thread',ownerId:'owner'});
      change=true;
      const result=await acceptedFixture(runtime,{text:'CURRENT COMPANY QUESTION',threadId:'thread',ownerId:'owner'});
      assert.equal(result.text,'Saved company answer');assert.equal(result.sourceRevision,revision);
      await runtime.generateChat({text:'What did I just ask?',threadId:'thread',ownerId:'owner'});
      const prompt=JSON.stringify(model.doGenerateCalls.at(-1)?.prompt);
      assert.ok(prompt.includes('EARLIER ACCEPTED OWNER CONTEXT'));
      assert.ok(prompt.includes('CURRENT COMPANY QUESTION'));
      assert.equal(model.doGenerateCalls.length,3,'no automatic model retry');
    } finally {await runtime.close();await rm(directory,{recursive:true,force:true});}
  });
}
for(const transition of ['revision','google-revoked','connector-revoked'] as const) {
  test(`source guard still purges and withholds for ${transition}`,async()=>{
    const directory=await mkdtemp(join(tmpdir(),'sotto-availability-revocation-'));
    let state:SourceAuthority={...authority(true),connector_sources:['granola']},purges=0;
    const guard=new ConversationSourceGuard({reader:{read:async()=>state},baselinePath:join(directory,'proof.json'),purge:async()=>{purges++;}});
    try {
      const before=await guard.begin();
      state=transition==='revision'?{...state,revision:'b'.repeat(64)}:
        transition==='google-revoked'?{...state,google_sources:[]}:{...state,connector_sources:[]};
      await assert.rejects(guard.finish(before),/withheld/);
      assert.equal(purges,2);
    } finally {await rm(directory,{recursive:true,force:true});}
  });
}
test('Bridge tools remain unavailable offline and withhold a read when availability changes',async()=>{
  let state=authority(false),calls=0;
  const options={name:'get_messages',args:{},reader:{read:async()=>state},revision,
    invoke:async()=>{calls++;state=authority(false);return {content:[{type:'text',text:'{"messages":["private"]}'}]};}};
  await assert.rejects(guardedSourceCall(options),/withheld/);assert.equal(calls,0);
  state=authority(true);
  await assert.rejects(guardedSourceCall(options),/withheld/);assert.equal(calls,1);
});

test('temporary unknown authority withholds access without destroying the verified cache',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'sotto-authority-outage-'));
  const path=join(directory,'proof.json');let state=authority(true);
  let cache:string[]=[];
  const guard=new ConversationSourceGuard({reader:{read:async()=>state},baselinePath:path,purge:async()=>{cache=[];}});
  try {
    const before=await guard.begin();cache=['Previously accepted source-backed reply'];
    const proof=await readFile(path,'utf8');state=unavailableAuthority();
    await assert.rejects(guard.finish(before),/withheld/);
    assert.equal((await guard.begin()).complete,false,'unknown authority never authorizes recall');
    assert.deepEqual(cache,['Previously accepted source-backed reply']);
    assert.equal(await readFile(path,'utf8'),proof,'outage must not replace the verified baseline');
    state=authority(true);assert.equal((await guard.begin()).revision,revision);
    assert.deepEqual(cache,['Previously accepted source-backed reply'],'same verified revision retains history');
    state={...state,revision:'b'.repeat(64)};await guard.begin();
    assert.deepEqual(cache,[],'a confirmed change still destroys old source history before recall');
  }finally{await rm(directory,{recursive:true,force:true});}
});

test('first verified consent after unknown startup remains fenced',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'sotto-first-consent-'));
  const path=join(directory,'proof.json');let state=unavailableAuthority();
  let cache=['Legacy history with no verified baseline'];
  const guard=new ConversationSourceGuard({reader:{read:async()=>state},baselinePath:path,purge:async()=>{cache=[];}});
  try {
    const before=await guard.begin();
    assert.equal(before.complete,false,'unknown startup cannot authorize retained history');
    await assert.rejects(readFile(path),{code:'ENOENT'},'unknown startup cannot invent a baseline');
    state=authority(true);
    await assert.rejects(guard.finish(before),/withheld/,'verification must not release the already uncertain turn');
    assert.deepEqual(cache,[],'first verification clears legacy history before any future recall');
    assert.equal(JSON.parse(await readFile(path,'utf8')).revision,revision);
    assert.equal((await guard.begin()).revision,revision);
    assert.deepEqual(cache,[]);
  }finally{await rm(directory,{recursive:true,force:true});}
});
