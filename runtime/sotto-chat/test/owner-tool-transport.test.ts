import assert from 'node:assert/strict';
import test from 'node:test';
import {createOwnerLoopClient} from '../src/tools/owner-loop-client.js';
import {createOwnerQuietClient} from '../src/tools/owner-quiet-client.js';
import {createOwnerReminderClient} from '../src/tools/owner-reminder-client.js';
import {createOwnerReminderToolClient} from '../src/tools/owner-reminder-tool-client.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';
import {ownerReminderToolKind} from '../src/tools/owner-reminder-tool-contract.js';

const identity = {tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',
  line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:'b'.repeat(64)};
const options = {bridgeUrl:'http://127.0.0.1:1234/mcp',bearer:'a'.repeat(64),identity};
const request = {actionId:'c'.repeat(64),eventId:'d'.repeat(64),payloadHash:'e'.repeat(64),
  text:'fixture owner request',admittedAtMs:1,providerThreadId:'thread'};
const clients = [
  {name:'loop command',limit:2048,reply:{version:1,status:'prepared',zone:'UTC'},
    call:(fetch:typeof globalThis.fetch,signal?:AbortSignal) => createOwnerLoopClient({...options,fetch}).call('prepare',{...request,zone:null},signal)},
  {name:'quiet command',limit:2048,reply:{version:1,status:'prepared',zone:'UTC'},
    call:(fetch:typeof globalThis.fetch,signal?:AbortSignal) => createOwnerQuietClient({...options,fetch}).call('prepare',{...request,zone:null},signal)},
  {name:'reminder command',limit:524288,reply:{version:1,status:'listed',items:[]},
    call:(fetch:typeof globalThis.fetch,signal?:AbortSignal) => createOwnerReminderClient({...options,fetch}).call('list',request,signal)},
  {name:'reminder tool',limit:8192,reply:{version:1,status:'context',event_id:request.eventId,payload_hash:request.payloadHash,zone:'UTC',now:'2026-11-05T12:00:00Z'},
    call:(fetch:typeof globalThis.fetch,signal?:AbortSignal) => createOwnerReminderToolClient({...options,fetch}).call('context',
      {...request,actionId:stableActionIdentity(request.eventId,ownerReminderToolKind,null).actionId,args:null},signal)},
];

for (const client of clients) {
  test(`${client.name} rejects HTTP503 without reading its body`,async () => {
    let bodyAccesses=0;
    const unavailable = {status:503,get body(){bodyAccesses++;throw new Error('PRIVATE ERROR BODY');}} as unknown as Response;
    await assert.rejects(client.call(async () => unavailable),/unavailable/);
    assert.equal(bodyAccesses,0);
  });
  test(`${client.name} preserves its exact streamed response capacity`,async () => {
    const json=JSON.stringify(client.reply);
    let bytes=client.limit;
    const fetch:typeof globalThis.fetch=async () => new Response(json+' '.repeat(bytes-Buffer.byteLength(json)));
    assert.deepEqual(await client.call(fetch),client.reply);
    bytes++;
    await assert.rejects(client.call(fetch),/unavailable/);
  });
  test(`${client.name} makes no contact after caller cancellation`,async () => {
    let calls=0;const controller=new AbortController();controller.abort();
    await assert.rejects(client.call(async () => {calls++;return Response.json(client.reply);},controller.signal),/unavailable/);
    assert.equal(calls,0);
  });
}
