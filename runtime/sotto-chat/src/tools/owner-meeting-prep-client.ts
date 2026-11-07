import {createHash} from 'node:crypto';
import {z} from 'zod';
import {createOwnerToolTransport} from './owner-tool-transport.js';
import type {OwnerToolClientOptions} from './owner-contract.js';
import {stableActionIdentity} from './action-gate.js';
import type {Inbound} from '../channels/types.js';

const hex = z.string().regex(/^[a-f0-9]{64}$/);
const providerId = z.string().min(1).max(512)
  .refine(value => !/[\s\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
export const ownerMeetingPrepKind = 'owner_meeting_prep_request';
export const clearMeetingPrepYes = (text: string) => /^(yes|yes please|yes, please|yes pls|yep|yup|sure|do it|full prep please|full prep pls)[.!]?$/i.test(text.trim()) || namedMeetingPrepReply(text) !== null;
/** A named request may accept an offer only after the receiver checks its person. */
export function namedMeetingPrepReply(text: string): string | null {
  return /^(?:please\s+)?(?:(?:give|send|pull|get)(?:\s+me)?\s+)?(?:the\s+)?(?:full\s+|meeting\s+)?prep\s+(?:on|for)\s+([\p{L}\p{M}][\p{L}\p{M}\s.'’\-]*?)[.!?]?$/iu.exec(text.trim())?.[1]?.trim() ?? null;
}
/** Selectors come from durable provider ingress, never from model arguments. */
export const ownerMeetingPrepArgsSchema = z.object({
  provider_message_id: providerId,
  reply_to_message_id: providerId.nullable(),
  provider_sent_at_ms: z.number().int().positive().max(Number.MAX_SAFE_INTEGER).nullable(),
  selected_offer: z.object({mode: z.literal('accepted_dispatch'), dispatch_id: providerId,
    message_id: providerId, provider_sent_at_ms: z.number().int().positive().max(Number.MAX_SAFE_INTEGER)}).strict().optional(),
}).strict();
export type OwnerMeetingPrepArgs = z.infer<typeof ownerMeetingPrepArgsSchema>;
export function meetingPrepSelectors(inbound: Inbound): OwnerMeetingPrepArgs | null {
  if (inbound.channel !== 'photon' || !inbound.text || !clearMeetingPrepYes(inbound.text) ||
      inbound.media?.length || !inbound.replyBound || !inbound.replyToMessageId) return null;
  const parsed = ownerMeetingPrepArgsSchema.safeParse({provider_message_id: inbound.eventId,
    reply_to_message_id: inbound.replyBound ? inbound.replyToMessageId : null,
    provider_sent_at_ms: inbound.providerSentAtMs ?? null});
  // A timestamp alone cannot establish that a Python offer remains the latest
  // question after an intervening Node reply. Never infer that authority here.
  return parsed.success ? parsed.data : null;
}
export const ownerMeetingPrepRejectionSchema = z.enum(['event_conflict', 'already_requested',
  'no_matching_offer', 'ambiguous_offer', 'unsupported_offer', 'needs_context', 'pending_delivery', 'unavailable']);
export const ownerMeetingPrepReceiptSchema = z.object({
  version: z.literal(1), status: z.literal('committed'), action_id: hex, event_id: hex,
  payload_hash: hex, args_hash: hex,
  outcome: z.object({operation: z.literal('admit'), request_id: z.string().regex(/^[a-f0-9]{32}$/),
    offer_id: z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/), status: z.literal('queued')}).strict(),
}).strict();
export type OwnerMeetingPrepReceipt = z.infer<typeof ownerMeetingPrepReceiptSchema>;
export interface OwnerMeetingPrepRequest {
  actionId: string; eventId: string; payloadHash: string; text: string; admittedAtMs: number;
  providerThreadId: string; args: OwnerMeetingPrepArgs;
}
const responseSchema = z.discriminatedUnion('status', [ownerMeetingPrepReceiptSchema,
  z.object({version: z.literal(1), status: z.literal('not_found')}).strict(),
  z.object({version: z.literal(1), status: z.literal('rejected'), code: ownerMeetingPrepRejectionSchema}).strict(),
]);

export function createOwnerMeetingPrepClient(options: OwnerToolClientOptions) {
  const transport = createOwnerToolTransport(options, '/runtime/owner-meeting-prep-request');
  if (transport.identity.channel !== 'photon') throw new Error('Meeting prep requests require Photon');
  async function call(operation: 'admit' | 'inspect' | 'preview', input: OwnerMeetingPrepRequest, signal?: AbortSignal) {
    const args = ownerMeetingPrepArgsSchema.parse(input.args);
    if (args.selected_offer && (args.reply_to_message_id !== null || args.provider_sent_at_ms === null
        || args.selected_offer.provider_sent_at_ms >= args.provider_sent_at_ms))
      throw new Error('Invalid unthreaded offer selection');
    if (operation === 'preview' && !args.selected_offer && !args.reply_to_message_id) throw new Error('Preview requires a trusted selection');
    const binding = stableActionIdentity(input.eventId, ownerMeetingPrepKind, args);
    if (!hex.safeParse(input.eventId).success || !hex.safeParse(input.payloadHash).success ||
        input.actionId !== binding.actionId || !Number.isSafeInteger(input.admittedAtMs) ||
        !input.providerThreadId || !clearMeetingPrepYes(input.text))
      throw new Error('Meeting prep admission is invalid');
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
        (operation === 'admit' && receipt.status === 'not_found') ||
        (receipt.status === 'committed' && (receipt.action_id !== input.actionId ||
          receipt.event_id !== input.eventId || receipt.payload_hash !== input.payloadHash ||
          receipt.args_hash !== binding.payloadHash)))
      throw new Error('Meeting prep receipt is invalid');
    return receipt;
  }
  return {identity: transport.identity, scopeHash: transport.scopeHash, call};
}
export type OwnerMeetingPrepClient = ReturnType<typeof createOwnerMeetingPrepClient>;
