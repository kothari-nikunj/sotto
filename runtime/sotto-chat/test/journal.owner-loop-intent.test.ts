import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { OperationalJournal } from '../src/journal/index.js';

const digest = (text: string) => createHash('sha256').update(text).digest('hex');
const owner = { runId: 'run-1', accountId: 'owner-account', ownerId: 'owner-42',
  lineKey: 'line-1', channel: 'telegram' as const };
const context = { threadId: 'telegram:owner-42', destination: 'owner-target', lineKey: 'line-1' };
function fixture() {
  const dir = mkdtempSync(join(tmpdir(), 'sotto-owner-fence-'));
  const path = join(dir, 'operations.sqlite');
  const journal = new OperationalJournal(path);
  return { journal, path, close: () => { journal.close(); rmSync(dir, { recursive: true, force: true }); } };
}
function payload(providerEventId: string, text: string) {
  return JSON.stringify({ inbound: { eventId: providerEventId, accountId: owner.accountId,
    channel: owner.channel, senderId: owner.ownerId, text }, context });
}
function admit(journal: OperationalJournal, providerEventId: string, text: string) {
  return journal.admitInbound({ eventId: providerEventId, accountKey: owner.accountId,
    lineKey: owner.lineKey, threadKey: context.threadId, channel: owner.channel,
    sender: owner.ownerId, payload: payload(providerEventId, text) });
}

test('second coalesced owner event supplies the command, not run ownerRequestKey', () => {
  const f = fixture();
  try {
    const first = admit(f.journal, 'provider-first', 'Please think about my day').event;
    const second = admit(f.journal, 'provider-second', '/loop add Send the signed deck').event;
    f.journal.startRun({ runId: owner.runId, eventId: first.id as string });
    const intent = f.journal.prepareExplicitOwnerLoopIntent(owner);
    assert.equal(intent.sourceEventId, second.id);
    assert.equal(intent.text, 'Send the signed deck');
    assert.equal(intent.status, 'pending');
    const row = f.journal.getAction(intent.actionId)!;
    assert.equal(row.run_id, owner.runId);
    assert.equal(row.source_event_id, second.id);
    assert.equal(row.source_payload_hash, second.payload_hash);
    assert.equal(row.source_text_hash, digest(intent.text));
    assert.equal(JSON.stringify(row).includes('Send the signed deck'), false);
    assert.throws(() => f.journal.recordActionIntent({ actionId: 'forged', runId: owner.runId,
      kind: 'owner_loop_add_admitted', payloadHash: digest('forged') }), /admitted-event proof/);
    assert.equal(f.journal.prepareExplicitOwnerLoopIntent(owner).actionId, intent.actionId);
    f.journal.beginAction(intent.actionId);
    assert.throws(() => f.journal.settleAction(intent.actionId, 'succeeded',
      { text: 'Send the signed deck' }), /compact verified receipt/);
    f.journal.settleExplicitOwnerLoopIntent(intent.actionId, digest('receipt:accepted'));
    const replay = f.journal.prepareExplicitOwnerLoopIntent(owner);
    assert.equal(replay.status, 'succeeded');
    assert.equal(replay.receiptHash, digest('receipt:accepted'));
    assert.equal(JSON.stringify(f.journal.getAction(intent.actionId)).includes('Send the signed deck'), false);
  } finally { f.close(); }
});

test('pending intent adopts a replacement run and changed provider payload cannot reuse it', () => {
  const f = fixture();
  try {
    const admitted = admit(f.journal, 'provider-one', '/loop add Send the signed deck');
    const source = admitted.event.id as string;
    f.journal.startRun({ runId: owner.runId, eventId: source });
    const first = f.journal.prepareExplicitOwnerLoopIntent(owner);
    f.journal.cancelRun(owner.runId);
    f.journal.startRun({ runId: 'run-2', eventId: source });
    const again = f.journal.prepareExplicitOwnerLoopIntent({ ...owner, runId: 'run-2' });
    assert.equal(again.actionId, first.actionId);
    assert.equal(f.journal.getAction(first.actionId)?.run_id, 'run-2');
    assert.throws(() => admit(f.journal, 'provider-one', '/loop add Different text'), /identity conflict/);
    assert.equal(admit(f.journal, 'provider-one', '/loop add Send the signed deck').admitted, false);
  } finally { f.close(); }
});

test('crashed executing mutation stays held when canonical row is absent', () => {
  const f = fixture();
  try {
    const source = admit(f.journal, 'provider-uncertain', '/loop add Send the signed deck').event.id as string;
    f.journal.startRun({ runId: owner.runId, eventId: source });
    const intent = f.journal.prepareExplicitOwnerLoopIntent(owner);
    f.journal.beginAction(intent.actionId);
    f.journal.close();
    const reopened = new OperationalJournal(f.path);
    try {
      assert.equal(reopened.recoverInterrupted().actions, 1);
      reopened.startRun({ runId: 'run-2', eventId: source });
      const held = reopened.prepareExplicitOwnerLoopIntent({ ...owner, runId: 'run-2' });
      assert.equal(held.status, 'unknown');
      assert.equal(held.receiptHash, null);
      assert.throws(() => reopened.beginAction(held.actionId), /not eligible/);
      assert.throws(() => reopened.settleExplicitOwnerLoopIntent(held.actionId, digest('unproved')), /not eligible/);
      const cutoff = Date.now() + 365 * 24 * 60 * 60 * 1000;
      reopened.pruneTerminal(cutoff, cutoff);
      assert.equal(reopened.getAction(held.actionId)?.status, 'unknown');
      assert.notEqual(reopened.getInbound(source)?.payload, '', 'pending event is never scrubbed');
    } finally { reopened.close(); }
  } finally { rmSync(join(f.path, '..'), { recursive: true, force: true }); }
});

test('terminal action survives 180-day cutoff with hashes while resolved quote is scrubbed', () => {
  const f = fixture();
  try {
    const first = admit(f.journal, 'provider-first', 'An earlier owner message').event.id as string;
    const source = admit(f.journal, 'provider-command', '/loop add Send the signed deck').event.id as string;
    f.journal.startRun({ runId: owner.runId, eventId: first });
    const intent = f.journal.prepareExplicitOwnerLoopIntent(owner);
    f.journal.beginAction(intent.actionId);
    f.journal.settleExplicitOwnerLoopIntent(intent.actionId, digest('effect receipt'));
    f.journal.completeRun(owner.runId);
    f.journal.resolveInbound(first, 'resolved');
    f.journal.resolveInbound(source, 'resolved');
    const cutoff = Date.now() + 365 * 24 * 60 * 60 * 1000;
    f.journal.pruneTerminal(cutoff, cutoff);
    assert.equal(f.journal.getAction(intent.actionId)?.status, 'succeeded');
    assert.deepEqual(JSON.parse(f.journal.getAction(intent.actionId)?.result_json as string),
      { receipt_hash: digest('effect receipt') });
    assert.equal(f.journal.getInbound(source)?.payload, '');
    assert.equal(f.journal.getInbound(first)?.payload, '');
    assert.equal(f.journal.getInbound(source)?.payload_hash, digest(payload('provider-command', '/loop add Send the signed deck')));
    assert.equal(admit(f.journal, 'provider-command', '/loop add Send the signed deck').admitted, false);
    assert.throws(() => admit(f.journal, 'provider-command', '/loop add Changed'), /identity conflict/);
    f.journal.close();
    const reopened = new OperationalJournal(f.path);
    try {
      assert.equal(reopened.getAction(intent.actionId)?.status, 'succeeded');
      assert.equal(admit(reopened, 'provider-command', '/loop add Send the signed deck').admitted, false);
    } finally { reopened.close(); }
  } finally { rmSync(join(f.path, '..'), { recursive: true, force: true }); }
});

test('model-like text without explicit owner command never authorizes an intent', () => {
  const f = fixture();
  try {
    const first = admit(f.journal, 'provider-one', 'Send the signed deck').event.id as string;
    f.journal.startRun({ runId: owner.runId, eventId: first });
    assert.throws(() => f.journal.prepareExplicitOwnerLoopIntent(owner), /exactly one explicit owner command/);
    assert.equal(f.journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 0);
  } finally { f.close(); }
});

test('wrong configured owner or two explicit commands cannot mint a bound action', () => {
  const f = fixture();
  try {
    const first = admit(f.journal, 'provider-one', '/loop add Send the signed deck').event.id as string;
    admit(f.journal, 'provider-two', '/loop add Call the contractor');
    f.journal.startRun({ runId: owner.runId, eventId: first });
    assert.throws(() => f.journal.prepareExplicitOwnerLoopIntent(owner), /exactly one explicit owner command/);
    assert.equal(f.journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 0);
    f.journal.resolveInbound(first, 'resolved');
    assert.throws(() => f.journal.prepareExplicitOwnerLoopIntent({ ...owner, ownerId: 'someone-else' }), /not from the configured owner/);
    assert.equal(f.journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 0);
  } finally { f.close(); }
});

test('run uses its captured admission boundary, excluding a later same-thread command', () => {
  const f = fixture();
  try {
    const first = admit(f.journal, 'provider-first', 'Earlier message').event.id as string;
    f.journal.startRun({ runId: owner.runId, eventId: first });
    const later = admit(f.journal, 'provider-later', '/loop add Send the signed deck').event.id as string;
    assert.throws(() => f.journal.prepareExplicitOwnerLoopIntent(owner), /exactly one explicit owner command/);
    f.journal.cancelRun(owner.runId);
    f.journal.startRun({ runId: 'run-2', eventId: first });
    assert.equal(f.journal.prepareExplicitOwnerLoopIntent({ ...owner, runId: 'run-2' }).sourceEventId, later);
  } finally { f.close(); }
});

test('prepared reply covering coalesced command keeps both payloads until its proof retires', () => {
  const f = fixture();
  try {
    const first = admit(f.journal, 'provider-first', 'Earlier message').event.id as string;
    const source = admit(f.journal, 'provider-command', '/loop add Send the signed deck').event.id as string;
    f.journal.startRun({ runId: owner.runId, eventId: first });
    const intent = f.journal.prepareExplicitOwnerLoopIntent(owner);
    f.journal.beginAction(intent.actionId);
    f.journal.settleExplicitOwnerLoopIntent(intent.actionId, digest('receipt'));
    f.journal.prepareReply({ replyId: 'reply', runId: owner.runId, eventId: first,
      coveredEventIds: [first, source], dispatchId: 'dispatch', kind: 'interactive',
      destination: 'owner-target', payload: 'Acknowledged' });
    f.journal.completeRun(owner.runId);
    f.journal.resolveInbound(first, 'resolved');
    f.journal.resolveInbound(source, 'resolved');
    const cutoff = Date.now() + 365 * 24 * 60 * 60 * 1000;
    f.journal.pruneTerminal(cutoff, cutoff);
    assert.notEqual(f.journal.getInbound(first)?.payload, '');
    assert.notEqual(f.journal.getInbound(source)?.payload, '');
    f.journal.cancelPreparedDispatch('dispatch');
    f.journal.pruneTerminal(cutoff, cutoff);
    assert.equal(f.journal.getInbound(first)?.payload, '');
    assert.equal(f.journal.getInbound(source)?.payload, '');
  } finally { f.close(); }
});

test('generic unknown action in coalesced thread blocks owner command payload scrub', () => {
  const f = fixture();
  try {
    const first = admit(f.journal, 'provider-first', 'Earlier message').event.id as string;
    const source = admit(f.journal, 'provider-command', '/loop add Send the signed deck').event.id as string;
    f.journal.startRun({ runId: owner.runId, eventId: first });
    const intent = f.journal.prepareExplicitOwnerLoopIntent(owner);
    f.journal.beginAction(intent.actionId);
    f.journal.settleExplicitOwnerLoopIntent(intent.actionId, digest('receipt'));
    f.journal.recordActionIntent({ actionId: 'other-unknown', runId: owner.runId,
      kind: 'external-other', payloadHash: digest('other') });
    f.journal.beginAction('other-unknown');
    f.journal.settleAction('other-unknown', 'unknown');
    f.journal.completeRun(owner.runId);
    f.journal.resolveInbound(first, 'resolved');
    f.journal.resolveInbound(source, 'resolved');
    const cutoff = Date.now() + 365 * 24 * 60 * 60 * 1000;
    f.journal.pruneTerminal(cutoff, cutoff);
    assert.notEqual(f.journal.getInbound(first)?.payload, '');
    assert.notEqual(f.journal.getInbound(source)?.payload, '');
    assert.equal(f.journal.getAction('other-unknown')?.source_event_id, null);
  } finally { f.close(); }
});

test('unacknowledged activation obligation keeps owner command payload', () => {
  const f = fixture();
  try {
    const source = admit(f.journal, 'provider-command', '/loop add Send the signed deck').event.id as string;
    f.journal.startRun({ runId: owner.runId, eventId: source });
    const intent = f.journal.prepareExplicitOwnerLoopIntent(owner);
    f.journal.beginAction(intent.actionId);
    f.journal.settleExplicitOwnerLoopIntent(intent.actionId, digest('receipt'));
    f.journal.db.prepare('INSERT INTO activation_obligation(id,event_id,created_at) VALUES(1,?,?)')
      .run(source, Date.now());
    f.journal.completeRun(owner.runId);
    f.journal.resolveInbound(source, 'resolved');
    const cutoff = Date.now() + 365 * 24 * 60 * 60 * 1000;
    f.journal.pruneTerminal(cutoff, cutoff);
    assert.notEqual(f.journal.getInbound(source)?.payload, '');
  } finally { f.close(); }
});

test('retention cannot recycle the highest same-thread rowid below a running-run boundary', () => {
  const f = fixture();
  try {
    const first = admit(f.journal, 'provider-first', 'Earlier message').event.id as string;
    const high = admit(f.journal, 'provider-high', 'Resolved older message').event.id as string;
    f.journal.resolveInbound(high, 'resolved');
    f.journal.startRun({ runId: owner.runId, eventId: first });
    const bound = f.journal.getRun(owner.runId)?.admitted_through as number;
    const cutoff = Date.now() + 365 * 24 * 60 * 60 * 1000;
    f.journal.pruneTerminal(cutoff, cutoff);
    assert.ok(f.journal.getInbound(high), 'a running run preserves even a resolved high rowid');
    const later = admit(f.journal, 'provider-later', '/loop add Send the signed deck').event.id as string;
    const laterRowid = f.journal.db.prepare('SELECT rowid AS sequence FROM inbound_events WHERE id=?').get(later)!.sequence as number;
    assert.ok(laterRowid > bound);
    assert.throws(() => f.journal.prepareExplicitOwnerLoopIntent(owner), /exactly one explicit owner command/);
  } finally { f.close(); }
});

test('other-thread terminal pruning cannot move an admission below the protected thread bound', () => {
  const f = fixture();
  try {
    const first = admit(f.journal, 'provider-first', 'Earlier message').event.id as string;
    const high = admit(f.journal, 'provider-high', 'Resolved older message').event.id as string;
    const other = f.journal.admitInbound({ eventId: 'other-thread', accountKey: owner.accountId,
      lineKey: owner.lineKey, threadKey: 'telegram:other', channel: owner.channel,
      sender: owner.ownerId, payload: '{"other":true}' }).event.id as string;
    f.journal.resolveInbound(high, 'resolved');
    f.journal.resolveInbound(other, 'resolved');
    f.journal.startRun({ runId: owner.runId, eventId: first });
    const bound = f.journal.getRun(owner.runId)?.admitted_through as number;
    const cutoff = Date.now() + 365 * 24 * 60 * 60 * 1000;
    f.journal.pruneTerminal(cutoff, cutoff);
    assert.equal(f.journal.getInbound(other), undefined);
    assert.ok(f.journal.getInbound(high));
    const later = admit(f.journal, 'provider-later', '/loop add Send the signed deck').event.id as string;
    const laterRowid = f.journal.db.prepare('SELECT rowid AS sequence FROM inbound_events WHERE id=?').get(later)!.sequence as number;
    assert.ok(laterRowid > bound);
    assert.throws(() => f.journal.prepareExplicitOwnerLoopIntent(owner), /exactly one explicit owner command/);
  } finally { f.close(); }
});

test('a running run migrated from schema v4 has no guessed command admission boundary', () => {
  const f = fixture();
  try {
    const source = admit(f.journal, 'provider-command', '/loop add Send the signed deck').event.id as string;
    f.journal.startRun({ runId: owner.runId, eventId: source });
    f.journal.db.exec('UPDATE runs SET admitted_through=NULL; PRAGMA user_version=4');
    f.journal.close();
    const migrated = new OperationalJournal(f.path);
    try {
      assert.equal(migrated.db.prepare('PRAGMA user_version').get()!.user_version, 7);
      assert.throws(() => migrated.prepareExplicitOwnerLoopIntent(owner), /admission boundary is unavailable/);
      migrated.startRun({ runId: 'replacement', eventId: source });
      assert.equal(migrated.prepareExplicitOwnerLoopIntent({ ...owner, runId: 'replacement' }).sourceEventId, source);
    } finally { migrated.close(); }
  } finally { rmSync(join(f.path, '..'), { recursive: true, force: true }); }
});
