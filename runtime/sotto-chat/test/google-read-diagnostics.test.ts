import test from 'node:test';
import assert from 'node:assert/strict';
import {readOwnerGoogle, GoogleReadUnavailable, type GoogleReadOperation} from '../src/agent/google-read.js';
const revision='a'.repeat(64);
const secret='PRIVATE-QUERY-MESSAGE-ID-TOKEN';
const authority={version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['gmail'] as ['gmail']};
const valid={version:1,source:'gmail',operation:'gmail_search',items:[],observation:{status:'ok',complete:true,observed_at:'2026-10-23T00:00:00Z',coverage:{since:null,until:null}}};
test('Google diagnostics distinguish failure stages without content, retry or changed error behavior',async()=>{
 const warn=console.warn;const logs:string[]=[];
 console.warn=(...args)=>{logs.push(args.join(' '));};
 try{
  for(const [mode,stage] of [['transport','receiver_transport'],['schema','result_schema'],['contract','result_contract'],['authority','authority_after'],['input','input_validation'],['unknown_operation','input_validation']] as const){
   logs.length=0;let reads=0,fetches=0;
   const options={bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:{read:async()=>{reads++;return {...authority,revision:mode==='authority'&&reads===2?'b'.repeat(64):revision};}},fetch:async()=>{
    fetches++;
    if(mode==='transport')throw new Error(secret);
    return new Response(JSON.stringify(mode==='schema'?{...valid,items:[{id:secret}]}:mode==='contract'?{...valid,private:secret}:valid));
   }};
   await assert.rejects(readOwnerGoogle(options,mode==='unknown_operation'?secret as GoogleReadOperation:'gmail_search',{query:secret,max:mode==='input'?0:1},revision),GoogleReadUnavailable);
   assert.equal(logs.length,1);assert.equal(logs[0]!.includes(secret),false);
   assert.deepEqual(JSON.parse(logs[0]!.slice('[sotto-google-read] '.length)),{operation:mode==='unknown_operation'?'unknown':'gmail_search',stage,outcome:'unavailable'});
   assert.equal(fetches,mode==='input'||mode==='unknown_operation'?0:1);
  }
  logs.length=0;
  await readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:{read:async()=>authority},fetch:async()=>new Response(JSON.stringify(valid))},'gmail_search',{query:secret,max:1},revision);
  assert.deepEqual(logs,[]);
  console.warn=()=>{throw new Error(secret);};
  await assert.rejects(readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:{read:async()=>authority},fetch:async()=>new Response('',{status:503})},'gmail_search',{query:secret,max:1},revision),GoogleReadUnavailable);
 }finally{console.warn=warn;}
});
