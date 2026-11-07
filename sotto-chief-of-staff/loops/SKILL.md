---
name: sotto-loops
description: 'Use when the user asks what is outstanding — "what am I waiting on" / "what''s open" / "my open loops" / "what do I owe" / "my action ledger" / "loose ends" — or corrects that list: "this is done" / "nothing to do" / "they will get back to me" / "did Granola capture these" / "clear my stale action items" / "clean up my open loops" / "this list is stale" / "I keep seeing the same items" / "tune up Sotto" / "too noisy". Reads and applies explicit corrections to the continuity ledger, split into what the user owes vs what they are waiting on others for. Never sends anything.'
metadata:
  tags: [chief-of-staff, sotto, continuity, preferences]
  category: productivity
---

# Sotto — Open loops (and clearing the stale ones)

Two jobs over one ledger: **show what's outstanding**, and — when the user asks — **clear what's
gone stale**. Both are read-first. The runtime’s always-on task readback contract also applies before this skill is loaded: conversation identifies a requested subset, but current canonical rows establish what remains open. Short prose guidance does not replace the saved task list. This skill never sends a message, email, or calendar change.

> **How this runs on Sotto:** `run_sotto_script X …` means one `run_sotto_script` call with id `X`
> and the rest as its argv array — no shell, no file paths. Other backticked names are native tools.

## A · What's open

1. **For a list the owner will act on, use `owner_task_list`.** It reads current permitted open
   tasks and delivers a saved numbered list, including recent tasks. Use it for “what's open?”
   and “what do I owe?”. For a requested numbered inventory of previously discussed tasks, inspect canonical rows, then pass the remaining matching anchor_keys, even for a single remaining task. A factual question about whether a named task remains open, or a mixed question about completion evidence and another task’s status, is targeted inspection: answer from the current permission-filtered obligation history or loops-query rows without creating a selectable list. Reuse exact task identities and source evidence already returned; a list preview ends the turn and cannot supply the historical explanation. Deliver its exact `review_text`; do not renumber or paraphrase the list.
   The next owner reply can mark numbers done through `owner_task_cleanup` with `action: resolve`
   and each exact number token as `correction` (for example “1, 4 and 22 are done”). The runtime
   supplies the saved list and target proofs; never reinterpret an old number using current order.
   If the saved list is unavailable, offer a fresh list instead of asking the owner to retype tasks.
   The accepted open list asks which tasks are done. A short numeric answer such as `3-17` uses an explicit selection with one resolve clause covering the whole current owner message and exact numeric references. `All? 1–20?` can use that numeric answer only if it names every displayed task. Other words, negation, unconsumed text and stale-loop cleanup questions do not supply this implicit completion intent.
   An explicit completion of every displayed task, such as “Close all of them”, uses `owner_task_cleanup` with `selection: {mode: "all", correction: <whole current owner text>, reference: <exact collective phrase>}`. The runtime derives only the frozen displayed tasks, including an explicitly requested subset. A missing accepted snapshot returns a fresh canonical list without changing anything. If only some tasks are done and the owner has not identified which ones, reissue the same bounded selection with `owner_task_list` and request the numbers, without changing any task. A failed typed selection can retain that snapshot for one repair only through the runtime’s accepted clarification; plain assistant prose cannot do so.
   When the accepted list displays exactly one task, an explicit next reply such as “That one is done too” uses `owner_task_cleanup` with `selection: {mode: "singleton", correction: <whole current owner text>, reference: <exact singleton phrase>}`. The runtime derives the sole target from that accepted list; ordinary assistant prose never supplies selection authority. No second confirmation is needed for an explicit completion. Never infer completion from “yes”.

   **For targeted inspection or matching a named correction, use `run_sotto_script loops-query`.**
   It reads the ledger the brief maintains (currently permitted, attributable canonical loops) and
   returns `{you_owe:[…], waiting_on_them:[…], counts}`. Counts do not cover withheld, unknown or
   revoked sources. Each item: `{anchor_key, loop_version, loop_identity, name, what, channel, identifier,
   age_days, deadline, overdue, chased_count, last_chased_at, chased_out}`, earliest valid deadline
   first, then undated work oldest first; equal deadlines also use age. Invalid or relative dates
   stay undated. Don't hand-read the ledger. (`owner_open_loops` is the separate owner-authored task view.)
   When the user explicitly asks to see parked loops or restore one, use
   `run_sotto_script loops-query --parked`. The opt-in response adds `parked:[…]` and `counts.parked`; parked items are historical
   obligations and must not be presented or counted as open. If more than one parked item could
   match a request such as "keep Maya", show the matches and resolve the ambiguity before applying
   `keep` with that exact row's identity.
2. **For named completions, use `owner_task_resolve_many`.** Include every requested completion.
   Copy exact current row proofs for unique matches. For a missing/ambiguous name, include its
   exact owner phrase as name/correction and omit all target proofs. Close clear independent
   selections and ask only about unresolved ones. “Handled” means completed, not dismissed.
   Preserve the receiver's per-item outcomes; a queued or uncertain effect is not a completed edit.

3. **For targeted inspection answers, deliver tight and skimmable prose.** Numbered selectable
   lists must come from `owner_task_list`; do not invent numbering for a model-written summary.
   Keep birthday greetings visible, but do not group them as urgent or time-sensitive merely because a birthday is mentioned. An optional greeting or a legacy birthday request stays visible without inventing urgency. Only an explicit task deadline supports a deadline label. `age_days` is how long a loop has been captured, not its deadline or proof of when the source message was sent. Stored `what`/`ask` wording such as "today", "tonight" or "tomorrow" is historical evidence: do not repeat it as a current deadline or resolve it from capture age. Use the explicit absolute `deadline` when available; otherwise omit the relative time or say the timing needs confirmation. Do not infer completion or hide an open task because its wording is old.
   - Lead with the count ("4 you owe, 2 you're waiting on").
   - **You owe** — name + the one-line `what`, flag `overdue` or age ("3 days"). For the top items
     offer a draft through `sotto-draft-reply`: "say *draft Dhruv*."
   - **Waiting on them** — name, what you're awaiting, how long, **and what Sotto already did about
     it**. Each item carries `chased_count` / `last_chased_at` / `chased_out`, so say it plainly
     ("reminded you once, Tuesday" · "I've reminded you twice — want a follow-up draft, or let it go?").
     These counts never establish that anyone was contacted. `chased_out` means automatic reminders
     to the owner have stopped and the call is theirs. Offer a draft for stale requests as needed.
   - Both lists empty: "Nothing open in the sources I can currently read." Do not claim all-source completeness.
   - Deliver as **Sotto**. Any draft honors the approval tiers
     (`_shared/references/approval-tiers.md`) — never auto-send.

## B · Clearing the stale ones

Read → confirm → apply. Propose the cleanup, let the user pick, then write it. **Don't auto-clear**
— these are their commitments. The one exception is an obvious snooze on their explicit say-so;
when you can't tell which loop they mean, ask.

1. **Scan (read-only)** — `run_sotto_script retune-scan` (no arguments).
   Returns `stale_loops[]` (each with `anchor_key`, `name`, `what`, `direction`, `age_days`,
   `times_surfaced`, `overdue`, `deadline`, `chased_count`, `chased_out`, `suggestion`),
   `mute_suggestions[]`,
   and `current` (timezone + the mutes and tone in effect). All empty → say so in one line ("Your
   loops are clean — nothing stale to clear") and stop.
   It shows currently permitted canonical loops and explicit settings; withheld or unknown sources
   are not covered.
   An empty result means no stale items in that view, not a complete all-source all-clear.
   `owner_stale_loops` is the narrower owner-authored-only fallback.
2. **Present, grouped and short**, using the scan's suggestion verb:
   - **You owe** → "do it or dismiss", oldest and most-overdue first.
   - **Waiting on them** → "nudge or drop" — except the ones the owner has been reminded about twice
     (`chased_out: true`, verb "reminded you twice — draft a follow-up or drop"). **Lead with those**
     and say it out loud: automatic reminders have stopped, and this is the decision it hands back.
     Those reminders never prove the other person was contacted. It's the
     one thing here Sotto owes the user rather than merely suggests, so surface it on its own even
     when the rest of the list is short.
   Show `name — what` and why it's flagged ("surfaced 5×", "12 days old", "overdue"). Then any
   `mute_suggestions` is currently always empty: historical dismissal signals include inferred
   draft non-use. Never turn those signals into a suggestion to mute someone. Honor an explicit
   mute request through `sotto-feedback` as usual.
3. **Apply only what they chose.**
   Offer multi-item cleanup through `run_sotto_script retune-apply list`. This uses
   the original permitted stale scan and returns a numbered `review_text`. Deliver that exact
   text unchanged; it binds each displayed number to its actual loop. Nothing has changed yet.
   The list shows up to 20 items and discloses any remaining items. After an intervening owner
   turn, request a fresh list rather than guessing what an old number means.

   For the owner's next explicit choices, use `run_sotto_script retune-apply cleanup '<JSON>'`, where JSON is
   `{"items":[{"index":1,"action":"dismiss","correction":"dismiss item 1"},
   {"index":2,"action":"snooze","days":7,"correction":"snooze item 2 for 7 days"}]}`.
   Each correction must be an exact span of that current owner message and explicitly state the
   selected number and action; snooze also states the number of days. `keep` has no days field.
   Use the owner's actual choices, not this example's numbers or words. Clarify unsupported or
   ambiguous wording; bare "yes" and "clean everything" select nothing. The runtime supplies the
   accepted list and refreshes protected targets; never supply your own list, proof or anchors.
   These explicit choices authorize the private edits without another confirmation.

   Report the returned per-item outcomes and protected readback. A refused item did not change;
   an uncertain item may have changed and must not be retried. Later skipped items were not
   attempted. Never call a partial result all done. Request a new list for remaining choices.
   Single-item choices first read `run_sotto_script loops-query` (with `--parked` when needed) and
   copy that exact row's `anchor_key`, `loop_version` and `loop_identity`; clarify ambiguous targets.
   The scan alone grants no edit permission.
   - **Dismiss / snooze / keep** — one item, the owner's exact words as `--correction`:
     ```
     run_sotto_script knowledge-edit --op loop --to dismissed --anchor <A> --loop-version <V> --loop-identity <I> --correction "<owner's words>"
     run_sotto_script retune-apply snooze --anchor <A> --loop-version <V> --loop-identity <I> --correction "<owner's words>" --days 7
     run_sotto_script retune-apply keep --anchor <A> --loop-version <V> --loop-identity <I> --correction "<owner's words>"
     ```
     `dismiss` = done with it, won't resurface. `snooze N` = hidden N days, then back. `keep` = "I
     still care": on something they **owe**, it records a new `reopened_at` touch and un-parks a
     parked loop while preserving the original `created_at` evidence cutoff. `snooze N` also
     preserves `created_at`; its park clock restarts when it returns. An untouched loop can park
     only after 14 days and an accepted prior-day warning: it remains kept with its history, out of
     the brief and open list, never deleted. On something they're **owed**, `keep` resets the nudge count so
     Sotto chases again from scratch (a parked waiting-on loop stays parked). `keep` does not clear an
     existing snooze, and a snooze uses owner-local calendar days without resolving or dismissing the
     obligation. Either way, tell them which one they got.
   - **"too noisy right now" / "quieter today"** — a *cadence* change, not a mute: route to
     `sotto-feedback` §C (`run_sotto_script preferences snooze-nudges …`, and
     `unsnooze-nudges` for "back to normal"). Snoozes expire on their own; mutes don't.
   - **Mute a sender / person / section, or set tone** — route through `sotto-feedback`
     (`run_sotto_script preferences mute-person "<name>"`, `mute-sender <addr|@domain>`,
     `mute-section <id>`, `tone "<note>"`). These take effect on the next brief.
   - **"Nudge them"** on a waiting loop is a *send* — hand off to `sotto-draft-reply`. This skill
     never sends.
   - **Brief timing** ("move my morning brief to 7am") → `sotto-setup` owns the system schedule.
4. **Confirm in one line.** "Cleared 4 stale loops, snoozed 2 for a week, and I'll stop surfacing
   Bob. Your list is lighter now."

## C · Applying explicit corrections

When the user directly corrects an item, that statement **is** the authorization to edit the
private ledger. Do not ask for a second confirmation. Query first (§A), match the current rows,
and apply only what the user said:

- **Two or more items done / already handled** → use the native `owner_task_resolve_many` once
  for every selected item, rather than repeating single-item commands. Set `selection_mode` to
  `"owner_tasks"` for owner-authored manual tasks, reading `owner_open_loops` first. A short
  phrase such as "taxes" must uniquely match the saved title (for example, "Finish quarterly
  taxes") at word boundaries. Use `"named_people"` for uniquely matched
  person or group labels; a first name is usable only when unique. Ignore label punctuation and a descriptive “text thread”, “chat thread”, or “thread” suffix when identifying a unique group, but keep the exact owner phrase as the selection. For each item, copy `anchor_key`,
  `loop_version` and `loop_identity` from its current row, and put the exact phrase from this
  owner message in both `name` and `correction`. Do not expand "taxes" into the full saved title.
  For "taxes and expenses are done. what's left?", select taxes and expenses together, then
  answer the remaining-items question from the protected readback. Leave explicitly still-open
  items out. If a selection is missing or ambiguous, include only its exact name and correction, without target proofs. Close the independent clear selections and ask only about the unresolved item.
  These explicit completions need no additional yes. Report each returned outcome; never retry
  an uncertain item or treat a partial result as all done.
- **Exactly one owner-authored manual task done** → read `owner_open_loops` first, then use
  native `owner_task_close` with its current `anchor` and `correction` copied as the exact matching task phrase from the owner
  message. This tool cannot consume only the first selection in a multiple-completion request.
- **Exactly one source-derived item done / already handled** → resolve that existing item,
  copying the current row's proofs and an exact span of the owner's correction:
  ```
  run_sotto_script knowledge-edit --op loop --to resolved --anchor <A> --loop-version <V> --loop-identity <I> --correction "<owner's words>"
  ```
- **Not a real obligation / nothing to do / extracted incorrectly** → dismiss it with the same
  command and `--to dismissed`.
- **The direction changed** (for example, "James will get back to me") → retarget it in one locked
  operation:
  ```
  run_sotto_script knowledge-edit --op loop-retarget --direction them --anchor <A> --loop-version <V> --loop-identity <I> --correction "<owner's words>"
  ```
  Use `--direction you` when the correction says the user owes it. Omit `--text`, `--contact` and
  `--identifier` when unchanged — the receiver preserves the full canonical values, while the
  displayed loop text may be truncated. Changed values must use the owner's exact correction words;
  never guess an identifier or borrow an old one for a changed person.
- **A clearly stated item is missing entirely** → add it with
  `run_sotto_script knowledge-edit --op loop-add --text "<owner's words>"` (or `owner_task_add`)
  and the correct `--direction`.

Every mutation must return its receipt. Once a task mutation settles, finish with its protected
receipt; the runtime owns any required current-state verification and acknowledgement. Do not call
more tools in that turn or present pre-mutation rows as current. A fresh selectable list uses
`owner_task_list` on a later owner turn. Report counts only when the runtime verified them.
Disclose any additional requested edits that were not attempted; a receipt proves only its own action.
If a write or verification fails, say
which change could not be recorded in plain language, without internal terms such as "ledger
validation error". A confirmed refusal means it did not change; an uncertain write means "I
couldn't confirm whether it was marked done", not that it remains open. Never describe intended
state as actual state. An uncertain write is held for inspection, never retried as a fresh edit.

## Notes
- **Never infer closure.** The brief's Learn step owns observational
  resolution; this skill applies only the user's explicit §B/§C edits through the canonical writer.
  Both paths persist the same terminal fields, so the view stays consistent with the brief.
- `waiting_on_them` = the things the OTHER side owes — the only kind Sotto chases. Everything else
  active, including a thread gone quiet that needs YOUR nudge, is `you_owe`. One predicate decides
  it (`ledger_io.WAITING_ON_TYPES`), shared with the brief and the stale scan, so all three agree
  about which way a debt points.
- **Age is not completion.** Ordinary obligations do not close because seven days passed, a
  deadline passed, or a recipient is unreachable. Completion needs the brief's reviewed source
  evidence or the owner's explicit correction. §B flags stale candidates for the owner's choice.
  An untouched you-owe item can park after 14 days only following its accepted prior-day warning;
  parking preserves it for `loops-query --parked`, rather than resolving or deleting it. A future
  deadline or explicit-resolution mode keeps it out of that automatic parking path.
- **Waiting on them stays open.** Two delivered nudges stop automatic chasing and hand the choice
  back to the owner ("nudge again or drop"). Missing contact details do not prove fulfillment or
  authorize dropping the item. Say what cannot be chased, and offer the explicit §B choices.
- **A nudge counts only when it was delivered.** A chase Sotto proposed but never sent (quiet hours,
  the day's interrupt budget spent) is not counted against the two, so the numbers the user hears
  are nudges they really received.
- `dismiss` is terminal and pruned after 30 days; `snooze` and `keep` keep the loop and just
  reschedule it.
- Inverse companion to `sotto-triage`, which works the live inbox and threads. This works the
  durable ledger across days. For a standing weekly tidy-up, `sotto-routines` can create the job.
