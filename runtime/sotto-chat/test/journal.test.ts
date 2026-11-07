import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { OperationalJournal, SCHEDULED_TEXT_MAX_ATTEMPTS } from '../src/journal/index.js';
import type { ActivationIdentity } from '../src/activation.js';

const activationIdentity: ActivationIdentity = {
  tenant_id: 'managed-tenant', project_fingerprint: 'a'.repeat(64), owner_id: '+15555550100',
  account_id: 'tenant-a', line_key: 'line-1', provider_thread_id: 'thread-1',
};

function withJournal(body: (journal: OperationalJournal, path: string) => void): void {
  const dir = mkdtempSync(join(tmpdir(), 'sotto-chat-journal-'));
  const path = join(dir, 'operations.sqlite3');
  const journal = new OperationalJournal(path);
  try { body(journal, path); }
  finally { try { journal.close(); } catch { /* an earlier reopen test already closed it */ }
    rmSync(dir, { recursive: true, force: true }); }
}

function admit(journal: OperationalJournal, eventId = 'provider-1') {
  return journal.admitInbound({ eventId, accountKey: 'tenant-a', lineKey: 'line-1',
    threadKey: 'thread-1', channel: 'photon', sender: '+15555550100', payload: '{"text":"hello"}' });
}

test('journal refuses oversized admission and a future schema without rewriting it', () => {
  withJournal((journal, path) => {
    assert.throws(() => journal.admitInbound({ eventId: 'large', accountKey: 'tenant-a', lineKey: 'line-1',
      threadKey: 'thread-1', channel: 'photon', sender: 'owner', payload: 'x'.repeat(1_048_577) }), /allowed bounds/);
    assert.equal(journal.listPendingInbound().length, 0);
    journal.db.exec('PRAGMA user_version=99');
    assert.throws(() => new OperationalJournal(path), /schema is not supported/);
    assert.equal(journal.db.prepare('PRAGMA user_version').get()!.user_version, 99);
  });
});

test('schema v1 migration adds nullable source proof without losing prepared reply or pending inbound', () => {
  withJournal((_journal, path) => {
    const legacy = new OperationalJournal(path);
    const eventId = admit(legacy, 'legacy-event').event.id as string;
    legacy.startRun({ runId: 'legacy-run', eventId });
    legacy.prepareReply({ replyId: 'legacy-reply', runId: 'legacy-run', eventId,
      dispatchId: 'legacy-dispatch', kind: 'interactive', destination: 'owner', payload: 'prepared before upgrade' });
    legacy.db.exec('ALTER TABLE prepared_replies DROP COLUMN source_revision; PRAGMA user_version=1;');
    legacy.close();

    const migrated = new OperationalJournal(path);
    try {
      assert.equal(migrated.db.prepare('PRAGMA user_version').get()!.user_version, 7);
      const reply = migrated.getPreparedReply('legacy-reply');
      assert.equal(reply?.payload, 'prepared before upgrade');
      assert.equal(reply?.source_revision, null);
      assert.equal(migrated.getDispatch('legacy-dispatch')?.status, 'prepared');
      assert.equal(migrated.getInbound(eventId)?.outcome, 'pending');
      assert.equal(migrated.listPendingInbound().length, 1);
    } finally { migrated.close(); }
  });
});

test('schema v5 migration leaves accepted and ambiguous historical routes unproved', () => {
  withJournal((_journal, path) => {
    const legacy = new OperationalJournal(path);
    legacy.prepareDispatch({ dispatchId: 'old-accepted', kind: 'scheduled_text',
      destination: 'owner', payload: 'old text' });
    legacy.beginDispatch('old-accepted');
    legacy.settleDispatch('old-accepted', 1, 'accepted', 'provider-1');
    legacy.prepareDispatch({ dispatchId: 'old-unknown', kind: 'gallery',
      destination: 'owner', payload: 'old cards' });
    legacy.beginDispatch('old-unknown');
    legacy.settleDispatch('old-unknown', 1, 'unknown');
    legacy.db.exec('ALTER TABLE dispatches DROP COLUMN route_digest; PRAGMA user_version=5;');
    legacy.close();
    const migrated = new OperationalJournal(path);
    try {
      assert.equal(migrated.db.prepare('PRAGMA user_version').get()!.user_version, 7);
      assert.equal(migrated.getDispatch('old-accepted')?.route_digest, null);
      assert.equal(migrated.getDispatch('old-accepted')?.status, 'accepted');
      assert.equal(migrated.getDispatch('old-unknown')?.route_digest, null);
      assert.equal(migrated.getDispatch('old-unknown')?.status, 'unknown');
    } finally { migrated.close(); }
  });
});

test('discovery seed binds the first admitted owner thread and activation obligation atomically', () => {
  withJournal((journal, path) => {
    const { provider_thread_id: _old, ...base } = activationIdentity;
    const line = '+15555550199';
    const thread = `imessage:iMessage;-;+15555550100~${line}`;
    const seed = { ...base, expected_provider_line: line };
    const candidate = { ...base, provider_thread_id: thread };
    assert.equal(journal.configureActivationDiscovery(seed), null);
    const input = { eventId: 'first', accountKey: base.account_id, lineKey: base.line_key,
      threadKey: thread, channel: 'photon', sender: base.owner_id, payload: '{"text":"owner"}', activation: candidate };
    journal.db.exec(`CREATE TRIGGER stop_first_activation BEFORE INSERT ON activation_obligation
      BEGIN SELECT RAISE(ABORT,'fixture rollback'); END`);
    assert.throws(() => journal.admitInbound(input), /fixture rollback/);
    assert.equal(journal.getActivationIdentity(), null);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM activation_identity').get()!.n, 0);
    assert.equal(journal.listPendingInbound().length, 0);
    journal.db.exec('DROP TRIGGER stop_first_activation');
    const first = journal.admitInbound(input);
    assert.equal(first.admitted, true);
    assert.deepEqual(journal.getActivationIdentity(), candidate);
    assert.equal(journal.getActivationEvidence()?.event_id, first.event.id);
    assert.equal(journal.admitInbound(input).admitted, false);
    assert.throws(() => journal.admitInbound({ ...input, eventId: 'other', threadKey: `${thread}x`,
      activation: { ...candidate, provider_thread_id: `${thread}x` } }), /destination conflict|candidate/);
    assert.equal(journal.listPendingInbound().length, 1);
    assert.equal(journal.acknowledgeActivation(String(first.event.id)), true);
    assert.equal(journal.isActivationAcknowledged(), true);
    journal.resolveInbound(String(first.event.id), 'resolved');
    assert.equal(journal.pruneTerminal(Date.now() + 1000).events, 1);
    assert.equal(journal.getInbound(String(first.event.id)), undefined);
    assert.deepEqual(journal.getActivationIdentity(), candidate,
      'retention may remove acknowledged input but cannot remove destination binding');
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      assert.deepEqual(reopened.configureActivationDiscovery(seed), candidate);
      assert.equal(reopened.isActivationAcknowledged(), true);
      assert.throws(() => reopened.configureActivationDiscovery({ ...seed, expected_provider_line: '+15555550200' }), /seed/);
      assert.throws(() => reopened.configureActivationIdentity(candidate), /discovery state/);
    } finally { reopened.close(); }
  });
});

test('activation identity binds separately and never reuses an old journal for a changed project or tenant', () => {
  withJournal((journal, path) => {
    journal.configureActivationIdentity(activationIdentity);
    journal.configureActivationIdentity({ ...activationIdentity });
    const first = admit(journal, 'first-owner-message').event.id as string;
    assert.equal(journal.getActivationEvidence()?.event_id, first);
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      assert.throws(() => reopened.configureActivationIdentity(undefined), /does not match/);
      assert.throws(() => reopened.configureActivationIdentity({ ...activationIdentity, tenant_id: 'other' }), /does not match/);
      assert.throws(() => reopened.configureActivationIdentity({ ...activationIdentity, project_fingerprint: 'b'.repeat(64) }), /does not match/);
      assert.equal(reopened.getActivationEvidence(), null);
      assert.equal(reopened.acknowledgeActivation(first), false);
      reopened.configureActivationIdentity(activationIdentity);
      assert.equal(reopened.getActivationEvidence()?.provider_event_id, 'first-owner-message');
      assert.equal(reopened.getActivationEvidence()?.tenant_id, 'managed-tenant');
    } finally { reopened.close(); }
  });
});

test('only first matching Photon owner DM creates the activation obligation; duplicates and other scopes cannot replace it', () => {
  withJournal(journal => {
    journal.configureActivationIdentity(activationIdentity);
    const different = [
      { accountKey: 'other' }, { lineKey: 'other' }, { threadKey: 'other' },
      { sender: 'stranger' }, { channel: 'telegram' },
    ];
    for (let index = 0; index < different.length; index++) {
      journal.admitInbound({ eventId: `mismatch-${index}`, accountKey: 'tenant-a', lineKey: 'line-1',
        threadKey: 'thread-1', channel: 'photon', sender: '+15555550100', payload: '{}', ...different[index] });
    }
    assert.equal(journal.getActivationEvidence(), null);
    const first = admit(journal, 'first').event.id as string;
    assert.equal(admit(journal, 'first').admitted, false);
    admit(journal, 'second');
    assert.equal(journal.getActivationEvidence()?.event_id, first);
    assert.equal(journal.getActivationEvidence()?.provider_event_id, 'first');
    assert.equal(journal.acknowledgeActivation('b'.repeat(64)), false);
    assert.equal(journal.acknowledgeActivation(first), true);
    assert.equal(journal.acknowledgeActivation(first), true);
    assert.equal(journal.getActivationEvidence(), null);
    const third = admit(journal, 'third').event.id as string;
    assert.equal(journal.getActivationEvidence()?.event_id, third);
    assert.equal(journal.acknowledgeActivation(first), false);
  });
});

test('new owner DM after ACK repairs missing receipt; duplicate replay and stale ACK cannot replace it', () => {
  withJournal((journal, path) => {
    journal.configureActivationIdentity(activationIdentity);
    const first = admit(journal, 'original-owner-dm').event.id as string;
    journal.resolveInbound(first, 'resolved');
    assert.equal(journal.acknowledgeActivation(first), true);
    assert.equal(admit(journal, 'original-owner-dm').admitted, false);
    assert.equal(journal.getActivationEvidence(), null);
    assert.equal(journal.pruneTerminal(Date.now() + 10_000).events, 1);
    assert.equal(journal.getInbound(first), undefined);
    // The completed slot retains an event-ID tombstone even after the 90-day
    // raw inbound row is gone; the same provider event cannot reopen activation.
    admit(journal, 'original-owner-dm');
    assert.equal(journal.getActivationEvidence(), null);

    const repair = admit(journal, 'new-owner-dm').event.id as string;
    assert.notEqual(repair, first);
    assert.equal(journal.getActivationEvidence()?.event_id, repair);
    assert.equal(journal.acknowledgeActivation(first), false);
    assert.equal(journal.getActivationEvidence()?.event_id, repair);
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      reopened.configureActivationIdentity(activationIdentity);
      assert.equal(reopened.getActivationEvidence()?.event_id, repair);
      assert.equal(reopened.acknowledgeActivation(first), false);
      assert.equal(reopened.acknowledgeActivation(repair), true);
      assert.equal(reopened.acknowledgeActivation(repair), true);
    } finally { reopened.close(); }
  });
});

test('activation admission rolls back inbound if its obligation cannot commit', () => {
  withJournal(journal => {
    journal.configureActivationIdentity(activationIdentity);
    journal.db.exec("CREATE TRIGGER fail_activation BEFORE INSERT ON activation_obligation BEGIN SELECT RAISE(ABORT, 'forced failure'); END;");
    assert.throws(() => admit(journal, 'atomic'), /forced failure/);
    assert.equal(journal.listPendingInbound().length, 0);
    assert.equal(journal.getActivationEvidence(), null);
    journal.db.exec('DROP TRIGGER fail_activation');
    const first = admit(journal, 'atomic').event.id as string;
    assert.equal(journal.acknowledgeActivation(first), true);
    journal.db.exec("CREATE TRIGGER fail_repair BEFORE UPDATE ON activation_obligation BEGIN SELECT RAISE(ABORT, 'forced repair failure'); END;");
    assert.throws(() => admit(journal, 'repair-atomic'), /forced repair failure/);
    assert.equal(journal.listPendingInbound().length, 1);
    assert.equal(journal.getActivationEvidence(), null);
    journal.db.exec('DROP TRIGGER fail_repair');
    assert.equal(admit(journal, 'repair-atomic').admitted, true);
    assert.equal(journal.getActivationEvidence()?.provider_event_id, 'repair-atomic');
  });
});

test('pending activation survives stop, reopen and retention; ACK releases raw inbound but preserves exact idempotency', () => {
  withJournal((journal, path) => {
    journal.configureActivationIdentity(activationIdentity);
    const first = admit(journal, 'stop-before-activation').event.id as string;
    journal.resolveInbound(first, 'cancelled');
    const cutoff = Date.now() + 10_000;
    assert.equal(journal.pruneTerminal(cutoff).events, 0);
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      reopened.configureActivationIdentity(activationIdentity);
      reopened.recoverInterrupted();
      assert.equal(reopened.getActivationEvidence()?.event_id, first);
      assert.equal(reopened.getInbound(first)?.outcome, 'cancelled');
      assert.equal(reopened.acknowledgeActivation(first), true);
      assert.equal(reopened.pruneTerminal(cutoff).events, 1);
      assert.equal(reopened.getInbound(first), undefined);
      assert.equal(reopened.acknowledgeActivation(first), true);
      assert.equal(reopened.getActivationEvidence(), null);
    } finally { reopened.close(); }
  });
});

test('inbound admission persists replay data and scopes provider IDs by tenant and line', () => {
  withJournal((journal, path) => {
    const first = admit(journal);
    assert.equal(first.admitted, true);
    assert.equal(first.event.payload, '{"text":"hello"}');
    assert.equal(admit(journal).admitted, false);
    assert.throws(() => journal.admitInbound({ eventId: 'provider-1', accountKey: 'tenant-a',
      lineKey: 'line-1', threadKey: 'thread-1', channel: 'photon', sender: '+15555550100',
      payload: '{"text":"changed"}' }), /identity conflict/);
    const anotherLine = journal.admitInbound({ eventId: 'provider-1', accountKey: 'tenant-a',
      lineKey: 'line-2', threadKey: 'thread-1', channel: 'photon', sender: '+15555550100',
      payload: '{"text":"hello"}' });
    assert.notEqual(anotherLine.event.id, first.event.id);
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      reopened.recoverInterrupted();
      assert.equal(reopened.getInbound(first.event.id as string)?.payload, '{"text":"hello"}');
      assert.equal(reopened.listPendingInbound().length, 2);
    } finally { reopened.close(); }
  });
});

test('prepared reply is independent of transcript, fenced, and atomically bound to its dispatch', () => {
  withJournal((journal) => {
    const eventId = admit(journal).event.id as string;
    journal.startRun({ runId: 'run-1', eventId });
    const reply = journal.prepareReply({ replyId: 'reply-1', runId: 'run-1', eventId,
      dispatchId: 'dispatch-1', kind: 'interactive', destination: 'photon:owner', payload: 'Final answer' });
    assert.equal(reply.payload, 'Final answer');
    assert.equal(journal.getDispatch('dispatch-1')?.status, 'prepared');
    assert.throws(() => journal.prepareReply({ replyId: 'reply-1', runId: 'run-1', eventId,
      dispatchId: 'dispatch-2', kind: 'interactive', destination: 'photon:owner', payload: 'Second answer' }), /reply id already used/);
    assert.equal(journal.getDispatch('dispatch-2'), undefined, 'failed reply insert left no partial dispatch');
    const nextEvent = admit(journal, 'provider-2').event.id as string;
    journal.startRun({ runId: 'run-2', eventId: nextEvent });
    assert.equal(journal.isRunCurrent('run-1'), false);
    assert.throws(() => journal.beginDispatch('dispatch-1'), /stale run/);
    assert.throws(() => journal.recordActionIntent({ actionId: 'action-1', runId: 'run-1',
      kind: 'google-send', payloadHash: 'abcd' }), /stale/);
  });
});

test('dispatching becomes unknown on reopen; interactive/gallery never replay automatically', () => {
  withJournal((journal, path) => {
    journal.prepareDispatch({ dispatchId: 'interactive-1', kind: 'interactive',
      destination: 'photon:owner', payload: 'reply' });
    const attempt = journal.beginDispatch('interactive-1');
    assert.equal(attempt.number, 1);
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      assert.equal(reopened.getDispatch('interactive-1')?.status, 'dispatching',
        'opening a handle alone is not proof that the sender crashed');
      reopened.recoverInterrupted();
      assert.equal(reopened.getDispatch('interactive-1')?.status, 'unknown');
      assert.equal(reopened.getAttempts('interactive-1')[0]?.status, 'unknown');
      assert.throws(() => reopened.beginDispatch('interactive-1'), /not eligible/);
      assert.throws(() => reopened.retryScheduledText('interactive-1'), /forbidden/);
      assert.equal(reopened.listRecoverableDispatches()[0]?.id, 'interactive-1');
      reopened.settleDispatch('interactive-1', 1, 'accepted', 'provider-id-1');
      assert.equal(reopened.getDispatch('interactive-1')?.provider_message_id, 'provider-id-1');
    } finally { reopened.close(); }
  });
});

test('unknown scheduled text holds; definite rejection permits bounded retry; gallery has late proof', () => {
  withJournal((journal) => {
    journal.prepareDispatch({ dispatchId: 'text-1', kind: 'scheduled_text',
      destination: 'photon:owner', payload: 'Morning brief', maxAttempts: 2 });
    assert.throws(() => journal.prepareDispatch({ dispatchId: 'text-1', kind: 'scheduled_text',
      destination: 'photon:other', payload: 'Morning brief', maxAttempts: 2 }), /identity conflict/);
    const first = journal.beginDispatch('text-1');
    journal.settleDispatch('text-1', first.number as number, 'unknown');
    assert.throws(() => journal.retryScheduledText('text-1'), /unresolved/);
    assert.deepEqual(journal.getAttempts('text-1').map(a => a.status), ['unknown']);

    journal.prepareDispatch({ dispatchId: 'rejected-text', kind: 'scheduled_text',
      destination: 'photon:owner', payload: 'Morning brief rejected', maxAttempts: 2 });
    journal.beginDispatch('rejected-text');
    journal.settleDispatch('rejected-text', 1, 'rejected');
    const second = journal.retryScheduledText('rejected-text');
    assert.equal(second.number, 2);
    journal.settleDispatch('rejected-text', second.number as number, 'rejected');
    assert.throws(() => journal.retryScheduledText('rejected-text'), /attempt limit/);
    assert.deepEqual(journal.getAttempts('rejected-text').map(a => a.status), ['rejected', 'rejected']);
    assert.equal(SCHEDULED_TEXT_MAX_ATTEMPTS, 96);

    journal.prepareDispatch({ dispatchId: 'gallery-1', kind: 'gallery',
      destination: 'photon:owner', payload: '{"images":[1,2,3,4]}' });
    const gallery = journal.beginDispatch('gallery-1');
    journal.settleDispatch('gallery-1', gallery.number as number, 'unknown');
    assert.throws(() => journal.retryScheduledText('gallery-1'), /forbidden/);
    journal.settleDispatch('gallery-1', gallery.number as number, 'accepted', 'parent-id',
      ['parent-id', 'image-1', 'image-2', 'image-3', 'image-4']);
    assert.deepEqual(JSON.parse(journal.getDispatch('gallery-1')?.provider_message_ids as string),
      ['parent-id', 'image-1', 'image-2', 'image-3', 'image-4']);
  });
});

test('action execution is fenced; crash ambiguity and terminal pruning preserve unresolved proof', () => {
  withJournal((journal, path) => {
    const eventId = admit(journal).event.id as string;
    journal.startRun({ runId: 'run-1', eventId });
    journal.recordActionIntent({ actionId: 'action-1', runId: 'run-1', kind: 'bridge-send', payloadHash: 'same' });
    journal.beginAction('action-1');
    journal.prepareDispatch({ dispatchId: 'unknown-gallery', kind: 'gallery',
      destination: 'photon:owner', payload: 'uncertain' });
    journal.beginDispatch('unknown-gallery');
    journal.prepareDispatch({ dispatchId: 'accepted-text', kind: 'scheduled_text',
      destination: 'photon:owner', payload: 'delivered' });
    journal.beginDispatch('accepted-text');
    journal.settleDispatch('accepted-text', 1, 'accepted', 'id');
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      reopened.recoverInterrupted();
      assert.equal(reopened.getAction('action-1')?.status, 'unknown');
      assert.equal(reopened.getDispatch('unknown-gallery')?.status, 'unknown');
      assert.throws(() => reopened.beginAction('action-1'), /not eligible/);
      reopened.pruneTerminal(Date.now() + 10_000);
      assert.equal(reopened.getDispatch('unknown-gallery')?.payload, 'uncertain');
      assert.equal(reopened.getAction('action-1')?.status, 'unknown');
      assert.equal(reopened.getDispatch('accepted-text'), undefined);
    } finally { reopened.close(); }
  });
});

test('late acceptance settles an unknown text attempt without a second provider contact', () => {
  withJournal((journal, path) => {
    journal.prepareDispatch({ dispatchId: 'text', kind: 'scheduled_text',
      destination: 'photon:owner', payload: 'brief', maxAttempts: 2 });
    journal.beginDispatch('text');
    journal.settleDispatch('text', 1, 'unknown');
    assert.throws(() => journal.retryScheduledText('text'), /unresolved/);
    journal.settleDispatch('text', 1, 'accepted', 'first-provider-id');
    assert.equal(journal.getDispatch('text')?.status, 'accepted');
    assert.deepEqual(journal.getAttempts('text').map(a => a.status), ['accepted']);
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      assert.deepEqual(reopened.recoverInterrupted(), { dispatches: 0, actions: 0 });
      assert.equal(reopened.getDispatch('text')?.provider_message_id, 'first-provider-id');
    } finally { reopened.close(); }
  });
});

test('unknown interactive prefix receipts survive restart without becoming accepted conversation proof', () => {
  withJournal((journal, path) => {
    const eventId = admit(journal, 'partial-owner-event').event.id as string;
    journal.startRun({runId: 'partial-run', eventId});
    journal.prepareReply({replyId: 'partial-reply', runId: 'partial-run', eventId,
      dispatchId: 'partial-dispatch', kind: 'interactive', destination: 'owner',
      payload: 'A reply spanning multiple posts', sourceRevision: 'owner-only-context-v1'});
    journal.beginDispatch('partial-dispatch');
    journal.settleDispatch('partial-dispatch', 1, 'unknown', undefined, ['known-prefix']);
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      const dispatch = reopened.getDispatch('partial-dispatch')!;
      assert.equal(dispatch.status, 'unknown');
      assert.equal(dispatch.provider_message_id, null);
      assert.equal(dispatch.provider_message_ids, '["known-prefix"]');
      assert.equal(reopened.getAttempts('partial-dispatch')[0]!.provider_message_id, null);
      const nextEvent = admit(reopened, 'quote-after-partial').event.id as string;
      assert.deepEqual(reopened.quotedReplyForEvent(nextEvent, 'known-prefix'), {status: 'unavailable'});
      assert.throws(() => reopened.beginDispatch('partial-dispatch'));
    } finally { reopened.close(); }
  });
});

test('replacement run adopts only uncontacted same-thread action intent and reuses completed result', () => {
  withJournal((journal) => {
    const eventOne = admit(journal, 'event-one').event.id as string;
    const eventTwo = admit(journal, 'event-two').event.id as string;
    journal.startRun({ runId: 'run-one', eventId: eventOne });
    journal.recordActionIntent({ actionId: 'intent', runId: 'run-one', kind: 'google-draft', payloadHash: 'hash' });
    journal.startRun({ runId: 'run-two', eventId: eventTwo });
    assert.equal(journal.recordActionIntent({ actionId: 'intent', runId: 'run-two', kind: 'google-draft', payloadHash: 'hash' }).run_id, 'run-two');
    journal.beginAction('intent');
    journal.settleAction('intent', 'succeeded', { draft_id: 'D123' });
    assert.equal(journal.recordActionIntent({ actionId: 'intent', runId: 'run-two', kind: 'google-draft', payloadHash: 'hash' }).status, 'succeeded');
    assert.throws(() => journal.beginAction('intent'), /not eligible/);
  });
});

test('terminal retention prunes completed run and event, preserving unknown receipt metadata', () => {
  withJournal((journal) => {
    const eventId = admit(journal).event.id as string;
    journal.startRun({ runId: 'run', eventId });
    journal.prepareReply({ replyId: 'reply', runId: 'run', eventId, dispatchId: 'accepted',
      kind: 'interactive', destination: 'owner', payload: 'safe to expire' });
    journal.beginDispatch('accepted');
    journal.settleDispatch('accepted', 1, 'accepted', 'provider-id');
    journal.completeRun('run');
    journal.resolveInbound(eventId, 'resolved');
    const result = journal.pruneTerminal(Date.now() + 10_000);
    assert.deepEqual(result, { replies: 1, dispatches: 1, actions: 0, runs: 1, events: 1 });
    assert.equal(journal.getInbound(eventId), undefined);

    journal.prepareDispatch({ dispatchId: 'unknown', kind: 'gallery',
      destination: 'owner', payload: 'private content' });
    journal.beginDispatch('unknown');
    journal.settleDispatch('unknown', 1, 'unknown');
    assert.equal(journal.pruneTerminal(Date.now() + 10_000).dispatches, 0);
    assert.equal(journal.scrubUnknownPayload(Date.now() + 10_000).dispatches, 1);
    assert.equal(journal.getDispatch('unknown')?.payload, '');
    assert.equal(journal.getDispatch('unknown')?.payload_hash?.toString().length, 64);
  });
});

test('delivery receipts may expire before action audit while unresolved proof survives both cutoffs', () => {
  withJournal(journal => {
    const eventId = admit(journal, 'retention-event').event.id as string;
    journal.startRun({ runId: 'retention-run', eventId });
    journal.recordActionIntent({ actionId: 'send-action', runId: 'retention-run', kind: 'send', payloadHash: 'hash' });
    journal.beginAction('send-action');
    journal.settleAction('send-action', 'succeeded', { provider: 'accepted' });
    journal.prepareReply({ replyId: 'retention-reply', runId: 'retention-run', eventId,
      dispatchId: 'retention-dispatch', kind: 'interactive', destination: 'owner', payload: 'answer' });
    journal.beginDispatch('retention-dispatch');
    journal.settleDispatch('retention-dispatch', 1, 'accepted', 'provider-id');
    journal.completeRun('retention-run');
    journal.resolveInbound(eventId, 'resolved');
    journal.prepareDispatch({ dispatchId: 'uncertain-dispatch', kind: 'gallery', destination: 'owner', payload: 'cards' });
    journal.beginDispatch('uncertain-dispatch');
    journal.settleDispatch('uncertain-dispatch', 1, 'unknown');

    const deliveryBefore = Date.now() + 10_000;
    const actionBefore = Date.now() - 10_000;
    assert.deepEqual(journal.pruneTerminal(deliveryBefore, actionBefore),
      { replies: 1, dispatches: 1, actions: 0, runs: 0, events: 0 });
    assert.equal(journal.getAction('send-action')?.status, 'succeeded');
    assert.equal(journal.getDispatch('uncertain-dispatch')?.status, 'unknown');
    assert.equal(journal.getInbound(eventId)?.outcome, 'resolved');
    assert.throws(() => journal.pruneTerminal(actionBefore, deliveryBefore), /invalid retention cutoff/);

    assert.deepEqual(journal.pruneTerminal(deliveryBefore, deliveryBefore),
      { replies: 0, dispatches: 0, actions: 1, runs: 1, events: 1 });
    assert.equal(journal.getDispatch('uncertain-dispatch')?.status, 'unknown');
  });
});

test('age cannot erase accepted reply proof for a pending covered event or a pending action anchor', () => {
  withJournal(journal => {
    const first = admit(journal, 'covered-first').event.id as string;
    const second = admit(journal, 'covered-second').event.id as string;
    journal.startRun({ runId: 'covered-run', eventId: first });
    journal.prepareReply({ replyId: 'covered-reply', runId: 'covered-run', eventId: first,
      coveredEventIds: [first, second], dispatchId: 'covered-dispatch', kind: 'interactive',
      destination: 'owner', payload: 'accepted words' });
    journal.beginDispatch('covered-dispatch');
    journal.settleDispatch('covered-dispatch', 1, 'accepted', 'provider-id');
    journal.completeRun('covered-run');
    journal.resolveInbound(first, 'resolved');

    const actionEvent = admit(journal, 'action-pending').event.id as string;
    journal.startRun({ runId: 'action-run', eventId: actionEvent });
    journal.recordActionIntent({ actionId: 'pending-anchor-action', runId: 'action-run', kind: 'send', payloadHash: 'hash' });
    journal.beginAction('pending-anchor-action');
    journal.settleAction('pending-anchor-action', 'succeeded', { provider: 'accepted' });
    journal.completeRun('action-run');

    const cutoff = Date.now() + 10_000;
    assert.deepEqual(journal.pruneTerminal(cutoff, cutoff),
      { replies: 0, dispatches: 0, actions: 0, runs: 0, events: 0 });
    assert.equal(journal.getDispatch('covered-dispatch')?.provider_message_id, 'provider-id');
    assert.equal(journal.getAction('pending-anchor-action')?.status, 'succeeded');
    assert.equal(journal.getInbound(second)?.outcome, 'pending');

    journal.resolveInbound(second, 'resolved');
    journal.resolveInbound(actionEvent, 'resolved');
    assert.deepEqual(journal.pruneTerminal(cutoff, cutoff),
      { replies: 1, dispatches: 1, actions: 1, runs: 2, events: 3 });
  });
});

test('gallery receipt is retained until receiver acknowledgement exists', () => {
  withJournal(journal => {
    journal.prepareDispatch({ dispatchId: 'gallery-receipt', kind: 'gallery', destination: 'owner', payload: 'four cards' });
    journal.beginDispatch('gallery-receipt');
    journal.settleDispatch('gallery-receipt', 1, 'accepted', 'parent', ['parent', 'a', 'b', 'c', 'd']);
    assert.equal(journal.pruneTerminal(Date.now() + 10_000).dispatches, 0);
    assert.equal(journal.getDispatch('gallery-receipt')?.provider_message_id, 'parent');
  });
});

test('shared-project discovery preserves exact owner thread across ACK, restart and replay', () => {
  withJournal((journal, path) => {
    const {provider_thread_id: _old, ...base} = activationIdentity;
    const seed = {...base, provider_route_mode: 'shared_project' as const};
    const thread = 'imessage:any;-;+15555550100~shared';
    const candidate = {...base, provider_thread_id: thread};
    journal.configureActivationDiscovery(seed);
    const input = {eventId: 'shared-first', accountKey: base.account_id, lineKey: base.line_key,
      threadKey: thread, channel: 'photon', sender: base.owner_id, payload: '{"text":"owner"}', activation: candidate};
    const admitted = journal.admitInbound(input);
    assert.equal(admitted.admitted, true);
    assert.equal(journal.acknowledgeActivation(String(admitted.event.id)), true);
    assert.equal(journal.admitInbound(input).admitted, false);
    assert.throws(() => journal.admitInbound({...input, eventId: 'physical', threadKey: thread.replace('shared', '+15555550199'),
      activation: {...candidate, provider_thread_id: thread.replace('shared', '+15555550199')}}), /candidate/);
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      assert.deepEqual(reopened.configureActivationDiscovery(seed), candidate);
      assert.equal(reopened.isActivationAcknowledged(), true);
      assert.equal(reopened.admitInbound(input).admitted, false);
      assert.throws(() => reopened.configureActivationDiscovery({...base, expected_provider_line: '+15555550199'}), /seed/);
    } finally { reopened.close(); }
  });
});

function seedOwnerToolTurn(journal: OperationalJournal, index: number, threadKey: string, result: unknown, kind = 'owner_routine_tool') {
  journal.db.prepare("INSERT INTO inbound_events(id,source_id,account_key,line_key,thread_key,channel,sender,payload_hash,payload,received_at,outcome) VALUES(?,?,?,?,?,?,?,?,?,0,'resolved')")
    .run(`e${index}`, `e${index}`, 'tenant-a', 'line-1', threadKey, 'photon', '+15555550100', 'h'.repeat(64), '');
  journal.db.prepare("INSERT INTO runs(id,event_id,thread_key,generation,status,started_at,ended_at) VALUES(?,?,?,1,'completed',0,0)")
    .run(`r${index}`, `e${index}`, threadKey);
  journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'succeeded',?,0,0)")
    .run(`a${index}`, `r${index}`, kind, `p${index}`, `e${index}`, 'h'.repeat(64), 't'.repeat(64), JSON.stringify(result));
}

test('admission evaluates each proposal-kind action once; settled history is never re-read inside BEGIN IMMEDIATE', () => {
  withJournal((journal, path) => {
    const thread = admit(journal, 'scope-probe').event.thread_key as string;
    const settled = {args: {operation: 'apply', text: 'x'.repeat(2000)}, scope_hash: 'f'.repeat(64)};
    journal.db.exec('BEGIN');
    for (let index = 0; index < 500; index++) seedOwnerToolTurn(journal, index, thread, settled);
    seedOwnerToolTurn(journal, 9999, thread, {args: {operation: 'propose'}, scope_hash: 'f'.repeat(64)});
    journal.db.exec('COMMIT');
    const first = admit(journal, 'first-after').event;
    assert.equal(journal.db.prepare('SELECT COUNT(*) AS n FROM actions WHERE proposal_checked IS NULL').get()!.n, 0);
    // The one open proposal bound only to the first subsequent event (unchanged binding semantics).
    assert.equal(JSON.parse(journal.getAction('a9999')!.result_json as string).next_event_id, first.id);
    // A settled row is not parsed again: even unreadable legacy content cannot fail a later admission.
    journal.db.exec('DROP TRIGGER action_proposal_recheck');
    journal.db.prepare("UPDATE actions SET result_json='not json' WHERE id='a7'").run();
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      assert.equal(reopened.admitInbound({ eventId: 'second-after', accountKey: 'tenant-a', lineKey: 'line-1',
        threadKey: 'thread-1', channel: 'photon', sender: '+15555550100', payload: '{"text":"hello"}' }).admitted, true);
      assert.equal(JSON.parse(reopened.getAction('a9999')!.result_json as string).next_event_id, first.id);
      // A rewrite of result_json (for example a later settle) makes the row eligible again, exactly as before.
      reopened.db.prepare('UPDATE actions SET result_json=? WHERE id=?').run(JSON.stringify({args: {operation: 'propose'}}), 'a9999');
      const third = reopened.admitInbound({ eventId: 'third-after', accountKey: 'tenant-a', lineKey: 'line-1',
        threadKey: 'thread-1', channel: 'photon', sender: '+15555550100', payload: '{"text":"hello"}' }).event.id;
      assert.equal(JSON.parse(reopened.getAction('a9999')!.result_json as string).next_event_id, third);
      const plan = JSON.stringify(reopened.db.prepare(`EXPLAIN QUERY PLAN SELECT a.* FROM actions a JOIN runs r ON r.id=a.run_id
          WHERE a.proposal_checked IS NULL AND a.kind IN ('owner_routine_tool','owner_gmail_draft_tool','owner_calendar_tool','owner_preference_tool','owner_loop_tool','owner_contacts_tool','owner_web_read') AND r.thread_key=?`).all('x'));
      assert.match(plan, /action_proposal_unchecked/);
    } finally { reopened.close(); }
  });
});

test('retention compaction bounds owner-tool history and gallery content while every fence and receipt survives', () => {
  withJournal(journal => {
    const thread = admit(journal, 'scope-probe').event.thread_key as string;
    journal.resolveInbound(journal.listPendingInbound()[0]!.id as string, 'resolved');
    const big = {args: {operation: 'apply', text: 'private owner wording '.repeat(100)}, scope_hash: 'f'.repeat(64), receipt: {ok: true}};
    journal.db.exec('BEGIN');
    for (let index = 0; index < 50; index++) seedOwnerToolTurn(journal, index, thread, big);
    seedOwnerToolTurn(journal, 60, thread, {args: {operation: 'cleanup', proposal: {action_id: 'a59', event_id: 'e59'}, text: 'secret'}}, 'owner_loop_tool');
    journal.db.exec('COMMIT');
    journal.prepareDispatch({ dispatchId: 'gallery-receipt', kind: 'gallery', destination: 'owner', payload: '{"summary":"private brief","images":[1,2,3,4]}' });
    journal.beginDispatch('gallery-receipt');
    journal.settleDispatch('gallery-receipt', 1, 'accepted', 'parent', ['parent', 'a', 'b', 'c', 'd']);
    journal.prepareDispatch({ dispatchId: 'gallery-unknown', kind: 'gallery', destination: 'owner', payload: 'uncertain cards' });
    journal.beginDispatch('gallery-unknown');
    journal.settleDispatch('gallery-unknown', 1, 'unknown');
    const galleryHash = journal.getDispatch('gallery-receipt')!.payload_hash;
    const bytes = () => Number(journal.db.prepare("SELECT SUM(LENGTH(result_json)) AS n FROM actions WHERE kind IN ('owner_routine_tool','owner_loop_tool')").get()!.n);
    const before = bytes();
    const cutoff = Date.now() + 10_000;
    assert.deepEqual(journal.compactTerminal(cutoff, cutoff), { gallery: 1, actions: 51 });
    assert.ok(bytes() < before / 10, 'terminal owner-tool results are reduced to digests');
    assert.equal(journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind IN ('owner_routine_tool','owner_loop_tool')").get()!.n, 51);
    const fence = journal.getAction('a3')!;
    assert.equal(fence.status, 'succeeded'); assert.equal(fence.payload_hash, 'p3'); assert.equal(fence.source_event_id, 'e3');
    assert.doesNotMatch(String(fence.result_json), /private owner wording/);
    assert.match(JSON.parse(String(fence.result_json)).result_hash, /^[a-f0-9]{64}$/);
    assert.deepEqual(JSON.parse(String(journal.getAction('a60')!.result_json)).args, {operation: 'cleanup', proposal: {action_id: 'a59'}});
    assert.ok(journal.getInbound('e3'), 'the source event remains as the duplicate-delivery fence');
    // Gallery: receipt fields and duplicate fence intact; only the summary payload is gone. Unknown stays untouched.
    const receipt = journal.getDispatch('gallery-receipt')!;
    assert.equal(receipt.payload, ''); assert.equal(receipt.payload_hash, galleryHash); assert.equal(receipt.provider_message_id, 'parent');
    assert.equal(journal.getDispatch('gallery-unknown')!.payload, 'uncertain cards');
    assert.deepEqual(journal.compactTerminal(cutoff, cutoff), { gallery: 0, actions: 0 }, 'compaction is idempotent');
    // Pending owner input defers compaction of that thread.
    const pending = admit(journal, 'pending-owner').event.id as string;
    seedOwnerToolTurn(journal, 70, thread, big);
    assert.equal(journal.compactTerminal(cutoff, cutoff).actions, 0);
    journal.resolveInbound(pending, 'resolved');
    assert.equal(journal.compactTerminal(cutoff, cutoff).actions, 1);
    // A permanently unknown (parked) action keeps its own tool's chain readable; other tools still compact.
    seedOwnerToolTurn(journal, 80, thread, {args: {operation: 'propose_cleanup'}, preview: {review: 'kept'}}, 'owner_loop_tool');
    seedOwnerToolTurn(journal, 81, thread, big);
    seedOwnerToolTurn(journal, 82, thread, {args: {operation: 'cleanup', proposal: {action_id: 'a80'}}}, 'owner_loop_tool');
    journal.db.prepare("UPDATE actions SET status='unknown' WHERE id='a82'").run();
    assert.equal(journal.compactTerminal(cutoff, cutoff).actions, 1);
    assert.equal(JSON.parse(String(journal.getAction('a80')!.result_json)).preview.review, 'kept');
    assert.equal(JSON.parse(String(journal.getAction('a81')!.result_json)).compacted, 1);
    assert.equal(journal.getAction('a82')!.status, 'unknown');
  });
});

test('each stop targets the latest open request once, including replay after reopen', () => {
  withJournal((journal, path) => {
    const delayed = admit(journal, 'delayed-calendar').event.id as string;
    journal.startRun({runId: 'calendar-run', eventId: delayed});
    journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,'unknown','{}',?,?)")
      .run('calendar-action', 'calendar-run', 'owner_calendar_tool', 'a'.repeat(64), delayed, Date.now(), Date.now());
    journal.prepareReply({replyId: 'notice', runId: 'calendar-run', eventId: delayed,
      dispatchId: 'notice-dispatch', kind: 'interactive', destination: 'owner', payload: 'pending outcome', purpose: 'outcome_pending'});
    journal.beginDispatch('notice-dispatch');
    journal.settleDispatch('notice-dispatch', 1, 'accepted', 'notice-provider');
    const research = admit(journal, 'research').event.id as string;
    journal.startRun({runId: 'research-run', eventId: research});
    journal.prepareReply({replyId: 'research-reply', runId: 'research-run', eventId: research,
      dispatchId: 'research-dispatch', kind: 'interactive', destination: 'owner', payload: 'research answer'});
    journal.beginDispatch('research-dispatch');
    journal.settleDispatch('research-dispatch', 1, 'unknown');
    const firstStop = admit(journal, 'stop-one').event.id as string;
    journal.stopThrough(firstStop);
    assert.equal(journal.getInbound(research)?.outcome, 'cancelled');
    assert.equal(journal.getInbound(delayed)?.outcome, 'pending');
    journal.stopThrough(firstStop);
    assert.equal(journal.getInbound(delayed)?.outcome, 'pending', 'same-stop replay preserves the unrelated delayed source');
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      reopened.recoverInterrupted();
      reopened.stopThrough(firstStop);
      assert.equal(reopened.getInbound(delayed)?.outcome, 'pending', 'reopen retains the original stop decision');
      reopened.resolveInbound(firstStop, 'resolved');
      const secondStop = admit(reopened, 'stop-two').event.id as string;
      reopened.stopThrough(secondStop);
      assert.equal(reopened.getInbound(delayed)?.outcome, 'cancelled', 'a later stop closes the sole remaining open request');
      reopened.stopThrough(secondStop);
      assert.equal(reopened.getAction('calendar-action')?.status, 'unknown');
      assert.equal(reopened.getDispatch('notice-dispatch')?.status, 'accepted');
      assert.equal(reopened.getDispatch('research-dispatch')?.status, 'unknown');
      assert.equal(reopened.db.prepare('SELECT status FROM dispatch_attempts WHERE dispatch_id=? AND number=1').get('research-dispatch')?.status, 'unknown');
    } finally { reopened.close(); }
  });
});

test('legacy stop acknowledgement proves the stop was applied before the marker existed', () => {
  for (const status of ['accepted', 'unknown'] as const) withJournal((journal, path) => {
    const delayed = admit(journal, `delayed-${status}`).event.id as string;
    journal.startRun({runId: 'notice-run', eventId: delayed});
    journal.prepareReply({replyId: 'notice', runId: 'notice-run', eventId: delayed,
      dispatchId: 'notice-dispatch', kind: 'interactive', destination: 'owner', payload: 'pending outcome', purpose: 'outcome_pending'});
    journal.beginDispatch('notice-dispatch'); journal.settleDispatch('notice-dispatch', 1, 'accepted', 'notice-provider');
    const research = admit(journal, 'research').event.id as string;
    journal.resolveInbound(research, 'cancelled');
    const stop = admit(journal, 'stop').event.id as string;
    journal.startRun({runId: 'stop-run', eventId: stop});
    journal.prepareReply({replyId: 'stop-reply', runId: 'stop-run', eventId: stop,
      dispatchId: 'stop-dispatch', kind: 'interactive', destination: 'owner', payload: 'stopped', purpose: 'owner_control'});
    journal.beginDispatch('stop-dispatch'); journal.settleDispatch('stop-dispatch', 1, status, status === 'accepted' ? 'stop-provider' : undefined);
    // Simulate a schema-7 journal written before per-stop application bookkeeping.
    if (journal.db.prepare('PRAGMA table_info(inbound_events)').all().some(row => row.name === 'stop_applied'))
      journal.db.exec('ALTER TABLE inbound_events DROP COLUMN stop_applied');
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      reopened.stopThrough(stop);
      assert.equal(reopened.getInbound(delayed)?.outcome, 'pending');
      assert.equal(reopened.getDispatch('stop-dispatch')?.status, status);
      assert.equal(reopened.getInbound(stop)?.stop_applied, 1);
      reopened.resolveInbound(stop, 'resolved');
      const laterStop = admit(reopened, 'later-stop').event.id as string;
      reopened.stopThrough(laterStop);
      assert.equal(reopened.getInbound(delayed)?.outcome, 'cancelled');
      assert.equal(reopened.getDispatch('notice-dispatch')?.status, 'accepted');
    } finally { reopened.close(); }
  });
});

test('stop application bookkeeping rolls back with cancellation and terminal retention removes it', () => {
  withJournal(journal => {
    const target = admit(journal, 'target').event.id as string;
    const stop = admit(journal, 'stop').event.id as string;
    journal.db.exec("CREATE TRIGGER fail_stop BEFORE UPDATE OF outcome ON inbound_events WHEN NEW.outcome='cancelled' BEGIN SELECT RAISE(ABORT,'fixture failure'); END");
    assert.throws(() => journal.stopThrough(stop), /fixture failure/);
    assert.equal(journal.getInbound(target)?.outcome, 'pending');
    assert.equal(journal.getInbound(stop)?.stop_applied, null);
    journal.db.exec('DROP TRIGGER fail_stop');
    journal.stopThrough(stop);
    assert.equal(journal.getInbound(target)?.outcome, 'cancelled');
    assert.equal(journal.getInbound(stop)?.stop_applied, 1);
    journal.resolveInbound(stop, 'resolved');
    assert.equal(journal.pruneTerminal(Date.now() + 10_000).events, 2);
    assert.equal(journal.getInbound(stop), undefined);
  });
});
