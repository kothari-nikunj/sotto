/** Shared bounded policy for inbound media; signatures check MIME consistency only. */
export const MAX_INBOUND_MEDIA_ITEMS = 4;
export const MAX_INBOUND_MEDIA_BYTES = 8 * 1024 * 1024;
export const MAX_INBOUND_TURN_MEDIA_BYTES = 16 * 1024 * 1024;

export const ALLOWED_INBOUND_MEDIA_TYPES: ReadonlySet<string> = new Set([
  'image/png', 'image/jpeg', 'image/webp', 'image/gif',
  'audio/wav', 'audio/mpeg', 'audio/ogg', 'audio/webm',
]);

export type MediaOmission =
  | 'too_many'
  | 'too_large'
  | 'unsupported_type'
  | 'unavailable'
  | 'invalid_type'
  | 'empty'
  | 'timed_out';

/** Return the allowlisted MIME essence, stripping parameters without guessing from filenames. */
export function normalizeMediaMime(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const essence = value.split(';', 1)[0]?.trim().toLowerCase();
  return essence && ALLOWED_INBOUND_MEDIA_TYPES.has(essence) ? essence : undefined;
}

/**
 * A bounded signature/MIME consistency check, not a decoder or proof of valid or safe media.
 * Unknown bytes and recognized mismatches both return false. Container signatures do not
 * establish codecs or playable content.
 */
export function mediaSignatureMatches(bytes: Uint8Array, mime: string): boolean {
  const normalized = normalizeMediaMime(mime);
  if (!normalized || !(bytes instanceof Uint8Array) || bytes.byteLength === 0) return false;

  switch (normalized) {
    case 'image/png':
      return startsWith(bytes, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    case 'image/jpeg':
      return startsWith(bytes, [0xff, 0xd8, 0xff]);
    case 'image/gif':
      return startsWithAscii(bytes, 'GIF87a') || startsWithAscii(bytes, 'GIF89a');
    case 'image/webp':
      return startsWithAscii(bytes, 'RIFF') && matchesAsciiAt(bytes, 8, 'WEBP');
    case 'audio/wav':
      return startsWithAscii(bytes, 'RIFF') && matchesAsciiAt(bytes, 8, 'WAVE');
    case 'audio/ogg':
      return startsWithAscii(bytes, 'OggS');
    case 'audio/webm':
      return startsWith(bytes, [0x1a, 0x45, 0xdf, 0xa3]);
    case 'audio/mpeg':
      return startsWithAscii(bytes, 'ID3') ||
        (bytes.byteLength >= 2 && bytes[0] === 0xff && (bytes[1]! & 0xe0) === 0xe0);
    default:
      return false;
  }
}

function startsWith(bytes: Uint8Array, signature: readonly number[]): boolean {
  return bytes.byteLength >= signature.length && signature.every((byte, index) => bytes[index] === byte);
}

function startsWithAscii(bytes: Uint8Array, signature: string): boolean {
  return matchesAsciiAt(bytes, 0, signature);
}

function matchesAsciiAt(bytes: Uint8Array, offset: number, signature: string): boolean {
  return bytes.byteLength >= offset + signature.length &&
    [...signature].every((character, index) => bytes[offset + index] === character.charCodeAt(0));
}
