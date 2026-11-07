import {createManagedProxyModels} from '../src/agent/models.js';
import {APICallError,NoOutputGeneratedError,ToolCallRepairError,InvalidToolInputError,TypeValidationError} from 'ai';
import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import {createTool} from '@mastra/core/tools';
import {z} from 'zod';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {classifyModelFailure,projectModelMetrics,type ModelMetrics} from '../src/agent/metrics.js';
import {acceptedFixture} from './helpers/accepted-memory.js';

test('native hooks measure schemas, context, model and tool steps without retaining content',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'model-metrics-'));
  const snapshots:ModelMetrics[]=[];
  const secret='private-owner-argument-result-and-prompt';
  let step=0;
  const tool=createTool({id:'fixture_read',description:'Read fixture',inputSchema:z.object({text:z.string()}),
    execute:async()=>({text:secret})});
  const runtime=createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer safely.',
    chatTools:()=>({fixture_read:tool}),onModelMetrics:value=>{snapshots.push(value);throw new Error('sink unavailable');},
    model:new MockLanguageModelV4({doGenerate:async()=>{
      step++;
      return {content:step===1?[{type:'tool-call',toolCallId:'private-call-id',toolName:'fixture_read',input:JSON.stringify({text:secret})}]:[{type:'text',text:secret}],
        finishReason:{unified:step===1?'tool-calls':'stop',raw:'PRIVATE-FINISH'},
        usage:{inputTokens:{total:7,noCache:7,cacheRead:0,cacheWrite:0},outputTokens:{total:3,text:3,reasoning:0}},warnings:[]};
    }})});
  try{
    assert.equal((await runtime.generateChat({text:secret,threadId:'t',ownerId:'o'})).text,secret);
    const value=snapshots[0]!;
    assert.equal(value.stepCount,2);assert.equal(value.steps.length,2);
    assert.equal(value.toolCount,1);assert.ok(value.toolSchemaBytes>0);assert.equal(value.schemaUnavailable,0);
    assert.ok(value.promptBytes>secret.length);
    assert.deepEqual(value.steps.map(step=>[step.inputTokens,step.outputTokens,step.processorRetries]),[[7,3,0],[7,3,0]]);
    assert.equal(value.tools.length,1);assert.equal(value.tools[0]!.tool,'fixture_read');
    assert.equal(value.tools[0]!.outcome,'success');
    const serialized=JSON.stringify(value);
    for(const forbidden of [secret,'private-call-id','PRIVATE-FINISH'])assert.equal(serialized.includes(forbidden),false);
    assert.deepEqual(projectModelMetrics(value),value);
    for(const invalid of [{...value,prompt:secret},{...value,tools:[{...value.tools[0],arguments:secret}]},
      {...value,steps:[{...value.steps[0],finishReason:secret}]},{...value,toolSchemaBytes:-1}])
      assert.equal(projectModelMetrics(invalid),undefined);
  }finally{await runtime.close();await rm(directory,{recursive:true,force:true});}
});

test('large standing context preserves the recent accepted exchange for a terse follow-up',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'context-budget-'));
  const standing='Current standing instruction. '.repeat(2500);
  const prompts:string[]=[];const metrics:ModelMetrics[]=[];
  const runtime=createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer safely.',
    readMasterContext:async()=>({text:standing,prompt:standing,chars:standing.length,cap:standing.length}),
    onModelMetrics:value=>metrics.push(value),model:new MockLanguageModelV4({doGenerate:async request=>{
      prompts.push(JSON.stringify(request.prompt));
      return {content:[{type:'text',text:'Fixture answer.'}],finishReason:{unified:'stop',raw:'STOP'},
        usage:{inputTokens:{total:10,noCache:10,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}},warnings:[]};
    }})});
  try{
    await acceptedFixture(runtime,{text:'Help me choose an option.',threadId:'t',ownerId:'o'});
    await runtime.generateChat({text:'Yes.',threadId:'t',ownerId:'o'});
    assert.deepEqual(metrics[1]!.rememberedHistory,{users:1,assistants:1});
    assert.ok(prompts[1]!.includes('Help me choose an option.'));
    assert.ok(prompts[1]!.includes('Fixture answer.'));
    assert.equal(prompts.length,2,'preserving native history needs no extra model request');
    assert.ok(prompts[1]!.includes(standing));assert.ok(prompts[1]!.includes('Yes.'));
    assert.equal(metrics[1]!.standingChars,standing.length);
    assert.ok(metrics[1]!.promptBytes>standing.length,'the history window does not truncate current source context');
  }finally{await runtime.close();await rm(directory,{recursive:true,force:true});}
});

test('history diagnostics count recalled roles without current input and explain source purging',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'history-metrics-'));const metrics:ModelMetrics[]=[];
  let complete=true,revision='a'.repeat(64);
  const runtime=createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer.',
    sourceAuthority:{read:async()=>({version:2 as const,complete,revision,bridge_sources:[],google_sources:[]})},
    onModelMetrics:value=>metrics.push(value),model:new MockLanguageModelV4({doGenerate:async()=>({
      content:[{type:'text',text:'Private company answer.'}],finishReason:{unified:'stop',raw:'STOP'},
      usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]})})});
  try {
    await acceptedFixture(runtime,{text:'Private prior request',threadId:'t',ownerId:'o'});
    await runtime.projectConversation({threadId:'t',ownerId:'o',messages:[{id:'current-owner',role:'user',text:'shorter pls',createdAt:Date.now()+2,sourceRevision:'owner-only-context-v1'}]});
    await runtime.generateChat({text:'shorter pls',threadId:'t',ownerId:'o',historyInputIds:['current-owner']});
    assert.deepEqual(metrics.at(-1)!.rememberedHistory,{users:1,assistants:1});
    assert.equal(metrics.at(-1)!.lastMemoryPurge!.reason,'baseline_unavailable');
    complete=false;await runtime.projectConversation({threadId:'t',ownerId:'o',messages:[]});complete=true;
    await runtime.generateChat({text:'shorter pls',threadId:'t',ownerId:'o'});
    assert.deepEqual(metrics.at(-1)!.rememberedHistory,{users:2,assistants:1});
    assert.equal(metrics.at(-1)!.lastMemoryPurge!.reason,'baseline_unavailable');
    revision='b'.repeat(64);await runtime.generateChat({text:'hello',threadId:'t',ownerId:'o'});
    assert.equal(metrics.at(-1)!.lastMemoryPurge!.reason,'revision_changed');
    const value=metrics.at(-1)!;
    for(const bad of [{...value,rememberedHistory:{users:1,assistants:0,text:'private'}},
      {...value,rememberedHistory:{users:-1,assistants:0}},
      {...value,lastMemoryPurge:{reason:'private error',atMs:1}},
      {...value,lastMemoryPurge:{reason:'revision_changed',atMs:1,text:'private'}}]) assert.equal(projectModelMetrics(bad),undefined);
    assert.equal(JSON.stringify(metrics).includes('Private'),false);
  }finally{await runtime.close();await rm(directory,{recursive:true,force:true});}
});


test('native history recalls only the latest twenty messages across restart without trimming current context',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'native-history-window-'));
  const prompts:string[]=[];const metrics:ModelMetrics[]=[];
  const make=()=>createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Current system rule.',
    onModelMetrics:value=>metrics.push(value),model:new MockLanguageModelV4({doGenerate:async request=>{
      prompts.push(JSON.stringify(request.prompt));
      return {content:[{type:'text',text:'Current answer.'}],finishReason:{unified:'stop',raw:'STOP'},
        usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]};
    }})});
  let runtime=make();
  try{
    await runtime.projectConversation({threadId:'t',ownerId:'o',messages:Array.from({length:24},(_,i)=>({
      id:`saved-${i}`,role:i%2?'assistant' as const:'user' as const,text:`Distinct history message ${String(i).padStart(2,'0')}.`,
      createdAt:Date.now()-1000+i,sourceRevision:'owner-only-context-v1',
    }))});
    await runtime.close();runtime=make();
    await runtime.generateChat({text:'shorter pls',threadId:'t',ownerId:'o'});
    assert.deepEqual(metrics[0]!.rememberedHistory,{users:10,assistants:10});
    for(let i=0;i<24;i++)assert.equal(prompts[0]!.includes(`Distinct history message ${String(i).padStart(2,'0')}.`),i>=4);
    assert.ok(prompts[0]!.includes('Current system rule.'));assert.ok(prompts[0]!.includes('shorter pls'));
    assert.equal(prompts.length,1,'windowing performs no summarization or extra model request');
  }finally{await runtime.close();await rm(directory,{recursive:true,force:true});}
});


test('oversized accepted text has a visible history-only bound while ordinary replies remain intact',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'history-message-bound-'));const prompts:string[]=[];
  const runtime=createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer safely.',
    model:new MockLanguageModelV4({doGenerate:async request=>{
      prompts.push(JSON.stringify(request.prompt));return {content:[{type:'text',text:'Ordinary complete reply.'}],finishReason:{unified:'stop',raw:'STOP'},
        usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]};
    }})});
  try{
    const oversized='x'.repeat(40_000)+'UNSTORED_END';
    const messages=[{id:'big',role:'assistant' as const,text:oversized,createdAt:Date.now()-2,sourceRevision:'owner-only-context-v1'},
      {id:'normal',role:'assistant' as const,text:'Ordinary complete reply.',createdAt:Date.now()-1,sourceRevision:'owner-only-context-v1'}];
    await runtime.projectConversation({threadId:'t',ownerId:'o',messages});
    const stored=await runtime.memory.recall({threadId:'t',resourceId:'o',perPage:false});
    const text=stored.messages.find(message=>message.id==='big')!.content.parts.find(part=>part.type==='text')!;
    assert.equal(text.type,'text');if(text.type!=='text')throw new Error('missing text');
    assert.equal(text.text.length,32_768);assert.ok(text.text.endsWith('[Earlier message truncated to the conversation history limit.]'));
    assert.equal(messages[0]!.text,oversized,'projection never mutates the accepted journal input');
    await runtime.generateChat({text:'shorter pls',threadId:'t',ownerId:'o'});
    assert.ok(prompts[0]!.includes('Ordinary complete reply.'));assert.equal(prompts[0]!.includes('UNSTORED_END'),false);
    assert.ok(prompts[0]!.includes('[Earlier message truncated to the conversation history limit.]'));
  }finally{await runtime.close();await rm(directory,{recursive:true,force:true});}
});

for(const [name,category,status] of [
 ['AI_NoOutputGeneratedError','invalid_output',undefined], ['AI_TypeValidationError','invalid_output',undefined],
 ['AI_ToolCallRepairError','invalid_tool_call',undefined], ['AI_APICallError','provider_api',429],
 ['AbortError','aborted',undefined], ['TimeoutError','aborted',undefined], ['private-name','unknown',undefined],
] as const)test(`failed generation has only fixed diagnostic: ${name}`,async()=>{
 const directory=await mkdtemp(join(tmpdir(),'failure-metrics-'));const values:ModelMetrics[]=[];
 const secret='PRIVATE-error-body-url-and-prompt';
 const cause=name==='AbortError'||name==='TimeoutError'?new DOMException(secret,name):
  name==='AI_NoOutputGeneratedError'?new NoOutputGeneratedError({message:secret}):
  name==='AI_TypeValidationError'?new TypeValidationError({value:secret,cause:new Error(secret)}):
  name==='AI_ToolCallRepairError'?new ToolCallRepairError({cause:new Error(secret),originalError:
   new InvalidToolInputError({toolName:secret,toolInput:secret,cause:new Error(secret)})}):
  name==='AI_APICallError'?new APICallError({message:secret,url:'https://private.invalid',
   requestBodyValues:{secret},statusCode:status,responseBody:secret}):new Error(secret);
 const runtime=createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Respond safely.',
  onModelMetrics:value=>values.push(value),model:new MockLanguageModelV4({doGenerate:async()=>{throw new Error('wrapper',{cause});}})});
 try{
  await assert.rejects(runtime.generateChat({text:secret,threadId:'thread',ownerId:'owner'}));
  assert.equal(values.length,1);assert.equal(values[0]!.outcome,'failed');
  assert.deepEqual(values[0]!.failure,{category,...(status?{httpStatus:status}:{})});
  assert.equal(JSON.stringify(values).includes(secret),false);
  assert.deepEqual(projectModelMetrics(values[0]),values[0]);
  for(const failure of [{category:secret},{category:'provider_api',httpStatus:999},
   {category:'invalid_output',message:secret}])assert.equal(projectModelMetrics({...values[0],failure}),undefined);
 }finally{await runtime.close();await rm(directory,{recursive:true,force:true});}
});


for(const [raw,unified,category] of [
 ['MALFORMED_FUNCTION_CALL','error','invalid_tool_call'],
 ['OTHER','other','invalid_output'],
 ['PRIVATE_PROVIDER_FINISH_REASON','error','invalid_output'],
] as const)test(`installed Mastra terminal-output error is classified without provider text: ${unified}/${category}`,async()=>{
 const directory=await mkdtemp(join(tmpdir(),'stream-failure-metrics-'));const values:ModelMetrics[]=[];
 const runtime=createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Respond safely.',
  onModelMetrics:value=>values.push(value),model:new MockLanguageModelV4({doGenerate:async()=>({
   content:[],finishReason:{unified,raw},
   usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]})})});
 try{
  let thrown:unknown;
  await assert.rejects(runtime.generateChat({text:'PRIVATE_OWNER_TEXT',threadId:'thread',ownerId:'owner'}),error=>{
   thrown=error;return true;
  });
  const native=thrown as Error&{id:string;domain:string;details:{rawFinishReason:string};toJSON:()=>unknown};
  assert.equal(native.name,'Error');assert.equal(native.id,'AGENT_STREAM_ERROR');
  assert.equal(native.domain,'AGENT');assert.equal(native.details.rawFinishReason,raw);
  assert.equal(values.length,1);assert.equal(values[0]!.outcome,'failed');
  assert.deepEqual(values[0]!.failure,{category});
  assert.deepEqual(classifyModelFailure(new Error('PRIVATE_WRAPPER',{cause:native.toJSON()})),{category});
  const serialized=JSON.stringify(values);
  for(const secret of ['PRIVATE_OWNER_TEXT',raw,'AGENT_STREAM_ERROR'])assert.equal(serialized.includes(secret),false);
  assert.deepEqual(projectModelMetrics(values[0]),values[0]);
 }finally{await runtime.close();await rm(directory,{recursive:true,force:true});}
});

test('unknown Mastra identities and private details cannot become diagnostic fields',()=>{
 for(const error of [
  {domain:'AGENT',id:'PRIVATE_ID',details:{rawFinishReason:'MALFORMED_FUNCTION_CALL'}},
  {domain:'PRIVATE_DOMAIN',id:'AGENT_STREAM_ERROR',details:{rawFinishReason:'MALFORMED_FUNCTION_CALL'}},
  {domain:'AGENT',code:'PRIVATE_CODE',message:'PRIVATE_MESSAGE'},
 ])assert.deepEqual(classifyModelFailure(error),{category:'unknown'});
 assert.deepEqual(classifyModelFailure({domain:'AGENT',id:'AGENT_STREAM_ERROR',
  get details(){throw new Error('PRIVATE_ACCESSOR');}}),{category:'unknown'});
});


test('Mastra stream wrapper preserves a more specific serialized provider cause',()=>{
 const api=new APICallError({message:'PRIVATE',url:'https://private.invalid',requestBodyValues:{},statusCode:503});
 const wrapper={domain:'AGENT',code:'AGENT_STREAM_ERROR',details:{provider:'PRIVATE'},
  cause:{name:api.name,statusCode:api.statusCode,message:api.message}};
 assert.deepEqual(classifyModelFailure(wrapper),{category:'provider_api',httpStatus:503});
 assert.deepEqual(classifyModelFailure({...wrapper,cause:new Error('PRIVATE')}),{category:'invalid_output'});
});


test('managed compatibility transport exposes malformed terminal output after one HTTP response',async()=>{
 const directory=await mkdtemp(join(tmpdir(),'managed-stream-failure-'));const values:ModelMetrics[]=[];
 let calls=0;
 const models=createManagedProxyModels({origin:'https://proxy.example',allowedOrigin:'https://proxy.example',
  bearer:'fixture',chatModelId:'chat-fixture',nativeMediaModelId:'media-fixture',fetch:async(input,init)=>{
   calls++;assert.equal(String(input),'https://proxy.example/openai/v1/chat/completions');
   assert.equal(new Headers(init?.headers).get('X-Sotto-Attempt'),'1');
   return new Response(JSON.stringify({id:'fixture',created:1,model:'chat-fixture',choices:[{index:0,
    message:{role:'assistant',content:''},finish_reason:'MALFORMED_FUNCTION_CALL'}],
    usage:{prompt_tokens:1,completion_tokens:1,total_tokens:2}}),{status:200,headers:{'content-type':'application/json'}});
  }});
 const runtime=createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Respond safely.',
  model:models.chatModel,onModelMetrics:value=>values.push(value)});
 try{
  await assert.rejects(runtime.generateChat({text:'PRIVATE_OWNER_TEXT',threadId:'thread',ownerId:'owner'}),error=>{
   const native=error as Error&{id:string;details:{rawFinishReason:string}};
   assert.equal(native.name,'Error');assert.equal(native.id,'AGENT_STREAM_ERROR');
   assert.equal(native.details.rawFinishReason,'MALFORMED_FUNCTION_CALL');return true;
  });
  assert.equal(calls,1,'diagnostics add no retry or second transport request');
  assert.equal(values.length,1);assert.equal(values[0]!.outcome,'failed');
  assert.deepEqual(values[0]!.failure,{category:'invalid_tool_call'});
  for(const privateText of ['PRIVATE_OWNER_TEXT','MALFORMED_FUNCTION_CALL','proxy.example'])
   assert.equal(JSON.stringify(values).includes(privateText),false);
 }finally{await runtime.close();await rm(directory,{recursive:true,force:true});}
});
