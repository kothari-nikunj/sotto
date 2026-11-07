import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { OperationalJournal } from '../src/journal/index.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';

test('action hashing preserves JSON keys that resemble prototype properties', () => {
  const plain = stableActionIdentity('request', 'send', { body: 'hello' });
  const extra = stableActionIdentity('request', 'send', JSON.parse('{"body":"hello","__proto__":{"target":"other"}}'));
  const reordered = stableActionIdentity('request', 'send', JSON.parse('{"__proto__":{"target":"other"},"body":"hello"}'));
  assert.notEqual(extra.actionId, plain.actionId);
  assert.notEqual(extra.payloadHash, plain.payloadHash);
  assert.deepEqual(extra, reordered);
});

function fixture() {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-action-'));
  const journal = new OperationalJournal(join(directory, 'journal.sqlite'));
  const admit = (eventId: string) => journal.admitInbound({
    eventId, accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'imessage',
    sender: 'owner', payload: 'request',
  }).event.id as string;
  const run1 = 'run-1';
  journal.startRun({ runId: run1, eventId: admit('event-1') });
  return { journal, directory, admit, run1, close: () => { journal.close(); rmSync(directory, { recursive: true, force: true }); } };
}

test('interrupted executing action remains held after journal recovery', async () => {
  const f = fixture();
  try {
    const identity = stableActionIdentity('owner-request-3', 'send', { body: 'maybe sent' });
    f.journal.recordActionIntent({ ...identity, runId: f.run1, kind: 'send' });
    f.journal.beginAction(identity.actionId);
    assert.equal(f.journal.recoverInterrupted().actions, 1);
    assert.equal(f.journal.getAction(identity.actionId)?.status, 'unknown');
  } finally { f.close(); }
});
