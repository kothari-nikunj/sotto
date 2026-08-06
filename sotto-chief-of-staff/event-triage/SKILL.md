---
name: sotto-event
description: 'Use ONLY when the event receiver hands you a triaged event bundle (a real-time iMessage/WhatsApp/call/email that survived the triage funnel — the prompt names a bundle path under $SOTTO_DATA/events/), or when the sotto-midday-digest cron fires (digest mode). Deliver ONE short nudge as Sotto with a ready-to-send draft — never auto-send. NOT for polled proactive checks (that is sotto-proactive) and never invoked by the user directly.'
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
   it is the ground truth for who/what.
2. **Context (read-only views only):**
   - Open loops with this person — `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/loops_query.py"`
     → `{you_owe:[…], waiting_on_them:[…]}`; keep only entries matching the sender (a loop with them
     is often the "why now").
   - Who they are — `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/knowledge_query.py" --person "<name|id>"`
     → identity + facts + talking points. Unknown sender = fine, proceed with what the bundle has.
3. **Compose ONE nudge as Sotto** — who / what / why-now in **≤ 2 sentences**. Lead with the ask
   ("Dhruv just asked if you can move tomorrow's 10am — you still owe him the deck too"), not with
   "you received a message". A missed call nudge is "X just called (no answer)" + the callback draft.
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
3. Else compose **ONE compact catch-up message** from its `items` — **hard cap 6 lines**, grouped by
   person, most actionable first, no drafts and no links needed here (the user can ask for any).
   Deliver as Sotto.
4. After delivering, stamp it so tomorrow's window starts here:
   `python3 "$HOME/.hermes/skills/sotto/event-triage/scripts/digest_check.py" --stamp`.

## Notes
- Cooldowns, quiet hours, dedup, and drop/queue decisions all live in the funnel
  (`triage_event.py`) — don't reimplement or second-guess them here.
- One event = at most one message. If the bundle looks stale (hours old), lean toward brevity — the
  next brief is the backstop.
- If a script fails, do nothing — a missed nudge is fine; never improvise a nudge without the bundle.
