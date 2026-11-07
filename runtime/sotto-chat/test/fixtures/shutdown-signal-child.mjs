import { setTimeout as delay } from 'node:timers/promises';
import { waitForShutdownSignal } from '../../src/bootstrap/main.js';

// A foreign exit hook and SIGTERM handler must not preempt the supervisor's
// SIGUSR2 cleanup. The Bridge no longer imports the MCP client's exit-hook.
process.once('beforeExit', () => { process.stdout.write('FOREIGN_EXIT_HOOK\n'); });
process.on('SIGTERM', () => { process.stdout.write('FOREIGN_SIGTERM\n'); process.exit(143); });
const waiter = waitForShutdownSignal({
  async close() {
    process.stdout.write('DISCONNECT_STARTED\n');
    await delay(350);
    process.stdout.write('DISCONNECT_SETTLED\n');
  },
}, 2_000);
// A signal listener and unresolved Promise do not keep Node's event loop alive.
// The configured runtime normally has a live HTTP listener; model that here so
// the foreign beforeExit handler cannot win before the supervisor sends SIGUSR2.
const keepAlive = setInterval(() => {}, 1_000);
process.stdout.write('READY\n');
try {
  await waiter;
} finally {
  clearInterval(keepAlive);
}
process.stdout.write('SHUTDOWN_SETTLED\n');
