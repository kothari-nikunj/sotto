import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { OwnerToolClientOptions } from './owner-contract.js';
import { createOwnerToolTransport } from './owner-tool-transport.js';
import { stableActionIdentity } from './action-gate.js';
import { ownerPreferenceToolArgsSchema, ownerPreferenceToolKind, ownerPreferenceToolReadSchema, ownerPreferenceToolReceiptSchema,
  ownerPreferenceToolRejectionSchema, usefulnessListArgsSchema,usefulnessListSchema, procedurePreviewSchema, validProcedurePreview, standingPreviewMatches, procedureCapacitySchema, preferenceReceiptMatches, type OwnerPreferenceToolArgs } from './owner-preference-tool-contract.js';
const schema = z.union([usefulnessListSchema,ownerPreferenceToolReadSchema, ownerPreferenceToolReceiptSchema,procedurePreviewSchema,
 z.object({version:z.literal(1),status:z.literal('rejected'),code:z.literal('master_capacity'),capacity:procedureCapacitySchema}).strict(),
  z.object({version: z.literal(1), status: z.literal('match'), offer_id: z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/), kind: z.literal('procedure')}).strict(),
  z.object({version: z.literal(1), status: z.literal('no_match')}).strict(),
  z.object({version: z.literal(1), status: z.literal('unknown')}).strict(),
  z.object({version: z.literal(1), status: z.literal('not_found')}).strict(),
  z.object({version: z.literal(1), status: z.literal('rejected'), code: ownerPreferenceToolRejectionSchema}).strict()]);
export interface OwnerPreferenceToolRequest {
  actionId: string; eventId: string; payloadHash: string; text: string; admittedAtMs: number;
  providerThreadId: string; args: OwnerPreferenceToolArgs | null;
}
export function createOwnerPreferenceToolClient(options: OwnerToolClientOptions) {
  const transport = createOwnerToolTransport(options, '/runtime/owner-preference-tool');
  async function call(operation: 'read' | 'apply' | 'inspect' | 'preview', input: OwnerPreferenceToolRequest | (Omit<OwnerPreferenceToolRequest,'args'> & {args:z.infer<typeof usefulnessListArgsSchema>}), signal?: AbortSignal) {
    if (!/^[a-f0-9]{64}$/.test(input.eventId) || !/^[a-f0-9]{64}$/.test(input.payloadHash) || !Number.isSafeInteger(input.admittedAtMs) ||
        !input.providerThreadId || !input.text || input.text.length > 16000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(input.text) ||
        (operation === 'read' ? input.args !== null && !usefulnessListArgsSchema.safeParse(input.args).success : !ownerPreferenceToolArgsSchema.safeParse(input.args).success)) throw new Error('Owner preference request is invalid');
    if (operation === 'preview' && !['accept-procedure-offer','propose-procedure','propose-priorities','propose-context'].includes(input.args?.operation??'')) throw new Error('Invalid standing-rule preview');
    const binding = stableActionIdentity(input.eventId, ownerPreferenceToolKind, input.args);
    if (binding.actionId !== input.actionId) throw new Error('Owner preference request is invalid');
    const response = await transport.request({version: 1, operation, action_id: input.actionId, event_id: input.eventId,
      payload_hash: input.payloadHash, text_hash: createHash('sha256').update(input.text).digest('hex'), text: input.text,
      admitted_at_ms: input.admittedAtMs, provider_thread_id: input.providerThreadId, args: input.args}, signal, {responseLimit: operation === 'read' ? input.args?.operation==='usefulness-list'?1024*1024:512 * 1024 : ['propose-procedure','append-procedure','propose-priorities','replace-priorities','propose-context','append-context','prioritize'].includes(input.args?.operation??'')?64*1024:8192});
    const result = schema.parse(response.value);
    if ((operation === 'preview' ? !(['propose-procedure','propose-priorities','propose-context'].includes(input.args?.operation??'')?['preview','rejected']:['match','no_match']).includes(result.status) : ['match','no_match','preview'].includes(result.status)) ||
        (response.status === 409) !== (result.status === 'rejected') ||
        (operation === 'read' ? result.status !== (input.args?.operation==='usefulness-list'?'usefulness_list':'read') : ['read','usefulness_list'].includes(result.status)) ||
        (operation === 'apply' && result.status === 'not_found') ||
        ((result.status === 'read'||result.status==='usefulness_list') && (result.event_id !== input.eventId || result.payload_hash !== input.payloadHash)) ||
        (result.status === 'committed' && (result.action_id !== input.actionId || result.event_id !== input.eventId ||
          result.payload_hash !== input.payloadHash || result.args_hash !== binding.payloadHash || !input.args || input.args.operation==='usefulness-list' || !preferenceReceiptMatches(result, input.args))))
      throw new Error('Owner preference receipt is invalid');
    if(result.status==='usefulness_list'&&(input.args?.operation!=='usefulness-list'||result.source_revision!==input.args.source_revision))throw new Error('Usefulness list source proof invalid');
    if(result.status==='preview'&&(!input.args||input.args.operation==='usefulness-list'||result.event_id!==input.eventId||result.payload_hash!==input.payloadHash||!standingPreviewMatches(result,input.args)))throw new Error('Standing-rule preview is invalid');
    if(result.status==='rejected'&&'capacity' in result&&!['propose-procedure','append-procedure','propose-priorities','replace-priorities','propose-context','append-context','prioritize'].includes(input.args?.operation??''))throw new Error('Owner preference receipt is invalid');
    return result;
  }
  return {identity: transport.identity, scopeHash: transport.scopeHash, call};
}
export type OwnerPreferenceToolClient = ReturnType<typeof createOwnerPreferenceToolClient>;
