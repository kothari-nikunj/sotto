import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { startApplication, type ApplicationAgent, type ApplicationOptions } from '../src/application.js';
import { photonProjectFingerprint } from '../src/activation.js';
import { OperationalJournal } from '../src/journal/index.js';
import { DatabaseSync } from 'node:sqlite';
import { Message } from 'chat';
import { createiMessageAdapter } from '@photon-ai/chat-adapter-imessage';
import { startOwnerGateway, type GatewayAdapter } from '../src/channels/gateway.js';
import { PollerOwnership } from '../src/channels/lifecycle.js';

test('first owner DM binds once; scheduled delivery waits for the exact activation ACK', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-first-contact-'));
  const thread = 'imessage:iMessage;-;+15550001111~+15550002222';
  const seed = { tenant_id: 'tenant', project_fingerprint: photonProjectFingerprint('project'),
    owner_id: '+15550001111', account_id: 'account', line_key: 'line', expected_provider_line: '+15550002222' };
  const options: ApplicationOptions = { directory, mediaRoot: directory,
    bearerToken: 'offline-first-contact-bearer-token-000000',
    owner: { channel: 'photon', accountId: seed.account_id, ownerId: seed.owner_id },
    lineKey: seed.line_key, target: 'owner', discoverySeed: seed };
  const sends: string[] = [];
  const turns: string[] = [];
  const agent: ApplicationAgent = {
    generateChat: async request => { turns.push(request.threadId); return { text: 'reply' }; },
    generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0,
  };
  const deps = { agent, transport: { send: async (input: { threadId: string }) => {
    sends.push(input.threadId); return { acceptance: 'accepted' as const, providerMessageIds: [`sent-${sends.length}`] };
  } } };
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  const post = async (route: string, body: unknown) => {
    const response = await fetch(`http://127.0.0.1:${app!.port}/v1/${route}`, { method: 'POST',
      headers: { authorization: `Bearer ${options.bearerToken}`, 'content-type': 'application/json' },
      body: JSON.stringify(body) });
    assert.equal(response.status, 200);
    return response.json() as Promise<Record<string, any>>;
  };
  const scheduled = (dispatch_id: string) => post('send', { dispatch_id, target: 'owner', text: 'scheduled' });
  const health = async () => {
    const response = await fetch(`http://127.0.0.1:${app!.port}/v1/health`, {
      headers: { authorization: `Bearer ${options.bearerToken}` },
    });
    assert.equal(response.status, 200);
    return response.json() as Promise<Record<string, any>>;
  };
  const inbound = { channel: 'photon' as const, eventId: 'first-owner', accountId: seed.account_id,
    senderId: seed.owner_id, isDirect: true, text: 'hello' };
  try {
    app = await startApplication(options, deps);
    assert.deepEqual((await health()).activation, { bound: false, acknowledged: false, pending: false });
    assert.equal((await health()).ready, false);
    assert.deepEqual(await scheduled('before-binding'), { acceptance: 'not_attempted' });
    assert.equal((await app.receive({ ...inbound, senderId: 'other' }, { threadId: thread })).status, 'denied');
    assert.deepEqual(await post('activation-evidence', {}), { version: 1, pending: null });
    assert.equal((await app.receive(inbound, { threadId: thread })).status, 'accepted');
    assert.equal(sends.length, 1, 'interactive reply uses captured thread before ACK');
    assert.deepEqual(sends, [thread]);
    const evidence = (await post('activation-evidence', {})).pending;
    assert.equal(evidence.provider_thread_id, thread);
    assert.deepEqual((await health()).activation, { bound: true, acknowledged: false, pending: true });
    assert.equal((await health()).ready, false);
    assert.deepEqual(await scheduled('before-ack'), { acceptance: 'not_attempted' });
    assert.deepEqual(await post('activation-ack', { event_id: '0'.repeat(64) }), { acknowledged: false });
    assert.deepEqual(await scheduled('wrong-ack'), { acceptance: 'not_attempted' });
    const journal = new OperationalJournal(join(directory, 'operations.sqlite'));
    try {
      assert.equal(journal.getDispatch('before-binding'), undefined);
      assert.equal(journal.getDispatch('before-ack'), undefined);
      assert.equal(journal.getDispatch('wrong-ack'), undefined);
    } finally { journal.close(); }
    assert.deepEqual(await post('activation-ack', { event_id: evidence.event_id }), { acknowledged: true });
    assert.deepEqual((await health()).activation, { bound: true, acknowledged: true, pending: false });
    assert.equal((await health()).ready, true);
    assert.equal((await scheduled('after-ack')).acceptance, 'accepted');
    assert.deepEqual(sends, [thread, thread]);
    assert.equal((await app.receive({ ...inbound, eventId: 'repair-owner' }, { threadId: thread })).status, 'accepted');
    assert.deepEqual(await scheduled('repair-before-ack'), { acceptance: 'not_attempted' });
    assert.deepEqual(await post('activation-ack', { event_id: evidence.event_id }), { acknowledged: false });
    const repaired = (await post('activation-evidence', {})).pending;
    assert.notEqual(repaired.event_id, evidence.event_id);
    assert.deepEqual(await post('activation-ack', { event_id: repaired.event_id }), { acknowledged: true });
    assert.deepEqual(sends, [thread, thread, thread]);
    assert.throws(() => app!.receive({ ...inbound, eventId: 'conflict' },
      { threadId: 'imessage:iMessage;-;+15550001111~+15550003333' }), /candidate/);
    await app.close(); app = undefined;
    app = await startApplication(options, deps);
    assert.equal((await app.receive(inbound, { threadId: thread })).status, 'duplicate');
    assert.equal((await scheduled('after-restart')).acceptance, 'accepted');
    assert.deepEqual(sends, [thread, thread, thread, thread]);
    await app.close(); app = undefined;
    await assert.rejects(startApplication({ ...options, discoverySeed: { ...seed,
      expected_provider_line: '+15550003333' } }, deps), /different owner|seed/);
  } finally { await app?.close(); await rm(directory, { recursive: true, force: true }); }
});

test('real Photon codec gateway passes only the verified owner thread into atomic admission', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-first-gateway-'));
  const owner = '+15550001111';
  const line = '+15550002222';
  const thread = `imessage:iMessage;-;${owner}~${line}`;
  const seed = { tenant_id: '', project_fingerprint: photonProjectFingerprint('project'),
    owner_id: owner, account_id: 'account', line_key: 'line', expected_provider_line: line };
  const app = await startApplication({ directory, mediaRoot: directory,
    bearerToken: 'offline-first-contact-bearer-token-000000',
    owner: { channel: 'photon', accountId: 'account', ownerId: owner },
    lineKey: 'line', target: 'owner', discoverySeed: seed, deferListen: true }, {
    agent: { generateChat: async () => ({ text: '' }), generateUnattended: async () => ({ text: '' }),
      rotateThread: async () => {}, purgeThreadsBefore: async () => 0 },
    transport: { send: async () => ({ acceptance: 'not_attempted', providerMessageIds: [] }) },
  });
  const codec = createiMessageAdapter({ local: false, projectId: 'project', projectSecret: 'fixture',
    serverUrl: '', apiKey: '', phone: '', webhookSecret: '' });
  const adapter = {
    name: 'imessage', userName: 'fixture', initialize: async () => {}, disconnect: async () => {},
    startGatewayListener: async (webhook: { waitUntil(promise: Promise<void>): void }, _duration: number, signal: AbortSignal) => {
      webhook.waitUntil(new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true })));
      return new Response('', { status: 200 });
    },
    isDM: (id: string) => codec.isDM(id), decodeThreadId: (id: string) => codec.decodeThreadId(id),
    encodeThreadId: (value: { chatGuid: string; phone?: string }) => codec.encodeThreadId(value),
    channelIdFromThreadId: () => thread, postMessage: async () => ({ id: 'unused' }),
    fetchMessages: async () => ({ messages: [], hasMore: false }),
    addReaction: async () => {}, removeReaction: async () => {}, deleteMessage: async () => {},
    editMessage: async () => ({ id: 'unused' }), startTyping: async () => {}, renderFormatted: () => '',
  } as unknown as GatewayAdapter;
  const db = new DatabaseSync(':memory:');
  let gateway: Awaited<ReturnType<typeof startOwnerGateway>> | undefined;
  const ownerMessage = (id: string, threadId = thread, isMe = false) => new Message({
    id, threadId, text: 'hello', raw: { id }, formatted: { type: 'root', children: [] },
    attachments: [], metadata: { dateSent: new Date(), edited: false },
    author: { userId: owner, userName: 'owner', fullName: 'Owner', isBot: false, isMe },
  });
  try {
    gateway = await startOwnerGateway({ manifest: { channel: 'photon', accountId: 'account', ownerId: owner,
      lineKey: 'line', expectedProviderLine: line, credentials: { photonProjectId: 'project', photonProjectSecret: 'fixture' } },
    application: app, database: db, adapter, ownership: new PollerOwnership() });
    await gateway.chat.processMessage(adapter, thread, ownerMessage('outbound', thread, true));
    await gateway.chat.processMessage(adapter, `imessage:iMessage;-;${owner}~+15550003333`,
      ownerMessage('wrong-line', `imessage:iMessage;-;${owner}~+15550003333`));
    const journal = new OperationalJournal(join(directory, 'operations.sqlite'));
    try { assert.equal(journal.getActivationIdentity(), null); }
    finally { journal.close(); }
    await gateway.chat.processMessage(adapter, thread, ownerMessage('owner-first'));
    const bound = new OperationalJournal(join(directory, 'operations.sqlite'));
    try {
      // The activation identity is loaded into the owning journal; a fresh reader
      // verifies both durable rows directly without becoming a second runtime.
      assert.equal(JSON.parse(String(bound.db.prepare('SELECT identity_json FROM activation_identity WHERE id=1').get()!.identity_json)).provider_thread_id, thread);
      assert.equal(bound.db.prepare('SELECT count(*) AS n FROM inbound_events').get()!.n, 1);
      assert.equal(bound.db.prepare('SELECT count(*) AS n FROM activation_obligation').get()!.n, 1);
    } finally { bound.close(); }
  } finally { await gateway?.close(); db.close(); await rm(directory, { recursive: true, force: true }); }
});

test('restart recovers the admitted discovery event with its exact saved provider thread', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-first-recovery-'));
  const thread = 'imessage:iMessage;-;+15550001111~+15550002222';
  const seed = { tenant_id: '', project_fingerprint: photonProjectFingerprint('project'),
    owner_id: '+15550001111', account_id: 'account', line_key: 'line', expected_provider_line: '+15550002222' };
  const options: ApplicationOptions = { directory, mediaRoot: directory, deferListen: true,
    bearerToken: 'offline-first-contact-bearer-token-000000',
    owner: { channel: 'photon', accountId: seed.account_id, ownerId: seed.owner_id },
    lineKey: 'line', target: 'owner', discoverySeed: seed };
  let hold = true;
  const recoveredThreads: string[] = [];
  const agent: ApplicationAgent = {
    generateChat: async request => {
      if (hold) await new Promise<void>(resolve => request.signal.addEventListener('abort', () => resolve(), { once: true }));
      else recoveredThreads.push(request.threadId);
      return { text: '' };
    },
    generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0,
  };
  const deps = { agent, transport: { send: async () => ({ acceptance: 'not_attempted' as const, providerMessageIds: [] }) } };
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    app = await startApplication(options, deps);
    const pending = app.receive({ channel: 'photon', eventId: 'first', accountId: 'account',
      senderId: seed.owner_id, isDirect: true, text: 'remember this' }, { threadId: thread });
    const journal = new OperationalJournal(join(directory, 'operations.sqlite'));
    try {
      assert.equal(journal.db.prepare('SELECT count(*) AS n FROM inbound_events').get()!.n, 1);
      assert.equal(JSON.parse(String(journal.db.prepare('SELECT identity_json FROM activation_identity WHERE id=1').get()!.identity_json)).provider_thread_id, thread);
    } finally { journal.close(); }
    await app.close(); app = undefined;
    await pending;
    hold = false;
    app = await startApplication(options, deps);
    assert.equal(recoveredThreads.length, 1);
    assert.match(recoveredThreads[0]!, /^[a-f0-9]{64}$/,
      'the model sees the stable scoped memory identity, not a model-selected destination');
    const evidence = new OperationalJournal(join(directory, 'operations.sqlite'));
    try {
      const raw = evidence.db.prepare('SELECT payload FROM inbound_events WHERE source_id=?').get('first');
      assert.equal(JSON.parse(String(raw!.payload)).context.threadId, thread);
    } finally { evidence.close(); }
  } finally { await app?.close(); await rm(directory, { recursive: true, force: true }); }
});

test('a second owner chat on the bound line is denied, not thrown, and the bound chat keeps working', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-first-contact-second-chat-'));
  const thread = 'imessage:iMessage;-;+15550001111~+15550002222';
  const smsThread = 'imessage:SMS;-;+15550001111~+15550002222';
  const seed = { tenant_id: 'tenant', project_fingerprint: photonProjectFingerprint('project'),
    owner_id: '+15550001111', account_id: 'account', line_key: 'line', expected_provider_line: '+15550002222' };
  const options: ApplicationOptions = { directory, mediaRoot: directory,
    bearerToken: 'offline-first-contact-bearer-token-000000',
    owner: { channel: 'photon', accountId: seed.account_id, ownerId: seed.owner_id },
    lineKey: seed.line_key, target: 'owner', discoverySeed: seed };
  const sends: string[] = [];
  const agent: ApplicationAgent = {
    generateChat: async () => ({ text: 'reply' }),
    generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0,
  };
  const deps = { agent, transport: { send: async (input: { threadId: string }) => {
    sends.push(input.threadId); return { acceptance: 'accepted' as const, providerMessageIds: [`sent-${sends.length}`] };
  } } };
  const inbound = (eventId: string) => ({ channel: 'photon' as const, eventId, accountId: seed.account_id,
    senderId: seed.owner_id, isDirect: true, text: 'hello' });
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    app = await startApplication(options, deps);
    assert.equal((await app.receive(inbound('first-owner'), { threadId: thread })).status, 'accepted');
    // Photon delivers this through a non-awaiting callback: a synchronous throw would crash the process.
    const second = app.receive(inbound('sms-owner'), { threadId: smsThread });
    assert.deepEqual(await second, { status: 'denied', reason: 'activation_destination_conflict' });
    assert.equal((await app.receive(inbound('next-owner'), { threadId: thread })).status, 'accepted');
    assert.deepEqual(sends, [thread, thread]);
  } finally { await app?.close(); await rm(directory, { recursive: true, force: true }); }
});
