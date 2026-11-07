import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { mkdtemp, readdir, rm, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { OperationalJournal } from '../src/journal/index.js';

const sourceRoot = process.env.SOTTO_FIXTURE_SOURCE_ROOT || resolve(dirname(fileURLToPath(import.meta.url)), '../../../');
const childScript = join(dirname(fileURLToPath(import.meta.url)), 'owner-loop-writer-child.py');
const pythonPath = join(sourceRoot, '.venv', 'bin', 'python');
const python = process.env.PYTHON || (existsSync(pythonPath) ? pythonPath : 'python3');
const writerAvailable = existsSync(join(sourceRoot, 'sotto-chief-of-staff', '_shared', 'knowledge', 'knowledge_edit.py')) &&
  spawnSync(python, ['-I', '-c', 'import yaml'], { timeout: 5000, stdio: 'ignore' }).status === 0;
if (!writerAvailable) throw new Error('canonical owner-loop writer or locked PyYAML dependency is unavailable');
const fixtureOptions = { timeout: 45_000 };
const syntheticCollisionClock = '2026-09-29T12:00:00+00:00';
const hash = (value: string) => createHash('sha256').update(value).digest('hex');
const authority = { runId: 'run-1', accountId: 'owner-account', ownerId: 'owner-42',
  lineKey: 'line-1', channel: 'telegram' as const };
const context = { threadId: 'telegram:owner-42', destination: 'owner-target', lineKey: 'line-1' };
function payload(id: string, text: string) {
  return JSON.stringify({ inbound: { eventId: id, accountId: authority.accountId,
    channel: authority.channel, senderId: authority.ownerId, text }, context });
}
function admit(journal: OperationalJournal, id: string, text: string) {
  return journal.admitInbound({ eventId: id, accountKey: authority.accountId, lineKey: authority.lineKey,
    threadKey: context.threadId, channel: authority.channel, sender: authority.ownerId, payload: payload(id, text) });
}
async function pythonWriter(dataRoot: string, action: 'write' | 'write_fail_dirsync' | 'inspect' | 'seed_source_collision',
                            text = 'Send the signed deck', eventId = 'fixture-event',
                            admittedAt = syntheticCollisionClock): Promise<Record<string, unknown>> {
  const child = spawn(python, ['-I', '-B', childScript, sourceRoot], {
    env: { PATH: process.env.PATH ?? '/usr/bin:/bin', SOTTO_DATA: dataRoot },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  const output: Buffer[] = []; const errors: Buffer[] = [];
  child.stdout.on('data', chunk => output.push(chunk));
  child.stderr.on('data', chunk => errors.push(chunk));
  child.stdin.end(JSON.stringify({ action, data_root: dataRoot, text, event_id: eventId, now: admittedAt }));
  const startedAt = Date.now();
  const status = await new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolveStatus, reject) => {
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, 15_000);
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('close', (code, signal) => { clearTimeout(timer);
      if (timedOut) reject(new Error(`canonical writer fixture timed out after ${Date.now() - startedAt}ms; signal=${signal ?? 'none'}; stderr=${Buffer.concat(errors).toString('utf8').slice(-4096)}`));
      else resolveStatus({ code, signal });
    });
  });
  assert.equal(status.code, 0,
    `canonical writer exited signal=${status.signal ?? 'none'} after ${Date.now() - startedAt}ms; stderr=${Buffer.concat(errors).toString('utf8').slice(-4096)}`);
  assert.ok(Buffer.concat(output).byteLength <= 8192);
  return JSON.parse(Buffer.concat(output).toString('utf8')) as Record<string, unknown>;
}
async function deleteCanonicalRow(dataRoot: string) {
  const directory = join(dataRoot, 'knowledge', 'continuity');
  const names = (await readdir(directory)).filter(name => name.endsWith('.md'));
  assert.equal(names.length, 1);
  await unlink(join(directory, names[0]!));
}
function admittedUtcClock(journal: OperationalJournal, eventId: string): string {
  const receivedAt = journal.getInbound(eventId)?.received_at;
  assert.equal(typeof receivedAt, 'number');
  // This fixture's owner zone is UTC. Production must use the receiver's
  // authoritative owner zone for the writer's day-based anchor.
  return new Date(receivedAt as number).toISOString().replace(/Z$/, '+00:00');
}

test('second admitted command reaches canonical writer; journal retains only compact receipt after pruning', fixtureOptions, async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'sotto-owner-loop-python-'));
  const journal = new OperationalJournal(join(dataRoot, 'operations.sqlite'));
  try {
    const first = admit(journal, 'first', 'Earlier owner message').event.id as string;
    const command = admit(journal, 'command', '/loop add Send the signed deck').event.id as string;
    journal.startRun({ runId: authority.runId, eventId: first });
    const intent = journal.prepareExplicitOwnerLoopIntent(authority);
    assert.equal(intent.sourceEventId, command);
    journal.beginAction(intent.actionId);
    const written = await pythonWriter(dataRoot, 'write', intent.text, intent.sourceEventId,
      admittedUtcClock(journal, intent.sourceEventId));
    assert.equal(written.ok, true);
    const result = written.result as { ok: boolean; anchor_key: string; created: boolean };
    assert.equal(result.created, true);
    assert.deepEqual((written.durability_trace as string[]).slice(0, 5),
      ['file_fsync', 'replace', 'directory_fsync', 'directory_fsync', 'directory_fsync']);
    const repeated = await pythonWriter(dataRoot, 'write', intent.text, intent.sourceEventId,
      admittedUtcClock(journal, intent.sourceEventId));
    assert.equal((repeated.result as { created: boolean }).created, false);
    assert.deepEqual(repeated.durability_trace, ['directory_fsync', 'directory_fsync', 'directory_fsync']);
    const inspected = await pythonWriter(dataRoot, 'inspect');
    assert.deepEqual(inspected.rows, [{ event_id: command, summary: intent.text,
      anchor: result.anchor_key, source: 'user_added', provenance: 'owner' }]);
    journal.settleExplicitOwnerLoopIntent(intent.actionId, hash(JSON.stringify(result)));
    // This receipt proves only the canonical ledger write, never reply delivery or channel activation.
    journal.completeRun(authority.runId);
    journal.resolveInbound(first, 'resolved'); journal.resolveInbound(command, 'resolved');
    const cutoff = Date.now() + 365 * 24 * 60 * 60 * 1000;
    journal.pruneTerminal(cutoff, cutoff);
    assert.equal(journal.getAction(intent.actionId)?.status, 'succeeded');
    assert.equal(journal.getInbound(command)?.payload, '');
    assert.equal(journal.getInbound(first)?.payload, '');
    assert.equal(JSON.stringify(journal.getAction(intent.actionId)).includes(intent.text), false);
    await deleteCanonicalRow(dataRoot); // Simulates terminal pruning or removal.
    assert.equal(admit(journal, 'command', '/loop add Send the signed deck').admitted, false);
    assert.equal(journal.getAction(intent.actionId)?.status, 'succeeded');
    assert.equal(((await pythonWriter(dataRoot, 'inspect')).rows as unknown[]).length, 0);
  } finally { journal.close(); await rm(dataRoot, { recursive: true, force: true }); }
});

test('crash after real canonical write but before Node settlement remains unknown, even if row vanishes', fixtureOptions, async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'sotto-owner-loop-unknown-'));
  const path = join(dataRoot, 'operations.sqlite');
  let journal = new OperationalJournal(path);
  try {
    const first = admit(journal, 'first', 'Earlier owner message').event.id as string;
    admit(journal, 'command', '/loop add Send the signed deck');
    journal.startRun({ runId: authority.runId, eventId: first });
    const intent = journal.prepareExplicitOwnerLoopIntent(authority);
    journal.beginAction(intent.actionId);
    assert.equal((await pythonWriter(dataRoot, 'write', intent.text, intent.sourceEventId,
      admittedUtcClock(journal, intent.sourceEventId))).ok, true);
    journal.close();
    journal = new OperationalJournal(path);
    assert.equal(journal.recoverInterrupted().actions, 1);
    journal.startRun({ runId: 'run-2', eventId: first });
    assert.equal(journal.prepareExplicitOwnerLoopIntent({ ...authority, runId: 'run-2' }).status, 'unknown');
    await deleteCanonicalRow(dataRoot);
    assert.equal(journal.prepareExplicitOwnerLoopIntent({ ...authority, runId: 'run-2' }).status, 'unknown');
    assert.throws(() => journal.beginAction(intent.actionId), /not eligible/);
    assert.throws(() => journal.settleExplicitOwnerLoopIntent(intent.actionId, hash('unproved')), /not eligible/);
  } finally { journal.close(); await rm(dataRoot, { recursive: true, force: true }); }
});

test('canonical source-backed collision and same-event changed text cannot gain owner proof', fixtureOptions, async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'sotto-owner-loop-collision-'));
  const journal = new OperationalJournal(join(dataRoot, 'operations.sqlite'));
  try {
    const command = admit(journal, 'owner-command', '/loop add Send the signed deck').event.id as string;
    const admittedAt = admittedUtcClock(journal, command);
    assert.equal((await pythonWriter(dataRoot, 'seed_source_collision', 'Send the signed deck',
      'fixture-seed', admittedAt)).ok, true);
    journal.startRun({ runId: authority.runId, eventId: command });
    const intent = journal.prepareExplicitOwnerLoopIntent(authority);
    journal.beginAction(intent.actionId);
    const refused = await pythonWriter(dataRoot, 'write', intent.text, intent.sourceEventId, admittedAt);
    assert.equal(refused.ok, false);
    assert.equal(refused.error, 'owner_anchor_collision');
    assert.deepEqual(refused.durability_trace, []);
    journal.rejectExplicitOwnerLoopIntent(intent.actionId, 'owner_anchor_collision');
    assert.equal(journal.getAction(intent.actionId)?.status, 'rejected');
    const rows = (await pythonWriter(dataRoot, 'inspect')).rows as Array<Record<string, unknown>>;
    assert.equal(rows.length, 1);
    assert.equal(rows[0]?.provenance, 'sources');
    assert.equal(rows[0]?.event_id, null);
    assert.equal(rows[0]?.source, 'gmail');
    await deleteCanonicalRow(dataRoot);
    assert.equal((await pythonWriter(dataRoot, 'write', 'Send the signed deck', 'same-event')).ok, true);
    const changed = await pythonWriter(dataRoot, 'write', 'Send a different deck', 'same-event');
    assert.equal(changed.ok, false);
    assert.equal(changed.error, 'owner_event_conflict');
    const after = (await pythonWriter(dataRoot, 'inspect')).rows as Array<Record<string, unknown>>;
    assert.equal(after.length, 1);
    assert.equal(after[0]?.summary, 'Send the signed deck');
    assert.equal(after[0]?.provenance, 'owner');
  } finally { journal.close(); await rm(dataRoot, { recursive: true, force: true }); }
});

test('directory fsync failure after replacement yields no success receipt and an unknown held action', fixtureOptions, async () => {
  const dataRoot = await mkdtemp(join(tmpdir(), 'sotto-owner-loop-dirsync-'));
  const path = join(dataRoot, 'operations.sqlite');
  let journal = new OperationalJournal(path);
  try {
    const command = admit(journal, 'command', '/loop add Send the signed deck').event.id as string;
    journal.startRun({ runId: authority.runId, eventId: command });
    const intent = journal.prepareExplicitOwnerLoopIntent(authority);
    journal.beginAction(intent.actionId);
    const response = await pythonWriter(dataRoot, 'write_fail_dirsync', intent.text,
      intent.sourceEventId, admittedUtcClock(journal, command));
    assert.equal(response.ok, false);
    assert.equal(response.error, 'uncertain');
    assert.deepEqual(response.durability_trace, ['file_fsync', 'replace', 'directory_fsync']);
    assert.equal(((await pythonWriter(dataRoot, 'inspect')).rows as unknown[]).length, 1);
    journal.close();
    journal = new OperationalJournal(path);
    assert.equal(journal.recoverInterrupted().actions, 1);
    journal.startRun({ runId: 'run-2', eventId: command });
    assert.equal(journal.prepareExplicitOwnerLoopIntent({ ...authority, runId: 'run-2' }).status, 'unknown');
    assert.throws(() => journal.rejectExplicitOwnerLoopIntent(intent.actionId, 'owner_anchor_collision'), /not eligible/);
  } finally { journal.close(); await rm(dataRoot, { recursive: true, force: true }); }
});
