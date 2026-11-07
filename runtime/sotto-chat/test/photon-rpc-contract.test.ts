import assert from 'node:assert/strict';
import { spawn, type ChildProcessByStdio } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Readable } from 'node:stream';
import test from 'node:test';

const childScript = fileURLToPath(new URL('./photon-rpc-contract-child.mjs', import.meta.url));
const outputLimit = 8 * 1024;
const timeoutMs = 20_000;

test('pinned public Photon SDK send RPC contract over a synthetic loopback gRPC server', async (t) => {
  const home = await mkdtemp(join(tmpdir(), 'photon-rpc-contract-home-'));
  t.after(async () => rm(home, { recursive: true, force: true }));

  const child = spawn(process.execPath, [childScript], {
    cwd: process.cwd(),
    env: {
      PATH: process.env.PATH ?? '',
      HOME: home,
      TMPDIR: tmpdir(),
      LANG: 'C',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  }) as ChildProcessByStdio<null, Readable, Readable>;
  let stdout = '';
  let stderr = '';
  let overflow = false;
  child.stdout.setEncoding('utf8').on('data', (chunk: string) => {
    if (stdout.length + chunk.length > outputLimit) overflow = true;
    else stdout += chunk;
  });
  child.stderr.setEncoding('utf8').on('data', (chunk: string) => {
    if (stderr.length + chunk.length > outputLimit) overflow = true;
    else stderr += chunk;
  });
  const closed = once(child, 'close') as Promise<[number | null, NodeJS.Signals | null]>;
  const timer = setTimeout(() => child.kill('SIGKILL'), timeoutMs);
  try {
    const [code, signal] = await closed;
    assert.equal(overflow, false, 'fixture output stayed bounded');
    assert.equal(signal, null, 'child shut down without a forced signal');
    assert.equal(code, 0, `fixture completed successfully; stderr=${stderr}`);
    const lines = stdout.trim().split('\n');
    assert.equal(lines.length, 1, 'fixture emits one bounded JSON result');
    assert.deepEqual(JSON.parse(lines[0]!), {
      ok: true,
      sdk: { spectrum: '12.14.0', advanced: '2.4.0', niceGrpc: '2.1.18', grpc: '1.14.6' },
      calls: { text: 2, multipart: 1, upload: 1 },
      successfulRootGuid: 'synthetic-root-message-guid',
      keysPresent: { text: true, multipart: true, upload: true },
      errorsRetryable: true,
      simulatedEffects: 3,
    });
  } finally {
    clearTimeout(timer);
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGKILL');
      await closed;
    }
  }
});
