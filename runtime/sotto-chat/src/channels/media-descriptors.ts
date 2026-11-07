import type { Message } from 'chat';
import { ALLOWED_INBOUND_MEDIA_TYPES, MAX_INBOUND_MEDIA_ITEMS, normalizeMediaMime } from './media-policy.js';
import type { Channel, Media, PhotonMediaReference } from './types.js';

const MAX_REFERENCE_LENGTH = 512;
const MAX_FILENAME_LENGTH = 255;
const MAX_CONTENT_DEPTH = 8;
const MAX_CONTENT_NODES = 128;

type RecordValue = Record<string, unknown>;
export interface PhotonMediaContent {
  type: 'attachment' | 'voice';
  id?: string;
  name?: string;
  mimeType?: string;
  size?: number;
  stream?: () => Promise<ReadableStream<Uint8Array>>;
}

function record(value: unknown): RecordValue | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : null;
}

function referenceId(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 && value.length <= MAX_REFERENCE_LENGTH &&
    !/[\x00-\x1f\x7f]/.test(value) ? value : null;
}

function metadata(value: RecordValue): Pick<Media, 'mimeType' | 'filename' | 'sizeBytes'> {
  const mimeType = normalizeMediaMime(value.mimeType);
  const name = value.name;
  const filename = typeof name === 'string'
    ? name.slice(0, MAX_FILENAME_LENGTH).replace(/[\x00-\x1f\x7f]/g, '_') : undefined;
  const size = value.size;
  const sizeBytes = typeof size === 'number' && Number.isSafeInteger(size) && size >= 0 ? size : undefined;
  return { mimeType, filename, sizeBytes };
}

function descriptor(value: RecordValue, source: Media['source']): Media {
  const result = metadata(value);
  if (!source) return { ...result, omission: 'unavailable' };
  if (!result.mimeType || !ALLOWED_INBOUND_MEDIA_TYPES.has(result.mimeType)) {
    return { ...result, omission: 'unsupported_type' };
  }
  return { ...result, source };
}

function tooMany(count: number): Media[] {
  return count > MAX_INBOUND_MEDIA_ITEMS
    ? [{ omission: 'too_many', omittedCount: Math.min(count - MAX_INBOUND_MEDIA_ITEMS, Number.MAX_SAFE_INTEGER) }]
    : [];
}

interface ContentLeaf { content: RecordValue; path: Array<number | 'reply'> }

function photonLeaves(content: unknown): { leaves: ContentLeaf[]; malformed: boolean } {
  const leaves: ContentLeaf[] = [];
  const seen = new WeakSet<object>();
  let visited = 0;
  let malformed = false;
  function visit(value: unknown, path: Array<number | 'reply'>): void {
    if (malformed) return;
    const node = record(value);
    if (!node || path.length > MAX_CONTENT_DEPTH || ++visited > MAX_CONTENT_NODES || seen.has(node)) {
      malformed = true;
      return;
    }
    seen.add(node);
    if (node.type === 'attachment' || node.type === 'voice') {
      leaves.push({ content: node, path });
    } else if (node.type === 'reply') {
      if (node.content !== undefined && node.content !== null) visit(node.content, [...path, 'reply']);
    } else if (node.type === 'group') {
      if (!Array.isArray(node.items) || node.items.length > MAX_CONTENT_NODES) { malformed = true; return; }
      node.items.forEach((item, index) => {
        if (malformed) return;
        const groupItem = record(item);
        if (!groupItem) { malformed = true; return; }
        visit(groupItem.content, [...path, index]);
      });
    }
  }
  visit(content, []);
  return { leaves, malformed };
}

/** Persist provider references from an admitted owner message; never fetch or store provider objects. */
export function collectMediaDescriptors(channel: Channel, message: Pick<Message, 'id' | 'attachments' | 'raw'>): Media[] {
  const attachments = Array.isArray(message.attachments) ? message.attachments : [];
  if (channel === 'telegram') {
    return [
      ...attachments.slice(0, MAX_INBOUND_MEDIA_ITEMS).map((item) => {
        const value = record(item) ?? {};
        const fileId = referenceId(record(value.fetchMetadata)?.fileId);
        return descriptor(value, fileId ? { version: 1, provider: 'telegram', fileId } : undefined);
      }),
      ...tooMany(attachments.length),
    ];
  }
  const raw = record(message.raw);
  if (attachments.length === 0 && !raw?.content) return [];
  const found = photonLeaves(raw?.content);
  const count = Math.max(attachments.length, found.leaves.length);
  const media: Media[] = [];
  for (let index = 0; index < Math.min(count, MAX_INBOUND_MEDIA_ITEMS); index++) {
    const leaf = found.leaves[index];
    const normalized = record(attachments[index]);
    if (!leaf || !normalized || found.malformed) {
      media.push({ ...(normalized ? metadata(normalized) : {}), omission: 'unavailable' });
      continue;
    }
    const { content, path } = leaf;
    const messageId = referenceId(message.id);
    let source: PhotonMediaReference | undefined;
    if (messageId && content.type === 'attachment') {
      const attachmentId = referenceId(content.id);
      if (attachmentId) source = { version: 1, provider: 'photon', messageId, path, contentKind: 'attachment', attachmentId };
    } else if (messageId && content.type === 'voice') {
      source = { version: 1, provider: 'photon', messageId, path, contentKind: 'voice' };
    }
    // The SDK derives this list from the same content tree. A mismatch means that
    // the stored ordinal could no longer identify the attachment unambiguously.
    // Photon normalizes an omitted raw size to zero in buildChatMessage.
    const contentMeta = metadata({ ...content, size: content.size ?? 0 });
    const normalizedMeta = metadata(normalized);
    if (contentMeta.mimeType !== normalizedMeta.mimeType || contentMeta.sizeBytes !== normalizedMeta.sizeBytes) {
      media.push({ ...normalizedMeta, omission: 'unavailable' });
      continue;
    }
    media.push(descriptor(normalized, source));
  }
  if (found.malformed && media.length === 0) media.push({ omission: 'unavailable' });
  return [...media, ...tooMany(count)];
}

/** Follow only a saved content path and recheck the exact terminal kind and GUID. */
export function resolvePhotonMediaContent(rawContent: unknown, reference: PhotonMediaReference): PhotonMediaContent | null {
  if (reference.version !== 1 || reference.provider !== 'photon' ||
      !Array.isArray(reference.path) || reference.path.length > MAX_CONTENT_DEPTH ||
      !referenceId(reference.messageId) ||
      (reference.contentKind !== 'attachment' && reference.contentKind !== 'voice')) return null;
  let current: unknown = rawContent;
  const seen = new WeakSet<object>();
  let visited = 0;
  for (const step of [...reference.path, null]) {
    const node = record(current);
    if (!node || ++visited > MAX_CONTENT_NODES || seen.has(node)) return null;
    seen.add(node);
    if (step === null) {
      if (node.type !== reference.contentKind) return null;
      if (reference.contentKind === 'attachment' &&
          (!referenceId(reference.attachmentId) || node.id !== reference.attachmentId)) return null;
      return node as unknown as PhotonMediaContent;
    }
    if (step === 'reply') {
      if (node.type !== 'reply') return null;
      current = node.content;
    } else {
      if (!Number.isSafeInteger(step) || step < 0 || step >= MAX_CONTENT_NODES || node.type !== 'group' ||
          !Array.isArray(node.items) || node.items.length > MAX_CONTENT_NODES || step >= node.items.length) return null;
      current = record(node.items[step])?.content;
    }
  }
  return null;
}
