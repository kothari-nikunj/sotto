import {ManagedProxyUpstreamUnavailable} from '../src/agent/models.js';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import test from 'node:test';
import {mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {isOwnerFollowupRequest, isOwnerRelationshipPulseRequest, isOwnerUrgentCheck, isOwnerMeetingPrepRequest, createOwnerSkillRequestClient, ownerSkillRequestKind, skillRejectionAcknowledgement} from '../src/tools/owner-skill-request-client.js';
import {createOwnerSkillExecution} from '../src/tools/owner-skill-execution.js';
import {SHARED_SKILL_EXECUTION_GUIDE, createSkillCommand, parseSkillCommand} from '../src/tools/skill-command.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';

const identity = {tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner', line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64)};
const sourceAuthority = {read: async () => ({version: 2 as const, complete: true, revision: 'a'.repeat(64), bridge_sources: [], google_sources: ['calendar' as const]})};
const inbound = (eventId: string, text: string) => ({eventId, text, channel: 'photon' as const, accountId: 'account', senderId: 'owner', isDirect: true});
const context = {threadId: 'thread', destination: 'photon', lineKey: 'line'};
const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: 'owner'};

test('rejected shared work gives a plain reason without claiming a loop change', () => {
  assert.match(skillRejectionAcknowledgement('followup', 'needs_context'), /list what is currently open/);
  assert.match(skillRejectionAcknowledgement('followup', 'needs_context'), /cannot confirm that any loops were closed/);
  assert.match(skillRejectionAcknowledgement('followup', 'source_unavailable'), /could not check the sources/);
  assert.match(skillRejectionAcknowledgement('followup', 'meeting_notes_not_connected'), /meeting notes aren.t connected/);
  assert.doesNotMatch(skillRejectionAcknowledgement('followup', 'meeting_notes_not_connected'), /try again when/);
  assert.match(skillRejectionAcknowledgement('ask-research', 'event_conflict'), /saved request/);
  assert.match(skillRejectionAcknowledgement('meeting-prep', 'unavailable'), /Nothing was queued/);
  for (const code of ['needs_context', 'source_unavailable', 'meeting_notes_not_connected', 'event_conflict', 'unavailable'] as const)
    assert.doesNotMatch(skillRejectionAcknowledgement('followup', code), /skill request|exact owner selection|source or/);
});

test('a mistaken follow-up attempt on a mixed loop correction queues nothing and leaves the reply to the current request', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-mixed-loop-rejection-'));
  const journal = new OperationalJournal(join(directory, 'journal.sqlite'));
  let contacts = 0;
  const client = createOwnerSkillRequestClient({identity, bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), fetch: async () => {
    contacts++;
    return new Response(JSON.stringify({version: 1, status: 'rejected', code: 'needs_context'}), {status: 409});
  }});
  const command = createSkillCommand(createOwnerSkillExecution({client, journal: () => journal, sourceAuthority}));
  const sent: string[] = [];
  const coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerSkillRequest: client,
    model: {generate: async request => {
      const result = await command({id: 'followup', args: []}, {mode: 'chat', runId: request.runId,
        ownerRequestKey: request.ownerRequestKey, allowOwnerSkill: request.allowOwnerSkill,
        sourceRevision: 'a'.repeat(64), signal: request.signal});
      assert.equal(result.result.status, 'rejected');
      return 'Current loop request continues normally';
    }}, postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: 'rejected-followup'};}});
  try {
    assert.equal((await coordinator.receive(inbound('mixed-loop', 'Dana, Morgan, and Ravi loops are closed. What else is open?'), context)).status, 'accepted');
    assert.equal(contacts, 0);
    assert.equal(sent.length, 1);
    // The local admission gate refuses before the receiver; no fixed acknowledgement replaces the reply.
    assert.equal(sent[0], 'Current loop request continues normally');
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_skill_request' AND status='rejected'").get()!.n, 0);
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind!='owner_skill_request'").get()!.n, 0);
  } finally {await coordinator.close(); journal.close(); rmSync(directory, {recursive: true, force: true});}
});

test('meeting-prep command maps only complete sweep/focus and rejects subprocess paths or malformed selectors', () => {
  assert.deepEqual(parseSkillCommand(['--person', 'Antim'], 'ask-research'), {skill: 'ask-research', selection: {mode: 'person', person: 'Antim'}});
  assert.deepEqual(parseSkillCommand(['--company', 'Cedar Labs'], 'ask-research'), {skill: 'ask-research', selection: {mode: 'company', company: 'Cedar Labs'}});
  assert.doesNotThrow(() => parseSkillCommand(['--company', '🗓'.repeat(200)], 'ask-research'));
  for (const value of ['🗓'.repeat(201), ' Cedar', 'Cedar\u0085Labs', 'Cedar\u2028Labs', '\ud800']) assert.throws(() => parseSkillCommand(['--company', value], 'ask-research'));
  assert.throws(() => parseSkillCommand(['--company', 'Cedar', '--person', 'Dana'], 'ask-research'));
  for (const args of [[], ['--person'], ['--person', ' Antim'], ['--focus', 'Antim'], ['--person', 'Antim', '--out', '/tmp/x']]) assert.throws(() => parseSkillCommand(args, 'ask-research'));
  assert.deepEqual(parseSkillCommand([], 'proactive'), {skill: 'proactive', selection: {mode: 'current'}});
  for (const args of [['--destination', 'other'], ['--override'], ['--out', '/tmp/x']]) assert.throws(() => parseSkillCommand(args, 'proactive'));
  assert.deepEqual(parseSkillCommand([], 'relationship-pulse'), {skill: 'relationship-pulse', selection: {mode: 'current'}});
  for (const args of [['--person', 'Dana'], ['--backlog'], ['--out', '/tmp/pulse']]) assert.throws(() => parseSkillCommand(args, 'relationship-pulse'));
  assert.deepEqual(parseSkillCommand([]), {skill: 'meeting-prep', selection: {mode: 'sweep'}});
  assert.deepEqual(parseSkillCommand(['--focus', 'Spencer Kim']), {skill: 'meeting-prep', selection: {mode: 'focus', focus: 'Spencer Kim'}});
  const meeting = {event_id:'exact-calendar-id',start:'2026-10-28T15:00:00+00:00'};
  assert.deepEqual(parseSkillCommand(['--focus','Cedar review'],'meeting-prep',meeting),
    {skill:'meeting-prep',selection:{mode:'focus',focus:'Cedar review',meeting}});
  const scopedMeeting={...meeting,calendar_id:'primary'};
  assert.deepEqual(parseSkillCommand(['--focus','Cedar review'],'meeting-prep',scopedMeeting),
    {skill:'meeting-prep',selection:{mode:'focus',focus:'Cedar review',meeting:scopedMeeting}});
  for(const calendar_id of ['', ' invalid', 'bad calendar', '\ud800'])assert.throws(()=>
    parseSkillCommand(['--focus','Cedar review'],'meeting-prep',{...meeting,calendar_id}));
  assert.throws(()=>parseSkillCommand([],'meeting-prep',meeting));
  assert.throws(()=>parseSkillCommand(['--person','Dana'],'ask-research',meeting));
  assert.throws(()=>parseSkillCommand(['--focus','Cedar review'],'meeting-prep',{...meeting,start:'tomorrow'}));
  for (const args of [['--focus'], ['--focus', ' Spencer'], ['--focus', 'line\nline'], ['--event-id', 'forged'], ['--focus', 'x', '--out', '/tmp/x']]) assert.throws(() => parseSkillCommand(args));
});

test('a narrow company blurb does not admit broad or focused meeting prep', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-prep-intent-'));
  const journal = new OperationalJournal(join(directory, 'journal.sqlite'));
  const authority = {runId: 'run', accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon' as const, scopeHash: 'c'.repeat(64)};
  try {
    const text = 'Can you write me a blurb for Cedar Labs for our IC tomorrow? What they do etc. short 3-4 sentences';
    const event = journal.admitInbound({eventId: 'source', accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'photon', sender: 'owner', payload: JSON.stringify({inbound: inbound('source', text), context})}).event.id as string;
    journal.startRun({runId: 'run', eventId: event});
    assert.throws(() => journal.prepareOwnerSkillRequestAction(authority, event, parseSkillCommand([])), /meeting prep request/);
    assert.throws(() => journal.prepareOwnerSkillRequestAction(authority, event, parseSkillCommand(['--focus', 'Cedar Labs'])), /meeting prep request/);
    assert.throws(() => journal.prepareOwnerSkillRequestAction(authority, event, parseSkillCommand(
      ['--focus','Cedar Labs'],'meeting-prep',{event_id:'calendar-source-id',start:'2026-10-28T15:00:00Z'})), /meeting prep request/);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 0);
    assert.doesNotThrow(() => journal.prepareOwnerSkillRequestAction(authority, event, parseSkillCommand(['--company', 'Cedar Labs'], 'ask-research')));
    const copyText = 'Prepare a short blurb for Birch Labs for tomorrow’s meeting';
    const copyEvent = journal.admitInbound({eventId: 'copy-source', accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'photon', sender: 'owner', payload: JSON.stringify({inbound: inbound('copy-source', copyText), context})}).event.id as string;
    journal.startRun({runId: 'copy-run', eventId: copyEvent});
    assert.throws(() => journal.prepareOwnerSkillRequestAction({...authority, runId: 'copy-run'}, copyEvent,
      parseSkillCommand(['--focus', 'Birch Labs'])), /meeting prep request/);
    assert.equal(journal.db.prepare("SELECT count(*) AS n FROM actions WHERE source_event_id=?").get(copyEvent)!.n, 0);
  } finally { journal.close(); rmSync(directory, {recursive: true, force: true}); }
});

test('explicit meeting prep remains available for a sweep or one meeting', () => {
  assert.equal(isOwnerMeetingPrepRequest('Prep my meetings', 'sweep'), true);
  assert.equal(isOwnerMeetingPrepRequest('Who am I meeting tomorrow?', 'sweep'), true);
  assert.equal(isOwnerMeetingPrepRequest('Meeting prep for tomorrow', 'sweep'), true);
  for (const text of ['Full prep on Akanksha', 'Give me full prep on Akanksha', 'Prep for Akanksha Jagwani']) {
    assert.equal(isOwnerMeetingPrepRequest(text, 'focus'), true, text);
    assert.equal(isOwnerMeetingPrepRequest(text, 'sweep'), false, text);
  }
  assert.equal(isOwnerMeetingPrepRequest('Prep Spencer Kim', 'focus'), true);
  for (const text of ["prep went well", "prep is done, thanks", "prep for Dana was great, thanks", "Prep looked really good", "the prep was helpful"])
    assert.equal(isOwnerMeetingPrepRequest(text, 'focus'), false, text);
  for (const text of ["prep me for my 3pm", "prep for Dana", "Prep Spencer Kim", "prep for the call Dana was asking about"])
    assert.equal(isOwnerMeetingPrepRequest(text, 'focus'), true, text);
  assert.equal(isOwnerMeetingPrepRequest('Prep me for my 2:15', 'focus'), true);
  assert.equal(isOwnerMeetingPrepRequest('Give me meeting prep for Dana', 'focus'), true);
  assert.equal(isOwnerMeetingPrepRequest('Can you prepare me for my meeting with Dana?', 'focus'), true);
  assert.equal(isOwnerMeetingPrepRequest('Prep Spencer Kim', 'sweep'), false);
  assert.equal(isOwnerMeetingPrepRequest('Prepare a short blurb for Cedar Labs for my IC tomorrow', 'focus'), false);
  assert.equal(isOwnerMeetingPrepRequest('Prep a 3-4 sentence summary of Cedar Labs', 'focus'), false);
});

test('ordinary sweep phrasings, including curly apostrophes, still admit meeting prep', () => {
  for (const text of ["Prepare me for today's meetings", 'Prep tomorrow’s meetings', 'meeting prep', 'Meeting prep please.',
    'Prep me for this week’s meetings', 'Prep all my calls today', 'Prep for today', 'Get me ready for tomorrow',
    'Who’s in my meetings today?', 'Prep me for my upcoming meetings'])
    assert.equal(isOwnerMeetingPrepRequest(text, 'sweep'), true, text);
  for (const text of ['Help me get ready for my call with Dana', 'What should I know before my meeting with Dana?'])
    assert.equal(isOwnerMeetingPrepRequest(text, 'focus'), true, text);
  // A named person never widens to the whole calendar, and copy stays copy.
  for (const text of ['Give me meeting prep for Dana', 'Prep me for my meeting with Dana', 'meeting prep blurb for Cedar Labs',
    'Prep tomorrow’s meetings in 3 sentences'])
    assert.equal(isOwnerMeetingPrepRequest(text, 'sweep'), false, text);
});

for (const skill of ['meeting-prep', 'ask-research', 'ask-company', 'followup', 'relationship-pulse', 'proactive'] as const) test(`shared ${skill} durable receipt ACK, lost response inspection and unrelated safe chat use one action`, async () => {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-skill-'));
  let journal = new OperationalJournal(join(directory, 'journal.sqlite'));
  let lose = true, blocked = true, admissions = 0;
  const receipts = new Map<string, unknown>();
  const client = createOwnerSkillRequestClient({identity, bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), fetch: async (_url, init) => {
    const body = JSON.parse(init!.body as string);
    if (body.operation === 'inspect') {
      if (blocked) throw new Error('unavailable');
      return new Response(JSON.stringify(receipts.get(body.action_id) ?? {version: 1, status: 'not_found'}));
    }
    admissions++;
    const receipt = {version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id, payload_hash: body.payload_hash,
      args_hash: stableActionIdentity(body.event_id, ownerSkillRequestKind, body.args).payloadHash,
      outcome: {operation: 'admit', skill: body.args.skill, request_id: 'b'.repeat(32), status: 'queued'}};
    receipts.set(body.action_id, receipt);
    if (lose) throw new Error('lost after commit');
    return new Response(JSON.stringify(receipt));
  }});
  const execution = createOwnerSkillExecution({client, journal: () => journal, sourceAuthority});
  const command = createSkillCommand(execution);
  const sent: string[] = [];
  let coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerSkillRequest: client,
    model: {generate: async request => {
      await command({id: skill === 'ask-company' ? 'ask-research' : skill, args: (skill === 'relationship-pulse' || skill === 'proactive') ? [] : skill === 'followup' ? ['--backlog'] : [skill === 'ask-company' ? '--company' : skill === 'ask-research' ? '--person' : '--focus', 'Spencer Kim']}, {mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey, allowOwnerSkill: request.allowOwnerSkill, sourceRevision: 'a'.repeat(64), signal: request.signal});
      return 'invented completed prep';
    }}, postPrepared: async request => {assert.match(request.text, /I couldn’t confirm whether the skill request/);sent.push(request.text);return {acceptance:'accepted',messageId:'pending-notice'};}});
  let silentAccepted=0;
  try {
    assert.equal((await coordinator.receive(inbound('prep', skill === 'proactive' ? 'Check for anything urgent' : skill === 'relationship-pulse' ? 'quick relationship pulse?' : skill === 'followup' ? 'who do I owe a reply?' : 'Prep Spencer Kim'), context)).status, 'held');
    const saved = journal.db.prepare('SELECT * FROM actions WHERE kind=?').get(ownerSkillRequestKind)!;
    assert.equal(saved.status, 'unknown');
    await coordinator.close(); journal.close(); journal = new OperationalJournal(join(directory, 'journal.sqlite')); journal.recoverInterrupted();
    coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerSkillRequest: client, model: {generate: async request => {assert.equal(request.text, 'Hello'); return 'safe reply';}},
      onOwnerSilentWorkAccepted:()=>{silentAccepted++;}, postPrepared: async request => {sent.push(request.text); return {acceptance: 'accepted', messageId: `ack-${sent.length}`};}});
    assert.equal((await coordinator.recover())[0]?.status, 'held');
    assert.equal((await coordinator.receive(inbound('safe', 'Hello'), context)).status, 'accepted');
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_skill_request' AND status='unknown'").run();
    blocked = false; lose = false;
    assert.equal((await coordinator.recover())[0]?.status, skill === 'proactive' ? 'silent' : 'accepted');
    assert.equal(admissions, 1);assert.equal(silentAccepted,0,'recovery must not fabricate fresh completion');
    assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether')).length,1);
    assert.deepEqual(sent.slice(1), skill === 'proactive' ? ['safe reply'] : ['safe reply', skill === 'relationship-pulse' ? 'Your relationship pulse is queued. It will arrive separately.' : skill === 'followup' ? 'Your follow-up is queued. It will arrive separately.' : (skill === 'ask-research' || skill === 'ask-company') ? 'Your research is queued. It will arrive separately.' : 'Your meeting prep is queued. It will arrive separately.']);
    assert.equal((await coordinator.receive(inbound('prep', skill === 'proactive' ? 'Check for anything urgent' : skill === 'relationship-pulse' ? 'quick relationship pulse?' : skill === 'followup' ? 'who do I owe a reply?' : 'Prep Spencer Kim'), context)).status, 'duplicate');
    assert.throws(() => journal.prepareOwnerPreferenceToolAction({runId: saved.run_id as string, accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon', scopeHash: client.scopeHash}, saved.source_event_id as string, {operation: 'clear-tone'}), /another private mutation/);
  } finally {await coordinator.close(); journal.close(); rmSync(directory, {recursive: true, force: true});}
});

test('journal binds exact focus, refuses changed args and reciprocal writes before contact', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-skill-binding-'));
  const journal = new OperationalJournal(join(directory, 'journal.sqlite'));
  const authority = {runId: 'run', accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon' as const, scopeHash: 'c'.repeat(64)};
  try {
    const event = journal.admitInbound({eventId: 'source', accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'photon', sender: 'owner', payload: JSON.stringify({inbound: inbound('source', 'Prep Spencer Kim'), context})}).event.id as string;
    journal.startRun({runId: 'run', eventId: event});
    assert.throws(() => journal.prepareOwnerSkillRequestAction(authority, event, parseSkillCommand(['--person', 'Another person'], 'ask-research')), /exact owner span/);
    assert.equal(journal.db.prepare('SELECT count(*) n FROM actions').get()!.n, 0);
    const request = journal.prepareOwnerSkillRequestAction(authority, event, parseSkillCommand(['--focus', 'Spencer Kim']));
    assert.throws(() => journal.prepareOwnerSkillRequestAction(authority, event, parseSkillCommand([])), /conflicts/);
    assert.throws(() => journal.prepareOwnerSkillRequestAction(authority, event, parseSkillCommand(['--person', 'Spencer Kim'], 'ask-research')), /conflicts/);
    assert.throws(() => journal.prepareOwnerPreferenceToolAction(authority, event, {operation: 'clear-tone'}), /another private mutation/);
    assert.deepEqual(journal.readOwnerSkillRequestAction(request.actionId, authority).request, request);
  } finally {journal.close(); rmSync(directory, {recursive: true, force: true});}
});


test('client refuses a receipt for the other skill even with matching action proof hashes', async () => {
  const args = parseSkillCommand(['--person', 'Dana'], 'ask-research');
  const eventId = 'e'.repeat(64), binding = stableActionIdentity(eventId, ownerSkillRequestKind, args);
  const client = createOwnerSkillRequestClient({identity, bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), fetch: async () =>
    new Response(JSON.stringify({version: 1, status: 'committed', action_id: binding.actionId, event_id: eventId,
      payload_hash: 'f'.repeat(64), args_hash: binding.payloadHash,
      outcome: {operation: 'admit', skill: 'meeting-prep', request_id: 'b'.repeat(32), status: 'queued'}}))});
  await assert.rejects(client.call('admit', {actionId: binding.actionId, eventId, payloadHash: 'f'.repeat(64),
    text: 'Research Dana', admittedAtMs: Date.now(), providerThreadId: 'thread', args}), /receipt is invalid/);
});

test('known disconnected meeting notes retain a typed refusal without queuing follow-up work', async () => {
  const args = parseSkillCommand([], 'followup');
  const eventId = 'e'.repeat(64), binding = stableActionIdentity(eventId, ownerSkillRequestKind, args);
  const client = createOwnerSkillRequestClient({identity, bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), fetch: async () =>
    new Response(JSON.stringify({version: 1, status: 'rejected', code: 'meeting_notes_not_connected'}), {status: 409})});
  const result = await client.call('admit', {actionId: binding.actionId, eventId, payloadHash: 'f'.repeat(64),
    text: 'What did I promise in yesterday’s meetings?', admittedAtMs: Date.now(), providerThreadId: 'thread', args});
  assert.deepEqual(result, {version: 1, status: 'rejected', code: 'meeting_notes_not_connected'});
  assert.match(skillRejectionAcknowledgement('followup', result.code), /can.t verify those promises/);
  assert.doesNotMatch(skillRejectionAcknowledgement('followup', result.code), /queued|try again/i);
});

 test('followup command has only fixed recent/backlog lanes', () => {
  assert.deepEqual(parseSkillCommand([], 'followup'), {skill: 'followup', selection: {mode: 'recent'}});
  assert.deepEqual(parseSkillCommand(['--backlog'], 'followup'), {skill: 'followup', selection: {mode: 'backlog'}});
  for (const args of [['--days', '14'], ['--backlog', '--backlog'], ['--out', '/tmp/x']])
    assert.throws(() => parseSkillCommand(args, 'followup'));
 });

test('rejected urgent admission reports unavailable rather than silently claiming an empty scan', async () => {
 const folder=mkdtempSync(join(tmpdir(),'sotto-urgent-rejected-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));const sent:string[]=[];
 const client=createOwnerSkillRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async()=>new Response(JSON.stringify({version:1,status:'rejected',code:'source_unavailable'}),{status:409})});
 const command=createSkillCommand(createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority}));
 const coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerSkillRequest:client,model:{generate:async request=>{await command({id:'proactive',args:[]},{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:'a'.repeat(64),signal:request.signal});return 'Nothing urgent.';}},postPrepared:async request=>{sent.push(request.text);return{acceptance:'accepted',messageId:'rejected'};}});
 try{assert.equal((await coordinator.receive(inbound('urgent-rejected','Check for anything urgent'),context)).status,'accepted');assert.equal(sent.length,1);assert.ok(!sent[0]!.includes('Nothing urgent'));assert.match(sent[0]!,/could not|unavailable|couldn.t/i);}finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});


test('fresh current silent admission invokes work feedback once and duplicates/reopen do not', async()=>{
 const folder=mkdtempSync(join(tmpdir(),'urgent-feedback-'));let journal=new OperationalJournal(join(folder,'journal.sqlite'));let callbacks=0,admissions=0;
 const client=createOwnerSkillRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_url,init)=>{const body=JSON.parse(String(init?.body));admissions++;return new Response(JSON.stringify({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:stableActionIdentity(body.event_id,ownerSkillRequestKind,body.args).payloadHash,outcome:{operation:'admit',skill:'proactive',request_id:'b'.repeat(32),status:'queued'}}));}});
 const command=createSkillCommand(createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority}));
 const dependencies={ownerPolicy,ownerSkillRequest:client,onOwnerSilentWorkAccepted:()=>{callbacks++;},model:{generate:async(request:import('../src/conversation.js').ModelRequest)=>{await command({id:'proactive',args:[]},{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:'a'.repeat(64),signal:request.signal});return 'No all clear';}},postPrepared:async()=>{throw Error('no textual reply');}};
 let coordinator=new ConversationCoordinator({journal,...dependencies});
 try{assert.equal((await coordinator.receive(inbound('urgent-feedback','Check for anything urgent'),context)).status,'silent');assert.equal(callbacks,1);assert.equal((await coordinator.receive(inbound('urgent-feedback','Check for anything urgent'),context)).status,'duplicate');await coordinator.close();journal.close();journal=new OperationalJournal(join(folder,'journal.sqlite'));coordinator=new ConversationCoordinator({journal,...dependencies});await coordinator.recover();assert.equal(callbacks,1);assert.equal(admissions,1);}finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

 test('company research selector binds exact current owner text at fresh and saved admission', () => {
  const directory=mkdtempSync(join(tmpdir(),'sotto-company-span-'));const journal=new OperationalJournal(join(directory,'journal.sqlite'));
  const authority={runId:'company-run',accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon' as const,scopeHash:'c'.repeat(64)};
  try {
   const event=journal.admitInbound({eventId:'company-source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:inbound('company-source','Research Cedar Labs'),context})}).event.id as string;
   journal.startRun({runId:authority.runId,eventId:event});
   assert.throws(()=>journal.prepareOwnerSkillRequestAction(authority,event,parseSkillCommand(['--company','Other Labs'],'ask-research')),/exact owner span/);
   assert.equal(journal.db.prepare('SELECT count(*) n FROM actions').get()!.n,0);
   const request=journal.prepareOwnerSkillRequestAction(authority,event,parseSkillCommand(['--company','Cedar Labs'],'ask-research'));
   assert.deepEqual(journal.readOwnerSkillRequestAction(request.actionId,authority).request.args,request.args);
   const row=journal.getInbound(event)!;const payload=JSON.parse(row.payload as string);payload.inbound.text='Research Other Labs';
   journal.db.prepare('UPDATE inbound_events SET payload=? WHERE id=?').run(JSON.stringify(payload),event);
   assert.throws(()=>journal.readOwnerSkillRequestAction(request.actionId,authority));
  }finally{journal.close();rmSync(directory,{recursive:true,force:true});}
 });


test('attended proactive requires an affirmative whole urgent-check request', () => {
 for (const text of ['Check for anything urgent',' please CHECK for anything urgent! ','Could you please check for anything urgent?','Check for anything urgent please.']) assert.equal(isOwnerUrgentCheck(text),true,text);
 for (const text of ['Hello, Sotto.','"check for anything urgent"',"Don't check for anything urgent",'Do not check for anything urgent','The note says check for anything urgent.','Check for anything urgent, but do not run it']) assert.equal(isOwnerUrgentCheck(text),false,text);
});

test('forced proactive on greeting and unrelated text refuses before action/contact then ordinary reply', async()=>{
 for(const text of ['Hello, Sotto.','"check for anything urgent"',"Don't check for anything urgent",'Tell me about an urgent meeting']) {
  const folder=mkdtempSync(join(tmpdir(),'proactive-refused-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));let contacts=0,callbacks=0;const sent:string[]=[];
  const client=createOwnerSkillRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async()=>{contacts++;throw Error('must not contact');}});
  const command=createSkillCommand(createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority}));
  const coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerSkillRequest:client,onOwnerSilentWorkAccepted:()=>{callbacks++;},model:{generate:async request=>{
   const output=await command({id:'proactive',args:[]},{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:'a'.repeat(64),signal:request.signal});
   assert.equal(output.result.status,'rejected');assert.equal('code' in output.result && output.result.code,'needs_context');return 'Ordinary reply';
  }},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'ordinary-reply'};}});
  try {assert.equal((await coordinator.receive(inbound('wrong-proactive',text),context)).status,'accepted');assert.deepEqual(sent,['Ordinary reply']);assert.equal(contacts,0);assert.equal(callbacks,0);assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);}
  finally {await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
 }
});


test('historical nonurgent proactive proof remains inspect-settleable while uncontacted pending stays held',()=>{
 const folder=mkdtempSync(join(tmpdir(),'proactive-historical-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));
 const authority={runId:'old-run',accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon' as const,scopeHash:'c'.repeat(64)};
 const hash=(text:string)=>createHash('sha256').update(text).digest('hex');
 try {
  const text='Hello, Sotto.',payload=JSON.stringify({inbound:inbound('old-source',text),context});
  const event=journal.admitInbound({eventId:'old-source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload}).event.id as string;
  journal.startRun({runId:authority.runId,eventId:event});
  const args=parseSkillCommand([],'proactive'),binding=stableActionIdentity(event,ownerSkillRequestKind,args),stamp=Date.now();
  // Exact pre-fix persisted row, not a new admission through the corrected API.
  journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)").run(binding.actionId,authority.runId,ownerSkillRequestKind,binding.payloadHash,event,hash(payload),hash(text),JSON.stringify({args,scope_hash:authority.scopeHash}),stamp,stamp);
  assert.equal(journal.readOwnerSkillRequestAction(binding.actionId,authority).status,'pending');
  assert.throws(()=>journal.prepareOwnerSkillRequestAction(authority,event,args),/affirmative urgent-check/);
  assert.equal(journal.getAction(binding.actionId)!.status,'pending');
  journal.db.prepare("UPDATE actions SET status='unknown' WHERE id=?").run(binding.actionId);
  const saved=journal.readOwnerSkillRequestAction(binding.actionId,authority);
  const receipt={version:1 as const,status:'committed' as const,action_id:binding.actionId,event_id:event,payload_hash:saved.request.payloadHash,args_hash:binding.payloadHash,outcome:{operation:'admit' as const,skill:'proactive' as const,request_id:'b'.repeat(32),status:'queued' as const}};
  journal.settleOwnerSkillRequestAction(binding.actionId,authority,receipt,true);
  assert.deepEqual(journal.readOwnerSkillRequestAction(binding.actionId,authority).receipt,receipt);
  assert.equal(journal.prepareOwnerSkillRequestAction(authority,event,args).actionId,binding.actionId);
 }finally{journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('pulse history cannot authorize another pulse on the next calendar turn', async()=>{
 const folder=mkdtempSync(join(tmpdir(),'pulse-current-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));let contacts=0;const sent:string[]=[];
 const client=createOwnerSkillRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_url,init)=>{
  contacts++;const body=JSON.parse(String(init?.body));return Response.json({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:stableActionIdentity(body.event_id,ownerSkillRequestKind,body.args).payloadHash,outcome:{operation:'admit',skill:'relationship-pulse',request_id:'b'.repeat(32),status:'queued'}});
 }});
 const command=createSkillCommand(createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority}));
 const coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerSkillRequest:client,model:{generate:async request=>{
  const output=await command({id:'relationship-pulse',args:[]},{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:'a'.repeat(64),signal:request.signal});
  if(request.text==='quick relationship pulse?')assert.equal(output.result.status,'queued');
  else {
   assert.equal(output.result.status,'rejected');assert.equal('code' in output.result&&output.result.code,'needs_context');
   assert.ok('message' in output.result);
   assert.match(output.result.message!, /not requested by the current owner message and nothing was queued/);
   assert.match(output.result.message!, /without the unrequested workflow heading, empty-result summary, or rejected-call narration/);
   assert.match(output.result.message!, /owner asks about prior workflow status or failure, explain that truthfully using available evidence/);
   assert.doesNotMatch(output.result.message!, /No relationship pulse|No saved attention items/);
  }
  return 'Current calendar request continues normally';
 }},postPrepared:async request=>{sent.push(request.text);return{acceptance:'accepted',messageId:'reply-'+sent.length};}});
 try{
  await coordinator.receive(inbound('pulse-first','quick relationship pulse?'),context);
  assert.equal(contacts,1);
  assert.equal((await coordinator.receive(inbound('calendar-next','what does tomorrow look like?'),context)).status,'accepted');
  assert.equal(contacts,1);assert.equal(sent.at(-1),'Current calendar request continues normally');
  assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions WHERE kind=?').get(ownerSkillRequestKind)!.n,1);
 }finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('current pulse intent allows shorthand but rejects quotes, negation and calendar context',()=>{
 for(const text of ['quick relationship pulse?','pulse pls','Show my relationship pulse','please run a fresh pulse', 'who am I losing touch with?', "who haven't I talked to", "who haven’t I talked to?", "who's waiting on me", 'who is waiting on me please'])assert.equal(isOwnerRelationshipPulseRequest(text),true,text);
 for(const text of ['what does tomorrow look like?', '"pulse pls"', "don't run a pulse",'she asked for a relationship pulse','pulse please but do not run it', '"who am I losing touch with"', "don't tell me who's waiting on me", 'she asked who am I losing touch with'])assert.equal(isOwnerRelationshipPulseRequest(text),false,text);
});


test('off-request pulse guidance preserves requested failure and status explanations', () => {
 assert.match(SHARED_SKILL_EXECUTION_GUIDE, /If its needs_context refusal says the attempted workflow was not requested by the current owner message/);
 assert.match(SHARED_SKILL_EXECUTION_GUIDE, /without a pulse heading, empty-result summary, or narration of the unrequested attempt/);
 assert.match(SHARED_SKILL_EXECUTION_GUIDE, /only to an off-request attempt; explain requested workflow failures or prior status truthfully using available evidence/);
 assert.match(skillRejectionAcknowledgement('relationship-pulse', 'source_unavailable'), /could not check the sources/);
});

test('follow-up history cannot authorize work on unrelated, quoted, negated or ambiguous turns', async()=>{
 const folder=mkdtempSync(join(tmpdir(),'followup-current-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));let contacts=0;const sent:string[]=[];
 const client=createOwnerSkillRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_url,init)=>{
  contacts++;const body=JSON.parse(String(init?.body));return Response.json({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:stableActionIdentity(body.event_id,ownerSkillRequestKind,body.args).payloadHash,outcome:{operation:'admit',skill:'followup',request_id:'b'.repeat(32),status:'queued'}});
 }});
 const command=createSkillCommand(createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority}));
 const coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerSkillRequest:client,model:{generate:async request=>{
  const output=await command({id:'followup',args:[]},{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:'a'.repeat(64),signal:request.signal});
  if(isOwnerFollowupRequest(request.text))assert.equal(output.result.status,'queued');
  else {
   assert.equal(output.result.status,'rejected');assert.equal('code' in output.result&&output.result.code,'needs_context');
   assert.ok('message' in output.result);
   assert.match(output.result.message!, /not requested by the current owner message and nothing was queued/);
   assert.match(output.result.message!, /without the unrequested workflow heading, empty-result summary, or rejected-call narration/);
   assert.match(output.result.message!, /owner asks about prior workflow status or failure, explain that truthfully using available evidence/);
   assert.doesNotMatch(output.result.message!, /No relationship pulse|No saved attention items/);
  }
  return 'Current calendar request continues normally';
 }},postPrepared:async request=>{sent.push(request.text);return{acceptance:'accepted',messageId:'reply-'+sent.length};}});
 try{
  await coordinator.receive(inbound('pulse-first','who do I owe a reply?'),context);
  assert.equal(contacts,1);
  await coordinator.receive(inbound('meeting-promises','what did I promise in yesterday’s meetings?'),context);
  assert.equal(contacts,2,'the current meeting question may queue the existing follow-up lane');
  for (const [index, text] of ['what does tomorrow look like?', 'yes', '"who do I owe a reply?"', "don't run follow-ups", 'she asked who do I owe a reply', 'follow up but do not run it'].entries()) {
   assert.equal((await coordinator.receive(inbound('unrelated-'+index,text),context)).status,'accepted');
   assert.equal(contacts,2);assert.equal(sent.at(-1),'Current calendar request continues normally');
  }
  assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions WHERE kind=?').get(ownerSkillRequestKind)!.n,2);
 }finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});


test('historical follow-up proof remains inspect-settleable while uncontacted pending stays held',()=>{
 const folder=mkdtempSync(join(tmpdir(),'proactive-historical-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));
 const authority={runId:'old-run',accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon' as const,scopeHash:'c'.repeat(64)};
 const hash=(text:string)=>createHash('sha256').update(text).digest('hex');
 try {
  const text='Hello, Sotto.',payload=JSON.stringify({inbound:inbound('old-source',text),context});
  const event=journal.admitInbound({eventId:'old-source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload}).event.id as string;
  journal.startRun({runId:authority.runId,eventId:event});
  const args=parseSkillCommand([],'followup'),binding=stableActionIdentity(event,ownerSkillRequestKind,args),stamp=Date.now();
  // Exact pre-fix persisted row, not a new admission through the corrected API.
  journal.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)").run(binding.actionId,authority.runId,ownerSkillRequestKind,binding.payloadHash,event,hash(payload),hash(text),JSON.stringify({args,scope_hash:authority.scopeHash}),stamp,stamp);
  assert.equal(journal.readOwnerSkillRequestAction(binding.actionId,authority).status,'pending');
  assert.throws(()=>journal.prepareOwnerSkillRequestAction(authority,event,args),/affirmative follow-up/);
  assert.equal(journal.getAction(binding.actionId)!.status,'pending');
  journal.db.prepare("UPDATE actions SET status='unknown' WHERE id=?").run(binding.actionId);
  const saved=journal.readOwnerSkillRequestAction(binding.actionId,authority);
  const receipt={version:1 as const,status:'committed' as const,action_id:binding.actionId,event_id:event,payload_hash:saved.request.payloadHash,args_hash:binding.payloadHash,outcome:{operation:'admit' as const,skill:'followup' as const,request_id:'b'.repeat(32),status:'queued' as const}};
  journal.settleOwnerSkillRequestAction(binding.actionId,authority,receipt,true);
  assert.deepEqual(journal.readOwnerSkillRequestAction(binding.actionId,authority).receipt,receipt);
  assert.equal(journal.prepareOwnerSkillRequestAction(authority,event,args).actionId,binding.actionId);
 }finally{journal.close();rmSync(folder,{recursive:true,force:true});}
});


test('affirmative natural follow-up phrasings', () => {
 assert.match(SHARED_SKILL_EXECUTION_GUIDE, /what did I promise in yesterday’s meetings\?/);
 assert.match(SHARED_SKILL_EXECUTION_GUIDE, /prior inline draft is not a meeting promise or a sent message/);
 // The four phrases after the original six are the follow-up skill's documented triggers.
 for (const text of ["who do I owe a reply?", "Who do I owe replies to?", "show my follow-ups pls", "Please run my backlog followups.", "Follow up on my meetings", "Could you list my recent follow-ups?", "what did I commit to", "what did I promise in yesterday’s meetings?", "open items from my Granola notes", "post-meeting follow-ups", "anything to send after my meetings", "any followups?", "what follow-ups do I have?", "can you please run my follow-ups", "run my follow-ups again", "run a follow-up", "follow up on today’s meetings"])
  assert.equal(isOwnerFollowupRequest(text), true, text);
 for (const text of ["follow up with Sam by email", "check my follow-up with Sam", "don't run follow-ups", "never run my follow-ups", "no follow-ups", "stop follow-ups", "did you run my follow-ups?", "should I run follow-ups?", "she asked for my follow-ups", "\"any followups?\"", "\"what did I promise in yesterday’s meetings?\"", "what did I commit to Dana", "any follow-ups from Dana?", "follow up", "follow up but do not run it", "yes"])
  assert.equal(isOwnerFollowupRequest(text), false, text);
});

// These checks prove provenance and effect ownership. Language interpretation is
// exercised by evals/conversation.ts with the actual model, not a scripted answer.
for (const mode of ['company', 'person'] as const)
for (const scenario of ['ellipse', 'ellipse-intervening', 'ellipse-remark', 'valid', 'queued', 'multi-turn', 'restart', 'old-in-session', 'revoked', 'thread', 'route', 'new', 'nightly', 'unaccepted', 'missing-proof', 'unknown-subject', 'wrong-quote', 'lost'] as const)
test(`accepted research context ${mode}: ${scenario}`, async () => {
  const folder=mkdtempSync(join(tmpdir(),'research-context-')), path=join(folder,'journal.sqlite');
  let journal=new OperationalJournal(path), revision='a'.repeat(64);
  const bodies:any[]=[];
  const followupText=scenario.startsWith('ellipse')?'Any competitors to know?':'look into that a little more';
  const subject=mode==='company'?'Cedar Labs':'Dana Reed';
  const allowed=['ellipse','valid','queued','multi-turn','restart','lost'].includes(scenario);
  const client=createOwnerSkillRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_url,init)=>{
    const body=JSON.parse(String(init?.body));
    if(body.operation==='admit') {bodies.push(body);if(scenario==='lost')throw new Error('lost response');}
    return Response.json({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,
      args_hash:stableActionIdentity(body.event_id,ownerSkillRequestKind,body.args).payloadHash,
      outcome:{operation:'admit',skill:'ask-research',request_id:'b'.repeat(32),status:'queued'}});
  }});
  const execution=createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision,bridge_sources:[],google_sources:[]})}});
  const make=()=>new ConversationCoordinator({journal,ownerPolicy,ownerSkillRequest:client,model:{generate:async request=>{
    if(scenario==='queued' && request.text!== followupText) {
      await execution.change({skill:'ask-research',selection:mode==='company'?{mode,company:subject}:{mode,person:subject}},
        {mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:true,sourceRevision:revision});
      return {text:'Queued',sourceRevision:revision};
    }
    if(request.text!==followupText)return scenario==='missing-proof'?'Saved context':{text:`${subject} builds clinic tools.`,sourceRevision:revision};
    const selected=scenario==='unknown-subject'?'Invented Subject':subject;
    const input={skill:'ask-research',selection:mode==='company'?{mode,company:selected}:{mode,person:selected}};
    const invoke=()=>execution.change(input,{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:revision});
    if(allowed) {if(scenario==='lost')await assert.rejects(invoke);else await invoke();}
    else await assert.rejects(invoke);
    return {text:'Checked',sourceRevision:revision};
  }},postPrepared:async()=>({acceptance:'accepted',messageId:'accepted-source'})});
  let coordinator=make();
  try {
    await coordinator.receive(inbound('first',scenario==='ellipse-remark'?`Had coffee with ${subject}`:`what do we know about ${subject}?`),context);
    if(scenario==='ellipse-intervening')await coordinator.receive(inbound('edit','one sentence please'),context);
    if(scenario==='multi-turn')for(let i=0;i<4;i++)await coordinator.receive(inbound('edit-'+i,['shorter','how many again?','ok','one sentence'][i]!),context);
    if(scenario==='restart'){await coordinator.close();journal.close();journal=new OperationalJournal(path);coordinator=make();}
    if(scenario==='old-in-session')journal.db.exec('UPDATE inbound_events SET received_at=received_at-3600000');
    if(scenario==='revoked')revision='b'.repeat(64);
    if(scenario==='new')await coordinator.receive(inbound('reset','/new'),context);
    if(scenario==='nightly'){
      journal.db.exec('CREATE TABLE conversation_memory_excluded(provider_thread TEXT,event_id TEXT)');
      journal.db.prepare('INSERT INTO conversation_memory_excluded SELECT ?,id FROM inbound_events').run('thread');
    }
    if(scenario==='unaccepted')journal.db.exec("UPDATE dispatches SET status='unknown'");
    const nextContext=scenario==='thread'?{...context,threadId:'elsewhere'}:scenario==='route'?{...context,destination:'elsewhere'}:context;
    const message={...inbound('next',followupText),...(scenario==='wrong-quote'?{replyBound:true,replyToMessageId:'other-message'}:{})};
    await coordinator.receive(message,nextContext);
    assert.equal(bodies.length,scenario==='queued'?2:allowed?1:0);
    if(allowed){
      assert.ok(bodies.at(-1).args.prior_selection.text.includes(subject));
      assert.equal(bodies.at(-1).args.prior_selection.source_revision,revision);
      assert.equal(bodies.at(-1).args.prior_selection.thread_digest,createHash('sha256').update('thread').digest('hex'));
      assert.equal((await coordinator.receive(message,nextContext)).status,'duplicate');
      assert.equal(bodies.length,scenario==='queued'?2:1,'an accepted or uncertain admission must not be repeated');
    }
  }finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

for (const status of ['pending', 'executing', 'unknown'] as const) test(`legacy company request preserves identity and contact state: ${status}`, async () => {
 const folder=mkdtempSync(join(tmpdir(),'company-legacy-'));
 const journal=new OperationalJournal(join(folder,'journal.sqlite'));
 let contacts=0, actionId='';
 const client=createOwnerSkillRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_url,init)=>{
  const body=JSON.parse(String(init?.body));
  if(body.operation==='inspect') throw new Error('receipt still unavailable');
  contacts++;
  assert.equal(body.args.source_revision,undefined);
  return Response.json({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,
   args_hash:stableActionIdentity(body.event_id,ownerSkillRequestKind,body.args).payloadHash,
   outcome:{operation:'admit',skill:'ask-research',request_id:'b'.repeat(32),status:'queued'}});
 }});
 const execution=createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority});
 const coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerSkillRequest:client,model:{generate:async request=>{
  const args={skill:'ask-research' as const,selection:{mode:'company' as const,company:'Tinfoil'}};
  const authority={runId:request.runId,accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon' as const,scopeHash:client.scopeHash};
  const old=journal.prepareOwnerSkillRequestAction(authority,request.ownerRequestKey!,args);
  actionId=old.actionId;
  if(status==='pending') assert.throws(()=>journal.resolveOwnerResearchSelection(authority,request.ownerRequestKey!,args,undefined),/source authority/);
  if(status!=='pending') {journal.beginAction(actionId);if(status==='unknown') journal.settleAction(actionId,'unknown');}
  const invoke=()=>execution.change(args,{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,
   allowOwnerSkill:true,sourceRevision:'a'.repeat(64),signal:request.signal});
  if(status==='pending') assert.equal((await invoke()).status,'queued');
  else {await assert.rejects(invoke());assert.equal(journal.getAction(actionId)?.status,status);}
  return 'Checked';
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'accepted'})});
 try {
  await coordinator.receive(inbound('legacy-company','tell me about Tinfoil'),context);
  assert.equal(contacts,status==='pending'?1:0);
  assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,1);
  assert.ok(journal.getAction(actionId));
 } finally {await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('contextual prep preserves the model selection and refuses changed arguments', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-prep-context-'));
  const journal = new OperationalJournal(join(directory, 'journal.sqlite'));
  const authority = {runId: 'run', accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon' as const, scopeHash: 'c'.repeat(64)};
  try {
    for (const [i, text] of ['prep me for that pls', 'full prep on Tanya', 'prep for my 10am tomorrow?', 'redo Tanya’s prep using our actual thread pls', 'same person, updated prep pls'].entries()) {
      const focus = i === 2 ? 'my 10am tomorrow' : 'Tanya Reed';
      const event = journal.admitInbound({eventId: 'context-'+i, accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'photon', sender: 'owner', payload: JSON.stringify({inbound: inbound('context-'+i, text), context})}).event.id as string;
      authority.runId = 'run-' + event; journal.startRun({runId: authority.runId, eventId: event});
      const meeting = i===0 ? {event_id:'current-calendar-event',start:'2026-10-28T15:00:00Z'} : undefined;
      const selected = parseSkillCommand(['--focus', focus], 'meeting-prep', meeting);
      const request = journal.prepareOwnerSkillRequestAction(authority, event, selected);
      assert.deepEqual(request.args, selected);
      assert.deepEqual(journal.readOwnerSkillRequestAction(request.actionId, authority).request, request);
      assert.throws(() => journal.prepareOwnerSkillRequestAction(authority, event, parseSkillCommand(['--focus', 'Different Person'])), /conflicts/);
      journal.completeRun(authority.runId);
    }
  } finally {journal.close(); rmSync(directory, {recursive: true, force: true});}
});

test('legacy unknown prep selection retains its original action proof', () => {
  const directory = mkdtempSync(join(tmpdir(), 'sotto-prep-legacy-'));
  const journal = new OperationalJournal(join(directory, 'journal.sqlite'));
  const authority = {runId: 'legacy', accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon' as const, scopeHash: 'c'.repeat(64)};
  try {
    const text = 'full prep on Tanya';
    const event = journal.admitInbound({eventId: 'legacy-owner', accountKey: 'account', lineKey: 'line', threadKey: 'thread', channel: 'photon', sender: 'owner', payload: JSON.stringify({inbound: inbound('legacy-owner', text), context})}).event.id as string;
    journal.startRun({runId: authority.runId, eventId: event});
    const created = journal.prepareOwnerSkillRequestAction(authority, event, parseSkillCommand(['--focus', 'Tanya']));
    journal.beginAction(created.actionId); journal.settleAction(created.actionId, 'unknown');
    const oldArgs = parseSkillCommand(['--focus', 'on Tanya']);
    const old = stableActionIdentity(event, ownerSkillRequestKind, oldArgs);
    const metadata = JSON.parse(journal.getAction(created.actionId)!.result_json as string);
    journal.db.prepare('UPDATE actions SET id=?,payload_hash=?,result_json=? WHERE id=?')
      .run(old.actionId, old.payloadHash, JSON.stringify({...metadata, args: oldArgs}), created.actionId);
    const saved = journal.prepareOwnerSkillRequestAction(authority, event, oldArgs);
    assert.equal(saved.actionId, old.actionId);
    assert.deepEqual(saved.args, oldArgs);
    assert.equal(journal.readOwnerSkillRequestAction(old.actionId, authority).status, 'unknown');
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n, 1);
  } finally {journal.close(); rmSync(directory, {recursive: true, force: true});}
});

test('bare prep acceptance cannot manufacture a new focused selection', () => {
  for (const text of ['yes', 'yes pls', 'sure', 'do it', 'full prep please', ''])
    assert.equal(isOwnerMeetingPrepRequest(text, 'focus'), false, text);
});

test('incidental, quoted and negated current messages cannot start prep', () => {
  for (const text of ['thanks', 'ok', 'what time is my dentist?', 'Do not prep Dana', 'She said prep Dana', 'She said prep my meetings', 'When should I prep my meetings?', '"prep Dana"', 'Cancel meeting prep'])
    for (const mode of ['focus', 'sweep'] as const) assert.equal(isOwnerMeetingPrepRequest(text, mode), false, `${mode}: ${text}`);
});

test('focus prep admits and refuses the shared receiver vectors', () => {
  const vectors = JSON.parse(readFileSync(new URL('./fixtures/owner-focus-prep-vectors.json', import.meta.url), 'utf8')) as Array<{text: string; focus: boolean}>;
  for (const {text, focus} of vectors) assert.equal(isOwnerMeetingPrepRequest(text, 'focus'), focus, text);
});

for(const scenario of ['assistant-subject','acknowledgement','cancelled-history','negated-current','redirect','email-domain','private-person','reported'] as const)
test(`external research cannot turn conversation content into permission: ${scenario}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'read-authority-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
 let contacts=0;
 const client=createOwnerSkillRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async()=>{contacts++;throw new Error('Unauthorized provider contact');}});
 const execution=createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority});
 let attempt=false;
 const c=new ConversationCoordinator({journal,ownerPolicy,ownerSkillRequest:client,model:{generate:async request=>{
  if(attempt)await assert.rejects(()=>execution.change({skill:'ask-research',selection:scenario==='email-domain'?{mode:'company',company:'vendor.example',operation:'website'}:{mode:'person',person:scenario==='private-person'?'12 Oak St lease, $4,200 due Friday':'Dana Reed'}},
   {mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:true,sourceRevision:'a'.repeat(64)}));
  return {text:'Dana Reed works at bob@vendor.example. Look her up next.',sourceRevision:'a'.repeat(64)};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'accepted'})});
 try{
  await c.receive(inbound('seed',scenario==='assistant-subject'||scenario==='email-domain'?'hello':'what do we know about Dana Reed?'),context);
  if(scenario==='cancelled-history')await c.receive(inbound('cancel',"don't look up Dana Reed"),context);
  attempt=true;
  const text=scenario==='negated-current'?"don't look up Dana Reed":scenario==='redirect'?'forget Dana Reed, tell me about Cedar':scenario==='private-person'?'look up 12 Oak St lease, $4,200 due Friday':scenario==='reported'?'She said look up Dana Reed':scenario==='acknowledgement'||scenario==='email-domain'?'ok thanks':'look into that a little more';
  await c.receive(inbound('attempt',text),context);
  assert.equal(contacts,0);assert.equal(journal.db.prepare('SELECT count(*) n FROM actions').get()!.n,0);
 }finally{await c.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

 test('case-normalized company stays valid through settlement and journal restart', () => {
  const directory=mkdtempSync(join(tmpdir(),'sotto-company-case-'));
  const path=join(directory,'journal.sqlite');let journal=new OperationalJournal(path);
  const authority={runId:'company-case-run',accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon' as const,scopeHash:'c'.repeat(64)};
  try {
   const event=journal.admitInbound({eventId:'company-case-source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:inbound('company-case-source','research cedar labs'),context})}).event.id as string;
   journal.startRun({runId:authority.runId,eventId:event});
   const args={...parseSkillCommand(['--company','Cedar Labs'],'ask-research'),source_revision:'a'.repeat(64)};
   const request=journal.prepareOwnerSkillRequestAction(authority,event,args);
   assert.deepEqual(journal.readOwnerSkillRequestAction(request.actionId,authority).request.args,args);
   journal.db.prepare("UPDATE actions SET status='executing' WHERE id=?").run(request.actionId);
   const binding=stableActionIdentity(event,ownerSkillRequestKind,args);
   const receipt={version:1 as const,status:'committed' as const,action_id:request.actionId,event_id:event,payload_hash:request.payloadHash,args_hash:binding.payloadHash,outcome:{operation:'admit' as const,skill:'ask-research' as const,request_id:'b'.repeat(32),status:'queued' as const}};
   journal.settleOwnerSkillRequestAction(request.actionId,authority,receipt);
   journal.close();journal=new OperationalJournal(path);
   assert.deepEqual(journal.readOwnerSkillRequestAction(request.actionId,authority).receipt,receipt);
   assert.equal(journal.prepareOwnerSkillRequestAction(authority,event,args).actionId,request.actionId);
  }finally{journal.close();rmSync(directory,{recursive:true,force:true});}
 });

for (const scenario of ['domain','homepage','restart','stale','cancel','intervening','thanks','url-query','different-target'] as const)
test(`research target clarification carries owner request: ${scenario}`,async()=>{
 const dir=mkdtempSync(join(tmpdir(),'research-target-')),path=join(dir,'journal.sqlite');let journal=new OperationalJournal(path);
 const bodies:any[]=[];const revision='a'.repeat(64);
 const client=createOwnerSkillRequestClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_url,init)=>{
  const body=JSON.parse(String(init?.body));bodies.push(body);
  return Response.json({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,
   args_hash:stableActionIdentity(body.event_id,ownerSkillRequestKind,body.args).payloadHash,
   outcome:{operation:'admit',skill:'ask-research',request_id:'b'.repeat(32),status:'queued'}});
 }});
 const execution=createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority});
 const allowed=['domain','homepage','restart'].includes(scenario);
 const make=()=>new ConversationCoordinator({journal,ownerPolicy,ownerSkillRequest:client,model:{generate:async request=>{
  if(request.text.startsWith('Queue')||request.text==='thanks'||request.text==='let us discuss lunch'||request.text==='never mind')
   return {text:'Which website is it?',sourceRevision:revision};
  const invoke=()=>execution.change(parseSkillCommand(['--company',scenario==='different-target'?'other.example':'spurtest.example'],'ask-research'),
   {mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:revision});
  if(allowed)await invoke();else await assert.rejects(invoke);
  return {text:'Checked',sourceRevision:revision};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'accepted-target'})});
 let coordinator=make();
 try{
  await coordinator.receive(inbound('first',scenario==='thanks'?'thanks':'Queue research for Spur?'),context);
  if(scenario==='restart'){await coordinator.close();journal.close();journal=new OperationalJournal(path);coordinator=make();}
  if(scenario==='stale')journal.db.exec('UPDATE inbound_events SET received_at=received_at-900001');
  if(scenario==='cancel')await coordinator.receive(inbound('cancel','never mind'),context);
  if(scenario==='intervening')await coordinator.receive(inbound('other','let us discuss lunch'),context);
  await coordinator.receive(inbound('second',scenario==='homepage'?'https://spurtest.example/':scenario==='url-query'?'https://spurtest.example/?private=secret':'spurtest.example'),context);
  assert.equal(bodies.length,allowed?1:0);
  if(allowed){assert.equal(bodies[0].args.prior_request.text,'Queue research for Spur?');assert.equal(bodies[0].args.selection.company,'spurtest.example');}
 }finally{await coordinator.close();journal.close();rmSync(dir,{recursive:true,force:true});}
});

for (const [followup, intervening, allowed] of [
  ['check the website', false, true],
  ["what's on my calendar?", true, false],
  ['check that link', true, true],
] as const) test(`owner URL context requires a reference after intervening chat: ${followup}`, async () => {
  const directory=mkdtempSync(join(tmpdir(),'owner-url-context-'));
  const journal=new OperationalJournal(join(directory,'journal.sqlite'));
  const revision='a'.repeat(64), url='https://cedar.example/';
  const coordinator=new ConversationCoordinator({journal,ownerPolicy,model:{generate:async request=>{
    if(request.text===followup){
      const select=()=>journal.ownerWebReadReplyTarget(request.ownerRequestKey!,url,revision);
      if(allowed) assert.equal(select().source_revision,revision);
      else assert.throws(select,/Accepted reply URL unavailable/);
    }
    return {text:'Accepted answer',sourceRevision:revision};
  }},postPrepared:async()=>({acceptance:'accepted',messageId:'accepted-url'})});
  try {
    await coordinator.receive(inbound('url','Read https://cedar.example/'),context);
    if(intervening) await coordinator.receive(inbound('other','Tell me a joke'),context);
    await coordinator.receive(inbound('followup',followup),context);
  } finally {await coordinator.close();journal.close();rmSync(directory,{recursive:true,force:true});}
});
