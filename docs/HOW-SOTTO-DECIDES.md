# How Sotto decides when to interrupt you

After the existing admission rules, a fixed procedure selects one primary notification per push,
renders a template or makes one direct structured writing call. Omitted items retain coverage.
Transient writing failures wait for queue backoff; malformed copy gets one repair. Deterministic
reminders survive writer failures. Scheduling remains a question until all-calendar availability
can be verified. Selected-calendar gathering reports per-calendar failures; an unread calendar or tentative invitation cannot establish complete availability. No new daily spend limit is added.

An external calendar, Contacts or Gmail action with an uncertain outcome stays held without
repeating the effect. Sotto sends
one durable notice asking the owner to check the target; confirmed notice delivery releases only
that input so unrelated conversation can continue. An uncertain notice itself is never resent
automatically. Definitive permission refusals before calendar/contact provider contact are rejected.
Managed monthly-budget notices explain the next UTC-month reset; lifetime limits require operator
action. These notices do not change the cap, source consent, or nudge eligibility.

In managed deployments, connecting Granola opens its source gate only after OAuth succeeds;
disconnect records explicit revocation before removing the credential. At startup, an older linked
Granola credential can repair only a missing gate row in an already matching tenant capability
file. Missing, malformed, foreign-tenant and explicitly disabled state remain closed.
Granola’s scheduled MCP and REST gathers follow redirects only within the original HTTPS origin.
If the provider points to another host, port or protocol, the gather reports a failed read.

| Operation | Attempts | Native Gemini output ceiling | Thinking override |
|---|---:|---:|---|
| `notification` | 2 | 4,096 | low |
| `triage` | 2 | 2,048 | low |
| `digest` | 2 | 4,096 | low |
| `memory_extract` | 2 | 16,384 | low |
| `memory_curate` | 2 | 8,192 | low |
| `brief` | 12 | 65,536 | low |
| `brief_extract` | 6 | 65,536 | low |
| `brief_critic` | 3 | 65,536 | low |
| `brief_revise` | 3 | 65,536 | low |
| `followup` | 3 | 8,192 | low |
| `meeting_prep` | 3 | 65,536 | unchanged |
| `research` | 4 | 8,192 | low |
| `web_search` | 2 | 65,536 | unchanged |
| `web_fetch` | 2 | 8,192 | unchanged |
| `deck_read` | 2 | 32,768 | unchanged |

Gemini 3.8 Flash uses the existing **65,536-token combined thinking and answer ceiling** for every brief stage, with `thinkingLevel: low`. Visible brief size alone cannot set a safe ceiling: a recorded revision used 1,623 visible tokens plus 59,607 thinking tokens. That historical receipt does not establish usage under the current low-thinking setting. A truncated extraction uses its existing second semantic attempt immediately with identical source context, without a queue delay or another gathering pass. Safety refusals are not retried as truncation. The receiver retains its 600-second composition subprocess timeout; token headroom does not guarantee completion before that deadline.

Model-work receipt retention is stated in [DATA-FLOW.md](DATA-FLOW.md#everything-written-to-disk-and-for-how-long). An unknown worker claim has a **900-second** lease, with at most **2** interruption replacements per operation. Artifact families use **256** lock shards. Notification context is capped at **48,000 characters**, including instructions and schema.
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
  standups never mint a change. Shared-calendar prep, taps, holds and change nudges require the connected
  owner's address as attendee or organizer; Google's `self` on a colleague's copy is not the owner.
  Free/transparent meetings qualify for post-meeting follow-up only when the owner explicitly
  accepted or tentatively accepted as an invitee. Owner-organized FYIs and free holds stay quiet;
  they never reserve busy time. Canceled, declined and working-location events remain excluded.
  Other visible events remain context only. Decline nudges also require
  exactly one other human: group RSVP changes and meetings merely visible on a shared calendar
  stay quiet. Room/resource attendees do not count as people in either the calendar or prep lane.
  The last complete calendar comparison and acknowledged changes survive restarts. Failed or
  partial reads cannot prove a cancellation. Successful rows from partial reads still support
  holds and taps; a removed calendar does not imply its meetings were cancelled. Reconnecting Google
  invalidates a saved calendar selection without disabling independent Gmail or Mac sources.
  Default discovery reads visible readable calendars (at most 50); hidden calendars require explicit
  selection. Bounded/truncated discovery is partial, never a complete empty schedule. Calendar permission removal, an account change, or
  an unknown account identity starts a fresh comparison once the account is known; disabling
  calendar nudges advances it quietly without replay.
  Explicitly labelled context/prep notes never generate a meeting-change notification, including
  after their meeting moves. A missing entry is described as removed from the calendar; only an
  explicit cancelled status proves cancellation. A replacement event ID is joined only through
  a unique shared iCalUID in both observations, with an exact originalStartTime for recurring
  instances; title similarity never joins events. The detector supplies dated old/new times and its exact copy survives composition.

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
| **Explicit owner timer** (unconditional, admitted by the owner reminder tool) | Existing receiver heartbeat, every 60 seconds | The saved action is delivered through the same durable outbox at or after its due time, normally within one tick and only within 15 minutes of its requested time. Quiet hours, meeting holds and interrupt budgets do not postpone an expressly requested timer. Current owner/route proof and cancellation are rechecked before contact. Missed-delivery notices expire 24 hours after the requested time, use local dates, and batch up to five reminders in one message. Unlike the requested timer itself, these status notices wait through quiet hours, snoozes and a nudge budget set to zero; they never spend the budget. Each notice keeps its own deadline: when an unattempted or definitively rejected batch expires, only its expired members end; still-fresh members are regrouped. Unreserved expired notices retire even when owner-route checks are unavailable. A held notice keeps the timezone used when it was composed and survives midnight within its own deadline. Uncertain sends stay held. Conditional and legacy intentions keep the proactive rules. Older proactive reservations remain held rather than risking a second send. |
| **Proactive watcher** (intention · meeting prep · commitment · chase · birthday · handoff) | `proactive_scan.main`, the `*/15` cron | `triage_event.triage` — the tick goes in as ONE bundle of synthetic `source: "proactive"` events, classified by `_classify_proactive` (snooze → quiet hours → mutes; the nudge's kind is its class) and then through every gate below. The bundle that comes back is what the watcher delivers |
| **"Nudge me now"** (you promote a held item from the dashboard) | `dashboard._post_cadence` → `receiver.run_promote` | `triage_event.promote_one` — the same `_valve_candidate` rule the valve uses; the explicit request bypasses the unsolicited budget |

An admitted pre-meeting reminder carries the known role and countdown, plus one or two short
sentences (at most 60 words and 500 characters) of grounded relationship context, such as where
things left off, a dated development, why this meeting exists or a commitment later evidence still
supports, written by the existing bounded notification writer from stored person memory, the
invitation, current conversation text, recent correspondence and open items joined by identifier
(a shared first name joins only when one side has no identifier). With no such text, a held or
failed writer, or a writer already spent on another candidate in the same job, it is the basic
reminder; the scanner's unchecked open item is never appended to it or carried into the offer a
yes answers. The selected attendee reuses up to five dated excerpts
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
A model-selected brief prep action can render one source-titled offer even when the model omitted
its prose paragraph. This fallback requires one exact, allowed calendar occurrence and a sole
eligible external attendee; the receipt uses that attendee's source email. A missing calendar name
never makes the model's guessed name authoritative. Conflicting addresses, incomplete coverage,
multiple selected occurrences and ineligible meetings remain withheld. The accepted-delivery receipt
and ordinary prep admission still own a subsequent “yes”; rendering an offer performs no research.
Private prep evidence follows the same identity boundary in focused and automatic preparation:
an exact attendee email may use its explicitly linked phone or canonical record. When an attendee
has an email, a name match cannot attach another person's private texts, knowledge or obligations.
Email-less attendees may use an unambiguous contact-name match. A known contradictory identifier
cannot be overridden by a matching name or canonical ID.

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
DocSend hosts and preserves the host through the email gate and page reads. An explicit owner
cancellation, including “dont open”, blocks a direct DocSend read before any deck contact or viewer-email submission.
Direct reads use the same conservative refusal check as ordinary URL reads. If “no passcode needed” blocks an intended read, the owner can send “read” with the link again. A verification gate
says that the cloud reader cannot use the Mac browser session, and asks for a PDF or a link without
verification rather than promising browser sign-in will unlock cloud access.

The watcher suppresses an imminent prep only after provider acceptance records the exact calendar
event ID and start. Research is reusable context, not evidence the user received an offer or prep;
moving the meeting creates a new occurrence and makes it eligible again. A completed full prep can
also retain a bounded location baseline under its provider-proven owner route. Within the existing
prep lead window, a different nonempty location produces a short factual update without model
work or another prep offer. Unchanged locations, blank/removal states, legacy receipts and accepted
offers stay silent. Source consent and the same route must still hold at send. The exact occurrence
and expected location must match a fresh complete current-day cache observation. The candidate
uses that cache observation time, corroborated by the gathered event location; the scan clock does
not pretend a new cache observation happened. A stale or missing
cache withholds this update; future-day meetings wait for their day to be covered. The cache proves
the latest observation, not edits made after it. A newer accepted full prep replaces the baseline;
only an accepted update advances its last-notified location.

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
  (default 20 min). Only escalations and calendar changes are exempt.
- **Quiet hours and snooze always win** — no nudges between `SOTTO_QUIET_START` and
  `SOTTO_QUIET_END` (default 21:00–07:00; a missed call from a VIP is the one carve-out), and an
  explicit "be quiet until…" snooze holds *everything*, missed calls included. An expressly
  requested owner timer is the one exception (see the producer table); unsolicited nudges never
  bypass. **VIP** is whoever
  you said it is — the stated list (`preferences.explicit.vip_people`, written by "make Sarah a
  VIP" in chat or the toggle on her dashboard page) is checked first, then one fallback: a typed
  `family_of` relation in their file. Both are a choice, yours or the graph's; **message volume is
  not one.** A top-of-queue relationship-pulse priority used to qualify too — and since that
  priority is interactions × days-waiting × type-weight, four messages and three days of silence
  bought a 3 a.m. interrupt, off a file up to a week stale. It was removed (Sep 2026); the pulse
  still reaches Tier 1 as the sender one-liner's relationship signal, which is context, not a gate.
- **In-meeting hold** — while you are inside a busy timed calendar event with at least one other human,
  would-be nudges queue as `meeting_hold` — except missed calls, escalations, calendar changes
  and imminent meeting prep. Prep still obeys quiet hours, snooze, mutes and cooldown, but not the
  daily interrupt budget — it can arrive during the previous meeting so back-to-back prep does not
  expire before reaching you, and a spent budget must not silence "your pitch starts in 20 minutes"
  either: the valve never promotes a nudge Sotto planned, so a prep demoted to `budget` had no way
  back at all. Solo blocks and all-day events never hold, and a calendar
  cache that is stale or from another day never holds — Sotto won't act on a stale belief about
  where you are. Events marked “Show as free,” working-location notices, cancelled/declined events
  and shared-calendar entries without your participation never hold notifications or block proposed
  scheduling slots. Free entries stay visible; post-meeting follow-up requires an explicitly accepted or tentative
  invitation, not an owner-organized hold. Your
  primary-calendar focus and out-of-office blocks still reserve time when marked busy.
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
  watcher all wait for the ACTIVE channel (`SOTTO_CRON_DELIVER`, which the supervisor sets to
  `photon` or `telegram`) to actually be linked before dispatching: Photon's authenticated owner DM
  receipt, Telegram's captured chat id. Any other channel value holds every nudge.
- **An explicit Telegram brief stays with its original owner** — requested morning/evening briefs
  and their failure notices require the original protected owner/bot pairing, runtime route and
  source consent revision through queued work and delivery; a changed binding holds the work,
  and a lost receipt is inspected without sending the brief again.
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
  which is the job. Quiet items may be shown when readable space permits; a short text-only brief
  uses the existing 200-word card budget and retained-text bound rather than requiring four cards.
  A reorder-only revision that changes an approved exact task line is rejected in favor of the
  reviewed draft; changed prose never inherits that line's approval.
- **"The brief already told you" is decided by identity, not by names** — a loop counts as covered
  when the brief's own tap marker carries that person's identifier, not when their name appears in a
  sentence. Two people share a first name; a line about one of them must never silence the other's
  overdue ask, and "Maya" in a paragraph is not proof that Maya Chen was told. The same fact governs
  the chase, with one exception: only a **first** chase (nothing chased yet on that loop) is held
  back when today's brief already named it — a loop chased once is urgent by contract and the brief
  names it every day afterwards as a still-open line, so treating that as "already told you" a
  second time would mean the loop never reached its two chases or the hand-off question.
- **An email you sent that nobody answered is context, not a debt** — the gather asks Gmail, for
  each thread you wrote on 3 to 14 days ago, whether the last word is still yours (20 threads at
  most, the same 3-day clock the chase uses), and the brief model sees the ones that went to
  somebody in your contacts or your graph — never a stranger, a no-reply address, or a calendar
  RSVP your Gmail sent to an organizer ("Accepted: …", "Updated invitation: …"). Silence alone
  never mints a `waiting_on`: the model may propose one, or a `loopUpdates` change to a tracked
  loop, only when the source shows a specific outstanding request or deliverable, with the
  recipient, the original date and the message evidence.
- **They replied, they just haven't delivered** — any inbound from the counterpart of something
  you're owed (a text, an email on the thread, a call — answered or missed) restarts that loop's
  chase clock without counting a chase: to the date they named ("by Friday", "the 16th of July",
  "tomorrow", "end of week"), else the usual 3 days. Only substance closes the loop.
- **A meeting you declined is not on your day; a meeting you haven't answered is an ask** — the
  gather reads your own answer to each invite. A declined event never reaches the brief, a research
  call, the prep nudge, or the in-meeting hold. An unanswered invite with somebody else on it
  becomes an `rsvp` ask once it is within 48 hours — minted by code, and closed by the calendar
  (you answer, or it passes), never a ledger row. An FYI/context notice or drill, or a hold or
  prep block with nobody else on it, never becomes an `rsvp`; it still shows in Coming Up.
- **New promises still require observed source proof.** Required `newObligation` actions bind their
  source ID, timestamp, quoted clause and verified counterpart before capture. Curly/straight quote
  glyph differences can be recovered from the observed text; the ledger retains that original
  wording. Case, words, negation and message-part boundaries are not relaxed. Capture uses the same
  collision-aware Contacts/graph aliases as completion. An unsupported first capture is withheld,
  never downgraded into an unproved task; `capture_outcomes` reports verified/rejected source binds
  and normalized quotes in the resolver result and diagnostic log.
- **Reconcile before curating.** Extraction always returns `loopUpdates` (empty when no update is
  supported). It compares recorded tasks with original messages before choosing what earns a
  visible line. A non-urgent task omitted from Already Handled still needs its completion
  proposal; a substantive answer can fulfill a reply request without saying “done.” The existing
  critic review and identity/time/quotation checks still decide whether the proposal is accepted.
- **Contact is not completion.** The brief model may propose a `loopUpdates` change only for one
  ledger ID and revision, backed by an original source message ID and verbatim quote. Code verifies
  the counterpart, direction, original timestamp and quote against the snapshot. A generic reply,
  call, calendar attendee, unrelated link, or appearance in Already Handled cannot close a debt.
  A closure applies only after the brief's critic explicitly approved that exact update, so one the critic
  skipped, could not run, or could not fit stays open. If revision introduces a closure or changes its
  identity or evidence, those new proposals receive one bounded review through the same source-bound
  critic contract, within the existing critic allowance. This review cannot trigger another revision;
  unchanged rejected proposals are not retried, and prose `mark_handled` advice never grants approval.
  A rejection applies to its exact proof. Review of changed proof includes the prior bounded source
  review, verdicts and summary, so a rejected claim cannot receive a blind second attempt.
  A quote under 12 characters must be the entire
  observed message, not a selected fragment, and still needs that task-specific approval.
  A verified connected phone call in either direction may settle a callback only, using the full native call row and
  current source authority. Unanswered attempts, group calls and calls unrelated to a deliverable do
  not settle it. WhatsApp's current call schema does not prove connection, so only a relevant
  reviewed message can settle a WhatsApp callback. Open risk: the native CallHistory answered flag
is not known to distinguish a voicemail pickup from the person answering. Capturing an observed missed-call request preserves its original event time so a
  relevant reply before the next brief can count. Legacy rows without that proof keep their
  conservative capture-time cutoff. Shared Contacts or graph aliases cannot prove cross-channel identity; duplicate copies of one
Contacts card (same name and first email, one copy holding every alias) are one person, not a collision.
  Native To/Cc recipients and Bcc recipients on the owner's sent copy can establish delivery;
  inbound Bcc and body mentions cannot. Typography-only quote recovery happens before critic
  review and receipt hashing; changed words cannot reuse approval.
  An email closes a group's debt only when it went to everyone on the group's roster.
  Missing or ambiguous evidence leaves it open; explicit user corrections remain authoritative.
  The gather gives carried-over loops one bounded refresh of at most 6 exact targets,
  10 requests, 24 messages and 48,000 characters, admitting no new read after 20 seconds.
  Exact Gmail threads and verified direct iMessage handles share those limits; handles are
  deduplicated across sibling asks. iMessage reads run concurrently with the Gmail lane and
  receive the remaining gather timeout, rather than adding a sequential read per obligation.
  Each native handle read returns at most its newest 24 rows within the reader's 90-day limit;
  only messages after an original ask and no later than the pass are admitted. Oversize texts
  are omitted intact. Original-source and iMessage grants remain required when these rows are
  consumed. The same resolved-name person mutes filter supplemental evidence from both the
  composition prompt and critic manifest. Historical texts render separately as `LOOP_EVIDENCE`, with their original source
  IDs, timestamps and source dependencies, for the composer and critic. They remain available to
  existing obligation validation, but are excluded from current reply triage, activity signals,
  new-ask/reopen capture, writing-style samples and the brief's new-fact binding. A current native
  row with the same source ID stays ordinary daily context. Text failures cannot mask a Gmail
  quota failure; optional local-merge failure leaves the ordinary gather available. Valid text
  can displace trailing Gmail evidence within the same shared capacity, with omissions recorded
  in the partial receipt. This adds bounded evidence gathering, not a complete history scan or a
  closure decision. A slow last Gmail request can still exceed the admission window; no new hard
  end-to-end wall-time guarantee is claimed. Partial/failing reads never prove nothing happened.
  WhatsApp has no equivalent supplemental handle refresh in this path.
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
- **Open-loop queries balance due work and undated obligations.** Both directions interleave
  two overdue/due-today rows (earliest deadline, then age) with one undated row (oldest first).
  Remaining undated work precedes future deadlines. The same ranking is reapplied after person
  matching for notifications, so a three-line tap and a 40-row follow-up retain undated replies. Relative or invalid dates stay unknown rather than inventing urgency. The
  explicitly requested parked list uses the same ordering without making those tasks active.
  This only orders the existing permitted rows; it does not change counts or completion state.
- **Obligations never expire from age, a passed deadline, or an unreachable counterpart.** Something
  you're *waiting on* closes only when they
  actually deliver (a substantive reply — a link, a file, or real text, never a bare "ok" or a
  promise to send it later), or after `SOTTO_CHASE_AFTER_DAYS` (default 3) of silence it becomes one
  short, warm chase — at most one chase nudge a day, at most 2 per item. A chase is only counted
  once its reminder is actually delivered to you, so one that quiet hours or a snooze swallowed is not one of your
  two — and a chase that was stamped but never delivered sends that loop to the back of the next
  day's pick (`chase_stalls`), so one perpetually-blocked loop cannot hold the whole chase lane.
  After the second, Sotto stops and asks you plainly, by name: *"I've reminded you twice about
  Maya and the contract — want a follow-up draft, or let it go?"* — **once**, and **on its own clock**: the question
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
  meeting time; it does not expire or resolve ordinary obligations. Reminder counts never prove
  contact with the other person: handoffs, cleanup suggestions and evening summaries describe
  reminders to you, and only supplied sent-message evidence can establish an external follow-up.
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
- **A task you added in your own words stays provably yours through Sotto's bookkeeping.** Its
  content digest is carried forward (`continuity_resolve._BOOKKEEPING_FIELDS`) when a write changes
  only delivery receipts or the chase, park or snooze clocks, so a delivered
  brief, a chase or a park/keep cycle never hides it from owner-only views or blocks closing it.
  The dashboard refuses deadline changes to a proved owner task, including a duplicate add: its
  generic writer has no admitted owner event to authorize a new content digest. Dashboard-added
  tasks use that generic writer too; they appear in the dashboard and ordinary loop reads, but
  are not stamped as owner
  tasks and do not enter chat owner-only views. Anything else - new wording, a counterpart the
  graph would adopt, source evidence, a closure through an unproved writer - leaves the old digest,
  and owner-only views withhold the row from
  then on (said once on stderr). A withheld row never fails the daily pass; the graph never
  rewrites the counterpart of a proved row.
  Automatic message-evidence closure currently applies to source-backed tasks, not isolated
  owner-authored tasks. The latter still require explicit owner completion. Automatic parity would
  need an audited transition retaining the owner origin plus the closing source and its consent
  dependency; refreshing an owner-only digest after adding source content is not permitted.
- **Recorded obligation history uses canonical source grants only.** A Google Contacts capability
  does not block Gmail/iMessage history and never grants the separate Mac Contacts source. Malformed
  captured authority stays unavailable; every original and closing source still needs its own grant.
  Empty history proves only that no matching permitted rows were returned, never that canonical
  records are absent or nothing is outstanding; it conveys no withheld-row existence or counts.
  Retired merge/retarget rows are bookkeeping, not extra dismissed obligations. An unreadable person
  identity keeps the whole targeted lookup unavailable rather than guessing that it is unrelated.

- **One completion message accounts for every selected task.** Native single-task closing requires
  an exact owner phrase uniquely matching one current owner-authored task and the whole completion
  clause, including simple conversational endings such as “gamma is done too” or “done now.”
  Negated or uncertain completion stays refused. Missing selection or selecting only the first of several completed tasks returns repair
  guidance before any action is reserved. Two or more explicit completions use one aggregate
  `owner_task_resolve_many` action with per-item receipts; explicitly open peers stay open. A partial
  or uncertain receipt acknowledges only the confirmed items. Existing saved single-close requests
  remain readable for inspection and recovery.
  Numbered replies require exact unique current owner number tokens, saved row proofs and the existing
  whole-request completion check. A redundant trailing “close them” also works without “please” or
  “you can”, so “1 and 3 are done. close them” is accepted. Other trailing wording must still pass
  the existing check; arbitrary natural-language meaning is not inferred from number spans alone.
  A short numeric answer to the accepted open-list question “which ones are done” can resolve its bounded numbers without repeating a completion verb. It uses one explicit resolve clause that consumes the whole owner message. Numeric separators and a final period or exclamation mark are allowed; a bare numeric question stays unchanged; an “All?” prefix additionally requires a complete snapshot and every displayed index. Other words, negation, partial all claims and stale cleanup-list questions retain the explicit completion requirement.
  The same cleanup tool accepts explicit completion of all displayed tasks through a typed all selector with the exact owner collective phrase. It derives targets only from the accepted snapshot and never includes unseen tasks. An explicit reference such as `all of this`, `all of that`, `all of these`, `all of those`, or `all of them` can complete a nonempty displayed open-task page even when more tasks are not shown. Bare `all` still requires a complete list or an explicit accepted subset. It also accepts a typed all-except selector on a complete displayed open
  list (global or an explicitly requested subset), or explicit scalar/range completion and leave-open clauses. The model supplies the owner's
  exact selector and references; the receiver derives and checks the selected indices against the
  frozen accepted list. Excluded and explicitly open rows are untouched, not passed to the keep
  writer. All-except requires an unambiguous whole completion claim and explicit exceptions;
  ranges must be ascending, in bounds and nonoverlapping. Every resolve clause retains the exact
  completion check across sentence recursion and refuses unreferenced numeric residuals.
  A requested subset must display every requested anchor in its exact order; unrelated global
  remaining counts never expand this scope. An unkeyed truncated global list cannot use all-except.
  Unmentioned rows stay unchanged.
  The first subsequent owner turn consumes the list's selection opportunity. A typed selection that fails after its accepted snapshot is validated can retain that same snapshot for one repair, only when the runtime’s exact clarification was accepted on the same route and source revision. An unrelated turn, a newer list, an uncertain clarification delivery or ordinary assistant prose cannot reuse that repair. A later selection otherwise needs a new list. Typed spans bind this supported contract,
  not arbitrary natural-language meaning. Quoted, conditional or unconsumed wording is refused.
  A subset uses the same list tool with ordered canonical task IDs. The receiver supplies every
  displayed row and binds that order to the accepted reply; ordinary model prose cannot authorize
  a numbered change. Missing or stale list proof stops changes. An all selector without an accepted snapshot returns a fresh canonical list in the same turn; other selections ask for a fresh list and stop before further tool work, on both native and shared-script paths.
  After a durable task preview, recorded receipt or rejection, the coordinator supplies the exact
  acknowledgement or review without another model restatement, on native and admitted shared-command
  paths. Repairs and uncertain effects keep their existing holds. Numbered cleanup uses the accepted
  row proofs directly, retaining current source checks, receiver target revalidation and fresh
  post-effect readback. A wholly applied resolve-only batch larger than five items gets one
  completion-count sentence; smaller, mixed, partial and uncertain results retain per-item detail.
  Content-free task timing records acknowledgement/dispatch durations and
  elapsed admission/receipt-to-dispatch result; provider acceptance does not prove owner reception.
- **Historical date wording cannot block a task list.** Open-task and stale-loop lists share the
  validated calendar-day projection used by ordinary loop reads: relative or invalid deadlines stay
  unknown, while a valid explicit date keeps its day, including a legacy weekday annotation. Listing
  preserves each task and its original ledger content and proofs; it never guesses a relative date.
- **A reminder tied to a loop follows that loop.** An ordinary one (`schedule_wakeup.py create
  --anchor-key`) fires while its loop is open and cancels once the loop is closed, parked,
  snoozed or gone (pruned) - the same rule whether the loop was hand-added or captured from
  iMessage, WhatsApp or Gmail. A loop whose content provenance is proved must also pass the
  protected check (an owner task waits through a snooze instead of cancelling). An owner-bound
  condition from the reminder tool fires only while its exact loop is active under current
  grants and cancels only once that loop is terminal. One unreadable ledger file holds no other
  reminder: a malformed file is no loop anywhere, and a file the protected reader will not trust
  (duplicate keys, oversized, a symlink) only stops an absence from counting as "gone".
- **Protected reads are bounded for years, not weeks.** Because parked rows and receipt-bearing
  closed rows stay on disk, a whole-ledger protected read accepts up to 16384 rows and 64 MiB
  (`protected_ledger_io.MAX_FILES`, `MAX_TOTAL_BYTES`) - about a decade at a heavy pace - and is
  unavailable, never partial, past that. Reminders and offer snapshots read only their loop's
  rows, so their bound is what they keep, not the ledger's size. When a loop nudge's protected
  read is unavailable or partial, the batch still composes from the ordinary loop view and binds
  no offer to loop sources.

## Managed Cloud pilot activation

In managed mode, scheduled morning/evening briefs and Bridge wake-triggered briefs wait until the owner has texted Sotto and at least one consented context source is connected. A sleeping Mac and a source with zero events are not disconnected sources. Missing capability state means zero sources. After the first owner DM, the receiver delivers one `status:no-sources` notice through the nudge outbox and remembers its acceptance on disk; no daily quiet-day or sources-unavailable brief is generated. Manual run-now remains an explicit action. Fresh self-host first briefs also wait for context and the linked delivery channel; local readiness needs a valid successful extraction rather than a Bridge connection alone. Established self-host installations retain their existing recovery behavior.

The managed model credential is a lease: the receiver's heartbeat renews it once it is within 72 hours of expiry (`model_lease.RENEW_BEFORE_SECONDS`) and, after a failed attempt, retries hourly (`model_lease.RETRY_SECONDS`); a failed renewal never invalidates the current credential. Before the first runtime starts, provisioning requires more than one hour before its configured expiry. Later recovery checks the effective renewed lease through the authenticated proxy capability endpoint, along with the existing finite-budget and remaining-allowance checks.

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
(up to 400 characters each — `personal_context.CONVERSATION_TEXT_CHARS`, the digest and prep
preview cap; Tier 1 uses its own 4,800-character budget), including outgoing answers, before the six-item delivery cap.
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
| Morning brief | 6:30 local (or the moment your Mac wakes past 7am) | opens with the time of day and the date in your zone ("Good morning — Saturday, September 6"; good afternoon when the wake path composes it after noon), then your day across messages, email, calendar, plus open loops. At most one accepted morning brief per day. After setup is established and the source/channel gates permit delivery, a sleeping Mac can use the last saved snapshot; fresh setup waits for context and a linked channel, and when the Mac wakes later its fresh data is folded into that snapshot instead of composing a second brief — the nudges and the midday digest surface whatever the morning brief couldn't see |
| Evening brief | 17:30 local | opens with "Good evening — <the date>", then accountability, tomorrow, follow-up drafts, and **What moved today**. On Friday only, it adds a short review of up to three stale or repeatedly surfaced obligations plus a count/link for the rest and a count of parked loops. No stale or parked items and no eligible candidate means no review; a stated `weekly_review` section mute suppresses it. It never silently expires them and sends no separate Sunday review message. **At most one review question**, shared with a *"make that a standing rule?"* confirmation when today's transcripts showed you stating one; that confirmation takes precedence. Explicit "mute Bob" instructions still work |
| Midday digest | 12:30 local | reviews what queued **since the last delivered brief** when at least `SOTTO_DIGEST_MIN` (default 8) known-sender ambient/deferred events justify review; sends only relevant items, otherwise silent. Nudges Sotto raised itself never count toward that 8 (they aren't people), though they may ride along in the message |
| Nudges | any time, subject to every rule above | one short message with a reply already drafted — and an offer to act on it: an email asks ("want this in your Gmail drafts?" — on your yes it saves a real, threaded Gmail draft you send yourself; a `mailto:` is removed at the delivery seam, so one can never reach you from a scheduled run), every other channel gets a one-tap link — and a tap link is kept only when its recipient validates (a real phone of 7-15 digits, a Messages short code, or an Apple ID email), so a model-masked or non-recipient link (`imessage://+141****3682`, an RCS business handle, a group identifier) is removed at that same seam while the draft text around it stays, because a dead link is worse than none |
| Proactive nudges | a meeting starting in ~45 min you haven't prepped, a commitment due today, one chase for something you're owed, a birthday, or a question about an ask nobody answered twice | the whole push spends **one** unit of the daily interrupt budget and obeys the shared mutes, quiet hours, snooze and effective nudge cap. “Fewer nudges” halves the cap, “no nudges” sets zero for every unsolicited path, and “more nudges” restores the configured ceiling. Direct requests, briefs and digests remain available |
| Weekly pulse | Mondays 9:00 local | who is waiting on you and who is going quiet, from six weeks of messages, calls **and email** (a mail you sent is a touch to each recipient, a mail you received a touch from its sender — for people in your Contacts or graph only), with the people you've fully lost touch with ranked below — someone is shown as fully lost touch only when every channel you've used with them was read in full this window; when one was not, the pulse says some history may be missing instead of guessing |

The digest window advances after provider acceptance, to the brief's original source cutoff.
A delayed send therefore leaves later messages eligible for catch-up. The daily marker only chooses
which run may send; it does not claim that its content was accepted. A successful silent digest
closes the window it reviewed, while a failed relevance review preserves it for retry.

The Learn step remains **one command** (`learn_step.py`) with two execution phases and one
`briefs/<date>.<kind>.learned.json` receipt. Knowledge and continuity writes are attempted before
delivery; a failed write stays in the durable learning follow-up rather than suppressing a valid brief.
Draft outcomes, voice learning, meeting-note learning and Contacts synchronization run as a durable
background follow-up; their failure is visible in the receipt and cannot suppress the brief.
The receipt remains partial while follow-up work is queued or an item was rejected. A draft with no
observed response after its matching window becomes **unknown**, never a presumed failed send or
rejection. A later-ingested send from inside that original window can still establish its outcome.

The brief projects only evidence-verified updates to an exact open task before presenting its final
handled/open sections. The durable resolver repeats those checks under its lock. Identity, source
permission, message direction, chronology, quoted evidence and task fulfillment must all agree. An
email carrying the native `DRAFT` label never proves fulfillment, even if another sent flag conflicts;
malformed label data is held for review.
An ambiguous older paragraph without a task marker cannot safely close or hide a different task
involving the same person. Older open items show when they entered the ledger. The critic sees the
source-derived schedule and birthday evidence, and its draft score is kept separate from final
deterministic validation. Mechanical revisions use a bounded evidence manifest; semantic changes
retain the original evidence context.
Unattended briefs and nudges propose actions. Sending email or changing a calendar still requires
the separate action authorization gate; a suggestion is never approval.
When a brief has one source-addressed reply task, it may add “I can draft a reply.” beside that
task. This is an invitation to ask for drafting, not a saved draft or an accepted offer that a
bare “yes” can execute; Sotto needs an unambiguous request and still requires review before saving.
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
When adopting a pre-existing installation, a brief marker is only a claim: matching provider
acceptance evidence must confirm that the scheduled brief for that day and kind was delivered.
An established installation is never reset automatically, even if old receipts were pruned and a
later scheduled brief failed. Fresh onboarding waits for scheduled work already accepted by the
durable queue or outbox. Welcome reservation and wake or cron admission share one receiver lock:
an arriving wake folds its context into the snapshot while welcome is pending, and a scheduled
brief admitted first gets its delivery chance before welcome. Fresh onboarding retains an
ambiguous hold through outbox pruning. Only an explicitly unaccepted terminal attempt permits a
fresh welcome; unknown outcomes are never treated as safe to replay. A first scheduled brief whose
acceptance is uncertain counts as that ambiguous case even while its outbox row stays pending, as a
gallery awaiting its receipt does: no welcome follows it, but later scheduled briefs still run.

Before gathering a fresh managed brief or its optional preparation, Sotto checks the authenticated,
content-free proxy capability. A finite allowance must admit one native reservation; this does not
promise enough for the entire brief. Unlimited managed routes and direct self-host keys retain their
existing policy. An exact proxy budget denial stops composition retries immediately; authentication,
network and unsupported-capability errors are distinct. A saved useful composition remains eligible
for delivery. Optional critic/revision failures do not discard an otherwise valid draft.
After an accepted first look, the existing background heartbeat can deliver one further discovery
once at least one eligible source's initial history window has been reviewed, while other sources
may still be processing or held. It gives the existing composer up to six cited, non-conflicted
saved observations, one per canonical person before a second from the same person. Muted names
and sender rules are excluded before selection through the existing preference check. The existing
reciprocal-relationship and explicit-VIP rules order those candidates; the composer chooses one
useful current opportunity against the current ledger and later replies, without a new importance
model. The same bounded handoff, including observation evidence and coverage, stays available to
the existing critic and revision pass. Its fixed discovery purpose is policy for those existing
stages, while observations and coverage remain untrusted data. Visible prose uses names and
relevant roles; canonical IDs stay in existing hidden markers. It is asked to name the source and observed date, discloses the bounded
coverage, and never turns an old fact into a new obligation. The handoff uses the existing initial-window coverage projection:
completed traversal with skipped, omitted, truncated or rejected observations remains partial;
missing measurements remain unknown. A valid saved fact can still be useful without claiming
complete learning. It distinguishes reviewed windows from processing, held, partial and
unknown coverage; it remains a single lifetime discovery, with later help offered through ordinary
briefs. No useful grounded observation means no handoff. Quiet hours, snooze,
source consent and the existing background allowance still apply, and the send spends one unit of the
shared daily interrupt budget. The existing work queue, onboarding record and outbox own admission,
restart recovery and acceptance; unknown sends stay held. Every send claim is recorded in the
onboarding record before provider contact, so an accepted send stays fenced even if its delivery
callback failed and its outbox row has aged out; only a definite non-send permits another day's try.
This is a progressive initial review, not a claim to have read every historical conversation.

A welcome held for allowance reports that account attention is needed. The denied admission does
not spend a welcome fault attempt, but queue and model-call evidence remain recorded. The existing
tick resumes only after availability is verified, and accepted or pending delivery takes precedence.
No allowance is increased or reset automatically.

Shared writing rules describe the outcome and next useful step in ordinary language; technical
detail is for a user who asks for it.

Every message is persisted **before** its first send attempt. The adapter requires structured
success and a provider message ID; an exit code alone cannot mark delivery. The outbox retries every
minute, backing off 1 → 2 → 4 minutes and doubling to a fifteen-minute cap. It rechecks source
permission and relevant loop/Calendar state before sending. Invalidated brief replacement is itself
retryable, so a temporary enqueue failure cannot lose the day's brief. Plain-text and gallery sends
with uncertain acceptance remain held for receipt reconciliation instead of automatic replay.
Provider acceptance does not prove device delivery or a universal exactly-once guarantee.

Waiting is not forever, and how long depends on what it is:

| | Waits until | Then |
|---|---|---|
| An explicit owner timer | 15 minutes after its requested due time, including downtime before enqueue | a definitive failed/expired/superseded delivery cancels the timer and queues a missed-reminder notice to its proved owner route (up to five reminders per message, each expiring 24 hours after its requested time); unknown sends remain held for a receipt, without a cancellation claim or resend |
| An ordinary real-time nudge | 30 minutes after the source event | it is dropped rather than delivered as fresh news after it stopped being fresh |
| An ordinary event waiting in the release valve | 240 minutes after the source event | the valve records one stale drop; the event can still ride the digest or a brief |
| A nudge held for a meeting | the meeting can run as long as it runs; after release it gets 240 minutes | it is dropped after that post-release window, so Sotto's own hold cannot consume its chance to arrive |
| A missed call | 240 minutes after the ring | it stays truthful about when the call happened or falls back to the digest/brief |
| A calendar change | the affected meeting's start | it is dropped once the change can no longer help before that meeting |
| A morning or evening brief | the end of its local day | it **fails visibly** — a day with no brief is something you should be told about |
| The weekly pulse (and the midday digest) with no channel to land on | four hours after its minute — the same catch-up window a brief gets, retried every tick | it is not sent; a channel that stays unlinked past the window loses that run, and the boot log says the channel was never linked |
| The midday digest | the end of its local day, which ends before the next digest window opens | it is dropped: tomorrow's digest covers what it would have said |

Missed-reminder notices use the same durable outbox and ordinary nudge retry budget. Their terminal
receipt is saved in the reminder log, preventing replay after outbox pruning. Unavailable delivery
can also prevent the notice from arriving; failed notices remain visible in the Record. Explicit
owner cancellations retain their existing command acknowledgement instead of a second notice.

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

Storage lifetimes, sweep protection and deletion are owned by [DATA-FLOW.md](DATA-FLOW.md#everything-written-to-disk-and-for-how-long).

The [daily retention slot](DATA-FLOW.md#everything-written-to-disk-and-for-how-long) also **rotates the chat session** (a silent call to the Node runtime's
private sessions API — nothing is broadcast, and there is no `/resume`). One sentence: a chat
session lasts a day, or until you send `/new`. Why: sessions reset only on
deploy since Aug 26, and a week's transcript of delivered briefs and nudges is material the chat
model will copy back into an unrelated reply — a persona rule against it lost to a transcript
carrying the same authority (Sep 1–2). A same-day copy is still possible; a week-old one is not.

Within a session, chat recalls at most the newest 20 accepted messages and 65,536 characters
of their text. The immediately preceding accepted owner/reply pair stays together, subject to
the per-message text limit; older pairs
fall out of the model prompt when the aggregate limit is reached, with an explicit omission
notice. Each accepted message is separately capped at 32,768 characters. These prompt limits
do not delete the full delivery journal or change the current owner message. Quoted, edited
or background text reused as context is at most 16,000 characters, or it is not reused.

Gmail thread background is bounded the same way everywhere: a daily-brief inbox thread excerpt
is at most 4,000 characters; focused prep reads at most 2 threads per attendee, 8 messages per
thread (the opening two and the latest six), 8,000 characters per thread and 24,000 per run.

## Where to see what happened

Every verdict — nudged, queued, dropped, or promoted — is written to a ledger with its reason, and
the dashboard's **Record** view (`/app#record`) renders it. If you're asking "why didn't I get
nudged about that?", the answer is a row there: *muted sender*, *quiet hours*, *daily interrupt
budget spent (4 nudges today)*, *in a meeting until 2:30 PM — Sarah Chen*, and so on. Nothing is
silently discarded without a reason you can read. Provider acceptance is labelled Sent, not proof
that a device displayed it. Photo briefs record whether four photos were sent or why they used text;
that explanation comes from the existing outbox receipt and survives its retries. Expandable Activity details connect a send to its recorded prior decisions by identity; missing or later evidence is never guessed. On the Mac,
Settings → Account → Activity opens this same record. Full Disk Access on the Mac stays Checking after a permission
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

The pulse shows at most **five** reply-review or reconnect suggestions. An inbound message alone
does not establish a reply debt: reply reviews need a visible direct request cue or missed call,
3–14 days old, with no later owner touch in available history. They include source/date, and the
attended reply also quotes the subject/excerpt of a request it read fresh; they suggest reviewing or
calling back only if still relevant. `relationship_state.json` keeps only the channel, date and
whether a request cue was seen — never message wording or subjects — so a retained request is
offered without its wording, an older file's stored wording is ignored and scrubbed on the next
write, and the daily brief drops a saved suggestion whose channel is no longer permitted. Known
bulk/newsletter evidence, CC-only email, greetings and subject-only rows do not earn reply reviews;
observed owner responses still clear suggestions regardless of bulk labels. Existing sustained
relationship importance and reciprocal history rank ahead of age within reply reviews. A quiet
result says no clear follow-up was found in available history. Partial source coverage stays visible,
and only fully read channels can establish a lapsed relationship.

For the managed personal pilot, successful Google consent opens only the Gmail/Calendar source capabilities actually granted; missing or declined permissions leave the scheduled-brief gate closed when no other source is connected.

A Bridge with a local Cloud policy reads only its consented sources and excludes the Sotto iMessage handle before transferring history, unread messages or events. Contacts follow their source consent; source access does not authorize outbound actions.

Bridge read tools validate arguments before opening sources. Unknown fields, explicit nulls and
malformed numbers are tool errors; only omitted fields receive defaults. Snapshot windows accept
0–8760 integer hours (default 24), and per-contact message reads accept 1–1000 rows (default 20)
with a nonblank identifier. Historical message reads keep their existing 366-day window and
1–500-row page bound; invalid windows, sources or cursors report a tool error rather than source
unavailability. An explicit empty snapshot source list still reads nothing.

iMessage attachment metadata is limited to 16 visible items per message, with bounded IDs (256
characters), basenames (512), MIME types (128) and optional byte sizes. Hidden plugin metadata is
excluded; missing fields, unknown visibility and truncation stay explicit. The prompt repeats
these bounds for cached inputs and says that file contents were not read. An attachment name or
type never proves that an ask was answered. No attachment files are opened or uploaded.

Phone/FaceTime metadata includes the local record ID, provider and an explicit UTC instant when
available, alongside the legacy display time. Call comparisons and callback evidence prefer UTC;
legacy wall times use the configured user zone (UTC if unavailable). A record ID alone is never
completion evidence: the full row and current source authority still bind the reference, and
event store generations continue to separate replacement/reset databases.

Bridge recovery keeps one active gather worker per reader label; a timeout requests SQLite cancellation
instead of spawning overlapping replacements. Read connections have a 12-second SQL budget. A
worker still active after 600 seconds causes an engine exit on the watchdog's next 15-second check,
for the native supervisor to restart. Result delivery retries the same response up to three times
(five seconds per attempt, one- then two-second waits), rechecking local authority each time; it
never re-executes the tool. Relay acknowledgement receipts contain hashes only, capped at 256 for
120 seconds. Event catch-up shares its 50-event budget across enabled sources. Upload/persistence
failures retain cursors and back off to 60 seconds; they remain visible as retry states.

Local sources follow these rules:

- Both setup/prewarm and recurring Contacts/style writers recheck contributing source consent before writing learned data.
- Unknown style channels are rejected; legacy channel-less messages require iMessage consent and email aliases require Gmail consent.
- A disable reported by a Bridge probe takes effect on arrival whatever either clock says, only a strictly later probe restores access, and a completed read never does.
- An unreadable consent receipt withholds every local source and is left for repair rather than rewritten. A permitted brief can use other connected sources and says why local context is absent; fresh self-host setup waits if no eligible source remains.
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
they happen. If essential memory writes fail, the staged brief can still be delivered only when
its retry is admitted to the existing durable queue. If both required writes and retry admission
fail, the parent brief work retries the saved artifact instead of handing it off. Ancillary-only
admission failure does not withhold the brief. Optional research and ancillary learning are separate from
initial delivery. A composition older than two hours is invalidated before delivery, including when it waits in the
saved-result queue or outbox. The existing invalidation path releases only a provably unaccepted
reservation and admits fresh work; ambiguous acceptance never authorizes a second brief. Every
replacement gets a distinct learning revision and required-write receipt. Composition and learning
share the same lock; unfinished essential writes must complete before replacement retires their
inputs. A deterministic failure in that old essential phase can withhold replacement until the
existing work deadline or attempt limit; a fresh replacement that day is not guaranteed. The old
inputs remain intact. An old follow-up cannot learn from or delete replacement inputs. Optional ancillary work
for a superseded generation is not guaranteed.
Recovery can wait behind learning already holding that lock. Acquisition contention requeues the
job for one minute later without spending a fault attempt, within the existing catch-up deadline.
Terminal work rejects a scheduler replay unless an ingress path explicitly admits a fresh retry. A successful empty Gmail search is a
quiet inbox; an unavailable or partial source is reported separately.


### Relationship importance and gifts

Only an explicit VIP choice or sustained reciprocal activity qualifies a person for proactive gift help. A saved birthday, meeting invitation, graph depth, raw message count or overdue reply alone does not. This policy is shared by Cloud/self-host and every delivery channel. Explicit user requests for gift help are unaffected; no purchase is authorized by an inferred tier.

Scheduled and requested relationship pulses share one pipeline, reusing the existing continuity ledger, relationship activity, local
snapshot and background history cursor. They put up to five recorded open relationship commitments
and contextual reply/reconnect suggestions in priority order. The ledger's current source union,
active/snooze rules and birthday-courtesy exclusion apply before any text is shown.
Birthday rows follow the brief's one courtesy rule: a provably unsourced, brief-created birthday
courtesy is omitted, while an observed request, an owner reminder and birthday planning appear.
Contacts-backed missed greetings for proven VIPs or sustained reciprocal contacts remain optional,
rank after real commitments, carry no deadline, and are never grouped into a gift/planning request.
The brief, `sotto-loops` and the pulse apply one birthday visibility rule: a captured optional
greeting carries the optional label and is hidden in all three once its person no longer proves VIP or
reciprocal importance, while a stored source-backed request remains an ordinary task everywhere.
Relative request wording is quoted under its original recording date on the owner's calendar day,
or marked as an earlier request when that date is unknown.
Multiple requests for one proven canonical person share one compact pulse line with an extra-item
count. A person is proven by a row identifier's unique graph owner (graph normalization, so a
WhatsApp JID meets its phone); a stored canonical ID counts only for a row without an identifier
or one its identifier proves. Commitments beyond five are counted as "+N more saved commitments",
and a suggestion is folded into a commitment only by proven canonical ID, never by display name.
A pulse may similarly say “I can draft a reply to [name]” for one unambiguous, directly addressed
commitment. It does not create a pending Gmail draft or authorize a send.
A pass without any fresh observation is degraded: it may offer retained asks but rewrites no state.
Legacy relative deadlines are omitted; only valid calendar dates appear as due dates.
A fresh Gmail check is one both-direction metadata query (20 rows, 12-second child deadline; paced
metadata reads take at most about 5 seconds, so a busy inbox returns a capped `partial` read); Bridge gets an
8-second deadline. The Bridge read is merged in memory and never replaces the brief's local snapshot
or its day archive. Without a reliable cursor the fresh check starts one hour before the local snapshot's observed time, or uses the 24-hour snapshot interval when that is unknown,
and says historical learning is not yet available. Neither is a new full backfill. Missing or capped evidence is disclosed, and
stored requests remain conditional reply reviews rather than inferred obligations. History learning
keeps the full traversal. A prior `initial_complete` window covering six weeks and a complete
current window, without excluded pages, can join a contiguous complete fresh read to preserve
lost-touch detection. Without that proof, lost-touch suggestions are withheld, and the one-line
disclaimer still follows any open commitments. Calls have no background history traversal, so a
calls channel never counts as fully read: someone you also phone is never called lost touch by an
pulse. History-learning availability counts only sources that are currently permitted.
Weekly cron and dashboard Run now bind the current source revision and proved delivery route before
starting the same checkpointed procedure. Cached content is withheld after a source-generation or
route change; a lost result receipt never repeats a completed persistence stage. Linked Telegram
uses its existing protected owner/bot route proof. No new store, model call or nudge writer is introduced.

The relationship pulse is the sole writer of dated `importance_evidence` within each canonical person's history in `knowledge/relationship_state.json`. It combines known-person iMessage, WhatsApp, calls and Gmail touches. The reader deduplicates days and uses a rolling **42-day** window. **VIP:** at least **6 active days across 3 weeks**, including **2 days in each direction**. **VVIP:** at least **12 active days across 4 weeks**, including **4 days in each direction**. Confirmed Granola meetings can strengthen an already reciprocal relationship: **3 held meetings across 3 weeks** for VIP, or **6 across 4 weeks** for VVIP, with at least **2 days in each direction**. These meetings require exactly **1 counterpart** after excluding the owner; multi-counterpart meetings cannot qualify anyone. Scheduled invitations, missed calls and unconnected calls do not count. This prevents one-way outreach or a single chat burst from qualifying. Missing, stale or ambiguous evidence suppresses a gift prompt; explicit `vip_people` choices remain authoritative. Day-of greetings retain their existing gates. The attention queue no longer grants any quiet-hour VIP bypass at all (Sep 2026 — volume is not importance); gift importance likewise grants no extra interrupt permission.

Gmail polling keeps independent inbox and sent-mail bounds, page cursors and pending IDs in the
version-2 `events/gmail_seen.json`. It resumes bounded oldest-first pages after downtime and advances
only after receiver acceptance. Fresh or legacy state starts with a defined 24-hour recovery window;
recovered messages keep their original timestamps and enter as catch-up.

### Memory retrieval

Interactive chat reads have an additional 48 KiB aggregate budget, checked both as UTF-8 JSON
and as the receiver reserializes it with ASCII escapes. Whole assertions and conflict groups stay
together. Omitted people, assertions and contacts are counted with a narrower-query suggestion;
editable reads count facts beyond their existing selection cap too. The internal brief identity
index remains complete. This is deterministic formatting around the same canonical selector.

One reader, `knowledge_query.py`, supplies chat, briefs, meeting prep and notifications. Current
consented email, calendar, direct-message and active-loop subjects select older relevant facts by
exact canonical identifiers and contact aliases. Display names and nearby timestamps never join
conversations. Topic hints are data used for lexical ranking, never instructions.

For saved facts and learning questions, chat uses `--person-coverage --facts-only`: the same
permitted fact/evidence and coverage view, without reading or returning the obligation ledger.
The flag requires `--person --person-coverage` and refuses edit/company/cohort combinations. Mixed
facts-and-obligations questions retain full person-coverage; specific outcomes retain the topic
read. A facts-only result cannot establish whether any commitment exists or is complete.

Chat's person lookup adds `--person-coverage`. A missing graph match returns guidance
from existing consent and source-health records: check permitted historical sources,
verify an unknown connection, or explain an unavailable source. An empty recent
snapshot does not mean older history is empty. Ask and People check the indicated
sources before answering a past-conversation question, then name the sources and
material coverage limits if nothing is found. A missing graph match is never proof that
a previous conversation did not happen. The lookup itself makes no network or
model call and does not change memory; existing query consumers retain their output.

The person-coverage read also explains selected active graph facts with their canonical fact IDs,
current status, retained source references and effective confidence. Evidence is bound to the
exact assertions retained in the response, including related-person context. References are
withheld when their source authority is unknown or revoked. Graph first/last observation dates
are labeled separately from the original source timestamp, which remains unavailable when it was
not retained. This is a bounded current view, not a complete edit history: evidence has a 12 KiB
cap and at most eight references per fact, preserving first and latest retained references inside
the existing 48 KiB aggregate interactive cap.

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
For local first-use readiness, an enabled source must have a fresh valid successful read; an
explicitly empty read counts. Contacts alone cannot unlock a first brief in either hosting mode.
A quiet newly paired self-host Mac gets a consent-aware collection
on the existing heartbeat, with at most one in flight. A collection that leaves setup unready waits
twice as long before the next, from 1 minute up to 60 minutes, and a change in source health or
consent starts over at once, including re-enabling a source between heartbeats when its health label
is unchanged. Until then the setup page says the Mac is linked but not yet read. Re-enabling a source requires a read started
after the server accepted restored consent. A healthy probe does not make an older extraction fresh.
Quitting the Mac app or changing its connection stops its owned engine and pending requests together.
The engine is stopped from starting another request before its downloads are terminated; unrelated
processes are left running.
Before a self-hosted installation has its first accepted brief, scheduled preparation and
composition also wait for a context source and linked delivery channel. A Bridge wake during
that wait retains its context in the existing snapshot without composing. Google-only setup
remains supported; after an accepted first brief, temporary source or messaging outages retain
normal scheduled delivery and recovery. An already-composed scheduled request can also recover through this gate only when its exact completed work and invalidated outbox receipt prove it was not accepted; this does not unlock later fresh runs. Explicit manual requests keep their existing behavior.

Background learning progressively reviews the last six weeks of direct iMessage, WhatsApp and
Gmail exchanges, with per-source coverage receipts. Only durable cited facts enter the graph;
historical discussions, decisions, commitments and asks are not stored or resurrected as work.
Other sources keep their existing learning paths. The receiver starts a background memory pass at
most once every 15 minutes (`receiver.MEMORY_INTERVAL_SECONDS`), never while the previous one is
still running, and each pass advances at most 3 rotating history pages
(`memory_cycle.PAGES_PER_RUN`) and one unfinished Dreamer batch. Disabled, parked and backoff
sources do not spend page slots. The pass rotates fairly, stops each completed, failed or
content-excluded source for that pass, and inspects at most three rotations. Fetch attempts
spend page slots even when they fail. Model admission holds still stop all remaining work.
The Dreamer selects useful existing facts for person summaries, archives exact duplicates and selected equivalent assertions, and records contradictions.
The same existing model pass selects bounded groups of existing IDs; it cannot invent replacement
prose. User corrections remain active. Archived originals retain their own sources and evidence;
curation never increases confidence or pools evidence across source permissions. Review coverage
belongs to each fact's evidence, not to the code: a release, prompt or model-route change re-curates
nothing, and only an intentional `dreamer.CURATION_VERSION` bump makes already-reviewed memory
eligible for one ordinary bounded review, without a new scheduled call. Bare numeric Apple Contacts notes are kept out of active assertions. Facts record `archived_reason`
separately from their source. Only an exact observation of a fact archived for age (`stale`) may
revive it. User archives, corrections, superseded facts, duplicates and legacy archives with unknown
reasons stay archived. It cannot change source grants,
approvals, priorities, mutes or code. Conflicting evidence stays uncertain; the user's corrections
win and do not expire. Rereading the same source or a reference-free repetition is not additional
confirmation and does not refresh the fact's recency; distinct evidence or an explicit correction can.
Evidence novelty is checked across the keeper and every explicit reverse/transitive duplicate in that
person's records, regardless of which wording was observed. Replay cannot revive a stale keeper.
Invalid lineage supplies no corroboration. Saturated or incomplete legacy evidence requires a
strictly newer known source date across the family; an unknown family date cannot borrow the
keeper's date. Incomplete legacy reference lists stay incomplete rather than becoming falsely
complete after an update. Archived siblings keep their own provenance; owner corrections and
explicit archives retain their existing authority. No new model stage or memory store is involved.

Bridge Contacts reads share trimmed note excerpts of at most 600 Unicode characters, matching
the graph note cap. Longer excerpts report partial source coverage. The stored Contacts card
remains unchanged.

The same memory heartbeat sweeps untouched people at most once every 7 days
(`memory_cycle.PRUNE_INTERVAL_SECONDS`), using the existing graph lock and
`knowledge_update.prune_stale_memory` writer. It applies the existing 60-day archival rule
only to uncorroborated facts; owner corrections, notes copied from the owner's Contacts cards and
corroborated facts remain active. Archives retain text and provenance. Unchanged files are not
rewritten, evidence dates are not refreshed, and no continuity task or preference is pruned. An
unreadable person file is skipped and counted, never rewritten, and does not hold back the others.
The existing history checkpoint records completion only after a successful sweep; a failed sweep
can retry without repeating archival effects.
This local maintenance runs even when paid background learning is held and makes no model call.

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
unchanged failed Dreamer work version gets at most two attempts. Its reservation is durable before
contact, so a crash or ambiguous transport consumes an attempt; regrouping cannot reset it. Only a
proven pre-execution hold refunds the reservation. Raw HTTP 401/403 does not prove non-admission:
the proxy can retain its full allowance for an upstream authorization failure, so those responses
consume the existing finite attempt budget. Proxy authentication rejected by the capability
preflight spends no candidate attempt and can resume after repair. An unchanged-route upstream
credential repair alone does not release exhausted work. For unchanged extraction inputs, only
an effective request-contract change currently releases the latch; no supported operator reset
exists. No credential value or credential hash enters the request identity. Permanent request rejection retains its existing
hold. Parked facts are excluded both as work and supporting context; their dependent work stays held
while unrelated eligible people advance. A changed evidence version allows bounded new work; a changed request
contract (prompt, schema, code or model route) only gives parked work a fresh bounded try. History extraction retains its separate existing retry policy.
Dreamer uses a fair person cursor and per-version coverage in its existing state, with the same
eight-person/forty-fact caps. An unreadable person file is skipped without changing its bytes
or blocking healthy people. Dreamer receipts report only the unreadable-person count, without
filenames, identity or parser content. Unreadable canonical files retain their previous coverage
and charged attempts; actual deletion removes them. An unchanged repair does not replay covered
or parked work. A repaired new or changed fact is reconsidered on the next pass.
Preparation writes its checkpoint only when state changes. Successful partial windows do not mark unseen tail facts reviewed.
A recorded contradiction is kept while both of its facts stay active — editing or archiving either
side retires it for every reader and for Dreamer's dependency check — and storage is bounded by
eviction, never by refusing curation: at most three pairs per person (a new review's first, then the
oldest kept) and 100 people (the oldest record evicted first). Bookkeeping changes create no fresh work. Capacity, dependency and
unmappable legacy holds are reported rather than counted as successful reviews.
Receipts retain fixed response codes and allowlisted provider finish/block reasons, never message
bodies. Bridge fetch receipts distinguish offline, timeout, permission denial and tool errors;
these availability failures retain the ordinary retry clock and do not park extraction.
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
Live ingress adds copies of the current event batch to the retained conversation snapshot before
any Tier-1 judgment. An ask followed by a reply or withdrawal in that same batch is therefore
judged with both messages available. Current mutes filter retained and incoming context before it
reaches the model. The primary event uses the same mute check. Tier-0 noise, such as a
verification code, also stays out of related messages’ review context. Existing source/thread identity, observation-time, message-count and text bounds
still apply. Context is scoped to this batch's conversations before additional sender lookup.
Explicit name mutes can use the existing graph name lookup when Contacts lacks that identifier,
at most once per identifier per batch; without name mutes, this adds no graph lookup. An empty
batch skips the retained-context read; quiet or snoozed batches skip review-context construction
entirely. This supplies evidence, not an automatic completion rule: a later greeting or partial
reply cannot deterministically erase an outstanding ask. Changed context uses a new relevance
artifact; an unchanged retry can reuse its existing judgment. Cadence and interrupt budgets are unchanged.
Triage, notification composition and send-time validation share the same contact/graph name
resolution and current mute check. An explicit mail display-name mute also applies when Contacts
uses another name for that address. A person muted after work admission is removed before writing;
muted background messages also stay out of reminder and meeting-offer context. Each newly composed
notification binds only its selected sender's name and transport identifiers to its existing
eligibility receipt, without message prose. Before an unaccepted send or retry, the outbox checks
that proof against current person and sender mutes. An unselected sender cannot veto the chosen
notification. Legacy receipts without this proof retain their earlier checks. An uncertain provider
outcome still stays held for receipt reconciliation; a new mute never authorizes replay or erases
uncertainty.
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
entries. Birthdays use the same local date and contact permissions and use only spare lines after meetings.
A routine birthday greeting is optional context, not an overdue obligation. Birthdays remain
visible for ordinary contacts. After the birthday, only an unambiguously identified explicit VIP
or a person with recent reciprocal contact can have an optional greeting follow-up, backed by the
actual Contacts birthday and the permitted sources behind importance. It has no deadline or urgency,
does not accrue chases, and cannot replace a real task. It expires once the birthday is further back
than the same relationship-importance window that admitted it. A greeting already observed after that birthday
prevents a later courtesy from being captured; unrelated conversation does not count as a greeting.
An observed request or explicit owner instruction about gifts, plans or wishes remains a real task,
as does a stored source-backed request saved before the optional mode existed, or any row whose
provenance cannot be classified. Existing provably unsourced, brief-created birthday courtesies remain stored but are omitted from
active obligation views. Explicit section
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
other transient failures retain the hourly retry. A wholly invalid extraction or an admitted/ambiguous
HTTP 401/403 extraction failure parks after two attempts for the same source revision, across
scheduled occurrences and restart. A repaired upstream credential alone does not reset that latch.
For unchanged extraction inputs, only an effective request-contract change currently releases it;
no supported operator reset exists.
Empty, valid no-fact responses remain valid reviews.
Malformed history protocol responses likewise park after two unchanged attempts and make one
compatibility probe every 24 hours. Dreamer separately reserves per-fact-version attempts before
contact and parks unchanged failed or ambiguous work after two attempts. Only proven unadmitted
holds avoid a learning candidate attempt; raw HTTP 401/403 remains charged. All model-call and
proxy-allowance records remain. Its fair cursor advances unrelated safe work without weakening authority
context or marking unreviewed facts as covered.
For Gmail, two HTTP 400 rejections of a nonempty continuation token restart the same frozen window
once. An empty-cursor rejection, or another bad token in that window, stays parked.
Tier 1 conversation context keeps up to 12 observed messages and 4,800 characters of message text
in total. It retains short messages in full before discarding older observed turns to fit that
budget; an oversized single message keeps its beginning and ending with a clipping marker and
original length. The context states how many older observed turns were omitted. Digest and meeting
prep retain their separate 400-character message previews. This isolation adds no repair call, new
queue, or weaker evidence check. These are code-level evidence-preservation rules, not a measured
claim that the model interprets every completion or negation correctly.

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
uses one atomic SQLite ledger and permits 45 admitted requests per tenant and 60 across tenants in a rolling
60-second window. This short rate bound is separate from the optional spending admission cutoff. Self-host
background learning marks its native requests as requiring a finite tenant budget, so an unlimited
tenant is rejected before an upstream call; the same ledger and reservation allowance are used.
The allowance is conservative admission accounting rather than a provider invoice.
The proxy rechecks bearer validity inside the admission transaction after body reads and lock waits;
an expired credential returns 401 without reserving spend or reaching the provider.

Messages readiness comes from authenticated transport plus the receiver's existing source and
first-delivery facts; an unreachable instance is unknown, not ready. Live shared routing remains a
release gate.

### Visual presentation

Morning/evening briefs and focused meeting backgrounds default to four-photo galleries on iMessage; other channels, short updates and consent questions stay text. The shared renderer changes presentation only. After mandatory brief sections, the composer can use spare space in a readable four-card gallery to name quiet active obligations, ordered by deadline then age. It uses the same layout to check fit; the remaining items stay a count. The named details enter the canonical text before delivery attribution, so all channels and chase suppression agree. This does not make an item urgent. It makes no additional relevance or model call. Crowded briefs first redistribute complete paragraphs using measured height, preserving font size and source labels. Needs-you and today can share one card, leaving room for follow-ups on two. A brief with an action that still cannot fit stays text; uncertain multipart acceptance is held rather than blindly retried. A content-free operation receipt can confirm late acceptance without sending again. Unknown galleries retain their recovery identity through expiry, source changes and exhausted retries; missing or corrupt receipts remain unknown. Interactive gallery recovery verifies the durable dispatch ID, original content and destination before reusing an accepted receipt. Identical text, a recent receipt, or a matching old manifest alone cannot confirm a newer obligation; ambiguous legacy sends remain held. Receipt lookup follows the existing capped outbox backoff. See [visual briefs](VISUAL-BRIEFS.md).

## Shared writing rule

All Sotto-generated prose uses the same [_shared/references/writing-style.md](../sotto-chief-of-staff/_shared/references/writing-style.md): plain, concrete language, no em dashes, and no canned AI phrasing or filler. The Gemini/OpenAI/Anthropic composition adapters attach it as a system instruction; raw document extraction transport preserves its original instructions. The Node chat runtime's instructions come from the installation's launch manifest. The shared chat formatter normalizes prose punctuation while leaving URLs and literal machine text intact. AI-ism avoidance is a generation instruction, not a word-deletion filter.
The Photon formatter uses a fixed executable Python path: `/usr/bin/python3` on macOS, or the first
executable of `/usr/local/bin/python3` and `/usr/bin/python3` on Linux. It keeps isolated mode, an
empty child environment and its five-second deadline; a formatter failure stops before sending.



### Execution and checkpoint safety (pending release)

The scheduled and wake proactive checks keep the same cadence and shared relevance rules.
Gathering, due-item selection, quiet-hour holds and reservations run deterministically before
the notification composer. An empty check makes no composer call; an accepted nudge uses the existing skill and
approval rules. Delivery still rechecks consent and current eligibility.

One calendar refresh is one triage batch with individual acknowledgments. Post-meeting tap slots
are reserved before dispatch and count toward the existing cap while pending. Failed checkpoint
writes do not reset that count. Work ownership and completed queue/drop receipts reconcile after
restart without another triage. An unreadable checkpoint pauses taps; unresolved pending taps that
leave the lookback keep their slot until local midnight. A meeting gets one tap however many of
your calendars hold its invite; two different meetings in the same slot each get their own.

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
A pass marked partial because it rejected proposals still counts.
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

When separate graph people share a display name, that name alone cannot bind a new commitment
to either person or refresh an existing task. An exact unambiguous email or messaging identifier
can still establish the counterpart through the existing person index; the ambiguous request
leaves every saved obligation unchanged.

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

The chat tool `accepted_brief_detail` separately reads accepted visual galleries and newly retained
text preps under current source consent and the exact owner route, with pages of up to 4,000
characters. New preps
also carry at most 16 KiB of provider-reported research evidence, covering at most twelve batch
records with truncation and omissions disclosed. Its content proof binds that evidence to the
delivered prep. Provider citations explain attribution; they do not verify a claim or establish
that two domains identify the same company. Legacy galleries return unavailable research evidence.
Saved prose and URLs cannot authorize further external reads. Plain-text preps and photo
fallbacks retain the same bounded evidence when the provider confirms the exact text payload
and current owner route. Historical text and transports without that receipt remain unavailable.

`release_one_proof.py --days 7 --complete-days --timezone America/Los_Angeles` excludes today's
partial day and reports which morning/evening receipts contain exact counters for each local day.
Two exact scheduled Learn receipts establish that day's observation coverage, not model accuracy.
Missing historical counters remain unknown; a zero observed error candidate count is not zero errors.

### Short answers and relationship importance

A short yes resolves against the last delivered question in the transport-identified conversation,
using the incoming message timestamp. If another question was accepted after the reply, or within
a few seconds of it, Sotto asks which one was meant instead of choosing the older question. Reply-to messages
and thumbs-up tapbacks keep their exact original message target, including older questions. Two
questions in one message remain ambiguous. A later conversational topic or question takes precedence
over an unrelated stored offer. Real effects still require the exact action and payload hash and
consume the offer once; this introduces no additional model call. Legacy unbound offers cannot
use automatic recency selection.

The person page shows automatic VIP/VVIP from the existing 42-day sustained reciprocal activity
classifier, separately from manual quiet-hours overrides. A busy one-sided sender is not promoted.
This is a local read of saved activity, not new research or a model request.

### Narrow requests and uncertain outcomes

A specific commitment or outcome, including one involving a known person, can be read directly through `knowledge-query
--topic` with concrete subject keywords. This returns at most 12 independent retained obligation
candidates within the existing 18 KiB evidence and 48 KiB interactive budgets, including closed work
and its original/closing evidence, without a broad people/cohort read. Current source permission
filters the full evidence union before matching, ambiguity, excerpts or omission counts. Matching
uses all supplied Unicode whole-word keywords; a shared name never joins people or obligations.
Multiple candidates require disambiguation before explaining one closure. Missing or truncated
results remain a limited permitted view, never proof that no other obligation exists. This reads
saved evidence only; it does not search source archives, update the ledger or grant action authority.
Bounded reference lists retain their first and latest stored entries, and rows expose retained
reopen, park and chase markers. Storage order is not independently verified chronology. Meeting
commitments retain the source's actual date/time precision and identify a meeting start/end/date;
that is not an exact utterance time. Missing source timestamps stay explicitly unavailable, and
the resolver's `created_at` cutoff must not be presented as the original promise time.
An active row (open, waiting, failed or blocked) reports `completion_basis: not_recorded` through
the shared ledger reader rule, also used by meeting history: not recorded as fulfilled, not proof
that the owner has not done or sent the work. Later fulfillment may still be unread.
`--topic`-only outcome results also carry the existing measured history-learning projection under captured
source grants. Current and initial extraction windows stay separate; omitted, truncated and rejected
inputs remain visible, while a revoked source has a constant withheld shape regardless of its saved
checkpoint. These source-wide counters do not establish person/topic completeness, prove every fact
was learned, or replace a row's closing evidence. Unknown outside-window coverage stays unknown.
Only Gmail, iMessage and WhatsApp learning is measured; other sources are unmeasured. An absent
checkpoint means no saved measurement, not no history. Person-coverage reads use the same captured
grants and projector at `coverage.historical_sources[].learning`. The one bounded regular-file
checkpoint read adds no graph/archive/API read. Evidence keeps its original 18 KiB budget; coverage
has a separate 4 KiB cap, with the combined result inside the existing 48 KiB interactive cap.

An explicit `--relevant-days N` selects the existing people/cohort read instead, even when N is 7;
combined `--topic` ranks facts within that cohort. These days describe the cohort's file-mtime
fallback window, not an obligation-history date range. Omitting both selectors keeps the usual
current-participant/recent people view and its default seven-day fallback.

A uniquely selected company's homepage read permits successful provider retrieval at the saved
HTTPS root host or its single `www` alias, and carries the actual retrieved URL into the answer's
evidence. This applies only to the trusted company-site procedure: paths, queries, fragments,
ports, credentials, different schemes and other subdomains are not aliases. A literal URL read
still requires exact retrieved-URL proof. No second fetch or model call is added.

A company blurb or investment-committee writing request stays a writing/research request even when
it mentions tomorrow. Only an explicit prep request can queue the meeting-prep procedure. Prep that
names nobody (“full prep, please”) is not a focused request; as a reply it can only accept the
offer it answers. Focused prep that finds no matching upcoming meeting asks for clarification
instead of expanding into a calendar-wide sweep. Historical follow-up text containing relative dates
is attributed as an earlier dated quote, never silently presented as a current deadline.

An imminent prep reminder matches its saved open task by the attendee's exact email when known;
the same display name cannot pull in a different person's task. A name-only match is usable only
when one saved row matches. This reuses the current loop read and performs no extra lookup.

External attendee research sends only the selected name/email and its domain; company-only research
sends the explicit company selector. All profile, recency and focused prompts exclude learned person
and company knowledge, calendar descriptions, email snippets, expanded threads and direct messages,
including provider fallbacks. Private context stays in local prep composition. Existing result caches,
canonical duplicate handling and owner-edit precedence remain; no new model stage is added. A private
project hint is not exported for disambiguation: ambiguous public identities must stay unresolved.

When you say several named loops are done and ask what remains open, Sotto reads the currently
permitted source loops and the separate owner-authored task view. These are partial views. A
follow-up background run is not the open-list read. One explicit message can resolve several
uniquely matched current rows through one admitted request with a separate outcome for each row;
an ambiguous name or owner-named task phrase requires clarification before the batch. A task phrase may identify one current saved title without repeating its full title. Unknown effects stay
held without replay, and the remaining currently permitted open rows are read back after a confirmed receipt. When you ask what else is open, the reply lists up to eight remaining rows and says when more exist; this is not a complete account of unavailable sources. An ordinary completion gets only the per-item result. Sotto
does not claim any row closed without its item receipt. If an attempted shared run is rejected, its
reply says what was not queued and gives a plain next step; it is not a closure confirmation.

When a reviewed calendar, contact or Gmail action loses its receipt, Sotto can say the outcome is
unconfirmed while unrelated chat continues. If inspection later supplies a definitive receipt,
it sends the normal confirmation once; it never repeats the action to obtain a better answer.
Stopping the source request suppresses that later confirmation.


### Dashboard loop source consent

The authenticated loop list, its overview count, and the brief's action ledger use the same protected source-union projection. A row backed by multiple sources requires every source to remain permitted. Fresh owner-authored rows remain visible without source grants; unknown or stale provenance stays withheld. The brief also carries the sources of included ledger rows into its delivery-time permission check. The receiver checks complete source authority before and after the dashboard read. Changed authority, an unavailable helper, or an unsafe ledger read returns an unavailable response, with no raw-filesystem fallback. Rendering does not alter ledger rows. Task lists and cleanup reviews display the nonempty canonical ask first, then summary, then action type, capped at 200 characters. The optional birthday label and explicit deadline fields are unchanged. Summary-only timing is not inferred or appended when an ask exists. They label an unnamed, proven owner-authored manual task "Your task"; a sourced obligation without a known counterpart remains "(unknown)".

### Explicitly completing a described task

When you say “Dana’s family photo plans are done”, the description must select exactly one
active task among all matching saved contact names. The writer also checks active tasks that
are hidden from the current list, so two matching photo requests still require clarification.
A bare name cannot choose between multiple tasks. A qualified task description may distinguish
two people with the same name; that closes only the selected task and never links or changes
their identities. Current task version, identity proof, source permission and exact owner wording
are checked again before any change. Old receipts remain authoritative for recovery.


### Evidence-bound loop closure and calendar follow-through

Two tasks from one source retain separate identities across extraction order changes. An exact
origin or a unique quoted source clause may recover a task; a paraphrase shared by several tasks
must name the existing loop ID. No position in a model response identifies a task. New incoming
requests and explicitly source-backed outgoing commitments can follow a closed task on the same
day; automatic closures retain their exact instant. Legacy date-only closures remain conservative.

A brief cannot announce a proposed closure unless the canonical ledger contains that resolution
and its evidence. A closure the brief presents as done holds delivery until its ledger write
lands; a deferred or rejected write requires fresh composition. Proposals the composer rejected
stay in Still open and do not hold the brief. The ordinary learning follow-up remains recoverable.

When a selected saved task contains several obligations, extraction and the existing critic/revision
are instructed to consolidate its full supported scope once under the proven person or group.
A narrow precise ask must not hide distinct outstanding work from the summary, and an individual
email tap does not transfer a group's obligations. Separate new asks keep their recipients;
unreviewed wording must show the whole saved summary to suppress the Still open backstop.
A precise ask alone cannot prove a broader summary was shown. The existing source-, version-
and unchanged-line-bound semantic review can approve a legitimate paraphrase or outdated
summary context. The existing critic may approve one exact corrected presentation line for
its original proposal. Code independently rechecks that replacement against the same current
source task/version and recipient boundary, then reuses the ordinary display receipt. It counts
only if the approved replacement survives the ordinary revision unchanged in an action section;
missing, malformed, conflicting or stale corrections approve neither line. This adds no model
pass or task/action authority. Corrected lines retain the original review's shared source-context
budget; the identity recheck does not add fresh excerpts to the revision prompt. Corrected group
entries must lead with the saved group label. Terminal CR/LF transport endings are normalized;
null/empty, internal multiline and over-budget corrections remain unapproved.

The existing critic also receives each selected action's actual `newObligation`, existing loop
binding and bounded source evidence. It reviews capture against the full saved summary and ask:
represented components refer to the existing row, while genuinely distinct source-backed requests
retain their separate new capture. The existing resolver preserves an active row's canonical scope
when several partial actions explicitly reference it; no meaning or same-sender heuristic is added.
The action index keeps at most the existing loop-review evidence count, 500 characters per snippet
and 512 per source field, with explicit truncation flags. Original capture evidence stays intact.
If the critic is skipped, unavailable or semantically mistaken, the preexisting possibility of
misclassified new capture remains; no extra capture gate or model pass was added.

The critic both authors and judges a corrected line in one call. Its semantic judgment can still
omit or misstate supported scope; code checks source identity, task version and exact surviving
presentation, not semantic completeness. This remains a model-quality risk, without an extra
model pass or a lexical scope-percentage rule.

The outbox rechecks the closed task version
before accepting delivery. A last reply, archived email, display name or calendar invite alone
never proves completion. Waiting dates come from the cited promise clause. Existing hard deadlines
continue to bound chasing; a later counterparty promise is not owner consent to extend a deadline.
Grounded late meeting-note captures use observed meeting time, not the time the note was processed.

Numeric email local parts remain email identities. Phone matching strips punctuation and an
optional North American 1 prefix, retaining other country codes. A task keyed on the older
last-ten-digit form of an international number moves onto its full number; another number that
shares those ten digits never joins it. WhatsApp phone JIDs can use a
unique matching Contacts card; shared aliases and privacy JIDs cannot invent a person match.
Inbound group email evidence must come from a verified member and cover the group's roster through
From/To/Cc. Legacy rosters at the reader cap, or explicitly partial rosters, cannot prove delivery
to the whole group. MIME alternatives are selected within each section, preserving independent
plain and HTML sections. Read attachment text can supply bounded verbatim evidence; a filename,
unreadable attachment or clipping marker cannot. Connected native call evidence reaches the same
critic as message evidence, and can resolve only a matching callback obligation.

Periodic calendar refresh reads one day backward for ended meetings. Today views use the owner's
timezone and exclusive event ends, including overnight meetings and multi-day all-day entries.
An event without an end remains visible on its start day, but cannot trigger an ended-meeting tap.
Post-meeting taps keep their existing elapsed end-time grace/lookback across midnight. Distinct
original recurrence starts stay distinct even when moved to one current interval. Equivalent
UTC offsets do not count as a move. Availability continues to use the shared owner/busy predicate.


### October 15 review follow-through

An explicit owner timer is reconciled with its terminal outbox outcome before that receipt can
expire: definitive non-delivery cancels the timer with its delivery reason, while an unknown send
stays held for provider reconciliation. Only a definitive mismatch at the pre-contact recheck
(a missing or no-longer-scheduled timer, or a changed body, target or proof) supersedes it; a
recheck that cannot run, such as a briefly unreadable route proof, holds the timer for the next
outbox pass. The same old timer never becomes a fresh send after
seven days. A failed reconciliation retains the original outbox receipt.

Prep and brief work require a current affirmative owner request at admission. Calendar-time
questions and courtesy replies cannot authorize research or a brief. For a brief, negation or cancellation words void the
request when they appear in the brief clause or open another clause; inside an independent
remainder ("and tell me which tasks are not done") they are ordinary content. The chat runtime
and the receiver apply that one rule, checked against one shared vector table. The independent answer in
a mixed brief request stays in its source-checked durable reply alongside the brief acknowledgement.
A pure shortening request rewrites exact accepted text with source tools disabled; stale or
unavailable text requires clarification, not fresh research.

Automatic historical-message fact withdrawal is disabled. The Bridge retains only its newest-64
message cache check and store-generation recovery; it does not scan older history or send
retractions. Old queued `source_retractions` metadata is acknowledged with status `disabled`
and `applied: 0`, without reading/writing the graph or blocking ordinary events. Legacy cursor
history is ignored on load and dropped on the next successful checkpoint (attempted at startup).
Legacy `knowledge/source-retractions.json` files remain inert while the source-correction gate is off: they cannot hide facts or block new
learning. Already archived facts are not automatically restored. Message edits/unsends therefore
do not yet reliably retract learned facts; roadmap B1 defines the stable identity, migration,
reset-completeness and acceptance requirements before this feature can return.

Native stable identity, content versions and version-bound citations remain. The dormant
correction-candidate builder, automatic admission and projection worker have been retired.
Historical receipt files and graph provenance readers remain; old correction payloads receive
`disabled` and old queued jobs become terminal failed rows with their payload retained under
the existing work-row retention policy. No source absence, reset or consent change authorizes
fact withdrawal. Any future automatic historical correction still needs the B1 acceptance work.

Ask, follow-up and relationship pulse share one stage receipt helper without changing their
budgets: a started stage is unconfirmed, a completed result requires current authority, and only
Ask's existing incomplete-persistence case may explicitly retry a completed stage. Pulse retains
its durability checks. The Bridge wake loop uses the existing fixed 90-second boot floor, with
no configurable message-quiescence gate. Model attempt receipts and the live `parked_requests`
latch retain their existing behavior. The unused legacy column stays in place so previous workers
can write during rollout overlap and rollback.

### Brief review and derived links (October 17 correction)

Two bold entries with the same name may be different requests or different people. The existing
critic receives that hint and decides whether a rewrite is warranted; the name count alone does
not force another model call. This never authorizes a task closure or changes exact-task backstops.
Every action tap link is rebuilt from the current source allowlist, even if the input already
contains a link. Generated or previously saved derived links cannot skip recipient checks.

### Accepted conversation for read tools

The model resolves what a follow-up means; recalling or editing a previous answer requires no new read permission.
A new research or URL read needs a current affirmative request and an owner-selected lookup identity.
The runtime journal examines at most 32 preceding **inbound rows** within **15 minutes**, in the
same route/session with current source authority. Only owner-written text supplies historical
subjects; ordinary assistant prose does not. Cancellation cuts off earlier context. Exact whole
mentions exclude email/subdomain fragments; research identities exclude private prose such as
addresses and payment instructions. Negated and reported requests are refused.

Frozen background messages remain available for recall and drafting, but their names and links
never grant generic research or URL-read permission. A Photon quote of one accepted scheduled text
can also supply model context within the existing 15-minute window and current session. The exact
provider ID, route, payload and current source revision must match; unknown sends remain unavailable.
This projection is context only and does not enter the quote helper used for web-read targets. A bare yes/ok cannot select a target from
background prose. Meeting-prep yes replies retain the separate structured, exact delivered-offer
path. A chat prep suggestion uses `owner_meeting_prep_offer` to select one exact current Calendar
occurrence and its sole external attendee. The receiver returns one canonical question, without
queueing prep; only the exact accepted reply activates the existing pending offer. A later bare
“yes” uses the same receipt-bound consumer as a delivered prep nudge. The proposal expires from
the original owner request, never from a retry. Unsupported or ambiguous selections ask for
clarification rather than creating an offer from ordinary assistant prose.
Recovery repairs only the question selected by the owner's reply. A cancelled, changed or expired
offer cannot hold a later unrelated acceptance. If repairing the selected question temporarily fails,
the owner gets one journaled reply explaining that prep has not started and asking for a direct
prep request; that acceptance is not left pending for a silent retry. Recovery of the original
accepted offer remains bounded by its original lifetime. Only timed occurrences in the next 72
hours with one external attendee qualify for this chat offer; malformed provider dates or titles
are rejected as invalid targets.

For unthreaded replies, provider recovery can inspect an accepted prep candidate up to the normal
three-hour offer lifetime, starting at that candidate’s native provider timestamp. The incoming
reply must still be fresh (15 minutes); provider order must be complete and unambiguous, and the
receiver independently enforces the actual offer expiry. Historical uncertain deliveries remain
held. An interactive chat question is eligible only when it matches a succeeded typed prep offer
and its exact accepted delivery; ordinary assistant messages never qualify. Generic research offers need that same producer-owned binding before bare confirmations can
be supported; they currently require an explicit owner-named target. Native quoted replies retain
exact-message binding, current read intent and the same expiry. Company-website requests use the
existing company tool and canonical company record, not a domain inferred from assistant prose.
A public company question can also continue a recently delivered focused text prep. The runtime
binds the question to the accepted message in the current session. The receiver checks its own
delivery receipt, route, source revision and retained prep. A single focus input must identify a
corporate domain that matches one saved company. Only that domain goes to the company research
worker. Text in the prep and links in provider results cannot select a lookup target.
Cancellations including “nvm” and “scratch that” prevent reads; “no worries” alone does not erase
an earlier subject. Personal recall such as “how was lunch with Dana?” does not authorize research.

The authenticated runtime owns conversation provenance. The receiver independently checks the
current request, lookup identity and source revision. Research context additionally checks thread
and freshness; URL reply freshness is enforced by the runtime. The receiver **does not independently
look up runtime delivery receipts** for text-context proofs. Legacy queue-receipt proofs keep their
receiver-held binding and expiry for compatibility. This is not a second conversation-provenance
boundary. The small read policy data is shared by Node and Python; no subject store is added.
External writes keep their existing exact approval and durable receipt checks.

The actual-model conversation runner uses production bootstrap, tools, receiver, worker and outbox
with synthetic sources and captured delivery. It is a component end-to-end evaluation, not proof
of Photon/device delivery or real provider retrieval. See `runtime/sotto-chat/evals/README.md` for
all replaced boundaries and limitations.

Company read selectors use the same whole-token, case-insensitive match when admitted and when a saved action is read or settled. A capitalization change does not invalidate a valid request.

Brief rendering removes the deterministic Still open heading when its last remaining count disappears; other task lines and sections stay intact.

Weekly review details share the existing quiet-loop layout budget. Optional named rows can
be replaced by the normal open-loop count; parking notices and review decisions remain.
A repeated request's full visible row stays required when its keep/snooze/dismiss question
is shown. Required content may still require the safe text fallback.

Selected draft/research/prep actions now expose a provable mismatch between their source
recipient/request and the visible paragraph to the existing validation and single revision
path. Offer eligibility, full visible request, identity checks and the three-offer cap are
unchanged. This does not guarantee an offer count: unsupported help stays absent. Synthetic
revision coverage proves a grounded repair while preserving a distinct ask; offline retained
layout replay fits four cards instead of five, including three hypothetical short suffixes.
The retained replay makes no model request and still has zero eligible offers.

A source-backed external meeting within the existing calendar horizon, including later today, may
earn preparation attention when concrete work is worth starting now; a routine nearer meeting stays
a schedule preview. Dates alone create no urgency or saved obligation. A concise prep offer
resolves its event ID, title and start from a complete permitted calendar read; human-readable
meetingTime is display text. At most one prep offer joins the existing three-offer budget.
A sole external attendee without a supplied full name can use the exact source email (including an
email-valued display-name placeholder). The visible exact meeting title, event marker and complete
ask must agree; conflicting names, other emails and multiple eligible attendees stay unsupported.
This exception does not extend to draft or research offers. Selecting prep does not consume a real
email request: the existing task action keeps its source evidence alongside the calendar action
without duplicating the visible paragraph. A calendar agenda alone does not create a saved task.
The saved artifact retains its pending-offer identity and original expiry across retries, and
only accepted delivery activates it through the existing message/route-bound offer store.
Multiple actionable offers remain ambiguous to a generic yes. This early offer does not mark
imminent prep delivered or suppress the existing reminder. Explicit contextual prep can carry
an exact occurrence through the current shared skill tool; the worker revalidates fresh ID/start
before paid work. Missing or moved occurrences cannot fall back to another meeting. Explicit
prep retains free owner events and existing named-person selection; shared prep eligibility
excludes cancelled, declined and nonparticipant rows.

A requested inline reply draft defaults to one grounded version. Alternatives remain available
when requested or proactively offered. Drafting consent does not decide whether to accept the
sender's request: ask the owner for a missing decision before drafting or fetching voice.
An unambiguously selected inline offer uses chat wording; multiple or unknown offers retain
receiver-owned selection or clarification. Saving and sending keep their distinct review gates.
Availability and decline reasons need evidence. Interpret relative timing at the source date,
preserve uncertain windows, and keep these qualifications when shortening a draft.
A source-relative week that clearly ended may be described as past without claiming a missed
obligation; broad windows otherwise preserve uncertainty.


### One clear preparation handoff in a brief

The model can select preparation for a source-backed decision even without an explicit request
to prepare. When its exact calendar prep passes the existing identity, coverage and visible-ask
checks, the renderer makes that the brief’s single help offer and retains all other task text and
actions. This prevents competing offers from making “yes” ambiguous. If the prep cannot render,
ordinary grounded offers remain. The existing delivery receipt, expiry, consent and occurrence
checks still govern whether accepting the question queues work; assistant prose is not authority.

Research and full meeting prep share a source-fidelity instruction. It asks the model to retain
source qualifications and literal citation URLs, and to distinguish membership from certification.
Prep questions ask whether an unverified capability exists before asking how it works.
Research remains facts-only; this adds no new citation field or inline-URL requirement to saved bios.
This is requested model behavior, not a guarantee that each claim or cited page was verified.

### Attended calendar availability evidence

The existing Google read tools preserve event transparency/type and the owner's attendee response.
An event marked free or declined is not a busy commitment; tentative attendance remains uncertain.
Use current start/end for a moved occurrence, not originalStartTime. Bounded conflicts describe
those intervals only, and partial calendar coverage cannot establish complete availability.
The model uses this evidence in the existing chat loop; no separate availability pass runs.

Delayed brief learning rechecks current source consent. After a consumed source is disabled,
only facts whose full reference set still matches permitted saved evidence can be written.
Unreferenced profile, relationship and company proposals are held. The learning receipt remains
partial when proposals are held, while supported facts from other sources can still be saved.

History learning reports a held source and its retry time during backoff, without fetching it or consuming an eligible page slot.


### Failed admission and bounded recovery

A deferred event counts as queued only after its queue write and file sync succeed. Failed admission
is retried by the source. A Photon send stopped during preparation makes no message submission;
a send already contacted retains its uncertain outcome. Granola gets one forced authentication
refresh per gather, shared across connection and read failures. Graph repair retries stop at five;
original intent is quarantined (32 records, at most 1 MiB each) rather than discarded, and a full
quarantine requires operator repair before more intent can be retired.
