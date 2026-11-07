/** Framework-neutral private preference execution; model runtimes supply only trusted context. */
import type {OperationalJournal, ExplicitOwnerLoopAuthority} from '../journal/index.js';
import {readAuthority, SourceAuthorityChanged, type SourceAuthorityReader} from '../agent/source-authority.js';
import {stableActionIdentity, ActionHeldError} from './action-gate.js';
import {ownerPreferenceToolArgsSchema, ownerPreferenceToolKind, usefulnessListSchema, type UsefulnessList} from './owner-preference-tool-contract.js';
import type {OwnerPreferenceToolClient} from './owner-preference-tool-client.js';
export interface OwnerPreferenceExecutionContext {
  mode?: unknown; runId?: unknown; ownerRequestKey?: unknown; allowOwnerPreference?: unknown;
  sourceRevision?: unknown; usefulnessList?: unknown; setUsefulnessList?(value:UsefulnessList):void; signal?: AbortSignal;
}
export interface OwnerPreferenceExecutionOptions {
  client: OwnerPreferenceToolClient;
  journal(): OperationalJournal | undefined;
  sourceAuthority: SourceAuthorityReader;
}
export function createOwnerPreferenceExecution(options: OwnerPreferenceExecutionOptions) {
  const trusted = (context: OwnerPreferenceExecutionContext) => {
    const runId = context.runId, eventId = context.ownerRequestKey;
    const journal = options.journal();
    if (!journal || context.mode !== 'chat' || typeof runId !== 'string' ||
        typeof eventId !== 'string' || context.signal?.aborted) throw new Error('Owner preference authority is unavailable');
    const authority: ExplicitOwnerLoopAuthority = {runId, accountId: options.client.identity.account_id,
      ownerId: options.client.identity.owner_id, lineKey: options.client.identity.line_key,
      channel: options.client.identity.channel, scopeHash: options.client.scopeHash};
    return {journal, authority, eventId};
  };
  const sourceCurrent = async (context: OwnerPreferenceExecutionContext) => {
    const current = await readAuthority(options.sourceAuthority, context.signal);
    const revision = context.sourceRevision;
    if (context.signal?.aborted || (current.complete ? current.revision !== revision : revision !== undefined))
      throw new SourceAuthorityChanged();
  };
  const usefulnessSource = async (context: OwnerPreferenceExecutionContext) => {
    const current = await readAuthority(options.sourceAuthority, context.signal);
    if (context.signal?.aborted || !current.complete || !current.revision || current.revision !== context.sourceRevision)
      throw new SourceAuthorityChanged();
    return current.revision;
  };
  const change = async (input: unknown, context: OwnerPreferenceExecutionContext) => {
    const {journal, authority, eventId} = trusted(context);
    if (context.allowOwnerPreference !== true) throw new Error('Owner preference requires one owner message');
    const args = ownerPreferenceToolArgsSchema.parse(input);
    if(args.operation==='usefulness'||args.operation==='snooze-nudges'||args.operation==='unsnooze-nudges') {
      journal.ownerReminderToolAdmission(authority,eventId,true);
      const existing=journal.listOwnerPreferenceToolActionsForThread(eventId).find(action=>action.source_event_id===eventId);
      if(existing) {
        const saved=journal.readOwnerPreferenceToolAction(existing.id as string,authority);
        if(stableActionIdentity(eventId,ownerPreferenceToolKind,saved.request.args).actionId!==stableActionIdentity(eventId,ownerPreferenceToolKind,args).actionId)
          throw new Error('Owner preference event conflicts with saved arguments');
        if(saved.status==='succeeded')return {status:'recorded',outcome:saved.receipt!.outcome};
        if(saved.status==='rejected')return {status:'rejected',code:saved.rejection};
        if(saved.status!=='pending')throw new ActionHeldError();
      }
      if(args.operation==='usefulness') {if(await usefulnessSource(context)!==args.source_revision)throw new SourceAuthorityChanged();}
      else await sourceCurrent(context);
    } else await sourceCurrent(context);
    const admission = journal.ownerReminderToolAdmission(authority, eventId, true);
    const binding = stableActionIdentity(eventId, ownerPreferenceToolKind, args);
    if(args.operation==='propose-procedure'||args.operation==='propose-priorities'||args.operation==='propose-context'){
      const request=journal.prepareOwnerPreferenceToolAction(authority,eventId,args);
      const saved=journal.readOwnerPreferenceToolAction(request.actionId,authority);
      if(saved.status==='succeeded'||saved.status==='rejected')return {status:saved.status==='succeeded'?'proposed':'rejected'};
      if(saved.status!=='pending')throw new ActionHeldError();
      const preview=await options.client.call('preview',request,context.signal);
      await sourceCurrent(context);journal.ownerReminderToolAdmission(authority,eventId,true);
      if(preview.status!=='preview'&&preview.status!=='rejected')throw new Error('Standing-rule preview unavailable');
      journal.beginAction(request.actionId);
      journal.settleOwnerPreferenceToolAction(request.actionId,authority,preview.status==='preview'?preview:'capacity' in preview?{code:'master_capacity',capacity:preview.capacity}:preview.code);
      return {status:preview.status==='preview'?'proposed':'rejected'};
    }
    if (args.operation === 'accept-procedure-offer' && !journal.getAction(binding.actionId)) {
      const preview = await options.client.call('preview', {...admission, actionId: binding.actionId, args}, context.signal);
      await sourceCurrent(context);
      journal.ownerReminderToolAdmission(authority, eventId, true);
      if (preview.status === 'no_match') return {status: 'no_match'};
      if (preview.status !== 'match') throw new Error('Standing-rule preview is unavailable');
    }
    // The final awaited source check precedes synchronous current-run admission and the contact marker.
    await sourceCurrent(context);
    const request = journal.prepareOwnerPreferenceToolAction(authority, eventId, args);
    const saved = journal.readOwnerPreferenceToolAction(request.actionId, authority);
    if (saved.status === 'succeeded') return {status: 'recorded', outcome: saved.receipt!.outcome};
    if (saved.status === 'rejected') return {status: 'rejected', code: saved.rejection};
    if (saved.status !== 'pending') throw new ActionHeldError();
    journal.beginAction(request.actionId);
    try {
      const result = await options.client.call('apply', request, context.signal);
      if (result.status === 'committed') {
        journal.settleOwnerPreferenceToolAction(request.actionId, authority, result);
        return {status: 'recorded', outcome: result.outcome};
      }
      if (result.status === 'rejected') {
        journal.settleOwnerPreferenceToolAction(request.actionId, authority, 'capacity' in result?{code:'master_capacity',capacity:result.capacity}:result.code);
        return {status: 'rejected', code: result.code};
      }
      throw new Error('Owner preference receipt is unavailable');
    } catch {
      const action = journal.getAction(request.actionId);
      if (action?.status === 'executing') journal.settleAction(request.actionId, 'unknown');
      throw new ActionHeldError();
    }
  };
  const read = async (context: OwnerPreferenceExecutionContext) => {
    const {journal, authority, eventId} = trusted(context);
    await sourceCurrent(context);
    const admitted = journal.ownerReminderToolAdmission(authority, eventId);
    const binding = stableActionIdentity(eventId, ownerPreferenceToolKind, null);
    const result = await options.client.call('read', {...admitted, actionId: binding.actionId, args: null}, context.signal);
    if (result.status !== 'read' || !journal.isRunCurrent(authority.runId)) throw new Error('Owner preferences unavailable');
    await sourceCurrent(context);
    return {coverage: result.coverage, preferences: result.preferences, nudge_budget: result.nudge_budget, ...(result.nudge_snooze_until===undefined?{}:{nudge_snooze_until:result.nudge_snooze_until}), truncated_fields: result.truncated_fields};
  };
  const acceptOffer = async (context: OwnerPreferenceExecutionContext) => {
    const {journal, eventId} = trusted(context);
    const selection = journal.ownerProcedureOfferSelection(eventId);
    if (!selection) throw new Error('Reply directly to the standing-rule offer');
    return change({operation: 'accept-procedure-offer', selection}, context);
  };
  const confirmProcedure=async(context:OwnerPreferenceExecutionContext,operation:'append-procedure'|'replace-priorities'|'append-context'='append-procedure',section?:'About'|'People')=>{
    const {journal,authority,eventId}=trusted(context);
    const selected=journal.selectOwnerProcedureProposal(authority,eventId);
    if(selected.operation!==operation||(section!==undefined&&(selected.operation!=='append-context'||selected.review.section!==section)))throw new Error('The accepted review is for a different standing-context operation');
    return change(selected,context);
  };
  const listUsefulness = async (context: OwnerPreferenceExecutionContext) => {
    const {journal,authority,eventId}=trusted(context);
    const revision=await usefulnessSource(context);
    const admitted=journal.ownerReminderToolAdmission(authority,eventId);
    const args={operation:'usefulness-list' as const,source_revision:revision};
    const binding=stableActionIdentity(eventId,ownerPreferenceToolKind,args);
    const result=await options.client.call('read',{...admitted,actionId:binding.actionId,args},context.signal);
    if(result.status!=='usefulness_list')throw new Error('Usefulness list unavailable');
    await usefulnessSource(context);
    journal.ownerReminderToolAdmission(authority,eventId);
    context.setUsefulnessList?.(result);
    return result;
  };
  const rateUsefulness = async (input:{rating:'useful'|'not_useful';reference:string;excerpt:string},context:OwnerPreferenceExecutionContext) => {
    const {journal,authority,eventId}=trusted(context);
    if(context.allowOwnerPreference!==true)throw new Error('Owner preference requires one owner message');
    journal.ownerReminderToolAdmission(authority,eventId,true);
    const existing=journal.listOwnerPreferenceToolActionsForThread(eventId).find(action=>action.source_event_id===eventId);
    if(existing) {
      const args=journal.readOwnerPreferenceToolAction(existing.id as string,authority).request.args;
      if(args?.operation!=='usefulness'||args.rating!==input.rating||args.reference!==input.reference||args.excerpt!==input.excerpt)
        throw new Error('Owner preference event conflicts with saved arguments');
      return change(args,context);
    }
    const parsedList=usefulnessListSchema.safeParse(context.usefulnessList);
    if(!parsedList.success||parsedList.data.event_id!==eventId||parsedList.data.source_revision!==context.sourceRevision)
      throw new Error('Read the current usefulness list before rating: run_sotto_script id "usefulness-feedback" with args ["list"] in this owner turn.');
    const listed=parsedList.data;
    const item=listed.items.find(item=>item.reference===input.reference);
    if(!item)throw new Error('Usefulness target unavailable');
    return change({operation:'usefulness',...input,archive_version:item.archive_version,source_revision:listed.source_revision},context);
  };
  return {read, change, acceptOffer,confirmProcedure,listUsefulness,rateUsefulness};
}
