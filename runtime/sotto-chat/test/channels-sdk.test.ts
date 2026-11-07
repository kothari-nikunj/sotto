import test from 'node:test';
import assert from 'node:assert/strict';
import { createPhotonSdkAdapter, createTelegramSdkAdapter } from '../src/channels/sdk.js';

test('Telegram adapter construction ignores ambient endpoint, identity, and reply settings', () => {
  const keys = [
    'TELEGRAM_API_BASE_URL', 'TELEGRAM_BOT_USERNAME', 'TELEGRAM_MENTION_ON_REPLY',
    'TELEGRAM_WEBHOOK_SECRET_TOKEN', 'TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS', 'TELEGRAM_ALLOWED_USER_IDS',
  ];
  const previous = new Map(keys.map(key => [key, process.env[key]]));
  try {
    process.env.TELEGRAM_API_BASE_URL = 'https://attacker.invalid';
    process.env.TELEGRAM_BOT_USERNAME = 'ambient-user';
    process.env.TELEGRAM_MENTION_ON_REPLY = 'true';
    process.env.TELEGRAM_WEBHOOK_SECRET_TOKEN = 'ambient-secret';
    process.env.TELEGRAM_ALLOW_UNVERIFIED_WEBHOOKS = 'true';
    process.env.TELEGRAM_ALLOWED_USER_IDS = '999';
    const adapter = createTelegramSdkAdapter({
      telegramToken: 'explicit-token',
      telegramLinkFile: { bot_token: 'explicit-token', allowed_user: 123 },
    }) as unknown as {
      apiBaseUrl: string;
      _userName: string;
      mentionOnReply: boolean;
      secretToken: string | undefined;
      allowUnverifiedWebhooks: boolean;
      allowedUserIds: Set<string> | undefined;
      mode: string;
      longPolling: { deleteWebhook: boolean; dropPendingUpdates: boolean };
    };
    assert.equal(adapter.apiBaseUrl, 'https://api.telegram.org');
    assert.equal(adapter._userName, 'sotto');
    assert.equal(adapter.mentionOnReply, false);
    assert.equal(adapter.secretToken, '');
    assert.equal(adapter.allowUnverifiedWebhooks, false);
    assert.deepEqual([...adapter.allowedUserIds ?? []], ['123']);
    assert.equal(adapter.mode, 'polling');
    assert.deepEqual(adapter.longPolling, { deleteWebhook: false, dropPendingUpdates: false });
  } finally {
    for (const key of keys) {
      const value = previous.get(key);
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
  }
});

test('Telegram private-message hook cannot type before Sotto owner admission', () => {
  const adapter = createTelegramSdkAdapter({ telegramToken: 'explicit-token',
    telegramLinkFile: { bot_token: 'explicit-token', allowed_user: 123 } });
  let typing = 0;
  adapter.startTyping = async () => { typing++; };
  (adapter as unknown as { startTypingForPrivateMessage(message: unknown, thread: string): void })
    .startTypingForPrivateMessage({ chat: { type: 'private' }, from: { is_bot: false } }, 'telegram:123');
  assert.equal(typing, 0);
});

test('configured Telegram and Photon adapter loggers suppress raw provider errors', () => {
  const telegram = createTelegramSdkAdapter({ telegramToken: 'explicit-token',
    telegramLinkFile: { bot_token: 'explicit-token', allowed_user: 123 } });
  const photon = createPhotonSdkAdapter({ photonProjectId: 'project', photonProjectSecret: 'secret' });
  const seen: string[] = [];
  const original = console.error;
  console.error = (...parts: unknown[]) => { seen.push(parts.map(String).join(' ')); };
  try {
    for (const adapter of [telegram, photon]) {
      (adapter as unknown as { logger: { error(message: string, error?: unknown): void } })
        .logger.error('provider failed', new Error('https://provider.example?token=private'));
    }
    assert.deepEqual(seen, []);
  } finally { console.error = original; }
});
