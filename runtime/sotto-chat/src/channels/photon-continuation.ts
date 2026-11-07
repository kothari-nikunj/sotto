import {PREP_OFFER_MAX_AGE_MS} from './policy.js';
import {cloud} from '@spectrum-ts/core';
import {createGrpcClient} from '@photon-ai/advanced-imessage/grpc';
import {resolvePhotonCredentials} from './credentials.js';
import type {CredentialSource} from './types.js';

export interface ContinuationMessage {
  guid: string; isFromMe: boolean; dateCreated?: Date; sender?: {address?: string};
}

/** The direct history reader uses v2 gRPC; the Photon adapter owns its separate v1 send client. */
export function createPhotonHistoryClient(token: string,
    endpoint = {address: 'imessage.spectrum.photon.codes:443', tls: true}) {
  return createGrpcClient({...endpoint, token, retry: false, timeout: 5_000});
}
/** A complete bounded provider window proves adjacency, without interpreting text. */
export function precedingPrepMessage(page: {messages: readonly ContinuationMessage[]; nextPageToken?: string},
    input: {inboundId: string; inboundAtMs: number}, ownerId: string) {
  if (page.nextPageToken || !page.messages.length || page.messages.length > 20) return null;
  const rows = page.messages.map(row => ({...row, at: row.dateCreated?.getTime()}));
  if (rows.some(row => typeof row.guid !== 'string' || !row.guid || row.guid.length > 512 || !Number.isSafeInteger(row.at))
      || new Set(rows.map(row => row.guid)).size !== rows.length) return null;
  rows.sort((a, b) => a.at! - b.at!);
  if (rows.some((row, i) => i > 0 && row.at === rows[i - 1]!.at)) return null;
  const index = rows.findIndex(row => row.guid === input.inboundId);
  const incoming = rows[index], previous = rows[index - 1];
  if (!incoming || incoming.isFromMe !== false || incoming.sender?.address !== ownerId
      || incoming.at !== input.inboundAtMs || !previous || previous.isFromMe !== true
      || previous.at! >= input.inboundAtMs || input.inboundAtMs - previous.at! >= PREP_OFFER_MAX_AGE_MS
      || rows.some(row => row.at! > input.inboundAtMs)) return null;
  return {messageId: previous.guid, sentAtMs: previous.at!};
}

/** Include the exact accepted candidate, avoiding unrelated earlier busy-chat traffic. */
export function prepContinuationWindowStart(inboundAtMs: number, candidateAtMs?: number): number | null {
  if (!Number.isSafeInteger(inboundAtMs) || inboundAtMs <= 0) return null;
  // Legacy receipts without native time retain the existing bounded 15-minute lookup.
  if (candidateAtMs === undefined) return inboundAtMs - 15 * 60_000;
  if (!Number.isSafeInteger(candidateAtMs) || candidateAtMs <= 0 || candidateAtMs >= inboundAtMs
      || inboundAtMs - candidateAtMs >= PREP_OFFER_MAX_AGE_MS) return null;
  return candidateAtMs - 1;
}

/** Existing shared Photon credentials, one exact owner DM, no listener or send. */
export function createPhotonPrepContinuation(source: CredentialSource, ownerId: string,
    decode: (threadId: string) => unknown) {
  const credentials = resolvePhotonCredentials(source);
  return async (input: {threadId: string; inboundId: string; inboundAtMs: number; candidateAtMs?: number}) => {
    if (!credentials.projectId || !credentials.projectSecret || !/^\+[1-9][0-9]{7,14}$/.test(ownerId)
        || !Number.isSafeInteger(input.inboundAtMs) || Math.abs(Date.now() - input.inboundAtMs) > 15 * 60_000) return null;
    const after = prepContinuationWindowStart(input.inboundAtMs, input.candidateAtMs);
    if (after === null) return null;
    const decoded = decode(input.threadId);
    if (!decoded || typeof decoded !== 'object') return null;
    const {chatGuid, phone} = decoded as {chatGuid?: unknown; phone?: unknown};
    if (chatGuid !== `any;-;${ownerId}` || phone !== 'shared') return null;
    let expired = false;
    const work = async () => {
      const issued = await cloud.issueImessageTokens(credentials.projectId!, credentials.projectSecret!);
      if (expired || issued.type !== 'shared') return null;
      const client = createPhotonHistoryClient(issued.token);
      try {
        const page = await client.messages.listInChat(chatGuid, {pageSize: 20,
          after: new Date(after), before: new Date(input.inboundAtMs + 1)});
        return precedingPrepMessage(page, input, ownerId);
      } finally {await client.close();}
    };
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {return await Promise.race([work(), new Promise<null>(resolve => {timer = setTimeout(() => {expired = true; resolve(null);}, 8_000);})]);}
    catch {return null;}
    finally {if (timer) clearTimeout(timer);}
  };
}
