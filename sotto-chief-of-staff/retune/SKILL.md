---
name: sotto-retune
description: Use when the user wants to clean up or tune Sotto — "clear my stale action items" / "clean up my open loops" / "tune up Sotto" / "this list is stale" / "I keep seeing the same items" / "retune my briefs" / "too noisy". Surfaces stale open loops (to dismiss / snooze / keep) and suggests mutes from what the user keeps dismissing. Read-then-confirm; never sends anything.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, continuity, preferences]
    category: productivity
    requires_tools: [execute_code]
required_environment_variables:
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: reading the continuity ledger + preferences
---

# Sotto — Retune & clear stale loops

A periodic tune-up: clear the open loops that have gone stale, and quiet the things the user keeps
dismissing. Everything here is **read → confirm → apply** — propose the cleanup, let the user pick,
then write it. **Never send a message, email, or calendar change from this skill.** Be brief and calm.

> **Don't auto-clear.** These are the user's commitments — surface them and let them decide. The one
> exception is obvious snooze on the user's explicit say-so. When unsure which loop they mean, ask.

## Procedure

> **Script paths:** `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/<name>.py"`.

1. **Scan** (read-only) — `execute_code`:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/_shared/scripts/retune_scan.py"
   ```
   Returns `stale_loops[]` (each with `anchor_key`, `name`, `what`, `direction`, `age_days`,
   `times_surfaced`, `overdue`, `suggestion`), `mute_suggestions[]`, and `current` (timezone +
   the mutes/tone in effect). If everything's empty, say so in one line ("Your loops are clean —
   nothing stale to clear") and stop.

2. **Present, grouped & short.** As Sotto, list the stale loops in two groups with the script's
   suggestion verb:
   - **You owe** → "do it or dismiss" (oldest / most-overdue first).
   - **Waiting on them** → "nudge or drop".
   Show `name — what` and why it's flagged (e.g. "surfaced 5×", "12 days old", "overdue"). Then list
   any **mute suggestions** ("You've dismissed Bob's items repeatedly — mute him?"). Keep it skimmable.

3. **Apply only what they choose.** Per their answers:
   - **Dismiss / snooze / keep a loop** — `retune_apply.py`:
     ```bash
     python3 "$HOME/.hermes/skills/sotto/_shared/scripts/retune_apply.py" dismiss <anchor_key>
     python3 "$HOME/.hermes/skills/sotto/_shared/scripts/retune_apply.py" snooze  <anchor_key> 7
     python3 "$HOME/.hermes/skills/sotto/_shared/scripts/retune_apply.py" keep    <anchor_key>
     ```
     `dismiss` = done with it (won't resurface). `snooze N` = hidden N days then back. `keep` = resets
     the aging clock so the 7-day auto-expiry won't drop something they still intend to do.
   - **Mute a sender / person / section, or set tone** — route through `sotto-feedback` (or call
     `preferences.py` directly: `mute-person "<name>"`, `mute-sender <addr|@domain>`, `mute-section <id>`,
     `tone "<note>"`). These take effect on the next brief.
   - **"Nudge them"** for a waiting loop → that's a *send*, so hand off to `sotto-draft-reply` (draft,
     honor approval tiers) — this skill never sends.
   - **Brief timing** ("move my morning brief to 7am") → adjust the host cron (`hermes cron`), then
     confirm the new time.

4. **Confirm in one line.** E.g. "Cleared 4 stale loops, snoozed 2 for a week, and I'll stop surfacing
   Bob. Your list is lighter now."

## Notes
- Loops auto-expire after 7 days regardless; this is the *user-driven* exit (dismiss/snooze/keep) plus
  the early catch (3–7 day window, repeat-surfacers) so they never pile into the brief.
- `dismiss` is terminal and pruned after 30 days; `snooze`/`keep` keep the loop, just reschedule it.
- Cron (optional): a gentle weekly tune-up —
  `hermes cron create "0 18 * * 5" "Run my retune" --skill sotto-retune` (Friday 6pm). Off by default;
  the skill is primarily on-demand so it never nags.
