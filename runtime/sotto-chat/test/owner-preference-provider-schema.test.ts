import assert from 'node:assert/strict';
import test from 'node:test';
import { generateText, tool } from 'ai';
import { createGoogleGenerativeAI } from '@ai-sdk/google';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { createOwnerPreferenceTools } from '../src/tools/owner-preference-tool.js';
import { createOwnerPreferenceToolClient } from '../src/tools/owner-preference-tool-client.js';

for (const provider of ['openai', 'google'] as const) {
  test(`${provider} receives object-root preference parameters through the installed provider adapter`, async () => {
    const client = createOwnerPreferenceToolClient({bridgeUrl: 'http://127.0.0.1:8080/mcp', bearer: 'c'.repeat(64),
      identity: {tenant_id: 'tenant', channel: 'photon', account_id: 'account', owner_id: 'owner', line_key: 'line',
        target: 'photon', route_mode: 'fixed', route_id: 'thread', project_fingerprint: 'a'.repeat(64)}});
    const tools = createOwnerPreferenceTools({client, journal: () => undefined,
      sourceAuthority: {read: async () => ({version: 2, complete: true, revision: 'd'.repeat(64), bridge_sources: [], google_sources: []})}});
    let body: Record<string, any> | undefined;
    const capture: typeof fetch = async (_url, init) => {
      body = JSON.parse(init!.body as string);
      throw new Error('fixture captured request without network');
    };
    const model = provider === 'openai'
      ? createOpenAICompatible({name: 'fixture', baseURL: 'http://fixture.invalid/v1', apiKey: 'synthetic', fetch: capture}).chatModel('fixture')
      : createGoogleGenerativeAI({apiKey: 'synthetic', fetch: capture})('gemini-2.5-flash');
    await assert.rejects(generateText({model, prompt: 'fixture', maxRetries: 0,
      tools: {owner_preference_change: tool({inputSchema: tools.owner_preference_change.inputSchema!})}}));
    assert.ok(body);
    const schema = provider === 'openai' ? body.tools[0].function.parameters : body.tools[0].functionDeclarations[0].parametersJsonSchema;
    assert.equal(schema.type, 'object');
    assert.equal(schema.anyOf, undefined);
    assert.deepEqual(schema.required, ['operation']);
    assert.deepEqual(Object.keys(schema.properties).sort(), ['direction', 'field', 'operation', 'value']);
    assert.equal(schema.additionalProperties, false);
    assert.deepEqual(schema.properties.operation.enum, ['add', 'remove', 'clear-tone', 'nudge-budget']);
  });
}
