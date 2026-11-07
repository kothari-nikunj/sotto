import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createOwnerLoopClient } from '../src/tools/owner-loop-client.js';
import { createOwnerReminderClient } from '../src/tools/owner-reminder-client.js';
import { createOwnerQuietClient } from '../src/tools/owner-quiet-client.js';

const identity = {
  tenant_id: 'local-tenant', channel: 'telegram' as const, account_id: 'local-account',
  owner_id: '8675309', line_key: 'telegram-owner', target: 'telegram' as const,
  route_mode: 'fixed' as const, route_id: 'telegram:8675309',
  bot_fingerprint: createHash('sha256').update('sotto-telegram-bot-v1:fixture-token').digest('hex'),
};
const options = { bridgeUrl: 'http://127.0.0.1:19982/mcp', bearer: 'a'.repeat(64) };

test('both private command clients accept only the same canonical Telegram identity', () => {
  assert.deepEqual(createOwnerLoopClient({ ...options, identity }).identity, identity);
  assert.deepEqual(createOwnerReminderClient({ ...options, identity }).identity, identity);
  assert.deepEqual(createOwnerQuietClient({ ...options, identity }).identity, identity);
  for (const changed of [
    { ...identity, route_id: 'telegram:other' },
    { ...identity, owner_id: '08675309', route_id: 'telegram:08675309' },
    { ...identity, owner_id: '9007199254740992', route_id: 'telegram:9007199254740992' },
    { ...identity, route_mode: 'first_contact' },
    { ...identity, target: 'photon' },
    { ...identity, project_fingerprint: '0'.repeat(64) },
  ]) {
    assert.throws(() => createOwnerLoopClient({ ...options, identity: changed as typeof identity }));
    assert.throws(() => createOwnerReminderClient({ ...options, identity: changed as typeof identity }));
    assert.throws(() => createOwnerQuietClient({ ...options, identity: changed as typeof identity }));
  }
});
