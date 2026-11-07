import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const run = promisify(execFile);
const child = fileURLToPath(new URL('./telegram-polling-child.mjs', import.meta.url));

test('pinned Telegram polling commits admission or retry state before offset advancement and survives restart',
  { timeout: 70_000 }, async () => {
    const home = await mkdtemp(join(tmpdir(), 'telegram-polling-home-'));
    try {
      const result = await run(process.execPath, ['--import', 'tsx', child], {
        cwd: dirname(dirname(child)),
        env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: home, TMPDIR: home,
          NODE_ENV: 'test', MASTRA_TELEMETRY_DISABLED: '1', GOMAXPROCS: '1' },
        timeout: 60_000, killSignal: 'SIGKILL', maxBuffer: 8 * 1024,
      });
      assert.match(result.stdout, /PASS pinned Telegram polling:/);
    } finally {
      await rm(home, { recursive: true, force: true });
    }
  });

test('a Telegram owner edit is denied and acknowledged instead of becoming a permanently retried conflict',
  { timeout: 70_000 }, async () => {
    const home = await mkdtemp(join(tmpdir(), 'telegram-edit-home-'));
    try {
      const result = await run(process.execPath, ['--import', 'tsx', fileURLToPath(new URL('./telegram-edit-child.mjs', import.meta.url))], {
        cwd: dirname(dirname(child)),
        env: { PATH: process.env.PATH ?? '/usr/bin:/bin', HOME: home, TMPDIR: home,
          NODE_ENV: 'test', MASTRA_TELEMETRY_DISABLED: '1', GOMAXPROCS: '1' },
        timeout: 60_000, killSignal: 'SIGKILL', maxBuffer: 64 * 1024,
      });
      assert.match(result.stdout, /receives=1 admissionErrors=0 admitted=1 pending=\[\]/);
    } finally {
      await rm(home, { recursive: true, force: true });
    }
  });
