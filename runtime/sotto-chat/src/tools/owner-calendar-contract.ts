import { createHash } from 'node:crypto';
import { z } from 'zod';
import {stableActionIdentity} from './action-gate.js';
export const ownerCalendarKind = 'owner_calendar_tool';
const hex = z.string().regex(/^[a-f0-9]{64}$/);
const id = z.string().regex(/^[A-Za-z0-9_@.-]{1,256}$/);
export const calendarIdSchema=z.string().min(1).max(2048).refine(value=>value.trim()===value&&Array.from(value).length<=1024&&!/[\s\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u.test(value));
const selectedCalendar=calendarIdSchema.refine(value=>value!=='primary').optional();
const scalar = (n: number) => z.string().max(n * 2).refine(v => Array.from(v).length <= n && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(v));
const commentText = z.string().max(2048).refine(v=>Array.from(v).length<=1024&&!/[\u0000-\u0009\u000b-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u.test(v));
const realTime = (value: string) => {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|([+-])(\d{2}):(\d{2}))$/.exec(value);
    if (!match)
        return false;
    const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
    return year! > 0 && month! >= 1 && month! <= 12 && day! >= 1 && day! <= new Date(Date.UTC(year!, month!, 0)).getUTCDate() && hour! < 24 && minute! < 60 && second! < 60 && (!match[7] || Number(match[8]) < 24 && Number(match[9]) < 60) && Number.isFinite(Date.parse(value));
};
const time = z.string().max(64).regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/).refine(realTime);
export const calendarProposalInputSchema = z.object({ operation: z.literal('propose_move'), event_id: id, calendar_id:selectedCalendar, start: time, end: time }).strict();
export const calendarProposalSchema = calendarProposalInputSchema.refine(v => Date.parse(v.end) > Date.parse(v.start));
const calendarMoveReviewSchema = z.object({ operation: z.literal('move'), calendar_id: calendarIdSchema, event_id: id, etag: scalar(1024).min(1), summary: scalar(1024), html_link: scalar(2048).refine(v => !v || /^https:\/\/(?:calendar\.google\.com|www\.google\.com)\//.test(v)), old_start: time, old_end: time, new_start: time, new_end: time, notifications: z.literal('all'), source_revision: hex, review_hash: hex }).strict().refine(v => Date.parse(v.old_end) > Date.parse(v.old_start) && Date.parse(v.new_end) > Date.parse(v.new_start));
const date = z.string().regex(/^\d{4}-\d{2}-\d{2}$/).refine(v => realTime(v+'T00:00:00Z'));
const response = z.enum(['accepted','declined','tentative']);
export const calendarRsvpInputSchema = z.object({operation:z.literal('propose_rsvp'),event_id:id,calendar_id:selectedCalendar,response,comment:commentText.optional()}).strict();
export const calendarRsvpProposalSchema = calendarRsvpInputSchema.transform((v):z.infer<typeof calendarRsvpInputSchema>=>{const {comment,...rest}=v;return comment?{...rest,comment}:rest;});
const calendarRsvpReviewSchema = z.object({operation:z.literal('rsvp'),calendar_id:calendarIdSchema,event_id:id,etag:scalar(1024).min(1),summary:scalar(1024),html_link:scalar(2048).refine(v=>!v||/^https:\/\/(?:calendar\.google\.com|www\.google\.com)\//.test(v)),event_scope:z.enum(['single','occurrence','series']),all_day:z.boolean(),start:z.string().max(64),end:z.string().max(64),recurring_event_id:id.nullable(),original_start:z.string().max(64).nullable(),self_email:scalar(320).min(1).regex(/^[^\s@]+@[^\s@]+$/),old_response:z.enum(['needsAction','accepted','declined','tentative']).nullable(),new_response:response,old_comment:commentText.nullable().optional(),new_comment:commentText.optional(),notifications:z.literal('all'),source_revision:hex,review_hash:hex}).strict().refine(r=>{
 const schema=r.all_day?date:time;
 return ((r.old_comment===undefined)===(r.new_comment===undefined))&&schema.safeParse(r.start).success&&schema.safeParse(r.end).success&&(r.all_day?r.end>r.start:Date.parse(r.end)>Date.parse(r.start))&&(r.event_scope==='occurrence'?r.recurring_event_id!==null&&r.original_start!==null&&schema.safeParse(r.original_start).success:r.recurring_event_id===null&&r.original_start===null);
});
const createScalar = (n: number, multiline = false) => z.string().max(n * 2).refine(value => {
    const forbidden = multiline
        ? /[\u0000-\u0009\u000b-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u
        : /[\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u;
    return Array.from(value).length <= n && !forbidden.test(value);
});
export const calendarUpdateFieldsSchema=z.object({summary:createScalar(1024).optional(),location:createScalar(2048).optional(),description:createScalar(4000,true).optional()}).strict().refine(fields=>Object.keys(fields).length>0);
const calendarBeforeFieldsSchema=z.object({summary:createScalar(2048).optional(),location:createScalar(2048).optional(),description:createScalar(16384,true).optional()}).strict();
export const calendarUpdateProposalSchema=z.object({operation:z.literal('propose_update'),event_id:id,calendar_id:selectedCalendar,fields:calendarUpdateFieldsSchema}).strict();
const calendarUpdateReviewSchema=z.object({operation:z.literal('update'),calendar_id:calendarIdSchema,event_id:id,etag:scalar(1024).min(1),summary:createScalar(2048),html_link:scalar(2048).refine(v=>!v||/^https:\/\/(?:calendar\.google\.com|www\.google\.com)\//.test(v)),event_scope:z.enum(['single','occurrence','series']),all_day:z.boolean(),start:z.string().max(64),end:z.string().max(64),recurring_event_id:id.nullable(),original_start:z.string().max(64).nullable(),before_fields:calendarBeforeFieldsSchema,fields:calendarUpdateFieldsSchema,notifications:z.literal('all'),source_revision:hex,review_hash:hex}).strict().refine(r=>{
 const schema=r.all_day?date:time;
 return JSON.stringify(Object.keys(r.before_fields).sort())===JSON.stringify(Object.keys(r.fields).sort())&&schema.safeParse(r.start).success&&schema.safeParse(r.end).success&&(r.all_day?r.end>r.start:Date.parse(r.end)>Date.parse(r.start))&&(r.event_scope==='occurrence'?r.recurring_event_id!==null&&r.original_start!==null&&schema.safeParse(r.original_start).success:r.recurring_event_id===null&&r.original_start===null);
});
export const calendarMoveUpdateInputSchema=calendarProposalInputSchema.extend({operation:z.literal('propose_move_update'),fields:calendarUpdateFieldsSchema}).strict();
export const calendarMoveUpdateProposalSchema=calendarMoveUpdateInputSchema.refine(v=>Date.parse(v.end)>Date.parse(v.start));
const calendarMoveUpdateReviewSchema=z.object({operation:z.literal('move_update'),calendar_id:calendarIdSchema,event_id:id,etag:scalar(1024).min(1),summary:createScalar(2048),html_link:scalar(2048).refine(v=>!v||/^https:\/\/(?:calendar\.google\.com|www\.google\.com)\//.test(v)),event_scope:z.enum(['single','occurrence']),all_day:z.literal(false),old_start:time,old_end:time,new_start:time,new_end:time,recurring_event_id:id.nullable(),original_start:time.nullable(),before_fields:calendarBeforeFieldsSchema,fields:calendarUpdateFieldsSchema,notifications:z.literal('all'),source_revision:hex,review_hash:hex}).strict().refine(r=>
 (Date.parse(r.old_start)!==Date.parse(r.new_start)||Date.parse(r.old_end)!==Date.parse(r.new_end)||Object.keys(r.fields).some(key=>r.fields[key as keyof typeof r.fields]!==r.before_fields[key as keyof typeof r.before_fields]))&&JSON.stringify(Object.keys(r.before_fields).sort())===JSON.stringify(Object.keys(r.fields).sort())&&Date.parse(r.old_end)>Date.parse(r.old_start)&&Date.parse(r.new_end)>Date.parse(r.new_start)&&(r.event_scope==='occurrence'?r.recurring_event_id!==null&&r.original_start!==null:r.recurring_event_id===null&&r.original_start===null));
const email = createScalar(320).regex(/^[^\s@,<>]+@[^\s@,<>]+$/);
const addressOrder = (a: string, b: string) => {
    const x = Array.from(a, c => c.codePointAt(0)!), y = Array.from(b, c => c.codePointAt(0)!);
    for (let i = 0; i < Math.min(x.length, y.length); i++) {
        if (x[i] !== y[i]) return x[i]! - y[i]!;
    }
    return x.length - y.length;
};
const attendees = z.array(email).max(50).refine(value =>
    JSON.stringify(value) === JSON.stringify([...new Set(value.map(a => a.toLowerCase()))].sort(addressOrder)));
export const calendarCreateInputSchema = z.object({
    operation: z.literal('propose_create'), summary: createScalar(1024).min(1), start: time, end: time,
    attendees: z.array(email).max(50).transform(value => [...new Set(value.map(a => a.toLowerCase()))].sort(addressOrder)),
    location: createScalar(2048), description: createScalar(4000, true),
}).strict();
export const calendarCreateProposalSchema = calendarCreateInputSchema.refine(value => Date.parse(value.end) > Date.parse(value.start));
const calendarCreateReviewSchema = z.object({
    operation: z.literal('create'), calendar_id: z.literal('primary'), event_id: hex,
    summary: createScalar(1024).min(1), start: time, end: time, attendees, effective_attendees: attendees,
    organizer_email: email.nullable(), organizer_added: z.boolean(), location: createScalar(2048),
    description: createScalar(4000, true), notifications: z.literal('all'), source_revision: hex, review_hash: hex,
}).strict().refine(review => {
    const effective = [...new Set([...review.attendees,
        ...(review.attendees.length && review.organizer_email ? [review.organizer_email] : [])])].sort(addressOrder);
    return Date.parse(review.end) > Date.parse(review.start)
        && (!review.organizer_email || review.organizer_email === review.organizer_email.toLowerCase())
        && JSON.stringify(effective) === JSON.stringify(review.effective_attendees)
        && review.organizer_added === Boolean(review.attendees.length && review.organizer_email && !review.attendees.includes(review.organizer_email))
        && (review.attendees.length > 0 || (review.organizer_email === null && !review.organizer_added));
});
export function calendarCreateEventId(actionId: string): string {
    return createHash('sha256').update('sotto.calendar-create-event.v1\0' + actionId).digest('hex');
}
export const calendarReviewSchema=z.union([calendarMoveReviewSchema,calendarRsvpReviewSchema,calendarCreateReviewSchema,calendarUpdateReviewSchema,calendarMoveUpdateReviewSchema]);
const proposalProof=z.object({event_id:hex,action_id:hex,args_hash:hex,accepted_dispatch_id:z.string().min(1).max(160),source_revision:hex}).strict();
export const calendarArgsSchema = z.union([calendarMoveUpdateProposalSchema,z.object({operation:z.literal('move_update'),review:calendarMoveUpdateReviewSchema,proposal:proposalProof}).strict(),calendarProposalSchema,calendarRsvpProposalSchema,calendarCreateProposalSchema,calendarUpdateProposalSchema,z.object({operation:z.literal('move'),review:calendarMoveReviewSchema,proposal:proposalProof}).strict(),z.object({operation:z.literal('rsvp'),review:calendarRsvpReviewSchema,proposal:proposalProof}).strict(),z.object({operation:z.literal('create'),review:calendarCreateReviewSchema,proposal:proposalProof}).strict(),z.object({operation:z.literal('update'),review:calendarUpdateReviewSchema,proposal:proposalProof}).strict()]);
export type CalendarArgs = z.infer<typeof calendarArgsSchema>;
export const calendarPreviewSchema = z.object({ version: z.literal(1), status: z.literal('preview'), event_id: hex, payload_hash: hex, review: calendarReviewSchema }).strict();
export type CalendarPreview = z.infer<typeof calendarPreviewSchema>;
export const calendarReceiptSchema = z.object({ version: z.literal(1), status: z.literal('committed'), action_id: hex, event_id: hex, payload_hash: hex, args_hash: hex, outcome: z.union([z.object({operation:z.literal('move_update'),calendar_id:calendarIdSchema,event_id:id,start:time,end:time,review_hash:hex,status:z.literal('updated')}).strict(),z.object({ operation: z.literal('move'), calendar_id: calendarIdSchema, event_id: id, start: time, end: time, review_hash: hex, status: z.literal('moved') }).strict(),z.object({operation:z.literal('rsvp'),calendar_id:calendarIdSchema,event_id:id,response,review_hash:hex,status:z.literal('rsvped')}).strict(),z.object({operation:z.literal('create'),calendar_id:z.literal('primary'),event_id:hex,summary:createScalar(1024).min(1),start:time,end:time,html_link:scalar(2048).min(1).refine(v=>/^https:\/\/(?:calendar\.google\.com|www\.google\.com)\//.test(v)),review_hash:hex,status:z.literal('created'),organizer_listed:z.boolean().nullable(),location_attached:z.boolean().nullable(),description_attached:z.boolean().nullable()}).strict(),z.object({operation:z.literal('update'),calendar_id:calendarIdSchema,event_id:id,review_hash:hex,status:z.literal('updated')}).strict()]) }).strict();
export type CalendarReceipt = z.infer<typeof calendarReceiptSchema>;
export const calendarRejectionSchema = z.enum(['authority_changed', 'calendar_permission_unavailable', 'invalid_target', 'unsupported_event', 'target_changed', 'review_unavailable', 'event_conflict']);
export type CalendarRejection = z.infer<typeof calendarRejectionSchema>;
export function calendarReviewHash(review: z.infer<typeof calendarReviewSchema>): string {
    const { review_hash: _, ...fields } = review;
    if(review.operation==='update'||review.operation==='move_update')return stableActionIdentity('calendar-review',ownerCalendarKind,fields).payloadHash;
    return createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.entries(fields).sort(([a], [b]) => a.localeCompare(b))))).digest('hex');
}
// Render the reviewed local components, never the server's timezone. An offset
// is not a timezone name: keep it explicit, including when the endpoints differ.
function calendarDateText(value: string): string {
    const [year, month, day] = value.split('-');
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    return `${months[Number(month) - 1]} ${Number(day)}, ${year}`;
}
function calendarTimeParts(value: string) {
    const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(value)!;
    const hour = Number(match[2]);
    const seconds = match[4] !== '00' || match[5] ? `:${match[4]}${match[5] ?? ''}` : '';
    return { date: match[1]!, clock: `${hour % 12 || 12}:${match[3]}${seconds} ${hour < 12 ? 'AM' : 'PM'}`, offset: match[6] === 'Z' ? 'UTC' : `UTC${match[6]}` };
}
export function calendarWhenText(start: string, end: string, allDay = false): string {
    if (allDay) return `All day, ${calendarDateText(start)}; ends before ${calendarDateText(end)}`;
    const first = calendarTimeParts(start), last = calendarTimeParts(end);
    if (first.date === last.date && first.offset === last.offset)
        return `${calendarDateText(first.date)}, ${first.clock} to ${last.clock} (${first.offset})`;
    return `${calendarDateText(first.date)}, ${first.clock} (${first.offset}) to ${calendarDateText(last.date)}, ${last.clock} (${last.offset})`;
}
function calendarLabel(id: string): string { return id === 'primary' ? 'main calendar' : id; }
export function calendarReviewText(preview: CalendarPreview): string {
    const r = preview.review;
    if(r.operation==='update'||r.operation==='move_update'){
      const scope=r.event_scope==='series'?'entire recurring series':r.event_scope==='occurrence'?'this occurrence':'this event';
      const target=r.event_scope==='series'?'the entire series':r.event_scope==='occurrence'?'this occurrence':'this event';
      const lines=[`Update: ${r.summary||'(no title)'}`,`Calendar: ${calendarLabel(r.calendar_id)}`,`Applies to: ${scope}`,...(r.operation==='move_update'?[`From: ${calendarWhenText(r.old_start, r.old_end)}`,`To: ${calendarWhenText(r.new_start, r.new_end)}`]:[`When: ${calendarWhenText(r.start, r.end, r.all_day)}`])];
      for(const [key,label] of [['summary','Title'],['location','Location'],['description','Agenda']] as const){
        if(Object.hasOwn(r.fields,key)){const empty=key==='summary'?'(no title)':'(empty)';lines.push('',`${label}:`,'Before:',r.before_fields[key]||empty,'After:',r.fields[key]||empty);}
      }
      lines.push('',`Guests will be notified. Update ${target}?`);return lines.join('\n');
    }
    if(r.operation==='create') {
      const lines=[`I’ll add ${r.summary} to your ${calendarLabel(r.calendar_id)}.`,calendarWhenText(r.start, r.end)];
      if(r.effective_attendees.length)lines.push(`Guests: ${r.effective_attendees.join(', ')}`);
      if(r.location)lines.push(`Location: ${r.location}`);
      if(r.description)lines.push(`Agenda:\n${r.description}`);
      if(r.attendees.length&&r.organizer_email===null)lines.push('', 'Your address is not known, so you may not appear in the guest list.');
      lines.push('', r.effective_attendees.length?'Create it and invite these guests?':'Create it?');
      return lines.join('\n');
    }
    if(r.operation==='rsvp'){
      const scope=r.event_scope==='series'?'entire recurring series':r.event_scope==='occurrence'?'this occurrence':'this event';
      const target=r.event_scope==='series'?'the entire series':r.event_scope==='occurrence'?'this occurrence':'this event';
      const readable={needsAction:'Needs response',accepted:'Accepted',declined:'Declined',tentative:'Tentative'};
      const verb={accepted:'Accept',declined:'Decline',tentative:'Mark tentative for'};
      return `RSVP: ${r.summary}\nCalendar: ${calendarLabel(r.calendar_id)}\nApplies to: ${scope}\nWhen: ${calendarWhenText(r.start, r.end, r.all_day)}\nYour address: ${r.self_email}\nResponse: ${r.old_response===null?'No recorded response':readable[r.old_response]} → ${readable[r.new_response]}${r.new_comment===undefined?'':`\n\nYour comment before: ${r.old_comment===null?'None':r.old_comment}\nYour comment after: ${r.new_comment||'Unchanged'}`}\n\nThe organizer and guests will be notified. ${verb[r.new_response]} ${target}?`;
    }
    return `Move: ${r.summary}\nCalendar: ${calendarLabel(r.calendar_id)}\nFrom: ${calendarWhenText(r.old_start, r.old_end)}\nTo: ${calendarWhenText(r.new_start, r.new_end)}\n\nGuests will be notified. Move this meeting?`;
}
export function validCalendarReview(preview: CalendarPreview): boolean { return calendarReviewText(preview).length <= (['update','move_update'].includes(preview.review.operation)?16000:4000) && calendarReviewHash(preview.review) === preview.review.review_hash; }
export function calendarPreviewMatches(preview:CalendarPreview,args:CalendarArgs, actionId?:string):boolean {
 if(!validCalendarReview(preview))return false;
 if (args.operation === 'propose_create') {
    const review = preview.review;
    return review.operation === 'create' && Boolean(actionId) && review.event_id === calendarCreateEventId(actionId!)
        && review.summary === args.summary && review.start === args.start && review.end === args.end
        && JSON.stringify(review.attendees) === JSON.stringify(args.attendees)
        && review.location === args.location && review.description === args.description;
 }
 if(preview.review.calendar_id!==('calendar_id' in args?args.calendar_id??'primary':'primary'))return false;
 if(args.operation==='propose_move_update')return preview.review.operation==='move_update'&&preview.review.event_id===args.event_id&&preview.review.new_start===args.start&&preview.review.new_end===args.end&&stableActionIdentity('fields',ownerCalendarKind,preview.review.fields).payloadHash===stableActionIdentity('fields',ownerCalendarKind,args.fields).payloadHash;
 if(args.operation==='propose_update')return preview.review.operation==='update'&&preview.review.event_id===args.event_id&&stableActionIdentity('fields',ownerCalendarKind,preview.review.fields).payloadHash===stableActionIdentity('fields',ownerCalendarKind,args.fields).payloadHash;
 if(args.operation==='propose_move')return preview.review.operation==='move'&&preview.review.event_id===args.event_id&&preview.review.new_start===args.start&&preview.review.new_end===args.end;
 return args.operation==='propose_rsvp'&&preview.review.operation==='rsvp'&&preview.review.event_id===args.event_id&&preview.review.new_response===args.response&&preview.review.new_comment===args.comment;
}
export function calendarReceiptMatches(receipt:CalendarReceipt,args:CalendarArgs):boolean {
 if(args.operation!=='move'&&args.operation!=='rsvp'&&args.operation!=='create'&&args.operation!=='update'&&args.operation!=='move_update')return false;
 if(receipt.outcome.calendar_id!==args.review.calendar_id||receipt.outcome.event_id!==args.review.event_id||receipt.outcome.review_hash!==args.review.review_hash)return false;
 if(args.operation==='move_update')return receipt.outcome.operation==='move_update'&&receipt.outcome.start===args.review.new_start&&receipt.outcome.end===args.review.new_end;
 if(args.operation==='update')return receipt.outcome.operation==='update';
 if (args.operation === 'create') {
    const outcome = receipt.outcome;
    return outcome.operation === 'create' && outcome.summary === args.review.summary
        && outcome.start === args.review.start && outcome.end === args.review.end
        && outcome.organizer_listed === (args.review.attendees.length ? Boolean(args.review.organizer_email) : null)
        && outcome.location_attached === (args.review.location ? true : null)
        && outcome.description_attached === (args.review.description ? true : null);
 }
 return args.operation==='move'?receipt.outcome.operation==='move'&&receipt.outcome.start===args.review.new_start&&receipt.outcome.end===args.review.new_end:receipt.outcome.operation==='rsvp'&&receipt.outcome.response===args.review.new_response;
}
