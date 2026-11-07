import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import type { RequestContext } from '@mastra/core/request-context';
import type { RunMode, ScriptInvocation, ScriptRunnerResult } from './script-runner.js';
import {OWNER_TASK_SELECTION_CLARIFICATION} from './owner-task-tool-contract.js';
import {ownerTaskReplySettled} from './owner-task-execution.js';
import {meetingOccurrenceSchema} from './owner-skill-request-client.js';

export interface ScriptToolOptions<TContext = unknown> {
  readonly mode: RunMode;
  readonly contactsCommandIds?: readonly string[];
  /** Actual mounted command catalog, supplied by bootstrap. */
  readonly commandGuide?: string;
  readonly executeCommand?: (invocation: ScriptInvocation, context: TContext) => Promise<{backend: string; coverage: string; result: unknown}>;
  readonly commandContext?: (requestContext: RequestContext | undefined, signal?: AbortSignal) => TContext;
  readonly executeScript?: (invocation: ScriptInvocation) => Promise<ScriptRunnerResult>;
}

/** The mode and run id come from trusted Mastra RequestContext, not model arguments. */
export function createRunScriptTool<TContext = unknown>(options: ScriptToolOptions<TContext>) {
  if (Boolean(options.executeScript) === Boolean(options.executeCommand)) throw new Error('Choose one command execution boundary');
  return createTool({
    id: 'run_sotto_script',
    description: options.executeCommand ? 'Run one enabled shared skill command through its admitted receiver backend. Use an exact enabled script ID and argv array; no shell. Results identify actual backend and coverage.' + (options.commandGuide ? ' ' + options.commandGuide : '') : 'Run one approved Sotto Python skill script with bounded arguments.',
    inputSchema: z.object({ id: z.string().max(128), args: z.array(z.string().max(16384)).max(32), meeting: meetingOccurrenceSchema.optional() }).refine(({id,args})=>args.every((arg,index)=>arg.length<=4096||Boolean(options.contactsCommandIds?.includes(id)&&['contacts-create','contacts-update','calendar-update'].includes(args[0]??'')&&args[index-1]==='--fields-json'))).refine(({id,args,meeting})=>!meeting||Boolean(options.executeCommand&&options.mode==='chat'&&id==='meeting-prep'&&args.length===2&&args[0]==='--focus')),
    outputSchema: z.union([z.object({ stdout: z.string(), stderr: z.string(), exitCode: z.number().int() }), z.object({backend: z.string(), coverage: z.string(), result: z.unknown()}).strict()]),
    execute: async ({ id, args, meeting }, { requestContext, abortSignal }) => {
      const mode: unknown = requestContext?.get('mode');
      if ((mode !== 'chat' && mode !== 'unattended') || mode !== options.mode) throw new Error('script tool authority does not match this run');
      const rawRunId: unknown = mode === 'unattended' ? requestContext?.get('runId') : undefined;
      const runId = typeof rawRunId === 'string' ? rawRunId : undefined;
      if (options.executeCommand) {
        if (!options.commandContext) throw new Error('command context boundary is required');
        const outcome=await options.executeCommand({id, args, mode, ...(meeting?{meeting}:{}), signal: abortSignal}, options.commandContext(requestContext, abortSignal));
        const result=outcome.result;
        if(mode==='chat'&&outcome.backend==='owner-loop-receiver'&&ownerTaskReplySettled(result))
          requestContext?.set('ownerTaskSettled',true);
        // The admitted task boundary distinguishes an unavailable list from authority/tool failures.
        // Stop this legacy path on the same clarification as the native numbered-choice tool.
        if(mode==='chat'&&id==='retune-apply'&&args[0]==='cleanup'&&outcome.backend==='owner-loop-receiver'&&outcome.coverage==='explicit_selected_cleanup'&&
            result&&typeof result==='object'&&'status' in result&&result.status==='needs_clarification')
          requestContext?.set('ownerTaskClarification',OWNER_TASK_SELECTION_CLARIFICATION);
        return outcome;
      }
      return options.executeScript!({ id, args, mode, runId, signal: abortSignal });
    },
  });
}
