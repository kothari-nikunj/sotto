import assert from 'node:assert/strict';
import test from 'node:test';
import {RequestContext} from '@mastra/core/request-context';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createOwnerGmailDraftClient} from '../src/tools/owner-gmail-draft-client.js';
import {createOwnerGmailDraftTools} from '../src/tools/owner-gmail-draft-tool.js';
import {gmailDraftReviewHash} from '../src/tools/owner-gmail-draft-contract.js';
import {createOwnerOfferReplyClient, ownerOfferReplyKind} from '../src/tools/owner-offer-reply-client.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';

const identity = {tenant_id: 'tenant', channel: 'photon' as const, account_id: 'account', owner_id: 'owner',
  line_key: 'line', target: 'photon' as const, route_mode: 'fixed' as const, route_id: 'thread',
  project_fingerprint: 'a'.repeat(64)};
const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: 'owner'};
const context = {threadId: 'thread', destination: 'owner', lineKey: 'line'};

test('Review draft selects the provider-quoted brief and only presents the saved draft', async () => {
  const journal = new OperationalJournal(':memory:');
  const revision = 'd'.repeat(64), offerId = 'e'.repeat(64);
  const calls: {operation: string; args: {operation: string; message_id: string}}[] = [];
  const client = createOwnerGmailDraftClient({bridgeUrl: 'http://127.0.0.1:8080/mcp',
    bearer: 'c'.repeat(64), identity, fetch: async (_url, init) => {
      const body = JSON.parse(String(init?.body));
      calls.push(body);
      assert.equal(body.operation, 'preview');
      assert.equal(body.args.message_id, 'quoted-brief');
      if (body.args.operation === 'list_followup')
        return Response.json({version: 1, status: 'offers', offers: [
          {offer_id: offerId, to_name: 'Sam', to_email: 'sam@example.test', channel: 'email', subject: 'Harbor'},
        ]});
      const review = {kind: 'new_email' as const, to: 'sam@example.test', subject: 'Harbor',
        body: 'Exact saved draft.', source_revision: revision, review_hash: ''};
      review.review_hash = gmailDraftReviewHash(review);
      return Response.json({version: 1, status: 'preview', event_id: body.event_id,
        payload_hash: body.payload_hash, offer_id: offerId, review});
    }});
  const tools = createOwnerGmailDraftTools({client, journal: () => journal,
    sourceAuthority: {read: async () => ({version: 2 as const, complete: true, revision,
      bridge_sources: [], google_sources: ['gmail' as const]})}});
  const replies: string[] = [];
  const coordinator = new ConversationCoordinator({journal, ownerGmailDraft: client,
    ownerPolicy,
    model: {generate: async request => {
      const requestContext = new RequestContext();
      for (const [key, value] of Object.entries({mode: 'chat', runId: request.runId,
        ownerRequestKey: request.ownerRequestKey, sourceRevision: revision,
        allowOwnerGmailDraft: request.allowOwnerGmailDraft})) requestContext.set(key, value);
      const options = {requestContext, abortSignal: request.signal} as never;
      const result = await tools.gmail_draft_followup_list.execute!({}, options);
      if ((result as {status: string}).status === 'offers')
        await tools.gmail_draft_followup_review.execute!({offer_id: offerId}, options);
      return 'Review';
    }},
    postPrepared: async request => {
      replies.push(request.text);
      return {acceptance: 'accepted', messageId: 'preview-' + replies.length};
    }});
  const inbound = {channel: 'photon' as const, accountId: 'account', senderId: 'owner',
    isDirect: true, text: 'Review draft'};
  try {
    await coordinator.receive({...inbound, eventId: 'unbound'}, context);
    assert.equal(calls.length, 0);
    await coordinator.receive({...inbound, eventId: 'bound', replyBound: true, replyToMessageId: 'quoted-brief'}, context);
    assert.deepEqual(calls.map(call => call.args.operation), ['list_followup', 'propose_followup']);
    assert.match(replies.at(-1) ?? '', /Subject: Harbor\n\nExact saved draft\./u);
  } finally {
    await coordinator.close();
    journal.close();
  }
});

test('unquoted Hide after a scheduled brief declines its exact offer without going to the model', async () => {
  const journal = new OperationalJournal(':memory:');
  const routeDigest = 'b'.repeat(64), offerId = 'e'.repeat(64);
  const operations: string[] = [];
  const client = createOwnerOfferReplyClient({bridgeUrl: 'http://127.0.0.1:8080/mcp',
    bearer: 'c'.repeat(64), identity, fetch: async (_url, init) => {
      const body = JSON.parse(String(init?.body));
      operations.push(body.operation);
      assert.equal(body.args.selected_offer.message_id, 'scheduled-brief');
      if (body.operation === 'preview')
        return Response.json({version: 1, status: 'match', offer_id: offerId, kind: 'followup_draft'});
      const binding = stableActionIdentity(body.event_id, ownerOfferReplyKind, body.args);
      return Response.json({version: 1, status: 'committed', event_id: body.event_id,
        action_id: binding.actionId, payload_hash: body.payload_hash, args_hash: binding.payloadHash,
        outcome: {operation: 'apply', offer_id: offerId, action: 'declined', loop_changed: false}});
    }});
  const coordinator = new ConversationCoordinator({journal, ownerPolicy, ownerOfferReply: client,
    routeDigestForThread: () => routeDigest, model: {generate: async () => assert.fail('unexpected model call')},
    postPrepared: async () => ({acceptance: 'accepted', messageId: 'hide-receipt'})});
  try {
    journal.prepareDispatch({dispatchId: 'brief-dispatch', kind: 'scheduled_text',
      destination: 'owner', payload: 'Review draft or Hide?', routeDigest});
    journal.beginDispatch('brief-dispatch');
    journal.settleDispatch('brief-dispatch', 1, 'accepted', 'scheduled-brief', ['scheduled-brief'], Date.now() - 1000);
    await new Promise(resolve => setTimeout(resolve, 5));
    await coordinator.receive({channel: 'photon', accountId: 'account', senderId: 'owner',
      isDirect: true, eventId: 'hide', text: 'Hide!!'}, context, Date.now());
    assert.deepEqual(operations, ['preview', 'apply']);
  } finally {
    await coordinator.close();
    journal.close();
  }
});
