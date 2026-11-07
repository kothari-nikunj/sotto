import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { RequestContext } from '@mastra/core/request-context';
import { MockLanguageModelV4 } from 'ai/test';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator, type ModelRequest } from '../src/conversation.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createOwnerPreferenceToolClient } from '../src/tools/owner-preference-tool-client.js';
import { createOwnerPreferenceTools } from '../src/tools/owner-preference-tool.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { ownerPreferenceToolKind } from '../src/tools/owner-preference-tool-contract.js';

const identity = {tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64)};
const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: 'owner'};
const context = {threadId: 'thread', destination: 'owner', lineKey: 'line'};
const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: 'owner',
  isDirect: true, eventId, text});
const args = {operation: 'add' as const, field: 'mute_senders' as const, value: 'News@Example.COM'};
const authority = {version: 2 as const, complete: true, revision: 'd'.repeat(64), bridge_sources: [], google_sources: []};
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), {status});
const usage = {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}};
function fixture(folder: string) {
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  const calls: Record<string, unknown>[] = [], receipts = new Map<string, Record<string, unknown>>();
  let loseCreate = false, hideInspect = false, state = authority;
  const client = createOwnerPreferenceToolClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string); calls.push(body);
      assert.equal(body.text_hash, createHash('sha256').update(body.text).digest('hex')); assert.ok(!body.text.startsWith('/loop'));
      if (body.operation === 'read') return reply({version: 1, status: 'read', event_id: body.event_id, payload_hash: body.payload_hash,
        coverage: 'explicit_only', preferences: {mute_senders: [], mute_people: [], mute_sections: [], tone_notes: [], vip_people: []},
        nudge_budget: {effective: 4, ceiling: 4}, truncated_fields: []});
      if (body.operation === 'inspect') return reply(!hideInspect && receipts.get(body.action_id) || {version: 1, status: 'not_found'});
      const binding = stableActionIdentity(body.event_id, ownerPreferenceToolKind, body.args);
      const receipt = {version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
        payload_hash: body.payload_hash, args_hash: binding.payloadHash, outcome: {operation: body.args.operation, field: body.args.field,
          value: body.args.field === 'mute_senders' ? body.args.value.toLowerCase() : body.args.value, present: body.args.operation === 'add'}};
      receipts.set(body.action_id, receipt); if (loseCreate) throw new Error('lost after commit'); return reply(receipt);
    }});
  const sourceAuthority = {read: async () => state};
  const tools = createOwnerPreferenceTools({client, journal: () => journal, sourceAuthority});
  const callTool = async (name: keyof typeof tools, input: unknown, request: ModelRequest, overrides: Record<string, unknown> = {}) => {
    const requestContext = new RequestContext();
    for (const [key, value] of Object.entries({mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey,
      sourceRevision: state.revision, allowOwnerPreference: request.allowOwnerPreference, ...overrides})) requestContext.set(key, value);
    return tools[name].execute!(input as never, {requestContext, abortSignal: request.signal} as never);
  };
  return {get journal() {return journal;}, client, calls, tools, sourceAuthority, callTool,
    set loseCreate(value: boolean) {loseCreate = value;}, set hideInspect(value: boolean) {hideInspect = value;},
    revoke() {state = {...authority, revision: 'e'.repeat(64)};},
    reopen() {journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted();}};
}



test('real Mastra explicit read/change records factual ACK after source revocation; completed effect cannot replay', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-pref-mastra-')), f = fixture(folder), sent: string[] = []; let step = 0;
  const model = new MockLanguageModelV4({doGenerate: async () => {
    step++;
    if (step <= 2) return {content: [{type: 'tool-call', toolCallId: `call-${step}`, toolName: step === 1 ? 'owner_preferences' : 'owner_preference_change', input: JSON.stringify(step === 1 ? {} : args)}],
      finishReason: {unified: 'tool-calls', raw: 'tool_calls'}, usage, warnings: []};
    f.revoke(); return {content: [{type: 'text', text: 'Unsafe source-derived preference prose'}], finishReason: {unified: 'stop', raw: 'STOP'}, usage, warnings: []};
  }});
  const runtime = createSottoAgentRuntime({model, storagePath: join(folder, 'memory.db'), instructions: 'Use explicit preference tools',
    sourceAuthority: f.sourceAuthority, chatTools: async () => f.tools});
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerPreferenceTool: f.client,
    model: {generate: request => runtime.generateChat({...request, threadId: 'memory', ownerId: 'owner'})},
    postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: 'ack'};}});
  try {
    const sentence = `Mute sender ${args.value}`;
    assert.equal((await coordinator.receive(inbound('mute', sentence), context)).status, 'accepted');
    assert.equal(step, 3); assert.deepEqual(f.calls.map(row => row.operation), ['read', 'apply']);
    assert.deepEqual(sent, ['Recorded your explicit sender mute.']);
    assert.equal((await coordinator.receive(inbound('mute', sentence), context)).status, 'duplicate');
    await coordinator.close(); await runtime.close(); f.reopen();
    const recovered = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerPreferenceTool: f.client,
      model: {generate: async () => {throw new Error('replayed');}}, postPrepared: async () => {throw new Error('resent');}});
    assert.deepEqual(await recovered.recover(), []); f.journal.pruneTerminal(Date.now() + 1000);
    assert.equal(f.journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_preference_tool'").get()!.n, 1);
    assert.ok(!JSON.stringify(model.doGenerateCalls).includes('c'.repeat(64)));
  } finally {await runtime.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('uncertain preference holds only its input; reopened journal inspects exact args while safe chat and new preference proceed', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-pref-held-')), f = fixture(folder), sent: string[] = [];
  f.loseCreate = true; f.hideInspect = true;
  const first = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerPreferenceTool: f.client,
    model: {generate: async request => {await f.callTool('owner_preference_change', args, request); return 'no';}}, postPrepared: async () => {throw new Error('uncertain ack');}});
  try {
    const sentence = `Mute ${args.value}`;
    assert.equal((await first.receive(inbound('held', sentence), context)).status, 'held');
    const held = f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_preference_tool'").get()!;
    await first.close(); f.reopen(); f.loseCreate = false;
    const second = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerPreferenceTool: f.client,
      model: {generate: async request => {if (request.text.includes('Keep it short')) await f.callTool('owner_preference_change', {operation: 'add', field: 'tone_notes', value: 'Keep it short'}, request); return 'safe answer';}},
      postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    assert.equal((await second.recover())[0]?.status, 'held');
    assert.equal((await second.receive(inbound('safe', 'Hello safely'), context)).status, 'accepted');
    assert.equal((await second.receive(inbound('tone', 'Tone: Keep it short'), context)).status, 'accepted');
    assert.equal(f.journal.getAction(held.id as string)?.status, 'unknown');
    f.hideInspect = false;
    f.journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(held.id);
    assert.equal((await second.recover())[0]?.status, 'accepted');
    assert.equal(f.calls.filter(row => row.operation === 'apply').length, 2);
    for (const call of f.calls.filter(row => row.operation === 'inspect')) assert.deepEqual(call.args, args);
  } finally {f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('preference tool refuses spoofed, unattended, stale, nonowner span, changed/composite args and batched authority', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-pref-auth-')), f = fixture(folder);
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerPreferenceTool: f.client,
    model: {generate: async request => {
      for (const overrides of [{mode: 'unattended'}, {runId: 'spoof'}, {ownerRequestKey: 'spoof'}, {allowOwnerPreference: false}, {sourceRevision: 'e'.repeat(64)}])
        await assert.rejects(f.callTool('owner_preference_change', args, request, overrides));
      await assert.rejects(f.callTool('owner_preference_change', {...args, value: 'source@secret.test'}, request));
      for (const invalid of [
        {operation: 'add', value: args.value}, {operation: 'remove', field: 'mute_senders'},
        {operation: 'clear-tone', field: 'tone_notes'}, {operation: 'nudge-budget'},
        {operation: 'nudge-budget', direction: 'no', value: args.value}, {...args, direction: 'fewer'},
      ]) await assert.rejects(f.callTool('owner_preference_change', invalid, request));
      assert.equal(f.calls.length, 0); await f.callTool('owner_preferences', {}, request); await f.callTool('owner_preference_change', args, request);
      await assert.rejects(f.callTool('owner_preference_change', {operation: 'clear-tone'}, request));
      const authority = {runId: request.runId, accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon' as const, scopeHash: f.client.scopeHash};
      assert.throws(() => f.journal.prepareOwnerTaskToolAction(authority, request.ownerRequestKey, {operation: 'add', text: args.value}));
      assert.throws(() => f.journal.prepareOwnerReminderToolAction(authority, request.ownerRequestKey, {due: '2026-10-09T08:15:00Z', action: args.value}));
      assert.equal(f.calls.length, 2); return 'not used';
    }}, postPrepared: async () => ({acceptance: 'accepted', messageId: 'ack'})});
  try {assert.equal((await coordinator.receive(inbound('explicit', `Mute ${args.value}`), context)).status, 'accepted');}
  finally {f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('budget refusal preserves unknown preference hold and committed preference acknowledgment',async()=>{
  const {ManagedProxyBudgetExhausted}=await import('../src/agent/models.js');
  for(const lost of [false,true]){
    const folder=mkdtempSync(join(tmpdir(),'preference-budget-')),f=fixture(folder),sent:string[]=[];
    f.loseCreate=lost;f.hideInspect=lost;
    const service=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerPreferenceTool:f.client,
      model:{generate:async request=>{try{await f.callTool('owner_preference_change',args,request);}catch{/* original action remains authoritative */}throw new ManagedProxyBudgetExhausted();}},
      postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'ack'};}});
    try{assert.equal((await service.receive(inbound('mute',`Mute ${args.value}`),context)).status,lost?'held':'accepted');
      if(lost){assert.equal(sent.length,1);assert.match(sent[0]!,/^I couldn’t confirm whether the preference change/);}else assert.deepEqual(sent,['Recorded your explicit sender mute.']);assert.equal(f.calls.filter(row=>row.operation==='apply').length,1);}
    finally{await service.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
  }
});


test('one-off wording edits reject native preference writes before journal admission or receiver calls', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-pref-rewrite-')), f = fixture(folder), sent: string[] = [];
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerPreferenceTool: f.client,
    model: {generate: async request => {
      await assert.rejects(f.callTool('owner_preference_change', {operation: 'add', field: 'tone_notes', value: request.text}, request), /one-off rewrite request/);
      return 'Here is the shorter version.';
    }}, postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `reply-${sent.length}`};}});
  try {
    for (const [i, text] of ['shorter pls', 'Shorter!', 'please make it shorter', 'make this more concise, please', 'more terse', 'rewrite that', 'rephrase this pls',
      'can you make that shorter?', 'make it a little shorter', 'too long', 'tighten it up', 'be more concise'].entries())
      assert.equal((await coordinator.receive(inbound(`rewrite-${i}`, text), context)).status, 'accepted');
    assert.equal(f.calls.length, 0);
    assert.equal(f.journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_preference_tool'").get()!.n, 0);
    assert.ok(sent.every(text => text === 'Here is the shorter version.'));
  } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});
