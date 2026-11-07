import test from 'node:test';
import assert from 'node:assert/strict';
import { Message } from 'chat';
import { createInboundMediaHydrator } from '../src/channels/media-hydration.js';
import type { Inbound, Media } from '../src/channels/types.js';

const png = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2]);
const ogg = Uint8Array.from([0x4f, 0x67, 0x67, 0x53, 1, 2, 3]);
const owner = { channel: 'telegram' as const, accountId: 'bot-1', ownerId: '42' };
const inbound = (media: Media[], channel: 'telegram' | 'photon' = 'telegram'): Inbound => ({
  channel, accountId: channel === 'telegram' ? 'bot-1' : 'line-1', senderId: '42',
  eventId: channel === 'telegram' ? 'owner-event-1' : 'msg-1', isDirect: true, media,
});
const current = () => true;

test('Telegram hydration uses only fixed API routes, streamed bounds, and album file IDs', async () => {
  const called: Array<{ url: string; init: RequestInit }> = [];
  const transport = async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    called.push({ url, init: init ?? {} });
    if (url.endsWith('/getFile')) {
      const fileId = JSON.parse(String(init?.body)).file_id as string;
      return Response.json({ ok: true, result: { file_path: `photos/${fileId}.png` } });
    }
    return new Response(png);
  };
  const hydrate = createInboundMediaHydrator({ channel: 'telegram', owner, providerThreadId: 'telegram:42', adapter: {},
    credentials: { telegramToken: '123:abc', telegramLinkFile: { bot_token: '123:abc', allowed_user: 42 } },
    fetch: transport as typeof fetch });
  const items: Media[] = [
    { mimeType: 'image/png', source: { version: 1, provider: 'telegram', fileId: 'albumA' } },
    { mimeType: 'image/png', source: { version: 1, provider: 'telegram', fileId: 'albumB' } },
    { omission: 'too_many', omittedCount: 3 },
  ];
  const result = await hydrate([inbound(items)], { signal: new AbortController().signal, isCurrent: current });
  assert.deepEqual(result[0]?.media?.slice(0, 2).map(item => [...item.data ?? []]), [[...png], [...png]]);
  assert.deepEqual(result[0]?.media?.[2], items[2]);
  assert.deepEqual(called.map(call => call.url), [
    'https://api.telegram.org/bot123:abc/getFile', 'https://api.telegram.org/file/bot123:abc/photos/albumA.png',
    'https://api.telegram.org/bot123:abc/getFile', 'https://api.telegram.org/file/bot123:abc/photos/albumB.png',
  ]);
  assert.ok(called.every(call => call.init.redirect === 'error'));
});

test('Telegram rejects URL escapes, oversized bodies and wrong owner without fetching arbitrary URLs', async () => {
  const called: string[] = [];
  let cancelCount = 0;
  const transport = async (input: string | URL | Request) => {
    called.push(String(input));
    if (String(input).endsWith('/getFile')) return Response.json({ ok: true, result: { file_path: '../evil' } });
    return new Response(new ReadableStream({ cancel() { cancelCount++; } }));
  };
  const hydrate = createInboundMediaHydrator({ channel: 'telegram', owner, providerThreadId: 'telegram:42', adapter: {},
    credentials: { telegramToken: '123:abc', telegramLinkFile: { bot_token: '123:abc', allowed_user: 42 } },
    fetch: transport as typeof fetch });
  await assert.rejects(hydrate([{ ...inbound([]), senderId: '43' }], { signal: new AbortController().signal, isCurrent: current }), /owner context/);
  assert.equal(called.length, 0);
  const result = await hydrate([inbound([{ mimeType: 'image/png', url: 'https://attacker.test/file',
    source: { version: 1, provider: 'telegram', fileId: 'safe' } }])],
  { signal: new AbortController().signal, isCurrent: current });
  assert.equal(result[0]?.media?.[0]?.omission, 'unavailable');
  assert.equal(called.length, 1);
  assert.equal(cancelCount, 0);
});

function photonMessage(content: unknown, overrides: Record<string, unknown> = {}) {
  return new Message({
    id: 'msg-1', threadId: 'imessage:owner', text: '', raw: { id: 'msg-1', sender: { id: '42' }, direction: 'inbound', content },
    formatted: { type: 'root', children: [] },
    author: { userId: '42', userName: 'owner', fullName: 'Owner', isBot: false, isMe: false },
    metadata: { dateSent: new Date(), edited: false }, attachments: [], ...overrides,
  });
}

test('Photon resolves an exact nested group path and GUID using the fixed thread', async () => {
  const calls: Array<[string, string]> = [];
  let cancelled = false;
  const content = { type: 'group', items: [{ content: { type: 'reply', content: {
    type: 'attachment', id: 'attachment-1', mimeType: 'image/png', name: 'one.png', size: png.byteLength,
    stream: async () => new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(png); controller.close(); }, cancel() { cancelled = true; } }),
  } } }] };
  const adapter = { fetchMessage: async (thread: string, id: string) => { calls.push([thread, id]); return photonMessage(content); } };
  const hydrate = createInboundMediaHydrator({ channel: 'photon', owner: { channel: 'photon', accountId: 'line-1', ownerId: '42' },
    providerThreadId: 'imessage:owner', adapter, credentials: { photonProjectId: 'project', photonProjectSecret: 'secret' } });
  const result = await hydrate([inbound([{ mimeType: 'image/png', filename: 'one.png', sizeBytes: png.byteLength,
    source: { version: 1, provider: 'photon', messageId: 'msg-1', path: [0, 'reply'], contentKind: 'attachment', attachmentId: 'attachment-1' },
  }], 'photon')], { signal: new AbortController().signal, isCurrent: current });
  assert.deepEqual(calls, [['imessage:owner', 'msg-1']]);
  assert.deepEqual([...result[0]!.media![0]!.data!], [...png]);
  assert.equal(cancelled, false);
});

test('Photon refuses a global-cache wrong-thread result and supports GUID-less voice with exact metadata', async () => {
  let reads = 0;
  const voice = { type: 'voice', mimeType: 'audio/ogg', stream: async () => {
    reads++;
    return new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(ogg); controller.close(); } });
  } };
  let wrong = true;
  const adapter = { fetchMessage: async () => wrong
    ? photonMessage(voice, { threadId: 'imessage:other' }) : photonMessage(voice) };
  const hydrate = createInboundMediaHydrator({ channel: 'photon', owner: { channel: 'photon', accountId: 'line-1', ownerId: '42' },
    providerThreadId: 'imessage:owner', adapter, credentials: { photonProjectId: 'project', photonProjectSecret: 'secret' } });
  const item: Media = { mimeType: 'audio/ogg', filename: 'voice', sizeBytes: 0,
    source: { version: 1, provider: 'photon', messageId: 'msg-1', path: [], contentKind: 'voice' } };
  const first = await hydrate([inbound([item], 'photon')], { signal: new AbortController().signal, isCurrent: current });
  assert.equal(first[0]?.media?.[0]?.omission, 'unavailable');
  assert.equal(reads, 0);
  wrong = false;
  const second = await hydrate([inbound([item], 'photon')], { signal: new AbortController().signal, isCurrent: current });
  assert.deepEqual([...second[0]!.media![0]!.data!], [...ogg]);
});

test('Photon cancelled lookup keeps its concurrency slot until underlying SDK promise settles', async () => {
  let resolveLookup: ((value: ReturnType<typeof photonMessage>) => void) | undefined;
  let calls = 0;
  const adapter = { fetchMessage: () => { calls++; return new Promise<ReturnType<typeof photonMessage>>(resolve => { resolveLookup = resolve; }); } };
  const hydrate = createInboundMediaHydrator({ channel: 'photon', owner: { channel: 'photon', accountId: 'line-1', ownerId: '42' },
    providerThreadId: 'imessage:owner', adapter, credentials: { photonProjectId: 'project', photonProjectSecret: 'secret' } });
  const item: Media = { mimeType: 'image/png', source: { version: 1, provider: 'photon', messageId: 'msg-1', path: [], contentKind: 'attachment', attachmentId: 'attachment-1' } };
  const controller = new AbortController();
  const first = hydrate([inbound([item], 'photon')], { signal: controller.signal, isCurrent: current });
  await new Promise<void>(resolve => setImmediate(resolve));
  controller.abort();
  await assert.rejects(first, /cancelled/);
  const second = await hydrate([inbound([item], 'photon')], { signal: new AbortController().signal, isCurrent: current });
  assert.equal(second[0]?.media?.[0]?.omission, 'unavailable');
  assert.equal(calls, 1);
  resolveLookup?.(photonMessage({ type: 'attachment', id: 'attachment-1' }));
  await new Promise<void>(resolve => setImmediate(resolve));
  assert.equal(calls, 1);
});

test('Photon cancellation during stream acquisition cancels the late stream and makes no further lookup', async () => {
  let releaseStream: ((stream: ReadableStream<Uint8Array>) => void) | undefined;
  let cancelled = 0;
  let lookups = 0;
  const content = { type: 'attachment', id: 'attachment-1', mimeType: 'image/png', size: png.byteLength,
    stream: () => new Promise<ReadableStream<Uint8Array>>(resolve => { releaseStream = resolve; }) };
  const adapter = { fetchMessage: async () => { lookups++; return photonMessage(content); } };
  const hydrate = createInboundMediaHydrator({ channel: 'photon', owner: { channel: 'photon', accountId: 'line-1', ownerId: '42' },
    providerThreadId: 'imessage:owner', adapter, credentials: { photonProjectId: 'project', photonProjectSecret: 'secret' } });
  const item: Media = { mimeType: 'image/png', sizeBytes: png.byteLength,
    source: { version: 1, provider: 'photon', messageId: 'msg-1', path: [], contentKind: 'attachment', attachmentId: 'attachment-1' } };
  const controller = new AbortController();
  const first = hydrate([inbound([item], 'photon')], { signal: controller.signal, isCurrent: current });
  await new Promise<void>(resolve => setImmediate(resolve));
  assert.equal(typeof releaseStream, 'function');
  controller.abort();
  await assert.rejects(first, /cancelled/);
  const second = await hydrate([inbound([item], 'photon')], { signal: new AbortController().signal, isCurrent: current });
  assert.equal(second[0]?.media?.[0]?.omission, 'unavailable');
  assert.equal(lookups, 1);
  releaseStream?.(new ReadableStream<Uint8Array>({ cancel() { cancelled++; } }));
  await new Promise<void>(resolve => setImmediate(resolve));
  assert.equal(cancelled, 1);
});

test('rejected streamed bytes still consume the shared turn budget and omissions use attachment positions', async () => {
  let downloads = 0;
  const junk = new Uint8Array(8 * 1024 * 1024);
  const transport = async (input: string | URL | Request) => {
    if (String(input).endsWith('/getFile')) return Response.json({ ok: true, result: { file_path: 'photos/a.png' } });
    downloads++;
    return new Response(junk);
  };
  const hydrate = createInboundMediaHydrator({ channel: 'telegram', owner, providerThreadId: 'telegram:42', adapter: {},
    credentials: { telegramToken: '123:abc', telegramLinkFile: { bot_token: '123:abc', allowed_user: 42 } },
    fetch: transport as typeof fetch });
  const item: Media = { mimeType: 'image/png', source: { version: 1, provider: 'telegram', fileId: 'a' } };
  const result = await hydrate([inbound([item, item, item])], { signal: new AbortController().signal, isCurrent: current });
  assert.deepEqual(result[0]?.media?.map(media => media.omission), ['invalid_type', 'invalid_type', 'too_large']);
  assert.equal(downloads, 2);
  const positions = await hydrate([inbound([{ omission: 'unsupported_type' }, { omission: 'unavailable' },
    { omission: 'too_many', omittedCount: 4 }, { omission: 'empty' }, { omission: 'invalid_type' }, item])],
  { signal: new AbortController().signal, isCurrent: current });
  assert.equal(positions[0]?.media?.at(-1)?.omission, 'too_many');
  assert.equal(downloads, 2);
});

test('Telegram deadline cancellation cannot turn partial valid bytes into completed media', async () => {
  const originalTimeout = AbortSignal.timeout;
  const deadline = new AbortController();
  let contentStarted = false;
  let contentCancelled = false;
  try {
    AbortSignal.timeout = () => deadline.signal;
    const transport = async (input: string | URL | Request) => {
      if (String(input).endsWith('/getFile')) return Response.json({ ok: true, result: { file_path: 'photos/a.png' } });
      return new Response(new ReadableStream<Uint8Array>({
        start(controller) { controller.enqueue(png); contentStarted = true; },
        cancel() { contentCancelled = true; },
      }));
    };
    const hydrate = createInboundMediaHydrator({ channel: 'telegram', owner, providerThreadId: 'telegram:42', adapter: {},
      credentials: { telegramToken: '123:abc', telegramLinkFile: { bot_token: '123:abc', allowed_user: 42 } },
      fetch: transport as typeof fetch });
    const promise = hydrate([inbound([{ mimeType: 'image/png', source: { version: 1, provider: 'telegram', fileId: 'a' } }])],
      { signal: new AbortController().signal, isCurrent: current });
    for (let attempt = 0; !contentStarted && attempt < 20; attempt++) await new Promise<void>(resolve => setImmediate(resolve));
    assert.equal(contentStarted, true);
    deadline.abort();
    const result = await promise;
    assert.equal(result[0]?.media?.[0]?.data, undefined);
    assert.equal(result[0]?.media?.[0]?.omission, 'timed_out');
    assert.equal(contentCancelled, true);
  } finally {
    AbortSignal.timeout = originalTimeout;
  }
});
