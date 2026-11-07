import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { once } from 'node:events';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import type { OwnerToolIdentity } from '../../src/tools/owner-contract.js';

/** Fixed managed receiver fixture; each caller supplies its lane-specific test flags. */
export async function startManagedReceiver(folder: string, laneEnv: Record<string, string>) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'),
    [resolve('test/owner-loop-receiver-child.py')], {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: {
        PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
        SOTTO_ROUTINE_EPOCH: 'e'.repeat(32),
        SOTTO_TEST_ROUTINE_SOURCE_REVISION: 'a'.repeat(64),
        SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
        SOTTO_DATA: folder, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'tenant', SOTTO_ACCOUNT_ID: 'account',
        SOTTO_LINE_KEY: 'line', SOTTO_MCP_TOKEN: '0123456789abcdef0123456789abcdef',
        PHOTON_HOME_CHANNEL: '+15555550123', PHOTON_ALLOWED_USERS: '+15555550123',
        PHOTON_PROJECT_ID: 'fixture-project', PHOTON_PROJECT_SECRET: 'fixture-secret',
        PHOTON_ALLOW_ALL_USERS: 'false', SOTTO_IMESSAGE_NUMBER: '+15555550999',
        SOTTO_TIMEZONE: 'America/Los_Angeles',
        ...laneEnv,
      },
    });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr = (stderr + String(chunk)).slice(-4096); });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const line = await Promise.race([new Promise<string>((done, fail) => {
      let output = '';
      child.stdout.on('data', chunk => {
        output += String(chunk);
        const i = output.indexOf('\n');
        if (i >= 0) done(output.slice(0, i));
      });
      child.once('exit', code => fail(new Error(`receiver exited ${code}: ${stderr}`)));
      child.once('error', fail);
    }), new Promise<never>((_, fail) => {
      timer = setTimeout(() => fail(new Error('receiver start timeout')), 15_000);
    })]);
    const parsed = JSON.parse(line) as { port: number; identity: OwnerToolIdentity; bearer: string; readBearer: string };
    return { child, ...parsed, providerThreadId: parsed.identity.route_id,
      bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp` };
  } catch (error) {
    child.kill('SIGKILL');
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

export async function stopManagedReceiver(child: ChildProcessWithoutNullStreams): Promise<void> {
  if (child.exitCode !== null) return;
  const ended = once(child, 'exit');
  child.stdin.end('quit\n');
  const timer = setTimeout(() => child.kill('SIGKILL'), 5_000);
  try { await ended; }
  finally { clearTimeout(timer); }
}
