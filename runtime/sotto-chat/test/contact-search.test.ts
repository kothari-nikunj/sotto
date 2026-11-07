import test from 'node:test';
import assert from 'node:assert/strict';
import { RequestContext } from '@mastra/core/request-context';
import { createBridgeMcpClients } from '../src/agent/bridge.js';
import { searchContactResult } from '../src/agent/contact-search.js';
import type { SourceAuthority } from '../src/agent/source-authority.js';

const wrapped = (contacts: unknown[]) => ({content: [{type: 'text', text: JSON.stringify({contacts})}]});
const unpack = (value: unknown) => JSON.parse((value as {content: [{text: string}]}).content[0].text);

test('targeted contact results keep same-name candidates and omit unrelated private notes', () => {
  const rows = Array.from({length: 2000}, (_, i) => ({name: `Other Contact ${i}`,
    phones: ['+15555550000'], emails: [`other${i}@example.test`], notes: 'private '.repeat(100)}));
  rows.push({name: 'Dana Reed', phones: ['+14155550123'], emails: ['dana@one.test'], notes: 'private first'});
  rows.push({name: 'Dana Reed', phones: ['+442055550123'], emails: ['dana@two.test'], notes: 'private second'});
  const result = searchContactResult(wrapped(rows), 'Dana Reed', 10);
  assert.equal(unpack(result).contacts.length, 2);
  assert.equal(unpack(result).matched, 2);
  assert.equal(unpack(result).truncated, false);
  assert.ok(JSON.stringify(result).length < 1000);
  assert.ok(!JSON.stringify(result).includes('private'));
  assert.equal(unpack(searchContactResult(wrapped(rows), 'dana@two.test', 10)).contacts[0].phones[0], '+442055550123');
  assert.equal(unpack(searchContactResult(wrapped(rows), '+1 (415) 555-0123', 10)).contacts.length, 1);
  assert.equal(unpack(searchContactResult(wrapped(rows), 'Dana Reed', 1)).truncated, true);
  assert.equal(unpack(searchContactResult(wrapped(rows), 'missing', 10)).matched, 0);
});

test('oversized identities are omitted honestly rather than changed into another identifier', () => {
  const result = unpack(searchContactResult(wrapped([{name: 'Dana Reed', phones: [], emails: ['x'.repeat(900)]}]), 'Dana', 10));
  assert.deepEqual(result.contacts, []);
  assert.equal(result.matched, 1);
  assert.equal(result.truncated, true);
  assert.throws(() => searchContactResult(wrapped([{name: 'Dana', phones: 'bad', emails: []}]), 'Dana', 10));
});

test('chat contact search uses the existing read, rechecks consent, and leaves background schema intact', async () => {
  const revision = 'a'.repeat(64);
  let state: SourceAuthority = {version: 2, complete: true, revision, bridge_sources: ['contacts'], google_sources: []};
  let revoke = false;
  let calls = 0;
  const bridge = createBridgeMcpClients({url: 'http://127.0.0.1:12345/mcp', readBearer: 'fixture',
    sourceAuthority: {read: async () => state}, fetch: async input => {
      const body = await new Request(input).json() as {id: number; method: string; params?: {name: string; arguments: unknown}};
      let result: unknown;
      if (body.method === 'initialize') result = {protocolVersion: '2025-06-18'};
      else if (body.method === 'tools/list') result = {tools: [{name: 'get_contacts', inputSchema: {
        type: 'object', properties: {}, additionalProperties: false}}]};
      else {
        assert.equal(body.params?.name, 'get_contacts');
        assert.deepEqual(body.params?.arguments, {});
        calls++;
        if (revoke) state = {...state, revision: 'b'.repeat(64), bridge_sources: []};
        result = wrapped([{name: 'Dana Reed', phones: ['+15555550123'], emails: []}]);
      }
      return Response.json({jsonrpc: '2.0', id: body.id, result});
    }});
  try {
    const tools = await bridge.chatTools({sourceRevision: revision});
    assert.ok(tools.bridge_get_contacts); // Birthdays and explicit whole-book tasks still work.
    assert.ok((await bridge.unattendedTools({sourceRevision: revision})).bridge_get_contacts);
    const requestContext = new RequestContext(); requestContext.set('sourceRevision', revision);
    const observe = {span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {}};
    const execute = tools.bridge_search_contacts!.execute!;
    const result = await execute({query: 'Dana', limit: 10}, {requestContext, observe});
    assert.equal(unpack(result).contacts.length, 1);
    revoke = true;
    await assert.rejects(execute({query: 'Dana', limit: 10}, {requestContext, observe}), /withheld/);
    assert.equal(calls, 2);
    await assert.rejects(execute({query: 'Dana', limit: 10}, {requestContext, observe}), /withheld/);
    assert.equal(calls, 2);
  } finally { await bridge.close(); }
});

for (const payload of [{contacts: [], status: 'disabled'}, {contacts: [], error: 'private path'},
  {contacts: [], status: 'partial'}, {contacts: [], error: null}]) {
  test(`failed contact read never becomes a complete no-match: ${JSON.stringify(payload)}`, () => {
    assert.throws(() => searchContactResult({content: [{type: 'text', text: JSON.stringify(payload)}]}, 'Dana', 10),
      error => error instanceof Error && error.message === 'Contacts read is unavailable');
  });
}

test('contact matching normalizes accents without changing the returned identifiers', () => {
  const name = 'Jose\u0301 Nota\u0301rio';
  const result = unpack(searchContactResult(wrapped([{name, phones: [], emails: ['jose@example.test']}]), 'José Notário', 10));
  assert.equal(result.matched, 1);
  assert.equal(result.contacts[0].name, name);
  assert.equal(unpack(searchContactResult(wrapped([{name: 'José Notário', phones: [], emails: []}]), name, 10)).matched, 1);
});

test('the actual guarded source boundary still rejects an oversized full Contacts response', async () => {
  const {guardedSourceCall} = await import('../src/agent/source-tools.js');
  const revision = 'a'.repeat(64);
  const reader = {read: async (): Promise<SourceAuthority> => ({version: 2, complete: true, revision,
    bridge_sources: ['contacts'], google_sources: []})};
  await assert.rejects(guardedSourceCall({name:'get_contacts',args:{},revision,reader,
    invoke:async () => wrapped([{name:'Dana Reed',phones:[],emails:[],notes:'x'.repeat(1_048_576)}])}), /exceeds its bound/);
});
