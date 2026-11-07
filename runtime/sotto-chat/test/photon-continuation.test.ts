import {PREP_OFFER_MAX_AGE_MS} from '../src/channels/policy.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {Metadata, Server, ServerCredentials, status} from '@grpc/grpc-js';
import {createPhotonHistoryClient, createPhotonPrepContinuation, prepContinuationWindowStart, precedingPrepMessage, type ContinuationMessage} from '../src/channels/photon-continuation.js';
const at = 1791910128968, owner = '+15555550123';
const input = {inboundId: 'yes', inboundAtMs: at};
const offer: ContinuationMessage = {guid: 'offer', isFromMe: true, dateCreated: new Date(at - 180_000)};
const yes: ContinuationMessage = {guid: 'yes', isFromMe: false, sender: {address: owner}, dateCreated: new Date(at)};
test('complete native provider window proves one preceding outgoing, independent of response order', () => {
  assert.deepEqual(precedingPrepMessage({messages: [yes, offer]}, input, owner), {messageId: 'offer', sentAtMs: at - 180_000});
});
test('unavailable, truncated, ambiguous, wrong-owner or intervening history cannot grant continuation', () => {
  const cases = [
    {messages: [offer, yes], nextPageToken: 'more'}, {messages: []}, {messages: [offer]},
    {messages: [offer, {...yes, sender: {address: 'someone-else'}}]},
    {messages: [offer, {...yes, dateCreated: new Date(at + 1)}]},
    {messages: [offer, {...yes, isFromMe: true}]},
    {messages: [{...offer, dateCreated: undefined}, yes]},
    {messages: [offer, offer, yes]},
    {messages: [offer, {...offer, guid: 'tie'}, yes]},
    {messages: [{...offer, dateCreated: new Date(at - PREP_OFFER_MAX_AGE_MS - 1)}, yes]},
    {messages: [offer, {...yes, guid: 'earlier-owner', dateCreated: new Date(at - 1)}, yes]},
    {messages: [offer, yes, {...offer, guid: 'future', dateCreated: new Date(at + 1)}]},
  ];
  for (const page of cases) assert.equal(precedingPrepMessage(page, input, owner), null);
});

// Real v2 gRPC decoding must retain the exact timestamps, sender, direction and pagination
// on which the existing continuation proof depends. No model or provider is contacted.
test('v2 history client preserves bounded continuation evidence and never retries a failed read', async () => {
  const uint = (value: number) => {
    let remaining = BigInt(value);
    const bytes: number[] = [];
    while (remaining > 127n) { bytes.push(Number(remaining & 127n) | 128); remaining >>= 7n; }
    return Buffer.from([...bytes, Number(remaining)]);
  };
  const field = (number: number, value: Buffer | string) => {
    const bytes = Buffer.from(value);
    return Buffer.concat([uint(number * 8 + 2), uint(bytes.length), bytes]);
  };
  const stamp = (ms: number) => Buffer.concat([uint(8), uint(Math.floor(ms / 1_000)),
    uint(16), uint(ms % 1_000 * 1_000_000)]);
  const message = (row: ContinuationMessage) => field(1, Buffer.concat([
    field(1, row.guid), field(2, Buffer.alloc(0)), field(10, stamp(row.dateCreated!.getTime())),
    ...(row.sender ? [field(20, field(1, row.sender.address!))] : []), uint(21 * 8), uint(Number(row.isFromMe)),
  ]));
  const chat = `any;-;${owner}`;
  const after = prepContinuationWindowStart(at,offer.dateCreated!.getTime())!, before = at + 1;
  const expectedRequest = Buffer.concat([field(1, chat), uint(16), uint(20),
    field(6, stamp(before)), field(7, stamp(after))]);
  let calls = 0, partial = false, fail = false;
  const requests: Buffer[] = [];
  const authorization: unknown[][] = [];
  const server = new Server();
  server.addService({listChatMessages: {
    path: '/photon.imessage.v1.MessageService/ListChatMessages',
    requestStream: false, responseStream: false,
    requestSerialize: (value: Buffer) => value, requestDeserialize: (value: Buffer) => value,
    responseSerialize: (value: Buffer) => value, responseDeserialize: (value: Buffer) => value,
  }}, {listChatMessages(call: any, callback: any) {
    calls++;
    requests.push(call.request);
    authorization.push(call.metadata.get('authorization'));
    if (fail) {
      const metadata = new Metadata();
      metadata.set('x-retryable', 'true');
      callback(Object.assign(new Error('synthetic transient read failure'), {code: status.UNAVAILABLE, metadata}));
    } else callback(null, Buffer.concat([message(yes), message(offer), ...(partial ? [field(2, 'more')] : [])]));
  }});
  const port = await new Promise<number>((resolve, reject) => server.bindAsync('127.0.0.1:0',
    ServerCredentials.createInsecure(), (error, port) => error ? reject(error) : resolve(port)));
  const client = createPhotonHistoryClient('synthetic-history-only', {address: `127.0.0.1:${port}`, tls: false});
  const read = () => client.messages.listInChat(chat, {pageSize: 20, after: new Date(after), before: new Date(before)});
  try {
    assert.deepEqual(precedingPrepMessage(await read(), input, owner), {messageId: 'offer', sentAtMs: at - 180_000});
    partial = true;
    const page = await read();
    assert.equal(page.nextPageToken, 'more');
    assert.equal(precedingPrepMessage(page, input, owner), null);
    fail = true;
    await assert.rejects(read());
    assert.equal(calls, 3, 'failed read makes one RPC even when the server marks it retryable');
    for (const request of requests) assert.deepEqual(request, expectedRequest, 'exact owner chat, page size and time bounds');
    assert.deepEqual(authorization, Array.from({length: 3}, () => ['Bearer synthetic-history-only']));
  } finally {
    await client.close();
    await new Promise<void>(resolve => server.tryShutdown(() => resolve()));
  }
});

test('prep adjacency recovers a 61-minute offer but rejects the 180-minute boundary and overflow', () => {
  for (const age of [61*60_000,PREP_OFFER_MAX_AGE_MS-1,PREP_OFFER_MAX_AGE_MS,PREP_OFFER_MAX_AGE_MS+1]) {
    const prior={...offer,dateCreated:new Date(at-age)};
    assert.deepEqual(precedingPrepMessage({messages:[prior,yes]},input,owner),
      age<PREP_OFFER_MAX_AGE_MS?{messageId:'offer',sentAtMs:at-age}:null,String(age));
  }
  const tooMany=Array.from({length:21},(_,i)=>({...offer,guid:`extra-${i}`,dateCreated:new Date(at-1000-i)}));
  assert.equal(precedingPrepMessage({messages:[...tooMany,yes]},input,owner),null);
});

test('older inbound events never read provider history even when their offer could remain fresh', async () => {
  let decodes=0;
  const resolve=createPhotonPrepContinuation({photonProjectId:'synthetic-project',photonProjectSecret:'synthetic-secret'},owner,
    ()=>{decodes++;throw new Error('Old inbound must stop before provider lookup');});
  assert.equal(await resolve({threadId:'unused',inboundId:'old-yes',inboundAtMs:Date.now()-15*60_000-1}),null);
  assert.equal(decodes,0);
});


test('candidate-time provider window preserves fresh offers despite older busy-chat traffic', () => {
  for(const age of [5*60_000,61*60_000]){
    const candidate={...offer,dateCreated:new Date(at-age)};
    const busy=Array.from({length:25},(_,i)=>({...offer,guid:`older-${i}`,dateCreated:new Date(at-age-60_000-i)}));
    const history=[...busy,candidate,yes];
    assert.ok(history.filter(row=>row.dateCreated!.getTime()>at-PREP_OFFER_MAX_AGE_MS).length>20,'broad three-hour query would truncate');
    const after=prepContinuationWindowStart(at,candidate.dateCreated!.getTime());
    assert.equal(after,at-age-1);
    const window=history.filter(row=>row.dateCreated!.getTime()>after!&&row.dateCreated!.getTime()<at+1);
    assert.equal(window.length,2,'one complete provider page excludes unrelated older messages');
    assert.deepEqual(precedingPrepMessage({messages:window},input,owner),{messageId:'offer',sentAtMs:at-age});
    assert.equal(precedingPrepMessage({messages:window,nextPageToken:'more'},input,owner),null,'pagination is never ignored');
  }
  assert.equal(prepContinuationWindowStart(at),at-15*60_000,'missing native candidate keeps legacy window');
  for(const invalid of [at,at+1,at-PREP_OFFER_MAX_AGE_MS,at-PREP_OFFER_MAX_AGE_MS-1,NaN])
    assert.equal(prepContinuationWindowStart(at,invalid),null);
});
