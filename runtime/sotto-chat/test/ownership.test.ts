import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { claimRuntimeDirectory } from '../src/ownership.js';

test('process-held ownership refuses a second connection and releases explicitly', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-ownership-'));
  let release = await claimRuntimeDirectory(directory);
  try {
    await assert.rejects(claimRuntimeDirectory(directory), /owned/);
    await release();
    await release();
    release = await claimRuntimeDirectory(directory);
  } finally { await release(); await rm(directory, { recursive: true, force: true }); }
});

test('kernel ownership recovers after SIGKILL without a TTL or PID assumption', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-crash-owner-'));
  const source = new URL('../src/ownership.ts', import.meta.url).href;
  const child = spawn(process.execPath, ['--import', 'tsx', '--input-type=module', '-e',
    `import { claimRuntimeDirectory } from ${JSON.stringify(source)};
     await claimRuntimeDirectory(process.argv[1]); process.stdout.write('ready\\n'); setInterval(()=>{},1000);`, directory],
  { stdio: ['ignore', 'pipe', 'pipe'] });
  try {
    await new Promise<void>((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('owner child did not start')), 10_000);
      child.stdout.once('data', () => { clearTimeout(timeout); resolve(); });
      child.once('exit', code => { clearTimeout(timeout); reject(new Error(`owner exited: ${code}`)); });
    });
    const old = JSON.parse(await readFile(join(directory, '.runtime-owner/owner.json'), 'utf8'));
    await assert.rejects(claimRuntimeDirectory(directory), /owned/);
    const exited = once(child, 'exit');
    child.kill('SIGKILL');
    await exited;
    const release = await claimRuntimeDirectory(directory);
    try {
      const current = JSON.parse(await readFile(join(directory, '.runtime-owner/owner.json'), 'utf8'));
      assert.notEqual(current.nonce, old.nonce);
      await assert.rejects(claimRuntimeDirectory(directory), /owned/);
    } finally { await release(); }
  } finally {
    if (child.exitCode === null && child.signalCode === null) { const exited = once(child, 'exit'); child.kill('SIGKILL'); await exited; }
    await rm(directory, { recursive: true, force: true });
  }
});

test('legacy and incomplete ownership markers remain fail-closed', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-legacy-owner-'));
  const marker = join(directory, '.runtime-owner');
  try {
    await mkdir(marker, { mode: 0o700 });
    await assert.rejects(claimRuntimeDirectory(directory), /ownership cannot/);
    await writeFile(join(marker, 'owner.json'), JSON.stringify({ pid: 99999999, nonce: 'legacy' }));
    await assert.rejects(claimRuntimeDirectory(directory), /ownership cannot/);
    assert.equal(JSON.parse(await readFile(join(marker, 'owner.json'), 'utf8')).nonce, 'legacy');
  } finally { await rm(directory, { recursive: true, force: true }); }
});
