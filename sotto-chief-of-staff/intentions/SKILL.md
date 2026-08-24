---
name: sotto-intentions
description: Use when the user wants a one-time future check or reminder in plain language — “if Sarah hasn’t replied by Thursday at 3, nudge me”, “remind me tomorrow to review the deck”, “what one-time reminders do I have?”, or “cancel that reminder”. For recurring schedules, use sotto-routines instead.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, reminders, intentions]
    category: productivity
    requires_tools: [execute_code]
required_environment_variables:
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: one-shot intention state
---

# Sotto — One-shot intentions

Turn a plain-language future check into one durable wake-up serviced by the existing 15-minute
proactive heartbeat. This creates no cron and no second scheduler.

## Create

1. Resolve the user's due time into ISO 8601 with their configured timezone. Ask only when the date
   or hour is genuinely ambiguous; never guess “later”. Timing has 15-minute granularity.
2. For a conditional “if they haven't replied” request, run `sotto-loops` first and find the exact
   `waiting_on` item. Pass its `anchor_key`; when that loop closes before the due time, the intention
   auto-cancels. If no matching loop exists, say that plainly and offer an unconditional reminder.
3. Write it immediately—this is private, reversible bookkeeping, not an external action:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/_shared/scripts/schedule_wakeup.py" create \
     --due "2026-08-27T15:00:00-07:00" \
     --action "Sarah still hasn't replied about the deck — want me to draft a quick nudge?" \
     --context "Waiting on the revised board deck" \
     --anchor-key "<open-loop anchor, only for a conditional check>" --created-by user
   ```
   The same due time + action is idempotent, so a retried turn cannot create a duplicate.
4. Confirm in one short line with the date, time, and timezone. Never expose the internal id unless
   the user asks for it.

## List or cancel

- List: `schedule_wakeup.py list`. Render only scheduled items in plain language, soonest first.
- Cancel: list first, resolve the user's reference to exactly one item, then
  `schedule_wakeup.py cancel <id>`. If several match, ask which one. Confirm in one line.

## Guardrails

- One-shot only. Recurring requests belong to `sotto-routines`.
- The wake-up may remind, check a tracked open loop, or offer a draft. It may never send, book,
  purchase, or modify an external system on the user's behalf.
- Keep the action self-contained: the future run has the stored action/context, not this chat.
- Deliver as Sotto, never “Hermes Agent”.
