import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {createBridgeMcpClients} from '../src/agent/bridge.js';
import {SourceAuthorityChanged, type SourceAuthority} from '../src/agent/source-authority.js';

const usage = {inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},
  outputTokens:{total:2,text:2,reasoning:0}};
const answer = () => ({content:[{type:'text' as const,text:'Fixture answer.'}],
  finishReason:{unified:'stop' as const,raw:'STOP'},usage,warnings:[]});
const health = {name:'health',description:'Read health',inputSchema:{type:'object',properties:{}}};

for (const mode of ['chat','unattended'] as const) {
  test(`${mode} resolves the Bridge catalog once per turn and refreshes it on the next turn`, async () => {
    const directory = await mkdtemp(join(tmpdir(),'sotto-tool-catalog-'));
    const calls:string[] = [];
    const modelTools:string[][] = [];
    let includeExtra = false;
    let step = 0;
    const bridge = createBridgeMcpClients({url:'http://127.0.0.1:43210/mcp',readBearer:'fixture-read',
      fetch:async input => {
        const request = await new Request(input).json() as {id:number;method:string};
        calls.push(request.method);
        const result = request.method === 'initialize' ? {protocolVersion:'2025-06-18'}
          : request.method === 'tools/list' ? {tools:[health,
            ...(includeExtra ? [{...health,name:'diagnostic'}] : [])]}
          : {content:[{type:'text',text:'Fixture health.'}]};
        return Response.json({jsonrpc:'2.0',id:request.id,result});
      }});
    const runtime = createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer briefly.',
      chatTools:authority => bridge.chatTools(authority),
      unattendedTools:authority => bridge.unattendedTools(authority),
      model:new MockLanguageModelV4({doGenerate:async request => {
        modelTools.push((request.tools ?? []).map(tool => tool.name).sort());
        step++;
        return step % 2 === 1 ? {content:[{type:'tool-call',toolCallId:`call-${step}`,
          toolName:'bridge_health',input:'{}'}],finishReason:{unified:'tool-calls',raw:'TOOL_CALLS'},usage,warnings:[]}
          : answer();
      }})});
    const run = () => mode === 'chat'
      ? runtime.generateChat({text:'Check health.',threadId:'t',ownerId:'o',runId:'same-run-id'})
      : runtime.generateUnattended({text:'Check health.',runId:'same-run-id'});
    try {
      assert.equal((await run()).text,'Fixture answer.');
      assert.deepEqual(calls,['initialize','tools/list','tools/call']);
      includeExtra = true;
      assert.equal((await run()).text,'Fixture answer.');
      assert.deepEqual(calls,['initialize','tools/list','tools/call','initialize','tools/list','tools/call']);
      assert.deepEqual(modelTools,[['bridge_health'],['bridge_health'],
        ['bridge_diagnostic','bridge_health'],['bridge_diagnostic','bridge_health']]);
    } finally { await runtime.close(); await bridge.close(); await rm(directory,{recursive:true,force:true}); }
  });

  test(`${mode} does not retry failed discovery within a turn or retain its failure for the next turn`, async () => {
    const directory = await mkdtemp(join(tmpdir(),'sotto-tool-discovery-failure-'));
    let resolutions = 0;
    let modelCalls = 0;
    const resolve = async () => { if (++resolutions === 1) throw new Error('fixture discovery unavailable'); return {}; };
    const runtime = createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer briefly.',
      chatTools:resolve,unattendedTools:resolve,
      model:new MockLanguageModelV4({doGenerate:async () => { modelCalls++; return answer(); }})});
    const run = () => mode === 'chat'
      ? runtime.generateChat({text:'Hello.',threadId:'t',ownerId:'o',runId:'same-run-id'})
      : runtime.generateUnattended({text:'Hello.',runId:'same-run-id'});
    try {
      await assert.rejects(run());
      assert.equal(resolutions,1);
      assert.equal(modelCalls,0);
      assert.equal((await run()).text,'Fixture answer.');
      assert.equal(resolutions,2);
      assert.equal(modelCalls,1);
    } finally { await runtime.close(); await rm(directory,{recursive:true,force:true}); }
  });
}

test('sharing discovery within a turn still rechecks source authority before executing a Bridge tool', async () => {
  const directory = await mkdtemp(join(tmpdir(),'sotto-tool-authority-'));
  let authority:SourceAuthority = {version:2,complete:true,revision:'a'.repeat(64),
    bridge_sources:['imessage'],google_sources:[],connector_sources:[]};
  const reader = {read:async () => authority};
  let discoveries = 0;
  let providerCalls = 0;
  let modelCalls = 0;
  const bridge = createBridgeMcpClients({url:'http://127.0.0.1:43210/mcp',readBearer:'fixture-read',sourceAuthority:reader,
    fetch:async input => {
      const request = await new Request(input).json() as {id:number;method:string};
      if (request.method === 'tools/list') discoveries++;
      if (request.method === 'tools/call') providerCalls++;
      return Response.json({jsonrpc:'2.0',id:request.id,result:request.method === 'initialize'
        ? {protocolVersion:'2025-06-18'} : {tools:[{...health,name:'get_messages'}]}});
    }});
  const runtime = createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer briefly.',
    sourceAuthority:reader,chatTools:context => bridge.chatTools(context),
    model:new MockLanguageModelV4({doGenerate:async () => {
      if (++modelCalls > 1) return answer();
      authority = {...authority,revision:'b'.repeat(64),bridge_sources:[]};
      return {content:[{type:'tool-call',toolCallId:'revoked-call',toolName:'bridge_get_messages',input:'{}'}],
        finishReason:{unified:'tool-calls',raw:'TOOL_CALLS'},usage,warnings:[]};
    }})});
  try {
    await assert.rejects(runtime.generateChat({text:'Read messages.',threadId:'t',ownerId:'o'}),SourceAuthorityChanged);
    assert.equal(discoveries,1);
    assert.equal(providerCalls,0,'revocation after discovery must prevent source contact');
  } finally { await runtime.close(); await bridge.close(); await rm(directory,{recursive:true,force:true}); }
});

test('answer-only unattended turns do not resolve either tool catalog', async () => {
  const directory = await mkdtemp(join(tmpdir(),'sotto-answer-only-catalog-'));
  let resolutions = 0;
  const resolve = () => { resolutions++; return {}; };
  const runtime = createSottoAgentRuntime({storagePath:join(directory,'memory.db'),instructions:'Answer briefly.',
    chatTools:resolve,unattendedTools:resolve,model:new MockLanguageModelV4({doGenerate:async request => {
      assert.equal(request.tools?.length ?? 0,0);
      return answer();
    }})});
  try {
    await runtime.generateUnattended({text:'Just answer.',runId:'answer-only',answerOnly:true});
    assert.equal(resolutions,0);
  } finally { await runtime.close(); await rm(directory,{recursive:true,force:true}); }
});
