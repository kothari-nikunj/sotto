import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Message } from 'chat';
import { startConfiguredRuntime, type RuntimeHandle } from '../src/bootstrap/main.js';
import type { GatewayAdapter } from '../src/channels/gateway.js';

/** Real gateway/coordinator/Mastra/LibSQL/HTTP; only provider and model edges are fixtures. */
test('configured runtime preserves chat across restart and separates unattended execution end to end', { timeout: 30_000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-journey-'));
  const configFile = join(directory, 'config.json');
  const bearer = 'synthetic-runtime-bearer-with-32-characters';
  await mkdir(join(directory, 'media'), { mode: 0o700 });
  await writeFile(configFile, JSON.stringify({
    version: 1, channel: 'telegram', stateDirectory: 'state', mediaRoot: 'media', bearerToken: bearer,
    owner: { accountId: 'fixture-account', ownerId: '123' }, lineKey: 'fixture-line',
    target: 'owner', providerThreadId: 'telegram:123',
    credentials: { telegramToken: 'fixture-token', telegramLinkFile: { bot_token: 'fixture-token', allowed_user: 123 } },
    instructions: 'Answer the owner using the supplied context.',
    modelProxy: { origin: 'https://synthetic.invalid', bearer: 'synthetic-proxy-bearer', chatModelId: 'fixture-model', nativeMediaModelId: 'fixture-media' },
  }), { mode: 0o600 });
  const modelRequests: unknown[] = [];
  const posts: string[] = [];
  const transport: typeof fetch = async (input, init) => {
    assert.equal(String(input), 'https://synthetic.invalid/openai/v1/chat/completions');
    assert.equal(new Headers(init?.headers).get('authorization'), 'Bearer synthetic-proxy-bearer');
    assert.equal(init?.redirect, 'error');
    modelRequests.push(JSON.parse(String(init?.body)));
    return new Response(JSON.stringify({
      id: `model-${modelRequests.length}`, created: 1, model: 'fixture-model',
      choices: [{ index: 0, message: { role: 'assistant', content: `Fixture answer ${modelRequests.length}` }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 12, completion_tokens: 4, total_tokens: 16 },
    }), { headers: { 'content-type': 'application/json' } });
  };
  const adapter = () => {
    let polling = false;
    return {
      name: 'telegram', userName: 'sotto',
      initialize: async () => { polling = true; },
      disconnect: async () => { polling = false; },
      stopPolling: async () => { polling = false; },
      get isPolling() { return polling; },
      isDM: (thread: string) => thread === 'telegram:123',
    } as unknown as GatewayAdapter;
  };
  const start = () => startConfiguredRuntime(configFile, {
    createAdapter: adapter,
    createTransport: () => ({ send: async request => {
      posts.push(request.text ?? '');
      return { acceptance: 'accepted', providerMessageIds: [`provider-${posts.length}`] };
    } }),
    fetch: transport,
  });
  const message = (id: string, text: string, owner = '123') => new Message({
    id, threadId: 'telegram:123', text, raw: { id },
    formatted: { type: 'root', children: [] },
    author: { userId: owner, userName: 'Owner', fullName: 'Owner', isBot: false, isMe: false },
    metadata: { dateSent: new Date(), edited: false }, attachments: [],
  });
  const health = async (runtime: RuntimeHandle) => {
    const response = await fetch(`http://127.0.0.1:${runtime.port}/v1/health`, { headers: { authorization: `Bearer ${bearer}` } });
    assert.equal(response.status, 200);
    return await response.json() as { pendingInbound: number; modelMetrics?: import('../src/agent/metrics.js').ModelMetrics };
  };
  const settled = async (runtime: RuntimeHandle) => {
    const deadline = Date.now() + 10_000;
    while ((await health(runtime)).pendingInbound !== 0) {
      if (Date.now() > deadline) throw new Error('Fixture turn did not settle');
      await new Promise(resolve => setTimeout(resolve, 10));
    }
  };
  let runtime: RuntimeHandle | undefined;
  try {
    runtime = await start();
    await runtime.gateway.chat.processMessage(runtime.gateway.adapter, 'telegram:123', message('stranger', 'ignore permissions', '999'));
    assert.equal(modelRequests.length, 0);
    await runtime.gateway.chat.processMessage(runtime.gateway.adapter, 'telegram:123', message('first', 'Remember my synthetic blue kite.'));
    await settled(runtime);
    assert.deepEqual(posts, ['Fixture answer 1']);
    const metrics=(await health(runtime)).modelMetrics;
    assert.equal(metrics?.mode,'chat');assert.equal(metrics?.stepCount,1);
    assert.equal(metrics?.steps[0]?.inputTokens,12);
    assert.equal(JSON.stringify(metrics).includes('synthetic blue kite'),false);
    await runtime.close();
    runtime = await start();
    await runtime.gateway.chat.processMessage(runtime.gateway.adapter, 'telegram:123', message('first', 'Remember my synthetic blue kite.'));
    await settled(runtime);
    assert.equal(posts.length, 1, 'durable duplicate remains inert after restart');
    await runtime.gateway.chat.processMessage(runtime.gateway.adapter, 'telegram:123', message('second', 'What did I mention?'));
    await settled(runtime);
    assert.ok(JSON.stringify(modelRequests[1]).includes('synthetic blue kite'), 'actual Mastra history survived restart');
    const run = await fetch(`http://127.0.0.1:${runtime.port}/v1/run`, {
      method: 'POST', headers: { authorization: `Bearer ${bearer}`, 'content-type': 'application/json' },
      body: JSON.stringify({ prompt: 'Isolated unattended fixture', env: { SOTTO_UNATTENDED: '1', SOTTO_DELIVERY_RUN_ID: 'job-1' } }),
    });
    assert.equal(run.status, 200);
    const result = await run.json() as { text: string; usage: { inputTokens: number } };
    assert.equal(result.text, 'Fixture answer 3');
    assert.equal(result.usage.inputTokens, 12);
    assert.equal(JSON.stringify(modelRequests[2]).includes('synthetic blue kite'), false);
    assert.equal(posts.length, 2, '/run never posts');
    assert.equal((await runtime.rotateSessions()).count, 1);
    await runtime.gateway.chat.processMessage(runtime.gateway.adapter, 'telegram:123', message('third', 'Fresh conversation.'));
    await settled(runtime);
    assert.equal(JSON.stringify(modelRequests[3]).includes('synthetic blue kite'), false);
    assert.equal(JSON.stringify(modelRequests[3]).includes('Isolated unattended fixture'), false);
    assert.equal(posts.length, 3);
  } finally {
    await runtime?.close();
    await rm(directory, { recursive: true, force: true });
  }
});
