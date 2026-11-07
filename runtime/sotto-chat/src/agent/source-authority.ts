import { constants } from 'node:fs';
import { open, rename, unlink } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { dirname } from 'node:path';
import { z } from 'zod';

export const BRIDGE_SOURCES = ['imessage', 'whatsapp', 'calls', 'whatsapp_calls', 'contacts',
  'reminders', 'apple_notes', 'chrome', 'safari', 'recent_files', 'screen_time'] as const;
export type BridgeSource = typeof BRIDGE_SOURCES[number];
export const CONNECTOR_SOURCES = ['granola'] as const;
export const GOOGLE_SOURCES = ['gmail', 'calendar', 'google_contacts'] as const;
const schema = z.object({ version: z.literal(2), revision: z.string().regex(/^[a-f0-9]{64}$/).nullable(),
  complete: z.boolean(), bridge_sources: z.array(z.enum(BRIDGE_SOURCES)).max(BRIDGE_SOURCES.length),
  google_sources: z.array(z.enum(GOOGLE_SOURCES)).max(GOOGLE_SOURCES.length),
  connector_sources: z.array(z.enum(CONNECTOR_SOURCES)).max(CONNECTOR_SOURCES.length).default([]) }).strict();
export type SourceAuthority = z.input<typeof schema>;
export interface SourceAuthorityReader { read(signal?: AbortSignal): Promise<SourceAuthority> }
export const OWNER_ONLY_CONTEXT_PROOF = 'owner-only-context-v1';
// A local baseline for configurations with no source reader. This grants no tools.
const OWNER_ONLY_BASELINE = createHash('sha256').update('sotto:owner-only-history-baseline:v2').digest('hex');
export const ownerOnlyAuthority = (): SourceAuthority => ({ version: 2, revision: OWNER_ONLY_BASELINE, complete: true, bridge_sources: [], google_sources: [], connector_sources: [] });
export const unavailableAuthority = (): SourceAuthority => ({ version: 2, revision: null, complete: false, bridge_sources: [], google_sources: [], connector_sources: [] });
export class SourceAuthorityChanged extends Error {
  constructor() { super('Source permissions changed or could not be verified; the result was withheld.'); }
}

/** One authenticated receiver route; no redirects, ambient credentials, or arbitrary remote URL. */
export function createSourceAuthorityReader(options: { bridgeUrl: string; bearer: string; fetch?: typeof fetch }): SourceAuthorityReader {
  const url = new URL(options.bridgeUrl);
  if (url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname) ||
      url.pathname !== '/mcp' || url.search || url.hash || url.username || url.password ||
      !options.bearer || /[\r\n]/.test(options.bearer)) throw new Error('Source authority configuration is invalid');
  url.pathname = '/runtime/source-authority';
  const transport = options.fetch ?? fetch;
  return { async read(signal) {
    try {
      const response = await transport(url, { method: 'POST', redirect: 'error',
        headers: { authorization: `Bearer ${options.bearer}`, 'content-type': 'application/json' }, body: '{}',
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(10_000)]) : AbortSignal.timeout(10_000) });
      if (!response.ok || !response.body) { await response.body?.cancel(); return unavailableAuthority(); }
      const reader = response.body.getReader();
      const chunks: Uint8Array[] = [];
      let size = 0;
      try {
        while (true) {
          const next = await reader.read();
          if (next.done) break;
          size += next.value.byteLength;
          if (size > 8192) { await reader.cancel(); return unavailableAuthority(); }
          chunks.push(next.value);
        }
      } finally { reader.releaseLock(); }
      const parsed = schema.safeParse(JSON.parse(Buffer.concat(chunks).toString('utf8')));
      if (!parsed.success || !parsed.data.complete || !parsed.data.revision ||
          new Set(parsed.data.bridge_sources).size !== parsed.data.bridge_sources.length ||
          new Set(parsed.data.google_sources).size !== parsed.data.google_sources.length ||
          new Set(parsed.data.connector_sources).size !== parsed.data.connector_sources.length) return unavailableAuthority();
      return parsed.data;
    } catch { return unavailableAuthority(); }
  } };
}

export async function readAuthority(reader: SourceAuthorityReader, signal?: AbortSignal): Promise<SourceAuthority> {
  let value: unknown;
  try { value = await reader.read(signal); } catch { return unavailableAuthority(); }
  const result = schema.safeParse(value);
  return result.success && result.data.complete && result.data.revision &&
    new Set(result.data.bridge_sources).size === result.data.bridge_sources.length &&
    new Set(result.data.google_sources).size === result.data.google_sources.length &&
    new Set(result.data.connector_sources).size === result.data.connector_sources.length ? result.data : unavailableAuthority();
}

/** Consent equality, not an access grant. Bridge availability may change with the same
 * receiver-verified consent revision. Incomplete authority still grants no source tools;
 * its stable owner-only fallback is preserved, while transitions remain fenced. */
export function sameConsent(a: SourceAuthority, b: SourceAuthority): boolean {
  return a.complete === b.complete && a.revision === b.revision &&
    [...a.google_sources].sort().join(',') === [...b.google_sources].sort().join(',') &&
    [...(a.connector_sources ?? [])].sort().join(',') === [...(b.connector_sources ?? [])].sort().join(',');
}

/** Bridge reads also require unchanged live source availability around the call. */
export function sameAuthority(a: SourceAuthority, b: SourceAuthority): boolean {
  return sameConsent(a, b) &&
    [...a.bridge_sources].sort().join(',') === [...b.bridge_sources].sort().join(',');
}

/**
 * This file records which authority proof the transcript was cleaned against. It
 * grants nothing. Delete history first, then commit the proof; a crash repeats the
 * deletion instead of treating stale history as clean. Caller serializes chat use.
 */
export type MemoryPurgeReason = 'authority_recovered' | 'authority_unavailable' | 'baseline_unavailable' | 'revision_changed' | 'authority_changed_during_turn';
export class ConversationSourceGuard {
  // Process-local evidence only; never persisted as consent or used to authorize recall.
  private lastPurge?: {reason:MemoryPurgeReason;atMs:number};
  purgeObservation() { return this.lastPurge ? {...this.lastPurge} : undefined; }
  constructor(private readonly options: { reader: SourceAuthorityReader; baselinePath: string; purge: () => Promise<void> }) {}

  private async baseline(): Promise<string | null> {
    let file;
    try {
      file = await open(this.options.baselinePath, constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
      const stat = await file.stat();
      if (!stat.isFile() || stat.size > 512 || (stat.mode & 0o077) !== 0 ||
          (process.getuid && stat.uid !== process.getuid())) return null;
      const row = JSON.parse(await file.readFile('utf8')) as { version?: unknown; revision?: unknown };
      return row.version === 1 && typeof row.revision === 'string' && /^[a-f0-9]{64}$/.test(row.revision) ? row.revision : null;
    } catch { return null; }
    finally { await file?.close(); }
  }

  private async cleaned(revision: string | null): Promise<void> {
    const temporary = `${this.options.baselinePath}.${randomUUID()}.tmp`;
    try {
      const file = await open(temporary, 'wx', 0o600);
      try { await file.writeFile(JSON.stringify({ version: 1, revision })); await file.sync(); }
      finally { await file.close(); }
      await rename(temporary, this.options.baselinePath);
      const directory = await open(dirname(this.options.baselinePath), 'r');
      try { await directory.sync(); } finally { await directory.close(); }
    } finally { await unlink(temporary).catch(() => undefined); }
  }

  async begin(signal?: AbortSignal): Promise<SourceAuthority> {
    // Independent reads: no purge or baseline write starts until both settle.
    const [authority, baseline] = await Promise.all([
      readAuthority(this.options.reader, signal), this.baseline(),
    ]);
    // Unknown is not a new permission revision. Callers suspend recall/projection until
    // verification returns; keep the last verified baseline to detect a real change then.
    if (!authority.complete) return authority;
    if (baseline !== authority.revision) {
      await this.options.purge();
      this.lastPurge = {reason:baseline === null ? 'baseline_unavailable' : 'revision_changed',atMs:Date.now()};
      await this.cleaned(authority.revision);
    }
    return authority;
  }

  async finish(expected: SourceAuthority, signal?: AbortSignal): Promise<void> {
    const current = await readAuthority(this.options.reader, signal);
    if (sameConsent(expected, current)) return;
    // No result is released across an uncertain check. Only a verified change can
    // destroy previously verified history; an outage never grants access to it.
    if (current.complete && (expected.complete || await this.baseline() !== current.revision)) {
      await this.options.purge();
      this.lastPurge = {reason:'authority_changed_during_turn',atMs:Date.now()};
      await this.cleaned(current.revision);
    }
    throw new SourceAuthorityChanged();
  }
}
