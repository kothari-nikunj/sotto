---
name: sotto-proactive
description: 'Use ONLY when the proactive cron fires (every ~15 min), when the Bridge fires a wake trigger (the moment you open your laptop), or when the user says "check for anything urgent" — surface time-sensitive nudges (a meeting about to start, a commitment due today, a birthday) with a draft ready. NOT a brief; it stays silent unless something genuinely needs the user now. Principle: auto-draft, never auto-send.'
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
once-per-day dedup in `proactive_scan.py` apply the same to both triggers, so the wake path never
double-nudges what the cron already sent.

> **CRITICAL — the decision is deterministic; do NOT improvise nudges.** Run `proactive_scan.py` and act
> ONLY on the nudges it returns. It already enforces quiet hours, the meeting lead window, and
> once-per-day dedup. If it returns `{"nudges": []}` — **say nothing, end the turn.** Do not compose a
> brief, do not "check in", do not announce that there's nothing. Silence is the correct, common output.

## Procedure

> Scripts live under `$HOME/.hermes/skills/sotto/`. Use absolute paths.

1. **Gather (deterministic, fast):**
   - Calendar (next few hours) — `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --skip-gmail` → `/tmp/sotto_cal.json` (host-agnostic fallback as in the brief if the CLI isn't this host's Google path).
   - Continuity open-loops — read the active items from `$SOTTO_DATA/knowledge/continuity/*.md` (or the brief's last `actions[]`) into `/tmp/sotto_cont.json` as `[{id,title,deadline,channel,identifier}]`. Skip if none.
   - Local contacts (for birthdays) — `read_local` → `/tmp/sotto_local.json` (or the cached snapshot). Optional.
2. **Decide — ONE command:**
   ```bash
   python3 "$HOME/.hermes/skills/sotto/proactive/scripts/proactive_scan.py" \
     --calendar /tmp/sotto_cal.json --continuity /tmp/sotto_cont.json --local /tmp/sotto_local.json
   ```
   It prints `{"nudges":[…], "quiet":bool}`. **If `nudges` is empty (or `quiet` is true) → STOP, send nothing.**
   It has already recorded what it returns, so it won't repeat a nudge later today.
3. **For each nudge, draft (never send) and deliver ONE concise message:**
   - `meeting_prep` → optionally run `sotto-meeting-prep` for that meeting (or a 2-line who/what), and offer: "want me to pull full prep?" Include the calendar tap-link if handy.
   - `commitment` → draft the reply/message for that open loop (use `sotto-draft-reply` style) and present it with a one-tap link; ask before sending.
   - `birthday` → draft a short, warm note and present it with the contact's tap-link.
   - `retune_offer` → DON'T draft anything. Deliver the one-liner as a light offer ("Your open-loops list
     is getting heavy — N items keep showing up. Want me to run a quick cleanup?"). If the user says yes,
     run **`sotto-retune`**. If they ignore it, drop it — the cooldown means it won't ask again for days.
   - Keep the whole push SHORT — a nudge, not a brief. Lead with the single most time-sensitive item.
   - **Honor `sotto-approval-tiers`: present drafts, never auto-send.** Deliver as **Sotto**.

## Notes
- Quiet hours (default 21:00–07:00), the meeting lead window (default 45 min), and dedup are all in
  `proactive_scan.py` — tune via `SOTTO_QUIET_START/END`, `SOTTO_PROACTIVE_LEAD_MIN`. Don't reimplement them.
- The `retune_offer` nudge fires when ≥`SOTTO_RETUNE_OFFER_MIN` (default 6) loops are stale, at most once
  per `SOTTO_RETUNE_OFFER_COOLDOWN_DAYS` (default 7) — a periodic "want to tidy up?", never a daily nag.
- This skill never writes the knowledge graph or continuity ledger (that's the brief's job) — it's read-only
  except its own nudge-dedup state.
- If `proactive_scan.py` can't run (e.g. `execute_code` unavailable), do nothing — a missed nudge is fine;
  the morning/evening brief is the backstop.
