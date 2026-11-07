---
name: sotto-intentions
description: Use when the user wants a one-time future check or reminder in plain language — “if Sarah hasn’t replied by Thursday at 3, nudge me”, “remind me tomorrow to review the deck”, “what one-time reminders do I have?”, or “cancel that reminder”. For recurring schedules, use sotto-routines instead.
metadata:
  tags: [chief-of-staff, sotto, reminders, intentions]
  category: productivity
---

# Sotto — One-shot intentions

Turn a plain-language future check into one durable wake-up serviced by the existing 15-minute
proactive heartbeat. This creates no cron and no second scheduler.

> **How this runs on Sotto:** `run_sotto_script X …` means one `run_sotto_script` call with id `X`
> and the rest as its argv array — no shell, no file paths. Other backticked names are native tools.

## Create

1. Resolve the user's due time into ISO 8601 with their configured timezone — `owner_reminder_context`
   supplies the current owner time and zone. Ask only when the date or hour is genuinely ambiguous;
   never guess “later”. Timing has 15-minute granularity.
2. For a conditional “if they haven't replied” request, read `run_sotto_script loops-query`, select one
   exact current `waiting_on_them` row, and use `owner_reminder_conditional` with its anchor, version
   and identity plus an exact condition quote from the owner. When that loop closes before the due
   time, the intention auto-cancels. If several rows fit, clarify; never choose an arbitrary loop. A
   snoozed or unavailable loop is held, not treated as closed. If no matching loop exists, say that
   plainly and offer an unconditional reminder.
3. Otherwise record it immediately with `owner_reminder_create`, using an explicit-offset due time,
   the self-contained action ("Sarah still hasn't replied about the deck — want me to draft a quick
   nudge?") and its context ("Waiting on the revised board deck"). This is private, reversible
   bookkeeping, not an external action, so no second approval is needed. A retried turn cannot create
   a duplicate.
4. Confirm in one short line with the date, time, and timezone. Never expose the internal id unless
   the user asks for it.

## List or cancel

- List: use `owner_reminder_list {}`: it reads up to 50 scheduled reminders for this owner from the same canonical store, including slash and natural-language creations. Render the returned items in plain language, soonest first, using the returned timezone; do not expose internal IDs unless asked. Listing creates no reminder and does not authorize a cancellation or conditional check.
- Cancel: first use `owner_reminder_list {}` and then `owner_reminder_cancel` with the exact listed reminder ID and an exact cancellation quote from the current owner message. If several reminders fit, ask which one and do not cancel an arbitrary item. Confirm only a proven cancellation in one line; a fired or unavailable reminder cannot be canceled.

## Guardrails

- One-shot only. Recurring requests belong to `sotto-routines`.
- The wake-up may remind, check a tracked open loop, or offer a draft. It may never send, book,
  purchase, or modify an external system on the user's behalf.
- Keep the action self-contained: the future run has the stored action/context, not this chat.
- Deliver as Sotto.
