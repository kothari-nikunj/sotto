import { z } from 'zod';

export const ownerReminderToolKind = 'owner_reminder_tool';
export function offsetDate(value: string): boolean {
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::(\d\d)(?:\.\d{1,6})?)?(Z|[+-](\d\d):(\d\d))$/.exec(value);
  if (!m) return false;
  const y = Number(m[1]), month = Number(m[2]), day = Number(m[3]);
  const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
  return y >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= days! &&
    Number(m[4]) <= 23 && Number(m[5]) <= 59 && Number(m[6] ?? 0) <= 59 &&
    (m[7] === 'Z' || (Number(m[8]) <= 23 && Number(m[9]) <= 59)) && Number.isFinite(Date.parse(value));
}
/** Preserve Python's supported microsecond precision when validating normalized receipts. */
export function sameOffsetInstant(left: string, right: string): boolean {
  const instant = (value: string) => {
    const fraction = /\.(\d{1,6})(?:Z|[+-]\d\d:\d\d)$/.exec(value)?.[1] ?? '';
    return BigInt(Date.parse(value)) * 1000n + BigInt(fraction.padEnd(6, '0').slice(3));
  };
  return offsetDate(left) && offsetDate(right) && instant(left) === instant(right);
}
export const ownerReminderCreateArgsSchema = z.object({
  due: z.string().max(64).refine(offsetDate),
  action: z.string().min(1).refine(value => value.trim() === value && Array.from(value).length <= 500 &&
    !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(value)),
}).strict();
export const ownerReminderCancelArgsSchema = z.object({operation: z.literal('cancel'),
  reminder_id: z.string().regex(/^int_[a-f0-9]{32}$/),
  cancellation: z.string().min(1).max(16000).refine(value => value.trim() === value &&
    !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(value)),
}).strict();
export const ownerReminderConditionalArgsSchema = ownerReminderCreateArgsSchema.extend({
  operation: z.literal('conditional'),
  condition: ownerReminderCancelArgsSchema.shape.cancellation,
  anchor_key: z.string().min(1).max(200).refine(value => value.trim() === value && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value)),
  loop_version: z.string().regex(/^[a-f0-9]{64}$/),
  loop_identity: z.string().regex(/^[a-f0-9]{64}$/),
  source_revision: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();
export const ownerReminderConditionalInputSchema = ownerReminderConditionalArgsSchema.omit({source_revision: true});
export const ownerReminderRejectionSchema = z.enum(['event_conflict', 'due_unavailable', 'target_unavailable', 'target_not_owner',
  'owner_span_unavailable', 'target_changed', 'source_unavailable']);
export type OwnerReminderRejection = z.infer<typeof ownerReminderRejectionSchema>;
export const ownerReminderToolArgsSchema = z.union([ownerReminderCreateArgsSchema, ownerReminderCancelArgsSchema, ownerReminderConditionalArgsSchema]);
export type OwnerReminderToolArgs = z.infer<typeof ownerReminderToolArgsSchema>;
export const ownerReminderToolReceiptSchema = z.object({ version: z.literal(1), status: z.literal('committed'),
  action_id: z.string().regex(/^[a-f0-9]{64}$/), event_id: z.string().regex(/^[a-f0-9]{64}$/),
  payload_hash: z.string().regex(/^[a-f0-9]{64}$/), args_hash: z.string().regex(/^[a-f0-9]{64}$/),
  reminder_id: z.string().regex(/^int_[a-f0-9]{32}$/), reminder_status: z.enum(['scheduled', 'canceled', 'fired']),
  due: z.string().max(64).refine(offsetDate), operation: z.enum(['cancel', 'conditional']).optional(), anchor_key: ownerReminderConditionalArgsSchema.shape.anchor_key.optional(), action: ownerReminderCreateArgsSchema.shape.action,
}).strict();
export type OwnerReminderToolReceipt = z.infer<typeof ownerReminderToolReceiptSchema>;

export function reminderReceiptMatches(args: OwnerReminderToolArgs, receipt: OwnerReminderToolReceipt): boolean {
  if ('operation' in args && args.operation === 'cancel') return receipt.operation === 'cancel' &&
    receipt.anchor_key === undefined && receipt.reminder_id === args.reminder_id && ['canceled', 'fired'].includes(receipt.reminder_status);
  if ('operation' in args && args.operation === 'conditional') return receipt.operation === 'conditional' &&
    receipt.anchor_key === args.anchor_key && receipt.action === args.action && sameOffsetInstant(receipt.due, args.due);
  return receipt.operation === undefined && receipt.anchor_key === undefined && receipt.action === args.action && sameOffsetInstant(receipt.due, args.due);
}
