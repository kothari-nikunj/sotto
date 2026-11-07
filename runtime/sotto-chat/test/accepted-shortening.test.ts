import assert from 'node:assert/strict';
import test from 'node:test';
import {mkdtempSync, rmSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {MockLanguageModelV4} from 'ai/test';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';

for (const state of ['current', 'missing', 'stale', 'quoted'] as const) {
  test(`shortening uses accepted text without source tools: ${state}`, async () => {
    const folder = mkdtempSync(join(tmpdir(), 'sotto-shorten-'));
    let calls = 0;
    const revision = 'a'.repeat(64);
    const text = 'The review is on Thursday at 2 PM, subject to Dana confirming.';
    const runtime = createSottoAgentRuntime({storagePath:join(folder,'memory.sqlite'), instructions:'Answer the owner. TOOL EXECUTION RECIPE',
      answerOnlyInstructions:'Answer the owner. Preserve privacy and source uncertainty.',
      sourceAuthority:{read:async () => ({version:2,complete:true,revision,bridge_sources:[],google_sources:['gmail']})},
      chatTools:() => {throw new Error('Shortening must not discover source tools');},
      readMasterContext:async () => {throw new Error('Shortening must not gather standing context');},
      model:new MockLanguageModelV4({doGenerate:async input => {
        calls++;
        assert.equal(input.tools?.length ?? 0,0);
        const prompt = JSON.stringify(input.prompt);
        assert.match(prompt, /Preserve privacy and source uncertainty/);
        assert.doesNotMatch(prompt, /TOOL EXECUTION RECIPE/);
        assert.equal(prompt.includes(text), state === 'current' || state === 'quoted');
        assert.match(prompt, state === 'current' || state === 'quoted' ? /Shorten this exact accepted text/ : /Ask the owner to paste/);
        return {content:[{type:'text',text:'Thursday, 2 PM—pending Dana’s confirmation.'}],
          finishReason:{unified:'stop',raw:'STOP'},warnings:[],usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}}};
      }})});
    try {
      await runtime.generateChat({text:'shorter pls',threadId:'thread',ownerId:'owner',
        ...(state === 'missing' ? {} : state === 'quoted'
          ? {quotedReply:{status:'found' as const,messageId:'provider-id',text,sourceRevision:revision}}
          : {acceptedEditContext:{text,sourceRevision:state === 'stale' ? 'b'.repeat(64) : revision}})});
      assert.equal(calls,1);
    } finally {await runtime.close();rmSync(folder,{recursive:true,force:true});}
  });
}

test('compact edit instructions preserve revocation checks and do not replace ordinary tool instructions', async () => {
  const folder = mkdtempSync(join(tmpdir(), 'sotto-shorten-authority-'));
  let revision = 'a'.repeat(64), calls = 0;
  const runtime = createSottoAgentRuntime({storagePath:join(folder,'memory.sqlite'),
    instructions:'Core privacy rules. TOOL EXECUTION RECIPE', answerOnlyInstructions:'Core privacy rules.',
    sourceAuthority:{read:async () => ({version:2,complete:true,revision,bridge_sources:[],google_sources:[]})},
    model:new MockLanguageModelV4({doGenerate:async input => {
      calls++;
      const prompt=JSON.stringify(input.prompt);
      assert.match(prompt,/Core privacy rules/);
      if(calls===1)assert.match(prompt,/TOOL EXECUTION RECIPE/);
      else {assert.doesNotMatch(prompt,/TOOL EXECUTION RECIPE/);revision='b'.repeat(64);}
      return {content:[{type:'text',text:'Thursday, pending confirmation.'}],finishReason:{unified:'stop',raw:'STOP'},warnings:[],
        usage:{inputTokens:{total:1,noCache:1,cacheRead:0,cacheWrite:0},outputTokens:{total:1,text:1,reasoning:0}}};
    }})});
  try {
    await runtime.generateChat({text:'hello',threadId:'thread',ownerId:'owner'});
    await assert.rejects(runtime.generateChat({text:'shorter pls',threadId:'thread',ownerId:'owner',
      acceptedEditContext:{text:'Thursday, pending confirmation.',sourceRevision:revision}}));
    assert.equal(calls,2);
  } finally {await runtime.close();rmSync(folder,{recursive:true,force:true});}
});
