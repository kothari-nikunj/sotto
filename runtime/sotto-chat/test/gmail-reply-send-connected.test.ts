import assert from 'node:assert/strict';
import test from 'node:test';
import {existsSync,readFileSync,writeFileSync,mkdtempSync,mkdirSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {DatabaseSync} from 'node:sqlite';
import {Message} from 'chat';
import {startConfiguredRuntime} from '../src/bootstrap/main.js';
import {startManagedReceiver, stopManagedReceiver as stopReceiver} from './helpers/receiver.js';
import {gmailDraftReviewText} from '../src/tools/owner-gmail-draft-contract.js';
const startReceiver = (folder: string) => startManagedReceiver(folder, {
  SOTTO_TEST_GMAIL_SEND: '1', SOTTO_HOME: join(folder, 'sotto-home'),
});

for (const scenario of ['lost-committed','postcontact-unknown'] as const) test(`default original Draft-reply -> protected Gmail read -> exact send review -> distinct yes; ${scenario} no replay`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'gmail-reply-send-'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined,observer:DatabaseSync|undefined;
 let phase='review',step=0,blockInspect=true,loseResponse=scenario==='lost-committed';const sent:string[]=[];let applies=0;
 const body='I reviewed the Cedar packet.\nThank you.';
 const waitFor=async(predicate:()=>boolean)=>{const until=Date.now()+15000;while(!predicate()){if(Date.now()>until)throw Error('synthetic send turn did not settle');await new Promise(resolve=>setTimeout(resolve,10));}};
 try{
  receiver=await startReceiver(join(folder,'receiver'));const thread=receiver.identity.route_id;
  const media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const configPath=join(folder,'runtime.json');
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async()=>null,addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  const external:typeof fetch=async(input,init)=>{
   const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
   if(url.origin!=='https://proxy.example'){
    assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin);
    const request=JSON.parse(String(init?.body??'{}'));
    if(url.pathname==='/runtime/owner-gmail-draft-tool'&&request.operation==='inspect'&&blockInspect)throw Error('synthetic inspection unavailable');
    if(url.pathname==='/runtime/owner-gmail-draft-tool'&&request.operation==='apply')applies++;
    const response=await fetch(input,init);
    if(url.pathname==='/runtime/owner-gmail-draft-tool'&&request.operation==='apply'){
     const result=await response.clone().json();assert.equal(result.status,scenario==='lost-committed'?'committed':'unknown');
     if(loseResponse){await response.body?.cancel();throw Error('synthetic committed response lost');}
    }
    return response;
   }
   assert.equal(url.pathname,'/openai/v1/chat/completions');const request=JSON.parse(String(init!.body));
   const calls=phase==='review'?[{name:'skill',args:{name:'sotto-draft-reply'}},{name:'gmail_search',args:{query:'from:alice@example.com subject:Cedar',max:5}},{name:'gmail_get',args:{message_id:'message-123'}},{name:'gmail_send_review',args:{message_id:'message-123',body}},null]:phase==='confirm'?[{name:'gmail_send_confirm',args:{}},null]:[null];
   const call=calls[step++];
   if(phase==='review'&&step===2)assert.ok(JSON.stringify(request.messages).includes('Reply within the thread'),'actual original Draft-reply loaded');
   if(phase==='review'&&step===3)assert.ok(JSON.stringify(request.messages).includes('message-123'),'actual search identity observed');
   if(phase==='review'&&step===4)assert.ok(JSON.stringify(request.messages).includes('Alice <alice@example.com>'),'actual source message observed');
   return new Response(JSON.stringify({id:'synthetic-'+phase+'-'+step,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'call-'+phase+'-'+step,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:'Hello.'},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
  };
  const mount=async()=>{
   const config={version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-gmail-send-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver!.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use the original loaded skills and enabled tools.',skillPackRoot:process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver!.bridgeUrl,readBearer:receiver!.readBearer},ownerLoop:{mode:'explicit_command',identity:receiver!.identity,bearer:receiver!.bearer}};
   writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
   app=await startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['accepted-'+sent.length]};}}),openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
   observer=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
  };
  await mount();
  const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  await app!.gateway.chat.processMessage(adapter as never,thread,message('request','Reply to Alice about Cedar review: I reviewed the Cedar packet. Thank you.'));
  await waitFor(()=>sent.length===1);
  const previewAction=observer!.prepare("SELECT * FROM actions WHERE json_extract(result_json,'$.args.operation')='propose_send'").get()!;
  const preview=JSON.parse(String(previewAction.result_json)).preview;assert.equal(preview.review.kind,'reply_send');assert.equal(sent[0],gmailDraftReviewText(preview));assert.equal(preview.review.body,body);assert.equal(preview.review.sender_email,'owner@example.com');assert.equal(preview.review.references,'<earlier@example.com>');
  const providerPath=join(folder,'receiver/provider-calls.jsonl');assert.equal(existsSync(providerPath),false,'zero provider send before distinct yes');
  if(scenario==='postcontact-unknown')writeFileSync(join(folder,'receiver/provider_unknown'),'1');
  phase='confirm';step=0;await app!.gateway.chat.processMessage(adapter as never,thread,message('yes','Yes, send it.'));
  await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE status='unknown'").get()));
  // The single uncertainty notice may already have been sent in this running turn; a lost response cannot reply yet.
  if(scenario==='lost-committed')assert.equal(sent.length,1);
  const contacts=readFileSync(providerPath,'utf8').trim().split('\n').map(line=>JSON.parse(line));assert.equal(contacts.length,1);assert.equal(contacts[0].operation,'messages.send');
  const mime=Buffer.from(contacts[0].payload.raw,'base64url').toString();assert.match(mime,/To: alice@example.com/);assert.match(mime,/Subject: Re: Cedar review/);assert.match(mime,/In-Reply-To: <cedar@example.com>/);assert.match(mime,/References: <earlier@example.com> <cedar@example.com>/);assert.equal(Buffer.from(mime.split('\n\n')[1]!.replace(/\s/g,''),'base64').toString(),body);
  observer!.close();observer=undefined;await app!.close();app=undefined;await stopReceiver(receiver.child);receiver=await startReceiver(join(folder,'receiver'));blockInspect=false;loseResponse=false;phase='safe';step=0;await mount();
  if(scenario==='lost-committed'){await waitFor(()=>sent.length===2);assert.equal(sent[1],'Email sent.');}
  else{await waitFor(()=>sent.length===2);assert.match(sent[1]!,/^I couldn’t confirm whether the Gmail action happened\./);assert.ok(observer!.prepare("SELECT id FROM actions WHERE status='unknown'").get());}
  const before=sent.length;await app!.gateway.chat.processMessage(adapter as never,thread,message('safe','Hello'));await waitFor(()=>sent.length===before+1);assert.equal(sent.at(-1),'Hello.');assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the Gmail action happened.')).length,scenario==='lost-committed'?0:1);assert.equal(applies,1);assert.equal(readFileSync(providerPath,'utf8').trim().split('\n').length,1);
  const reads=readFileSync(join(folder,'receiver/provider-reads.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));if(scenario==='lost-committed')assert.ok(reads.some(row=>row.path.endsWith('/sent-123')&&row.format?.includes('raw')),'actual sent-message readback');
 }finally{observer?.close();await app?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
