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
  standups never mint a change.

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
`urgent` · `actionable` · `scheduling_ask` → nudge; `ambient` → queue; `ignore` → drop. **Any error
queues** — the funnel fails toward silence, never toward noise. One taught judgment worth naming:
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
  would-be nudges queue as `meeting_hold` — except missed calls, escalations and calendar changes,
  which come through anyway. Solo blocks and all-day events never hold, and a calendar
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
| Morning brief | 6:30 local (or the moment your Mac wakes past 7am) | your day across messages, email, calendar, plus open loops. One per day, always: if your Mac slept through 6:30 the brief still goes out from the last saved snapshot, and when the Mac wakes later its fresh data is folded into that snapshot instead of composing a second brief — the nudges and the midday digest surface whatever the morning brief couldn't see |
| Evening brief | 17:30 local | accountability, tomorrow, post-meeting follow-up drafts, and a **What moved today** block — chases delivered, loops closed, interruptions held, people prepped, follow-ups offered, named where the record has a name. Outcomes only; nothing moved means no block, and it will never tell you how many emails it read. Plus at most one *"make that a standing rule?"* confirmation when today's transcripts showed you stating one — your yes writes it to the master file; it is never written unconfirmed |
| Midday digest | 12:30 local | everything queued **since the last delivered brief** — and only if there are at least `SOTTO_DIGEST_MIN` (default 8) real signals from people you know; otherwise silent. Nudges Sotto raised itself never count toward that 8 (they aren't people), though they may ride along in the message |
| Nudges | any time, subject to every rule above | one short message with a reply already drafted — and an offer to act on it: an email asks ("want this in your Gmail drafts?" — on your yes it saves a real, threaded Gmail draft you send yourself), every other channel gets a one-tap link |
| Proactive nudges | a meeting starting in ~45 min you haven't prepped, a commitment due today, one chase for something you're owed, a birthday (`SOTTO_BIRTHDAY_LEAD_DAYS`, default 3, days out and on the day — unless a brief already delivered today), a plain question about an ask nobody answered twice, an offer to tidy a heavy pile | the same thing — and the whole push spends **one** unit of the same daily budget, queues to the same digest when it's gone, waits out the same mutes and in-meeting hold, and lands in the same ledger |

The digest window is anchored to the brief that actually *delivered* — the deliver-once claim
advances the stamp when it wins (`brief_marker.py` on a local install; the receiver's send seam on
the cloud, where the claim happens at the moment of delivery) — so the 12:30 digest can never
repeat what the morning brief just covered. Briefs and nudges are always **drafts**; Sotto never sends for you — a
Gmail draft is the most literal version of that promise, since it sits in your own drafts folder
until you press send.

## Delivery — what happens after Sotto decides to say something

**Nothing Sotto says is marked delivered until the channel says so; what fails waits its turn
instead of dying.** Deciding to send and actually sending are two facts, and the second one used to
be a hope: a gateway that was down for ninety seconds threw away the words, the interrupt budget
and the tokens that produced them, and left an honest "failed" receipt in place of the message.

The send seam also strips machine markers (`<!--id:…-->` / `<!--meeting:…-->`) from every outgoing
message: they are dashboard-and-tap-link plumbing the composer keeps in its archive artifact, and
no run's choice of what to print can leak them to a phone.

**A brief is sent once: the send seam itself claims the deliver-once marker, so a run that forgets
its claim can no longer double-deliver; the second copy is receipted `superseded`, never sent.** Two
paths compose your morning and evening brief — the schedule and the Mac's wake-push — and the day
belongs to whichever of them gets to the marker first. **Both are the same lane now:** the schedule
lives where it always did (`crons.json`), but the receiver fires it on its own clock rather than the
agent's, so a scheduled brief is written down before the first send attempt and retried like
everything else — the 6:30 brief no longer disappears because the channel was down at 6:30.
That marker used to be claimed only because
the skill was told to; on August 30 a run wasn't listening and the evening brief arrived twice, so
the claim now lives in the machinery every message passes through rather than in an instruction.
And it is claimed **at delivery, not before**: a run that claimed early and then died before its
words reached the outbox used to leave the day marked delivered with nothing queued to deliver it —
so on the cloud only the send seam writes the marker, and the skill's own claim just answers.
The wake-push also stops composing a brief it would only have to throw away: a wake that lands
within **10 minutes** of a brief's scheduled time presumes that run is still writing (a compose
takes three to five), and folds its fresh data into the local snapshot instead of starting a second
one. A wake outside that window still composes, because then the scheduled brief really is missing.

So every message Sotto composes is written down — with its own id — **before** the first send
attempt, and only the channel's acknowledgement moves it to delivered. Anything else waits and is
tried again: the outbox retries every minute, backing off 1 → 2 → 4 minutes and doubling to a
fifteen-minute cap, and the same message is never sent twice (the id is the words themselves, so a
repeated attempt is recognised as the same message rather than a second one).

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
- **The receipt names the bytes without keeping them**: it carries `payload_sha256`, the hash of the
  exact text that left, so what was sent can be checked against what you were shown while the ledger
  holds not one readable word of it.
- **A scheduled run cannot send mail or touch your calendar at all** — the refusal is in the code,
  before anything reaches Google, and it is recorded like any other attempt.
- **When Sotto asked in one place and you answered in another, your yes is bound to the exact
  content it was given**: the acting verb recomputes that hash and refuses on a mismatch, so text
  that changed after you approved it never goes out.
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
| Delivered briefs and their per-day markers | **60 days** | the same clock as the snapshot each was built from |
| Staged payloads and a crashed run's leftovers | **7 days** | read by the run they were staged for; a week collects the ones whose run died |
| The nudge-dedup stamps | **30 days** | only today's is ever read |
| What you did with each draft (`outcomes.jsonl`) | **90 days** | the learning loop re-reads the whole file after every brief; a quarter is all it can use |
| The brief log | **last 5 MB** | truncated in place, because a running brief holds it open |

**Nothing in your memory is ever auto-deleted.** The graph (people, companies, `master.md`), the
continuity ledger, your style profile, your preferences, the golden corpus, your credentials and any
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
