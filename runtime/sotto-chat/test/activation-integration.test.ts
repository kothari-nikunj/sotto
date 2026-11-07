import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { startApplication, type ApplicationAgent, type ApplicationOptions } from '../src/application.js';
import { photonProjectFingerprint } from '../src/activation.js';

test('durable owner admission survives restart and the actual Python writer retries a lost activation ACK', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-activation-'));
  const owner = '+15550001111';
  const project = 'offline-fixture-project';
  const identity = { tenant_id: 'fixture-tenant', owner_id: owner, account_id: 'fixture-account',
    line_key: 'fixture-line', provider_thread_id: 'imessage:fixture-chat-guid~+15550002222',
    project_fingerprint: photonProjectFingerprint(project) };
  const options: ApplicationOptions = { directory: join(directory, 'node'), mediaRoot: directory,
    bearerToken: 'offline-activation-fixture-token-0000000000',
    owner: { channel: 'photon', ownerId: owner, accountId: identity.account_id },
    lineKey: identity.line_key, target: 'owner', providerThreadId: identity.provider_thread_id, activation: identity };
  let block = true; let models = 0; let sends = 0;
  const agent: ApplicationAgent = {
    generateChat: async request => {
      models++;
      if (block) await new Promise<void>(done => {
        if (request.signal?.aborted) done();
        else request.signal?.addEventListener('abort', () => done(), { once: true });
      });
      return { text: '' };
    },
    generateUnattended: async () => ({ text: '' }),
    rotateThread: async () => {}, purgeThreadsBefore: async () => 0,
  };
  const dependencies = { agent, transport: { send: async () => {
    sends++; return { acceptance: 'accepted' as const, providerMessageIds: ['unused-fixture'] };
  } } };
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  const post = async (route: string, body: unknown) => {
    const response = await fetch(`http://127.0.0.1:${app!.port}/v1/${route}`, { method: 'POST',
      headers: { authorization: `Bearer ${options.bearerToken}`, 'content-type': 'application/json' }, body: JSON.stringify(body) });
    assert.equal(response.status, 200);
    return response.json() as Promise<Record<string, any>>;
  };
  try {
    app = await startApplication(options, dependencies);
    const inbound = { channel: 'photon' as const, eventId: 'owner-message', accountId: identity.account_id,
      senderId: owner, isDirect: true, text: 'fixture owner hello' };
    for (const invalid of [{ ...inbound, senderId: 'other' }, { ...inbound, accountId: 'other' }, { ...inbound, isDirect: false }]) {
      assert.equal((await app.receive(invalid)).status, 'denied');
    }
    assert.deepEqual(await post('activation-evidence', {}), { version: 1, pending: null });
    assert.equal(models, 0);
    const unfinished = app.receive(inbound);
    const admitted = (await post('activation-evidence', {})).pending;
    assert.equal(admitted.provider_event_id, inbound.eventId);
    assert.equal(admitted.project_fingerprint, identity.project_fingerprint);
    assert.equal(sends, 0, 'activation evidence exists while the model has not finished');
    await app.close(); app = undefined;
    await unfinished;
    block = false;
    app = await startApplication(options, dependencies);
    await app.rotateSessions();
    assert.deepEqual((await post('activation-evidence', {})).pending, admitted);

    const script = `
import importlib.util, json, pathlib, sys
def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
client = load('activation_client', sys.argv[1]).ChatClient(sys.argv[4], sys.argv[5])
activation = load('runtime_activation', sys.argv[2])
photon = load('fixture_photon', sys.argv[3])
pending = client.call('activation-evidence', {})
assert pending.ok and pending.value['version'] == 1
evidence = pending.value['pending']
policy = dict(tenant_id='fixture-tenant', owner_id=sys.argv[7], project_fingerprint=photon.project_fingerprint(sys.argv[8]))
first = activation.consume(evidence, **policy, writer=photon.record_owner_activation, ack=lambda _: False)
receipt_path = pathlib.Path(sys.argv[6]) / 'config/photon-activation.json'
receipt = json.loads(receipt_path.read_text())
before = receipt_path.stat().st_mtime_ns
still_pending = client.call('activation-evidence', {}).value['pending']
assert still_pending == evidence
def ack(event_id):
    response = client.call('activation-ack', {'event_id': event_id})
    return response.ok and response.value.get('acknowledged') is True
second = activation.consume(still_pending, **policy, writer=photon.record_owner_activation, ack=ack)
assert ack(evidence['event_id'])
print(json.dumps({'first':first, 'second':second, 'receipt':receipt,
                  'same_write':before == receipt_path.stat().st_mtime_ns,
                  'pending':client.call('activation-evidence', {}).value['pending']}))
`;
    const data = join(directory, 'receiver');
    const runReceiver = () => promisify(execFile)('python3', ['-I', '-c', script,
      resolve('../../adapters/sotto/client.py'), resolve('../trigger-receiver/runtime_activation.py'),
      resolve('../../adapters/sotto/photon_owner.py'), `http://127.0.0.1:${app!.port}`, options.bearerToken, data, owner, project],
    { timeout: 20_000, maxBuffer: 32_000, env: { PATH: process.env.PATH, SOTTO_DATA: data,
      SOTTO_DEPLOYMENT_MODE: 'managed', SOTTO_TENANT_ID: 'fixture-tenant', PHOTON_HOME_CHANNEL: owner,
      PHOTON_ALLOWED_USERS: owner, PHOTON_ALLOW_ALL_USERS: 'false', PHOTON_PROJECT_ID: project } });
    const result = await runReceiver();
    const observed = JSON.parse(result.stdout);
    assert.deepEqual(observed, { first: false, second: true, same_write: true, pending: null,
      receipt: { tenant_id: 'fixture-tenant', owner, chat_id: 'fixture-chat-guid', activated: true,
        project_fingerprint: identity.project_fingerprint } });
    assert.equal(sends, 0);
    // Existing Hermes behavior repairs a lost receipt on a new authenticated owner DM.
    // Replayed old input must not manufacture that new admission.
    await rm(join(data, 'config/photon-activation.json'));
    assert.equal((await app.receive(inbound)).status, 'duplicate');
    assert.deepEqual(await post('activation-evidence', {}), { version: 1, pending: null });
    await app.receive({ ...inbound, eventId: 'repair-owner-message' });
    assert.notEqual((await post('activation-evidence', {})).pending.event_id, admitted.event_id);
    assert.deepEqual(await post('activation-ack', { event_id: admitted.event_id }), { acknowledged: false });
    assert.deepEqual(JSON.parse((await runReceiver()).stdout), observed);
    assert.equal(sends, 0);
    await app.close(); app = undefined;
    await assert.rejects(startApplication({ ...options, activation: { ...identity, project_fingerprint: 'b'.repeat(64) } }, dependencies), /activation/i);
    await assert.rejects(startApplication({ ...options, activation: undefined }, dependencies), /activation/i);
    app = await startApplication(options, dependencies);
    assert.deepEqual(await post('activation-evidence', {}), { version: 1, pending: null });
  } finally { await app?.close(); await rm(directory, { recursive: true, force: true }); }
});
