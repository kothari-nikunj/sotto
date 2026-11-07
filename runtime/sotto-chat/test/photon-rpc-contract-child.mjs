import assert from 'node:assert/strict';
import { mkdir, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { findPackageJSON } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { Metadata, Server, ServerCredentials, status } from '@grpc/grpc-js';

const adapterEntry = fileURLToPath(import.meta.resolve('@photon-ai/chat-adapter-imessage'));
const spectrumEntry = fileURLToPath(import.meta.resolve('@spectrum-ts/imessage'));
const spectrumPackage = await resolvedPackage('@spectrum-ts/imessage', import.meta.url);
const spectrumFromAdapter = await resolvedPackage('@spectrum-ts/imessage', pathToFileURL(adapterEntry));
const corePackage = await resolvedPackage('@spectrum-ts/core', import.meta.url);
const coreFromAdapter = await resolvedPackage('@spectrum-ts/core', pathToFileURL(adapterEntry));
assert.equal(spectrumFromAdapter.path, spectrumPackage.path, 'transport helpers use the adapter-owned Spectrum');
assert.equal(coreFromAdapter.path, corePackage.path, 'content helpers use the adapter-owned Spectrum core');
const advancedRootPackage = await resolvedPackage('@photon-ai/advanced-imessage', import.meta.url);
const advancedFromSpectrum = await resolvedPackage('@photon-ai/advanced-imessage', pathToFileURL(spectrumEntry));
// Exercise the gRPC client used by the adapter-owned Spectrum provider.
const advancedManifest = JSON.parse(await readFile(advancedFromSpectrum.path, 'utf8'));
const advancedEntry = join(dirname(advancedFromSpectrum.path), advancedManifest.exports['./grpc'].import);
const {createGrpcClient: createClient} = await import(pathToFileURL(advancedEntry).href);
const niceGrpcPackage = await resolvedPackage('nice-grpc', pathToFileURL(advancedEntry));
const grpcPackage = await resolvedPackage('@grpc/grpc-js', pathToFileURL(advancedEntry));
const grpcFromNiceGrpc = await resolvedPackage('@grpc/grpc-js', pathToFileURL(join(dirname(niceGrpcPackage.path), 'index.js')));
assert.equal(await realpath(grpcFromNiceGrpc.path), await realpath(grpcPackage.path),
  'nice-grpc and Advanced iMessage resolve the same grpc-js package');
assert.equal(advancedFromSpectrum.path, advancedRootPackage.path, 'Spectrum12 shares Advanced2.4');
assert.equal(advancedRootPackage.version, '2.4.0');
assert.deepEqual(
  { spectrum: spectrumPackage.version, advanced: advancedFromSpectrum.version, niceGrpc: niceGrpcPackage.version, grpc: grpcPackage.version },
  { spectrum: '12.14.0', advanced: '2.4.0', niceGrpc: '2.1.18', grpc: '1.14.6' },
  'the observed RPC contract is tied to the actual installed transport dependency chain',
);
await assertBaseAwareResolution();
const paths = {
  text: '/photon.imessage.v1.MessageService/SendTextMessage',
  multipart: '/photon.imessage.v1.MessageService/SendMultipartMessage',
  upload: '/photon.imessage.v1.AttachmentService/UploadAttachment',
};
const attempts = new Map([['text', []], ['multipart', []], ['upload', []]]);
const effects = new Set();
const successfulRootGuid = 'synthetic-root-message-guid';
let server;
let client;

async function resolvedPackage(specifier, base) {
  const path = findPackageJSON(specifier, base);
  assert.ok(path, `package resolution found ${specifier}`);
  const realPath = await realpath(path);
  const manifest = JSON.parse(await readFile(realPath, 'utf8'));
  return { path: realPath, version: manifest.version };
}

async function assertBaseAwareResolution() {
  const root = join(process.env.HOME, 'resolution-base-check');
  const spectrumRoot = join(root, 'node_modules/@spectrum-ts/imessage');
  const rootAdvanced = join(root, 'node_modules/@photon-ai/advanced-imessage');
  const nestedAdvanced = join(spectrumRoot, 'node_modules/@photon-ai/advanced-imessage');
  const spectrumEntry = join(spectrumRoot, 'dist/index.js');
  try {
    await mkdir(dirname(spectrumEntry), { recursive: true });
    await mkdir(rootAdvanced, { recursive: true });
    await mkdir(nestedAdvanced, { recursive: true });
    await writeFile(join(rootAdvanced, 'package.json'), JSON.stringify({ name: '@photon-ai/advanced-imessage', version: '1.0.0' }));
    await writeFile(join(nestedAdvanced, 'package.json'), JSON.stringify({ name: '@photon-ai/advanced-imessage', version: '9.9.9' }));
    await writeFile(spectrumEntry, '');
    const rootResolution = await resolvedPackage('@photon-ai/advanced-imessage', pathToFileURL(join(root, 'consumer.mjs')));
    const nestedResolution = await resolvedPackage('@photon-ai/advanced-imessage', pathToFileURL(spectrumEntry));
    assert.equal(rootResolution.version, '1.0.0');
    assert.equal(nestedResolution.version, '9.9.9', 'findPackageJSON honors the Spectrum importer base');
    assert.notEqual(await realpath(rootResolution.path), await realpath(nestedResolution.path));
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

function method(path, originalName) {
  return {
    path,
    requestStream: false,
    responseStream: false,
    requestSerialize: (value) => Buffer.from(value),
    requestDeserialize: (bytes) => Buffer.from(bytes),
    responseSerialize: (value) => Buffer.from(value),
    responseDeserialize: (bytes) => Buffer.from(bytes),
    originalName,
  };
}

try {
  assert.ok(process.env.HOME, 'test runner supplied a private synthetic HOME');
  server = new Server();
  server.addService({
    sendTextMessage: method(paths.text, 'sendTextMessage'),
    sendMultipartMessage: method(paths.multipart, 'sendMultipartMessage'),
    uploadAttachment: method(paths.upload, 'uploadAttachment'),
  }, {
    sendTextMessage(call, callback) {
      if (call.request.includes(Buffer.from('success root id'))) {
        const values = call.metadata.get('x-idempotency-key');
        attempts.get('text').push({ key: typeof values[0] === 'string' ? values[0] : '', success: true });
        callback(null, successMessageResponse(successfulRootGuid));
      } else {
        respondAmbiguously('text', call, callback);
      }
    },
    sendMultipartMessage(call, callback) {
      respondAmbiguously('multipart', call, callback);
    },
    uploadAttachment(call, callback) {
      respondAmbiguously('upload', call, callback);
    },
  });

  let port;
  await new Promise((resolve, reject) => {
    server.bindAsync('127.0.0.1:0', ServerCredentials.createInsecure(), (error, boundPort) => {
      if (error) reject(error);
      else { port = boundPort; resolve(); }
    });
  });
  client = createClient({
    address: `127.0.0.1:${port}`,
    tls: false,
    token: 'synthetic-test-only',
    autoIdempotency: true,
    retry: true,
    timeout: 3000,
  });

  const textError = await capture(() => client.messages.sendText('any;-;+15551111111', 'synthetic test text'));
  assertRetryableUnknown(textError, 'SendTextMessage');
  const successfulMessage = await client.messages.sendText('any;-;+15551111111', 'synthetic success root id');
  assert.equal(successfulMessage.guid, successfulRootGuid, 'successful SDK result exposes the root provider message GUID');
  const multipartError = await capture(() => client.messages.sendMultipart(
    'any;-;+15551111111', [{ text: 'synthetic test multipart' }],
  ));
  assertRetryableUnknown(multipartError, 'SendMultipartMessage');
  const uploadError = await capture(() => client.attachments.upload({
    fileName: 'synthetic.bin', data: new Uint8Array([1, 2, 3, 4]),
  }));
  assertRetryableUnknown(uploadError, 'UploadAttachment');

  for (const kind of ['text', 'multipart', 'upload']) {
    const calls = attempts.get(kind);
    assert.equal(calls.length, kind === 'text' ? 2 : 1, `${kind} made the expected RPC count for this installed SDK behavior`);
    const ambiguousCall = calls.find((call) => !call.success);
    assert.ok(ambiguousCall);
    assert.match(ambiguousCall.key, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
      `${kind} carried one generated idempotency key`);
    assert.equal(effects.has(`${kind}:${ambiguousCall.key}`), true,
      `${kind} simulated one accepted provider-side effect before its ambiguous response`);
  }

  process.stdout.write(JSON.stringify({
    ok: true,
    sdk: { spectrum: spectrumPackage.version, advanced: advancedFromSpectrum.version, niceGrpc: niceGrpcPackage.version, grpc: grpcPackage.version },
    calls: Object.fromEntries([...attempts].map(([kind, calls]) => [kind, calls.length])),
    successfulRootGuid,
    keysPresent: Object.fromEntries([...attempts].map(([kind, calls]) => [kind, Boolean(calls[0]?.key)])),
    errorsRetryable: true,
    simulatedEffects: effects.size,
  }) + '\n');
} finally {
  if (client) await client.close();
  if (server) await new Promise((resolve) => server.tryShutdown(resolve));
}

function successMessageResponse(guid) {
  const field = (number, value) => Buffer.concat([Buffer.from([(number << 3) | 2]), varint(value.length), value]);
  const message = Buffer.concat([
    field(1, Buffer.from(guid)),
    field(2, Buffer.alloc(0)),
    field(10, Buffer.alloc(0)),
  ]);
  return field(1, message);
}

function varint(value) {
  const bytes = [];
  let remaining = value;
  while (remaining > 127) {
    bytes.push((remaining & 127) | 128);
    remaining >>>= 7;
  }
  bytes.push(remaining);
  return Buffer.from(bytes);
}

function respondAmbiguously(kind, call, callback) {
  const values = call.metadata.get('x-idempotency-key');
  const key = typeof values[0] === 'string' ? values[0] : '';
  attempts.get(kind).push({ key, requestBytes: call.request.byteLength });
  if (!key) {
    callback(Object.assign(new Error('fixture expected idempotency key'), { code: status.INVALID_ARGUMENT }));
    return;
  }
  effects.add(`${kind}:${key}`);
  const trailer = new Metadata();
  trailer.set('x-retryable', 'true');
  callback(Object.assign(new Error('synthetic ambiguous response after accepted effect'), {
    code: status.UNAVAILABLE,
    metadata: trailer,
    details: 'synthetic retryable gRPC response',
  }));
}

async function capture(operation) {
  try {
    await operation();
    assert.fail('expected the synthetic gRPC error');
  } catch (error) {
    return error;
  }
}

function assertRetryableUnknown(error, operation) {
  assert.equal(typeof error?.retryable, 'boolean', `${operation} exposes the public retryable status`);
  assert.equal(error.retryable, true, `${operation} preserves the server retryable status`);
  assert.equal(typeof error?.code, 'string', `${operation} returns a classified public error`);
}
