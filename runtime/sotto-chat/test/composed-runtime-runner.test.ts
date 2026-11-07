import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const testDirectory = dirname(fileURLToPath(import.meta.url));
const sourceRoot = resolve(testDirectory, '../../..');
const pythonFixture = join(testDirectory, 'fixtures/composed-runtime/test_run_script.py');
const bundledPython = join(sourceRoot, '.venv', 'bin', 'python');
const python = process.env.PYTHON || (existsSync(bundledPython) ? bundledPython : 'python3');
const outputLimit = 8 * 1024;

test('composed fixture runner resolves paths in exported no-Git trees', { timeout: 20_000 }, async () => {
  const child = spawn(python, ['-I', '-B', pythonFixture], {
    cwd: sourceRoot,
    env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: '/tmp', LANG: 'C' },
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  let outputBytes = 0;
  let exceeded = false;
  let stoppedCapture = false;
  const stopChildGroup = () => {
    if (child.pid === undefined) return;
    try { process.kill(-child.pid, 'SIGKILL'); } catch { /* already exited */ }
  };
  const capture = (chunks: Buffer[], chunk: Buffer) => {
    if (stoppedCapture) return;
    const remaining = outputLimit - outputBytes;
    const keepLength = Math.min(chunk.byteLength, Math.max(0, remaining));
    if (keepLength > 0) {
      chunks.push(Buffer.from(chunk.subarray(0, keepLength)));
      outputBytes += keepLength;
    }
    if (keepLength < chunk.byteLength) {
      exceeded = true;
      stoppedCapture = true;
      stopChildGroup();
    }
  };
  child.stdout.on('data', (chunk: Buffer) => capture(stdout, chunk));
  child.stderr.on('data', (chunk: Buffer) => capture(stderr, chunk));
  let timedOut = false;
  const close = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolveClose, reject) => {
    child.once('error', reject);
    child.once('close', (code, signal) => resolveClose({ code, signal }));
  });
  const timer = setTimeout(() => {
    timedOut = true;
    stopChildGroup();
  }, 15_000);
  let result: { code: number | null; signal: NodeJS.Signals | null };
  try { result = await close; } finally { clearTimeout(timer); }
  const out = Buffer.concat(stdout).toString('utf8');
  const err = Buffer.concat(stderr).toString('utf8');
  assert.equal(timedOut, false, 'fake-Docker path regression timed out');
  assert.equal(exceeded, false, 'fake-Docker path regression exceeded bounded output');
  assert.deepEqual(result, { code: 0, signal: null }, `${out}${err}`.slice(-outputLimit));
});
