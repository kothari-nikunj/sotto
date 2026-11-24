---
name: sotto-followup
description: Use when the user says "follow up on my meetings" / "what did I commit to" / "what did I promise in yesterday's meetings" / "open items from my Granola notes" / "post-meeting follow-ups" / "anything to send after my meetings", or after a meeting ends — pull commitments + decisions from recent meeting notes/transcripts, record them in open loops, and DRAFT any follow-ups to send. On-demand only (the scheduled end-of-day pass now runs inside the evening brief; the standalone 16:45 cron is retired). Never auto-send.
metadata:
  tags: [chief-of-staff, sotto, meetings, continuity]
  category: productivity
---

# Sotto — Post-meeting follow-up

Close the loop **after** meetings: read the transcripts of meetings that just ended, pull out what was
decided and what the user committed to, and **draft** the follow-ups to send. The forward companion to
`sotto-meeting-prep` (prep before) — this is the after. **Draft only; never auto-send.**

> **How this runs on Sotto:** `run_sotto_script X …` means one `run_sotto_script` call with id `X`
> and the rest as its argv array — no shell, no file paths. Other backticked names are native tools.

## Procedure

1. **Choose the window, then run ONE command.** The receiver owns the gather, extraction and
   capture; do not hand-fetch Granola or compose follow-ups yourself.
   - The short lane, for a just-ended meeting or “follow up on my meetings”:
     `run_sotto_script followup` — three days of notes, with full transcripts for meetings that
     ended in the last 36 hours.
   - The backlog lane, for “open items from Granola”, “what did I commit to”, or “did we capture
     these in open loops”: `run_sotto_script followup --backlog` — fourteen days of notes, without
     paying to fetch old transcripts, reconciled against the open loops.
   The result is a queued acknowledgement; stop there. The receiver runs the original extraction
   over the meetings inside the selected window and writes every `commitments[]` item into the
   continuity ledger **before** it delivers `{followup_markdown, commitments[], drafts[], ledger[, open_loops]}`
   in one worker-owned message. `ledger` is the write receipt and `open_loops` is the readback. The recent-meeting message starts with `followup_markdown` and adds the applicable draft text and next steps. For the backlog lane, the delivered answer to
   “what is open” comes from `open_loops`, not merely from extracted intent, and names anything in
   `ledger.skipped_terminal > 0` as already closed and not reopened. Do not compose a replacement or
   claim delivery; stopping this chat does not cancel the queued job.
   - **No transcripts and no notes** (Granola not connected, or nothing ended recently) means there
     is nothing to follow up: "I don't have transcripts for your recent meetings (connect Granola)" —
     don't invent follow-ups. Notes without transcripts still work (fewer, safer items).
2. **Offer the drafts — email asks, every other channel is text to send yourself**
   (`sotto-draft-reply` step 4 is the one rule; don't restate it differently here). Each delivered draft gives its own next step. **Email with a known address** → the worker asks *"Would you like to review this follow-up for Gmail drafts?"*; on
   the user's yes, call `gmail_draft_followup_list`, clarify when more than one offer fits, then
   `gmail_draft_followup_review` with that exact returned offer ID: it presents the full
   recipient/subject/body review, and only a distinct later yes permits `gmail_draft_save`. Confirm
   in one line. **iMessage/SMS/WhatsApp** →
   the user sends the body in the app themselves. A known local channel gets the draft text and
   an instruction to paste it into the existing conversation, without email subject/destination
   placeholders or another channel/address question. An unknown channel or a missing email destination needs clarification.
   **Ask before saving or sending — never auto-send** (`_shared/references/approval-tiers.md`).
   - **`to_email`/`channel` may be `null`** — the extraction prompt mandates null when the data has no
     address for that attendee. In that branch: ask the user which channel (and address) to send it
     on, and NEVER construct, guess, or look up an address yourself. For email, pass only the exact
     address the user supplies in the current message as the review's optional `to`.
3. **Trust the receipt, not intent.** Recording a private open loop is reversible bookkeeping, not
   an outward action, so it does **not** require a confirmation turn. Confirmation still gates Gmail
   drafts and every send in step 2. Claim only the changes the `ledger` receipt confirms. A source failure before capture means the notes could not be processed. If the worker cannot confirm the outcome, describe it as unconfirmed. Safe
   re-runs dedupe by `anchor_key`, and terminal items are never resurrected. When the user corrects
   the result ("that is done", "nothing for me to do", "James will get back to me"), route the
   explicit edits to `sotto-loops` §C and verify the resulting ledger before confirming them.

## Notes
- Deliver as **Sotto**.
- **Grounded only** — commitments/decisions/names come from the transcript; the pipeline + prompt
  (`references/followup-prompt.md`) forbid inventing them. Thin transcript → fewer items, never padded.
  An inline reply drafted in chat is wording, not evidence the owner promised it in a meeting or sent it.
  If current meeting notes are unavailable, say the promise cannot be verified from them.
- Runs on demand (say "follow up on my meetings"); the scheduled end-of-day pass lives inside the
  evening brief now (`sotto-evening-brief` carries the merged follow-up content). There is no
  standalone follow-up job in Sotto's schedule.
