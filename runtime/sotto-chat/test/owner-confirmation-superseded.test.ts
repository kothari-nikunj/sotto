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

for (const kind of ['scheduled_text', 'interactive'] as const)
test(`a ${kind} send between a review and "Sure." voids the review; a fresh review then confirms`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-superseded-review-')), f = fixture(folder);
  let rejected = 0;
  const c = new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerRoutineTool: f.client,
    model: { generate: async (r) => {
        if (r.text.startsWith('Every')) await f.callTool('owner_routine_propose', { name: args.name, schedule: args.schedule, prompt: args.prompt }, r);
        else if (r.text === 'Sure.') await f.callTool('owner_routine_create', {}, r).catch(() => { rejected++; });
        return { text: 'ok', sourceRevision: (await f.sourceAuthority.read()).revision };
      } },
    rotateSession: async () => { }, postPrepared: async () => ({ acceptance: 'accepted', messageId: 'ack' }) });
  try {
    await c.receive(inbound('proposal', 'Every Friday summarize my loops'), context);
    f.journal.prepareDispatch({ dispatchId: 'nudge-1', kind, destination: 'owner', payload: JSON.stringify({text:'Dana is waiting on the deck. Want me to draft a reply?'}),
      routeDigest: 'f'.repeat(64), sourceRevision: authority.revision, maxAttempts: kind === 'scheduled_text' ? 3 : 1 } as never);
    const attempt = f.journal.beginDispatch('nudge-1', 1);
    f.journal.settleDispatch('nudge-1', Number(attempt.number), 'accepted', 'nudge-msg', ['nudge-msg'], Date.now() + 1);
    await c.receive(inbound('yes', 'Sure.'), context);
    assert.equal(rejected, 1);
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 0, 'a yes after a later send must not confirm the older review');
    await c.receive(inbound('proposal-2', 'Every Friday summarize my loops'), context);
    await c.receive(inbound('yes-2', 'Sure.'), context);
    assert.equal(rejected, 1);
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 1, 'the re-shown review is the latest message, so its yes confirms');
  } finally { await c.close(); f.journal.close(); rmSync(folder, { recursive: true, force: true }); }
});

test('a nudge accepted while the review is still at the provider voids the review', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-overlapped-review-')), f = fixture(folder);
  const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
  let rejected = 0, posts = 0;
  const c = new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerRoutineTool: f.client,
    model: { generate: async (r) => {
        if (r.text.startsWith('Every')) await f.callTool('owner_routine_propose', { name: args.name, schedule: args.schedule, prompt: args.prompt }, r);
        else if (r.text === 'Sure.') await f.callTool('owner_routine_create', {}, r).catch(() => { rejected++; });
        return { text: 'ok', sourceRevision: (await f.sourceAuthority.read()).revision };
      } },
    rotateSession: async () => { },
    postPrepared: async () => {
      if (posts++ === 0) {   // the review is at the provider; a nudge leaves on the same route and is accepted first
        await sleep(5);
        f.journal.prepareDispatch({ dispatchId: 'nudge-1', kind: 'scheduled_text', destination: 'owner', payload: JSON.stringify({text:'Dana is waiting on the deck. Want me to draft a reply?'}),
          routeDigest: 'f'.repeat(64), sourceRevision: authority.revision, maxAttempts: 3 } as never);
        const attempt = f.journal.beginDispatch('nudge-1', 1);
        f.journal.settleDispatch('nudge-1', Number(attempt.number), 'accepted', 'nudge-msg', ['nudge-msg'], Date.now());
        await sleep(5);
      }
      return { acceptance: 'accepted', messageId: 'ack' + posts };
    } });
  try {
    await c.receive(inbound('proposal', 'Every Friday summarize my loops'), context);
    await c.receive(inbound('yes', 'Sure.'), context);
    assert.equal(rejected, 1);
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 0, 'a yes must not confirm a review that a later-started send overlapped');
  } finally { await c.close(); f.journal.close(); rmSync(folder, { recursive: true, force: true }); }
});
