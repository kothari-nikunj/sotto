import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { OperationalJournal } from '../src/journal/index.js';

const hash = (value: string) => createHash('sha256').update(value).digest('hex');
const anchor = 'owner:deck:2026-10-01';
const close = `/loop close ${anchor}`;
const authority = { runId: 'run-1', accountId: 'account', ownerId: 'owner',
  lineKey: 'line', channel: 'photon' as const, scopeHash: hash('private-capability') };
const context = { threadId: 'imessage:owner~line', destination: 'owner-target', lineKey: 'line' };
function fixture() {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-close-fence-'));
  const path = join(directory, 'operations.sqlite');
  const journal = new OperationalJournal(path);
  return { journal, path, dispose: () => { journal.close(); rmSync(directory, { recursive: true, force: true }); } };
}
function admit(journal: OperationalJournal, sourceId: string, text: string) {
  const payload = JSON.stringify({ inbound: { eventId: sourceId, accountId: authority.accountId,
    channel: authority.channel, senderId: authority.ownerId, text }, context });
  return journal.admitInbound({ eventId: sourceId, accountKey: authority.accountId, lineKey: authority.lineKey,
    threadKey: context.threadId, channel: authority.channel, sender: authority.ownerId, payload }).event;
}

test('close binds the full admitted command, zone and row digest before effect contact', () => {
  const f = fixture();
  try {
    const ordinary = admit(f.journal, 'earlier', 'ordinary owner text');
    const command = admit(f.journal, 'close', close);
    f.journal.startRun({ runId: authority.runId, eventId: ordinary.id as string });
    const intent = f.journal.prepareExplicitOwnerLoopCloseIntent(authority);
    assert.equal(intent.sourceEventId, command.id);
    assert.equal(intent.text, close);
    assert.equal(intent.anchorKey, anchor);
    assert.equal(intent.actionId, hash(JSON.stringify(['sotto.owner-loop-close-command.v1', command.id])));
    const row = f.journal.getAction(intent.actionId)!;
    assert.equal(row.source_text_hash, hash(close));
    assert.equal(row.payload_hash, hash(JSON.stringify([command.payload_hash, hash(close), authority.scopeHash])));
    assert.equal(JSON.stringify(row).includes(anchor), false);
    assert.throws(() => f.journal.beginAction(intent.actionId), /prepare proof/);
    const pre = hash('original owner row');
    f.journal.bindExplicitOwnerLoopClosePrepare(intent.actionId, 'America/Los_Angeles', pre);
    assert.deepEqual(JSON.parse(f.journal.getAction(intent.actionId)!.result_json as string),
      { owner_zone: 'America/Los_Angeles', expected_row_digest: pre });
    assert.throws(() => f.journal.bindExplicitOwnerLoopClosePrepare(intent.actionId, 'UTC', pre), /changed/);
    f.journal.beginAction(intent.actionId);
    assert.throws(() => f.journal.settleAction(intent.actionId, 'succeeded', { body: 'forged' }), /compact verified receipt/);
    const receipt = hash('canonical post-state');
    f.journal.settleExplicitOwnerLoopCloseIntent(intent.actionId, receipt);
    const saved = f.journal.prepareExplicitOwnerLoopCloseIntent(authority);
    assert.equal(saved.status, 'succeeded');
    assert.equal(saved.receiptHash, receipt);
    assert.equal(saved.expectedRowDigest, pre);
  } finally { f.dispose(); }
});

test('mixed add and close commands cannot mint either action in a captured run', () => {
  const f = fixture();
  try {
    const first = admit(f.journal, 'add', '/loop add Send the deck');
    admit(f.journal, 'close', close);
    f.journal.startRun({ runId: authority.runId, eventId: first.id as string });
    assert.throws(() => f.journal.prepareExplicitOwnerLoopIntent(authority), /exactly one explicit owner command/);
    assert.throws(() => f.journal.prepareExplicitOwnerLoopCloseIntent(authority), /exactly one explicit owner command/);
    assert.equal((f.journal.db.prepare('SELECT count(*) AS n FROM actions').get() as { n: number }).n, 0);
  } finally { f.dispose(); }
});

test('unknown close survives reopen, cannot write again, and reconciles only with compact proof', () => {
  const f = fixture();
  try {
    const command = admit(f.journal, 'close', close);
    f.journal.startRun({ runId: authority.runId, eventId: command.id as string });
    const intent = f.journal.prepareExplicitOwnerLoopCloseIntent(authority);
    const pre = hash('pre');
    f.journal.bindExplicitOwnerLoopClosePrepare(intent.actionId, 'UTC', pre);
    f.journal.beginAction(intent.actionId);
    f.journal.close();
    const reopened = new OperationalJournal(f.path);
    try {
      assert.equal(reopened.recoverInterrupted().actions, 1);
      const held = reopened.readUnknownExplicitOwnerLoopCloseIntent(intent.actionId, authority);
      assert.equal(held?.expectedRowDigest, pre);
      assert.equal(held?.anchorKey, anchor);
      assert.throws(() => reopened.beginAction(intent.actionId), /not eligible/);
      assert.throws(() => reopened.settleAction(intent.actionId, 'succeeded', { forged: true }), /compact verified receipt/);
      const cutoff = Date.now() + 365 * 24 * 60 * 60 * 1000;
      reopened.pruneTerminal(cutoff, cutoff);
      assert.equal(reopened.getAction(intent.actionId)?.status, 'unknown');
      assert.notEqual(reopened.getInbound(command.id as string)?.payload, '');
      const receipt = hash('exact canonical inspect receipt');
      reopened.reconcileExplicitOwnerLoopCloseIntent(intent.actionId, receipt);
      reopened.completeRun(authority.runId);
      reopened.resolveInbound(command.id as string, 'resolved');
      reopened.pruneTerminal(cutoff, cutoff);
      assert.equal(reopened.getAction(intent.actionId)?.status, 'succeeded');
      assert.equal(reopened.getInbound(command.id as string)?.payload, '');
      assert.equal(admit(reopened, 'close', close).id, command.id);
      assert.throws(() => admit(reopened, 'close', `/loop close ${anchor}-changed`), /identity conflict/);
    } finally { reopened.close(); }
  } finally { rmSync(join(f.path, '..'), { recursive: true, force: true }); }
});
