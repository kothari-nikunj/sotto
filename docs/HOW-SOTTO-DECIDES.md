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
There is no second nudge path, and that is now structural rather than a matter of discipline: the
proactive watcher (the ~15-min cron that notices a meeting about to start, a commitment due today, a
birthday) decides only WHAT is due, then hands its whole tick to the same `triage()` call in
process. Its nudges pass the rules below in the order below — quiet hours, the snooze, muted people,
the in-meeting hold, the daily budget, the digest queue, and the same ledger row per verdict,
including for the ones the clock suppressed — because they are the same code, not a second copy.

## Who can produce a nudge

Six things in the whole system can start a nudge. Each one is listed here with the function that
begins it and the point where it rejoins the rules below — so "where does a message become a nudge?"
is a table lookup, not a code search.

| Producer | Starts at | Rejoins the funnel at |
|---|---|---|
| **Bridge events** (iMessage · WhatsApp · calls) | `receiver.handle_events` — the `/bridge/events` endpoint | `triage_event.triage` — the whole gate order below |
| **Gmail poll** (inbound email) | `receiver._poll_gmail_once` — every `SOTTO_EMAIL_POLL_SECS` | the same `handle_events` → `triage_event.triage` path; email is not a separate lane |
| **Release valve** (something held earlier, let out now) | `receiver._valve_tick` — every 15 min (`receiver.VALVE_INTERVAL_SECS_DEFAULT`) | `triage_event.release_valve` — re-checks class, sender, age and cooldown, then spends the budget like any nudge |
| **Post-meeting tap** (a meeting just ended) | `calcache.tap_tick` → `receiver._dispatch_meeting_tap` | `triage_event.triage` as an ordinary event, classified `post_meeting` — its own daily cap, exempt from the interrupt budget |
| **Proactive watcher** (meeting prep · commitment · chase · birthday · handoff · retune offer) | `proactive_scan.main`, the `*/15` cron | `triage_event.triage` — the tick goes in as ONE bundle of synthetic `source: "proactive"` events, classified by `_classify_proactive` (snooze → quiet hours → mutes; the nudge's kind is its class) and then through every gate below. The bundle that comes back is what the watcher delivers |
| **"Nudge me now"** (you promote a held item from the dashboard) | `dashboard._post_cadence` → `receiver.run_promote` | `triage_event.promote_one` — the same `_valve_candidate` rule the valve uses, spending exactly one budget unit |

## The gate order

**Tier 0 is deterministic and free.** In order, an event is:

1. a meeting that just ended → the *post-meeting tap* (below); a nudge the watcher planned → its
   own kind (a birthday, something you're owed), held by the snooze then quiet hours then a mute,
   and then through every gate the rest of this list ends in;
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

- **Interrupts spend a daily budget** — at most `SOTTO_NUDGE_BUDGET` (default 4) nudges per *local*
  day, counting the proactive watcher's; beyond it, nudge-worthy events queue with class `budget`.
  The unit is a **message**, not an event: a batch of arrivals is one nudge and costs one unit, and
  so does the watcher's whole push.
- **The honest version of that sentence** — the budget covers *everything except three exempt
  classes, which are uncapped*: missed calls (at most one per thread per
  `SOTTO_EVENT_COOLDOWN_MIN`) and cross-channel escalations (at most one per person per
  escalation window) have no daily ceiling at all, and post-meeting taps have their own
  (3/day). So a worst-case busy day is: 4 budgeted interrupts + 3 taps + however many missed calls
  and escalations the day actually contains + the midday digest + your two briefs.
- **Post-meeting taps have their own cap** — at most `SOTTO_TAP_MAX_PER_DAY` (default 3) per local
  day. Taps do not spend the interrupt budget and interrupts do not spend the tap cap: **neither
  eats the other.**
- **Escalation** — the same known person, on a *second* channel, within a 45-minute window
  (`triage_event.ESCALATION_WINDOW_MIN_DEFAULT`), where at least one side is a call or a real ask →
  one nudge, exempt from both the budget and the cooldown, and **only once per window**. Identity
  matching is exact (resolved name or normalized phone/email); nothing fuzzy. **Only a person
  counts as evidence**: a meeting ending is not someone reaching you, neither is a nudge Sotto
  raised itself, and neither is a message *you* sent them.
- **Cooldown** — at most one nudge per conversation thread per `SOTTO_EVENT_COOLDOWN_MIN`
  (default 20 min). Only escalations are exempt.
- **Quiet hours and snooze always win** — no nudges between `SOTTO_QUIET_START` and
  `SOTTO_QUIET_END` (default 21:00–07:00; a missed call from a VIP is the one carve-out), and an
  explicit "be quiet until…" snooze holds *everything*, missed calls included. **VIP** is whoever
  you said it is — the stated list (`preferences.explicit.vip_people`, written by "make Sarah a
  VIP" in chat or the toggle on her dashboard page) is checked first, then two fallbacks: a
  top-of-queue relationship-pulse priority, or a "family" mention in their file.
- **In-meeting hold** — while you are inside a timed calendar event with at least one other human,
  would-be nudges queue as `meeting_hold`. Solo blocks and all-day events never hold, and a calendar
  cache that is stale or from another day never holds — Sotto won't act on a stale belief about
  where you are.
- **The release valve** — every 15 min, when nothing is holding, up to 2 queued events per tick
  and at most 2/hour (`triage_event.VALVE_MAX_PER_HOUR`) that were deferred for cooldown / quiet
  hours / catch-up / budget / in-meeting and are younger than 4h (`VALVE_MAX_AGE_MIN`) are
  promoted back into a nudge. It respects the same daily budget. An ask held by a *meeting* skips
  the age limit — a long meeting must not silently expire something Sotto itself held. An explicit
  snooze is deliberately *not* promotable — a snooze that lifts must not become a burst.
- **You can release one yourself** — the dashboard's Cadence page lists what is being held and
  offers *"nudge me now"* on each. That is the valve, with one entry: the same promotability rule,
  the same budget spend, the same ledger row. The two gates it skips are the clock ones (quiet
  hours and the snooze) — those exist to stop *unprompted* interruptions, and you asked for this
  one. The room you're in and the day's budget still apply.
- **Undeliverable nudges are never spent** — when `SOTTO_CRON_DELIVER` is `whatsapp` (the default),
  the valve, the post-meeting tap and the proactive watcher all wait for WhatsApp to actually be
  linked before dispatching; on any other delivery channel there is nothing to probe and they just
  run.
- **Reconnect grace** — a message older than 30 min (`triage_event.EVENT_MAX_AGE_MIN`), or anything
  in a catch-up batch after your Mac was asleep, never nudges in real time. Missed calls are exempt.
- **An open loop is a DEBT, not everything that mentions you** — something a person is waiting on
  from you, or something you promised: a specific thing owed, by or to a specific person, with a
  request or a promise behind it. A benefits-enrollment notice, a receipt reminder, a cold pitch, a
  launch announcement, a mention in a document, or someone *answering* you is news, not a debt, and
  never opens a loop. Three of those tests need no judgment and are enforced in code rather than
  asked of the model — a row with no summary, a row that names nobody, and a counterpart that is a
  no-reply/notifications/billing address (the same "is this automated?" rule the event funnel drops
  senders by). The rest — is this an ask or an FYI? — is the extraction prompt's job, because it is
  a question about what a message *means*, and a keyword rule pretending otherwise would drop real
  asks. Rows already on your volume that name nobody are closed on the next pass for the same
  reason: they can never be resolved, chased, or told apart from each other.
- **One debt per counterpart** — an open loop is keyed by *who* it is with, and who is a machine
  fact, not a label: a group ask is keyed by the group's own platform ID (iMessage's chat id,
  WhatsApp's group JID), a person's by their entry in your people graph (which is what makes a
  capture carrying only their name and one carrying their email the same debt). The **channel is not
  part of who**, so the same ask filed under iMessage one morning and WhatsApp the next is one debt;
  neither is the *word* the model reaches for — every action type outside the small closed set
  (owed-to-you, calendar, scheduling, owed-by-you) counts as owed-by-you rather than opening a
  family of its own. Rename a group, write the same ask up differently tomorrow, or answer across
  three email threads, and it is still the one open debt — never a second row. What still forks is
  DIRECTION: a reply you owe and a deliverable you're owed are two debts on one thread. The only
  thing keyed by a thread is a debt with no counterpart at all (a commitment you made to nobody in
  particular, a loop you added by hand).
- **Meetings are not debts** — a meeting prep/info action is a calendar shadow: the docket is its
  surface and the calendar closes it by passing. It never opens a loop, whichever of `meeting`,
  `meeting_prep`, `meeting_info` or `calendar` the extractor called it.
- **The brief decides, it doesn't inventory** — an open loop earns its own line in a brief only when
  it is **overdue, due within 24 hours, or already chased without an answer**. Every other open loop
  is one quiet line — how many there are and where to see them — and is worked by the nudges (the
  chase, the commitment reminder, the "nudge her again or let it go?" question, the cleanup offer),
  not by the brief. One line per person, however many things they owe you (past three, the line says
  how many more rather than dropping them). Every open loop is in exactly one of three places: named
  in the brief, printed on its own line, or inside that count — nothing falls between them. A loop
  you have **snoozed** in `/app#loops` is in none of them: hidden there is hidden everywhere, brief
  included. A brief that lists everything outstanding is a to-do list you have to triage yourself,
  which is the job.
- **"The brief already told you" is decided by identity, not by names** — a loop counts as covered
  when the brief's own tap marker carries that person's identifier, not when their name appears in a
  sentence. Two people share a first name; a line about one of them must never silence the other's
  overdue ask, and "Maya" in a paragraph is not proof that Maya Chen was told. The same fact governs
  the chase: a nudge is held back only when today's brief actually named *that* loop.
- **What you owe expires quietly; what you're owed gets chased** — an open loop you owe drops off
  after 7 silent days, but something you're *waiting on* never expires: it closes the moment they
  actually deliver (a substantive reply — a link, a file, or real text, never a bare "ok" or a
  promise to send it later), or after `SOTTO_CHASE_AFTER_DAYS` (default 3) of silence it becomes one
  short, warm chase — at most one chase nudge a day, at most 2 per item. A chase is only counted
  once it is actually delivered, so one that quiet hours or a snooze swallowed is not one of your
  two. After the second, Sotto stops and asks you plainly, by name: *"I've nudged Maya twice about
  the contract — nudge her again, or let it go?"* — **once**. That question is asked one time (it is
  stamped on the loop when it is delivered, on the same rule as the chase), and from then on the
  loop stops taking a line in every brief: you have been asked, so it waits in the count line and on
  `/app#loops` until you resolve it, drop it, or say keep waiting — which restarts its chases and
  makes it askable about again.

## What you get, and when

| | When | What |
|---|---|---|
| Morning brief | 6:30 local (or the moment your Mac wakes past 7am) | your day across messages, email, calendar, plus open loops |
| Evening brief | 17:30 local | accountability, tomorrow, post-meeting follow-up drafts, and a **What moved today** block — chases delivered, loops closed, interruptions held, people prepped, follow-ups offered, named where the record has a name. Outcomes only; nothing moved means no block, and it will never tell you how many emails it read |
| Midday digest | 12:30 local | everything queued **since the last delivered brief** — and only if there are at least `SOTTO_DIGEST_MIN` (default 8) real signals from people you know; otherwise silent. Nudges Sotto raised itself never count toward that 8 (they aren't people), though they may ride along in the message |
| Nudges | any time, subject to every rule above | one short message with a reply already drafted — and an offer to act on it: an email asks ("want this in your Gmail drafts?" — on your yes it saves a real, threaded Gmail draft you send yourself), every other channel gets a one-tap link |
| Proactive nudges | a meeting starting in ~45 min you haven't prepped, a commitment due today, one chase for something you're owed, a birthday (`SOTTO_BIRTHDAY_LEAD_DAYS`, default 3, days out and on the day — unless a brief already delivered today), a plain question about an ask nobody answered twice, an offer to tidy a heavy pile | the same thing — and the whole push spends **one** unit of the same daily budget, queues to the same digest when it's gone, waits out the same mutes and in-meeting hold, and lands in the same ledger |

The digest window is anchored to the brief that actually *delivered* — the deliver-once claim
(`brief_marker.py`) advances the stamp when it wins — so the 12:30 digest can never repeat what the
morning brief just covered. Briefs and nudges are always **drafts**; Sotto never sends for you — a
Gmail draft is the most literal version of that promise, since it sits in your own drafts folder
until you press send.

## Where to see what happened

Every verdict — nudged, queued, dropped, or promoted — is written to a ledger with its reason, and
the dashboard's **Record** view (`/app#record`) renders it. If you're asking "why didn't I get
nudged about that?", the answer is a row there: *muted sender*, *quiet hours*, *daily interrupt
budget spent (4 nudges today)*, *in a meeting until 2:30 PM — Sarah Chen*, and so on. Nothing is
silently discarded without a reason you can read.
