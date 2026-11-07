import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startApplication, type ApplicationOptions } from '../src/application.js';
import { photonProjectFingerprint } from '../src/activation.js';
import { configuredChannelIdentityForConfig } from '../src/configured-identity.js';
import type { RuntimeConfig } from '../src/bootstrap/config.js';
import { OperationalJournal } from '../src/journal/index.js';

const bearer = 'activation-state-private-bearer-000000000000';
const owner = '+15555550123';
const line = '+15555550999';
const thread = `imessage:iMessage;-;${owner}~${line}`;
const project = 'fixture-project';
const projectFingerprint = photonProjectFingerprint(project);

function configured(mode: 'fixed' | 'first_contact', secret = 'fixture-project-secret') {
  return configuredChannelIdentityForConfig({ channel: 'photon', tenantId: 'tenant',
    bearerToken: bearer, owner: { accountId: 'account', ownerId: owner }, lineKey: 'line', target: 'photon',
    providerThreadId: mode === 'fixed' ? thread : undefined,
    firstContact: mode === 'first_contact' ? { mode: 'owner_dm', expectedProviderLine: line } : undefined,
    credentials: { mode: 'cloud', photonProjectId: project, photonProjectSecret: secret },
  } as RuntimeConfig);
}
function options(directory: string, mode: 'fixed' | 'first_contact'): ApplicationOptions {
  const base = { tenant_id: 'tenant', project_fingerprint: projectFingerprint,
    owner_id: owner, account_id: 'account', line_key: 'line' };
  return { directory, mediaRoot: directory, bearerToken: bearer, owner: { channel: 'photon', ownerId: owner,
    accountId: 'account' }, lineKey: 'line', target: 'photon', configuredChannelIdentity: configured(mode),
    ...(mode === 'fixed' ? { providerThreadId: thread, activation: { ...base, provider_thread_id: thread } } :
      { discoverySeed: { ...base, expected_provider_line: line } }) };
}
const dependencies = { agent: { generateChat: async () => ({ text: '' }),
  generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0 },
transport: { send: async () => ({ acceptance: 'not_attempted' as const, providerMessageIds: [] }) } };

async function post(port: number, route: string, body: unknown = {}, token = bearer, method = 'POST') {
  const response = await fetch(`http://127.0.0.1:${port}/v1/${route}`, { method,
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    body: method === 'POST' ? JSON.stringify(body) : undefined, redirect: 'error' });
  return { status: response.status, body: await response.json() as Record<string, any>,
    bytes: Number(response.headers.get('content-length') ?? 0) };
}

for (const mode of ['fixed', 'first_contact'] as const) {
  test(`${mode} activation state distinguishes configured route from admitted DM and durable ACK`, async () => {
    const directory = await mkdtemp(join(tmpdir(), `sotto-activation-state-${mode}-`));
    const setup = options(directory, mode);
    let app: Awaited<ReturnType<typeof startApplication>> | undefined;
    try {
      app = await startApplication(setup, dependencies);
      assert.equal((await post(app.port, 'activation-state', {}, 'wrong')).status, 401);
      assert.equal((await post(app.port, 'activation-state', {}, bearer, 'GET')).status, 405);
      assert.equal((await post(app.port, 'activation-state', { event_id: 'injected' })).status, 400);
      assert.equal((await post(app.port, 'activation-state', { padding: 'x'.repeat(64) })).status, 413);
      const absent = await post(app.port, 'activation-state');
      assert.equal(absent.status, 200);
      assert.deepEqual(absent.body.activation, { status: 'absent', mode });
      assert.deepEqual(absent.body.configured, setup.configuredChannelIdentity);
      assert.equal(JSON.stringify(absent.body).includes('fixture-project-secret'), false);
      assert.equal(JSON.stringify(absent.body).includes(bearer), false);
      assert.ok(JSON.stringify(absent.body).length < 8192);

      const inbound = { channel: 'photon' as const, eventId: 'first-owner-message',
        accountId: 'account', senderId: owner, isDirect: true, text: 'Hello' };
      assert.equal((await app.receive({ ...inbound, senderId: '+15550009999' }, { threadId: thread })).status, 'denied');
      assert.deepEqual((await post(app.port, 'activation-state')).body.activation, { status: 'absent', mode });
      assert.equal((await app.receive(inbound, { threadId: thread })).status, 'silent');
      const pending = (await post(app.port, 'activation-state')).body.activation;
      assert.equal(pending.status, 'pending');
      assert.equal(pending.mode, mode);
      assert.equal(pending.binding.provider_thread_id, thread);
      assert.equal(pending.binding.project_fingerprint, projectFingerprint);
      assert.match(pending.event_id, /^[a-f0-9]{64}$/);
      assert.equal(typeof pending.admitted_at, 'number');
      assert.equal((await post(app.port, 'activation-ack', { event_id: '0'.repeat(64) })).body.acknowledged, false);
      assert.equal((await post(app.port, 'activation-state')).body.activation.status, 'pending');
      assert.equal((await post(app.port, 'activation-ack', { event_id: pending.event_id })).body.acknowledged, true);
      const acknowledged = (await post(app.port, 'activation-state')).body.activation;
      assert.equal(acknowledged.status, 'acknowledged');
      assert.equal(acknowledged.event_id, pending.event_id);
      assert.deepEqual(acknowledged.binding, pending.binding);
      assert.ok(acknowledged.acknowledged_at >= pending.admitted_at);
      const malformed = new OperationalJournal(join(directory, 'operations.sqlite'));
      try {
        malformed.db.prepare('UPDATE activation_obligation SET event_id=? WHERE id=1').run('malformed');
        const refused = await post(app.port, 'activation-state');
        assert.equal(refused.status, 500);
        assert.deepEqual(refused.body, { error: 'operation_failed' });
        malformed.db.prepare('UPDATE activation_obligation SET event_id=? WHERE id=1').run(pending.event_id);
      } finally { malformed.close(); }
      await app.close(); app = undefined;

      const journal = new OperationalJournal(join(directory, 'operations.sqlite'));
      try {
        if (setup.discoverySeed) journal.configureActivationDiscovery(setup.discoverySeed);
        else journal.configureActivationIdentity(setup.activation);
        const cutoff = Date.now() + 10_000;
        journal.pruneTerminal(cutoff, cutoff);
        assert.equal(journal.getInbound(pending.event_id), undefined, 'raw admitted event may be pruned after ACK');
        assert.equal(journal.getDurableActivationAdmission().status, 'acknowledged');
      } finally { journal.close(); }
      app = await startApplication(setup, dependencies);
      assert.deepEqual((await post(app.port, 'activation-state')).body.activation, acknowledged);
      assert.equal((await app.receive({ ...inbound, eventId: 'later-owner-message' },
        { threadId: thread })).status, 'silent');
      assert.equal((await post(app.port, 'activation-state')).body.activation.status, 'pending',
        'a later admitted owner event resets ACK until the receipt is repaired');
      await app.close(); app = undefined;
      for (const changed of [
        { ...setup, owner: { ...setup.owner, ownerId: '+15555550234' } },
        { ...setup, owner: { ...setup.owner, accountId: 'other' } },
        { ...setup, lineKey: 'other' },
        mode === 'fixed' ? { ...setup, activation: { ...setup.activation!, project_fingerprint: 'a'.repeat(64) } } :
          { ...setup, discoverySeed: { ...setup.discoverySeed!, project_fingerprint: 'a'.repeat(64) } },
      ]) await assert.rejects(startApplication(changed, dependencies));
      assert.notEqual(configured(mode).credential_proof, configured(mode, 'changed-secret').credential_proof);
    } finally { await app?.close(); await rm(directory, { recursive: true, force: true }); }
  });
}

test('legacy and non-Photon constructors cannot synthesize activation state', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-activation-state-legacy-'));
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    const setup = options(directory, 'fixed');
    app = await startApplication({ ...setup, configuredChannelIdentity: undefined }, dependencies);
    assert.equal((await post(app.port, 'activation-state')).status, 501);
    await app.close(); app = undefined;
    app = await startApplication({ ...setup, directory: join(directory, 'legacy'),
      activation: undefined, configuredChannelIdentity: undefined }, dependencies);
    assert.equal((await post(app.port, 'activation-state')).status, 501);
    await app.close(); app = undefined;
    const selfHost = configuredChannelIdentityForConfig({ channel: 'photon', tenantId: 'tenant',
      bearerToken: bearer, owner: { accountId: 'account', ownerId: owner }, lineKey: 'line',
      target: 'photon', providerThreadId: thread,
      credentials: { mode: 'self_host', photonServerUrl: 'https://photon.example', photonApiKey: 'fixture-key' },
    } as RuntimeConfig);
    app = await startApplication({ ...setup, directory: join(directory, 'self-host'),
      activation: undefined, configuredChannelIdentity: selfHost }, dependencies);
    assert.equal((await post(app.port, 'activation-state')).status, 501,
      'self-host static destination is not an admitted owner-DM proof');
  } finally { await app?.close(); await rm(directory, { recursive: true, force: true }); }
});
