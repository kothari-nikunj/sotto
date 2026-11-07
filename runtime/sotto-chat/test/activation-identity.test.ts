import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { createiMessageAdapter } from '@photon-ai/chat-adapter-imessage';
import { photonProjectFingerprint } from '../src/activation.js';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const projectId = 'activation-parity-fixture-project';
const ownerId = '+15555550123';
const tenantId = 'tenant-parity-fixture';
const guids = [
  'iMessage;-;+15555550123',
  'iMessage;-;owner@example.com',
  'iMessage;-;+15555550123;email:owner@example.com',
];
const threadIds = [
  `imessage:${guids[0]}`,
  `imessage:${guids[1]}~+15555550999`,
  `imessage:${guids[2]}~owner@example.com`,
];

test('Node and Python activation fingerprints and decoded Photon chat GUIDs match pinned SDK', () => {
  // Constructor config is explicit and blanks every optional ambient provider field;
  // decodeThreadId is synchronous/public and needs no initialize or provider connection.
  const adapter = createiMessageAdapter({
    local: false,
    projectId,
    projectSecret: 'synthetic-project-secret',
    serverUrl: '',
    apiKey: '',
    phone: '',
    webhookSecret: '',
  });
  const fingerprint = photonProjectFingerprint(projectId);
  const fixture = {
    projectId,
    fingerprint,
    tenantId,
    ownerId,
    threadIds: threadIds.map((provider_thread_id, index) => ({
      provider_thread_id,
      guid: guids[index],
    })),
  };
  const python = String.raw`
import json, sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import photon_owner
import runtime_activation
value = json.load(sys.stdin)
result = {
  'fingerprint': photon_owner.project_fingerprint(value['projectId']),
  'guids': [],
}
for i, item in enumerate(value['threadIds']):
  evidence = {
    'version': 1,
    'event_id': ('%064x' % (i + 1)),
    'provider_event_id': 'fixture-event-' + str(i),
    'tenant_id': value['tenantId'],
    'project_fingerprint': value['fingerprint'],
    'owner_id': value['ownerId'],
    'account_id': 'fixture-account',
    'line_key': 'fixture-line',
    'provider_thread_id': item['provider_thread_id'],
    'admitted_at': 1780000000000,
  }
  result['guids'].append(runtime_activation.verified_chat_guid(
    evidence, tenant_id=value['tenantId'], owner_id=value['ownerId'],
    project_fingerprint=value['fingerprint']))
print(json.dumps(result))
`;
  const pythonResult = JSON.parse(execFileSync('python3', [
    '-I', '-B', '-c', python,
    resolve(repoRoot, 'adapters/sotto'),
    resolve(repoRoot, 'runtime/trigger-receiver'),
  ], { input: JSON.stringify(fixture), encoding: 'utf8' })) as { fingerprint: string; guids: string[] };

  assert.equal(pythonResult.fingerprint, fingerprint);
  for (const [index, threadId] of threadIds.entries()) {
    const decoded = adapter.decodeThreadId(threadId);
    assert.equal(decoded.chatGuid, pythonResult.guids[index]);
    assert.equal(decoded.chatGuid, guids[index]);
    const expectedPhone = threadId.split('~')[1];
    assert.equal(decoded.phone, expectedPhone);
  }
});
