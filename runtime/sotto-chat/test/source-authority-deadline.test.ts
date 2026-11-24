import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';
import {ChatTurnDeadlineExceeded} from '../src/agent/models.js';
import {ConversationSourceGuard, createSourceAuthorityReader, readAuthority, SourceAuthorityChanged,
  unavailableAuthority, type SourceAuthority} from '../src/agent/source-authority.js';

const granted = (revision = 'a'.repeat(64)): SourceAuthority => ({version:2, revision,
  complete:true, bridge_sources:[], google_sources:['calendar'], connector_sources:[]});

for (const revoked of [false, true]) {
  test(`final access check preserves ${revoked ? 'observed revocation' : 'turn timeout'} as the cause`, async () => {
    const directory = await mkdtemp(join(tmpdir(), 'sotto-authority-deadline-'));
    let reads = 0;
    const model = new MockLanguageModelV4({doGenerate: async () => ({
      content:[{type:'text', text:'private fixture prep'}], finishReason:{unified:'stop', raw:'STOP'},
      usage:{inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},
        outputTokens:{total:2,text:2,reasoning:0}}, warnings:[],
    })});
    const runtime = createSottoAgentRuntime({model, storagePath:join(directory,'memory.db'),
      instructions:'Answer briefly.', modelTurnDeadlineMs:5000, sourceAuthority:{read:async signal => {
        if (++reads !== 2) return granted();
        if (!signal?.aborted) await new Promise<void>(resolve => signal?.addEventListener('abort', () => resolve(), {once:true}));
        return revoked ? granted('b'.repeat(64)) : unavailableAuthority();
      }}});
    const keepAlive = setTimeout(() => {}, 15_000);
    try {
      await assert.rejects(runtime.generateChat({text:'Prep me for the 9:15 meeting',threadId:'thread',ownerId:'owner'}),
        revoked ? SourceAuthorityChanged : ChatTurnDeadlineExceeded);
      assert.equal(model.doGenerateCalls.length, 1);
      assert.equal((await runtime.generateChat({text:'Try again',threadId:'thread',ownerId:'owner'})).text, 'private fixture prep');
    } finally {clearTimeout(keepAlive); await runtime.close(); await rm(directory,{recursive:true,force:true});}
  });
}

test('aborted authority transport preserves the caller abort instead of unknown permissions', async () => {
  const controller = new AbortController();
  const reason = new Error('fixture caller cancellation');
  const reader = createSourceAuthorityReader({bridgeUrl:'http://127.0.0.1:43210/mcp', bearer:'fixture',
    fetch:async () => {controller.abort(reason); throw reason;}});
  await assert.rejects(readAuthority(reader, controller.signal), error => error === reason);
});

test('a successful final authority read cannot release an already cancelled result', async () => {
  const controller = new AbortController();
  const reason = new Error('fixture caller cancellation');
  const guard = new ConversationSourceGuard({baselinePath:'unused', purge:async () => assert.fail('no permission change'),
    reader:{read:async () => {controller.abort(reason); return granted();}}});
  await assert.rejects(guard.finish(granted(), controller.signal), error => error === reason);
});
