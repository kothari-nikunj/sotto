import {projectTaskToolDiagnostics,type TaskToolDiagnosticBatch} from '../src/agent/tool-diagnostics.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {MockLanguageModelV4} from 'ai/test';
import {createTool} from '@mastra/core/tools';
import {z} from 'zod';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {createGoogleReadTools} from '../src/agent/google-read.js';
import {startApplication} from '../src/application.js';

// Pass real health through the existing adapter and overview, never a copied projector.
const overview = `import json,sys
from types import SimpleNamespace
sys.path.insert(0,sys.argv[1]+'/adapters/sotto')
sys.path.insert(0,sys.argv[1]+'/runtime/trigger-receiver')
import runtime_api,dashboard
health=json.load(sys.stdin)
runtime_api._client=lambda timeout:SimpleNamespace(call=lambda *a,**kw:SimpleNamespace(ok=True,value=health))
dashboard.HOOKS['runtime_observation']=runtime_api.runtime_observation
print(json.dumps(dashboard.api_overview()))
`;

test('actual task rejection survives model step, authenticated health and receiver overview without private content',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'task-refusal-overview-'));
 const secret='PRIVATE-OWNER-TEXT-TOOL-ARGS';
 let batch:TaskToolDiagnosticBatch|undefined;let calls=0;
 const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:secret,
  chatTools:()=>({owner_task_resolve_many:createTool({id:'owner_task_resolve_many',description:'Synthetic rejection',inputSchema:z.object({text:z.string()}),
   execute:async()=>({status:'rejected',code:'owner_span_unavailable',private:secret})})}),
  onTaskToolDiagnostics:events=>{batch={events:[...events],completedAtMs:1791450000000};},
  model:new MockLanguageModelV4({doGenerate:async()=>{
   calls++;return {content:calls===1?[{type:'tool-call',toolCallId:'fixture',toolName:'owner_task_resolve_many',input:JSON.stringify({text:secret})}]:[{type:'text',text:'Nothing changed.'}],
    finishReason:{unified:calls===1?'tool-calls':'stop',raw:'STOP'},warnings:[],usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}}};
  }})});
 const bearer='synthetic-diagnostic-health-bearer-000000000';
 const app=await startApplication({directory:join(folder,'app'),mediaRoot:folder,bearerToken:bearer,
  owner:{channel:'telegram',accountId:'bot',ownerId:'42'},lineKey:'bot',target:'owner',providerThreadId:'telegram:42'}, {
  agent:{generateChat:async()=>({text:''}),generateUnattended:async()=>({text:''}),rotateThread:async()=>{},purgeThreadsBefore:async()=>0},
  transport:{send:async()=>({acceptance:'accepted',providerMessageIds:['fixture']})},taskToolDiagnostics:()=>batch});
 try{
  await runtime.generateChat({text:secret,threadId:'thread',ownerId:'owner'});
  batch!.events.push({tool:'read_writing_style',status:'unknown',error:'none'},
    {tool:'bridge_search_contacts',status:'unknown',error:'tool_error'},
    {tool:'owner_task_add',status:'recorded',error:'none'},
    {tool:'owner_task_list',status:'proposed',error:'none'});
  const url=`http://127.0.0.1:${app.port}/v1/health`;
  assert.equal((await fetch(url)).status,401);
  const health=await(await fetch(url,{headers:{authorization:`Bearer ${bearer}`}})).json();
  const expected={tool:'owner_task_resolve_many',status:'rejected',error:'none',rejection:'owner_span_unavailable'};
  const expectedEvents=[expected,...batch!.events.slice(1)];
  assert.deepEqual(health.taskToolDiagnostics,{events:expectedEvents,completedAtMs:1791450000000});
  assert.ok(!JSON.stringify(health).includes(secret));
  const child=spawnSync(process.env.SOTTO_PYTHON??process.env.PYTHON??'python3',['-c',overview,resolve('../..')],{
   input:JSON.stringify(health),encoding:'utf8',env:{...process.env,SOTTO_DATA:join(folder,'receiver')},timeout:10000});
  assert.equal(child.status,0,child.stderr);
  const result=JSON.parse(child.stdout);
  assert.deepEqual(result.runtime_health.task_tool_diagnostics,{events:expectedEvents,completed_at_ms:1791450000000});
  assert.ok(!JSON.stringify(result).includes(secret));assert.equal(calls,2);
 }finally{await app.close();await runtime.close();rmSync(folder,{recursive:true,force:true});}
});


test('real Mastra Google exception survives Node health and Python dashboard; validation and later success stay separate',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'google-stage-overview-'));const secret='PRIVATE-QUERY-RECEIVER-BODY';
 let batch:TaskToolDiagnosticBatch|undefined,calls=0,reads=0;
 const revision='a'.repeat(64);const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['gmail'] as ['gmail']})};
 const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Synthetic',sourceAuthority,
  chatTools:()=>createGoogleReadTools({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority,fetch:async()=>{reads++;return reads===1?new Response(JSON.stringify({error:'Google read unavailable',read_stage:'provider_http_other',read_http_status:404,read_phase:'search_metadata'}),{status:503}):new Response(JSON.stringify({version:1,source:'gmail',operation:'gmail_search',items:[],observation:{status:'ok',complete:true,observed_at:'2026-10-23T00:00:00Z',coverage:{since:null,until:null}}}));}}),
  onTaskToolDiagnostics:events=>{batch=projectTaskToolDiagnostics({events:[...events],completedAtMs:1791450000000});},
  model:new MockLanguageModelV4({doGenerate:async()=>{calls++;return {content:calls<=3?[{type:'tool-call',toolCallId:'synthetic-'+calls,toolName:'gmail_search',input:JSON.stringify(calls===1?{query:secret}:{query:secret,max:1})}]:[{type:'text',text:'Synthetic completion.'}],finishReason:{unified:calls<=3?'tool-calls':'stop',raw:'STOP'},warnings:[],usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}}};}})});
 const bearer='synthetic-google-stage-bearer-0000000000';
 const app=await startApplication({directory:join(folder,'app'),mediaRoot:folder,bearerToken:bearer,owner:{channel:'telegram',accountId:'bot',ownerId:'42'},lineKey:'bot',target:'owner',providerThreadId:'telegram:42'},{agent:{generateChat:async()=>({text:''}),generateUnattended:async()=>({text:''}),rotateThread:async()=>{},purgeThreadsBefore:async()=>0},transport:{send:async()=>({acceptance:'accepted',providerMessageIds:['synthetic']})},taskToolDiagnostics:()=>batch});
 try{
  await runtime.generateChat({text:secret,threadId:'synthetic',ownerId:'synthetic',runId:'synthetic-run',ownerRequestKey:'synthetic-request'});
  const expected=[{tool:'gmail_search',status:'unknown',error:'input_validation'},{tool:'gmail_search',status:'unknown',error:'tool_error',google_read_stage:'provider_http_other',google_read_http_status:404,google_read_phase:'search_metadata'},{tool:'gmail_search',status:'unknown',error:'none'}];
  const health=await(await fetch(`http://127.0.0.1:${app.port}/v1/health`,{headers:{authorization:`Bearer ${bearer}`}})).json();
  assert.deepEqual(health.taskToolDiagnostics.events,expected);assert.ok(!JSON.stringify(health).includes(secret));
  const child=spawnSync(process.env.SOTTO_PYTHON??process.env.PYTHON??'python3',['-c',overview,resolve('../..')],{input:JSON.stringify(health),encoding:'utf8',env:{...process.env,SOTTO_DATA:join(folder,'receiver')},timeout:10000});
  assert.equal(child.status,0,child.stderr);const result=JSON.parse(child.stdout);
  assert.deepEqual(result.runtime_health.task_tool_diagnostics.events,expected);assert.ok(!JSON.stringify(result).includes(secret));
  assert.equal(reads,2);assert.equal(calls,4);
  await runtime.generateChat({text:'Synthetic next turn',threadId:'synthetic',ownerId:'synthetic'});assert.deepEqual(batch?.events,[]);
 }finally{await app.close();await runtime.close();rmSync(folder,{recursive:true,force:true});}
});


test('Node Google stage projection rejects injected fields/categories and retains eight-event bound',()=>{
 const event={tool:'gmail_search',status:'unknown',error:'tool_error',google_read_stage:'provider_forbidden'};
 const raw={events:[event],completedAtMs:1};
 assert.deepEqual(projectTaskToolDiagnostics(raw)?.events,[event]);
 for(const extra of [{google_read_stage:'PRIVATE'},{google_read_stage:['provider_forbidden']},{tool:'read_writing_style'},{error:'none'},{private_query:'PRIVATE'}])assert.equal(projectTaskToolDiagnostics({...raw,events:[{...event,...extra}]}),undefined);
 assert.equal(projectTaskToolDiagnostics({...raw,events:Array(8).fill(event)})?.events.length,8);
 assert.equal(projectTaskToolDiagnostics({...raw,events:Array(9).fill(event)}),undefined);
});


test('HTTP and phase health metadata remain content-free, typed and tool-scoped',()=>{
 const event={tool:'gmail_search',status:'unknown',error:'tool_error',google_read_stage:'provider_http_other',google_read_http_status:404,google_read_phase:'search_metadata'};
 const raw={events:[event],completedAtMs:1};
 assert.deepEqual(projectTaskToolDiagnostics(raw)?.events,[event]);
 for(const extra of [{google_read_http_status:99},{google_read_http_status:600},{google_read_http_status:'404'},{google_read_http_status:true},{google_read_http_status:404.5},{google_read_phase:'message_get'},{google_read_phase:'PRIVATE'},{google_read_stage:undefined},{tool:'read_writing_style'}])assert.equal(projectTaskToolDiagnostics({...raw,events:[{...event,...extra}]}),undefined);
});
