import {PREP_OFFER_MAX_AGE_MS} from '../src/channels/policy.js';
import {precedingPrepMessage,prepContinuationWindowStart} from '../src/channels/photon-continuation.js';
import {READ_CONTEXT_WINDOW_MS} from '../src/tools/read-admission.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync, rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createOwnerOfferReplyClient} from '../src/tools/owner-offer-reply-client.js';
const digest = 'b'.repeat(64), stamp = 1791126000000;
const input = {channel: 'photon' as const, eventId: 'owner-no', accountId: 'account', senderId: 'owner', isDirect: true, text: 'No.'};
const context = {threadId: 'thread', destination: 'owner', lineKey: 'line'};
function setup() {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-continuation-'));
  const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
  return {journal, folder, close: () => {journal.close(); rmSync(folder, {recursive: true, force: true});}};
}
function accepted(journal: OperationalJournal, id = 'offer', native = true) {
  journal.prepareDispatch({dispatchId: id, kind: 'scheduled_text', destination: 'owner', payload: 'Question?', routeDigest: digest});
  journal.beginDispatch(id);
  journal.settleDispatch(id, 1, 'accepted', `${id}-provider`, [`${id}-provider`], native ? stamp : undefined);
  journal.db.prepare('UPDATE dispatches SET created_at=100,updated_at=200 WHERE id=?').run(id);
  journal.db.prepare('UPDATE dispatch_attempts SET started_at=110,finished_at=200 WHERE dispatch_id=?').run(id);
}
function admit(journal: OperationalJournal, eventId = 'owner-no', sentAt = stamp + 1000) {
  return journal.admitInbound({eventId, accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'photon', sender: 'owner',
    payload: JSON.stringify({inbound: {...input, eventId}, context}), continuation: {routeDigest: digest, providerSentAtMs: sentAt}}).event;
}
test('native same-clock continuation freezes once without changing ordinary event payload or legacy replay', () => {
  const {journal, close} = setup();
  try {
    accepted(journal);
    const event = admit(journal);
    assert.equal(journal.continuationArgs(String(event.id))!.selected_offer!.message_id, 'offer-provider');
    assert.equal(JSON.parse(String(event.payload)).inbound.providerSentAtMs, undefined);
    const original = event.continuation_json;
    accepted(journal, 'newer');
    assert.equal(admit(journal).continuation_json, original);
    const legacy = journal.admitInbound({eventId: 'legacy', accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'photon', sender: 'owner',
      payload: JSON.stringify({inbound: {...input, eventId: 'legacy'}, context})});
    assert.equal(admit(journal, 'legacy').continuation_json, null);
    assert.equal(journal.getInbound(String(legacy.event.id))!.payload_hash, legacy.event.payload_hash);
  } finally {close();}
});
test('latest missing native time, equal provider clocks, prior owner, unknown and overlapping sends all refuse without older fallback', () => {
  for (const kind of ['missing', 'equal', 'owner', 'unknown', 'overlap', 'legacy-interactive'] as const) {
    const {journal, close} = setup();
    try {
      accepted(journal);
      if (kind === 'missing') {accepted(journal, 'newer', false); journal.db.prepare("UPDATE dispatches SET updated_at=300 WHERE id='newer'").run();}
      if (kind === 'owner') {const prior = admit(journal, 'intervening'); journal.db.prepare('UPDATE inbound_events SET received_at=200 WHERE id=?').run(String(prior.id));}
      if (kind === 'unknown') {journal.prepareDispatch({dispatchId: 'unknown', kind: 'scheduled_text', destination: 'owner', payload: 'Other', routeDigest: digest}); journal.beginDispatch('unknown'); journal.settleDispatch('unknown', 1, 'unknown');}
      if (kind === 'overlap') {accepted(journal, 'overlap'); journal.db.prepare("UPDATE dispatches SET updated_at=150 WHERE id='overlap'").run(); journal.db.prepare("UPDATE dispatch_attempts SET started_at=100,finished_at=150 WHERE dispatch_id='overlap'").run();}
      if (kind === 'legacy-interactive') {
        const event = journal.admitInbound({eventId: 'question-owner', accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'photon', sender: 'owner', payload: JSON.stringify({inbound: {...input, eventId: 'question-owner'}, context})}).event;
        journal.startRun({runId: 'run', eventId: String(event.id)});
        journal.prepareReply({replyId: 'reply', eventId: String(event.id), runId: 'run', dispatchId: 'legacy-question', kind: 'interactive', destination: 'owner', payload: 'Ordinary question?'});
        journal.beginDispatch('legacy-question'); journal.settleDispatch('legacy-question', 1, 'accepted', 'legacy-provider');
        journal.db.prepare("UPDATE inbound_events SET received_at=50 WHERE id=?").run(String(event.id));
        journal.db.prepare("UPDATE dispatches SET updated_at=300 WHERE id='legacy-question'").run();
      }
      const event = admit(journal, 'new', kind === 'equal' ? stamp : stamp + 1000);
      assert.equal(journal.continuationArgs(String(event.id)), null, kind);
    } finally {close();}
  }
});
test('ordinary-question no_match returns to model without private action; terminal replies never preview', async () => {
  for (const text of ['No.', 'Done.', 'Drop it.', '2']) {
    const {journal, close} = setup(); let previews = 0, models = 0;
    const client = createOwnerOfferReplyClient({identity: {tenant_id: 'tenant', channel: 'photon', account_id: 'account', owner_id: 'owner', line_key: 'line', target: 'photon', route_mode: 'fixed', route_id: 'thread', project_fingerprint: 'a'.repeat(64)}, bearer: 'c'.repeat(64), bridgeUrl: 'http://127.0.0.1:1/mcp', fetch: async (_url, init) => {
      assert.equal(JSON.parse(String(init!.body)).operation, 'preview'); previews++; return Response.json({version: 1, status: 'no_match'});
    }});
    const coordinator = new ConversationCoordinator({journal, ownerPolicy: {channel: 'photon', accountId: 'account', ownerId: 'owner'}, routeDigestForThread: () => digest, ownerOfferReply: client,
      model: {generate: async () => {models++; return 'Ordinary answer';}}, postPrepared: async () => ({acceptance: 'accepted', messageId: 'answer'})});
    try {
      accepted(journal);
      await coordinator.receive({...input, text}, context, stamp + 1000);
      assert.equal(previews, text === 'No.' ? 1 : 0); assert.equal(models, 1);
      assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 0);
    } finally {await coordinator.close(); close();}
  }
});

test('delayed preview cannot begin effects after close or a newer owner correction', async () => {
  for (const stop of ['close', 'correction'] as const) {
    const {journal, close} = setup(); let resolvePreview!: (response: Response) => void, previews = 0, applies = 0;
    const client = createOwnerOfferReplyClient({identity: {tenant_id: 'tenant', channel: 'photon', account_id: 'account', owner_id: 'owner', line_key: 'line', target: 'photon', route_mode: 'fixed', route_id: 'thread', project_fingerprint: 'a'.repeat(64)}, bearer: 'c'.repeat(64), bridgeUrl: 'http://127.0.0.1:1/mcp', fetch: async (_url, init) => {
      if (JSON.parse(String(init!.body)).operation !== 'preview') {applies++; throw new Error('Unexpected mutation');}
      previews++; return new Promise<Response>(done => {resolvePreview = done;});
    }});
    const coordinator = new ConversationCoordinator({journal, ownerPolicy: {channel: 'photon', accountId: 'account', ownerId: 'owner'}, routeDigestForThread: () => digest, ownerOfferReply: client,
      model: {generate: async () => 'Safe correction'}, postPrepared: async () => ({acceptance: 'accepted', messageId: 'answer'})});
    try {
      accepted(journal);
      const work = coordinator.receive(input, context, stamp + 1000);
      while (!resolvePreview) await new Promise(done => setTimeout(done, 1));
      const follow = stop === 'close' ? coordinator.close() : coordinator.receive({...input, eventId: 'correction', text: 'Actually, hello'}, context);
      resolvePreview(Response.json({version: 1, status: 'match', offer_id: 'e'.repeat(32), kind: 'chase'}));
      await work; await follow;
      assert.equal(applies, 0); assert.equal(previews, 1);
      assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 0);
    } finally {await coordinator.close(); close();}
  }
});

test('v6 additive upgrade preserves legacy admission hashes and accepted sends without retroactive native proof', () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-v6-upgrade-')), path = join(folder, 'journal.sqlite');
  let journal = new OperationalJournal(path);
  try {
    const event = journal.admitInbound({eventId: input.eventId, accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'photon', sender: 'owner', payload: JSON.stringify({inbound: input, context})}).event;
    accepted(journal, 'legacy', false);
    journal.db.exec('ALTER TABLE inbound_events DROP COLUMN continuation_json; ALTER TABLE dispatches DROP COLUMN provider_sent_at_ms; PRAGMA user_version=6');
    journal.close(); journal = new OperationalJournal(path);
    assert.equal(journal.db.prepare('PRAGMA user_version').get()!.user_version, 7);
    assert.equal(admit(journal).payload_hash, event.payload_hash);
    assert.equal(journal.getInbound(String(event.id))!.continuation_json, null);
    journal.settleDispatch('legacy', 1, 'accepted', 'legacy-provider', ['legacy-provider'], stamp);
    assert.equal(journal.getDispatch('legacy')!.provider_sent_at_ms, null);
    accepted(journal, 'fresh');
    assert.equal(journal.getDispatch('fresh')!.provider_sent_at_ms, stamp);
  } finally {journal.close(); rmSync(folder, {recursive: true, force: true});}
});

test('unavailable preview clarifies through existing reply journal and safe chat resumes without private action', async () => {
  for (const unknownReply of [false, true]) {
  const {journal, close} = setup(); let models = 0; const modelText: string[] = [], feedback: string[] = [];
  const client = createOwnerOfferReplyClient({identity: {tenant_id: 'tenant', channel: 'photon', account_id: 'account', owner_id: 'owner', line_key: 'line', target: 'photon', route_mode: 'fixed', route_id: 'thread', project_fingerprint: 'a'.repeat(64)}, bearer: 'c'.repeat(64), bridgeUrl: 'http://127.0.0.1:1/mcp', fetch: async () => {throw new Error('Synthetic unavailable preview');}});
  const coordinator = new ConversationCoordinator({journal, ownerPolicy: {channel: 'photon', accountId: 'account', ownerId: 'owner'}, routeDigestForThread: () => digest, ownerOfferReply: client,
    onOwnerReplyAccepted: message => {feedback.push(message.eventId);},
    model: {generate: async request => {models++; modelText.push(request.text); return 'Safe hello';}}, postPrepared: async request => request.text.startsWith('I couldn’t') && unknownReply ? {acceptance: 'unknown'} : {acceptance: 'accepted', messageId: 'safe-reply'}});
  try {
    accepted(journal);
    assert.equal((await coordinator.receive(input, context, stamp + 1000)).status, unknownReply ? 'held' : 'accepted');
    assert.equal(models, 0); assert.equal(journal.listPendingInbound().length, unknownReply ? 1 : 0);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 0);
    assert.deepEqual(feedback, [], 'an unconfirmed offer match gives no completion feedback');
    await coordinator.receive({...input, eventId: 'safe-hello', text: 'Hello'}, context);
    assert.equal(models, 1); assert.deepEqual(modelText, ['Hello']); assert.deepEqual(feedback, ['safe-hello']);
    assert.equal(journal.listPendingInbound().length, unknownReply ? 1 : 0);
    if (unknownReply) assert.equal(journal.listPendingInbound()[0].source_id, input.eventId);
  } finally {await coordinator.close(); close();}
  }
});

test('clarification preflight is cancelled on shutdown before provider contact', async () => {
  const {journal, close} = setup(); let allow!: (value: boolean) => void, sends = 0;
  const client = createOwnerOfferReplyClient({identity: {tenant_id: 'tenant', channel: 'photon', account_id: 'account', owner_id: 'owner', line_key: 'line', target: 'photon', route_mode: 'fixed', route_id: 'thread', project_fingerprint: 'a'.repeat(64)}, bearer: 'c'.repeat(64), bridgeUrl: 'http://127.0.0.1:1/mcp', fetch: async () => {throw new Error('Synthetic unavailable preview');}});
  const coordinator = new ConversationCoordinator({journal, ownerPolicy: {channel: 'photon', accountId: 'account', ownerId: 'owner'}, routeDigestForThread: () => digest, ownerOfferReply: client,
    model: {generate: async () => 'Unused'}, canPostPrepared: async () => new Promise<boolean>(done => {allow = done;}),
    postPrepared: async () => {sends++; return {acceptance: 'accepted', messageId: 'unused'};}});
  try {
    accepted(journal);
    const work = coordinator.receive(input, context, stamp + 1000);
    while (!allow) await new Promise(done => setTimeout(done, 1));
    const closing = coordinator.close(); allow(true); await work; await closing;
    assert.equal(sends, 0); assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 0);
  } finally {await coordinator.close(); close();}
});

test('provider continuation rescue remains exact, fresh, route-bound and leaves unknown dispatches untouched', () => {
  for (const invalid of ['', 'route', 'timestamp', 'expired', 'unaccepted', 'newer-input', 'started-run', 'legacy'] as const) {
    const {journal, close} = setup();
    try {
      accepted(journal);
      journal.prepareDispatch({dispatchId: 'old-unknown', kind: 'scheduled_text', destination: 'owner', payload: 'Old', routeDigest: digest});
      journal.beginDispatch('old-unknown'); journal.settleDispatch('old-unknown', 1, 'unknown');
      const event = invalid === 'legacy' ? journal.admitInbound({eventId: 'owner-no', accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'photon', sender: 'owner', payload: JSON.stringify({inbound: input, context})}).event : admit(journal);
      assert.equal(journal.continuationArgs(String(event.id)), null);
      if (invalid === 'newer-input') admit(journal, 'correction');
      if (invalid === 'started-run') journal.startRun({runId: 'begun', eventId: String(event.id)});
      journal.freezeProviderContinuation(String(event.id), invalid === 'route' ? 'c'.repeat(64) : digest, stamp + 1000,
        {messageId: invalid === 'unaccepted' ? 'old-unknown' : 'offer-provider', sentAtMs: invalid === 'timestamp' ? stamp + 1000 : invalid === 'expired' ? stamp - PREP_OFFER_MAX_AGE_MS - 1 : stamp});
      assert.equal(journal.continuationArgs(String(event.id))?.selected_offer?.message_id ?? null, invalid ? null : 'offer-provider', invalid);
      assert.equal(journal.getDispatch('old-unknown')!.status, 'unknown');
    } finally {close();}
  }
});

test('provider rescue is not requested for recovered legacy Yes or negative/mixed text', async () => {
  for (const text of ['Yes', 'no', 'not now', 'yes but cancel it']) {
    const {journal, close} = setup(); let reads = 0;
    accepted(journal);
    const {createOwnerMeetingPrepClient} = await import('../src/tools/owner-meeting-prep-client.js');
    const prep = createOwnerMeetingPrepClient({identity: {tenant_id:'tenant', channel:'photon',account_id:'account',owner_id:'owner',line_key:'line',target:'photon',route_mode:'fixed',route_id:'thread',project_fingerprint:'a'.repeat(64)}, bearer:'c'.repeat(64), bridgeUrl:'http://127.0.0.1:1/mcp', fetch:async()=>{throw new Error('No preview allowed');}});
    const coordinator = new ConversationCoordinator({journal, ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerMeetingPrep:prep,routeDigestForThread:()=>digest,
      resolvePrepContinuation:async()=>{reads++;return null;}, model:{generate:async()=> 'Clarify safely'},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});
    try {
      if (text === 'Yes') {
        journal.admitInbound({eventId:'legacy-yes',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:{...input,text,eventId:'legacy-yes'},context})});
        await coordinator.recover();
      } else await coordinator.receive({...input,text},context,stamp+1000);
      assert.equal(reads,0,text);
      assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
    } finally {await coordinator.close();close();}
  }
});

test('shutdown or a newer owner correction cancels an in-flight provider proof', async () => {
  const {createOwnerMeetingPrepClient} = await import('../src/tools/owner-meeting-prep-client.js');
  for (const stop of ['close', 'correction']) {
    const {journal, close} = setup(); let done!: (value: {messageId:string;sentAtMs:number}) => void, previews = 0;
    const prep = createOwnerMeetingPrepClient({identity:{tenant_id:'tenant',channel:'photon',account_id:'account',owner_id:'owner',line_key:'line',target:'photon',route_mode:'fixed',route_id:'thread',project_fingerprint:'a'.repeat(64)},bearer:'c'.repeat(64),bridgeUrl:'http://127.0.0.1:1/mcp',fetch:async()=>{previews++;throw new Error('No late preview');}});
    const coordinator = new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerMeetingPrep:prep,routeDigestForThread:()=>digest,
      resolvePrepContinuation:async request=>{assert.equal(request.candidateAtMs,stamp);return new Promise(resolve=>{done=resolve;});},model:{generate:async()=> 'Safe correction'},postPrepared:async()=>({acceptance:'accepted',messageId:'answer'})});
    try {
      accepted(journal);
      journal.db.prepare("UPDATE dispatches SET updated_at=? WHERE id='offer'").run(Date.now()-1000);
      journal.prepareDispatch({dispatchId:'uncertain',kind:'scheduled_text',destination:'owner',payload:'Unknown',routeDigest:digest});
      journal.beginDispatch('uncertain');journal.settleDispatch('uncertain',1,'unknown');
      const work = coordinator.receive({...input,text:'Yes'},context,stamp+1000);
      while (!done) await new Promise(resolve=>setTimeout(resolve,1));
      const follow = stop === 'close' ? coordinator.close() : coordinator.receive({...input,eventId:'correction',text:'Actually never mind'},context);
      done({messageId:'offer-provider',sentAtMs:stamp});await work;await follow;
      assert.equal(previews,0);assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
      assert.equal(journal.getDispatch('uncertain')!.status,'unknown');
    } finally {await coordinator.close();close();}
  }
});

test('complete provider proof recovers delayed prep without settling historical unknowns or extending recall', t => {
  const at=stamp+61*60_000;
  t.mock.method(Date,'now',()=>at);
  const {journal,close}=setup();
  try {
    journal.prepareDispatch({dispatchId:'historical-unknown',kind:'scheduled_text',destination:'owner',payload:'Uncertain',routeDigest:digest});
    journal.beginDispatch('historical-unknown');journal.settleDispatch('historical-unknown',1,'unknown');
    journal.db.prepare('UPDATE dispatches SET created_at=?,updated_at=? WHERE id=?').run(stamp-2*86400000,stamp-2*86400000,'historical-unknown');
    journal.db.prepare('UPDATE dispatch_attempts SET started_at=?,finished_at=? WHERE dispatch_id=?').run(stamp-2*86400000,stamp-2*86400000,'historical-unknown');
    journal.prepareDispatch({dispatchId:'fresh-prep',kind:'scheduled_text',destination:'owner',payload:JSON.stringify({text:'Want full prep?'}),routeDigest:digest,sourceRevision:'a'.repeat(64)});
    journal.beginDispatch('fresh-prep');journal.settleDispatch('fresh-prep',1,'accepted','prep-provider',['prep-provider'],stamp);
    journal.db.prepare('UPDATE dispatches SET created_at=?,updated_at=? WHERE id=?').run(stamp-1,stamp,'fresh-prep');
    journal.db.prepare('UPDATE dispatch_attempts SET started_at=?,finished_at=? WHERE dispatch_id=?').run(stamp-1,stamp,'fresh-prep');
    const unknownBefore={...journal.getDispatch('historical-unknown')!};
    const attemptsBefore=journal.db.prepare('SELECT * FROM dispatch_attempts WHERE dispatch_id=?').all('historical-unknown');
    const inbound={...input,eventId:'delayed-yes',text:'Yes'};
    const event=journal.admitInbound({eventId:inbound.eventId,accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
      payload:JSON.stringify({inbound,context}),continuation:{routeDigest:digest,providerSentAtMs:at}}).event;
    assert.equal(journal.continuationArgs(String(event.id)),null,'uncertain local ordering still refuses');
    assert.equal(journal.hasPrepContinuationCandidate(String(event.id),digest),true);
    assert.deepEqual(journal.prepContinuationCandidate(String(event.id),digest),{providerSentAtMs:stamp});
    const proof=precedingPrepMessage({messages:[
      {guid:'prep-provider',isFromMe:true,dateCreated:new Date(stamp)},
      {guid:'delayed-yes',isFromMe:false,dateCreated:new Date(at),sender:{address:'owner'}}]},
      {inboundId:'delayed-yes',inboundAtMs:at},'owner');
    assert.ok(proof);
    journal.freezeProviderContinuation(String(event.id),digest,at,proof);
    assert.equal(journal.continuationArgs(String(event.id))?.selected_offer?.message_id,'prep-provider');
    assert.deepEqual({...journal.getDispatch('historical-unknown')!},unknownBefore);
    assert.deepEqual(journal.db.prepare('SELECT * FROM dispatch_attempts WHERE dispatch_id=?').all('historical-unknown'),attemptsBefore);
    assert.equal(READ_CONTEXT_WINDOW_MS,15*60_000);
    assert.equal(journal.acceptedBackgroundMessage(String(event.id),stamp-2),undefined,'effect selector does not restore old model context');
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0,'selector alone grants no action');
  } finally {close();}
});

test('provider rescue age boundary and the selected dispatch own uncertainty remain fail closed', t => {
  const at=stamp+PREP_OFFER_MAX_AGE_MS+1;
  t.mock.method(Date,'now',()=>at);
  for(const mode of ['fresh-edge','boundary','expired','own-unknown'] as const){
    const {journal,close}=setup();
    try {
      const age=mode==='fresh-edge'?PREP_OFFER_MAX_AGE_MS-1:mode==='boundary'?PREP_OFFER_MAX_AGE_MS:mode==='expired'?PREP_OFFER_MAX_AGE_MS+1:61*60_000;
      const sent=at-age;
      accepted(journal);
      journal.db.prepare("UPDATE dispatches SET created_at=?,updated_at=?,provider_sent_at_ms=? WHERE id='offer'").run(sent-1,sent,sent);
      journal.db.prepare("UPDATE dispatch_attempts SET started_at=?,finished_at=? WHERE dispatch_id='offer'").run(sent-1,sent);
      journal.prepareDispatch({dispatchId:'old-unknown',kind:'scheduled_text',destination:'owner',payload:'Uncertain',routeDigest:digest});
      journal.beginDispatch('old-unknown');journal.settleDispatch('old-unknown',1,'unknown');
      if(mode==='own-unknown')journal.db.prepare("INSERT INTO dispatch_attempts(dispatch_id,number,status,started_at) VALUES('offer',2,'unknown',?)").run(sent-1);
      const event=admit(journal,'fresh-yes',at);
      assert.equal(journal.hasPrepContinuationCandidate(String(event.id),digest),age<PREP_OFFER_MAX_AGE_MS,mode);
      journal.freezeProviderContinuation(String(event.id),digest,at,{messageId:'offer-provider',sentAtMs:sent});
      assert.equal(Boolean(journal.continuationArgs(String(event.id))),mode==='fresh-edge',mode);
      assert.equal(journal.getDispatch('old-unknown')!.status,'unknown');
    } finally {close();}
  }
});


test('candidate without native time retains the original 15-minute provider fallback', t => {
  const at=stamp+5*60_000;
  t.mock.method(Date,'now',()=>at);
  const {journal,close}=setup();
  try {
    accepted(journal,'offer',false);
    journal.db.prepare("UPDATE dispatches SET updated_at=? WHERE id='offer'").run(stamp);
    const event=admit(journal,'legacy-native-missing',at);
    assert.equal(journal.continuationArgs(String(event.id)),null);
    const candidate=journal.prepContinuationCandidate(String(event.id),digest);
    assert.deepEqual(candidate,{});
    assert.equal(prepContinuationWindowStart(at,candidate!.providerSentAtMs),at-15*60_000);
  } finally {close();}
});
