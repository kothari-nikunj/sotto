import {acceptedFixture} from './helpers/accepted-memory.js';
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { execFileSync } from 'node:child_process';
import { MockLanguageModelV4 } from 'ai/test';
import { ConversationSourceGuard, createSourceAuthorityReader, OWNER_ONLY_CONTEXT_PROOF, type SourceAuthority, unavailableAuthority, sameAuthority } from '../src/agent/source-authority.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { guardedSourceCall, SOURCE_FIELDS } from '../src/agent/source-tools.js';

const granted = (revision = 'a'.repeat(64)): SourceAuthority => ({ version: 2, revision, complete: true, bridge_sources: ['imessage'], google_sources: [], connector_sources: [] });

test('authority reader uses one bounded authenticated route and withholds unavailable or malformed evidence', async () => {
  const requests: Request[] = [];
  let response = JSON.stringify(granted());
  let status = 200;
  const reader = createSourceAuthorityReader({ bridgeUrl: 'http://127.0.0.1:43210/mcp', bearer: 'fixture-read',
    fetch: async (input, init) => { requests.push(new Request(input, init)); return new Response(response, { status }); } });
  assert.deepEqual(await reader.read(), granted());
  assert.equal(requests[0]!.url, 'http://127.0.0.1:43210/runtime/source-authority');
  assert.equal(requests[0]!.headers.get('authorization'), 'Bearer fixture-read');
  assert.equal(requests[0]!.redirect, 'error');
  assert.equal(await requests[0]!.text(), '{}');
  for (const malformed of ['secret provider failure', 'x'.repeat(8193), JSON.stringify({ ...granted(), bridge_sources: ['gmail'] }), JSON.stringify({ ...granted(), revision: null })]) {
    response = malformed;
    assert.deepEqual(await reader.read(), unavailableAuthority());
  }
  response = JSON.stringify(granted()); status = 503;
  assert.deepEqual(await reader.read(), unavailableAuthority());
  assert.throws(() => createSourceAuthorityReader({ bridgeUrl: 'https://remote.example/mcp', bearer: 'x' }));
});

test('transcript proof commits after deletion and detects off/on transitions with unchanged grants', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-source-baseline-'));
  const baselinePath = join(directory, 'baseline.json');
  let state = granted(); let purges = 0; let fail = true;
  const options = { baselinePath, reader: { read: async () => state }, purge: async () => { purges++; if (fail) throw new Error('deletion failed'); } };
  try {
    await assert.rejects(new ConversationSourceGuard(options).begin(), /deletion failed/);
    await assert.rejects(readFile(baselinePath), { code: 'ENOENT' });
    fail = false;
    const guard = new ConversationSourceGuard(options);
    await guard.begin(); assert.equal(purges, 2);
    await new ConversationSourceGuard(options).begin(); assert.equal(purges, 2, 'proof survives restart');
    state = granted('b'.repeat(64));
    await guard.begin(); assert.equal(purges, 3, 'off/on does not look like a no-op');
    const before = await guard.begin();
    state = unavailableAuthority();
    await assert.rejects(guard.finish(before), /withheld/);
    assert.equal(purges, 3, 'outage withholds the turn without erasing verified history');
    await guard.begin(); assert.equal(purges, 3, 'unknown authority suspends recall instead of purging');
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('source tools scope mixed reads and withhold revoked or unconsented content before model use', async () => {
  let state = granted(); const calls: unknown[] = [];
  const reader = { read: async () => state };
  const invoke = async (args: unknown) => { calls.push(args); return { content: [{ type: 'text', text: JSON.stringify({ imessage: ['allowed'], contacts: [] }) }] }; };
  const result = await guardedSourceCall({ name: 'read_local', args: {}, reader, revision: state.revision!, invoke });
  assert.deepEqual(calls, [{ sources: ['imessage'] }]);
  assert.ok(JSON.stringify(result).includes('allowed'));
  await assert.rejects(guardedSourceCall({ name: 'get_contacts', args: {}, reader, revision: state.revision!, invoke }), /withheld/);
  await assert.rejects(guardedSourceCall({ name: 'read_local', args: { sources: ['contacts'] }, reader, revision: state.revision!, invoke }), /withheld/);
  await assert.rejects(guardedSourceCall({ name: 'read_local', args: {}, reader, revision: state.revision!,
    invoke: async () => ({ content: [{ type: 'text', text: JSON.stringify({ contacts: ['private'] }) }] }) }), /withheld/);
  await assert.rejects(guardedSourceCall({ name: 'get_messages', args: {}, reader, revision: state.revision!,
    invoke: async () => { state = granted('b'.repeat(64)); return { content: [{ type: 'text', text: '{"messages":["private"]}' }] }; } }), /withheld/);
  await assert.rejects(guardedSourceCall({ name: 'get_messages', args: {}, reader, revision: state.revision!,
    invoke: async () => { throw new Error('private server traceback'); } }), error => !String(error).includes('private server'));
  assert.equal(calls.length, 1);
});

test('Bridge field projection matches the canonical Python source catalog', () => {
  const source = resolve('../../sotto-chief-of-staff/_shared/lib/source_catalog.py');
  const actual = JSON.parse(execFileSync('python3', ['-I', '-c', 'import json,runpy,sys; print(json.dumps(runpy.run_path(sys.argv[1])["SOURCE_FIELDS"]))', source], { encoding: 'utf8' }));
  assert.deepEqual(SOURCE_FIELDS, actual);
});

test('real Mastra history is purged before changed-authority prompts and withheld after an in-flight change', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-source-memory-'));
  const storagePath = join(directory, 'memory.db');
  let state = granted(); let changeDuringModel = false;
  const model = new MockLanguageModelV4({ doGenerate: async () => {
    if (changeDuringModel) state = granted('c'.repeat(64));
    return { content: [{ type: 'text', text: 'fixture answer' }], finishReason: { unified: 'stop', raw: 'STOP' },
      usage: { inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 2, text: 2, reasoning: 0 } }, warnings: [] };
  } });
  const authority = { read: async () => state };
  let runtime = createSottoAgentRuntime({ model, storagePath, instructions: 'Answer briefly.' });
  try {
    await acceptedFixture(runtime, { text: 'LEGACY PRIVATE HISTORY', threadId: 't1', ownerId: 'o1' });
    await runtime.close();
    runtime = createSottoAgentRuntime({ model, storagePath, instructions: 'Answer briefly.', sourceAuthority: authority });
    await acceptedFixture(runtime, { text: 'first authorized question', threadId: 't1', ownerId: 'o1' });
    assert.equal(JSON.stringify(model.doGenerateCalls.at(-1)?.prompt).includes('LEGACY PRIVATE'), false);
    await runtime.generateChat({ text: 'second question', threadId: 't1', ownerId: 'o1' });
    assert.equal(await runtime.canDeliverReplySource(), false, 'legacy prepared content has no policy proof');
    assert.equal(await runtime.canDeliverReplySource(state.revision!), true);
    assert.ok(JSON.stringify(model.doGenerateCalls.at(-1)?.prompt).includes('first authorized question'));
    await runtime.close();
    state = granted('b'.repeat(64));
    runtime = createSottoAgentRuntime({ model, storagePath, instructions: 'Answer briefly.', sourceAuthority: authority });
    assert.equal(await runtime.canDeliverReplySource('a'.repeat(64)), false, 'prepared content cannot bypass revocation on restart');
    await runtime.generateChat({ text: 'after off and on', threadId: 't1', ownerId: 'o1' });
    assert.equal(JSON.stringify(model.doGenerateCalls.at(-1)?.prompt).includes('first authorized question'), false);
    changeDuringModel = true;
    await assert.rejects(runtime.generateChat({ text: 'in-flight change', threadId: 't1', ownerId: 'o1' }), /withheld/);
    assert.equal((await runtime.memory.listThreads({ perPage: false })).threads.length, 0);
    changeDuringModel = false; state = unavailableAuthority();
    await runtime.generateChat({ text: 'fresh owner only question', threadId: 't1', ownerId: 'o1' });
    assert.equal(JSON.stringify(model.doGenerateCalls.at(-1)?.prompt).includes('after off and on'), false);
    const before = model.doGenerateCalls.length;
    await assert.rejects(runtime.generateUnattended({ text: 'background', runId: 'r1' }), /withheld/);
    assert.equal(model.doGenerateCalls.length, before, 'unattended source work waits for current authority');
  } finally { await runtime.close(); await rm(directory, { recursive: true, force: true }); }
});

test('temporary authority outage explains missing recall without deleting accepted conversation', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-context-gap-'));
  let state = granted();
  const model = new MockLanguageModelV4({ doGenerate: async () => ({
    content: [{ type: 'text', text: 'fixture answer' }], finishReason: { unified: 'stop', raw: 'STOP' },
    usage: { inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 2, text: 2, reasoning: 0 } }, warnings: [],
  }) });
  const runtime = createSottoAgentRuntime({model, storagePath:join(directory,'memory.db'),
    instructions:'Answer naturally.', sourceAuthority:{read:async()=>state}});
  const gap = 'Earlier conversation and source access could not be verified this turn';
  try {
    await acceptedFixture(runtime,{text:'PRIVATE_HISTORY_CONTEXT',threadId:'t',ownerId:'o'});
    state=unavailableAuthority();
    await runtime.generateChat({text:'What did she say?',threadId:'t',ownerId:'o'});
    const missing=JSON.stringify(model.doGenerateCalls.at(-1)?.prompt);
    assert.ok(missing.includes(gap));
    assert.equal(missing.includes('PRIVATE_HISTORY_CONTEXT'),false);
    state=granted();
    await runtime.generateChat({text:'What did she say?',threadId:'t',ownerId:'o'});
    const restored=JSON.stringify(model.doGenerateCalls.at(-1)?.prompt);
    assert.ok(restored.includes('PRIVATE_HISTORY_CONTEXT'));
    assert.equal(restored.includes(gap),false);
  } finally {await runtime.close();await rm(directory,{recursive:true,force:true});}
});

test('chat listing receives begin snapshot without a second authority read and final check still revokes', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-listing-snapshot-'));
  let state = granted(); let reads = 0; let revokeDuringModel = false;
  const seen: SourceAuthority[] = [];
  const model = new MockLanguageModelV4({ doGenerate: async () => {
    if (revokeDuringModel) state = granted('b'.repeat(64));
    return { content: [{ type: 'text', text: 'fixture answer' }], finishReason: { unified: 'stop', raw: 'STOP' },
      usage: { inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 },
        outputTokens: { total: 2, text: 2, reasoning: 0 } }, warnings: [] };
  } });
  const runtime = createSottoAgentRuntime({ model, storagePath: join(directory, 'memory.db'), instructions: 'Answer briefly.',
    sourceAuthority: { read: async () => { reads++; return state; } },
    chatTools: authority => { assert.equal(authority.sourceRevision, authority.listingAuthority?.revision);
      seen.push(authority.listingAuthority!); return {}; } });
  try {
    await runtime.generateChat({ text: 'hello', threadId: 't1', ownerId: 'o1' });
    assert.equal(reads, 2, 'begin and final checks only');
    assert.ok(seen.length >= 1);
    assert.deepEqual(seen[0], granted());
    revokeDuringModel = true;
    await assert.rejects(runtime.generateChat({ text: 'again', threadId: 't1', ownerId: 'o1' }), /withheld/);
    assert.equal(reads, 4, 'revocation still checked after generation');
  } finally { await runtime.close(); await rm(directory, { recursive: true, force: true }); }
});


test('removing the source reader purges source-derived history and withholds old prepared replies across restarts', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-source-removed-'));
  const storagePath = join(directory, 'memory.db');
  const model = new MockLanguageModelV4({ doGenerate: async () => ({
    content: [{ type: 'text', text: 'fixture answer' }], finishReason: { unified: 'stop', raw: 'STOP' },
    usage: { inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 2, text: 2, reasoning: 0 } }, warnings: [],
  }) });
  const options = { model, storagePath, instructions: 'Answer briefly.' };
  const authority = { read: async () => granted() };
  let runtime = createSottoAgentRuntime({ ...options, sourceAuthority: authority });
  try {
    const sourcedReply = await acceptedFixture(runtime, { text: 'SOURCE DERIVED HISTORY', threadId: 't1', ownerId: 'o1' });
    assert.equal(sourcedReply.sourceRevision, granted().revision);
    await runtime.close();
    runtime = createSottoAgentRuntime(options);
    assert.equal(await runtime.canDeliverReplySource(sourcedReply.sourceRevision), false);
    assert.equal(await runtime.canDeliverReplySource(), false, 'unknown legacy replies are not assumed owner-only');
    const ownerReply = await acceptedFixture(runtime, { text: 'OWNER ONLY HISTORY', threadId: 't1', ownerId: 'o1' });
    assert.equal(JSON.stringify(model.doGenerateCalls.at(-1)?.prompt).includes('SOURCE DERIVED'), false);
    assert.equal(ownerReply.sourceRevision, OWNER_ONLY_CONTEXT_PROOF);
    assert.equal(await runtime.canDeliverReplySource(ownerReply.sourceRevision), true);
    await runtime.close();
    runtime = createSottoAgentRuntime(options);
    await runtime.generateChat({ text: 'owner-only restart', threadId: 't1', ownerId: 'o1' });
    assert.ok(JSON.stringify(model.doGenerateCalls.at(-1)?.prompt).includes('OWNER ONLY HISTORY'));
    assert.equal(await runtime.canDeliverReplySource(sourcedReply.sourceRevision), false);
    await runtime.close();
    runtime = createSottoAgentRuntime({ ...options, sourceAuthority: authority });
    assert.equal(await runtime.canDeliverReplySource(ownerReply.sourceRevision), true);
    await runtime.generateChat({ text: 'reader restored', threadId: 't1', ownerId: 'o1' });
    assert.equal(JSON.stringify(model.doGenerateCalls.at(-1)?.prompt).includes('OWNER ONLY HISTORY'), false);
  } finally { await runtime.close(); await rm(directory, { recursive: true, force: true }); }
});

test('connector grants normalize legacy absence, reject unknown/duplicate values, and participate in authority equality',async()=>{
  let value: unknown={...granted(),connector_sources:['granola']};
  const reader=createSourceAuthorityReader({bridgeUrl:'http://127.0.0.1:43210/mcp',bearer:'fixture-read',fetch:async()=>new Response(JSON.stringify(value))});
  assert.deepEqual((await reader.read()).connector_sources,['granola']);
  value={...granted()};delete (value as Record<string,unknown>).connector_sources;
  assert.deepEqual((await reader.read()).connector_sources,[]);
  assert.ok(sameAuthority(granted(),value as SourceAuthority));
  assert.equal(sameAuthority(granted(),{...granted(),connector_sources:['granola']}),false);
  for(const connector_sources of [['unknown'],['granola','granola'],['gmail']]){
    value={...granted(),connector_sources};assert.deepEqual(await reader.read(),unavailableAuthority());
  }
});
test('Granola generation revision change purges prior transcript before reuse and withholds mid-run change',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'sotto-granola-baseline-'));let purges=0;
  let state:SourceAuthority={...granted(),bridge_sources:[],connector_sources:['granola']};
  const guard=new ConversationSourceGuard({baselinePath:join(directory,'baseline.json'),reader:{read:async()=>state},purge:async()=>{purges++;}});
  try{await guard.begin();assert.equal(purges,1);await guard.begin();assert.equal(purges,1);
    state={...state,revision:'b'.repeat(64)};const before=await guard.begin();assert.equal(purges,2);
    state={...state,revision:'c'.repeat(64)};await assert.rejects(guard.finish(before));assert.equal(purges,3);
  }finally{await rm(directory,{recursive:true,force:true});}
});

test('on-demand note bodies require current Notes authority before and after invocation', async () => {
  let state: SourceAuthority = { ...granted(), bridge_sources: ['apple_notes'] };
  const reader = { read: async () => state };
  const payload = { content: [{ type: 'text', text: '{"status":"ok","body":"Note text"}' }] };
  assert.ok(JSON.stringify(await guardedSourceCall({ name: 'get_note', args: { id: 7 }, reader,
    revision: state.revision!, invoke: async () => payload })).includes('Note text'));
  await assert.rejects(guardedSourceCall({ name: 'get_note', args: { id: 7 }, reader,
    revision: state.revision!, invoke: async () => { state = granted('b'.repeat(64)); return payload; } }), /withheld/);
  await assert.rejects(guardedSourceCall({ name: 'get_note', args: { id: 7 }, reader,
    revision: state.revision!, invoke: async () => { assert.fail('unconsented Notes read ran'); } }), /withheld/);
});

test('local search requires explicit consented scope and rejects mixed-source results', async () => {
  let state = granted();
  const reader = { read: async () => state };
  let calls = 0;
  const invoke = async () => { calls++; return { structuredContent: {
    status: 'ok', results: [{ source: 'imessage', source_id: '7', excerpt: 'October deadline', author: 'me', matched_fields: ['body'] }],
    coverage: { imessage: { status: 'ok', indexed: 1, skipped: 0, truncated: 0, stopped_early: false,
      newest_at: '2026-11-05T14:51:24Z', complete: true, since: 1, until: 2 } },
  } }; };
  const options = { name: 'search_local', reader, revision: state.revision!, invoke };
  for (const args of [{}, { sources: [] }, { sources: ['contacts'] }, { sources: ['imessage', 'whatsapp'] }]) {
    await assert.rejects(guardedSourceCall({ ...options, args }), /withheld/);
  }
  assert.equal(calls, 0);
  const args = { sources: ['imessage'], query: 'October', since: 1, until: 2 };
  const result = await guardedSourceCall({ ...options, args });
  assert.ok(JSON.stringify(result).includes('October deadline'));
  for (const payload of [
    { results: [{ source: 'whatsapp', excerpt: 'blocked' }], coverage: {} },
    { results: [], coverage: { whatsapp: { status: 'ok' } } },
    { results: [], coverage: {}, private: 'blocked' },
    { results: [{ source: 'imessage', unscoped: 'blocked' }], coverage: {} },
  ]) {
    await assert.rejects(guardedSourceCall({ ...options, args, invoke: async () => ({ structuredContent: payload }) }), /withheld/);
  }
  await assert.rejects(guardedSourceCall({ ...options, args, invoke: async () => {
    state = { ...granted('b'.repeat(64)), bridge_sources: [] }; return invoke();
  } }), /withheld/);
});

test('snapshot coverage is scoped to requested and consented sources', async () => {
  const state = granted();
  let coverage: Record<string, unknown> = { imessage: { limit_reached: true, content_partial: true } };
  const call = () => guardedSourceCall({ name: 'read_local', args: { sources: ['imessage'] },
    revision: state.revision!, reader: { read: async () => state },
    invoke: async () => ({ structuredContent: { source_coverage: coverage } }) });
  assert.match(JSON.stringify(await call()), /limit_reached/);
  coverage = { deferred_unread_whatsapp: { rows_scanned: 1 } };
  await assert.rejects(call(), /withheld/);
  coverage = { imessage: { body: 'unexpected content' } };
  await assert.rejects(call(), /withheld/);
});

test('negotiated coverage reaches the model without losing precise gaps or accepting content fields', async () => {
  const state = granted();
  const detail = {observed_at:'2026-10-21T10:00:00Z',status:'partial',rows_scanned:30,scan_observed:true,
    limit_reached:true,content_partial:false,bounded:true,
    requested_window:{since:'2026-10-20T10:00:00Z',until:'2026-10-21T10:00:00Z'},
    effective_window:{since:'2026-10-20T10:00:00Z',until:'2026-10-21T10:00:00Z',basis:'sent'},
    last_successful_read_at:'2026-10-21T10:00:00Z',rows_returned:30,query_row_caps:[30],omitted_rows:null,
    failure_category:null,components:{deferred_unread:{status:'failed',failure_category:'query'}},
    attachments:{metadata:'partial',body:'not_read'}};
  const call = (coverage:unknown) => guardedSourceCall({name:'read_local',args:{sources:['imessage']},
    revision:state.revision!,reader:{read:async()=>state},
    invoke:async()=>({structuredContent:{source_coverage:coverage}})});
  const output=JSON.stringify(await call({imessage:detail}));
  assert.match(output,/last_successful_read_at/);
  assert.match(output,/deferred_unread/);
  const invalid = [
    {...detail,components:{deferred_unread:{status:'failed',failure_category:'PRIVATE ERROR TEXT'}}},
    {...detail,components:{private_identifier:{status:'ok',failure_category:null}}},
    {...detail,effective_window:{...detail.effective_window,body:'PRIVATE SOURCE CONTENT'}},
    {...detail,last_successful_read_at:'PRIVATE SOURCE CONTENT'},
    {...detail,last_successful_read_at:'2026-10-21T10:00Z'},
    {...detail,effective_window:{...detail.effective_window,basis:'due_or_modified'}},
    {...detail,rows_returned:-1},
    {...detail,query_row_caps:[30,30]},
    {...detail,attachments:{metadata:'available',body:'complete'}},
  ];
  for(const bad of invalid)await assert.rejects(call({imessage:bad}),/withheld/);
  const source = resolve('../../sotto-chief-of-staff/_shared/lib/source_catalog.py');
  const pythonValidity = JSON.parse(execFileSync('python3', ['-I', '-c',
    'import json,runpy,sys; validate=runpy.run_path(sys.argv[1])["valid_source_coverage"]; print(json.dumps([validate(v) for v in json.load(sys.stdin)]))', source],
    { encoding: 'utf8', input: JSON.stringify([detail, ...invalid]) }));
  assert.deepEqual(pythonValidity, [true, ...invalid.map(() => false)]);
  await assert.rejects(call({whatsapp:detail}),/withheld/);
});

test('receiver-inspected synthetic receipts survive the real model tool projection', async () => {
  const probe = resolve('../../sotto-chief-of-staff/evals/inspect_source_coverage.py');
  const report = JSON.parse(execFileSync('python3', [probe], { encoding: 'utf8' }));
  const state = granted();
  for (const payload of [report.primary_payload, report.legacy_payload]) {
    const result = await guardedSourceCall({ name: 'read_local', args: { sources: ['imessage'] },
      revision: state.revision!, reader: { read: async () => state },
      invoke: async () => ({ structuredContent: payload }) });
    assert.deepEqual(JSON.parse((result as { content: { text: string }[] }).content[0]!.text), payload);
  }
  const rustPayload = report.rust_fixture.payload;
  const rustSources = Object.keys(rustPayload.source_coverage).filter(source => !source.startsWith('deferred_unread_'));
  const rustAuthority = { ...state, bridge_sources: rustSources as SourceAuthority['bridge_sources'] };
  const rustOutput = await guardedSourceCall({ name: 'read_local', args: { sources: rustSources },
    revision: state.revision!, reader: { read: async () => rustAuthority },
    invoke: async () => ({ structuredContent: rustPayload }) });
  assert.deepEqual(JSON.parse((rustOutput as { content: { text: string }[] }).content[0]!.text), rustPayload);
  const contacts: SourceAuthority = { ...state, bridge_sources: ['contacts'] };
  await assert.rejects(guardedSourceCall({ name: 'read_local', args: { sources: ['contacts'] },
    revision: contacts.revision!, reader: { read: async () => contacts },
    invoke: async () => ({ structuredContent: { source_coverage: { deferred_unread_contacts: { status: 'ok' } } } }) }), /withheld/);
});

test('an unverifiable initial baseline never exposes retained history and verification still purges it',async()=>{
  const directory=await mkdtemp(join(tmpdir(),'unknown-memory-baseline-'));
  const storagePath=join(directory,'memory.db');let state=granted();
  const model=new MockLanguageModelV4({doGenerate:async()=>({content:[{type:'text',text:'PRIVATE OLD ANSWER'}],
    finishReason:{unified:'stop',raw:'STOP'},usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]})});
  const runtime=createSottoAgentRuntime({model,storagePath,instructions:'Answer.',sourceAuthority:{read:async()=>state}});
  try {
    await acceptedFixture(runtime,{text:'PRIVATE OLD REQUEST',threadId:'t',ownerId:'o'});
    await rm(storagePath+'.source-authority.json');
    state=unavailableAuthority();await runtime.generateChat({text:'fresh question',threadId:'t',ownerId:'o'});
    assert.equal(JSON.stringify(model.doGenerateCalls.at(-1)!.prompt).includes('PRIVATE OLD'),false);
    state=granted();await runtime.generateChat({text:'verified question',threadId:'t',ownerId:'o'});
    assert.equal(JSON.stringify(model.doGenerateCalls.at(-1)!.prompt).includes('PRIVATE OLD'),false);
  }finally{await runtime.close();await rm(directory,{recursive:true,force:true});}
});

 test('conflicting verified grants purge even if a faulty authority reuses its revision',async()=>{
 const directory=await mkdtemp(join(tmpdir(),'conflicting-source-proof-'));let state=granted(),purges=0;
 const guard=new ConversationSourceGuard({baselinePath:join(directory,'baseline.json'),reader:{read:async()=>state},purge:async()=>{purges++;}});
 try {const before=await guard.begin();state={...state,google_sources:['gmail']};await assert.rejects(guard.finish(before));assert.equal(purges,2);}
 finally{await rm(directory,{recursive:true,force:true});}
 });
