import { z } from 'zod';
import { createHash } from 'node:crypto';
export const ownerRoutineToolKind = 'owner_routine_tool';
const hex = z.string().regex(/^[a-f0-9]{64}$/);
const prompt = z.string().min(1).max(4000).refine(v => v.trim() === v && Array.from(v).length <= 2000 && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(v));
export const routineScheduleSchema = z.object({
  hour: z.number().int().min(0).max(23).nullable(), minute: z.number().int().min(0).max(59), weekday: z.union([z.number().int().min(0).max(6),z.literal('weekdays'),z.literal('first_of_month'),
    z.array(z.number().int().min(0).max(6)).min(1).max(7).refine(days => days.every((day, index) => index === 0 || days[index - 1]! < day))]).nullable()
}).strict().refine(s => s.hour !== null || s.weekday === null);
export const routineProposalArgsSchema = z.object({
  operation: z.literal('propose'), name: z.string().regex(/^user-[a-z0-9]+(?:-[a-z0-9]+)*$/).max(29), schedule: routineScheduleSchema, prompt
}).strict();
export const routineDefinitionSchema = routineProposalArgsSchema.omit({
  operation: true
}).extend({
  zone: z.string().min(1).max(160), destination: z.enum(['this Photon conversation', 'this Telegram conversation'])
}).strict();
const proposalProof = z.object({ event_id: hex, action_id: hex, args_hash: hex, accepted_dispatch_id: z.string().min(1).max(160), source_revision: z.string().min(1).max(160) }).strict();
export const routineAdjustProposalSchema = z.object({operation:z.literal('propose_adjust'),routine_id:hex,schedule:routineScheduleSchema}).strict();
export const ownerRoutineToolArgsSchema = z.union([routineProposalArgsSchema, routineAdjustProposalSchema, z.object({operation:z.literal('adjust'),routine_id:hex,previous_definition_hash:hex,definition:routineDefinitionSchema,proposal:proposalProof}).strict(),
  z.object({
    operation: z.literal('create'), definition: routineDefinitionSchema, proposal: z.object({
      event_id: hex, action_id: hex, args_hash: hex, accepted_dispatch_id: z.string().min(1).max(160), source_revision: z.string().min(1).max(160)
    }).strict()
  }).strict(),
  z.object({
    operation: z.literal('cancel'), routine_id: hex
  }).strict()]);
export type OwnerRoutineToolArgs = z.infer<typeof ownerRoutineToolArgsSchema>;
const createPreviewSchema = z.object({
  version: z.literal(1), status: z.literal('preview'), event_id: hex, payload_hash: hex, definition: routineDefinitionSchema, definition_hash: hex
}).strict();
export const routinePreviewSchema = z.union([createPreviewSchema,createPreviewSchema.extend({operation:z.literal('adjust'),routine_id:hex,previous_definition:routineDefinitionSchema,previous_definition_hash:hex}).strict()]);
export const ownerRoutineToolReceiptSchema = z.object({
  version: z.literal(1), status: z.literal('committed'), action_id: hex, event_id: hex, payload_hash: hex, args_hash: hex,
  outcome: z.union([z.object({
    operation: z.enum(['create', 'cancel']), routine_id: hex, definition_hash: hex, status: z.enum(['active', 'cancelled'])
  }).strict().refine(v => v.status === (v.operation === 'create' ? 'active' : 'cancelled')),z.object({operation:z.literal('adjust'),routine_id:hex,definition_hash:hex,status:z.literal('active'),previous_routine_id:hex,previous_definition_hash:hex}).strict()])
}).strict();
export type OwnerRoutineToolReceipt = z.infer<typeof ownerRoutineToolReceiptSchema>;
export type RoutinePreview = z.infer<typeof routinePreviewSchema>;
export const routineListSchema = z.object({
  version: z.literal(1), status: z.literal('list'), event_id: hex, payload_hash: hex, coverage: z.literal('fresh_only'), routines: z.array(routineDefinitionSchema.omit({
    destination: true
  }).extend({
    routine_id: hex, status: z.enum(['active', 'cancelled'])
  }).strict()).max(100)
}).strict();
export const ownerRoutineToolRejectionSchema = z.enum(['event_conflict', 'target_unavailable', 'proposal_unavailable', 'routine_limit', 'routine_conflict', 'unsupported_definition']);
export type OwnerRoutineToolRejection = z.infer<typeof ownerRoutineToolRejectionSchema>;
export function routineDefinitionHash(definition: z.infer<typeof routineDefinitionSchema>): string {
  const canonical = {
    destination: definition.destination,
    name: definition.name,
    prompt: definition.prompt,
    schedule: {
      hour: definition.schedule.hour,
      minute: definition.schedule.minute,
      weekday: definition.schedule.weekday,
    },
    zone: definition.zone,
  };
  return createHash('sha256').update(JSON.stringify(canonical)).digest('hex');
}
export function routineReceiptMatches(receipt: OwnerRoutineToolReceipt, args: OwnerRoutineToolArgs): boolean {
  if (args.operation === 'propose' || args.operation === 'propose_adjust' || receipt.outcome.operation !== args.operation)
    return false;
  if(args.operation==='adjust') return receipt.outcome.operation==='adjust' && receipt.outcome.routine_id===createHash('sha256').update(JSON.stringify(['sotto.fresh-routine.v1',receipt.action_id])).digest('hex') && receipt.outcome.routine_id!==args.routine_id && receipt.outcome.previous_routine_id===args.routine_id && receipt.outcome.previous_definition_hash===args.previous_definition_hash && receipt.outcome.definition_hash===routineDefinitionHash(args.definition);
  return args.operation === 'cancel' ? receipt.outcome.routine_id === args.routine_id : receipt.outcome.definition_hash === routineDefinitionHash(args.definition);
}
export function routineProposalText(preview: RoutinePreview): string {
  if('operation' in preview) return `Change this routine:\nFrom: ${routineScheduleText(preview.previous_definition)}\nTo: ${routineScheduleText(preview.definition)}\n${preview.definition.prompt}\n\nFuture results will arrive here. Update it?`;
  const { definition: d } = preview;
  return `${routineScheduleText(d, true)}:\n${d.prompt}\n\nI'll send the result here. Set this up?`;
}
export function routineAcknowledgement(receipt: OwnerRoutineToolReceipt,args?:OwnerRoutineToolArgs): string {
  if(receipt.outcome.operation==='adjust'){if(args?.operation!=='adjust')throw new Error('Routine adjustment acknowledgement unavailable');return `Updated. ${routineScheduleText(args.definition)}. Results will arrive here.`;}
  return receipt.outcome.operation === 'create' ? 'Set up. Results will arrive here.' : 'Stopped that routine.';
}
export const clearRoutineYes = (text: string) => /^(yes|yes please|yes, please|yep|yup|sure|do it|set it up)[.!]?$/i.test(text.trim());

function routineScheduleText(d:z.infer<typeof routineDefinitionSchema>, currentOwnerTime=false):string {
  // Creation captures the current owner zone. Adjustments retain the saved zone,
  // which may differ after travel or a settings change, so name it plainly.
  let zone=d.zone;
  try {zone=new Intl.DateTimeFormat('en-US',{timeZone:d.zone,timeZoneName:'longGeneric'}).formatToParts(new Date(0)).find(part=>part.type==='timeZoneName')?.value??zone;} catch { /* Keep an unknown saved zone explicit. */ }
  const clock=currentOwnerTime?'your time':d.zone==='UTC'?'UTC':zone;
  if(d.schedule.hour===null)return `${d.schedule.minute===0?'Every hour on the hour':`Every hour at ${d.schedule.minute} minutes past`} ${clock}`;
  const days=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
  const weekday=d.schedule.weekday;
  const selected=Array.isArray(weekday)?weekday.map(day=>days[day]!) : [];
  const list=selected.length<3?selected.join(' and '):`${selected.slice(0,-1).join(', ')}, and ${selected.at(-1)}`;
  const time=`${d.schedule.hour%12||12}${d.schedule.minute?`:${String(d.schedule.minute).padStart(2,'0')}`:''}${d.schedule.hour<12?'am':'pm'}`;
  return `${weekday===null?'Every day':weekday==='first_of_month'?'On the first day of every month':weekday==='weekdays'?'Every weekday (Monday–Friday)':Array.isArray(weekday)?`Every ${list}`:`Every ${days[weekday]}`} at ${time} ${clock}`;
}
/** Mirrors fresh_routines.schedule_key: weekday-set aliases of daily, weekdays or one weekday are one schedule. */
export function routineScheduleKey(s:z.infer<typeof routineScheduleSchema>):string {const d=s.weekday;return JSON.stringify([s.hour,s.minute,d===null?[0,1,2,3,4,5,6]:d==='weekdays'?[1,2,3,4,5]:d==='first_of_month'?['monthly']:Array.isArray(d)?d:[d]]);}
export function routinePreviewMatches(p:RoutinePreview,a:OwnerRoutineToolArgs):boolean {if(p.definition_hash!==routineDefinitionHash(p.definition))return false;if(a.operation==='propose')return !('operation' in p)&&p.definition.name===a.name&&p.definition.prompt===a.prompt&&JSON.stringify(p.definition.schedule)===JSON.stringify(a.schedule);if(a.operation!=='propose_adjust'||!('operation' in p))return false;const old=p.previous_definition;return p.routine_id===a.routine_id&&p.previous_definition_hash===routineDefinitionHash(old)&&old.name===p.definition.name&&old.prompt===p.definition.prompt&&old.zone===p.definition.zone&&old.destination===p.definition.destination&&JSON.stringify(p.definition.schedule)===JSON.stringify(a.schedule)&&routineScheduleKey(old.schedule)!==routineScheduleKey(p.definition.schedule);}
