import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {createPhotonTextFormatter} from '../src/channels/photon-text-formatter.js';
import {createPhotonTransport, type PhotonAdapterLike} from '../src/channels/transport.js';

const sharedLibrary = path.join(process.env.SOTTO_TEST_SKILL_PACK ?? path.resolve('../../sotto-chief-of-staff'), '_shared/lib');
const format = createPhotonTextFormatter(sharedLibrary);

test('actual shared formatter handles markup and preserves supported Unicode bound', async () => {
  assert.equal(await format('**Heading**\n\n*Useful detail*'), 'Heading\n\nUseful detail');
  assert.equal(await format('界'.repeat(32000)), '界'.repeat(32000));
  assert.equal(await format('Plain message'), 'Plain message');
});

test('formatting an outbound Photon text never blocks the event loop', async () => {
  let ticks = 0;
  const timer = setInterval(() => ticks++, 1);
  try {
    await new Promise(resolve => setTimeout(resolve, 20));
    const before = ticks;
    for (let index = 0; index < 5; index++) assert.match(await format('**Heading** ' + 'x'.repeat(100_000)), /^Heading x/);
    assert.ok(ticks - before > 0, 'timers ran while the shared formatter child was working');
  } finally { clearInterval(timer); }
});

test('formatter bounds and failures reject without contacting the provider', async () => {
  await assert.rejects(format('x'.repeat(128 * 1024 + 1)), /exceeds bound/);
  await assert.rejects(createPhotonTextFormatter('/missing/shared/lib')('Hello'), /Command failed:/);
  assert.throws(() => createPhotonTextFormatter('relative/lib'), /path is invalid/);
});

test('Photon text and actual grouped caption use shared formatter; review stays verbatim', async () => {
  const posted: unknown[] = [];
  const adapter = {
    postMessage: async (_thread: string, text: unknown) => {posted.push(text); return {id:'text-id'};},
    decodeThreadId: () => ({chatGuid:'chat',phone:'+15557654321'}),
    encodeThreadId: () => 'thread',
    app: {send: async (_space: unknown, content: {build():Promise<unknown>}) => {posted.push(await content.build());return {id:'gallery-id'};}},
  } as unknown as PhotonAdapterLike;
  const transport = createPhotonTransport(adapter, {formatText:format, gallerySpace:async()=>({send:async()=>({})})});
  assert.equal((await transport.send({threadId:'thread',text:'**Heading**'})).acceptance,'accepted');
  assert.equal(posted[0],'Heading');
  await transport.send({threadId:'thread',text:'**Caption**',media:[{data:Buffer.from([1,2,3]),mimeType:'image/png',filename:'card.png'}]});
  assert.match(JSON.stringify(posted[1]), /Caption/);
  assert.doesNotMatch(JSON.stringify(posted[1]), /\*\*/);
  await transport.send({threadId:'thread',text:'**Exact review** — owner text',verbatim:true});
  assert.equal(posted[2],'**Exact review** — owner text');
});

test('formatter unavailable or empty refuses before provider contact', async () => {
  let contacts=0;
  const adapter={postMessage:async()=>{contacts++;return {id:'unexpected'};}} as unknown as PhotonAdapterLike;
  for(const formatText of [createPhotonTextFormatter('/missing/shared/lib'),()=> '']) {
    const receipt=await createPhotonTransport(adapter,{formatText}).send({threadId:'thread',text:'Hello'});
    assert.equal(receipt.acceptance,'not_attempted');
  }
  assert.equal(contacts,0);
});


test('Photon stop during formatting prevents first provider contact', async () => {
  const controller = new AbortController();
  let contacts = 0;
  let release!: (text: string) => void;
  const adapter = {postMessage: async () => {contacts++;return {id:'unexpected'};}} as unknown as PhotonAdapterLike;
  const transport = createPhotonTransport(adapter, {formatText: () => new Promise<string>(resolve => {release=resolve;})});
  const pending = transport.send({threadId:'thread',text:'hello',signal:controller.signal});
  controller.abort();
  release('hello');
  assert.equal((await pending).acceptance,'not_attempted');
  assert.equal(contacts,0);
});

test('Photon stop during gallery resolution prevents message submission', async () => {
  const controller = new AbortController();
  let contacts = 0;
  const adapter = {decodeThreadId:()=>({chatGuid:'chat'}),encodeThreadId:()=> 'thread',
    app:{send:async()=>{contacts++;return {id:'unexpected'};}}} as unknown as PhotonAdapterLike;
  const transport = createPhotonTransport(adapter,{gallerySpace:async()=>{controller.abort();return {} as never;}});
  const receipt = await transport.send({threadId:'thread',media:[{data:Buffer.from([1]),mimeType:'image/png'}],signal:controller.signal});
  assert.equal(receipt.acceptance,'not_attempted');
  assert.equal(contacts,0);
});
