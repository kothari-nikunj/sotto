import {projectTaskToolDiagnostics, type TaskToolDiagnosticBatch} from './agent/tool-diagnostics.js';
import {projectConversationContext} from './background-context.js';
import type {OwnerBridgeSendClient} from './tools/owner-bridge-send-client.js';
import {projectModelMetrics} from './agent/metrics.js';
import type {OwnerContactsClient} from './tools/owner-contacts-client.js';
import type {OwnerCalendarClient} from './tools/owner-calendar-client.js';
import type {OwnerWebReadClient} from './tools/owner-web-read.js';
import type {OwnerSkillRequestClient} from './tools/owner-skill-request-client.js';
import {routeDigest} from './route-proof.js';
import type {OwnerOfferReplyClient} from './tools/owner-offer-reply-client.js';
import {meetingPrepSelectors, type OwnerMeetingPrepClient} from './tools/owner-meeting-prep-client.js';
import type {OwnerMeetingPrepOfferClient} from './tools/owner-meeting-prep-offer-client.js';
import type {OwnerBriefRequestClient} from './tools/owner-brief-request-client.js';
import type { OwnerGmailDraftClient } from './tools/owner-gmail-draft-client.js';
import type { OwnerRoutineToolClient } from './tools/owner-routine-tool-client.js';
import { createHash, randomUUID } from 'node:crypto';
import path from 'node:path';
import { OperationalJournal } from './journal/index.js';
import { ConversationCoordinator, type ConversationDependencies, type ModelRequest } from './conversation.js';
import type { Inbound, OwnerPolicy } from './channels/types.js';
import type { ChannelTransport } from './channels/transport.js';
import { ReceiptedDelivery } from './delivery.js';
import { createHttpServices, type UnattendedAgent } from './services.js';
import { createChatHttpServer, listenLoopback } from './http.js';
import { claimRuntimeDirectory } from './ownership.js';
import { activationDiscoverySeedSchema, activationIdentitySchema,
  type ActivationDiscoverySeed, type ActivationIdentity } from './activation.js';
import { configuredChannelIdentitySchema, type ConfiguredChannelIdentity } from './configured-identity.js';
import { activationStateSchema } from './activation-state.js';
import type { OwnerLoopClient } from './tools/owner-loop-client.js';
import type { OwnerReminderClient } from './tools/owner-reminder-client.js';
import type { OwnerPreferenceToolClient } from './tools/owner-preference-tool-client.js';
import type { OwnerTaskToolClient } from './tools/owner-task-tool-client.js';
import type { OwnerReminderToolClient } from './tools/owner-reminder-tool-client.js';
import type { OwnerQuietClient } from './tools/owner-quiet-client.js';
import { AcceptedConversationMemory, type ConversationMemoryProjection, type ConversationMemoryResult } from './conversation-memory.js';

export interface ApplicationOptions {
  directory: string;
  mediaRoot: string;
  bearerToken: string;
  owner: OwnerPolicy;
  lineKey: string;
  target: string;
  /** Exact provider destination; caller cannot choose an arbitrary thread. */
  providerThreadId?: string;
  port?: number;
  /** Hold the loopback listener closed until the provider gateway is initialized. */
  deferListen?: boolean;
  allowedSkills?: ReadonlySet<string>;
  /** Explicit Spectrum Cloud owner identity, bound before provider intake. */
  activation?: ActivationIdentity;
  /** Explicit Cloud-only first-owner-DM mode. Fixed destinations never use this seed. */
  discoverySeed?: ActivationDiscoverySeed;
  /** Static loaded-config proof; no dynamic activation or provider-status claim. */
  configuredChannelIdentity?: ConfiguredChannelIdentity;
}
export interface ApplicationCloseOptions { retainOwnership?: boolean }
export interface ApplicationAgent extends UnattendedAgent {
  projectConversation?(input: ConversationMemoryProjection): Promise<ConversationMemoryResult>;
  generateChat(request: ModelRequest): Promise<{ text: string; sourceRevision?: string }>;
  canDeliverReplySource?(revision?: string): Promise<boolean>;
  rotateThread(threadId: string): Promise<void>;
  purgeThreadsBefore(before: Date): Promise<number>;
}
const chatTimingKeys = ['sourceCheckMs', 'standingContextMs', 'toolSetupMs', 'generationMs', 'finalSourceCheckMs', 'totalMs'] as const;
const optionalChatTimingKeys = ['queueMs', 'promptPreparationMs'] as const;
const allowedChatTimingKeys = [...chatTimingKeys, ...optionalChatTimingKeys] as const;
export type ChatTiming = Record<(typeof chatTimingKeys)[number], number> &
  Partial<Record<(typeof optionalChatTimingKeys)[number], number>>;

/** Health exposes only bounded stage durations, never request or owner context. */
export function projectChatTiming(value: unknown): ChatTiming | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      chatTimingKeys.some(key => !Object.hasOwn(value, key)) ||
      Object.keys(value).some(key => !allowedChatTimingKeys.includes(key as (typeof allowedChatTimingKeys)[number]))) return undefined;
  const fields = value as Record<string, unknown>;
  const keys = allowedChatTimingKeys.filter(key => Object.hasOwn(fields, key));
  if (keys.some(key => typeof fields[key] !== 'number' || !Number.isFinite(fields[key]) ||
      !Number.isInteger(fields[key]) || (fields[key] as number) < 0 || (fields[key] as number) > 300000)) return undefined;
  return Object.fromEntries(keys.map(key => [key, fields[key]])) as ChatTiming;
}
export interface ApplicationDependencies {
  agent: ApplicationAgent;
  transport: ChannelTransport;
  resolvePrepContinuation?: ConversationDependencies['resolvePrepContinuation'];
  hydrateInbound?: ConversationDependencies['hydrateInbound'];
  startTurnPresence?: ConversationDependencies['startTurnPresence'];
  onOwnerAdmission?: ConversationDependencies['onOwnerAdmission'];
  onOwnerReplyAccepted?: ConversationDependencies['onOwnerReplyAccepted'];
  onOwnerSilentWorkAccepted?: ConversationDependencies['onOwnerSilentWorkAccepted'];
  onOwnerTaskTiming?: ConversationDependencies['onOwnerTaskTiming'];
  confirmSwipeRoutine?: ConversationDependencies['confirmSwipeRoutine'];
  feedbackStatus?(): import('./channels/presence.js').PhotonFeedbackSnapshot;
  providerLifecycle?(): string;
  providerIntake?(): { receivedEvents: number; handedOffEvents: number; listenerStarts?: number;
    listenerStops?: number; streamErrors?: number; handlerErrors?: number };
  taskToolDiagnostics?(): unknown;
  modelMetrics?(): unknown;
  chatTiming?(): unknown;
  chatTimingCompletedAtMs?(): unknown;
  chatTimingOutcome?(): unknown;
  pruneExpiredState?(): Promise<number>;
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
  ownerCalendar?:OwnerCalendarClient;
  ownerContacts?:OwnerContactsClient;
  ownerBridgeSend?:OwnerBridgeSendClient;
  ownerBriefRequest?: OwnerBriefRequestClient;
  ownerMeetingPrep?: OwnerMeetingPrepClient;
  ownerMeetingPrepOffer?: OwnerMeetingPrepOfferClient;
  ownerOfferReply?: OwnerOfferReplyClient;
  onJournalReady?(journal: OperationalJournal): void;
  /** Offline test seam; production always uses the loopback listener. */
  listen?(server: ReturnType<typeof createChatHttpServer>, port: number | undefined): Promise<number>;
}

const hash = (value: unknown) => createHash('sha256').update(JSON.stringify(value)).digest('hex');

/**
 * One owner/line per state directory. Dependencies are injected: creating this service
 * neither looks up credentials nor starts a provider consumer. A supervisor must start
 * intake only after this promise resolves and stop it before close().
 */
export async function startApplication(
  options: ApplicationOptions,
  dependencies: ApplicationDependencies,
  externalReleaseOwnership?: () => Promise<void>,
) {
  if (!options.lineKey || !options.target || !options.owner.ownerId || !options.owner.accountId ||
      (options.providerThreadId === undefined) === (options.discoverySeed === undefined)) {
    throw new Error('Application owner and destination are required');
  }
  const activation = options.activation === undefined ? undefined : activationIdentitySchema.parse(options.activation);
  const discoverySeed = options.discoverySeed === undefined ? undefined : activationDiscoverySeedSchema.parse(options.discoverySeed);
  if (discoverySeed && (activation || options.owner.channel !== 'photon' ||
      discoverySeed.owner_id !== options.owner.ownerId || discoverySeed.account_id !== options.owner.accountId ||
      discoverySeed.line_key !== options.lineKey)) throw new Error('Discovery seed does not match the configured owner');
  if (activation && (options.owner.channel !== 'photon' || activation.owner_id !== options.owner.ownerId ||
      activation.account_id !== options.owner.accountId || activation.line_key !== options.lineKey ||
      activation.provider_thread_id !== options.providerThreadId)) throw new Error('Activation identity does not match the configured owner');
  const configured = options.configuredChannelIdentity === undefined ? undefined :
    configuredChannelIdentitySchema.parse(options.configuredChannelIdentity);
  if (configured) {
    const claim = configured.identity;
    if (claim.channel !== options.owner.channel || claim.owner_id !== options.owner.ownerId ||
        claim.account_id !== options.owner.accountId || claim.line_key !== options.lineKey ||
        claim.target !== options.target ||
        (discoverySeed ? ('provider_route_mode' in discoverySeed ? claim.route.mode !== 'first_contact_shared_project' : claim.route.mode !== 'first_contact' ||
          claim.route.expected_provider_line !== discoverySeed.expected_provider_line) :
          claim.route.mode !== 'fixed' || claim.route.provider_thread_id !== options.providerThreadId) ||
        ((activation || discoverySeed) && (claim.channel !== 'photon' ||
          claim.tenant_id !== (activation ?? discoverySeed)!.tenant_id))) {
      throw new Error('Configured channel identity does not match the application');
    }
  }
  const release = externalReleaseOwnership ?? await claimRuntimeDirectory(options.directory);
  let journal: OperationalJournal | undefined;
  try {
    journal = new OperationalJournal(path.join(options.directory, 'operations.sqlite'));
    // The historical resolved route is unknowable after a v5→v6 migration.
    // Stop startup before intake or scheduled HTTP service until an operator
    // reconciles unresolved legacy sends; never replay them against today's map.
    journal.assertNoUnresolvedLegacyDispatches();
    const db = journal.db;
    db.exec(`CREATE TABLE IF NOT EXISTS runtime_identity (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS memory_sessions (provider_thread TEXT PRIMARY KEY, memory_thread TEXT NOT NULL, created_at INTEGER NOT NULL);`);
    const identity = hash([options.owner, options.lineKey, options.target, options.providerThreadId ?? discoverySeed]);
    const saved = db.prepare('SELECT identity FROM runtime_identity WHERE id=1').get();
    if (saved && saved.identity !== identity) throw new Error('State directory belongs to a different owner or destination');
    db.prepare('INSERT OR IGNORE INTO runtime_identity VALUES(1,?)').run(identity);
    if (discoverySeed) journal.configureActivationDiscovery(discoverySeed);
    else journal.configureActivationIdentity(activation);
    journal.recoverInterrupted();
    dependencies.onJournalReady?.(journal);

    const boundThread = () => discoverySeed
      ? journal!.getActivationIdentity()?.provider_thread_id
      : options.providerThreadId;

    const memoryThread = (providerThread: string): string => {
      const row = db.prepare('SELECT memory_thread FROM memory_sessions WHERE provider_thread=?').get(providerThread);
      if (row) return String(row.memory_thread);
      const id = hash([identity, providerThread, randomUUID()]);
      db.prepare('INSERT INTO memory_sessions VALUES(?,?,?)').run(providerThread, id, Date.now());
      return id;
    };
    const projection = dependencies.agent.projectConversation ? new AcceptedConversationMemory({
      journal, ownerId:hash([identity, options.owner.ownerId]), threadFor:memoryThread,
      sessionStartedAt:thread => {
        const row = db.prepare('SELECT created_at FROM memory_sessions WHERE provider_thread=?').get(thread);
        return row ? Number(row.created_at) : undefined;
      },
      scopeThread:thread => hash([options.owner.accountId, options.lineKey, options.owner.channel, thread]),
      routeDigestForThread:thread => routeDigest(routeIdentity, thread),
      reset:threadId => dependencies.agent.rotateThread(threadId),
      project:input => dependencies.agent.projectConversation!(input),
    }) : undefined;
    const projectConversation = async (thread?: string) => {
      if (!projection) return;
      const threads = thread ? [thread] : boundThread() ? [boundThread()!] : [];
      for (const providerThread of threads) await projection.sync(providerThread);
    };
    const rotateThread = async (providerThread: string, throughEventId?: string) => {
      const rotate = async () => {
        const row = db.prepare('SELECT memory_thread FROM memory_sessions WHERE provider_thread=?').get(providerThread);
        if (row) await dependencies.agent.rotateThread(String(row.memory_thread));
        projection?.rotate(providerThread, throughEventId);
        db.prepare('DELETE FROM memory_sessions WHERE provider_thread=?').run(providerThread);
      };
      await (projection ? projection.exclusive(rotate) : rotate());
    };
    const destinations = new Map<string, { threadId: string; transport: ChannelTransport }>();
    const syncScheduledDestination = () => {
      const thread = boundThread();
      if (thread && (!discoverySeed || journal!.isActivationAcknowledged())) {
        destinations.set(options.target, { threadId: thread, transport: dependencies.transport });
      } else destinations.delete(options.target);
    };
    syncScheduledDestination();
    const routeIdentity = {
      channel: options.owner.channel,
      tenantId: activation?.tenant_id ?? discoverySeed?.tenant_id ??
        options.configuredChannelIdentity?.identity.tenant_id ?? '',
      projectFingerprint: activation?.project_fingerprint ?? discoverySeed?.project_fingerprint ?? '',
      ownerId: options.owner.ownerId,
      accountId: options.owner.accountId,
      lineKey: options.lineKey,
    };
    const delivery = new ReceiptedDelivery(journal, destinations, routeIdentity);
    const coordinator = new ConversationCoordinator({
      journal, ownerPolicy: options.owner,
      routeDigestForThread: thread => routeDigest(routeIdentity, thread),
      resolvePrepContinuation: input => input.threadId === boundThread()
        ? dependencies.resolvePrepContinuation?.(input) ?? Promise.resolve(null) : Promise.resolve(null),
      ownerLoop: dependencies.ownerLoop,
      ownerReminder: dependencies.ownerReminder,
      ownerQuiet: dependencies.ownerQuiet,
      ownerReminderTool: dependencies.ownerReminderTool,
      ownerTaskTool: dependencies.ownerTaskTool,
      ownerPreferenceTool: dependencies.ownerPreferenceTool,
      ownerSkillRequest: dependencies.ownerSkillRequest,
      ownerWebRead: dependencies.ownerWebRead,
      ownerRoutineTool: dependencies.ownerRoutineTool,
      ownerGmailDraft: dependencies.ownerGmailDraft,
      ownerCalendar:dependencies.ownerCalendar,
      ownerContacts:dependencies.ownerContacts,
      ownerBridgeSend:dependencies.ownerBridgeSend,
      ownerBriefRequest: dependencies.ownerBriefRequest,
      ownerMeetingPrep: dependencies.ownerMeetingPrep,
      ownerMeetingPrepOffer: dependencies.ownerMeetingPrepOffer,
      ownerOfferReply: dependencies.ownerOfferReply,
      confirmSwipeRoutine: dependencies.confirmSwipeRoutine,
      hydrateInbound: dependencies.hydrateInbound ? (messages, context) => {
        if (context.threadId !== boundThread()) throw new Error('media context does not match the bound provider thread');
        return dependencies.hydrateInbound!(messages, context);
      } : undefined,
      startTurnPresence: dependencies.startTurnPresence,
      onOwnerAdmission: dependencies.onOwnerAdmission,
      onOwnerReplyAccepted: dependencies.onOwnerReplyAccepted,
      onOwnerSilentWorkAccepted: dependencies.onOwnerSilentWorkAccepted,
      onOwnerTaskTiming: dependencies.onOwnerTaskTiming,
      projectConversation,
      model: { generate: async request => {
        await projectConversation(request.threadId);
        const session = db.prepare('SELECT created_at FROM memory_sessions WHERE provider_thread=?').get(request.threadId);
        return dependencies.agent.generateChat({
          ...request, ...projectConversationContext(journal!, request, Number(session?.created_at ?? Infinity), Boolean(projection), routeDigest(routeIdentity, request.threadId)),
          threadId: memoryThread(request.threadId), ownerId: hash([identity, request.ownerId]),
          historyInputIds:projection?.currentInputIds(request.threadId, request.messages.map(message => message.eventId)),
        });
      } },
      canPostPrepared: revision => dependencies.agent.canDeliverReplySource?.(revision) ?? Promise.resolve(true),
      postPrepared: async request => {
        if (request.destination !== options.target) return { acceptance: 'not_attempted' };
        const thread = boundThread();
        if (!thread) return { acceptance: 'not_attempted' };
        const receipt = await dependencies.transport.send({ threadId: thread, text: request.text, verbatim: request.verbatim, signal: request.signal });
        const ids = receipt.providerMessageIds.filter(id => typeof id === 'string' && id.length > 0 && id.length <= 512).slice(0, 8);
        return { acceptance: receipt.acceptance, messageId: ids[0], messageIds: ids, providerSentAtMs: receipt.providerSentAtMs };
      },
      rotateSession: rotateThread,
    });
    const rotateSessions = async (): Promise<{ rotated: true; count: number }> => coordinator.exclusive(async () => {
      const rows = db.prepare('SELECT provider_thread FROM memory_sessions').all();
      for (const row of rows) await rotateThread(String(row.provider_thread));
      return { rotated: true, count: rows.length };
    });
    await coordinator.recover();
    let closed = false;
    const unattendedControllers = new Set<AbortController>();
    const unattendedRuns = new Set<Promise<unknown>>();
    const unattendedAgent: UnattendedAgent = {
      async generateUnattended(input) {
        const controller = new AbortController();
        unattendedControllers.add(controller);
        const signal = input.signal ? AbortSignal.any([input.signal, controller.signal]) : controller.signal;
        const run = dependencies.agent.generateUnattended({ ...input, signal });
        unattendedRuns.add(run);
        try { return await run; }
        finally { unattendedControllers.delete(controller); unattendedRuns.delete(run); }
      },
    };
    const server = createChatHttpServer(createHttpServices({
      delivery, agent: unattendedAgent, allowedSkills: options.allowedSkills ?? new Set(),
      sessions: { execute: async request => {
        if (request.action === 'list') return { sessions: db.prepare('SELECT memory_thread AS id, created_at FROM memory_sessions').all() };
        if (request.action === 'rotate') return rotateSessions();
        return coordinator.exclusive(async () => {
          return { purged: await dependencies.agent.purgeThreadsBefore(new Date(request.before)) };
        });
      } },
      retention: async request => {
        const deliveryBefore = Date.parse(request.delivery_before), actionBefore = Date.parse(request.action_before);
        const pruned = journal!.pruneTerminal(deliveryBefore, actionBefore);
        // Same explicit cutoffs; the response shape is unchanged. Compaction keeps every
        // fence/receipt, and unknown content is scrubbed only past the longer action window.
        journal!.compactTerminal(deliveryBefore, actionBefore);
        journal!.scrubUnknownPayload(actionBefore);
        return { ...pruned, sdkExpired: await dependencies.pruneExpiredState?.() ?? 0 };
      },
      activationEvidence: async () => journal!.getActivationEvidence(),
      activationState: configured && (activation || discoverySeed) ? async () => {
        const admitted = journal!.getDurableActivationAdmission();
        const mode = discoverySeed ? ('provider_route_mode' in discoverySeed ? 'first_contact_shared_project' : 'first_contact') : 'fixed';
        return activationStateSchema.parse({ version: 1, configured, activation: { ...admitted, mode } });
      } : undefined,
      activationAck: async eventId => {
        const acknowledged = journal!.acknowledgeActivation(eventId);
        if (acknowledged) {
          syncScheduledDestination();
          // A first-contact owner command was held until the receiver's sole
          // activation receipt existed; resume pending journal input after ACK.
          if (dependencies.ownerLoop || dependencies.ownerReminder || dependencies.ownerQuiet || dependencies.ownerReminderTool || dependencies.ownerTaskTool || dependencies.ownerPreferenceTool || dependencies.ownerSkillRequest || dependencies.ownerWebRead || dependencies.ownerRoutineTool || dependencies.ownerGmailDraft || dependencies.ownerCalendar || dependencies.ownerContacts || dependencies.ownerBridgeSend || dependencies.ownerBriefRequest || dependencies.ownerMeetingPrep || dependencies.ownerMeetingPrepOffer || dependencies.ownerOfferReply) void coordinator.recover().catch(() => undefined);
        }
        return acknowledged;
      },
      health: async () => {
        const providerLifecycle = dependencies.providerLifecycle?.() ?? 'not_managed';
        let taskToolDiagnostics: TaskToolDiagnosticBatch | undefined;
        try { taskToolDiagnostics = projectTaskToolDiagnostics(dependencies.taskToolDiagnostics?.()); } catch { /* Optional telemetry fails closed. */ }
        let modelMetrics;
        try { modelMetrics = projectModelMetrics(dependencies.modelMetrics?.()); } catch { /* Fail closed. */ }
        const chatTiming = projectChatTiming(dependencies.chatTiming?.());
        const completedAtMs = dependencies.chatTimingCompletedAtMs?.();
        const outcome = dependencies.chatTimingOutcome?.();
        return {
          ready: !closed && serverListening && providerLifecycle !== 'failed' && providerLifecycle !== 'starting' &&
            (!discoverySeed || (!!boundThread() && journal!.isActivationAcknowledged())),
          ...(dependencies.feedbackStatus ? {feedback: dependencies.feedbackStatus()} : {}),
          ...(taskToolDiagnostics ? {taskToolDiagnostics} : {}),
          ...(modelMetrics ? {modelMetrics} : {}),
          runtime: 'sotto', providerConnection: 'not_observed', providerLifecycle,
          ...(dependencies.providerIntake ? {providerIntake: dependencies.providerIntake()} : {}),
          ...(chatTiming && typeof completedAtMs === 'number' && Number.isSafeInteger(completedAtMs) &&
            completedAtMs >= 0 ? {chatTiming, chatTimingCompletedAtMs: completedAtMs,
              ...(outcome === 'success' || outcome === 'failed' ? {chatTimingOutcome: outcome} : {})} : {}),
          activation: discoverySeed ? { bound: !!boundThread(), acknowledged: journal!.isActivationAcknowledged(),
            pending: !!journal!.getActivationEvidence() } : undefined,
          pendingInbound: journal!.listPendingInbound().length,
          uncertainDispatches: Number(db.prepare("SELECT count(*) AS n FROM dispatches WHERE status IN ('unknown','dispatching')").get()!.n),
          uncertainActions: Number(db.prepare("SELECT count(*) AS n FROM actions WHERE status IN ('unknown','executing')").get()!.n),
        };
      },
    }), { bearerToken: options.bearerToken, mediaRoot: options.mediaRoot, allowedTargets: new Set([options.target]),
      configuredChannelIdentity: configured });
    let port: number | undefined;
    let serverListening = false;
    const listen = async (): Promise<number> => {
      if (closed) throw new Error('Application is closed');
      if (port !== undefined) return port;
      port = dependencies.listen
        ? await dependencies.listen(server, options.port)
        : await listenLoopback(server, options.port);
      serverListening = true;
      return port;
    };
    if (!options.deferListen) await listen();
    let closing: Promise<void> | undefined;
    return {
      get port(): number {
        if (port === undefined) throw new Error('Application listener is not ready');
        return port;
      },
      listen,
      rotateSessions,
      /** Synchronous admission happens before the returned turn promise yields. */
      receive(inbound: Inbound, provider?: { threadId: string; sentAtMs?: number }) {
        if (closed) return Promise.reject(new Error('Application is closed'));
        const thread = discoverySeed ? provider?.threadId : options.providerThreadId;
        if (!thread || (!discoverySeed && provider && provider.threadId !== thread)) {
          return Promise.resolve({ status: 'denied' as const, reason: 'provider destination is invalid' });
        }
        // The gateway supplied the candidate independently of message content. The
        // journal validates the immutable seed and commits binding with admission.
        const candidate = discoverySeed ? activationIdentitySchema.parse({
          tenant_id: discoverySeed.tenant_id, project_fingerprint: discoverySeed.project_fingerprint,
          owner_id: discoverySeed.owner_id, account_id: discoverySeed.account_id,
          line_key: discoverySeed.line_key, provider_thread_id: thread,
        }) : undefined;
        const work = coordinator.receive(inbound, { threadId: thread, lineKey: options.lineKey,
          destination: options.target, activation: candidate }, provider?.sentAtMs);
        if (discoverySeed) syncScheduledDestination();
        return work.then(async result => {
          try { await projectConversation(thread); } catch { /* Repaired before the next model turn or on recovery. */ }
          return result;
        });
      },
      close(closeOptions: ApplicationCloseOptions = {}): Promise<void> {
        return closing ??= (async () => {
        closed = true;
        // Closing sockets aborts active /run requests; the coordinator then fences model turns.
        const stopped = server.listening
          ? new Promise<void>((resolve, reject) => {
              server.close(error => error ? reject(error) : resolve());
              server.closeAllConnections();
            })
          : Promise.resolve();
        for (const controller of unattendedControllers) controller.abort(new Error('runtime_shutdown'));
        let coordinatorError: unknown;
        let serverError: unknown;
        let unattendedError: unknown;
        try { await coordinator.close(); } catch (error) { coordinatorError = error; }
        try { await stopped; } catch (error) { serverError = error; }
        // Accepted scheduled sends must persist their receipts even after the caller's
        // socket closes. Never close the journal while a handler still owns a contact.
        await server.drainOperations();
        try { await Promise.allSettled([...unattendedRuns]); } catch (error) { unattendedError = error; }
        if (coordinatorError || serverError || unattendedError) {
          throw new AggregateError([coordinatorError, serverError, unattendedError].filter(Boolean), 'Application shutdown did not complete cleanly');
        }
        await projection?.drain();
        journal!.close();
        if (!closeOptions.retainOwnership) await release();
        })();
      },
    };
  } catch (error) {
    journal?.close();
    await release();
    throw error;
  }
}
