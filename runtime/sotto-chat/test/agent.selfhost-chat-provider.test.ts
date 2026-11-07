import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { generateText, tool } from 'ai';
import { z } from 'zod';
import { createSelfHostChatModel, type SelfHostChatConnection } from '../src/agent/selfhost-chat-provider.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';

const common = { apiKey: 'explicit-fixture-key', modelId: 'fixture-model' };
const connections: SelfHostChatConnection[] = [
  { ...common, provider: 'google' }, { ...common, provider: 'openai' }, { ...common, provider: 'anthropic' },
  { ...common, provider: 'qwen', deployment: { region: 'us' } },
  { ...common, provider: 'qwen', deployment: { region: 'singapore', workspaceId: 'fixture-workspace' } },
];
function response(provider: string) {
  const body = provider === 'google'
    ? { candidates: [{ content: { role: 'model', parts: [{ text: 'answer' }] }, finishReason: 'STOP' }],
        usageMetadata: { promptTokenCount: 3, candidatesTokenCount: 2, totalTokenCount: 5 } }
    : provider === 'anthropic'
      ? { id: 'msg_fixture', type: 'message', role: 'assistant', model: common.modelId,
          content: [{ type: 'text', text: 'answer' }], stop_reason: 'end_turn', stop_sequence: null,
          usage: { input_tokens: 3, output_tokens: 2 } }
      : { id: 'chatcmpl_fixture', object: 'chat.completion', created: 1, model: common.modelId,
          choices: [{ index: 0, message: { role: 'assistant', content: 'answer' }, finish_reason: 'stop' }],
          usage: { prompt_tokens: 3, completion_tokens: 2, total_tokens: 5 } };
  return new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
}

for (const connection of connections) {
  const label = connection.provider === 'qwen' ? `qwen ${connection.deployment.region}` : connection.provider;
  test(`${label}: explicit credentials, provider wire format and tool schema survive the common interface`, async () => {
    let calls = 0;
    const model = createSelfHostChatModel({ ...connection, fetch: async (input, init) => {
      calls++;
      const url = new URL(String(input));
      const body = JSON.parse(String(init?.body));
      assert.equal(init?.redirect, 'error');
      assert.equal(init?.method, 'POST');
      assert.ok(body.tools.length > 0);
      assert.match(JSON.stringify(body.tools), /city/);
      const headers = new Headers(init?.headers);
      if (connection.provider === 'google') {
        assert.equal(url.href, 'https://generativelanguage.googleapis.com/v1beta/models/fixture-model:generateContent');
        assert.equal(headers.get('x-goog-api-key'), common.apiKey);
      } else {
        assert.equal(body.model, common.modelId);
        if (connection.provider === 'anthropic') {
          assert.equal(url.href, 'https://api.anthropic.com/v1/messages');
          assert.equal(headers.get('x-api-key'), common.apiKey);
        } else {
          const expected = connection.provider === 'openai' ? 'https://api.openai.com/v1/chat/completions'
            : connection.provider === 'qwen' && connection.deployment.region === 'us' ? 'https://dashscope-us.aliyuncs.com/compatible-mode/v1/chat/completions'
              : 'https://fixture-workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions';
          assert.equal(url.href, expected);
          assert.equal(headers.get('authorization'), `Bearer ${common.apiKey}`);
        }
      }
      return response(connection.provider);
    } });
    const result = await generateText({ model, prompt: 'hello', maxRetries: 0,
      tools: { weather: tool({ description: 'Read weather', inputSchema: z.object({ city: z.string() }) }) } });
    assert.equal(result.text, 'answer');
    assert.equal(result.usage.inputTokens, 3);
    assert.equal(calls, 1);
  });
  test(`${label}: rejects injected credentials before any contact`, async () => {
    let calls = 0;
    const model = createSelfHostChatModel({ ...connection, fetch: async () => { calls++; return response(connection.provider); } });
    for (const header of ['authorization', 'x-api-key', 'x-goog-api-key']) {
      await assert.rejects(generateText({ model, prompt: 'hello', maxRetries: 0, headers: { [header]: 'replacement-key' } }), /request is invalid/);
    }
    assert.equal(calls, 0);
  });
}

test('invalid credentials, model IDs and Qwen deployment cannot resolve through environment defaults', () => {
  for (const apiKey of ['', ' key', 'key\n', 'keyé']) {
    assert.throws(() => createSelfHostChatModel({ ...common, provider: 'openai', apiKey }));
  }
  assert.throws(() => createSelfHostChatModel({ ...common, provider: 'anthropic', modelId: '../elsewhere' }));
  assert.throws(() => createSelfHostChatModel({ ...common, provider: 'qwen', deployment: { region: 'beijing', workspaceId: 'x.evil.example' } }));
  assert.throws(() => createSelfHostChatModel({ ...common, provider: 'other' } as unknown as SelfHostChatConnection));
});

test('provider failure makes one request and does not contact another provider', async () => {
  let calls = 0;
  const model = createSelfHostChatModel({ ...common, provider: 'openai', fetch: async () => {
    calls++;
    return new Response(JSON.stringify({ error: { message: 'busy', type: 'server_error' } }), { status: 503 });
  } });
  await assert.rejects(generateText({ model, prompt: 'hello', maxRetries: 0 }));
  assert.equal(calls, 1);
});

test('alternative provider models are accepted by the actual Mastra runtime', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'provider-mastra-'));
  try {
    for (const provider of ['openai', 'anthropic', 'qwen'] as const) {
      const connection = provider === 'qwen' ? { ...common, provider, deployment: { region: 'us' as const } }
        : { ...common, provider };
      let contacts = 0;
      const model = createSelfHostChatModel({ ...connection, fetch: async () => { contacts++; return response(provider); } });
      const runtime = createSottoAgentRuntime({ model, storagePath: join(directory, `${provider}.db`), instructions: 'Answer briefly.' });
      try {
        assert.equal((await runtime.generateChat({ text: 'hello', threadId: 'fixture', ownerId: '123' })).text, 'answer');
        assert.equal(contacts, 1);
      } finally { await runtime.close(); }
    }
  } finally { await rm(directory, { recursive: true, force: true }); }
});
