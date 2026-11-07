import type { TelegramAdapter } from '@chat-adapter/telegram';
import type { iMessageAdapter } from '@photon-ai/chat-adapter-imessage';
import { imessage } from '@spectrum-ts/imessage';
import type { Message as SpectrumMessage, Space } from '@spectrum-ts/core';

const PRESENCE_DELAY_MS = 2_000;
const PROVIDER_DEADLINE_MS = 1_000;

export interface PresenceProvider {
  start(threadId: string, isCurrent: () => boolean): Promise<boolean>;
  stop?(threadId: string): Promise<void>;
}

export interface PresenceOptions {
  provider: PresenceProvider;
  delayMs?: number;
  providerDeadlineMs?: number;
  clock?: PresenceClock;
}

export interface PresenceClock {
  setTimeout(callback: () => void, delayMs: number): { unref?(): void };
  clearTimeout(timer: { unref?(): void }): void;
}
const systemClock: PresenceClock = {
  setTimeout: (callback, delayMs) => setTimeout(callback, delayMs),
  clearTimeout: timer => clearTimeout(timer as ReturnType<typeof setTimeout>),
};

interface Turn {
  runId: string;
  signal: AbortSignal;
  isCurrent(): boolean;
  timer?: { unref?(): void };
  started: boolean;
}
interface Thread {
  current?: Turn;
  tail: Promise<void>;
  poisoned: boolean;
}

/** Best-effort, turn-owned typing. Never waits on a provider during model cleanup. */
export class TurnPresence {
  readonly #threads = new Map<string, Thread>();
  readonly #provider: PresenceProvider;
  readonly #delay: number;
  readonly #deadline: number;
  readonly #clock: PresenceClock;
  #closed = false;
  #closing?: Promise<void>;

  constructor(options: PresenceOptions) {
    this.#provider = options.provider;
    this.#delay = options.delayMs ?? PRESENCE_DELAY_MS;
    this.#deadline = options.providerDeadlineMs ?? PROVIDER_DEADLINE_MS;
    this.#clock = options.clock ?? systemClock;
    if (!Number.isFinite(this.#delay) || this.#delay < 0 || !Number.isFinite(this.#deadline) || this.#deadline <= 0) {
      throw new Error('Presence timing must be finite and positive');
    }
  }

  startTurnPresence(threadId: string, runId: string, signal: AbortSignal, isCurrent: () => boolean): () => void {
    if (this.#closed) return () => {};
    let thread = this.#threads.get(threadId);
    if (!thread) {
      thread = { tail: Promise.resolve(), poisoned: false };
      this.#threads.set(threadId, thread);
    }
    if (thread.current) this.#end(threadId, thread, thread.current);
    const turn: Turn = { runId, signal, isCurrent, started: false };
    thread.current = turn;
    const onAbort = () => this.#end(threadId, thread!, turn);
    signal.addEventListener('abort', onAbort, { once: true });
    if (!thread.poisoned && !signal.aborted && isCurrent()) this.#schedule(threadId, thread, turn);
    else onAbort();
    return () => { signal.removeEventListener('abort', onAbort); this.#end(threadId, thread!, turn); };
  }

  /** Fence new pulses synchronously, then drain only bounded local work before provider stop. */
  close(): Promise<void> {
    if (this.#closing) return this.#closing;
    this.#closed = true;
    for (const [id, thread] of this.#threads) {
      if (thread.current) this.#end(id, thread, thread.current);
    }
    this.#closing = Promise.all([...this.#threads.values()].map(thread => thread.tail)).then(() => undefined);
    return this.#closing;
  }

  #live(thread: Thread, turn: Turn): boolean {
    return !this.#closed && thread.current === turn && !thread.poisoned && !turn.signal.aborted && turn.isCurrent();
  }

  #schedule(id: string, thread: Thread, turn: Turn): void {
    if (!this.#live(thread, turn)) return;
    turn.timer = this.#clock.setTimeout(() => {
      turn.timer = undefined;
      this.#queue(thread, async () => {
        if (!this.#live(thread, turn)) return;
        const operation = Promise.resolve().then(() => this.#provider.start(id, () => this.#live(thread, turn)));
        const result = await this.#bounded(thread, operation, async outcome => {
          // Rejected/unknown starts may still have reached the provider. A
          // timed-out start is compensated only after its original call settles.
          if (outcome !== false) await this.#bounded(thread, Promise.resolve().then(() => this.#provider.stop?.(id)));
        });
        if (result === true) {
          if (this.#live(thread, turn)) turn.started = true;
          else await this.#bounded(thread, Promise.resolve().then(() => this.#provider.stop?.(id)));
        }
        if (this.#live(thread, turn)) this.#schedule(id, thread, turn);
      });
    }, this.#delay);
    turn.timer.unref?.();
  }

  #end(id: string, thread: Thread, turn: Turn): void {
    if (turn.timer) this.#clock.clearTimeout(turn.timer);
    turn.timer = undefined;
    if (thread.current !== turn) return;
    thread.current = undefined;
    if (turn.started && this.#provider.stop) {
      this.#queue(thread, async () => {
        await this.#bounded(thread, Promise.resolve().then(() => this.#provider.stop!(id)));
      });
    }
  }

  #queue(thread: Thread, work: () => Promise<void>): void {
    thread.tail = thread.tail.then(work, work).catch(() => { thread.poisoned = true; });
  }

  async #bounded<T>(thread: Thread, operation: Promise<T>, onUncertain?: (value?: T) => Promise<void>): Promise<T | undefined> {
    let timeout: { unref?(): void } | undefined;
    const timedOut = Symbol('timed-out');
    const deadline = new Promise<typeof timedOut>(resolve => { timeout = this.#clock.setTimeout(() => resolve(timedOut), this.#deadline); timeout.unref?.(); });
    try {
      const result = await Promise.race([operation, deadline]);
      if (result === timedOut) {
        thread.poisoned = true;
        if (onUncertain) void operation.then(value => onUncertain(value), () => onUncertain()).catch(() => undefined);
        else void operation.catch(() => undefined);
        return undefined;
      }
      return result;
    } catch {
      thread.poisoned = true;
      if (onUncertain) await onUncertain();
      return undefined;
    } finally { if (timeout) this.#clock.clearTimeout(timeout); }
  }
}

/** Uses only the public Spectrum instance and public Space typing methods. */
type TypingSpace = Pick<Space, 'startTyping' | 'stopTyping'>;
export interface PhotonPresenceOptions {
  /** Offline seam; production resolves through the public Spectrum space namespace. */
  resolveSpace?(app: NonNullable<iMessageAdapter['app']>, chatGuid: string, phone?: string): Promise<TypingSpace>;
}
export function photonPresenceProvider(adapter: Pick<iMessageAdapter, 'app' | 'decodeThreadId'>,
  options: PhotonPresenceOptions = {}): PresenceProvider {
  const spaces = new Map<string, TypingSpace>();
  const resolveSpace = options.resolveSpace ?? (async (app, chatGuid, phone) => {
    const namespace = imessage(app).space;
    return phone ? namespace.get(chatGuid, { phone }) : namespace.get(chatGuid);
  });
  return {
    async start(threadId, isCurrent) {
      if (!adapter.app) return false;
      const { chatGuid, phone } = adapter.decodeThreadId(threadId);
      const space = await resolveSpace(adapter.app, chatGuid, phone);
      if (!isCurrent()) return false;
      spaces.set(threadId, space);
      await space.startTyping();
      return true;
    },
    async stop(threadId) {
      const space = spaces.get(threadId);
      if (!space) return;
      spaces.delete(threadId);
      await space.stopTyping();
    },
  };
}

/** Telegram's public adapter has startTyping but no matching stop primitive. */
export function telegramPresenceProvider(adapter: Pick<TelegramAdapter, 'startTyping'>): PresenceProvider {
  return { async start(threadId, isCurrent) {
    if (!isCurrent()) return false;
    await adapter.startTyping(threadId);
    return true;
  } };
}

/** Local request hint for the Photon processing tapback; never an effect claim. */
export function processingTapback(text: string): string {
  const value = text.toLowerCase().replace(/\s+/g, ' ').trim().slice(0, 512);
  if (/^(?:thanks|thank you|thank you so much|thanks so much|appreciate it)[.!\s🙏❤️]*$/u.test(value)) return '❤️';
  for (const [pattern, emoji] of [
    [/\b(?:draft|write|rewrite|compose|wording|reply)\b/, '📝'],
    [/\b(?:remember|forget|memory|memories)\b/, '🧠'],
    [/\b(?:research|look up|lookup|find out|background|prep|prepare|who is|who.s|what do (?:i|you) know)\b/, '🔎'],
    [/\b(?:calendar|schedule|reschedule|availability|available|meeting|meetings|appointment)\b/, '📅'],
  ] as const) if (pattern.test(value)) return emoji;
  return '💭';
}

export type FeedbackStageName = 'read' | 'processing' | 'completion';
export type FeedbackErrorClass = 'target_unavailable' | 'provider_unavailable' | 'provider_error';
export interface FeedbackStageSnapshot {
  stage: FeedbackStageName;
  outcome: 'not_started' | 'running' | 'success' | 'timeout' | 'error' | 'skipped';
  elapsed_ms: number | null;
  step: 'lookup' | 'effect' | null;
  lookup_ms: number | null;
  effect_ms: number | null;
  error_class: FeedbackErrorClass | null;
  late_outcome: 'success' | 'error' | null;
}
export type PhotonFeedbackSnapshot = {available: false} | {
  available: true; scope: 'latest_fresh_turn'; turn_sequence: number; age_ms: number;
  reply_accepted: boolean; work_accepted: boolean; uncertain: boolean; stages: FeedbackStageSnapshot[];
};
type FeedbackObservation = (step: 'lookup' | 'effect', outcome: 'running' | 'success' | 'error' | 'skipped', error?: FeedbackErrorClass) => void;
const elapsed = (start: number) => Math.min(86_400_000, Math.max(0, Math.round(performance.now() - start)));
class FeedbackFailure extends Error {
  constructor(readonly category: FeedbackErrorClass) {super('Owner feedback unavailable');}
}
interface FeedbackTurn { createdAt: number; sequence: number; stages: FeedbackStageSnapshot[]; eventId: string; emoji: string; control: boolean; accepted: boolean; replyAccepted: boolean; workAccepted: boolean; uncertain: boolean; processingStarted: boolean; completionStarted: boolean; isCurrent(): boolean }
interface FeedbackThread { current?: FeedbackTurn; tail: Promise<void> }

interface PhotonFeedbackProvider {
  markRead(threadId: string, messageId: string, isCurrent: () => boolean, observe?: FeedbackObservation): Promise<void>;
  addReaction(threadId: string, messageId: string, emoji: string, isCurrent: () => boolean, observe?: FeedbackObservation): Promise<void>;
}

/** The Chat adapter restricts reaction names; Spectrum's public Message supports Unicode. */
export function photonFeedbackProvider(adapter: Pick<iMessageAdapter, 'fetchMessage'>): PhotonFeedbackProvider {
  async function target(threadId: string, messageId: string): Promise<SpectrumMessage> {
    const message = await adapter.fetchMessage(threadId, messageId);
    if (!message || message.id !== messageId || message.threadId !== threadId || message.author.isMe) {
      throw new FeedbackFailure('target_unavailable');
    }
    const raw = message.raw as SpectrumMessage;
    if (!raw || typeof raw.read !== 'function' || typeof raw.react !== 'function') {
      throw new FeedbackFailure('provider_unavailable');
    }
    return raw;
  }
  async function perform(threadId: string, messageId: string, isCurrent: () => boolean,
                         effect: (message: SpectrumMessage) => Promise<unknown>, observe?: FeedbackObservation): Promise<void> {
    const note: FeedbackObservation = (step, outcome, error) => {
      try {observe?.(step, outcome, error);} catch { /* observation cannot affect provider work */ }
    };
    note('lookup', 'running');
    let message: SpectrumMessage;
    try {message = await target(threadId, messageId); note('lookup', 'success');}
    catch (error) {note('lookup', 'error', error instanceof FeedbackFailure ? error.category : 'provider_error'); throw error;}
    if (!isCurrent()) {note('effect', 'skipped'); return;}
    note('effect', 'running');
    try {await effect(message); note('effect', 'success');}
    catch (error) {note('effect', 'error', 'provider_error'); throw error;}
  }
  return {
    markRead: (threadId, messageId, isCurrent, observe) => perform(threadId, messageId, isCurrent, message => message.read(), observe),
    addReaction: (threadId, messageId, emoji, isCurrent, observe) => perform(threadId, messageId, isCurrent, message => message.react(emoji), observe),
  };
}

/** Fresh admission only. No recovery/retry of uncertain provider feedback. */
export class PhotonOwnerFeedback {
  private readonly threads = new Map<string, FeedbackThread>();
  private closed = false;
  private latest?: FeedbackTurn;
  private sequence = 0;
  constructor(private readonly adapter: PhotonFeedbackProvider,
              private readonly deadlineMs = PROVIDER_DEADLINE_MS) {}

  admitted(inbound: {eventId: string; text?: string}, threadId: string, isCurrent: () => boolean): void {
    if (this.closed) return;
    const thread = this.threads.get(threadId) ?? {tail: Promise.resolve()};
    this.threads.set(threadId, thread);
    const turn: FeedbackTurn = {createdAt: performance.now(), sequence: ++this.sequence,
      stages: (['read', 'processing', 'completion'] as const).map(stage => ({stage, outcome: 'not_started', elapsed_ms: null,
        step: null, lookup_ms: null, effect_ms: null, error_class: null, late_outcome: null})), eventId: inbound.eventId, emoji: processingTapback(inbound.text ?? ''),
      control: ['/stop', '/new'].includes(inbound.text?.trim().toLowerCase() ?? ''),
      accepted: false, replyAccepted: false, workAccepted: false, uncertain: false, processingStarted: false, completionStarted: false, isCurrent};
    thread.current = turn;
    this.latest = turn;
    this.queue(thread, turn, async () => {
      if (!this.live(thread, turn)) {this.skip(turn, 'read'); this.skip(turn, 'processing'); return;}
      await this.bounded(turn, 'read', observe => this.adapter.markRead(threadId, turn.eventId, () => this.live(thread, turn), observe), () => this.resume(thread, turn, threadId));
      await this.remaining(thread, turn, threadId);
    });
  }

  accepted(inbound: {eventId: string}, threadId: string): void {
    this.accept(inbound, threadId, 'reply');
  }

  workAccepted(inbound: {eventId: string}, threadId: string): void {
    this.accept(inbound, threadId, 'work');
  }

  private accept(inbound: {eventId: string}, threadId: string, kind: 'reply' | 'work'): void {
    const thread = this.threads.get(threadId);
    const turn = thread?.current;
    if (!thread || !turn || turn.eventId !== inbound.eventId || this.closed) return;
    turn.accepted = true;
    if (kind === 'reply') turn.replyAccepted = true;
    else turn.workAccepted = true;
    this.queue(thread, turn, () => this.remaining(thread, turn, threadId));
  }

  private resume(thread: FeedbackThread, turn: FeedbackTurn, threadId: string): void {
    if (this.closed || thread.current !== turn || !(turn.accepted || turn.isCurrent())) return;
    turn.uncertain = false;
    this.queue(thread, turn, () => this.remaining(thread, turn, threadId));
  }

  private async remaining(thread: FeedbackThread, turn: FeedbackTurn, threadId: string): Promise<void> {
    if (!this.live(thread, turn)) {this.skip(turn, 'processing'); this.skip(turn, 'completion'); return;}
    if (turn.control) this.skip(turn, 'processing');
    else if (!turn.processingStarted) {
      turn.processingStarted = true;
      await this.bounded(turn, 'processing', observe => this.adapter.addReaction(threadId, turn.eventId, turn.emoji, () => this.live(thread, turn), observe), () => this.resume(thread, turn, threadId));
    }
    if (!this.live(thread, turn) || !turn.accepted || turn.emoji === '❤️') {
      if (turn.emoji === '❤️' || !this.live(thread, turn)) this.skip(turn, 'completion');
      return;
    }
    if (!turn.completionStarted) {
      turn.completionStarted = true;
      await this.bounded(turn, 'completion', observe => this.adapter.addReaction(threadId, turn.eventId, '✅', () => this.live(thread, turn), observe), () => this.resume(thread, turn, threadId));
    }
  }

  async close(): Promise<void> {
    this.closed = true;
    await Promise.all([...this.threads.values()].map(thread => thread.tail));
  }

  private live(thread: FeedbackThread, turn: FeedbackTurn): boolean {
    return !this.closed && !turn.uncertain && thread.current === turn && (turn.accepted || turn.isCurrent());
  }
  private queue(thread: FeedbackThread, turn: FeedbackTurn, work: () => Promise<void>): void {
    thread.tail = thread.tail.then(work, work).catch(() => {turn.uncertain = true;});
  }
  snapshot(): PhotonFeedbackSnapshot {
    const turn = this.latest;
    return turn ? {available: true, scope: 'latest_fresh_turn', turn_sequence: turn.sequence,
      age_ms: elapsed(turn.createdAt), reply_accepted: turn.replyAccepted, work_accepted: turn.workAccepted, uncertain: turn.uncertain,
      stages: turn.stages.map(stage => ({...stage}))} : {available: false};
  }
  private skip(turn: FeedbackTurn, name: FeedbackStageName): void {
    const stage = turn.stages.find(value => value.stage === name)!;
    if (stage.outcome === 'not_started') stage.outcome = 'skipped';
  }
  private async bounded(turn: FeedbackTurn, name: FeedbackStageName, call: (observe: FeedbackObservation) => Promise<void>, lateEffectSuccess?: () => void): Promise<void> {
    const stage = turn.stages.find(value => value.stage === name)!;
    const started = performance.now();
    let stepStarted = started;
    let effectSkipped = false;
    let effectSucceeded = false;
    stage.outcome = 'running';
    const observe: FeedbackObservation = (step, outcome, error) => {
      try {
        if (step !== 'lookup' && step !== 'effect') return;
        if (!['running', 'success', 'error', 'skipped'].includes(outcome)) return;
        if (outcome === 'running') {stage.step = step; stepStarted = performance.now();}
        else if (outcome === 'skipped') {
          if (step === 'effect' && stage.outcome !== 'timeout') {effectSkipped = true; stage.outcome = 'skipped';}
        } else {
          if (step === 'effect' && outcome === 'success') effectSucceeded = true;
          if (step === 'lookup') stage.lookup_ms = elapsed(stepStarted);
          else stage.effect_ms = elapsed(stepStarted);
          if (error) stage.error_class = ['target_unavailable', 'provider_unavailable', 'provider_error'].includes(error) ? error : 'provider_error';
        }
      } catch { /* diagnostics cannot affect feedback */ }
    };
    let timer: ReturnType<typeof setTimeout> | undefined;
    const operation = Promise.resolve().then(() => call(observe));
    try {
      const completed = await Promise.race([operation.then(() => true),
        new Promise<false>(resolve => {timer = setTimeout(() => resolve(false), this.deadlineMs);})]);
      stage.elapsed_ms = elapsed(started);
      if (!completed) {
        turn.uncertain = true;
        stage.outcome = 'timeout';
        void operation.then(() => {
          stage.late_outcome = 'success';
          if (effectSucceeded) lateEffectSuccess?.();
        }, () => {stage.late_outcome = 'error';});
      } else stage.outcome = effectSkipped ? 'skipped' : 'success';
    } catch {
      turn.uncertain = true;
      stage.outcome = 'error'; stage.elapsed_ms = elapsed(started);
      stage.error_class ??= 'provider_error';
    } finally { if (timer) clearTimeout(timer); }
  }
}
