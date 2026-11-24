import assert from 'node:assert/strict';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import test from 'node:test';
import {startApplication} from '../src/application.js';
import {startOwnerGateway} from '../src/channels/gateway.js';
import {createPhotonSdkAdapter} from '../src/channels/sdk.js';

test('late native owner text preserves first callback and journal clocks across redelivery', {timeout: 5_000}, async t => {
  const ownerId = '+15550100001';
  const credentials = {photonProjectId: 'fixture', photonProjectSecret: 'fixture'};
  const adapter = createPhotonSdkAdapter(credentials);
  const space = {id: 'any;-;' + ownerId, phone: 'shared'};
  const providerThreadId = adapter.encodeThreadId({chatGuid: space.id, phone: space.phone});
  const occurred = Date.parse('2026-11-01T13:00:00Z');
  const callback = occurred + 174_789;
  let clock = callback;
  t.mock.method(Date, 'now', () => clock);
  const directory = await mkdtemp(join(tmpdir(), 'sotto-photon-ingress-'));
  const application = await startApplication({directory, mediaRoot: directory, deferListen: true,
    bearerToken: 'fixture-only-bearer-for-local-tests-000', lineKey: 'line', target: 'owner', providerThreadId,
    owner: {channel: 'photon', accountId: 'fixture', ownerId},
  }, {
    agent: {generateChat: async () => ({text: 'Fixture response.'}), generateUnattended: async () => ({text: ''}),
      rotateThread: async () => {}, purgeThreadsBefore: async () => 0},
    transport: {send: async () => ({acceptance: 'accepted', providerMessageIds: ['fixture-response']})},
  });
  const db = new DatabaseSync(join(directory, 'operations.sqlite'));
  const message = {id: 'fixture-inbound', direction: 'inbound', content: {type: 'text', text: 'Done'},
    sender: {id: ownerId}, timestamp: new Date(occurred)};
  let markConsumed!: () => void;
  const consumed = new Promise<void>(resolve => { markConsumed = resolve; });
  adapter.app = {messages: (async function* () {
    yield [space, {...message, id: 'foreign', sender: {id: '+15550100002'}}];
    yield [space, message];
    clock += 9_000;
    yield [space, message];
    markConsumed();
  })(), stop: async () => {}} as unknown as NonNullable<typeof adapter.app>;
  const gateway = await startOwnerGateway({adapter, database: db, application: {
    receive(inbound, provider) {
      clock += 7; // Work between SDK handoff and the journal INSERT.
      return application.receive(inbound, provider);
    },
    close: () => application.close(),
  }, manifest: {
    channel: 'photon', accountId: 'fixture', ownerId, lineKey: 'line', credentials, providerThreadId,
  }});
  try {
    await consumed;
    const rows = db.prepare('SELECT * FROM inbound_events').all();
    assert.equal(rows.length, 1);
    assert.equal(rows[0].received_at, callback + 7);
    assert.equal(rows[0].provider_occurred_at_ms, occurred);
    assert.equal(rows[0].gateway_callback_at_ms, callback);
    assert.equal(JSON.parse(String(rows[0].payload)).inbound.providerSentAtMs, undefined);
    assert.deepEqual(JSON.parse(String(rows[0].continuation_json)), {version: 1, eligible: false});
  } finally {
    await gateway.close();
    db.close();
    await rm(directory, {recursive: true, force: true});
  }
});
