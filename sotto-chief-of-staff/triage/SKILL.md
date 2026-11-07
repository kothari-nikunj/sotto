---
name: sotto-triage
description: 'Use when the user says "triage my inbox" / "let''s clear what needs me" / "process my messages" / "inbox zero" / "what do I owe people" / "clear my queue" — walk the cross-channel needs-a-reply queue (email + iMessage + WhatsApp) one item at a time: draft a reply, or record an explicit correction so a handled obligation stops re-surfacing. This is THE way to run a triage pass — never hand-improvise a list.'
metadata:
  tags: [chief-of-staff, sotto, triage]
  category: productivity
---

# Sotto — Triage (clear what needs you, across channels)

A focused working session to clear the cross-channel **needs-a-reply** queue — email AND local
(iMessage/WhatsApp). Unlike the brief (informational), triage is **action**: for each item, draft a
reply, or record an explicit correction so tomorrow's brief doesn't renag.

> **How this runs on Sotto:** `run_sotto_script X …` means one `run_sotto_script` call with id `X`
> and the rest as its argv array — no shell, no file paths. Other backticked names are native tools.

## Procedure

1. **Build the queue — ONE command** (deterministic — don't hand-fetch): `run_sotto_script triage-queue`
   (no arguments). The receiver gathers the bounded current consented inputs (local messages from
   the last 72 hours, recent and unread email) and builds the cross-channel candidate inventory → JSON
   `{email[], imessage[], whatsapp[], counts, coverage}`. Each item is name-resolved with a
   `last_snippet`. `coverage` names disabled, unavailable and partial sources; an empty list is not
   a complete inbox audit. The grouped queue is candidate inventory, not a priority decision: important/unread email and a latest inbound message do not prove that a reply is owed.
   In the first item, introduce this as a recent-message pass. Any N in an item count means items in this pass, not the total inbox. Briefly name sources reported partial or unavailable in the returned coverage; do not invent gaps or wait until the wrap-up to disclose them.
2. **Choose the next useful action before proposing a reply.** Use the current exchange to identify the ask, what the owner already did, later fulfillment and any unresolved dependency. For email, use the existing `gmail_thread_get` with the candidate's exact `threadId` when the full exchange is not already available; a queue snippet alone cannot establish current work. A newer acknowledgment does not itself renew an ask, but an acknowledgment can also contain a new question. An owner's promise to consult the team remains open until fulfilled; while that consultation is pending, name that step instead of proposing a redundant holding reply. Compare the other candidates when the first is blocked or handled. Prefer a supported ready action using the owner's priorities and any stated deadline, regardless of channel. If none is ready, say so within the observed coverage rather than manufacturing a reply. Honor an explicitly requested courtesy draft without presenting it as necessary. Do not close, snooze or change any record during this judgment.
3. **For the selected action, propose ONE useful next step** (fast pass — no narrative):
   - **Reply** → hand to `sotto-draft-reply` (drafts in the user's voice; a reviewed Gmail draft or send for email). For an email item, read the selected item’s `message_id` directly with `gmail_get`; verify the returned `threadId` matches that queue item before drafting. Only older items without a `message_id` need `gmail_search` / `gmail_get` to identify an exact thread match. If the message is gone or its thread differs, refresh the queue instead of guessing a replacement. When the current queue already grounds the exact message, unambiguous sender and reply topic, request `gmail_get` and `knowledge-query` together in one tool step; they are independent reads. If any of those inputs is missing or ambiguous, read/resolve the message first. Wait for both results and verify the returned `threadId` before drafting. Then run `style-apply` using the exact returned canonical ID when available; do not invent an ID or run style before the person lookup returns. iMessage/SMS/WhatsApp replies are drafted for the user to send in the app themselves.
   - **Declinable ask → two drafts, not one** (`sotto-draft-reply`'s no-draft rule). If the item is a request the user could plausibly say no to — time, money, an intro, a favor — the item gets `Accept:` and `Decline:` as two labeled drafts, decline second, or decline FIRST when it's clearly low-value (cold sender, templated outreach, a deprioritized contact). The decline follows the decline register in `_shared/references/voice.md`. Present both flat and let the user pick one; the queue moves on either way. A decline is `review` tier forever — full text, an explicit go-ahead, never one-tapped and never swept into a batch approval — and it carries `action_type: "decline"` both when it is OFFERED (`gmail_draft_pair` records the offered pair for email) and when it is saved or sent.
   - **Email housekeeping** (no reply needed) → archive and label are not available in Sotto chat; say so in one line and leave the message as it is.
   - **Skip / snooze** → leave it; it stays in the queue. A skip/next request advances only the selected item. Prior tool results are not retained as chat history: if the next item’s exact queue identity/context is unavailable this turn, rerun triage-queue and use the accepted owner skip history to find the next unprocessed item; clarify if that selection is ambiguous. Before drafting for every next item, read that exact item and apply the same draft-reply context and Verify gate. Queue snippets and the previous item’s draft are not enough. Do not say a link, attachment or instructions are being supplied unless the actual material is available and included; otherwise briefly ask for it or offer to prepare it.
   - Honor the approval tiers (`_shared/references/approval-tiers.md`) before saving or sending anything.
4. **Record an explicit correction** — when the owner explicitly says an obligation is done or dismissed, read the current canonical loops (`run_sotto_script loops-query`) and identify one exact obligation using its source thread and wording. Use the existing source/revision-bound correction command (`run_sotto_script knowledge-edit --op loop …`, see `sotto-loops` §C) with the owner's actual words, then read the loops back. Ask if the target is ambiguous; if no matching obligation exists, say so. Generic contact-level `signals.handled` or `replied_thread_ids` do not close ordinary obligations. A saved draft is not a sent reply or a completed obligation; never close one merely because a draft was saved. A canonical correction also does not remove the original source message from the inbox, and its canonical counts are distinct from this fresh inbox queue.
5. **Wrap** — run `run_sotto_script triage-queue` again and give one short, accurate summary: drafts saved, confirmed actions or canonical corrections, and the returned queue counts. Report unavailable or partial source coverage. Count an item as cleared only when the actual action and readback support that claim; an unsent draft's inbound thread can remain in the queue. Offer "continue triage" for the remaining items.

## Notes
- **Group chats: never a deep link.** The queue emits group threads with `is_group_chat: true` and `identifier: ""` — there is no per-group address to link to (same rule as the brief, which never emits a link for a group). Their only actions are **reply in the app yourself** or an explicit correction; never create a link or draft destination from them. Group thread lines may carry per-sender attribution ("Name: message") — quote a message only as the **named sender's**, never attribute it to the group or another member.
- **Local has no archive.** You can't archive/label a text — for iMessage/WhatsApp the only triage actions are **reply** or an explicit correction.
- Keep it fast and skimmable — triage is a clearing pass, not a brief. No prose, no calendar.
- Local data comes from the Bridge; if it's unreachable, triage **email only** and say so in one line.
- Deliver as **Sotto**.
