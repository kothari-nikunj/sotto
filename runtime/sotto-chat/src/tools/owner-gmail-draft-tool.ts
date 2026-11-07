import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import type { OperationalJournal, ExplicitOwnerLoopAuthority } from '../journal/index.js';
import { readAuthority, SourceAuthorityChanged, type SourceAuthorityReader } from '../agent/source-authority.js';
import { stableActionIdentity, ActionHeldError } from './action-gate.js';
import { gmailDraftReviewText,gmailDraftPairProposalSchema,gmailNewSendProposalSchema,gmailDraftArgsSchema, ownerGmailDraftKind, gmailDraftProposalSchema, gmailSendProposalSchema, gmailTypedProposalSchema, followupEmailFromOwnerText } from './owner-gmail-draft-contract.js';
import type { OwnerGmailDraftClient } from './owner-gmail-draft-client.js';
export function createOwnerGmailDraftTools(options: {
  client: OwnerGmailDraftClient;
  journal(): OperationalJournal | undefined;
  sourceAuthority: SourceAuthorityReader;
}) {
  type Context = {
    requestContext?: {
      get(key: string): unknown;
    };
    abortSignal?: AbortSignal;
  };
  const trusted = (context: Context) => {
    const runId = context.requestContext?.get('runId'), eventId = context.requestContext?.get('ownerRequestKey');
    const journal = options.journal();
    if (!journal || context.requestContext?.get('mode') !== 'chat' || typeof runId !== 'string' ||
      typeof eventId !== 'string' || context.abortSignal?.aborted)
      throw new Error('Gmail draft authority is unavailable');
    const authority: ExplicitOwnerLoopAuthority = {
      runId, accountId: options.client.identity.account_id,
      ownerId: options.client.identity.owner_id, lineKey: options.client.identity.line_key,
      channel: options.client.identity.channel, scopeHash: options.client.scopeHash
    };
    return {
      journal, authority, eventId
    };
  };
  const sourceCurrent = async (context: Context) => {
    const current = await readAuthority(options.sourceAuthority, context.abortSignal);
    const revision = context.requestContext?.get('sourceRevision');
    if (context.abortSignal?.aborted || !current.complete || current.revision !== revision || !current.google_sources.includes('gmail'))
      throw new SourceAuthorityChanged();
  };
  const proposed=(preview:Parameters<typeof gmailDraftReviewText>[0])=>({status:'proposed',review_text:gmailDraftReviewText(preview),needs_owner_confirmation:true,instruction:'kind' in preview.review&&preview.review.kind==='reply_pair'?'Finish this turn normally with review_text exactly. The runtime delivers the canonical pair. Wait for the owner to choose Accept or Decline; neither has been saved or sent.':'Finish this turn normally with review_text exactly. The runtime delivers the canonical review. Wait for a distinct later owner yes; do not send now.'});
  const execute = async (input: unknown, context: Context) => {
    const { journal, authority, eventId } = trusted(context);
    if (context.requestContext?.get('allowOwnerGmailDraft') !== true)
      throw new Error('Gmail draft requires one owner message');
    const currentAdmission=journal.ownerReminderToolAdmission(authority,eventId,true);
    const args = gmailDraftArgsSchema.parse(input);
    if(args.operation==='send'||args.operation==='propose_pair'||args.operation==='propose_typed'||args.operation==='propose_send_typed'||args.operation==='save'&&'draft_origin' in args.review&&args.review.draft_origin) {
      const binding=stableActionIdentity(eventId,ownerGmailDraftKind,args);
      if(journal.getAction(binding.actionId)) {
        const saved=journal.readOwnerGmailDraftAction(binding.actionId,authority);
        if(saved.status==='succeeded'&&args.operation==='propose_pair'){await sourceCurrent(context);journal.ownerReminderToolAdmission(authority,eventId,true);if(saved.sourceRevision!==context.requestContext?.get('sourceRevision'))throw new SourceAuthorityChanged();return proposed(saved.preview!);}
        if(saved.status==='succeeded')return saved.preview?(args.operation==='propose_pair'?proposed(saved.preview):{status:'proposed'}):{status:'recorded',outcome:saved.receipt!.outcome};
        if(saved.status==='rejected')return {status:'rejected',code:saved.rejection};
        if(saved.status!=='pending')throw new ActionHeldError();
      }
    }
    await sourceCurrent(context);
    if(args.operation==='propose_followup' && args.to !== undefined && !followupEmailFromOwnerText(currentAdmission.text,args.to)) throw new Error('Follow-up recipient must be an exact email from the current owner message');
    // The final awaited source check precedes synchronous current-run admission and the contact marker.
    await sourceCurrent(context);
    if(args.operation==='propose_pair'||args.operation==='propose_typed'||args.operation==='propose_send_typed'){
      if(args.operation==='propose_pair'){const admitted=journal.ownerReminderToolAdmission(authority,eventId,true);options.client.preflightPair({...admitted,actionId:stableActionIdentity(eventId,ownerGmailDraftKind,args).actionId,args});}
      const request=journal.prepareOwnerGmailDraftAction(authority,eventId,args,context.requestContext?.get('sourceRevision') as string);
      const saved=journal.readOwnerGmailDraftAction(request.actionId,authority);
      if(saved.status!=='pending')throw new ActionHeldError();
      journal.beginAction(request.actionId);
      try{
        const result=await options.client.call('preview',request,context.abortSignal);
        await sourceCurrent(context);
        journal.ownerReminderToolAdmission(authority,eventId,true);
        if(result.status!=='preview'&&result.status!=='rejected')throw new Error('Typed draft review unavailable');
        journal.settleOwnerGmailDraftAction(request.actionId,authority,result.status==='preview'?result:result.code);
        return result.status==='preview'?(args.operation==='propose_pair'?proposed(result):{status:'proposed'}):{status:'rejected',code:result.code};
      }catch{
        if(journal.getAction(request.actionId)?.status==='executing')journal.settleAction(request.actionId,'unknown');
        throw new ActionHeldError();
      }
    }
    if (args.operation !== 'save' && args.operation !== 'send') {
      const admitted = journal.ownerReminderToolAdmission(authority, eventId, true);
      const binding = stableActionIdentity(eventId, ownerGmailDraftKind, args);
      const result = await options.client.call('preview', {
        ...admitted, actionId: binding.actionId, args
      }, context.abortSignal);
      await sourceCurrent(context);
      if (result.status === 'preview' && result.review.source_revision !== context.requestContext?.get('sourceRevision'))
        throw new SourceAuthorityChanged();
      if (result.status === 'no_match') return {status:'no_match'};
      if (result.status === 'offers') return result;
      if (result.status !== 'preview' && result.status !== 'rejected')
        throw new Error('Draft review unavailable');
      const request = journal.prepareOwnerGmailDraftAction(authority, eventId, args, context.requestContext?.get('sourceRevision') as string);
      const saved = journal.readOwnerGmailDraftAction(request.actionId, authority);
      if (saved.status === 'succeeded')
        return args.operation==='propose_new_send'?proposed(saved.preview!):{status:'proposed'};
      if (saved.status === 'rejected')
        return {
          status: 'rejected', code: saved.rejection
        };
      journal.beginAction(request.actionId);
      journal.settleOwnerGmailDraftAction(request.actionId, authority, result.status === 'rejected' ? result.code : result);
      return result.status === 'rejected' ? {
        status: 'rejected', code: result.code
      } : args.operation==='propose_new_send'?proposed(result):{status:'proposed'};
    }
    const request = journal.prepareOwnerGmailDraftAction(authority, eventId, args, context.requestContext?.get('sourceRevision') as string);
    const saved = journal.readOwnerGmailDraftAction(request.actionId, authority);
    if (saved.status === 'succeeded')
      return saved.preview ? {
        status: 'proposed'
      } : {
        status: 'recorded', outcome: saved.receipt!.outcome
      };
    if (saved.status === 'rejected')
      return {
        status: 'rejected', code: saved.rejection
      };
    if (saved.status !== 'pending')
      throw new ActionHeldError();
    journal.beginAction(request.actionId);
    try {
      const result = await options.client.call('apply', request, context.abortSignal);
      if (result.status === 'preview') {
        journal.settleOwnerGmailDraftAction(request.actionId, authority, result);
        return {
          status: 'proposed'
        };
      }
      if (result.status === 'committed') {
        journal.settleOwnerGmailDraftAction(request.actionId, authority, result);
        return {
          status: 'recorded', outcome: result.outcome
        };
      }
      if (result.status === 'rejected') {
        journal.settleOwnerGmailDraftAction(request.actionId, authority, result.code);
        return {
          status: 'rejected', code: result.code
        };
      }
      throw new Error('Gmail draft receipt is unavailable');
    }
    catch {
      const action = journal.getAction(request.actionId);
      if (action?.status === 'executing')
        journal.settleAction(request.actionId, 'unknown');
      throw new ActionHeldError();
    }
  };
  const typedReview = async (input: {message_id:string;body:string;action_type?:'reply'|'decline'}, sending:boolean, context:Context) => {
    if(input.action_type===undefined)return execute({operation:sending?'propose_send':'propose',message_id:input.message_id,body:input.body},context);
    const {journal,authority,eventId}=trusted(context);
    journal.ownerReminderToolAdmission(authority,eventId,true);
    const operation=sending?'propose_send_typed':'propose_typed';
    const existing=journal.listOwnerGmailDraftActionsForThread(eventId).find(action=>action.source_event_id===eventId);
    if(existing){
      const saved=journal.readOwnerGmailDraftAction(existing.id as string,authority);const args=saved.request.args;
      if(args.operation!==operation||args.message_id!==input.message_id||args.body!==input.body||args.action_type!==input.action_type)throw new Error('Typed draft event conflicts with saved arguments');
      return execute(args,context);
    }
    await sourceCurrent(context);
    const revision=context.requestContext?.get('sourceRevision');if(typeof revision!=='string')throw new SourceAuthorityChanged();
    const baseline=journal.ownerGmailTypedBaseline(authority,eventId,revision,input.message_id,input.action_type);
    return execute(gmailTypedProposalSchema.parse({operation,message_id:input.message_id,body:input.body,action_type:input.action_type,...(baseline?{baseline}:{})}),context);
  };
  const reviewInput=gmailDraftProposalSchema.omit({operation:true}).extend({action_type:z.enum(['reply','decline']).optional()});
  return {
    gmail_draft_pair:createTool({id:'gmail_draft_pair',description:'Present both original Accept and Decline reply choices for the actual Gmail message. Recording the accepted offered pair is local learning only; no draft save or send. Finish normally with exact returned review_text and wait for a choice. Later selected typed review still requires separate save/send approval.',inputSchema:gmailDraftPairProposalSchema.omit({operation:true}),execute:async(input,context)=>execute({operation:'propose_pair',...gmailDraftPairProposalSchema.omit({operation:true}).parse(input)},context)}),
    gmail_new_send_review:createTool({id:'gmail_new_send_review',description:'Review an explicitly requested new outgoing email with one grounded bare recipient address, subject and full body. No fabricated reply message or thread. Full From/To/Subject/body review then a distinct later yes via gmail_send_confirm; nothing is sent during review.',inputSchema:gmailNewSendProposalSchema.omit({operation:true}),execute:async(input,context)=>execute({operation:'propose_new_send',...gmailNewSendProposalSchema.omit({operation:true}).parse(input)},context)}),
    gmail_draft_followup_list: createTool({id:'gmail_draft_followup_list',description:'List accepted follow-up draft offers on the current owner route. Read-only; do not invent a recipient or body. Clarify multiple offers before choosing an exact returned offer ID.',inputSchema:z.object({}).strict(),execute:async(input,context)=>{z.object({}).strict().parse(input);return execute({operation:'list_followup'},context);}}),
    gmail_draft_followup_review: createTool({id:'gmail_draft_followup_review',description:'Present the full recipient/subject/body review for one exact offer ID returned by gmail_draft_followup_list. For a missing email destination only, optional to must be an exact email supplied in the current owner message; known destinations cannot change and non-email drafts are unsupported. A distinct subsequent yes is mandatory before saving. Never sends.',inputSchema:z.object({offer_id:z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/),to:z.string().min(1).max(254).optional()}).strict(),execute:async(input,context)=>execute({operation:'propose_followup',...z.object({offer_id:z.string().regex(/^(?:[a-f0-9]{32}|[a-f0-9]{64})$/),to:z.string().min(1).max(254).optional()}).strict().parse(input)},context)}),
    gmail_draft_offer: createTool({id:'gmail_draft_offer',description:'Continue an accepted offer to save a prepared reply in Gmail drafts, not an inline offer to write reply wording in chat. Continue only the accepted Gmail draft offer that the owner is affirming in this message. Takes no arguments: the receiver selects the trusted exact reply or admitted continuation and offered body. Produces a complete review; a separate yes is mandatory before saving. Never sends.',inputSchema:z.object({}).strict(),execute:async(input,context)=>{
      z.object({}).strict().parse(input);
      const {journal,authority,eventId}=trusted(context);
      journal.ownerReminderToolAdmission(authority,eventId,true);
      const selection=journal.ownerGmailDraftOfferSelection(eventId);
      if(!selection)throw new Error('Reply directly to the current Gmail draft offer');
      return execute({operation:'propose_offer',selection},context);
    }}),
    gmail_draft_propose:createTool({id:'gmail_draft_propose',description:'Review one owner-requested Gmail reply draft. Resolve the actual message. When the owner selects an original Accept/Decline choice, pass action_type reply|decline to visibly type that selected draft; edits preserve an eligible accepted typed baseline. Full review and separate yes required before saving; never sends.',inputSchema:reviewInput,execute:async(input,context)=>typedReview(reviewInput.parse(input),false,context)}),
    gmail_send_review:createTool({id:'gmail_send_review',description:'Present complete From/To/Subject/body review for an explicit owner-requested outgoing reply. For the selected original choice pass action_type reply|decline; preserve that same type when editing its accepted baseline. Separate later send confirmation required; never consumes saved drafts.',inputSchema:reviewInput,execute:async(input,context)=>typedReview(reviewInput.parse(input),true,context)}),
    gmail_send_confirm:createTool({id:'gmail_send_confirm',description:'Send only the exact most recent accepted email-send review after a distinct subsequent owner yes. No content or recipient inputs; never treats draft-saving approval as send consent.',inputSchema:z.object({}).strict(),execute:async(input,context)=>{
      z.object({}).strict().parse(input);
      const {journal,authority,eventId}=trusted(context);
      journal.ownerReminderToolAdmission(authority,eventId,true);
      const existing=journal.listOwnerGmailDraftActionsForThread(eventId).find(action=>action.source_event_id===eventId);
      if(existing) {
        const saved=journal.readOwnerGmailDraftAction(existing.id as string,authority);
        if(saved.request.args.operation==='send')return execute(saved.request.args,context);
      }
      await sourceCurrent(context);
      const revision=context.requestContext?.get('sourceRevision');if(typeof revision!=='string')throw new Error('Email send review source proof unavailable');
      const selected=journal.selectOwnerGmailDraftProposal(authority,eventId,revision);if(selected.operation!=='send')throw new Error('This accepted review is for saving a draft, not sending');
      return execute(selected,context);
    }}),
    gmail_draft_save: createTool({
      id: 'gmail_draft_save', description: 'Save only the exact most recent accepted Gmail draft review when the owner separately says a clear yes. Takes no content, recipient or approval arguments. Edits require a new review. Never sends.', inputSchema: z.object({}).strict(), execute: async (_input, context) => {
        z.object({}).strict().parse(_input);
        const { journal, authority, eventId } = trusted(context);
        journal.ownerReminderToolAdmission(authority,eventId,true);
        const existing=journal.listOwnerGmailDraftActionsForThread(eventId).find(action=>action.source_event_id===eventId);
        if(existing){
          const saved=journal.readOwnerGmailDraftAction(existing.id as string,authority);
          if(saved.request.args.operation==='save'&&'draft_origin' in saved.request.args.review&&saved.request.args.review.draft_origin)return execute(saved.request.args,context);
        }
        await sourceCurrent(context);
        const revision = context.requestContext?.get('sourceRevision');
        if (typeof revision !== 'string')
          throw new Error('Draft review source proof unavailable');
        const selected=journal.selectOwnerGmailDraftProposal(authority,eventId,revision);
        if(selected.operation!=='save') throw new Error('This accepted review requires Gmail send confirmation');
        return execute(selected, context);
      }
    }),
  };
}

export const GMAIL_DRAFT_EXECUTION_GUIDE = `Only when the proved current context unambiguously selects an inline wording offer, with no competing pending offer of any kind, first follow the draft-reply skill: if a source asks for an owner decision and stance is unknown, ask the owner before drafting or fetching voice guidance; after any required owner clarification, deliver its single requested version in chat without gmail_draft_offer, gmail_draft_propose or gmail_draft_pair. This single-version restriction does not apply when the owner explicitly requests alternatives; use the alternatives path below. With multiple offers or an unknown offer kind, keep receiver-owned selection or clarify; never choose from a bare yes. A request for wording alone is not a request to save it in Gmail. An accepted external save offer such as “Want this in your Gmail drafts?” follows the receiver-owned offer review below. For the loaded draft-reply skill, a yes to an accepted proactive Gmail draft offer uses gmail_draft_offer with {}. Its selector and offered body are receiver-owned; do not paraphrase the offer or invent an address, message ID or thread. A no-match is not a draft proposal. The receiver presents the full exact To/Subject/body review; only a distinct subsequent yes may use gmail_draft_save with {}. This bounded adapter requires that separate review even when the earlier offer already showed a draft. For follow-up output, call gmail_draft_followup_list with {}, clarify multiple candidates, then gmail_draft_followup_review with one exact returned offer_id. The receiver owns the new-email recipient/subject/body; no guessed Gmail message/thread IDs. If an email destination is missing, ask the owner for it, then pass optional to containing that exact current-message email. Never look it up or invent it; known destinations remain immutable. Non-email drafts cannot be saved. Explicit owner-requested Gmail draft saves retain gmail_draft_propose. When the owner wants alternative replies to an undecided original declinable Gmail request, use gmail_draft_pair with the actual message_id and both complete accept_body and decline_body (optional first reply|decline). Finish normally with its exact review_text; the runtime records both only after accepted delivery, never saves or sends them. Wait for the owner’s choice; bare yes cannot choose between them. After the owner selects an original Accept or Decline choice, pass action_type "reply" or "decline" to gmail_draft_propose or gmail_send_review. Preserve the same action_type during edits; the adapter supplies the exact accepted root baseline, never provide draft keys, baseline text or proof. Declines remain review-tier. Each edit presents a fresh complete typed review and requires a distinct later approval. Only confirmed save/send receipts can record original draft outcomes; pending learning does not invalidate a successful primary effect. Saving never sends mail. For an explicit owner-requested new email, use gmail_new_send_review with grounded to, subject and full body; never fabricate a reply message or thread. Finish the full review normally, then wait for a distinct later yes via gmail_send_confirm. For an explicit owner-requested outgoing reply, use gmail_send_review with the actual message_id and full body. The receiver presents the complete From/To/Subject/body send review; only a distinct later owner yes permits gmail_send_confirm with {}. Draft-save confirmation never authorizes send. This sends reviewed reply text, not a saved Gmail draft. Never claim recipient delivery or close obligations from a send receipt.`;
