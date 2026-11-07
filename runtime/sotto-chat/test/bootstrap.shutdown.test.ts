import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

test('supervisor SIGUSR2 awaits provider cleanup despite foreign termination handlers', async () => {
  const child = spawn(process.execPath, ['--import', 'tsx', path.join(here, 'fixtures/shutdown-signal-child.mjs')], {
    cwd: path.dirname(here), stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stdout = '';
  let stderr = '';
  let signalled = false;
  // Cold tsx/provider imports are startup work, not time spent shutting down.
  let timer = setTimeout(() => { child.kill('SIGKILL'); }, 20_000);
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk: string) => {
    stdout += chunk;
    if (!signalled && stdout.includes('READY\n')) {
      signalled = true;
      clearTimeout(timer);
      timer = setTimeout(() => { child.kill('SIGKILL'); }, 5_000);
      child.kill('SIGUSR2');
    }
  });
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk: string) => { stderr += chunk; });
  try {
    const result = await new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
      child.once('error', reject);
      child.once('exit', (code, signal) => resolve({ code, signal }));
    });
    assert.equal(signalled, true, stderr);
    assert.deepEqual(result, { code: 0, signal: null }, `${stdout}\n${stderr}`);
    assert.match(stdout, /READY\nDISCONNECT_STARTED\nDISCONNECT_SETTLED\nSHUTDOWN_SETTLED\n/);
    assert.doesNotMatch(stdout, /FOREIGN_SIGTERM/);
    assert.ok(stdout.indexOf('FOREIGN_EXIT_HOOK') > stdout.indexOf('SHUTDOWN_SETTLED'), stdout);
  } finally {
    clearTimeout(timer);
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
  }
});
