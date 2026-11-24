import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { ConversationCoordinator, type ModelRequest, type PreparedPost } from '../src/conversation.js';
import type { Inbound, OwnerPolicy } from '../src/channels/types.js';
import { OperationalJournal } from '../src/journal/index.js';
import { OWNER_ONLY_CONTEXT_PROOF, SourceAuthorityChanged } from '../src/agent/source-authority.js';
import { REPLY_UNCONFIRMED_NOTICE } from '../src/reply-unconfirmed.js';

const ownerPolicy: OwnerPolicy = { channel: 'photon', accountId: 'account', ownerId: 'owner' };
const context = { threadId: 'thread', destination: 'owner-destination', lineKey: 'line' };
const inbound = (eventId: string, text: string): Inbound =>
  ({ channel: 'photon', accountId: 'account', senderId: 'owner', isDirect: true, eventId, text });
type Outcome = 'accepted' | 'unknown' | 'rejected' | 'throw';

async function withService(outcomes: Outcome[], work: (service: ConversationCoordinator, journal: OperationalJournal,
  sent: PreparedPost[], generated: ModelRequest[]) => Promise<void>): Promise<void> {
  const dir = mkdtempSync(join(tmpdir(), 'sotto-unconfirmed-'));
  const journal = new OperationalJournal(join(dir, 'journal.sqlite3'));
  const sent: PreparedPost[] = [];
  const generated: ModelRequest[] = [];
  const service = new ConversationCoordinator({ journal, ownerPolicy,
    model: { generate: async request => { generated.push(request); return 'answer to ' + request.text; } },
    postPrepared: async request => {
      sent.push(request);
      const outcome = outcomes.shift() ?? 'accepted';
      if (outcome === 'throw') throw new Error('socket reset');
      return outcome === 'accepted' ? { acceptance: 'accepted', messageId: 'p' + sent.length } : { acceptance: outcome };
    } });
  try { await work(service, journal, sent, generated); }
  finally { journal.close(); rmSync(dir, { recursive: true, force: true }); }
}
const eventId = (journal: OperationalJournal, source: string) =>
  (journal.db.prepare('SELECT id FROM inbound_events WHERE source_id=?').get(source) as { id: string }).id;

test('an unconfirmed answer gets one notice before the next answer and its question resolves', async () => {
  await withService(['throw'], async (service, journal, sent, generated) => {
    assert.equal((await service.receive(inbound('q1', 'first question'), context)).status, 'held');
    assert.equal(journal.getInbound(eventId(journal, 'q1'))?.outcome, 'pending');
    assert.equal((await service.receive(inbound('q2', 'second question'), context)).status, 'accepted');
    assert.deepEqual(sent.map(post => post.text),
      ['answer to first question', REPLY_UNCONFIRMED_NOTICE, 'answer to second question']);
    assert.equal(journal.getInbound(eventId(journal, 'q1'))?.outcome, 'resolved');
    assert.deepEqual(generated.map(request => request.text), ['first question', 'second question']);
    await service.receive(inbound('q3', 'third question'), context);
    assert.equal(sent.filter(post => post.text === REPLY_UNCONFIRMED_NOTICE).length, 1);
  });
});

test('recovery alone delivers the notice; an unknown notice is never repeated', async () => {
  await withService(['unknown', 'unknown'], async (service, journal, sent) => {
    await service.receive(inbound('q1', 'first question'), context);
    await service.recover();
    await service.recover();
    await service.receive(inbound('q2', 'second question'), context);
    assert.deepEqual(sent.map(post => post.text),
      ['answer to first question', REPLY_UNCONFIRMED_NOTICE, 'answer to second question']);
  });
});

test('a rejected notice is tried once more, then never again', async () => {
  await withService(['unknown', 'rejected', 'rejected'], async (service, _journal, sent) => {
    await service.receive(inbound('q1', 'first question'), context);
    for (let i = 0; i < 3; i++) await service.recover();
    assert.equal(sent.filter(post => post.text === REPLY_UNCONFIRMED_NOTICE).length, 2);
  });
});

for (const state of ['contacted','uncertain','pending action','executing action','unknown action','covered prior run','wrong_route','wrong_scope','other_notice'] as const) {
  test(`a revoked-answer notice cannot replace ${state} work`, () => withService([],async (_service,journal) => {
    const admit=(eventId:string)=>String(journal.admitInbound({eventId,accountKey:'account',lineKey:'line',
      channel:'photon',sender:'owner',threadKey:'thread',payload:'{}'}).event.id);
    const id=admit('original'), other=admit('other');
    if (state === 'covered prior run') {
      journal.startRun({runId:'prior-run',eventId:other});
      journal.recordActionIntent({actionId:'older-write',runId:'prior-run',kind:'test-action',payloadHash:'fixture'});
      journal.beginAction('older-write');
      journal.settleAction('older-write','unknown');
    }
    journal.startRun({runId:'run',eventId:id});
    const input = {runId:'run',eventId:id,replyId:'original',dispatchId:'original',kind:'interactive' as const,
      coveredEventIds:state==='covered prior run'?[id,other]:[id],destination:'owner',routeDigest:'a'.repeat(64),sourceRevision:'b'.repeat(64),payload:'private'};
    journal.prepareReply({...input,...(state==='other_notice'?{purpose:'model_failure' as const}:{})});
    if(state==='contacted'||state==='uncertain') journal.beginDispatch(input.dispatchId);
    if(state==='uncertain') journal.settleDispatch(input.dispatchId,1,'unknown');
    if (state.endsWith(' action')) {
      journal.recordActionIntent({actionId:'write',runId:'run',kind:'bridge-send',payloadHash:'fixture'});
      if (state !== 'pending action') journal.beginAction('write');
      if (state === 'unknown action') journal.settleAction('write','unknown');
    }
    const before=journal.getDispatch(input.dispatchId)!.status;
    assert.throws(()=>journal.prepareReply({...input,replacesUnsent:'original',dispatchId:'notice',replyId:'notice',
      sourceRevision:'owner-only-context-v1',payload:'safe notice',purpose:'source_withheld',
      routeDigest:state==='wrong_route'?'c'.repeat(64):input.routeDigest,coveredEventIds:state==='wrong_scope'?[id,other]:input.coveredEventIds}),
      /cannot be retired/);
    assert.equal(journal.getDispatch(input.dispatchId)!.status,before);
    assert.equal(journal.getDispatch('notice'),undefined);
  }));
}

for (const mode of ['settled revoke', 'persistent outage', 'rejected notice', 'unknown notice', 'persistent outage and rejection', 'legacy rejected notice', 'notice not attempted'] as const) {
  test(`a later owner turn cannot replay the private answer or its action: ${mode}`, async () => {
    const folder = mkdtempSync(join(tmpdir(), 'held-answer-'));
    const journal = new OperationalJournal(join(folder, 'journal.sqlite3'));
    const generated:string[] = [], sent:string[] = [];
    const proof = 'a'.repeat(64);
    const route = 'b'.repeat(64);
    const service = new ConversationCoordinator({journal, ownerPolicy, routeDigestForThread: () => route,
      model:{generate:async request => {
        generated.push(request.text);
        if (request.text === 'original') {
          if (mode === 'settled revoke') {
            journal.recordActionIntent({actionId:'settled',runId:request.runId,kind:'test-action',payloadHash:'fixture'});
            journal.beginAction('settled');
            journal.settleAction('settled','succeeded');
          }
          return {text:'private answer', sourceRevision:proof};
        }
        return {text:'new answer', sourceRevision:OWNER_ONLY_CONTEXT_PROOF};
      }},
      canPostPrepared:async revision => {
        if (revision === OWNER_ONLY_CONTEXT_PROOF) return true;
        if (mode.startsWith('persistent outage')) return false;
        throw new SourceAuthorityChanged(true);
      }, postPrepared:async post => {
        sent.push(post.text);
        if (post.text !== 'new answer' && ['rejected notice','persistent outage and rejection','legacy rejected notice'].includes(mode)) return {acceptance:'rejected'};
        if (post.text !== 'new answer' && mode === 'notice not attempted') return {acceptance:'not_attempted'};
        if (post.text !== 'new answer' && mode === 'unknown notice') return {acceptance:'unknown'};
        return {acceptance:'accepted',messageId:'p'+sent.length};
      }});
    try {
      await service.receive(inbound('prior', 'original'), context);
      if (mode.startsWith('persistent outage')) {
        await service.recover();
        assert.deepEqual(sent,[],'a recovery pass alone keeps the unverified draft prepared');
      }
      if (mode === 'legacy rejected notice') journal.db.prepare("UPDATE prepared_replies SET purpose='model_failure' WHERE purpose='source_withheld'").run();
      await service.receive(inbound('next', 'unrelated new question'), context);
      assert.deepEqual(generated,['original','unrelated new question']);
      assert.equal(sent.at(-1),'new answer');
      assert.ok(!sent.includes('private answer'));
      assert.match(sent[0]!, mode.startsWith('persistent outage') ? /verify access/ : /access changed/);
      const expected = ['unknown notice','legacy rejected notice','notice not attempted'].includes(mode) ? 'pending' : ['rejected notice','persistent outage and rejection'].includes(mode) ? 'cancelled' : 'resolved';
      assert.equal(journal.getInbound(eventId(journal,'prior'))?.outcome,expected);
      assert.equal(sent.length,2);
      await service.recover();
      assert.equal(sent.length,2,'the old notice is never replayed');
      if (mode === 'notice not attempted') {
        const notice = journal.listRepliesForThread(eventId(journal,'prior')).find(row => row.purpose === 'source_withheld');
        assert.equal(notice?.dispatch_status,'cancelled','locally stopped before contact, distinct from provider rejection');
      }
      if(mode==='settled revoke')assert.equal(journal.getAction('settled')?.status,'succeeded');
    } finally { journal.close(); rmSync(folder,{recursive:true,force:true}); }
  });
}

for (const admission of ['after restart','before restart'] as const) for (const outcome of ['accepted','rejected','unknown','not_attempted','legacy rejected','still-unverifiable original'] as const) {
  test(`new input proceeds when restart resumes the previous prepared source notice: ${outcome}, ${admission}`, async () => {
    const folder = mkdtempSync(join(tmpdir(),'resume-source-notice-'));
    const path = join(folder,'journal.sqlite3');
    let journal = new OperationalJournal(path);
    const generated:string[] = [], attempted:string[] = [];
    const start = () => new ConversationCoordinator({journal,ownerPolicy,routeDigestForThread:()=> 'a'.repeat(64),
      canPostPrepared:async proof => {
        if(proof === OWNER_ONLY_CONTEXT_PROOF) return true;
        if(outcome==='still-unverifiable original') return false;
        throw new SourceAuthorityChanged(true);
      },
      model:{generate:async request => {
        generated.push(request.text);
        return request.text === 'A' ? {text:'private A',sourceRevision:'b'.repeat(64)}
          : {text:'answer B',sourceRevision:OWNER_ONLY_CONTEXT_PROOF};
      }},
      postPrepared:async request => {
        attempted.push(request.text);
        if(request.text === 'answer B' || outcome==='accepted' || outcome==='still-unverifiable original') return {acceptance:'accepted',messageId:'p'+attempted.length};
        return {acceptance:outcome === 'legacy rejected' ? 'rejected' : outcome};
      }});
    try {
      // Crash boundary: the notice committed, but no provider attempt began.
      journal.beginDispatch = () => { throw Error('fixture interrupted before beginDispatch'); };
      await start().receive(inbound('prior','A'),context);
      const oldId = eventId(journal,'prior');
      const prepared = journal.listRepliesForThread(oldId).find(row=>row.dispatch_status==='prepared')!;
      assert.equal(prepared.purpose,outcome==='still-unverifiable original'?null:'source_withheld');
      assert.deepEqual(attempted,[]);
      if(outcome==='legacy rejected') journal.db.prepare("UPDATE prepared_replies SET purpose='model_failure' WHERE id=?").run(String(prepared.id));
      if(admission==='before restart') admitLike(journal,oldId,'next','B');
      journal.close();
      journal = new OperationalJournal(path);
      const restarted = start();
      if(admission==='before restart') await restarted.recover();
      else await restarted.receive(inbound('next','B'),context);
      assert.equal(journal.getInbound(eventId(journal,'next'))?.outcome,'resolved','B gets its answer without another wakeup');
      assert.deepEqual(generated,['A','B']);
      assert.deepEqual(attempted,[new SourceAuthorityChanged(outcome!=='still-unverifiable original').notice,'answer B']);
      const expected = outcome==='rejected' ? 'cancelled' : ['accepted','still-unverifiable original'].includes(outcome) ? 'resolved' : 'pending';
      assert.equal(journal.getInbound(oldId)?.outcome,expected);
      await restarted.recover();
      assert.equal(attempted.length,2);
    } finally {journal.close();rmSync(folder,{recursive:true,force:true});}
  });
}


function admitLike(journal:OperationalJournal,referenceId:string,id:string,text:string):string {
  const previous = journal.getInbound(referenceId)!;
  const saved = JSON.parse(String(previous.payload));
  const event=journal.admitInbound({eventId:id,channel:'photon',accountKey:saved.inbound.accountId,lineKey:saved.context.lineKey,
    threadKey:saved.context.threadId,sender:saved.inbound.senderId,payload:JSON.stringify({...saved,inbound:inbound(id,text)})}).event;
  assert.equal(event.thread_key,previous.thread_key);
  return String(event.id);
}

for (const legacy of [false,true]) for (const disposition of ['accepted','unknown','not_attempted'] as const)
test(`an older unknown reply respects the prepared source notice (${disposition}, legacy=${legacy})`,async()=>{
  const folder=mkdtempSync(join(tmpdir(),'prior-unknown-')),path=join(folder,'journal.sqlite3');
  let journal=new OperationalJournal(path), granted=true;
  const attempts:string[]=[],generated:string[]=[];
  const start=()=>new ConversationCoordinator({journal,ownerPolicy,routeDigestForThread:()=> 'a'.repeat(64),
    canPostPrepared:async proof=>{if(granted||proof===OWNER_ONLY_CONTEXT_PROOF)return true;throw new SourceAuthorityChanged(true);},
    model:{generate:async req=>{generated.push(req.text);return {text:'answer '+req.text,sourceRevision:OWNER_ONLY_CONTEXT_PROOF};}},
    postPrepared:async req=>{
      assert.equal(req.destination,context.destination);
      attempts.push(req.text);
      if(req.text==='answer C')return {acceptance:'unknown'};
      if(req.dispatchId==='notice-a' && disposition!=='accepted')return {acceptance:disposition};
      return {acceptance:'accepted',messageId:'p'+attempts.length};
    }});
  try{
    await start().receive(inbound('c','C'),context);
    const c=eventId(journal,'c');
    journal.startRun({runId:'c-first-notice',eventId:c});
    journal.prepareReply({replyId:'notice-c',dispatchId:'notice-c',kind:'interactive',purpose:'reply_unconfirmed',sourceRevision:OWNER_ONLY_CONTEXT_PROOF,
      runId:'c-first-notice',eventId:c,destination:context.destination,payload:REPLY_UNCONFIRMED_NOTICE,routeDigest:'a'.repeat(64)});
    journal.beginDispatch('notice-c');journal.settleDispatch('notice-c',1,'rejected');
    const a=admitLike(journal,c,'a','A');
    journal.startRun({runId:'run-a',eventId:a});
    const input={runId:'run-a',eventId:a,destination:context.destination,kind:'interactive' as const,routeDigest:'a'.repeat(64)};
    journal.prepareReply({...input,replyId:'answer-a',dispatchId:'answer-a',sourceRevision:'b'.repeat(64),payload:'private answer A'});
    journal.prepareReply({...input,replyId:'notice-a',dispatchId:'notice-a',sourceRevision:OWNER_ONLY_CONTEXT_PROOF,purpose:'source_withheld',
      replacesUnsent:'answer-a',payload:new SourceAuthorityChanged(true).notice});
    if(legacy)journal.db.prepare("UPDATE prepared_replies SET purpose='model_failure' WHERE id='notice-a'").run();
    const b=admitLike(journal,a,'b','B');
    journal.close();journal=new OperationalJournal(path);granted=false;
    const restarted=start();
    await restarted.recover();
    assert.deepEqual(generated,['C','B']);
    assert.deepEqual(attempts,['answer C',new SourceAuthorityChanged(true).notice,REPLY_UNCONFIRMED_NOTICE,'answer B']);
    assert.equal(journal.getInbound(a)?.outcome,disposition==='accepted'?'resolved':'pending');
    assert.equal(journal.getInbound(b)?.outcome,'resolved');
    await restarted.recover();assert.deepEqual(generated,['C','B']);assert.equal(attempts.length,4);
  }finally{journal.close();rmSync(folder,{recursive:true,force:true});}
});
