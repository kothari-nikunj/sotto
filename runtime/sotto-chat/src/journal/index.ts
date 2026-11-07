import {resolveResearchSelection} from '../tools/research-selection.js';
import {ownerCleanupProposalCurrent,freezeCleanupRepairSelection,markCleanupSelectionRepair} from './owner-task-selection.js';
import {freezeQuotedBackground, acceptedBackgroundMessage} from '../background-context.js';
import {PREP_OFFER_MAX_AGE_MS} from '../channels/policy.js';
import {ownerBridgeSendKind,bridgeSendArgsSchema,bridgeSendReceiptSchema,bridgeSendRejectionSchema,bridgeSendPreviewSchema,bridgeSendReceiptMatches,bridgeSendReviewText,validBridgeSendReview,bridgeSendPreviewMatches,type BridgeSendArgs,type BridgeSendReceipt,type BridgeSendRejection,type BridgeSendPreview} from '../tools/owner-bridge-send-contract.js';
import type {OwnerBridgeSendRequest} from '../tools/owner-bridge-send-client.js';
import {READ_CONTEXT_WINDOW_MS, readCancelled, ownerReadRequest, contextualReadRequest, subjectMention, validResearchSubject, ownerSubjectMention, researchTargetAnswer, explicitReadReference} from '../tools/read-admission.js';
import {ownerBriefIntent} from '../tools/requested-brief-classifier.js';
import {OWNER_ONLY_CONTEXT_PROOF} from '../agent/source-authority.js';
import { DELAYED_ACTION_KINDS } from '../delayed-outcome-policy.js';
import {contactsActionIdentity,ownerContactsKind,contactsArgsSchema,contactsReceiptSchema,contactsRejectionSchema,contactsPreviewSchema,contactsReceiptMatches,contactsReviewText,validContactsReview,contactsPreviewMatches,type ContactsArgs,type ContactsReceipt,type ContactsRejection,type ContactsPreview} from '../tools/owner-contacts-contract.js';
import type {OwnerContactsRequest} from '../tools/owner-contacts-client.js';
import {ownerCalendarKind,calendarArgsSchema,calendarReceiptSchema,calendarRejectionSchema,calendarPreviewSchema,calendarReceiptMatches,calendarReviewText,validCalendarReview,calendarPreviewMatches,type CalendarArgs,type CalendarReceipt,type CalendarRejection,type CalendarPreview} from '../tools/owner-calendar-contract.js';
import type {OwnerCalendarRequest} from '../tools/owner-calendar-client.js';
import {ownerUrlTokens,webReadReplyTargetSchema,type WebReadReplyTarget,ownerWebReadKind,ownerWebReadArgsSchema,ownerWebReadReceiptSchema,ownerWebReadRejectionSchema,webReadAdmissionMatches,webReadArgsRevision,webReadReceiptMatches,docsendPreviewSchema,docsendPreviewMatches,docsendReviewText,type DocSendPreview,type OwnerWebReadArgs,type OwnerWebReadReceipt,type OwnerWebReadRejection,type OwnerWebReadRequest} from '../tools/owner-web-read.js';
import {isOwnerFollowupRequest, FollowupAdmissionRequiredError, isOwnerRelationshipPulseRequest, RelationshipPulseAdmissionRequiredError, isOwnerUrgentCheck, isOwnerMeetingPrepRequest, ProactiveAdmissionRequiredError, ownerSkillRequestKind, ownerSkillRequestArgsSchema, ownerSkillRequestReceiptSchema, ownerSkillRequestRejectionSchema, type OwnerSkillRequestArgs, type OwnerSkillRequestReceipt, type OwnerSkillRequestRejection, type OwnerSkillRequest} from '../tools/owner-skill-request-client.js';
import {ownerOfferReplyArgsSchema, ownerOfferReplyKind, ownerOfferReplyReceiptSchema, ownerOfferReplyRejectionSchema, offerReplySelectors, ownerOfferReplyAction, type OwnerOfferReplyArgs, type OwnerOfferReplyRequest, type OwnerOfferReplyReceipt} from '../tools/owner-offer-reply-client.js';
import {clearMeetingPrepYes, ownerMeetingPrepArgsSchema, ownerMeetingPrepKind, ownerMeetingPrepReceiptSchema, ownerMeetingPrepRejectionSchema, meetingPrepSelectors, type OwnerMeetingPrepArgs, type OwnerMeetingPrepRequest, type OwnerMeetingPrepReceipt} from '../tools/owner-meeting-prep-client.js';
import {ownerMeetingPrepOfferArgsSchema,ownerMeetingPrepOfferKind,ownerMeetingPrepOfferPreviewSchema,ownerMeetingPrepOfferReceiptSchema,ownerMeetingPrepOfferRejectionSchema,ownerMeetingPrepOfferRefusalSchema,ownerMeetingPrepOfferMatches,ownerMeetingPrepOfferQuestion,ownerMeetingPrepOfferAcceptedDeliverySchema,type OwnerMeetingPrepOfferArgs,type OwnerMeetingPrepOfferPreview,type OwnerMeetingPrepOfferReceipt,type OwnerMeetingPrepOfferAcceptedDelivery,type OwnerMeetingPrepOfferRefusal} from '../tools/owner-meeting-prep-offer-contract.js';
import type {OwnerMeetingPrepOfferRequest} from '../tools/owner-meeting-prep-offer-client.js';
import type {Inbound} from '../channels/types.js';
import { discoveryIdentity, discoveryRoute } from '../activation.js';
import {ownerBriefRequestArgsSchema, ownerBriefRequestKind, ownerBriefRequestReceiptSchema, type OwnerBriefRequestArgs, type OwnerBriefRequest, type OwnerBriefRequestReceipt} from '../tools/owner-brief-request-client.js';
import {gmailAcceptedDeliverySchema,type GmailAcceptedDelivery, ownerGmailDraftKind, followupEmailFromOwnerText, gmailDraftArgsSchema, gmailDraftReceiptSchema, gmailDraftRejectionSchema, gmailDraftPreviewSchema, gmailDraftReceiptMatches, gmailDraftReviewText, clearGmailSendYes, validGmailDraftReview, gmailDraftPreviewMatches, type GmailDraftArgs, type GmailDraftReceipt, type GmailDraftRejection, type GmailDraftPreview } from '../tools/owner-gmail-draft-contract.js';
import type { OwnerGmailDraftRequest } from '../tools/owner-gmail-draft-client.js';
import {ownerRoutineToolArgsSchema,ownerRoutineToolKind,ownerRoutineToolReceiptSchema,ownerRoutineToolRejectionSchema,routineReceiptMatches,routinePreviewSchema, routinePreviewMatches,routineProposalText,clearRoutineYes,
  type OwnerRoutineToolArgs,type OwnerRoutineToolReceipt,type OwnerRoutineToolRejection,type RoutinePreview} from '../tools/owner-routine-tool-contract.js';
import type {OwnerRoutineToolRequest} from '../tools/owner-routine-tool-client.js';
/** Durable operational journal. Conversation transcripts are deliberately stored elsewhere. */
import { createHash } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { isQuietUntil } from '../quiet-until.js';
import { stableActionIdentity } from '../tools/action-gate.js';
import { ownerReminderToolArgsSchema, ownerReminderToolKind, ownerReminderToolReceiptSchema, reminderReceiptMatches,
  type OwnerReminderToolArgs, type OwnerReminderToolReceipt } from '../tools/owner-reminder-tool-contract.js';
import { ownerTaskToolArgsSchema, ownerTaskToolKind, ownerTaskToolReceiptSchema, ownerTaskToolRejectionSchema, taskReceiptMatches,
  taskPreviewSchema,taskPreviewMatches,taskReviewText,cleanupSelectionsMatch,cleanupRequestMatches,cleanupSelectionItems,cleanupSelectionSchema,cleanupItemsSchema,cleanupListHash,OwnerTaskListUnavailable,resolveManyOwnerTextMatches,type TaskPreview,validRelationReview,type OwnerTaskToolArgs, type OwnerTaskToolReceipt, type OwnerTaskToolRejection } from '../tools/owner-task-tool-contract.js';
import { ownerPreferenceToolArgsSchema, ownerPreferenceToolKind, ownerPreferenceToolReceiptSchema, ownerPreferenceToolRejectionSchema, preferenceReceiptMatches,priorityCtaMatches,oneOffToneRequest,oneOffShorteningRequest,ownerQuietCommand,
  procedurePreviewSchema,procedureReviewText,validProcedurePreview, standingPreviewMatches,procedureCapacitySchema,type ProcedurePreview,type ProcedureCapacity,type OwnerPreferenceToolArgs, type OwnerPreferenceToolReceipt, type OwnerPreferenceToolRejection } from '../tools/owner-preference-tool-contract.js';
import type { OwnerPreferenceToolRequest } from '../tools/owner-preference-tool-client.js';
import type { OwnerTaskToolRequest } from '../tools/owner-task-tool-client.js';
import type { OwnerReminderToolRequest } from '../tools/owner-reminder-tool-client.js';
import { activationDiscoverySeedSchema, activationEvidenceSchema, activationIdentitySchema, capturedPhotonThreadMatchesLine,
  type ActivationDiscoverySeed, type ActivationEvidence, type ActivationIdentity } from '../activation.js';

export type DurableActivationAdmission = { status: 'absent' } | {
  status: 'pending' | 'acknowledged'; binding: ActivationIdentity;
  event_id: string; admitted_at: number; acknowledged_at?: number;
};

export type DispatchKind = 'interactive' | 'gallery' | 'scheduled_text';
export type DispatchOutcome = 'accepted' | 'rejected' | 'unknown';
export type ActionOutcome = 'succeeded' | 'rejected' | 'unknown';

export interface InboundInput {
  eventId: string;
  accountKey: string;
  lineKey: string;
  threadKey: string;
  channel: string;
  sender: string;
  /** Normalized, bounded adapter payload; sufficient to replay an admitted event. */
  payload: string;
  /** Trusted gateway candidate; bound atomically with the first admitted owner event. */
  activation?: ActivationIdentity;
  continuation?: {routeDigest: string; providerSentAtMs: number};
}

export interface RunInput { runId: string; eventId: string }
export interface ReplyInput {
  /** Trusted coordinator metadata; never derived from model output. */
  purpose?: 'outcome_pending' | 'acknowledgement_unavailable' | 'owner_control' | 'model_failure';
  replyId: string;
  runId: string;
  eventId: string;
  coveredEventIds?: string[];
  dispatchId: string;
  kind: DispatchKind;
  destination: string;
  payload: string;
  maxAttempts?: number;
  sourceRevision?: string;
  routeDigest?: string;
}
export interface DispatchInput {
  sourceRevision?: string;
  dispatchId: string;
  kind: DispatchKind;
  destination: string;
  payload: string;
  maxAttempts?: number;
  /** Immutable digest of the resolved provider thread and configured owner scope. */
  routeDigest?: string;
}
export interface ActionInput {
  actionId: string;
  runId: string;
  kind: string;
  payloadHash: string;
}
/** Trusted application identity; never built from model tool arguments. */
export interface ExplicitOwnerLoopAuthority {
  runId: string;
  accountId: string;
  ownerId: string;
  lineKey: string;
  channel: 'telegram' | 'photon';
  /** Hash of the configured private mutation capability; binds pending intents to one runtime identity. */
  scopeHash?: string;
  /**
   * Read-only reconciliation of a saved action whose source event a later /stop
   * (or resolution) already closed. It lets exact inspect proof settle that action;
   * it is ignored by every current-admission (prepare) path and never authorizes a new effect.
   */
  inspectClosedSource?: boolean;
}
export interface ExplicitOwnerLoopIntent {
  actionId: string;
  sourceEventId: string;
  /** Transient exact admitted command span. This is never copied into the action row. */
  text: string;
  status: string;
  receiptHash: string | null;
  sourcePayloadHash: string;
  admittedAtMs: number;
  providerThreadId: string;
  zone: string | null;
}
export interface ExplicitOwnerLoopCloseIntent extends ExplicitOwnerLoopIntent {
  anchorKey: string;
  expectedRowDigest: string | null;
}
export interface ExplicitOwnerReminderIntent {
  actionId: string; sourceEventId: string; text: string; operation: 'create' | 'cancel';
  status: string; sourcePayloadHash: string; admittedAtMs: number; providerThreadId: string;
  reminderId: string | null; reminderStatus: 'scheduled' | 'canceled' | 'fired' | null;
}
export interface ExplicitOwnerQuietIntent {
  actionId: string; sourceEventId: string; text: string; operation: 'write';
  status: string; sourcePayloadHash: string; admittedAtMs: number; providerThreadId: string;
  zone: string | null; snoozeUntil: string | null;
}

/**
 * A deterministic admission refusal: retrying the same provider event can never succeed.
 * Intake denies (and lets the provider acknowledge) it instead of retrying or crashing.
 */
export class InboundAdmissionConflict extends Error {
  constructor(message: string, readonly reason: 'event_identity_conflict' | 'activation_destination_conflict') {
    super(message);
    this.name = 'InboundAdmissionConflict';
  }
}

type Row = Record<string, unknown>;
const now = () => Date.now();
/** Owner-tool kinds whose proposals bind the next admitted owner event (literal: partial index). */
const PROPOSAL_KINDS_SQL = [ownerRoutineToolKind, ownerGmailDraftKind, ownerCalendarKind, ownerPreferenceToolKind,
  ownerTaskToolKind, ownerContactsKind, ownerBridgeSendKind, ownerWebReadKind].map(kind => `'${kind}'`).join(',');
/** Model-invoked owner tools: lifetime fences whose bulky terminal results retention compacts. */
const OWNER_TOOL_KINDS_SQL = ['owner_reminder_tool', 'owner_loop_tool', 'owner_preference_tool', 'owner_routine_tool',
  'owner_gmail_draft_tool', 'owner_calendar_tool', 'owner_contacts_tool','owner_bridge_send_tool', 'owner_brief_request', 'owner_meeting_prep_request','owner_meeting_prep_offer',
  'owner_offer_reply', 'owner_skill_request', 'owner_web_read'].map(kind => `'${kind}'`).join(',');
const PROPOSAL_OPERATIONS = ['propose','propose_adjust','propose_offer','propose_followup','propose_send','propose_new_send','propose_pair','propose_typed','propose_send_typed','propose_move','propose_move_update','propose_rsvp','propose_create','propose_update','propose_delete','propose-procedure','propose-priorities','propose-context','propose_relation','propose_identifier','propose_fact','propose_cleanup','propose_docsend'];
const hash = (value: string) => createHash('sha256').update(value).digest('hex');
export const SCHEDULED_TEXT_MAX_ATTEMPTS = 96;

export function parseOwnerReminderCommand(text: string): { operation: 'create' | 'cancel' | 'list' } | null {
  if (text === '/reminder list') return { operation: 'list' };
  if (/^\/reminder cancel int_[a-f0-9]{32}$/.test(text)) return { operation: 'cancel' };
  const match = /^\/reminder at (\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d(?:\.\d{1,6})?)?(?:Z|[+-]\d\d:\d\d)) (.+)$/u.exec(text);
  if (!match) return null;
  const message = match[2]!;
  const calendar = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)(?::(\d\d)(?:\.\d{1,6})?)?(Z|[+-](\d\d):(\d\d))$/.exec(match[1]!);
  if (!calendar) return null;
  const year = Number(calendar[1]), month = Number(calendar[2]), day = Number(calendar[3]);
  const hour = Number(calendar[4]), minute = Number(calendar[5]), second = Number(calendar[6] ?? '0');
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days ||
      hour > 23 || minute > 59 || second > 59 ||
      (calendar[7] !== 'Z' && (Number(calendar[8]) > 23 || Number(calendar[9]) > 59))) return null;
  if (!message || message.trim() !== message || Array.from(message).length > 500 ||
      /[\u0000-\u001f\u007f\ud800-\udfff]/u.test(message) ||
      Buffer.from(message, 'utf8').toString('utf8') !== message || !Number.isFinite(Date.parse(match[1]!))) return null;
  return { operation: 'create' };
}

export function parseOwnerQuietCommand(text: string): { mode: 'for' | 'off' } | null {
  if (text === '/quiet off') return { mode: 'off' };
  const match = /^\/quiet for ([1-9][0-9]{0,3})([hm])$/.exec(text);
  if (!match) return null;
  const count = Number(match[1]);
  return count <= (match[2] === 'h' ? 24 : 1440) ? { mode: 'for' } : null;
}

function required(value: string, name: string): void {
  if (typeof value !== 'string' || value.length === 0) throw new Error(`${name} is required`);
}

export class OperationalJournal {
  readonly db: DatabaseSync;
  private activationIdentity: ActivationIdentity | undefined;

  constructor(path: string) {
    required(path, 'database path');
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    const version = Number(this.db.prepare('PRAGMA user_version').get()!.user_version);
    if (version !== 0 && version !== 1 && version !== 2 && version !== 3 && version !== 4 && version !== 5 && version !== 6 && version !== 7) {
      this.db.close();
      throw new Error('Operational journal schema is not supported by this runtime');
    }
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000; PRAGMA synchronous=FULL;');
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS inbound_events (
        id TEXT PRIMARY KEY, source_id TEXT NOT NULL, account_key TEXT NOT NULL, line_key TEXT NOT NULL,
        thread_key TEXT NOT NULL, channel TEXT NOT NULL,
        sender TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL, received_at INTEGER NOT NULL,
        outcome TEXT NOT NULL DEFAULT 'pending' CHECK(outcome IN ('pending','resolved','cancelled'))
      );
      CREATE TABLE IF NOT EXISTS thread_generations (
        thread_key TEXT PRIMARY KEY, generation INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES inbound_events(id),
        thread_key TEXT NOT NULL, generation INTEGER NOT NULL, admitted_through INTEGER,
        status TEXT NOT NULL CHECK(status IN ('running','completed','cancelled')),
        started_at INTEGER NOT NULL, ended_at INTEGER
      );
      CREATE TABLE IF NOT EXISTS dispatches (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('interactive','gallery','scheduled_text')),
        destination TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
        route_digest TEXT,
        status TEXT NOT NULL CHECK(status IN ('prepared','dispatching','accepted','rejected','unknown','cancelled')),
        current_attempt INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
        provider_message_id TEXT, provider_message_ids TEXT,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS dispatch_attempts (
        dispatch_id TEXT NOT NULL REFERENCES dispatches(id), number INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('dispatching','accepted','rejected','unknown')),
        provider_message_id TEXT, started_at INTEGER NOT NULL, finished_at INTEGER,
        PRIMARY KEY(dispatch_id,number)
      );
      CREATE TABLE IF NOT EXISTS prepared_replies (
        id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES inbound_events(id),
        run_id TEXT NOT NULL REFERENCES runs(id), dispatch_id TEXT NOT NULL UNIQUE REFERENCES dispatches(id),
        payload TEXT NOT NULL, covered_event_ids TEXT NOT NULL, prepared_at INTEGER NOT NULL,
        source_revision TEXT
      );
      CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
        kind TEXT NOT NULL, payload_hash TEXT NOT NULL,
        source_event_id TEXT REFERENCES inbound_events(id), source_payload_hash TEXT, source_text_hash TEXT,
        status TEXT NOT NULL CHECK(status IN ('pending','executing','succeeded','rejected','unknown')),
        result_json TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS activation_identity (
        id INTEGER PRIMARY KEY CHECK(id=1), identity_json TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS activation_discovery_seed (
        id INTEGER PRIMARY KEY CHECK(id=1), seed_json TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS activation_obligation (
        id INTEGER PRIMARY KEY CHECK(id=1), event_id TEXT NOT NULL,
        created_at INTEGER NOT NULL, acknowledged_at INTEGER
      );
      CREATE INDEX IF NOT EXISTS dispatch_status_age ON dispatches(status,updated_at);
      CREATE INDEX IF NOT EXISTS action_status_age ON actions(status,updated_at);
    `);
    if (!this.db.prepare('PRAGMA table_info(prepared_replies)').all().some(column => column.name === 'source_revision')) {
      this.db.exec('ALTER TABLE prepared_replies ADD COLUMN source_revision TEXT');
    }
    if (!this.db.prepare('PRAGMA table_info(prepared_replies)').all().some(column => column.name === 'purpose')) {
      this.db.exec('ALTER TABLE prepared_replies ADD COLUMN purpose TEXT');
    }
    const actionColumns = new Set(this.db.prepare('PRAGMA table_info(actions)').all().map(column => column.name));
    if (!actionColumns.has('source_event_id')) this.db.exec('ALTER TABLE actions ADD COLUMN source_event_id TEXT REFERENCES inbound_events(id)');
    if (!actionColumns.has('source_payload_hash')) this.db.exec('ALTER TABLE actions ADD COLUMN source_payload_hash TEXT');
    if (!actionColumns.has('source_text_hash')) this.db.exec('ALTER TABLE actions ADD COLUMN source_text_hash TEXT');
    if (!this.db.prepare('PRAGMA table_info(runs)').all().some(column => column.name === 'admitted_through')) {
      this.db.exec('ALTER TABLE runs ADD COLUMN admitted_through INTEGER');
    }
    if (!this.db.prepare('PRAGMA table_info(dispatches)').all().some(column => column.name === 'route_digest')) {
      this.db.exec('ALTER TABLE dispatches ADD COLUMN route_digest TEXT');
    }
    if (!this.db.prepare('PRAGMA table_info(inbound_events)').all().some(column => column.name === 'continuation_json'))
      this.db.exec('ALTER TABLE inbound_events ADD COLUMN continuation_json TEXT');
    // Additive schema-7 bookkeeping: cancellation and this marker commit together,
    // so replay never selects a different request after the original target closes.
    if (!this.db.prepare('PRAGMA table_info(inbound_events)').all().some(column => column.name === 'stop_applied'))
      this.db.exec('ALTER TABLE inbound_events ADD COLUMN stop_applied INTEGER CHECK(stop_applied=1)');
    if (!this.db.prepare('PRAGMA table_info(dispatches)').all().some(column => column.name === 'provider_sent_at_ms'))
      this.db.exec('ALTER TABLE dispatches ADD COLUMN provider_sent_at_ms INTEGER');
    if (!this.db.prepare('PRAGMA table_info(dispatches)').all().some(column => column.name === 'source_revision'))
      this.db.exec('ALTER TABLE dispatches ADD COLUMN source_revision TEXT');
    // Additive, schema-7-compatible bookkeeping. proposal_checked marks a proposal-kind
    // action whose current result_json admission has already evaluated for next-event
    // binding; any rewrite of result_json clears it, so admission re-evaluates exactly the
    // rows it would have bound before. A NULL (legacy or new) row is evaluated once.
    // compacted_at records retention compaction of a terminal owner-tool fence.
    if (!actionColumns.has('proposal_checked')) this.db.exec('ALTER TABLE actions ADD COLUMN proposal_checked INTEGER');
    if (!actionColumns.has('compacted_at')) this.db.exec('ALTER TABLE actions ADD COLUMN compacted_at INTEGER');
    this.db.exec(`
      CREATE TRIGGER IF NOT EXISTS action_proposal_recheck AFTER UPDATE OF result_json ON actions
        WHEN NEW.proposal_checked IS NOT NULL
        BEGIN UPDATE actions SET proposal_checked=NULL WHERE id=NEW.id; END;
      CREATE INDEX IF NOT EXISTS action_proposal_unchecked_v2 ON actions(run_id)
        WHERE proposal_checked IS NULL;
      DROP INDEX IF EXISTS action_proposal_unchecked;
    `);
    this.db.exec('PRAGMA user_version=7');
    // The supervisor must establish single-process ownership before calling recoverInterrupted().
    // Merely opening a second handle is not evidence that an in-flight provider call crashed.
  }

  /** An upgraded route-null scheduled send cannot be safely replayed or discharged. */
  assertNoUnresolvedLegacyDispatches(): void {
    const unresolved = this.db.prepare(`SELECT 1 FROM dispatches
      WHERE route_digest IS NULL AND kind IN ('scheduled_text','gallery')
        AND status NOT IN ('accepted','cancelled') LIMIT 1`).get();
    if (unresolved) throw new Error('unresolved legacy dispatch requires offline reconciliation');
  }

  close(): void { this.db.close(); }

  private transaction<T>(body: () => T): T {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = body();
      this.db.exec('COMMIT');
      return result;
    } catch (error) {
      this.db.exec('ROLLBACK');
      throw error;
    }
  }

  private one(sql: string, ...params: (string | number)[]): Row | undefined {
    return this.db.prepare(sql).get(...params) as Row | undefined;
  }

  /** Bind the current runtime's Photon project/tenant/owner before provider intake. */
  configureActivationIdentity(identity?: ActivationIdentity): void {
    const normalized = identity === undefined ? undefined : activationIdentitySchema.parse(identity);
    this.transaction(() => {
      if (this.one('SELECT id FROM activation_discovery_seed WHERE id=1')) {
        throw new Error('activation discovery state cannot be opened as fixed destination');
      }
      const stored = this.one('SELECT identity_json FROM activation_identity WHERE id=1');
      if (stored) {
        let previous: ActivationIdentity;
        try { previous = activationIdentitySchema.parse(JSON.parse(stored.identity_json as string)); }
        catch { throw new Error('stored activation identity is invalid'); }
        if (!normalized || JSON.stringify(previous) !== JSON.stringify(normalized)) {
          throw new Error('activation identity does not match this journal');
        }
      } else if (normalized) {
        // An obligation without its manifest is corrupt, not a reason to rebind it.
        if (this.one('SELECT event_id FROM activation_obligation WHERE id=1')) {
          throw new Error('activation obligation has no identity');
        }
        this.db.prepare('INSERT INTO activation_identity(id,identity_json) VALUES(1,?)').run(JSON.stringify(normalized));
      }
    });
    this.activationIdentity = normalized;
  }

  /** Persist an immutable seed without inventing a destination. A committed binding may already exist. */
  configureActivationDiscovery(seed: ActivationDiscoverySeed): ActivationIdentity | null {
    const normalized = activationDiscoverySeedSchema.parse(seed);
    let binding: ActivationIdentity | null = null;
    this.transaction(() => {
      const stored = this.one('SELECT seed_json FROM activation_discovery_seed WHERE id=1');
      if (stored) {
        let previous: ActivationDiscoverySeed;
        try { previous = activationDiscoverySeedSchema.parse(JSON.parse(String(stored.seed_json))); }
        catch { throw new Error('stored activation discovery seed is invalid'); }
        if (JSON.stringify(previous) !== JSON.stringify(normalized)) throw new Error('activation discovery seed does not match this journal');
      } else {
        if (this.one('SELECT id FROM activation_identity WHERE id=1')) {
          throw new Error('fixed activation identity cannot be opened as discovery');
        }
        this.db.prepare('INSERT INTO activation_discovery_seed(id,seed_json) VALUES(1,?)').run(JSON.stringify(normalized));
      }
      const row = this.one('SELECT identity_json FROM activation_identity WHERE id=1');
      if (row) {
        try { binding = activationIdentitySchema.parse(JSON.parse(String(row.identity_json))); }
        catch { throw new Error('stored activation identity is invalid'); }
        const { provider_thread_id: _thread, ...base } = binding!;
        const expected = discoveryIdentity(normalized);
        if (JSON.stringify(base) !== JSON.stringify(expected) ||
            !capturedPhotonThreadMatchesLine(binding!.provider_thread_id, discoveryRoute(normalized))) {
          throw new Error('activation binding does not match discovery seed');
        }
      } else if (this.one('SELECT event_id FROM activation_obligation WHERE id=1')) {
        throw new Error('activation obligation has no discovery binding');
      }
    });
    this.activationIdentity = binding ?? undefined;
    return binding;
  }

  getActivationIdentity(): ActivationIdentity | null { return this.activationIdentity ?? null; }

  isActivationAcknowledged(): boolean {
    return !!this.activationIdentity && !!this.one('SELECT event_id FROM activation_obligation WHERE id=1 AND acknowledged_at IS NOT NULL');
  }

  /** Read only journal facts. A configured fixed thread alone is never admitted owner-DM proof. */
  getDurableActivationAdmission(): DurableActivationAdmission {
    const identityRow = this.one('SELECT identity_json FROM activation_identity WHERE id=1');
    const obligation = this.one('SELECT event_id,created_at,acknowledged_at FROM activation_obligation WHERE id=1');
    if (!identityRow) {
      if (obligation || this.activationIdentity) throw new Error('activation identity is inconsistent');
      return { status: 'absent' };
    }
    let binding: ActivationIdentity;
    try { binding = activationIdentitySchema.parse(JSON.parse(String(identityRow.identity_json))); }
    catch { throw new Error('stored activation identity is invalid'); }
    if (!this.activationIdentity || JSON.stringify(binding) !== JSON.stringify(this.activationIdentity)) {
      throw new Error('activation identity does not match current runtime');
    }
    if (!obligation) {
      // Fixed destinations write activation_identity at startup, before a real owner DM.
      if (this.one('SELECT id FROM activation_discovery_seed WHERE id=1')) {
        throw new Error('discovery binding has no admission obligation');
      }
      return { status: 'absent' };
    }
    const event_id = String(obligation.event_id);
    const admitted_at = Number(obligation.created_at);
    const acknowledged_at = obligation.acknowledged_at === null ? null : Number(obligation.acknowledged_at);
    if (!/^[a-f0-9]{64}$/.test(event_id) || !Number.isSafeInteger(admitted_at) || admitted_at < 0 ||
        (acknowledged_at !== null && (!Number.isSafeInteger(acknowledged_at) || acknowledged_at < admitted_at))) {
      throw new Error('activation obligation is invalid');
    }
    // Pending evidence must still have its admitted source event. After ACK, retention may
    // discard that event, while the compact obligation and immutable binding remain durable.
    const event = this.getInbound(event_id);
    if (acknowledged_at === null && this.getActivationEvidence()?.event_id !== event_id) {
      throw new Error('pending activation admission is unavailable');
    }
    if (event) {
      const expectedThread = hash(JSON.stringify([binding.account_id, binding.line_key, 'photon', binding.provider_thread_id]));
      if (event.account_key !== binding.account_id || event.line_key !== binding.line_key ||
          event.channel !== 'photon' || event.sender !== binding.owner_id || event.thread_key !== expectedThread) {
        throw new Error('activation obligation does not match its admission');
      }
    }
    return acknowledged_at === null
      ? { status: 'pending', binding, event_id, admitted_at }
      : { status: 'acknowledged', binding, event_id, admitted_at, acknowledged_at };
  }

  /** Return metadata only; a pending obligation cannot outlive its admitted event. */
  getActivationEvidence(): ActivationEvidence | null {
    const identity = this.activationIdentity;
    if (!identity) return null;
    const row = this.one(`SELECT e.* FROM activation_obligation o
      JOIN inbound_events e ON e.id=o.event_id WHERE o.id=1 AND o.acknowledged_at IS NULL`);
    if (!row) return null;
    const expectedThread = hash(JSON.stringify([identity.account_id, identity.line_key, 'photon', identity.provider_thread_id]));
    if (row.account_key !== identity.account_id || row.line_key !== identity.line_key || row.channel !== 'photon' ||
        row.sender !== identity.owner_id || row.thread_key !== expectedThread) {
      throw new Error('activation obligation no longer matches its admission');
    }
    return activationEvidenceSchema.parse({ version: 1, event_id: row.id,
      provider_event_id: row.source_id, tenant_id: identity.tenant_id,
      project_fingerprint: identity.project_fingerprint, owner_id: identity.owner_id,
      account_id: identity.account_id, line_key: identity.line_key,
      provider_thread_id: identity.provider_thread_id, admitted_at: row.received_at });
  }

  /** Only the exact current owner event can close the obligation; repeats are harmless. */
  acknowledgeActivation(eventId: string): boolean {
    if (!this.activationIdentity || !/^[a-f0-9]{64}$/.test(eventId)) return false;
    return this.transaction(() => {
      const row = this.one('SELECT event_id,acknowledged_at FROM activation_obligation WHERE id=1');
      if (!row || row.event_id !== eventId) return false;
      if (row.acknowledged_at === null) {
        this.db.prepare('UPDATE activation_obligation SET acknowledged_at=? WHERE id=1 AND acknowledged_at IS NULL').run(now());
      }
      return true;
    });
  }

  /** Admission must be committed before an adapter acknowledges or deduplicates an event. */
  admitInbound(input: InboundInput): { admitted: boolean; event: Row } {
    for (const key of ['eventId', 'accountKey', 'lineKey', 'threadKey', 'channel', 'sender', 'payload'] as const) required(input[key], key);
    if (Buffer.byteLength(input.payload) > 1_048_576 ||
        [input.eventId, input.accountKey, input.lineKey, input.threadKey, input.channel, input.sender].some(value => value.length > 512)) {
      throw new Error('Inbound journal payload exceeds its allowed bounds');
    }
    const id = hash(JSON.stringify([input.accountKey, input.lineKey, input.channel, input.eventId]));
    const scopedThreadKey = hash(JSON.stringify([input.accountKey, input.lineKey, input.channel, input.threadKey]));
    const payloadHash = hash(input.payload);
    const candidate = input.activation === undefined ? undefined : activationIdentitySchema.parse(input.activation);
    const saved = this.transaction(() => {
      let identity = this.activationIdentity;
      const existing = this.one('SELECT * FROM inbound_events WHERE id=?', id);
      if (candidate && existing && !identity) throw new Error('unbound activation duplicate cannot create a binding');
      if (!candidate && input.channel === 'photon' &&
          this.one('SELECT id FROM activation_discovery_seed WHERE id=1')) {
        throw new Error('discovery admission requires a trusted activation candidate');
      }
      if (candidate) {
        const seedRow = this.one('SELECT seed_json FROM activation_discovery_seed WHERE id=1');
        if (!seedRow) throw new Error('activation discovery was not configured');
        const seed = activationDiscoverySeedSchema.parse(JSON.parse(String(seedRow.seed_json)));
        const line = discoveryRoute(seed), base = discoveryIdentity(seed);
        const { provider_thread_id: thread, ...candidateBase } = candidate;
        if (input.channel !== 'photon' || input.accountKey !== candidate.account_id ||
            input.lineKey !== candidate.line_key || input.threadKey !== thread ||
            input.sender !== candidate.owner_id || JSON.stringify(base) !== JSON.stringify(candidateBase) ||
            !capturedPhotonThreadMatchesLine(thread, line)) {
          throw new Error('activation candidate does not match the owner discovery seed');
        }
        if (identity && JSON.stringify(identity) !== JSON.stringify(candidate)) {
          throw new InboundAdmissionConflict('activation destination conflict', 'activation_destination_conflict');
        }
        if (!identity) {
          if (this.one('SELECT id FROM activation_identity WHERE id=1')) throw new Error('activation identity is inconsistent');
          this.db.prepare('INSERT INTO activation_identity(id,identity_json) VALUES(1,?)').run(JSON.stringify(candidate));
          identity = candidate;
        }
      }
      if (input.channel === 'photon' && !identity && this.one('SELECT id FROM activation_identity WHERE id=1')) {
        throw new Error('Photon activation identity has not been configured');
      }
      if (existing) {
        if (existing.payload_hash !== payloadHash || existing.thread_key !== scopedThreadKey ||
            existing.channel !== input.channel || existing.sender !== input.sender) {
          throw new InboundAdmissionConflict('inbound event identity conflict', 'event_identity_conflict');
        }
        return { admitted: false, event: existing };
      }
      // Bind a proposal to its first subsequent admitted event before any later pruning.
      // A yes arriving before the proposal was accepted cannot become approval later.
      // Only rows whose current result_json was never evaluated are read (partial index);
      // an already-bound or non-proposal row cannot change outcome until it is rewritten.
      const priorProposalActions = this.db.prepare(
        `SELECT a.* FROM actions a JOIN runs r ON r.id=a.run_id
          WHERE a.proposal_checked IS NULL AND a.kind IN (${PROPOSAL_KINDS_SQL}) AND r.thread_key=?`,
      ).all(scopedThreadKey) as Row[];
      const checked = this.db.prepare('UPDATE actions SET proposal_checked=1 WHERE id=?');
      for (const proposalAction of priorProposalActions) {
        const metadata = JSON.parse(proposalAction.result_json as string);
        if (PROPOSAL_OPERATIONS.includes(metadata.args?.operation) && metadata.next_event_id === undefined) {
          const proposalReply = this.getReplyForEvent(proposalAction.source_event_id as string);
          this.db.prepare('UPDATE actions SET result_json=? WHERE id=?').run(JSON.stringify({
            ...metadata,
            next_event_id: id,
            next_event_accepted: proposalReply?.dispatch_status === 'accepted',
          }), proposalAction.id as string);
        }
        freezeCleanupRepairSelection(this,proposalAction,id);
        checked.run(proposalAction.id as string);
      }
      const continuation = this.freezeContinuation(input, scopedThreadKey);
      this.db.prepare('INSERT INTO inbound_events(id,source_id,account_key,line_key,thread_key,channel,sender,payload_hash,payload,received_at,continuation_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)')
        .run(id, input.eventId, input.accountKey, input.lineKey, scopedThreadKey, input.channel, input.sender, payloadHash, input.payload, now(), continuation);
      if (identity && input.channel === 'photon' && input.accountKey === identity.account_id &&
          input.lineKey === identity.line_key && input.threadKey === identity.provider_thread_id &&
          input.sender === identity.owner_id) {
        const obligation = this.one('SELECT event_id,acknowledged_at FROM activation_obligation WHERE id=1');
        if (!obligation || (obligation.acknowledged_at !== null && obligation.event_id !== id)) {
          // A fresh real owner DM can repair a lost receipt after the previous ACK.
          // An unacknowledged first event remains the sole pending proof.
          activationEvidenceSchema.parse({ version: 1, event_id: id, provider_event_id: input.eventId,
            ...identity, admitted_at: now() });
          if (obligation) {
            this.db.prepare('UPDATE activation_obligation SET event_id=?,created_at=?,acknowledged_at=NULL WHERE id=1')
              .run(id, now());
          } else {
            this.db.prepare('INSERT INTO activation_obligation(id,event_id,created_at) VALUES(1,?,?)').run(id, now());
          }
        }
      }
      return { admitted: true, event: this.one('SELECT * FROM inbound_events WHERE id=?', id)! };
    });
    if (candidate) this.activationIdentity = candidate;
    return saved;
  }

  private freezeContinuation(input: InboundInput, threadKey: string): string | null {
    if (!input.continuation) return null;
    const ineligible = JSON.stringify({version: 1, eligible: false});
    const {routeDigest, providerSentAtMs} = input.continuation;
    if (input.channel !== 'photon' || !/^[a-f0-9]{64}$/.test(routeDigest)
        || !Number.isSafeInteger(providerSentAtMs)) return ineligible;
    const inbound = JSON.parse(input.payload).inbound as Inbound;
    if (inbound.replyBound) return freezeQuotedBackground(this, inbound, routeDigest, providerSentAtMs);
    const sameRoute = "(d.route_digest=? OR d.id IN (SELECT p.dispatch_id FROM prepared_replies p JOIN runs r ON r.id=p.run_id WHERE r.thread_key=?))";
    const rows = this.db.prepare(`SELECT d.id,d.updated_at,d.route_digest,d.provider_message_ids,d.provider_message_id,d.provider_sent_at_ms,d.current_attempt FROM dispatches d WHERE ${sameRoute} AND d.status='accepted' ORDER BY d.updated_at DESC,d.id DESC LIMIT 2`).all(routeDigest, threadKey) as Row[];
    const latest = rows[0];
    if (!latest || rows[1]?.updated_at === latest.updated_at) return ineligible;
    const ids = JSON.parse(String(latest.provider_message_ids ?? '[]'));
    if (latest.route_digest !== routeDigest || ids.length !== 1 || ids[0] !== latest.provider_message_id || !Number.isSafeInteger(latest.provider_sent_at_ms)
        || Number(latest.provider_sent_at_ms) >= providerSentAtMs || this.hasUnresolvedDispatchAttempt(String(latest.id))) return ineligible;
    // Local ordering is only a barrier, never compared to the provider clock.
    if (this.one('SELECT id FROM inbound_events WHERE thread_key=? AND received_at>=? LIMIT 1', threadKey, Number(latest.updated_at))
        || this.one(`SELECT d.id FROM dispatches d WHERE ${sameRoute} AND d.id<>? AND (d.status IN ('prepared','dispatching','unknown') OR d.created_at>=?) LIMIT 1`, routeDigest, threadKey, String(latest.id), Number(latest.updated_at))) return ineligible;
    const contact = this.one("SELECT started_at,finished_at FROM dispatch_attempts WHERE dispatch_id=? AND number=? AND status='accepted'", String(latest.id), Number(latest.current_attempt));
    if (!contact || contact.finished_at === null || this.one(`SELECT a.dispatch_id FROM dispatch_attempts a JOIN dispatches d ON d.id=a.dispatch_id WHERE ${sameRoute} AND d.id<>? AND a.started_at<=? AND (a.finished_at IS NULL OR a.finished_at>=?) LIMIT 1`,
        routeDigest, threadKey, String(latest.id), Number(contact.finished_at), Number(contact.started_at))) return ineligible;
    return JSON.stringify({version: 1, eligible: true, dispatch_id: latest.id,
      provider_message_id: ids[0], route_digest: routeDigest,
      inbound_provider_sent_at_ms: providerSentAtMs, provider_sent_at_ms: latest.provider_sent_at_ms});
  }

  /** Avoid a provider read for ordinary chat or a pending local proposal. */
  hasPrepContinuationCandidate(eventId: string, routeDigest: string,
      offerAuthority?:Omit<ExplicitOwnerLoopAuthority,'runId'>): boolean {
    return this.prepContinuationCandidate(eventId, routeDigest,offerAuthority) !== null;
  }

  /** Native candidate time narrows the single provider window; it grants no authority. */
  prepContinuationCandidate(eventId: string, routeDigest: string,
      offerAuthority?:Omit<ExplicitOwnerLoopAuthority,'runId'>): {providerSentAtMs?: number;ownerMeetingPrepOfferActionId?:string} | null {
    const event = this.getInbound(eventId);
    if (!event) return null;
    const row = this.one(`SELECT d.* FROM dispatches d WHERE d.status='accepted'
      AND (d.route_digest=? OR d.id IN (SELECT p.dispatch_id FROM prepared_replies p
        JOIN runs r ON r.id=p.run_id WHERE r.thread_key=?)) ORDER BY d.updated_at DESC,d.id DESC LIMIT 1`,
      routeDigest, String(event.thread_key));
    if (!row || row.route_digest !== routeDigest || Number(event.received_at) - Number(row.updated_at) >= PREP_OFFER_MAX_AGE_MS)
      return null;
    if(row.kind==='scheduled_text')return Number.isSafeInteger(row.provider_sent_at_ms)&&Number(row.provider_sent_at_ms)>0
      ? {providerSentAtMs:Number(row.provider_sent_at_ms)} : {};
    if(row.kind!=='interactive'||!offerAuthority||!Number.isSafeInteger(row.provider_sent_at_ms)||Number(row.provider_sent_at_ms)<1)return null;
    const thread=String(event.thread_key);
    const sameRoute = "(d.route_digest=? OR d.id IN (SELECT p.dispatch_id FROM prepared_replies p JOIN runs r ON r.id=p.run_id WHERE r.thread_key=?))";
    if(this.one(`SELECT d.id FROM dispatches d WHERE ${sameRoute} AND d.id<>? AND
        (d.status IN ('prepared','dispatching') OR d.created_at>=?) LIMIT 1`,
      routeDigest,thread,String(row.id),Number(row.updated_at)))return null;
    const matches=this.db.prepare(`SELECT a.id,a.source_event_id FROM actions a
      JOIN prepared_replies p ON p.event_id=a.source_event_id JOIN runs r ON r.id=p.run_id
      WHERE p.dispatch_id=? AND r.thread_key=? AND a.kind=? AND a.status='succeeded'`)
      .all(String(row.id),thread,ownerMeetingPrepOfferKind) as Row[];
    if(matches.length!==1)return null;
    try{
      const action=this.readOwnerMeetingPrepOfferAction(String(matches[0]!.id),offerAuthority);
      const reply=this.getReplyForEvent(String(matches[0]!.source_event_id));
      if(action.status!=='succeeded'||action.refusal||!action.preview||action.sourceRevision!==reply?.source_revision||
         !reply||reply.dispatch_status!=='accepted'||reply.dispatch_id!==row.id||
         reply.payload!==ownerMeetingPrepOfferQuestion(action.preview.review))return null;
      if(action.receipt){
        if(action.receipt.accepted_dispatch_id!==row.id||action.receipt.provider_message_id!==row.provider_message_id||
           action.receipt.accepted_at_ms!==row.provider_sent_at_ms)return null;
      }else{
        const accepted=this.acceptedOwnerMeetingPrepOffer(String(matches[0]!.id),offerAuthority);
        if(!accepted||accepted.acceptedDelivery.accepted_dispatch_id!==row.id||
           accepted.acceptedDelivery.provider_message_id!==row.provider_message_id||
           accepted.acceptedDelivery.accepted_at_ms!==row.provider_sent_at_ms)return null;
      }
      return {providerSentAtMs:Number(row.provider_sent_at_ms),ownerMeetingPrepOfferActionId:String(matches[0]!.id)};
    }catch{return null;}
  }

  /** A fresh owner input may replace failed local ordering with exact provider ordering.
   * This never settles, retries or discards any uncertain delivery. */
  freezeProviderContinuation(eventId: string, routeDigest: string, inboundAtMs: number,
      proof: {messageId: string; sentAtMs: number},
      ownerOffer?:{actionId:string;authority:Omit<ExplicitOwnerLoopAuthority,'runId'>}): void {
    this.transaction(() => {
      const event = this.getInbound(eventId);
      if (!event || event.outcome !== 'pending' || !event.continuation_json ||
          JSON.parse(String(event.continuation_json)).eligible !== false || !/^[a-f0-9]{64}$/.test(routeDigest)
          || !Number.isSafeInteger(inboundAtMs) || !Number.isSafeInteger(proof.sentAtMs)
          || proof.sentAtMs >= inboundAtMs || inboundAtMs - proof.sentAtMs >= PREP_OFFER_MAX_AGE_MS) return;
      const latest = this.one('SELECT id FROM inbound_events WHERE thread_key=? ORDER BY rowid DESC LIMIT 1', String(event.thread_key));
      if (latest?.id !== eventId || this.one('SELECT id FROM runs WHERE event_id=? LIMIT 1', eventId)) return;
      const matches = this.db.prepare("SELECT * FROM dispatches WHERE route_digest=? AND status='accepted' AND provider_message_id=? AND updated_at<=?")
        .all(routeDigest, proof.messageId, Number(event.received_at)) as Row[];
      if (matches.length !== 1) return;
      const dispatch = matches[0]!;
      const ids = JSON.parse(String(dispatch.provider_message_ids ?? '[]'));
      if (ids.length !== 1 || ids[0] !== proof.messageId
          || dispatch.provider_sent_at_ms !== null && dispatch.provider_sent_at_ms !== proof.sentAtMs
          || this.hasUnresolvedDispatchAttempt(String(dispatch.id))) return;
      if(dispatch.kind==='interactive'){
        if(!ownerOffer)return;
        const candidate=this.prepContinuationCandidate(eventId,routeDigest,ownerOffer.authority);
        if(candidate?.ownerMeetingPrepOfferActionId!==ownerOffer.actionId||candidate.providerSentAtMs!==proof.sentAtMs)return;
      }else if(ownerOffer||dispatch.kind!=='scheduled_text')return;
      this.db.prepare('UPDATE inbound_events SET continuation_json=? WHERE id=?').run(JSON.stringify({version: 1,
        eligible: true, dispatch_id: dispatch.id, provider_message_id: proof.messageId, route_digest: routeDigest,
        inbound_provider_sent_at_ms: inboundAtMs, provider_sent_at_ms: proof.sentAtMs}), eventId);
    });
  }

  /** Frozen selector; duplicate ingress and recovered legacy input cannot acquire new evidence. */
  continuationArgs(eventId: string): OwnerMeetingPrepArgs | null {
    const event = this.getInbound(eventId);
    if (!event?.continuation_json) return null;
    const saved = JSON.parse(String(event.payload));
    const inbound = saved.inbound as Inbound;
    const frozen = JSON.parse(String(event.continuation_json));
    if (inbound.channel !== 'photon' || inbound.replyBound || inbound.media?.length || !frozen.eligible) return null;
    return ownerMeetingPrepArgsSchema.parse({provider_message_id: event.source_id,
      reply_to_message_id: null, provider_sent_at_ms: frozen.inbound_provider_sent_at_ms,
      selected_offer: {mode: 'accepted_dispatch', dispatch_id: frozen.dispatch_id,
        message_id: frozen.provider_message_id, provider_sent_at_ms: frozen.provider_sent_at_ms}});
  }

  /** Context only: the exact accepted background text frozen at this owner input. */
  acceptedBackgroundContext(eventId: string, sessionStartedAt: number): {text:string;sourceRevision:string}|undefined {
    const message = this.acceptedBackgroundMessage(eventId, sessionStartedAt);
    return message && message.text.length <= 16000
      ? {text:message.text,sourceRevision:message.sourceRevision} : undefined;
  }

  /** Original accepted output for the existing native history projection; never provider-returned prose. */
  acceptedBackgroundMessage(eventId: string, sessionStartedAt: number): {dispatchId:string;providerMessageId:string;text:string;sourceRevision:string;acceptedAt:number;routeDigest:string}|undefined {
    return acceptedBackgroundMessage(this, eventId, sessionStartedAt);
  }

  getInbound(eventId: string): Row | undefined { return this.one('SELECT * FROM inbound_events WHERE id=?', eventId); }

  /** A new generation fences late actions and replies from any prior run in the thread. */
  startRun(input: RunInput): Row {
    required(input.runId, 'runId'); required(input.eventId, 'eventId');
    return this.transaction(() => {
      const event = this.getInbound(input.eventId);
      if (!event) throw new Error('inbound event missing');
      if (event.outcome !== 'pending') throw new Error('inbound event already resolved');
      if (this.getRun(input.runId)) throw new Error('run id already used');
      const threadKey = event.thread_key as string;
      // Retention never deletes an inbound row in a thread with a running run,
      // so SQLite cannot reuse a rowid at/below this captured boundary.
      const admittedThrough = this.one('SELECT max(rowid) AS sequence FROM inbound_events WHERE thread_key=?', threadKey)?.sequence;
      if (typeof admittedThrough !== 'number' || admittedThrough < 1) throw new Error('run admission boundary is unavailable');
      this.db.prepare('INSERT INTO thread_generations(thread_key,generation) VALUES(?,1) ON CONFLICT(thread_key) DO UPDATE SET generation=generation+1')
        .run(threadKey);
      const generation = this.one('SELECT generation FROM thread_generations WHERE thread_key=?', threadKey)!.generation as number;
      this.db.prepare("UPDATE runs SET status='cancelled',ended_at=? WHERE thread_key=? AND status='running'")
        .run(now(), threadKey);
      this.db.prepare("INSERT INTO runs(id,event_id,thread_key,generation,admitted_through,status,started_at) VALUES(?,?,?,?,?,'running',?)")
        .run(input.runId, input.eventId, threadKey, generation, admittedThrough, now());
      return this.getRun(input.runId)!;
    });
  }

  getRun(runId: string): Row | undefined { return this.one('SELECT * FROM runs WHERE id=?', runId); }

  private isRunGenerationCurrent(runId: string): boolean {
    const run = this.getRun(runId);
    return !!run && run.status !== 'cancelled' &&
      this.one('SELECT generation FROM thread_generations WHERE thread_key=?', run.thread_key as string)?.generation === run.generation;
  }

  isRunCurrent(runId: string): boolean {
    const run = this.getRun(runId);
    return !!run && run.status === 'running' && this.isRunGenerationCurrent(runId);
  }

  cancelRun(runId: string): boolean {
    return this.transaction(() => {
      if (!this.isRunCurrent(runId)) return false;
      const run = this.getRun(runId)!;
      this.db.prepare('UPDATE thread_generations SET generation=generation+1 WHERE thread_key=?').run(run.thread_key as string);
      this.db.prepare("UPDATE runs SET status='cancelled',ended_at=? WHERE id=?").run(now(), runId);
      return true;
    });
  }

  completeRun(runId: string): boolean {
    return this.transaction(() => {
      if (!this.isRunCurrent(runId)) return false;
      this.db.prepare("UPDATE runs SET status='completed',ended_at=? WHERE id=?").run(now(), runId);
      return true;
    });
  }

  resolveInbound(eventId: string, outcome: 'resolved' | 'cancelled'): void {
    this.transaction(() => {
      const changed = this.db.prepare("UPDATE inbound_events SET outcome=? WHERE id=? AND outcome='pending'").run(outcome, eventId);
      if (changed.changes !== 1) throw new Error('inbound event missing or already resolved');
    });
  }

  /**
   * Cancel earlier input through a received stop, retaining the stop for its own durable acknowledgement.
   * The stop targets the newest earlier pending input once; its application marker makes replay
   * preserve that decision. An older source still awaiting delayed proof (a pending-outcome
   * notice, or an uncertain delayed-policy action) is a different request and stays open, so an
   * unrelated /stop cannot suppress its final confirmation; a /stop whose target is that source
   * still closes it and suppresses the late acknowledgement.
   */
  stopThrough(eventId: string): void {
    this.transaction(() => {
      const event = this.one('SELECT rowid AS sequence,thread_key,stop_applied FROM inbound_events WHERE id=?', eventId);
      if (!event) throw new Error('inbound event missing');
      if (event.stop_applied === 1) return;
      this.db.prepare('UPDATE inbound_events SET stop_applied=1 WHERE id=?').run(eventId);
      // A pre-marker control reply is only prepared after stopThrough committed.
      // Preserve that already-applied stop when upgrading an older journal.
      if (this.one("SELECT 1 FROM prepared_replies WHERE event_id=? AND purpose='owner_control' LIMIT 1", eventId)) return;
      const thread = event.thread_key as string;
      const sequence = event.sequence as number;
      const target = (this.one("SELECT id FROM inbound_events WHERE thread_key=? AND rowid<? AND outcome='pending' ORDER BY rowid DESC LIMIT 1", thread, sequence)?.id as string | undefined) ?? null;
      const kinds = DELAYED_ACTION_KINDS.map(() => '?').join(',');
      const preserved = JSON.stringify((this.db.prepare(`SELECT e.id FROM inbound_events e WHERE e.thread_key=? AND e.rowid<? AND e.outcome='pending' AND e.id IS NOT ? AND (EXISTS (SELECT 1 FROM prepared_replies p WHERE p.event_id=e.id AND p.purpose='outcome_pending') OR EXISTS (SELECT 1 FROM actions a WHERE a.source_event_id=e.id AND a.status IN ('executing','unknown') AND a.kind IN (${kinds})))`)
        .all(thread, sequence, target, ...DELAYED_ACTION_KINDS) as Row[]).map(row => row.id as string));
      this.db.prepare("UPDATE dispatches SET status='cancelled',updated_at=? WHERE status='prepared' AND id IN (SELECT p.dispatch_id FROM prepared_replies p JOIN inbound_events e ON e.id=p.event_id WHERE e.thread_key=? AND e.rowid<? AND e.id NOT IN (SELECT value FROM json_each(?)))")
        .run(now(), thread, sequence, preserved);
      this.db.prepare("UPDATE runs SET status='cancelled',ended_at=? WHERE status='running' AND event_id IN (SELECT id FROM inbound_events WHERE thread_key=? AND rowid<? AND id NOT IN (SELECT value FROM json_each(?)))")
        .run(now(), thread, sequence, preserved);
      this.db.prepare("UPDATE inbound_events SET outcome='cancelled' WHERE outcome='pending' AND thread_key=? AND rowid<? AND id NOT IN (SELECT value FROM json_each(?))")
        .run(thread, sequence, preserved);
    });
  }

  private insertDispatch(input: DispatchInput): Row {
    for (const key of ['dispatchId', 'destination', 'payload'] as const) required(input[key], key);
    if (!['interactive', 'gallery', 'scheduled_text'].includes(input.kind)) throw new Error('invalid dispatch kind');
    const maxAttempts = input.maxAttempts ?? (input.kind === 'scheduled_text' ? SCHEDULED_TEXT_MAX_ATTEMPTS : 1);
    if (!Number.isSafeInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > SCHEDULED_TEXT_MAX_ATTEMPTS ||
        (input.kind !== 'scheduled_text' && maxAttempts !== 1)) throw new Error('invalid maxAttempts');
    if (input.routeDigest !== undefined && !/^[a-f0-9]{64}$/u.test(input.routeDigest)) {
      throw new Error('invalid dispatch route digest');
    }
    if (input.sourceRevision !== undefined && !/^[a-f0-9]{64}$/.test(input.sourceRevision)) throw new Error('invalid dispatch source revision');
    const payloadHash = hash(input.payload);
    const existing = this.getDispatch(input.dispatchId);
    if (existing) {
      if (existing.kind !== input.kind || existing.destination !== input.destination || existing.payload_hash !== payloadHash || existing.max_attempts !== maxAttempts)
        throw new Error('dispatch identity conflict');
      if (input.sourceRevision !== undefined && existing.source_revision !== null && existing.source_revision !== input.sourceRevision)
        throw new Error('dispatch source identity conflict');
      if (input.routeDigest !== undefined) {
        if (existing.route_digest === null) {
          if (existing.status === 'prepared') throw new Error('legacy dispatch route proof unavailable');
        } else if (existing.route_digest !== input.routeDigest) {
          throw new Error('dispatch route identity conflict');
        }
      }
      return existing;
    }
    const timestamp = now();
    this.db.prepare("INSERT INTO dispatches(id,kind,destination,payload_hash,payload,route_digest,source_revision,status,max_attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'prepared',?,?,?)")
      .run(input.dispatchId, input.kind, input.destination, payloadHash, input.payload,
        input.routeDigest ?? null, input.sourceRevision ?? null, maxAttempts, timestamp, timestamp);
    return this.getDispatch(input.dispatchId)!;
  }

  /** Scheduled receiver sends do not need an invented interactive event or run. */
  prepareDispatch(input: DispatchInput): Row { return this.transaction(() => this.insertDispatch(input)); }

  /** A reply and its dispatch binding are one transaction; the transcript is never the send journal. */
  prepareReply(input: ReplyInput): Row {
    for (const key of ['replyId', 'runId', 'eventId', 'dispatchId', 'destination', 'payload'] as const) required(input[key], key);
    if (!['interactive', 'gallery', 'scheduled_text'].includes(input.kind)) throw new Error('invalid dispatch kind');
    if (input.purpose !== undefined && !['outcome_pending', 'acknowledgement_unavailable', 'owner_control', 'model_failure'].includes(input.purpose)) throw new Error('invalid reply purpose');
    if (input.sourceRevision !== undefined && !/^[A-Za-z0-9_-]{1,160}$/.test(input.sourceRevision)) throw new Error('invalid source revision');
    return this.transaction(() => {
      if (!this.isRunCurrent(input.runId) || this.getRun(input.runId)?.event_id !== input.eventId) throw new Error('run is stale');
      const covered = input.coveredEventIds ?? [input.eventId];
      if (!covered.length || !covered.includes(input.eventId) || new Set(covered).size !== covered.length ||
          covered.some(id => !this.getInbound(id) || this.getInbound(id)?.thread_key !== this.getInbound(input.eventId)?.thread_key)) {
        throw new Error('invalid reply event coverage');
      }
      const existing = this.getPreparedReply(input.replyId);
      if (existing) throw new Error('reply id already used');
      if (this.getDispatch(input.dispatchId)) throw new Error('dispatch id already used');
      const timestamp = now();
      this.insertDispatch({...input, sourceRevision: undefined});
      this.db.prepare('INSERT INTO prepared_replies(id,event_id,run_id,dispatch_id,payload,covered_event_ids,prepared_at,source_revision,purpose) VALUES(?,?,?,?,?,?,?,?,?)')
        .run(input.replyId, input.eventId, input.runId, input.dispatchId, input.payload, JSON.stringify(covered), timestamp, input.sourceRevision ?? null, input.purpose ?? null);
      return this.getPreparedReply(input.replyId)!;
    });
  }

  getPreparedReply(replyId: string): Row | undefined { return this.one('SELECT * FROM prepared_replies WHERE id=?', replyId); }
  getDispatch(dispatchId: string): Row | undefined { return this.one('SELECT * FROM dispatches WHERE id=?', dispatchId); }
  listPendingInbound(): Row[] {
    return this.db.prepare("SELECT * FROM inbound_events WHERE outcome='pending' ORDER BY rowid").all() as Row[];
  }
  listPendingInboundForThread(eventId: string): Row[] {
    const event = this.getInbound(eventId);
    if (!event) throw new Error('inbound event missing');
    return this.db.prepare("SELECT * FROM inbound_events WHERE thread_key=? AND outcome='pending' ORDER BY rowid")
      .all(event.thread_key as string) as Row[];
  }
  listUnknownActionsForThread(eventId: string): Row[] {
    const event = this.getInbound(eventId);
    if (!event) throw new Error('inbound event missing');
    return this.db.prepare("SELECT a.* FROM actions a JOIN runs r ON r.id=a.run_id WHERE r.thread_key=? AND a.status IN ('executing','unknown') ORDER BY a.created_at,a.id")
      .all(event.thread_key as string) as Row[];
  }
  /** Delayed-policy actions run for their own still-pending original owner event in this thread. */
  listDelayedActionsForPendingSources(eventId: string): Row[] {
    const event = this.getInbound(eventId);
    if (!event) throw new Error('inbound event missing');
    return this.db.prepare(`SELECT a.* FROM actions a JOIN runs r ON r.id=a.run_id JOIN inbound_events e ON e.id=a.source_event_id WHERE r.thread_key=? AND e.thread_key=r.thread_key AND e.outcome='pending' AND r.event_id=a.source_event_id AND a.kind IN (${DELAYED_ACTION_KINDS.map(() => '?').join(',')}) ORDER BY a.created_at,a.id`)
      .all(event.thread_key as string, ...DELAYED_ACTION_KINDS) as Row[];
  }
  /** Natural reminder tools may use only independently authenticated, single owner input. */
  ownerReminderToolAdmission(authority: ExplicitOwnerLoopAuthority, eventId: string,
                            _single = false, current = true, resolved = false): Omit<OwnerReminderToolRequest, 'actionId' | 'args'> {
    const run = this.getRun(authority.runId);
    const event = this.getInbound(eventId);
    if (!run || !event || run.event_id !== eventId || (current && !this.isRunCurrent(authority.runId)) ||
        (event.outcome !== 'pending' && !(resolved && !current && event.outcome === 'resolved') &&
          !(authority.inspectClosedSource === true && !current)) || run.thread_key !== event.thread_key ||
        event.account_key !== authority.accountId || event.sender !== authority.ownerId ||
        event.line_key !== authority.lineKey || event.channel !== authority.channel ||
        !/^[a-f0-9]{64}$/.test(authority.scopeHash ?? '') || typeof event.payload !== 'string' ||
        hash(event.payload) !== event.payload_hash) throw new Error('Owner reminder admission is invalid');
    const bound = this.one('SELECT rowid AS sequence FROM inbound_events WHERE id=?', eventId);
    if (!bound || typeof run.admitted_through !== 'number' || Number(bound.sequence) > run.admitted_through)
      throw new Error('Owner reminder admission is invalid');
    let saved: Record<string, unknown>;
    try {saved = JSON.parse(event.payload);} catch {throw new Error('Owner reminder admission is invalid');}
    const inbound = saved.inbound as Record<string, unknown> | undefined;
    const context = saved.context as Record<string, unknown> | undefined;
    if (!inbound || !context || JSON.stringify(saved) !== event.payload ||
        inbound.eventId !== event.source_id || inbound.accountId !== event.account_key ||
        inbound.senderId !== event.sender || inbound.channel !== event.channel ||
        context.lineKey !== event.line_key || typeof context.threadId !== 'string' ||
        hash(JSON.stringify([event.account_key, event.line_key, event.channel, context.threadId])) !== event.thread_key ||
        typeof inbound.text !== 'string' || !inbound.text.trim() || inbound.text.length > 16000 ||
        inbound.text.startsWith('/') || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(inbound.text))
      throw new Error('Owner reminder admission is invalid');
    return {eventId, payloadHash: event.payload_hash as string, text: inbound.text,
      admittedAtMs: event.received_at as number, providerThreadId: context.threadId};
  }

  prepareOwnerReminderToolAction(authority: ExplicitOwnerLoopAuthority, eventId: string,
                                input: OwnerReminderToolArgs): OwnerReminderToolRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerReminderToolKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = ownerReminderToolArgsSchema.parse(input);
      if ('operation' in args && args.operation === 'conditional' && !admitted.text.includes(args.condition))
        throw new Error('Owner condition quote is unavailable');
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerTaskToolKind, ownerPreferenceToolKind))
        throw new Error('Owner message already has another private mutation');
      const binding = stableActionIdentity(eventId, ownerReminderToolKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerReminderToolKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Owner reminder event conflicts with saved arguments');
      if (previous) {
        this.readOwnerReminderToolAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      } else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerReminderToolKind, binding.payloadHash, eventId,
            admitted.payloadHash, hash(admitted.text), JSON.stringify({args, scope_hash: authority.scopeHash}), stamp, stamp);
      }
      return {...admitted, actionId: binding.actionId, args};
    });
  }

  /** Shared query mechanics only; callers retain per-kind admission and receipt checks. */
  private listOwnerActionsForThread(eventId: string, kind: string): Row[] {
    const event = this.getInbound(eventId);
    return event ? this.db.prepare('SELECT a.* FROM actions a JOIN runs r ON r.id=a.run_id WHERE r.thread_key=? AND a.kind=? ORDER BY a.created_at,a.id')
      .all(event.thread_key as string, kind) as Row[] : [];
  }

  private assertNoOtherOwnerAction(eventId: string, kind: string): void {
    if (this.one("SELECT id FROM actions WHERE source_event_id=? AND kind IN ('owner_reminder_tool','owner_loop_tool','owner_preference_tool','owner_routine_tool','owner_gmail_draft_tool','owner_calendar_tool','owner_contacts_tool','owner_bridge_send_tool','owner_brief_request','owner_meeting_prep_request','owner_meeting_prep_offer','owner_offer_reply','owner_skill_request','owner_web_read') AND kind<>?", eventId, kind))
      throw new Error('Owner message already has another private mutation');
  }

  listOwnerReminderToolActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerReminderToolKind);
  }

  readOwnerReminderToolAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerReminderToolRequest; status: string; receipt: OwnerReminderToolReceipt | null;
    rejection: 'event_conflict' | 'due_unavailable' | 'target_unavailable' | 'target_not_owner' | 'owner_span_unavailable' | 'target_changed' | 'source_unavailable' | null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerReminderToolKind || typeof action.source_event_id !== 'string')
      throw new Error('Owner reminder action is invalid');
    const admission = this.ownerReminderToolAdmission({...authority, runId: action.run_id as string}, action.source_event_id, false, false);
    let metadata: Record<string, unknown>;
    try {metadata = JSON.parse(action.result_json as string);} catch {throw new Error('Owner reminder action is invalid');}
    const args = ownerReminderToolArgsSchema.parse(metadata.args);
    if ('operation' in args && args.operation === 'conditional' && !admission.text.includes(args.condition))
      throw new Error('Owner condition quote is unavailable');
    const binding = stableActionIdentity(admission.eventId, ownerReminderToolKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
        action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Owner reminder action is invalid');
    const receipt = action.status === 'succeeded' ? ownerReminderToolReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
        receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
        !reminderReceiptMatches(args, receipt)))
      throw new Error('Owner reminder receipt is invalid');
    const rejection = action.status === 'rejected' ? metadata.rejection : null;
    if (rejection !== null && rejection !== 'event_conflict' && !['due_unavailable', 'target_unavailable', 'target_not_owner', 'owner_span_unavailable', 'target_changed', 'source_unavailable'].includes(rejection as string))
      throw new Error('Owner reminder rejection is invalid');
    return {request: {...admission, actionId, args}, status: action.status as string, receipt,
      rejection: rejection as 'event_conflict' | 'due_unavailable' | 'target_unavailable' | 'target_not_owner' | 'owner_span_unavailable' | 'target_changed' | 'source_unavailable' | null};
  }

  settleOwnerReminderToolAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>,
                               result: OwnerReminderToolReceipt | 'event_conflict' | 'due_unavailable' | 'target_unavailable' | 'target_not_owner' | 'owner_span_unavailable' | 'target_changed' | 'source_unavailable', reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerReminderToolAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing')) throw new Error('Owner reminder action is not eligible');
      const metadata = {args: saved.request.args, scope_hash: authority.scopeHash};
      if (typeof result === 'string') {
        if (reconciled || !['event_conflict', 'due_unavailable', 'target_unavailable', 'target_not_owner', 'owner_span_unavailable', 'target_changed', 'source_unavailable'].includes(result)) throw new Error('Owner reminder rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, rejection: result}), now(), actionId);
      } else {
        const receipt = ownerReminderToolReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerReminderToolKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
            receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
            !saved.request.args || !reminderReceiptMatches(saved.request.args, receipt))
          throw new Error('Owner reminder receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, receipt}), now(), actionId);
      }
    });
  }

  prepareOwnerBriefRequestAction(authority: ExplicitOwnerLoopAuthority, eventId: string,
                                input: OwnerBriefRequestArgs): OwnerBriefRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerBriefRequestKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = ownerBriefRequestArgsSchema.parse(input);
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerPreferenceToolKind))
        throw new Error('Owner message already has another private mutation');
      const source = this.getInbound(eventId)!;
      const saved = JSON.parse(source.payload as string);
      if (args.provider_message_id !== source.source_id
          || (args.remainder !== null && !saved.inbound.text?.includes(args.remainder)))
        throw new Error('Requested brief differs from admitted owner request');
      const binding = stableActionIdentity(eventId, ownerBriefRequestKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerBriefRequestKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Requested brief event conflicts with saved arguments');
      if (!previous || previous.status === 'pending') {
        const intent = ownerBriefIntent(admitted.text);
        if (!intent || (intent.kind !== 'auto' && args.kind !== intent.kind) || (args.refresh && !intent.refresh))
          throw new Error('Brief requires a current affirmative owner brief request');
      }
      if (previous) {
        this.readOwnerBriefRequestAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      } else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerBriefRequestKind, binding.payloadHash, eventId,
            admitted.payloadHash, hash(admitted.text), JSON.stringify({args, scope_hash: authority.scopeHash}), stamp, stamp);
      }
      return {...admitted, actionId: binding.actionId, args};
    });
  }

  listOwnerBriefRequestActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerBriefRequestKind);
  }

  readOwnerBriefRequestAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerBriefRequest; status: string; receipt: OwnerBriefRequestReceipt | null;
    rejection: string | null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerBriefRequestKind || typeof action.source_event_id !== 'string')
      throw new Error('Requested brief action is invalid');
    const admission = this.ownerReminderToolAdmission({...authority, runId: action.run_id as string}, action.source_event_id, false, false, true);
    let metadata: Record<string, unknown>;
    try {metadata = JSON.parse(action.result_json as string);} catch {throw new Error('Requested brief action is invalid');}
    const args = ownerBriefRequestArgsSchema.parse(metadata.args);
    const event = this.getInbound(admission.eventId)!;
    if (args.provider_message_id !== event.source_id
        || (args.remainder !== null && !admission.text.includes(args.remainder)))
      throw new Error('Requested brief differs from original admission');
    const binding = stableActionIdentity(admission.eventId, ownerBriefRequestKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
        action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Requested brief action is invalid');
    const receipt = action.status === 'succeeded' ? ownerBriefRequestReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
        receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
         receipt.outcome.refresh !== args.refresh || (args.kind !== 'auto' && receipt.outcome.kind !== args.kind)))
      throw new Error('Requested brief receipt is invalid');
    const rejection = action.status === 'rejected' ? String(metadata.rejection) : null;
    if (rejection !== null && !['event_conflict','pending_delivery','needs_context','unavailable'].includes(rejection))
      throw new Error('Requested brief rejection is invalid');
    return {request: {...admission, actionId, args}, status: action.status as string, receipt, rejection};
  }

  settleOwnerBriefRequestAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>,
                               result: OwnerBriefRequestReceipt | string, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerBriefRequestAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing')) throw new Error('Requested brief action is not eligible');
      const metadata = {args: saved.request.args, scope_hash: authority.scopeHash};
      if (typeof result === 'string') {
        if (reconciled || !['event_conflict','pending_delivery','needs_context','unavailable'].includes(result)) throw new Error('Requested brief rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, rejection: result}), now(), actionId);
      } else {
        const receipt = ownerBriefRequestReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerBriefRequestKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
            receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
            receipt.outcome.refresh !== saved.request.args.refresh || (saved.request.args.kind !== 'auto' && receipt.outcome.kind !== saved.request.args.kind))
          throw new Error('Requested brief receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, receipt}), now(), actionId);
      }
    });
  }

  prepareOwnerMeetingPrepAction(authority: ExplicitOwnerLoopAuthority, eventId: string,
                                input: OwnerMeetingPrepArgs): OwnerMeetingPrepRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerMeetingPrepKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = ownerMeetingPrepArgsSchema.parse(input);
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerPreferenceToolKind))
        throw new Error('Owner message already has another private mutation');
      const source = this.getInbound(eventId)!;
      const saved = JSON.parse(source.payload as string);
      const classified = meetingPrepSelectors(saved.inbound) ?? (clearMeetingPrepYes(admitted.text) ? this.continuationArgs(eventId) : null);
      if (!classified || args.provider_message_id !== source.source_id || args.reply_to_message_id !== classified.reply_to_message_id
          || args.provider_sent_at_ms !== classified.provider_sent_at_ms
          || JSON.stringify(args.selected_offer) !== JSON.stringify(classified.selected_offer))
        throw new Error('Meeting prep differs from admitted owner request');
      const binding = stableActionIdentity(eventId, ownerMeetingPrepKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerMeetingPrepKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Meeting prep event conflicts with saved arguments');
      if (previous) {
        this.readOwnerMeetingPrepAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      } else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerMeetingPrepKind, binding.payloadHash, eventId,
            admitted.payloadHash, hash(admitted.text), JSON.stringify({args, scope_hash: authority.scopeHash}), stamp, stamp);
      }
      return {...admitted, actionId: binding.actionId, args};
    });
  }

  listOwnerMeetingPrepActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerMeetingPrepKind);
  }

  readOwnerMeetingPrepAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerMeetingPrepRequest; status: string; receipt: OwnerMeetingPrepReceipt | null;
    rejection: string | null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerMeetingPrepKind || typeof action.source_event_id !== 'string')
      throw new Error('Meeting prep action is invalid');
    const admission = this.ownerReminderToolAdmission({...authority, runId: action.run_id as string}, action.source_event_id, false, false, true);
    let metadata: Record<string, unknown>;
    try {metadata = JSON.parse(action.result_json as string);} catch {throw new Error('Meeting prep action is invalid');}
    const args = ownerMeetingPrepArgsSchema.parse(metadata.args);
    const event = this.getInbound(admission.eventId)!;
    const classified = meetingPrepSelectors(JSON.parse(event.payload as string).inbound) ?? (clearMeetingPrepYes(admission.text) ? this.continuationArgs(admission.eventId) : null);
    if (!classified || args.provider_message_id !== event.source_id || args.reply_to_message_id !== classified.reply_to_message_id
        || args.provider_sent_at_ms !== classified.provider_sent_at_ms
          || JSON.stringify(args.selected_offer) !== JSON.stringify(classified.selected_offer))
      throw new Error('Meeting prep differs from original admission');
    const binding = stableActionIdentity(admission.eventId, ownerMeetingPrepKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
        action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Meeting prep action is invalid');
    const receipt = action.status === 'succeeded' ? ownerMeetingPrepReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
        receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash))
      throw new Error('Meeting prep receipt is invalid');
    const rejection = action.status === 'rejected' ? String(metadata.rejection) : null;
    if (rejection !== null && !ownerMeetingPrepRejectionSchema.safeParse(rejection).success)
      throw new Error('Meeting prep rejection is invalid');
    return {request: {...admission, actionId, args}, status: action.status as string, receipt, rejection};
  }

  settleOwnerMeetingPrepAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>,
                               result: OwnerMeetingPrepReceipt | string, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerMeetingPrepAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing')) throw new Error('Meeting prep action is not eligible');
      const metadata = {args: saved.request.args, scope_hash: authority.scopeHash};
      if (typeof result === 'string') {
        if (reconciled || !ownerMeetingPrepRejectionSchema.safeParse(result).success) throw new Error('Meeting prep rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, rejection: result}), now(), actionId);
      } else {
        const receipt = ownerMeetingPrepReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerMeetingPrepKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
            receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash)
          throw new Error('Meeting prep receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, receipt}), now(), actionId);
      }
    });
  }

  prepareOwnerMeetingPrepOfferAction(authority:ExplicitOwnerLoopAuthority,eventId:string,
    input:OwnerMeetingPrepOfferArgs,sourceRevision:string):OwnerMeetingPrepOfferRequest{
    return this.transaction(()=>{
      this.assertNoOtherOwnerAction(eventId,ownerMeetingPrepOfferKind);
      const admitted=this.ownerReminderToolAdmission(authority,eventId,true);
      const args=ownerMeetingPrepOfferArgsSchema.parse(input);
      const source=this.getInbound(eventId);
      if(!source||source.channel!=='photon'||args.source_revision!==sourceRevision||!/^([a-f0-9]{64})$/.test(sourceRevision))
        throw new Error('Meeting prep offer source is invalid');
      const saved=JSON.parse(source.payload as string);
      const inbound=saved.inbound as Inbound;
      if(inbound.channel!=='photon'||inbound.replyBound||inbound.media?.length||!inbound.text||
          inbound.eventId!==source.source_id||inbound.accountId!==source.account_key||inbound.senderId!==source.sender)
        throw new Error('Meeting prep offer requires one direct owner message');
      const binding=stableActionIdentity(eventId,ownerMeetingPrepOfferKind,args);
      const previous=this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?',eventId,ownerMeetingPrepOfferKind);
      if(previous&&(previous.id!==binding.actionId||previous.payload_hash!==binding.payloadHash))
        throw new Error('Meeting prep offer event conflicts with saved arguments');
      if(previous){
        this.readOwnerMeetingPrepOfferAction(previous.id as string,authority);
        if(previous.status==='pending'&&previous.run_id!==authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId,now(),previous.id as string);
      }else{
        const stamp=now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId,authority.runId,ownerMeetingPrepOfferKind,binding.payloadHash,eventId,
            admitted.payloadHash,hash(admitted.text),JSON.stringify({args,scope_hash:authority.scopeHash,source_revision:sourceRevision}),stamp,stamp);
      }
      return {...admitted,actionId:binding.actionId,args,sourceRevision};
    });
  }

  listOwnerMeetingPrepOfferActionsForThread(eventId:string):Row[]{
    return this.listOwnerActionsForThread(eventId,ownerMeetingPrepOfferKind);
  }

  listPendingOwnerMeetingPrepOffers():Row[]{
    const at=now();
    const rows=this.db.prepare(`SELECT a.*,r.thread_key,d.provider_sent_at_ms AS accepted_at_ms,
        json_extract(a.result_json,'$.preview.review.start') AS event_start
      FROM actions a JOIN runs r ON r.id=a.run_id
      JOIN prepared_replies p ON p.event_id=a.source_event_id
      JOIN dispatches d ON d.id=p.dispatch_id
      WHERE a.kind=? AND a.status='succeeded' AND json_extract(a.result_json,'$.offer_receipt') IS NULL
        AND json_extract(a.result_json,'$.offer_refusal') IS NULL AND d.status='accepted'
        AND p.source_revision=json_extract(a.result_json,'$.source_revision')
        AND d.provider_sent_at_ms IS NOT NULL AND d.provider_sent_at_ms>0 AND d.provider_sent_at_ms<=?
        AND a.created_at+? > ?
      ORDER BY a.created_at,a.id LIMIT 100`)
      .all(ownerMeetingPrepOfferKind,at,PREP_OFFER_MAX_AGE_MS,at) as Row[];
    return rows.filter(row=>typeof row.event_start==='string'&&Number.isFinite(Date.parse(row.event_start as string))&&
      Date.parse(row.event_start as string)>at);
  }

  readOwnerMeetingPrepOfferAction(actionId:string,authority:Omit<ExplicitOwnerLoopAuthority,'runId'>):{
    request:OwnerMeetingPrepOfferRequest;status:string;preview:OwnerMeetingPrepOfferPreview|null;
    receipt:OwnerMeetingPrepOfferReceipt|null;refusal:OwnerMeetingPrepOfferRefusal|null;
    rejection:string|null;sourceRevision:string;
  }{
    const action=this.getAction(actionId);
    if(!action||action.kind!==ownerMeetingPrepOfferKind||typeof action.source_event_id!=='string'||
       !['pending','executing','unknown','succeeded','rejected'].includes(action.status as string))
      throw new Error('Meeting prep offer action is invalid');
    const admission=this.ownerReminderToolAdmission({...authority,runId:action.run_id as string},action.source_event_id,false,false,true);
    let metadata:Record<string,unknown>;
    try{metadata=JSON.parse(action.result_json as string);}catch{throw new Error('Meeting prep offer action is invalid');}
    const args=ownerMeetingPrepOfferArgsSchema.parse(metadata.args),revision=metadata.source_revision;
    const binding=stableActionIdentity(admission.eventId,ownerMeetingPrepOfferKind,args);
    if(typeof revision!=='string'||args.source_revision!==revision||metadata.scope_hash!==authority.scopeHash||
       action.id!==binding.actionId||action.payload_hash!==binding.payloadHash||action.source_payload_hash!==admission.payloadHash||
       action.source_text_hash!==hash(admission.text))throw new Error('Meeting prep offer action is invalid');
    const sourceRevision=revision as string,source=this.getInbound(admission.eventId);
    if(!source||source.channel!=='photon'||source.line_key!==authority.lineKey||source.sender!==authority.ownerId)
      throw new Error('Meeting prep offer authority changed');
    const preview=action.status==='succeeded'?ownerMeetingPrepOfferPreviewSchema.parse(metadata.preview):null;
    if(action.status==='succeeded'&&!preview)throw new Error('Meeting prep offer preview missing');
    if(preview&&(preview.action_id!==actionId||preview.event_id!==admission.eventId||preview.payload_hash!==admission.payloadHash||
       preview.args_hash!==binding.payloadHash||!ownerMeetingPrepOfferMatches(preview.review,args,sourceRevision)))
      throw new Error('Meeting prep offer preview binding invalid');
    const receipt=metadata.offer_receipt===undefined?null:ownerMeetingPrepOfferReceiptSchema.parse(metadata.offer_receipt);
    const refusal=metadata.offer_refusal===undefined?null:ownerMeetingPrepOfferRefusalSchema.parse(metadata.offer_refusal);
    if(receipt&&refusal)throw new Error('Meeting prep offer outcome conflicts');
    if(receipt){
      const reply=this.getReplyForEvent(admission.eventId),dispatch=reply?this.getDispatch(String(reply.dispatch_id)):undefined;
      if(!preview||receipt.action_id!==actionId||receipt.event_id!==admission.eventId||receipt.payload_hash!==admission.payloadHash||
         receipt.args_hash!==binding.payloadHash||receipt.offer_id!==preview.review.offer_id||receipt.review_hash!==preview.review.review_hash||
         !reply||reply.dispatch_status!=='accepted'||reply.dispatch_id!==receipt.accepted_dispatch_id||
         reply.payload!==ownerMeetingPrepOfferQuestion(preview.review)||reply.source_revision!==revision||
         !dispatch||dispatch.status!=='accepted'||dispatch.provider_message_id!==receipt.provider_message_id||
         dispatch.provider_sent_at_ms!==receipt.accepted_at_ms)
        throw new Error('Meeting prep offer receipt binding invalid');
    }
    const rejection=action.status==='rejected'?String(metadata.rejection):null;
    if(rejection!==null&&!ownerMeetingPrepOfferRejectionSchema.safeParse(rejection).success)
      throw new Error('Meeting prep offer rejection is invalid');
    return {request:{...admission,actionId,args,sourceRevision},status:action.status as string,preview,receipt,refusal,rejection,sourceRevision};
  }

  settleOwnerMeetingPrepOfferAction(actionId:string,authority:Omit<ExplicitOwnerLoopAuthority,'runId'>,
    result:OwnerMeetingPrepOfferPreview|string,reconciled=false):void{
    this.transaction(()=>{
      const saved=this.readOwnerMeetingPrepOfferAction(actionId,authority);
      if(saved.status!==(reconciled?'unknown':'executing'))throw new Error('Meeting prep offer action is not eligible');
      const metadata={args:saved.request.args,scope_hash:authority.scopeHash,source_revision:saved.sourceRevision};
      if(typeof result==='string'){
        if(reconciled||!ownerMeetingPrepOfferRejectionSchema.safeParse(result).success)throw new Error('Meeting prep offer rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata,rejection:result}),now(),actionId);
      }else{
        const preview=ownerMeetingPrepOfferPreviewSchema.parse(result);
        const binding=stableActionIdentity(saved.request.eventId,ownerMeetingPrepOfferKind,saved.request.args);
        if(preview.action_id!==actionId||preview.event_id!==saved.request.eventId||preview.payload_hash!==saved.request.payloadHash||
           preview.args_hash!==binding.payloadHash||!ownerMeetingPrepOfferMatches(preview.review,saved.request.args,saved.sourceRevision))
          throw new Error('Meeting prep offer preview is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata,preview}),now(),actionId);
      }
    });
  }

  acceptedOwnerMeetingPrepOffer(actionId:string,authority:Omit<ExplicitOwnerLoopAuthority,'runId'>):{
    request:OwnerMeetingPrepOfferRequest;acceptedDelivery:OwnerMeetingPrepOfferAcceptedDelivery;
  }|null{
    const saved=this.readOwnerMeetingPrepOfferAction(actionId,authority);
    if(saved.status!=='succeeded'||!saved.preview||saved.receipt)return null;
    const reply=this.getReplyForEvent(saved.request.eventId);
    if(!reply||reply.dispatch_status!=='accepted'||reply.source_revision!==saved.sourceRevision||
       reply.payload!==ownerMeetingPrepOfferQuestion(saved.preview.review))return null;
    const dispatch=this.getDispatch(String(reply.dispatch_id));
    if(!dispatch||dispatch.status!=='accepted'||typeof dispatch.provider_message_id!=='string'||
       typeof dispatch.provider_sent_at_ms!=='number'||!Number.isSafeInteger(dispatch.provider_sent_at_ms)||dispatch.provider_sent_at_ms<1)return null;
    const run=this.getRun(String(reply.run_id)),event=this.getInbound(saved.request.eventId);
    let destination:unknown;
    try{destination=(JSON.parse(String(event?.payload)) as {context?:{destination?:unknown}}).context?.destination;}catch{return null;}
    if(!run||!event||run.event_id!==saved.request.eventId||run.thread_key!==event.thread_key||dispatch.destination!==destination)return null;
    return {request:saved.request,acceptedDelivery:ownerMeetingPrepOfferAcceptedDeliverySchema.parse({
      accepted_dispatch_id:reply.dispatch_id,provider_message_id:dispatch.provider_message_id,source_revision:saved.sourceRevision,
      review_hash:saved.preview.review.review_hash,payload_hash:hash(ownerMeetingPrepOfferQuestion(saved.preview.review)),
      accepted_at_ms:dispatch.provider_sent_at_ms,
    })};
  }

  settleOwnerMeetingPrepOfferReceipt(actionId:string,authority:Omit<ExplicitOwnerLoopAuthority,'runId'>,
    receipt:OwnerMeetingPrepOfferReceipt):void{
    this.transaction(()=>{
      const action=this.getAction(actionId);
      if(!action||action.kind!==ownerMeetingPrepOfferKind||action.status!=='succeeded')throw new Error('Meeting prep offer unavailable');
      const metadata=JSON.parse(String(action.result_json)),before=ownerMeetingPrepOfferPreviewSchema.parse(metadata.preview);
      const result=ownerMeetingPrepOfferReceiptSchema.parse(receipt);
      const {args,scope_hash,source_revision}=metadata as {args:OwnerMeetingPrepOfferArgs;scope_hash:string;source_revision:string};
      if(scope_hash!==authority.scopeHash)throw new Error('Meeting prep offer scope changed');
      const accepted=this.acceptedOwnerMeetingPrepOffer(actionId,authority);
      if(!accepted||result.action_id!==actionId||result.event_id!==action.source_event_id||
         result.payload_hash!==action.source_payload_hash||result.args_hash!==before.args_hash||
         result.review_hash!==before.review.review_hash||result.offer_id!==before.review.offer_id||
         result.accepted_dispatch_id!==accepted.acceptedDelivery.accepted_dispatch_id||
         result.provider_message_id!==accepted.acceptedDelivery.provider_message_id||
         result.accepted_at_ms!==accepted.acceptedDelivery.accepted_at_ms||
         before.review.source_revision!==source_revision||args.source_revision!==source_revision)
        throw new Error('Meeting prep offer receipt invalid');
      if(metadata.offer_receipt&&JSON.stringify(metadata.offer_receipt)!==JSON.stringify(result))throw new Error('Meeting prep offer receipt changed');
      this.db.prepare('UPDATE actions SET result_json=?,updated_at=? WHERE id=?')
        .run(JSON.stringify({...metadata,offer_receipt:result}),now(),actionId);
    });
  }

  refuseOwnerMeetingPrepOfferReceipt(actionId:string,authority:Omit<ExplicitOwnerLoopAuthority,'runId'>,
    refusal:OwnerMeetingPrepOfferRefusal):void{
    this.transaction(()=>{
      const saved=this.readOwnerMeetingPrepOfferAction(actionId,authority);
      if(saved.status!=='succeeded'||saved.receipt)throw new Error('Meeting prep offer receipt is not refusible');
      const action=this.getAction(actionId)!;
      const metadata=JSON.parse(String(action.result_json)) as Record<string,unknown>;
      if(metadata.offer_refusal!==undefined&&metadata.offer_refusal!==refusal)throw new Error('Meeting prep offer refusal changed');
      this.db.prepare('UPDATE actions SET result_json=?,updated_at=? WHERE id=?')
        .run(JSON.stringify({...metadata,offer_refusal:ownerMeetingPrepOfferRefusalSchema.parse(refusal)}),now(),actionId);
    });
  }

  prepareOwnerOfferReplyAction(authority: ExplicitOwnerLoopAuthority, eventId: string,
                                input: OwnerOfferReplyArgs): OwnerOfferReplyRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerOfferReplyKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = ownerOfferReplyArgsSchema.parse(input);
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerPreferenceToolKind))
        throw new Error('Owner message already has another private mutation');
      const source = this.getInbound(eventId)!;
      const saved = JSON.parse(source.payload as string);
      const classified = offerReplySelectors(saved.inbound) ?? (ownerOfferReplyAction(admitted.text) === 'declined' ? this.continuationArgs(eventId) : null);
      if (!classified || args.provider_message_id !== source.source_id || args.reply_to_message_id !== classified.reply_to_message_id
          || args.provider_sent_at_ms !== classified.provider_sent_at_ms
          || JSON.stringify(args.selected_offer) !== JSON.stringify(classified.selected_offer))
        throw new Error('Offer reply differs from admitted owner request');
      const binding = stableActionIdentity(eventId, ownerOfferReplyKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerOfferReplyKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Offer reply event conflicts with saved arguments');
      if (previous) {
        this.readOwnerOfferReplyAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      } else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerOfferReplyKind, binding.payloadHash, eventId,
            admitted.payloadHash, hash(admitted.text), JSON.stringify({args, scope_hash: authority.scopeHash}), stamp, stamp);
      }
      return {...admitted, actionId: binding.actionId, args};
    });
  }

  listOwnerOfferReplyActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerOfferReplyKind);
  }

  readOwnerOfferReplyAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerOfferReplyRequest; status: string; receipt: OwnerOfferReplyReceipt | null;
    rejection: string | null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerOfferReplyKind || typeof action.source_event_id !== 'string')
      throw new Error('Offer reply action is invalid');
    const admission = this.ownerReminderToolAdmission({...authority, runId: action.run_id as string}, action.source_event_id, false, false, true);
    let metadata: Record<string, unknown>;
    try {metadata = JSON.parse(action.result_json as string);} catch {throw new Error('Offer reply action is invalid');}
    const args = ownerOfferReplyArgsSchema.parse(metadata.args);
    const event = this.getInbound(admission.eventId)!;
    const classified = offerReplySelectors(JSON.parse(event.payload as string).inbound) ?? (ownerOfferReplyAction(admission.text) === 'declined' ? this.continuationArgs(admission.eventId) : null);
    if (!classified || args.provider_message_id !== event.source_id || args.reply_to_message_id !== classified.reply_to_message_id
        || args.provider_sent_at_ms !== classified.provider_sent_at_ms
          || JSON.stringify(args.selected_offer) !== JSON.stringify(classified.selected_offer))
      throw new Error('Offer reply differs from original admission');
    const binding = stableActionIdentity(admission.eventId, ownerOfferReplyKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
        action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Offer reply action is invalid');
    const receipt = action.status === 'succeeded' ? ownerOfferReplyReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
        receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash || receipt.outcome.action !== ownerOfferReplyAction(admission.text)))
      throw new Error('Offer reply receipt is invalid');
    const rejection = action.status === 'rejected' ? String(metadata.rejection) : null;
    if (rejection !== null && !ownerOfferReplyRejectionSchema.safeParse(rejection).success)
      throw new Error('Offer reply rejection is invalid');
    return {request: {...admission, actionId, args}, status: action.status as string, receipt, rejection};
  }

  settleOwnerOfferReplyAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>,
                               result: OwnerOfferReplyReceipt | string, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerOfferReplyAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing')) throw new Error('Offer reply action is not eligible');
      const metadata = {args: saved.request.args, scope_hash: authority.scopeHash};
      if (typeof result === 'string') {
        if (reconciled || !ownerOfferReplyRejectionSchema.safeParse(result).success) throw new Error('Offer reply rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, rejection: result}), now(), actionId);
      } else {
        const receipt = ownerOfferReplyReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerOfferReplyKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
            receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash || receipt.outcome.action !== ownerOfferReplyAction(saved.request.text))
          throw new Error('Offer reply receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, receipt}), now(), actionId);
      }
    });
  }

  prepareOwnerTaskToolAction(authority: ExplicitOwnerLoopAuthority, eventId: string,
                                input: OwnerTaskToolArgs): OwnerTaskToolRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerTaskToolKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = ownerTaskToolArgsSchema.parse(input);
      if(args.operation==='add_identifier')throw new Error('Identifier teaching requires an accepted review and separate yes');
      if(args.operation==='fact_add'&&stableActionIdentity(eventId,ownerTaskToolKind,args).actionId!==stableActionIdentity(eventId,ownerTaskToolKind,this.selectOwnerFactProposal(authority,eventId,args.review.source_revision)).actionId)throw new Error('Fact review binding unavailable');
      if(args.operation==='identifier'&&stableActionIdentity(eventId,ownerTaskToolKind,args).actionId!==stableActionIdentity(eventId,ownerTaskToolKind,this.selectOwnerIdentifierProposal(authority,eventId,args.review.source_revision)).actionId)throw new Error('Identifier review binding unavailable');
      if(args.operation==='relation'&&stableActionIdentity(eventId,ownerTaskToolKind,args).actionId!==stableActionIdentity(eventId,ownerTaskToolKind,this.selectOwnerRelationProposal(authority,eventId,args.review.source_revision)).actionId)throw new Error('Relationship review binding unavailable');
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerPreferenceToolKind))
        throw new Error('Owner message already has another private mutation');
      if(args.operation==='cleanup'){if(!cleanupRequestMatches(args,admitted.text)||JSON.stringify(args)!==JSON.stringify(this.selectOwnerCleanupProposal(authority,eventId,args.list.source_revision,args.selection?{selection:args.selection}:args.items)))throw new Error('Cleanup selection unavailable');}
      if(args.operation==='close'&&args.correction&&!resolveManyOwnerTextMatches([{name:args.correction,correction:args.correction}],admitted.text,'owner_tasks'))throw new Error('Exact single-task closure unavailable');
      if(args.operation==='resolve_many'&&!resolveManyOwnerTextMatches(args.items,admitted.text,args.selection_mode))throw new Error('Exact multi-loop closure unavailable');
      if(args.operation==='propose_identifier'&&!admitted.text.includes(args.identifier))throw new Error('Identifier is not an exact owner span');
      if(args.operation==='add')for(const value of [args.contact,args.identifier])if(value!==undefined&&!admitted.text.includes(value))throw new Error('Task detail is not an exact owner span');
      if(args.operation==='fact_correct')for(const value of [args.title,args.company])if(value!==undefined&&!admitted.text.includes(value))throw new Error('Identity detail is not an exact owner span');
      if ((args.operation === 'add' || args.operation === 'fact_correct' || args.operation === 'propose_fact') && !admitted.text.includes(args.text)) throw new Error('Task text is not an exact owner span');
      if ((args.operation === 'correct' || args.operation === 'retarget' || args.operation === 'snooze' || args.operation === 'keep') && !admitted.text.includes(args.correction)) throw new Error('Correction is not an exact owner span');
      const binding = stableActionIdentity(eventId, ownerTaskToolKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerTaskToolKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Owner task event conflicts with saved arguments');
      if (previous) {
        this.readOwnerTaskToolAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      } else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerTaskToolKind, binding.payloadHash, eventId,
            admitted.payloadHash, hash(admitted.text), JSON.stringify({args, scope_hash: authority.scopeHash}), stamp, stamp);
      }
      return {...admitted, actionId: binding.actionId, args};
    });
  }


  selectOwnerCleanupProposal(authority:ExplicitOwnerLoopAuthority,eventId:string,sourceRevision:string,input:unknown):OwnerTaskToolArgs {
    const current=this.ownerReminderToolAdmission(authority,eventId,true);
    const selection=typeof input==='object'&&input!==null&&!Array.isArray(input)&&Object.keys(input).length===1&&'selection' in input?cleanupSelectionSchema.parse(input.selection):undefined;
    let items=selection?[]:cleanupItemsSchema.parse(input).slice().sort((a,b)=>a.index-b.index);
    if(!selection&&!cleanupSelectionsMatch(items,current.text))throw new OwnerTaskListUnavailable('Explicit numbered cleanup choices required');
    try {
      const prior=this.one('SELECT id,outcome,payload FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1',eventId,eventId);
      if(!prior||prior.outcome!=='resolved')throw new OwnerTaskListUnavailable('Accepted cleanup list unavailable');
      const action=this.listOwnerTaskToolActionsForThread(eventId).find(a=>{const metadata=JSON.parse(a.result_json as string);return metadata.args?.operation==='propose_cleanup'&&(a.source_event_id===prior.id||metadata.cleanup_repair_event_id===prior.id)&&ownerCleanupProposalCurrent(this,a,eventId);});if(!action)throw new OwnerTaskListUnavailable('Accepted cleanup list unavailable');
      const saved=this.readOwnerTaskToolAction(action.id as string,authority);const reply=this.getReplyForEvent(action.source_event_id as string);
      if(!saved.preview||saved.preview.review.operation!=='cleanup_list'||saved.sourceRevision!==sourceRevision||!reply||reply.dispatch_status!=='accepted'||reply.source_revision!==sourceRevision||reply.payload!==taskReviewText(saved.preview.review))throw new OwnerTaskListUnavailable('Accepted cleanup list unavailable');
      this.validateConfirmationReplyTarget(eventId,reply);
      const currentEvent=JSON.parse(this.getInbound(eventId)!.payload as string);const priorEvent=JSON.parse(this.getInbound(action.source_event_id as string)!.payload as string);if(JSON.stringify(currentEvent.context.destination)!==JSON.stringify(priorEvent.context.destination)||this.getDispatch(reply.dispatch_id as string)?.destination!==priorEvent.context.destination)throw new OwnerTaskListUnavailable('Cleanup route changed');
      for(const item of this.listOwnerTaskToolActionsForThread(eventId)){const value=JSON.parse(item.result_json as string);if(item.source_event_id!==eventId&&value.args?.operation==='cleanup'&&value.args.proposal?.action_id===action.id)throw new OwnerTaskListUnavailable('Cleanup list already consumed');}
      if(selection){try{items=cleanupSelectionItems(saved.preview.review,selection,current.text);}catch(error){if(error instanceof OwnerTaskListUnavailable)markCleanupSelectionRepair(this,action,eventId);throw error;}}
      return ownerTaskToolArgsSchema.parse({operation:'cleanup',list:saved.preview.review,items,...(selection?{selection}:{}),proposal:{event_id:action.source_event_id,action_id:action.id,args_hash:action.payload_hash,accepted_dispatch_id:reply.dispatch_id,source_revision:sourceRevision}});
    } catch(error) {if(error instanceof OwnerTaskListUnavailable)throw error;throw new OwnerTaskListUnavailable('Accepted cleanup list unavailable');}
  }

  selectOwnerRelationProposal(authority:ExplicitOwnerLoopAuthority,eventId:string,sourceRevision:string):OwnerTaskToolArgs {
    const current=this.ownerReminderToolAdmission(authority,eventId,true);
    if(!clearRoutineYes(current.text))throw new Error('A clear subsequent yes is required');
    const prior=this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1',eventId,eventId);
    if(!prior||prior.outcome!=='resolved')throw new Error('Accepted relationship review unavailable');
    const action=this.listOwnerTaskToolActionsForThread(eventId).find(a=>a.source_event_id===prior.id);if(!action)throw new Error('Accepted relationship review unavailable');
    const metadata=JSON.parse(action.result_json as string);if(metadata.next_event_id!==eventId||metadata.next_event_accepted!==true)throw new Error('Relationship review is not the current accepted question');
    const saved=this.readOwnerTaskToolAction(action.id as string,authority);const reply=this.getReplyForEvent(prior.id as string);
    if(!saved.preview||saved.preview.review.operation!=='relation'||saved.sourceRevision!==sourceRevision||!reply||reply.dispatch_status!=='accepted'||reply.source_revision!==sourceRevision||reply.payload!==taskReviewText(saved.preview.review))throw new Error('Accepted relationship review unavailable');
    this.validateConfirmationReplyTarget(eventId,reply);
    for(const item of this.listOwnerTaskToolActionsForThread(eventId)){const value=JSON.parse(item.result_json as string);if(item.source_event_id!==eventId&&value.args?.operation==='relation'&&value.args.proposal?.action_id===action.id)throw new Error('Relationship review already consumed');}
    return ownerTaskToolArgsSchema.parse({operation:'relation',review:saved.preview.review,proposal:{event_id:prior.id,action_id:action.id,args_hash:action.payload_hash,accepted_dispatch_id:reply.dispatch_id,source_revision:sourceRevision}});
  }

  selectOwnerIdentifierProposal(authority:ExplicitOwnerLoopAuthority,eventId:string,sourceRevision:string):OwnerTaskToolArgs {
    const current=this.ownerReminderToolAdmission(authority,eventId,true);
    if(!clearRoutineYes(current.text))throw new Error('A clear subsequent yes is required');
    const prior=this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1',eventId,eventId);
    if(!prior||prior.outcome!=='resolved')throw new Error('Accepted identifier review unavailable');
    const action=this.listOwnerTaskToolActionsForThread(eventId).find(a=>a.source_event_id===prior.id);if(!action)throw new Error('Accepted identifier review unavailable');
    const metadata=JSON.parse(action.result_json as string);if(metadata.next_event_id!==eventId||metadata.next_event_accepted!==true)throw new Error('Identifier review is not the current accepted question');
    const saved=this.readOwnerTaskToolAction(action.id as string,authority);const reply=this.getReplyForEvent(prior.id as string);
    if(!saved.preview||saved.preview.review.operation!=='identifier'||saved.sourceRevision!==sourceRevision||!reply||reply.dispatch_status!=='accepted'||reply.source_revision!==sourceRevision||reply.payload!==taskReviewText(saved.preview.review))throw new Error('Accepted identifier review unavailable');
    this.validateConfirmationReplyTarget(eventId,reply);
    for(const item of this.listOwnerTaskToolActionsForThread(eventId)){const value=JSON.parse(item.result_json as string);if(item.source_event_id!==eventId&&value.args?.operation==='identifier'&&value.args.proposal?.action_id===action.id)throw new Error('Identifier review already consumed');}
    return ownerTaskToolArgsSchema.parse({operation:'identifier',review:saved.preview.review,proposal:{event_id:prior.id,action_id:action.id,args_hash:action.payload_hash,accepted_dispatch_id:reply.dispatch_id,source_revision:sourceRevision}});
  }

  selectOwnerFactProposal(authority:ExplicitOwnerLoopAuthority,eventId:string,sourceRevision:string):OwnerTaskToolArgs {
    const current=this.ownerReminderToolAdmission(authority,eventId,true);
    if(!clearRoutineYes(current.text))throw new Error('A clear subsequent yes is required');
    const prior=this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1',eventId,eventId);
    if(!prior||prior.outcome!=='resolved')throw new Error('Accepted fact review unavailable');
    const action=this.listOwnerTaskToolActionsForThread(eventId).find(a=>a.source_event_id===prior.id);if(!action)throw new Error('Accepted fact review unavailable');
    const metadata=JSON.parse(action.result_json as string);if(metadata.next_event_id!==eventId||metadata.next_event_accepted!==true)throw new Error('Fact review is not the current accepted question');
    const saved=this.readOwnerTaskToolAction(action.id as string,authority);const reply=this.getReplyForEvent(prior.id as string);
    if(!saved.preview||saved.preview.review.operation!=='fact_add'||saved.sourceRevision!==sourceRevision||!reply||reply.dispatch_status!=='accepted'||reply.source_revision!==sourceRevision||reply.payload!==taskReviewText(saved.preview.review))throw new Error('Accepted fact review unavailable');
    this.validateConfirmationReplyTarget(eventId,reply);
    for(const item of this.listOwnerTaskToolActionsForThread(eventId)){const value=JSON.parse(item.result_json as string);if(item.source_event_id!==eventId&&value.args?.operation==='fact_add'&&value.args.proposal?.action_id===action.id)throw new Error('Fact review already consumed');}
    return ownerTaskToolArgsSchema.parse({operation:'fact_add',review:saved.preview.review,proposal:{event_id:prior.id,action_id:action.id,args_hash:action.payload_hash,accepted_dispatch_id:reply.dispatch_id,source_revision:sourceRevision}});
  }

  listOwnerTaskToolActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerTaskToolKind);
  }

  readOwnerTaskToolAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerTaskToolRequest; status: string; receipt: OwnerTaskToolReceipt | null;
    rejection: OwnerTaskToolRejection | null; preview:TaskPreview|null; sourceRevision?:string;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerTaskToolKind || typeof action.source_event_id !== 'string')
      throw new Error('Owner task action is invalid');
    let metadata: Record<string, unknown>;
    try {metadata = JSON.parse(action.result_json as string);} catch {throw new Error('Owner task action is invalid');}
    const args = ownerTaskToolArgsSchema.parse(metadata.args);
    const admission = this.ownerReminderToolAdmission({...authority, runId: action.run_id as string}, action.source_event_id, false, false,['relation','propose_relation','identifier','propose_identifier','fact_add','propose_fact','cleanup','propose_cleanup','resolve_many'].includes(args.operation));
    if(args.operation==='cleanup'){
      if(!cleanupRequestMatches(args,admission.text,true)||cleanupListHash(args.list)!==args.list.review_hash||args.proposal.source_revision!==args.list.source_revision||args.proposal.action_id===actionId)throw new Error('Cleanup list binding invalid');
      const parentAction=this.getAction(args.proposal.action_id);const metadata=parentAction?JSON.parse(parentAction.result_json as string):{};if(metadata.args?.operation!=='propose_cleanup')throw new Error('Cleanup proposal unavailable');const parent=this.readOwnerTaskToolAction(args.proposal.action_id,authority);const reply=this.getReplyForEvent(args.proposal.event_id);
      if(!parent.preview||JSON.stringify(parent.preview.review)!==JSON.stringify(args.list)||parent.request.eventId!==args.proposal.event_id||parentAction?.payload_hash!==args.proposal.args_hash||!parentAction||!ownerCleanupProposalCurrent(this,parentAction,admission.eventId)||!reply||reply.dispatch_id!==args.proposal.accepted_dispatch_id||reply.dispatch_status!=='accepted'||reply.payload!==taskReviewText(args.list)||reply.source_revision!==args.list.source_revision)throw new Error('Cleanup proposal binding invalid');
      this.validateConfirmationReplyTarget(admission.eventId,reply);const priorEvent=JSON.parse(this.getInbound(args.proposal.event_id)!.payload as string);const currentEvent=JSON.parse(this.getInbound(admission.eventId)!.payload as string);if(priorEvent.context.destination!==currentEvent.context.destination||this.getDispatch(reply.dispatch_id as string)?.destination!==priorEvent.context.destination)throw new Error('Cleanup route changed');
    }
    if(args.operation==='resolve_many'&&!resolveManyOwnerTextMatches(args.items,admission.text,args.selection_mode))throw new Error('Exact multi-loop closure unavailable');
    if(args.operation==='relation'){
      if(!clearRoutineYes(admission.text)||!validRelationReview(args.review)||args.proposal.source_revision!==args.review.source_revision||args.proposal.action_id===actionId)throw new Error('Relationship review is invalid');
      const parentAction=this.getAction(args.proposal.action_id);const parentMetadata=parentAction?JSON.parse(parentAction.result_json as string):{};if(parentMetadata.args?.operation!=='propose_relation')throw new Error('Relationship proposal proof is invalid');const parent=this.readOwnerTaskToolAction(args.proposal.action_id,authority);const reply=this.getReplyForEvent(args.proposal.event_id);
      if(!parent.preview||JSON.stringify(parent.preview.review)!==JSON.stringify(args.review)||parent.request.eventId!==args.proposal.event_id||parentAction?.payload_hash!==args.proposal.args_hash||parentMetadata.next_event_id!==admission.eventId||parentMetadata.next_event_accepted!==true||!reply||reply.dispatch_id!==args.proposal.accepted_dispatch_id||reply.dispatch_status!=='accepted'||reply.payload!==taskReviewText(args.review)||reply.source_revision!==args.review.source_revision)throw new Error('Relationship proposal proof is invalid');
      this.validateConfirmationReplyTarget(admission.eventId,reply);
    }
    if(args.operation==='identifier'){
      if(!clearRoutineYes(admission.text)||!taskPreviewMatches({version:1,status:'preview',event_id:args.proposal.event_id,payload_hash:admission.payloadHash,review:args.review},{operation:'propose_identifier',canonical_id:args.review.canonical_id,identifier:args.review.identifier,source_revision:args.review.source_revision})||args.proposal.source_revision!==args.review.source_revision||args.proposal.action_id===actionId)throw new Error('Identifier review is invalid');
      const parentAction=this.getAction(args.proposal.action_id);const parentMetadata=parentAction?JSON.parse(parentAction.result_json as string):{};if(parentMetadata.args?.operation!=='propose_identifier')throw new Error('Identifier proposal proof is invalid');const parent=this.readOwnerTaskToolAction(args.proposal.action_id,authority);const reply=this.getReplyForEvent(args.proposal.event_id);
      if(!parent.preview||JSON.stringify(parent.preview.review)!==JSON.stringify(args.review)||parent.request.eventId!==args.proposal.event_id||parentAction?.payload_hash!==args.proposal.args_hash||parentMetadata.next_event_id!==admission.eventId||parentMetadata.next_event_accepted!==true||!reply||reply.dispatch_id!==args.proposal.accepted_dispatch_id||reply.dispatch_status!=='accepted'||reply.payload!==taskReviewText(args.review)||reply.source_revision!==args.review.source_revision)throw new Error('Identifier proposal proof is invalid');
      this.validateConfirmationReplyTarget(admission.eventId,reply);
    }
    if(args.operation==='fact_add'){
      if(!clearRoutineYes(admission.text)||!taskPreviewMatches({version:1,status:'preview',event_id:args.proposal.event_id,payload_hash:admission.payloadHash,review:args.review},{operation:'propose_fact',canonical_id:args.review.canonical_id,text:args.review.text,source_revision:args.review.source_revision})||args.proposal.source_revision!==args.review.source_revision||args.proposal.action_id===actionId)throw new Error('Fact review is invalid');
      const parentAction=this.getAction(args.proposal.action_id);const parentMetadata=parentAction?JSON.parse(parentAction.result_json as string):{};if(parentMetadata.args?.operation!=='propose_fact')throw new Error('Fact proposal proof is invalid');const parent=this.readOwnerTaskToolAction(args.proposal.action_id,authority);const reply=this.getReplyForEvent(args.proposal.event_id);
      if(!parent.preview||JSON.stringify(parent.preview.review)!==JSON.stringify(args.review)||parent.request.eventId!==args.proposal.event_id||parentAction?.payload_hash!==args.proposal.args_hash||parentMetadata.next_event_id!==admission.eventId||parentMetadata.next_event_accepted!==true||!reply||reply.dispatch_id!==args.proposal.accepted_dispatch_id||reply.dispatch_status!=='accepted'||reply.payload!==taskReviewText(args.review)||reply.source_revision!==args.review.source_revision)throw new Error('Fact proposal proof is invalid');
      this.validateConfirmationReplyTarget(admission.eventId,reply);
    }
    if((args.operation==='add_identifier'||args.operation==='propose_identifier')&&!admission.text.includes(args.identifier))throw new Error('Identifier is not an exact owner span');
    if(args.operation==='add')for(const value of [args.text,args.contact,args.identifier])if(value!==undefined&&!admission.text.includes(value))throw new Error('Task detail is not an exact owner span');
    if(args.operation==='fact_correct')for(const value of [args.title,args.company])if(value!==undefined&&!admission.text.includes(value))throw new Error('Identity detail is not an exact owner span');
    if ((args.operation === 'fact_correct' || args.operation === 'propose_fact') && !admission.text.includes(args.text)) throw new Error('Fact text is not an exact owner span');
    if ((args.operation === 'correct' || args.operation === 'retarget' || args.operation === 'snooze' || args.operation === 'keep') && !admission.text.includes(args.correction)) throw new Error('Correction is not an exact owner span');
    const binding = stableActionIdentity(admission.eventId, ownerTaskToolKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
        action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Owner task action is invalid');
    const preview=action.status==='succeeded'&&['propose_relation','propose_identifier','propose_fact','propose_cleanup'].includes(args.operation)?taskPreviewSchema.parse(metadata.preview):null;
    if(preview&&(preview.event_id!==admission.eventId||preview.payload_hash!==admission.payloadHash||!taskPreviewMatches(preview,args)))throw new Error('Task preview is invalid');
    const receipt=action.status==='succeeded'&&!['propose_relation','propose_identifier','propose_fact','propose_cleanup'].includes(args.operation)?ownerTaskToolReceiptSchema.parse(metadata.receipt):null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
        receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
         !taskReceiptMatches(receipt, args)))
      throw new Error('Owner task receipt is invalid');
    const rejection = action.status === 'rejected' ? ownerTaskToolRejectionSchema.parse(metadata.rejection) : null;
    if (rejection !== null && !ownerTaskToolRejectionSchema.safeParse(rejection).success)
      throw new Error('Owner task rejection is invalid');
    return {request: {...admission, actionId, args}, status: action.status as string, receipt, rejection,preview,sourceRevision:preview?.review.source_revision};
  }

  settleOwnerTaskToolAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>,
                               result: OwnerTaskToolReceipt | OwnerTaskToolRejection | TaskPreview, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerTaskToolAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing')) throw new Error('Owner task action is not eligible');
      const metadata = {args: saved.request.args, scope_hash: authority.scopeHash};
      if (typeof result === 'string') {
        if (reconciled || !ownerTaskToolRejectionSchema.safeParse(result).success) throw new Error('Owner task rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, rejection: result}), now(), actionId);
      } else if(result.status==='preview') {
        const preview=taskPreviewSchema.parse(result);const args=saved.request.args;
        if(preview.event_id!==saved.request.eventId||preview.payload_hash!==saved.request.payloadHash||!taskPreviewMatches(preview,args))throw new Error('Task preview is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?").run(JSON.stringify({...metadata,preview}),now(),actionId);
      } else {
        const receipt = ownerTaskToolReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerTaskToolKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
            receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
            !taskReceiptMatches(receipt, saved.request.args))
          throw new Error('Owner task receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, receipt}), now(), actionId);
      }
    });
  }

  prepareOwnerPreferenceToolAction(authority: ExplicitOwnerLoopAuthority, eventId: string,
                                input: OwnerPreferenceToolArgs): OwnerPreferenceToolRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerPreferenceToolKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = ownerPreferenceToolArgsSchema.parse(input);
      if (args.operation === 'accept-procedure-offer' && JSON.stringify(args.selection) !== JSON.stringify(this.ownerProcedureOfferSelection(eventId)))
        throw new Error('Standing-rule selection is not the admitted owner reply');
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerTaskToolKind))
        throw new Error('Owner message already has another private mutation');
      if((args.operation==='snooze-nudges'||args.operation==='unsnooze-nudges')&&JSON.stringify(ownerQuietCommand(admitted.text))!==JSON.stringify(args))throw new Error('Quiet command is not the exact owner request');
      if(args.operation==='prioritize'&&!priorityCtaMatches(admitted.text,args.name))throw new Error('Priority command is not the exact owner request');
      if(args.operation==='propose-context'&&!admitted.text.includes(args.text))throw new Error('Context is not an exact owner span');
      if(args.operation==='propose-priorities'&&!admitted.text.includes(args.text))throw new Error('Priorities are not an exact owner span');
      if(args.operation==='propose-procedure'&&!admitted.text.includes(args.rule))throw new Error('Standing rule is not an exact owner span');
      if((args.operation==='append-procedure'||args.operation==='replace-priorities'||args.operation==='append-context')&&stableActionIdentity(eventId,ownerPreferenceToolKind,args).actionId!==stableActionIdentity(eventId,ownerPreferenceToolKind,this.selectOwnerProcedureProposal(authority,eventId)).actionId)throw new Error('Standing-rule review binding unavailable');
      if (args.operation === 'add' && !admitted.text.includes(args.value)) throw new Error('Preference value is not an exact owner span');
      const binding = stableActionIdentity(eventId, ownerPreferenceToolKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerPreferenceToolKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Owner preference event conflicts with saved arguments');
      if (previous) {
        this.readOwnerPreferenceToolAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      } else {
        if (args.operation === 'add' && args.field === 'tone_notes' && oneOffToneRequest(admitted.text))
          throw new Error('This is a one-off rewrite request. Revise the prior wording in chat; do not save a preference.');
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerPreferenceToolKind, binding.payloadHash, eventId,
            admitted.payloadHash, hash(admitted.text), JSON.stringify({args, scope_hash: authority.scopeHash}), stamp, stamp);
      }
      return {...admitted, actionId: binding.actionId, args};
    });
  }

  listOwnerPreferenceToolActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerPreferenceToolKind);
  }

  readOwnerPreferenceToolAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerPreferenceToolRequest; status: string; receipt: OwnerPreferenceToolReceipt | null;
    rejection: OwnerPreferenceToolRejection | null;preview:ProcedurePreview|null;capacity:ProcedureCapacity|null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerPreferenceToolKind || typeof action.source_event_id !== 'string')
      throw new Error('Owner preference action is invalid');
    const admission = this.ownerReminderToolAdmission({...authority, runId: action.run_id as string}, action.source_event_id, false, false, true);
    let metadata: Record<string, unknown>;
    try {metadata = JSON.parse(action.result_json as string);} catch {throw new Error('Owner preference action is invalid');}
    const args = ownerPreferenceToolArgsSchema.parse(metadata.args);
    if (args.operation === 'accept-procedure-offer' && JSON.stringify(args.selection) !== JSON.stringify(this.ownerProcedureOfferSelection(admission.eventId)))
      throw new Error('Standing-rule selection is not the saved owner reply');
    const binding = stableActionIdentity(admission.eventId, ownerPreferenceToolKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
        action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Owner preference action is invalid');
    if((args.operation==='snooze-nudges'||args.operation==='unsnooze-nudges')&&JSON.stringify(ownerQuietCommand(admission.text))!==JSON.stringify(args))throw new Error('Quiet command is not the saved owner request');
    if(args.operation==='prioritize'&&!priorityCtaMatches(admission.text,args.name))throw new Error('Priority command is not the saved owner request');
    if(args.operation==='propose-context'&&!admission.text.includes(args.text))throw new Error('Context is not the saved owner span');
    if(args.operation==='propose-priorities'&&!admission.text.includes(args.text))throw new Error('Priorities are not the saved owner span');
    if(args.operation==='propose-procedure'&&!admission.text.includes(args.rule))throw new Error('Standing rule is not the saved owner span');
    const preview=action.status==='succeeded'&&(args.operation==='propose-procedure'||args.operation==='propose-priorities'||args.operation==='propose-context')?procedurePreviewSchema.parse(metadata.preview):null;
    if(preview&&(preview.event_id!==admission.eventId||preview.payload_hash!==admission.payloadHash||!standingPreviewMatches(preview,args)))throw new Error('Standing-rule preview binding invalid');
    const receipt = action.status === 'succeeded'&&args.operation!=='propose-procedure'&&args.operation!=='propose-priorities'&&args.operation!=='propose-context' ? ownerPreferenceToolReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
        receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
         !preferenceReceiptMatches(receipt, args)))
      throw new Error('Owner preference receipt is invalid');
    const rejection = action.status === 'rejected' ? ownerPreferenceToolRejectionSchema.parse(metadata.rejection) : null;
    if (rejection !== null && !ownerPreferenceToolRejectionSchema.safeParse(rejection).success)
      throw new Error('Owner preference rejection is invalid');
    return {request: {...admission, actionId, args}, status: action.status as string, receipt, rejection,preview,capacity:metadata.capacity===undefined?null:procedureCapacitySchema.parse(metadata.capacity)};
  }

  settleOwnerPreferenceToolAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>,
                               result: OwnerPreferenceToolReceipt | OwnerPreferenceToolRejection | ProcedurePreview | {code:'master_capacity';capacity:ProcedureCapacity}, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerPreferenceToolAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing')) throw new Error('Owner preference action is not eligible');
      const metadata = {args: saved.request.args, scope_hash: authority.scopeHash};
      if (typeof result === 'string'||'code' in result) {
        const code=typeof result==='string'?result:result.code;
        if (reconciled || !ownerPreferenceToolRejectionSchema.safeParse(code).success) throw new Error('Owner preference rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, rejection: code,...(typeof result==='string'?{}:{capacity:procedureCapacitySchema.parse(result.capacity)})}), now(), actionId);
      } else if(result.status==='preview'){
        const preview=procedurePreviewSchema.parse(result);
        if(reconciled||!saved.request.args||preview.event_id!==saved.request.eventId||preview.payload_hash!==saved.request.payloadHash||!standingPreviewMatches(preview,saved.request.args))throw new Error('Standing-rule preview invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?").run(JSON.stringify({...metadata,preview}),now(),actionId);
      } else {
        const receipt = ownerPreferenceToolReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerPreferenceToolKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
            receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
            !saved.request.args || !preferenceReceiptMatches(receipt, saved.request.args))
          throw new Error('Owner preference receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, receipt}), now(), actionId);
      }
    });
  }

  private conversationExclusionCheck(providerThread: string): (eventId: string) => boolean {
    const present = Boolean(this.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_memory_excluded'"));
    return eventId => present && Boolean(this.one(
      'SELECT 1 FROM conversation_memory_excluded WHERE provider_thread=? AND event_id=?', providerThread, eventId));
  }

  /** Read targets may come from accepted context in this session, never from a guessed history. */
  private acceptedReadContexts(eventId: string, sourceRevision: unknown): Array<{eventId: string; admittedAt: number; dispatchId: string; text: string}> {
    const current = this.getInbound(eventId);
    if (!current || typeof sourceRevision !== 'string' || !/^[a-f0-9]{64}$/.test(sourceRevision)) return [];
    const payload = JSON.parse(String(current.payload));
    const excluded = this.conversationExclusionCheck(payload.context.threadId);
    const rows = this.db.prepare(`SELECT * FROM inbound_events WHERE thread_key=?
      AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 32`)
      .all(current.thread_key as string, eventId) as Row[];
    const result: Array<{eventId: string; admittedAt: number; dispatchId: string; text: string}> = [];
    // Background deliveries remain in native memory, but prose is not read authority.
    // Accepted meeting-prep offers use the separate receipt-bound offer path.
    for (const event of rows) {
      if (!event.payload) continue;
      const prior = JSON.parse(String(event.payload));
      if (prior.inbound.text?.trim().toLowerCase() === '/new' || readCancelled(prior.inbound.text ?? '')) break;
      if (Number(current.received_at)-Number(event.received_at)>READ_CONTEXT_WINDOW_MS) break;
      if (event.outcome !== 'resolved' || Number(event.received_at) > Number(current.received_at) ||
          event.channel !== current.channel || event.account_key !== current.account_key || event.sender !== current.sender) continue;
      if (excluded(event.id as string)) continue;
      if (JSON.stringify(prior.context) !== JSON.stringify(payload.context)) continue;
      const reply = this.getReplyForEvent(event.id as string);
      if (!reply || reply.dispatch_status !== 'accepted') continue;
      const includeReply = reply.source_revision === sourceRevision;
      if (!includeReply) {
        // A queued acknowledgement is owner-only text. Its saved research action can
        // still prove the original owner subject, without upgrading the ACK's prose.
        const action = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', event.id as string, ownerSkillRequestKind);
        if (reply.source_revision !== OWNER_ONLY_CONTEXT_PROOF || action?.status !== 'succeeded') continue;
        const metadata = JSON.parse(String(action.result_json));
        if (metadata.args?.source_revision !== sourceRevision) continue;
      }
      const dispatch = this.getDispatch(reply.dispatch_id as string);
      if (!dispatch || dispatch.destination !== payload.context.destination || Number(dispatch.updated_at) > Number(current.received_at)) continue;
      try { this.validateConfirmationReplyTarget(eventId, reply); } catch { continue; }
      // Whitespace normalization only: no inferred subjects or reconstructed source facts.
      const text = String(prior.inbound.text ?? '').replace(/\s+/gu, ' ').trim();
      result.push({eventId:String(event.id),admittedAt:Number(event.received_at),dispatchId:String(reply.dispatch_id),text});
    }
    return result;
  }

  resolveOwnerResearchSelection(authority: ExplicitOwnerLoopAuthority, eventId: string,
                                input: OwnerSkillRequestArgs, sourceRevision: unknown): OwnerSkillRequestArgs {
    return resolveResearchSelection(this,authority,eventId,input,sourceRevision,(id,revision)=>this.acceptedReadContexts(id,revision));
  }

  prepareOwnerSkillRequestAction(authority: ExplicitOwnerLoopAuthority, eventId: string,
                                input: OwnerSkillRequestArgs): OwnerSkillRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerSkillRequestKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      let args = ownerSkillRequestArgsSchema.parse(input);
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerTaskToolKind))
        throw new Error('Owner message already has another private mutation');
      const selector = args.skill === 'ask-research' ? (args.selection.mode === 'company' ? args.selection.company : args.selection.person)
        : args.selection.mode === 'focus' ? args.selection.focus : null;
      if (args.skill !== 'meeting-prep' && selector !== null && !ownerSubjectMention(admitted.text,selector) && !(args.skill === 'ask-research' && (args.prior_selection || args.prior_request || args.prior_prep))) throw new Error('Skill selector is not an exact owner span');
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerSkillRequestKind);
      if (args.skill==='ask-research' && (!previous || previous.status==='pending') &&
          (!validResearchSubject(selector!,args.selection.mode) ||
           !(args.prior_request ? researchTargetAnswer(admitted.text,selector!,args.selection.mode) :
             (args.prior_selection || args.prior_prep) ? contextualReadRequest(admitted.text) : ownerReadRequest(admitted.text)) ||
           !args.prior_selection && !args.prior_request && !args.prior_prep && !ownerSubjectMention(admitted.text,selector!)))
        throw new Error('Research requires an affirmative owner request and exact owner span');
      // Meeting prep is a source read: the model resolves its subject from conversation.
      // Keep the exact admitted selection immutable; the shared pipeline must bind it to
      // one actual upcoming attendee/occurrence before it researches or delivers prep.
      const binding = stableActionIdentity(eventId, ownerSkillRequestKind, args);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Owner skill request event conflicts with saved arguments');
      if (args.skill === 'ask-research' && (args.prior_selection || args.prior_request || args.prior_prep) && (!previous || previous.status === 'pending') &&
          JSON.stringify(args) !== JSON.stringify(this.resolveOwnerResearchSelection(authority, eventId, {...args, prior_selection: undefined, prior_request: undefined, prior_prep:undefined}, args.source_revision)))
        throw new Error('Prior company selection unavailable');
      if (args.skill === 'meeting-prep' && (!previous || previous.status === 'pending') &&
          !isOwnerMeetingPrepRequest(admitted.text, args.selection.mode))
        throw new Error('Meeting prep requires an explicit owner meeting prep request');
      if (args.skill === 'followup' && (!previous || previous.status === 'pending') && !isOwnerFollowupRequest(admitted.text))
        throw new FollowupAdmissionRequiredError();
      if (args.skill === 'relationship-pulse' && (!previous || previous.status === 'pending') && !isOwnerRelationshipPulseRequest(admitted.text))
        throw new RelationshipPulseAdmissionRequiredError();
      if (args.skill === 'proactive' && (!previous || previous.status === 'pending') && !isOwnerUrgentCheck(admitted.text))
        throw new ProactiveAdmissionRequiredError();
      if (previous) {
        this.readOwnerSkillRequestAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      } else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerSkillRequestKind, binding.payloadHash, eventId,
            admitted.payloadHash, hash(admitted.text), JSON.stringify({args, scope_hash: authority.scopeHash}), stamp, stamp);
      }
      return {...admitted, actionId: binding.actionId, args};
    });
  }

  listOwnerSkillRequestActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerSkillRequestKind);
  }

  readOwnerSkillRequestAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerSkillRequest; status: string; receipt: OwnerSkillRequestReceipt | null;
    rejection: OwnerSkillRequestRejection | null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerSkillRequestKind || typeof action.source_event_id !== 'string')
      throw new Error('Owner skill request action is invalid');
    const admission = this.ownerReminderToolAdmission({...authority, runId: action.run_id as string}, action.source_event_id, false, false);
    let metadata: Record<string, unknown>;
    try {metadata = JSON.parse(action.result_json as string);} catch {throw new Error('Owner skill request action is invalid');}
    const args = ownerSkillRequestArgsSchema.parse(metadata.args);
    if (args.skill === 'ask-research' && args.selection.mode === 'company' && !subjectMention(admission.text, args.selection.company) &&
        !((args.prior_selection || args.prior_request || args.prior_prep) && args.source_revision))
      throw new Error('Skill selector is not an exact owner span');
    const binding = stableActionIdentity(admission.eventId, ownerSkillRequestKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
        action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Owner skill request action is invalid');
    const receipt = action.status === 'succeeded' ? ownerSkillRequestReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
        receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
         receipt.outcome.skill !== args.skill))
      throw new Error('Owner skill request receipt is invalid');
    const rejection = action.status === 'rejected' ? ownerSkillRequestRejectionSchema.parse(metadata.rejection) : null;
    if (rejection !== null && !ownerSkillRequestRejectionSchema.safeParse(rejection).success)
      throw new Error('Owner skill request rejection is invalid');
    return {request: {...admission, actionId, args}, status: action.status as string, receipt, rejection};
  }

  settleOwnerSkillRequestAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>,
                               result: OwnerSkillRequestReceipt | OwnerSkillRequestRejection, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerSkillRequestAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing')) throw new Error('Owner skill request action is not eligible');
      const metadata = {args: saved.request.args, scope_hash: authority.scopeHash};
      if (typeof result === 'string') {
        if (reconciled || !ownerSkillRequestRejectionSchema.safeParse(result).success) throw new Error('Owner skill request rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, rejection: result}), now(), actionId);
      } else {
        const receipt = ownerSkillRequestReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerSkillRequestKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
            receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
            !saved.request.args || receipt.outcome.skill !== saved.request.args.skill)
          throw new Error('Owner skill request receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, receipt}), now(), actionId);
      }
    });
  }

  ownerWebReadReplyTarget(eventId:string,url:string,sourceRevision:unknown):WebReadReplyTarget {
    const event=this.getInbound(eventId);
    if(!event||typeof event.payload!=='string')throw new Error('Accepted reply URL unavailable');
    const saved=JSON.parse(event.payload),inbound=saved.inbound;
    if(!inbound||readCancelled(inbound.text??'')||ownerUrlTokens(inbound.text??'').length)throw new Error('Accepted reply URL unavailable');
    if (inbound.replyBound || inbound.replyToMessageId !== undefined) {
      const quoted=this.quotedReplyForEvent(eventId,inbound.replyToMessageId);
      if(quoted.status==='found' && Number(event.received_at)-Number(this.getDispatch(quoted.acceptedDispatchId)?.updated_at)>READ_CONTEXT_WINDOW_MS)throw new Error('Accepted reply URL expired');
      if(!ownerReadRequest(inbound.text)||quoted.status!=='found'||quoted.sourceRevision!==sourceRevision||!webReadAdmissionMatches(inbound.text,{url,source_revision:quoted.sourceRevision,reply_target:{accepted_dispatch_id:quoted.acceptedDispatchId,provider_message_id:quoted.messageId,text:quoted.text,source_revision:quoted.sourceRevision}}))throw new Error('Accepted reply URL unavailable');
      return webReadReplyTargetSchema.parse({accepted_dispatch_id:quoted.acceptedDispatchId,provider_message_id:quoted.messageId,text:quoted.text,source_revision:quoted.sourceRevision});
    }
    for (const context of this.acceptedReadContexts(eventId, sourceRevision)) {
      if (!contextualReadRequest(inbound.text) || !ownerUrlTokens(context.text).includes(url) ||
          new Set(ownerUrlTokens(context.text)).size!==1) continue;
      if (!explicitReadReference(inbound.text)) {
        const immediate=this.one('SELECT id FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1',eventId,eventId);
        if (immediate?.id!==context.eventId) continue;
      }
      const dispatch = this.getDispatch(context.dispatchId);
      if (typeof dispatch?.provider_message_id !== 'string') continue;
      return webReadReplyTargetSchema.parse({accepted_dispatch_id: context.dispatchId,
        provider_message_id: dispatch.provider_message_id, text: context.text, source_revision: sourceRevision});
    }
    throw new Error('Accepted reply URL unavailable');
  }

  prepareOwnerWebReadAction(authority: ExplicitOwnerLoopAuthority, eventId: string,
                                input: OwnerWebReadArgs): OwnerWebReadRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerWebReadKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = ownerWebReadArgsSchema.parse(input);
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerTaskToolKind))
        throw new Error('Owner message already has another private mutation');
      if(!('operation' in args)&&args.reply_target&&JSON.stringify(args.reply_target)!==JSON.stringify(this.ownerWebReadReplyTarget(eventId,args.url,args.source_revision)))throw new Error('Accepted reply URL binding unavailable');
      if (!webReadAdmissionMatches(admitted.text,args)) throw new Error('URL is not a complete current owner token');
      if('operation' in args&&args.operation==='confirm_docsend'&&stableActionIdentity(eventId,ownerWebReadKind,args).actionId!==stableActionIdentity(eventId,ownerWebReadKind,this.selectOwnerDocsendProposal(authority,eventId,args.review.source_revision)).actionId)throw new Error('DocSend confirmation unavailable');
      const binding = stableActionIdentity(eventId, ownerWebReadKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerWebReadKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Owner URL read event conflicts with saved arguments');
      if (previous) {
        this.readOwnerWebReadAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      } else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerWebReadKind, binding.payloadHash, eventId,
            admitted.payloadHash, hash(admitted.text), JSON.stringify({args, scope_hash: authority.scopeHash}), stamp, stamp);
      }
      return {...admitted, actionId: binding.actionId, args};
    });
  }

  listOwnerWebReadActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerWebReadKind);
  }

  readOwnerWebReadAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerWebReadRequest; status: string; receipt: OwnerWebReadReceipt | null;
    rejection: OwnerWebReadRejection | null; preview:DocSendPreview|null; sourceRevision:string|null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerWebReadKind || typeof action.source_event_id !== 'string')
      throw new Error('Owner URL read action is invalid');
    const admission = this.ownerReminderToolAdmission({...authority, runId: action.run_id as string}, action.source_event_id, false, false, true);
    let metadata: Record<string, unknown>;
    try {metadata = JSON.parse(action.result_json as string);} catch {throw new Error('Owner URL read action is invalid');}
    const args = ownerWebReadArgsSchema.parse(metadata.args);
    const binding = stableActionIdentity(admission.eventId, ownerWebReadKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
        action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Owner URL read action is invalid');
    if(!('operation' in args)&&args.reply_target&&JSON.stringify(args.reply_target)!==JSON.stringify(this.ownerWebReadReplyTarget(admission.eventId,args.url,args.source_revision)))throw new Error('Accepted reply URL binding unavailable');
    const preview=action.status==='succeeded'&&metadata.preview?docsendPreviewSchema.parse(metadata.preview):null;
    if(!webReadAdmissionMatches(admission.text,args)||preview&&!docsendPreviewMatches(preview,args,admission.eventId,admission.payloadHash))throw new Error('DocSend saved review invalid');
    if('operation' in args&&args.operation==='confirm_docsend') {
      const prior=this.getAction(args.proposal.action_id);
      if(!prior||prior.kind!==ownerWebReadKind||prior.source_event_id!==args.proposal.event_id||prior.payload_hash!==args.proposal.args_hash||prior.id===actionId)throw new Error('DocSend proposal proof invalid');
      const priorMetadata=JSON.parse(prior.result_json as string),priorPreview=docsendPreviewSchema.parse(priorMetadata.preview);
      const priorArgs=ownerWebReadArgsSchema.parse(priorMetadata.args);
      if(!('operation' in priorArgs)||priorArgs.operation!=='propose_docsend')throw new Error('DocSend proposal kind invalid');
      this.readOwnerWebReadAction(prior.id as string,authority);
      const priorEvent=this.getInbound(args.proposal.event_id),currentEvent=this.getInbound(admission.eventId);
      const reply=this.getReplyForEvent(args.proposal.event_id);
      if(prior.status!=='succeeded'||!priorEvent||!currentEvent||priorEvent.thread_key!==currentEvent.thread_key||priorMetadata.next_event_id!==admission.eventId||priorMetadata.next_event_accepted!==true||
        !docsendPreviewMatches(priorPreview,priorArgs,args.proposal.event_id,prior.source_payload_hash as string)||
        stableActionIdentity(args.proposal.event_id,ownerWebReadKind,priorArgs).actionId!==prior.id||
        JSON.stringify(priorPreview.review)!==JSON.stringify(args.review)||args.proposal.source_revision!==args.review.source_revision||
        !reply||reply.dispatch_status!=='accepted'||reply.dispatch_id!==args.proposal.accepted_dispatch_id||reply.source_revision!==args.review.source_revision||reply.payload!==docsendReviewText(priorPreview))throw new Error('DocSend accepted proposal unavailable');
      this.validateConfirmationReplyTarget(admission.eventId,reply);
      if(!clearRoutineYes(admission.text))throw new Error('DocSend requires a distinct yes');
    }
    const receipt = action.status === 'succeeded'&&!preview ? ownerWebReadReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
        receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
         !webReadReceiptMatches(receipt,args)))
      throw new Error('Owner URL read receipt is invalid');
    const rejection = action.status === 'rejected' ? ownerWebReadRejectionSchema.parse(metadata.rejection) : null;
    if (rejection !== null && !ownerWebReadRejectionSchema.safeParse(rejection).success)
      throw new Error('Owner URL read rejection is invalid');
    return {request: {...admission, actionId, args}, status: action.status as string, receipt, rejection, preview, sourceRevision:webReadArgsRevision(args)};
  }

  settleOwnerWebReadAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>,
                               result: OwnerWebReadReceipt | OwnerWebReadRejection | DocSendPreview, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerWebReadAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing')) throw new Error('Owner URL read action is not eligible');
      const metadata = {args: saved.request.args, scope_hash: authority.scopeHash};
      if (typeof result === 'string') {
        if (reconciled || !ownerWebReadRejectionSchema.safeParse(result).success) throw new Error('Owner URL read rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, rejection: result}), now(), actionId);
      } else if(result.status==='preview') {
        const preview=docsendPreviewSchema.parse(result);
        if(reconciled||!docsendPreviewMatches(preview,saved.request.args,saved.request.eventId,saved.request.payloadHash))throw new Error('DocSend preview invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?").run(JSON.stringify({...metadata,preview}),now(),actionId);
      } else {
        const receipt = ownerWebReadReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerWebReadKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
            receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
            !saved.request.args || !webReadReceiptMatches(receipt,saved.request.args))
          throw new Error('Owner URL read receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({...metadata, receipt}), now(), actionId);
      }
    });
  }

  prepareOwnerRoutineToolAction(authority: ExplicitOwnerLoopAuthority, eventId: string, input: OwnerRoutineToolArgs, sourceRevision?: string): OwnerRoutineToolRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerRoutineToolKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = ownerRoutineToolArgsSchema.parse(input);
      if ((args.operation === 'propose' || args.operation === 'propose_adjust') && !sourceRevision)
        throw new Error('Routine proposal source proof unavailable');
      if ((args.operation === 'create' || args.operation === 'adjust') && (!sourceRevision || stableActionIdentity(eventId, ownerRoutineToolKind, args).actionId !== stableActionIdentity(eventId, ownerRoutineToolKind, this.selectOwnerRoutineProposal(authority,eventId,sourceRevision,args.operation==='adjust'?'adjust':'create')).actionId))
        throw new Error('Routine proposal binding unavailable');
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerTaskToolKind))
        throw new Error('Owner message already has another private mutation');
      const binding = stableActionIdentity(eventId, ownerRoutineToolKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerRoutineToolKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Owner routine event conflicts with saved arguments');
      if (previous) {
        this.readOwnerRoutineToolAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      }
      else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerRoutineToolKind, binding.payloadHash, eventId, admitted.payloadHash, hash(admitted.text), JSON.stringify({ args, scope_hash: authority.scopeHash, source_revision: sourceRevision }), stamp, stamp);
      }
      return { ...admitted, actionId: binding.actionId, args };
    });
  }

  listOwnerRoutineToolActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerRoutineToolKind);
  }

  readOwnerRoutineToolAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerRoutineToolRequest;
    status: string;
    receipt: OwnerRoutineToolReceipt | null;
    preview: RoutinePreview | null;
    sourceRevision: string | undefined;
    rejection: OwnerRoutineToolRejection | null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerRoutineToolKind || typeof action.source_event_id !== 'string')
      throw new Error('Owner routine action is invalid');
    const admission = this.ownerReminderToolAdmission({ ...authority, runId: action.run_id as string }, action.source_event_id, false, false, true);
    let metadata: Record<string, unknown>;
    try {
      metadata = JSON.parse(action.result_json as string);
    }
    catch {
      throw new Error('Owner routine action is invalid');
    }
    const args = ownerRoutineToolArgsSchema.parse(metadata.args);
    const binding = stableActionIdentity(admission.eventId, ownerRoutineToolKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
      action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Owner routine action is invalid');
    const preview = action.status === 'succeeded' && (args.operation === 'propose' || args.operation === 'propose_adjust') ? routinePreviewSchema.parse(metadata.preview) : null;
    if (preview && (preview.event_id !== admission.eventId || preview.payload_hash !== admission.payloadHash || !routinePreviewMatches(preview,args)))
      throw new Error('Routine preview binding invalid');
    const receipt = action.status === 'succeeded' && (args.operation !== 'propose' && args.operation !== 'propose_adjust') ? ownerRoutineToolReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
      receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
      !routineReceiptMatches(receipt, args)))
      throw new Error('Owner routine receipt is invalid');
    const rejection = action.status === 'rejected' ? ownerRoutineToolRejectionSchema.parse(metadata.rejection) : null;
    if (rejection !== null && !ownerRoutineToolRejectionSchema.safeParse(rejection).success)
      throw new Error('Owner routine rejection is invalid');
    return { request: { ...admission, actionId, args }, status: action.status as string, receipt, rejection, preview, sourceRevision: typeof metadata.source_revision === 'string' ? metadata.source_revision : undefined };
  }

  settleOwnerRoutineToolAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>, result: OwnerRoutineToolReceipt | OwnerRoutineToolRejection | RoutinePreview, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerRoutineToolAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing'))
        throw new Error('Owner routine action is not eligible');
      const metadata = { args: saved.request.args, scope_hash: authority.scopeHash, source_revision: saved.sourceRevision };
      if (typeof result === 'string') {
        if (reconciled || !ownerRoutineToolRejectionSchema.safeParse(result).success)
          throw new Error('Owner routine rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({ ...metadata, rejection: result }), now(), actionId);
      }
      else if (result.status === 'preview') {
        const preview = routinePreviewSchema.parse(result);
        if (reconciled || !saved.request.args || !routinePreviewMatches(preview,saved.request.args) || preview.event_id !== saved.request.eventId || preview.payload_hash !== saved.request.payloadHash)
          throw new Error('Routine preview invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?").run(JSON.stringify({ ...metadata, preview }), now(), actionId);
      }
      else {
        const receipt = ownerRoutineToolReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerRoutineToolKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
          receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
          !saved.request.args || !routineReceiptMatches(receipt, saved.request.args))
          throw new Error('Owner routine receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({ ...metadata, receipt }), now(), actionId);
      }
    });
  }

  private validateConfirmationReplyTarget(eventId: string, reply: Row): void {
    const event = this.getInbound(eventId)!;
    const inbound = JSON.parse(event.payload as string).inbound as Record<string, unknown>;
    if (!inbound.replyBound && inbound.replyToMessageId === undefined) return;
    const dispatch = this.getDispatch(reply.dispatch_id as string);
    const ids = dispatch?.provider_message_ids ? JSON.parse(dispatch.provider_message_ids as string) : [];
    if (typeof inbound.replyToMessageId !== 'string' || !dispatch || dispatch.status !== 'accepted'
        || ![dispatch.provider_message_id, ...ids].includes(inbound.replyToMessageId))
      throw new Error('Explicit reply does not address the accepted confirmation question');
  }

  selectOwnerRoutineProposal(authority: ExplicitOwnerLoopAuthority, eventId: string, sourceRevision:string,expectedOperation:'create'|'adjust'='create'):OwnerRoutineToolArgs {
    const current = this.ownerReminderToolAdmission(authority, eventId, true);
    if (!clearRoutineYes(current.text))
      throw new Error('A clear subsequent yes is required');
    const prior = this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1', eventId, eventId);
    if (!prior || prior.outcome !== 'resolved')
      throw new Error('Accepted routine proposal unavailable');
    const action = this.listOwnerRoutineToolActionsForThread(eventId).find(a => a.source_event_id === prior.id);
    if (!action)
      throw new Error('Accepted routine proposal unavailable');
    const proposalMetadata = JSON.parse(action.result_json as string);
    if (proposalMetadata.next_event_id !== eventId || proposalMetadata.next_event_accepted !== true)
      throw new Error('Routine proposal is not the current accepted question');
    const saved = this.readOwnerRoutineToolAction(action.id as string, authority);
    const reply = this.getReplyForEvent(prior.id as string);
    if (!saved.preview || saved.sourceRevision !== sourceRevision || !reply || reply.dispatch_status !== 'accepted' || reply.source_revision !== sourceRevision || reply.payload !== routineProposalText(saved.preview))
      throw new Error('Accepted routine proposal unavailable');
    this.validateConfirmationReplyTarget(eventId, reply);
    for (const item of this.listOwnerRoutineToolActionsForThread(eventId)) {
      const metadata = JSON.parse(item.result_json as string);
      if (item.source_event_id !== eventId && ['create','adjust'].includes(metadata.args?.operation) && metadata.args.proposal?.action_id === action.id)
        throw new Error('Routine proposal already consumed');
    }
    if(('operation' in saved.preview)!==(expectedOperation==='adjust'))throw new Error('Routine confirmation operation mismatch');
    if('operation' in saved.preview)return {operation:'adjust',routine_id:saved.preview.routine_id,previous_definition_hash:saved.preview.previous_definition_hash,definition:saved.preview.definition,proposal:{event_id:prior.id as string,action_id:action.id as string,args_hash:action.payload_hash as string,accepted_dispatch_id:reply.dispatch_id as string,source_revision:sourceRevision}};
    return { operation: 'create', definition: saved.preview.definition, proposal: { event_id: prior.id as string, action_id: action.id as string, args_hash: action.payload_hash as string, accepted_dispatch_id: reply.dispatch_id as string, source_revision: sourceRevision } };
  }

  selectOwnerProcedureProposal(authority:ExplicitOwnerLoopAuthority,eventId:string):OwnerPreferenceToolArgs {
    const current=this.ownerReminderToolAdmission(authority,eventId,true);
    if(!clearRoutineYes(current.text))throw new Error('A clear subsequent yes is required');
    const prior=this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1',eventId,eventId);
    if(!prior||prior.outcome!=='resolved')throw new Error('Accepted standing-rule review unavailable');
    const action=this.listOwnerPreferenceToolActionsForThread(eventId).find(a=>a.source_event_id===prior.id);
    if(!action)throw new Error('Accepted standing-rule review unavailable');
    const metadata=JSON.parse(action.result_json as string);
    if(metadata.next_event_id!==eventId||metadata.next_event_accepted!==true)throw new Error('Standing-rule review is not the current accepted question');
    const saved=this.readOwnerPreferenceToolAction(action.id as string,authority),reply=this.getReplyForEvent(prior.id as string);
    if(!saved.preview||!reply||reply.dispatch_status!=='accepted'||reply.source_revision!=='owner-only-context-v1'||reply.payload!==procedureReviewText(saved.preview.review))throw new Error('Accepted standing-rule review unavailable');
    this.validateConfirmationReplyTarget(eventId,reply);
    for(const item of this.listOwnerPreferenceToolActionsForThread(eventId)){
      const other=JSON.parse(item.result_json as string);
      if(item.source_event_id!==eventId&&['append-procedure','replace-priorities','append-context'].includes(other.args?.operation)&&other.args.proposal?.action_id===action.id)throw new Error('Standing-rule review already consumed');
    }
    return ownerPreferenceToolArgsSchema.parse({operation:saved.preview.review.operation,review:saved.preview.review,proposal:{event_id:prior.id,action_id:action.id,args_hash:action.payload_hash,accepted_dispatch_id:reply.dispatch_id,source_revision:'owner-only-context-v1'}});
  }

  acceptedOwnerProcedureReview(authority: ExplicitOwnerLoopAuthority, eventId: string):
      {text: string; sourceRevision: 'owner-only-context-v1'} | null {
    try {
      const args = this.selectOwnerProcedureProposal(authority, eventId);
      if (args.operation !== 'append-procedure'&&args.operation !== 'replace-priorities'&&args.operation !== 'append-context') return null;
      return {text: procedureReviewText(args.review), sourceRevision: 'owner-only-context-v1'};
    } catch {
      // An ineligible confirmation supplies no remembered review to the model.
      return null;
    }
  }

  ownerProcedureOfferSelection(eventId: string): OwnerMeetingPrepArgs | null {
    return this.ownerGmailDraftOfferSelection(eventId);
  }

  ownerGmailDraftOfferSelection(eventId: string): OwnerMeetingPrepArgs | null {
    const row = this.getInbound(eventId);
    if (!row) return null;
    const inbound = (JSON.parse(String(row.payload)) as {inbound: Inbound}).inbound;
    return meetingPrepSelectors(inbound) ?? (clearMeetingPrepYes(inbound.text ?? '') && !inbound.replyBound && !inbound.media?.length
      ? this.continuationArgs(eventId) : null);
  }

  ownerGmailTypedBaseline(authority: ExplicitOwnerLoopAuthority, eventId: string, sourceRevision: string,
      messageId: string, actionType: 'reply'|'decline'): {event_id:string;action_id:string;args_hash:string;accepted_dispatch_id:string;source_revision:string} | undefined {
    this.ownerReminderToolAdmission(authority,eventId,true);
    const prior=this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1',eventId,eventId);
    if(!prior||prior.outcome!=='resolved')return undefined;
    const action=this.listOwnerGmailDraftActionsForThread(eventId).find(item=>item.source_event_id===prior.id);
    if(!action)return undefined;
    const saved=this.readOwnerGmailDraftAction(action.id as string,authority);
    if(!saved.preview){if(['propose_typed','propose_send_typed','propose_pair'].includes(saved.request.args.operation))throw new Error('Prior typed draft review unavailable');return undefined;}
    if(!('draft_origin' in saved.preview.review)||!saved.preview.review.draft_origin){if(!('kind' in saved.preview.review)||saved.preview.review.kind!=='reply_pair')return undefined;if(!saved.preview.offer_receipt)throw new Error('Offered pair recording unavailable');}
    const metadata=JSON.parse(String(action.result_json));const reply=this.getReplyForEvent(prior.id as string);
    if(metadata.next_event_id!==eventId||metadata.next_event_accepted!==true||saved.sourceRevision!==sourceRevision||
        !reply||reply.dispatch_status!=='accepted'||reply.source_revision!==sourceRevision||reply.payload!==gmailDraftReviewText(saved.preview))
      throw new Error('Accepted typed draft baseline unavailable');
    this.validateConfirmationReplyTarget(eventId,reply);
    if(saved.preview.review.message_id!==messageId||('draft_origin' in saved.preview.review&&saved.preview.review.draft_origin?.action_type!==actionType))
      throw new Error('Typed draft target or choice cannot change during an edit');
    for(const item of this.listOwnerGmailDraftActionsForThread(eventId)){
      const other=JSON.parse(String(item.result_json));
      if(item.source_event_id!==eventId&&['save','send'].includes(other.args?.operation)&&other.args.proposal?.action_id===action.id)
        throw new Error('Typed draft baseline already consumed');
    }
    return {event_id:prior.id as string,action_id:action.id as string,args_hash:action.payload_hash as string,accepted_dispatch_id:reply.dispatch_id as string,source_revision:sourceRevision};
  }

  prepareOwnerGmailDraftAction(authority: ExplicitOwnerLoopAuthority, eventId: string, input: GmailDraftArgs, sourceRevision?: string): OwnerGmailDraftRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerGmailDraftKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = gmailDraftArgsSchema.parse(input);
      if((args.operation==='propose_typed'||args.operation==='propose_send_typed')&&!this.one('SELECT id FROM actions WHERE source_event_id=? AND kind=?',eventId,ownerGmailDraftKind)&&JSON.stringify(args.baseline)!==JSON.stringify(this.ownerGmailTypedBaseline(authority,eventId,sourceRevision!,args.message_id,args.action_type)))throw new Error('Typed draft baseline binding unavailable');
      if (args.operation === 'list_followup') throw new Error('Draft list is read-only');
      if (args.operation === 'propose_followup' && args.to !== undefined && !followupEmailFromOwnerText(admitted.text,args.to)) throw new Error('Follow-up recipient is not an exact owner email');
      if (!['save','send'].includes(args.operation) && !sourceRevision)
        throw new Error('Draft review source proof unavailable');
      if (args.operation === 'propose_offer' && JSON.stringify(args.selection) !== JSON.stringify(this.ownerGmailDraftOfferSelection(eventId)))
        throw new Error('Gmail offer selection does not match admitted ingress');
      if (['save','send'].includes(args.operation) && (!sourceRevision || stableActionIdentity(eventId, ownerGmailDraftKind, args).actionId !== stableActionIdentity(eventId, ownerGmailDraftKind, this.selectOwnerGmailDraftProposal(authority, eventId, sourceRevision)).actionId))
        throw new Error('Draft review binding unavailable');
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerTaskToolKind))
        throw new Error('Owner message already has another private mutation');
      const binding = stableActionIdentity(eventId, ownerGmailDraftKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerGmailDraftKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Gmail draft event conflicts with saved arguments');
      if (previous) {
        this.readOwnerGmailDraftAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      }
      else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerGmailDraftKind, binding.payloadHash, eventId, admitted.payloadHash, hash(admitted.text), JSON.stringify({ args, scope_hash: authority.scopeHash, source_revision: sourceRevision }), stamp, stamp);
      }
      return { ...admitted, actionId: binding.actionId, args };
    });
  }

  listOwnerGmailDraftActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerGmailDraftKind);
  }

  readOwnerGmailDraftAction(actionId:string,authority:Omit<ExplicitOwnerLoopAuthority,'runId'>) {
    return this.readOwnerGmailDraftRecord(actionId,authority,true);
  }

  private readOwnerGmailDraftRecord(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>, validateLineage:boolean): {
    request: OwnerGmailDraftRequest;
    status: string;
    receipt: GmailDraftReceipt | null;
    preview: GmailDraftPreview | null;
    sourceRevision: string | undefined;
    rejection: GmailDraftRejection | null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerGmailDraftKind || typeof action.source_event_id !== 'string')
      throw new Error('Gmail draft action is invalid');
    const admission = this.ownerReminderToolAdmission({ ...authority, runId: action.run_id as string }, action.source_event_id, false, false, true);
    let metadata: Record<string, unknown>;
    try {
      metadata = JSON.parse(action.result_json as string);
    }
    catch {
      throw new Error('Gmail draft action is invalid');
    }
    const args = gmailDraftArgsSchema.parse(metadata.args);
    if (args.operation === 'propose_offer' && JSON.stringify(args.selection) !== JSON.stringify(this.ownerGmailDraftOfferSelection(admission.eventId)))
      throw new Error('Gmail offer selection does not match admitted ingress');
    if (args.operation === 'list_followup' || args.operation === 'propose_followup' && args.to !== undefined && !followupEmailFromOwnerText(admission.text,args.to)) throw new Error('Follow-up draft binding is invalid');
    const binding = stableActionIdentity(admission.eventId, ownerGmailDraftKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
      action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Gmail draft action is invalid');
    const preview = action.status === 'succeeded' && !['save','send'].includes(args.operation) ? gmailDraftPreviewSchema.parse(metadata.preview) : null;
    if (preview && (preview.event_id !== admission.eventId || preview.payload_hash !== admission.payloadHash || !validGmailDraftReview(preview) || !gmailDraftPreviewMatches(preview,args) || preview.review.source_revision !== metadata.source_revision))
      throw new Error('Draft preview binding invalid');
    if(preview?.offer_receipt){const r=preview.review,receipt=preview.offer_receipt;const reply=this.getReplyForEvent(admission.eventId);if(!('kind' in r)||r.kind!=='reply_pair'||receipt.action_id!==actionId||receipt.event_id!==admission.eventId||receipt.payload_hash!==admission.payloadHash||receipt.args_hash!==binding.payloadHash||receipt.review_hash!==r.review_hash||receipt.reply_key!==r.reply_key||receipt.decline_key!==r.decline_key||!reply||reply.dispatch_status!=='accepted'||receipt.accepted_dispatch_id!==reply.dispatch_id||reply.payload!==gmailDraftReviewText(preview)||reply.source_revision!==r.source_revision)throw new Error('Saved offered pair receipt invalid');}
    if(validateLineage&&preview&&(args.operation==='propose_typed'||args.operation==='propose_send_typed')){
      const origin='draft_origin' in preview.review?preview.review.draft_origin:undefined;
      if(!origin)throw new Error('Typed draft origin unavailable');
      if(args.baseline){
        if(args.baseline.action_id===actionId||origin.baseline_action_id===actionId)throw new Error('Typed draft self-reference invalid');
        const baseline=this.readOwnerGmailDraftRecord(args.baseline.action_id,authority,false);
        const root=baseline.preview&&'draft_origin' in baseline.preview.review?baseline.preview.review.draft_origin:baseline.preview&&'kind' in baseline.preview.review&&baseline.preview.review.kind==='reply_pair'&&baseline.preview.offer_receipt?{action_type:args.action_type,baseline_action_id:args.baseline.action_id,baseline_key:args.action_type==='reply'?baseline.preview.review.reply_key:baseline.preview.review.decline_key,baseline_body:args.action_type==='reply'?baseline.preview.review.accept_body:baseline.preview.review.decline_body}:undefined;
        const accepted=this.getReplyForEvent(args.baseline.event_id);
        const baselineAction=this.getAction(args.baseline.action_id);
        if(!root||!baselineAction||baselineAction.source_event_id!==args.baseline.event_id||baselineAction.payload_hash!==args.baseline.args_hash||
            !accepted||accepted.dispatch_status!=='accepted'||accepted.dispatch_id!==args.baseline.accepted_dispatch_id||accepted.payload!==gmailDraftReviewText(baseline.preview!)||
            baseline.sourceRevision!==args.baseline.source_revision||!('message_id' in baseline.preview!.review)||baseline.preview!.review.message_id!==args.message_id||root.action_type!==args.action_type||JSON.stringify(origin)!==JSON.stringify(root))
          throw new Error('Typed draft root binding invalid');
        const rootAction=this.readOwnerGmailDraftRecord(origin.baseline_action_id,authority,false);
        const rootArgs=rootAction.request.args;
        const rootPreview=rootAction.preview;
        if(rootArgs.operation==='propose_pair'){if(!rootPreview||!rootPreview.offer_receipt||!('kind' in rootPreview.review)||rootPreview.review.kind!=='reply_pair'||rootArgs.message_id!==args.message_id||origin.baseline_key!==(args.action_type==='reply'?rootPreview.review.reply_key:rootPreview.review.decline_key)||origin.baseline_body!==(args.action_type==='reply'?rootPreview.review.accept_body:rootPreview.review.decline_body))throw new Error('Pair root baseline invalid');}
        else if((rootArgs.operation!=='propose_typed'&&rootArgs.operation!=='propose_send_typed')||rootArgs.baseline||rootArgs.message_id!==args.message_id||rootArgs.action_type!==args.action_type||rootArgs.body!==origin.baseline_body||!rootPreview||!('draft_origin' in rootPreview.review)||JSON.stringify(rootPreview.review.draft_origin)!==JSON.stringify(origin))throw new Error('Typed root baseline invalid');
      }else if(origin.baseline_action_id!==action.id||origin.baseline_body!==args.body)throw new Error('Typed draft root binding invalid');
    }
    const receipt = action.status === 'succeeded' && ['save','send'].includes(args.operation) ? gmailDraftReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
      receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
      !gmailDraftReceiptMatches(receipt, args)))
      throw new Error('Gmail draft receipt is invalid');
    const rejection = action.status === 'rejected' ? gmailDraftRejectionSchema.parse(metadata.rejection) : null;
    if (rejection !== null && !gmailDraftRejectionSchema.safeParse(rejection).success)
      throw new Error('Gmail draft rejection is invalid');
    return { request: { ...admission, actionId, args }, status: action.status as string, receipt, rejection, preview, sourceRevision: typeof metadata.source_revision === 'string' ? metadata.source_revision : undefined };
  }

  settleOwnerGmailDraftAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>, result: GmailDraftReceipt | GmailDraftRejection | GmailDraftPreview, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerGmailDraftAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing'))
        throw new Error('Gmail draft action is not eligible');
      const metadata = { args: saved.request.args, scope_hash: authority.scopeHash, source_revision: saved.sourceRevision };
      if (typeof result === 'string') {
        if (!gmailDraftRejectionSchema.safeParse(result).success)
          throw new Error('Gmail draft rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({ ...metadata, rejection: result }), now(), actionId);
      }
      else if (result.status === 'preview') {
        const preview = gmailDraftPreviewSchema.parse(result);
        if ((reconciled && !['propose_typed','propose_send_typed','propose_pair'].includes(saved.request.args.operation)) || ['save','send'].includes(saved.request.args?.operation ?? '') || preview.event_id !== saved.request.eventId || preview.payload_hash !== saved.request.payloadHash || !validGmailDraftReview(preview) || !gmailDraftPreviewMatches(preview,saved.request.args) || preview.review.source_revision !== saved.sourceRevision)
          throw new Error('Draft preview invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?").run(JSON.stringify({ ...metadata, preview }), now(), actionId);
        this.readOwnerGmailDraftAction(actionId,authority);
      }
      else {
        const receipt = gmailDraftReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerGmailDraftKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
          receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
          !saved.request.args || !gmailDraftReceiptMatches(receipt, saved.request.args))
          throw new Error('Gmail draft receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({ ...metadata, receipt }), now(), actionId);
      }
    });
  }

  listPendingGmailOfferedPairs(): Row[] {
    return this.db.prepare("SELECT a.*,r.thread_key FROM actions a JOIN runs r ON r.id=a.run_id WHERE a.kind=? AND a.status='succeeded' AND json_extract(a.result_json,'$.args.operation')='propose_pair' AND json_extract(a.result_json,'$.preview.offer_receipt') IS NULL AND EXISTS (SELECT 1 FROM prepared_replies p JOIN dispatches d ON d.id=p.dispatch_id WHERE p.event_id=a.source_event_id AND d.status='accepted') ORDER BY a.created_at,a.id LIMIT 100").all(ownerGmailDraftKind) as Row[];
  }

  acceptedGmailOfferedPair(actionId:string,authority:Omit<ExplicitOwnerLoopAuthority,'runId'>):{request:OwnerGmailDraftRequest;acceptedDelivery:GmailAcceptedDelivery}|null {
    const saved=this.readOwnerGmailDraftAction(actionId,authority);
    if(saved.request.args.operation!=='propose_pair'||!saved.preview||saved.preview.offer_receipt||saved.status!=='succeeded')return null;
    const reply=this.getReplyForEvent(saved.request.eventId);
    if(!reply||reply.dispatch_status!=='accepted'||reply.source_revision!==saved.sourceRevision||reply.payload!==gmailDraftReviewText(saved.preview))return null;
    const dispatch=this.getDispatch(String(reply.dispatch_id));
    if(!dispatch||dispatch.status!=='accepted'||typeof dispatch.provider_message_id!=='string')return null;
    const run=this.getRun(String(reply.run_id));const event=this.getInbound(saved.request.eventId);
    if(!run||!event||run.event_id!==saved.request.eventId||run.thread_key!==event.thread_key||dispatch.destination!==(JSON.parse(String(event.payload)) as {context?:{destination?:unknown}}).context?.destination)return null;
    return {request:saved.request,acceptedDelivery:gmailAcceptedDeliverySchema.parse({accepted_dispatch_id:reply.dispatch_id,provider_message_id:dispatch.provider_message_id,source_revision:saved.sourceRevision,review_hash:saved.preview.review.review_hash,payload_hash:hash(gmailDraftReviewText(saved.preview))})};
  }

  settleGmailOfferedPair(actionId:string,response:GmailDraftPreview):void {
    this.transaction(()=>{
      const action=this.getAction(actionId);if(!action||action.kind!==ownerGmailDraftKind||action.status!=='succeeded')throw new Error('Offered pair unavailable');
      const metadata=JSON.parse(String(action.result_json));const before=gmailDraftPreviewSchema.parse(metadata.preview);const after=gmailDraftPreviewSchema.parse(response);
      const {offer_receipt:_old,...oldPreview}=before;const {offer_receipt:receipt,...newPreview}=after;
      if(!receipt||!('kind' in after.review)||after.review.kind!=='reply_pair'||JSON.stringify(oldPreview)!==JSON.stringify(newPreview)||metadata.args.operation!=='propose_pair')throw new Error('Offered pair changed');
      const binding=stableActionIdentity(String(action.source_event_id),ownerGmailDraftKind,metadata.args);const reply=this.getReplyForEvent(String(action.source_event_id));
      if(receipt.action_id!==actionId||receipt.event_id!==action.source_event_id||receipt.payload_hash!==action.source_payload_hash||receipt.args_hash!==binding.payloadHash||receipt.review_hash!==after.review.review_hash||receipt.reply_key!==after.review.reply_key||receipt.decline_key!==after.review.decline_key||!reply||reply.dispatch_status!=='accepted'||receipt.accepted_dispatch_id!==reply.dispatch_id||reply.payload!==gmailDraftReviewText(after)||reply.source_revision!==after.review.source_revision)throw new Error('Offered receipt binding invalid');
      if(before.offer_receipt&&JSON.stringify(before.offer_receipt)!==JSON.stringify(receipt))throw new Error('Offered receipt changed');
      this.db.prepare('UPDATE actions SET result_json=?,updated_at=? WHERE id=?').run(JSON.stringify({...metadata,preview:after}),now(),actionId);
    });
  }

  listPendingGmailDraftLearning(): Row[] {
    return this.db.prepare("SELECT a.*,r.thread_key FROM actions a JOIN runs r ON r.id=a.run_id WHERE a.kind=? AND a.status='succeeded' AND json_extract(a.result_json,'$.receipt.learning')='pending' ORDER BY a.created_at,a.id LIMIT 100").all(ownerGmailDraftKind) as Row[];
  }

  refreshOwnerGmailDraftLearning(actionId:string, authority:Omit<ExplicitOwnerLoopAuthority,'runId'>, receipt:GmailDraftReceipt):void {
    this.transaction(()=>{
      const saved=this.readOwnerGmailDraftAction(actionId,authority);
      const incoming=gmailDraftReceiptSchema.parse(receipt);
      if(saved.status!=='succeeded'||saved.receipt?.learning!=='pending'||incoming.learning!=='recorded')throw new Error('Draft learning refresh unavailable');
      const {learning:_old,...before}=saved.receipt;const {learning:_new,...after}=incoming;
      if(JSON.stringify(before)!==JSON.stringify(after))throw new Error('Draft effect receipt changed during learning refresh');
      const action=this.getAction(actionId)!;const metadata=JSON.parse(String(action.result_json));
      this.db.prepare('UPDATE actions SET result_json=?,updated_at=? WHERE id=?').run(JSON.stringify({...metadata,receipt:incoming}),now(),actionId);
    });
  }

  selectOwnerGmailDraftProposal(authority: ExplicitOwnerLoopAuthority, eventId: string, sourceRevision: string): GmailDraftArgs {
    const current = this.ownerReminderToolAdmission(authority, eventId, true);
    const prior = this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1', eventId, eventId);
    if (!prior || prior.outcome !== 'resolved')
      throw new Error('Accepted draft review unavailable');
    const action = this.listOwnerGmailDraftActionsForThread(eventId).find(a => a.source_event_id === prior.id);
    if (!action)
      throw new Error('Accepted draft review unavailable');
    const proposalMetadata = JSON.parse(action.result_json as string);
    if (proposalMetadata.next_event_id !== eventId || proposalMetadata.next_event_accepted !== true)
      throw new Error('Draft review is not the current accepted question');
    const saved = this.readOwnerGmailDraftAction(action.id as string, authority);
    const reply = this.getReplyForEvent(prior.id as string);
    if (!saved.preview || saved.sourceRevision !== sourceRevision || !reply || reply.dispatch_status !== 'accepted' || reply.source_revision !== sourceRevision || reply.payload !== gmailDraftReviewText(saved.preview))
      throw new Error('Accepted draft review unavailable');
    if('kind' in saved.preview.review&&saved.preview.review.kind==='reply_pair')throw new Error('Choose Accept or Decline before reviewing a single draft');
    const sending='kind' in saved.preview.review && ['reply_send','new_send'].includes(saved.preview.review.kind);
    if(!clearRoutineYes(current.text) && !(sending && clearGmailSendYes(current.text)))throw new Error('A clear subsequent yes is required');
    this.validateConfirmationReplyTarget(eventId, reply);
    for (const item of this.listOwnerGmailDraftActionsForThread(eventId)) {
      const metadata = JSON.parse(item.result_json as string);
      if (item.source_event_id !== eventId && ['save','send'].includes(metadata.args?.operation) && metadata.args.proposal?.action_id === action.id)
        throw new Error('Draft review already consumed');
    }
    if ('kind' in saved.preview.review && (saved.preview.review.kind==='reply_send'||saved.preview.review.kind==='new_send')) return {operation:'send',review:saved.preview.review,proposal:{event_id:prior.id as string,action_id:action.id as string,args_hash:action.payload_hash as string,accepted_dispatch_id:reply.dispatch_id as string,source_revision:sourceRevision}};
    return { operation: 'save', review: saved.preview.review, proposal: { event_id: prior.id as string, action_id: action.id as string, args_hash: action.payload_hash as string, accepted_dispatch_id: reply.dispatch_id as string, source_revision: sourceRevision, ...(saved.request.args.operation === 'propose_offer' ? {origin:{selection:saved.request.args.selection,offer_id:saved.preview.offer_id!}} : saved.request.args.operation === 'propose_followup' ? {origin:{kind:'followup' as const,offer_id:saved.request.args.offer_id,...(saved.request.args.to === undefined ? {} : {to:saved.request.args.to})}} : {}) } };
  }

  prepareOwnerCalendarAction(authority: ExplicitOwnerLoopAuthority, eventId: string, input: CalendarArgs, sourceRevision?: string): OwnerCalendarRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerCalendarKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = calendarArgsSchema.parse(input);
      if (!['move','rsvp','create','update','move_update'].includes(args.operation) && !sourceRevision)
        throw new Error('Calendar review source proof unavailable');
      if (['move','rsvp','create','update','move_update'].includes(args.operation) && (!sourceRevision || stableActionIdentity(eventId, ownerCalendarKind, args).actionId !== stableActionIdentity(eventId, ownerCalendarKind, this.selectOwnerCalendarProposal(authority, eventId, sourceRevision)).actionId))
        throw new Error('Calendar review binding unavailable');
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerTaskToolKind))
        throw new Error('Owner message already has another private mutation');
      const binding = stableActionIdentity(eventId, ownerCalendarKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerCalendarKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Calendar move event conflicts with saved arguments');
      if (previous) {
        this.readOwnerCalendarAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      }
      else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerCalendarKind, binding.payloadHash, eventId, admitted.payloadHash, hash(admitted.text), JSON.stringify({ args, scope_hash: authority.scopeHash, source_revision: sourceRevision }), stamp, stamp);
      }
      return { ...admitted, actionId: binding.actionId, args };
    });
  }

  listOwnerCalendarActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerCalendarKind);
  }

  readOwnerCalendarAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerCalendarRequest;
    status: string;
    receipt: CalendarReceipt | null;
    preview: CalendarPreview | null;
    sourceRevision: string | undefined;
    rejection: CalendarRejection | null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerCalendarKind || typeof action.source_event_id !== 'string')
      throw new Error('Calendar move action is invalid');
    const admission = this.ownerReminderToolAdmission({ ...authority, runId: action.run_id as string }, action.source_event_id, false, false, true);
    let metadata: Record<string, unknown>;
    try {
      metadata = JSON.parse(action.result_json as string);
    }
    catch {
      throw new Error('Calendar move action is invalid');
    }
    const args = calendarArgsSchema.parse(metadata.args);
    const binding = stableActionIdentity(admission.eventId, ownerCalendarKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
      action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Calendar move action is invalid');
    const preview = action.status === 'succeeded' && !['move','rsvp','create','update','move_update'].includes(args.operation) ? calendarPreviewSchema.parse(metadata.preview) : null;
    if (preview && (preview.event_id !== admission.eventId || preview.payload_hash !== admission.payloadHash || !validCalendarReview(preview) || !calendarPreviewMatches(preview,args,actionId) || preview.review.source_revision !== metadata.source_revision))
      throw new Error('Calendar preview binding invalid');
    const receipt = action.status === 'succeeded' && ['move','rsvp','create','update','move_update'].includes(args.operation) ? calendarReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
      receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
      !calendarReceiptMatches(receipt, args)))
      throw new Error('Calendar move receipt is invalid');
    const rejection = action.status === 'rejected' ? calendarRejectionSchema.parse(metadata.rejection) : null;
    if (rejection !== null && !calendarRejectionSchema.safeParse(rejection).success)
      throw new Error('Calendar move rejection is invalid');
    return { request: { ...admission, actionId, args }, status: action.status as string, receipt, rejection, preview, sourceRevision: typeof metadata.source_revision === 'string' ? metadata.source_revision : undefined };
  }

  settleOwnerCalendarAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>, result: CalendarReceipt | CalendarRejection | CalendarPreview, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerCalendarAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing'))
        throw new Error('Calendar move action is not eligible');
      const metadata = { args: saved.request.args, scope_hash: authority.scopeHash, source_revision: saved.sourceRevision };
      if (typeof result === 'string') {
        if (!calendarRejectionSchema.safeParse(result).success)
          throw new Error('Calendar move rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({ ...metadata, rejection: result }), now(), actionId);
      }
      else if (result.status === 'preview') {
        const preview = calendarPreviewSchema.parse(result);
        if (reconciled || ['move','rsvp','create','update','move_update'].includes(saved.request.args.operation) || preview.event_id !== saved.request.eventId || preview.payload_hash !== saved.request.payloadHash || !validCalendarReview(preview) || !calendarPreviewMatches(preview,saved.request.args,actionId) || preview.review.source_revision !== saved.sourceRevision)
          throw new Error('Calendar preview invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?").run(JSON.stringify({ ...metadata, preview }), now(), actionId);
      }
      else {
        const receipt = calendarReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerCalendarKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
          receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
          !saved.request.args || !calendarReceiptMatches(receipt, saved.request.args))
          throw new Error('Calendar move receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({ ...metadata, receipt }), now(), actionId);
      }
    });
  }

  selectOwnerCalendarProposal(authority: ExplicitOwnerLoopAuthority, eventId: string, sourceRevision: string): CalendarArgs {
    const current = this.ownerReminderToolAdmission(authority, eventId, true);
    if (!clearRoutineYes(current.text))
      throw new Error('A clear subsequent yes is required');
    const prior = this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1', eventId, eventId);
    if (!prior || prior.outcome !== 'resolved')
      throw new Error('Accepted Calendar review unavailable');
    const action = this.listOwnerCalendarActionsForThread(eventId).find(a => a.source_event_id === prior.id);
    if (!action)
      throw new Error('Accepted Calendar review unavailable');
    const proposalMetadata = JSON.parse(action.result_json as string);
    if (proposalMetadata.next_event_id !== eventId || proposalMetadata.next_event_accepted !== true)
      throw new Error('Calendar review is not the current accepted question');
    const saved = this.readOwnerCalendarAction(action.id as string, authority);
    const reply = this.getReplyForEvent(prior.id as string);
    if (!saved.preview || saved.sourceRevision !== sourceRevision || !reply || reply.dispatch_status !== 'accepted' || reply.source_revision !== sourceRevision || reply.payload !== calendarReviewText(saved.preview))
      throw new Error('Accepted Calendar review unavailable');
    this.validateConfirmationReplyTarget(eventId, reply);
    for (const item of this.listOwnerCalendarActionsForThread(eventId)) {
      const metadata = JSON.parse(item.result_json as string);
      if (item.source_event_id !== eventId && ['move','rsvp','create','update','move_update'].includes(metadata.args?.operation) && metadata.args.proposal?.action_id === action.id)
        throw new Error('Calendar review already consumed');
    }
    return calendarArgsSchema.parse({operation:saved.preview.review.operation,review:saved.preview.review,proposal:{event_id:prior.id as string,action_id:action.id as string,args_hash:action.payload_hash as string,accepted_dispatch_id:reply.dispatch_id as string,source_revision:sourceRevision}});
  }

  prepareOwnerContactsAction(authority: ExplicitOwnerLoopAuthority, eventId: string, input: ContactsArgs, sourceRevision?: string): OwnerContactsRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerContactsKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = contactsArgsSchema.parse(input);
      if (!['create','update','delete'].includes(args.operation) && !sourceRevision)
        throw new Error('Contacts review source proof unavailable');
      if (['create','update','delete'].includes(args.operation) && (!sourceRevision || contactsActionIdentity(eventId, ownerContactsKind, args).actionId !== contactsActionIdentity(eventId, ownerContactsKind, this.selectOwnerContactsProposal(authority, eventId, sourceRevision)).actionId))
        throw new Error('Contacts review binding unavailable');
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerTaskToolKind))
        throw new Error('Owner message already has another private mutation');
      const binding = contactsActionIdentity(eventId, ownerContactsKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerContactsKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Contacts move event conflicts with saved arguments');
      if (previous) {
        this.readOwnerContactsAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      }
      else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerContactsKind, binding.payloadHash, eventId, admitted.payloadHash, hash(admitted.text), JSON.stringify({ args, scope_hash: authority.scopeHash, source_revision: sourceRevision }), stamp, stamp);
      }
      return { ...admitted, actionId: binding.actionId, args };
    });
  }

  listOwnerContactsActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerContactsKind);
  }

  readOwnerContactsAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerContactsRequest;
    status: string;
    receipt: ContactsReceipt | null;
    preview: ContactsPreview | null;
    sourceRevision: string | undefined;
    rejection: ContactsRejection | null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerContactsKind || typeof action.source_event_id !== 'string')
      throw new Error('Contacts move action is invalid');
    const admission = this.ownerReminderToolAdmission({ ...authority, runId: action.run_id as string }, action.source_event_id, false, false, true);
    let metadata: Record<string, unknown>;
    try {
      metadata = JSON.parse(action.result_json as string);
    }
    catch {
      throw new Error('Contacts move action is invalid');
    }
    const args = contactsArgsSchema.parse(metadata.args);
    const binding = contactsActionIdentity(admission.eventId, ownerContactsKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
      action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Contacts move action is invalid');
    const preview = action.status === 'succeeded' && !['create','update','delete'].includes(args.operation) ? contactsPreviewSchema.parse(metadata.preview) : null;
    if (preview && (preview.event_id !== admission.eventId || preview.payload_hash !== admission.payloadHash || !validContactsReview(preview) || !contactsPreviewMatches(preview,args) || preview.review.source_revision !== metadata.source_revision))
      throw new Error('Contacts preview binding invalid');
    const receipt = action.status === 'succeeded' && ['create','update','delete'].includes(args.operation) ? contactsReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
      receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
      !contactsReceiptMatches(receipt, args)))
      throw new Error('Contacts move receipt is invalid');
    const rejection = action.status === 'rejected' ? contactsRejectionSchema.parse(metadata.rejection) : null;
    if (rejection !== null && !contactsRejectionSchema.safeParse(rejection).success)
      throw new Error('Contacts move rejection is invalid');
    return { request: { ...admission, actionId, args }, status: action.status as string, receipt, rejection, preview, sourceRevision: typeof metadata.source_revision === 'string' ? metadata.source_revision : undefined };
  }

  settleOwnerContactsAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>, result: ContactsReceipt | ContactsRejection | ContactsPreview, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerContactsAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing'))
        throw new Error('Contacts move action is not eligible');
      const metadata = { args: saved.request.args, scope_hash: authority.scopeHash, source_revision: saved.sourceRevision };
      if (typeof result === 'string') {
        if (!contactsRejectionSchema.safeParse(result).success)
          throw new Error('Contacts move rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({ ...metadata, rejection: result }), now(), actionId);
      }
      else if (result.status === 'preview') {
        const preview = contactsPreviewSchema.parse(result);
        if (reconciled || ['create','update','delete'].includes(saved.request.args.operation) || preview.event_id !== saved.request.eventId || preview.payload_hash !== saved.request.payloadHash || !validContactsReview(preview) || !contactsPreviewMatches(preview,saved.request.args) || preview.review.source_revision !== saved.sourceRevision)
          throw new Error('Contacts preview invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?").run(JSON.stringify({ ...metadata, preview }), now(), actionId);
      }
      else {
        const receipt = contactsReceiptSchema.parse(result);
        const binding = contactsActionIdentity(saved.request.eventId, ownerContactsKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
          receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
          !saved.request.args || !contactsReceiptMatches(receipt, saved.request.args))
          throw new Error('Contacts move receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({ ...metadata, receipt }), now(), actionId);
      }
    });
  }

  selectOwnerContactsProposal(authority: ExplicitOwnerLoopAuthority, eventId: string, sourceRevision: string): ContactsArgs {
    const current = this.ownerReminderToolAdmission(authority, eventId, true);
    if (!clearRoutineYes(current.text))
      throw new Error('A clear subsequent yes is required');
    const prior = this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1', eventId, eventId);
    if (!prior || prior.outcome !== 'resolved')
      throw new Error('Accepted Contacts review unavailable');
    const action = this.listOwnerContactsActionsForThread(eventId).find(a => a.source_event_id === prior.id);
    if (!action)
      throw new Error('Accepted Contacts review unavailable');
    const proposalMetadata = JSON.parse(action.result_json as string);
    if (proposalMetadata.next_event_id !== eventId || proposalMetadata.next_event_accepted !== true)
      throw new Error('Contacts review is not the current accepted question');
    const saved = this.readOwnerContactsAction(action.id as string, authority);
    const reply = this.getReplyForEvent(prior.id as string);
    if (!saved.preview || saved.sourceRevision !== sourceRevision || !reply || reply.dispatch_status !== 'accepted' || reply.source_revision !== sourceRevision || reply.payload !== contactsReviewText(saved.preview))
      throw new Error('Accepted Contacts review unavailable');
    this.validateConfirmationReplyTarget(eventId, reply);
    for (const item of this.listOwnerContactsActionsForThread(eventId)) {
      const metadata = JSON.parse(item.result_json as string);
      if (item.source_event_id !== eventId && ['create','update','delete'].includes(metadata.args?.operation) && metadata.args.proposal?.action_id === action.id)
        throw new Error('Contacts review already consumed');
    }
    return contactsArgsSchema.parse({operation:saved.preview.review.operation,review:saved.preview.review,proposal:{event_id:prior.id as string,action_id:action.id as string,args_hash:action.payload_hash as string,accepted_dispatch_id:reply.dispatch_id as string,source_revision:sourceRevision}});
  }

  prepareOwnerBridgeSendAction(authority: ExplicitOwnerLoopAuthority, eventId: string, input: BridgeSendArgs, sourceRevision?: string): OwnerBridgeSendRequest {
    return this.transaction(() => {
      this.assertNoOtherOwnerAction(eventId, ownerBridgeSendKind);
      const admitted = this.ownerReminderToolAdmission(authority, eventId, true);
      const args = bridgeSendArgsSchema.parse(input);
      if (!['send'].includes(args.operation) && !sourceRevision)
        throw new Error('BridgeSend review source proof unavailable');
      if (['send'].includes(args.operation) && (!sourceRevision || stableActionIdentity(eventId, ownerBridgeSendKind, args).actionId !== stableActionIdentity(eventId, ownerBridgeSendKind, this.selectOwnerBridgeSendProposal(authority, eventId, sourceRevision)).actionId))
        throw new Error('BridgeSend review binding unavailable');
      if (this.one('SELECT id FROM actions WHERE source_event_id=? AND kind IN (?,?)', eventId, ownerReminderToolKind, ownerTaskToolKind))
        throw new Error('Owner message already has another private mutation');
      const binding = stableActionIdentity(eventId, ownerBridgeSendKind, args);
      const previous = this.one('SELECT * FROM actions WHERE source_event_id=? AND kind=?', eventId, ownerBridgeSendKind);
      if (previous && (previous.id !== binding.actionId || previous.payload_hash !== binding.payloadHash))
        throw new Error('Bridge message event conflicts with saved arguments');
      if (previous) {
        this.readOwnerBridgeSendAction(previous.id as string, authority);
        if (previous.status === 'pending' && previous.run_id !== authority.runId)
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), previous.id as string);
      }
      else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)")
          .run(binding.actionId, authority.runId, ownerBridgeSendKind, binding.payloadHash, eventId, admitted.payloadHash, hash(admitted.text), JSON.stringify({ args, scope_hash: authority.scopeHash, source_revision: sourceRevision }), stamp, stamp);
      }
      return { ...admitted, actionId: binding.actionId, args };
    });
  }

  listOwnerBridgeSendActionsForThread(eventId: string): Row[] {
    return this.listOwnerActionsForThread(eventId, ownerBridgeSendKind);
  }

  readOwnerBridgeSendAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): {
    request: OwnerBridgeSendRequest;
    status: string;
    receipt: BridgeSendReceipt | null;
    preview: BridgeSendPreview | null;
    sourceRevision: string | undefined;
    rejection: BridgeSendRejection | null;
  } {
    const action = this.getAction(actionId);
    if (!action || action.kind !== ownerBridgeSendKind || typeof action.source_event_id !== 'string')
      throw new Error('Bridge message action is invalid');
    const admission = this.ownerReminderToolAdmission({ ...authority, runId: action.run_id as string }, action.source_event_id, false, false, true);
    let metadata: Record<string, unknown>;
    try {
      metadata = JSON.parse(action.result_json as string);
    }
    catch {
      throw new Error('Bridge message action is invalid');
    }
    const args = bridgeSendArgsSchema.parse(metadata.args);
    const binding = stableActionIdentity(admission.eventId, ownerBridgeSendKind, args);
    if (metadata.scope_hash !== authority.scopeHash || action.id !== binding.actionId || action.payload_hash !== binding.payloadHash ||
      action.source_payload_hash !== admission.payloadHash || action.source_text_hash !== hash(admission.text))
      throw new Error('Bridge message action is invalid');
    const preview = action.status === 'succeeded' && !['send'].includes(args.operation) ? bridgeSendPreviewSchema.parse(metadata.preview) : null;
    if (preview && (preview.event_id !== admission.eventId || preview.payload_hash !== admission.payloadHash || !validBridgeSendReview(preview) || !bridgeSendPreviewMatches(preview,args) || preview.review.source_revision !== metadata.source_revision))
      throw new Error('BridgeSend preview binding invalid');
    const receipt = action.status === 'succeeded' && ['send'].includes(args.operation) ? bridgeSendReceiptSchema.parse(metadata.receipt) : null;
    if (receipt && (receipt.action_id !== action.id || receipt.event_id !== admission.eventId ||
      receipt.payload_hash !== admission.payloadHash || receipt.args_hash !== binding.payloadHash ||
      !bridgeSendReceiptMatches(receipt, args)))
      throw new Error('Bridge message receipt is invalid');
    const rejection = action.status === 'rejected' ? bridgeSendRejectionSchema.parse(metadata.rejection) : null;
    if (rejection !== null && !bridgeSendRejectionSchema.safeParse(rejection).success)
      throw new Error('Bridge message rejection is invalid');
    return { request: { ...admission, actionId, args }, status: action.status as string, receipt, rejection, preview, sourceRevision: typeof metadata.source_revision === 'string' ? metadata.source_revision : undefined };
  }

  settleOwnerBridgeSendAction(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>, result: BridgeSendReceipt | BridgeSendRejection | BridgeSendPreview, reconciled = false): void {
    this.transaction(() => {
      const saved = this.readOwnerBridgeSendAction(actionId, authority);
      if (saved.status !== (reconciled ? 'unknown' : 'executing'))
        throw new Error('Bridge message action is not eligible');
      const metadata = { args: saved.request.args, scope_hash: authority.scopeHash, source_revision: saved.sourceRevision };
      if (typeof result === 'string') {
        if (!bridgeSendRejectionSchema.safeParse(result).success)
          throw new Error('Bridge message rejection is invalid');
        this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({ ...metadata, rejection: result }), now(), actionId);
      }
      else if (result.status === 'preview') {
        const preview = bridgeSendPreviewSchema.parse(result);
        if (reconciled || ['send'].includes(saved.request.args.operation) || preview.event_id !== saved.request.eventId || preview.payload_hash !== saved.request.payloadHash || !validBridgeSendReview(preview) || !bridgeSendPreviewMatches(preview,saved.request.args) || preview.review.source_revision !== saved.sourceRevision)
          throw new Error('BridgeSend preview invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?").run(JSON.stringify({ ...metadata, preview }), now(), actionId);
      }
      else {
        const receipt = bridgeSendReceiptSchema.parse(result);
        const binding = stableActionIdentity(saved.request.eventId, ownerBridgeSendKind, saved.request.args);
        if (receipt.action_id !== actionId || receipt.event_id !== saved.request.eventId ||
          receipt.payload_hash !== saved.request.payloadHash || receipt.args_hash !== binding.payloadHash ||
          !saved.request.args || !bridgeSendReceiptMatches(receipt, saved.request.args))
          throw new Error('Bridge message receipt is invalid');
        this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
          .run(JSON.stringify({ ...metadata, receipt }), now(), actionId);
      }
    });
  }

  selectOwnerBridgeSendProposal(authority: ExplicitOwnerLoopAuthority, eventId: string, sourceRevision: string): BridgeSendArgs {
    const current = this.ownerReminderToolAdmission(authority, eventId, true);
    if (!clearRoutineYes(current.text) && !clearGmailSendYes(current.text))
      throw new Error('A clear subsequent yes is required');
    const prior = this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1', eventId, eventId);
    if (!prior || prior.outcome !== 'resolved')
      throw new Error('Accepted BridgeSend review unavailable');
    const action = this.listOwnerBridgeSendActionsForThread(eventId).find(a => a.source_event_id === prior.id);
    if (!action)
      throw new Error('Accepted BridgeSend review unavailable');
    const proposalMetadata = JSON.parse(action.result_json as string);
    if (proposalMetadata.next_event_id !== eventId || proposalMetadata.next_event_accepted !== true)
      throw new Error('BridgeSend review is not the current accepted question');
    const saved = this.readOwnerBridgeSendAction(action.id as string, authority);
    const reply = this.getReplyForEvent(prior.id as string);
    if (!saved.preview || saved.sourceRevision !== sourceRevision || !reply || reply.dispatch_status !== 'accepted' || reply.source_revision !== sourceRevision || reply.payload !== bridgeSendReviewText(saved.preview))
      throw new Error('Accepted BridgeSend review unavailable');
    this.validateConfirmationReplyTarget(eventId, reply);
    for (const item of this.listOwnerBridgeSendActionsForThread(eventId)) {
      const metadata = JSON.parse(item.result_json as string);
      if (item.source_event_id !== eventId && ['send'].includes(metadata.args?.operation) && metadata.args.proposal?.action_id === action.id)
        throw new Error('BridgeSend review already consumed');
    }
    return bridgeSendArgsSchema.parse({operation:saved.preview.review.operation,review:saved.preview.review,proposal:{event_id:prior.id as string,action_id:action.id as string,args_hash:action.payload_hash as string,accepted_dispatch_id:reply.dispatch_id as string,source_revision:sourceRevision}});
  }

  selectOwnerDocsendProposal(authority: ExplicitOwnerLoopAuthority, eventId: string, sourceRevision: string): OwnerWebReadArgs {
    const current = this.ownerReminderToolAdmission(authority, eventId, true);
    if (!clearRoutineYes(current.text))
      throw new Error('A clear subsequent yes is required');
    const prior = this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=(SELECT thread_key FROM inbound_events WHERE id=?) AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1', eventId, eventId);
    if (!prior || prior.outcome !== 'resolved')
      throw new Error('Accepted Docsend review unavailable');
    const action = this.listOwnerWebReadActionsForThread(eventId).find(a => a.source_event_id === prior.id);
    if (!action)
      throw new Error('Accepted Docsend review unavailable');
    const proposalMetadata = JSON.parse(action.result_json as string);
    if (proposalMetadata.next_event_id !== eventId || proposalMetadata.next_event_accepted !== true)
      throw new Error('Docsend review is not the current accepted question');
    const saved = this.readOwnerWebReadAction(action.id as string, authority);
    const reply = this.getReplyForEvent(prior.id as string);
    if (!saved.preview || saved.sourceRevision !== sourceRevision || !reply || reply.dispatch_status !== 'accepted' || reply.source_revision !== sourceRevision || reply.payload !== docsendReviewText(saved.preview))
      throw new Error('Accepted Docsend review unavailable');
    this.validateConfirmationReplyTarget(eventId, reply);
    for (const item of this.listOwnerWebReadActionsForThread(eventId)) {
      const metadata = JSON.parse(item.result_json as string);
      if (item.source_event_id !== eventId && metadata.args?.operation==='confirm_docsend' && metadata.args.proposal?.action_id === action.id)
        throw new Error('Docsend review already consumed');
    }
    return ownerWebReadArgsSchema.parse({operation:'confirm_docsend',review:saved.preview.review,proposal:{event_id:prior.id as string,action_id:action.id as string,args_hash:action.payload_hash as string,accepted_dispatch_id:reply.dispatch_id as string,source_revision:sourceRevision}});
  }

  getReplyForEvent(eventId: string): Row | undefined {
    return this.one("SELECT p.*,d.status AS dispatch_status FROM prepared_replies p JOIN dispatches d ON d.id=p.dispatch_id WHERE p.event_id=? ORDER BY p.prepared_at DESC,p.id DESC LIMIT 1", eventId);
  }
  listRepliesForThread(eventId: string): Row[] {
    const event = this.getInbound(eventId);
    if (!event) throw new Error('inbound event missing');
    return this.db.prepare("SELECT p.*,d.status AS dispatch_status,d.provider_message_id,d.updated_at AS dispatch_updated_at FROM prepared_replies p JOIN runs r ON r.id=p.run_id JOIN dispatches d ON d.id=p.dispatch_id WHERE r.thread_key=? ORDER BY p.prepared_at,p.id")
      .all(event.thread_key as string) as Row[];
  }
  /** Reuse the action timestamp to bound delayed inspector calls across replacement. */
  markDelayedActionInspection(actionId: string): void {
    const changed = this.db.prepare(`UPDATE actions SET updated_at=? WHERE id=? AND status='unknown' AND kind IN (${DELAYED_ACTION_KINDS.map(() => '?').join(',')})`)
      .run(now(), actionId, ...DELAYED_ACTION_KINDS).changes;
    if (changed !== 1) throw new Error('delayed action is not inspectable');
  }
  /** Exact accepted local conversation context; never searches provider history or latest replies. */
  acceptedEditContext(eventId: string, sessionStartedAt: number): {text:string;sourceRevision:string} | undefined {
    const event = this.getInbound(eventId);
    if (!event) return;
    const current = JSON.parse(event.payload as string);
    if (!oneOffShorteningRequest(current.inbound.text ?? '') || current.inbound.replyBound || current.inbound.media?.length) return;
    const prior = this.one('SELECT id,outcome FROM inbound_events WHERE thread_key=? AND rowid<(SELECT rowid FROM inbound_events WHERE id=?) ORDER BY rowid DESC LIMIT 1', event.thread_key as string, eventId);
    if (!prior || prior.outcome !== 'resolved') return;
    if (this.conversationExclusionCheck(current.context.threadId)(prior.id as string)) return;
    const reply = this.getReplyForEvent(prior.id as string);
    if (!reply || reply.dispatch_status !== 'accepted' || typeof reply.source_revision !== 'string' ||
        typeof reply.payload !== 'string' || !reply.payload || reply.payload.length > 16000 ||
        Number(reply.prepared_at) < sessionStartedAt || Number(reply.prepared_at) > Number(event.received_at)) return;
    const dispatch = this.getDispatch(reply.dispatch_id as string);
    if (dispatch?.status !== 'accepted' || dispatch.destination !== current.context.destination ||
        Number(dispatch.updated_at) > Number(event.received_at)) return;
    // Only the last text the owner saw there is the edit target. A later accepted send supersedes the reply:
    // it is the target only when it is the source-bound background message, else the owner is asked.
    const later = this.db.prepare("SELECT id,updated_at FROM dispatches WHERE destination=? AND status='accepted' AND id<>? AND updated_at>=? AND updated_at<=? ORDER BY updated_at DESC")
      .all(dispatch.destination as string, dispatch.id as string, Number(dispatch.updated_at), Number(event.received_at));
    if (later.length) {
      const background = this.acceptedBackgroundMessage(eventId, sessionStartedAt);
      const latest = later.filter(row => row.updated_at === later[0]!.updated_at);
      return background && latest.length === 1 && latest[0]!.id === background.dispatchId && background.text.length <= 16000
        ? {text:background.text, sourceRevision:background.sourceRevision} : undefined;
    }
    return {text:reply.payload, sourceRevision:reply.source_revision};
  }

  quotedReplyForEvent(eventId: string, providerId: string):
      {status: 'found'; messageId: string; text: string; sourceRevision: string; acceptedDispatchId:string} | {status: 'unavailable'} {
    const event = this.getInbound(eventId);
    if (!event || !providerId) return {status: 'unavailable'};
    const rows = this.db.prepare(`
      SELECT p.*, d.provider_message_id, d.provider_message_ids, d.updated_at
      FROM prepared_replies p JOIN runs r ON r.id=p.run_id JOIN dispatches d ON d.id=p.dispatch_id
      WHERE r.thread_key=? AND d.kind='interactive' AND d.status='accepted'
    `).all(event.thread_key as string);
    const matches = rows.filter(row => {
      let ids: unknown = [];
      try { ids = typeof row.provider_message_ids === 'string' ? JSON.parse(row.provider_message_ids) : []; }
      catch { return false; }
      return row.provider_message_id === providerId || Array.isArray(ids) && ids.includes(providerId);
    });
    if (matches.length !== 1) return {status: 'unavailable'};
    const row = matches[0]!;
    if (typeof row.payload !== 'string' || !row.payload || row.payload.length > 16000 ||
        typeof row.source_revision !== 'string' || Number(row.prepared_at) > Number(event.received_at) ||
        Number(row.updated_at) > Number(event.received_at)) return {status: 'unavailable'};
    return {status: 'found', messageId: providerId, text: row.payload, sourceRevision: row.source_revision,acceptedDispatchId:row.dispatch_id as string};
  }

  listRecoverableReplies(): Row[] {
    return this.db.prepare("SELECT p.*,d.status AS dispatch_status,d.kind,d.destination,d.payload_hash FROM prepared_replies p JOIN dispatches d ON d.id=p.dispatch_id WHERE d.status IN ('prepared','unknown') ORDER BY p.prepared_at,p.id").all() as Row[];
  }
  listRecoverableDispatches(): Row[] {
    return this.db.prepare("SELECT * FROM dispatches WHERE status IN ('prepared','unknown') ORDER BY created_at,id").all() as Row[];
  }
  getAttempts(dispatchId: string): Row[] {
    return this.db.prepare('SELECT * FROM dispatch_attempts WHERE dispatch_id=? ORDER BY number').all(dispatchId) as Row[];
  }

  cancelPreparedDispatch(dispatchId: string): boolean {
    return this.transaction(() => Number(this.db.prepare("UPDATE dispatches SET status='cancelled',updated_at=? WHERE id=? AND status='prepared'")
      .run(now(), dispatchId).changes) === 1);
  }

  private newAttempt(dispatchId: string, allowed: string[], requestedNumber?: number): Row {
    const dispatch = this.getDispatch(dispatchId);
    if (!dispatch || !allowed.includes(dispatch.status as string)) throw new Error('dispatch is not eligible');
    if ((dispatch.current_attempt as number) >= (dispatch.max_attempts as number)) throw new Error('dispatch attempt limit reached');
    const reply = this.one('SELECT run_id FROM prepared_replies WHERE dispatch_id=?', dispatchId);
    if (reply && dispatch.status === 'prepared' && !this.isRunGenerationCurrent(reply.run_id as string)) {
      throw new Error('prepared reply belongs to a stale run');
    }
    // Python charges an outbox attempt before making HTTP contact. A crash in between
    // leaves a gap here; preserve its attempt identity without inventing provider calls.
    const next = (dispatch.current_attempt as number) + 1;
    const number = requestedNumber ?? next;
    if (!Number.isSafeInteger(number) || number < next || number > (dispatch.max_attempts as number) ||
        (dispatch.kind !== 'scheduled_text' && number !== next)) throw new Error('dispatch attempt number is invalid');
    const timestamp = now();
    this.db.prepare("INSERT INTO dispatch_attempts(dispatch_id,number,status,started_at) VALUES(?,?,'dispatching',?)")
      .run(dispatchId, number, timestamp);
    this.db.prepare("UPDATE dispatches SET status='dispatching',current_attempt=?,updated_at=? WHERE id=?")
      .run(number, timestamp, dispatchId);
    return this.one('SELECT * FROM dispatch_attempts WHERE dispatch_id=? AND number=?', dispatchId, number)!;
  }

  beginDispatch(dispatchId: string, requestedNumber?: number): Row {
    return this.transaction(() => this.newAttempt(dispatchId, ['prepared'], requestedNumber));
  }

  /** Only a definite rejection may receive another automatic text attempt.
   * An unknown outcome may already have reached the provider; it remains held
   * for receipt reconciliation, including across process replacement.
   */
  retryScheduledText(dispatchId: string, requestedNumber?: number): Row {
    return this.transaction(() => {
      const dispatch = this.getDispatch(dispatchId);
      if (dispatch?.kind !== 'scheduled_text') throw new Error('automatic retry forbidden for this dispatch kind');
      if (this.hasUnresolvedDispatchAttempt(dispatchId)) throw new Error('earlier provider outcome is unresolved');
      return this.newAttempt(dispatchId, ['rejected'], requestedNumber);
    });
  }

  hasUnresolvedDispatchAttempt(dispatchId: string): boolean {
    return this.one("SELECT 1 AS unresolved FROM dispatch_attempts WHERE dispatch_id=? AND status IN ('unknown','dispatching') LIMIT 1", dispatchId) !== undefined;
  }

  settleDispatch(dispatchId: string, attemptNo: number, outcome: DispatchOutcome, messageId?: string, messageIds?: string[], providerSentAtMs?: number): Row {
    if (!['accepted', 'rejected', 'unknown'].includes(outcome)) throw new Error('invalid dispatch outcome');
    if (outcome === 'accepted') required(messageId!, 'provider message id');
    if (messageIds !== undefined && (!Array.isArray(messageIds) || messageIds.some(id => typeof id !== 'string' || !id))) {
      throw new Error('invalid provider message ids');
    }
    return this.transaction(() => {
      const dispatch = this.getDispatch(dispatchId);
      const attempt = this.one('SELECT * FROM dispatch_attempts WHERE dispatch_id=? AND number=?', dispatchId, attemptNo);
      if (!dispatch || !attempt) throw new Error('dispatch attempt missing');
      if (attempt.status === outcome && (outcome !== 'accepted' || attempt.provider_message_id === messageId)) return dispatch;
      if (attempt.status !== 'dispatching' && !(attempt.status === 'unknown' && outcome === 'accepted')) {
        throw new Error('dispatch attempt already settled');
      }
      if (dispatch.status === 'accepted' && outcome !== 'accepted') throw new Error('accepted dispatch cannot be changed');
      const timestamp = now();
      this.db.prepare('UPDATE dispatch_attempts SET status=?,provider_message_id=?,finished_at=? WHERE dispatch_id=? AND number=?')
        .run(outcome, outcome === 'accepted' ? messageId! : null, timestamp, dispatchId, attemptNo);
      if (dispatch.status === 'accepted') return this.getDispatch(dispatchId)!;
      // Late proof for an old uncertain attempt may settle the whole operation after a text retry.
      if (outcome === 'accepted' || dispatch.current_attempt === attemptNo) {
        // A late proof for attempt N can arrive after text retry N+1 began. It proves
        // acceptance, but the newer contact is still ambiguous and must remain inspectable.
        if (outcome === 'accepted' && dispatch.current_attempt !== attemptNo) {
          this.db.prepare("UPDATE dispatch_attempts SET status='unknown',finished_at=? WHERE dispatch_id=? AND status='dispatching'")
            .run(timestamp, dispatchId);
        }
        this.db.prepare('UPDATE dispatches SET status=?,provider_message_id=?,provider_message_ids=?,updated_at=? WHERE id=?')
          .run(outcome, outcome === 'accepted' ? messageId! : null,
            outcome === 'accepted' ? JSON.stringify(messageIds ?? [messageId!])
              : outcome === 'unknown' && messageIds?.length ? JSON.stringify(messageIds) : null, timestamp, dispatchId);
      }
      if (outcome === 'accepted' && (messageIds ?? [messageId]).length === 1
          && Number.isSafeInteger(providerSentAtMs) && providerSentAtMs! >= 946684800000 && providerSentAtMs! <= 4102444800000)
        this.db.prepare('UPDATE dispatches SET provider_sent_at_ms=? WHERE id=? AND provider_sent_at_ms IS NULL').run(providerSentAtMs!, dispatchId);
      return this.getDispatch(dispatchId)!;
    });
  }

  /** Consequential actions are separately journaled. The caller still applies authorization. */
  recordActionIntent(input: ActionInput): Row {
    for (const key of ['actionId', 'runId', 'kind', 'payloadHash'] as const) required(input[key], key);
    if (['owner_loop_add_admitted', 'owner_loop_close_admitted', 'owner_reminder_admitted', 'owner_quiet_admitted', 'owner_reminder_tool', 'owner_loop_tool', 'owner_preference_tool', 'owner_routine_tool','owner_gmail_draft_tool','owner_calendar_tool','owner_contacts_tool','owner_bridge_send_tool','owner_brief_request','owner_meeting_prep_request','owner_meeting_prep_offer','owner_offer_reply','owner_skill_request','owner_web_read'].includes(input.kind)) {
      throw new Error('owner command requires admitted-event proof');
    }
    return this.transaction(() => {
      const existing = this.getAction(input.actionId);
      if (existing) {
        const oldRun = this.getRun(existing.run_id as string);
        const newRun = this.getRun(input.runId);
        if (!oldRun || !newRun || oldRun.thread_key !== newRun.thread_key ||
            existing.kind !== input.kind || existing.payload_hash !== input.payloadHash)
          throw new Error('action identity conflict');
        if (!this.isRunCurrent(input.runId)) throw new Error('run is stale');
        if (existing.status === 'pending' && existing.run_id !== input.runId) {
          // No provider contact started; the replacement run may own this intent.
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(input.runId, now(), input.actionId);
          return this.getAction(input.actionId)!;
        }
        // Executing/unknown/succeeded results stay tied to original proof. The replacement
        // can inspect them but cannot launch them again.
        return existing;
      }
      if (!this.isRunCurrent(input.runId)) throw new Error('run is stale');
      const timestamp = now();
      this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,status,created_at,updated_at) VALUES(?,?,?,?,'pending',?,?)")
        .run(input.actionId, input.runId, input.kind, input.payloadHash, timestamp, timestamp);
      return this.getAction(input.actionId)!;
    });
  }

  /** Fixture-only explicit command proof. No model selector, receiver call, or UX is mounted. */
  prepareExplicitOwnerLoopIntent(authority: ExplicitOwnerLoopAuthority): ExplicitOwnerLoopIntent {
    for (const field of ['runId', 'accountId', 'ownerId', 'lineKey', 'channel'] as const) {
      required(authority[field], field);
    }
    if (!['telegram', 'photon'].includes(authority.channel)) throw new Error('owner command authority is invalid');
    return this.transaction(() => {
      if (!this.isRunCurrent(authority.runId)) throw new Error('run is stale');
      const run = this.getRun(authority.runId)!;
      if (typeof run.admitted_through !== 'number' || run.admitted_through < 1) {
        throw new Error('run admission boundary is unavailable');
      }
      const events = this.db.prepare("SELECT * FROM inbound_events WHERE thread_key=? AND outcome='pending' AND rowid<=? ORDER BY rowid")
        .all(run.thread_key as string, run.admitted_through) as Row[];
      const commands: { event: Row; text: string; textHash: string; threadId: string }[] = [];
      for (const event of events) {
        const payload = event.payload;
        if (typeof payload !== 'string' || hash(payload) !== event.payload_hash) {
          throw new Error('admitted owner command payload is invalid');
        }
        let saved: unknown;
        try { saved = JSON.parse(payload); } catch { throw new Error('admitted owner command payload is invalid'); }
        if (!saved || typeof saved !== 'object' || Array.isArray(saved) || JSON.stringify(saved) !== payload) {
          throw new Error('admitted owner command payload is invalid');
        }
        const record = saved as Record<string, unknown>;
        const inbound = record.inbound as Record<string, unknown> | undefined;
        const context = record.context as Record<string, unknown> | undefined;
        if (!inbound || !context || typeof inbound !== 'object' || typeof context !== 'object' ||
            inbound.eventId !== event.source_id || inbound.accountId !== event.account_key ||
            inbound.senderId !== event.sender || inbound.channel !== event.channel ||
            context.lineKey !== event.line_key || typeof context.threadId !== 'string' ||
            hash(JSON.stringify([event.account_key, event.line_key, event.channel, context.threadId])) !== event.thread_key) {
          throw new Error('admitted owner command identity is invalid');
        }
        if (typeof inbound.text === 'string' && inbound.text.startsWith('/loop close ')) {
          throw new Error('exactly one explicit owner command is required');
        }
        if (typeof inbound.text !== 'string' || !inbound.text.startsWith('/loop add ')) continue;
        const text = inbound.text.slice('/loop add '.length);
        if (!text || text.trim() !== text || Array.from(text).length > 500 ||
            /[\u0000-\u001f\u007f]/u.test(text) || Buffer.from(text, 'utf8').toString('utf8') !== text) {
          throw new Error('explicit owner command text is invalid');
        }
        if (event.account_key !== authority.accountId || event.line_key !== authority.lineKey ||
            event.channel !== authority.channel || event.sender !== authority.ownerId) {
          throw new Error('explicit owner command is not from the configured owner');
        }
        commands.push({ event, text, textHash: hash(text), threadId: context.threadId });
      }
      if (commands.length !== 1) throw new Error('exactly one explicit owner command is required');
      const { event, text, textHash, threadId } = commands[0]!;
      const eventId = event.id as string;
      const actionId = hash(JSON.stringify(['sotto.owner-loop-command.v1', eventId]));
      if (authority.scopeHash !== undefined && !/^[a-f0-9]{64}$/.test(authority.scopeHash)) {
        throw new Error('owner command scope is invalid');
      }
      const payloadHash = hash(JSON.stringify([event.payload_hash, textHash, authority.scopeHash ?? null]));
      const existing = this.getAction(actionId);
      if (existing) {
        const prior = this.getRun(existing.run_id as string);
        if (!prior || prior.thread_key !== run.thread_key || existing.kind !== 'owner_loop_add_admitted' ||
            existing.payload_hash !== payloadHash || existing.source_event_id !== eventId ||
            existing.source_payload_hash !== event.payload_hash || existing.source_text_hash !== textHash) {
          throw new Error('owner command action identity conflict');
        }
        if (existing.status === 'pending' && existing.run_id !== authority.runId) {
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?')
            .run(authority.runId, now(), actionId);
        }
      } else {
        const timestamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)")
          .run(actionId, authority.runId, 'owner_loop_add_admitted', payloadHash,
            eventId, event.payload_hash as string, textHash, timestamp, timestamp);
      }
      const action = this.getAction(actionId)!;
      let receiptHash: string | null = null;
      let zone: string | null = null;
      if (action.result_json !== null) {
        let metadata: unknown;
        try { metadata = JSON.parse(action.result_json as string); } catch { throw new Error('owner command metadata is invalid'); }
        if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata) ||
            ((metadata as Row).owner_zone !== undefined &&
              (typeof (metadata as Row).owner_zone !== 'string' ||
               !/^[A-Za-z0-9_+./:-]{1,64}$/.test((metadata as Row).owner_zone as string)))) {
          throw new Error('owner command metadata is invalid');
        }
        zone = (metadata as Row).owner_zone as string | undefined ?? null;
      }
      if (action.status === 'succeeded') {
        let receipt: unknown;
        try { receipt = JSON.parse(action.result_json as string); } catch { throw new Error('owner command receipt is invalid'); }
        if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt) ||
            ![1, 2].includes(Object.keys(receipt).length) || typeof (receipt as Row).receipt_hash !== 'string' ||
            !/^[a-f0-9]{64}$/.test((receipt as Row).receipt_hash as string)) {
          throw new Error('owner command receipt is invalid');
        }
        receiptHash = (receipt as Row).receipt_hash as string;
      }
      return { actionId, sourceEventId: eventId, text, status: action.status as string, receiptHash,
        sourcePayloadHash: event.payload_hash as string, admittedAtMs: event.received_at as number,
        providerThreadId: threadId, zone };
    });
  }

  /** Bind one literal close command to the captured owner event, never to model text. */
  prepareExplicitOwnerLoopCloseIntent(authority: ExplicitOwnerLoopAuthority): ExplicitOwnerLoopCloseIntent {
    for (const field of ['runId', 'accountId', 'ownerId', 'lineKey', 'channel'] as const) required(authority[field], field);
    if (!['telegram', 'photon'].includes(authority.channel)) throw new Error('owner command authority is invalid');
    return this.transaction(() => {
      if (!this.isRunCurrent(authority.runId)) throw new Error('run is stale');
      const run = this.getRun(authority.runId)!;
      if (typeof run.admitted_through !== 'number' || run.admitted_through < 1) {
        throw new Error('run admission boundary is unavailable');
      }
      const events = this.db.prepare("SELECT * FROM inbound_events WHERE thread_key=? AND outcome='pending' AND rowid<=? ORDER BY rowid")
        .all(run.thread_key as string, run.admitted_through) as Row[];
      const commands: { event: Row; text: string; anchor: string; threadId: string }[] = [];
      for (const event of events) {
        const payload = event.payload;
        if (typeof payload !== 'string' || hash(payload) !== event.payload_hash) throw new Error('admitted owner command payload is invalid');
        let saved: unknown;
        try { saved = JSON.parse(payload); } catch { throw new Error('admitted owner command payload is invalid'); }
        if (!saved || typeof saved !== 'object' || Array.isArray(saved) || JSON.stringify(saved) !== payload) {
          throw new Error('admitted owner command payload is invalid');
        }
        const record = saved as Record<string, unknown>;
        const inbound = record.inbound as Record<string, unknown> | undefined;
        const context = record.context as Record<string, unknown> | undefined;
        if (!inbound || !context || typeof inbound !== 'object' || typeof context !== 'object' ||
            inbound.eventId !== event.source_id || inbound.accountId !== event.account_key ||
            inbound.senderId !== event.sender || inbound.channel !== event.channel ||
            context.lineKey !== event.line_key || typeof context.threadId !== 'string' ||
            hash(JSON.stringify([event.account_key, event.line_key, event.channel, context.threadId])) !== event.thread_key) {
          throw new Error('admitted owner command identity is invalid');
        }
        if (typeof inbound.text !== 'string' ||
            (!inbound.text.startsWith('/loop add ') && !inbound.text.startsWith('/loop close '))) continue;
        if (!inbound.text.startsWith('/loop close ')) throw new Error('exactly one explicit owner command is required');
        const anchor = inbound.text.slice('/loop close '.length);
        if (!anchor || anchor.trim() !== anchor || Array.from(anchor).length > 256 ||
            /[\u0000-\u001f\u007f\ud800-\udfff]/u.test(anchor) ||
            Buffer.from(anchor, 'utf8').toString('utf8') !== anchor) {
          throw new Error('explicit owner command anchor is invalid');
        }
        if (event.account_key !== authority.accountId || event.line_key !== authority.lineKey ||
            event.channel !== authority.channel || event.sender !== authority.ownerId) {
          throw new Error('explicit owner command is not from the configured owner');
        }
        commands.push({ event, text: inbound.text, anchor, threadId: context.threadId });
      }
      if (commands.length !== 1) throw new Error('exactly one explicit owner command is required');
      const { event, text, anchor, threadId } = commands[0]!;
      const eventId = event.id as string;
      const actionId = hash(JSON.stringify(['sotto.owner-loop-close-command.v1', eventId]));
      if (authority.scopeHash !== undefined && !/^[a-f0-9]{64}$/.test(authority.scopeHash)) {
        throw new Error('owner command scope is invalid');
      }
      const textHash = hash(text);
      const payloadHash = hash(JSON.stringify([event.payload_hash, textHash, authority.scopeHash ?? null]));
      const existing = this.getAction(actionId);
      if (existing) {
        const prior = this.getRun(existing.run_id as string);
        if (!prior || prior.thread_key !== run.thread_key || existing.kind !== 'owner_loop_close_admitted' ||
            existing.payload_hash !== payloadHash || existing.source_event_id !== eventId ||
            existing.source_payload_hash !== event.payload_hash || existing.source_text_hash !== textHash) {
          throw new Error('owner command action identity conflict');
        }
        if (existing.status === 'pending' && existing.run_id !== authority.runId) {
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), actionId);
        }
      } else {
        const timestamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)")
          .run(actionId, authority.runId, 'owner_loop_close_admitted', payloadHash,
            eventId, event.payload_hash as string, textHash, timestamp, timestamp);
      }
      const action = this.getAction(actionId)!;
      let zone: string | null = null, expectedRowDigest: string | null = null, receiptHash: string | null = null;
      if (action.result_json !== null) {
        let metadata: unknown;
        try { metadata = JSON.parse(action.result_json as string); } catch { throw new Error('owner command metadata is invalid'); }
        if (!metadata || typeof metadata !== 'object' || Array.isArray(metadata)) throw new Error('owner command metadata is invalid');
        const saved = metadata as Row;
        if (action.status === 'rejected' && Object.keys(saved).length === 1 &&
            typeof saved.code === 'string' && ['owner_close_target_unavailable', 'owner_close_target_changed',
              'owner_close_event_conflict'].includes(saved.code)) {
          return { actionId, sourceEventId: eventId, text, anchorKey: anchor, status: 'rejected', receiptHash,
            sourcePayloadHash: event.payload_hash as string, admittedAtMs: event.received_at as number,
            providerThreadId: threadId, zone, expectedRowDigest };
        }
        if (typeof saved.owner_zone !== 'string' || !/^[A-Za-z0-9_+./:-]{1,64}$/.test(saved.owner_zone) ||
            typeof saved.expected_row_digest !== 'string' || !/^[a-f0-9]{64}$/.test(saved.expected_row_digest)) {
          throw new Error('owner command metadata is invalid');
        }
        zone = saved.owner_zone;
        expectedRowDigest = saved.expected_row_digest;
        if (action.status === 'succeeded') {
          if (typeof saved.receipt_hash !== 'string' || !/^[a-f0-9]{64}$/.test(saved.receipt_hash) ||
              Object.keys(saved).length !== 3) throw new Error('owner command receipt is invalid');
          receiptHash = saved.receipt_hash;
        } else if (action.status === 'rejected') {
          if (typeof saved.code !== 'string' || !['owner_close_target_unavailable', 'owner_close_target_changed',
            'owner_close_event_conflict'].includes(saved.code) || Object.keys(saved).length !== 3) {
            throw new Error('owner command refusal is invalid');
          }
        } else if (Object.keys(saved).length !== 2) throw new Error('owner command metadata is invalid');
      }
      return { actionId, sourceEventId: eventId, text, anchorKey: anchor, status: action.status as string,
        receiptHash, sourcePayloadHash: event.payload_hash as string, admittedAtMs: event.received_at as number,
        providerThreadId: threadId, zone, expectedRowDigest };
    });
  }

  bindExplicitOwnerLoopZone(actionId: string, zone: string): void {
    if (!/^[A-Za-z0-9_+./:-]{1,64}$/.test(zone)) throw new Error('owner timezone is invalid');
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_loop_add_admitted' || action.status !== 'pending' ||
          !this.isRunCurrent(action.run_id as string)) throw new Error('owner command action is not eligible');
      const previous = action.result_json === null ? null : JSON.parse(action.result_json as string) as Row;
      if (previous && previous.owner_zone !== zone) throw new Error('owner timezone changed');
      this.db.prepare('UPDATE actions SET result_json=?,updated_at=? WHERE id=?')
        .run(JSON.stringify({ owner_zone: zone }), now(), actionId);
    });
  }

  /** One durable transaction records both receiver-owned pre-effect facts. */
  bindExplicitOwnerLoopClosePrepare(actionId: string, zone: string, expectedRowDigest: string): void {
    if (!/^[A-Za-z0-9_+./:-]{1,64}$/.test(zone) || !/^[a-f0-9]{64}$/.test(expectedRowDigest)) {
      throw new Error('owner close prepare proof is invalid');
    }
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_loop_close_admitted' || action.status !== 'pending' ||
          !this.isRunCurrent(action.run_id as string)) throw new Error('owner command action is not eligible');
      const prior = action.result_json === null ? null : JSON.parse(action.result_json as string) as Row;
      if (prior && (prior.owner_zone !== zone || prior.expected_row_digest !== expectedRowDigest ||
                    Object.keys(prior).length !== 2)) throw new Error('owner close prepare proof changed');
      this.db.prepare('UPDATE actions SET result_json=?,updated_at=? WHERE id=?')
        .run(JSON.stringify({ owner_zone: zone, expected_row_digest: expectedRowDigest }), now(), actionId);
    });
  }

  private ownerCloseMetadata(action: Row): { owner_zone: string; expected_row_digest: string } {
    if (typeof action.result_json !== 'string') throw new Error('owner close prepare proof is missing');
    let saved: unknown;
    try { saved = JSON.parse(action.result_json); } catch { throw new Error('owner close prepare proof is invalid'); }
    if (!saved || typeof saved !== 'object' || Array.isArray(saved) || Object.keys(saved).length !== 2 ||
        typeof (saved as Row).owner_zone !== 'string' ||
        !/^[A-Za-z0-9_+./:-]{1,64}$/.test((saved as Row).owner_zone as string) ||
        typeof (saved as Row).expected_row_digest !== 'string' ||
        !/^[a-f0-9]{64}$/.test((saved as Row).expected_row_digest as string)) {
      throw new Error('owner close prepare proof is invalid');
    }
    return { owner_zone: (saved as Row).owner_zone as string,
      expected_row_digest: (saved as Row).expected_row_digest as string };
  }

  settleExplicitOwnerLoopCloseIntent(actionId: string, receiptHash: string): void {
    if (!/^[a-f0-9]{64}$/.test(receiptHash)) throw new Error('owner command receipt is invalid');
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_loop_close_admitted' || action.status !== 'executing') {
        throw new Error('owner command action is not eligible');
      }
      const metadata = this.ownerCloseMetadata(action);
      this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
        .run(JSON.stringify({ receipt_hash: receiptHash, ...metadata }), now(), actionId);
    });
  }

  rejectExplicitOwnerLoopCloseIntent(actionId: string,
    code: 'owner_close_target_unavailable' | 'owner_close_target_changed' | 'owner_close_event_conflict'): void {
    if (!['owner_close_target_unavailable', 'owner_close_target_changed', 'owner_close_event_conflict'].includes(code)) {
      throw new Error('owner command refusal is invalid');
    }
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_loop_close_admitted' ||
          !['pending', 'executing'].includes(action.status as string) ||
          (action.status === 'pending' && !this.isRunCurrent(action.run_id as string))) {
        throw new Error('owner command action is not eligible');
      }
      const metadata = action.status === 'executing' ? this.ownerCloseMetadata(action) : {};
      this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
        .run(JSON.stringify({ code, ...metadata }), now(), actionId);
    });
  }

  reconcileExplicitOwnerLoopCloseIntent(actionId: string, receiptHash: string): void {
    if (!/^[a-f0-9]{64}$/.test(receiptHash)) throw new Error('owner command receipt is invalid');
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_loop_close_admitted' || action.status !== 'unknown') {
        throw new Error('owner command action is not eligible');
      }
      const metadata = this.ownerCloseMetadata(action);
      this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
        .run(JSON.stringify({ receipt_hash: receiptHash, ...metadata }), now(), actionId);
    });
  }

  /** Persist only a digest of the effect receipt; a future writer must prove it first. */
  settleExplicitOwnerLoopIntent(actionId: string, receiptHash: string): void {
    if (!/^[a-f0-9]{64}$/.test(receiptHash)) throw new Error('owner command receipt is invalid');
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_loop_add_admitted' || action.status !== 'executing') {
        throw new Error('owner command action is not eligible');
      }
      this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
        .run(JSON.stringify({ receipt_hash: receiptHash, ...this.ownerZoneMetadata(action) }), now(), actionId);
    });
  }

  /** Only a trusted writer's typed pre-effect conflict may close an executing intent as rejected. */
  rejectExplicitOwnerLoopIntent(actionId: string, code: 'owner_event_conflict' | 'owner_anchor_collision'): void {
    if (code !== 'owner_event_conflict' && code !== 'owner_anchor_collision') {
      throw new Error('owner command refusal is invalid');
    }
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_loop_add_admitted' || action.status !== 'executing') {
        throw new Error('owner command action is not eligible');
      }
      this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
        .run(JSON.stringify({ code, ...this.ownerZoneMetadata(action) }), now(), actionId);
    });
  }

  /** A current canonical read proves an interrupted write without launching it again. */
  reconcileExplicitOwnerLoopIntent(actionId: string, receiptHash: string): void {
    if (!/^[a-f0-9]{64}$/.test(receiptHash)) throw new Error('owner command receipt is invalid');
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_loop_add_admitted' || action.status !== 'unknown') {
        throw new Error('owner command action is not eligible');
      }
      this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
        .run(JSON.stringify({ receipt_hash: receiptHash, ...this.ownerZoneMetadata(action) }), now(), actionId);
    });
  }

  private ownerZoneMetadata(action: Row): { owner_zone?: string } {
    if (action.result_json === null) return {};
    const metadata = JSON.parse(action.result_json as string) as Row;
    return typeof metadata.owner_zone === 'string' ? { owner_zone: metadata.owner_zone } : {};
  }

  readUnknownExplicitOwnerLoopIntent(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>):
      ExplicitOwnerLoopIntent | null {
    const action = this.getAction(actionId);
    if (!action || action.kind !== 'owner_loop_add_admitted' || action.status !== 'unknown' ||
        !authority.scopeHash || !/^[a-f0-9]{64}$/.test(authority.scopeHash)) return null;
    const event = this.getInbound(action.source_event_id as string);
    if (!event || event.account_key !== authority.accountId || event.line_key !== authority.lineKey ||
        event.channel !== authority.channel || event.sender !== authority.ownerId ||
        event.payload_hash !== action.source_payload_hash || typeof event.payload !== 'string' ||
        hash(event.payload) !== event.payload_hash) return null;
    let saved: unknown;
    try { saved = JSON.parse(event.payload as string); } catch { return null; }
    if (!saved || typeof saved !== 'object' || Array.isArray(saved) || JSON.stringify(saved) !== event.payload) return null;
    const record = saved as Record<string, unknown>;
    const inbound = record.inbound as Record<string, unknown> | undefined;
    const context = record.context as Record<string, unknown> | undefined;
    if (!inbound || !context || inbound.eventId !== event.source_id ||
        inbound.accountId !== event.account_key || inbound.senderId !== event.sender ||
        inbound.channel !== event.channel || context.lineKey !== event.line_key ||
        typeof context.threadId !== 'string' ||
        hash(JSON.stringify([event.account_key, event.line_key, event.channel, context.threadId])) !== event.thread_key ||
        typeof inbound.text !== 'string' || !inbound.text.startsWith('/loop add ')) return null;
    const text = inbound.text.slice('/loop add '.length);
    if (!text || hash(text) !== action.source_text_hash ||
        hash(JSON.stringify([event.payload_hash, hash(text), authority.scopeHash])) !== action.payload_hash ||
        action.id !== hash(JSON.stringify(['sotto.owner-loop-command.v1', event.id]))) return null;
    let metadata: unknown;
    try { metadata = JSON.parse(action.result_json as string); } catch { return null; }
    const zone = metadata && typeof metadata === 'object' && !Array.isArray(metadata)
      ? (metadata as Row).owner_zone : undefined;
    if (typeof zone !== 'string' || !/^[A-Za-z0-9_+./:-]{1,64}$/.test(zone)) return null;
    return { actionId, sourceEventId: event.id as string, text, status: 'unknown', receiptHash: null,
      sourcePayloadHash: event.payload_hash as string, admittedAtMs: event.received_at as number,
      providerThreadId: context.threadId, zone };
  }

  readUnknownExplicitOwnerLoopCloseIntent(actionId: string, authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>):
      ExplicitOwnerLoopCloseIntent | null {
    const action = this.getAction(actionId);
    if (!action || action.kind !== 'owner_loop_close_admitted' || action.status !== 'unknown' ||
        !authority.scopeHash || !/^[a-f0-9]{64}$/.test(authority.scopeHash)) return null;
    const event = this.getInbound(action.source_event_id as string);
    if (!event || event.account_key !== authority.accountId || event.line_key !== authority.lineKey ||
        event.channel !== authority.channel || event.sender !== authority.ownerId ||
        event.payload_hash !== action.source_payload_hash || typeof event.payload !== 'string' ||
        hash(event.payload) !== event.payload_hash) return null;
    let saved: unknown;
    try { saved = JSON.parse(event.payload as string); } catch { return null; }
    if (!saved || typeof saved !== 'object' || Array.isArray(saved) || JSON.stringify(saved) !== event.payload) return null;
    const record = saved as Record<string, unknown>;
    const inbound = record.inbound as Record<string, unknown> | undefined;
    const context = record.context as Record<string, unknown> | undefined;
    if (!inbound || !context || inbound.eventId !== event.source_id ||
        inbound.accountId !== event.account_key || inbound.senderId !== event.sender ||
        inbound.channel !== event.channel || context.lineKey !== event.line_key ||
        typeof context.threadId !== 'string' ||
        hash(JSON.stringify([event.account_key, event.line_key, event.channel, context.threadId])) !== event.thread_key ||
        typeof inbound.text !== 'string' || !inbound.text.startsWith('/loop close ')) return null;
    const text = inbound.text;
    const anchor = text.slice('/loop close '.length);
    if (!anchor || anchor.trim() !== anchor || Array.from(anchor).length > 256 ||
        /[\u0000-\u001f\u007f\ud800-\udfff]/u.test(anchor) ||
        Buffer.from(anchor, 'utf8').toString('utf8') !== anchor ||
        hash(text) !== action.source_text_hash ||
        hash(JSON.stringify([event.payload_hash, hash(text), authority.scopeHash])) !== action.payload_hash ||
        action.id !== hash(JSON.stringify(['sotto.owner-loop-close-command.v1', event.id]))) return null;
    let metadata: { owner_zone: string; expected_row_digest: string };
    try { metadata = this.ownerCloseMetadata(action); } catch { return null; }
    return { actionId, sourceEventId: event.id as string, text, anchorKey: anchor,
      status: 'unknown', receiptHash: null, sourcePayloadHash: event.payload_hash as string,
      admittedAtMs: event.received_at as number, providerThreadId: context.threadId,
      zone: metadata.owner_zone, expectedRowDigest: metadata.expected_row_digest };
  }

  getAction(actionId: string): Row | undefined { return this.one('SELECT * FROM actions WHERE id=?', actionId); }

  /** One literal quiet command, bound to the exact admitted owner event and private scope. */
  prepareExplicitOwnerQuietIntent(authority: ExplicitOwnerLoopAuthority): ExplicitOwnerQuietIntent {
    return this.transaction(() => {
      const run = this.getRun(authority.runId);
      if (!run || !this.isRunCurrent(authority.runId) || !/^[a-f0-9]{64}$/.test(authority.scopeHash ?? '')) {
        throw new Error('owner quiet authority is invalid');
      }
      const event = this.db.prepare('SELECT e.* FROM inbound_events e WHERE e.id=? AND e.rowid<=?')
        .get(run.event_id as string, run.admitted_through as number) as Row | undefined;
      if (!event || event.outcome !== 'pending' || event.thread_key !== run.thread_key ||
          event.account_key !== authority.accountId || event.line_key !== authority.lineKey ||
          event.channel !== authority.channel || event.sender !== authority.ownerId ||
          typeof event.payload !== 'string' || hash(event.payload) !== event.payload_hash) {
        throw new Error('owner quiet event is invalid');
      }
      let saved: unknown;
      try { saved = JSON.parse(event.payload); } catch { throw new Error('owner quiet payload is invalid'); }
      if (!saved || typeof saved !== 'object' || Array.isArray(saved) || JSON.stringify(saved) !== event.payload) {
        throw new Error('owner quiet payload is invalid');
      }
      const record = saved as Record<string, unknown>;
      const inbound = record.inbound as Record<string, unknown> | undefined;
      const context = record.context as Record<string, unknown> | undefined;
      if (!inbound || !context || inbound.eventId !== event.source_id || inbound.accountId !== event.account_key ||
          inbound.senderId !== event.sender || inbound.channel !== event.channel ||
          context.lineKey !== event.line_key || typeof context.threadId !== 'string' ||
          hash(JSON.stringify([event.account_key, event.line_key, event.channel, context.threadId])) !== event.thread_key ||
          typeof inbound.text !== 'string' || !parseOwnerQuietCommand(inbound.text)) {
        throw new Error('owner quiet identity is invalid');
      }
      const actionId = hash(JSON.stringify(['sotto.owner-quiet-command.v1', event.id]));
      const textHash = hash(inbound.text);
      const payloadHash = hash(JSON.stringify([event.payload_hash, textHash, authority.scopeHash]));
      const existing = this.getAction(actionId);
      if (existing) {
        const oldRun = this.getRun(existing.run_id as string);
        if (!oldRun || oldRun.thread_key !== run.thread_key || existing.kind !== 'owner_quiet_admitted' ||
            existing.payload_hash !== payloadHash || existing.source_event_id !== event.id ||
            existing.source_payload_hash !== event.payload_hash || existing.source_text_hash !== textHash) {
          throw new Error('owner quiet action identity conflict');
        }
        if (existing.status === 'pending' && existing.run_id !== authority.runId) {
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), actionId);
        }
      } else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)")
          .run(actionId, authority.runId, 'owner_quiet_admitted', payloadHash,
            event.id as string, event.payload_hash as string, textHash, stamp, stamp);
      }
      const action = this.getAction(actionId)!;
      const metadata = this.ownerQuietMetadata(action);
      return { actionId, sourceEventId: event.id as string, text: inbound.text,
        operation: 'write', status: action.status as string,
        sourcePayloadHash: event.payload_hash as string, admittedAtMs: event.received_at as number,
        providerThreadId: context.threadId, zone: metadata.zone,
        snoozeUntil: metadata.snoozeUntil };
    });
  }

  private ownerQuietMetadata(action: Row): { zone: string | null; snoozeUntil: string | null } {
    if (action.result_json === null) return { zone: null, snoozeUntil: null };
    let value: unknown;
    try { value = JSON.parse(action.result_json as string); } catch { throw new Error('owner quiet metadata is invalid'); }
    const row = value as Row | null;
    if (!row || typeof row !== 'object' || Array.isArray(row) ||
        !/^[A-Za-z0-9_+./:-]{1,64}$/.test(row.owner_zone as string)) {
      throw new Error('owner quiet metadata is invalid');
    }
    if (action.status === 'succeeded' &&
        (!/^[a-f0-9]{64}$/.test(row.receipt_hash as string) ||
         !isQuietUntil(row.snooze_until))) {
      throw new Error('owner quiet receipt is invalid');
    }
    return { zone: row.owner_zone as string,
      snoozeUntil: action.status === 'succeeded' ? row.snooze_until as string : null };
  }

  bindExplicitOwnerQuietZone(actionId: string, zone: string): void {
    if (!/^[A-Za-z0-9_+./:-]{1,64}$/.test(zone)) throw new Error('owner timezone is invalid');
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_quiet_admitted' || action.status !== 'pending' ||
          !this.isRunCurrent(action.run_id as string)) throw new Error('owner quiet action is not eligible');
      const previous = this.ownerQuietMetadata(action).zone;
      if (previous !== null && previous !== zone) throw new Error('owner timezone changed');
      this.db.prepare('UPDATE actions SET result_json=?,updated_at=? WHERE id=?')
        .run(JSON.stringify({ owner_zone: zone }), now(), actionId);
    });
  }

  settleExplicitOwnerQuietIntent(actionId: string, receiptHash: string, zone: string,
                                 snoozeUntil: string, reconciled = false): void {
    if (!/^[a-f0-9]{64}$/.test(receiptHash) || !/^[A-Za-z0-9_+./:-]{1,64}$/.test(zone) ||
        !isQuietUntil(snoozeUntil)) throw new Error('owner quiet receipt is invalid');
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_quiet_admitted' ||
          action.status !== (reconciled ? 'unknown' : 'executing') ||
          this.ownerQuietMetadata(action).zone !== zone) throw new Error('owner quiet action is not eligible');
      this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
        .run(JSON.stringify({ owner_zone: zone, receipt_hash: receiptHash,
          snooze_until: snoozeUntil }), now(), actionId);
    });
  }

  rejectExplicitOwnerQuietIntent(actionId: string, code: 'event_conflict'): void {
    if (code !== 'event_conflict') throw new Error('owner quiet rejection is invalid');
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_quiet_admitted' || action.status !== 'executing') {
        throw new Error('owner quiet action is not eligible');
      }
      const zone = this.ownerQuietMetadata(action).zone;
      this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
        .run(JSON.stringify({ owner_zone: zone, code }), now(), actionId);
    });
  }

  readUnknownExplicitOwnerQuietIntent(actionId: string,
    authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): ExplicitOwnerQuietIntent | null {
    const action = this.getAction(actionId);
    if (!action || action.kind !== 'owner_quiet_admitted' || action.status !== 'unknown' ||
        !/^[a-f0-9]{64}$/.test(authority.scopeHash ?? '')) return null;
    const run = this.getRun(action.run_id as string);
    const event = this.getInbound(action.source_event_id as string);
    if (!run || !event || run.event_id !== event.id || run.thread_key !== event.thread_key ||
        event.outcome !== 'pending' || event.account_key !== authority.accountId ||
        event.line_key !== authority.lineKey || event.channel !== authority.channel ||
        event.sender !== authority.ownerId || typeof event.payload !== 'string' ||
        hash(event.payload) !== event.payload_hash || event.payload_hash !== action.source_payload_hash) return null;
    let saved: unknown;
    try { saved = JSON.parse(event.payload); } catch { return null; }
    if (!saved || typeof saved !== 'object' || Array.isArray(saved) || JSON.stringify(saved) !== event.payload) return null;
    const record = saved as Record<string, unknown>;
    const inbound = record.inbound as Record<string, unknown> | undefined;
    const context = record.context as Record<string, unknown> | undefined;
    if (!inbound || !context || inbound.eventId !== event.source_id || inbound.accountId !== event.account_key ||
        inbound.senderId !== event.sender || inbound.channel !== event.channel ||
        context.lineKey !== event.line_key || typeof context.threadId !== 'string' ||
        hash(JSON.stringify([event.account_key, event.line_key, event.channel, context.threadId])) !== event.thread_key ||
        typeof inbound.text !== 'string' || !parseOwnerQuietCommand(inbound.text) ||
        action.id !== hash(JSON.stringify(['sotto.owner-quiet-command.v1', event.id])) ||
        action.source_text_hash !== hash(inbound.text) ||
        action.payload_hash !== hash(JSON.stringify([event.payload_hash, hash(inbound.text), authority.scopeHash]))) return null;
    let metadata: { zone: string | null; snoozeUntil: string | null };
    try { metadata = this.ownerQuietMetadata(action); } catch { return null; }
    if (!metadata.zone) return null;
    return { actionId, sourceEventId: event.id as string, text: inbound.text,
      operation: 'write', status: 'unknown', sourcePayloadHash: event.payload_hash as string,
      admittedAtMs: event.received_at as number, providerThreadId: context.threadId,
      zone: metadata.zone, snoozeUntil: null };
  }

  /** One literal owner reminder command, bound to the run's exact admitted event. */
  prepareExplicitOwnerReminderIntent(authority: ExplicitOwnerLoopAuthority): ExplicitOwnerReminderIntent {
    return this.transaction(() => {
      const run = this.getRun(authority.runId);
      if (!run || !this.isRunCurrent(authority.runId) || !/^[a-f0-9]{64}$/.test(authority.scopeHash ?? '')) {
        throw new Error('owner reminder authority is invalid');
      }
      const event = this.db.prepare('SELECT e.* FROM inbound_events e WHERE e.id=? AND e.rowid<=?')
        .get(run.event_id as string, run.admitted_through as number) as Row | undefined;
      if (!event || event.outcome !== 'pending' || event.thread_key !== run.thread_key ||
          event.account_key !== authority.accountId || event.line_key !== authority.lineKey ||
          event.channel !== authority.channel || event.sender !== authority.ownerId ||
          typeof event.payload !== 'string' || hash(event.payload) !== event.payload_hash) {
        throw new Error('owner reminder event is invalid');
      }
      let saved: unknown;
      try { saved = JSON.parse(event.payload); } catch { throw new Error('owner reminder payload is invalid'); }
      if (!saved || typeof saved !== 'object' || Array.isArray(saved) || JSON.stringify(saved) !== event.payload) {
        throw new Error('owner reminder payload is invalid');
      }
      const record = saved as Record<string, unknown>;
      const inbound = record.inbound as Record<string, unknown> | undefined;
      const context = record.context as Record<string, unknown> | undefined;
      if (!inbound || !context || inbound.eventId !== event.source_id ||
          inbound.accountId !== event.account_key || inbound.senderId !== event.sender ||
          inbound.channel !== event.channel || context.lineKey !== event.line_key ||
          typeof context.threadId !== 'string' ||
          hash(JSON.stringify([event.account_key, event.line_key, event.channel, context.threadId])) !== event.thread_key ||
          typeof inbound.text !== 'string') throw new Error('owner reminder identity is invalid');
      const parsed = parseOwnerReminderCommand(inbound.text);
      if (!parsed || parsed.operation === 'list') throw new Error('owner reminder mutation is invalid');
      const actionId = hash(JSON.stringify(['sotto.owner-reminder-command.v1', event.id]));
      const textHash = hash(inbound.text);
      const payloadHash = hash(JSON.stringify([event.payload_hash, textHash, authority.scopeHash]));
      const existing = this.getAction(actionId);
      if (existing) {
        const oldRun = this.getRun(existing.run_id as string);
        if (!oldRun || oldRun.thread_key !== run.thread_key || existing.kind !== 'owner_reminder_admitted' ||
            existing.payload_hash !== payloadHash || existing.source_event_id !== event.id ||
            existing.source_payload_hash !== event.payload_hash || existing.source_text_hash !== textHash) {
          throw new Error('owner reminder action identity conflict');
        }
        if (existing.status === 'pending' && existing.run_id !== authority.runId) {
          this.db.prepare('UPDATE actions SET run_id=?,updated_at=? WHERE id=?').run(authority.runId, now(), actionId);
        }
      } else {
        const stamp = now();
        this.db.prepare("INSERT INTO actions(id,run_id,kind,payload_hash,source_event_id,source_payload_hash,source_text_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)")
          .run(actionId, authority.runId, 'owner_reminder_admitted', payloadHash,
            event.id as string, event.payload_hash as string, textHash, stamp, stamp);
      }
      const action = this.getAction(actionId)!;
      const result = this.ownerReminderResult(action);
      return { actionId, sourceEventId: event.id as string, text: inbound.text,
        operation: parsed.operation, status: action.status as string,
        sourcePayloadHash: event.payload_hash as string, admittedAtMs: event.received_at as number,
        providerThreadId: context.threadId, reminderId: result?.reminder_id ?? null,
        reminderStatus: result?.reminder_status ?? null };
    });
  }

  /** Read-only list still requires an exact current admitted owner command. */
  validateExplicitOwnerReminderList(authority: ExplicitOwnerLoopAuthority): {
    actionId: string; sourceEventId: string; text: string; operation: 'list';
    sourcePayloadHash: string; admittedAtMs: number; providerThreadId: string;
  } {
    const run = this.getRun(authority.runId);
    if (!run || !this.isRunCurrent(authority.runId) || !/^[a-f0-9]{64}$/.test(authority.scopeHash ?? '')) {
      throw new Error('owner reminder list authority is invalid');
    }
    const event = this.db.prepare('SELECT e.* FROM inbound_events e WHERE e.id=? AND e.rowid<=?')
      .get(run.event_id as string, run.admitted_through as number) as Row | undefined;
    if (!event || event.outcome !== 'pending' || event.thread_key !== run.thread_key ||
        event.account_key !== authority.accountId || event.line_key !== authority.lineKey ||
        event.channel !== authority.channel || event.sender !== authority.ownerId ||
        typeof event.payload !== 'string' || hash(event.payload) !== event.payload_hash) {
      throw new Error('owner reminder list event is invalid');
    }
    let saved: unknown;
    try { saved = JSON.parse(event.payload); } catch { throw new Error('owner reminder list payload is invalid'); }
    if (!saved || typeof saved !== 'object' || Array.isArray(saved) || JSON.stringify(saved) !== event.payload) {
      throw new Error('owner reminder list payload is invalid');
    }
    const record = saved as Record<string, unknown>;
    const inbound = record.inbound as Record<string, unknown> | undefined;
    const context = record.context as Record<string, unknown> | undefined;
    if (!inbound || !context || inbound.eventId !== event.source_id || inbound.accountId !== event.account_key ||
        inbound.senderId !== event.sender || inbound.channel !== event.channel ||
        context.lineKey !== event.line_key || typeof context.threadId !== 'string' ||
        hash(JSON.stringify([event.account_key, event.line_key, event.channel, context.threadId])) !== event.thread_key ||
        inbound.text !== '/reminder list') throw new Error('owner reminder list identity is invalid');
    return { actionId: hash(JSON.stringify(['sotto.owner-reminder-command.v1', event.id])),
      sourceEventId: event.id as string, text: inbound.text, operation: 'list',
      sourcePayloadHash: event.payload_hash as string, admittedAtMs: event.received_at as number,
      providerThreadId: context.threadId };
  }

  private ownerReminderResult(action: Row): { reminder_id: string; reminder_status: 'scheduled' | 'canceled' | 'fired' } | null {
    if (action.status !== 'succeeded') return null;
    let result: unknown;
    try { result = JSON.parse(action.result_json as string); } catch { throw new Error('owner reminder receipt is invalid'); }
    const row = result as Record<string, unknown> | null;
    if (!row || typeof row !== 'object' || !/^[a-f0-9]{64}$/.test(row.receipt_hash as string) ||
        !/^int_[a-f0-9]{32}$/.test(row.reminder_id as string) ||
        !['scheduled', 'canceled', 'fired'].includes(row.reminder_status as string)) {
      throw new Error('owner reminder receipt is invalid');
    }
    return { reminder_id: row.reminder_id as string,
      reminder_status: row.reminder_status as 'scheduled' | 'canceled' | 'fired' };
  }

  settleExplicitOwnerReminderIntent(actionId: string, receiptHash: string, reminderId: string,
                                    reminderStatus: 'scheduled' | 'canceled' | 'fired', reconciled = false): void {
    if (!/^[a-f0-9]{64}$/.test(receiptHash) || !/^int_[a-f0-9]{32}$/.test(reminderId) ||
        !['scheduled', 'canceled', 'fired'].includes(reminderStatus)) throw new Error('owner reminder receipt is invalid');
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_reminder_admitted' ||
          action.status !== (reconciled ? 'unknown' : 'executing')) throw new Error('owner reminder action is not eligible');
      this.db.prepare("UPDATE actions SET status='succeeded',result_json=?,updated_at=? WHERE id=?")
        .run(JSON.stringify({ receipt_hash: receiptHash, reminder_id: reminderId,
          reminder_status: reminderStatus }), now(), actionId);
    });
  }

  rejectExplicitOwnerReminderIntent(actionId: string, code: string): void {
    if (!['event_conflict', 'target_unavailable', 'target_not_owner', 'due_unavailable'].includes(code)) {
      throw new Error('owner reminder rejection is invalid');
    }
    this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.kind !== 'owner_reminder_admitted' || action.status !== 'executing') {
        throw new Error('owner reminder action is not eligible');
      }
      this.db.prepare("UPDATE actions SET status='rejected',result_json=?,updated_at=? WHERE id=?")
        .run(JSON.stringify({ code }), now(), actionId);
    });
  }

  readUnknownExplicitOwnerReminderIntent(actionId: string,
    authority: Omit<ExplicitOwnerLoopAuthority, 'runId'>): ExplicitOwnerReminderIntent | null {
    const action = this.getAction(actionId);
    if (!action || action.kind !== 'owner_reminder_admitted' || action.status !== 'unknown') return null;
    const run = this.getRun(action.run_id as string);
    const event = this.getInbound(action.source_event_id as string);
    if (!run || !event || run.event_id !== event.id || run.thread_key !== event.thread_key ||
        event.outcome !== 'pending' || event.account_key !== authority.accountId ||
        event.line_key !== authority.lineKey || event.channel !== authority.channel ||
        event.sender !== authority.ownerId || typeof event.payload !== 'string' ||
        hash(event.payload) !== event.payload_hash || event.payload_hash !== action.source_payload_hash ||
        !/^[a-f0-9]{64}$/.test(authority.scopeHash ?? '')) return null;
    let saved: unknown;
    try { saved = JSON.parse(event.payload); } catch { return null; }
    if (!saved || typeof saved !== 'object' || Array.isArray(saved) || JSON.stringify(saved) !== event.payload) return null;
    const record = saved as Record<string, unknown>;
    const inbound = record.inbound as Record<string, unknown> | undefined;
    const context = record.context as Record<string, unknown> | undefined;
    if (!inbound || !context || inbound.eventId !== event.source_id || inbound.accountId !== event.account_key ||
        inbound.senderId !== event.sender || inbound.channel !== event.channel ||
        context.lineKey !== event.line_key || typeof context.threadId !== 'string' ||
        hash(JSON.stringify([event.account_key, event.line_key, event.channel, context.threadId])) !== event.thread_key ||
        typeof inbound.text !== 'string') return null;
    const parsed = parseOwnerReminderCommand(inbound.text);
    if (!parsed || parsed.operation === 'list' ||
        action.id !== hash(JSON.stringify(['sotto.owner-reminder-command.v1', event.id])) ||
        action.source_text_hash !== hash(inbound.text) ||
        action.payload_hash !== hash(JSON.stringify([event.payload_hash, hash(inbound.text), authority.scopeHash]))) return null;
    return { actionId, sourceEventId: event.id as string, text: inbound.text,
      operation: parsed.operation, status: 'unknown', sourcePayloadHash: event.payload_hash as string,
      admittedAtMs: event.received_at as number, providerThreadId: context.threadId,
      reminderId: null, reminderStatus: null };
  }

  beginAction(actionId: string): Row {
    return this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || action.status !== 'pending' || !this.isRunCurrent(action.run_id as string)) throw new Error('action not eligible');
      if (action.kind === 'owner_loop_close_admitted') this.ownerCloseMetadata(action);
      this.db.prepare("UPDATE actions SET status='executing',updated_at=? WHERE id=?").run(now(), actionId);
      return this.getAction(actionId)!;
    });
  }

  settleAction(actionId: string, outcome: ActionOutcome, result?: unknown): Row {
    if (!['succeeded', 'rejected', 'unknown'].includes(outcome)) throw new Error('invalid action outcome');
    return this.transaction(() => {
      const action = this.getAction(actionId);
      if (!action || !['executing', 'unknown'].includes(action.status as string)) throw new Error('action not eligible');
      if (['owner_loop_add_admitted', 'owner_loop_close_admitted', 'owner_reminder_admitted', 'owner_quiet_admitted', 'owner_reminder_tool', 'owner_loop_tool', 'owner_preference_tool', 'owner_routine_tool','owner_gmail_draft_tool','owner_calendar_tool','owner_contacts_tool','owner_bridge_send_tool','owner_brief_request','owner_meeting_prep_request','owner_meeting_prep_offer','owner_offer_reply','owner_skill_request','owner_web_read'].includes(action.kind as string) &&
          (outcome !== 'unknown' || result !== undefined)) {
        throw new Error('owner command requires a compact verified receipt');
      }
      if (['owner_loop_add_admitted', 'owner_loop_close_admitted', 'owner_reminder_admitted', 'owner_quiet_admitted', 'owner_reminder_tool', 'owner_loop_tool', 'owner_preference_tool', 'owner_routine_tool','owner_gmail_draft_tool','owner_calendar_tool','owner_contacts_tool','owner_bridge_send_tool','owner_brief_request','owner_meeting_prep_request','owner_meeting_prep_offer','owner_offer_reply','owner_skill_request','owner_web_read'].includes(action.kind as string) && outcome === 'unknown') {
        this.db.prepare("UPDATE actions SET status='unknown',updated_at=? WHERE id=?")
          .run(now(), actionId);
        return this.getAction(actionId)!;
      }
      if (action.status === 'unknown' && outcome !== 'succeeded' && outcome !== 'rejected') return action;
      // Late authoritative proof can resolve an unknown; caller must provide that proof.
      const encoded = result === undefined ? null : JSON.stringify(result);
      this.db.prepare('UPDATE actions SET status=?,result_json=?,updated_at=? WHERE id=?')
        .run(outcome, encoded, now(), actionId);
      return this.getAction(actionId)!;
    });
  }

  /** No execution is repeated on reopen. An in-progress contact is classified as unknown. */
  recoverInterrupted(): { dispatches: number; actions: number } {
    return this.transaction(() => {
      const timestamp = now();
      this.db.prepare("UPDATE dispatch_attempts SET status='unknown',finished_at=? WHERE status='dispatching'").run(timestamp);
      const dispatches = Number(this.db.prepare("UPDATE dispatches SET status='unknown',updated_at=? WHERE status='dispatching'").run(timestamp).changes);
      const actions = Number(this.db.prepare("UPDATE actions SET status='unknown',updated_at=? WHERE status='executing'").run(timestamp).changes);
      return { dispatches, actions };
    });
  }

  /** Terminal records may age out; prepared/dispatching/unknown proof remains untouched. */
  pruneTerminal(beforeMs: number, actionBeforeMs = beforeMs): { replies: number; dispatches: number; actions: number; runs: number; events: number } {
    if (!Number.isFinite(beforeMs) || !Number.isFinite(actionBeforeMs) || actionBeforeMs > beforeMs) {
      throw new Error('invalid retention cutoff');
    }
    return this.transaction(() => {
      // Late acceptance of attempt N can coexist with uncertain attempt N+1.
      // An accepted reply can also cover still-pending inbound messages after a
      // crash. Keep that provider proof until every covered message resolves.
      // Gallery receipts have no receiver reconciliation acknowledgement yet,
      // so no age alone can prove the outbox has consumed them.
      const terminal = "status IN ('accepted','rejected','cancelled') AND kind<>'gallery' AND updated_at<? " +
        "AND id NOT IN (SELECT dispatch_id FROM dispatch_attempts WHERE status='unknown') " +
        "AND id NOT IN (SELECT p.dispatch_id FROM prepared_replies p JOIN json_each(p.covered_event_ids) c JOIN inbound_events e ON e.id=c.value WHERE e.outcome='pending')";
      const replies = Number(this.db.prepare(`DELETE FROM prepared_replies WHERE dispatch_id IN (SELECT id FROM dispatches WHERE ${terminal})`).run(beforeMs).changes);
      this.db.prepare(`DELETE FROM dispatch_attempts WHERE dispatch_id IN (SELECT id FROM dispatches WHERE ${terminal} AND id NOT IN (SELECT dispatch_id FROM prepared_replies))`).run(beforeMs);
      const dispatches = Number(this.db.prepare(`DELETE FROM dispatches WHERE ${terminal} AND id NOT IN (SELECT dispatch_id FROM prepared_replies)`).run(beforeMs).changes);
      const actions = Number(this.db.prepare("DELETE FROM actions WHERE kind NOT IN ('owner_loop_add_admitted','owner_loop_close_admitted','owner_reminder_admitted','owner_quiet_admitted','owner_reminder_tool', 'owner_loop_tool', 'owner_preference_tool', 'owner_routine_tool','owner_gmail_draft_tool','owner_calendar_tool','owner_contacts_tool','owner_bridge_send_tool','owner_brief_request','owner_meeting_prep_request','owner_meeting_prep_offer','owner_offer_reply','owner_skill_request','owner_web_read') AND status IN ('succeeded','rejected') AND updated_at<? AND run_id NOT IN (SELECT r.id FROM runs r JOIN inbound_events e ON e.id=r.event_id WHERE e.outcome='pending')").run(actionBeforeMs).changes);
      const runs = Number(this.db.prepare("DELETE FROM runs WHERE status IN ('completed','cancelled') AND ended_at<? AND id NOT IN (SELECT run_id FROM prepared_replies) AND id NOT IN (SELECT run_id FROM actions)").run(beforeMs).changes);
      // A running run has captured max(rowid) as its admission boundary. Keep every
      // row in that thread until the run ends so a later admission cannot reuse a
      // pruned rowid and appear to have preceded the run.
      const events = Number(this.db.prepare("DELETE FROM inbound_events WHERE outcome IN ('resolved','cancelled') AND received_at<? AND thread_key NOT IN (SELECT thread_key FROM runs WHERE status='running') AND id NOT IN (SELECT event_id FROM prepared_replies) AND id NOT IN (SELECT event_id FROM runs) AND id NOT IN (SELECT source_event_id FROM actions WHERE source_event_id IS NOT NULL) AND id NOT IN (SELECT event_id FROM activation_obligation WHERE acknowledged_at IS NULL)").run(beforeMs).changes);
      // The owner-command action is a lifetime idempotency fence. Its source event and run
      // remain as compact identity/hash rows, but a resolved quote is no longer needed once
      // no pending action, reply, or activation obligation depends on that payload.
      this.db.exec(`UPDATE inbound_events AS e SET payload=''
        WHERE e.outcome IN ('resolved','cancelled') AND e.payload<>''
          AND (e.id IN (SELECT source_event_id FROM actions WHERE kind IN ('owner_loop_add_admitted','owner_loop_close_admitted','owner_reminder_admitted','owner_quiet_admitted','owner_reminder_tool', 'owner_loop_tool', 'owner_preference_tool', 'owner_routine_tool','owner_gmail_draft_tool','owner_calendar_tool','owner_contacts_tool','owner_bridge_send_tool','owner_brief_request','owner_meeting_prep_request','owner_meeting_prep_offer','owner_offer_reply','owner_skill_request','owner_web_read'))
            OR e.id IN (SELECT r.event_id FROM runs r JOIN actions a ON a.run_id=r.id
              WHERE a.kind IN ('owner_loop_add_admitted','owner_loop_close_admitted','owner_reminder_admitted','owner_quiet_admitted','owner_reminder_tool', 'owner_loop_tool', 'owner_preference_tool', 'owner_routine_tool','owner_gmail_draft_tool','owner_calendar_tool','owner_contacts_tool','owner_bridge_send_tool','owner_brief_request','owner_meeting_prep_request','owner_meeting_prep_offer','owner_offer_reply','owner_skill_request','owner_web_read')))
          AND e.id NOT IN (SELECT event_id FROM activation_obligation WHERE acknowledged_at IS NULL)
          AND e.id NOT IN (SELECT p.event_id FROM prepared_replies p)
          AND e.id NOT IN (SELECT j.value FROM prepared_replies p, json_each(p.covered_event_ids) j)
          AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.thread_key=e.thread_key AND r.status='running')
          AND NOT EXISTS (SELECT 1 FROM actions a JOIN runs r ON r.id=a.run_id
            WHERE r.thread_key=e.thread_key AND a.status IN ('pending','executing','unknown'))`);
      return { replies, dispatches, actions, runs, events };
    });
  }

  /**
   * Retention compaction that keeps every idempotency fence and receipt. Settled gallery
   * dispatches lose only their payload (summary and image hashes); route, payload hash,
   * attempts and provider IDs still answer receipt lookups and duplicate sends. Terminal
   * model-invoked owner-tool actions keep their id/kind/run/source/payload hashes and
   * status (their lifetime fence) while result_json becomes a digest. A consumer keeps
   * only the proposal link that the already-consumed checks read. Nothing in a thread with
   * pending input or a running run is compacted, nor any action of the same kind as an
   * unresolved action in its thread: a later inspect of that action must still be able to
   * read its proposal/baseline chain. A parked unknown action of one tool therefore never
   * blocks compaction of the thread's other tools.
   */
  compactTerminal(beforeMs: number, actionBeforeMs = beforeMs): { gallery: number; actions: number } {
    if (!Number.isFinite(beforeMs) || !Number.isFinite(actionBeforeMs) || actionBeforeMs > beforeMs) {
      throw new Error('invalid retention cutoff');
    }
    return this.transaction(() => {
      const gallery = Number(this.db.prepare(`UPDATE dispatches SET payload='' WHERE kind='gallery'
        AND status IN ('accepted','rejected','cancelled') AND updated_at<? AND payload<>''
        AND id NOT IN (SELECT dispatch_id FROM dispatch_attempts WHERE status IN ('unknown','dispatching'))`).run(beforeMs).changes);
      const candidates = this.db.prepare(`SELECT a.id,a.result_json FROM actions a JOIN runs r ON r.id=a.run_id
        WHERE a.kind IN (${OWNER_TOOL_KINDS_SQL}) AND a.status IN ('succeeded','rejected') AND a.compacted_at IS NULL AND a.updated_at<?
          AND NOT EXISTS (SELECT 1 FROM inbound_events e WHERE e.thread_key=r.thread_key AND e.outcome='pending')
          AND NOT EXISTS (SELECT 1 FROM runs x WHERE x.thread_key=r.thread_key AND x.status='running')
          AND NOT EXISTS (SELECT 1 FROM actions b JOIN runs y ON y.id=b.run_id
            WHERE y.thread_key=r.thread_key AND b.kind=a.kind AND b.status IN ('pending','executing','unknown'))`).all(actionBeforeMs) as Row[];
      const compact = this.db.prepare('UPDATE actions SET result_json=?,compacted_at=? WHERE id=?');
      const checked = this.db.prepare('UPDATE actions SET proposal_checked=1 WHERE id=?');
      const stamp = now();
      for (const action of candidates) {
        const original = typeof action.result_json === 'string' ? action.result_json : '';
        let link: {operation: string; proposal: {action_id: string}} | undefined;
        try {
          const args = (JSON.parse(original) as Row | null)?.args as Row | undefined;
          const operation = args?.operation, proposal = (args?.proposal as Row | undefined)?.action_id;
          if (typeof operation === 'string' && !PROPOSAL_OPERATIONS.includes(operation) && typeof proposal === 'string')
            link = {operation, proposal: {action_id: proposal}};
        } catch { /* malformed legacy content is still reduced to its digest */ }
        compact.run(JSON.stringify({compacted: 1, result_hash: hash(original), ...(link ? {args: link} : {})}), stamp, action.id as string);
        checked.run(action.id as string);
      }
      return { gallery, actions: candidates.length };
    });
  }

  /** After an explicit expiry decision, remove old uncertain content while retaining proof keys. */
  scrubUnknownPayload(beforeMs: number): { dispatches: number; replies: number } {
    if (!Number.isFinite(beforeMs)) throw new Error('invalid retention cutoff');
    return this.transaction(() => {
      const replies = Number(this.db.prepare("UPDATE prepared_replies SET payload='' WHERE dispatch_id IN (SELECT id FROM dispatches WHERE status='unknown' AND updated_at<?) AND payload<>''").run(beforeMs).changes);
      const dispatches = Number(this.db.prepare("UPDATE dispatches SET payload='' WHERE status='unknown' AND updated_at<? AND payload<>''").run(beforeMs).changes);
      return { dispatches, replies };
    });
  }
}
