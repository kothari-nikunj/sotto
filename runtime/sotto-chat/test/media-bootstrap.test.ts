import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { Message } from 'chat';
import { startConfiguredRuntime } from '../src/bootstrap/main.js';
import type { GatewayAdapter } from '../src/channels/gateway.js';

function fakeTelegram() {
  let polling = false;
  let initialized = false;
  const adapter = {
    name: 'telegram', userName: 'sotto',
    initialize: async () => { initialized = true; polling = true; },
    disconnect: async () => { polling = false; },
    stopPolling: async () => { polling = false; },
    get isPolling() { return polling; },
    isDM: (thread: string) => thread === 'telegram:42',
    channelIdFromThreadId: () => 'telegram:42',
    decodeThreadId: (thread: string) => thread,
    encodeThreadId: (value: unknown) => String(value),
    postMessage: async () => ({ id: 'unused' }),
    fetchMessages: async () => ({ messages: [], hasMore: false }),
    addReaction: async () => {}, removeReaction: async () => {}, deleteMessage: async () => {},
    editMessage: async () => ({ id: 'unused' }), startTyping: async () => {}, renderFormatted: () => '',
  } as unknown as GatewayAdapter;
  return { adapter, get initialized() { return initialized; } };
}

test('configured Telegram album survives interruption and recovers media through the exact proxy before intake restarts', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-media-bootstrap-'));
  const configPath = join(directory, 'runtime.json');
  const stateDirectory = join(directory, 'state');
  const config = { version: 1, channel: 'telegram', stateDirectory, mediaRoot: join(directory, 'media'),
    bearerToken: 'offline-media-bootstrap-token-00000000', owner: { accountId: 'bot', ownerId: '42' },
    lineKey: 'primary', target: 'owner', providerThreadId: 'telegram:42',
    credentials: { telegramToken: '123:fixture-token', telegramLinkFile: { bot_token: '123:fixture-token', allowed_user: 42 } },
    instructions: 'Answer the owner briefly.',
    modelProxy: { origin: 'https://fixture-proxy.example', bearer: 'offline-proxy-token', chatModelId: 'fixture-chat', nativeMediaModelId: 'fixture-media' } };
  await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });
  let downloading!: () => void;
  const startedDownload = new Promise<void>(resolve => { downloading = resolve; });
  let firstRun = true;
  let nativeCalls = 0;
  let chatCalls = 0;
  let posted = 0;
  let provider: ReturnType<typeof fakeTelegram>;
  const downloaded: string[] = [];
  const fakeFetch: typeof fetch = async (input, init) => {
    const url = new URL(String(input));
    if (url.origin === 'https://api.telegram.org') {
      assert.equal(init?.redirect, 'error');
      const journal = new DatabaseSync(join(stateDirectory, 'operations.sqlite'), { readOnly: true });
      try {
        const row = journal.prepare('SELECT payload FROM inbound_events WHERE source_id=?').get('album-event');
        assert.ok(row, 'provider read must follow SQLite admission');
        const media = JSON.parse(String(row.payload)).inbound.media;
        assert.deepEqual(media.map((item: any) => item.source.fileId), ['photo-one', 'photo-two']);
        assert.ok(media.every((item: any) => item.data === undefined && item.url === undefined));
      } finally { journal.close(); }
      if (url.pathname.endsWith('/getFile')) {
        const body = JSON.parse(String(init?.body));
        assert.ok(['photo-one', 'photo-two'].includes(body.file_id));
        return Response.json({ ok: true, result: { file_id: body.file_id, file_path: `photos/${body.file_id}.png`, file_size: 8 } });
      }
      assert.ok(url.pathname.includes('/file/bot123:fixture-token/photos/') || url.pathname.includes('/file/bot123%3Afixture-token/photos/'));
      if (firstRun) {
        downloading();
        return new Promise<Response>((_resolve, reject) => {
          const abort = () => reject(new DOMException('fixture cancellation', 'AbortError'));
          if (init?.signal?.aborted) abort();
          else init?.signal?.addEventListener('abort', abort, { once: true });
        });
      }
      assert.equal(provider.initialized, false, 'recovery must precede reopening the provider consumer');
      downloaded.push(url.pathname.split('/').at(-1)!);
      return new Response(Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10]), { headers: { 'content-type': 'image/png', 'content-length': '8' } });
    }
    assert.equal(url.origin, 'https://fixture-proxy.example');
    assert.equal(new Headers(init?.headers).get('authorization'), 'Bearer offline-proxy-token');
    assert.equal(init?.redirect, 'error');
    const body = JSON.parse(String(init?.body));
    if (url.pathname === '/native/v1beta/models/fixture-media:generateContent') {
      nativeCalls++;
      assert.equal('tools' in body, false);
      return Response.json({ candidates: [{ content: { role: 'model', parts: [{ text: `photo ${nativeCalls} shows a kite` }] }, finishReason: 'STOP' }],
        usageMetadata: { promptTokenCount: 3, candidatesTokenCount: 3, totalTokenCount: 6 } });
    }
    assert.equal(url.pathname, '/openai/v1/chat/completions');
    chatCalls++;
    const prompt = JSON.stringify(body.messages);
    assert.ok(prompt.includes('photo 1 shows a kite') && prompt.includes('photo 2 shows a kite'));
    assert.ok(prompt.includes('untrusted attachment evidence'));
    return Response.json({ id: 'reply', created: 1, model: 'fixture-chat', choices: [{ index: 0,
      message: { role: 'assistant', content: 'Both photos show kites.' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 5, completion_tokens: 4, total_tokens: 9 } });
  };
  const dependencies = {
    fetch: fakeFetch,
    createAdapter: () => { provider = fakeTelegram(); return provider.adapter; },
    createTransport: () => ({ send: async (request: { text?: string }) => {
      assert.equal(firstRun, false);
      assert.equal(request.text, 'Both photos show kites.');
      posted++;
      return { acceptance: 'accepted' as const, providerMessageIds: ['provider-reply'] };
    } }),
    listenApplication: async () => 0,
  };
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  try {
    runtime = await startConfiguredRuntime(configPath, dependencies);
    const message = new Message({ id: 'album-event', threadId: 'telegram:42', text: 'What are in these photos?',
      formatted: { type: 'root', children: [] }, raw: { message_id: 'latest-album-member' },
      author: { userId: '42', userName: 'owner', fullName: 'Owner', isBot: false, isMe: false },
      metadata: { dateSent: new Date(), edited: false },
      attachments: ['photo-one', 'photo-two'].map(fileId => ({ type: 'image' as const, mimeType: 'image/png',
        name: `${fileId}.png`, size: 8, fetchMetadata: { fileId } })),
    });
    await runtime.gateway.chat.processMessage(provider!.adapter, 'telegram:42', message);
    await startedDownload;
    await runtime.close(); runtime = undefined;
    assert.deepEqual({ nativeCalls, chatCalls, posted }, { nativeCalls: 0, chatCalls: 0, posted: 0 });
    firstRun = false;
    runtime = await startConfiguredRuntime(configPath, dependencies);
    assert.deepEqual(downloaded, ['photo-one.png', 'photo-two.png']);
    assert.deepEqual({ nativeCalls, chatCalls, posted }, { nativeCalls: 2, chatCalls: 1, posted: 1 });
    assert.equal(provider!.initialized, true);
    const journal = new DatabaseSync(join(stateDirectory, 'operations.sqlite'), { readOnly: true });
    try {
      const row = journal.prepare('SELECT payload,outcome FROM inbound_events WHERE source_id=?').get('album-event')!;
      assert.equal(row.outcome, 'resolved');
      assert.ok(JSON.parse(String(row.payload)).inbound.media.every((item: any) => item.data === undefined));
    } finally { journal.close(); }
  } finally { await runtime?.close(); await rm(directory, { recursive: true, force: true }); }
});
