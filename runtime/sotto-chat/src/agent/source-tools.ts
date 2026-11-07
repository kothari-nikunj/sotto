import { SourceAuthorityChanged, readAuthority, sameAuthority, type SourceAuthority, type SourceAuthorityReader } from './source-authority.js';
import { BRIDGE_CONTENT } from './bridge-result.js';
import {z} from 'zod';

// Mirrors source_catalog.SOURCE_FIELDS; cross-language verification pins this projection.
export const SOURCE_FIELDS: Readonly<Record<string, readonly string[]>> = {
  imessage: ['imessage', 'deferred_unread_imessage'], whatsapp: ['whatsapp', 'deferred_unread_whatsapp'],
  calls: ['calls'], whatsapp_calls: ['whatsapp_calls'], contacts: ['contacts', 'contacts_total'],
  reminders: ['reminders'], apple_notes: ['apple_notes'], chrome: ['chrome_history', 'search_queries'],
  safari: ['safari_history', 'safari_search_queries'], recent_files: ['recent_files'], screen_time: ['screen_time'],
};

const SEARCH_SOURCES = ['imessage', 'whatsapp', 'apple_notes', 'recent_files'];

// Match source_catalog.valid_source_coverage: these negotiated receipts contain metadata,
// never free-form errors, identifiers or source content. Sparse legacy rows remain valid.
const coverageInstant=z.iso.datetime({offset:true}).max(64)
  .regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/);
const coverageCount=z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const coverageFailure=z.enum(['permission_denied','missing_source','locked','schema','query','decode',
  'timeout','busy','worker_failed']).nullable();
const orderedWindow=(value:{since:string|null;until:string|null})=>
  value.since===null||value.until===null||Date.parse(value.since)<=Date.parse(value.until);
const requestedWindow=z.object({since:coverageInstant,until:coverageInstant}).strict().refine(orderedWindow);
const effectiveWindow=z.object({since:coverageInstant.nullable(),until:coverageInstant.nullable(),
  basis:z.enum(['sent','modified','created','due_or_created','inventory','local_day','snapshot'])}).strict().refine(orderedWindow);
const coverageMeasures={
  requested_window:requestedWindow.optional(),effective_window:effectiveWindow.optional(),
  last_successful_read_at:coverageInstant.nullable().optional(),rows_returned:coverageCount.optional(),
  query_row_caps:z.array(coverageCount).max(32).refine(values=>new Set(values).size===values.length).optional(),
  omitted_rows:coverageCount.nullable().optional(),
};
const coverageComponent=z.object({...coverageMeasures,status:z.enum(['ok','partial','failed','unavailable']),
  failure_category:coverageFailure}).strict();
const sourceCoverage=z.object({
  observed_at:coverageInstant.optional(),
  status:z.enum(['ok','disabled','unavailable','degraded','needs_fda','partial','unread','empty','failed']).optional(),
  rows_scanned:coverageCount.optional(),scan_observed:z.boolean().optional(),limit_reached:z.boolean().optional(),
  content_partial:z.boolean().optional(),bounded:z.boolean().optional(),...coverageMeasures,
  failure_category:coverageFailure.optional(),
  components:z.object({deferred_unread:coverageComponent.optional(),search_queries:coverageComponent.optional(),
    first_active:coverageComponent.optional()}).strict().optional(),
  attachments:z.object({metadata:z.enum(['available','partial','unknown','not_read']),body:z.literal('not_read')}).strict().optional(),
}).strict();

export function sourceToolAvailable(name: string, authority: SourceAuthority): boolean {
  if (!authority.complete) return false;
  const sources = authority.bridge_sources;
  if (name === 'search_local') return sources.some(source => SEARCH_SOURCES.includes(source));
  if (name === 'health') return true;
  if (name === 'get_messages') return sources.includes('imessage');
  if (name === 'get_note') return sources.includes('apple_notes');
  if (name === 'get_contacts') return sources.includes('contacts');
  if (name === 'read_history') return sources.includes('imessage') || sources.includes('whatsapp');
  if (name === 'read_local') return sources.length > 0;
  return false; // Unknown server tools never acquire source authority implicitly.
}

function contentPresent(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0;
  if (value && typeof value === 'object') return Object.values(value).some(contentPresent);
  return Boolean(value);
}

/** Re-emit one bounded JSON payload, so a second content block cannot bypass projection. */
function sourceResult(name: string, result: unknown, authority: SourceAuthority, args: Record<string, unknown>): unknown {
  if (!result || typeof result !== 'object') throw new Error('Bridge source result is invalid');
  const row = result as Record<string, unknown>;
  if (row.isError === true) throw new Error('Bridge source read is unavailable');
  let payload: unknown;
  if ((result as Record<symbol, unknown>)[BRIDGE_CONTENT] !== undefined) payload = result;
  else if (row.structuredContent && typeof row.structuredContent === 'object') payload = row.structuredContent;
  else if (Array.isArray(row.content) && row.content.length === 1 && row.content[0]?.type === 'text') {
    try { payload = JSON.parse(String(row.content[0].text)); } catch { throw new Error('Bridge source result is invalid'); }
  } else throw new Error('Bridge source result is invalid');
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('Bridge source result is invalid');
  const encoded = JSON.stringify(payload);
  if (Buffer.byteLength(encoded) > 1_048_576) throw new Error('Bridge source result exceeds its bound');
  let visible = payload as Record<string, unknown>;
  if (name === 'search_local') {
    const sources = args.sources as string[];
    const fields = new Set(['status', 'reason', 'results', 'coverage', 'searched_at']);
    const resultFields = new Set(['source', 'source_id', 'title', 'timestamp', 'reference', 'author', 'partial', 'excerpt',
      'matched_fields']);
    const coverageFields = new Set(['status', 'indexed', 'skipped', 'truncated', 'stopped_early', 'newest_at',
      'complete', 'since', 'until']);
    if (Object.keys(visible).some(key => !fields.has(key)) || !Array.isArray(visible.results) ||
        visible.results.length > 20 || !visible.coverage || typeof visible.coverage !== 'object' || Array.isArray(visible.coverage) ||
        visible.results.some(result => !result || typeof result !== 'object' || !sources.includes(result.source) ||
          Object.keys(result).some(key => !resultFields.has(key))) ||
        Object.entries(visible.coverage).some(([source, state]) => !sources.includes(source) || !state ||
          typeof state !== 'object' || Array.isArray(state) || Object.keys(state).some(key => !coverageFields.has(key)))) {
      throw new SourceAuthorityChanged();
    }
  }
  if (name === 'read_local') {
    // The receiver adds this ordering marker after validating the Bridge reply.
    // It is internal provenance, not source content for the model. Reject an
    // invalid marker and project only this exact reserved field away.
    if (Object.hasOwn(visible, '_bridge_request_started_at')) {
      const marker = visible._bridge_request_started_at;
      if (typeof marker !== 'string' || marker.length < 1 || marker.length > 64) {
        throw new SourceAuthorityChanged();
      }
      visible = { ...visible };
      delete visible._bridge_request_started_at;
    }
    const coverage = visible.source_coverage ?? {};
    const requested = Array.isArray(args.sources) ? args.sources as string[] : authority.bridge_sources;
    if (!coverage || typeof coverage !== 'object' || Array.isArray(coverage) ||
        Object.entries(coverage).some(([source, value]) =>
          (source.startsWith('deferred_unread_') && !['deferred_unread_imessage', 'deferred_unread_whatsapp'].includes(source)) ||
          !requested.includes(source.replace(/^deferred_unread_/, '')) ||
          !sourceCoverage.safeParse(value).success)) {
      throw new SourceAuthorityChanged();
    }
    const permitted = new Set(['generated_at', 'window_hours', 'source_status', 'source_coverage',
      ...authority.bridge_sources.flatMap(source => SOURCE_FIELDS[source] ?? [])]);
    if (Object.entries(visible).some(([key, value]) => !permitted.has(key) && contentPresent(value))) {
      throw new SourceAuthorityChanged();
    }
  }
  return { content: [{ type: 'text', text: JSON.stringify(visible) }] };
}

export async function guardedSourceCall(options: {
  name: string; args: unknown; revision: string | undefined; reader: SourceAuthorityReader;
  signal?: AbortSignal; invoke: (args: unknown) => Promise<unknown>;
}): Promise<unknown> {
  const before = await readAuthority(options.reader, options.signal);
  if (!options.revision || before.revision !== options.revision || !sourceToolAvailable(options.name, before)) throw new SourceAuthorityChanged();
  if (!options.args || typeof options.args !== 'object' || Array.isArray(options.args)) throw new Error('Bridge source arguments are invalid');
  let args = options.args as Record<string, unknown>;
  if (options.name === 'search_local') {
    if (!Array.isArray(args.sources) || !args.sources.length || args.sources.length > 4 ||
        args.sources.some(source => !SEARCH_SOURCES.includes(source) || !before.bridge_sources.includes(source))) {
      throw new SourceAuthorityChanged();
    }
  }
  if (options.name === 'read_history' && !before.bridge_sources.includes(args.source as never)) throw new SourceAuthorityChanged();
  if (options.name === 'read_local') {
    const requested = args.sources;
    if (requested !== undefined && (!Array.isArray(requested) || requested.some(source => !before.bridge_sources.includes(source)))) throw new SourceAuthorityChanged();
    // Empty/omitted means "all" in the legacy API. Make its actual scope explicit.
    args = { ...args, sources: Array.isArray(requested) && requested.length ? requested : [...before.bridge_sources] };
  }
  if (options.signal?.aborted) throw new SourceAuthorityChanged();
  let result: unknown;
  try { result = await options.invoke(args); }
  catch { throw new Error('Bridge source read is unavailable'); }
  const after = await readAuthority(options.reader, options.signal);
  if (options.signal?.aborted || !sameAuthority(before, after)) throw new SourceAuthorityChanged();
  return sourceResult(options.name, result, after, args);
}
