import {createHash} from 'node:crypto';
import type {OperationalJournal} from './journal/index.js';
import type {Inbound} from './channels/types.js';
import type {ModelRequest} from './conversation.js';
import {READ_CONTEXT_WINDOW_MS} from './tools/read-admission.js';

/** A provider quote selects context only, never an unthreaded action selector. */
export function freezeQuotedBackground(journal:OperationalJournal, inbound:Inbound, routeDigest:string, providerSentAtMs:number):string {
  const ineligible = JSON.stringify({version:1,eligible:false});
  if (!inbound.replyToMessageId || inbound.media?.length) return ineligible;
  const matches = journal.db.prepare("SELECT id,kind,provider_message_ids,provider_sent_at_ms FROM dispatches WHERE route_digest=? AND status='accepted' AND provider_message_id=?")
    .all(routeDigest, inbound.replyToMessageId);
  if (matches.length !== 1) return ineligible;
  const selected = matches[0]!;
  const ids = JSON.parse(String(selected.provider_message_ids ?? '[]'));
  if (selected.kind !== 'scheduled_text' || ids.length !== 1 || ids[0] !== inbound.replyToMessageId || !Number.isSafeInteger(selected.provider_sent_at_ms) ||
      Number(selected.provider_sent_at_ms) >= providerSentAtMs || journal.hasUnresolvedDispatchAttempt(String(selected.id))) return ineligible;
  return JSON.stringify({version:1,eligible:false,quoted_context:true,dispatch_id:selected.id,
    provider_message_id:inbound.replyToMessageId,route_digest:routeDigest,
    inbound_provider_sent_at_ms:providerSentAtMs,provider_sent_at_ms:selected.provider_sent_at_ms});
}

export function acceptedBackgroundMessage(journal:OperationalJournal, eventId:string, sessionStartedAt:number):
  {dispatchId:string;providerMessageId:string;text:string;sourceRevision:string;acceptedAt:number;routeDigest:string}|undefined {
  const event = journal.getInbound(eventId);
  if (!event?.continuation_json || !Number.isFinite(sessionStartedAt)) return undefined;
  const frozen = JSON.parse(String(event.continuation_json));
  const inbound = JSON.parse(String(event.payload)).inbound as Inbound;
  const selected = inbound.replyBound && frozen.quoted_context === true &&
      inbound.replyToMessageId === frozen.provider_message_id && !inbound.media?.length
    ? {dispatch_id:frozen.dispatch_id,message_id:frozen.provider_message_id}
    : journal.continuationArgs(eventId)?.selected_offer;
  if (!selected) return undefined;
  const row = journal.getDispatch(selected.dispatch_id);
  if (!row || row.kind !== 'scheduled_text' || row.status !== 'accepted' ||
      typeof row.source_revision !== 'string' || !/^[a-f0-9]{64}$/.test(row.source_revision) ||
      Number(row.created_at) <= sessionStartedAt || Number(event.received_at) - Number(row.updated_at) > READ_CONTEXT_WINDOW_MS ||
      Number(row.updated_at) > Number(event.received_at) || row.provider_message_id !== selected.message_id ||
      journal.hasUnresolvedDispatchAttempt(String(row.id))) return undefined;
  const ids = JSON.parse(String(row.provider_message_ids ?? '[]'));
  if (row.route_digest !== frozen.route_digest || ids.length !== 1 || ids[0] !== selected.message_id ||
      createHash('sha256').update(String(row.payload)).digest('hex') !== row.payload_hash) return undefined;
  const body = JSON.parse(String(row.payload));
  if (typeof body.text !== 'string' || !body.text || body.text.length > 16000) return undefined;
  return {dispatchId:String(row.id),providerMessageId:selected.message_id,text:body.text,sourceRevision:row.source_revision,acceptedAt:Number(row.updated_at),routeDigest:String(row.route_digest)};
}

/** Only the model boundary may see scheduled text as a quote. Journal action lookups stay exact. */
export function projectConversationContext(journal:OperationalJournal, request:ModelRequest, sessionStartedAt:number, hasProjection:boolean, currentRouteDigest:string):
    Pick<ModelRequest,'acceptedBackgroundContext'|'acceptedEditContext'|'quotedReply'> {
  const quotedBackground = request.messages.length === 1 && request.messages[0]?.replyBound &&
      request.quotedReply?.status === 'unavailable'
    ? journal.acceptedBackgroundMessage(request.ownerRequestKey, sessionStartedAt) : undefined;
  const quotedReply = quotedBackground && quotedBackground.routeDigest === currentRouteDigest
    ? {status:'found' as const,messageId:request.messages[0]!.replyToMessageId!,
      text:quotedBackground.text,sourceRevision:quotedBackground.sourceRevision} : request.quotedReply;
  return {
    acceptedBackgroundContext: !hasProjection && request.messages.length === 1 && !request.quotedReply
      ? journal.acceptedBackgroundContext(request.ownerRequestKey, sessionStartedAt) : undefined,
    acceptedEditContext: request.messages.length === 1
      ? journal.acceptedEditContext(request.ownerRequestKey, sessionStartedAt) : undefined,
    quotedReply,
  };
}
