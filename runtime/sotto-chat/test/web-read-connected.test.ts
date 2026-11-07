import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn,type ChildProcessWithoutNullStreams} from 'node:child_process';
import {existsSync,readFileSync,writeFileSync,mkdirSync,mkdtempSync,rmSync} from 'node:fs';
import {once} from 'node:events';
import {DatabaseSync} from 'node:sqlite';
import {Message} from 'chat';
import {startConfiguredRuntime} from '../src/bootstrap/main.js';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {DELAYED_OUTCOME_POLICY} from '../src/delayed-outcome-policy.js';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {readSkillIndex} from '../src/tools/skills.js';
import {createRunScriptTool} from '../src/tools/run-script-tool.js';
import {createSourceAuthorityReader} from '../src/agent/source-authority.js';
import {docsendReviewText,createOwnerWebReadClient,createWebReadCommand,WEB_READ_EXECUTION_GUIDE,ownerWebReadKind} from '../src/tools/owner-web-read.js';
async function startReceiver(folder: string, loops = false, granola = false, history = false, docsend = false, reply = false) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_KNOWLEDGE_READ: undefined,SOTTO_TEST_WEB_REPLY:reply?'1':'0', SOTTO_TEST_WEB_READ:docsend?'0':'1',SOTTO_TEST_DOCSEND:docsend?'1':'0', SOTTO_TEST_LOOPS_READ: loops ? '1' : undefined, SOTTO_TEST_GRANOLA_READ:granola?'1':undefined, SOTTO_TEST_GRANOLA_HISTORY:history?'1':undefined, SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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
    const parsed = JSON.parse(line) as {port: number; identity: OwnerToolIdentity; bearer: string; readBearer: string;dashboardToken?:string};
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




const usage={inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
for(const scenario of [{loss:false,reply:false},{loss:true,reply:false},{loss:true,reply:true}])test(`native original Ask ordinary URL provider fallback and same attending answer loss=${scenario.loss} reply=${scenario.reply}`,async()=>{
 const {loss,reply}=scenario;let phase=reply?'seed':'read';
 const folder=mkdtempSync(join(tmpdir(),'web-read-native-'));let journal=new OperationalJournal(join(folder,'journal.sqlite'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,runtime:ReturnType<typeof createSottoAgentRuntime>|undefined,coordinator:ConversationCoordinator|undefined;
 try {
  receiver=await startReceiver(join(folder,'receiver'),false,false,false,false,reply);
  const authority=createSourceAuthorityReader({bridgeUrl:receiver.bridgeUrl,bearer:receiver.readBearer});
  assert.equal((await authority.read()).revision,reply?'a'.repeat(64):null);
  let blocked=loss,executions=0,inspections=0;
  const client=createOwnerWebReadClient({...receiver,fetch:async(input,init)=>{
    const body=JSON.parse(String(init!.body));
    if(body.operation==='inspect'){inspections++;if(blocked)throw new Error('synthetic inspection outage');}
    else executions++;
    const response=await fetch(input,init);assert.equal(response.status,200);
    if(loss&&body.operation==='execute')throw new Error('synthetic response loss after real receipt');
    return response;
  }});
  const command=createWebReadCommand({client,journal:()=>journal,sourceAuthority:authority});
  const tool=createRunScriptTool({mode:'chat',executeCommand:command,commandContext:(ctx,signal)=>({mode:ctx?.get('mode'),runId:ctx?.get('runId'),ownerRequestKey:ctx?.get('ownerRequestKey'),sourceRevision:ctx?.get('sourceRevision'),allowOwnerSkill:ctx?.get('allowOwnerSkill'),signal})});
  let step=0;
  const model=new MockLanguageModelV4({doGenerate:async request=>{
   if(phase==='seed')return {content:[{type:'text' as const,text:'Read https://example.com/cedar'}],finishReason:{unified:'stop' as const,raw:'STOP'},usage,warnings:[]};
   const prompt=JSON.stringify(request.prompt);
   if(step===1)assert.ok(prompt.includes('Links the user asks about'));
   if(step>=2&&(!loss||!blocked))assert.ok(prompt.includes('pediatric clinic pilot opens in November'));
   const call=step===0?{name:'skill',args:{name:'sotto-ask'}}:step===1?{name:'run_sotto_script',args:{id:'web-read',args:['--url','https://example.com/cedar']}}:null;
   step++;return {content:call?[{type:'tool-call' as const,toolCallId:`web-${step}`,toolName:call.name,input:JSON.stringify(call.args)}]:[{type:'text' as const,text:blocked?'I cannot yet confirm the page.':'The Cedar page says the pediatric clinic pilot opens in November.'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
  }});
  runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:WEB_READ_EXECUTION_GUIDE,
   skillPaths:(await readSkillIndex(process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority:authority,chatTools:()=>({run_sotto_script:tool})});
  const sent:string[]=[];
  const makeCoordinator=()=>new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver!.identity.owner_id},ownerWebRead:client,
   model:{generate:request=>runtime!.generateChat({...request,threadId:'web-memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`reply-${sent.length}`};}});
  coordinator=makeCoordinator();
  const event={channel:'photon' as const,accountId:'account',senderId:receiver.identity.owner_id,isDirect:true,eventId:'web-request',text:reply?'Read this link please':'Read https://example.com/cedar',...(reply?{replyBound:true,replyToMessageId:'reply-1'}:{})};
  const context={threadId:receiver.identity.route_id,destination:'photon',lineKey:'line'};
  if(reply){assert.equal((await coordinator.receive({...event,eventId:'link-seed',text:'Give me a link',replyBound:false,replyToMessageId:undefined},context)).status,'accepted');assert.equal(sent[0],'Read https://example.com/cedar');phase='read';}
  assert.equal((await coordinator.receive(event,context)).status,loss?'held':'accepted');
  if(loss){
   assert.match(sent.at(-1)!,/^I couldn’t confirm whether the URL read happened\./);
   await coordinator.close();journal.close();await runtime.close();await stopReceiver(receiver.child);
   journal=new OperationalJournal(join(folder,'journal.sqlite'));journal.recoverInterrupted();receiver=await startReceiver(join(folder,'receiver'),false,false,false,false,reply);
   // New receiver port must be used without resubmitting paid work.
   const recoveredClient=createOwnerWebReadClient({...receiver,fetch:async(input,init)=>{const body=JSON.parse(String(init!.body));assert.equal(body.operation,'inspect');inspections++;return fetch(input,init);}});
   const nextAuthority=createSourceAuthorityReader({bridgeUrl:receiver.bridgeUrl,bearer:receiver.readBearer});
   blocked=false;
   journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_web_read' AND status='unknown'").run();
   runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:WEB_READ_EXECUTION_GUIDE,skillPaths:(await readSkillIndex(process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority:nextAuthority});
   coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:receiver.identity.owner_id},ownerWebRead:recoveredClient,model:{generate:request=>runtime!.generateChat({...request,threadId:'web-memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:'recovered-answer'};}});
   assert.equal((await coordinator.recover())[0]?.status,'accepted');assert.ok(inspections>=1);
  }
  assert.equal(executions,1);assert.equal(sent.length,(reply?2:1)+(loss?1:0));assert.match(sent.at(-1)!,/pediatric clinic pilot opens in November/);
  const calls=readFileSync(join(folder,'receiver/web-provider-calls.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));
  assert.equal(calls.length,2);assert.equal(calls[0].url,'https://api.exa.ai/contents');assert.ok(calls[1].body.tools[0].url_context);
  assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions WHERE kind=?').get(ownerWebReadKind)!.n,1);
  const modelDb=new DatabaseSync(join(folder,'receiver/events/model-work.sqlite3'),{readOnly:true});try{assert.equal(modelDb.prepare('SELECT count(*) AS n FROM attempts').get()!.n,1);}finally{modelDb.close();}
  assert.equal((await coordinator.receive(event,context)).status,'duplicate');
 }finally{await coordinator?.close();await runtime?.close();if(receiver)await stopReceiver(receiver.child);journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('recovered URL evidence is revision-bound before attending model or paid media contact',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'web-evidence-revision-'));let calls=0;
 const model=new MockLanguageModelV4({doGenerate:async()=>{calls++;throw new Error('must not contact');}});
 const authority={read:async()=>({version:2 as const,complete:true,revision:'b'.repeat(64),bridge_sources:[],google_sources:[]})};
 const runtime=createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Use evidence only.',sourceAuthority:authority});
 try{await assert.rejects(runtime.generateChat({text:'Read this page',threadId:'thread',ownerId:'owner',runId:'run',recoveredWebReads:[{sourceRevision:null,result:{url:'https://example.com/cedar',title:'Old',text:'stale source',provider:'exa'}}]}));assert.equal(calls,0);}
 finally{await runtime.close();rmSync(folder,{recursive:true,force:true});}
});

test('configured original Ask DocSend uses configured gate, original vision/PDF/cache and accepted fresh consent without replay',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'docsend-connected-'));let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined,db:DatabaseSync|undefined;
 const deck='http://docsend.com/view/Cedar_1/?q=1#page';let phase='read',step=0,lose=false,inspectionUnavailable=false,lastReview='';const sent:string[]=[];
 const steps:Record<string,({name:string;args:unknown}|null)[]>={read:[{name:'skill',args:{name:'sotto-ask'}},{name:'run_sotto_script',args:{id:'docsend-fetch',args:['--url',deck]}},null],cache:[{name:'run_sotto_script',args:{id:'docsend-fetch',args:['--url',deck]}},null],fresh:[{name:'run_sotto_script',args:{id:'docsend-fetch',args:['--url',deck,'--fresh','--passcode','secret123']}},null],yes:[{name:'run_sotto_script',args:{id:'confirm-docsend',args:[]}},null],unknown:[{name:'run_sotto_script',args:{id:'docsend-fetch',args:['--url','https://docsend.com/view/Unknown']}},null],hello:[null]};
 const wait=async(p:()=>boolean)=>{const end=Date.now()+15000;while(!p()){if(Date.now()>end)throw Error('DocSend local turn timeout');await new Promise(r=>setTimeout(r,10));}};
 try{
  receiver=await startReceiver(join(folder,'receiver'),false,false,false,true);const thread=receiver.identity.route_id,media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const config={version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-docsend-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use original skills and enabled commands.',skillPackRoot:process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver.bridgeUrl,readBearer:receiver.readBearer},ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}};
  const configPath=join(folder,'runtime.json');writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async()=>null,addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  const external:typeof fetch=async(input,init)=>{
   const target=new URL(typeof input==='string'||input instanceof URL?input:input.url);
   if(target.origin!=='https://proxy.example'){
    assert.equal(target.origin,new URL(receiver!.bridgeUrl).origin);
    // Keep the synthetic receipt outage active until restart. Otherwise periodic
    // recovery can settle the action before the test observes its unknown state.
    if(target.pathname==='/runtime/owner-web-read'&&inspectionUnavailable&&JSON.parse(String(init?.body)).operation==='inspect')throw Error('synthetic receipt inspection unavailable');
    const response=await fetch(input,init);
    if(target.pathname==='/runtime/owner-web-read'){
      const body=JSON.parse(String(init?.body)),result=await response.clone().json();
      if(result.status==='preview')lastReview=docsendReviewText(result);
      if(lose&&body.operation==='execute'){lose=false;assert.equal(result.status,'committed');throw Error('synthetic lost committed receipt');}
    }
    return response;
   }
   const body=JSON.parse(String(init?.body));if(phase==='yes'&&step===0)assert.ok(JSON.stringify(body.messages).includes(JSON.stringify(lastReview).slice(1,-1)),'full accepted consent retained');
   const call=steps[phase]![step++];
   if(phase==='read'&&step===2)assert.ok(JSON.stringify(body.messages).includes('Links the user asks about'));
   if(['read','cache','yes'].includes(phase)&&!call)assert.ok(JSON.stringify(body.messages).includes('Cedar revenue is $10'));
   return new Response(JSON.stringify({id:'synthetic-'+phase+'-'+step,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'call-'+phase+'-'+step,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:phase==='fresh'?lastReview:phase==='hello'?'Hello.':'Cedar revenue is $10, from '+(phase==='cache'?'the cached deck.':'the deck.')},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
  };
  const mount=async()=>startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async r=>{sent.push(r.text??'');return {acceptance:'accepted',providerMessageIds:['accepted-'+sent.length]};}}),openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
  app=await mount();db=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
  const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  const turn=async(p:string,text:string,reply=true)=>{phase=p;step=0;await app!.gateway.chat.processMessage(adapter as never,thread,message('owner-'+p,text));await wait(()=>reply?Boolean(db!.prepare("SELECT p.dispatch_id FROM prepared_replies p JOIN dispatches d ON d.id=p.dispatch_id JOIN inbound_events e ON e.id=p.event_id WHERE e.source_id=? AND d.status='accepted'").get('owner-'+p)):Boolean(db!.prepare("SELECT id FROM actions WHERE kind='owner_web_read' AND status='unknown'").get()));};
  const provider=()=>existsSync(join(folder,'receiver/docsend-provider.jsonl'))?readFileSync(join(folder,'receiver/docsend-provider.jsonl'),'utf8').trim().split('\n').map(x=>JSON.parse(x)):[];
  await turn('read','Read this deck '+deck);assert.equal(provider().filter(x=>x.kind==='vision').length,1);assert.equal(provider().filter(x=>x.kind==='gate').length,1);
  const pdf=readFileSync(join(folder,'receiver/decks/Cedar_1.pdf'));assert.ok(pdf.subarray(0,5).equals(Buffer.from('%PDF-')));
  const origin=new URL(receiver.bridgeUrl).origin;assert.equal((await fetch(origin+'/api/decks/Cedar_1.pdf')).status,401);
  const download=await fetch(origin+'/api/decks/Cedar_1.pdf',{headers:{Cookie:'sotto_session='+receiver.dashboardToken}});assert.equal(download.status,200);assert.deepEqual(Buffer.from(await download.arrayBuffer()),pdf);
  const initialContacts=provider().length;await turn('cache','Read again '+deck);assert.equal(provider().length,initialContacts);assert.match(sent.at(-1)!,/cached/);
  await turn('fresh','Read '+deck+' fresh with secret123');assert.equal(provider().length,initialContacts);assert.equal(sent.at(-1),lastReview);assert.ok(!lastReview.includes('secret123'));
  lose=true;inspectionUnavailable=true;await turn('yes','Yes.',false);assert.equal(provider().filter(x=>x.kind==='vision').length,2);assert.ok(provider().filter(x=>x.kind==='gate').at(-1).passcode_present);
  await wait(()=>Boolean(db!.prepare("SELECT p.dispatch_id FROM prepared_replies p JOIN dispatches d ON d.id=p.dispatch_id JOIN inbound_events e ON e.id=p.event_id WHERE e.source_id='owner-yes' AND p.purpose='outcome_pending' AND d.status='accepted'").get()));
  await app.close();app=undefined;db.close();db=undefined;
  const recovery=new DatabaseSync(join(folder,'state/operations.sqlite'));
  try{assert.equal(recovery.prepare("UPDATE actions SET updated_at=? WHERE kind='owner_web_read' AND status='unknown' AND source_event_id=(SELECT id FROM inbound_events WHERE source_id='owner-yes')")
    .run(Date.now()-DELAYED_OUTCOME_POLICY.owner_web_read.inspectIntervalMs-1).changes,1);}finally{recovery.close();}
  inspectionUnavailable=false;phase='yes';step=1;app=await mount();db=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});await wait(()=>sent.filter(x=>x.includes('Cedar revenue')).length===3);assert.equal(provider().filter(x=>x.kind==='vision').length,2);
  writeFileSync(join(folder,'receiver/docsend-unknown'),'1');await turn('unknown','Read https://docsend.com/view/Unknown',false);await wait(()=>Boolean(db!.prepare("SELECT p.dispatch_id FROM prepared_replies p JOIN dispatches d ON d.id=p.dispatch_id JOIN inbound_events e ON e.id=p.event_id WHERE e.source_id='owner-unknown' AND d.status='accepted'").get()));assert.match(sent.at(-1)!,/^I couldn’t confirm whether the URL read happened\./);const afterUnknown=provider().length;await turn('hello','Hello.');assert.equal(sent.at(-1),'Hello.');assert.equal(provider().length,afterUnknown);
  await app.close();app=undefined;db.close();db=undefined;phase='hello';step=0;app=await mount();assert.equal(provider().length,afterUnknown);
 }finally{if(app)await app.close();db?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
