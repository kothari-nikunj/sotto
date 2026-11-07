import { TelegramAdapter } from "@chat-adapter/telegram";
import { createiMessageAdapter, type iMessageAdapter } from "@photon-ai/chat-adapter-imessage";
import { ConsoleLogger } from "chat";
import { resolvePhotonCredentials, resolveTelegramCredentials } from "./credentials.js";
import type { CredentialSource } from "./types.js";

// Photon 3.2.0's waitUntil promise tracks a timer, not the message pump. Observe
// its fixed lifecycle messages without retaining provider arguments or errors.
const photonIntake = new WeakMap<object, PhotonIntakeLogger>();
class PhotonIntakeLogger extends ConsoleLogger {
  listenerStarts = 0;
  listenerStops = 0;
  streamErrors = 0;
  handlerErrors = 0;
  constructor() { super('silent'); }
  override child(): PhotonIntakeLogger { return this; }
  override info(message: string): void {
    if (message === 'Starting iMessage Gateway listener') this.listenerStarts++;
    if (message === 'iMessage Gateway listener stopped') this.listenerStops++;
  }
  override error(message: string): void {
    if (message === 'iMessage message stream error') this.streamErrors++;
    if (message === 'iMessage inbound handler error') this.handlerErrors++;
  }
}

export function photonIntakeSnapshot(adapter: object) {
  const value = photonIntake.get(adapter);
  return value ? { listenerStarts: value.listenerStarts, listenerStops: value.listenerStops,
    streamErrors: value.streamErrors, handlerErrors: value.handlerErrors } : undefined;
}

/** Telegram's inherited ingress hook otherwise sends typing before our journal admission. */
class OwnerAdmittedTelegramAdapter extends TelegramAdapter {
  protected override startTypingForPrivateMessage(): void {}
}

/** Importing this module never connects or reads ambient credentials. */
export function createTelegramSdkAdapter(source: CredentialSource) {
  const credentials = resolveTelegramCredentials(source);
  return new OwnerAdmittedTelegramAdapter({
    botToken: credentials.botToken,
    allowedUserIds: [credentials.ownerId],
    apiBaseUrl: "https://api.telegram.org",
    userName: "sotto",
    mentionOnReply: false,
    secretToken: "",
    allowUnverifiedWebhooks: false,
    mode: "polling",
    // Startup must not mutate a pre-existing webhook or discard its pending updates.
    longPolling: { deleteWebhook: false, dropPendingUpdates: false },
    logger: new ConsoleLogger('silent'),
  });
}

/** Construct the one explicitly credentialed adapter used by both the transport and gateway. */
export function createChannelSdkAdapter(channel: "photon" | "telegram", source: CredentialSource): TelegramAdapter | iMessageAdapter {
  return channel === "photon" ? createPhotonSdkAdapter(source) : createTelegramSdkAdapter(source);
}
export function createPhotonSdkAdapter(source: CredentialSource) {
  const credentials = resolvePhotonCredentials(source);
  const logger = new PhotonIntakeLogger();
  const adapter = createiMessageAdapter({
    local: false,
    projectId: credentials.projectId ?? '', projectSecret: credentials.projectSecret ?? '',
    serverUrl: credentials.serverUrl ?? '', apiKey: credentials.apiKey ?? '',
    phone: '', webhookSecret: '',
    logger,
  });
  photonIntake.set(adapter, logger);
  return adapter;
}
