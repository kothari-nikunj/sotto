import {acceptedFixture} from './helpers/accepted-memory.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { inspect } from 'node:util';
import { MockLanguageModelV4 } from 'ai/test';
import { createSottoAgentRuntime, MODEL_TURN_DEADLINE_MS } from '../src/agent/runtime.js';
import { OWNER_WEB_READ_DEADLINE_MS } from '../src/tools/owner-web-read.js';
import { createManagedProxyModels, ManagedProxyAdmissionBusy } from '../src/agent/models.js';

function fakeModel() {
  const model = new MockLanguageModelV4({
    doGenerate: async () => ({
      content: [{ type: 'text', text: 'fixture answer' }],
      finishReason: { unified: 'stop', raw: 'STOP' },
      usage: {
        inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 },
        outputTokens: { total: 2, text: 2, reasoning: 0 },
      },
      warnings: [],
    }),
  });
  return model;
}

test('model failures do not log owner prompts or provider error contents', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-agent-private-error-'));
  const secret = 'private-owner-prompt-and-provider-body-fixture';
  const logged: string[] = [];
  const original = {log:console.log, info:console.info, warn:console.warn, error:console.error, debug:console.debug};
  for (const method of ['log','info','warn','error','debug'] as const) {
    console[method] = (...args: unknown[]) => { logged.push(inspect(args, {depth:null})); };
  }
  const runtime = createSottoAgentRuntime({
    model: new MockLanguageModelV4({doGenerate: async () => { throw new Error(secret); }}),
    storagePath: join(directory, 'memory.db'), instructions:'Answer briefly.',
  });
  try {
    await assert.rejects(runtime.generateChat({text:secret,threadId:'private-test',ownerId:'owner-test'}));
    await assert.rejects(runtime.generateUnattended({text:secret,runId:'private-unattended'}));
    assert.equal(logged.join('\n').includes(secret), false, 'SDK failure logs must exclude content');
  } finally {
    await runtime.close();
    Object.assign(console, original);
    await rm(directory, {recursive:true,force:true});
  }
});

test('real unattended Mastra call preserves one verified proxy busy refusal', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-agent-proxy-busy-'));
  let contacts = 0;
  const transport: typeof fetch = async (_input, init) => {
    contacts++;
    assert.equal(new Headers(init?.headers).get('X-Sotto-Workload'), 'unattended');
    return new Response('{"error":"busy"}', { status: 429, headers: { 'X-Sotto-Admission': 'busy' } });
  };
  const models = createManagedProxyModels({ origin: 'https://proxy.example', allowedOrigin: 'https://proxy.example',
    bearer: 'fixture-secret', chatModelId: 'chat-fixture', nativeMediaModelId: 'native-fixture', fetch: transport });
  const runtime = createSottoAgentRuntime({ model: models.chatModel, storagePath: join(directory, 'memory.db'),
    instructions: 'Answer briefly.' });
  try {
    await assert.rejects(runtime.generateUnattended({ text: 'background', runId: 'r1' }), ManagedProxyAdmissionBusy);
    assert.equal(contacts, 1);
  } finally { await runtime.close(); await rm(directory, { recursive: true, force: true }); }
});

for (const mode of ['chat', 'unattended'] as const) {
  for (const failure of ['500', '503', 'busy', 'budget', 'connection', 'cancelled'] as const) {
    test(`${mode} ${failure} makes only one managed proxy reservation`, async () => {
      const directory = await mkdtemp(join(tmpdir(), 'sotto-agent-no-retry-'));
      const operations: string[] = [];
      const controller = new AbortController();
      const models = createManagedProxyModels({ origin: 'https://proxy.example', allowedOrigin: 'https://proxy.example',
        bearer: 'fixture-secret', chatModelId: 'chat-fixture', nativeMediaModelId: 'native-fixture',
        fetch: async (_input, init) => {
          operations.push(new Headers(init?.headers).get('X-Sotto-Operation')!);
          if (failure === 'connection') throw new TypeError('fetch failed');
          if (failure === 'cancelled') { controller.abort(); throw new DOMException('Cancelled', 'AbortError'); }
          if (failure === 'busy') return new Response('{}', {status:429, headers:{'X-Sotto-Admission':'busy'}});
          if (failure === 'budget') return new Response(JSON.stringify({error:{code:'sotto_budget_exhausted',type:'budget_exhausted'}}), {status:402});
          return new Response('{"error":"temporary"}', {status:Number(failure)});
        } });
      const runtime = createSottoAgentRuntime({model:models.chatModel,storagePath:join(directory,'memory.db'),instructions:'Answer briefly.'});
      try {
        await assert.rejects(mode === 'chat'
          ? runtime.generateChat({text:'hello',threadId:'t1',ownerId:'o1',signal:controller.signal})
          : runtime.generateUnattended({text:'hello',runId:'r1',signal:controller.signal}));
        assert.equal(operations.length, 1);
        assert.equal(new Set(operations).size, 1);
      } finally { await runtime.close(); await rm(directory,{recursive:true,force:true}); }
    });
  }
}

test('the default turn deadline outlasts every in-turn owner tool deadline', () => {
  // A DocSend read is attended inside the chat turn; aborting it early turns a healthy read into unknown.
  assert.ok(MODEL_TURN_DEADLINE_MS > OWNER_WEB_READ_DEADLINE_MS.operation + 60_000);
  assert.ok(MODEL_TURN_DEADLINE_MS > OWNER_WEB_READ_DEADLINE_MS.read + 60_000);
});

test('a model deadline aborts a hung turn and lets the queued turn run', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-agent-deadline-'));
  let first = true;
  const model = new MockLanguageModelV4({doGenerate: async ({abortSignal}) => {
    if (first) {
      first = false;
      if (abortSignal?.aborted) throw abortSignal.reason;
      await new Promise<void>((_resolve, reject) => abortSignal?.addEventListener('abort', () => reject(abortSignal.reason), {once:true}));
    }
    return {content:[{type:'text' as const,text:'next turn'}],finishReason:{unified:'stop' as const,raw:'STOP'},
      usage:{inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}},warnings:[]};
  }});
  // Cold Mastra setup must reach the held provider call even under full-suite load.
  // Keep the exact two-call assertion below: the timeout must release the queued turn.
  const runtime = createSottoAgentRuntime({model,storagePath:join(directory,'memory.db'),instructions:'Answer briefly.',modelTurnDeadlineMs:5000});
  const controller = new AbortController();
  let fallbackTriggered = false;
  const fallback = setTimeout(() => { fallbackTriggered = true; controller.abort(new Error('test fallback abort')); }, 15_000);
  try {
    const hung = runtime.generateChat({text:'hung',threadId:'t1',ownerId:'o1',signal:controller.signal});
    const next = runtime.generateChat({text:'next',threadId:'t1',ownerId:'o1'});
    await assert.rejects(hung);
    assert.equal(fallbackTriggered, false, 'turn must end from its own deadline');
    assert.equal((await next).text,'next turn');
    assert.equal(model.doGenerateCalls.length, 2);
  } finally { clearTimeout(fallback); await runtime.close(); await rm(directory,{recursive:true,force:true}); }
});

test('Mastra runs fake-model chat with durable history, but unattended has no transcript', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-agent-'));
  const storagePath = join(directory, 'memory.db');
  const firstModel = fakeModel();
  const first = createSottoAgentRuntime({ model: firstModel, storagePath, instructions: 'Answer briefly.' });
  try {
    assert.equal(process.env.MASTRA_TELEMETRY_DISABLED, 'true');
    assert.equal((await acceptedFixture(first, { text: 'first', threadId: 't1', ownerId: 'o1' })).text, 'fixture answer');
    await first.close();
    const model = fakeModel();
    const runtime = createSottoAgentRuntime({ model, storagePath, instructions: 'Answer briefly.' });
    try {
    assert.equal((await runtime.generateChat({ text: 'second', threadId: 't1', ownerId: 'o1' })).text, 'fixture answer');
    assert.equal((await runtime.generateUnattended({ text: 'background', runId: 'r1' })).text, 'fixture answer');
    const threads = await runtime.memory.listThreads({ perPage: false });
    assert.equal(threads.threads.length, 1);
    assert.equal(threads.threads[0]?.id, 't1');
    assert.equal(model.doGenerateCalls.length, 2);
    assert.ok(JSON.stringify(model.doGenerateCalls[0]?.prompt).includes('first'), 'history survived runtime restart');
    assert.equal(JSON.stringify(model.doGenerateCalls[1]?.prompt).includes('first'), false);
    await runtime.rotateThread('t1');
    assert.equal((await runtime.memory.listThreads({ perPage: false })).threads.length, 0);
    } finally { await runtime.close(); }
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('chat interprets only supplied bounded media bytes and labels limitations', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-agent-media-'));
  const model = fakeModel();
  const interpreted: Array<{ count: number; mediaType: string; bytes: number }> = [];
  const runtime = createSottoAgentRuntime({ model, storagePath: join(directory, 'memory.db'), instructions: 'Answer briefly.',
    mediaInterpreter: async (_prompt, media) => {
      interpreted.push({ count: media.length, mediaType: media[0]!.mediaType, bytes: media[0]!.bytes.byteLength });
      return { text: 'a red kite' };
    },
  });
  try {
    await runtime.generateChat({ text: '[attachment received]', threadId: 't1', ownerId: 'o1', messages: [{
      channel: 'photon', eventId: 'e1', accountId: 'a1', senderId: 'o1', isDirect: true,
      media: [
        { data: new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), mimeType: 'image/png' },
        { url: 'https://remote.example/private.png', mimeType: 'image/png' },
        { data: new Uint8Array([9]), mimeType: 'application/pdf' },
      ],
    }] });
    assert.deepEqual(interpreted, [{ count: 1, mediaType: 'image/png', bytes: 8 }]);
    const prompt = JSON.stringify(model.doGenerateCalls[0]?.prompt);
    assert.ok(prompt.includes('a red kite'));
    assert.ok(prompt.includes('untrusted attachment evidence'));
    assert.ok(prompt.includes('bytes were unavailable'));
    assert.ok(prompt.includes('unsupported media type'));
    assert.equal(prompt.includes('https://remote.example'), false);
    await runtime.generateUnattended({ text: 'background', runId: 'r1' });
    assert.equal(JSON.stringify(model.doGenerateCalls[1]?.prompt).includes('red kite'), false);
  } finally { await runtime.close(); await rm(directory, { recursive: true, force: true }); }
});

test('media omission counts and mismatched signatures remain explicit without interpreting missing content', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-agent-media-omissions-'));
  const model = fakeModel();
  let interpretations = 0;
  const runtime = createSottoAgentRuntime({ model, storagePath: join(directory, 'memory.db'), instructions: 'Answer briefly.',
    mediaInterpreter: async () => { interpretations++; return { text: 'the one available photo' }; },
  });
  const png = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
  try {
    await runtime.generateChat({ text: '[attachment received]', threadId: 't1', ownerId: 'o1', messages: [{
      channel: 'telegram', eventId: 'album', accountId: 'a1', senderId: 'o1', isDirect: true,
      media: [
        { data: png, mimeType: 'image/jpeg' },
        { omission: 'too_many', omittedCount: 6 },
        { data: png, mimeType: 'image/png' },
        { omission: 'timed_out' },
      ],
    }] });
    assert.equal(interpretations, 1);
    const prompt = JSON.stringify(model.doGenerateCalls[0]?.prompt);
    assert.ok(prompt.includes('6 additional attachments omitted'));
    assert.ok(prompt.includes('declared media type did not match'));
    assert.ok(prompt.includes('provider read timed out'));
    assert.ok(prompt.includes('the one available photo'));
  } finally { await runtime.close(); await rm(directory, { recursive: true, force: true }); }
});


test('chat timing reports only durations and cannot fail a completed reply', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-chat-timing-'));
  const timings: unknown[] = [];
  const runtime = createSottoAgentRuntime({model:fakeModel(),storagePath:join(directory,'memory.db'),instructions:'Answer briefly.',
    onChatTiming: timing => { timings.push(timing); throw new Error('diagnostic sink unavailable'); }});
  try {
    assert.equal((await runtime.generateChat({text:'private owner text',threadId:'private-thread',ownerId:'private-owner'})).text,'fixture answer');
    assert.equal(timings.length,1);
    const timing = timings[0] as Record<string,number>;
    assert.deepEqual(Object.keys(timing).sort(),['sourceCheckMs','standingContextMs','toolSetupMs','generationMs','finalSourceCheckMs','totalMs','queueMs','promptPreparationMs'].sort());
    assert.ok(Object.values(timing).every(value => Number.isInteger(value) && value >= 0));
    assert.ok(timing.totalMs! >= timing.generationMs!);
  } finally { await runtime.close(); await rm(directory,{recursive:true,force:true}); }
});

test('failed model attempt reports completed numeric stages without changing the failure', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-chat-failed-timing-'));
  const timings: Array<{timing: Record<string, number>; outcome: string | undefined}> = [];
  const model = new MockLanguageModelV4({doGenerate: async () => { throw new Error('private proxy 503'); }});
  const runtime = createSottoAgentRuntime({model, storagePath: join(directory, 'memory.db'), instructions: 'Answer briefly.',
    onChatTiming: (timing, outcome) => { timings.push({timing, outcome}); }});
  try {
    await assert.rejects(runtime.generateChat({text: 'private owner prompt', threadId: 't1', ownerId: 'o1'}));
    assert.equal(timings.length, 1);
    assert.equal(timings[0]!.outcome, 'failed');
    assert.deepEqual(Object.keys(timings[0]!.timing).sort(),
      ['sourceCheckMs', 'standingContextMs', 'toolSetupMs', 'generationMs', 'finalSourceCheckMs', 'totalMs', 'queueMs', 'promptPreparationMs'].sort());
    assert.ok(Object.values(timings[0]!.timing).every(value => Number.isInteger(value) && value >= 0));
    assert.ok(timings[0]!.timing.totalMs! >= timings[0]!.timing.generationMs!);
  } finally { await runtime.close(); await rm(directory, {recursive: true, force: true}); }
});
