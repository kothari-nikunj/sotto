import assert from 'node:assert/strict';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import test from 'node:test';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {unavailableAuthority, type SourceAuthority} from '../src/agent/source-authority.js';
import {startApplication} from '../src/application.js';
import type {OperationalJournal} from '../src/journal/index.js';

const revision = 'a'.repeat(64);
const pulse = 'Fresh message check unavailable; retained permitted observations may be older.\n' +
  'Lena Example — Review the Orchard invitation spreadsheet. I can draft a reply to Lena Example.';
const draft = 'Lena, I’ll review the Orchard invitation spreadsheet.';

for (const mode of ['steady', 'transient', 'revoked', 'unavailable', 'transient_then_revoked', 'delivery_revoked', 'delivery_transient', 'delivery_outage_recovers', 'delivery_outage_revokes', 'fresh_session', 'clock_rollback'] as const) {
  test(`private draft after delivered pulse and thanks, final source check ${mode}`, async t => {
    // Every fictional provider receipt and journal write uses the same ordered clock.
    let fixtureMs = Date.now();
    t.mock.method(Date,'now',() => ++fixtureMs);
    const folder = await mkdtemp(join(tmpdir(), 'pulse-draft-'));
    const granted = (rev: string): SourceAuthority => ({
      version:2, revision:rev, complete:true, bridge_sources:[], google_sources:['gmail'],
    });
    let final = false, checks = 0, checksAtGenerationEnd = 0, calls = 0, journal!: OperationalJournal;
    let offlineAtDelivery = true;
    const sent: string[] = [];
    const hasPulse: boolean[] = [];
    const runtime = createSottoAgentRuntime({
      storagePath:join(folder, 'memory.db'), instructions:'Draft in chat.',
      sourceAuthority:{read:async () => {
        if (!final) return granted(revision);
        checks++;
        if (mode.startsWith('delivery_outage') && checks >= 2) {
          if (offlineAtDelivery) return unavailableAuthority();
          return granted(mode==='delivery_outage_revokes' ? 'b'.repeat(64) : revision);
        }
        if (mode === 'delivery_transient' && checks === 2) return unavailableAuthority();
        if (mode === 'unavailable' || (checks === 1 && ['transient','transient_then_revoked','delivery_revoked'].includes(mode))) return unavailableAuthority();
        return granted(mode === 'revoked' || mode === 'transient_then_revoked' || (mode === 'delivery_revoked' && checks >= 3) ? 'b'.repeat(64) : revision);
      }},
      model:new MockLanguageModelV4({doGenerate:async request => {
        calls++;
        const prompt = JSON.stringify(request.prompt);
        if (calls === 4) {
          assert.match(prompt,/Hello again/);
          if (mode !== 'delivery_outage_recovers') assert.ok(!prompt.includes(draft));
          return {content:[{type:'text',text:'Hello again!'}],finishReason:{unified:'stop',raw:'STOP'},
            usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]};
        }
        let text = 'Good morning.';
        if (calls >= 2) {
          hasPulse.push(request.prompt.some(msg => msg.role === 'assistant' && JSON.stringify(msg.content).includes('Orchard invitation spreadsheet')));
          text = 'I can draft the reply to Lena.';
        }
        if (calls === 3) {
          assert.equal(prompt.includes('Thanks!'), mode !== 'fresh_session');
          if (mode === 'fresh_session') assert.doesNotMatch(prompt,/Orchard invitation spreadsheet/);
          assert.match(prompt, /Yes draft reply to Lena please/);
          final = true;
          text = mode === 'fresh_session' ? 'What would you like to say to Lena?' : draft;
        }
        return {content:[{type:'text', text}],finishReason:{unified:'stop',raw:'STOP'},
          usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[]};
      }}),
    });
    const token = 'fictional-pulse-draft-bearer-0000';
    const start = () => startApplication({directory:join(folder,'state'),mediaRoot:folder,bearerToken:token,
      owner:{channel:'photon',accountId:'account',ownerId:'owner'},lineKey:'line',target:'owner',providerThreadId:'thread'},
    {agent:{...runtime,generateChat:async input => {
      try {return await runtime.generateChat(input);}
      finally {if (final) checksAtGenerationEnd = checks;}
    }},onJournalReady(value) {journal=value;},transport:{send:async input => {
      sent.push(input.text??'');
      if (mode === 'clock_rollback' && sent.length === 1) fixtureMs += 10_000;
      return {acceptance:'accepted',providerMessageIds:['msg-'+sent.length],providerSentAtMs:Date.now()};
    }}});
    let app = await start();
    const receive = (eventId:string,text:string) => app.receive(
      {channel:'photon',accountId:'account',senderId:'owner',isDirect:true,eventId,text},
      {threadId:'thread',sentAtMs:Date.now()});
    try {
      await receive('hello','Hello.');
      if (mode === 'clock_rollback') fixtureMs -= 10_000;
      const response = await fetch(`http://127.0.0.1:${app.port}/v1/send`, {method:'POST',
        headers:{authorization:'Bearer '+token,'content-type':'application/json'},
        body:JSON.stringify({dispatch_id:'pulse',target:'owner',text:pulse,source_revision:revision})});
      assert.equal(response.status,200);
      assert.equal((await response.json()).acceptance,'accepted');
      await receive('thanks','Thanks!');
      if (mode === 'clock_rollback') {
        const row = journal.db.prepare("SELECT continuation_json FROM inbound_events WHERE source_id='thanks'").get();
        assert.deepEqual(JSON.parse(String(row?.continuation_json)), {version:1,eligible:false});
        assert.deepEqual(hasPulse,[false], 'conflicting timestamps cannot select the pulse');
        return;
      }
      if (mode === 'fresh_session') await app.rotateSessions();
      await receive('draft','Yes draft reply to Lena please');
      assert.deepEqual(hasPulse,[true,mode!=='fresh_session']);
      assert.equal(sent[2],'I can draft the reply to Lena.');
      assert.equal(calls,3,'each owner turn generated exactly once');
      assert.equal(checksAtGenerationEnd,['steady','revoked','delivery_transient','delivery_outage_recovers','delivery_outage_revokes','fresh_session'].includes(mode) ? 1 : 2);
      if (mode === 'delivery_revoked') {
        const afterDraft=[...sent]; // Record the owner's experience before any repair attempt.
        await app.close();app=await start();
        await app.close();app=await start();
        await receive('next','Hello again');
        assert.equal(sent.at(-1),'Hello again!','the older private answer must not swallow new owner requests');
        assert.equal(calls,4,'recovery must not regenerate the old draft');
        assert.equal(afterDraft.length,4);
        assert.match(afterDraft[3]!,/Source access changed/);
        assert.equal(sent.filter(reply=>reply===afterDraft[3]).length,1);
        assert.equal(journal.db.prepare("SELECT count(*) AS n FROM dispatch_attempts a JOIN prepared_replies p ON p.dispatch_id=a.dispatch_id WHERE p.payload=?").get(draft)!.n,0);
      } else if(mode==='delivery_outage_recovers'||mode==='delivery_outage_revokes') {
        assert.equal(sent.length,3,'no contact while both delivery checks fail');
        await app.close();app=await start();
        assert.equal(sent.length,3);
        offlineAtDelivery=false;
        await receive('next','Hello again');
        assert.equal(sent.at(-1),'Hello again!');
        assert.equal(calls,4);
        assert.equal(sent.length,5);
        if(mode==='delivery_outage_recovers') assert.equal(sent[3],draft);
        else assert.match(sent[3]!,/Source access changed/);
      } else assert.equal(sent.length,4);
      if(mode==='unavailable') assert.match(sent[3]!,/couldn’t verify access/);
      if(mode==='revoked'||mode==='transient_then_revoked') assert.match(sent[3]!,/Source access changed/);
      assert.equal(sent.includes(draft),['steady','transient','delivery_transient','delivery_outage_recovers'].includes(mode),sent.at(-1) ?? 'missing reply');
      assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
    } finally {await app.close();await runtime.close();await rm(folder,{recursive:true,force:true});}
  });
}

for (const context of ['accepted offer', 'unavailable standing context'] as const) {
  for (const finalState of ['unchanged','revoked','cancelled'] as const) {
    test(`before generation, ${context} with an unavailable proof then ${finalState}`, async () => {
      const folder = await mkdtemp(join(tmpdir(), 'pulse-context-'));
      const controller = new AbortController();
      const reason = new Error('cancelled before the next read');
      let reads = 0;
      const model = new MockLanguageModelV4({doGenerate:async () => ({
        content:[{type:'text',text:draft}],finishReason:{unified:'stop',raw:'STOP'},
        usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[],
      })});
      const runtime = createSottoAgentRuntime({
        storagePath:join(folder,'memory.db'), model, instructions:'Draft in chat.',
        readMasterContext:context === 'unavailable standing context' ? async () => {throw Error('offline');} : undefined,
        sourceAuthority:{read:async () => {
          if (++reads === 2) {
            if (finalState === 'cancelled') controller.abort(reason);
            return unavailableAuthority();
          }
          return {version:2,complete:true,revision:finalState==='revoked' && reads>=3 ? 'b'.repeat(64) : revision,
            bridge_sources:[],google_sources:['gmail']};
        }},
      });
      try {
        const result = runtime.generateChat({text:'Draft a reply to Lena',threadId:'thread',ownerId:'owner',
          signal:controller.signal,
          acceptedBackgroundContext:context === 'accepted offer' ? {sourceRevision:revision,text:pulse} : undefined});
        if (finalState === 'unchanged') {
          assert.equal((await result).text,draft);
          assert.equal(model.doGenerateCalls.length,1);
          assert.equal(reads,4); // Initial, context fence, fresh proof, completed answer.
        } else {
          await assert.rejects(result, finalState==='cancelled' ? error => error===reason : /result was withheld/);
          assert.equal(model.doGenerateCalls.length,0);
          assert.equal(reads,finalState==='cancelled'?2:3);
        }
      } finally {await runtime.close();await rm(folder,{recursive:true,force:true});}
    });
  }
}

for (const restored of ['original', 'revoked', 'cancelled'] as const) {
  test(`previously verified draft thread, missed initial consent check then ${restored}`, async () => {
    const folder = await mkdtemp(join(tmpdir(), 'pulse-initial-'));
    const controller = new AbortController(), reason = new Error('owner cancelled');
    let drafting = false, reads = 0;
    const model = new MockLanguageModelV4({doGenerate:async request => ({
      content:[{type:'text',text:JSON.stringify(request.prompt).includes('Orchard invitation spreadsheet') ? draft : 'Please share the request.'}],
      finishReason:{unified:'stop',raw:'STOP'},
      usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}},warnings:[],
    })});
    const runtime = createSottoAgentRuntime({storagePath:join(folder,'memory.db'), model, instructions:'Draft in chat.',
      sourceAuthority:{read:async () => {
        if (drafting && ++reads === 1) {
          if (restored === 'cancelled') controller.abort(reason);
          return unavailableAuthority();
        }
        return {version:2,complete:true,revision:drafting && restored === 'revoked' ? 'b'.repeat(64) : revision,
          bridge_sources:[],google_sources:['gmail']};
      }},
    });
    try {
      const thread = {threadId:'thread',ownerId:'owner'};
      await runtime.generateChat({...thread,text:'Hello'});
      drafting = true;
      const answer = runtime.generateChat({...thread,text:'Yes, draft a reply',signal:controller.signal,
        acceptedBackgroundContext:{sourceRevision:revision,text:pulse}});
      if (restored === 'cancelled') {
        await assert.rejects(answer, error => error === reason);
        assert.equal(reads,1);
        assert.equal(model.doGenerateCalls.length,1);
      } else {
        const result = await answer;
        const prompt = JSON.stringify(model.doGenerateCalls.at(-1)!.prompt);
        assert.equal(prompt.includes('Orchard invitation spreadsheet'),restored === 'original');
        assert.equal(result.text,restored === 'original' ? draft : 'Please share the request.');
        assert.equal(model.doGenerateCalls.length,2);
      }
    } finally {await runtime.close();await rm(folder,{recursive:true,force:true});}
  });
}
