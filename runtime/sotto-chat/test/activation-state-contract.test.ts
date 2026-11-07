import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { chmod, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { startApplication, type ApplicationOptions } from '../src/application.js';
import { photonProjectFingerprint } from '../src/activation.js';
import { loadRuntimeConfig } from '../src/bootstrap/config.js';
import { configuredChannelIdentityForConfig } from '../src/configured-identity.js';
import { OperationalJournal } from '../src/journal/index.js';

const sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const child = join(sourceRoot, 'runtime/sotto-chat/test/activation-state-python-child.py');
const python = process.env.PYTHON || (existsSync(join(sourceRoot, '.venv/bin/python'))
  ? join(sourceRoot, '.venv/bin/python') : 'python3');
const bearer = 'node-python-activation-state-private-bearer-000000';
const owner = '+15555550123';
const physicalLine = '+15555550999';
const thread = `imessage:iMessage;-;${owner}~${physicalLine}`;
const project = 'contract-project';

function expected(directory: string, mode: 'fixed' | 'first_contact') {
  return { version: 1, channel: 'photon', tenantId: 'tenant',
    stateDirectory: join(directory, 'state'), mediaRoot: directory, bearerToken: bearer, port: 0,
    owner: { accountId: 'account', ownerId: owner }, lineKey: 'line', target: 'photon',
    providerThreadId: mode === 'fixed' ? thread : undefined,
    firstContact: mode === 'first_contact' ? { mode: 'owner_dm', expectedProviderLine: physicalLine } : undefined,
    credentials: { mode: 'cloud', photonProjectId: project, photonProjectSecret: 'synthetic-project-secret' },
    instructions: 'Offline test only', modelProxy: { origin: 'https://proxy.example', bearer: 'synthetic-proxy-secret',
      chatModelId: 'chat-model', nativeMediaModelId: 'media-model' },
  };
}

async function pythonVerify(url: string, expectedConfig: unknown, token = bearer) {
  const processChild = spawn(python, ['-I', '-B', child, sourceRoot], {
    stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH ?? '' },
  });
  const output: Buffer[] = []; const errors: Buffer[] = [];
  let outputBytes = 0; let errorBytes = 0; let excessive = false;
  processChild.stdout.on('data', (chunk: Buffer) => {
    outputBytes += chunk.length;
    if (outputBytes > 4096) { excessive = true; processChild.kill('SIGKILL'); }
    else output.push(chunk);
  });
  processChild.stderr.on('data', (chunk: Buffer) => {
    errorBytes += chunk.length;
    if (errorBytes > 4096) { excessive = true; processChild.kill('SIGKILL'); }
    else errors.push(chunk);
  });
  processChild.stdin.end(JSON.stringify({ url, token, expected: expectedConfig }));
  const status = await new Promise<number | null>((done, reject) => {
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; processChild.kill('SIGKILL'); }, 15_000);
    processChild.once('error', error => { clearTimeout(timer); reject(error); });
    processChild.once('close', code => { clearTimeout(timer);
      if (timedOut) reject(new Error('Python activation verifier timed out'));
      else done(code);
    });
  });
  assert.equal(excessive, false, 'Python verifier output exceeded 4 KiB');
  assert.equal(status, 0, Buffer.concat(errors).toString('utf8'));
  return JSON.parse(Buffer.concat(output).toString('utf8')) as {
    matched: boolean; status?: string; mode?: string; event_id?: string | null; thread?: string | null;
  };
}

for (const mode of ['fixed', 'first_contact'] as const) {
  test(`real Node and Python contract: ${mode} admitted binding and ACK survive restart and raw-event pruning`, async () => {
    const directory = await mkdtemp(join(tmpdir(), `sotto-activation-cross-${mode}-`));
    const raw = expected(directory, mode);
    const configFile = join(directory, 'runtime.json');
    await writeFile(configFile, JSON.stringify(raw), { mode: 0o600 });
    await chmod(configFile, 0o600);
    const config = await loadRuntimeConfig(configFile);
    const base = { tenant_id: 'tenant', project_fingerprint: photonProjectFingerprint(project),
      owner_id: owner, account_id: 'account', line_key: 'line' };
    const options: ApplicationOptions = { directory: config.stateDirectory, mediaRoot: config.mediaRoot,
      bearerToken: config.bearerToken, owner: { ...config.owner, channel: 'photon' },
      lineKey: config.lineKey, target: config.target,
      providerThreadId: config.providerThreadId,
      configuredChannelIdentity: configuredChannelIdentityForConfig(config),
      ...(mode === 'fixed' ? { activation: { ...base, provider_thread_id: thread } } :
        { discoverySeed: { ...base, expected_provider_line: physicalLine } }) };
    const dependencies = { agent: { generateChat: async () => ({ text: '' }),
      generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0 },
    transport: { send: async () => ({ acceptance: 'not_attempted' as const, providerMessageIds: [] }) } };
    let app: Awaited<ReturnType<typeof startApplication>> | undefined;
    try {
      app = await startApplication(options, dependencies);
      let url = `http://127.0.0.1:${app.port}`;
      assert.deepEqual(await pythonVerify(url, raw), { matched: true, status: 'absent', mode,
        event_id: null, thread: null });
      assert.deepEqual(await pythonVerify(url, raw, 'wrong-private-bearer-0000000000000000'), { matched: false });
      assert.equal((await app.receive({ channel: 'photon', eventId: 'owner-message', accountId: 'account',
        senderId: owner, isDirect: true, text: 'Hello' }, { threadId: thread })).status, 'silent');
      const pending = await pythonVerify(url, raw);
      assert.equal(pending.matched, true);
      assert.equal(pending.status, 'pending');
      assert.equal(pending.thread, thread);
      assert.match(pending.event_id!, /^[a-f0-9]{64}$/);
      const ack = await fetch(`${url}/v1/activation-ack`, { method: 'POST', redirect: 'error',
        headers: { authorization: `Bearer ${bearer}`, 'content-type': 'application/json' },
        body: JSON.stringify({ event_id: pending.event_id }) });
      assert.equal(ack.status, 200);
      assert.deepEqual(await ack.json(), { acknowledged: true });
      assert.equal((await pythonVerify(url, raw)).status, 'acknowledged');
      const exposed = await fetch(`${url}/v1/activation-state`, { method: 'POST', redirect: 'error',
        headers: { authorization: `Bearer ${bearer}`, 'content-type': 'application/json' }, body: '{}' });
      const responseText = await exposed.text();
      assert.ok(Buffer.byteLength(responseText) < 8192);
      for (const secret of [bearer, raw.credentials.photonProjectSecret, raw.modelProxy.bearer]) {
        assert.equal(responseText.includes(secret), false);
      }
      await app.close(); app = undefined;
      const journal = new OperationalJournal(join(config.stateDirectory, 'operations.sqlite'));
      try {
        if (options.discoverySeed) journal.configureActivationDiscovery(options.discoverySeed);
        else journal.configureActivationIdentity(options.activation);
        const cutoff = Date.now() + 10_000;
        journal.pruneTerminal(cutoff, cutoff);
        assert.equal(journal.getInbound(pending.event_id!), undefined);
      } finally { journal.close(); }
      app = await startApplication(options, dependencies);
      url = `http://127.0.0.1:${app.port}`;
      assert.deepEqual(await pythonVerify(url, raw), { matched: true, status: 'acknowledged', mode,
        event_id: pending.event_id, thread });
      for (const changed of [
        { owner: { ...raw.owner, ownerId: '+15555550124' } },
        { owner: { ...raw.owner, accountId: 'other-account' } },
        { lineKey: 'other-line' },
        { credentials: { ...raw.credentials, photonProjectId: 'other-project' } },
        { credentials: { ...raw.credentials, photonProjectSecret: 'other-secret' } },
        mode === 'fixed' ? { providerThreadId: `imessage:iMessage;-;${owner}~+15555550888` } :
          { firstContact: { mode: 'owner_dm', expectedProviderLine: '+15555550888' } },
      ]) assert.deepEqual(await pythonVerify(url, { ...raw, ...changed }), { matched: false });
    } finally { await app?.close(); await rm(directory, { recursive: true, force: true }); }
  });
}
