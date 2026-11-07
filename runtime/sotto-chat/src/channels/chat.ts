import { Chat } from "chat";
import type { Adapter, ChatConfig, Message, WebhookOptions } from "chat";

export interface DurableAdmissionInput {
  adapter: Adapter;
  threadId: string;
  /** Chat SDK normalized message, retaining the SDK-specific event at .raw. */
  message: Message;
  rawMessage: unknown;
}

export type DurableAdmissionDecision =
  | { admitted: true; handoff?: "sdk" | "coordinator" }
  | { admitted: false; reason?: string };

export type DurableAdmission = (input: DurableAdmissionInput) => Promise<DurableAdmissionDecision> | DurableAdmissionDecision;

/**
 * A failed admission rejects for an awaiting adapter (Telegram keeps the update for retry),
 * but Photon 3.2.0 drops this promise without a handler. Attach a passive observer so the
 * rejection can never become a process-fatal unhandled rejection; it logs a fixed,
 * content-free line (provider errors may carry message text or authenticated URLs).
 */
function observed(work: Promise<void>): Promise<void> {
  work.catch(() => {
    try { process.stderr.write('Sotto channel admission failed; the provider event was not admitted.\n'); }
    catch { /* logging cannot affect intake */ }
  });
  return work;
}

/**
 * Public Chat.processMessage ingress wrapper. Admission completes before the SDK can
 * deduplicate, lock, queue, or dispatch handlers. A coordinator handoff suppresses the
 * SDK route so one event cannot invoke both coordinator and Chat handlers.
 *
 * Provider acknowledgement is adapter-specific: Telegram awaits processMessage in its
 * polling path; Photon 3.2.0 calls it without awaiting from routeInbound, so an async
 * admission may still finish after Photon has returned from that callback. For Photon,
 * use a synchronous SQLite admission callback or own the Spectrum receive/ack boundary
 * when commit-before-ack is required.
 */
export class DurableAdmissionChat<
  TAdapters extends Record<string, Adapter> = Record<string, Adapter>,
  TState = Record<string, unknown>,
> extends Chat<TAdapters, TState> {
  readonly #admit: DurableAdmission;

  constructor(config: ChatConfig<TAdapters>, admit: DurableAdmission) {
    super(config);
    this.#admit = admit;
  }

  override processMessage(
    adapter: Adapter,
    threadId: string,
    messageOrFactory: Message | (() => Promise<Message>),
    options?: WebhookOptions,
  ): Promise<void> {
    if (typeof messageOrFactory === "function") {
      return observed(this.#admitLazy(adapter, threadId, messageOrFactory, options));
    }
    // Invoke admission synchronously before returning. A synchronous journal callback
    // can commit its SQLite transaction before a non-awaiting adapter returns upstream.
    let decision: Promise<DurableAdmissionDecision> | DurableAdmissionDecision;
    try {
      decision = this.#admit({ adapter, threadId, message: messageOrFactory, rawMessage: messageOrFactory.raw });
    } catch (error) {
      return observed(Promise.reject(error));
    }
    return observed(Promise.resolve(decision).then((result) => this.#handoff(adapter, threadId, messageOrFactory, options, result)));
  }

  async #admitLazy(
    adapter: Adapter,
    threadId: string,
    factory: () => Promise<Message>,
    options?: WebhookOptions,
  ): Promise<void> {
    const message = await factory();
    const decision = await this.#admit({ adapter, threadId, message, rawMessage: message.raw });
    await this.#handoff(adapter, threadId, message, options, decision);
  }

  async #handoff(adapter: Adapter, threadId: string, message: Message, options: WebhookOptions | undefined, decision: DurableAdmissionDecision): Promise<void> {
    if (!decision.admitted || decision.handoff === "coordinator") return;
    await super.processMessage(adapter, threadId, message, options);
  }
}
