import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { routeDigest } from '../src/route-proof.js';

test('v1 resolved route digest matches the independent UTF-8 length-prefix vector', () => {
  assert.equal(routeDigest({
    channel: 'photon', tenantId: 'tenant-1', projectFingerprint: 'a'.repeat(64),
    ownerId: 'owner-é', accountId: 'account-1', lineKey: 'line-1',
  }, 'imessage:iMessage;-;+15555550123~+15555550999'),
  '5d1bb3f8b0ddb0a2b32969f08aaebaf488c9f0125390d44cea7d0cccd2c6d609');
  assert.throws(() => routeDigest({
    channel: 'photon', tenantId: '', projectFingerprint: 'bad',
    ownerId: 'owner', accountId: 'account', lineKey: 'line',
  }, 'imessage:thread'), /invalid dispatch route identity/);
});

test('routeDigest matches every shared cross-language vector and rejects the shared invalid inputs', () => {
  const vectors = JSON.parse(readFileSync(new URL('./fixtures/route-digest-vectors.json', import.meta.url), 'utf8')) as {
    version: number; fields: string[];
    valid: Array<{name: string; input: Record<string, string>; digest: string}>;
    invalid: Array<{name: string; input: Record<string, string>}>;
  };
  assert.equal(vectors.version, 1);
  assert.deepEqual(vectors.fields, ['channel', 'tenantId', 'projectFingerprint', 'ownerId', 'accountId', 'lineKey', 'providerThreadId']);
  assert.ok(vectors.valid.length >= 5 && vectors.invalid.length >= 3);
  const identity = (input: Record<string, string>) => ({channel: input.channel as 'photon' | 'telegram', tenantId: input.tenantId!,
    projectFingerprint: input.projectFingerprint!, ownerId: input.ownerId!, accountId: input.accountId!, lineKey: input.lineKey!});
  for (const vector of vectors.valid) {
    assert.match(vector.digest, /^[a-f0-9]{64}$/, vector.name);
    assert.equal(routeDigest(identity(vector.input), vector.input.providerThreadId!), vector.digest, vector.name);
  }
  for (const vector of vectors.invalid)
    assert.throws(() => routeDigest(identity(vector.input), vector.input.providerThreadId!), /invalid dispatch route identity/, vector.name);
});
