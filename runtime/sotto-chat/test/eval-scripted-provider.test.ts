import test from 'node:test';
import assert from 'node:assert/strict';
import {assertScriptedEndpoint,createScriptedProvider,scriptedProviderSchema} from '../evals/scripted-provider.js';

test('scripted HTTP boundary rejects unexpected same-origin paths, methods and query selectors',()=>{
  assert.doesNotThrow(()=>assertScriptedEndpoint(new Request('https://scripted-model.invalid/openai/v1/chat/completions',{method:'POST'})));
  for(const [url,method] of [['https://scripted-model.invalid/other','POST'],
    ['https://scripted-model.invalid/openai/v1/chat/completions','GET'],
    ['https://scripted-model.invalid/openai/v1/chat/completions?route=other','POST']])
    assert.throws(()=>assertScriptedEndpoint(new Request(url!,{method})),/Unexpected scripted model endpoint/);
});

test('scripted contract requires actual returned tool evidence, never seeded prompt prose',()=>{
  const script=scriptedProviderSchema.parse({instructions:'Synthetic contract.',steps:[
    {tool:{name:'read',arguments:{}}},
    {text:'Delivered.',toolResultContains:['closing-source-id']},
  ]});
  const seeded=createScriptedProvider(script);
  seeded.respond({tools:[{function:{name:'read'}}]});
  assert.throws(()=>seeded.respond({messages:[{role:'user',content:'closing-source-id'},
    {role:'tool',tool_call_id:'stale-prior-call',content:'closing-source-id'}]}),/returned tool evidence missing/);
  const provider=createScriptedProvider(script);
  provider.respond({tools:[{function:{name:'read'}}]});
  assert.equal(provider.respond({messages:[{role:'tool',tool_call_id:'scripted-1',content:'closing-source-id'}]}).status,200);
  assert.throws(()=>provider.respond({messages:[]}),/exhausted/);
});

test('scripted provider cannot select an unmounted tool or emit forbidden source evidence',()=>{
  const provider=createScriptedProvider({instructions:'Contract.',steps:[{tool:{name:'unknown',arguments:{}}}]});
  assert.throws(()=>provider.respond({tools:[{function:{name:'skill'}}]}),/unavailable tool/);
  const denied=createScriptedProvider({instructions:'Contract.',steps:[{tool:{name:'read',arguments:{}}},{text:'Withheld.',toolResultExcludes:['private-source']}]});
  denied.respond({tools:[{function:{name:'read'}}]});
  assert.throws(()=>denied.respond({messages:[{role:'tool',tool_call_id:'scripted-1',content:'private-source'}]}),/Forbidden returned/);
});

test('a later final response cannot borrow a previous completed turn tool result',()=>{
  const provider=createScriptedProvider({instructions:'Contract.',steps:[
    {tool:{name:'read',arguments:{}}},{text:'First result.',toolResultContains:['evidence']},
    {text:'Later result.',toolResultContains:['evidence']},
  ]});
  provider.respond({tools:[{function:{name:'read'}}]});
  const messages=[{role:'tool',tool_call_id:'scripted-1',content:'evidence'}];
  provider.respond({messages});
  assert.throws(()=>provider.respond({messages}),/returned tool evidence missing/);
});

test('native scripted composition is explicit, bounded and cannot accept a chat request or invented input',()=>{
  const script=scriptedProviderSchema.parse({instructions:'Synthetic only.',steps:[{text:'Chat.'}],nativeResponses:[{
    requestContains:['exact-calendar-occurrence'],response:{prep_markdown:'Synthetic prep.'},
  }]});
  const provider=createScriptedProvider(script);
  assert.throws(()=>provider.respondNative({contents:[]}),/composition input missing/);
  const fresh=createScriptedProvider(script);
  assert.equal(fresh.respondNative({contents:[{text:'exact-calendar-occurrence'}]}).status,200);
  assert.equal(fresh.nativeConsumed,1);
  assert.throws(()=>fresh.respondNative({contents:[]}),/exhausted/);
  assert.doesNotThrow(()=>assertScriptedEndpoint(new Request('https://scripted-model.invalid/native/v1beta/models/gemini-3.8-flash:generateContent',{method:'POST'}),true));
  assert.throws(()=>assertScriptedEndpoint(new Request('https://scripted-model.invalid/openai/v1/chat/completions',{method:'POST'}),true),/Unexpected/);
  assert.throws(()=>scriptedProviderSchema.parse({...script,nativeResponses:Array(3).fill(script.nativeResponses![0])}));
});
