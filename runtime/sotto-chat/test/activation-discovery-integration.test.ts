import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { mkdtemp, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { startApplication, type ApplicationAgent, type ApplicationOptions } from '../src/application.js';
import { photonProjectFingerprint } from '../src/activation.js';

test('first Photon owner DM binds durably; sole Python receipt writer and lost ACK unlock scheduled delivery once',
  { timeout: 30_000 }, async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-first-contact-'));
  const owner = '+15550001111';
  const line = '+15550002222';
  const guid = `iMessage;-;${owner}`;
  const thread = `imessage:${guid}~${line}`;
  const project = 'offline-first-contact-project';
  const seed = { tenant_id: 'fixture-tenant', owner_id: owner, account_id: 'fixture-account',
    line_key: 'fixture-line', expected_provider_line: line,
    project_fingerprint: photonProjectFingerprint(project) };
  const options: ApplicationOptions = { directory: join(directory, 'node'), mediaRoot: directory,
    bearerToken: 'offline-first-contact-token-000000000000',
    owner: { channel: 'photon', ownerId: owner, accountId: seed.account_id },
    lineKey: seed.line_key, target: 'photon', discoverySeed: seed };
  let releaseModel: (() => void) | undefined;
  let markModelStarted: (() => void) | undefined;
  const modelStarted = new Promise<void>(done => { markModelStarted = done; });
  let modelCalls = 0;
  const sends: Array<{ threadId: string; text?: string }> = [];
  const agent: ApplicationAgent = {
    generateChat: async request => {
      modelCalls++;
      await new Promise<void>(done => { releaseModel = done; markModelStarted?.(); });
      if (request.signal?.aborted) return { text: '' };
      return { text: 'Welcome, fixture owner.' };
    },
    generateUnattended: async () => ({ text: '' }),
    rotateThread: async () => {}, purgeThreadsBefore: async () => 0,
  };
  const dependencies = { agent, transport: { send: async (request: { threadId: string; text?: string }) => {
    sends.push({ threadId: request.threadId, text: request.text });
    return { acceptance: 'accepted' as const, providerMessageIds: ['fake-provider-receipt'] };
  } } };
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  const post = async (route: string, body: unknown) => {
    const response = await fetch(`http://127.0.0.1:${app!.port}/v1/${route}`, { method: 'POST',
      headers: { authorization: `Bearer ${options.bearerToken}`, 'content-type': 'application/json' },
      body: JSON.stringify(body) });
    assert.equal(response.status, 200, route);
    return response.json() as Promise<Record<string, any>>;
  };
  const health = async () => {
    const response = await fetch(`http://127.0.0.1:${app!.port}/v1/health`, {
      headers: { authorization: `Bearer ${options.bearerToken}` },
    });
    assert.equal(response.status, 200);
    return response.json() as Promise<Record<string, any>>;
  };
  const send = () => post('send', { dispatch_id: 'first-brief', target: 'photon', text: 'offline fixture brief', attempt: 1 });
  try {
    app = await startApplication(options, dependencies);
    assert.deepEqual(await post('activation-evidence', {}), { version: 1, pending: null });
    assert.equal((await health()).activation.bound, false);
    assert.equal((await send()).acceptance, 'not_attempted');
    assert.deepEqual(sends, []);

    const inbound = { channel: 'photon' as const, eventId: 'first-owner-message',
      accountId: seed.account_id, senderId: owner, isDirect: true, text: 'Hello Sotto' };
    assert.equal((await app.receive({ ...inbound, senderId: '+15550009999' }, { threadId: thread })).status, 'denied');
    assert.equal((await app.receive({ ...inbound, isDirect: false }, { threadId: thread })).status, 'denied');
    assert.deepEqual(await post('activation-evidence', {}), { version: 1, pending: null });

    // Admission is committed synchronously before the turn/model promise resolves.
    const firstTurn = app.receive(inbound, { threadId: thread });
    const evidence = (await post('activation-evidence', {})).pending;
    assert.equal(evidence.provider_thread_id, thread);
    assert.equal(evidence.owner_id, owner);
    assert.equal(evidence.provider_event_id, inbound.eventId);
    assert.equal((await health()).activation.bound, true);
    assert.equal((await health()).activation.acknowledged, false);
    assert.equal((await send()).acceptance, 'not_attempted');
    assert.deepEqual(sends, []);
    await modelStarted;
    releaseModel!();
    await firstTurn;
    assert.deepEqual(sends, [{ threadId: thread, text: 'Welcome, fixture owner.' }]);
    await app.close(); app = undefined;

    app = await startApplication(options, dependencies);
    assert.deepEqual((await post('activation-evidence', {})).pending, evidence);
    assert.equal((await send()).acceptance, 'not_attempted');

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
pending = client.call('activation-evidence', {}).value['pending']
policy = dict(tenant_id='fixture-tenant', owner_id=sys.argv[7], project_fingerprint=photon.project_fingerprint(sys.argv[8]))
first = activation.consume(pending, **policy, writer=photon.record_owner_activation, ack=lambda _: False)
receipt_path = pathlib.Path(sys.argv[6]) / 'config/photon-activation.json'
receipt = json.loads(receipt_path.read_text())
before = receipt_path.stat().st_mtime_ns
assert client.call('activation-evidence', {}).value['pending'] == pending
def ack(event_id):
    response = client.call('activation-ack', {'event_id': event_id})
    return response.ok and response.value.get('acknowledged') is True
second = activation.consume(pending, **policy, writer=photon.record_owner_activation, ack=ack)
assert ack(pending['event_id'])
print(json.dumps({'first': first, 'second': second, 'same_write': before == receipt_path.stat().st_mtime_ns,
                  'receipt': receipt, 'pending': client.call('activation-evidence', {}).value['pending']}))
`;
    const data = join(directory, 'receiver');
    const result = await promisify(execFile)('python3', ['-I', '-c', script,
      resolve('../../adapters/sotto/client.py'), resolve('../trigger-receiver/runtime_activation.py'),
      resolve('../../adapters/sotto/photon_owner.py'), `http://127.0.0.1:${app.port}`,
      options.bearerToken, data, owner, project],
    { timeout: 20_000, maxBuffer: 32_000, env: { PATH: process.env.PATH, SOTTO_DATA: data,
      SOTTO_DEPLOYMENT_MODE: 'managed', SOTTO_TENANT_ID: seed.tenant_id,
      PHOTON_HOME_CHANNEL: owner, PHOTON_ALLOWED_USERS: owner, PHOTON_ALLOW_ALL_USERS: 'false',
      PHOTON_PROJECT_ID: project } });
    const observed = JSON.parse(result.stdout);
    assert.deepEqual(observed, { first: false, second: true, same_write: true, pending: null,
      receipt: { tenant_id: seed.tenant_id, owner, chat_id: guid, activated: true,
        project_fingerprint: seed.project_fingerprint } });
    assert.deepEqual(await readdir(join(data, 'config')), ['photon-activation.json'],
      'the activation writer must not create a source grant');
    assert.equal((await health()).activation.acknowledged, true);
    assert.equal((await send()).acceptance, 'accepted');
    assert.deepEqual(sends, [{ threadId: thread, text: 'Welcome, fixture owner.' },
      { threadId: thread, text: 'offline fixture brief' }]);
    assert.equal((await send()).acceptance, 'accepted');
    assert.equal(sends.length, 2, 'replayed scheduled dispatch must not post again');
    const beforeReplayModels = modelCalls;
    assert.equal((await app.receive(inbound, { threadId: thread })).status, 'duplicate');
    assert.deepEqual(await post('activation-evidence', {}), { version: 1, pending: null });
    assert.equal(modelCalls, beforeReplayModels, 'lost ACK and duplicate event cannot regenerate a welcome');
  } finally { releaseModel?.(); await app?.close(); await rm(directory, { recursive: true, force: true }); }
});
