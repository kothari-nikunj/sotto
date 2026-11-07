/**
 * Offline attribution probe, not a production latency estimate. Uses the real
 * Mastra runtime with mock models/tools, synthetic delays and temporary storage.
 * No credentials, provider requests, Bridge connections or real source data.
 * Run `npm run benchmark:latency`; use `npm run --silent benchmark:latency` for JSON.
 */
import assert from 'node:assert/strict';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import type {ToolsInput} from '@mastra/core/agent';
import {createTool} from '@mastra/core/tools';
import {z} from 'zod';
import {createSottoAgentRuntime, type ChatTurn, type SottoAgentOptions} from '../src/agent/runtime.js';
import type {ModelMetrics} from '../src/agent/metrics.js';
import {startApplication} from '../src/application.js';

const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));
const round = (ms: number) => Math.round(ms * 10) / 10;
const modelDelays = {toolStep:180, answer:280};
const toolInputs = [
  {tool:'delay_a', ms:240}, {tool:'delay_b', ms:420}, {tool:'delay_a', ms:610},
];
const usage = {
  inputTokens:{total:17, noCache:17, cacheRead:0, cacheWrite:0},
  outputTokens:{total:7, text:7, reasoning:0},
};

interface Scenario {
  name: string;
  tools?: boolean;
  catalogMs?: number;
  standingMs?: number;
  mediaMs?: number;
  queued?: boolean;
}
type ChatTiming = Parameters<NonNullable<SottoAgentOptions['onChatTiming']>>[0];

async function runScenario(scenario: Scenario) {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-latency-benchmark-'));
  const timings: Array<{outcome:string; phases:ChatTiming; elapsedMs:number}> = [];
  const metrics: ModelMetrics[] = [];
  const modelDurations: number[] = [];
  const toolStartedAt: number[] = [];
  const counts = {modelCalls:0, toolCalls:0, catalogResolutions:0, standingReads:0, mediaCalls:0};
  let activeTools = 0;
  let maxConcurrentTools = 0;
  let started = 0;
  const fixtureTool = (id: string) => createTool({
    id, description:'Wait for a synthetic local benchmark delay.',
    inputSchema:z.object({ms:z.number().int().min(0).max(1000)}),
    execute:async ({ms}) => {
      counts.toolCalls++;
      toolStartedAt.push(performance.now());
      activeTools++;
      maxConcurrentTools = Math.max(maxConcurrentTools, activeTools);
      try { await delay(ms); return {ok:true}; }
      finally { activeTools--; }
    },
  });
  const model = new MockLanguageModelV4({doGenerate:async () => {
    counts.modelCalls++;
    const toolStep = scenario.tools === true && counts.modelCalls === 1;
    const modelStarted = performance.now();
    await delay(toolStep ? modelDelays.toolStep : modelDelays.answer);
    modelDurations.push(round(performance.now() - modelStarted));
    return {
      content:toolStep
        ? toolInputs.map(({tool, ms}, index) => ({type:'tool-call' as const,
          toolCallId:`synthetic-${index}`, toolName:tool, input:JSON.stringify({ms})}))
        : [{type:'text' as const, text:'Synthetic benchmark complete.'}],
      finishReason:{unified:toolStep ? 'tool-calls' as const : 'stop' as const, raw:'STOP'},
      usage, warnings:[],
    };
  }});
  const runtime = createSottoAgentRuntime({
    model, storagePath:join(directory, 'memory.db'), instructions:'Synthetic offline latency probe.',
    chatTools:async (): Promise<ToolsInput> => {
      counts.catalogResolutions++;
      if (scenario.catalogMs) await delay(scenario.catalogMs);
      return scenario.tools ? {delay_a:fixtureTool('delay_a'), delay_b:fixtureTool('delay_b')} : {};
    },
    ...(scenario.standingMs ? {readMasterContext:async () => {
      counts.standingReads++;
      await delay(scenario.standingMs!);
      const text = 'Synthetic standing context.';
      return {text, prompt:text, chars:text.length, cap:100};
    }} : {}),
    ...(scenario.mediaMs ? {mediaInterpreter:async () => {
      counts.mediaCalls++;
      await delay(scenario.mediaMs!);
      return {text:'Synthetic media description.'};
    }} : {}),
    onChatTiming:(phases, outcome) => timings.push({
      outcome:outcome ?? 'unknown', phases, elapsedMs:round(performance.now() - started),
    }),
    onModelMetrics:value => { metrics.push(value); },
  });
  const turn = (index: number): ChatTurn => ({
    text:'Run the synthetic probe.', threadId:`benchmark-${index}`, ownerId:'synthetic-owner',
    ...(scenario.mediaMs ? {messages:[{
      channel:'photon', eventId:'synthetic-event', accountId:'synthetic-account',
      senderId:'synthetic-owner', isDirect:true,
      // Only the mock interpreter sees these signature bytes; no image is fetched.
      media:[{data:new Uint8Array([137,80,78,71,13,10,26,10]), mimeType:'image/png'}],
    }]} : {}),
  });
  try {
    started = performance.now();
    const first = runtime.generateChat(turn(1));
    // Different threads expose the runtime's shared chat queue.
    const pending = scenario.queued ? [first, runtime.generateChat(turn(2))] : [first];
    await Promise.all(pending);
    return {
      scenario:scenario.name,
      injected:{
        turns:scenario.queued ? 2 : 1,
        modelDelayMs:scenario.tools ? [modelDelays.toolStep, modelDelays.answer]
          : Array.from({length:scenario.queued ? 2 : 1}, () => modelDelays.answer),
        sameStepToolDelayMs:scenario.tools ? toolInputs.map(input => input.ms) : [],
        catalogDelayPerResolutionMs:scenario.catalogMs ?? 0,
        standingContextDelayMs:scenario.standingMs ?? 0,
        mediaInterpretationDelayMs:scenario.mediaMs ?? 0,
      },
      observed:{
        wallMs:round(performance.now() - started), ...counts, modelCallMs:modelDurations,
        maxConcurrentTools,
        toolStartSpreadMs:toolStartedAt.length
          ? round(Math.max(...toolStartedAt) - Math.min(...toolStartedAt)) : null,
        turns:timings.map((timing, index) => ({...timing,
          modelMetrics:metrics[index] ? {
            outcome:metrics[index]!.outcome, stepCount:metrics[index]!.stepCount,
            steps:metrics[index]!.steps, tools:metrics[index]!.tools,
          } : null,
        })),
      },
    };
  } finally {
    try { await runtime.close(); }
    finally { await rm(directory, {recursive:true, force:true}); }
  }
}

async function runApplicationScenario() {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-application-latency-benchmark-'));
  const injectedMs = 25;
  let runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  let started = 0;
  let timing: ChatTiming | undefined;
  let timingPublishedMs: number | null = null;
  let providerAcceptedMs: number | null = null;
  let sentSequence = 0;
  const counts = {sourceAuthorityReads:0, modelCalls:0, providerSends:0, catalogs:0, sourceDataReads:0};
  let shortening = false;
  const turns = [];
  try {
    runtime = createSottoAgentRuntime({
      storagePath:join(directory, 'memory.db'), instructions:'Synthetic offline application probe.',
      sourceAuthority:{read:async () => {
        counts.sourceAuthorityReads++;
        await delay(injectedMs);
        return {version:2, revision:'a'.repeat(64), complete:true,
          bridge_sources:[], google_sources:[], connector_sources:[]};
      }},
      chatTools: async () => {
        counts.catalogs++;
        return {source_probe: createTool({id:'source_probe', description:'Synthetic source read.', inputSchema:z.object({}),
          execute:async () => {counts.sourceDataReads++; return {text:'Synthetic source'};}})};
      },
      model:new MockLanguageModelV4({doGenerate:async options => {
        if (shortening) {
          assert.match(JSON.stringify(options.prompt), /Shorten this exact accepted text/);
          assert.match(JSON.stringify(options.prompt), /Synthetic benchmark complete/);
          assert.equal(options.tools?.length ?? 0, 0);
        }
        counts.modelCalls++;
        await delay(injectedMs);
        return {content:[{type:'text', text:'Synthetic benchmark complete.'}],
          finishReason:{unified:'stop', raw:'STOP'}, usage, warnings:[]};
      }}),
      onChatTiming:value => {
        timing = value;
        timingPublishedMs = round(performance.now() - started);
      },
    });
    app = await startApplication({
      directory, mediaRoot:directory, bearerToken:'fixture-only-bearer-for-local-tests-000',
      owner:{channel:'telegram', accountId:'synthetic-bot', ownerId:'42'},
      lineKey:'synthetic-bot', target:'synthetic-owner', providerThreadId:'telegram:42',
      deferListen:true,
    }, {agent:runtime, transport:{send:async () => {
      counts.providerSends++;
      await delay(injectedMs);
      providerAcceptedMs = round(performance.now() - started);
      return {acceptance:'accepted', providerMessageIds:[`synthetic-${++sentSequence}`]};
    }}});
    // Cold means fresh application state; earlier scenarios may warm SDK modules.
    for (const [index, phase] of ['cold', 'first-shortening', 'warm', 'shortening'].entries()) {
      counts.sourceAuthorityReads = counts.modelCalls = counts.providerSends = counts.catalogs = counts.sourceDataReads = 0;
      shortening = phase.endsWith('shortening');
      timing = undefined;
      timingPublishedMs = providerAcceptedMs = null;
      started = performance.now();
      await app.receive({channel:'telegram', eventId:`synthetic-${index}`, accountId:'synthetic-bot',
        senderId:'42', isDirect:true, text:shortening ? 'shorter pls' : 'Run the synthetic application probe.'});
      const receiveCompletedMs = round(performance.now() - started);
      if (shortening) {
        assert.equal(counts.modelCalls, 1);
        assert.equal(counts.catalogs, 0);
        assert.equal(counts.sourceDataReads, 0);
        assert.equal(counts.providerSends, 1);
        assert.notEqual(providerAcceptedMs, null);
      }
      // The callback runs inside receive(); preserve only bounded numeric fields.
      const chatTiming = timing as ChatTiming | undefined;
      turns.push({phase, receiveCompletedMs, providerAcceptedMs, timingPublishedMs,
        outsideActiveTurnMs:chatTiming ? round(receiveCompletedMs - chatTiming.totalMs) : null,
        ...counts, chatTiming:chatTiming ?? null});
    }
    return {scenario:'application-cold-and-warm',
      injected:{turns:4, sourceAuthorityDelayPerReadMs:injectedMs,
        modelDelayMs:injectedMs, providerSendDelayMs:injectedMs},
      observed:{turns}};
  } finally {
    try { await app?.close(); }
    finally {
      try { await runtime?.close(); }
      finally { await rm(directory, {recursive:true, force:true}); }
    }
  }
}

const scenarios: Scenario[] = [
  {name:'single-model'},
  {name:'parallel-tools', tools:true, catalogMs:71},
  {name:'media-and-standing-context', standingMs:89, mediaMs:457},
  {name:'queued-distinct-threads', queued:true},
];
const results: unknown[] = [];
for (const scenario of scenarios) results.push(await runScenario(scenario));
results.push(await runApplicationScenario());
console.log(JSON.stringify({
  version:1,
  limitation:'Synthetic offline attribution only. Injected delays and cold SDK setup do not estimate production latency.',
  timingSemantics:'totalMs excludes queueMs; toolSetupMs is included in generationMs; concurrent tool durations must not be summed as wall time.',
  results,
}, null, 2));
