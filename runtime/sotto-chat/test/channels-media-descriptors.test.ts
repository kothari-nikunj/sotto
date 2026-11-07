import test from 'node:test';
import assert from 'node:assert/strict';
import type { Message } from 'chat';
import { collectMediaDescriptors, resolvePhotonMediaContent } from '../src/channels/media-descriptors.js';
import type { PhotonMediaReference } from '../src/channels/types.js';

function input(id: string, raw: unknown, attachments: unknown[]): Pick<Message, 'id' | 'raw' | 'attachments'> {
  return { id, raw, attachments } as Pick<Message, 'id' | 'raw' | 'attachments'>;
}

test('Telegram album descriptors use each normalized member file ID and never store download material', () => {
  const attachments = [
    { mimeType: 'image/jpeg', name: 'one.jpg', size: 12, fetchMetadata: { fileId: 'member-1' }, url: 'https://bad.example/one', fetchData: () => { throw Error('downloaded'); } },
    { mimeType: 'audio/ogg', name: 'two.ogg', size: 13, fetchMetadata: { fileId: 'member-2' } },
  ];
  const media = collectMediaDescriptors('telegram', input('album-latest', { photo: [{ file_id: 'member-2' }] }, attachments));
  assert.deepEqual(media.map((item) => item.source), [
    { version: 1, provider: 'telegram', fileId: 'member-1' },
    { version: 1, provider: 'telegram', fileId: 'member-2' },
  ]);
  assert.equal(JSON.stringify(media).includes('bad.example'), false);
  assert.equal(JSON.stringify(media).includes('fetchData'), false);
});

test('Photon nested group/reply preserves exact content paths and GUIDs, including voice', () => {
  const content = { type: 'group', items: [
    { content: { type: 'attachment', id: 'guid-a', mimeType: 'image/png', name: 'a.png', size: 3, stream: async () => undefined } },
    { content: { type: 'reply', content: { type: 'group', items: [
      { content: { type: 'voice', mimeType: 'audio/ogg', name: 'voice', size: 4, stream: async () => undefined } },
    ] } } },
  ] };
  const media = collectMediaDescriptors('photon', input('msg-1', { content }, [
    { mimeType: 'image/png', name: 'a.png', size: 3 },
    { mimeType: 'audio/ogg', name: 'voice', size: 4 },
  ]));
  assert.deepEqual(media.map((item) => item.source), [
    { version: 1, provider: 'photon', messageId: 'msg-1', path: [0], contentKind: 'attachment', attachmentId: 'guid-a' },
    { version: 1, provider: 'photon', messageId: 'msg-1', path: [1, 'reply', 0], contentKind: 'voice' },
  ]);
  assert.equal(JSON.stringify(media).includes('stream'), false);
  const attachment = media[0]!.source as PhotonMediaReference;
  assert.equal(resolvePhotonMediaContent(content, attachment)?.id, 'guid-a');
  assert.equal(resolvePhotonMediaContent(content, { ...attachment, attachmentId: 'other' } as PhotonMediaReference), null);
  assert.equal(resolvePhotonMediaContent(content, { ...attachment, path: [1] } as PhotonMediaReference), null);
  assert.equal(resolvePhotonMediaContent(content, { ...attachment, contentKind: 'group' } as unknown as PhotonMediaReference), null);
  assert.equal(resolvePhotonMediaContent({ type: 'group', items: Array(129).fill({ content: content.items[0]!.content }) }, attachment), null);
  assert.equal(resolvePhotonMediaContent(content, media[1]!.source as PhotonMediaReference)?.type, 'voice');
});

test('untrusted IDs, malformed Photon trees, and unsupported MIME fail to omission markers', () => {
  assert.deepEqual(collectMediaDescriptors('telegram', input('m', {}, [
    { mimeType: 'image/png', fetchMetadata: { fileId: 'bad\nkey' } },
  ]))[0]?.omission, 'unavailable');
  assert.deepEqual(collectMediaDescriptors('telegram', input('m', {}, [
    { mimeType: 'application/x-executable', fetchMetadata: { fileId: 'safe' } },
  ]))[0]?.omission, 'unsupported_type');
  const cycle: { type: string; items: unknown[] } = { type: 'group', items: [] };
  cycle.items.push({ content: cycle });
  assert.deepEqual(collectMediaDescriptors('photon', input('m', { content: cycle }, [
    { mimeType: 'image/png', size: 1 },
  ]))[0]?.omission, 'unavailable');
  const nested = Array.from({ length: 10 }).reduce<unknown>((value) => ({ type: 'reply', content: value }),
    { type: 'attachment', id: 'guid', mimeType: 'image/png', size: 1 });
  assert.equal(collectMediaDescriptors('photon', input('m', { content: nested }, [
    { mimeType: 'image/png', size: 1 },
  ]))[0]?.omission, 'unavailable');
});

test('only four attachment positions are eligible and excess count is one bounded marker', () => {
  const attachments = Array.from({ length: 7 }, (_, index) => ({
    mimeType: 'image/jpeg', size: 1, fetchMetadata: { fileId: `file-${index}` },
  }));
  const media = collectMediaDescriptors('telegram', input('album', {}, attachments));
  assert.equal(media.length, 5);
  assert.deepEqual(media[4], { omission: 'too_many', omittedCount: 3 });
});

test('Photon voice with SDK default size and name keeps a resumable path reference', () => {
  const content = { type: 'voice', mimeType: 'audio/ogg', stream: async () => undefined };
  const media = collectMediaDescriptors('photon', input('voice-1', { content }, [
    { mimeType: 'audio/ogg', name: 'voice', size: 0 },
  ]));
  assert.deepEqual(media[0]?.source, {
    version: 1, provider: 'photon', messageId: 'voice-1', path: [], contentKind: 'voice',
  });
});
