import assert from 'node:assert/strict';
import test from 'node:test';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { createOwnerReminderToolClient } from '../src/tools/owner-reminder-tool-client.js';
import { ownerReminderToolKind } from '../src/tools/owner-reminder-tool-contract.js';

test('natural reminder list preserves the conditional marker and rejects unknown triggers', async () => {
  const eventId = 'a'.repeat(64), payloadHash = 'b'.repeat(64);
  const input = {
    actionId: stableActionIdentity(eventId, ownerReminderToolKind, null).actionId,
    eventId, payloadHash, text: 'List my reminders', admittedAtMs: Date.now(),
    providerThreadId: 'thread', args: null,
  };
  const ordinary = {
    id: `int_${'a'.repeat(32)}`, due: '2026-10-05T12:00:00Z',
    action: 'Call Dana', status: 'scheduled',
  };
  const conditional = {...ordinary, id: `int_${'b'.repeat(32)}`, trigger: 'conditional'};
  let items: unknown[] = [ordinary, conditional];
  const client = createOwnerReminderToolClient({
    identity: {tenant_id: 'tenant', channel: 'photon', account_id: 'account', owner_id: 'owner',
      line_key: 'line', target: 'photon', route_mode: 'fixed', route_id: 'thread',
      project_fingerprint: 'a'.repeat(64)},
    bridgeUrl: 'http://127.0.0.1:8080/mcp',
    bearer: 'c'.repeat(64),
    fetch: async () => new Response(JSON.stringify({
      version: 1, status: 'listed', event_id: eventId, payload_hash: payloadHash,
      zone: 'America/Los_Angeles', items,
    })),
  });
  const listed = await client.call('list', input);
  assert.equal(listed.status, 'listed');
  assert.deepEqual(listed.items, [ordinary, conditional]);

  items = [ordinary, {...conditional, trigger: 'recurring'}];
  await assert.rejects(client.call('list', input), /unavailable/);
});
