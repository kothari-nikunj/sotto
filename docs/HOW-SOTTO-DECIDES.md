# How Sotto decides when to interrupt you

Sotto is quiet by default. This page explains, in plain rules, why a message became a nudge — or
why it didn't. It is a summary: the code is the source of truth, and every rule below lives in
`sotto-chief-of-staff/event-triage/scripts/triage_event.py` (the funnel),
`runtime/trigger-receiver/receiver.py` (what arrives, and what gets dispatched), and
`runtime/trigger-receiver/calcache.py` (the calendar cache and the post-meeting tap). Every knob
named here is documented in [RAILWAY.md](../RAILWAY.md) § *Environment variables*.

## What arrives

- **Bridge events** — the Mac app watches iMessage, WhatsApp, and calls and POSTs new rows to
  `/bridge/events` within seconds (`SOTTO_EVENTS_TICK_SECS`, default 3s).
- **Email** — the container polls Gmail itself every `SOTTO_EMAIL_POLL_SECS` (default 90s) and feeds
  new mail through the same endpoint. No Mac needed.
- **Calendar** — a background thread refreshes today's events every `SOTTO_CALENDAR_REFRESH_SECS`
  (default 15 min). It powers the in-meeting hold and detects meetings that just ended.

Every event is deduped by `(source, rowid)` and then runs through **one** funnel, synchronously.
There is no second nudge path.

## The gate order

**Tier 0 is deterministic and free.** In order, an event is:

1. a meeting that just ended → the *post-meeting tap* (below);
2. your own outbound → queued as a `signal` (ledger fodder, never a nudge);
3. an answered or outgoing call → dropped; a missed call from someone you know → **nudge**; from an
   unknown number → queued;
4. an OTP, shortcode, or system message → dropped silently;
5. from a muted sender or muted person → dropped;
6. inside an active snooze → queued; inside quiet hours → queued;
7. a group message that doesn't name you → queued;
8. an unknown non-VIP 1:1 → queued (never a nudge);
9. anything with no text to judge → queued.

**Tier 1 is one cheap LLM call** (`SOTTO_TRIAGE_MODEL`) on whatever survives: the event text plus a
one-line "who is this" from the knowledge graph. It returns exactly one of
`urgent` · `actionable` · `scheduling_ask` → nudge; `ambient` → queue; `ignore` → drop. **Any error
queues** — the funnel fails toward silence, never toward noise.

## The rules, one sentence each

- **Interrupts spend a daily budget** — at most `SOTTO_NUDGE_BUDGET` (default 4) event nudges per
  *local* day; beyond it, nudge-worthy events queue with class `budget`.
- **Post-meeting taps have their own cap** — at most `SOTTO_TAP_MAX_PER_DAY` (default 3) per local
  day. Taps do not spend the interrupt budget and interrupts do not spend the tap cap: **neither
  eats the other.**
- **Escalation** — the same known person, on a *second* channel, within
  `SOTTO_ESCALATION_WINDOW_MIN` (default 45 min), where at least one side is a call or a real ask →
  one nudge, exempt from both the budget and the cooldown, and **only once per window**. Identity
  matching is exact (resolved name or normalized phone/email); nothing fuzzy. A meeting-end row is
  never evidence — a meeting ending is not someone reaching you.
- **Cooldown** — at most one nudge per conversation thread per `SOTTO_EVENT_COOLDOWN_MIN`
  (default 20 min). Only escalations are exempt.
- **Quiet hours and snooze always win** — no nudges between `SOTTO_QUIET_START` and
  `SOTTO_QUIET_END` (default 21:00–07:00; a missed call from a VIP is the one carve-out), and an
  explicit "be quiet until…" snooze holds *everything*, missed calls included.
- **In-meeting hold** — while you are inside a timed calendar event with at least one other human,
  would-be nudges queue as `meeting_hold`. Solo blocks and all-day events never hold, and a calendar
  cache that is stale or from another day never holds — Sotto won't act on a stale belief about
  where you are.
- **The release valve** — every 15 min, when nothing is holding, up to 2 queued events per tick
  (`SOTTO_VALVE_MAX_PER_HOUR`, default 2/hour) that were deferred for cooldown / quiet hours /
  catch-up / budget / in-meeting and are younger than `SOTTO_VALVE_MAX_AGE_MIN` (default 4h) are
  promoted back into a nudge. It respects the same daily budget. An ask held by a *meeting* skips
  the age limit — a long meeting must not silently expire something Sotto itself held. An explicit
  snooze is deliberately *not* promotable — a snooze that lifts must not become a burst.
- **Undeliverable nudges are never spent** — when `SOTTO_CRON_DELIVER` is `whatsapp` (the default),
  the valve and the post-meeting tap wait for WhatsApp to actually be linked before dispatching; on
  any other delivery channel there is nothing to probe and they just run.
- **Reconnect grace** — a message older than `SOTTO_EVENT_MAX_AGE_MIN` (default 30 min), or anything
  in a catch-up batch after your Mac was asleep, never nudges in real time. Missed calls are exempt.

## What you get, and when

| | When | What |
|---|---|---|
| Morning brief | 6:30 local (or the moment your Mac wakes past 7am) | your day across messages, email, calendar, plus open loops |
| Evening brief | 17:30 local | accountability, tomorrow, and post-meeting follow-up drafts |
| Midday digest | 12:30 local | everything queued **since the last delivered brief** — and only if there are at least `SOTTO_DIGEST_MIN` (default 8) real signals from people you know; otherwise silent |
| Nudges | any time, subject to every rule above | one short message with a reply already drafted |

The digest window is anchored to the brief that actually *delivered* — the deliver-once claim
(`brief_marker.py`) advances the stamp when it wins — so the 12:30 digest can never repeat what the
morning brief just covered. Briefs and nudges are always **drafts**; Sotto never sends for you.

## Where to see what happened

Every verdict — nudged, queued, dropped, or promoted — is written to a ledger with its reason, and
the dashboard's **Record** view (`/app#record`) renders it. If you're asking "why didn't I get
nudged about that?", the answer is a row there: *muted sender*, *quiet hours*, *daily interrupt
budget spent (4 nudges today)*, *in a meeting until 2:30 PM — Sarah Chen*, and so on. Nothing is
silently discarded without a reason you can read.
