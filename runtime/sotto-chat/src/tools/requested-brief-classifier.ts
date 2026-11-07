import type { Inbound } from '../channels/types.js';
export interface RequestedBriefTurn {kind: 'morning' | 'evening' | 'auto'; refresh: boolean; remainder: string | null}
function request(text: string): Omit<RequestedBriefTurn, 'remainder'> | null {
  if (text.length > 120) return null;
  const value = briefText(text);
  // Only a whole request clause is direct: the intent parser also accepts separate
  // conversational clauses, which must remain available to the model as context.
  return briefClause.test(value) ? ownerBriefIntent(value) : null;
}
/** Narrow explicit request grammar for owner brief requests; anything else stays ordinary chat. */
export function classifyRequestedBrief(inbound: Inbound): RequestedBriefTurn | null {
  if (inbound.replyBound || inbound.media?.length || !inbound.text) return null;
  const text = inbound.text.trim();
  if (text.length > 400 || /["“”‘]|(?<!\w)['’]|['’](?!\w)/.test(text)) return null;
  // Never more permissive than the journal admission it feeds.
  const intent = ownerBriefIntent(text);
  const admitted = (turn: RequestedBriefTurn) => intent && intent.kind === turn.kind && (!turn.refresh || intent.refresh) ? turn : null;
  const direct = request(text);
  if (direct) return admitted({...direct, remainder: null});
  const clauses = text.split(/\s*,?\s+and\s+/i);
  if (clauses.length !== 2) return null;
  const matches = clauses.map(request);
  if (matches.filter(Boolean).length !== 1) return null;
  const index = matches[0] ? 0 : 1;
  const remainder = clauses[1 - index]!.trim();
  if (!/^(?:please\s+)?(?:tell|explain|text|send|draft|write|remind|schedule|look|find|check|summarize|show|give|update|what|when|where|who|how|why|can|could|would|is|are|do|does|did)\b/i.test(remainder)
      || /\b(?:it|this|that|them|those|brief)\b/i.test(remainder)) return null;
  return admitted({...matches[index]!, remainder});
}

export function isReadChatBriefRemainder(text: string): boolean {
  const direct = text.replace(/^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?/i, '');
  return /^(?:tell|explain|look|find|check|summarize|show|what|when|where|who|how|why|is|are|do|does|did)\b/i.test(direct);
}

function briefText(text: string): string {
  return text.trim().replace(/[‘’]/g, "'").replace(/\b(?:pls|plz)\b/gi, 'please');
}
const briefClause = /^(?:please\s+)?(?:(?:(?:can|could|would)\s+you|(?:can|could)\s+i)\s+)?(?:(?:(?:try|run|prepare|create|make|send|show|give|get|refresh|update)(?:\s+me)?|i(?:\s+would|'d)\s+like)\s+)?(?:(?:a|an|my|the)\s+)?(?:(fresh|current|updated)\s+)?(?:(morning|evening|daily)\s+)?brief(?:\s+again)?(?:,?\s+please)?[.!?]*$/i;
const negation = /\b(?:don't|do not|not|never|without|instead of|cancel|stop)\b/i;
const withdrawal = /^(?:(?:actually|then|but|so|oh|ok|okay|and|please)[\s,]+)*(?:don't|do not|not|never|nevermind|no|nope|without|instead|cancel|stop|skip|forget|scratch|ignore|hold|wait)\b/i;
/** Admission for model-selected wording; the fast path remains deliberately narrower. Only a
 * bounded composition continuation can extend a complete brief request; it cannot select new
 * request arguments. The receiver derives composition context from the verified owner message;
 * the model supplies only kind/refresh. Mirrored by owner_brief_intent in the receiver. */
export function ownerBriefIntent(text: string): {kind: RequestedBriefTurn['kind']; refresh: boolean} | null {
  const value = briefText(text);
  if (/["“”]|(?:^|\s)'/.test(value)) return null;
  const extended = /^(.+?)(?:\s*[,;]\s*|\s+[—–]\s+)(.+)$/s.exec(value);
  if (!briefClause.test(value) && extended && briefClause.test(extended[1]!) && !/^and\s+/i.test(extended[2]!)) {
    const modifier = extended[2]!;
    if (!/^(?:including|focus(?:ing|ed)|prioritiz(?:e|ing)|keep|covering|emphasiz(?:e|ing)|with)\b/i.test(modifier)
        || /\b(?:morning|evening|daily|fresh|current|updated|refresh|update|retry|again|cached|brief)\b/i.test(modifier)
        || negation.test(modifier)
        || modifier.split(/\s*,?\s+and\s+|[.!?;,]\s*|\s+[—–]\s+/i).some(clause => withdrawal.test(clause))) return null;
    return ownerBriefIntent(extended[1]!);
  }
  const clauses = value.split(/\s*,?\s+and\s+|[.!?]\s+/i).map(clause => clause.trim());
  const matches = clauses.map(clause => briefClause.exec(clause));
  if (matches.filter(Boolean).length !== 1
      || clauses.some((clause, i) => matches[i] ? negation.test(clause) : withdrawal.test(clause))) return null;
  const [clause, fresh, kind] = matches.find(Boolean)!;
  return {kind: (kind && kind.toLowerCase() !== 'daily' ? kind.toLowerCase() : 'auto') as RequestedBriefTurn['kind'],
    refresh: Boolean(fresh) || /\b(?:refresh|update)\b/i.test(clause)};
}
