import type { MediaOmission } from './media-policy.js';

export type Channel = "photon" | "telegram";
export type Acceptance = "accepted" | "rejected" | "unknown" | "not_attempted";
export interface Receipt { acceptance: Acceptance; providerMessageIds: string[]; reason?: string; providerSentAtMs?: number }
export type PhotonMediaReference = {
  version: 1; provider: 'photon'; messageId: string; path: Array<number | 'reply'>;
} & ({ contentKind: 'attachment'; attachmentId: string } | { contentKind: 'voice' });
export type ProviderMediaReference = PhotonMediaReference | {
  version: 1; provider: 'telegram'; fileId: string;
};
export interface Media {
  url?: string; data?: Uint8Array; mimeType?: string; filename?: string;
  sizeBytes?: number; oversize?: boolean;
  /** Persisted provider identity only; never a download URL, closure or credential. */
  source?: ProviderMediaReference;
  omission?: MediaOmission;
  /** One bounded aggregate marker preserves attachment counts beyond the download limit. */
  omittedCount?: number;
}
export interface Inbound { channel: Channel; eventId: string; accountId: string; senderId: string; isDirect: boolean; text?: string; media?: Media[]; receiptOnly?: boolean; replyBound?: boolean; replyToMessageId?: string; providerSentAtMs?: number }
export interface OwnerPolicy { channel: Channel; accountId: string; ownerId: string }
/** Internal durable-review exemption; never accepted from HTTP/model arguments. */
export interface Send { verbatim?: boolean; text?: string; media?: Media[]; threadId: string; signal?: AbortSignal }
export interface CredentialSource { telegramToken?: string; telegramLinkFile?: unknown; photonProjectId?: string; photonProjectSecret?: string; photonServerUrl?: string; photonApiKey?: string }
