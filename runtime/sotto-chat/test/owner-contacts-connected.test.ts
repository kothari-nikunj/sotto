import assert from 'node:assert/strict';
import test from 'node:test';
import {existsSync,readFileSync,writeFileSync,mkdtempSync,mkdirSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {DatabaseSync} from 'node:sqlite';
import {Message} from 'chat';
import {startConfiguredRuntime} from '../src/bootstrap/main.js';
import {startManagedReceiver, stopManagedReceiver as stopReceiver} from './helpers/receiver.js';
import {contactsReviewText} from '../src/tools/owner-contacts-contract.js';
const startReceiver = (folder: string) => startManagedReceiver(folder, {
  SOTTO_TEST_CONTACTS: '1',
});

test('configured original People Contacts CRUD reviews, approval, lost receipt and held unknown use original writer once',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'contacts-connected-'));let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined,db:DatabaseSync|undefined;
 let phase='create',step=0,lose=false,lastReview='';const sent:string[]=[];
 let unknownResponseReady=false,releaseUnknown=()=>{};let unknownResponseHold:Promise<void>|undefined;
 const fields={names:[{givenName:'Dana',familyName:'Cedar'}],emailAddresses:[{value:'dana@example.com'}],phoneNumbers:[{value:'+15555550122'}],organizations:[{name:'Cedar'}],birthdays:[{date:{year:1990,month:1,day:2}}],addresses:[{streetAddress:'1 Cedar Lane'}],biographies:[{value:'Owner-stated context '+ 'é'.repeat(5000)}]};
 const steps:Record<string,(unknown|null)[]>={create:[{name:'skill',args:{name:'sotto-people'}},{name:'run_sotto_script',args:{id:'google-action',args:['capabilities']}},{name:'run_sotto_script',args:{id:'google-action',args:['contacts-search','--query','Dana']}},{name:'run_sotto_script',args:{id:'google-action',args:['contacts-get','--resource-name','people/cAlice']}},{name:'run_sotto_script',args:{id:'google-action',args:['contacts-create','--fields-json',JSON.stringify(fields)]}},null],yes:[{name:'run_sotto_script',args:{id:'google-action',args:['confirm-contacts']}},null],update:[{name:'run_sotto_script',args:{id:'google-action',args:['contacts-update','--resource-name','people/cCreated','--fields-json',JSON.stringify({phoneNumbers:[{value:'+15555550999'}]})]}},null],lost:[{name:'run_sotto_script',args:{id:'google-action',args:['confirm-contacts']}},null],delete:[{name:'run_sotto_script',args:{id:'google-action',args:['contacts-delete','--resource-name','people/cCreated']}},null],unknown:[{name:'run_sotto_script',args:{id:'google-action',args:['confirm-contacts']}},null],hello:[null]};
 const wait=async(p:()=>boolean)=>{const end=Date.now()+15000;while(!p()){if(Date.now()>end)throw Error('Contacts local turn timeout');await new Promise(r=>setTimeout(r,10));}};
 try{
  receiver=await startReceiver(join(folder,'receiver'));const thread=receiver.identity.route_id,media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const config={version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-contacts-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use original skills and enabled commands.',skillPackRoot:process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver.bridgeUrl,readBearer:receiver.readBearer},ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}};
  const configPath=join(folder,'runtime.json');writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async()=>null,addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  const external:typeof fetch=async(input,init)=>{
   const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
   if(url.origin!=='https://proxy.example'){assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin);const response=await fetch(input,init);if(url.pathname==='/runtime/google-read'&&JSON.parse(String(init?.body)).operation==='capabilities'){const result=await response.clone().json();assert.equal(result.item.connected,true);assert.equal(result.item.contacts_write,true);assert.equal(result.item.gmail_write,false);}
    if(url.pathname==='/runtime/owner-contacts-tool'){const result=await response.clone().json();if(result.status==='preview')lastReview=contactsReviewText(result);
     if(phase==='unknown'&&JSON.parse(String(init?.body)).operation==='apply'&&unknownResponseHold){unknownResponseReady=true;await unknownResponseHold;}}
    if(lose&&url.pathname==='/runtime/owner-contacts-tool'&&JSON.parse(String(init?.body)).operation==='apply'){lose=false;assert.equal((await response.clone().json()).status,'committed');throw Error('synthetic lost receipt');}return response;}
   const body=JSON.parse(String(init?.body));if(phase==='yes'&&step===0)assert.ok(JSON.stringify(body.messages).includes(JSON.stringify(lastReview).slice(1,-1)),'accepted exact review retained for distinct yes');const call=steps[phase]![step++] as {name:string;args:unknown}|null;
   if(phase==='create'&&step===2)assert.ok(JSON.stringify(body.messages).includes('Sotto — People'));
   if(phase==='create'&&step===3){const text=JSON.stringify(body.messages);assert.ok(text.includes('contacts_write')&&text.includes('connected'));}
   if(phase==='create'&&step===6)assert.ok(JSON.stringify(body.messages).includes('needs_owner_confirmation'));
   return new Response(JSON.stringify({id:'synthetic-'+phase+'-'+step,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'call-'+phase+'-'+step,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:phase==='hello'?'Hello.':['create','update','delete'].includes(phase)?lastReview:'Review complete.'},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
  };
  const mount=async()=>startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async r=>{sent.push(r.text??'');return {acceptance:'accepted',providerMessageIds:['accepted-'+sent.length]};}}),openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
  app=await mount();db=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
  const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  const turn=async(p:string,text:string,reply=true)=>{phase=p;step=0;const n=sent.length;await app!.gateway.chat.processMessage(adapter as never,thread,message('owner-'+p,text));if(reply)await wait(()=>sent.length>n);else if(p==='unknown'&&unknownResponseHold)await wait(()=>unknownResponseReady);else await wait(()=>Boolean(db!.prepare("SELECT id FROM actions WHERE kind='owner_contacts_tool' AND status='unknown'").get()));};
  const provider=()=>existsSync(join(folder,'receiver/contacts-provider.jsonl'))?readFileSync(join(folder,'receiver/contacts-provider.jsonl'),'utf8').trim().split('\n').map(x=>JSON.parse(x)):[];
  await turn('create','Add Dana Cedar to Google Contacts with the details I supplied.');assert.equal(provider().length,0);assert.match(sent[0]!,/Add Google contact/);assert.match(sent[0]!,/First name: Dana/);
  const preview=JSON.parse(db.prepare("SELECT result_json FROM actions WHERE kind='owner_contacts_tool'").get()!.result_json as string).preview;assert.equal(sent[0],contactsReviewText(preview));
  await turn('yes','Yes.');assert.equal(sent.at(-1),'Created the Google contact.');assert.equal(provider().length,1);
  await turn('update','Replace Dana’s phone number with +15555550999.');assert.match(sent.at(-1)!,/Other details stay unchanged/);assert.equal(provider().length,1);
  lose=true;await turn('lost','Yes.',false);assert.equal(provider().length,2);await app.close();app=undefined;db.close();db=undefined;phase='hello';step=0;app=await mount();db=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});await wait(()=>sent.some(text=>text==='Updated the Google contact.'||text.startsWith('I couldn’t confirm whether the contact change happened.')));assert.equal(sent.filter(text=>text==='Updated the Google contact.'||text.startsWith('I couldn’t confirm whether the contact change happened.')).length,1);assert.equal(provider().length,2);
  const current=JSON.parse(readFileSync(join(folder,'receiver/contacts-provider.json'),'utf8'))['people/cCreated'];assert.deepEqual(current.emailAddresses,fields.emailAddresses);assert.deepEqual(current.phoneNumbers,[{value:'+15555550999'}]);
  await turn('delete','Delete Dana’s exact Google contact.');assert.match(sent.at(-1)!,/Current details:/);assert.equal(provider().length,2);
  // Admit the safe message before releasing the uncertain tool response. Merely
  // polling action.status raced the automatic notice and did not establish this ordering.
  unknownResponseHold=new Promise<void>(resolve=>{releaseUnknown=resolve;});
  writeFileSync(join(folder,'receiver/contacts-unknown'),'1');await turn('unknown','Yes.',false);assert.equal(provider().length,3);
  const hello=turn('hello','Hello.');
  await wait(()=>Boolean(db!.prepare("SELECT id FROM inbound_events WHERE source_id='owner-hello'").get()));
  releaseUnknown();await hello;await wait(()=>sent.at(-1)!.startsWith('I couldn’t confirm whether the contact change happened.'));assert.equal(sent.at(-2),'Hello.','safe chat first, then the notice in the same running turn');assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the contact change happened.')).length,1);assert.equal(provider().length,3);
  await app.close();app=undefined;db.close();db=undefined;phase='hello';step=0;app=await mount();assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the contact change happened.')).length,1,'restart never resends the notice');assert.equal(provider().length,3);
 }finally{releaseUnknown();await app?.close();db?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
