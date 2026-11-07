import { z } from 'zod';
import {createHash} from 'node:crypto';
import {ownerMeetingPrepArgsSchema} from './owner-meeting-prep-client.js';
export const ownerPreferenceToolKind = 'owner_preference_tool';
export const preferenceFieldSchema = z.enum(['mute_senders', 'mute_people', 'mute_sections', 'tone_notes', 'vip_people']);
const value = z.string().min(1).max(1000).refine(text => text.trim() === text && Array.from(text).length <= 500 &&
  !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(text));
const hex = z.string().regex(/^[a-f0-9]{64}$/);
export const usefulnessReferenceSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}_(?:morning|evening|welcome)$/);
export const usefulnessExcerptSchema = z.string().max(640).refine(text => Array.from(text).length <= 320 && text.trim() === text && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(text));
export const usefulnessListArgsSchema = z.object({operation:z.literal('usefulness-list'),source_revision:hex}).strict();
export const usefulnessListSchema = z.object({version:z.literal(1),status:z.literal('usefulness_list'),event_id:hex,payload_hash:hex,source_revision:hex,
  items:z.array(z.object({reference:usefulnessReferenceSchema,text:z.string().min(1).max(1024*1024).refine(text=>!/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(text)),archive_version:hex}).strict()).max(8)
    .refine(items=>new Set(items.map(item=>item.reference)).size===items.length)}).strict();
export type UsefulnessList=z.infer<typeof usefulnessListSchema>;
const usefulnessRating=z.enum(['useful','not_useful']);
const usefulnessLocator=z.string().max(100).refine(value=>value==='output'||/^excerpt:(?:0|[1-9]\d*):(?:[1-9]\d{0,2}):[a-f0-9]{20}$/.test(value)&&Number(value.split(':')[2])<=320);
export const priorityNameSchema=value.refine(text=>!/[\u007f-\u009f\u2028\u2029]/u.test(text));
// A request to edit the current wording is not permission to save an ongoing preference.
export function oneOffShorteningRequest(text: string): boolean {
  return /^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:make\s+(?:it|this|that)\s+(?:(?:a\s+(?:bit|little)\s+)?shorter|more\s+(?:concise|terse))|shorten\s+(?:it|this|that)|tighten\s+(?:it|this|that)\s+up|too\s+long|be\s+more\s+(?:concise|terse)|(?:a\s+(?:bit|little)\s+)?shorter)(?:,?\s+(?:please|pls))?[.!?]*$/i.test(text.trim());
}
export function oneOffToneRequest(text: string): boolean {
  return oneOffShorteningRequest(text) || /^(?:please\s+)?(?:(?:make\s+(?:it|this|that)\s+)?(?:a\s+bit\s+)?(?:shorter|longer|more\s+(?:concise|terse|casual|formal))|(?:shorten|rewrite|rephrase)\s+(?:it|this|that))(?:,?\s+(?:please|pls))?[.!?]*$/i.test(text.trim());
}
export function priorityCtaMatches(text:string,name:string):boolean{const matched=/^prioritize staying in touch with (.+)$/i.exec(text.trim());return !!matched&&matched[1]===name;}
export const procedureRuleSchema=z.string().min(1).max(8000).refine(text=>text.trim()===text&&Array.from(text).length<=4000&&!/[\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u.test(text)&&!/^##\s+/.test(text)&&procedureReviewText({rule:text}).length<=4000);
export const procedureReviewSchema=z.object({operation:z.literal('append-procedure'),section:z.literal('Procedures'),rule:procedureRuleSchema,review_hash:hex}).strict();
export const prioritiesTextSchema=z.string().min(1).max(8000).refine(text=>text.trim()===text&&Array.from(text).length<=4000&&text.split('\n').length<=3&&text.split('\n').every(line=>!!line.trim())&&!/[\u0000-\u0009\u000b-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u.test(text)&&!/^##\s+/m.test(text));
export const prioritiesReviewSchema=z.object({operation:z.literal('replace-priorities'),section:z.literal('Priorities'),text:prioritiesTextSchema,previous_text:z.string().max(16000).refine(text=>Array.from(text).length<=8000),review_hash:hex}).strict();
export const ownerContextTextSchema=z.string().min(1).max(8000).refine(text=>text.trim()===text&&Array.from(text).length<=4000&&!/[\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u.test(text)&&!/^##\s+/.test(text));
export const ownerContextSectionSchema=z.enum(['About','People']);
export const ownerContextReviewSchema=z.object({operation:z.literal('append-context'),section:ownerContextSectionSchema,text:ownerContextTextSchema,review_hash:hex}).strict();
const standingReviewSchema=z.union([procedureReviewSchema,prioritiesReviewSchema,ownerContextReviewSchema]);
export const procedurePreviewSchema=z.object({version:z.literal(1),status:z.literal('preview'),event_id:hex,payload_hash:hex,review:standingReviewSchema}).strict();
export type ProcedurePreview=z.infer<typeof procedurePreviewSchema>;
export const procedureCapacitySchema=z.object({cap:z.literal(8000),largest_section:z.object({name:z.string().max(400).refine(v=>Array.from(v).length<=200),chars:z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),name_truncated:z.boolean()}).strict().nullable()}).strict();
export type ProcedureCapacity=z.infer<typeof procedureCapacitySchema>;
export function procedureReviewText(review:{rule:string}|z.infer<typeof prioritiesReviewSchema>|z.infer<typeof ownerContextReviewSchema>):string{if('operation' in review&&review.operation==='append-context')return `Add to your standing context (${review.section}):\n\n${review.text}\n\nSave this context?`;if('previous_text' in review)return `Replace your current priorities (complete set):\n\nCurrent:\n${review.previous_text||'None'}\n\nNew:\n${review.text}\n\nReplace these priorities?`;return `Add to your standing rules (Procedures):\n\n${review.rule}\n\nSave this rule?`;}
export function procedureReviewHash(review:z.infer<typeof standingReviewSchema>):string{const {review_hash:_,...fields}=review;return createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.entries(fields).sort(([a],[b])=>a.localeCompare(b))))).digest('hex');}
export function validProcedurePreview(preview:ProcedurePreview):boolean{return procedureReviewText(preview.review).length<=4000&&procedureReviewHash(preview.review)===preview.review.review_hash;}
export const nudgeSnoozeSpecSchema = z.string().min(1).max(64).refine(text => text.trim() === text && !/[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/u.test(text));
const nudgeSnoozeUntilSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/).or(z.literal(''));
export function normalizeOwnerQuietText(text:string):string {
  let owner=text.trim();
  const question=owner.endsWith('?');
  if(/[.!?]$/.test(owner))owner=owner.slice(0,-1).trimEnd();
  const polite=owner.replace(/^(?:please|pls)[ \t\n\r\f\v]+/i,'')
    .replace(/,?[ \t\n\r\f\v]+(?:please|pls)$/i,'').trim();
  // Only a polite request may end in a question mark; a bare question is not a change request.
  return question&&polite===owner?owner+'?':polite;
}
export function ownerQuietCommand(text:string): {operation:'snooze-nudges';spec:string}|{operation:'unsnooze-nudges'}|null {
  const owner = normalizeOwnerQuietText(text);
  if (/^(?:resume nudges|back to normal nudges|stop quieting nudges|back to normal)$/i.test(owner)) return {operation:'unsnooze-nudges'};
  if (/^(?:quieter today|noisy today|too many notifications|you're being noisy)$/i.test(owner)) return {operation:'snooze-nudges',spec:'tomorrow'};
  const until = /^(?:be )?quiet until (today|tomorrow|[0-9]{1,2}(?::[0-9]{2})?(?:am|pm)?|[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[Tt][0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?)$/i.exec(owner);
  const duration = /^(?:be )?quiet for (\+?[0-9]+(?:\.[0-9]+)?[hm]|[0-9]+(?:\.[0-9]+)? (?:hours?|minutes?))$/i.exec(owner);
  const spec = (until?.[1] ?? duration?.[1])?.toLowerCase();
  return spec && spec.length<=64 ? {operation:'snooze-nudges',spec} : null;
}
export const ownerPreferenceToolArgsSchema = z.union([
  z.object({operation:z.literal('snooze-nudges'),spec:nudgeSnoozeSpecSchema}).strict(),
  z.object({operation:z.literal('unsnooze-nudges')}).strict(),
  z.object({operation:z.literal('prioritize'),name:priorityNameSchema}).strict(),
  z.object({operation:z.literal('usefulness'),rating:usefulnessRating,reference:usefulnessReferenceSchema,archive_version:hex,excerpt:usefulnessExcerptSchema,source_revision:hex}).strict(),
  z.object({operation:z.literal('propose-procedure'),rule:procedureRuleSchema}).strict(),
  z.object({operation:z.literal('append-procedure'),review:procedureReviewSchema,proposal:z.object({event_id:hex,action_id:hex,args_hash:hex,accepted_dispatch_id:z.string().min(1).max(160),source_revision:z.literal('owner-only-context-v1')}).strict()}).strict(),
  z.object({operation:z.literal('propose-priorities'),text:prioritiesTextSchema}).strict(),
  z.object({operation:z.literal('replace-priorities'),review:prioritiesReviewSchema,proposal:z.object({event_id:hex,action_id:hex,args_hash:hex,accepted_dispatch_id:z.string().min(1).max(160),source_revision:z.literal('owner-only-context-v1')}).strict()}).strict(),
  z.object({operation:z.literal('propose-context'),section:ownerContextSectionSchema,text:ownerContextTextSchema}).strict(),
  z.object({operation:z.literal('append-context'),review:ownerContextReviewSchema,proposal:z.object({event_id:hex,action_id:hex,args_hash:hex,accepted_dispatch_id:z.string().min(1).max(160),source_revision:z.literal('owner-only-context-v1')}).strict()}).strict(),
  z.object({operation: z.literal('accept-procedure-offer'), selection: ownerMeetingPrepArgsSchema}).strict(),
  z.object({operation: z.enum(['add', 'remove']), field: preferenceFieldSchema, value}).strict(),
  z.object({operation: z.literal('clear-tone')}).strict(),
  z.object({operation: z.literal('nudge-budget'), direction: z.enum(['fewer', 'no', 'more'])}).strict(),
]);
export type OwnerPreferenceToolArgs = z.infer<typeof ownerPreferenceToolArgsSchema>;
const budget = z.object({effective: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
  ceiling: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER)}).strict().refine(v => v.effective <= v.ceiling);
const outcome = z.union([
 z.object({operation:z.literal('snooze-nudges'),spec:nudgeSnoozeSpecSchema,snooze_until:nudgeSnoozeUntilSchema.refine(text=>!!text)}).strict(),
 z.object({operation:z.literal('unsnooze-nudges'),snooze_until:z.literal('')}).strict(),
 z.object({operation:z.literal('prioritize'),section:z.literal('Priorities'),name:priorityNameSchema,text:z.string(),status:z.literal('saved')}).strict().refine(v=>v.text==='Staying in touch with '+v.name),
 ownerContextReviewSchema.extend({status:z.literal('saved')}).strict(),
 prioritiesReviewSchema.extend({status:z.literal('saved')}).strict(),
 z.object({operation:z.literal('usefulness'),reference:usefulnessReferenceSchema,item_locator:usefulnessLocator,rating:usefulnessRating,status:z.literal('recorded')}).strict(),
 z.object({operation:z.literal('append-procedure'),section:z.literal('Procedures'),rule:procedureRuleSchema,review_hash:hex,status:z.literal('saved')}).strict(),
  z.object({operation: z.literal('accept-procedure-offer'), offer_id: z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/),
    section: z.literal('Procedures'), rule: z.string().min(1).max(600).refine(text => Array.from(text).length <= 300 &&
      !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(text)), status: z.literal('saved')}).strict(),
  z.object({operation: z.enum(['add', 'remove']), field: preferenceFieldSchema, value, present: z.boolean()}).strict()
    .refine(v => v.present === (v.operation === 'add')),
  z.object({operation: z.literal('clear-tone'), count: z.literal(0)}).strict(),
  z.object({operation: z.literal('nudge-budget'), direction: z.enum(['fewer', 'no', 'more']),
    effective: budget.shape.effective, ceiling: budget.shape.ceiling}).strict().refine(v => v.effective <= v.ceiling &&
      (v.direction !== 'no' || v.effective === 0) && (v.direction !== 'more' || v.effective === v.ceiling)),
]);
export const ownerPreferenceToolReceiptSchema = z.object({version: z.literal(1), status: z.literal('committed'),
  action_id: hex, event_id: hex, payload_hash: hex, args_hash: hex, outcome}).strict();
export type OwnerPreferenceToolReceipt = z.infer<typeof ownerPreferenceToolReceiptSchema>;
export const ownerPreferenceToolReadSchema = z.object({version: z.literal(1), status: z.literal('read'), event_id: hex, payload_hash: hex,
  coverage: z.literal('explicit_only'), preferences: z.object({mute_senders: z.array(value).max(100), mute_people: z.array(value).max(100),
    mute_sections: z.array(value).max(100), tone_notes: z.array(value).max(100), vip_people: z.array(value).max(100)}).strict(),
  nudge_budget: budget, nudge_snooze_until:z.string().regex(/^(?:\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?)?)?$/).optional(), truncated_fields: z.array(preferenceFieldSchema).max(5).refine(v => new Set(v).size === v.length)}).strict();
export const ownerPreferenceToolRejectionSchema = z.enum(['event_conflict', 'target_unavailable', 'owner_span_unavailable', 'master_capacity','source_unavailable','target_changed','priorities_full']);
export type OwnerPreferenceToolRejection = z.infer<typeof ownerPreferenceToolRejectionSchema>;
export function preferenceReceiptMatches(receipt: OwnerPreferenceToolReceipt, args: OwnerPreferenceToolArgs): boolean {
  const out = receipt.outcome;
  if(args.operation==='snooze-nudges')return out.operation==='snooze-nudges'&&out.spec===args.spec;
  if(args.operation==='unsnooze-nudges')return out.operation==='unsnooze-nudges';
  if(args.operation==='prioritize')return out.operation==='prioritize'&&out.name===args.name&&out.text==='Staying in touch with '+args.name;
  if(args.operation==='propose-procedure'||args.operation==='propose-priorities'||args.operation==='propose-context')return false;
  if(args.operation==='append-context')return out.operation==='append-context'&&out.section===args.review.section&&out.text===args.review.text&&out.review_hash===args.review.review_hash;
  if(args.operation==='replace-priorities')return out.operation==='replace-priorities'&&out.text===args.review.text&&out.previous_text===args.review.previous_text&&out.review_hash===args.review.review_hash;
  if(args.operation==='usefulness') {
    if(out.operation!=='usefulness'||out.reference!==args.reference||out.rating!==args.rating)return false;
    if(!args.excerpt)return out.item_locator==='output';
    const parts=out.item_locator.split(':');
    return parts[0]==='excerpt'&&Number(parts[2])===Array.from(args.excerpt).length&&parts[3]===createHash('sha256').update(args.excerpt).digest('hex').slice(0,20);
  }
  if (out.operation !== args.operation) return false;
  if(args.operation==='append-procedure')return out.operation==='append-procedure'&&out.rule===args.review.rule&&out.review_hash===args.review.review_hash;
  if (args.operation === 'accept-procedure-offer') return out.operation === 'accept-procedure-offer';
  if ('field' in args) return 'field' in out && args.field === out.field && out.value ===
    (args.field === 'mute_senders' ? args.value.toLowerCase() : args.value);
  if (args.operation === 'nudge-budget') return out.operation === 'nudge-budget' && out.direction === args.direction;
  return out.operation === 'clear-tone';
}
function quietUntilText(local: string): string {
  // The receipt is already owner-local wall time, not a UTC instant. Keep its
  // calendar fields unchanged and include the date without guessing "today".
  const [date, time] = local.split('T');
  const [year, month, day] = date!.split('-');
  const [hour, minute] = time!.split(':');
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  return `${months[Number(month) - 1]} ${Number(day)}, ${year} at ${Number(hour) % 12 || 12}:${minute} ${Number(hour) < 12 ? 'AM' : 'PM'}`;
}
export function preferenceAcknowledgement(receipt: OwnerPreferenceToolReceipt): string {
  const out = receipt.outcome;
  if(out.operation==='snooze-nudges')return `Nudges paused until ${quietUntilText(out.snooze_until)} your time. Your briefs will still arrive.`;
  if(out.operation==='unsnooze-nudges')return 'Nudges are back on.';
  if(out.operation==='prioritize')return `Prioritized staying in touch with ${out.name}.`;
  if(out.operation==='append-context')return `Standing context saved (${out.section}): ${out.text}`;
  if(out.operation==='replace-priorities')return 'Current priorities saved.';
  if(out.operation==='usefulness')return out.rating==='useful'?'Recorded that as useful.':'Recorded that as not useful.';
  if(out.operation==='append-procedure')return `Standing rule saved: ${out.rule}`;
  if (out.operation === 'accept-procedure-offer') return `Standing rule saved: ${out.rule}`;
  if (out.operation === 'nudge-budget') return `Recorded unsolicited nudge limit: ${out.effective} (configured maximum ${out.ceiling}). Scheduled briefs and digests continue.`;
  if (out.operation === 'clear-tone') return 'Removed your explicit tone notes.';
  const labels = {mute_senders: 'sender mute', mute_people: 'person mute', mute_sections: 'brief section mute', tone_notes: 'tone note', vip_people: 'VIP preference'};
  return `${out.present ? 'Recorded' : 'Removed'} your explicit ${labels[out.field]}.`;
}

export function standingPreviewMatches(preview:ProcedurePreview,args:OwnerPreferenceToolArgs):boolean{return validProcedurePreview(preview)&&(args.operation==='propose-context'?preview.review.operation==='append-context'&&preview.review.section===args.section&&preview.review.text===args.text:args.operation==='propose-procedure'?preview.review.operation==='append-procedure'&&preview.review.rule===args.rule:args.operation==='propose-priorities'&&preview.review.operation==='replace-priorities'&&preview.review.text===args.text);}
