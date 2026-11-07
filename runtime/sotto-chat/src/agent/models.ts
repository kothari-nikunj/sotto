import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { generateText } from 'ai';
import { AsyncLocalStorage } from 'node:async_hooks';
import { createHash, randomBytes } from 'node:crypto';
import { MAX_INBOUND_MEDIA_ITEMS, MAX_INBOUND_MEDIA_BYTES, MAX_INBOUND_TURN_MEDIA_BYTES,
  ALLOWED_INBOUND_MEDIA_TYPES, mediaSignatureMatches } from '../channels/media-policy.js';

export interface ManagedProxyOptions {
  /** The already-authorized proxy origin. No provider default endpoint is used. */
  readonly origin: string;
  readonly allowedOrigin: string;
  readonly bearer: string;
  readonly chatModelId: string;
  readonly nativeMediaModelId: string;
  readonly fetch?: typeof fetch;
}

export interface NativeMediaPart {
  readonly bytes: Uint8Array;
  readonly mediaType: 'image/png' | 'image/jpeg' | 'image/webp' | 'image/gif' |
    'audio/wav' | 'audio/mpeg' | 'audio/ogg' | 'audio/webm';
}

type ModelWorkload = 'chat' | 'unattended';
const modelWork = new AsyncLocalStorage<{ workload: ModelWorkload; session?: string }>();

/** Keep the model's scheduling label with its asynchronous turn, including tool steps. */
export function withModelWorkload<T>(workload: ModelWorkload, session: string | undefined,
                                     work: () => Promise<T>): Promise<T> {
  return modelWork.run({ workload, session }, work);
}

export class ManagedProxyAdmissionBusy extends Error {
  constructor() { super('managed model admission busy'); }
}

export class ManagedProxyRequestTooLarge extends Error {
  constructor() { super('managed model request too large'); }
}

export class ManagedProxyBudgetExhausted extends Error {
  constructor(readonly period: 'calendar_month' | 'lifetime' | 'unknown' = 'unknown') {
    super('managed model spending allowance exhausted');
  }
}

export class ManagedProxyUpstreamUnavailable extends Error {
  constructor(readonly status: 502 | 503 | 504) {
    super('managed model service is temporarily unavailable');
  }
}

function checkedOrigin(value: string, allowed: string): string {
  const url = new URL(value);
  if (url.origin !== allowed || url.pathname !== '/' || url.search || url.hash || url.username || url.password ||
      (url.protocol !== 'https:' && !(url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)))) {
    throw new Error('managed model proxy origin is not approved');
  }
  return url.origin;
}

function modelId(value: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value)) throw new Error('model ID is invalid');
  return value;
}

/**
 * Only these two proxy routes can be reached by the injected transport. The native
 * adapter is deliberately restricted to nonstreaming, tool-free generateContent.
 */
export function createManagedProxyModels(options: ManagedProxyOptions) {
  const origin = checkedOrigin(options.origin, options.allowedOrigin);
  const bearer = options.bearer;
  if (!bearer || /[\r\n]/.test(bearer)) throw new Error('managed proxy bearer is required');
  const chatId = modelId(options.chatModelId);
  const mediaId = modelId(options.nativeMediaModelId);
  const transport = options.fetch ?? fetch;
  const guardedFetch: typeof fetch = async (input, init) => {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    if (url.origin !== origin || url.search || url.hash || init?.method !== 'POST') {
      throw new Error('model request escaped the managed proxy');
    }
    const chatPath = '/openai/v1/chat/completions';
    const nativePath = `/native/v1beta/models/${mediaId}:generateContent`;
    if (url.pathname !== chatPath && url.pathname !== nativePath) throw new Error('model request path is not allowed');
    const headers = new Headers(init?.headers);
    if (headers.get('authorization') !== `Bearer ${bearer}`) throw new Error('model request bearer is missing');
    const attribution = modelWork.getStore();
    headers.set('X-Sotto-Workload', attribution?.workload ?? 'chat');
    headers.set('X-Sotto-Operation', randomBytes(32).toString('hex'));
    headers.set('X-Sotto-Attempt', '1');
    if (attribution?.session) headers.set('X-Sotto-Session', createHash('sha256').update(attribution.session).digest('hex'));
    else headers.delete('X-Sotto-Session');
    if (url.pathname === nativePath) {
      if (typeof init?.body !== 'string') throw new Error('native media request body is invalid');
      const body: unknown = JSON.parse(init.body);
      if (!body || typeof body !== 'object' || 'tools' in body || 'toolConfig' in body || 'tool_config' in body) {
        throw new Error('native media request cannot include tools');
      }
    }
    const response = await transport(input, { ...init, headers, redirect: 'error' });
    if (response.status === 429 && response.headers.get('X-Sotto-Admission') === 'busy') {
      throw new ManagedProxyAdmissionBusy();
    }
    if (response.status === 413) throw new ManagedProxyRequestTooLarge();
    if (url.pathname === chatPath && (response.status === 502 || response.status === 503 || response.status === 504)) {
      void response.body?.cancel().catch(() => undefined);
      throw new ManagedProxyUpstreamUnavailable(response.status);
    }
    if (response.status === 402) {
      const reader = response.clone().body?.getReader();
      if (reader) {
        let size = 0;
        const chunks: Uint8Array[] = [];
        try {
          while (true) {
            const part = await reader.read();
            if (part.done) break;
            size += part.value.byteLength;
            if (size > 4096) break;
            chunks.push(part.value);
          }
          if (size <= 4096) {
            const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
            if (body?.error?.code === 'sotto_budget_exhausted' && body?.error?.type === 'budget_exhausted') {
              throw new ManagedProxyBudgetExhausted(body.error.period === 'calendar_month' ? 'calendar_month'
                : body.error.period === 'lifetime' ? 'lifetime' : 'unknown');
            }
          }
        } catch (error) {
          if (error instanceof ManagedProxyBudgetExhausted) throw error;
        } finally { void reader.cancel().catch(() => {}); }
      }
    }
    return response;
  };
  const openai = createOpenAICompatible({
    name: 'sotto-managed-proxy', baseURL: `${origin}/openai/v1`, apiKey: bearer, fetch: guardedFetch,
  });
  const google = createGoogleGenerativeAI({
    baseURL: `${origin}/native/v1beta`, apiKey: 'managed-proxy',
    headers: { Authorization: `Bearer ${bearer}` }, fetch: guardedFetch,
  });
  const nativeMediaModel = google(mediaId);
  return {
    chatModel: openai.chatModel(chatId),
    nativeMediaModel,
    async generateNativeMedia(prompt: string, media: readonly NativeMediaPart[] = [], signal?: AbortSignal) {
      if (!prompt.trim() || prompt.length > 16_384) throw new Error('native media prompt is invalid');
      if (media.length > MAX_INBOUND_MEDIA_ITEMS || media.some(part => !(part.bytes instanceof Uint8Array) || part.bytes.byteLength < 1 ||
          part.bytes.byteLength > MAX_INBOUND_MEDIA_BYTES || !ALLOWED_INBOUND_MEDIA_TYPES.has(part.mediaType) ||
          !mediaSignatureMatches(part.bytes, part.mediaType)) ||
          media.reduce((total, part) => total + part.bytes.byteLength, 0) > MAX_INBOUND_TURN_MEDIA_BYTES) {
        throw new Error('native media exceeds its allowed bounds');
      }
      const content = [
        { type: 'text' as const, text: prompt },
        ...media.map(part => ({ type: 'file' as const, data: part.bytes, mediaType: part.mediaType })),
      ];
      const result = await withModelWorkload('chat', modelWork.getStore()?.session,
        () => generateText({ model: nativeMediaModel, messages: [{ role: 'user', content }],
                           abortSignal: signal, maxRetries: 0 }));
      return { text: result.text, files: result.files, usage: result.usage };
    },
  };
}
