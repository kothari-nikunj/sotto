import test from 'node:test';
import assert from 'node:assert/strict';
import {createGoogleReadTools,readOwnerGoogle,GoogleReadUnavailable} from '../src/agent/google-read.js';
const revision='a'.repeat(64);
const authority={version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['gmail'] as ['gmail']};
const message=(id:string,time:string,body:string)=>({id,threadId:'thread1',from:'owner@example.test',to:'counsel@example.test',cc:'',subject:'Cedar grant',date:'2026-10-23T12:00:00Z',snippet:body,body,labels:['SENT'],internal_date:time,body_truncated:false});
const result=()=>({version:1,source:'gmail',operation:'gmail_thread_get',thread_id:'thread1',items:[message('request','1000','Please approve.'),message('reply','2000','Approved, please proceed.')],omitted_messages:0,observation:{status:'ok',complete:true,observed_at:'2026-10-23T12:00:00Z',coverage:{since:null,until:null}}});
const options=(raw:unknown,read=async()=>authority)=>({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'synthetic',sourceAuthority:{read},fetch:async()=>new Response(JSON.stringify(raw))});
test('thread read preserves message identities/wording and excludes unattended factory',async()=>{
 const raw=result();const value=await readOwnerGoogle(options(raw),'gmail_thread_get',{thread_id:'thread1'},revision);
 assert.deepEqual(value,raw);assert.ok(createGoogleReadTools(options(raw)).gmail_thread_get);
 assert.equal(createGoogleReadTools(options(raw),'unattended').gmail_thread_get,undefined);
 const partial=result();partial.omitted_messages=2;partial.observation={...partial.observation,status:'partial',complete:false};
 await readOwnerGoogle(options(partial),'gmail_thread_get',{thread_id:'thread1'},revision);
 partial.omitted_messages=0;partial.items[0]!.body_truncated=true;
 await readOwnerGoogle(options(partial),'gmail_thread_get',{thread_id:'thread1'},revision);
});
test('thread read fails closed for mixed identities, duplicates, dishonest coverage and grant changes',async()=>{
 const warn=console.warn;console.warn=()=>{};
 try{
  for(const mutate of [
   (x:ReturnType<typeof result>)=>{x.thread_id='other';},
   (x:ReturnType<typeof result>)=>{x.items[1]!.threadId='other';},
   (x:ReturnType<typeof result>)=>{x.items[1]!.id=x.items[0]!.id;},
   (x:ReturnType<typeof result>)=>{x.items[1]!.id='';},
   (x:ReturnType<typeof result>)=>{x.items.reverse();},
   (x:ReturnType<typeof result>)=>{x.items[0]!.internal_date='invalid';},
   (x:ReturnType<typeof result>)=>{x.items[0]!.body_truncated=true;},
   (x:ReturnType<typeof result>)=>{x.omitted_messages=1;},
   (x:ReturnType<typeof result>)=>{x.items=[];},
   (x:ReturnType<typeof result>)=>{x.items[1]!.labels=['DRAFT'];},
   (x:ReturnType<typeof result>)=>{x.items[0]!.body='x'.repeat(16*1024+1);},
   (x:ReturnType<typeof result>)=>{x.items=Array(21).fill(x.items[0]);},
   (x:ReturnType<typeof result>)=>{Object.assign(x,{extra:'untrusted'});},
  ]){const raw=result();mutate(raw);await assert.rejects(readOwnerGoogle(options(raw),'gmail_thread_get',{thread_id:'thread1'},revision),GoogleReadUnavailable);}
  let reads=0;await assert.rejects(readOwnerGoogle(options(result(),async()=>({...authority,revision:++reads===1?revision:'b'.repeat(64)})),'gmail_thread_get',{thread_id:'thread1'},revision),GoogleReadUnavailable);
  let fetched=false;await assert.rejects(readOwnerGoogle({...options(result()),fetch:async()=>{fetched=true;return new Response('{}');}},'gmail_thread_get',{thread_id:'../bad'},revision),GoogleReadUnavailable);assert.equal(fetched,false);
 }finally{console.warn=warn;}
});
