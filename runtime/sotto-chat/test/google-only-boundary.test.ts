import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import test from 'node:test';
import { MockLanguageModelV4 } from 'ai/test';
import type { GatewayAdapter } from '../src/channels/gateway.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { CALENDAR_EXECUTION_GUIDE } from '../src/tools/owner-calendar-tool.js';
import { createGoogleReadTools, readOwnerGoogle } from '../src/agent/google-read.js';
import { startConfiguredRuntime, type RuntimeAgent } from '../src/bootstrap/main.js';
import type { SourceAuthority } from '../src/agent/source-authority.js';

const granted = (revision = 'b'.repeat(64)): SourceAuthority => ({ version: 2, revision,
  complete: true, bridge_sources: [], google_sources: ['gmail'] });

test('a v1 transcript baseline is purged before a v2 Google-only model prompt', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-google-v1-history-'));
  const storagePath = join(directory, 'memory.db');
  const model = new MockLanguageModelV4({ doGenerate: async () => ({
    content: [{ type: 'text', text: 'fixture response' }],
    finishReason: { unified: 'stop', raw: 'STOP' },
    usage: { inputTokens: { total: 3, noCache: 3, cacheRead: 0, cacheWrite: 0 },
      outputTokens: { total: 2, text: 2, reasoning: 0 } }, warnings: [],
  }) });
  let runtime = createSottoAgentRuntime({ model, storagePath, instructions: 'Offline test.' });
  try {
    await runtime.generateChat({ text: 'LEGACY PRIVATE GMAIL CONTENT', threadId: 'thread', ownerId: 'owner' });
    await runtime.close();
    // The old baseline file format had version 1 and a revision with no Google dimension.
    await writeFile(`${storagePath}.source-authority.json`, JSON.stringify({ version: 1,
      revision: 'a'.repeat(64) }), { mode: 0o600 });
    runtime = createSottoAgentRuntime({ model, storagePath, instructions: 'Offline test.',
      sourceAuthority: { read: async () => granted() } });
    const result = await runtime.generateChat({ text: 'What can I read now?', threadId: 'thread', ownerId: 'owner' });
    assert.equal(result.sourceRevision, granted().revision);
    assert.equal(JSON.stringify(model.doGenerateCalls.at(-1)?.prompt).includes('LEGACY PRIVATE GMAIL CONTENT'), false);
    assert.equal(JSON.parse(await readFile(`${storagePath}.source-authority.json`, 'utf8')).revision,
      granted().revision);
  } finally { await runtime.close(); await rm(directory, { recursive: true, force: true }); }
});

test('configured receiver selects Google chat tools while the Mac Bridge is offline', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sotto-google-only-tools-'));
  const mediaRoot = join(directory, 'media');
  await mkdir(mediaRoot, { recursive: true, mode: 0o700 });
  const configPath = join(directory, 'runtime.json');
  await writeFile(configPath, JSON.stringify({ version: 1, channel: 'telegram',
    stateDirectory: join(directory, 'state'), mediaRoot, port: 0,
    bearerToken: 'fixture-runtime-bearer-is-at-least-32-characters',
    owner: { accountId: 'owner-account', ownerId: '123' }, lineKey: 'primary',
    target: 'owner', providerThreadId: 'telegram:123',
    credentials: { telegramToken: 'synthetic-token', telegramLinkFile: {
      bot_token: 'synthetic-token', allowed_user: 123 } },
    instructions: 'Offline test.', modelProxy: { origin: 'https://proxy.example',
      bearer: 'synthetic-proxy', chatModelId: 'fake-chat', nativeMediaModelId: 'fake-media' },
    bridge: { url: 'http://127.0.0.1:8787/mcp', readBearer: 'receiver-read-bearer' },
  }), { mode: 0o600 });
  let tools: { chat(revision?: string): Promise<Record<string, unknown>>;
    unattended(revision?: string): Promise<Record<string, unknown>> } | undefined;
  const calls: string[] = [];
  const adapter = { name: 'telegram', userName: 'fixture', initialize: async () => {},
    disconnect: async () => {}, stopPolling: async () => {}, isDM: () => true,
    channelIdFromThreadId: () => 'telegram:123', decodeThreadId: (value: string) => value,
    encodeThreadId: (value: unknown) => String(value), postMessage: async () => ({ id: 'unused' }),
    fetchMessages: async () => ({ messages: [], hasMore: false }),
    addReaction: async () => {}, removeReaction: async () => {}, deleteMessage: async () => {},
    editMessage: async () => ({ id: 'unused' }), startTyping: async () => {}, renderFormatted: () => '',
  } as unknown as GatewayAdapter;
  const agent: RuntimeAgent = { generateChat: async () => ({ text: '' }),
    generateUnattended: async () => ({ text: '' }), rotateThread: async () => {},
    purgeThreadsBefore: async () => 0, close: async () => {} };
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  try {
    runtime = await startConfiguredRuntime(configPath, {
      createAdapter: () => adapter,
      createTransport: () => ({ send: async () => ({ acceptance: 'not_attempted', providerMessageIds: [] }) }),
      createAgent: (_config, _model, _media, _skills, selected) => {
        tools = selected as typeof tools;
        return agent;
      },
      openStateDatabase: () => new DatabaseSync(':memory:'),
      listenApplication: async () => 32145,
      fetch: async input => {
        const path = new URL(String(input)).pathname;
        calls.push(path);
        if (path === '/runtime/source-authority') return new Response(JSON.stringify(granted()));
        if (path === '/runtime/google-read') return new Response(JSON.stringify({ version: 1,
          source: 'gmail', operation: 'gmail_search', items: [], observation: {
            status: 'ok', complete: true, observed_at: '2026-09-30T00:00:00Z',
            coverage: { since: null, until: null },
          } }));
        throw new Error('Mac Bridge is offline');
      },
    });
    assert.ok(tools);
    const chat = await tools.chat(granted().revision!);
    assert.ok(chat.gmail_search);
    assert.ok(chat.gmail_get);
    assert.equal(chat.calendar_list, undefined);
    assert.equal(chat.bridge_read_local, undefined);
    const gmail = chat.gmail_search as { execute(input: unknown, context: unknown): Promise<unknown> };
    const context = { requestContext: { get: (key: string) => ({ mode: 'chat', runId: 'run',
      ownerRequestKey: 'event', sourceRevision: granted().revision })[key as 'mode'] } };
    const result = await gmail.execute({ query: 'in:inbox', max: 4 }, context) as { source: string; items: unknown[] };
    assert.equal(result.source, 'gmail');
    assert.deepEqual(result.items, []);
    const unattended = await tools.unattended(granted().revision!);
    assert.deepEqual(Object.keys(unattended).sort(), ['gmail_get','gmail_search','owner_open_loops','run_sotto_script']);
    assert.deepEqual(Object.keys(await tools.unattended('c'.repeat(64))), ['owner_open_loops']);
    const scheduled = unattended.gmail_search as typeof gmail;
    const scheduledContext = { requestContext: { get: (key: string) => ({mode:'unattended',runId:'routine',sourceRevision:granted().revision})[key as 'mode'] } };
    assert.deepEqual((await scheduled.execute({query:'in:inbox',max:4},scheduledContext) as {items:unknown[]}).items, []);
    await assert.rejects(scheduled.execute({query:'in:inbox',max:4},context), /unavailable/);
    assert.equal(calls.includes('/mcp'), false, 'Mac discovery was not required for Google');
    await assert.rejects(gmail.execute({ query: 'in:inbox', max: 4 }, {
      requestContext: { get: () => 'unattended' },
    }), /unavailable/);
  } finally { await runtime?.close(); await rm(directory, { recursive: true, force: true }); }
});

test('a Google result is withheld when source authority changes during the read', async () => {
  let authority = granted();
  let reads = 0;
  const options = { bridgeUrl: 'http://127.0.0.1:8787/mcp', readBearer: 'receiver-read-bearer',
    sourceAuthority: { read: async () => authority },
    fetch: async () => {
      reads++;
      authority = granted('c'.repeat(64));
      return new Response(JSON.stringify({ version: 1, source: 'gmail', operation: 'gmail_search',
        items: [], observation: { status: 'ok', complete: true,
          observed_at: '2026-09-30T00:00:00Z', coverage: { since: null, until: null } } }));
    },
  };
  await assert.rejects(readOwnerGoogle(options, 'gmail_search', { query: 'in:inbox', max: 4 },
    granted().revision!), /unavailable/);
  assert.equal(reads, 1, 'the provider result reached Node but was withheld before model use');
});


test('Calendar accepts explicit offsets and UTC while rejecting invalid windows before receiver contact', async () => {
  const authority = {...granted(), google_sources: ['calendar'] as ('calendar')[]};
  const requests: Record<string, unknown>[] = [];
  const options = {bridgeUrl: 'http://127.0.0.1:8787/mcp', readBearer: 'receiver-read-bearer',
    sourceAuthority: {read: async () => authority},
    fetch: async (_input: unknown, init?: RequestInit) => {
      requests.push(JSON.parse(init!.body as string));
      return Response.json({version: 1, source: 'calendar', operation: 'calendar_list', items: [],
        observation: {status: 'ok', complete: true, observed_at: '2026-10-04T09:00:00Z',
          coverage: {since: '2026-10-04T07:00:00Z', until: '2026-10-05T07:00:00Z'}}});
    }};
  for (const args of [
    {start: '2026-10-04T00:00:00-07:00', end: '2026-10-05T00:00:00-07:00'},
    {start: '2026-10-04T07:00:00Z', end: '2026-10-05T07:00:00Z'},
  ]) await readOwnerGoogle(options, 'calendar_list', args, authority.revision!);
  assert.deepEqual(requests.map(request => request.arguments), [
    {start: '2026-10-04T07:00:00Z', end: '2026-10-05T07:00:00Z'},
    {start: '2026-10-04T07:00:00Z', end: '2026-10-05T07:00:00Z'},
  ]);
  for (const args of [
    {start: '2026-10-04T00:00:00', end: '2026-10-05T00:00:00'},
    {start: '2026-02-30T00:00:00Z', end: '2026-03-01T00:00:00Z'},
    {start: '2026-10-04T00:00:00+25:00', end: '2026-10-05T00:00:00Z'},
    {start: '0001-01-01T00:00:00+01:00', end: '0001-01-02T00:00:00Z'},
    {start: '2026-10-04T00:00:00.123Z', end: '2026-10-05T00:00:00Z'},
    {start: '2026-10-05T00:00:00Z', end: '2026-10-04T00:00:00Z'},
    {start: '2026-10-04T00:00:00Z', end: '2026-11-05T00:00:00Z'},
  ]) await assert.rejects(readOwnerGoogle(options, 'calendar_list', args, authority.revision!), /unavailable/);
  assert.equal(requests.length, 2, 'invalid windows never contact the receiver');
});

test('exact Calendar get carries a known selector and refuses mismatched target echoes',async()=>{
 const authority={...granted(),google_sources:['calendar'] as ('calendar')[]};let contacts=0;let echo='Team#Cal%40example.com';let eventId='event_one';
 const options={bridgeUrl:'http://127.0.0.1:8787/mcp',readBearer:'receiver-read-bearer',sourceAuthority:{read:async()=>authority},fetch:async(_input:unknown,init?:RequestInit)=>{contacts++;const body=JSON.parse(init!.body as string);assert.equal(body.arguments.calendar_id,'Team#Cal%40example.com');return Response.json({version:1,source:'calendar',operation:'calendar_get',item:{id:eventId,calendar_id:echo,summary:'Team meeting',description:'',location:'',status:'confirmed',start:{dateTime:'2026-10-10T10:00:00Z'},end:{dateTime:'2026-10-10T11:00:00Z'},attendees:[]},observation:{status:'ok',complete:true,observed_at:'2026-10-06T10:00:00Z',coverage:{since:null,until:null}}});}};
 const input={event_id:'event_one',calendar_id:'Team#Cal%40example.com'};
 await readOwnerGoogle(options,'calendar_get',input,authority.revision!);
 echo='primary';await assert.rejects(readOwnerGoogle(options,'calendar_get',input,authority.revision!),/unavailable/);
 echo=input.calendar_id;eventId='other';await assert.rejects(readOwnerGoogle(options,'calendar_get',input,authority.revision!),/unavailable/);
 for(const calendar_id of [' bad','bad calendar','bad\u0085'])await assert.rejects(readOwnerGoogle(options,'calendar_get',{...input,calendar_id},authority.revision!),/unavailable/);
 assert.equal(contacts,3);
});

test('calendar list preserves shared event pairs and partial per-calendar coverage', async()=>{
 const authority={version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['calendar' as const]};
 const item={id:'same',summary:'All day',description:'',location:'',status:'confirmed',start:{date:'2026-11-01'},end:{date:'2026-11-02'},attendees:[]};
 const result=await readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8787/mcp',readBearer:'receiver-read-bearer',sourceAuthority:{read:async()=>authority},fetch:async()=>Response.json({version:1,source:'calendar',operation:'calendar_list',items:[{...item,calendar_id:'primary'},{...item,calendar_id:'team#calendar'}],calendars:[{calendar_id:'primary',status:'ok'},{calendar_id:'team#calendar',status:'ok'},{calendar_id:'revoked',status:'unavailable'}],observation:{status:'partial',complete:false,observed_at:'2026-11-01T00:00:00Z',coverage:{since:'2026-11-01T00:00:00Z',until:'2026-11-02T00:00:00Z'}}})},'calendar_list',{start:'2026-11-01T00:00:00Z',end:'2026-11-02T00:00:00Z'},authority.revision) as {items:{id:string;calendar_id:string}[];observation:{complete:boolean}};
 assert.deepEqual(result.items.map(item=>[item.calendar_id,item.id]),[['primary','same'],['team#calendar','same']]);
 assert.equal(result.observation.complete,false);
});


test('calendar evidence reaches the tool with current occurrence and response identity', async () => {
 const authority={version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['calendar' as const]};
 const item={id:'hold',calendar_id:'primary',summary:'Evening hold',description:'',location:'',status:'tentative',transparency:'transparent',eventType:'default',start:{dateTime:'2026-11-03T18:00:00-08:00',timeZone:'America/Los_Angeles'},end:{dateTime:'2026-11-03T19:00:00-08:00',timeZone:'America/Los_Angeles'},originalStartTime:{dateTime:'2026-11-03T17:00:00-08:00'},attendees:[{email:'owner@example.com',responseStatus:'tentative',owner:true}]};
 const result=await readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8787/mcp',readBearer:'fixture',sourceAuthority:{read:async()=>authority},fetch:async()=>Response.json({version:1,source:'calendar',operation:'calendar_list',items:[item],observation:{status:'ok',complete:true,observed_at:'2026-11-03T00:00:00Z',coverage:{since:'2026-11-03T00:00:00Z',until:'2026-11-04T00:00:00Z'}}})},'calendar_list',{start:'2026-11-03T00:00:00Z',end:'2026-11-04T00:00:00Z'},authority.revision) as {items:unknown[]};
 assert.deepEqual(result.items,[item]);
});

test('calendar instructions keep non-owner timed events visible as shared context', () => {
 const tools=createGoogleReadTools({bridgeUrl:'http://127.0.0.1:8787/mcp',readBearer:'fixture',sourceAuthority:{read:async()=>granted()}});
 for(const text of [tools.calendar_list?.description,tools.calendar_get?.description,CALENDAR_EXECUTION_GUIDE]) {
   assert.match(text ?? '', /owner_participates=false/);
   assert.match(text ?? '', /shared.calendar context/);
   assert.match(text ?? '', /timed events/);
   assert.match(text ?? '', /missing.*participation|missing.*attendance|missing.*flag/i);
 }
 assert.doesNotMatch(CALENDAR_EXECUTION_GUIDE,/No calendar discovery or secondary listing is available/);
});


test('unattended Google reads refuse incomplete, revoked or cancelled sources and never expose mutators',async()=>{
 for(const gate of ['incomplete','revoked','cancel','changed'] as const){
  let reads=0,revisionNow=granted().revision!;
  const abort=new AbortController();if(gate==='cancel')abort.abort();
  const tools=createGoogleReadTools({bridgeUrl:'http://127.0.0.1:8787/mcp',readBearer:'reader',
   sourceAuthority:{read:async()=>({...granted(revisionNow),complete:gate!=='incomplete',google_sources:gate==='revoked'?[]:['gmail']})},
   fetch:async()=>{reads++;revisionNow='c'.repeat(64);return Response.json({version:1,source:'gmail',operation:'gmail_search',items:[],observation:{status:'ok',complete:true,observed_at:'2026-10-14T00:00:00Z',coverage:{since:null,until:null}}});}},'unattended');
  assert.deepEqual(Object.keys(tools).sort(),['calendar_get','calendar_list','gmail_get','gmail_search']);
  const tool=tools.gmail_search as {execute(input:unknown,context:unknown):Promise<unknown>};
  const context={requestContext:{get:(key:string)=>({mode:'unattended',runId:'routine',sourceRevision:granted().revision})[key as 'mode']},abortSignal:abort.signal};
  await assert.rejects(tool.execute({query:'in:inbox',max:2},context));
  assert.equal(reads,gate==='changed'?1:0);
 }
});
