import { createHash } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { basename } from 'node:path';
import { OperationalJournal, SCHEDULED_TEXT_MAX_ATTEMPTS, type DispatchKind } from './journal/index.js';
import type { ChannelTransport } from './channels/transport.js';
import type { Send } from './channels/types.js';
import type { GalleryBytesRequest, GalleryRequest, SendRequest } from './http.js';
import { routeDigest, type RouteIdentity } from './route-proof.js';

export interface Destination {
  readonly threadId: string;
  readonly transport: ChannelTransport;
}
type Row = Record<string, unknown>;
export interface AcceptanceReceipt {
  acceptance: 'accepted' | 'unknown' | 'rejected' | 'not_attempted';
  message_id?: string;
  /** Known accepted parts only; an unknown dispatch is still incomplete and never retried. */
  message_ids?: string[];
  route_proof_version?: 1;
  /** Present only when an accepted dispatch has an immutable resolved-route binding. */
  route_digest?: string;
  /** SHA-256 of the exact journaled delivery payload; paired with route_digest. */
  payload_hash?: string;
}

function receipt(row: Row): AcceptanceReceipt {
  const acceptance = row.status === 'accepted' ? 'accepted' : row.status === 'rejected' ? 'rejected' : 'unknown';
  const ids = JSON.parse(String(row.provider_message_ids ?? '[]')) as string[];
  if (acceptance !== 'accepted') return { acceptance,
    ...(acceptance === 'unknown' && ids.length ? {message_ids: ids} : {}) };
  const route = row.route_digest;
  if (route !== null && route !== undefined && !/^[a-f0-9]{64}$/u.test(String(route))) {
    throw new Error('stored dispatch route proof is invalid');
  }
  if (route !== null && route !== undefined && !/^[a-f0-9]{64}$/u.test(String(row.payload_hash))) {
    throw new Error('stored dispatch payload proof is invalid');
  }
  return { acceptance, message_id: String(row.provider_message_id), message_ids: ids,
    ...(route === null || route === undefined ? {} :
      { route_proof_version: 1 as const, route_digest: String(route),
        payload_hash: String(row.payload_hash) }) };
}

/** Scheduled text/gallery receipt owner; interactive replies use the coordinator's path. */
export class ReceiptedDelivery {
  private readonly routeIdentity: RouteIdentity;
  constructor(readonly journal: OperationalJournal, private readonly destinations: ReadonlyMap<string, Destination>,
    routeIdentity: RouteIdentity) {
    this.routeIdentity = Object.freeze({ ...routeIdentity });
  }

  private receiptForDispatch(row: Record<string, unknown>): AcceptanceReceipt {
    return this.journal.hasUnresolvedDispatchAttempt(String(row.id))
      ? receipt({...row, status: 'unknown'}) : receipt(row);
  }

  getReceipt(dispatchId: string): { found: boolean; receipt?: AcceptanceReceipt } {
    const row = this.journal.getDispatch(dispatchId);
    return row ? { found: true, receipt: this.receiptForDispatch(row) } : { found: false };
  }

  private async dispatch(dispatchId: string, destination: string, kind: DispatchKind, payload: string, send: Send, requestedAttempt: number, sourceRevision?: string): Promise<AcceptanceReceipt> {
    const target = this.destinations.get(destination);
    if (!target) return { acceptance: 'not_attempted' };
    const threadId = target.threadId;
    const digest = routeDigest(this.routeIdentity, threadId);
    const row = this.journal.prepareDispatch({ dispatchId, kind, destination, payload,
      routeDigest: digest, sourceRevision, maxAttempts: kind === 'scheduled_text' ? SCHEDULED_TEXT_MAX_ATTEMPTS : 1 });
    // A pre-v6 row has no historical resolved-route proof. Never infer one
    // from today's destination map or retry an old unknown outcome.
    if (row.route_digest === null) return this.receiptForDispatch(row);
    if (this.journal.hasUnresolvedDispatchAttempt(dispatchId)) return this.receiptForDispatch(row);
    if (row.status === 'accepted' || row.status === 'unknown' || row.status === 'dispatching') return this.receiptForDispatch(row);
    const currentAttempt = Number(row.current_attempt);
    if (requestedAttempt <= currentAttempt) return this.receiptForDispatch(row);
    // Synchronous durable transition occurs before any asynchronous provider call.
    const attempt = row.status === 'prepared'
      ? this.journal.beginDispatch(dispatchId, requestedAttempt)
      : this.journal.retryScheduledText(dispatchId, requestedAttempt);
    let outcome: AcceptanceReceipt;
    let providerSentAtMs: number | undefined;
    try {
      const sent = await target.transport.send({ ...send, threadId });
      const ids = sent.providerMessageIds.filter(id => typeof id === 'string' && id.length > 0 && id.length <= 512).slice(0, 8);
      if (sent.acceptance === 'accepted' && ids.length) {
        outcome = { acceptance: 'accepted', message_id: ids[0], message_ids: ids };
        providerSentAtMs = sent.providerSentAtMs;
      } else if (ids.length) {
        // A known accepted prefix is diagnostic evidence, never proof that the
        // complete dispatch succeeded or can safely be retried.
        outcome = {acceptance: 'unknown', message_ids: ids};
      } else outcome = { acceptance: sent.acceptance === 'rejected' || sent.acceptance === 'not_attempted' ? 'rejected' : 'unknown' };
    } catch { outcome = { acceptance: 'unknown' }; }
    const settled = this.journal.settleDispatch(dispatchId, Number(attempt.number), outcome.acceptance === 'not_attempted' ? 'rejected' : outcome.acceptance, outcome.message_id, outcome.message_ids, providerSentAtMs);
    return this.receiptForDispatch(settled);
  }

  sendText(request: SendRequest): Promise<AcceptanceReceipt> {
    return this.dispatch(request.dispatch_id, request.target, 'scheduled_text', JSON.stringify({ text: request.text }), { threadId: '', text: request.text }, request.attempt, request.source_revision);
  }

  async sendGallery(request: GalleryRequest): Promise<AcceptanceReceipt> {
    if (!this.destinations.has(request.target)) return { acceptance: 'not_attempted' };
    if (request.paths.length !== 4) return { acceptance: 'not_attempted' };
    const images = await Promise.all(request.paths.map(async path => {
      const bytes = await readFile(path);
      if (!bytes.length || bytes.length > 5_000_000) throw new Error('Gallery image size invalid');
      return { bytes, name: basename(path) };
    }));
    return this.sendGalleryBytes({ dispatch_id: request.dispatch_id, target: request.target,
      summary: request.summary, images });
  }

  sendGalleryBytes(request: GalleryBytesRequest): Promise<AcceptanceReceipt> {
    if (!this.destinations.has(request.target)) return Promise.resolve({ acceptance: 'not_attempted' });
    if (request.images.length !== 4) return Promise.resolve({ acceptance: 'not_attempted' });
    const images = request.images.map(image => {
      const bytes = Buffer.from(image.bytes);
      return { name: image.name, bytes, hash: createHash('sha256').update(bytes).digest('hex') };
    });
    const payload = JSON.stringify({ summary: request.summary, images: images.map(image => image.hash) });
    const media = images.map(image => ({ url: '', data: image.bytes, mimeType: 'image/png', filename: image.name, sizeBytes: image.bytes.length }));
    return this.dispatch(request.dispatch_id, request.target, 'gallery', payload, { threadId: '', text: request.summary, media }, 1);
  }
}
