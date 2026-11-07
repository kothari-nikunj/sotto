/** Framework-neutral admitted task execution; transport and journal own effects. */
import type { OperationalJournal, ExplicitOwnerLoopAuthority } from '../journal/index.js';
import { readAuthority, SourceAuthorityChanged, type SourceAuthorityReader } from '../agent/source-authority.js';
import {stableActionIdentity} from './action-gate.js';
import {ownerTaskToolKind,taskReviewText,SIMILAR_FACT_TEXT,resolveManyOwnerTextMatches,ownerTaskTitleMatches,namedLoopSelection,namedLoopDescriptionMatches,loopContactName,explicitLoopThreadSelection,type TaskPreview,OwnerTaskListUnavailable,OWNER_TASK_SELECTION_CLARIFICATION} from './owner-task-tool-contract.js';
import {CleanupDiagnosticError} from './cleanup-diagnostic.js';
import { ActionHeldError } from './action-gate.js';
import { ownerTaskToolArgsSchema, type OwnerTaskToolArgs } from './owner-task-tool-contract.js';
import type { OwnerTaskToolClient } from './owner-task-tool-client.js';

export interface OwnerTaskExecutionContext {mode?:unknown;runId?:unknown;ownerRequestKey?:unknown;allowOwnerTask?:unknown;sourceRevision?:unknown;signal?:AbortSignal}
export class ResolveManyClarification extends Error {}
/** Only durable outcomes whose exact reply is owned by the coordinator end a model turn. */
export function ownerTaskReplySettled(result: unknown): boolean {
  return !!result && typeof result === 'object' && 'status' in result &&
    ['recorded', 'proposed', 'rejected'].includes(String(result.status));
}
export function createOwnerTaskExecution(options: {
  client: OwnerTaskToolClient; journal(): OperationalJournal | undefined; sourceAuthority: SourceAuthorityReader;
}) {

  const trusted = (context: OwnerTaskExecutionContext) => {
    const runId = context.runId, eventId = context.ownerRequestKey;
    const journal = options.journal();
    if (!journal || context.mode !== 'chat' || typeof runId !== 'string' ||
        typeof eventId !== 'string' || context.signal?.aborted) throw new Error('Owner task authority is unavailable');
    const authority: ExplicitOwnerLoopAuthority = {runId, accountId: options.client.identity.account_id,
      ownerId: options.client.identity.owner_id, lineKey: options.client.identity.line_key,
      channel: options.client.identity.channel, scopeHash: options.client.scopeHash};
    return {journal, authority, eventId};
  };
  const sourceCurrent = async (context: OwnerTaskExecutionContext) => {
    const current = await readAuthority(options.sourceAuthority, context.signal);
    const revision = context.sourceRevision;
    if (context.signal?.aborted || (current.complete ? current.revision !== revision : revision !== undefined))
      throw new SourceAuthorityChanged();
  };
  const recorded = (receipt: import('./owner-task-tool-contract.js').OwnerTaskToolReceipt) => receipt.operation==='fact_add'?{status:'recorded',canonical_id:receipt.canonical_id,fact_id:receipt.fact_id,added:receipt.added}:receipt.operation==='cleanup'||receipt.operation==='resolve_many'?{status:'recorded',outcome:receipt.outcome,items:receipt.items}:receipt.operation==='relation'?{status:'recorded',mode:receipt.mode,changed:receipt.changed}:(receipt.operation==='add_identifier'||receipt.operation==='identifier')?{status:'recorded',canonical_id:receipt.canonical_id,identifier:receipt.identifier,added:receipt.added}:receipt.operation==='fact_correct'
    ? {status:'recorded',canonical_id:receipt.canonical_id,fact_id:receipt.fact_id,replacement_fact_id:receipt.replacement_fact_id}
    : {status:'recorded',anchor_key:receipt.anchor_key,loop_status:receipt.loop_status};
  const proposed=(preview:TaskPreview)=>preview.review.operation==='cleanup_list'?{status:'proposed' as const,review_text:taskReviewText(preview.review),needs_owner_selection:true as const,instruction:'Finish this turn normally with an assistant response containing review_text exactly, without more tools. Nothing has changed. Wait for a later owner message with explicit numbered done, dismiss, snooze or keep choices; a yes is not a selection.'}:({status:'proposed' as const,review_text:taskReviewText(preview.review),needs_owner_confirmation:true as const,instruction:'The runtime will deliver this exact review. Finish this turn normally with an assistant response containing review_text exactly, without more tools, then wait for a distinct later owner yes. This is a proposal, not permission to apply the change.'});
  const execute = async (input: OwnerTaskToolArgs, context: OwnerTaskExecutionContext) => {
    const {journal, authority, eventId} = trusted(context);
    if (context.allowOwnerTask !== true) throw new Error('Owner task requires one owner message');
    const args = ownerTaskToolArgsSchema.parse(input);
    if(args.operation==='add_identifier')throw new Error('Identifier teaching requires an accepted review and separate yes');
    if(args.operation==='cleanup'&&context.sourceRevision!==args.list.source_revision)throw new SourceAuthorityChanged();
    if(args.operation==='resolve_many'&&context.sourceRevision!==args.source_revision)throw new SourceAuthorityChanged();
    if((args.operation==='relation'||args.operation==='identifier'||args.operation==='fact_add')&&context.sourceRevision!==args.review.source_revision)throw new SourceAuthorityChanged();
    if((args.operation==='snooze'||args.operation==='keep')){
      journal.ownerReminderToolAdmission(authority,eventId,true);
      const previous=journal.getAction(stableActionIdentity(eventId,ownerTaskToolKind,args).actionId);
      if(previous){const saved=journal.readOwnerTaskToolAction(previous.id as string,authority);if(saved.status==='succeeded')return recorded(saved.receipt!);if(saved.status==='rejected')return {status:'rejected',code:saved.rejection};if(saved.status!=='pending')throw new ActionHeldError();}
    }
    await sourceCurrent(context);
    if((args.operation==='fact_correct'||args.operation==='propose_relation'||args.operation==='propose_identifier'||args.operation==='propose_fact'||args.operation==='propose_cleanup') && context.sourceRevision!==args.source_revision)throw new SourceAuthorityChanged();
    const admission=journal.ownerReminderToolAdmission(authority,eventId,true);
    // Reuse the exact durable result before rediscovering its former open rows.
    // Successful closures remove those rows; normalizing a replay against the
    // new projection would change its bound arguments and lose the receipt.
    let previous=journal.getAction(stableActionIdentity(eventId,ownerTaskToolKind,args).actionId);
    if(!previous&&args.operation==='resolve_many'&&args.allow_partial){
      const candidates=journal.db.prepare('SELECT id FROM actions WHERE source_event_id=? AND kind=? LIMIT 2').all(eventId,ownerTaskToolKind);
      if(candidates.length>1)throw new Error('Owner task event conflicts with saved arguments');
      previous=candidates[0];
    }
    if(previous){
      const saved=journal.readOwnerTaskToolAction(previous.id as string,authority);
      let replayArgs=args;
      if(args.operation==='resolve_many'&&saved.request.args.operation==='resolve_many'&&args.allow_partial){
        const stored=saved.request.args;
        // The first selection erased unusable proofs. Reproduce only those
        // omissions; proved items and every owner phrase must still match.
        // Changed proofs for a refused item can only replay its old refusal;
        // they cannot upgrade or retry this already-settled owner event.
        replayArgs={...args,items:args.items.map((item,index)=>{
          if(stored.items[index]?.anchor_key!==undefined)return item;
          return {name:item.name,correction:item.correction};
        })};
      }
      if(stableActionIdentity(eventId,ownerTaskToolKind,replayArgs).actionId!==previous.id)
        throw new Error('Owner task event conflicts with saved arguments');
      if(saved.status==='succeeded')return saved.preview?proposed(saved.preview):recorded(saved.receipt!);
      if(saved.status==='rejected')return {status:'rejected',code:saved.rejection};
      if(saved.status!=='pending')throw new ActionHeldError();
    }
    if(args.operation==='close'&&!journal.getAction(stableActionIdentity(eventId,ownerTaskToolKind,args).actionId)){
      const repair={status:'needs_selection',message:'Nothing changed. For one finished task, supply correction as its exact phrase from this owner message. If several tasks are done, read the current rows and use owner_task_resolve_many once for all of them. Leave explicitly open tasks out.'};
      if(!args.correction||!resolveManyOwnerTextMatches([{name:args.correction,correction:args.correction}],admission.text,'owner_tasks'))return repair;
      let rows;
      try{rows=await options.client.readOwnerTasks(context.signal);}catch{return {status:'needs_clarification',message:'Nothing changed. I could not check the current owner task selection.'};}
      const matches=[...rows.you_owe,...rows.waiting_on_them].filter(row=>row.channel==='manual'&&ownerTaskTitleMatches(row.what,args.correction!));
      if(matches.length!==1||matches[0]!.anchor_key!==args.anchor)return {status:'needs_clarification',message:'Nothing changed. That phrase does not uniquely select the current task; use its exact task text.'};
    }
    if((args.operation==='correct' || args.operation==='retarget' || (args.operation==='snooze'||args.operation==='keep')) && !journal.getAction(stableActionIdentity(eventId,ownerTaskToolKind,args).actionId)) {
      if(context.sourceRevision!==args.source_revision || !admission.text.includes(args.correction))throw new Error('Correction authority unavailable');
      const rows=await options.client.readLoops(args.source_revision,context.signal,args.operation==='keep');
      const matches=[...rows.you_owe,...rows.waiting_on_them,...(rows.parked??[])].filter(row=>row.anchor_key===args.anchor);
      if(matches.length!==1 || matches[0]!.loop_version!==args.loop_version || matches[0]!.loop_identity!==args.loop_identity)throw new Error('Correction target changed');
      if(args.operation==='retarget')for(const [field,canonical] of [['text','what'],['contact','name'],['identifier','identifier']] as const) {
        const value=args[field];
        if(value && value!==matches[0]![canonical] && !admission.text.includes(value))throw new Error('Correction value is not an owner span');
      }
    }
    if(args.operation==='cleanup'){const binding=stableActionIdentity(eventId,ownerTaskToolKind,args);options.client.preflight({...admission,args,actionId:binding.actionId});}
    if(args.operation==='resolve_many'){
      if(!resolveManyOwnerTextMatches(args.items,admission.text,args.selection_mode))throw new ResolveManyClarification();
      // Validate each selection against the read projection; the receiver repeats this against raw peers.
      const rows=await options.client.readLoops(args.source_revision,context.signal);
      const all=[...rows.you_owe,...rows.waiting_on_them];
      for(const item of args.items){
        const selected=args.selection_mode==='named_people'?namedLoopSelection(item.name).person:item.name.toLocaleLowerCase('en-US');
        let matches=all.filter(row=>args.selection_mode==='owner_tasks'
          ? row.channel==='manual'&&ownerTaskTitleMatches(row.what,selected)
          : (()=>{const full=loopContactName(row.name);return full===selected||!explicitLoopThreadSelection(item.name)&&full.split(' ')[0]===selected;})());
        if(args.selection_mode==='named_people'){
          // Select a task only; the writer repeats this over every raw active peer.
          matches=matches.filter(row=>namedLoopDescriptionMatches(item.name,row.what,row.ask));
        }
        if(matches.length!==1||(args.selection_mode==='named_people'&&!namedLoopDescriptionMatches(item.name,matches[0]!.what,matches[0]!.ask))||matches[0]!.anchor_key!==item.anchor_key||matches[0]!.loop_version!==item.loop_version||matches[0]!.loop_identity!==item.loop_identity){
          if(!args.allow_partial)throw new ResolveManyClarification();
          delete item.anchor_key;delete item.loop_version;delete item.loop_identity;
        }
      }
      const binding=stableActionIdentity(eventId,ownerTaskToolKind,args);options.client.preflight({...admission,args,actionId:binding.actionId});
    }
    // The final awaited source check precedes synchronous current-run admission and the contact marker.
    await sourceCurrent(context);
    const request = journal.prepareOwnerTaskToolAction(authority, eventId, args);
    const saved = journal.readOwnerTaskToolAction(request.actionId, authority);
    if (saved.status === 'succeeded') return saved.preview?proposed(saved.preview):recorded(saved.receipt!);
    if (saved.status === 'rejected') return {status: 'rejected', code: saved.rejection};
    if (saved.status !== 'pending') throw new ActionHeldError();
    if(args.operation==='propose_relation'||args.operation==='propose_identifier'||args.operation==='propose_fact'||args.operation==='propose_cleanup'){
      const result=await options.client.call('preview',request,context.signal);
      await sourceCurrent(context);journal.ownerReminderToolAdmission(authority,eventId,true);
      if(result.status!=='preview'&&result.status!=='rejected')throw new Error('Relationship preview unavailable');
      journal.beginAction(request.actionId);journal.settleOwnerTaskToolAction(request.actionId,authority,result.status==='preview'?result:result.code);
      return result.status==='preview'?proposed(result):result.code==='similar_fact_exists'?{status:'rejected',code:result.code,saved:false,instruction:SIMILAR_FACT_TEXT}:{status:'rejected'};
    }
    journal.beginAction(request.actionId);
    try {
      const result = await options.client.call('apply', request, context.signal);
      if (result.status === 'committed') {
        journal.settleOwnerTaskToolAction(request.actionId, authority, result);
        return recorded(result);
      }
      if (result.status === 'rejected') {
        journal.settleOwnerTaskToolAction(request.actionId, authority, result.code);
        return {status: 'rejected', code: result.code};
      }
      throw new Error('Owner task receipt is unavailable');
    } catch (error) {
      const action = journal.getAction(request.actionId);
      if (action?.status === 'executing') journal.settleAction(request.actionId, 'unknown');
      const held=new ActionHeldError();
      if(args.operation==='cleanup'&&error instanceof CleanupDiagnosticError)held.message+=' '+error.message;
      throw held;
    }
  };
  const confirmRelation=async(context:OwnerTaskExecutionContext)=>{const {journal,authority,eventId}=trusted(context);if(context.allowOwnerTask!==true||typeof context.sourceRevision!=='string')throw new Error('Relationship confirmation authority unavailable');await sourceCurrent(context);return execute(journal.selectOwnerRelationProposal(authority,eventId,context.sourceRevision),context);};
  const confirmIdentifier=async(context:OwnerTaskExecutionContext)=>{const {journal,authority,eventId}=trusted(context);if(context.allowOwnerTask!==true||typeof context.sourceRevision!=='string')throw new Error('Identifier confirmation authority unavailable');await sourceCurrent(context);return execute(journal.selectOwnerIdentifierProposal(authority,eventId,context.sourceRevision),context);};
  const confirmFact=async(context:OwnerTaskExecutionContext)=>{const {journal,authority,eventId}=trusted(context);if(context.allowOwnerTask!==true||typeof context.sourceRevision!=='string')throw new Error('Fact confirmation authority unavailable');await sourceCurrent(context);return execute(journal.selectOwnerFactProposal(authority,eventId,context.sourceRevision),context);};
  const cleanup=async(items:unknown,context:OwnerTaskExecutionContext)=>{const {journal,authority,eventId}=trusted(context);if(context.allowOwnerTask!==true||typeof context.sourceRevision!=='string')throw new Error('Cleanup authority unavailable');await sourceCurrent(context);try{return await execute(journal.selectOwnerCleanupProposal(authority,eventId,context.sourceRevision,items),context);}catch(error){if(error instanceof OwnerTaskListUnavailable){if(typeof items==='object'&&items!==null&&'selection' in items&&(items.selection as {mode?:string})?.mode==='all'&&error.message==='Accepted cleanup list unavailable')return execute({operation:'propose_cleanup',scope:'open',source_revision:context.sourceRevision},context);return {status:'needs_clarification',message:OWNER_TASK_SELECTION_CLARIFICATION};}throw error;}};
  return {execute,confirmRelation,confirmIdentifier,confirmFact,cleanup};
}
