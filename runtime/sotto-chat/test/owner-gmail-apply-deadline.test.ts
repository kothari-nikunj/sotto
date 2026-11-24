import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import { createOwnerGmailDraftClient } from '../src/tools/owner-gmail-draft-client.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { ownerGmailDraftKind } from '../src/tools/owner-gmail-draft-contract.js';

const identity = { tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64) };

test('Gmail apply waits as long as the receiver Gmail send budget; preview keeps the short deadline', async (t) => {
  const deadlines: number[] = [];
  const timeout = AbortSignal.timeout.bind(AbortSignal);
  t.mock.method(AbortSignal, 'timeout', (ms: number) => {deadlines.push(ms); return timeout(ms);});
  const client = createOwnerGmailDraftClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => JSON.parse(init!.body as string).operation === 'apply'
      ? Response.json({version: 1, status: 'unknown'})
      : Response.json({version: 1, status: 'rejected', code: 'target_changed'}, {status: 409})});
  const eventId = createHash('sha256').update('event').digest('hex');
  const args = {operation: 'propose' as const, message_id: 'message-1', body: 'Thanks, Robin will send the notes.'};
  const input = {actionId: stableActionIdentity(eventId, ownerGmailDraftKind, args).actionId, eventId,
    payloadHash: 'b'.repeat(64), text: 'reply to Robin', admittedAtMs: 1, providerThreadId: 'thread', args};
  await client.call('preview', input);
  await client.call('apply', input);
  assert.deepEqual(deadlines, [5000, 15_000]);
});
