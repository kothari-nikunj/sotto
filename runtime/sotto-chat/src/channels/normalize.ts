import type { Inbound, Media } from "./types.js";
import { MAX_INBOUND_MEDIA_BYTES } from './media-policy.js';

export const MAX_MEDIA_ITEMS = 4;
export const MAX_MEDIA_BYTES = 20 * 1024 * 1024;
export function normalizeMedia(items: readonly Media[] = []): Media[] {
  return items.map((item) => ({
    ...item, filename: item.filename?.replace(/[\r\n]/g, "_") || undefined,
    mimeType: item.mimeType?.toLowerCase(), url: item.url?.trim(),
    sizeBytes: item.sizeBytes ?? item.data?.byteLength,
    oversize: item.oversize || ((item.sizeBytes ?? item.data?.byteLength) !== undefined && (item.sizeBytes ?? item.data?.byteLength)! > MAX_MEDIA_BYTES),
  }));
}
export function normalizeInbound(event: Inbound): Inbound {
  return { ...event, text: event.text?.trim() || undefined, media: normalizeMedia(event.media).map(item => ({
    ...item, oversize: item.oversize || (item.sizeBytes !== undefined && item.sizeBytes > MAX_INBOUND_MEDIA_BYTES),
  })) };
}

/** Truncate to at most `max` UTF-16 code units without splitting a surrogate pair. */
export function truncateUtf16(text: string, max: number): string {
  if (text.length <= max) return text;
  const end = max > 0 && /[\ud800-\udbff]/.test(text[max - 1]!) ? max - 1 : Math.max(0, max);
  return text.slice(0, end);
}
