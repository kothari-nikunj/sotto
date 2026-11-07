import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {createHash} from 'node:crypto';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {RequestContext} from '@mastra/core/request-context';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator,type ModelRequest} from '../src/conversation.js';
import {createOwnerContactsClient} from '../src/tools/owner-contacts-client.js';
import {createOwnerContactsTools} from '../src/tools/owner-contacts-tool.js';
import {contactsActionIdentity,contactsReviewHash,contactsCanonical,contactsPreviewMatches,contactsReceiptMatches,contactWriteFieldsSchema,contactsReviewText,type ContactsPreview} from '../src/tools/owner-contacts-contract.js';
import {parseContactsCommand} from '../src/tools/contacts-command.js';
const revision='a'.repeat(64),fields={names:[{givenName:'Dana'}]},review={operation:'create' as const,resource_name:null,before:null,fields,source_revision:revision,review_hash:''};review.review_hash=contactsReviewHash(review);
const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:revision};
const context={threadId:'thread',destination:'photon',lineKey:'line'},inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId,text});
function fixture(lost=false){const folder=mkdtempSync(join(tmpdir(),'contacts-proof-')),journal=new OperationalJournal(join(folder,'operations.sqlite'));let contacts=0;const client=createOwnerContactsClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async(_i,init)=>{contacts++;if(lost)throw Error('lost readonly preview');const b=JSON.parse(String(init?.body));assert.equal(b.operation,'preview');return new Response(JSON.stringify({version:1,status:'preview',event_id:b.event_id,payload_hash:b.payload_hash,review}));}});const tools=createOwnerContactsTools({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision,bridge_sources:[],google_sources:['google_contacts']})}});const call=(name:keyof typeof tools,input:unknown,r:ModelRequest)=>tools[name].execute!(input as never,{requestContext:new RequestContext(Object.entries({mode:'chat',runId:r.runId,ownerRequestKey:r.ownerRequestKey,allowOwnerSkill:true,sourceRevision:revision})),abortSignal:r.signal} as never);return {journal,client,call,get contacts(){return contacts;},close(){journal.close();rmSync(folder,{recursive:true,force:true});}};}
test('Contacts original field arrays, codepoint hash, closed argv and immutable receipt binding',()=>{
 const vector={birthdays:[{date:{day:6,month:10,year:2026},'\ue000':'bmp','𐀀':'astral'}]};assert.equal(createHash('sha256').update(contactsCanonical(vector)).digest('hex'),'e09e2fc8aba2142e5c472f6997d9f6302c4a89f9e3baef4e0d96c87289e12dec');
 assert.equal(contactWriteFieldsSchema.safeParse(vector).success,true);for(const n of [0.5,Number.MAX_SAFE_INTEGER+1,Infinity])assert.equal(contactWriteFieldsSchema.safeParse({birthdays:[{date:{year:n}}]}).success,false);
 assert.equal(contactWriteFieldsSchema.safeParse({unknown:[]}).success,false);assert.equal(contactWriteFieldsSchema.safeParse({names:['generic original JSON',null,1]}).success,true);
 assert.deepEqual(parseContactsCommand(['contacts-create','--fields-json',JSON.stringify(fields)]),{kind:'write',args:{operation:'propose_create',fields}});assert.throws(()=>parseContactsCommand(['confirm-contacts','--resource-name','people/cWrong']));
 const p={version:1 as const,status:'preview' as const,event_id:revision,payload_hash:revision,review};assert.equal(contactsPreviewMatches(p,{operation:'propose_create',fields}),true);assert.equal(contactsPreviewMatches(p,{operation:'propose_create',fields:{names:[]}}),false);
 const args={operation:'create' as const,review,proposal:{event_id:revision,action_id:revision,args_hash:revision,accepted_dispatch_id:'accepted',source_revision:revision}};const r={version:1 as const,status:'committed' as const,action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,outcome:{operation:'create' as const,status:'created' as const,resource_name:'people/cCreated',review_hash:review.review_hash}};assert.equal(contactsReceiptMatches(r,args),true);assert.equal(contactsReceiptMatches({...r,outcome:{...r.outcome,review_hash:'b'.repeat(64)}},args),false);
});
test('early, undelivered, wrong reply target and intervening owner input refuse Contacts confirmation without apply',async()=>{
 for(const mode of ['early','undelivered','wrong-target','intervening']){const f=fixture();let failed=0;const sent:string[]=[];const c=new ConversationCoordinator({journal:f.journal,ownerContacts:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async r=>{if(r.text==='Create')await f.call('contacts_create_review',{fields},r);else if(r.text==='yes'){await assert.rejects(f.call('contacts_confirm',{},r));failed++;}return 'ordinary response';}},postPrepared:async p=>{sent.push(p.text);return mode==='undelivered'?{acceptance:'unknown'}:{acceptance:'accepted',messageId:'accepted-'+sent.length};}});
 try{if(mode!=='early')await c.receive(inbound('create','Create'),context);if(mode==='intervening')await c.receive(inbound('intervening','Another question'),context);await c.receive({...inbound('yes','yes'),...(mode==='wrong-target'?{replyBound:true,replyToMessageId:'other'}:{})},context);assert.equal(f.contacts,mode==='early'?0:1);if(mode!=='undelivered')assert.equal(failed,1);}finally{await c.close();f.close();}}
});
test('lost readonly Contacts preview makes no uncertain effect record and current grant remains required',async()=>{const f=fixture(true);const c=new ConversationCoordinator({journal:f.journal,ownerContacts:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async r=>{await assert.rejects(f.call('contacts_create_review',{fields},r));return 'Review unavailable.';}},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});try{await c.receive(inbound('create','Create'),context);assert.equal(f.journal.db.prepare("SELECT count(*) n FROM actions WHERE kind='owner_contacts_tool'").get()!.n,0);assert.equal(f.contacts,1);}finally{await c.close();f.close();}});
test('Calendar action excludes Contacts in same event; revoked full review is never delivered on recovery',async()=>{
 const f=fixture();let sourceAllowed=true,sends=0;
 const c=new ConversationCoordinator({journal:f.journal,ownerContacts:f.client,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},model:{generate:async r=>{
  if(r.text==='Calendar first'){
   f.journal.prepareOwnerCalendarAction({runId:r.runId,accountId:'account',ownerId:'owner',channel:'photon',lineKey:'line',scopeHash:f.client.scopeHash},r.ownerRequestKey!,{operation:'propose_move',event_id:'cedar',start:'2026-10-10T10:00:00-07:00',end:'2026-10-10T11:00:00-07:00'},revision);
   await assert.rejects(f.call('contacts_create_review',{fields},r),/another private mutation/);
  }else{await f.call('contacts_create_review',{fields},r);sourceAllowed=false;}
  return {text:'ordinary',sourceRevision:revision};
 }},canPostPrepared:async()=>sourceAllowed,postPrepared:async()=>{sends++;return {acceptance:'accepted',messageId:'reply'};}});
 try{await c.receive(inbound('mixed','Calendar first'),context);assert.equal(f.journal.db.prepare("SELECT count(*) n FROM actions WHERE kind='owner_contacts_tool'").get()!.n,0);const before=sends;await c.receive(inbound('contacts','Create'),context);assert.equal(sends,before);await c.recover();assert.equal(sends,before);}finally{await c.close();f.close();}
});
test('Contacts shared integer/Unicode proof and scoped common argv bounds preserve legacy limits',()=>{
 const fields={birthdays:[{date:{day:6,month:10,year:2026},'\ue000':'bmp','𐀀':'astral','10':'ten','2':'two'}]};const args={operation:'propose_create' as const,fields};
 assert.deepEqual(contactsActionIdentity(revision,'owner_contacts_tool',args),{payloadHash:'3a273b5590d48964f5bd1df59ec36c6b9db55b36e649785504697f624b535cc9',actionId:'2579c0f757aef26bb8eb6d0cb465aeb17dae4a721fe47f1f5b20d129020aa50c'});
 assert.equal(contactsReviewHash({...review,fields}),'96ceef1f7ff39d74077824f144e57ece5c788d5dd7375b68a867ec48593a26fa');
 const t=createRunScriptTool({mode:'chat',contactsCommandIds:['google-action'],commandContext:()=>({}),executeCommand:async()=>({backend:'synthetic',coverage:'test',result:{}})});const schema=t.inputSchema as unknown as {safeParse(v:unknown):{success:boolean}};
 const large=JSON.stringify({biographies:[{value:'é'.repeat(5000)}]});assert.ok(large.length>4096);assert.equal(schema.safeParse({id:'google-action',args:['contacts-create','--fields-json',large]}).success,true);
 const calendarDetails=JSON.stringify({description:'🌲'.repeat(4000)});
 assert.equal(schema.safeParse({id:'google-action',args:['calendar-update','--event-id','event123','--fields-json',calendarDetails]}).success,true);
 for(const input of [{id:'google-action',args:['calendar-update','--event-id',calendarDetails]},{id:'unrecognized',args:['calendar-update','--fields-json',calendarDetails]},{id:'google-action',args:['calendar-update-confirm','--fields-json',calendarDetails]}])assert.equal(schema.safeParse(input).success,false);
 for(const input of [{id:'preferences',args:[large]},{id:'google-action',args:['contacts-search','--query',large]},{id:'unrecognized',args:['contacts-create','--fields-json',large]}])assert.equal(schema.safeParse(input).success,false);
});
test('full readable Contacts review preserves nested rich values safely and refuses oversized delivery',async()=>{
 const rich={biographies:[{value:'First line\nSecond line\\end',metadata:{primary:true,'key\nInjected':'value\u2028tail'}}]};
 const p={version:1 as const,status:'preview' as const,event_id:revision,payload_hash:revision,review:{...review,fields:rich}};p.review.review_hash=contactsReviewHash(p.review);
 const text=contactsReviewText(p);assert.ok(text.includes('Value: First line\\nSecond line\\\\end'));assert.ok(text.includes('Key\\nInjected: value\\u2028tail'));assert.ok(!text.includes('\nInjected'));assert.match(text,/Additional details:\n/);
 const large={names:Array.from({length:1000},()=>({x:[]}))};const huge={...p,review:{...review,fields:large}};huge.review.review_hash=contactsReviewHash(huge.review);assert.equal(contactWriteFieldsSchema.safeParse(large).success,true);assert.ok(contactsReviewText(huge).length>16000);
 const client=createOwnerContactsClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async()=>new Response(JSON.stringify(huge))});const args={operation:'propose_create' as const,fields:large},binding=contactsActionIdentity(revision,'owner_contacts_tool',args);
 await assert.rejects(client.call('preview',{actionId:binding.actionId,eventId:revision,payloadHash:revision,text:'Create my contact.',admittedAtMs:Date.now(),providerThreadId:'thread',args}),/review is invalid/);
});
test('original Google capabilities metadata works with zero grants and refuses changed source authority',async()=>{
 const {readOwnerGoogle}=await import('../src/agent/google-read.js');let changed=false,reads=0;
 const reader={read:async()=>({version:2 as const,complete:true,revision:changed?'b'.repeat(64):revision,bridge_sources:[],google_sources:[]})};
 const options={bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'synthetic-read-bearer',sourceAuthority:reader,fetch:async()=>{reads++;return new Response(JSON.stringify({version:1,source:'google',operation:'capabilities',observation:{status:'ok',observed_at:'2026-10-06T00:00:00Z',complete:true,coverage:{since:null,until:null}},item:{status:'ok',connected:false,gmail_read:false,gmail_draft:false,gmail_write:false,gmail_send:false,calendar_read:false,calendar_write:false,contacts_read:false,contacts_write:false}}));}};
 assert.ok(await readOwnerGoogle(options,'capabilities',{},revision));assert.equal(reads,1);changed=true;await assert.rejects(readOwnerGoogle(options,'capabilities',{},revision));assert.equal(reads,1);
});
