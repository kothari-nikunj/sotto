import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { OwnerToolClientOptions } from './owner-contract.js';
import { createOwnerToolTransport } from './owner-tool-transport.js';
import { contactsActionIdentity, contactsArgsSchema, ownerContactsKind, contactsReceiptSchema, contactsRejectionSchema, contactsReceiptMatches, contactsPreviewSchema, validContactsReview, contactsReviewText, contactsPreviewMatches, type ContactsArgs } from './owner-contacts-contract.js';
const schema = z.union([contactsPreviewSchema, contactsReceiptSchema, z.object({ version: z.literal(1), status: z.literal('not_found') }).strict(), z.object({ version: z.literal(1), status: z.literal('unknown') }).strict(), z.object({ version: z.literal(1), status: z.literal('rejected'), code: contactsRejectionSchema }).strict()]);
export interface OwnerContactsRequest {
    actionId: string;
    eventId: string;
    payloadHash: string;
    text: string;
    admittedAtMs: number;
    providerThreadId: string;
    args: ContactsArgs;
}
export function createOwnerContactsClient(options: OwnerToolClientOptions) {
    const transport = createOwnerToolTransport(options, '/runtime/owner-contacts-tool');
    async function call(operation: 'preview' | 'apply' | 'inspect', input: OwnerContactsRequest, signal?: AbortSignal) {
        if (!/^[a-f0-9]{64}$/.test(input.eventId) || !/^[a-f0-9]{64}$/.test(input.payloadHash) || !Number.isSafeInteger(input.admittedAtMs) ||
            !input.providerThreadId || !input.text || input.text.length > 16000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(input.text) ||
            !contactsArgsSchema.safeParse(input.args).success)
            throw new Error('Contacts move request is invalid');
        const binding = contactsActionIdentity(input.eventId, ownerContactsKind, input.args);
        if (binding.actionId !== input.actionId)
            throw new Error('Contacts move request is invalid');
        const response = await transport.request({ version: 1, operation, action_id: input.actionId, event_id: input.eventId,
            payload_hash: input.payloadHash, text_hash: createHash('sha256').update(input.text).digest('hex'), text: input.text,
            admitted_at_ms: input.admittedAtMs, provider_thread_id: input.providerThreadId, args: input.args }, signal, {responseLimit: operation === 'preview' ? 256 * 1024 : 8192, deadlineMs: 5000, requestLimit: 256*1024});
        const result = schema.parse(response.value);
        if ((response.status === 409) !== (result.status === 'rejected') ||
            (operation === 'preview' ? !['preview', 'rejected'].includes(result.status) : result.status === 'preview') ||
            (operation === 'apply' && result.status === 'not_found') ||
            (result.status === 'preview' && 'event_id' in result && (result.event_id !== input.eventId || result.payload_hash !== input.payloadHash)) ||
            (result.status === 'committed' && (result.action_id !== input.actionId || result.event_id !== input.eventId ||
                result.payload_hash !== input.payloadHash || result.args_hash !== binding.payloadHash || !input.args || !contactsReceiptMatches(result, input.args))))
            throw new Error('Contacts move receipt is invalid');
        if (result.status === 'preview' && (!contactsPreviewMatches(result, input.args)||contactsReviewText(result).length>16000))
            throw new Error('Contacts move review is invalid');
        return result;
    }
    return { identity: transport.identity, scopeHash: transport.scopeHash, call };
}
export type OwnerContactsClient = ReturnType<typeof createOwnerContactsClient>;
