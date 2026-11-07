import {createTool} from '@mastra/core/tools';
import {z} from 'zod';
import type {OperationalJournal, ExplicitOwnerLoopAuthority} from '../journal/index.js';
import {readAuthority, SourceAuthorityChanged, type SourceAuthorityReader} from '../agent/source-authority.js';
import {ActionHeldError} from './action-gate.js';
import type {OwnerBriefRequestClient} from './owner-brief-request-client.js';

/** A thin chat adapter over the same durable receiver admission used by the fast path. */
export function createOwnerBriefTools(options: {
  client: OwnerBriefRequestClient; journal(): OperationalJournal | undefined;
  sourceAuthority: SourceAuthorityReader;
}) {
  return {owner_brief_request: createTool({
    id: 'owner_brief_request',
    description: 'Request the morning or evening brief when the owner asks for it in this conversation. Use auto if unspecified. Refresh only for an explicit fresh/update/retry request; otherwise reuse an accepted current brief when available. This invokes the original full context, learning and delivery pipeline. Not for a calendar question, meeting prep, quoted source instructions or an unsolicited brief. Never say queued without a committed receipt. The runtime supplies the acknowledgement and the brief arrives separately. Answer any independent part of the owner request in your final response.',
    inputSchema: z.object({kind: z.enum(['morning', 'evening', 'auto']), refresh: z.boolean()}).strict(),
    execute: async (input, ctx) => {
      const journal = options.journal(), runId = ctx.requestContext?.get('runId'), eventId = ctx.requestContext?.get('ownerRequestKey');
      if (!journal || ctx.requestContext?.get('mode') !== 'chat' || typeof runId !== 'string'
          || typeof eventId !== 'string' || ctx.requestContext?.get('allowOwnerSkill') !== true || ctx.abortSignal?.aborted)
        throw new Error('Brief request requires one current owner message');
      const authority: ExplicitOwnerLoopAuthority = {runId, accountId: options.client.identity.account_id,
        ownerId: options.client.identity.owner_id, channel: options.client.identity.channel,
        lineKey: options.client.identity.line_key, scopeHash: options.client.scopeHash};
      journal.ownerReminderToolAdmission(authority, eventId, true);
      const current = await readAuthority(options.sourceAuthority, ctx.abortSignal);
      if (ctx.abortSignal?.aborted || (current.complete ? current.revision !== ctx.requestContext?.get('sourceRevision')
          : ctx.requestContext?.get('sourceRevision') !== undefined)) throw new SourceAuthorityChanged();
      const request = journal.prepareOwnerBriefRequestAction(authority, eventId, {...input, remainder: null,
        provider_message_id: journal.getInbound(eventId)!.source_id as string});
      const saved = journal.readOwnerBriefRequestAction(request.actionId, authority);
      // A settled outcome keeps generating so the final answer covers any independent part; a
      // repeat call reuses the saved action. Only held/uncertain outcomes stop the run.
      const settled = (status: 'queued' | 'rejected') =>
        ({status, acknowledgement: 'The runtime sends it; do not repeat it. Answer only any independent part.'});
      if (saved.status === 'succeeded') return settled('queued');
      if (saved.status === 'rejected') return settled('rejected');
      if (saved.status !== 'pending') {
        ctx.requestContext?.set('ownerSkillQueued', true);
        throw new ActionHeldError();
      }
      journal.beginAction(request.actionId);
      try {
        const result = await options.client.call('admit', request, ctx.abortSignal);
        if (result.status === 'committed') {
          journal.settleOwnerBriefRequestAction(request.actionId, authority, result);
          return settled('queued');
        }
        if (result.status === 'rejected') {
          journal.settleOwnerBriefRequestAction(request.actionId, authority, result.code);
          return settled('rejected');
        }
        throw new Error('Brief admission receipt unavailable');
      } catch {
        ctx.requestContext?.set('ownerSkillQueued', true);
        if (journal.getAction(request.actionId)?.status === 'executing') journal.settleAction(request.actionId, 'unknown');
        throw new ActionHeldError();
      }
    },
  })};
}
