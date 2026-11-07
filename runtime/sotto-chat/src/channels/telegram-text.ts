import { convertEmojiPlaceholders } from 'chat';

// The pinned adapter truncates plain text at these UTF-16 limits before posting.
// Keep the existing eight-message receipt budget and HTTP text-size bound.
const MESSAGE_LIMIT = 4096;
const CAPTION_LIMIT = 1024;
const MAX_TEXT_LENGTH = 32000;
const MAX_CHUNKS = 8;
const graphemes = new Intl.Segmenter(undefined, { granularity: 'grapheme' });

/** Preflight the complete send before contacting Telegram; raw posts have no parse mode. */
export function telegramTextParts(body: string, hasMedia: boolean): string[] | undefined {
  if (body.length > MAX_TEXT_LENGTH) return undefined;
  // Match the pinned SDK's existing display conversion before measuring/splitting,
  // so a placeholder spanning a chunk boundary cannot become literal output.
  const rendered = convertEmojiPlaceholders(body, 'gchat');
  if (hasMedia) return rendered.length <= CAPTION_LIMIT ? [rendered] : undefined;
  const parts: string[] = [];
  let part = '';
  for (const { segment } of graphemes.segment(rendered)) {
    if (segment.length > MESSAGE_LIMIT) return undefined;
    if (part.length + segment.length > MESSAGE_LIMIT) {
      if (!part.trim()) return undefined;
      parts.push(part);
      if (parts.length >= MAX_CHUNKS) return undefined;
      part = '';
    }
    part += segment;
  }
  if (!part.trim()) return undefined;
  parts.push(part);
  return parts;
}
