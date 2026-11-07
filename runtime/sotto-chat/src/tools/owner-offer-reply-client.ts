import {createHash} from 'node:crypto';
import {z} from 'zod';
import {createOwnerToolTransport} from './owner-tool-transport.js';
import type {OwnerToolClientOptions} from './owner-contract.js';
import {stableActionIdentity} from './action-gate.js';
import type {Inbound} from '../channels/types.js';

const hex = z.string().regex(/^[a-f0-9]{64}$/);
const providerId = z.string().min(1).max(512)
  .refine(value => !/[\s\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
export const ownerOfferReplyKind = 'owner_offer_reply';
export function ownerOfferReplyAction(text: string): 'declined' | 'resolved' | 'dismissed' | null {
  const phrase = text.toLowerCase().replace(/[ .!…\t\r\n]+$/gu, '').trim().replace(/\s+/gu, ' ');
  if (['no', 'nah', 'nope', 'no thanks', 'no thank you', 'not now', 'not yet', 'skip', 'skip it', 'leave it', 'forget it'].includes(phrase)) return 'declined';
  if (['done', 'handled', 'already handled', 'sorted', 'taken care of'].includes(phrase)) return 'resolved';
  if (['let it go', 'drop it', 'stop tracking this', 'drop this loop'].includes(phrase)) return 'dismissed';
  return null;
}
/** Selectors come from durable provider ingress, never from model arguments. */
export const ownerOfferReplyArgsSchema = z.object({
  provider_message_id: providerId,
  reply_to_message_id: providerId.nullable(),
  provider_sent_at_ms: z.number().int().positive().max(Number.MAX_SAFE_INTEGER).nullable(),
  selected_offer: z.object({mode: z.literal('accepted_dispatch'), dispatch_id: providerId,
    message_id: providerId, provider_sent_at_ms: z.number().int().positive().max(Number.MAX_SAFE_INTEGER)}).strict().optional(),
}).strict();
export type OwnerOfferReplyArgs = z.infer<typeof ownerOfferReplyArgsSchema>;
export function offerReplySelectors(inbound: Inbound): OwnerOfferReplyArgs | null {
  if (inbound.channel !== 'photon' || !inbound.text || !ownerOfferReplyAction(inbound.text) ||
      inbound.media?.length || !inbound.replyBound || !inbound.replyToMessageId) return null;
  const parsed = ownerOfferReplyArgsSchema.safeParse({provider_message_id: inbound.eventId,
    reply_to_message_id: inbound.replyBound ? inbound.replyToMessageId : null,
    provider_sent_at_ms: inbound.providerSentAtMs ?? null});
  // A timestamp alone cannot establish that a Python offer remains the latest
  // question after an intervening Node reply. Never infer that authority here.
  return parsed.success ? parsed.data : null;
}
export const ownerOfferReplyRejectionSchema = z.enum(['event_conflict', 'no_matching_offer',
  'ambiguous_offer', 'unsupported_offer', 'needs_context', 'unavailable', 'target_changed',
  'target_unavailable', 'source_unavailable']);
export const ownerOfferReplyReceiptSchema = z.object({
  version: z.literal(1), status: z.literal('committed'), action_id: hex, event_id: hex,
  payload_hash: hex, args_hash: hex,
  outcome: z.object({operation: z.literal('apply'), offer_id: z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/),
    action: z.enum(['declined', 'resolved', 'dismissed']), loop_changed: z.boolean()}).strict()
    .refine(value => value.loop_changed === (value.action !== 'declined')),
}).strict();
export type OwnerOfferReplyReceipt = z.infer<typeof ownerOfferReplyReceiptSchema>;
export interface OwnerOfferReplyRequest {
  actionId: string; eventId: string; payloadHash: string; text: string; admittedAtMs: number;
  providerThreadId: string; args: OwnerOfferReplyArgs;
}
const responseSchema = z.discriminatedUnion('status', [ownerOfferReplyReceiptSchema,
  z.object({version: z.literal(1), status: z.literal('not_found')}).strict(),
  z.object({version: z.literal(1), status: z.literal('rejected'), code: ownerOfferReplyRejectionSchema}).strict(),
]);

export function createOwnerOfferReplyClient(options: OwnerToolClientOptions) {
  const transport = createOwnerToolTransport(options, '/runtime/owner-offer-reply');
  if (transport.identity.channel !== 'photon') throw new Error('Offer reply requests require Photon');
  async function call(operation: 'apply' | 'inspect' | 'preview', input: OwnerOfferReplyRequest, signal?: AbortSignal) {
    const args = ownerOfferReplyArgsSchema.parse(input.args);
    if (args.selected_offer && (args.reply_to_message_id !== null || args.provider_sent_at_ms === null
        || args.selected_offer.provider_sent_at_ms >= args.provider_sent_at_ms || ownerOfferReplyAction(input.text) !== 'declined'))
      throw new Error('Invalid unthreaded offer selection');
    if (operation === 'preview' && !args.selected_offer) throw new Error('Preview requires frozen selection');
    const binding = stableActionIdentity(input.eventId, ownerOfferReplyKind, args);
    if (!hex.safeParse(input.eventId).success || !hex.safeParse(input.payloadHash).success ||
        input.actionId !== binding.actionId || !Number.isSafeInteger(input.admittedAtMs) ||
        !input.providerThreadId || !ownerOfferReplyAction(input.text))
      throw new Error('Offer reply admission is invalid');
    const response = await transport.request({version: 1, operation, action_id: input.actionId,
      event_id: input.eventId, payload_hash: input.payloadHash,
      text_hash: createHash('sha256').update(input.text).digest('hex'), text: input.text,
      admitted_at_ms: input.admittedAtMs, provider_thread_id: input.providerThreadId, args}, signal);
    if (operation === 'preview' && response.status !== 200) throw new Error('Offer preview unavailable');
    if (operation === 'preview') return z.discriminatedUnion('status', [
      z.object({version: z.literal(1), status: z.literal('match'), offer_id: z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/), kind: z.string().min(1).max(64)}).strict(),
      z.object({version: z.literal(1), status: z.literal('no_match')}).strict(),
    ]).parse(response.value);
    const receipt = responseSchema.parse(response.value);
    if ((response.status === 409) !== (receipt.status === 'rejected') ||
        (operation === 'apply' && receipt.status === 'not_found') ||
        (receipt.status === 'committed' && (receipt.action_id !== input.actionId ||
          receipt.event_id !== input.eventId || receipt.payload_hash !== input.payloadHash ||
          receipt.args_hash !== binding.payloadHash || receipt.outcome.action !== ownerOfferReplyAction(input.text))))
      throw new Error('Offer reply receipt is invalid');
    return receipt;
  }
  return {identity: transport.identity, scopeHash: transport.scopeHash, call};
}
export type OwnerOfferReplyClient = ReturnType<typeof createOwnerOfferReplyClient>;
