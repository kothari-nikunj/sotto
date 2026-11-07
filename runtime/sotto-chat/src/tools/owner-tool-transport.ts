import { createHash } from 'node:crypto';
import { ownerToolIdentitySchema, type OwnerToolClientOptions } from './owner-contract.js';

export class OwnerToolConfigurationError extends Error {
  constructor() { super('Owner tool configuration is invalid'); }
}

/** Only fixed failure facts are observable; an HTTP error body never enters the error message. */
export class OwnerToolTransportError extends Error {
  readonly #body: unknown;
  constructor(readonly stage: 'transport' | 'response',
      readonly category: 'network' | 'aborted' | 'http' | 'invalid_response',
      readonly httpStatus?: number, body?: unknown) {
    super('Owner tool unavailable');
    this.#body = body;
  }
  detail<T>(decode: (body: unknown) => T): T { return decode(this.#body); }
}

export function createOwnerToolTransport(options: OwnerToolClientOptions, route: string) {
  const identity = ownerToolIdentitySchema.parse(options.identity), url = new URL(options.bridgeUrl);
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname) || url.pathname !== '/mcp' ||
      url.search || url.hash || url.username || url.password || !/^[a-f0-9]{64}$/.test(options.bearer))
    throw new OwnerToolConfigurationError();
  url.pathname = route;
  const untilAbort = <T>(operation: Promise<T>, signal: AbortSignal): Promise<T> => new Promise((resolve, reject) => {
    const stop = () => reject(new Error('Owner tool unavailable'));
    if (signal.aborted) return stop();
    signal.addEventListener('abort', stop, {once: true});
    operation.then(value => {signal.removeEventListener('abort', stop); if (signal.aborted) stop(); else resolve(value);},
      error => {signal.removeEventListener('abort', stop); reject(error);});
  });
  return {identity, scopeHash: createHash('sha256').update(options.bearer).digest('hex'),
    async request(value: Record<string, unknown>, signal?: AbortSignal,
        limits: {responseLimit?: number; deadlineMs?: number; requestLimit?: number; errorBodyStatuses?: readonly number[]} = {}) {
      const {responseLimit: limit = 8192, deadlineMs = 5000, requestLimit = 65536, errorBodyStatuses = []} = limits;
      const body = JSON.stringify({...value, identity});
      if (Buffer.byteLength(body) > requestLimit) throw new Error('Owner tool request is invalid');
      const combined = signal ? AbortSignal.any([signal, AbortSignal.timeout(deadlineMs)]) : AbortSignal.timeout(deadlineMs);
      let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
      let stage: 'transport' | 'response' = 'transport';
      let category: 'network' | 'http' | 'invalid_response' = 'network';
      let httpStatus: number | undefined;
      let errorBody: unknown;
      try {
        if (combined.aborted) throw new Error();
        const response = await untilAbort((options.fetch ?? fetch)(url, {method: 'POST', redirect: 'error', cache: 'no-store',
          headers: {authorization: `Bearer ${options.bearer}`, 'content-type': 'application/json'}, body, signal: combined}), combined);
        stage = 'response'; category = 'http'; httpStatus = response.status;
        if ((![200, 409].includes(response.status) && !errorBodyStatuses.includes(response.status)) || !response.body || (response.url && response.url !== url.href) ||
            Number(response.headers.get('content-length') ?? 0) > limit) throw new Error();
        category = 'invalid_response';
        reader = response.body.getReader(); const chunks: Uint8Array[] = []; let size = 0;
        while (true) {const next = await untilAbort(reader.read(), combined); if (next.done) break;
          size += next.value.byteLength; if (size > limit) throw new Error(); chunks.push(next.value);}
        const parsed: unknown = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(Buffer.concat(chunks)));
        if (![200, 409].includes(response.status)) {
          category = 'http'; errorBody = parsed;
          throw new Error();
        }
        return {status: response.status, value: parsed};
      } catch {
        if (combined.aborted) throw new OwnerToolTransportError('transport', 'aborted');
        throw new OwnerToolTransportError(stage, category, httpStatus, errorBody);
      }
      finally {if (reader) {try {void reader.cancel().catch(() => undefined);} catch {/* closed */}
        try {reader.releaseLock();} catch {/* pending */}}}
    },
  };
}
