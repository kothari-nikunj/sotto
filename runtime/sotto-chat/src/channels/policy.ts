import type { Inbound, OwnerPolicy } from "./types.js";

/** Recovery ceiling matching the existing 180-minute default pending-offer lifetime; not a general recall window. */
export const PREP_OFFER_MAX_AGE_MS = 180 * 60_000;

export type Admission = { admitted: true; journalKey: string } | { admitted: false; reason: "wrong_channel" | "wrong_account" | "not_direct" | "not_owner" | "contentless" };

/** Run after normalization, before activation/model work; persistence belongs to the inbox journal. */
export function admitOwnerDirectMessage(event: Inbound, owner: OwnerPolicy): Admission {
  if (event.channel !== owner.channel) return { admitted: false, reason: "wrong_channel" };
  if (event.accountId !== owner.accountId) return { admitted: false, reason: "wrong_account" };
  if (!event.isDirect) return { admitted: false, reason: "not_direct" };
  if (event.senderId !== owner.ownerId) return { admitted: false, reason: "not_owner" };
  if (event.receiptOnly || (!event.text?.trim() && !event.media?.length)) return { admitted: false, reason: "contentless" };
  return { admitted: true, journalKey: JSON.stringify([event.channel, event.accountId, event.eventId]) };
}
