import { startConfiguredRuntime } from '/app/runtime/sotto-chat/dist/bootstrap/main.js';
import { writeFileSync } from 'node:fs';

let runtime;
try {
  // Force the receiver's bounded local identity retry on cold startup.
  await new Promise(resolve => setTimeout(resolve, 500));
  runtime = await startConfiguredRuntime(process.argv[2], {
    createAdapter() {
      return {
        name: 'imessage', userName: 'synthetic',
        initialize: async () => {}, disconnect: async () => {},
        startGatewayListener: async (options, _duration, signal) => {
          options.waitUntil(new Promise(resolve => signal.addEventListener('abort', resolve, { once: true })));
          return { ok: true, status: 200 };
        },
        isDM: () => true, channelIdFromThreadId: value => value,
        decodeThreadId: value => value, encodeThreadId: value => String(value),
        postMessage: async () => ({ id: 'unused' }),
        fetchMessages: async () => ({ messages: [], hasMore: false }),
        addReaction: async () => {}, removeReaction: async () => {},
        deleteMessage: async () => {}, editMessage: async () => ({ id: 'unused' }),
        startTyping: async () => {}, renderFormatted: () => '',
      };
    },
    createTransport() {
      return { async send() { throw new Error('fixture forbids provider send'); } };
    },
    createAgent() {
      return { async generateChat() { throw new Error('fixture forbids model'); },
        async generateUnattended() { throw new Error('fixture forbids model'); },
        async rotateThread() {}, async purgeThreadsBefore() { return 0; }, async close() {} };
    },
    fetch: async () => { throw new Error('fixture forbids outbound fetch'); },
  });
  writeFileSync('/data/tenant/node-state/fixture-ready', 'ready\n', { mode: 0o600 });
  process.stdout.write(JSON.stringify({ ready: true, uid: process.getuid(), port: runtime.port }) + '\n');
  let closing = false;
  const stop = async () => {
    if (closing) return;
    closing = true;
    try { await runtime.close(); process.exitCode = 0; }
    catch { process.exitCode = 1; }
  };
  process.on('SIGTERM', () => { void stop(); });
  process.on('SIGINT', () => { void stop(); });
} catch (error) {
  process.stderr.write('synthetic Node startup failed: ' + String(error?.message ?? error).slice(0, 120) + '\n');
  process.exitCode = 1;
}
