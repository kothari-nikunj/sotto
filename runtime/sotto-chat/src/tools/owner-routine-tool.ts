import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import type { OperationalJournal, ExplicitOwnerLoopAuthority } from '../journal/index.js';
import { readAuthority, SourceAuthorityChanged, type SourceAuthorityReader } from '../agent/source-authority.js';
import { stableActionIdentity, ActionHeldError } from './action-gate.js';
import { ownerRoutineToolArgsSchema, ownerRoutineToolKind, routineProposalArgsSchema, routineAdjustProposalSchema } from './owner-routine-tool-contract.js';
import type { OwnerRoutineToolClient } from './owner-routine-tool-client.js';
export function createOwnerRoutineTools(options: {
  client: OwnerRoutineToolClient;
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
      throw new Error('Owner routine authority is unavailable');
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
    if (context.abortSignal?.aborted || (current.complete ? current.revision !== revision : revision !== undefined))
      throw new SourceAuthorityChanged();
  };
  const execute = async (input: unknown, context: Context) => {
    const { journal, authority, eventId } = trusted(context);
    if (context.requestContext?.get('allowOwnerRoutine') !== true)
      throw new Error('Owner routine requires one owner message');
    const args = ownerRoutineToolArgsSchema.parse(input);
    journal.ownerReminderToolAdmission(authority,eventId,true);
    await sourceCurrent(context);
    journal.ownerReminderToolAdmission(authority, eventId, true);
    // The final awaited source check precedes synchronous current-run admission and the contact marker.
    await sourceCurrent(context);
    if (args.operation === 'propose' || args.operation === 'propose_adjust') {
      const admitted = journal.ownerReminderToolAdmission(authority, eventId, true);
      const binding = stableActionIdentity(eventId, ownerRoutineToolKind, args);
      const result = await options.client.call('preview', {
        ...admitted, actionId: binding.actionId, args
      }, context.abortSignal);
      await sourceCurrent(context);
      if (result.status !== 'preview' && result.status !== 'rejected')
        throw new Error('Routine proposal unavailable');
      const request = journal.prepareOwnerRoutineToolAction(authority, eventId, args, context.requestContext?.get('sourceRevision') as string);
      const saved = journal.readOwnerRoutineToolAction(request.actionId, authority);
      if (saved.status === 'succeeded')
        return {
          status: 'proposed'
        };
      if (saved.status === 'rejected')
        return {
          status: 'rejected', code: saved.rejection
        };
      journal.beginAction(request.actionId);
      journal.settleOwnerRoutineToolAction(request.actionId, authority, result.status === 'rejected' ? result.code : result);
      return result.status === 'rejected' ? {
        status: 'rejected', code: result.code
      } : {
        status: 'proposed'
      };
    }
    const request = journal.prepareOwnerRoutineToolAction(authority, eventId, args, context.requestContext?.get('sourceRevision') as string);
    const saved = journal.readOwnerRoutineToolAction(request.actionId, authority);
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
        journal.settleOwnerRoutineToolAction(request.actionId, authority, result);
        return {
          status: 'proposed'
        };
      }
      if (result.status === 'committed') {
        journal.settleOwnerRoutineToolAction(request.actionId, authority, result);
        return {
          status: 'recorded', outcome: result.outcome
        };
      }
      if (result.status === 'rejected') {
        journal.settleOwnerRoutineToolAction(request.actionId, authority, result.code);
        return {
          status: 'rejected', code: result.code
        };
      }
      throw new Error('Owner routine receipt is unavailable');
    }
    catch {
      const action = journal.getAction(request.actionId);
      if (action?.status === 'executing')
        journal.settleAction(request.actionId, 'unknown');
      throw new ActionHeldError();
    }
  };
  return {
    owner_routines: createTool({
      id: 'owner_routines', description: 'List only fresh personal routines; imported candidates and system jobs are excluded.', inputSchema: z.object({}).strict(), execute: async (_input, context) => {
        const { journal, authority, eventId } = trusted(context);
        await sourceCurrent(context);
        const admitted = journal.ownerReminderToolAdmission(authority, eventId);
        const binding = stableActionIdentity(eventId, ownerRoutineToolKind, null);
        const result = await options.client.call('list', {
          ...admitted, actionId: binding.actionId, args: null
        }, context.abortSignal);
        await sourceCurrent(context);
        if (result.status !== 'list' || !journal.isRunCurrent(authority.runId))
          throw new Error('Routine list unavailable');
        return {
          coverage: result.coverage, routines: result.routines
        };
      }
    }),
    owner_routine_propose: createTool({
      id: 'owner_routine_propose', description: 'Propose one owner-requested recurring read-only model-produced result, hourly, daily, every weekday (Monday–Friday), one weekday, an exact set of weekdays, or on the first day of every month at an explicit local time. Use schedule.weekday null for daily, integer0..6 for one weekday, an ascending unique array of integers0..6 for an exact weekday set (Sunday=0), exact "weekdays" for Monday–Friday, or exact "first_of_month" for the first day of every month. For every local hour use hour:null, weekday:null and an explicit minute; ordinary hourly means minute0, other offsets must be owner-stated. Clarify vague times. Scheduled work supports reminders, current canonical loops, bounded Gmail/Calendar and permitted local Bridge reads only. Do not propose unavailable web research, full prep/background workflows or mutations. Source availability is rechecked at execution. Use a standalone short paragraph; no external sends, system duplicates, or schedule recursion. The owner receives an exact proposal and must separately say yes before creation.', inputSchema: routineProposalArgsSchema.omit({
        operation: true
      }), execute: async (input, context) => execute({
        operation: 'propose', ...routineProposalArgsSchema.omit({
          operation: true
        }).parse(input)
      }, context)
    }),
    owner_routine_create: createTool({
      id: 'owner_routine_create', description: 'Create only the most recent accepted routine proposal when the owner separately says a clear yes. Takes no definition or authority arguments. A tweak requires a new proposal.', inputSchema: z.object({}).strict(), execute: async (_input, context) => {
        z.object({}).strict().parse(_input);
        const { journal, authority, eventId } = trusted(context);
        await sourceCurrent(context);
        const revision = context.requestContext?.get('sourceRevision');
        if (typeof revision !== 'string')
          throw new Error('Routine proposal source proof unavailable');
        return execute(journal.selectOwnerRoutineProposal(authority, eventId, revision), context);
      }
    }),
    owner_routine_adjust_propose:createTool({id:'owner_routine_adjust_propose',description:'Review a schedule adjustment to one exact personal routine selected from owner_routines, preserving task, zone and destination. Separate yes required.',inputSchema:routineAdjustProposalSchema.omit({operation:true}),execute:async(input,context)=>execute({operation:'propose_adjust',...routineAdjustProposalSchema.omit({operation:true}).parse(input)},context)}),
    owner_routine_adjust_confirm:createTool({id:'owner_routine_adjust_confirm',description:'Apply the latest accepted schedule adjustment after a separate clear yes.',inputSchema:z.object({}).strict(),execute:async(input,context)=>{z.object({}).strict().parse(input);const {journal,authority,eventId}=trusted(context);const revision=context.requestContext?.get('sourceRevision');if(typeof revision!=='string')throw new Error('Routine proposal source proof unavailable');return execute(journal.selectOwnerRoutineProposal(authority,eventId,revision,'adjust'),context);}}),
    owner_routine_cancel: createTool({
      id: 'owner_routine_cancel', description: 'Cancel one fresh personal routine explicitly requested by the owner. Read owner_routines and select its exact ID; clarify ambiguous targets. No additional confirmation required. Never cancel a system job or imported candidate.', inputSchema: z.object({
        routine_id: z.string().regex(/^[a-f0-9]{64}$/)
      }).strict(), execute: async (input, context) => execute({
        operation: 'cancel', ...z.object({
          routine_id: z.string().regex(/^[a-f0-9]{64}$/)
        }).strict().parse(input)
      }, context)
    }),
  };
}
