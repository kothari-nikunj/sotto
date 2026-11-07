import test from 'node:test';
import assert from 'node:assert/strict';
import type { ContentInput } from '@spectrum-ts/core';
import { createiMessageAdapter } from '@photon-ai/chat-adapter-imessage';
import { TelegramAdapter } from '@chat-adapter/telegram';
import { convertEmojiPlaceholders } from 'chat';
import { createPhotonTransport, createTelegramTransport, type PhotonAdapterLike } from '../src/channels/transport.js';

test('only root provider message IDs prove Telegram acceptance', async () => {
  const accepted = createTelegramTransport({ postMessage: async () => ({ id: 'tg-provider-message',
    raw: { content: { id: 'unrelated-content' } } }) as never });
  assert.deepEqual(await accepted.send({ threadId: 'telegram:42', text: 'hello' }),
    { acceptance: 'accepted', providerMessageIds: ['tg-provider-message'] });
  const nestedOnly = createTelegramTransport({ postMessage: async () => ({
    content: { items: [{ id: 'uploaded-file-id' }] },
  }) as never });
  assert.deepEqual(await nestedOnly.send({ threadId: 'telegram:42', text: 'hello' }),
    { acceptance: 'unknown', providerMessageIds: [] });
  for (const result of [
    [{ id: 'array-id' }],
    { id: { value: 'nested-id' } },
    { id: '' },
    { id: ' \t ' },
    { id: 'x'.repeat(513) },
  ]) {
    const invalid = createTelegramTransport({ postMessage: async () => result as never });
    assert.deepEqual(await invalid.send({ threadId: 'telegram:42', text: 'hello' }),
      { acceptance: 'unknown', providerMessageIds: [] });
  }
});

test('Telegram chunks preserve literal formatting and graphemes within the receipt budget', async () => {
  const posts: Array<{ thread: string; raw: string }> = [];
  const transport = createTelegramTransport({ postMessage: async (thread, message) => {
    if (typeof message !== 'object' || !message || !('raw' in message)) throw new Error('expected raw Telegram post');
    posts.push({ thread, raw: String(message.raw) });
    return { id: `part-${posts.length}` } as never;
  } });
  const longText = 'x'.repeat(4095) + '👩🏽‍💻e\u0301 *literal* <b>HTML</b> &amp;\n'.repeat(300);
  assert.deepEqual(await transport.send({ threadId: 'telegram:42', text: longText }),
    { acceptance: 'accepted', providerMessageIds: posts.map((_, i) => `part-${i + 1}`) });
  assert.equal(posts[0]!.raw.length, 4095);
  assert.equal(posts.map(post => post.raw).join(''), longText);
  assert.ok(posts.length > 1 && posts.length <= 8);
  for (const post of posts) { assert.equal(post.thread, 'telegram:42'); assert.ok(post.raw.length <= 4096); }
  const boundaries = new Set([...new Intl.Segmenter(undefined, {granularity: 'grapheme'}).segment(longText)].map(segment => segment.index));
  let offset = 0;
  for (const post of posts) { assert.ok(boundaries.has(offset)); offset += post.raw.length; }
});

test('Telegram preflights size and captions before any part is contacted', async () => {
  let calls = 0;
  const transport = createTelegramTransport({postMessage: async () => { calls++; return {id: `${calls}`} as never; }});
  for (const input of [
    {text: 'a'.repeat(32001)},
    {text: 'a' + '\u0301'.repeat(4096)}, // An indivisible oversized grapheme.
    {text: 'a'.repeat(1025), media: [{url: 'https://example.invalid/image.png', mimeType: 'image/png'}]},
  ]) assert.equal((await transport.send({threadId: 'telegram:42', ...input})).acceptance, 'rejected');
  assert.equal(calls, 0);
  assert.equal((await transport.send({threadId: 'telegram:42', text: 'a'.repeat(32000)})).acceptance, 'accepted');
  assert.equal(calls, 8);
  assert.equal((await transport.send({threadId: 'telegram:42', text: '',
    media: [{url: 'https://example.invalid/image.png', mimeType: 'image/png'}]})).acceptance, 'accepted');
});

test('Telegram stops at the first uncertain or repeated root receipt and preserves accepted prefix IDs', async () => {
  for (const result of [undefined, {id: 'first'}, new Error('response lost')]) {
    let calls = 0;
    const transport = createTelegramTransport({postMessage: async () => {
      if (++calls === 1) return {id: 'first'} as never;
      if (result instanceof Error) throw result;
      return result as never;
    }});
    const receipt = await transport.send({threadId: 'telegram:42', text: 'x'.repeat(9000)});
    assert.equal(receipt.acceptance, 'unknown');
    assert.deepEqual(receipt.providerMessageIds, ['first']);
    assert.equal(calls, 2);
  }
});

test('Telegram cancellation prevents new provider contacts and returns the known prefix as unknown', async () => {
  const controller = new AbortController();
  let calls = 0;
  const transport = createTelegramTransport({postMessage: async () => {
    calls++; controller.abort(); return {id: 'contacted-first'} as never;
  }});
  assert.deepEqual(await transport.send({threadId: 'telegram:42', text: 'x'.repeat(9000), signal: controller.signal}),
    {acceptance: 'unknown', providerMessageIds: ['contacted-first'], reason: 'delivery cancelled before next provider contact'});
  assert.equal(calls, 1);
  assert.equal((await transport.send({threadId: 'telegram:42', text: 'next', signal: controller.signal})).acceptance, 'not_attempted');
  assert.equal(calls, 1);
});

test('pinned Telegram SDK sends every chunk untruncated as plain text at the HTTP boundary', async () => {
  const originalFetch = globalThis.fetch;
  const posts: Record<string, unknown>[] = [];
  globalThis.fetch = async (input, init) => {
    const request = new Request(input, init);
    assert.equal(request.url, 'https://api.telegram.org/bot123456:offline-fixture/sendMessage');
    const body = await request.json() as Record<string, unknown>;
    posts.push(body);
    return Response.json({ok: true, result: {message_id: posts.length, date: 1790726400,
      chat: {id: 42, type: 'private'}, text: body.text}});
  };
  try {
    const adapter = new TelegramAdapter({botToken: '123456:offline-fixture', userName: 'fixture'});
    const text = 'x'.repeat(4090) + '{{emoji:thumbs_up}} *literal* &amp; <b>tag</b> 🧭\n'.repeat(130);
    const receipt = await createTelegramTransport(adapter).send({threadId: 'telegram:42', text});
    assert.equal(receipt.acceptance, 'accepted');
    assert.equal(receipt.providerMessageIds.length, posts.length);
    assert.ok(posts.length > 1);
    assert.equal(posts.map(post => post.text).join(''), convertEmojiPlaceholders(text, 'gchat'));
    for (const post of posts) {
      assert.equal(post.chat_id, '42');
      assert.equal(post.parse_mode, undefined);
      assert.equal(post.entities, undefined);
      assert.ok(String(post.text).length <= 4096);
    }
  } finally { globalThis.fetch = originalFetch; }
});

test('Photon grouped acceptance uses the parent message ID, never derived part or attachment IDs', async () => {
  const thread = 'imessage:iMessage;-;+15550001111~+15550002222';
  let output: unknown = { id: 'parent-guid', content: { items: [
    { id: 'derived-part', content: { id: 'attachment-guid' } },
  ] } };
  const adapter = { app: { send: async () => output }, postMessage: async () => output,
    decodeThreadId: () => ({ chatGuid: 'iMessage;-;+15550001111', phone: '+15550002222' }),
    encodeThreadId: () => thread } as unknown as PhotonAdapterLike;
  const transport = createPhotonTransport(adapter, { gallerySpace: async () => ({ send: async () => output }) });
  const gallery = { threadId: thread, text: 'gallery', media: [{ url: 'https://example.invalid/photo.png', mimeType: 'image/png' }] };
  assert.deepEqual(await transport.send(gallery), { acceptance: 'accepted', providerMessageIds: ['parent-guid'] });
  output = { content: { items: [{ id: 'attachment-guid' }] } };
  assert.deepEqual(await transport.send(gallery), { acceptance: 'unknown', providerMessageIds: [] });
});

test('post-invocation exceptions remain unknown even when an untrusted status resembles a 4xx rejection', async () => {
  for (const error of [Object.assign(new Error('socket closed'), { status: 400 }),
    Object.assign(new Error('post-send parse failed'), { statusCode: 403 }),
    Object.assign(new Error('provider validation'), { code: 'VALIDATION_ERROR' })]) {
    const telegram = createTelegramTransport({ postMessage: async () => { throw error; } });
    assert.deepEqual(await telegram.send({ threadId: 'telegram:42', text: 'hello' }),
      { acceptance: 'unknown', providerMessageIds: [], reason: 'provider outcome uncertain' });
  }
});

test('shared gallery requires the exact captured project thread, never a physical alias', async () => {
  const thread = 'imessage:any;-;+15550001111~shared';
  let sends = 0, textSends = 0;
  const adapter = {app: {send: async () => {sends++; return {id: 'gallery'};}}, postMessage: async () => {textSends++; return {id: 'text'};},
    decodeThreadId: () => ({chatGuid: 'any;-;+15550001111', phone: 'shared'}),
    encodeThreadId: () => thread} as unknown as PhotonAdapterLike;
  const options = {gallerySpace: async () => ({send: async () => {throw new Error("app.send owns grouped posting");}})};
  const request = {threadId: thread, text: 'summary', media: [{url: 'https://example.invalid/photo.png', mimeType: 'image/png'}]};
  assert.equal((await createPhotonTransport(adapter, options).send(request)).acceptance, 'rejected');
  assert.equal((await createPhotonTransport(adapter, {...options, sharedProjectThread: () => thread + 'other'}).send(request)).acceptance, 'rejected');
  assert.equal((await createPhotonTransport(adapter, {...options, sharedProjectThread: () => thread}).send(request)).acceptance, 'accepted');
  assert.equal(sends, 1);
  const shared = createPhotonTransport(adapter, {...options, sharedProjectThread: () => thread});
  assert.equal((await shared.send({threadId: thread + 'other', text: 'hello'})).acceptance, 'rejected');
  assert.equal((await createPhotonTransport(adapter, {...options, sharedProjectThread: () => undefined}).send({threadId: thread, text: 'hello'})).acceptance, 'rejected');
  assert.equal((await shared.send({threadId: 'imessage:any;-;+15550001111~+15557654321', text: 'hello'})).acceptance, 'rejected');
  assert.equal(textSends, 0);
  assert.equal((await shared.send({threadId: thread, text: 'hello'})).acceptance, 'accepted');
  assert.equal(textSends, 1);
});


test('Photon gallery preserves upload-facing PNG filenames and exact bytes through the pinned SDK', async () => {
  const codec = createiMessageAdapter({projectId: 'synthetic', projectSecret: 'synthetic'});
  const thread = codec.encodeThreadId({chatGuid: 'any;-;+15550001111', phone: '+15557654321'});
  const bytes = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const uploads: {fileName: string; data: Uint8Array}[] = [];
  const adapter = {
    app: {send: async (_space: unknown, input: ContentInput) => {
      if (typeof input !== 'object' || input === null || !('build' in input)) {
        throw new Error('Expected Spectrum content builder');
      }
      const content = await input.build();
      assert.equal(content.type, 'group');
      if (content.type !== 'group') throw new Error('Expected grouped gallery');
      for (const member of content.items) {
        if (member.content.type === 'attachment') {
          // The installed iMessage provider uploads exactly these two fields.
          uploads.push({fileName: member.content.name, data: await member.content.read()});
          assert.equal(member.content.mimeType, 'image/png');
        }
      }
      return {id: 'gallery-provider-id'};
    }},
    decodeThreadId: codec.decodeThreadId.bind(codec),
    encodeThreadId: codec.encodeThreadId.bind(codec),
  } as unknown as PhotonAdapterLike;
  const transport = createPhotonTransport(adapter, {
    gallerySpace: async () => ({send: async () => {throw new Error('Grouped posting uses app.send');}}),
  });
  assert.equal((await transport.send({threadId: thread, text: 'Morning brief', media: [
    {data: bytes, mimeType: 'image/png', filename: 'brief-card-1.png'},
    {data: bytes, mimeType: 'image/png'},
  ]})).acceptance, 'accepted');
  assert.deepEqual(uploads.map(upload => upload.fileName), ['brief-card-1.png', 'image.png']);
  for (const upload of uploads) assert.deepEqual(Buffer.from(upload.data), bytes);
});

test('original pinned Photon postMessage preserves only native accepted root timestamps', async () => {
  const adapter = createiMessageAdapter({projectId: 'synthetic', projectSecret: 'synthetic'});
  const thread = adapter.encodeThreadId({chatGuid: 'any;-;+15550001111', phone: '+15557654321'});
  let native: unknown = new Date('2026-10-04T12:00:00Z');
  const record = () => ({id: 'native-id', direction: 'outbound', timestamp: native, space: {id: 'any;-;+15550001111', phone: '+15557654321'}, content: {type: 'text', text: 'Question?'}});
  // Only the provider space send is synthetic; installed adapter.postMessage
  // builds its actual public raw receipt from that original-send record.
  (adapter as unknown as {resolveSpace(): Promise<unknown>}).resolveSpace = async () => ({send: async () => record()});
  const transport = createPhotonTransport(adapter);
  assert.equal((await transport.send({threadId: thread, text: 'Question?'})).providerSentAtMs, Date.parse('2026-10-04T12:00:00Z'));
  for (const value of [undefined, '2026-10-04T12:00:00Z', new Date(NaN)]) {
    native = value;
    const receipt = await transport.send({threadId: thread, text: 'Question?'});
    assert.equal(receipt.acceptance, 'accepted'); assert.equal(receipt.providerSentAtMs, undefined);
  }
});
