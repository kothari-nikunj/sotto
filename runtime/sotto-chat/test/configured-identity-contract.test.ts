import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chmod, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createChatHttpServer, listenLoopback, type HttpServices } from '../src/http.js';
import { loadRuntimeConfig } from '../src/bootstrap/config.js';
import { channelIdentityTranscript, configuredChannelIdentityForConfig } from '../src/configured-identity.js';

const token = 'configured-channel-test-private-bearer-00000000';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const child = join(root, 'runtime/sotto-chat/test/configured-identity-python-child.py');
const services: HttpServices = {
  send: async () => ({ acceptance: 'not_attempted' }), sendGallery: async () => ({ acceptance: 'not_attempted' }),
  receipt: async () => ({}), run: async () => ({}), sessions: async () => ({}), health: async () => ({}),
};
function expected(overrides: Record<string, unknown> = {}) {
  return { version: 1, channel: 'telegram', stateDirectory: '/tmp/test-state', mediaRoot: '/tmp/test-media',
    bearerToken: token, port: 0, owner: { accountId: 'telegram-owner', ownerId: '123' }, lineKey: 'primary',
    target: 'owner-target', providerThreadId: 'telegram:123',
    credentials: { telegramToken: 'test-bot-token', telegramLinkFile: { bot_token: 'test-bot-token', allowed_user: 123 } },
    instructions: 'Fixture', modelProxy: { origin: 'https://proxy.example', bearer: 'proxy-secret',
      chatModelId: 'chat-model', nativeMediaModelId: 'media-model' }, ...overrides };
}
async function pythonVerify(url: string, config: unknown, bearer = token): Promise<boolean> {
  const proc = spawn('python3', ['-I', '-B', child, root], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH ?? '' } });
  const output: Buffer[] = []; const errors: Buffer[] = [];
  proc.stdout.on('data', chunk => output.push(chunk));
  proc.stderr.on('data', chunk => errors.push(chunk));
  proc.stdin.end(JSON.stringify({ url, token: bearer, expected: config }));
  const status = await new Promise<number | null>((resolveStatus, reject) => {
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; proc.kill('SIGKILL'); }, 15_000);
    proc.on('error', error => { clearTimeout(timer); reject(error); });
    proc.on('close', code => { clearTimeout(timer);
      if (timedOut) reject(new Error('Python verifier timed out'));
      else resolveStatus(code);
    });
  });
  assert.equal(status, 0, Buffer.concat(errors).toString('utf8'));
  const result = JSON.parse(Buffer.concat(output).toString('utf8')) as { matched: boolean };
  return result.matched;
}
async function fixture(config: ReturnType<typeof expected> | undefined) {
  const directory = await mkdtemp(join(tmpdir(), 'configured-channel-'));
  const configPath = join(directory, 'runtime.json');
  if (config) { await writeFile(configPath, JSON.stringify(config), { mode: 0o600 }); await chmod(configPath, 0o600); }
  const identity = config ? configuredChannelIdentityForConfig(await loadRuntimeConfig(configPath)) : undefined;
  const server = createChatHttpServer(services, { bearerToken: token, allowedTargets: new Set(['owner-target']),
    mediaRoot: directory, configuredChannelIdentity: identity });
  const port = await listenLoopback(server);
  const url = `http://127.0.0.1:${port}`;
  return { url, close: async () => { server.closeAllConnections(); await new Promise<void>((resolveClose, reject) =>
    server.close(error => error ? reject(error) : resolveClose())); await rm(directory, { recursive: true, force: true }); } };
}
function request(url: string, body: unknown = {}, bearer = token, method = 'POST') {
  return fetch(`${url}/v1/configured-identity`, { method, headers: { authorization: `Bearer ${bearer}`,
    'content-type': 'application/json' }, body: method === 'GET' ? undefined : JSON.stringify(body), redirect: 'error' });
}

test('configured identity is private, bounded, redacted, and unavailable to legacy constructors', async () => {
  const noConfig = await fixture(undefined);
  try {
    assert.equal((await request(noConfig.url)).status, 501);
    assert.equal(await pythonVerify(noConfig.url, expected()), false);
  } finally { await noConfig.close(); }
  const config = expected(); const f = await fixture(config);
  try {
    assert.equal((await request(f.url, {}, 'wrong-token')).status, 401);
    assert.equal((await request(f.url, {}, token, 'GET')).status, 405);
    assert.equal((await request(f.url, { ignored: true })).status, 400);
    const response = await request(f.url);
    assert.equal(response.status, 200);
    const raw = await response.text();
    assert.equal(Buffer.byteLength(raw) < 8192, true);
    for (const secret of [config.credentials.telegramToken, token, config.modelProxy.bearer]) {
      assert.equal(raw.includes(secret), false);
    }
    assert.equal(await pythonVerify(f.url, config), true);
    for (const mutation of [
      { owner: { ...config.owner, ownerId: '124' } }, { owner: { ...config.owner, accountId: 'other' } },
      { lineKey: 'secondary' }, { target: 'other-target' }, { providerThreadId: 'telegram:other' },
      { credentials: { ...config.credentials, telegramToken: 'other-bot-token' } },
      { credentials: { ...config.credentials, telegramLinkFile: { ...config.credentials.telegramLinkFile, bot_token: 'other-link-bot' } } },
      { credentials: { ...config.credentials, telegramLinkFile: { ...config.credentials.telegramLinkFile, allowed_user: 124 } } },
    ]) assert.equal(await pythonVerify(f.url, { ...config, ...mutation }), false);
    assert.equal(await pythonVerify(f.url, config, 'other-private-bearer-0000000000000000'), false);
    assert.equal(await pythonVerify(f.url, { ...config, bearerToken: 'other-private-bearer-0000000000000000' }), false);
  } finally { await f.close(); }
});

test('first-contact physical line and Photon Cloud credentials bind the static proof', async () => {
  const config = expected({ channel: 'photon', tenantId: 'tenant-1',
    owner: { accountId: 'photon-account', ownerId: '+15555550123' },
    providerThreadId: undefined, firstContact: { mode: 'owner_dm', expectedProviderLine: '+15555550999' },
    credentials: { mode: 'cloud', photonProjectId: 'photon-project-id', photonProjectSecret: 'photon-project-secret' },
  });
  const f = await fixture(config);
  try {
    const raw = await (await request(f.url)).text();
    for (const secret of ['photon-project-id', 'photon-project-secret', token]) assert.equal(raw.includes(secret), false);
    assert.equal(await pythonVerify(f.url, config), true);
    for (const mutation of [
      { firstContact: { mode: 'owner_dm', expectedProviderLine: '+15555550888' } },
      { tenantId: 'tenant-2' },
      { credentials: { mode: 'cloud', photonProjectId: 'other-project', photonProjectSecret: 'photon-project-secret' } },
      { credentials: { mode: 'cloud', photonProjectId: 'photon-project-id', photonProjectSecret: 'other-secret' } },
      { firstContact: undefined, providerThreadId: 'imessage:other' },
    ]) assert.equal(await pythonVerify(f.url, { ...config, ...mutation }), false);
  } finally { await f.close(); }
});

test('fixed Photon self-host credentials and Unicode use the same cross-language transcript', async () => {
  const unicodeBearer = 'clé-configured-channel-private-bearer-0000000';
  const config = expected({ channel: 'photon', bearerToken: unicodeBearer,
    owner: { accountId: 'photon-café', ownerId: '+15555550123' }, lineKey: 'línea',
    target: 'owner-café', providerThreadId: 'imessage:café;phone=primary',
    credentials: { mode: 'self_host', photonServerUrl: 'https://photon.example', photonApiKey: 'clé-secret' },
  });
  const directory = await mkdtemp(join(tmpdir(), 'configured-unicode-'));
  const configPath = join(directory, 'runtime.json');
  await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });
  const identity = configuredChannelIdentityForConfig(await loadRuntimeConfig(configPath));
  const server = createChatHttpServer(services, { bearerToken: unicodeBearer,
    allowedTargets: new Set(['owner-café']), mediaRoot: directory, configuredChannelIdentity: identity });
  const url = `http://127.0.0.1:${await listenLoopback(server)}`;
  try {
    assert.equal(await pythonVerify(url, config, unicodeBearer), true);
    assert.equal(await pythonVerify(url, { ...config, credentials: { ...config.credentials, photonApiKey: 'other-key' } }, unicodeBearer), false);
    assert.equal(await pythonVerify(url, { ...config, providerThreadId: 'imessage:other;phone=primary' }, unicodeBearer), false);
    assert.throws(() => channelIdentityTranscript(['bad\ud800']), /invalid/);
    assert.equal(await pythonVerify(url, { ...config, lineKey: 'bad\ud800' }, unicodeBearer), false);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolveClose, reject) => server.close(error => error ? reject(error) : resolveClose()));
    await rm(directory, { recursive: true, force: true });
  }
});


test('shared-project identity HMAC binds explicit route mode without a physical line', async () => {
  const config = expected({channel: 'photon', tenantId: 'tenant-1',
    owner: {accountId: 'photon-account', ownerId: '+15555550123'},
    providerThreadId: undefined, firstContact: {mode: 'owner_dm_shared_project'},
    credentials: {mode: 'cloud', photonProjectId: 'photon-project-id', photonProjectSecret: 'photon-project-secret'},
    target: 'photon'});
  const setup = await fixture(config);
  try {
    const wire = await (await request(setup.url)).json();
    assert.deepEqual(wire.identity.route, {mode: 'first_contact_shared_project'});
    assert.equal(await pythonVerify(setup.url, config), true);
    assert.equal(await pythonVerify(setup.url, {...config, firstContact: {mode: 'owner_dm', expectedProviderLine: '+15555550999'}}), false);
  } finally { await setup.close(); }
});
