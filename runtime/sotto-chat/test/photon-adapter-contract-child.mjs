import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { Metadata, Server, ServerCredentials, status } from '@grpc/grpc-js';
import { createiMessageAdapter } from '@photon-ai/chat-adapter-imessage';

const server = new Server();
const attempts = [];
const guid = 'synthetic-adapter-root-guid';
const sentAtMs = 1_700_000_000_000;
function varint(value) {
  const bytes = [];
  while (value > 127) { bytes.push((value & 127) | 128); value = Math.floor(value / 128); }
  return Buffer.from([...bytes, value]);
}
const field = (number, value) => Buffer.concat([Buffer.from([(number << 3) | 2, value.length]), value]);
const success = field(1, Buffer.concat([
  field(1, Buffer.from(guid)), field(2, Buffer.alloc(0)), field(10, Buffer.concat([Buffer.from([8]), varint(sentAtMs / 1000)])),
]));
server.addService({ sendTextMessage: {
  path: '/photon.imessage.v1.MessageService/SendTextMessage',
  requestStream: false, responseStream: false,
  requestSerialize: value => Buffer.from(value), requestDeserialize: value => Buffer.from(value),
  responseSerialize: value => Buffer.from(value), responseDeserialize: value => Buffer.from(value),
  originalName: 'sendTextMessage',
} }, { sendTextMessage(call, callback) {
  const key = call.metadata.get('x-idempotency-key')[0];
  attempts.push({ key, ambiguous: call.request.includes(Buffer.from('ambiguous')) });
  if (attempts.at(-1).ambiguous) {
    const metadata = new Metadata();
    metadata.set('x-retryable', 'true');
    callback(Object.assign(new Error('accepted synthetic effect, response unavailable'), {
      code: status.UNAVAILABLE, metadata,
    }));
  } else callback(null, success);
} });
let adapter;
try {
  const cert = await readFile(process.env.FIXTURE_CERT);
  const key = await readFile(process.env.FIXTURE_KEY);
  const port = await new Promise((resolve, reject) => server.bindAsync('127.0.0.1:0',
    ServerCredentials.createSsl(null, [{ private_key: key, cert_chain: cert }]),
    (error, port) => error ? reject(error) : resolve(port)));
  const noop = () => {};
  adapter = createiMessageAdapter({
    clients: [{ address: `localhost:${port}`, token: 'synthetic-only', phone: '+15551111111' }],
    logger: { info: noop, debug: noop, warn: noop, error: noop },
  });
  const threadId = adapter.encodeThreadId({ chatGuid: 'any;-;+15552222222', phone: '+15551111111' });
  const sent = await adapter.postMessage(threadId, { raw: 'synthetic success' });
  assert.equal(sent.id, guid);
  assert.equal(sent.threadId, threadId);
  assert.equal(sent.raw.id, guid);
  assert.equal(sent.raw.direction, 'outbound');
  assert.ok(sent.raw.timestamp instanceof Date);
  assert.equal(sent.raw.timestamp.getTime(), sentAtMs);
  assert.equal(sent.raw.space.id, 'any;-;+15552222222');
  await assert.rejects(() => adapter.postMessage(threadId, { raw: 'synthetic ambiguous' }),
    error => error.retryable === true);
  assert.equal(attempts.length, 2, 'one successful send and one ambiguous send; adapter must not resend');
  for (const attempt of attempts) assert.match(attempt.key, /^[0-9a-f-]{36}$/i,
    'real Spectrum provider enables automatic idempotency');
  assert.notEqual(attempts[0].key, attempts[1].key, 'distinct logical sends have distinct keys');
  process.stdout.write('CONTRACT_RESULT=' + JSON.stringify({ ok: true, adapterCalls: 2, ambiguousEffects: 1, rootGuid: guid }) + '\n');
} finally {
  // The installed adapter exposes no disposal method; stop its owned Spectrum instance for fixture cleanup.
  if (adapter?.app) await adapter.app.stop();
  await new Promise(resolve => server.tryShutdown(resolve));
}
