import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {MockLanguageModelV4} from 'ai/test';
import {SourceAuthorityChanged} from '../src/agent/source-authority.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
const usage={inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}};
test('standing context is freshly read each attended generation; failure frames unavailable and leaves chat working',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'master-context-runtime-'));let reads=0,step=0;
  const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Answer normally',
    readMasterContext:async()=>{reads++;if(reads===2)throw Error('private reader failure');return {text:'Rule one',chars:8,cap:8000,prompt:'Original master framing: Rule one'};},
    model:new MockLanguageModelV4({doGenerate:async request=>{
      const prompt=JSON.stringify(request.prompt);
      if(step===0)assert.ok(prompt.includes('Original master framing: Rule one'));
      else {assert.ok(prompt.includes('Current standing context is unavailable'));assert.ok(!prompt.includes('Original master framing: Rule one'));}
      step++;return {content:[{type:'text',text:'safe answer'}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};
    }})});
  try {for(const id of ['first','second'])assert.equal((await runtime.generateChat({text:'Hello',threadId:id,ownerId:'owner'})).text,'safe answer');assert.equal(reads,2);}
  finally {await runtime.close();rmSync(folder,{recursive:true,force:true});}
});

test('changed source authority during standing read blocks model contact',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'master-context-change-'));let contacts=0;
  const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Answer normally',
    readMasterContext:async()=>{throw new SourceAuthorityChanged();},
    model:new MockLanguageModelV4({doGenerate:async()=>{contacts++;throw Error('model must not be contacted');}})});
  try {await assert.rejects(runtime.generateChat({text:'Hello',threadId:'fresh',ownerId:'owner'}),SourceAuthorityChanged);assert.equal(contacts,0);}
  finally {await runtime.close();rmSync(folder,{recursive:true,force:true});}
});

for(const mode of ['cancel','generic-source-change','changed','removed'] as const)test(`standing context boundary ${mode}`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'master-context-boundary-'));let contacts=0,revision='a'.repeat(64),reads=0;
  const abort=new AbortController();
  const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Answer normally',
    sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:[]})},
    readMasterContext:async()=>{reads++;if(mode==='cancel'){abort.abort();throw Error('unavailable');}
      if(mode==='generic-source-change'){revision='b'.repeat(64);throw Error('generic receiver failure');}
      const text=reads===1?'Old rule':mode==='removed'?'':'New rule';return {text,chars:text.length,cap:8000,prompt:text?`Original framing: ${text}`:''};},
    model:new MockLanguageModelV4({doGenerate:async request=>{const prompt=JSON.stringify(request.prompt);
      if(contacts===1){assert.ok(!prompt.includes('Original framing: Old rule'));assert.ok(prompt.includes(mode==='removed'?'No current saved standing context':'Original framing: New rule'));}
      contacts++;return {content:[{type:'text',text:'answer'}],finishReason:{unified:'stop',raw:'STOP'},usage,warnings:[]};}})});
  try {if(mode==='cancel'||mode==='generic-source-change'){await assert.rejects(runtime.generateChat({text:'Hello',threadId:'same',ownerId:'owner',signal:abort.signal}));assert.equal(contacts,0);}
    else {for(let i=0;i<2;i++)await runtime.generateChat({text:'Hello',threadId:'same',ownerId:'owner'});assert.equal(reads,2);}}
  finally {await runtime.close();rmSync(folder,{recursive:true,force:true});}
});
