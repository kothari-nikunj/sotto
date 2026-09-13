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
  new mail through the same endpoint. No Mac needed. A smaller `in:sent` lane rides the same poll:
  your OWN outbound mail enters as a silent "signal" (queued, never a nudge, never a model call) —
  it exists so offered email drafts can be graded against what you actually sent and so replies you
  send can close loops.
- **Calendar** — a background thread refreshes today's events every `SOTTO_CALENDAR_REFRESH_SECS`
  (default 15 min). It powers the in-meeting hold, detects meetings that just ended, and **diffs
  each refresh against the last** to catch what changed about the imminent calendar: a decline, a
  last-minute invite, a moved meeting, a cancellation (`SOTTO_CALENDAR_NUDGES=0` disables). Only
  *imminent* changes count: a move or cancellation within 24 hours, a decline within 48 — and a
  NEW invite only within 4 hours (plus a 15-min grace for a meeting that just started): a next-day
  invite is ordinary scheduling, not an interrupt — the email lane already nudges a real invite
  with a draft, and tomorrow's brief covers tomorrow. Solo blocks, all-day events and internal
  standups never mint a change. Decline nudges require explicit participation by the user and
  exactly one other human: group RSVP changes and meetings merely visible on a shared calendar
  stay quiet. Room/resource attendees do not count as people in either the calendar or prep lane.

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
| **Proactive watcher** (intention · meeting prep · commitment · chase · birthday · handoff · retune offer) | `proactive_scan.main`, the `*/15` cron | `triage_event.triage` — the tick goes in as ONE bundle of synthetic `source: "proactive"` events, classified by `_classify_proactive` (snooze → quiet hours → mutes; the nudge's kind is its class) and then through every gate below. The bundle that comes back is what the watcher delivers |
| **"Nudge me now"** (you promote a held item from the dashboard) | `dashboard._post_cadence` → `receiver.run_promote` | `triage_event.promote_one` — the same `_valve_candidate` rule the valve uses, spending exactly one budget unit |

## The gate order

**Tier 0 is deterministic and free.** In order, an event is:

1. a meeting that just ended → the *post-meeting tap* (below); a nudge the watcher planned → its
   own kind (a birthday, something you're owed), held by the snooze then quiet hours then a mute,
   and then through every gate the rest of this list ends in; a *calendar change* (a decline, a
   last-minute invite, a move, a cancellation of an imminent meeting) → **nudge**, held only by
   the snooze, quiet hours, and a muted person;
2. your own outbound → queued as a `signal` (ledger fodder, never a nudge);
3. an answered or outgoing call → dropped; a missed call from someone you know → **nudge**; from an
   unknown number → queued;
4. an OTP, shortcode, or system message → dropped silently;
5. from a muted sender or muted person → dropped;
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
- **The honest version of that sentence** — the budget covers *everything except four exempt
  classes, which are uncapped*: missed calls (at most one per thread per
  `SOTTO_EVENT_COOLDOWN_MIN`) and cross-channel escalations (at most one per person per
  escalation window) have no daily ceiling at all, post-meeting taps have their own
  (3/day), and calendar changes are bounded only by your calendar actually changing (each distinct
  change nudges once, ever). So a worst-case busy day is: 4 budgeted interrupts + 3 taps + however
  many missed calls, escalations and calendar changes the day actually contains + the midday digest
  + your two briefs.
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
  top-of-queue relationship-pulse priority, or a typed `family_of` relation in their file.
- **In-meeting hold** — while you are inside a timed calendar event with at least one other human,
  would-be nudges queue as `meeting_hold` — except missed calls, escalations, calendar changes
  and imminent meeting prep. Prep still obeys quiet hours, snooze, mutes, cooldown and the daily
  interrupt budget; it can arrive during the previous meeting so back-to-back prep does not expire
  before reaching you. Solo blocks and all-day events never hold, and a calendar
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
  the same budget spend, the same ledger row. The two gates it skips are the clock ones (quiet
  hours and the snooze) — those exist to stop *unprompted* interruptions, and you asked for this
  one. The room you're in and the day's budget still apply.
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
- **A debt you owe closes on your own outbound, on any channel** — an outgoing iMessage, WhatsApp,
  **email**, or call to that person, or a meeting with them now on the books, closes the loop before
  the brief is composed; the resolver reads your sent mail off the gather's own `in:sent` lane, not
  off a list the agent remembered to write. **Never tell you twice** is then measured, not
  requested: a person Sotto already nudged you about today (a delivered nudge, correlated by id to
  the channel's receipt) never opens the model's draft and takes **at most 2 lines** in it — one
  status line, plus a Coming Up mention — and the validator hands a violation to the critic's
  revise pass (the lines the composer itself appends afterwards, the still-open backstop and the
  receipts, are read from the record, not written by the model, and are not counted).
- **What you owe expires quietly; what you're owed gets chased** — an open loop you owe drops off
  after 7 silent days, but something you're *waiting on* never expires: it closes the moment they
  actually deliver (a substantive reply — a link, a file, or real text, never a bare "ok" or a
  promise to send it later), or after `SOTTO_CHASE_AFTER_DAYS` (default 3) of silence it becomes one
  short, warm chase — at most one chase nudge a day, at most 2 per item. A chase is only counted
  once it is actually delivered, so one that quiet hours or a snooze swallowed is not one of your
  two — and a chase that was stamped but never delivered sends that loop to the back of the next
  day's pick (`chase_stalls`), so one perpetually-blocked loop cannot hold the whole chase lane.
  After the second, Sotto stops and asks you plainly, by name: *"I've nudged Maya twice about
  the contract — nudge her again, or let it go?"* — **once**, and **on its own clock**: the question
  is asked the first watcher tick it comes due (outside the two hours after a brief, which carried
  the loop), never behind the tidy-up offer's weekly cooldown. That question is asked one time (it is
  stamped on the loop when it is delivered, on the same rule as the chase), and from then on the
  loop stops taking a line in every brief: you have been asked, so it waits in the count line and on
  `/app#loops` until you resolve it, drop it, or say keep waiting — which restarts its chases and
  makes it askable about again.

## Managed Cloud pilot activation

In managed mode, scheduled morning/evening briefs and Bridge wake-triggered briefs wait until the owner has texted Sotto and at least one consented context source is connected. A sleeping Mac and a source with zero events are not disconnected sources. Missing capability state means zero sources. After the first owner DM, the receiver delivers one `status:no-sources` notice through the nudge outbox and remembers its acceptance on disk; no daily quiet-day or sources-unavailable brief is generated. Manual run-now remains an explicit action. Self-host keeps its current behavior.

The managed model credential is a lease: the receiver's heartbeat renews it once it is within 72 hours of expiry (`model_lease.RENEW_BEFORE_SECONDS`) and, after a failed attempt, retries hourly (`model_lease.RETRY_SECONDS`); a failed renewal never invalidates the current credential.

## Shared relevance — before choosing how to surface it

One rule governs briefs, digests and nudges: surface an item only when evidence shows a concrete
action, decision, preparation need, or meaningful development for this user. The sole policy is
`sotto-chief-of-staff/_shared/references/relevance.md`; the brief and its critic load it, event
triage and digest review use it through `relevance.py`, and the nudge skills read it before composing.
A VIP label or a sender's deadline does not establish relevance. Services and assistant relays can
carry real obligations; later answers and completion evidence can remove them. No sender/category
blacklist is added. Explicit consent, mutes and delivery gates still govern what may be considered.

The digest's existing eight-event activity threshold now buys a review, not a promise to send, and
one queued actionable, scheduling or urgent item buys that review on its own, below the threshold.
One bounded native-model call, given the current local clock, reviews at most 100 conversations with their latest 20 messages
(up to 400 characters each — `personal_context.CONVERSATION_TEXT_CHARS`, the one cap every
conversation rendering shares), including outgoing answers, before the six-item delivery cap.
Existing priority bands decide which conversations fit in that review; the brief remains the
backstop beyond its bound. Review can retain fewer items or none. Invalid/incomplete judgment or
provider failure stays silent. The normal digest-window stamp still advances, including silent
runs; no second state writer, model route, per-item call, or schedule is introduced.

## What you get, and when

| | When | What |
|---|---|---|
| Morning brief | 6:30 local (or the moment your Mac wakes past 7am) | opens with the time of day and the date in your zone ("Good morning — Saturday, September 6"; good afternoon when the wake path composes it after noon), then your day across messages, email, calendar, plus open loops. One per day, always: if your Mac slept through 6:30 the brief still goes out from the last saved snapshot, and when the Mac wakes later its fresh data is folded into that snapshot instead of composing a second brief — the nudges and the midday digest surface whatever the morning brief couldn't see |
| Evening brief | 17:30 local | opens with "Good evening — <the date>", then accountability, tomorrow, post-meeting follow-up drafts, and a **What moved today** block — reminders delivered ("Reminded you to chase Maya…" — a reminder to you with a draft behind it, never a message Sotto sent to Maya; a loop reminded today is not listed again under Still open), loops closed, interruptions held, people prepped, follow-ups offered, named where the record has a name. Outcomes only; nothing moved means no block, and it will never tell you how many emails it read. Plus **at most one question**: a *"make that a standing rule?"* confirmation when today's transcripts showed you stating one — your yes writes it to the master file; it is never written unconfirmed. **Automatic mute questions are paused**: the recorded dismissal signals include inferred draft non-use, which does not establish that a person matters less. The producer returns without scanning the ledger or writing a question. Explicit "mute Bob" instructions still work |
| Midday digest | 12:30 local | reviews what queued **since the last delivered brief** when at least `SOTTO_DIGEST_MIN` (default 8) known-sender ambient/deferred events justify review; sends only relevant items, otherwise silent. Nudges Sotto raised itself never count toward that 8 (they aren't people), though they may ride along in the message |
| Nudges | any time, subject to every rule above | one short message with a reply already drafted — and an offer to act on it: an email asks ("want this in your Gmail drafts?" — on your yes it saves a real, threaded Gmail draft you send yourself; a `mailto:` is removed at the delivery seam, so one can never reach you from a scheduled run), every other channel gets a one-tap link — and a phone-shaped link whose number is not dialable (a model-masked `imessage://+141****3682`) is removed at that same seam, because a dead link is worse than none |
| Proactive nudges | a meeting starting in ~45 min you haven't prepped (the nudge **carries** who they are and the one thing open with them), a commitment due today, one chase for something you're owed, a birthday (`SOTTO_BIRTHDAY_LEAD_DAYS`, default 3, days out for VIP/VVIP gifts and on the day for saved contacts — unless a brief already delivered today), a question about an ask nobody answered twice, an offer to tidy a heavy pile | the whole push spends **one** unit of the daily interrupt budget and obeys the shared mutes, quiet hours and snooze; imminent prep alone bypasses the in-meeting hold so it can arrive before a back-to-back meeting |
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
execution attempts. A saved completed output is reused for handoff without another model call.
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
replies and actionable source/provider failures keep their existing behavior.

Every message is persisted **before** its first send attempt. The adapter requires structured
success and a provider message ID; an exit code alone cannot mark delivery. The outbox retries every
minute, backing off 1 → 2 → 4 minutes and doubling to a fifteen-minute cap. It rechecks source
permission and relevant loop/Calendar state before sending. Invalidated brief replacement is itself
retryable, so a temporary enqueue failure cannot lose the day's brief. A provider acceptance followed
by a process crash can still cause a duplicate if the provider lacks an idempotency contract.

Waiting is not forever, and how long depends on what it is:

| | Waits until | Then |
|---|---|---|
| A nudge | it is older than 240 minutes — the same window the release valve refuses to promote a held nudge past | it is dropped, and the reason is written to the Record. A *"meeting in 10 minutes"* ping delivered an hour late is worse than silence |
| A morning or evening brief (and the weekly pulse) | the end of its local day | it **fails visibly** — a day with no brief is something you should be told about |
| The midday digest | the end of its local day, which ends before the next digest window opens | it is dropped: tomorrow's digest covers what it would have said |

Nothing is thrown away quietly: every failure and every expiry is a row in the Record with its
reason, and the Briefs page carries a line whenever anything is still waiting or has given up.

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
silently discarded without a reason you can read.

Preferences come from explicit instructions. Draft usage does not relax approval tiers or produce
mute suggestions; the unused behavioral learner has been removed. Draft matching still records
outcomes and confirms verbatim voice samples in the Learn step.

System jobs use the receiver in Cloud and receiver-based self-host. The weekly relationship pulse runs at 9 a.m. Monday in the user’s timezone. Managed scheduled jobs additionally require messaging activation and a connected context source. Both modes share work recovery, the outbox and silence handling.

For the managed personal pilot, successful Google consent opens only the Gmail/Calendar source capabilities actually granted; missing or declined permissions leave the scheduled-brief gate closed when no other source is connected.

A Bridge with a local Cloud policy reads only its consented sources and excludes the Sotto iMessage handle before transferring history, unread messages or events. Contacts follow their source consent; source access does not authorize outbound actions.

The shared brief runner executes gather and compose directly; the agent does not choose whether
they happen. Once a valid brief artifact is staged, essential memory writes continue as durable
background work after delivery and cannot block that brief. Optional research and ancillary learning
also cannot gate a valid brief. Terminal work rejects a scheduler replay unless an ingress path
explicitly admits a fresh retry. A successful empty Gmail search is a
quiet inbox; an unavailable or partial source is reported separately.


### Relationship importance and gifts

Only an explicit VIP choice or sustained reciprocal activity qualifies a person for proactive gift help. A saved birthday, meeting invitation, graph depth, raw message count or overdue reply alone does not. This policy is shared by Cloud/self-host and every delivery channel. Explicit user requests for gift help are unaffected; no purchase is authorized by an inferred tier.

The relationship pulse is the sole writer of dated `importance_evidence` within each canonical person's history in `knowledge/relationship_state.json`. It combines known-person iMessage, WhatsApp, calls and Gmail touches. The reader deduplicates days and uses a rolling **42-day** window. **VIP:** at least **6 active days across 3 weeks**, including **2 days in each direction**. **VVIP:** at least **12 active days across 4 weeks**, including **4 days in each direction**. This prevents one-way outreach or a single chat burst from qualifying. Missing, stale or ambiguous evidence suppresses a gift prompt; explicit `vip_people` choices remain authoritative. Day-of greetings retain their existing gates. The attention queue's urgency-based quiet-hour VIP rule is unchanged; gift importance grants no extra interrupt permission.

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
summaries, archives exact duplicates and records contradictions. It cannot change source grants,
approvals, priorities, mutes or code. Conflicting evidence stays uncertain; the user's corrections
win and do not expire. Rereading the same source is not additional confirmation.

Self-host background history and Dreamer calls are held until `SOTTO_MODEL_PROXY_URL` and
`SOTTO_MODEL_PROXY_TOKEN` select the existing native proxy and that tenant has a finite
`budget_cents`. A missing setting or HTTP 402 changes no source cursor and stops the remaining
history/curation work for that pass. Foreground chat and ordinary briefs retain their direct BYOK
behavior. An owner who deliberately accepts unbounded background BYOK spend can set
`SOTTO_BACKGROUND_UNMETERED=true`; no other value opts in. Managed tenants retain their configured
policy, including an explicit null budget for the personal pilot.
Before reading a source, the cycle authenticates to the proxy's versioned, content-free background
budget capability. A 404 from an older proxy, invalid credential, null budget or exhausted allowance
holds the cycle; the actual model request repeats the finite-budget requirement for atomic admission.
History and skipped Dreamer work retain the actual hold reason in their receipts.

Observed sends and edits are evidence for writing voice; sustained reciprocal activity helps assess
relationships. Those observations are contextual, not blanket preferences. A stated instruction or
correction overrides an inferred pattern. Explicit “useful” / “not useful” feedback on an identifiable
brief item or draft supplies an example for future relevance and writing. No response, an unsent
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

An earned nudge does not choose an unanswered decision. The shared approval policy requires an open accept/decline choice, or labeled alternative drafts, until the user chooses a direction. It forbids inventing a pass or its rationale from relevance, timing or past preferences. This is an agent instruction; real account writes remain independently gated in code.


### Calendar context and preview (September 9)

A calendar entry explicitly labelled CONTEXT, prep notes, briefing notes or meeting notes is
supporting material when it uniquely matches a real meeting's subject and exact start/end. It stays
available for preparation, but does not manufacture a conflict or a second meeting nudge. A shared
time alone is not sufficient; ambiguous or unrelated meetings remain separate. Sotto never deletes
or declines an event on this basis. Coming Up is labelled as a preview, with the full agenda in
Calendar. Its disclosure sits outside the five schedule-line cap. A quiet evening uses “Nothing
needs your attention right now,” rather than morning wording or an unsupported claim about the
whole inbox.

Background memory uses a constant native response shape with bounded inner evidence arrays.
Its writer validates supplied subject IDs, text length and source references before any writes.
Native HTTP 400/422 extraction failures hold the page until the schema, prompt, native client implementation or model route changes;
other transient failures retain the hourly retry. No page is skipped to make coverage appear complete. Operator receipts
park an unchanged extraction after two attempts for the same source revision. Each conversation
candidate is capped at 12 messages, 400 characters per message and 4,800 characters in total.

Provider acceptance is never repeated while its local state effects run. The outbox makes at most
five post-delivery effect attempts, then exposes a quarantined failure while retaining the provider
receipt and replayable effect metadata for recovery.
identify the failed stage and validation category without storing private source text or raw model
output in diagnostics. No evidence or consent validation is relaxed to make a checkpoint advance.

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

Native Cloud sign-in starts with an opaque polling bearer and an account-service browser URL; it
returns no confirmation code or Google authorization URL to the Mac. After Google succeeds, only
the initiating cookie-bound browser sees the eight-character code. The user enters it in Bridge,
which submits it to the native-only confirmation endpoint with that polling bearer and then resumes
status polling. The broker permits five native code attempts, retains at most 50 unverified pending
sign-ins and 500 total sign-in sessions, and never evicts verified or in-flight handoffs to admit an
anonymous start. No credential handoff begins until the native code confirmation succeeds.

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
