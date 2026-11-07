import {randomUUID} from 'node:crypto';
import type {createSottoAgentRuntime, ChatTurn} from '../../src/agent/runtime.js';
import {OWNER_ONLY_CONTEXT_PROOF} from '../../src/agent/source-authority.js';

/** Adapter-only tests supply explicit admission/acceptance; application tests use real receipts. */
export async function acceptedFixture(runtime:ReturnType<typeof createSottoAgentRuntime>, turn:ChatTurn) {
  const result = await runtime.generateChat(turn);
  const now = Date.now();
  await runtime.projectConversation({threadId:turn.threadId,ownerId:turn.ownerId,messages:[
    {id:randomUUID(),role:'user',text:turn.text,createdAt:now,sourceRevision:OWNER_ONLY_CONTEXT_PROOF},
    ...(result.text ? [{id:randomUUID(),role:'assistant' as const,text:result.text,createdAt:now+1,sourceRevision:result.sourceRevision}] : []),
  ]});
  return result;
}
