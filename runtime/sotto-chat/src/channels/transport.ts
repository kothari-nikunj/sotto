import { attachment, group, text } from "@spectrum-ts/core";
import { imessage } from "@spectrum-ts/imessage";
import type { SpectrumInstance } from "@spectrum-ts/core";
import type { iMessageAdapter, iMessageThreadId } from "@photon-ai/chat-adapter-imessage";
import type { TelegramAdapter } from "@chat-adapter/telegram";
import type { ContentInput } from "@spectrum-ts/core";
import type { AdapterPostableMessage, Attachment as ChatAttachment } from "chat";
import { normalizeMedia } from "./normalize.js";
import { telegramTextParts } from './telegram-text.js';
import type { Media, Receipt, Send } from "./types.js";

export interface ChannelTransport { send(input: Send): Promise<Receipt> }
function ids(result: unknown): string[] {
  // Both pinned adapters return an outbound message with its provider id at
  // the root. Nested content IDs may identify uploaded attachments or derived
  // group parts; they do not independently prove a message was accepted.
  if (!result || typeof result !== "object" || Array.isArray(result)) return [];
  const id = "id" in result ? result.id : undefined;
  return typeof id === "string" && id.length <= 512 && id.trim() ? [id] : [];
}
async function attempt(run: () => Promise<unknown>, nativePhoton = false): Promise<Receipt> {
  try {
    const result = await run();
    const providerMessageIds = ids(result);
    const record = result && typeof result === 'object' ? result as Record<string, unknown> : {};
    const raw = record.raw && typeof record.raw === 'object' ? record.raw as Record<string, unknown> : record;
    // Original Spectrum sends use sent.dateCreated directly; fetched messages can
    // synthesize a date and must never enter this original-send receipt path.
    const stamp = nativePhoton && raw.direction === 'outbound' && raw.id === providerMessageIds[0]
      && raw.timestamp instanceof Date ? raw.timestamp.getTime() : NaN;
    return { acceptance: providerMessageIds.length ? "accepted" : "unknown", providerMessageIds,
      ...(Number.isSafeInteger(stamp) && stamp >= 946684800000 && stamp <= 4102444800000
        ? {providerSentAtMs: stamp} : {}) };
  } catch {
    // A thrown SDK/network error is not no-contact proof. The pinned adapters
    // do not expose an authoritative accepted/rejected result on this path.
    return { acceptance: 'unknown', providerMessageIds: [], reason: 'provider outcome uncertain' };
  }
}
export function createTelegramTransport(adapter: Pick<TelegramAdapter, "postMessage">): ChannelTransport {
  return { send: async ({ threadId, text: body = "", media = [], signal }) => {
    const files = normalizeMedia(media);
    if (files.length > 4 || files.some((m) => (!m.url && !m.data) || m.oversize)) return Promise.resolve({ acceptance: "rejected", providerMessageIds: [], reason: "invalid media manifest" });
    if (!body.trim() && !files.length) return Promise.resolve({ acceptance: "not_attempted", providerMessageIds: [], reason: "empty payload" });
    const parts = telegramTextParts(body, files.length > 0);
    if (!parts) return { acceptance: 'rejected', providerMessageIds: [], reason: 'Telegram text exceeds safe message limits' };
    const providerMessageIds: string[] = [];
    for (const raw of parts) {
      if (signal?.aborted) return {acceptance: providerMessageIds.length ? 'unknown' : 'not_attempted',
        providerMessageIds, reason: 'delivery cancelled before next provider contact'};
      const receipt = await attempt(() => adapter.postMessage(threadId, {
        raw,
        attachments: files.map((m): ChatAttachment => ({ type: mediaType(m.mimeType), url: m.url || undefined, data: m.data ? Buffer.from(m.data) : undefined, mimeType: m.mimeType, name: m.filename, size: m.sizeBytes })),
      } satisfies AdapterPostableMessage));
      // A contacted part with no new root receipt makes the entire immutable
      // dispatch uncertain. Stop here; the journal must hold it, never replay it.
      if (receipt.acceptance !== 'accepted' || providerMessageIds.includes(receipt.providerMessageIds[0]!))
        return { ...receipt, acceptance: 'unknown', providerMessageIds };
      providerMessageIds.push(...receipt.providerMessageIds);
    }
    return { acceptance: 'accepted', providerMessageIds };
  } };
}
export interface PhotonGallerySpace { send(content: unknown): Promise<unknown> }
export interface PhotonAdapterLike extends Pick<iMessageAdapter, "postMessage" | "decodeThreadId" | "encodeThreadId"> { app: SpectrumInstance | null }
/** Offline seam matching the public imessage(app).space.get(chatGuid, { phone }) namespace. */
export interface PhotonTransportOptions {
  /** May be asynchronous: the canonical formatter runs in a child process off the event loop. */
  formatText?: (text: string) => string | Promise<string>;
  sharedProjectThread?: () => string | undefined;
  gallerySpace?: (app: SpectrumInstance, chatGuid: string, phone?: string) => Promise<PhotonGallerySpace>;
}
function decodedPhotonDestination(adapter: PhotonAdapterLike, threadId: string, sharedProjectThread?: () => string | undefined): iMessageThreadId {
  const decoded = adapter.decodeThreadId(threadId);
  if (sharedProjectThread && (sharedProjectThread() !== threadId || decoded.phone !== 'shared'))
    throw new Error('Photon shared destination is not bound');
  if (!decoded || typeof decoded.chatGuid !== "string" || !decoded.chatGuid.trim() ||
      (decoded.phone !== undefined && !/^\+[1-9]\d{7,14}$/.test(decoded.phone) &&
        !(decoded.phone === 'shared' && sharedProjectThread?.() === threadId)) ||
      adapter.encodeThreadId(decoded) !== threadId) {
    throw new Error("Photon gallery destination is invalid");
  }
  return decoded;
}
export function createPhotonTransport(adapter: PhotonAdapterLike, options: PhotonTransportOptions = {}): ChannelTransport {
  return { send: async ({ threadId, text: body = "", media = [], verbatim = false, signal }) => {
    if (options.sharedProjectThread) {
      try { decodedPhotonDestination(adapter, threadId, options.sharedProjectThread); }
      catch { return Promise.resolve({ acceptance: 'rejected', providerMessageIds: [], reason: 'unbound shared destination' }); }
    }
    const cancelled = (): Receipt => ({acceptance: 'not_attempted', providerMessageIds: [], reason: 'cancelled before provider contact'});
    if (signal?.aborted) return cancelled();
    try { body = !verbatim && options.formatText ? await options.formatText(body) : body; }
    catch { return Promise.resolve({acceptance: 'not_attempted', providerMessageIds: [], reason: 'Photon text formatting unavailable'}); }
    if (signal?.aborted) return cancelled();
    // A formatter seam must not widen the immutable shared route binding.
    if (options.sharedProjectThread) {
      try { decodedPhotonDestination(adapter, threadId, options.sharedProjectThread); }
      catch { return Promise.resolve({acceptance: 'not_attempted', providerMessageIds: [], reason: 'shared destination changed'}); }
    }
    const files = normalizeMedia(media);
    if (files.length > 4 || files.some((m) => (!m.url && !m.data) || m.oversize)) return Promise.resolve({ acceptance: "rejected", providerMessageIds: [], reason: "invalid gallery manifest" });
    if (!body.trim() && !files.length) return Promise.resolve({ acceptance: "not_attempted", providerMessageIds: [], reason: "empty payload" });
    if (!files.length) return attempt(() => adapter.postMessage(threadId, body), true);
    let destination: iMessageThreadId;
    try { destination = decodedPhotonDestination(adapter, threadId, options.sharedProjectThread); }
    catch { return Promise.resolve({ acceptance: "rejected", providerMessageIds: [], reason: "invalid gallery destination" }); }
    try {
      if (!adapter.app) throw new Error("Photon app is not initialized");
      const resolveSpace = options.gallerySpace ?? (async (app, chatGuid, phone) => {
        const namespace = imessage(app).space;
        return phone ? namespace.get(chatGuid, { phone }) : namespace.get(chatGuid);
      });
      const space = await resolveSpace(adapter.app, destination.chatGuid, destination.phone);
      if (signal?.aborted) return cancelled();
      const parts: ContentInput[] = body.trim()
        ? [text(body), ...files.map(photonAttachment)]
        : files.map(photonAttachment);
      const content = group(...(parts as [ContentInput, ContentInput, ...ContentInput[]]));
      return attempt(() => adapter.app!.send(space as never, content), true);
    } catch {
      return {acceptance: 'unknown', providerMessageIds: [], reason: 'provider outcome uncertain'};
    }
  } };
}
function photonAttachment(file: Media): ContentInput {
  const input = file.data ? Buffer.from(file.data) : new URL(file.url!);
  return attachment(input, { mimeType: file.mimeType,
    name: file.filename || (file.mimeType === 'image/png' ? 'image.png' : undefined) });
}
function mediaType(mimeType?: string): ChatAttachment["type"] {
  if (mimeType?.startsWith("image/")) return "image";
  if (mimeType?.startsWith("video/")) return "video";
  if (mimeType?.startsWith("audio/")) return "audio";
  return "file";
}
