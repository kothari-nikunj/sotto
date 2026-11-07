import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn,type ChildProcessWithoutNullStreams} from 'node:child_process';
import {once} from 'node:events';
import {existsSync,mkdtempSync,mkdirSync,writeFileSync,readFileSync,readdirSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {Message} from 'chat';
import {startConfiguredRuntime} from '../src/bootstrap/main.js';
import {taskReviewText} from '../src/tools/owner-task-tool-contract.js';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
async function startReceiver(folder: string, proactive = false) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_KNOWLEDGE_READ:'1', SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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




test('native owner teaching reviews canonical fact, recovers lost receipt after restart, and Ask reads it next turn',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'fact-default-'));let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined,observer:DatabaseSync|undefined;
 let phase='add',step=0,applies=0,queries=0,askRead=false,lose=false,hide=false;const sent:string[]=[];const ids:Record<string,string>={};
 const waitFor=async(predicate:()=>boolean)=>{const until=Date.now()+15000;while(!predicate()){if(Date.now()>until)throw Error('relationship turn did not settle');await new Promise(resolve=>setTimeout(resolve,10));}};
 try{
  receiver=await startReceiver(join(folder,'receiver'));const thread=receiver.identity.route_id;const media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const config={version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-relation-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use original skills and enabled commands.',skillPackRoot:resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver.bridgeUrl,readBearer:receiver.readBearer},ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}};
  const configPath=join(folder,'runtime.json');writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async()=>null,addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  const external:typeof fetch=async(input,init)=>{
   const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);const body=JSON.parse(String(init?.body??'{}'));
   if(url.origin!=='https://proxy.example'){
    assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin);
    if(url.pathname==='/runtime/owner-loop-tool'&&body.operation==='inspect'&&hide)return new Response('{}',{status:503});
    const response=await fetch(input,init);
    if(body.operation==='knowledge_query'){queries++;
     const json=await response.clone().json();const found=new Set<string>();const visit=(value:any)=>{if(value&&typeof value==='object'){for(const [key,item] of Object.entries(value)){if(/^c_[a-f0-9]{6,64}$/.test(key))found.add(key);if(key==='canonical_id'&&typeof item==='string')found.add(item);visit(item);}}};visit(json);assert.equal(found.size,1);const argv=body.arguments.argv;ids[argv[argv.indexOf('--person')+1]]=Array.from(found)[0]!;
    }
    if(url.pathname==='/runtime/owner-loop-tool'&&body.operation==='apply'){applies++;assert.equal((await response.clone().json()).status,'committed');if(lose){lose=false;throw Error('synthetic response loss');}}
    return response;
   }
   step++;let call:{name:string;args:unknown}|undefined;
   if(phase==='add'){
    if(step===1)call={name:'skill',args:{name:'sotto-feedback'}};
    if(step===2){assert.match(JSON.stringify(body.messages),/Match the correction/);call={name:'run_sotto_script',args:{id:'knowledge-query',args:['--person','Dana Cedar']}};}
    if(step===3){assert.ok(ids['Dana Cedar']);call={name:'run_sotto_script',args:{id:'knowledge-edit',args:['--op','add','--slug',ids['Dana Cedar'],'--text','Dana Cedar leads infrastructure at Cedar.']}};}
   }else if(phase==='confirm'&&step===1)call={name:'run_sotto_script',args:{id:'knowledge-edit',args:['--op','confirm-fact']}};
   else if(phase==='ask'&&step===1)call={name:'skill',args:{name:'sotto-ask'}};
   else if(phase==='ask'&&step===2)call={name:'run_sotto_script',args:{id:'knowledge-query',args:['--person','Dana Cedar']}};
   if(phase==='ask'&&step===3){assert.match(JSON.stringify(body.messages),/Dana Cedar leads infrastructure at Cedar/);askRead=true;}
   return new Response(JSON.stringify({id:'synthetic-'+phase+'-'+step,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'call-'+phase+'-'+step,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:phase==='safe'?'Hello safely.':'Original read complete.'},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
  };
  const mount=()=>startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['accepted-'+sent.length]};}}),openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
  const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  app=await mount();observer=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
  const turn=async(next:string,id:string,text:string,unknown=false)=>{phase=next;step=0;const before=sent.length;await app!.gateway.chat.processMessage(adapter as never,thread,message(id,text));await waitFor(()=>unknown?Boolean(observer!.prepare("SELECT id FROM actions WHERE status='unknown'").get()):sent.length>before);};
  const people=()=>readdirSync(join(folder,'receiver/knowledge/people')).filter(name=>name.endsWith('.md')).map(name=>readFileSync(join(folder,'receiver/knowledge/people',name),'utf8'));
  await turn('add','add','Remember: Dana Cedar leads infrastructure at Cedar.');assert.equal(applies,0);const preview=JSON.parse(String(observer.prepare("SELECT result_json FROM actions WHERE kind='owner_loop_tool'").get()!.result_json)).preview;assert.equal(sent.at(-1),taskReviewText(preview.review));assert.ok(people().every(text=>!text.includes('Dana Cedar leads infrastructure at Cedar.')));
  lose=true;hide=true;await turn('confirm','add-yes','Yes.',true);assert.equal(applies,1);assert.ok(people().some(text=>text.includes('Dana Cedar leads infrastructure at Cedar.')));
  await app.close();app=undefined;hide=false;
  {const aged=new DatabaseSync(join(folder,'state/operations.sqlite'));aged.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE status='unknown'").run();aged.close();}
  app=await mount();await waitFor(()=>sent.at(-1)==='Fact recorded.');assert.equal(applies,1);
  const beforeQueries=queries;await turn('ask','ask','What do you know about Dana Cedar?');assert.equal(applies,1);assert.equal(queries,beforeQueries+1);assert.equal(askRead,true);assert.equal(step,3);
  assert.ok(people().some(text=>text.includes('In 2020 Dana discussed')));

 }finally{observer?.close();await app?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
