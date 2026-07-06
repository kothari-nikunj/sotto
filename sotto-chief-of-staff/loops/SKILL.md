---
name: sotto-loops
description: Use when the user asks "what am I waiting on" / "what's open" / "my open loops" / "what do I owe" / "what's outstanding" / "my action ledger" / "loose ends" — surface the open loops from the continuity ledger, split into what the user owes vs what they're waiting on others for. Read-only; the brief resolves loops.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, continuity]
    category: productivity
    requires_tools: [execute_code]
required_environment_variables:
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: reading the continuity ledger
---

# Sotto — Open loops (waiting-on / action ledger)

Surface the user's open loops from the continuity ledger — the chief-of-staff "what's outstanding" view,
split by **direction**: what the user **owes** vs what they're **waiting on others** for.

## Procedure

1. **Query (deterministic — ONE command):**
   `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/loops_query.py"`
   It reads `$SOTTO_DATA/knowledge/continuity/*.md` (the ledger the brief maintains) and returns
   `{you_owe:[…], waiting_on_them:[…], counts}`. Each item: `{name, what, channel, identifier,
   age_days, deadline, overdue}`, oldest/most-overdue first. Do NOT hand-read the ledger.
2. **Deliver, tight and skimmable:**
   - Lead with the count ("4 you owe, 2 you're waiting on").
   - **You owe** — name + the one-line `what`, flag `overdue`/age ("3 days"). For the top items,
     offer a one-tap action (reuse the brief's tap-link logic / `sotto-draft-reply`): "say *draft Dhruv*."
   - **Waiting on them** — name + what you're awaiting + how long. Offer a nudge draft for stale ones.
   - If both lists are empty: "You're clear — no open loops." (One line; don't pad.)
   - Deliver as **Sotto**. Honor `sotto-approval-tiers` for any draft (never auto-send).

## Notes
- **Read-only.** This never resolves or writes loops — the morning/evening brief's Learn step
  (`continuity_resolve.py`) is the single place that opens/closes them. So this view is always
  consistent with the brief.
- `waiting_on_them` = items whose `action_type` is `waiting_on`/`follow_up_stale` (you acted, awaiting
  their side); everything else active is `you_owe`.
- This is the inverse companion to `sotto-triage` (which works the live inbox/threads); loops works the
  durable ledger across days.
