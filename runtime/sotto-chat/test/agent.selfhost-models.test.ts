import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { generateText } from 'ai';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createSelfHostGoogleModels } from '../src/agent/selfhost-models.js';
import { acceptedFixture } from './helpers/accepted-memory.js';

const configuration = { apiKey: 'explicit-fixture-key', chatModelId: 'gemini-chat-fixture', nativeMediaModelId: 'gemini-media-fixture' };
function reply(text: string) {
  return new Response(JSON.stringify({ candidates: [{ content: { role: 'model', parts: [{ text }] }, finishReason: 'STOP' }],
    usageMetadata: { promptTokenCount: 3, candidatesTokenCount: 2, totalTokenCount: 5 } }),
  { headers: { 'content-type': 'application/json' } });
}

test('explicit self-host Google model preserves accepted history and separates unattended generation', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'selfhost-google-'));
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
  const transport: typeof fetch = async (input, init) => {
    const headers = new Headers(init?.headers);
    assert.equal(headers.get('x-goog-api-key'), configuration.apiKey);
    assert.equal(headers.get('authorization'), null);
    assert.equal(init?.redirect, 'error');
    calls.push({ url: String(input), body: JSON.parse(String(init?.body)) });
    return reply('synthetic Google answer');
  };
  const models = createSelfHostGoogleModels({ ...configuration, fetch: transport });
  const options = { model: models.chatModel, storagePath: join(directory, 'memory.db'), instructions: 'Answer briefly.' };
  let runtime = createSottoAgentRuntime(options);
  try {
    assert.equal((await acceptedFixture(runtime, { text: 'first private question', threadId: 't1', ownerId: '123' })).text, 'synthetic Google answer');
    await runtime.close();
    runtime = createSottoAgentRuntime(options);
    assert.equal((await runtime.generateChat({ text: 'second question', threadId: 't1', ownerId: '123' })).text, 'synthetic Google answer');
    assert.equal((await runtime.generateUnattended({ text: 'scheduled question', runId: 'r1' })).text, 'synthetic Google answer');
    assert.equal(calls.length, 3);
    assert.ok(calls.every(call => call.url === 'https://generativelanguage.googleapis.com/v1beta/models/gemini-chat-fixture:generateContent'));
    assert.match(JSON.stringify(calls[1]!.body), /first private question/);
    assert.doesNotMatch(JSON.stringify(calls[2]!.body), /first private question|second question/);
    assert.equal((await runtime.memory.listThreads({ perPage: false })).threads.length, 1);
    assert.doesNotMatch(JSON.stringify(await runtime.memory.recall({threadId:'t1',resourceId:'123'})),
      /second question|scheduled question/, 'generation alone must not persist an unaccepted turn');
  } finally { await runtime.close(); await rm(directory, { recursive: true, force: true }); }
});

test('self-host model credential cannot be overridden by call headers; native media stays bounded and tool-free', async () => {
  let contacts = 0;
  const transport: typeof fetch = async (input, init) => {
    contacts++;
    assert.equal(String(input), 'https://generativelanguage.googleapis.com/v1beta/models/gemini-media-fixture:generateContent');
    const body = JSON.parse(String(init?.body));
    assert.equal('tools' in body, false);
    assert.equal('toolConfig' in body, false);
    assert.match(JSON.stringify(body), /inlineData/);
    return reply('synthetic image description');
  };
  const models = createSelfHostGoogleModels({ ...configuration, fetch: transport });
  for (const headers of [{ 'x-goog-api-key': 'different-key' }, { authorization: 'Bearer different-key' }]) {
    await assert.rejects(generateText({ model: models.chatModel, prompt: 'hello', headers, maxRetries: 0 }), /Self-host model request is invalid/);
  }
  assert.equal(contacts, 0);
  await assert.rejects(models.generateNativeMedia('bad image', [{ bytes: new Uint8Array([1]), mediaType: 'image/png' }]));
  await assert.rejects(models.generateNativeMedia('oversize', [{ bytes: new Uint8Array(8 * 1024 * 1024 + 1), mediaType: 'image/png' }]));
  assert.equal(contacts, 0);
  assert.equal((await models.generateNativeMedia('describe', [{ bytes: new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), mediaType: 'image/png' }])).text,
    'synthetic image description');
  assert.equal(contacts, 1);
  for (const apiKey of ['', ' key', 'key\n', 'keyé']) assert.throws(() => createSelfHostGoogleModels({ ...configuration, apiKey }));
  assert.throws(() => createSelfHostGoogleModels({ ...configuration, chatModelId: '../other' }));
});
