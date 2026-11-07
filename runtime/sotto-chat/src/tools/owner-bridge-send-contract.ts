import {createHash} from 'node:crypto';
import {z} from 'zod';
export const ownerBridgeSendKind='owner_bridge_send_tool';
const hex=z.string().regex(/^[a-f0-9]{64}$/);
const clean=(max:number)=>z.string().min(1).max(max).refine(v=>!!v.trim()&&!/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(v));
export const bridgeSendMessageSchema=z.object({channel:z.enum(['imessage','sms','auto']),to:clean(254).refine(v=>v===v.trim()&&!/[\r\n]/.test(v)),body:clean(4000)}).strict();
const reviewSchema=bridgeSendMessageSchema.extend({operation:z.literal('send'),source_revision:hex,review_hash:hex}).strict();
const proposal=z.object({event_id:hex,action_id:hex,args_hash:hex,accepted_dispatch_id:clean(160),source_revision:hex}).strict();
export const bridgeSendArgsSchema=z.union([
 bridgeSendMessageSchema.extend({operation:z.literal('propose_send')}).strict(),
 z.object({operation:z.literal('send'),review:reviewSchema,proposal}).strict().refine(v=>v.review.source_revision===v.proposal.source_revision)
]);
export type BridgeSendArgs=z.infer<typeof bridgeSendArgsSchema>;
export const bridgeSendPreviewSchema=z.object({version:z.literal(1),status:z.literal('preview'),event_id:hex,payload_hash:hex,review:reviewSchema}).strict();
export type BridgeSendPreview=z.infer<typeof bridgeSendPreviewSchema>;
export const bridgeSendRejectionSchema=z.enum(['permission_unavailable','authority_changed','review_unavailable','event_conflict','invalid_target','send_unavailable']);
export type BridgeSendRejection=z.infer<typeof bridgeSendRejectionSchema>;
export const bridgeSendReceiptSchema=z.object({version:z.literal(1),status:z.literal('committed'),action_id:hex,event_id:hex,payload_hash:hex,args_hash:hex,outcome:z.object({operation:z.literal('send'),channel:z.enum(['imessage','sms','auto']),to:clean(254),review_hash:hex}).strict()}).strict();
export type BridgeSendReceipt=z.infer<typeof bridgeSendReceiptSchema>;
const canonical=(value:Record<string,unknown>)=>JSON.stringify(Object.fromEntries(Object.keys(value).sort().map(k=>[k,value[k]])));
export function validBridgeSendReview(preview:BridgeSendPreview){const {review_hash,...rest}=preview.review;return createHash('sha256').update(canonical(rest)).digest('hex')===review_hash;}
export function bridgeSendPreviewMatches(preview:BridgeSendPreview,args:BridgeSendArgs){return args.operation==='propose_send'&&validBridgeSendReview(preview)&&['to','body','channel'].every(k=>preview.review[k as 'to'|'body'|'channel']===args[k as 'to'|'body'|'channel']);}
export function bridgeSendReceiptMatches(receipt:BridgeSendReceipt,args:BridgeSendArgs){return args.operation==='send'&&receipt.outcome.review_hash===args.review.review_hash&&receipt.outcome.to===args.review.to&&receipt.outcome.channel===args.review.channel;}
export function bridgeSendReviewText(preview:BridgeSendPreview){const r=preview.review;return `Send ${r.channel==='sms'?'a text':r.channel==='imessage'?'an iMessage':'a message'} to ${r.to} from your Mac?\n\n${r.body}\n\nSend this?`;}
