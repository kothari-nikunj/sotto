import { stableActionIdentity } from '../src/tools/action-gate.js';
import { createOwnerGmailDraftClient } from '../src/tools/owner-gmail-draft-client.js';
import { gmailDraftReviewHash } from '../src/tools/owner-gmail-draft-contract.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { RequestContext } from '@mastra/core/request-context';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator, type ModelRequest } from '../src/conversation.js';
import { createOwnerCalendarClient } from '../src/tools/owner-calendar-client.js';
import { createOwnerCalendarTools,createCalendarCommand } from '../src/tools/owner-calendar-tool.js';
import { calendarIdSchema,calendarArgsSchema,calendarProposalSchema, calendarReviewHash, calendarPreviewMatches, calendarReceiptMatches, calendarReviewSchema, calendarCreateEventId, calendarCreateProposalSchema, calendarReceiptSchema,calendarMoveUpdateProposalSchema,calendarUpdateProposalSchema,calendarReviewText,validCalendarReview, type CalendarPreview } from '../src/tools/owner-calendar-contract.js';
const revision = 'a'.repeat(64);
const args = { operation: 'propose_move' as const, event_id: 'cedar', start: '2026-10-10T12:00:00-07:00', end: '2026-10-10T13:00:00-07:00' };
const review = { operation: 'move' as const, calendar_id: 'primary' as const, event_id: 'cedar', etag: '"1"', summary: 'Cedar *review*', html_link: '', old_start: '2026-10-10T10:00:00-07:00', old_end: '2026-10-10T11:00:00-07:00', new_start: args.start, new_end: args.end, notifications: 'all' as const, source_revision: revision, review_hash: '' };
review.review_hash = calendarReviewHash(review);
const identity = { tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner', line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: revision };
const context = { threadId: 'thread', destination: 'photon', lineKey: 'line' };
const inbound = (eventId: string, text: string) => ({ channel: 'photon' as const, accountId: 'account', senderId: 'owner', isDirect: true, eventId, text });
function fixture(previewReview: CalendarPreview["review"] | ((actionId:string)=>CalendarPreview["review"]) = review) {
    const folder = mkdtempSync(join(tmpdir(), 'calendar-proof-')), journal = new OperationalJournal(join(folder, 'journal.sqlite'));
    let contacts = 0;
    const client = createOwnerCalendarClient({ identity, bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: revision, fetch: async (_input, init) => {
            const body = JSON.parse(init!.body as string);
            contacts++;
            assert.equal(body.operation, 'preview');
            return new Response(JSON.stringify({ version: 1, status: 'preview', event_id: body.event_id, payload_hash: body.payload_hash, review:typeof previewReview==='function'?previewReview(body.action_id):previewReview }));
        } });
    const tools = createOwnerCalendarTools({ client, journal: () => journal, sourceAuthority: { read: async () => ({ version: 2 as const, complete: true, revision, bridge_sources: [], google_sources: ['calendar' as const] }) } });
    const call = async (name: keyof typeof tools, input: unknown, request: ModelRequest, overrides: Record<string, unknown> = {}) => {
        const requestContext = new RequestContext();
        for (const [key, value] of Object.entries({ mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey, sourceRevision: revision, allowOwnerSkill: request.allowOwnerSkill, ...overrides }))
            requestContext.set(key, value);
        return tools[name].execute!(input as never, { requestContext, abortSignal: request.signal } as never);
    };
    return { journal, client, call, get contacts() { return contacts; }, close() { journal.close(); rmSync(folder, { recursive: true, force: true }); } };
}
test('Calendar closed input and exact review/receipt bindings reject invalid dates, recipient fields and changed times', () => {
    assert.ok(calendarProposalSchema.safeParse(args).success);
    for (const changed of [{ start: '2026-02-30T12:00:00Z' }, { end: args.start }, { start: '2026-10-10T24:00:00Z' }, { calendar_id: 'primary' }, { attendees: [] }])
        assert.equal(calendarProposalSchema.safeParse({ ...args, ...changed }).success, false);
    assert.equal(calendarReviewSchema.safeParse({ ...review, html_link: 'https://evil.example/' }).success, false);
    const preview: CalendarPreview = { version: 1, status: 'preview', event_id: revision, payload_hash: revision, review };
    assert.ok(calendarPreviewMatches(preview, args));
    assert.equal(calendarPreviewMatches(preview, { ...args, start: '2026-10-10T14:00:00-07:00' }), false);
    const move = { operation: 'move' as const, review, proposal: { event_id: revision, action_id: revision, args_hash: revision, accepted_dispatch_id: 'dispatch', source_revision: revision } };
    const receipt = { version: 1 as const, status: 'committed' as const, action_id: revision, event_id: revision, payload_hash: revision, args_hash: revision, outcome: { operation: 'move' as const, calendar_id: 'primary' as const, event_id: 'cedar', start: args.start, end: args.end, review_hash: review.review_hash, status: 'moved' as const } };
    assert.ok(calendarReceiptMatches(receipt, move));
    assert.equal(calendarReceiptMatches({ ...receipt, outcome: { ...receipt.outcome, event_id: 'other' } }, move), false);
});
test('early, foreign and intervening-question yes cannot move', async () => {
    const f = fixture();
    const sent: string[] = [];
    let confirmFailed = 0;
    const c = new ConversationCoordinator({ journal: f.journal, ownerCalendar: f.client, ownerPolicy: { channel: 'photon', accountId: 'account', ownerId: 'owner' }, model: { generate: async (request) => {
                if (request.text.startsWith('Move'))
                    await f.call('calendar_move_review', { event_id: args.event_id, start: args.start, end: args.end }, request);
                else if (request.text === 'yes') {
                    await assert.rejects(f.call('calendar_move_confirm', {}, request));
                    confirmFailed++;
                }
                return { text: 'A newer Gmail-style question: save this separate review?', sourceRevision: revision };
            } }, postPrepared: async (post) => { sent.push(post.text); return { acceptance: 'accepted', messageId: 'accepted-' + sent.length }; } });
    try {
        await c.receive(inbound('early', 'yes'), context);
        assert.equal(f.contacts, 0);
        assert.equal((await c.receive({ ...inbound('foreign', 'yes'), senderId: 'other' }, context)).status, 'denied');
        await c.receive(inbound('proposal', 'Move Cedar'), context);
        assert.match(sent.at(-1)!, /^Move:/);
        await c.receive(inbound('newer', 'Another question'), context);
        await c.receive(inbound('yes', 'yes'), context);
        assert.equal(confirmFailed, 2);
        assert.equal(f.contacts, 1);
        assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_calendar_tool'").get()!.n, 1);
    }
    finally {
        await c.close();
        f.close();
    }
});
test('Calendar review ACK stays fenced when close happens during awaited delivery preflight', async () => {
    const f = fixture();
    let contacts = 0, release!: () => void, entered!: () => void;
    const gate = new Promise<void>(done => { release = done; }), started = new Promise<void>(done => { entered = done; });
    const c = new ConversationCoordinator({ journal: f.journal, ownerCalendar: f.client, ownerPolicy: { channel: 'photon', accountId: 'account', ownerId: 'owner' }, model: { generate: async (request) => { await f.call('calendar_move_review', { event_id: args.event_id, start: args.start, end: args.end }, request); return 'ignored'; } }, canPostPrepared: async () => { entered(); await gate; return true; }, postPrepared: async () => { contacts++; return { acceptance: 'accepted', messageId: 'late' }; } });
    try {
        const result = c.receive(inbound('proposal', 'Move Cedar'), context);
        await started;
        const closing = c.close();
        release();
        await closing;
        await result;
        assert.equal(contacts, 0);
        assert.equal(f.journal.db.prepare("SELECT status FROM actions WHERE kind='owner_calendar_tool'").get()!.status, 'succeeded');
    }
    finally {
        await c.close();
        f.close();
    }
});
for (const scenario of ['undelivered', 'wrong_target', 'competing_review'] as const)
    test(`Calendar confirmation refuses ${scenario}; cross-kind mutation cannot borrow the proposal event`, async () => {
        const f = fixture();
        let refusal = 0, crossKind = 0;
        const gmailClient = createOwnerGmailDraftClient({ identity, bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: revision, fetch: async () => { throw new Error('No Gmail provider contact'); } });
        const c = new ConversationCoordinator({ journal: f.journal, ownerCalendar: f.client, ownerGmailDraft: gmailClient, ownerPolicy: { channel: 'photon', accountId: 'account', ownerId: 'owner' }, model: { generate: async (request) => {
                    const authority = { runId: request.runId, channel: 'photon' as const, accountId: 'account', ownerId: 'owner', lineKey: 'line', scopeHash: f.client.scopeHash };
                    if (request.text === 'Move Cedar') {
                        await f.call('calendar_move_review', { event_id: args.event_id, start: args.start, end: args.end }, request);
                        assert.throws(() => f.journal.prepareOwnerTaskToolAction(authority, request.ownerRequestKey!, { operation: 'add', text: 'Move Cedar' }), /another private mutation/);
                        crossKind++;
                    }
                    else if (request.text === 'Review this Gmail draft') {
                        const requestArgs = { operation: 'propose' as const, message_id: 'message-1', body: 'Exact separate draft' };
                        const pending = f.journal.prepareOwnerGmailDraftAction(authority, request.ownerRequestKey!, requestArgs, revision);
                        const gmailReview = { message_id: 'message-1', thread_id: 'thread-1', to: 'dana@example.com', subject: 'Re: Cedar', body: requestArgs.body, reply_message_id: '<message@example.com>', source_revision: revision, review_hash: '' };
                        gmailReview.review_hash = gmailDraftReviewHash(gmailReview);
                        f.journal.beginAction(pending.actionId);
                        f.journal.settleOwnerGmailDraftAction(pending.actionId, authority, { version: 1, status: 'preview', event_id: pending.eventId, payload_hash: pending.payloadHash, review: gmailReview });
                    }
                    else {
                        await assert.rejects(f.call('calendar_move_confirm', {}, request));
                        refusal++;
                    }
                    return { text: 'ordinary response', sourceRevision: revision };
                } }, postPrepared: async (post) => ({ acceptance: scenario === 'undelivered' && post.text.startsWith('Move:') ? 'rejected' : 'accepted', messageId: post.text.startsWith('Move:') ? 'calendar-review' : 'gmail-review' }) });
        try {
            await c.receive(inbound('proposal', 'Move Cedar'), context);
            if (scenario === 'competing_review')
                await c.receive(inbound('gmail-proposal', 'Review this Gmail draft'), context);
            await c.receive({ ...inbound('yes', 'yes'), ...(scenario === 'wrong_target' ? { replyBound: true, replyToMessageId: 'wrong-review' } : {}) }, context);
            if (scenario === 'undelivered') {
                const event = f.journal.db.prepare("SELECT id FROM inbound_events WHERE source_id='yes'").get()!;
                f.journal.startRun({ runId: 'undelivered-confirmation', eventId: event.id as string });
                assert.throws(() => f.journal.selectOwnerCalendarProposal({ runId: 'undelivered-confirmation', channel: 'photon', accountId: 'account', ownerId: 'owner', lineKey: 'line', scopeHash: f.client.scopeHash }, event.id as string, revision), /Accepted Calendar review unavailable|current accepted question/);
            }
            else
                assert.equal(refusal, 1);
            assert.equal(crossKind, 1);
            assert.equal(f.contacts, 1);
            assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_calendar_tool' AND json_extract(result_json,'$.args.operation')='move'").get()!.n, 0);
        }
        finally {
            await c.close();
            f.close();
        }
    });

test('RSVP discriminated review preserves move hash and binds response, dates and scope',()=>{
 const originalHash=calendarReviewHash(review);
 assert.equal(originalHash,'ec3f05e5819134742cafd996891f94cce97a3db1484644a6fca086b16bfb523e');
 const r={operation:'rsvp' as const,calendar_id:'primary' as const,event_id:'cedar',etag:'"1"',summary:'Cedar',html_link:'',event_scope:'occurrence' as const,all_day:true,start:'2026-10-10',end:'2026-10-11',recurring_event_id:'cedar_series',original_start:'2026-10-10',self_email:'owner@example.com',old_response:'needsAction' as const,new_response:'declined' as const,notifications:'all' as const,source_revision:revision,review_hash:''};
 r.review_hash=calendarReviewHash(r);
 assert.ok(calendarReviewSchema.safeParse(r).success);
 const preview={version:1 as const,status:'preview' as const,event_id:revision,payload_hash:revision,review:r};
 assert.ok(calendarPreviewMatches(preview,{operation:'propose_rsvp',event_id:'cedar',response:'declined'}));
 assert.equal(calendarPreviewMatches(preview,{operation:'propose_rsvp',event_id:'cedar',response:'accepted'}),false);
 assert.equal(calendarPreviewMatches(preview,args),false);
 assert.equal(calendarReviewSchema.safeParse({...r,event_scope:'series'}).success,false);
 assert.equal(calendarReviewSchema.safeParse({...r,end:'2026-02-30'}).success,false);
 assert.equal(calendarReviewSchema.safeParse({...r,all_day:false}).success,false);
 assert.equal(calendarReviewHash(review),originalHash);
});

for(const rsvp of [false,true])test(`accepted ${rsvp?'RSVP':'move'} review cannot use opposite confirmation wrapper`,async()=>{
 const r:CalendarPreview['review']={operation:'rsvp',calendar_id:'primary',event_id:'cedar',etag:'"1"',summary:'Cedar',html_link:'',event_scope:'single',all_day:false,start:review.old_start,end:review.old_end,recurring_event_id:null,original_start:null,self_email:'owner@example.com',old_response:'needsAction',new_response:'accepted',notifications:'all',source_revision:revision,review_hash:''};
 r.review_hash=calendarReviewHash(r);
 const f=fixture(rsvp?r:review);let refused=0;
 const c=new ConversationCoordinator({journal:f.journal,ownerCalendar:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{
  if(request.text==='Review')await f.call(rsvp?'calendar_rsvp_review':'calendar_move_review',rsvp?{event_id:'cedar',response:'accepted'}:{event_id:args.event_id,start:args.start,end:args.end},request);
  else {await assert.rejects(f.call(rsvp?'calendar_move_confirm':'calendar_rsvp_confirm',{},request),/Accepted Calendar review unavailable/);refused++;}
  return {text:'Please clarify.',sourceRevision:revision};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'accepted-review'})});
 try{await c.receive(inbound('review','Review'),context);await c.receive(inbound('yes','yes'),context);assert.equal(refused,1);assert.equal(f.contacts,1);assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_calendar_tool'").get()!.n,1);}
 finally{await c.close();f.close();}
});

test('same owner event with admitted Gmail proposal cannot prepare Calendar move or RSVP',async()=>{
 const f=fixture();let fenced=0;
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{
  const authority={runId:request.runId,channel:'photon' as const,accountId:'account',ownerId:'owner',lineKey:'line',scopeHash:f.client.scopeHash};
  f.journal.prepareOwnerGmailDraftAction(authority,request.ownerRequestKey!,{operation:'propose',message_id:'message-1',body:'Exact draft'},revision);
  for(const candidate of [args,{operation:'propose_rsvp' as const,event_id:'cedar',response:'accepted' as const},{operation:'propose_create' as const,summary:'Cedar',start:args.start,end:args.end,attendees:[],location:'',description:''}]){
   assert.throws(()=>f.journal.prepareOwnerCalendarAction(authority,request.ownerRequestKey!,candidate,revision),/another private mutation/);fenced++;
  }
  return {text:'Please clarify.',sourceRevision:revision};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});
 try{await c.receive(inbound('same','Review this draft'),context);assert.equal(fenced,3);assert.equal(f.contacts,0);assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_calendar_tool'").get()!.n,0);}
 finally{await c.close();f.close();}
});

test('creation normalizes guest identity and binds the complete review to its backend event identity', async () => {
    const {calendarCreateProposalSchema,calendarCreateEventId,calendarReviewText} = await import('../src/tools/owner-calendar-contract.js');
    const {stableActionIdentity} = await import('../src/tools/action-gate.js');
    const raw={operation:'propose_create' as const,summary:'Cedar planning',start:args.start,end:args.end,attendees:['DANA@example.com','dana@example.com'],location:'Cedar office, 10 Main Street',description:'Discuss roadmap\nAgree next steps'};
    const normalized=calendarCreateProposalSchema.parse(raw);
    assert.deepEqual(normalized.attendees,['dana@example.com']);
    assert.deepEqual(calendarCreateProposalSchema.parse({...raw,attendees:['𐀀@example.com','\ue000@example.com','İ@example.com']}).attendees,['i\u0307@example.com','\ue000@example.com','𐀀@example.com']);
    for(const changed of [{end:raw.start},{summary:''},{location:'Cedar\nOffice'},{description:'Agenda\tsecret'},{attendees:['Dana <dana@example.com>']}])assert.equal(calendarCreateProposalSchema.safeParse({...raw,...changed}).success,false);
    const binding=stableActionIdentity(revision,'owner_calendar_tool',normalized);
    const r={...normalized,operation:'create' as const,calendar_id:'primary' as const,event_id:calendarCreateEventId(binding.actionId),effective_attendees:['dana@example.com','owner@example.com'],organizer_email:'owner@example.com',organizer_added:true,notifications:'all' as const,source_revision:revision,review_hash:''};
    r.review_hash=calendarReviewHash(r);
    const preview:CalendarPreview={version:1,status:'preview',event_id:revision,payload_hash:revision,review:r};
    assert.ok(calendarPreviewMatches(preview,normalized,binding.actionId));
    assert.equal(calendarPreviewMatches(preview,normalized,'b'.repeat(64)),false);
    assert.equal(calendarPreviewMatches(preview,{...normalized,description:'Changed agenda'},binding.actionId),false);
    assert.match(calendarReviewText(preview),/Guests: dana@example.com, owner@example.com/);
    assert.match(calendarReviewText(preview),/Agenda:\nDiscuss roadmap\nAgree next steps/);
    assert.equal(calendarReviewSchema.safeParse({...r,effective_attendees:['dana@example.com']}).success,false);
    const personal={...r,attendees:[],effective_attendees:[],organizer_email:null,organizer_added:false};
    assert.ok(calendarReviewSchema.safeParse(personal).success);
    const create={operation:'create' as const,review:r,proposal:{event_id:revision,action_id:binding.actionId,args_hash:binding.payloadHash,accepted_dispatch_id:'accepted',source_revision:revision}};
    const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,outcome:{operation:'create' as const,calendar_id:'primary' as const,event_id:r.event_id,summary:r.summary,start:r.start,end:r.end,html_link:'https://calendar.google.com/calendar/event?eid=created',review_hash:r.review_hash,status:'created' as const,organizer_listed:true,location_attached:true,description_attached:true}};
    assert.ok(calendarReceiptMatches(receipt,create));
    for(const change of [{organizer_listed:null},{location_attached:false},{description_attached:null}])assert.equal(calendarReceiptMatches({...receipt,outcome:{...receipt.outcome,...change}},create),false);
    for(const html_link of ['', 'https://evil.example/event'])assert.equal(calendarReceiptSchema.safeParse({...receipt,outcome:{...receipt.outcome,html_link}}).success,false);
    assert.equal(calendarReceiptMatches({...receipt,outcome:{...receipt.outcome,event_id:'b'.repeat(64)}},create),false);
    const largeReview={...r,summary:'😀'.repeat(1024)};
    largeReview.review_hash=calendarReviewHash(largeReview);
    const largeArgs={...create,review:largeReview};
    const largeBinding=stableActionIdentity(revision,'owner_calendar_tool',largeArgs);
    const largeReceipt={...receipt,action_id:largeBinding.actionId,args_hash:largeBinding.payloadHash,outcome:{...receipt.outcome,summary:largeReview.summary,review_hash:largeReview.review_hash}};
    const escaped=JSON.stringify(largeReceipt).replace(/[\u0080-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
    assert.ok(Buffer.byteLength(escaped)>8192);
    const client=createOwnerCalendarClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async()=>new Response(escaped)});
    assert.equal((await client.call('inspect',{actionId:largeBinding.actionId,eventId:revision,payloadHash:revision,text:'yes',admittedAtMs:Date.now(),providerThreadId:'thread',args:largeArgs})).status,'committed');
    const overlong={...r,description:'規'.repeat(4000)};overlong.review_hash=calendarReviewHash(overlong);
    const {validCalendarReview}=await import('../src/tools/owner-calendar-contract.js');
    assert.equal(validCalendarReview({...preview,review:overlong}),false);
});


for(const scenario of ['wrong_wrapper','wrong_reply','undelivered','intervening'] as const)test(`creation accepted-review fence: ${scenario}`,async()=>{
 const proposal=calendarCreateProposalSchema.parse({operation:'propose_create',summary:'Cedar planning',start:args.start,end:args.end,attendees:[],location:'',description:''});
 const f=fixture(actionId=>{const r={...proposal,operation:'create' as const,calendar_id:'primary' as const,event_id:calendarCreateEventId(actionId),effective_attendees:[],organizer_email:null,organizer_added:false,notifications:'all' as const,source_revision:revision,review_hash:''};r.review_hash=calendarReviewHash(r);return r;});
 let refused=0;
 const c=new ConversationCoordinator({journal:f.journal,ownerCalendar:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{
  if(request.text==='Review')await f.call('calendar_create_review',{summary:proposal.summary,start:proposal.start,end:proposal.end,attendees:[],location:'',description:''},request);
  else if(request.text==='yes'){await assert.rejects(f.call(scenario==='wrong_wrapper'?'calendar_move_confirm':'calendar_create_confirm',{},request));refused++;}
  return {text:'Please clarify.',sourceRevision:revision};
 }},postPrepared:async()=>({acceptance:scenario==='undelivered'?'rejected':'accepted',messageId:'accepted-review'})});
 try{
  await c.receive(inbound('review','Review'),context);
  if(scenario==='intervening')await c.receive(inbound('other','Another question'),context);
  await c.receive({...inbound('yes','yes'),...(scenario==='wrong_reply'?{replyBound:true,replyToMessageId:'another-provider-message'}:{})},context);
  if(scenario==='undelivered'){
   const row=f.journal.db.prepare("SELECT id FROM inbound_events WHERE source_id='yes'").get()!;
   f.journal.startRun({runId:'confirm-undelivered',eventId:row.id as string});
   assert.throws(()=>f.journal.selectOwnerCalendarProposal({runId:'confirm-undelivered',channel:'photon',accountId:'account',ownerId:'owner',lineKey:'line',scopeHash:f.client.scopeHash},row.id as string,revision));
  }else assert.equal(refused,1);
  assert.equal(f.contacts,1);
  assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_calendar_tool' AND json_extract(result_json,'$.args.operation')='create'").get()!.n,0);
 }finally{await c.close();f.close();}
});

const updateReview={operation:'update' as const,calendar_id:'primary' as const,event_id:'event_one',etag:'"version1"',summary:'Café 🗓',html_link:'',event_scope:'single' as const,all_day:true,start:'2026-10-07',end:'2026-10-08',recurring_event_id:null,original_start:null,before_fields:{summary:'Café 🗓',location:'Old place',description:'Old\nagenda'},fields:{summary:'Café planning 🗓',location:'',description:'First line\nSecond line'},notifications:'all' as const,source_revision:revision,review_hash:'4d010363ed612535a5562bda97cdd8821f43d2907e3796dc1a02ea8da19e56f3'};
test('Calendar detail update nested Unicode hash, explicit clears, selected key sets and full review bounds',()=>{
 assert.equal(calendarReviewHash(updateReview),updateReview.review_hash);assert.ok(calendarReviewSchema.safeParse(updateReview).success);
 const proposal={operation:'propose_update' as const,event_id:updateReview.event_id,fields:updateReview.fields};
 const preview={version:1 as const,status:'preview' as const,event_id:revision,payload_hash:revision,review:updateReview};assert.ok(calendarPreviewMatches(preview,proposal));
 for(const fields of [{},{start:'2026-10-10'}, {summary:'bad\nname'},{description:'bad\ttab'}])assert.equal(calendarUpdateProposalSchema.safeParse({...proposal,fields}).success,false);
 assert.ok(calendarUpdateProposalSchema.safeParse({...proposal,fields:{summary:''}}).success);
 assert.equal(calendarReviewSchema.safeParse({...updateReview,before_fields:{summary:updateReview.summary}}).success,false);
 assert.match(calendarReviewText(preview),/Location:\nBefore:\nOld place\nAfter:\n\(empty\)/);
 assert.match(calendarReviewText({...preview,review:{...updateReview,fields:{...updateReview.fields,summary:''}}}),/After:\n\(no title\)/);
 for(const scope of ['single','occurrence','series'] as const){const r={...updateReview,event_scope:scope,recurring_event_id:scope==='occurrence'?'master':null,original_start:scope==='occurrence'?updateReview.start:null};assert.ok(calendarReviewSchema.safeParse(r).success);assert.match(calendarReviewText({...preview,review:r}),scope==='series'?/entire recurring series/:scope==='occurrence'?/this occurrence/:/this event/);}
 const args={operation:'update' as const,review:updateReview,proposal:{event_id:revision,action_id:revision,args_hash:revision,accepted_dispatch_id:'dispatch',source_revision:revision}};
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,outcome:{operation:'update' as const,calendar_id:'primary' as const,event_id:updateReview.event_id,review_hash:updateReview.review_hash,status:'updated' as const}};
 assert.ok(calendarReceiptMatches(receipt,args));assert.equal(calendarReceiptMatches({...receipt,outcome:{...receipt.outcome,event_id:'other'}},args),false);
 const huge={...updateReview,before_fields:{description:'李'.repeat(16000)},fields:{description:''},review_hash:''};huge.review_hash=calendarReviewHash(huge);assert.equal(validCalendarReview({...preview,review:huge}),false);
});
for(const wrong of ['calendar_move_confirm','calendar_rsvp_confirm','calendar_create_confirm'] as const)test(`accepted detail review refuses ${wrong}`,async()=>{
 const f=fixture(updateReview);const c=new ConversationCoordinator({journal:f.journal,ownerCalendar:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async r=>{
  if(r.text==='Update')return String(await f.call('calendar_update_review',{event_id:updateReview.event_id,fields:updateReview.fields},r));
  await assert.rejects(f.call(wrong,{},r));return 'Wrong confirmation refused';
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});
 try{await c.receive(inbound('review','Update'),context);await c.receive(inbound('yes','Yes'),context);assert.equal(f.contacts,1);}finally{await c.close();f.close();}
});
test('original calendar-update command delegates exact review and refuses mixed event fields',async()=>{
 let modelError:unknown;const f=fixture(updateReview);const command=createCalendarCommand({client:f.client,journal:()=>f.journal,sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['calendar' as const]})}});
 const c=new ConversationCoordinator({journal:f.journal,ownerCalendar:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async r=>{
  try { const ctx={mode:'chat',runId:r.runId,ownerRequestKey:r.ownerRequestKey,allowOwnerSkill:r.allowOwnerSkill,sourceRevision:revision,signal:r.signal};
  await assert.rejects(command({id:'google-action',mode:'chat',args:['calendar-update','--event-id',updateReview.event_id,'--fields-json',JSON.stringify({summary:'New',start:{date:'2026-10-10'}})]},ctx));
  const result=await command({id:'google-action',mode:'chat',args:['calendar-update','--event-id',updateReview.event_id,'--fields-json',JSON.stringify(updateReview.fields)]},ctx);assert.equal((result.result as {review_text:string}).review_text,calendarReviewText({version:1,status:'preview',event_id:revision,payload_hash:revision,review:updateReview}));return 'Review'; } catch(error){modelError=error;throw error;}
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});
 try{await c.receive(inbound('review','Update'),context);if(modelError)throw modelError;assert.equal(f.contacts,1);}finally{await c.close();f.close();}
});
test('oversized serialized update request includes transport identity and refuses before HTTP',()=>{
 const f=fixture(updateReview);const args={operation:'update' as const,review:updateReview,proposal:{event_id:revision,action_id:revision,args_hash:revision,accepted_dispatch_id:'dispatch',source_revision:revision}};
 try{
 const largeArgs={...args,review:{...updateReview,before_fields:{description:'李'.repeat(15500)},fields:{description:''},review_hash:calendarReviewHash({...updateReview,before_fields:{description:'李'.repeat(15500)},fields:{description:''}})}};
 assert.throws(()=>f.client.preflight({eventId:revision,payloadHash:revision,admittedAtMs:1,providerThreadId:'thread',text:'李'.repeat(16000),actionId:stableActionIdentity(revision,'owner_calendar_tool',largeArgs).actionId,args:largeArgs}),/request capacity/);assert.equal(f.contacts,0);assert.equal(f.journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
 }finally{f.close();}
});

const combinedReview={"operation":"move_update","calendar_id":"primary","event_id":"event_emoji","etag":"\"v1\"","summary":"Café 🗓","html_link":"","event_scope":"occurrence","all_day":false,"recurring_event_id":"series_one","original_start":"2026-10-10T10:00:00-07:00","before_fields":{"description":"Old\nagenda","location":"Old place"},"fields":{"description":"First line\nSecond line 🗓","location":""},"notifications":"all","source_revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","review_hash":"09326ee7d8dbbf2db6fc29bc0313fc28223cede1b4461c29dcd68e1497bd3aea","old_start":"2026-10-10T10:00:00-07:00","old_end":"2026-10-10T11:00:00-07:00","new_start":"2026-10-10T12:00:00-07:00","new_end":"2026-10-10T13:00:00-07:00"} as const;
test('combined Calendar Unicode hash, complete render and precise timed occurrence binding',()=>{
 const parsed=calendarReviewSchema.parse(combinedReview);assert.equal(calendarReviewHash(parsed),combinedReview.review_hash);
 const preview={version:1 as const,status:'preview' as const,event_id:revision,payload_hash:revision,review:parsed};
 const proposal={operation:'propose_move_update' as const,event_id:combinedReview.event_id,start:combinedReview.new_start,end:combinedReview.new_end,fields:combinedReview.fields};
 assert.ok(calendarPreviewMatches(preview,proposal));assert.match(calendarReviewText(preview),/From:.*\nTo:/);assert.match(calendarReviewText(preview),/Location:\nBefore:\nOld place\nAfter:\n\(empty\)/);
 for(const change of [{fields:{}},{end:proposal.start},{start:'2026-10-10'},{fields:{attendees:[]}}])assert.equal(calendarMoveUpdateProposalSchema.safeParse({...proposal,...change}).success,false);
 for(const change of [{all_day:true},{event_scope:'series'},{original_start:null},{recurring_event_id:null},{before_fields:{description:'Old'}},{old_start:'invalid'}])assert.equal(calendarReviewSchema.safeParse({...combinedReview,...change}).success,false);
 const noChange={...combinedReview,new_start:combinedReview.old_start,new_end:combinedReview.old_end,fields:combinedReview.before_fields};assert.equal(calendarReviewSchema.safeParse(noChange).success,false);
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,outcome:{operation:'move_update' as const,calendar_id:'primary' as const,event_id:combinedReview.event_id,start:combinedReview.new_start,end:combinedReview.new_end,review_hash:combinedReview.review_hash,status:'updated' as const}};
 const apply={operation:'move_update' as const,review:combinedReview,proposal:{event_id:revision,action_id:revision,args_hash:revision,accepted_dispatch_id:'dispatch',source_revision:revision}};
 assert.ok(calendarReceiptMatches(receipt,apply));assert.ok(!calendarReceiptMatches({...receipt,outcome:{...receipt.outcome,end:combinedReview.old_end}},apply));
});
for(const wrong of ['calendar_move_confirm','calendar_update_confirm','calendar_rsvp_confirm','calendar_create_confirm'] as const)test(`accepted combined review refuses ${wrong}`,async()=>{
 const f=fixture(combinedReview);const c=new ConversationCoordinator({journal:f.journal,ownerCalendar:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async r=>{
  if(r.text==='Combined')return String(await f.call('calendar_move_update_review',{event_id:combinedReview.event_id,start:combinedReview.new_start,end:combinedReview.new_end,fields:combinedReview.fields},r));
  await assert.rejects(f.call(wrong,{},r));return 'Wrong confirmation refused';
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});
 try{await c.receive(inbound('review','Combined'),context);await c.receive(inbound('yes','Yes'),context);assert.equal(f.contacts,1);}finally{await c.close();f.close();}
});
test('original calendar-update combined alias delegates one typed review and refuses partial times',async()=>{
 const f=fixture(combinedReview);const command=createCalendarCommand({client:f.client,journal:()=>f.journal,sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['calendar' as const]})}});
 let failure:unknown;
 const c=new ConversationCoordinator({journal:f.journal,ownerCalendar:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async r=>{
  try{const ctx={mode:'chat',runId:r.runId,ownerRequestKey:r.ownerRequestKey,allowOwnerSkill:r.allowOwnerSkill,sourceRevision:revision,signal:r.signal};
   const base={start:{dateTime:combinedReview.new_start},end:{dateTime:combinedReview.new_end},...combinedReview.fields};
   for(const fields of [{start:base.start,location:''},{...base,start:{date:'2026-10-10'}},{...base,attendees:[]}])await assert.rejects(command({id:'google-action',mode:'chat',args:['calendar-update','--event-id',combinedReview.event_id,'--fields-json',JSON.stringify(fields)]},ctx));
   const result=await command({id:'google-action',mode:'chat',args:['calendar-update','--event-id',combinedReview.event_id,'--fields-json',JSON.stringify(base)]},ctx);assert.equal((result.result as {review_text:string}).review_text,calendarReviewText({version:1,status:'preview',event_id:revision,payload_hash:revision,review:combinedReview}));return 'Reviewed';
  }catch(error){failure=error;throw error;}
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});
 try{await c.receive(inbound('review','Combined'),context);if(failure)throw failure;assert.equal(f.contacts,1);}finally{await c.close();f.close();}
});

test('known secondary Calendar selector binds the full review and receipt without changing primary bytes',()=>{
 const calendar_id='team#calendar%40example.com';
 const proposal=calendarUpdateProposalSchema.parse({operation:'propose_update',event_id:updateReview.event_id,calendar_id,fields:updateReview.fields});
 const selected={...updateReview,calendar_id,review_hash:''};selected.review_hash=calendarReviewHash(selected);
 const preview={version:1 as const,status:'preview' as const,event_id:revision,payload_hash:revision,review:selected};
 assert.ok(calendarPreviewMatches(preview,proposal));assert.match(calendarReviewText(preview),/Calendar: team#calendar%40example.com/);
 assert.equal(calendarPreviewMatches(preview,{...proposal,calendar_id:'other@example.com'}),false);
 assert.equal(calendarPreviewMatches(preview,{operation:'propose_update',event_id:proposal.event_id,fields:proposal.fields}),false);
 const apply=calendarArgsSchema.parse({operation:'update',review:selected,proposal:{event_id:revision,action_id:revision,args_hash:revision,accepted_dispatch_id:'dispatch',source_revision:revision}});
 const receipt={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,outcome:{operation:'update' as const,calendar_id,event_id:selected.event_id,review_hash:selected.review_hash,status:'updated' as const}};
 assert.ok(calendarReceiptMatches(receipt,apply));assert.equal(calendarReceiptMatches({...receipt,outcome:{...receipt.outcome,calendar_id:'primary'}},apply),false);
 assert.equal(calendarReviewHash(updateReview),updateReview.review_hash);
 assert.deepEqual(calendarUpdateProposalSchema.parse({operation:'propose_update',event_id:updateReview.event_id,fields:updateReview.fields}),{operation:'propose_update',event_id:updateReview.event_id,fields:updateReview.fields});
 for(const value of ['', ' team@example.com','team example.com','team\u0085@example.com','team\u2028@example.com','x'.repeat(1025)])assert.equal(calendarIdSchema.safeParse(value).success,false);
 assert.equal(calendarUpdateProposalSchema.safeParse({...proposal,calendar_id:'primary'}).success,false);
 assert.equal(calendarCreateProposalSchema.safeParse({operation:'propose_create',calendar_id,summary:'Task',start:args.start,end:args.end,attendees:[],location:'',description:''}).success,false);
});
test('original calendar-update preserves the exact known calendar',async()=>{
 const calendar_id='Team#Cal%40example.com';const selected={...updateReview,calendar_id,review_hash:''};selected.review_hash=calendarReviewHash(selected);
 const f=fixture(selected);let failure:unknown;
 const command=createCalendarCommand({client:f.client,journal:()=>f.journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision,bridge_sources:[],google_sources:['calendar']})}});
 const coordinator=new ConversationCoordinator({journal:f.journal,ownerCalendar:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{try{const ctx={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:revision};const response=await command({id:'google-action',mode:'chat',args:['calendar-update','--event-id',selected.event_id,'--calendar',calendar_id,'--fields-json',JSON.stringify(selected.fields)]},ctx);assert.match((response.result as {review_text:string}).review_text,/Calendar: Team#Cal%40example.com/);return 'Review';}catch(error){failure=error;throw error;}}},postPrepared:async()=>({acceptance:'accepted',messageId:'provider'})});
 try{await coordinator.receive(inbound('secondary-command','Update the team event'),context);if(failure)throw failure;assert.equal(f.contacts,1);}finally{await coordinator.close();f.close();}
});

test('all existing event proposal kinds bind the selected calendar',()=>{
 const calendar_id='Team#Cal%40example.com';
 const rsvp={operation:'rsvp' as const,calendar_id,event_id:'cedar',etag:'"1"',summary:'Cedar',html_link:'',event_scope:'single' as const,all_day:false,start:review.old_start,end:review.old_end,recurring_event_id:null,original_start:null,self_email:'owner@example.com',old_response:'needsAction' as const,new_response:'accepted' as const,notifications:'all' as const,source_revision:revision,review_hash:''};
 for(const [rawReview,rawArgs] of [[{...review,calendar_id},{...args,calendar_id}],[rsvp,{operation:'propose_rsvp',calendar_id,event_id:'cedar',response:'accepted'}],[{...combinedReview,calendar_id},{operation:'propose_move_update',calendar_id,event_id:combinedReview.event_id,start:combinedReview.new_start,end:combinedReview.new_end,fields:combinedReview.fields}]] as const){
  const selected=calendarReviewSchema.parse({...rawReview,review_hash:calendarReviewHash(rawReview)});const proposal=calendarArgsSchema.parse(rawArgs);const preview={version:1 as const,status:'preview' as const,event_id:revision,payload_hash:revision,review:selected};
  assert.ok(calendarPreviewMatches(preview,proposal));assert.equal(calendarPreviewMatches(preview,calendarArgsSchema.parse({...rawArgs,calendar_id:'other@example.com'})),false);
 }
});
for(const selector of ['primary','Team#Cal%40example.com'])test(`original RSVP alias reviews exact selector ${selector}`,async()=>{
 const selected:CalendarPreview['review']={operation:'rsvp',calendar_id:selector,event_id:'cedar',etag:'"1"',summary:'Cedar',html_link:'',event_scope:'single',all_day:false,start:review.old_start,end:review.old_end,recurring_event_id:null,original_start:null,self_email:'owner@example.com',old_response:'needsAction',new_response:'accepted',notifications:'all',source_revision:revision,review_hash:''};selected.review_hash=calendarReviewHash(selected);
 const f=fixture(selected);let failure:unknown;let savedArgs:unknown;
 const command=createCalendarCommand({client:f.client,journal:()=>f.journal,sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['calendar' as const]})}});
 const c=new ConversationCoordinator({journal:f.journal,ownerCalendar:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{try{const ctx={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:revision};const argv=['calendar-rsvp','--event-id','cedar','--response','accepted','--calendar',selector];await assert.rejects(command({id:'google-action',mode:'chat',args:[...argv,'--unknown','extra']},ctx));await command({id:'google-action',mode:'chat',args:argv},ctx);const action=f.journal.listOwnerCalendarActionsForThread(request.ownerRequestKey!)[0]!;savedArgs=JSON.parse(String(action.result_json)).args;return 'Review';}catch(error){failure=error;throw error;}}},postPrepared:async()=>({acceptance:'accepted',messageId:'provider'})});
 try{await c.receive(inbound('rsvp-command','Accept the known calendar invite'),context);if(failure)throw failure;assert.deepEqual(savedArgs,{operation:'propose_rsvp',event_id:'cedar',...(selector==='primary'?{}:{calendar_id:selector}),response:'accepted'});assert.equal(f.contacts,1);}finally{await c.close();f.close();}
});

test('secondary confirmation reports selected coverage and retains the reviewed calendar',async()=>{
 const selected={...updateReview,calendar_id:'Team#Cal%40example.com',review_hash:''};selected.review_hash=calendarReviewHash(selected);const f=fixture(selected);let failure:unknown;let phase=0;let applies=0;
 const client={...f.client,call:async(operation:Parameters<typeof f.client.call>[0],request:Parameters<typeof f.client.call>[1],signal?:AbortSignal)=>{if(operation!=='apply')return f.client.call(operation,request,signal);assert.equal(request.args.operation,'update');if(request.args.operation!=='update')throw new Error('Wrong operation');applies++;return {version:1 as const,status:'committed' as const,action_id:request.actionId,event_id:request.eventId,payload_hash:request.payloadHash,args_hash:stableActionIdentity(request.eventId,'owner_calendar_tool',request.args).payloadHash,outcome:{operation:'update' as const,calendar_id:request.args.review.calendar_id,event_id:request.args.review.event_id,review_hash:request.args.review.review_hash,status:'updated' as const}};}};
 const command=createCalendarCommand({client,journal:()=>f.journal,sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['calendar' as const]})}});
 const c=new ConversationCoordinator({journal:f.journal,ownerCalendar:client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{try{const ctx={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:revision};const argv=phase++===0?['calendar-update','--event-id',selected.event_id,'--calendar',selected.calendar_id,'--fields-json',JSON.stringify(selected.fields)]:['calendar-update-confirm'];const result=await command({id:'google-action',mode:'chat',args:argv},ctx);assert.equal(result.coverage,'one_selected_event');return 'Review';}catch(error){failure=error;throw error;}}},postPrepared:async()=>({acceptance:'accepted',messageId:'provider'})});
 try{await c.receive(inbound('secondary-review','Update the selected event'),context);assert.equal(applies,0);await c.receive(inbound('secondary-yes','Yes'),context);if(failure)throw failure;assert.equal(applies,1);}finally{await c.close();f.close();}
});

test('original time-only update routes existing move and matching empty confirmation',async()=>{
 const selected={...review,calendar_id:'Team#Cal%40example.com',review_hash:''};selected.review_hash=calendarReviewHash(selected);const f=fixture(selected);let failure:unknown;let phase=0;let applies=0;
 const client={...f.client,call:async(operation:Parameters<typeof f.client.call>[0],request:Parameters<typeof f.client.call>[1],signal?:AbortSignal)=>{if(operation!=='apply')return f.client.call(operation,request,signal);assert.equal(request.args.operation,'move');if(request.args.operation!=='move')throw new Error('Wrong operation');applies++;return {version:1 as const,status:'committed' as const,action_id:request.actionId,event_id:request.eventId,payload_hash:request.payloadHash,args_hash:stableActionIdentity(request.eventId,'owner_calendar_tool',request.args).payloadHash,outcome:{operation:'move' as const,calendar_id:request.args.review.calendar_id,event_id:request.args.review.event_id,start:request.args.review.new_start,end:request.args.review.new_end,review_hash:request.args.review.review_hash,status:'moved' as const}};}};
 const command=createCalendarCommand({client,journal:()=>f.journal,sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['calendar' as const]})}});
 const c=new ConversationCoordinator({journal:f.journal,ownerCalendar:client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{try{const ctx={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:revision};if(phase++===0){const fields={start:{dateTime:args.start},end:{dateTime:args.end}};for(const invalid of [{start:fields.start},{...fields,start:{date:'2026-10-10'}},{...fields,attendees:[]}])await assert.rejects(command({id:'google-action',mode:'chat',args:['calendar-update','--event-id','cedar','--calendar',selected.calendar_id,'--fields-json',JSON.stringify(invalid)]},ctx));const result=await command({id:'google-action',mode:'chat',args:['calendar-update','--event-id','cedar','--calendar',selected.calendar_id,'--fields-json',JSON.stringify(fields)]},ctx);assert.equal((result.result as {review_text:string}).review_text,calendarReviewText({version:1,status:'preview',event_id:revision,payload_hash:revision,review:selected}));}else{for(const name of ['calendar-update-confirm','calendar-move-update-confirm'])await assert.rejects(command({id:'google-action',mode:'chat',args:[name]},ctx));await assert.rejects(command({id:'google-action',mode:'chat',args:['calendar-move-confirm','--event-id','cedar']},ctx));const result=await command({id:'google-action',mode:'chat',args:['calendar-move-confirm']},ctx);assert.equal(result.coverage,'one_selected_event');}return 'Review';}catch(error){failure=error;throw error;}}},postPrepared:async()=>({acceptance:'accepted',messageId:'provider'})});
 try{await c.receive(inbound('time-alias-review','Move the team event'),context);assert.equal(applies,0);await c.receive(inbound('time-alias-yes','Yes'),context);if(failure)throw failure;assert.equal(applies,1);}finally{await c.close();f.close();}
});

for(const comment of ['', '  Join after 3pm.\nThanks  '])test(`original RSVP alias preserves optional comment ${JSON.stringify(comment)}`,async()=>{
 const selector='primary';
 const selected:CalendarPreview['review']={operation:'rsvp',calendar_id:selector,event_id:'cedar',etag:'"1"',summary:'Cedar',html_link:'',event_scope:'single',all_day:false,start:review.old_start,end:review.old_end,recurring_event_id:null,original_start:null,self_email:'owner@example.com',old_response:'needsAction',new_response:'accepted',...(comment?{old_comment:'Prior note',new_comment:comment}:{}),notifications:'all',source_revision:revision,review_hash:''};selected.review_hash=calendarReviewHash(selected);
 const f=fixture(selected);let failure:unknown;let savedArgs:unknown;
 const command=createCalendarCommand({client:f.client,journal:()=>f.journal,sourceAuthority:{read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['calendar' as const]})}});
 const c=new ConversationCoordinator({journal:f.journal,ownerCalendar:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async request=>{try{const ctx={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:revision};const argv=['calendar-rsvp','--event-id','cedar','--response','accepted','--calendar',selector,'--comment',comment];await assert.rejects(command({id:'google-action',mode:'chat',args:[...argv,'--unknown','extra']},ctx));await command({id:'google-action',mode:'chat',args:argv},ctx);const action=f.journal.listOwnerCalendarActionsForThread(request.ownerRequestKey!)[0]!;savedArgs=JSON.parse(String(action.result_json)).args;return 'Review';}catch(error){failure=error;throw error;}}},postPrepared:async()=>({acceptance:'accepted',messageId:'provider'})});
 try{await c.receive(inbound('rsvp-command','Accept the known calendar invite'),context);if(failure)throw failure;assert.deepEqual(savedArgs,{operation:'propose_rsvp',event_id:'cedar',...(selector==='primary'?{}:{calendar_id:selector}),response:'accepted',...(comment?{comment}:{})});assert.equal(f.contacts,1);}finally{await c.close();f.close();}
});
