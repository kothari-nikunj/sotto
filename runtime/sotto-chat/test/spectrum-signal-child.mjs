import fs from 'node:fs';
import assert from 'node:assert/strict';
import {findPackageJSON} from 'node:module';
import { Spectrum } from '@spectrum-ts/core';

const adapterEntry = import.meta.resolve('@photon-ai/chat-adapter-imessage');
assert.equal(fs.realpathSync(findPackageJSON('@spectrum-ts/core', adapterEntry)),
  fs.realpathSync(findPackageJSON('@spectrum-ts/core', import.meta.url)),
  'shutdown probe must exercise the same Spectrum instance as the production adapter');
const log = message => fs.writeSync(1, `${message}\n`);
const app = await Spectrum({ providers: [], options: { logLevel: 'silent' } });
if (typeof app.stop !== 'function') throw new Error('Spectrum public stop API missing');
assert.equal(process.listenerCount('SIGTERM'), 0, 'Spectrum does not install SIGTERM handlers');
assert.equal(process.listenerCount('SIGINT'), 0, 'Spectrum does not install SIGINT handlers');
process.on('SIGTERM', () => {
  log('LATE_HANDLER_ENTERED');
  app.stop().then(() => {
    log('LATE_APP_STOP_RETURNED');
    setTimeout(() => {
      log('LATE_HANDLER_SETTLED');
      clearInterval(keepAlive);
    }, 1_000);
  });
});
const keepAlive = setInterval(() => {}, 60_000);
log(`READY LISTENERS=${process.listenerCount('SIGTERM')}`);
