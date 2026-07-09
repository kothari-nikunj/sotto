---
name: sotto-relationship-pulse
description: Use when the user asks "who am I losing touch with" / "who haven't I talked to" / "relationship pulse" / "who's waiting on me", or on the weekly relationship-pulse cron — a weekly relationship check-in. Produces ONE message flagging people going quiet (you used to talk more) and people waiting on a reply — computed from ~6 weeks of message/call history.
metadata:
  hermes:
    tags: [relationships, chief-of-staff, sotto]
    category: productivity
    requires_toolsets: [sotto-local]
    requires_tools: [execute_code]
required_environment_variables:
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: persisting the relationship state for the daily brief
---

# Sotto — Relationship Pulse

A weekly check on the people in the user's world: **who's going quiet** (relationships that used to
be active and are drifting) and **who's waiting on a reply**. This is the standalone version of the
Mac app's relationship analytics — it runs weekly instead of being computed continuously, by reading
a wide window of message/call history from the Bridge and diffing recent vs. baseline cadence.

> **CRITICAL — do not improvise.** The flags MUST come from the script (it ports the Mac's cadence /
> losing-touch thresholds). Do NOT guess who someone is drifting from. Deliver as **Sotto**, calm and
> matter-of-fact — this is a gentle nudge, not an alarm.

## Procedure

> **Script path:** use the absolute path —
> `python3 "$HOME/.hermes/skills/sotto/relationship-pulse/scripts/relationship_pulse.py"`.

1. **Gather a wide window** — call the Bridge `read_local(since_hours=1008)` (≈6 weeks). This is the
   one input; it gives the message/call history the cadence math needs. Save it to `/tmp/sotto_local_6w.json`.
   (If the Bridge is unreachable, say so in one line and stop — there's nothing to compute without local history.)
2. **Compute** — `execute_code`:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/relationship-pulse/scripts/relationship_pulse.py" /tmp/sotto_local_6w.json
   ```
   It prints JSON: `attention_queue[]`, `relationship_insights[]`, `lapsed[]`, `pulse_markdown`. It
   also writes `/data/knowledge/relationship_state.json` so the **daily brief** can surface the same
   flags (compose_brief merges it automatically) — including a per-contact `history` snapshot
   (last-contact date + cadence) the NEXT run uses to catch people who go silent longer than the window.
3. **Deliver** `pulse_markdown` **verbatim** as **Sotto**. It's already one tight message: who's
   waiting on you, who you're going quiet with, and — when history exists — who you've **fully lost
   touch with** (`lapsed`: previously-regular contacts absent from the whole ~6-week window, shown
   with their last-known contact date, ranked below the going-quiet list). Where a nudge is obvious,
   you may add a short conversational offer ("want me to draft a quick hello to Dhruv?") using the
   names in the list — but keep it light, and don't fabricate anyone who isn't in the output.
4. **Offer reconnect drafts (grounded only).** For the top 1–3 `losing_touch` (or `lapsed`) entries, you may offer a
   short reconnect message. Build it **only** from that entry's `graph_context` (company / title /
   `talking_point` / `fact` / `summary`) — a real, specific hook the user already knows ("saw Acme
   shipped X — wanted to say hi"). If an entry has **no** `graph_context`, keep the offer generic
   ("want me to send a quick hello?") and invent nothing. Match the user's voice using the style
   profile (`$SOTTO_DATA/style.json`) if present. **These are drafts — present them, never auto-send;**
   honor the user's send-approval tier exactly as the brief's worker dispatch does.

## Notes
- **Known contacts only.** People who only ever showed up as a raw phone number / shortcode are
  excluded (same is_known_contact filter as the brief) — this is about real relationships.
- **Cadence, not just recency.** "Going quiet" means the *interval* between contacts is growing for
  someone you used to talk to regularly — not merely that it's been a few days. Someone you ping
  monthly won't be flagged at day 20.
- **Longitudinal memory (lapsed).** Each run snapshots per-contact last-contact + cadence into
  `relationship_state.json`'s `history` block. A contact who was previously regular (≥5 touches in a
  past window) but is **absent from the current window entirely** surfaces as `lapsed` — "you've fully
  lost touch with…" — instead of silently vanishing when they fall off the 6-week read. First run
  (no history yet) simply has no lapsed section. Reconnect offers for lapsed people follow the exact
  same grounding rules as `losing_touch`: graph context only, drafts only, never auto-send.
- **Graph-weighted ranking.** The queue is ordered by interaction volume *and* knowledge-graph
  importance: a person you actively track (a `people/*.md` file with facts / talking points / a known
  company) outranks a chatty-but-shallow contact going quiet. Untracked people fall back to the old
  volume-only ranking, so the weighting only ever sharpens the order — it never hides anyone.
- Cron: schedule weekly, e.g. `hermes cron create "0 9 * * 1" "Run my relationship pulse" --skill sotto-relationship-pulse` (Monday 9am).
