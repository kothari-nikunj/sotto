import assert from 'node:assert/strict';
import test from 'node:test';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { MockLanguageModelV4 } from 'ai/test';
import { OperationalJournal } from '../src/journal/index.js';
import { ConversationCoordinator } from '../src/conversation.js';
import { createSottoAgentRuntime } from '../src/agent/runtime.js';
import { createOwnerGmailDraftClient } from '../src/tools/owner-gmail-draft-client.js';
import { createOwnerGmailDraftTools } from '../src/tools/owner-gmail-draft-tool.js';
import { createGoogleReadTools } from '../src/agent/google-read.js';
import { stableActionIdentity } from '../src/tools/action-gate.js';
import { RequestContext } from '@mastra/core/request-context';
import type { OwnerToolIdentity } from '../src/tools/owner-contract.js';
async function startReceiver(folder: string) {
  const python = resolve('../../.venv/bin/python');
  const child = spawn(process.env.SOTTO_PYTHON ?? (existsSync(python) ? python : 'python3'), [resolve('test/owner-loop-receiver-child.py')], { stdio: ['pipe', 'pipe', 'pipe'], env: { PATH: process.env.PATH, PYTHONDONTWRITEBYTECODE: '1',
      SOTTO_TEST_GMAIL_DRAFT: '1', SOTTO_HOME: join(folder, 'sotto-home'), SOTTO_ROUTINE_EPOCH: 'e'.repeat(32), SOTTO_TEST_ROUTINE_SOURCE_REVISION: 'a'.repeat(64), SOTTO_RUNTIME_PROFILE: 'managed-photon-fixed-five-v1', SOTTO_DEPLOYMENT_MODE: 'managed',
      SOTTO_DATA: folder, SOTTO_CRON_DELIVER: 'photon', SOTTO_TENANT_ID: 'tenant', SOTTO_ACCOUNT_ID: 'account',
      SOTTO_LINE_KEY: 'line', SOTTO_MCP_TOKEN: '0123456789abcdef0123456789abcdef',
      PHOTON_HOME_CHANNEL: '+15555550123', PHOTON_ALLOWED_USERS: '+15555550123', PHOTON_PROJECT_ID: 'fixture-project',
      PHOTON_PROJECT_SECRET: 'fixture-secret', PHOTON_ALLOW_ALL_USERS: 'false', SOTTO_IMESSAGE_NUMBER: '+15555550999',
      SOTTO_TIMEZONE: 'America/Los_Angeles' } });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr = (stderr + String(chunk)).slice(-4096); });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const line = await Promise.race([new Promise<string>((done, fail) => {
        let output = '';
        child.stdout.on('data', chunk => { output += String(chunk); const i = output.indexOf('\n'); if (i >= 0)
          done(output.slice(0, i)); });
        child.once('exit', code => fail(new Error(`receiver exited ${code}: ${stderr}`)));
        child.once('error', fail);
      }), new Promise<never>((_, fail) => { timer = setTimeout(() => fail(new Error('receiver start timeout')), 15000); })]);
    const parsed = JSON.parse(line) as {
      port: number;
      readBearer: string;
      identity: OwnerToolIdentity;
      bearer: string;
    };
    return { child, ...parsed, bridgeUrl: `http://127.0.0.1:${parsed.port}/mcp` };
  }
  catch (error) {
    child.kill('SIGKILL');
    throw error;
  }
  finally {
    if (timer)
      clearTimeout(timer);
  }
}
async function stopReceiver(child: ChildProcessWithoutNullStreams) {
  if (child.exitCode !== null)
    return;
  const ended = once(child, 'exit');
  child.stdin.end('quit\n');
  const timer = setTimeout(() => child.kill('SIGKILL'), 5000);
  try {
    await ended;
  }
  finally {
    clearTimeout(timer);
  }
}
const usage = { inputTokens: { total: 5, noCache: 5, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 2, text: 2, reasoning: 0 } };
const sourceAuthority = { read: async () => ({ version: 2 as const, revision: 'a'.repeat(64), complete: true, bridge_sources: [], google_sources: ['gmail' as const] }) };

test('actual Mastra Gmail read -> complete accepted review -> separate yes saves exact threaded MIME once across restart', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-gmail-connected-'));
  let journal = new OperationalJournal(join(folder, 'operations.sqlite'));
  let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
  let runtime: ReturnType<typeof createSottoAgentRuntime> | undefined;
  try {
    receiver = await startReceiver(join(folder, 'receiver'));
    const client = createOwnerGmailDraftClient(receiver);
    const tools = createOwnerGmailDraftTools({client, journal: () => journal, sourceAuthority});
    const reads = createGoogleReadTools({...receiver, sourceAuthority});
    const ownerPolicy = {channel: 'photon' as const, accountId: 'account', ownerId: receiver.identity.owner_id};
    const context = {threadId: receiver.identity.route_id, destination: receiver.identity.owner_id, lineKey: 'line'};
    const inbound = (eventId: string, text: string) => ({channel: 'photon' as const, accountId: 'account', senderId: receiver!.identity.owner_id, isDirect: true, eventId, text});
    const body = 'I reviewed the cedar résumé.\nThank you.';
    let step = 0;
    const calls = [{name:'gmail_get',args:{message_id:'message-123'}}, {name:'gmail_draft_propose',args:{message_id:'message-123',body}}, null, {name:'gmail_draft_save',args:{}}, null];
    const model = new MockLanguageModelV4({doGenerate: async options => {
      const call = calls[step++];
      if (step === 2) assert.ok(JSON.stringify(options.prompt).includes('Alice <alice@example.com>'));
      return {content: call ? [{type:'tool-call' as const,toolCallId:`gmail-${step}`,toolName:call.name,input:JSON.stringify(call.args)}] : [{type:'text' as const,text:'model completed'}],finishReason:{unified:call?'tool-calls':'stop',raw:'STOP'},usage,warnings:[]};
    }});
    runtime = createSottoAgentRuntime({model,storagePath:join(folder,'memory.db'),instructions:'Read actual Gmail message then review draft; save on separate yes.',sourceAuthority,chatTools:async()=>({...reads,...tools})});
    const sent: string[] = [];
    const make = () => new ConversationCoordinator({journal,ownerPolicy,ownerGmailDraft:client,model:{generate:r=>runtime!.generateChat({...r,threadId:'memory',ownerId:'owner'})},canPostPrepared:r=>runtime!.canDeliverReplySource(r),postPrepared:async r=>{sent.push(r.text);return {acceptance:'accepted',messageId:`ack-${sent.length}`};}});
    let coordinator = make();
    assert.equal((await coordinator.receive(inbound('review','Draft a reply to Alice about the cedar packet'),context)).status,'accepted');
    assert.equal(sent[0],`To: alice@example.com\nSubject: Re: Cedar review\n\n${body}\n\nSave as a Gmail draft? Nothing will be sent.`);
    const providerPath = join(folder,'receiver/provider-calls.jsonl');
    assert.equal(existsSync(providerPath),false);
    assert.equal((await coordinator.receive(inbound('yes','Yes, please.'),context)).status,'accepted');
    assert.equal(sent[1],'Saved your Gmail draft. Nothing was sent.');
    const contacts = readFileSync(providerPath,'utf8').trim().split('\n').map(line=>JSON.parse(line));
    assert.equal(contacts.length,1);assert.equal(contacts[0].operation,'drafts.create');
    const message=contacts[0].args.body.message;
    assert.equal(message.threadId,'thread-123');
    const mime=Buffer.from(message.raw,'base64url').toString();
    assert.match(mime,/To: alice@example.com/);assert.match(mime,/Subject: Re: Cedar review/);
    assert.match(mime,/In-Reply-To: <cedar@example.com>/);assert.match(mime,/References: <cedar@example.com>/);
    // MIMEText uses base64 for the UTF-8 body; verify decoded bytes exactly.
    assert.equal(Buffer.from(mime.split('\n\n')[1]!.replace(/\s/g,''),'base64').toString(),body);
    const action=journal.db.prepare("SELECT * FROM actions WHERE kind='owner_gmail_draft_tool' AND json_extract(result_json,'$.args.operation')='save'").get()!;
    const saved=journal.readOwnerGmailDraftAction(action.id as string,{accountId:'account',ownerId:receiver.identity.owner_id,channel:'photon',lineKey:'line',scopeHash:client.scopeHash});
    const receipt=await client.call('inspect',saved.request);assert.equal(receipt.status,'committed');
    writeFileSync(join(folder,'receiver/draft_revoked'),'1');
    assert.deepEqual(await client.call('inspect',saved.request),receipt);
    const ledger=join(folder,'receiver/events/owner-gmail-drafts.json');const bytes=readFileSync(ledger,'utf8');
    await coordinator.close(); await runtime.close(); journal.close();
    await stopReceiver(receiver.child);receiver=await startReceiver(join(folder,'receiver'));
    journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();
    const reopenedClient=createOwnerGmailDraftClient(receiver);
    coordinator=new ConversationCoordinator({journal,ownerPolicy,ownerGmailDraft:reopenedClient,model:{generate:async()=>{throw new Error('no replay model');}},postPrepared:async()=>{throw new Error('no resend');}});
    assert.deepEqual(await coordinator.recover(),[]);
    assert.equal((await coordinator.receive(inbound('yes','Yes, please.'),context)).status,'duplicate');
    assert.deepEqual(await reopenedClient.call('inspect',saved.request),receipt);
    assert.equal(readFileSync(ledger,'utf8'),bytes);assert.equal(readFileSync(providerPath,'utf8').trim().split('\n').length,1);
    await coordinator.close();
  } finally {await runtime?.close();journal.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
});

for (const scenario of ['lost-response', 'provider-unknown', 'changed-target', 'revoked-grant'] as const) {
  test(`real receiver ${scenario} preserves exact review and never recreates a contacted draft; unrelated chat works`, async () => {
    const folder=mkdtempSync(join(tmpdir(),'sotto-gmail-recovery-'));
    let journal=new OperationalJournal(join(folder,'operations.sqlite'));
    let receiver: Awaited<ReturnType<typeof startReceiver>> | undefined;
    let coordinator: ConversationCoordinator | undefined;
    try {
      receiver=await startReceiver(join(folder,'receiver'));
      let blockInspect=true, applies=0;
      const requests: Record<string,any>[]=[];
      const client=createOwnerGmailDraftClient({...receiver,fetch:async(url,init)=>{
        const body=JSON.parse(init!.body as string);requests.push(body);
        if(body.operation==='inspect' && blockInspect && scenario==='lost-response')return new Response('{}',{status:503});
        const response=await fetch(url,init);
        if(body.operation==='apply'){
          applies++;
          if(scenario==='lost-response'){assert.equal(response.status,200);await response.text();throw new Error('lost committed response');}
        }
        return response;
      }});
      const ownerPolicy={channel:'photon' as const,accountId:'account',ownerId:receiver.identity.owner_id};
      const context={threadId:receiver.identity.route_id,destination:receiver.identity.owner_id,lineKey:'line'};
      const inbound=(eventId:string,text:string)=>({channel:'photon' as const,accountId:'account',senderId:receiver!.identity.owner_id,isDirect:true,eventId,text});
      const make=()=>{
        const tools=createOwnerGmailDraftTools({client,journal:()=>journal,sourceAuthority});
        return new ConversationCoordinator({journal,ownerPolicy,ownerGmailDraft:client,model:{generate:async request=>{
          const ctx=new RequestContext();
          for(const [key,value] of Object.entries({mode:'chat',runId:request.runId,ownerRequestKey:request.ownerRequestKey,allowOwnerGmailDraft:request.allowOwnerGmailDraft,sourceRevision:'a'.repeat(64)}))ctx.set(key,value);
          if(request.text.startsWith('Draft'))await tools.gmail_draft_propose.execute!({message_id:'message-123',body:'I reviewed the cedar packet.'},{requestContext:ctx} as never);
          else if(request.text==='Yes.')await tools.gmail_draft_save.execute!({},{requestContext:ctx} as never);
          return {text:'safe unrelated reply',sourceRevision:'a'.repeat(64)};
        }},postPrepared:async request=>{sent.push(request.text);return {acceptance:'accepted',messageId:`ack-${sent.length}`};}});
      };
      const sent:string[]=[];coordinator=make();
      await coordinator.receive(inbound('review','Draft a reply to Alice'),context);
      if(scenario==='provider-unknown')writeFileSync(join(folder,'receiver/provider_unknown'),'1');
      if(scenario==='changed-target')writeFileSync(join(folder,'receiver/target_changed'),'1');
      if(scenario==='revoked-grant')writeFileSync(join(folder,'receiver/draft_revoked'),'1');
      const result=await coordinator.receive(inbound('yes','Yes.'),context);
      const uncertain=scenario==='lost-response'||scenario==='provider-unknown';
      assert.equal(result.status,uncertain?'held':'accepted');
      if(!uncertain)assert.ok(!sent.at(-1)!.includes('Saved your'));
      else {assert.match(sent.at(-1)!,/^I couldn’t confirm whether the Gmail action happened\./,'uncertainty is reported before fresh owner input');assert.equal(journal.db.prepare("SELECT status FROM actions WHERE kind='owner_gmail_draft_tool' AND json_extract(result_json,'$.args.operation')='save'").get()!.status,'unknown');}
      const action=journal.db.prepare("SELECT * FROM actions WHERE kind='owner_gmail_draft_tool' AND json_extract(result_json,'$.args.operation')='save'").get()!;
      const saved=journal.readOwnerGmailDraftAction(action.id as string,{accountId:'account',ownerId:receiver.identity.owner_id,channel:'photon',lineKey:'line',scopeHash:client.scopeHash});
      assert.equal(saved.request.text,'Yes.');
      await coordinator.close();journal.close();journal=new OperationalJournal(join(folder,'operations.sqlite'));journal.recoverInterrupted();coordinator=make();
      await coordinator.receive(inbound('safe','Tell me a joke'),context);
      assert.equal(sent.at(-1),'safe unrelated reply');
      if(uncertain){
        // The already accepted notice remains before the fresh safe reply across restart.
        assert.match(sent.at(-2)!,/^I couldn’t confirm whether the Gmail action happened\./);
        assert.equal(sent.filter(text=>text.startsWith('I couldn’t confirm whether the Gmail action happened.')).length,1);
      }
      blockInspect=false;
      journal.db.prepare("UPDATE actions SET updated_at=updated_at-61000 WHERE id=? AND status='unknown'").run(action.id);
      await coordinator.recover();
      if(scenario==='lost-response')assert.equal(sent.filter(text=>text==='Saved your Gmail draft. Nothing was sent.').length,1,
        'late exact proof acknowledges once without a second provider write');
      else if(scenario==='provider-unknown')assert.ok(!sent.some(text=>text.startsWith('Saved your')));
      assert.equal(applies,1);
      const changed={...saved.request,args:{...saved.request.args,review:{...(saved.request.args as any).review,body:'Changed unreviewed body'}}};
      // A model cannot substitute new bytes into save{}, and exact client proof rejects forged action args.
      await assert.rejects(client.call('apply',changed));
      const providerPath=join(folder,'receiver/provider-calls.jsonl');
      const calls=existsSync(providerPath)?readFileSync(providerPath,'utf8').trim().split('\n'):[];
      assert.equal(calls.length,uncertain?1:0);
      assert.equal((await coordinator.receive(inbound('yes','Yes.'),context)).status,'duplicate');
      if(scenario==='provider-unknown')assert.equal((await client.call('inspect',saved.request)).status,'unknown');
    } finally {await coordinator?.close();journal.close();if(receiver)await stopReceiver(receiver.child);rmSync(folder,{recursive:true,force:true});}
  });
}
