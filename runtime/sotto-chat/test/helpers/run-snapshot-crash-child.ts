import {MockLanguageModelV4} from 'ai/test';
import {createTool} from '@mastra/core/tools';
import {z} from 'zod';
import {createSottoAgentRuntime} from '../../src/agent/runtime.js';

const [path, mode, text] = process.argv.slice(2);
const usage = {inputTokens:{total:5,noCache:5,cacheRead:0,cacheWrite:0},outputTokens:{total:2,text:2,reasoning:0}};
const crash = createTool({id:'fixture_crash',description:'Crash',inputSchema:z.object({}).strict(),
  execute: async () => { process.kill(process.pid, 'SIGKILL'); return {}; }});
const tools = async () => ({fixture_crash: crash});
const runtime = createSottoAgentRuntime({storagePath:path!,instructions:'Answer briefly.',chatTools:tools,unattendedTools:tools,
  model:new MockLanguageModelV4({doGenerate:async () => ({content:[{type:'tool-call' as const,toolCallId:'crash-1',
    toolName:'fixture_crash',input:'{}'}],finishReason:{unified:'tool-calls' as const,raw:'TOOL_CALLS'},usage,warnings:[]})})});
await (mode === 'chat'
  ? runtime.generateChat({text:text!,threadId:'t1',ownerId:'o1',runId:'crash-run'})
  : runtime.generateUnattended({text:text!,runId:'crash-run'}));
