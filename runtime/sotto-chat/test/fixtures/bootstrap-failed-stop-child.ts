import assert from 'node:assert/strict';
import {mkdtemp,readFile,rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {startConfiguredRuntime} from '../../src/bootstrap/main.js';
import {writeConfig,fakeTelegram,fakeAgent} from '../helpers/bootstrap.js';

const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-bootstrap-stop-'));
const events: string[] = [];
let stateDb: DatabaseSync | undefined;
try {
  const config = await writeConfig(directory);
  const runtime = await startConfiguredRuntime(config.configPath, {
    createAdapter() { return fakeTelegram(events, true); },
    createTransport() { return { send: async () => ({ acceptance: 'unknown', providerMessageIds: [] }) }; },
    createAgent() { return fakeAgent(events); },
    openStateDatabase() { stateDb = new DatabaseSync(':memory:'); return stateDb; },
    listenApplication() { return Promise.resolve(32124); },
  });
  await assert.rejects(runtime.close(), /Gateway cleanup did not complete cleanly/);
  assert.equal(events.includes('agent-close'), false);
  assert.ok(await readFile(path.join(config.stateDirectory, '.runtime-owner', 'owner.json'), 'utf8'));
  stateDb?.close();
} finally { await rm(directory, { recursive: true, force: true }); }
console.log('FAILED_STOP_ASSERTIONS_PASSED');
