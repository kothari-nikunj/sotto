import {acceptedFixture} from './helpers/accepted-memory.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import {createTool} from '@mastra/core/tools';
import {z} from 'zod';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
const usage={inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}};
for(const settled of [false,true])for(const large of [false,true])test(`installed history boundary, large trace=${large}, settled=${settled}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'history-boundary-'));let calls=0,phase=0;const final='Accept: I can help.\nDecline: I cannot take this on.';let seen=false,original=false;
 const tool=createTool({id:'synthetic_read',description:'Synthetic read transport result',inputSchema:z.object({}).strict(),execute:async()=>({text:large?'distinct synthetic record '.repeat(20000):'short result'})});
 const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Answer the owner.',sourceAuthority:{read:async()=>({version:2,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['gmail']})},chatTools:()=>({synthetic_read:tool}),model:new MockLanguageModelV4({doGenerate:async request=>{
  calls++;if(phase===0&&calls===2)assert.ok(JSON.stringify(request.prompt).includes(large?'distinct synthetic record':'short result'),'current tool result remains visible');if(phase===1){const prompt=JSON.stringify(request.prompt);seen=prompt.includes(JSON.stringify(final).slice(1,-1));original=prompt.includes('Help me compose options.');}
  const content=phase===0&&calls===1?[{type:'tool-call' as const,toolCallId:'read',toolName:'synthetic_read',input:'{}'}]:[{type:'text' as const,text:phase===0?final:'Selection received.'}];
  return {content,finishReason:{unified:phase===0&&calls===1?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
 }})});
 try{
  assert.equal((await acceptedFixture(runtime, {text:'Help me compose options.',threadId:'same-thread',ownerId:'owner'})).text,final);
  const before=await runtime.memory.listThreads({perPage:false});assert.equal(before.threads.length,1);
  if(settled)await runtime.memory.settled();phase=1;await runtime.generateChat({text:'Use Decline.',threadId:'same-thread',ownerId:'owner'});
  await runtime.memory.settled();const stored=await (await runtime.memory.storage.getStore('memory'))!.listMessages({threadId:'same-thread',perPage:false});
  for(const message of stored.messages)if(message.role==='assistant'){assert.deepEqual(Object.keys(message.content).sort(),['format','parts']);assert.ok(message.content.parts.every(part=>part.type==='text'));}
  console.log(JSON.stringify({large,stored_groups:stored.messages.map(message=>({role:message.role,parts:message.content.parts.map(part=>part.type),has_final:JSON.stringify(message.content).includes(JSON.stringify(final).slice(1,-1)),has_owner:JSON.stringify(message.content).includes('Help me compose options.'),tool_ids:[...new Set(message.content.parts.filter(part=>part.type==='tool-invocation').map(part=>part.toolInvocation.toolCallId))]}))}));
  const after=await runtime.memory.listThreads({perPage:false});assert.equal(after.threads.length,1);assert.equal(before.threads[0]!.id,after.threads[0]!.id);
  console.log(JSON.stringify({large,settled,final_present:seen,original_present:original,thread_unchanged:true,boundary:after.threads[0]!.metadata?.memoryTokenLimiter??null}));
  assert.equal(seen,true);assert.equal(original,true);
 }finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});
for(const revoked of ['revision','incomplete'] as const)test(`native conversation history survives reopen and ${revoked} purges it`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'history-reopen-'));const final='Accept: A distinctive retained option.\nDecline: A distinctive retained decline.';let phase=0,calls=0,revision='a'.repeat(64),complete=true;
 const sourceAuthority={read:async()=>({version:2 as const,complete,revision:complete?revision:null,bridge_sources:[],google_sources:['gmail' as const]})};
 const tool=createTool({id:'synthetic_read',description:'Read synthetic data',inputSchema:z.object({}).strict(),execute:async()=>({text:'distinct synthetic record '.repeat(20000)})});
 const model=new MockLanguageModelV4({doGenerate:async request=>{
  calls++;const prompt=JSON.stringify(request.prompt);
  if(phase===1){assert.ok(prompt.includes(JSON.stringify(final).slice(1,-1)));assert.ok(prompt.includes('Original owner request.'));assert.ok(!prompt.includes('distinct synthetic record'));}
  if(phase===2){assert.ok(!prompt.includes('distinctive retained'));assert.ok(!prompt.includes('Original owner request.'));}
  return {content:phase===0&&calls===1?[{type:'tool-call' as const,toolCallId:'read',toolName:'synthetic_read',input:'{}'}]:[{type:'text' as const,text:phase===0?final:'Current answer.'}],finishReason:{unified:phase===0&&calls===1?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
 }});
 const mount=()=>createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Answer safely.',sourceAuthority,chatTools:()=>({synthetic_read:tool})});let runtime=mount();
 try{
  assert.equal((await acceptedFixture(runtime, {text:'Original owner request.',threadId:'same-thread',ownerId:'owner'})).text,final);await runtime.close();
  runtime=mount();phase=1;await runtime.generateChat({text:'Use Decline.',threadId:'same-thread',ownerId:'owner'});
  if(revoked==='revision')revision='b'.repeat(64);else complete=false;
  phase=2;await runtime.generateChat({text:'Hello.',threadId:'same-thread',ownerId:'owner'});
 }finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});
test('terminal tool-only handoff does not leave an oversized trace in native history',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'history-tool-only-'));let calls=0;
 const tool=createTool({id:'synthetic_queue',description:'Synthetic validated queue handoff',inputSchema:z.object({}).strict(),execute:async(_input,context)=>{context!.requestContext!.set('ownerSkillQueued',true);return {text:'large completed trace '.repeat(20000)};}});
 const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Answer.',chatTools:()=>({synthetic_queue:tool}),model:new MockLanguageModelV4({doGenerate:async request=>{
  calls++;if(calls===2){assert.ok(JSON.stringify(request.prompt).includes('Queue request.'));assert.ok(!JSON.stringify(request.prompt).includes('large completed trace'));}
  return {content:calls===1?[{type:'tool-call' as const,toolCallId:'queue',toolName:'synthetic_queue',input:'{}'}]:[{type:'text' as const,text:'Hello.'}],finishReason:{unified:calls===1?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
 }})});
 try{await acceptedFixture(runtime, {text:'Queue request.',threadId:'same-thread',ownerId:'owner'});assert.equal(calls,1);await runtime.generateChat({text:'Hello.',threadId:'same-thread',ownerId:'owner'});assert.equal(calls,2);}
 finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});
