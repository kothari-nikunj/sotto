import assert from 'node:assert/strict';
import test from 'node:test';
import { createiMessageAdapter, type iMessageAdapter } from '@photon-ai/chat-adapter-imessage';
import { PhotonOwnerFeedback, photonFeedbackProvider, processingTapback, TurnPresence, photonPresenceProvider, telegramPresenceProvider, type PresenceClock } from '../src/channels/presence.js';

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
async function flush(): Promise<void> { for (let i = 0; i < 20; i++) await Promise.resolve(); }

class ManualClock implements PresenceClock {
  now = 0;
  #nextId = 0;
  #timers = new Map<number, { due: number; callback: () => void }>();
  setTimeout(callback: () => void, delayMs: number) {
    const id = ++this.#nextId;
    this.#timers.set(id, { due: this.now + delayMs, callback });
    return { id, unref() {} };
  }
  clearTimeout(timer: { unref?(): void }): void { this.#timers.delete((timer as { id: number }).id); }
  async advance(ms: number): Promise<void> {
    const end = this.now + ms;
    while (true) {
      const next = [...this.#timers].filter(([, task]) => task.due <= end)
        .sort((a, b) => a[1].due - b[1].due || a[0] - b[0])[0];
      if (!next) break;
      this.now = next[1].due;
      this.#timers.delete(next[0]);
      next[1].callback();
      await flush();
    }
    this.now = end;
    await flush();
  }
}

test('production defaults wait exactly 2s before the first pulse and refresh every 2s', async () => {
  const clock = new ManualClock();
  const events: string[] = [];
  const presence = new TurnPresence({ provider: {
    async start() { events.push('start'); return true; },
    async stop() { events.push('stop'); },
  }, clock });
  const stopFast = presence.startTurnPresence('thread', 'fast', new AbortController().signal, () => true);
  await clock.advance(1_999);
  assert.deepEqual(events, []);
  stopFast();
  await clock.advance(1);
  assert.deepEqual(events, [], 'completed fast turn never flashes');
  const stopLong = presence.startTurnPresence('thread', 'long', new AbortController().signal, () => true);
  await clock.advance(1_999);
  assert.deepEqual(events, []);
  await clock.advance(1);
  assert.deepEqual(events, ['start']);
  await clock.advance(1_999);
  assert.deepEqual(events, ['start']);
  await clock.advance(1);
  assert.deepEqual(events, ['start', 'start']);
  stopLong();
  await flush();
  assert.deepEqual(events, ['start', 'start', 'stop']);
  await clock.advance(4_000);
  assert.equal(events.length, 3);
});

test('cancelled in-flight start is stopped before replacement may start', async () => {
  const clock = new ManualClock();
  const first = deferred<boolean>();
  const events: string[] = [];
  const presence = new TurnPresence({ provider: {
    async start() { events.push('start'); return events.filter(value => value === 'start').length === 1 ? first.promise : true; },
    async stop() { events.push('stop'); },
  }, clock });
  const abort = new AbortController();
  const stopOld = presence.startTurnPresence('thread', 'old', abort.signal, () => !abort.signal.aborted);
  await clock.advance(2_000);
  assert.deepEqual(events, ['start']);
  abort.abort();
  stopOld();
  const stopNew = presence.startTurnPresence('thread', 'new', new AbortController().signal, () => true);
  await clock.advance(500);
  assert.deepEqual(events, ['start']);
  first.resolve(true);
  await flush();
  await clock.advance(1_500);
  assert.deepEqual(events.slice(0, 3), ['start', 'stop', 'start']);
  stopNew();
});

test('hung start poisons the thread; late success is compensated without delaying cleanup', async () => {
  const clock = new ManualClock();
  const late = deferred<boolean>();
  const events: string[] = [];
  const presence = new TurnPresence({ provider: {
    async start() { events.push('start'); return late.promise; },
    async stop() { events.push('stop'); },
  }, clock });
  const stop = presence.startTurnPresence('thread', 'old', new AbortController().signal, () => true);
  await clock.advance(2_000);
  await clock.advance(1_000);
  stop();
  const replacement = presence.startTurnPresence('thread', 'new', new AbortController().signal, () => true);
  await clock.advance(4_000);
  assert.deepEqual(events, ['start'], 'poisoned thread cannot start replacement typing');
  late.resolve(true);
  await flush();
  assert.deepEqual(events, ['start', 'stop']);
  replacement();
});

test('Photon decodes public thread ID and fences typing after delayed public space lookup', async () => {
  const resolved = deferred<{ startTyping(): Promise<void>; stopTyping(): Promise<void> }>();
  const calls: string[] = [];
  const adapter = {
    app: {} as NonNullable<iMessageAdapter['app']>,
    decodeThreadId: (threadId: string) => {
      assert.equal(threadId, 'imessage:iMessage;-;+15551234567~line-A');
      return { chatGuid: 'iMessage;-;+15551234567', phone: 'line-A' };
    },
  };
  const provider = photonPresenceProvider(adapter, { resolveSpace: async (_app, guid, phone) => {
    calls.push(`lookup:${guid}:${phone}`);
    return resolved.promise;
  } });
  let current = true;
  const start = provider.start('imessage:iMessage;-;+15551234567~line-A', () => current);
  current = false;
  resolved.resolve({ async startTyping() { calls.push('start'); }, async stopTyping() { calls.push('stop'); } });
  assert.equal(await start, false);
  assert.deepEqual(calls, ['lookup:iMessage;-;+15551234567:line-A']);
});

test('Photon uses the resolved public Space for both start and stop on the explicit line', async () => {
  const calls: string[] = [];
  const provider = photonPresenceProvider({
    app: {} as NonNullable<iMessageAdapter['app']>,
    decodeThreadId: () => ({ chatGuid: 'iMessage;-;+owner', phone: 'line-B' }),
  }, { resolveSpace: async (_app, guid, phone) => {
    calls.push(`lookup:${guid}:${phone}`);
    return { async startTyping() { calls.push('start'); }, async stopTyping() { calls.push('stop'); } };
  } });
  assert.equal(await provider.start('imessage:iMessage;-;+owner~line-B', () => true), true);
  await provider.stop?.('imessage:iMessage;-;+owner~line-B');
  assert.deepEqual(calls, ['lookup:iMessage;-;+owner:line-B', 'start', 'stop']);
});

test('Photon rejected/unknown start retains resolved space and attempts stop', async () => {
  const events: string[] = [];
  const provider = photonPresenceProvider({
    app: {} as NonNullable<iMessageAdapter['app']>,
    decodeThreadId: () => ({ chatGuid: 'iMessage;-;+owner' }),
  }, { resolveSpace: async () => ({
    async startTyping() { events.push('start'); throw new Error('provider may have accepted typing'); },
    async stopTyping() { events.push('stop'); },
  }) });
  const clock = new ManualClock();
  const presence = new TurnPresence({ provider, clock });
  const stop = presence.startTurnPresence('imessage:iMessage;-;+owner', 'run', new AbortController().signal, () => true);
  await clock.advance(2_000);
  assert.deepEqual(events, ['start', 'stop']);
  await clock.advance(4_000);
  assert.deepEqual(events, ['start', 'stop'], 'failed start poisons this thread, with no refresh');
  stop();
});

test('Telegram provider invokes only public startTyping and has no fabricated stop', async () => {
  const threads: string[] = [];
  const provider = telegramPresenceProvider({ startTyping: async thread => { threads.push(thread); } });
  assert.equal(await provider.start('telegram:owner', () => true), true);
  assert.deepEqual(threads, ['telegram:owner']);
  assert.equal(provider.stop, undefined);
});


test('close fences pending and future pulses and joins a started turn stop', async () => {
  const clock = new ManualClock();
  const stopped = deferred<void>();
  const events: string[] = [];
  const presence = new TurnPresence({ clock, provider: {
    async start() { events.push('start'); return true; },
    async stop() { events.push('stop'); await stopped.promise; },
  } });
  const stop = presence.startTurnPresence('thread', 'active', new AbortController().signal, () => true);
  await clock.advance(2_000);
  presence.startTurnPresence('pending', 'pending', new AbortController().signal, () => true);
  let closed = false;
  const closing = presence.close();
  assert.equal(presence.close(), closing);
  void closing.then(() => { closed = true; });
  presence.startTurnPresence('later', 'late', new AbortController().signal, () => true);
  await flush();
  assert.deepEqual(events, ['start', 'stop']);
  assert.equal(closed, false);
  stopped.resolve();
  await closing;
  stop();
  await clock.advance(4_000);
  assert.equal(closed, true);
  assert.deepEqual(events, ['start', 'stop']);
});

test('close waits only for bounded start/stop work; late start still compensates', async () => {
  const clock = new ManualClock();
  const late = deferred<boolean>();
  const events: string[] = [];
  const presence = new TurnPresence({ clock, provider: {
    async start() { events.push('start'); return late.promise; },
    async stop() { events.push('stop'); },
  } });
  presence.startTurnPresence('thread', 'active', new AbortController().signal, () => true);
  await clock.advance(2_000);
  let closed = false;
  const closing = presence.close().then(() => { closed = true; });
  await clock.advance(999);
  assert.equal(closed, false);
  await clock.advance(1);
  await closing;
  late.resolve(true);
  await flush();
  assert.deepEqual(events, ['start', 'stop']);
  presence.startTurnPresence('thread', 'late', new AbortController().signal, () => true);
  await clock.advance(4_000);
  assert.deepEqual(events, ['start', 'stop']);
});


test('close bounds a hung stop and never resumes typing afterward', async () => {
  const clock = new ManualClock();
  const events: string[] = [];
  const presence = new TurnPresence({ clock, provider: {
    async start() { events.push('start'); return true; },
    async stop() { events.push('stop'); await new Promise<void>(() => {}); },
  } });
  presence.startTurnPresence('thread', 'active', new AbortController().signal, () => true);
  await clock.advance(2_000);
  let closed = false;
  const closing = presence.close().then(() => { closed = true; });
  await flush();
  await clock.advance(999);
  assert.equal(closed, false);
  await clock.advance(1);
  await closing;
  presence.startTurnPresence('thread', 'late', new AbortController().signal, () => true);
  await clock.advance(4_000);
  assert.deepEqual(events, ['start', 'stop']);
});


test('fresh Photon feedback uses pinned adapter read and direct reaction methods', async () => {
  const calls: string[] = [];
  const adapter = createiMessageAdapter({projectId: 'synthetic', projectSecret: 'synthetic'});
  const thread = adapter.encodeThreadId({chatGuid: 'any;-;+15550001111', phone: 'shared'});
  const target = {id: 'owner-message', content: {type: 'text', text: 'draft a meeting invite'},
    direction: 'inbound', timestamp: new Date(), sender: {id: '+15550001111'},
    space: {id: 'any;-;+15550001111', phone: 'shared'},
    read: async () => {calls.push('read');}, react: async (emoji: string) => {calls.push(emoji);}};
  // Replace only provider lookup; the installed adapter's markRead/addReaction run unchanged.
  const lookup = adapter as unknown as {resolveMessage(threadId: string, messageId: string): Promise<typeof target>};
  lookup.resolveMessage = async (threadId, messageId) => {
    assert.equal(threadId, thread); assert.equal(messageId, 'owner-message'); return target;
  };
  const feedback = new PhotonOwnerFeedback(photonFeedbackProvider(adapter));
  assert.deepEqual(feedback.snapshot(), {available: false});
  feedback.admitted({eventId: 'owner-message', text: 'draft a meeting invite'}, thread, () => true);
  feedback.accepted({eventId: 'owner-message'}, thread);
  await flush();
  await new Promise<void>(resolve => setImmediate(resolve));
  assert.deepEqual(calls, ['read', '📝', '✅']);
  const snapshot = feedback.snapshot();
  assert.equal(snapshot.available, true);
  if (snapshot.available) {
    assert.equal(snapshot.reply_accepted, true); assert.equal(snapshot.work_accepted, false); assert.equal(snapshot.uncertain, false);
    assert.deepEqual(snapshot.stages.map(stage => [stage.stage, stage.outcome, stage.step]),
      [['read', 'success', 'effect'], ['processing', 'success', 'effect'], ['completion', 'success', 'effect']]);
    assert.ok(snapshot.stages.every(stage => Number.isInteger(stage.lookup_ms) && Number.isInteger(stage.effect_ms)));
    snapshot.stages[0]!.outcome = 'error';
    const copied = feedback.snapshot();
    assert.ok(copied.available && copied.stages[0]!.outcome === 'success');
    assert.doesNotMatch(JSON.stringify(copied), /owner-message|15550001111|draft a meeting invite|📝|✅/);
  }
  await feedback.close();
  assert.equal(processingTapback('Thank you! ❤️'), '❤️');
  assert.equal(processingTapback('what do you know about Alex'), '🔎');
});

test('exact owner controls keep read feedback but skip thinking and complete only after accepted reply', async () => {
  for (const command of ['/stop', '/new']) {
    const calls: string[] = [];
    const feedback = new PhotonOwnerFeedback({
      markRead: async () => {calls.push('read');},
      addReaction: async (_thread, _id, emoji) => {calls.push(emoji);},
    });
    feedback.admitted({eventId: command, text: ` ${command.toUpperCase()} `}, 'thread', () => true);
    await flush();
    assert.deepEqual(calls, ['read']);
    feedback.accepted({eventId: command}, 'thread');
    await flush();
    assert.deepEqual(calls, ['read', '✅']);
    const snapshot = feedback.snapshot();
    assert.ok(snapshot.available);
    assert.equal(snapshot.stages.find(stage => stage.stage === 'processing')?.outcome, 'skipped');
    await feedback.close();
  }
});

test('Photon feedback fences superseded input, preserves thanks heart, and does not retry uncertainty', async () => {
  const calls: string[] = [];
  const pending = deferred<void>();
  const feedback = new PhotonOwnerFeedback({
    markRead: async (_thread, id) => {calls.push('read:' + id); if (id === 'old') await pending.promise;},
    addReaction: async (_thread, id, emoji) => {calls.push(id + ':' + emoji);},
  }, 10);
  feedback.admitted({eventId: 'old', text: 'hello'}, 'thread', () => true);
  await flush();
  feedback.admitted({eventId: 'new', text: 'thanks'}, 'thread', () => true);
  feedback.accepted({eventId: 'old'}, 'thread');
  pending.resolve();
  await flush();
  feedback.accepted({eventId: 'new'}, 'thread');
  await flush();
  assert.deepEqual(calls, ['read:old', 'read:new', 'new:❤️']);
  await feedback.close();
  const uncertain: string[] = [];
  const failed = new PhotonOwnerFeedback({markRead: async () => {uncertain.push('read'); throw new Error('private error');},
    addReaction: async () => {uncertain.push('reaction');}});
  failed.admitted({eventId: 'one'}, 'thread', () => true);
  failed.accepted({eventId: 'one'}, 'thread');
  await flush();
  failed.admitted({eventId: 'two'}, 'thread', () => true);
  await flush();
  assert.deepEqual(uncertain, ['read', 'read']);
  await failed.close();
});


test('delayed Photon target lookup cannot begin a read or tapback after supersession or close', async () => {
  for (const reason of ['superseded', 'closed', 'timed-out']) {
    const resolved = deferred<void>();
    const calls: string[] = [];
    const adapter = createiMessageAdapter({projectId: 'synthetic', projectSecret: 'synthetic'});
    const thread = adapter.encodeThreadId({chatGuid: 'any;-;+15550001111', phone: 'shared'});
    const target = {id: 'old', content: {type: 'text', text: 'hello'}, direction: 'inbound',
      timestamp: new Date(), sender: {id: '+15550001111'}, space: {id: 'any;-;+15550001111', phone: 'shared'},
      read: async () => {calls.push('read');}, react: async () => {calls.push('react');}};
    const lookup = adapter as unknown as {resolveMessage(): Promise<typeof target>};
    lookup.resolveMessage = async () => {await resolved.promise; return target;};
    const feedback = new PhotonOwnerFeedback(photonFeedbackProvider(adapter), 5);
    feedback.admitted({eventId: 'old'}, thread, () => true);
    await flush();
    if (reason === 'superseded') feedback.admitted({eventId: 'new'}, thread, () => false);
    else if (reason === 'closed') await feedback.close();
    else await new Promise(resolve => setTimeout(resolve, 10));
    resolved.resolve();
    await new Promise<void>(resolve => setImmediate(resolve));
    assert.deepEqual(calls, [], reason);
    await feedback.close();
  }
});

test('fresh owner feedback resumes after a prior turn failure without retrying the old event', async () => {
  const calls: string[] = [];
  const feedback = new PhotonOwnerFeedback({
    markRead: async (_thread, id) => {
      calls.push('read:' + id);
      if (id === 'failed') throw new Error('synthetic provider failure');
    },
    addReaction: async (_thread, id, emoji) => {calls.push(id + ':' + emoji);},
  }, 5);
  try {
    feedback.admitted({eventId: 'failed', text: 'hello'}, 'thread', () => true);
    await flush();
    await new Promise<void>(resolve => setImmediate(resolve));
    feedback.accepted({eventId: 'failed'}, 'thread');
    feedback.admitted({eventId: 'fresh', text: 'hello'}, 'thread', () => true);
    feedback.accepted({eventId: 'fresh'}, 'thread');
    await flush();
    await new Promise<void>(resolve => setImmediate(resolve));
    assert.deepEqual(calls, ['read:failed', 'read:fresh', 'fresh:💭', 'fresh:✅']);
  } finally {await feedback.close();}
});

for (const delayed of ['lookup', 'read', 'reaction'] as const) test(`late old Photon ${delayed} does not poison fresh feedback or repeat old effects`, async () => {
  const pending = deferred<void>();
  const calls: string[] = [];
  const feedback = new PhotonOwnerFeedback({
    markRead: async (_thread, id, current) => {
      if (id === 'old' && delayed === 'lookup') {await pending.promise;if (!current()) return;}
      calls.push('read:' + id);
      if (id === 'old' && delayed === 'read') {await pending.promise;throw new Error('late synthetic read failure');}
    },
    addReaction: async (_thread, id, emoji) => {
      calls.push(id + ':' + emoji);
      if (id === 'old' && delayed === 'reaction') {await pending.promise;throw new Error('late synthetic reaction failure');}
    },
  }, 5);
  try {
    feedback.admitted({eventId: 'old', text: 'hello'}, 'thread', () => true);
    feedback.accepted({eventId: 'old'}, 'thread');
    await new Promise(resolve => setTimeout(resolve, 15));
    feedback.admitted({eventId: 'fresh', text: 'hello'}, 'thread', () => true);
    feedback.accepted({eventId: 'fresh'}, 'thread');
    await flush();
    await new Promise<void>(resolve => setImmediate(resolve));
    assert.deepEqual(calls.filter(call => call.includes('fresh')), ['read:fresh', 'fresh:💭', 'fresh:✅']);
    const beforeLate = feedback.snapshot();
    pending.resolve();await flush();
    const afterLate = feedback.snapshot();
    assert.ok(beforeLate.available && afterLate.available);
    assert.equal(afterLate.turn_sequence, 2); assert.equal(afterLate.uncertain, false);
    assert.deepEqual(afterLate.stages, beforeLate.stages, 'old settlement cannot alter fresh diagnostics');
    assert.ok(!calls.includes('old:✅'));
    assert.equal(calls.filter(call => call === 'read:old').length, delayed === 'lookup' ? 0 : 1);
    assert.equal(calls.filter(call => call === 'old:💭').length, delayed === 'reaction' ? 1 : 0);
  } finally {pending.resolve();await feedback.close();}
});


for (const step of ['lookup', 'effect', 'invalid_target', 'provider_unavailable'] as const) test(`Photon feedback diagnostics classify ${step} without retaining provider details`, async () => {
 const privateText='private-token https://private.example/message secret-stack';
 let reads=0,reactions=0,lookups=0;
 const adapter={fetchMessage:async()=>{
  lookups++;
  if(step==='lookup')throw Object.assign(new Error(privateText),{name:privateText});
  if(step==='provider_unavailable')return {id:'owner',threadId:'thread',author:{isMe:false},raw:{}};
  return {id:step==='invalid_target'?'foreign':'owner',threadId:'thread',author:{isMe:false},raw:{
   read:async()=>{reads++;throw new Error(privateText);},react:async()=>{reactions++;}}};
 }};
 const feedback=new PhotonOwnerFeedback(photonFeedbackProvider(adapter as never));
 feedback.admitted({eventId:'owner',text:privateText},'thread',()=>true);
 feedback.accepted({eventId:'owner'},'thread');
 await new Promise<void>(resolve=>setImmediate(resolve));
 await feedback.close();
 const snapshot=feedback.snapshot();assert.ok(snapshot.available);
 assert.equal(snapshot.reply_accepted,true);assert.equal(snapshot.uncertain,true);
 assert.equal(snapshot.stages[0]!.outcome,'error');
 assert.equal(snapshot.stages[0]!.step,step==='effect'?'effect':'lookup');
 assert.equal(snapshot.stages[0]!.error_class,step==='invalid_target'?'target_unavailable':step==='provider_unavailable'?'provider_unavailable':'provider_error');
 assert.equal(lookups,1);assert.equal(reads,step==='effect'?1:0);assert.equal(reactions,0);
 assert.doesNotMatch(JSON.stringify(snapshot),/private-token|private.example|secret-stack|owner|thread/);
});

test('Photon diagnostic timeout remains captured after late lookup and fresh success; restart has no snapshot', async () => {
 const old=deferred<void>();const calls:string[]=[];
 const adapter={fetchMessage:async(thread:string,id:string)=>{
  calls.push('lookup:'+id);if(id==='old')await old.promise;
  return {id,threadId:thread,author:{isMe:false},raw:{read:async()=>{calls.push('read:'+id);},react:async()=>{calls.push('react:'+id);}}};
 }};
 const feedback=new PhotonOwnerFeedback(photonFeedbackProvider(adapter as never),5);
 feedback.admitted({eventId:'old'},'thread',()=>true);feedback.accepted({eventId:'old'},'thread');
 await new Promise(resolve=>setTimeout(resolve,15));
 const timedOut=feedback.snapshot();assert.ok(timedOut.available);assert.equal(timedOut.stages[0]!.outcome,'timeout');
 assert.equal(timedOut.stages[0]!.step,'lookup');assert.equal(timedOut.uncertain,true);
 old.resolve();await new Promise<void>(resolve=>setImmediate(resolve));
 const late=feedback.snapshot();assert.ok(late.available);assert.equal(late.stages[0]!.outcome,'timeout');assert.equal(late.stages[0]!.late_outcome,'success');
 assert.equal(late.stages[0]!.elapsed_ms,timedOut.stages[0]!.elapsed_ms);assert.deepEqual(calls,['lookup:old']);
 feedback.admitted({eventId:'fresh'},'thread',()=>true);feedback.accepted({eventId:'fresh'},'thread');
 await new Promise<void>(resolve=>setImmediate(resolve));
 const fresh=feedback.snapshot();assert.ok(fresh.available);assert.equal(fresh.turn_sequence,2);assert.equal(fresh.uncertain,false);
 assert.ok(fresh.stages.every(stage=>stage.outcome==='success' && stage.late_outcome===null));
 assert.deepEqual(calls,['lookup:old','lookup:fresh','read:fresh','lookup:fresh','react:fresh','lookup:fresh','react:fresh']);
 await feedback.close();
 const restarted=new PhotonOwnerFeedback(photonFeedbackProvider(adapter as never));assert.deepEqual(restarted.snapshot(),{available:false});await restarted.close();
});


test('lookup completed after close reports skipped effect, not successful read', async () => {
 const pending=deferred<void>();let effects=0;
 const provider=photonFeedbackProvider({fetchMessage:async()=>{await pending.promise;return {id:'owner',threadId:'thread',author:{isMe:false},raw:{read:async()=>{effects++;},react:async()=>{effects++;}}};}} as never);
 const feedback=new PhotonOwnerFeedback(provider);
 feedback.admitted({eventId:'owner'},'thread',()=>true);await flush();
 const closing=feedback.close();pending.resolve();await closing;
 const snapshot=feedback.snapshot();assert.ok(snapshot.available);
 assert.equal(snapshot.stages[0]!.outcome,'skipped');assert.equal(snapshot.stages[0]!.step,'lookup');
 assert.equal(snapshot.stages[0]!.effect_ms,null);assert.equal(effects,0);
});

for (const acceptance of ['before', 'after', 'heart'] as const) {
 test(`confirmed original late read resumes remaining feedback once: ${acceptance}`, async () => {
  const read = deferred<void>(); const calls: string[] = [];
  const provider = photonFeedbackProvider({fetchMessage: async (threadId: string, id: string) => ({
   id, threadId, author: {isMe: false}, raw: {
    read: async () => {calls.push('read'); await read.promise;},
    react: async (emoji: string) => {calls.push(emoji);},
   },
  })} as never);
  const feedback = new PhotonOwnerFeedback(provider, 5);
  feedback.admitted({eventId: 'owner', text: acceptance === 'heart' ? 'thanks' : 'hello'}, 'thread', () => true);
  if (acceptance !== 'after') feedback.accepted({eventId: 'owner'}, 'thread');
  await new Promise(resolve => setTimeout(resolve, 15));
  assert.deepEqual(calls, ['read']);
  read.resolve(); await new Promise<void>(resolve => setImmediate(resolve));
  feedback.accepted({eventId: 'owner'}, 'thread');
  feedback.accepted({eventId: 'owner'}, 'thread');
  await new Promise<void>(resolve => setImmediate(resolve));
  assert.deepEqual(calls, acceptance === 'heart' ? ['read', '❤️'] : ['read', '💭', '✅']);
  const snapshot = feedback.snapshot(); assert.ok(snapshot.available);
  assert.equal(snapshot.uncertain, false); assert.equal(snapshot.stages[0]!.outcome, 'timeout');
  assert.equal(snapshot.stages[0]!.late_outcome, 'success'); assert.equal(snapshot.stages[0]!.step, 'effect');
  await feedback.close();
 });
}

for (const boundary of ['superseded', 'closed', 'rejected'] as const) {
 test(`late original read does not resume after ${boundary}`, async () => {
  const read = deferred<void>(); const calls: string[] = [];
  const provider = photonFeedbackProvider({fetchMessage: async (threadId: string, id: string) => ({
   id, threadId, author: {isMe: false}, raw: {
    read: async () => {calls.push('read:' + id); if (id === 'old') {await read.promise; if (boundary === 'rejected') throw Error('private');}},
    react: async (emoji: string) => {calls.push(id + ':' + emoji);},
   },
  })} as never);
  const feedback = new PhotonOwnerFeedback(provider, 5);
  feedback.admitted({eventId: 'old'}, 'thread', () => true); feedback.accepted({eventId: 'old'}, 'thread');
  await new Promise(resolve => setTimeout(resolve, 15));
  if (boundary === 'closed') await feedback.close();
  if (boundary === 'superseded') {
   feedback.admitted({eventId: 'fresh'}, 'thread', () => true); feedback.accepted({eventId: 'fresh'}, 'thread');
  }
  read.resolve(); await new Promise<void>(resolve => setImmediate(resolve));
  assert.deepEqual(calls, boundary === 'superseded' ? ['read:old', 'read:fresh', 'fresh:💭', 'fresh:✅'] : ['read:old']);
  await feedback.close();
 });
}

for (const settlement of ['success', 'rejected'] as const) {
 test(`late processing ${settlement} never replays and only confirmed success starts completion`, async () => {
 const reaction = deferred<void>(); const calls: string[] = [];
 const feedback = new PhotonOwnerFeedback(photonFeedbackProvider({fetchMessage: async (threadId: string, id: string) => ({
  id, threadId, author: {isMe: false}, raw: {
   read: async () => {calls.push('read');}, react: async (emoji: string) => {calls.push(emoji); if (emoji !== '✅') {await reaction.promise; if (settlement === 'rejected') throw Error('private');}},
  },
 })} as never), 5);
 feedback.admitted({eventId: 'owner'}, 'thread', () => true); feedback.accepted({eventId: 'owner'}, 'thread');
 await new Promise(resolve => setTimeout(resolve, 15)); reaction.resolve();
 await new Promise<void>(resolve => setImmediate(resolve)); feedback.accepted({eventId: 'owner'}, 'thread');
 await new Promise<void>(resolve => setImmediate(resolve));
 assert.deepEqual(calls, settlement === 'success' ? ['read', '💭', '✅'] : ['read', '💭']); const snapshot = feedback.snapshot(); assert.ok(snapshot.available);
 assert.equal(snapshot.uncertain, settlement === 'rejected'); assert.equal(snapshot.stages[1]!.outcome, 'timeout');
 assert.equal(snapshot.stages[1]!.late_outcome, settlement === 'success' ? 'success' : 'error'); await feedback.close();
 });
}

test('silent accepted work completes feedback once without claiming a reply or retrying uncertain effects', async () => {
 const calls:string[]=[];
 const feedback=new PhotonOwnerFeedback({markRead:async()=>{calls.push('read');},addReaction:async(_thread,_message,emoji)=>{calls.push(emoji);}});
 feedback.admitted({eventId:'silent',text:'Check for anything urgent'},'thread',()=>true);
 feedback.workAccepted({eventId:'silent'},'thread');feedback.workAccepted({eventId:'silent'},'thread');
 await flush();await new Promise<void>(resolve=>setImmediate(resolve));
 assert.deepEqual(calls,['read','💭','✅']);const snapshot=feedback.snapshot();assert.ok(snapshot.available);if(snapshot.available){assert.equal(snapshot.reply_accepted,false);assert.equal(snapshot.work_accepted,true);}
 feedback.workAccepted({eventId:'older'},'thread');await flush();assert.equal(calls.length,3);await feedback.close();
 const uncertainCalls:string[]=[];
 const uncertain=new PhotonOwnerFeedback({markRead:async()=>{uncertainCalls.push('read');throw Error('synthetic failure');},addReaction:async()=>{uncertainCalls.push('reaction');}});
 uncertain.admitted({eventId:'unknown'},'thread',()=>true);await flush();uncertain.workAccepted({eventId:'unknown'},'thread');await flush();assert.deepEqual(uncertainCalls,['read']);await uncertain.close();
});
