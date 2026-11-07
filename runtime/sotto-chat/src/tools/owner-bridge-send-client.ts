import {stableActionIdentity} from './action-gate.js';
import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { OwnerToolClientOptions } from './owner-contract.js';
import { createOwnerToolTransport } from './owner-tool-transport.js';
import { bridgeSendArgsSchema, ownerBridgeSendKind, bridgeSendReceiptSchema, bridgeSendRejectionSchema, bridgeSendReceiptMatches, bridgeSendPreviewSchema, validBridgeSendReview, bridgeSendReviewText, bridgeSendPreviewMatches, type BridgeSendArgs } from './owner-bridge-send-contract.js';
const schema = z.union([bridgeSendPreviewSchema, bridgeSendReceiptSchema, z.object({ version: z.literal(1), status: z.literal('not_found') }).strict(), z.object({ version: z.literal(1), status: z.literal('unknown') }).strict(), z.object({ version: z.literal(1), status: z.literal('rejected'), code: bridgeSendRejectionSchema }).strict()]);
export interface OwnerBridgeSendRequest {
    actionId: string;
    eventId: string;
    payloadHash: string;
    text: string;
    admittedAtMs: number;
    providerThreadId: string;
    args: BridgeSendArgs;
}
export function createOwnerBridgeSendClient(options: OwnerToolClientOptions) {
    const transport = createOwnerToolTransport(options, '/runtime/owner-bridge-send-tool');
    async function call(operation: 'preview' | 'apply' | 'inspect', input: OwnerBridgeSendRequest, signal?: AbortSignal) {
        if (!/^[a-f0-9]{64}$/.test(input.eventId) || !/^[a-f0-9]{64}$/.test(input.payloadHash) || !Number.isSafeInteger(input.admittedAtMs) ||
            !input.providerThreadId || !input.text || input.text.length > 16000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(input.text) ||
            !bridgeSendArgsSchema.safeParse(input.args).success)
            throw new Error('Bridge message request is invalid');
        const binding = stableActionIdentity(input.eventId, ownerBridgeSendKind, input.args);
        if (binding.actionId !== input.actionId)
            throw new Error('Bridge message request is invalid');
        const response = await transport.request({ version: 1, operation, action_id: input.actionId, event_id: input.eventId,
            payload_hash: input.payloadHash, text_hash: createHash('sha256').update(input.text).digest('hex'), text: input.text,
            admitted_at_ms: input.admittedAtMs, provider_thread_id: input.providerThreadId, args: input.args }, signal, {responseLimit: operation === 'preview' ? 256 * 1024 : 8192, deadlineMs: 105000, requestLimit: 65536});
        const result = schema.parse(response.value);
        if ((response.status === 409) !== (result.status === 'rejected') ||
            (operation === 'preview' ? !['preview', 'rejected'].includes(result.status) : result.status === 'preview') ||
            (operation === 'apply' && result.status === 'not_found') ||
            (result.status === 'preview' && 'event_id' in result && (result.event_id !== input.eventId || result.payload_hash !== input.payloadHash)) ||
            (result.status === 'committed' && (result.action_id !== input.actionId || result.event_id !== input.eventId ||
                result.payload_hash !== input.payloadHash || result.args_hash !== binding.payloadHash || !input.args || !bridgeSendReceiptMatches(result, input.args))))
            throw new Error('Bridge message receipt is invalid');
        if (result.status === 'preview' && (!bridgeSendPreviewMatches(result, input.args)||bridgeSendReviewText(result).length>16000))
            throw new Error('Bridge message review is invalid');
        return result;
    }
    return { identity: transport.identity, scopeHash: transport.scopeHash, call };
}
export type OwnerBridgeSendClient = ReturnType<typeof createOwnerBridgeSendClient>;
