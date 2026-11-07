import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { generateText } from 'ai';
import { MAX_INBOUND_MEDIA_ITEMS, MAX_INBOUND_MEDIA_BYTES, MAX_INBOUND_TURN_MEDIA_BYTES,
  ALLOWED_INBOUND_MEDIA_TYPES, mediaSignatureMatches } from '../channels/media-policy.js';
import { type NativeMediaPart, withModelWorkload } from './models.js';

export interface SelfHostGoogleOptions {
  readonly apiKey: string;
  readonly chatModelId: string;
  readonly nativeMediaModelId: string;
  readonly fetch?: typeof fetch;
}

/** Explicit self-host model credentials; never a source grant or environment fallback. */
export function createSelfHostGoogleModels(options: SelfHostGoogleOptions) {
  const origin = 'https://generativelanguage.googleapis.com';
  if (!options.apiKey || options.apiKey.length > 4096 || /[^\x21-\x7e]/u.test(options.apiKey) ||
      ![options.chatModelId, options.nativeMediaModelId].every(id => /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(id))) {
    throw new Error('Self-host model configuration is invalid');
  }
  const transport = options.fetch ?? fetch;
  function provider(id: string, nativeOnly: boolean) {
    const guardedFetch: typeof fetch = async (input, init) => {
      const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
      const generate = url.pathname === `/v1beta/models/${id}:generateContent` && !url.search;
      const stream = !nativeOnly && url.pathname === `/v1beta/models/${id}:streamGenerateContent` && url.search === '?alt=sse';
      const headers = new Headers(init?.headers);
      if (url.origin !== origin || url.username || url.password || url.hash ||
          init?.method !== 'POST' || !(generate || stream) ||
          headers.get('x-goog-api-key') !== options.apiKey || headers.has('authorization') || headers.has('x-api-key') ||
          typeof init.body !== 'string') throw new Error('Self-host model request is invalid');
      if (nativeOnly) {
        const body: unknown = JSON.parse(init.body);
        if (!body || typeof body !== 'object' || 'tools' in body || 'toolConfig' in body || 'tool_config' in body) {
          throw new Error('Native media request cannot include tools');
        }
      }
      return transport(input, { ...init, headers, redirect: 'error' });
    };
    return createGoogleGenerativeAI({ baseURL: `${origin}/v1beta`, apiKey: options.apiKey, fetch: guardedFetch })(id);
  }
  const chatModel = provider(options.chatModelId, false);
  const nativeMediaModel = provider(options.nativeMediaModelId, true);
  return {
    chatModel,
    nativeMediaModel,
    async generateNativeMedia(prompt: string, media: readonly NativeMediaPart[] = [], signal?: AbortSignal) {
      if (!prompt.trim() || prompt.length > 16_384) throw new Error('Native media prompt is invalid');
      if (media.length > MAX_INBOUND_MEDIA_ITEMS || media.some(part => !(part.bytes instanceof Uint8Array) || part.bytes.byteLength < 1 ||
          part.bytes.byteLength > MAX_INBOUND_MEDIA_BYTES || !ALLOWED_INBOUND_MEDIA_TYPES.has(part.mediaType) ||
          !mediaSignatureMatches(part.bytes, part.mediaType)) ||
          media.reduce((total, part) => total + part.bytes.byteLength, 0) > MAX_INBOUND_TURN_MEDIA_BYTES) {
        throw new Error('Native media exceeds its allowed bounds');
      }
      const content = [{ type: 'text' as const, text: prompt },
        ...media.map(part => ({ type: 'file' as const, data: part.bytes, mediaType: part.mediaType }))];
      const result = await withModelWorkload('chat', undefined,
        () => generateText({ model: nativeMediaModel, messages: [{ role: 'user', content }],
                           abortSignal: signal, maxRetries: 0 }));
      return { text: result.text, files: result.files, usage: result.usage };
    },
  };
}
