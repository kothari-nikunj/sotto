import assert from 'node:assert/strict';
import { spawn, type ChildProcessByStdio } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { Readable } from 'node:stream';
import test from 'node:test';

type ProbeProcess = ChildProcessByStdio<null, Readable, Readable>;
type ExitResult = { code: number | null; signal: NodeJS.Signals | null };

const childScript = join(dirname(fileURLToPath(import.meta.url)), 'spectrum-signal-child.mjs');
const STARTUP_TIMEOUT_MS = 20_000;
const EXIT_TIMEOUT_MS = 20_000;
const CLEANUP_TIMEOUT_MS = 2_000;
const MAX_CAPTURED_OUTPUT_CHARS = 8 * 1024;

function waitForText(proc: ProbeProcess, getOutput: () => string, marker: string, timeoutMs: number,
  hasExceeded: () => boolean): Promise<void> {
  if (hasExceeded()) return Promise.reject(new Error('Spectrum probe output exceeded its bound'));
  if (getOutput().includes(marker)) return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const onData = () => {
      if (hasExceeded()) { cleanup(); reject(new Error('Spectrum probe output exceeded its bound')); }
      else if (getOutput().includes(marker)) { cleanup(); resolve(); }
    };
    const onClose = (code: number | null, signal: NodeJS.Signals | null) => {
      cleanup(); reject(new Error(`child closed before ${marker}: ${code}/${signal}`));
    };
    const onError = (error: Error) => { cleanup(); reject(error); };
    const timer = setTimeout(() => { cleanup(); reject(new Error(`timed out waiting for ${marker}`)); }, timeoutMs);
    const cleanup = () => {
      clearTimeout(timer);
      proc.stdout.off('data', onData);
      proc.off('close', onClose);
      proc.off('error', onError);
    };
    proc.stdout.on('data', onData);
    proc.on('close', onClose);
    proc.on('error', onError);
  });
}

function waitForClose(closePromise: Promise<ExitResult>, timeoutMs: number): Promise<ExitResult> {
  return new Promise<ExitResult>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Spectrum child did not close after SIGTERM')), timeoutMs);
    closePromise.then(result => { clearTimeout(timer); resolve(result); }, error => { clearTimeout(timer); reject(error); });
  });
}

test('pinned Spectrum leaves SIGTERM ownership to application receipt settlement', { timeout: 45_000 }, async () => {
  const home = await mkdtemp(join(tmpdir(), 'spectrum-signal-home-'));
  const proc: ProbeProcess = spawn(process.execPath, [childScript], {
    cwd: dirname(dirname(childScript)),
    env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: home, NODE_ENV: 'test' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const closePromise = new Promise<ExitResult>((resolve, reject) => {
    proc.once('close', (code: number | null, signal: NodeJS.Signals | null) => resolve({ code, signal }));
    proc.once('error', reject);
  });
  // Observe an early spawn failure while the readiness waiter owns reporting.
  void closePromise.catch(() => undefined);
  let output = '';
  let errors = '';
  let outputExceeded = false;
  const capture = (current: string, chunk: string): string => {
    const remaining = MAX_CAPTURED_OUTPUT_CHARS - output.length - errors.length;
    if (chunk.length > remaining) outputExceeded = true;
    return current + chunk.slice(0, Math.max(0, remaining));
  };
  proc.stdout.setEncoding('utf8'); proc.stderr.setEncoding('utf8');
  proc.stdout.on('data', (chunk: string) => { output = capture(output, chunk); });
  proc.stderr.on('data', (chunk: string) => { errors = capture(errors, chunk); });
  try {
    await waitForText(proc, () => output, 'READY LISTENERS=1', STARTUP_TIMEOUT_MS, () => outputExceeded);
    assert.equal(proc.kill('SIGTERM'), true, 'child accepted SIGTERM');
    const result = await waitForClose(closePromise, EXIT_TIMEOUT_MS);
    assert.deepEqual(result, { code: 0, signal: null });
    assert.equal(outputExceeded, false, 'probe output stays bounded');
    assert.ok(output.includes('LATE_HANDLER_ENTERED'), `later handler ran; output=${output}; stderr=${errors}`);
    assert.ok(output.includes('LATE_APP_STOP_RETURNED'), `public app.stop() returned after SDK shutdown began; output=${output}`);
    assert.equal(output.includes('LATE_HANDLER_SETTLED'), true,
      `application cleanup completed before process exit; output=${output}`);
  } finally {
    if (proc.exitCode === null && proc.signalCode === null) {
      proc.kill('SIGKILL');
      await waitForClose(closePromise, CLEANUP_TIMEOUT_MS).catch(() => undefined);
    }
    await rm(home, { recursive: true, force: true });
  }
});
