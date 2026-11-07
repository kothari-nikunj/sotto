import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ConversationCoordinator } from '../src/conversation.js';
import { OperationalJournal } from '../src/journal/index.js';
import type { Inbound } from '../src/channels/types.js';

const policy = { channel: 'telegram' as const, ownerId: '42', accountId: 'fixture-bot' };
const context = { threadId: 'telegram:42', lineKey: 'primary', destination: 'owner' };
const png = Uint8Array.from([137, 80, 78, 71, 13, 10, 26, 10]);
const input = (id: string): Inbound => ({ channel: 'telegram', eventId: id, accountId: policy.accountId,
  senderId: policy.ownerId, isDirect: true, media: [{ mimeType: 'image/png', filename: 'owner.png',
    source: { version: 1, provider: 'telegram', fileId: 'provider-file-id' } }] });
function deferred<T>() {
  let resolve!: (value: T) => void;
  return { promise: new Promise<T>(done => { resolve = done; }), get resolve() { return resolve; } };
}

async function withJournal(work: (journal: OperationalJournal, path: string) => Promise<void>) {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-media-admission-'));
  const path = join(directory, 'operations.sqlite');
  const journal = new OperationalJournal(path);
  try { await work(journal, path); }
  finally { try { journal.close(); } catch { /* a test explicitly reopens it */ } rmSync(directory, { recursive: true, force: true }); }
}

test('provider hydration follows durable owner admission and leaves only descriptors in the journal', async () => {
  await withJournal(async journal => {
    let reads = 0; let models = 0; let sends = 0;
    const service = new ConversationCoordinator({ journal, ownerPolicy: policy,
      hydrateInbound: async (messages, guard) => {
        reads++;
        const persisted = journal.listPendingInbound();
        assert.equal(persisted.length, 1);
        assert.equal(guard.isCurrent(), true);
        assert.equal(guard.signal.aborted, false);
        assert.equal(JSON.parse(String(persisted[0]!.payload)).inbound.media[0].source.fileId, 'provider-file-id');
        return messages.map(message => ({ ...message, media: message.media!.map(item => ({ ...item, data: png })) }));
      },
      model: { generate: async request => {
        models++;
        assert.equal(request.text, '[attachment received]');
        assert.deepEqual(request.messages[0]!.media![0]!.data, png);
        assert.equal(String(journal.listPendingInbound()[0]!.payload).includes('__sotto_bytes'), false);
        return '';
      } },
      postPrepared: async () => { sends++; return { acceptance: 'accepted', messageId: 'unused' }; },
    });
    try {
      assert.equal((await service.receive({ ...input('denied'), senderId: 'stranger' }, context)).status, 'denied');
      assert.equal(reads, 0);
      assert.equal((await service.receive(input('owner-image'), context)).status, 'silent');
      assert.equal((await service.receive(input('owner-image'), context)).status, 'duplicate');
      assert.deepEqual({ reads, models, sends }, { reads: 1, models: 1, sends: 0 });
      const saved = journal.db.prepare('SELECT payload FROM inbound_events').get()!;
      assert.equal('data' in JSON.parse(String(saved.payload)).inbound.media[0], false);
    } finally { await service.close(); }
  });
});

test('/stop during an uncooperative media read fences its late bytes before any model call', async () => {
  await withJournal(async journal => {
    const started = deferred<{ signal: AbortSignal; isCurrent(): boolean }>();
    const finish = deferred<void>();
    let models = 0; const sent: string[] = [];
    const service = new ConversationCoordinator({ journal, ownerPolicy: policy,
      hydrateInbound: async (messages, guard) => {
        started.resolve(guard);
        await finish.promise; // model boundary must recheck even if a read ignored cancellation
        return messages.map(message => ({ ...message, media: [{ mimeType: 'image/png', data: png }] }));
      },
      model: { generate: async () => { models++; return 'stale answer'; } },
      postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: 'stop-ack' }; },
    });
    try {
      const turn = service.receive(input('slow-image'), context);
      const guard = await started.promise;
      assert.equal((await service.receive({ ...input('stop'), text: '/stop', media: [] }, context)).status, 'stopped');
      assert.equal(guard.signal.aborted, true);
      assert.equal(guard.isCurrent(), false);
      finish.resolve();
      assert.equal((await turn).status, 'cancelled');
      assert.equal(models, 0);
      assert.equal(journal.db.prepare("SELECT outcome FROM inbound_events WHERE source_id='slow-image'").get()!.outcome, 'cancelled');
      await service.drain();
      assert.deepEqual(sent, ['I stopped working on your earlier request. An action already sent may still have happened; please check before retrying.']);
      assert.equal(journal.listPendingInbound().length, 0);
    } finally { finish.resolve(); await service.close(); }
  });
});

test('restart hydrates exact admitted descriptors but an uncertain prepared reply never downloads or regenerates', async () => {
  await withJournal(async (journal, path) => {
    journal.admitInbound({ eventId: 'persisted-image', accountKey: policy.accountId, lineKey: context.lineKey,
      threadKey: context.threadId, channel: policy.channel, sender: policy.ownerId,
      payload: JSON.stringify({ inbound: input('persisted-image'), context }) });
    journal.close();
    const reopened = new OperationalJournal(path);
    reopened.recoverInterrupted();
    let reads = 0; let models = 0; let sends = 0;
    const service = new ConversationCoordinator({ journal: reopened, ownerPolicy: policy,
      hydrateInbound: async messages => {
        reads++;
        assert.deepEqual(messages, [input('persisted-image')]);
        return messages.map(message => ({ ...message, media: [{ mimeType: 'image/png', data: png }] }));
      },
      model: { generate: async () => { models++; return 'prepared description'; } },
      postPrepared: async () => { sends++; return { acceptance: 'unknown' }; },
    });
    try { assert.deepEqual(await service.recover(), [{ status: 'held' }]); }
    finally { await service.close(); reopened.close(); }
    const again = new OperationalJournal(path);
    again.recoverInterrupted();
    const recovery = new ConversationCoordinator({ journal: again, ownerPolicy: policy,
      hydrateInbound: async () => { throw new Error('must not rehydrate a contacted reply'); },
      model: { generate: async () => { throw new Error('must not regenerate a contacted reply'); } },
      postPrepared: async () => { throw new Error('must not replay a contacted reply'); },
    });
    try {
      assert.deepEqual(await recovery.recover(), [{ status: 'held' }]);
      assert.deepEqual({ reads, models, sends }, { reads: 1, models: 1, sends: 1 });
    } finally { await recovery.close(); again.close(); }
  });
});
