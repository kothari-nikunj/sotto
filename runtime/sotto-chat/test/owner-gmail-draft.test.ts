import {PROCEDURE_EXECUTION_GUIDE} from '../src/tools/preference-command.js';
import {createOwnerOfferReplyClient} from '../src/tools/owner-offer-reply-client.js';
import {createOwnerMeetingPrepClient} from '../src/tools/owner-meeting-prep-client.js';
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
import { createOwnerGmailDraftClient } from '../src/tools/owner-gmail-draft-client.js';
import { GMAIL_DRAFT_EXECUTION_GUIDE, createOwnerGmailDraftTools } from '../src/tools/owner-gmail-draft-tool.js';
import { stableActionIdentity, ActionHeldError } from '../src/tools/action-gate.js';
import { ownerGmailDraftKind, gmailDraftReviewHash, gmailDraftPreviewMatches, gmailDraftReceiptMatches,gmailNewSendProposalSchema,gmailDraftReviewText } from '../src/tools/owner-gmail-draft-contract.js';
const identity = { tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread', project_fingerprint: 'a'.repeat(64) };
const forbiddenMeetingPrep = createOwnerMeetingPrepClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
  fetch: async () => {assert.fail('Accepted local review must not be stolen by meeting prep');}});
const ownerPolicy = { channel: 'photon' as const, accountId: 'account', ownerId: 'owner' };
const context = { threadId: 'thread', destination: 'owner', lineKey: 'line' };
const inbound = (eventId: string, text: string) => ({ channel: 'photon' as const, accountId: 'account', senderId: 'owner',
  isDirect: true, eventId, text });
const args = { operation: 'propose' as const, message_id: 'message-1', body: 'Thanks, I will send the report tomorrow.\nBest, Owner' };
const authority = { version: 2 as const, complete: true, revision: 'd'.repeat(64), bridge_sources: [], google_sources: ['gmail' as const] };
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const usage = { inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 2, text: 2, reasoning: 0 } };
function fixture(folder: string) {
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  const calls: Record<string, unknown>[] = [], receipts = new Map<string, Record<string, unknown>>();
  let loseCreate = false, hideInspect = false, revokeOnApply = false, state = authority;
  let previewRefusal: 'draft_permission_unavailable' | 'target_changed' | 'invalid_target' | undefined;
  const client = createOwnerGmailDraftClient({ bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async (_url, init) => {
      const body = JSON.parse(init!.body as string);
      calls.push(body);
      assert.equal(body.text_hash, createHash('sha256').update(body.text).digest('hex'));
      assert.ok(!body.text.startsWith('/loop'));
      if (body.operation === 'preview') {
        if (previewRefusal) return reply({version:1,status:'rejected',code:previewRefusal},409);
        if(body.args.operation==='list_followup') return reply({version:1,status:'offers',offers:[{offer_id:'f'.repeat(32),to_name:'Dana',to_email:null,channel:'email',subject:''}]});
        if(body.args.operation==='propose_followup'){
          const review={kind:'new_email' as const,to:body.args.to ?? 'dana@example.com',subject:'',body:'**Exact follow-up** — no invented thread',source_revision:state.revision,review_hash:''};
          review.review_hash=gmailDraftReviewHash(review);
          return reply({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review,offer_id:body.args.offer_id});
        }
        if(body.args.operation==='propose_pair'){
          const review={kind:'reply_pair' as const,message_id:body.args.message_id,thread_id:'thread-1',to:'sender@example.test',subject:'Re: Report',reply_message_id:'<message@example.test>',source_revision:state.revision,accept_body:body.args.accept_body,decline_body:body.args.decline_body,first:body.args.first??'reply',reply_key:'1'.repeat(16),decline_key:'2'.repeat(16),review_hash:''};review.review_hash=gmailDraftReviewHash(review);const preview={version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review};receipts.set(body.action_id,preview);return reply(preview);
        }
        if(body.args.operation==='propose_new_send'){
          const review={kind:'new_send' as const,to:body.args.to,subject:body.args.subject,body:body.args.body,sender_email:'owner@example.test',source_revision:state.revision,review_hash:''};review.review_hash=gmailDraftReviewHash(review);
          return reply({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review});
        }
        const review = { message_id: body.args.operation === 'propose_offer' ? 'message-offer' : body.args.message_id, thread_id: 'thread-1', to: 'sender@example.test', subject: 'Re: Report', body: body.args.operation === 'propose_offer' ? '**Original offered body** — exact' : body.args.body, reply_message_id: '<message@example.test>', source_revision: state.revision, review_hash: '' };
        if(['propose_send','propose_send_typed'].includes(body.args.operation))Object.assign(review,{kind:'reply_send',sender_email:'owner@example.test',references:'<earlier@example.test>'});
        if(['propose_typed','propose_send_typed'].includes(body.args.operation)){
          const root=body.args.baseline?(receipts.get(body.args.baseline.action_id) as any).review.draft_origin:{action_type:body.args.action_type,baseline_action_id:body.action_id,baseline_key:'1'.repeat(16),baseline_body:body.args.body};
          Object.assign(review,{draft_origin:root});
        }
        review.review_hash = gmailDraftReviewHash(review);
        const preview={ version: 1, status: 'preview', event_id: body.event_id, payload_hash: body.payload_hash, review, ...(body.args.operation === 'propose_offer' ? {offer_id:'e'.repeat(64)} : {}) };
        if(['propose_typed','propose_send_typed'].includes(body.args.operation)){receipts.set(body.action_id,preview);if(loseCreate)throw new Error('typed preview response lost');}
        return reply(preview);
      }
      if(body.operation==='record_offer'){
        const preview=receipts.get(body.action_id) as any;const review=preview.review;const result={...preview,offer_receipt:{version:1,action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:stableActionIdentity(body.event_id,ownerGmailDraftKind,body.args).payloadHash,review_hash:review.review_hash,accepted_dispatch_id:body.accepted_delivery.accepted_dispatch_id,reply_key:review.reply_key,decline_key:review.decline_key}};receipts.set(body.action_id,result);if(loseCreate)throw Error('local recording response lost');return reply(result);
      }
      if (body.operation === 'inspect')
        return reply(!hideInspect && receipts.get(body.action_id) || { version: 1, status: 'not_found' });
      const binding = stableActionIdentity(body.event_id, ownerGmailDraftKind, body.args);
      const receipt = { version: 1, status: 'committed', action_id: body.action_id, event_id: body.event_id, payload_hash: body.payload_hash, args_hash: binding.payloadHash,
        ...('draft_origin' in body.args.review?{learning:'recorded'}:{}),outcome: body.args.operation==='send' ? {operation:'send',message_id:'sent-1',thread_id:body.args.review.thread_id??'returned-thread',review_hash:body.args.review.review_hash,status:'sent'} : { operation: 'save', draft_id: 'draft-1', thread_id: body.args.review.thread_id ?? 'new-thread', review_hash: body.args.review.review_hash, status: 'draft_saved' } };
      receipts.set(body.action_id, receipt);
      if (revokeOnApply) state = {...authority,revision:'e'.repeat(64)};
      if (loseCreate)
        throw new Error('lost after commit');
      return reply(receipt);
    } });
  const sourceAuthority = { read: async () => state };
  const tools = createOwnerGmailDraftTools({ client, journal: () => journal, sourceAuthority });
  const callTool = async (name: keyof typeof tools, input: unknown, request: ModelRequest, overrides: Record<string, unknown> = {}) => {
    const requestContext = new RequestContext();
    for (const [key, value] of Object.entries({ mode: 'chat', runId: request.runId, ownerRequestKey: request.ownerRequestKey,
      sourceRevision: state.revision, allowOwnerGmailDraft: request.allowOwnerGmailDraft, ...overrides }))
      requestContext.set(key, value);
    return tools[name].execute!(input as never, { requestContext, abortSignal: request.signal } as never);
  };
  return { get journal() { return journal; }, client, calls, tools, sourceAuthority, callTool,
    set previewRefusal(value: 'draft_permission_unavailable' | 'target_changed' | 'invalid_target') {previewRefusal=value;},
    set revokeOnApply(value: boolean) {revokeOnApply=value;},
    set loseCreate(value: boolean) { loseCreate = value; }, set hideInspect(value: boolean) { hideInspect = value; },
    revoke() { state = { ...authority, revision: 'e'.repeat(64) }; },
    reopen() { journal.close(); journal = new OperationalJournal(join(folder, 'operations.sqlite')); journal.recoverInterrupted(); } };
}
test('actual Mastra full draft review -> separate yes -> exact save; replay/reopen preserve receipt', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-')), f = fixture(folder), sent: string[] = [];
  let step = 0;
  const calls = [{ name: 'gmail_draft_propose', input: { message_id: args.message_id, body: args.body } }, null, { name: 'gmail_draft_save', input: {} }, null];
  const model = new MockLanguageModelV4({ doGenerate: async () => { const call = calls[step++]; return { content: call ? [{ type: 'tool-call' as const, toolCallId: `routine-${step}`, toolName: call.name, input: JSON.stringify(call.input) }] : [{ type: 'text' as const, text: 'model completion' }], finishReason: { unified: call ? 'tool-calls' : 'stop', raw: 'STOP' }, usage, warnings: [] }; } });
  const runtime = createSottoAgentRuntime({ model, storagePath: join(folder, 'memory.db'), instructions: 'Use owner routines', sourceAuthority: f.sourceAuthority, chatTools: async () => f.tools });
  const c = new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerGmailDraft: f.client, ownerMeetingPrep: forbiddenMeetingPrep, model: { generate: r => runtime.generateChat({ ...r, threadId: 'memory', ownerId: 'owner' }) }, postPrepared: async (r) => { assert.equal(r.verbatim, sent.length === 0); sent.push(r.text); return { acceptance: 'accepted', messageId: `ack-${sent.length}` }; }, canPostPrepared: r => runtime.canDeliverReplySource(r) });
  try {
    assert.equal((await c.receive(inbound('proposal', 'Every Friday at 4pm summarize my open loops'), context)).status, 'accepted');
    assert.ok(sent[0]!.includes('To: sender@example.test') && sent[0]!.includes(args.body));
    assert.match(sent[0]!, /Nothing will be sent/);
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 0);
    f.revokeOnApply=true; // Effect receipt survives the runtime's final source-revision rejection.
    assert.equal((await c.receive({...inbound('yes', 'Yes, please.'), replyBound: true, replyToMessageId: 'ack-1', providerSentAtMs: 946684800000}, context)).status, 'accepted');
    assert.equal(sent[1], 'Saved your Gmail draft. Nothing was sent.');
    const create = f.calls.find(x => x.operation === 'apply')!;
    assert.equal(create.text, 'Yes, please.');
    assert.equal((create.args as any).review.body, args.body);
    assert.notEqual((create.args as any).proposal.event_id, create.event_id);
    const actions = f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_gmail_draft_tool' ORDER BY id").all();
    await c.close();
    await runtime.close();
    f.journal.close();
    const reopened = new OperationalJournal(join(folder, 'operations.sqlite'));
    reopened.recoverInterrupted();
    const recovery = new ConversationCoordinator({ journal: reopened, ownerPolicy, ownerGmailDraft: f.client, model: { generate: async () => { throw new Error('replay'); } }, postPrepared: async () => { throw new Error('resend'); } });
    assert.deepEqual(await recovery.recover(), []);
    assert.equal((await recovery.receive({...inbound('yes', 'Yes, please.'), replyBound: true, replyToMessageId: 'ack-1', providerSentAtMs: 946684800000}, context)).status, 'duplicate');
    assert.deepEqual(reopened.db.prepare("SELECT * FROM actions WHERE kind='owner_gmail_draft_tool' ORDER BY id").all(), actions);
    reopened.close();
  }
  finally {
    await runtime.close();
    try {
      f.journal.close();
    }
    catch { }
    rmSync(folder, { recursive: true, force: true });
  }
});
test('unknown draft save recovers by saved inspection while safe chat proceeds, never saves again', async (t) => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-held-')), f = fixture(folder), sent: string[] = [];
  const make = () => new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerGmailDraft: f.client, model: { generate: async (r) => {
        if (r.text.startsWith('Every'))
          await f.callTool('gmail_draft_propose', { message_id: args.message_id, body: args.body }, r);
        else if (r.text === 'Yes.')
          await f.callTool('gmail_draft_save', {}, r);
        return { text: 'safe chat', sourceRevision: authority.revision };
      } }, postPrepared: async (r) => { sent.push(r.text); return { acceptance: 'accepted', messageId: `ack-${sent.length}` }; } });
  let c = make();
  try {
    await c.receive(inbound('proposal', 'Every Friday at4 summarize my loops'), context);
    f.loseCreate = true;
    f.hideInspect = true;
    assert.equal((await c.receive(inbound('yes', 'Yes.'), context)).status, 'held');
    await c.close();
    f.journal.recoverInterrupted();
    c = make();
    await c.receive(inbound('safe', 'Tell me a joke'), context);
    // The pending-effect notice arrived before restart; safe chat does not repeat it.
    assert.equal(sent.at(-1), 'safe chat');
    assert.match(sent.at(-2)!, /^I couldn’t confirm whether the Gmail action happened\./);
    assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the Gmail action happened.')).length,1);
    f.hideInspect = false;
    const later = Date.now() + 61_000;
    t.mock.method(Date, 'now', () => later);
    await c.recover();
    assert.equal(sent.at(-1), 'Saved your Gmail draft. Nothing was sent.');
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 1);
    assert.ok(f.calls.filter(x => x.operation === 'inspect').length > 0);
  }
  finally {
    await c.close();
    f.journal.close();
    rmSync(folder, { recursive: true, force: true });
  }
});
test('confirmation rejects tweaks, intervening owner input, rotations, stale source and early yes; all remain effect-free', async () => {
  for (const scenario of ['ambiguous', 'intervening', 'new', 'stop', 'stale', 'unknown-send', 'wrong-reply', 'missing-reply-id'] as const) {
    const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-confirm-')), f = fixture(folder);
    let rejection = 0;
    const c = new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerGmailDraft: f.client, ownerMeetingPrep: scenario === 'stale' ? forbiddenMeetingPrep : undefined,
      model: { generate: async (r) => {
          if (r.text.startsWith('Every'))
            await f.callTool('gmail_draft_propose', { message_id: args.message_id, body: args.body }, r);
          else if (r.text.startsWith('Yes')) {
            await assert.rejects(f.callTool('gmail_draft_save', {}, r));
            rejection++;
          }
          return { text: 'Please clarify.', sourceRevision: (await f.sourceAuthority.read()).revision };
        } },
      rotateSession: async () => { }, postPrepared: async () => scenario === 'unknown-send' && f.calls.length === 1 ? { acceptance: 'unknown' } : { acceptance: 'accepted', messageId: 'ack' } });
    try {
      await c.receive(inbound('proposal', 'Every Friday summarize my loops'), context);
      if (scenario === 'intervening')
        await c.receive(inbound('other', 'Tell me a joke'), context);
      if (scenario === 'new')
        await c.receive(inbound('rotate', '/new'), context);
      if (scenario === 'stop')
        await c.receive(inbound('stop', '/stop'), context);
      if (scenario === 'stale')
        f.revoke();
      await c.receive({...inbound('yes', scenario === 'ambiguous' ? 'Yes, but make it five.' : 'Yes.'),
        ...(scenario === 'stale' ? {replyBound: true, replyToMessageId: 'ack'} : {}),
        ...(scenario === 'wrong-reply' ? {replyBound: true, replyToMessageId: 'other-question'} : {}),
        ...(scenario === 'missing-reply-id' ? {replyBound: true} : {}),
      }, context);
      assert.equal(rejection, 1, scenario);
      assert.equal(f.calls.filter(x => x.operation === 'apply').length, 0, scenario);
    }
    finally {
      await c.close();
      f.journal.close();
      rmSync(folder, { recursive: true, force: true });
    }
  }
});
test('draft authority and composite mutation fences reject before any writer contact', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-routine-fence-')), f = fixture(folder);
  const c = new ConversationCoordinator({ journal: f.journal, ownerPolicy, ownerGmailDraft: f.client, model: { generate: async (r) => {
        for (const overrides of [{ mode: 'unattended' }, { runId: 'spoof' }, { ownerRequestKey: 'spoof' }, { allowOwnerGmailDraft: false }, { sourceRevision: 'e'.repeat(64) }])
          await assert.rejects(f.callTool('gmail_draft_propose', { message_id: args.message_id, body: args.body }, r, overrides));
        assert.equal(f.calls.length, 0);
        await f.callTool('gmail_draft_propose', { message_id: args.message_id, body: args.body }, r);
        const authority = { runId: r.runId, accountId: 'account', ownerId: 'owner', lineKey: 'line', channel: 'photon' as const, scopeHash: f.client.scopeHash };
        assert.throws(() => f.journal.prepareOwnerPreferenceToolAction(authority, r.ownerRequestKey, { operation: 'clear-tone' }));
        assert.throws(() => f.journal.prepareOwnerTaskToolAction(authority, r.ownerRequestKey, { operation: 'add', text: 'Friday' }));
        assert.throws(() => f.journal.prepareOwnerReminderToolAction(authority, r.ownerRequestKey, { due: '2026-10-09T08:15:00Z', action: 'Friday' }));
        return { text: 'unused', sourceRevision: authority.scopeHash };
      } }, postPrepared: async () => ({ acceptance: 'accepted', messageId: 'ack' }) });
  try {
    await c.receive(inbound('proposal', 'Every Friday summarize loops'), context);
    assert.equal(f.calls.filter(x => x.operation === 'apply').length, 0);
  }
  finally {
    await c.close();
    f.journal.close();
    rmSync(folder, { recursive: true, force: true });
  }
});


test('receiver preview refusal records a factual refusal without a question or create contact', async () => {
  for (const code of ['draft_permission_unavailable','target_changed','invalid_target'] as const) {
    const folder = mkdtempSync(join(tmpdir(),'sotto-routine-refusal-'));
    const f = fixture(folder);
    const sent: string[] = [];
    f.previewRefusal = code;
    const coordinator = new ConversationCoordinator({
      journal: f.journal, ownerPolicy, ownerGmailDraft: f.client,
      model: {generate: async request => {
        assert.deepEqual(await f.callTool('gmail_draft_propose',{message_id:args.message_id,body:args.body},request), {status:'rejected',code});
        return 'Should I create this routine?';
      }},
      postPrepared: async request => {sent.push(request.text);return {acceptance:'accepted',messageId:'refused'};},
    });
    try {
      assert.equal((await coordinator.receive(inbound('proposal','Every Friday summarize my loops'),context)).status,'accepted');
      assert.ok(!sent[0]!.includes('Should I create'));
      assert.equal(f.calls.filter(row=>row.operation==='apply').length,0);
      const action=f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_gmail_draft_tool'").get()!;
      assert.equal(action.status,'rejected');
    } finally {await coordinator.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
  }
});

test('full review refuses oversize text and spoofed save arguments before write; fences work in both directions', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-draft-bounds-')),f=fixture(folder);
  const sent:string[]=[];
  const coordinator=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,
    model:{generate:async request=>{
      if(request.text==='Oversize'){
        await assert.rejects(f.callTool('gmail_draft_propose',{message_id:args.message_id,body:'a'.repeat(4000)},request));
        assert.equal(f.journal.db.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_gmail_draft_tool'").get()!.n,0);
      }else{
        const authority={runId:request.runId,accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon' as const,scopeHash:f.client.scopeHash};
        f.journal.prepareOwnerTaskToolAction(authority,request.ownerRequestKey,{operation:'add',text:'Task'});
        await assert.rejects(f.callTool('gmail_draft_propose',{message_id:args.message_id,body:args.body},request));
        await assert.rejects(f.callTool('gmail_draft_save',{review:'forged',approved:true},request));
      }
      return 'Please clarify.';
    }},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'bounds'};}});
  try{
    await coordinator.receive(inbound('big','Oversize'),context);
    await coordinator.receive(inbound('task','Task'),context);
    assert.equal(f.calls.filter(row=>row.operation==='apply').length,0);
    assert.ok(sent.every(text=>!text.includes('Save as a Gmail draft?')));
  }finally{await coordinator.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

for (const stale of [false, true]) test(`exact no to local Gmail review keeps local precedence, stale=${stale}`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-local-offer-priority-')), f = fixture(folder);
  const replies: string[] = [];
  const sourceOffer = createOwnerOfferReplyClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64), identity,
    fetch: async () => {assert.fail('Local proposal cannot be consumed as a source-loop offer');}});
  const coordinator = new ConversationCoordinator({journal: f.journal, ownerPolicy, ownerGmailDraft: f.client,
    ownerOfferReply: sourceOffer, model: {generate: async request => {
      if (request.text === 'Draft a reply to the report') await f.callTool('gmail_draft_propose', {message_id: args.message_id, body: args.body}, request);
      else assert.equal(request.text, 'No.');
      return {text: 'I will not create it.', sourceRevision: (await f.sourceAuthority.read()).revision};
    }}, postPrepared: async request => {replies.push(request.text); return {acceptance: 'accepted', messageId: `local-${replies.length}`};}});
  try {
    await coordinator.receive(inbound('local-proposal', 'Draft a reply to the report'), context);
    if (stale) f.revoke();
    await coordinator.receive({...inbound('local-no', 'No.'), replyBound: true, replyToMessageId: 'local-1'}, context);
    assert.equal(replies.at(-1), 'I will not create it.');
    assert.equal(f.journal.db.prepare("SELECT count(*) AS n FROM actions WHERE kind='owner_offer_reply'").get()!.n, 0);
    assert.equal(f.calls.filter(call => call.operation === 'apply').length, 0);
  } finally {await coordinator.close(); f.journal.close(); rmSync(folder, {recursive: true, force: true});}
});

 test('reopened prepared approval review retains verbatim delivery exemption', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-verbatim-')), f = fixture(folder);
  let step = 0;
  const model = new MockLanguageModelV4({doGenerate: async () => ({content: step++ === 0
    ? [{type:'tool-call' as const, toolCallId:'review',toolName:'gmail_draft_propose',input:JSON.stringify({message_id: args.message_id, body: '**Exact body** — preserve'})}]
    : [{type:'text' as const,text:'finished'}],finishReason:{unified:step === 1 ? 'tool-calls' : 'stop',raw:'STOP'},usage,warnings:[]})});
  const runtime = createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Review',sourceAuthority:f.sourceAuthority,chatTools:async()=>f.tools});
  const c = new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:r=>runtime.generateChat({...r,threadId:'memory',ownerId:'owner'})},canPostPrepared:async()=>false,postPrepared:async()=>{throw Error('blocked');}});
  try {
    assert.equal((await c.receive(inbound('review','Prepare my review'),context)).status,'held');
    await c.close(); await runtime.close(); f.journal.close();
    const reopened = new OperationalJournal(join(folder,'operations.sqlite')); reopened.recoverInterrupted();
    let delivered = false;
    const recovery = new ConversationCoordinator({journal:reopened,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async()=>{throw Error('no model');}},canPostPrepared:async()=>true,postPrepared:async r=>{
      assert.equal(r.verbatim,true); assert.match(r.text,/\*\*Exact/); assert.match(r.text,/— preserve/); delivered=true; return {acceptance:'accepted',messageId:'review-message'};
    }});
    await recovery.recover(); assert.equal(delivered,true); await recovery.close(); reopened.close();
  } finally {await runtime.close(); try {f.journal.close();} catch {} rmSync(folder,{recursive:true,force:true});}
});

test('empty-input accepted offer review retains original selector through separate save and local precedence', async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-offer-review-')),f=fixture(folder),sent:string[]=[];
  let step=0;
  const calls=[{name:'gmail_draft_offer',input:{}},null,{name:'gmail_draft_save',input:{}},null];
  const model=new MockLanguageModelV4({doGenerate:async()=>{const call=calls[step++];return {content:call?[{type:'tool-call' as const,toolCallId:`offer-${step}`,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text' as const,text:'finished'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};}});
  const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Use draft review',sourceAuthority:f.sourceAuthority,chatTools:async()=>f.tools});
  const noPrep=createOwnerMeetingPrepClient({bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),identity,fetch:async()=>Response.json({version:1,status:'no_match'})});
  const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,ownerMeetingPrep:noPrep,
    model:{generate:r=>runtime.generateChat({...r,threadId:'memory',ownerId:'owner'})},postPrepared:async r=>{sent.push(r.text);return {acceptance:'accepted',messageId:`review-${sent.length}`};}});
  try{
    const first=await c.receive({...inbound('offer-yes','Yes'),replyBound:true,replyToMessageId:'accepted-offer'},context);
    assert.ok(['accepted','silent'].includes(first.status));
    assert.match(sent[0]!,/\*\*Original offered body\*\* — exact/);
    assert.equal(f.calls.filter(c=>c.operation==='apply').length,0);
    assert.equal((await c.receive({...inbound('save-yes','Yes'),replyBound:true,replyToMessageId:'review-1'},context)).status,'accepted');
    const save=f.calls.find(c=>c.operation==='apply')!;
    const origin=(save.args as any).proposal.origin;
    assert.equal(origin.offer_id,'e'.repeat(64));assert.equal(origin.selection.reply_to_message_id,'accepted-offer');
    assert.equal(origin.selection.provider_message_id,'offer-yes');
    assert.equal(sent[1],'Saved your Gmail draft. Nothing was sent.');
    const ctx=new RequestContext(new Map(Object.entries({mode:'chat',runId:'untrusted',ownerRequestKey:'unknown',allowOwnerGmailDraft:true})));
    const before=f.calls.length;
    await f.tools.gmail_draft_offer.execute!({reply_to_message_id:'fabricated'} as never,{requestContext:ctx} as never);
    assert.equal(f.calls.length,before,'invalid model selector never contacts receiver');
  }finally{await c.close();await runtime.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});


test('followup list is effect-free, new-email review survives reopen and separate save uses honest origin', async () => {
  const folder=mkdtempSync(join(tmpdir(),'sotto-followup-draft-')),f=fixture(folder),sent:string[]=[];
  let coordinator:ConversationCoordinator;
  const make=()=>new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,
    model:{generate:async request=>{
      if(request.text==='Hello')return 'Safe hello';
      if(request.text==='Yes'){await f.callTool('gmail_draft_save',{},request);return 'Saved';}
      const listed=await f.callTool('gmail_draft_followup_list',{},request);
      assert.equal((listed as {status:string}).status,'offers');
      assert.equal(f.journal.db.prepare('SELECT COUNT(*) AS n FROM actions').get()!.n,0);
      await assert.rejects(f.callTool('gmail_draft_followup_review',{offer_id:'f'.repeat(32),to:'invented@example.com'},request));
      assert.equal(f.journal.db.prepare('SELECT COUNT(*) AS n FROM actions').get()!.n,0);
      await f.callTool('gmail_draft_followup_review',{offer_id:'f'.repeat(32),to:'dana@example.com'},request);
      return 'Review';
    }},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'followup-review-'+sent.length};}});
  try{
    coordinator=make();
    await coordinator.receive(inbound('proposal','Use dana@example.com for the follow-up'),context);
    assert.ok(sent[0]!.startsWith('To: dana@example.com\nSubject: \n\n**Exact follow-up** — no invented thread'));
    await coordinator.close();f.reopen();coordinator=make();
    f.loseCreate=true;f.hideInspect=true;
    await coordinator.receive(inbound('approval','Yes'),context);
    const applied=f.calls.filter(row=>row.operation==='apply');assert.equal(applied.length,1);
    const save=applied[0]!.args as {review:Record<string,unknown>;proposal:{origin:unknown}};
    assert.equal(save.review.kind,'new_email');assert.equal(save.review.message_id,undefined);
    assert.deepEqual(save.proposal.origin,{kind:'followup',offer_id:'f'.repeat(32),to:'dana@example.com'});
    await coordinator.receive(inbound('hello','Hello'),context);assert.ok(sent.includes('Safe hello'));
    await coordinator.close();f.reopen();f.hideInspect=false;coordinator=make();
    await coordinator.recover();
    assert.equal(f.calls.filter(row=>row.operation==='apply').length,1);
    assert.ok(sent.some(text=>text.includes('draft')));
  }finally{await coordinator!.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('new-email preview must preserve the exact owner-supplied recipient',()=>{
 const review={kind:'new_email' as const,to:'different@example.com',subject:'',body:'Exact body',source_revision:'d'.repeat(64),review_hash:''};
 review.review_hash=gmailDraftReviewHash(review);
 const preview={version:1 as const,status:'preview' as const,event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),offer_id:'f'.repeat(32),review};
 assert.equal(gmailDraftPreviewMatches(preview,{operation:'propose_followup',offer_id:'f'.repeat(32),to:'owner-supplied@example.com'}),false);
 assert.equal(gmailDraftPreviewMatches(preview,{operation:'propose_followup',offer_id:'f'.repeat(32),to:review.to}),true);
});

for(const operation of ['save','send'] as const) test(`accepted ${operation} review cannot authorize the other confirmation wrapper`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'gmail-wrapper-')),f=fixture(folder);let refused=0;
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async r=>{
  if(r.text==='Review')await f.callTool(operation==='save'?'gmail_draft_propose':'gmail_send_review',{message_id:args.message_id,body:args.body},r);
  else if(r.text==='Yes.') {await assert.rejects(f.callTool(operation==='save'?'gmail_send_confirm':'gmail_draft_save',{},r));refused++;}
  return {text:'Please clarify.',sourceRevision:authority.revision};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'review'})});
 try{await c.receive(inbound('review','Review'),context);await c.receive(inbound('yes','Yes.'),context);assert.equal(refused,1);assert.equal(f.calls.filter(x=>x.operation==='apply').length,0);}finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
for (const recovery of ['receipt', 'restart', 'stop'] as const) test(`send-specific accepted review commits exact receipt; lost response ${recovery} never sends twice`,async(t)=>{
 const folder=mkdtempSync(join(tmpdir(),'gmail-send-recover-')),f=fixture(folder),sent:string[]=[];
 const make=()=>new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async r=>{
  if(r.text==='Send a reply')await f.callTool('gmail_send_review',{message_id:args.message_id,body:args.body},r);
  else if(r.text==='Yes.')await f.callTool('gmail_send_confirm',{},r);
  return {text:'Safe unrelated answer.',sourceRevision:authority.revision};
 }},postPrepared:async r=>{sent.push(r.text);return {acceptance:'accepted',messageId:'review-'+sent.length};}});
 let c=make();try{await c.receive(inbound('review','Send a reply'),context);assert.equal(sent[0],`From: owner@example.test\nTo: sender@example.test\nSubject: Re: Report\n\n${args.body}\n\nSend this email reply now?`);assert.equal(f.calls.filter(x=>x.operation==='apply').length,0);
 f.loseCreate=true;f.hideInspect=true;assert.equal((await c.receive(inbound('yes','Yes.'),context)).status,'held');await c.close();f.reopen();c=make();await c.receive(inbound('safe','Hello'),context);assert.equal(sent.at(-1),'Safe unrelated answer.');assert.match(sent.at(-2)!,/^I couldn’t confirm whether the Gmail action happened\./);assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the Gmail action happened.')).length,1);f.hideInspect=false;
 if(recovery==='restart'){await c.close();f.reopen();c=make();}
 if(recovery==='stop'){await c.receive(inbound('stop','/stop'),context);await c.drain();}
 const later=Date.now()+61_000;t.mock.method(Date,'now',()=>later);
 await c.recover();
 if(recovery==='stop'){
  assert.ok(sent.includes('Safe unrelated answer.'));
  assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the Gmail action happened.')).length,1);
  assert.equal(sent.filter(text=>text.startsWith('I stopped working on your earlier request.')).length,1);
  assert.equal(sent.filter(text=>text==='Email sent.').length,0,'a stopped owner request gets no late Gmail ACK');
 }else assert.equal(sent.at(-1),'Email sent.');
 const confirmed=sent.length;await c.recover();assert.equal(sent.length,confirmed,'late confirmation is sent once');
 assert.equal(f.calls.filter(x=>x.operation==='apply').length,1);
 }finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
for (const scenario of ['undelivered','wrong-reply','intervening','stale','cross-kind'] as const) test(`send approval ${scenario} stays effect-free`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'gmail-send-fence-')),f=fixture(folder);let refusals=0;
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async r=>{
  if(r.text==='Review send')await f.callTool('gmail_send_review',{message_id:args.message_id,body:args.body},r);
  else if(r.text==='Yes.'){
   if(scenario==='cross-kind'){
    const authorityContext={runId:r.runId!,accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon' as const,scopeHash:f.client.scopeHash};
    f.journal.prepareOwnerTaskToolAction(authorityContext,r.ownerRequestKey!,{operation:'add',text:'Yes.'});
   }
   await assert.rejects(f.callTool('gmail_send_confirm',{},r));refusals++;
  }
  return {text:'Please clarify.',sourceRevision:(await f.sourceAuthority.read()).revision};
 }},postPrepared:async r=>scenario==='undelivered'&&r.text.includes('Send this email')?{acceptance:'rejected'}:{acceptance:'accepted',messageId:'send-review'}});
 try{await c.receive(inbound('review','Review send'),context);if(scenario==='intervening')await c.receive(inbound('other','Hello'),context);if(scenario==='stale')f.revoke();
 await c.receive({...inbound('yes','Yes.'),...(scenario==='wrong-reply'?{replyBound:true,replyToMessageId:'another-review'}:{})},context);
 assert.equal(refusals,scenario==='undelivered'?0:1);assert.equal(f.calls.filter(x=>x.operation==='apply').length,0);
 }finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('legacy reply draft hash/render remains unchanged',()=>{
 const review={message_id:args.message_id,thread_id:'thread-1',to:'sender@example.test',subject:'Re: Report',body:args.body,reply_message_id:'<message@example.test>',source_revision:authority.revision,review_hash:''};
 assert.equal(gmailDraftReviewHash(review),'d4ff3a5a22e301e27143bf42476422b2b82627a0879ece194b434c1a059dfbb2');
});
test('close during committed send acknowledgment preflight prevents late provider reply',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'gmail-send-close-')),f=fixture(folder);let ackStarted!:()=>void,release!:()=>void,contacts=0;
 const started=new Promise<void>(resolve=>{ackStarted=resolve;}),gate=new Promise<boolean>(resolve=>{release=()=>resolve(true);});
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async r=>{
  if(r.text==='Review')await f.callTool('gmail_send_review',{message_id:args.message_id,body:args.body},r);else await f.callTool('gmail_send_confirm',{},r);
  return {text:'ignored',sourceRevision:authority.revision};
 }},canPostPrepared:async r=>{if(r==='owner-only-context-v1'){ackStarted();return gate;}return true;},postPrepared:async()=>{contacts++;return {acceptance:'accepted',messageId:'review'};}});
 try{await c.receive(inbound('review','Review'),context);const work=c.receive(inbound('yes','Yes.'),context);await started;const closing=c.close();release();await closing;await work;assert.equal(contacts,1);assert.equal(f.calls.filter(x=>x.operation==='apply').length,1);assert.ok(f.journal.db.prepare("SELECT id FROM actions WHERE status='succeeded' AND json_extract(result_json,'$.args.operation')='send'").get());}finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
for(const text of ['Yes, send it.','Send this reply.','Do not send it.','Send it to bob@example.com','Yes, send it but change the body'])test(`send review confirmation literal ${text}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'gmail-send-words-')),f=fixture(folder);const accepted=['Yes, send it.','Send this reply.'].includes(text);
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async r=>{
  if(r.text==='Review')await f.callTool('gmail_send_review',{message_id:args.message_id,body:args.body},r);else if(accepted)await f.callTool('gmail_send_confirm',{},r);else await assert.rejects(f.callTool('gmail_send_confirm',{},r));return {text:'Please clarify.',sourceRevision:authority.revision};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'review'})});
 try{await c.receive(inbound('review','Review'),context);await c.receive(inbound('confirmation',text),context);assert.equal(f.calls.filter(x=>x.operation==='apply').length,accepted?1:0);}finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('explicit send words never authorize a saved-draft review',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'gmail-save-words-')),f=fixture(folder);let denied=0;
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async r=>{if(r.text==='Review')await f.callTool('gmail_draft_propose',{message_id:args.message_id,body:args.body},r);else{await assert.rejects(f.callTool('gmail_draft_save',{},r));await assert.rejects(f.callTool('gmail_send_confirm',{},r));denied++;}return {text:'Please clarify.',sourceRevision:authority.revision};}},postPrepared:async()=>({acceptance:'accepted',messageId:'review'})});
 try{await c.receive(inbound('review','Review'),context);await c.receive(inbound('confirmation','Yes, send it.'),context);assert.equal(denied,1);assert.equal(f.calls.filter(x=>x.operation==='apply').length,0);}finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
for(const held of [false,true])test(`saved send ${held?'unknown':'committed'} precedes fresh authority and never rebinds`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'gmail-send-saved-')),f=fixture(folder);let repeated=false;
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async r=>{
  if(r.text==='Review')await f.callTool('gmail_send_review',{message_id:args.message_id,body:args.body},r);
  else {
   if(held){f.loseCreate=true;f.hideInspect=true;await assert.rejects(f.callTool('gmail_send_confirm',{},r));}else await f.callTool('gmail_send_confirm',{},r);
   f.revoke();if(held)await assert.rejects(f.callTool('gmail_send_confirm',{},r),error=>error instanceof ActionHeldError);else assert.equal((await f.callTool('gmail_send_confirm',{},r) as {status:string}).status,'recorded');repeated=true;
  }
  return {text:'ignored',sourceRevision:authority.revision};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'review'})});
 try{await c.receive(inbound('review','Review'),context);await c.receive(inbound('yes','Yes.'),context);assert.ok(repeated);assert.equal(f.calls.filter(x=>x.operation==='apply').length,1);const row=f.journal.db.prepare("SELECT result_json FROM actions WHERE json_extract(result_json,'$.args.operation')='send'").get()!;assert.equal(JSON.parse(String(row.result_json)).args.review.source_revision,authority.revision);}finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('typed choice edit keeps accepted root and rejects target/type/proof substitution; old save/send stay unchanged',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'typed-draft-binding-')),f=fixture(folder);let phase='root';const sent:string[]=[];
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async request=>{
  if(phase==='root')await f.callTool('gmail_draft_propose',{message_id:'message-1',body:'Original decline.',action_type:'decline'},request);
  else{
   const before=f.calls.length;
   await assert.rejects(f.callTool('gmail_send_review',{message_id:'message-other',body:'Changed body.',action_type:'decline'},request));
   await assert.rejects(f.callTool('gmail_send_review',{message_id:'message-1',body:'Changed body.',action_type:'reply'},request));
   assert.equal(f.calls.length,before,'wrong target/type never contacts receiver');
   await f.callTool('gmail_send_review',{message_id:'message-1',body:'Changed body.',action_type:'decline'},request);
  }
  return {text:'discarded'};
 }},postPrepared:async reply=>{sent.push(reply.text);return {acceptance:'accepted',messageId:'typed-'+sent.length};},canPostPrepared:async()=>true});
 try{
  assert.equal((await c.receive(inbound('root','Use the Decline option.'),context)).status,'accepted');assert.match(sent[0]!,/^Draft type: Decline/);
  phase='edit';assert.equal((await c.receive(inbound('edit','Change it to this declined reply.'),context)).status,'accepted');assert.match(sent[1]!,/^Draft type: Decline\nEdited draft/);
  const actions=f.journal.db.prepare("SELECT * FROM actions WHERE kind='owner_gmail_draft_tool' ORDER BY created_at,id").all();
  const root=JSON.parse(String(actions[0]!.result_json));const edit=JSON.parse(String(actions[1]!.result_json));
  assert.equal(edit.args.baseline.action_id,actions[0]!.id);assert.deepEqual(edit.preview.review.draft_origin,root.preview.review.draft_origin);
  const journalAuthority={runId:actions[1]!.run_id as string,accountId:'account',ownerId:'owner',channel:'photon' as const,lineKey:'line',scopeHash:f.client.scopeHash};
  const forged=JSON.parse(JSON.stringify(edit));forged.preview.review.draft_origin.baseline_body='Replaced original';forged.preview.review.review_hash=gmailDraftReviewHash(forged.preview.review);
  f.journal.db.prepare('UPDATE actions SET result_json=? WHERE id=?').run(JSON.stringify(forged),actions[1]!.id as string);
  assert.throws(()=>f.journal.readOwnerGmailDraftAction(actions[1]!.id as string,journalAuthority),/root binding/);
 }finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('typed preview response loss is inspect-only, survives restart and delivers exact original review',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'typed-preview-loss-')),f=fixture(folder);const sent:string[]=[];let modelCalls=0;
 let c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async request=>{modelCalls++;await f.callTool('gmail_draft_propose',{message_id:'message-1',body:'Decline this invitation.',action_type:'decline'},request);return {text:'ignored'};}},postPrepared:async reply=>{sent.push(reply.text);return {acceptance:'accepted',messageId:'typed-review'};},canPostPrepared:async()=>true});
 try{
  f.loseCreate=true;f.hideInspect=true;assert.equal((await c.receive(inbound('typed','Use Decline.'),context)).status,'held');assert.equal(sent.length,1);assert.match(sent[0]!,/^I couldn’t confirm whether the Gmail action happened\./);
  assert.equal(f.journal.db.prepare("SELECT status FROM actions WHERE kind='owner_gmail_draft_tool'").get()!.status,'unknown');
  await c.close();f.reopen();f.hideInspect=false;f.loseCreate=false;
  f.journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE status='unknown'").run();
  c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async()=>{assert.fail('typed replay must not regenerate');}},postPrepared:async reply=>{sent.push(reply.text);return {acceptance:'accepted',messageId:'typed-review'};},canPostPrepared:async()=>true});
  await c.recover();assert.equal(sent.length,2);assert.match(sent[1]!,/^Draft type: Decline/);
  await c.recover();assert.equal(sent.length,2,'the accepted original review is sent once');assert.equal(modelCalls,1);assert.equal(f.calls.filter(call=>call.operation==='preview').length,1);
 }finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('typed inspect accepts bounded Unicode baseline larger than legacy receipt ceiling',async()=>{
 const source_revision='d'.repeat(64),eventId='a'.repeat(64),payloadHash='b'.repeat(64);
 const typedArgs={operation:'propose_typed' as const,message_id:'message-1',body:'界'.repeat(800),action_type:'decline' as const};const binding=stableActionIdentity(eventId,ownerGmailDraftKind,typedArgs);
 const review={message_id:'message-1',thread_id:'thread-1',to:'alice@example.test',subject:'Re: Review',body:typedArgs.body,reply_message_id:'<source@example.test>',source_revision,review_hash:'',draft_origin:{action_type:'decline' as const,baseline_action_id:binding.actionId,baseline_key:'1'.repeat(16),baseline_body:typedArgs.body}};review.review_hash=gmailDraftReviewHash(review);
 const preview={version:1,status:'preview',event_id:eventId,payload_hash:payloadHash,review};
 const escaped=JSON.stringify(preview).replace(/界/g,'\\u754c');assert.ok(Buffer.byteLength(escaped)>8192);
 const client=createOwnerGmailDraftClient({bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),identity,fetch:async()=>new Response(escaped)});
 const result=await client.call('inspect',{actionId:binding.actionId,eventId,payloadHash,text:'Use Decline',admittedAtMs:Date.now(),providerThreadId:'thread',args:typedArgs});assert.equal(result.status,'preview');
});

test('saved typed preview is recovered before fresh authority; cancelled current run cannot reuse it',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'typed-known-source-')),f=fixture(folder);let requests=0;
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async request=>{
  requests++;const input={message_id:'message-1',body:'Original selected decline.',action_type:'decline'};
  await f.callTool('gmail_draft_propose',input,request);
  f.revoke();const before=f.calls.length;assert.equal((await f.callTool('gmail_draft_propose',input,request) as any).status,'proposed');assert.equal(f.calls.length,before);
  f.journal.cancelRun(request.runId);await assert.rejects(f.callTool('gmail_draft_propose',input,request));assert.equal(f.calls.length,before);
  return {text:'cancelled'};
 }},postPrepared:async()=>{assert.fail('cancelled turn cannot post review');},canPostPrepared:async()=>true});
 try{await c.receive(inbound('typed-source','Use Decline.'),context);assert.equal(requests,1);assert.equal(f.calls.filter(call=>call.operation==='preview').length,1);}finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('learning marker is required only for typed committed receipts',()=>{
 const origin={action_type:'decline' as const,baseline_action_id:'a'.repeat(64),baseline_key:'1'.repeat(16),baseline_body:'Original decline.'};
 const review={message_id:'message-1',thread_id:'thread-1',to:'alice@example.test',subject:'Re: Review',body:'Original decline.',reply_message_id:'<source@example.test>',source_revision:'d'.repeat(64),review_hash:'b'.repeat(64)};
 const proposal={event_id:'a'.repeat(64),action_id:'a'.repeat(64),args_hash:'a'.repeat(64),accepted_dispatch_id:'dispatch',source_revision:'d'.repeat(64)};
 const receipt={version:1 as const,status:'committed' as const,action_id:'a'.repeat(64),event_id:'a'.repeat(64),payload_hash:'a'.repeat(64),args_hash:'a'.repeat(64),outcome:{operation:'save' as const,draft_id:'draft-1',thread_id:'thread-1',review_hash:review.review_hash,status:'draft_saved' as const}};
 assert.equal(gmailDraftReceiptMatches(receipt,{operation:'save',review,proposal}),true);
 assert.equal(gmailDraftReceiptMatches(receipt,{operation:'save',review:{...review,draft_origin:origin},proposal}),false);
 assert.equal(gmailDraftReceiptMatches({...receipt,learning:'pending'},{operation:'save',review:{...review,draft_origin:origin},proposal}),true);
 assert.equal(gmailDraftReceiptMatches({...receipt,learning:'pending'},{operation:'save',review,proposal}),false);
});

test('fresh email closed inputs, exact full review and returned thread receipt preserve old send isolation',()=>{
 const args={operation:'propose_new_send' as const,to:'alice@example.com',subject:'',body:'Hello Alice.'};
 for(const to of ['Alice <alice@example.com>','a@example.com;b@example.com','a@example.com\u0085'])assert.equal(gmailNewSendProposalSchema.safeParse({...args,to}).success,false);
 assert.equal(gmailNewSendProposalSchema.safeParse({...args,thread_id:'invented'}).success,false);
 const review={kind:'new_send' as const,to:args.to,subject:args.subject,body:args.body,sender_email:'owner@example.test',source_revision:authority.revision,review_hash:''};review.review_hash=gmailDraftReviewHash(review);
 const preview={version:1 as const,status:'preview' as const,event_id:authority.revision,payload_hash:authority.revision,review};assert.ok(gmailDraftPreviewMatches(preview,args));assert.equal(gmailDraftPreviewMatches(preview,{...args,to:'other@example.com'}),false);assert.match(gmailDraftReviewText(preview),/From: owner@example.test/);
 const send={operation:'send' as const,review,proposal:{event_id:authority.revision,action_id:authority.revision,args_hash:authority.revision,accepted_dispatch_id:'review',source_revision:authority.revision}};
 const receipt={version:1 as const,status:'committed' as const,action_id:authority.revision,event_id:authority.revision,payload_hash:authority.revision,args_hash:authority.revision,outcome:{operation:'send' as const,message_id:'sent',thread_id:'returned-existing-thread',review_hash:review.review_hash,status:'sent' as const}};
 assert.ok(gmailDraftReceiptMatches(receipt,send));assert.equal(gmailDraftReceiptMatches({...receipt,outcome:{...receipt.outcome,review_hash:'b'.repeat(64)}},send),false);
});
test('fresh accepted send review refuses save wrapper then permits distinct send yes once',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'gmail-new-send-')),f=fixture(folder);let refused=0;
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,model:{generate:async r=>{if(r.text==='Review')await f.callTool('gmail_new_send_review',{to:'alice@example.com',subject:'Cedar',body:args.body},r);else{await assert.rejects(f.callTool('gmail_draft_save',{},r));refused++;await f.callTool('gmail_send_confirm',{},r);}return {text:'Completed.',sourceRevision:authority.revision};}},postPrepared:async()=>({acceptance:'accepted',messageId:'review'})});
 try{await c.receive(inbound('review','Review'),context);assert.equal(f.calls.filter(x=>x.operation==='apply').length,0);await c.receive(inbound('yes','Yes, send it.'),context);assert.equal(refused,1);assert.equal(f.calls.filter(x=>x.operation==='apply').length,1);}finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}

});

test('offered pair exact accepted text records local keys, rejects bare yes and recovers without redelivery',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'gmail-pair-')),f=fixture(folder),sent:string[]=[];
 const pair={message_id:'message-1',accept_body:'Yes, happy to review it.',decline_body:'Thanks for asking. I will pass.',first:'decline' as const};
 let replay=false;
 const coordinator=()=>new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,canPostPrepared:async()=>true,model:{generate:async request=>{if(replay){if(request.text==='Yes.')await assert.rejects(f.callTool('gmail_draft_save',{},request),/Choose Accept or Decline/);return 'Hello';}const first=await f.callTool('gmail_draft_pair',pair,request) as any;assert.equal(first.needs_owner_confirmation,true);assert.match(first.review_text,/Decline:\nThanks/);const again=await f.callTool('gmail_draft_pair',pair,request) as any;assert.equal(again.review_text,first.review_text);assert.equal(again.instruction,first.instruction);assert.match(first.instruction,/choose Accept or Decline/);return first.review_text;}},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'pair-accepted'};}});
 let c=coordinator();
 try{
  f.loseCreate=true;assert.equal((await c.receive(inbound('pair','Help me reply to this request'),context)).status,'accepted');
  assert.equal(sent.length,1);assert.equal(f.calls.filter(x=>x.operation==='apply').length,0);
  const action=f.journal.listPendingGmailOfferedPairs()[0]!;assert.ok(action);const authorityFor={channel:'photon' as const,accountId:'account',ownerId:'owner',lineKey:'line',scopeHash:f.client.scopeHash};const bound=f.journal.acceptedGmailOfferedPair(String(action.id),authorityFor)!;assert.equal(bound.acceptedDelivery.payload_hash,createHash('sha256').update(sent[0]!).digest('hex'));
  const wrong={...JSON.parse(String(action.result_json)).preview,offer_receipt:{...(f.calls.find(x=>x.operation==='record_offer') as any),version:1}};assert.throws(()=>f.journal.settleGmailOfferedPair(String(action.id),wrong));
  await c.close();f.reopen();f.loseCreate=false;replay=true;c=coordinator();await c.recover();assert.equal(sent.length,1);assert.equal(f.journal.listPendingGmailOfferedPairs().length,0);
  assert.equal((await c.receive(inbound('yes-pair','Yes.'),context)).status,'accepted');assert.equal(f.calls.filter(x=>x.operation==='apply').length,0);
 }finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('pair preserves complete render/hash and reserves recording envelope before contact',()=>{
 const review={kind:'reply_pair' as const,message_id:'m',thread_id:'t',to:'sender@example.test',subject:'Re: 李',reply_message_id:'<m@test>',source_revision:authority.revision,accept_body:'A'.repeat(6000),decline_body:'D'.repeat(6000),first:'reply' as const,reply_key:'1'.repeat(16),decline_key:'2'.repeat(16),review_hash:''};review.review_hash=gmailDraftReviewHash(review);
 const preview={version:1 as const,status:'preview' as const,event_id:'a'.repeat(64),payload_hash:'b'.repeat(64),review};const pairArgs={operation:'propose_pair' as const,message_id:'m',accept_body:review.accept_body,decline_body:review.decline_body};
 assert.ok(gmailDraftPreviewMatches(preview,pairArgs));assert.ok(gmailDraftReviewText(preview).length>4000);assert.ok(!gmailDraftPreviewMatches(preview,{...pairArgs,first:'decline'}));assert.ok(!gmailDraftPreviewMatches(preview,{operation:'propose',message_id:'m',body:review.accept_body}));
 const client=createOwnerGmailDraftClient({bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),identity,fetch:async()=>assert.fail('Preflight must not contact')});
 const binding=stableActionIdentity(preview.event_id,ownerGmailDraftKind,pairArgs);const request={actionId:binding.actionId,eventId:preview.event_id,payloadHash:preview.payload_hash,text:'owner',admittedAtMs:1,providerThreadId:'thread',args:pairArgs};client.preflightPair(request);
 assert.throws(()=>client.preflightPair({...request,text:'\n'.repeat(16000),args:{...pairArgs,accept_body:'\n'.repeat(7999)+'x',decline_body:'\n'.repeat(7999)+'x'}}),/capacity/);
});

 test('cached pair text is withheld after source revocation, with no recording or Gmail apply',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'pair-revoked-')),f=fixture(folder);let delivered=0;
 const c=new ConversationCoordinator({journal:f.journal,ownerPolicy,ownerGmailDraft:f.client,canPostPrepared:async()=>false,model:{generate:async request=>{const pair={message_id:'message-1',accept_body:'Happy to review it.',decline_body:'I will pass.'};await f.callTool('gmail_draft_pair',pair,request);f.revoke();await assert.rejects(f.callTool('gmail_draft_pair',pair,request));return 'withheld';}},postPrepared:async()=>{delivered++;return {acceptance:'accepted',messageId:'bad'};}});
 try{await c.receive(inbound('revoked-pair','Help me reply'),context);assert.equal(delivered,0);assert.equal(f.calls.filter(x=>['record_offer','apply'].includes(String(x.operation))).length,0);}finally{await c.close();f.journal.close();rmSync(folder,{recursive:true,force:true});}
 });


test('inline wording and external save offers have consistent tool boundaries', () => {
  const inline = GMAIL_DRAFT_EXECUTION_GUIDE.split('For the loaded draft-reply skill')[0]!;
  assert.match(inline, /unambiguously selects an inline wording offer/);
  assert.match(inline, /without gmail_draft_offer, gmail_draft_propose or gmail_draft_pair/);
  assert.match(inline, /Want this in your Gmail drafts/);
  assert.match(inline, /no competing pending offer of any kind/);
  for (const guide of [inline, PROCEDURE_EXECUTION_GUIDE]) {
    assert.match(guide, /ask the owner before drafting or fetching voice guidance/);
    assert.match(guide, /owner clarification/);
  }
  assert.match(inline, /does not apply when the owner explicitly requests alternatives/);
  assert.match(PROCEDURE_EXECUTION_GUIDE, /no competing pending offer of any kind/);
  assert.match(PROCEDURE_EXECUTION_GUIDE, /If both return no_match, clarify/);
  assert.match(PROCEDURE_EXECUTION_GUIDE, /unambiguously selects an inline reply-wording offer/);
  assert.match(PROCEDURE_EXECUTION_GUIDE, /do not probe procedure or Gmail draft-save offer tools/);
  // Unknown offers still require receiver-owned selection; external saves still need fresh review.
  assert.match(PROCEDURE_EXECUTION_GUIDE, /kind is absent.*try this command first/);
  assert.match(GMAIL_DRAFT_EXECUTION_GUIDE, /only a distinct subsequent yes may use gmail_draft_save/);
  assert.match(GMAIL_DRAFT_EXECUTION_GUIDE, /Explicit owner-requested Gmail draft saves retain gmail_draft_propose/);
});
