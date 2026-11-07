import {ownerBridgeSendKind,bridgeSendReviewText} from './tools/owner-bridge-send-contract.js';
import type {OwnerBridgeSendClient} from './tools/owner-bridge-send-client.js';
import {delayedPolicy, delayedProofSettles} from './delayed-outcome-policy.js';
import {ownerContactsKind,contactsReviewText} from './tools/owner-contacts-contract.js';
import type {OwnerContactsClient} from './tools/owner-contacts-client.js';
import {taskReviewText,cleanupAcknowledgement,resolveManyAcknowledgement,SIMILAR_FACT_TEXT,ownerTaskReadback} from './tools/owner-task-tool-contract.js';
import {normalizeOwnerQuietText} from './tools/owner-preference-tool-contract.js';
import type {OwnerCalendarClient} from './tools/owner-calendar-client.js';
import {ownerCalendarKind,calendarReviewText,calendarWhenText} from './tools/owner-calendar-contract.js';
import { ManagedProxyAdmissionBusy, ManagedProxyBudgetExhausted, ManagedProxyRequestTooLarge, ManagedProxyUpstreamUnavailable } from './agent/models.js';
import {ownerWebReadKind,docsendReviewText,webReadArgsRevision,type OwnerWebReadClient} from './tools/owner-web-read.js';
import {ownerSkillRequestKind, skillAcknowledgement, skillRejectionAcknowledgement, type OwnerSkillRequestClient} from './tools/owner-skill-request-client.js';
import {ownerOfferReplyAction, offerReplySelectors, type OwnerOfferReplyClient} from './tools/owner-offer-reply-client.js';
import {clearMeetingPrepYes, meetingPrepSelectors, type OwnerMeetingPrepClient} from './tools/owner-meeting-prep-client.js';
import {ownerMeetingPrepOfferKind,ownerMeetingPrepOfferQuestion,type OwnerMeetingPrepOfferRefusal} from './tools/owner-meeting-prep-offer-contract.js';
import type {OwnerMeetingPrepOfferClient} from './tools/owner-meeting-prep-offer-client.js';
import {classifyRequestedBrief, isReadChatBriefRemainder} from './tools/requested-brief-classifier.js';
import type {OwnerBriefRequestClient} from './tools/owner-brief-request-client.js';
import type { OwnerGmailDraftClient } from './tools/owner-gmail-draft-client.js';
import { ownerGmailDraftKind, gmailDraftReviewText } from './tools/owner-gmail-draft-contract.js';
import type { OwnerRoutineToolClient } from './tools/owner-routine-tool-client.js';
import { ownerRoutineToolKind, routineAcknowledgement, routineProposalText } from './tools/owner-routine-tool-contract.js';
import { randomUUID } from 'node:crypto';
import type { Inbound, OwnerPolicy } from './channels/types.js';
import { normalizeInbound, truncateUtf16 } from './channels/normalize.js';
import { admitOwnerDirectMessage, PREP_OFFER_MAX_AGE_MS } from './channels/policy.js';
import { OperationalJournal, InboundAdmissionConflict, parseOwnerReminderCommand, parseOwnerQuietCommand } from './journal/index.js';
import type { ActivationIdentity } from './activation.js';
import type { OwnerLoopClient, OwnerLoopRequest, OwnerLoopCloseRequest } from './tools/owner-loop-client.js';
import type { OwnerReminderClient, OwnerReminderRequest } from './tools/owner-reminder-client.js';
import type { OwnerReminderToolClient } from './tools/owner-reminder-tool-client.js';
import type { OwnerTaskToolClient } from './tools/owner-task-tool-client.js';
import type { OwnerPreferenceToolClient } from './tools/owner-preference-tool-client.js';
import { ownerPreferenceToolKind, procedureReviewText, preferenceAcknowledgement } from './tools/owner-preference-tool-contract.js';
import { ownerTaskToolKind } from './tools/owner-task-tool-contract.js';
import { ownerReminderToolKind } from './tools/owner-reminder-tool-contract.js';
import type { OwnerQuietClient, OwnerQuietRequest } from './tools/owner-quiet-client.js';
import { OWNER_ONLY_CONTEXT_PROOF, SourceAuthorityChanged } from './agent/source-authority.js';
import { createHash } from 'node:crypto';

export interface ConversationContext {
  /** Provider thread identity, scoped internally by account, line and channel. */
  threadId: string;
  /** Exact sending destination resolved by the owner policy layer. */
  destination: string;
  lineKey: string;
  /** Trusted gateway identity candidate, never model-supplied. */
  activation?: ActivationIdentity;
}

export interface ModelRequest {
  historyInputIds?: readonly string[];
  text: string;
  acceptedBackgroundContext?: {text:string;sourceRevision:string};
  acceptedEditContext?: {text:string;sourceRevision:string};
  quotedReply?: {status:'found';messageId:string;text:string;sourceRevision:string}|{status:'unavailable'};
  acceptedProcedureReview?: {text:string;sourceRevision:'owner-only-context-v1'};
  recoveredWebReads?: Array<{sourceRevision:string|null;result:unknown}>;
  /** Full normalized owner messages, including bounded media descriptors/data. */
  messages: Inbound[];
  threadId: string;
  ownerId: string;
  runId: string;
  /** Journal-scoped first owner event, stable across replacement generations. */
  ownerRequestKey: string;
  allowOwnerReminder: boolean;
  allowOwnerTask: boolean;
  allowOwnerPreference: boolean;
  allowOwnerSkill?: boolean;
  allowOwnerRoutine: boolean;
  allowOwnerGmailDraft: boolean;
  allowOwnerMeetingPrepOffer?: boolean;
  signal: AbortSignal;
}

export interface PreparedPost {
  verbatim?: boolean;
  /** Stops future multipart contacts; an already contacted provider call may still settle. */
  signal?: AbortSignal;
  dispatchId: string;
  destination: string;
  text: string;
  runId: string;
}

export type PostReceipt = {
  acceptance: 'accepted' | 'rejected' | 'unknown' | 'not_attempted';
  messageId?: string;
  messageIds?: string[];
  providerSentAtMs?: number;
};

export interface ConversationDependencies {
  journal: OperationalJournal;
  routeDigestForThread?(threadId: string): string;
  resolvePrepContinuation?(input: {threadId: string; inboundId: string; inboundAtMs: number; candidateAtMs?: number}): Promise<{messageId: string; sentAtMs: number} | null>;
  ownerPolicy: OwnerPolicy;
  model: { generate(request: ModelRequest): Promise<string | { text: string; sourceRevision?: string }> };
  /** Provider-bound reads after admission; returned bytes are transient and never journaled. */
  hydrateInbound?(messages: Inbound[], options: { signal: AbortSignal; isCurrent(): boolean; threadId: string }): Promise<Inbound[]>;
  /** Best-effort presence starts only for a durable, current model run. */
  startTurnPresence?(threadId: string, runId: string, signal: AbortSignal, isCurrent: () => boolean): () => void;
  /** Best-effort provider feedback only for newly committed owner input. */
  onOwnerAdmission?(inbound: Inbound, threadId: string, isCurrent: () => boolean): void;
  onOwnerReplyAccepted?(inbound: Inbound, threadId: string): void;
  onOwnerSilentWorkAccepted?(inbound: Inbound, threadId: string): void;
  /** Durations only; dispatch acceptance is not proof the owner read the reply. */
  onOwnerTaskTiming?(timing: {acknowledgementMs:number;dispatchMs:number;receiptToDispatchMs:number;admissionToDispatchMs:number;outcome:ConversationResult['status']}): void;
  /** Repair native conversation memory from durable journal evidence; never send. */
  projectConversation?(threadId?: string): Promise<void>;
  canPostPrepared?(sourceRevision?: string): Promise<boolean>;
  /** A contacted control acknowledgement becomes unknown after this wait; the provider is never retried. */
  controlDeliveryDeadlineMs?: number;
  /** Provider-only send. The coordinator owns the durable prepare/begin/settle states. */
  postPrepared(request: PreparedPost): Promise<PostReceipt>;
  rotateSession?(threadId: string, throughEventId?: string): Promise<void>;
  /** Static private receiver capability; never exposed to Mastra tools. */
  ownerLoop?: OwnerLoopClient;
  ownerReminder?: OwnerReminderClient;
  ownerQuiet?: OwnerQuietClient;
  ownerReminderTool?: OwnerReminderToolClient;
  ownerTaskTool?: OwnerTaskToolClient;
  ownerPreferenceTool?: OwnerPreferenceToolClient;
  ownerSkillRequest?: OwnerSkillRequestClient;
  ownerWebRead?: OwnerWebReadClient;
  ownerRoutineTool?: OwnerRoutineToolClient;
  ownerGmailDraft?: OwnerGmailDraftClient;
  ownerCalendar?: OwnerCalendarClient;
  ownerContacts?:OwnerContactsClient;
  ownerBridgeSend?:OwnerBridgeSendClient;
  ownerBriefRequest?: OwnerBriefRequestClient;
  ownerMeetingPrep?: OwnerMeetingPrepClient;
  ownerMeetingPrepOffer?: OwnerMeetingPrepOfferClient;
  ownerOfferReply?: OwnerOfferReplyClient;
  /** Runs the mounted routine confirmation tool with the current trusted run context. */
  confirmSwipeRoutine?(request: {eventId:string;runId:string;sourceRevision:string;operation:'create'|'adjust';signal:AbortSignal}): Promise<void>;
}

export type ConversationResult =
  | { status: 'denied'; reason: string }
  | { status: 'duplicate' }
  | { status: 'accepted'; messageId: string }
  | { status: 'silent' | 'stopped' | 'rotated' | 'held' | 'cancelled' | 'queued' }
  | { status: 'error'; reason: string };

type SavedPayload = { inbound: Inbound; context: ConversationContext };

/** Mirrors the chat agent's per-turn text bound (agent/runtime.ts chatPrompt). */
export const MAX_MODEL_TURN_TEXT = 32_768;
const PARKED_INSPECT_INTERVAL_MS = 60_000;
/** Fixed /stop or /new acknowledgement dispatches after definite non-delivery; never after unknown contact. */
const CONTROL_ACK_MAX_DISPATCHES = 2;
const TRUNCATION_MARKER = '\n[Owner message truncated: it exceeded the chat length limit.]';
const HELD_NOTICE_SUFFIX = ' Please check it before trying again, and tell me what you find. You can keep chatting in the meantime.';
function isHeldOutcomeNotice(reply: Record<string, unknown>): boolean {
  const payload = reply.payload;
  if (reply.source_revision !== OWNER_ONLY_CONTEXT_PROOF || typeof payload !== 'string' ||
      typeof reply.event_id !== 'string') return false;
  try {
    const covered = JSON.parse(reply.covered_event_ids as string);
    if (!Array.isArray(covered) || covered.length !== 1 || covered[0] !== reply.event_id) return false;
  } catch { return false; }
  if (reply.purpose === 'outcome_pending' || reply.purpose === 'acknowledgement_unavailable') return true;
  // Read compatibility for already prepared notices from older releases only.
  return payload === `I couldn’t confirm that change.${HELD_NOTICE_SUFFIX}` ||
    ['the calendar change', 'the contact change', 'the Gmail action', 'the task change',
      'the reminder change', 'the preference change', 'that change'].some(subject =>
      payload === `I couldn’t confirm whether ${subject} happened.${HELD_NOTICE_SUFFIX}`);
}
function isDelayedOutcomeNotice(reply: Record<string, unknown>): boolean {
  return isHeldOutcomeNotice(reply) && (reply.purpose === 'outcome_pending' ||
    reply.purpose == null && typeof reply.payload === 'string' && reply.payload.startsWith('I couldn’t confirm'));
}


function encode(input: SavedPayload): string {
  return JSON.stringify(input, (_key, value) => value instanceof Uint8Array
    ? { __sotto_bytes: Buffer.from(value).toString('base64') } : value);
}
function decode(payload: string): SavedPayload {
  return JSON.parse(payload, (_key, value) => value && typeof value === 'object' &&
    typeof value.__sotto_bytes === 'string' ? Uint8Array.from(Buffer.from(value.__sotto_bytes, 'base64')) : value) as SavedPayload;
}

/** One in-process thread queue; the journal remains the sole recovery authority. */
export class ConversationCoordinator {
  private readonly queue = new Map<string, Promise<ConversationResult>>();
  private readonly active = new Map<string, { runId: string; controller: AbortController }>();
  /** Contacted replies that only an owner /stop in the same thread may release as unknown. */
  private readonly stopReleases = new Map<() => void, string>();
  private readonly freshFeedback = new Map<string, { eventId: string; inbound: Inbound; providerThreadId: string; providerSentAtMs?: number; backgroundProofChecked?: boolean }>();
  /** Threads whose latest turn deferred newer pending input to stay within the model bound. */
  private readonly deferredOverflow = new Set<string>();
  /** One wakeup for held exact action proofs, serialized by the existing thread queue. */
  private delayedRecoveryTimer: ReturnType<typeof setTimeout> | undefined;
  /** Delayed actions inspected once in this process before their pending-outcome notice exists. */
  private readonly delayedInspected = new Set<string>();
  /** Owner sources whose acknowledgement failed after a contacted reply; held alone until the time shown. */
  private readonly acknowledgementHeldUntil = new Map<string, number>();
  private acknowledgementHeldEpoch = 0;
  private closed = false;
  private suspended = false;
  private maintenance: Promise<unknown> | undefined;

  constructor(private readonly deps: ConversationDependencies) {}

  /** The returned promise may wait for work; admission itself commits synchronously before yielding. */
  receive(raw: Inbound, context: ConversationContext, providerSentAtMs?: number): Promise<ConversationResult> {
    if (this.closed) return Promise.resolve({ status: 'error', reason: 'coordinator unavailable' });
    const inbound = normalizeInbound(raw);
    const admission = admitOwnerDirectMessage(inbound, this.deps.ownerPolicy);
    if (!admission.admitted) return Promise.resolve({ status: 'denied', reason: admission.reason });
    if (!context.threadId || !context.destination || !context.lineKey) throw new Error('conversation context incomplete');
    let saved: ReturnType<OperationalJournal['admitInbound']>;
    try {
      saved = this.deps.journal.admitInbound({ eventId: inbound.eventId,
        accountKey: inbound.accountId, lineKey: context.lineKey, threadKey: context.threadId,
        channel: inbound.channel, sender: inbound.senderId, payload: encode({ inbound, context }), activation: context.activation,
        continuation: providerSentAtMs !== undefined && this.deps.routeDigestForThread
          ? {routeDigest: this.deps.routeDigestForThread(context.threadId), providerSentAtMs} : undefined });
    } catch (error) {
      // A changed redelivery of an admitted event, or a second owner chat after the
      // destination is bound, can never be admitted. Deny it so the provider can
      // acknowledge it; transient journal failures still throw (provider retry).
      if (error instanceof InboundAdmissionConflict) return Promise.resolve({ status: 'denied', reason: error.reason });
      throw error;
    }
    if (!saved.admitted) return Promise.resolve({ status: 'duplicate' });
    const eventId = saved.event.id as string;
    const threadKey = saved.event.thread_key as string;
    this.freshFeedback.set(threadKey, {eventId, inbound, providerThreadId: context.threadId, providerSentAtMs});
    try {
      this.deps.onOwnerAdmission?.(inbound, context.threadId,
        () => !this.closed && this.freshFeedback.get(threadKey)?.eventId === eventId);
    } catch { /* Provider feedback cannot affect admitted work. */ }
    const command = inbound.text?.trim().toLowerCase();
    // A correction or /stop signals the active model before it waits behind the queue.
    const active = this.active.get(threadKey);
    if (active) {
      active.controller.abort();
      this.deps.journal.cancelRun(active.runId);
    }
    if (command === '/stop') {
      // Only /stop releases a contacted send; a correction, close or maintenance awaits its receipt.
      for (const [release, key] of [...this.stopReleases]) if (key === threadKey) release();
      // Cancelling input processing says nothing about whether a contacted effect
      // happened. Its separate action/dispatch proof survives this transaction.
      this.deps.journal.stopThrough(eventId);
      void this.enqueue(threadKey, () => this.acknowledgeControl(eventId, context.destination,
        'I stopped working on your earlier request. An action already sent may still have happened; please check before retrying.')).catch(() => {
        // The pending stop is durable; activation recovery can prepare its acknowledgement.
      });
      return Promise.resolve({status: 'stopped'});
    }
    // Maintenance pauses execution, not durable intake. A non-awaiting provider must
    // never lose a received owner event merely because transcript rotation is running.
    if (this.suspended) return Promise.resolve({ status: 'queued' });
    if (command === '/new') {
      return this.enqueue(threadKey, () => this.processThroughRotation(eventId));
    }
    if (this.deps.ownerLoop && (inbound.text?.startsWith('/loop add ') || inbound.text?.startsWith('/loop close '))) {
      return this.enqueue(threadKey, () => this.processThroughRotation(eventId));
    }
    if (this.deps.ownerReminder && inbound.text?.startsWith('/reminder')) {
      return this.enqueue(threadKey, () => this.processThroughRotation(eventId));
    }
    if (this.deps.ownerBriefRequest && classifyRequestedBrief(inbound)) {
      return this.enqueue(threadKey, () => this.processThroughRotation(eventId));
    }
    if (this.deps.ownerMeetingPrep && meetingPrepSelectors(inbound)
      && !this.isLocalProposalReply(eventId, inbound.replyToMessageId!)) {
      const selected=meetingPrepSelectors(inbound)!;
      return this.enqueue(threadKey, async () => {
        const messageId=selected.selected_offer?.message_id??selected.reply_to_message_id??undefined;
        if (!(await this.repairAcceptedMeetingPrepOffers(threadKey,messageId)))
          return this.heldOwnerAcknowledgement(eventId,undefined,
            'I couldn’t verify the meeting prep request just now, so I haven’t started preparing it. Please ask me directly to prepare it again.');
        return this.processThroughRotation(eventId);
      });
    }
    if (this.deps.ownerOfferReply && offerReplySelectors(inbound)
      && !this.isLocalProposalReply(eventId, inbound.replyToMessageId!)) {
      return this.enqueue(threadKey, () => this.processThroughRotation(eventId));
    }
    if (this.deps.ownerQuiet && inbound.text?.startsWith('/quiet')) {
      return this.enqueue(threadKey, () => this.processThroughRotation(eventId));
    }
    // A newly admitted safe turn can arrive behind an unknown owner command. If exact
    // inspect later proves it, drain its fixed acknowledgement before the safe
    // turn; if proof is still absent, process only the new turn.
    // An action whose source a /stop already closed has no acknowledgement left to drain.
    // A late final reply not yet delivered is drained the same way, then the new turn runs.
    if ((this.deps.ownerLoop || this.deps.ownerReminder || this.deps.ownerQuiet || this.deps.ownerReminderTool || this.deps.ownerTaskTool || this.deps.ownerPreferenceTool || this.deps.ownerSkillRequest || this.deps.ownerWebRead || this.deps.ownerRoutineTool || this.deps.ownerGmailDraft || this.deps.ownerCalendar || this.deps.ownerContacts || this.deps.ownerBridgeSend || this.deps.ownerBriefRequest || this.deps.ownerMeetingPrep || this.deps.ownerMeetingPrepOffer || this.deps.ownerOfferReply) && (this.deps.journal.listUnknownActionsForThread(eventId)
      .some(row => Boolean(delayedPolicy(row.kind)) &&
        (typeof row.source_event_id !== 'string' || this.deps.journal.getInbound(row.source_event_id)?.outcome !== 'cancelled' &&
          this.deps.journal.getInbound(row.source_event_id)?.outcome !== 'resolved')) ||
        this.delayedRecoverySources(eventId, 'route').size > 0)) {
      return this.enqueue(threadKey, () => this.processThroughRotation(eventId));
    }
    return this.enqueue(threadKey, () => this.processThreadWithOverflow(eventId, threadKey));
  }

  /** A size-capped turn left newer owner input pending; drain it now rather than at the next message. */
  private async processThreadWithOverflow(eventId: string, threadKey: string): Promise<ConversationResult> {
    let result = await this.processThread(eventId);
    for (let i = 0; i < 100 && this.deferredOverflow.delete(threadKey) &&
        (result.status === 'accepted' || result.status === 'silent'); i++) {
      const next = await this.processThread(eventId);
      if (next.status !== 'silent') result = next;
    }
    return this.followDelayedNotice(eventId, result);
  }

  /** Pending owner input that only a held uncertain action or contacted reply keeps open. */
  private onlyHeldRemain(eventId: string, quietAware = false): boolean {
    const remaining = this.deps.journal.listPendingInboundForThread(eventId);
    const heldCommandIds = new Set(this.deps.journal.listUnknownActionsForThread(eventId)
      .filter(row => Boolean(delayedPolicy(row.kind)))
      .map(row => row.source_event_id as string));
    for (const reply of this.deps.journal.listRepliesForThread(eventId)) {
      if (reply.purpose !== 'owner_control' || !['unknown', 'dispatching'].includes(reply.dispatch_status as string)) continue;
      for (const id of JSON.parse(reply.covered_event_ids as string) as string[]) heldCommandIds.add(id);
    }
    const now = Date.now();
    for (const [id, until] of this.acknowledgementHeldUntil) if (until > now) heldCommandIds.add(id);
    return remaining.length > 0 && remaining.every(row => heldCommandIds.has(row.id as string)) &&
      (!quietAware || !remaining.some(row => decode(row.payload as string).inbound.text?.startsWith('/quiet')) ||
        this.deps.journal.listUnknownActionsForThread(eventId).some(row => row.kind === 'owner_quiet_admitted'));
  }

  /** True when a held delayed-policy action may give its bounded pending-outcome notice now. */
  private delayedNoticeDue(eventId: string): boolean {
    const pendingIds = new Set(this.deps.journal.listPendingInboundForThread(eventId).map(row => row.id as string));
    if (!pendingIds.size) return false;
    const replies = this.deps.journal.listRepliesForThread(eventId);
    return this.deps.journal.listUnknownActionsForThread(eventId).some(action => {
      const policy = delayedPolicy(action.kind);
      const sourceId = action.source_event_id as string;
      if (!policy || action.status !== 'unknown' || !pendingIds.has(sourceId) ||
          this.deps.journal.getRun(action.run_id as string)?.event_id !== sourceId) return false;
      const notices = replies.filter(reply => reply.event_id === sourceId && isDelayedOutcomeNotice(reply));
      if (notices.some(reply => reply.dispatch_status !== 'rejected') || notices.length >= policy.maxNoticeAttempts) return false;
      const latest = notices.at(-1);
      return !latest || Date.now() - Number(latest.dispatch_updated_at) >= policy.inspectIntervalMs;
    });
  }

  /**
   * A fresh safe turn goes first; once only held input remains, give a due
   * pending-outcome notice in the same turn instead of waiting for recovery.
   * One extra pass at most: the notice itself is bounded by the delayed policy.
   */
  private async followDelayedNotice(eventId: string, result: ConversationResult): Promise<ConversationResult> {
    if (this.closed || this.suspended || !['accepted', 'silent', 'rotated', 'held'].includes(result.status) ||
        !this.onlyHeldRemain(eventId) || !this.delayedNoticeDue(eventId)) return result;
    try { await this.processThread(eventId); }
    catch { /* The notice stays due; recovery or the next owner turn can give it. */ }
    return result;
  }

  private holdAcknowledgementSource(eventId: string): void {
    if (this.acknowledgementHeldUntil.size >= 1024) this.acknowledgementHeldUntil.clear();
    this.acknowledgementHeldUntil.set(eventId, Date.now() + PARKED_INSPECT_INTERVAL_MS);
    this.acknowledgementHeldEpoch++;
  }

  private clearDelayedRecovery(): void {
    if (this.delayedRecoveryTimer) clearTimeout(this.delayedRecoveryTimer);
    this.delayedRecoveryTimer = undefined;
  }

  private scheduleDelayedRecovery(): void {
    if (this.closed || this.suspended) { this.clearDelayedRecovery(); return; }
    let relevant: boolean;
    try {
      const threads = new Map<string, string>();
      for (const row of this.deps.journal.listPendingInbound())
        if (!threads.has(row.thread_key as string)) threads.set(row.thread_key as string, row.id as string);
      relevant = [...threads.values()].some(eventId => this.delayedRecoverySources(eventId, 'wake').size > 0);
    } catch {
      // Scheduling is advisory; a failed scan never changes the durable effect hold.
      this.clearDelayedRecovery();
      return;
    }
    if (!relevant) { this.clearDelayedRecovery(); return; }
    if (this.delayedRecoveryTimer || this.queue.size) return;
    // Even an unreadable held proof costs at most one recovery wakeup per minute.
    // Kind-specific inspectors persist their inspection start before provider contact.
    this.delayedRecoveryTimer = setTimeout(() => {
      this.delayedRecoveryTimer = undefined;
      if (this.closed || this.suspended) return;
      void this.recoverDelayedProofs().catch(() => undefined).finally(() => this.scheduleDelayedRecovery());
    }, PARKED_INSPECT_INTERVAL_MS);
    this.delayedRecoveryTimer.unref?.();
  }

  /**
   * Original pending requests that delayed-outcome work may finish: an unknown
   * delayed action, or a settled one whose notice was given but whose final reply
   * was never contacted. A contacted (accepted, unknown or dispatching) final reply
   * is never resent; a definite non-delivery is retried by the wakeup a bounded
   * number of times. 'route' ignores cadence and that bound so owner input drains it.
   */
  private delayedRecoverySources(eventId: string, use: 'route' | 'wake' | 'due'): Set<string> {
    const replies = this.deps.journal.listRepliesForThread(eventId);
    const sources = new Set<string>();
    for (const action of this.deps.journal.listDelayedActionsForPendingSources(eventId)) {
      const sourceId = action.source_event_id as string, policy = delayedPolicy(action.kind)!;
      if (action.status === 'unknown') {
        if (use !== 'due' || Date.now() - Number(action.updated_at) >= policy.inspectIntervalMs) sources.add(sourceId);
        continue;
      }
      if (!['succeeded', 'rejected'].includes(action.status as string) ||
          !replies.some(reply => reply.event_id === sourceId && isDelayedOutcomeNotice(reply))) continue;
      const finals = replies.filter(reply => !isDelayedOutcomeNotice(reply) &&
        (JSON.parse(reply.covered_event_ids as string) as string[]).includes(sourceId));
      if (finals.some(reply => ['accepted', 'unknown', 'dispatching'].includes(reply.dispatch_status as string))) continue;
      if (use !== 'route' && finals.filter(reply => ['rejected', 'cancelled'].includes(reply.dispatch_status as string)).length >=
          policy.maxNoticeAttempts) continue;
      sources.add(sourceId);
    }
    return sources;
  }

  private async recoverDelayedProofs(): Promise<void> {
    if (this.closed || this.suspended) return;
    const threads = new Map<string, string>();
    for (const row of this.deps.journal.listPendingInbound()) threads.set(row.thread_key as string, row.id as string);
    await Promise.all([...threads].map(([threadKey, eventId]) => this.enqueue(threadKey, async () => {
      for (const sourceId of this.delayedRecoverySources(eventId, 'due')) await this.processThread(eventId, new Set([sourceId]));
      return {status: 'held'};
    })));
  }

  /** Call after supervisor ownership and journal.recoverInterrupted(), before opening intake. */
  async recover(): Promise<ConversationResult[]> {
    if (this.closed || this.suspended) return [];
    try { await this.deps.projectConversation?.(); }
    catch { /* Startup can open the receiver; the next model turn requires successful repair. */ }
    if(this.deps.ownerMeetingPrepOffer){
      const controller=new AbortController();const deadline=setTimeout(()=>controller.abort(),5000);
      try{await Promise.all(this.deps.journal.listPendingOwnerMeetingPrepOffers().map(action=>this.enqueue(action.thread_key as string,async()=>{
        await this.recordAcceptedMeetingPrepOffer(action.id as string,controller.signal);
        return {status:'silent'};
      })));}finally{clearTimeout(deadline);}
    }
    if(this.deps.ownerGmailDraft){
      const authority=this.ownerGmailDraftAuthority();
      const controller=new AbortController();const deadline=setTimeout(()=>controller.abort(),5000);
      try{await Promise.all([
        ...this.deps.journal.listPendingGmailOfferedPairs().map(action=>this.enqueue(action.thread_key as string,async()=>{
          await this.recordAcceptedGmailPair(action.id as string,controller.signal);
          return {status:'silent'};
        })),
        ...this.deps.journal.listPendingGmailDraftLearning().map(action=>this.enqueue(action.thread_key as string,async()=>{
        if(this.closed||this.suspended||controller.signal.aborted)return {status:'held'};
        try{
          const saved=this.deps.journal.readOwnerGmailDraftAction(action.id as string,authority);
          if(saved.receipt?.learning==='pending'){
            const inspected=await this.deps.ownerGmailDraft!.call('inspect',saved.request,controller.signal);
            if(!this.closed&&!this.suspended&&!controller.signal.aborted&&inspected.status==='committed'&&inspected.learning==='recorded')this.deps.journal.refreshOwnerGmailDraftLearning(action.id as string,authority,inspected);
          }
        }catch{/* Pending local learning never reopens or retries the primary Gmail effect. */}
        return {status:'silent'};
      }))]);}finally{clearTimeout(deadline);}
    }
    if(this.closed||this.suspended)return [];
    const firstByThread = new Map<string, string>();
    for (const event of this.deps.journal.listPendingInbound()) {
      const threadKey = event.thread_key as string;
      if (!firstByThread.has(threadKey)) firstByThread.set(threadKey, event.id as string);
    }
    return Promise.all([...firstByThread].map(([threadKey, eventId]) =>
      this.enqueue(threadKey, async () => {
        let last: ConversationResult = { status: 'silent' };
        for (let i = 0; i < 100; i++) {
          const before = this.deps.journal.listPendingInboundForThread(eventId).length;
          if (!before) break;
          last = await this.processThread(eventId);
          const after = this.deps.journal.listPendingInboundForThread(eventId).length;
          if (!after || (after >= before && last.status !== 'accepted') || ['held', 'error', 'cancelled'].includes(last.status)) break;
        }
        return last;
      })));
  }

  /** Stop new admission, cancel running model turns, then wait for contacted sends to settle. */
  async close(): Promise<void> {
    this.closed = true;
    this.clearDelayedRecovery();
    for (const active of this.active.values()) {
      active.controller.abort();
      this.deps.journal.cancelRun(active.runId);
    }
    await this.maintenance?.catch(() => undefined);
    await this.drain();
  }

  /** Fence execution while preserving newly admitted input until maintenance completes. */
  exclusive<T>(work: () => Promise<T>): Promise<T> {
    if (this.closed || this.suspended) return Promise.reject(new Error('coordinator unavailable'));
    this.suspended = true;
    this.clearDelayedRecovery();
    const operation = (async () => {
      try {
        for (const active of this.active.values()) {
          active.controller.abort();
          this.deps.journal.cancelRun(active.runId);
        }
        await this.drain();
        return await work();
      } finally {
        this.suspended = false;
        // recover() installs its queued work synchronously. close()/drain() can join it.
        if (!this.closed) void this.recover().catch(() => undefined);
      }
    })();
    this.maintenance = operation;
    const clear = () => { if (this.maintenance === operation) this.maintenance = undefined; };
    void operation.then(clear, clear);
    return operation;
  }

  async drain(): Promise<void> {
    while (this.queue.size) await Promise.allSettled([...this.queue.values()]);
  }

  private async processThroughRotation(eventId: string): Promise<ConversationResult> {
    return this.followDelayedNotice(eventId, await this.processThroughRotationUntilHeld(eventId));
  }

  private async processThroughRotationUntilHeld(eventId: string): Promise<ConversationResult> {
    for (let i = 0; i < 100; i++) {
      const before = this.deps.journal.listPendingInboundForThread(eventId).length;
      if (!before) return { status: 'silent' };
      const result = await this.processThread(eventId);
      if (result.status === 'rotated' || result.status === 'stopped' || ['held', 'error', 'cancelled'].includes(result.status)) return result;
      if (result.status === 'accepted' && this.onlyHeldRemain(eventId, true)) return result;
      if (this.deps.journal.listPendingInboundForThread(eventId).length >= before && result.status !== 'accepted') return result;
    }
    return { status: 'held' };
  }

  private enqueue(threadKey: string, work: () => Promise<ConversationResult>): Promise<ConversationResult> {
    const prior = this.queue.get(threadKey);
    const next = (prior ?? Promise.resolve({ status: 'silent' } as ConversationResult))
      .catch(() => ({ status: 'error', reason: 'previous turn failed' } as ConversationResult))
      .then(work);
    this.queue.set(threadKey, next);
    const cleanup = () => {
      if (this.queue.get(threadKey) === next) this.queue.delete(threadKey);
      this.scheduleDelayedRecovery();
    };
    void next.then(cleanup, cleanup);
    return next;
  }

  /** A failed acknowledgement holds only its own source; other pending input is then processed. */
  private async processThread(eventId: string, delayedSources?: ReadonlySet<string>): Promise<ConversationResult> {
    for (let i = 0; ; i++) {
      const epoch = this.acknowledgementHeldEpoch;
      const result = await this.processThreadOnce(eventId, delayedSources);
      if (i >= 3 || result.status !== 'held' || epoch === this.acknowledgementHeldEpoch) return result;
    }
  }

  private async processThreadOnce(eventId: string, delayedSources?: ReadonlySet<string>): Promise<ConversationResult> {
    if (this.closed || this.suspended) return { status: 'queued' };
    let pending = this.deps.journal.listPendingInboundForThread(eventId);
    if (pending.length === 0) return { status: 'silent' };
    if (!delayedSources) this.deferredOverflow.delete(pending[0]!.thread_key as string);
    // A crash can occur after durable command admission but before its cancellation
    // transaction. Replay commands before any model work, preserving later arrivals.
    const stop = pending.findLast(row => decode(row.payload as string).inbound.text?.trim().toLowerCase() === '/stop');
    let heldStopId: string | undefined;
    if (stop && delayedSources) return {status: 'held'};
    if (stop) {
      this.deps.journal.stopThrough(stop.id as string);
      const command = decode(stop.payload as string);
      const acknowledgement = await this.acknowledgeControl(stop.id as string, command.context.destination,
        'I stopped working on your earlier request. An action already sent may still have happened; please check before retrying.');
      if (acknowledgement.status === 'held') heldStopId = stop.id as string;
      else if (acknowledgement.status !== 'stopped') return acknowledgement;
      pending = this.deps.journal.listPendingInboundForThread(eventId);
      if (!pending.some(row => row.id !== heldStopId)) return acknowledgement;
    }
    const replies = this.deps.journal.listRepliesForThread(eventId);
    const pendingIds = new Set(pending.map(row => row.id as string));
    // Reconcile provider acceptance before doing more work, including when the anchor was resolved.
    for (const reply of replies) {
      if (reply.dispatch_status !== 'accepted' || reply.purpose === 'outcome_pending') continue;
      const covered = JSON.parse(reply.covered_event_ids as string) as string[];
      for (const id of covered) if (pendingIds.has(id)) this.deps.journal.resolveInbound(id, 'resolved');
    }
    const stillPending = this.deps.journal.listPendingInboundForThread(eventId);
    if (!stillPending.length) return { status: 'silent' };
    const heldCommandIds = new Set<string>(heldStopId ? [heldStopId] : []);
    // An owner action whose source event a later /stop (or resolution) already closed
    // can never be acknowledged or replayed: no model turn will run for that event again.
    // It stays recorded and is still inspected, so late proof settles it, but an unknown
    // effect is parked on that action alone and never holds unrelated owner input.
    const parkedActionIds = new Set<string>();
    const hasDelayedNotice = (sourceId: string) => replies.some(reply => reply.event_id === sourceId && isDelayedOutcomeNotice(reply));
    const cancelPreparedDelayedNotice = (sourceId: string) => {
      for (const reply of replies) if (reply.event_id === sourceId && reply.dispatch_status === 'prepared' && isDelayedOutcomeNotice(reply))
        this.deps.journal.cancelPreparedDispatch(reply.dispatch_id as string);
    };
    // One inspect per process before the notice exists, then the durable per-action cadence.
    const mayInspectDelayed = (action: Record<string, unknown>, source: {id:string;closed:boolean}) => {
      const policy = delayedPolicy(action.kind)!;
      const actionId = action.id as string;
      if (delayedSources) return delayedSources.has(source.id) && Date.now() - Number(action.updated_at) >= policy.inspectIntervalMs;
      if (!source.closed && !hasDelayedNotice(source.id) && !this.delayedInspected.has(actionId)) {
        if (this.delayedInspected.size >= 1024) this.delayedInspected.clear();
        this.delayedInspected.add(actionId);
        return true;
      }
      return Date.now() - Number(action.updated_at) >= policy.inspectIntervalMs;
    };
    const threadKeyNow = stillPending[0]!.thread_key;
    const actionSource = (action: Record<string, unknown>): {id: string; closed: boolean} | null => {
      const sourceId = action.source_event_id;
      if (typeof sourceId !== 'string') return null;
      if (stillPending.some(row => row.id === sourceId)) return {id: sourceId, closed: false};
      const source = this.deps.journal.getInbound(sourceId);
      const run = this.deps.journal.getRun(action.run_id as string);
      if (!source || source.outcome === 'pending' || !run || run.thread_key !== source.thread_key ||
          source.thread_key !== threadKeyNow) return null;
      return {id: sourceId, closed: true};
    };
    const partition = <T extends object>(action: Record<string, unknown>, source: {id: string; closed: boolean}, authority: T) => {
      if (source.closed) parkedActionIds.add(action.id as string); else heldCommandIds.add(source.id);
      return source.closed ? {...authority, inspectClosedSource: true} : authority;
    };
    if (this.deps.ownerReminderTool) {
      const authority = this.ownerReminderToolAuthority();
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== ownerReminderToolKind) continue;
        const source = actionSource(action);
        if (!source) return {status: 'held'};
        const scoped = partition(action, source, authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const saved = this.deps.journal.readOwnerReminderToolAction(action.id as string, scoped);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerReminderTool.call('inspect', saved.request);
          if (delayedProofSettles(ownerReminderToolKind, proof.status) && proof.status === 'committed') {
            this.deps.journal.settleOwnerReminderToolAction(action.id as string, scoped, proof, true);
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          }
        } catch {/* Keep only this owner input held; never recreate an uncertain reminder. */}
      }
    }
    if (this.deps.ownerTaskTool) {
      const authority = this.ownerTaskToolAuthority();
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== ownerTaskToolKind) continue;
        const source = actionSource(action);
        if (!source) return {status: 'held'};
        const scoped = partition(action, source, authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const saved = this.deps.journal.readOwnerTaskToolAction(action.id as string, scoped);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerTaskTool.call('inspect', saved.request);
          if (delayedProofSettles(ownerTaskToolKind, proof.status) &&
              (proof.status === 'committed' || proof.status === 'preview')) {
            this.deps.journal.settleOwnerTaskToolAction(action.id as string, scoped, proof, true);
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          }
        } catch {/* Keep only this owner input held; never recreate an uncertain task. */}
      }
    }
    if (this.deps.ownerPreferenceTool) {
      const authority = this.ownerPreferenceToolAuthority();
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== ownerPreferenceToolKind) continue;
        const source = actionSource(action);
        if (!source) return {status: 'held'};
        const scoped = partition(action, source, authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const saved = this.deps.journal.readOwnerPreferenceToolAction(action.id as string, scoped);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerPreferenceTool.call('inspect', saved.request);
          if (delayedProofSettles(ownerPreferenceToolKind, proof.status) && proof.status === 'committed') {
            this.deps.journal.settleOwnerPreferenceToolAction(action.id as string, scoped, proof, true);
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          }
        } catch {/* Keep only this owner input held; never recreate an uncertain preference. */}
      }
    }
    if (this.deps.ownerWebRead) {
      const client=this.deps.ownerWebRead;
      const authority={accountId:client.identity.account_id,ownerId:client.identity.owner_id,lineKey:client.identity.line_key,channel:client.identity.channel,scopeHash:client.scopeHash};
      for(const action of this.deps.journal.listOwnerWebReadActionsForThread(eventId)) {
        if(!['executing','unknown','succeeded'].includes(action.status as string))continue;
        const source=actionSource(action);
        // A missing/foreign source is left to the final unknown-action fence below.
        if(!source||(source.closed&&action.status==='succeeded'))continue;
        const scoped=partition(action,source,authority);
        // Another request's held read stays held; a timer pass inspects only its due request.
        if(delayedSources && !delayedSources.has(source.id))continue;
        if(action.status==='executing'||(action.status==='unknown'&&!mayInspectDelayed(action,source)))continue;
        try {
          const saved=this.deps.journal.readOwnerWebReadAction(action.id as string,scoped);
          if(saved.preview){heldCommandIds.delete(source.id);continue;}
          if(action.status==='unknown')this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof=await client.call('inspect',saved.request);
          if(delayedProofSettles(ownerWebReadKind,proof.status)&&proof.status==='committed'){
            if(action.status==='unknown')this.deps.journal.settleOwnerWebReadAction(action.id as string,scoped,proof,true);
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          }
        }catch{/* Inspect only; unavailable completed evidence holds this input, not safe chat. */}
      }
    }
    if (this.deps.ownerSkillRequest) {
      const authority = this.ownerSkillRequestAuthority();
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== ownerSkillRequestKind) continue;
        const source = actionSource(action);
        if (!source) return {status: 'held'};
        const scoped = partition(action, source, authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const saved = this.deps.journal.readOwnerSkillRequestAction(action.id as string, scoped);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerSkillRequest.call('inspect', saved.request);
          if (delayedProofSettles(ownerSkillRequestKind, proof.status) && proof.status === 'committed') {
            this.deps.journal.settleOwnerSkillRequestAction(action.id as string, scoped, proof, true);
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          }
        } catch {/* Keep only this owner input held; never recreate an uncertain skill request. */}
      }
    }
    if (this.deps.ownerRoutineTool) {
      const authority = this.ownerRoutineToolAuthority();
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== ownerRoutineToolKind) continue;
        const source = actionSource(action);
        if (!source) return {status: 'held'};
        const scoped = partition(action, source, authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const saved = this.deps.journal.readOwnerRoutineToolAction(action.id as string, scoped);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerRoutineTool.call('inspect', saved.request);
          if (delayedProofSettles(ownerRoutineToolKind, proof.status) && proof.status === 'committed') {
            this.deps.journal.settleOwnerRoutineToolAction(action.id as string, scoped, proof, true);
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          }
        } catch {/* Keep only this owner input held; never recreate an uncertain routine. */}
      }
    }
    if (this.deps.ownerGmailDraft) {
      const authority = this.ownerGmailDraftAuthority();
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== ownerGmailDraftKind) continue;
        const source = actionSource(action);
        if (!source) return {status: 'held'};
        const scoped = partition(action, source, authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const saved = this.deps.journal.readOwnerGmailDraftAction(action.id as string, scoped);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerGmailDraft.call('inspect', saved.request);
          if (delayedProofSettles(ownerGmailDraftKind, proof.status) &&
              (proof.status === 'committed' || proof.status === 'preview' || proof.status === 'rejected')) {
            this.deps.journal.settleOwnerGmailDraftAction(action.id as string, scoped, proof.status === 'committed'||proof.status==='preview' ? proof : proof.code, true);
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          }
        } catch {/* Keep only this owner input held; never recreate an uncertain draft. */}
      }
    }
    if (this.deps.ownerCalendar) {
      const authority = this.ownerCalendarAuthority();
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== ownerCalendarKind) continue;
        const source = actionSource(action);
        if (!source) return {status: 'held'};
        const scoped = partition(action, source, authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const saved = this.deps.journal.readOwnerCalendarAction(action.id as string, scoped);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerCalendar.call('inspect', saved.request);
          if (delayedProofSettles(ownerCalendarKind, proof.status) &&
              (proof.status === 'committed' || proof.status === 'rejected')) {
            this.deps.journal.settleOwnerCalendarAction(action.id as string, scoped, proof.status === 'committed' ? proof : proof.code, true);
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          }
        } catch {/* Keep only this owner input held; never recreate an uncertain draft. */}
      }
    }
    if (this.deps.ownerContacts) {
      const authority = this.ownerContactsAuthority();
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== ownerContactsKind) continue;
        const source = actionSource(action);
        if (!source) return {status: 'held'};
        const scoped = partition(action, source, authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const saved = this.deps.journal.readOwnerContactsAction(action.id as string, scoped);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerContacts.call('inspect', saved.request);
          if (delayedProofSettles(ownerContactsKind, proof.status) &&
              (proof.status === 'committed' || proof.status === 'rejected')) {
            this.deps.journal.settleOwnerContactsAction(action.id as string, scoped, proof.status === 'committed' ? proof : proof.code, true);
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          }
        } catch {/* Keep only this owner input held; never recreate an uncertain draft. */}
      }
    }
    if (this.deps.ownerBridgeSend) {
      const authority = this.ownerBridgeSendAuthority();
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== ownerBridgeSendKind) continue;
        const source = actionSource(action);
        if (!source) return {status: 'held'};
        const scoped = partition(action, source, authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const saved = this.deps.journal.readOwnerBridgeSendAction(action.id as string, scoped);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerBridgeSend.call('inspect', saved.request);
          if (delayedProofSettles(ownerBridgeSendKind, proof.status) &&
              (proof.status === 'committed' || proof.status === 'rejected')) {
            this.deps.journal.settleOwnerBridgeSendAction(action.id as string, scoped, proof.status === 'committed' ? proof : proof.code, true);
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          }
        } catch {/* Keep only this owner input held; never recreate an uncertain draft. */}
      }
    }
    if (this.deps.ownerLoop) {
      const authority = { accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
        channel: this.deps.ownerPolicy.channel, lineKey: this.deps.ownerLoop.identity.line_key,
        scopeHash: this.deps.ownerLoop.scopeHash };
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (!['owner_loop_add_admitted','owner_loop_close_admitted'].includes(action.kind as string)) continue;
        const source = actionSource(action);
        if (!source) return {status:'held'};
        partition(action,source,authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        if (action.kind === 'owner_loop_add_admitted') {
          const intent = this.deps.journal.readUnknownExplicitOwnerLoopIntent(action.id as string, authority);
          if (!intent) {
            // An invalid effect authority cannot be inspected or acknowledged. It
            // can still be partitioned if its original admitted command is intact.
            const sourceId = action.source_event_id;
            if (typeof sourceId !== 'string') return { status: 'held' };
            const source = this.deps.journal.getInbound(sourceId);
            const run = this.deps.journal.getRun(action.run_id as string);
            // A /stop already closed its source: never inspected or acknowledged, parked alone.
            if (actionSource(action)?.closed) { parkedActionIds.add(action.id as string); continue; }
            if (!source || !run || source.outcome !== 'pending' ||
                source.thread_key !== stillPending[0]!.thread_key || run.thread_key !== source.thread_key ||
                run.event_id !== sourceId || source.account_key !== authority.accountId ||
                source.line_key !== authority.lineKey || source.channel !== authority.channel ||
                source.sender !== authority.ownerId || typeof source.payload !== 'string' ||
                createHash('sha256').update(source.payload).digest('hex') !== source.payload_hash) {
              return { status: 'held' };
            }
            let saved: SavedPayload;
            try { saved = decode(source.payload); } catch { return { status: 'held' }; }
            if (saved?.inbound?.eventId !== source.source_id ||
                saved.inbound.accountId !== source.account_key || saved.inbound.senderId !== source.sender ||
                saved.inbound.channel !== source.channel || saved.context?.lineKey !== source.line_key ||
                !saved.inbound.text?.startsWith('/loop add ') ||
                !saved.inbound.text.slice('/loop add '.length).trim()) return { status: 'held' };
            heldCommandIds.add(sourceId);
            continue;
          }
          try {
            this.deps.journal.markDelayedActionInspection(action.id as string);
            const proof = await this.deps.ownerLoop.call('inspect', this.ownerLoopRequest(intent), undefined);
            if (!delayedProofSettles('owner_loop_add_admitted',proof.status) || proof.status !== 'committed') {
              heldCommandIds.add(intent.sourceEventId);
              continue;
            }
            this.deps.journal.reconcileExplicitOwnerLoopIntent(intent.actionId,
              this.ownerLoopReceiptHash(intent.sourceEventId, proof.anchor_key));
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          } catch { heldCommandIds.add(intent.sourceEventId); }
        } else if (action.kind === 'owner_loop_close_admitted') {
          const intent = this.deps.journal.readUnknownExplicitOwnerLoopCloseIntent(action.id as string, authority);
          if (!intent || !intent.expectedRowDigest) {
            heldCommandIds.add(action.source_event_id as string);
            continue;
          }
          try {
            this.deps.journal.markDelayedActionInspection(action.id as string);
            const proof = await this.deps.ownerLoop.callClose('close_inspect', this.ownerLoopCloseRequest(intent), undefined);
            if (!delayedProofSettles('owner_loop_close_admitted',proof.status) || proof.status !== 'committed') {
              heldCommandIds.add(action.source_event_id as string);
              continue;
            }
            this.deps.journal.reconcileExplicitOwnerLoopCloseIntent(intent.actionId,
              this.ownerLoopCloseReceiptHash(intent.sourceEventId, proof.anchor_key, proof.post_digest));
            cancelPreparedDelayedNotice(source.id);
            heldCommandIds.delete(source.id);
          } catch { heldCommandIds.add(action.source_event_id as string); }
        }
      }
    }
    if (this.deps.ownerReminder) {
      const authority = { accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
        channel: this.deps.ownerPolicy.channel, lineKey: this.deps.ownerReminder.identity.line_key,
        scopeHash: this.deps.ownerReminder.scopeHash };
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== 'owner_reminder_admitted') continue;
        const source = actionSource(action);
        if (!source) return {status:'held'};
        partition(action,source,authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        const intent = this.deps.journal.readUnknownExplicitOwnerReminderIntent(action.id as string, authority);
        if (!intent) {
          // Invalid effect authority may still identify its intact command for
          // isolation, but must never authorize inspect or an acknowledgement.
          const sourceId = action.source_event_id;
          if (typeof sourceId !== 'string') return { status: 'held' };
          const source = this.deps.journal.getInbound(sourceId);
          const run = this.deps.journal.getRun(action.run_id as string);
          if (actionSource(action)?.closed) { parkedActionIds.add(action.id as string); continue; }
          if (!source || !run || source.outcome !== 'pending' ||
              source.thread_key !== stillPending[0]!.thread_key || run.thread_key !== source.thread_key ||
              run.event_id !== sourceId || source.account_key !== authority.accountId ||
              source.line_key !== authority.lineKey || source.channel !== authority.channel ||
              source.sender !== authority.ownerId || typeof source.payload !== 'string' ||
              createHash('sha256').update(source.payload).digest('hex') !== source.payload_hash) return { status: 'held' };
          let saved: SavedPayload;
          try { saved = decode(source.payload); } catch { return { status: 'held' }; }
          if (saved.inbound.eventId !== source.source_id || saved.inbound.accountId !== source.account_key ||
              saved.inbound.senderId !== source.sender || saved.inbound.channel !== source.channel ||
              saved.context.lineKey !== source.line_key ||
              !parseOwnerReminderCommand(saved.inbound.text ?? '')) return { status: 'held' };
          heldCommandIds.add(sourceId);
          continue;
        }
        try {
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerReminder.call('inspect', this.ownerReminderRequest(intent));
          if (!delayedProofSettles('owner_reminder_admitted',proof.status) || proof.status !== 'committed' || proof.operation !== intent.operation) {
            heldCommandIds.add(intent.sourceEventId);
            continue;
          }
          this.deps.journal.settleExplicitOwnerReminderIntent(intent.actionId,
            this.ownerReminderReceiptHash(proof), proof.reminder_id, proof.reminder_status, true);
          cancelPreparedDelayedNotice(source.id);
          heldCommandIds.delete(source.id);
        } catch { heldCommandIds.add(intent.sourceEventId); }
      }
    }
    if (this.deps.ownerQuiet) {
      const client = this.deps.ownerQuiet;
      const authority = { accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
        channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash };
      for (const action of this.deps.journal.listUnknownActionsForThread(eventId)) {
        if (action.kind !== 'owner_quiet_admitted') continue;
        const source = actionSource(action);
        if (!source) return {status:'held'};
        partition(action,source,authority);
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        const intent = this.deps.journal.readUnknownExplicitOwnerQuietIntent(action.id as string, authority);
        if (!intent) {
          // Preserve an intact, unauthorized prior effect as held while later chat proceeds.
          const sourceId = action.source_event_id;
          if (typeof sourceId !== 'string') return { status: 'held' };
          const source = this.deps.journal.getInbound(sourceId);
          const run = this.deps.journal.getRun(action.run_id as string);
          if (actionSource(action)?.closed) { parkedActionIds.add(action.id as string); continue; }
          if (!source || !run || source.outcome !== 'pending' || run.event_id !== sourceId ||
              run.thread_key !== source.thread_key || source.thread_key !== stillPending[0]!.thread_key ||
              source.account_key !== authority.accountId || source.line_key !== authority.lineKey ||
              source.channel !== authority.channel || source.sender !== authority.ownerId ||
              typeof source.payload !== 'string' ||
              createHash('sha256').update(source.payload).digest('hex') !== source.payload_hash) return { status: 'held' };
          let saved: SavedPayload;
          try { saved = decode(source.payload); } catch { return { status: 'held' }; }
          if (saved.inbound.eventId !== source.source_id || saved.inbound.accountId !== source.account_key ||
              saved.inbound.senderId !== source.sender || saved.inbound.channel !== source.channel ||
              saved.context.lineKey !== source.line_key || !parseOwnerQuietCommand(saved.inbound.text ?? '')) return { status: 'held' };
          heldCommandIds.add(sourceId);
          continue;
        }
        try {
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await client.call('inspect', this.ownerQuietRequest(intent));
          if (!delayedProofSettles('owner_quiet_admitted',proof.status) || proof.status !== 'committed') { heldCommandIds.add(intent.sourceEventId); continue; }
          this.deps.journal.settleExplicitOwnerQuietIntent(intent.actionId,
            this.ownerQuietReceiptHash(proof), proof.zone, proof.snooze_until, true);
          cancelPreparedDelayedNotice(source.id);
          heldCommandIds.delete(source.id);
        } catch { heldCommandIds.add(intent.sourceEventId); }
      }
    }
    if (this.deps.ownerBriefRequest) {
      for (const action of this.deps.journal.listOwnerBriefRequestActionsForThread(eventId)) {
        if (!['unknown','executing'].includes(action.status as string)) continue;
        const source = actionSource(action);
        if (!source) return {status:'held'};
        const closed = source.closed;
        partition(action,source,this.briefAuthority());
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const authority = closed ? {...this.briefAuthority(), inspectClosedSource: true} : this.briefAuthority();
          const saved = this.deps.journal.readOwnerBriefRequestAction(action.id as string, authority);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerBriefRequest.call('inspect', saved.request);
          if (!delayedProofSettles('owner_brief_request',proof.status) || proof.status !== 'committed') { if (!closed) heldCommandIds.add(action.source_event_id as string); continue; }
          this.deps.journal.settleOwnerBriefRequestAction(action.id as string, authority, proof, true);
          cancelPreparedDelayedNotice(source.id);
          heldCommandIds.delete(source.id);
        } catch { if (!closed) heldCommandIds.add(action.source_event_id as string); }
      }
    }
    if (this.deps.ownerMeetingPrep) {
      for (const action of this.deps.journal.listOwnerMeetingPrepActionsForThread(eventId)) {
        if (!['unknown','executing'].includes(action.status as string)) continue;
        const source = actionSource(action);
        if (!source) return {status:'held'};
        const closed = source.closed;
        partition(action,source,this.meetingPrepAuthority());
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const authority = closed ? {...this.meetingPrepAuthority(), inspectClosedSource: true} : this.meetingPrepAuthority();
          const saved = this.deps.journal.readOwnerMeetingPrepAction(action.id as string, authority);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerMeetingPrep.call('inspect', saved.request);
          if (!delayedProofSettles('owner_meeting_prep_request',proof.status) || proof.status !== 'committed') { if (!closed) heldCommandIds.add(action.source_event_id as string); continue; }
          this.deps.journal.settleOwnerMeetingPrepAction(action.id as string, authority, proof, true);
          cancelPreparedDelayedNotice(source.id);
          heldCommandIds.delete(source.id);
        } catch { if (!closed) heldCommandIds.add(action.source_event_id as string); }
      }
    }
    if(this.deps.ownerMeetingPrepOffer){
      const authority=this.ownerMeetingPrepOfferAuthority();
      for(const action of this.deps.journal.listOwnerMeetingPrepOfferActionsForThread(eventId)){
        if(!['unknown','executing'].includes(action.status as string))continue;
        const source=actionSource(action);
        if(!source)return {status:'held'};
        const closed=source.closed;
        partition(action,source,authority);
        if(action.status!=='unknown'||!mayInspectDelayed(action,source))continue;
        try{
          const saved=this.deps.journal.readOwnerMeetingPrepOfferAction(action.id as string,
            closed?{...authority,inspectClosedSource:true}:authority);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const accepted=this.deps.journal.acceptedOwnerMeetingPrepOffer(action.id as string,authority);
          const proof=await this.deps.ownerMeetingPrepOffer.call('inspect',saved.request,undefined,accepted?.acceptedDelivery);
          if(proof.status==='preview'){
            this.deps.journal.settleOwnerMeetingPrepOfferAction(action.id as string,authority,proof,true);
            cancelPreparedDelayedNotice(source.id);heldCommandIds.delete(source.id);
          }else if(proof.status==='recorded'&&accepted){
            this.deps.journal.settleOwnerMeetingPrepOfferReceipt(action.id as string,authority,proof);
            cancelPreparedDelayedNotice(source.id);heldCommandIds.delete(source.id);
          }else if(!closed)heldCommandIds.add(action.source_event_id as string);
        }catch{if(!closed)heldCommandIds.add(action.source_event_id as string);}
      }
    }
    if (this.deps.ownerOfferReply) {
      for (const action of this.deps.journal.listOwnerOfferReplyActionsForThread(eventId)) {
        if (!['unknown','executing'].includes(action.status as string)) continue;
        const source = actionSource(action);
        if (!source) return {status:'held'};
        const closed = source.closed;
        partition(action,source,this.offerReplyAuthority());
        if (action.status !== 'unknown' || !mayInspectDelayed(action,source)) continue;
        try {
          const authority = closed ? {...this.offerReplyAuthority(), inspectClosedSource: true} : this.offerReplyAuthority();
          const saved = this.deps.journal.readOwnerOfferReplyAction(action.id as string, authority);
          this.deps.journal.markDelayedActionInspection(action.id as string);
          const proof = await this.deps.ownerOfferReply.call('inspect', saved.request);
          if (!delayedProofSettles('owner_offer_reply',proof.status) || proof.status !== 'committed') { if (!closed) heldCommandIds.add(action.source_event_id as string); continue; }
          this.deps.journal.settleOwnerOfferReplyAction(action.id as string, authority, proof, true);
          cancelPreparedDelayedNotice(source.id);
          heldCommandIds.delete(source.id);
        } catch { if (!closed) heldCommandIds.add(action.source_event_id as string); }
      }
    }
    // An unknown consequential action requires proof. A model rerun could repeat it.
    if (this.deps.journal.listUnknownActionsForThread(eventId)
      .some(row => !parkedActionIds.has(row.id as string) && (!Boolean(delayedPolicy(row.kind)) ||
        !heldCommandIds.has(row.source_event_id as string)))) {
      return { status: 'held' };
    }
    // Quiet commands mutate one scalar. Only the earliest pending one can run;
    // later quiet commands wait for its exact effect and reply while safe chat proceeds.
    let firstQuiet = false;
    for (const row of stillPending) {
      if (!decode(row.payload as string).inbound.text?.startsWith('/quiet')) continue;
      if (firstQuiet) heldCommandIds.add(row.id as string);
      firstQuiet = true;
    }
    // A contacted reply holds only the owner events it actually covered. Later
    // admissions can form a separate run; prior Mastra transcript context is not
    // canonical input replay and cannot authorize another send of this reply.
    const heldReplyIds = new Set<string>(heldStopId ? [heldStopId] : []);
    const stillPendingIds = new Set(stillPending.map(row => row.id as string));
    for (const reply of replies) {
      if (isDelayedOutcomeNotice(reply)) continue;
      if (reply.dispatch_status !== 'unknown' && reply.dispatch_status !== 'dispatching') continue;
      for (const id of JSON.parse(reply.covered_event_ids as string) as string[]) {
        if (stillPendingIds.has(id)) heldReplyIds.add(id);
      }
    }
    const acknowledgementNow = Date.now();
    for (const id of stillPendingIds) {
      const until = this.acknowledgementHeldUntil.get(id);
      if (until === undefined) continue;
      if (until > acknowledgementNow) heldReplyIds.add(id); else this.acknowledgementHeldUntil.delete(id);
    }
    // A crash may leave the already journaled notice prepared before provider
    // contact. Resume only that exact dispatch; contacted notices are never resent.
    const preparedHeldNotice = replies.find(reply => reply.dispatch_status === 'prepared' &&
      (!delayedSources || delayedSources.has(reply.event_id as string)) &&
      isHeldOutcomeNotice(reply) && heldCommandIds.has(reply.event_id as string) &&
      stillPendingIds.has(reply.event_id as string));
    if (preparedHeldNotice) {
      const run = this.deps.journal.getRun(preparedHeldNotice.run_id as string);
      if (!run || run.event_id !== preparedHeldNotice.event_id) return {status: 'held'};
      if (run.status === 'cancelled') this.deps.journal.cancelPreparedDispatch(preparedHeldNotice.dispatch_id as string);
      else return this.postReply(preparedHeldNotice);
    }
    // A contacted effect may remain unprovable forever. Tell the owner once, then
    // keep its input pending for receipt reconciliation while safe chat continues.
    const unknownOwnerAction = this.deps.journal.listUnknownActionsForThread(eventId).find(action => {
      const policy = delayedPolicy(action.kind);
      if (!policy || action.status !== 'unknown' ||
          (delayedSources && !delayedSources.has(action.source_event_id as string)) ||
          this.deps.journal.getRun(action.run_id as string)?.event_id !== action.source_event_id ||
          !heldCommandIds.has(action.source_event_id as string) ||
          !stillPendingIds.has(action.source_event_id as string) ||
          stillPending.some(row => (!delayedSources || delayedSources.has(row.id as string)) && row.id !== action.source_event_id &&
            !heldCommandIds.has(row.id as string) && !heldReplyIds.has(row.id as string)) ||
          replies.some(reply => (JSON.parse(reply.covered_event_ids as string) as string[])
            .includes(action.source_event_id as string) && !isDelayedOutcomeNotice(reply))) return false;
      const notices = replies.filter(reply => reply.event_id === action.source_event_id && isDelayedOutcomeNotice(reply));
      if (notices.some(reply => reply.dispatch_status !== 'rejected') || notices.length >= policy.maxNoticeAttempts) return false;
      const latest = notices.at(-1);
      return !latest || Date.now() - Number(latest.dispatch_updated_at) >= policy.inspectIntervalMs;
    });
    if (unknownOwnerAction) {
      const sourceId = unknownOwnerAction.source_event_id as string;
      const policy = delayedPolicy(unknownOwnerAction.kind)!;
      const source = stillPending.find(row => row.id === sourceId)!;
      const saved = decode(source.payload as string);
      const runId = randomUUID();
      this.deps.journal.startRun({runId, eventId: sourceId});
      const reply = this.prepareReply({replyId: randomUUID(), runId, eventId: sourceId,
        coveredEventIds: [sourceId], dispatchId: randomUUID(), kind: 'interactive',
        destination: saved.context.destination,
        purpose: 'outcome_pending',
        payload: `I couldn’t confirm whether ${policy.subject} happened.${HELD_NOTICE_SUFFIX}`,
        sourceRevision: OWNER_ONLY_CONTEXT_PROOF});
      return this.postReply(reply);
    }
    const remainingIds = new Set([...stillPendingIds].filter(id => !heldReplyIds.has(id) && (!delayedSources || delayedSources.has(id))));
    const priorReply = replies.find(row => {
      if (isDelayedOutcomeNotice(row)) return false;
      const covered = JSON.parse(row.covered_event_ids as string) as string[];
      return covered.some(id => remainingIds.has(id) && !heldCommandIds.has(id)) &&
        ['prepared', 'unknown', 'dispatching'].includes(row.dispatch_status as string);
    });
    if (priorReply?.dispatch_status === 'unknown' || priorReply?.dispatch_status === 'dispatching') return { status: 'held' };
    if (priorReply?.dispatch_status === 'prepared') {
      const run = this.deps.journal.getRun(priorReply.run_id as string);
      if (run?.status === 'cancelled') {
        this.deps.journal.cancelPreparedDispatch(priorReply.dispatch_id as string);
      } else {
        return this.postReply(priorReply);
      }
    }
    const remaining = this.deps.journal.listPendingInboundForThread(eventId)
      .filter(row => (!delayedSources || delayedSources.has(row.id as string)) && !heldCommandIds.has(row.id as string) && !heldReplyIds.has(row.id as string));
    if (!remaining.length) return heldCommandIds.size || heldReplyIds.size ? { status: 'held' } : { status: 'silent' };
    const commandRows = remaining.filter(row => decode(row.payload as string).inbound.text?.trim().toLowerCase() === '/new');
    const workRows = remaining.filter(row => !commandRows.includes(row));
    if (!workRows.length) {
      const context = decode(commandRows[commandRows.length - 1]!.payload as string).context;
      // A recorded control acknowledgement proves this rotation already ran; never rotate twice.
      const applied = replies.some(reply => reply.purpose === 'owner_control' &&
        reply.event_id === commandRows[commandRows.length - 1]!.id);
      if (!applied) {
        try { await this.deps.rotateSession?.(context.threadId, commandRows[commandRows.length - 1]!.id as string); }
        catch { return { status: 'error', reason: 'session rotation failed' }; }
      }
      return this.acknowledgeControl(commandRows[commandRows.length - 1]!.id as string,
        context.destination, 'I started a new conversation.', commandRows.map(row => row.id as string));
    }
    // A timer can acknowledge exact settled work, but cannot preview a new offer,
    // admit unrelated input, rotate a session, or enter the model loop.
    if (delayedSources) {
      const prep = this.deps.journal.listOwnerMeetingPrepActionsForThread(eventId).find(action =>
        ['succeeded','rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (prep && this.deps.ownerMeetingPrep) return this.processMeetingPrep(prep.source_event_id as string);
      const prepOffer=this.deps.journal.listOwnerMeetingPrepOfferActionsForThread(eventId).find(action=>
        ['succeeded','rejected'].includes(action.status as string)&&workRows.some(row=>row.id===action.source_event_id));
      if(prepOffer&&this.deps.ownerMeetingPrepOffer)return this.acknowledgeOwnerMeetingPrepOffer(prepOffer.source_event_id as string);
      const offer = this.deps.journal.listOwnerOfferReplyActionsForThread(eventId).find(action =>
        ['succeeded','rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (offer && this.deps.ownerOfferReply) return this.processOfferReply(offer.source_event_id as string);
    }
    if (this.deps.ownerMeetingPrep && !delayedSources) {
      const unthreaded = await this.previewContinuation(workRows, 'prep');
      if (unthreaded === undefined) return {status: 'queued'};
      if (unthreaded && typeof unthreaded === 'object') return unthreaded;
      if (unthreaded) return this.processMeetingPrep(unthreaded);
    }
    if (this.deps.ownerOfferReply && !delayedSources) {
      const offer = workRows.find(row => {
        const inbound = decode(row.payload as string).inbound;
        return offerReplySelectors(inbound) && !this.isLocalProposalReply(row.id as string, inbound.replyToMessageId!);
      });
      if (offer) return this.processOfferReply(offer.id as string);
      const unthreaded = await this.previewContinuation(workRows, 'decline');
      if (unthreaded === undefined) return {status: 'queued'};
      if (unthreaded && typeof unthreaded === 'object') return unthreaded;
      if (unthreaded) return this.processOfferReply(unthreaded);
    }
    if (!delayedSources && !await this.freezeFreshBackgroundContinuation(workRows)) return {status: 'queued'};
    if (this.deps.ownerBriefRequest) {
      const brief = workRows.find(row => classifyRequestedBrief(decode(row.payload as string).inbound)
        || this.deps.journal.listOwnerBriefRequestActionsForThread(eventId).some(action => action.source_event_id === row.id));
      if (brief) {
        const result = await this.processRequestedBrief(brief.id as string, Boolean(delayedSources));
        if (result) return result;
        // Only the admitted independent clause reaches the model. Original journal input is immutable.
        workRows.splice(0, workRows.length, brief);
      }
    }
    if (this.deps.ownerReminderTool) {
      const completed = this.deps.journal.listOwnerReminderToolActionsForThread(eventId).find(action =>
        ['succeeded', 'rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (completed) return this.acknowledgeOwnerReminderTool(completed.source_event_id as string);
    }
    if (this.deps.ownerTaskTool) {
      const completed = this.deps.journal.listOwnerTaskToolActionsForThread(eventId).find(action =>
        ['succeeded', 'rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (completed) return this.acknowledgeOwnerTaskTool(completed.source_event_id as string);
    }
    if (this.deps.ownerPreferenceTool) {
      const completed = this.deps.journal.listOwnerPreferenceToolActionsForThread(eventId).find(action =>
        ['succeeded', 'rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (completed) return this.acknowledgeOwnerPreferenceTool(completed.source_event_id as string);
    }
    if (this.deps.ownerSkillRequest) {
      const completed = this.deps.journal.listOwnerSkillRequestActionsForThread(eventId).find(action =>
        ['succeeded', 'rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (completed) return this.acknowledgeOwnerSkillRequest(completed.source_event_id as string);
    }
    if (this.deps.ownerRoutineTool) {
      const completed = this.deps.journal.listOwnerRoutineToolActionsForThread(eventId).find(action =>
        ['succeeded', 'rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (completed) return this.acknowledgeOwnerRoutineTool(completed.source_event_id as string);
    }
    if (this.deps.ownerGmailDraft) {
      const completed = this.deps.journal.listOwnerGmailDraftActionsForThread(eventId).find(action =>
        ['succeeded', 'rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (completed) return await this.acknowledgeOwnerGmailDraft(completed.source_event_id as string);
    }
    if (this.deps.ownerCalendar) {
      const completed = this.deps.journal.listOwnerCalendarActionsForThread(eventId).find(action =>
        ['succeeded', 'rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (completed) return await this.acknowledgeOwnerCalendar(completed.source_event_id as string);
    }
    if (this.deps.ownerContacts) {
      const completed = this.deps.journal.listOwnerContactsActionsForThread(eventId).find(action =>
        ['succeeded', 'rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (completed) return await this.acknowledgeOwnerContacts(completed.source_event_id as string);
    }
    if (this.deps.ownerBridgeSend) {
      const completed = this.deps.journal.listOwnerBridgeSendActionsForThread(eventId).find(action =>
        ['succeeded', 'rejected'].includes(action.status as string) && workRows.some(row => row.id === action.source_event_id));
      if (completed) return await this.acknowledgeOwnerBridgeSend(completed.source_event_id as string);
    }
    if(this.deps.ownerWebRead) {
      const preview=this.deps.journal.listOwnerWebReadActionsForThread(eventId).find(action=>workRows.some(row=>row.id===action.source_event_id)&&action.status==='succeeded'&&JSON.parse(action.result_json as string).preview);
      if(preview)return await this.acknowledgeOwnerDocsend(preview.source_event_id as string);
      if (delayedSources) {
        const recovered = this.deps.journal.listOwnerWebReadActionsForThread(eventId).find(action =>
          action.status === 'succeeded' && workRows.some(row => row.id === action.source_event_id));
        if (recovered) return this.acknowledgeOwnerDocsend(recovered.source_event_id as string, undefined, true);
      }
    }
    const ownerCommand = this.deps.ownerLoop && workRows.find(row =>
      decode(row.payload as string).inbound.text?.startsWith('/loop add '));
    const ownerClose = this.deps.ownerLoop && workRows.find(row =>
      decode(row.payload as string).inbound.text?.startsWith('/loop close '));
    const ownerReminder = this.deps.ownerReminder && workRows.find(row =>
      decode(row.payload as string).inbound.text?.startsWith('/reminder'));
    const ownerQuiet = this.deps.ownerQuiet && workRows.find(row =>
      decode(row.payload as string).inbound.text?.startsWith('/quiet'));
    if (ownerQuiet) return this.processOwnerQuietCommand(ownerQuiet.id as string, workRows);
    if (ownerReminder) return this.processOwnerReminderCommand(ownerReminder.id as string, workRows);
    if (ownerCommand && ownerClose) return { status: 'held' };
    if (ownerCommand) return this.processOwnerLoopCommand(ownerCommand.id as string, workRows);
    if (ownerClose) return this.processOwnerLoopCloseCommand(ownerClose.id as string, workRows);
    if (delayedSources) return {status: 'held'};
    const batched = workRows.map(row => {
      const saved = decode(row.payload as string);
      const classified = this.deps.ownerBriefRequest ? classifyRequestedBrief(saved.inbound) : null;
      return classified?.remainder ? {...saved, inbound: {...saved.inbound, text: classified.remainder}} : saved;
    });
    // Pending owner input is re-batched after a failed turn. The model's chat bound is a
    // deterministic validation, so a batch that exceeds it would fail forever: process the
    // oldest messages that fit and leave the rest pending for the following turn. A lone
    // message above the bound reaches the model visibly truncated; its journal row is intact.
    let kept = 0;
    for (let length = 0; kept < batched.length; kept++) {
      const size = (batched[kept]!.inbound.text ?? '[attachment received]').length + (kept ? 1 : 0);
      if (kept && length + size > MAX_MODEL_TURN_TEXT) break;
      length += size;
    }
    if (kept < workRows.length) {
      workRows.splice(kept);
      this.deferredOverflow.add(workRows[0]!.thread_key as string);
    }
    const messages = batched.slice(0, kept).map(item => item.inbound.text && item.inbound.text.length > MAX_MODEL_TURN_TEXT
      ? {...item, inbound: {...item.inbound, text: truncateUtf16(item.inbound.text, MAX_MODEL_TURN_TEXT - TRUNCATION_MARKER.length) + TRUNCATION_MARKER}}
      : item);
    const context = messages[messages.length - 1]!.context;
    const projectedBrief = Boolean(this.deps.ownerBriefRequest && classifyRequestedBrief(decode(workRows[0]!.payload as string).inbound)?.remainder);
    const text = messages.map(item => item.inbound.text ?? '[attachment received]').join('\n');
    const recoveredWebReads: Array<{sourceRevision:string|null;result:unknown}> = [];
    if(this.deps.ownerWebRead) {
      const client=this.deps.ownerWebRead;
      const authority={accountId:client.identity.account_id,ownerId:client.identity.owner_id,lineKey:client.identity.line_key,channel:client.identity.channel,scopeHash:client.scopeHash};
      for(const action of this.deps.journal.listOwnerWebReadActionsForThread(eventId).filter(row=>workRows.some(event=>event.id===row.source_event_id)&&row.status==='succeeded')) {
        const saved=this.deps.journal.readOwnerWebReadAction(action.id as string,authority);
        if(saved.receipt)recoveredWebReads.push({sourceRevision:webReadArgsRevision(saved.request.args),result:saved.receipt.outcome.result});
      }
    }
    const runId = randomUUID();
    this.deps.journal.startRun({ runId, eventId: workRows[0]!.id as string });
    const controller = new AbortController();
    const threadKey = workRows[0]!.thread_key as string;
    this.active.set(threadKey, { runId, controller });
    const isCurrent = () => !controller.signal.aborted && this.deps.journal.isRunCurrent(runId);
    let modelFailed = false;
    let stopPresence: (() => void) | undefined;
    try {
      try { stopPresence = this.deps.startTurnPresence?.(context.threadId, runId, controller.signal, isCurrent); }
      catch { /* presence never affects the durable conversation */ }
      const admittedMessages = messages.map(item => item.inbound);
      const hydrated = this.deps.hydrateInbound
        ? await this.deps.hydrateInbound(admittedMessages, { signal: controller.signal, isCurrent, threadId: context.threadId })
        : admittedMessages;
      if (!isCurrent()) return { status: 'cancelled' };
      const quotedInbound = messages.length === 1 ? messages[0]!.inbound : undefined;
      if (this.deps.confirmSwipeRoutine && quotedInbound?.replyBound && !quotedInbound.media?.length && /^(yes|yes please|yes, please|yep|yup|sure|do it|set it up|no|no thanks|no, thanks)[.!]?$/i.test(quotedInbound.text?.trim() ?? '')) {
        const sourceEventId = workRows[0]!.id as string;
        const quoted = quotedInbound.replyToMessageId
          ? this.deps.journal.quotedReplyForEvent(sourceEventId, quotedInbound.replyToMessageId)
          : {status:'unavailable' as const};
        const routine = this.deps.journal.listOwnerRoutineToolActionsForThread(sourceEventId).find(action => {
          const reply = this.deps.journal.getReplyForEvent(action.source_event_id as string);
          if (reply?.dispatch_status !== 'accepted') return false;
          const dispatch = this.deps.journal.getDispatch(reply.dispatch_id as string);
          let ids: unknown = [];
          try { ids = dispatch?.provider_message_ids ? JSON.parse(dispatch.provider_message_ids as string) : []; }
          catch { return false; }
          if (dispatch?.status !== 'accepted' || !quotedInbound.replyToMessageId ||
              !Array.isArray(ids) || ![dispatch.provider_message_id, ...ids].includes(quotedInbound.replyToMessageId)) return false;
          const metadata = JSON.parse(action.result_json as string);
          return ['propose','propose_adjust'].includes(metadata.args?.operation);
        });
        if (quoted.status === 'found' && routine && !/^no/i.test(quotedInbound.text!.trim()) && this.deps.confirmSwipeRoutine) {
          const metadata = JSON.parse(routine.result_json as string);
          try {
            await this.deps.confirmSwipeRoutine({eventId:sourceEventId,runId,sourceRevision:quoted.sourceRevision,
              operation:metadata.args.operation === 'propose_adjust' ? 'adjust' : 'create',signal:controller.signal});
          } catch { /* Existing tool records any contacted uncertainty; no model fallback. */ }
          const action = this.deps.journal.listOwnerRoutineToolActionsForThread(sourceEventId).find(item => item.source_event_id === sourceEventId);
          if (action && ['succeeded','rejected'].includes(action.status as string) && isCurrent())
            return await this.acknowledgeOwnerRoutineTool(sourceEventId,runId);
          if (action && ['pending','executing','unknown'].includes(action.status as string)) return {status:'held'};
        }
        if (routine) {
          if (!isCurrent()) return {status:'cancelled'};
          const metadata = JSON.parse(routine.result_json as string);
          const payload = /^no/i.test(quotedInbound.text!.trim()) &&
            metadata.next_event_id === sourceEventId && metadata.next_event_accepted === true
            ? 'Okay, I did not make that change.'
            : 'I could not confirm that exact proposal. Please review the current proposal and reply directly to it.';
          return await this.postReply(this.prepareReply({replyId:randomUUID(),runId,eventId:sourceEventId,
            coveredEventIds:[sourceEventId],dispatchId:randomUUID(),kind:'interactive',
            destination:context.destination,payload,sourceRevision:OWNER_ONLY_CONTEXT_PROOF}));
        }
      }
      let quotedReply: ModelRequest['quotedReply'];
      if (messages.length > 1 && messages.some(item => item.inbound.replyBound)) {
        quotedReply = {status: 'unavailable'};
      } else if (quotedInbound?.replyBound) {
        quotedReply = quotedInbound.replyToMessageId
          ? this.deps.journal.quotedReplyForEvent(workRows[0]!.id as string, quotedInbound.replyToMessageId)
          : {status: 'unavailable'};
      }
      let acceptedProcedureReview: ModelRequest['acceptedProcedureReview'];
      if (messages.length === 1 && !projectedBrief && !quotedInbound?.replyBound && this.deps.ownerPreferenceTool) {
        const client = this.deps.ownerPreferenceTool;
        acceptedProcedureReview = this.deps.journal.acceptedOwnerProcedureReview({runId,
          accountId: client.identity.account_id, ownerId: client.identity.owner_id,
          lineKey: client.identity.line_key, channel: client.identity.channel, scopeHash: client.scopeHash},
          workRows[0]!.id as string) ?? undefined;
      }
      let generated: string | {text: string; sourceRevision?: string};
      try {
        generated = await this.deps.model.generate({ text, quotedReply, acceptedProcedureReview, recoveredWebReads, messages: hydrated, threadId: context.threadId,
          ownerId: this.deps.ownerPolicy.ownerId, runId, ownerRequestKey: workRows[0]!.id as string,
          allowOwnerReminder: workRows.length === 1 && !projectedBrief, allowOwnerTask: workRows.length === 1 && !projectedBrief, allowOwnerPreference: workRows.length === 1 && !projectedBrief, allowOwnerSkill: workRows.length === 1 && !projectedBrief, allowOwnerRoutine: workRows.length === 1 && !projectedBrief, allowOwnerGmailDraft: workRows.length === 1 && !projectedBrief, allowOwnerMeetingPrepOffer:Boolean(this.deps.ownerMeetingPrepOffer)&&workRows.length===1&&!projectedBrief, signal: controller.signal });
      } catch (error) { modelFailed = true; throw error; }
      if (this.deps.ownerReminderTool) {
        const actions = this.deps.journal.listOwnerReminderToolActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['pending', 'executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string))) {
          if (!isCurrent()) return {status: 'cancelled'};
          return this.acknowledgeOwnerReminderTool(workRows[0]!.id as string, runId);
        }
      }
      if (this.deps.ownerTaskTool) {
        const actions = this.deps.journal.listOwnerTaskToolActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['pending', 'executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string))) {
          if (!isCurrent()) return {status: 'cancelled'};
          return await this.acknowledgeOwnerTaskTool(workRows[0]!.id as string, runId);
        }
      }
      if (this.deps.ownerPreferenceTool) {
        const actions = this.deps.journal.listOwnerPreferenceToolActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['pending', 'executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string))) {
          if (!isCurrent()) return {status: 'cancelled'};
          return await this.acknowledgeOwnerPreferenceTool(workRows[0]!.id as string, runId);
        }
      }
      if(this.deps.ownerWebRead && this.deps.journal.listOwnerWebReadActionsForThread(eventId).some(action=>action.source_event_id===workRows[0]!.id&&['pending','executing','unknown'].includes(action.status as string)))return {status:'held'};
      if (this.deps.ownerSkillRequest) {
        const actions = this.deps.journal.listOwnerSkillRequestActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['pending', 'executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string))) {
          if (!isCurrent()) return {status: 'cancelled'};
          return await this.acknowledgeOwnerSkillRequest(workRows[0]!.id as string, runId);
        }
      }
      if (this.deps.ownerRoutineTool) {
        const actions = this.deps.journal.listOwnerRoutineToolActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['pending', 'executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string))) {
          if (!isCurrent()) return {status: 'cancelled'};
          return this.acknowledgeOwnerRoutineTool(workRows[0]!.id as string, runId);
        }
      }
      if (this.deps.ownerGmailDraft) {
        const actions = this.deps.journal.listOwnerGmailDraftActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['pending', 'executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string))) {
          if (!isCurrent()) return {status: 'cancelled'};
          return await this.acknowledgeOwnerGmailDraft(workRows[0]!.id as string, runId);
        }
      }
      if(this.deps.ownerMeetingPrepOffer){
        const actions=this.deps.journal.listOwnerMeetingPrepOfferActionsForThread(eventId).filter(action=>action.source_event_id===workRows[0]!.id);
        if(actions.some(action=>['pending','executing','unknown'].includes(action.status as string)))return {status:'held'};
        if(actions.some(action=>['succeeded','rejected'].includes(action.status as string))){
          if(!isCurrent())return {status:'cancelled'};
          return await this.acknowledgeOwnerMeetingPrepOffer(workRows[0]!.id as string,runId);
        }
      }
    if (this.deps.ownerCalendar) {
        const actions = this.deps.journal.listOwnerCalendarActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['pending', 'executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string))) {
          if (!isCurrent()) return {status: 'cancelled'};
          return await this.acknowledgeOwnerCalendar(workRows[0]!.id as string, runId);
        }
      }
    if(this.deps.ownerWebRead) {
      const preview=this.deps.journal.listOwnerWebReadActionsForThread(eventId).find(action=>action.source_event_id===workRows[0]!.id&&action.status==='succeeded'&&JSON.parse(action.result_json as string).preview);
      if(preview){if(!isCurrent())return {status:'cancelled'};return await this.acknowledgeOwnerDocsend(workRows[0]!.id as string,runId);}
    }
    if (this.deps.ownerContacts) {
        const actions = this.deps.journal.listOwnerContactsActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['pending', 'executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string))) {
          if (!isCurrent()) return {status: 'cancelled'};
          return await this.acknowledgeOwnerContacts(workRows[0]!.id as string, runId);
        }
      }
    if (this.deps.ownerBridgeSend) {
        const actions = this.deps.journal.listOwnerBridgeSendActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['pending', 'executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string))) {
          if (!isCurrent()) return {status: 'cancelled'};
          return await this.acknowledgeOwnerBridgeSend(workRows[0]!.id as string, runId);
        }
      }
      let briefAck = projectedBrief ? 'Your brief request is queued.\n\n' : '';
      if (this.deps.ownerBriefRequest && !projectedBrief) {
        const action = this.deps.journal.listOwnerBriefRequestActionsForThread(eventId)
          .find(action => action.source_event_id === workRows[0]!.id);
        if (action) {
          if (!isCurrent()) return {status: 'cancelled'};
          const saved = this.deps.journal.readOwnerBriefRequestAction(action.id as string, this.briefAuthority());
          if (!saved.receipt && !saved.rejection) return {status: 'held'};
          briefAck = saved.receipt ? 'Your brief request is queued.\n\n' : 'I could not queue your brief request.\n\n';
        }
      }
      // Keep the generated answer and its source proof in the ordinary durable reply path.
      const answer = typeof generated === 'string' ? generated : generated.text;
      // The receipt owns the acknowledgement, but the model may already have used its
      // exact wording. Keep that one copy and preserve the rest of a mixed answer.
      const response = (briefAck && answer.trimStart().startsWith(briefAck.trimEnd())
        ? answer : briefAck + answer).trimEnd();
      const sourceRevision = typeof generated === 'string' ? undefined : generated.sourceRevision;
      if (controller.signal.aborted || !this.deps.journal.isRunCurrent(runId)) return { status: 'cancelled' };
      if (!response.trim()) {
        this.deps.journal.completeRun(runId);
        for (const row of workRows) this.deps.journal.resolveInbound(row.id as string, 'resolved');
        return { status: 'silent' };
      }
      const replyId = randomUUID();
      const dispatchId = randomUUID();
      const reply = this.prepareReply({ replyId, runId,
        eventId: workRows[0]!.id as string, coveredEventIds: workRows.map(row => row.id as string),
        dispatchId, kind: 'interactive', destination: context.destination, payload: response, sourceRevision });
      if (controller.signal.aborted || !this.deps.journal.isRunCurrent(runId)) {
        this.deps.journal.cancelPreparedDispatch(dispatchId);
        return { status: 'cancelled' };
      }
      return await this.postReply(reply);
    } catch (error) {
      if (controller.signal.aborted) return { status: 'cancelled' };
      if (modelFailed && isCurrent() && this.deps.journal.listUnknownActionsForThread(eventId)
        .some(action => action.run_id === runId)) return {status: 'held'};
      if(this.deps.ownerWebRead && isCurrent() && this.deps.journal.listOwnerWebReadActionsForThread(eventId).some(action=>action.source_event_id===workRows[0]!.id&&['executing','unknown'].includes(action.status as string)))return {status:'held'};
      if (this.deps.ownerBriefRequest && !projectedBrief && isCurrent() && this.deps.journal.listOwnerBriefRequestActionsForThread(eventId)
          .some(action => action.source_event_id === workRows[0]!.id))
        return (await this.processRequestedBrief(workRows[0]!.id as string, true)) ?? {status: 'held'};
      if (this.deps.ownerReminderTool && isCurrent()) {
        const actions = this.deps.journal.listOwnerReminderToolActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string)))
          return this.acknowledgeOwnerReminderTool(workRows[0]!.id as string, runId);
        if (actions.some(action => ['executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
      }
      if (this.deps.ownerTaskTool && isCurrent()) {
        const actions = this.deps.journal.listOwnerTaskToolActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string)))
          return await this.acknowledgeOwnerTaskTool(workRows[0]!.id as string, runId);
        if (actions.some(action => ['executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
      }
      if (this.deps.ownerPreferenceTool && isCurrent()) {
        const actions = this.deps.journal.listOwnerPreferenceToolActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string)))
          return await this.acknowledgeOwnerPreferenceTool(workRows[0]!.id as string, runId);
        if (actions.some(action => ['executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
      }
      if (this.deps.ownerSkillRequest && isCurrent()) {
        const actions = this.deps.journal.listOwnerSkillRequestActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string)))
          return await this.acknowledgeOwnerSkillRequest(workRows[0]!.id as string, runId);
        if (actions.some(action => ['executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
      }
      if (this.deps.ownerRoutineTool && isCurrent()) {
        const actions = this.deps.journal.listOwnerRoutineToolActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string)))
          return this.acknowledgeOwnerRoutineTool(workRows[0]!.id as string, runId);
        if (actions.some(action => ['executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
      }
      if (this.deps.ownerGmailDraft && isCurrent()) {
        const actions = this.deps.journal.listOwnerGmailDraftActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string)))
          return await this.acknowledgeOwnerGmailDraft(workRows[0]!.id as string, runId);
        if (actions.some(action => ['executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
      }
      if (this.deps.ownerCalendar && isCurrent()) {
        const actions = this.deps.journal.listOwnerCalendarActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string)))
          return await this.acknowledgeOwnerCalendar(workRows[0]!.id as string, runId);
        if (actions.some(action => ['executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
      }
    if (this.deps.ownerContacts && isCurrent()) {
        const actions = this.deps.journal.listOwnerContactsActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string)))
          return await this.acknowledgeOwnerContacts(workRows[0]!.id as string, runId);
        if (actions.some(action => ['executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
      }
    if (this.deps.ownerBridgeSend && isCurrent()) {
        const actions = this.deps.journal.listOwnerBridgeSendActionsForThread(eventId).filter(action => action.source_event_id === workRows[0]!.id);
        if (actions.some(action => ['succeeded', 'rejected'].includes(action.status as string)))
          return await this.acknowledgeOwnerBridgeSend(workRows[0]!.id as string, runId);
        if (actions.some(action => ['executing', 'unknown'].includes(action.status as string))) return {status: 'held'};
      }
    if(this.deps.ownerWebRead&&isCurrent()) {
      const preview=this.deps.journal.listOwnerWebReadActionsForThread(eventId).find(action=>action.source_event_id===workRows[0]!.id&&action.status==='succeeded'&&JSON.parse(action.result_json as string).preview);
      if(preview){if(!isCurrent())return {status:'cancelled'};return await this.acknowledgeOwnerDocsend(workRows[0]!.id as string,runId);}
    }
      if (modelFailed && error instanceof ManagedProxyBudgetExhausted && isCurrent() && !this.closed && !this.suspended) {
        const reply = this.prepareReply({replyId: randomUUID(), runId,
          eventId: workRows[0]!.id as string, coveredEventIds: workRows.map(row => row.id as string),
          dispatchId: randomUUID(), kind: 'interactive', destination: context.destination,
          payload: error.period === 'calendar_month'
            ? 'I couldn’t continue because the remaining monthly model spending allowance is too low to start another request. It resets at the start of the next UTC month; please ask me again then.'
            : error.period === 'lifetime'
              ? 'I couldn’t continue because the remaining model spending allowance is too low to start another request. An operator needs to change the allowance before I can continue.'
              : 'I couldn’t continue because the current model spending allowance is too low to start another request. Please ask me again when it is available.',
          sourceRevision: OWNER_ONLY_CONTEXT_PROOF, purpose: 'model_failure'});
        return await this.postReply(reply);
      }
      if (modelFailed && error instanceof ManagedProxyUpstreamUnavailable && isCurrent() && !this.closed && !this.suspended) {
        const reply = this.prepareReply({replyId: randomUUID(), runId,
          eventId: workRows[0]!.id as string, coveredEventIds: workRows.map(row => row.id as string),
          dispatchId: randomUUID(), kind: 'interactive', destination: context.destination,
          payload: 'I couldn’t complete a response because the model service is temporarily unavailable. Please try again shortly. If you asked me to take an action, check its status before retrying.',
          sourceRevision: OWNER_ONLY_CONTEXT_PROOF, purpose: 'model_failure'});
        return await this.postReply(reply);
      }
      if (modelFailed && isCurrent() && !this.closed && !this.suspended) {
        const payload = error instanceof SourceAuthorityChanged
          ? 'I couldn’t use that result because source permissions changed or could not be verified while I was working. Please check the current source access and ask me again. If you asked me to take an action, check its status before retrying.'
          : error instanceof ManagedProxyAdmissionBusy
          ? 'I’m busy with another model request right now. Please try again shortly. If you asked me to take an action, check its status before retrying.'
          : error instanceof ManagedProxyRequestTooLarge
            ? 'That request was too large for the model. Please send a shorter request or fewer attachments. If you asked me to take an action, check its status before retrying.'
            : 'I couldn’t complete a response because the model request failed or took too long. Please try again shortly. If you asked me to take an action, check its status before retrying.';
        const reply = this.prepareReply({replyId: randomUUID(), runId,
          eventId: workRows[0]!.id as string, coveredEventIds: workRows.map(row => row.id as string),
          dispatchId: randomUUID(), kind: 'interactive', destination: context.destination,
          payload, sourceRevision: OWNER_ONLY_CONTEXT_PROOF, purpose: 'model_failure'});
        return await this.postReply(reply);
      }
      return { status: 'error', reason: 'model failed' };
    } finally {
      try { stopPresence?.(); } catch { /* presence cleanup cannot hold the model turn */ }
      if (this.active.get(threadKey)?.runId === runId) this.active.delete(threadKey);
    }
  }

  private async heldOwnerAcknowledgement(eventId: string, existingRunId?: string, message?:string): Promise<ConversationResult> {
    // Keep diagnostics content-free even when the caught error contains owner text.
    console.error('sotto owner acknowledgement held by exception');
    // A reply was already contacted (or is unusable) for this source: hold only this
    // source for the bounded cadence, so fresh owner input in the thread still proceeds.
    const holdSource = (): ConversationResult => { this.holdAcknowledgementSource(eventId); return {status: 'held'}; };
    try {
      const prior = this.deps.journal.getReplyForEvent(eventId);
      if (prior) {
        if (prior.dispatch_status === 'prepared' && isHeldOutcomeNotice(prior)) {
          const run = this.deps.journal.getRun(prior.run_id as string);
          if (!run || run.event_id !== eventId) return holdSource();
          if (run.status === 'cancelled') this.deps.journal.cancelPreparedDispatch(prior.dispatch_id as string);
          else return this.postReply(prior);
        }
        return holdSource();
      }
      const row = this.deps.journal.getInbound(eventId);
      if (!row || row.outcome !== 'pending' || this.closed || this.suspended) return {status: 'held'};
      const runId = existingRunId ?? randomUUID();
      if (!existingRunId) this.deps.journal.startRun({runId, eventId});
      if (!this.deps.journal.isRunCurrent(runId)) return {status: 'held'};
      const reply = this.prepareReply({replyId: randomUUID(), runId, eventId,
        coveredEventIds: [eventId], dispatchId: randomUUID(), kind: 'interactive',
        destination: decode(row.payload as string).context.destination,
        purpose: 'acknowledgement_unavailable',
        payload: message??'I couldn’t confirm the result of that request yet. Please check before trying again. You can keep chatting in the meantime.',
        sourceRevision: OWNER_ONLY_CONTEXT_PROOF});
      return this.postReply(reply);
    } catch { return holdSource(); }
  }

  private ownerReminderToolAuthority() {
    const client = this.deps.ownerReminderTool!;
    return {accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash};
  }

  private async acknowledgeOwnerReminderTool(eventId: string, existingRunId?: string): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId);
    if (!row || row.outcome !== 'pending') return {status: 'held'};
    try {
      const action = this.deps.journal.listOwnerReminderToolActionsForThread(eventId).find(item => item.source_event_id === eventId);
      if (!action) return {status: 'held'};
      const saved = this.deps.journal.readOwnerReminderToolAction(action.id as string, this.ownerReminderToolAuthority());
      if (!['succeeded', 'rejected'].includes(saved.status)) return {status: 'held'};
      const runId = existingRunId ?? randomUUID();
      if (!existingRunId) this.deps.journal.startRun({runId, eventId});
      if (!this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      const payload = saved.receipt
        ? saved.receipt.reminder_status === 'fired' ? 'That reminder was already sent.'
          : saved.receipt.reminder_status === 'canceled' ? 'That reminder is canceled.'
          : saved.receipt.operation === 'conditional' ? 'Conditional reminder set.' : 'Reminder set.'
        : 'That reminder could not be changed because the target or time is unavailable or it conflicts with a saved request.';
      const reply = this.prepareReply({replyId: randomUUID(), runId, eventId, coveredEventIds: [eventId],
        dispatchId: randomUUID(), kind: 'interactive', destination: decode(row.payload as string).context.destination,
        payload, sourceRevision: OWNER_ONLY_CONTEXT_PROOF});
      if (!this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      return this.postReply(reply);
    } catch {return this.heldOwnerAcknowledgement(eventId, existingRunId);}
  }

  private ownerTaskToolAuthority() {
    const client = this.deps.ownerTaskTool!;
    return {accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash};
  }

  private async acknowledgeOwnerTaskTool(eventId: string, existingRunId?: string): Promise<ConversationResult> {
    const acknowledgementStarted=performance.now();
    const row = this.deps.journal.getInbound(eventId);
    if (!row || row.outcome !== 'pending') return {status: 'held'};
    let ownsActive=false,activeRun:string|undefined;
    const threadKey=row.thread_key as string;
    try {
      const action = this.deps.journal.listOwnerTaskToolActionsForThread(eventId).find(item => item.source_event_id === eventId);
      if (!action) return {status: 'held'};
      const saved = this.deps.journal.readOwnerTaskToolAction(action.id as string, this.ownerTaskToolAuthority());
      if (!['succeeded', 'rejected'].includes(saved.status)) return {status: 'held'};
      const runId = existingRunId ?? randomUUID();
      if (!existingRunId) this.deps.journal.startRun({runId, eventId});
      if (!this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      let replySourceRevision=OWNER_ONLY_CONTEXT_PROOF;
      let payload = saved.rejection==='similar_fact_exists'?SIMILAR_FACT_TEXT:(saved.request.args.operation === 'propose_fact'||saved.request.args.operation === 'fact_add')?'That fact could not be recorded because its reviewed person, owner wording or permission changed.':saved.request.args.operation === 'add_identifier'?'That identifier could not be recorded because its person or owner wording is unavailable, or it conflicts with an existing identity.':saved.request.args.operation === 'fact_correct'
        ? 'That correction could not be recorded because its exact fact or owner wording is unavailable, or it conflicts with a saved request.'
        : 'That task change could not be recorded because its target or owner wording is unavailable, or it conflicts with a saved request.';
      if (saved.receipt) {
        payload = saved.receipt.operation==='fact_add'?(saved.receipt.added?'Fact recorded.':'That fact is already recorded.'):saved.receipt.operation==='cleanup'?cleanupAcknowledgement(saved.receipt):saved.receipt.operation==='resolve_many'?resolveManyAcknowledgement(saved.receipt,saved.request.args as Extract<typeof saved.request.args,{operation:'resolve_many'}>):saved.receipt.operation==='relation'?(saved.receipt.mode==='add'?(saved.receipt.changed?'Relationship added.':'That relationship is already recorded.'):(saved.receipt.changed?'Relationship removed.':'That relationship is already absent.')):(saved.receipt.operation === 'add_identifier'||saved.receipt.operation==='identifier')?(saved.receipt.added?'Identifier added.':'That identifier is already known.'):saved.receipt.operation === 'fact_correct'
          ? 'Correction recorded.'
          : saved.receipt.operation==='add'&&saved.request.args.operation==='add'
            ? `Added: ${saved.request.args.text}`
            : saved.receipt.operation==='close'?'Marked that task complete.'
            : saved.receipt.operation==='correct'?'Task correction recorded.'
            : saved.receipt.operation==='retarget'?'Task updated.'
            : saved.receipt.operation==='snooze'?'Task snoozed.'
            : saved.receipt.operation==='keep'?'Task kept open.'
            : 'Task change recorded.';
      }
      if(saved.preview){payload=taskReviewText(saved.preview.review);replySourceRevision=saved.preview.review.source_revision;}
      if(saved.receipt?.operation==='cleanup'||saved.receipt?.operation==='resolve_many'){try{const counts=await this.deps.ownerTaskTool!.readback(saved.request.args,saved.receipt);replySourceRevision=counts.source_revision;
        if(saved.receipt.operation==='cleanup')payload+='\nYour currently available list has '+counts.you_owe+' you owe and '+counts.waiting_on_them+' waiting on others.';
        else if(ownerTaskReadback(saved.request.text)&&'remaining' in counts&&Array.isArray(counts.remaining)){
          const rows=counts.remaining;const clean=(value:string)=>value.replace(/\s+/g,' ').trim();
          const scoped=Boolean(ownerTaskReadback(saved.request.text)?.scope);
          // The receipt proves completion, not an interpretation of urgency, channel or task type.
          // A qualified readback must never silently hide source-derived obligations.
          payload+=rows.length?'\n'+(scoped?'Here’s the full remaining list:':'Still open:')+'\n'+rows.slice(0,8).map(row=>`• ${clean(row.name)||'Task'}: ${clean(row.what)}`).join('\n')+(rows.length>8?`\n…and ${rows.length-8} more.`:''):'\nNothing else is open in the sources I can currently see.';
        }
      }catch{payload+='\nI couldn’t verify the current list.';}}
      if(saved.receipt && ((saved.request.args.operation==='add'&&saved.request.args.direction!==undefined) || saved.request.args.operation==='correct' || saved.request.args.operation==='retarget' || saved.request.args.operation==='snooze'||saved.request.args.operation==='keep')) {
        const label=saved.request.args.operation==='add'?'Task added':saved.request.args.operation==='keep'?'Kept this obligation':saved.request.args.operation==='snooze' && saved.receipt.operation==='snooze'?`Obligation snoozed until ${saved.receipt.snoozed_until}`:saved.request.args.operation==='retarget'?'Obligation updated':saved.request.args.operation==='correct' && saved.request.args.to==='resolved'?'Marked done':'Obligation dismissed';
        if(this.closed || this.suspended)return {status:'cancelled'};
        const currentActive=this.active.get(threadKey);
        const controller=currentActive?.runId===runId?currentActive.controller:new AbortController();
        if(!currentActive){this.active.set(threadKey,{runId,controller});ownsActive=true;activeRun=runId;}
        try {
          const counts=await this.deps.ownerTaskTool!.readback(saved.request.args,saved.receipt,controller.signal);
          replySourceRevision=counts.source_revision;
          payload=`${label}. Your currently available list has ${counts.you_owe} you owe and ${counts.waiting_on_them} waiting on others.`;
        } catch {payload=saved.receipt.operation==='snooze'?`A snooze until ${saved.receipt.snoozed_until} was recorded, but I couldn’t verify that it’s hidden now.`:`${label} was recorded, but I couldn’t verify the current list.`;}
      }
      if(this.closed || this.suspended || !this.deps.journal.isRunCurrent(runId))return {status:'cancelled'};
      const reply = this.prepareReply({replyId: randomUUID(), runId, eventId, coveredEventIds: [eventId],
        dispatchId: randomUUID(), kind: 'interactive', destination: decode(row.payload as string).context.destination,
        payload, sourceRevision: replySourceRevision});
      if (!this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      const dispatchStarted=performance.now();
      const result=await this.postReply(reply);
      const finished=Date.now();
      try{this.deps.onOwnerTaskTiming?.({
        acknowledgementMs:Math.max(0,Math.floor(dispatchStarted-acknowledgementStarted)),
        dispatchMs:Math.max(0,Math.floor(performance.now()-dispatchStarted)),
        receiptToDispatchMs:Math.max(0,finished-Number(action.updated_at)),
        admissionToDispatchMs:Math.max(0,finished-saved.request.admittedAtMs),outcome:result.status});}catch{/* Diagnostics cannot change delivery. */}
      return result;
    } catch {return this.heldOwnerAcknowledgement(eventId, existingRunId);}
    finally {if(ownsActive && this.active.get(threadKey)?.runId===activeRun)this.active.delete(threadKey);}
  }

  private ownerPreferenceToolAuthority() {
    const client = this.deps.ownerPreferenceTool!;
    return {accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash};
  }

  private async acknowledgeOwnerPreferenceTool(eventId: string, existingRunId?: string): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId);
    if (!row || row.outcome !== 'pending') return {status: 'held'};
    const threadKey=row.thread_key as string;let ownedRun:string|undefined;
    try {
      const action = this.deps.journal.listOwnerPreferenceToolActionsForThread(eventId).find(item => item.source_event_id === eventId);
      if (!action) return {status: 'held'};
      const saved = this.deps.journal.readOwnerPreferenceToolAction(action.id as string, this.ownerPreferenceToolAuthority());
      if (!['succeeded', 'rejected'].includes(saved.status)) return {status: 'held'};
      const runId = existingRunId ?? randomUUID();
      if(!existingRunId){if(this.closed||this.active.has(threadKey))return {status:'held'};this.deps.journal.startRun({runId,eventId});this.active.set(threadKey,{runId,controller:new AbortController()});ownedRun=runId;}
      if (!this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      const payload = saved.preview?procedureReviewText(saved.preview.review):saved.receipt
        ? preferenceAcknowledgement(saved.receipt)
        : saved.rejection==='target_unavailable'&&saved.request.args?.operation==='unsnooze-nudges'&&normalizeOwnerQuietText(saved.request.text).toLowerCase()==='back to normal' ? 'Do you mean end the temporary quiet period or restore your permanent nudge limit?'
        : saved.rejection === 'priorities_full' ? 'Your three priorities are already full. Tell me the complete replacement set if you want to change them.'
        : saved.rejection === 'master_capacity' ? saved.capacity?.largest_section?`Your standing-rule file is full. The largest ${saved.capacity.largest_section.name_truncated?'section begins':'section is'} ${saved.capacity.largest_section.name} (${saved.capacity.largest_section.chars} characters). What would you like to trim?`:['propose-procedure','append-procedure','propose-priorities','replace-priorities','propose-context','append-context'].includes(saved.request.args!.operation)?'Your standing-rule file is full, so I couldn’t save that rule. What would you like to trim?':'Your standing-rule file is full, so I couldn’t save that rule.'
        : 'That preference change could not be recorded because its exact target or owner wording is unavailable, or it conflicts with a saved request.';
      const reply = this.prepareReply({replyId: randomUUID(), runId, eventId, coveredEventIds: [eventId],
        dispatchId: randomUUID(), kind: 'interactive', destination: decode(row.payload as string).context.destination,
        payload, sourceRevision: OWNER_ONLY_CONTEXT_PROOF});
      if (!this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      return await this.postReply(reply);
    } catch {return this.heldOwnerAcknowledgement(eventId, existingRunId);} finally{if(ownedRun&&this.active.get(threadKey)?.runId===ownedRun)this.active.delete(threadKey);}
  }

  private ownerSkillRequestAuthority() {
    const client = this.deps.ownerSkillRequest!;
    return {accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash};
  }

  private async acknowledgeOwnerSkillRequest(eventId: string, existingRunId?: string): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId);
    if (!row || row.outcome !== 'pending') return {status: 'held'};
    try {
      const action = this.deps.journal.listOwnerSkillRequestActionsForThread(eventId).find(item => item.source_event_id === eventId);
      if (!action) return {status: 'held'};
      const saved = this.deps.journal.readOwnerSkillRequestAction(action.id as string, this.ownerSkillRequestAuthority());
      if (!['succeeded', 'rejected'].includes(saved.status)) return {status: 'held'};
      const runId = existingRunId ?? randomUUID();
      if (!existingRunId) this.deps.journal.startRun({runId, eventId});
      if (!this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      if (saved.receipt?.outcome.skill === 'proactive') {
        if (!this.deps.journal.completeRun(runId)) return {status: 'cancelled'};
        this.deps.journal.resolveInbound(eventId, 'resolved');
        const threadId = row.thread_key as string;
        const fresh = this.freshFeedback.get(threadId);
        if (fresh?.eventId === eventId) {
          if (existingRunId && !this.closed && !this.suspended) {
            try {this.deps.onOwnerSilentWorkAccepted?.(fresh.inbound, fresh.providerThreadId);}
            catch { /* Committed work remains accepted when best-effort feedback fails. */ }
          }
          this.freshFeedback.delete(threadId);
        }
        return {status: 'silent'};
      }
      const payload = saved.receipt
        ? skillAcknowledgement(saved.receipt)
        : skillRejectionAcknowledgement(saved.request.args.skill, saved.rejection!);
      const reply = this.prepareReply({replyId: randomUUID(), runId, eventId, coveredEventIds: [eventId],
        dispatchId: randomUUID(), kind: 'interactive', destination: decode(row.payload as string).context.destination,
        payload, sourceRevision: OWNER_ONLY_CONTEXT_PROOF});
      if (!this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      return this.postReply(reply);
    } catch {return this.heldOwnerAcknowledgement(eventId, existingRunId);}
  }

  private ownerRoutineToolAuthority() {
    const client = this.deps.ownerRoutineTool!;
    return { accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash };
  }

  private async acknowledgeOwnerRoutineTool(eventId: string, existingRunId?: string): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId);
    if (!row || row.outcome !== 'pending')
      return { status: 'held' };
    try {
      const action = this.deps.journal.listOwnerRoutineToolActionsForThread(eventId).find(item => item.source_event_id === eventId);
      if (!action)
        return { status: 'held' };
      const saved = this.deps.journal.readOwnerRoutineToolAction(action.id as string, this.ownerRoutineToolAuthority());
      if (!['succeeded', 'rejected'].includes(saved.status))
        return { status: 'held' };
      const runId = existingRunId ?? randomUUID();
      if (!existingRunId)
        this.deps.journal.startRun({ runId, eventId });
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      const payload = saved.preview ? routineProposalText(saved.preview) : saved.receipt
        ? routineAcknowledgement(saved.receipt,saved.request.args??undefined)
        : 'That routine change could not be recorded because its exact target or owner wording is unavailable, or it conflicts with a saved request.';
      const reply = this.prepareReply({ replyId: randomUUID(), runId, eventId, coveredEventIds: [eventId],
        dispatchId: randomUUID(), kind: 'interactive', destination: decode(row.payload as string).context.destination,
        payload, sourceRevision: saved.preview ? saved.sourceRevision : OWNER_ONLY_CONTEXT_PROOF });
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      return this.postReply(reply);
    }
    catch {
      return this.heldOwnerAcknowledgement(eventId, existingRunId);
    }
  }

  private ownerGmailDraftAuthority() {
    const client = this.deps.ownerGmailDraft!;
    return { accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash };
  }

  private ownerMeetingPrepOfferAuthority(){
    const client=this.deps.ownerMeetingPrepOffer!;
    return {accountId:this.deps.ownerPolicy.accountId,ownerId:this.deps.ownerPolicy.ownerId,
      channel:this.deps.ownerPolicy.channel,lineKey:client.identity.line_key,scopeHash:client.scopeHash};
  }

  private async acknowledgeOwnerMeetingPrepOffer(eventId:string,existingRunId?:string):Promise<ConversationResult>{
    const row=this.deps.journal.getInbound(eventId);
    if(!row||row.outcome!=='pending')return {status:'held'};
    const threadKey=row.thread_key as string;
    let ownedRun:string|undefined;
    try{
      const action=this.deps.journal.listOwnerMeetingPrepOfferActionsForThread(eventId).find(item=>item.source_event_id===eventId);
      if(!action)return {status:'held'};
      const saved=this.deps.journal.readOwnerMeetingPrepOfferAction(action.id as string,this.ownerMeetingPrepOfferAuthority());
      if(!['succeeded','rejected'].includes(saved.status))return {status:'held'};
      const runId=existingRunId??randomUUID();
      if(!existingRunId){
        if(this.closed||this.active.has(threadKey))return {status:'held'};
        this.deps.journal.startRun({runId,eventId});
        this.active.set(threadKey,{runId,controller:new AbortController()});
        ownedRun=runId;
      }
      if(!this.deps.journal.isRunCurrent(runId))return {status:'cancelled'};
      const payload=saved.preview?ownerMeetingPrepOfferQuestion(saved.preview.review):
        'I could not offer meeting preparation because the current event, attendee, or permission could not be verified.';
      const reply=this.prepareReply({replyId:randomUUID(),runId,eventId,coveredEventIds:[eventId],dispatchId:randomUUID(),
        kind:'interactive',destination:decode(row.payload as string).context.destination,payload,
        sourceRevision:saved.preview?saved.sourceRevision:OWNER_ONLY_CONTEXT_PROOF});
      if(!this.deps.journal.isRunCurrent(runId))return {status:'cancelled'};
      return await this.postReply(reply);
    }catch{return this.heldOwnerAcknowledgement(eventId,existingRunId);}
    finally{if(ownedRun&&this.active.get(threadKey)?.runId===ownedRun)this.active.delete(threadKey);}
  }

  private async recordAcceptedMeetingPrepOffer(actionId:string,signal:AbortSignal):Promise<void>{
    if(!this.deps.ownerMeetingPrepOffer||this.closed||this.suspended||signal.aborted)return;
    try{
      const authority=this.ownerMeetingPrepOfferAuthority();
      const action=this.deps.journal.readOwnerMeetingPrepOfferAction(actionId,authority);
      if(action.receipt||action.refusal||action.status!=='succeeded'||!action.preview)return;
      const saved=this.deps.journal.acceptedOwnerMeetingPrepOffer(actionId,authority);
      if(!saved)return;
      const refuse=(reason:OwnerMeetingPrepOfferRefusal)=>
        this.deps.journal.refuseOwnerMeetingPrepOfferReceipt(actionId,authority,reason);
      const started=Date.parse(action.preview.review.start);
      if(Date.now()-saved.request.admittedAtMs>=PREP_OFFER_MAX_AGE_MS){refuse('expired');return;}
      if(Number.isFinite(started)&&started<=Date.now()){refuse('event_started');return;}
      if(this.deps.canPostPrepared){
        let stop:(()=>void)|undefined;
        try{
          const permitted=await Promise.race([this.deps.canPostPrepared(saved.acceptedDelivery.source_revision),
            new Promise<false>(resolve=>{stop=()=>resolve(false);signal.addEventListener('abort',stop,{once:true});if(signal.aborted)stop();})]);
          if(signal.aborted)return;
          if(!permitted){refuse('source_changed');return;}
        }finally{if(stop)signal.removeEventListener('abort',stop);}
      }
      if(this.closed||this.suspended||signal.aborted)return;
      const receipt=await this.deps.ownerMeetingPrepOffer.recordOffer(saved.request,saved.acceptedDelivery,signal);
      if(this.closed||this.suspended||signal.aborted)return;
      if('status' in receipt&&receipt.status==='rejected'){
        if(receipt.code==='event_conflict')refuse('event_conflict');
        else if(receipt.code==='invalid_target')refuse('invalid_target');
        return;
      }
      this.deps.journal.settleOwnerMeetingPrepOfferReceipt(actionId,authority,receipt);
    }catch{/* The accepted question stays accepted; startup recovery may repair this exact offer. */}
  }

  /** Repair only already-accepted questions before a later affirmative can consume them. */
  private async repairAcceptedMeetingPrepOffers(threadKey:string,providerMessageId?:string):Promise<boolean>{
    if(!this.deps.ownerMeetingPrepOffer)return true;
    if(!providerMessageId)return true;
    const authority=this.ownerMeetingPrepOfferAuthority();
    const pending=this.deps.journal.listPendingOwnerMeetingPrepOffers().filter(action=>{
      if(action.thread_key!==threadKey)return false;
      try{
        const saved=this.deps.journal.readOwnerMeetingPrepOfferAction(String(action.id),authority);
        if(saved.refusal||saved.receipt)return false;
        const accepted=this.deps.journal.acceptedOwnerMeetingPrepOffer(String(action.id),authority);
        return accepted?.acceptedDelivery.provider_message_id===providerMessageId;
      }catch{return false;}
    });
    if(!pending.length)return true;
    const selectedIds=new Set(pending.map(action=>String(action.id)));
    const controller=new AbortController();
    const deadline=setTimeout(()=>controller.abort(),5000);
    try{
      for(const action of pending){
        if(this.closed||this.suspended||controller.signal.aborted)return false;
        await this.recordAcceptedMeetingPrepOffer(action.id as string,controller.signal);
      }
    }finally{clearTimeout(deadline);}
    return !this.deps.journal.listPendingOwnerMeetingPrepOffers().some(action=>selectedIds.has(String(action.id)));
  }

  private async acknowledgeOwnerGmailDraft(eventId: string, existingRunId?: string): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId);
    if (!row || row.outcome !== 'pending')
      return { status: 'held' };
    const threadKey = row.thread_key as string;
    let ownedRun: string | undefined;
    try {
      const action = this.deps.journal.listOwnerGmailDraftActionsForThread(eventId).find(item => item.source_event_id === eventId);
      if (!action)
        return { status: 'held' };
      const saved = this.deps.journal.readOwnerGmailDraftAction(action.id as string, this.ownerGmailDraftAuthority());
      if (!['succeeded', 'rejected'].includes(saved.status))
        return { status: 'held' };
      const runId = existingRunId ?? randomUUID();
      if (!existingRunId) {
        if (this.closed || this.active.has(threadKey)) return {status:'held'};
        this.deps.journal.startRun({runId,eventId});
        this.active.set(threadKey,{runId,controller:new AbortController()});
        ownedRun = runId;
      }
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      const payload = saved.preview ? gmailDraftReviewText(saved.preview) : saved.receipt
        ? (saved.receipt.outcome.operation==='send' ? 'Email sent.' : 'Saved your Gmail draft. Nothing was sent.')+(saved.receipt.learning==='pending'?' I couldn’t save the feedback from this draft yet.':'')
        : saved.request.args.operation==='send' || saved.request.args.operation==='propose_send' || saved.request.args.operation==='propose_send_typed' ? 'The email reply could not be sent because its reviewed target or permission is unavailable, changed, or conflicts with a saved request.' : 'The Gmail draft could not be saved because its reviewed target or permission is unavailable, changed, or conflicts with a saved request.';
      const reply = this.prepareReply({ replyId: randomUUID(), runId, eventId, coveredEventIds: [eventId],
        dispatchId: randomUUID(), kind: 'interactive', destination: decode(row.payload as string).context.destination,
        payload, sourceRevision: saved.preview ? saved.sourceRevision : OWNER_ONLY_CONTEXT_PROOF });
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      return await this.postReply(reply);
    }
    catch {
      return this.heldOwnerAcknowledgement(eventId, existingRunId);
    } finally {
      if (ownedRun && this.active.get(threadKey)?.runId===ownedRun) this.active.delete(threadKey);
    }
  }

  private ownerCalendarAuthority() {
    const client = this.deps.ownerCalendar!;
    return { accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash };
  }

  private async acknowledgeOwnerCalendar(eventId: string, existingRunId?: string): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId);
    if (!row || row.outcome !== 'pending')
      return { status: 'held' };
    const threadKey=row.thread_key as string;
    let ownedRun:string|undefined;
    try {
      const action = this.deps.journal.listOwnerCalendarActionsForThread(eventId).find(item => item.source_event_id === eventId);
      if (!action)
        return { status: 'held' };
      const saved = this.deps.journal.readOwnerCalendarAction(action.id as string, this.ownerCalendarAuthority());
      if (!['succeeded', 'rejected'].includes(saved.status))
        return { status: 'held' };
      const runId = existingRunId ?? randomUUID();
      if (!existingRunId) {
        if(this.closed||this.active.has(threadKey))return {status:"held"};
        this.deps.journal.startRun({runId,eventId});
        this.active.set(threadKey,{runId,controller:new AbortController()});ownedRun=runId;
      }
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      let creationAcknowledgement: string | undefined;
      if (saved.receipt?.outcome.operation === 'create' && saved.request.args.operation === 'create') {
        const review = saved.request.args.review, outcome = saved.receipt.outcome;
        creationAcknowledgement = `Added ${review.summary} to your calendar.\n${calendarWhenText(review.start, review.end)}`
          + (review.location ? `\nLocation: ${review.location}` : '')
          + `\n${outcome.html_link}`
          + (outcome.organizer_listed === false ? '\nYour address was not listed among the guests.' : '');
      }
      const payload = saved.preview ? calendarReviewText(saved.preview) : saved.receipt
        ? saved.receipt.outcome.operation==='move_update'?'Updated the meeting time and details. Guest notifications were requested.':saved.receipt.outcome.operation==='update'?`Updated ${saved.request.args.operation==='update'&&saved.request.args.review.event_scope==='series'?'the entire series':saved.request.args.operation==='update'&&saved.request.args.review.event_scope==='occurrence'?'that occurrence':'the event'}. Guest notifications were requested.`: saved.receipt.outcome.operation === 'create' ? creationAcknowledgement! : saved.receipt.outcome.operation === 'move' ? 'Meeting moved. Guest notifications were requested.' : `${saved.receipt.outcome.response==='accepted'?'Accepted':saved.receipt.outcome.response==='declined'?'Declined':'Marked'} ${saved.request.args.operation==='rsvp'&&saved.request.args.review.event_scope==='series'?'the entire series':'that invitation'}${saved.receipt.outcome.response==='tentative'?' tentative':''}. Notifications were requested.`
        : ['propose_update','update','propose_move_update','move_update'].includes(saved.request.args.operation)?'I couldn’t update the event because the reviewed details or permission are unavailable or changed.': ['propose_create','create'].includes(saved.request.args.operation) ? 'I couldn’t create the event because the reviewed details or permission are unavailable or changed.' : ['propose_rsvp','rsvp'].includes(saved.request.args.operation) ? 'I couldn’t update the invitation response because the reviewed event or permission is unavailable or changed.' : 'I couldn’t move the meeting because the reviewed event or permission is unavailable or changed.';
      const reply = this.prepareReply({ replyId: randomUUID(), runId, eventId, coveredEventIds: [eventId],
        dispatchId: randomUUID(), kind: 'interactive', destination: decode(row.payload as string).context.destination,
        payload, sourceRevision: saved.preview ? saved.sourceRevision : OWNER_ONLY_CONTEXT_PROOF });
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      return await this.postReply(reply);
    }
    catch {
      return this.heldOwnerAcknowledgement(eventId, existingRunId);
    } finally {if(ownedRun&&this.active.get(threadKey)?.runId===ownedRun)this.active.delete(threadKey);}
  }
  private ownerContactsAuthority() {
    const client = this.deps.ownerContacts!;
    return { accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash };
  }

  private async acknowledgeOwnerContacts(eventId: string, existingRunId?: string): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId);
    if (!row || row.outcome !== 'pending')
      return { status: 'held' };
    const threadKey=row.thread_key as string;
    let ownedRun:string|undefined;
    try {
      const action = this.deps.journal.listOwnerContactsActionsForThread(eventId).find(item => item.source_event_id === eventId);
      if (!action)
        return { status: 'held' };
      const saved = this.deps.journal.readOwnerContactsAction(action.id as string, this.ownerContactsAuthority());
      if (!['succeeded', 'rejected'].includes(saved.status))
        return { status: 'held' };
      const runId = existingRunId ?? randomUUID();
      if (!existingRunId) {
        if(this.closed||this.active.has(threadKey))return {status:"held"};
        this.deps.journal.startRun({runId,eventId});
        this.active.set(threadKey,{runId,controller:new AbortController()});ownedRun=runId;
      }
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      const payload=saved.preview?contactsReviewText(saved.preview):saved.receipt?`${saved.receipt.outcome.operation==='create'?'Created':saved.receipt.outcome.operation==='update'?'Updated':'Deleted'} the Google contact.`:'I couldn’t change the contact because the reviewed details or permission are unavailable or changed.';
      const reply = this.prepareReply({ replyId: randomUUID(), runId, eventId, coveredEventIds: [eventId],
        dispatchId: randomUUID(), kind: 'interactive', destination: decode(row.payload as string).context.destination,
        payload, sourceRevision: saved.preview ? saved.sourceRevision : OWNER_ONLY_CONTEXT_PROOF });
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      return await this.postReply(reply);
    }
    catch {
      return this.heldOwnerAcknowledgement(eventId, existingRunId);
    } finally {if(ownedRun&&this.active.get(threadKey)?.runId===ownedRun)this.active.delete(threadKey);}
  }

  private ownerBridgeSendAuthority() {
    const client = this.deps.ownerBridgeSend!;
    return { accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash };
  }

  private async acknowledgeOwnerBridgeSend(eventId: string, existingRunId?: string): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId);
    if (!row || row.outcome !== 'pending')
      return { status: 'held' };
    const threadKey=row.thread_key as string;
    let ownedRun:string|undefined;
    try {
      const action = this.deps.journal.listOwnerBridgeSendActionsForThread(eventId).find(item => item.source_event_id === eventId);
      if (!action)
        return { status: 'held' };
      const saved = this.deps.journal.readOwnerBridgeSendAction(action.id as string, this.ownerBridgeSendAuthority());
      if (!['succeeded', 'rejected'].includes(saved.status))
        return { status: 'held' };
      const runId = existingRunId ?? randomUUID();
      if (!existingRunId) {
        if(this.closed||this.active.has(threadKey))return {status:"held"};
        this.deps.journal.startRun({runId,eventId});
        this.active.set(threadKey,{runId,controller:new AbortController()});ownedRun=runId;
      }
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      const payload=saved.preview?bridgeSendReviewText(saved.preview):saved.receipt?'Sent from your Mac. Delivery to the recipient isn’t confirmed yet.':saved.rejection==='invalid_target'?'I couldn’t use that recipient. Please give me their phone number or email.':saved.rejection==='review_unavailable'||saved.rejection==='authority_changed'?'That message review is no longer current. Ask me again so you can review it before sending.':'I couldn’t send that message. Check that your Mac is awake and sending is enabled, then ask me again.';
      const reply = this.prepareReply({ replyId: randomUUID(), runId, eventId, coveredEventIds: [eventId],
        dispatchId: randomUUID(), kind: 'interactive', destination: decode(row.payload as string).context.destination,
        payload, sourceRevision: saved.preview ? saved.sourceRevision : OWNER_ONLY_CONTEXT_PROOF });
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      return await this.postReply(reply);
    }
    catch {
      return this.heldOwnerAcknowledgement(eventId, existingRunId);
    } finally {if(ownedRun&&this.active.get(threadKey)?.runId===ownedRun)this.active.delete(threadKey);}
  }

  private ownerWebReadAuthority() {
    const client = this.deps.ownerWebRead!;
    return { accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: client.identity.line_key, scopeHash: client.scopeHash };
  }

  private async acknowledgeOwnerDocsend(eventId: string, existingRunId?: string, recoveredRead = false): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId);
    if (!row || row.outcome !== 'pending')
      return { status: 'held' };
    const threadKey=row.thread_key as string;
    let ownedRun:string|undefined;
    try {
      const action = this.deps.journal.listOwnerWebReadActionsForThread(eventId).find(item => item.source_event_id === eventId);
      if (!action)
        return { status: 'held' };
      const saved = this.deps.journal.readOwnerWebReadAction(action.id as string, this.ownerWebReadAuthority());
      if(saved.status!=='succeeded'||(!saved.preview && !(recoveredRead && saved.receipt)))
        return { status: 'held' };
      const runId = existingRunId ?? randomUUID();
      if (!existingRunId) {
        if(this.closed||this.active.has(threadKey))return {status:"held"};
        this.deps.journal.startRun({runId,eventId});
        this.active.set(threadKey,{runId,controller:new AbortController()});ownedRun=runId;
      }
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      const payload=saved.preview ? docsendReviewText(saved.preview)
        : 'I recovered the URL-read result, but couldn’t finish your requested response. Please ask me again if you still need it.';
      const reply = this.prepareReply({ replyId: randomUUID(), runId, eventId, coveredEventIds: [eventId],
        dispatchId: randomUUID(), kind: 'interactive', destination: decode(row.payload as string).context.destination,
        payload, ...(saved.preview ? {} : {purpose:'acknowledgement_unavailable' as const}),
        sourceRevision:saved.preview ? saved.sourceRevision! : OWNER_ONLY_CONTEXT_PROOF });
      if (!this.deps.journal.isRunCurrent(runId))
        return { status: 'cancelled' };
      return await this.postReply(reply);
    }
    catch {
      return this.heldOwnerAcknowledgement(eventId, existingRunId);
    } finally {if(ownedRun&&this.active.get(threadKey)?.runId===ownedRun)this.active.delete(threadKey);}
  }

  private ownerLoopRequest(intent: { actionId: string; sourceEventId: string; sourcePayloadHash: string;
    text: string; admittedAtMs: number; zone: string | null; providerThreadId: string }): OwnerLoopRequest {
    return { actionId: intent.actionId, eventId: intent.sourceEventId, payloadHash: intent.sourcePayloadHash,
      text: intent.text, admittedAtMs: intent.admittedAtMs, zone: intent.zone,
      providerThreadId: intent.providerThreadId };
  }

  private ownerLoopReceiptHash(eventId: string, anchor: string): string {
    return createHash('sha256').update(JSON.stringify(['sotto.owner-loop-receipt.v1', eventId, anchor])).digest('hex');
  }

  private ownerLoopCloseRequest(intent: { actionId: string; sourceEventId: string; sourcePayloadHash: string;
    text: string; anchorKey: string; expectedRowDigest: string | null; admittedAtMs: number;
    zone: string | null; providerThreadId: string }): OwnerLoopCloseRequest {
    return { ...this.ownerLoopRequest(intent), anchorKey: intent.anchorKey,
      expectedRowDigest: intent.expectedRowDigest };
  }

  private ownerLoopCloseReceiptHash(eventId: string, anchor: string, postDigest: string): string {
    return createHash('sha256').update(JSON.stringify(['sotto.owner-loop-close-receipt.v1',
      eventId, anchor, postDigest])).digest('hex');
  }

  private ownerReminderRequest(intent: { actionId: string; sourceEventId: string; sourcePayloadHash: string;
    text: string; admittedAtMs: number; providerThreadId: string }): OwnerReminderRequest {
    return { actionId: intent.actionId, eventId: intent.sourceEventId,
      payloadHash: intent.sourcePayloadHash, text: intent.text,
      admittedAtMs: intent.admittedAtMs, providerThreadId: intent.providerThreadId };
  }

  private ownerReminderReceiptHash(receipt: { operation: string; action_id: string; event_id: string;
    payload_hash: string; reminder_id: string; reminder_status: string }): string {
    return createHash('sha256').update(JSON.stringify(['sotto.owner-reminder-receipt.v1',
      receipt.operation, receipt.action_id, receipt.event_id, receipt.payload_hash,
      receipt.reminder_id, receipt.reminder_status])).digest('hex');
  }

  private ownerQuietRequest(intent: { actionId: string; sourceEventId: string; sourcePayloadHash: string;
    text: string; admittedAtMs: number; providerThreadId: string; zone: string | null }): OwnerQuietRequest {
    return { actionId: intent.actionId, eventId: intent.sourceEventId,
      payloadHash: intent.sourcePayloadHash, text: intent.text,
      admittedAtMs: intent.admittedAtMs, providerThreadId: intent.providerThreadId, zone: intent.zone };
  }

  private ownerQuietReceiptHash(receipt: { action_id: string; event_id: string;
    payload_hash: string; zone: string; snooze_until: string }): string {
    return createHash('sha256').update(JSON.stringify(['sotto.owner-quiet-receipt.v1',
      receipt.action_id, receipt.event_id, receipt.payload_hash,
      receipt.zone, receipt.snooze_until])).digest('hex');
  }

  private briefAuthority() {
    return {accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: this.deps.ownerBriefRequest!.identity.line_key,
      scopeHash: this.deps.ownerBriefRequest!.scopeHash};
  }

  private async processRequestedBrief(eventId: string, acknowledgeOnly = false): Promise<ConversationResult | undefined> {
    const row = this.deps.journal.getInbound(eventId)!;
    const saved = decode(row.payload as string);
    const previous = this.deps.journal.listOwnerBriefRequestActionsForThread(eventId).find(action => action.source_event_id === eventId);
    const original = previous ? this.deps.journal.readOwnerBriefRequestAction(previous.id as string, this.briefAuthority()) : null;
    const classified = original?.request.args ?? classifyRequestedBrief(saved.inbound)!;
    const runId = randomUUID();
    this.deps.journal.startRun({runId, eventId});
    const controller = new AbortController();
    const threadKey = row.thread_key as string;
    this.active.set(threadKey, {runId, controller});
    try {
      const authority = {...this.briefAuthority(), runId};
      let request = original?.request;
      if (!request) try {
        request = this.deps.journal.prepareOwnerBriefRequestAction(authority, eventId,
          {...classified, provider_message_id: row.source_id as string});
      } catch (error) {
        // A refused admission saved nothing to inspect; answer it so it never wedges the thread.
        if (this.deps.journal.listOwnerBriefRequestActionsForThread(eventId).some(action => action.source_event_id === eventId)) throw error;
      }
      let action = request ? this.deps.journal.readOwnerBriefRequestAction(request.actionId, authority)
        : {status: 'rejected', receipt: null, rejection: null};
      if (action.status === 'unknown' || action.status === 'executing') return {status: 'held'};
      if (request && action.status === 'pending') {
        this.deps.journal.beginAction(request.actionId);
        try {
          const receipt = await this.deps.ownerBriefRequest!.call('admit', request, controller.signal);
          if (receipt.status === 'committed') this.deps.journal.settleOwnerBriefRequestAction(request.actionId, authority, receipt);
          else if (receipt.status === 'rejected') this.deps.journal.settleOwnerBriefRequestAction(request.actionId, authority, receipt.code);
          else throw new Error('Invalid requested brief receipt');
        } catch {
          this.deps.journal.settleAction(request.actionId, 'unknown');
          return {status: 'held'};
        }
        action = this.deps.journal.readOwnerBriefRequestAction(request.actionId, authority);
      }
      if (controller.signal.aborted || !this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      if (!acknowledgeOnly && action.receipt && classified.remainder && isReadChatBriefRemainder(classified.remainder)) {
        this.deps.journal.cancelRun(runId);
        return undefined;
      }
      const payload = action.receipt ? 'Your brief request is queued.' + (classified.remainder ? ' Please ask the other part in a separate message.' : '')
        : action.rejection === 'pending_delivery'
          ? 'I couldn’t queue a new brief because an earlier brief’s delivery is still unconfirmed. I’m keeping that delivery on hold to avoid sending it twice.'
          : action.rejection === 'needs_context'
            ? 'I could not queue your brief because its context is not ready yet.'
            : 'I could not queue your brief request.';
      const reply = this.prepareReply({replyId: randomUUID(), runId, eventId,
        coveredEventIds: [eventId], dispatchId: randomUUID(), kind: 'interactive',
        destination: saved.context.destination, payload: payload + (!action.receipt && classified.remainder
          ? ' The other part was not processed; please ask it separately.' : ''),
        // An unconfirmed earlier delivery is uncertainty, not completion feedback.
        ...(action.rejection === 'pending_delivery' ? {purpose: 'acknowledgement_unavailable' as const} : {}),
        sourceRevision: OWNER_ONLY_CONTEXT_PROOF});
      return await this.postReply(reply);
    } catch { return {status: 'held'}; }
    finally { if (this.active.get(threadKey)?.runId === runId) this.active.delete(threadKey); }
  }

  private prepareReply(input: Parameters<OperationalJournal['prepareReply']>[0]) {
    const row = this.deps.journal.getInbound(input.eventId)!;
    const context = decode(String(row.payload)).context;
    return this.deps.journal.prepareReply({...input,
      routeDigest: this.deps.routeDigestForThread?.(context.threadId)});
  }

  private async acknowledgeControl(eventId: string, destination: string, payload: string,
                                   coveredEventIds: string[] = [eventId]): Promise<ConversationResult> {
    const command = decode(this.deps.journal.getInbound(eventId)!.payload as string).inbound.text?.trim().toLowerCase();
    const completed = command === '/stop' ? 'stopped' : 'rotated';
    const priors = this.deps.journal.listRepliesForThread(eventId)
      .filter(row => row.event_id === eventId && row.purpose === 'owner_control');
    // Only a definite non-delivery (rejected, or cancelled before contact) may be replaced.
    const prior = priors.find(row => !['rejected', 'cancelled'].includes(row.dispatch_status as string));
    if (prior) {
      if (prior.payload !== payload || JSON.stringify(coveredEventIds) !== prior.covered_event_ids)
        return {status: 'held'};
      const result = await this.postReply(prior);
      return result.status === 'accepted' ? {status: completed} : result;
    }
    if (priors.length >= CONTROL_ACK_MAX_DISPATCHES) {
      // The control itself was applied; stop offering it after bounded definite non-delivery.
      for (const id of coveredEventIds) if (this.deps.journal.getInbound(id)?.outcome === 'pending')
        this.deps.journal.resolveInbound(id, 'resolved');
      return {status: completed};
    }
    const runId = randomUUID();
    this.deps.journal.startRun({runId, eventId});
    const reply = this.prepareReply({replyId: randomUUID(), runId, eventId, coveredEventIds,
      dispatchId: randomUUID(), kind: 'interactive', destination, payload,
      sourceRevision: OWNER_ONLY_CONTEXT_PROOF, purpose: 'owner_control'});
    const result = await this.postReply(reply);
    return result.status === 'accepted' ? {status: completed} : result;
  }

  /** Recover exact background ordering for context, never infer an action from the reply. */
  private async freezeFreshBackgroundContinuation(rows: Record<string, unknown>[]): Promise<boolean> {
    if (rows.length !== 1 || !this.deps.resolvePrepContinuation) return true;
    const row = rows[0]!, eventId = String(row.id);
    const inbound = decode(String(row.payload)).inbound;
    const fresh = this.freshFeedback.get(String(row.thread_key));
    // Only this process's newly admitted, single unquoted input may read provider
    // ordering. Duplicate ingress, recovery and a second pass cannot acquire proof.
    if (!inbound.text || inbound.replyBound || inbound.media?.length ||
        this.deps.journal.continuationArgs(eventId) || !fresh || fresh.eventId !== eventId ||
        fresh.providerSentAtMs === undefined || fresh.backgroundProofChecked) return true;
    const offerAuthority=this.deps.ownerMeetingPrepOffer?this.ownerMeetingPrepOfferAuthority():undefined;
    const candidate = this.deps.journal.prepContinuationCandidate(eventId,
      this.deps.routeDigestForThread?.(fresh.providerThreadId) ?? '',offerAuthority);
    if (!candidate) return true;
    fresh.backgroundProofChecked = true;
    const current = () => !this.closed && !this.suspended
      && this.freshFeedback.get(String(row.thread_key))?.eventId === eventId
      && this.deps.journal.getInbound(eventId)?.outcome === 'pending';
    const proof = await this.deps.resolvePrepContinuation({threadId: fresh.providerThreadId,
      inboundId: inbound.eventId, inboundAtMs: fresh.providerSentAtMs, candidateAtMs: candidate.providerSentAtMs}).catch(() => null);
    if (!current()) return false;
    if (proof) this.deps.journal.freezeProviderContinuation(eventId,
      this.deps.routeDigestForThread?.(fresh.providerThreadId) ?? '', fresh.providerSentAtMs, proof,
      candidate.ownerMeetingPrepOfferActionId&&offerAuthority
        ? {actionId:candidate.ownerMeetingPrepOfferActionId,authority:offerAuthority}:undefined);
    return true;
  }

  private async previewContinuation(rows: Record<string, unknown>[], kind: 'prep' | 'decline'): Promise<string | ConversationResult | null | undefined> {
    for (const row of rows) {
      const actionKind = kind === 'prep' ? 'owner_meeting_prep_request' : 'owner_offer_reply';
      const existing = this.deps.journal.db.prepare('SELECT id FROM actions WHERE source_event_id=? AND kind=?').get(String(row.id), actionKind);
      if (existing) return String(row.id);
      const inbound = decode(String(row.payload)).inbound;
      if (!inbound.text || (inbound.replyBound && kind !== 'prep') || inbound.media?.length ||
          (kind === 'prep' ? !clearMeetingPrepYes(inbound.text) : ownerOfferReplyAction(inbound.text) !== 'declined')) continue;
      if (kind === 'prep' && !await this.freezeFreshBackgroundContinuation(rows)) return undefined;
      const args = (kind === 'prep' ? meetingPrepSelectors(inbound) : null) ?? this.deps.journal.continuationArgs(String(row.id));
      if(kind==='prep'&&rows.length===1&&!inbound.replyBound&&args?.selected_offer?.message_id&&this.deps.ownerMeetingPrepOffer&&
         !await this.repairAcceptedMeetingPrepOffers(String(row.thread_key),args.selected_offer.message_id))
        return this.heldOwnerAcknowledgement(String(row.id),undefined,
          'I couldn’t verify the meeting prep request just now, so I haven’t started preparing it. Please ask me directly to prepare it again.');
      if (!args || this.isLocalProposalReply(String(row.id), args.reply_to_message_id ?? args.selected_offer!.message_id)) continue;
      const client = kind === 'prep' ? this.deps.ownerMeetingPrep! : this.deps.ownerOfferReply!;
      const {stableActionIdentity} = await import('./tools/action-gate.js');
      const request = {actionId: stableActionIdentity(String(row.id), actionKind, args).actionId,
        eventId: String(row.id), payloadHash: String(row.payload_hash), text: inbound.text,
        admittedAtMs: Number(row.received_at), providerThreadId: decode(String(row.payload)).context.threadId, args};
      const latest = () => this.deps.journal.db.prepare('SELECT max(rowid) AS n FROM inbound_events WHERE thread_key=?').get(String(row.thread_key))!.n;
      const sequence = latest();
      const eventSequence = this.deps.journal.db.prepare('SELECT rowid AS n FROM inbound_events WHERE id=?').get(String(row.id))!.n;
      if (eventSequence !== sequence) continue;
      const current = () => !this.closed && !this.suspended
        && this.deps.journal.getInbound(String(row.id))?.outcome === 'pending' && latest() === sequence;
      try {
        const result = await client.call('preview', request);
        if (!current()) return undefined;
        if (result.status === 'match') return String(row.id);
      } catch {
        if (!current()) return undefined;
        // Resolve this one input only through the existing accepted fixed reply;
        // no action or model interpretation is authorized by an unavailable read.
        const eventId = String(row.id), runId = randomUUID();
        this.deps.journal.startRun({runId, eventId});
        const saved = decode(String(row.payload));
        const threadKey = String(row.thread_key);
        this.active.set(threadKey, {runId, controller: new AbortController()});
        try {
          return await this.postReply(this.prepareReply({replyId: randomUUID(), runId, eventId,
            coveredEventIds: [eventId], dispatchId: randomUUID(), kind: 'interactive',
            destination: saved.context.destination,
            payload: 'I couldn’t confirm which offer that answers. Please reply directly to the offer.',
            purpose: 'acknowledgement_unavailable', sourceRevision: OWNER_ONLY_CONTEXT_PROOF}));
        } finally {
          if (this.active.get(threadKey)?.runId === runId) this.active.delete(threadKey);
        }
      }
    }
    return null;
  }

  private isLocalProposalReply(eventId: string, providerId: string): boolean {
    const actions = [...this.deps.journal.listOwnerPreferenceToolActionsForThread(eventId),...this.deps.journal.listOwnerRoutineToolActionsForThread(eventId),
      ...this.deps.journal.listOwnerGmailDraftActionsForThread(eventId), ...this.deps.journal.listOwnerBridgeSendActionsForThread(eventId),
      ...this.deps.journal.listOwnerCalendarActionsForThread(eventId)];
    return actions.some(action => {
      const metadata = JSON.parse(action.result_json as string);
      if (!['propose','propose_adjust','propose_offer','propose_followup','propose_send','propose_typed','propose_send_typed','propose_move','propose_rsvp','propose_create','propose-procedure','propose-priorities','propose-context'].includes(metadata.args?.operation)) return false;
      const reply = this.deps.journal.getReplyForEvent(action.source_event_id as string);
      if (!reply) return false;
      const dispatch = this.deps.journal.getDispatch(reply.dispatch_id as string);
      if (!dispatch || dispatch.status !== 'accepted') return false;
      const ids = dispatch.provider_message_ids ? JSON.parse(dispatch.provider_message_ids as string) : [];
      return [dispatch.provider_message_id, ...ids].includes(providerId);
    });
  }

  private meetingPrepAuthority() {
    return {accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: this.deps.ownerMeetingPrep!.identity.line_key,
      scopeHash: this.deps.ownerMeetingPrep!.scopeHash};
  }

  private async processMeetingPrep(eventId: string): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId)!;
    const saved = decode(row.payload as string);
    const classified = meetingPrepSelectors(saved.inbound) ?? this.deps.journal.continuationArgs(eventId)!;
    const runId = randomUUID();
    this.deps.journal.startRun({runId, eventId});
    const controller = new AbortController();
    const threadKey = row.thread_key as string;
    this.active.set(threadKey, {runId, controller});
    try {
      const authority = {...this.meetingPrepAuthority(), runId};
      const request = this.deps.journal.prepareOwnerMeetingPrepAction(authority, eventId,
        {...classified, provider_message_id: row.source_id as string});
      let action = this.deps.journal.readOwnerMeetingPrepAction(request.actionId, authority);
      if (action.status === 'unknown' || action.status === 'executing') return {status: 'held'};
      if (action.status === 'pending') {
        this.deps.journal.beginAction(request.actionId);
        try {
          const receipt = await this.deps.ownerMeetingPrep!.call('admit', request, controller.signal);
          if (receipt.status === 'committed') this.deps.journal.settleOwnerMeetingPrepAction(request.actionId, authority, receipt);
          else if (receipt.status === 'rejected') this.deps.journal.settleOwnerMeetingPrepAction(request.actionId, authority, receipt.code);
          else throw new Error('Invalid meeting prep receipt');
        } catch {
          this.deps.journal.settleAction(request.actionId, 'unknown');
          return {status: 'held'};
        }
        action = this.deps.journal.readOwnerMeetingPrepAction(request.actionId, authority);
      }
      if (controller.signal.aborted || !this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      const payload = action.receipt ? 'Your meeting prep is queued.'
        : 'I could not identify an available meeting-prep offer from that reply. Please reply directly to a current meeting-prep offer.';
      const reply = this.prepareReply({replyId: randomUUID(), runId, eventId,
        coveredEventIds: [eventId], dispatchId: randomUUID(), kind: 'interactive',
        destination: saved.context.destination, payload, sourceRevision: OWNER_ONLY_CONTEXT_PROOF});
      return await this.postReply(reply);
    } catch { return {status: 'held'}; }
    finally { if (this.active.get(threadKey)?.runId === runId) this.active.delete(threadKey); }
  }

  private offerReplyAuthority() {
    return {accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
      channel: this.deps.ownerPolicy.channel, lineKey: this.deps.ownerOfferReply!.identity.line_key,
      scopeHash: this.deps.ownerOfferReply!.scopeHash};
  }

  private async processOfferReply(eventId: string): Promise<ConversationResult> {
    const row = this.deps.journal.getInbound(eventId)!;
    const saved = decode(row.payload as string);
    const classified = offerReplySelectors(saved.inbound) ?? this.deps.journal.continuationArgs(eventId)!;
    const runId = randomUUID();
    this.deps.journal.startRun({runId, eventId});
    const controller = new AbortController();
    const threadKey = row.thread_key as string;
    this.active.set(threadKey, {runId, controller});
    try {
      const authority = {...this.offerReplyAuthority(), runId};
      const request = this.deps.journal.prepareOwnerOfferReplyAction(authority, eventId,
        {...classified, provider_message_id: row.source_id as string});
      let action = this.deps.journal.readOwnerOfferReplyAction(request.actionId, authority);
      if (action.status === 'unknown' || action.status === 'executing') return {status: 'held'};
      if (action.status === 'pending') {
        this.deps.journal.beginAction(request.actionId);
        try {
          const receipt = await this.deps.ownerOfferReply!.call('apply', request, controller.signal);
          if (receipt.status === 'committed') this.deps.journal.settleOwnerOfferReplyAction(request.actionId, authority, receipt);
          else if (receipt.status === 'rejected') this.deps.journal.settleOwnerOfferReplyAction(request.actionId, authority, receipt.code);
          else throw new Error('Invalid offer reply receipt');
        } catch {
          this.deps.journal.settleAction(request.actionId, 'unknown');
          return {status: 'held'};
        }
        action = this.deps.journal.readOwnerOfferReplyAction(request.actionId, authority);
      }
      if (controller.signal.aborted || !this.deps.journal.isRunCurrent(runId)) return {status: 'cancelled'};
      const payload = !action.receipt ? 'I could not apply that reply to a current offer. Please reply directly to the offer you mean.'
        : action.receipt.outcome.action === 'declined' ? 'I will leave that for now.'
        : action.receipt.outcome.action === 'resolved' ? 'Marked that as handled.'
        : 'Stopped tracking that loop.';
      const reply = this.prepareReply({replyId: randomUUID(), runId, eventId,
        coveredEventIds: [eventId], dispatchId: randomUUID(), kind: 'interactive',
        destination: saved.context.destination, payload, sourceRevision: OWNER_ONLY_CONTEXT_PROOF});
      return await this.postReply(reply);
    } catch { return {status: 'held'}; }
    finally { if (this.active.get(threadKey)?.runId === runId) this.active.delete(threadKey); }
  }

  private async processOwnerQuietCommand(eventId: string, workRows: Record<string, unknown>[]): Promise<ConversationResult> {
    const client = this.deps.ownerQuiet!;
    const row = this.deps.journal.getInbound(eventId)!;
    const saved = decode(row.payload as string);
    const runId = randomUUID();
    this.deps.journal.startRun({ runId, eventId });
    const controller = new AbortController();
    const threadKey = row.thread_key as string;
    this.active.set(threadKey, { runId, controller });
    const current = () => !controller.signal.aborted && this.deps.journal.isRunCurrent(runId);
    try {
      if (workRows.filter(item => decode(item.payload as string).inbound.text?.startsWith('/quiet')).length !== 1) {
        return { status: 'held' };
      }
      const parsed = parseOwnerQuietCommand(saved.inbound.text ?? '');
      let payload: string;
      if (!parsed) {
        payload = 'Use /quiet for <positive number>h or <positive number>m (up to 24 hours), or /quiet off.';
      } else {
        const authority = { runId, accountId: this.deps.ownerPolicy.accountId,
          ownerId: this.deps.ownerPolicy.ownerId, lineKey: saved.context.lineKey,
          channel: this.deps.ownerPolicy.channel, scopeHash: client.scopeHash };
        const intent = this.deps.journal.prepareExplicitOwnerQuietIntent(authority);
        if (intent.sourceEventId !== eventId || intent.text !== saved.inbound.text) return { status: 'held' };
        if (intent.status === 'unknown' || intent.status === 'executing') return { status: 'held' };
        let zone = intent.zone;
        let until = intent.snoozeUntil;
        let rejected = intent.status === 'rejected';
        if (intent.status === 'pending') {
          if (zone === null) {
            const prepared = await client.call('prepare', this.ownerQuietRequest(intent), controller.signal);
            if (prepared.status !== 'prepared' || !current()) return { status: 'held' };
            zone = prepared.zone;
            this.deps.journal.bindExplicitOwnerQuietZone(intent.actionId, zone);
          }
          if (!current()) return { status: 'cancelled' };
          this.deps.journal.beginAction(intent.actionId);
          try {
            const receipt = await client.call('write', { ...this.ownerQuietRequest(intent), zone }, controller.signal);
            if (receipt.status === 'committed') {
              this.deps.journal.settleExplicitOwnerQuietIntent(intent.actionId,
                this.ownerQuietReceiptHash(receipt), receipt.zone, receipt.snooze_until);
              until = receipt.snooze_until;
            } else if (receipt.status === 'rejected') {
              this.deps.journal.rejectExplicitOwnerQuietIntent(intent.actionId, receipt.code);
              rejected = true;
            } else throw new Error('owner quiet receipt is invalid');
          } catch {
            try { this.deps.journal.settleAction(intent.actionId, 'unknown'); } catch { /* proof remains authoritative */ }
            return { status: 'held' };
          }
        }
        if (rejected) payload = 'That quiet change could not be applied because the owner event conflicts with a saved command.';
        else if (zone === null || until === null) return { status: 'held' };
        else payload = parsed.mode === 'off'
          ? 'Recorded your request to resume nudges. Ordinary quiet hours remain in effect.'
          : `Recorded your nudge pause until ${until} (${zone}). Briefs and ordinary quiet hours are unchanged.`;
      }
      if (!current()) return { status: 'cancelled' };
      const reply = this.prepareReply({ replyId: randomUUID(), runId, eventId,
        coveredEventIds: [eventId], dispatchId: randomUUID(), kind: 'interactive',
        destination: saved.context.destination, payload, sourceRevision: OWNER_ONLY_CONTEXT_PROOF });
      if (!current()) {
        this.deps.journal.cancelPreparedDispatch(reply.dispatch_id as string);
        return { status: 'cancelled' };
      }
      return await this.postReply(reply);
    } catch { return { status: 'held' }; }
    finally { if (this.active.get(threadKey)?.runId === runId) this.active.delete(threadKey); }
  }

  private async processOwnerReminderCommand(eventId: string, workRows: Record<string, unknown>[]): Promise<ConversationResult> {
    const client = this.deps.ownerReminder!;
    const row = this.deps.journal.getInbound(eventId)!;
    const saved = decode(row.payload as string);
    const runId = randomUUID();
    this.deps.journal.startRun({ runId, eventId });
    const controller = new AbortController();
    const threadKey = row.thread_key as string;
    this.active.set(threadKey, { runId, controller });
    const current = () => !controller.signal.aborted && this.deps.journal.isRunCurrent(runId);
    try {
      if (workRows.filter(item => decode(item.payload as string).inbound.text?.startsWith('/reminder')).length !== 1) {
        return { status: 'held' };
      }
      const parsed = parseOwnerReminderCommand(saved.inbound.text ?? '');
      const authority = { runId, accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
        lineKey: saved.context.lineKey, channel: this.deps.ownerPolicy.channel, scopeHash: client.scopeHash };
      let payload: string;
      if (!parsed) {
        payload = 'Use /reminder at <ISO 8601 time with offset> <text>, /reminder list, or /reminder cancel <reminder ID>.';
      } else if (parsed.operation === 'list') {
        const intent = this.deps.journal.validateExplicitOwnerReminderList(authority);
        if (intent.sourceEventId !== eventId) return { status: 'held' };
        const listed = await client.call('list', this.ownerReminderRequest(intent), controller.signal);
        if (!current() || listed.status !== 'listed') return { status: 'held' };
        const shown = listed.items.slice(0, 10);
        payload = shown.length ? 'Scheduled reminders:\n' + shown.map(item => {
          const words = Array.from(item.action);
          return `${item.id} at ${item.due}: ${words.slice(0, 100).join('')}${words.length > 100 ? '…' : ''}`;
        }).join('\n') + (listed.items.length > shown.length || shown.some(item => Array.from(item.action).length > 100)
          ? `\nShowing the first ${shown.length} scheduled reminders; long descriptions may be shortened.` : '')
          : 'You have no scheduled reminders.';
      } else {
        const intent = this.deps.journal.prepareExplicitOwnerReminderIntent(authority);
        if (intent.sourceEventId !== eventId || intent.operation !== parsed.operation) return { status: 'held' };
        if (intent.status === 'unknown' || intent.status === 'executing') return { status: 'held' };
        let reminderId = intent.reminderId;
        let reminderStatus = intent.reminderStatus;
        let rejected = intent.status === 'rejected';
        if (intent.status === 'pending') {
          if (!current()) return { status: 'cancelled' };
          this.deps.journal.beginAction(intent.actionId);
          try {
            const receipt = await client.call(intent.operation, this.ownerReminderRequest(intent), controller.signal);
            if (receipt.status === 'committed' && receipt.operation === intent.operation) {
              this.deps.journal.settleExplicitOwnerReminderIntent(intent.actionId,
                this.ownerReminderReceiptHash(receipt), receipt.reminder_id, receipt.reminder_status);
              reminderId = receipt.reminder_id;
              reminderStatus = receipt.reminder_status;
            } else if (receipt.status === 'rejected') {
              this.deps.journal.rejectExplicitOwnerReminderIntent(intent.actionId, receipt.code);
              rejected = true;
            } else throw new Error('owner reminder receipt is invalid');
          } catch {
            try { this.deps.journal.settleAction(intent.actionId, 'unknown'); } catch { /* exact proof survives */ }
            return { status: 'held' };
          }
        }
        if (rejected) payload = intent.operation === 'cancel'
          ? 'That reminder could not be canceled because it is unavailable, changed, or already fired.'
          : 'That reminder could not be scheduled.';
        else if (!reminderId || !reminderStatus) return { status: 'held' };
        else if (intent.operation === 'create') payload = reminderStatus === 'fired'
          ? `Reminder ${reminderId} has already fired.` : reminderStatus === 'canceled'
            ? `Reminder ${reminderId} was created and has since been canceled.`
            : `Scheduled reminder ${reminderId}.`;
        else payload = reminderStatus === 'fired'
          ? `Reminder ${reminderId} has already fired; the cancellation did not recall that delivery.`
          : `Cancellation recorded for reminder ${reminderId}. A delivery already in progress may still arrive.`;
      }
      if (!current()) return { status: 'cancelled' };
      const reply = this.prepareReply({ replyId: randomUUID(), runId, eventId,
        coveredEventIds: [eventId], dispatchId: randomUUID(), kind: 'interactive',
        destination: saved.context.destination, payload, sourceRevision: OWNER_ONLY_CONTEXT_PROOF });
      if (!current()) {
        this.deps.journal.cancelPreparedDispatch(reply.dispatch_id as string);
        return { status: 'cancelled' };
      }
      return await this.postReply(reply);
    } catch { return { status: 'held' }; }
    finally { if (this.active.get(threadKey)?.runId === runId) this.active.delete(threadKey); }
  }

  private async processOwnerLoopCloseCommand(eventId: string, workRows: Record<string, unknown>[]): Promise<ConversationResult> {
    const client = this.deps.ownerLoop!;
    const row = this.deps.journal.getInbound(eventId)!;
    const saved = decode(row.payload as string);
    const runId = randomUUID();
    this.deps.journal.startRun({ runId, eventId });
    const controller = new AbortController();
    const threadKey = row.thread_key as string;
    this.active.set(threadKey, { runId, controller });
    const current = () => !controller.signal.aborted && this.deps.journal.isRunCurrent(runId);
    try {
      const intent = this.deps.journal.prepareExplicitOwnerLoopCloseIntent({ runId,
        accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
        lineKey: saved.context.lineKey, channel: this.deps.ownerPolicy.channel, scopeHash: client.scopeHash });
      if (intent.sourceEventId !== eventId || workRows.filter(item => {
        const text = decode(item.payload as string).inbound.text;
        return text?.startsWith('/loop add ') || text?.startsWith('/loop close ');
      }).length !== 1) return { status: 'held' };
      if (intent.status === 'unknown' || intent.status === 'executing') return { status: 'held' };
      let outcome: 'succeeded' | 'rejected' = intent.status === 'rejected' ? 'rejected' : 'succeeded';
      if (intent.status === 'pending') {
        let zone = intent.zone;
        let expectedRowDigest = intent.expectedRowDigest;
        if (zone === null || expectedRowDigest === null) {
          const prepared = await client.callClose('close_prepare',
            { ...this.ownerLoopCloseRequest(intent), zone: null, expectedRowDigest: null }, controller.signal);
          if (!current()) return { status: 'held' };
          if (prepared.status === 'rejected') {
            this.deps.journal.rejectExplicitOwnerLoopCloseIntent(intent.actionId, prepared.code);
            outcome = 'rejected';
          } else if (prepared.status === 'prepared') {
            zone = prepared.zone;
            expectedRowDigest = prepared.expected_row_digest;
            this.deps.journal.bindExplicitOwnerLoopClosePrepare(intent.actionId, zone, expectedRowDigest);
          } else return { status: 'held' };
        }
        if (outcome === 'succeeded') {
          if (!current() || zone === null || expectedRowDigest === null) return { status: 'held' };
          this.deps.journal.beginAction(intent.actionId);
          try {
            const receipt = await client.callClose('close_write',
              { ...this.ownerLoopCloseRequest(intent), zone, expectedRowDigest }, controller.signal);
            if (receipt.status === 'committed') this.deps.journal.settleExplicitOwnerLoopCloseIntent(intent.actionId,
              this.ownerLoopCloseReceiptHash(eventId, receipt.anchor_key, receipt.post_digest));
            else if (receipt.status === 'rejected') {
              this.deps.journal.rejectExplicitOwnerLoopCloseIntent(intent.actionId, receipt.code);
              outcome = 'rejected';
            } else throw new Error('owner close receipt is invalid');
          } catch {
            try { this.deps.journal.settleAction(intent.actionId, 'unknown'); } catch { /* proof remains authoritative */ }
            return { status: 'held' };
          }
        }
      }
      if (!current()) return { status: 'cancelled' };
      const replyId = randomUUID(), dispatchId = randomUUID();
      const payload = outcome === 'succeeded' ? 'Closed your open loop.' :
        'That owner-authored open loop could not be closed because it is unavailable or changed.';
      const reply = this.prepareReply({ replyId, runId, eventId,
        coveredEventIds: [eventId], dispatchId, kind: 'interactive', destination: saved.context.destination,
        payload, sourceRevision: OWNER_ONLY_CONTEXT_PROOF });
      if (!current()) {
        this.deps.journal.cancelPreparedDispatch(dispatchId);
        return { status: 'cancelled' };
      }
      return await this.postReply(reply);
    } catch { return { status: 'held' }; }
    finally {
      if (this.active.get(threadKey)?.runId === runId) this.active.delete(threadKey);
    }
  }

  private async processOwnerLoopCommand(eventId: string, workRows: Record<string, unknown>[]): Promise<ConversationResult> {
    const client = this.deps.ownerLoop!;
    const row = this.deps.journal.getInbound(eventId)!;
    const saved = decode(row.payload as string);
    const runId = randomUUID();
    this.deps.journal.startRun({ runId, eventId });
    const controller = new AbortController();
    const threadKey = row.thread_key as string;
    this.active.set(threadKey, { runId, controller });
    const current = () => !controller.signal.aborted && this.deps.journal.isRunCurrent(runId);
    try {
      const intent = this.deps.journal.prepareExplicitOwnerLoopIntent({ runId,
        accountId: this.deps.ownerPolicy.accountId, ownerId: this.deps.ownerPolicy.ownerId,
        lineKey: saved.context.lineKey, channel: this.deps.ownerPolicy.channel, scopeHash: client.scopeHash });
      if (intent.sourceEventId !== eventId || workRows.filter(item =>
          decode(item.payload as string).inbound.text?.startsWith('/loop add ')).length !== 1) return { status: 'held' };
      if (intent.status === 'unknown' || intent.status === 'executing') return { status: 'held' };
      let outcome: 'succeeded' | 'rejected' = intent.status === 'rejected' ? 'rejected' : 'succeeded';
      if (intent.status === 'pending') {
        let zone = intent.zone;
        if (zone === null) {
          const prepared = await client.call('prepare', this.ownerLoopRequest(intent), controller.signal);
          if (prepared.status !== 'prepared' || !current()) return { status: 'held' };
          zone = prepared.zone;
          this.deps.journal.bindExplicitOwnerLoopZone(intent.actionId, zone);
        }
        if (!current()) return { status: 'cancelled' };
        this.deps.journal.beginAction(intent.actionId);
        try {
          const receipt = await client.call('write', { ...this.ownerLoopRequest(intent), zone }, controller.signal);
          if (receipt.status === 'committed') this.deps.journal.settleExplicitOwnerLoopIntent(intent.actionId,
            this.ownerLoopReceiptHash(eventId, receipt.anchor_key));
          else if (receipt.status === 'rejected') {
            this.deps.journal.rejectExplicitOwnerLoopIntent(intent.actionId, receipt.code);
            outcome = 'rejected';
          } else throw new Error('owner command receipt is invalid');
        } catch {
          // A writer may have committed before the receipt or journal write failed.
          // Keep the action held even if its local status already became terminal.
          try { this.deps.journal.settleAction(intent.actionId, 'unknown'); } catch { /* proof remains authoritative */ }
          return { status: 'held' };
        }
      }
      if (!current()) return { status: 'cancelled' };
      const replyId = randomUUID(), dispatchId = randomUUID();
      const payload = outcome === 'succeeded' ? 'Added to your open loops.' : 'That loop could not be added because it conflicts with an existing loop.';
      const reply = this.prepareReply({ replyId, runId, eventId,
        coveredEventIds: [eventId], dispatchId, kind: 'interactive', destination: saved.context.destination,
        payload, sourceRevision: OWNER_ONLY_CONTEXT_PROOF });
      if (!current()) {
        this.deps.journal.cancelPreparedDispatch(dispatchId);
        return { status: 'cancelled' };
      }
      return await this.postReply(reply);
    } catch { return { status: 'held' }; }
    finally {
      if (this.active.get(threadKey)?.runId === runId) this.active.delete(threadKey);
    }
  }

  private isVerbatimReview(reply: Record<string, unknown>): boolean {
    const eventId = reply.event_id as string;
    try {
      if(this.deps.ownerPreferenceTool){
        const action=this.deps.journal.listOwnerPreferenceToolActionsForThread(eventId).find(item=>item.source_event_id===eventId);
        if(action){const saved=this.deps.journal.readOwnerPreferenceToolAction(action.id as string,this.ownerPreferenceToolAuthority());if(saved.preview&&reply.source_revision===OWNER_ONLY_CONTEXT_PROOF&&procedureReviewText(saved.preview.review)===reply.payload)return true;}
      }
      if (this.deps.ownerGmailDraft) {
        const action = this.deps.journal.listOwnerGmailDraftActionsForThread(eventId).find(item => item.source_event_id === eventId);
        if (action) {
          const saved = this.deps.journal.readOwnerGmailDraftAction(action.id as string, this.ownerGmailDraftAuthority());
          if (saved.preview && saved.sourceRevision === reply.source_revision && gmailDraftReviewText(saved.preview) === reply.payload) return true;
        }
      }
    if (this.deps.ownerCalendar) {
        const action = this.deps.journal.listOwnerCalendarActionsForThread(eventId).find(item => item.source_event_id === eventId);
        if (action) {
          const saved = this.deps.journal.readOwnerCalendarAction(action.id as string, this.ownerCalendarAuthority());
          if (saved.preview && saved.sourceRevision === reply.source_revision && calendarReviewText(saved.preview) === reply.payload) return true;
        }
      }
    if (this.deps.ownerContacts) {
        const action = this.deps.journal.listOwnerContactsActionsForThread(eventId).find(item => item.source_event_id === eventId);
        if (action) {
          const saved = this.deps.journal.readOwnerContactsAction(action.id as string, this.ownerContactsAuthority());
          if (saved.preview && saved.sourceRevision === reply.source_revision && contactsReviewText(saved.preview) === reply.payload) return true;
        }
      }
    if (this.deps.ownerBridgeSend) {
        const action = this.deps.journal.listOwnerBridgeSendActionsForThread(eventId).find(item => item.source_event_id === eventId);
        if (action) {
          const saved = this.deps.journal.readOwnerBridgeSendAction(action.id as string, this.ownerBridgeSendAuthority());
          if (saved.preview && saved.sourceRevision === reply.source_revision && bridgeSendReviewText(saved.preview) === reply.payload) return true;
        }
      }
    if(this.deps.ownerWebRead) {
      const action=this.deps.journal.listOwnerWebReadActionsForThread(eventId).find(item=>item.source_event_id===eventId);
      if(action){const saved=this.deps.journal.readOwnerWebReadAction(action.id as string,this.ownerWebReadAuthority());
        if(saved.preview&&saved.sourceRevision===reply.source_revision&&docsendReviewText(saved.preview)===reply.payload)return true;}
    }
      if (this.deps.ownerRoutineTool) {
        const action = this.deps.journal.listOwnerRoutineToolActionsForThread(eventId).find(item => item.source_event_id === eventId);
        if (action) {
          const saved = this.deps.journal.readOwnerRoutineToolAction(action.id as string, this.ownerRoutineToolAuthority());
          if (saved.preview && saved.sourceRevision === reply.source_revision && routineProposalText(saved.preview) === reply.payload) return true;
        }
      }
    } catch { /* An unvalidated preview cannot grant a verbatim exemption. */ }
    return false;
  }

  private async postReply(reply: Record<string, unknown>): Promise<ConversationResult> {
    const dispatchId = reply.dispatch_id as string;
    const dispatch = this.deps.journal.getDispatch(dispatchId);
    if (dispatch?.status === 'unknown') return { status: 'held' };
    if (dispatch?.status === 'accepted') return this.resolveAccepted(reply, dispatch);
    if (dispatch?.status !== 'prepared') return { status: 'held' };
    if (this.deps.canPostPrepared) {
      try {
        if (!await this.deps.canPostPrepared(typeof reply.source_revision === 'string' ? reply.source_revision : undefined)) return { status: 'held' };
      } catch { return { status: 'held' }; }
    }
    if (this.closed || this.suspended || !this.deps.journal.isRunCurrent(reply.run_id as string)) {
      return {status: 'held'};
    }
    let attempt: Record<string, unknown>;
    try { attempt = this.deps.journal.beginDispatch(dispatchId); }
    catch { return { status: 'held' }; }
    let receipt: PostReceipt;
    try {
      const sendController = new AbortController();
      const threadKey = this.deps.journal.getRun(reply.run_id as string)?.thread_key as string | undefined;
      let deadline: ReturnType<typeof setTimeout> | undefined;
      let onStop: (() => void) | undefined;
      try {
        const waits: Promise<PostReceipt>[] = [];
        // Notices and recovered acknowledgements have no active model run, but
        // their contacted sends must not block this thread after an owner stop.
        if (threadKey) waits.push(new Promise<PostReceipt>(resolve => {
          onStop = () => { sendController.abort(); resolve({acceptance: 'unknown'}); };
          this.stopReleases.set(onStop, threadKey);
        }));
        if (reply.purpose === 'owner_control') waits.push(new Promise<PostReceipt>(resolve => {
          deadline = setTimeout(() => { sendController.abort(); resolve({acceptance: 'unknown'}); },
            this.deps.controlDeliveryDeadlineMs ?? 10_000);
        }));
        waits.push(this.deps.postPrepared({dispatchId, destination: dispatch.destination as string,
          text: reply.payload as string, runId: reply.run_id as string,
          verbatim: this.isVerbatimReview(reply), signal: sendController.signal}));
        receipt = await Promise.race(waits);
      } finally {
        if (deadline) clearTimeout(deadline);
        if (onStop) this.stopReleases.delete(onStop);
      }
    } catch { receipt = { acceptance: 'unknown' }; }
    const outcome = receipt.acceptance === 'accepted' && receipt.messageId
      ? 'accepted' : receipt.acceptance === 'rejected' || receipt.acceptance === 'not_attempted'
        ? 'rejected' : 'unknown';
    const settled = this.deps.journal.settleDispatch(dispatchId, attempt.number as number,
      outcome, outcome === 'accepted' ? receipt.messageId : undefined, receipt.messageIds, receipt.providerSentAtMs);
    if (settled.status !== 'accepted') return { status: 'held' };
    return this.resolveAccepted(reply, settled);
  }

  /** Only records already accepted options locally; never sends or saves an email. */
  private async recordAcceptedGmailPair(actionId: string, signal: AbortSignal): Promise<void> {
    if (!this.deps.ownerGmailDraft || this.closed || this.suspended || signal.aborted) return;
    try {
      const saved = this.deps.journal.acceptedGmailOfferedPair(actionId, this.ownerGmailDraftAuthority());
      if (!saved) return;
      if (this.deps.canPostPrepared) {
        let stop: (() => void) | undefined;
        try {
          const permitted = await Promise.race([
            this.deps.canPostPrepared(saved.acceptedDelivery.source_revision),
            new Promise<false>(resolve => {
              stop = () => resolve(false);
              signal.addEventListener('abort', stop, {once: true});
              if (signal.aborted) stop();
            }),
          ]);
          if (!permitted) return;
        } finally { if (stop) signal.removeEventListener('abort', stop); }
      }
      if (this.closed || this.suspended || signal.aborted) return;
      const receipt = await this.deps.ownerGmailDraft.recordOffer(saved.request, saved.acceptedDelivery, signal);
      if (!this.closed && !this.suspended && !signal.aborted)
        this.deps.journal.settleGmailOfferedPair(actionId, receipt);
    } catch {
      // The accepted reply remains accepted. Existing recovery can repair this
      // idempotent local learning without replaying the chat or Gmail effect.
    }
  }

  private async resolveAccepted(reply: Record<string, unknown>, dispatch: Record<string, unknown>): Promise<ConversationResult> {
    const covered = JSON.parse(reply.covered_event_ids as string) as string[];
    const threadId = this.deps.journal.getInbound(reply.event_id as string)?.thread_key as string;
    const fresh = this.freshFeedback.get(threadId);
    const eligibleFeedback = reply.purpose !== 'model_failure' && reply.purpose !== 'outcome_pending' && !isHeldOutcomeNotice(reply) && !this.closed && fresh && covered.includes(fresh.eventId);
    let incompleteTaskOutcome=false;
    // Read the exact receipt while its source is still pending; add/close reads
    // intentionally refuse resolved sources. Settling first would hide proved completion.
    if (eligibleFeedback) {
      if(this.deps.ownerTaskTool){
        try{
          const action=this.deps.journal.listOwnerTaskToolActionsForThread(fresh.eventId).find(item=>item.source_event_id===fresh.eventId);
          if(action){
            const saved=this.deps.journal.readOwnerTaskToolAction(action.id as string,this.ownerTaskToolAuthority());
            const receipt=saved.receipt;
            incompleteTaskOutcome=saved.status==='rejected'||Boolean(receipt&&(receipt.operation==='cleanup'||receipt.operation==='resolve_many')&&receipt.outcome!=='complete');
          }
        }catch{incompleteTaskOutcome=true;}
      }
    }
    if (reply.purpose !== 'outcome_pending') {
      for (const id of covered) if (this.deps.journal.getInbound(id)?.outcome === 'pending') this.deps.journal.resolveInbound(id, 'resolved');
    }
    this.deps.journal.completeRun(reply.run_id as string);
    try {
      const source = this.deps.journal.getInbound(reply.event_id as string);
      if (source) await this.deps.projectConversation?.(decode(source.payload as string).context.threadId);
    }
    catch { /* Acceptance is durable. Recovery repairs projection without another send. */ }
    if (eligibleFeedback) {
      try { if(!incompleteTaskOutcome)this.deps.onOwnerReplyAccepted?.(fresh.inbound, fresh.providerThreadId); }
      catch { /* Acceptance remains true when best-effort feedback fails. */ }
      this.freshFeedback.delete(threadId);
    }
    if (this.deps.ownerGmailDraft && !this.closed && !this.suspended) {
      const action = this.deps.journal.listOwnerGmailDraftActionsForThread(reply.event_id as string)
        .find(item => item.source_event_id === reply.event_id);
      if (action) {
        const controller = new AbortController();
        const deadline = setTimeout(() => controller.abort(), 5000);
        try { await this.recordAcceptedGmailPair(action.id as string, controller.signal); }
        finally { clearTimeout(deadline); }
      }
    }
    if(this.deps.ownerMeetingPrepOffer&&!this.closed&&!this.suspended){
      const action=this.deps.journal.listOwnerMeetingPrepOfferActionsForThread(reply.event_id as string)
        .find(item=>item.source_event_id===reply.event_id);
      if(action){
        const controller=new AbortController();
        const deadline=setTimeout(()=>controller.abort(),5000);
        try{await this.recordAcceptedMeetingPrepOffer(action.id as string,controller.signal);}
        finally{clearTimeout(deadline);}
      }
    }
    return { status: 'accepted', messageId: dispatch.provider_message_id as string };
  }
}
