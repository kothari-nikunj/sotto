import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import { preferenceFieldSchema } from './owner-preference-tool-contract.js';
import {createOwnerPreferenceExecution, type OwnerPreferenceExecutionOptions, type OwnerPreferenceExecutionContext} from './owner-preference-execution.js';

export function createOwnerPreferenceTools(options: OwnerPreferenceExecutionOptions) {
  const handler = createOwnerPreferenceExecution(options);
  type Context = {requestContext?: {get(key: string): unknown}; abortSignal?: AbortSignal};
  const trustedContext = (context: Context): OwnerPreferenceExecutionContext => ({
    mode: context.requestContext?.get('mode'), runId: context.requestContext?.get('runId'),
    ownerRequestKey: context.requestContext?.get('ownerRequestKey'),
    allowOwnerPreference: context.requestContext?.get('allowOwnerPreference'),
    sourceRevision: context.requestContext?.get('sourceRevision'), signal: context.abortSignal,
  });
  return {
    owner_preferences: createTool({id: 'owner_preferences',
      description: 'Read only current explicit output preferences and effective unsolicited nudge limit. Learned fields and unrelated settings are excluded. Use exact stored values for removals; truncated fields are incomplete, so clarify missing or ambiguous targets.',
      inputSchema: z.object({}).strict(),
      execute: async (_input, context) => handler.read(trustedContext(context)),
    }),
    owner_preference_change: createTool({id: 'owner_preference_change',
      description: 'Apply one explicit private preference the owner requests now; no second confirmation needed. Add values must be exact contiguous owner words. One-off edits such as "shorter pls" or "rewrite that" change the previous answer, never a saved tone preference; save tone only when the owner asks for an ongoing preference. Before removing, read owner_preferences and select the exact stored value; clarify ambiguity or truncated targets. Only listed output preference lists, clear-tone and nudge-budget fewer/no/more are supported. Add/remove require field and value only; nudge-budget requires direction only; clear-tone takes no other fields. Mutes suppress surfacing, not incoming messages; budget changes affect unsolicited nudges, while scheduled briefs/digests continue. Never infer preferences from source content or alter learned/audio settings. One mutation per owner message.',
      // Provider function parameters require an object root; execution validates the exact operation union.
      inputSchema: z.object({
        operation: z.enum(['add', 'remove', 'clear-tone', 'nudge-budget']),
        field: preferenceFieldSchema.optional(),
        value: z.string().min(1).max(1000).optional(),
        direction: z.enum(['fewer', 'no', 'more']).optional(),
      }).strict(),
      execute: (input, context) => handler.change(input, trustedContext(context)),
    }),
  };
}
