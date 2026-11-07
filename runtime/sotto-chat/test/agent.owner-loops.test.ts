import assert from 'node:assert/strict';
import test from 'node:test';
import { createOwnerLoopsTool, readOwnerLoops } from '../src/agent/owner-loops.js';

const url = 'http://127.0.0.1:8793/mcp';
const row = { name: 'Alex', what: 'Send the plan', channel: 'manual', identifier: '',
  thread_id: '', group_id: '', action_type: 'follow_up', age_days: 2,
  deadline: null, overdue: false, anchor_key: 'manual:key', chased_count: 0,
  chase_pending: null, last_chased_at: null, chase_after: null,
  chased_out: false, handoff_asked_at: null };
const result = { coverage: 'owner_authored_only', you_owe: [row], waiting_on_them: [],
  counts: { you_owe: 1, waiting_on_them: 0 } };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status,
  headers: { 'content-type': 'application/json' } });

test('client sends one authenticated exact-loopback request and accepts only owner-only coverage', async () => {
  let calls = 0;
  const fetcher: typeof fetch = async (input, init) => {
    calls++;
    assert.equal(String(input), 'http://127.0.0.1:8793/runtime/owner-loops');
    assert.equal(init?.method, 'POST');
    assert.equal(init?.redirect, 'error');
    assert.equal(init?.cache, 'no-store');
    assert.equal((init?.headers as Record<string, string>).authorization, 'Bearer read-only');
    assert.deepEqual(JSON.parse(String(init?.body)), { include_parked: false });
    return response(result);
  };
  assert.deepEqual(await readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: fetcher },
    { include_parked: false }), result);
  assert.equal(calls, 1);
  const parked = { ...result, parked: [row], counts: { ...result.counts, parked: 1 } };
  assert.deepEqual(await readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => response(parked) },
    { include_parked: true }), parked);
});

test('client rejects foreign endpoints, invalid bearer, redirects, and non-200 status', async () => {
  for (const bridgeUrl of ['https://127.0.0.1:8793/mcp', 'http://example.test/mcp',
    'http://127.0.0.1:8793/other', 'http://127.0.0.1:8793/mcp?x=1']) {
    await assert.rejects(readOwnerLoops({ bridgeUrl, readBearer: 'read-only', fetch: async () => response(result) },
      { include_parked: false }), /configuration/);
  }
  await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'bad\r\ntoken' },
    { include_parked: false }), /configuration/);
  for (const status of [201, 301, 401, 403, 503]) {
    await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => response(result, status) },
      { include_parked: false }), /unavailable/);
  }
});

test('strict schema rejects source-backed label, unknown fields, bad counts, and parked mismatch', async () => {
  const invalid = [
    { ...result, coverage: 'all_sources' },
    { ...result, extra: 'not allowed' },
    { ...result, counts: { you_owe: 0, waiting_on_them: 0 } },
    { ...result, parked: [] },
    { ...result, you_owe: [{ ...row, unknown: 'field' }] },
    { ...result, you_owe: [{ ...row, age_days: -1 }] },
  ];
  for (const body of invalid) {
    await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => response(body) },
      { include_parked: false }), /unavailable/);
  }
  await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => response(result) },
    { include_parked: true }), /unavailable/);
});

test('Python code-point truncation accepts 200 emoji but rejects 201', async () => {
  const valid = { ...result, you_owe: [{ ...row, what: '😀'.repeat(200) }] };
  assert.equal((await readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => response(valid) },
    { include_parked: false })).you_owe[0]?.what, '😀'.repeat(200));
  const invalid = { ...result, you_owe: [{ ...row, what: '😀'.repeat(201) }] };
  await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => response(invalid) },
    { include_parked: false }), /unavailable/);
});

test('full streamed body is bounded even with absent or false content-length', async () => {
  const oversized = new ReadableStream<Uint8Array>({ start(controller) {
    controller.enqueue(new Uint8Array(64 * 1024));
    controller.enqueue(new Uint8Array(1));
    controller.close();
  } });
  await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => new Response(oversized) },
    { include_parked: false }), /unavailable/);
  await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () =>
    new Response(JSON.stringify(result), { headers: { 'content-length': '65537' } }) },
  { include_parked: false }), /unavailable/);
});

test('caller abort and deadline bound a body stream that never finishes', async () => {
  const controller = new AbortController();
  let cancelled = 0;
  let entered!: () => void;
  const reading = new Promise<void>(resolve => { entered = resolve; });
  const hanging = () => new Response(new ReadableStream<Uint8Array>({
    start(stream) { stream.enqueue(new TextEncoder().encode('{')); },
    pull() { entered(); },
    cancel() { cancelled++; },
  }));
  const request = readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => hanging() },
    { include_parked: false, signal: controller.signal });
  await reading;
  controller.abort();
  await assert.rejects(request, /unavailable/);
  assert.equal(cancelled, 1);
  // A fake fetch that ignores AbortSignal is still fenced at the client.
  // AbortSignal.timeout is unref'ed. This fake fetch has no socket to keep the
  // isolated test process alive, so retain a bounded fixture handle until it settles.
  const hold = setTimeout(() => undefined, 20_000);
  try {
    const waiting = readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () =>
      new Promise<Response>(() => {}) }, { include_parked: false, signal: AbortSignal.timeout(5) });
    await assert.rejects(waiting, /unavailable/);
  } finally { clearTimeout(hold); }
});

test('pre-aborted request starts no fetch and later rejected fetch is observed', async () => {
  const already = new AbortController();
  already.abort();
  let calls = 0;
  await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => {
    calls++; return response(result);
  } }, { include_parked: false, signal: already.signal }), /unavailable/);
  assert.equal(calls, 0);

  const controller = new AbortController();
  let rejectFetch!: (error: Error) => void;
  let entered!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  const pending = readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => {
    entered();
    return new Promise<Response>((_resolve, reject) => { rejectFetch = reject; });
  } }, { include_parked: false, signal: controller.signal });
  await started;
  controller.abort();
  await assert.rejects(pending, /unavailable/);
  rejectFetch(new Error('late private URL'));
  await new Promise(resolve => setImmediate(resolve));
});

test('late fetch response and rejected status/header bodies are cancelled', async () => {
  const controller = new AbortController();
  let resolveFetch!: (response: Response) => void;
  let entered!: () => void;
  const started = new Promise<void>(resolve => { entered = resolve; });
  const pending = readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => {
    entered();
    return new Promise<Response>(resolve => { resolveFetch = resolve; });
  } }, { include_parked: false, signal: controller.signal });
  await started;
  controller.abort();
  await assert.rejects(pending, /unavailable/);
  let lateCancelled = 0;
  resolveFetch(new Response(new ReadableStream({ cancel() { lateCancelled++; } })));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(lateCancelled, 1);

  for (const kind of ['status', 'header']) {
    let cancelled = 0;
    const body = new ReadableStream({ cancel() { cancelled++; } });
    const fetcher: typeof fetch = async () => new Response(body, kind === 'status'
      ? { status: 503 } : { headers: { 'content-length': '65537' } });
    await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: fetcher },
      { include_parked: false }), /unavailable/);
    assert.equal(cancelled, 1);
  }
});

test('internal five-second deadline bounds a hung response body', { timeout: 20_000 }, async () => {
  let cancelled = 0;
  const hold = setTimeout(() => undefined, 20_000); // keep the process alive for AbortSignal.timeout
  try {
    const started = Date.now();
    const fetcher: typeof fetch = async () => new Response(new ReadableStream({ cancel() { cancelled++; } }));
    await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only', fetch: fetcher },
      { include_parked: false }), /unavailable/);
    assert.ok(Date.now() - started >= 4_500 && Date.now() - started < 15_000);
    assert.equal(cancelled, 1);
  } finally { clearTimeout(hold); }
});

test('chat-only tool requires coordinator run authority and labels partial coverage', async () => {
  const tool = createOwnerLoopsTool({ bridgeUrl: url, readBearer: 'read-only', fetch: async () => response(result) });
  const execute = tool.execute!;
  const context = (mode: string, runId?: string, ownerRequestKey?: string) => ({
    requestContext: { get: (key: string) => ({ mode, runId, ownerRequestKey })[key as 'mode'] },
  });
  await assert.rejects(execute({ include_parked: false }, context('unattended', 'r', 'e') as never), /unavailable/);
  await assert.rejects(execute({ include_parked: false }, context('chat') as never), /unavailable/);
  const output = await execute({ include_parked: false }, context('chat', 'r', 'e') as never) as {
    coverage: string; coverage_note: string;
  };
  assert.equal(output.coverage, 'owner_authored_only');
  assert.match(output.coverage_note, /Empty lists do not prove/);
});


test('invalid UTF-8 cannot silently alter returned owner text', async () => {
  const serialized = JSON.stringify(result);
  const position = serialized.indexOf('Send the plan');
  const raw = Buffer.concat([Buffer.from(serialized.slice(0, position)), Buffer.from([0xff]),
    Buffer.from(serialized.slice(position + 1))]);
  await assert.rejects(readOwnerLoops({ bridgeUrl: url, readBearer: 'read-only',
    fetch: async () => new Response(raw) }, { include_parked: false }), /unavailable/);
});

test('actual unattended Mastra reads owner loops without invented owner input, and changed source authority withholds its result',async()=>{
 const {mkdtempSync,rmSync}=await import('node:fs');const {tmpdir}=await import('node:os');const {join}=await import('node:path');
 const {MockLanguageModelV4}=await import('ai/test');const {createSottoAgentRuntime}=await import('../src/agent/runtime.js');
 const folder=mkdtempSync(join(tmpdir(),'sotto-unattended-loops-'));let step=0,contacts=0,revision='a'.repeat(64),revoke=false;
 const loops=createOwnerLoopsTool({bridgeUrl:url,readBearer:'read-only',fetch:async(_url,init)=>{contacts++;assert.equal(new Headers(init?.headers).get('authorization'),'Bearer read-only');if(revoke)revision='b'.repeat(64);return response(result);}},'unattended');
 const model=new MockLanguageModelV4({doGenerate:async options=>{
  step++;
  if(step%2===1)return {content:[{type:'tool-call' as const,toolCallId:`read-${step}`,toolName:'owner_open_loops',input:'{"include_parked":false}'}],finishReason:{unified:'tool-calls' as const,raw:'STOP'},usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]};
  assert.ok(JSON.stringify(options.prompt).includes('Send the plan'));
  return {content:[{type:'text' as const,text:'Alex — Send the plan'}],finishReason:{unified:'stop' as const,raw:'STOP'},usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]};
 }});
 const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Read owner loops',sourceAuthority:{read:async()=>({version:2,complete:true,revision,bridge_sources:[],google_sources:[]})},unattendedTools:async()=>({owner_open_loops:loops})});
 try{
  const output=await runtime.generateUnattended({runId:'routine-occurrence',text:'Summarize my owner loops'});assert.equal(output.text,'Alex — Send the plan');assert.equal(contacts,1);
  revoke=true;await assert.rejects(runtime.generateUnattended({runId:'revoked-occurrence',text:'Summarize owner loops'}),/source authority|source permissions|Source/i);assert.equal(contacts,2);
  await assert.rejects(loops.execute!({include_parked:false},{requestContext:{get:()=>undefined}} as never),/unavailable/);
 }finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});
