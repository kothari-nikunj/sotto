# How Sotto decides when to interrupt you

After the existing admission rules, a fixed procedure selects one primary notification per push,
renders a template or makes one direct structured writing call. Omitted items retain coverage.
Transient writing failures wait for queue backoff; malformed copy gets one repair. Deterministic
reminders survive writer failures. Scheduling remains a question until all-calendar availability
can be verified; today's primary-calendar gather is insufficient. No new daily spend limit is added.

In managed deployments, connecting Granola opens its source gate only after OAuth succeeds;
disconnect records explicit revocation before removing the credential. At startup, an older linked
Granola credential can repair only a missing gate row in an already matching tenant capability
file. Missing, malformed, foreign-tenant and explicitly disabled state remain closed.

| Operation | Attempts | Native Gemini output ceiling | Thinking override |
|---|---:|---:|---|
| `notification` | 2 | 4,096 | low |
| `triage` | 2 | 2,048 | low |
| `digest` | 2 | 4,096 | low |
| `memory_extract` | 2 | 16,384 | low |
| `memory_curate` | 2 | 8,192 | low |
| `brief` | 12 | 65,536 | unchanged |
| `brief_extract` | 6 | 65,536 | unchanged |
| `brief_critic` | 3 | 65,536 | unchanged |
| `brief_revise` | 3 | 65,536 | unchanged |
| `followup` | 3 | 8,192 | low |
| `meeting_prep` | 3 | 65,536 | unchanged |
| `research` | 4 | 8,192 | low |
| `web_search` | 2 | 65,536 | unchanged |
| `web_fetch` | 2 | 8,192 | unchanged |
| `deck_read` | 2 | 32,768 | unchanged |

Model-work receipts retain **90 days**. An unknown worker claim has a **900-second** lease, with at most **2** interruption replacements per operation. Artifact families use **256** lock shards. Notification context is capped at **48,000 characters**, including instructions and schema.
Selected notification copy (text plus draft and decline) is limited to **1,200 characters**.
The brief parent contains extraction/critic/revision allowances; optional evening follow-up is a
separate child. A held quality stage delivers the draft and says so in the run record, never as
though it passed. New scheduled jobs
have separate attempt budgets, but worker retries keep the same identity. Interrupted requests stay
visible as unknown spend even when replaced. See [bounded model work](BOUNDED-MODEL-WORK.md).


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
  new mail through the same endpoint. No Mac needed. Full message bodies use the shared recursive
  MIME reader, including nested plain text or HTML. A failed read stays unacknowledged for the
  next poll while successfully read messages continue; a snippet is never substituted as the full body. A smaller `in:sent` lane rides the same poll:
  your OWN outbound mail enters as a silent "signal" (queued, never a nudge, never a model call) —
  it exists so offered email drafts can be graded against what you actually sent and so replies you
  send can close loops.
- **Calendar** — a background thread refreshes today's events every `SOTTO_CALENDAR_REFRESH_SECS`
  (default 15 min). It powers the in-meeting hold, detects meetings that just ended, and **diffs
  each refresh against the last** to catch what changed about the imminent calendar: a decline, a
  last-minute invite, a moved meeting, a removed slot or cancellation (`SOTTO_CALENDAR_NUDGES=0` disables). Only
  *imminent* changes count: a move, removal or cancellation within 24 hours, a decline within 48 — and a
  NEW invite only within 4 hours (plus a 15-min grace for a meeting that just started): a next-day
  invite is ordinary scheduling, not an interrupt — the email lane already nudges a real invite
  with a draft, and tomorrow's brief covers tomorrow. Solo blocks, all-day events and internal
  standups never mint a change. Decline nudges require explicit participation by the user and
  exactly one other human: group RSVP changes and meetings merely visible on a shared calendar
  stay quiet. Room/resource attendees do not count as people in either the calendar or prep lane.
  The last complete calendar comparison and acknowledged changes survive restarts. Failed or
  partial reads cannot prove a cancellation. Calendar permission removal, an account change, or
  an unknown account identity starts a fresh comparison once the account is known; disabling
  calendar nudges advances it quietly without replay.
  Explicitly labelled context/prep notes never generate a meeting-change notification, including
  after their meeting moves. A missing entry is described as removed from the calendar; only an
  explicit cancelled status proves cancellation. A replacement event ID is joined only through
  a unique shared iCalUID in both observations for a nonrecurring event, never a title or a
  recurring occurrence. The detector supplies dated old/new times and its exact copy survives composition.

**What arrives is untrusted.** A message's text is written by whoever sent it, so nothing in the
text can steer Sotto: the deterministic gates below read only metadata (sender, channel, clock,
calendar) — words like "urgent" or "ignore your instructions" cannot buy a mute back, skip quiet
hours, or spend the budget; the one model call that does read the text is fenced to classify it as
data, never follow it; and the synthetic source names Sotto mints internally (`meeting_end`,
`calendar_change`, `proactive` — which carry gate exemptions) are re-labeled to `unknown` if an
event over the wire ever claims one.

Every event is deduped by `(source, rowid)` and then runs through **one** funnel, synchronously.
There is no second nudge path, and that is now structural rather than a matter of discipline: the
proactive watcher (the ~15-min cron that notices a meeting about to start, a commitment due today, a
birthday) decides only WHAT is due, then hands its whole tick to the same `triage()` call in
process. Its nudges pass the rules below in the order below — quiet hours, the snooze, muted people,
the in-meeting hold, the daily budget, the digest queue, and the same ledger row per verdict,
including for the ones the clock suppressed — because they are the same code, not a second copy.

## Attachments — what the brief reads, and what it only names

**An attachment Sotto can read becomes text under its email; one it can't is named, never guessed.**

Until this lane existed the gather fetched email *bodies* only, so the deck the whole thread was
about was invisible: Sotto would write "Dana sent the Q3 deck" without ever having opened the Q3
deck. Now the files on the mail it is already reading closely get converted to Markdown and rendered
under their email, and the ones it cannot open are listed by name so the brief never writes about a
document nobody read.

Four bounds, and they are the whole rule:

- **The bodies cohort only, inbox only.** Attachments are fetched for the same top emails that get a
  full body (`--bodies`, default 12) and for the inbox lane alone. An email thin enough to be
  snippet-only is not one the brief is reading closely enough to need its files, and the `in:sent`
  lane is style exhaust — converting the files you attached to your own mail would tell you what you
  already know.
- **At most 3 converted per email** (`attachments.MAX_ATTACHMENTS_PER_EMAIL`). *Every* filename is
  still listed; the cap bounds how many get read, not how many the brief knows about, because "there
  were two more files" is itself information.
- **8 MB per attachment** (`attachments.MAX_ATTACHMENT_BYTES`) — bigger ones are named without ever
  being downloaded, so one video attachment can't spend the brief's whole wall clock.
- **12,000 characters each** (`attachments.MAX_ATTACHMENT_CHARS`), truncated with a visible
  `… [truncated]` tail so the model knows it is reading an excerpt. That is a whole deck or a
  contract's operative sections — the cap bounds the pathological document, not the ordinary one.
- **A day's attachments share one 60,000-character budget**
  (`attachments.MAX_ATTACHMENT_CHARS_PER_BRIEF`), spent in email order: one document can be read
  whole, ten documents can't blow up the prompt. The document that crosses the line is truncated
  to what remains; everything after it is named with *"the brief's attachment budget is spent"* —
  told, never silently thinner.

**Conversion is local, and there is no second option.** It runs in-process through `anydoc`, whose
`ocr='reject'` default is never overridden anywhere in the lane. So a **scanned** PDF is named
*"scanned document — no local text"*, an **encrypted** one *"password-protected"*, an **oversized**
one *"too large to read"*, and an image *"image — not converted"* — named, never read, and never
sent anywhere to be made readable. There is no hosted-OCR path, no API key, and no environment
variable to turn one on; the three caps above are named constants in
`_shared/lib/attachments.py`, which owns them for both the fetch side and the prompt side.

Attachment text is **untrusted content**, exactly like a message body: it is rendered into the
brief's data section for the writing model to summarize, and it reaches no gate. Nothing inside a
PDF can spend the interrupt budget, buy a mute back, or skip quiet hours — the funnel below never
opens an attachment at all. Deciding whether to ring you is made on metadata, and this lane is the
brief's, not the funnel's.

## Who can produce a nudge

Seven things in the whole system can start a nudge. Each one is listed here with the function that
begins it and the point where it rejoins the rules below — so "where does a message become a nudge?"
is a table lookup, not a code search.

| Producer | Starts at | Rejoins the funnel at |
|---|---|---|
| **Bridge events** (iMessage · WhatsApp · calls) | `receiver.handle_events` — the `/bridge/events` endpoint | `triage_event.triage` — the whole gate order below |
| **Gmail poll** (inbound email) | `receiver._poll_gmail_once` — every `SOTTO_EMAIL_POLL_SECS` | the same `handle_events` → `triage_event.triage` path; email is not a separate lane |
| **Release valve** (something held earlier, let out now) | `receiver._valve_tick` — every 15 min (`receiver.VALVE_INTERVAL_SECS_DEFAULT`) | `triage_event.release_valve` — re-checks class, sender, age and cooldown, then spends the budget like any nudge |
| **Post-meeting tap** (a meeting just ended) | `calcache.tap_tick` → `receiver._dispatch_meeting_tap` | `triage_event.triage` as an ordinary event, classified `post_meeting` — its own daily cap, exempt from the interrupt budget |
| **Calendar diff** (a decline · a last-minute invite · a move · a cancellation of an imminent meeting) | `calcache.change_tick` — the same refresh tick as the tap | `triage_event.triage` as a `calendar_change` event — snooze, quiet hours and mutes still hold it; exempt from the budget, the in-meeting hold and the cooldown, because a change expires with the meeting it's about (per-change deduped at the source) |
| **Proactive watcher** (intention · meeting prep · commitment · chase · birthday · handoff) | `proactive_scan.main`, the `*/15` cron | `triage_event.triage` — the tick goes in as ONE bundle of synthetic `source: "proactive"` events, classified by `_classify_proactive` (snooze → quiet hours → mutes; the nudge's kind is its class) and then through every gate below. The bundle that comes back is what the watcher delivers |
| **"Nudge me now"** (you promote a held item from the dashboard) | `dashboard._post_cadence` → `receiver.run_promote` | `triage_event.promote_one` — the same `_valve_candidate` rule the valve uses; the explicit request bypasses the unsolicited budget |

An admitted pre-meeting reminder carries the known role, countdown and open item, plus at most
two short sentences about the introduction or meeting purpose when supported by existing memory,
the invitation or recent correspondence. The selected attendee reuses up to five dated excerpts
from the existing consented mail snapshot/queue within the shared seven-day conversation window;
no fresh mail fetch or research runs for the offer. Exact sender/recipient addresses select this
background, while only an explicit thread binding establishes this invitation's provenance.
The existing notification writer and artifact cache own composition. Missing evidence, source
failures and held/failed model work retain the basic reminder and its named prep offer.
Gmail consent is checked before and after the read, after
composition and at delivery. Accepting that offer runs the focused meeting prep, presented as four
photos on iMessage when it fits; the initial reminder is text.

Automatic meeting prep and background attendee research require an external work email. Personal
mailboxes are excluded, including when the owner uses a personal mailbox; explicit prep requests
remain available for anyone. Prep names come from exact-email contacts, calendar names or the
existing research cache, never a prettified email handle. A missing name can also use an unambiguous
display name paired with that exact email in a cached From/To/Cc header; body mentions and names
attached to other addresses cannot name them. That source permission is checked again at delivery. Missing names use the meeting title.

Notifications based on messages at least an hour old carry their source time: a local clock time
for today, otherwise the original month/day (and year when different). Bridge chat wall times
stay in the user's configured zone; queue arrival never resets the date. The writer gets the
original timestamp and current local date so an old "today" is not
reinterpreted as today. Signature reminders are attributed requests, not proof a document remains
unsigned; explicit user completion closes the matching obligation through the existing ledger edit.

Before notification composition, held asks and due commitments get current thread and calendar
context. The writer omits scheduling questions already settled by a matching invitation or later
confirmation; a meeting alone does not complete a document promise. Exact source-message evidence
also suppresses an ask whose matching ledger obligations are all terminal, both before writing
and again at delivery. A new message on the same thread is independent. These checks withhold
stale notifications without marking obligations complete by fuzzy calendar or person matches.

For managed owner chat, a bare text-only "download this" with no URL, attachment or reply target asks for the link before
starting model work. A new URL is the current document target. The DocSend reader accepts branded
DocSend hosts and preserves the host through the email gate and page reads. A verification gate
says that the cloud reader cannot use the Mac browser session, and asks for a PDF or a link without
verification rather than promising browser sign-in will unlock cloud access.

The watcher suppresses an imminent prep only after provider acceptance records the exact calendar
event ID and start. Research is reusable context, not evidence the user received an offer or prep;
moving the meeting creates a new occurrence and makes it eligible again.

## The gate order

**Tier 0 is deterministic and free.** In order, an event is:

1. a meeting that just ended → the *post-meeting tap* (below); a nudge the watcher planned → its
   own kind (a birthday, something you're owed), held by the snooze then quiet hours then a mute,
   and then through every gate the rest of this list ends in; a *calendar change* (a decline, a
   last-minute invite, a move, a cancellation of an imminent meeting) → **nudge**, held only by
   the snooze, quiet hours, and a muted person;
2. your own outbound → queued as a `signal` (ledger fodder, never a nudge);
3. from a muted sender or muted person → dropped, on every channel **and before any per-channel
   branch**, so a muted person's missed call drops exactly like their messages (until Sep 2026 this
   gate sat after the calls branch, and a muted person's call still nudged — budget-exempt, and
   VIP-exempt from quiet hours): the funnel's ingress gate, the release valve, a tapped
   "nudge me now" and the midday digest all ask one predicate
   (`preferences.proactively_muted`), so "stop surfacing X" can never drop yesterday's queued
   message and still let today's ring through. A muted phone number matches however either side
   writes it — "+1 202 555 0171", "(202) 555-0171", "12025550171", or the number inside a WhatsApp
   identifier — because both go through the same normalizer the rest of Sotto threads by; the
   number next to it (…0170) is a different person and still reaches you;
4. an answered or outgoing call → dropped; a missed call from someone you know → **nudge**; from an
   unknown number → queued;
5. an OTP, shortcode, or system message → dropped silently;
6. a formal calendar invite or RSVP notification arriving as **email** → queued: accepting an
   invite is a calendar action, not an email draft, and meeting interrupts belong to the calendar
   lane — a brand-new invite nudges there only when the meeting starts within 4 hours;
7. inside an active snooze → queued; inside quiet hours → queued;
8. a group message that doesn't name you → queued;
9. an unknown non-VIP 1:1 → queued (never a nudge). **Known, on every channel,** means your
   Contacts resolved a real name **or the knowledge graph has a person file for the sender** — the
   graph fallback exists because a thin contacts snapshot must not turn everyone who texts you
   into a stranger, and a graph hit also supplies the name the nudge uses;
10. anything with no text to judge → queued.

**Tier 1 is one cheap LLM call** (`SOTTO_TRIAGE_MODEL`) on whatever survives: the event text plus a
one-line "who is this" from the knowledge graph, and — for email — whether you were To'd or merely
Cc'd. It returns exactly one of
`urgent` → nudge; `actionable` · `scheduling_ask` → queued under that class, where only the release
valve (below) can turn one into a nudge and the midday digest reviews whatever it left; `ambient` →
queue; `ignore` → drop. **Any error queues** — the funnel fails toward silence, never toward noise. One taught judgment worth naming:
**a reply on an intro you made, where the two people you introduced are now coordinating with each
other, is ambient** — once both sides are talking your job is done, and a courtesy "leaving you two
to connect!" never earns an interrupt.

## The rules, one sentence each

- **Interrupts spend a daily budget** — at most `SOTTO_NUDGE_BUDGET` (default 4) nudges per *local*
  day, counting the proactive watcher's; beyond it, nudge-worthy events queue with class `budget`.
  The unit is a **message**, not an event: a batch of arrivals is one nudge and costs one unit, and
  so does the watcher's whole push.
- **The honest version of that sentence** — the budget covers *everything except five exempt
  classes, which are uncapped*: missed calls (at most one per thread per
  `SOTTO_EVENT_COOLDOWN_MIN`) and cross-channel escalations (at most one per person per
  escalation window) have no daily ceiling at all, post-meeting taps have their own
  (3/day), and calendar changes and imminent meeting prep are bounded only by your calendar — each
  distinct change nudges once, ever, and each meeting is prepped once, because both expire with the
  meeting they are about and are deduped per key where they are produced. So a worst-case busy day
  is: 4 budgeted interrupts + 3 taps + however many missed calls, escalations, calendar changes and
  imminent preps the day actually contains + the midday digest + your two briefs.
- **“No nudges” is stronger than the normal exemption set** — an explicit effective cap of zero
  stops every unsolicited nudge at delivery, including missed calls, escalations, taps, calendar
  changes and meeting prep. Briefs, digests, direct requests and an item you explicitly promote
  remain available.
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
  VIP" in chat or the toggle on her dashboard page) is checked first, then one fallback: a typed
  `family_of` relation in their file. Both are a choice, yours or the graph's; **message volume is
  not one.** A top-of-queue relationship-pulse priority used to qualify too — and since that
  priority is interactions × days-waiting × type-weight, four messages and three days of silence
  bought a 3 a.m. interrupt, off a file up to a week stale. It was removed (Sep 2026); the pulse
  still reaches Tier 1 as the sender one-liner's relationship signal, which is context, not a gate.
- **In-meeting hold** — while you are inside a timed calendar event with at least one other human,
  would-be nudges queue as `meeting_hold` — except missed calls, escalations, calendar changes
  and imminent meeting prep. Prep still obeys quiet hours, snooze, mutes and cooldown, but not the
  daily interrupt budget — it can arrive during the previous meeting so back-to-back prep does not
  expire before reaching you, and a spent budget must not silence "your pitch starts in 20 minutes"
  either: the valve never promotes a nudge Sotto planned, so a prep demoted to `budget` had no way
  back at all. Solo blocks and all-day events never hold, and a calendar
  cache that is stale or from another day never holds — Sotto won't act on a stale belief about
  where you are.
- **The release valve** — every 15 min, when nothing is holding, up to 2 queued events per tick
  and at most 2/hour (`triage_event.VALVE_MAX_PER_HOUR`) that were deferred for cooldown / quiet
  hours / catch-up / budget / in-meeting and are younger than 4h (`VALVE_MAX_AGE_MIN`) are
  promoted back into a nudge. It respects the same daily budget. An ask held by a *meeting* skips
  the age limit — a long meeting must not silently expire something Sotto itself held. An explicit
  snooze is deliberately *not* promotable — a snooze that lifts must not become a burst. Ordinary
  `actionable` and `scheduling_ask` judgments enter catch-up and are therefore valve-promotable.
  When the same relevance judgment finds an evidenced deadline, the ask waits until it is within
  24 hours. A scheduling ask closes when its invitation time passes; an overdue actionable task
  remains eligible until answered, under the ordinary age, cooldown and budget bounds.
- **You can release one yourself** — the dashboard's Cadence page lists what is being held and
  offers *"nudge me now"* on each. That is the valve, with one entry: the same promotability rule,
  the same ledger row. It skips the unsolicited budget and the clock gates (quiet
  hours and the snooze) — those exist to stop *unprompted* interruptions, and you asked for this
  one. The room you're in still applies.
- **Undeliverable nudges are never spent** — the valve, the post-meeting tap and the proactive
  watcher all wait for the ACTIVE channel (`SOTTO_CRON_DELIVER`, `telegram` by default) to actually
  be linked before dispatching: WhatsApp's session creds on the volume, Telegram's captured chat id.
  A channel this deploy cannot probe holds nothing back — nobody is denied a nudge for a setup we
  can't see.
- **Reconnect grace** — a message older than 30 min (`triage_event.EVENT_MAX_AGE_MIN`), or anything
  in a catch-up batch after your Mac was asleep, never nudges in real time. Missed calls keep a
  longer leash, not a free pass: a missed call buzzes up to 4 hours after the ring
  (`MISSED_CALL_MAX_AGE_MIN` — the same clock as the valve's age cap, so a held nudge and a stale
  call age out together); past that it's told in the digest or the next brief, never rung as
  "just called" the morning after.
- **An open loop is a DEBT, not everything that mentions you** — something a person is waiting on
  from you, or something you promised: a specific thing owed, by or to a specific person, with a
  request or a promise behind it. A benefits-enrollment notice, a receipt reminder, a cold pitch, a
  launch announcement, a mention in a document, or someone *answering* you is news, not a debt, and
  never opens a loop. Three of those tests need no judgment and are enforced in code rather than
  asked of the model — a row with no summary, a row that names nobody, and a counterpart that is a
  no-reply/notify/notifications/newsletter/billing address (the same "is this automated?" rule the
  event funnel drops senders by — a mailbox named for what it emits is nobody). The rest — is this an ask or an FYI? — is the extraction prompt's job, because it is
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
- **Meeting promises are captured once after learning, not announced on intent** — ancillary Learn
  checks at most three changed meetings from a fourteen-day overlap, records only
  grounded obligations, and checkpoints a meeting revision only after the canonical writer returns.
  A successful transcript extraction also checkpoints the same meeting's no-transcript alias, so
  the short-lived transcript aging out reuses the saved result while transcript additions or edits
  remain new evidence. The bounded
  pass gets nine minutes inside the background worker's ten-minute process ceiling and caches each
  completed meeting before moving on, so a later worker retry does not repay successful work. Delayed notes therefore
  retry without re-reading unchanged notes. Obligation identity preserves action, counterpart and deliverable qualifiers, including weekday
  and relative-time words; a Monday report cannot fold into a Tuesday report. A promise without one unambiguous counterpart is left
  out rather than becoming an identifier-less debt. New captured promises close only when a later source message both names the promised
  deliverable, including its qualifiers, and affirms the matching action; thanks, questions,
  future plans, negation and counterfactuals leave it open. Ambiguous shorthand also stays open
  for correction rather than guessing completion. An explicit user lock always remains manual. Meeting prep
  carries at most three relevant open obligations per attendee, including undated work in either
  direction.
- **Invitation email linkage is exact or unknown** — when a calendar provider supplies an actual
  Gmail thread identifier, that thread may explain the invitation. Otherwise bounded mail with the
  attendee is labeled recent background and cannot prove who introduced the meeting or why it exists.
- **The brief decides, it doesn't inventory** — an open loop earns its own line in a brief only when
  it is **overdue, due within 24 hours, or already chased without an answer**. Every other open loop
  is one quiet line — how many there are and where to see them — and is worked by the nudges (the
  chase, the commitment reminder, and the "nudge her again or let it go?" question),
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
  the chase, with one exception: only a **first** chase (nothing chased yet on that loop) is held
  back when today's brief already named it — a loop chased once is urgent by contract and the brief
  names it every day afterwards as a still-open line, so treating that as "already told you" a
  second time would mean the loop never reached its two chases or the hand-off question.
- **An email you sent that nobody answered is a debt owed to you** — the gather asks Gmail, for
  each thread you wrote on 3 to 14 days ago, whether the last word is still yours (20 threads at
  most, the same 3-day clock the chase uses), and the composer mints a `waiting_on` for each one
  that went to somebody in your contacts or your graph. Never for a stranger, a no-reply address,
  an intro you made for two other people, a two-word thanks, or a calendar RSVP your Gmail sent to
  an organizer ("Accepted: …", "Updated invitation: …"). The row is dated the day you wrote,
  so the first chase ripens on the silence that already happened.
- **They replied, they just haven't delivered** — any inbound from the counterpart of something
  you're owed (a text, an email on the thread, a call — answered or missed) restarts that loop's
  chase clock without counting a chase: to the date they named ("by Friday", "the 16th of July",
  "tomorrow", "end of week"), else the usual 3 days. Only substance closes the loop.
- **A meeting you declined is not on your day; a meeting you haven't answered is an ask** — the
  gather reads your own answer to each invite. A declined event never reaches the brief, a research
  call, the prep nudge, or the in-meeting hold. An unanswered invite with somebody else on it
  becomes an `rsvp` ask once it is within 48 hours — minted by code, and closed by the calendar
  (you answer, or it passes), never a ledger row.
- **Contact is not completion.** The brief model may propose a `loopUpdates` change only for one
  ledger ID and revision, backed by an original source message ID and verbatim quote. Code verifies
  the counterpart, direction, original timestamp and quote against the snapshot. A generic reply,
  call, calendar attendee, unrelated link, or appearance in Already Handled cannot close a debt.
  Missing or ambiguous evidence leaves it open; explicit user corrections remain authoritative.
- **A loop you closed by hand stays closed** — when tomorrow's extraction reads the same thread
  again, a loop Sotto itself expired or auto-resolved re-opens (the person coming back is evidence
  the guess was wrong), but one you dismissed or resolved yourself needs new incoming request evidence after your closure.
  A new extraction timestamp or a paraphrase is insufficient. Evidence must bind a newer inbound
  message from that person or group in the source snapshot. iMessage and WhatsApp prompts carry
  per-message references, derived from the original message fields when Bridge omits native IDs;
  these references are never reply addresses. Email can also bind a new thread. Legacy date-only closures cover the whole
  day; new user closures record the instant. **Never tell you twice** is then measured, not
  requested: a person Sotto already nudged you about today (a delivered nudge, correlated by id to
  the channel's receipt) never opens the model's draft and takes **at most 2 lines** in it — one
  status line, plus a Coming Up mention — and the validator hands a violation to the critic's
  revise pass, which a violation forces to run even on a quiet brief the critic would otherwise
  skip (the lines the composer itself appends afterwards, the still-open backstop and the
  receipts, are read from the record, not written by the model, and are not counted).
- **Obligations never expire from age, a passed deadline, or an unreachable counterpart.** Something
  you're *waiting on* closes only when they
  actually deliver (a substantive reply — a link, a file, or real text, never a bare "ok" or a
  promise to send it later), or after `SOTTO_CHASE_AFTER_DAYS` (default 3) of silence it becomes one
  short, warm chase — at most one chase nudge a day, at most 2 per item. A chase is only counted
  once it is actually delivered, so one that quiet hours or a snooze swallowed is not one of your
  two — and a chase that was stamped but never delivered sends that loop to the back of the next
  day's pick (`chase_stalls`), so one perpetually-blocked loop cannot hold the whole chase lane.
  After the second, Sotto stops and asks you plainly, by name: *"I've nudged Maya twice about
  the contract — nudge her again, or let it go?"* — **once**, and **on its own clock**: the question
  is asked the first watcher tick it comes due (outside the two hours after a brief, which carried
  the loop). That question is asked one time (it is
  stamped on the loop when it is delivered, on the same rule as the chase), and from then on the
  loop stops taking a line in every brief: you have been asked, so it waits in the count line and on
  `/app#loops` until you resolve it, drop it, or say keep waiting — which restarts its chases and
  makes it askable about again. Carry-forward and paraphrases reuse the existing loop ID. By
  default, counterpart plus direction preserves that task across prose changes; when observed
  request evidence shows a genuinely second task, the model must say `newObligation: true` and
  code mints a stable source-and-ordinal origin. If extraction omits `loopId`, evidence matching exactly one existing task recovers that row. Shared evidence cannot choose a sibling, and a different message alone does not create a new obligation. Legacy aliases still fold rows sharing their old
  base, while independently minted task, custom and synthetic keys stay separate. Briefs group
  open work into one readable entry per person without using display grouping as task identity.
  The resolver's two-day deadline grace is compatibility for legacy meeting shadows that lack a
  meeting time; it does not expire or resolve ordinary obligations.
- **Parked, never deleted.** Something you owe that nothing has touched for **14 days** parks only
  after its warning is accepted for delivery on an earlier local day,
  unless its deadline is still ahead, or you confirmed the commitment yourself (`explicit`: only
  you change its state). A touch is a brief re-capture, a *keep*, or the scheduled end of a snooze.
  Keep and snooze preserve the original creation date used to verify completion evidence.
  A parked loop keeps its file and history, leaves the brief, the count line and the chase clock,
  and comes back on re-capture, keep or snooze without new-request evidence. A verified completion
  can still resolve it. **What moved today** names every warned task with "parks tomorrow unless
  you say *keep*." Failed, skipped or muted warnings cannot hide a task; upgrades with old rows
  and newly overdue tasks wait for the same accepted warning. Re-capture, keep, snooze or a task
  correction invalidates an old warning. Person, sender and `what_moved_today` section mutes apply.
  `/app#loops` lists the parked group with a *keep*
  button, and Friday's review counts them. What you're *waiting on* never parks: it is chased,
  then handed back to you by name. Nothing is ever deleted by age.

## Managed Cloud pilot activation

In managed mode, scheduled morning/evening briefs and Bridge wake-triggered briefs wait until the owner has texted Sotto and at least one consented context source is connected. A sleeping Mac and a source with zero events are not disconnected sources. Missing capability state means zero sources. After the first owner DM, the receiver delivers one `status:no-sources` notice through the nudge outbox and remembers its acceptance on disk; no daily quiet-day or sources-unavailable brief is generated. Manual run-now remains an explicit action. Self-host keeps its current behavior.

The managed model credential is a lease: the receiver's heartbeat renews it once it is within 72 hours of expiry (`model_lease.RENEW_BEFORE_SECONDS`) and, after a failed attempt, retries hourly (`model_lease.RETRY_SECONDS`); a failed renewal never invalidates the current credential.

## Shared relevance — before choosing how to surface it

One rule governs briefs, digests and nudges: surface an item only when evidence shows a concrete
action, decision, preparation need, or meaningful development for this user. The sole policy is
`sotto-chief-of-staff/_shared/references/relevance.md`; the brief and its critic load it, event
triage and digest review use it through `relevance.py`, and the nudge skills read it before composing.
A write-up delivered for a requested review or ongoing evaluation can require action without a
question mark. An unsolicited promise to send a pitch does not establish user interest. Completed
or declined reviews stay closed. A VIP label or a sender's deadline does not establish relevance.
Proactive surfaces are primarily about people. Proactive use is suppressed only when a structured
source field marks the sender as an assistant or the relevance judgment classifies the sender role
as assistant; a business-messaging transport address, such as the RCS `*_agent@rbm.goog` handle a
bank's fraud alert and a vendor's bot both arrive on, is not that evidence. That judgment labels
every other sender as a person, assistant, or unknown from conversation evidence and excludes
software assistants even when their outreach sounds actionable; a vendor you never want to hear
from is an ordinary mute. Display names are never a blacklist: a human named Poke with an
unanswered invitation remains eligible, as does an automated school or medical relay of a real
human obligation unless the evidence identifies the sender itself as an assistant. The source
conversation remains available to an explicit chat request. Later answers and completion evidence
can remove human obligations. Explicit consent, mutes and delivery gates still govern what may be
considered.

The digest's existing eight-event activity threshold now buys a review, not a promise to send, and
one queued actionable, scheduling or urgent item buys that review on its own, below the threshold.
One bounded native-model call, given the current local clock, reviews at most 100 conversations with their latest 20 messages
(up to 400 characters each — `personal_context.CONVERSATION_TEXT_CHARS`, the one cap every
conversation rendering shares), including outgoing answers, before the six-item delivery cap.
Within an equal relevance band, an evidenced deadline wins, then a current match against the
owner's optional priority set (at most three stable IDs at one revision). An edited priority set
invalidates old matches; priority never manufactures urgency. The brief remains the backstop
beyond the digest bound. Review can retain fewer items or none. Invalid/incomplete judgment or
provider failure stays silent. `events/digest_accepted.json` records a conversation version only
after successful silent review or accepted delivery. Items outside review/output bounds remain
eligible, and a failed send consumes nothing; no extra model route, per-item call or schedule is introduced.

## What you get, and when

| | When | What |
|---|---|---|
| Morning brief | 6:30 local (or the moment your Mac wakes past 7am) | opens with the time of day and the date in your zone ("Good morning — Saturday, September 6"; good afternoon when the wake path composes it after noon), then your day across messages, email, calendar, plus open loops. One per day, always: if your Mac slept through 6:30 the brief still goes out from the last saved snapshot, and when the Mac wakes later its fresh data is folded into that snapshot instead of composing a second brief — the nudges and the midday digest surface whatever the morning brief couldn't see |
| Evening brief | 17:30 local | opens with "Good evening — <the date>", then accountability, tomorrow, follow-up drafts, and **What moved today**. On Friday only, it adds a short review of up to three stale or repeatedly surfaced obligations plus a count/link for the rest and a count of parked loops. No stale or parked items and no eligible candidate means no review; a stated `weekly_review` section mute suppresses it. It never silently expires them and sends no separate Sunday review message. **At most one review question**, shared with a *"make that a standing rule?"* confirmation when today's transcripts showed you stating one; that confirmation takes precedence. Explicit "mute Bob" instructions still work |
| Midday digest | 12:30 local | reviews what queued **since the last delivered brief** when at least `SOTTO_DIGEST_MIN` (default 8) known-sender ambient/deferred events justify review; sends only relevant items, otherwise silent. Nudges Sotto raised itself never count toward that 8 (they aren't people), though they may ride along in the message |
| Nudges | any time, subject to every rule above | one short message with a reply already drafted — and an offer to act on it: an email asks ("want this in your Gmail drafts?" — on your yes it saves a real, threaded Gmail draft you send yourself; a `mailto:` is removed at the delivery seam, so one can never reach you from a scheduled run), every other channel gets a one-tap link — and a tap link is kept only when its recipient validates (a real phone of 7-15 digits, a Messages short code, or an Apple ID email), so a model-masked or non-recipient link (`imessage://+141****3682`, an RCS business handle, a group identifier) is removed at that same seam while the draft text around it stays, because a dead link is worse than none |
| Proactive nudges | a meeting starting in ~45 min you haven't prepped, a commitment due today, one chase for something you're owed, a birthday, or a question about an ask nobody answered twice | the whole push spends **one** unit of the daily interrupt budget and obeys the shared mutes, quiet hours, snooze and effective nudge cap. “Fewer nudges” halves the cap, “no nudges” sets zero for every unsolicited path, and “more nudges” restores the configured ceiling. Direct requests, briefs and digests remain available |
| Weekly pulse | Mondays 9:00 local | who is waiting on you and who is going quiet, from six weeks of messages, calls **and email** (a mail you sent is a touch to each recipient, a mail you received a touch from its sender — for people in your Contacts or graph only), with the people you've fully lost touch with ranked below |

The digest window advances after provider acceptance, to the brief's original source cutoff.
A delayed send therefore leaves later messages eligible for catch-up. The daily marker only chooses
which run may send; it does not claim that its content was accepted. A successful silent digest
closes the window it reviewed, while a failed relevance review preserves it for retry.

The Learn step remains **one command** (`learn_step.py`) with two execution phases and one
`briefs/<date>.<kind>.learned.json` receipt. Required knowledge and continuity writes precede delivery.
Draft outcomes, voice learning, meeting-note learning and Contacts synchronization run as a durable
background follow-up; their failure is visible in the receipt and cannot suppress the brief.
Unattended briefs and nudges propose actions. Sending email or changing a calendar still requires
the separate action authorization gate; a suggestion is never approval.
The send seam also strips machine markers (`<!--id:…-->` / `<!--meeting:…-->`) from every outgoing
message: they are dashboard-and-tap-link plumbing the composer keeps in its archive artifact, and
no run's choice of what to print can leak them to a phone.

**A brief is sent once: the send seam itself claims the deliver-once marker, so a run that forgets
its claim can no longer double-deliver; the second copy is receipted `superseded`, never sent.** Two
paths compose your morning and evening brief — the schedule and the Mac's wake-push — and the day
belongs to whichever of them gets to the marker first. **Both are the same lane now:** the schedule
lives where it always did (`crons.json`), but the receiver fires it on its own clock rather than the
agent's, so a scheduled brief is written down before the first send attempt and retried like
everything else — the 6:30 brief no longer disappears because the channel was down at 6:30. A
scheduled run that dies mid-compose is retried by the durable work queue, with at most three
execution attempts. A saved completed output is reused for at most three handoff attempts without
another model call. Graceful stops refund the active phase; ambiguous crashes consume an attempt. Incoming-event
notification writing has at most four additional provider recoveries for explicit HTTP 429, 500,
502, 503 or 504 responses. These refusals keep their model attempt receipts but do not consume
notification validation attempts. Recovery waits 1, 2, 4, then 8 minutes; a further refusal stops.
The original relevance deadline still wins, and each retry rechecks current eligibility. Unknown
transport outcomes, authentication failures and delivery handoffs retain their ordinary limits.
The final failure receipt says that no retry is queued.
That marker used to be claimed only because
the skill was told to; on August 30 a run wasn't listening and the evening brief arrived twice, so
the claim now lives in the machinery every message passes through rather than in an instruction.
And it is claimed **at delivery, not before**: a run that claimed early and then died before its
words reached the outbox used to leave the day marked delivered with nothing queued to deliver it —
so on the receiver path in both hosting modes only the send seam writes the marker.
The wake-push folds fresh data into the snapshot when the matching scheduled run is still
composing within **10 minutes** of a brief's scheduled time. A dead run is not assumed to be working.
Stable job identity also prevents a repeated wake or receiver restart from buying the same work.
Normal preparation begins ten minutes early; composition becomes runnable two minutes before the
declared time, then the outbox holds an early result until that time.
Missed daily jobs can be admitted within four hours, subject to activation and source policy.
Preparation is reusable for 30 minutes, cached meeting notes for 24 hours, and a welcome run gives
its optional seeders 20 seconds. Welcome admission uses a 20-minute lease, retries after 30 minutes,
and stops after three attempts per UTC day; ancillary follow-up work remains recoverable for seven days.

Infrastructure lifecycle notices (gateway shutdown, restart, startup and interrupted native cron)
stay in operator logs across Sotto channels. They do not become chat interruptions; ordinary
replies and actionable source/provider failures keep their existing behavior. Mid-run corrections
still reach the active task, without a separate redirected, steered, queued or interrupting notice.
This uses Hermes' existing busy-acknowledgment setting in both Cloud and self-host. Displayed
reasoning, automatic memory-update notices, runtime footers and routine compression progress are
also disabled, including existing per-channel display overrides. This changes presentation only:
memory work, compression, approval policy and actionable failure reporting remain. Shared writing
rules describe the outcome and next useful step in ordinary language; technical detail is for
a user who asks for it.

Every message is persisted **before** its first send attempt. The adapter requires structured
success and a provider message ID; an exit code alone cannot mark delivery. The outbox retries every
minute, backing off 1 → 2 → 4 minutes and doubling to a fifteen-minute cap. It rechecks source
permission and relevant loop/Calendar state before sending. Invalidated brief replacement is itself
retryable, so a temporary enqueue failure cannot lose the day's brief. A provider acceptance followed
by a process crash can still cause a duplicate if the provider lacks an idempotency contract.

Waiting is not forever, and how long depends on what it is:

| | Waits until | Then |
|---|---|---|
| An ordinary real-time nudge | 30 minutes after the source event | it is dropped rather than delivered as fresh news after it stopped being fresh |
| An ordinary event waiting in the release valve | 240 minutes after the source event | the valve records one stale drop; the event can still ride the digest or a brief |
| A nudge held for a meeting | the meeting can run as long as it runs; after release it gets 240 minutes | it is dropped after that post-release window, so Sotto's own hold cannot consume its chance to arrive |
| A missed call | 240 minutes after the ring | it stays truthful about when the call happened or falls back to the digest/brief |
| A calendar change | the affected meeting's start | it is dropped once the change can no longer help before that meeting |
| A morning or evening brief | the end of its local day | it **fails visibly** — a day with no brief is something you should be told about |
| The weekly pulse (and the midday digest) with no channel to land on | four hours after its minute — the same catch-up window a brief gets, retried every tick | it is not sent; a channel that stays unlinked past the window loses that run, and the boot log says the channel was never linked |
| The midday digest | the end of its local day, which ends before the next digest window opens | it is dropped: tomorrow's digest covers what it would have said |

Nothing is thrown away quietly: every failure and every expiry is a row in the Record with its
reason, and the Briefs page carries a line whenever anything is still waiting or has given up. That
includes the quietest expiry of all — an ordinary ask the release valve lets age past its promotion
window leaves one `drop` row of class `stale` naming how old it got, written once (the queue entry
is marked, so a tick every 15 minutes cannot write it again) while the item itself still rides the
digest and your briefs.

### What Sotto did on your behalf — the receipt

Delivery is about messages Sotto sends *you*. A send, a reply, an invite, an RSVP or a deleted event
is different: it reaches someone else, and it should leave proof you can check rather than a promise
you have to take. One sentence each:

- **Every real effect leaves a receipt**, allowed or refused, in `events/sends.jsonl` — the verb, who
  it was aimed at, whether the run was unattended, and how it ended.
- **The receipt names the action without keeping its content**: it carries `payload_sha256`, the hash
  of one canonical action payload containing the verb's complete target and content (for example,
  Gmail recipient, subject and body), so the effect can be checked against what you were shown while
  the ledger holds not one readable word of it.
- **A scheduled run cannot send mail or touch your calendar at all** — the refusal is in the code,
  before anything reaches Google, and it is recorded like any other attempt.
- **When Sotto asked in one place and you answered in another, your yes is bound to the exact fresh
  offer ID, verb, target and content**: the acting verb recomputes the full-action hash and refuses
  on a mismatch. It atomically consumes the selected offer before the provider call; after that,
  success, failure, timeout or crash all require fresh approval because the outcome may be uncertain.
- **In the same conversation, that binding is a record rather than a wall** — when you say yes and
  Sotto acts on the spot, Sotto computes both halves, so the hash lets you *prove* afterwards what
  was sent; it does not pretend to prevent it. Saying so is the point: an overclaimed guarantee is
  worse than a stated gap.

## Retention — what ages out, and when

Sotto's volume used to only ever grow: `forget.py` deleted what you asked it to, and everything you
never asked about stayed forever. Since Aug 2026 a **daily sweep at 3:30 AM local**, on the same
receiver clock that fires your briefs, ages out the exhaust. It is machinery, not an instruction —
the same reason the deliver-once claim moved into the send seam.

The same 3:30 AM slot also **archives the chat session** (the boot path's own silent
`hermes sessions archive` — nothing is broadcast, `/resume` reopens the transcript). One sentence:
a gateway session lasts a day or a deploy, whichever comes first. Why: sessions reset only on
deploy since Aug 26, and a week's transcript of delivered briefs and nudges is material the chat
model will copy back into an unrelated reply — a persona rule against it lost to a transcript
carrying the same authority (Sep 1–2). A same-day copy is still possible; a week-old one is not.

One sentence per family:

| What | Kept | Why that long |
|---|---|---|
| What Sotto **sent** on your behalf (`events/sends.jsonl`) | **180 days** | the authorization trail for outbound acts, kept twice as long as anything else on purpose |
| Delivery receipts, triage verdicts, dashboard writes | **90 days** | "did it land?" and "why wasn't I nudged?" are questions about last week, not last year |
| Drafts Sotto offered you | **30 days** | matched to what you sent within a day; a month is learning, longer would be an archive of things you didn't say |
| Delivered briefs, their per-day markers, and each one's Learn receipt (`learned.json`) | **60 days** | the same clock as the snapshot each was built from |
| Staged payloads and a crashed run's leftovers | **7 days** | read by the run they were staged for; a week collects the ones whose run died |
| The nudge-dedup stamps | **30 days** | only today's is ever read |
| What you did with each draft (`outcomes.jsonl`) | **90 days** | the learning loop re-reads the whole file after every brief; a quarter is all it can use |
| The brief log | **last 5 MB** | truncated in place, because a running brief holds it open |

**Nothing in your memory is ever auto-deleted.** The graph (people, companies, `master.md`), the
continuity ledger, your style profile (self-capped by its writer — the drafts-you-shipped bucket
keeps its newest 20 per register), your preferences, the golden corpus, your credentials and any
deck you asked Sotto to read are never a sweep target — the guard is checked per path, below every
rule, so a future mistake in the table still cannot reach them. Deleting *that* is a decision you
make about your own graph, which is why it belongs to the graph's editor and to `forget.py`, not to
a daemon. A missed sweep costs nothing: every rule is stated as an age, so the next day's sweep
removes exactly what the missed one would have.

## Where to see what happened

Every verdict — nudged, queued, dropped, or promoted — is written to a ledger with its reason, and
the dashboard's **Record** view (`/app#record`) renders it. If you're asking "why didn't I get
nudged about that?", the answer is a row there: *muted sender*, *quiet hours*, *daily interrupt
budget spent (4 nudges today)*, *in a meeting until 2:30 PM — Sarah Chen*, and so on. Nothing is
silently discarded without a reason you can read. Provider acceptance is labelled Sent, not proof
that a device displayed it. Photo briefs record whether four photos were sent or why they used text;
that explanation comes from the existing outbox receipt and survives its retries. Expandable Activity details connect a send to its recorded prior decisions by identity; missing or later evidence is never guessed. The Mac menu's
View activity opens this same record. Full Disk Access on the Mac stays Checking after a permission
restart until the Bridge confirms access; source read failures stay visible independently of the
app's permission check.

A mute is not an exception to that: when the valve would have promoted a held item and only the
mute stops it, the ledger gets a *dropped — muted* row like the ingress drop does, and tapping
"nudge me now" on a muted person's item answers with the mute by name ("you asked Sotto to stop
surfacing Sarah Chen") instead of a generic refusal.

Preferences come from explicit instructions. Draft usage does not relax approval tiers or produce
mute suggestions; the unused behavioral learner has been removed. Draft matching still records
outcomes and confirms verbatim voice samples in the Learn step.

System jobs use the receiver in Cloud and receiver-based self-host. The weekly relationship pulse runs at 9 a.m. Monday in the user’s timezone. Managed scheduled jobs additionally require messaging activation and a connected context source. Both modes share work recovery, the outbox and silence handling.

For the managed personal pilot, successful Google consent opens only the Gmail/Calendar source capabilities actually granted; missing or declined permissions leave the scheduled-brief gate closed when no other source is connected.

A Bridge with a local Cloud policy reads only its consented sources and excludes the Sotto iMessage handle before transferring history, unread messages or events. Contacts follow their source consent; source access does not authorize outbound actions.

Local sources follow these rules:

- Both setup/prewarm and recurring Contacts/style writers recheck contributing source consent before writing learned data.
- Unknown style channels are rejected; legacy channel-less messages require iMessage consent and email aliases require Gmail consent.
- A disable reported by a Bridge probe takes effect on arrival whatever either clock says, only a strictly later probe restores access, and a completed read never does.
- An unreadable consent receipt withholds every local source and is left for repair rather than rewritten, while the brief still ships and says why.
- A healthy access probe cannot erase failed extraction or make an old read fresh.
- Relayed reads and probes retain server request order through snapshot replay, independent of Mac clock corrections; older unsolicited wake uploads still rely on Mac timestamps.
- The Bridge serializes status-file updates, and a failed deferred-unread reader makes its messaging source partial without discarding valid messages.
- Schema/query failures report degradation; only access failures ask for Full Disk Access.
- Missing Chrome or WhatsApp stays quiet until that optional source has been available, while lost access to a previously available source is reported.
- Authenticated diagnostics retain status, counts and timestamps, never message bodies, browsing queries or file names.
- Partial observations retain only valid fields returned by the current read, including explicit empty results, and disclose coverage gaps from the first brief onward.
- Failed file metadata means unknown open status, never unread; isolated retries preserve valid peers within the existing six-second metadata budget.
- Browser and file observations feed the existing brief pipeline without another history-learning queue.

X attendee context follows these rules:

- Explicit owner credentials enable optional X context in either hosting mode; no token is unconfigured, and an untested token is unverified.
- Actual request results and last success appear in authenticated diagnostics, and a run with no requests cannot clear an earlier failure.
- A protected attendee's permission error appears only beside that attendee, without a connection alarm or a claim that they have no recent posts; it cannot stop other attendees or clear a prior connection failure.
- A lookup rate limit preserves already-linked attendees, and a timeline rate limit stops only further timeline calls while retaining other usable context.
- Failed or malformed API responses never become cached missing identities.
- Removing public or bookmark credentials excludes that staged input at consumption and invalidates queued messages that used it, without immediately deleting files.
- Previously learned public-profile facts remain editable graph memory; staged Posts and bookmarks are deleted by learning cleanup or the existing seven-day abandoned-input sweep.

The shared brief runner executes gather and compose directly; the agent does not choose whether
they happen. If essential memory writes fail, the staged brief can still be delivered and durable
background work retries those writes. Optional research and ancillary learning are separate from
initial delivery. A composition older than two hours is invalidated before delivery, including when it waits in the
saved-result queue or outbox. The existing invalidation path releases only a provably unaccepted
reservation and admits fresh work; ambiguous acceptance never authorizes a second brief. Every
replacement gets a distinct learning revision and required-write receipt. Composition and learning
share the same lock; an old follow-up cannot learn from or delete replacement inputs.
Recovery can wait behind learning already holding that lock. Acquisition contention requeues the
job for one minute later without spending a fault attempt, within the existing catch-up deadline.
Terminal work rejects a scheduler replay unless an ingress path explicitly admits a fresh retry. A successful empty Gmail search is a
quiet inbox; an unavailable or partial source is reported separately.


### Relationship importance and gifts

Only an explicit VIP choice or sustained reciprocal activity qualifies a person for proactive gift help. A saved birthday, meeting invitation, graph depth, raw message count or overdue reply alone does not. This policy is shared by Cloud/self-host and every delivery channel. Explicit user requests for gift help are unaffected; no purchase is authorized by an inferred tier.

The relationship pulse is the sole writer of dated `importance_evidence` within each canonical person's history in `knowledge/relationship_state.json`. It combines known-person iMessage, WhatsApp, calls and Gmail touches. The reader deduplicates days and uses a rolling **42-day** window. **VIP:** at least **6 active days across 3 weeks**, including **2 days in each direction**. **VVIP:** at least **12 active days across 4 weeks**, including **4 days in each direction**. Confirmed Granola meetings can strengthen an already reciprocal relationship: **3 held meetings across 3 weeks** for VIP, or **6 across 4 weeks** for VVIP, with at least **2 days in each direction**. These meetings require exactly **1 counterpart** after excluding the owner; multi-counterpart meetings cannot qualify anyone. Scheduled invitations, missed calls and unconnected calls do not count. This prevents one-way outreach or a single chat burst from qualifying. Missing, stale or ambiguous evidence suppresses a gift prompt; explicit `vip_people` choices remain authoritative. Day-of greetings retain their existing gates. The attention queue no longer grants any quiet-hour VIP bypass at all (Sep 2026 — volume is not importance); gift importance likewise grants no extra interrupt permission.

Gmail polling keeps independent inbox and sent-mail bounds, page cursors and pending IDs in the
version-2 `events/gmail_seen.json`. It resumes bounded oldest-first pages after downtime and advances
only after receiver acceptance. Fresh or legacy state starts with a defined 24-hour recovery window;
recovered messages keep their original timestamps and enter as catch-up.

### Memory retrieval

One reader, `knowledge_query.py`, supplies chat, briefs, meeting prep and notifications. Current
consented email, calendar, direct-message and active-loop subjects select older relevant facts by
exact canonical identifiers and contact aliases. Display names and nearby timestamps never join
conversations. Topic hints are data used for lexical ranking, never instructions.

Chat's person lookup adds `--person-coverage`. A missing graph match returns guidance
from existing consent and source-health records: check permitted historical sources,
verify an unknown connection, or explain an unavailable source. An empty recent
snapshot does not mean older history is empty. Ask and People check the indicated
sources before answering a past-conversation question, then name the sources and
material coverage limits if nothing is found. A missing graph match is never proof that
a previous conversation did not happen. The lookup itself makes no network or
model call and does not change memory; existing query consumers retain their output.

Each person block shares a **5-fact compact / 15-fact expanded** allowance across primary and
related facts, with a **3,200-character compact / 8,000-character expanded** ceiling. Explicit
corrections rank first, followed by topical overlap, primary-person context and curated summary
references. Confidence and recency settle ties within each person's facts. Summary references emit no duplicate
copy; a busy week cannot append facts beyond the allowance. Assertions fit whole or are omitted,
never shortened by cutting off a qualifier. The expanded read says when anything was left out;
the compact read is a summary by design and says so only when the character ceiling refused a
whole assertion. The graph itself is unchanged by a read budget. Legacy notes keep their leading
complete sentences inside the excerpt, including its omission marker in the limit. A line wrap
alone never ends a sentence; a single sentence too long to fit is left out whole. Situational
fields remain bounded inside the character ceiling.

Topical queries inspect at most **5 relation edges**, load exact canonical files and select at most
**2 related people**, with at most **2 related facts per person**, inside that same allowance.
Only facts sharing topic words qualify. Each excerpt names its subject and relation, and cannot
expand the current participant list, create a task or authorize an action. There is no recursive
expansion or fallback through a relation's display name. A primary conflict is shown with both
active assertions or omitted as a whole; conflicting indirect facts are left for a direct query.

Topic hints use at most **6 records per identifier**, **800 characters per record** and
**2,400 characters per identifier**. Conversations precede calendar and standing-work topic hints;
exact aliases share a single person-topic budget so a busy phone cannot hide that person's email.
The participant cohort retains its existing cap. Disconnecting
a source excludes its current hints and observations; it does not erase learned graph facts.
Explicit corrections and archives use the existing graph writer and apply to direct and indirect
retrieval alike. No new store, scheduler, model call or inferred standing authority is added.

## Learning before and between briefs

A new installation prepares a short first useful look after a context source and the delivery
channel connect. It uses recent conversations, calendar and the user's actual writing samples;
there is no mandatory profile questionnaire. Existing installations keep their memory and current
conversation. A connected source is not proof that all of its history has been reviewed.

Background learning progressively reviews the last six weeks of direct iMessage, WhatsApp and
Gmail exchanges, with per-source coverage receipts. Only durable cited facts enter the graph;
historical discussions, decisions, commitments and asks are not stored or resurrected as work.
Other sources keep their existing learning paths. The receiver starts a background memory pass at
most once every 15 minutes (`receiver.MEMORY_INTERVAL_SECONDS`), never while the previous one is
still running, and each pass advances at most 3 rotating history pages
(`memory_cycle.PAGES_PER_RUN`) and one unfinished Dreamer batch; the Dreamer selects useful existing facts for person
summaries, archives exact duplicates and records contradictions. Facts record `archived_reason`
separately from their source. Only an exact observation of a fact archived for age (`stale`) may
revive it. User archives, corrections, superseded facts, duplicates and legacy archives with unknown
reasons stay archived. It cannot change source grants,
approvals, priorities, mutes or code. Conflicting evidence stays uncertain; the user's corrections
win and do not expire. Rereading the same source or a reference-free repetition is not additional
confirmation and does not refresh the fact's recency; distinct evidence or an explicit correction can.

Self-host background history and Dreamer calls are held until `SOTTO_MODEL_PROXY_URL` and
`SOTTO_MODEL_PROXY_TOKEN` select the existing native proxy and that tenant has a finite
`budget_cents`. A missing setting or HTTP 402 changes no source cursor and stops the remaining
history/curation work for that pass. Foreground chat and ordinary briefs retain their direct BYOK
behavior. An owner who deliberately accepts unbounded background BYOK spend can set
`SOTTO_BACKGROUND_UNMETERED=true`; no other value opts in. Managed tenants retain their configured
policy, including an explicit null budget for the personal pilot.

Draft learning uses original send timestamps and canonical counterpart identity across channels.
One observed send can credit at most one offered draft, with the source event identity persisted in
`outcomes.jsonl`. A late genuine send can repair an inferred old result; explicit user dismissal
remains final. Missing outbound telemetry and silence are unknown, never rejection or style evidence,
and only a verified verbatim send confirms a voice sample.
Before reading a source, proxy-backed cycles authenticate to the versioned, content-free background
capability and check the effective primary model against its supported native models. An
unreachable capability, an unsupported model, an invalid credential or an exhausted finite
allowance holds the cycle. A proxy that predates the model list — one answering without
`supported_native_models` — holds it too, and is reported as an unsupported proxy capability
rather than as a budget problem, because during that rollout the owner's budget is fine and the
proxy is what needs upgrading.
Self-host also requires a finite budget; managed tenants retain their explicit null-budget option.
The actual self-host model request repeats the finite-budget requirement for atomic admission.
Each background extraction or Dreamer call makes one upstream attempt, including with explicit
unmetered BYOK. Transient failures wait for the existing retry clock; foreground briefs retain
their immediate retry and fallback behavior.
History and skipped Dreamer work retain the actual hold reason in their receipts, including when
the Dreamer's own evidence selection failed underneath a hold — a held cycle reports the hold,
because the hold is why nothing ran.
An unchanged malformed history page or nonadvancing cursor gets two attempts, then pauses with one
compatibility probe every 24 hours so a repaired Bridge resumes without manual state edits. An
unchanged malformed Dreamer response or native HTTP 400/422 gets two attempts, then waits until its
prompt/schema/model route or exact candidate evidence changes. Only what the model actually
returned counts as a malformed response: a transport or configuration fault raised before the
answer arrives is an ordinary failure and spends none of those attempts. Availability, network,
timeout, HTTP 429 and HTTP 503 failures keep their ordinary retry behavior. These are retry bounds,
not a new daily model allowance or work counter.
Only the Gmail adapter decides that a continuation token is dead: it names a page-token request the
provider rejected as an invalid argument, and it names it with a code, never with the provider's
message. Any other deterministic Gmail rejection — a bad query, impossible window bounds, an adapter
regression — is a broken request and parks like every other protocol failure, with no replay. A dead
token is retried twice, then its same frozen window restarts from page one; that window may restart
again only after an attempt reaches further into it than the attempt before the last restart did.
Depth counts only pages in this window, excluding completed windows from the lifetime totals.
An older checkpoint without a window baseline can restart once, but its unknown depth cannot earn
another reset. Older reset markers without both progress counts stay consumed across upgrades.
A window whose restart budget is spent records
`continuation_restart_exhausted` and its blocked receipt names that reason, so a stuck window says so
instead of probing a dead token forever. A completed window clears its marker and starts fresh.

Observed sends and edits are evidence for writing voice; sustained reciprocal activity helps assess
relationships. Those observations are contextual, not blanket preferences. A stated instruction or
correction overrides an inferred pattern. Explicit “useful” / “not useful” feedback on an identifiable
retained brief item supplies an example for future relevance and writing. Item ratings remain
separate within a brief, and ambiguous repeated excerpts require a more specific selection.
Examples are read only while the archive and its recorded source permissions remain available;
legacy archives and offered drafts without provenance are not reused as prompt examples. No response, an unsent
draft or a processing reaction is not a negative rating. A rating never authorizes an action or
cancels an obligation. Existing relevance and approval rules still govern every nudge and write.

Joining two conversations requires evidence that they concern the same event or obligation.
A matching time or topic alone is insufficient. Separate overlapping plans may reveal a conflict;
Sotto must not invent a shared organizer or claim the user's answer to one answered the other.


## Shared relevance and delivery recovery (September 7)

Briefs, digest review and event triage use the same native-model relevance policy with current
conversation context. `urgent` may enter the interruption lane; an ordinary `actionable` item or
`scheduling_ask` normally waits for catch-up unless an existing explicit escalation applies.
One already-actionable pending item can trigger digest review without waiting for eight unrelated
messages. Conversations are keyed by source and thread identity, not a person's display name.
A failed review does not advance its coverage window. A successful review with output carries an
explicit cutoff; only provider acceptance advances that window.

The preschool waiver remains actionable until evidence says it was completed. A generic donation
blast has no personal obligation. A greeting alone is context, not a task. An unanswered invitation
retains its ask even if a later greeting arrives. These are applications of one judgment, with
regression examples; there are no special sender-name filters.

Before a delayed nudge is sent, its useful deadline, source permission and applicable loop/meeting
identity are checked again. A meeting-bound nudge is cancelled by the calendar only on a complete
same-day observation younger than 600 seconds or three refresh intervals, whichever is longer
(`delivery_effects.CALENDAR_MIN_FRESH_SECONDS`) — a stale or failed read is never treated as a
cancellation — and a post-meeting tap stays deliverable for 30 minutes after the meeting ended
(`delivery_effects.POST_MEETING_VALID_SECONDS`), then expires. Closed or changed work does not produce an obsolete reminder. Held
proactive candidates are not counted as delivered, and an intention is not finished merely because
the scanner considered it. Offers become actionable only after the question was accepted by the
provider. Declines and outbound writes keep their existing explicit approval requirements.

An earned nudge does not choose an unanswered decision. The shared approval policy requires an open accept/decline choice, or labeled alternative drafts, until the user chooses a direction. It forbids inventing a pass or its rationale from relevance, timing or past preferences. Even labeled alternatives may not invent a completed review, team debrief, or past decision. This is an agent instruction; real account writes remain independently gated in code.


### Calendar context and preview (September 9)

A calendar entry explicitly labelled CONTEXT, prep notes, briefing notes or meeting notes is
supporting material when it uniquely matches a real meeting's subject and exact start/end. It stays
available for preparation, but does not manufacture a conflict or a second meeting nudge. A shared
time alone is not sufficient; ambiguous or unrelated meetings remain separate. Sotto never deletes
or declines an event on this basis. Coming Up shows up to five schedule lines, without a repeated
instruction to open Calendar for the full agenda. After extraction and revision, code rebuilds
that preview from the consented source snapshot in the user's timezone. It selects the nearest
remaining or ongoing events through the next three local days, excluding declined and cancelled
entries. Birthdays use the same local date and contact permissions; when present, the nearest
birthday reserves one of the five lines and further birthdays use any spare lines. Explicit section
mutes still apply. Missing calendar access cannot resurrect cached meetings. Extraction, RSVP capture
and the critic share the same eligible source list. Quiet-loop expansion runs after the schedule,
so optional detail cannot take its space. A quiet evening uses “Nothing
needs your attention right now,” rather than morning wording or an unsupported claim about the
whole inbox.

Background memory uses a constant native response shape with bounded inner evidence arrays.
The writer validates every subject, citation and text bound, rejects conflicting input references
before a model call, and never guesses a replacement citation. It validates each person's complete
result independently: if any fact is invalid, that person's entire result is discarded. All copies
of a repeated output subject are rejected. Fully valid people can still be applied together after
consent is rechecked. The existing history checkpoint records the last extraction and cumulative
rejected-person-result counts by safe error code; reviewed messages are not a claim that every
proposed fact was stored. No private message or rejected fact text is retained in diagnostics.

A partially valid page advances with explicit rejection counts. Invalid JSON, a malformed top-level
response, or a nonempty response with no valid people preserves the checkpoint. Native HTTP 400/422
failures hold the page until the schema, prompt, native client implementation or model route changes;
other transient failures retain the hourly retry. A wholly invalid extraction parks after two attempts for the same source revision. Empty, valid no-fact responses remain valid reviews.
Malformed history protocol responses likewise park after two unchanged attempts and make one
compatibility probe every 24 hours. Dreamer response-contract failures park after two attempts for
the same prompt/model revision and exact semantic evidence batch; changed evidence for the same
person releases the latch. Transient availability, transport, HTTP 429 and HTTP 503 failures do not
enter either latch.
For Gmail, two HTTP 400 rejections of a nonempty continuation token restart the same frozen window
once. An empty-cursor rejection, or another bad token in that window, stays parked.
Each conversation candidate is capped at 12 messages, 400 characters per message and 4,800 characters
in total. This isolation adds no repair call, new queue, or weaker evidence check.

Provider acceptance is never repeated while its local state effects run. The outbox makes at most
five post-delivery effect attempts, then exposes a quarantined failure that keeps its provider
receipt, label, run id and the reason it failed, and drops the effects' addressing — nothing
replays a quarantined row, so it does not keep the identifiers a replay would have needed, and a
row that stopped there carries no one's phone number or chat id. Process death inside a callback counts as an
attempt; recovery quarantines an exhausted row before running that callback again. Callbacks are serialized across
processes, even when they take longer than the retry interval. Closed receipts remain for seven
days after their last delivery or effect settlement.

Contacts-only access (Apple or Google) is identity metadata, not enough context to trigger a
personal brief. The setup screen reads the same managed source and messaging gates as scheduling,
and reports first-brief completion only from the existing onboarding delivery receipt.

Mac onboarding starts every supported source on and shows the toggles before requesting Full Disk
Access. Google pairing does not start Mac collection. The shared app gate permits startup and
reconnect only after source confirmation; skip disables all readers. Upgrades retain saved choices.
A toggle being on is not a successful read, and Contacts alone does not justify a scheduled brief.

Google reconnects preserve authorization order across restart: the receiver records the generation
before installing credentials. An interrupted request may retry only while still current; older
requests cannot restore permissions superseded by newer consent. Completed retries do not reinstall.


The model proxy rejects duplicate JSON keys and forwards canonical JSON after validation. Admission
uses one atomic SQLite ledger and permits 60 admitted requests per tenant in a rolling 60-second
window. This short rate bound is separate from the optional spending admission cutoff. Self-host
background learning marks its native requests as requiring a finite tenant budget, so an unlimited
tenant is rejected before an upstream call; the same ledger and reservation allowance are used.
The allowance is conservative admission accounting rather than a provider invoice.
The proxy rechecks bearer validity inside the admission transaction after body reads and lock waits;
an expired credential returns 401 without reserving spend or reaching the provider.

Messages readiness comes from authenticated transport plus the receiver's existing source and
first-delivery facts; an unreachable instance is unknown, not ready. Live shared routing remains a
release gate.

### Visual presentation

Morning/evening briefs and focused meeting backgrounds default to four-photo galleries on iMessage; other channels, short updates and consent questions stay text. The shared renderer changes presentation only. After mandatory brief sections, the composer can use spare space in a readable four-card gallery to name quiet active obligations, ordered by deadline then age. It uses the same layout to check fit; the remaining items stay a count. The named details enter the canonical text before delivery attribution, so all channels and chase suppression agree. This does not make an item urgent. It makes no additional relevance or model call. Crowded briefs first redistribute complete paragraphs using measured height, preserving font size and source labels. Needs-you and today can share one card, leaving room for follow-ups on two. A brief with an action that still cannot fit stays text; uncertain multipart acceptance is held rather than blindly retried. See [visual briefs](VISUAL-BRIEFS.md).

## Shared writing rule

All Sotto-generated prose uses the same [_shared/references/writing-style.md](../sotto-chief-of-staff/_shared/references/writing-style.md): plain, concrete language, no em dashes, and no canned AI phrasing or filler. The Gemini/OpenAI/Anthropic composition adapters attach it as a system instruction; raw document extraction transport preserves its original instructions; Hermes receives the same file with its persona in managed and self-host installs. The shared chat formatter normalizes prose punctuation while leaving URLs and literal machine text intact. AI-ism avoidance is a generation instruction, not a word-deletion filter.


### Execution and checkpoint safety (pending release)

The scheduled and wake proactive checks keep the same cadence and shared relevance rules.
Gathering, due-item selection, quiet-hour holds and reservations run deterministically before
Hermes. An empty check makes no composer call; an accepted nudge uses the existing skill and
approval rules. Delivery still rechecks consent and current eligibility.

One calendar refresh is one triage batch with individual acknowledgments. Post-meeting tap slots
are reserved before dispatch and count toward the existing cap while pending. Failed checkpoint
writes do not reset that count. Work ownership and completed queue/drop receipts reconcile after
restart without another triage. An unreadable checkpoint pauses taps; unresolved pending taps that
leave the lookback keep their slot until local midnight.

Writing-style extraction precedes draft confirmation. Dreamer reruns for changed fact evidence,
not bookkeeping-only file updates. Exhausted memory-work buckets wait for the next bucket;
transient admission failures retain their retry behavior. These changes add no new cadence,
interrupt allowance, source, or model route.

Rejected completion proposals retain content-free reason counts in the existing continuity Learn
receipt, even when the pass succeeds. This is one summary per pass, not a log of private evidence.

## Measured learning and personal attention

The operator's read-only `release_one_proof.py --days 30` report uses existing Learn receipts
and ledger files. It states the retained date range and coverage.
New Learn receipts count accepted and rejected completion proposals, including zero proposals.
Older receipts without this field are unknown. A quick dismissal is a review signal, not proof of
an incorrect capture. Missing rejected-proposal, reopening or manual-resolution links are reported
as unavailable, never reconstructed from today's status.

A brief includes one plain line when configured older history has failed, is held or is stale after
**24 hours** without success. A real source failure takes precedence over a spending hold. It
distinguishes history from today's source feed. Recovery removes the line; unconfigured sources
and ordinary pending backfill stay silent, and a source whose feed the same brief already reports
unavailable gets no second line about its history (one cause, one line).

Researching a person cannot raise their relationship rank. Stored-fact count, tracked status,
job title and talking points supply context only. The pulse orders waiting/drifting relationships
by their existing age/cadence classification with a positive owner-reply tie-break. Silence does
not reduce importance. Priorities remain ranking context permanently, never an exclusion filter.

Reply timing needs **2 completed replies** in each direction separately, within the same channel
and conversation. A burst is one turn. The existing relationship state retains at most **32 reply
samples** per person; native IDs deduplicate overlapping pages. Completed pairs can accumulate
across pages, but gaps between pages never manufacture a pair. Calls do not establish reply speeds.
Learned weight uses the graph's **0.08 weekly decay** and expires after **60 days** without evidence.
Stated VIP and priority choices never decay. The owner's reply speed helps attention ties; the
counterpart's speed may extend a default chase only within a future deadline. An overdue
obligation retains its ordinary cooldown; learning never accelerates a chase. Sparse evidence
keeps the ordinary chase timing. Explicit promise dates keep their existing authority.

Recapturing or recomposing an obligation never counts as surfacing. The accepted outbox delivery
records an exact obligation ID and idempotent delivery key in that obligation's existing file.
Stable obligation identity (anchor, clock start, direction and source message, never wording or
the evidence list) preserves attribution across restatements by essential learning, and the
finalizer checks that same identity at the channel's acceptance, quietly skipping a closed row or
a different debt. Counting never cancels
a brief. Exact receipt hashes are retained for retry safety; no duplicate numeric count is stored. Person-name mentions and aggregate counts do not count. A narrative line counts when it
names the person, carries the model's `<!--loop:ID-->` marker for that ask, and shows all of the
ask's content words; a hidden marker beside words that do not show the ask counts nothing.
Free paraphrases may undercount rather than credit a different obligation with shared action words.
Without a marker, the ledger summary must appear verbatim (the still-open backstop's own lines). Newly captured tasks are not counted until a later brief can bind their ledger identity. Legacy capture counters cannot
trigger a repeated-delivery question; age and overdue status still select stale work.

Friday appends at most one review question. A standing-rule confirmation takes precedence;
otherwise one repeatedly delivered obligation or supported priority/VIP candidate may use the
allowance. Quoted questions in messages and open asks do not consume this explicit allowance.
Reply speed alone proposes nothing: a VIP candidate needs sustained reciprocal importance or a
user-confirmed family relation, and a priority candidate needs your attention plus contact on at
least two distinct days in each direction. Sustained reciprocity ranks first, a confirmed relation
second, an attention priority last, because your own words about a person outrank an inference.
A candidate that went stale between compose and send never withholds the brief, and its cooldown
is still recorded at delivery: the question reached the user, and recording that grants no
authority. Candidate suggestions have a **30-day cooldown**,
spent only by accepted delivery and stored in the existing relationship state. A suggestion never
writes a preference. The stated command uses the existing VIP or master-file writer, preserving
other priorities and refusing additions when the three-priority set is full.

Two audit corrections use existing writers: heard-once facts decay and archive from the same
first-observation clock, so repeating an unsupported claim cannot refresh confidence or retention; explicit `tone_notes` reach the draft
writer alongside observed style samples. User-corrected facts retain their authority.


## Read back the work

A missing or non-text Granola attendee name is absent identity evidence, not an extraction failure.
The existing ownership and counterpart checks still reject ambiguous promises. Learn receipts
retain each failed revision's exception class, never the exception body, so a successful provider
call cannot hide a failed grounding or ledger write.

`meeting_context.py` reads notes and joins obligations by the exact Granola occurrence. It returns
up to **20 obligations**, with **8,000 characters per note field** and explicit omission metadata.
Directions remain separate. Evidence-backed resolution, explicit user completion and other ledger
closures have different labels. Completion evidence is withheld when its source is disabled.
The next prep carries these current states beside the newest prior meeting notes, and the actual
calendar description can explain the meeting's purpose. Unlinked email remains background.

`brief_detail.py` retrieves only an accepted owner gallery within **7 days**, under unchanged
source permissions. Pages carry up to **12,000 characters** with a continuation offset; ambiguous
searches list up to **20 choices** and report omitted matches. Old artifacts without permission
metadata require a fresh source read. The lookup neither sends nor recomposes the brief.

`release_one_proof.py --days 7 --complete-days --timezone America/Los_Angeles` excludes today's
partial day and reports which morning/evening receipts contain exact counters for each local day.
Two exact scheduled Learn receipts establish that day's observation coverage, not model accuracy.
Missing historical counters remain unknown; a zero observed error candidate count is not zero errors.
