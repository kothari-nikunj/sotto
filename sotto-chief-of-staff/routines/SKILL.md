---
name: sotto-routines
description: Use when the user wants a recurring job of their OWN — "every Friday at 4, summarize my open loops by person", "set up a routine", "every Monday remind me to…", "what routines do I have", "stop the Friday summary", "move my Friday routine to 5". Creates/lists/removes PERSONAL scheduled jobs only (the `user-` name prefix); it never touches the five system jobs (morning/evening brief, relationship pulse, proactive check, midday digest) — those live in adapters/hermes/crons.json and are changed via sotto-setup.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, routines, scheduling]
    category: productivity
    requires_tools: [terminal]
---

# Sotto — Personal routines (say it once, it runs on your clock)

"Every Friday at 4pm, summarize my open loops by person" → a real recurring job. One sentence in,
one job out. Personal routines are **fenced off** from Sotto's own schedule: every job you create
here is named `user-<slug>`, and nothing without that prefix is yours to touch.

> Run the `hermes cron …` commands through the `terminal` tool, verbatim as written below.
> **On a non-Hermes host, use that host's scheduler with the same values** — OpenClaw is
> `openclaw cron add "<cron>" "<prompt>" --name user-<slug> --declaration-key sotto:user-<slug> --announce --channel "${SOTTO_CRON_DELIVER:-whatsapp}"`,
> plus `openclaw cron list` / `openclaw cron remove`. The fence below is host-independent.

## The fence (read once, obey always)

- **Create only `user-` names.** `--name user-<slug>` — always. Never create a name starting with
  `sotto-`, and never create a job that duplicates a system one (a second morning brief, a second
  proactive watcher). If the user wants a *system* job at a different time, say so plainly and point
  at `sotto-setup` — don't shadow it with a routine.
- **Remove only `user-` jobs.** Before any `hermes cron remove`, confirm from `hermes cron list`
  that the job you matched carries the `user-` prefix. If it doesn't, refuse in one line: "That's one
  of Sotto's own jobs — I won't touch it here."
- **A routine's prompt must never quote a system job's prompt.** ("Run my morning brief", "Run my
  evening brief", "Run my relationship pulse", "Run my proactive check", "Run my midday digest".)
  Boot-time cleanup recognizes system jobs partly by those exact strings; a routine wearing one is
  asking to be mistaken for a duplicate. Rephrase the ask in the user's own words instead.
- **Drafts, never sends — inherited.** A routine prompt may not instruct Sotto to send, reply,
  forward, book, or spend on the user's behalf. It may compose, summarize, prep, and **deliver its
  own result** to the user's channel. Anything actionable it produces is a draft the user sends
  (the approval tiers still govern every one).
- **No routine schedules a routine.** A routine prompt may not create, edit, or remove crons.
  Recursion here is how you wake up to forty jobs.
- **Cap: 10 routines.** At the 11th, list what's there and ask which to drop first.

## Procedure

### CREATE — "every Friday at 4pm, summarize my open loops by person"

1. **Read the current list first (dedup, always).** `hermes cron list`. Look at the `user-` jobs
   only. If one already covers this ask (same name, or plainly the same thing on the same day),
   don't add a second — offer to **replace** it (remove, then create) or to adjust the time. This is
   the same discipline `sotto-setup` uses for the system jobs; duplicate crons have caused 429 storms
   here before. If there are already 10 `user-` jobs, stop and ask which one to drop.
2. **Turn the words into a cron expression — in the user's LOCAL time.** The container's timezone is
   already the user's (`/setup` persists it; `SOTTO_TIMEZONE`/`TZ` override), so a plain `0 16 * * 5`
   *is* Friday 4pm their time — never convert to UTC yourself.
   - "every Friday at 4pm" → `0 16 * * 5` · "weekday mornings at 8" → `0 8 * * 1-5` ·
     "the 1st of every month at 9am" → `0 9 1 * *` · "every Sunday evening" → ask which hour rather
     than guessing (a routine that fires at the wrong hour is worse than one question).
   - **Nothing finer than hourly.** If they ask for every 5 minutes, say what it would cost them in
     interruptions and offer hourly or a daily digest instead.
3. **Compose the job's prompt — standalone.** The prompt IS the entire message a fresh Sotto turn
   will receive: no memory of this conversation, no "as we discussed". Write it as a complete
   instruction in the user's own words, naming the skill to use when there is an obvious one:
   > "Summarize my open loops grouped by person — who I owe, who I'm waiting on, and how long each
   > has been open. Use the sotto-loops view. Keep it under 10 lines."
   Keep it one short paragraph. No dates ("this Friday") — it recurs. No sending instructions.
4. **Propose before creating — ONE message.** Schedule in plain words + what it will do + where it
   lands:
   > "Every Friday at 4:00pm your time, I'll send you a summary of your open loops grouped by
   > person — who you owe, who you're waiting on, how long each has been open. It'll arrive here on
   > WhatsApp. Want me to set that up?"
   Create **only** on a clear yes. If they tweak it, re-propose the changed line (don't re-explain).
5. **Create it.** Slug the subject: lowercase, hyphens, ≤24 chars (`user-open-loops-friday`).
   ```
   hermes cron create "<cron>" "<prompt>" --name user-<slug> --deliver "${SOTTO_CRON_DELIVER:-whatsapp}"
   ```
   `--deliver` is not optional: without it the job runs into the default `local` sink and the user
   never sees it. There is no `--skill` flag here — routines are prompt-based, and the prompt names
   the skill.
6. **Confirm in one line**, with the words, not the cron: "Done — *open-loops summary*, every Friday
   at 4pm, delivered here. Say *stop the Friday summary* any time."

### LIST — "what routines do I have"

`hermes cron list`, then show **only** the `user-` jobs, rendered plainly — schedule in words, then
what it does. Never print cron expressions, job ids, or the system jobs:

> - **Friday 4pm** — open-loops summary by person
> - **Weekdays 8am** — reading list from yesterday's saved links

If there are none: "No personal routines yet — tell me one in a sentence and I'll set it up
(*every Friday at 4, summarize my open loops by person*)." If asked about the briefs themselves, say
in one line that the morning/evening briefs, the weekly pulse and the watchers are Sotto's own
schedule and are changed via *set up Sotto* — then stop.

### REMOVE — "stop the Friday summary"

1. `hermes cron list` and match the user's plain reference (day, time, subject) against the `user-`
   jobs. **One match** → proceed. **Several** → ask which, showing them the way LIST does. **None**
   → say so; don't guess.
2. Verify the match's name starts with `user-`, then remove it by that name (the id from the list
   also works). Answer any confirmation prompt non-interactively — never leave the command hanging:
   ```
   hermes cron remove user-<slug>
   ```
3. Confirm what went: "Removed — the Friday 4pm open-loops summary. Nothing else changed."

**Edit = remove + create** (there is no in-place update). Say it as one change, not two.

## Notes

- **Timezone changes don't move existing routines (v1).** Hermes captures the zone when a job is
  created, and only Sotto's own five jobs are re-registered when you change your timezone — your
  personal routines keep firing on the OLD clock until you recreate them. If the user changes zones,
  offer to recreate their routines; don't pretend they followed.
- **Where the fence is enforced in code:** boot cleanup (`adapters/hermes/start.sh`) removes only the
  jobs named/prompted in `adapters/hermes/crons.json`, and explicitly skips any block carrying a
  `user-` name; the receiver's timezone re-registration walks `crons.json` and drops `user-` names
  too. So a redeploy never eats a routine — but that safety rests on the prefix, which is why the
  fence above is absolute.
- **Single user.** These are the account owner's routines; there is no per-person scoping and no
  sharing. Everything a routine can see, the owner can already see.
- Deliver as **Sotto**, never "Hermes Agent". One short line before doing the work at most —
  the proposal and the confirmation are the deliverables here.
- Routine *suggestions* (Sotto proposing a routine from an observed rhythm) are not this skill's job
  — the weekly pulse may offer one, and it lands here as a normal CREATE with the same proposal step.
