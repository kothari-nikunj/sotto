import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { ConversationCoordinator, type ModelRequest, type PreparedPost } from '../src/conversation.js';
import type { Inbound, OwnerPolicy } from '../src/channels/types.js';
import { OperationalJournal } from '../src/journal/index.js';
import { createTelegramTransport } from '../src/channels/transport.js';
import type { OwnerLoopClient } from '../src/tools/owner-loop-client.js';
import type { OwnerGmailDraftClient } from '../src/tools/owner-gmail-draft-client.js';
import { MockLanguageModelV4 } from 'ai/test';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import type { SourceAuthority } from '../src/agent/source-authority.js';

const ownerPolicy: OwnerPolicy = { channel: 'photon', accountId: 'account', ownerId: 'owner' };
const context = { threadId: 'thread', destination: 'owner-destination', lineKey: 'line' };
function inbound(eventId: string, text: string, change: Partial<Inbound> = {}): Inbound {
  return { channel: 'photon', accountId: 'account', senderId: 'owner', isDirect: true, eventId, text, ...change };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
async function withDb(work: (journal: OperationalJournal, path: string) => Promise<void>): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), 'sotto-conversation-'));
  const path = join(dir, 'journal.sqlite3');
  const journal = new OperationalJournal(path);
  try { await work(journal, path); }
  finally { try { journal.close(); } catch { /* reopened in test */ } rmSync(dir, { recursive: true, force: true }); }
}
function coordinator(journal: OperationalJournal, model: (request: ModelRequest) => Promise<string>,
  post: (request: PreparedPost) => Promise<{ acceptance: 'accepted'; messageId: string }> = async () => ({ acceptance: 'accepted', messageId: 'posted' })) {
  return new ConversationCoordinator({ journal, ownerPolicy, model: { generate: model }, postPrepared: post });
}
function admitForRecovery(journal: OperationalJournal, id: string, text: string): string {
  const saved = journal.admitInbound({ eventId: id, accountKey: 'account', lineKey: 'line', threadKey: 'thread',
    channel: 'photon', sender: 'owner', payload: JSON.stringify({ inbound: inbound(id, text), context }) });
  return saved.event.id as string;
}
function admittedId(journal: OperationalJournal, sourceId: string): string {
  return (journal.db.prepare('SELECT id FROM inbound_events WHERE source_id=?').get(sourceId) as {id: string}).id;
}

test('owner direct-message admission precedes persistence and activation; duplicate is inert', async () => {
  await withDb(async journal => {
    let models = 0;
    let sends = 0;
    const service = coordinator(journal, async () => { models++; return 'reply'; }, async () => {
      sends++; return { acceptance: 'accepted', messageId: 'provider-1' };
    });
    assert.equal((await service.receive(inbound('bad-sender', 'x', { senderId: 'stranger' }), context)).status, 'denied');
    assert.equal((await service.receive(inbound('group', 'x', { isDirect: false }), context)).status, 'denied');
    assert.equal(journal.listPendingInbound().length, 0);
    assert.deepEqual(await service.receive(inbound('event', 'hello'), context), { status: 'accepted', messageId: 'provider-1' });
    assert.equal((await service.receive(inbound('event', 'hello'), context)).status, 'duplicate');
    assert.equal(models, 1);
    assert.equal(sends, 1);
    assert.equal(journal.listPendingInbound().length, 0);
  });
});

test('/stop fences a model immediately and dispatches only its fixed acknowledgement', async () => {
  await withDb(async journal => {
    const started = deferred<ModelRequest>();
    const finish = deferred<string>();
    let sends = 0;
    const service = coordinator(journal, request => { started.resolve(request); return finish.promise; }, async () => {
      sends++; return { acceptance: 'accepted', messageId: 'unexpected' };
    });
    const first = service.receive(inbound('one', 'draft an answer'), context);
    const request = await started.promise;
    assert.equal((await service.receive(inbound('stop', '/stop'), context)).status, 'stopped');
    assert.equal(request.signal.aborted, true);
    finish.resolve('too late');
    assert.equal((await first).status, 'cancelled');
    await service.recover();
    assert.equal(sends, 1);
    const replies = journal.listRepliesForThread(admittedId(journal, 'stop'));
    assert.deepEqual(replies.map(row => [row.purpose, row.payload, row.dispatch_status]),
      [['owner_control', 'I stopped working on your earlier request. An action already sent may still have happened; please check before retrying.', 'accepted']]);
    assert.equal(journal.listPendingInbound().length, 0);
  });
});

test('/stop cancels input processing while unknown action proof holds later work for reconciliation', async () => {
  await withDb(async journal => {
    const event = admitForRecovery(journal, 'original', 'perform the original action');
    journal.startRun({ runId: 'run', eventId: event });
    journal.recordActionIntent({ actionId: 'effect', runId: 'run', kind: 'send', payloadHash: 'h' });
    journal.beginAction('effect');
    journal.recoverInterrupted();
    const texts: string[] = [];
    const service = coordinator(journal, async request => { texts.push(request.text); return 'new answer'; });
    assert.equal((await service.receive(inbound('stop', '/stop'), context)).status, 'stopped');
    assert.equal(journal.getInbound(event)?.outcome, 'cancelled');
    assert.equal(journal.getAction('effect')?.status, 'unknown');
    await service.recover();
    assert.equal(journal.getInbound(admittedId(journal, 'stop'))?.outcome, 'resolved');
    assert.equal((await service.receive(inbound('later', 'a later request'), context)).status, 'held');
    assert.deepEqual(texts, []);
    journal.settleAction('effect', 'succeeded', { providerId: 'proof' });
    await service.recover();
    assert.deepEqual(texts, ['a later request']);
    assert.equal(journal.getAction('effect')?.status, 'succeeded');
  });
});

test('/stop during reply contact preserves unknown proof without replaying cancelled input', async () => {
  await withDb(async journal => {
    const contacted = deferred<void>();
    const outcome = deferred<{ acceptance: 'unknown' }>();
    let modelCalls = 0;
    let sends = 0;
    const service = new ConversationCoordinator({ journal, ownerPolicy,
      model: { generate: async () => { modelCalls++; return 'prepared answer'; } },
      postPrepared: async () => { sends++; if (sends === 1) { contacted.resolve(); return outcome.promise; }
        return {acceptance: 'accepted', messageId: 'stop-ack'}; },
    });
    const first = service.receive(inbound('one', 'first request'), context);
    await contacted.promise;
    await service.receive(inbound('stop', '/stop'), context);
    outcome.resolve({ acceptance: 'unknown' });
    assert.equal((await first).status, 'held');
    await service.recover();
    assert.equal(journal.listPendingInbound().length, 0);
    const [proof] = journal.listRecoverableDispatches();
    assert.equal(proof?.status, 'unknown');
    await service.recover();
    assert.equal(modelCalls, 1);
    assert.equal(sends, 2, 'original contacted reply plus one stop acknowledgement');
    journal.settleDispatch(proof!.id as string, 1, 'accepted', 'late-provider-proof');
    await service.recover();
    assert.equal(sends, 2);
  });
});

test('/stop during Telegram chunk one prevents later chunks without revoking contacted work', async () => {
  await withDb(async journal => {
    const contacted = deferred<void>();
    const late = deferred<{id: string}>();
    const posts: string[] = [];
    const longAnswer = 'x'.repeat(9000);
    const transport = createTelegramTransport({postMessage: async (_thread, message) => {
      const text = String((message as {raw: string}).raw);
      posts.push(text);
      if (text === longAnswer.slice(0, 4096)) { contacted.resolve(); return await late.promise as never; }
      return {id: `message-${posts.length}`} as never;
    }});
    const policy: OwnerPolicy = {channel: 'telegram', accountId: 'account', ownerId: '42'};
    const receive = (eventId: string, text: string) => service.receive(
      {channel: 'telegram', accountId: 'account', senderId: '42', isDirect: true, eventId, text},
      {threadId: 'telegram:42', destination: 'telegram', lineKey: 'line'});
    const service = new ConversationCoordinator({journal, ownerPolicy: policy,
      model: {generate: async () => longAnswer},
      postPrepared: async request => {
        const receipt = await transport.send({threadId: 'telegram:42', text: request.text, signal: request.signal});
        return {acceptance: receipt.acceptance, messageId: receipt.providerMessageIds[0], messageIds: receipt.providerMessageIds};
      }});
    try {
      const first = receive('chunked-answer', 'Give me a detailed answer');
      await contacted.promise;
      assert.equal((await receive('stop-chunks', '/stop')).status, 'stopped');
      assert.equal((await first).status, 'held');
      late.resolve({id: 'late-first-chunk'});
      await new Promise<void>(resolve => setImmediate(resolve));
      await service.recover();
      assert.equal(posts.filter(text => text.startsWith('xxx')).length, 1);
      assert.equal(posts.length, 2, 'only contacted first chunk and separate stop acknowledgement');
      const dispatch = journal.db.prepare("SELECT * FROM dispatches WHERE status='unknown'").get()!;
      assert.ok(dispatch);
      assert.equal(dispatch.provider_message_id, null);
      assert.equal(journal.getAttempts(String(dispatch.id)).length, 1);
    } finally { await service.close(); }
  });
});

test('/stop releases a hung contacted reply, acknowledges once, and resumes safe chat without late rewrite', async () => {
  await withDb(async journal => {
    const contacted = deferred<void>();
    const late = deferred<{acceptance: 'accepted'; messageId: string}>();
    const sends: string[] = [];
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async request => request.text === 'first request' ? 'stale answer' : 'safe answer'},
      postPrepared: async request => {
        sends.push(request.text);
        if (request.text === 'stale answer') {contacted.resolve(); return late.promise;}
        return {acceptance: 'accepted', messageId: `provider-${sends.length}`};
      },
    });
    const first = service.receive(inbound('old', 'first request'), context);
    await contacted.promise;
    assert.equal((await service.receive(inbound('stop', '/stop'), context)).status, 'stopped');
    assert.equal((await first).status, 'held');
    assert.equal((await service.receive(inbound('safe', 'Hello'), context)).status, 'accepted');
    await service.drain();
    assert.deepEqual(sends, ['stale answer',
      'I stopped working on your earlier request. An action already sent may still have happened; please check before retrying.',
      'safe answer']);
    const old = admittedId(journal, 'old');
    const oldReply = journal.listRepliesForThread(old).find(row => row.event_id === old);
    assert.equal(journal.getDispatch(oldReply!.dispatch_id as string)?.status, 'unknown');
    assert.deepEqual(journal.getAttempts(oldReply!.dispatch_id as string).map(row => row.status), ['unknown']);
    assert.equal(journal.getInbound(old)?.outcome, 'cancelled');
    assert.equal(journal.getInbound(admittedId(journal, 'stop'))?.outcome, 'resolved');
    assert.equal(journal.getInbound(admittedId(journal, 'safe'))?.outcome, 'resolved');
    late.resolve({acceptance: 'accepted', messageId: 'late-stale-provider-id'});
    await new Promise<void>(resolve => setImmediate(resolve));
    assert.equal(journal.getDispatch(oldReply!.dispatch_id as string)?.status, 'unknown');
    assert.equal(sends.length, 3);
  });
});

for (const origin of ['live', 'recovery'] as const) test(`/stop releases a hung ${origin} outcome notice without replaying its contacted send`, async () => {
  await withDb(async journal => {
    const source = admitForRecovery(journal, 'uncertain-calendar', 'Update my calendar');
    journal.startRun({runId: 'effect-run', eventId: source});
    journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,'unknown',?,?,?)")
      .run('calendar-effect', 'effect-run', 'owner_calendar_tool', 'a'.repeat(64), source, '{}', Date.now(), Date.now());
    const contacted = deferred<void>();
    const late = deferred<{acceptance: 'accepted'; messageId: string}>();
    const posts: string[] = [];
    const service = new ConversationCoordinator({journal, ownerPolicy,
      ownerCalendar: {identity: {line_key: 'line'}, scopeHash: 'a'.repeat(64),
        call: async () => {throw Error('receipt unavailable');}} as never,
      model: {generate: async request => `answer: ${request.text}`},
      postPrepared: async request => {
        posts.push(request.text);
        if (request.text.startsWith('I couldn’t confirm whether')) {contacted.resolve(); return late.promise;}
        return {acceptance: 'accepted', messageId: `m${posts.length}`};
      },
    });
    const initial = origin === 'live'
      ? service.receive(inbound('safe-before', 'Hello before'), context)
      : service.recover();
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      await contacted.promise;
      const notice = journal.listRepliesForThread(source).find(row => row.purpose === 'outcome_pending')!;
      assert.equal(journal.getDispatch(notice.dispatch_id as string)?.status, 'dispatching');
      // A stop in another thread must not release this contacted notice.
      await service.receive(inbound('other-stop', '/stop'), {...context, threadId: 'other-thread'});
      await new Promise<void>(resolve => setImmediate(resolve));
      assert.equal(journal.getDispatch(notice.dispatch_id as string)?.status, 'dispatching');
      assert.equal((await service.receive(inbound('stop-notice', '/stop'), context)).status, 'stopped');
      const safe = service.receive(inbound('safe-after', 'Hello after'), context);
      const completed = await Promise.race([
        Promise.all([initial, safe, service.drain()]).then(() => true),
        new Promise<false>(resolve => {timeout = setTimeout(() => resolve(false), 1000);}),
      ]);
      assert.equal(completed, true, 'the same-thread stop must release the notice without its provider receipt');
      assert.equal(journal.getDispatch(notice.dispatch_id as string)?.status, 'unknown');
      assert.deepEqual(journal.getAttempts(notice.dispatch_id as string).map(row => row.status), ['unknown']);
      assert.equal(journal.getAction('calendar-effect')?.status, 'unknown');
      assert.equal(journal.getInbound(source)?.outcome, 'cancelled');
      assert.equal(journal.getInbound(admittedId(journal, 'stop-notice'))?.outcome, 'resolved');
      assert.equal(journal.getInbound(admittedId(journal, 'safe-after'))?.outcome, 'resolved');
      assert.equal(posts.filter(text => text.startsWith('I couldn’t confirm whether')).length, 1);
      assert.equal(posts.filter(text => text.startsWith('I stopped working')).length, 2);
      assert.equal(posts.at(-1), 'answer: Hello after');
      late.resolve({acceptance: 'accepted', messageId: 'late-notice'});
      await new Promise<void>(resolve => setImmediate(resolve));
      await service.recover();
      assert.equal(journal.getDispatch(notice.dispatch_id as string)?.status, 'unknown');
      assert.equal(posts.filter(text => text.startsWith('I couldn’t confirm whether')).length, 1);
    } finally {
      if (timeout) clearTimeout(timeout);
      late.resolve({acceptance: 'accepted', messageId: 'late-notice'});
      await initial;
      await service.drain();
      await service.close();
    }
  });
});

test('recovery applies a durably admitted stop before model work and preserves later arrivals', async () => {
  await withDb(async journal => {
    const first = admitForRecovery(journal, 'before', 'cancel this old request');
    journal.startRun({ runId: 'old-run', eventId: first });
    journal.prepareReply({ replyId: 'reply', runId: 'old-run', eventId: first, dispatchId: 'dispatch',
      kind: 'interactive', destination: context.destination, payload: 'old prepared reply' });
    admitForRecovery(journal, 'stop', '/stop');
    admitForRecovery(journal, 'after', 'keep this later request');
    // Arrival order must survive equal timestamps or a wall-clock adjustment.
    journal.db.prepare('UPDATE inbound_events SET received_at=0').run();
    const texts: string[] = [];
    const service = coordinator(journal, async request => { texts.push(request.text); return 'later reply'; });
    await service.recover();
    assert.deepEqual(texts, ['keep this later request']);
    assert.equal(journal.getDispatch('dispatch')?.status, 'cancelled');
    assert.equal(journal.getInbound(first)?.outcome, 'cancelled');
    assert.equal(journal.listPendingInbound().length, 0);
  });
});

test('prepared stop acknowledgement posts once after restart while the cancelled turn stays cancelled', async () => {
  await withDb(async (journal, path) => {
    const old = admitForRecovery(journal, 'old', 'cancel this');
    const stop = admitForRecovery(journal, 'stop', '/stop');
    journal.stopThrough(stop);
    journal.startRun({runId: 'stop-run', eventId: stop});
    journal.prepareReply({replyId: 'stop-reply', runId: 'stop-run', eventId: stop,
      dispatchId: 'stop-dispatch', kind: 'interactive', destination: context.destination,
      payload: 'I stopped working on your earlier request. An action already sent may still have happened; please check before retrying.',
      sourceRevision: 'owner-only-context-v1', purpose: 'owner_control'});
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      let sends = 0; let models = 0;
      const service = coordinator(reopened, async () => {models++; return 'wrong';}, async () => {
        sends++; return {acceptance: 'accepted', messageId: 'stop-provider-id'};
      });
      await service.recover(); await service.recover();
      assert.equal(models, 0);
      assert.equal(sends, 1);
      assert.equal(reopened.getInbound(old)?.outcome, 'cancelled');
      assert.equal(reopened.getInbound(stop)?.outcome, 'resolved');
      assert.equal(reopened.getDispatch('stop-dispatch')?.status, 'accepted');
    } finally {reopened.close();}
  });
});

test('contacted unknown stop acknowledgement stays held and does not retry after restart', async () => {
  await withDb(async (journal, path) => {
    const stop = admitForRecovery(journal, 'stop', '/stop');
    journal.startRun({runId: 'stop-run', eventId: stop});
    journal.prepareReply({replyId: 'stop-reply', runId: 'stop-run', eventId: stop,
      dispatchId: 'stop-dispatch', kind: 'interactive', destination: context.destination,
      payload: 'I stopped working on your earlier request. An action already sent may still have happened; please check before retrying.',
      sourceRevision: 'owner-only-context-v1', purpose: 'owner_control'});
    journal.beginDispatch('stop-dispatch'); journal.close();
    const reopened = new OperationalJournal(path);
    try {
      reopened.recoverInterrupted(); let sends = 0; let models = 0;
      const service = coordinator(reopened, async () => {models++; return 'safe answer';}, async () => {
        sends++; return {acceptance: 'accepted', messageId: 'wrong'};
      });
      await service.recover(); await service.recover();
      assert.equal(sends, 0);
      assert.equal(reopened.getInbound(stop)?.outcome, 'pending');
      assert.equal(reopened.getDispatch('stop-dispatch')?.status, 'unknown');
      assert.equal((await service.receive(inbound('safe-after-stop', 'Hello'), context)).status, 'accepted');
      assert.equal(models, 1); assert.equal(sends, 1);
      assert.equal(reopened.getInbound(stop)?.outcome, 'pending');
      assert.equal(reopened.getDispatch('stop-dispatch')?.status, 'unknown');
    } finally {reopened.close();}
  });
});

test('hung contacted stop acknowledgement becomes unknown and does not block later safe chat', async () => {
  await withDb(async journal => {
    const contacted = deferred<void>();
    const late = deferred<{acceptance: 'accepted'; messageId: string}>();
    let sends = 0; let models = 0;
    const service = new ConversationCoordinator({journal, ownerPolicy, controlDeliveryDeadlineMs: 20,
      model: {generate: async () => {models++; return 'safe answer';}},
      postPrepared: async request => {
        sends++;
        if (request.text.startsWith('I stopped working')) {
          contacted.resolve(); return late.promise;
        }
        return {acceptance: 'accepted', messageId: 'safe-provider-id'};
      },
    });
    assert.equal((await service.receive(inbound('stop', '/stop'), context)).status, 'stopped');
    await contacted.promise;
    const later = await service.receive(inbound('later', 'Hello'), context);
    assert.equal(later.status, 'accepted');
    const stop = admittedId(journal, 'stop');
    const reply = journal.listRepliesForThread(stop).find(row => row.purpose === 'owner_control');
    assert.equal(journal.getDispatch(reply!.dispatch_id as string)?.status, 'unknown');
    assert.equal(journal.getInbound(stop)?.outcome, 'pending');
    assert.equal(models, 1); assert.equal(sends, 2);
    await service.recover();
    assert.equal(sends, 2, 'a timed out contacted acknowledgement is never resent');
    late.resolve({acceptance: 'accepted', messageId: 'late-stop-provider-id'});
    await new Promise<void>(resolve => setImmediate(resolve));
    assert.equal(journal.getDispatch(reply!.dispatch_id as string)?.status, 'unknown');
  });
});

test('correction aborts the old generation, waits it out, and includes all pending owner messages', async () => {
  await withDb(async journal => {
    const firstStarted = deferred<ModelRequest>();
    const finishFirst = deferred<string>();
    const requests: ModelRequest[] = [];
    const sent: string[] = [];
    const service = coordinator(journal, request => {
      requests.push(request);
      if (requests.length === 1) { firstStarted.resolve(request); return finishFirst.promise; }
      return Promise.resolve('corrected reply');
    }, async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: 'provider-2' }; });
    const first = service.receive(inbound('first', 'Find the draft'), context);
    const firstRequest = await firstStarted.promise;
    const correction = service.receive(inbound('second', 'Actually, use the latest draft'), context);
    assert.equal(firstRequest.signal.aborted, true);
    finishFirst.resolve('stale reply');
    assert.equal((await first).status, 'cancelled');
    assert.deepEqual(await correction, { status: 'accepted', messageId: 'provider-2' });
    assert.equal(requests.length, 2);
    assert.equal(requests[1]!.text, 'Find the draft\nActually, use the latest draft');
    assert.deepEqual(requests[1]!.messages.map(message => message.eventId), ['first', 'second']);
    assert.deepEqual(sent, ['corrected reply']);
    assert.equal(journal.listPendingInbound().length, 0);
  });
});

test('prepared reply survives reopen and posts original payload without model regeneration', async () => {
  await withDb(async (journal, path) => {
    const eventId = admitForRecovery(journal, 'event', 'owner question');
    journal.startRun({ runId: 'run', eventId });
    journal.prepareReply({ replyId: 'reply', runId: 'run', eventId, dispatchId: 'dispatch',
      kind: 'interactive', destination: context.destination, payload: 'durable original' });
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      reopened.recoverInterrupted();
      let models = 0;
      const sent: PreparedPost[] = [];
      let presence = 0;
      const service = new ConversationCoordinator({ journal: reopened, ownerPolicy,
        model: { generate: async () => { models++; return 'regenerated'; } },
        postPrepared: async request => { sent.push(request); return { acceptance: 'accepted', messageId: 'provider-3' }; },
        startTurnPresence: () => { presence++; return () => {}; },
      });
      assert.equal((await service.recover())[0]?.status, 'accepted');
      assert.equal(models, 0);
      assert.equal(presence, 0, 'prepared-reply recovery never starts a new model-turn presence');
      assert.deepEqual(sent.map(item => item.text), ['durable original']);
      assert.equal(reopened.getInbound(eventId)?.outcome, 'resolved');
      assert.equal(reopened.getRun('run')?.status, 'completed');
    } finally { reopened.close(); }
  });
});

test('presence starts after durable run admission, excludes denied and duplicate input, and ends on /stop', async () => {
  await withDb(async journal => {
    const started = deferred<ModelRequest>();
    const finish = deferred<string>();
    const presence: string[] = [];
    const service = new ConversationCoordinator({ journal, ownerPolicy,
      model: { generate: request => { started.resolve(request); return finish.promise; } },
      postPrepared: async () => ({ acceptance: 'accepted', messageId: 'provider' }),
      startTurnPresence: (_thread, runId, signal, isCurrent) => {
        assert.equal(journal.getRun(runId)?.status, 'running');
        assert.equal(signal.aborted, false);
        assert.equal(isCurrent(), true);
        presence.push('start');
        return () => presence.push('stop');
      },
    });
    assert.equal((await service.receive(inbound('foreign', 'hello', { senderId: 'stranger' }), context)).status, 'denied');
    const first = service.receive(inbound('one', 'hello'), context);
    await started.promise;
    assert.equal((await service.receive(inbound('one', 'hello'), context)).status, 'duplicate');
    assert.deepEqual(presence, ['start']);
    assert.equal((await service.receive(inbound('stop', '/stop'), context)).status, 'stopped');
    finish.resolve('late');
    assert.equal((await first).status, 'cancelled');
    assert.deepEqual(presence, ['start', 'stop']);
  });
});

test('failed durable startRun cannot start presence or the model', async () => {
  await withDb(async journal => {
    let presence = 0;
    let models = 0;
    const startRun = journal.startRun.bind(journal);
    journal.startRun = () => { throw new Error('injected journal failure'); };
    const service = new ConversationCoordinator({ journal, ownerPolicy,
      model: { generate: async () => { models++; return 'reply'; } },
      postPrepared: async () => ({ acceptance: 'accepted', messageId: 'provider' }),
      startTurnPresence: () => { presence++; return () => {}; },
    });
    await assert.rejects(service.receive(inbound('run-fails', 'hello'), context), /journal failure/);
    assert.equal(presence, 0);
    assert.equal(models, 0);
    journal.startRun = startRun;
  });
});

test('generated source revision is persisted; recovery checks current proof before any provider contact', async () => {
  await withDb(async (journal, path) => {
    const eventId = admitForRecovery(journal, 'source-event', 'summarize current sources');
    let generated = 0;
    let sends = 0;
    let authority = 'revision-old';
    const first = new ConversationCoordinator({ journal, ownerPolicy,
      model: { generate: async () => { generated++; return { text: 'source-derived answer', sourceRevision: 'revision-current' }; } },
      canPostPrepared: async revision => revision === authority,
      postPrepared: async () => { sends++; return { acceptance: 'accepted', messageId: 'proofed-send' }; },
    });
    assert.equal((await first.recover())[0]?.status, 'held');
    const [prepared] = journal.listRecoverableReplies();
    assert.equal(prepared?.source_revision, 'revision-current');
    assert.equal(generated, 1, 'the initial recovery prepares the reply exactly once');
    assert.equal(sends, 0);
    journal.close();

    const reopened = new OperationalJournal(path);
    try {
      reopened.recoverInterrupted();
      const recovery = new ConversationCoordinator({ journal: reopened, ownerPolicy,
        model: { generate: async () => { generated++; return 'must not regenerate'; } },
        canPostPrepared: async revision => revision === authority,
        postPrepared: async () => { sends++; return { acceptance: 'accepted', messageId: 'proofed-send' }; },
      });
      assert.equal((await recovery.recover())[0]?.status, 'held');
      assert.equal(sends, 0, 'mismatched source proof must block before beginDispatch/provider call');
      authority = 'revision-current';
      assert.equal((await recovery.recover())[0]?.status, 'accepted');
      assert.equal(generated, 1, 'restart recovery reuses the prepared reply without rerunning the model');
      assert.equal(sends, 1);
      assert.equal(reopened.getInbound(eventId)?.outcome, 'resolved');
    } finally { reopened.close(); }
  });
});

test('/stop cancels a prepared source-derived reply before provider contact', async () => {
  await withDb(async journal => {
    let sends = 0;
    const service = new ConversationCoordinator({ journal, ownerPolicy,
      model: { generate: async () => ({ text: 'held source answer', sourceRevision: 'revision-current' }) },
      canPostPrepared: async () => false,
      postPrepared: async () => { sends++; return { acceptance: 'accepted', messageId: 'must-not-send' }; },
    });
    assert.equal((await service.receive(inbound('held-input', 'answer with current sources'), context)).status, 'held');
    const [prepared] = journal.listRecoverableReplies();
    assert.equal(prepared?.source_revision, 'revision-current');
    assert.equal((await service.receive(inbound('stop-held', '/stop'), context)).status, 'stopped');
    assert.equal(journal.getDispatch(prepared!.dispatch_id as string)?.status, 'cancelled');
    assert.equal(sends, 0);
  });
});

test('contacted reply and contacted action become unknown on crash and cannot regenerate', async () => {
  await withDb(async (journal, path) => {
    const eventId = admitForRecovery(journal, 'event', 'send the message');
    journal.startRun({ runId: 'run', eventId });
    journal.recordActionIntent({ actionId: 'action', runId: 'run', kind: 'bridge-send', payloadHash: 'h' });
    journal.beginAction('action');
    journal.prepareReply({ replyId: 'reply', runId: 'run', eventId, dispatchId: 'dispatch',
      kind: 'interactive', destination: context.destination, payload: 'possibly sent' });
    journal.beginDispatch('dispatch');
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      reopened.recoverInterrupted();
      let models = 0;
      let sends = 0;
      const service = coordinator(reopened, async () => { models++; return 'bad replay'; }, async () => {
        sends++; return { acceptance: 'accepted', messageId: 'bad' };
      });
      assert.equal((await service.recover())[0]?.status, 'held');
      assert.equal(models, 0);
      assert.equal(sends, 0);
      assert.equal(reopened.getInbound(eventId)?.outcome, 'pending');
      assert.equal(reopened.getDispatch('dispatch')?.status, 'unknown');
      assert.equal(reopened.getAction('action')?.status, 'unknown');
    } finally { reopened.close(); }
  });
});

test('invalid add effect authority isolates its command and gives only an uncertainty notice, never inspecting or claiming success', async () => {
  for (const variant of ['wrong-scope', 'action-payload-mismatch'] as const) {
    await withDb(async journal => {
      const old = admitForRecovery(journal, `old-${variant}`, '/loop add Confirm budget with Alex');
      journal.startRun({ runId: `run-${variant}`, eventId: old });
      const intent = journal.prepareExplicitOwnerLoopIntent({ runId: `run-${variant}`, accountId: 'account',
        ownerId: 'owner', lineKey: 'line', channel: 'photon', scopeHash: 'a'.repeat(64) });
      journal.bindExplicitOwnerLoopZone(intent.actionId, 'America/Los_Angeles');
      journal.beginAction(intent.actionId);
      journal.settleAction(intent.actionId, 'unknown');
      if (variant === 'action-payload-mismatch') {
        journal.db.prepare('UPDATE actions SET source_payload_hash=? WHERE id=?')
          .run('f'.repeat(64), intent.actionId);
      }
      let receiverCalls = 0;
      const ownerLoop = { identity: { line_key: 'line' },
        scopeHash: variant === 'wrong-scope' ? 'b'.repeat(64) : 'a'.repeat(64),
        call: async () => { receiverCalls++; throw new Error('invalid authority contacted receiver'); },
        callClose: async () => { receiverCalls++; throw new Error('invalid authority contacted receiver'); },
      } as unknown as OwnerLoopClient;
      const generated: ModelRequest[] = [];
      const sent: PreparedPost[] = [];
      const service = new ConversationCoordinator({ journal, ownerPolicy, ownerLoop,
        model: { generate: async request => { generated.push(request); return 'safe answer'; } },
        postPrepared: async request => { sent.push(request); return { acceptance: 'accepted', messageId: 'safe-provider' }; },
      });
      assert.equal((await service.recover())[0]?.status, 'held');
      assert.equal((await service.receive(inbound(`safe-${variant}`, 'What remains open?'), context)).status, 'accepted');
      assert.deepEqual(generated.map(request => request.text), ['What remains open?']);
      assert.match(sent[0]!.text, /^I couldn’t confirm whether the task addition/);
      assert.deepEqual(sent.slice(1).map(request => request.text), ['safe answer']);
      assert.deepEqual(JSON.parse(journal.getReplyForEvent(generated[0]!.ownerRequestKey)?.covered_event_ids as string),
        [generated[0]!.ownerRequestKey]);
      assert.equal(journal.getInbound(old)?.outcome, 'pending');
      assert.equal(journal.getAction(intent.actionId)?.status, 'unknown');
      assert.equal(receiverCalls, 0);
      if (variant === 'wrong-scope') {
        // A source row that no longer identifies the pending admitted command
        // cannot be used even for the weaker partition decision.
        journal.db.prepare('UPDATE actions SET source_event_id=? WHERE id=?')
          .run(generated[0]!.ownerRequestKey, intent.actionId);
        assert.equal((await service.receive(inbound('after-invalid-source', 'another safe question'), context)).status, 'held');
        assert.equal(generated.length, 1);
        assert.equal(sent.length, 2, 'corrupt source cannot add a reply after the prior uncertainty notice and safe answer');
      }
    });
  }
});

test('recovery holds only contacted reply coverage and batches later admitted owner events separately', async () => {
  await withDb(async (journal, path) => {
    const oldFirst = admitForRecovery(journal, 'old-first', 'old question first');
    const oldSecond = admitForRecovery(journal, 'old-second', 'old question second');
    journal.startRun({ runId: 'old-run', eventId: oldFirst });
    journal.prepareReply({ replyId: 'old-reply', runId: 'old-run', eventId: oldFirst,
      coveredEventIds: [oldFirst, oldSecond], dispatchId: 'old-dispatch', kind: 'interactive',
      destination: context.destination, payload: 'possibly sent old answer' });
    journal.beginDispatch('old-dispatch');
    const newFirst = admitForRecovery(journal, 'new-first', 'new question first');
    const newSecond = admitForRecovery(journal, 'new-second', 'new question second');
    journal.close();

    const reopened = new OperationalJournal(path);
    try {
      reopened.recoverInterrupted();
      const generated: ModelRequest[] = [];
      const sent: PreparedPost[] = [];
      const service = new ConversationCoordinator({ journal: reopened, ownerPolicy,
        model: { generate: async request => { generated.push(request); return 'new answer'; } },
        postPrepared: async request => { sent.push(request); return { acceptance: 'accepted', messageId: 'new-provider' }; },
      });
      assert.equal((await service.recover())[0]?.status, 'held');
      assert.deepEqual(generated.map(request => request.text), ['new question first\nnew question second']);
      assert.deepEqual(generated[0]?.messages.map(message => message.eventId), ['new-first', 'new-second']);
      assert.equal(generated[0]?.ownerRequestKey, newFirst);
      assert.deepEqual(sent.map(request => request.text), ['new answer']);
      const replies = reopened.listRepliesForThread(oldFirst);
      const fresh = replies.find(reply => reply.dispatch_id === sent[0]?.dispatchId);
      assert.deepEqual(JSON.parse(fresh?.covered_event_ids as string), [newFirst, newSecond]);
      assert.equal(reopened.getInbound(oldFirst)?.outcome, 'pending');
      assert.equal(reopened.getInbound(oldSecond)?.outcome, 'pending');
      assert.equal(reopened.getDispatch('old-dispatch')?.status, 'unknown');
      assert.deepEqual(reopened.getAttempts('old-dispatch').map(row => [row.number, row.status]), [[1, 'unknown']]);
      assert.equal(reopened.getInbound(newFirst)?.outcome, 'resolved');
      assert.equal(reopened.getInbound(newSecond)?.outcome, 'resolved');
      assert.equal((await service.recover())[0]?.status, 'held');
      assert.equal(generated.length, 1);
      assert.equal(sent.length, 1);
    } finally { reopened.close(); }
  });
});

test('a new same-thread owner turn and /new proceed while an earlier contacted reply stays unknown', async () => {
  await withDb(async (journal, path) => {
    const old = admitForRecovery(journal, 'old', 'old question');
    journal.startRun({ runId: 'old-run', eventId: old });
    journal.prepareReply({ replyId: 'old-reply', runId: 'old-run', eventId: old,
      dispatchId: 'old-dispatch', kind: 'interactive', destination: context.destination,
      payload: 'possibly sent old answer' });
    journal.beginDispatch('old-dispatch');
    journal.close();

    const reopened = new OperationalJournal(path);
    try {
      reopened.recoverInterrupted();
      const generated: ModelRequest[] = [];
      const sent: PreparedPost[] = [];
      const rotated: string[] = [];
      const service = new ConversationCoordinator({ journal: reopened, ownerPolicy,
        model: { generate: async request => { generated.push(request); return 'new answer'; } },
        postPrepared: async request => { sent.push(request); return { acceptance: 'accepted', messageId: 'new-provider' }; },
        rotateSession: async threadId => { rotated.push(threadId); },
      });
      assert.equal((await service.recover())[0]?.status, 'held');
      assert.equal((await service.receive(inbound('new-turn', 'new question'), context)).status, 'accepted');
      assert.deepEqual(generated.map(request => request.text), ['new question']);
      assert.deepEqual(generated[0]?.messages.map(message => message.eventId), ['new-turn']);
      assert.deepEqual(sent.map(request => request.text), ['new answer']);
      const newEvent = reopened.listRepliesForThread(old).find(reply => reply.dispatch_id === sent[0]?.dispatchId);
      assert.deepEqual(JSON.parse(newEvent?.covered_event_ids as string), [generated[0]?.ownerRequestKey]);
      assert.equal(reopened.getInbound(old)?.outcome, 'pending');
      assert.deepEqual(reopened.getAttempts('old-dispatch').map(row => [row.number, row.status]), [[1, 'unknown']]);

      assert.equal((await service.receive(inbound('rotate', '/new'), context)).status, 'rotated');
      assert.deepEqual(rotated, [context.threadId]);
      assert.equal(reopened.getInbound(old)?.outcome, 'pending');
      assert.equal(reopened.getDispatch('old-dispatch')?.status, 'unknown');
      assert.equal(generated.length, 1);
      assert.deepEqual(sent.map(request => request.text), ['new answer', 'I started a new conversation.']);

      reopened.settleDispatch('old-dispatch', 1, 'accepted', 'late-old-provider-proof');
      await service.recover();
      assert.equal(reopened.getInbound(old)?.outcome, 'resolved');
      assert.equal(generated.length, 1);
      assert.equal(sent.length, 2);
    } finally { reopened.close(); }
  });
});

test('a prepared earlier reply posts unchanged before later admitted owner input', async () => {
  await withDb(async journal => {
    const old = admitForRecovery(journal, 'old', 'old question');
    journal.startRun({ runId: 'old-run', eventId: old });
    journal.prepareReply({ replyId: 'old-reply', runId: 'old-run', eventId: old,
      dispatchId: 'old-dispatch', kind: 'interactive', destination: context.destination,
      payload: 'durable old answer' });
    const later = admitForRecovery(journal, 'later', 'later question');
    const generated: ModelRequest[] = [];
    const sent: PreparedPost[] = [];
    const service = new ConversationCoordinator({ journal, ownerPolicy,
      model: { generate: async request => { generated.push(request); return 'later answer'; } },
      postPrepared: async request => { sent.push(request); return { acceptance: 'accepted', messageId: request.dispatchId }; },
    });
    await service.recover();
    assert.deepEqual(sent.map(request => request.text), ['durable old answer', 'later answer']);
    assert.deepEqual(generated.map(request => request.text), ['later question']);
    assert.deepEqual(JSON.parse(journal.getReplyForEvent(later)?.covered_event_ids as string), [later]);
    assert.equal(journal.getInbound(old)?.outcome, 'resolved');
    assert.equal(journal.getInbound(later)?.outcome, 'resolved');
  });
});

test('/new preserves pending obligations before rotation and never includes command in model text', async () => {
  await withDb(async journal => {
    const firstStarted = deferred<ModelRequest>();
    const finishFirst = deferred<string>();
    const modelTexts: string[] = [];
    const rotated: string[] = [];
    const presence: string[] = [];
    const service = new ConversationCoordinator({ journal, ownerPolicy,
      model: { generate: request => {
        modelTexts.push(request.text);
        if (modelTexts.length === 1) { firstStarted.resolve(request); return finishFirst.promise; }
        return Promise.resolve('completed pending');
      } },
      postPrepared: async () => ({ acceptance: 'accepted', messageId: 'provider' }),
      rotateSession: async threadId => { rotated.push(threadId); },
      startTurnPresence: (_thread, runId) => { presence.push(`start:${runId}`); return () => presence.push(`stop:${runId}`); },
    });
    const first = service.receive(inbound('one', 'unfinished question'), context);
    await firstStarted.promise;
    const rotation = service.receive(inbound('new', '/new'), context);
    finishFirst.resolve('stale');
    assert.equal((await first).status, 'cancelled');
    assert.equal((await rotation).status, 'rotated');
    assert.deepEqual(modelTexts, ['unfinished question', 'unfinished question']);
    assert.deepEqual(rotated, ['thread']);
    assert.equal(presence.length, 4);
    assert.equal(presence[0]?.replace('start:', ''), presence[1]?.replace('stop:', ''));
    assert.equal(presence[2]?.replace('start:', ''), presence[3]?.replace('stop:', ''));
    assert.equal(journal.listPendingInbound().length, 0);
  });
});

test('prepared new-conversation acknowledgement resumes after restart without rotating or sending twice', async () => {
  await withDb(async (journal, path) => {
    let rotations = 0; let sends = 0; let models = 0;
    const first = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async () => {models++; return 'wrong';}},
      rotateSession: async () => {rotations++;},
      canPostPrepared: async () => false,
      postPrepared: async () => {sends++; return {acceptance: 'accepted', messageId: 'wrong'};},
    });
    assert.equal((await first.receive(inbound('new', '/new'), context)).status, 'held');
    const command = admittedId(journal, 'new');
    const reply = journal.listRepliesForThread(command).find(row => row.purpose === 'owner_control');
    assert.equal(reply?.payload, 'I started a new conversation.');
    assert.equal(rotations, 1); assert.equal(sends, 0); assert.equal(models, 0);
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      const recovery = new ConversationCoordinator({journal: reopened, ownerPolicy,
        model: {generate: async () => {models++; return 'wrong';}},
        rotateSession: async () => {rotations++;},
        postPrepared: async () => {sends++; return {acceptance: 'accepted', messageId: 'new-provider-id'};},
      });
      await recovery.recover(); await recovery.recover();
      assert.equal(rotations, 1); assert.equal(sends, 1); assert.equal(models, 0);
      assert.equal(reopened.getInbound(command)?.outcome, 'resolved');
      assert.equal(reopened.getDispatch(reply!.dispatch_id as string)?.status, 'accepted');
    } finally {reopened.close();}
  });
});

test('contacted unknown new-conversation acknowledgement stays held without a second rotation or send', async () => {
  await withDb(async (journal, path) => {
    let rotations = 0; let sends = 0;
    const first = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async () => 'wrong'}, rotateSession: async () => {rotations++;},
      postPrepared: async () => {sends++; return {acceptance: 'unknown'};},
    });
    assert.equal((await first.receive(inbound('new', '/new'), context)).status, 'held');
    assert.equal(rotations, 1); assert.equal(sends, 1);
    const command = admittedId(journal, 'new');
    const reply = journal.listRepliesForThread(command).find(row => row.purpose === 'owner_control');
    assert.equal(journal.getDispatch(reply!.dispatch_id as string)?.status, 'unknown');
    journal.close();
    const reopened = new OperationalJournal(path);
    try {
      const recovery = new ConversationCoordinator({journal: reopened, ownerPolicy,
        model: {generate: async () => 'wrong'}, rotateSession: async () => {rotations++;},
        postPrepared: async () => {sends++; return {acceptance: 'accepted', messageId: 'wrong'};},
      });
      await recovery.recover(); await recovery.recover();
      assert.equal(rotations, 1); assert.equal(sends, 1);
      assert.equal(reopened.getInbound(command)?.outcome, 'pending');
      assert.equal(reopened.getDispatch(reply!.dispatch_id as string)?.status, 'unknown');
      assert.equal((await recovery.receive(inbound('safe-after-new', 'Hello'), context)).status, 'accepted');
      assert.equal(rotations, 1); assert.equal(sends, 2);
      assert.equal(reopened.getInbound(command)?.outcome, 'pending');
      assert.equal(reopened.getDispatch(reply!.dispatch_id as string)?.status, 'unknown');
    } finally {reopened.close();}
  });
});

test('hung contacted new-conversation acknowledgement releases later safe chat without rerotation', async () => {
  await withDb(async journal => {
    let rotations = 0; let sends = 0;
    const service = new ConversationCoordinator({journal, ownerPolicy, controlDeliveryDeadlineMs: 20,
      model: {generate: async () => 'safe answer'}, rotateSession: async () => {rotations++;},
      postPrepared: async request => {
        sends++;
        if (request.text === 'I started a new conversation.') await new Promise<void>(() => {});
        return {acceptance: 'accepted', messageId: 'safe-provider-id'};
      },
    });
    assert.equal((await service.receive(inbound('new', '/new'), context)).status, 'held');
    assert.equal((await service.receive(inbound('later', 'Hello'), context)).status, 'accepted');
    const command = admittedId(journal, 'new');
    const reply = journal.listRepliesForThread(command).find(row => row.purpose === 'owner_control');
    assert.equal(journal.getDispatch(reply!.dispatch_id as string)?.status, 'unknown');
    assert.equal(journal.getInbound(command)?.outcome, 'pending');
    assert.equal(rotations, 1); assert.equal(sends, 2);
  });
});

test('close cancels active model and cleans presence without waiting on provider operations', async () => {
  await withDb(async journal => {
    const entered = deferred<void>();
    const finish = deferred<string>();
    const presence: string[] = [];
    const service = new ConversationCoordinator({ journal, ownerPolicy,
      model: { generate: request => { entered.resolve(); assert.equal(request.signal.aborted, false); return finish.promise; } },
      postPrepared: async () => ({ acceptance: 'accepted', messageId: 'unexpected' }),
      startTurnPresence: () => { presence.push('start'); return () => presence.push('stop'); },
    });
    const turn = service.receive(inbound('close-presence', 'question'), context);
    await entered.promise;
    const closing = service.close();
    finish.resolve('late answer');
    assert.equal((await turn).status, 'cancelled');
    await closing;
    assert.deepEqual(presence, ['start', 'stop']);
  });
});

test('maintenance admits durably, then resumes pending owner input even when maintenance fails', async () => {
  await withDb(async journal => {
    const entered = deferred<void>();
    const release = deferred<void>();
    const texts: string[] = [];
    const service = coordinator(journal, async request => { texts.push(request.text); return 'recovered answer'; });
    const maintenance = service.exclusive(async () => {
      entered.resolve();
      await release.promise;
      throw new Error('fixture rotation failure');
    });
    await entered.promise;
    assert.equal((await service.receive(inbound('during-rotation', 'keep this request'), context)).status, 'queued');
    assert.equal(journal.listPendingInbound().length, 1);
    assert.equal(texts.length, 0);
    release.resolve();
    await assert.rejects(maintenance, /fixture rotation failure/);
    await service.drain();
    assert.deepEqual(texts, ['keep this request']);
    assert.equal(journal.listPendingInbound().length, 0);
  });
});

test('close joins maintenance and leaves queued input durable for restart', async () => {
  await withDb(async journal => {
    const entered = deferred<void>();
    const release = deferred<void>();
    let models = 0;
    const service = coordinator(journal, async () => { models++; return 'must not run after close'; });
    const maintenance = service.exclusive(async () => { entered.resolve(); await release.promise; });
    await entered.promise;
    await service.receive(inbound('during-close', 'pending request'), context);
    let closed = false;
    const closing = service.close().then(() => { closed = true; });
    await Promise.resolve();
    assert.equal(closed, false);
    release.resolve();
    await maintenance;
    await closing;
    assert.equal(models, 0);
    assert.equal(journal.listPendingInbound().length, 1);
  });
});


test('owner feedback is fresh admission only and terminal feedback requires accepted reply', async () => {
  await withDb(async journal => {
    const admitted: string[] = [], accepted: string[] = [];
    const providerContext = {...context, threadId: 'imessage:any;-;+15550001111~shared'};
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async () => 'reply'}, postPrepared: async () => ({acceptance: 'accepted', messageId: 'accepted'}),
      onOwnerAdmission: (input, threadId) => {assert.equal(threadId, providerContext.threadId); admitted.push(input.eventId);},
      onOwnerReplyAccepted: (input, threadId) => {assert.equal(threadId, providerContext.threadId); accepted.push(input.eventId);},
    });
    for (const rejected of [inbound('foreign-feedback', 'hello', {senderId: 'foreign'}),
      inbound('group-feedback', 'hello', {isDirect: false}),
      inbound('receipt-feedback', '', {receiptOnly: true})]) await service.receive(rejected, providerContext);
    await service.receive(inbound('fresh-feedback', 'hello'), providerContext);
    await service.receive(inbound('fresh-feedback', 'hello'), providerContext);
    assert.deepEqual(admitted, ['fresh-feedback']);
    assert.deepEqual(accepted, ['fresh-feedback']);
    const held = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async () => 'reply'}, postPrepared: async () => ({acceptance: 'unknown'}),
      onOwnerAdmission: (input, threadId) => {assert.equal(threadId, providerContext.threadId); admitted.push(input.eventId);},
      onOwnerReplyAccepted: (input, threadId) => {assert.equal(threadId, providerContext.threadId); accepted.push(input.eventId);},
    });
    assert.equal((await held.receive(inbound('held-feedback', 'hello'), providerContext)).status, 'held');
    assert.deepEqual(accepted, ['fresh-feedback']);
    const recovered = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async () => 'unused'}, postPrepared: async () => ({acceptance: 'accepted', messageId: 'unused'}),
      onOwnerAdmission: () => assert.fail('recovery is not fresh admission'),
      onOwnerReplyAccepted: () => assert.fail('recovery must not replay feedback'),
    });
    await recovered.recover();
  });
});


test('provider feedback failure does not block an accepted owner reply', async () => {
  await withDb(async journal => {
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async () => 'reply'}, postPrepared: async () => ({acceptance: 'accepted', messageId: 'provider'}),
      onOwnerAdmission: () => {throw new Error('provider unavailable');},
      onOwnerReplyAccepted: () => {throw new Error('provider unavailable');},
    });
    assert.equal((await service.receive(inbound('failed-feedback', 'hello'), context)).status, 'accepted');
  });
});

test('offered-draft recovery stops waiting on stalled source authority and permits safe chat', async t => {
  t.mock.timers.enable({apis: ['setTimeout']});
  await withDb(async journal => {
    const entered = deferred<void>(), source = deferred<boolean>();
    let stalled = true, recordings = 0;
    // Acceptance validation is exercised by Gmail connected tests. This fixture
    // isolates a source read that ignores the maintenance cancellation signal.
    journal.listPendingGmailOfferedPairs = () => [{id: 'pair', thread_key: 'thread'}];
    journal.acceptedGmailOfferedPair = () => ({request: {}, acceptedDelivery: {source_revision: 'a'.repeat(64)}} as
      NonNullable<ReturnType<OperationalJournal['acceptedGmailOfferedPair']>>);
    const client = {identity: {line_key: 'line'}, scopeHash: 'scope',
      recordOffer: async () => { recordings++; throw new Error('must not record'); },
    } as unknown as OwnerGmailDraftClient;
    const service = new ConversationCoordinator({journal, ownerPolicy, ownerGmailDraft: client,
      model: {generate: async () => 'safe reply'},
      postPrepared: async () => ({acceptance: 'accepted', messageId: 'safe'}),
      canPostPrepared: async () => { if (!stalled) return true; entered.resolve(); return source.promise; },
    });
    const recovery = service.recover();
    await entered.promise;
    t.mock.timers.tick(5000);
    assert.deepEqual(await recovery, []);
    stalled = false;
    assert.equal((await service.receive(inbound('safe-after-offer-hold', 'Hello'), context)).status, 'accepted');
    source.resolve(true);
    await Promise.resolve();
    assert.equal(recordings, 0, 'late source authority must not begin local recording');
    await service.close();
  });
});

test('verified budget refusal posts durable owner-only notice once and unclassified errors disclose no raw details', async () => {
  const {ManagedProxyBudgetExhausted}=await import('../src/agent/models.js');
  const {OWNER_ONLY_CONTEXT_PROOF}=await import('../src/agent/source-authority.js');
  await withDb(async journal=>{
    let models=0,sends=0;
    const service=new ConversationCoordinator({journal,ownerPolicy,model:{generate:async()=>{models++;throw new ManagedProxyBudgetExhausted();}},
      canPostPrepared:async proof=>{assert.equal(proof,OWNER_ONLY_CONTEXT_PROOF);return true;},
      postPrepared:async post=>{sends++;assert.match(post.text,/spending allowance is too low/);return {acceptance:'accepted',messageId:'notice'};}});
    assert.equal((await service.receive(inbound('budget','Hello'),context)).status,'accepted');
    assert.equal((await service.receive(inbound('budget','Hello'),context)).status,'duplicate');
    await service.recover();assert.equal(models,1);assert.equal(sends,1);await service.close();
    let genericNotice='';
    const other=coordinator(journal,async()=>{throw Error('402 sotto_budget_exhausted');},async post=>{
      genericNotice=post.text;return {acceptance:'accepted',messageId:'generic-notice'};});
    assert.equal((await other.receive(inbound('other','Hello'),context)).status,'accepted');
    assert.match(genericNotice,/model request failed or took too long/);
    assert.doesNotMatch(genericNotice,/402|sotto_budget_exhausted/);
    await other.close();
  });
});

test('managed upstream outage posts one fixed failure reply and activation recovery does not rerun the model', async () => {
  const {ManagedProxyUpstreamUnavailable} = await import('../src/agent/models.js');
  const {OWNER_ONLY_CONTEXT_PROOF} = await import('../src/agent/source-authority.js');
  await withDb(async journal => {
    let models = 0, sends = 0;
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async () => { models++; throw new ManagedProxyUpstreamUnavailable(503); }},
      canPostPrepared: async proof => { assert.equal(proof, OWNER_ONLY_CONTEXT_PROOF); return true; },
      postPrepared: async post => {
        sends++;
        assert.match(post.text, /model service is temporarily unavailable/);
        assert.match(post.text, /check its status before retrying/);
        assert.doesNotMatch(post.text, /503|provider/i);
        return {acceptance: 'accepted', messageId: 'failure-reply'};
      },
    });
    try {
      assert.equal((await service.receive(inbound('outage', 'Hello'), context)).status, 'accepted');
      assert.equal(journal.listPendingInbound().length, 0);
      assert.equal(journal.db.prepare('SELECT purpose FROM prepared_replies').get()!.purpose,'model_failure');
      await service.recover(); // The activation ACK invokes this after admission.
      assert.equal(models, 1);
      assert.equal(sends, 1);
    } finally { await service.close(); }
  });
});

test('unknown owner action remains held when the model reports managed upstream outage', async () => {
  const {ManagedProxyUpstreamUnavailable} = await import('../src/agent/models.js');
  await withDb(async journal => {
    let sends = 0, models = 0;
    const ownerCalendar = {identity: {line_key: 'line'}, scopeHash: 'a'.repeat(64),
      call: async () => { throw new Error('effect proof unavailable'); }} as never;
    const service = new ConversationCoordinator({journal, ownerPolicy, ownerCalendar,
      model: {generate: async request => {
        models++;
        journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,'unknown',?,?,?)")
          .run('unknown-calendar-effect', request.runId, 'owner_calendar_tool', 'a'.repeat(64), request.ownerRequestKey, '{}', Date.now(), Date.now());
        throw new ManagedProxyUpstreamUnavailable(503);
      }},
      postPrepared: async () => { sends++; return {acceptance: 'accepted', messageId: 'unsafe'}; },
    });
    try {
      assert.equal((await service.receive(inbound('outage-action', 'Update my calendar'), context)).status, 'held');
      assert.equal(models, 1);
      assert.equal(sends, 1, 'an honest uncertainty notice replaces any unverified model success');
      assert.equal(journal.listPendingInbound().length, 1);
      assert.equal(journal.getAction('unknown-calendar-effect')?.status, 'unknown');
    } finally { await service.close(); }
  });
});

test('busy, oversized, and plain model failures send one safe fixed reply without retrying the model', async () => {
  const {ManagedProxyAdmissionBusy, ManagedProxyRequestTooLarge} = await import('../src/agent/models.js');
  for (const [label, failure, expected] of [
    ['busy', new ManagedProxyAdmissionBusy(), /busy/],
    ['oversized', new ManagedProxyRequestTooLarge(), /too large/],
    ['plain', new Error('private provider body'), /couldn’t complete a response/],
  ] as const) await withDb(async journal => {
    let models = 0; const replies: string[] = [];
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async () => {models++; throw failure;}},
      postPrepared: async post => {replies.push(post.text); return {acceptance: 'accepted', messageId: 'fixed'};}});
    try {
      assert.equal((await service.receive(inbound(`model-${label}`, 'Hello'), context)).status, 'accepted');
      await service.recover();
      assert.equal(models, 1);
      assert.equal(replies.length, 1);
      assert.match(replies[0]!, expected);
      assert.doesNotMatch(replies[0]!, /private provider body/);
      assert.equal(journal.listPendingInbound().length, 0);
    } finally {await service.close();}
  });
});

test('model deadline after an uncertain action stays held while stopped work sends no fallback', async () => {
  await withDb(async (journal, path) => {
    let sends = 0, generations = 0;
    const model = new MockLanguageModelV4({doGenerate: async ({abortSignal}) => {
      // AbortSignal.timeout does not keep Node's event loop alive; hold it until the turn deadline
      // aborts, so the test exercises the deadline rather than racing Mastra's setup.
      await new Promise<void>((_resolve, reject) => { const hold = setTimeout(() => {}, 60_000);
        abortSignal?.addEventListener('abort', () => { clearTimeout(hold); reject(abortSignal.reason); }, {once: true}); });
      throw new Error('unreachable');
    }});
    const runtime = createSottoAgentRuntime({model, storagePath: `${path}.deadline-memory`,
      instructions: 'Answer briefly.', modelTurnDeadlineMs: 1000});
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async request => {
        generations++;
        journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,'unknown',?,?,?)")
          .run('deadline-effect', request.runId, 'owner_calendar_tool', 'a'.repeat(64), request.ownerRequestKey, '{}', Date.now(), Date.now());
        return runtime.generateChat(request);
      }},
      postPrepared: async () => {sends++; return {acceptance: 'accepted', messageId: 'unsafe'};}});
    try {
      assert.equal((await service.receive(inbound('deadline-effect-source', 'Update my calendar'), context)).status, 'held');
      assert.equal(sends, 0);
      assert.equal(journal.getAction('deadline-effect')?.status, 'unknown');
      assert.equal(generations, 1);
      assert.equal(model.doGenerateCalls.length, 1, 'the held provider request must time out without retry');
    } finally {await service.close(); await runtime.close();}
  });
  await withDb(async journal => {
    const entered = deferred<ModelRequest>(); const release = deferred<void>(); let sends = 0;
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async request => {entered.resolve(request); await release.promise; throw new Error('late model error');}},
      postPrepared: async () => {sends++; return {acceptance: 'accepted', messageId: 'unsafe'};}});
    const first = service.receive(inbound('cancelled-failure', 'Hello'), context);
    await entered.promise;
    await service.receive(inbound('cancelled-stop', '/stop'), context);
    release.resolve();
    assert.equal((await first).status, 'cancelled');
    assert.equal(sends, 0);
    await service.close();
  });
});

test('own model deadline without an action gives one durable fixed reply', async () => {
  await withDb(async (journal, path) => {
    const model = new MockLanguageModelV4({doGenerate: async ({abortSignal}) => {
      // AbortSignal.timeout does not keep Node's event loop alive; hold it until the turn deadline
      // aborts, so the test exercises the deadline rather than racing Mastra's setup.
      await new Promise<void>((_resolve, reject) => { const hold = setTimeout(() => {}, 60_000);
        abortSignal?.addEventListener('abort', () => { clearTimeout(hold); reject(abortSignal.reason); }, {once: true}); });
      throw new Error('unreachable');
    }});
    // Leave room for cold Mastra setup in the full suite: this case must reach
    // the held provider call before its test-only deadline expires.
    const runtime = createSottoAgentRuntime({model, storagePath: `${path}.safe-deadline-memory`,
      instructions: 'Answer briefly.', modelTurnDeadlineMs: 5000});
    const replies: string[] = [];
    let generations = 0;
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: request => {generations++; return runtime.generateChat(request);}},
      postPrepared: async post => {replies.push(post.text); return {acceptance: 'accepted', messageId: 'deadline-notice'};}});
    try {
      assert.equal((await service.receive(inbound('safe-deadline', 'Hello'), context)).status, 'accepted');
      await service.recover();
      assert.equal(generations, 1);
      assert.equal(model.doGenerateCalls.length, 1, 'the held provider request must time out without retry');
      assert.equal(replies.length, 1);
      assert.match(replies[0]!, /model request failed or took too long/);
      assert.equal(journal.listPendingInbound().length, 0);
    } finally {await service.close(); await runtime.close();}
  });
});

test('final source revocation withholds generated content and sends one source-specific notice', async () => {
  await withDb(async (journal, path) => {
    let source: SourceAuthority = {version: 2, revision: 'a'.repeat(64), complete: true,
      bridge_sources: ['imessage'], google_sources: [], connector_sources: []};
    let sourceReads = 0; const posts: string[] = [];
    const model = new MockLanguageModelV4({doGenerate: async () => {
      source = {...source, revision: 'b'.repeat(64)};
      return {content: [{type: 'text', text: 'REVOKED PRIVATE CONTENT'}],
        finishReason: {unified: 'stop', raw: 'STOP'}, usage: mockUsage, warnings: []};
    }});
    const runtime = createSottoAgentRuntime({model, storagePath: `${path}.revocation-memory`,
      instructions: 'Answer briefly.', sourceAuthority: {read: async () => {sourceReads++; return source;}}});
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: request => runtime.generateChat(request)},
      postPrepared: async post => {posts.push(post.text); return {acceptance: 'accepted', messageId: 'source-notice'};}});
    try {
      assert.equal((await service.receive(inbound('revoked-source', 'Read my messages'), context)).status, 'accepted');
      await service.recover();
      assert.equal(model.doGenerateCalls.length, 1, 'recovery does not regenerate');
      assert.equal(sourceReads, 2, 'begin and final source checks remain live');
      assert.equal(posts.length, 1, 'only the fixed notice contacts delivery');
      assert.match(posts[0]!, /source permissions changed or could not be verified/);
      assert.doesNotMatch(posts[0]!, /REVOKED PRIVATE CONTENT|model request failed/i);
      assert.equal(journal.listPendingInbound().length, 0);
    } finally {await service.close(); await runtime.close();}
  });
});

test('provider delivery failure never becomes a second model-failure reply', async () => {
  await withDb(async journal => {
    let sends = 0;
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: async () => 'answer'},
      postPrepared: async () => {sends++; throw new Error('private provider delivery failure');}});
    try {
      assert.equal((await service.receive(inbound('delivery-failed', 'Hello'), context)).status, 'held');
      assert.equal(sends, 1);
      assert.equal(journal.listPendingInbound().length, 1);
    } finally {await service.close();}
  });
});

test('unknown budget notice is inspect-only across reopen and does not block fresh chat',async()=>{
  const {ManagedProxyBudgetExhausted}=await import('../src/agent/models.js');
  await withDb(async(journal,path)=>{
    let models=0,sends=0;
    const first=new ConversationCoordinator({journal,ownerPolicy,model:{generate:async()=>{models++;throw new ManagedProxyBudgetExhausted();}},
      postPrepared:async()=>{sends++;return {acceptance:'unknown'};}});
    assert.equal((await first.receive(inbound('budget','Hello'),context)).status,'held');await first.close();journal.close();
    const reopened=new OperationalJournal(path);reopened.recoverInterrupted();
    const second=new ConversationCoordinator({journal:reopened,ownerPolicy,model:{generate:async request=>{models++;assert.equal(request.text,'Fresh hello');return 'fresh answer';}},
      postPrepared:async()=>{sends++;return {acceptance:'accepted',messageId:'fresh'};}});
    try{await second.recover();assert.equal(models,1);assert.equal(sends,1);
      assert.equal((await second.receive(inbound('fresh','Fresh hello'),context)).status,'accepted');assert.equal(models,2);assert.equal(sends,2);}
    finally{await second.close();reopened.close();}
  });
});

test('budget notice cannot contact provider after close during final preflight',async()=>{
  const {ManagedProxyBudgetExhausted}=await import('../src/agent/models.js');
  await withDb(async journal=>{
    const entered=deferred<void>(),release=deferred<boolean>();let sends=0;
    const service=new ConversationCoordinator({journal,ownerPolicy,model:{generate:async()=>{throw new ManagedProxyBudgetExhausted();}},
      canPostPrepared:async()=>{entered.resolve();return release.promise;},postPrepared:async()=>{sends++;return {acceptance:'accepted',messageId:'bad'};}});
    const turn=service.receive(inbound('budget','Hello'),context);await entered.promise;
    const closing=service.close();release.resolve(true);await closing;await turn;assert.equal(sends,0);
  });
});

const mockUsage = {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}};
test('model failures get fixed replies while owner text stays within the model bound and a long message is truncated', async () => {
  await withDb(async (journal, path) => {
    let calls = 0; const prompts: number[] = []; const sent: string[] = [];
    const model = new MockLanguageModelV4({doGenerate: async options => {
      prompts.push(JSON.stringify(options.prompt).length);
      if (++calls <= 2) throw new Error('provider outage');
      return {content: [{type: 'text', text: `answer ${calls}`}], finishReason: {unified: 'stop', raw: 'STOP'}, usage: mockUsage, warnings: []};
    }});
    const runtime = createSottoAgentRuntime({model, storagePath: `${path}.memory`, instructions: 'Answer briefly.'});
    const texts: string[] = [];
    const service = new ConversationCoordinator({journal, ownerPolicy,
      model: {generate: request => { texts.push(request.text); return runtime.generateChat(request); }},
      postPrepared: async request => { sent.push(request.text); return {acceptance: 'accepted', messageId: `m${sent.length}`}; }});
    try {
      const big = 'p'.repeat(12_000);
      const statuses: string[] = [];
      for (const [id, text] of [['a', big], ['b', big], ['c', big], ['d', 'hello?'], ['e', 'are you there?']] as const)
        statuses.push((await service.receive(inbound(id, text), context)).status);
      assert.deepEqual(statuses.slice(0, 2), ['accepted', 'accepted']);
      assert.deepEqual(statuses.slice(2), ['accepted', 'accepted', 'accepted']);
      assert.ok(texts.every(text => text.length <= 32_768), 'every model turn fits the chat bound');
      assert.equal(journal.listPendingInbound().length, 0, 'failed turns and later owner input are resolved');
      assert.equal(sent.length, 5);
      assert.ok(sent.slice(0,2).every(reply=>reply.includes('model request failed or took too long')));
      // A single message above the bound (direct callers bypass the gateway cap) is truncated explicitly.
      const huge = 'q'.repeat(40_000);
      assert.equal((await service.receive(inbound('f', huge), context)).status, 'accepted');
      assert.ok(texts.at(-1)!.length <= 32_768);
      assert.match(texts.at(-1)!, /\[Owner message truncated/);
      assert.equal(journal.listPendingInbound().length, 0);
    } finally { await service.close(); await runtime.close(); }
  });
});


test('monthly and lifetime budget notices give the correct next step', async () => {
  const {ManagedProxyBudgetExhausted}=await import('../src/agent/models.js');
  for (const period of ['calendar_month','lifetime'] as const) await withDb(async journal=>{
    let notice='';
    const service=new ConversationCoordinator({journal,ownerPolicy,
      model:{generate:async()=>{throw new ManagedProxyBudgetExhausted(period);}},
      postPrepared:async post=>{notice=post.text;return {acceptance:'accepted',messageId:'notice'};}});
    try {
      assert.equal((await service.receive(inbound(`budget-${period}`,'Hello'),context)).status,'accepted');
      if(period==='calendar_month') assert.match(notice,/start of the next UTC month/);
      else assert.match(notice,/operator needs to change the allowance/);
      assert.doesNotMatch(notice,/Please increase it/);
    } finally {await service.close();}
  });
});

test('prepared unknown-effect notice resumes once after restart without retrying the effect', async () => {
  await withDb(async (journal,path) => {
    const source=admitForRecovery(journal,'notice-crash','Please update my calendar event');
    journal.startRun({runId:'effect-run',eventId:source});
    const actionId='calendar-effect';
    journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,'unknown',?,?,?)")
      .run(actionId,'effect-run','owner_calendar_tool','a'.repeat(64),source,'{}',Date.now(),Date.now());
    let effectCalls=0,noticeContacts=0;
    const ownerCalendar={identity:{line_key:'line'},scopeHash:'a'.repeat(64),
      call:async()=>{effectCalls++;throw Error('effect proof unavailable');},
    } as never;
    const first=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar,
      model:{generate:async()=>{throw Error('model must not run');}},
      canPostPrepared:async()=>false,
      postPrepared:async()=>{noticeContacts++;throw Error('notice contacted too early');}});
    assert.equal((await first.recover())[0]?.status,'held');
    const prepared=journal.getReplyForEvent(source)!;
    assert.equal(prepared.dispatch_status,'prepared');
    assert.match(prepared.payload as string,/couldn’t confirm/);
    assert.equal(noticeContacts,0);
    await first.close();journal.close();
    const reopened=new OperationalJournal(path);reopened.recoverInterrupted();
    const second=new ConversationCoordinator({journal:reopened,ownerPolicy,ownerCalendar,
      model:{generate:async()=> 'Hello.'},
      postPrepared:async post=>{noticeContacts++;if(noticeContacts===1)assert.equal(post.dispatchId,prepared.dispatch_id);
        return {acceptance:'accepted',messageId:'notice-accepted'};}});
    try {
      assert.equal((await second.recover())[0]?.status,'held');
      assert.equal(noticeContacts,1);
      assert.equal(reopened.getAction(actionId)?.status,'unknown');
      assert.equal(reopened.getInbound(source)?.outcome,'pending');
      assert.equal(reopened.getReplyForEvent(source)?.purpose,'outcome_pending');
      await second.recover();
      assert.equal(noticeContacts,1);
      assert.equal(reopened.listRepliesForThread(source).length,1);
      assert.equal(effectCalls,0,'invalid saved proof never reaches provider');
      await second.receive(inbound('safe-after-notice','Hello'),context);
      assert.equal(noticeContacts,2,'later safe chat gets its own reply');
      assert.equal(reopened.getAction(actionId)?.status,'unknown');
      assert.equal(effectCalls,0,'safe chat never replays the held effect');
    } finally {await second.close();reopened.close();}
  });
});

test('contacted unknown-effect notice stays inspect-only after restart', async () => {
  await withDb(async (journal,path) => {
    const source=admitForRecovery(journal,'notice-contacted','Please update my calendar event');
    journal.startRun({runId:'effect-run',eventId:source});
    const actionId='calendar-effect';
    journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,'unknown',?,?,?)")
      .run(actionId,'effect-run','owner_calendar_tool','a'.repeat(64),source,'{}',Date.now(),Date.now());
    const ownerCalendar={identity:{line_key:'line'},scopeHash:'a'.repeat(64),
      call:async()=>{throw Error('effect proof unavailable');}} as never;
    const first=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar,
      model:{generate:async()=>{throw Error('model must not run');}},canPostPrepared:async()=>false,
      postPrepared:async()=>{throw Error('notice must remain uncontacted');}});
    assert.equal((await first.recover())[0]?.status,'held');
    const prepared=journal.getReplyForEvent(source)!;
    journal.beginDispatch(prepared.dispatch_id as string);
    await first.close();journal.close();
    const reopened=new OperationalJournal(path);reopened.recoverInterrupted();
    let sends=0;
    const second=new ConversationCoordinator({journal:reopened,ownerPolicy,ownerCalendar,
      model:{generate:async()=>{throw Error('model must not run');}},
      postPrepared:async()=>{sends++;throw Error('unknown notice resent');}});
    try {
      assert.equal((await second.recover())[0]?.status,'held');
      assert.equal(sends,0);
      assert.equal(reopened.getDispatch(prepared.dispatch_id as string)?.status,'unknown');
      assert.equal(reopened.getAction(actionId)?.status,'unknown');
      assert.equal(reopened.listRepliesForThread(source).length,1);
    } finally {await second.close();reopened.close();}
  });
});

test('unknown action with forged pending source cannot notify or consume unrelated input', async () => {
  await withDb(async journal => {
    const original=admitForRecovery(journal,'source-original','Update my calendar');
    const unrelated=admitForRecovery(journal,'source-unrelated','What remains open?');
    journal.startRun({runId:'effect-run',eventId:original});
    const actionId='forged-calendar-effect';
    journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,'unknown',?,?,?)")
      .run(actionId,'effect-run','owner_calendar_tool','a'.repeat(64),unrelated,'{}',Date.now(),Date.now());
    journal.resolveInbound(original,'resolved');
    let sends=0,models=0,effects=0;
    const ownerCalendar={identity:{line_key:'line'},scopeHash:'a'.repeat(64),
      call:async()=>{effects++;throw Error('forged action reached receiver');}} as never;
    const service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar,
      model:{generate:async()=>{models++;return 'unsafe';}},
      postPrepared:async()=>{sends++;return {acceptance:'accepted',messageId:'unsafe'};}});
    try {
      assert.equal((await service.recover())[0]?.status,'held');
      assert.equal(sends,0);assert.equal(models,0);
      assert.equal(journal.getInbound(unrelated)?.outcome,'pending');
      assert.equal(journal.getInbound(original)?.outcome,'resolved');
      assert.equal(journal.getAction(actionId)?.status,'unknown');
    } finally {await service.close();}
  });
});

test('a plain correction or close during reply contact keeps awaiting the real provider receipt', async () => {
  for (const interruption of ['correction', 'close'] as const) {
    await withDb(async journal => {
      const contacted = deferred<void>();
      const late = deferred<{acceptance: 'accepted'; messageId: string}>();
      const sent: string[] = [];
      const service = coordinator(journal, async request => `answer: ${request.text}`, async request => {
        sent.push(request.text);
        if (sent.length === 1) {contacted.resolve(); return late.promise;}
        return {acceptance: 'accepted', messageId: `m${sent.length}`};
      });
      const first = service.receive(inbound('one', 'first question'), context);
      await contacted.promise;
      const interrupted = interruption === 'correction'
        ? service.receive(inbound('two', 'and another thing'), context) : service.close();
      setTimeout(() => late.resolve({acceptance: 'accepted', messageId: 'm1'}), 5);
      assert.deepEqual(await first, {status: 'accepted', messageId: 'm1'});
      await interrupted;
      const one = admittedId(journal, 'one');
      const reply = journal.listRepliesForThread(one).find(row => row.event_id === one)!;
      assert.equal(journal.getDispatch(reply.dispatch_id as string)?.status, 'accepted');
      assert.equal(journal.getDispatch(reply.dispatch_id as string)?.provider_message_id, 'm1');
      assert.equal(journal.getInbound(one)?.outcome, 'resolved');
      if (interruption === 'correction') {
        assert.deepEqual(sent, ['answer: first question', 'answer: and another thing']);
        assert.equal(journal.listPendingInbound().length, 0);
      }
      await service.close();
    });
  }
});

test('a definitely undelivered control acknowledgement is replaced once, never re-rotates, then resolves', async () => {
  for (const [command, ack] of [['/new', 'I started a new conversation.'],
    ['/stop', 'I stopped working on your earlier request. An action already sent may still have happened; please check before retrying.']] as const) {
    for (const outcome of ['rejected', 'not_attempted'] as const) {
      for (const recovers of [true, false]) {
        await withDb(async (journal, path) => {
          let rotations = 0; let models = 0; const sent: string[] = [];
          const make = (current: OperationalJournal) => new ConversationCoordinator({journal: current, ownerPolicy,
            model: {generate: async () => {models++; return 'wrong';}}, rotateSession: async () => {rotations++;},
            postPrepared: async request => {
              sent.push(request.text);
              if (request.text === ack && (sent.length === 1 || !recovers)) return {acceptance: outcome};
              return {acceptance: 'accepted', messageId: `m${sent.length}`};
            }});
          let service = make(journal);
          await service.receive(inbound('command', command), context);
          await service.drain();
          const id = admittedId(journal, 'command');
          assert.equal(journal.getInbound(id)?.outcome, 'pending');
          let current = journal;
          for (let i = 0; i < 3; i++) {
            await service.close(); current.close();
            current = new OperationalJournal(path); current.recoverInterrupted();
            service = make(current); await service.recover();
          }
          try {
            assert.equal(rotations, command === '/new' ? 1 : 0, 'a recorded control acknowledgement proves the rotation ran');
            assert.equal(models, 0);
            assert.deepEqual(sent, [ack, ack], 'one fresh dispatch after definite non-delivery, then no more');
            assert.equal(current.getInbound(id)?.outcome, 'resolved');
            const replies = current.listRepliesForThread(id).filter(row => row.purpose === 'owner_control');
            assert.deepEqual(replies.map(row => row.dispatch_status), recovers ? [outcome === 'not_attempted' ? 'rejected' : outcome, 'accepted']
              : ['rejected', 'rejected']);
          } finally {await service.close(); current.close();}
        });
      }
    }
  }
});

test('model failure is delivered without completion feedback and next success still gets feedback',async()=>{
 await withDb(async journal=>{
  const feedback:string[]=[];let calls=0;
  const coordinator=new ConversationCoordinator({journal,ownerPolicy,
   model:{generate:async()=>{if(calls++===0)throw new Error('private model failure');return 'Here is the answer.';}},
   onOwnerReplyAccepted:message=>{feedback.push(message.eventId);},
   postPrepared:async()=>({acceptance:'accepted',messageId:'accepted-'+calls})});
  try{
   assert.equal((await coordinator.receive(inbound('failed-turn','Hello'),context)).status,'accepted');
   assert.equal(journal.db.prepare('SELECT purpose FROM prepared_replies').get()!.purpose,'model_failure');
   assert.deepEqual(feedback,[]);
   assert.equal((await coordinator.receive(inbound('good-turn','Try again'),context)).status,'accepted');
   assert.deepEqual(feedback,['good-turn']);
  }finally{await coordinator.close();}
 });
});
