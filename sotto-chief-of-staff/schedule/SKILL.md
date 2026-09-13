---
name: sotto-schedule
description: Use when the user says "schedule a meeting" / "find time with X" / "book 30 min" / "set up a call" / "put it on my calendar" / "reschedule" / "when am I free" — or wants to RSVP to an existing invite ("accept my 3pm" / "decline the 9am tomorrow" / "tentative on the board dinner"). Propose times from the user's real calendar and create/move/RSVP the event with their approval. This is THE way to book time; do not hand-wave times without checking the calendar.
metadata:
  hermes:
    tags: [chief-of-staff, sotto, scheduling]
    category: productivity
    requires_toolsets: [google-workspace]
    requires_tools: [execute_code]
required_environment_variables:
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: continuity (logging the booked event)
---

# Sotto — Schedule (find time + book it)

Turn "find time with Dhruv" into real, conflict-free options and a booked event — cloud-side, no Mac.

## Procedure

> **Script paths:** absolute, e.g. `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/google_action.py"`.

1. **Read the real calendar (deterministic)** — `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --skip-gmail` → `/tmp/sotto_cal.json` (next 3 days; widen the horizon by editing the script call only if the user asks for later). **Host-agnostic fallback:** if it reports the CLI isn't this host's Google path (`google_api.py not found … FALLBACK …`), list the next 3 days with the host's Calendar **MCP** tool, dump raw to `/tmp/sotto_cal_raw.json`, then `gather_google.py --skip-gmail --from-mcp-calendar /tmp/sotto_cal_raw.json`. Only if neither exists is Calendar unavailable — say so, don't guess times.
2. **Propose times** — reason over the busy blocks in `/tmp/sotto_cal.json` to find **free slots** that fit the ask (duration, the user's preferred window, working hours, timezone). There is no free/busy API — you compute availability from the events. Offer **2–3 concrete options** with explicit dates + times + timezone. Never propose a slot that overlaps an existing event.
   - **Resolve the place in the same proposal.** THE TEST IS MECHANICAL: **a location without a
     street number is unresolved** — "Plucky's Cafe, Burlingame, CA" fails it exactly like a bare
     "Blue Bottle" does, and books a string Google Maps guesses at per-attendee (each invitee can be
     pointed at a DIFFERENT branch). Before proposing, for ANY location failing the test:
     (a) check the gathered calendar for past events at that venue and reuse their full location
     string; (b) else resolve to one full street address with a single grounded lookup —
     `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/web_research.py" "Plucky's Cafe Burlingame
     CA street address"` (bias the query with whatever geography you know: the user's city/timezone,
     the other attendee's company location, surrounding events that day); the booked `--location` is
     `"<venue name>, <street>, <city, state zip>"` — name AND address, never one without the other;
     (c) if it's genuinely ambiguous ("Blue Bottle" in a city with ten), ask WHICH one as part of the
     time proposal — one question, not two rounds. The proposal the user approves AND your
     confirmation after booking must both show the full address, so approval covers the time AND the
     exact place. Only a location that already carries a street number passes through verbatim —
     don't "improve" those.
3. **On approval, book it** — `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/google_action.py" calendar-create --summary "<title>" --start <ISO-with-offset> --end <ISO-with-offset> --attendees "a@x.com,b@y.com" --location "<place, if the user gave one>" --description "<agenda note, optional>"`. It returns `{status:"created", id, summary, htmlLink}`. Confirm with the `htmlLink`. If the result carries `location_attached: false`, the host's CLI couldn't attach the place — say so honestly and include the address in your confirmation message (never silently drop it, never stuff it into the title). If it carries `organizer_listed: false`, tell the user plainly in the confirmation ("heads-up: Sotto doesn't know your own address yet — connect Google on `/setup` (or set `SOTTO_USER_EMAIL`) — so you won't appear in the invite's guest list") — the invite still works; the guest list just won't show them.
4. **Reschedule/update** — use `google_action.py calendar-update --event-id <id> --fields-json '{"start":{"dateTime":"<ISO-with-offset>"},"end":{"dateTime":"<ISO-with-offset>"}}'`. This patches the same event, preserving its guests, conferencing and other fields. Supported fields: summary, start, end, location, description. Omitted fields stay unchanged. Resolve the exact event and get the user's approval before changing it; do not delete/recreate an event to move it. A permission error is not a reason to recreate someone else's event.
5. **RSVP to an invite** ("accept my 3pm", "decline the 9am tomorrow", "tentative on the board dinner") — resolve WHICH event from the gathered calendar (`/tmp/sotto_cal.json`), matching the user's phrasing (time, title, day) to an event **id**. If more than one plausibly matches, **confirm which one** before acting. On approval (`one_tap`), run `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/google_action.py" calendar-rsvp --event-id <id> --response accepted|declined|tentative` (add `--calendar <cal-id>` if it's not on `primary`, `--comment "<note>"` to pass a note to the organizer). It resolves your own attendee entry, preserves all other attendees, and notifies the organizer (`sendUpdates=all`). It returns `{status:"rsvped", event_id, response, summary, start}` — confirm the result to the user ("Declined *Board Dinner* — the organizer's been notified"). If you're the organizer with no attendee entry it returns a clear error ("nothing to RSVP"); don't retry. **If it returns `{status:"error", fallback:"deep_link"}`** (this host's `google_api.py` can't do calendar get/patch — RSVP by API is unavailable here), fall back to the **pre-RSVP behavior**: give the user the event's calendar **deep link** so they can RSVP by hand, and tell them their host's `google-workspace` CLI needs updating to enable one-tap RSVP. **Never** show the raw usage/error text — surface the plain-English capability note instead.
6. **Record** (optional) — note the booked event in continuity so the brief reflects it.

## Notes
- Gathered `supporting_context` entries are prep notes attached to the same meeting, not extra commitments. Preserve their descriptions for context. Do not propose declining either entry as a conflict. Raw calendar entries only qualify when explicitly labelled context/notes, uniquely matched by subject, and exactly equal in start/end; a shared time alone is not evidence.
- **Approval first, always** (`_shared/references/approval-tiers.md`). Creating/deleting an event is a real action — never do it without an explicit go-ahead. `review` → show the proposed event and let the user adjust before you create it.
- **The calendar verbs are gated in code, not just here.** In a scheduled, proactive or event-triage run they refuse before any network call (`{status:"error", error:"refused: unattended run …", fallback:"propose_in_brief"}`, non-zero exit): **do not retry it and do not work around it** — propose the event in the brief and let the user ask for it in conversation. Every attempt, allowed or refused, leaves one metadata-only line in `$SOTTO_DATA/events/sends.jsonl` with the hash of the event's own content — never its text.
- **If the go-ahead came from a nudge Sotto delivered in another session** (a bare "sure" resolved through `pending_offer.py get`, and that offer carries a `payload_sha256`), add `--offer-bound --offer-id <that offer's offer_id>` to the verb: it requires that exact fresh offer, re-checks the hash of what you are about to write against what the user approved, refuses on a mismatch, and consumes the offer before the write starts (a retry after any outcome needs a fresh offer). A bare `--offer-bound` without the id is refused. Nothing to add for an in-session yes.
- **ISO 8601 with timezone offset** for `--start`/`--end` (e.g. `2026-06-27T14:00:00-07:00`), or UTC `Z`. A bare local time will be rejected.
- **RSVP is `one_tap`**: reversible and low-risk, but still a calendar write — never fire it without the user's explicit in-chat go-ahead, and **never** in an unattended/proactive context. Calendar updates require the account to have edit access (see step 4); `calendar-rsvp` only changes YOUR responseStatus.
- External attendees get a Google invite automatically when you pass `--attendees`.
- Deliver as **Sotto**, never "Hermes Agent".
