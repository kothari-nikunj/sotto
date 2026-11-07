import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn,type ChildProcessWithoutNullStreams} from 'node:child_process';
import {once} from 'node:events';
import {existsSync,readFileSync,writeFileSync,mkdtempSync,mkdirSync,rmSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {DatabaseSync} from 'node:sqlite';
import {Message} from 'chat';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import {startConfiguredRuntime} from '../src/bootstrap/main.js';
import {procedureReviewText} from '../src/tools/owner-preference-tool-contract.js';
async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_DIRECT_PROCEDURE: '1',
      SOTTO_ROUTINE_EPOCH: 'e'.repeat(32), SOTTO_TEST_ROUTINE_SOURCE_REVISION: 'a'.repeat(64), SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
      SOTTO_DATA: folder, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'tenant', SOTTO_ACCOUNT_ID: 'account',
      SOTTO_LINE_KEY: 'line', SOTTO_MCP_TOKEN: '0123456789abcdef0123456789abcdef',
      PHOTON_HOME_CHANNEL: '+15555550123', PHOTON_ALLOWED_USERS: '+15555550123', PHOTON_PROJECT_ID: 'fixture-project',
      PHOTON_PROJECT_SECRET: 'fixture-secret', PHOTON_ALLOW_ALL_USERS: 'false', SOTTO_IMESSAGE_NUMBER: '+15555550999',
      SOTTO_TIMEZONE: 'America/Los_Angeles' } });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr = (stderr + String(chunk)).slice(-4096); });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const line = await Promise.race([new Promise<string>((done, fail) => {
        let output = '';
        child.stdout.on('data', chunk => { output += String(chunk); const i = output.indexOf('\n'); if (i >= 0)
          done(output.slice(0, i)); });
        child.once('exit', code => fail(new Error(`receiver exited ${code}: ${stderr}`)));
        child.once('error', fail);
      }), new Promise<never>((_, fail) => { timer = setTimeout(() => fail(new Error('receiver start timeout')), 15000); })]);
    const parsed = JSON.parse(line) as {
      port: number;
      identity: OwnerToolIdentity;
      bearer: string;
      readBearer: string;
    };
    return { child, ...parsed, providerThreadId:parsed.identity.route_id, bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp` };
  }
  catch (error) {
    child.kill('SIGKILL');
    throw error;
  }
  finally {
    if (timer)
      clearTimeout(timer);
  }
}
async function stopReceiver(child: ChildProcessWithoutNullStreams) {
  if (child.exitCode !== null)
    return;
  const ended = once(child, 'exit');
  child.stdin.end('quit\n');
  const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
  try {
    await ended;
  }
  finally {
    clearTimeout(timer);
  }
}

test('default bootstrap stops settled preference steps and sends canonical responses; nonterminal results continue',async()=>{
 const folder=mkdtempSync(join(tmpdir(),'preference-handoff-'));
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined,observer:DatabaseSync|undefined;
 const rule='Never book me Fridays.',unknownRule='Never book me Saturdays.';
 let phase='preview';const calls:Record<string,number>={},sent:string[]=[];
 const resultStatuses:Record<string,string[]>={};let masterReads=0;
 const commands:Record<string,string[]>={oneoff:['tone','shorter pls'],read:['show'],rejected:['remove-tone','Missing tone'],error:['unsupported'],nomatch:['accept-offer']};
 const waitFor=async(predicate:()=>boolean)=>{const until=Date.now()+15000;while(!predicate()){if(Date.now()>until)throw Error('local handoff did not settle');await new Promise(resolve=>setTimeout(resolve,10));}};
 try{
  receiver=await startReceiver(join(folder,'receiver'));const thread=receiver.identity.route_id;
  const media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const config={version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-preference-handoff-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use the original Feedback skill and enabled commands.',skillPackRoot:resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver.bridgeUrl,readBearer:receiver.readBearer},ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}};
  const configPath=join(folder,'runtime.json');writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async()=>null,addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  const external:typeof fetch=async(input,init)=>{
   const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
   if(url.origin!=='https://proxy.example'){
    assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin);
    const response=await fetch(input,init);const request=JSON.parse(String(init?.body??'{}'));
    if(request.operation==='master_file')masterReads++;
    if(url.pathname==='/runtime/owner-preference-tool'){const result=await response.clone().json();(resultStatuses[phase]??=[]).push(result.status);}
    return response;
   }
   assert.equal(url.pathname,'/openai/v1/chat/completions');calls[phase]=(calls[phase]??0)+1;const count=calls[phase]!;
   const body=JSON.parse(String(init!.body));
   let call:{name:string;args:unknown}|undefined;
   if(phase==='preview'){
    assert.ok(count<=2,'proposed receipt must prevent a third model contact');
    call=count===1?{name:'skill',args:{name:'sotto-feedback'}}:{name:'run_sotto_script',args:{id:'master-file',args:['append','--section','Procedures','--text',rule]}};
    if(count===2)assert.ok(JSON.stringify(body.messages).includes('Adding to your standing file'));
   }else if(phase==='confirm'){
    assert.equal(count,1,'committed receipt must prevent another model contact');assert.ok(JSON.stringify(body.messages).includes('Accepted standing-rule review for this owner confirmation'));
    call={name:'run_sotto_script',args:{id:'master-file',args:['confirm-procedure']}};
   }else if(phase==='rejected'){
    assert.equal(count,1,'rejected recorded action must prevent another model contact');call={name:'run_sotto_script',args:{id:'preferences',args:commands.rejected}};
   }else if(phase==='unknown_preview'){
    assert.equal(count,1);call={name:'run_sotto_script',args:{id:'master-file',args:['append','--section','Procedures','--text',unknownRule]}};
   }else if(count===1){
    const args=phase==='unknown'?['confirm-procedure']:commands[phase]!;
    call={name:'run_sotto_script',args:{id:phase==='unknown'||phase==='nomatch'?'master-file':'preferences',args,ownerPreferenceSettled:true}};
   }else{assert.equal(count,2,'nonterminal control has one continuation');}
   return new Response(JSON.stringify({id:'synthetic-'+phase+'-'+count,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'call-'+phase+'-'+count,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:'Continued '+phase+'.'},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
  };
  // Intentionally omit createAgent: the production default runtime/model/schema/automatic master reader run unchanged.
  app=await startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['accepted-'+sent.length]};}}),openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
  observer=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
  const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  const turn=async(next:string,text:string,bound=false)=>{
   phase=next;const previous=sent.length;const inbound=message('owner-'+next,text);
   if(bound)inbound.replyTo=message('nonexistent-offer','Synthetic missing offer');
   await app!.gateway.chat.processMessage(adapter as never,thread,inbound);
   await waitFor(()=>next==='unknown'?calls[next]===2&&Boolean(observer!.prepare("SELECT id FROM actions WHERE status='unknown'").get()):sent.length>previous);
  };
  await turn('preview','Remember: '+rule);assert.equal(calls.preview,2);assert.equal(sent.at(-1),procedureReviewText({rule}));assert.equal(existsSync(join(folder,'receiver/knowledge/master.md')),false);
  await turn('confirm','Yes.');assert.equal(calls.confirm,1);assert.equal(sent.at(-1),'Standing rule saved: '+rule);assert.equal(readFileSync(join(folder,'receiver/knowledge/master.md'),'utf8').split(rule).length-1,1);
  await turn('rejected','Remove the tone note Missing tone.');assert.equal(calls.rejected,1);assert.equal(sent.at(-1),'That preference change could not be recorded because its exact target or owner wording is unavailable, or it conflicts with a saved request.');
  const prefsBefore=existsSync(join(folder,'receiver/preferences.json'))?readFileSync(join(folder,'receiver/preferences.json'),'utf8'):null;
  const actionCount=observer.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_preference_tool'").get()!.n;
  await turn('oneoff','shorter pls');assert.equal(calls.oneoff,2);assert.equal(sent.at(-1),'Continued oneoff.');
  assert.equal(observer.prepare("SELECT COUNT(*) AS n FROM actions WHERE kind='owner_preference_tool'").get()!.n,actionCount);
  assert.equal(existsSync(join(folder,'receiver/preferences.json'))?readFileSync(join(folder,'receiver/preferences.json'),'utf8'):null,prefsBefore);
  assert.equal(resultStatuses.oneoff,undefined,'one-off script request is rejected before contacting receiver');
  await turn('read','Show my preferences.');assert.equal(calls.read,2);assert.equal(sent.at(-1),'Continued read.');
  await turn('error','Show my preferences.');assert.equal(calls.error,2);assert.equal(sent.at(-1),'Continued error.');
  await turn('nomatch','Yes.',true);assert.equal(calls.nomatch,2);assert.equal(sent.at(-1),'Continued nomatch.');assert.ok(resultStatuses.nomatch?.includes('no_match'));
  await turn('unknown_preview','Remember: '+unknownRule);writeFileSync(join(folder,'receiver/direct-master-sync-fail'),'1');
  const before=sent.length;await turn('unknown','Yes.');assert.equal(calls.unknown,2);assert.equal(sent.length,before);assert.ok(resultStatuses.unknown?.includes('unknown'));
  assert.equal(readFileSync(join(folder,'receiver/knowledge/master.md'),'utf8').split(unknownRule).length-1,1);assert.ok(masterReads>=8,'default automatic original master reader runs for every turn');
 }finally{observer?.close();await app?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
