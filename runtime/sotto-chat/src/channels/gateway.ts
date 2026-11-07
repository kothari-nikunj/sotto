import { DatabaseSync } from "node:sqlite";
import type { TelegramAdapter } from "@chat-adapter/telegram";
import type { iMessageAdapter } from "@photon-ai/chat-adapter-imessage";
import { Chat, type Adapter, type Message } from "chat";
import type { CredentialSource, Inbound } from "./types.js";
import { collectMediaDescriptors } from "./media-descriptors.js";
import { truncateUtf16 } from "./normalize.js";
import { resolvePhotonCredentials, resolveTelegramCredentials } from "./credentials.js";
import { createChannelSdkAdapter, photonIntakeSnapshot } from "./sdk.js";
import { DurableAdmissionChat } from "./chat.js";
import { PollerOwnership } from "./lifecycle.js";
import { SqliteChatStateAdapter } from "./state.js";

const MAX_INBOUND_TEXT = 16_000;
const PHOTON_LISTENER_WINDOW_MS = 2_147_000_000;
const processOwnership = new PollerOwnership();

export type GatewayAdapter = Adapter & Partial<Pick<TelegramAdapter, "startPolling" | "stopPolling" | "isPolling">> &
  Partial<Pick<iMessageAdapter, "startGatewayListener" | "app" | "decodeThreadId" | "encodeThreadId">>;
export interface GatewayApplication {
  receive(inbound: Inbound, provider?: { threadId: string; sentAtMs?: number }): Promise<unknown>;
  close(options?: { retainOwnership?: boolean }): Promise<void>;
}
export interface OwnerGatewayManifest {
  channel: "photon" | "telegram";
  accountId: string;
  ownerId: string;
  lineKey: string;
  providerThreadId?: string;
  expectedProviderLine?: string;
  sharedProjectRoute?: boolean;
  credentials: CredentialSource;
}
export interface StartGatewayOptions {
  manifest: OwnerGatewayManifest;
  application: GatewayApplication;
  database: DatabaseSync;
  /** Tests and supervised hosts can inject the already-explicit SDK instance. */
  adapter?: GatewayAdapter;
  /** Share one instance across locally managed consumers to enforce single ownership. */
  ownership?: PollerOwnership;
  /** Fence typing and drain its bounded local work after intake closes, before provider stop. */
  quiescePresence?(): Promise<void>;
}

export type GatewayLifecycle = "active" | "failed" | "stopped";

export class GatewayStartupError extends Error {
  constructor(cause: unknown, readonly providerStopped: boolean) {
    // Provider exceptions can contain authenticated URLs. Only our own fixed
    // listener status string is safe to surface through supervisor logs.
    super(cause instanceof Error && /^Photon gateway listener returned HTTP [1-5][0-9]{2}$/.test(cause.message)
      ? cause.message : 'Gateway startup failed');
    this.name = 'GatewayStartupError';
  }
}

/**
 * Start one configured owner line. Intake is admitted through the operational application
 * before Chat SDK routing; no provider message is posted from this module.
 */
export async function startOwnerGateway(options: StartGatewayOptions) {
  const { manifest } = options;
  validateManifest(manifest);
  const expectedOwner = manifest.channel === "telegram"
    ? resolveTelegramCredentials(manifest.credentials).ownerId
    : manifest.ownerId;
  if (expectedOwner !== manifest.ownerId) throw new Error("Channel credential owner does not match gateway owner");
  if (manifest.channel === "photon") {
    const credentials = resolvePhotonCredentials(manifest.credentials);
    if ((manifest.expectedProviderLine || manifest.sharedProjectRoute) && !credentials.projectId) {
      throw new Error('Photon first-contact binding requires Cloud credentials');
    }
  }
  const adapter = options.adapter ?? createChannelSdkAdapter(manifest.channel, manifest.credentials) as GatewayAdapter;
  // SQLite setup can throw (for example, on a closed or read-only database).
  // No poller has been touched yet, so do it before claiming process ownership.
  const state = new SqliteChatStateAdapter(options.database);
  const ownership = options.ownership ?? processOwnership;
  const release = ownership.claim(JSON.stringify([manifest.channel, manifest.accountId, manifest.lineKey]));
  const background = new Set<Promise<unknown>>();
  let accepting = true;
  let receivedEvents = 0;
  let handedOffEvents = 0;
  let lifecycle: GatewayLifecycle = "active";
  let closeAttempted = false;
  let providerStopped = false;
  let shutdownSignalObserved = false;
  let adapterInitialized = false;
  let photonListenerAttempted = false;
  let photonAppStopSucceeded = false;
  let photonAppStopPromise: Promise<void> | undefined;
  let photonDisconnectSucceeded = false;
  let photonDisconnectPromise: Promise<void> | undefined;
  let originalDisconnect: GatewayAdapter['disconnect'] = undefined;
  const stopPhotonApp = async (): Promise<void> => {
    const app = (adapter as GatewayAdapter).app;
    if (!app || typeof app.stop !== 'function') return;
    // Spectrum's second stop() returns immediately while its first is pending.
    // Retain and await the original public stop promise on every cleanup path.
    photonAppStopPromise ??= Promise.resolve().then(() => app.stop()).then(() => { photonAppStopSucceeded = true; });
    await photonAppStopPromise;
  };
  const photonStopConfirmed = () => !shutdownSignalObserved &&
    (photonAppStopSucceeded || photonDisconnectSucceeded || (adapterInitialized && !photonListenerAttempted));
  const restoreDisconnect = () => {
    if (manifest.channel === 'photon' && typeof originalDisconnect === 'function' && providerStopped) {
      adapter.disconnect = originalDisconnect;
    }
  };
  let listenerAbort: AbortController | undefined;
  let listenerTask: Promise<void> | undefined;
  let photonStartResolve: (() => void) | undefined;
  let photonStartReject: ((error: unknown) => void) | undefined;

  let chat: DurableAdmissionChat;
  try {
    originalDisconnect = adapter.disconnect;
    if (manifest.channel === 'photon' && typeof originalDisconnect === 'function') {
      // Chat.shutdown() invokes disconnect but swallows its rejection. Observe that
      // one public call without running a second disconnect against the same client.
      adapter.disconnect = () => photonAppStopSucceeded ? Promise.resolve() : photonDisconnectPromise ??= Promise.resolve()
        .then(() => originalDisconnect!.call(adapter))
        .then(() => { photonDisconnectSucceeded = true; });
    }
    chat = new DurableAdmissionChat({
      userName: "sotto",
      adapters: { [manifest.channel === "photon" ? "imessage" : "telegram"]: adapter },
      state,
      concurrency: "concurrent",
      logger: 'silent',
    }, ({ adapter: sourceAdapter, threadId, message }) => {
      receivedEvents++;
      // An edit reuses the original provider message ID with new text. Edits are not
      // re-processed: deny so the SDK acknowledges it instead of retrying a conflict forever.
      if (isTelegramEdit(manifest, message)) return { admitted: false, reason: "edited messages are not re-processed" };
      const inbound = normalizeOwnerMessage(manifest, sourceAdapter, threadId, message);
      if (!accepting || !inbound) return { admitted: false, reason: "not an owner direct message" };
      // receive() synchronously commits admission. Its turn continues independently of provider intake.
      const work = options.application.receive(inbound, { threadId,
        sentAtMs: nativePhotonIngressTime(message, threadId) });
      handedOffEvents++;
      const observed = Promise.resolve(work).catch(() => undefined);
      background.add(observed);
      void observed.finally(() => background.delete(observed));
      return { admitted: true, handoff: "coordinator" };
    });
  } catch (error) {
    // Nothing has initialized or started a listener yet. Undo any adapter
    // instrumentation before releasing the unconsumed ownership claim.
    try { if (manifest.channel === 'photon' && originalDisconnect) adapter.disconnect = originalDisconnect; }
    catch { /* the adapter rejected mutation; no provider consumer was started */ }
    release();
    throw error;
  }

  // Spectrum may install its signal handler before this gateway (recovery can
  // lazily build the app). Every signal listener runs synchronously before our
  // awaited close confirmation; record the signal because Spectrum's later
  // stopOnce call returns immediately while its first stop is still pending.
  const observeShutdownSignal = () => { shutdownSignalObserved = true; };
  if (manifest.channel === 'photon') {
    process.on('SIGINT', observeShutdownSignal);
    process.on('SIGTERM', observeShutdownSignal);
  }
  const removeShutdownObserver = () => {
    process.removeListener('SIGINT', observeShutdownSignal);
    process.removeListener('SIGTERM', observeShutdownSignal);
  };

  try {
    await chat.initialize();
    adapterInitialized = true;
    if (manifest.channel === "photon") {
      const photon = adapter as GatewayAdapter;
      if (typeof photon.startGatewayListener !== "function") throw new Error("Photon adapter does not expose startGatewayListener");
      listenerAbort = new AbortController();
      const firstStart = new Promise<void>((resolve, reject) => {
        photonStartResolve = resolve;
        photonStartReject = reject;
      });
      listenerTask = runPhotonListener(photon, listenerAbort.signal, () => { photonListenerAttempted = true; }, () => {
        photonStartResolve?.();
      }, (error) => photonStartReject?.(error));
      void listenerTask.catch(() => { lifecycle = "failed"; });
      await firstStart;
    } else {
      const telegram = adapter as GatewayAdapter;
      if (telegram.isPolling === false) throw new Error("Telegram adapter initialized without an active poller");
    }
    return {
      chat,
      adapter,
      /** Lifecycle state says what the SDK start/stop API reported, not whether messages are flowing. */
      get lifecycle(): GatewayLifecycle {
        // A terminated Spectrum broadcaster cannot be revived by repeating
        // startGatewayListener. Require a supervised restart, never report ready.
        return lifecycle === 'active' && (photonIntakeSnapshot(adapter)?.listenerStops ?? 0) > 0
          ? 'failed' : lifecycle;
      },
      get intake() { return { ...photonIntakeSnapshot(adapter), receivedEvents, handedOffEvents }; },
      /** Local consumer stop proof from public app.stop/disconnect; remote teardown can swallow errors. */
      get providerStopped(): boolean { return providerStopped; },
      providerFlow: "not_observed" as const,
      /** Explicit retention hook; subscriptions and non-expiring SDK state are preserved. */
      pruneExpiredState: () => state.pruneExpired(),
      async close() {
        if (lifecycle === "stopped") return;
        if (closeAttempted) throw new Error('Gateway close was already attempted');
        closeAttempted = true;
        accepting = false;
        let shutdownError: unknown;
        let applicationError: unknown;
        try {
          try { await options.quiescePresence?.(); }
          catch { shutdownError = new Error('Turn presence cleanup failed'); }
          if (manifest.channel === 'photon') {
            listenerAbort?.abort();
            try { if (listenerTask) await listenerTask; }
            catch { shutdownError = new Error('Photon listener stop failed'); }
            try { await stopPhotonApp(); }
            catch { shutdownError ??= new Error('Photon public app stop failed'); }
          } else {
            try {
              const telegram = adapter as GatewayAdapter;
              if (typeof telegram.stopPolling !== 'function') throw new Error('Telegram adapter does not expose stopPolling');
              await telegram.stopPolling();
              if (telegram.isPolling === true) throw new Error('Telegram polling remained active after stopPolling');
              providerStopped = true;
            } catch { shutdownError = new Error('Telegram poller stop failed'); }
          }
          try { await chat.shutdown(); } catch { shutdownError ??= new Error('Chat SDK shutdown failed'); }
          if (manifest.channel === 'photon') {
            providerStopped = photonStopConfirmed();
            if (!providerStopped) shutdownError ??= new Error('Photon provider stop is unconfirmed');
          }
          restoreDisconnect();
          // Stop/cancel the coordinator before joining its outstanding turn promises.
          try { await options.application.close({ retainOwnership: !providerStopped }); }
          catch { applicationError = new Error('Gateway application cleanup failed'); }
          await Promise.allSettled([...background]);
          if (shutdownError || applicationError) {
            lifecycle = "failed";
            throw new AggregateError([shutdownError, applicationError].filter(Boolean),
              manifest.channel === 'photon' && !providerStopped
                ? 'Photon provider stop is unconfirmed; gateway ownership retained'
                : 'Gateway cleanup did not complete cleanly');
          }
          lifecycle = "stopped";
        } finally {
          // If the provider listener could not be stopped, retain ownership to prevent a second poller.
          removeShutdownObserver();
          if (providerStopped) release();
        }
      },
    };
  } catch (error) {
    accepting = false;
    let shutdownError: unknown;
    try { await options.quiescePresence?.(); }
    catch { shutdownError = new Error('Turn presence cleanup failed'); }
    listenerAbort?.abort();
    if (manifest.channel === 'photon') {
      try { if (listenerTask) await listenerTask; }
      catch { shutdownError = new Error('Photon listener stop failed'); }
      try { await stopPhotonApp(); }
      catch { shutdownError ??= new Error('Photon public app stop failed'); }
    } else {
      try {
        const telegram = adapter as GatewayAdapter;
        if (typeof telegram.stopPolling !== 'function') throw new Error('Telegram adapter does not expose stopPolling');
        await telegram.stopPolling();
        if (telegram.isPolling === true) throw new Error('Telegram polling remained active after stopPolling');
        providerStopped = true;
      } catch { shutdownError = new Error('Telegram poller stop failed'); }
    }
    try { await chat.shutdown(); } catch { shutdownError ??= new Error('Chat SDK shutdown failed'); }
    if (manifest.channel === 'photon') providerStopped = photonStopConfirmed();
    restoreDisconnect();
    try { await options.application.close({ retainOwnership: !providerStopped }); } catch { /* preserve startup failure */ }
    await Promise.allSettled([...background]);
    removeShutdownObserver();
    if (providerStopped) release();
    throw new GatewayStartupError(error, providerStopped);
  }
}

function validateManifest(manifest: OwnerGatewayManifest): void {
  for (const [name, value] of Object.entries({
    accountId: manifest.accountId, ownerId: manifest.ownerId, lineKey: manifest.lineKey,
  })) {
    if (typeof value !== "string" || !value.trim()) throw new Error(`Gateway ${name} must be explicit`);
  }
  if ([manifest.providerThreadId !== undefined, manifest.expectedProviderLine !== undefined, manifest.sharedProjectRoute === true].filter(Boolean).length !== 1 ||
      (manifest.sharedProjectRoute && manifest.channel !== 'photon') ||
      (manifest.providerThreadId !== undefined && !manifest.providerThreadId.trim()) ||
      (manifest.expectedProviderLine !== undefined &&
        (manifest.channel !== 'photon' || !/^\+[1-9]\d{7,14}$/.test(manifest.expectedProviderLine)))) {
    throw new Error('Gateway destination mode is invalid');
  }
}

async function runPhotonListener(
  adapter: GatewayAdapter,
  signal: AbortSignal,
  attempted: () => void,
  started: () => void,
  failed: (error: unknown) => void,
): Promise<void> {
  while (!signal.aborted) {
    let lifetime: Promise<unknown> | undefined;
    try {
      attempted();
      const response = await adapter.startGatewayListener!({
        waitUntil: (promise) => { lifetime = Promise.resolve(promise); },
      }, PHOTON_LISTENER_WINDOW_MS, signal);
      if (!response.ok) throw new Error(`Photon gateway listener returned HTTP ${response.status}`);
      if (!lifetime) throw new Error("Photon gateway listener did not register its lifecycle promise");
      started();
      await lifetime;
    } catch (error) {
      if (!signal.aborted) failed(error);
      throw error;
    }
  }
}

function normalizeOwnerMessage(manifest: OwnerGatewayManifest, adapter: Adapter, threadId: string, message: Message): Inbound | null {
  if (manifest.providerThreadId && threadId !== manifest.providerThreadId) return null;
  if (message.threadId !== threadId) return null;
  try { if (typeof adapter.isDM !== "function" || !adapter.isDM(threadId)) return null; }
  catch { return null; }
  if (manifest.expectedProviderLine || manifest.sharedProjectRoute) {
    const photon = adapter as GatewayAdapter;
    if (typeof photon.decodeThreadId !== 'function' || typeof photon.encodeThreadId !== 'function') return null;
    try {
      const decoded = photon.decodeThreadId(threadId) as { chatGuid: string; phone?: string };
      if (photon.encodeThreadId(decoded) !== threadId || decoded.phone !== (manifest.sharedProjectRoute ? 'shared' : manifest.expectedProviderLine) ||
          !decoded.chatGuid.includes(';-;')) return null;
    } catch { return null; }
  }
  const raw = message.raw as Record<string, unknown> | null;
  if (receiptOnly(raw)) return null;
  if (manifest.channel === "photon" && message.author.isMe !== false) return null;
  const senderId = message.author.userId;
  if (senderId !== manifest.ownerId) return null;
  const text = truncateUtf16(message.text.trim(), MAX_INBOUND_TEXT);
  const media = collectMediaDescriptors(manifest.channel, message);
  if (!text && media.length === 0) return null;
  return {
    channel: manifest.channel,
    eventId: message.id,
    accountId: manifest.accountId,
    senderId,
    isDirect: true,
    text: text || undefined,
    media,
    receiptOnly: false,
    ...replyMetadata(message, raw, threadId),
  };
}

function isTelegramEdit(manifest: OwnerGatewayManifest, message: Message): boolean {
  if (manifest.channel !== "telegram") return false;
  const raw = message.raw as Record<string, unknown> | null;
  return message.metadata?.edited === true || (raw !== null && typeof raw === "object" && raw.edit_date !== undefined);
}

function replyMetadata(message: Message, raw: Record<string, unknown> | null, threadId: string) {
  const bound = message.replyTo !== undefined || Boolean(raw?.reply_to_message) || hasReplyContent(raw?.content);
  if (!bound) return {}; // Preserve ordinary admitted-event serialization.
  if (message.replyTo && message.replyTo.threadId !== threadId) return {replyBound: true};
  const candidates: string[] = [];
  const add = (id: unknown) => {
    if (typeof id === 'string' && id.length > 0 && id.length <= 512 && !/[\u0000-\u001f\u007f]/.test(id)) candidates.push(id);
  };
  if (message.replyTo?.threadId === threadId) add(message.replyTo.id);
  const visit = (value: unknown, depth = 0) => {
    if (!value || typeof value !== 'object' || depth > 8) return;
    const content = value as Record<string, unknown>;
    if (content.type === 'reply' && content.target && typeof content.target === 'object')
      add((content.target as Record<string, unknown>).id);
    if (content.type === 'group' && Array.isArray(content.items))
      for (const item of content.items) if (item && typeof item === 'object') visit((item as Record<string, unknown>).content, depth + 1);
  };
  visit(raw?.content);
  const ids = [...new Set(candidates)];
  const sentAt = message.metadata.dateSent?.getTime();
  return {replyBound: true, ...(ids.length === 1 ? {replyToMessageId: ids[0]} : {}),
    ...(Number.isSafeInteger(sentAt) && sentAt >= 946684800000 && sentAt <= 4102444800000 ? {providerSentAtMs: sentAt} : {})};
}

function hasReplyContent(value: unknown, depth = 0): boolean {
  if (!value || typeof value !== 'object' || depth > 8) return false;
  const content = value as Record<string, unknown>;
  if (content.type === 'reply') return true;
  return content.type === 'group' && Array.isArray(content.items)
    && content.items.some(item => item && typeof item === 'object'
      && hasReplyContent((item as Record<string, unknown>).content, depth + 1));
}

function receiptOnly(raw: Record<string, unknown> | null): boolean {
  if (!raw) return false;
  if (raw.receiptOnly === true || raw.isReceipt === true) return true;
  const content = raw.content && typeof raw.content === "object" ? raw.content as Record<string, unknown> : undefined;
  const kind = String(raw.type ?? raw.eventType ?? content?.type ?? "").toLowerCase();
  return ["read", "receipt", "delivery_receipt", "reaction", "typing"].includes(kind);
}

/** Only the native Spectrum event record carries event.occurredAt without fallback. */
function nativePhotonIngressTime(message: Message, threadId: string): number | undefined {
  const raw = message.raw as {id?: unknown; direction?: unknown; timestamp?: unknown} | undefined;
  if (message.threadId !== threadId || raw?.id !== message.id || raw.direction !== 'inbound'
      || !(raw.timestamp instanceof Date)) return undefined;
  const stamp = raw.timestamp.getTime();
  return Number.isSafeInteger(stamp) && stamp >= 946684800000 && stamp <= 4102444800000 ? stamp : undefined;
}
