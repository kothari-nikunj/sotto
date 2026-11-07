import test from 'node:test';
import assert from 'node:assert/strict';
import { createBridgeMcpClients } from '../src/agent/bridge.js';
import { RequestContext } from '@mastra/core/request-context';
import type { SourceAuthority } from '../src/agent/source-authority.js';

test('MCP Bridge clients refresh per turn and isolate interactive send capability', async () => {
  let includeExtra = false;
  const seen: Array<{ method: string; sendHeader: string | null; auth: string | null; tool?: string }> = [];
  const transport: typeof fetch = async input => {
    const request = new Request(input);
    const body = await request.json() as { id?: number; method: string; params?: { name?: string } };
    seen.push({ method: body.method, tool: body.params?.name, sendHeader: request.headers.get('X-Sotto-Chat-Send'), auth: request.headers.get('authorization') });
    if (body.method === 'notifications/initialized') return new Response(null, { status: 202 });
    let result: unknown;
    if (body.method === 'initialize') result = { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fixture-bridge', version: '1' } };
    else if (body.method === 'tools/list') result = { tools: [
      { name: 'health', description: 'Health', inputSchema: { type: 'object', properties: {} } },
      { name: 'send_message', description: 'Send', inputSchema: { type: 'object', properties: {} } },
      ...(includeExtra ? [{ name: 'read_local', description: 'Read', inputSchema: { type: 'object', properties: {} } }] : []),
    ] };
    else if (body.method === 'tools/call') {
      if (body.params?.name === 'send_message' && !request.headers.get('X-Sotto-Chat-Send')) {
        return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, error: { code: -32002, message: 'denied' } }), { headers: { 'content-type': 'application/json' } });
      }
      result = { content: [{ type: 'text', text: 'ok' }] };
    } else throw new Error(`unexpected fixture method ${body.method}`);
    return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }), { headers: { 'content-type': 'application/json' } });
  };
  const bridge = createBridgeMcpClients({
    url: 'http://127.0.0.1:43210/mcp', readBearer: 'read-secret',
    interactiveSendBearer: 'send-secret', fetch: transport,
    executeInteractiveSend: async call => {
      assert.equal(call.runId, 'run-1');
      assert.equal(call.ownerRequestKey, 'owner-event-1');
      return call.invoke({}, () => {});
    },
  });
  try {
    const [chat, background, readOnlyChat] = await Promise.all([
      bridge.chatTools({ allowSend: true }), bridge.unattendedTools(), bridge.chatTools(),
    ]);
    assert.ok(chat.bridge_send_message);
    assert.equal(background.bridge_send_message, undefined);
    assert.equal(readOnlyChat.bridge_send_message, undefined);
    assert.ok(background.bridge_health);
    assert.ok(seen.some(call => call.method === 'tools/list' && call.sendHeader === 'send-secret'));
    assert.ok(seen.some(call => call.method === 'tools/list' && call.sendHeader === null));
    includeExtra = true;
    assert.ok((await bridge.unattendedTools()).bridge_read_local, 'each turn re-lists tools');
    const send = chat.bridge_send_message?.execute;
    const health = background.bridge_health?.execute;
    assert.ok(send);
    assert.ok(health);
    const observe = { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} };
    await assert.rejects(send({}, { observe }), /authority/);
    const requestContext = new RequestContext();
    requestContext.set('mode', 'chat'); requestContext.set('allowSend', true);
    requestContext.set('runId', 'run-1'); requestContext.set('ownerRequestKey', 'owner-event-1');
    await send({}, { observe, requestContext });
    await health({}, { observe });
    assert.ok(seen.some(call => call.method === 'tools/call' && call.tool === 'send_message' && call.sendHeader === 'send-secret'));
    assert.ok(seen.some(call => call.method === 'tools/call' && call.tool === 'health' && call.sendHeader === null));
    assert.ok(seen.every(call => call.auth === 'Bearer read-secret'));
  } finally { await bridge.close(); }
});

test('Bridge MCP endpoint rejects remote or ambiguous URLs', () => {
  for (const url of ['https://remote.example/mcp', 'http://127.0.0.1:1/mcp?token=x', 'http://localhost:1/mcp', 'http://127.0.0.1:1/other']) {
    assert.throws(() => createBridgeMcpClients({ url, readBearer: 'read', interactiveSendBearer: 'send' }));
  }
});

test('close aborts stalled discovery even when fetch ignores its signal', async () => {
  for (const stalledMethod of ['initialize', 'tools/list']) {
    let contacted!: () => void;
    const started = new Promise<void>(resolve => { contacted = resolve; });
    const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
      fetch: async input => {
        const body = await new Request(input).json() as { id: number; method: string };
        if (body.method === stalledMethod) { contacted(); return new Promise<Response>(() => {}); }
        return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id,
          result: { protocolVersion: '2025-06-18' } }));
      } });
    const discovery = bridge.chatTools();
    await started;
    await bridge.close();
    await assert.rejects(discovery, /unavailable/);
  }
});

test('abort settles an unresponsive send without another provider contact', async () => {
  let contacted!: () => void;
  const started = new Promise<void>(resolve => { contacted = resolve; });
  const controller = new AbortController();
  let contacts = 0;
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    interactiveSendBearer: 'send',
    executeInteractiveSend: async call => {
      const first = call.invoke({}, () => {});
      await started;
      controller.abort();
      await assert.rejects(first, /cannot be retried/);
      await assert.rejects(call.invoke({}, () => {}), /cannot be retried/);
      return { held: true };
    },
    fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string };
      if (body.method === 'tools/call') { contacts++; contacted(); return new Promise<Response>(() => {}); }
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id,
        result: body.method === 'initialize' ? { protocolVersion: '2025-06-18' }
          : { tools: [{ name: 'send_message', inputSchema: { type: 'object', properties: {} } }] } }));
    } });
  try {
    const tool = (await bridge.chatTools({ allowSend: true })).bridge_send_message!;
    const requestContext = new RequestContext();
    requestContext.set('mode', 'chat'); requestContext.set('allowSend', true);
    requestContext.set('runId', 'run'); requestContext.set('ownerRequestKey', 'event');
    assert.deepEqual(await tool.execute!({}, { requestContext, abortSignal: controller.signal,
      observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } }), { held: true });
    assert.equal(contacts, 1);
  } finally { await bridge.close(); }
});

test('approved send arguments that fail the advertised schema are refused before the contact fence', async () => {
  let contacts = 0, fenced = 0;
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    interactiveSendBearer: 'send', executeInteractiveSend: async call => call.invoke({ to: 7 }, () => { fenced++; }),
    fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string };
      if (body.method === 'tools/call') contacts++;
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id,
        result: body.method === 'initialize' ? { protocolVersion: '2025-06-18' }
          : { tools: [{ name: 'send_message', inputSchema: { type: 'object', required: ['to'], properties: { to: { type: 'string' } } } }] } }));
    } });
  try {
    const tool = (await bridge.chatTools({ allowSend: true })).bridge_send_message!;
    const requestContext = new RequestContext();
    requestContext.set('mode', 'chat'); requestContext.set('allowSend', true);
    requestContext.set('runId', 'run'); requestContext.set('ownerRequestKey', 'event');
    await assert.rejects(tool.execute!({ to: '+15555550123' }, { requestContext, abortSignal: new AbortController().signal,
      observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } }), /advertised schema/);
    assert.equal(fenced, 0, 'a refused send must not be journaled as contacted');
    assert.equal(contacts, 0);
  } finally { await bridge.close(); }
});

test('an already aborted send makes no provider contact', async () => {
  const controller = new AbortController();
  let contacts = 0;
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    interactiveSendBearer: 'send', executeInteractiveSend: async call => call.invoke({}, () => {}),
    fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string };
      if (body.method === 'tools/call') contacts++;
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id,
        result: body.method === 'initialize' ? { protocolVersion: '2025-06-18' }
          : { tools: [{ name: 'send_message', inputSchema: { type: 'object', properties: {} } }] } }));
    } });
  try {
    const tool = (await bridge.chatTools({ allowSend: true })).bridge_send_message!;
    const requestContext = new RequestContext();
    requestContext.set('mode', 'chat'); requestContext.set('allowSend', true);
    requestContext.set('runId', 'run'); requestContext.set('ownerRequestKey', 'event');
    controller.abort();
    await assert.rejects(tool.execute!({}, { requestContext, abortSignal: controller.signal,
      observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } }), /cannot be retried/);
    assert.equal(contacts, 0);
  } finally { await bridge.close(); }
});

test('advertised input and output schemas stay enforced; unsupported schemas fail closed', async () => {
  let unsupported = false;
  let malformedOutput = false;
  let contacts = 0;
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string };
      const result = body.method === 'initialize'
        ? { protocolVersion: '2025-06-18' }
        : body.method === 'tools/list'
          ? { tools: [{ name: 'health', inputSchema: unsupported ? { type: 'unsupported' } : {
            type: 'object', properties: { probe: { type: 'string', minLength: 3 } },
            required: ['probe'], additionalProperties: false },
          outputSchema: { type: 'object', properties: { ok: { type: 'boolean' } },
            required: ['ok'], additionalProperties: false } }] }
          : (contacts++, { structuredContent: malformedOutput ? { ok: 'wrong' } : { ok: true },
            content: [{ type: 'text', text: 'ok' }] });
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }),
        { headers: { 'content-type': 'application/json' } });
    } });
  try {
    const tool = (await bridge.chatTools()).bridge_health!;
    const context = { observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } };
    assert.equal((await tool.execute!({ probe: 'x' }, context) as { error?: boolean }).error, true);
    assert.equal((await tool.execute!({ probe: 'good', extra: true }, context) as { error?: boolean }).error, true);
    assert.equal(contacts, 0);
    assert.deepEqual(await tool.execute!({ probe: 'good' }, context), { ok: true });
    malformedOutput = true;
    await assert.rejects(tool.execute!({ probe: 'good' }, context), /advertised schema/);
    unsupported = true;
    await assert.rejects(bridge.chatTools(), /discovery failed/);
  } finally { await bridge.close(); }
});

test('interactive MCP send cannot be enabled without an action wrapper', async () => {
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read', interactiveSendBearer: 'send' });
  try { assert.throws(() => bridge.chatTools({ allowSend: true }), /action gate/); }
  finally { await bridge.close(); }
});

test('read-only Bridge requires no send credential and cannot borrow authority from a wrapper', async () => {
  const headers: Array<string | null> = [];
  const bridge = createBridgeMcpClients({
    url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    executeInteractiveSend: async call => call.invoke({}, () => {}),
    fetch: async input => {
      const request = new Request(input);
      headers.push(request.headers.get('X-Sotto-Chat-Send'));
      const body = await request.json() as { id?: number; method: string };
      if (body.method === 'notifications/initialized') return new Response(null, { status: 202 });
      const result = body.method === 'initialize'
        ? { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } }
        : { tools: [{ name: 'send_message', inputSchema: { type: 'object', properties: {} } }] };
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }), { headers: { 'content-type': 'application/json' } });
    },
  });
  try {
    assert.equal((await bridge.chatTools()).bridge_send_message, undefined);
    assert.equal((await bridge.unattendedTools()).bridge_send_message, undefined);
    assert.throws(() => bridge.chatTools({ allowSend: true }), /capability/);
    assert.ok(headers.length > 0);
    assert.ok(headers.every(value => value === null));
  } finally { await bridge.close(); }
});

test('real MCP source wrappers use per-run authority and sanitize structured and text result channels', async () => {
  let state: SourceAuthority = { version: 2, complete: true, revision: 'a'.repeat(64), bridge_sources: ['imessage'], google_sources: [] };
  let structured = false;
  let revoke = false;
  let foreignField = false;
  const requests: Array<{ name?: string; arguments?: unknown }> = [];
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    sourceAuthority: { read: async () => state },
    fetch: async input => {
      const request = new Request(input);
      const body = await request.json() as { id: number; method: string; params: { name?: string; arguments?: unknown } };
      if (body.method === 'notifications/initialized') return new Response(null, { status: 202 });
      let result: unknown;
      if (body.method === 'initialize') result = { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } };
      else if (body.method === 'tools/list') result = { tools: ['read_local', 'get_contacts', 'new_unknown_tool'].map(name => ({ name,
        inputSchema: { type: 'object', properties: { sources: { type: 'array', items: { type: 'string' } } } } })) };
      else {
        requests.push(body.params);
        if (revoke) state = { ...state, revision: 'b'.repeat(64) };
        const payload = { imessage: ['allowed text'],
          _bridge_request_started_at: '2026-10-05T17:36:16.123456+00:00',
          ...(foreignField ? { whatsapp: ['unconsented text'] } : {}) };
        result = structured
          ? { structuredContent: payload, content: [{ type: 'text', text: 'UNVALIDATED ALTERNATE CHANNEL' }] }
          : { content: [{ type: 'text', text: JSON.stringify(payload) }] };
      }
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }), { headers: { 'content-type': 'application/json' } });
    },
  });
  try {
    assert.deepEqual(await bridge.chatTools(), {}, 'missing request baseline exposes no source tools');
    const tools = await bridge.chatTools({ sourceRevision: state.revision! });
    assert.deepEqual(Object.keys(tools), ['bridge_read_local']);
    const requestContext = new RequestContext(); requestContext.set('sourceRevision', state.revision);
    const observe = { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} };
    const execute = tools.bridge_read_local!.execute!;
    for (const mode of [false, true]) {
      structured = mode;
      const result = await execute({}, { observe, requestContext });
      assert.ok(JSON.stringify(result).includes('allowed text'));
      assert.equal(JSON.stringify(result).includes('ALTERNATE'), false);
      assert.equal(JSON.stringify(result).includes('_bridge_request_started_at'), false,
        'receiver-only ordering marker must not enter model-visible content');
    }
    assert.deepEqual(requests[0]?.arguments, { sources: ['imessage'] });
    foreignField = true;
    await assert.rejects(execute({}, { observe, requestContext }), /withheld/,
      'unrelated unconsented source content must remain withheld');
    foreignField = false;
    revoke = true;
    await assert.rejects(execute({}, { observe, requestContext }), /withheld/);
    const before = requests.length;
    await assert.rejects(execute({}, { observe, requestContext }), /withheld/);
    assert.equal(requests.length, before, 'old cached tool cannot bypass the new authority revision');
  } finally { await bridge.close(); }
});

test('a supplied listing snapshot saves one probe while execution still checks live authority', async () => {
  const revision = 'a'.repeat(64);
  let state: SourceAuthority = { version: 2, complete: true, revision, bridge_sources: ['imessage'], google_sources: [] };
  let reads = 0, listings = 0, contacts = 0;
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    sourceAuthority: { read: async () => { reads++; return state; } },
    fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string };
      if (body.method === 'tools/list') listings++;
      if (body.method === 'tools/call') contacts++;
      const result = body.method === 'initialize' ? { protocolVersion: '2025-06-18' }
        : body.method === 'tools/list' ? { tools: [{ name: 'read_local', inputSchema: { type: 'object', properties: {} } }] }
        : { content: [{ type: 'text', text: '{}' }] };
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }));
    } });
  try {
    assert.deepEqual(await bridge.chatTools({ sourceRevision: 'b'.repeat(64), listingAuthority: state }), {});
    assert.deepEqual(await bridge.chatTools({ sourceRevision: revision, listingAuthority: { ...state, complete: false } }), {});
    assert.equal(reads, 0); assert.equal(listings, 0);
    const tools = await bridge.chatTools({ sourceRevision: revision, listingAuthority: state });
    assert.ok(tools.bridge_read_local); assert.equal(reads, 0); assert.equal(listings, 1);
    await bridge.chatTools({ sourceRevision: revision });
    assert.equal(reads, 1, 'direct bridge callers still obtain their own authority snapshot');
    state = { ...state, revision: 'b'.repeat(64) };
    const requestContext = new RequestContext(); requestContext.set('sourceRevision', revision);
    await assert.rejects(tools.bridge_read_local!.execute!({}, { requestContext,
      observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } }), /withheld/);
    assert.equal(contacts, 0, 'revocation refuses the stale listed tool before provider contact');
  } finally { await bridge.close(); }
});

test('interactive send wrapper cannot make a second HTTP contact within one action', async () => {
  let contacts = 0;
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read', interactiveSendBearer: 'send',
    executeInteractiveSend: async call => {
      const result = await call.invoke({}, () => {});
      // Also protects SDK reconnect/retry: both calls inherit the same action scope.
      await assert.rejects(call.invoke({}, () => {}));
      return result;
    },
    fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string };
      if (body.method === 'notifications/initialized') return new Response(null, { status: 202 });
      const result = body.method === 'initialize'
        ? { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } }
        : body.method === 'tools/list'
          ? { tools: [{ name: 'send_message', inputSchema: { type: 'object', properties: {} } }] }
          : (contacts++, { content: [{ type: 'text', text: 'accepted' }] });
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }), { headers: { 'content-type': 'application/json' } });
    },
  });
  try {
    const tools = await bridge.chatTools({ allowSend: true });
    const requestContext = new RequestContext();
    requestContext.set('mode', 'chat'); requestContext.set('allowSend', true);
    requestContext.set('runId', 'run'); requestContext.set('ownerRequestKey', 'event');
    await tools.bridge_send_message!.execute!({}, { requestContext,
      observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } });
    assert.equal(contacts, 1);
  } finally { await bridge.close(); }
});

test('lost send response cannot trigger a second provider contact', async () => {
  let contacts = 0;
  let initializations = 0;
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read', interactiveSendBearer: 'send',
    executeInteractiveSend: async call => call.invoke({}, () => {}),
    fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string };
      if (body.method === 'notifications/initialized') return new Response(null, { status: 202 });
      if (body.method === 'tools/call') {
        contacts++;
        // The provider may have accepted this action before its response was lost.
        throw new TypeError('fetch failed');
      }
      const result = body.method === 'initialize'
        ? (initializations++, { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } })
        : { tools: [{ name: 'send_message', inputSchema: { type: 'object', properties: {} } }] };
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }), { headers: { 'content-type': 'application/json' } });
    },
  });
  try {
    const tools = await bridge.chatTools({ allowSend: true });
    const requestContext = new RequestContext();
    requestContext.set('mode', 'chat'); requestContext.set('allowSend', true);
    requestContext.set('runId', 'run'); requestContext.set('ownerRequestKey', 'event');
    await assert.rejects(tools.bridge_send_message!.execute!({}, { requestContext,
      observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } }), /cannot be retried/);
    assert.equal(initializations, 1, 'the direct transport never reconnects or retries a send');
    assert.equal(contacts, 1, 'the lost response did not repeat provider contact');
  } finally { await bridge.close(); }
});

test('interactive wrapper sends only factory-supplied arguments, not model request IDs', async () => {
  let wireArgs: unknown;
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    interactiveSendBearer: 'send', executeInteractiveSend: async call => {
      assert.deepEqual(call.args, { channel: 'auto', to: '+12065550199', body: 'model text', request_id: 'model-key' });
      return call.invoke({ channel: 'imessage', to: '+12065550123', body: 'approved text', request_id: 'factory-key' }, () => {});
    }, fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string; params?: { arguments?: unknown } };
      if (body.method === 'notifications/initialized') return new Response(null, { status: 202 });
      const result = body.method === 'initialize'
        ? { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } }
        : body.method === 'tools/list'
          ? { tools: [{ name: 'send_message', inputSchema: { type: 'object', properties: {} } }] }
          : (wireArgs = body.params?.arguments, { structuredContent: { status: 'sent' } });
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }), { headers: { 'content-type': 'application/json' } });
    } });
  try {
    const tools = await bridge.chatTools({ allowSend: true });
    const requestContext = new RequestContext();
    requestContext.set('mode', 'chat'); requestContext.set('allowSend', true);
    requestContext.set('runId', 'run'); requestContext.set('ownerRequestKey', 'event');
    await tools.bridge_send_message!.execute!({ channel: 'auto', to: '+12065550199', body: 'model text', request_id: 'model-key' },
      { requestContext, observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } });
    assert.deepEqual(wireArgs, { channel: 'imessage', to: '+12065550123', body: 'approved text', request_id: 'factory-key' });
  } finally { await bridge.close(); }
});

test('fresh source discovery can include a separately gated send without treating source grants as approval', async () => {
  const authority: SourceAuthority = { version: 2, complete: true, revision: 'a'.repeat(64), bridge_sources: ['imessage'], google_sources: [] };
  let approved = 0;
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    interactiveSendBearer: 'send', sourceAuthority: { read: async () => authority },
    executeInteractiveSend: async call => { approved++; return call.invoke({ channel: 'auto', to: '+12065550123', body: 'approved' }, () => {}); },
    fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string };
      if (body.method === 'notifications/initialized') return new Response(null, { status: 202 });
      const result = body.method === 'initialize'
        ? { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } }
        : body.method === 'tools/list'
          ? { tools: ['send_message', 'read_local'].map(name => ({ name, inputSchema: { type: 'object', properties: {} } })) }
          : { structuredContent: { status: 'sent' } };
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }), { headers: { 'content-type': 'application/json' } });
    } });
  try {
    assert.deepEqual(await bridge.chatTools({ allowSend: true }), {}, 'missing source revision cannot select tools');
    const readOnly = await bridge.chatTools({ sourceRevision: authority.revision! });
    assert.equal(readOnly.bridge_send_message, undefined);
    assert.ok(readOnly.bridge_read_local);
    const mixed = await bridge.chatTools({ allowSend: true, sourceRevision: authority.revision! });
    assert.ok(mixed.bridge_send_message);
    assert.ok(mixed.bridge_read_local);
    const requestContext = new RequestContext();
    requestContext.set('mode', 'chat'); requestContext.set('allowSend', true);
    requestContext.set('runId', 'run'); requestContext.set('ownerRequestKey', 'event');
    requestContext.set('sourceRevision', authority.revision);
    await mixed.bridge_send_message!.execute!({}, { requestContext,
      observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } });
    assert.equal(approved, 1);
  } finally { await bridge.close(); }
});

test('source policy change during asynchronous approval blocks final send contact', async () => {
  const revision = 'a'.repeat(64);
  let liveRevision = revision;
  let contacts = 0;
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    interactiveSendBearer: 'send',
    sourceAuthority: { read: async () => ({ version: 2, complete: true, revision: liveRevision,
      bridge_sources: ['imessage'], google_sources: [] }) },
    executeInteractiveSend: async call => {
      liveRevision = 'b'.repeat(64);
      return call.invoke({ channel: 'imessage', to: '+12065550123', body: 'approved' }, () => {});
    },
    fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string };
      if (body.method === 'notifications/initialized') return new Response(null, { status: 202 });
      const result = body.method === 'initialize'
        ? { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } }
        : body.method === 'tools/list'
          ? { tools: [{ name: 'send_message', inputSchema: { type: 'object', properties: {} } }] }
          : (contacts++, { structuredContent: { status: 'sent' } });
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }),
        { headers: { 'content-type': 'application/json' } });
    } });
  try {
    const tool = (await bridge.chatTools({ allowSend: true, sourceRevision: revision })).bridge_send_message!;
    const requestContext = new RequestContext();
    requestContext.set('mode', 'chat'); requestContext.set('allowSend', true);
    requestContext.set('runId', 'run'); requestContext.set('ownerRequestKey', 'event');
    requestContext.set('sourceRevision', revision);
    await assert.rejects(tool.execute!({}, { requestContext,
      observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } }),
    /source permissions changed/i);
    assert.equal(contacts, 0);
  } finally { await bridge.close(); }
});

test('concurrent MCP attempts cannot both contact while source refresh is awaiting', async () => {
  const revision = 'a'.repeat(64);
  let reads = 0;
  let contacts = 0;
  let refreshes = 0;
  let release!: () => void;
  const bothRefreshing = new Promise<void>(resolve => { release = resolve; });
  const bridge = createBridgeMcpClients({ url: 'http://127.0.0.1:43210/mcp', readBearer: 'read',
    interactiveSendBearer: 'send',
    sourceAuthority: { read: async () => {
      if (++reads >= 3) {
        refreshes++;
        if (refreshes === 2) release();
        await bothRefreshing;
      }
      return { version: 2, complete: true, revision, bridge_sources: ['imessage'], google_sources: [] };
    } },
    executeInteractiveSend: async call => {
      const fence = () => {};
      const outcomes = await Promise.allSettled([call.invoke({}, fence), call.invoke({}, fence)]);
      assert.equal(outcomes.filter(result => result.status === 'fulfilled').length, 1);
      assert.equal(outcomes.filter(result => result.status === 'rejected').length, 1);
      return outcomes;
    },
    fetch: async input => {
      const body = await new Request(input).json() as { id: number; method: string };
      if (body.method === 'notifications/initialized') return new Response(null, { status: 202 });
      const result = body.method === 'initialize'
        ? { protocolVersion: '2025-06-18', capabilities: { tools: {} }, serverInfo: { name: 'fixture', version: '1' } }
        : body.method === 'tools/list'
          ? { tools: [{ name: 'send_message', inputSchema: { type: 'object', properties: {} } }] }
          : (contacts++, { structuredContent: { status: 'sent' } });
      return new Response(JSON.stringify({ jsonrpc: '2.0', id: body.id, result }),
        { headers: { 'content-type': 'application/json' } });
    } });
  try {
    const tool = (await bridge.chatTools({ allowSend: true, sourceRevision: revision })).bridge_send_message!;
    const requestContext = new RequestContext();
    requestContext.set('mode', 'chat'); requestContext.set('allowSend', true);
    requestContext.set('runId', 'run'); requestContext.set('ownerRequestKey', 'event');
    requestContext.set('sourceRevision', revision);
    await tool.execute!({}, { requestContext,
      observe: { span: async <T>(_name: string, fn: () => Promise<T> | T) => fn(), log: () => {} } });
    assert.equal(refreshes, 2);
    assert.equal(contacts, 1);
  } finally { await bridge.close(); }
});
