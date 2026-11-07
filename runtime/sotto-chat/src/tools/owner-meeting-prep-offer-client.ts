import {createHash} from 'node:crypto';
import {z} from 'zod';
import {createOwnerToolTransport} from './owner-tool-transport.js';
import type {OwnerToolClientOptions} from './owner-contract.js';
import {stableActionIdentity} from './action-gate.js';
import {ownerMeetingPrepOfferArgsSchema, ownerMeetingPrepOfferAcceptedDeliverySchema,
  ownerMeetingPrepOfferInspectSchema, ownerMeetingPrepOfferKind, ownerMeetingPrepOfferMatches,
  ownerMeetingPrepOfferPreviewSchema, ownerMeetingPrepOfferReceiptSchema,
  type OwnerMeetingPrepOfferAcceptedDelivery, type OwnerMeetingPrepOfferArgs,
  type OwnerMeetingPrepOfferPreview, type OwnerMeetingPrepOfferReceipt} from './owner-meeting-prep-offer-contract.js';

export interface OwnerMeetingPrepOfferRequest {
  actionId: string; eventId: string; payloadHash: string; text: string; admittedAtMs: number;
  providerThreadId: string; sourceRevision: string; args: OwnerMeetingPrepOfferArgs;
}

const responseSchema = z.union([ownerMeetingPrepOfferInspectSchema,
  z.object({version:z.literal(1),status:z.literal('rejected'),code:z.enum(['event_conflict','source_unavailable','invalid_target','unavailable'])}).strict()]);
export type OwnerMeetingPrepOfferRejected = {version:1;status:'rejected';code:'event_conflict'|'source_unavailable'|'invalid_target'|'unavailable'};

export function createOwnerMeetingPrepOfferClient(options: OwnerToolClientOptions) {
  const transport = createOwnerToolTransport(options, '/runtime/owner-meeting-prep-request');
  if (transport.identity.channel !== 'photon') throw new Error('Meeting prep offers require Photon');
  const validateRequest = (request: OwnerMeetingPrepOfferRequest) => {
    const args = ownerMeetingPrepOfferArgsSchema.parse(request.args);
    if (!/^[a-f0-9]{64}$/.test(request.eventId) || !/^[a-f0-9]{64}$/.test(request.payloadHash) ||
        !/^[a-f0-9]{64}$/.test(request.sourceRevision) || args.source_revision !== request.sourceRevision ||
        !Number.isSafeInteger(request.admittedAtMs) || request.admittedAtMs < 1 ||
        !request.providerThreadId || !request.text || request.text.length > 16000 ||
        /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(request.text))
      throw new Error('Meeting prep offer request is invalid');
    const binding = stableActionIdentity(request.eventId, ownerMeetingPrepOfferKind, args);
    if (binding.actionId !== request.actionId) throw new Error('Meeting prep offer request is invalid');
    return {args, binding};
  };
  async function call(operation: 'preview'|'inspect'|'record_offer', request: OwnerMeetingPrepOfferRequest,
                      signal?: AbortSignal, acceptedDelivery?: OwnerMeetingPrepOfferAcceptedDelivery) {
    const {args,binding} = validateRequest(request);
    if (operation === 'record_offer' && !acceptedDelivery) throw new Error('Accepted offer receipt is required');
    if (operation === 'preview' && acceptedDelivery) throw new Error('Unexpected accepted offer receipt');
    if (acceptedDelivery) {
      const delivery = ownerMeetingPrepOfferAcceptedDeliverySchema.parse(acceptedDelivery);
      if (delivery.source_revision !== request.sourceRevision) throw new Error('Accepted offer source revision changed');
    }
    const body = {version:1, operation, action_id:request.actionId, event_id:request.eventId,
      payload_hash:request.payloadHash, text_hash:createHash('sha256').update(request.text).digest('hex'),
      text:request.text, admitted_at_ms:request.admittedAtMs, provider_thread_id:request.providerThreadId,
      args, ...(operation==='record_offer'&&acceptedDelivery
        ? {accepted_delivery:ownerMeetingPrepOfferAcceptedDeliverySchema.parse(acceptedDelivery)} : {})};
    const response = await transport.request(body, signal, {responseLimit: 64 * 1024});
    const result = responseSchema.parse(response.value);
    if ((response.status === 409) !== (result.status === 'rejected')) throw new Error('Meeting prep offer status is invalid');
    if (result.status === 'preview') {
      if (operation === 'record_offer' || result.action_id !== request.actionId || result.event_id !== request.eventId ||
          result.payload_hash !== request.payloadHash || result.args_hash !== binding.payloadHash ||
          !ownerMeetingPrepOfferMatches(result.review,args,request.sourceRevision))
        throw new Error('Meeting prep offer preview is invalid');
    } else if (result.status === 'recorded') {
      if ((operation !== 'record_offer' && operation !== 'inspect') || !acceptedDelivery ||
          result.action_id !== request.actionId || result.event_id !== request.eventId ||
          result.payload_hash !== request.payloadHash || result.args_hash !== binding.payloadHash ||
          !acceptedDelivery || result.review_hash !== acceptedDelivery.review_hash ||
          result.accepted_dispatch_id !== acceptedDelivery.accepted_dispatch_id ||
          result.provider_message_id !== acceptedDelivery.provider_message_id ||
          result.accepted_at_ms !== acceptedDelivery.accepted_at_ms)
        throw new Error('Meeting prep offer receipt is invalid');
    } else if (result.status === 'unknown' && operation !== 'inspect') {
      throw new Error('Meeting prep offer result is uncertain');
    } else if (result.status === 'not_found' && operation === 'preview') {
      throw new Error('Meeting prep offer preview is unavailable');
    }
    return result;
  }
  return {
    identity: transport.identity,
    scopeHash: transport.scopeHash,
    call: (operation:'preview'|'inspect', request:OwnerMeetingPrepOfferRequest, signal?:AbortSignal,
      acceptedDelivery?:OwnerMeetingPrepOfferAcceptedDelivery) => call(operation,request,signal,acceptedDelivery),
    recordOffer: async (request:OwnerMeetingPrepOfferRequest, acceptedDelivery:OwnerMeetingPrepOfferAcceptedDelivery,
                       signal?:AbortSignal): Promise<OwnerMeetingPrepOfferReceipt|OwnerMeetingPrepOfferRejected> => {
      const result = await call('record_offer',request,signal,acceptedDelivery);
      if (result.status === 'rejected') return result;
      if (result.status !== 'recorded') throw new Error('Meeting prep offer activation unavailable');
      return ownerMeetingPrepOfferReceiptSchema.parse(result);
    },
  };
}
export type OwnerMeetingPrepOfferClient = ReturnType<typeof createOwnerMeetingPrepOfferClient>;
