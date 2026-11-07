import test from 'node:test';
import assert from 'node:assert/strict';
import { generateText } from 'ai';
import { createHash } from 'node:crypto';
import { createManagedProxyModels, ManagedProxyAdmissionBusy, ManagedProxyRequestTooLarge,
  ManagedProxyUpstreamUnavailable, withModelWorkload } from '../src/agent/models.js';

test('managed adapters use only exact proxy routes, Bearer, and tool-free native nonstreaming', async () => {
  const calls: Array<{ url: string; body: Record<string, unknown>; auth: string | null; redirect: RequestRedirect | undefined; headers: Headers }> = [];
  const transport: typeof fetch = async (input, init) => {
    const url = String(input);
    calls.push({ url, body: JSON.parse(String(init?.body)), auth: new Headers(init?.headers).get('authorization'), redirect: init?.redirect,
      headers: new Headers(init?.headers) });
    const payload = url.includes('/openai/')
      ? { id: 'test', created: 1, model: 'chat-fixture', choices: [{ index: 0, message: { role: 'assistant', content: 'chat reply' }, finish_reason: 'stop' }], usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 } }
      : { candidates: [{ content: { role: 'model', parts: [{ text: 'native reply' }] }, finishReason: 'STOP' }], usageMetadata: { promptTokenCount: 3, candidatesTokenCount: 2, totalTokenCount: 5 } };
    return new Response(JSON.stringify(payload), { status: 200, headers: { 'content-type': 'application/json' } });
  };
  const models = createManagedProxyModels({
    origin: 'http://127.0.0.1:43123', allowedOrigin: 'http://127.0.0.1:43123', bearer: 'fixture-secret',
    chatModelId: 'chat-fixture', nativeMediaModelId: 'native-fixture', fetch: transport,
  });
  assert.equal((await generateText({ model: models.chatModel, prompt: 'hello' })).text, 'chat reply');
  assert.equal((await models.generateNativeMedia('describe an image', [
    { bytes: new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), mediaType: 'image/png' },
  ])).text, 'native reply');
  assert.deepEqual(calls.map(call => call.url), [
    'http://127.0.0.1:43123/openai/v1/chat/completions',
    'http://127.0.0.1:43123/native/v1beta/models/native-fixture:generateContent',
  ]);
  for (const call of calls) {
    assert.equal(call.auth, 'Bearer fixture-secret');
    assert.equal(call.redirect, 'error');
    assert.equal(call.headers.get('X-Sotto-Workload'), 'chat');
    assert.match(call.headers.get('X-Sotto-Operation') ?? '', /^[a-f0-9]{64}$/);
    assert.equal(call.headers.get('X-Sotto-Attempt'), '1');
  }
  assert.equal('tools' in calls[1]!.body, false);
  assert.ok(JSON.stringify(calls[1]!.body).includes('inlineData'));
  await assert.rejects(models.generateNativeMedia('oversize', [{ bytes: new Uint8Array(8 * 1024 * 1024 + 1), mediaType: 'image/png' }]));
  await assert.rejects(models.generateNativeMedia('mismatch', [{ bytes: new Uint8Array([71, 73, 70, 56, 57, 97]), mediaType: 'image/png' }]));
  const full = new Uint8Array(8 * 1024 * 1024);
  full.set([137, 80, 78, 71, 13, 10, 26, 10]);
  await assert.rejects(models.generateNativeMedia('aggregate oversize', [full, full, full].map(bytes => ({ bytes, mediaType: 'image/png' }))));
  assert.equal(calls.length, 2, 'invalid media never contacts the model proxy');
  assert.throws(() => createManagedProxyModels({
    origin: 'https://evil.example', allowedOrigin: 'https://managed.example', bearer: 'secret',
    chatModelId: 'chat', nativeMediaModelId: 'native', fetch: transport,
  }));
});

test('model attribution separates unattended work and hashes private chat session', async () => {
  const labels: Headers[] = [];
  const transport: typeof fetch = async (_input, init) => {
    labels.push(new Headers(init?.headers));
    return new Response(JSON.stringify({ id: 'test', created: 1, model: 'chat-fixture',
      choices: [{ index: 0, message: { role: 'assistant', content: 'ok' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }),
    { status: 200, headers: { 'content-type': 'application/json' } });
  };
  const models = createManagedProxyModels({ origin: 'https://proxy.example', allowedOrigin: 'https://proxy.example',
    bearer: 'fixture-secret', chatModelId: 'chat-fixture', nativeMediaModelId: 'native-fixture', fetch: transport });
  await withModelWorkload('chat', 'private-thread-id', () => generateText({ model: models.chatModel, prompt: 'hello' }));
  await withModelWorkload('unattended', undefined, () => generateText({ model: models.chatModel, prompt: 'brief' }));
  assert.equal(labels[0]!.get('X-Sotto-Workload'), 'chat');
  assert.equal(labels[0]!.get('X-Sotto-Session'), createHash('sha256').update('private-thread-id').digest('hex'));
  assert.equal(labels[1]!.get('X-Sotto-Workload'), 'unattended');
  assert.equal(labels[1]!.get('X-Sotto-Session'), null);
  assert.notEqual(labels[0]!.get('X-Sotto-Operation'), labels[1]!.get('X-Sotto-Operation'));
});

test('verified proxy busy and oversized refusals stop at one HTTP attempt', async () => {
  for (const status of [429, 413]) {
    let attempts = 0;
    const transport: typeof fetch = async () => {
      attempts++;
      return new Response(JSON.stringify({ error: 'refused' }), { status,
        headers: status === 429 ? { 'X-Sotto-Admission': 'busy' } : {} });
    };
    const models = createManagedProxyModels({ origin: 'https://proxy.example', allowedOrigin: 'https://proxy.example',
      bearer: 'fixture-secret', chatModelId: 'chat-fixture', nativeMediaModelId: 'native-fixture', fetch: transport });
    await assert.rejects(generateText({ model: models.chatModel, prompt: 'hello' }),
      status === 429 ? ManagedProxyAdmissionBusy : ManagedProxyRequestTooLarge);
    assert.equal(attempts, 1);
  }
});

test('managed chat 502, 503, and 504 become bounded unavailable errors without retry or provider prose', async () => {
  for (const status of [502, 503, 504] as const) {
    let attempts = 0;
    const models = createManagedProxyModels({origin: 'https://proxy.example', allowedOrigin: 'https://proxy.example',
      bearer: 'fixture-secret', chatModelId: 'chat-fixture', nativeMediaModelId: 'native-fixture',
      fetch: async () => { attempts++; return new Response('private provider failure text', {status}); }});
    await assert.rejects(generateText({model: models.chatModel, prompt: 'hello', maxRetries: 0}), error =>
      error instanceof ManagedProxyUpstreamUnavailable && error.status === status &&
      !String(error).includes('private provider failure text'));
    assert.equal(attempts, 1);
  }
});

test('managed 503 propagates through installed Mastra without another proxy call', async () => {
  const {createSottoAgentRuntime} = await import('../src/agent/runtime.js');
  const {mkdtempSync, rmSync} = await import('node:fs');
  const {tmpdir} = await import('node:os');
  const {join} = await import('node:path');
  const folder = mkdtempSync(join(tmpdir(), 'unavailable-model-'));
  let calls = 0;
  const models = createManagedProxyModels({origin: 'https://proxy.example', allowedOrigin: 'https://proxy.example',
    bearer: 'fixture', chatModelId: 'chat', nativeMediaModelId: 'media',
    fetch: async () => { calls++; return new Response('private provider failure text', {status: 503}); }});
  const runtime = createSottoAgentRuntime({storagePath: join(folder, 'memory.db'), instructions: 'Answer normally', model: models.chatModel});
  try {
    await assert.rejects(runtime.generateChat({text: 'hello', threadId: 'thread', ownerId: 'owner'}), ManagedProxyUpstreamUnavailable);
    assert.equal(calls, 1);
  } finally { await runtime.close(); rmSync(folder, {recursive: true, force: true}); }
});

test('exact managed budget refusal propagates through installed Mastra without retry', async () => {
  const {ManagedProxyBudgetExhausted} = await import('../src/agent/models.js');
  const {createSottoAgentRuntime} = await import('../src/agent/runtime.js');
  const {mkdtempSync,rmSync} = await import('node:fs');
  const {tmpdir} = await import('node:os');
  const {join} = await import('node:path');
  const folder=mkdtempSync(join(tmpdir(),'budget-model-'));
  let calls=0;
  const models=createManagedProxyModels({origin:'https://proxy.example',allowedOrigin:'https://proxy.example',bearer:'fixture',
    chatModelId:'chat',nativeMediaModelId:'media',fetch:async()=>{calls++;return new Response(JSON.stringify({error:{code:'sotto_budget_exhausted',type:'budget_exhausted'}}),{status:402});}});
  const runtime=createSottoAgentRuntime({storagePath:join(folder,'memory.db'),instructions:'Answer normally',model:models.chatModel});
  try {await assert.rejects(runtime.generateChat({text:'hello',threadId:'thread',ownerId:'owner'}),ManagedProxyBudgetExhausted);assert.equal(calls,1);}
  finally {await runtime.close();rmSync(folder,{recursive:true,force:true});}
});

test('other HTTP refusals and malformed budget bodies are not allowance notices', async () => {
  const {ManagedProxyBudgetExhausted} = await import('../src/agent/models.js');
  for(const [status,body] of [[403,{error:{code:'sotto_budget_exhausted',type:'budget_exhausted'}}],
    [402,{error:{code:'other',type:'budget_exhausted'}}],[402,{error:{code:'sotto_budget_exhausted',type:'other'}}],
    [402,'not json'],[402,'x'.repeat(4097)]] as const){
    const models=createManagedProxyModels({origin:'https://proxy.example',allowedOrigin:'https://proxy.example',bearer:'fixture',
      chatModelId:'chat',nativeMediaModelId:'media',fetch:async()=>new Response(typeof body==='string'?body:JSON.stringify(body),{status})});
    await assert.rejects(generateText({model:models.chatModel,prompt:'hello',maxRetries:0}),error=>!(error instanceof ManagedProxyBudgetExhausted));
  }
});


test('verified proxy budget period is bounded metadata, never provider prose', async () => {
  const {ManagedProxyBudgetExhausted} = await import('../src/agent/models.js');
  for (const [wire, expected] of [['calendar_month', 'calendar_month'], ['lifetime', 'lifetime'],
    ['arbitrary-provider-text', 'unknown']] as const) {
    const models = createManagedProxyModels({origin:'https://proxy.example', allowedOrigin:'https://proxy.example',
      bearer:'fixture', chatModelId:'chat', nativeMediaModelId:'media', fetch:async()=>new Response(JSON.stringify({
        error:{code:'sotto_budget_exhausted', type:'budget_exhausted', period:wire, message:'untrusted text'}
      }), {status:402})});
    await assert.rejects(generateText({model:models.chatModel, prompt:'hello', maxRetries:0}),
      error=>error instanceof ManagedProxyBudgetExhausted && error.period === expected &&
        !String(error).includes('untrusted text'));
  }
});
