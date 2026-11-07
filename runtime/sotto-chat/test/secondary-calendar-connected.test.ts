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
import {calendarReviewText} from '../src/tools/owner-calendar-contract.js';
import {startConfiguredRuntime} from '../src/bootstrap/main.js';

async function startReceiver(folder: string, rsvp: boolean, move = false) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')],
    {stdio: ['pipe', 'pipe', 'pipe'], env: {PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_CALENDAR_MOVE: '1', SOTTO_TEST_CALENDAR_ID: 'team+planning@example.com', SOTTO_TEST_CALENDAR_RSVP: rsvp ? 'single' : undefined, SOTTO_TEST_CALENDAR_COMBINED: rsvp || move ? undefined : 'single', SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
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




// External model and Calendar transport are synthetic. Default packaged bootstrap,
// admission, original reader/writer, accepted review, consent, and recovery stay real.
for (const scenario of ['combined', 'rsvp', 'unknown', 'move'] as const) test(`default bootstrap known secondary calendar: ${scenario}`, async () => {
  const originalFetch=globalThis.fetch;
  const folder=mkdtempSync(join(tmpdir(),'sotto-secondary-calendar-'));
  const calendarId='team+planning@example.com';
  let receiver:Awaited<ReturnType<typeof startReceiver>>|undefined;
  let app:Awaited<ReturnType<typeof startConfiguredRuntime>>|undefined;
  let observer:DatabaseSync|undefined;
  let phase:'review'|'confirm'|'safe'='review', phaseCalls=0, totalCalls=0;
  let applies=0, inspections=0, lose=true, hide=true, exactReview='';
  const sent:string[]=[];
  const readStatuses:number[]=[];
  const waitFor=async(predicate:()=>boolean)=>{
    const until=Date.now()+15000;
    while(!predicate()) {
      if(Date.now()>until)throw Error('Calendar did not settle '+JSON.stringify({phase,phaseCalls,applies,sent,actions:observer?.prepare('SELECT kind,status,result_json FROM actions').all()}));
      await new Promise(resolve=>setTimeout(resolve,10));
    }
  };
  try {
    // This test selects a known secondary calendar; keep the real read-selection
    // guard active without adding an unrelated CalendarList transport fixture.
    const settingsDirectory=join(folder,'receiver','config');
    mkdirSync(settingsDirectory,{recursive:true,mode:0o700});
    writeFileSync(join(settingsDirectory,'settings.json'),JSON.stringify({google_calendar_selection:{
      mode:'selected',calendar_ids:['primary',calendarId]}}),{mode:0o600});
    receiver=await startReceiver(join(folder,'receiver'),scenario==='rsvp',scenario==='move');
    const thread = receiver.identity.route_id;
    const media = join(folder, 'media'); mkdirSync(media, {mode:0o700});
    const config = {version:1,channel:'photon',stateDirectory:join(folder,'state'),mediaRoot:media,
      bearerToken:'synthetic-calendar-at-least-32-character-token',port:0,tenantId:'tenant',
      owner:{accountId:'account',ownerId:receiver.identity.owner_id},lineKey:'line',target:'photon',providerThreadId:thread,
      credentials:{mode:'cloud',photonProjectId:'fixture-project',photonProjectSecret:'fixture-secret'},
      instructions:'Use the original Schedule skill and existing Calendar tools.',
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

    const external:typeof fetch=async(input,init)=>{
      const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
      const body=JSON.parse(String(init?.body??'{}'));
      if(url.origin!=='https://proxy.example') {
        assert.equal(url.origin,new URL(receiver!.bridgeUrl).origin,'unexpected provider contact');
        const calendar=url.pathname==='/runtime/owner-calendar-tool';
        if(calendar&&body.operation==='inspect'){inspections++;if(hide)return new Response('{}',{status:503});}
        if(calendar&&body.operation==='apply')applies++;
        const response=await originalFetch(input,init);
        if(url.pathname==='/runtime/google-read') {
          readStatuses.push(response.status);
          assert.equal(response.status,200,await response.clone().text());
          const read=await response.clone().json();
          assert.equal(read.item.calendar_id,calendarId);
          assert.equal(read.item.id,'cedar_occurrence');
        }
        if(calendar&&body.operation==='preview') {
          const preview=await response.clone().json();
          assert.equal(preview.status,'preview',JSON.stringify(preview));
          exactReview=calendarReviewText(preview);
        }
        if(calendar&&body.operation==='apply'&&lose) {
          assert.equal(response.status,200,await response.clone().text());
          assert.equal((await response.clone().json()).status,scenario==='unknown'?'unknown':'committed');
          throw Error('synthetic lost Calendar response');
        }
        return response;
      }
      phaseCalls++;totalCalls++;assert.ok(totalCalls<=10,'bounded model calls');
      let call:{name:string;args:unknown}|undefined;
      let answer='Hello safely.';
      if(phase==='review') {
        assert.ok(phaseCalls<=4);
        if(phaseCalls===1)call={name:'skill',args:{name:'sotto-schedule'}};
        else if(phaseCalls===2)call={name:'calendar_get',args:{event_id:'cedar_occurrence',calendar_id:calendarId}};
        else if(phaseCalls===3) {
          assert.ok(JSON.stringify(body.messages).includes(calendarId),'reader echoes exact calendar');
          call=scenario==='move'?{name:'run_sotto_script',args:{id:'google-action',args:['calendar-update','--event-id','cedar_occurrence','--calendar',calendarId,'--fields-json',JSON.stringify({start:{dateTime:'2026-10-10T12:00:00-07:00'},end:{dateTime:'2026-10-10T13:00:00-07:00'}})]}}:scenario==='rsvp'?{name:'run_sotto_script',args:{id:'google-action',args:['calendar-rsvp','--event-id','cedar_occurrence','--response','accepted','--calendar',calendarId,'--comment','  Join after 3pm.\nThanks  ']}}:
            {name:'calendar_move_update_review',args:{event_id:'cedar_occurrence',calendar_id:calendarId,start:'2026-10-10T12:00:00-07:00',end:'2026-10-10T13:00:00-07:00',fields:{summary:'Cedar planning',location:'',description:'New agenda\nExact second line'}}};
        } else {assert.ok(exactReview);answer=exactReview;}
      } else if(phase==='confirm') {
        assert.ok(phaseCalls<=2);
        if(phaseCalls===1)call=scenario==='move'?{name:'run_sotto_script',args:{id:'google-action',args:['calendar-move-confirm']}}:scenario==='rsvp'?{name:'run_sotto_script',args:{id:'google-action',args:['calendar-rsvp-confirm']}}:{name:'calendar_move_update_confirm',args:{}};
        else answer='Pending confirmation.';
      }
      return new Response(JSON.stringify({id:'calendar-'+totalCalls,object:'chat.completion',created:1,model:'chat-model',
        choices:[{index:0,message:call?{role:'assistant',content:null,tool_calls:[{id:'calendar-call-'+totalCalls,type:'function',function:{name:call.name,arguments:JSON.stringify(call.args)}}]}:{role:'assistant',content:answer},finish_reason:call?'tool_calls':'stop'}],
        usage:{prompt_tokens:5,completion_tokens:2,total_tokens:7}}),{headers:{'content-type':'application/json'}});
    };
    const mount = () => startConfiguredRuntime(configPath,{fetch:external,createAdapter:()=>adapter as never,
      createTransport:()=>({send:async request=>{sent.push(request.text??'');return {acceptance:'accepted',providerMessageIds:['calendar-accepted-'+sent.length]};}}),
      openStateDatabase:()=>new DatabaseSync(':memory:'),listenApplication:async()=>32134});
    const message=(id:string,text:string)=>new Message({id,threadId:thread,text,formatted:{type:'root',children:[]},raw:{id,timestamp:new Date()},author:{userId:receiver!.identity.owner_id,userName:'owner',fullName:'Owner',isBot:false,isMe:false},metadata:{dateSent:new Date(),edited:false},attachments:[]});
    globalThis.fetch=external;
    app=await mount(); observer=new DatabaseSync(join(folder,'state/operations.sqlite'),{readOnly:true});

    const primary=readFileSync(join(folder,'receiver/calendar-primary.json'),'utf8');
    await app.gateway.chat.processMessage(adapter as never,thread,message('calendar-proposal',scenario==='move'?`Move Cedar to noon until 1pm on October 10 on calendar ${calendarId}`:scenario==='rsvp'?`Accept the Cedar invitation on calendar ${calendarId}`:`Move Cedar to noon until 1pm on October 10 and update title, location and agenda on calendar ${calendarId}`));
    await waitFor(()=>sent.length===1);
    await waitFor(()=>Boolean(observer!.prepare("SELECT e.id FROM inbound_events e JOIN prepared_replies r ON r.event_id=e.id JOIN dispatches d ON d.id=r.dispatch_id WHERE e.source_id='calendar-proposal' AND e.outcome='resolved' AND d.status='accepted'").get()));
    assert.equal(sent[0],exactReview);
    assert.ok(sent[0]!.includes('Calendar: '+calendarId),'full accepted review identifies exact calendar');
    assert.equal(applies,0);assert.equal(existsSync(join(folder,'receiver/calendar-provider.jsonl')),false);
    assert.deepEqual(readStatuses,[200],'the current exact-calendar read must succeed before a review');
    const reads=readFileSync(join(folder,'receiver/calendar-reads.jsonl'),'utf8').trim().split('\n').map(line=>JSON.parse(line));
    assert.deepEqual(reads.map(read=>[read.operation,read.calendar_id]),[['calendar_get',calendarId]]);
    // The distinct confirmation may arrive after a clean restart.
    await app.close();app=undefined;app=await mount();
    if(scenario==='unknown')writeFileSync(join(folder,'receiver/calendar-provider-unknown'),'1');
    phase='confirm';phaseCalls=0;
    await app.gateway.chat.processMessage(adapter as never,thread,message('calendar-confirm','yes'));
    await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE kind='owner_calendar_tool' AND status='unknown'").get()));
    assert.equal(applies,1);
    const uncertainCalendar = 'I couldn’t confirm whether the calendar change happened.';
    // The bounded pending-outcome notice may already have been accepted while
    // this test observes the durable unknown action. It is never a success ACK.
    assert.ok(sent.slice(1).every(text=>text.startsWith(uncertainCalendar)),
      'lost response cannot claim Calendar success: '+JSON.stringify(sent));
    assert.ok(sent.length<=2,'at most one early uncertainty notice');
    const logPath=join(folder,'receiver/calendar-provider.jsonl');
    const writes=readFileSync(logPath,'utf8');const calls=writes.trim().split('\n').map(line=>JSON.parse(line));
    assert.equal(calls.length,1);assert.equal(calls[0].args.calendarId,calendarId);assert.equal(calls[0].args.eventId,'cedar_occurrence');
    assert.equal(calls[0].headers['If-Match'],'"version-1"');assert.equal(calls[0].args.sendUpdates,'all');
    assert.equal(readFileSync(join(folder,'receiver/calendar-primary.json'),'utf8'),primary,'same-ID primary event untouched');
    const changed=readFileSync(join(folder,'receiver/calendar-event.json'),'utf8');const event=JSON.parse(changed);
    if(scenario==='rsvp'){assert.equal(event.attendees[1].responseStatus,'accepted');assert.equal(event.attendees[1].comment,'  Join after 3pm.\nThanks  ');assert.deepEqual(event.attendees[0],{email:'dana@example.com'});assert.match(exactReview,/Your comment before: None/);assert.match(exactReview,/Your comment after:   Join after 3pm\.\nThanks  /);assert.deepEqual(Object.keys(calls[0].args.body),['attendees']);}
    else if(scenario==='move'){assert.equal(event.start.dateTime,'2026-10-10T12:00:00-07:00');assert.equal(event.end.dateTime,'2026-10-10T13:00:00-07:00');assert.equal(event.summary,'Cedar *review*');assert.equal(event.location,'Cedar office');assert.deepEqual(Object.keys(calls[0].args.body).sort(),['end','start']);assert.deepEqual(event.conferenceData,{conferenceId:'untouched'});assert.deepEqual(event.attendees,[{email:'dana@example.com'}]);}
    else {assert.equal(event.summary,'Cedar planning');assert.equal(event.start.dateTime,'2026-10-10T12:00:00-07:00');assert.equal(event.location,'');assert.equal(event.description,'New agenda\nExact second line');}
    phase='safe';phaseCalls=0;
    await app.gateway.chat.processMessage(adapter as never,thread,message('calendar-safe','Hello'));
    await waitFor(()=>sent.includes('Hello safely.')&&sent.some(text=>text.startsWith(uncertainCalendar)));
    assert.equal(sent.filter(text=>text==='Hello safely.').length,1,'safe chat still answers while Calendar is held');
    assert.equal(sent.filter(text=>text.startsWith(uncertainCalendar)).length,1,'one honest uncertainty notice');
    await app.close();app=undefined;await stopReceiver(receiver.child);
    const writable=new DatabaseSync(join(folder,'state/operations.sqlite'));
    writable.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE kind='owner_calendar_tool' AND status='unknown'").run();
    writable.close();
    receiver=await startReceiver(join(folder,'receiver'),scenario==='rsvp',scenario==='move');
    config.bridge.url=receiver.bridgeUrl;config.bridge.readBearer=receiver.readBearer;config.ownerLoop.bearer=receiver.bearer;
    writeFileSync(configPath,JSON.stringify(config),{mode:0o600});hide=false;lose=false;app=await mount();
    await waitFor(()=>inspections>0);
    if(scenario!=='unknown') {
      const recoveredAck=scenario==='rsvp'?'Accepted that invitation. Notifications were requested.'
        :scenario==='move'?'Meeting moved. Guest notifications were requested.'
        :'Updated the meeting time and details. Guest notifications were requested.';
      await waitFor(()=>sent.includes(recoveredAck));
      assert.equal(sent.filter(text=>text===recoveredAck).length,1,'late Calendar proof acknowledges once');
      assert.equal(observer.prepare("SELECT outcome FROM inbound_events WHERE source_id='calendar-confirm'").get()!.outcome,'resolved');
      await app.gateway.chat.processMessage(adapter as never,thread,message('calendar-safe-restarted','Hello again'));
      await waitFor(()=>Boolean(observer!.prepare("SELECT id FROM actions WHERE kind='owner_calendar_tool' AND status='succeeded'").get()));
      await waitFor(()=>sent.length===5);
      assert.equal(sent[4],'Hello safely.');
      assert.equal(sent.filter(text=>text===recoveredAck).length,1,'safe chat does not repeat recovered acknowledgement');
    } else {
      await app.gateway.chat.processMessage(adapter as never,thread,message('calendar-safe-restarted','Hello again'));
      await waitFor(()=>sent.length===4);
      assert.equal(sent[3],'Hello safely.');
      assert.equal(observer.prepare("SELECT count(*) n FROM actions WHERE kind='owner_calendar_tool' AND status='unknown'").get()!.n,1);
    }
    assert.equal(applies,1);assert.equal(readFileSync(logPath,'utf8'),writes,'restart only inspects; no second PATCH');
    assert.equal(readFileSync(join(folder,'receiver/calendar-event.json'),'utf8'),changed);
    assert.equal(readFileSync(join(folder,'receiver/calendar-primary.json'),'utf8'),primary);
  } finally {
    await app?.close();observer?.close();if(receiver)await stopReceiver(receiver.child);
    globalThis.fetch=originalFetch;rmSync(folder,{recursive:true,force:true});
  }
});
