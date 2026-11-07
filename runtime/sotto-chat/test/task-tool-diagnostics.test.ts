import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import {tmpdir} from 'node:os';
import {MockLanguageModelV4} from 'ai/test';
import {createTool} from '@mastra/core/tools';
import {z} from 'zod';
import {CleanupDiagnosticError} from '../src/tools/cleanup-diagnostic.js';
import {ActionHeldError} from '../src/tools/action-gate.js';
import {createOwnerTaskToolClient} from '../src/tools/owner-task-tool-client.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';
import {ownerTaskToolKind,cleanupListHash,type OwnerTaskToolArgs} from '../src/tools/owner-task-tool-contract.js';
import {projectTaskToolDiagnostics} from '../src/agent/tool-diagnostics.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import type {TaskToolDiagnostic} from '../src/agent/tool-diagnostics.js';
const usage={inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}};
const secret='PRIVATE-PROMPT-ARG-RESULT-IDENTIFIER';
for(const tool of ['owner_task_add','owner_task_list'] as const)for(const mode of ['recorded','rejected','private-rejection','validation','failure'] as const)test(`${tool} diagnostics survive Node and Python projection without owner text: ${mode}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'task-add-diagnostics-'));let calls=0;let batch:TaskToolDiagnostic[]=[];
 const add=createTool({id:tool,description:'Fixture',inputSchema:z.object({text:z.string()}),execute:async()=>{
  if(mode==='failure')throw new Error(secret);
  return mode==='rejected'||mode==='private-rejection'?{status:'rejected',code:mode==='rejected'?'owner_span_unavailable':secret,private:secret}:tool==='owner_task_list'?{status:'proposed',review_text:secret,needs_owner_selection:true}:{status:'recorded',anchor_key:secret};
 }});
 const model=new MockLanguageModelV4({doGenerate:async()=>({content:++calls===1?[{type:'tool-call',toolCallId:secret,toolName:tool,input:JSON.stringify(mode==='validation'?{wrong:secret}:{text:secret})}]:[{type:'text',text:'Final answer.'}],finishReason:{unified:calls===1?'tool-calls':'stop',raw:'STOP'},usage,warnings:[]})});
 const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:secret,chatTools:()=>({[tool]:add}),onTaskToolDiagnostics:events=>{batch=[...events];}});
 try{
  await runtime.generateChat({text:secret,threadId:'fixture',ownerId:'fixture'});
  const expected={tool,status:mode==='recorded'?(tool==='owner_task_list'?'proposed':'recorded'):mode==='rejected'||mode==='private-rejection'?'rejected':'unknown',error:mode==='validation'?'input_validation':mode==='failure'?'result_unavailable':'none',...(mode==='rejected'?{rejection:'owner_span_unavailable'}:{})};
  assert.deepEqual(batch,[expected]);assert.equal(calls,2);
  const projected=projectTaskToolDiagnostics({events:batch,completedAtMs:1});assert.deepEqual(projected,{events:[expected],completedAtMs:1});
  assert.ok(!JSON.stringify(projected).includes(secret));
  assert.equal(projectTaskToolDiagnostics({events:[{...expected,private:secret}],completedAtMs:1}),undefined);
  const python=spawnSync(process.env.SOTTO_PYTHON??'python3',['-c','import json,sys;sys.path.insert(0,sys.argv[1]);import runtime_api;print(json.dumps(runtime_api._task_tool_diagnostics_projection(json.load(sys.stdin))))',resolve('../../adapters/sotto')],{input:JSON.stringify(projected),encoding:'utf8',timeout:10000});
  assert.equal(python.status,0,python.stderr);assert.deepEqual(JSON.parse(python.stdout),{events:[expected],completed_at_ms:1});
  await runtime.generateChat({text:'Hello',threadId:'fixture',ownerId:'fixture'});assert.deepEqual(batch,[]);
 }finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});
for(const mode of ['projection','bound','callback-failure','rejection','private-rejection','cleanup-rejection'] as const)test(`task step diagnostics are content-free and turn-preserving: ${mode}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'task-diagnostics-'));let calls=0;const batches:TaskToolDiagnostic[][]=[];
 const inputSchema=z.object({value:z.string()});
 const close=createTool({id:'owner_task_close',description:'Fixture',inputSchema,execute:async()=>(mode==='rejection'||mode==='private-rejection'||mode==='cleanup-rejection')?({status:'rejected',code:mode==='private-rejection'?secret:'target_changed',message:secret}):({status:'needs_selection',message:secret,result:secret,stdout:secret})});
 const many=createTool({id:'owner_task_resolve_many',description:'Fixture',inputSchema,execute:async()=>{throw new Error(secret);}});
 const script=createTool({id:'run_sotto_script',description:'Fixture',inputSchema:z.object({id:z.string(),args:z.array(z.string())}),execute:async()=>({status:secret,stdout:secret})});
 const other=createTool({id:'private_tool',description:'Fixture',inputSchema,execute:async()=>({status:'recorded',secret})});
 const model=new MockLanguageModelV4({doGenerate:async()=>{
  calls++;
  const names=mode==='projection'?['owner_task_close','owner_task_resolve_many','run_sotto_script','run_sotto_script','private_tool','owner_task_close','read_writing_style','bridge_search_contacts']:Array.from({length:12},()=> mode==='cleanup-rejection'?'owner_task_cleanup':'owner_task_close');
  return {content:calls===1?names.map((name,index)=>({type:'tool-call' as const,toolCallId:secret+index,toolName:name,input:JSON.stringify(name==='run_sotto_script'?{id:index===2?'loops-query':secret,args:[secret]}:mode==='projection'&&index===5?{wrong:secret}:{value:secret})})):[{type:'text' as const,text:'Final answer.'}],finishReason:{unified:calls===1?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
 }});
 const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:secret,
  chatTools:()=>({owner_task_cleanup:close,owner_task_close:close,owner_task_resolve_many:many,run_sotto_script:script,private_tool:other,read_writing_style:other,bridge_search_contacts:other}),
  onTaskToolDiagnostics:events=>{batches.push([...events]);if(mode==='callback-failure')throw new Error(secret);}});
 try{
  assert.equal((await runtime.generateChat({text:secret,threadId:'fixture-thread',ownerId:'fixture-owner'})).text,'Final answer.');
  assert.equal(calls,2,'observing a step must not add model calls');assert.equal(batches.length,1);
  assert.ok(!JSON.stringify(batches).includes(secret));
  if(mode==='projection')assert.deepEqual(batches[0],[
   {tool:'owner_task_close',status:'needs_selection',error:'none'},
   {tool:'owner_task_resolve_many',status:'unknown',error:'result_unavailable'},
   {tool:'run_sotto_script',script:'loops-query',status:'unknown',error:'none'},
   {tool:'run_sotto_script',script:'unknown',status:'unknown',error:'none'},
   {tool:'owner_task_close',status:'unknown',error:'input_validation'},
   {tool:'read_writing_style',status:'recorded',error:'none'},
   {tool:'bridge_search_contacts',status:'recorded',error:'none'},
  ]);
  else if(mode==='rejection')assert.deepEqual(batches[0],Array.from({length:8},()=>({tool:'owner_task_close',status:'rejected',error:'none',rejection:'target_changed'})));
  else if(mode==='cleanup-rejection'){
   const expected=Array.from({length:8},()=>({tool:'owner_task_cleanup',status:'rejected',error:'none',rejection:'target_changed'}));
   const projected=projectTaskToolDiagnostics({events:batches[0],completedAtMs:1});
   assert.deepEqual(projected,{events:expected,completedAtMs:1});
   const python=spawnSync(process.env.SOTTO_PYTHON??'python3',['-c',
    'import json,sys;sys.path.insert(0,sys.argv[1]);import runtime_api;print(json.dumps(runtime_api._task_tool_diagnostics_projection(json.load(sys.stdin))))',resolve('../../adapters/sotto')],{input:JSON.stringify(projected),encoding:'utf8',timeout:10000});
   assert.equal(python.status,0,python.stderr);
   assert.deepEqual(JSON.parse(python.stdout),{events:expected,completed_at_ms:1});
  }
  else if(mode==='private-rejection')assert.deepEqual(batches[0],Array.from({length:8},()=>({tool:'owner_task_close',status:'rejected',error:'none'})));
  else assert.equal(batches[0]!.length,8,'one turn has at most eight emitted events');
  assert.equal((await runtime.generateChat({text:'Hello',threadId:'fixture-thread',ownerId:'fixture-owner'})).text,'Final answer.');
  assert.equal(calls,3);assert.equal(batches.length,2,'fresh turn replaces the old diagnostic batch');assert.deepEqual(batches[1],[]);
  const aborted=new AbortController();aborted.abort();await assert.rejects(runtime.generateChat({text:'Hello',threadId:'fixture-thread',ownerId:'fixture-owner',signal:aborted.signal}));
  assert.equal(calls,3);assert.deepEqual(batches[2],[],'failed fresh turn also clears the prior batch');
 }finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});


function cleanupRequest() {
 const revision='b'.repeat(64);
 const row={index:1,anchor_key:'cedar-one',name:'Dana',what:'Return the Cedar deck',direction:'you_owe' as const,age_days:7,times_surfaced:3,surface_count_provenance:'accepted_delivery' as const,overdue:false,deadline:null,chased_count:0,chased_out:false,suggestion:'do it or dismiss' as const,loop_version:revision,loop_identity:revision};
 const list={operation:'cleanup_list' as const,source_revision:revision,rows:[row],total:1,remaining:0,review_hash:''};list.review_hash=cleanupListHash(list);
 const args:OwnerTaskToolArgs={operation:'cleanup',list,items:[{index:1,action:'dismiss',correction:'Dismiss item 1'}],proposal:{event_id:revision,action_id:revision,args_hash:revision,accepted_dispatch_id:'accepted-list',source_revision:revision}};
 const eventId='c'.repeat(64);
 return {actionId:stableActionIdentity(eventId,ownerTaskToolKind,args).actionId,eventId,payloadHash:'d'.repeat(64),text:'Dismiss item 1',admittedAtMs:1,providerThreadId:'thread',args};
}

test('native cleanup transport failures survive held errors and Mastra into strict health projection',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'cleanup-diagnostics-'));let calls=0;const batches:TaskToolDiagnostic[][]=[];
 const client=createOwnerTaskToolClient({bridgeUrl:'http://127.0.0.1:1234/mcp',bearer:'a'.repeat(64),identity:{tenant_id:'tenant',channel:'photon',account_id:'account',owner_id:'owner',line_key:'line',target:'photon',route_mode:'fixed',route_id:'route',project_fingerprint:'b'.repeat(64)},fetch:async()=>new Response(JSON.stringify({cleanup_stage:'receiver_perform',cleanup_category:'unavailable',cleanup_http_status:503}),{status:503})});
 const tool=createTool({id:'owner_task_cleanup',description:'Fixture',inputSchema:z.object({}),execute:async()=>{
  try{await client.call('apply',cleanupRequest());}catch(error){
   assert.ok(error instanceof CleanupDiagnosticError);const held=new ActionHeldError();held.message+=' '+error.message;throw held;
  }
 }});
 const model=new MockLanguageModelV4({doGenerate:async()=>({content:++calls===1?[{type:'tool-call' as const,toolCallId:secret,toolName:'owner_task_cleanup',input:'{}'}]:[{type:'text' as const,text:'Final answer.'}],finishReason:{unified:calls===1?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]})});
 const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:secret,chatTools:()=>({owner_task_cleanup:tool}),onTaskToolDiagnostics:events=>batches.push([...events])});
 try{
  await runtime.generateChat({text:secret,threadId:'fixture',ownerId:'fixture'});
  const batch=projectTaskToolDiagnostics({events:batches[0],completedAtMs:1});
  assert.deepEqual(batch,{events:[{tool:'owner_task_cleanup',status:'unknown',error:'tool_error',cleanup_stage:'receiver_perform',cleanup_category:'unavailable',cleanup_http_status:503}],completedAtMs:1});
  assert.ok(!JSON.stringify(batch).includes(secret));
  assert.equal(projectTaskToolDiagnostics({events:[{...batch!.events[0],private_text:secret}],completedAtMs:1}),undefined);
 }finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});

test('cleanup inspection HTTP503 keeps the generic transport error',async()=>{
 const client=createOwnerTaskToolClient({bridgeUrl:'http://127.0.0.1:1234/mcp',bearer:'a'.repeat(64),identity:{tenant_id:'tenant',channel:'photon',account_id:'account',owner_id:'owner',line_key:'line',target:'photon',route_mode:'fixed',route_id:'route',project_fingerprint:'b'.repeat(64)},fetch:async()=>new Response(JSON.stringify({cleanup_stage:'receiver_perform',cleanup_category:'io',cleanup_http_status:503}),{status:503})});
 await assert.rejects(client.call('inspect',cleanupRequest()),error=>error instanceof Error&&!(error instanceof CleanupDiagnosticError)&&error.message==='Owner tool unavailable');
});
