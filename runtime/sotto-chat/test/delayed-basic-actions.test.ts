import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ConversationCoordinator, type PostReceipt} from '../src/conversation.js';
import {OperationalJournal} from '../src/journal/index.js';
import {identifierReviewHash} from '../src/tools/owner-task-tool-contract.js';

const scopeHash = 'a'.repeat(64);
const context = {threadId:'thread',destination:'owner-destination',lineKey:'line'};
const ownerPolicy = {channel:'photon' as const,accountId:'account',ownerId:'owner'};
const inbound = (eventId:string,text:string) => ({channel:'photon' as const,accountId:'account',senderId:'owner',isDirect:true,eventId,text});
const cases = [
  {kind:'task', args:{operation:'add',text:'Change it'}, subject:'the task change'},
  {kind:'reminder', args:{due:'2026-10-10T12:00:00-07:00',action:'Change it'}, subject:'the reminder change'},
  {kind:'preference', args:{operation:'clear-tone'}, subject:'the preference change'},
] as const;
type Item = typeof cases[number];

function prepare(journal:OperationalJournal,item:Item) {
  const source = journal.admitInbound({eventId:'source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
    payload:JSON.stringify({inbound:inbound('source','Change it'),context})}).event.id as string;
  journal.startRun({runId:'effect-run',eventId:source});
  const authority = {runId:'effect-run',...ownerPolicy,lineKey:'line',scopeHash};
  const action = item.kind === 'task' ? journal.prepareOwnerTaskToolAction(authority,source,item.args)
    : item.kind === 'reminder' ? journal.prepareOwnerReminderToolAction(authority,source,item.args)
    : journal.prepareOwnerPreferenceToolAction(authority,source,item.args);
  journal.beginAction(action.actionId);
  journal.settleAction(action.actionId,'unknown');
  const argsHash = journal.getAction(action.actionId)!.payload_hash as string;
  const binding = {version:1,status:'committed',action_id:action.actionId,event_id:source,payload_hash:action.payloadHash,args_hash:argsHash};
  const receipt = item.kind === 'task' ? {...binding,operation:'add',anchor_key:'saved-task',post_digest:'c'.repeat(64),loop_status:'open'}
    : item.kind === 'reminder' ? {...binding,reminder_id:`int_${'f'.repeat(32)}`,reminder_status:'scheduled',due:item.args.due,action:item.args.action}
    : {...binding,outcome:{operation:'clear-tone',count:0}};
  return {source,action,receipt};
}

function dependencies(journal:OperationalJournal,item:Item,client:never,postPrepared:(post:{text:string})=>Promise<PostReceipt>,models:{count:number}) {
  return {journal,ownerPolicy,model:{generate:async()=>{models.count++;return 'Safe answer.';}},postPrepared,
    ...(item.kind === 'task' ? {ownerTaskTool:client} : item.kind === 'reminder' ? {ownerReminderTool:client} : {ownerPreferenceTool:client})};
}

for (const item of cases) test(`${item.kind} rejected notices are bounded across restart; late rejection stays held`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-basic-'));
  const file=join(folder,'journal.sqlite');
  let journal=new OperationalJournal(file);
  let service:ConversationCoordinator|undefined;
  let notices=0,inspections=0,proof='unavailable';
  const models={count:0};
  try {
    const {source,action}=prepare(journal,item);
    const client={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
      assert.equal(operation,'inspect');inspections++;
      if(proof==='unavailable')throw Error('inspection unavailable');
      return {status:'rejected',code:'target_changed'};
    }} as never;
    const deps=()=>dependencies(journal,item,client,async post=>{
      assert.match(post.text,new RegExp(item.subject));notices++;return {acceptance:'rejected'};
    },models);
    service=new ConversationCoordinator(deps());
    await service.recover();assert.equal(notices,1);
    await service.recover();assert.equal(inspections,1);assert.equal(notices,1);
    await service.close();journal.close();
    journal=new OperationalJournal(file);journal.recoverInterrupted();service=new ConversationCoordinator(deps());
    await service.recover();assert.equal(inspections,1,'replacement preserves inspection cadence');
    journal.db.prepare("UPDATE dispatches SET updated_at=updated_at-61000 WHERE status='rejected'").run();
    journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.actionId);
    await service.recover();assert.equal(notices,2);
    journal.db.prepare("UPDATE dispatches SET updated_at=updated_at-61000 WHERE status='rejected'").run();
    await service.recover();assert.equal(notices,2,'notice contacts are capped');
    proof='rejected';
    journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.actionId);
    await service.recover();
    assert.equal(journal.getAction(action.actionId)?.status,'unknown');
    assert.equal(journal.getInbound(source)?.outcome,'pending');
    assert.equal(journal.listRepliesForThread(source).filter(reply=>reply.purpose!=='outcome_pending').length,0);
    assert.equal(models.count,0);
  } finally {await service?.close();try{journal.close();}catch{}rmSync(folder,{recursive:true,force:true});}
});

async function assertUnknownNoticeAndLateCommittedProof(item:Item) {
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-basic-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  const models={count:0};let prove=false,inspections=0;
  const posts:string[]=[];
  try {
    const {source,action,receipt}=prepare(journal,item);
    const client={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
      assert.equal(operation,'inspect','the effect is never rerun');inspections++;
      if(!prove)throw Error('lost effect receipt');return receipt;
    }} as never;
    service=new ConversationCoordinator(dependencies(journal,item,client,async post=>{
      posts.push(post.text);
      return post.text.includes('couldn’t confirm whether') ? {acceptance:'unknown'} : {acceptance:'accepted',messageId:'final-accepted'};
    },models));
    await service.recover();assert.equal(posts.length,1);assert.match(posts[0]!,new RegExp(item.subject));
    await service.recover();assert.equal(posts.length,1);
    prove=true;journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.actionId);
    await service.recover();
    assert.equal(journal.getAction(action.actionId)?.status,'succeeded');
    assert.equal(journal.getInbound(source)?.outcome,'resolved');
    assert.equal(posts.length,2);
    assert.equal(journal.listRepliesForThread(source).filter(reply=>reply.event_id===source).length,2);
    await service.recover();assert.equal(posts.length,2);assert.equal(inspections,2);assert.equal(models.count,0);
  } finally {await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
}
test('task unknown notice and late committed proof have distinct sends',()=>assertUnknownNoticeAndLateCommittedProof(cases[0]));
test('reminder unknown notice and late committed proof have distinct sends',()=>assertUnknownNoticeAndLateCommittedProof(cases[1]));
test('preference unknown notice and late committed proof have distinct sends',()=>assertUnknownNoticeAndLateCommittedProof(cases[2]));

for (const item of cases) test(`${item.kind} pending notice follows, but does not preempt, a fresh safe message`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-basic-safe-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  const posts:string[]=[];const models={count:0};
  try {
    prepare(journal,item);
    let inspections=0;
    const client={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
      assert.equal(operation,'inspect');inspections++;throw Error('inspection unavailable');
    }} as never;
    service=new ConversationCoordinator(dependencies(journal,item,client,async post=>{
      posts.push(post.text);return {acceptance:'accepted',messageId:`accepted-${posts.length}`};
    },models));
    // A running process: no recover() between the safe answer and the notice.
    assert.equal((await service.receive(inbound('safe','Hello'),context)).status,'accepted');
    assert.equal(posts.length,2);assert.equal(posts[0],'Safe answer.');assert.match(posts[1]!,new RegExp(item.subject));
    assert.equal(models.count,1);
    assert.equal((await service.receive(inbound('safe-2','Hello again'),context)).status,'accepted');
    assert.deepEqual(posts.slice(2),['Safe answer.'],'the notice is not repeated');
    assert.equal(inspections,1,'live turns respect the inspect cadence');
    await service.recover();
    assert.equal(posts.length,3);
  } finally {await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

for (const item of cases) test(`${item.kind} /stop suppresses late acknowledgement and leaves safe chat available`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-basic-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  const models={count:0};let prove=false;
  const posts:string[]=[];
  try {
    const {source,action,receipt}=prepare(journal,item);
    const client={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{
      assert.equal(operation,'inspect');if(!prove)throw Error('inspection unavailable');return receipt;
    }} as never;
    service=new ConversationCoordinator(dependencies(journal,item,client,async post=>{
      posts.push(post.text);return {acceptance:'accepted',messageId:`accepted-${posts.length}`};
    },models));
    await service.recover();assert.equal(posts.length,1);
    assert.equal((await service.receive(inbound('stop','/stop'),context)).status,'stopped');
    await service.drain();
    assert.equal(posts.filter(text=>text.startsWith('I stopped working on your earlier request.')).length,1);
    assert.equal(journal.getInbound(source)?.outcome,'cancelled');
    prove=true;journal.db.prepare('UPDATE actions SET updated_at=updated_at-61000 WHERE id=?').run(action.actionId);
    assert.equal((await service.receive(inbound('safe','Hello'),context)).status,'accepted');
    assert.equal(journal.getAction(action.actionId)?.status,'succeeded');
    assert.equal(posts.length,3,'only the earlier notice, stop acknowledgement, and safe answer are sent');
    assert.equal(posts[2],'Safe answer.');
    assert.equal(models.count,1);
    assert.equal(journal.listRepliesForThread(source).filter(reply=>reply.event_id===source).length,1);
  } finally {await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});

test('a delayed task proposal still recovers its supported preview',async()=>{
  const folder=mkdtempSync(join(tmpdir(),'sotto-delayed-task-preview-'));
  const journal=new OperationalJournal(join(folder,'journal.sqlite'));
  let service:ConversationCoordinator|undefined;
  try {
    const text='Save dana@example.com as an identifier';
    const source=journal.admitInbound({eventId:'source',accountKey:'account',lineKey:'line',threadKey:'thread',channel:'photon',sender:'owner',
      payload:JSON.stringify({inbound:inbound('source',text),context})}).event.id as string;
    journal.startRun({runId:'effect-run',eventId:source});
    const authority={runId:'effect-run',...ownerPolicy,lineKey:'line',scopeHash};
    const args={operation:'propose_identifier' as const,canonical_id:'c_123abc',identifier:'dana@example.com',source_revision:'b'.repeat(64)};
    const action=journal.prepareOwnerTaskToolAction(authority,source,args);
    journal.beginAction(action.actionId);journal.settleAction(action.actionId,'unknown');
    const review={operation:'identifier' as const,canonical_id:args.canonical_id,identifier:args.identifier,
      source_revision:args.source_revision,name:'Dana',normalized_identifier:args.identifier,present:false,review_hash:''};
    review.review_hash=identifierReviewHash(review);
    const preview={version:1 as const,status:'preview' as const,event_id:source,payload_hash:action.payloadHash,review};
    const client={identity:{line_key:'line'},scopeHash,call:async(operation:string)=>{assert.equal(operation,'inspect');return preview;}} as never;
    const posts:string[]=[];
    service=new ConversationCoordinator(dependencies(journal,cases[0],client,async post=>{
      posts.push(post.text);return {acceptance:'accepted',messageId:'review-accepted'};
    },{count:0}));
    await service.recover();
    assert.equal(journal.getAction(action.actionId)?.status,'succeeded');
    assert.equal(journal.getInbound(source)?.outcome,'resolved');
    assert.equal(posts.length,1);assert.match(posts[0]!,/Save identifier/);
  } finally {await service?.close();journal.close();rmSync(folder,{recursive:true,force:true});}
});
