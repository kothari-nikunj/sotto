import assert from 'node:assert/strict';
import test from 'node:test';
import {existsSync,readFileSync,writeFileSync,mkdtempSync,mkdirSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {DatabaseSync} from 'node:sqlite';
import {Message} from 'chat';
import {startConfiguredRuntime} from '../src/bootstrap/main.js';
import {startManagedReceiver, stopManagedReceiver as stopReceiver} from './helpers/receiver.js';
import {bridgeSendReviewText} from '../src/tools/owner-bridge-send-contract.js';
const startReceiver = (folder: string) => startManagedReceiver(folder, {
  SOTTO_TEST_BRIDGE_SEND: '1',
});

test('configured chat reviews a Mac message, confirms once, survives restart and holds a lost send',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'bridge-send-connected-'));let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined,db:DatabaseSync|undefined;
 let phase='review',step=0,lastReview='',turnId=0;const sent:string[]=[];
 const steps:Record<string,({name:string;args:unknown}|null)[]>={
  review:[{name:'bridge_send_review',args:{channel:'imessage',to:'+15555550123',body:'Hi!'}},null],
  yes:[{name:'bridge_send_confirm',args:{}},null],
  second:[{name:'bridge_send_confirm',args:{}},null],
  retry:[{name:'bridge_send_review',args:{channel:'imessage',to:'+15555550123',body:'Another message'}},null],
  unknown:[{name:'bridge_send_confirm',args:{}},null],hello:[null]};
 const wait=async(p:()=>boolean)=>{const end=Date.now()+15000;while(!p()){if(Date.now()>end)throw Error('Bridge local turn timeout '+JSON.stringify({phase,step,lastReview,actions:db?.prepare('SELECT kind,status,result_json FROM actions').all()}));await new Promise(r=>setTimeout(r,10));}};
 try{
  receiver=await startReceiver(join(folder,'receiver'));const thread=receiver.identity.route_id,media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const config={version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-contacts-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use original skills and enabled commands.',skillPackRoot:process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver.bridgeUrl,readBearer:receiver.readBearer},ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}};
  const configPath=join(folder,'runtime.json');writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async()=>null,addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  const external:typeof fetch=async(input,init)=>{
   const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
   if(url.origin!=='https://proxy.example'){
    assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin);const response=await fetch(input,init);
    if(url.pathname==='/runtime/owner-bridge-send-tool'){
      const result=await response.clone().json();if(result.status==='preview')lastReview=bridgeSendReviewText(result);
    }return response;
   }
   const body=JSON.parse(String(init?.body));
   const offered=(body.tools??[]).map((t:any)=>t.function.name);
   assert.ok(offered.includes('bridge_send_review'));assert.ok(!offered.includes('bridge_send_message'));
   assert.ok(!JSON.stringify(body.messages).includes('Bridge send remains unavailable'));
   const call=steps[phase]![step++];
   return new Response(JSON.stringify({id:'synthetic-'+phase+'-'+step,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'call-'+phase+'-'+step,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:phase==='hello'?'Hello.':['review','retry'].includes(phase)?lastReview:'Review complete.'},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
  };
  const mount=async()=>startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async r=>{sent.push(r.text??'');return {acceptance:'accepted',providerMessageIds:['accepted-'+sent.length]};}}),openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
  app=await mount();db=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
  const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  const turn=async(p:string,text:string,reply=true)=>{phase=p;step=0;const n=sent.length;await app!.gateway.chat.processMessage(adapter as never,thread,message('owner-'+(++turnId),text));if(reply)await wait(()=>sent.length>n);else await wait(()=>Boolean(db!.prepare("SELECT id FROM actions WHERE kind='owner_bridge_send_tool' AND status='unknown'").get()));};
  const provider=()=>existsSync(join(folder,'receiver/bridge-sends.jsonl'))?readFileSync(join(folder,'receiver/bridge-sends.jsonl'),'utf8').trim().split('\n').map(x=>JSON.parse(x)):[];
  for (const interruption of ['/stop','/new','actually never mind']) {
    await turn('review','Send hi to +15555550123 from my Mac');
    await turn('hello',interruption);
    await turn('yes','yes');
    assert.equal(provider().length,0,'intervening owner input invalidates the review: '+interruption);
  }
  await turn('review','Send hi to +15555550123 from my Mac');assert.equal(provider().length,0);assert.equal(sent.at(-1),lastReview);assert.match(lastReview,/Hi!/);
  await app.close();app=await mount();
  await turn('yes','send it');assert.equal(provider().length,1);assert.match(sent.at(-1)!,/Sent from your Mac/);
  await turn('second','Yes');assert.equal(provider().length,1,'same review cannot be consumed again');
  await turn('retry','Send another message to +15555550123');writeFileSync(join(folder,'receiver/send-lost'),'1');
  await turn('unknown','yes, send it',false);assert.equal(provider().length,2);
  await app.close();app=await mount();await turn('hello','hello');assert.equal(provider().length,2,'restart and later chat never resend an uncertain message');
 }finally{await app?.close();db?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
