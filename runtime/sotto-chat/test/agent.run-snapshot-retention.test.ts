import test from 'node:test';
import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {promisify} from 'node:util';
import {fileURLToPath} from 'node:url';
import {MockLanguageModelV4} from 'ai/test';
import {createTool} from '@mastra/core/tools';
import {LibSQLStore} from '@mastra/libsql';
import {z} from 'zod';
import {createSottoAgentRuntime} from '../src/agent/runtime.js';

const usage = {inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
const crashChild = fileURLToPath(new URL('./helpers/run-snapshot-crash-child.ts', import.meta.url));

async function snapshotRuns(path: string) {
  const store = new LibSQLStore({id:'snapshot-probe', url:`file:${path}`});
  try {
    await store.init();
    const workflows = await store.getStore('workflows');
    return (await workflows!.listWorkflowRuns()).total;
  } finally { await store.close(); }
}

for (const mode of ['chat','unattended'] as const) {
  for (const outcome of ['complete','tool-throw','abort'] as const) {
    test(`${mode} ${outcome} turn writes no run snapshot to the memory file`, async () => {
      const directory = await mkdtemp(join(tmpdir(),'sotto-run-snapshot-'));
      const path = join(directory,'memory.db');
      const controller = new AbortController();
      let midRun = -1;
      let step = 0;
      const probe = createTool({id:'fixture_probe',description:'Probe',inputSchema:z.object({}).strict(),
        execute: async () => {
          midRun = await snapshotRuns(path);
          if (outcome === 'tool-throw') throw new Error('fixture tool failure');
          if (outcome === 'abort') { controller.abort(); throw new DOMException('Cancelled','AbortError'); }
          return {ok:true};
        }});
      const tools = async () => ({fixture_probe: probe});
      const runtime = createSottoAgentRuntime({storagePath:path,instructions:'Answer briefly.',
        chatTools:tools,unattendedTools:tools,
        model:new MockLanguageModelV4({doGenerate:async () => ++step === 1
          ? {content:[{type:'tool-call' as const,toolCallId:'probe-1',toolName:'fixture_probe',input:'{}'}],
            finishReason:{unified:'tool-calls' as const,raw:'TOOL_CALLS'},usage,warnings:[]}
          : {content:[{type:'text' as const,text:'Fixture answer.'}],finishReason:{unified:'stop' as const,raw:'STOP'},usage,warnings:[]}})});
      try {
        const run = mode === 'chat'
          ? runtime.generateChat({text:'Brief me on Avery Lin.',threadId:'t1',ownerId:'o1',runId:'run-1',signal:controller.signal})
          : runtime.generateUnattended({text:'Brief me on Avery Lin.',runId:'run-1',signal:controller.signal});
        if (outcome === 'abort') await assert.rejects(run); else assert.equal((await run).text, 'Fixture answer.');
        assert.equal(midRun, 0, 'a turn killed at this point would leave these rows behind');
        assert.equal(await snapshotRuns(path), 0);
      } finally { await runtime.close(); await rm(directory,{recursive:true,force:true}); }
    });
  }

  test(`${mode} turn killed mid-tool leaves no run snapshot that thread purges would miss`, async () => {
    const directory = await mkdtemp(join(tmpdir(),'sotto-run-snapshot-crash-'));
    const path = join(directory,'memory.db');
    try {
      await assert.rejects(promisify(execFile)(process.execPath, ['--import','tsx',crashChild,path,mode,'Brief me on Avery Lin.'],
        {cwd:fileURLToPath(new URL('..', import.meta.url))}), (error: {signal?: string}) => error.signal === 'SIGKILL');
      const runtime = createSottoAgentRuntime({storagePath:path,instructions:'Answer briefly.',
        model:new MockLanguageModelV4({doGenerate:async () => { throw new Error('unused'); }})});
      try { await runtime.purgeAllThreads(); } finally { await runtime.close(); }
      assert.equal(await snapshotRuns(path), 0);
    } finally { await rm(directory,{recursive:true,force:true}); }
  });
}
