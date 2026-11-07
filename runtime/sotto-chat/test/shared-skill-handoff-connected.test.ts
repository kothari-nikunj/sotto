import {startApplication} from '../src/application.js';
import {ManagedProxyUpstreamUnavailable} from '../src/agent/models.js';
import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync, readFileSync, readdirSync} from 'node:fs';
import {once} from 'node:events';
import {DatabaseSync} from 'node:sqlite';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {readSkillIndex} from '../src/tools/skills.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import {createSkillCommand, SHARED_SKILL_EXECUTION_GUIDE} from '../src/tools/skill-command.js';
import {createOwnerSkillExecution} from '../src/tools/owner-skill-execution.js';
import {createOwnerSkillRequestClient, ownerSkillRequestKind} from '../src/tools/owner-skill-request-client.js';
async function startReceiver(folder: string, proactive: boolean | 'company' = false, company = 'Cedar Labs') {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_COMPANY_NAME: company, SOTTO_TEST_RELATIONSHIP_PULSE: proactive ? undefined : '1', SOTTO_TEST_PROACTIVE: proactive === true ? '1' : undefined, SOTTO_TEST_ASK_COMPANY: proactive === 'company' ? '1' : undefined, SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
      SOTTO_DATA: folder, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'tenant', SOTTO_ACCOUNT_ID: 'account',
      SOTTO_LINE_KEY: 'line', SOTTO_MCP_TOKEN: '0123456789abcdef0123456789abcdef',
      PHOTON_HOME_CHANNEL: '+15555550123', PHOTON_ALLOWED_USERS: '+15555550123', PHOTON_PROJECT_ID: 'fixture-project',
      PHOTON_PROJECT_SECRET: 'fixture-secret', PHOTON_ALLOW_ALL_USERS: 'false', SOTTO_IMESSAGE_NUMBER: '+15555550999',
      SOTTO_TIMEZONE: 'America/Los_Angeles'}});
  let stderr = ''; child.stderr.on('data', chunk => {stderr = (stderr + String(chunk)).slice(-4096);});
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const line = await Promise.race([new Promise<string>((done, fail) => {
      let output = ''; child.stdout.on('data', chunk => {output += String(chunk); const i = output.indexOf('\n'); if (i >= 0) done(output.slice(0, i));});
      child.once('exit', code => fail(new Error(`receiver exited ${code}: ${stderr}`))); child.once('error', fail);
    }), new Promise<never>((_, fail) => {timer = setTimeout(() => fail(new Error('receiver start timeout')), 15000);})]);
    const parsed = JSON.parse(line) as {port: number; identity: OwnerToolIdentity; bearer: string; readBearer:string};
    return {child, ...parsed, bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp`};
  } catch (error) {child.kill('SIGKILL'); throw error;}
  finally {if (timer) clearTimeout(timer);}
}
async function stopReceiver(child: ChildProcessWithoutNullStreams) {
  if (child.exitCode !== null) return;
  const ended = once(child, 'exit'); child.stdin.end('quit\n');
  const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
  try {await ended;} finally {clearTimeout(timer);}
}




const usage = {inputTokens: {total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0}, outputTokens: {total: 2, text: 2, reasoning: 0}};

import {startConfiguredRuntime} from '../src/bootstrap/main.js';
import {Message} from 'chat';
import {writeFileSync, mkdirSync} from 'node:fs';
test('actual bootstrap catalog and native queued receipt stop before another model step, then fixed ACK', async () => {
 const folder=mkdtempSync(join(tmpdir(),'sotto-queued-handoff-'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 let app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined;
 let agent:ReturnType<typeof createSottoAgentRuntime>|undefined;
 try {
  receiver=await startReceiver(join(folder,'receiver'));
  const thread=receiver.identity.route_id;
  const media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const config={version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-handoff-at-least-32-character-token',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use the loaded original skill and enabled command.',skillPackRoot:resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver.bridgeUrl,readBearer:(receiver as unknown as {readBearer:string}).readBearer},ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}};
  const configPath=join(folder,'runtime.json');writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
  const sent:string[]=[];let modelCalls=0;let done!:()=>void;
  const generated=new Promise<void>(resolve=>{done=resolve;});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async()=>null,addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  app=await startConfiguredRuntime(configPath,{
   createAdapter:()=>adapter as never,
   createTransport:()=>({send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['handoff-ack']};}}),
   openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134,
   createAgent:(assembled,_model,_media,paths,bridgeTools,authority)=>{
    const model=new MockLanguageModelV4({doGenerate:async request=>{
     modelCalls++;assert.ok(modelCalls<=2,'queued receipt must prevent the third model request');
     const call=modelCalls===1?{name:'skill',args:{name:'sotto-relationship-pulse'}}:{name:'run_sotto_script',args:{id:'relationship-pulse',args:[]}};
     if(modelCalls===2){assert.ok(JSON.stringify(request.prompt).includes('run_sotto_script relationship-pulse'));const tools=request.tools as {name:string;description?:string}[];assert.ok(tools.find(tool=>tool.name==='run_sotto_script')?.description?.includes('relationship-pulse: []'));}
     return {content:[{type:'tool-call' as const,toolCallId:'handoff-'+modelCalls,toolName:call.name,input:JSON.stringify(call.args)}],finishReason:{unified:'tool-calls' as const,raw:'STOP'},usage,warnings:[]};
    }});
    agent=createSottoAgentRuntime({model,storagePath:join(folder,'native.db'),instructions:assembled.instructions,skillPaths:paths,sourceAuthority:authority,chatTools:context=>bridgeTools!.chat(context.sourceRevision)});
    return {...agent,generateChat:async request=>{try{return await agent!.generateChat(request);}finally{done();}}};
   },
  });
  const owner=receiver.identity.owner_id;
  const message=new Message({id:'handoff-owner',threadId:thread,text:'Give me a relationship pulse.',formatted:{type:'root',children:[]},raw:{id:'handoff-owner',timestamp:new Date()},author:{userId:owner,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  await app.gateway.chat.processMessage(adapter as never,thread,message);
  await generated;
  for(let i=0;i<50&&sent.length===0;i++)await new Promise(resolve=>setImmediate(resolve));
  assert.equal(modelCalls,2);assert.deepEqual(sent,['Your relationship pulse is queued. It will arrive separately.']);
 } finally {await app?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});


test('default bootstrap admits original urgent check silently and recovery never admits it twice', async () => {
 const originalFetch=globalThis.fetch;
 const folder=mkdtempSync(join(tmpdir(),'sotto-urgent-handoff-'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined, app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined, observer:DatabaseSync|undefined;
 let phase='urgent',calls=0,admissions=0,lose=true,hide=true;const sent:string[]=[];const feedbackEffects:{id:string;effect:string}[]=[];
 const waitFor=async(predicate:()=>boolean)=>{const until=Date.now()+15000;while(!predicate()){if(Date.now()>until)throw Error('urgent admission did not settle '+JSON.stringify({calls,admissions,actions:observer?.prepare('SELECT status FROM actions').all()}));await new Promise(resolve=>setTimeout(resolve,10));}};
 try {
  receiver=await startReceiver(join(folder,'receiver'),true);const thread=receiver.identity.route_id;
  const media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const config={version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-urgent-handoff-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use the original skills and enabled commands.',skillPackRoot:resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver.bridgeUrl,readBearer:receiver.readBearer},ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}};
  const configPath=join(folder,'runtime.json');writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async(_thread:string,id:string)=>({id,threadId:thread,author:{isMe:false},raw:{read:async()=>{feedbackEffects.push({id,effect:'read'});},react:async(emoji:string)=>{feedbackEffects.push({id,effect:emoji});}}}),addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  const external:typeof fetch=async(input,init)=>{
   const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
   if(url.origin!=='https://proxy.example'){
    assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin);
    const body=JSON.parse(String(init?.body??'{}'));
    if(url.pathname==='/runtime/owner-skill-request'&&body.operation==='inspect'&&hide)return new Response('{}',{status:503});
    const response=await originalFetch(input,init);
    if(url.pathname==='/runtime/owner-skill-request'&&body.operation==='admit'){
     admissions++;assert.deepEqual(body.args,{skill:'proactive',selection:{mode:'current'}});
     assert.equal((await response.clone().json()).status,'committed');
     if(lose)throw Error('synthetic lost committed response');
    }
    return response;
   }
   calls++;const body=JSON.parse(String(init?.body));let call:{name:string;args:unknown}|undefined;
   if(phase==='urgent'){
    assert.ok(calls<=3);call=calls===1?{name:'skill',args:{name:'sotto-proactive'}}:{name:'run_sotto_script',args:{id:'proactive',args:[]}};
    if(calls===3)call=undefined;
    if(calls===2){assert.ok(JSON.stringify(body.messages).includes('run_sotto_script proactive'));assert.ok(body.tools.find((tool:{function:{name:string;description:string}})=>tool.function.name==='run_sotto_script').function.description.includes('proactive: []'));}
   }else if(phase==='success'){call={name:'run_sotto_script',args:{id:'proactive',args:[]}};}
   return new Response(JSON.stringify({id:'synthetic-'+calls,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'urgent-'+calls,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:'Hello.'},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
  };
  const mount=()=>startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['accepted-'+sent.length]};}}),openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
  const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  globalThis.fetch=external;app=await mount();observer=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
  const urgent=message('owner-urgent','Check for anything urgent.');await app.gateway.chat.processMessage(adapter as never,thread,urgent);
  await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE kind='owner_skill_request' AND status='unknown'").get()));
  await waitFor(()=>calls===3);await waitFor(()=>sent.length===1);const notice=sent[0]!;assert.match(notice,/^I couldn’t confirm whether the skill request/);assert.equal(admissions,1);
  assert.equal(feedbackEffects.filter(value=>value.id==='owner-urgent'&&value.effect==='✅').length,0,'an uncertain skill request must not receive completion feedback');
  const held=observer.prepare("SELECT a.status,e.outcome FROM actions a JOIN inbound_events e ON e.id=a.source_event_id WHERE a.kind='owner_skill_request'").get()!;assert.equal(held.status,'unknown');assert.equal(held.outcome,'pending','reply feedback does not confirm the held work');
  phase='safe';await app.gateway.chat.processMessage(adapter as never,thread,message('owner-safe','Hello'));await waitFor(()=>sent.length===2);assert.deepEqual(sent,[notice,'Hello.']);
  await app.close();app=undefined;
  // Make the persisted inspection cadence due before reopening for the late exact receipt.
  const due=new DatabaseSync(join(folder,'state/operations.sqlite'));try{due.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_skill_request' AND status='unknown'").run();}finally{due.close();}
  hide=false;lose=false;app=await mount();
  await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE kind='owner_skill_request' AND status='succeeded'").get()));
  const action=observer.prepare("SELECT result_json FROM actions WHERE kind='owner_skill_request'").get()!;assert.ok(String(action.result_json).includes('proactive'));
  assert.deepEqual(sent,[notice,'Hello.']);assert.equal(admissions,1);assert.equal(feedbackEffects.filter(value=>value.id==='owner-urgent'&&value.effect==='✅').length,0,'receipt recovery does not manufacture fresh completion feedback');
  const execute=async()=>{const response=new Promise<{executed:boolean;jobs:{status:string}|null}>((done,fail)=>{let text='';const listener=(chunk:Buffer)=>{text+=String(chunk);const end=text.indexOf('\n');if(end>=0){receiver!.child.stdout.off('data',listener);clearTimeout(timer);try{done(JSON.parse(text.slice(0,end)));}catch(error){fail(error);}}};const timer=setTimeout(()=>{receiver!.child.stdout.off('data',listener);fail(Error('original urgent worker timeout'));},15000);receiver!.child.stdout.on('data',listener);});receiver!.child.stdin.write('execute-proactive\n');return response;};
  const executed=await execute();assert.equal(executed.executed,true);assert.equal(executed.jobs?.status,'done');
  const outputs=readFileSync(join(folder,'receiver/synthetic-sends.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));assert.equal(outputs.length,1);assert.match(outputs[0].text,/Review the Cedar deck/);assert.equal((await execute()).executed,false);assert.equal(readFileSync(join(folder,'receiver/synthetic-sends.jsonl'),'utf8').trim().split('\n').length,1);
  const before=calls;await app.gateway.chat.processMessage(adapter as never,thread,urgent);await new Promise(resolve=>setTimeout(resolve,30));assert.equal(calls,before);assert.equal(admissions,1);assert.deepEqual(sent,[notice,'Hello.']);
  phase='success';const previousCalls=calls;await app.gateway.chat.processMessage(adapter as never,thread,message('owner-urgent-success','Check for anything urgent.'));await waitFor(()=>Number(observer!.prepare("SELECT count(*) n FROM actions WHERE kind='owner_skill_request' AND status='succeeded'").get()!.n)===2);await new Promise(resolve=>setTimeout(resolve,30));assert.equal(calls-previousCalls,1,'validated queued receipt stops before another model contact');assert.deepEqual(sent,[notice,'Hello.']);assert.equal(admissions,2);await waitFor(()=>feedbackEffects.some(value=>value.id==='owner-urgent-success'&&value.effect==='✅'));assert.equal(feedbackEffects.filter(value=>value.id==='owner-urgent-success'&&value.effect==='✅').length,1);assert.equal(feedbackEffects.filter(value=>value.id==='owner-urgent'&&value.effect==='✅').length,0,'the original uncertain request never receives completion feedback');
 }finally{globalThis.fetch=originalFetch;observer?.close();await app?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});

test('default bootstrap Ask company queues original research, persists company only, and reads saved context after recovery', async () => {
 const originalFetch=globalThis.fetch;
 const folder=mkdtempSync(join(tmpdir(),'sotto-company-handoff-'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined, app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined, observer:DatabaseSync|undefined;
 let phase='urgent',calls=0,lookupStart=0,admissions=0,lostCommitted=false,lose=true,hide=true;let sourceRevision:string|undefined;const sent:string[]=[];const inspections:unknown[]=[];
 const waitFor=async(predicate:()=>boolean)=>{const until=Date.now()+15000;while(!predicate()){if(Date.now()>until)throw Error('company research did not settle '+JSON.stringify({calls,admissions,actions:observer?.prepare('SELECT status FROM actions').all(),inspections}));await new Promise(resolve=>setTimeout(resolve,10));}};
 try {
  receiver=await startReceiver(join(folder,'receiver'),'company');const thread=receiver.identity.route_id;
  const media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const config={version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-urgent-handoff-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use the original skills and enabled commands.',skillPackRoot:process.env.SOTTO_TEST_SKILL_PACK ?? resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver.bridgeUrl,readBearer:receiver.readBearer},ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}};
  const configPath=join(folder,'runtime.json');writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async(_thread:string,id:string)=>({id,threadId:thread,author:{isMe:false},raw:{read:async()=>{},react:async()=>{}}}),addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  const external:typeof fetch=async(input,init)=>{
   const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
   if(url.origin!=='https://proxy.example'){
    assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin);
    const body=JSON.parse(String(init?.body??'{}'));
    if(url.pathname==='/runtime/owner-skill-request'&&body.operation==='inspect'&&hide)return new Response('{}',{status:503});
    const response=await originalFetch(input,init);if(url.pathname==='/runtime/source-authority'){const value=await response.clone().json();assert.equal(value.complete,true);sourceRevision=value.revision;}if(url.pathname==='/runtime/owner-skill-request'&&body.operation==='inspect'&&!hide){const value=await response.clone().json();inspections.push(value);assert.equal(value.status,'committed',JSON.stringify(value));}
    if(url.pathname==='/runtime/owner-skill-request'&&body.operation==='admit'){
     admissions++;assert.match(sourceRevision??'',/^[a-f0-9]{64}$/);assert.deepEqual(body.args,{skill:'ask-research',selection:{mode:'company',company:'Cedar Labs'},source_revision:sourceRevision});
     const admitted=await response.clone().json();inspections.push({admitted});assert.equal(admitted.status,'committed');
     if(lose){lostCommitted=true;throw Error('synthetic lost committed response');}
    }
    return response;
   }
   calls++;const body=JSON.parse(String(init?.body));let call:{name:string;args:unknown}|undefined;
   if(phase==='urgent'){
    assert.ok(calls<=3);call=calls===1?{name:'skill',args:{name:'sotto-ask'}}:{name:'run_sotto_script',args:{id:'ask-research',args:['--company','Cedar Labs']}};
    if(calls===3)call=undefined;
    if(calls===2){assert.ok(JSON.stringify(body.messages).includes('run_sotto_script ask-research --person'));assert.ok(body.tools.find((tool:{function:{name:string;description:string}})=>tool.function.name==='run_sotto_script').function.description.includes('ask-research:'));}
   }else if(phase==='lookup'){call=calls===lookupStart+1?{name:'run_sotto_script',args:{id:'knowledge-query',args:['--company','Cedar Labs']}}:undefined;if(!call){const history=JSON.stringify(body.messages);assert.ok(history.includes('Builds clinic tools.'));assert.ok(history.includes('https://cedar.example/pilot'));}}
   return new Response(JSON.stringify({id:'synthetic-'+calls,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'urgent-'+calls,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:'Hello.'},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
  };
  const mount=()=>startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['accepted-'+sent.length]};}}),openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
  const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  globalThis.fetch=external;app=await mount();observer=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
  const urgent=message('owner-urgent','Research Cedar Labs for me.');await app.gateway.chat.processMessage(adapter as never,thread,urgent);
  await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE kind='owner_skill_request' AND status='unknown'").get()));
  await waitFor(()=>calls===3);assert.equal(lostCommitted,true,JSON.stringify(inspections));await waitFor(()=>sent.length===1);const notice=sent[0]!;assert.match(notice,/^I couldn’t confirm whether the skill request/);assert.equal(admissions,1);
  phase='safe';await app.gateway.chat.processMessage(adapter as never,thread,message('owner-safe','Hello'));await waitFor(()=>sent.length===2);assert.deepEqual(sent,[notice,'Hello.']);
  await app.close();app=undefined;
  // Make the persisted inspection cadence due before reopening for the late exact receipt.
  const due=new DatabaseSync(join(folder,'state/operations.sqlite'));try{due.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_skill_request' AND status='unknown'").run();}finally{due.close();}
  hide=false;lose=false;app=await mount();
  await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE kind='owner_skill_request' AND status='succeeded'").get()));
  const action=observer.prepare("SELECT result_json FROM actions WHERE kind='owner_skill_request'").get()!;assert.ok(String(action.result_json).includes('company'));
  await waitFor(()=>sent.length===3);assert.deepEqual(sent,[notice,'Hello.','Your research is queued. It will arrive separately.']);assert.equal(admissions,1);
  const execute=async()=>{const response=new Promise<{executed:boolean;jobs:{status:string;error?:string}|null}>((done,fail)=>{let text='';const listener=(chunk:Buffer)=>{text+=String(chunk);const end=text.indexOf('\n');if(end>=0){receiver!.child.stdout.off('data',listener);clearTimeout(timer);try{done(JSON.parse(text.slice(0,end)));}catch(error){fail(error);}}};const timer=setTimeout(()=>{receiver!.child.stdout.off('data',listener);fail(Error('original company worker timeout'));},15000);receiver!.child.stdout.on('data',listener);});receiver!.child.stdin.write('execute-ask-research\n');return response;};
  const executed=await execute();assert.equal(executed.executed,true);assert.equal(executed.jobs?.status,'done',JSON.stringify(executed));
  const outputs=readFileSync(join(folder,'receiver/synthetic-sends.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));assert.equal(outputs.length,1);assert.match(outputs[0].text,/Cedar Labs builds clinic tools/);assert.equal((await execute()).executed,false);assert.equal(readFileSync(join(folder,'receiver/synthetic-sends.jsonl'),'utf8').trim().split('\n').length,1);
  const count=(name:string)=>readFileSync(join(folder,'receiver',name),'utf8').trim().split('\n').length;
  assert.equal(count('synthetic-company-calls.jsonl'),1);assert.equal(count('synthetic-answer-calls.jsonl'),1);
  assert.equal((existsSync(join(folder,'receiver/knowledge/people'))?readdirSync(join(folder,'receiver/knowledge/people')):[]).filter(name=>name.endsWith('.md')).length,0);
  assert.equal(readdirSync(join(folder,'receiver/knowledge/companies')).filter(name=>name.endsWith('.md')).length,1);
  await app.close();app=undefined;app=await mount();
  phase='lookup';lookupStart=calls;await app.gateway.chat.processMessage(adapter as never,thread,message('owner-company-lookup','What is saved about Cedar Labs?'));await waitFor(()=>sent.length===4);
  assert.equal(admissions,1);assert.equal(count('synthetic-company-calls.jsonl'),1);assert.equal(count('synthetic-answer-calls.jsonl'),1);
  const before=calls;await app.gateway.chat.processMessage(adapter as never,thread,urgent);await new Promise(resolve=>setTimeout(resolve,30));assert.equal(calls,before);assert.equal(admissions,1);
 }finally{globalThis.fetch=originalFetch;observer?.close();await app?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});

for (const [firstText, edit, failedEdit] of [
 ["quick 3 sentence blurb on Tinfoil for IC?", true, false],
 ["couple lines on Tinfoil", true, false],
 ["short Tinfoil blurb for IC", true, false],
 ['Tinfoil for IC, 3 sentences?', true, true],
 ['Tinfoil for IC, 3 sentences?', true, false],
 ['Tinfoil, quick blurb for our IC', false, false],
 ['tell me more about Tinfoil. quick blurb for IC', false, false],
 ['Can you write me a blurb for Tinfoil for our IC tomorrow? What they do etc. short 3-4 sentences', false, false],
 ['Tinfoil. give me a short blurb for IC', false, false],
 ['Can you write me a blurb for Tinfoil for our IC tomorrow? What they do etc. short 3-4 sentences', true, false],
] as const)
test(`inline company writing request reaches homepage retrieval: ${firstText}, edit=${edit}, failed=${failedEdit}`, async () => {
 const natural=firstText==='quick 3 sentence blurb on Tinfoil for IC?';
 const editText=natural?'can you make that shorter?':'shorter pls';
 const websiteText=natural?'check their website - anything else worth knowing?':'their site too?';
 const folder=mkdtempSync(join(tmpdir(),'sotto-inline-company-connected-'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 let coordinator:ConversationCoordinator|undefined;
 let journal:OperationalJournal|undefined;
 try {
  receiver=await startReceiver(join(folder,'receiver'),'company','Tinfoil');
  const companies=join(folder,'receiver/knowledge/companies');mkdirSync(companies,{recursive:true});
  writeFileSync(join(companies,'tinfoil.md'),'---\nnormalized: tinfoil\naliases: [Tinfoil]\ndomain: cedar.example\n---\n## About\nKnown clinic tools.\n');
  journal=new OperationalJournal(join(folder,'operations.sqlite'));
  const responses:unknown[]=[];
  const client=createOwnerSkillRequestClient({identity:receiver.identity,bridgeUrl:receiver.bridgeUrl,bearer:receiver.bearer,fetch:async(input,init)=>{
   const response=await fetch(input,init); responses.push({status:response.status,body:await response.clone().json()});return response;
  }});
  const revision='a'.repeat(64);
  const execution=createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision,bridge_sources:[],google_sources:[]})}});
  const command=createSkillCommand(execution);
  const sent:string[]=[];let editAttempts=0;
  coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},ownerSkillRequest:client,
   model:{generate:async request=>{
    if(failedEdit&&request.text===editText&&editAttempts++===0)throw new ManagedProxyUpstreamUnavailable(503);
    if(request.text===websiteText) {
     const result=await command({id:'ask-research',args:['--company','Tinfoil','--website']},{mode:'chat',runId:request.runId,
      ownerRequestKey:request.ownerRequestKey,allowOwnerSkill:request.allowOwnerSkill,sourceRevision:revision,signal:request.signal});
     assert.equal(result.result.status,'queued',JSON.stringify(result));
     return {text:'Queued',sourceRevision:revision};
    }
    return {text:'Tinfoil builds clinic tools.',sourceRevision:revision};
   }},postPrepared:async reply=>{sent.push(reply.text);return {acceptance:'accepted',messageId:'accepted-'+sent.length};}});
  const inbound=(eventId:string,text:string)=>({eventId,text,channel:'photon' as const,accountId:'account',senderId:receiver!.identity.owner_id,isDirect:true});
  const context={threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'};
  await coordinator.receive(inbound('inline-blurb',firstText),context);
  assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
  if(failedEdit){await coordinator.receive(inbound('failed-edit',editText),context);assert.match(sent.pop()!,/temporarily unavailable/);}
  if(edit)await coordinator.receive(inbound('inline-edit',editText),context);
  await coordinator.receive(inbound('inline-website',websiteText),context);
  assert.deepEqual(sent,[...(edit?['Tinfoil builds clinic tools.']:[]),'Tinfoil builds clinic tools.','Your research is queued. It will arrive separately.'],JSON.stringify(responses));
  const action=journal.db.prepare("SELECT id,status,result_json FROM actions WHERE kind='owner_skill_request'").get()!;
  assert.equal(action.status,'succeeded',String(action.result_json));
  const saved=JSON.parse(String(action.result_json));
  const request=journal.readOwnerSkillRequestAction(action.id as string,{
   channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id,lineKey:'line',scopeHash:client.scopeHash,inspectClosedSource:true}).request;
  const receipt=await client.call('inspect',request);
  assert.equal(receipt.status,'committed');
  assert.equal(saved.args.prior_selection.text,firstText);
  assert.equal((await coordinator.receive(inbound('inline-website',websiteText),context)).status,'duplicate');
  assert.equal(sent.length,edit?3:2);
  const queue=new DatabaseSync(join(folder,'receiver/events/work.sqlite3'),{readOnly:true});
  try {
   const jobs=queue.prepare('SELECT payload,admission FROM jobs').all();assert.equal(jobs.length,1);
   const procedure=JSON.parse(JSON.parse(String(jobs[0]!.payload)).prompt);
   assert.equal(procedure.question,websiteText+'\nSubject from accepted conversation: Tinfoil');
   assert.deepEqual(procedure.selection,{mode:'company',company:'Tinfoil',operation:'website'});
   assert.equal(JSON.parse(String(jobs[0]!.admission)).source_revision,revision);
  }finally{queue.close();}
  const execute=async()=>{
   const result=new Promise<{executed:boolean;jobs:{status:string;error?:string}|null}>((done,fail)=>{
    let text='';const listener=(chunk:Buffer)=>{text+=String(chunk);const end=text.indexOf('\n');if(end>=0){receiver!.child.stdout.off('data',listener);clearTimeout(timer);try{done(JSON.parse(text.slice(0,end)));}catch(error){fail(error);}}};
    const timer=setTimeout(()=>{receiver!.child.stdout.off('data',listener);fail(Error('company continuation worker timeout'));},15000);
    receiver!.child.stdout.on('data',listener);
   });
   receiver!.child.stdin.write('execute-ask-research\n');return result;
  };
  const completed=await execute();assert.equal(completed.executed,true);assert.equal(completed.jobs?.status,'done',JSON.stringify(completed));
  const deliveries=readFileSync(join(folder,'receiver/synthetic-sends.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));
  assert.equal(deliveries.length,1);assert.match(deliveries[0].text,/Tinfoil builds clinic tools/);
  assert.equal((await execute()).executed,false);
  assert.equal(existsSync(join(folder,'receiver/synthetic-company-calls.jsonl')),false);
  assert.equal(readFileSync(join(folder,'receiver/synthetic-website-calls.jsonl'),'utf8').trim().split('\n').length,1);
  assert.equal(readFileSync(join(folder,'receiver/synthetic-answer-calls.jsonl'),'utf8').trim().split('\n').length,1);
  assert.equal(readFileSync(join(folder,'receiver/synthetic-sends.jsonl'),'utf8').trim().split('\n').length,1);
 }finally{await coordinator?.close();journal?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});


test('accepted descriptive company answer survives native memory and binds exact website worker once',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'sotto-recall-company-'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
 let app:Awaited<ReturnType<typeof startApplication>>|undefined;
 let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
 let journal:OperationalJournal|undefined;
 try {
  receiver=await startReceiver(join(folder,'receiver'),'company','SixSense');
  const companies=join(folder,'receiver/knowledge/companies');mkdirSync(companies,{recursive:true});
  writeFileSync(join(companies,'sixsense.md'),'---\nnormalized: sixsense\naliases: [SixSense]\ndomain: cedar.example\n---\n## About\nKnown clinic tools.\n');
  const sourceAuthority={read:async()=>({version:2 as const,complete:true,revision:'a'.repeat(64),bridge_sources:[],google_sources:[]})};
  const client=createOwnerSkillRequestClient({...receiver,identity:receiver.identity});
  const execution=createOwnerSkillExecution({client,journal:()=>journal,sourceAuthority});
  const script=createRunScriptTool({mode:'chat',executeCommand:createSkillCommand(execution),commandContext:(ctx,signal)=>({
   mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),allowOwnerSkill:ctx?.get('allowOwnerSkill'),sourceRevision:ctx?.get('sourceRevision'),signal})});
  let step=0;const firstText='what does SixSense do again? short version';
  const model=new MockLanguageModelV4({doGenerate:async request=>{
   const n=step++;
   if(n===1){const prompt=JSON.stringify(request.prompt);assert.ok(prompt.includes(firstText));assert.ok(prompt.includes('SixSense builds clinic tools.'));
    return {content:[{type:'tool-call' as const,toolCallId:'exact-company-site',toolName:'run_sotto_script',input:JSON.stringify({id:'ask-research',args:['--company','SixSense']})}],finishReason:{unified:'tool-calls' as const,raw:'STOP'},usage,warnings:[]};}
   return {content:[{type:'text' as const,text:n===0?'SixSense builds clinic tools.':'Queued'}],finishReason:{unified:'stop' as const,raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:SHARED_SKILL_EXECUTION_GUIDE,sourceAuthority,chatTools:()=>({run_sotto_script:script})});
  const sent:string[]=[];
  app=await startApplication({directory:join(folder,'app'),mediaRoot:folder,bearerToken:'fixture-only-bearer-for-local-tests-000',
   owner:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:receiver.identity.route_id,deferListen:true},
   {agent:runtime,onJournalReady:value=>{journal=value;},ownerSkillRequest:client,
    transport:{send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['recall-'+sent.length]};}}});
  const inbound=(eventId:string,text:string)=>({eventId,text,channel:'photon' as const,accountId:'account',senderId:receiver!.identity.owner_id,isDirect:true});
  await app.receive(inbound('recall',firstText));
  assert.equal(journal!.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
  await app.receive(inbound('website','check their website too'));
  assert.deepEqual(sent,['SixSense builds clinic tools.','Your research is queued. It will arrive separately.']);
  const saved=journal!.db.prepare("SELECT status,result_json FROM actions WHERE kind='owner_skill_request'").get()!;
  assert.equal(saved.status,'succeeded');assert.equal(JSON.parse(String(saved.result_json)).args.prior_selection.text,firstText);
  assert.equal((await app.receive(inbound('website','check their website too'))).status,'duplicate');
  const execute=async()=>{
   const result=new Promise<{executed:boolean;jobs:{status:string;error?:string}|null}>((done,fail)=>{
    let text='';const listener=(chunk:Buffer)=>{text+=String(chunk);const end=text.indexOf('\n');if(end>=0){receiver!.child.stdout.off('data',listener);clearTimeout(timer);try{done(JSON.parse(text.slice(0,end)));}catch(error){fail(error);}}};
    const timer=setTimeout(()=>{receiver!.child.stdout.off('data',listener);fail(Error('company worker timeout'));},15000);
    receiver!.child.stdout.on('data',listener);
   });receiver!.child.stdin.write('execute-ask-research\n');return result;
  };
  const completed=await execute();assert.equal(completed.executed,true);assert.equal(completed.jobs?.status,'done',JSON.stringify(completed));
  const lines=(name:string)=>readFileSync(join(folder,'receiver',name),'utf8').trim().split('\n');
  assert.equal(lines('synthetic-website-calls.jsonl').length,1);assert.equal(lines('synthetic-answer-calls.jsonl').length,1);
  assert.equal(lines('synthetic-sends.jsonl').length,1);assert.match(JSON.parse(lines('synthetic-sends.jsonl')[0]!).text,/SixSense builds clinic tools/);
  assert.equal((await execute()).executed,false);assert.equal(lines('synthetic-sends.jsonl').length,1);
 }finally{await app?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
