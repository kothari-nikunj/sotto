import {createTool} from '@mastra/core/tools';
import {z} from 'zod';
import type {OperationalJournal, ExplicitOwnerLoopAuthority} from '../journal/index.js';
import {readAuthority, SourceAuthorityChanged, type SourceAuthorityReader} from '../agent/source-authority.js';
import {stableActionIdentity, ActionHeldError} from './action-gate.js';
import {ownerMeetingPrepOfferArgsSchema, ownerMeetingPrepOfferKind, ownerMeetingPrepOfferMatches,
  ownerMeetingPrepOfferQuestion, ownerMeetingPrepOfferToolInputSchema} from './owner-meeting-prep-offer-contract.js';
import type {OwnerMeetingPrepOfferClient} from './owner-meeting-prep-offer-client.js';

export const MEETING_PREP_OFFER_GUIDE = 'Use this offer only for one current Calendar occurrence within the next 72 hours that has exactly one external invitee; verify its exact title, time, calendar ID and attendee email with current calendar_list/get results. If the owner has already asked directly for preparation, use the existing meeting-prep worker instead of making another offer. Then call owner_meeting_prep_offer with the exact calendar_id, event_id, start and attendee_email. It validates consent and source identity and returns one canonical question. Finish the turn with that exact review_text only; do not say preparation is queued or done. The offer activates only if that exact question is accepted by the provider. Wait for the owner to answer it before claiming full preparation is queued. Never research or send a proposal from this offer tool.';

export function createOwnerMeetingPrepOfferTool(options: {
  client: OwnerMeetingPrepOfferClient;
  journal(): OperationalJournal | undefined;
  sourceAuthority: SourceAuthorityReader;
}) {
  type Context = {requestContext?:{get(key:string):unknown}; abortSignal?:AbortSignal};
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
  const proposed=(question:string)=>({status:'proposed',review_text:question,needs_owner_confirmation:true,
    instruction:'Finish this turn with the exact review_text only. The runtime sends that canonical offer question. Do not claim preparation is queued; wait for the owner to answer it.'});
  const execute=async(input:unknown,context:Context)=>{
    const {journal,authority,eventId}=trusted(context);
    const toolInput=ownerMeetingPrepOfferToolInputSchema.parse(input);
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
      inputSchema:ownerMeetingPrepOfferToolInputSchema,
      execute,
    }),
  };
}
