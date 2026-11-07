import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import test from 'node:test';
import { createOwnerLoopClient } from '../src/tools/owner-loop-client.js';

const hash = (value: string) => createHash('sha256').update(value).digest('hex');
const anchor = 'manual:deck-1';
const command = `/loop close ${anchor}`;
const identity = { tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const,
  route_id: 'imessage:owner~line', project_fingerprint: hash('project') };
const input = { actionId: hash('action'), eventId: hash('event'), payloadHash: hash('payload'),
  text: command, anchorKey: anchor, admittedAtMs: 1_780_000_000_000,
  zone: null, expectedRowDigest: null, providerThreadId: identity.route_id };

test('close client sends only the exact admitted command and typed prepare/write/inspect fields', async () => {
  const requests: Record<string, unknown>[] = [];
  const pre = hash('owner row before close'), post = hash('owner row after close');
  const client = createOwnerLoopClient({ bridgeUrl: 'http://127.0.0.1:17890/mcp', bearer: hash('capability'), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      requests.push(body);
      const operation = body.operation;
      const response = operation === 'close_prepare'
        ? { version: 1, status: 'prepared', zone: 'UTC', expected_row_digest: pre }
        : { version: 1, status: 'committed', anchor_key: anchor, post_digest: post };
      return new Response(JSON.stringify(response), { status: 200 });
    } });
  const prepared = await client.callClose('close_prepare', input);
  assert.deepEqual(prepared, { version: 1, status: 'prepared', zone: 'UTC', expected_row_digest: pre });
  const bound = { ...input, zone: prepared.zone, expectedRowDigest: prepared.expected_row_digest };
  assert.deepEqual(await client.callClose('close_write', bound),
    { version: 1, status: 'committed', anchor_key: anchor, post_digest: post });
  assert.deepEqual(await client.callClose('close_inspect', bound),
    { version: 1, status: 'committed', anchor_key: anchor, post_digest: post });
  assert.deepEqual(requests.map(row => row.operation), ['close_prepare', 'close_write', 'close_inspect']);
  for (const request of requests) {
    assert.equal(request.text, command);
    assert.equal(request.text_hash, hash(command));
    assert.equal(request.anchor_key, anchor);
    assert.equal(request.event_id, input.eventId);
    assert.equal(request.payload_hash, input.payloadHash);
    assert.deepEqual(request.identity, identity);
  }
  assert.deepEqual(requests.map(row => [row.zone, row.expected_row_digest]),
    [[null, null], ['UTC', pre], ['UTC', pre]]);
});

test('typed pre-effect refusal is accepted, but malformed proof or changed anchor is unavailable', async () => {
  const refusal = createOwnerLoopClient({ bridgeUrl: 'http://127.0.0.1:17890/mcp', bearer: hash('capability'), identity,
    fetch: async () => new Response(JSON.stringify({ version: 1, status: 'rejected',
      code: 'owner_close_target_unavailable' }), { status: 409 }) });
  assert.deepEqual(await refusal.callClose('close_prepare', input),
    { version: 1, status: 'rejected', code: 'owner_close_target_unavailable' });
  const mismatched = createOwnerLoopClient({ bridgeUrl: 'http://127.0.0.1:17890/mcp', bearer: hash('capability'), identity,
    fetch: async () => new Response(JSON.stringify({ version: 1, status: 'committed',
      anchor_key: 'different', post_digest: hash('post') }), { status: 200 }) });
  await assert.rejects(mismatched.callClose('close_write', { ...input, zone: 'UTC',
    expectedRowDigest: hash('pre') }), /unavailable/);
  await assert.rejects(mismatched.callClose('close_prepare', input), /unavailable/);
  await assert.rejects(refusal.callClose('close_prepare', { ...input, text: `/loop close ${anchor}-changed` }),
    /invalid/);
});
