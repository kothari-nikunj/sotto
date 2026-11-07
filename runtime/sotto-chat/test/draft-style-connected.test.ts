import assert from 'node:assert/strict';
import test from 'node:test';
import {existsSync,readFileSync,writeFileSync,mkdtempSync,mkdirSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {DatabaseSync} from 'node:sqlite';
import {spawnSync} from 'node:child_process';
import {Message} from 'chat';
import {startConfiguredRuntime} from '../src/bootstrap/main.js';
import {startManagedReceiver, stopManagedReceiver as stopReceiver} from './helpers/receiver.js';
import {gmailDraftReviewText} from '../src/tools/owner-gmail-draft-contract.js';
const startReceiver = (folder: string) => startManagedReceiver(folder, {
  SOTTO_TEST_GMAIL_DRAFT: '1', SOTTO_TEST_STYLE_APPLY: '1', SOTTO_HOME: join(folder, 'sotto-home'),
});

test('default original Draft-reply uses original style before Accept/Decline; selected Decline saves once after review',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'draft-style-'));const sent:string[]=[];
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined,observer:DatabaseSync|undefined;
 let phase='options',step=0;let styleSeen=false,skillSeen=false;
 const decline='Thanks for asking. I will pass on this one.';
 const waitFor=async(predicate:()=>boolean)=>{const until=Date.now()+20000;while(!predicate()){if(Date.now()>until)throw Error('draft style turn did not settle');await new Promise(resolve=>setTimeout(resolve,10));}};
 try{
  receiver=await startReceiver(join(folder,'receiver'));const thread=receiver.identity.route_id;
  const media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async()=>null,addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  const external:typeof fetch=async(input,init)=>{
   const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
   if(url.origin!=='https://proxy.example'){assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin);return fetch(input,init);}
   const request=JSON.parse(String(init!.body));const prompt=JSON.stringify(request.messages);
   const calls=phase==='options'?[
    {name:'skill',args:{name:'sotto-draft-reply'}},
    {name:'gmail_search',args:{query:'from:alice@example.com subject:Cedar',max:5}},
    {name:'gmail_get',args:{message_id:'message-123'}},
    {name:'run_sotto_script',args:{id:'knowledge-query',args:['--person','Alice','--topic','Cedar review']}},
    {name:'run_sotto_script',args:{id:'style-apply',args:[JSON.stringify({recipient:'Alice',channel:'email'})]}},null
   ]:phase==='review'?[{name:'gmail_draft_propose',args:{message_id:'message-123',body:decline}},null]:[{name:'gmail_draft_save',args:{}},null];
   const call=calls[step++];
   if(phase==='options'&&step===2){assert.ok(prompt.includes('run_sotto_script style-apply'));assert.ok(prompt.includes('Decline'));skillSeen=true;}
   if(phase==='options'&&step===4)assert.ok(prompt.includes('Alice <alice@example.com>'));
   if(phase==='options'&&step===6){assert.ok(prompt.includes('How to write this'));assert.ok(prompt.includes(decline));assert.ok(prompt.includes('work_email'));assert.ok(prompt.includes('The samples above guide style only, not current facts, recipients, or completed owner actions.'));assert.ok(!prompt.includes('The samples above are the ground truth'));styleSeen=true;}
   const content=phase==='options'?'Accept: Happy to review the packet.\nDecline: '+decline:'Hello.';
   return new Response(JSON.stringify({id:'synthetic-'+phase+'-'+step,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'call-'+phase+'-'+step,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
  };
  const configPath=join(folder,'runtime.json');
  writeFileSync(configPath,JSON.stringify({version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-draft-style-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use the original loaded skills and enabled tools.',skillPackRoot:resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver.bridgeUrl,readBearer:receiver.readBearer},ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}}),{mode:0o600});
  app=await startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['accepted-'+sent.length]};}}),openStateDatabase:()=>new DatabaseSync(':memory:')});
  observer=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
  const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  const stylePath=join(folder,'receiver/style.json');const styleBefore=readFileSync(stylePath,'utf8');
  await app.gateway.chat.processMessage(adapter as never,thread,message('request','Help me reply to Alice about the Cedar review request.'));
  await waitFor(()=>sent.length===1);assert.ok(skillSeen&&styleSeen);assert.match(sent[0]!,/Accept:/);assert.match(sent[0]!,/Decline:/);
  const providerPath=join(folder,'receiver/provider-calls.jsonl');assert.equal(existsSync(providerPath),false);
  const health=await(await fetch(`http://127.0.0.1:${app.port}/v1/health`,{headers:{authorization:'Bearer synthetic-draft-style-at-least-32-characters'}})).json();
  const expected=[{tool:'gmail_search',status:'unknown',error:'none'}, {tool:'gmail_get',status:'unknown',error:'none'},
   {tool:'run_sotto_script',script:'knowledge-query',status:'unknown',error:'none'},
   {tool:'run_sotto_script',script:'style-apply',status:'unknown',error:'none'}];
  assert.deepEqual(health.taskToolDiagnostics.events,expected);
  const projection=spawnSync(process.env.SOTTO_PYTHON??process.env.PYTHON??'python3',['-c',
   "import json,sys;sys.path.insert(0,sys.argv[1]);import runtime_api;print(json.dumps(runtime_api._task_tool_diagnostics_projection(json.load(sys.stdin))))",
   resolve('../../adapters/sotto')],{input:JSON.stringify(health.taskToolDiagnostics),encoding:'utf8',timeout:10000});
  assert.equal(projection.status,0,projection.stderr);
  assert.deepEqual(JSON.parse(projection.stdout).events,expected);
  assert.ok(!JSON.stringify(health.taskToolDiagnostics).includes('Alice'));
  assert.ok(!JSON.stringify(health.taskToolDiagnostics).includes('message-123'));

  phase='review';step=0;await app.gateway.chat.processMessage(adapter as never,thread,message('choice','Use the Decline option.'));
  await waitFor(()=>sent.length===2);
  const row=observer.prepare("SELECT result_json FROM actions WHERE json_extract(result_json,'$.args.operation')='propose'").get()!;
  const preview=JSON.parse(String(row.result_json)).preview;assert.equal(preview.review.body,decline);assert.equal(sent[1],gmailDraftReviewText(preview));assert.equal(existsSync(providerPath),false);
  phase='confirm';step=0;await app.gateway.chat.processMessage(adapter as never,thread,message('yes','Yes'));
  await waitFor(()=>sent.length===3);assert.match(sent[2]!,/draft/i);
  const contacts=readFileSync(providerPath,'utf8').trim().split('\n').map(line=>JSON.parse(line));assert.equal(contacts.length,1);assert.equal(contacts[0].operation,'drafts.create');
  const mime=Buffer.from(contacts[0].args.body.message.raw,'base64url').toString();assert.match(mime,/To: alice@example.com/);assert.match(mime,/Content-Transfer-Encoding: 7bit/);assert.equal(mime.split('\n\n')[1],decline);
  assert.equal(readFileSync(stylePath,'utf8'),styleBefore,'read did not rewrite style');
 }finally{observer?.close();await app?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
