import type { iMessageAdapter } from '@photon-ai/chat-adapter-imessage';
import { resolvePhotonMediaContent } from './media-descriptors.js';
import { resolveTelegramCredentials } from './credentials.js';
import {
  MAX_INBOUND_MEDIA_BYTES, MAX_INBOUND_MEDIA_ITEMS, MAX_INBOUND_TURN_MEDIA_BYTES,
  mediaSignatureMatches, normalizeMediaMime,
} from './media-policy.js';
import type { Channel, CredentialSource, Inbound, Media, OwnerPolicy, PhotonMediaReference } from './types.js';

const TELEGRAM_ORIGIN = 'https://api.telegram.org';
const DEADLINE_MS = 30_000;
const MAX_FILE_INFO_BYTES = 16_384;
const MAX_ID_LENGTH = 512;

export interface MediaHydratorOptions {
  channel: Channel;
  owner: OwnerPolicy;
  providerThreadId: string;
  adapter: Pick<iMessageAdapter, 'fetchMessage'> | object;
  credentials: CredentialSource;
  /** Only for offline tests; production uses global fetch and the fixed Telegram origin. */
  fetch?: typeof fetch;
}

export interface HydrationContext {
  signal: AbortSignal;
  isCurrent(): boolean;
}

function cancelled(context: HydrationContext): boolean {
  return context.signal.aborted || !context.isCurrent();
}

function assertCurrent(context: HydrationContext): void {
  if (cancelled(context)) throw new Error('media hydration cancelled');
}

function safeId(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= MAX_ID_LENGTH && !/[\x00-\x1f\x7f]/.test(value);
}

function omission(media: Media, reason: NonNullable<Media['omission']>): Media {
  const { data: _data, ...rest } = media;
  return { ...rest, omission: reason };
}

function readerLimit(response: Response, maximum: number, signal: AbortSignal, charge?: (bytes: number) => void): Promise<Uint8Array> {
  return (async () => {
    const declared = Number(response.headers.get('content-length'));
    if (Number.isFinite(declared) && declared > maximum) {
      void response.body?.cancel().catch(() => undefined);
      throw new Error('media too large');
    }
    const reader = response.body?.getReader();
    if (!reader) throw new Error('media body unavailable');
    const chunks: Uint8Array[] = [];
    let total = 0;
    const abort = () => { void reader.cancel().catch(() => undefined); };
    signal.addEventListener('abort', abort, { once: true });
    try {
      while (true) {
        if (signal.aborted) throw new Error('media request cancelled');
        const item = await reader.read();
        if (signal.aborted) throw new Error('media request cancelled');
        if (item.done) break;
        total += item.value.byteLength;
        charge?.(item.value.byteLength);
        if (total > maximum) {
          void reader.cancel().catch(() => undefined);
          throw new Error('media too large');
        }
        chunks.push(item.value);
      }
    } catch (error) {
      void reader.cancel().catch(() => undefined);
      throw error;
    } finally {
      signal.removeEventListener('abort', abort);
      reader.releaseLock();
    }
    if (signal.aborted) throw new Error('media request cancelled');
    return joinChunks(chunks, total);
  })();
}

function joinChunks(chunks: Uint8Array[], total: number): Uint8Array {
  const data = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
  return data;
}

function safeFilePath(value: unknown): value is string {
  if (typeof value !== 'string' || value.length < 1 || value.length > 1024 || value.startsWith('/') || value.includes('\\') ||
      value.includes('?') || value.includes('#') || value.includes('%') || /[\x00-\x20\x7f]/.test(value)) return false;
  return value.split('/').every(part => part.length > 0 && part !== '.' && part !== '..' && /^[A-Za-z0-9._-]+$/.test(part));
}

function requestSignal(context: HydrationContext): AbortSignal {
  const timeout = AbortSignal.timeout(DEADLINE_MS);
  return AbortSignal.any([context.signal, timeout]);
}

async function telegramBytes(fileId: string, token: string, transport: typeof fetch, context: HydrationContext,
  maximum: number, charge: (bytes: number) => void): Promise<Uint8Array> {
  assertCurrent(context);
  if (!safeId(fileId) || !/^[A-Za-z0-9_-]{1,512}$/.test(fileId) || !/^[A-Za-z0-9:_-]{1,256}$/.test(token)) {
    throw new Error('media reference unavailable');
  }
  const signal = requestSignal(context);
  const base = `${TELEGRAM_ORIGIN}/bot${token}`;
  const info = await transport(`${base}/getFile`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ file_id: fileId }),
    signal, redirect: 'error',
  });
  assertCurrent(context);
  if (!info.ok) throw new Error('media lookup unavailable');
  const bytes = await readerLimit(info, MAX_FILE_INFO_BYTES, signal);
  assertCurrent(context);
  const parsed: unknown = JSON.parse(new TextDecoder().decode(bytes));
  const record = parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : {};
  const result = record.result && typeof record.result === 'object' ? record.result as Record<string, unknown> : {};
  if (record.ok !== true || !safeFilePath(result.file_path)) throw new Error('media lookup unavailable');
  const content = await transport(`${TELEGRAM_ORIGIN}/file/bot${token}/${result.file_path}`, {
    method: 'GET', signal, redirect: 'error',
  });
  assertCurrent(context);
  if (!content.ok) throw new Error('media unavailable');
  const data = await readerLimit(content, maximum, signal, charge);
  assertCurrent(context);
  return data;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

async function streamBytes(stream: ReadableStream<Uint8Array>, maximum: number, context: HydrationContext,
  signal: AbortSignal, charge: (bytes: number) => void): Promise<Uint8Array> {
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let complete = false;
  const abort = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener('abort', abort, { once: true });
  try {
    while (true) {
      assertCurrent(context);
      if (signal.aborted) throw new Error('media timed out');
      const chunk = await reader.read();
      assertCurrent(context);
      if (signal.aborted) throw new Error('media timed out');
      if (chunk.done) { complete = true; break; }
      total += chunk.value.byteLength;
      charge(chunk.value.byteLength);
      if (total > maximum) throw new Error('media too large');
      chunks.push(chunk.value);
    }
    return joinChunks(chunks, total);
  } finally {
    signal.removeEventListener('abort', abort);
    if (!complete) void reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

/** Provider-bound hydration after durable owner admission, before any chat model call. */
export function createInboundMediaHydrator(options: MediaHydratorOptions) {
  const transport = options.fetch ?? fetch;
  const token = options.channel === 'telegram' ? resolveTelegramCredentials(options.credentials).botToken : undefined;
  const photon = options.adapter as Partial<Pick<iMessageAdapter, 'fetchMessage'>>;
  let pendingPhotonOperation: Promise<unknown> | undefined;

  async function photonBytes(reference: PhotonMediaReference, media: Media, context: HydrationContext,
    maximum: number, charge: (bytes: number) => void): Promise<Uint8Array> {
    if (!safeId(reference.messageId) || typeof photon.fetchMessage !== 'function' || pendingPhotonOperation) {
      throw new Error('media lookup unavailable');
    }
    assertCurrent(context);
    const deadline = AbortSignal.timeout(DEADLINE_MS);
    const signal = AbortSignal.any([context.signal, deadline]);
    const operation = Promise.resolve().then(async () => {
      // Check again in the microtask so /stop can prevent even the first SDK lookup.
      assertCurrent(context);
      if (signal.aborted) throw new Error('media timed out');
      const fetched = await photon.fetchMessage!(options.providerThreadId, reference.messageId);
      assertCurrent(context);
      if (signal.aborted) throw new Error('media timed out');
      if (!fetched || fetched.id !== reference.messageId || fetched.threadId !== options.providerThreadId ||
          fetched.author.userId !== options.owner.ownerId || fetched.author.isMe !== false) throw new Error('media identity unavailable');
      const raw = asRecord(fetched.raw);
      const sender = asRecord(raw?.sender);
      if (!raw || (raw.id !== undefined && raw.id !== reference.messageId) ||
          (sender?.id !== undefined && sender.id !== options.owner.ownerId) ||
          (raw.direction !== undefined && raw.direction !== 'inbound')) throw new Error('media identity unavailable');
      const content = resolvePhotonMediaContent(raw.content, reference);
      if (!content || typeof content.stream !== 'function' || normalizeMediaMime(content.mimeType) !== normalizeMediaMime(media.mimeType) ||
          (media.sizeBytes !== undefined && (content.size ?? 0) !== media.sizeBytes) ||
          (media.filename !== undefined && (content.name ?? (reference.contentKind === 'voice' ? 'voice' : undefined)) !== media.filename)) {
        throw new Error('media content unavailable');
      }
      const stream = await content.stream();
      if (cancelled(context) || signal.aborted) {
        void stream.cancel().catch(() => undefined);
        throw new Error('media hydration cancelled');
      }
      return streamBytes(stream, maximum, context, signal, charge);
    });
    pendingPhotonOperation = operation;
    void operation.finally(() => { if (pendingPhotonOperation === operation) pendingPhotonOperation = undefined; }).catch(() => undefined);
    // SDK fetchMessage and stream() cannot be aborted. A stopped run stops waiting,
    // but the one-operation slot stays occupied until the SDK promise settles.
    let onAbort: (() => void) | undefined;
    const aborted = new Promise<never>((_, reject) => {
      onAbort = () => reject(new Error(deadline.aborted ? 'media timed out' : 'media hydration cancelled'));
      if (signal.aborted) onAbort();
      else signal.addEventListener('abort', onAbort, { once: true });
    });
    try { return await Promise.race([operation, aborted]); }
    finally { if (onAbort) signal.removeEventListener('abort', onAbort); }
  }

  return async function hydrate(messages: Inbound[], context: HydrationContext): Promise<Inbound[]> {
    assertCurrent(context);
    let count = 0;
    let consumed = 0;
    const charge = (bytes: number) => {
      consumed += bytes;
      if (consumed > MAX_INBOUND_TURN_MEDIA_BYTES) throw new Error('media too large');
    };
    const hydrated: Inbound[] = [];
    for (const message of messages) {
      assertCurrent(context);
      if (message.channel !== options.channel || message.channel !== options.owner.channel ||
          message.accountId !== options.owner.accountId || message.senderId !== options.owner.ownerId || !message.isDirect) {
        throw new Error('media owner context mismatch');
      }
      const media: Media[] = [];
      for (const item of message.media ?? []) {
        assertCurrent(context);
        if (item.omittedCount !== undefined) { media.push(item); continue; }
        if (++count > MAX_INBOUND_MEDIA_ITEMS) { media.push(omission(item, 'too_many')); continue; }
        if (item.omission) { media.push(item); continue; }
        const mime = normalizeMediaMime(item.mimeType);
        if (!mime) { media.push(omission(item, 'unsupported_type')); continue; }
        const remaining = Math.min(MAX_INBOUND_MEDIA_BYTES, MAX_INBOUND_TURN_MEDIA_BYTES - consumed);
        if (remaining <= 0 || item.oversize || (item.sizeBytes !== undefined && item.sizeBytes > remaining)) {
          media.push(omission(item, 'too_large')); continue;
        }
        try {
          let bytes: Uint8Array;
          if (item.source?.version === 1 && item.source.provider === 'telegram' && options.channel === 'telegram' && token) {
            bytes = await telegramBytes(item.source.fileId, token, transport, context, remaining, charge);
          } else if (item.source?.version === 1 && item.source.provider === 'photon' && options.channel === 'photon') {
            if (item.source.messageId !== message.eventId) { media.push(omission(item, 'unavailable')); continue; }
            bytes = await photonBytes(item.source, item, context, remaining, charge);
          } else if (item.data instanceof Uint8Array) {
            bytes = item.data;
            charge(bytes.byteLength);
          } else {
            media.push(omission(item, 'unavailable')); continue;
          }
          assertCurrent(context);
          if (bytes.byteLength === 0) { media.push(omission(item, 'empty')); continue; }
          if (bytes.byteLength > remaining) { media.push(omission(item, 'too_large')); continue; }
          if (!mediaSignatureMatches(bytes, mime)) { media.push(omission(item, 'invalid_type')); continue; }
          media.push({ ...item, mimeType: mime, data: bytes, omission: undefined });
        } catch (error) {
          if (cancelled(context)) throw new Error('media hydration cancelled');
          const reason = error instanceof Error && /too large/.test(error.message) ? 'too_large' :
            error instanceof Error && /timed out|abort|cancelled/i.test(error.message) ? 'timed_out' : 'unavailable';
          media.push(omission(item, reason));
        }
      }
      hydrated.push({ ...message, media });
    }
    return hydrated;
  };
}
