---
name: sotto-proactive
description: 'Use ONLY when the proactive cron fires (every ~15 min), when the Bridge fires a wake trigger (the moment you open your laptop), or when the user says "check for anything urgent" — surface time-sensitive nudges (a meeting about to start, a commitment due today, something you are waiting on that has gone quiet, a birthday coming up, an ask nobody answered twice) with a draft ready. NOT a brief; it stays silent unless something genuinely needs the user now. Principle: auto-draft, never auto-send.'
metadata:
  tags: [proactive, chief-of-staff, sotto]
  category: productivity
---

# Sotto — Proactive nudges

A lightweight watcher. It runs on the receiver's polled schedule (~15 min, the fallback) AND is fired
event-driven the moment the Mac wakes from sleep (the Bridge POSTs a `proactive_wake` trigger → the
receiver runs this procedure) — so "you have a meeting in 20 min" lands as you open the laptop, not up to 15 min later. Either
way the procedure is identical: it is **silent by default** — it speaks ONLY when something is genuinely
time-sensitive, and then with a **draft ready, never auto-sent**. Most runs send nothing. Quiet hours and
once-per-day dedup apply the same to both triggers, so the wake path never double-nudges what the
cron already sent.

> **CRITICAL — the decision is deterministic; do NOT improvise nudges.** The receiver's scanner
> decides, and only the nudges it returns are composed. It already enforces quiet hours, the meeting lead window, and
> once-per-day dedup. If it returns `{"nudges": []}`, the output is the single token `NO_NUDGES`
> (the delivery seam turns that into silence). Do not compose a brief, do not "check
> in", do not announce that there's nothing — no "all clear", no "scan complete". A no-nudge run is
> the correct, common output, and the user never hears about it.

Read `_shared/references/relevance.md` before composing the scanner's eligible candidates.
Use that shared bar to explain the actual action, decision, preparation need or development.
Do not add candidates or override the scanner's cadence/consent gates. If current evidence shows
all candidates are resolved or irrelevant, return `NO_NUDGES` instead of padding the message.

## Procedure

> **How this runs on Sotto:** this procedure is receiver-owned. Scheduled and wake-triggered runs
> gather the calendar for the next few hours, the deadline-bearing open loops and contacts (for
> birthdays), run the scanner, and call the direct notification composer for admitted candidates.
> They start no agent and repeat no discovery; empty results make no model call. The composer
> follows the approval, pending-offer and acknowledged-delivery rules below. In chat, a current
> owner request for an urgent check is `run_sotto_script proactive` (no arguments): it queues the
> original scanner/funnel/composer without overriding quiet, budget or consent. Successful admission
> is silent — do not say checking, scan complete or all clear; only an eligible notification
> arrives. A `needs_context` refusal queues nothing: answer the current message normally.
> Unavailable or rejected admission is not an empty scan.

1. **Gather (deterministic, fast):** the calendar (next few hours); continuity open loops — the
   scanner reads the ledger itself through the sanctioned read view and keeps the deadline-bearing
   loops; local contacts for birthdays (optional).
2. **Decide — the scanner** prints `{"nudges":[…], "held":[…], "quiet":bool}`. **If `nudges` is
   empty (or `quiet` is true) → nothing is sent.** It has already recorded what it returns, so it
   won't repeat a nudge later today.
   **`held` is never delivered** — the funnel did not hand those back: it queued them for the midday digest / next brief (the day's shared interrupt budget, the room you're in, the hour) or dropped them (a muted person). Say nothing about them, and never mention the budget.
3. **For each nudge, follow `_shared/references/approval-tiers.md` and deliver ONE concise message.**
   An open decision stays open: ask which way the user wants to go, or show labeled alternatives
   under `sotto-draft-reply`. Never convert a request for a decision into an unsolicited pass.
   Draft when the direction is established; never send:
   - `intention` → deliver the stored action and context plainly. It was explicitly scheduled by
     the user, so do not reinterpret its condition, add another reminder, or turn it into a brief.
     A condition tied to an open loop has already been checked; if that loop closed, the scanner
     canceled the intention before it reached you.
   - `meeting_prep` → **make the reminder useful without requesting full prep.** Name the person, their known role and the time; never invent a title or introduction. Add one or two short sentences selecting useful relationship memory: where you left off, a relevant development or shared interest, why this meeting is happening, or a supported commitment. An introduction is optional, not a required opening. Use supplied dates when helpful; an old discussion is not automatically today's agenda, and an organizer is not necessarily an introducer. Treat stored open items as prior bookkeeping and check later evidence before calling something still owed; never append the scanner's `open_loop` unchecked. The direct composer reuses the existing bounded notification writer, stored person memory and at most five permitted cached attendee-mail excerpts; the reminder performs no new Gmail fetch or web research. Context stays within 60 words / 500 characters, plus the fixed identity/time and offer. A held/failed writer or insufficient grounded context falls back to the basic reminder without an unreviewed task. Finish with one named question: "Want the full prep on Spencer Kim?" A bare yes runs `sotto-meeting-prep` focused on that person, with no list of other meetings. Full prep uses the four-photo iMessage gallery when eligible; the reminder stays short text.
     The scanner matches a known attendee address exactly when selecting prior commitments; a shared
     display name cannot attach another person's task. Without an address, only one unambiguous
     name match may supply that context.
   - `commitment` → draft the reply/message for that open loop (use `sotto-draft-reply` style) and present it, then **ask** — never act unasked.
     - **Email → ask, don't paste a link.** Show the draft text and one plain question: *"Want this in your Gmail drafts?"* The owner's yes in chat continues that exact offer through `gmail_draft_offer` (the receiver selects the offered reply, threaded on the nudge's `thread_id` when the loop came from an email thread), which presents the full review; only a distinct later yes saves it with `gmail_draft_save`, confirmed in one line ("Drafted in Gmail — it's in your drafts, ready to send."). Without a Gmail draft grant, the text is the offer and the user sends it themselves.
     - **Every other channel** (iMessage/SMS/WhatsApp/call) → the one-tap link the composer builds; ask before sending.
   - `chase` → **you're waiting on THEM — a nag is not a reply.** Draft a *chase*, not a follow-up:
     one short, warm line that asks about the thing, with zero pressure. No deadline invention, no
     guilt, no mention that Sotto has been tracking it, and never the banned openers
     ("following up", "waiting for your response" — a chase is exactly where a model reaches for
     both). Present with the one-tap link; ask before sending.
     - ✅ "hey — any word on the deck?"
     - ✅ "morning! is the contract still coming this week? no rush if not."
     - ❌ "Following up on my previous message — I'm still waiting for your response regarding the
       contract. Please advise at your earliest convenience." (nag, banned phrases, invented urgency)
     If the nudge's `detail` says *reminded you once already*, keep it even lighter: this is the last
     automatic reminder to the owner before a `handoff` (below). A reminder or an offered draft
     never proves that the other person was contacted; only actual sent-message evidence can do that.
     The offer follows the same rule as `commitment` above: **email asks** ("want this in your
     Gmail drafts?" → `gmail_draft_offer`, threaded on the nudge's `thread_id`), every other channel
     gets the one-tap link.
   - `handoff` → **the owner has received two reminders; ask, don't tidy.** Deliver the nudge's
     `detail` as the whole message — it is already the sentence: *"I've reminded you twice about Maya
     and the contract — want a follow-up draft, or let it go?"* Person, thing, binary choice.
     Don't dress it up, don't add a draft unless they request one, and don't fold it into a list of other items.
     A short reply explicitly attached to that question — "let it go", "drop it", "done", "no",
     "skip it" — is resolved by the runtime against that exact recorded offer before any model turn.
     Never directly dismiss the loop or hand-clear the offer for these replies; if no offer matches,
     answer in the visible conversation. If they request the draft or say nudge again, draft a chase
     per the rules above; sending it still needs separate approval.
   - `birthday` → draft a short, warm note and present it with the contact's tap-link.
     When the nudge carries **`lead_days`** (the birthday is a few days out, not today), it's a *gift*
     nudge. It must carry `importance.tier` of `vip` or `vvip`; otherwise suppress the gift offer. Do not infer importance from a birthday, a meeting invite or unanswered mail. For eligible people, the
     person's graph knowledge (`run_sotto_script knowledge-query --person "<name>"` in chat) supplies
     that person's own `interest`/`preference` facts to suggest ONE concrete idea
     ("Jordan's birthday is Thursday — he's been into film photography; a roll of Portra + a card?").
     No facts on file → say the date and offer to help pick something; never invent an interest.
   - Keep the whole push SHORT — a nudge, not a brief. Lead with the single most time-sensitive item.
     ONE message for the whole tick — that is what the day's budget was charged for.
   - **Plain words only.** Never say "proactive", "chase", "retune", "ledger", "loop anchor" or any
     other word from Sotto's own machinery. Say what happened to a person: *"you're still owed the
     contract"*, *"open items"*, *"want me to tidy these up?"*
   - **It must read like a text from a person, not a notification.** A nudge is one or two plain
     sentences and nothing else. Concretely, none of these:
     - **No labelled field lines.** Not `📍 Backhaus (261 California Dr, Burlingame)` on its own
       row — if the place matters, it belongs in the sentence: *"…at Backhaus in ~13 min"*.
     - **No URLs.** Never paste a calendar link, and never a bare `https://…` of any kind. The
       event is already on their calendar; a 200-character Google URL in a text message is noise
       they have to scroll past. The ONE exception is an action link they asked for — a draft's
       `mailto:`/`imessage:` tap-link, which exists to be tapped.
     - **No headers, footers, subject lines or sign-offs.** No "Update:", no "Reminder:", no
       bullet list for a single item.
     Good: *"You're meeting Vignesh Ravikumar and Shomik (Sierra Ventures) at Backhaus in ~13 min
     — want me to pull full prep on them?"* That is the whole message.
   - **Honor the approval tiers (`_shared/references/approval-tiers.md`): present drafts, never auto-send.** Deliver as **Sotto**.
4. **If the push ENDS in a question, the composer stages it before delivery.**
   The user's "sure" arrives in a different session, which never saw your question; unless the
   question is on the volume, that session has nothing to resolve the "sure" against. So the
   receiver records each delivered question (kind `meeting_prep`, `commitment`, `chase`, `handoff`
   or `intention`, with the person and the question exactly as delivered) and activates it only
   after the provider accepts the message. Two fresh delivered questions remain separate; a bare
   "yes" that could mean either is ambiguous — ask which question they mean; never resolve it by
   choosing the newest offer. A `chase`, `commitment` or `handoff` offer carries the nudge's
   `anchor_key`, so a short "done", "handled" or "let it go" resolves or drops that exact loop
   without re-matching it by name. **If the tick delivered nothing, or the push asked nothing,
   nothing is recorded.** An offer whose yes would send something or write the calendar is bound to
   the hash of that exact action; one whose yes only runs a read — prep, a cleanup, a standing
   rule — has nothing to hash.

## Notes
- The meeting lead window (default 45 min) and the once-per-day dedup are in the scanner;
  quiet hours (default 21:00–07:00) are the funnel's, applied to this lane like any other event.
  The lead window is `proactive_scan.PROACTIVE_LEAD_MIN`; quiet hours are `SOTTO_QUIET_START/END`.
  Don't reimplement them.
- A receiver run reserves its fired keys and caches its decisions under its stable delivery run ID.
  Held keys remain eligible, and scheduled intentions stay scheduled. The delivery receipt marks
  keys seen, finishes intentions, and applies chase/hand-off/offer effects; a failed send does none
  of those. Retrying the run uses the same decisions without another interrupt charge.
- **One rulebook, not two — structurally.** The scanner decides only WHAT is due and then
  hands the whole tick to the event funnel's own `triage()`, in process; what comes back is what you
  deliver. So the same gates apply because they are the same code: the **snooze** and **quiet
  hours** hold everything; a **muted** person is dropped; the **in-meeting hold** queues the rest
  while you're in a room with someone; the **daily interrupt budget** (`SOTTO_NUDGE_BUDGET`,
  default 4) is charged **once per delivered push** — this whole message is one interrupt, not one
  per kind. “Fewer nudges” halves the effective cap, “no nudges” sets zero for every unsolicited
  path, and “more nudges” restores the configured ceiling. Beyond a nonzero cap the push demotes to
  the SAME digest queue. Every verdict, fired or
  held, writes the SAME `surfaced.jsonl` row, including the ones the clock suppressed. A `meeting_prep` nudge is skipped only when a provider-accepted prep offer or full prep records the same calendar event ID and start; research alone never suppresses it, and a reschedule is a new occurrence. The **lead**
  `birthday` nudge waits 2h after a delivered brief, and the **day-of** one is dropped entirely once
  a brief has delivered today — that brief already carried the 🎂 line and the same tap link.
- The `handoff` question is asked the first tick it comes due (outside the 2h post-brief window),
  once; its delivery stamps the row. One unanswered ask is worth asking about directly.
- **The chase clock isn't yours.** The brief's Learn step is the ONE writer of
  the chase fields: it marks at most one waiting-on per local day as chase-pending after
  `SOTTO_CHASE_AFTER_DAYS` (default 3) of silence, and stops after two chases. This skill only *delivers*
  what is pending today — at most **one chase nudge a day**, paying the same interrupt budget as
  everything else — and the count is finalized by the scan itself, on delivery, so a chase you never
  sent was never spent. Don't stamp anything, don't chase twice in a day, don't improvise a chase
  for a loop the scan didn't return.
- The `birthday` nudge fires twice per person per year at most: once `SOTTO_BIRTHDAY_LEAD_DAYS`
  (default 3) ahead for VIP/VVIP relationships only, and once on the day for other saved contacts too. Gift eligibility comes from sustained reciprocal activity or your explicit VIP choice; missing evidence suppresses the gift offer. The dedup key carries
  the year, so neither can repeat.
- This skill never writes the knowledge graph or continuity ledger (that's the brief's job) — it's read-only
  except its own nudge-dedup state.
- If the scanner can't run, nothing is sent — a missed nudge is fine; the morning/evening brief is
  the backstop.
