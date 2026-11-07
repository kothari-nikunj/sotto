import {chmod,mkdir,writeFile} from 'node:fs/promises';
import path from 'node:path';
import type {RuntimeAgent} from '../../src/bootstrap/main.js';
import type {GatewayAdapter} from '../../src/channels/gateway.js';

export async function writeConfig(directory: string, overrides: Record<string, unknown> = {}) {
  const stateDirectory = path.join(directory, 'state');
  const mediaRoot = path.join(directory, 'media');
  await mkdir(mediaRoot, { recursive: true, mode: 0o700 });
  const config = {
    version: 1,
    channel: 'telegram',
    stateDirectory,
    mediaRoot,
    bearerToken: 'test-only-bearer-token-is-at-least-32-characters-long',
    port: 0,
    owner: { accountId: 'telegram-owner', ownerId: '123' },
    lineKey: 'primary',
    target: 'owner-target',
    providerThreadId: 'telegram:123',
    credentials: { telegramToken: 'test-token', telegramLinkFile: { bot_token: 'test-token', allowed_user: 123 } },
    instructions: 'Answer briefly.',
    modelProxy: { origin: 'https://proxy.example', bearer: 'test-proxy-token', chatModelId: 'chat-model', nativeMediaModelId: 'media-model' },
    ...overrides,
  };
  const configPath = path.join(directory, 'runtime.json');
  await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });
  await chmod(configPath, 0o600);
  return { config, configPath, stateDirectory, mediaRoot };
}

export function fakeTelegram(events: string[], failDisconnect = false) {
  let polling = false;
  const adapter = {
    name: 'telegram', userName: 'test',
    initialize: async () => { events.push('provider-initialize'); polling = true; },
    disconnect: async () => { events.push('provider-stop'); polling = false; if (failDisconnect) throw new Error('test shutdown failure'); },
    stopPolling: async () => { events.push('provider-stop-polling'); if (failDisconnect) throw new Error('test polling stop failure'); polling = false; },
    get isPolling() { return polling; },
    isDM: (threadId: string) => threadId === 'telegram:123',
    channelIdFromThreadId: () => 'telegram:123',
    decodeThreadId: (threadId: string) => threadId,
    encodeThreadId: (threadId: unknown) => String(threadId),
    postMessage: async () => ({ id: 'unused' }),
    fetchMessages: async () => ({ messages: [], hasMore: false }),
    addReaction: async () => {}, removeReaction: async () => {}, deleteMessage: async () => {},
    editMessage: async () => ({ id: 'unused' }), startTyping: async () => {}, renderFormatted: () => '',
  } as unknown as GatewayAdapter;
  return adapter;
}

export function fakeAgent(events: string[]): RuntimeAgent {
  return {
    async generateChat() { events.push('model-chat'); return { text: 'unused' }; },
    async generateUnattended() { return { text: 'unused' }; },
    async rotateThread() {}, async purgeThreadsBefore() { return 0; },
    async close() { events.push('agent-close'); },
  };
}

