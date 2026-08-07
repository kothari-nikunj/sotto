---
name: sotto-event
description: 'Use ONLY when the event receiver hands you a triaged event bundle (a real-time iMessage/WhatsApp/call/email — or a meeting that just ended, class post_meeting — that survived the triage funnel; the prompt names a bundle path under $SOTTO_DATA/events/), or when the sotto-midday-digest cron fires (digest mode). Deliver ONE short nudge as Sotto with a ready-to-send draft — never auto-send. NOT for polled proactive checks (that is sotto-proactive) and never invoked by the user directly.'
metadata:
  hermes:
    tags: [proactive, events, chief-of-staff, sotto]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace]
    requires_tools: [execute_code]
required_environment_variables:
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: reading event bundles and the digest queue
---

# Sotto — Event nudge (Tier 2 of the triage funnel)

The agent end of event-driven proactivity: the Bridge/email poller pushes events within seconds, the
deterministic triage funnel (`triage_event.py`, Tier 0 + a Flash-Lite Tier 1) has ALREADY decided this
one deserves the user's attention — your job is only to say it well. **ONE short message, a draft
ready, never auto-sent.** Everything that didn't reach you was dropped or queued on purpose; do not go
looking for more events, and do not re-triage.

> Scripts live under `$HOME/.hermes/skills/sotto/`. Use absolute paths. All context reads here are
> READ-ONLY views — **NEVER run `continuity_resolve` or any ledger/graph writer from this skill**
> (the brief's Learn step owns writes; the digest queue is consumed by `digest_check.py`, not by you).

## EVENT mode — the prompt names a bundle path (`$SOTTO_DATA/events/bundle-<ts>.json`)

1. **Read the bundle** — `execute_code` → `cat` the given path. It carries the triage verdict:
   the event (source, sender handle/JID/email, text, `is_from_me`, `is_group_chat`, group
   participants), the Tier-1 class + why, and any thread snippet the funnel attached. Trust it —
   it is the ground truth for who/what. A bundle with `"promoted": true` came through the
   deferred-queue release valve: the event was held earlier (cooldown/quiet/catch-up, or because the
   user was in a meeting) and the hold has lifted — say so naturally ("From earlier — Ben asked 2h
   ago…", the `why` carries the age) instead of pretending it just arrived. A promotion whose class
   was `meeting_hold` can be genuinely old — a long meeting outlasts the ordinary promotion window,
   and an ask Sotto itself held is never too old to deliver — so lead with the age, briefly.
2. **Context (read-only views only):**
   - Open loops with this person — `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/loops_query.py"`
     → `{you_owe:[…], waiting_on_them:[…]}`; keep only entries matching the sender (a loop with them
     is often the "why now").
   - Who they are — `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/knowledge_query.py" --person "<name|id>"`
     → identity + facts + talking points. Unknown sender = fine, proceed with what the bundle has.
3. **Compose ONE nudge as Sotto** — who / what / why-now in **≤ 2 sentences** (the nudge format in
   `_shared/references/voice.md` — calm, specific, no warning icons). Lead with the ask
   ("Dhruv just asked if you can move tomorrow's 10am — you still owe him the deck too"), not with
   "you received a message". A missed call nudge is "X just called (no answer)" + the callback draft.
3a. **Class `scheduling_ask` — offer times, not just words** (the sender is asking to find time:
   "can we do coffee Thursday?", "got 30 min next week?"). The nudge must propose real slots:
   - Read the calendar **deterministically** — `execute_code` →
     `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --skip-gmail` →
     `/tmp/sotto_cal.json` (the same one-command read `sotto-schedule` step 1 uses, including its
     MCP fallback if the CLI is missing). If the calendar is genuinely unavailable, fall back to a
     plain reply draft — never guess times.
   - Compute **2–3 conflict-free slots** that fit the ask, per `sotto-schedule`'s rules: availability
     comes from the busy blocks, never propose a slot that overlaps an existing event, explicit
     dates + times + timezone, honor working hours and any window the sender named.
   - The nudge = the ask + the proposed times + the drafted reply offering them — e.g. "Sarah wants
     coffee Thursday — you're free 2 and 4:30. Want me to send: 'Thursday works — 2pm or 4:30?'".
     Still ONE short message (the ≤ 2-sentence nudge + the draft); plain chat text, no markdown
     headings/bold (the chatfmt rule — chat clients render them literally).
   - **Booking still needs the user's explicit approval** — draft-never-send. If they pick a slot,
     the `sotto-schedule` skill's approval rules govern creating the event; never create a calendar
     event from this skill unattended.
3b. **Class `post_meeting` — the meeting just wrapped, offer the follow-up.** The event is synthetic
   (`source: "meeting_end"`, carrying `summary`, `start`, `end`, `attendees:[{name,email}]`); the
   receiver's calendar watcher noticed it end minutes ago. There is no sender and no inbound text —
   the ask is *"want me to send the follow-up?"*, and the draft is the follow-up itself.
   - **Compose it with the battle-tested composer — don't write a follow-up yourself.** `execute_code`:
     ```bash
     python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_granola.py" --days 1 --transcripts-since-hours 3
     python3 "$HOME/.hermes/skills/sotto/followup/scripts/compose_followup.py" \
       --granola /tmp/sotto_granola.json --local /tmp/sotto_local.json --since-hours 3
     ```
     (the same two commands `sotto-followup` step 1 + 3 run, windowed to the meeting that just
     ended). Use ONLY the `drafts[]`/`commitments[]` whose meeting matches the bundle's `summary` /
     `attendees` — a second meeting inside the window is not this nudge's business, and its
     follow-up will get its own tap.
   - **No transcript (Granola not connected, or nothing recorded)** → do NOT invent what was
     discussed. Fall back to what you can ground: open loops with the attendee
     (`loops_query.py`) and their graph file (`knowledge_query.py --person`), and draft a short
     "good talking — here's what I owe you" note built ONLY from those. **If there is no transcript
     AND no open loop, say nothing and end the turn** — a follow-up with nothing in it is noise.
   - **The nudge**: the meeting, the offer, the draft — e.g. "Your 2:00 PM with Sarah Chen just
     wrapped. Want me to send the follow-up? — 'Great chat — I'll get you the revised deck by
     Thursday and loop in Dhruv on pricing.'" Still ONE short message, plain chat text (chatfmt:
     no markdown headings/bold — clients render them literally).
   - Tap-link the draft with `action_links.py` as in step 4 (usually `mailto:` — the attendee's
     calendar address is right there in the bundle; `channel` may legitimately be null for an
     attendee with no address, in which case present the body and ask which channel, never guess one).
   - **Draft-never-send**, and **write nothing**: do NOT run `apply_commitments.py` from here (the
     ledger is the brief's Learn step's — this skill's no-writer rule has no post-meeting exception).
3c. **Class `escalation` — lead with the cross-channel fact.** The funnel joined the SAME person
   across two channels inside the window, and the `why` says it in words: *"Sarah called AND emailed
   within 20 min"*. **That fact is the nudge's first clause** — it is the reason this one reached the
   user at all, and the one thing they cannot see for themselves. E.g. "Sarah called and emailed in
   the last 20 minutes — she wants the revised deck before her 3pm." Then the draft, as always.
   - **Still ONE message**, and only one: an escalation is not permission to send two, or to send a
     second one later about the same push. The funnel suppresses the repeats; you must too.
   - Never dramatize — no warning icons, no all-caps, no "URGENT" (`voice.md` holds here like
     everywhere). State the two channels and the ask; the facts carry the weight.
   - The bundle's event is the LATEST channel; the earlier one is named in the `why` and is not
     re-fetched — don't go looking for it. Draft on the channel that just arrived, unless a missed
     call makes a callback the obvious move.

4. **Draft the reply in the user's voice** (this is what makes the nudge actionable, not just noisy):
   - `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/style_apply.py" '{"recipient":…,"channel":…,"canonical_id":…}'`
     → verbatim sample messages + voice guardrails. **Match the quoted samples' voice, length, and
     punctuation exactly** — they are the ground truth (see `sotto-draft-reply` for the full rules).
   - Tap-link — `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/action_links.py" '{"channel":…,"identifier":…,"message":…,"subject":…}'`
     → `imessage:` / `sms:` / `wa.me` / `mailto:` deep link with the draft prefilled.
   - **Group chats get NO deep link, ever** (`is_group_chat: true` → no linkable identifier; never
     substitute a member's number). Draft the text for the user to send in the app themselves, and
     attribute quotes only to the named per-sender lines — `sotto-draft-reply` rules apply verbatim.
5. **Deliver** — nudge + draft + link in one message, per **`sotto-approval-tiers`**: present the
   draft, **NEVER auto-send** (no tier earns unattended send from an event). Deliver as **Sotto**,
   never "Hermes Agent".
6. **Ledger discipline** — if the event adds a genuinely new ask, or is an `is_from_me` signal that
   likely closes a loop, you may say so in the nudge ("I'll log this one") — but **DO NOT run the
   ledger writer**; the brief's Learn step / deterministic resolution owns it and will pick the
   signal up from the queue. This skill writes nothing.

## DIGEST mode — the `sotto-midday-digest` cron (or the prompt says "digest")

1. `execute_code` → `python3 "$HOME/.hermes/skills/sotto/event-triage/scripts/digest_check.py"`.
   It reads the ambient queue since the last digest/brief and decides deterministically.
2. **If it prints `{"deliver": false}` → output NOTHING and stop.** Silence is the correct, common
   outcome — a quiet day has no midday digest. Do not announce that there's nothing.
3. Else compose **ONE compact catch-up message** from its `items` — **hard cap 6 lines** (the digest
   format in `_shared/references/voice.md`), grouped by person, most actionable first, no drafts and
   no links needed here (the user can ask for any). Deliver as Sotto.
4. After delivering, stamp it so tomorrow's window starts here:
   `python3 "$HOME/.hermes/skills/sotto/event-triage/scripts/digest_check.py" --stamp`.

## Notes
- Cooldowns, quiet hours, the in-meeting hold, the daily interrupt budget, the user's nudge snooze,
  dedup, and drop/queue decisions all live in the funnel (`triage_event.py`) — don't reimplement or
  second-guess them here. If a bundle reached you, it already fit inside the day's budget and no
  snooze was active; never mention the budget in a nudge, and never offer to raise it (the cadence
  verbs live in `sotto-feedback`). Two caps, not one: interrupts spend the daily budget
  (`SOTTO_NUDGE_BUDGET`, 4/day) and post-meeting taps spend their own (`SOTTO_TAP_MAX_PER_DAY`,
  3/day) — neither eats the other. Class `escalation` bypasses the cooldown and the budget — that is
  the funnel's judgment, already made; don't remark on it either.
- One event = at most one message. If the bundle looks stale (hours old), lean toward brevity — the
  next brief is the backstop.
- A `post_meeting` tap that reaches you already cleared the same bar every other nudge does (quiet
  hours, snooze, and the hold that keeps it silent while you're in the NEXT meeting), and the day is
  capped at `SOTTO_TAP_MAX_PER_DAY` taps — its own cap, not the interrupt budget. So: never apologize for the timing, never ask whether now is a good
  moment, and never fire a second message about the same meeting.
- If a script fails, do nothing — a missed nudge is fine; never improvise a nudge without the bundle.
