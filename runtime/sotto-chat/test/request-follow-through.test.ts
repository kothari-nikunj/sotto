import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync,rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator,type ConversationDependencies,type ModelRequest} from '../src/conversation.js';
import {DELAYED_OUTCOME_POLICY} from '../src/delayed-outcome-policy.js';
import {stableActionIdentity} from '../src/tools/action-gate.js';

const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:'owner'};
const context={threadId:'thread',destination:'owner',lineKey:'line'};
const scopeHash='a'.repeat(64),revision='b'.repeat(64);
const inbound=(eventId:string,text:string)=>({...ownerPolicy,senderId:'owner',isDirect:true,eventId,text});
const client={identity:{line_key:'line'},scopeHash,call:async()=>({status:'not_found'})} as never;
type Authority={runId:string;accountId:string;ownerId:string;channel:'photon';lineKey:string;scopeHash:string};
const tools=[
  {kind:'owner_calendar_tool',dependency:'ownerCalendar',text:'Move cedar',prepare:(j:OperationalJournal,a:Authority,id:string)=>j.prepareOwnerCalendarAction(a,id,{operation:'propose_move',event_id:'cedar',start:'2026-10-10T12:00:00-07:00',end:'2026-10-10T13:00:00-07:00'},revision)},
  {kind:'owner_contacts_tool',dependency:'ownerContacts',text:'Create Dana',prepare:(j:OperationalJournal,a:Authority,id:string)=>j.prepareOwnerContactsAction(a,id,{operation:'propose_create',fields:{names:[{givenName:'Dana'}]}},revision)},
  {kind:'owner_gmail_draft_tool',dependency:'ownerGmailDraft',text:'Draft Exact draft',prepare:(j:OperationalJournal,a:Authority,id:string)=>j.prepareOwnerGmailDraftAction(a,id,{operation:'propose',message_id:'message',body:'Exact draft'},revision)},
  {kind:'owner_loop_tool',dependency:'ownerTaskTool',text:'Change it',prepare:(j:OperationalJournal,a:Authority,id:string)=>j.prepareOwnerTaskToolAction(a,id,{operation:'add',text:'Change it'})},
  {kind:'owner_reminder_tool',dependency:'ownerReminderTool',text:'Change it',prepare:(j:OperationalJournal,a:Authority,id:string)=>j.prepareOwnerReminderToolAction(a,id,{due:'2026-10-10T12:00:00-07:00',action:'Change it'})},
  {kind:'owner_preference_tool',dependency:'ownerPreferenceTool',text:'Clear tone',prepare:(j:OperationalJournal,a:Authority,id:string)=>j.prepareOwnerPreferenceToolAction(a,id,{operation:'clear-tone'})},
  {kind:'owner_routine_tool',dependency:'ownerRoutineTool',text:'Cancel routine',prepare:(j:OperationalJournal,a:Authority,id:string)=>j.prepareOwnerRoutineToolAction(a,id,{operation:'cancel',routine_id:'c'.repeat(64)},revision)},
  {kind:'owner_skill_request',dependency:'ownerSkillRequest',text:'Research Cedar Labs',prepare:(j:OperationalJournal,a:Authority,id:string)=>j.prepareOwnerSkillRequestAction(a,id,{skill:'ask-research',selection:{mode:'company',company:'Cedar Labs'}})},
  {kind:'owner_web_read',dependency:'ownerWebRead',text:'Read https://example.com/cedar',prepare:(j:OperationalJournal,a:Authority,id:string)=>j.prepareOwnerWebReadAction(a,id,{url:'https://example.com/cedar',source_revision:null})},
] as const;
test('all supported original action kinds send one notice without another owner turn',async(t)=>{
for(const item of tools)await t.test(`${item.kind} original request gets one honest notice without another owner turn`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-through-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let models=0;const sends:string[]=[];
  const service=new ConversationCoordinator({journal,ownerPolicy,[item.dependency]:client,
    model:{generate:async(request)=>{models++;const action=item.prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'Unverified model success';}},
    postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'notice'};}});
  try {
    assert.equal((await service.receive(inbound('original',item.text),context)).status,'held');
    assert.equal(sends.length,1);assert.match(sends[0]!,/I couldn’t confirm whether/);
    assert.ok(sends[0]!.includes(DELAYED_OUTCOME_POLICY[item.kind].subject));
    await service.recover();await service.recover();
    assert.equal(sends.length,1);assert.equal(models,1);
    assert.equal(journal.listPendingInbound().length,1);assert.equal(journal.db.prepare('SELECT status FROM actions').get()?.status,'unknown');
  } finally {await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

const commands=[
 {kind:'owner_loop_add_admitted',dependency:'ownerLoop',text:'/loop add Confirm cedar'},
 {kind:'owner_loop_close_admitted',dependency:'ownerLoop',text:'/loop close cedar'},
 {kind:'owner_reminder_admitted',dependency:'ownerReminder',text:'/reminder at 2026-10-10T12:00:00-07:00 Check cedar'},
 {kind:'owner_quiet_admitted',dependency:'ownerQuiet',text:'/quiet for 1h'},
 {kind:'owner_brief_request',dependency:'ownerBriefRequest',text:'Give me my evening brief'},
 {kind:'owner_meeting_prep_request',dependency:'ownerMeetingPrep',text:'yes please'},
 {kind:'owner_offer_reply',dependency:'ownerOfferReply',text:'done'},
] as const;
for(const item of commands)await t.test(`${item.kind} lost original receipt gives one honest notice without another turn`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-command-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  const operations:string[]=[],sends:string[]=[];
  const call=async(operation:string)=>{operations.push(operation);if(operation==='preview')return {status:'match',offer_id:'c'.repeat(64),kind:'meeting-prep'};if(operation==='prepare'||operation==='close_prepare')return {status:'prepared',zone:'UTC',expected_row_digest:revision};if(operation.includes('inspect'))throw Error('proof unavailable');throw Error('lost after provider contact');};
  const service=new ConversationCoordinator({journal,ownerPolicy,[item.dependency]:{identity:{line_key:'line'},scopeHash,call,callClose:call} as never,
    model:{generate:async()=>{throw Error('literal or selected request reached model');}},
    postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'notice'};}});
  const selected=item.kind==='owner_meeting_prep_request'||item.kind==='owner_offer_reply';
  try {
    const original={...inbound('original',item.text),...(selected?{replyBound:true,replyToMessageId:'accepted-offer',providerSentAtMs:Date.now()}: {})};
    assert.equal((await service.receive(original,context)).status,'held');
    assert.equal(journal.db.prepare('SELECT kind,status FROM actions').get()?.kind,item.kind);
    assert.equal(sends.length,1);assert.ok(sends[0]!.includes(DELAYED_OUTCOME_POLICY[item.kind].subject));
    await service.recover();await service.recover();assert.equal(sends.length,1);
    assert.equal(operations.filter(op=>op.includes('inspect')).length,1,'same-minute recovery never polls twice');
  } finally {await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

});

for(const lostNotice of [false,true])test(`timer follows original Calendar action to exact late refusal without owner input, lost notice=${lostNotice}`,async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-wakeup-')),path=join(folder,'journal.sqlite');
  let journal=new OperationalJournal(path),service:ConversationCoordinator|undefined,prove=false,models=0,inspections=0;
  const sends:string[]=[];
  const deps=():ConversationDependencies=>({journal,ownerPolicy,ownerCalendar:{identity:{line_key:'line'},scopeHash,call:async()=>{inspections++;if(!prove)return {status:'not_found'};return {status:'rejected',code:'target_changed'};}} as never,
    model:{generate:async(request:ModelRequest)=>{models++;const action=tools[0].prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';}},
    postPrepared:async(post)=>{sends.push(post.text);return sends.length===1&&lostNotice?{acceptance:'unknown'}:{acceptance:'accepted',messageId:'send-'+sends.length};}});
  try {
    service=new ConversationCoordinator(deps());await service.receive(inbound('original','Move cedar'),context);
    assert.equal(sends.length,1);assert.equal(inspections,1);
    await service.close();journal.close();journal=new OperationalJournal(path);journal.recoverInterrupted();
    service=new ConversationCoordinator(deps());await service.recover();assert.equal(inspections,1,'restart retains durable inspect cadence');
    prove=true;t.mock.timers.tick(60_000);await service.drain();await Promise.resolve();
    assert.equal(inspections,2);assert.equal(sends.length,2);assert.match(sends[1]!,/^I couldn’t move the meeting/);assert.equal(models,1);
    assert.equal(journal.listPendingInbound().length,0);
    t.mock.timers.tick(120_000);await service.drain();assert.equal(sends.length,2,'settled records do not keep a recovery timer');
    await service.close();t.mock.timers.tick(120_000);assert.equal(inspections,2);
  } finally {await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('repeated due-proof timers leave unrelated pending input for fresh owner intake',async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-isolated-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let models=0,inspections=0;const sends:string[]=[];
  const service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar:{identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
    assert.equal(operation,'inspect');inspections++;return {status:'not_found'};
  }} as never,model:{generate:async(request)=>{
    models++;if(request.text!=='Move cedar')return 'Safe answer';
    const action=tools[0].prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);
    journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';
  }},postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'send-'+sends.length};}});
  try {
    await service.receive(inbound('original','Move cedar'),context);await service.drain();
    // Durable input left by an interrupted ordinary turn must not become timer work.
    const pending=journal.admitInbound({eventId:'interrupted',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
      payload:JSON.stringify({inbound:inbound('interrupted','Earlier safe question'),context})}).event.id as string;
    for(let minute=1;minute<=3;minute++){
      t.mock.timers.tick(60_000);await service.drain();await Promise.resolve();
      assert.equal(inspections,minute+1,'the real timer still inspects only the delayed proof');
      assert.equal(models,1,'the timer must never enter paid model work for other pending input');
      assert.equal(sends.length,1,'no ordinary reply or duplicate notice is sent by the timer');
      assert.equal(journal.getInbound(pending)?.outcome,'pending');
    }
    assert.equal((await service.receive(inbound('safe','New safe question'),context)).status,'accepted');
    assert.equal(models,2);assert.deepEqual(sends.slice(1),['Safe answer']);
    assert.equal(journal.getInbound(pending)?.outcome,'resolved');
    assert.equal(journal.db.prepare('SELECT status FROM actions').get()?.status,'unknown');
  }finally{await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('pending outcome notice has no completion feedback and exact late final reply gets it once',async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-feedback-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let prove=false,models=0;const sends:string[]=[],feedback:string[]=[];
  const feedbackState:unknown[]=[];
  const service=new ConversationCoordinator({journal,ownerPolicy,ownerTaskTool:{identity:{line_key:'line'},scopeHash,
    call:async(operation:string,request:ReturnType<OperationalJournal['prepareOwnerTaskToolAction']>)=>{
      assert.equal(operation,'inspect');if(!prove)return {status:'not_found'};
      return {version:1,status:'committed',action_id:request.actionId,event_id:request.eventId,payload_hash:request.payloadHash,
        args_hash:stableActionIdentity(request.eventId,'owner_loop_tool',request.args).payloadHash,
        operation:'add',anchor_key:'owner:cedar',post_digest:'c'.repeat(64),loop_status:'open'};
    }} as never,
    model:{generate:async(request)=>{models++;const action=tools[3].prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);
      journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';}},
    onOwnerReplyAccepted:message=>{
      feedback.push(message.eventId);
      feedbackState.push([journal.db.prepare('SELECT outcome FROM inbound_events').get()?.outcome,
        journal.db.prepare('SELECT status FROM actions').get()?.status]);
    },
    postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'send-'+sends.length};}});
  try {
    await service.receive(inbound('original','Change it'),context);await service.drain();
    assert.equal(sends.length,1);assert.deepEqual(feedback,[],'accepted uncertainty is not completion');
    prove=true;t.mock.timers.tick(60_000);await service.drain();await Promise.resolve();
    assert.equal(sends.length,2);assert.equal(sends[1],'Added: Change it');
    assert.equal(journal.db.prepare('SELECT status FROM actions').get()?.status,'succeeded');
    assert.deepEqual(feedback,['original'],'only the proved final reply supplies completion feedback');
    assert.deepEqual(feedbackState,[['resolved','succeeded']],'feedback follows durable settlement of the proved receipt');
    t.mock.timers.tick(120_000);await service.drain();await service.recover();
    assert.equal(sends.length,2);assert.equal(models,1);assert.deepEqual(feedback,['original']);
  }finally{await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('due-proof timers do not post an unrelated prepared reply',async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-prepared-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let models=0,inspections=0;const sends:string[]=[];
  const service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar:{identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
    assert.equal(operation,'inspect');inspections++;return {status:'not_found'};
  }} as never,model:{generate:async(request)=>{models++;const action=tools[0].prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);
    journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';}},
    canPostPrepared:async()=>true,
    postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'send-'+sends.length};}});
  try {
    await service.receive(inbound('original','Move cedar'),context);await service.drain();
    const pending=journal.admitInbound({eventId:'prepared',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
      payload:JSON.stringify({inbound:inbound('prepared','Earlier question'),context})}).event.id as string;
    journal.startRun({runId:'prepared-run',eventId:pending});
    journal.prepareReply({replyId:'prepared-reply',runId:'prepared-run',eventId:pending,dispatchId:'prepared-dispatch',
      kind:'interactive',destination:context.destination,payload:'Earlier prepared answer',sourceRevision:revision});
    for(let minute=1;minute<=3;minute++){
      t.mock.timers.tick(60_000);await service.drain();await Promise.resolve();
      assert.equal(inspections,minute+1);assert.equal(models,1);assert.equal(sends.length,1);
      assert.equal(journal.getDispatch('prepared-dispatch')?.status,'prepared');
    }
    await service.recover();assert.deepEqual(sends.slice(1),['Earlier prepared answer'],'explicit recovery retains its existing dispatch semantics');
    assert.equal(journal.getInbound(pending)?.outcome,'resolved');
  }finally{await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('an unavailable final acknowledgement does not give completion feedback',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-unavailable-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  const feedback:string[]=[],sends:string[]=[];
  const ownerTaskTool={identity:{line_key:'line'},scopeHash,call:async()=>{throw Error('unexpected provider call');}};
  const service=new ConversationCoordinator({journal,ownerPolicy,ownerTaskTool:ownerTaskTool as never,
    model:{generate:async(request)=>{
      const authority={runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash};
      const action=tools[3].prepare(journal,authority,request.ownerRequestKey);journal.beginAction(action.actionId);
      journal.settleOwnerTaskToolAction(action.actionId,authority,{version:1,status:'committed',action_id:action.actionId,event_id:action.eventId,
        payload_hash:action.payloadHash,args_hash:stableActionIdentity(action.eventId,'owner_loop_tool',action.args).payloadHash,
        operation:'add',anchor_key:'owner:cedar',post_digest:'c'.repeat(64),loop_status:'open'});
      ownerTaskTool.scopeHash='d'.repeat(64);return 'unverified';
    }},onOwnerReplyAccepted:message=>{feedback.push(message.eventId);},
    postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'fallback'};}});
  try {
    await service.receive(inbound('original','Change it'),context);
    assert.equal(sends.length,1);assert.match(sends[0]!,/^I couldn’t confirm the result/);
    assert.equal(journal.db.prepare('SELECT purpose FROM prepared_replies').get()?.purpose,'acknowledgement_unavailable');
    assert.deepEqual(feedback,[]);
  }finally{await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('timer gives one honest URL-read failure only after exact proof and skips unrelated completed reads',async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-read-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let phase='missing',models=0;const inspected:string[]=[],sends:string[]=[],feedback:string[]=[];
  const receipt=(request:ReturnType<OperationalJournal['prepareOwnerWebReadAction']>)=>({version:1 as const,status:'committed' as const,
    action_id:request.actionId,event_id:request.eventId,payload_hash:request.payloadHash,
    args_hash:stableActionIdentity(request.eventId,'owner_web_read',request.args).payloadHash,
    outcome:{operation:'read' as const,result:{url:'https://example.com/cedar',title:'Cedar',text:'Recovered private contents',provider:'exa' as const}}});
  const service=new ConversationCoordinator({journal,ownerPolicy,ownerWebRead:{identity:{account_id:'account',owner_id:'owner',channel:'photon',line_key:'line'},scopeHash,
    call:async(operation:string,request:ReturnType<OperationalJournal['prepareOwnerWebReadAction']>)=>{
      assert.equal(operation,'inspect');inspected.push(request.eventId);
      if(phase==='missing')return {status:'not_found'};
      const proof=receipt(request);return phase==='mismatch'?{...proof,payload_hash:'d'.repeat(64)}:proof;
    }} as never,model:{generate:async(request)=>{models++;const action=tools[8].prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);
      journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';}},
    onOwnerReplyAccepted:message=>{feedback.push(message.eventId);},
    postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'send-'+sends.length};}});
  try {
    await service.receive(inbound('original',tools[8].text),context);await service.drain();
    const source=journal.listPendingInbound()[0]!.id as string;
    const other=journal.admitInbound({eventId:'other-read',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
      payload:JSON.stringify({inbound:inbound('other-read',tools[8].text),context})}).event.id as string;
    journal.startRun({runId:'other-run',eventId:other});
    const authority={runId:'other-run',...ownerPolicy,lineKey:'line',scopeHash},otherAction=tools[8].prepare(journal,authority,other);
    journal.beginAction(otherAction.actionId);journal.settleOwnerWebReadAction(otherAction.actionId,authority,receipt(otherAction));
    inspected.length=0;phase='mismatch';t.mock.timers.tick(60_000);await service.drain();await Promise.resolve();
    assert.deepEqual(inspected,[source]);assert.equal(journal.getInbound(source)?.outcome,'pending');assert.equal(sends.length,1);
    phase='valid';t.mock.timers.tick(60_000);await service.drain();await Promise.resolve();
    assert.deepEqual(inspected,[source,source]);assert.equal(models,1);assert.equal(sends.length,2);
    assert.equal(sends[1],'I recovered the URL-read result, but couldn’t finish your requested response. Please ask me again if you still need it.');
    assert.ok(sends.every(text=>!text.includes('Recovered private contents')));assert.deepEqual(feedback,[]);
    assert.equal(journal.getInbound(source)?.outcome,'resolved');assert.equal(journal.getInbound(other)?.outcome,'pending');
    const final=journal.listRepliesForThread(source).filter(reply=>reply.event_id===source&&reply.purpose==='acknowledgement_unavailable');
    assert.equal(final.length,1);assert.deepEqual(JSON.parse(final[0]!.covered_event_ids as string),[source]);
    t.mock.timers.tick(120_000);await service.drain();assert.equal(sends.length,2);assert.equal(inspected.length,2);
  }finally{await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('timer acknowledges a queued brief and asks its independent read remainder separately',async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-brief-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let prove=false,models=0;const operations:string[]=[],sends:string[]=[];
  const service=new ConversationCoordinator({journal,ownerPolicy,ownerBriefRequest:{identity:{line_key:'line'},scopeHash,
    call:async(operation:string,request:ReturnType<OperationalJournal['prepareOwnerBriefRequestAction']>)=>{
      operations.push(operation);if(operation==='admit')throw Error('lost queued receipt');if(!prove)return {status:'not_found'};
      return {version:1,status:'committed',action_id:request.actionId,event_id:request.eventId,payload_hash:request.payloadHash,
        args_hash:stableActionIdentity(request.eventId,'owner_brief_request',request.args).payloadHash,
        outcome:{operation:'admit',request_id:'c'.repeat(32),kind:'evening',refresh:false,status:'queued'}};
    }} as never,model:{generate:async()=>{models++;throw Error('timer must not generate the independent remainder');}},
    postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'send-'+sends.length};}});
  try {
    await service.receive(inbound('original','Give me my evening brief and tell me what is on my calendar'),context);await service.drain();
    assert.equal(sends.length,1);prove=true;t.mock.timers.tick(60_000);await service.drain();await Promise.resolve();
    assert.deepEqual(operations,['admit','inspect','inspect']);assert.equal(models,0);
    assert.equal(sends[1],'Your brief request is queued. Please ask the other part in a separate message.');
    assert.equal(journal.listPendingInbound().length,0);
    t.mock.timers.tick(120_000);await service.drain();assert.equal(sends.length,2);
  }finally{await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

for(const end of ['stop','close'] as const)test(`${end} disposes the due-proof wakeup while the original action is still unknown`,async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-end-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let inspections=0;const sends:string[]=[];
  const service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar:{identity:{line_key:'line'},scopeHash,call:async()=>{inspections++;return {status:'not_found'};}} as never,
    model:{generate:async(request)=>{const action=tools[0].prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';}},
    postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'send-'+sends.length};}});
  try {
    await service.receive(inbound('original','Move cedar'),context);assert.equal(inspections,1);
    if(end==='stop'){await service.receive(inbound('stop','/stop'),context);await service.drain();}else await service.close();
    const before=sends.length;t.mock.timers.tick(120_000);await service.drain();await Promise.resolve();
    assert.equal(inspections,1);assert.equal(sends.length,before);assert.equal(journal.db.prepare('SELECT status FROM actions').get()?.status,'unknown');
  } finally {await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('due-proof wakeup queues behind a fresh active model run and never reexecutes the old action',async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-queue-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let inspections=0,models=0,prove=false,release!:()=>void,entered!:()=>void;
  const gate=new Promise<void>(resolve=>release=resolve),ready=new Promise<void>(resolve=>entered=resolve),sends:string[]=[];
  const service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar:{identity:{line_key:'line'},scopeHash,call:async()=>{inspections++;return prove?{status:'rejected',code:'target_changed'}:{status:'not_found'};}} as never,
    model:{generate:async(request)=>{models++;if(request.text==='Safe question'){entered();await gate;return 'Safe answer';}const action=tools[0].prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';}},
    postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'send-'+sends.length};}});
  try {
    await service.receive(inbound('original','Move cedar'),context);
    const safe=service.receive(inbound('safe','Safe question'),context);await ready;
    prove=true;t.mock.timers.tick(60_000);await Promise.resolve();assert.equal(inspections,1,'timer never runs proof work concurrently with active thread work');
    release();await safe;await service.drain();await Promise.resolve();
    assert.equal(inspections,2);assert.equal(models,2);assert.deepEqual(sends.map(text=>text.startsWith('I couldn’t confirm whether')?'notice':text.startsWith('I couldn’t move')?'final':text),['notice','Safe answer','final']);
    assert.equal(journal.listPendingInbound().length,0);
  } finally {release();await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('a failed due-proof scan stays contained and preserves the held action',async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-scan-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let models=0,inspections=0,failScan=false;
  const listUnknown=journal.listUnknownActionsForThread.bind(journal);
  journal.listUnknownActionsForThread=(eventId)=>{if(failScan)throw Error('controlled journal scan failure');return listUnknown(eventId);};
  const service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar:{identity:{line_key:'line'},scopeHash,call:async()=>{inspections++;return {status:'not_found'};}} as never,
    model:{generate:async(request)=>{models++;const action=tools[0].prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';}},
    postPrepared:async()=>({acceptance:'accepted',messageId:'notice'})});
  try {
    await service.receive(inbound('original','Move cedar'),context);await service.drain();
    failScan=true;
    // Exercise the scan from the same advisory boundary used by queue cleanup.
    assert.doesNotThrow(()=>(service as unknown as {scheduleDelayedRecovery():void}).scheduleDelayedRecovery());
    t.mock.timers.tick(120_000);await service.drain();assert.equal(inspections,1);
    assert.equal(journal.db.prepare('SELECT status FROM actions').get()?.status,'unknown');
    assert.equal(journal.listPendingInbound().length,1);
    failScan=false;await service.recover();assert.equal(models,1,'held-only recovery never re-enters the model');assert.equal(inspections,2);
    t.mock.timers.tick(60_000);await service.drain();assert.equal(inspections,3,'later recovery rearms the bounded wakeup');
  } finally {failScan=false;await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

const webReadClient=(inspections:string[])=>({identity:{line_key:'line',account_id:'account',owner_id:'owner',channel:'photon'},scopeHash,
  call:async(operation:string)=>{inspections.push(operation);return {status:'not_found'};}}) as never;
test('a held URL read in another request does not strand a late proved final reply',async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-web-peer-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let prove=false,models=0;const sends:string[]=[],webCalls:string[]=[];
  const service=new ConversationCoordinator({journal,ownerPolicy,
    ownerCalendar:{identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{assert.equal(operation,'inspect');return prove?{status:'rejected',code:'target_changed'}:{status:'not_found'};}} as never,
    ownerWebRead:webReadClient(webCalls),
    model:{generate:async(request)=>{models++;const authority={runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash};
      const action=(request.text==='Move cedar'?tools[0]:tools[8]).prepare(journal,authority,request.ownerRequestKey);
      journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';}},
    postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'send-'+sends.length};}});
  try {
    await service.receive(inbound('calendar','Move cedar'),context);await service.drain();
    await service.receive(inbound('read','Read https://example.com/cedar'),context);await service.drain();
    assert.equal(sends.length,2);assert.ok(sends.every(text=>text.startsWith('I couldn’t confirm whether')));
    prove=true;t.mock.timers.tick(60_000);await service.drain();await Promise.resolve();await service.drain();
    assert.equal(sends.length,3,'the proved calendar request gets its final reply without owner input');
    assert.match(sends[2]!,/^I couldn’t move the meeting/);
    const calendarSource=journal.db.prepare("SELECT source_event_id FROM actions WHERE kind='owner_calendar_tool'").get()!.source_event_id as string;
    const readAction=journal.db.prepare("SELECT source_event_id,status FROM actions WHERE kind='owner_web_read'").get()!;
    assert.equal(journal.getInbound(calendarSource)?.outcome,'resolved');
    assert.equal(journal.getInbound(readAction.source_event_id as string)?.outcome,'pending');
    assert.equal(readAction.status,'unknown','the other held read is never relabelled');
    assert.ok(webCalls.every(operation=>operation==='inspect'));assert.equal(models,2);
  }finally{await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('timer retries only a definitely undelivered late final reply',async(outer)=>{
for(const finalDelivery of ['rejected-once','rejected-always','unknown'] as const)await outer.test(finalDelivery,async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-final-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let prove=false,models=0;const sends:string[]=[];
  const service=new ConversationCoordinator({journal,ownerPolicy,
    ownerCalendar:{identity:{line_key:'line'},scopeHash,call:async()=>prove?{status:'rejected',code:'target_changed'}:{status:'not_found'}} as never,
    model:{generate:async(request)=>{models++;const action=tools[0].prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);
      journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';}},
    postPrepared:async(post)=>{sends.push(post.text);if(sends.length===1)return {acceptance:'accepted',messageId:'notice'};
      if(finalDelivery==='unknown')return {acceptance:'unknown'};
      if(finalDelivery==='rejected-always'||sends.length===2)return {acceptance:'rejected'};
      return {acceptance:'accepted',messageId:'final'};}});
  try {
    await service.receive(inbound('original','Move cedar'),context);await service.drain();
    prove=true;
    for(let minute=1;minute<=5;minute++){t.mock.timers.tick(60_000);await service.drain();await Promise.resolve();await service.drain();}
    const finals=sends.slice(1);
    assert.ok(finals.every(text=>/^I couldn’t move the meeting/.test(text)));
    assert.equal(finals.length,finalDelivery==='unknown'?1:2,finalDelivery==='unknown'?'a contacted unknown final reply is never resent':'only definite non-delivery is retried, and boundedly');
    assert.equal(journal.listPendingInbound().length,finalDelivery==='rejected-once'?0:1);
    assert.equal(journal.db.prepare('SELECT status FROM actions').get()?.status,'rejected');assert.equal(models,1);
  }finally{await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
});

test('owner input after an undelivered late final reply drains it and still answers the new message',async(t)=>{
  t.mock.timers.enable({apis:['Date','setTimeout'],now:Date.parse('2026-10-09T20:00:00Z')});
  const folder=mkdtempSync(join(tmpdir(),'sotto-follow-drain-')),journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let prove=false;const sends:string[]=[],models:string[]=[];
  const service=new ConversationCoordinator({journal,ownerPolicy,
    ownerCalendar:{identity:{line_key:'line'},scopeHash,call:async()=>prove?{status:'rejected',code:'target_changed'}:{status:'not_found'}} as never,
    model:{generate:async(request)=>{models.push(request.text);if(request.text==='hello')return 'Hi there';
      const action=tools[0].prepare(journal,{runId:request.runId,...ownerPolicy,lineKey:'line',scopeHash},request.ownerRequestKey);
      journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');return 'unverified';}},
    postPrepared:async(post)=>{sends.push(post.text);return sends.length===2?{acceptance:'rejected'}:{acceptance:'accepted',messageId:'send-'+sends.length};}});
  try {
    await service.receive(inbound('original','Move cedar'),context);await service.drain();
    prove=true;t.mock.timers.tick(60_000);await service.drain();await Promise.resolve();await service.drain();
    assert.equal(sends.length,2);assert.equal(journal.listPendingInbound().length,1);
    await service.receive(inbound('hello','hello'),context);await service.drain();
    assert.match(sends[2]!,/^I couldn’t move the meeting/);assert.equal(sends[3],'Hi there');assert.equal(sends.length,4);
    assert.deepEqual(models,['Move cedar','hello']);assert.equal(journal.listPendingInbound().length,0);
  }finally{await service.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});


test('restart resolves the saved action before any pending notice without sending an obsolete warning',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-recovery-first-')),path=join(folder,'journal.sqlite');
  let journal=new OperationalJournal(path);let service:ConversationCoordinator|undefined;
  const sends:string[]=[];let inspections=0,models=0;
  try {
    const event=journal.admitInbound({eventId:'original',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
      payload:JSON.stringify({inbound:inbound('original','Move cedar'),context})}).event;
    journal.startRun({runId:'original-run',eventId:String(event.id)});
    const action=tools[0].prepare(journal,{runId:'original-run',...ownerPolicy,lineKey:'line',scopeHash},String(event.id));
    journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');
    // Crash happened before a notice was prepared or contacted.
    journal.close();journal=new OperationalJournal(path);journal.recoverInterrupted();
    service=new ConversationCoordinator({journal,ownerPolicy,
      ownerCalendar:{identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
        assert.equal(operation,'inspect','never replay the original action');inspections++;return {status:'rejected',code:'target_changed'};
      }} as never,
      model:{generate:async()=>{models++;throw Error('recovery must use the receipt');}},
      postPrepared:async(post)=>{sends.push(post.text);return {acceptance:'accepted',messageId:'resolved'};}});
    await service.recover();await service.recover();
    assert.equal(inspections,1);assert.equal(models,0);
    assert.equal(sends.length,1);assert.match(sends[0]!,/^I couldn’t move the meeting/);
    assert.equal(sends.filter(text=>text.startsWith('I couldn’t confirm whether')).length,0);
    assert.equal(journal.listPendingInbound().length,0);
    assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,1);
    assert.equal(journal.db.prepare('SELECT status FROM actions').get()!.status,'rejected');
  } finally {await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
