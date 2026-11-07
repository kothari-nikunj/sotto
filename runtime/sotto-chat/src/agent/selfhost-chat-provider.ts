import { createAnthropic } from '@ai-sdk/anthropic';
import { createOpenAI } from '@ai-sdk/openai';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { createSelfHostGoogleModels } from './selfhost-models.js';

type Credentials = { readonly apiKey: string; readonly modelId: string; readonly fetch?: typeof fetch };
export type SelfHostChatConnection = Credentials & (
  | { readonly provider: 'google' | 'openai' | 'anthropic' }
  | { readonly provider: 'qwen'; readonly deployment:
      | { readonly region: 'us' }
      | { readonly region: 'singapore' | 'beijing' | 'hongkong' | 'tokyo'; readonly workspaceId: string } }
);

/** Explicit chat transport only. Audio, search and context capacity are separate capabilities.
 * Not registered in bootstrap until the active owner-runtime migration lands.
 * Callers own retry policy (maxRetries: 0), deadlines and context admission.
 */
export function createSelfHostChatModel(connection: SelfHostChatConnection) {
  if (!connection.apiKey || connection.apiKey.length > 4096 || /[^\x21-\x7e]/u.test(connection.apiKey) ||
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(connection.modelId)) {
    throw new Error('Self-host chat configuration is invalid');
  }
  if (connection.provider === 'google') {
    return createSelfHostGoogleModels({ apiKey: connection.apiKey, chatModelId: connection.modelId,
      nativeMediaModelId: connection.modelId, fetch: connection.fetch }).chatModel;
  }
  let baseURL: string;
  let path: string;
  let credentialHeader: string;
  let credentialValue: string;
  switch (connection.provider) {
    case 'openai':
      baseURL = 'https://api.openai.com/v1'; path = '/v1/chat/completions';
      credentialHeader = 'authorization'; credentialValue = `Bearer ${connection.apiKey}`;
      break;
    case 'anthropic':
      baseURL = 'https://api.anthropic.com/v1'; path = '/v1/messages';
      credentialHeader = 'x-api-key'; credentialValue = connection.apiKey;
      break;
    case 'qwen': {
      const deployment = connection.deployment;
      if (!deployment) throw new Error('Qwen deployment is required');
      if (deployment.region === 'us') baseURL = 'https://dashscope-us.aliyuncs.com/compatible-mode/v1';
      else {
        const regions = { singapore: 'ap-southeast-1', beijing: 'cn-beijing', hongkong: 'cn-hongkong', tokyo: 'ap-northeast-1' };
        if (!Object.hasOwn(regions, deployment.region) || !/^[A-Za-z0-9][A-Za-z0-9-]{0,62}$/.test(deployment.workspaceId)) {
          throw new Error('Qwen deployment is invalid');
        }
        baseURL = `https://${deployment.workspaceId}.${regions[deployment.region]}.maas.aliyuncs.com/compatible-mode/v1`;
      }
      path = '/compatible-mode/v1/chat/completions';
      credentialHeader = 'authorization'; credentialValue = `Bearer ${connection.apiKey}`;
      break;
    }
    default: throw new Error('Self-host chat provider is unsupported');
  }
  const origin = new URL(baseURL).origin;
  const transport = connection.fetch ?? fetch;
  const guardedFetch: typeof fetch = async (input, init) => {
    const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url);
    const headers = new Headers(init?.headers);
    if (url.origin !== origin || url.pathname !== path || url.search || url.hash || url.username || url.password ||
        init?.method !== 'POST' || typeof init.body !== 'string' || headers.get(credentialHeader) !== credentialValue ||
        ['authorization', 'x-api-key', 'x-goog-api-key'].some(header => header !== credentialHeader && headers.has(header))) {
      throw new Error('Self-host chat request is invalid');
    }
    const body: unknown = JSON.parse(init.body);
    if (!body || typeof body !== 'object' || !('model' in body) || body.model !== connection.modelId) {
      throw new Error('Self-host chat model is invalid');
    }
    return transport(input, { ...init, headers, redirect: 'error' });
  };
  const options = { apiKey: connection.apiKey, baseURL, fetch: guardedFetch };
  if (connection.provider === 'anthropic') return createAnthropic(options)(connection.modelId);
  if (connection.provider === 'openai') return createOpenAI(options).chat(connection.modelId);
  return createOpenAICompatible({ ...options, name: 'qwen' }).chatModel(connection.modelId);
}
