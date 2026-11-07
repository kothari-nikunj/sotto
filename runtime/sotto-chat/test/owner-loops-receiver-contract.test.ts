import assert from 'node:assert/strict';
import { spawn, type ChildProcessByStdio } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Readable } from 'node:stream';
import test from 'node:test';
import { readOwnerLoops } from '../src/agent/owner-loops.js';
import { readOwnerRetune } from '../src/agent/owner-retune.js';

type FixtureProcess = ChildProcessByStdio<null, Readable, Readable>;
type ExitResult = { code: number | null; signal: NodeJS.Signals | null };
interface Ready { ready: true; url: string; readBearer: string; expectedVisibleSummary: string }
const testDir = dirname(fileURLToPath(import.meta.url));
const childScript = join(testDir, 'owner-loops-receiver-child.py');
const sourceRoot = process.env.SOTTO_FIXTURE_SOURCE_ROOT || resolve(testDir, '../../../');
const STARTUP_TIMEOUT_MS = 20_000;
const CLEANUP_TIMEOUT_MS = 20_000;
const MAX_OUTPUT_CHARS = 8 * 1024;

function waitForClose(closePromise: Promise<ExitResult>, timeoutMs: number): Promise<ExitResult> {
  return new Promise<ExitResult>((resolveClose, reject) => {
    const timer = setTimeout(() => reject(new Error('synthetic receiver child did not close')), timeoutMs);
    closePromise.then(result => { clearTimeout(timer); resolveClose(result); }, error => {
      clearTimeout(timer); reject(error);
    });
  });
}

function waitForReady(proc: FixtureProcess, dataRoot: string): Promise<Ready> {
  return new Promise<Ready>((resolveReady, reject) => {
    let stdout = '';
    let stderr = '';
    let exceeded = false;
    const capture = (current: string, chunk: string): string => {
      const remaining = MAX_OUTPUT_CHARS - stdout.length - stderr.length;
      if (chunk.length > remaining) exceeded = true;
      return current + chunk.slice(0, Math.max(0, remaining));
    };
    proc.stdout.setEncoding('utf8');
    proc.stderr.setEncoding('utf8');
    const onData = (chunk: string) => {
      stdout = capture(stdout, chunk);
      if (exceeded) finish(new Error('synthetic receiver output exceeded its bound'));
      const line = stdout.split('\n').slice(0, -1).find(value => value.startsWith('{"ready":'));
      if (!line) return;
      try {
        const ready = JSON.parse(line) as Ready;
        if (ready.ready !== true || !ready.url.startsWith('http://127.0.0.1:') || !ready.readBearer ||
            !ready.expectedVisibleSummary || !dataRoot) throw new Error();
        finish(undefined, ready);
      } catch { finish(new Error('synthetic receiver readiness record is invalid')); }
    };
    const onError = () => finish(new Error('synthetic receiver child failed to start'));
    const onClose = () => finish(new Error('synthetic receiver child closed before readiness'));
    const timer = setTimeout(() => finish(new Error('synthetic receiver startup timed out')), STARTUP_TIMEOUT_MS);
    const finish = (error?: Error, value?: Ready) => {
      clearTimeout(timer);
      proc.stdout.off('data', onData);
      proc.off('error', onError);
      proc.off('close', onClose);
      if (error) reject(error);
      else resolveReady(value!);
    };
    proc.stdout.on('data', onData);
    proc.stderr.on('data', (chunk: string) => { stderr = capture(stderr, chunk); });
    proc.once('error', onError);
    proc.once('close', onClose);
  });
}

test('real Python receiver and TS client share owner-only loopback contract', { timeout: 90_000 }, async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'sotto-owner-loops-receiver-'));
  const bundledPython = join(sourceRoot, '.venv', 'bin', 'python');
  const python = process.env.PYTHON || (existsSync(bundledPython) ? bundledPython : 'python3');
  const proc: FixtureProcess = spawn(python, ['-I', '-B', childScript, sourceRoot, dataRoot], {
    env: { PATH: process.env.PATH ?? '/usr/bin:/bin', LANG: 'C', HOME: join(dataRoot, 'launcher-home') },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const closePromise = new Promise<ExitResult>((resolveClose, reject) => {
    proc.once('close', (code: number | null, signal: NodeJS.Signals | null) => resolveClose({ code, signal }));
    proc.once('error', reject);
  });
  void closePromise.catch(() => undefined);
  try {
    const ready = await waitForReady(proc, dataRoot);
    const options = { bridgeUrl: ready.url, readBearer: ready.readBearer };
    const active = await readOwnerLoops(options, { include_parked: false });
    assert.equal(active.coverage, 'owner_authored_only');
    assert.deepEqual(active.counts, { you_owe: 1, waiting_on_them: 0 });
    assert.equal(active.you_owe[0]?.what, ready.expectedVisibleSummary);
    assert.match(active.you_owe[0]?.loop_version ?? '', /^[a-f0-9]{64}$/);
    assert.match(active.you_owe[0]?.loop_identity ?? '', /^[a-f0-9]{64}$/);
    const raw = await fetch(new URL('/runtime/owner-loops', ready.url), {method:'POST',
      headers:{authorization:`Bearer ${ready.readBearer}`,'content-type':'application/json'},
      body:JSON.stringify({include_parked:false})});
    const canonical = await raw.json();
    assert.deepEqual(active, canonical, 'actual Python projection survives the strict Node schema unchanged');
    for (const field of ['loop_version','loop_identity']) {
      const invalid = {...canonical,you_owe:[{...canonical.you_owe[0],[field]:'not-a-canonical-digest'}]};
      await assert.rejects(readOwnerLoops({...options,fetch:async()=>new Response(JSON.stringify(invalid))},
        {include_parked:false}), /unavailable/);
    }
    assert.equal('parked' in active, false);
    assert.ok(!JSON.stringify(active).includes('fixture-event-1'));
    assert.ok(!JSON.stringify(active).includes('Fixture unproved legacy obligation'));

    const retune = await readOwnerRetune(options);
    assert.equal(retune.coverage, 'owner_authored_only');
    assert.deepEqual(retune.counts, { stale: 1, mute_suggestions: 0 });
    assert.equal(retune.stale_loops[0]?.what, ready.expectedVisibleSummary);
    assert.deepEqual(retune.current, {});
    assert.deepEqual(retune.mute_suggestions, []);
    assert.ok(!JSON.stringify(retune).includes('Fixture unproved legacy obligation'));

    const parked = await readOwnerLoops(options, { include_parked: true });
    assert.deepEqual(parked.counts, { you_owe: 1, waiting_on_them: 0, parked: 0 });
    assert.deepEqual(parked.parked, []);
    await assert.rejects(readOwnerLoops({ ...options, readBearer: 'wrong-synthetic-bearer' },
      { include_parked: false }), error => !String(error).includes('wrong-synthetic-bearer'));
  } finally {
    let cleanupError: unknown;
    if (proc.pid !== undefined && proc.exitCode === null && proc.signalCode === null) {
      try {
        if (!proc.kill('SIGTERM')) throw new Error('synthetic receiver did not accept SIGTERM');
        const result = await waitForClose(closePromise, CLEANUP_TIMEOUT_MS);
        assert.deepEqual(result, { code: 0, signal: null });
      } catch (error) {
        cleanupError = error;
        if (proc.pid !== undefined && proc.exitCode === null && proc.signalCode === null) proc.kill('SIGKILL');
        await waitForClose(closePromise, CLEANUP_TIMEOUT_MS).catch(() => undefined);
      }
    }
    await rm(dataRoot, { recursive: true, force: true });
    if (cleanupError) throw cleanupError;
  }
});
