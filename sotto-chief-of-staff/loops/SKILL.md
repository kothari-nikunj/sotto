---
name: sotto-loops
description: 'Use when the user asks what is outstanding — "what am I waiting on" / "what''s open" / "my open loops" / "what do I owe" / "my action ledger" / "loose ends" — or wants the list tidied: "clear my stale action items" / "clean up my open loops" / "this list is stale" / "I keep seeing the same items" / "tune up Sotto" / "too noisy". Reads the continuity ledger, split into what the user owes vs what they are waiting on others for, and on request clears the stale ones. Never sends anything.'
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

# Sotto — Open loops (and clearing the stale ones)

Two jobs over one ledger: **show what's outstanding**, and — when the user asks — **clear what's
gone stale**. Both are read-first. This skill never sends a message, email, or calendar change.

> Scripts live under `$HOME/.hermes/skills/sotto/`. Use absolute paths.

## A · What's open

1. **Query — ONE command.** `execute_code` →
   `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/loops_query.py"`
   It reads `$SOTTO_DATA/knowledge/continuity/*.md` (the ledger the brief maintains) and returns
   `{you_owe:[…], waiting_on_them:[…], counts}`. Each item: `{name, what, channel, identifier,
   age_days, deadline, overdue, chased_count, last_chased_at, chased_out}`, oldest and most-overdue
   first. Don't hand-read the ledger.
2. **Deliver, tight and skimmable.**
   - Lead with the count ("4 you owe, 2 you're waiting on").
   - **You owe** — name + the one-line `what`, flag `overdue` or age ("3 days"). For the top items
     offer a one-tap action (the brief's tap-link logic, or `sotto-draft-reply`): "say *draft Dhruv*."
   - **Waiting on them** — name, what you're awaiting, how long, **and what Sotto already did about
     it**. Each item carries `chased_count` / `last_chased_at` / `chased_out`, so say it plainly
     ("chased once, Tuesday" · "I've nudged twice — want me to drop it?") instead of offering a
     nudge as if nothing had been tried. `chased_out` means automatic nudging has stopped and the
     call is theirs. Offer a nudge draft for the un-chased stale ones.
   - Both lists empty: "You're clear — no open loops." One line; don't pad.
   - Deliver as **Sotto**. Any draft honors the approval tiers
     (`_shared/references/approval-tiers.md`) — never auto-send.

## B · Clearing the stale ones

Read → confirm → apply. Propose the cleanup, let the user pick, then write it. **Don't auto-clear**
— these are their commitments. The one exception is an obvious snooze on their explicit say-so;
when you can't tell which loop they mean, ask.

1. **Scan (read-only)** — `execute_code`:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/_shared/scripts/retune_scan.py"
   ```
   Returns `stale_loops[]` (each with `anchor_key`, `name`, `what`, `direction`, `age_days`,
   `times_surfaced`, `overdue`, `deadline`, `chased_count`, `chased_out`, `suggestion`),
   `mute_suggestions[]`,
   and `current` (timezone + the mutes and tone in effect). All empty → say so in one line ("Your
   loops are clean — nothing stale to clear") and stop.
2. **Present, grouped and short**, using the script's suggestion verb:
   - **You owe** → "do it or dismiss", oldest and most-overdue first.
   - **Waiting on them** → "nudge or drop" — except the ones Sotto has already chased twice
     (`chased_out: true`, verb "chased twice — nudge again or drop"). **Lead with those** and say it
     out loud: the automatic nudging has stopped, and this is the decision it hands back. It's the
     one thing here Sotto owes the user rather than merely suggests, so surface it on its own even
     when the rest of the list is short.
   Show `name — what` and why it's flagged ("surfaced 5×", "12 days old", "overdue"). Then any
   **mute suggestions** ("You've dismissed Bob's items repeatedly — mute him?").
3. **Apply only what they chose.**
   - **Dismiss / snooze / keep** — `retune_apply.py`:
     ```bash
     python3 "$HOME/.hermes/skills/sotto/_shared/scripts/retune_apply.py" dismiss <anchor_key>
     python3 "$HOME/.hermes/skills/sotto/_shared/scripts/retune_apply.py" snooze  <anchor_key> 7
     python3 "$HOME/.hermes/skills/sotto/_shared/scripts/retune_apply.py" keep    <anchor_key>
     ```
     `dismiss` = done with it, won't resurface. `snooze N` = hidden N days, then back. `keep` = "I
     still care": on something they **owe**, it resets the clock so the 7-day drop-off won't take
     it; on something they're **owed**, it resets the nudge count so Sotto chases again from
     scratch. Either way, tell them which one they got.
   - **"too noisy right now" / "quieter today"** — a *cadence* change, not a mute: route to
     `sotto-feedback` §C (`preferences.py snooze-nudges tomorrow | "+2h" | 15:00`, and
     `unsnooze-nudges` for "back to normal"). Snoozes expire on their own; mutes don't.
   - **Mute a sender / person / section, or set tone** — route through `sotto-feedback` (or call
     `preferences.py` directly: `mute-person "<name>"`, `mute-sender <addr|@domain>`,
     `mute-section <id>`, `tone "<note>"`). These take effect on the next brief.
   - **"Nudge them"** on a waiting loop is a *send* — hand off to `sotto-draft-reply`. This skill
     never sends.
   - **Brief timing** ("move my morning brief to 7am") → `sotto-setup` owns the system crons.
4. **Confirm in one line.** "Cleared 4 stale loops, snoozed 2 for a week, and I'll stop surfacing
   Bob. Your list is lighter now."

## Notes
- **This skill never resolves loops on its own.** The brief's Learn step (`continuity_resolve.py`)
  is the single place that opens and closes them; §B only applies what the user just chose. So the
  view is always consistent with the brief.
- `waiting_on_them` = the things the OTHER side owes — the only kind Sotto chases. Everything else
  active, including a thread gone quiet that needs YOUR nudge, is `you_owe`. One predicate decides
  it (`ledger_io.WAITING_ON_TYPES`), shared with the brief and the stale scan, so all three agree
  about which way a debt points.
- **Direction changes the ending.** What the user owes drops off quietly after 7 silent days (and on
  a passed due date). §B catches those earlier — a loop is stale at 4 days old, or 3 surfacings, or
  once it's overdue (`retune_scan.STALE_AGE_DAYS` / `STALE_SURFACED`) — which is the user-driven
  exit ahead of the silent sweep. What someone owes THEM never drops off on its own; a due
  date only makes it urgent sooner. It closes when they deliver, or Sotto nudges twice and then asks
  ("nudge again or drop"). So "what am I waiting on" can legitimately show old items — that's the
  point, not a leak, and §B is their only exit.
- **The one exception:** something someone owes with no way to reach them (no email, no number) can
  neither arrive nor be chased, so after its two nudges it closes as *unreachable*. Say that plainly
  if it comes up: "I had no way to chase this one, so I've let it go."
- **A nudge counts only when it was delivered.** A chase Sotto proposed but never sent (quiet hours,
  the day's interrupt budget spent) is not counted against the two, so the numbers the user hears
  are nudges they really received.
- `dismiss` is terminal and pruned after 30 days; `snooze` and `keep` keep the loop and just
  reschedule it.
- Inverse companion to `sotto-triage`, which works the live inbox and threads. This works the
  durable ledger across days. For a standing weekly tidy-up, `sotto-routines` can create the job.
