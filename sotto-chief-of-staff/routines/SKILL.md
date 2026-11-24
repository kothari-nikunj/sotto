---
name: sotto-routines
description: Use when the user wants a recurring job of their OWN — "every Friday at 4, summarize my open loops by person", "set up a routine", "every Monday remind me to…", "what routines do I have", "stop the Friday summary", "move my Friday routine to 5". Creates/lists/adjusts/removes PERSONAL scheduled jobs only (the `user-` name prefix); it never touches the five system jobs (morning/evening brief, relationship pulse, proactive check, midday digest) — those are Sotto's own schedule and are changed via sotto-setup.
metadata:
  tags: [chief-of-staff, sotto, routines, scheduling]
  category: productivity
---

# Sotto — Personal routines (say it once, it runs on your clock)

"Every Friday at 4pm, summarize my open loops by person" → a real recurring job. One sentence in,
one job out. Personal routines are **fenced off** from Sotto's own schedule: every job you create
here is named `user-<slug>`, and nothing without that prefix is yours to touch.

> **How this runs on Sotto:** the routine tools are native tools — `owner_routines` (list),
> `owner_routine_propose` → `owner_routine_create` (create), `owner_routine_adjust_propose` →
> `owner_routine_adjust_confirm` (move), and `owner_routine_cancel` (remove). The receiver owns the
> schedule and the fence checks. The destination is fixed by the receiver. Never name or guess a channel.

## The fence (read once, obey always)

- **Create only `user-` names.** The proposal's `name` is always `user-<slug>`. Never create a name
  starting with `sotto-`, and never create a job that duplicates a system one (a second morning
  brief, a second proactive watcher). If the user wants a *system* job at a different time, say so
  plainly and point at `sotto-setup` — don't shadow it with a routine.
- **Remove only `user-` jobs.** `owner_routines` lists only personal routines — imported candidates
  and system jobs are excluded — and `owner_routine_cancel` refuses anything else. If the user
  points at one of Sotto's own jobs, refuse in one line: "That's one of Sotto's own jobs — I won't
  touch it here."
- **A routine's prompt must never quote a system job's prompt.** ("Run my morning brief", "Run my
  evening brief", "Run my relationship pulse", "Run my proactive check", "Run my midday digest".)
  A routine wearing one is asking to be mistaken for a duplicate. Rephrase the ask in the user's
  own words instead.
- **Drafts, never sends — inherited.** A routine prompt may not instruct Sotto to send, reply,
  forward, book, or spend on the user's behalf. It may compose and summarize from permitted reads, and **deliver its
  own result** to the user's channel. Anything actionable it produces is a draft the user sends
  (the approval tiers still govern every one).
- **No routine schedules a routine.** A routine prompt may not create, edit, or remove routines.
  Recursion here is how you wake up to forty jobs.
- **Cap: 10 routines.** At the 11th the receiver refuses (`routine_limit`): list what's there and
  ask which to drop first.

## Procedure

### CREATE — "every Friday at 4pm, summarize my open loops by person"

1. **Read the current list first (dedup, always).** `owner_routines`. If one already covers this ask
   (same name, or plainly the same thing on the same day), don't add a second — offer to **adjust**
   its time or to **replace** it (cancel, then create). The receiver also refuses an exact duplicate
   (`routine_conflict`). If there are already 10, stop and ask which one to drop.
2. **Turn the words into a schedule — in the user's LOCAL time.** The receiver captures the owner's
   configured timezone with the proposal, so a plain hour *is* their time — never convert to UTC
   yourself. The schedule is `{hour, minute, weekday}`:
   - "every Friday at 4pm" → `{hour: 16, minute: 0, weekday: 5}` (Sunday = 0) · "weekday mornings
     at 8" → `{hour: 8, minute: 0, weekday: "weekdays"}` · "Mondays and Thursdays at 9" → `weekday:
     [1, 4]` (ascending, unique) · "every day at 7am" → `weekday: null` · "the 1st of every month at
     9am" → `weekday: "first_of_month"` · "every Sunday evening" → ask which hour rather than
     guessing (a routine that fires at the wrong hour is worse than one question).
   - **Nothing finer than hourly.** Hourly is `{hour: null, minute: 0, weekday: null}`; another
     minute offset must be owner-stated. If they ask for every 5 minutes, say what it would cost them
     in interruptions and offer hourly or a daily digest instead.
3. **Check execution capability, then compose the job's prompt — standalone.** Scheduled runs support simple reminders, current consent-filtered canonical loops (`run_sotto_script loops-query`), bounded Gmail/Calendar reads and permitted local Bridge reads. They cannot run web research, full meeting-prep/background workflows or mutations. Explain an unsupported request and offer a supported narrower routine before proposing; do not promise unavailable work. Source access is checked again when the routine runs, so a current grant is not a guarantee of future coverage.

   The prompt IS the entire message a fresh Sotto turn
   will receive: no memory of this conversation, no "as we discussed". Write it as a complete
   instruction in the user's own words using the available reads:
   > "Summarize my open loops grouped by person — who I owe, who I'm waiting on, and how long each
   > has been open. Read the current canonical loops. Keep it under 10 lines."
   Keep it one short paragraph. No dates ("this Friday") — it recurs. No sending instructions. The
   result is read-only and model-produced.
4. **Propose before creating.** Slug the subject: lowercase, hyphens, ≤24 chars
   (`user-open-loops-friday`). Call `owner_routine_propose` with that name, the schedule and the
   prompt. The runtime delivers the exact proposal, including the schedule, the instructions, and
   this conversation as the destination. Finish the turn there. If the owner tweaks it, propose
   the changed schedule or prompt.
5. On a separate clear yes, call `owner_routine_create` with no arguments. The runtime binds
   the accepted proposal and replies after recording the change.

### LIST — "what routines do I have"

`owner_routines`, rendered plainly — schedule in words, then what it does. Never print schedule
objects or routine ids:

> - **Friday 4pm** — open-loops summary by person
> - **Weekdays 8am** — today's calendar agenda

If there are none: "No personal routines yet — tell me one in a sentence and I'll set it up
(*every Friday at 4, summarize my open loops by person*)." If asked about the briefs themselves, say
in one line that the morning/evening briefs, the weekly pulse and the watchers are Sotto's own
schedule. Point to Activity to view it or the setup page to change it.

### ADJUST — "move my Friday routine to 5"

`owner_routines`, match the one routine, then `owner_routine_adjust_propose` with its exact
`routine_id` and the new schedule. The runtime shows the previous and proposed
schedules. Apply the adjustment only after a separate clear yes with
`owner_routine_adjust_confirm`. To replace the routine’s instructions, explain that cancellation
and creation are separate steps. Get an explicit cancellation request and use the normal creation
review for the new routine.

### REMOVE — "stop the Friday summary"

1. `owner_routines` and match the user's plain reference (day, time, subject). **One match** →
   proceed. **Several** → ask which, showing them the way LIST does. **None** → say so; don't guess.
2. `owner_routine_cancel` with that exact routine ID. The owner's explicit request is the
   authorization; no extra confirmation is needed.
3. The runtime confirms the cancellation from its receipt.

## Notes

- **Timezone changes don't move existing routines (v1).** The zone is captured when a routine is
  proposed — your personal routines keep firing on the OLD clock until you recreate them. If the
  user changes zones, offer to recreate their routines; don't pretend they followed.
- **Where the fence is enforced in code:** the receiver accepts only `user-` names, counts at most
  10 active routines per owner, refuses duplicates, and never lists or cancels a system job — so a
  redeploy never eats a routine and a routine never shadows the schedule.
- **Single user.** These are the account owner's routines; there is no per-person scoping and no
  sharing. Everything a routine can see, the owner can already see.
- Use plain language for lists and clarifications. Let the runtime deliver each proposal and
  confirmation.
- Routine *suggestions* (Sotto proposing a routine from an observed rhythm) are not this skill's job
  — the weekly pulse may offer one, and it lands here as a normal CREATE with the same proposal step.
