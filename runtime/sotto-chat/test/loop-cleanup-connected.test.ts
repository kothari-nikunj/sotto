import assert from 'node:assert/strict';
import test from 'node:test';
import {spawn, type ChildProcessWithoutNullStreams} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync, readFileSync, readdirSync, writeFileSync, mkdirSync} from 'node:fs';
import {once} from 'node:events';
import {DatabaseSync} from 'node:sqlite';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {Message} from 'chat';
import type {OwnerToolIdentity} from '../src/tools/owner-contract.js';
import {startConfiguredRuntime} from '../src/bootstrap/main.js';

async function startReceiver(folder: string, crash = false) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_LOOP_CLEANUP: '1', SOTTO_TEST_LOOP_CLEANUP_CRASH: crash ? '1' : undefined, SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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




// The model and channel are synthetic; bootstrap, skills, admission, original writers,
// journal, readback and restart recovery are the actual application.

for (const crash of [false, true]) test(`default bootstrap cleanup: saved list and ${crash ? 'mid-item receiver crash with partial outcomes' : 'lost complete receipt'} recover without replay`, async () => {
  const originalFetch = globalThis.fetch;
  const folder = mkdtempSync(join(tmpdir(), 'sotto-cleanup-'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  let app: Awaited<ReturnType<typeof startConfiguredRuntime>> | undefined;
  let observer: DatabaseSync | undefined;
  let phase: 'list' | 'choices' | 'safe' = 'list';
  let phaseCalls = 0, totalCalls = 0, applies = 0, inspections = 0, lose = true, hide = true;
  let listText = '', ownerText = '';
  let intents: {index:number;action:'dismiss'|'snooze'|'keep';correction:string;days?:number}[] = [];
  const sent: string[] = [], diagnostic: unknown[] = [];
  const waitFor = async (predicate: () => boolean) => {
    const until = Date.now() + 15000;
    while (!predicate()) {
      if (Date.now() > until) throw Error('cleanup did not settle '+JSON.stringify({phase,phaseCalls,applies,sent,diagnostic,actions:observer?.prepare('SELECT kind,status,result_json FROM actions').all()}));
      await new Promise(resolve => setTimeout(resolve, 10));
    }
  };
  const snapshot = () => {
    const directory = join(folder, 'receiver/knowledge/continuity');
    return Object.fromEntries(readdirSync(directory).filter(name=>name.endsWith('.md')).sort().map(name => [name, readFileSync(join(directory, name), 'utf8')]));
  };
  try {
    receiver = await startReceiver(join(folder, 'receiver'), crash);
    const thread = receiver.identity.route_id;
    const media = join(folder, 'media'); mkdirSync(media, {mode:0o700});
    const config = {version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,
      bearerToken:'synthetic-cleanup-at-least-32-character-token',port:0,tenantId:'tenant',
      owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,
      credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},
      instructions:'Use the original Loops skill and enabled cleanup commands.',
      skillPackRoot:process.env.SOTTO_TEST_SKILL_PACK ?? resolve('../../sotto-chief-of-staff'),
      modelProxy:{origin:'https://proxy.example',bearer:'synthetic-proxy',chatModelId:'chat-model',nativeMediaModelId:'media-model'},
      bridge:{url:receiver.bridgeUrl,readBearer:receiver.readBearer},
      ownerLoop:{mode:'explicit_command',identity:receiver.identity,bearer:receiver.bearer}};
    const configPath = join(folder,'runtime.json'); writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
    const adapter = {name:'imessage',userName:'fixture',app:null,initialize:async()=>{},disconnect:async()=>{},
      startGatewayListener:async(options:{waitUntil:(work:Promise<void>)=>void},_duration:unknown,signal:AbortSignal)=>{options.waitUntil(new Promise<void>(resolve=>signal.addEventListener('abort',()=>resolve(),{once:true})));return {ok:true,status:200};},
      isDM:()=>true,channelIdFromThreadId:()=>thread,decodeThreadId:()=>({chatGuid:'iMessage;-;+15555550123',phone:'+15555550999'}),encodeThreadId:()=>thread,
      postMessage:async()=>({id:'reply'}),fetchMessages:async()=>({messages:[],hasMore:false}),fetchMessage:async()=>null,
      addReaction:async()=>{},removeReaction:async()=>{},deleteMessage:async()=>{},editMessage:async()=>({id:'unused'}),startTyping:async()=>{},renderFormatted:()=>''};
    const external: typeof fetch = async(input, init) => {
      const url = new URL(typeof input==='string'||input instanceof URL ? input : input.url);
      const body = JSON.parse(String(init?.body ?? '{}'));
      if (url.origin !== 'https://proxy.example') {
        assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin,'unexpected provider contact');
        const cleanup = url.pathname==='/runtime/owner-loop-tool' && body.args?.operation==='cleanup';
        if (cleanup && body.operation==='inspect') {inspections++; if (hide) return new Response('{}',{status:503});}
        if (cleanup && body.operation==='apply') applies++;
        const response = await originalFetch(input,init);
        if (url.pathname==='/runtime/owner-loop-tool') diagnostic.push({request:body.operation,operation:body.args?.operation,status:response.status,result:await response.clone().json()});
        if (cleanup && body.operation==='apply') {
          assert.equal(response.status,200);
          assert.equal((await response.clone().json()).status,'committed');
          if (lose) throw Error('synthetic lost committed cleanup response');
        }
        return response;
      }
      phaseCalls++; totalCalls++; assert.ok(totalCalls<=12,'bounded synthetic model calls');
      let call: {name:string;args:unknown}|undefined;
      let answer = 'Hello safely.';
      if (phase==='list') {
        assert.ok(phaseCalls<=2,'the coordinator delivers the saved list without a model restatement');
        if (phaseCalls===1) call={name:'skill',args:{name:'sotto-loops'}};
        else if (phaseCalls===2) {
          assert.match(JSON.stringify(body.messages),/Clearing the stale ones/);
          assert.ok(body.tools.some((tool:any)=>tool.function?.name==='run_sotto_script'&&tool.function.description.includes('retune-apply')));
          call={name:'run_sotto_script',args:{id:'retune-apply',args:['list']}};
        }
      } else if (phase==='choices') {
        assert.ok(phaseCalls<=2);
        if (phaseCalls===1) call={name:'run_sotto_script',args:{id:'retune-apply',args:['cleanup',JSON.stringify({items:intents})]}};
        else answer='The cleanup result is pending.';
      }
      return new Response(JSON.stringify({id:'synthetic-'+totalCalls,object:'chat.completion',created:1,model:'chat-model',
        choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'cleanup-'+totalCalls,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:answer},finish_reason:call?'tool_calls':'stop'}],
        usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
    };
    const mount = () => startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,
      createTransport:()=>({send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['cleanup-accepted-'+sent.length]};}}),
      openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
    const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
    globalThis.fetch=external;
    app=await mount(); observer=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});
    const before=snapshot();
    await app.gateway.chat.processMessage(adapter as never,thread,message('cleanup-list','Help me clean up my stale loops.'));
    await waitFor(()=>sent.length===1);
    await waitFor(()=>Boolean(observer!.prepare("SELECT e.id FROM inbound_events e JOIN prepared_replies r ON r.event_id=e.id JOIN dispatches d ON d.id=r.dispatch_id WHERE e.source_id='cleanup-list' AND e.outcome='resolved' AND d.status='accepted'").get()));
    listText=sent[0]!;assert.equal(phaseCalls,2,'skill load plus list tool, no discarded continuation');assert.match(listText,/Send the Cedar deck/); assert.match(listText,/Review the Cedar notes/);
    assert.deepEqual(snapshot(),before,'listing has no canonical write'); assert.equal(applies,0);
    const numbered=(needle:string)=>{const line=listText.split('\n').find(line=>line.includes(needle));assert.ok(line);const match=line.match(/^\s*(\d+)[.)]/);assert.ok(match,'canonical item must be numbered: '+line);return Number(match[1]);};
    const deck=numbered('Send the Cedar deck'), notes=numbered('Review the Cedar notes'), reply=numbered('Await the Cedar reply');
    if(crash) assert.ok(reply<deck && deck<notes,'fixture list orders committed keep, uncertain dismiss, skipped snooze');
    ownerText=`Dismiss item ${deck} and snooze item ${notes} for 7 days, and keep item ${reply}.`;
    intents=[{index:deck,action:'dismiss',correction:`Dismiss item ${deck}`},{index:notes,action:'snooze',correction:`snooze item ${notes} for 7 days`,days:7},{index:reply,action:'keep',correction:`keep item ${reply}`}];
    await app.close(); app=undefined; app=await mount();
    phase='choices';phaseCalls=0;
    await app.gateway.chat.processMessage(adapter as never,thread,message('cleanup-choices',ownerText));
    await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE kind='owner_loop_tool' AND status='unknown'").get()));
    assert.equal(applies,1);
    // An uncertainty notice may already be accepted when the action becomes unknown.
    // Assert its meaning and count, not which asynchronous callback won the race.
    await waitFor(()=>sent.some(text=>text.startsWith('I couldn’t confirm whether the task change happened.')));
    assert.equal(sent.length,2,'one list and one uncertainty notice, no success claim');
    assert.match(sent[1]!,/^I couldn’t confirm whether the task change happened\./);
    const changed=snapshot();
    assert.ok(existsSync(join(folder,'receiver/cleanup-writes.jsonl')),JSON.stringify(diagnostic));
    const writes=readFileSync(join(folder,'receiver/cleanup-writes.jsonl'),'utf8');
    assert.equal(writes.trim().split('\n').length,crash?2:3);
    const find=(rows:Record<string,string>,anchor:string)=>{const row=Object.values(rows).find(text=>text.includes('anchor_key: '+anchor+'\n'));assert.ok(row,anchor);return row;};
    assert.match(find(changed,'cleanup-deck'),/status: dismissed/);
    if(crash) assert.equal(find(changed,'cleanup-notes'),find(before,'cleanup-notes'));
    else assert.match(find(changed,'cleanup-notes'),/snoozed_until:/);
    assert.equal(find(changed,'cleanup-untouched'),find(before,'cleanup-untouched'),'unselected row unchanged');
    for(const anchor of ['cleanup-deck','cleanup-notes','cleanup-reply'])assert.equal(find(changed,anchor).match(/^created_at:.*$/m)?.[0],find(before,anchor).match(/^created_at:.*$/m)?.[0]);
    if(crash) {
      await app.close();app=undefined;
      await waitFor(()=>receiver!.child.exitCode!==null);assert.equal(receiver.child.exitCode,86);
      receiver=await startReceiver(join(folder,'receiver'));
      config.bridge.url=receiver.bridgeUrl;config.bridge.readBearer=receiver.readBearer;config.ownerLoop.bearer=receiver.bearer;
      writeFileSync(configPath,JSON.stringify(config),{mode:0o600});app=await mount();
    }
    phase='safe';phaseCalls=0;
    await app.gateway.chat.processMessage(adapter as never,thread,message('cleanup-safe','Hello'));
    await waitFor(()=>sent.includes('Hello safely.'));
    // Exercise notice-before-recovery deliberately; seeing the safe reply alone
    // does not mean its following notice has finished. Recovery-first is covered
    // separately and must not manufacture an obsolete uncertainty message.
    await waitFor(()=>sent.some(text=>text.startsWith('I couldn’t confirm whether the task change happened.')));
    await app.close();app=undefined;await stopReceiver(receiver.child);
    const journalForCadence=new DatabaseSync(join(folder,'state/operations.sqlite'));
    try {
      journalForCadence.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_loop_tool' AND status='unknown' AND json_extract(result_json,'$.args.operation')='cleanup'").run();
    } finally {journalForCadence.close();}
    receiver=await startReceiver(join(folder,'receiver'));
    config.bridge.url=receiver.bridgeUrl;config.bridge.readBearer=receiver.readBearer;config.ownerLoop.bearer=receiver.bearer;
    writeFileSync(configPath,JSON.stringify(config),{mode:0o600});
    hide=false;lose=false;app=await mount();
    await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE kind='owner_loop_tool' AND status='succeeded' AND json_extract(result_json,'$.args.operation')='cleanup'").get()));
    await waitFor(()=>sent.some(text=>crash ? /Not attempted\./.test(text) : /dismiss/i.test(text)&&/snooz/i.test(text)));
    assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the task change happened.')).length,1,'uncertainty notice is sent exactly once');
    assert.equal(sent.length,4,'one list, one uncertainty notice, one safe answer, one final outcome');
    assert.equal(sent[2],'Hello safely.');
    assert.match(sent[1]!,/^I couldn’t confirm whether the task change happened\./);
    assert.equal(applies,1);assert.ok(inspections>0);
    if(crash) {assert.match(sent.at(-1)!,/uncertain|unknown|unconfirmed/i);assert.match(sent.at(-1)!,/skip|not attempted/i);}
    else {assert.match(sent.at(-1)!,/dismiss/i);assert.match(sent.at(-1)!,/snooz/i);}
    assert.deepEqual(snapshot(),changed,'receipt recovery preserves canonical bytes');
    assert.equal(readFileSync(join(folder,'receiver/cleanup-writes.jsonl'),'utf8'),writes,'recovery never calls an original row writer again');
    const receipt=JSON.parse(String(observer.prepare("SELECT result_json FROM actions WHERE json_extract(result_json,'$.args.operation')='cleanup'").get()!.result_json)).receipt;
    assert.equal(receipt.items.length,3);
    if(crash) {assert.equal(receipt.outcome,'partial');assert.deepEqual(receipt.items.map((item:any)=>item.status),['applied','unknown','skipped']);}
    else {assert.equal(receipt.outcome,'complete');assert.ok(receipt.items.every((item:any)=>item.status==='applied'));}
    assert.ok(!find(changed,'cleanup-reply').includes('chased_count:'),'keep resets waiting-on chasing');
    assert.equal(observer.prepare("SELECT count(*) AS n FROM actions WHERE json_extract(result_json,'$.args.operation')='cleanup'").get()!.n,1);
  } finally {
    await app?.close(); observer?.close(); if(receiver)await stopReceiver(receiver.child);
    globalThis.fetch=originalFetch;rmSync(folder,{recursive:true,force:true});
  }
});
