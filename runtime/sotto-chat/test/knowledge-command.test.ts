import test from 'node:test';
import assert from 'node:assert/strict';
import {RequestContext} from '@mastra/core/request-context';
import {createStyleReadTool} from '../src/tools/style-read-tool.js';
import {STYLE_APPLY_EXECUTION_GUIDE, createKnowledgeCommand, parseKnowledgeCommand, parseGranolaHistoryCommand, parseStyleApplyCommand, isStyleApplyCommand} from '../src/tools/knowledge-command.js';
const revision = 'a'.repeat(64);
const authority = {version:2 as const,complete:true,revision,bridge_sources:[],google_sources:[]};
test('canonical knowledge scalar flags preserve query semantics and reject paths or ambiguous argv', () => {
  const args=['--person','Dana','--person-coverage','--editable-person','--topic','old funding','--relevant-days','365'];
  assert.deepEqual(parseKnowledgeCommand(args),args); assert.deepEqual(parseKnowledgeCommand([]),[]);
  assert.deepEqual(parseKnowledgeCommand(['--topic','HP']),['--topic','HP']);
  for(const args of [['--calendar','/tmp/input'],['--person','Dana','--person','Other'],['--person-coverage'],['--editable-person'],['--relevant-days','036'],['--relevant-days','366'],['--topic','bad\ntext'],['--person',' Dana'],['--person','\ud800'],['--person','x'.repeat(201)]])
    assert.throws(()=>parseKnowledgeCommand(args));
  assert.deepEqual(parseKnowledgeCommand(['--person','界'.repeat(200)]),['--person','界'.repeat(200)]);
});
test('pure read returns actual JSON with no mutation grant; stale authority or cancelled run discards result',async()=>{
  let current=true, change=false, contacts=0;
  const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>current,
    sourceAuthority:{read:async()=>({...authority,revision:change?'b'.repeat(64):revision})},fetch:async(_input,init)=>{
      contacts++; assert.deepEqual(JSON.parse(init!.body as string),{operation:'knowledge_query',arguments:{argv:['--person','Dana','--person-coverage']},source_revision:revision});
      return new Response(JSON.stringify({person_knowledge:{name:'Dana',facts:['old funding fact']},coverage:{graph:'found'}}));
    }});
  const invocation={id:'knowledge-query',args:['--person','Dana','--person-coverage'],mode:'chat' as const};
  const context={mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision};
  assert.equal((await command(invocation,context)).backend,'canonical-knowledge-receiver');
  current=false; await assert.rejects(command(invocation,context)); assert.equal(contacts,1);
  current=true;change=true;await assert.rejects(command(invocation,context));assert.equal(contacts,1);
  const abort=new AbortController();abort.abort();await assert.rejects(command(invocation,{...context,signal:abort.signal}));assert.equal(contacts,1);
});
test('source change and cancellation while receiver reads never reach attending result',async()=>{
  for(const cancel of [false,true]) {
    let current=true,revisionNow=revision;
    const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>current,
      sourceAuthority:{read:async()=>({...authority,revision:revisionNow})},fetch:async()=>{
        if(cancel)current=false;else revisionNow='b'.repeat(64);
        return new Response('{}');
      }});
    await assert.rejects(command({id:'knowledge-query',args:[],mode:'chat'},{mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision}));
  }
});

test('master get reads actual standing text without mutation or fresh source grants; bad argv/results refuse', async () => {
  let contacts = 0, authorityReads = 0;
  const standing = {prompt:'Actual master framing',text:'## Procedures\nNever book me Fridays.\nUse mornings instead.\n',chars:0,cap:8000};
  standing.chars = Array.from(standing.text).length;
  const command = createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>true,
    sourceAuthority:{read:async()=>{ authorityReads++; throw new Error('source authority unavailable'); }},fetch:async(_input,init)=>{
      contacts++;assert.deepEqual(JSON.parse(String(init!.body)),{operation:'master_file',arguments:{argv:['get']},source_revision:null});
      return new Response(JSON.stringify(standing));
    }});
  const context={mode:'chat',runId:'run',ownerRequestKey:'event'};
  assert.deepEqual(await command({id:'master-file',args:['get'],mode:'chat'},context),{backend:'canonical-knowledge-receiver',coverage:'owner_standing_context',result:standing});
  assert.deepEqual(await command({id:'master-file',args:['get'],mode:'chat'},{...context,sourceRevision:revision}),
    {backend:'canonical-knowledge-receiver',coverage:'owner_standing_context',result:standing});
  for(const args of [[],['get','other'],['append','Procedures','rule']]) await assert.rejects(command({id:'master-file',args,mode:'chat'},context));
  assert.equal(contacts,2);
  assert.equal(authorityReads,0,'owner-authored standing context does not depend on connector health');
});

test('master get withholds a late result after owner run cancellation without source probes', async () => {
  let current=true, authorityReads=0;
  const standing={prompt:'Owner rule',text:'## Procedures\nOwner rule',chars:24,cap:8000};
  const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',
    isCurrent:()=>current,sourceAuthority:{read:async()=>{authorityReads++;throw new Error('source unavailable');}},
    fetch:async()=>{current=false;return new Response(JSON.stringify(standing));}});
  await assert.rejects(command({id:'master-file',args:['get'],mode:'chat'},
    {mode:'chat',runId:'run',ownerRequestKey:'event'}));
  assert.equal(authorityReads,0);
});

test('master get does not wait for stalled source authority and refuses a stale run before contact', async () => {
  let authorityReads=0, contacts=0, current=true;
  const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',
    isCurrent:()=>current,sourceAuthority:{read:async()=>{authorityReads++;return new Promise<never>(()=>{});}},
    fetch:async()=>{contacts++;return new Response(JSON.stringify({text:'',chars:0,cap:8000,prompt:''}));}});
  const request={id:'master-file',args:['get'],mode:'chat' as const};
  const context={mode:'chat',runId:'run',ownerRequestKey:'event'};
  await Promise.race([command(request,context),new Promise((_,reject)=>setTimeout(()=>reject(new Error('master waited for source authority')),500))]);
  assert.equal(authorityReads,0);assert.equal(contacts,1);
  current=false;
  await assert.rejects(command(request,context));
  assert.equal(authorityReads,0);assert.equal(contacts,1);
});

test('loops catalog reads only exact scalar argv; source change, incomplete context and cancellation withhold results',async()=>{
  for (const gate of ['valid','changed','incomplete','cancel'] as const) {
    let current=true, revisionNow=revision, contacts=0;
    const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>current,
      sourceAuthority:{read:async()=>({...authority,complete:gate!=='incomplete',revision:gate==='incomplete'?null:revisionNow})},fetch:async(_input,init)=>{
        contacts++;assert.deepEqual(JSON.parse(String(init!.body)),{operation:'loops_query',arguments:{argv:['--parked']},source_revision:revision});
        if(gate==='changed')revisionNow='b'.repeat(64);if(gate==='cancel')current=false;
        return new Response(JSON.stringify({you_owe:[],waiting_on_them:[],parked:[],counts:{you_owe:0,waiting_on_them:0,parked:0},coverage:'currently_permitted_canonical_loops'}));
      }});
    const invocation={id:'loops-query',args:['--parked'],mode:'chat' as const};
    const context={mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision};
    if(gate==='valid')assert.equal((await command(invocation,context)).coverage,'currently_permitted_canonical_loops');
    else await assert.rejects(command(invocation,context));
    assert.equal(contacts,gate==='incomplete'?0:1);
    for(const args of [['--person','Dana'],['--parked','--parked'],['--input','/tmp/ledger']])await assert.rejects(command({...invocation,args},context));
    assert.equal(contacts,gate==='incomplete'?0:1);
  }
});

test('Granola command preserves bounded scalar grammar and current connector grant',async()=>{
  for (const args of [[],['--days','90'],['--meeting-id','meeting-one','--days','1']]) assert.deepEqual(parseGranolaHistoryCommand(args),args);
  for (const args of [['--days','0'],['--days','91'],['--days','01'],['--days','1','--days','2'],['--meeting-id',' bad'],['--meeting-id','bad\nvalue'],['--input','/tmp/file']]) assert.throws(()=>parseGranolaHistoryCommand(args));
  let granted=false,changed=false,contacts=0;
  const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>true,
    sourceAuthority:{read:async()=>({...authority,connector_sources:granted?['granola' as const]:[],revision:changed?'b'.repeat(64):revision})},
    fetch:async(_input,init)=>{contacts++;assert.deepEqual(JSON.parse(String(init!.body)),{operation:'granola_history',arguments:{argv:[]},source_revision:revision});return new Response(JSON.stringify({status:'found',days:14,meetings:[],coverage:'supplied_granola_window'}));}});
  const invocation={id:'granola-history',args:[],mode:'chat' as const},context={mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision};
  await assert.rejects(command(invocation,context));assert.equal(contacts,0);
  granted=true;assert.equal((await command(invocation,context)).coverage,'supplied_granola_window');
  granted=true;changed=true;await assert.rejects(command(invocation,context));assert.equal(contacts,1);
  granted=false;changed=false;await assert.rejects(command(invocation,context));assert.equal(contacts,1);
});

test('relationship importance uses only original scalar person read and withholds changed/incomplete/cancelled authority',async()=>{
 for(const gate of ['valid','changed','incomplete','cancel'] as const){
  let current=true,revisionNow=revision,contacts=0;
  const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>current,
   sourceAuthority:{read:async()=>({...authority,complete:gate!=='incomplete',revision:gate==='incomplete'?null:revisionNow})},fetch:async(_input,init)=>{
    contacts++;assert.deepEqual(JSON.parse(String(init!.body)),{operation:'relationship_importance',arguments:{argv:['--person','Dana Cedar']},source_revision:revision});
    if(gate==='changed')revisionNow='b'.repeat(64);if(gate==='cancel')current=false;
    return new Response(JSON.stringify({people:[{name:'Dana Cedar',tier:'vip',reason:'you marked this person VIP'}]}));}});
  const invocation={id:'relationship-importance',args:['--person','Dana Cedar'],mode:'chat' as const},context={mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision};
  if(gate==='valid')assert.equal((await command(invocation,context)).coverage,'saved_relationship_importance');else await assert.rejects(command(invocation,context));
  assert.equal(contacts,gate==='incomplete'?0:1);
  for(const args of [['--person'],['--person',' Dana'],['--person','Dana','--person','Other'],['--topic','work'],['--person','x'.repeat(201)],['--input','/tmp/history']])await assert.rejects(command({...invocation,args},context));
  assert.equal(contacts,gate==='incomplete'?0:1);
 }
});

test('triage command preserves original queue and coverage; rejects file argv, changed authority and unsafe group destinations',async()=>{
 const result={email:[{from:'Dana <dana@example.com>',subject:'Packet',threadId:'thread-1',channel:'email',last_snippet:'Please review',important:true,unread:true}],imessage:[{name:'Planning group',identifier:'',channel:'imessage',is_group_chat:true,last_snippet:'Dana: are you coming?',message_count:2}],whatsapp:[],counts:{email:1,imessage:1,whatsapp:0},coverage:{kind:'bounded_current_sources',local_since_hours:72,gmail_query:'newer_than:1d',sources:['gmail','imessage','whatsapp','contacts'].map(source=>({source,status:'ok',complete:true}))}};
 let revisionNow=revision,contacts=0,changed=false;
 const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>true,sourceAuthority:{read:async()=>({...authority,revision:revisionNow})},fetch:async(_input,init)=>{contacts++;assert.deepEqual(JSON.parse(String(init!.body)),{operation:'triage_queue',arguments:{argv:[]},source_revision:revision});if(changed)revisionNow='b'.repeat(64);return new Response(JSON.stringify(result));}});
 const context={mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision};
 assert.deepEqual((await command({id:'triage-queue',args:[],mode:'chat'},context)).result,result);
 await assert.rejects(command({id:'triage-queue',args:['--local','/tmp/unsafe'],mode:'chat'},context));assert.equal(contacts,1);
 Object.assign(result.email[0]!,{message_id:'exact-message'});
 assert.equal(((await command({id:'triage-queue',args:[],mode:'chat'},context)).result as any).email[0].message_id,'exact-message');
 Object.assign(result.email[0]!,{message_id:{private:'invalid'}});
 await assert.rejects(command({id:'triage-queue',args:[],mode:'chat'},context));
 Object.assign(result.email[0]!,{message_id:'x'.repeat(129)});
 await assert.rejects(command({id:'triage-queue',args:[],mode:'chat'},context));
 Object.assign(result.email[0]!,{message_id:'not/an/id'});
 await assert.rejects(command({id:'triage-queue',args:[],mode:'chat'},context));
 Object.assign(result.email[0]!,{message_id:''});
 assert.equal(((await command({id:'triage-queue',args:[],mode:'chat'},context)).result as any).email[0].message_id,'');
 result.imessage[0]!.identifier='+15555550123';await assert.rejects(command({id:'triage-queue',args:[],mode:'chat'},context));result.imessage[0]!.identifier='';
 result.coverage.sources[0]!.status='partial';await assert.rejects(command({id:'triage-queue',args:[],mode:'chat'},context));result.coverage.sources[0]!.status='ok';
 changed=true;await assert.rejects(command({id:'triage-queue',args:[],mode:'chat'},context));
});

const originalStyle = {guidance:'match this actual sample',bucket:'work_email',source:'confirmed',master:{avg_length:20,greetings:[],signoffs:[],uses_exclamation_marks:false,uses_em_dashes:false,uses_semicolons:false,uses_ellipsis:false,capitalization:'mixed',formality:'mixed',patterns:[]},has_person:false,tone_notes:['Be direct.']};
test('style apply preserves the original closed JSON request and exact path aliases',()=>{
 const request={recipient:'Alice',channel:'email',canonical_id:'c_abcdef',context:'work'};
 assert.deepEqual(parseStyleApplyCommand([JSON.stringify(request)]),[JSON.stringify(request)]);
 for(const value of [{...request,work:true},{...request,channel:'gmail'},{...request,recipient:' Alice'},{...request,canonical_id:'wrong'},{...request,recipient:'界'.repeat(201)}])assert.throws(()=>parseStyleApplyCommand([JSON.stringify(value)]));
 for(const argv of [[],['{}','{}'],['not json']])assert.throws(()=>parseStyleApplyCommand(argv));
 assert.ok(isStyleApplyCommand('/pack/_shared/scripts/style_apply.py','/pack'));assert.ok(!isStyleApplyCommand('/other/style_apply.py','/pack'));
 for(const id of ['$HOME/.hermes/skills/sotto/_shared/scripts/style_apply.py','~/.hermes/skills/sotto/_shared/scripts/style_apply.py'])assert.ok(!isStyleApplyCommand(id,'/pack'),'retired agent install path is not an alias');
});
test('style read returns original guidance only under current complete authority and closed result types',async()=>{
 for(const gate of ['valid','empty','changed','cancel','incomplete','malformed'] as const){
  let current=true,now=revision,contacts=0;
  const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'reader',skillRoot:'/pack',isCurrent:()=>current,
   sourceAuthority:{read:async()=>({...authority,complete:gate!=='incomplete',revision:now})},fetch:async(_input,init)=>{
    contacts++;assert.deepEqual(JSON.parse(String(init!.body)),{operation:'style_apply',arguments:{argv:['{"recipient":"Alice","channel":"email"}']},source_revision:revision});
    if(gate==='changed')now='b'.repeat(64);if(gate==='cancel')current=false;
    return new Response(JSON.stringify(gate==='malformed'?{...originalStyle,source:'invented',extra:'untrusted'}:gate==='empty'?{...originalStyle,master:{},source:'bucket',guidance:'No writing samples yet'}:originalStyle));
   }});
  const invocation={id:'/pack/_shared/scripts/style_apply.py',args:['{"recipient":"Alice","channel":"email"}'],mode:'chat' as const};
  const context={mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision};
  if(gate==='valid'||gate==='empty'){const result=await command(invocation,context);assert.deepEqual(result.result,gate==='empty'?{...originalStyle,master:{},source:'bucket',guidance:'No writing samples yet'}:originalStyle);assert.equal(result.coverage,'currently_permitted_style');}
  else await assert.rejects(command(invocation,context));
  assert.equal(contacts,gate==='incomplete'?0:1);
 }
});

test('saved relationship attention is closed, bounded and withheld on changed authority',async()=>{
 let changed=false,calls=0;
 let result:unknown={status:'available',freshness:'unknown',attention_queue:[{display_name:'Dana',queue_type:'waiting_on_you',reason:'Historical observation.'}],truncated:false};
 const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>true,sourceAuthority:{read:async()=>({...authority,revision:changed?'b'.repeat(64):revision})},fetch:async(_input,init)=>{
  calls++;assert.deepEqual(JSON.parse(init!.body as string),{operation:'relationship_attention',arguments:{argv:[]},source_revision:revision});return new Response(JSON.stringify(result));
 }});
 const invocation={id:'relationship-attention',args:[],mode:'chat' as const},context={mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision};
 assert.equal((await command(invocation,context)).coverage,'saved_relationship_attention');
 await assert.rejects(command({...invocation,args:['--person','Dana']},context));assert.equal(calls,1);
 for(const malformed of [{...result as object,freshness:'current'},{...result as object,attention_queue:Array(13).fill({display_name:'Dana',queue_type:'lapsed',reason:'old'})},{...result as object,unexpected:true}]){result=malformed;await assert.rejects(command(invocation,context));}
 result={status:'missing',freshness:'unknown',attention_queue:[],truncated:false};assert.equal((await command(invocation,context)).coverage,'saved_relationship_attention');
 const changing=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>true,sourceAuthority:{read:async()=>({...authority,revision:changed?'b'.repeat(64):revision})},fetch:async()=>{changed=true;return new Response(JSON.stringify(result));}});
 await assert.rejects(changing(invocation,context));
});

test('company selector preserves one literal name/domain and refuses mixed or unsafe flags',()=>{
 for(const name of ['Cedar Labs','cedar.example','李'.repeat(200)])assert.deepEqual(parseKnowledgeCommand(['--company',name]),['--company',name,'--company-page','first']);
 for(const argv of [['--company'],['--company',''],['--company',' Cedar'],['--company','x'.repeat(201)],['--company','Cedar','--person','Dana'],['--topic','tools','--company','Cedar'],['--company','bad\u0085name'],['--company','bad\u2028name']])assert.throws(()=>parseKnowledgeCommand(argv));
});

test('chat requests full saved company pages and accepts only versioned continuation cursors', () => {
  const base = ['--company', 'Cedar Labs'];
  const cursor = 'a'.repeat(64) + ':3000';
  assert.deepEqual(parseKnowledgeCommand(base), [...base, '--company-page', 'first']);
  assert.deepEqual(parseKnowledgeCommand([...base, '--company-page', cursor]), [...base, '--company-page', cursor]);
  assert.throws(() => parseKnowledgeCommand([...base, '--company-page', '../secrets']));
});

test('company result is one original saved packed record; malformed or stale results are withheld',async()=>{
 const identity={slug:'cedar-labs',name:'Cedar Labs',domain:'cedar.example',last_researched:'2026-10-17'};
 const valid={status:'saved',company_knowledge:{'cedar-labs':'Cedar Labs — Builds clinic tools.'},company_identity:identity,coverage:'Saved company record only; original sources were not searched.',next_cursor:null,truncated:false};
 let result:unknown=valid,changed=false,revisionNow=revision;
 const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>true,sourceAuthority:{read:async()=>({...authority,revision:revisionNow})},fetch:async()=>{if(changed)revisionNow='b'.repeat(64);return new Response(JSON.stringify(result));}});
 const invocation={id:'knowledge-query',args:['--company','cedar.example'],mode:'chat' as const},context={mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision};
 assert.deepEqual((await command(invocation,context)).result,valid);
 for(const bad of [{...identity,domain:'https://private.example/?data=secret'},{...identity,slug:'other-company'}]){
  result={...valid,company_identity:bad};await assert.rejects(command(invocation,context));
 }
 for(const status of ['missing','ambiguous','unavailable']){result={status,company_knowledge:{},truncated:false};assert.deepEqual((await command(invocation,context)).result,result);}
 for(const bad of [{...valid,company_knowledge:{}},{...valid,company_knowledge:{a:'A',b:'B'}},{status:'missing',company_knowledge:{},truncated:true},{...valid,fresh:true}]){result=bad;await assert.rejects(command(invocation,context));}
 result=valid;changed=true;await assert.rejects(command(invocation,context));
});

test('retune scan is closed read-only original shape and respects current authority bookends',async()=>{
 const valid={coverage:'currently_permitted_canonical_loops',stale_loops:[],mute_suggestions:[],current:{timezone:'America/Los_Angeles',mute_senders:[],mute_people:[],mute_sections:[],tone_notes:[],vip_people:[],nudge_snooze_until:'',nudge_budget:''},counts:{stale:0,mute_suggestions:0}};
 let result:unknown=valid,changed=false,calls=0,complete=true;
 const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>true,sourceAuthority:{read:async()=>({...authority,complete,revision:changed?'b'.repeat(64):revision})},fetch:async(_input,init)=>{calls++;assert.deepEqual(JSON.parse(String(init!.body)),{operation:'retune_scan',arguments:{argv:[]},source_revision:revision});return new Response(JSON.stringify(result));}});
 const invocation={id:'retune-scan',args:[],mode:'chat' as const},context={mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision};
 assert.deepEqual((await command(invocation,context)).result,valid);
 await assert.rejects(command({...invocation,args:['--all']},context));assert.equal(calls,1);
 for(const malformed of [{...valid,coverage:'all_loops'},{...valid,counts:{stale:1,mute_suggestions:0}},{...valid,mute_suggestions:[{person:'Dana'}]},{...valid,current:{...valid.current,new_setting:true}}]){result=malformed;await assert.rejects(command(invocation,context));}
 result=valid;complete=false;const before=calls;await assert.rejects(command(invocation,context));assert.equal(calls,before);
 complete=true;
 const changing=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',isCurrent:()=>true,sourceAuthority:{read:async()=>({...authority,revision:changed?'b'.repeat(64):revision})},fetch:async()=>{changed=true;return new Response(JSON.stringify(valid));}});
 await assert.rejects(changing(invocation,context));
});


test('trusted unattended loops read has no owner-event impersonation, rejects every other command and withholds changed/cancelled/incomplete authority', async()=>{
 for(const gate of ['valid','changed','cancel','incomplete'] as const){
  let revisionNow=revision,calls=0;
  const abort=new AbortController();
  const command=createKnowledgeCommand({mode:'unattended',bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'reader',
   sourceAuthority:{read:async()=>({...authority,complete:gate!=='incomplete',revision:revisionNow})},fetch:async(_input,init)=>{
    calls++;assert.deepEqual(JSON.parse(String(init!.body)),{operation:'loops_query',arguments:{argv:[]},source_revision:revision});
    if(gate==='changed')revisionNow='b'.repeat(64);if(gate==='cancel')abort.abort();
    return Response.json({you_owe:[],waiting_on_them:[],counts:{you_owe:0,waiting_on_them:0},coverage:'currently_permitted_canonical_loops'});
   }});
  const context={mode:'unattended',runId:'routine',sourceRevision:revision,signal:abort.signal};
  for(const id of ['master-file','knowledge-query','triage-queue','retune-scan','loops-resolve','schedule-wakeup','research','style-apply'])
   await assert.rejects(command({id,args:id==='master-file'?['get']:[],mode:'unattended'},context));
  await assert.rejects(command({id:'loops-query',args:[],mode:'chat'},context));
  await assert.rejects(command({id:'loops-query',args:[],mode:'unattended'},{...context,mode:'chat',ownerRequestKey:'forged'}));
  await assert.rejects(command({id:'loops-query',args:[],mode:'unattended'},{...context,runId:undefined}));
  assert.equal(calls,0);
  const pending=command({id:'loops-query',args:[],mode:'unattended'},context);
  if(gate==='valid')assert.equal((await pending).coverage,'currently_permitted_canonical_loops');else await assert.rejects(pending);
  assert.equal(calls,gate==='incomplete'?0:1);
 }
});


test('typed style read preserves receiver authority and does not require model JSON encoding', async () => {
 for (const gate of ['valid','revoked','cancelled','wrong-mode','model-authority'] as const) {
  let calls=0, current=true, rev=revision;
  const command=createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',
   isCurrent:(run,event)=>current && run==='run' && event==='event',
   sourceAuthority:{read:async()=>({...authority,revision:rev})},fetch:async(_input,init)=>{
    calls++;
    assert.deepEqual(JSON.parse(String(init!.body)),{operation:'style_apply',arguments:{argv:[JSON.stringify({recipient:'Dana "DJ" Reed',channel:'email'})]},source_revision:revision});
    if(gate==='revoked')rev='b'.repeat(64);
    if(gate==='cancelled')current=false;
    return Response.json(originalStyle);
   }});
  const requestContext=new RequestContext();
  for(const [key,value] of Object.entries({mode:gate==='wrong-mode'?'unattended':'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision}))requestContext.set(key,value);
  const execute=createStyleReadTool(command).execute!;
  const input={recipient:'Dana "DJ" Reed',channel:'email' as const,...(gate==='model-authority'?{sourceRevision:revision}:{})};
  const promise=execute(input,{requestContext,observe:{span:async <T>(_name:string,fn:()=>Promise<T>|T)=>fn(),log:()=>{}}});
  if(gate==='valid')assert.deepEqual((await promise as {result:unknown}).result,originalStyle);
  else if(gate==='model-authority')assert.match(JSON.stringify(await promise), /validation|invalid|unrecognized/i);
  else await assert.rejects(promise,gate);
  assert.equal(calls,['wrong-mode','model-authority'].includes(gate)?0:1);
 }
});


test('topic-only outcome read preserves recorded evidence and current source authority', async () => {
  let revisionNow = revision, contacts = 0;
  const result = {person_knowledge: {}, obligation_history: {status: 'matched', omitted: 0,
    absence_is_authoritative: false, items: [{anchor_key: 'orion', canonical_id: 'c_abc111',
      contact_identifier: 'alex@example.test', status: 'resolved', completion_basis: 'source_evidence',
      source_refs: [{sourceType: 'gmail', sourceId: 'original'}],
      resolution_source_refs: [{sourceType: 'gmail', sourceId: 'sent'}]}]}};
  const command = createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp', readBearer:'read-only',
    isCurrent:()=>true, sourceAuthority:{read:async()=>({...authority,revision:revisionNow})},
    fetch:async(_url, init)=>{
      contacts++;
      assert.deepEqual(JSON.parse(String(init!.body)), {operation:'knowledge_query',
        arguments:{argv:['--topic','Orion']},source_revision:revision});
      return Response.json(result);
    }});
  const invocation = {id:'knowledge-query',args:['--topic','Orion'],mode:'chat' as const};
  const context = {mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision};
  assert.deepEqual((await command(invocation,context)).result,result);
  revisionNow = 'b'.repeat(64);
  await assert.rejects(command(invocation,context));
  assert.equal(contacts,1);
});

test('facts-only narrows person coverage and rejects edit or cohort scopes', () => {
  const args=['--person','Dana','--person-coverage','--facts-only','--topic','sensors'];
  assert.deepEqual(parseKnowledgeCommand(args),args);
  for(const invalidArgs of [[],['--person','Dana'],['--person-coverage'],
    ['--person','Dana','--person-coverage','--editable-person'],
    ['--person','Dana','--person-coverage','--relevant-days','7'],['--company','Cedar']]) {
    assert.throws(()=>parseKnowledgeCommand([...invalidArgs,'--facts-only']));
  }
});


test('facts-only command preserves receiver facts, evidence and coverage without obligation history', async () => {
  const argv = ['--person','Dana','--person-coverage','--facts-only'];
  const result = {person_knowledge:{c_dana:'Dana\n= Builds coastal sensors'},
    fact_evidence:{items:[{canonical_id:'c_dana',fact_id:'f_sensors',text:'Builds coastal sensors',
      source_ref:'gmail:sensors'}],omitted:0},
    coverage:{graph:'found',historical_sources:[{source:'gmail',learning:{status:'partial'}}]}};
  let contacts = 0;
  const command = createKnowledgeCommand({bridgeUrl:'http://127.0.0.1:8080/mcp',readBearer:'read-only',
    isCurrent:()=>true,sourceAuthority:{read:async()=>authority},fetch:async(_url,init)=>{
      contacts++;
      assert.deepEqual(JSON.parse(String(init!.body)),{operation:'knowledge_query',
        arguments:{argv},source_revision:revision});
      return Response.json(result);
    }});
  assert.deepEqual(await command({id:'knowledge-query',args:argv,mode:'chat'},
    {mode:'chat',runId:'run',ownerRequestKey:'event',sourceRevision:revision}),
    {backend:'canonical-knowledge-receiver',coverage:'canonical_knowledge_query',result});
  assert.equal(contacts,1);
  assert.equal('obligation_history' in result,false);
});


test('draft style guidance separates requested future intent from invented owner commitments',()=>{
 assert.match(STYLE_APPLY_EXECUTION_GUIDE,/Future commitments also need an explicit owner request or statement/);
 assert.match(STYLE_APPLY_EXECUTION_GUIDE,/ask the owner one useful question/);
 assert.match(STYLE_APPLY_EXECUTION_GUIDE,/Do not present an unprompted Accept\/Decline menu/);
 assert.doesNotMatch(STYLE_APPLY_EXECUTION_GUIDE,/otherwise use prospective wording|show both options/);
});
