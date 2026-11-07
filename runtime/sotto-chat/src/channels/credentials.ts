import type { CredentialSource } from "./types.js";
export interface TelegramCredentials { botToken: string; ownerId: string }
export interface PhotonCredentials { projectId?: string; projectSecret?: string; serverUrl?: string; apiKey?: string }

/** Match trigger-receiver telegram-link.json semantics: token must match the selected bot. */
export function resolveTelegramCredentials(source: CredentialSource): TelegramCredentials {
  const token = source.telegramToken?.trim();
  const link = source.telegramLinkFile as { bot_token?: unknown; allowed_user?: unknown } | undefined;
  if (!token || typeof link?.bot_token !== "string" || link.bot_token !== token || (typeof link.allowed_user !== "number" && typeof link.allowed_user !== "string")) {
    throw new Error("Telegram credentials require an explicit token and matching owner link file");
  }
  if (!/^[1-9][0-9]{0,15}$/.test(String(link.allowed_user)) || !Number.isSafeInteger(Number(link.allowed_user))) {
    throw new Error("Telegram owner must be a positive safe integer user ID");
  }
  return { botToken: token, ownerId: String(link.allowed_user) };
}
/** Explicit credentials only. No process.env fallback and no ambiguous cloud/self-host combination. */
export function resolvePhotonCredentials(source: CredentialSource): PhotonCredentials {
  const cloud = Boolean(source.photonProjectId || source.photonProjectSecret);
  const selfHost = Boolean(source.photonServerUrl || source.photonApiKey);
  if (cloud && selfHost) throw new Error("Photon cloud and self-host credentials cannot be mixed");
  if (cloud && (!source.photonProjectId || !source.photonProjectSecret)) throw new Error("Photon cloud requires project ID and secret");
  if (selfHost && (!source.photonServerUrl || !source.photonApiKey)) throw new Error("Photon self-host requires server URL and API key");
  if (!cloud && !selfHost) throw new Error("Explicit Photon credentials are required");
  return cloud ? { projectId: source.photonProjectId, projectSecret: source.photonProjectSecret } : { serverUrl: source.photonServerUrl, apiKey: source.photonApiKey };
}
