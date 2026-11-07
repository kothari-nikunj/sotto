import {googleReadFailureMessage,isGoogleReadFailureStage,isGoogleReadHttpStatus,isGoogleReadFailurePhase,googleReadPhaseMatches,type GoogleReadFailureStage,type GoogleReadFailureDetails} from './google-read-diagnostic.js';
import {calendarIdSchema} from '../tools/owner-calendar-contract.js';
import {contactSnapshotSchema,contactResourceSchema} from '../tools/owner-contacts-contract.js';
import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import { ownerContinuityEndpoint, readOwnerContinuityJson, OwnerContinuityUnavailable, type OwnerContinuityClientOptions } from './owner-continuity-client.js';
import { readAuthority, sameConsent, type SourceAuthorityReader } from './source-authority.js';

const gmail = z.object({
  id: z.string().max(128), threadId: z.string().max(128), from: z.string().max(1024),
  to: z.string().max(1024), cc: z.string().max(1024), subject: z.string().max(2048),
  date: z.string().max(128), snippet: z.string().max(4096), body: z.string().max(32 * 1024),
  labels: z.array(z.string().max(128)).max(40),
}).strict();
const calendarEvidenceGuide = 'Use start/end for the current occurrence; originalStartTime identifies a recurring occurrence and is not its current scheduled time. transparency=transparent means the event does not block time. An attendee owner=true identifies the owner response; declined is not an owner commitment, and tentative remains uncertain. owner_participates=false means shared-calendar context only: keep useful titles/times visible in a clearly labeled shared context section, including timed events, never assert the owner is attending or busy. Use plain language such as Other calendars; never print internal field names or boolean values in the reply. A missing flag does not establish participation. Describe conflicts by their actual intervals; bounded conflicts do not establish whole-day unavailability. Availability claims must stay within the observed coverage and readable calendars; partial coverage cannot prove free time.';
const calendar = z.object({
  calendar_id:calendarIdSchema.optional(),
  owner_participates:z.boolean().optional(),
  transparency:z.string().max(128).optional(), eventType:z.string().max(128).optional(),
  id: z.string().max(256), summary: z.string().max(2048), description: z.string().max(16 * 1024),
  originalStartTime: z.object({dateTime:z.string().max(128).optional(),date:z.string().max(128).optional(),timeZone:z.string().max(128).optional()}).strict().optional(),
  recurringEventId: z.string().regex(/^[A-Za-z0-9_@.-]{1,256}$/).optional(),
  location: z.string().max(2048), status: z.string().max(128),
  start: z.object({ dateTime: z.string().max(128).optional(), date: z.string().max(128).optional(), timeZone: z.string().max(128).optional() }).strict(),
  end: z.object({ dateTime: z.string().max(128).optional(), date: z.string().max(128).optional(), timeZone: z.string().max(128).optional() }).strict(),
  attendees: z.array(z.object({ email: z.string().max(320), responseStatus: z.string().max(128), owner:z.boolean().optional() }).strict()).max(100),
}).strict();
const observation = z.object({ status: z.enum(['ok', 'partial']), observed_at: z.string().max(64),
  complete: z.boolean(), coverage: z.object({ since: z.string().max(64).nullable(),
    until: z.string().max(64).nullable() }).strict() }).strict();
const envelope = z.object({ version: z.literal(1), source: z.enum(['gmail', 'calendar','google_contacts','google']),
  operation: z.enum(['gmail_search', 'gmail_get', 'gmail_thread_get', 'calendar_list', 'calendar_get','contacts_search','contacts_get','capabilities']),
  observation });
const calendarTimestamp = z.string().max(32).datetime({ offset: true, precision: 0,
  message: 'Use an explicit timezone timestamp with whole seconds, such as 2026-10-04T00:00:00-07:00 or 2026-10-04T07:00:00Z.' })
  .refine(value => {
    const year = new Date(value).getUTCFullYear();
    return year >= 1 && year <= 9999;
  }, { message: 'Calendar timestamp must normalize to a year between 0001 and 9999.' });
const calendarWindow = z.object({ start: calendarTimestamp, end: calendarTimestamp }).strict()
  .refine(({ start, end }) => {
    const duration = Date.parse(end) - Date.parse(start);
    return duration > 0 && duration <= 31 * 24 * 60 * 60 * 1000;
  }, { message: 'Calendar end must follow start by no more than 31 days.' });

const operations = {
  capabilities:{source:'google',input:z.object({}).strict(),result:z.object({item:z.object({status:z.literal('ok'),connected:z.boolean(),gmail_read:z.boolean(),gmail_draft:z.boolean(),gmail_write:z.boolean(),gmail_send:z.boolean(),calendar_read:z.boolean(),calendar_write:z.boolean(),contacts_read:z.boolean(),contacts_write:z.boolean()}).strict()})},
  contacts_search:{source:'google_contacts',input:z.object({query:z.string().min(1).max(256),max:z.number().int().min(1).max(30)}).strict(),result:z.object({items:z.array(contactSnapshotSchema).max(30)})},
  contacts_get:{source:'google_contacts',input:z.object({resource_name:contactResourceSchema}).strict(),result:z.object({item:contactSnapshotSchema})},
  gmail_search: { source: 'gmail', input: z.object({ query: z.string().min(1).max(256), max: z.number().int().min(1).max(20) }).strict(),
    result: z.object({ items: z.array(gmail).max(20) }) },
  gmail_thread_get: { source: 'gmail', input: z.object({ thread_id: z.string().regex(/^[A-Za-z0-9_-]{1,128}$/) }).strict(),
    result: z.object({thread_id:z.string().regex(/^[A-Za-z0-9_-]{1,128}$/),items:z.array(gmail.extend({body:z.string().refine(value=>Buffer.byteLength(value,'utf8')<=16*1024),body_truncated:z.boolean(),internal_date:z.string().regex(/^[0-9]{1,16}$/)})).min(1).max(20),omitted_messages:z.number().int().nonnegative()}) },
  gmail_get: { source: 'gmail', input: z.object({ message_id: z.string().regex(/^[A-Za-z0-9_-]{1,128}$/) }).strict(),
    result: z.object({ item: gmail }) },
  calendar_list: { source: 'calendar', input: calendarWindow,
    result: z.object({ items: z.array(calendar).max(100), calendars: z.array(z.object({calendar_id:z.string().max(1024),status:z.enum(['ok','partial','unavailable'])}).strict()).max(51).optional() }) },
  calendar_get: { source: 'calendar', input: z.object({ event_id: z.string().regex(/^[A-Za-z0-9_@.-]{1,256}$/),calendar_id:calendarIdSchema.optional() }).strict(),
    result: z.object({ item: calendar }) },
} as const;
export type GoogleReadOperation = keyof typeof operations;
export interface GoogleReadOptions extends OwnerContinuityClientOptions { sourceAuthority: SourceAuthorityReader }
export class GoogleReadUnavailable extends Error {
  readonly stage:GoogleReadFailureStage;
  constructor(stage:GoogleReadFailureStage='execution_context',details:GoogleReadFailureDetails={}) {
    const safe=isGoogleReadFailureStage(stage)?stage:'unknown';
    super(googleReadFailureMessage(safe,details));this.stage=safe;
  }
}

/** Only a current receiver grant can authorize this bounded provider read. */
export async function readOwnerGoogle(options: GoogleReadOptions, operation: GoogleReadOperation,
                                      args: Record<string, unknown>, sourceRevision: string,
                                      signal?: AbortSignal): Promise<unknown> {
  let stage:GoogleReadFailureStage = 'input_validation';
  try {
    ownerContinuityEndpoint(options, 'google-read');
    const spec = operations[operation];
    if (!spec || !spec.input.safeParse(args).success || !/^[a-f0-9]{64}$/.test(sourceRevision)) throw new Error('invalid input');
    stage = 'authority_before';
    const before = await readAuthority(options.sourceAuthority, signal);
    if (!before.complete || before.revision !== sourceRevision || (spec.source!=='google'&&!before.google_sources.includes(spec.source))) throw new Error('grant unavailable');
    stage = 'receiver_transport';
    const raw = await readOwnerContinuityJson(options, 'google-read', {
      operation, arguments: operation === 'calendar_list'
        ? { start: new Date(args.start as string).toISOString().replace('.000Z', 'Z'),
            end: new Date(args.end as string).toISOString().replace('.000Z', 'Z') }
        : args, source_revision: sourceRevision,
    }, signal);
    stage = 'result_schema';
    const value = envelope.and(spec.result).safeParse(raw);
    stage = 'authority_after';
    const after = await readAuthority(options.sourceAuthority, signal);
    const expectedKeys = (operation.endsWith('_get')||operation==='capabilities') ? ['item', 'observation', 'operation', 'source', 'version']
      : ['items', 'observation', 'operation', 'source', 'version'];
    if (operation === 'gmail_thread_get') expectedKeys.splice(0,expectedKeys.length,'items','observation','omitted_messages','operation','source','thread_id','version');
    if (operation === 'calendar_list' && raw && typeof raw === 'object' && 'calendars' in raw) expectedKeys.unshift('calendars');
    stage = !value.success ? 'result_schema' : !sameConsent(before, after) ? 'authority_after' : 'result_contract';
    if (!value.success || value.data.source !== spec.source || value.data.operation !== operation ||
        !raw || typeof raw !== 'object' || Array.isArray(raw) ||
        Object.keys(raw).sort().join(',') !== expectedKeys.join(',') ||
        value.data.observation.complete !== (value.data.observation.status === 'ok') ||
        !sameConsent(before, after)) throw new Error('grant changed or result invalid');
    stage = 'target_contract';
    if(operation==='gmail_thread_get'){
      const thread=value.data as {thread_id:string;items:Array<{id:string;threadId:string;labels:string[];body_truncated:boolean;internal_date:string}>;omitted_messages:number;observation:{complete:boolean}};
      if(thread.thread_id!==args.thread_id||thread.items.some(item=>!/^[A-Za-z0-9_-]{1,128}$/.test(item.id)||item.threadId!==args.thread_id||item.labels.includes('DRAFT'))||new Set(thread.items.map(item=>item.id)).size!==thread.items.length||
          thread.observation.complete!==(thread.omitted_messages===0&&!thread.items.some(item=>item.body_truncated))||
          thread.items.some((item,index)=>index>0&&BigInt(item.internal_date)<BigInt(thread.items[index-1]!.internal_date)))throw new Error('Gmail thread target changed');
    }
    if(operation==='calendar_get'){const item=(value.data as {item:{id:string;calendar_id?:string}}).item;if(item.id!==args.event_id||item.calendar_id!==args.calendar_id)throw new Error('Calendar target changed');}
    return value.data;
  } catch(error) {
    if(error instanceof OwnerContinuityUnavailable&&isGoogleReadFailureStage(error.googleReadStage))stage=error.googleReadStage;
    try { console.warn('[sotto-google-read]', JSON.stringify({
      operation:Object.hasOwn(operations,operation)?operation:'unknown', stage, outcome:'unavailable',
    })); } catch { /* Diagnostics must not change failure behavior. */ }
    const details:GoogleReadFailureDetails=error instanceof OwnerContinuityUnavailable?{
      ...(isGoogleReadHttpStatus(error.googleReadDetails.httpStatus)?{httpStatus:error.googleReadDetails.httpStatus}:{}),
      ...(isGoogleReadFailurePhase(error.googleReadDetails.phase)&&googleReadPhaseMatches(operation,error.googleReadDetails.phase)?{phase:error.googleReadDetails.phase}:{}),
    }:{};
    throw new GoogleReadUnavailable(stage,details);
  }
}

/** Explicit unattended factories expose only bounded Gmail/Calendar reads, never writes. */
export function createGoogleReadTools(options: GoogleReadOptions, mode: 'chat' | 'unattended' = 'chat') {
  ownerContinuityEndpoint(options, 'google-read');
  const tools: Record<string, ReturnType<typeof createTool>> = {};
  for (const operation of Object.keys(operations) as GoogleReadOperation[]) {
    if (mode !== 'chat' && operation === 'gmail_thread_get') continue;
    if (mode === 'unattended' && !['gmail_search','gmail_get','calendar_list','calendar_get'].includes(operation)) continue;
    const spec = operations[operation];
    tools[operation] = createTool({ id: operation,
      description: operation==='capabilities'?'Read original local Google connection capability flags intersected with current grants; no provider request or new permission.':operation==='contacts_search'?'Search current Google Contacts by name/email/phone, returning exact resource names and field arrays; clarify ambiguous matches.':operation==='contacts_get'?'Read one exact current Google contact before editing/deleting.':operation === 'gmail_search' ? 'Search one bounded Gmail page of up to 20 message metadata rows. Empty or complete coverage applies to this query, not the whole mailbox.'
        : operation === 'gmail_thread_get' ? 'Read the exact Gmail thread_id returned by a current permitted Gmail message or saved reference. Returns up to 20 newest messages ordered by provider internal_date, with full projected wording within bounds. Use when later replies or original thread context are needed instead of rediscovering a known thread through search. Partial coverage, omitted_messages or body_truncated means incomplete evidence; never infer absence or completion from omitted content. Unsent drafts are excluded. Check SENT labels before claiming an owner-sent reply. A shared thread or quoted prior text alone does not prove the current author said it or completed an action; distinguish newly authored wording from quoted content and verify exact message, author, wording and time. No attachments or raw MIME.'
        : operation === 'gmail_get' ? 'Read one Gmail message by exact ID, projecting headers, body text, snippet and labels; no attachments or raw MIME.'
          : operation === 'calendar_list' ? 'Read up to 100 events across configured Google calendars (all readable calendars by default) in an explicit timezone-aware time window of at most 31 days (whole seconds; offsets or UTC Z). Boundaries are normalized to UTC; failed calendars and bounded continuation are marked partial. Keep calendar_id with each event ID. An unreadable or hidden calendar is not covered just because the account can access it. ' + calendarEvidenceGuide
            : 'Read one exact event by ID, optionally on an explicitly known calendar_id. Omit calendar_id for primary. Calendar listings include the exact calendar_id for each event; preserve it with the event ID. Preserve the exact calendar ID for reviewed edits. ' + calendarEvidenceGuide,
      inputSchema: spec.input,
      execute: async (input, context) => {
        if (context.requestContext?.get('mode') !== mode ||
            typeof context.requestContext?.get('runId') !== 'string' ||
            (mode === 'chat' && typeof context.requestContext?.get('ownerRequestKey') !== 'string') ||
            typeof context.requestContext?.get('sourceRevision') !== 'string') throw new GoogleReadUnavailable();
        return readOwnerGoogle(options, operation, input as Record<string, unknown>,
          context.requestContext.get('sourceRevision') as string, context.abortSignal);
      },
    });
  }
  return tools;
}
