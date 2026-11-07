import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { startApplication, type ApplicationOptions } from '../src/application.js';
import { photonProjectFingerprint } from '../src/activation.js';
import { loadRuntimeConfig } from '../src/bootstrap/config.js';
import { configuredChannelIdentityForConfig } from '../src/configured-identity.js';
import { readBriefDetail } from '../src/agent/brief-detail.js';
import { routeDigest } from '../src/route-proof.js';

const sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const child = join(sourceRoot, 'runtime/sotto-chat/test/brief-gallery-chain-child.py');
const python = process.env.PYTHON || (existsSync(join(sourceRoot, '.venv/bin/python'))
  ? join(sourceRoot, '.venv/bin/python') : 'python3');

test('saved brief uses receiver-private PNG bytes, actual Node acceptance and current route to permit typed read',
  { timeout: 90_000 }, async () => {
    const directory = await mkdtemp(join(tmpdir(), 'sotto-brief-gallery-chain-'));
    const data = join(directory, 'receiver');
    const owner = '+15555550123';
    const line = '+15555550999';
    const thread = `imessage:iMessage;-;${owner}~${line}`;
    const project = 'offline-brief-gallery-project';
    const raw = { version: 1, channel: 'photon', tenantId: 'tenant-fixture',
      stateDirectory: join(directory, 'node'), mediaRoot: directory,
      bearerToken: 'offline-brief-gallery-runtime-bearer-000000000000', port: 0,
      owner: { accountId: 'account-fixture', ownerId: owner }, lineKey: 'line-fixture',
      target: 'photon', providerThreadId: thread,
      credentials: { mode: 'cloud', photonProjectId: project, photonProjectSecret: 'synthetic-secret' },
      instructions: 'Offline fixture only', modelProxy: { origin: 'https://proxy.example',
        bearer: 'synthetic-proxy-secret', chatModelId: 'fake', nativeMediaModelId: 'fake' } };
    const configPath = join(directory, 'runtime.json');
    await writeFile(configPath, JSON.stringify(raw), { mode: 0o600 });
    const config = await loadRuntimeConfig(configPath);
    const options: ApplicationOptions = { directory: config.stateDirectory, mediaRoot: config.mediaRoot,
      bearerToken: config.bearerToken, owner: { ...config.owner, channel: 'photon' },
      lineKey: config.lineKey, target: config.target, providerThreadId: config.providerThreadId,
      configuredChannelIdentity: configuredChannelIdentityForConfig(config),
      activation: { tenant_id: raw.tenantId, project_fingerprint: photonProjectFingerprint(project),
        owner_id: owner, account_id: raw.owner.accountId, line_key: raw.lineKey,
        provider_thread_id: thread } };
    const media: Array<{ threadId: string; summary: string; hashes: string[] }> = [];
    const dependencies = { agent: { generateChat: async () => ({ text: '' }),
      generateUnattended: async () => ({ text: '' }), rotateThread: async () => {},
      purgeThreadsBefore: async () => 0 },
    transport: { send: async (request: { threadId: string; text?: string; media?: Array<{ data?: Buffer }> }) => {
      if (request.media) media.push({ threadId: request.threadId, summary: request.text!,
        hashes: request.media.map(item => createHash('sha256').update(item.data!).digest('hex')) });
      return { acceptance: 'accepted' as const, providerMessageIds: ['offline-parent-guid'] };
    } } };
    let app: Awaited<ReturnType<typeof startApplication>> | undefined;
    let proc: ReturnType<typeof spawn> | undefined;
    let closed: Promise<number | null> | undefined;
    try {
      app = await startApplication(options, dependencies);
      const admission = await app.receive({ channel: 'photon', eventId: 'owner-fixture-dm',
        accountId: raw.owner.accountId, senderId: owner, isDirect: true, text: 'Hello' },
      { threadId: thread });
      assert.equal(admission.status, 'silent');
      await writeFile(configPath, JSON.stringify({ ...raw, port: app.port }), { mode: 0o600 });
      proc = spawn(python, ['-I', '-B', child, sourceRoot, data, configPath], {
        env: { PATH: process.env.PATH ?? '/usr/bin:/bin', LANG: 'C.UTF-8', HOME: directory },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      closed = new Promise<number | null>((resolveClose, reject) => {
        proc!.once('close', code => resolveClose(code)); proc!.once('error', reject);
      });
      let stderr = '';
      proc.stderr!.setEncoding('utf8');
      proc.stderr!.on('data', chunk => { stderr = (stderr + String(chunk)).slice(-8192); });
      let stdout = '';
      proc.stdout!.setEncoding('utf8');
      const ready = await new Promise<{ url: string; bearer: string; artifact: string;
        route_digest: string; payload_hash: string; image_hashes: string[]; revision: string }>((resolveReady, reject) => {
        const timer = setTimeout(() => reject(new Error(`brief chain child timed out: ${stderr}`)), 40_000);
        proc!.stdout!.on('data', chunk => {
          stdout += String(chunk);
          if (stdout.length > 8192) { clearTimeout(timer); reject(new Error('child output exceeded bound')); return; }
          const line = stdout.split('\n').find(value => value.startsWith('{"ready":'));
          if (!line) return;
          try { clearTimeout(timer); resolveReady(JSON.parse(line)); }
          catch { clearTimeout(timer); reject(new Error('invalid child readiness')); }
        });
        proc!.once('close', () => { clearTimeout(timer); reject(new Error(`brief child exited: ${stderr}`)); });
      });
      assert.match(ready.route_digest, /^[a-f0-9]{64}$/);
      assert.match(ready.payload_hash, /^[a-f0-9]{64}$/);
      assert.equal(media.length, 1);
      assert.equal(media[0].threadId, thread);
      assert.deepEqual(media[0].hashes, ready.image_hashes);
      assert.equal(ready.route_digest, routeDigest({ channel: 'photon', tenantId: raw.tenantId,
        projectFingerprint: photonProjectFingerprint(project), ownerId: owner,
        accountId: raw.owner.accountId, lineKey: raw.lineKey }, thread));
      assert.equal(ready.payload_hash, createHash('sha256').update(JSON.stringify({
        summary: media[0].summary, images: media[0].hashes,
      })).digest('hex'));
      const sourceAuthority = { read: async () => ({ version: 2 as const, complete: true,
        revision: ready.revision, google_sources: [], bridge_sources: [] }) };
      const tool = { bridgeUrl: ready.url, readBearer: ready.bearer, sourceAuthority };
      const result = await readBriefDetail(tool, { latest: true, offset: 0, limit: 80 }, ready.revision);
      assert.equal(result.status, 'found');
      if (result.status !== 'found') throw new Error('accepted brief withheld');
      assert.equal(result.id, ready.artifact);
      assert.match(result.text, /Good morning/);
      assert.equal(JSON.stringify(result).includes(ready.payload_hash), false);
      await writeFile(join(directory, 'revoked'), 'off');
      assert.deepEqual(await readBriefDetail(tool, { latest: true, offset: 0, limit: 80 }, ready.revision),
        { status: 'withheld', coverage: 'accepted_artifacts_only' });
    } finally {
      if (proc && closed && proc.exitCode === null && proc.signalCode === null) {
        proc.kill('SIGTERM');
        const timeout = setTimeout(() => proc!.kill('SIGKILL'), 5_000);
        try { assert.equal(await closed, 0); } finally { clearTimeout(timeout); }
      }
      await app?.close();
      await rm(directory, { recursive: true, force: true });
    }
  });
