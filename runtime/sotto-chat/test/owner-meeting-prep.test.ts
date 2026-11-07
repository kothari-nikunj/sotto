import {precedingPrepMessage} from '../src/channels/photon-continuation.js';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {once} from 'node:events';
import {existsSync} from 'node:fs';
import {resolve} from 'node:path';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync, readFileSync, rmSync, writeFileSync, unlinkSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {OperationalJournal} from '../src/journal/index.js';
import {createOwnerMeetingPrepClient, meetingPrepSelectors, ownerMeetingPrepKind} from '../src/tools/owner-meeting-prep-client.js';
import {createOwnerMeetingPrepOfferClient} from '../src/tools/owner-meeting-prep-offer-client.js';
import {createOwnerMeetingPrepOfferTool} from '../src/tools/owner-meeting-prep-offer-tool.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';

const identity = {tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64)};
const inbound = {channel: 'photon' as const, accountId: 'account', senderId: 'owner', isDirect: true,
  eventId: 'owner-message', text: 'yes please', replyBound: true, replyToMessageId: 'accepted-offer',
  providerSentAtMs: Date.now()};
const globalPrepInbound = inbound;
const context = {threadId: 'thread', destination: 'owner', lineKey: 'line'};

test('prep selection requires an exact affirmative reply, never timing, numbers or injected selectors', () => {
  assert.equal(meetingPrepSelectors({...inbound, replyBound: false, replyToMessageId: undefined}), null);
  assert.equal(meetingPrepSelectors({...inbound, replyToMessageId: undefined}), null);
  for (const text of ['2', 'no', 'yes and send the email', 'set it up'])
    assert.equal(meetingPrepSelectors({...inbound, text}), null);
  assert.equal(meetingPrepSelectors({...inbound, media: [{data: new Uint8Array([1])}]}), null);
  assert.deepEqual(meetingPrepSelectors(inbound), {provider_message_id: inbound.eventId,
    reply_to_message_id: inbound.replyToMessageId, provider_sent_at_ms: inbound.providerSentAtMs});
});

test('journal binds prep selector to original owner admission and preserves uncertain action across reopen', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-prep-journal-'));
  const path = join(directory, 'journal.sqlite');
  let journal = new OperationalJournal(path);
  const authority = {runId: 'run', accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon' as const,
    scopeHash: 'c'.repeat(64)};
  try {
    const event = journal.admitInbound({eventId: inbound.eventId, accountKey: 'account', lineKey: 'line',
      threadKey: 'thread', channel: 'photon', sender: 'owner', payload: JSON.stringify({inbound, context})}).event.id as string;
    journal.startRun({runId: 'run', eventId: event});
    const args = meetingPrepSelectors(inbound)!;
    assert.throws(() => journal.prepareOwnerMeetingPrepAction(authority, event, {...args, reply_to_message_id: 'other-meeting'}), /differs/);
    assert.throws(() => journal.prepareOwnerMeetingPrepAction(authority, event, {...args, provider_sent_at_ms: args.provider_sent_at_ms! + 1}), /differs/);
    const request = journal.prepareOwnerMeetingPrepAction(authority, event, args);
    journal.beginAction(request.actionId);
    journal.close();
    journal = new OperationalJournal(path);
    journal.recoverInterrupted();
    const recovered = journal.readOwnerMeetingPrepAction(request.actionId, authority);
    assert.equal(recovered.status, 'unknown');
    assert.deepEqual(recovered.request, request);
    assert.throws(() => journal.beginAction(request.actionId), /held|pending|eligible|unknown/i);
    assert.equal(journal.listOwnerMeetingPrepActionsForThread(event).length, 1);
  } finally {journal.close(); rmSync(directory, {recursive: true, force: true});}
});

test('prep client binds accepted job receipt to the exact owner proof and does not retry a bad response', async () => {
  const args = meetingPrepSelectors(inbound)!;
  const eventId = 'a'.repeat(64), payloadHash = 'b'.repeat(64);
  const binding = stableActionIdentity(eventId, ownerMeetingPrepKind, args);
  const request = {eventId, payloadHash, actionId: binding.actionId, text: inbound.text,
    admittedAtMs: Date.now(), providerThreadId: 'thread', args};
  let calls = 0, forge = false;
  const client = createOwnerMeetingPrepClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (url, init) => {
      calls++;
      assert.equal(String(url), 'http://127.0.0.1:8080/runtime/owner-meeting-prep-request');
      assert.equal(init!.redirect, 'error');
      const body = JSON.parse(init!.body as string);
      assert.deepEqual(body.args, args);
      return Response.json({version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
        payload_hash: body.payload_hash, args_hash: forge ? 'f'.repeat(64) : binding.payloadHash,
        outcome: {operation: 'admit', request_id: 'd'.repeat(32), offer_id: 'e'.repeat(64), status: 'queued'}});
    }});
  assert.equal((await client.call('admit', request)).status, 'committed');
  forge = true;
  await assert.rejects(client.call('inspect', request), /receipt is invalid/);
  assert.equal(calls, 2);
});

import {ConversationCoordinator} from '../src/conversation.js';
for (const text of ['yes please', 'Full prep on Dana', 'Give me full prep on Dana']) for (const lost of [false, true]) test(`coordinator exact prep admission ${text} survives lost response and reopen=${lost}`, async () => {
  const inbound = {...globalPrepInbound, text};
  const folder = mkdtempSync(join(tmpdir(), 'sotto-prep-coordinator-'));
  const path = join(folder, 'journal.sqlite');
  let journal = new OperationalJournal(path), held = lost;
  const contacts: string[] = [], sends: string[] = [], model: string[] = [];
  const client = createOwnerMeetingPrepClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      contacts.push(body.operation);
      assert.equal(body.args.provider_message_id, inbound.eventId);
      assert.equal(body.args.reply_to_message_id, inbound.replyToMessageId);
      if (body.operation === 'preview') return Response.json({version:1,status:'match',offer_id:'e'.repeat(64),kind:'meeting_prep'});
      if (held && body.operation === 'admit') throw new Error('lost response');
      if (held) return Response.json({version: 1, status: 'not_found'});
      return Response.json({version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id,
        payload_hash: body.payload_hash, args_hash: stableActionIdentity(body.event_id, ownerMeetingPrepKind, body.args).payloadHash,
        outcome: {operation: 'admit', request_id: 'd'.repeat(32), offer_id: 'e'.repeat(64), status: 'queued'}});
    }});
  const create = () => new ConversationCoordinator({journal,
    ownerPolicy: {channel: 'photon', accountId: 'account', ownerId: 'owner'}, ownerMeetingPrep: client,
    model: {generate: async request => {model.push(request.text); return 'Safe answer';}},
    postPrepared: async request => {sends.push(request.text); return {acceptance: 'accepted', messageId: `reply-${sends.length}`};}});
  let coordinator = create();
  try {
    assert.equal((await coordinator.receive({...inbound, eventId: 'foreign', senderId: 'other'}, context)).status, 'denied');
    const first = await coordinator.receive(inbound, context);
    if (lost) {
      assert.equal(first.status, 'held');
      assert.match(sends[0]!, /I couldn’t confirm whether the meeting prep request/);
      assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_meeting_prep_request'").get()!.status, 'unknown');
      await coordinator.close(); journal.close();
      journal = new OperationalJournal(path); journal.recoverInterrupted(); coordinator = create();
      await coordinator.receive({...inbound, eventId: 'safe', text: 'hello', replyBound: false, replyToMessageId: undefined}, context);
      assert.deepEqual(model, ['hello']);
      // A later exact receipt is inspected only after the persisted cadence is due.
      journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_meeting_prep_request' AND status='unknown'").run();
      held = false;
      await coordinator.recover();
    } else assert.ok(['accepted', 'silent'].includes(first.status));
    assert.equal(contacts.filter(x => x === 'admit').length, 1);
    assert.equal(sends.filter(x => x === 'Your meeting prep is queued.').length, 1);
    assert.equal((await coordinator.receive(inbound, context)).status, 'duplicate');
    assert.equal(model.filter(x => x !== 'hello').length, 0);
  } finally {await coordinator.close(); journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('unmatched exact affirmative remains ordinary chat without a prep mutation', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-prep-miss-'));
  const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
  const client = createOwnerMeetingPrepClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async () => Response.json({version: 1, status: 'no_match'})});
  let answer = '';
  const coordinator = new ConversationCoordinator({journal, ownerMeetingPrep: client,
    ownerPolicy: {channel: 'photon', accountId: 'account', ownerId: 'owner'},
    model: {generate: async () => 'Please identify the offer you mean.'},
    postPrepared: async request => {answer = request.text; return {acceptance: 'accepted', messageId: 'clarification'};}});
  try {
    assert.ok(['accepted', 'silent'].includes((await coordinator.receive({...inbound, replyToMessageId: 'wrong-offer'}, context)).status));
    assert.equal(answer,'Please identify the offer you mean.');
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
  } finally {await coordinator.close(); journal.close(); rmSync(folder, {recursive: true, force: true});}
});

async function startReceiver(folder: string, channel: 'photon' | 'telegram' = 'photon', ageSeconds = 0, ttlMinutes = 180, proposal = false, proposalStart?: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_TELEGRAM_ROUTINE: channel === 'telegram' ? '1' : '0', SOTTO_TEST_MEETING_PREP: '1',
      SOTTO_TEST_PREP_OFFER_PROPOSAL: proposal ? '1' : '0',
      ...(proposalStart ? {SOTTO_TEST_PREP_OFFER_START:proposalStart} : {}),
      SOTTO_TEST_PREP_OFFER_AGE_SECONDS: String(ageSeconds), SOTTO_TEST_PREP_OFFER_TTL_MINUTES: String(ttlMinutes),
      SOTTO_ROUTINE_EPOCH: 'e'.repeat(32), SOTTO_TEST_ROUTINE_SOURCE_REVISION: 'a'.repeat(64), SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
      SOTTO_DATA: folder, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'tenant', SOTTO_ACCOUNT_ID: 'account',
      SOTTO_LINE_KEY: 'line', SOTTO_MCP_TOKEN: '0123456789abcdef0123456789abcdef',
      PHOTON_HOME_CHANNEL: '+15555550123', PHOTON_ALLOWED_USERS: '+15555550123', PHOTON_PROJECT_ID: 'fixture-project',
      PHOTON_PROJECT_SECRET: 'fixture-secret', PHOTON_ALLOW_ALL_USERS: 'false', SOTTO_IMESSAGE_NUMBER: '+15555550999',
      SOTTO_TIMEZONE: 'America/Los_Angeles' } });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr = (stderr + String(chunk)).slice(-4096); });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const line = await Promise.race([new Promise<string>((done, fail) => {
        let output = '';
        child.stdout.on('data', chunk => { output += String(chunk); const i = output.indexOf('\n'); if (i >= 0)
          done(output.slice(0, i)); });
        child.once('exit', code => fail(new Error(`receiver exited ${code}: ${stderr}`)));
        child.once('error', fail);
      }), new Promise<never>((_, fail) => { timer = setTimeout(() => fail(new Error('receiver start timeout')), 15000); })]);
    const parsed = JSON.parse(line) as {
      port: number;
      meetingOfferMessageId: string;
      identity: OwnerToolIdentity;
      bearer: string;
    };
    return { child, ...parsed, bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp` };
  }
  catch (error) {
    child.kill('SIGKILL');
    throw error;
  }
  finally {
    if (timer)
      clearTimeout(timer);
  }
}
async function stopReceiver(child: ChildProcessWithoutNullStreams) {
  if (child.exitCode !== null)
    return;
  const ended = once(child, 'exit');
  child.stdin.end('quit\n');
  const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
  try {
    await ended;
  }
  finally {
    clearTimeout(timer);
  }
}
for (const variant of [{loseResponse: false, unthreaded: false, rescue: false, text: 'yes please'}, {loseResponse: true, unthreaded: false, rescue: false, text: 'yes please'}, {loseResponse: true, unthreaded: true, rescue: false, text: 'yes please'},
  {loseResponse: false, unthreaded: true, rescue: true, text: 'Yes', delayMinutes: 61},
  ...['Yes', 'yes pls', 'sure', 'full prep pls', 'Full prep on Dana', 'prep for Dana'].map(text => ({loseResponse: text === 'Yes', unthreaded: true, rescue: true, text}))]) {
const {loseResponse, unthreaded, rescue, text} = variant;
const delayMinutes = 'delayMinutes' in variant ? Number(variant.delayMinutes) : 0;
test(`actual scoped HTTP receiver commits original queue receipt with lost-response=${loseResponse}, rescue=${rescue}, text=${text}, ageMinutes=${delayMinutes}`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-brief-http-'));
  const receiver = await startReceiver(folder, 'photon', delayMinutes*60);
  let journal = new OperationalJournal(join(folder, 'node.sqlite'));
  const sent: string[] = [];
  let held = loseResponse;
  const contacts: string[] = [];
  const client = createOwnerMeetingPrepClient({...receiver, fetch: async (url, init) => {
    const body = JSON.parse(init!.body as string);
    contacts.push(body.operation);
    if (held && body.operation === 'inspect') return Response.json({version: 1, status: 'not_found'});
    const result = await fetch(url, init);


    if (loseResponse && body.operation === 'admit' && result.status === 200) {
      await result.body?.cancel();
      throw new Error('Synthetic lost response after actual queue commit');
    }
    return result;
  }});
  let modelCalls = 0;
  const runtime = createSottoAgentRuntime({storagePath: join(folder, 'memory.sqlite'), instructions: 'Answer the read-only owner question.',
    sourceAuthority: {read: async () => ({version: 2, complete: true, revision: 'a'.repeat(64), bridge_sources: [], google_sources: []})},
    model: new MockLanguageModelV4({doGenerate: async () => {
      modelCalls++;
      return {content: [{type: 'text', text: 'Safe independent answer'}], finishReason: {unified: 'stop', raw: 'STOP'},
        usage: {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}}, warnings: []};
    }})});
  let providerProofReads = 0;
  const createCoordinator = () => new ConversationCoordinator({journal,
    routeDigestForThread: () => 'b'.repeat(64),
    resolvePrepContinuation: async request => {
      providerProofReads++;
      assert.equal(request.candidateAtMs,offeredAt);
      return precedingPrepMessage({messages: [
        {guid: receiver.meetingOfferMessageId, isFromMe: true, dateCreated: new Date(offeredAt)},
        {guid: request.inboundId, isFromMe: false, sender: {address: receiver.identity.owner_id}, dateCreated: new Date(request.inboundAtMs)},
      ]}, request, receiver.identity.owner_id);
    },
    ownerPolicy: {channel: 'photon', accountId: receiver.identity.account_id, ownerId: receiver.identity.owner_id},
    ownerMeetingPrep: client, model: {generate: request => runtime.generateChat({...request, threadId: 'memory', ownerId: 'owner'})},
    canPostPrepared: request => runtime.canDeliverReplySource(request),
    postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: 'synthetic-ack'};}});
  let service = createCoordinator();
  const offeredAt = Date.now() - (delayMinutes ? delayMinutes*60_000 : 1000);
  if (unthreaded) {
    journal.prepareDispatch({dispatchId: 'native-prep-offer', kind: 'scheduled_text', destination: 'owner', payload: 'Prep?', routeDigest: 'b'.repeat(64)});
    journal.beginDispatch('native-prep-offer');
    journal.settleDispatch('native-prep-offer', 1, 'accepted', receiver.meetingOfferMessageId, [receiver.meetingOfferMessageId], offeredAt);
    if(delayMinutes){
      journal.db.prepare("UPDATE dispatches SET created_at=?,updated_at=? WHERE id='native-prep-offer'").run(offeredAt-1,offeredAt);
      journal.db.prepare("UPDATE dispatch_attempts SET started_at=?,finished_at=? WHERE dispatch_id='native-prep-offer'").run(offeredAt-1,offeredAt);
    }
    await new Promise(done => setTimeout(done, 5));
  }
  if (rescue) {
    journal.prepareDispatch({dispatchId: 'old-uncertain', kind: 'scheduled_text', destination: 'owner', payload: 'Old notice', routeDigest: 'b'.repeat(64)});
    journal.beginDispatch('old-uncertain'); journal.settleDispatch('old-uncertain', 1, 'unknown');
    journal.db.prepare("UPDATE dispatches SET created_at=created_at-86400000, updated_at=updated_at-86400000 WHERE id='old-uncertain'").run();
  }
  const uncertainBefore = rescue ? {...journal.getDispatch('old-uncertain')!} : undefined;
  try {
    const input = {...inbound, eventId: 'http-provider-original', text, senderId: receiver.identity.owner_id,
      replyBound: !unthreaded, replyToMessageId: unthreaded ? undefined : receiver.meetingOfferMessageId};
    const result = await service.receive(input, {...context, threadId: receiver.identity.route_id}, unthreaded ? Date.now() : undefined);
    if (loseResponse) {
      assert.equal(result.status, 'held');
      assert.match(sent[0]!, /I couldn’t confirm whether the meeting prep request/);
      assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_meeting_prep_request'").get()!.status, 'unknown');
      assert.equal(modelCalls, 0);
      await service.close();
      journal.close();
      journal = new OperationalJournal(join(folder, 'node.sqlite'));
      journal.recoverInterrupted();
      service = createCoordinator();
      await service.receive({...inbound, eventId: 'safe-http', text: 'hello while prep is held', replyBound: false, replyToMessageId: undefined, senderId: receiver.identity.owner_id}, {...context, threadId: receiver.identity.route_id});
      assert.equal(modelCalls, 1);
      // A later exact receipt is inspected only after the persisted cadence is due.
      journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_meeting_prep_request' AND status='unknown'").run();
      held = false;
      await service.recover();
      assert.equal(modelCalls, 1);
      assert.match(sent.at(-1)!, /^Your meeting prep is queued/);
    } else {
      assert.ok(['accepted', 'silent'].includes(result.status));
      assert.equal(modelCalls, 0);
      assert.deepEqual(sent, ['Your meeting prep is queued.']);
    }
    if (rescue) assert.deepEqual({...journal.getDispatch('old-uncertain')!}, uncertainBefore, 'provider context must not settle or replay uncertain sends');
    assert.equal(contacts.filter(operation => operation === 'admit').length, 1);
    const action = journal.db.prepare("SELECT * FROM actions WHERE kind='owner_meeting_prep_request'").get()!;
    assert.equal(action.status, 'succeeded');
    if(delayMinutes){
      assert.equal(providerProofReads,1);
      const event=journal.getInbound(String(action.source_event_id))!;
      assert.equal(JSON.parse(String(event.continuation_json)).eligible,true);
      assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_meeting_prep_request'").get()!.n,1);
    }
    const metadata = JSON.parse(action.result_json as string);
    assert.equal(metadata.args.provider_message_id, input.eventId);
    const saved = journal.readOwnerMeetingPrepAction(action.id as string, {accountId: 'account', ownerId: receiver.identity.owner_id,
      channel: 'photon', lineKey: 'line', scopeHash: client.scopeHash});
    assert.deepEqual(await client.call('inspect', saved.request), metadata.receipt);
    assert.deepEqual(await client.call('inspect', saved.request), metadata.receipt);
    assert.equal((await service.receive(input, {...context, threadId: receiver.identity.route_id})).status, 'duplicate');
  } finally {await service.close(); await runtime.close(); journal.close(); await stopReceiver(receiver.child); rmSync(folder, {recursive: true, force: true});}
});

}


test('actual receiver rejects an accepted short-TTL prep offer despite valid provider adjacency', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-prep-short-ttl-'));
  const receiver=await startReceiver(folder,'photon',120,1);
  const journal=new OperationalJournal(join(folder,'node.sqlite'));
  const contacts:string[]=[];
  let models=0,proofReads=0;
  const client=createOwnerMeetingPrepClient({...receiver,fetch:async(url,init)=>{
    contacts.push(JSON.parse(String(init!.body)).operation);
    return fetch(url,init);
  }});
  const offeredAt=Date.now()-120_000;
  journal.prepareDispatch({dispatchId:'expired-prep',kind:'scheduled_text',destination:'owner',payload:'Prep?',routeDigest:'b'.repeat(64)});
  journal.beginDispatch('expired-prep');
  journal.settleDispatch('expired-prep',1,'accepted',receiver.meetingOfferMessageId,[receiver.meetingOfferMessageId],offeredAt);
  journal.db.prepare("UPDATE dispatches SET created_at=?,updated_at=? WHERE id='expired-prep'").run(offeredAt-1,offeredAt);
  journal.prepareDispatch({dispatchId:'historical-unknown',kind:'scheduled_text',destination:'owner',payload:'Old notice',routeDigest:'b'.repeat(64)});
  journal.beginDispatch('historical-unknown');journal.settleDispatch('historical-unknown',1,'unknown');
  const service=new ConversationCoordinator({journal,routeDigestForThread:()=> 'b'.repeat(64),ownerMeetingPrep:client,
    ownerPolicy:{channel:'photon',accountId:receiver.identity.account_id,ownerId:receiver.identity.owner_id},
    resolvePrepContinuation:async request=>{
      proofReads++;
      return precedingPrepMessage({messages:[
        {guid:receiver.meetingOfferMessageId,isFromMe:true,dateCreated:new Date(offeredAt)},
        {guid:request.inboundId,isFromMe:false,sender:{address:receiver.identity.owner_id},dateCreated:new Date(request.inboundAtMs)}]},request,receiver.identity.owner_id);
    },model:{generate:async()=>{models++;return 'Please identify a current offer.';}},
    postPrepared:async()=>({acceptance:'accepted',messageId:'safe-clarification'})});
  try {
    await service.receive({...inbound,eventId:'short-ttl-yes',text:'Yes',replyBound:false,replyToMessageId:undefined,senderId:receiver.identity.owner_id},
      {...context,threadId:receiver.identity.route_id},Date.now());
    assert.equal(proofReads,1);
    assert.deepEqual(contacts,['preview'],'expired receiver receipt never reaches admission');
    assert.equal(models,1,'no_match returns to ordinary safe clarification');
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_meeting_prep_request'").get()!.n,0);
    assert.equal(journal.getDispatch('historical-unknown')!.status,'unknown');
  } finally {await service.close();journal.close();await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});

for (const {unthreaded,permanentRefusal,olderTransient} of [{unthreaded:false,permanentRefusal:false,olderTransient:false},
  {unthreaded:true,permanentRefusal:false,olderTransient:false},{unthreaded:true,permanentRefusal:true,olderTransient:false},
  {unthreaded:true,permanentRefusal:false,olderTransient:true}]) {
test(`typed meeting-prep offer activates only after accepted exact question and repairs lost record ACK after restart unthreaded=${unthreaded} permanentRefusal=${permanentRefusal} olderTransient=${olderTransient}`, async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-prep-offer-accepted-http-'));
  const proposalStart=new Date(Date.now()+24*60*60_000).toISOString();
  let receiver=await startReceiver(join(folder,'receiver'),'photon',0,180,true,proposalStart);
  let journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let coordinator:ConversationCoordinator|undefined;
  let loseRecordAck=true,recordCalls=0,rejectRecord=permanentRefusal;
  let transientEventId:string|undefined,olderTransientRecords=0;
  const blockedFirst=permanentRefusal||olderTransient;
  const source={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['calendar' as const]})};
  const offerClient=createOwnerMeetingPrepOfferClient({...receiver,fetch:async(input,init)=>{
    const body=JSON.parse(String(init!.body)) as {operation:string;event_id:string};
    if(body.operation==='record_offer'&&rejectRecord)writeFileSync(join(folder,'receiver','synthetic-calendar-cancelled'),'1');
    const unavailable=join(folder,'receiver','synthetic-calendar-unavailable');
    if(body.operation==='record_offer'&&olderTransient){
      transientEventId??=body.event_id;
      if(body.event_id===transientEventId){writeFileSync(unavailable,'1');olderTransientRecords++;}
      else if(existsSync(unavailable))unlinkSync(unavailable);
    }
    const response=await fetch(input,init);
    if(body.operation==='record_offer'){
      recordCalls++;
      if(olderTransient&&body.event_id===transientEventId){assert.equal(response.status,503);return response;}
      if(rejectRecord){
        assert.equal(response.status,409);
        assert.equal((await response.clone().json()).code,'invalid_target','real cancelled Calendar target is a permanent rejection');
        return response;
      }
      assert.equal(response.status,200);
      if(loseRecordAck){await response.body?.cancel();throw new Error('synthetic lost record_offer acknowledgement after receiver commit');}
    }
    return response;
  }});
  const prepClient=createOwnerMeetingPrepClient(receiver);
  const offerTools=createOwnerMeetingPrepOfferTool({client:offerClient,journal:()=>journal,sourceAuthority:source});
  const sent:string[]=[];
  const acceptedQuestions:{id:string;at:number}[]=[];let providerProofReads=0;
  const ownerPolicy={channel:'photon' as const,accountId:receiver.identity.account_id,ownerId:receiver.identity.owner_id};
  const context={threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'};
  const createCoordinator=()=>new ConversationCoordinator({journal,ownerPolicy,routeDigestForThread:()=> 'b'.repeat(64),ownerMeetingPrep:prepClient,
    resolvePrepContinuation:async request=>{
      providerProofReads++;
      const accepted=acceptedQuestions[blockedFirst?1:0]!;
      assert.equal(request.candidateAtMs,accepted.at);
      return precedingPrepMessage({messages:[
        {guid:accepted.id,isFromMe:true,dateCreated:new Date(accepted.at)},
        {guid:request.inboundId,isFromMe:false,sender:{address:ownerPolicy.ownerId},dateCreated:new Date(request.inboundAtMs)},
      ]},request,ownerPolicy.ownerId);
    },
    ownerMeetingPrepOffer:offerClient,model:{generate:async request=>{
      const values:Record<string,unknown>={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,
        allowOwnerMeetingPrepOffer:request.allowOwnerMeetingPrepOffer,sourceRevision:'a'.repeat(64)};
      const tool=offerTools.owner_meeting_prep_offer;
      const execute=tool.execute as (input:unknown,context:unknown)=>Promise<unknown>;
      const result=await execute({operation:'propose_prep',calendar_id:'primary',event_id:'fixture-event-123',
        start:proposalStart,attendee_email:'ada@cedar.example'},
        {requestContext:{get:(key:string)=>values[key]},abortSignal:request.signal});
      assert.equal((result as {status?:string}).status,'proposed',JSON.stringify(result));
      return (result as {review_text:string}).review_text;
    }},
    postPrepared:async request=>{sent.push(request.text);const accepted={id:`offer-question-${sent.length}`,at:Date.now()};acceptedQuestions.push(accepted);return {acceptance:'accepted',messageId:accepted.id,providerSentAtMs:accepted.at};}});
  try{
    coordinator=createCoordinator();
    const first=await coordinator.receive({channel:'photon',accountId:receiver.identity.account_id,senderId:ownerPolicy.ownerId,isDirect:true,eventId:'proposal-event',
      text:'Could you help me prep for Ada?',providerSentAtMs:Date.now()},context);
    assert.ok(['accepted','silent'].includes(first.status),JSON.stringify(first));
    assert.equal(sent.length,1);
    const date=new Date(proposalStart);
    const weekday=new Intl.DateTimeFormat('en-US',{weekday:'long',timeZone:'America/Los_Angeles'}).format(date);
    const clock=new Intl.DateTimeFormat('en-US',{hour:'numeric',minute:'2-digit',hour12:true,timeZone:'America/Los_Angeles'}).format(date);
    assert.equal(sent[0],`I can prepare for Cedar planning review on ${weekday} at ${clock}. Want me to?`);
    const debugAction=journal.db.prepare("SELECT * FROM actions WHERE kind='owner_meeting_prep_offer'").get() as {source_event_id:string};
    assert.equal(recordCalls,1,JSON.stringify({actions:journal.db.prepare("SELECT kind,status,result_json,source_event_id FROM actions").all(),dispatches:journal.db.prepare("SELECT * FROM dispatches").all(),reply:journal.getReplyForEvent(debugAction.source_event_id),accepted:journal.acceptedOwnerMeetingPrepOffer(journal.db.prepare("SELECT id FROM actions WHERE kind='owner_meeting_prep_offer'").get()!.id as string,{accountId:'account',ownerId:ownerPolicy.ownerId,lineKey:'line',channel:'photon',scopeHash:offerClient.scopeHash}),pending:journal.listPendingOwnerMeetingPrepOffers()}));
    assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_meeting_prep_offer'").get()!.status,'succeeded');
    if(blockedFirst){
      if(permanentRefusal)assert.equal(JSON.parse(String(journal.db.prepare("SELECT result_json FROM actions WHERE kind='owner_meeting_prep_offer'").get()!.result_json)).offer_refusal,'invalid_target');
      assert.equal(journal.listPendingOwnerMeetingPrepOffers().length,olderTransient?1:0,'only a transient older refusal remains pending');
      rejectRecord=false;if(permanentRefusal)unlinkSync(join(folder,'receiver','synthetic-calendar-cancelled'));
      if(olderTransient)unlinkSync(join(folder,'receiver','synthetic-calendar-unavailable'));
      await new Promise(resolve=>setTimeout(resolve,2));
      const another=await coordinator.receive({channel:'photon',accountId:receiver.identity.account_id,senderId:ownerPolicy.ownerId,isDirect:true,
        eventId:'different-proposal-event',text:'Could you offer preparation for Ada again?',providerSentAtMs:Date.now()},context);
      assert.ok(['accepted','silent'].includes(another.status));
      assert.equal(sent.length,2,'a later distinct owner request can create its own accepted offer');
    }
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_meeting_prep_request'").get()!.n,0,
      'the accepted offer question alone has not queued prep');
    assert.equal(journal.listPendingOwnerMeetingPrepOffers().length,olderTransient?2:1,'selected lost ACK remains pending alongside any older transient offer');
    const reads=readFileSync(join(folder,'receiver','synthetic-calendar-reads.txt'),'utf8').trim().split('\n').length;
    assert.equal(reads,blockedFirst?8:4,'preview and first record each perform the bounded Calendar revalidation');

    await coordinator.close();coordinator=undefined;journal.close();
    journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();
    loseRecordAck=false;coordinator=createCoordinator();
    if(unthreaded){
      journal.prepareDispatch({dispatchId:'older-unknown',kind:'scheduled_text',destination:'photon',payload:'Old notice',routeDigest:'b'.repeat(64)});
      journal.beginDispatch('older-unknown');journal.settleDispatch('older-unknown',1,'unknown');
      const beforeOffer=acceptedQuestions[blockedFirst?1:0]!.at-1000;
      journal.db.prepare("UPDATE dispatches SET created_at=?,updated_at=? WHERE id='older-unknown'").run(beforeOffer,beforeOffer);
    }
    await new Promise(resolve=>setTimeout(resolve,2));
    const yesAt=Date.now();
    const oldRecordCalls=olderTransientRecords;
    const yes=await coordinator.receive({channel:'photon',accountId:receiver.identity.account_id,senderId:ownerPolicy.ownerId,isDirect:true,eventId:'proposal-yes',text:'Yes',
      replyBound:!unthreaded,replyToMessageId:unthreaded?undefined:acceptedQuestions[blockedFirst?1:0]!.id,providerSentAtMs:yesAt},context,unthreaded?yesAt:undefined);
    assert.ok(['accepted','silent'].includes(yes.status),JSON.stringify(yes));
    assert.equal(providerProofReads,unthreaded?1:0,'unthreaded Yes uses actual accepted outgoing adjacency proof');
    if(unthreaded)assert.equal(journal.db.prepare("SELECT status FROM dispatches WHERE id='older-unknown'").get()!.status,'unknown','native adjacency does not rewrite historical unknown delivery');
    assert.equal(recordCalls,blockedFirst?3:2,'the next affirmative repairs the same accepted offer before selecting it');
    assert.equal(readFileSync(join(folder,'receiver','synthetic-calendar-reads.txt'),'utf8').trim().split('\n').length,reads,
      'restart/affirmative recovery does not reread or replace the accepted occurrence');
    const offerReceipt=JSON.parse(String(journal.db.prepare("SELECT result_json FROM actions WHERE kind='owner_meeting_prep_offer'").get()!.result_json));
    if(!blockedFirst)assert.ok(offerReceipt.offer_receipt?.provider_message_id);
    else if(permanentRefusal)assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_meeting_prep_offer' AND json_extract(result_json,'$.offer_refusal')='invalid_target'").get()!.n,1,'the old refused action remains terminal');
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_meeting_prep_request' AND status='succeeded'").get()!.n,1);
    assert.ok(sent.some(text=>text==='Your meeting prep is queued.'));
    if(olderTransient){
      assert.equal(olderTransientRecords,oldRecordCalls,'selected-question repair never contacts the older transient offer');
      assert.equal(journal.listPendingOwnerMeetingPrepOffers().length,1,'older transient evidence remains pending without holding selected success');
    }
  }finally{await coordinator?.close();journal.close();await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});

}

test('uncertain delivery of the typed prep question never activates the receiver offer',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-prep-offer-undelivered-'));
  const proposalStart=new Date(Date.now()+24*60*60_000).toISOString();
  const receiver=await startReceiver(join(folder,'receiver'),'photon',0,180,true,proposalStart);
  const journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let recordCalls=0;
  const source={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['calendar' as const]})};
  const client=createOwnerMeetingPrepOfferClient({...receiver,fetch:async(input,init)=>{
    const body=JSON.parse(String(init!.body)) as {operation:string};
    if(body.operation==='record_offer')recordCalls++;
    return fetch(input,init);
  }});
  const tools=createOwnerMeetingPrepOfferTool({client,journal:()=>journal,sourceAuthority:source});
  const ownerPolicy={channel:'photon' as const,accountId:receiver.identity.account_id,ownerId:receiver.identity.owner_id};
  const coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerMeetingPrepOffer:client,
    model:{generate:async request=>{
      const values:Record<string,unknown>={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,
        allowOwnerMeetingPrepOffer:request.allowOwnerMeetingPrepOffer,sourceRevision:'a'.repeat(64)};
      const execute=tools.owner_meeting_prep_offer.execute as (input:unknown,context:unknown)=>Promise<unknown>;
      const result=await execute({operation:'propose_prep',calendar_id:'primary',
        event_id:'fixture-event-123',start:proposalStart,attendee_email:'ada@cedar.example'},
        {requestContext:{get:(key:string)=>values[key]},abortSignal:request.signal});
      return (result as {review_text:string}).review_text;
    }},postPrepared:async()=>({acceptance:'unknown'})});
  try{
    const result=await coordinator.receive({channel:'photon',accountId:receiver.identity.account_id,senderId:ownerPolicy.ownerId,
      isDirect:true,eventId:'uncertain-offer-event',text:'Can you offer preparation?',providerSentAtMs:Date.now()},
      {threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'});
    assert.equal(result.status,'held');
    assert.equal(recordCalls,0,'a question without an accepted provider receipt is never activated');
    assert.equal(journal.listPendingOwnerMeetingPrepOffers().length,0);
    assert.equal(existsSync(join(folder,'receiver','proactive','pending_offer.json')),false);
  }finally{await coordinator.close();journal.close();await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});

for(const unthreaded of [false,true])test(`unconfirmed accepted prep repair replies once without starting work, unthreaded=${unthreaded}`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-prep-repair-unconfirmed-'));
  const proposalStart=new Date(Date.now()+24*60*60_000).toISOString();
  let receiver=await startReceiver(join(folder,'receiver'),'photon',0,180,true,proposalStart);
  let journal=new OperationalJournal(join(folder,'operations.sqlite'));
  let coordinator:ConversationCoordinator|undefined;
  let recordCalls=0,modelCalls=0,prepCalls=0,denyRepair=false;
  const sent:Array<{text:string;id:string;at:number}> = [];
  const source={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:['calendar' as const]})};
  const offerClient=createOwnerMeetingPrepOfferClient({...receiver,fetch:async(input,init)=>{
    const body=JSON.parse(String(init!.body)) as {operation:string};
    if(body.operation==='record_offer'){
      recordCalls++;
      if(denyRepair)return Response.json({error:'owner tool unavailable'},{status:503});
    }
    const response=await fetch(input,init);
    if(body.operation==='record_offer'&&!denyRepair&&recordCalls===1){
      await response.body?.cancel();
      throw new Error('synthetic lost record_offer acknowledgement after receiver commit');
    }
    return response;
  }});
  const prepClient=createOwnerMeetingPrepClient({...receiver,fetch:async(_input,_init)=>{prepCalls++;throw new Error('prep worker must not run');}});
  const offerTool=createOwnerMeetingPrepOfferTool({client:offerClient,journal:()=>journal,sourceAuthority:source});
  const ownerPolicy={channel:'photon' as const,accountId:receiver.identity.account_id,ownerId:receiver.identity.owner_id};
  const context={threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'};
  const create=()=>new ConversationCoordinator({journal,ownerPolicy,ownerMeetingPrep:prepClient,ownerMeetingPrepOffer:offerClient,
    routeDigestForThread:()=> 'b'.repeat(64),
    resolvePrepContinuation:async request=>({messageId:sent[0]!.id,sentAtMs:sent[0]!.at}),
    model:{generate:async request=>{
      modelCalls++;
      const values:Record<string,unknown>={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,
        allowOwnerMeetingPrepOffer:request.allowOwnerMeetingPrepOffer,sourceRevision:'a'.repeat(64)};
      const execute=offerTool.owner_meeting_prep_offer.execute as (input:unknown,context:unknown)=>Promise<unknown>;
      const result=await execute({operation:'propose_prep',calendar_id:'primary',event_id:'fixture-event-123',
        start:proposalStart,attendee_email:'ada@cedar.example'},
        {requestContext:{get:(key:string)=>values[key]},abortSignal:request.signal});
      return (result as {review_text:string}).review_text;
    }},
    postPrepared:async request=>{const item={text:request.text,id:`repair-reply-${sent.length+1}`,at:Date.now()};sent.push(item);
      return {acceptance:'accepted',messageId:item.id,providerSentAtMs:item.at};}});
  try{
    coordinator=create();
    const initial=await coordinator.receive({channel:'photon',accountId:ownerPolicy.accountId,senderId:ownerPolicy.ownerId,
      isDirect:true,eventId:'repair-offer-source',text:'Could you offer preparation for Ada?',providerSentAtMs:Date.now()},context);
    assert.ok(['accepted','silent'].includes(initial.status));
    assert.equal(recordCalls,1,'first receiver commit loses only its local ACK');
    assert.equal(journal.listPendingOwnerMeetingPrepOffers().length,1);
    await coordinator.close();coordinator=undefined;journal.close();
    journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();
    coordinator=create();denyRepair=true;
    const yesAt=Date.now();
    const yes=await coordinator.receive({channel:'photon',accountId:ownerPolicy.accountId,senderId:ownerPolicy.ownerId,isDirect:true,
      eventId:'repair-yes',text:'Yes',replyBound:!unthreaded,replyToMessageId:unthreaded?undefined:sent[0]!.id,
      providerSentAtMs:yesAt},context,unthreaded?yesAt:undefined);
    assert.equal(yes.status,'accepted');
    assert.equal(modelCalls,1,'the Yes is not returned to the model');
    assert.equal(prepCalls,0,'no meeting-prep job was admitted');
    assert.equal(recordCalls,2,'one selected offer repair was attempted');
    assert.equal(sent.length,2,'exactly one honest acknowledgement follows the offer');
    assert.equal(sent[1]!.text,'I couldn’t verify the meeting prep request just now, so I haven’t started preparing it. Please ask me directly to prepare it again.');
    const ack=journal.db.prepare(`SELECT p.payload,p.purpose,d.status AS dispatch_status FROM prepared_replies p JOIN dispatches d ON d.id=p.dispatch_id WHERE p.payload=?`).all(sent[1]!.text) as Array<{payload:string;purpose:string;dispatch_status:string}>;
    assert.equal(ack.length,1,'one journaled honest acknowledgement was prepared');
    assert.equal(ack[0]!.purpose,'acknowledgement_unavailable');
    assert.equal(ack[0]!.dispatch_status,'accepted');
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_meeting_prep_request'").get()!.n,0);
    assert.equal(journal.listPendingOwnerMeetingPrepOffers().length,1,'the uncertain activation remains held without auto-replay');
    assert.equal((await coordinator.receive({channel:'photon',accountId:ownerPolicy.accountId,senderId:ownerPolicy.ownerId,isDirect:true,
      eventId:'repair-yes',text:'Yes',replyBound:!unthreaded,replyToMessageId:unthreaded?undefined:sent[0]!.id,
      providerSentAtMs:yesAt},context,unthreaded?yesAt:undefined)).status,'duplicate');
    assert.equal(sent.length,2,'replay does not resend the acknowledgement');
  }finally{await coordinator?.close();journal.close();await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
