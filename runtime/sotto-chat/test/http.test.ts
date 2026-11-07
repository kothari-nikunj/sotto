import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { request as httpRequest } from 'node:http';
import { createChatHttpServer, listenLoopback, type HttpServices } from '../src/http.js';
import { ManagedProxyAdmissionBusy, ManagedProxyRequestTooLarge } from '../src/agent/models.js';
import { createManagedProxyModels } from '../src/agent/models.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';

const token = 'local-fixture-bearer-000000000000000000';

async function fixture(overrides: Partial<HttpServices> = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-http-'));
  const root = join(directory, 'media');
  await mkdir(root);
  const calls: unknown[] = [];
  const services: HttpServices = {
    send: async (body) => { calls.push(body); return { acceptance: 'accepted', message_id: 'text-id' }; },
    sendGallery: async (body) => { calls.push(body); return { acceptance: 'accepted', message_id: 'group-id' }; },
    receipt: async (id) => ({ found: id === 'known', receipt: { acceptance: 'unknown' } }),
    run: async (body) => { calls.push(body); return { text: 'result' }; },
    sessions: async (body) => { calls.push(body); return { ok: true }; },
    health: async () => ({ version: 'test', connected: false }),
    ...overrides,
  };
  const server = createChatHttpServer(services, { bearerToken: token, allowedTargets: new Set(['photon:+15550000000']), mediaRoot: root });
  const port = await listenLoopback(server);
  const url = `http://127.0.0.1:${port}`;
  return {
    directory, root, calls, server, url,
    request: (path: string, body?: unknown, auth = token, method = body === undefined ? 'GET' : 'POST') => fetch(url + path, {
      method,
      headers: { authorization: `Bearer ${auth}`, 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
    close: async () => {
      server.closeAllConnections();
      await new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
      await rm(directory, { recursive: true, force: true });
    },
  };
}

test('authenticated routes expose no framework or query-token surface', async () => {
  const f = await fixture();
  try {
    assert.equal((f.server.address() as { address: string }).address, '127.0.0.1');
    for (const path of ['/v1/health', '/v1/send', '/v1/send-gallery', '/v1/send-gallery-bytes', '/v1/receipt', '/v1/run', '/v1/sessions', '/v1/retention', '/v1/activation-evidence', '/v1/activation-ack']) {
      assert.equal((await f.request(path, undefined, 'bad')).status, 401);
    }
    for (const path of ['/api/agents', '/studio', '/v1/health?apiKey=anything', '/v1/send/']) {
      assert.equal((await f.request(path)).status, 404);
    }
    assert.equal((await f.request('/v1/send')).status, 405);
    assert.equal((await f.request('/v1/health', {})).status, 405);
    assert.deepEqual(await (await f.request('/v1/health')).json(), { version: 'test', connected: false });
    assert.equal(f.calls.length, 0);
  } finally { await f.close(); }
});

test('run reports only verified proxy admission busy as a typed local refusal', async () => {
  const request = { prompt: 'prepare', env: { SOTTO_UNATTENDED: '1', SOTTO_DELIVERY_RUN_ID: 'job-1' } };
  for (const [error, status, code] of [
    [new ManagedProxyAdmissionBusy(), 429, 'model_admission_busy'],
    [new ManagedProxyRequestTooLarge(), 413, 'model_request_too_large'],
    [new Error('unverified 429'), 500, 'operation_failed'],
  ] as const) {
    const f = await fixture({ run: async () => { throw error; } });
    try {
      const response = await f.request('/v1/run', request);
      assert.equal(response.status, status);
      assert.deepEqual(await response.json(), { error: code });
      assert.equal(response.headers.get('X-Sotto-Admission'), status === 429 ? 'busy' : null);
    } finally { await f.close(); }
  }
});

test('real unattended Mastra request carries one proxy busy refusal through local run HTTP', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-run-busy-'));
  let contacts = 0;
  const proxy = createManagedProxyModels({ origin: 'https://proxy.example', allowedOrigin: 'https://proxy.example',
    bearer: 'fixture-secret', chatModelId: 'chat-fixture', nativeMediaModelId: 'native-fixture',
    fetch: async (_input, init) => {
      contacts++;
      assert.equal(new Headers(init?.headers).get('X-Sotto-Workload'), 'unattended');
      return new Response('{"error":"busy"}', { status: 429, headers: { 'X-Sotto-Admission': 'busy' } });
    } });
  const agent = createSottoAgentRuntime({ model: proxy.chatModel,
    storagePath: join(directory, 'memory.db'), instructions: 'Answer briefly.' });
  const f = await fixture({ run: async (input) => agent.generateUnattended({ text: input.prompt,
    runId: input.env.SOTTO_DELIVERY_RUN_ID }) });
  try {
    const response = await f.request('/v1/run', { prompt: 'prepare',
      env: { SOTTO_UNATTENDED: '1', SOTTO_DELIVERY_RUN_ID: 'job-1' } });
    assert.equal(response.status, 429);
    assert.equal(response.headers.get('X-Sotto-Admission'), 'busy');
    assert.deepEqual(await response.json(), { error: 'model_admission_busy' });
    assert.equal(contacts, 1);
  } finally { await f.close(); await agent.close(); await rm(directory, { recursive: true, force: true }); }
});

test('activation API reads bounded journal evidence and acknowledges only a supplied event identity', async () => {
  const evidence = { version: 1 as const, event_id: 'e'.repeat(64), provider_event_id: 'provider-message',
    tenant_id: 'tenant', project_fingerprint: 'a'.repeat(64), owner_id: '+15550000000',
    account_id: 'account', line_key: 'line', provider_thread_id: 'imessage:fixture-chat', admitted_at: 1 };
  let reads = 0; const acknowledgements: string[] = [];
  const f = await fixture({ activationEvidence: async () => { reads++; return evidence; },
    activationAck: async eventId => { acknowledgements.push(eventId); return eventId === evidence.event_id; } });
  try {
    assert.equal((await f.request('/v1/activation-evidence')).status, 405);
    assert.equal((await f.request('/v1/activation-evidence', { owner_id: 'injected' })).status, 400);
    assert.equal((await f.request('/v1/activation-evidence', { padding: 'x'.repeat(1024) })).status, 413);
    assert.equal(reads, 0);
    assert.deepEqual(await (await f.request('/v1/activation-evidence', {})).json(), { version: 1, pending: evidence });
    assert.equal(reads, 1);
    assert.equal((await f.request('/v1/activation-ack', { event_id: evidence.event_id, activated: true })).status, 400);
    assert.equal((await f.request('/v1/activation-ack', { event_id: 'not-an-event' })).status, 400);
    assert.equal((await f.request('/v1/activation-ack', { event_id: evidence.event_id }, 'wrong')).status, 401);
    assert.deepEqual(acknowledgements, []);
    assert.deepEqual(await (await f.request('/v1/activation-ack', { event_id: 'f'.repeat(64) })).json(), { acknowledged: false });
    assert.deepEqual(await (await f.request('/v1/activation-ack', { event_id: evidence.event_id })).json(), { acknowledged: true });
    assert.equal(f.calls.length, 0, 'neither route contacts a provider or model');
  } finally { await f.close(); }
});

test('retention requires a handler and finite ordered UTC cutoffs before mutation', async () => {
  const unavailable = await fixture();
  try {
    assert.equal((await unavailable.request('/v1/retention', {
      delivery_before: '2026-01-01T00:00:00Z', action_before: '2025-01-01T00:00:00Z',
    })).status, 501);
  } finally { await unavailable.close(); }
  const calls: unknown[] = [];
  const f = await fixture({ retention: async request => { calls.push(request); return { pruned: 1 }; } });
  try {
    const good = { delivery_before: '2026-01-01T00:00:00Z', action_before: '2025-01-01T00:00:00Z' };
    for (const invalid of [
      { ...good, action_before: '2026-02-01T00:00:00Z' },
      { ...good, action_before: '2025-01-01T00:00:00+00:00' },
      { ...good, delivery_before: '9999-01-01T00:00:00Z' },
      { ...good, extra: true },
    ]) assert.equal((await f.request('/v1/retention', invalid)).status, 400);
    assert.deepEqual(calls, []);
    assert.deepEqual(await (await f.request('/v1/retention', good)).json(), { pruned: 1 });
    assert.deepEqual(calls, [good]);
  } finally { await f.close(); }
});

test('outbound destinations and input schemas are enforced before provider contact', async () => {
  const f = await fixture();
  try {
    const valid = { dispatch_id: 'dispatch:1', target: 'photon:+15550000000', text: 'hello' };
    assert.equal((await f.request('/v1/send', { ...valid, target: 'photon:+15551111111' })).status, 403);
    assert.equal((await f.request('/v1/send', { ...valid, env: { SOTTO_CHAT_SEND: 'secret' } })).status, 400);
    assert.equal((await f.request('/v1/send', { ...valid, attempt: -1 })).status, 400);
    assert.equal(f.calls.length, 0);
    assert.equal((await f.request('/v1/send', valid)).status, 200);
    assert.deepEqual(f.calls, [{ ...valid, attempt: 1 }]);
  } finally { await f.close(); }
});

test('run endpoint accepts only unattended context and never arbitrary process environment', async () => {
  const f = await fixture();
  try {
    const body = { prompt: 'summarize', env: { SOTTO_UNATTENDED: '1', SOTTO_DELIVERY_RUN_ID: 'run-1' } };
    assert.equal((await f.request('/v1/run', { ...body, env: { ...body.env, BRIDGE_TOKEN: 'injected' } })).status, 400);
    assert.equal((await f.request('/v1/run', { ...body, env: { ...body.env, SOTTO_UNATTENDED: '0' } })).status, 400);
    assert.equal((await f.request('/v1/run', { prompt: 'no mode' })).status, 400);
    assert.equal(f.calls.length, 0);
    assert.deepEqual(await (await f.request('/v1/run', body)).json(), { text: 'result' });
    assert.equal((await f.request('/v1/run', {...body, answer_only: true})).status, 200);
    assert.equal((await f.request('/v1/run', {...body, answer_only: 'yes'})).status, 400);
    assert.deepEqual(f.calls, [body, {...body, answer_only: true}]);
  } finally { await f.close(); }
});

test('gallery files cannot escape the approved root using traversal or symlinks', async () => {
  const f = await fixture();
  try {
    const paths = await Promise.all([0, 1, 2, 3].map(async (i) => {
      const path = join(f.root, `${i}.png`);
      await writeFile(path, Buffer.from([137, 80, 78, 71]));
      return path;
    }));
    const body = { dispatch_id: 'gallery-1', target: 'photon:+15550000000', summary: 'brief', paths };
    const outside = join(f.directory, 'outside.png');
    await writeFile(outside, 'private fixture');
    await symlink(outside, join(f.root, 'link.png'));
    assert.equal((await f.request('/v1/send-gallery', { ...body, paths: [outside, ...paths.slice(1)] })).status, 400);
    assert.equal((await f.request('/v1/send-gallery', { ...body, paths: [join(f.root, 'link.png'), ...paths.slice(1)] })).status, 400);
    assert.equal(f.calls.length, 0);
    assert.equal((await f.request('/v1/send-gallery', body)).status, 200);
    assert.equal(f.calls.length, 1);
  } finally { await f.close(); }
});

test('gallery bytes require authentication, four canonical ordered PNGs, and owner target', async () => {
  const observed: unknown[] = [];
  const f = await fixture({ sendGalleryBytes: async request => {
    observed.push(request);
    return { acceptance: 'accepted', message_id: 'group-id' };
  } });
  const png = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10, 1]);
  const body = { dispatch_id: 'gallery-bytes-1', target: 'photon:+15550000000', summary: 'Résumé 📨 / 東京',
    images: [1, 2, 3, 4].map(index => ({ name: `0${index}.png`, base64: png.toString('base64') })) };
  try {
    assert.equal((await f.request('/v1/send-gallery-bytes', body, 'bad')).status, 401);
    assert.equal((await f.request('/v1/send-gallery-bytes', { ...body, target: 'other' })).status, 403);
    assert.equal((await f.request('/v1/send-gallery-bytes', { ...body, images: [body.images[1], ...body.images.slice(1)] })).status, 400);
    assert.equal((await f.request('/v1/send-gallery-bytes', { ...body,
      images: [{ ...body.images[0], base64: `${body.images[0].base64}=` }, ...body.images.slice(1)] })).status, 400);
    assert.equal((await f.request('/v1/send-gallery-bytes', { ...body,
      images: [{ ...body.images[0], base64: Buffer.from('not a png').toString('base64') }, ...body.images.slice(1)] })).status, 400);
    assert.equal(observed.length, 0);
    assert.equal((await f.request('/v1/send-gallery-bytes', body)).status, 200);
    assert.deepEqual(observed, [{ dispatch_id: body.dispatch_id, target: body.target,
      summary: body.summary, images: body.images.map(image => ({ name: image.name, bytes: png })) }]);
  } finally { await f.close(); }
});

test('gallery byte route accepts near-limit PNGs and rejects declared oversize before service', async () => {
  let calls = 0;
  const f = await fixture({ sendGalleryBytes: async () => { calls++; return { acceptance: 'accepted', message_id: 'group-1' }; } });
  const image = Buffer.alloc(4_900_000, 1);
  Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]).copy(image);
  const body = { dispatch_id: 'large-gallery', target: 'photon:+15550000000', summary: 'brief',
    images: [1, 2, 3, 4].map(index => ({ name: `0${index}.png`, base64: image.toString('base64') })) };
  try {
    assert.equal((await f.request('/v1/send-gallery-bytes', body)).status, 200);
    assert.equal(calls, 1);
    const status = await new Promise<number>((resolve, reject) => {
      const request = httpRequest(`${f.url}/v1/send-gallery-bytes`, { method: 'POST', headers: {
        authorization: `Bearer ${token}`, 'content-type': 'application/json',
        'content-length': '27000001',
      } }, response => { response.resume(); response.on('end', () => resolve(response.statusCode ?? 0)); });
      request.on('error', reject);
      request.end();
    });
    assert.equal(status, 413);
    assert.equal(calls, 1);
  } finally { await f.close(); }
});

test('receipt lookup is read-only and exceptions do not echo credentials', async () => {
  const f = await fixture({ sessions: async () => { throw new Error(`provider secret ${token}`); } });
  try {
    assert.deepEqual(await (await f.request('/v1/receipt', { dispatch_id: 'known' })).json(), { found: true, receipt: { acceptance: 'unknown' } });
    assert.equal(f.calls.length, 0);
    const response = await f.request('/v1/sessions', { action: 'rotate' });
    assert.equal(response.status, 500);
    assert.deepEqual(await response.json(), { error: 'operation_failed' });
    assert.equal((await f.request('/v1/sessions', { action: 'purge_before', before: 'not-a-date' })).status, 400);
  } finally { await f.close(); }
});

test('body limits reject oversized requests before executing a tool', async () => {
  const f = await fixture();
  try {
    const response = await f.request('/v1/run', { prompt: 'x'.repeat(1_048_577) });
    assert.equal(response.status, 413);
    assert.equal(f.calls.length, 0);
  } finally { await f.close(); }
});


test('unattended failure diagnostic exposes only fixed category and location', async () => {
  const logs: unknown[][] = [];
  const previous = console.error;
  console.error = (...args) => { logs.push(args); };
  const f = await fixture({run: async () => { throw new Error('private-token-and-prompt'); }});
  try {
    const response = await f.request('/v1/run', {prompt: 'private supplied evidence', answer_only: true,
      env: {SOTTO_UNATTENDED: '1', SOTTO_DELIVERY_RUN_ID: 'diagnostic-run'}});
    assert.equal(response.status, 500);
    assert.deepEqual(logs, [['[sotto-run-failure]', JSON.stringify({location: 'http.run', category: 'operation_failed'})]]);
    assert.equal(JSON.stringify(logs).includes('private'), false);
  } finally { await f.close(); console.error = previous; }
});
