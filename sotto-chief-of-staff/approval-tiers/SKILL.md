---
name: sotto-approval-tiers
description: Use when Sotto is about to send, schedule, or execute anything on the user's behalf — the always-on policy defining what may run automatically vs. needs confirmation vs. is forbidden.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, policy]
    category: productivity
    requires_tools: [execute_code]
---

# Sotto — Approval Tiers (autonomy policy)

PORT SOURCE: api/src/services/approval-policy.ts. **Never exceed a tier without explicit user say-so.**

| Tier | Meaning | Default actions |
|---|---|---|
| `auto` | run immediately, no confirmation; just log it | meeting info, meeting prep, opening a meeting link, copying talking points, calls (`tel:`) |
| `one_tap` | one confirmation, then run | iMessage / SMS, WhatsApp, calendar RSVP |
| `review` | show full content, allow edits, confirm, then run | email drafts, follow-ups, calendar create/reschedule |
| `forbidden` | never auto-execute; surface only | anything destructive, financial, or irreversible |

## Applying it
- Default an action to its tier above. When in doubt, escalate (treat as `review`).
- **Learned overrides:** the morning brief's Learn step runs `scripts/learn_preferences.py`, which tallies `$SOTTO_DATA/outcomes.jsonl` into `preferences.json` → `approval_defaults` (keyed `contact|action_type`, emitted only after ≥3 accepted outcomes at ≥80% acceptance). Honor them **narrowly**: a learned default may relax `review` → `one_tap` for that **exact** contact + action_type only. A learned default NEVER relaxes anything into `auto`, never overrides `forbidden`, and never overrides an explicit user preference (the reserved `explicit` block in `preferences.json` — user-stated prefs always win). When the learned default is *stricter* than the table, apply the stricter tier.
- A scheduled/unattended send may only use `auto` (and `one_tap` only if the user has pre-approved that recipient). That unattended `one_tap` allowance covers **message drafts** (iMessage/SMS/WhatsApp) to pre-approved recipients ONLY — it does **not** cover the `one_tap` calendar write. A calendar RSVP is NEVER unattended: it may run only on the user's explicit in-chat instruction in the same conversation, never from a scheduled/proactive/cron context. When unattended and an action needs `review` (or is a calendar write), queue it for the next interaction instead of sending.
