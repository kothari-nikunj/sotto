import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import type { OperationalJournal, ExplicitOwnerLoopAuthority } from '../journal/index.js';
import { readAuthority, SourceAuthorityChanged, type SourceAuthorityReader } from '../agent/source-authority.js';
import { ActionHeldError, stableActionIdentity } from './action-gate.js';
import { calendarProposalSchema, calendarProposalInputSchema, calendarRsvpProposalSchema, calendarRsvpInputSchema, calendarCreateInputSchema, calendarMoveUpdateInputSchema,calendarMoveUpdateProposalSchema,calendarUpdateProposalSchema,calendarIdSchema,calendarReviewText, ownerCalendarKind, type CalendarArgs } from './owner-calendar-contract.js';
import type { OwnerCalendarClient } from './owner-calendar-client.js';
export function createOwnerCalendarTools(options: {
    client: OwnerCalendarClient;
    journal(): OperationalJournal | undefined;
    sourceAuthority: SourceAuthorityReader;
}) {
    type Context = {
        requestContext?: {
            get(key: string): unknown;
        };
        abortSignal?: AbortSignal;
    };
    const trusted = (ctx: Context) => {
        const journal = options.journal(), runId = ctx.requestContext?.get('runId'), eventId = ctx.requestContext?.get('ownerRequestKey');
        if (!journal || ctx.requestContext?.get('mode') !== 'chat' || typeof runId !== 'string' || typeof eventId !== 'string' || ctx.requestContext?.get('allowOwnerSkill') !== true || ctx.abortSignal?.aborted)
            throw new Error('Calendar move requires one current owner message');
        const authority: ExplicitOwnerLoopAuthority = { runId, accountId: options.client.identity.account_id, ownerId: options.client.identity.owner_id, lineKey: options.client.identity.line_key, channel: options.client.identity.channel, scopeHash: options.client.scopeHash };
        journal.ownerReminderToolAdmission(authority, eventId, true);
        return { journal, authority, eventId };
    };
    const sourceCurrent = async (ctx: Context) => {
        const current = await readAuthority(options.sourceAuthority, ctx.abortSignal);
        if (ctx.abortSignal?.aborted || !current.complete || current.revision !== ctx.requestContext?.get('sourceRevision') || !current.google_sources.includes('calendar'))
            throw new SourceAuthorityChanged();
    };
    const proposed=(preview:Parameters<typeof calendarReviewText>[0])=>({status:'proposed',review_text:calendarReviewText(preview),needs_owner_confirmation:true,instruction:'Finish this turn normally with review_text exactly. The runtime delivers the canonical review. Wait for a distinct later owner yes; do not apply now.'});
    const execute = async (args: CalendarArgs, ctx: Context) => {
        const { journal, authority, eventId } = trusted(ctx);
        await sourceCurrent(ctx);
        const revision = ctx.requestContext?.get('sourceRevision') as string;
        if (args.operation === 'propose_move' || args.operation === 'propose_rsvp' || args.operation === 'propose_create' || args.operation==='propose_update'||args.operation==='propose_move_update') {
            const admitted = journal.ownerReminderToolAdmission(authority, eventId, true), binding = stableActionIdentity(eventId, ownerCalendarKind, args);
            const result = await options.client.call('preview', { ...admitted, actionId: binding.actionId, args }, ctx.abortSignal);
            await sourceCurrent(ctx);
            if (result.status !== 'preview' && result.status !== 'rejected')
                throw new Error('Calendar review unavailable');
            const request = journal.prepareOwnerCalendarAction(authority, eventId, args, revision);
            const saved = journal.readOwnerCalendarAction(request.actionId, authority);
            if (saved.status === 'succeeded' || saved.status === 'rejected')
                return saved.preview&&(saved.preview.review.calendar_id!=='primary'||['update','move_update'].includes(saved.preview.review.operation))?proposed(saved.preview):{ status: saved.status === 'succeeded' ? 'proposed' : 'rejected' };
            journal.beginAction(request.actionId);
            journal.settleOwnerCalendarAction(request.actionId, authority, result.status === 'preview' ? result : result.code);
            return result.status==='preview'&&(result.review.calendar_id!=='primary'||['update','move_update'].includes(result.review.operation))?proposed(result):{ status: result.status === 'preview' ? 'proposed' : 'rejected' };
        }
        if(args.operation==='update'||args.operation==='move_update'){const admitted=journal.ownerReminderToolAdmission(authority,eventId,true),binding=stableActionIdentity(eventId,ownerCalendarKind,args);options.client.preflight({...admitted,actionId:binding.actionId,args});}
        const request = journal.prepareOwnerCalendarAction(authority, eventId, args, revision), saved = journal.readOwnerCalendarAction(request.actionId, authority);
        if (saved.status === 'succeeded')
            return { status: 'recorded', outcome: saved.receipt!.outcome };
        if (saved.status === 'rejected')
            return { status: 'rejected', code: saved.rejection };
        if (saved.status !== 'pending')
            throw new ActionHeldError();
        journal.beginAction(request.actionId);
        try {
            const result = await options.client.call('apply', request, ctx.abortSignal);
            if (result.status === 'committed' || result.status === 'rejected') {
                journal.settleOwnerCalendarAction(request.actionId, authority, result.status === 'committed' ? result : result.code);
                return result.status === 'committed' ? { status: 'recorded', outcome: result.outcome } : { status: 'rejected', code: result.code };
            }
            throw new Error('Calendar receipt unavailable');
        }
        catch {
            if (journal.getAction(request.actionId)?.status === 'executing')
                journal.settleAction(request.actionId, 'unknown');
            throw new ActionHeldError();
        }
    };
    const confirm = async(input:unknown,ctx:Context,operation:'move'|'rsvp'|'create'|'update'|'move_update')=>{
        z.object({}).strict().parse(input);
        const {journal,authority,eventId}=trusted(ctx);
        await sourceCurrent(ctx);
        const selected=journal.selectOwnerCalendarProposal(authority,eventId,ctx.requestContext?.get('sourceRevision') as string);
        if(selected.operation!==operation)throw new Error('Accepted Calendar review unavailable');
        return execute(selected,ctx);
    };
    return {
        calendar_move_update_review:createTool({id:'calendar_move_update_review',description:'Review a combined explicit-offset time and selected title/location/agenda edit for one exact timed event on primary or an explicitly known calendar or recurring occurrence. Full before/after review and distinct later yes; no all-day or series move, guests or recurrence change.',inputSchema:calendarMoveUpdateInputSchema.omit({operation:true}),execute:async(input,ctx)=>execute(calendarMoveUpdateProposalSchema.parse({operation:'propose_move_update',...calendarMoveUpdateInputSchema.omit({operation:true}).parse(input)}),ctx)}),
        calendar_move_update_confirm:createTool({id:'calendar_move_update_confirm',description:'Apply only the exact accepted combined time/details review after a distinct owner yes. Empty input.',inputSchema:z.object({}).strict(),execute:async(input,ctx)=>confirm(input,ctx,'move_update')}),
        calendar_update_review:createTool({id:'calendar_update_review',description:'Review exact title, location or agenda edits on one Calendar event on primary or an explicitly known calendar, timed or all-day, single/occurrence or explicitly entire recurring series. Resolve with current calendar_list/get. Omitted fields unchanged; empty string clears selected field. Full old/new details before distinct yes; no time/guest/recurrence edits.',inputSchema:calendarUpdateProposalSchema.omit({operation:true}),execute:async(input,ctx)=>execute({operation:'propose_update',...calendarUpdateProposalSchema.omit({operation:true}).parse(input)},ctx)}),
        calendar_update_confirm:createTool({id:'calendar_update_confirm',description:'Update only exact accepted Calendar detail review after distinct owner yes. Empty input; no changed fields or invented approval.',inputSchema:z.object({}).strict(),execute:async(input,ctx)=>confirm(input,ctx,'update')}),
        calendar_create_review:createTool({id:'calendar_create_review',description:'Review creation of one timed primary Calendar event. Check current Calendar reads first; clarify unresolved venue or guests. Supply all fields, [] for no guests and empty optional location/description. Full effective guest list, times, place and agenda are reviewed before distinct later yes. No recurrence/all-day or conflict-free guarantee.',inputSchema:calendarCreateInputSchema.omit({operation:true}),execute:async(input,ctx)=>execute({operation:'propose_create',...calendarCreateInputSchema.omit({operation:true}).parse(input)},ctx)}),
        calendar_create_confirm:createTool({id:'calendar_create_confirm',description:'Create only the exact accepted event review after a distinct clear owner yes. Empty input; never change details or invent approval.',inputSchema:z.object({}).strict(),execute:async(input,ctx)=>confirm(input,ctx,'create')}),
        calendar_move_review: createTool({ id: 'calendar_move_review', description: 'Review a requested time change of one exact timed Calendar occurrence on primary or an explicitly known calendar. Resolve event ID with current Calendar reads; clarify ambiguity. Supply explicit offset start/end. Full old/new times and guest notifications are reviewed before a distinct later yes. No series/all-day or availability guarantee.', inputSchema: calendarProposalInputSchema.omit({ operation: true }), execute: async (input, ctx) => execute({ operation: 'propose_move', ...calendarProposalInputSchema.omit({ operation: true }).parse(input) }, ctx) }),
        calendar_move_confirm: createTool({ id: 'calendar_move_confirm', description: 'Move only the exact accepted Calendar review after a distinct clear owner yes. Empty input; never invent approval or change content. Guests are notified.', inputSchema: z.object({}).strict(), execute: async(input,ctx)=>confirm(input,ctx,'move') }),
        calendar_rsvp_review:createTool({id:'calendar_rsvp_review',description:'Review an exact Calendar invitation on primary or an explicitly known calendar response. Resolve event ID with actual Calendar list/get. Supports timed/all-day, occurrence and exact entire series; discover master ID from provider recurringEventId. Accepted, declined or tentative with optional own-attendee comment. Full review before distinct later yes.',inputSchema:calendarRsvpInputSchema.omit({operation:true}),execute:async(input,ctx)=>execute(calendarRsvpProposalSchema.parse({operation:'propose_rsvp',...input}),ctx)}),
        calendar_rsvp_confirm:createTool({id:'calendar_rsvp_confirm',description:'Apply only the exact accepted RSVP review after a distinct owner yes. Empty input; never invent approval or change the invitation/response.',inputSchema:z.object({}).strict(),execute:async(input,ctx)=>confirm(input,ctx,'rsvp')})
    };
}
export const CALENDAR_EXECUTION_GUIDE = 'For original Schedule creation: use current calendar_list to check busy events, clarify exact guest addresses and unresolved venues, then calendar_create_review with the full event details. Stop after full review; distinct later yes permits only calendar_create_confirm with empty input. No conflict-free guarantee or automatic venue lookup. For existing events: calendar_list reads the configured calendar selection and reports per-calendar coverage; calendar_get accepts an explicitly known calendar_id within that selection. Treat owner_participates=false as shared-calendar context, not an owner commitment or busy time. Keep useful titles/times visible with that label, including timed events. Use plain language such as Other calendars; never print internal field names or boolean values in the reply. A missing participation flag does not prove attendance. Do not claim unselected, hidden or unavailable calendars were covered. Omit calendar_id for primary, supply the exact same non-primary ID to all review tools; do not infer or normalize it. Use current calendar_get/list to resolve one exact timed occurrence, retaining its exact known calendar_id when non-primary; calendar_move_review presents the complete trusted old/new time review. Stop after the review. Only a distinct subsequent clear yes permits calendar_move_confirm with empty input. Edits need a new review. For title, location or agenda edits use calendar_update_review with event_id and selected fields. Omitted fields remain unchanged, empty strings explicitly clear; resolve timed/all-day event or explicit entire-series master with original reads. Finish normally with review_text exactly, then distinct later yes permits only calendar_update_confirm. Original google-action calendar-update with only start/end dateTime objects uses the existing move review; after a distinct yes use empty calendar-move-confirm. When any details are supplied, use the combined review and calendar-move-update-confirm. For combined time and details use calendar_move_update_review with both explicit-offset start/end and nonempty selected fields on one timed single event or exact recurring occurrence. Finish normally with review_text exactly; distinct later yes permits only calendar_move_update_confirm. Never split mixed time/detail requests into separate effects. Original google-action calendar-rsvp supports exact --event-id, --response and optional known --calendar; calendar-rsvp-confirm is empty after distinct yes. Optional --comment is reviewed exactly as your own attendee comment; empty leaves it unchanged. For RSVP use calendar_rsvp_review and distinct later yes via calendar_rsvp_confirm. Resolve exact provider invitation; an occurrence recurringEventId can identify its master via calendar_get. Entire-series RSVP must be explicitly reviewed, never inferred from an occurrence. No series/all-day moves, attendee edits or conflict-free claim. Unknown outcomes remain held and are inspected, never retried.';

export function createCalendarCommand(options:Parameters<typeof createOwnerCalendarTools>[0]) {
 const tools=createOwnerCalendarTools(options);
 return async(invocation:import('./script-runner.js').ScriptInvocation,context:{mode?:unknown;runId?:unknown;ownerRequestKey?:unknown;allowOwnerSkill?:unknown;sourceRevision?:unknown;signal?:AbortSignal})=>{
  if(invocation.id!=='google-action'||invocation.mode!=='chat'||context.mode!=='chat')throw new Error('Calendar update command unavailable');
  const [operation,...argv]=invocation.args;
  const requestContext={get:(key:string)=>(context as Record<string,unknown>)[key]};
  const ctx={requestContext,abortSignal:context.signal} as never;
  if(operation==='calendar-update-confirm'||operation==='calendar-move-update-confirm'||operation==='calendar-rsvp-confirm'||operation==='calendar-move-confirm'){
   if(argv.length)throw new Error('Calendar confirmation takes no arguments');
   const result=await (operation==='calendar-update-confirm'?tools.calendar_update_confirm:operation==='calendar-rsvp-confirm'?tools.calendar_rsvp_confirm:operation==='calendar-move-confirm'?tools.calendar_move_confirm:tools.calendar_move_update_confirm).execute!({},ctx);
   const selected=(result as {outcome?:{calendar_id?:string}}).outcome?.calendar_id;
   return {backend:'shared-calendar',coverage:selected===undefined?'one_calendar_event':selected==='primary'?'one_primary_event':'one_selected_event',result};
  }
  if(operation==='calendar-rsvp'){
   const values:Record<string,string>={};
   for(let i=0;i<argv.length;i+=2){const key=argv[i]!;if(!['--event-id','--response','--calendar','--comment'].includes(key)||values[key]!==undefined||typeof argv[i+1]!=='string')throw new Error('Calendar RSVP arguments invalid');values[key]=argv[i+1]!;}
   const target=values['--calendar']===undefined||values['--calendar']==='primary'?{}:{calendar_id:values['--calendar']};
   const parsed=calendarRsvpProposalSchema.parse({operation:'propose_rsvp',event_id:values['--event-id'],response:values['--response'],...(values['--comment']===undefined?{}:{comment:values['--comment']}),...target});
   return {backend:'shared-calendar',coverage:Object.keys(target).length?'one_selected_event':'one_primary_event',result:await tools.calendar_rsvp_review.execute!({event_id:parsed.event_id,response:parsed.response,...(parsed.comment===undefined?{}:{comment:parsed.comment}),...target},ctx)};
  }
  if(operation!=='calendar-update')throw new Error('Calendar update command unavailable');
  const values:Record<string,string>={};
  for(let i=0;i<argv.length;i+=2){const key=argv[i]!;if(!['--event-id','--fields-json','--calendar'].includes(key)||values[key]!==undefined||typeof argv[i+1]!=='string')throw new Error('Calendar update arguments invalid');values[key]=argv[i+1]!;}
  if(!values['--event-id']||values['--fields-json']===undefined||values['--calendar']!==undefined&&!calendarIdSchema.safeParse(values['--calendar']).success)throw new Error('Calendar update target unavailable');
  const target=values['--calendar']&&values['--calendar']!=='primary'?{calendar_id:values['--calendar']}:{};
  const coverage=Object.keys(target).length?'one_selected_event':'one_primary_event';
  const fields:unknown=JSON.parse(values['--fields-json']);
  if(fields&&typeof fields==='object'&&!Array.isArray(fields)&&('start' in fields||'end' in fields)){
   // Keep the original command shape, but delegate both changes to one trusted review.
   const combined=z.object({start:z.object({dateTime:z.string()}).strict(),end:z.object({dateTime:z.string()}).strict(),summary:z.string().optional(),location:z.string().optional(),description:z.string().optional()}).strict().parse(fields);
   const {start,end}=combined;
   const supplied=Object.fromEntries(Object.entries(combined).filter(([key,value])=>['summary','location','description'].includes(key)&&value!==undefined));
   if(Object.keys(supplied).length===0)return {backend:'shared-calendar',coverage,result:await tools.calendar_move_review.execute!({event_id:values['--event-id'],...target,start:start.dateTime,end:end.dateTime},ctx)};
   const details=calendarUpdateProposalSchema.parse({operation:'propose_update',event_id:values['--event-id'],...target,fields:supplied}).fields;
   return {backend:'shared-calendar',coverage,result:await tools.calendar_move_update_review.execute!({event_id:values['--event-id'],...target,start:start.dateTime,end:end.dateTime,fields:details},ctx)};
  }
  const parsed=calendarUpdateProposalSchema.parse({operation:'propose_update',event_id:values['--event-id'],...target,fields});
  return {backend:'shared-calendar',coverage,result:await tools.calendar_update_review.execute!({event_id:parsed.event_id,...target,fields:parsed.fields},ctx)};
 };
}
