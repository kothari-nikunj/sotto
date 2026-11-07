import assert from 'node:assert/strict';
import { execFileSync, spawn, type ChildProcessByStdio } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Readable } from 'node:stream';
import test from 'node:test';

const childScript = fileURLToPath(new URL('./photon-adapter-contract-child.mjs', import.meta.url));
const outputLimit = 8 * 1024;
const timeoutMs = 20_000;

test('installed Photon adapter sends through Spectrum over trusted loopback TLS', async (t) => {
  const home = await mkdtemp(join(tmpdir(), 'photon-adapter-contract-home-'));
  t.after(async () => rm(home, { recursive: true, force: true }));

  const cert = join(home, 'loopback.pem');
  const key = join(home, 'loopback.key');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes',
    '-keyout', key, '-out', cert, '-days', '1', '-subj', '/CN=localhost',
    '-addext', 'subjectAltName=DNS:localhost'], { stdio: 'ignore' });

  const child = spawn(process.execPath, [childScript], {
    cwd: process.cwd(),
    env: {
      PATH: process.env.PATH ?? '',
      HOME: home,
      TMPDIR: tmpdir(),
      LANG: 'C',
      FIXTURE_CERT: cert,
      FIXTURE_KEY: key,
      GRPC_DEFAULT_SSL_ROOTS_FILE_PATH: cert,
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
    const lines = stdout.trim().split('\n').filter(line => line.startsWith('CONTRACT_RESULT='));
    assert.equal(lines.length, 1, 'fixture emits one bounded JSON result');
    assert.deepEqual(JSON.parse(lines[0]!.slice('CONTRACT_RESULT='.length)), {
      ok: true,
      adapterCalls: 2,
      ambiguousEffects: 1,
      rootGuid: 'synthetic-adapter-root-guid',
    });
  } finally {
    clearTimeout(timer);
    if (child.exitCode === null && child.signalCode === null) {
      child.kill('SIGKILL');
      await closed;
    }
  }
});
