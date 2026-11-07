import {writeConfig,fakeTelegram,fakeAgent} from './helpers/bootstrap.js';
import {fileURLToPath} from 'node:url';
import {readSkillIndex} from '../src/tools/skills.js';
import {OWNER_TASK_READBACK_GUIDE} from '../src/tools/owner-task-readback.js';
import test from 'node:test';
import {stableActionIdentity} from '../src/tools/action-gate.js';
import assert from 'node:assert/strict';
import { chmod, lstat, mkdtemp, mkdir, readFile, rm, symlink } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { OperationalJournal } from '../src/journal/index.js';
import { loadRuntimeConfig } from '../src/bootstrap/config.js';
import { activationDiscoverySeedForConfig, activationIdentityForConfig, skillPackRootForConfig, startConfiguredRuntime } from '../src/bootstrap/main.js';
import { photonProjectFingerprint } from '../src/activation.js';
import type { GatewayAdapter } from '../src/channels/gateway.js';
import { Message } from 'chat';
import { spawn } from 'node:child_process';
import { MockLanguageModelV4 } from 'ai/test';
import { createSottoAgentRuntime, type SottoAgentRuntime } from '../src/agent/runtime.js';
import { unavailableAuthority, type SourceAuthority } from '../src/agent/source-authority.js';

function fakePhoton(events: string[]): GatewayAdapter {
  return {
    name: 'imessage', userName: 'test',
    initialize: async () => { events.push('provider-initialize'); },
    disconnect: async () => { events.push('provider-stop'); },
    startGatewayListener: async (options: { waitUntil(task: Promise<unknown>): void }, _durationMs: number, signal: AbortSignal) => {
      events.push('provider-listen');
      options.waitUntil(new Promise<void>(resolve => signal.addEventListener('abort', () => resolve(), { once: true })));
      return { ok: true, status: 200 } as Response;
    },
    isDM: (threadId: string) => threadId === 'imessage:chat-guid;phone=primary',
    channelIdFromThreadId: () => 'imessage:chat-guid;phone=primary',
    decodeThreadId: (threadId: string) => threadId,
    encodeThreadId: (threadId: unknown) => String(threadId),
    postMessage: async () => ({ id: 'unused' }),
    fetchMessages: async () => ({ messages: [], hasMore: false }),
    addReaction: async () => {}, removeReaction: async () => {}, deleteMessage: async () => {},
    editMessage: async () => ({ id: 'unused' }), startTyping: async () => {}, renderFormatted: () => '',
  } as unknown as GatewayAdapter;
}

test('actual unattended Google model reads owner loops without empty-grant MCP discovery; granted discovery failure blocks model contact', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-unattended-bridge-grants-'));
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  let agent: ReturnType<typeof createSottoAgentRuntime> | undefined;
  const events: string[] = [];
  let grants: string[] = [];
  let modelCalls = 0;
  let ownerReads = 0;
  let dueRoutine = false;
  const mcpMethods: string[] = [];
  try {
    const config = await writeConfig(directory, {
      owner: { accountId: 'unattended-owner', ownerId: '123' },
      deployment: 'self_host', modelProxy: undefined,
      modelDirect: { provider: 'google', apiKey: 'fixture-key', chatModelId: 'gemini-fixture', nativeMediaModelId: 'gemini-fixture' },
      bridge: { url: 'http://127.0.0.1:8787/mcp', readBearer: 'fixture-read-bearer' },
    });
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter() { return fakeTelegram(events); },
      createTransport() { return { send: async () => ({ acceptance: 'not_attempted', providerMessageIds: [] }) }; },
      createAgent(_config, model, _media, _skills, tools, authority) {
        agent = createSottoAgentRuntime({ model, storagePath: path.join(directory, 'model-memory.db'),
          instructions: 'Read owner loops, never write.', sourceAuthority: authority,
          unattendedTools: context => tools!.unattended(context.sourceRevision) });
        return agent;
      },
      openStateDatabase() { return new DatabaseSync(':memory:'); },
      fetch: async (input, init) => {
        const request = new Request(input, init);
        const url = new URL(request.url);
        if (url.pathname === '/runtime/source-authority') return new Response(JSON.stringify({ version: 2,
          complete: true, revision: 'a'.repeat(64), bridge_sources: grants, google_sources: [] }));
        if (url.pathname === '/mcp') {
          const body = await request.clone().json();
          mcpMethods.push(body.method);
          // Generic MCP discovery failure; actual receiver Relay handles initialize locally.
          return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id,
            error: { code: -32601, message: 'Synthetic Bridge method unavailable' } }),
            { headers: { 'content-type': 'application/json' } });
        }
        if (url.pathname === '/runtime/owner-loops') {
          ownerReads++;
          return new Response(JSON.stringify({ coverage: 'owner_authored_only', you_owe: [], waiting_on_them: [],
            counts: { you_owe: 0, waiting_on_them: 0 } }));
        }
        assert.equal(url.origin, 'https://generativelanguage.googleapis.com');
        const body = await request.clone().json();
        const names = body.tools.flatMap((group: any) => group.functionDeclarations.map((item: any) => item.name));
        assert.deepEqual(names, ['run_sotto_script', 'owner_open_loops']);
        if (dueRoutine) {
          const sentPrompt=JSON.stringify(body.contents);
          assert.ok(sentPrompt.includes('due execution of an already approved personal routine'));
          assert.ok(sentPrompt.includes('write the reminder itself now'));
          assert.ok(sentPrompt.includes('For a simple reminder, deliver only the reminder: do not look up unrelated context or append source-availability caveats.'));
          assert.ok(sentPrompt.includes('For a task that actually needs current source context, use available read tools and disclose only coverage limits that affect the requested result.'));
          assert.ok(sentPrompt.includes('Do not create or change reminders or routines'));
          assert.ok(sentPrompt.includes('or suggest slash commands or unavailable tool workarounds'));
          assert.ok(sentPrompt.includes('Remind me to check the synthetic results.'));
          assert.ok(sentPrompt.includes('Available scheduled work is read-only'));
          assert.ok(sentPrompt.includes('Do not promise web research'));
        }
        modelCalls++;
        const parts = modelCalls === 1 ? [{ functionCall: { name: 'owner_open_loops', args: { include_parked: false } } }]
          : [{ text: 'Owner-authored-only projection has zero rows.' }];
        return new Response(JSON.stringify({ candidates: [{ content: { role: 'model', parts }, finishReason: 'STOP' }],
          usageMetadata: { promptTokenCount: 10, candidatesTokenCount: 10, totalTokenCount: 20 } }),
          { headers: { 'content-type': 'application/json' } });
      },
    });
    const result = await agent!.generateUnattended({ text: 'Read current owner loops', runId: 'empty-grant-routine' });
    assert.equal(result.text, 'Owner-authored-only projection has zero rows.');
    assert.equal(ownerReads, 1);
    assert.equal(modelCalls, 2);
    assert.deepEqual(mcpMethods, []);
    dueRoutine = true;
    const request={pack:path.resolve('../../sotto-chief-of-staff'),kind:'personal-routine',
      routine_id:'c'.repeat(64),definition_hash:'d'.repeat(64),prompt:'Remind me to check the synthetic results.'};
    const child=spawn(process.env.SOTTO_PYTHON??process.env.PYTHON??'python3',
      [path.resolve('../trigger-receiver/procedure_runner.py'),JSON.stringify(request)],
      {env:{...process.env,SOTTO_DATA:directory,SOTTO_DELIVERY_RUN_ID:'f'.repeat(32),
        SOTTO_CHAT_RUNTIME_URL:`http://127.0.0.1:${runtime.port}`,SOTTO_CHAT_RUNTIME_TOKEN:config.config.bearerToken}});
    let childOutput='',childError='';
    child.stdout.on('data',chunk=>{childOutput+=chunk;});child.stderr.on('data',chunk=>{childError+=chunk;});
    const exit=await new Promise<number|null>((resolve,reject)=>{child.on('error',reject);child.on('close',resolve);});
    assert.equal(exit,0,childError);
    assert.match(childOutput,/Owner-authored-only projection has zero rows/);
    assert.equal(modelCalls,3,'due execution uses one existing model call, no scheduling turn');
    assert.equal(ownerReads,1,'this reminder needs no new source read');
    dueRoutine = false;

    grants = ['contacts'];
    await assert.rejects(agent!.generateUnattended({ text: 'Read current owner loops', runId: 'granted-bridge-routine' }),
      /Bridge request is unavailable/);
    assert.deepEqual(mcpMethods, ['initialize']);
    assert.equal(modelCalls, 3, 'granted discovery failure must block provider contact');
  } finally {
    await runtime?.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test('Telegram bootstrap mounts routines, requested briefs and protected readback while other attended mutations stay gated', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-telegram-routines-'));
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  let resolver: { chat(revision?: string): Promise<Record<string, unknown>>;
    unattended(revision?: string): Promise<Record<string, unknown>> } | undefined;
  const events: string[] = [];
  const receiverPaths: string[] = [];
  const replies: string[] = [];
  let adapter: GatewayAdapter | undefined;
  try {
    const golden = JSON.parse(await readFile(path.resolve('../../adapters/sotto/test_startup_config_v40_golden.json'), 'utf8')) as {config:{instructions:string}};
    const { createHash } = await import('node:crypto');
    const config = await writeConfig(directory, {
      instructions: golden.config.instructions.replace('Managed Photon capabilities:', 'Self-host Telegram capabilities:'),
      owner: { accountId: 'telegram-routine-owner', ownerId: '123' },
      tenantId: 'fixture-tenant', target: 'telegram', deployment: 'self_host',
      modelProxy: undefined, modelDirect: { provider: 'google', apiKey: 'fixture-key', chatModelId: 'gemini-fixture', nativeMediaModelId: 'gemini-fixture' },
      bridge: { url: 'http://127.0.0.1:8787/mcp', readBearer: 'fixture-read-bearer' },
      ownerLoop: { mode: 'explicit_command', bearer: 'e'.repeat(64), identity: {
        tenant_id: 'fixture-tenant', channel: 'telegram', account_id: 'telegram-routine-owner',
        owner_id: '123', line_key: 'primary', target: 'telegram', route_mode: 'fixed',
        route_id: 'telegram:123', bot_fingerprint: createHash('sha256').update('sotto-telegram-bot-v1:test-token').digest('hex'),
      } },
    });
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter() { return adapter = fakeTelegram(events); },
      createTransport() { return { send: async input => { replies.push(input.text ?? ''); return { acceptance: 'accepted', providerMessageIds: [`reply-${replies.length}`] }; } }; },
      createAgent(agentConfig, _model, _media, _skills, tools) {
        assert.match(agentConfig.instructions, /narrow owner question, answer in one or two plain sentences/);
        assert.match(agentConfig.instructions, /Explain evidence metadata in ordinary words, preserving who said or confirmed what and when/);
        assert.match(agentConfig.instructions, /Omit internal field names, status labels and opaque record or message IDs unless the owner asks/);
        assert.match(agentConfig.instructions, /an acknowledgment of a request does not establish a later action the message itself does not report/);
        assert.match(agentConfig.instructions, /Describe missing evidence as a limit of the read.*never as proof that activity stopped or continued elsewhere.*Describe relevant source, conversation and time limits in plain words/);
        assert.match(agentConfig.instructions, /just the wording, give one ready-to-use version, not several options unless asked/);
        assert.match(agentConfig.instructions, /“shorter pls” revises that wording; it does not save or send anything/);
        assert.match(agentConfig.instructions, /An inline draft is wording only, not evidence that the owner sent it or promised it in a meeting/);
        assert.match(agentConfig.instructions, /Health and capabilities show connection and enabled permissions, not proof that every source was read/);
        assert.doesNotMatch(agentConfig.instructions, /You cannot create or change personal routines/);
        assert.match(agentConfig.instructions, /owner_routine_propose/);
        assert.match(agentConfig.instructions, /preferences.*run_sotto_script|run_sotto_script.*preferences/s);
        assert.doesNotMatch(agentConfig.instructions, /cannot create an external draft/);
        assert.match(agentConfig.instructions, /Gmail.*current.*source|current.*source.*Gmail/s);
        resolver = tools; return fakeAgent(events);
      },
      openStateDatabase() { return new DatabaseSync(':memory:'); },
      listenApplication() { return Promise.resolve(32146); },
      fetch: async (input, init) => {
        const url = new URL(String(input));
        receiverPaths.push(url.pathname);
        if (url.pathname === '/runtime/owner-brief-request') {
          const body = JSON.parse(init!.body as string);
          assert.equal(body.identity.channel, 'telegram');
          assert.equal(body.operation, 'admit');
          return Response.json({version: 1, status: 'committed', action_id: body.action_id,
            event_id: body.event_id, payload_hash: body.payload_hash,
            args_hash: stableActionIdentity(body.event_id, 'owner_brief_request', body.args).payloadHash,
            outcome: {operation: 'admit', request_id: 'b'.repeat(32), kind: 'evening',
              refresh: false, status: 'queued'}});
        }
        assert.equal(url.pathname, '/runtime/source-authority');
        return new Response(JSON.stringify({ version: 2, complete: true,
          revision: 'a'.repeat(64), bridge_sources: [], google_sources: [] }));
      },
    });
    assert.ok(resolver);
    assert.equal(JSON.parse(await readFile(config.configPath, 'utf8')).instructions, config.config.instructions,
      'effective guidance must not rewrite the persisted startup configuration');
    const chat = await resolver.chat('a'.repeat(64));
    const unattended = await resolver.unattended();
    for (const name of ['owner_routines','owner_routine_propose','owner_routine_create','owner_routine_cancel','owner_routine_adjust_propose','owner_routine_adjust_confirm']) {
      assert.ok(chat[name], name);
      assert.equal(unattended[name], undefined, name);
    }
    assert.ok(chat.accepted_brief_detail, 'Telegram exposes protected accepted-brief readback');
    assert.equal(unattended.accepted_brief_detail, undefined);
    const adjustment=chat.owner_routine_adjust_propose as {execute(input:unknown,context:unknown):Promise<unknown>};
    await assert.rejects(adjustment.execute({routine_id:'a'.repeat(64),schedule:{hour:17,minute:0,weekday:5}},{requestContext:{get:()=>undefined}}),/Owner routine authority is unavailable/);
    const script=chat.run_sotto_script as {execute(input:unknown,context:unknown):Promise<unknown>};
    for (const args of [[], ['--focus', 'Dana']]) {
      await assert.rejects(script.execute({id:'meeting-prep',args},{requestContext:{get:(key:string)=>key==='mode'?'chat':undefined}}),/Shared skill execution is unavailable/);
    }
    await assert.rejects(script.execute({id:'ask-research',args:['--company','Example']},{requestContext:{get:(key:string)=>key==='mode'?'chat':undefined}}),/Shared skill execution is unavailable/);
    await assert.rejects(script.execute({id:'knowledge-edit',args:['--op','add-identifier','--slug','c_123abc','--identifier','Dana.New@Example.com']},{requestContext:{get:(key:string)=>key==='sourceRevision'?'a'.repeat(64):key==='mode'?'chat':undefined}}),/Owner task authority is unavailable/);
    // The common original Google alias must reach Calendar, not the Contacts parser.
    for (const args of [['calendar-update','--event-id','event123','--fields-json','{"location":"Room 4"}'],['calendar-update-confirm'],['calendar-update','--event-id','event123','--fields-json','{"start":{"dateTime":"2026-10-10T15:00:00-07:00"},"end":{"dateTime":"2026-10-10T16:00:00-07:00"},"location":"Room 4"}'],['calendar-move-update-confirm']]) {
      await assert.rejects(script.execute({id:'google-action',args},{requestContext:{get:(key:string)=>key==='mode'?'chat':undefined}}),/Calendar move requires one current owner message/);
    }
    for (const args of [['gmail-send','--to','alice@example.com','--body','Hello'],['gmail-send-confirm']]) {
      await assert.rejects(script.execute({id:'google-action',args},{requestContext:{get:(key:string)=>key==='mode'?'chat':undefined}}),/Gmail send requires one current owner message/);
    }
    assert.ok(unattended.owner_open_loops);
    assert.ok(adapter);
    for (const [id, text, replyTarget] of [
      ['brief-request', 'Send me my brief', undefined],
      ['offered-reply', 'Done', 'accepted-offer'],
    ] as const) {
      const raw = { id, ...(replyTarget ? { content: { type: 'reply', target: { id: replyTarget } } } : {}) };
      const inbound = new Message({ id, threadId: 'telegram:123', text,
        formatted: { type: 'root', children: [] }, raw,
        author: { userId: '123', userName: 'owner', fullName: 'Owner', isBot: false, isMe: false },
        metadata: { dateSent: new Date(), edited: false }, attachments: [] });
      await runtime.gateway.chat.processMessage(adapter, 'telegram:123', inbound);
      const deadline = Date.now() + 5000;
      while (replies.length < (id === 'brief-request' ? 1 : 2) && Date.now() < deadline)
        await new Promise<void>(resolve => setTimeout(resolve, 10));
    }
    assert.deepEqual(replies, ['Your brief request is queued.', 'unused']);
    assert.equal(events.filter(event => event === 'model-chat').length, 1);
    assert.equal(receiverPaths.filter(path => path === '/runtime/owner-brief-request').length, 1);
    assert.ok(receiverPaths.every(path => ['/runtime/source-authority', '/runtime/owner-brief-request'].includes(path)));
    const journal = new DatabaseSync(path.join(config.stateDirectory, 'operations.sqlite'), { readOnly: true });
    try {
      assert.equal(journal.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_brief_request' AND status='succeeded'").get()!.n, 1);
      assert.equal(journal.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_offer_reply'").get()!.n, 0);
    } finally { journal.close(); }
  } finally {
    await runtime?.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test('private owner-command capability is absent from the agent factory and model tool config', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-owner-command-config-'));
  const events: string[] = [];
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  const commandBearer = 'e'.repeat(64);
  let resolver: {chat(revision?: string): Promise<Record<string,unknown>>} | undefined;
  let current = {version:2,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['gmail','calendar']};
  let available = true;
  let assembledInstructions = '';
  let readbackAgent: SottoAgentRuntime | undefined;
  try {
    const golden = JSON.parse(await readFile(path.resolve('../../adapters/sotto/test_startup_config_v40_golden.json'), 'utf8')) as {config:{instructions:string}};
    const thread = 'imessage:iMessage;-;+15555550123~+15555550999';
    const config = await writeConfig(directory, { channel: 'photon', tenantId: 'fixture-tenant',
      instructions: golden.config.instructions,
      owner: { accountId: 'fixture-account', ownerId: '+15555550123' }, lineKey: 'fixture-line',
      target: 'photon', providerThreadId: thread,
      credentials: { mode: 'cloud', photonProjectId: 'fixture-project', photonProjectSecret: 'fixture-secret' },
      bridge: { url: 'http://127.0.0.1:8787/mcp', readBearer: 'fixture-read-bearer' },
      ownerLoop: { mode: 'explicit_command', identity: { tenant_id: 'fixture-tenant', channel: 'photon',
        account_id: 'fixture-account', owner_id: '+15555550123', line_key: 'fixture-line', target: 'photon',
        route_mode: 'fixed', route_id: thread, project_fingerprint: photonProjectFingerprint('fixture-project') },
        bearer: commandBearer },
    });
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter() { return fakePhoton(events); },
      createTransport() { return { send: async () => ({ acceptance: 'not_attempted', providerMessageIds: [] }) }; },
      createAgent(agentConfig, _model, _media, _skills, tools) {
        assert.equal(Object.hasOwn(agentConfig, 'ownerLoop'), false);
        assert.doesNotMatch(agentConfig.instructions, /You cannot create or change personal routines/);
        assert.match(agentConfig.instructions, /Gmail draft and send review tools require a current Gmail source grant/);
        assert.match(agentConfig.instructions, /Calendar write review tools require a current Calendar source grant/);
        assert.match(agentConfig.instructions, /knowledge-edit/);
        assert.match(agentConfig.instructions, /calendar_move_review/);
        assert.match(agentConfig.instructions, /calendar_move_confirm/);
        assert.match(agentConfig.instructions, /calendar_create_review/);
        assert.match(agentConfig.instructions, /calendar_create_confirm/);
        assert.match(agentConfig.instructions, /calendar_move_update_review/);
        assert.match(agentConfig.instructions, /calendar_move_update_confirm/);
        assert.match(agentConfig.instructions, /--loop-identity/);
        assert.match(agentConfig.instructions, /owner_task_add, owner_task_close or owner_task_resolve_many/);
        assert.match(agentConfig.instructions, /cannot complete only the first of several/);
        assert.match(agentConfig.instructions, /Prior assistant drafts, style samples, open tasks and help offers do not prove the owner acted/);
        assert.match(agentConfig.instructions, /Past-tense owner claims in a draft need an actual source record or explicit owner statement/);
        assert.match(agentConfig.instructions, /With partial source coverage, describe what was checked/);
        assert.match(agentConfig.instructions, /Preserve source qualifiers and strength: typically is not exclusively, and possible is not certain, including in shorter versions and declines/);
        assert.equal(JSON.stringify(agentConfig).includes(commandBearer), false);
        assert.ok(tools);
        assembledInstructions = agentConfig.instructions;
        resolver = tools;
        return fakeAgent(events);
      },
      openStateDatabase() { return new DatabaseSync(':memory:'); },
      listenApplication() { return Promise.resolve(32135); },
      fetch: async input => {
        assert.equal(new URL(String(input)).pathname,'/runtime/source-authority');
        return available ? new Response(JSON.stringify(current)) : new Response('{}',{status:503});
      },
    });
    assert.equal((await loadRuntimeConfig(config.configPath)).ownerLoop?.bearer, commandBearer);
    assert.ok(resolver);
    const granted = await resolver.chat(current.revision);
    assert.ok(granted.gmail_draft_pair && granted.gmail_draft_propose && granted.gmail_draft_save && granted.gmail_get);
    assert.ok(granted.calendar_move_update_review && granted.calendar_move_update_confirm && granted.calendar_create_review && granted.calendar_create_confirm && granted.calendar_move_review && granted.calendar_move_confirm && granted.calendar_rsvp_review && granted.calendar_rsvp_confirm && granted.calendar_get && granted.calendar_list);
    // Exercise the provider prompt after bootstrap assembly, with accepted prose history
    // and no activated skills. The capability contract must survive every new turn.
    const model = new MockLanguageModelV4({ doGenerate: async () => {
      return {content:[{type:'text' as const,text:'Fixture response.'}],
        finishReason:{unified:'stop' as const,raw:'STOP'},
        usage:{inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}},warnings:[]};
    }});
    readbackAgent = createSottoAgentRuntime({model, storagePath:path.join(directory,'readback.db'),
      instructions:assembledInstructions, skillPaths:(await readSkillIndex(path.resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),
      chatTools:() => granted as never});
    await readbackAgent.projectConversation({threadId:'readback-thread',ownerId:'readback-owner',messages:[
      {id:'prior-prose',role:'assistant',text:'Only pine remains.',createdAt:Date.now(),sourceRevision:'owner-only-context-v1'},
    ]});
    for (const [index,text] of ['What remains of maple, pine, and willow?', 'What tasks are still open?'].entries()) {
      await readbackAgent.generateChat({threadId:'readback-thread',ownerId:'readback-owner',runId:`readback-${index}`,text});
    }
    assert.equal(model.doGenerateCalls.length,2);
    for (const {prompt, tools} of model.doGenerateCalls) {
      const system = prompt.filter(message => message.role === 'system').map(message => message.content).join('\n');
      assert.ok(system.includes(OWNER_TASK_READBACK_GUIDE));
      assert.ok(system.includes('Use when the user asks what is outstanding'), 'native discovery catalog remains available');
      assert.ok(tools?.some(tool => tool.type === 'function' && tool.name === 'skill'));
      assert.match(system, /A settled mutation ends the turn/);
      assert.doesNotMatch(system, /Answer from those two partial views/);
      assert.ok(JSON.stringify(prompt).includes('Only pine remains.'));
      const list = tools?.find(tool => tool.type === 'function' && tool.name === 'owner_task_list');
      assert.ok(list && 'description' in list && list.description?.includes(OWNER_TASK_READBACK_GUIDE));
    }
    current = {...current,revision:'b'.repeat(64),google_sources:[]};
    for (const revision of [current.revision,'a'.repeat(64)]) {
      const revoked = await resolver.chat(revision);
      assert.ok(!Object.keys(revoked).some(name=>name.startsWith('gmail_') || name.startsWith('calendar_')));
      assert.ok(revoked.owner_open_loops && revoked.owner_task_add, 'private owner capabilities remain independent');
    }
    available=false;
    const unavailable=await resolver.chat(current.revision);
    assert.equal(unavailable.gmail_draft_propose,undefined);
    assert.equal(unavailable.gmail_draft_save,undefined);
  } finally { try { await readbackAgent?.close(); } catch { /* preserve assertion */ }
    try { await runtime?.close(); } catch { /* preserve assertion */ }
    await rm(directory, { recursive: true, force: true }); }
});

test('runtime config requires a private file, strict shape, and explicit channel owner', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-config-'));
  try {
    const good = await writeConfig(directory);
    const loaded = await loadRuntimeConfig(good.configPath);
    assert.equal(loaded.turnPresence, false);
    const optIn = await writeConfig(directory, { turnPresence: true });
    assert.equal((await loadRuntimeConfig(optIn.configPath)).turnPresence, true);
    assert.equal(loaded.owner.ownerId, '123');
    assert.equal(loaded.tenantId, undefined);
    assert.equal(loaded.stateDirectory, good.stateDirectory);
    await assert.rejects(lstat(good.stateDirectory));

    const loose = await writeConfig(directory, { unexpected: 'field' });
    await assert.rejects(loadRuntimeConfig(loose.configPath), /shape or field bounds/);
    const mismatch = await writeConfig(directory, { owner: { accountId: 'telegram-owner', ownerId: '999' } });
    await assert.rejects(loadRuntimeConfig(mismatch.configPath), /does not match pairing/);
    const invalidBridge = await writeConfig(directory, { bridge: { url: 'https://remote.example/mcp', readBearer: 'bridge-read-token' } });
    await assert.rejects(loadRuntimeConfig(invalidBridge.configPath), /exact loopback/);
    const invalidModel = await writeConfig(directory, { modelProxy: { origin: 'https://proxy.example', bearer: 'test', chatModelId: 'bad/id', nativeMediaModelId: 'media' } });
    await assert.rejects(loadRuntimeConfig(invalidModel.configPath), /shape or field bounds/);
    const emptyTenant = await writeConfig(directory, { tenantId: '' });
    await assert.rejects(loadRuntimeConfig(emptyTenant.configPath), /shape or field bounds/);
    const controlledTenant = await writeConfig(directory, { tenantId: 'tenant\nforged' });
    await assert.rejects(loadRuntimeConfig(controlledTenant.configPath), /shape or field bounds/);
    const explicitTenant = await writeConfig(directory, { tenantId: 'managed-tenant-1' });
    assert.equal((await loadRuntimeConfig(explicitTenant.configPath)).tenantId, 'managed-tenant-1');

    const publicFile = await writeConfig(directory);
    await chmod(publicFile.configPath, 0o644);
    await assert.rejects(loadRuntimeConfig(publicFile.configPath), /group or other/);
    const link = path.join(directory, 'linked.json');
    await symlink(good.configPath, link);
    await assert.rejects(loadRuntimeConfig(link), /unavailable or invalid/);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('activation identity is derived only for explicit Photon Cloud credentials', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-activation-config-'));
  try {
    const cloud = await writeConfig(directory, {
      channel: 'photon',
      tenantId: 'tenant-managed',
      owner: { accountId: 'photon-account', ownerId: '+15555550123' },
      lineKey: 'photon-primary',
      providerThreadId: 'imessage:chat-guid;phone=primary',
      credentials: { mode: 'cloud', photonProjectId: 'project-exact-bytes', photonProjectSecret: 'secret-not-in-evidence' },
    });
    const cloudConfig = await loadRuntimeConfig(cloud.configPath);
    assert.deepEqual(activationIdentityForConfig(cloudConfig), {
      tenant_id: 'tenant-managed',
      project_fingerprint: photonProjectFingerprint('project-exact-bytes'),
      owner_id: '+15555550123',
      account_id: 'photon-account',
      line_key: 'photon-primary',
      provider_thread_id: 'imessage:chat-guid;phone=primary',
    });
    assert.equal(JSON.stringify(activationIdentityForConfig(cloudConfig)).includes('secret-not-in-evidence'), false);

    const unscopedCloud = await writeConfig(directory, {
      channel: 'photon',
      owner: { accountId: 'photon-account', ownerId: '+15555550123' },
      credentials: { mode: 'cloud', photonProjectId: 'project-exact-bytes', photonProjectSecret: 'secret' },
    });
    assert.equal(activationIdentityForConfig(await loadRuntimeConfig(unscopedCloud.configPath))?.tenant_id, '');

    const selfHostedPhoton = await writeConfig(directory, {
      channel: 'photon',
      credentials: { mode: 'self_host', photonServerUrl: 'https://photon.example:443', photonApiKey: 'self-host-key' },
    });
    assert.equal(activationIdentityForConfig(await loadRuntimeConfig(selfHostedPhoton.configPath)), undefined);

    const telegram = await writeConfig(directory);
    assert.equal(activationIdentityForConfig(await loadRuntimeConfig(telegram.configPath)), undefined);
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('first-contact configuration is explicit Cloud-only and binds an expected physical line', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-discovery-config-'));
  const cloud = { channel: 'photon', owner: { accountId: 'account', ownerId: '+15550001111' },
    credentials: { mode: 'cloud', photonProjectId: 'project', photonProjectSecret: 'fixture-secret' } };
  try {
    const valid = await writeConfig(directory, { ...cloud, providerThreadId: undefined,
      firstContact: { mode: 'owner_dm', expectedProviderLine: '+15550002222' } });
    const config = await loadRuntimeConfig(valid.configPath);
    assert.equal(config.providerThreadId, undefined);
    assert.equal(activationIdentityForConfig(config), undefined);
    assert.deepEqual(activationDiscoverySeedForConfig(config), {
      tenant_id: '', project_fingerprint: photonProjectFingerprint('project'), owner_id: '+15550001111',
      account_id: 'account', line_key: 'primary', expected_provider_line: '+15550002222',
    });
    const shared = await writeConfig(directory, {...cloud, providerThreadId: undefined,
      firstContact: {mode: 'owner_dm_shared_project'}});
    const sharedConfig = await loadRuntimeConfig(shared.configPath);
    assert.deepEqual(activationDiscoverySeedForConfig(sharedConfig), {
      tenant_id: '', project_fingerprint: photonProjectFingerprint('project'), owner_id: '+15550001111',
      account_id: 'account', line_key: 'primary', provider_route_mode: 'shared_project',
    });
    for (const overrides of [
      { ...cloud, providerThreadId: undefined },
      {...cloud, providerThreadId: undefined, firstContact: {mode: 'owner_dm_shared_project', expectedProviderLine: '+15550002222'}},
      { ...cloud, firstContact: { mode: 'owner_dm', expectedProviderLine: '+15550002222' } },
      { ...cloud, providerThreadId: undefined, firstContact: { mode: 'owner_dm', expectedProviderLine: 'primary' } },
      { ...cloud, providerThreadId: undefined, firstContact: { mode: 'owner_dm', expectedProviderLine: '+15550002222' },
        credentials: { mode: 'self_host', photonServerUrl: 'https://photon.example', photonApiKey: 'fixture' } },
      { channel: 'telegram', providerThreadId: undefined,
        firstContact: { mode: 'owner_dm', expectedProviderLine: '+15550002222' } },
    ]) {
      const candidate = await writeConfig(directory, overrides);
      await assert.rejects(loadRuntimeConfig(candidate.configPath), /shape or field bounds/);
    }
  } finally { await rm(directory, { recursive: true, force: true }); }
});

test('configured runtime keeps one Photon lookup slot across a cancelled turn', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-photon-slot-bootstrap-'));
  const thread = 'imessage:chat-guid;phone=primary';
  const owner = '+15555550123';
  const config = await writeConfig(directory, { channel: 'photon',
    owner: { accountId: 'photon-account', ownerId: owner }, lineKey: 'primary', providerThreadId: thread,
    credentials: { mode: 'cloud', photonProjectId: 'project', photonProjectSecret: 'fixture' } });
  let calls = 0;
  let modelCalls = 0;
  let providerContacts = 0;
  let lookupStarted!: () => void;
  const lookup = new Promise<void>(resolve => { lookupStarted = resolve; });
  let releaseLookup!: (value: null) => void;
  const held = new Promise<null>(resolve => { releaseLookup = resolve; });
  const adapter = fakePhoton([]) as GatewayAdapter & { fetchMessage: (threadId: string, id: string) => Promise<null> };
  adapter.fetchMessage = async (_threadId, _id) => { calls++; lookupStarted(); return held; };
  const message = (id: string) => new Message({ id, threadId: thread, text: '',
    formatted: { type: 'root', children: [] },
    raw: { id, content: { type: 'attachment', id: `attachment-${id}`,
      mimeType: 'image/png', name: 'photo.png', size: 4 } },
    author: { userId: owner, userName: 'owner', fullName: 'Owner', isBot: false, isMe: false },
    metadata: { dateSent: new Date(), edited: false },
    attachments: [{ type: 'image', mimeType: 'image/png', name: 'photo.png', size: 4 }],
  });
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  try {
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter: () => adapter,
      createTransport: () => ({ send: async () => { providerContacts++; return { acceptance: 'not_attempted', providerMessageIds: [] }; } }),
      createAgent: () => ({ generateChat: async () => { modelCalls++; return { text: '' }; }, generateUnattended: async () => ({ text: '' }),
        rotateThread: async () => {}, purgeThreadsBefore: async () => 0, close: async () => {} }),
    });
    await runtime.gateway.chat.processMessage(adapter, thread, message('first'));
    await lookup;
    // Read feedback and media hydration each own one lookup; both remain held here.
    assert.equal(calls, 2);
    await runtime.gateway.chat.processMessage(adapter, thread, message('second'));
    for (let i = 0; i < 20 && modelCalls === 0; i++) await new Promise<void>(resolve => setImmediate(resolve));
    assert.equal(calls, 2, 'unresolved feedback and media lookups retain their slots across cancellation');
    assert.equal(modelCalls, 1, 'replacement turn proceeds with an unavailable marker');
    releaseLookup(null);
    await new Promise<void>(resolve => setImmediate(resolve));
    assert.equal(modelCalls, 1, 'late lookup completion cannot revive the cancelled turn');
    assert.equal(providerContacts, 0);
  } finally {
    releaseLookup(null);
    await runtime?.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test('bootstrap recovers before provider start, shares one adapter, and binds HTTP last', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-bootstrap-'));
  const events: string[] = [];
  const stateDbs: DatabaseSync[] = [];
  try {
    const config = await writeConfig(directory);
    await mkdir(config.stateDirectory, { mode: 0o700 });
    const journal = new OperationalJournal(path.join(config.stateDirectory, 'operations.sqlite'));
    const inbound = journal.admitInbound({
      eventId: 'recovery-event', accountKey: 'telegram-owner', lineKey: 'primary', threadKey: 'telegram:123',
      channel: 'telegram', sender: '123', payload: JSON.stringify({
        inbound: { text: 'already admitted' },
        context: { threadId: 'telegram:123', destination: 'owner-target' },
      }),
    });
    const eventId = inbound.event.id as string;
    journal.startRun({ runId: 'recovery-run', eventId });
    journal.prepareReply({ replyId: 'recovery-reply', runId: 'recovery-run', eventId,
      dispatchId: 'recovery-dispatch', kind: 'interactive', destination: 'owner-target', payload: 'original prepared reply' });
    journal.close();
    let sharedAdapter: GatewayAdapter | undefined;
    const runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter() { sharedAdapter = fakeTelegram(events); return sharedAdapter; },
      createTransport(_channel, adapter) {
        assert.equal(adapter, sharedAdapter);
        events.push('transport-ready');
        return { send: async () => {
          events.push('recovery-send');
          return { acceptance: 'accepted', providerMessageIds: ['provider-id'] };
        } };
      },
      createAgent() { events.push('agent-ready'); return fakeAgent(events); },
      openStateDatabase() { const db = new DatabaseSync(':memory:'); stateDbs.push(db); return db; },
      listenApplication() { events.push('http-listen'); return Promise.resolve(32123); },
      fetch: async () => { throw new Error('model network is not expected in startup'); },
    });
    assert.equal(runtime.port, 32123);
    assert.equal(runtime.gateway.adapter, sharedAdapter);
    assert.ok(events.indexOf('agent-ready') < events.indexOf('provider-initialize'));
    assert.ok(events.indexOf('recovery-send') < events.indexOf('provider-initialize'));
    assert.ok(events.indexOf('provider-initialize') < events.indexOf('http-listen'));
    assert.equal(await runtime.rotateSessions().then(result => result.count), 0);
    await runtime.close();
    assert.ok(events.indexOf('provider-stop') < events.indexOf('agent-close'));
    assert.equal(stateDbs.length, 1);
    await assert.rejects(lstat(path.join(config.stateDirectory, '.runtime-owner')));
  } finally {
    for (const db of stateDbs) { try { db.close(); } catch { /* runtime already closed it */ } }
    await rm(directory, { recursive: true, force: true });
  }
});

test('attended health remains readable with zero local grants while content tools and unattended discovery stay withheld', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-zero-grants-health-'));
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  let resolver: {chat(revision?: string): Promise<Record<string, unknown>>;
    unattended(revision?: string): Promise<Record<string, unknown>>} | undefined;
  const revision = 'a'.repeat(64);
  let current = {version: 2, complete: true, revision, bridge_sources: [], google_sources: []};
  const methods: string[] = [];
  let healthReads = 0;
  try {
    const config = await writeConfig(directory, {channel: 'photon', tenantId: 'health-fixture',
      owner: {accountId: 'health-account', ownerId: '+15555550123'}, target: 'photon',
      providerThreadId: 'imessage:iMessage;-;+15555550123~+15555550999',
      credentials: {mode: 'cloud', photonProjectId: 'fixture-project', photonProjectSecret: 'fixture-secret'},
      bridge: {url: 'http://127.0.0.1:8787/mcp', readBearer: 'fixture-read-bearer'}});
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter() { return fakePhoton([]); },
      createTransport() { return {send: async () => ({acceptance: 'not_attempted', providerMessageIds: []})}; },
      createAgent(_config, _model, _media, _skills, tools) {resolver = tools; return fakeAgent([]);},
      openStateDatabase() {return new DatabaseSync(':memory:');},
      listenApplication() {return Promise.resolve(32147);},
      fetch: async (input, init) => {
        const request = new Request(input, init);
        if (new URL(request.url).pathname === '/runtime/source-authority') return Response.json(current);
        assert.equal(new URL(request.url).pathname, '/mcp');
        const body = await request.json(); methods.push(body.method);
        const reply = (result: unknown) => Response.json({jsonrpc: '2.0', id: body.id, result});
        if (body.method === 'initialize') return reply({protocolVersion: '2025-06-18'});
        if (body.method === 'tools/list') return reply({tools: ['health', 'read_local', 'get_contacts', 'get_messages', 'read_history', 'search_local', 'send_message'].map(name => ({
          name, inputSchema: {type: 'object', properties: {}, additionalProperties: false}}))});
        assert.equal(body.method, 'tools/call');
        assert.equal(body.params.name, 'health', 'no source content or send reaches the Bridge');
        healthReads++;
        return reply({content: [{type: 'text', text: JSON.stringify({status: 'connected', sources: {}})}]});
      },
    });
    assert.ok(resolver);
    const chat = await resolver.chat(revision);
    assert.deepEqual(Object.keys(chat).filter(name => name.startsWith('bridge_')), ['bridge_health']);
    const health = chat.bridge_health as {execute(input: unknown, context: unknown): Promise<unknown>};
    const context = {requestContext: {get: (key: string) => key === 'sourceRevision' ? revision : undefined}};
    assert.deepEqual(await health.execute({}, context), {content: [{type: 'text', text: '{"status":"connected","sources":{}}'}]});
    assert.equal(healthReads, 1);
    const count = methods.length;
    assert.equal((await resolver.unattended(revision)).bridge_health, undefined);
    assert.equal(methods.length, count, 'unattended empty-grant execution still avoids discovery');
    current = {...current, complete: false};
    assert.equal((await resolver.chat(revision)).bridge_health, undefined);
    await assert.rejects(health.execute({}, context), /source|permission/i);
    current = {...current, complete: true, revision: 'b'.repeat(64)};
    assert.equal((await resolver.chat(revision)).bridge_health, undefined);
    await assert.rejects(health.execute({}, context), /source|permission/i);
    assert.equal(methods.length, count, 'incomplete or changed authority cannot discover or invoke');
    assert.equal(healthReads, 1);
  } finally {
    await runtime?.close();
    await rm(directory, {recursive: true, force: true});
  }
});

test('bootstrap exposes owner-only loops to chat and read-only unattended runs; retune and mutations remain chat-only', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-owner-loops-bootstrap-'));
  const events: string[] = [];
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  let tools: { chat(revision?: string, listingAuthority?: SourceAuthority): Promise<Record<string, unknown>>;
    unattended(revision?: string): Promise<Record<string, unknown>> } | undefined;
  let retuneBody: unknown = { coverage: 'owner_authored_only', stale_loops: [{
    anchor_key: 'manual:owner', name: '(unknown)', what: 'Send the signed deck',
    direction: 'you_owe', age_days: 9, times_surfaced: 0,
    surface_count_provenance: 'unknown_legacy', overdue: false, deadline: null,
    chased_count: 0, chased_out: false, suggestion: 'do it or dismiss',
  }], mute_suggestions: [], current: {}, counts: { stale: 1, mute_suggestions: 0 } };
  let retuneReads = 0;
  let sourceReads = 0;
  try {
    const golden = JSON.parse(await readFile(path.resolve('../../adapters/sotto/test_startup_config_v40_golden.json'), 'utf8')) as {config:{instructions:string}};
    const config = await writeConfig(directory, { instructions: golden.config.instructions, bridge: {
      url: 'http://127.0.0.1:8787/mcp', readBearer: 'fixture-read-bearer',
    } });
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter() { return fakeTelegram(events); },
      createTransport() { return { send: async () => ({ acceptance: 'not_attempted', providerMessageIds: [] }) }; },
      createAgent(_config, _model, _media, _skills, bridgeTools) {
        assert.ok(_config.instructions.includes('triage-queue'));
        assert.ok(_config.instructions.includes('Saving a draft never sends'));
        assert.doesNotMatch(_config.instructions, /Run an enabled preferences command through run_sotto_script/);
        assert.match(_config.instructions, /Personal routine changes are unavailable/);
        assert.match(_config.instructions, /Preference changes are unavailable/);
        assert.doesNotMatch(_config.instructions, /Offered owner_routine_propose/);
        tools = bridgeTools as typeof tools;
        return fakeAgent(events);
      },
      openStateDatabase() { return new DatabaseSync(':memory:'); },
      listenApplication() { return Promise.resolve(32126); },
      fetch: async input => {
        const path = new URL(String(input)).pathname;
        if (path === '/runtime/source-authority') { sourceReads++; return new Response('{}', { status: 503 }); }
        if (path === '/runtime/owner-loops') return new Response(JSON.stringify({
          coverage: 'owner_authored_only', you_owe: [], waiting_on_them: [],
          counts: { you_owe: 0, waiting_on_them: 0 },
        }));
        if (path === '/runtime/owner-retune') { retuneReads++; return new Response(JSON.stringify(retuneBody)); }
        throw new Error('MCP discovery must not be required for owner-only projection');
      },
    });
    assert.ok(tools);
    const chat = await tools.chat();
    const unattended = await tools.unattended();
    const beforeSnapshot = sourceReads;
    const fromSnapshot = await tools.chat(undefined, unavailableAuthority());
    assert.equal(sourceReads, beforeSnapshot, 'turn listing reuses validated begin snapshot');
    assert.equal(fromSnapshot.owner_open_loops === undefined, false, 'owner-only tools survive unavailable source authority');
    assert.ok(chat.owner_open_loops);
    assert.ok(chat.run_sotto_script, 'pure knowledge command does not require a private mutation capability');
    const readOnlyScript = chat.run_sotto_script as {execute(input:unknown, context:unknown):Promise<unknown>};
    await assert.rejects(readOnlyScript.execute({id:'preferences',args:['show']},
      {requestContext:{get:(key:string)=>key==='mode'?'chat':undefined}}), /Preference execution is unavailable/);
    assert.equal(unattended.run_sotto_script, undefined);
    assert.ok(chat.owner_stale_loops);
    assert.ok(chat.accepted_brief_detail, 'Telegram readback independently requires current receiver source and channel proof');
    assert.equal(unattended.accepted_brief_detail, undefined);
    assert.ok(unattended.owner_open_loops);
    for (const name of ['gmail_draft_propose','gmail_draft_save','owner_routine_propose','owner_routine_create','owner_routine_cancel','owner_task_add','owner_task_close','owner_reminder_create','owner_preference_change'])
      assert.equal(unattended[name],undefined);
    const unattendedLoops = unattended.owner_open_loops as {execute(input:unknown,context:unknown):Promise<unknown>};
    await assert.rejects(unattendedLoops.execute({include_parked:false},{requestContext:{get:()=>undefined}}));
    const unattendedProjection = await unattendedLoops.execute({include_parked:false},{requestContext:{get:(key:string)=>({mode:'unattended',runId:'routine-occurrence'})[key as 'mode']}}) as {coverage:string};
    assert.equal(unattendedProjection.coverage,'owner_authored_only');
    assert.equal(unattended.owner_stale_loops, undefined);
    const ownerTool = chat.owner_open_loops as { execute(input: unknown, context: unknown): Promise<unknown> };
    const result = await ownerTool.execute({ include_parked: false }, {
      requestContext: { get: (key: string) => ({ mode: 'chat', runId: 'r', ownerRequestKey: 'e' })[key as 'mode'] },
    }) as { coverage: string; coverage_note: string };
    assert.equal(result.coverage, 'owner_authored_only');
    assert.match(result.coverage_note, /Empty lists do not prove/);
    const retuneTool = chat.owner_stale_loops as { execute(input: unknown, context: unknown): Promise<unknown> };
    const authority = { requestContext: { get: (key: string) =>
      ({ mode: 'chat', runId: 'r', ownerRequestKey: 'e' })[key as 'mode'] } };
    const retune = await retuneTool.execute({}, authority) as { coverage: string; coverage_note: string;
      stale_loops: Array<{ what: string }>; current: Record<string, unknown> };
    assert.equal(retune.coverage, 'owner_authored_only');
    assert.equal(retune.stale_loops[0]?.what, 'Send the signed deck');
    assert.deepEqual(retune.current, {});
    assert.match(retune.coverage_note, /Empty results do not prove/);
    assert.equal(retuneReads, 1);
    await assert.rejects(retuneTool.execute({}, { requestContext: { get: () => 'unattended' } }), /unavailable/);
    assert.equal(retuneReads, 1, 'unattended authority did not contact the protected read route');
    retuneBody = { ...retuneBody as object, current: { private_preference: 'must be withheld' } };
    await assert.rejects(retuneTool.execute({}, authority), /unavailable/);
    const cancelled = new AbortController(); cancelled.abort();
    await assert.rejects(retuneTool.execute({}, { ...authority, abortSignal: cancelled.signal }), /unavailable/);
    assert.equal(retuneReads, 2, 'cancelled chat turn did not contact the protected read route');
  } finally {
    try { await runtime?.close(); } catch { /* preserve fixture assertions */ }
    await rm(directory, { recursive: true, force: true });
  }
});

test('fake model selects mounted owner-only retune while source discovery is unavailable', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-owner-retune-model-'));
  const events: string[] = [];
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  let agent: SottoAgentRuntime | undefined;
  let generations = 0;
  const model = new MockLanguageModelV4({ doGenerate: async () => {
    const first = ++generations === 1;
    return {
      content: first ? [{ type: 'tool-call' as const, toolCallId: 'retune-call-1',
        toolName: 'owner_stale_loops', input: '{}' }] : [{ type: 'text' as const, text: 'One owner-authored stale loop.' }],
      finishReason: { unified: first ? 'tool-calls' as const : 'stop' as const, raw: first ? 'tool_calls' : 'STOP' },
      usage: { inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 },
        outputTokens: { total: 2, text: 2, reasoning: 0 } }, warnings: [],
    };
  } });
  try {
    const config = await writeConfig(directory, { bridge: {
      url: 'http://127.0.0.1:8787/mcp', readBearer: 'fixture-read-bearer',
    } });
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter() { return fakeTelegram(events); },
      createTransport() { return { send: async () => ({ acceptance: 'not_attempted', providerMessageIds: [] }) }; },
      createAgent(loaded, _model, _media, _skills, bridgeTools, sourceAuthority) {
        agent = createSottoAgentRuntime({ model, storagePath: path.join(loaded.stateDirectory, 'agent-memory.db'),
          instructions: 'Answer briefly.', sourceAuthority,
          chatTools: authority => bridgeTools?.chat(authority.sourceRevision) ?? {},
          unattendedTools: authority => bridgeTools?.unattended(authority.sourceRevision) ?? {} });
        return agent;
      },
      openStateDatabase() { return new DatabaseSync(':memory:'); },
      listenApplication() { return Promise.resolve(32127); },
      fetch: async input => {
        const route = new URL(String(input)).pathname;
        if (route === '/runtime/source-authority') return new Response('{}', { status: 503 });
        if (route === '/runtime/owner-retune') return new Response(JSON.stringify({
          coverage: 'owner_authored_only', stale_loops: [{ anchor_key: 'manual:owner', name: '(unknown)',
            what: 'Send the signed deck', direction: 'you_owe', age_days: 9, times_surfaced: 0,
            surface_count_provenance: 'unknown_legacy', overdue: false, deadline: null,
            chased_count: 0, chased_out: false, suggestion: 'do it or dismiss' }],
          mute_suggestions: [], current: {}, counts: { stale: 1, mute_suggestions: 0 },
        }));
        throw new Error('source MCP discovery must not be required');
      },
    });
    assert.ok(agent);
    const output = await agent.generateChat({ text: 'Which owner-only loops need review?',
      threadId: 'telegram:123', ownerId: '123', runId: 'run-retune', ownerRequestKey: 'owner-event-retune' });
    assert.equal(output.text, 'One owner-authored stale loop.');
    assert.equal(model.doGenerateCalls.length, 2);
    const toolResultPrompt = JSON.stringify(model.doGenerateCalls[1]?.prompt);
    assert.match(toolResultPrompt, /Send the signed deck/);
    assert.match(toolResultPrompt, /owner_authored_only/);
    assert.equal(toolResultPrompt.includes('source MCP'), false);
  } finally {
    try { await runtime?.close(); } catch { /* preserve fixture assertions */ }
    await rm(directory, { recursive: true, force: true });
  }
});

test('without a configured receiver no owner-continuity or Bridge tool resolver is mounted', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-no-owner-continuity-'));
  const events: string[] = [];
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  let capturedTools: unknown = 'not inspected';
  try {
    const config = await writeConfig(directory);
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter() { return fakeTelegram(events); },
      createTransport() { return { send: async () => ({ acceptance: 'not_attempted', providerMessageIds: [] }) }; },
      createAgent(_config, _model, _media, _skills, bridgeTools) {
        capturedTools = bridgeTools;
        return fakeAgent(events);
      },
      openStateDatabase() { return new DatabaseSync(':memory:'); },
      listenApplication() { return Promise.resolve(32128); },
      fetch: async () => { throw new Error('unconfigured receiver route must not be called'); },
    });
    assert.equal(capturedTools, undefined);
  } finally {
    try { await runtime?.close(); } catch { /* preserve fixture assertions */ }
    await rm(directory, { recursive: true, force: true });
  }
});

test('bootstrap binds the exact Photon Cloud activation identity before provider intake', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-bootstrap-activation-'));
  const events: string[] = [];
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  let inspectDb: DatabaseSync | undefined;
  try {
    const config = await writeConfig(directory, {
      channel: 'photon', tenantId: 'managed-tenant',
      owner: { accountId: 'photon-account', ownerId: '+15555550123' },
      lineKey: 'photon-primary', providerThreadId: 'imessage:chat-guid;phone=primary',
      credentials: { mode: 'cloud', photonProjectId: 'project-binding', photonProjectSecret: 'secret' },
    });
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter() { return fakePhoton(events); },
      createTransport() { return { send: async () => ({ acceptance: 'not_attempted', providerMessageIds: [] }) }; },
      createAgent() { return fakeAgent(events); },
      openStateDatabase() { const db = new DatabaseSync(':memory:'); return db; },
      listenApplication() { events.push('http-listen'); return Promise.resolve(32125); },
      fetch: async () => { throw new Error('model network is not expected during startup'); },
    });
    inspectDb = new DatabaseSync(path.join(config.stateDirectory, 'operations.sqlite'));
    const row = inspectDb.prepare('SELECT identity_json FROM activation_identity WHERE id=1').get() as { identity_json: string } | undefined;
    assert.ok(row);
    assert.deepEqual(JSON.parse(row.identity_json), {
      tenant_id: 'managed-tenant',
      project_fingerprint: photonProjectFingerprint('project-binding'),
      owner_id: '+15555550123',
      account_id: 'photon-account',
      line_key: 'photon-primary',
      provider_thread_id: 'imessage:chat-guid;phone=primary',
    });
    assert.ok(events.indexOf('provider-initialize') < events.indexOf('provider-listen'));
    assert.ok(events.indexOf('provider-listen') < events.indexOf('http-listen'));
  } finally {
    try { inspectDb?.close(); } catch { /* best effort after test failure */ }
    try { await runtime?.close(); } catch { /* preserve assertion failure */ }
    await rm(directory, { recursive: true, force: true });
  }
});

test('configured bootstrap serves only the static identity of its loaded private config', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-channel-identity-bootstrap-'));
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  const events: string[] = [];
  try {
    const file = await writeConfig(directory, { lineKey: 'identity-fixture' });
    runtime = await startConfiguredRuntime(file.configPath, {
      createAdapter: () => fakeTelegram(events),
      createTransport: () => ({ send: async () => ({ acceptance: 'not_attempted', providerMessageIds: [] }) }),
      createAgent: () => fakeAgent(events),
      openStateDatabase: () => new DatabaseSync(':memory:'),
      fetch: async () => { throw new Error('model network is not expected'); },
    });
    const response = await fetch(`http://127.0.0.1:${runtime.port}/v1/configured-identity`, {
      method: 'POST', headers: { authorization: `Bearer ${file.config.bearerToken}`,
        'content-type': 'application/json' }, body: '{}', redirect: 'error',
    });
    assert.equal(response.status, 200);
    const observed = await response.json() as { identity: { channel: string; owner_id: string; route: { mode: string } }; credential_proof: string };
    assert.equal(observed.identity.channel, 'telegram');
    assert.equal(observed.identity.owner_id, '123');
    assert.equal(observed.identity.route.mode, 'fixed');
    assert.match(observed.credential_proof, /^[a-f0-9]{64}$/);
    assert.equal(JSON.stringify(observed).includes('test-token'), false);
  } finally { await runtime?.close(); await rm(directory, { recursive: true, force: true }); }
});

test('Photon bootstrap omitted skill root uses installed formatter; explicit root is honored without fallback', async () => {
  const {createPhotonTransport}=await import('../src/channels/transport.js');
  const {existsSync}=await import('node:fs');
  const {fileURLToPath}=await import('node:url');
  const canonicalPack=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../../../sotto-chief-of-staff');
  for(const skillPackRoot of [undefined,canonicalPack]){
    const directory=await mkdtemp(path.join(os.tmpdir(),'photon-default-formatter-'));
    let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined;
    let transport: ReturnType<typeof createPhotonTransport>|undefined;
    let contacts=0;
    let formatterRefusal:Promise<void>|undefined;
    try{
      const config=await writeConfig(directory,{channel:'photon',owner:{accountId:'photon-account',ownerId:'+15555550123'},
        providerThreadId:'imessage:chat-guid;phone=primary',
        credentials:{mode:'cloud',photonProjectId:'project',photonProjectSecret:'fixture-secret'},
        ...(skillPackRoot?{skillPackRoot}: {})});
      runtime=await startConfiguredRuntime(config.configPath,{
        createAdapter(){const adapter=fakePhoton([]);adapter.postMessage=async(_thread,message)=>{
          contacts++;assert.equal(message,'Hello owner');return {id:'accepted'} as never;};return adapter;},
        createTransport(_channel,adapter,options){
          assert.equal(typeof options?.formatText,'function');
          if(!skillPackRoot&&!existsSync('/app/sotto-skills/_shared/lib/chatfmt.py')){
            // Host has no installed /app pack; prove the production factory attempts
            // exactly its image-owned path rather than the old unconditional refusal.
            formatterRefusal=assert.rejects(Promise.resolve().then(()=>options!.formatText!('Hello owner')),error=>
              error instanceof Error && error.message.includes('Command failed:') &&
              error.message.includes('/app/sotto-skills/_shared/lib'));
          }
          transport=createPhotonTransport(adapter as never,options);return transport;
        },createAgent(){return fakeAgent([]);},openStateDatabase(){return new DatabaseSync(':memory:');},
        listenApplication(){return Promise.resolve(32129);},
      });
      const receipt=await transport!.send({threadId:config.config.providerThreadId,text:'**Hello owner**'});
      const installed=!!skillPackRoot||existsSync('/app/sotto-skills/_shared/lib/chatfmt.py');
      if(!installed)assert.ok(formatterRefusal,'the production formatter path was exercised');
      await formatterRefusal;
      assert.equal(receipt.acceptance,installed?'accepted':'not_attempted');assert.equal(contacts,installed?1:0);
      const missing=createPhotonTransport(fakePhoton([]) as never,{formatText:(await import('../src/channels/photon-text-formatter.js')).createPhotonTextFormatter(path.join(directory,'missing'))});
      assert.equal((await missing.send({threadId:'thread',text:'Hello'})).acceptance,'not_attempted');
    }finally{await runtime?.close();await rm(directory,{recursive:true,force:true});}
  }
});

test('native skill pack selection defaults only in the installed layout and preserves explicit paths', async () => {
  const config = {configPath: '/data/private/runtime.json'};
  assert.equal(skillPackRootForConfig(config, 'file:///app/runtime/sotto-chat/dist/bootstrap/main.js'), '/app/sotto-skills');
  assert.equal(skillPackRootForConfig(config, 'file:///workspace/runtime/sotto-chat/dist/bootstrap/main.js'), undefined);
  assert.equal(skillPackRootForConfig(config, 'file:///app/runtime/sotto-chat/src/bootstrap/main.ts'), undefined);
  assert.equal(skillPackRootForConfig({...config,skillPackRoot:'../owner-pack'},
    'file:///app/runtime/sotto-chat/dist/bootstrap/main.js'), '/data/owner-pack');
  const explicit = skillPackRootForConfig({...config,skillPackRoot:'/missing-explicit-pack'},
    'file:///app/runtime/sotto-chat/dist/bootstrap/main.js');
  assert.equal(explicit, '/missing-explicit-pack');
  const {readSkillIndex}=await import('../src/tools/skills.js');
  await assert.rejects(readSkillIndex(explicit!));
});

for (const scan of [false,true]) test(`configured bootstrap dispatches advertised ${scan?'retune scan':'saved attention'} to protected read without private effects`,async()=>{
  const directory=await mkdtemp(path.join(os.tmpdir(),'attention-dispatch-'));
  const events:string[]=[],adapter=fakeTelegram(events),revision='a'.repeat(64);
  let runtime:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined;
  let reads=0,completed=false;
  const expected=scan?{coverage:'currently_permitted_canonical_loops',stale_loops:[],mute_suggestions:[],counts:{stale:0,mute_suggestions:0},current:{timezone:'America/Los_Angeles',mute_senders:[],mute_people:[],mute_sections:[],tone_notes:['Be brief'],vip_people:[],nudge_snooze_until:'',nudge_budget:''}}:{status:'available',freshness:'unknown',attention_queue:[{display_name:'Dana',queue_type:'waiting_on_you',reason:'Saved observation.'}],truncated:false};
  try{
   const config=await writeConfig(directory,{bridge:{url:'http://127.0.0.1:8787/mcp',readBearer:'fixture-read-bearer'}});
   runtime=await startConfiguredRuntime(config.configPath,{
    createAdapter:()=>adapter,createTransport:()=>({send:async()=>({acceptance:'accepted',providerMessageIds:['synthetic-answer']})}),
    openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32149,
    createAgent(agentConfig,_model,_media,_skills,tools){
     assert.ok(agentConfig.instructions.includes(scan?'retune-scan':'relationship-attention'));
     return {...fakeAgent(events),generateChat:async request=>{
      const mounted=await tools!.chat(revision);
      const command=mounted.run_sotto_script as {execute(input:unknown,context:unknown):Promise<any>};
      const values={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,sourceRevision:revision};
      const result=await command.execute({id:scan?'retune-scan':'relationship-attention',args:[]},{requestContext:{get:(key:string)=>values[key as keyof typeof values]},abortSignal:request.signal});
      assert.equal(result.coverage,scan?'currently_permitted_canonical_loops':'saved_relationship_attention');assert.deepEqual(result.result,expected);completed=true;
      return {text:'Saved suggestions have unknown freshness.',sourceRevision:revision};
     }};
    },fetch:async(input,init)=>{
     const url=new URL(String(input));
     if(url.pathname==='/runtime/source-authority')return new Response(JSON.stringify({version:2,complete:true,revision,bridge_sources:[],google_sources:[]}));
     if(url.pathname==='/runtime/knowledge-read'){
      reads++;assert.deepEqual(JSON.parse(init!.body as string),{operation:scan?'retune_scan':'relationship_attention',arguments:{argv:[]},source_revision:revision});
      return new Response(JSON.stringify(expected));
     }
     throw new Error('No other provider or private operation is allowed');
    },
   });
   const message=new Message({id:'attention-owner',threadId:'telegram:123',text:scan?'Show my stale loops':'Who needs attention in the saved snapshot?',formatted:{type:'root',children:[]},raw:{id:'attention-owner',timestamp:new Date()},author:{userId:'123',userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
   await runtime.gateway.chat.processMessage(adapter,'telegram:123',message);
   const deadline=Date.now()+5000;while(!completed&&Date.now()<deadline)await new Promise<void>(resolve=>setTimeout(resolve,10));
   assert.equal(completed,true);assert.equal(reads,1);
   const db=new DatabaseSync(path.join(config.stateDirectory,'operations.sqlite'),{readOnly:true});try{assert.equal(db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);}finally{db.close();}
  }finally{await runtime?.close();await rm(directory,{recursive:true,force:true});}
 });

test('answer-only research uses supplied evidence without Bridge or skill tools and retains authority checks', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-unattended-bridge-grants-'));
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  let agent: ReturnType<typeof createSottoAgentRuntime> | undefined;
  const events: string[] = [];
  let grants: string[] = ['contacts'];
  let complete = true;
  let revision = 'a'.repeat(64);
  let changeAfterModel = false;
  let modelCalls = 0;
  let ownerReads = 0;
  const mcpMethods: string[] = [];
  try {
    const config = await writeConfig(directory, {
      owner: { accountId: 'unattended-owner', ownerId: '123' },
      deployment: 'self_host', modelProxy: undefined,
      modelDirect: { provider: 'google', apiKey: 'fixture-key', chatModelId: 'gemini-fixture', nativeMediaModelId: 'gemini-fixture' },
      bridge: { url: 'http://127.0.0.1:8787/mcp', readBearer: 'fixture-read-bearer' },
    });
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter() { return fakeTelegram(events); },
      createTransport() { return { send: async () => ({ acceptance: 'not_attempted', providerMessageIds: [] }) }; },
      createAgent(_config, model, _media, _skills, tools, authority) {
        agent = createSottoAgentRuntime({ model, storagePath: path.join(directory, 'model-memory.db'),
          instructions: 'Answer from supplied evidence only.', sourceAuthority: authority,
          skillPaths: [path.resolve('../../sotto-chief-of-staff')],
          unattendedTools: context => tools!.unattended(context.sourceRevision) });
        return agent;
      },
      openStateDatabase() { return new DatabaseSync(':memory:'); },
      listenApplication() { return Promise.resolve(32147); },
      fetch: async (input, init) => {
        const request = new Request(input, init);
        const url = new URL(request.url);
        if (url.pathname === '/runtime/source-authority') return new Response(JSON.stringify({ version: 2,
          complete, revision, bridge_sources: grants, google_sources: [] }));
        if (url.pathname === '/mcp') {
          const body = await request.clone().json();
          mcpMethods.push(body.method);
          // Generic MCP discovery failure; actual receiver Relay handles initialize locally.
          return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id,
            error: { code: -32601, message: 'Synthetic Bridge method unavailable' } }),
            { headers: { 'content-type': 'application/json' } });
        }
        if (url.pathname === '/runtime/owner-loops') {
          ownerReads++;
          return new Response(JSON.stringify({ coverage: 'owner_authored_only', you_owe: [], waiting_on_them: [],
            counts: { you_owe: 0, waiting_on_them: 0 } }));
        }
        assert.equal(url.origin, 'https://generativelanguage.googleapis.com');
        const body = await request.clone().json();
        assert.ok(!body.tools || body.tools.length === 0, 'no Bridge or skill tool schemas reach provider');
        modelCalls++;
        if (changeAfterModel) revision = 'b'.repeat(64);
        const parts = [{text: 'Supplied evidence says Cedar opens in November.'}];
        return new Response(JSON.stringify({ candidates: [{ content: { role: 'model', parts }, finishReason: 'STOP' }],
          usageMetadata: { promptTokenCount: 10, candidatesTokenCount: 10, totalTokenCount: 20 } }),
          { headers: { 'content-type': 'application/json' } });
      },
    });
    const result = await agent!.generateUnattended({text: 'Answer only: Cedar opens in November.', runId: 'answer-only', answerOnly: true});
    assert.equal(result.text, 'Supplied evidence says Cedar opens in November.');
    assert.equal(modelCalls, 1); assert.equal(ownerReads, 0); assert.deepEqual(mcpMethods, []);
    complete = false;
    await assert.rejects(agent!.generateUnattended({text: 'Supplied evidence', runId: 'incomplete', answerOnly: true}));
    assert.equal(modelCalls, 1);
    complete = true; changeAfterModel = true;
    await assert.rejects(agent!.generateUnattended({text: 'Supplied evidence', runId: 'changed', answerOnly: true}));
    assert.equal(modelCalls, 2); assert.deepEqual(mcpMethods, []);
  } finally {
    await runtime?.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test('configured agent exposes latest task diagnostics through bearer health, including empty failed turns', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-task-health-bootstrap-'));
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  const adapter = fakeTelegram([]);
  let calls = 0;
  let failModel = false;
  const secret = 'PRIVATE-OWNER-TEXT-AND-ARGS';
  try {
    const config = await writeConfig(directory, {owner: {accountId: 'task-health-fixture', ownerId: '123'}, deployment: 'self_host', modelProxy: undefined,
      modelDirect: {provider: 'google', apiKey: 'fixture', chatModelId: 'gemini-fixture', nativeMediaModelId: 'gemini-fixture'},
      bridge: {url: 'http://127.0.0.1:8787/mcp', readBearer: 'fixture'}});
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter: () => adapter,
      createTransport: () => ({send: async () => ({acceptance: 'accepted', providerMessageIds: ['fixture-reply']})}),
      openStateDatabase: () => new DatabaseSync(':memory:'),
      fetch: async (input, init) => {
        const request = new Request(input, init);
        const url = new URL(request.url);
        if (url.pathname === '/runtime/source-authority') return new Response(JSON.stringify({version: 2,
          complete: true, revision: 'a'.repeat(64), bridge_sources: [], google_sources: []}));
        if (url.pathname === '/runtime/knowledge-read') {
          assert.equal((await request.json()).operation, 'master_file');
          return new Response(JSON.stringify({text: '', chars: 0, cap: 8000, prompt: ''}));
        }
        assert.equal(url.origin, 'https://generativelanguage.googleapis.com'); calls++;
        if (failModel) return new Response(JSON.stringify({error: {message: secret}}), {status: 400});
        const parts = calls === 1 ? [{functionCall: {name: 'run_sotto_script', args: {id: 'loops-query', args: secret}}}]
          : [{text: 'Fixture answer.'}];
        return new Response(JSON.stringify({candidates: [{content: {role: 'model', parts}, finishReason: 'STOP'}],
          usageMetadata: {promptTokenCount: 10, candidatesTokenCount: 10, totalTokenCount: 20}}),
          {headers: {'content-type': 'application/json'}});
      },
    });
    const url = `http://127.0.0.1:${runtime.port}/v1/health`;
    const health = async () => (await fetch(url, {headers: {authorization: `Bearer ${config.config.bearerToken}`}})).json() as Promise<any>;
    assert.equal((await fetch(url)).status, 401);
    assert.equal((await health()).taskToolDiagnostics, undefined);
    let previous = -1;
    for (let turn = 0; turn < 3; turn++) {
      failModel = turn === 2; const id = `fixture-${turn}`;
      await runtime.gateway.chat.processMessage(adapter, 'telegram:123', new Message({id, threadId: 'telegram:123',
        text: secret, formatted: {type: 'root', children: []}, raw: {id, timestamp: new Date()},
        author: {userId: '123', userName: 'owner', fullName: 'Owner', isBot: false, isMe: false},
        metadata: {dateSent: new Date(), edited: false}, attachments: []}));
      let observed = await health(); const deadline = Date.now() + 7000;
      while ((observed.taskToolDiagnostics?.completedAtMs ?? -1) <= previous && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 10)); observed = await health();
      }
      assert.ok(observed.taskToolDiagnostics.completedAtMs > previous); previous = observed.taskToolDiagnostics.completedAtMs;
      assert.deepEqual(observed.taskToolDiagnostics.events, turn === 0 ? [
        {tool: 'run_sotto_script', script: 'loops-query', status: 'unknown', error: 'input_validation'},
      ] : []);
      assert.equal(observed.chatTimingOutcome, turn === 2 ? 'failed' : 'success');
      assert.ok(!JSON.stringify(observed).includes(secret));
    }
    assert.equal(calls, 4, 'telemetry adds no provider contacts');
  } finally { await runtime?.close(); await rm(directory, {recursive: true, force: true}); }
});

test('shared command catalog follows current Granola grant without claiming connectivity', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-command-grants-'));
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  let tools: {chat(revision?: string, authority?: SourceAuthority): Promise<Record<string, unknown>>} | undefined;
  const granted: SourceAuthority = {version: 2, complete: true, revision: 'a'.repeat(64),
    bridge_sources: [], google_sources: [], connector_sources: ['granola']};
  const revoked: SourceAuthority = {...granted, revision: 'b'.repeat(64), connector_sources: []};
  let current = granted, providerReads = 0;
  try {
    const config = await writeConfig(directory, {bridge: {url: 'http://127.0.0.1:8787/mcp', readBearer: 'fixture-read'}});
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter: () => fakeTelegram([]),
      createTransport: () => ({send: async () => ({acceptance: 'not_attempted', providerMessageIds: []})}),
      createAgent: (_config, _model, _media, _skills, bridgeTools) => {tools = bridgeTools as typeof tools; return fakeAgent([]);},
      openStateDatabase: () => new DatabaseSync(':memory:'),
      listenApplication: async () => 32126,
      fetch: async input => {
        if (new URL(String(input)).pathname === '/runtime/source-authority') return Response.json(current);
        providerReads++;
        return new Response('{}', {status: 503}); // A grant is not a live connection guarantee.
      },
    });
    assert.ok(tools);
    const description = async (revision?: string, authority?: SourceAuthority) =>
      ((await tools!.chat(revision, authority)).run_sotto_script as {description: string}).description;
    assert.match(await description(granted.revision!, granted), /granola-history:.*permission allows this read; connector availability/);
    current = revoked;
    for (const [revision, authority] of [[revoked.revision, revoked], [granted.revision, revoked], [undefined, unavailableAuthority()]] as const) {
      const guide = await description(revision ?? undefined, authority);
      assert.doesNotMatch(guide, /granola-history:/);
      assert.match(guide, /knowledge-query:.*loops-query:/); // Saved reads stay available.
    }
    current = granted;
    assert.match(await description(granted.revision!), /granola-history:/);
    assert.equal(providerReads, 0, 'catalog permission must not probe connector connectivity');
  } finally {await runtime?.close(); await rm(directory, {recursive: true, force: true});}
});

test('failed provider stop retains child ownership without contaminating later runtime startup', async () => {
  const child = spawn(process.execPath, ['--import', 'tsx', fileURLToPath(new URL('./fixtures/bootstrap-failed-stop-child.ts', import.meta.url))], {
    stdio: ['ignore','pipe','pipe'],
  });
  let stdout = '', stderr = '';
  child.stdout.setEncoding('utf8'); child.stdout.on('data', (chunk: string) => { stdout += chunk; });
  child.stderr.setEncoding('utf8'); child.stderr.on('data', (chunk: string) => { stderr += chunk; });
  const timer = setTimeout(() => child.kill('SIGKILL'), 20_000);
  try {
    const outcome = await new Promise<{code: number|null; signal: NodeJS.Signals|null}>((resolve,reject) => {
      child.once('error',reject); child.once('close',(code,signal) => resolve({code,signal}));
    });
    assert.deepEqual(outcome,{code:0,signal:null},`${stdout}\n${stderr}`);
    assert.match(stdout,/FAILED_STOP_ASSERTIONS_PASSED/);
  } finally { clearTimeout(timer); if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); }
  const directory = await mkdtemp(path.join(os.tmpdir(), 'sotto-bootstrap-after-stop-'));
  let runtime: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  try {
    const config = await writeConfig(directory);
    runtime = await startConfiguredRuntime(config.configPath, {
      createAdapter: () => fakeTelegram([]),
      createTransport: () => ({send: async () => ({acceptance:'unknown',providerMessageIds:[]})}),
      createAgent: () => fakeAgent([]), openStateDatabase: () => new DatabaseSync(':memory:'),
      listenApplication: async () => 32124,
    });
    await runtime.close();
  } finally { await runtime?.close(); await rm(directory,{recursive:true,force:true}); }
});
