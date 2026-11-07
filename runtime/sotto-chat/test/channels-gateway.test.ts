import test from "node:test";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { Message, type Adapter } from "chat";
import { createiMessageAdapter } from "@photon-ai/chat-adapter-imessage";
import { startOwnerGateway, type GatewayAdapter, type GatewayApplication, type OwnerGatewayManifest } from "../src/channels/gateway.js";
import { PollerOwnership } from "../src/channels/lifecycle.js";
import { createPhotonSdkAdapter } from '../src/channels/sdk.js';

const manifest = {
  channel: "photon",
  accountId: "photon-line-1",
  ownerId: "+15551234567",
  lineKey: "primary",
  providerThreadId: "imessage:iMessage;-;+15551234567",
  credentials: { photonProjectId: "test-project", photonProjectSecret: "test-secret" },
} satisfies OwnerGatewayManifest;

function message(id: string, author = manifest.ownerId, raw: unknown = { id }, isMe = false,
  threadId = manifest.providerThreadId, text = 'hello'): Message {
  return new Message({
    id, threadId, text,
    formatted: { type: "root", children: [{ type: "paragraph", children: [{ type: "text", value: "hello" }] }] },
    raw, author: { userId: author, userName: "owner", fullName: "Owner", isBot: false, isMe },
    metadata: { dateSent: new Date(), edited: false }, attachments: [],
  });
}

function fakePhoton(options: { failStart?: boolean; failListenerOnStop?: boolean;
  noDisconnect?: boolean; failDisconnect?: boolean; throwAfterStart?: boolean } = {}) {
  const threadCodec = createiMessageAdapter({ local: false, projectId: 'fixture-project',
    projectSecret: 'fixture-secret', serverUrl: '', apiKey: '', phone: '', webhookSecret: '' });
  let initialized = false;
  let disconnected = false;
  let disconnectCalls = 0;
  let listenerStarted = false;
  let listenerStopped = false;
  const adapter = {
    name: "imessage", userName: "sotto",
    initialize: async () => { initialized = true; },
    disconnect: options.noDisconnect ? undefined : async () => {
      disconnectCalls++;
      if (options.failDisconnect) throw new Error('secret-provider-url?token=private');
      disconnected = true;
    },
    startGatewayListener: async (webhook: { waitUntil: (promise: Promise<unknown>) => void }, _duration: number, signal?: AbortSignal) => {
      listenerStarted = true;
      if (options.throwAfterStart) throw new Error('partial listener startup');
      if (options.failStart) return new Response("not initialized", { status: 500 });
      webhook.waitUntil(new Promise<void>((resolve, reject) => {
        signal?.addEventListener("abort", () => {
          listenerStopped = true;
          if (options.failListenerOnStop) { reject(new Error("listener failed during shutdown")); return; }
          resolve();
        }, { once: true });
      }));
      return new Response(JSON.stringify({ status: "listening" }), { status: 200 });
    },
    isDM: (threadId: string) => threadCodec.isDM(threadId),
    channelIdFromThreadId: () => "imessage:dm",
    decodeThreadId: (threadId: string) => threadCodec.decodeThreadId(threadId),
    encodeThreadId: (value: { chatGuid: string; phone?: string }) => threadCodec.encodeThreadId(value),
    postMessage: async () => ({ id: "unused" }),
    fetchMessages: async () => ({ messages: [], hasMore: false }),
    addReaction: async () => {}, removeReaction: async () => {}, deleteMessage: async () => {},
    editMessage: async () => ({ id: "unused" }), startTyping: async () => {}, renderFormatted: () => "",
  } as unknown as GatewayAdapter;
  return {
    adapter,
    get initialized() { return initialized; },
    get disconnected() { return disconnected; },
    get disconnectCalls() { return disconnectCalls; },
    get listenerStarted() { return listenerStarted; },
    get listenerStopped() { return listenerStopped; },
  };
}

function appWithDb(db: DatabaseSync) {
  db.exec("CREATE TABLE admitted_events (event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, text TEXT)");
  let closeCalls = 0;
  const app: GatewayApplication = {
    receive(inbound) {
      db.prepare("INSERT INTO admitted_events(event_id,account_id,text) VALUES(?,?,?)").run(inbound.eventId, inbound.accountId, inbound.text ?? "");
      return Promise.resolve({ status: "accepted" });
    },
    async close() { closeCalls++; },
  };
  return { app, get closeCalls() { return closeCalls; } };
}

test("configured DM only: invalid sender, group, thread, and contentless receipt never reach receive", async () => {
  const db = new DatabaseSync(":memory:");
  const app = appWithDb(db);
  const fake = fakePhoton();
  const gateway = await startOwnerGateway({ manifest, application: app.app, database: db, adapter: fake.adapter, ownership: new PollerOwnership() });
  assert.equal(fake.initialized, true);
  assert.equal(fake.listenerStarted, true);
  await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message("foreign", "+15550000000"));
  await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message("self-outbound", manifest.ownerId, { id: "self-outbound" }, true));
  await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message("receipt", manifest.ownerId, { receiptOnly: true }));
  await gateway.chat.processMessage(fake.adapter, "imessage:iMessage;group;room", message("group"));
  await gateway.chat.processMessage(fake.adapter, "iMessage;-;+15559999999", message("other-thread"));
  assert.equal((db.prepare("SELECT count(*) AS n FROM admitted_events").get() as { n: number }).n, 0);
  assert.equal(gateway.lifecycle, "active");
  assert.equal(gateway.providerFlow, "not_observed");
  assert.equal(gateway.intake.receivedEvents, 5);
  assert.equal(gateway.intake.handedOffEvents, 0);
  await gateway.close();
  assert.equal(fake.listenerStopped, true);
  assert.equal(fake.disconnected, true);
  assert.equal(app.closeCalls, 1);
  db.close();
});

test('pinned Photon public decoder recognizes the configured DM; only inbound owner author is admitted', async () => {
  const db = new DatabaseSync(':memory:');
  const app = appWithDb(db);
  const fake = fakePhoton();
  assert.deepEqual(fake.adapter.decodeThreadId!(manifest.providerThreadId), { chatGuid: 'iMessage;-;+15551234567' });
  assert.equal(fake.adapter.isDM!(manifest.providerThreadId), true);
  const gateway = await startOwnerGateway({ manifest, application: app.app, database: db, adapter: fake.adapter, ownership: new PollerOwnership() });
  await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message('inbound-owner'));
  await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message('outbound-owner', manifest.ownerId, { id: 'outbound-owner' }, true));
  assert.deepEqual(db.prepare('SELECT event_id FROM admitted_events ORDER BY event_id').all()
    .map(row => String((row as { event_id: string }).event_id)), ['inbound-owner']);
  await gateway.close();
  db.close();
});

test('explicit Cloud discovery accepts only an incoming owner DM on the expected physical line', async () => {
  const db = new DatabaseSync(':memory:');
  const fake = fakePhoton();
  const received: string[] = [];
  const application: GatewayApplication = {
    receive(_inbound, provider) { received.push(provider?.threadId ?? 'missing'); return Promise.resolve({ status: 'queued' }); },
    close: async () => {},
  };
  const discovery = { ...manifest, providerThreadId: undefined, expectedProviderLine: '+15550002222' };
  const gateway = await startOwnerGateway({ manifest: discovery, application, database: db,
    adapter: fake.adapter, ownership: new PollerOwnership() });
  const ownerThread = 'imessage:iMessage;-;+15551234567~+15550002222';
  const wrongLine = 'imessage:iMessage;-;+15551234567~+15550003333';
  const group = 'imessage:iMessage;+;group~+15550002222';
  for (const [id, thread, author, isMe, raw] of [
    ['wrong-line', wrongLine, manifest.ownerId, false, { id: 'wrong-line' }],
    ['group', group, manifest.ownerId, false, { id: 'group' }],
    ['foreign', ownerThread, '+15550001111', false, { id: 'foreign' }],
    ['outbound', ownerThread, manifest.ownerId, true, { id: 'outbound' }],
    ['receipt', ownerThread, manifest.ownerId, false, { receiptOnly: true }],
  ] as const) await gateway.chat.processMessage(fake.adapter, thread, message(id, author, raw, isMe, thread));
  assert.deepEqual(received, []);
  await gateway.chat.processMessage(fake.adapter, ownerThread,
    message('empty', manifest.ownerId, { id: 'empty' }, false, ownerThread, ''));
  assert.deepEqual(received, []);
  await gateway.chat.processMessage(fake.adapter, ownerThread, message('first-owner', manifest.ownerId,
    { id: 'first-owner' }, false, ownerThread));
  assert.deepEqual(received, [ownerThread]);
  await gateway.close();
  db.close();
});

test("valid message is durably admitted synchronously and model turn is not awaited at intake", async () => {
  const db = new DatabaseSync(":memory:");
  db.exec("CREATE TABLE admitted_events (event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, text TEXT)");
  let releaseTurn!: () => void;
  let receives = 0;
  const application: GatewayApplication = {
    receive(inbound) {
      receives++;
      db.prepare("INSERT INTO admitted_events(event_id,account_id,text) VALUES(?,?,?)").run(inbound.eventId, inbound.accountId, inbound.text ?? "");
      return new Promise<void>((resolve) => { releaseTurn = resolve; });
    },
    async close() { releaseTurn(); },
  };
  const fake = fakePhoton();
  const gateway = await startOwnerGateway({ manifest, application, database: db, adapter: fake.adapter, ownership: new PollerOwnership() });
  const task = gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message("owner-event"));
  assert.equal((db.prepare("SELECT count(*) AS n FROM admitted_events WHERE event_id='owner-event'").get() as { n: number }).n, 1);
  await task;
  assert.equal(receives, 1);
  assert.equal(gateway.lifecycle, "active");
  releaseTurn();
  await gateway.close();
  db.close();
});

test('media-only owner DM reaches admission with sanitized Photon descriptor; foreign media does not', async () => {
  const db = new DatabaseSync(':memory:');
  const received: Array<{ senderId: string; media?: unknown[] }> = [];
  const application: GatewayApplication = {
    receive(inbound) { received.push({ senderId: inbound.senderId, media: inbound.media }); return Promise.resolve(); },
    async close() {},
  };
  const fake = fakePhoton();
  const gateway = await startOwnerGateway({ manifest, application, database: db, adapter: fake.adapter, ownership: new PollerOwnership() });
  const raw = { content: { type: 'attachment', id: 'att-guid', mimeType: 'image/png', name: 'photo.png', size: 4,
    stream: async () => { throw Error('intake must not download'); } } };
  const mediaMessage = (author: string) => new Message({
    id: 'photon-media-1', threadId: manifest.providerThreadId, text: '',
    formatted: { type: 'root', children: [] }, raw,
    author: { userId: author, userName: author, fullName: author, isBot: false, isMe: false },
    metadata: { dateSent: new Date(), edited: false },
    attachments: [{ type: 'image', mimeType: 'image/png', name: 'photo.png', size: 4, url: 'https://untrusted.example/photo' }],
  });
  await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, mediaMessage('+15550000000'));
  await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, mediaMessage(manifest.ownerId));
  assert.equal(received.length, 1);
  assert.deepEqual(received[0]?.media, [{ mimeType: 'image/png', filename: 'photo.png', sizeBytes: 4,
    source: { version: 1, provider: 'photon', messageId: 'photon-media-1', path: [], contentKind: 'attachment', attachmentId: 'att-guid' },
  }]);
  assert.equal(JSON.stringify(received).includes('untrusted.example'), false);
  await gateway.close();
  db.close();
});

test("explicit manifest and credential binding are required", async () => {
  const db = new DatabaseSync(":memory:");
  const app = appWithDb(db);
  const fake = fakePhoton();
  await assert.rejects(startOwnerGateway({
    manifest: { ...manifest, credentials: {} }, application: app.app, database: db, adapter: fake.adapter,
    ownership: new PollerOwnership(),
  }), /Explicit Photon credentials/);
  await assert.rejects(startOwnerGateway({
    manifest: { ...manifest, channel: "telegram", ownerId: "8", credentials: {
      telegramToken: "explicit-bot-token", telegramLinkFile: { bot_token: "explicit-bot-token", allowed_user: 7 },
    } }, application: app.app, database: db, adapter: fake.adapter,
    ownership: new PollerOwnership(),
  }), /Channel credential owner/);
  assert.equal(fake.initialized, false);
  db.close();
});

test("listener startup failure shuts down adapter/application and releases ownership", async () => {
  const db = new DatabaseSync(":memory:");
  const app = appWithDb(db);
  const fake = fakePhoton({ failStart: true });
  const ownership = new PollerOwnership();
  await assert.rejects(startOwnerGateway({ manifest, application: app.app, database: db, adapter: fake.adapter, ownership }), /HTTP 500/);
  assert.equal(fake.disconnected, true);
  assert.equal(app.closeCalls, 1);
  const next = await startOwnerGateway({ manifest, application: app.app, database: db, adapter: fakePhoton().adapter, ownership });
  await next.close();
  db.close();
});

test("SQLite setup failure does not claim ownership or initialize the provider", async () => {
  const closedDb = new DatabaseSync(":memory:");
  closedDb.close();
  const ownership = new PollerOwnership();
  const first = fakePhoton();
  const application: GatewayApplication = { receive: async () => {}, close: async () => {} };
  await assert.rejects(startOwnerGateway({ manifest, application, database: closedDb,
    adapter: first.adapter, ownership }));
  assert.equal(ownership.owner, undefined);
  assert.equal(first.initialized, false);
  const db = new DatabaseSync(":memory:");
  const gateway = await startOwnerGateway({ manifest, application, database: db,
    adapter: fakePhoton().adapter, ownership });
  await gateway.close();
  db.close();
});

test("pre-initialization adapter setup failure releases ownership for retry", async () => {
  const db = new DatabaseSync(":memory:");
  const ownership = new PollerOwnership();
  const first = fakePhoton();
  Object.defineProperty(first.adapter, "disconnect", {
    configurable: true, value: first.adapter.disconnect, writable: false,
  });
  const application: GatewayApplication = { receive: async () => {}, close: async () => {} };
  await assert.rejects(startOwnerGateway({ manifest, application, database: db,
    adapter: first.adapter, ownership }), TypeError);
  assert.equal(ownership.owner, undefined);
  assert.equal(first.initialized, false);
  const gateway = await startOwnerGateway({ manifest, application, database: db,
    adapter: fakePhoton().adapter, ownership });
  await gateway.close();
  db.close();
});

test("listener rejection reports failure but a successful public disconnect proves provider stop", async () => {
  const db = new DatabaseSync(":memory:");
  const app = appWithDb(db);
  const fake = fakePhoton({ failListenerOnStop: true });
  const ownership = new PollerOwnership();
  const gateway = await startOwnerGateway({ manifest, application: app.app, database: db, adapter: fake.adapter, ownership });
  await assert.rejects(gateway.close(), /Gateway cleanup did not complete cleanly/);
  assert.equal(fake.disconnected, true);
  assert.equal(fake.disconnectCalls, 1);
  assert.equal(gateway.providerStopped, true);
  assert.equal(app.closeCalls, 1);
  await assert.rejects(gateway.close(), /already attempted/);
  assert.equal(fake.disconnectCalls, 1);
  const next = await startOwnerGateway({ manifest, application: app.app, database: db, adapter: fakePhoton().adapter, ownership });
  await next.close();
  db.close();
});

test('Photon abort without public disconnect leaves provider stop unconfirmed and retains ownership', async () => {
  const db = new DatabaseSync(':memory:');
  const app = appWithDb(db);
  const fake = fakePhoton({ noDisconnect: true });
  const ownership = new PollerOwnership();
  const gateway = await startOwnerGateway({ manifest, application: app.app, database: db, adapter: fake.adapter, ownership });
  await assert.rejects(gateway.close(), /Photon provider stop is unconfirmed/);
  assert.equal(fake.listenerStopped, true); // waitUntil observed abort, not pump completion
  assert.equal(gateway.providerStopped, false);
  await assert.rejects(startOwnerGateway({ manifest, application: app.app, database: db,
    adapter: fakePhoton().adapter, ownership }), /poller already owned/);
  db.close();
});

test('manual Photon close awaits the one public app.stop and can release local consumer ownership', async () => {
  const db = new DatabaseSync(':memory:');
  const app = appWithDb(db);
  const fake = fakePhoton({ noDisconnect: true });
  let stopCalls = 0;
  Object.assign(fake.adapter, { app: { stop: async () => { stopCalls++; } } });
  const ownership = new PollerOwnership();
  const gateway = await startOwnerGateway({ manifest, application: app.app, database: db, adapter: fake.adapter, ownership });
  await gateway.close();
  assert.equal(stopCalls, 1);
  assert.equal(gateway.providerStopped, true);
  const next = await startOwnerGateway({ manifest, application: app.app, database: db,
    adapter: fakePhoton().adapter, ownership });
  await next.close();
  db.close();
});

test('failed public Photon stop cannot release ownership without another successful stop capability', async () => {
  const db = new DatabaseSync(':memory:');
  const app = appWithDb(db);
  const fake = fakePhoton({ noDisconnect: true });
  Object.assign(fake.adapter, { app: { stop: async () => { throw new Error('private provider URL'); } } });
  const ownership = new PollerOwnership();
  const gateway = await startOwnerGateway({ manifest, application: app.app, database: db, adapter: fake.adapter, ownership });
  await assert.rejects(gateway.close(), /Photon provider stop is unconfirmed/);
  assert.equal(gateway.providerStopped, false);
  await assert.rejects(startOwnerGateway({ manifest, application: app.app, database: db,
    adapter: fakePhoton().adapter, ownership }), /poller already owned/);
  db.close();
});

test('signal race cannot turn a second immediate app.stop into proof of first stop completion', async () => {
  const db = new DatabaseSync(':memory:');
  const fake = fakePhoton({ noDisconnect: true });
  let finishFirst!: () => void;
  const first = new Promise<void>(resolve => { finishFirst = resolve; });
  let calls = 0;
  const spectrumLike = { stop: () => ++calls === 1 ? first : Promise.resolve() };
  Object.assign(fake.adapter, { app: spectrumLike });
  let retained: boolean | undefined;
  const application: GatewayApplication = { receive: async () => {},
    close: async options => { retained = options?.retainOwnership; } };
  const ownership = new PollerOwnership();
  const gateway = await startOwnerGateway({ manifest, application, database: db, adapter: fake.adapter, ownership });
  // Fake-only: Spectrum's first signal stop has started but not finished.
  process.emit('SIGTERM');
  void spectrumLike.stop();
  await assert.rejects(gateway.close(), /Photon provider stop is unconfirmed/);
  assert.equal(calls, 2);
  assert.equal(gateway.providerStopped, false);
  assert.equal(retained, true);
  await assert.rejects(startOwnerGateway({ manifest, application, database: db,
    adapter: fakePhoton().adapter, ownership }), /poller already owned/);
  finishFirst();
  db.close();
});

test('Chat swallowed disconnect rejection never counts as shutdown proof or logs provider secrets', async () => {
  const db = new DatabaseSync(':memory:');
  const app = appWithDb(db);
  const fake = fakePhoton({ failDisconnect: true });
  const ownership = new PollerOwnership();
  const seen: string[] = [];
  const originalError = console.error;
  console.error = (...parts: unknown[]) => { seen.push(parts.map(String).join(' ')); };
  try {
    const gateway = await startOwnerGateway({ manifest, application: app.app, database: db,
      adapter: fake.adapter, ownership });
    await assert.rejects(gateway.close(), /Photon provider stop is unconfirmed/);
    assert.equal(gateway.providerStopped, false);
    assert.equal(fake.disconnectCalls, 1);
    assert.equal(seen.some(line => line.includes('secret-provider-url')), false);
    await assert.rejects(startOwnerGateway({ manifest, application: app.app, database: db,
      adapter: fakePhoton().adapter, ownership }), /poller already owned/);
  } finally { console.error = originalError; db.close(); }
});

test('a listener that throws during first start is an attempted consumer, not a clean stop', async () => {
  const db = new DatabaseSync(':memory:');
  const app = appWithDb(db);
  const fake = fakePhoton({ noDisconnect: true, throwAfterStart: true });
  const ownership = new PollerOwnership();
  await assert.rejects(startOwnerGateway({ manifest, application: app.app, database: db,
    adapter: fake.adapter, ownership }), (error: unknown) =>
    error instanceof Error && error.name === 'GatewayStartupError' &&
    (error as { providerStopped?: boolean }).providerStopped === false);
  assert.equal(fake.listenerStarted, true);
  await assert.rejects(startOwnerGateway({ manifest, application: app.app, database: db,
    adapter: fakePhoton().adapter, ownership }), /poller already owned/);
  db.close();
});

test("close cancels the application before joining a blocked turn", async () => {
  const db = new DatabaseSync(":memory:");
  db.exec("CREATE TABLE admitted_events (event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, text TEXT)");
  let finishTurn!: () => void;
  const turn = new Promise<void>((resolve) => { finishTurn = resolve; });
  let appClosed = false;
  const application: GatewayApplication = {
    receive(inbound) {
      db.prepare("INSERT INTO admitted_events(event_id,account_id,text) VALUES(?,?,?)").run(inbound.eventId, inbound.accountId, inbound.text ?? "");
      return turn;
    },
    async close() { appClosed = true; finishTurn(); },
  };
  const fake = fakePhoton();
  const gateway = await startOwnerGateway({ manifest, application, database: db, adapter: fake.adapter, ownership: new PollerOwnership() });
  await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message("blocked-turn"));
  await gateway.close();
  assert.equal(appClosed, true);
  assert.equal(fake.disconnected, true);
  db.close();
});


test('gateway closes intake and drains presence before stopping the Photon provider', async () => {
  const db = new DatabaseSync(':memory:');
  const app = appWithDb(db);
  const fake = fakePhoton();
  let finish!: () => void;
  let entered!: () => void;
  const ready = new Promise<void>(resolve => { entered = resolve; });
  const gate = new Promise<void>(resolve => { finish = resolve; });
  const gateway = await startOwnerGateway({ manifest, application: app.app, database: db,
    adapter: fake.adapter, ownership: new PollerOwnership(),
    quiescePresence: async () => { entered(); await gate; },
  });
  const closing = gateway.close();
  await ready;
  await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message('during-close'));
  assert.equal((db.prepare('SELECT count(*) AS n FROM admitted_events').get() as { n: number }).n, 0);
  assert.equal(fake.listenerStopped, false);
  assert.equal(fake.disconnected, false);
  finish();
  await closing;
  assert.equal(fake.listenerStopped, true);
  assert.equal(fake.disconnected, true);
  db.close();
});

test('failed Photon startup quiesces presence before provider cleanup', async () => {
  const db = new DatabaseSync(':memory:');
  const app = appWithDb(db);
  const fake = fakePhoton({ failStart: true });
  let quiesced = false;
  await assert.rejects(startOwnerGateway({ manifest, application: app.app, database: db,
    adapter: fake.adapter, ownership: new PollerOwnership(),
    quiescePresence: async () => { assert.equal(fake.disconnected, false); quiesced = true; },
  }));
  assert.equal(quiesced, true);
  assert.equal(fake.disconnected, true);
  db.close();
});

test('actual Photon reply content stays bound through owner normalization', async () => {
  const db = new DatabaseSync(':memory:');
  const fake = fakePhoton();
  const received: {bound?: boolean; id?: string; at?: number}[] = [];
  const gateway = await startOwnerGateway({manifest, database: db, adapter: fake.adapter,
    ownership: new PollerOwnership(), application: {
      receive(inbound) { received.push({bound: inbound.replyBound, id: inbound.replyToMessageId, at: inbound.providerSentAtMs}); return Promise.resolve({status: 'queued'}); },
      async close() {},
    }});
  try {
    const raw = {content: {type: 'reply', target: {id: 'assistant-message'}, content: {type: 'text', text: 'Give me my brief'}}};
    await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message('bound-brief', manifest.ownerId, raw, false, manifest.providerThreadId, 'Give me my brief'));
    await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message('plain-brief', manifest.ownerId, {content: {type: 'text', text: 'Give me my brief'}}, false, manifest.providerThreadId, 'Give me my brief'));
    const conflicting = message('conflicting-reply', manifest.ownerId, raw);
    conflicting.replyTo = message('other-assistant-message');
    await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, conflicting);
    const crossThread = message('cross-thread-reply', manifest.ownerId, raw);
    crossThread.replyTo = message('assistant-message', manifest.ownerId, {}, false, 'imessage:other-thread');
    await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, crossThread);
    assert.equal(received[0]!.bound, true);
    assert.equal(received[0]!.id, 'assistant-message');
    assert.ok(Number.isSafeInteger(received[0]!.at));
    assert.deepEqual(received[1], {bound: undefined, id: undefined, at: undefined});
    assert.equal(received[2]!.bound, true);
    assert.equal(received[2]!.id, undefined);
    assert.equal(received[3]!.bound, true);
    assert.equal(received[3]!.id, undefined);
  } finally { await gateway.close(); db.close(); }
});

test('explicit shared project admits only canonical shared owner DM; physical mode still refuses it', async () => {
  for (const shared of [true, false]) {
    const db = new DatabaseSync(':memory:');
    const app = appWithDb(db), fake = fakePhoton();
    const {providerThreadId: _fixed, ...base} = manifest;
    const gateway = await startOwnerGateway({manifest: {...base,
      ...(shared ? {sharedProjectRoute: true} : {expectedProviderLine: '+15557654321'})},
      application: app.app, database: db, adapter: fake.adapter, ownership: new PollerOwnership()});
    try {
      // Pinned Spectrum shared token emits the sentinel; use the real adapter codec.
      const thread = fake.adapter.encodeThreadId!({chatGuid: `any;-;${manifest.ownerId}`, phone: 'shared'});
      await gateway.chat.processMessage(fake.adapter, thread, message('shared-owner', manifest.ownerId, {}, false, thread));
      await gateway.chat.processMessage(fake.adapter, thread, message('foreign-shared', '+15550000000', {}, false, thread));
      await gateway.chat.processMessage(fake.adapter, thread, message('self-shared', manifest.ownerId, {}, true, thread));
      const group = fake.adapter.encodeThreadId!({chatGuid: 'any;+;group', phone: 'shared'});
      await gateway.chat.processMessage(fake.adapter, group, message('group-shared', manifest.ownerId, {}, false, group));
      const physical = fake.adapter.encodeThreadId!({chatGuid: `any;-;${manifest.ownerId}`, phone: '+15557654321'});
      await gateway.chat.processMessage(fake.adapter, physical, message('physical-owner', manifest.ownerId, {}, false, physical));
      const rows = db.prepare('SELECT event_id FROM admitted_events').all();
      assert.deepEqual(rows.map(row => row.event_id), [shared ? 'shared-owner' : 'physical-owner']);
    } finally { await gateway.close(); db.close(); }
  }
});


test('installed Spectrum shared token produces a shared space and pinned adapter thread', async () => {
  const script = `
    import assert from 'node:assert/strict';
    process.env.SPECTRUM_IMESSAGE_ADDRESS = '127.0.0.1:1';
    let tokenCalls = 0;
    globalThis.fetch = async (input, init) => {
      const request = new Request(input, init);
      const path = new URL(request.url).pathname;
      let data;
      if (path === '/projects/synthetic/') data = {id:'synthetic',name:'synthetic',profile:{},slug:'synthetic'};
      else if (path === '/projects/synthetic/platforms') data = {};
      else if (path === '/projects/synthetic/imessage/tokens' && request.method === 'POST') {
        tokenCalls++; data = {type:'shared',token:'synthetic',expiresIn:3600};
      } else throw new Error('Unexpected synthetic SDK request');
      return Response.json({succeed:true,data});
    };
    const {Spectrum} = await import('@spectrum-ts/core');
    const {imessage} = await import('@spectrum-ts/imessage');
    const {createiMessageAdapter} = await import('@photon-ai/chat-adapter-imessage');
    const app = await Spectrum({providers:[imessage.config({})],projectId:'synthetic',projectSecret:'synthetic',options:{logLevel:'silent'}});
    try {
      const space = await imessage(app).space.get('any;-;+15550001111',{phone:'+15557654321'});
      assert.equal(space.phone,'shared');
      const codec = createiMessageAdapter({projectId:'synthetic',projectSecret:'synthetic'});
      assert.equal(codec.encodeThreadId({chatGuid:space.id,phone:space.phone}),'imessage:any;-;+15550001111~shared');
      assert.equal(tokenCalls,1);
    } finally {await app.stop();}
  `;
  await promisify(execFile)(process.execPath, ['--input-type=module', '-e', script], {cwd:process.cwd(), timeout:15000});
});

test('unbound native Photon timing travels separately while ordinary admitted payload is unchanged', async () => {
  const fake = fakePhoton();
  const received: {inbound: unknown; sentAt?: number}[] = [];
  const db = new DatabaseSync(':memory:');
  const gateway = await startOwnerGateway({manifest, database: db, adapter: fake.adapter, ownership: new PollerOwnership(), application: {
    receive(inbound, provider) {received.push({inbound, sentAt: provider?.sentAtMs}); return Promise.resolve({status: 'queued'});},
    async close() {},
  }});
  try {
    const stamp = new Date('2026-10-04T12:00:00Z');
    const native = message('native-no', manifest.ownerId, {id: 'native-no', direction: 'inbound', timestamp: stamp}, false, manifest.providerThreadId, 'No.');
    await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, native);
    await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId, message('fallback-no', manifest.ownerId, {id: 'fallback-no', direction: 'inbound', timestamp: stamp.toISOString()}, false, manifest.providerThreadId, 'No.'));
    assert.equal(received[0].sentAt, stamp.getTime());
    assert.equal((received[0].inbound as {providerSentAtMs?: number}).providerSentAtMs, undefined);
    assert.equal(received[1].sentAt, undefined);
  } finally {await gateway.close(); db.close();}
});

test('inbound text truncation at the gateway bound never splits a surrogate pair', async () => {
  const db = new DatabaseSync(":memory:");
  const texts: string[] = [];
  const application: GatewayApplication = {
    receive(inbound) { texts.push(inbound.text ?? ''); return Promise.resolve({ status: 'accepted' }); },
    async close() {},
  };
  const fake = fakePhoton();
  const gateway = await startOwnerGateway({ manifest, application, database: db, adapter: fake.adapter, ownership: new PollerOwnership() });
  try {
    // 15,999 ASCII units, then an emoji whose high surrogate is unit 16,000.
    const long = 'a'.repeat(15_999) + '🌲' + 'tail';
    await gateway.chat.processMessage(fake.adapter, manifest.providerThreadId,
      message('long-owner-event', manifest.ownerId, { id: 'long-owner-event' }, false, manifest.providerThreadId, long));
    assert.equal(texts.length, 1);
    assert.equal(texts[0], 'a'.repeat(15_999));
    assert.equal(Buffer.from(texts[0]!, 'utf8').toString('utf8'), texts[0]);
    assert.doesNotMatch(texts[0]!, /[\ud800-\udbff]$/);
  } finally { await gateway.close(); db.close(); }
});

test('real Photon SDK pump termination fails readiness despite its unresolved timer promise', async () => {
  const db = new DatabaseSync(':memory:');
  const application = appWithDb(db);
  const adapter = createPhotonSdkAdapter(manifest.credentials);
  let failStream!: () => void;
  const failure = new Promise<void>(resolve => { failStream = resolve; });
  let stops = 0;
  // Exercise the installed SDK pump; replace only the remote Spectrum source.
  adapter.app = { messages: (async function* () {
    await failure;
    throw new Error('private message or authenticated URL must never escape');
  })(), stop: async () => { stops++; } } as unknown as NonNullable<typeof adapter.app>;
  const gateway = await startOwnerGateway({ manifest, application: application.app, database: db,
    adapter, ownership: new PollerOwnership() });
  try {
    assert.equal(gateway.lifecycle, 'active');
    failStream();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(gateway.lifecycle, 'failed');
    assert.deepEqual(gateway.intake, {listenerStarts: 1, listenerStops: 1,
      streamErrors: 1, handlerErrors: 0, receivedEvents: 0, handedOffEvents: 0});
  } finally { await gateway.close(); db.close(); }
  assert.equal(stops, 1);
});
