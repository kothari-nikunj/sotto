import {createHash} from 'node:crypto';
import {z} from 'zod';
import {createOwnerToolTransport} from './owner-tool-transport.js';
import type {OwnerToolClientOptions} from './owner-contract.js';
import {stableActionIdentity} from './action-gate.js';
const hex = z.string().regex(/^[a-f0-9]{64}$/);
export const ownerBriefRequestKind = 'owner_brief_request';
export const ownerBriefRequestArgsSchema = z.object({
  kind: z.enum(['morning', 'evening', 'auto']), refresh: z.boolean(),
  remainder: z.string().min(1).max(400).nullable(), provider_message_id: z.string().min(1).max(512),
}).strict();
export type OwnerBriefRequestArgs = z.infer<typeof ownerBriefRequestArgsSchema>;
export const ownerBriefRequestReceiptSchema = z.object({
  version: z.literal(1), status: z.literal('committed'), action_id: hex, event_id: hex,
  payload_hash: hex, args_hash: hex, outcome: z.object({operation: z.literal('admit'),
    request_id: z.string().regex(/^[a-f0-9]{32}$/), kind: z.enum(['morning', 'evening']),
    refresh: z.boolean(), status: z.literal('queued')}).strict(),
}).strict();
export type OwnerBriefRequestReceipt = z.infer<typeof ownerBriefRequestReceiptSchema>;
export interface OwnerBriefRequest {
  actionId: string; eventId: string; payloadHash: string; text: string; admittedAtMs: number;
  providerThreadId: string; args: OwnerBriefRequestArgs;
}
const responseSchema = z.discriminatedUnion('status', [ownerBriefRequestReceiptSchema,
  z.object({version: z.literal(1), status: z.literal('not_found')}).strict(),
  z.object({version: z.literal(1), status: z.literal('rejected'), code: z.enum(['event_conflict', 'pending_delivery', 'needs_context', 'unavailable'])}).strict(),
]);
export function createOwnerBriefRequestClient(options: OwnerToolClientOptions) {
  const transport = createOwnerToolTransport(options, '/runtime/owner-brief-request');
  async function call(operation: 'admit' | 'inspect', input: OwnerBriefRequest, signal?: AbortSignal) {
    const args = ownerBriefRequestArgsSchema.parse(input.args);
    const binding = stableActionIdentity(input.eventId, ownerBriefRequestKind, args);
    if (!hex.safeParse(input.eventId).success || !hex.safeParse(input.payloadHash).success ||
        input.actionId !== binding.actionId || !Number.isSafeInteger(input.admittedAtMs) ||
        !input.providerThreadId || !input.text || input.text.length > 16000 ||
        /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(input.text))
      throw new Error('Requested brief admission is invalid');
    const response = await transport.request({version: 1, operation, action_id: input.actionId,
      event_id: input.eventId, payload_hash: input.payloadHash,
      text_hash: createHash('sha256').update(input.text).digest('hex'), text: input.text,
      admitted_at_ms: input.admittedAtMs, provider_thread_id: input.providerThreadId, args}, signal);
    const receipt = responseSchema.parse(response.value);
    if ((response.status === 409) !== (receipt.status === 'rejected') ||
        (operation === 'admit' && receipt.status === 'not_found') ||
        (receipt.status === 'committed' && (receipt.action_id !== input.actionId || receipt.event_id !== input.eventId ||
          receipt.payload_hash !== input.payloadHash || receipt.args_hash !== binding.payloadHash ||
          receipt.outcome.refresh !== args.refresh || (args.kind !== 'auto' && receipt.outcome.kind !== args.kind))))
      throw new Error('Requested brief receipt is invalid');
    return receipt;
  }
  return {identity: transport.identity, scopeHash: transport.scopeHash, call};
}
export type OwnerBriefRequestClient = ReturnType<typeof createOwnerBriefRequestClient>;
