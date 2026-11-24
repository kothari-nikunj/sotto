import {createTool} from '@mastra/core/tools';
import {z} from 'zod';
import type {OperationalJournal, ExplicitOwnerLoopAuthority} from '../journal/index.js';
import {readAuthority, SourceAuthorityChanged, type SourceAuthorityReader} from '../agent/source-authority.js';
import {stableActionIdentity, ActionHeldError} from './action-gate.js';
import {ownerMeetingPrepOfferArgsSchema, ownerMeetingPrepOfferKind, ownerMeetingPrepOfferMatches,
  ownerMeetingPrepOfferQuestion, ownerMeetingPrepOfferToolInputSchema} from './owner-meeting-prep-offer-contract.js';
import type {OwnerMeetingPrepOfferClient} from './owner-meeting-prep-offer-client.js';

const inputSchema = ownerMeetingPrepOfferToolInputSchema.extend({
  standalone: z.boolean().optional().describe('True only if the canonical offer question answers the whole owner message. False if any separate work or answer is needed.'),
});

export const MEETING_PREP_OFFER_GUIDE = 'Use this offer only for one current Calendar occurrence within the next 72 hours that has exactly one external invitee; verify its exact title, time, calendar ID and attendee email with current calendar_list/get results. If the owner has already asked directly for preparation, use the existing meeting-prep worker instead of making another offer. Use owner_meeting_prep_offer with standalone:true only when the offer question is the whole reply. For mixed requests, complete the requested work and answer the independent parts without staging an optional prep question. The runtime delivers the canonical offer after consent and identity checks; do not say preparation is queued or done. The offer activates only if that exact question is accepted by the provider. Wait for the owner to answer it before claiming full preparation is queued. Never research or send a proposal from this offer tool.';

export function createOwnerMeetingPrepOfferTool(options: {
  client: OwnerMeetingPrepOfferClient;
  journal(): OperationalJournal | undefined;
  sourceAuthority: SourceAuthorityReader;
}) {
  type Context = {requestContext?:{get(key:string):unknown;set(key:string,value:string):void}; abortSignal?:AbortSignal};
  const trusted = (context:Context) => {
    const journal=options.journal(), runId=context.requestContext?.get('runId'), eventId=context.requestContext?.get('ownerRequestKey');
    if(!journal||context.requestContext?.get('mode')!=='chat'||typeof runId!=='string'||typeof eventId!=='string'||
       context.requestContext?.get('allowOwnerMeetingPrepOffer')!==true||context.abortSignal?.aborted)
      throw new Error('Meeting prep offer requires one current owner message');
    const authority:ExplicitOwnerLoopAuthority={runId,accountId:options.client.identity.account_id,
      ownerId:options.client.identity.owner_id,lineKey:options.client.identity.line_key,
      channel:options.client.identity.channel,scopeHash:options.client.scopeHash};
    return {journal,authority,eventId};
  };
  const sourceCurrent=async(context:Context,revision:unknown)=>{
    const current=await readAuthority(options.sourceAuthority,context.abortSignal);
    if(context.abortSignal?.aborted||!current.complete||current.revision!==revision||!current.google_sources.includes('calendar'))
      throw new SourceAuthorityChanged();
    return current.revision;
  };
  const execute=async(input:unknown,context:Context)=>{
    const {journal,authority,eventId}=trusted(context);
    const {standalone,...toolInput}=inputSchema.parse(input);
    if(standalone===false || context.requestContext?.get('ownerPrepOfferHandoff')==='deferred')return {status:'deferred',instruction:'No offer was staged. Finish the owner’s requested work and answer the independent parts. For directly requested prep, use the meeting-prep worker.'};
    const proposed=(question:string)=>{
      if(standalone===true)context.requestContext?.set('ownerPrepOfferHandoff','settled');
      return {status:'proposed',review_text:question,needs_owner_confirmation:true,
        instruction:'The runtime delivers this exact question. Wait for the owner to answer before claiming preparation is queued.'};
    };
    const revision=context.requestContext?.get('sourceRevision');
    await sourceCurrent(context,revision);
    const admitted=journal.ownerReminderToolAdmission(authority,eventId,true);
    const args=ownerMeetingPrepOfferArgsSchema.parse({...toolInput,source_revision:revision});
    const binding=stableActionIdentity(eventId,ownerMeetingPrepOfferKind,args);
    const prior=journal.getAction(binding.actionId);
    if(prior){
      const saved=journal.readOwnerMeetingPrepOfferAction(binding.actionId,authority);
      if(saved.status==='succeeded'&&saved.preview){
        if(saved.sourceRevision!==revision)throw new SourceAuthorityChanged();
        return proposed(ownerMeetingPrepOfferQuestion(saved.preview.review));
      }
      if(saved.status==='rejected')return {status:'rejected',code:saved.rejection};
      if(saved.status!=='pending')throw new ActionHeldError();
    }
    const request=journal.prepareOwnerMeetingPrepOfferAction(authority,eventId,args,String(revision));
    const saved=journal.readOwnerMeetingPrepOfferAction(request.actionId,authority);
    if(saved.status==='succeeded'&&saved.preview)return proposed(ownerMeetingPrepOfferQuestion(saved.preview.review));
    if(saved.status!=='pending')throw new ActionHeldError();
    journal.beginAction(request.actionId);
    try{
      const result=await options.client.call('preview',request,context.abortSignal);
      await sourceCurrent(context,revision);
      journal.ownerReminderToolAdmission(authority,eventId,true);
      if(result.status==='rejected'){
        journal.settleOwnerMeetingPrepOfferAction(request.actionId,authority,result.code);
        return {status:'rejected',code:result.code};
      }
      if(result.status!=='preview'||result.action_id!==request.actionId||result.event_id!==eventId||
         result.payload_hash!==admitted.payloadHash||!ownerMeetingPrepOfferMatches(result.review,args,String(revision)))
        throw new Error('Meeting prep offer preview is invalid');
      journal.settleOwnerMeetingPrepOfferAction(request.actionId,authority,result);
      return proposed(ownerMeetingPrepOfferQuestion(result.review));
    }catch{
      if(journal.getAction(request.actionId)?.status==='executing')journal.settleAction(request.actionId,'unknown');
      throw new ActionHeldError();
    }
  };
  return {
    owner_meeting_prep_offer:createTool({
      id:'owner_meeting_prep_offer',
      description:'Offer a full meeting-preparation briefing only for one exact Calendar occurrence within the next 72 hours with exactly one external invitee. Use only after current Calendar reads establish its exact calendar_id, event_id, start and invited email. If the owner has already asked directly for preparation, use the existing meeting-prep worker instead of creating another offer. The receiver independently revalidates consent, occurrence and invitee. This stages only a question; no research or prep is queued until the owner accepts that exact delivered question. Never invent an attendee, event or time.',
      inputSchema,
      execute,
    }),
  };
}
