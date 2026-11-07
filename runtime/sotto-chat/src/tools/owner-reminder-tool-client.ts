import {createOwnerToolTransport, OwnerToolConfigurationError} from './owner-tool-transport.js';
import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { OwnerToolClientOptions } from './owner-contract.js';
import { stableActionIdentity } from './action-gate.js';
import { ownerReminderToolArgsSchema, ownerReminderToolKind, ownerReminderToolReceiptSchema, offsetDate, reminderReceiptMatches, ownerReminderCreateArgsSchema,
  ownerReminderRejectionSchema, type OwnerReminderToolArgs } from './owner-reminder-tool-contract.js';
const hex = z.string().regex(/^[a-f0-9]{64}$/);
const schema = z.discriminatedUnion('status', [ownerReminderToolReceiptSchema,
  z.object({version: z.literal(1), status: z.literal('context'), event_id: hex, payload_hash: hex,
    zone: z.string().regex(/^[A-Za-z0-9_+./:-]{1,64}$/), now: z.string().max(64).refine(offsetDate)}).strict(),
  z.object({version: z.literal(1), status: z.literal('listed'), event_id: hex, payload_hash: hex,
    zone: z.string().regex(/^[A-Za-z0-9_+./:-]{1,64}$/),
    items: z.array(z.object({id: z.string().regex(/^int_[a-f0-9]{32}$/),
      due: z.string().max(64).refine(offsetDate), action: ownerReminderCreateArgsSchema.shape.action,
      status: z.literal('scheduled')}).strict()).max(50)}).strict(),
  z.object({version: z.literal(1), status: z.literal('not_found')}).strict(),
  z.object({version: z.literal(1), status: z.literal('rejected'), code: ownerReminderRejectionSchema}).strict(),
]);
export interface OwnerReminderToolRequest {
  actionId: string; eventId: string; payloadHash: string; text: string; admittedAtMs: number;
  providerThreadId: string; args: OwnerReminderToolArgs | null;
}
export type OwnerReminderToolReply = z.infer<typeof schema>;
const hash = (value: string) => createHash('sha256').update(value, 'utf8').digest('hex');
export function createOwnerReminderToolClient(options: OwnerToolClientOptions) {
  let transport: ReturnType<typeof createOwnerToolTransport>;
  try { transport = createOwnerToolTransport(options, '/runtime/owner-reminder-tool'); }
  catch (error) {
    if (error instanceof OwnerToolConfigurationError) throw new Error('Owner reminder configuration is invalid');
    throw error;
  }
  async function call(operation: 'context' | 'list' | 'create' | 'cancel' | 'inspect', input: OwnerReminderToolRequest,
                      signal?: AbortSignal): Promise<OwnerReminderToolReply> {
    if (!hex.safeParse(input.eventId).success || !hex.safeParse(input.payloadHash).success ||
        !Number.isSafeInteger(input.admittedAtMs) || !input.providerThreadId || !input.text || input.text.length > 16000 ||
        /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(input.text) ||
        (['context', 'list'].includes(operation) ? input.args !== null : !ownerReminderToolArgsSchema.safeParse(input.args).success))
      throw new Error('Owner reminder request is invalid');
    const binding = stableActionIdentity(input.eventId, ownerReminderToolKind, input.args);
    if (binding.actionId !== input.actionId) throw new Error('Owner reminder request is invalid');
    const body = {version: 1, operation, identity: transport.identity, action_id: input.actionId,
      event_id: input.eventId, payload_hash: input.payloadHash, text_hash: hash(input.text), text: input.text,
      admitted_at_ms: input.admittedAtMs, provider_thread_id: input.providerThreadId, args: input.args};
    if (Buffer.byteLength(JSON.stringify(body)) > 65536) throw new Error('Owner reminder request is invalid');
    const responseLimit = operation === 'list' ? 512 * 1024 : 8192;
    try {
      const response = await transport.request(body, signal, {responseLimit: responseLimit, deadlineMs: 5000, requestLimit: 65536});
      const value = schema.parse(response.value);
      if ((response.status === 409) !== (value.status === 'rejected') ||
          (operation === 'context' && value.status !== 'context') ||
          (operation === 'list' && value.status !== 'listed') ||
          (['create', 'cancel'].includes(operation) && !['committed', 'rejected'].includes(value.status)) ||
          (operation === 'inspect' && !['committed', 'not_found', 'rejected'].includes(value.status)) ||
          ((value.status === 'context' || value.status === 'listed') && (value.event_id !== input.eventId || value.payload_hash !== input.payloadHash)) ||
          (value.status === 'committed' && (value.action_id !== input.actionId || value.event_id !== input.eventId ||
            value.payload_hash !== input.payloadHash || value.args_hash !== binding.payloadHash ||
            !input.args || !reminderReceiptMatches(input.args, value)))) throw new Error();
      return value;
    } catch {throw new Error('Owner reminder unavailable');}
  }
  return {identity: transport.identity, scopeHash: transport.scopeHash, call};
}
export type OwnerReminderToolClient = ReturnType<typeof createOwnerReminderToolClient>;
