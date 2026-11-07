import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {OperationalJournal} from '../src/journal/index.js';
import {ConversationCoordinator} from '../src/conversation.js';
import {createOwnerTaskExecution} from '../src/tools/owner-task-execution.js';
import {createOwnerTaskToolClient} from '../src/tools/owner-task-tool-client.js';
import {SourceAuthorityChanged} from '../src/agent/source-authority.js';

for (const partial of [false, true]) test(`settled batch replay reuses its receipt after closed rows leave the open projection; partial=${partial}`, async () => {
  const folder = mkdtempSync(join(tmpdir(), 'task-settled-replay-'));
  const journal = new OperationalJournal(join(folder, 'journal.sqlite'));
  const revision = 'a'.repeat(64), proof = 'b'.repeat(64);
  let writes = 0, projections = 0, sourceRevision = revision, checked = false;
  let modelError: unknown;
  const row = {anchor_key:'dana-packet', loop_version:proof, loop_identity:proof, name:'Dana Cedar',
    what:'Review the packet', channel:'email', identifier:'dana@example.com', thread_id:'packet-thread',
    group_id:'', action_type:'follow_up', age_days:1, deadline:null, overdue:false, chased_count:0,
    chase_pending:null, last_chased_at:null, chase_after:null, chased_out:false, handoff_asked_at:null};
  const client = createOwnerTaskToolClient({bridgeUrl:'http://127.0.0.1:8080/mcp', bearer:revision,
    identity:{tenant_id:'tenant', channel:'photon', account_id:'account', owner_id:'owner', line_key:'line',
      target:'photon', route_mode:'fixed', route_id:'thread', project_fingerprint:revision},
    fetch:async (_url, init) => {
      const body = JSON.parse(String(init?.body));
      assert.equal(body.operation, 'apply'); writes++;
      return new Response(JSON.stringify({version:1, status:'committed', action_id:body.action_id,
        event_id:body.event_id, payload_hash:body.payload_hash,
        args_hash:journal.getAction(body.action_id)!.payload_hash, operation:'resolve_many', outcome:partial?'partial':'complete',
        items:[{index:1, action:'resolve', anchor_key:row.anchor_key, status:'applied', post_digest:proof, loop_status:'resolved'},
          ...(partial?[{index:2, action:'resolve', anchor_key:'', status:'refused', reason:'target_unavailable'}]:[])]}));
    }});
  client.readLoops = async () => {
    projections++;
    // A comparable synthetic receiver boundary; the closed row is no longer open.
    await new Promise(resolve => setTimeout(resolve, 8));
    const you_owe = writes ? [] : [row];
    return {coverage:'currently_permitted_canonical_loops', you_owe, waiting_on_them:[],
      counts:{you_owe:you_owe.length, waiting_on_them:0}};
  };
  client.readback = async () => ({you_owe:0, waiting_on_them:0, source_revision:revision, remaining:[]});
  const execution = createOwnerTaskExecution({client, journal:() => journal,
    sourceAuthority:{read:async () => ({version:2, complete:true, revision:sourceRevision,
      bridge_sources:[], google_sources:['gmail']})}});
  const coordinator = new ConversationCoordinator({journal,
    ownerPolicy:{channel:'photon', accountId:'account', ownerId:'owner'}, ownerTaskTool:client,
    model:{generate:async request => {
      try {
      const context = {mode:'chat', runId:request.runId, ownerRequestKey:request.ownerRequestKey,
        allowOwnerTask:request.allowOwnerTask, sourceRevision:revision, signal:request.signal};
      const args = {operation:'resolve_many' as const, allow_partial:true as const, source_revision:revision,
        selection_mode:'named_people' as const,
        items:[{name:'Dana Cedar', correction:'Dana Cedar', anchor_key:row.anchor_key,
          loop_version:proof, loop_identity:proof},
          ...(partial?[{name:'Robin Sage', correction:'Robin Sage', anchor_key:'missing-robin', loop_version:proof, loop_identity:proof}]:[])]};
      const first = await execution.execute(args, context);
      const started = performance.now();
      let replay;
      try { replay = await execution.execute(args, context); }
      catch (error) {
        console.log(JSON.stringify({boundary:'settled_batch_replay', partial, projections, writes,
          replayMs:performance.now() - started, error:error instanceof Error ? error.message : 'unknown'}));
        throw error;
      }
      const replayMs = performance.now() - started;
      assert.deepEqual(replay, first, 'the exact durable receipt survives projection changes');
      assert.equal(projections, 1, 'settled replay does not rediscover open rows');
      assert.equal(writes, 1, 'settled replay never repeats an effect');
      console.log(JSON.stringify({boundary:'settled_batch_replay', partial, projections, writes, replayMs}));
      if(partial){
        const changedRefusedProof={...args,items:[args.items[0]!,{...args.items[1]!,anchor_key:'new-robin',loop_identity:'e'.repeat(64)}]};
        assert.deepEqual(await execution.execute(changedRefusedProof,context),first,
          'new proofs for an already refused item only replay its original refusal');
        await assert.rejects(execution.execute({...args,items:[...args.items].reverse()},context));
      }
      sourceRevision = 'c'.repeat(64);
      await assert.rejects(execution.execute(args, context), SourceAuthorityChanged);
      sourceRevision = revision;
      await assert.rejects(execution.execute(args, {...context, runId:'other-run'}));
      const abort = new AbortController(); abort.abort();
      await assert.rejects(execution.execute(args, {...context, signal:abort.signal}));
      await assert.rejects(execution.execute({...args,items:args.items.map((item,index)=>index?item:{...item,loop_identity:'d'.repeat(64)})},context));
      await assert.rejects(execution.execute({...args,items:args.items.map((item,index)=>index?item:{...item,name:'Another Person',correction:'Another Person'})},context));
      assert.equal(writes, 1);
      assert.equal(projections,1,'changed caller proofs or phrases cannot trigger rediscovery');
      checked = true;
      return 'Receipt checked.';
      } catch (error) { modelError = error; throw error; }
    }}, postPrepared:async () => ({acceptance:'accepted', messageId:'ack'})});
  try {
    await coordinator.receive({channel:'photon', accountId:'account', senderId:'owner', isDirect:true,
      eventId:'completion', text:partial?'Dana Cedar and Robin Sage are handled':'Dana Cedar is handled'}, {threadId:'thread', destination:'photon', lineKey:'line'});
    if (modelError) throw modelError;
    assert.ok(checked, 'replay checks completed inside the admitted model run');
  } finally {
    await coordinator.close(); journal.close(); rmSync(folder, {recursive:true, force:true});
  }
});
