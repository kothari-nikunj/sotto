import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import {DatabaseSync} from 'node:sqlite';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {readSkillIndex} from '../src/tools/skills.js';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import { existsSync } from 'node:fs';
import { mkdtemp, rm, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { createBriefDetailTool, BRIEF_DETAIL_EXECUTION_GUIDE, readBriefDetail } from '../src/agent/brief-detail.js';

const here = dirname(fileURLToPath(import.meta.url));
const sourceRoot = process.env.SOTTO_FIXTURE_SOURCE_ROOT || resolve(here, '../../../');
const child = join(here, 'brief-detail-receiver-child.py');
function boundedClose(promise: Promise<{ code: number | null; signal: NodeJS.Signals | null }>) {
  return new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolveClose, reject) => {
    const timer = setTimeout(() => reject(new Error('brief receiver did not stop')), 20_000);
    promise.then(value => { clearTimeout(timer); resolveClose(value); }, error => {
      clearTimeout(timer); reject(error);
    });
  });
}

for (const channel of ['photon', 'telegram'] as const) {
test(`actual receiver handler serves only current accepted ${channel} brief through typed Node client`,
  { timeout: 90_000 }, async () => {
    const data = await mkdtemp(join(tmpdir(), 'sotto-brief-detail-'));
    const bundled = join(sourceRoot, '.venv/bin/python');
    const python = process.env.PYTHON || (existsSync(bundled) ? bundled : 'python3');
    const proc = spawn(python, ['-I', '-B', child, sourceRoot, data, channel], {
      env: { PATH: process.env.PATH ?? '/usr/bin:/bin', LANG: 'C.UTF-8', HOME: data },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    let errorOutput = '';
    proc.stdout.setEncoding('utf8');
    proc.stderr.setEncoding('utf8');
    proc.stderr.on('data', chunk => { errorOutput = (errorOutput + String(chunk)).slice(-4096); });
    const closed = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolveClose, reject) => {
      proc.once('close', (code, signal) => resolveClose({ code, signal }));
      proc.once('error', reject);
    });
    void closed.catch(() => undefined);
    const ready = new Promise<{ url: string; bearer: string; artifact: string; long_artifact: string; revision: string; date:string }>((resolveReady, reject) => {
      const timer = setTimeout(() => reject(new Error('brief receiver startup timed out')), 20_000);
      proc.stdout.on('data', chunk => {
        output += String(chunk);
        if (output.length > 8192) { clearTimeout(timer); reject(new Error('brief receiver output exceeded bound')); return; }
        const line = output.split('\n').find(value => value.startsWith('{"ready":'));
        if (!line) return;
        try {
          const value = JSON.parse(line) as { ready: boolean; url: string; bearer: string;
            artifact: string; long_artifact: string; revision: string; date:string };
          if (!value.ready || !value.url.startsWith('http://127.0.0.1:') ||
              !/^[a-f0-9]{24}$/.test(value.artifact) || !/^[a-f0-9]{24}$/.test(value.long_artifact) ||
              !/^[a-f0-9]{64}$/.test(value.revision)) throw new Error();
          clearTimeout(timer);
          resolveReady(value);
        } catch { clearTimeout(timer); reject(new Error('brief receiver readiness was invalid')); }
      });
      proc.once('close', () => { clearTimeout(timer); reject(new Error(`brief receiver exited: ${errorOutput}`)); });
    });
    try {
      const fixture = await ready;
      const sourceAuthority = { read: async () => ({ version: 2 as const, complete: true,
        revision: fixture.revision, google_sources: [], bridge_sources: [] }) };
      const options = { bridgeUrl: fixture.url, readBearer: fixture.bearer, sourceAuthority };
      const latest = await readBriefDetail(options, { latest: true, offset: 0, limit: 80 }, fixture.revision);
      assert.equal(latest.status, 'found');
      if (latest.status !== 'found') throw new Error('expected proved brief');
      assert.equal(latest.id, fixture.artifact);
      assert.match(latest.text, /Accepted owner detail/);
      assert.equal(JSON.stringify(latest).includes('route_digest'), false);
      assert.equal(JSON.stringify(latest).includes('payload_hash'), false);
      const exact = await readBriefDetail(options, { artifact_id: fixture.artifact, offset: 0, limit: 80 }, fixture.revision);
      assert.deepEqual(exact, latest);
      const longPage = await readBriefDetail(options, { artifact_id: fixture.long_artifact,
        offset: 0, limit: 4_000 }, fixture.revision);
      assert.equal(longPage.status, 'found');
      if (longPage.status !== 'found') throw new Error('expected long proved page');
      assert.equal(Array.from(longPage.text).length, 4_000);
      assert.equal(longPage.text, '😀'.repeat(4_000));
      const outboxPath=join(data,'data/events/outbox.json');
      const beforeOutbox=await readFile(outboxPath,'utf8');
      const archivePath=join(data,'data/briefs',fixture.date+'_morning.json');
      const beforeArchive=await readFile(archivePath,'utf8');
      const journal=new OperationalJournal(join(data,'operations.sqlite'));
      let runtime:ReturnType<typeof createSottoAgentRuntime>|undefined;
      let coordinator:ConversationCoordinator|undefined;
      const calls=[{name:'skill',input:{name:'sotto-ask'}},
        {name:'accepted_brief_detail',input:{date:fixture.date,query:'cEdAr',kind:'brief',offset:0,limit:80}},null,
        {name:'accepted_brief_detail',input:{artifact_id:fixture.artifact,offset:0,limit:20}},
        {name:'accepted_brief_detail',input:{artifact_id:fixture.artifact,offset:20,limit:4000}},null];
      let step=0;const requests:unknown[]=[];
      const nativeTool=createBriefDetailTool({...options,fetch:async(input,init)=>{
        requests.push(JSON.parse(String(init!.body)));const reply=await fetch(input,init);
        const body=await reply.clone().json();
        if(body.status==='ambiguous'){assert.equal(body.matches.length,2);assert.ok(!JSON.stringify(body).includes('Withheld'));assert.ok(!JSON.stringify(body).includes('full_text'));}
        return reply;
      }});
      const usage={inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}};
      const model=new MockLanguageModelV4({doGenerate:async request=>{
        const prompt=JSON.stringify(request.prompt);
        if(step===1){assert.ok(prompt.includes('A photo brief the user already received'),'actual original Ask skill loaded');
          assert.ok(prompt.includes('name at most three grounded actions you can actually help perform'),
            'ordinary accepted-brief chat receives bounded help-choice guidance');}
        if(step===2){assert.ok(prompt.includes('ambiguous'));assert.ok(!prompt.includes('Cedar procurement saved briefing detail'));}
        const call=calls[step++];return {content:call?[{type:'tool-call' as const,toolCallId:`gallery-${step}`,toolName:call.name,input:JSON.stringify(call.input)}]:[{type:'text' as const,text:step===3?'Which do you mean: Accepted morning brief or Accepted evening Cedar brief?':'The selected accepted morning brief says Cedar procurement saved briefing detail.'}],finishReason:{unified:call?'tool-calls' as const:'stop' as const,raw:'STOP'},usage,warnings:[]};
      }});
      try {
        runtime=createSottoAgentRuntime({model,storagePath:join(data,'memory.db'),instructions:BRIEF_DETAIL_EXECUTION_GUIDE,
          skillPaths:(await readSkillIndex(join(sourceRoot,'sotto-chief-of-staff'))).map(entry=>entry.path),sourceAuthority,chatTools:()=>({accepted_brief_detail:nativeTool})});
        const sent:string[]=[];
        const ownerId = channel === 'telegram' ? '123456789' : '+15555550123';
        coordinator=new ConversationCoordinator({journal,ownerPolicy:{channel,accountId:'account-1',ownerId},model:{generate:request=>runtime!.generateChat({...request,threadId:'memory',ownerId:'owner'})},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`gallery-answer-${sent.length}`};}});
        const context={threadId:channel === 'telegram' ? 'telegram:123456789' : 'imessage:iMessage;-;+15555550123~+15555550999',destination:channel,lineKey:'line-1'};
        const inbound=(id:string,text:string)=>({channel,accountId:'account-1',senderId:ownerId,isDirect:true,eventId:id,text});
        await coordinator.receive(inbound('gallery-question','Expand Cedar in today’s brief'),context);
        assert.match(sent[0]!,/Which do you mean/);assert.equal(requests.length,1,'ambiguity must wait for owner choice');
        await coordinator.receive(inbound('gallery-choice','The Accepted morning brief'),context);
        assert.match(sent[1]!,/Cedar procurement saved briefing detail/);assert.equal(requests.length,3);assert.equal(step,6);
        assert.equal(journal.db.prepare('SELECT count(*) AS n FROM actions').get()!.n,0);
        const queuePath=join(data,'data/events/work.sqlite3');
        if(existsSync(queuePath)){const queue=new DatabaseSync(queuePath,{readOnly:true});try{assert.equal(queue.prepare('SELECT count(*) AS n FROM jobs').get()!.n,0);}finally{queue.close();}}
        assert.equal(await readFile(outboxPath,'utf8'),beforeOutbox);assert.equal(await readFile(archivePath,'utf8'),beforeArchive);
      }finally{await coordinator?.close();await runtime?.close();journal.close();}
      await assert.rejects(readBriefDetail({ ...options, readBearer: 'wrong-bearer' },
        { latest: true, offset: 0, limit: 80 }, fixture.revision), /unavailable/);
      if (channel === 'telegram') {
        const linkPath = join(data, 'data/telegram-link.json');
        const original = await readFile(linkPath, 'utf8');
        const linked = JSON.parse(original) as {bot_token: string; allowed_user: number};
        for (const changed of [{...linked, allowed_user: 999},
          {...linked, bot_token: '123456789:rotatedSecret_ABCdef0123456789'}]) {
          await writeFile(linkPath, JSON.stringify(changed));
          await assert.rejects(readBriefDetail(options,
            {latest: true, offset: 0, limit: 80}, fixture.revision), /unavailable/);
          await writeFile(linkPath, original);
          assert.equal((await readBriefDetail(options,
            {latest: true, offset: 0, limit: 80}, fixture.revision)).status, 'found');
        }
      }
      await writeFile(join(data, 'revoked'), 'revoke');
      const hidden = await readBriefDetail(options, { latest: true, offset: 0, limit: 80 }, fixture.revision);
      assert.deepEqual(hidden, { status: 'withheld', coverage: 'accepted_artifacts_only' });
    } finally {
      let failure: unknown;
      if (proc.pid !== undefined && proc.exitCode === null && proc.signalCode === null) {
        proc.kill('SIGTERM');
        try {
          const result = await boundedClose(closed);
          assert.deepEqual(result, { code: 0, signal: null });
        } catch (error) {
          failure = error;
          if (proc.exitCode === null && proc.signalCode === null) proc.kill('SIGKILL');
          await closed.catch(() => undefined);
        }
      }
      await rm(data, { recursive: true, force: true });
      if (failure) throw failure;
    }
  });
}
