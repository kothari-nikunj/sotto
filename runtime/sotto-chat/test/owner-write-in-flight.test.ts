import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { executeTool } from './helpers/execute-tool.js';
import { MockLanguageModelV4 } from 'ai/test';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator, type ModelRequest } from '../src/conversation.js';
import { createOwnerRoutineToolClient } from '../src/tools/owner-routine-tool-client.js';
import { createOwnerRoutineTools } from '../src/tools/owner-routine-tool.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { ownerRoutineToolKind, routineDefinitionHash } from '../src/tools/owner-routine-tool-contract.js';
const identity = { tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64) };
const ownerPolicy = { channel: 'photon' as const, accountId: 'account', ownerId: 'owner' };
const context = { threadId: 'thread', destination: 'owner', lineKey: 'line' };
const inbound = (eventId: string, text: string) => ({ channel: 'photon' as const, accountId: 'account', senderId: 'owner',
  isDirect: true, eventId, text });
const args = { operation: 'propose' as const, name: 'user-friday-loops', schedule: { hour: 16, minute: 0, weekday: 5 }, prompt: 'Summarize my owner-authored open loops by person.' };
const authority = { version: 2 as const, complete: true, revision: 'd'.repeat(64), bridge_sources: [], google_sources: [] };
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const usage = { inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 2, text: 2, reasoning: 0 } };
let applyHook: (() => Promise<void>) | undefined;
function fixture(folder: string) {
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  const calls: Record<string, unknown>[] = [], receipts = new Map<string, Record<string, unknown>>();
  let loseCreate = false, hideInspect = false, state = authority;
  let previewRefusal: 'routine_limit' | 'routine_conflict' | 'unsupported_definition' | undefined;
  const client = createOwnerRoutineToolClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      calls.push(body);
      assert.equal(body.text_hash, createHash('sha256').update(body.text).digest('hex'));
      assert.ok(!body.text.startsWith('/loop'));
      if (body.operation === 'list')
        return reply({ version: 1, status: 'list', event_id: body.event_id, payload_hash: body.payload_hash, coverage: 'fresh_only', routines: [] });
      if (body.operation === 'apply' && applyHook) await applyHook();
      if (body.operation === 'preview') {
        if (previewRefusal) return reply({version:1,status:'rejected',code:previewRefusal},409);
        const definition = { name: body.args.name, schedule: body.args.schedule, prompt: body.args.prompt, zone: 'UTC', destination: 'this Photon conversation' as const };
        if (body.args.operation === 'propose_adjust') {
          const previous = {name:args.name,schedule:args.schedule,prompt:args.prompt,zone:'UTC',destination:'this Photon conversation' as const};
          const updated = {...previous,schedule:body.args.schedule};
          return reply({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,
            operation:'adjust',routine_id:body.args.routine_id,previous_definition:previous,
            previous_definition_hash:routineDefinitionHash(previous),definition:updated,definition_hash:routineDefinitionHash(updated)});
        }
        return reply({ version: 1, status: 'preview', event_id: body.event_id, payload_hash: body.payload_hash, definition, definition_hash: routineDefinitionHash(definition) });
      }
      if (body.operation === 'inspect')
        return reply(!hideInspect && receipts.get(body.action_id) || { version: 1, status: 'not_found' });
      const binding = stableActionIdentity(body.event_id, ownerRoutineToolKind, body.args);
      const adjustedId = createHash('sha256').update(JSON.stringify(['sotto.fresh-routine.v1',body.action_id])).digest('hex');
      const receipt = { version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id, payload_hash: body.payload_hash, args_hash: binding.payloadHash,
        outcome: body.args.operation === 'adjust'
          ? {operation:'adjust',routine_id:adjustedId,definition_hash:routineDefinitionHash(body.args.definition),status:'active',previous_routine_id:body.args.routine_id,previous_definition_hash:body.args.previous_definition_hash}
          : { operation: body.args.operation, routine_id: 'b'.repeat(64), definition_hash: body.args.operation === 'create' ? routineDefinitionHash(body.args.definition) : 'a'.repeat(64), status: body.args.operation === 'create' ? 'active' : 'cancelled' } };
      receipts.set(body.action_id, receipt);
      if (loseCreate)
        throw new Error('lost after commit');
      return reply(receipt);
    } });
  const sourceAuthority = { read: async () => state };
  const tools = createOwnerRoutineTools({ client, journal: () => journal, sourceAuthority });
  const callTool = async (name: keyof typeof tools, input: unknown, request: ModelRequest, overrides: Record<string, unknown> = {}) => {
    return executeTool(tools[name], input, request.signal, { mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey,
      sourceRevision: state.revision, allowOwnerRoutine: request.allowOwnerRoutine, ...overrides });
  };
  return { get journal() { return journal; }, client, calls, tools, sourceAuthority, callTool,
    set previewRefusal(value: 'routine_limit' | 'routine_conflict' | 'unsupported_definition') {previewRefusal=value;},
    set loseCreate(value: boolean) { loseCreate = value; }, set hideInspect(value: boolean) { hideInspect = value; },
    revoke() { state = { ...authority, revision: 'e'.repeat(64) }; },
    reopen() { journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted(); } };
}

test('a second owner bubble while a confirmed write executes lets it finish, report, then answers the bubble', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-write-in-flight-')), f = fixture(folder);
  const sent: string[] = [], seen: string[] = [];
  let c!: ConversationCoordinator, second: Promise<unknown> | undefined;
  applyHook = async () => { second = c.receive(inbound('thanks', 'thanks!'), context); await new Promise(r => setTimeout(r, 20)); };
  c = new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerRoutineTool: f.client,
    model: { generate: async (r) => {
        seen.push(r.text);
        if (r.text.startsWith('Every')) await f.callTool('owner_routine_propose', { name: args.name, schedule: args.schedule, prompt: args.prompt }, r);
        else if (r.text === 'Yes.') await f.callTool('owner_routine_create', {}, r);
        return { text: 'ok', sourceRevision: (await f.sourceAuthority.read()).revision };
      } },
    rotateSession: async () => { }, postPrepared: async request => { sent.push(request.text); return { acceptance: 'accepted', messageId: `ack-${sent.length}` }; } });
  try {
    await c.receive(inbound('proposal', 'Every Friday summarize my loops'), context);
    await c.receive(inbound('yes', 'Yes.'), context);
    applyHook = undefined;
    assert.ok(second, 'the second bubble arrived while apply was in flight');
    await second;
    const rows = f.journal.db.prepare('SELECT status FROM actions WHERE kind=? ORDER BY created_at').all(ownerRoutineToolKind) as {status: string}[];
    assert.deepEqual(rows.map(r => r.status), ['succeeded', 'succeeded']);
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 1);
    assert.ok(sent.includes('Set up. Results will arrive here.'), sent.join(' | '));
    assert.equal(seen.at(-1), 'thanks!', 'the bubble is answered after the write reports');
  } finally { applyHook = undefined; await c.close(); f.journal.close(); rmSync(folder, { recursive: true, force: true }); }
});
test('a write attempted for messages that arrived together explains itself and changes nothing', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-write-batched-')), f = fixture(folder);
  try {
    const request = { runId: 'run', ownerRequestKey: 'event', allowOwnerRoutine: false } as never;
    await assert.rejects(f.callTool('owner_routine_propose', { name: args.name, schedule: args.schedule, prompt: args.prompt }, request),
      /Nothing changed: messages that arrived together cannot start a change\. Ask the owner to send this request again as one message\./);
    assert.equal(f.calls.length, 0);
  } finally { f.journal.close(); rmSync(folder, { recursive: true, force: true }); }
});
test('a correction mid-write aborts the rest of that turn once the write settles; no later tool reaches the receiver', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-write-then-stop-')), f = fixture(folder);
  let c!: ConversationCoordinator, second: Promise<unknown> | undefined, afterWrite = '', secondCalls = -1;
  applyHook = async () => { second = c.receive(inbound('correction', 'wait, stop, do nothing else'), context); await new Promise(r => setTimeout(r, 20)); };
  c = new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerRoutineTool: f.client,
    model: { generate: async (r) => {
        if (r.text.startsWith('Every')) await f.callTool('owner_routine_propose', { name: args.name, schedule: args.schedule, prompt: args.prompt }, r);
        else if (r.text === 'Yes.') {
          await f.callTool('owner_routine_create', {}, r);
          afterWrite = `aborted=${r.signal.aborted} current=${f.journal.isRunCurrent(r.runId)}`;
          const before = f.calls.length;
          await f.callTool('owner_routine_propose', { name: 'user-second', schedule: args.schedule, prompt: 'Never seen.' }, r).catch(() => undefined);
          secondCalls = f.calls.length - before;
        }
        return { text: 'ok', sourceRevision: (await f.sourceAuthority.read()).revision };
      } },
    rotateSession: async () => { }, postPrepared: async () => ({ acceptance: 'accepted', messageId: `m-${Math.random()}` }) });
  try {
    await c.receive(inbound('proposal', 'Every Friday summarize my loops'), context);
    await c.receive(inbound('yes', 'Yes.'), context);
    applyHook = undefined; await second;
    assert.equal(afterWrite, 'aborted=true current=false');
    assert.equal(secondCalls, 0);
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 1);
  } finally { applyHook = undefined; await c.close(); f.journal.close(); rmSync(folder, { recursive: true, force: true }); }
});
test('only protected writes defer a correction; an executing owner URL read is aborted at once', () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-write-kinds-')), journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  try {
    for (const [index, kind, deferred] of [[1, 'owner_web_read', false], [2, ownerRoutineToolKind, true]] as const) {
      const event = journal.admitInbound({ eventId: `e${index}`, accountKey: 'account', lineKey: 'line', threadKey: `t${index}`,
        channel: 'photon', sender: 'owner', payload: '{"text":"x"}' }).event;
      journal.startRun({ runId: `r${index}`, eventId: String(event.id) });
      journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,status,created_at,updated_at) VALUES(?,?,?,'h','executing',0,0)").run(`a${index}`, `r${index}`, kind);
      let aborted = false;
      journal.cancelAfterWrite(`r${index}`, () => { aborted = true; });
      assert.equal(aborted, !deferred, kind);
      assert.equal(journal.isRunCurrent(`r${index}`), false, kind);
    }
  } finally { journal.close(); rmSync(folder, { recursive: true, force: true }); }
});
