---
name: sotto-followup
description: Use when the user says "follow up on my meetings" / "what did I commit to" / "post-meeting follow-ups" / "anything to send after my meetings", after a meeting ends, OR when the light evening followup cron fires (~16:45 local) — pull commitments + decisions from the recent meeting transcripts and DRAFT the follow-ups to send. Draft only, never auto-send. On the cron it is SILENT when nothing is actionable.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, meetings, continuity]
    category: productivity
    requires_toolsets: [granola]
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

1. **Gather Granola (with transcripts) — REQUIRED.** Via the Granola MCP: list meetings from the last
   ~36h that have ended, and for each fetch the full **transcript** (the get-transcript tool). Write
   `[{title, date, attendee_emails, transcript, ai_summary}]` to `/tmp/sotto_granola.json`. If the Granola
   MCP isn't configured or there are no transcripts, say "I don't have transcripts for your recent
   meetings (connect Granola)" and stop — don't invent follow-ups.
2. **Optional context:** `read_local` → `/tmp/sotto_local.json` (contacts, for resolving attendee
   names/emails) and the calendar → `/tmp/sotto_cal.json`.
3. **Extract + draft — ONE command (this IS the follow-up; don't write it yourself):**
   ```bash
   python3 "$HOME/.hermes/skills/sotto/followup/scripts/compose_followup.py" \
     --granola /tmp/sotto_granola.json --local /tmp/sotto_local.json --calendar /tmp/sotto_cal.json
   ```
   It picks the meetings that ended in the last 36h (transcript present), runs the extraction prompt, and
   prints `{followup_markdown, commitments[], drafts[]}`. Deliver `followup_markdown` **verbatim**.
4. **Offer the drafts, one tap each.** For each item in `drafts[]`, present the ready body and a tap-link
   (reuse the brief's tap-link logic — `mailto:`/`https://wa.me/`/`sms:`). **Ask before sending — never
   auto-send** (`sotto-approval-tiers`).
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

## Cron mode (evening followup cron, ~16:45 local)

When the `sotto-followup` cron fires (registered by start.sh/install.sh — a light evening pass so
follow-ups run without being asked), follow the SAME procedure above with three cron-specific rules.
The decision is deterministic; `followup_cron.py` owns the state so you don't improvise it.

1. **Window to only what ended SINCE THE LAST CRON RUN** (not a blanket 36h — that re-surfaces
   yesterday's follow-ups every evening). Get the look-back window first:
   ```bash
   SINCE=$(python3 "$HOME/.hermes/skills/sotto/followup/scripts/followup_cron.py" --since-hours)
   ```
   Then gather Granola for that window and pass it through:
   ```bash
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
- **Grounded only** — commitments/decisions/names come from the transcript; the script + prompt forbid
  inventing them. Thin transcript → fewer items, never padded.
- This is the natural follow-on to `sotto-meeting-prep`; together they bookend every meeting (prep before,
  follow-up after).
- Runs on demand (say "follow up on my meetings") AND on the light evening cron above. The cron's window,
  silence gate, and run-marker all live in `followup_cron.py` (deterministic, unit-tested) — don't
  reimplement them.
