---
name: sotto-followup
description: Use when the user says "follow up on my meetings" / "what did I commit to" / "open items from my Granola notes" / "post-meeting follow-ups" / "anything to send after my meetings", or after a meeting ends — pull commitments + decisions from recent meeting notes/transcripts, record them in open loops, and DRAFT any follow-ups to send. On-demand only (the scheduled end-of-day pass now runs inside the evening brief; the standalone 16:45 cron is retired). Never auto-send.
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

1. **Choose the window, then gather — REQUIRED and deterministic.** Do not hand-fetch via MCP tools.
   Use the short lane for a just-ended meeting or “follow up on my meetings”:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_granola.py" --days 3 --transcripts-since-hours 36
   ```
   Use the backlog lane for “open items from Granola”, “what did I commit to”, or “did we capture
   these in open loops” — fourteen days of notes, without paying to fetch old transcripts:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_granola.py" --days 14 --transcripts-since-hours 0
   ```
   It reads the Granola connector token from `/setup` (or falls back to `GRANOLA_API_KEY` REST), lists
   meetings, fetches full transcripts only for the short lane, and
   writes `/tmp/sotto_granola.json` as `{"meetings":[{meeting_id, title, start, end, date, time, attendee_emails, your_notes,
   ai_summary[, transcript]}]}`. It prints `[gather_granola] N meetings (T with transcripts, K with
   notes) …` — **gate on those counts**: if **T = 0 AND K = 0** (no transcripts and no notes — Granola
   not connected, or nothing ended recently), say "I don't have transcripts for your recent meetings
   (connect Granola)" and stop — don't invent follow-ups. If there are notes but no transcripts
   (T = 0, K > 0), proceed — `compose_followup.py` works from the `[notes]` branch (fewer, safer items).
2. **Optional context:** `read_local` → `/tmp/sotto_local.json` (contacts, for resolving attendee
   names/emails) and the calendar → `/tmp/sotto_cal.json`.
3. **Extract + persist — ONE command; use the same window as step 1.** For the short lane:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/followup/scripts/compose_followup.py" \
     --granola /tmp/sotto_granola.json --local /tmp/sotto_local.json --calendar /tmp/sotto_cal.json \
     --since-hours 36
   ```
   For the backlog lane, use all fourteen days and require the post-write canonical ledger snapshot:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/followup/scripts/compose_followup.py" \
     --granola /tmp/sotto_granola.json --local /tmp/sotto_local.json --calendar /tmp/sotto_cal.json \
     --since-hours 336 --reconcile-open-loops
   ```
   It picks meetings inside the selected window, runs the extraction prompt, and
   writes every `commitments[]` item into the continuity ledger **before** it prints
   `{followup_markdown, commitments[], drafts[], ledger[, open_loops]}`. `ledger` is the deterministic
   write receipt; backlog mode's `open_loops` is the read-back after those writes. For the short lane,
   deliver `followup_markdown` **verbatim** — it is already chat-ready. For the backlog lane, answer
   “what is open” from `open_loops`, not merely from extracted intent; mention anything in
   `ledger.skipped_terminal > 0` as already closed and not reopened.
   *Native fallback (ONLY if `execute_code` is truly unavailable):* run `references/followup-prompt.md`
   with your model over the same gathered inputs — this still applies the real extraction rules
   (grounded-only, verbatim emails, null-when-unknown), unlike free-handing. The script is strongly preferred.
4. **Offer the drafts — email asks, every other channel links** (`sotto-draft-reply` step 4 is the
   one rule; don't restate it differently here). For each item in `drafts[]`, present the ready body,
   then: **email** → ask *"want this in your Gmail drafts?"* and on yes run `google_action.py
   gmail-draft --to <addr> --body "<draft>"` (add `--thread-id` if the follow-up answers a known
   thread), confirming in one line; **iMessage/SMS/WhatsApp** → a tap-link (`https://wa.me/`/`sms:`).
   **Ask before sending — never auto-send** (`_shared/references/approval-tiers.md`).
   - **`to_email`/`channel` may be `null`** — the extraction prompt mandates null when the data has no
     address for that attendee. In that branch: present the draft body, ask the user which channel (and
     address) to send it on, and NEVER construct, guess, or look up an address yourself — no tap-link
     until the user supplies the destination.
5. **Trust the receipt, not intent.** Recording a private open loop is reversible bookkeeping, not
   an outward action, so it does **not** require a confirmation turn. Confirmation still gates Gmail
   drafts and every send in step 4. Never say commitments were captured unless step 3 returned a
   `ledger` receipt; if the command fails before printing, report that capture failed. Safe re-runs
   dedupe by `anchor_key`, and terminal items are never resurrected. When the user corrects the
   result ("that is done", "nothing for me to do", "James will get back to me"), route the explicit
   edits to `sotto-loops` §C and verify the resulting ledger before confirming them.

## Notes
- Deliver as **Sotto**, never "Hermes Agent".
- **Grounded only** — commitments/decisions/names come from the transcript; the script + prompt forbid
  inventing them. Thin transcript → fewer items, never padded.
- Runs on demand (say "follow up on my meetings"); the scheduled end-of-day pass lives inside the
  evening brief now (`sotto-evening-brief` carries the merged follow-up content). There is no
  standalone follow-up cron — `adapters/hermes/crons.json` is the schedule, and it does not list one.
