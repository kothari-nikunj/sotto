import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { OwnerToolClientOptions } from './owner-contract.js';
import { createOwnerToolTransport } from './owner-tool-transport.js';
import { stableActionIdentity } from './action-gate.js';
import { gmailAcceptedDeliverySchema,type GmailAcceptedDelivery,gmailDraftArgsSchema, ownerGmailDraftKind, gmailDraftReceiptSchema, gmailDraftRejectionSchema, gmailDraftReceiptMatches, gmailDraftPreviewSchema, validGmailDraftReview, gmailDraftPreviewMatches, type GmailDraftArgs } from './owner-gmail-draft-contract.js';
const schema = z.union([z.object({version:z.literal(1),status:z.literal('offers'),offers:z.array(z.object({offer_id:z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/),to_name:z.string().max(1024).refine(v=>Array.from(v).length<=512).nullable(),to_email:z.string().max(254).nullable(),channel:z.enum(['email','imessage','whatsapp']).nullable(),subject:z.string().max(1024).refine(v=>Array.from(v).length<=512)}).strict())}).strict(),z.object({version:z.literal(1),status:z.literal('no_match')}).strict(), gmailDraftPreviewSchema, gmailDraftReceiptSchema,
  z.object({ version: z.literal(1), status: z.literal('not_found') }).strict(),
  z.object({ version: z.literal(1), status: z.literal('unknown') }).strict(),
  z.object({ version: z.literal(1), status: z.literal('rejected'), code: gmailDraftRejectionSchema }).strict()]);
export interface OwnerGmailDraftRequest {
  actionId: string;
  eventId: string;
  payloadHash: string;
  text: string;
  admittedAtMs: number;
  providerThreadId: string;
  args: GmailDraftArgs;
}
export function createOwnerGmailDraftClient(options: OwnerToolClientOptions) {
  const transport = createOwnerToolTransport(options, '/runtime/owner-gmail-draft-tool');
  async function call(operation: 'preview' | 'apply' | 'inspect' | 'record_offer', input: OwnerGmailDraftRequest, signal?: AbortSignal, acceptedDelivery?:GmailAcceptedDelivery) {
    if (!/^[a-f0-9]{64}$/.test(input.eventId) || !/^[a-f0-9]{64}$/.test(input.payloadHash) || !Number.isSafeInteger(input.admittedAtMs) ||
      !input.providerThreadId || !input.text || input.text.length > 16000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(input.text) ||
      !gmailDraftArgsSchema.safeParse(input.args).success)
      throw new Error('Gmail draft request is invalid');
    const binding = stableActionIdentity(input.eventId, ownerGmailDraftKind, input.args);
    if (binding.actionId !== input.actionId)
      throw new Error('Gmail draft request is invalid');
    if(operation==='record_offer'&&(input.args.operation!=='propose_pair'||!acceptedDelivery))throw new Error('Invalid offered pair recording');
    const body={ version: 1, operation, action_id: input.actionId, event_id: input.eventId,
      payload_hash: input.payloadHash, text_hash: createHash('sha256').update(input.text).digest('hex'), text: input.text,
      admitted_at_ms: input.admittedAtMs, provider_thread_id: input.providerThreadId, args: input.args,
      ...(acceptedDelivery?{accepted_delivery:gmailAcceptedDeliverySchema.parse(acceptedDelivery)}:{})};
    if(input.args.operation==='propose_pair'&&Buffer.byteLength(JSON.stringify({...body,identity:transport.identity}))>65536)throw new Error('Gmail pair request exceeds capacity');
    const response = await transport.request(body, signal, {responseLimit: operation === 'preview'||operation==='record_offer'||operation==='inspect'&&['propose_typed','propose_send_typed','propose_pair'].includes(input.args.operation) ? 128 * 1024 : 8192});
    const result = schema.parse(response.value);
    if ((response.status === 409) !== (result.status === 'rejected') ||
      (operation === 'preview' ? !['preview', 'rejected', 'no_match', 'offers'].includes(result.status) : result.status === 'preview' && !['propose_typed','propose_send_typed','propose_pair'].includes(input.args.operation)) ||
      (operation === 'apply' && result.status === 'not_found') ||
      (result.status === 'preview' && 'event_id' in result && (result.event_id !== input.eventId || result.payload_hash !== input.payloadHash)) ||
      (result.status === 'committed' && (result.action_id !== input.actionId || result.event_id !== input.eventId ||
        result.payload_hash !== input.payloadHash || result.args_hash !== binding.payloadHash || !input.args || !gmailDraftReceiptMatches(result, input.args))))
      throw new Error('Gmail draft receipt is invalid');
    if (result.status === 'preview' && 'draft_origin' in result.review && result.review.draft_origin && (input.args.operation==='propose_typed'||input.args.operation==='propose_send_typed') && !input.args.baseline && result.review.draft_origin.baseline_action_id!==input.actionId)throw new Error('Typed root action binding invalid');
    if (result.status === 'preview' && !gmailDraftPreviewMatches(result,input.args))
      throw new Error('Gmail draft review is invalid');
    if (result.status === 'no_match' && (operation !== 'preview' || !['propose_offer','propose_followup','list_followup'].includes(input.args.operation))) throw new Error('Gmail draft no-match response is invalid');
    if (result.status === 'offers' && (operation !== 'preview' || input.args.operation !== 'list_followup')) throw new Error('Follow-up draft list is invalid');
    if(result.status==='preview'&&result.offer_receipt){
      const r=result.review,receipt=result.offer_receipt;
      if(!('kind' in r)||r.kind!=='reply_pair'||receipt.action_id!==input.actionId||receipt.event_id!==input.eventId||receipt.payload_hash!==input.payloadHash||receipt.args_hash!==binding.payloadHash||receipt.review_hash!==r.review_hash||receipt.reply_key!==r.reply_key||receipt.decline_key!==r.decline_key||(acceptedDelivery&&receipt.accepted_dispatch_id!==acceptedDelivery.accepted_dispatch_id))throw new Error('Invalid offered pair receipt');
    }
    if(operation==='record_offer'&&result.status==='preview'&&!result.offer_receipt)throw new Error('Offered pair recording incomplete');
    return result;
  }
  return { identity: transport.identity, scopeHash: transport.scopeHash, call: (operation:'preview'|'apply'|'inspect',input:OwnerGmailDraftRequest,signal?:AbortSignal)=>call(operation,input,signal), recordOffer:async(input:OwnerGmailDraftRequest,acceptedDelivery:GmailAcceptedDelivery,signal?:AbortSignal)=>{const result=await call('record_offer',input,signal,acceptedDelivery);if(result.status!=='preview')throw new Error('Offered recording unavailable');return result;}, preflightPair:(input:OwnerGmailDraftRequest)=>{const body={version:1,operation:'preview',action_id:input.actionId,event_id:input.eventId,payload_hash:input.payloadHash,text_hash:createHash('sha256').update(input.text).digest('hex'),text:input.text,admitted_at_ms:input.admittedAtMs,provider_thread_id:input.providerThreadId,args:input.args,identity:transport.identity};const record={...body,operation:'record_offer',accepted_delivery:{accepted_dispatch_id:'\u0000'.repeat(160),provider_message_id:'\u0000'.repeat(512),source_revision:'a'.repeat(64),review_hash:'a'.repeat(64),payload_hash:'a'.repeat(64)}};if(Buffer.byteLength(JSON.stringify(body))>65536||Buffer.byteLength(JSON.stringify(record))>65536)throw new Error('Gmail pair request exceeds capacity');} };
}
export type OwnerGmailDraftClient = ReturnType<typeof createOwnerGmailDraftClient>;
