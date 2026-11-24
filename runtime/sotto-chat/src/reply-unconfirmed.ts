import { randomUUID } from 'node:crypto';
import { OWNER_ONLY_CONTEXT_PROOF, SourceAuthorityChanged } from './agent/source-authority.js';
import type { OperationalJournal, ReplyInput } from './journal/index.js';

type Row = Record<string, unknown>;

export const REPLY_UNCONFIRMED_NOTICE = 'My last reply may not have reached you. If you don’t see it, please ask again.';
/** A definitively rejected notice may be tried once more; a contacted one is never repeated. */
export const REPLY_UNCONFIRMED_MAX_NOTICES = 2;

const covered = (row: Row) => JSON.parse(row.covered_event_ids as string) as string[];

/**
 * An owner answer whose provider acceptance stayed unknown gets one plain notice on the thread's
 * next pass, and accepting that notice resolves the owner events the answer covered.
 * Returns a prepared notice to resume, the event ids a new notice must cover, or undefined.
 */
export function unconfirmedReplyStep(replies: Row[], open: ReadonlySet<string>): Row | string[] | undefined {
  const notices = replies.filter(row => row.purpose === 'reply_unconfirmed');
  const prepared = notices.find(row => row.dispatch_status === 'prepared' && covered(row).some(id => open.has(id)));
  if (prepared) return prepared;
  for (const reply of replies) {
    // Model answers only: every notice has a purpose, except legacy held-outcome notices.
    if (reply.purpose != null || reply.dispatch_status !== 'unknown' ||
        String(reply.payload).startsWith('I couldn’t confirm')) continue;
    const ids = covered(reply).filter(id => open.has(id));
    const prior = notices.filter(row => covered(row).some(id => ids.includes(id)));
    if (ids.length && prior.length < REPLY_UNCONFIRMED_MAX_NOTICES &&
        prior.every(row => row.dispatch_status === 'rejected')) return ids;
  }
  return undefined;
}

/** Reconcile an unconfirmed reply after giving an active source notice its delivery attempt. */
export async function reconcileUnconfirmedReply(journal: OperationalJournal, replies: Row[], open: ReadonlySet<string>,
  prepare: (input: Omit<ReplyInput, 'destination'>) => Row, post: (reply: Row) => Promise<unknown>): Promise<Row[]> {
  const step = unconfirmedReplyStep(replies, open);
  if (!step) return replies;
  if (!Array.isArray(step)) {
    if (journal.isRunCurrent(step.run_id as string)) await post(step);
    else journal.cancelPreparedDispatch(step.dispatch_id as string);
    return journal.listRepliesForThread(String(step.event_id));
  }
  // Starting a new run retires the current one. Deliver its prepared source notice first.
  const current = sourceNotices(replies).find(row => row.dispatch_status === 'prepared' &&
    covered(row).every(id => open.has(id)) && journal.isRunCurrent(String(row.run_id)));
  if (current) {
    await post(current);
    if (journal.getDispatch(String(current.dispatch_id))?.status === 'prepared' ||
        !step.every(id => journal.getInbound(id)?.outcome === 'pending')) return journal.listRepliesForThread(step[0]!);
  }
  const runId = randomUUID();
  journal.startRun({ runId, eventId: step[0]! });
  await post(prepare({ replyId: randomUUID(), runId, eventId: step[0]!, coveredEventIds: step, dispatchId: randomUUID(),
    kind: 'interactive', purpose: 'reply_unconfirmed', payload: REPLY_UNCONFIRMED_NOTICE,
    sourceRevision: OWNER_ONLY_CONTEXT_PROOF }));
  return journal.listRepliesForThread(step[0]!);
}

/** Recheck permission before first contact. A later input may supersede a still-unverifiable, never-contacted answer. */
export async function sourceCheckedReply(reply: Row, dispatch: Row,
  verify: (revision?: string, reportChange?: boolean) => Promise<boolean>,
  prepare: (input: ReplyInput) => Row, active: () => boolean, laterInput = false): Promise<Row | undefined> {
  let failure: SourceAuthorityChanged;
  try {
    if (await verify(typeof reply.source_revision === 'string' ? reply.source_revision : undefined, true)) return reply;
    if (!laterInput || typeof reply.source_revision !== 'string' || !/^[a-f0-9]{64}$/.test(reply.source_revision)) return undefined;
    failure = new SourceAuthorityChanged();
  } catch (error) {
    if (!(error instanceof SourceAuthorityChanged) || !error.confirmedChange) return undefined;
    failure = error;
  }
  if (!active()) return undefined;
  try {
    return prepare({replyId:randomUUID(), dispatchId:randomUUID(), kind:'interactive', purpose:'source_withheld',
      sourceRevision:OWNER_ONLY_CONTEXT_PROOF, runId:String(reply.run_id), eventId:String(reply.event_id),
      coveredEventIds:covered(reply), destination:String(dispatch.destination), payload:failure.notice, replacesUnsent:String(dispatch.id)});
  } catch { return undefined; } // The journal keeps ineligible or superseded work held.
}

/** Source notice disposition affects delivery. Its original inputs never become new model work. */
function sourceNotices(replies: Row[]): Row[] {
  const originals = new Set(replies.filter(row => row.purpose == null && row.dispatch_status === 'cancelled')
    .map(row => JSON.stringify([row.run_id,row.event_id,row.covered_event_ids])));
  return replies.filter(row => row.purpose === 'source_withheld' ||
    (row.purpose === 'model_failure' && row.source_revision === OWNER_ONLY_CONTEXT_PROOF &&
      originals.has(JSON.stringify([row.run_id,row.event_id,row.covered_event_ids]))));
}

function retiredInputs(replies: Row[]): Set<string> {
  return new Set(sourceNotices(replies).flatMap(covered));
}

/** The anchor on restart may be the oldest event. Use ordered, still-eligible input instead. */
export function hasLaterInput(pending: Row[], reply: Row, replies: Row[], available: ReadonlySet<string>, held: ReadonlySet<string>): boolean {
  const waiting = new Set(covered(reply)), retired = retiredInputs(replies);
  for (const row of pending) {
    const id = String(row.id);
    if (waiting.delete(id) || waiting.size || !available.has(id) || held.has(id) || retired.has(id)) continue;
    const text = JSON.parse(String(row.payload)).inbound.text?.trim().toLowerCase();
    if (text !== '/new' && text !== '/stop' && !text?.startsWith('/quiet')) return true;
  }
  return false;
}

export function replyWasReplaced(reply: Row, replies: Row[]): boolean {
  const retired = retiredInputs(replies);
  return covered(reply).every(id => retired.has(id));
}

export function freshReplyInputs(journal: OperationalJournal, eventId: string, replies: Row[]): Row[] {
  const pending = journal.listPendingInboundForThread(eventId);
  const open = reconcileTerminalReplies(journal,pending,replies), retired = retiredInputs(replies);
  return pending.filter(row => open.has(String(row.id)) && !retired.has(String(row.id)));
}

export function reconcileTerminalReplies(journal: OperationalJournal, pending: Row[], replies: Row[]): Set<string> {
  const pendingIds = new Set(pending.map(row => String(row.id)));
  for (const reply of replies) {
    const outcome = reply.dispatch_status === 'accepted' && reply.purpose !== 'outcome_pending' ? 'resolved' :
      reply.dispatch_status === 'rejected' && reply.purpose === 'source_withheld' ? 'cancelled' : undefined;
    if (!outcome) continue;
    for (const id of covered(reply)) if (pendingIds.delete(id)) journal.resolveInbound(id, outcome);
  }
  return pendingIds;
}
