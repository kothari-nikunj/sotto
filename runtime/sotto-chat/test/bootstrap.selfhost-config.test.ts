import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { chmod, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadRuntimeConfig } from '../src/bootstrap/config.js';

const direct = { provider: 'google', apiKey: 'fixture-google-key-0123456789',
  chatModelId: 'gemini-3.8-flash', nativeMediaModelId: 'gemini-3.8-flash' };
const proxy = { origin: 'https://proxy.example', bearer: 'fixture-proxy-bearer',
  chatModelId: 'chat-v1', nativeMediaModelId: 'media-v1' };
function config(overrides: Record<string, unknown> = {}) {
  return { version: 1, deployment: 'self_host', channel: 'telegram', stateDirectory: 'state', mediaRoot: 'media',
    bearerToken: 'runtime-bearer-config-test-0123456789', port: 0,
    owner: { accountId: 'account-test', ownerId: '123456789' }, lineKey: 'primary', target: 'telegram',
    providerThreadId: 'telegram:123456789', instructions: 'Offline fixture.',
    credentials: { telegramToken: 'fixture-bot-token',
      telegramLinkFile: { bot_token: 'fixture-bot-token', allowed_user: 123456789 } },
    bridge: { url: 'http://127.0.0.1:19982/mcp', readBearer: 'fixture-read-bearer' },
    modelDirect: direct, ...overrides };
}
async function load(value: unknown) {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-selfhost-config-'));
  try {
    const path = join(directory, 'runtime.json');
    await writeFile(path, JSON.stringify(value), { mode: 0o600 });
    await chmod(path, 0o600);
    return await loadRuntimeConfig(path);
  } finally { await rm(directory, { recursive: true, force: true }); }
}

function selfhostPhoton() {
  const project = 'synthetic-project';
  return config({ channel: 'photon', target: 'photon', tenantId: 'fixture-local-tenant',
    owner: { accountId: 'fixture-account', ownerId: '+15551234567' }, lineKey: 'physical-line-a',
    providerThreadId: undefined, firstContact: { mode: 'owner_dm', expectedProviderLine: '+15557654321' },
    credentials: { mode: 'cloud', photonProjectId: project, photonProjectSecret: 'synthetic-secret' },
    ownerLoop: { mode: 'explicit_command', bearer: 'a'.repeat(64),
      identity: { tenant_id: 'fixture-local-tenant', channel: 'photon', account_id: 'fixture-account',
        owner_id: '+15551234567', line_key: 'physical-line-a', target: 'photon',
        route_mode: 'first_contact', route_id: '+15557654321',
        project_fingerprint: createHash('sha256').update(`sotto-photon-project-v1:${project}`).digest('hex') } },
  });
}

function selfhostTelegram() {
  const value = config({ tenantId: 'fixture-local-tenant' });
  return { ...value, ownerLoop: { mode: 'explicit_command', bearer: 'b'.repeat(64),
    identity: { tenant_id: 'fixture-local-tenant', channel: 'telegram', account_id: 'account-test',
      owner_id: '123456789', line_key: 'primary', target: 'telegram', route_mode: 'fixed',
      route_id: 'telegram:123456789',
      bot_fingerprint: createHash('sha256').update('sotto-telegram-bot-v1:fixture-bot-token').digest('hex') } } };
}

test('self-host Telegram owner commands require canonical paired owner, bot, route and direct model', async () => {
  const value = selfhostTelegram();
  assert.equal((await load(value)).ownerLoop?.identity.channel, 'telegram');
  const cases = [
    { ...value, owner: { accountId: 'account-test', ownerId: '000123456789' } },
    { ...value, providerThreadId: 'telegram:other' },
    { ...value, credentials: { telegramToken: 'changed-bot',
      telegramLinkFile: { bot_token: 'changed-bot', allowed_user: 123456789 } } },
    { ...value, credentials: { telegramToken: 'fixture-bot-token',
      telegramLinkFile: { bot_token: 'fixture-bot-token', allowed_user: 9007199254740992 } } },
    { ...value, ownerLoop: { ...value.ownerLoop, identity: { ...value.ownerLoop.identity,
      bot_fingerprint: '0'.repeat(64) } } },
    { ...value, ownerLoop: { ...value.ownerLoop, identity: { ...value.ownerLoop.identity,
      owner_id: '999999999', route_id: 'telegram:999999999' } } },
    { ...value, ownerLoop: { ...value.ownerLoop, identity: { ...value.ownerLoop.identity,
      channel: 'photon', project_fingerprint: '0'.repeat(64), bot_fingerprint: undefined } } },
    { ...value, modelDirect: undefined, modelProxy: proxy },
  ];
  for (const candidate of cases) await assert.rejects(load(candidate));
});

test('self-host direct Photon accepts only its exact private owner-command identity', async () => {
  const value = selfhostPhoton();
  const rendered = await load(value);
  assert.equal(rendered.ownerLoop?.identity.route_id, '+15557654321');
  assert.deepEqual(rendered.modelDirect, direct);
  await assert.rejects(load(config({ ownerLoop: (value as Record<string, unknown>).ownerLoop })), /shape or field bounds/);
  const mismatched = selfhostPhoton() as Record<string, unknown>;
  const ownerLoop = mismatched.ownerLoop as { identity: Record<string, string> };
  ownerLoop.identity.project_fingerprint = '0'.repeat(64);
  await assert.rejects(load(mismatched), /shape or field bounds/);
  await assert.rejects(load({ ...selfhostPhoton(), modelDirect: undefined, modelProxy: proxy }), /shape or field bounds/);
});

test('direct Google config is explicit, self-host only, and preserves managed proxy shape', async () => {
  const rendered = await load(config());
  assert.deepEqual(rendered.modelDirect, direct);
  assert.equal('modelProxy' in rendered, false);
  assert.equal(rendered.deployment, 'self_host');

  const managed = await load(config({ deployment: 'managed', modelDirect: undefined, modelProxy: proxy }));
  assert.deepEqual(managed.modelProxy, proxy);
  assert.equal('modelDirect' in managed, false);

  await assert.rejects(load(config({ modelProxy: proxy })), /shape or field bounds/);
  await assert.rejects(load(config({ modelDirect: undefined, modelProxy: undefined })), /shape or field bounds/);
  await assert.rejects(load(config({ deployment: 'managed' })), /shape or field bounds/);
});

test('direct Google key and model identifiers reject out-of-contract values', async () => {
  for (const apiKey of ['', 'contains space', 'has\nnewline', 'has\u007fdelete', 'x'.repeat(4097)]) {
    await assert.rejects(load(config({ modelDirect: { ...direct, apiKey } })), /shape or field bounds/);
  }
  for (const modelId of ['', 'bad/model', 'x'.repeat(129)]) {
    await assert.rejects(load(config({ modelDirect: { ...direct, chatModelId: modelId } })), /shape or field bounds/);
  }
  await assert.rejects(load(config({ modelDirect: { ...direct, provider: 'openai' } })), /shape or field bounds/);
});
