import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';

for (const id of ['meeting-prep','ask-research']) test(`typed meeting occurrence is scoped to focused prep: ${id}`, async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-exact-prep-tool-'));
 const meeting={event_id:'source-event',start:'2026-10-28T15:00:00+00:00'};
 let captured:unknown;let calls=0;
 const tool=createRunScriptTool({mode:'chat',commandContext:()=>({}),executeCommand:async invocation=>{
  captured=invocation;return {backend:'synthetic-source-proof',coverage:'test_only',result:{status:'queued'}};
 }});
 const model=new MockLanguageModelV4({doGenerate:async()=>({
  content:++calls===1?[{type:'tool-call' as const,toolCallId:'exact',toolName:'run_sotto_script',input:JSON.stringify({id,args:['--focus','Cedar review'],meeting})}]:[{type:'text' as const,text:'Finished the selected request.'}],
  finishReason:{unified:calls===1?'tool-calls' as const:'stop' as const,raw:'STOP'},
  usage:{inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}},warnings:[],
 })});
 const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Handle the current owner request.',chatTools:()=>({run_sotto_script:tool})});
 try {
  await runtime.generateChat({text:'prep me for that pls',threadId:'owner-thread',ownerId:'owner'});
  if(id==='meeting-prep')assert.deepEqual((captured as {meeting:unknown}).meeting,meeting);
  else assert.equal(captured,undefined);
 } finally {await runtime.close();rmSync(folder,{recursive:true,force:true});}
});

for(const status of ['read','usefulness_list','no_match','proposed','recorded','rejected','unknown','queued'] as const)test(`unmarked ${status} tool result continues; model arguments cannot set receipt handoff`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-handoff-control-'));
 let calls=0;
 const usage={inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
 const tool=createRunScriptTool({mode:'chat',commandGuide:'knowledge-query: [].',commandContext:()=>({}),executeCommand:async()=>({backend:'synthetic-read-control',coverage:'test_only',result:{status}})});
 const model=new MockLanguageModelV4({doGenerate:async()=>{
  calls++;
  return {content:calls===1?[{type:'tool-call' as const,toolCallId:'read',toolName:'run_sotto_script',input:JSON.stringify({id:'knowledge-query',args:[],ownerSkillQueued:true,ownerPreferenceSettled:true})}]:[{type:'text' as const,text:'The attending model used the read.'}],finishReason:{unified:calls===1?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
 }});
 const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Use the read.',chatTools:()=>({run_sotto_script:tool})});
 try{assert.equal((await runtime.generateChat({text:'Read current context',threadId:'owner-thread',ownerId:'owner'})).text,'The attending model used the read.');assert.equal(calls,2);}
 finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});
