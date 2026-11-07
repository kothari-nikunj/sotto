import {createHash} from 'node:crypto';
import {z} from 'zod';
import {stableActionIdentity} from './action-gate.js';

export const ownerMeetingPrepOfferKind = 'owner_meeting_prep_offer';
const hex = z.string().regex(/^[a-f0-9]{64}$/);
const providerId = z.string().min(1).max(1024).refine(value => !/[\s\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
const providerEventId = z.string().min(1).max(256).refine(value => !/[\s\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
const email = z.string().min(3).max(320).refine(value => /^[^\s@,<>]+@[^\s@,<>]+$/.test(value) && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
const offsetDateTime = z.string().min(20).max(64).regex(/^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d{1,9})?)?(?:Z|[+-]\d\d:\d\d)$/)
  .refine(value => Number.isFinite(Date.parse(value)));
export const ownerMeetingPrepOfferArgsSchema = z.object({
  operation: z.literal('propose_prep'),
  calendar_id: z.union([z.literal('primary'),providerId]),
  event_id: providerEventId,
  start: offsetDateTime,
  attendee_email: email,
  source_revision: z.string().regex(/^[a-f0-9]{64}$/),
}).strict();
export type OwnerMeetingPrepOfferArgs = z.infer<typeof ownerMeetingPrepOfferArgsSchema>;
export const ownerMeetingPrepOfferToolInputSchema = ownerMeetingPrepOfferArgsSchema.omit({source_revision:true});

export const ownerMeetingPrepOfferReviewSchema = z.object({
  question: z.string().min(1).max(512).refine(value => !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value)),
  title: z.string().min(1).max(200).refine(value => !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value)),
  calendar_id: providerId,
  provider_event_id: providerEventId,
  start: offsetDateTime,
  attendee_email: email,
  review_hash: hex,
  source_revision: hex,
  offer_id: z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/),
  person: email,
  meeting: z.object({event_id: providerId, start: offsetDateTime}).strict(),
}).strict();
export type OwnerMeetingPrepOfferReview = z.infer<typeof ownerMeetingPrepOfferReviewSchema>;

export function ownerMeetingPrepOfferQuestion(review: OwnerMeetingPrepOfferReview): string {
  return review.question;
}
export function ownerMeetingPrepOfferReviewHash(review: OwnerMeetingPrepOfferReview): string {
  const {review_hash: _hash, ...fields} = review;
  const canonical = (value: unknown): unknown => Array.isArray(value) ? value.map(canonical)
    : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, item]) => [key, canonical(item)]))
    : value;
  return createHash('sha256').update(JSON.stringify(canonical(fields))).digest('hex');
}
export function ownerMeetingPrepOfferMatches(review: OwnerMeetingPrepOfferReview, args: OwnerMeetingPrepOfferArgs,
                                             sourceRevision: string): boolean {
  const sameInstant = (left:string,right:string) => Number.isFinite(Date.parse(left)) &&
    Number.isFinite(Date.parse(right)) && Date.parse(left) === Date.parse(right);
  return review.source_revision === sourceRevision && review.person.toLowerCase() === args.attendee_email.toLowerCase()
    && review.attendee_email.toLowerCase() === args.attendee_email.toLowerCase()
    && (args.calendar_id==='primary'||review.calendar_id === args.calendar_id)
    && review.provider_event_id === args.event_id && review.meeting.event_id === scopedCalendarEventId(review.calendar_id,args.event_id)
    && sameInstant(review.start,args.start) && sameInstant(review.meeting.start,args.start)
    && ownerMeetingPrepOfferReviewHash(review) === review.review_hash;
}
function pythonAsciiJsonString(value:string):string {
  return JSON.stringify(value).replace(/[^\u0000-\u007f]/gu, character => Array.from(character).map(part=>{
    const code=part.codePointAt(0)!;
    if(code<=0xffff)return `\\u${code.toString(16).padStart(4,'0')}`;
    const scalar=code-0x10000;
    return `\\u${(0xd800+(scalar>>10)).toString(16)}\\u${(0xdc00+(scalar&0x3ff)).toString(16)}`;
  }).join(''));
}
export function scopedCalendarEventId(calendarId:string,eventId:string):string {
  const json=`[${pythonAsciiJsonString(calendarId)},${pythonAsciiJsonString(eventId)}]`;
  return `gcal_${createHash('sha256').update(json,'utf8').digest('hex')}`;
}

export const ownerMeetingPrepOfferPreviewSchema = z.object({
  version: z.literal(1), status: z.literal('preview'), action_id: hex, event_id: hex,
  payload_hash: hex, args_hash: hex, review: ownerMeetingPrepOfferReviewSchema,
}).strict();
export type OwnerMeetingPrepOfferPreview = z.infer<typeof ownerMeetingPrepOfferPreviewSchema>;
export const ownerMeetingPrepOfferAcceptedDeliverySchema = z.object({
  accepted_dispatch_id: providerId, provider_message_id: providerId, source_revision: hex,
  review_hash: hex, payload_hash: hex, accepted_at_ms: z.number().int().positive().max(Number.MAX_SAFE_INTEGER),
}).strict();
export type OwnerMeetingPrepOfferAcceptedDelivery = z.infer<typeof ownerMeetingPrepOfferAcceptedDeliverySchema>;
export const ownerMeetingPrepOfferReceiptSchema = z.object({
  version: z.literal(1), status: z.literal('recorded'), action_id: hex, event_id: hex,
  payload_hash: hex, args_hash: hex, review_hash: hex,
  accepted_dispatch_id: providerId, provider_message_id: providerId,
  accepted_at_ms: z.number().int().positive().max(Number.MAX_SAFE_INTEGER),
  offer_id: z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/),
}).strict();
export type OwnerMeetingPrepOfferReceipt = z.infer<typeof ownerMeetingPrepOfferReceiptSchema>;
export const ownerMeetingPrepOfferRejectionSchema = z.enum(['event_conflict', 'source_unavailable', 'invalid_target', 'unavailable']);
export const ownerMeetingPrepOfferRefusalSchema = z.enum([
  'event_conflict', 'invalid_target', 'source_changed', 'missing_delivery_time', 'expired', 'event_started',
]);
export type OwnerMeetingPrepOfferRefusal = z.infer<typeof ownerMeetingPrepOfferRefusalSchema>;
export const ownerMeetingPrepOfferInspectSchema = z.discriminatedUnion('status', [
  ownerMeetingPrepOfferPreviewSchema,
  ownerMeetingPrepOfferReceiptSchema,
  z.object({version: z.literal(1), status: z.literal('not_found')}).strict(),
  z.object({version: z.literal(1), status: z.literal('unknown')}).strict(),
]);
export function ownerMeetingPrepOfferArgsHash(args: OwnerMeetingPrepOfferArgs): string {
  return stableActionIdentity('meeting-prep-offer', ownerMeetingPrepOfferKind, args).payloadHash;
}
