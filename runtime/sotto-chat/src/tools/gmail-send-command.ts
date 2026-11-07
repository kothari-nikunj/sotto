import type {createOwnerGmailDraftTools} from './owner-gmail-draft-tool.js';

/** Original argv maps to the same typed review/confirmation; it never executes a shell. */
export function parseGmailSendCommand(argv: readonly string[]) {
  const [verb, ...rest] = argv;
  if (verb === 'gmail-send-confirm' && rest.length === 0) return {kind: 'confirm' as const};
  if (verb !== 'gmail-send') throw new Error('Gmail send command arguments are invalid');
  const flags = new Map<string, string>();
  for (let i = 0; i < rest.length; i += 2) {
    const key = rest[i]!, value = rest[i + 1];
    if (!['--to', '--subject', '--body'].includes(key) || value === undefined || flags.has(key))
      throw new Error('Gmail send command arguments are invalid');
    flags.set(key, value);
  }
  if (!flags.has('--to') || !flags.has('--body')) throw new Error('Gmail send command arguments are invalid');
  return {kind: 'review' as const, args: {to: flags.get('--to')!, subject: flags.get('--subject') ?? '', body: flags.get('--body')!}};
}

export function createGmailSendCommand(tools: ReturnType<typeof createOwnerGmailDraftTools>) {
  return async (request: {id: string; args: readonly string[]}, context: {
    mode?: unknown; runId?: unknown; ownerRequestKey?: unknown; allowOwnerGmailDraft?: unknown;
    sourceRevision?: unknown; signal?: AbortSignal;
  }) => {
    if (request.id !== 'google-action' || context.mode !== 'chat' || typeof context.runId !== 'string' ||
        typeof context.ownerRequestKey !== 'string' || context.allowOwnerGmailDraft !== true || context.signal?.aborted)
      throw new Error('Gmail send requires one current owner message');
    const parsed = parseGmailSendCommand(request.args);
    const toolContext = {requestContext: {get: (key: string) => (context as Record<string, unknown>)[key]}, abortSignal: context.signal};
    const result = parsed.kind === 'confirm'
      ? await tools.gmail_send_confirm.execute!({}, toolContext as never)
      : await tools.gmail_new_send_review.execute!(parsed.args, toolContext as never);
    return {backend: 'owner-gmail-receiver', coverage: 'reviewed_gmail_send', result};
  };
}
