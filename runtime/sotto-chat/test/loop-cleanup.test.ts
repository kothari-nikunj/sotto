import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createOwnerTaskToolClient} from '../src/tools/owner-task-tool-client.js';
import {createOwnerTaskExecution} from '../src/tools/owner-task-execution.js';
import {createTaskCommand} from '../src/tools/task-command.js';
import {reminderHandoff} from '../src/agent/owner-retune.js';
import {cleanupListSchema,cleanupListHash,cleanupListText,cleanupIntentMatches,cleanupSelectionsMatch,cleanupItemsSchema,taskReceiptMatches,ownerTaskToolReceiptSchema,cleanupAcknowledgement,type CleanupList,type OwnerTaskToolArgs} from '../src/tools/owner-task-tool-contract.js';
const revision='a'.repeat(64);
const row={index:1,anchor_key:'cedar-one',name:'Dana 李',what:'Return the Cedar deck',direction:'you_owe' as const,age_days:7,times_surfaced:3,surface_count_provenance:'accepted_delivery' as const,overdue:false,deadline:null,chased_count:0,chased_out:false,suggestion:'do it or dismiss' as const,loop_version:'b'.repeat(64),loop_identity:'c'.repeat(64)};
const list:CleanupList={operation:'cleanup_list',source_revision:revision,rows:[row,{...row,index:2,anchor_key:'cedar-two',direction:'waiting_on_them'}],total:2,remaining:0,review_hash:''};list.review_hash=cleanupListHash(list);
const items=[{index:1,action:'dismiss' as const,correction:'Dismiss item 1'},{index:2,action:'snooze' as const,correction:'snooze item 2 for 7 days',days:7}];
const identity={tenant_id:'tenant',channel:'photon' as const,account_id:'account',owner_id:'owner',line_key:'line',target:'photon' as const,route_mode:'fixed' as const,route_id:'thread',project_fingerprint:revision};
const context={threadId:'thread',destination:'photon',lineKey:'line'};
const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId,text});
function fixture(){
 const folder=mkdtempSync(join(tmpdir(),'cleanup-unit-'));const journal=new OperationalJournal(join(folder,'journal.sqlite'));let phase='list',applies=0,previews=0,losePreview=false,partial=false,failApply=false,failWriter=false;const sent:string[]=[];
 const client=createOwnerTaskToolClient({identity,bridgeUrl:'http://127.0.0.1:8080/mcp',bearer:revision,fetch:async(_url,init)=>{const body=JSON.parse(String(init?.body));if(body.operation==='preview'){previews++;if(losePreview)throw Error('lost readonly preview');return new Response(JSON.stringify({version:1,status:'preview',event_id:body.event_id,payload_hash:body.payload_hash,review:list}));}if(body.operation==='apply')applies++;assert.equal(body.args.operation,'cleanup');if(failWriter)return new Response(JSON.stringify(body.operation==='inspect'?{version:1,status:'unknown'}:{version:1,status:'unknown',cleanup_stage:'writer',cleanup_category:'io'}));if(failApply)return new Response(JSON.stringify({cleanup_stage:'receiver_perform',cleanup_category:'unavailable',cleanup_http_status:503}),{status:503});const outcomes=body.args.items.map((item:typeof items[number],i:number)=>({...{index:item.index,action:item.action,anchor_key:list.rows[item.index-1]!.anchor_key},...(partial&&i===1?{status:'unknown'}:{status:'applied',post_digest:revision,loop_status:item.action==='dismiss'?'dismissed':'open',...(item.action==='snooze'?{snoozed_until:'2026-10-13'}:{})})}));return new Response(JSON.stringify({version:1,status:'committed',action_id:body.action_id,event_id:body.event_id,payload_hash:body.payload_hash,args_hash:journal.getAction(body.action_id)!.payload_hash,operation:'cleanup',review_hash:list.review_hash,outcome:partial?'partial':'complete',items:outcomes}));}});
 client.readLoops=async()=>({coverage:'currently_permitted_canonical_loops',you_owe:[],waiting_on_them:[],parked:[],counts:{you_owe:0,waiting_on_them:0,parked:0}});
 client.readback=async()=>({you_owe:0,waiting_on_them:0,source_revision:revision});
 const execution=createOwnerTaskExecution({client,journal:()=>journal,sourceAuthority:{read:async()=>({version:2,complete:true,revision,bridge_sources:[],google_sources:[]})}});const command=createTaskCommand(execution);
 const coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel:'photon',accountId:'account',ownerId:'owner'},ownerTaskTool:client,model:{generate:async request=>{const authority={mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerTask:request.allowOwnerTask,sourceRevision:revision,signal:request.signal};if(phase==='list'){const result=await command({id:'retune-apply',args:['list']},authority);assert.equal(result.result.status,'proposed');return cleanupListText(list);}if(phase==='cleanup'){try{await command({id:'retune-apply',args:['cleanup',JSON.stringify({items})]},authority);}catch(error){if(failApply||failWriter){assert.match(String(error),new RegExp(failWriter?'writer':'receiver_perform'));return String(error);}throw error;}}return 'safe';}},postPrepared:async request=>{sent.push(request.text);return{acceptance:'accepted',messageId:'reply-'+sent.length};}});
 return {folder,journal,client,execution,coordinator,sent,get applies(){return applies;},get previews(){return previews;},set phase(value:string){phase=value;},set losePreview(value:boolean){losePreview=value;},set partial(value:boolean){partial=value;},set failApply(value:boolean){failApply=value;},set failWriter(value:boolean){failWriter=value;},close:async()=>{await coordinator.close();journal.close();rmSync(folder,{recursive:true,force:true});}};
}
test('cleanup list canonical render/hash and row bounds bind exact numbered map',()=>{
 assert.doesNotThrow(()=>cleanupListSchema.parse(list));assert.match(cleanupListText(list),/1\. Dana 李 — Return the Cedar deck\n   You owe/);assert.match(cleanupListText(list),/2\..*\n   Waiting on them/);
 const escaped={...list,rows:[{...row,name:'Dana\n2. Injected\u0085'}],total:1};assert.ok(cleanupListText(escaped).includes('Dana\\u000a2. Injected\\u0085'));assert.ok(!cleanupListText(escaped).includes('Dana\n2.'));
 assert.notEqual(cleanupListHash({...list,rows:[{...row,what:'Changed'}]}),list.review_hash);
 assert.throws(()=>cleanupListSchema.parse({...list,rows:[row,row]}));assert.throws(()=>cleanupListSchema.parse({...list,remaining:1}));
});
test('closed owner clauses bind action index and decimal days, never yes/all/inferred choices',()=>{
 for(const [correction,index,action,days] of [['dismiss the first.',1,'dismiss',undefined],['keep item #2',2,'keep',undefined],['SNOOZE the second for 7 days',2,'snooze',7]] as const){assert.ok(cleanupIntentMatches({index,action,correction,...(days?{days}:{})},correction));}
 for(const correction of ['Yes','clean everything','dismiss 2','do not dismiss 1','dismiss 1 and keep 2'])assert.equal(cleanupIntentMatches({...items[0]!,correction},correction),false);
 assert.equal(cleanupIntentMatches({...items[1]!,correction:'snooze second for seven days'},'snooze second for seven days'),false);
 assert.throws(()=>cleanupItemsSchema.parse([items[0],items[0]]));
});
test('accepted list then one explicit multi-item event injects exact proof and returns honest ACK',async()=>{const f=fixture();try{assert.equal((await f.coordinator.receive(inbound('list','Show stale loops'),context)).status,'accepted');assert.equal(f.applies,0);f.phase='cleanup';assert.equal((await f.coordinator.receive(inbound('choice','Dismiss item 1 and snooze item 2 for 7 days'),context)).status,'accepted');assert.equal(f.applies,1);assert.match(f.sent[1]!,/1\. Dismissed/);assert.match(f.sent[1]!,/2\. Snoozed until 2026-10-13/);assert.equal((await f.coordinator.receive(inbound('choice','Dismiss item 1 and snooze item 2 for 7 days'),context)).status,'duplicate');assert.equal(f.applies,1);}finally{await f.close();}});
test('bare yes and intervening owner turn cannot consume old cleanup list',async()=>{for(const between of [false,true]){const f=fixture();try{await f.coordinator.receive(inbound('list','Show stale loops'),context);if(between){f.phase='safe';await f.coordinator.receive(inbound('between','Hello'),context);}f.phase='cleanup';await f.coordinator.receive(inbound('choice',between?'Dismiss item 1 and snooze item 2 for 7 days':'Yes'),context);assert.equal(f.applies,0);}finally{await f.close();}}});
test('lost readonly list stays pending and makes no effect',async()=>{const f=fixture();try{f.losePreview=true;await f.coordinator.receive(inbound('list','Show stale loops'),context);assert.equal(f.applies,0);assert.equal(f.journal.db.prepare('SELECT status FROM actions').get()!.status,'pending');}finally{await f.close();}});
test('partial receipt binds every target and reports unknown without all-done claim',async()=>{const f=fixture();try{await f.coordinator.receive(inbound('list','Show stale loops'),context);f.partial=true;f.phase='cleanup';await f.coordinator.receive(inbound('choice','Dismiss item 1 and snooze item 2 for 7 days'),context);assert.match(f.sent[1]!,/Outcome unconfirmed; this item may have changed/);const row=f.journal.db.prepare("SELECT * FROM actions ORDER BY rowid DESC LIMIT 1").get()!;const saved=f.journal.readOwnerTaskToolAction(row.id as string,{channel:'photon',accountId:'account',ownerId:'owner',lineKey:'line',scopeHash:f.client.scopeHash});assert.equal(saved.receipt?.operation,'cleanup');const receipt=ownerTaskToolReceiptSchema.parse(saved.receipt);assert.ok(taskReceiptMatches(receipt,saved.request.args));if(receipt.operation==='cleanup'){assert.equal(taskReceiptMatches({...receipt,items:receipt.items.map(item=>({...item,anchor_key:'wrong'}))},saved.request.args),false);assert.ok(!cleanupAcknowledgement(receipt).includes('all done'));}}finally{await f.close();}});

test('cleanup consent covers the whole owner message',()=>{
 const choices=[{index:1,action:'dismiss' as const,correction:'Dismiss item 1'},{index:2,action:'snooze' as const,correction:'snooze item 2 for 7 days',days:7}];
 assert.ok(cleanupSelectionsMatch(choices,'Dismiss item 1 and snooze item 2 for 7 days.'));
 for(const text of ["Don't Dismiss item 1 and snooze item 2 for 7 days.",'Dismiss item 1 and snooze item 2 for 7 days?','Dismiss item 1 and snooze item 2 for 7 days and send email','Dismiss item 1; Dismiss item 1 and snooze item 2 for 7 days'])assert.equal(cleanupSelectionsMatch(choices,text),false);
 assert.equal(cleanupIntentMatches({index:1,action:'dismiss',correction:'dismiss 01'},'dismiss 01'),false);
 assert.equal(cleanupIntentMatches({index:1,action:'snooze',correction:'snooze 1 for 07 days',days:7},'snooze 1 for 07 days'),false);
});

test('cleanup matches the shared Unicode canonical vector',()=>{
 const review=cleanupListSchema.parse({"operation": "cleanup_list", "source_revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "rows": [{"index": 1, "anchor_key": "cedar-🗓", "name": "Dana 李\nSaved", "what": "Return Café deck", "direction": "you_owe", "age_days": 7, "times_surfaced": 3, "surface_count_provenance": "accepted_delivery", "overdue": true, "deadline": "2026-10-01", "chased_count": 2, "chased_out": true, "suggestion": "chased twice — nudge again or drop", "loop_version": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "loop_identity": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}], "total": 2, "remaining": 1, "review_hash": "f546b0fedbddaacf33a5e66deb675a64bf007b834dee7b53078f8aab9edf9f4c"});
 assert.equal(cleanupListHash(review),review.review_hash);
 assert.equal(cleanupListText(review),"Stale loops in your currently permitted sources:\n\n1. Dana 李\\u000aSaved — Return Café deck\n   You owe · overdue; 7 days old; surfaced 3 times; chased twice; automatic nudging has stopped\n   chased twice — nudge again or drop\n\n1 more stale loops are not shown in this list.\n\nTell me which numbers to dismiss, snooze, or keep. For example: dismiss 1; snooze 2 for 7 days; keep 3. Nothing has changed yet.");
 const current=cleanupListSchema.parse({...review,rows:review.rows.map(row=>({...row,suggestion:reminderHandoff}))});
 current.review_hash=cleanupListHash(current);
 assert.notEqual(current.review_hash,review.review_hash);
 assert.match(cleanupListText(current),/reminded you twice; automatic reminders have stopped/);
 assert.ok(cleanupListText(current).includes(reminderHandoff));
 assert.ok(!cleanupListText(current).includes('chased twice'));
 for(const suggestion of ['do it or dismiss','nudge or drop']){
  const legacy=cleanupListSchema.parse({...review,rows:review.rows.map(row=>({...row,suggestion}))});
  legacy.review_hash=cleanupListHash(legacy);
  assert.ok(cleanupListText(legacy).includes('chased twice; automatic nudging has stopped'));
 }
});


test('large wholly-applied completion uses one concise sentence; every other outcome keeps detail',()=>{
 const applied=(index:number)=>({index,action:'resolve' as const,anchor_key:'task-'+index,status:'applied' as const,post_digest:revision,loop_status:'resolved' as const});
 const receipt=(items:unknown[],outcome='complete')=>{
  const parsed=ownerTaskToolReceiptSchema.parse({version:1,status:'committed',operation:'cleanup',action_id:revision,event_id:revision,payload_hash:revision,args_hash:revision,review_hash:revision,outcome,items});
  assert.equal(parsed.operation,'cleanup');if(parsed.operation!=='cleanup')throw Error('Wrong fixture');return parsed;
 };
 const many=Array.from({length:43},(_,i)=>applied(i+1));
 assert.equal(cleanupAcknowledgement(receipt(many)),'Marked 43 tasks done.');
 assert.equal(cleanupAcknowledgement(receipt(many.slice(0,6))),'Marked 6 tasks done.');
 assert.equal(cleanupAcknowledgement(receipt(many.slice(0,5))),many.slice(0,5).map(item=>`${item.index}. Marked done.`).join('\n'));
 for(const status of ['unknown','refused','skipped'] as const){
  const item={index:43,action:'resolve',anchor_key:'task-43',status,...(status==='refused'?{reason:'target_changed'}:{})};
  const text=cleanupAcknowledgement(receipt([...many.slice(0,42),item],'partial'));
  assert.equal(text.split('\n').length,43);assert.match(text,/^1\. Marked done\./);assert.doesNotMatch(text,/^Marked 43 tasks done/);
  assert.match(text,status==='unknown'?/Outcome unconfirmed/:status==='skipped'?/Not attempted/:/Not changed/);
 }
 const mixed=cleanupAcknowledgement(receipt([...many.slice(0,42),{...applied(43),action:'keep',loop_status:'open'}]));
 assert.equal(mixed.split('\n').length,43);assert.match(mixed,/43\. Kept\./);
});

for(const stage of ['route','writer'])test(`held cleanup ${stage} diagnostic cannot become delivered or accepted conversation text even when model repeats it`,async()=>{
 const f=fixture();try{
  await f.coordinator.receive(inbound('list','Show stale loops'),context);f.phase='cleanup';if(stage==='route')f.failApply=true;else f.failWriter=true;
  const outcome=await f.coordinator.receive(inbound('choice','Dismiss item 1 and snooze item 2 for 7 days'),context);
  assert.equal(outcome.status,'held');assert.equal(f.applies,1);assert.equal(f.sent.length,2);assert.equal(f.sent[1],'I couldn’t confirm whether the task change happened. Please check it before trying again, and tell me what you find. You can keep chatting in the meantime.');
  const action=f.journal.db.prepare("SELECT id FROM actions WHERE status='unknown'").get()!;
  const saved=f.journal.readOwnerTaskToolAction(action.id as string,{channel:'photon',accountId:'account',ownerId:'owner',lineKey:'line',scopeHash:f.client.scopeHash});
  if(stage==='writer')assert.deepEqual(await f.client.call('inspect',saved.request),{version:1,status:'unknown'});
  const rows=f.journal.db.prepare('SELECT status FROM actions').all();assert.ok(rows.some(row=>row.status==='unknown'));
  const accepted=f.journal.db.prepare("SELECT payload FROM prepared_replies").all();
  assert.ok(!JSON.stringify(accepted).includes('Cleanup diagnostic:'));
  assert.ok(!JSON.stringify(f.sent).includes('cleanup_stage'));
 }finally{await f.close();}
});
