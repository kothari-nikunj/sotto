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

1. **Read the real calendar (deterministic)** — `execute_code` → `gather_google.py --skip-gmail` → `/tmp/sotto_cal.json` (next 3 days; widen the horizon by editing the script call only if the user asks for later). **Host-agnostic fallback:** if it reports the CLI isn't this host's Google path (`google_api.py not found … FALLBACK …`), list the next 3 days with the host's Calendar **MCP** tool, dump raw to `/tmp/sotto_cal_raw.json`, then `gather_google.py --skip-gmail --from-mcp-calendar /tmp/sotto_cal_raw.json`. Only if neither exists is Calendar unavailable — say so, don't guess times.
2. **Propose times** — reason over the busy blocks in `/tmp/sotto_cal.json` to find **free slots** that fit the ask (duration, the user's preferred window, working hours, timezone). There is no free/busy API — you compute availability from the events. Offer **2–3 concrete options** with explicit dates + times + timezone. Never propose a slot that overlaps an existing event.
3. **On approval, book it** — `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/google_action.py" calendar-create --summary "<title>" --start <ISO-with-offset> --end <ISO-with-offset> --attendees "a@x.com,b@y.com"`. It returns `{status:"created", id, summary, htmlLink}`. Confirm with the `htmlLink`.
4. **Reschedule** = create the new event, then `calendar-delete --event-id <old id>` (the CLI has no in-place update). Confirm both.
5. **RSVP to an invite** ("accept my 3pm", "decline the 9am tomorrow", "tentative on the board dinner") — resolve WHICH event from the gathered calendar (`/tmp/sotto_cal.json`), matching the user's phrasing (time, title, day) to an event **id**. If more than one plausibly matches, **confirm which one** before acting. On approval (`one_tap`), run `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/google_action.py" calendar-rsvp --event-id <id> --response accepted|declined|tentative` (add `--calendar <cal-id>` if it's not on `primary`, `--comment "<note>"` to pass a note to the organizer). It resolves your own attendee entry, preserves all other attendees, and notifies the organizer (`sendUpdates=all`). It returns `{status:"rsvped", event_id, response, summary, start}` — confirm the result to the user ("Declined *Board Dinner* — the organizer's been notified"). If you're the organizer with no attendee entry it returns a clear error ("nothing to RSVP"); don't retry. **If it returns `{status:"error", fallback:"deep_link"}`** (this host's `google_api.py` can't do calendar get/patch — RSVP by API is unavailable here), fall back to the **pre-RSVP behavior**: give the user the event's calendar **deep link** so they can RSVP by hand, and tell them their host's `google-workspace` CLI needs updating to enable one-tap RSVP. **Never** show the raw usage/error text — surface the plain-English capability note instead.
6. **Record** (optional) — note the booked event in continuity so the brief reflects it.

## Notes
- **Approval first, always** (`sotto-approval-tiers`). Creating/deleting an event is a real action — never do it without an explicit go-ahead. `review` → show the proposed event and let the user adjust before you create it.
- **ISO 8601 with timezone offset** for `--start`/`--end` (e.g. `2026-06-27T14:00:00-07:00`), or UTC `Z`. A bare local time will be rejected.
- **RSVP is `one_tap`** (`sotto-approval-tiers`): reversible and low-risk, but still a calendar write — never fire it without the user's explicit in-chat go-ahead, and **never** in an unattended/proactive context. In-place reschedule of someone else's event is still create+delete (see step 4); `calendar-rsvp` only changes YOUR responseStatus.
- External attendees get a Google invite automatically when you pass `--attendees`.
- Deliver as **Sotto**, never "Hermes Agent".
