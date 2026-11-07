import { createTool, type Tool } from '@mastra/core/tools';
import { z } from 'zod';
import { readAuthority, SourceAuthorityChanged, type SourceAuthority, type SourceAuthorityReader } from './source-authority.js';
import { guardedSourceCall, sourceToolAvailable } from './source-tools.js';
import { BRIDGE_CONTENT } from './bridge-result.js';
import { contactSearchInput, searchContactResult } from './contact-search.js';

export interface BridgeMcpOptions {
  readonly url: string;
  /** Derived receiver bearer. Never forwarded to a skill child process. */
  readonly readBearer: string;
  /** Private per-boot send capability held by the Node gateway only. */
  readonly interactiveSendBearer?: string;
  readonly fetch?: typeof fetch;
  readonly sourceAuthority?: SourceAuthorityReader;
  /** Must fence and journal a concrete owner-approved action before invoking the MCP send. */
  readonly executeInteractiveSend?: (call: {
    runId: string; ownerRequestKey: string; args: unknown; signal?: AbortSignal;
    invoke: (approvedArgs: Record<string, unknown>, beforeContact: () => void) => Promise<unknown>;
  }) => Promise<unknown>;
}

function token(value: string): string {
  if (!value || /[\r\n]/.test(value)) throw new Error('Bridge capability is invalid');
  return value;
}

type RpcReply = { jsonrpc: '2.0'; id: number; result?: unknown; error?: unknown };
type ListedTool = { name: string; description?: string; inputSchema: unknown; outputSchema?: unknown };

/** The receiver exposes one authenticated, stateless JSON-RPC endpoint. */
export function createBridgeMcpClients(options: BridgeMcpOptions) {
  const url = new URL(options.url);
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname) ||
      url.pathname !== '/mcp' || url.search || url.hash || url.username || url.password) {
    throw new Error('Bridge MCP endpoint must be exact loopback /mcp');
  }
  const readBearer = token(options.readBearer);
  const sendBearer = options.interactiveSendBearer === undefined ? undefined : token(options.interactiveSendBearer);
  const transport = options.fetch ?? fetch;
  let nextId = 0;
  let closed = false;
  const lifetime = new AbortController();

  async function rpc(method: string, params: unknown, canSend: boolean, signal?: AbortSignal): Promise<unknown> {
    if (closed) throw new Error('Bridge client is closed');
    const id = ++nextId;
    const headers = new Headers({ 'content-type': 'application/json', authorization: `Bearer ${readBearer}` });
    if (canSend) {
      if (!sendBearer) throw new Error('interactive send capability is required');
      headers.set('X-Sotto-Chat-Send', sendBearer);
    }
    const body = JSON.stringify({ jsonrpc: '2.0', id, method, ...(params === undefined ? {} : { params }) });
    // Match the former client's 3s connection and 60s request bounds. The
    // abort race also settles when a custom fetch ignores its AbortSignal.
    const deadline = new AbortController();
    const timer = setTimeout(() => deadline.abort(), method === 'initialize' ? 3_000 : 60_000);
    timer.unref();
    const requestSignal = AbortSignal.any([lifetime.signal, deadline.signal, ...(signal ? [signal] : [])]);
    let onAbort: (() => void) | undefined;
    const aborted = new Promise<never>((_, reject) => {
      onAbort = () => reject(new Error('Bridge request was cancelled'));
      if (!requestSignal.aborted) requestSignal.addEventListener('abort', onAbort, { once: true });
    });
    let response: Response;
    try {
      if (requestSignal.aborted) throw new Error('Bridge request was cancelled');
      response = await Promise.race([transport(new Request(url, {
        method: 'POST', headers, body, redirect: 'error', signal: requestSignal,
      })), aborted]);
      if (!response.ok) throw new Error('Bridge request is unavailable');
      const reply = await Promise.race([response.json(), aborted]) as RpcReply;
      if (!reply || reply.jsonrpc !== '2.0' || reply.id !== id ||
          Object.hasOwn(reply, 'error') || !Object.hasOwn(reply, 'result')) {
        throw new Error('Bridge response is invalid or refused');
      }
      return reply.result;
    } catch {
      throw new Error(method === 'tools/call' && canSend
        ? 'Bridge send outcome is unknown; cannot be retried after provider contact'
        : 'Bridge request is unavailable');
    } finally {
      clearTimeout(timer);
      if (onAbort) requestSignal.removeEventListener('abort', onAbort);
    }
  }

  async function discover(canSend: boolean): Promise<ListedTool[]> {
    const initial = await rpc('initialize', { protocolVersion: '2025-06-18',
      capabilities: {}, clientInfo: { name: 'sotto-chat', version: '1' } }, canSend);
    if (!initial || typeof initial !== 'object' ||
        (initial as { protocolVersion?: unknown }).protocolVersion !== '2025-06-18') {
      throw new Error('Bridge tool discovery failed');
    }
    // The relay accepts this notification but does not require a session cookie.
    const result = await rpc('tools/list', undefined, canSend);
    if (!result || typeof result !== 'object' || !Array.isArray((result as { tools?: unknown }).tools)) {
      throw new Error('Bridge tool discovery failed');
    }
    const tools = (result as { tools: unknown[] }).tools;
    if (tools.some(tool => !tool || typeof tool !== 'object' ||
        typeof (tool as ListedTool).name !== 'string' || !/^[A-Za-z0-9_-]+$/.test((tool as ListedTool).name) ||
        !(tool as ListedTool).inputSchema || typeof (tool as ListedTool).inputSchema !== 'object')) {
      throw new Error('Bridge tool discovery failed');
    }
    return tools as ListedTool[];
  }

  async function call(name: string, args: unknown, canSend: boolean,
                      outputSchema?: ReturnType<typeof z.fromJSONSchema>, signal?: AbortSignal): Promise<unknown> {
    const result = await rpc('tools/call', { name, arguments: args }, canSend, signal);
    if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error('Bridge tool result is invalid');
    const row = result as Record<string, unknown>;
    if (row.isError === true) throw new Error('Bridge tool reported an error');
    if (row.structuredContent !== undefined) {
      if (!row.structuredContent || typeof row.structuredContent !== 'object') throw new Error('Bridge tool result is invalid');
      if (outputSchema && !outputSchema.safeParse(row.structuredContent).success) {
        throw new Error('Bridge tool result failed its advertised schema');
      }
      const structured = row.structuredContent;
      Object.defineProperty(structured, BRIDGE_CONTENT, {
        value: row.content, enumerable: false,
      });
      return structured;
    }
    return row;
  }

  async function refreshTools(canSend: boolean, sourceRevision?: string, listingAuthority?: SourceAuthority,
                              attended = false): Promise<Record<string, Tool>> {
    // Bootstrap has already read this snapshot for the same turn. Direct callers
    // still read it here; execution always rechecks live authority separately.
    const authority = options.sourceAuthority ? listingAuthority ?? await readAuthority(options.sourceAuthority) : undefined;
    if (authority && (!sourceRevision || !authority.complete || authority.revision !== sourceRevision)) return {};
    const listed = await discover(canSend);
    const selected: Record<string, Tool> = {};
    for (const entry of listed) {
      if (entry.name === 'send_message' ? !canSend :
          authority && !sourceToolAvailable(entry.name, authority)) continue;
      // Do not expose malformed or unsupported advertised schemas as weaker tools.
      let inputSchema: ReturnType<typeof z.fromJSONSchema>;
      let outputSchema: ReturnType<typeof z.fromJSONSchema> | undefined;
      try {
        inputSchema = z.fromJSONSchema(entry.inputSchema as Parameters<typeof z.fromJSONSchema>[0]);
        if (entry.outputSchema !== undefined) {
          outputSchema = z.fromJSONSchema(entry.outputSchema as Parameters<typeof z.fromJSONSchema>[0]);
        }
      }
      catch { throw new Error('Bridge tool discovery failed'); }
      const name = entry.name;
      const rawExecute = (args: unknown, signal?: AbortSignal) => {
        if (!inputSchema.safeParse(args).success) throw new Error('Bridge tool arguments failed their advertised schema');
        return call(name, args, canSend, outputSchema, signal);
      };
      if (attended && name === 'get_contacts' && authority && options.sourceAuthority) {
        selected.bridge_search_contacts = createTool({id: 'bridge_search_contacts',
          description: 'Find contact candidates by name, email or phone. Use for targeted recipient lookup instead of reading the whole address book. Returns bounded names and identifiers, not notes. Multiple matches require disambiguation; no identity merge or permission to send is implied.',
          inputSchema: contactSearchInput,
          execute: async (args, context) => {
            const {query, limit} = contactSearchInput.parse(args);
            const revision = context.requestContext?.get('sourceRevision');
            if (typeof revision !== 'string') throw new SourceAuthorityChanged();
            const result = await guardedSourceCall({name, args: {}, revision, reader: options.sourceAuthority!,
              signal: context.abortSignal, invoke: approved => rawExecute(approved, context.abortSignal)});
            return searchContactResult(result, query, limit);
          },
        }) as unknown as Tool;
      }
      selected[`bridge_${name}`] = createTool({ id: `bridge_${name}`,
        description: attended && name === 'get_contacts'
          ? 'Read the complete contact book, including birthdays and notes. For a specific person, email or phone use bridge_search_contacts instead to avoid loading unrelated contacts. Use this full read only for whole-book tasks.'
          : entry.description ?? name, inputSchema,
        execute: async (args, context) => {
          if (name === 'send_message') {
            const runId: unknown = context.requestContext?.get('runId');
            const ownerRequestKey: unknown = context.requestContext?.get('ownerRequestKey');
            if (context.requestContext?.get('mode') !== 'chat' ||
                context.requestContext?.get('allowSend') !== true ||
                typeof runId !== 'string' || typeof ownerRequestKey !== 'string') {
              throw new Error('interactive send authority is missing');
            }
            const checkSource = async () => {
              if (!options.sourceAuthority) return;
              const revision = context.requestContext?.get('sourceRevision');
              const current = await readAuthority(options.sourceAuthority, context.abortSignal);
              if (typeof revision !== 'string' || !current.complete || current.revision !== revision) {
                throw new SourceAuthorityChanged();
              }
            };
            await checkSource();
            let contacted = false;
            return options.executeInteractiveSend!({ runId, ownerRequestKey, args, signal: context.abortSignal,
              invoke: async (approvedArgs, beforeContact) => {
                await checkSource();
                if (contacted) throw new Error('Bridge send cannot be retried after provider contact');
                // Refuse malformed approved arguments before the fence records provider contact.
                if (!inputSchema.safeParse(approvedArgs).success) throw new Error('Bridge tool arguments failed their advertised schema');
                // No awaited setup remains between this final synchronous fence
                // and the one transport invocation. A lost response never retries.
                beforeContact();
                contacted = true;
                return rawExecute(approvedArgs, context.abortSignal);
              } });
          }
          if (authority && options.sourceAuthority) {
            const revision = context.requestContext?.get('sourceRevision');
            if (typeof revision !== 'string') throw new SourceAuthorityChanged();
            return guardedSourceCall({ name, args, revision, reader: options.sourceAuthority,
              signal: context.abortSignal, invoke: approved => rawExecute(approved, context.abortSignal) });
          }
          return rawExecute(args, context.abortSignal);
        },
      }) as Tool;
    }
    return selected;
  }
  return {
    chatTools: ({ allowSend = false, sourceRevision, listingAuthority }: { allowSend?: boolean; sourceRevision?: string; listingAuthority?: SourceAuthority } = {}) => {
      if (allowSend && !options.executeInteractiveSend) throw new Error('interactive send action gate is required');
      if (allowSend && !sendBearer) throw new Error('interactive send capability is required');
      return refreshTools(allowSend, sourceRevision, listingAuthority, true);
    },
    unattendedTools: ({ sourceRevision }: { sourceRevision?: string } = {}) => refreshTools(false, sourceRevision),
    async close() { closed = true; lifetime.abort(); },
  };
}
