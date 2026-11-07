import assert from 'node:assert/strict';
import test from 'node:test';
import {createOwnerTaskToolClient} from '../src/tools/owner-task-tool-client.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';
import {cleanupListHash,ownerTaskToolKind,type CleanupList,type OwnerTaskToolArgs} from '../src/tools/owner-task-tool-contract.js';
import {CleanupDiagnosticError} from '../src/tools/cleanup-diagnostic.js';
const hex='a'.repeat(64);
const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:hex};
function cleanup(count:number):OwnerTaskToolArgs{
 const list:CleanupList={operation:'cleanup_list',scope:'open',source_revision:hex,total:count,remaining:0,review_hash:'',rows:Array.from({length:count},(_,i)=>({index:i+1,anchor_key:'task-'+i,name:'Task',what:'Task '+i,direction:'you_owe',age_days:1,times_surfaced:0,surface_count_provenance:'accepted_delivery',overdue:false,deadline:null,chased_count:0,chased_out:false,suggestion:'do it or dismiss',loop_version:hex,loop_identity:hex}))};list.review_hash=cleanupListHash(list);
 return {operation:'cleanup',list,proposal:{event_id:'b'.repeat(64),action_id:hex,args_hash:hex,accepted_dispatch_id:'accepted-list',source_revision:hex},items:list.rows.map(row=>({index:row.index,action:'resolve',correction:String(row.index)}))};
}
const request=(args:OwnerTaskToolArgs)=>({...stableActionIdentity(hex,ownerTaskToolKind,args),eventId:hex,text:'Owner task request',payloadHash:hex,admittedAtMs:1791216000000,providerThreadId:'thread',args});

test('only multi-item batch applies receive a measured bounded deadline',async()=>{
 const deadlines:number[]=[];const original=AbortSignal.timeout;let calls=0;
 AbortSignal.timeout=milliseconds=>{deadlines.push(milliseconds);return original(milliseconds);};
 const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:hex,fetch:async()=>{calls++;return new Response(JSON.stringify({version:1,status:'unknown'}));}});
 const unavailable=(error:unknown)=>{assert.ok(error instanceof CleanupDiagnosticError);assert.deepEqual(error.diagnostic,{cleanup_stage:'response',cleanup_category:'unavailable',cleanup_http_status:200});return true;};
 try{
  await assert.rejects(()=>client.call('apply',request(cleanup(12))),unavailable);assert.equal(deadlines.at(-1),29_000);
  await assert.rejects(()=>client.call('apply',request(cleanup(50))),unavailable);assert.equal(deadlines.at(-1),60_000);
  await assert.rejects(()=>client.call('apply',request(cleanup(1))),unavailable);assert.equal(deadlines.at(-1),5_000);
  await client.call('inspect',request(cleanup(12)));assert.equal(deadlines.at(-1),5_000);
  await client.call('preview',request({operation:'propose_cleanup',scope:'open',source_revision:hex}));assert.equal(deadlines.at(-1),5_000);
  await client.call('apply',request({operation:'add',text:'Task'}));assert.equal(deadlines.at(-1),5_000);
  const items=Array.from({length:12},(_,i)=>({name:'Person '+i,correction:'Person '+i}));
  await client.call('apply',request({operation:'resolve_many',allow_partial:true,selection_mode:'named_people',source_revision:hex,items}));assert.equal(deadlines.at(-1),29_000);
  assert.deepEqual(deadlines,[29_000,60_000,5_000,5_000,5_000,5_000,29_000]);
  assert.equal(calls,7,'each uncertain request makes exactly one transport attempt');
 }finally{AbortSignal.timeout=original;}
});

test('batch deadline never masks caller cancellation or retries an uncertain apply',async()=>{
 const controller=new AbortController();let calls=0;
 const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:hex,fetch:async(_url,init)=>{calls++;controller.abort();assert.ok(init?.signal?.aborted);return new Promise<Response>(()=>{});}});
 await assert.rejects(()=>client.call('apply',request(cleanup(12)),controller.signal),error=>{
  assert.ok(error instanceof CleanupDiagnosticError);
  assert.deepEqual(error.diagnostic,{cleanup_stage:'transport',cleanup_category:'aborted'});
  assert.ok(!error.message.includes('Owner task request'));
  return true;
 });
 assert.equal(calls,1);
});
