---
name: sotto-schedule
description: Use when the user says "schedule a meeting" / "find time with X" / "book 30 min" / "set up a call" / "put it on my calendar" / "reschedule" / "when am I free" — or wants to RSVP to an existing invite ("accept my 3pm" / "decline the 9am tomorrow" / "tentative on the board dinner"). Propose times from the user's real calendar and create/move/RSVP the event with their approval. This is THE way to book time; do not hand-wave times without checking the calendar.
metadata:
  tags: [chief-of-staff, sotto, scheduling]
  category: productivity
---

# Sotto — Schedule (find time + book it)

Turn "find time with Dhruv" into real, conflict-free options and a booked event — cloud-side, no Mac.

> **How this runs on Sotto:** the calendar tools named below are native tools; each write is a
> review followed by a separate confirmation. `run_sotto_script google-action calendar-update …` and
> `run_sotto_script google-action calendar-rsvp …` are command forms of the same reviewed edits.

## Procedure

1. **Read the real calendar** — `calendar_list` over the next 3 days (widen the window only if the user asks for later); `calendar_get` reads one exact event. If Calendar is unavailable, say so — don't guess times.
2. **Propose times** — reason over the busy blocks the listing returned to find **free slots** that fit the ask (duration, the user's preferred window, working hours, timezone). There is no free/busy API — you compute availability from the events, and that is not a conflict-free guarantee. Offer **2–3 concrete options** with explicit dates + times + timezone. Never propose a slot that overlaps an existing event.
   - **Resolve the place in the same proposal.** THE TEST IS MECHANICAL: **a location without a
     street number is unresolved** — "Plucky's Cafe, Burlingame, CA" fails it exactly like a bare
     "Blue Bottle" does, and books a string Google Maps guesses at per-attendee (each invitee can be
     pointed at a DIFFERENT branch). Before proposing, for ANY location failing the test:
     (a) check the listed calendar for past events at that venue and reuse their full location
     string; (b) else ask the user for the street address as part of the time proposal — Sotto has
     no automatic venue lookup, and the booked location is `"<venue name>, <street>, <city, state
     zip>"` — name AND address, never one without the other; (c) if it's genuinely ambiguous ("Blue
     Bottle" in a city with ten), ask WHICH one in that same proposal — one question, not two rounds.
     The proposal the user approves AND your confirmation after booking must both show the full
     address, so approval covers the time AND the exact place. Only a location that already carries
     a street number passes through verbatim — don't "improve" those.
3. **On approval, book it** — `calendar_create_review` with the full event details (title, explicit-offset start/end, exact guest addresses, location, optional agenda description). Clarify exact guest addresses and unresolved venues first. Show the full returned review and stop; only a distinct later owner yes permits `calendar_create_confirm` with empty input. Confirm with the event link from its receipt.
4. **Reschedule/update** — resolve the exact event first (`calendar_get` / `calendar_list`), then:
   - a time change: `calendar_move_review` presents the complete old/new time review; only a distinct subsequent clear yes permits `calendar_move_confirm` with empty input;
   - title, location or agenda: `calendar_update_review` with `event_id` and the selected fields, then `calendar_update_confirm` after a distinct yes;
   - a combined time-and-detail change on one timed event or exact occurrence: `calendar_move_update_review` with both explicit-offset start/end and the selected fields, then `calendar_move_update_confirm` only after a distinct owner yes.
   This patches the same event, preserving its guests, conferencing and other fields. An empty selected detail clears it; omission preserves it. Never silently split a combined time/detail request into separate writes; if it cannot be reviewed together, say so. Do not delete/recreate an event to move it. A permission error is not a reason to recreate someone else's event. Series and all-day moves and attendee edits are not supported.
5. **RSVP to an invite** ("accept my 3pm", "decline the 9am tomorrow", "tentative on the board dinner") — resolve WHICH event from the calendar, matching the user's phrasing (time, title, day) to an exact provider invitation. If more than one plausibly matches, **confirm which one** before acting. Use `calendar_rsvp_review` with the event and response `accepted|declined|tentative`; an occurrence's `recurringEventId` can identify its master via `calendar_get`, but an entire-series RSVP must be explicitly reviewed, never inferred from an occurrence. Show the full review, then wait for a separate owner yes before `calendar_rsvp_confirm`. It changes only YOUR response and notifies the organizer — confirm the result to the user ("Declined *Board Dinner* — the organizer's been notified"). If you're the organizer with no attendee entry there is nothing to RSVP; don't retry.
   - An already-known non-primary calendar must be supplied as `calendar_id` on the exact `calendar_get` and the matching review. Show the full review including its calendar ID, then wait for a separate owner yes; the matching confirmation carries no replacement calendar or event. Omit the selector for primary. Do not infer calendar discovery or secondary-calendar listing from this support. `calendar_rsvp_review` accepts optional `comment`. Show the exact old/new own-attendee comment in the full review and organizer/guest notification before separate yes. An empty comment leaves the existing note unchanged; it does not clear it. A genuinely changed comment may accompany an unchanged response; a true no-op is refused. Never silently drop a requested note.
6. **Record** (optional) — the brief sees the booked event on its next calendar read; there is nothing extra to write.

## Notes
- Gathered `supporting_context` entries are prep notes attached to the same meeting, not extra commitments. Preserve their descriptions for context. Do not propose declining either entry as a conflict. Raw calendar entries only qualify when explicitly labelled context/notes, uniquely matched by subject, and exactly equal in start/end; a shared time alone is not evidence.
- **Approval first, always** (`_shared/references/approval-tiers.md`). Creating/deleting an event is a real action — never do it without an explicit go-ahead. `review` → show the proposed event and let the user adjust before you create it.
- **The calendar verbs are gated in code, not just here.** In a scheduled, proactive or event-triage run they refuse before any network call: **do not retry it and do not work around it** — propose the event in the brief and let the user ask for it in conversation. Every attempt, allowed or refused, leaves one metadata-only record with the hash of the event's own content — never its text.
- **A go-ahead from an earlier review is not reusable.** Edits need a new review; an uncertain outcome remains held and is inspected, never retried.
- **ISO 8601 with timezone offset** for start/end (e.g. `2026-06-27T14:00:00-07:00`), or UTC `Z`. A bare local time will be rejected.
- **RSVP is `one_tap`**: reversible and low-risk, but still a calendar write — never fire it without the user's explicit in-chat go-ahead, and **never** in an unattended/proactive context. Calendar updates require the account to have edit access (see step 4); an RSVP only changes YOUR responseStatus.
- External attendees get a Google invite automatically when the event lists them as guests.
- Deliver as **Sotto**.
