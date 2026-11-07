import {createOwnerToolTransport, OwnerToolConfigurationError} from './owner-tool-transport.js';
import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { OwnerToolClientOptions } from './owner-contract.js';

const hex = z.string().regex(/^[a-f0-9]{64}$/);
const reminderId = z.string().regex(/^int_[a-f0-9]{32}$/);
const reminderText = z.string().min(1).refine(value => Array.from(value).length <= 500);
const committed = z.object({ version: z.literal(1), status: z.literal('committed'),
  operation: z.enum(['create', 'cancel']), action_id: hex, event_id: hex, payload_hash: hex,
  reminder_id: reminderId, reminder_status: z.enum(['scheduled', 'canceled', 'fired']) }).strict();
const listed = z.object({ version: z.literal(1), status: z.literal('listed'), items: z.array(z.object({
  id: reminderId, due: z.string().min(1).max(64), action: reminderText,
  status: z.literal('scheduled'),
}).strict()).max(50) }).strict();
const replySchema = z.discriminatedUnion('status', [committed, listed,
  z.object({ version: z.literal(1), status: z.literal('not_found') }).strict(),
  z.object({ version: z.literal(1), status: z.literal('rejected'),
    code: z.enum(['event_conflict', 'target_unavailable', 'target_not_owner', 'due_unavailable']) }).strict(),
]);
export type OwnerReminderReply = z.infer<typeof replySchema>;
export interface OwnerReminderRequest {
  actionId: string; eventId: string; payloadHash: string; text: string;
  admittedAtMs: number; providerThreadId: string;
}
const digest = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex');

export function createOwnerReminderClient(options: OwnerToolClientOptions) {
  let transport: ReturnType<typeof createOwnerToolTransport>;
  try { transport = createOwnerToolTransport(options, '/runtime/owner-reminder-intent'); }
  catch (error) {
    if (error instanceof OwnerToolConfigurationError) throw new Error('Owner reminder configuration is invalid');
    throw error;
  }
  async function call(operation: 'create' | 'cancel' | 'inspect' | 'list', input: OwnerReminderRequest,
                      signal?: AbortSignal): Promise<OwnerReminderReply> {
    if (!hex.safeParse(input.actionId).success || !hex.safeParse(input.eventId).success ||
        !hex.safeParse(input.payloadHash).success || !Number.isSafeInteger(input.admittedAtMs) ||
        !input.text || Array.from(input.text).length > 600 || !input.providerThreadId) {
      throw new Error('Owner reminder request is invalid');
    }
    const body = { version: 1, operation, identity: transport.identity, action_id: input.actionId,
      event_id: input.eventId, payload_hash: input.payloadHash, text_hash: digest(input.text),
      text: input.text, admitted_at_ms: input.admittedAtMs, provider_thread_id: input.providerThreadId };
    if (Buffer.byteLength(JSON.stringify(body)) > 8192) throw new Error('Owner reminder request is invalid');
    try {
      const response = await transport.request(body, signal, {responseLimit: 524288, deadlineMs: 5000, requestLimit: 8192});
      const value = replySchema.parse(response.value);
      if ((response.status === 409) !== (value.status === 'rejected') ||
          (operation === 'list' && value.status !== 'listed' && value.status !== 'rejected') ||
          (operation === 'inspect' && !['committed', 'not_found'].includes(value.status)) ||
          (['create', 'cancel'].includes(operation) && !['committed', 'rejected'].includes(value.status)) ||
          (value.status === 'committed' && (value.action_id !== input.actionId ||
            value.event_id !== input.eventId || value.payload_hash !== input.payloadHash ||
            (operation !== 'inspect' && value.operation !== operation)))) throw new Error();
      return value;
    } catch { throw new Error('Owner reminder unavailable'); }
  }
  return { identity: transport.identity, scopeHash: transport.scopeHash, call };
}
export type OwnerReminderClient = ReturnType<typeof createOwnerReminderClient>;
