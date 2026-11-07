import { createHash } from 'node:crypto';
import {createOwnerToolTransport, OwnerToolConfigurationError} from './owner-tool-transport.js';
import { z } from 'zod';
import { isQuietUntil } from '../quiet-until.js';
import type { OwnerToolClientOptions } from './owner-contract.js';

const hex = z.string().regex(/^[a-f0-9]{64}$/);
const zone = z.string().regex(/^[A-Za-z0-9_+./:-]{1,64}$/);
const exactUntil = z.string().refine(isQuietUntil);
const committed = z.object({ version: z.literal(1), status: z.literal('committed'),
  action_id: hex, event_id: hex, payload_hash: hex, zone,
  snooze_until: exactUntil }).strict();
const response = z.discriminatedUnion('status', [
  z.object({ version: z.literal(1), status: z.literal('prepared'), zone }).strict(),
  committed,
  z.object({ version: z.literal(1), status: z.literal('not_found') }).strict(),
  z.object({ version: z.literal(1), status: z.literal('rejected'), code: z.enum(['event_conflict']) }).strict(),
]);
export type OwnerQuietReply = z.infer<typeof response>;
export interface OwnerQuietRequest {
  actionId: string; eventId: string; payloadHash: string; text: string;
  admittedAtMs: number; providerThreadId: string; zone: string | null;
}

const hash = (value: string) => createHash('sha256').update(value, 'utf8').digest('hex');

export function createOwnerQuietClient(options: OwnerToolClientOptions) {
  let transport: ReturnType<typeof createOwnerToolTransport>;
  try { transport = createOwnerToolTransport(options, '/runtime/owner-quiet-intent'); }
  catch (error) {
    if (error instanceof OwnerToolConfigurationError) throw new Error('Owner quiet configuration is invalid');
    throw error;
  }
  async function call(operation: 'prepare' | 'write' | 'inspect', input: OwnerQuietRequest,
                      signal?: AbortSignal): Promise<OwnerQuietReply> {
    if (!hex.safeParse(input.actionId).success || !hex.safeParse(input.eventId).success ||
        !hex.safeParse(input.payloadHash).success || !Number.isSafeInteger(input.admittedAtMs) ||
        !input.providerThreadId || !input.text || input.text.length > 64 ||
        (operation === 'prepare' ? input.zone !== null : !zone.safeParse(input.zone).success)) {
      throw new Error('Owner quiet request is invalid');
    }
    const body = { version: 1, operation, identity: transport.identity, action_id: input.actionId,
      event_id: input.eventId, payload_hash: input.payloadHash, text_hash: hash(input.text),
      text: input.text, admitted_at_ms: input.admittedAtMs,
      provider_thread_id: input.providerThreadId, zone: input.zone };
    if (Buffer.byteLength(JSON.stringify(body)) > 8192) throw new Error('Owner quiet request is invalid');
    try {
      const reply = await transport.request(body, signal, {responseLimit: 2048, deadlineMs: 5000, requestLimit: 8192});
      const value = response.parse(reply.value);
      if ((reply.status === 409) !== (value.status === 'rejected') ||
          (operation === 'prepare' && value.status !== 'prepared') ||
          (operation === 'write' && !['committed', 'rejected'].includes(value.status)) ||
          (operation === 'inspect' && !['committed', 'not_found'].includes(value.status)) ||
          (value.status === 'committed' && (value.action_id !== input.actionId ||
            value.event_id !== input.eventId || value.payload_hash !== input.payloadHash ||
            value.zone !== input.zone ||
            (input.text === '/quiet off') !== (value.snooze_until === '')))) throw new Error();
      return value;
    } catch { throw new Error('Owner quiet unavailable'); }
  }
  return { identity: transport.identity, scopeHash: transport.scopeHash, call };
}
export type OwnerQuietClient = ReturnType<typeof createOwnerQuietClient>;
