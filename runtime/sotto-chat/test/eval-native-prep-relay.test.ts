import assert from 'node:assert/strict';
import test from 'node:test';
import {startNativePrepRelay} from '../evals/native-prep-relay.js';

test('native relay refuses unbounded chronological call limits before opening a socket',async()=>{
  for(const cap of [0,25,1.5,Infinity])await assert.rejects(startNativePrepRelay({
    origin:'https://approved-model.invalid',bearer:'synthetic-secret',nativeMediaModelId:'gemini-3.8-flash'},fetch,false,cap),
    /Invalid native eval call limit/);
});

test('native prep relay preserves approved protocol, counts attempts and refuses tools/third attempt',async()=>{
  const seen:Array<{url:string;body:string;authorization:string|null}>=[];
  const relay=await startNativePrepRelay({origin:'https://approved-model.invalid',bearer:'synthetic-secret',nativeMediaModelId:'gemini-3.8-flash'},
    (async(input,init)=>{seen.push({url:String(input),body:String(init?.body),authorization:new Headers(init?.headers).get('authorization')});
      return new Response('{"transport_only":true}',{status:200});}) as typeof fetch);
  const body={contents:[{parts:[{text:'Synthetic native protocol check'}]}],generationConfig:{response_mime_type:'application/json'}};
  const send=(value:unknown)=>fetch(relay.url,{method:'POST',headers:{authorization:`Bearer ${relay.bearer}`,'content-type':'application/json'},body:JSON.stringify(value)});
  try {
    assert.equal((await send({...body,tools:[{google_search:{}}]})).status,502);
    assert.equal(seen.length,0);
    for(let i=0;i<2;i++){const response=await send(body);assert.equal(await response.text(),'{"transport_only":true}');}
    assert.equal((await send(body)).status,502);
    assert.equal(seen.length,2);assert.equal(relay.calls.length,2);
    assert.ok(seen.every(row=>row.url==='https://approved-model.invalid/native/v1beta/models/gemini-3.8-flash:generateContent'&&row.body===JSON.stringify(body)&&row.authorization==='Bearer synthetic-secret'));
    assert.ok(relay.calls.every(call=>call.status===200&&call.responseBytes>0));
  } finally {await relay.close();}
});
