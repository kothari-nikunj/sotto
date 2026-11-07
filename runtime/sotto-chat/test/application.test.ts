import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { startApplication, projectChatTiming, type ApplicationAgent, type ApplicationOptions } from '../src/application.js';
import { claimRuntimeDirectory } from '../src/ownership.js';
import type { ModelRequest } from '../src/conversation.js';
import { OperationalJournal } from '../src/journal/index.js';
import {PhotonOwnerFeedback} from '../src/channels/presence.js';
import { routeDigest } from '../src/route-proof.js';

test('managed Telegram route proof uses the configured tenant', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-telegram-tenant-'));
  const options: ApplicationOptions = { directory, mediaRoot: directory,
    bearerToken: 'fixture-only-bearer-for-local-tests-000',
    owner: { channel: 'telegram', accountId: 'bot', ownerId: '42' },
    lineKey: 'bot', target: 'owner', providerThreadId: 'telegram:42',
    configuredChannelIdentity: { version: 1, credential_proof: 'a'.repeat(64), identity: {
      channel: 'telegram', tenant_id: 'managed-tenant', account_id: 'bot',
      owner_id: '42', line_key: 'bot', target: 'owner',
      route: { mode: 'fixed', provider_thread_id: 'telegram:42' },
    } },
  };
  const app = await startApplication(options, { agent: {
    generateChat: async () => ({ text: '' }), generateUnattended: async () => ({ text: '' }),
    rotateThread: async () => {}, purgeThreadsBefore: async () => 0,
  }, transport: { send: async () => ({ acceptance: 'accepted', providerMessageIds: ['p1'] }) } });
  try {
    const response = await fetch(`http://127.0.0.1:${app.port}/v1/send`, { method: 'POST',
      headers: { authorization: `Bearer ${options.bearerToken}`, 'content-type': 'application/json' },
      body: JSON.stringify({ dispatch_id: 'tenant-proof', target: 'owner', text: 'hello', attempt: 1 }) });
    assert.equal(response.status, 200);
    const receipt = await response.json() as { route_digest?: string };
    assert.equal(receipt.route_digest, routeDigest({ channel: 'telegram', tenantId: 'managed-tenant',
      projectFingerprint: '', ownerId: '42', accountId: 'bot', lineKey: 'bot' }, 'telegram:42'));
  } finally { await app.close(); await rm(directory, { recursive: true, force: true }); }
});

test('bearer health exposes only a bounded latest chat timing snapshot', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-timing-health-'));
  const bearerToken = 'fixture-only-bearer-for-local-tests-000';
  let timing: unknown = undefined;
  let completedAtMs: unknown = undefined;
  let outcome: unknown = 'failed';
  const app = await startApplication({directory, mediaRoot: directory, bearerToken,
    owner: {channel: 'telegram', accountId: 'bot', ownerId: '42'}, lineKey: 'bot',
    target: 'owner', providerThreadId: 'telegram:42'}, {
    agent: {generateChat: async () => ({text: ''}), generateUnattended: async () => ({text: ''}),
      rotateThread: async () => {}, purgeThreadsBefore: async () => 0},
    transport: {send: async () => ({acceptance: 'accepted', providerMessageIds: ['p1']})},
    chatTiming: () => timing,
    chatTimingCompletedAtMs: () => completedAtMs,
    chatTimingOutcome: () => outcome,
  });
  const url = `http://127.0.0.1:${app.port}/v1/health`;
  const health = async () => (await fetch(url, {headers: {authorization: `Bearer ${bearerToken}`}})).json() as Promise<Record<string, unknown>>;
  try {
    assert.equal((await fetch(url)).status, 401);
    assert.equal((await health()).chatTiming, undefined);
    const validTiming = {sourceCheckMs: 10, standingContextMs: 20, toolSetupMs: 30,
      generationMs: 40, finalSourceCheckMs: 50, totalMs: 150};
    timing = validTiming;
    completedAtMs = 1791450000000;
    assert.deepEqual((await health()).chatTiming, validTiming);
    assert.equal((await health()).chatTimingCompletedAtMs, completedAtMs);
    assert.equal((await health()).chatTimingOutcome, 'failed');
    for (const extra of [{queueMs: 300000}, {promptPreparationMs: 0}, {queueMs: 25, promptPreparationMs: 60}]) {
      timing = {...validTiming, ...extra};
      assert.deepEqual((await health()).chatTiming, timing);
      assert.deepEqual(projectChatTiming(timing), timing);
    }
    outcome = 'private error';
    assert.equal((await health()).chatTimingOutcome, undefined);
    for (const invalid of [{...validTiming, privatePrompt: 'secret'}, {...validTiming, totalMs: -1},
      {...validTiming, totalMs: 300001}, {...validTiming, totalMs: NaN}, {...validTiming, totalMs: '150'},
      ...['queueMs', 'promptPreparationMs'].flatMap(key => [-1, 300001, NaN, Infinity, 1.5, 'PRIVATE', true, null, undefined]
        .map(value => ({...validTiming, [key]: value}))),
      {queueMs: 10, promptPreparationMs: 20}, {...validTiming, prompt: 'PRIVATE', queueMs: 1}]) {
      timing = invalid;
      assert.equal((await health()).chatTiming, undefined);
      assert.equal(projectChatTiming(invalid), undefined);
    }
    timing = validTiming;
    completedAtMs = '1791450000000';
    assert.equal((await health()).chatTiming, undefined);
  } finally { await app.close(); await rm(directory, {recursive: true, force: true}); }
});

test('bearer health rejects unsafe task diagnostics and telemetry exceptions without losing health', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-task-health-'));
  const bearerToken = 'fixture-only-bearer-for-local-tests-000';
  const event = {tool: 'run_sotto_script', script: 'loops-query', status: 'unknown', error: 'none'};
  const valid = {events: [event], completedAtMs: 1791450000000};
  let batch: unknown = valid;
  let throws = false;
  const app = await startApplication({directory, mediaRoot: directory, bearerToken,
    owner: {channel: 'telegram', accountId: 'bot', ownerId: '42'}, lineKey: 'bot', target: 'owner', providerThreadId: 'telegram:42'}, {
    agent: {generateChat: async () => ({text: ''}), generateUnattended: async () => ({text: ''}), rotateThread: async () => {}, purgeThreadsBefore: async () => 0},
    transport: {send: async () => ({acceptance: 'accepted', providerMessageIds: ['p1']})},
    taskToolDiagnostics: () => { if (throws) throw new Error('PRIVATE'); return batch; },
  });
  const url = `http://127.0.0.1:${app.port}/v1/health`;
  const health = async () => (await fetch(url, {headers: {authorization: `Bearer ${bearerToken}`}})).json() as Promise<Record<string, unknown>>;
  try {
    assert.equal((await fetch(url)).status, 401);
    assert.deepEqual((await health()).taskToolDiagnostics, valid);
    const rejection = {tool: 'owner_task_resolve_many', status: 'rejected', error: 'none', rejection: 'target_unavailable'};
    batch = {...valid, events: [rejection]};
    assert.deepEqual((await health()).taskToolDiagnostics, batch);
    for (const extra of [{rejection: 'PRIVATE'}, {rejection: null}, {rejection: ['target_unavailable']},
      {status: 'recorded'}, {tool: 'run_sotto_script', script: 'loops-query'}, {args: 'PRIVATE'}]) {
      batch = {...valid, events: [{...rejection, ...extra}]};
      const response = await health();
      assert.equal(response.taskToolDiagnostics, undefined);
      assert.ok(!JSON.stringify(response).includes('PRIVATE'));
    }

    for (const invalid of [{...valid, prompt: 'PRIVATE'}, {...valid, completedAtMs: -1}, {...valid, completedAtMs: true},
      {...valid, events: Array(9).fill(event)}, ...[
        {...event, args: 'PRIVATE'}, {...event, status: 'PRIVATE'}, {...event, error: 'PRIVATE'},
        {...event, script: 'PRIVATE'}, {...event, tool: 'PRIVATE'},
        {tool: 'owner_task_close', status: 'recorded', error: 'none', script: 'loops-query'},
        {tool: 'run_sotto_script', status: 'unknown', error: 'none'},
      ].map(item => ({...valid, events: [item]}))]) {
      batch = invalid;
      const response = await health();
      assert.equal(response.taskToolDiagnostics, undefined);
      assert.ok(!JSON.stringify(response).includes('PRIVATE'));
    }
    batch = {...valid, events: []};
    assert.deepEqual((await health()).taskToolDiagnostics, batch);
    throws = true;
    assert.equal((await health()).taskToolDiagnostics, undefined);
    assert.equal((await health()).runtime, 'sotto');
  } finally { await app.close(); await rm(directory, {recursive: true, force: true}); }
});

test('startup holds unresolved pre-v6 scheduled rows before service or intake', async () => {
  for (const [kind, status] of [
    ['scheduled_text', 'prepared'], ['scheduled_text', 'dispatching'],
    ['scheduled_text', 'unknown'], ['scheduled_text', 'rejected'],
    ['gallery', 'unknown'],
  ] as const) {
    const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-legacy-route-'));
    const journal = new OperationalJournal(path.join(directory, 'operations.sqlite'));
    try {
      journal.prepareDispatch({ dispatchId: 'legacy', kind, destination: 'owner', payload: 'old' });
      if (status !== 'prepared') {
        journal.beginDispatch('legacy');
        if (status !== 'dispatching') journal.settleDispatch('legacy', 1, status);
      }
    } finally { journal.close(); }
    let sends = 0;
    const options: ApplicationOptions = { directory, mediaRoot: directory,
      bearerToken: 'fixture-only-bearer-for-local-tests-000',
      owner: { channel: 'telegram', accountId: 'bot', ownerId: '42' },
      lineKey: 'bot', target: 'owner', providerThreadId: 'telegram:42', deferListen: true };
    try {
      await assert.rejects(startApplication(options, {
        agent: { generateChat: async () => ({ text: '' }), generateUnattended: async () => ({ text: '' }),
          rotateThread: async () => {}, purgeThreadsBefore: async () => 0 },
        transport: { send: async () => { sends++; return { acceptance: 'accepted', providerMessageIds: ['wrong'] }; } },
      }), /offline reconciliation/, `${kind}/${status}`);
      assert.equal(sends, 0);
    } finally { await rm(directory, { recursive: true, force: true }); }
  }
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-legacy-safe-'));
  const journal = new OperationalJournal(path.join(directory, 'operations.sqlite'));
  try {
    journal.prepareDispatch({ dispatchId: 'accepted-scheduled', kind: 'scheduled_text',
      destination: 'owner', payload: 'old' });
    journal.beginDispatch('accepted-scheduled');
    journal.settleDispatch('accepted-scheduled', 1, 'accepted', 'old-provider-id');
    journal.prepareDispatch({ dispatchId: 'interactive-unknown', kind: 'interactive',
      destination: 'owner', payload: 'chat' });
    journal.beginDispatch('interactive-unknown');
    journal.settleDispatch('interactive-unknown', 1, 'unknown');
  } finally { journal.close(); }
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    app = await startApplication({ directory, mediaRoot: directory, deferListen: true,
      bearerToken: 'fixture-only-bearer-for-local-tests-000',
      owner: { channel: 'telegram', accountId: 'bot', ownerId: '42' },
      lineKey: 'bot', target: 'owner', providerThreadId: 'telegram:42' }, {
      agent: { generateChat: async () => ({ text: '' }), generateUnattended: async () => ({ text: '' }),
        rotateThread: async () => {}, purgeThreadsBefore: async () => 0 },
      transport: { send: async () => ({ acceptance: 'accepted', providerMessageIds: ['p1'] }) },
    });
  } finally { await app?.close(); await rm(directory, { recursive: true, force: true }); }
});

test('assembled service owns one directory, admits only owner, rotates scoped memory, and keeps /run silent', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-app-'));
  const options: ApplicationOptions = { directory, mediaRoot: directory, bearerToken: 'fixture-only-bearer-for-local-tests-000',
    owner: { channel: 'telegram', accountId: 'bot-a', ownerId: '42' }, lineKey: 'bot-a', target: 'owner', providerThreadId: 'telegram:42' };
  const turns: ModelRequest[] = [];
  const deleted: string[] = [];
  const sends: string[] = [];
  const agent: ApplicationAgent = {
    generateChat: async input => { turns.push(input); return { text: 'Prepared reply' }; },
    generateUnattended: async () => ({ text: 'Saved result' }),
    rotateThread: async id => { deleted.push(id); }, purgeThreadsBefore: async () => 0,
  };
  const deps = { agent, transport: { send: async (input: { text?: string }) => {
    sends.push(input.text ?? ''); return { acceptance: 'accepted' as const, providerMessageIds: [`provider-${sends.length}`] };
  } } };
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    app = await startApplication(options, deps);
    await assert.rejects(startApplication(options, deps), /already owned/);
    const inbound = { channel: 'telegram' as const, eventId: '1', accountId: 'bot-a', senderId: '42', isDirect: true, text: 'hello' };
    assert.equal((await app.receive({ ...inbound, senderId: 'other' })).status, 'denied');
    assert.equal((await app.receive(inbound)).status, 'accepted');
    assert.equal((await app.receive(inbound)).status, 'duplicate');
    assert.equal(turns.length, 1);
    assert.match(turns[0]!.threadId, /^[a-f0-9]{64}$/);
    assert.notEqual(turns[0]!.ownerId, '42');
    const post = (route: string, body: unknown) => fetch(`http://127.0.0.1:${app!.port}/v1/${route}`, {
      method: 'POST', headers: { authorization: `Bearer ${options.bearerToken}`, 'content-type': 'application/json' }, body: JSON.stringify(body),
    });
    assert.equal((await post('run', { prompt: 'work', env: { SOTTO_UNATTENDED: '1', SOTTO_DELIVERY_RUN_ID: 'run-1' } })).status, 200);
    assert.equal(sends.length, 1);
    assert.equal((await post('sessions', { action: 'rotate' })).status, 200);
    assert.deepEqual(deleted, [turns[0]!.threadId]);
    await app.receive({ ...inbound, eventId: '2' });
    assert.notEqual(turns[0]!.threadId, turns[1]!.threadId);
    await app.close(); app = undefined;
    app = await startApplication(options, deps);
    assert.equal((await app.receive(inbound)).status, 'duplicate');
    await app.close(); app = undefined;
    await assert.rejects(startApplication({ ...options, owner: { ...options.owner, ownerId: '99' } }, deps), /different owner/);
  } finally { await app?.close(); await rm(directory, { recursive: true, force: true }); }
});

test('ownership does not steal an existing directory and clean release permits restart', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-ownership-'));
  try {
    const release = await claimRuntimeDirectory(directory);
    await assert.rejects(claimRuntimeDirectory(directory), /already owned/);
    await release();
    await (await claimRuntimeDirectory(directory))();
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('shutdown joins a contacted HTTP send and saves its late receipt before releasing ownership', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-shutdown-receipt-'));
  let contacted!: () => void;
  const started = new Promise<void>(resolve => { contacted = resolve; });
  let finish!: () => void;
  const provider = new Promise<void>(resolve => { finish = resolve; });
  const options: ApplicationOptions = { directory, mediaRoot: directory, bearerToken: 'fixture-only-bearer-for-local-tests-000',
    owner: { channel: 'telegram', accountId: 'bot', ownerId: '42' }, lineKey: 'bot', target: 'owner', providerThreadId: 'telegram:42' };
  const app = await startApplication(options, {
    agent: { generateChat: async () => ({ text: '' }), generateUnattended: async () => ({ text: '' }),
      rotateThread: async () => {}, purgeThreadsBefore: async () => 0 },
    transport: { send: async () => { contacted(); await provider; return { acceptance: 'accepted', providerMessageIds: ['late-proof'] }; } },
  });
  let closing: Promise<void> | undefined;
  const request = fetch(`http://127.0.0.1:${app.port}/v1/send`, {
    method: 'POST', headers: { authorization: `Bearer ${options.bearerToken}`, 'content-type': 'application/json' },
    body: JSON.stringify({ dispatch_id: 'scheduled-shutdown', target: 'owner', text: 'fixture send' }),
  }).then(() => undefined, () => undefined); // shutdown deliberately closes the caller's socket
  try {
    await started;
    let closed = false;
    closing = app.close().then(() => { closed = true; });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(closed, false);
    await assert.rejects(claimRuntimeDirectory(directory), /already owned/);
    finish();
    await closing;
    await request;
    const journal = new OperationalJournal(path.join(directory, 'operations.sqlite'));
    try {
      assert.equal(journal.getDispatch('scheduled-shutdown')?.status, 'accepted');
      assert.equal(journal.getDispatch('scheduled-shutdown')?.provider_message_id, 'late-proof');
    } finally { journal.close(); }
    await (await claimRuntimeDirectory(directory))();
  } finally { finish(); await (closing ?? app.close()); await request; await rm(directory, { recursive: true, force: true }); }
});


test('existing bearer-protected health exposes only optional feedback snapshot without provider contact', async () => {
 const directory=await mkdtemp(path.join(os.tmpdir(),'sotto-feedback-health-'));
 let providerCalls=0;
 const feedback=new PhotonOwnerFeedback({markRead:async()=>{providerCalls++;},addReaction:async()=>{providerCalls++;}});
 const options:ApplicationOptions={directory,mediaRoot:directory,bearerToken:'fixture-only-bearer-for-local-tests-000',
  owner:{channel:'photon',accountId:'account',ownerId:'owner'},lineKey:'line',target:'photon',providerThreadId:'thread'};
 const app=await startApplication(options,{agent:{generateChat:async()=>({text:'Hello'}),generateUnattended:async()=>({text:''}),rotateThread:async()=>{},purgeThreadsBefore:async()=>0},
  transport:{send:async()=>({acceptance:'accepted',providerMessageIds:['synthetic']})},feedbackStatus:()=>feedback.snapshot()});
 try{
  const url=`http://127.0.0.1:${app.port}/v1/health`;
  assert.equal((await fetch(url)).status,401);
  const before=await (await fetch(url,{headers:{authorization:`Bearer ${options.bearerToken}`}})).json() as any;
  assert.deepEqual(before.feedback,{available:false});assert.equal(providerCalls,0);
  feedback.admitted({eventId:'private-message',text:'private content'},'private-thread',()=>true);
  await new Promise<void>(resolve=>setImmediate(resolve));
  const calls=providerCalls;
  const after=await (await fetch(url,{headers:{authorization:`Bearer ${options.bearerToken}`}})).json() as any;
  assert.equal(after.feedback.available,true);assert.equal(after.feedback.turn_sequence,1);assert.equal(providerCalls,calls);
  assert.doesNotMatch(JSON.stringify(after.feedback),/private-message|private content|private-thread/);
 }finally{await feedback.close();await app.close();await rm(directory,{recursive:true,force:true});}
});

test('the receiver retention route compacts fences, settled gallery content and expired unknown content', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-retention-compaction-'));
  const options: ApplicationOptions = { directory, mediaRoot: directory, bearerToken: 'fixture-only-bearer-for-local-tests-000',
    owner: { channel: 'telegram', accountId: 'bot', ownerId: '42' }, lineKey: 'bot', target: 'owner', providerThreadId: 'telegram:42' };
  const seed = new OperationalJournal(path.join(directory, 'operations.sqlite'));
  try {
    const at = (sql: string, ...values: (string | number)[]) => seed.db.prepare(sql).run(...values);
    at("INSERT INTO inbound_events(id,source_id,account_key,line_key,thread_key,channel,sender,payload_hash,payload,received_at,outcome) VALUES('e1','e1','bot','bot','t','telegram','42',?,'',0,'resolved')", 'h'.repeat(64));
    at("INSERT INTO runs(id,event_id,thread_key,generation,status,started_at,ended_at) VALUES('r1','e1','t',1,'completed',0,0)");
    at("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,status,result_json,created_at,updated_at) VALUES('a1','r1','owner_routine_tool','p','e1','succeeded',?,0,0)",
      JSON.stringify({ args: { operation: 'apply', text: 'private routine wording' } }));
    at("INSERT INTO dispatches(id,kind,destination,payload_hash,payload,status,current_attempt,max_attempts,provider_message_id,provider_message_ids,created_at,updated_at) VALUES('g1','gallery','owner',?,'private gallery summary','accepted',1,1,'p1','[\"p1\"]',0,0)", 'g'.repeat(64));
    at("INSERT INTO dispatch_attempts(dispatch_id,number,status,provider_message_id,started_at,finished_at) VALUES('g1',1,'accepted','p1',0,0)");
    at("INSERT INTO dispatches(id,kind,destination,payload_hash,payload,status,current_attempt,max_attempts,created_at,updated_at) VALUES('u1','interactive','owner',?,'private uncertain reply','unknown',1,1,0,0)", 'u'.repeat(64));
    at("INSERT INTO dispatch_attempts(dispatch_id,number,status,started_at,finished_at) VALUES('u1',1,'unknown',0,0)");
  } finally { seed.close(); }
  const app = await startApplication(options, { agent: {
    generateChat: async () => ({ text: '' }), generateUnattended: async () => ({ text: '' }),
    rotateThread: async () => {}, purgeThreadsBefore: async () => 0,
  }, transport: { send: async () => ({ acceptance: 'accepted', providerMessageIds: ['p'] }) } });
  try {
    const response = await fetch(`http://127.0.0.1:${app.port}/v1/retention`, { method: 'POST',
      headers: { authorization: `Bearer ${options.bearerToken}`, 'content-type': 'application/json' },
      body: JSON.stringify({ delivery_before: new Date(Date.now() - 60_000).toISOString(),
        action_before: new Date(Date.now() - 120_000).toISOString() }) });
    assert.equal(response.status, 200);
    assert.deepEqual(Object.keys(await response.json() as object).sort(), ['actions', 'dispatches', 'events', 'replies', 'runs', 'sdkExpired']);
    const journal = new OperationalJournal(path.join(directory, 'operations.sqlite'));
    try {
      assert.doesNotMatch(String(journal.getAction('a1')?.result_json), /private routine wording/);
      assert.equal(journal.getAction('a1')?.status, 'succeeded');
      assert.equal(journal.getDispatch('g1')?.payload, '');
      assert.equal(journal.getDispatch('g1')?.provider_message_id, 'p1');
      assert.equal(journal.getDispatch('u1')?.payload, '');
      assert.equal(journal.getDispatch('u1')?.status, 'unknown');
      assert.equal(journal.getDispatch('u1')?.payload_hash, 'u'.repeat(64));
    } finally { journal.close(); }
  } finally { await app.close(); await rm(directory, { recursive: true, force: true }); }
});
