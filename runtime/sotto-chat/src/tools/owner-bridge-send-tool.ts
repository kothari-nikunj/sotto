import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import type { OperationalJournal, ExplicitOwnerLoopAuthority } from '../journal/index.js';
import { readAuthority, SourceAuthorityChanged, type SourceAuthorityReader } from '../agent/source-authority.js';
import { ActionHeldError, stableActionIdentity } from './action-gate.js';
import { bridgeSendArgsSchema, bridgeSendMessageSchema, bridgeSendReviewText, ownerBridgeSendKind, type BridgeSendArgs } from './owner-bridge-send-contract.js';
import type { OwnerBridgeSendClient } from './owner-bridge-send-client.js';
export function createOwnerBridgeSendTools(options: {
    client: OwnerBridgeSendClient;
    journal(): OperationalJournal | undefined;
    sourceAuthority: SourceAuthorityReader;
}) {
    type Context = {
        requestContext?: {
            get(key: string): unknown;
        };
        abortSignal?: AbortSignal;
    };
    const trusted = (ctx: Context) => {
        const journal = options.journal(), runId = ctx.requestContext?.get('runId'), eventId = ctx.requestContext?.get('ownerRequestKey');
        if (!journal || ctx.requestContext?.get('mode') !== 'chat' || typeof runId !== 'string' || typeof eventId !== 'string' || ctx.requestContext?.get('allowOwnerSkill') !== true || ctx.abortSignal?.aborted)
            throw new Error('Bridge message requires one current owner message');
        const authority: ExplicitOwnerLoopAuthority = { runId, accountId: options.client.identity.account_id, ownerId: options.client.identity.owner_id, lineKey: options.client.identity.line_key, channel: options.client.identity.channel, scopeHash: options.client.scopeHash };
        journal.ownerReminderToolAdmission(authority, eventId, true);
        return { journal, authority, eventId };
    };
    const sourceCurrent = async (ctx: Context) => {
        const current = await readAuthority(options.sourceAuthority, ctx.abortSignal);
        if (ctx.abortSignal?.aborted || !current.complete || current.revision !== ctx.requestContext?.get('sourceRevision'))
            throw new SourceAuthorityChanged();
    };
    const execute = async (args: BridgeSendArgs, ctx: Context) => {
        const { journal, authority, eventId } = trusted(ctx);
        await sourceCurrent(ctx);
        const revision = ctx.requestContext?.get('sourceRevision') as string;
        if (args.operation === 'propose_send') {
            const admitted = journal.ownerReminderToolAdmission(authority, eventId, true), binding = stableActionIdentity(eventId, ownerBridgeSendKind, args);
            const result = await options.client.call('preview', { ...admitted, actionId: binding.actionId, args }, ctx.abortSignal);
            await sourceCurrent(ctx);
            if (result.status !== 'preview' && result.status !== 'rejected')
                throw new Error('BridgeSend review unavailable');
            const request = journal.prepareOwnerBridgeSendAction(authority, eventId, args, revision);
            const saved = journal.readOwnerBridgeSendAction(request.actionId, authority);
            if (saved.status === 'succeeded' || saved.status === 'rejected')
                return saved.preview?proposed(saved.preview):{status:'rejected',code:saved.rejection};
            journal.beginAction(request.actionId);
            journal.settleOwnerBridgeSendAction(request.actionId, authority, result.status === 'preview' ? result : result.code);
            return result.status==='preview'?proposed(result):{status:'rejected',code:result.code};
        }
        const request = journal.prepareOwnerBridgeSendAction(authority, eventId, args, revision), saved = journal.readOwnerBridgeSendAction(request.actionId, authority);
        if (saved.status === 'succeeded')
            return { status: 'recorded', outcome: saved.receipt!.outcome };
        if (saved.status === 'rejected')
            return { status: 'rejected', code: saved.rejection };
        if (saved.status !== 'pending')
            throw new ActionHeldError();
        journal.beginAction(request.actionId);
        try {
            const result = await options.client.call('apply', request, ctx.abortSignal);
            if (result.status === 'committed' || result.status === 'rejected') {
                journal.settleOwnerBridgeSendAction(request.actionId, authority, result.status === 'committed' ? result : result.code);
                return result.status === 'committed' ? { status: 'recorded', outcome: result.outcome } : { status: 'rejected', code: result.code };
            }
            throw new Error('BridgeSend receipt unavailable');
        }
        catch {
            if (journal.getAction(request.actionId)?.status === 'executing')
                journal.settleAction(request.actionId, 'unknown');
            throw new ActionHeldError();
        }
    };
    const proposed=(preview:Parameters<typeof bridgeSendReviewText>[0])=>({status:'proposed',review_text:bridgeSendReviewText(preview),needs_owner_confirmation:true,instruction:'Finish this turn normally with review_text exactly. The runtime delivers the canonical review. Wait for a distinct later owner yes; do not apply now.'});
    return {
      bridge_send_review: createTool({id:'bridge_send_review',description:'Review an iMessage/SMS the owner asks to send from their Mac. Resolve an exact phone or email through Contacts first if only a name is given; ask if ambiguous. Show recipient and full message, then wait for a separate yes. Never sends at review.',inputSchema:bridgeSendMessageSchema,execute:async(input,ctx)=>execute({operation:'propose_send',...input},ctx)}),
      bridge_send_confirm: createTool({id:'bridge_send_confirm',description:'Send the exact most recent accepted Bridge message review after a distinct owner yes. No recipient, content or approval arguments. Never retry an unconfirmed send.',inputSchema:z.object({}).strict(),execute:async(input,ctx)=>{z.object({}).strict().parse(input);const {journal,authority,eventId}=trusted(ctx);await sourceCurrent(ctx);return execute(journal.selectOwnerBridgeSendProposal(authority,eventId,ctx.requestContext?.get('sourceRevision') as string),ctx);}})
    };
}
