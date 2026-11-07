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
import {gmailDraftReviewText} from '../src/tools/owner-gmail-draft-contract.js';
async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_GMAIL_SEND: '1', SOTTO_TEST_GMAIL_DRAFT:'1', SOTTO_TEST_STYLE_APPLY:'1', SOTTO_TEST_TYPED_OUTCOME:'1', SOTTO_HOME:join(folder,'sotto-home'),
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


for(const scenario of ['edited-loss','unchanged','learning-fault','unknown-send','pair-loss','pair-unknown'] as const)test(`default original typed Decline -> edit/review -> approved send -> original outcome; ${scenario}`,async()=>{
 const folder=mkdtempSync(join(tmpdir(),'draft-outcome-'));const sent:string[]=[];
 let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined,app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined,observer:DatabaseSync|undefined;
 const baseline='Thanks for asking. I will pass on this one.';
 const body=scenario==='unchanged'?baseline:'Thank you for the invitation. I cannot review the Cedar packet. Please ask someone else.';
 let phase='options',step=0,loseResponse=scenario==='edited-loss',blockInspect=scenario==='edited-loss';let applies=0,inspectUnavailable=false,recordOffers=0,recordLossObserved=false;
 const waitFor=async(predicate:()=>boolean)=>{const until=Date.now()+20000;while(!predicate()){if(Date.now()>until)throw Error('typed draft turn did not settle');await new Promise(resolve=>setTimeout(resolve,10));}};
 const rows=(name:string)=>{const path=name==='outcomes.jsonl'?join(folder,'receiver',name):join(folder,'receiver/events',name);return existsSync(path)?readFileSync(path,'utf8').trim().split('\n').filter(Boolean).map(line=>JSON.parse(line)):[];};
 try{
  receiver=await startReceiver(join(folder,'receiver'));const thread=receiver.identity.route_id;
  const media=join(folder,'media');mkdirSync(media,{mode:0o700});
  const adapter={name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async()=>null,addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
  const external:typeof fetch=async(input,init)=>{
   const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
   if(url.origin!=='https://proxy.example'){
    assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin);const request=JSON.parse(String(init?.body??'{}'));
    if(url.pathname==='/runtime/owner-gmail-draft-tool'&&request.operation==='inspect'&&inspectUnavailable){await new Promise((_,reject)=>init?.signal?.addEventListener('abort',()=>reject(new DOMException('synthetic unavailable inspection','AbortError')),{once:true}));throw Error('inspection unavailable');}
    if(url.pathname==='/runtime/owner-gmail-draft-tool'&&request.operation==='inspect'&&blockInspect)throw Error('synthetic inspection unavailable');
    if(url.pathname==='/runtime/owner-gmail-draft-tool'&&request.operation==='apply')applies++;
    if(url.pathname==='/runtime/owner-gmail-draft-tool'&&request.operation==='record_offer')recordOffers++;
    const response=await fetch(input,init);
    if(url.pathname==='/runtime/owner-gmail-draft-tool'&&request.operation==='apply'){if(loseResponse){await response.body?.cancel();throw Error('synthetic committed response lost');}}
    if(url.pathname==='/runtime/owner-gmail-draft-tool'&&request.operation==='record_offer'&&scenario==='pair-loss'&&recordOffers===1){assert.equal(response.status,200);const recorded=await response.clone().json();assert.equal(recorded.status,'preview');assert.ok(recorded.offer_receipt);await response.body?.cancel();recordLossObserved=true;throw Error('offered recording receipt lost');}
    return response;
   }
   const prompt=JSON.stringify(JSON.parse(String(init!.body)).messages);
   const calls=phase==='options'?[
    {name:'skill',args:{name:'sotto-draft-reply'}},{name:'gmail_search',args:{query:'from:alice@example.com subject:Cedar',max:5}},
    {name:'gmail_get',args:{message_id:'message-123'}},{name:'run_sotto_script',args:{id:'style-apply',args:[JSON.stringify({recipient:'Alice',channel:'email'})]}},{name:'gmail_draft_pair',args:{message_id:'message-123',accept_body:'Happy to review it.',decline_body:baseline}},null
   ]:phase==='selected'?[{name:'gmail_draft_propose',args:{message_id:'message-123',body:baseline,action_type:'decline'}},null]:phase==='edited'?[{name:'gmail_send_review',args:{message_id:'message-123',body,action_type:'decline'}},null]:phase==='confirm'?[{name:'gmail_send_confirm',args:{}},null]:[null];
   const call=calls[step++];
   let finalText='Hello.';if(!call&&['options','selected','edited'].includes(phase)){const action=observer!.prepare('SELECT result_json FROM actions ORDER BY created_at DESC,id DESC LIMIT 1').get();const preview=JSON.parse(String(action!.result_json)).preview;finalText=gmailDraftReviewText(preview);}
   if(phase==='options'&&step===2)assert.ok(prompt.includes('run_sotto_script style-apply'));
   if(phase==='options'&&step===5){assert.ok(prompt.includes('How to write this'));assert.ok(prompt.includes(baseline));}
   return new Response(JSON.stringify({id:'synthetic-'+phase+'-'+step,object:'chat.completion',created:1,model:'chat-model',choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'call-'+phase+'-'+step,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:finalText},finish_reason:call?'tool_calls':'stop'}],usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
  };
  const mount=async()=>{
   const configPath=join(folder,'runtime.json');
   writeFileSync(configPath,JSON.stringify({version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,bearerToken:'synthetic-draft-outcome-at-least-32-characters',port:0,tenantId:'tenant',owner:{accountId:'account',ownerId:receiver!.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},instructions:'Use the original loaded skills and enabled tools.',skillPackRoot:process.env.SOTTO_TEST_SKILL_PACK??resolve('../../sotto-chief-of-staff'),modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},bridge:{url:receiver!.bridgeUrl,readBearer:receiver!.readBearer},ownerLoop:{mode:'explicit_command',identity:receiver!.identity,bearer:receiver!.bearer}}),{mode:0o600});
   app=await startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,createTransport:()=>({send:async request=>{sent.push(request.text??'');return scenario==='pair-unknown'&&phase==='options'?{acceptance:'unknown',providerMessageIds:[]}:{acceptance:'accepted',providerMessageIds:['accepted-'+sent.length]};}}),openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
   observer=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
  };
  await mount();
  const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
  const turn=async(id:string,text:string,target:number)=>{await app!.gateway.chat.processMessage(adapter as never,thread,message(id,text));await waitFor(()=>sent.length===target);};
  await turn('request','Help me reply to Alice about the Cedar review request.',1);assert.match(sent[0]!,/Accept:/);assert.match(sent[0]!,/Decline:/);
  if(scenario==='pair-loss')await waitFor(()=>recordLossObserved);
  if(scenario==='pair-loss'||scenario==='pair-unknown'){
   const prior=observer!.prepare("SELECT result_json FROM actions WHERE json_extract(result_json,'$.args.operation')='propose_pair'").get()!;assert.equal(JSON.parse(String(prior.result_json)).preview.offer_receipt,undefined);
   if(scenario==='pair-unknown'){assert.equal(rows('drafts.jsonl').length,0);assert.equal(recordOffers,0);}
   observer!.close();observer=undefined;await app!.close();app=undefined;await stopReceiver(receiver.child);receiver=await startReceiver(join(folder,'receiver'));phase='safe';step=0;await mount();assert.equal(sent.length,1,'accepted or uncertain pair is never resent');
   if(scenario==='pair-unknown'){await turn('safe-unknown-pair','Hello',2);assert.equal(rows('drafts.jsonl').length,0);assert.equal(recordOffers,0);assert.equal(applies,0);return;}
   assert.equal(recordOffers,2);assert.equal(applies,0);
  }
  await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE json_extract(result_json,'$.args.operation')='propose_pair' AND json_extract(result_json,'$.preview.offer_receipt') IS NOT NULL").get()));
  assert.equal(rows('drafts.jsonl').length,2);
  const pairAction=observer!.prepare("SELECT * FROM actions WHERE json_extract(result_json,'$.args.operation')='propose_pair'").get()!;
  const pair=JSON.parse(String(pairAction.result_json)).preview;assert.equal(sent[0],gmailDraftReviewText(pair));assert.equal(applies,0);assert.ok(pair.offer_receipt);assert.equal(rows('outcomes.jsonl').length,0);
  phase='selected';step=0;await turn('selected','Use the Decline option.',2);
  const rootAction=observer!.prepare("SELECT * FROM actions WHERE json_extract(result_json,'$.args.operation')='propose_typed'").get()!;
  const root=JSON.parse(String(rootAction.result_json)).preview;assert.equal(sent[1],gmailDraftReviewText(root));assert.equal(root.review.draft_origin.action_type,'decline');assert.equal(root.review.draft_origin.baseline_action_id,pairAction.id);assert.equal(root.review.draft_origin.baseline_key,pair.review.decline_key);assert.equal(rows('drafts.jsonl').length,2,'both delivered original options remain offered');
  phase='edited';step=0;await turn('edited',scenario==='unchanged'?'Send that exact Decline reply.':'Change the Decline to: '+body+' Prepare it for sending.',3);
  const editAction=observer!.prepare("SELECT * FROM actions WHERE json_extract(result_json,'$.args.operation')='propose_send_typed'").get()!;
  const edit=JSON.parse(String(editAction.result_json));assert.equal(sent[2],gmailDraftReviewText(edit.preview));assert.deepEqual(edit.preview.review.draft_origin,root.review.draft_origin);assert.equal(edit.args.baseline.action_id,rootAction.id);assert.equal(edit.preview.review.body,body);
  assert.equal(rows('drafts.jsonl').length,2);assert.equal(rows('drafts.jsonl').find(row=>row.action_type==='decline').text,baseline);assert.equal(rows('outcomes.jsonl').length,0);
  const providerPath=join(folder,'receiver/provider-calls.jsonl');assert.equal(existsSync(providerPath),false);
  if(scenario==='learning-fault')writeFileSync(join(folder,'receiver/learning_fault'),'1');if(scenario==='unknown-send')writeFileSync(join(folder,'receiver/provider_unknown'),'1');
  phase='confirm';step=0;await app!.gateway.chat.processMessage(adapter as never,thread,message('confirm','Yes, send it.'));
  await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE json_extract(result_json,'$.args.operation')='send' AND status IN ('unknown','succeeded')").get()));
  if(scenario==='unchanged'||scenario==='learning-fault'||scenario==='pair-loss'){await waitFor(()=>sent.length===4);assert.ok(sent[3]!.startsWith('Email sent.'));if(scenario==='learning-fault')assert.match(sent[3]!,/couldn’t save the feedback from this draft yet/);}
  const posts=readFileSync(providerPath,'utf8').trim().split('\n').map(line=>JSON.parse(line));assert.equal(posts.length,1);assert.equal(posts[0].operation,'messages.send');
  const mime=Buffer.from(posts[0].payload.raw,'base64url').toString();assert.equal(Buffer.from(mime.split('\n\n')[1]!.replace(/\s/g,''),'base64').toString(),body);
  if(scenario==='unknown-send')assert.equal(rows('outcomes.jsonl').length,0);
  observer!.close();observer=undefined;await app!.close();app=undefined;await stopReceiver(receiver.child);
  if(scenario==='learning-fault')inspectUnavailable=true;loseResponse=false;blockInspect=false;phase='safe';step=0;
  const startupBegan=Date.now();receiver=await startReceiver(join(folder,'receiver'));await mount();
  if(scenario==='edited-loss')await waitFor(()=>sent.length===4);
  if(scenario==='learning-fault'){
   const count=sent.length;await turn('safe-during-learning-hold','Hello',count+1);assert.ok(Date.now()-startupBegan<25000,'bounded startup inspect permits unrelated safe chat');
   assert.equal(observer!.prepare("SELECT count(*) n FROM actions WHERE json_extract(result_json,'$.receipt.learning')='pending' AND status='succeeded'").get()!.n,1);
   assert.equal(readFileSync(providerPath,'utf8').trim().split('\n').length,1);
   observer!.close();observer=undefined;await app!.close();app=undefined;await stopReceiver(receiver.child);
   rmSync(join(folder,'receiver/learning_fault'));inspectUnavailable=false;receiver=await startReceiver(join(folder,'receiver'));await mount();
  }
  if(scenario==='learning-fault')await waitFor(()=>observer!.prepare("SELECT count(*) n FROM actions WHERE json_extract(result_json,'$.receipt.learning')='recorded'").get()!.n===1);
  if(scenario!=='unknown-send'){
   const outcomes=rows('outcomes.jsonl');assert.equal(outcomes.length,1);assert.equal(outcomes[0].outcome,scenario==='unchanged'?'executed':'edited_and_sent');assert.equal(outcomes[0].action_type,'decline');assert.equal(outcomes[0].tier,'review');assert.equal(outcomes[0].action_id,root.review.draft_origin.baseline_key);assert.equal(outcomes[0].source_event_id,'email:sent-123');
   if(scenario==='unchanged'){const style=JSON.parse(readFileSync(join(folder,'receiver/style.json'),'utf8'));assert.ok(style.confirmed.some((sample:{text:string})=>sample.text===baseline));}
  }
  const before=sent.length;await turn('safe','Hello',before+1);assert.equal(sent.at(-1),'Hello.');assert.equal(applies,1);assert.equal(readFileSync(providerPath,'utf8').trim().split('\n').length,1);assert.equal(rows('drafts.jsonl').length,2);
 }finally{observer?.close();await app?.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});
