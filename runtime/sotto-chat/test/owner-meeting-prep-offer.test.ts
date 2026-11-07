import assert from 'node:assert/strict';
import test from 'node:test';
import {createOwnerMeetingPrepOfferClient} from '../src/tools/owner-meeting-prep-offer-client.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';
import {
  ownerMeetingPrepOfferKind, ownerMeetingPrepOfferReviewHash,
  ownerMeetingPrepOfferMatches, scopedCalendarEventId, type OwnerMeetingPrepOfferReview,
} from '../src/tools/owner-meeting-prep-offer-contract.js';

const identity = {tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',
  line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:'b'.repeat(64)};
const args = {operation:'propose_prep' as const,calendar_id:'primary',event_id:'event-123',
  start:'2026-10-29T10:30:00-07:00',attendee_email:'Ada@example.com',source_revision:'a'.repeat(64)};
const eventId='c'.repeat(64), payloadHash='d'.repeat(64), text='Can you help me prepare for Ada’s meeting?';
const binding=stableActionIdentity(eventId,ownerMeetingPrepOfferKind,args);
const request={actionId:binding.actionId,eventId,payloadHash,text,admittedAtMs:Date.now(),providerThreadId:'thread',
  sourceRevision:args.source_revision,args};

function review():OwnerMeetingPrepOfferReview {
  const value={question:'I can prepare for Acme planning on Thursday at 10:30 AM. Want me to?',
    title:'Acme planning',calendar_id:'calendar@example.com',provider_event_id:args.event_id,start:args.start,
    attendee_email:args.attendee_email,person:args.attendee_email,
    meeting:{event_id:scopedCalendarEventId('calendar@example.com',args.event_id),start:args.start},
    source_revision:args.source_revision,offer_id:'e'.repeat(64),review_hash:''};
  return {...value,review_hash:ownerMeetingPrepOfferReviewHash(value as OwnerMeetingPrepOfferReview)};
}

test('calendar event identity matches Python canonical JSON and primary alias review binding',()=>{
  assert.equal(scopedCalendarEventId('calendar@example.com','event-123'),
    'gcal_e8fe8fd5a5a558b7bb6a6c074d49e66b80c8858b4c2b2554d64b373d929b95e2');
  assert.equal(scopedCalendarEventId('cal-é','event-😀'),
    'gcal_47eae7ca25ac98cf7349a43438a4d79eb7294158320f564af972baa90d63af70');
  assert.equal(ownerMeetingPrepOfferReviewHash(review()),review().review_hash);
});

test('review accepts equivalent offset timestamps for the same provider event instant',()=>{
  const value=review();
  const utcArgs={...args,start:'2026-10-29T17:30:00Z'};
  assert.equal(ownerMeetingPrepOfferMatches(value,utcArgs,args.source_revision),true);
  assert.equal(ownerMeetingPrepOfferMatches({...value,meeting:{...value.meeting,start:'2026-10-29T18:30:00Z'}},
    utcArgs,args.source_revision),false);
});

test('prep offer client binds preview and accepted delivery to exact canonical question and source revision',async()=>{
  const calls:Array<{body:Record<string,unknown>;url:string;headers:Headers}> = [];
  const preview=review();
  let savedDelivery:Record<string,unknown>|undefined;
  const client=createOwnerMeetingPrepOfferClient({bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'f'.repeat(64),identity,
    fetch:async(url,init)=>{
      const body=JSON.parse(String(init?.body)) as Record<string,unknown>;
      calls.push({body,url:String(url),headers:new Headers(init?.headers)});
      if(body.operation==='preview')return Response.json({version:1,status:'preview',action_id:body.action_id,
        event_id:body.event_id,payload_hash:body.payload_hash,args_hash:binding.payloadHash,review:preview});
      if(body.operation==='record_offer'||body.operation==='inspect'){
        if(body.operation==='record_offer')savedDelivery=body.accepted_delivery as Record<string,unknown>;
        else assert.equal(body.accepted_delivery,undefined,'inspect keeps the locally held delivery out of the request');
        const accepted=savedDelivery!;
        return Response.json({version:1,status:'recorded',action_id:body.action_id,event_id:body.event_id,
          payload_hash:body.payload_hash,args_hash:binding.payloadHash,review_hash:preview.review_hash,
          accepted_dispatch_id:accepted.accepted_dispatch_id,provider_message_id:accepted.provider_message_id,
          accepted_at_ms:accepted.accepted_at_ms,offer_id:preview.offer_id});
      }
      throw new Error('unexpected operation');
    }});
  const result=await client.call('preview',request);
  assert.equal(result.status,'preview');
  assert.equal(calls[0]!.url,'http://127.0.0.1:8080/runtime/owner-meeting-prep-request');
  assert.equal(calls[0]!.headers.get('authorization'),`Bearer ${'f'.repeat(64)}`);
  assert.equal((calls[0]!.body.args as Record<string,unknown>).source_revision,args.source_revision);
  assert.equal((calls[0]!.body.identity as Record<string,unknown>).owner_id,identity.owner_id);

  const delivery={accepted_dispatch_id:'dispatch-1',provider_message_id:'message-1',source_revision:args.source_revision,
    review_hash:preview.review_hash,payload_hash:'1'.repeat(64),accepted_at_ms:1_798_000_000_000};
  const receipt=await client.recordOffer(request,delivery);
  assert.equal(receipt.status,'recorded');
  assert.deepEqual(calls[1]!.body.accepted_delivery,delivery);
  assert.equal(calls[1]!.body.text,text);
  await assert.rejects(client.call('inspect',request));
  const inspected=await client.call('inspect',request,undefined,delivery);
  assert.equal(inspected.status,'recorded');
});

test('prep offer client fails closed on review tampering and mismatched accepted receipt',async()=>{
  let tamper='review';
  const valid=review();
  const client=createOwnerMeetingPrepOfferClient({bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'f'.repeat(64),identity,
    fetch:async(_url,init)=>{
      const body=JSON.parse(String(init?.body)) as Record<string,unknown>;
      if(body.operation==='preview'){
        const supplied=tamper==='review'?{...valid,review_hash:'0'.repeat(64)}:valid;
        return Response.json({version:1,status:'preview',action_id:body.action_id,event_id:body.event_id,
          payload_hash:body.payload_hash,args_hash:binding.payloadHash,review:supplied});
      }
      return Response.json({version:1,status:'recorded',action_id:body.action_id,event_id:body.event_id,
        payload_hash:body.payload_hash,args_hash:binding.payloadHash,review_hash:valid.review_hash,
        accepted_dispatch_id:'other-dispatch',provider_message_id:'message-1',accepted_at_ms:1_798_000_000_000,
        offer_id:valid.offer_id});
    }});
  await assert.rejects(client.call('preview',request));
  tamper='receipt';
  const delivery={accepted_dispatch_id:'dispatch-1',provider_message_id:'message-1',source_revision:args.source_revision,
    review_hash:valid.review_hash,payload_hash:'1'.repeat(64),accepted_at_ms:1_798_000_000_000};
  await assert.rejects(client.recordOffer(request,delivery),/receipt is invalid/);
});
