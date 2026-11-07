import type {OwnerToolClientOptions} from './owner-contract.js';
import {createOwnerToolTransport, OwnerToolConfigurationError} from './owner-tool-transport.js';
import { createHash } from 'node:crypto';
import { z } from 'zod';

const hex = z.string().regex(/^[a-f0-9]{64}$/);
const replySchema = z.discriminatedUnion('status', [
  z.object({ version: z.literal(1), status: z.literal('prepared'), zone: z.string().min(1).max(64) }).strict(),
  z.object({ version: z.literal(1), status: z.literal('committed'), anchor_key: z.string().min(1).max(256) }).strict(),
  z.object({ version: z.literal(1), status: z.literal('rejected'), code: z.enum(['owner_anchor_collision', 'owner_event_conflict']) }).strict(),
]);
const closeReplySchema = z.discriminatedUnion('status', [
  z.object({ version: z.literal(1), status: z.literal('prepared'), zone: z.string().min(1).max(64),
    expected_row_digest: hex }).strict(),
  z.object({ version: z.literal(1), status: z.literal('committed'), anchor_key: z.string().min(1).max(256),
    post_digest: hex }).strict(),
  z.object({ version: z.literal(1), status: z.literal('rejected'),
    code: z.enum(['owner_close_target_unavailable', 'owner_close_target_changed', 'owner_close_event_conflict']) }).strict(),
]);
export interface OwnerLoopRequest {
  actionId: string; eventId: string; payloadHash: string; text: string;
  admittedAtMs: number; zone: string | null; providerThreadId: string;
}
export interface OwnerLoopCloseRequest extends OwnerLoopRequest {
  anchorKey: string;
  expectedRowDigest: string | null;
}
const anchorIsValid = (value: string) => value.length > 0 && Array.from(value).length <= 256 &&
  value.trim() === value && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value) &&
  Buffer.from(value, 'utf8').toString('utf8') === value;
const digest = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex');
const MAX_RESPONSE = 2048;

export function createOwnerLoopClient(options: OwnerToolClientOptions) {
  let transport: ReturnType<typeof createOwnerToolTransport>;
  try { transport = createOwnerToolTransport(options, '/runtime/owner-loop-intent'); }
  catch (error) {
    if (error instanceof OwnerToolConfigurationError) throw new Error('Owner command configuration is invalid');
    throw error;
  }
  async function call(operation: 'prepare' | 'write' | 'inspect', input: OwnerLoopRequest, signal?: AbortSignal) {
    const body = { version: 1, operation, identity: transport.identity,
      action_id: input.actionId, event_id: input.eventId, payload_hash: input.payloadHash,
      text_hash: digest(input.text), text: input.text, admitted_at_ms: input.admittedAtMs,
      zone: input.zone, provider_thread_id: input.providerThreadId };
    if (Buffer.byteLength(JSON.stringify(body)) > 8192 || !hex.safeParse(input.actionId).success ||
        !hex.safeParse(input.eventId).success || !hex.safeParse(input.payloadHash).success ||
        !input.text || Array.from(input.text).length > 500 || !Number.isSafeInteger(input.admittedAtMs)) {
      throw new Error('Owner command request is invalid');
    }
    try {
      const response = await transport.request(body, signal, {responseLimit: MAX_RESPONSE, deadlineMs: 5000, requestLimit: 8192});
      const value = replySchema.parse(response.value);
      if ((operation === 'prepare') !== (value.status === 'prepared') ||
          (operation === 'inspect' && value.status !== 'committed') ||
          (response.status === 409) !== (value.status === 'rejected')) throw new Error('Owner command unavailable');
      return value;
    } catch { throw new Error('Owner command unavailable'); }
  }
  async function callClose(operation: 'close_prepare' | 'close_write' | 'close_inspect',
                           input: OwnerLoopCloseRequest, signal?: AbortSignal) {
    if (!anchorIsValid(input.anchorKey) || input.text !== `/loop close ${input.anchorKey}` ||
        (operation === 'close_prepare' ? input.expectedRowDigest !== null || input.zone !== null :
          !hex.safeParse(input.expectedRowDigest).success || typeof input.zone !== 'string')) {
      throw new Error('Owner command request is invalid');
    }
    const body = { version: 1, operation, identity: transport.identity,
      action_id: input.actionId, event_id: input.eventId, payload_hash: input.payloadHash,
      text_hash: digest(input.text), text: input.text, admitted_at_ms: input.admittedAtMs,
      zone: input.zone, provider_thread_id: input.providerThreadId,
      anchor_key: input.anchorKey, expected_row_digest: input.expectedRowDigest };
    if (Buffer.byteLength(JSON.stringify(body)) > 8192 || !hex.safeParse(input.actionId).success ||
        !hex.safeParse(input.eventId).success || !hex.safeParse(input.payloadHash).success ||
        !Number.isSafeInteger(input.admittedAtMs)) throw new Error('Owner command request is invalid');
    try {
      const response = await transport.request(body, signal, {responseLimit: MAX_RESPONSE, deadlineMs: 5000, requestLimit: 8192});
      const value = closeReplySchema.parse(response.value);
      if ((operation === 'close_prepare' && value.status === 'committed') ||
          (operation !== 'close_prepare' && value.status === 'prepared') ||
          (operation === 'close_inspect' && value.status !== 'committed') ||
          (response.status === 409) !== (value.status === 'rejected') ||
          (value.status === 'committed' && value.anchor_key !== input.anchorKey)) throw new Error('Owner command unavailable');
      return value;
    } catch { throw new Error('Owner command unavailable'); }
  }
  return { identity: transport.identity, scopeHash: transport.scopeHash, call, callClose };
}
export type OwnerLoopClient = ReturnType<typeof createOwnerLoopClient>;
