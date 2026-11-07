import { startConfiguredRuntime } from '/app/runtime/sotto-chat/dist/bootstrap/main.js';
import { mkdtemp, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

let closing = false;
try {
  const root = await mkdtemp(join(tmpdir(), 'sotto-node-fixture-'));
  await mkdir(join(root, 'state'), { mode: 0o700 });
  await mkdir(join(root, 'media'), { mode: 0o700 });
  const config = JSON.parse(process.argv[2]);
  config.stateDirectory = join(root, 'state');
  config.mediaRoot = join(root, 'media');
  const configPath = join(root, 'runtime.json');
  await writeFile(configPath, JSON.stringify(config), { mode: 0o600 });
  const runtime = await startConfiguredRuntime(configPath, {
    createAdapter() {
      let polling = false;
      return {
        name: 'telegram', userName: 'fixture',
        initialize: async () => { polling = true; },
        disconnect: async () => { polling = false; },
        stopPolling: async () => { polling = false; },
        get isPolling() { return polling; },
        isDM: threadId => threadId === 'telegram:8675309',
        channelIdFromThreadId: () => 'telegram:8675309',
        decodeThreadId: threadId => threadId,
        encodeThreadId: threadId => String(threadId),
        postMessage: async () => ({ id: 'synthetic-unused' }),
        fetchMessages: async () => ({ messages: [], hasMore: false }),
        addReaction: async () => {}, removeReaction: async () => {}, deleteMessage: async () => {},
        editMessage: async () => ({ id: 'synthetic-unused' }),
        startTyping: async () => {}, renderFormatted: () => '',
      };
    },
    createTransport() {
      return { async send() { return { acceptance: 'accepted', providerMessageIds: ['unused'] }; } };
    },
    createAgent() {
      return {
        async generateChat() { return { text: 'unused' }; },
        async generateUnattended() { return { text: 'unused' }; },
        async rotateThread() {},
        async purgeThreadsBefore() { return 0; },
        async close() {},
      };
    },
    fetch: async () => { throw new Error('fixture forbids network fetch'); },
  });
  process.stdout.write(JSON.stringify({ kind: 'node-ready', uid: process.getuid(),
    port: runtime.port, gateway: runtime.gateway.lifecycle }) + '\n');
  const stop = async () => {
    if (closing) return;
    closing = true;
    try { await runtime.close(); process.exitCode = 0; }
    catch { process.exitCode = 1; }
  };
  process.once('SIGTERM', () => { void stop(); });
  process.once('SIGINT', () => { void stop(); });
} catch {
  process.stderr.write('NODE_FIXTURE_START_FAILED\n');
  process.exitCode = 1;
}
