import { createTool } from '@mastra/core/tools';
import { z } from 'zod';
import type { OperationalJournal, ExplicitOwnerLoopAuthority } from '../journal/index.js';
import { readAuthority, SourceAuthorityChanged, type SourceAuthorityReader } from '../agent/source-authority.js';
import { ActionHeldError } from './action-gate.js';
import { contactsActionIdentity, contactsArgsSchema, contactWriteFieldsSchema, contactResourceSchema, contactsReviewText, ownerContactsKind, type ContactsArgs } from './owner-contacts-contract.js';
import type { OwnerContactsClient } from './owner-contacts-client.js';
export function createOwnerContactsTools(options: {
    client: OwnerContactsClient;
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
            throw new Error('Contacts move requires one current owner message');
        const authority: ExplicitOwnerLoopAuthority = { runId, accountId: options.client.identity.account_id, ownerId: options.client.identity.owner_id, lineKey: options.client.identity.line_key, channel: options.client.identity.channel, scopeHash: options.client.scopeHash };
        journal.ownerReminderToolAdmission(authority, eventId, true);
        return { journal, authority, eventId };
    };
    const sourceCurrent = async (ctx: Context) => {
        const current = await readAuthority(options.sourceAuthority, ctx.abortSignal);
        if (ctx.abortSignal?.aborted || !current.complete || current.revision !== ctx.requestContext?.get('sourceRevision') || !current.google_sources.includes('google_contacts'))
            throw new SourceAuthorityChanged();
    };
    const execute = async (args: ContactsArgs, ctx: Context) => {
        const { journal, authority, eventId } = trusted(ctx);
        await sourceCurrent(ctx);
        const revision = ctx.requestContext?.get('sourceRevision') as string;
        if (args.operation === 'propose_update' || args.operation === 'propose_delete' || args.operation === 'propose_create') {
            const admitted = journal.ownerReminderToolAdmission(authority, eventId, true), binding = contactsActionIdentity(eventId, ownerContactsKind, args);
            const result = await options.client.call('preview', { ...admitted, actionId: binding.actionId, args }, ctx.abortSignal);
            await sourceCurrent(ctx);
            if (result.status !== 'preview' && result.status !== 'rejected')
                throw new Error('Contacts review unavailable');
            const request = journal.prepareOwnerContactsAction(authority, eventId, args, revision);
            const saved = journal.readOwnerContactsAction(request.actionId, authority);
            if (saved.status === 'succeeded' || saved.status === 'rejected')
                return saved.preview?proposed(saved.preview):{status:'rejected',code:saved.rejection};
            journal.beginAction(request.actionId);
            journal.settleOwnerContactsAction(request.actionId, authority, result.status === 'preview' ? result : result.code);
            return result.status==='preview'?proposed(result):{status:'rejected',code:result.code};
        }
        const request = journal.prepareOwnerContactsAction(authority, eventId, args, revision), saved = journal.readOwnerContactsAction(request.actionId, authority);
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
                journal.settleOwnerContactsAction(request.actionId, authority, result.status === 'committed' ? result : result.code);
                return result.status === 'committed' ? { status: 'recorded', outcome: result.outcome } : { status: 'rejected', code: result.code };
            }
            throw new Error('Contacts receipt unavailable');
        }
        catch {
            if (journal.getAction(request.actionId)?.status === 'executing')
                journal.settleAction(request.actionId, 'unknown');
            throw new ActionHeldError();
        }
    };
    const proposed=(preview:Parameters<typeof contactsReviewText>[0])=>({status:'proposed',review_text:contactsReviewText(preview),needs_owner_confirmation:true,instruction:'Finish this turn normally with review_text exactly. The runtime delivers the canonical review. Wait for a distinct later owner yes; do not apply now.'});
    const tools={
      contacts_create_review:createTool({id:'contacts_create_review',description:'Review owner-requested Google Contacts creation with the original seven fields. Full exact field arrays are shown before separate yes.',inputSchema:z.object({fields:contactWriteFieldsSchema}).strict(),execute:async(input,ctx)=>execute({operation:'propose_create',...input},ctx)}),
      contacts_update_review:createTool({id:'contacts_update_review',description:'Review replacement of supplied field arrays on one exact Google contact selected by current contacts_search/get. Omitted fields unchanged. No inferred merging; full prior/new review and distinct yes.',inputSchema:z.object({resource_name:contactResourceSchema,fields:contactWriteFieldsSchema}).strict(),execute:async(input,ctx)=>execute({operation:'propose_update',...input},ctx)}),
      contacts_delete_review:createTool({id:'contacts_delete_review',description:'Review deletion of one exact Google contact explicitly requested by owner. Full current fields and distinct yes required.',inputSchema:z.object({resource_name:contactResourceSchema}).strict(),execute:async(input,ctx)=>execute({operation:'propose_delete',...input},ctx)}),
      contacts_confirm:createTool({id:'contacts_confirm',description:'Apply only the exact accepted Contacts review after a distinct clear yes. Empty input; never invent approval or alter fields.',inputSchema:z.object({}).strict(),execute:async(input,ctx)=>{z.object({}).strict().parse(input);const {journal,authority,eventId}=trusted(ctx);await sourceCurrent(ctx);return execute(journal.selectOwnerContactsProposal(authority,eventId,ctx.requestContext?.get('sourceRevision') as string),ctx);}})
    };
    return tools;
}
export const CONTACTS_EXECUTION_GUIDE='For a phone/email lookup use the advertised bridge_search_contacts over Mac Contacts first; a saved biography is not an address-book lookup. Mac Contacts and Google Contacts are independent sources: an unavailable Google operation does not establish that Mac Contacts was checked or failed. Use Google Contacts for an explicit Google address-book request or an identifier still missing after the available Mac lookup. For original People Google Contacts use run_sotto_script google-action contacts-search --query NAME, contacts-get --resource-name exactID, contacts-create --fields-json JSON, contacts-update --resource-name exactID --fields-json JSON, contacts-delete --resource-name exactID. Search/get first to resolve exact existing contact; clarify ambiguity. Supplied arrays replace entirely and omitted fields stay unchanged, so preserve existing entries when adding another. Writes produce a complete trusted review; finish normally with review_text exactly and wait. Only a distinct later owner yes allows google-action confirm-contacts with no extra args. No approval from old save/review questions; uncertain effects are inspected, never retried.';
