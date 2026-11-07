import test from 'node:test';
import assert from 'node:assert/strict';
import {RequestContext} from '@mastra/core/request-context';
import {readOwnerGoogle,GoogleReadUnavailable,createGoogleReadTools} from '../src/agent/google-read.js';
import {readOwnerContinuityJson} from '../src/agent/owner-continuity-client.js';
const revision='a'.repeat(64),secret='PRIVATE-QUERY-ID-TOKEN';
const good={error:'Google read unavailable',read_stage:'provider_forbidden'};
const authority={read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:['gmail'] as ['gmail']})};
test('only exact bounded authenticated Google503 metadata becomes a closed diagnostic',async()=>{
 const warn=console.warn;console.warn=()=>{};
 try{
  for(const [mode,body,expected] of [
   ['valid',JSON.stringify(good),'provider_forbidden'],
   ['unknown',JSON.stringify({...good,read_stage:secret}),'unknown'],
   ['extra',JSON.stringify({...good,private:secret}),'unknown'],
   ['wrong_error',JSON.stringify({...good,error:secret}),'unknown'],
   ['oversize',JSON.stringify({...good,private:secret.repeat(100)}),'unknown'],
   ['declared_oversize',JSON.stringify(good),'unknown'],
   ['bad_json','{'+secret,'unknown'],
   ['array',JSON.stringify([good]),'unknown'],
   ['missing_body',null,'unknown'],
  ] as const){
   let calls=0;
   await assert.rejects(readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:authority,fetch:async()=>{calls++;return new Response(body,{status:503,...(mode==='declared_oversize'?{headers:{'content-length':'2048'}}:{})});}},'gmail_search',{query:secret,max:1},revision),error=>{
    assert.ok(error instanceof GoogleReadUnavailable,mode);assert.equal(error.stage,expected,mode);
    assert.equal(error.message.includes(secret),false,mode);return true;
   });assert.equal(calls,1);
  }
  // Authentication/redirect/other-route bodies are never interpreted as metadata.
  for(const status of [401,302])await assert.rejects(readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:authority,fetch:async()=>new Response(JSON.stringify(good),{status})},'gmail_search',{query:secret,max:1},revision),error=>error instanceof GoogleReadUnavailable&&error.stage==='receiver_transport');
  await assert.rejects(readOwnerContinuityJson({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,fetch:async()=>new Response(JSON.stringify(good),{status:503})},'owner-loops',{include_parked:false}),error=>!('googleReadStage' in (error as object)));
  assert.equal(new GoogleReadUnavailable(secret as never).message.includes(secret),false);
 }finally{console.warn=warn;}
});

test('Google503 diagnostic parsing obeys streaming size and caller cancellation',async()=>{
 const warn=console.warn;console.warn=()=>{};
 try{
  let cancelled=false;
  const stream=new ReadableStream<Uint8Array>({start(controller){controller.enqueue(new TextEncoder().encode('X'.repeat(1025)));},cancel(){cancelled=true;}});
  await assert.rejects(readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:authority,fetch:async()=>new Response(stream,{status:503})},'gmail_search',{query:secret,max:1},revision),error=>error instanceof GoogleReadUnavailable&&error.stage==='unknown');
  assert.equal(cancelled,true);
  const controller=new AbortController();let reads=0;
  await assert.rejects(readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:authority,fetch:async()=>new Response(new ReadableStream<Uint8Array>({pull(){reads++;controller.abort();}}),{status:503})},'gmail_search',{query:secret,max:1},revision,controller.signal),error=>error instanceof GoogleReadUnavailable&&error.stage==='receiver_transport');
  assert.ok(reads<=1);
 }finally{console.warn=warn;}
});

test('Google metadata timeout cancels its bounded reader and preserves Google deadline',async()=>{
 const original=AbortSignal.timeout,warn=console.warn;let cancelled=false;const deadlines:number[]=[];
 AbortSignal.timeout=(ms:number)=>{deadlines.push(ms);const controller=new AbortController();setTimeout(()=>controller.abort(),5);return controller.signal;};console.warn=()=>{};
 try{
  await assert.rejects(readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:authority,fetch:async()=>new Response(new ReadableStream<Uint8Array>({cancel(){cancelled=true;}}),{status:503})},'gmail_search',{query:secret,max:1},revision),error=>error instanceof GoogleReadUnavailable&&error.stage==='receiver_transport');
  assert.deepEqual(deadlines,[30000]);assert.equal(cancelled,true);
 }finally{AbortSignal.timeout=original;console.warn=warn;}
});


test('pre-reader execution-context refusal retains a closed stage and dispatches nothing',async()=>{
 let reads=0;const tools=createGoogleReadTools({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:authority,fetch:async()=>{reads++;throw new Error(secret);}});
 await assert.rejects(tools.gmail_search!.execute!({query:secret,max:1},{requestContext:new RequestContext()} as never),error=>error instanceof GoogleReadUnavailable&&error.stage==='execution_context');
 assert.equal(reads,0);
});


test('cancellation after Google metadata decode takes precedence over its stage',async()=>{
 const original=JSON.parse,warn=console.warn,controller=new AbortController();
 const encoded=JSON.stringify(good);console.warn=()=>{};
 JSON.parse=(text,reviver)=>{const value=original(text,reviver);if(text===encoded)controller.abort();return value;};
 try{
  await assert.rejects(readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:authority,fetch:async()=>new Response(encoded,{status:503})},'gmail_search',{query:secret,max:1},revision,controller.signal),error=>error instanceof GoogleReadUnavailable&&error.stage==='receiver_transport');
  assert.equal(controller.signal.aborted,true);
 }finally{JSON.parse=original;console.warn=warn;}
});


test('Google HTTP status and phase decode only from exact bounded error metadata',async()=>{
 const warn=console.warn;console.warn=()=>{};
 try{
  for(const [extra,expected] of [
   [{read_http_status:404,read_phase:'search_metadata'},true],
   [{read_http_status:100,read_phase:'search_list'},true],
   [{read_http_status:599},true],
   [{read_http_status:99},false],[{read_http_status:600},false],
   [{read_http_status:'404'},false],[{read_http_status:true},false],
   [{read_http_status:404.5},false],[{read_phase:'message_get'},false],
   [{read_phase:secret},false],[{read_http_status:404,private:secret},false],
  ] as const){
   await assert.rejects(readOwnerGoogle({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:authority,fetch:async()=>new Response(JSON.stringify({...good,...extra}),{status:503})},'gmail_search',{query:secret,max:1},revision),error=>{
    assert.ok(error instanceof GoogleReadUnavailable);assert.equal(error.stage,expected?'provider_forbidden':'unknown');
    assert.equal(error.message.includes(secret),false);
    if(expected&&'read_http_status'in extra)assert.ok(error.message.includes(`;http_status=${extra.read_http_status}`));
    if(!expected)assert.equal(error.message.includes(';http_status='),false);
    return true;
   });
  }
  const {googleReadDiagnosticFromErrorText,googleReadFailureMessage}=await import('../src/agent/google-read-diagnostic.js');
  const marker=googleReadFailureMessage('provider_http_other',{httpStatus:404,phase:'search_metadata'});
  assert.deepEqual(googleReadDiagnosticFromErrorText(marker),{stage:'provider_http_other',httpStatus:404,phase:'search_metadata'});
  for(const value of [marker+' PRIVATE','PRIVATE '+marker,marker.replace('404','099'),marker.replace('search_metadata','PRIVATE'),marker.replace(';http_status=404',';http_status=404;http_status=404')])assert.equal(googleReadDiagnosticFromErrorText(value),undefined);
 }finally{console.warn=warn;}
});

test('concurrent Google errors keep HTTP status and phase request-local',async()=>{
 const warn=console.warn;console.warn=()=>{};
 try{
  const options={bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:secret,sourceAuthority:authority,fetch:async(_url:unknown,init?:RequestInit)=>{
   const query=JSON.parse(String(init?.body)).arguments.query;
   await new Promise(resolve=>setTimeout(resolve,query==='first'?5:0));
   return new Response(JSON.stringify({error:'Google read unavailable',read_stage:'provider_http_other',read_http_status:query==='first'?404:410,read_phase:query==='first'?'search_list':'search_metadata'}),{status:503});
  }};
  const results=await Promise.allSettled(['first','second'].map(query=>readOwnerGoogle(options as never,'gmail_search',{query,max:1},revision)));
  const {googleReadDiagnosticFromErrorText}=await import('../src/agent/google-read-diagnostic.js');
  assert.deepEqual(results.map(result=>result.status==='rejected'?googleReadDiagnosticFromErrorText(result.reason.message):null),[
   {stage:'provider_http_other',httpStatus:404,phase:'search_list'},
   {stage:'provider_http_other',httpStatus:410,phase:'search_metadata'},
  ]);
 }finally{console.warn=warn;}
});
