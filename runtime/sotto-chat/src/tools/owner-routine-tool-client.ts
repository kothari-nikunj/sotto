import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { OwnerToolClientOptions } from './owner-contract.js';
import { createOwnerToolTransport } from './owner-tool-transport.js';
import { stableActionIdentity } from './action-gate.js';
import { ownerRoutineToolArgsSchema, ownerRoutineToolKind, routineListSchema, ownerRoutineToolReceiptSchema, ownerRoutineToolRejectionSchema, routineReceiptMatches, routinePreviewSchema, routinePreviewMatches, type OwnerRoutineToolArgs } from './owner-routine-tool-contract.js';
const schema = z.union([routinePreviewSchema, routineListSchema, ownerRoutineToolReceiptSchema,
  z.object({ version: z.literal(1), status: z.literal('not_found') }).strict(),
  z.object({ version: z.literal(1), status: z.literal('rejected'), code: ownerRoutineToolRejectionSchema }).strict()]);
export interface OwnerRoutineToolRequest {
  actionId: string;
  eventId: string;
  payloadHash: string;
  text: string;
  admittedAtMs: number;
  providerThreadId: string;
  args: OwnerRoutineToolArgs | null;
}
export function createOwnerRoutineToolClient(options: OwnerToolClientOptions) {
  const transport = createOwnerToolTransport(options, '/runtime/owner-routine-tool');
  async function call(operation: 'preview' | 'list' | 'apply' | 'inspect', input: OwnerRoutineToolRequest, signal?: AbortSignal) {
    if (!/^[a-f0-9]{64}$/.test(input.eventId) || !/^[a-f0-9]{64}$/.test(input.payloadHash) || !Number.isSafeInteger(input.admittedAtMs) ||
      !input.providerThreadId || !input.text || input.text.length > 16000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(input.text) ||
      (operation === 'list' ? input.args !== null : !ownerRoutineToolArgsSchema.safeParse(input.args).success))
      throw new Error('Owner routine request is invalid');
    const binding = stableActionIdentity(input.eventId, ownerRoutineToolKind, input.args);
    if (binding.actionId !== input.actionId)
      throw new Error('Owner routine request is invalid');
    const response = await transport.request({ version: 1, operation, action_id: input.actionId, event_id: input.eventId,
      payload_hash: input.payloadHash, text_hash: createHash('sha256').update(input.text).digest('hex'), text: input.text,
      admitted_at_ms: input.admittedAtMs, provider_thread_id: input.providerThreadId, args: input.args }, signal, {responseLimit: operation === 'list' ? 512 * 1024 : operation === 'preview' ? (input.args?.operation==='propose_adjust'?65536:16384) : 8192});
    const result = schema.parse(response.value);
    if ((response.status === 409) !== (result.status === 'rejected') ||
      (operation === 'list' ? result.status !== 'list' : operation === 'preview' ? !['preview', 'rejected'].includes(result.status) : ['preview', 'list'].includes(result.status)) ||
      (operation === 'apply' && result.status === 'not_found') ||
      (['preview', 'list'].includes(result.status) && 'event_id' in result && (result.event_id !== input.eventId || result.payload_hash !== input.payloadHash)) ||
      (result.status === 'committed' && (result.action_id !== input.actionId || result.event_id !== input.eventId ||
        result.payload_hash !== input.payloadHash || result.args_hash !== binding.payloadHash || !input.args || !routineReceiptMatches(result, input.args))))
      throw new Error('Owner routine receipt is invalid');
    if(result.status==='preview' && (!input.args || !routinePreviewMatches(result,input.args))) throw new Error('Owner routine preview is invalid');
    return result;
  }
  return { identity: transport.identity, scopeHash: transport.scopeHash, call };
}
export type OwnerRoutineToolClient = ReturnType<typeof createOwnerRoutineToolClient>;
