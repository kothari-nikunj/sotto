import test from 'node:test';
import assert from 'node:assert/strict';
import { Agent } from '@mastra/core/agent';
import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import { createSelfHostGoogleModels } from '../src/agent/selfhost-models.js';

const apiKey = 'synthetic-google-api-key-0123456789';
const chatModelId = 'fixture-chat-model';
const nativeModelId = 'fixture-native-model';
function response(value: unknown): Response {
  return new Response(JSON.stringify(value), { status: 200, headers: { 'content-type': 'application/json' } });
}

test('Google SDK model completes a real Mastra tool loop through the injected synthetic fetch', async () => {
  const requests: Array<{ url: string; headers: Headers; body: Record<string, unknown> }> = [];
  const model = createSelfHostGoogleModels({ apiKey, chatModelId, nativeMediaModelId: nativeModelId,
    fetch: async (input, init) => {
      const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
      const headers = new Headers(init?.headers);
      assert.equal(url.origin, 'https://generativelanguage.googleapis.com');
      assert.equal(url.pathname, `/v1beta/models/${chatModelId}:generateContent`);
      assert.equal(headers.get('x-goog-api-key'), apiKey);
      assert.equal(headers.has('authorization'), false);
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      requests.push({ url: url.toString(), headers, body });
      if (requests.length === 1) {
        return response({ candidates: [{ content: { role: 'model', parts: [
          { functionCall: { name: 'fixture_lookup', args: { query: 'cedar packet' } } },
        ] }, finishReason: 'STOP' }], usageMetadata: { promptTokenCount: 3, candidatesTokenCount: 2, totalTokenCount: 5 } });
      }
      return response({ candidates: [{ content: { role: 'model', parts: [
        { text: 'The fixture lookup found the cedar packet.' },
      ] }, finishReason: 'STOP' }], usageMetadata: { promptTokenCount: 5, candidatesTokenCount: 6, totalTokenCount: 11 } });
    } });
  let calls = 0;
  const fixtureTool = createTool({ id: 'fixture_lookup', description: 'Read one synthetic fixture value.',
    inputSchema: z.object({ query: z.string() }),
    execute: async ({ query }) => { calls++; return { query, result: 'found' }; } });
  const agent = new Agent({ id: 'selfhost-google-test', name: 'Self-host Google test',
    instructions: 'Use fixture_lookup for the requested fixture.', model: model.chatModel,
    tools: { fixture_lookup: fixtureTool } });
  const result = await agent.generate('Look up the cedar packet.');
  assert.match(result.text, /cedar packet/);
  assert.equal(calls, 1);
  assert.equal(requests.length, 2);
  assert.ok(JSON.stringify(requests[0]?.body).includes('fixture_lookup'));
  assert.ok(JSON.stringify(requests[1]?.body).includes('functionResponse'));
});

test('native Google media model rejects tool-bearing requests before synthetic transport', async () => {
  let contacts = 0;
  const models = createSelfHostGoogleModels({ apiKey, chatModelId, nativeMediaModelId: nativeModelId,
    fetch: async () => { contacts++; return response({ candidates: [] }); } });
  const tool = { description: 'must not be sent', inputSchema: z.object({ value: z.string() }),
    execute: async () => 'forbidden' };
  await assert.rejects(Promise.resolve(models.nativeMediaModel.doGenerate({
    prompt: [{ role: 'user', content: [{ type: 'text', text: 'interpret fixture' }] }],
    tools: [{ type: 'function', name: 'forbidden_tool', description: tool.description,
      inputSchema: tool.inputSchema }],
    toolChoice: { type: 'auto' },
    responseFormat: { type: 'text' },
    stopSequences: [],
    maxOutputTokens: 32,
    temperature: 0,
    topP: undefined,
    topK: undefined,
    presencePenalty: undefined,
    frequencyPenalty: undefined,
    seed: undefined,
    headers: undefined,
    abortSignal: undefined,
    providerOptions: {},
  } as never)), /cannot include tools/i);
  assert.equal(contacts, 0);
});
