import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import { ownerContinuityEndpoint, readOwnerContinuityJson } from './owner-continuity-client.js';

const boundedText = z.string().max(4_096);
export const reminderHandoff = 'reminded you twice — draft a follow-up or drop';
// Historical accepted cleanup reviews retain their original enum and rendered bytes.
export const legacyChaseHandoff = 'chased twice — nudge again or drop';
export const ownerRetuneRowSchema = z.object({
  anchor_key: boundedText,
  name: boundedText,
  what: z.string().max(400).refine(value => Array.from(value).length <= 200),
  direction: z.enum(['you_owe', 'waiting_on_them']),
  age_days: z.number().int().nonnegative().max(1_000_000).nullable(),
  times_surfaced: z.number().int().nonnegative().max(1_000_000),
  surface_count_provenance: z.enum(['accepted_delivery', 'unknown_legacy']),
  overdue: z.boolean(),
  deadline: z.string().regex(/^\d{4}-\d{2}-\d{2}$/).nullable(),
  chased_count: z.number().int().nonnegative().max(1_000_000),
  chased_out: z.boolean(),
  suggestion: z.enum(['do it or dismiss', 'nudge or drop', reminderHandoff, legacyChaseHandoff]),
}).strict();
const responseSchema = z.object({
  coverage: z.literal('owner_authored_only'),
  stale_loops: z.array(ownerRetuneRowSchema).max(1_024),
  mute_suggestions: z.array(z.never()).length(0),
  current: z.object({}).strict(),
  counts: z.object({ stale: z.number().int().nonnegative().max(1_024),
    mute_suggestions: z.literal(0) }).strict(),
}).strict();
export type OwnerRetune = z.infer<typeof responseSchema>;
export interface OwnerRetuneOptions { bridgeUrl: string; readBearer: string; fetch?: typeof fetch }
export class OwnerRetuneUnavailable extends Error {
  constructor() { super('Owner-only retune is unavailable; no conclusion about other obligations or preferences can be drawn.'); }
}

/** One authenticated receiver read. Empty owner-only results do not establish global absence. */
export async function readOwnerRetune(options: OwnerRetuneOptions, request: { signal?: AbortSignal } = {}): Promise<OwnerRetune> {
  ownerContinuityEndpoint(options, 'owner-retune');
  try {
    const parsed = responseSchema.safeParse(await readOwnerContinuityJson(options, 'owner-retune', {}, request.signal));
    if (!parsed.success || parsed.data.counts.stale !== parsed.data.stale_loops.length) throw new OwnerRetuneUnavailable();
    return parsed.data;
  } catch { throw new OwnerRetuneUnavailable(); }
}

/** The interactive chat resolver alone exposes this owner-only read. */
export function createOwnerRetuneTool(options: OwnerRetuneOptions) {
  ownerContinuityEndpoint(options, 'owner-retune');
  return createTool({
    id: 'owner_stale_loops',
    description: 'Read stale owner-authored obligations only. This is partial coverage; source-derived and legacy rows are withheld. No preferences or mutes are read or changed.',
    inputSchema: z.object({}).strict(),
    execute: async (_input, context) => {
      if (context.requestContext?.get('mode') !== 'chat' ||
          typeof context.requestContext?.get('runId') !== 'string' ||
          typeof context.requestContext?.get('ownerRequestKey') !== 'string') throw new OwnerRetuneUnavailable();
      const result = await readOwnerRetune(options, { signal: context.abortSignal });
      return { ...result, coverage_note: 'Only owner-authored stale loops are shown. Empty results do not prove there are no other obligations.' };
    },
  });
}
