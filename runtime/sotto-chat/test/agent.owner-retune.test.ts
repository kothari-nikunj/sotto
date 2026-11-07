import assert from 'node:assert/strict';
import test from 'node:test';
import { createOwnerRetuneTool, readOwnerRetune } from '../src/agent/owner-retune.js';

const bridgeUrl = 'http://127.0.0.1:8793/mcp';
const row = {
  anchor_key: 'manual:key', name: 'Alex', what: 'Send the signed deck',
  direction: 'you_owe', age_days: 9, times_surfaced: 0,
  surface_count_provenance: 'unknown_legacy', overdue: false,
  deadline: '2026-09-29', chased_count: 0, chased_out: false,
  suggestion: 'do it or dismiss',
};
const result = { coverage: 'owner_authored_only', stale_loops: [row],
  mute_suggestions: [], current: {}, counts: { stale: 1, mute_suggestions: 0 } };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

test('client calls only the authenticated receiver owner-retune route', async () => {
  let calls = 0;
  const fetcher: typeof fetch = async (input, init) => {
    calls++;
    assert.equal(String(input), 'http://127.0.0.1:8793/runtime/owner-retune');
    assert.equal(init?.method, 'POST');
    assert.equal(init?.redirect, 'error');
    assert.equal(init?.cache, 'no-store');
    assert.equal((init?.headers as Record<string, string>).authorization, 'Bearer read-only');
    assert.equal(init?.body, '{}');
    return response(result);
  };
  assert.deepEqual(await readOwnerRetune({ bridgeUrl, readBearer: 'read-only', fetch: fetcher }), result);
  assert.equal(calls, 1);
});

test('client rejects untrusted endpoints, bad coverage, preference leakage and count mismatch', async () => {
  for (const url of ['https://127.0.0.1:8793/mcp', 'http://example.test/mcp',
    'http://127.0.0.1:8793/other', 'http://127.0.0.1:8793/mcp?x=1']) {
    await assert.rejects(readOwnerRetune({ bridgeUrl: url, readBearer: 'read-only',
      fetch: async () => response(result) }), /configuration/);
  }
  await assert.rejects(readOwnerRetune({ bridgeUrl, readBearer: 'bad\r\ntoken' }), /configuration/);
  for (const body of [
    { ...result, coverage: 'all_sources' },
    { ...result, current: { timezone: 'America/Los_Angeles' } },
    { ...result, mute_suggestions: ['mute Alex'] },
    { ...result, stale_loops: [{ ...row, source: 'gmail' }] },
    { ...result, counts: { stale: 0, mute_suggestions: 0 } },
    { ...result, stale_loops: [{ ...row, what: '😀'.repeat(201) }] },
  ]) {
    await assert.rejects(readOwnerRetune({ bridgeUrl, readBearer: 'read-only',
      fetch: async () => response(body) }), /unavailable/);
  }
  for (const status of [201, 301, 401, 503]) {
    await assert.rejects(readOwnerRetune({ bridgeUrl, readBearer: 'read-only',
      fetch: async () => response(result, status) }), /unavailable/);
  }
});

test('read body is bounded, abortable, and does not expose a partial result', async () => {
  const oversized = new ReadableStream<Uint8Array>({ start(controller) {
    controller.enqueue(new Uint8Array(64 * 1024));
    controller.enqueue(new Uint8Array(1));
    controller.close();
  } });
  await assert.rejects(readOwnerRetune({ bridgeUrl, readBearer: 'read-only',
    fetch: async () => new Response(oversized) }), /unavailable/);
  const controller = new AbortController();
  let cancelled = 0;
  let entered!: () => void;
  const reading = new Promise<void>(resolve => { entered = resolve; });
  const pending = readOwnerRetune({ bridgeUrl, readBearer: 'read-only', fetch: async () =>
    new Response(new ReadableStream<Uint8Array>({
      start(stream) { stream.enqueue(new TextEncoder().encode('{')); },
      pull() { entered(); },
      cancel() { cancelled++; },
    })) }, { signal: controller.signal });
  await reading;
  controller.abort();
  await assert.rejects(pending, /unavailable/);
  assert.equal(cancelled, 1);
  const preAborted = new AbortController();
  preAborted.abort();
  let calls = 0;
  await assert.rejects(readOwnerRetune({ bridgeUrl, readBearer: 'read-only',
    fetch: async () => { calls++; return response(result); } }, { signal: preAborted.signal }), /unavailable/);
  assert.equal(calls, 0);
});

test('candidate tool is interactive only and labels partial owner-authored coverage', async () => {
  const tool = createOwnerRetuneTool({ bridgeUrl, readBearer: 'read-only', fetch: async () => response(result) });
  const execute = tool.execute!;
  const context = (mode: string, runId?: string, ownerRequestKey?: string) => ({
    requestContext: { get: (key: string) => ({ mode, runId, ownerRequestKey })[key as 'mode'] },
  });
  await assert.rejects(execute({}, context('unattended', 'r', 'e') as never), /unavailable/);
  await assert.rejects(execute({}, context('chat') as never), /unavailable/);
  const output = await execute({}, context('chat', 'r', 'e') as never) as {
    coverage: string; coverage_note: string; current: Record<string, unknown>;
  };
  assert.equal(output.coverage, 'owner_authored_only');
  assert.deepEqual(output.current, {});
  assert.match(output.coverage_note, /Empty results do not prove/);
});
