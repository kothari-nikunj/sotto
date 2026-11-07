---
name: sotto-relationship-pulse
description: Use when the user asks "who am I losing touch with" / "who haven't I talked to" / "relationship pulse" / "who's waiting on me", or on the weekly relationship-pulse cron — a weekly relationship check-in. Produces ONE message with up to five contextual reply-review or reconnect suggestions from ~6 weeks of message/call/email history.
metadata:
  tags: [relationships, chief-of-staff, sotto]
  category: productivity
---

# Sotto — Relationship Pulse

A weekly check on the people in the user's world: **who's going quiet** (relationships that used to
be active and are drifting) and **which messages may warrant a reply review**. This is the standalone version of the
Mac app's relationship analytics — it runs weekly instead of being computed continuously, by reading
a wide window of message/call history from the Bridge and diffing recent vs. baseline cadence.

> **CRITICAL — do not improvise.** The flags MUST come from the pulse computation (it ports the Mac's cadence /
> losing-touch thresholds). Do NOT guess who someone is drifting from. Deliver as **Sotto**, calm and
> matter-of-fact — this is a gentle nudge, not an alarm.

## Attended Sotto runtime

Use the runtime's relationship-pulse procedure for an attended request. It reads the existing
permitted continuity ledger first, then reuses `relationship_state.json`, the canonical local
snapshot and `history-state.json` progress. It does **not** run the full backfill below on every
request. Open commitments retain their recorded direction, deadline and source; birthdays alone,
closed, parked and snoozed entries do not become obligations. Present the recorded specific ask
when available, rather than replaying the long origin summary. Use human dates and short follow-up
lines; channel tags and internal direction labels stay in the structured record. Clearly distinguish
what they owe from what the owner owes, and keep optional birthday greetings nonurgent.

The fresh check uses the last source observation, one both-direction Gmail metadata query
(up to 20 rows, 12-second child deadline) and an 8-second Bridge deadline. The Bridge read is merged
in memory; it never replaces the brief's local snapshot. Without a reliable history cursor it uses the existing 24-hour snapshot interval and says
that historical learning is not yet available. A cap or failed read is disclosed;
old observations are not described as fresh or complete. Background history learning retains
ownership of the full traversal. No extra model, cache or obligation writer is involved.

A complete original history traversal plus contiguous complete fresh evidence can still support
`lapsed`; excluded history pages, capped refreshes or missing channels cannot. Calls have no
background history traversal, so a calls channel never counts as fully read here. When a lapse is
held back, the one-line disclaimer follows any open commitments too. The cadence and
reply-review thresholds below are unchanged. The deliberate manual/scheduled full-read procedure
below remains available; it is not the attended request path.

## Procedure

> **How this runs on Sotto:** the weekly pulse is a receiver-owned procedure. When the owner asks
> for it in chat, run `run_sotto_script relationship-pulse` (no arguments): it queues the complete
> original deterministic pulse — gather, compute, persist, deliver. Stop after the queued receipt;
> the pulse arrives separately as written, without a rewrite from you. It does not promise complete
> coverage of unavailable sources.

1. **Gather a wide window, both halves.** Messages and calls from the Bridge over ≈6 weeks
   (`read_local`, 1,008 hours), and email — the other half of a relationship — over the same 42
   days (inbox and sent, no bodies). If the Bridge is unreachable it runs on email alone; if Google
   isn't connected, on messages alone. Only when BOTH are missing is there nothing to compute — the
   pulse says so in one line.
2. **Compute** — the pulse produces `attention_queue[]`, `relationship_insights[]`, `lapsed[]` and
   `pulse_markdown`. It also writes `$SOTTO_DATA/knowledge/relationship_state.json` so the **daily
   brief** can surface the same flags (the composer merges it automatically) — including a
   per-contact `history` snapshot (last-contact date + cadence) the NEXT run uses to catch people who
   go silent longer than the window.
3. **Deliver** `pulse_markdown` **verbatim** as **Sotto**. It's already one tight message with at most
   five contextual reply-review or reconnect suggestions: visible requests or missed calls to review,
   who you're going quiet with, and — when history exists — who you've **fully lost
   touch with** (`lapsed`: previously-regular contacts absent from the whole ~6-week window, shown
   with their last-known contact date, ranked below the going-quiet list). If a channel someone
   used was not read in full, they are left out and the message ends with one line saying some
   history may be missing — it is delivered as written.
4. **Offer reconnect drafts (grounded only).** When the user follows up on the delivered pulse, you
   may offer a short reconnect message for the top 1–3 `losing_touch` (or `lapsed`) entries
   ("want me to draft a quick hello to Dhruv?"), using the names in the pulse — keep it light, and
   don't fabricate anyone who isn't in the output. Use only the company context already present on
   that entry; otherwise keep the offer generic ("want me to draft a quick hello?") and invent
   nothing. Draft through `sotto-draft-reply` so the user's voice applies. **These are drafts —
   present them, never auto-send;** honor the user's send-approval tier exactly as the brief does.
   Saved attention suggestions are readable with `run_sotto_script relationship-attention`; their
   freshness is unknown.

## Notes
- **Known contacts only.** People who only ever showed up as a raw phone number / shortcode are
  excluded (same is_known_contact filter as the brief) — this is about real relationships. Email
  follows the same gate: a mail counts as a touch only when the other address is in your Contacts
  or your people graph, never a stranger's or a no-reply address, and a mail you sent is a touch to
  each recipient.
- **Cadence, not just recency.** "Going quiet" means the *interval* between contacts is growing for
  someone you used to talk to regularly — not merely that it's been a few days. Someone you ping
  monthly won't be flagged at day 20.
- **How `lapsed` is found.** Each run snapshots per-contact last-contact + cadence into
  `relationship_state.json`'s `history`. Someone with repeated contact in both directions who
  is absent from the current window entirely surfaces as `lapsed` instead of silently vanishing when
  they fall off the 6-week read — but only when every channel you've used with them was read in
  full this window (a Bridge source reported `ok`; the Gmail file was not truncated). Save the
  `read_local` result whole, `source_status` included, so the script can tell. First run has no
  history, so no lapsed section.
- **Reply review is conditional.** A last inbound message is not a debt. Reply suggestions require
  a visible direct request cue or missed call, 3–14 days old, with no later owner touch in the
  available history. Each shows the source and date; a request read fresh also shows its subject/excerpt.
  `relationship_state.json` stores only channel, date and a request cue, never wording, so a retained
  request appears without its text and the brief drops one whose channel is no longer permitted. CC-only email,
  subject-only rows, greetings and visible bulk/newsletter evidence do not earn reply suggestions.
  Observed replies still clear suggestions even when that reply carries bulk labels. When nothing
  qualifies, the output says no clear follow-up was found in available history; it does not declare
  every relationship healthy or all replies complete.
- **Evidence-based attention.** Reply reviews rank ahead of reconnect suggestions; existing sustained
  relationship importance and reciprocal activity rank ahead of age within reply reviews. Observed
  owner replies supply a small positive tie-break. Research depth, titles and stored-fact counts never raise rank.
  Same-channel conversation turns supply two distinct signals: your replies inform attention, their
  replies inform deadline-bounded chase timing. Silence never counts against a person. Learned
  weight decays; explicit VIP and priority choices stay authoritative.
- The receiver owns this job's weekly schedule. Use it; do not create a second weekly job from this
  skill. Missing or unverified schedules follow the setup skill's installation check. Personal
  `user-*` routines are separate.

The same pass stores dated activity evidence for relationship importance: distinct contact days and days in each direction over six weeks. It combines iMessage, WhatsApp, calls and known-person email using the existing identity resolver. Overlapping reads are deduplicated; one noisy conversation does not become a VIP. The shared importance reader classifies VIP/VVIP for gift eligibility and the People skill. Existing attention/quiet-hour policy is separate: a reply-review suggestion establishes neither urgency nor proof of closeness.

When available, completed one-to-one Granola meetings contribute dated held-meeting evidence. They can
strengthen reciprocal relationship evidence but cannot establish importance on their own. A scheduled
meeting, multi-counterpart meeting, self-attendance, or missed call is not relationship evidence. Reply samples and delivery-confirmed Friday candidate
cooldowns share `relationship_state.json`; they never create a preference without your words.
