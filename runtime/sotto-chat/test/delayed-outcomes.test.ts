import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ConversationCoordinator, type PostReceipt} from '../src/conversation.js';
import {OperationalJournal} from '../src/journal/index.js';
import {calendarReviewHash,calendarReviewText} from '../src/tools/owner-calendar-contract.js';
import {contactsReviewHash,contactsReviewText} from '../src/tools/owner-contacts-contract.js';
import {gmailDraftReviewHash,gmailDraftReviewText} from '../src/tools/owner-gmail-draft-contract.js';

const scopeHash = 'a'.repeat(64);
const context = {threadId:'thread',destination:'owner-destination',lineKey:'line'};
const ownerPolicy = {channel:'photon' as const,accountId:'account',ownerId:'owner'};
const inbound = (eventId:string,text:string) => ({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId,text});
const cases = [
  {kind:'calendar', args:{operation:'propose_move' as const,event_id:'cedar',start:'2026-10-10T12:00:00-07:00',end:'2026-10-10T13:00:00-07:00'}, rejection:'target_changed'},
  {kind:'contacts', args:{operation:'propose_create' as const,fields:{names:[{givenName:'Dana'}]}}, rejection:'target_changed'},
  {kind:'gmail', args:{operation:'propose' as const,message_id:'message-1',body:'Exact draft'}, rejection:'target_changed'},
] as const;

for (const item of cases) test(`${item.kind} rejected notice is bounded and late refusal has a distinct ACK after restart`, async () => {
  const folder = mkdtempSync(join(tmpdir(),'sotto-delayed-outcome-'));
  const file = join(folder,'journal.sqlite');
  let journal = new OperationalJournal(file);
  let service: ConversationCoordinator | undefined;
  let effects = 0, notices = 0, models = 0, prove = false;
  try {
    const event = journal.admitInbound({eventId:'source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
      payload:JSON.stringify({inbound:inbound('source','Please change it'),context})}).event.id as string;
    const runId = 'effect-run';
    journal.startRun({runId,eventId:event});
    const authority = {runId,...ownerPolicy,lineKey:'line',scopeHash};
    const request = item.kind === 'calendar' ? journal.prepareOwnerCalendarAction(authority,event,item.args as never,'b'.repeat(64))
      : item.kind === 'contacts' ? journal.prepareOwnerContactsAction(authority,event,item.args as never,'b'.repeat(64))
      : journal.prepareOwnerGmailDraftAction(authority,event,item.args as never,'b'.repeat(64));
    journal.beginAction(request.actionId);
    journal.settleAction(request.actionId,'unknown');
    const client = {identity:{line_key:'line'},scopeHash,call:async (operation:string) => {
      assert.equal(operation,'inspect','the original provider mutation is never repeated');
      effects++;
      if (!prove) throw Error('inspection unavailable');
      return {status:'rejected',code:item.rejection};
    }} as never;
    const deps = () => ({journal,ownerPolicy,model:{generate:async () => {models++;return 'Safe answer.';}},
      ...(item.kind === 'calendar' ? {ownerCalendar:client} : item.kind === 'contacts' ? {ownerContacts:client} : {ownerGmailDraft:client}),
      postPrepared:async (post:{text:string}):Promise<PostReceipt> => {
        if (post.text.includes('couldn’t confirm whether')) {notices++;return {acceptance:'rejected'};}
        return {acceptance:'accepted',messageId:`accepted-${notices}-${models}`};
      }});
    service = new ConversationCoordinator(deps());
    assert.equal((await service.recover())[0]?.status,'held');
    assert.equal(notices,1);
    assert.equal(journal.getInbound(event)?.outcome,'pending');
    assert.equal(journal.getAction(request.actionId)?.status,'unknown');
    await service.recover();
    assert.equal(notices,1,'rejected notice waits for durable cadence');
    await service.close();journal.close();
    journal = new OperationalJournal(file);journal.recoverInterrupted();
    service = new ConversationCoordinator(deps());
    await service.recover();
    assert.equal(notices,1,'restart cannot bypass notice cadence');
    journal.db.prepare("UPDATE dispatches SET updated_at=updated_at-61000 WHERE status='rejected'").run();
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE id=?").run(request.actionId);
    await service.recover();
    assert.equal(notices,2);
    journal.db.prepare("UPDATE dispatches SET updated_at=updated_at-61000 WHERE status='rejected'").run();
    await service.recover();
    assert.equal(notices,2,'policy caps notice contacts');
    assert.equal(models,0,'the held effect never reruns through the model');
    prove = true;
    journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE id=?").run(request.actionId);
    await service.recover();
    assert.equal(journal.getAction(request.actionId)?.status,'rejected');
    assert.equal(journal.getInbound(event)?.outcome,'resolved');
    assert.equal(journal.listRepliesForThread(event).filter(reply => reply.purpose !== 'outcome_pending').length,1);
    assert.equal(notices,2);
  } finally {await service?.close();try{journal.close();}catch{}rmSync(folder,{recursive:true,force:true});}
});

test('unknown calendar notice never resends and does not block late refused outcome or unrelated work',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-unknown-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  try{
    const event=journal.admitInbound({eventId:'source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:inbound('source','Change calendar'),context})}).event.id as string;
    journal.startRun({runId:'effect-run',eventId:event});
    const authority={runId:'effect-run',...ownerPolicy,lineKey:'line',scopeHash};
    const action=journal.prepareOwnerCalendarAction(authority,event,cases[0]!.args,'b'.repeat(64));
    journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');
    let prove=false,notices=0,models=0;
    const ownerCalendar={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
      assert.equal(operation,'inspect');
      if(!prove)throw Error('inspection unavailable');return {status:'rejected',code:'target_changed'};
    }} as never;
    service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar,model:{generate:async()=>{models++;return 'Safe answer.';}},
      postPrepared:async post=>{if(post.text.includes('couldn’t confirm whether')){notices++;return {acceptance:'unknown'};}return {acceptance:'accepted',messageId:'accepted-'+models};}});
    await service.recover();assert.equal(notices,1);
    await service.recover();assert.equal(notices,1);
    assert.equal((await service.receive(inbound('safe','Hello'),context)).status,'accepted');
    assert.equal(models,1);
    prove=true;
    journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.actionId);
    await service.recover();
    assert.equal(journal.getInbound(event)?.outcome,'resolved');
    assert.equal(notices,1);
    assert.equal(journal.listRepliesForThread(event).filter(reply=>reply.event_id===event&&reply.purpose!=='outcome_pending').length,1);
  }finally{await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('late Calendar proof cancels a prepared unsent notice before final confirmation',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-prepared-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  try{
    const event=journal.admitInbound({eventId:'source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:inbound('source','Change calendar'),context})}).event.id as string;
    journal.startRun({runId:'effect-run',eventId:event});
    const authority={runId:'effect-run',...ownerPolicy,lineKey:'line',scopeHash};
    const action=journal.prepareOwnerCalendarAction(authority,event,cases[0]!.args,'b'.repeat(64));
    journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');
    let prove=false,posts:string[]=[];
    const ownerCalendar={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
      assert.equal(operation,'inspect');if(!prove)throw Error('inspection unavailable');return {status:'rejected',code:'target_changed'};
    }} as never;
    service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar,model:{generate:async()=>{throw Error('model reran');}},
      canPostPrepared:async()=>prove,
      postPrepared:async post=>{posts.push(post.text);return {acceptance:'accepted',messageId:'final'};}});
    await service.recover();
    const notice=journal.getReplyForEvent(event)!;assert.equal(notice.dispatch_status,'prepared');
    prove=true;journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.actionId);
    await service.recover();
    assert.equal(journal.getDispatch(notice.dispatch_id as string)?.status,'cancelled');
    assert.equal(posts.length,1);assert.match(posts[0]!,/couldn’t move the meeting/);
    assert.equal(journal.getInbound(event)?.outcome,'resolved');
  }finally{await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

async function assertLostCommittedReceipt(item: typeof cases[number]) {
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-committed-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  const revision='b'.repeat(64);
  try{
    const admit=(id:string,text:string)=>journal.admitInbound({eventId:id,accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:inbound(id,text),context})}).event.id as string;
    const source=admit('review','Review it');journal.startRun({runId:'review-run',eventId:source});
    const authority={runId:'review-run',...ownerPolicy,lineKey:'line',scopeHash};
    const proposed=item.kind==='calendar'?journal.prepareOwnerCalendarAction(authority,source,item.args as never,revision)
      :item.kind==='contacts'?journal.prepareOwnerContactsAction(authority,source,item.args as never,revision)
      :journal.prepareOwnerGmailDraftAction(authority,source,item.args as never,revision);
    const calendarReview={operation:'move' as const,calendar_id:'primary',event_id:'cedar',etag:'"1"',summary:'Cedar',html_link:'',old_start:'2026-10-10T10:00:00-07:00',old_end:'2026-10-10T11:00:00-07:00',new_start:'2026-10-10T12:00:00-07:00',new_end:'2026-10-10T13:00:00-07:00',notifications:'all' as const,source_revision:revision,review_hash:''};
    calendarReview.review_hash=calendarReviewHash(calendarReview);
    const contactsReview={operation:'create' as const,resource_name:null,before:null,fields:{names:[{givenName:'Dana'}]},source_revision:revision,review_hash:''};
    contactsReview.review_hash=contactsReviewHash(contactsReview);
    const gmailReview={message_id:'message-1',thread_id:'thread-1',to:'dana@example.com',subject:'Re: Cedar',body:'Exact draft',reply_message_id:'<message@example.com>',source_revision:revision,review_hash:''};
    gmailReview.review_hash=gmailDraftReviewHash(gmailReview);
    const review=item.kind==='calendar'?calendarReview:item.kind==='contacts'?contactsReview:gmailReview;
    const preview={version:1 as const,status:'preview' as const,event_id:source,payload_hash:proposed.payloadHash,review};
    journal.beginAction(proposed.actionId);
    if(item.kind==='calendar')journal.settleOwnerCalendarAction(proposed.actionId,authority,preview as never);
    else if(item.kind==='contacts')journal.settleOwnerContactsAction(proposed.actionId,authority,preview as never);
    else journal.settleOwnerGmailDraftAction(proposed.actionId,authority,preview as never);
    const reviewText=item.kind==='calendar'?calendarReviewText(preview as never):item.kind==='contacts'?contactsReviewText(preview as never):gmailDraftReviewText(preview as never);
    journal.prepareReply({replyId:'review-reply',runId:'review-run',eventId:source,dispatchId:'review-dispatch',kind:'interactive',destination:context.destination,payload:reviewText,sourceRevision:revision});
    journal.beginDispatch('review-dispatch');journal.settleDispatch('review-dispatch',1,'accepted','review-accepted');
    journal.resolveInbound(source,'resolved');journal.completeRun('review-run');
    const yes=admit('yes','yes');journal.startRun({runId:'effect-run',eventId:yes});
    const yesAuthority={...authority,runId:'effect-run'};
    const proposal={event_id:source,action_id:proposed.actionId,args_hash:journal.getAction(proposed.actionId)!.payload_hash as string,accepted_dispatch_id:'review-dispatch',source_revision:revision};
    const confirmed=item.kind==='calendar'?journal.prepareOwnerCalendarAction(yesAuthority,yes,{operation:'move',review:calendarReview,proposal},revision)
      :item.kind==='contacts'?journal.prepareOwnerContactsAction(yesAuthority,yes,{operation:'create',review:contactsReview,proposal},revision)
      :journal.prepareOwnerGmailDraftAction(yesAuthority,yes,{operation:'save',review:gmailReview,proposal},revision);
    journal.beginAction(confirmed.actionId);journal.settleAction(confirmed.actionId,'unknown');
    const argsHash=journal.getAction(confirmed.actionId)!.payload_hash as string;
    const outcome=item.kind==='calendar'?{operation:'move',calendar_id:'primary',event_id:'cedar',start:calendarReview.new_start,end:calendarReview.new_end,review_hash:calendarReview.review_hash,status:'moved'}
      :item.kind==='contacts'?{operation:'create',status:'created',resource_name:'people/cCreated',review_hash:contactsReview.review_hash}
      :{operation:'save',draft_id:'draft-1',thread_id:gmailReview.thread_id,review_hash:gmailReview.review_hash,status:'draft_saved'};
    const receipt={version:1,status:'committed',action_id:confirmed.actionId,event_id:yes,payload_hash:confirmed.payloadHash,args_hash:argsHash,outcome};
    let prove=false,inspects=0,notices=0,posts:string[]=[];
    const client={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{assert.equal(operation,'inspect');inspects++;if(!prove)throw Error('lost committed receipt');return receipt;}} as never;
    service=new ConversationCoordinator({journal,ownerPolicy,
      ...(item.kind==='calendar'?{ownerCalendar:client}:item.kind==='contacts'?{ownerContacts:client}:{ownerGmailDraft:client}),
      model:{generate:async()=>{throw Error('original effect must not rerun');}},
      postPrepared:async post=>{posts.push(post.text);if(post.text.includes('couldn’t confirm whether')){notices++;return {acceptance:'unknown'};}return {acceptance:'accepted',messageId:'final-accepted'};}});
    await service.recover();assert.equal(notices,1);assert.equal(journal.getAction(confirmed.actionId)?.status,'unknown');
    prove=true;journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(confirmed.actionId);
    await service.recover();
    assert.equal(journal.getAction(confirmed.actionId)?.status,'succeeded');
    assert.equal(journal.getInbound(yes)?.outcome,'resolved');
    assert.equal(posts.length,2,'unknown notice and final proof have separate sends');
    assert.equal(journal.listRepliesForThread(yes).filter(reply=>reply.event_id===yes).length,2);
    await service.recover();assert.equal(posts.length,2);assert.equal(inspects,2);
  }finally{await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
}
test('calendar lost committed receipt settles once after an unknown notice',()=>assertLostCommittedReceipt(cases[0]));
test('contacts lost committed receipt settles once after an unknown notice',()=>assertLostCommittedReceipt(cases[1]));
test('gmail lost committed receipt settles once after an unknown notice',()=>assertLostCommittedReceipt(cases[2]));

for (const item of cases) test(`${item.kind} /stop closes the original and suppresses late ACK while safe chat resumes`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-stop-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  try{
    const source=journal.admitInbound({eventId:'source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',payload:JSON.stringify({inbound:inbound('source','Please change it'),context})}).event.id as string;
    journal.startRun({runId:'effect-run',eventId:source});
    const authority={runId:'effect-run',...ownerPolicy,lineKey:'line',scopeHash};
    const action=item.kind==='calendar'?journal.prepareOwnerCalendarAction(authority,source,item.args as never,'b'.repeat(64))
      :item.kind==='contacts'?journal.prepareOwnerContactsAction(authority,source,item.args as never,'b'.repeat(64))
      :journal.prepareOwnerGmailDraftAction(authority,source,item.args as never,'b'.repeat(64));
    journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');
    let prove=false,posts:string[]=[],models=0;
    const client={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
      assert.equal(operation,'inspect');
      if(!prove)throw Error('inspection unavailable');return {status:'rejected',code:item.rejection};
    }} as never;
    service=new ConversationCoordinator({journal,ownerPolicy,
      ...(item.kind==='calendar'?{ownerCalendar:client}:item.kind==='contacts'?{ownerContacts:client}:{ownerGmailDraft:client}),
      model:{generate:async()=>{models++;return 'Safe answer.';}},
      postPrepared:async post=>{posts.push(post.text);return {acceptance:'accepted',messageId:`accepted-${posts.length}`};}});
    await service.recover();assert.equal(posts.length,1);
    assert.equal((await service.receive(inbound('stop','/stop'),context)).status,'stopped');
    await service.drain();
    assert.equal(posts.filter(text=>text.startsWith('I stopped working on your earlier request.')).length,1);
    assert.equal(journal.getInbound(source)?.outcome,'cancelled');
    prove=true;journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.actionId);
    assert.equal((await service.receive(inbound('safe','Hello'),context)).status,'accepted');
    assert.equal(journal.getAction(action.actionId)?.status,'rejected');
    assert.equal(posts.length,3,'only the earlier notice, stop acknowledgement, and unrelated safe answer were sent');
    assert.equal(posts[2],'Safe answer.');
    assert.equal(models,1);
    assert.equal(journal.listRepliesForThread(source).filter(reply=>reply.event_id===source).length,1);
  }finally{await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

function admitUnknownCalendar(journal:OperationalJournal) {
  const source=journal.admitInbound({eventId:'source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
    payload:JSON.stringify({inbound:inbound('source','Move cedar'),context})}).event.id as string;
  journal.startRun({runId:'effect-run',eventId:source});
  const authority={runId:'effect-run',...ownerPolicy,lineKey:'line',scopeHash};
  const action=journal.prepareOwnerCalendarAction(authority,source,cases[0]!.args,'b'.repeat(64));
  journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');
  return {source,authority,actionId:action.actionId as string};
}

test('live process gives the calendar notice right after a fresh safe turn, with cadence-bounded inspection',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-live-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  try{
    const {source,actionId}=admitUnknownCalendar(journal);
    let inspections=0,prove=false;const posts:string[]=[];
    const ownerCalendar={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
      assert.equal(operation,'inspect');inspections++;if(!prove)throw Error('inspection unavailable');return {status:'rejected',code:'target_changed'};
    }} as never;
    service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar,model:{generate:async(request)=>`answer: ${request.text}`},
      postPrepared:async post=>{posts.push(post.text);return {acceptance:'accepted',messageId:`accepted-${posts.length}`};}});
    // No recover(): a running process must not wait for restart to tell the owner.
    for(const id of ['safe-1','safe-2','safe-3'])assert.equal((await service.receive(inbound(id,`Hello ${id}`),context)).status,'accepted');
    assert.equal(posts.length,4);
    assert.equal(posts[0],'answer: Hello safe-1','the fresh safe turn still goes first');
    assert.match(posts[1]!,/^I couldn’t confirm whether the calendar change happened\./);
    assert.deepEqual(posts.slice(2),['answer: Hello safe-2','answer: Hello safe-3']);
    assert.equal(inspections,1,'later live turns respect the inspect cadence');
    assert.equal(journal.getInbound(source)?.outcome,'pending');
    prove=true;journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(actionId);
    await service.receive(inbound('safe-4','Anything else?'),context);
    assert.equal(inspections,2);
    assert.deepEqual(posts.slice(4),['I couldn’t move the meeting because the reviewed event or permission is unavailable or changed.','answer: Anything else?'],
      'late exact proof acknowledges once, before the fresh turn');
    assert.equal(posts.filter(text=>text.startsWith('I couldn’t move the meeting')).length,1);
    assert.equal(posts.filter(text=>text.startsWith('I couldn’t confirm whether')).length,1);
    assert.equal(journal.getInbound(source)?.outcome,'resolved');
  }finally{await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('an unrelated /stop leaves the delayed source open and its late confirmation is still sent once',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-unrelated-stop-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  try{
    const {source,actionId}=admitUnknownCalendar(journal);
    let prove=false,release!:()=>void;const gate=new Promise<void>(resolve=>{release=resolve;});const posts:string[]=[];
    const ownerCalendar={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
      assert.equal(operation,'inspect');if(!prove)throw Error('inspection unavailable');return {status:'rejected',code:'target_changed'};
    }} as never;
    service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar,
      model:{generate:async(request)=>{if(request.text.includes('Research'))await gate;return `answer: ${request.text}`;}},
      postPrepared:async post=>{posts.push(post.text);return {acceptance:'accepted',messageId:`accepted-${posts.length}`};}});
    await service.recover();assert.equal(posts.length,1);
    const research=service.receive(inbound('research','Research the Acme market'),context);
    await new Promise(resolve=>setTimeout(resolve,50));
    assert.equal((await service.receive(inbound('stop','/stop'),context)).status,'stopped');
    release();await research.catch(()=>undefined);await service.drain();
    assert.equal(journal.db.prepare("SELECT outcome FROM inbound_events WHERE source_id='research'").get()?.outcome,'cancelled');
    assert.equal(journal.getInbound(source)?.outcome,'pending','the /stop targeted the newer research request');
    assert.equal(journal.getAction(actionId)?.status,'unknown');
    prove=true;journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(actionId);
    await service.receive(inbound('safe','Hello'),context);
    await service.recover();
    assert.ok(posts.includes('answer: Hello'));
    assert.equal(posts.filter(text=>text.startsWith('I couldn’t move the meeting')).length,1);
    assert.equal(posts.filter(text=>text.startsWith('I stopped working on your earlier request.')).length,1);
    assert.ok(!posts.includes('answer: Research the Acme market'));
    assert.equal(journal.getInbound(source)?.outcome,'resolved');
  }finally{await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

for(const notice of ['acknowledgement_unavailable','outcome_pending'] as const)test(`a failed final acknowledgement after a contacted ${notice} reply holds only its source`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-ack-held-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  try{
    const {source,authority,actionId}=admitUnknownCalendar(journal);
    journal.db.prepare("UPDATE actions SET status='executing' WHERE id=?").run(actionId);
    journal.settleOwnerCalendarAction(actionId,authority,'target_changed');
    journal.startRun({runId:'notice-run',eventId:source});
    journal.prepareReply({replyId:'notice',runId:'notice-run',eventId:source,coveredEventIds:[source],dispatchId:'notice-dispatch',kind:'interactive',
      destination:context.destination,purpose:notice,sourceRevision:'owner-only-context-v1',
      payload:notice==='outcome_pending'?'I couldn’t confirm whether the calendar change happened. Please check it before trying again, and tell me what you find. You can keep chatting in the meantime.'
        :'I couldn’t confirm the result of that request yet. Please check before trying again. You can keep chatting in the meantime.'});
    journal.beginDispatch('notice-dispatch');journal.settleDispatch('notice-dispatch',1,notice==='outcome_pending'?'accepted':'unknown','notice-accepted');
    const posts:string[]=[];
    // The connected scope changed, so the saved action can no longer be read for its acknowledgement.
    const ownerCalendar={identity:{line_key:'line'},scopeHash:'c'.repeat(64),call:async()=>{throw Error('not inspected');}} as never;
    service=new ConversationCoordinator({journal,ownerPolicy,ownerCalendar,model:{generate:async(request)=>`answer: ${request.text}`},
      postPrepared:async post=>{posts.push(post.text);return {acceptance:'accepted',messageId:`accepted-${posts.length}`};}});
    assert.equal((await service.receive(inbound('safe-1','Hello'),context)).status,'accepted');
    assert.equal((await service.receive(inbound('safe-2','Hello again'),context)).status,'accepted');
    assert.deepEqual(posts,['answer: Hello','answer: Hello again']);
    assert.equal(journal.getInbound(source)?.outcome,'pending','the uncertain acknowledgement is neither resent nor claimed');
  }finally{await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
