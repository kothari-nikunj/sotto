import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ALLOWED_INBOUND_MEDIA_TYPES,
  MAX_INBOUND_MEDIA_BYTES,
  MAX_INBOUND_MEDIA_ITEMS,
  MAX_INBOUND_TURN_MEDIA_BYTES,
  mediaSignatureMatches,
  normalizeMediaMime,
  type MediaOmission,
} from '../src/channels/media-policy.js';

const ascii = (value: string) => Uint8Array.from([...value].map(character => character.charCodeAt(0)));
const withAt = (prefix: Uint8Array, offset: number, value: string) => {
  const bytes = new Uint8Array(offset + value.length);
  bytes.set(prefix);
  bytes.set(ascii(value), offset);
  return bytes;
};

const fixtures: Array<[string, Uint8Array]> = [
  ['image/png', new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])],
  ['image/jpeg', new Uint8Array([0xff, 0xd8, 0xff])],
  ['image/gif', ascii('GIF89a')],
  ['image/webp', withAt(ascii('RIFF'), 8, 'WEBP')],
  ['audio/wav', withAt(ascii('RIFF'), 8, 'WAVE')],
  ['audio/mpeg', ascii('ID3')],
  ['audio/ogg', ascii('OggS')],
  ['audio/webm', new Uint8Array([0x1a, 0x45, 0xdf, 0xa3])],
];

test('shared media bounds, allowlist, and omission vocabulary are closed and stable', () => {
  assert.equal(MAX_INBOUND_MEDIA_ITEMS, 4);
  assert.equal(MAX_INBOUND_MEDIA_BYTES, 8 * 1024 * 1024);
  assert.equal(MAX_INBOUND_TURN_MEDIA_BYTES, 16 * 1024 * 1024);
  assert.deepEqual([...ALLOWED_INBOUND_MEDIA_TYPES].sort(), fixtures.map(([mime]) => mime).sort());
  const omissions: MediaOmission[] = [
    'too_many', 'too_large', 'unsupported_type', 'unavailable', 'invalid_type', 'empty', 'timed_out',
  ];
  assert.equal(omissions.length, 7);
});

test('MIME normalization accepts only allowlisted essences and ignores parameters/case', () => {
  assert.equal(normalizeMediaMime(' IMAGE/PNG ; charset=binary '), 'image/png');
  assert.equal(normalizeMediaMime('audio/mpeg'), 'audio/mpeg');
  assert.equal(normalizeMediaMime('application/pdf'), undefined);
  assert.equal(normalizeMediaMime('image /png'), undefined);
  assert.equal(normalizeMediaMime(undefined), undefined);
  assert.equal(normalizeMediaMime({ type: 'image/png' }), undefined);
});

test('supported byte signatures match their declared MIME essences', () => {
  for (const [mime, bytes] of fixtures) {
    assert.equal(mediaSignatureMatches(bytes, mime), true, `${mime} fixture should match`);
    assert.equal(mediaSignatureMatches(bytes, ` ${mime.toUpperCase()}; fixture=1 `), true);
  }
});

test('recognized MIME mismatches, truncated bytes, and unknown signatures fail closed', () => {
  const png = fixtures.find(([mime]) => mime === 'image/png')![1];
  const webp = fixtures.find(([mime]) => mime === 'image/webp')![1];
  assert.equal(mediaSignatureMatches(png, 'image/jpeg'), false);
  assert.equal(mediaSignatureMatches(webp, 'audio/wav'), false);
  assert.equal(mediaSignatureMatches(png.subarray(0, 7), 'image/png'), false);
  assert.equal(mediaSignatureMatches(new Uint8Array([1, 2, 3, 4]), 'image/png'), false);
  assert.equal(mediaSignatureMatches(ascii('not media'), 'application/pdf'), false);
  assert.equal(mediaSignatureMatches(new Uint8Array(), 'audio/ogg'), false);
});
