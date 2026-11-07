import { createHash } from 'node:crypto';
import { z } from 'zod';
import {ownerMeetingPrepArgsSchema} from './owner-meeting-prep-client.js';

export const ownerGmailDraftKind = 'owner_gmail_draft_tool';
const hex = z.string().regex(/^[a-f0-9]{64}$/);
const header = (max: number) => z.string().min(1).max(max).refine(value => !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
export const gmailDraftProposalSchema = z.object({
  operation: z.literal('propose'), message_id: z.string().regex(/^[A-Za-z0-9_-]{1,256}$/),
  body: z.string().min(1).max(16000).refine(value => value.trim().length > 0 && Array.from(value).length <= 8000 && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(value)),
}).strict();
export const gmailDraftOriginSchema = z.object({action_type:z.enum(['reply','decline']),baseline_action_id:hex,baseline_key:z.string().regex(/^[a-f0-9]{16}$/),baseline_body:gmailDraftProposalSchema.shape.body}).strict();
const replyReviewSchema = z.object({
  message_id: z.string().regex(/^[A-Za-z0-9_-]{1,256}$/), thread_id: z.string().regex(/^[A-Za-z0-9_-]{1,256}$/), to: header(320), subject: header(2048),
  body: gmailDraftProposalSchema.shape.body, draft_origin:gmailDraftOriginSchema.optional(), reply_message_id: header(998), source_revision: hex, review_hash: hex,
}).strict();
const sendHeader = (max:number, empty=false) => z.string().max(max*2).refine(value =>
  (empty || value.length>0) && Array.from(value).length<=max && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value));
const sendAddress = sendHeader(320).refine(value=>/^[^\s@,<>]+@[^\s@,<>]+$/.test(value));
const sendReviewSchema = replyReviewSchema.extend({
  kind:z.literal('reply_send'), sender_email:sendAddress, to:sendAddress,
  reply_message_id:sendHeader(512), subject:sendHeader(512,true), references:sendHeader(2048,true),
}).strict();
const draftReviewSchema = z.union([replyReviewSchema, z.object({kind:z.literal('new_email'),to:header(320),subject:z.string().max(2048).refine(value=>!/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value)),body:gmailDraftProposalSchema.shape.body,source_revision:hex,review_hash:hex}).strict()]);
const freshHeader=(max:number,empty=false)=>sendHeader(max,empty).refine(v=>!/[\u0080-\u009f\u2028\u2029]/u.test(v));
const freshAddress=freshHeader(254).refine(v=>/^[^\s@,;<>]+@[^\s@,;<>]+$/.test(v));
export const gmailNewSendProposalSchema=z.object({operation:z.literal('propose_new_send'),to:freshAddress,subject:freshHeader(512,true),body:gmailDraftProposalSchema.shape.body}).strict();
const newSendReviewSchema=gmailNewSendProposalSchema.omit({operation:true}).extend({kind:z.literal('new_send'),sender_email:sendAddress,source_revision:hex,review_hash:hex}).strict();
export const gmailDraftPairProposalSchema=gmailDraftProposalSchema.omit({body:true}).extend({operation:z.literal('propose_pair'),accept_body:gmailDraftProposalSchema.shape.body,decline_body:gmailDraftProposalSchema.shape.body,first:z.enum(['reply','decline']).optional()}).strict();
export const gmailDraftPairReviewSchema=replyReviewSchema.omit({body:true,draft_origin:true}).extend({kind:z.literal('reply_pair'),accept_body:gmailDraftProposalSchema.shape.body,decline_body:gmailDraftProposalSchema.shape.body,first:z.enum(['reply','decline']),reply_key:z.string().regex(/^[a-f0-9]{16}$/),decline_key:z.string().regex(/^[a-f0-9]{16}$/)}).strict();
export const gmailOfferedReceiptSchema=z.object({version:z.literal(1),action_id:hex,event_id:hex,payload_hash:hex,args_hash:hex,review_hash:hex,accepted_dispatch_id:z.string().min(1).max(160),reply_key:z.string().regex(/^[a-f0-9]{16}$/),decline_key:z.string().regex(/^[a-f0-9]{16}$/)}).strict();
export const gmailAcceptedDeliverySchema=z.object({accepted_dispatch_id:z.string().min(1).max(160),provider_message_id:z.string().min(1).max(512),source_revision:hex,review_hash:hex,payload_hash:hex}).strict();
export type GmailAcceptedDelivery=z.infer<typeof gmailAcceptedDeliverySchema>;
export const gmailDraftReviewSchema = z.union([draftReviewSchema,sendReviewSchema,newSendReviewSchema,gmailDraftPairReviewSchema]);
export const gmailSendProposalSchema = gmailDraftProposalSchema.extend({operation:z.literal('propose_send')});
const proposalProof = z.object({event_id:hex,action_id:hex,args_hash:hex,accepted_dispatch_id:z.string().min(1).max(160),source_revision:hex}).strict();
export const gmailTypedProposalSchema = z.object({operation:z.enum(['propose_typed','propose_send_typed']),message_id:gmailDraftProposalSchema.shape.message_id,body:gmailDraftProposalSchema.shape.body,action_type:z.enum(['reply','decline']),baseline:proposalProof.optional()}).strict();
const offerId = z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/);
export const gmailDraftOfferProposalSchema = z.object({operation:z.literal('propose_offer'), selection:ownerMeetingPrepArgsSchema}).strict();
const replyOfferOrigin = z.object({selection:ownerMeetingPrepArgsSchema,offer_id:z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/)}).strict();
const offerOrigin = z.union([replyOfferOrigin,z.object({kind:z.literal('followup'),offer_id:offerId,to:header(254).optional()}).strict()]);
export const gmailDraftArgsSchema = z.union([gmailDraftPairProposalSchema,gmailDraftProposalSchema, gmailSendProposalSchema, gmailNewSendProposalSchema, gmailTypedProposalSchema, z.object({operation:z.literal('send'),review:z.union([sendReviewSchema,newSendReviewSchema]),proposal:proposalProof}).strict(), gmailDraftOfferProposalSchema, z.object({operation:z.literal('list_followup')}).strict(), z.object({operation:z.literal('propose_followup'),offer_id:offerId,to:header(254).optional()}).strict(), z.object({
  operation: z.literal('save'), review: draftReviewSchema,
  proposal: z.object({ event_id: hex, action_id: hex, args_hash: hex,
    accepted_dispatch_id: z.string().min(1).max(160), source_revision: hex, origin:offerOrigin.optional() }).strict(),
}).strict()]);
export type GmailDraftArgs = z.infer<typeof gmailDraftArgsSchema>;
export const gmailDraftPreviewSchema = z.object({ version: z.literal(1), status: z.literal('preview'),
  event_id: hex, payload_hash: hex, review: gmailDraftReviewSchema, offer_id:offerId.optional(),offer_receipt:gmailOfferedReceiptSchema.optional() }).strict();
export type GmailDraftPreview = z.infer<typeof gmailDraftPreviewSchema>;
export const gmailDraftReceiptSchema = z.object({ version: z.literal(1), status: z.literal('committed'),
  action_id: hex, event_id: hex, payload_hash: hex, args_hash: hex, learning:z.enum(['recorded','pending']).optional(),
  outcome: z.union([z.object({operation:z.literal('send'),message_id:z.string().regex(/^[A-Za-z0-9_-]{1,256}$/),thread_id:z.string().regex(/^[A-Za-z0-9_-]{1,256}$/),review_hash:hex,status:z.literal('sent')}).strict(), z.object({ operation: z.literal('save'), draft_id: header(256), thread_id: z.string().regex(/^[A-Za-z0-9_-]{1,256}$/),
    review_hash: hex, status: z.literal('draft_saved') }).strict()]),
}).strict();
export type GmailDraftReceipt = z.infer<typeof gmailDraftReceiptSchema>;
export const gmailDraftRejectionSchema = z.enum(['authority_changed', 'draft_permission_unavailable', 'send_permission_unavailable', 'target_changed', 'review_unavailable', 'event_conflict', 'invalid_target']);
export type GmailDraftRejection = z.infer<typeof gmailDraftRejectionSchema>;
export function gmailDraftReviewHash(review: z.infer<typeof gmailDraftReviewSchema>): string {
  if('kind' in review&&review.kind==='reply_pair'){const {review_hash,...fields}=review;return createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.entries(fields).sort(([a],[b])=>a<b?-1:a>b?1:0)))).digest('hex');}
  if('kind' in review&&review.kind==='new_send')return createHash('sha256').update(JSON.stringify({body:review.body,kind:review.kind,sender_email:review.sender_email,source_revision:review.source_revision,subject:review.subject,to:review.to})).digest('hex');
  if ('draft_origin' in review && review.draft_origin) {
    const fields={body:review.body,draft_origin:review.draft_origin,...('kind' in review?{kind:review.kind,references:review.references,sender_email:review.sender_email}:{}),message_id:review.message_id,reply_message_id:review.reply_message_id,source_revision:review.source_revision,subject:review.subject,thread_id:review.thread_id,to:review.to};
    const sorted=Object.fromEntries(Object.entries(fields).sort(([a],[b])=>a<b?-1:a>b?1:0));
    sorted.draft_origin={action_type:review.draft_origin.action_type,baseline_action_id:review.draft_origin.baseline_action_id,baseline_body:review.draft_origin.baseline_body,baseline_key:review.draft_origin.baseline_key};
    return createHash('sha256').update(JSON.stringify(sorted)).digest('hex');
  }
  if ('kind' in review && review.kind==='reply_send') {
    return createHash('sha256').update(JSON.stringify({
      body:review.body, kind:review.kind, message_id:review.message_id, references:review.references,
      reply_message_id:review.reply_message_id, sender_email:review.sender_email,
      source_revision:review.source_revision, subject:review.subject, thread_id:review.thread_id, to:review.to,
    })).digest('hex');
  }
  if ('kind' in review) return createHash('sha256').update(JSON.stringify({body:review.body,kind:review.kind,source_revision:review.source_revision,subject:review.subject,to:review.to})).digest('hex');
  return createHash('sha256').update(JSON.stringify({ body: review.body, message_id: review.message_id,
    reply_message_id: review.reply_message_id, source_revision: review.source_revision, subject: review.subject,
    thread_id: review.thread_id, to: review.to })).digest('hex');
}
export function gmailDraftReviewText(preview: GmailDraftPreview): string {
  const r = preview.review;
  if('kind' in r&&r.kind==='reply_pair'){const first=r.first==='reply'?['Accept',r.accept_body]:['Decline',r.decline_body];const second=r.first==='reply'?['Decline',r.decline_body]:['Accept',r.accept_body];return `To: ${r.to}\nSubject: ${r.subject}\n\n${first[0]}:\n${first[1]}\n\n${second[0]}:\n${second[1]}\n\nWhich would you like to use? Nothing has been saved or sent.`;}
  const label='draft_origin' in r&&r.draft_origin?`Draft type: ${r.draft_origin.action_type==='decline'?'Decline':'Reply'}\n${r.body!==r.draft_origin.baseline_body?'Edited draft\n':''}`:'';
  if('kind' in r&&r.kind==='new_send')return `From: ${r.sender_email}\nTo: ${r.to}\nSubject: ${r.subject}\n\n${r.body}\n\nSend this email now?`;
  if ('kind' in r && r.kind==='reply_send') return label+`From: ${r.sender_email}\nTo: ${r.to}\nSubject: ${r.subject}\n\n${r.body}\n\nSend this email reply now?`;
  return label+`To: ${r.to}\nSubject: ${r.subject}\n\n${r.body}\n\nSave as a Gmail draft? Nothing will be sent.`;
}
/** The full review must fit one accepted reply; approval can never refer to truncated content. */
export function validGmailDraftReview(preview: GmailDraftPreview): boolean {
  return preview.review.review_hash === gmailDraftReviewHash(preview.review) && gmailDraftReviewText(preview).length <= ('kind' in preview.review&&preview.review.kind==='reply_pair'?16000:4000) && (preview.offer_receipt===undefined||'kind' in preview.review&&preview.review.kind==='reply_pair');
}
export function gmailDraftReceiptMatches(receipt: GmailDraftReceipt, args: GmailDraftArgs): boolean {
  const typed='review' in args&&'draft_origin' in args.review&&Boolean(args.review.draft_origin);
  if((receipt.learning!==undefined)!==typed)return false;
  if(args.operation==='send') return receipt.outcome.operation==='send' && ('kind' in args.review&&args.review.kind==='new_send'||receipt.outcome.thread_id===args.review.thread_id) && receipt.outcome.review_hash===args.review.review_hash;
  return receipt.outcome.operation==='save' && args.operation === 'save' && ('kind' in args.review || receipt.outcome.thread_id === args.review.thread_id) && receipt.outcome.review_hash === args.review.review_hash;
}

export function gmailDraftPreviewMatches(preview: GmailDraftPreview, args: GmailDraftArgs): boolean {
  if(args.operation==='propose_pair'){const r=preview.review;return validGmailDraftReview(preview)&&'kind' in r&&r.kind==='reply_pair'&&!preview.offer_id&&r.message_id===args.message_id&&r.accept_body===args.accept_body&&r.decline_body===args.decline_body&&r.first===(args.first??'reply');}
  if('kind' in preview.review&&preview.review.kind==='reply_pair')return false;
  if(args.operation==='propose_new_send')return validGmailDraftReview(preview)&&'kind' in preview.review&&preview.review.kind==='new_send'&&!preview.offer_id&&preview.review.to===args.to&&preview.review.subject===args.subject&&preview.review.body===args.body;
  if(args.operation==='propose_typed'||args.operation==='propose_send_typed'){
    const r=preview.review;return validGmailDraftReview(preview)&&'draft_origin' in r&&Boolean(r.draft_origin)&&r.draft_origin!.action_type===args.action_type&&r.message_id===args.message_id&&r.body===args.body&&!preview.offer_id&&
      (args.operation==='propose_send_typed'?'kind' in r&&r.kind==='reply_send':!('kind' in r))&&
      (args.baseline===undefined?r.draft_origin!.baseline_body===args.body:true);
  }
  if('draft_origin' in preview.review&&preview.review.draft_origin)return false;
  return validGmailDraftReview(preview) && (args.operation === 'propose_send' ? 'kind' in preview.review && preview.review.kind==='reply_send' && !preview.offer_id && preview.review.message_id===args.message_id && preview.review.body===args.body : args.operation === 'propose'
    ? !('kind' in preview.review) && !preview.offer_id && preview.review.message_id === args.message_id && preview.review.body === args.body
    : args.operation === 'propose_followup' ? 'kind' in preview.review && preview.review.kind==='new_email' && preview.offer_id === args.offer_id && (args.to === undefined || preview.review.to === args.to)
    : args.operation === 'propose_offer' && !('kind' in preview.review) && Boolean(preview.offer_id));
}

export function followupEmailFromOwnerText(text:string,to:string):boolean {
  const tokens=text.match(/[^\s<>@,;]+@[^\s<>@,;]+/g) ?? [];
  return tokens.map(token=>token.replace(/^[()[\]{}"]+|[()[\]{}"]+$/g,'').replace(/[.,!?]+$/g,'')).includes(to);
}

/** Explicit send words apply only to an already accepted send-specific review. */
export const clearGmailSendYes = (text:string):boolean => /^(?:yes,?\s+)?send (?:it|this reply)[.!]?$/i.test(text.trim());
