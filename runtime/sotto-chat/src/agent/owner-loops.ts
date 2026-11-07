import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import { ownerContinuityEndpoint, readOwnerContinuityJson } from './owner-continuity-client.js';

const boundedText = z.string().max(4_096);
const day = z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable();
export const ownerLoopEntrySchema = z.object({
  loop_version: z.string().regex(/^[a-f0-9]{64}$/).optional(),
  loop_identity: z.string().regex(/^[a-f0-9]{64}$/).optional(),
  canonical_id: boundedText.optional(),
  ask: z.string().max(400).refine(value=>Array.from(value).length<=200).optional(),
  name: boundedText, what: z.string().max(400).refine(value => Array.from(value).length <= 200), channel: boundedText,
  identifier: boundedText, thread_id: boundedText, group_id: boundedText,
  action_type: boundedText, age_days: z.number().int().nonnegative().max(1_000_000).nullable(),
  deadline: day, overdue: z.boolean(), anchor_key: boundedText,
  chased_count: z.number().int().nonnegative().max(1_000_000),
  chase_pending: day, last_chased_at: day, chase_after: day,
  chased_out: z.boolean(), handoff_asked_at: day,
}).strict();
const count = z.number().int().nonnegative().max(1_024);
const responseSchema = z.object({
  coverage: z.literal('owner_authored_only'),
  you_owe: z.array(ownerLoopEntrySchema).max(1_024),
  waiting_on_them: z.array(ownerLoopEntrySchema).max(1_024),
  parked: z.array(ownerLoopEntrySchema).max(1_024).optional(),
  counts: z.object({ you_owe: count, waiting_on_them: count, parked: count.optional() }).strict(),
}).strict();
export type OwnerLoops = z.infer<typeof responseSchema>;
export interface OwnerLoopsOptions { bridgeUrl: string; readBearer: string; fetch?: typeof fetch }
export class OwnerLoopsUnavailable extends Error {
  constructor() { super('Owner continuity is unavailable; no conclusion about open obligations can be drawn.'); }
}

/** One read-only receiver request. An empty owner-only projection is never global absence proof. */
export async function readOwnerLoops(options: OwnerLoopsOptions, request: { include_parked: boolean; signal?: AbortSignal }): Promise<OwnerLoops> {
  ownerContinuityEndpoint(options, 'owner-loops');
  if (typeof request.include_parked !== 'boolean') throw new OwnerLoopsUnavailable();
  try {
    const parsed = responseSchema.safeParse(await readOwnerContinuityJson(options, 'owner-loops',
      { include_parked: request.include_parked }, request.signal));
    if (!parsed.success || parsed.data.counts.you_owe !== parsed.data.you_owe.length ||
        parsed.data.counts.waiting_on_them !== parsed.data.waiting_on_them.length ||
        (request.include_parked && (parsed.data.parked === undefined || parsed.data.counts.parked !== parsed.data.parked.length)) ||
        (!request.include_parked && (parsed.data.parked !== undefined || parsed.data.counts.parked !== undefined))) {
      throw new OwnerLoopsUnavailable();
    }
    return parsed.data;
  } catch { throw new OwnerLoopsUnavailable(); }
}

/** Exposed only to the interactive chat resolver; it grants no source-backed rows. */
export function createOwnerLoopsTool(options: OwnerLoopsOptions, mode: 'chat' | 'unattended' = 'chat') {
  ownerContinuityEndpoint(options, 'owner-loops');
  return createTool({
    id: 'owner_open_loops',
    description: 'Read the current owner-authored-only open-loop projection. This is partial coverage: an empty list does not establish that the owner has no other obligations. Read-only; never changes a loop.',
    inputSchema: z.object({ include_parked: z.boolean().default(false) }).strict(),
    execute: async ({ include_parked }, context) => {
      if (context.requestContext?.get('mode') !== mode ||
          typeof context.requestContext?.get('runId') !== 'string' ||
          (mode === 'chat' && typeof context.requestContext?.get('ownerRequestKey') !== 'string')) throw new OwnerLoopsUnavailable();
      const result = await readOwnerLoops(options, { include_parked, signal: context.abortSignal });
      return { ...result, coverage_note: 'Only owner-authored rows are shown. Empty lists do not prove there are no other open obligations.' };
    },
  });
}
