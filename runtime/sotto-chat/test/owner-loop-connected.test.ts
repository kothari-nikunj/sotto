import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync } from 'node:fs';
import { chmod, lstat, mkdtemp, readdir, readFile, rm, writeFile, unlink } from 'node:fs/promises';
import { createHmac } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { once } from 'node:events';
import { DatabaseSync } from 'node:sqlite';
import { startApplication, type ApplicationAgent, type ApplicationOptions } from '../src/application.js';
import { createOwnerLoopClient } from '../src/tools/owner-loop-client.js';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import { photonProjectFingerprint } from '../src/activation.js';
import { readOwnerLoops } from '../src/agent/owner-loops.js';

const owner = '+15555550123';
const line = '+15555550999';
const guid = `iMessage;-;${owner}`;
const thread = `imessage:${guid}~${line}`;
const project = 'fixture-project';
const root = '0123456789abcdef0123456789abcdef';

async function receiverFixture(dataRoot: string) {
  const localPython = resolve('../../.venv/bin/python');
  const python = process.env.SOTTO_PYTHON ?? process.env.PYTHON ?? (existsSync(localPython) ? localPython : 'python3');
  const child = spawn(python, [resolve('test/owner-loop-receiver-child.py')], {
    cwd: resolve('.'), detached: true, stdio: ['pipe', 'pipe', 'pipe'], env: {
      PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1', SOTTO_DEPLOYMENT_MODE: 'managed',
      SOTTO_DATA: dataRoot, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'fixture-tenant',
      SOTTO_ACCOUNT_ID: 'fixture-account', SOTTO_LINE_KEY: 'fixture-line', SOTTO_MCP_TOKEN: root,
      PHOTON_HOME_CHANNEL: owner, PHOTON_ALLOWED_USERS: owner, PHOTON_PROJECT_ID: project,
      PHOTON_PROJECT_SECRET: 'fixture-secret', PHOTON_ALLOW_ALL_USERS: 'false',
      SOTTO_IMESSAGE_NUMBER: line, SOTTO_TIMEZONE: 'America/Los_Angeles',
    },
  });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr = (stderr + String(chunk)).slice(-4096); });
  let timer: ReturnType<typeof setTimeout> | undefined;
  let firstLine: string;
  try { firstLine = await Promise.race([
    new Promise<string>((done, fail) => {
      let output = '';
      child.stdout.on('data', chunk => {
        if (output.length > 4096) return;
        output += String(chunk).slice(0, 4097 - output.length);
        if (output.length > 4096) { fail(new Error('receiver fixture output exceeded bound')); return; }
        const end = output.indexOf('\n');
        if (end >= 0) done(output.slice(0, end));
      });
      child.once('exit', code => fail(new Error(`receiver fixture exited ${code}: ${stderr.slice(0, 1024)}`)));
      child.once('error', () => fail(new Error('receiver fixture failed to start')));
    }),
    new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('receiver fixture timed out')), 15_000); }),
  ]).finally(() => { if (timer) clearTimeout(timer); });
  } catch (error) {
    killReceiver(child);
    throw error;
  }
  const parsed = JSON.parse(firstLine) as { port: number; identity: OwnerToolIdentity; bearer: string };
  return { child, endpoint: `http://127.0.0.1:${parsed.port}/mcp`, identity: parsed.identity, bearer: parsed.bearer };
}

function killReceiver(child: ChildProcessWithoutNullStreams): void {
  if (child.pid !== undefined) { try { process.kill(-child.pid, 'SIGKILL'); } catch { /* exited */ } }
}

async function stopReceiver(child: ChildProcessWithoutNullStreams): Promise<void> {
  if (child.exitCode !== null) return;
  child.stdin.end('\n');
  let timer: ReturnType<typeof setTimeout> | undefined;
  await Promise.race([once(child, 'exit'), new Promise<void>(done => { timer = setTimeout(() => { killReceiver(child); done(); }, 5000); })])
    .finally(() => { if (timer) clearTimeout(timer); });
}

async function setReceiverZone(child: ChildProcessWithoutNullStreams, zone: string): Promise<void> {
  const response = new Promise<string>(done => child.stdout.once('data', chunk => done(String(chunk))));
  child.stdin.write(`timezone=${zone}\n`);
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const reply = await Promise.race([response, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error('receiver timezone fixture timed out')), 5000);
    })]);
    assert.deepEqual(JSON.parse(reply), { timezone: zone });
  } finally { if (timer) clearTimeout(timer); }
}

async function receiverControl(child: ChildProcessWithoutNullStreams, command: string): Promise<Record<string, unknown>> {
  const response = new Promise<string>(done => child.stdout.once('data', chunk => done(String(chunk))));
  child.stdin.write(`${command}\n`);
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return JSON.parse(await Promise.race([response, new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error('receiver control fixture timed out')), 5000);
    })])) as Record<string, unknown>;
  } finally { if (timer) clearTimeout(timer); }
}

function makeUnknownActionDue(directory: string, kind: string): void {
  const db = new DatabaseSync(join(directory, 'operations.sqlite'));
  try {
    assert.equal(db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind=? AND status='unknown'").run(kind).changes, 1);
  } finally { db.close(); }
}

test('coalesced explicit owner command writes through actual receiver while ordinary input remains separate', async () => {
  const rootDir = await mkdtemp(join(tmpdir(), 'sotto-owner-connected-'));
  let receiver: Awaited<ReturnType<typeof receiverFixture>> | undefined;
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    receiver = await receiverFixture(join(rootDir, 'receiver'));
    assert.deepEqual(receiver.identity, { tenant_id: 'fixture-tenant', channel: 'photon', account_id: 'fixture-account',
      owner_id: owner, line_key: 'fixture-line', target: 'photon', route_mode: 'fixed', route_id: thread,
      project_fingerprint: photonProjectFingerprint(project) });
    let unblock!: () => void;
    const firstModel = new Promise<void>(done => { unblock = done; });
    let started!: () => void;
    const modelStarted = new Promise<void>(done => { started = done; });
    const generated: string[] = [];
    const sent: string[] = [];
    const agent: ApplicationAgent = {
      generateChat: async request => {
        generated.push(request.text);
        if (generated.length === 1) {
          started();
          await Promise.race([firstModel, new Promise<void>(done => request.signal.addEventListener('abort', () => done(), { once: true }))]);
        }
        return { text: 'ordinary response' };
      },
      generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0,
    };
    const options: ApplicationOptions = { directory: join(rootDir, 'node'), mediaRoot: rootDir,
      bearerToken: 'offline-owner-loop-node-control-token',
      owner: { channel: 'photon', accountId: 'fixture-account', ownerId: owner },
      lineKey: 'fixture-line', target: 'photon', providerThreadId: thread,
      activation: { tenant_id: 'fixture-tenant', owner_id: owner, account_id: 'fixture-account',
        line_key: 'fixture-line', provider_thread_id: thread, project_fingerprint: photonProjectFingerprint(project) } };
    const ownerLoop = createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: receiver.bearer,
      identity: receiver.identity });
    const readBearer = await fetch(receiver.endpoint.replace('/mcp', '/runtime/owner-loop-intent'), {
      method: 'POST', headers: { authorization: `Bearer ${root}`, 'content-type': 'application/json' },
      body: JSON.stringify({ identity: receiver.identity }),
    });
    assert.equal(readBearer.status, 401, 'the generic receiver root cannot call the derived command route');
    app = await startApplication(options, { agent, ownerLoop, transport: { send: async input => {
      sent.push(input.text ?? ''); return { acceptance: 'accepted', providerMessageIds: [`receipt-${sent.length}`] };
    } } });
    const first = app.receive({ channel: 'photon', eventId: 'ordinary-1', accountId: 'fixture-account',
      senderId: owner, isDirect: true, text: 'What is due?' });
    await modelStarted;
    const command = app.receive({ channel: 'photon', eventId: 'command-2', accountId: 'fixture-account',
      senderId: owner, isDirect: true, text: '/loop add Send the signed deck' });
    unblock();
    assert.equal((await first).status, 'cancelled');
    assert.equal((await command).status, 'silent');
    assert.deepEqual(sent, ['Added to your open loops.', 'ordinary response']);
    assert.deepEqual(generated, ['What is due?', 'What is due?']);
    const db = new DatabaseSync(join(rootDir, 'node/operations.sqlite'), { readOnly: true });
    const rows = db.prepare('SELECT covered_event_ids FROM prepared_replies ORDER BY rowid').all();
    assert.equal(rows.length, 2);
    const covered = rows.map(row => JSON.parse(row.covered_event_ids as string) as string[]);
    assert.equal(covered[0]!.length, 1);
    assert.equal(covered[1]!.length, 1);
    assert.notEqual(covered[0]![0], covered[1]![0]);
    assert.equal(db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_loop_add_admitted' AND status='succeeded'").get()!.n, 1);
    db.close();
    assert.equal((await app.receive({ channel: 'photon', eventId: 'command-2', accountId: 'fixture-account',
      senderId: owner, isDirect: true, text: '/loop add Send the signed deck' })).status, 'duplicate');
    assert.equal(sent.length, 2);
  } finally {
    await app?.close();
    if (receiver) await stopReceiver(receiver.child);
    await rm(rootDir, { recursive: true, force: true });
  }
});

test('lost command receipt is held, then reconciled by canonical read without a second write', async () => {
  const rootDir = await mkdtemp(join(tmpdir(), 'sotto-owner-reconcile-'));
  let receiver: Awaited<ReturnType<typeof receiverFixture>> | undefined;
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    receiver = await receiverFixture(join(rootDir, 'receiver'));
    const options: ApplicationOptions = { directory: join(rootDir, 'node'), mediaRoot: rootDir,
      bearerToken: 'offline-owner-loop-reconcile-control-token',
      owner: { channel: 'photon', accountId: 'fixture-account', ownerId: owner },
      lineKey: 'fixture-line', target: 'photon', providerThreadId: thread,
      activation: { tenant_id: 'fixture-tenant', owner_id: owner, account_id: 'fixture-account',
        line_key: 'fixture-line', provider_thread_id: thread, project_fingerprint: photonProjectFingerprint(project) } };
    const sent: string[] = [];
    const agent: ApplicationAgent = { generateChat: async () => { throw new Error('command reached model'); },
      generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0 };
    const transport = { send: async (input: { text?: string }) => {
      sent.push(input.text ?? ''); return { acceptance: 'accepted' as const, providerMessageIds: [`receipt-${sent.length}`] };
    } };
    let writes = 0;
    const loseReceipt: typeof fetch = async (input, init) => {
      const operation = JSON.parse(String(init?.body)).operation as string;
      if (operation === 'inspect') throw new Error('synthetic inspection outage until restart');
      const response = await fetch(input, init);
      if (operation === 'write') { writes++; throw new Error('synthetic lost response after canonical write'); }
      return response;
    };
    const lostClient = createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: receiver.bearer,
      identity: receiver.identity, fetch: loseReceipt });
    app = await startApplication(options, { agent, transport, ownerLoop: lostClient });
    const message = { channel: 'photon' as const, eventId: 'command-lost', accountId: 'fixture-account',
      senderId: owner, isDirect: true, text: '/loop add Confirm budget with Alex' };
    assert.equal((await app.receive(message)).status, 'held');
    assert.equal(writes, 1);
    assert.equal(sent.length, 1);
    assert.match(sent[0]!, /^I couldn’t confirm whether the task addition happened/);
    const notice = sent[0]!;
    const db = new DatabaseSync(join(rootDir, 'node/operations.sqlite'), { readOnly: true });
    assert.equal(db.prepare("SELECT status FROM actions WHERE kind='owner_loop_add_admitted'").get()!.status, 'unknown');
    db.close();
    await app.close(); app = undefined;
    const changedClient = createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: 'b'.repeat(64),
      identity: receiver.identity, fetch: async () => { throw new Error('changed authority must not contact receiver'); } });
    makeUnknownActionDue(options.directory, 'owner_loop_add_admitted');
    app = await startApplication(options, { agent, transport, ownerLoop: changedClient });
    assert.deepEqual(sent, [notice], 'a changed private capability cannot reconcile the prior action');
    await app.close(); app = undefined;
    await setReceiverZone(receiver.child, 'UTC');
    const zoneClient = createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: receiver.bearer,
      identity: receiver.identity });
    makeUnknownActionDue(options.directory, 'owner_loop_add_admitted');
    app = await startApplication(options, { agent, transport, ownerLoop: zoneClient });
    assert.deepEqual(sent, [notice], 'a changed receiver timezone cannot prove the original command');
    await app.close(); app = undefined;
    await setReceiverZone(receiver.child, 'America/Los_Angeles');
    const noWrite: typeof fetch = async (input, init) => {
      if (JSON.parse(String(init?.body)).operation === 'write') throw new Error('unknown action was replayed');
      return fetch(input, init);
    };
    const restoredClient = createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: receiver.bearer,
      identity: receiver.identity, fetch: noWrite });
    makeUnknownActionDue(options.directory, 'owner_loop_add_admitted');
    app = await startApplication(options, { agent, transport, ownerLoop: restoredClient });
    assert.deepEqual(sent, [notice, 'Added to your open loops.']);
    assert.equal((await app.receive(message)).status, 'duplicate');
    assert.deepEqual(sent, [notice, 'Added to your open loops.']);
    const after = new DatabaseSync(join(rootDir, 'node/operations.sqlite'), { readOnly: true });
    assert.equal(after.prepare("SELECT status FROM actions WHERE kind='owner_loop_add_admitted'").get()!.status, 'succeeded');
    after.close();
  } finally {
    await app?.close();
    if (receiver) await stopReceiver(receiver.child);
    await rm(rootDir, { recursive: true, force: true });
  }
});

test('unknown admitted add holds its command while a new owner read proceeds, then exact inspect settles once', async () => {
  const rootDir = await mkdtemp(join(tmpdir(), 'sotto-owner-add-isolation-'));
  let receiver: Awaited<ReturnType<typeof receiverFixture>> | undefined;
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    receiver = await receiverFixture(join(rootDir, 'receiver'));
    const options: ApplicationOptions = { directory: join(rootDir, 'node'), mediaRoot: rootDir,
      bearerToken: 'offline-owner-add-isolation-token',
      owner: { channel: 'photon', accountId: 'fixture-account', ownerId: owner },
      lineKey: 'fixture-line', target: 'photon', providerThreadId: thread,
      activation: { tenant_id: 'fixture-tenant', owner_id: owner, account_id: 'fixture-account',
        line_key: 'fixture-line', provider_thread_id: thread, project_fingerprint: photonProjectFingerprint(project) } };
    const readBearer = createHmac('sha256', root).update('sotto-mcp').digest('hex');
    const sent: string[] = [];
    const generated: string[] = [];
    const rotated: string[] = [];
    const agent: ApplicationAgent = {
      generateChat: async request => {
        generated.push(request.text);
        const loops = await readOwnerLoops({ bridgeUrl: receiver!.endpoint, readBearer }, { include_parked: false });
        assert.equal(loops.you_owe.length, 1, 'safe read observes one canonical add');
        return { text: `safe: ${request.text}` };
      },
      generateUnattended: async () => ({ text: '' }),
      rotateThread: async threadId => { rotated.push(threadId); }, purgeThreadsBefore: async () => 0,
    };
    const transport = { send: async (input: { text?: string }) => {
      sent.push(input.text ?? ''); return { acceptance: 'accepted' as const, providerMessageIds: [`receipt-${sent.length}`] };
    } };
    let writes = 0;
    let inspectUnavailable = true;
    const interrupted: typeof fetch = async (input, init) => {
      const operation = JSON.parse(String(init?.body)).operation as string;
      if (operation === 'inspect' && inspectUnavailable) throw new Error('synthetic inspect outage');
      const response = await fetch(input, init);
      if (operation === 'write') { writes++; throw new Error('canonical commit response lost'); }
      return response;
    };
    const client = createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: receiver.bearer,
      identity: receiver.identity, fetch: interrupted });
    app = await startApplication(options, { agent, transport, ownerLoop: client });
    const add = { channel: 'photon' as const, eventId: 'add-lost-response', accountId: 'fixture-account',
      senderId: owner, isDirect: true, text: '/loop add Confirm budget with Alex' };
    assert.equal((await app.receive(add)).status, 'held');
    assert.equal(writes, 1);
    assert.equal(sent.length, 1);
    assert.match(sent[0]!, /^I couldn’t confirm whether the task addition happened/);
    const notice = sent[0]!;
    await app.close(); app = undefined;

    app = await startApplication(options, { agent, transport, ownerLoop: client });
    const safe = { ...add, eventId: 'safe-after-add', text: 'What remains open?' };
    assert.equal((await app.receive(safe)).status, 'accepted');
    assert.deepEqual(generated, ['What remains open?']);
    assert.deepEqual(sent, [notice, 'safe: What remains open?']);
    assert.equal((await app.receive({ ...add, eventId: 'rotate-after-add', text: '/new' })).status, 'rotated');
    assert.equal(rotated.length, 1);
    assert.ok(rotated[0]);
    assert.deepEqual(sent, [notice, 'safe: What remains open?', 'I started a new conversation.']);
    const db = new DatabaseSync(join(rootDir, 'node/operations.sqlite'), { readOnly: true });
    const old = db.prepare("SELECT id,outcome FROM inbound_events WHERE source_id='add-lost-response'").get() as { id: string; outcome: string };
    const fresh = db.prepare("SELECT id,outcome FROM inbound_events WHERE source_id='safe-after-add'").get() as { id: string; outcome: string };
    const coverage = db.prepare("SELECT p.covered_event_ids FROM prepared_replies p JOIN inbound_events e ON e.id=p.event_id WHERE e.source_id='safe-after-add'").get() as { covered_event_ids: string };
    assert.equal(old.outcome, 'pending');
    assert.equal(fresh.outcome, 'resolved');
    assert.deepEqual(JSON.parse(coverage.covered_event_ids), [fresh.id]);
    assert.equal(db.prepare("SELECT status FROM actions WHERE kind='owner_loop_add_admitted'").get()!.status, 'unknown');
    db.close();

    inspectUnavailable = false;
    makeUnknownActionDue(options.directory, 'owner_loop_add_admitted');
    assert.equal((await app.receive({ ...add, eventId: 'safe-after-proof', text: 'Continue safely' })).status, 'silent');
    assert.deepEqual(generated, ['What remains open?', 'Continue safely']);
    assert.deepEqual(sent, [notice, 'safe: What remains open?', 'I started a new conversation.', 'Added to your open loops.', 'safe: Continue safely']);
    assert.equal(writes, 1, 'the admitted add is never rewritten');
    const after = new DatabaseSync(join(rootDir, 'node/operations.sqlite'), { readOnly: true });
    assert.equal(after.prepare("SELECT status FROM actions WHERE kind='owner_loop_add_admitted'").get()!.status, 'succeeded');
    assert.equal(after.prepare("SELECT outcome FROM inbound_events WHERE source_id='add-lost-response'").get()!.outcome, 'resolved');
    after.close();
    assert.equal((await readOwnerLoops({ bridgeUrl: receiver.endpoint, readBearer }, { include_parked: false })).you_owe.length, 1);
  } finally {
    await app?.close();
    if (receiver) await stopReceiver(receiver.child);
    await rm(rootDir, { recursive: true, force: true });
  }
});

test('private owner-command client aborts even if an injected fetch stalls', async () => {
  const client = createOwnerLoopClient({ bridgeUrl: 'http://127.0.0.1:17890/mcp', bearer: 'a'.repeat(64),
    identity: { tenant_id: 'fixture-tenant', channel: 'photon', account_id: 'fixture-account', owner_id: owner,
      line_key: 'fixture-line', target: 'photon', route_mode: 'fixed', route_id: thread,
      project_fingerprint: photonProjectFingerprint(project) },
    fetch: async () => new Promise<Response>(() => {}),
  });
  const controller = new AbortController();
  const call = client.call('prepare', { actionId: 'a'.repeat(64), eventId: 'b'.repeat(64),
    payloadHash: 'c'.repeat(64), text: 'Send the signed deck', admittedAtMs: 1780000000000,
    zone: null, providerThreadId: thread }, controller.signal);
  controller.abort();
  await assert.rejects(call, /unavailable/);
});

test('receiver refuses a write when the owner timezone changes after preparation', async () => {
  const rootDir = await mkdtemp(join(tmpdir(), 'sotto-owner-zone-'));
  let receiver: Awaited<ReturnType<typeof receiverFixture>> | undefined;
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    receiver = await receiverFixture(join(rootDir, 'receiver'));
    const fixture = receiver;
    const sent: string[] = [];
    const writeAfterZoneChange: typeof fetch = async (input, init) => {
      if (JSON.parse(String(init?.body)).operation === 'write') await setReceiverZone(fixture.child, 'UTC');
      return fetch(input, init);
    };
    const client = createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: receiver.bearer,
      identity: receiver.identity, fetch: writeAfterZoneChange });
    const options: ApplicationOptions = { directory: join(rootDir, 'node'), mediaRoot: rootDir,
      bearerToken: 'offline-owner-loop-zone-control-token',
      owner: { channel: 'photon', accountId: 'fixture-account', ownerId: owner },
      lineKey: 'fixture-line', target: 'photon', providerThreadId: thread,
      activation: { tenant_id: 'fixture-tenant', owner_id: owner, account_id: 'fixture-account',
        line_key: 'fixture-line', provider_thread_id: thread, project_fingerprint: photonProjectFingerprint(project) } };
    app = await startApplication(options, { ownerLoop: client,
      agent: { generateChat: async () => { throw new Error('command reached model'); },
        generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0 },
      transport: { send: async input => {
        sent.push(input.text ?? ''); return { acceptance: 'accepted', providerMessageIds: ['unexpected'] };
      } },
    });
    assert.equal((await app.receive({ channel: 'photon', eventId: 'command-zone', accountId: 'fixture-account',
      senderId: owner, isDirect: true, text: '/loop add Submit the invoice' })).status, 'held');
    assert.equal(sent.length, 1);
    assert.match(sent[0]!, /^I couldn’t confirm whether the task addition happened/);
    const db = new DatabaseSync(join(rootDir, 'node/operations.sqlite'), { readOnly: true });
    assert.equal(db.prepare("SELECT status FROM actions WHERE kind='owner_loop_add_admitted'").get()!.status, 'unknown');
    db.close();
    await assert.rejects(lstat(join(rootDir, 'receiver/knowledge/continuity')),
      /ENOENT/, 'timezone refusal happened before the canonical ledger was opened');
  } finally {
    await app?.close();
    if (receiver) await stopReceiver(receiver.child);
    await rm(rootDir, { recursive: true, force: true });
  }
});

test('one admitted owner close uses the real protected receiver writer and survives restart', async () => {
  const rootDir = await mkdtemp(join(tmpdir(), 'sotto-owner-close-connected-'));
  let receiver: Awaited<ReturnType<typeof receiverFixture>> | undefined;
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    receiver = await receiverFixture(join(rootDir, 'receiver'));
    const options: ApplicationOptions = { directory: join(rootDir, 'node'), mediaRoot: rootDir,
      bearerToken: 'offline-owner-close-node-control-token',
      owner: { channel: 'photon', accountId: 'fixture-account', ownerId: owner },
      lineKey: 'fixture-line', target: 'photon', providerThreadId: thread,
      activation: { tenant_id: 'fixture-tenant', owner_id: owner, account_id: 'fixture-account',
        line_key: 'fixture-line', provider_thread_id: thread, project_fingerprint: photonProjectFingerprint(project) } };
    const sent: string[] = [];
    const dependencies = { ownerLoop: createOwnerLoopClient({ bridgeUrl: receiver.endpoint,
      bearer: receiver.bearer, identity: receiver.identity }),
    agent: { generateChat: async () => { throw new Error('explicit close reached model'); },
      generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0 },
    transport: { send: async (input: { text?: string }) => {
      sent.push(input.text ?? ''); return { acceptance: 'accepted' as const, providerMessageIds: [`accepted-${sent.length}`] };
    } } };
    app = await startApplication(options, dependencies);
    const add = { channel: 'photon' as const, eventId: 'add-for-close', accountId: 'fixture-account',
      senderId: owner, isDirect: true, text: '/loop add Send the signed deck' };
    await app.receive(add);
    assert.deepEqual(sent, ['Added to your open loops.']);
    const readBearer = createHmac('sha256', root).update('sotto-mcp').digest('hex');
    const read = () => readOwnerLoops({ bridgeUrl: receiver!.endpoint, readBearer }, { include_parked: false });
    const active = await read();
    assert.equal(active.coverage, 'owner_authored_only');
    assert.equal(active.counts.you_owe, 1);
    const anchor = active.you_owe[0]!.anchor_key;
    const command = { ...add, eventId: 'close-owner-loop', text: `/loop close ${anchor}` };
    await app.receive(command);
    assert.deepEqual((await read()).you_owe, []);
    assert.deepEqual(sent, ['Added to your open loops.', 'Closed your open loop.']);
    const db = new DatabaseSync(join(rootDir, 'node/operations.sqlite'), { readOnly: true });
    assert.equal(db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_loop_close_admitted' AND status='succeeded'").get()!.n, 1);
    db.close();
    await app.close(); app = undefined;
    app = await startApplication(options, dependencies);
    assert.equal((await app.receive(command)).status, 'duplicate');
    assert.deepEqual(sent, ['Added to your open loops.', 'Closed your open loop.']);
    assert.deepEqual((await read()).you_owe, []);
  } finally {
    await app?.close();
    if (receiver) await stopReceiver(receiver.child);
    await rm(rootDir, { recursive: true, force: true });
  }
});

test('active legacy and source-backed anchors receive typed pre-effect refusal without mutation', async () => {
  const rootDir = await mkdtemp(join(tmpdir(), 'sotto-owner-close-refusal-'));
  let receiver: Awaited<ReturnType<typeof receiverFixture>> | undefined;
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    receiver = await receiverFixture(join(rootDir, 'receiver'));
    assert.deepEqual(await receiverControl(receiver.child, 'seed-close-targets'), { seeded: true });
    const directory = join(rootDir, 'receiver/knowledge/continuity');
    const before = await Promise.all(['legacy.md', 'source.md'].map(name => readFile(join(directory, name))));
    const sent: string[] = [];
    app = await startApplication({ directory: join(rootDir, 'node'), mediaRoot: rootDir,
      bearerToken: 'offline-owner-close-refusal-control-token',
      owner: { channel: 'photon', accountId: 'fixture-account', ownerId: owner },
      lineKey: 'fixture-line', target: 'photon', providerThreadId: thread,
      activation: { tenant_id: 'fixture-tenant', owner_id: owner, account_id: 'fixture-account',
        line_key: 'fixture-line', provider_thread_id: thread, project_fingerprint: photonProjectFingerprint(project) } },
    { ownerLoop: createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: receiver.bearer,
      identity: receiver.identity }),
      agent: { generateChat: async () => { throw new Error('refused close reached model'); },
        generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0 },
      transport: { send: async input => {
        sent.push(input.text ?? ''); return { acceptance: 'accepted', providerMessageIds: [`refusal-${sent.length}`] };
      } } });
    for (const [index, anchorKey] of ['legacy-open', 'gmail-open'].entries()) {
      await app.receive({ channel: 'photon', eventId: `refused-close-${index}`, accountId: 'fixture-account',
        senderId: owner, isDirect: true, text: `/loop close ${anchorKey}` });
    }
    assert.deepEqual(sent, [
      'That owner-authored open loop could not be closed because it is unavailable or changed.',
      'That owner-authored open loop could not be closed because it is unavailable or changed.',
    ]);
    const after = await Promise.all(['legacy.md', 'source.md'].map(name => readFile(join(directory, name))));
    assert.deepEqual(after, before);
    const db = new DatabaseSync(join(rootDir, 'node/operations.sqlite'), { readOnly: true });
    assert.equal(db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_loop_close_admitted' AND status='rejected'").get()!.n, 2);
    db.close();
  } finally {
    await app?.close();
    if (receiver) await stopReceiver(receiver.child);
    await rm(rootDir, { recursive: true, force: true });
  }
});

test('a valid owner row changed between prepare and write is refused before close effect', async () => {
  const rootDir = await mkdtemp(join(tmpdir(), 'sotto-owner-close-version-'));
  let receiver: Awaited<ReturnType<typeof receiverFixture>> | undefined;
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    receiver = await receiverFixture(join(rootDir, 'receiver'));
    const options: ApplicationOptions = { directory: join(rootDir, 'node'), mediaRoot: rootDir,
      bearerToken: 'offline-owner-close-version-control-token',
      owner: { channel: 'photon', accountId: 'fixture-account', ownerId: owner },
      lineKey: 'fixture-line', target: 'photon', providerThreadId: thread,
      activation: { tenant_id: 'fixture-tenant', owner_id: owner, account_id: 'fixture-account',
        line_key: 'fixture-line', provider_thread_id: thread, project_fingerprint: photonProjectFingerprint(project) } };
    const sent: string[] = [];
    const transport = { send: async (input: { text?: string }) => {
      sent.push(input.text ?? ''); return { acceptance: 'accepted' as const, providerMessageIds: [`accepted-${sent.length}`] };
    } };
    const agent: ApplicationAgent = { generateChat: async () => { throw new Error('close reached model'); },
      generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0 };
    const normal = createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: receiver.bearer,
      identity: receiver.identity });
    app = await startApplication(options, { ownerLoop: normal, agent, transport });
    const add = { channel: 'photon' as const, eventId: 'add-before-change', accountId: 'fixture-account',
      senderId: owner, isDirect: true, text: '/loop add Submit the report' };
    await app.receive(add);
    const readBearer = createHmac('sha256', root).update('sotto-mcp').digest('hex');
    const read = () => readOwnerLoops({ bridgeUrl: receiver!.endpoint, readBearer }, { include_parked: false });
    const anchorKey = (await read()).you_owe[0]!.anchor_key;
    await app.close(); app = undefined;
    let changes = 0;
    const changeBeforeWrite: typeof fetch = async (input, init) => {
      if (JSON.parse(String(init?.body)).operation === 'close_write') {
        changes++;
        assert.deepEqual(await receiverControl(receiver!.child, `change-owner-row=${anchorKey}`), { changed: 1 });
      }
      return fetch(input, init);
    };
    app = await startApplication(options, { ownerLoop: createOwnerLoopClient({ bridgeUrl: receiver.endpoint,
      bearer: receiver.bearer, identity: receiver.identity, fetch: changeBeforeWrite }), agent, transport });
    await app.receive({ ...add, eventId: 'close-after-change', text: `/loop close ${anchorKey}` });
    assert.equal(changes, 1);
    assert.deepEqual(sent, ['Added to your open loops.',
      'That owner-authored open loop could not be closed because it is unavailable or changed.']);
    assert.equal((await read()).counts.you_owe, 1);
    const db = new DatabaseSync(join(rootDir, 'node/operations.sqlite'), { readOnly: true });
    assert.equal(db.prepare("SELECT status FROM actions WHERE kind='owner_loop_close_admitted'").get()!.status, 'rejected');
    db.close();
  } finally {
    await app?.close();
    if (receiver) await stopReceiver(receiver.child);
    await rm(rootDir, { recursive: true, force: true });
  }
});

test('lost close response holds the effect, safe chat proceeds, and exact inspect later settles without a second write', async () => {
  const rootDir = await mkdtemp(join(tmpdir(), 'sotto-owner-close-unknown-'));
  let receiver: Awaited<ReturnType<typeof receiverFixture>> | undefined;
  let app: Awaited<ReturnType<typeof startApplication>> | undefined;
  try {
    receiver = await receiverFixture(join(rootDir, 'receiver'));
    const options: ApplicationOptions = { directory: join(rootDir, 'node'), mediaRoot: rootDir,
      bearerToken: 'offline-owner-close-unknown-control-token',
      owner: { channel: 'photon', accountId: 'fixture-account', ownerId: owner },
      lineKey: 'fixture-line', target: 'photon', providerThreadId: thread,
      activation: { tenant_id: 'fixture-tenant', owner_id: owner, account_id: 'fixture-account',
        line_key: 'fixture-line', provider_thread_id: thread, project_fingerprint: photonProjectFingerprint(project) } };
    const sent: string[] = [];
    let writes = 0;
    const lostResponse: typeof fetch = async (input, init) => {
      if (JSON.parse(String(init?.body)).operation === 'close_inspect') {
        throw new Error('synthetic inspection outage until restart');
      }
      if (JSON.parse(String(init?.body)).operation === 'close_write') {
        writes++;
        const response = await fetch(input, init);
        assert.equal(response.status, 200);
        throw new Error('canonical commit response lost');
      }
      return fetch(input, init);
    };
    const agent: ApplicationAgent = { generateChat: async request => ({ text: `safe: ${request.text}` }),
      generateUnattended: async () => ({ text: '' }), rotateThread: async () => {}, purgeThreadsBefore: async () => 0 };
    const transport = { send: async (input: { text?: string }) => {
      sent.push(input.text ?? ''); return { acceptance: 'accepted' as const, providerMessageIds: [`accepted-${sent.length}`] };
    } };
    const observeCloseWrites: typeof fetch = async (input, init) => {
      if (JSON.parse(String(init?.body)).operation === 'close_write') writes++;
      return fetch(input, init);
    };
    const normalClient = createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: receiver.bearer,
      identity: receiver.identity, fetch: observeCloseWrites });
    app = await startApplication(options, { agent, transport, ownerLoop: normalClient });
    const add = { channel: 'photon' as const, eventId: 'add-before-lost-close', accountId: 'fixture-account',
      senderId: owner, isDirect: true, text: '/loop add Confirm budget with Alex' };
    await app.receive(add);
    assert.deepEqual(sent, ['Added to your open loops.']);
    const readBearer = createHmac('sha256', root).update('sotto-mcp').digest('hex');
    const read = () => readOwnerLoops({ bridgeUrl: receiver!.endpoint, readBearer }, { include_parked: false });
    const anchor = (await read()).you_owe[0]!.anchor_key;
    await app.close(); app = undefined;
    const lostClient = createOwnerLoopClient({ bridgeUrl: receiver.endpoint, bearer: receiver.bearer,
      identity: receiver.identity, fetch: lostResponse });
    app = await startApplication(options, { agent, transport, ownerLoop: lostClient });
    const close = { ...add, eventId: 'close-lost-response', text: `/loop close ${anchor}` };
    assert.equal((await app.receive(close)).status, 'held');
    assert.equal(writes, 1);
    assert.deepEqual((await read()).you_owe, []);
    const directory = join(rootDir, 'receiver/knowledge/continuity');
    const names = (await readdir(directory)).filter(name => name.endsWith('.md'));
    assert.equal(names.length, 1);
    const path = join(directory, names[0]!);
    const exactRow = await readFile(path);
    await unlink(path);
    await app.close(); app = undefined;
    app = await startApplication(options, { agent, transport, ownerLoop: normalClient });
    const safe = { ...add, eventId: 'safe-after-held-close', text: 'What is my safe next step?' };
    assert.equal((await app.receive(safe)).status, 'accepted');
    assert.ok(sent.includes('safe: What is my safe next step?'));
    assert.equal(writes, 1, 'unknown close must never automatically write again');
    const db = new DatabaseSync(join(rootDir, 'node/operations.sqlite'), { readOnly: true });
    assert.equal(db.prepare("SELECT status FROM actions WHERE kind='owner_loop_close_admitted'").get()!.status, 'unknown');
    const reply = db.prepare("SELECT covered_event_ids FROM prepared_replies p JOIN inbound_events e ON e.id=p.event_id WHERE e.source_id='safe-after-held-close'").get() as { covered_event_ids: string };
    const covered = JSON.parse(reply.covered_event_ids) as string[];
    assert.equal(covered.length, 1, 'safe reply must not cover the held close');
    db.close();
    await writeFile(path, exactRow);
    await chmod(path, 0o600);
    makeUnknownActionDue(options.directory, 'owner_loop_close_admitted');
    const later = { ...add, eventId: 'safe-after-restored-proof', text: 'Continue safely' };
    await app.receive(later);
    assert.equal(writes, 1);
    assert.ok(sent.includes('Closed your open loop.'));
    assert.ok(sent.includes('safe: Continue safely'), JSON.stringify(sent));
    assert.equal((await read()).counts.you_owe, 0);
  } finally {
    await app?.close();
    if (receiver) await stopReceiver(receiver.child);
    await rm(rootDir, { recursive: true, force: true });
  }
});
