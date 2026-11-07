/** Framework-neutral shared skill admission; model runtimes supply only trusted context. */
import type {OperationalJournal, ExplicitOwnerLoopAuthority} from '../journal/index.js';
import {readAuthority, SourceAuthorityChanged, type SourceAuthorityReader} from '../agent/source-authority.js';
import {ActionHeldError} from './action-gate.js';
import {FollowupAdmissionRequiredError, RelationshipPulseAdmissionRequiredError, ProactiveAdmissionRequiredError, ownerSkillRequestArgsSchema} from './owner-skill-request-client.js';
import type {OwnerSkillRequestClient} from './owner-skill-request-client.js';
export interface OwnerSkillExecutionContext {
  mode?: unknown; runId?: unknown; ownerRequestKey?: unknown; allowOwnerSkill?: unknown;
  sourceRevision?: unknown; signal?: AbortSignal;
}
export interface OwnerSkillExecutionOptions {
  client: OwnerSkillRequestClient;
  journal(): OperationalJournal | undefined;
  sourceAuthority: SourceAuthorityReader;
}
export function createOwnerSkillExecution(options: OwnerSkillExecutionOptions) {
  const trusted = (context: OwnerSkillExecutionContext) => {
    const runId = context.runId, eventId = context.ownerRequestKey;
    const journal = options.journal();
    if (!journal || context.mode !== 'chat' || typeof runId !== 'string' ||
        typeof eventId !== 'string' || context.signal?.aborted) throw new Error('Owner skill request authority is unavailable');
    const authority: ExplicitOwnerLoopAuthority = {runId, accountId: options.client.identity.account_id,
      ownerId: options.client.identity.owner_id, lineKey: options.client.identity.line_key,
      channel: options.client.identity.channel, scopeHash: options.client.scopeHash};
    return {journal, authority, eventId};
  };
  const sourceCurrent = async (context: OwnerSkillExecutionContext) => {
    const current = await readAuthority(options.sourceAuthority, context.signal);
    const revision = context.sourceRevision;
    if (context.signal?.aborted || (current.complete ? current.revision !== revision : revision !== undefined))
      throw new SourceAuthorityChanged();
  };
  const change = async (input: unknown, context: OwnerSkillExecutionContext) => {
    const {journal, authority, eventId} = trusted(context);
    if (context.allowOwnerSkill !== true) throw new Error('Owner skill request requires one owner message');
    await sourceCurrent(context);
    let args = ownerSkillRequestArgsSchema.parse(input);
    if (args.skill === 'ask-research')
      args = journal.resolveOwnerResearchSelection(authority, eventId, args, context.sourceRevision);
    journal.ownerReminderToolAdmission(authority, eventId, true);
    // The final awaited source check precedes synchronous current-run admission and the contact marker.
    await sourceCurrent(context);
    let request;
    try { request = journal.prepareOwnerSkillRequestAction(authority, eventId, args); }
    catch (error) {
      if (error instanceof ProactiveAdmissionRequiredError) return {status: 'rejected', code: 'needs_context', message: 'No proactive work was queued. Respond normally to the current owner message; proactive requires an affirmative urgent-check request.'};
      if (error instanceof RelationshipPulseAdmissionRequiredError || error instanceof FollowupAdmissionRequiredError) return {status: 'rejected', code: 'needs_context', message: 'The attempted workflow was not requested by the current owner message and nothing was queued. Answer the current request without the unrequested workflow heading, empty-result summary, or rejected-call narration. If the owner asks about prior workflow status or failure, explain that truthfully using available evidence.'};
      throw error;
    }
    const saved = journal.readOwnerSkillRequestAction(request.actionId, authority);
    if (saved.status === 'succeeded') return {status: 'queued', receipt: saved.receipt!};
    if (saved.status === 'rejected') return {status: 'rejected', code: saved.rejection};
    if (saved.status !== 'pending') throw new ActionHeldError();
    journal.beginAction(request.actionId);
    try {
      const result = await options.client.call('admit', request, context.signal);
      if (result.status === 'committed') {
        journal.settleOwnerSkillRequestAction(request.actionId, authority, result);
        return {status: 'queued', receipt: result};
      }
      if (result.status === 'rejected') {
        journal.settleOwnerSkillRequestAction(request.actionId, authority, result.code);
        return {status: 'rejected', code: result.code};
      }
      throw new Error('Owner skill request receipt is unavailable');
    } catch {
      const action = journal.getAction(request.actionId);
      if (action?.status === 'executing') journal.settleAction(request.actionId, 'unknown');
      throw new ActionHeldError();
    }
  };
  return {change};
}
