import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime, type SottoAgentOptions} from '../src/agent/runtime.js';

type Timing = Parameters<NonNullable<SottoAgentOptions['onChatTiming']>>[0];
const usage = {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0},
  outputTokens: {total: 2, text: 2, reasoning: 0}};
const reply = () => ({content: [{type: 'text' as const, text: 'fixture answer'}],
  finishReason: {unified: 'stop' as const, raw: 'STOP'}, usage, warnings: []});
const mediaTurn = {text: 'Describe this.', threadId: 't1', ownerId: 'o1', messages: [{
  channel: 'telegram' as const, eventId: 'image', accountId: 'a1', senderId: 'o1', isDirect: true,
  media: [{data: new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), mimeType: 'image/png'}],
}]};
function gate() {
  let release!: () => void;
  const promise = new Promise<void>(resolve => {release = resolve;});
  return {promise, release};
}

test('prompt preparation includes native media interpretation before Mastra generation', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-timing-media-'));
  let timing: Timing | undefined;
  let interpretationMs = 0;
  const runtime = createSottoAgentRuntime({model: new MockLanguageModelV4({doGenerate: async () => reply()}),
    storagePath: join(directory, 'memory.db'), instructions: 'Answer briefly.',
    mediaInterpreter: async () => {
      const started = performance.now();
      await delay(35);
      interpretationMs = performance.now() - started;
      return {text: 'A synthetic image.'};
    }, onChatTiming: value => {timing = value;}});
  try {
    assert.equal((await runtime.generateChat(mediaTurn)).text, 'fixture answer');
    assert.ok(timing);
    assert.ok(timing.promptPreparationMs! >= Math.floor(interpretationMs), 'media time belongs to prompt preparation');
    assert.ok(timing.totalMs >= timing.promptPreparationMs!, 'preparation remains inside the existing total');
  } finally {await runtime.close(); await rm(directory, {recursive: true, force: true});}
});

test('cancelled prompt preparation reports elapsed work without inventing generation time', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-timing-preparation-failed-'));
  const controller = new AbortController();
  const timings: Array<{timing: Timing; outcome?: string}> = [];
  const model = new MockLanguageModelV4({doGenerate: async () => reply()});
  let interpretationMs = 0;
  const runtime = createSottoAgentRuntime({model, storagePath: join(directory, 'memory.db'), instructions: 'Answer briefly.',
    mediaInterpreter: async () => {
      const started = performance.now();
      await delay(35);
      interpretationMs = performance.now() - started;
      controller.abort();
      return {text: 'Discarded interpretation.'};
    }, onChatTiming: (timing, outcome) => {timings.push({timing, outcome});}});
  try {
    await assert.rejects(runtime.generateChat({...mediaTurn, signal: controller.signal}), /cancelled/);
    assert.equal(model.doGenerateCalls.length, 0);
    assert.equal(timings.length, 1);
    assert.equal(timings[0]!.outcome, 'failed');
    assert.ok(timings[0]!.timing.promptPreparationMs! >= Math.floor(interpretationMs));
    assert.equal(timings[0]!.timing.generationMs, 0);
    assert.equal(timings[0]!.timing.finalSourceCheckMs, 0);
  } finally {await runtime.close(); await rm(directory, {recursive: true, force: true});}
});

test('runtime serialization wait is reported separately from active turn total', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-timing-queue-'));
  const entered = gate(), held = gate();
  const timings: Timing[] = [];
  let calls = 0;
  const runtime = createSottoAgentRuntime({storagePath: join(directory, 'memory.db'), instructions: 'Answer briefly.',
    model: new MockLanguageModelV4({doGenerate: async () => {
      if (++calls === 1) {entered.release(); await held.promise;}
      return reply();
    }}), onChatTiming: timing => {timings.push(timing);}});
  const first = runtime.generateChat({text: 'First turn.', threadId: 't1', ownerId: 'o1'});
  try {
    await entered.promise;
    const secondStarted = performance.now();
    const second = runtime.generateChat({text: 'Second turn.', threadId: 't1', ownerId: 'o1'});
    await delay(35);
    const blockedMs = performance.now() - secondStarted;
    assert.equal(calls, 1, 'second model call is still queued');
    held.release();
    await first;
    assert.equal((await second).text, 'fixture answer');
    const wallMs = performance.now() - secondStarted;
    assert.equal(timings.length, 2);
    assert.ok(timings[1]!.queueMs! >= Math.floor(blockedMs) - 1);
    assert.ok(wallMs - timings[1]!.totalMs >= blockedMs - 2, 'existing total excludes the queue wait');
  } finally {held.release(); await first; await runtime.close(); await rm(directory, {recursive: true, force: true});}
});
