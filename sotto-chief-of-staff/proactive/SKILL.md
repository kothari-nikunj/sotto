---
name: sotto-proactive
description: 'Use ONLY when the proactive cron fires (every ~15 min), when the Bridge fires a wake trigger (the moment you open your laptop), or when the user says "check for anything urgent" — surface time-sensitive nudges (a meeting about to start, a commitment due today, something you are waiting on that has gone quiet, a birthday coming up, an ask nobody answered twice) with a draft ready. NOT a brief; it stays silent unless something genuinely needs the user now. Principle: auto-draft, never auto-send.'
metadata:
  hermes:
    tags: [proactive, chief-of-staff, sotto]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace]
    requires_tools: [execute_code]
required_environment_variables:
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: nudge dedup state
---

# Sotto — Proactive nudges

A lightweight watcher. It runs on the polled cron (~15 min, the fallback) AND is fired event-driven the
moment the Mac wakes from sleep (the Bridge POSTs a `proactive_wake` trigger → the receiver runs this
skill) — so "you have a meeting in 20 min" lands as you open the laptop, not up to 15 min later. Either
way the procedure is identical: it is **silent by default** — it speaks ONLY when something is genuinely
time-sensitive, and then with a **draft ready, never auto-sent**. Most runs send nothing. Quiet hours and
once-per-day dedup apply the same to both triggers, so the wake path never double-nudges what the
cron already sent.

> **CRITICAL — the decision is deterministic; do NOT improvise nudges.** Run `proactive_scan.py` and act
> ONLY on the nudges it returns. It already enforces quiet hours, the meeting lead window, and
> once-per-day dedup. If it returns `{"nudges": []}` — **say nothing, end the turn.** Do not compose a
> brief, do not "check in", do not announce that there's nothing. Silence is the correct, common output.

## Procedure

> Scripts live under `$HOME/.hermes/skills/sotto/`. Use absolute paths.

1. **Gather (deterministic, fast):**
   - Calendar (next few hours) — `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --skip-gmail` → `/tmp/sotto_cal.json` (host-agnostic fallback as in the brief if the CLI isn't this host's Google path).
   - Continuity open-loops — **nothing to do**: `proactive_scan.py` reads the ledger itself through `loops_query` (the only sanctioned read view) and keeps the deadline-bearing loops. Don't run `loops_query.py` here, and never write a `/tmp/sotto_cont.json` — the hand-reshape step is gone (it also collided with the brief's differently-shaped file of the same name).
   - Local contacts (for birthdays) — `read_local` → `/tmp/sotto_local.json` (or the cached snapshot). Optional.
2. **Decide — ONE command:**
   ```bash
   python3 "$HOME/.hermes/skills/sotto/proactive/scripts/proactive_scan.py" \
     --calendar /tmp/sotto_cal.json --local /tmp/sotto_local.json
   ```
   It prints `{"nudges":[…], "held":[…], "quiet":bool}`. **If `nudges` is empty (or `quiet` is true) → STOP, send nothing.**
   It has already recorded what it returns, so it won't repeat a nudge later today.
   **`held` is not yours to deliver** — the funnel did not hand those back: it queued them for the midday digest / next brief (the day's shared interrupt budget, the room you're in, the hour) or dropped them (a muted person). Say nothing about them, and never mention the budget.
3. **For each nudge, draft (never send) and deliver ONE concise message:**
   - `meeting_prep` → optionally run `sotto-meeting-prep` for that meeting (or a 2-line who/what), and offer prep **naming the person**: "You're meeting Spencer Kim in ~40 min — want me to pull full prep on him?" (the names are in the nudge's `detail`; use the first external attendee, or the meeting title when there are several). Naming them is what makes a bare "yes" answerable: a yes runs `sotto-meeting-prep` focused on that person — one deep dive on that meeting, no list of the user's other meetings.
   - `commitment` → draft the reply/message for that open loop (use `sotto-draft-reply` style) and present it, then **ask** — never act unasked.
     - **Email → ask, don't paste a link.** Show the draft text and one plain question: *"Want this in your Gmail drafts?"* On yes, `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/google_action.py" gmail-draft --to <identifier> --body "<draft>" --thread-id <the nudge's `thread_id`>` and confirm in one line ("Drafted in Gmail — it's in your drafts, ready to send."). The nudge carries `thread_id` when the loop came from an email thread; pass it so the reply lands IN that thread. On `{status:"error", fallback:"deep_link"}` fall back to the `mailto:` link and say Gmail isn't connected.
     - **Every other channel** (iMessage/SMS/WhatsApp/call) → the one-tap link as before; ask before sending.
   - `chase` → **you're waiting on THEM — a nag is not a reply.** Draft a *chase*, not a follow-up:
     one short, warm line that asks about the thing, with zero pressure. No deadline invention, no
     guilt, no mention that Sotto has been tracking it, and never the banned openers
     ("following up", "waiting for your response" — a chase is exactly where a model reaches for
     both). Present with the one-tap link; ask before sending.
     - ✅ "hey — any word on the deck?"
     - ✅ "morning! is the contract still coming this week? no rush if not."
     - ❌ "Following up on my previous message — I'm still waiting for your response regarding the
       contract. Please advise at your earliest convenience." (nag, banned phrases, invented urgency)
     If the nudge's `detail` says *chased once already*, keep it even lighter: this is the last one
     Sotto sends, and after it the item comes back as a `handoff` (below).
     The offer follows the same rule as `commitment` above: **email asks** ("want this in your
     Gmail drafts?" → `gmail-draft`, threaded on the nudge's `thread_id`), every other channel gets
     the one-tap link.
   - `handoff` → **you've nudged twice and heard nothing; ask, don't tidy.** Deliver the nudge's
     `detail` as the whole message — it is already the sentence: *"I've nudged Maya twice about the
     contract — nudge them again, or let it go?"* Person, thing, binary choice. Don't dress it up,
     don't add a draft unless they say "nudge them", and don't fold it into a list of other items.
     If they say let it go, run **`sotto-loops`**' drop for that item; if they say nudge again,
     draft a chase per the rules above.
   - `birthday` → draft a short, warm note and present it with the contact's tap-link.
     When the nudge carries **`lead_days`** (the birthday is a few days out, not today), it's a *gift*
     nudge, not a greeting: first run
     `python3 "$HOME/.hermes/skills/sotto/_shared/knowledge/knowledge_query.py" --person "<name>"`
     and use that person's own `interest`/`preference` facts to suggest ONE concrete idea
     ("Jordan's birthday is Thursday — he's been into film photography; a roll of Portra + a card?").
     No facts on file → say the date and offer to help pick something; never invent an interest.
   - `retune_offer` → DON'T draft anything. Deliver the one-liner as a light offer ("Your open-loops list
     is getting heavy — N items keep showing up. Want me to run a quick cleanup?"). If the user says yes,
     run **`sotto-loops`**' cleanup. If they ignore it, drop it — the cooldown means it won't ask again for days.
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
4. **If the push ENDED in a question, write it down — same turn, right after sending:**
   The user's "sure" arrives in the gateway's own session, which never saw your question; unless
   the question is on the volume, that session has nothing to resolve the "sure" against.
   ```bash
   python3 "$HOME/.hermes/skills/sotto/_shared/scripts/pending_offer.py" set \
     --kind meeting_prep --person "Shivani" \
     --question "You're meeting Shivani in ~44 min at Sightglass — want me to pull full prep on her?"
   ```
   Same for the other kinds (`--kind commitment|chase|handoff|retune_offer`), with the question
   exactly as you sent it. It overwrites — one pending question at a time, newest wins.
   **If the tick delivered nothing, or the push asked nothing, record nothing.**

## Notes
- The meeting lead window (default 45 min) and the once-per-day dedup are in `proactive_scan.py`;
  quiet hours (default 21:00–07:00) are the funnel's, applied to this lane like any other event.
  The lead window is `proactive_scan.PROACTIVE_LEAD_MIN`; quiet hours are `SOTTO_QUIET_START/END`.
  Don't reimplement them.
- **One rulebook, not two — structurally.** `proactive_scan.py` decides only WHAT is due and then
  hands the whole tick to the event funnel's own `triage()`, in process; what comes back is what you
  deliver. So the same gates apply because they are the same code: the **snooze** and **quiet
  hours** hold everything; a **muted** person is dropped; the **in-meeting hold** queues the rest
  while you're in a room with someone; the **daily interrupt budget** (`SOTTO_NUDGE_BUDGET`,
  default 4) is charged **once per delivered push** — this whole message is one interrupt, not one
  per kind — and beyond it the push demotes whole to the SAME digest queue. Every verdict, fired or
  held, writes the SAME `surfaced.jsonl` row, including the ones the clock suppressed. A `meeting_prep` nudge is also skipped when today's research cache
  already covers that meeting's attendees (a prep or brief run today prepped it); the **lead**
  `birthday` nudge waits 2h after a delivered brief, and the **day-of** one is dropped entirely once
  a brief has delivered today — that brief already carried the 🎂 line and the same tap link.
- The `retune_offer` nudge fires when ≥`RETUNE_OFFER_MIN` (6) loops are stale, at most once
  per `RETUNE_OFFER_COOLDOWN_DAYS` (7) — a periodic "want to tidy up?", never a daily nag.
  The `handoff` question shares that same cooldown but ignores the threshold: one unanswered ask is
  worth asking about even on a tidy day, and it is never folded into the generic offer.
- **The chase clock isn't yours.** `continuity_resolve.py` (the brief's Learn step) is the ONE writer of
  the chase fields: it marks at most one waiting-on per local day as chase-pending after
  `SOTTO_CHASE_AFTER_DAYS` (default 3) of silence, and stops after two chases. This skill only *delivers*
  what is pending today — at most **one chase nudge a day**, paying the same interrupt budget as
  everything else — and the count is finalized by the scan itself, on delivery, so a chase you never
  sent was never spent. Don't stamp anything, don't chase twice in a day, don't improvise a chase
  for a loop the scan didn't return.
- The `birthday` nudge fires twice per person per year at most: once `SOTTO_BIRTHDAY_LEAD_DAYS`
  (default 3) ahead — the one that can still become a gift — and once on the day. The dedup key carries
  the year, so neither can repeat.
- This skill never writes the knowledge graph or continuity ledger (that's the brief's job) — it's read-only
  except its own nudge-dedup state.
- If `proactive_scan.py` can't run (e.g. `execute_code` unavailable), do nothing — a missed nudge is fine;
  the morning/evening brief is the backstop.
