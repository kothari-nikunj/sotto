import {createHash} from 'node:crypto';
import {z} from 'zod';
import {createOwnerToolTransport} from './owner-tool-transport.js';
import type {OwnerToolClientOptions} from './owner-contract.js';
import {calendarIdSchema} from './owner-calendar-contract.js';
import {stableActionIdentity} from './action-gate.js';
const hex = z.string().regex(/^[a-f0-9]{64}$/);
export const ownerSkillRequestKind = 'owner_skill_request';
const ownerSelector = z.string().min(1).max(200).refine(value =>
  value === value.trim() && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
export const meetingOccurrenceSchema = z.object({
  event_id: z.string().min(1).max(512).refine(value => value === value.trim() && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value)),
  start: z.string().datetime({offset: true}),
  calendar_id: calendarIdSchema.optional(),
}).strict();
const companySelector = z.string().min(1).max(400).refine(value =>
  value === value.trim() && Array.from(value).length <= 200 &&
  !/[\u0000-\u001f\u007f-\u009f\u2028\u2029\ud800-\udfff]/u.test(value));
const acceptedOwnerTurn = z.object({event_id:hex, text:z.string().min(1).max(1000), admitted_at_ms:z.number().int(),
        accepted_dispatch_id:z.string().min(1).max(160), source_revision:hex,
        thread_digest:hex}).strict();
export const ownerSkillRequestArgsSchema = z.discriminatedUnion('skill', [
  z.object({skill: z.literal('meeting-prep'), selection: z.discriminatedUnion('mode', [
    z.object({mode: z.literal('sweep')}).strict(),
    z.object({mode: z.literal('focus'), focus: ownerSelector, meeting: meetingOccurrenceSchema.optional()}).strict(),
  ])}).strict(),
  z.object({skill:z.literal('proactive'),selection:z.object({mode:z.literal('current')}).strict()}).strict(),
  z.object({skill: z.literal('relationship-pulse'), selection: z.object({mode: z.literal('current')}).strict()}).strict(),
  z.object({skill: z.literal('followup'), selection: z.object({mode: z.enum(['recent', 'backlog'])}).strict()}).strict(),
  z.object({skill: z.literal('ask-research'), selection:
    z.discriminatedUnion('mode', [
      z.object({mode: z.literal('person'), person: ownerSelector}).strict(),
      z.object({mode: z.literal('company'), company: companySelector, operation: z.literal('website').optional()}).strict(),
    ]),
    source_revision: hex.optional(),
    prior_selection: z.union([
      z.object({event_id:hex, action_id:hex, args_hash:hex}).strict(),
      // Populated from the accepted journal turn, never from model command arguments.
      acceptedOwnerTurn,
    ]).optional(),
    prior_request: acceptedOwnerTurn.optional(),
    prior_prep: z.object({provider_message_id:z.string().min(1).max(512),source_revision:hex,route_digest:hex}).strict().optional(),
  }).strict(),
]);
export type OwnerSkillRequestArgs = z.infer<typeof ownerSkillRequestArgsSchema>;
export const ownerSkillRequestReceiptSchema = z.object({
  version: z.literal(1), status: z.literal('committed'), action_id: hex, event_id: hex,
  payload_hash: hex, args_hash: hex, outcome: z.object({operation: z.literal('admit'),
    request_id: z.string().regex(/^[a-f0-9]{32}$/), skill: z.enum(['meeting-prep', 'ask-research', 'followup', 'relationship-pulse', 'proactive']), status: z.literal('queued')}).strict(),
}).strict();
export type OwnerSkillRequestReceipt = z.infer<typeof ownerSkillRequestReceiptSchema>;
export interface OwnerSkillRequest {
  actionId: string; eventId: string; payloadHash: string; text: string; admittedAtMs: number;
  providerThreadId: string; args: OwnerSkillRequestArgs;
}
const responseSchema = z.discriminatedUnion('status', [ownerSkillRequestReceiptSchema,
  z.object({version: z.literal(1), status: z.literal('not_found')}).strict(),
  z.object({version: z.literal(1), status: z.literal('rejected'), code: z.enum(['event_conflict', 'source_unavailable', 'meeting_notes_not_connected', 'needs_context', 'unavailable'])}).strict(),
]);
export function createOwnerSkillRequestClient(options: OwnerToolClientOptions) {
  const transport = createOwnerToolTransport(options, '/runtime/owner-skill-request');
  if (transport.identity.channel !== 'photon') throw new Error('Shared skill execution requires Photon');
  async function call(operation: 'admit' | 'inspect', input: OwnerSkillRequest, signal?: AbortSignal) {
    const args = ownerSkillRequestArgsSchema.parse(input.args);
    const binding = stableActionIdentity(input.eventId, ownerSkillRequestKind, args);
    if (!hex.safeParse(input.eventId).success || !hex.safeParse(input.payloadHash).success ||
        input.actionId !== binding.actionId || !Number.isSafeInteger(input.admittedAtMs) ||
        !input.providerThreadId || !input.text || input.text.length > 16000 ||
        /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(input.text))
      throw new Error('Skill request admission is invalid');
    const response = await transport.request({version: 1, operation, action_id: input.actionId,
      event_id: input.eventId, payload_hash: input.payloadHash,
      text_hash: createHash('sha256').update(input.text).digest('hex'), text: input.text,
      admitted_at_ms: input.admittedAtMs, provider_thread_id: input.providerThreadId, args}, signal);
    const receipt = responseSchema.parse(response.value);
    if ((response.status === 409) !== (receipt.status === 'rejected') ||
        (operation === 'admit' && receipt.status === 'not_found') ||
        (receipt.status === 'committed' && (receipt.action_id !== input.actionId || receipt.event_id !== input.eventId ||
          receipt.payload_hash !== input.payloadHash || receipt.args_hash !== binding.payloadHash ||
          receipt.outcome.skill !== args.skill)))
      throw new Error('Skill request receipt is invalid');
    return receipt;
  }
  return {identity: transport.identity, scopeHash: transport.scopeHash, call};
}
export type OwnerSkillRequestClient = ReturnType<typeof createOwnerSkillRequestClient>;

export const ownerSkillRequestRejectionSchema = z.enum(['event_conflict', 'source_unavailable', 'meeting_notes_not_connected', 'needs_context', 'unavailable']);
export type OwnerSkillRequestRejection = z.infer<typeof ownerSkillRequestRejectionSchema>;
export function skillRejectionAcknowledgement(skill: OwnerSkillRequestArgs['skill'], code: OwnerSkillRequestRejection): string {
  if (skill === 'followup' && code === 'meeting_notes_not_connected') return 'I can’t verify those promises because your meeting notes aren’t connected. You can connect Granola in setup when you’re ready.';
  if (code === 'source_unavailable') return 'I could not check the sources needed for that work, so I did not queue it. Please try again when those sources are available.';
  if (code === 'event_conflict') return 'This message conflicts with a saved request, so I did not queue new work. Please send one specific request in a new message.';
  if (code === 'unavailable') return 'I could not start that work right now. Nothing was queued. Please try again later.';
  if (skill === 'followup') return 'I could not queue a follow-up from this message. I cannot confirm that any loops were closed. Ask me to list what is currently open, or name one exact loop to correct.';
  return 'I could not start that work from this message. Nothing was queued. Please make the request more specific.';
}
export function skillAcknowledgement(receipt: OwnerSkillRequestReceipt) {
  if (receipt.outcome.skill === 'relationship-pulse') return 'Your relationship pulse is queued. It will arrive separately.';
  if (receipt.outcome.skill === 'followup') return 'Your follow-up is queued. It will arrive separately.';
  return receipt.outcome.skill === 'ask-research'
    ? 'Your research is queued. It will arrive separately.'
    : 'Your meeting prep is queued. It will arrive separately.';
}

/** Only the original affirmative attended trigger; saved receipt readers remain structural. */
export function isOwnerUrgentCheck(text: string): boolean {
  return /^(?:please\s+)?(?:check|(?:can|could|would)\s+you\s+(?:please\s+)?check)\s+for\s+anything\s+urgent(?:\s+please)?[.!?]*$/i.test(text.trim());
}
/** Calendar timing alone does not authorize the full meeting-prep pipeline. */
const PREP_COPY = /\b(?:blurb|(?:short|quick|brief)\s+summary|\d+(?:[-–]\d+)?\s+sentences?)\b/i;
const PREP_WHEN = String.raw`(?:today|tonight|tomorrow|this\s+(?:morning|afternoon|week))`;
const PREP_SWEEP = new RegExp(String.raw`\b(?:` + [
  // "prep (me for) my/all/tomorrow's/upcoming meetings|calls|calendar"
  String.raw`prep(?:are)?(?:\s+me)?\s+(?:for\s+)?(?:(?:my|the|all|upcoming|${PREP_WHEN}'s)\s+)+(?:meetings|calls|calendar)`,
  String.raw`prep(?:are)?(?:\s+me)?\s+for\s+${PREP_WHEN}`,
  // Bare "meeting prep" is a sweep only alone or scoped to a day, never "meeting prep for Dana".
  String.raw`meeting\s+prep(?=(?:\s+please)?[.!?]*$|\s+for\s+(?:(?:my|the|all)\s+meetings|${PREP_WHEN}))`,
  String.raw`who\s+am\s+i\s+meeting`, String.raw`who(?:'s|\s+is)\s+in\s+my\s+meetings`,
  String.raw`brief\s+me\s+on\s+my\s+calendar`,
  String.raw`get\s+me\s+ready\s+for\s+(?:${PREP_WHEN}|(?:my|${PREP_WHEN}'s)\s+meetings)`,
].join('|') + String.raw`)\b`, 'i');
const UNNAMED_PREP = /^(?:please )?(?:(?:can|could|would) you (?:please )?)?(?:(?:give|send|show) me )?(?:(?:full|meeting) )?prep(?:are)?(?: me)?(?: (?:please|pls|plz))?$/;
export function isOwnerMeetingPrepRequest(text: string, mode: 'sweep' | 'focus'): boolean {
  // iMessage autocorrects apostrophes; "tomorrow’s" must read like "tomorrow's".
  const request = text.trim().replace(/[‘’]/g, "'");
  if (!request || PREP_COPY.test(request) || /["“”]|(?:^|\s)'|\b(?:don\x27t|do\s+not|no|not|never|without|instead\s+of|stop|cancel)\b/i.test(request)) return false;
  if (mode === 'sweep') return /^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:prep(?:are)?|meeting\s+prep|who|brief\s+me|get\s+me\s+ready)\b/i.test(request) && PREP_SWEEP.test(request);
  // A remark about prep that already happened ("prep for Dana was great") is not a request.
  if (/^(?:the\s+|my\s+|that\s+|this\s+|your\s+)?prep(?:are)?(?:\s+for\s+[^.!?\n]*?)?\s+(?:is|was|went|looked|looks|seemed|felt)\s+(?:(?:really|very|so|super|pretty|all)\s+)?(?:done|great|good|fine|well|helpful|useful|perfect|ok|okay|amazing|excellent|finished|complete|ready|thorough)\b/i.test(request)) return false;
  return /^(?:please\s+)?(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?(?:(?:(?:give|send|show)\s+me\s+)?(?:(?:full|meeting)\s+)?prep(?:are)?\b|(?:help\s+me\s+)?get\s+me\s+ready\s+for\b|help\s+me\s+get\s+ready\s+for\b|what\s+should\s+i\s+know\s+before\s+my\s+(?:meeting|call)\b|(?:redo|update|refresh)\s+[^.!?\n]+\bprep\b|same\s+(?:person|meeting),?\s+(?:updated|full)\s+prep\b)/i.test(request) &&
    // Prep that names nobody ("full prep, please?") selects nobody; as a reply it can only accept an offer.
    !UNNAMED_PREP.test(request.toLowerCase().replace(/[^\p{L}\p{N}_\s]/gu, ' ').trim().split(/\s+/).join(' '));
}
export class ProactiveAdmissionRequiredError extends Error {
  constructor() { super('Proactive requires a current affirmative urgent-check request'); }
}

/** A previously loaded skill or chat history is not a new pulse invocation. */
export function isOwnerRelationshipPulseRequest(text: string): boolean {
  const value = text.trim().replace(/[’]/g, "'").replace(/\b(?:pls|plz)\b/gi, 'please');
  if (/^(?:please\s+)?(?:who\s+am\s+i\s+losing\s+touch\s+with|who\s+(?:haven't|have\s+not)\s+i\s+talked\s+to|who(?:'s|\s+is)\s+waiting\s+on\s+me)(?:,?\s+please)?[.!?]*$/i.test(value)) return true;
  return /^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:(?:show|give|send|run|get)(?:\s+me)?\s+)?(?:(?:a|my|the|quick|current|fresh)\s+)*(?:relationship[ -])?pulse(?:\s+again)?(?:,?\s+please)?[.!?]*$/i.test(value);
}
export class RelationshipPulseAdmissionRequiredError extends Error {
  constructor() { super('Relationship pulse requires a current affirmative pulse request'); }
}

/** Only the current owner's affirmative request may start follow-up work. */
export function isOwnerFollowupRequest(text: string): boolean {
  const value = text.trim().replace(/[’]/g, "'").replace(/\b(?:pls|plz)\b/gi, 'please');
  // Includes the follow-up skill's own documented triggers (followup/SKILL.md).
  return /^(?:please\s+)?(?:(?:can|could|would)\s+you\s+(?:please\s+)?)?(?:who\s+do\s+i\s+owe\s+(?:a\s+)?repl(?:y|ies)(?:\s+to)?|(?:(?:show|list|check|run|get|give|send)(?:\s+me)?\s+(?:(?:a|any|my|the|recent|backlog|open|post[ -]meeting)\s+)*follow[ -]?ups?|(?:(?:any|my|recent|backlog|open|post[ -]meeting)\s+)*follow[ -]?ups)(?:\s+(?:now|again))?|what\s+follow[ -]?ups\s+do\s+i\s+have|follow\s+up\s+on\s+(?:my|today's)\s+meetings|what\s+did\s+i\s+commit\s+to|what\s+did\s+i\s+promise\s+in\s+(?:yesterday's|today's|my|recent)\s+meetings|open\s+items\s+from\s+my\s+(?:granola\s+)?(?:notes|meetings)|anything\s+to\s+send\s+after\s+my\s+meetings)(?:,?\s+please)?[.!?]*$/i.test(value);
}
export class FollowupAdmissionRequiredError extends Error {
  constructor() { super('Follow-up requires a current affirmative follow-up request'); }
}
