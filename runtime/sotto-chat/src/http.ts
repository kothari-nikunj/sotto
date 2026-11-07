/** Deliberately small loopback API. Authorization never depends on model output. */
import { createHash, timingSafeEqual } from 'node:crypto';
import { realpath, stat } from 'node:fs/promises';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { isAbsolute, relative, sep } from 'node:path';
import { z } from 'zod';
import { SCHEDULED_TEXT_MAX_ATTEMPTS } from './journal/index.js';
import { activationEvidenceSchema, type ActivationEvidence } from './activation.js';
import { configuredChannelIdentitySchema, type ConfiguredChannelIdentity } from './configured-identity.js';
import { activationStateSchema, type ActivationState } from './activation-state.js';
import { ManagedProxyAdmissionBusy, ManagedProxyRequestTooLarge } from './agent/models.js';

const identifier = z.string().regex(/^[A-Za-z0-9._:-]{1,160}$/);
const sendSchema = z.object({
  dispatch_id: identifier,
  target: z.string().min(1).max(512),
  text: z.string().min(1).max(32_000),
  source_revision: z.string().regex(/^[a-f0-9]{64}$/).optional(),
  // Repeated HTTP requests look up the same attempt, they do not authorize retries.
  attempt: z.number().int().min(1).max(SCHEDULED_TEXT_MAX_ATTEMPTS).default(1),
}).strict();
const gallerySchema = z.object({
  dispatch_id: identifier,
  target: z.string().min(1).max(512),
  paths: z.array(z.string().min(1).max(4096)).length(4),
  summary: z.string().min(1).max(1000),
}).strict();
const galleryBytesSchema = z.object({
  dispatch_id: identifier,
  target: z.string().min(1).max(512),
  summary: z.string().min(1).max(1000),
  images: z.array(z.object({
    name: z.string().regex(/^0[1-4]\.png$/),
    base64: z.string().min(12).max(6_666_668),
  }).strict()).length(4),
}).strict();
const runSchema = z.object({
  answer_only: z.literal(true).optional(),
  prompt: z.string().min(1).max(100_000),
  skill: z.string().regex(/^[a-z][a-z0-9-]{0,63}$/).optional(),
  env: z.object({
    SOTTO_UNATTENDED: z.literal('1'),
    SOTTO_DELIVERY_RUN_ID: identifier,
  }).strict(),
}).strict();
const sessionsSchema = z.discriminatedUnion('action', [
  z.object({ action: z.literal('list') }).strict(),
  z.object({ action: z.literal('rotate') }).strict(),
  z.object({ action: z.literal('purge_before'), before: z.iso.datetime() }).strict(),
]);
const retentionSchema = z.object({
  delivery_before: z.string().regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/),
  action_before: z.string().regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/),
}).strict();

export type SendRequest = z.infer<typeof sendSchema>;
export type GalleryRequest = z.infer<typeof gallerySchema>;
export type GalleryBytesRequest = Omit<z.infer<typeof galleryBytesSchema>, 'images'> & {
  images: { name: string; bytes: Buffer }[];
};
export type UnattendedRequest = z.infer<typeof runSchema>;
export type SessionsRequest = z.infer<typeof sessionsSchema>;
export type RetentionRequest = z.infer<typeof retentionSchema>;
export interface HttpServices {
  send(request: SendRequest): Promise<unknown>;
  sendGallery(request: GalleryRequest): Promise<unknown>;
  sendGalleryBytes?(request: GalleryBytesRequest): Promise<unknown>;
  receipt(dispatchId: string): Promise<unknown>;
  run(request: UnattendedRequest, signal: AbortSignal): Promise<unknown>;
  sessions(request: SessionsRequest): Promise<unknown>;
  retention?(request: RetentionRequest): Promise<unknown>;
  activationEvidence?(): Promise<ActivationEvidence | null>;
  activationState?(): Promise<ActivationState>;
  activationAck?(eventId: string): Promise<boolean>;
  health(): Promise<unknown>;
}
export interface HttpOptions {
  bearerToken: string;
  allowedTargets: ReadonlySet<string>;
  mediaRoot: string;
  /** Present only when bootstrap parsed an explicit private runtime config. */
  configuredChannelIdentity?: ConfiguredChannelIdentity;
  maxBodyBytes?: number;
  runTimeoutMs?: number;
}

class HttpFailure extends Error {
  constructor(readonly status: number, readonly code: string) { super(code); }
}

const digest = (value: string): Buffer => createHash('sha256').update(value).digest();

async function readJson(request: IncomingMessage, limit: number): Promise<unknown> {
  if (request.headers['content-type']?.split(';')[0].trim() !== 'application/json') {
    throw new HttpFailure(415, 'json_required');
  }
  const declared = request.headers['content-length'];
  if (declared && Number(declared) > limit) throw new HttpFailure(413, 'body_too_large');
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.length;
    if (size > limit) throw new HttpFailure(413, 'body_too_large');
    chunks.push(bytes);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new HttpFailure(400, 'invalid_json'); }
}

function respond(response: ServerResponse, status: number, body: unknown): void {
  if (response.destroyed || response.writableEnded) return;
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
  });
  response.end(JSON.stringify(body));
}

async function validateGalleryPaths(paths: string[], mediaRoot: string): Promise<string[]> {
  try {
    const root = await realpath(mediaRoot);
    const validated: string[] = [];
    for (const path of paths) {
      const resolved = await realpath(path);
      const child = relative(root, resolved);
      if (!child || child === '..' || child.startsWith(`..${sep}`) || isAbsolute(child) || !resolved.endsWith('.png')) {
        throw new Error('outside media root');
      }
      const info = await stat(resolved);
      if (!info.isFile() || info.size > 5_000_000 || info.size === 0) throw new Error('invalid image');
      validated.push(resolved);
    }
    return validated;
  } catch { throw new HttpFailure(400, 'invalid_gallery_files'); }
}

function decodeGalleryBytes(input: z.infer<typeof galleryBytesSchema>): GalleryBytesRequest {
  const images = input.images.map((image, index) => {
    if (image.name !== `0${index + 1}.png` || image.base64.length % 4 !== 0 ||
        !/^[A-Za-z0-9+/]*={0,2}$/u.test(image.base64)) {
      throw new HttpFailure(400, 'invalid_gallery_bytes');
    }
    const bytes = Buffer.from(image.base64, 'base64');
    if (bytes.length === 0 || bytes.length > 5_000_000 ||
        bytes.toString('base64') !== image.base64 ||
        !bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
      throw new HttpFailure(400, 'invalid_gallery_bytes');
    }
    return { name: image.name, bytes };
  });
  return { dispatch_id: input.dispatch_id, target: input.target, summary: input.summary, images };
}

/** No network listeners or SDK connections are created until listenLoopback is called. */
export function createChatHttpServer(services: HttpServices, options: HttpOptions) {
  if (options.bearerToken.length < 32 || /\s/.test(options.bearerToken)) {
    throw new Error('A private bearer of at least 32 characters is required');
  }
  if (options.allowedTargets.size === 0) throw new Error('At least one owner destination is required');
  const expected = digest(`Bearer ${options.bearerToken}`);
  const configuredChannelIdentity = options.configuredChannelIdentity === undefined
    ? undefined : configuredChannelIdentitySchema.parse(options.configuredChannelIdentity);
  const routes = new Set(['/v1/send', '/v1/send-gallery', '/v1/send-gallery-bytes', '/v1/receipt', '/v1/run', '/v1/sessions', '/v1/retention', '/v1/health', '/v1/activation-evidence', '/v1/activation-ack', '/v1/activation-state', '/v1/configured-identity']);
  const operations = new Set<Promise<void>>();
  const handle = async (request: IncomingMessage, response: ServerResponse): Promise<void> => {
    const path = request.url ?? '';
    try {
      // Reject duplicate Authorization values instead of accepting node's first value.
      const authCount = request.rawHeaders.filter((_, i, all) => i % 2 === 0 && all[i].toLowerCase() === 'authorization').length;
      if (authCount !== 1 || !timingSafeEqual(expected, digest(request.headers.authorization ?? ''))) {
        throw new HttpFailure(401, 'unauthorized');
      }
      if (!routes.has(path)) throw new HttpFailure(404, 'not_found');
      const expectedMethod = path === '/v1/health' ? 'GET' : 'POST';
      if (request.method !== expectedMethod) throw new HttpFailure(405, 'method_not_allowed');
      if (path === '/v1/health') { respond(response, 200, await services.health()); return; }
      const bodyLimit = path === '/v1/configured-identity' || path === '/v1/activation-state' ? 32 :
        path.startsWith('/v1/activation-') ? Math.min(1024, options.maxBodyBytes ?? 1024) :
        path === '/v1/send-gallery-bytes' ? Math.min(27_000_000, options.maxBodyBytes ?? 27_000_000) :
        options.maxBodyBytes ?? 1_048_576;
      const body = await readJson(request, bodyLimit);
      if (path === '/v1/configured-identity') {
        z.object({}).strict().parse(body);
        if (!configuredChannelIdentity) throw new HttpFailure(501, 'configured_identity_unavailable');
        respond(response, 200, configuredChannelIdentity);
      } else if (path === '/v1/activation-state') {
        z.object({}).strict().parse(body);
        if (!configuredChannelIdentity || !services.activationState) throw new HttpFailure(501, 'activation_state_unavailable');
        const state = activationStateSchema.parse(await services.activationState());
        if (JSON.stringify(state.configured) !== JSON.stringify(configuredChannelIdentity)) {
          throw new HttpFailure(503, 'activation_state_unavailable');
        }
        respond(response, 200, state);
      } else if (path === '/v1/send-gallery-bytes') {
        if (!services.sendGalleryBytes) throw new HttpFailure(501, 'gallery_bytes_unavailable');
        const parsed = galleryBytesSchema.parse(body);
        if (!options.allowedTargets.has(parsed.target)) throw new HttpFailure(403, 'destination_refused');
        respond(response, 200, await services.sendGalleryBytes(decodeGalleryBytes(parsed)));
      } else if (path === '/v1/send' || path === '/v1/send-gallery') {
        const parsed = path === '/v1/send' ? sendSchema.parse(body) : gallerySchema.parse(body);
        if (!options.allowedTargets.has(parsed.target)) throw new HttpFailure(403, 'destination_refused');
        if ('paths' in parsed) {
          const paths = await validateGalleryPaths(parsed.paths, options.mediaRoot);
          respond(response, 200, await services.sendGallery({ ...parsed, paths }));
        } else respond(response, 200, await services.send(parsed));
      } else if (path === '/v1/receipt') {
        const parsed = z.object({ dispatch_id: identifier }).strict().parse(body);
        respond(response, 200, await services.receipt(parsed.dispatch_id));
      } else if (path === '/v1/sessions') {
        respond(response, 200, await services.sessions(sessionsSchema.parse(body)));
      } else if (path === '/v1/activation-evidence') {
        z.object({}).strict().parse(body);
        if (!services.activationEvidence) throw new HttpFailure(501, 'activation_unavailable');
        const pending = await services.activationEvidence();
        respond(response, 200, { version: 1, pending: pending === null ? null : activationEvidenceSchema.parse(pending) });
      } else if (path === '/v1/activation-ack') {
        const parsed = z.object({ event_id: z.string().regex(/^[a-f0-9]{64}$/) }).strict().parse(body);
        if (!services.activationAck) throw new HttpFailure(501, 'activation_unavailable');
        respond(response, 200, { acknowledged: await services.activationAck(parsed.event_id) });
      } else if (path === '/v1/retention') {
        if (!services.retention) throw new HttpFailure(501, 'retention_unavailable');
        const parsed = retentionSchema.parse(body);
        const delivery = Date.parse(parsed.delivery_before);
        const action = Date.parse(parsed.action_before);
        if (!Number.isFinite(delivery) || !Number.isFinite(action) ||
            delivery >= Date.now() || action >= Date.now() || action > delivery) {
          throw new HttpFailure(400, 'invalid_retention_cutoff');
        }
        respond(response, 200, await services.retention(parsed));
      } else {
        const parsed = runSchema.parse(body);
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(new Error('run_timeout')), options.runTimeoutMs ?? 600_000);
        timer.unref();
        const onClose = () => { if (!response.writableEnded) controller.abort(new Error('caller_disconnected')); };
        response.once('close', onClose);
        try { respond(response, 200, await services.run(parsed, controller.signal)); }
        finally { clearTimeout(timer); response.removeListener('close', onClose); }
      }
    } catch (error) {
      if (path === '/v1/run') {
        // Fixed categories only: never emit the prompt, URL, bearer, error message or stack.
        const category = error instanceof Error && ['Bridge request is unavailable', 'Bridge tool discovery failed'].includes(error.message)
          ? 'bridge_discovery' : error instanceof ManagedProxyAdmissionBusy ? 'model_admission_busy'
          : error instanceof ManagedProxyRequestTooLarge ? 'model_request_too_large'
          : error instanceof z.ZodError ? 'invalid_request' : error instanceof HttpFailure ? 'http_refusal' : 'operation_failed';
        console.error('[sotto-run-failure]', JSON.stringify({location: 'http.run', category}));
      }
      if (path === '/v1/run' && error instanceof ManagedProxyAdmissionBusy) {
        response.setHeader('X-Sotto-Admission', 'busy');
        respond(response, 429, { error: 'model_admission_busy' });
      }
      else if (path === '/v1/run' && error instanceof ManagedProxyRequestTooLarge) {
        respond(response, 413, { error: 'model_request_too_large' });
      }
      else if (error instanceof HttpFailure) respond(response, error.status, { error: error.code });
      else if (error instanceof z.ZodError) respond(response, 400, { error: 'invalid_request' });
      else respond(response, 500, { error: 'operation_failed' }); // Never echo secret-bearing exception text.
    }
  };
  const server = createServer((request, response) => {
    const operation = handle(request, response);
    operations.add(operation);
    const settled = () => { operations.delete(operation); };
    void operation.then(settled, settled);
  });
  server.requestTimeout = 30_000;
  server.headersTimeout = 10_000;
  return Object.assign(server, {
    /** Socket shutdown does not join provider/model promises owned by request handlers. */
    async drainOperations(): Promise<void> {
      while (operations.size) await Promise.allSettled([...operations]);
    },
  });
}

/** Explicit localhost binding is the only supported public startup helper. */
export async function listenLoopback(server: ReturnType<typeof createChatHttpServer>, port = 0): Promise<number> {
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => { server.removeListener('error', reject); resolve(); });
  });
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('Expected loopback listener');
  return address.port;
}
