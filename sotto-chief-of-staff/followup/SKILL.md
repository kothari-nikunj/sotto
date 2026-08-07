---
name: sotto-followup
description: Use when the user says "follow up on my meetings" / "what did I commit to" / "post-meeting follow-ups" / "anything to send after my meetings", or after a meeting ends — pull commitments + decisions from the recent meeting transcripts and DRAFT the follow-ups to send. On-demand only (the scheduled end-of-day pass now runs inside the evening brief; the standalone 16:45 cron is retired). Draft only, never auto-send.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, meetings, continuity]
    category: productivity
    requires_toolsets: [sotto-local]
    requires_tools: [execute_code]
required_environment_variables:
  - name: GOOGLE_AI_API_KEY
    prompt: Gemini API key (for the follow-up extraction)
    help: https://aistudio.google.com/apikey
    required_for: follow-up extraction
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: feeding commitments into the continuity ledger
---

# Sotto — Post-meeting follow-up

Close the loop **after** meetings: read the transcripts of meetings that just ended, pull out what was
decided and what the user committed to, and **draft** the follow-ups to send. The forward companion to
`sotto-meeting-prep` (prep before) — this is the after. **Draft only; never auto-send.**

## Procedure

> Scripts live under `$HOME/.hermes/skills/sotto/`. Use absolute paths.

1. **Gather Granola (with transcripts) — REQUIRED, deterministic (ONE command; don't hand-fetch via
   MCP tools).** `execute_code`:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_granola.py" --days 3 --transcripts-since-hours 36
   ```
   It reads the Granola connector token from `/setup` (or falls back to `GRANOLA_API_KEY` REST), lists
   recent meetings, fetches the full **transcript** for each meeting that ended in the last ~36h, and
   writes `/tmp/sotto_granola.json` as `{"meetings":[{title, date, time, attendee_emails, your_notes,
   ai_summary[, transcript]}]}`. It prints `[gather_granola] N meetings (T with transcripts, K with
   notes) …` — **gate on those counts**: if **T = 0 AND K = 0** (no transcripts and no notes — Granola
   not connected, or nothing ended recently), say "I don't have transcripts for your recent meetings
   (connect Granola)" and stop — don't invent follow-ups. If there are notes but no transcripts
   (T = 0, K > 0), proceed — `compose_followup.py` works from the `[notes]` branch (fewer, safer items).
2. **Optional context:** `read_local` → `/tmp/sotto_local.json` (contacts, for resolving attendee
   names/emails) and the calendar → `/tmp/sotto_cal.json`.
3. **Extract + draft — ONE command (this IS the follow-up; don't write it yourself):**
   ```bash
   python3 "$HOME/.hermes/skills/sotto/followup/scripts/compose_followup.py" \
     --granola /tmp/sotto_granola.json --local /tmp/sotto_local.json --calendar /tmp/sotto_cal.json
   ```
   It picks the meetings that ended in the last 36h (transcript present), runs the extraction prompt, and
   prints `{followup_markdown, commitments[], drafts[]}`. Deliver `followup_markdown` **verbatim** —
   it is already chat-ready (markers stripped, `*bold*` WhatsApp syntax); don't re-format it.
   *Native fallback (ONLY if `execute_code` is truly unavailable):* run `references/followup-prompt.md`
   with your model over the same gathered inputs — this still applies the real extraction rules
   (grounded-only, verbatim emails, null-when-unknown), unlike free-handing. The script is strongly preferred.
4. **Offer the drafts, one tap each.** For each item in `drafts[]`, present the ready body and a tap-link
   (reuse the brief's tap-link logic — `mailto:`/`https://wa.me/`/`sms:`). **Ask before sending — never
   auto-send** (`sotto-approval-tiers`).
   - **`to_email`/`channel` may be `null`** — the extraction prompt mandates null when the data has no
     address for that attendee. In that branch: present the draft body, ask the user which channel (and
     address) to send it on, and NEVER construct, guess, or look up an address yourself — no tap-link
     until the user supplies the destination.
5. **Feed the ledger — deterministic, right after the user confirms the follow-up summary.** Save the
   full compose output (the JSON from step 3) to `/tmp/sotto_followup.json`, then `execute_code`:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/followup/scripts/apply_commitments.py" \
     /tmp/sotto_followup.json --user-email <the user's email>
   ```
   It writes each commitment as a continuity-ledger item (`$SOTTO_DATA/knowledge/continuity/*.md`,
   the same YAML-frontmatter format + `anchor_key` dedup the brief maintains): the user's commitments
   as `follow_up` (you owe), other attendees' as `waiting_on` (they owe you) — so they show up in
   `sotto-loops` and the next brief immediately, not a day later. Safe to re-run: existing items are
   deduped by anchor_key (bumped, not duplicated), already-resolved/dismissed items are never
   resurrected, and a later brief run won't duplicate them. **Writes files only — never sends.**

## Scheduled/triggered mode

**The standalone 16:45 followup cron is RETIRED** — its end-of-day pass now runs inside the 5:30
evening brief (one end-of-day report instead of two near-identical messages; `sotto-evening-brief`
carries the merged followup content). This skill runs **on demand** and for the coming post-meeting
trigger. If a scheduled/triggered invocation DOES fire this skill (e.g. a post-meeting trigger, or a
leftover cron not yet cleaned up by boot), follow the SAME procedure above with three rules. The
decision is deterministic; `followup_cron.py` owns the state so you don't improvise it.

1. **Window to only what ended SINCE THE LAST CRON RUN** (not a blanket 36h — that re-surfaces
   yesterday's follow-ups every evening). Get the look-back window first:
   ```bash
   SINCE=$(python3 "$HOME/.hermes/skills/sotto/followup/scripts/followup_cron.py" --since-hours)
   ```
   Then gather Granola for that window and pass it through:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_granola.py" \
     --days 3 --transcripts-since-hours "$SINCE"
   python3 "$HOME/.hermes/skills/sotto/followup/scripts/compose_followup.py" \
     --granola /tmp/sotto_granola.json --local /tmp/sotto_local.json --calendar /tmp/sotto_cal.json \
     --since-hours "$SINCE"
   ```
   Save that JSON output to `/tmp/sotto_followup.json`.
2. **Be SILENT when there is nothing actionable.** Ask the helper:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/followup/scripts/followup_cron.py" --silent-check /tmp/sotto_followup.json
   ```
   If it prints `silent` (no commitments AND no drafts) — **say nothing, end the turn.** Do not deliver
   a summary, do not "check in", do not announce that there's nothing. Silence is the correct, common
   output. (This mirrors `sotto-proactive` exactly.) If it prints `deliver`, present `followup_markdown`
   verbatim and offer the drafts as in steps 4–5 above. If it prints `error` (the output file is
   missing/unparseable — compose_followup failed or never ran), treat this window as **not yet
   processed**: say nothing to the user and **do NOT stamp** (step 3). The next cron re-covers the same
   window, so no commitments are lost.
3. **Stamp the marker only on a completed run** — after a `silent` result or a delivered summary (NOT
   after `error`) — so the next cron windows correctly from here:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/followup/scripts/followup_cron.py" --stamp
   ```
   On `error`, skip the stamp entirely; stamping a failed run would mark the window done and skip its
   commitments forever. Commitments still write via `apply_commitments.py` (step 5) after the summary. The cron may deliver
   the follow-up SUMMARY + drafts as a message — that's delivery of Sotto's own report, allowed — but
   sending any draft reply is still the human's tap. **Never auto-send a draft.**

## Notes
- Deliver as **Sotto**, never "Hermes Agent".
- **Grounded only** — commitments/decisions/names come from the transcript; the script + prompt forbid
  inventing them. Thin transcript → fewer items, never padded.
- This is the natural follow-on to `sotto-meeting-prep`; together they bookend every meeting (prep before,
  follow-up after).
- Runs on demand (say "follow up on my meetings"); the scheduled end-of-day pass lives inside the
  evening brief now. The window, silence gate, and run-marker for any scheduled/triggered invocation
  all live in `followup_cron.py` (deterministic, unit-tested) — don't reimplement them.
