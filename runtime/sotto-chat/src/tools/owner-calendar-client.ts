import { createHash } from 'node:crypto';
import { z } from 'zod';
import type { OwnerToolClientOptions } from './owner-contract.js';
import { createOwnerToolTransport } from './owner-tool-transport.js';
import { stableActionIdentity } from './action-gate.js';
import { calendarArgsSchema, ownerCalendarKind, calendarReceiptSchema, calendarRejectionSchema, calendarReceiptMatches, calendarPreviewSchema, validCalendarReview, calendarPreviewMatches, type CalendarArgs } from './owner-calendar-contract.js';
const schema = z.union([calendarPreviewSchema, calendarReceiptSchema, z.object({ version: z.literal(1), status: z.literal('not_found') }).strict(), z.object({ version: z.literal(1), status: z.literal('unknown') }).strict(), z.object({ version: z.literal(1), status: z.literal('rejected'), code: calendarRejectionSchema }).strict()]);
export interface OwnerCalendarRequest {
    actionId: string;
    eventId: string;
    payloadHash: string;
    text: string;
    admittedAtMs: number;
    providerThreadId: string;
    args: CalendarArgs;
}
export function createOwnerCalendarClient(options: OwnerToolClientOptions) {
    const transport = createOwnerToolTransport(options, '/runtime/owner-calendar-tool');
    function requestBody(operation:'preview'|'apply'|'inspect',input:OwnerCalendarRequest){
        if (!/^[a-f0-9]{64}$/.test(input.eventId) || !/^[a-f0-9]{64}$/.test(input.payloadHash) || !Number.isSafeInteger(input.admittedAtMs) ||
            !input.providerThreadId || !input.text || input.text.length > 16000 || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\ud800-\udfff]/u.test(input.text) ||
            !calendarArgsSchema.safeParse(input.args).success)
            throw new Error('Calendar move request is invalid');
        const binding = stableActionIdentity(input.eventId, ownerCalendarKind, input.args);
        if (binding.actionId !== input.actionId)
            throw new Error('Calendar move request is invalid');
        return {binding,body:{version:1,operation,action_id:input.actionId,event_id:input.eventId,payload_hash:input.payloadHash,text_hash:createHash('sha256').update(input.text).digest('hex'),text:input.text,admitted_at_ms:input.admittedAtMs,provider_thread_id:input.providerThreadId,args:input.args}};
    }
    function preflight(input:OwnerCalendarRequest){if(Buffer.byteLength(JSON.stringify({...requestBody('apply',input).body,identity:transport.identity}),'utf8')>65536)throw new Error('Calendar update request exceeds the existing request capacity');}
    async function call(operation: 'preview' | 'apply' | 'inspect', input: OwnerCalendarRequest, signal?: AbortSignal) {
        const {binding,body}=requestBody(operation,input);
        const response=await transport.request(body, signal, {responseLimit: operation==='preview'?128*1024:input.args.operation==='create'?64*1024:8192});
        const result = schema.parse(response.value);
        if ((response.status === 409) !== (result.status === 'rejected') ||
            (operation === 'preview' ? !['preview', 'rejected'].includes(result.status) : result.status === 'preview') ||
            (operation === 'apply' && result.status === 'not_found') ||
            (result.status === 'preview' && 'event_id' in result && (result.event_id !== input.eventId || result.payload_hash !== input.payloadHash)) ||
            (result.status === 'committed' && (result.action_id !== input.actionId || result.event_id !== input.eventId ||
                result.payload_hash !== input.payloadHash || result.args_hash !== binding.payloadHash || !input.args || !calendarReceiptMatches(result, input.args))))
            throw new Error('Calendar move receipt is invalid');
        if (result.status === 'preview' && !calendarPreviewMatches(result, input.args,input.actionId))
            throw new Error('Calendar move review is invalid');
        return result;
    }
    return { identity: transport.identity, scopeHash: transport.scopeHash, call,preflight };
}
export type OwnerCalendarClient = ReturnType<typeof createOwnerCalendarClient>;
