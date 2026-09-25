# Runtime architecture

Background proactive/event bundles now enter `compose_notification.py` directly after the existing
funnel. Fixed context readers and a bounded native writer replace the Hermes composition process.
One primary item is delivered per push; unselected items retain coverage. Scheduling remains a
question while gathering covers only the primary calendar. `model_work.py` owns durable per-revision
attempt claims and opaque receipts in `events/model-work.sqlite3`; validated artifacts survive
delivery retries. The proxy owns provider call accounting. Stable receiver job IDs distinguish
retries from later occurrences; dead-worker claims retain unknown spend and allow bounded recovery.
Notification source dates survive queue release and are displayed outside model-written copy.
Prep offers reuse exact-address mail already in the consented queue/snapshot, plus invitation and
graph facts; no source fetch or research call is added. Cached exact-address envelopes can supply a missing
prep name with delivery-time consent. Calendar-change copy comes directly from the detector:
dated moves, explicit cancellations, or a removed slot without guessing why. Context/prep entries
do not mint separate meeting-change nudges; unique nonrecurring iCalUIDs preserve identity across
replaced IDs. Calendar normalization retains explicit cancellation and recurring-occurrence metadata.
Brief extraction, critic and revision own separate retry allowances. See [bounded model
work](BOUNDED-MODEL-WORK.md) for contracts, storage and reports.

One page on what runs inside the container, who owns what, and which files cross between them. The
code is the source of truth; this is the map you read first. For *why* a nudge fired, see
[HOW-SOTTO-DECIDES.md](HOW-SOTTO-DECIDES.md); for every env var, [RAILWAY.md](../RAILWAY.md).

> **Prefer to click than to read?** [playground-architecture.html](playground-architecture.html) is
> this page made explorable — a layered node map of the same modules, threads and shared files,
> with six saved views, a clickable drawer per node, and the shared-file sheet; every number on it
> is interpolated from the same rules island the drift guard checks against code.
> [playground-feedback-loops.html](playground-feedback-loops.html) does the same for the six
> self-improvement loops, each badged CLOSED, RECORDING or PLANNED. Both are single self-contained
> files: open them from disk, or visit `/static/playground-architecture.html` on a running deploy.

## The spine — read this first

Daily and first-use briefs follow **gather → compose → validate → commit essential memory →
deliver**, with ancillary learning queued separately. Meeting prep, the proactive watcher and
the midday digest have their own procedures and share the source, relevance and delivery rules.

| Stage | Owner | In one sentence |
|---|---|---|
| **gather** | [`_shared/scripts/gather_google.py`](../sotto-chief-of-staff/_shared/scripts/gather_google.py) (+ `gather_granola.py`, `_shared/lib/attachments.py`, the Bridge's `read_local`) | Deterministic Python pulls the raw material; no model is involved. Two facts it reads that nothing read before (Sep 2026): the stale-sent lane (`gather_stale_sent` — one `in:sent` search plus one `threads.get` per candidate: is the last word on the thread still yours?) and your own answer to each invite (`normalize_event.my_response`). |
| **compose** | [`_shared/scripts/compose_brief.py`](../sotto-chief-of-staff/_shared/scripts/compose_brief.py) | One Gemini call turns the gathered payload into prose and actions, plus the optional critic/revise pass — and the debts code can see are minted by code beside them (`_stale_debt_actions`, `_rsvp_actions`): an email you sent that nobody answered, an invite you haven't answered that is nearly here. The model's own row for the same thread or event wins. After revision, a deterministic source-backed calendar preview precedes optional loop expansion; the model cannot delete its schedule. |
| **validate** | [`_shared/lib/brief_validate.py`](../sotto-chief-of-staff/_shared/lib/brief_validate.py) | Checks structure, identifiers and deterministic obligations; findings guide the critic/revision pass. These checks cannot prove every model judgment correct. |
| **commit essential memory** | [`_shared/scripts/learn_step.py`](../sotto-chief-of-staff/_shared/scripts/learn_step.py) `--phase essential` | Applies extracted knowledge and merges actions into continuity before returning the brief for delivery. Failure retries these writes against the saved artifact. |
| **deliver** | `receiver.py` → `outbox.py` → `adapters/hermes/runtime_api.py` | The outbox persists the artifact, checks its due time and current eligibility, then claims the brief marker at the send seam. A missing composed archive cannot claim the day. Structured provider acceptance completes delivery; retrying a known result does not recompose it. |
| **finish learning** | [`_shared/scripts/learn_step.py`](../sotto-chief-of-staff/_shared/scripts/learn_step.py) `--phase ancillary` | A durable background job runs `style_extract.py`, `draft_outcomes.py`, `granola_graph.py` and `prewarm_graph.py --sync-contacts`. It merges results into `briefs/<date>.<kind>.learned.json`; queued or failed ancillary work cannot suppress a valid brief. The default `--phase all` retains the standalone six-writer command. |

**The model judges meaning; code governs interruption and action authority.** Native Gemini classifies relevance, and the shared funnel applies cadence, consent, freshness and delivery policy. Hermes still exercises model discretion during interactive conversation; an adapter boundary cannot guarantee the quality of its replies. The funnel is documented rule by rule in [HOW-SOTTO-DECIDES.md](HOW-SOTTO-DECIDES.md), and
the seven things that can start a nudge are the producer table at the top of that page.

**One shared Gmail reader and client.** `_shared/lib/gmail_read.py` owns native Google client
construction on the existing `google_token.json` and recursive Gmail MIME body extraction.
`poll_gmail.py` and `gather_google.py` use the same full-message reader; nested multipart mail
retains its plain-text body or readable HTML fallback instead of becoming a search preview.
Failed message reads remain unacknowledged for the next poll while other messages proceed.
The gather reuses the full message's MIME tree for attachments. `gather_google._gmail_service`
remains the compatibility entry point used by `google_action.py`, backed by the shared builder.
The upstream Hermes CLI still owns the search/calendar command interface.
The attachment half converts what it fetched through
[`_shared/lib/attachments.py`](../sotto-chief-of-staff/_shared/lib/attachments.py), the owner of the
lane's three caps for both the fetch side and the prompt side: *an attachment Sotto can read becomes
text under its email; one it can't is named, never guessed.* Conversion is in-process and local —
no hosted OCR, no API key, no env var (see [HOW-SOTTO-DECIDES.md](HOW-SOTTO-DECIDES.md) §
*Attachments*).

### The open-loop path — input to outcome

This is the complete path for a Granola commitment. It deliberately has one model judgment and
small deterministic guards around it, rather than a second workflow engine:

```text
Granola notes / summary / transcript
  → gather_granola: meeting_id + exact start/end + source text
  → ancillary Learn capture: up to 3 new/changed revisions from the 14-day overlap;
       successful revisions checkpoint in the existing learned receipt, failed writes retry
  → compose_followup: owner_is_user + verbatim source_snippet + optional existing_anchor_key
  → apply_commitments:
       meeting id + copied quote + named owner + action/deliverable words must match source (or drop)
       same meeting + direction + source snippet → same occurrence
       validated live same-direction anchor     → merge with that open loop
       otherwise                                → create a new open loop
  → continuity ledger → brief / Loops page / proactive watcher
  → triage verdict with decision_id
  → receiver send → delivery receipt with the same decision_id
       delivered → count “already nudged” and finalize a chase/handoff
       failed     → neither
```

Capture does **not** wait for a confirmation turn: private bookkeeping is written when the grounded
extraction succeeds. Both composition and the actual write recheck four mechanical model claims
against the gathered meeting: its id exists, the supporting quote was copied, that quote names the
claimed owner, and it contains the obligation's action/deliverable words. Ambiguity drops the
item instead of opening a loop. Exact occurrence identity handles reruns; the model may suggest at
most one semantic merge, and code accepts it only if that anchor is live and points in the same
direction. There is no fuzzy-matching subsystem. New source-backed Granola commitments can close
only through the existing task-specific, source-message evidence checks; an explicit user lock
remains manual. An ordinary reply, an old creation date, or the user's own chase cannot silently
erase either.

The same ledger owns every obligation. Separate asks remain separate rows and the brief groups them
once per person only when rendering. Optional model `loopUpdates` name one ledger ID and revision
plus a source message ID and verbatim quote; the writer verifies counterpart, direction, original
timestamp and quote against the snapshot. Generic replies, links, calls, calendar contact, age,
passed deadlines and unreachable counterparts cannot close or expire an obligation. Something you
owe that nothing has touched for 14 days may park (`status: parked`, `parked_at`; the clock is
`ledger_io.last_touch_day`). The composer attaches exact `parking_notice` effects to its warning;
`brief_runner` stages them in the existing outbox and `delivery_effects.finalize` calls the locked
continuity writer after provider acceptance. `parking_notice_at`, `parking_notice_touch` and
`parking_notice_version` bind that receipt to the unchanged task. Only an accepted notice from an
earlier local day permits parking. Model-supplied notice metadata is discarded; the pre-send check
revalidates the task and its mutes, and a stale warning uses the existing brief-replacement path.
No warning or a stale notice leaves it active. The file stays;
`load_active()` skips parked rows, and re-capture or `keep` reopens one with `reopened_at`.
Snooze's return date restarts the parking clock; keep/snooze never rewrite the original request
date or completion cutoff. A valid completion proposal can resolve a parked task. `/api/loops`
serves the parked group apart from the open list, with `keep` as its verb.

## Accepted work and delivery

### Boundaries to preserve

The unit of deployment is one tenant runtime, with the same backend and skill pack in Cloud and
self-host. Keep new behavior within these owners:

| Responsibility | Authoritative owner | How a new feature uses it |
|---|---|---|
| Source access and authority | Shared source/consent checks; Bridge executes Mac operations | Add a normalized reader or an explicitly approved action, with the same checks in both modes. |
| Meaning and memory | Shared relevance judgment, cited knowledge graph and continuity ledger | Store durable facts or live obligations through their existing writers. History checkpoints describe progress, not another memory store. |
| Scheduling and execution | Receiver and `work_queue.py` | Declare a procedure on the existing schedule. Preserve its input identity, deadline and saved result across retries. |
| Automated delivery | `outbox.py` and channel adapters | Hand off the saved result once. Adapters return transport receipts; delivery effects update the existing domain stores. |
| Interactive conversation | Supervised Hermes process using shared tools and policy | Keep channel formatting in adapters. Chat does not create a second scheduled brief or memory implementation. |
| Managed accounts and spend | Cloud account service and model proxy | Authenticate tenants and enforce spend at admission. These services do not own relevance, memory or scheduling. |

Work and delivery are separate recovery stages because a completed model result must survive a
transport outage. Their records are not interchangeable. A new queue, scheduler, store or service
needs a responsibility that none of these owners can serve; a new feature alone is not that reason.

```mermaid
flowchart LR
  Bridge[One Bridge: consented Mac sources] --> Observe[Source observations]
  Google[Google and Granola] --> Observe
  Observe --> Context[Canonical people, evidence and open loops]
  Context --> Judge[Shared relevance judgment]
  Clock[One declared schedule] --> Work[Durable accepted work]
  Judge --> Work
  Work --> Procedure[Shared procedure and Gemini-native model calls]
  Procedure --> Outbox[Delivery outbox]
  Outbox --> Adapter[Hermes transport adapter]
  Adapter --> Channel[iMessage or self-host channel]
  Adapter --> Ack[Provider acceptance]
  Ack --> Effects[Replayable delivery effects]
  Effects --> Context
  Context --> Chat[Hermes interactive chat]
```

`events/work.sqlite3` owns **work before composition completes**. The existing
`events/outbox.json` remains the only owner of sends and their retries. These are sequential
stages with an idempotent handoff, not competing delivery ledgers. A job has a stable ID, due
time, useful deadline, separate composition and handoff attempt counts, and a renewable lease.
Each phase allows three attempts. Shutdown stops new dispatch, terminates tracked children and
gives each worker a fair share of a ten-second grace to return its own lease. A graceful stop
refunds its active phase, including the delivery handoff, where the worker has no interrupt path
and a send may outlast the grace: the shutdown itself refunds every lease its workers could not,
and a worker that settles concurrently makes that refund a no-op. Children still running at the
deadline are killed rather than orphaned, before any refund makes their job claimable again. A
refunded job is immediately claimable, and it keeps its committed composition, so the next instance
resumes at the handoff instead of composing a second time. An ambiguous crash still
consumes the attempt. Exhausted jobs are closed while the same claim continues to the next due job. Its exact completed output is committed
before handoff, so a crash there does not buy another composition. Duplicate queue items retain
ownership even if a later valve batch groups them differently. Retrying terminal input releases
only that input's alias; unrelated failed work retains ownership. Raw inputs/results are removed
from terminal work rows; diagnostic metadata lasts seven days.

The receiver runs at most two jobs concurrently, with at most one background job. Interactive
Hermes is a separate supervised process. Due work normally has priority; background work waiting
30 minutes receives a turn without occupying both slots. This keeps history and secondary learning
from starving under sustained traffic. Each lease has a distinct owner token, so an expired worker
cannot commit a replacement worker's result. The queue lives on the existing tenant volume.
Schema migration and mutation share one immediate SQLite transaction. Opening the queue preserves
its journal mode: fresh stores use SQLite's rollback journal and existing WAL stores retain WAL,
both with full synchronous durability. Admission does not race a journal-mode change at startup.
Shared JSON sidecar locks allow nesting only within the owning thread; concurrent threads and
processes remain excluded, including when consuming a one-use approval before a send.

The receiver loads shared delivery code from the immutable image's `/app/sotto-skills/_shared/lib`
or the equivalent source-checkout path, never from the writable Hermes home. Startup validates
the mandatory delivery imports before serving health checks. The image build runs
`runtime/trigger-receiver/check_runtime.py` as the managed user. Its synthetic sources drive a
nonempty proactive scan and both brief galleries through the real receiver, outbox, adapters,
acceptance receipts and completion effects. It also tests retry recovery and duplicate suppression.
Only source input and external transport are fixtures; the gallery fixture is a loopback sidecar
and the text fixture is a subprocess implementing the Hermes CLI contract. The receiver suite runs
this in both deployment modes and requires it to reject the two September 18 delivery regressions.
It uses temporary data without model calls or external sends; device display remains a live check.

Daily jobs can recover within four hours of their declared time. The separate ten-minute
wake-folding window describes a currently composing brief, not the lifetime of accepted work.
Normal brief preparation starts ten minutes early. The outbox's `not_before` holds its completed
text until the declared time; consent and relevant Calendar facts are checked again before claiming
the brief marker. A changed context revision permits a fresh composition. A slow essential model
call or prolonged outage can still miss the deadline; health exposes outstanding/failed work
rather than claiming punctuality it cannot prove.

Source outcomes distinguish successful empty reads from partial, unavailable and disabled reads.
A failed Calendar refresh retains the last good observation and cannot manufacture cancellations.
A revoked source is excluded from cached inputs and contextual memory. Observation timestamps and
coverage remain separate from the time a cache file is rewritten.

After provider acceptance, an outbox row drops its message text and retains `effects_pending`
until the associated bookkeeping succeeds. A separate cross-process file lock serializes these
callbacks without holding the main outbox lock; a slow callback cannot be reclaimed after its
retry timestamp. Callback process crashes also consume the five-attempt limit; an exhausted row is
quarantined before invoking it again. A quarantined row keeps what diagnoses it — its acceptance
receipt, label, run id and failure reason — and drops the effects' per-source addressing exactly as
an applied row does, so no handle, address or thread id survives in a row that will never replay.
Retrying those effects never resends the message. Closed receipts remain for
seven days after terminal delivery or effect settlement, including delayed recovery.
Chases, proactive seen state, intention completion and detached offers activate after acceptance.
An offer must correspond to the actual delivered question; multiple unanswered questions require
clarification. Acceptance is a provider receipt, not proof that the user read the message. A network
failure after an uncertain send remains an explicit ambiguity; universal exactly-once delivery
cannot be promised without a provider idempotency contract.

Optional research uses prepared/cached results. Notes in `cache/brief-granola.json` are reusable for
24 hours, with a freshness warning beyond 30 minutes, and expire under the one-day retention rule.
Welcome voice/identity seeders share a 20-second allowance. Essential knowledge and continuity writes
precede brief delivery; draft outcomes, voice learning, meeting-note learning and Contacts synchronization
have their own durable follow-up, which may finish before or after transport acceptance. Their failure
cannot suppress an already valid brief. Historical
learning leaves capacity for the existing Dreamer; explicit user corrections remain authoritative,
and silence is not negative feedback or permission for an external action.
Repeated or reference-free fact proposals leave evidence recency unchanged; only distinct source
evidence or an explicit correction refreshes it. A temporarily forbidden history source holds its
existing cursor and performs no learning until permission returns.

## The two processes

The receiver owns scheduling, accepted work and delivery; Hermes owns interactive chat and its
model/tool loop. `adapters/hermes/start.sh` resolves the delivery channel once for both. Managed
Cloud uses Photon/iMessage; self-host keeps its configured channel. Existing Telegram/WhatsApp
pairing remains bounded and an unlinked Telegram leaves the receiver running without starting a
gateway that would consume the next pairing message.

The shipped container first takes `runtime_lock.py`'s lifetime lock on the tenant volume. Managed
boot verifies the real mount and explicit tenant/volume identity receipt before starting writers.
The adapter retains both essential process IDs and supervises them as process groups. An unexpected
exit (including zero) recycles the whole instance; shutdown terminates and reaps both groups. It
does not hide a dead receiver behind a healthy gateway or mask child failure with a bare `wait`.
The receiver owns and stops its detached worker groups. No extra supervisor service is required.

`runtime_api.py` contains Hermes-specific argv, structured sends, paths, sessions, personal routines
and timezone reconciliation. The receiver owns the durable job and outbox stores, and the adapter
returns provider acceptance metadata rather than making business delivery claims. The separate
control credential stays out of Hermes and worker environments. That inheritance boundary does
not sandbox an agent that has unrestricted access to tenant files or processes.

Managed boot uses `managed_identity.py` to protect the product's `SOUL.md` and
its replacement paths before launching workloads. The data and Hermes directories
are root-owned and sticky with group write access: the Sotto UID can create and
update its own state, but cannot replace the root-owned identity or Hermes directory.
The default-home alias is protected and `HERMES_HOME` points to the canonical path.
Boot's Hermes CLI commands run as the workload UID so upstream home initialization
cannot remove these permissions. `managed_config.py` replaces the persona atomically
from image sources using an exclusive temporary file. Sessions, `preferences.json`
and `knowledge/master.md` remain writable. `check_identity.py` exercises that boundary
with the actual UID and pinned Hermes in every Linux image build. This protects
the product file; independent tool authorization and source-consent checks still
enforce actions and access. Self-host persona customization is unchanged.
The local adapter gate also measures the assembled persona and shared writing rules:
at most 19,000 characters, leaving 1,000 below pinned Hermes's 20,000-character
context-file limit. The Linux image gate verifies the complete identity loads unchanged.


## Receiver modules

All under `runtime/trigger-receiver/`; most modules are stdlib-only, while managed pairing uses the
image’s pinned cryptography dependency. `receiver.py` loads its hook-based helpers with
`importlib` and injects `HOOKS` — late-bound lambdas over its own globals — so no module ever
imports the receiver back.

| Module | Owns |
|---|---|
| `managed.py` | Managed activation/source checks, granted Google scope receipts, and the one-time source notice policy. Missing or foreign tenant state holds scheduled briefs, including Bridge wake triggers; self-host behavior is unchanged. |
| `onboarding.py` | Durable first-use reservation and delivery acknowledgement; read-only setup progress, recovery after failure and preservation of established installations |
| `brief_runner.py` | One deterministic daily/first-use procedure in Cloud and receiver-based self-host: durable artifact and inputs, essential knowledge/continuity writes, exact chat text, deferred ancillary learning |
| `procedure_runner.py` | Shared proactive, digest and relationship-pulse procedures; eligible proactive results use deterministic templates or the bounded direct writer, and reviewed digest coverage advances only after delivery acceptance |
| `work_queue.py` (shared library) | SQLite ownership of accepted work until output is handed to the outbox; stable input IDs, bounded leases/retries, two worker slots with at most one background worker |
| `receiver.py` | The HTTP surface (`/health`, `/trigger`, `/bridge/*`, `/mcp`, `/setup*`, `/google/*`, `/connect/*`, `/debug/*`), brief trigger dedup, the brief schedule (`crons.json`'s `runner: receiver` jobs), the event funnel's dispatch half, the setup wizard page, and every skills-tree subprocess it forks |
| `dashboard.py` | The Window: `/app`, `/app/login`, `/static/*`, `/api/*` — sessions, CSRF, CSP, lockout, the JSON API, and every write lever (facts, loops, prefs, cadence, graph, voice, run-now, golden labels); Cadence also shows scheduled one-shots and read-only `user-*` Hermes routines |
| `calendar_context.py` (copied from `_shared/lib/` by Docker) | Shared human-attendee normalization, explicit user participation and the schedule day grammar used by composition and card layout. Explicit context notes with the exact same interval and a unique matching meeting subject attach to that meeting as `supporting_context`; their descriptions remain available for prep, without creating a second busy block or invite. Calendar diffs only nudge for declines in the user's one-to-one meetings; prep and docket exclude resource rooms. |
| `calcache.py` | The ONE calendar cache — the `gather_google.py --skip-gmail` fork, its 10-min TTL, the refresh thread that writes `cache/calendar_today.json`, the post-meeting tap detector, and the calendar-diff detector with a durable comparison baseline (declines, last-minute invites, moves, cancellations → `calendar_change` events into the funnel). A meeting the user DECLINED is dropped before the served list (the Today view and the funnel's in-meeting hold never see it) while the raw wire events keep it for the diff |
| `connectors.py` | The connector registry, both kinds: remote-MCP OAuth 2.1 (discovery → DCR → PKCE → token file) for the Connect tiles, and the key-based search providers it renders read-only beside them — **and `write_text`/`write_json`, the one atomic-write helper the whole image uses** |
| `outbox.py` | The durable delivery outbox — `events/outbox.json`, the idempotency key, the retry backoff, the per-kind expiry, and the drain heartbeat. **Nothing Sotto says is marked delivered until the channel says so; what fails waits its turn instead of dying.** |
| `retention.py` | THE table of what the volume keeps and for how long — every TTL, the three policies that apply them, and the guard that keeps the graph, the ledger and the corpus off every rule. It owns no clock: `receiver._retention_tick` fires it once a local day from the cron thread. **Retention is machinery, not an instruction a run can decline.** |
| `tzchain.py` (a copy of `sotto-chief-of-staff/_shared/lib/tzchain.py`, placed here by the Dockerfile) | THE timezone chain — `SOTTO_TIMEZONE → TZ → the wizard's config/settings.json → UTC` — resolved by one file in every runtime: the skills import it as a sibling, the receiver and dashboard load it by path (this copy in the image, the original in a checkout), and `start.sh` runs it. Four hand-mirrored copies drifted on their last rung (server local vs UTC), invisible on Railway and a day off on any other machine after 5pm (Sep 2026). |
| `sessions.py` | Shared session-archive procedure, invoked at boot and nightly. It delegates Hermes session discovery and archive commands to `adapters/hermes/runtime_api.py`; runtime paths and CLI details stay inside the adapter. |
| `relay.py` | The reverse-MCP relay: the Mac long-polls `/bridge/poll`, Hermes calls `/mcp` locally, no tunnel |

The directory also carries `keys.py`, a byte-identical vendored
copy of `_shared/lib/keys.py`. The two runtimes must compute the same ids (`queue_key`,
`sample_hash`) for "nudge me now" and the Voice card to address the right row, and the receiver
image has to render those surfaces with no skills tree on the box — so it copies rather than
imports, and `tests/test_docs_drift.py` fails the suite the moment the copies diverge.

The shared Hermes `notification_config.py` reconciles platform lifecycle notices during
container boot and local installation. Shutdown, restart, startup and interrupted-cron
notices stay in operator logs on Telegram, Photon and configured channels; normal replies
and actionable source/provider failures retain their existing delivery paths. The scheduled
digest runner uses the checker's default CLI action, with a real subprocess regression check.
The same writer sets Hermes' existing `display.busy_ack_enabled` to false. Redirected, steered,
queued and interrupting-run acknowledgments stay out of chat; Hermes routes the correction before
checking that setting. The writer also disables displayed reasoning, automatic memory-update
notices, runtime footers (including per-channel overrides), and routine compression progress.
Memory and compression still run. Approval policy, final replies and actionable errors are
unaffected. The shared writing-style reference asks for ordinary-language outcomes and recovery
steps, without internal paths or tool jargon unless the user asks for technical detail.

## Background threads

These loops contain their own failures. The bounded work pool separately leases accepted jobs;
restarting a thread or process does not erase accepted work.

| Thread | Cadence | What it does |
|---|---|---|
| Gmail poll (`receiver.start_gmail_poll_thread`) | `SOTTO_EMAIL_POLL_SECS`, default 90s | Claims nothing while polling; feeds new mail through the same funnel as Bridge events, then acknowledges ids only after the receiver durably accepts them |
| Release valve (`receiver.start_valve_thread`) | `receiver.VALVE_INTERVAL_SECS_DEFAULT` = 900s | Forks `triage_event.py --valve` so a nudge held during cooldown/quiet/catchup can still get out |
| Sotto cron (`receiver.start_cron_thread`) | `receiver.CRON_TICK_SECS` = 60s | Reads the shared daily, weekly and interval declarations and durably admits due work. Briefs start up to ten minutes early with delivery held until due; missed daily jobs can catch up within four hours. Stable work IDs survive restart; the work queue owns bounded retries. The same heartbeat starts quiet memory work, renews the managed model lease, runs retention at 3:30 AM local, and archives sessions through the Hermes adapter. Timezone changes clear the process-local fired stamps; durable delivery markers still prevent a second accepted daily brief. |
| Delivery outbox drain (`outbox.start_drain_thread`) | `outbox.DRAIN_INTERVAL_SECS` = 60s | Retries every message the channel hasn't acknowledged — backoff doubling from 60s to a 900s cap, then `failed` (a brief, loudly, when its local day ends) or `expired` (a nudge past 240 min). After acceptance, effects retry five times without resending, then quarantine with their receipt, label, run id and reason retained and their per-source addressing dropped. |
| Update check (`receiver.start_update_check_thread`) | daily | One GitHub fetch → `cache/update_check.json` (the ONE writer); silent on an unstamped dev build |
| Work dispatch (`receiver.start_work_thread`, thread `sotto-work-dispatch`) | every 5s, or the moment a job is admitted or finishes (`_WORK_WAKE`) | Claims due jobs from `events/work.sqlite3` under a fresh per-claim owner and starts one `sotto-work` thread per claim, within the queue's two worker slots |
| Work execution (`receiver._work_one`, thread `sotto-work`, one per claimed job) | for the life of the job | Runs the declared procedure (or reuses a saved result), saves the exact output, renews the lease once more, hands the text to the outbox, then `finish`es or `fail`s the job for bounded retry. Incoming-event notification writing gets four separate provider recoveries for explicit 429/500/502/503/504 responses, with exponential delay inside its original deadline. `model_work` preserves every call receipt while leaving validation attempts available. Only the declared notification runner's typed exit can request recovery; saved delivery handoffs keep their own limit. Final receipts distinguish queued retries from stopped work. |
| Work lease (`receiver._work_one.keep_lease`, thread `sotto-work-lease`, one per running job) | every 30s while its job runs | Renews the job's 120s lease; a renewal the queue refuses (the lease was recovered by another claim) ends the keeper, and the worker's own pre-handoff renewal then fails rather than delivering twice |
| Session archive (`receiver._session_archive_tick`, thread `sotto-session-archive`) | once a local day at the retention sweep slot (3:30 AM), spawned from the cron tick | Archives every Hermes chat session through `sessions.py` on its own thread, so one slow CLI call cannot delay the brief window the cron thread is in |
| Calendar refresh (`calcache.start_refresh_thread`) | `SOTTO_CALENDAR_REFRESH_SECS`, default 900s | Refreshes the snapshot, rewrites `cache/calendar_today.json`, asks `tap_tick()` which meetings just ended, and `change_tick()` what changed about the imminent calendar |

## Subprocess boundaries

Longer skill procedures run in subprocesses; lightweight shared source, queue and delivery helpers
are imported directly. Python procedures use `sys.executable` and inherit `SOTTO_DATA` through the
receiver's environment policy. The tree is located by
`receiver._find_sotto_script`: `SOTTO_SKILLS_ROOT` → the Hermes layout → the repo-relative source
tree, resolved once per script name.

| Caller | Script | Shape |
|---|---|---|
| `receiver.run_triage` | `event-triage/scripts/triage_event.py` | events JSON on stdin → verdict JSON on stdout, synchronous, 30s |
| `receiver.run_valve` | `event-triage/scripts/triage_event.py --valve` | verdict JSON on stdout, 30s |
| `receiver._poll_gmail_once` | `event-triage/scripts/poll_gmail.py` | event list on stdout, 180s |
| `dashboard._run_skill_cli` | `_shared/knowledge/knowledge_edit.py` · `preferences.py` · `style_extract.py --confirm` · `event-triage/scripts/triage_event.py --promote` (via `receiver.run_promote`) | `{"ok": …}` on stdout, 30s — ONE subprocess policy for the whole write surface, so every dashboard edit rides the identical code path the same instruction typed in chat would |
| `calcache._run_calendar_gather` | `_shared/scripts/gather_google.py --skip-gmail` | writes legacy event data plus a source-result receipt, 60s; only a complete successful observation replaces the authoritative snapshot |
| `receiver._seed_snapshot_from` | `_shared/scripts/compose_brief.py --seed-snapshot` | daemon thread, 120s — a wake-push that arrives after the day's brief already delivered, or within `receiver.BRIEF_CRON_WINDOW_MIN` (10) minutes of its cron while that run is still composing, composes nothing; its payload is folded into the local snapshot by the brief's own snapshot writer, and the funnel surfaces the catch-up |

Accepted background work enters through `receiver._spawn_and_deliver`, which commits a job before
returning. The bounded worker executes the declared Python procedure or adapter-owned Hermes
command, renews its lease, and saves the exact output before handing it to `receiver._deliver_text`.
The outbox persists that result under its stable run ID before the first send attempt.
`adapters/hermes/runtime_api.py` owns the transport call: an exit code alone is insufficient;
the adapter requires structured success and a provider message ID. This records provider acceptance,
not device delivery or reading. Failed or uncertain sends remain retryable; acceptance followed by
a process crash can still produce a duplicate without a provider idempotency contract.
This is also where deliver-once
stopped being a prompt: a brief-kind row proves it owns `briefs/<date>.<kind>.delivered` before the
channel is asked — no marker and the row claims it with its own run id, another run's id and the row
goes `superseded`, receipted and never sent (Aug 30, 2026: the evening brief went out twice because
the skill's own claim step was skipped). Before it claims at all, the row must have a composed brief
behind it — `briefs/<date>_<kind>.json`, which `compose_brief.py` archives before printing a word —
or it goes `failed` on the spot as `not_a_brief`, unclaimed, so the lane that can still compose is
not stood down by an improvised recap (Sep 2, 2026). Silence is a token, not a hope: a spawned
run with nothing to deliver replies the `NO_NUDGES` sentinel, which the seam records as an empty
run instead of sending — "say nothing and end the turn" was an instruction models reliably
ignored, and three "all clear" messages in one evening proved it (Aug 2026). Email asks, every
other channel links — and the seam enforces that half too: a `mailto:` never leaves the box
(`receiver._strip_mailto` — a markdown link keeps its label, a bare URL and its "tap to send"
line go), because every run delivered here is unattended and an unattended run's email path IS
the Gmail-draft ask (Sep 4, 2026: a nudge arrived with the ask and a 200-character percent-encoded
mailto Telegram rendered as plain text).
The Hermes `google-workspace` skill's `setup.py` is an eighth, forked for Google auth only.

**Adapter/plumbing variables** (script-to-script, never a user setting — they are deliberately
absent from RAILWAY.md's table): `SOTTO_DATA` (the volume path, `/data` in the image),
`SOTTO_RUN_SKILL` and `SOTTO_SKILLS_ROOT` (above), `SOTTO_UNATTENDED` (set to `1` on every skill run the receiver spawns — the seam `google_action.py`'s send gate reads; the interactive gateway never carries it), `SOTTO_MCP_TOKEN` (the ROOT bearer,
which `start.sh` sets from `BRIDGE_TOKEN`; the Bridge's lanes — relay dial-in, event ingestion,
wake-push — and the operator surfaces take the root, while `/mcp` takes only
`receiver.derive_mcp_token(root)`, the one-way HMAC bearer `configure_mcp.py --derive-mcp` hands
Hermes at boot, so the prompt-injectable agent never holds the trust anchor), `SOTTO_TRIGGER_PORT` / `SOTTO_TRIGGER_BIND` (the
receiver's port and bind address, used only when Railway's `PORT` is absent — local runs get `8787`
on `127.0.0.1`), and `SOTTO_BRIDGE_BIN` (read once by `adapters/hermes/install.sh`: an explicit path
to a `sotto-bridged` engine, overriding both locations it probes for local mode — the built binary
and the one bundled in `/Applications/Sotto Bridge.app`).

## The shared `$SOTTO_DATA` files

Everything that crosses a process boundary crosses as a file on the volume. Receiver-owned JSON
snapshots use `connectors.write_json` (tmp at 0600 → `os.replace`); skill-owned transaction files
use the same temp-then-replace pattern, and the continuity ledger adds one cross-process lock around
read/modify/write. JSONL records are append-only and bounded. **"skills" below means the
`sotto-chief-of-staff` scripts, running in a different process.**

| File | Writer | Reader |
|---|---|---|
| `.sotto-volume.json` | `managed_volume.py initialize` | managed boot and recovery verify the mounted tenant identity |
| `.sotto-runtime.lock` | `runtime_lock.py` | supervisor and offline recovery refuse concurrent writers |
| `.sotto-recovery-hold.json` | `recovery.py restore` | managed boot holds a restored tenant until explicit activation |
| `proxy.sqlite3` | model proxy | per-tenant lease, rate, reservation and content-free usage ledger on the proxy's separate volume |
| `config/onboarding.json` | receiver `onboarding.py` | receiver first-use tick and scheduled hold |
| `config/model-lease.json` | adapter `model_lease.py` | receiver renewal heartbeat and operator recovery diagnostics |
| `config/cloud-pairing.sqlite` | receiver `cloud_pairing.py` | device grant redemption, authentication, listing and revocation |
| `config/source-state.json` | shared `source_context.py` | consent-aware readers, history learning and authenticated dashboard diagnostics; bounded per-source access status and last extraction counts/timestamp, never source content |
| `knowledge/history-state.json` | `memory_cycle.py` | diagnostics/setup: initial bounds, progress, extraction/Dreamer request latches, per-source history protocol latch and bounded work receipt |
| `knowledge/dreamer.json` | `dreamer.py` | Semantic fact-evidence selection and review receipt; full-file hashes still guard concurrent writes |
| `knowledge/conflicts.json` | `knowledge_update.consolidate` | `knowledge_query.py` renders unresolved pairs with their source facts |
| `setup_code` | receiver (boot) | receiver, `start.sh` |
| `config/photon-activation.json` | managed Photon adapter (first owner DM) | managed receiver gate and owner destination check |
| `Mac: cloud-policy.json` | personal pilot operator | Bridge readers and event watcher |
| `config/managed-capabilities.json` | managed receiver after Google OAuth, Granola OAuth/disconnect and Bridge consent | managed receiver gate (missing state means zero sources). Startup repairs only a missing Granola row for an already linked connector on the same tenant; explicit revocation remains false. |
| `config/managed-status.json` | receiver after outbox acceptance of `status:no-sources` | receiver's cron heartbeat (one-time notice) |
| `config/settings.json` | receiver (`/setup/timezone`) | receiver, dashboard, `start.sh`, skills (`timeutil`) |
| `briefs/<date>.<kind>.claim` · `briefs/<date>.<kind>.delivered` | receiver (the `.claim`; and the `.delivered` when the send seam's gate claims it), skills (`brief_marker.py --claim`) | receiver (trigger dedup, the cron-window fold, and the outbox's deliver-once gate — the `.delivered` file's CONTENT is the claiming run's id), skills (`proactive_scan.py`) |
| `briefs/<date>.<kind>.payload.json` | receiver | skills (`compose_brief.py`) |
| `briefs/<date>_<kind>.json` | skills | dashboard; consent-aware item feedback |
| `briefs/<date>.<kind>.named.json` | skills (`compose_brief.py`) | skills (`proactive_scan.py` — which open loops that brief NAMED, so a chase is held only for a genuine double-tell) |
| `briefs/<date>.<kind>.learned.json` | skills (`learn_step.py`, phases `essential`, `ancillary`, or legacy `all`) | Receiver diagnostics: per-writer `ok`, `skipped`, `queued` or `failed`; essential and ancillary durable background work may finish after a valid brief is delivered |
| `events/model-work.sqlite3` | shared model_work.py | opaque attempt claims and usage; 90-day pruning on use |
| `events/notification-artifacts/` | `compose_notification.py`, `capture_commitments.py` | validated notification copy plus persisted per-meeting extraction/apply results and bounded lock shards; seven-day output retention |
| `events/research-artifacts/` | research_attendees.py | coalesced batch results and bounded lock shards; seven-day output retention |
| `events/triage-artifacts/` | relevance.py | validated event classifications and bounded lock shards; seven-day output retention |
| `events/seen.json` | receiver | receiver (idempotency ring — Bridge events, keyed `(source,rowid)`) |
| `events/gmail_seen.json` | receiver, through `poll_gmail.py --ack` after accepted ingest | skills (`poll_gmail.py`) — version 2 keeps separate inbox/sent fixed query bounds, page cursors and pending page IDs; fetch or receiver failure advances neither lane |
| `events/work.sqlite3` | shared `work_queue.py` | receiver workers lease bounded accepted work and retain terminal results |
| `events/work-inputs/brief-<run>/` | receiver `brief_runner.py` | resumable brief and background learning procedures |
| `events/usage-<job>.json` | shared model metrics writer | receiver attaches content-free usage to delivery receipts |
| `events/last.stamp` | receiver | receiver (`/setup` liveness line) |
| `events/bundle-<random>.json` | receiver | skills (the `sotto-event` one-shot); atomically staged with thread/process-unique names and seven-day cleanup |
| `events/last_digest.txt` | skills (`digest_check.py --stamp`; on an in-agent install the brief that wins the deliver-once claim), receiver (`_on_delivered` — the brief the channel ACKED, not the one that claimed: a claim whose send fails and retries into the afternoon must not hide the morning from the 12:30 digest) | skills (`digest_check.py` window), dashboard (`/api/cadence` context line) |
| `events/digest_accepted.json` | skills (`digest_check.py`) after successful silent review; receiver after delivery acceptance | skills (`digest_check.py`) — durable conversation-version coverage prevents repeats without skipping bounded overflow or failed sends |
| `events/queue.jsonl` · `events/surfaced.jsonl` | skills (`triage_event.py`) | dashboard (the Record + the waiting room), skills (`compose_brief.py` reads only verdicts whose `decision_id` has a delivered receipt); surfaced `item_key` identifies completed queue/drop decisions during tap recovery |
| `events/drafts.jsonl` | skills (`action_links.py` — every tap link built with a draft) | skills (`draft_outcomes.py`, run directly by `learn_step.py` each brief: matched against the queue's `is_from_me` signals → outcomes.jsonl + style confirms) |
| `events/delivery.jsonl` | receiver (the ONE writer) | dashboard (the Record, source `delivery`), skills (`compose_brief.py`) — closing rows carry `usage`, correlated `decision_ids`, and content-free photo/text presentation detail |
| `events/outbox.json` | `outbox.py` (the ONE writer) | receiver (the retry drain), dashboard (`/api/runs` — the pending/failed line) — one row per message Sotto composed, written BEFORE the first send attempt and flipped to `delivered` only on the channel's ack |
| `events/outbox.json.effects.lock` | `outbox.py` | Empty persistent lock file for post-acceptance and invalidation callbacks; operating-system locks release on process exit |
| `events/delivery-effects-<run>.json` | shared `delivery_effects.py`, merging procedure contributions transactionally | Receiver result commit and outbox handoff: source/Calendar eligibility, original coverage cutoff, chase/handoff, proactive/intention and offer effects; only delivery-dependent effects activate after provider acceptance |
| `events/sends.jsonl` | skills (`google_action.py`) | you — one metadata-only line per real-effect **attempt** (send, reply, calendar create/delete/RSVP), allowed or refused, carrying `payload_sha256` so "what did Sotto send?" isn't answered by a prompt's promise |
| `cache/calendar_today.json` | calcache | In-meeting hold and delivery eligibility: daily projection, opaque event IDs, actual observation time, status and completeness; an older or failed observation cannot prove a newly observed meeting disappeared |
| `cache/calendar_changes.json` | calcache | Last complete calendar comparison window and acknowledged changes, persisted across restart; no descriptions or message bodies |
| `cache/meeting_taps.json` | calcache | Version 2 daily fired/pending cap ownership; reservations precede dispatch and reconcile with work ownership or completed queue/drop receipts after restart |
| `cache/research_<date>.json` | skills (`research_attendees.py`) | dashboard (`/api/research` cards), skills (`compose_brief.py` joins it) |
| `cache/visual-briefs/<id>/*` | shared `visual_brief.py` renderer | receiver gallery delivery; seven-day staged-artifact sweep |
| `cache/brief-granola.json` | receiver `brief_runner.py` | resumable brief preparation and composition |
| `cache/hermes-version.json` | `start.sh` | receiver (Integrations page) |
| `cache/update_check.json` | receiver (daily update check — the ONE writer) | receiver (`/setup` line, `/api/overview` banner), skills (`compose_brief.py` update line) |
| `cache/update_notice.json` | skills (`compose_brief.py`) | skills (`compose_brief.py` — the once-per-version marker) |
| `connectors/<service>.json` | connectors (OAuth write; `/setup` Disconnect deletes) | skills (`connector_tokens.py`), receiver (presence only) |
| `connectors/<service>.error` | skills (the gather; `/setup` Disconnect deletes) | receiver (`/setup` reconnect hint) |
| `dashboard_sessions.json` · `dashboard_audit.jsonl` | dashboard | dashboard |
| `decks/<view_id>.pdf` · `decks/<view_id>.json` | skills (`docsend_fetch.py` — the pdf is the deck's pages assembled, the json is the read cache that stops a re-ask logging a second view) | you (dashboard `GET /api/decks/<id>.pdf`), skills (cache hits, incl. unattended) |
| `knowledge/master.md` | `_shared/knowledge/master_file.py` (the ONE writer — user-stated words; gateway confirms, dashboard edits shell out to it) | skills (`compose_brief.py`, `compose_meeting_prep.py` — always in the prompt), gateway chat, dashboard (Learned page card) |
| `knowledge/last_local_snapshot.json` | skills (`compose_brief.py`) | skills — the RAW Bridge payload, overwritten each brief and never deleted; its 24h TTL stops reuse, not storage ([DATA-FLOW.md](DATA-FLOW.md)) |
| `knowledge/snapshots/<date>.json` | skills (`compose_brief.py` — dated archive copy, last write of the day wins, pruned after 60 days) | `tools/build_golden_corpus.py` — the corpus's message history; one live snapshot only holds ~a day |
| `corpus/<version>/` | `tools/build_golden_corpus.py` (`--out`; refuses to write on a leak), maintainer Labels route (`/app#labels`, absent from main navigation) (`labels.yaml` only — the owner's judgments) | `evals/run_golden.py` (scoring), maintainer Labels route (`/app#labels`, absent from main navigation). CONFIDENTIAL REGARDLESS — never leaves the volume |
| `knowledge/relationship_state.json` | skills (`relationship_pulse.py`; accepted candidate-offer stamps through `review_candidates.py`, same JSON lock) | skills (`compose_brief.py`, `triage_event.py`'s Tier-1 sender one-liner — a relationship signal, never the VIP gate, which is the stated list or a typed `family_of` relation), dashboard (the attention queue) |
| `knowledge/<kind>/*.md` · `style.json` · `outcomes.jsonl` | skills | dashboard (read), skills |
| `logs/compose_brief.log` | skills | receiver (`/debug/brief-log`) |
| `proactive/<date>.json` | skills (`proactive_scan.py`) | skills (`proactive_scan.py`) — the once-per-day nudge dedup; a read-modify-write, so both producers take `triage_event._locked` on it |
| `proactive/wake_run.last` | receiver (`handle_proactive_wake`) | receiver — its *mtime* is the sleep→wake throttle, nothing is read from inside it |
| `proactive/mute_offers.json` | legacy file; no current writer | no current reader — automatic mute offers and their no-op call seam are removed |
| `proactive/pending_offer.json` | `pending_offer.py`: staged questions become active through post-acceptance `delivery_effects` | Gateway reply resolution uses the delivered question, provider receipt and target. An unmatched reply leaves the offer unchanged; multiple unanswered questions require clarification. An action-bearing offer retains `payload_sha256`, which `google_action.py --offer-bound` must match before acting. |
| `intentions.jsonl` | skills (`schedule_wakeup.py`) | skills (`proactive_scan.py`), dashboard (`/api/cadence`) — append-only one-shot recipes, folded by id; an optional loop anchor cancels the recipe when the loop closes |
| `hermes/platforms/whatsapp/session/creds.json` | the Hermes gateway (**not** Sotto) | receiver (`_whatsapp_status`) — the positive "this account is linked" probe; `start.sh` (the same file decides whether a redeploy keeps WhatsApp as the channel) |
| `whatsapp-pairing.txt` · `google-auth-url.txt` | `wa_pair.py` / `start.sh` | receiver |
| `telegram-link.json` | `telegram_link.py` (the ONE owner of the Telegram handshake — run by `start.sh` at boot, or by hand: [CHANNELS.md](../CHANNELS.md) § Telegram setup) | `start.sh` (`--boot` reuses the captured id for this token, else captures it, and forwards it to Hermes as `TELEGRAM_ALLOWED_USERS` + `TELEGRAM_HOME_CHANNEL`), receiver (`_telegram_status` — the "this chat is linked" probe behind the wizard tile and the nudge gate). It holds the bot token, so 0600 |
| `preferences.json` | `preferences.py` (chat and dashboard invoke it) | skills, dashboard |

Durable delivery receipts carry the same opaque run ID as native model-work attempts. The dashboard
may join those exact identities for bounded attempt/status diagnostics; proxy usage remains the only
spend source, so local token observations are never added to proxy estimates. Legacy receipts without
a run ID remain explicitly unknown. Critic archives retain only fixed validator and critic repair
categories and counts. Chat proxy metadata likewise records declared tool names and result sizes, with
unmatched or truncated history grouped as `unknown`; it never stores result content.

`outcomes.jsonl` also carries content-free `loop_proposal` and `loop_transition` records with stable
IDs. The canonical ledger row saves its latest transition atomically, so a failed diagnostics append
cannot undo a saved correction, and repeated persistence deduplicates. The proof report joins observed
rejected-completion to later manual resolution, capture to dismissal within 24 hours, and reopen to
later dismissal by stable obligation identity. These are review candidates, not measured error rates
or complete historical coverage; missing history remains unknown. Durable delivery runs deduplicate
proposal observations by run identity. Local and legacy observations lack that identity, so the proof
report labels their proposal counts as candidate observations that may include repeated invocations.

Every row but the last is **one-way**: exactly one writer, and readers that never write. That is the
property that keeps the two processes from needing a lock.

`preferences.json` has one writer: `preferences.py`, invoked by chat and dashboard for mutes,
VIPs, tone and snoozes. Learn calls `draft_outcomes.py` directly for outcome receipts and voice
confirmation. It no longer infers approval defaults, dismissal hints or edit counters. Historical
fields and suppression tombstones remain on disk without being applied or exposed as controls;
there is no storage migration. Concurrent preference edits still take the shared JSON lock.

## Who produces a nudge, and who owns a memory

The container half above is the *plumbing*; the spine at the top of this page is the product. Two
small tables finish the map — the same writer/reader treatment as the `$SOTTO_DATA` table, applied
to the two questions it doesn't answer.

**Nudge producers** — seven, and only seven, things can start a nudge. (Full version, with the gate each
one rejoins: [HOW-SOTTO-DECIDES.md § Who can produce a nudge](HOW-SOTTO-DECIDES.md#who-can-produce-a-nudge).)

| Producer | Entry point | Cadence |
|---|---|---|
| Bridge events | `receiver.handle_events` | push, `SOTTO_EVENTS_TICK_SECS` on the Mac |
| Gmail poll | `receiver._poll_gmail_once` | `SOTTO_EMAIL_POLL_SECS` (90s) |
| Release valve | `receiver._valve_tick` → `triage_event.release_valve` | `receiver.VALVE_INTERVAL_SECS_DEFAULT` (900s) |
| Post-meeting tap | `calcache.tap_tick` → `receiver._dispatch_meeting_tap` | on the calendar refresh tick |
| Calendar diff | `calcache.change_tick` → `receiver._dispatch_synthetic` | the same refresh tick — declines, last-minute invites, moves, cancellations of imminent meetings |
| Proactive watcher | `proactive_scan.main` → `triage_event.triage` (in process, one bundle; due one-shot intentions are one input to this producer) | the `*/15` cron |
| "Nudge me now" | `dashboard._post_cadence` → `receiver.run_promote` → `triage_event.promote_one` | you, on the Cadence page |

**Memory owners** — every durable thing Sotto remembers has exactly one writer. (Shapes:
[contracts/exhaust-schema.md](../contracts/exhaust-schema.md), which names the owning script per row.)

| Memory | Lives at | Owner (the only writer) |
|---|---|---|
| The people/company graph | `knowledge/*.md` | `_shared/knowledge/knowledge_update.py` (`knowledge.py` is its model + serializer) |
| Grounded research (people **and** companies) | same files | `meeting-prep/scripts/persist_prep.py` and `_shared/scripts/prewarm_graph.py` — both *through* `knowledge_update.apply()`; there is no second writer for either file type |
| Verified X identity + public-profile provenance | person files above | `_shared/scripts/x_connectivity.py`: exact handle lookup only, then *through* `knowledge_update.apply()`; immutable `x_user_id`, handle alias history, and the 90-day negative cache are durable. Posts/bookmarks stay in staged run inputs, removed by learning cleanup or the existing seven-day abandoned-input sweep. |
| Ambiguous X identity suggestions | `knowledge/x_link_suggestions.json` | `_shared/scripts/x_connectivity.py`; a weak or already-owned match is proposed here instead of being silently attached |
| Meeting attendance + Apple Contacts identity | same files | `_shared/scripts/granola_graph.py` (who you sat with) and `_shared/scripts/prewarm_graph.py --sync-contacts` (every email/phone as an identifier, the card's notes + birthday) — both *through* `knowledge_update.apply()` |
| User-initiated graph edits | same files | `_shared/knowledge/knowledge_edit.py` — which routes *through* `knowledge_update.apply()`, so a dashboard edit and a texted correction are byte-identical |
| Open loops (the continuity ledger) | `knowledge/continuity/*.md` | `morning-brief/scripts/continuity_resolve.py` owns the locked, atomic write API; brief extraction, `apply_commitments.py`, and user edits in `knowledge_edit.py` all write through it (`ledger_io.py` is the shared read side) |
| The master memory file (who the user is, the people around them, their standing Procedures — always in every brief/prep prompt; the gateway reads it in chat; seeded by setup's four questions; editable on the dashboard's Learned page) | `knowledge/master.md` | `_shared/knowledge/master_file.py` — user-stated words only, gateway confirms before writing, dashboard edits shell out to the same CLI; size-capped so "always in context" stays honest |
| Stated preferences | `preferences.json` (`explicit` block) | `_shared/scripts/preferences.py` |
| Writing style | `style.json` | `_shared/scripts/style_extract.py` |
| Relationship analytics | `knowledge/relationship_state.json` | `relationship-pulse/scripts/relationship_pulse.py`; accepted candidate-offer receipts through `_shared/lib/review_candidates.py` under the same JSON lock |
| The Record (every verdict) | `events/surfaced.jsonl` · `events/queue.jsonl` | `event-triage/scripts/triage_event.py` |
| Outcomes | `outcomes.jsonl` | `_shared/scripts/log_outcome.py` |
| One-shot intentions | `intentions.jsonl` | `_shared/scripts/schedule_wakeup.py` |

Reading is unrestricted; writing is not. One writer per file is what lets two processes share the
volume with no lock.

**An interrupted graph update finishes the next time anything touches the graph.** Every write under
`knowledge/` goes through one temp-then-`os.replace` primitive (`knowledge.write_text_atomic`), so a
crash can never leave a person file truncated — `open(path, "w")` truncates first, and that window is
somebody's whole memory. Single files are only half of it: a relation is stored on BOTH people, a
merge writes the survivor and deletes the loser and repoints everyone who pointed at it, and a
re-key renames a file every other file's edges name. Those batches write their op list to
`knowledge/.journal.json` before touching the first file and remove it after the last;
`knowledge_update.graph_lock()` — the critical section every writer entry point opens with — replays
whatever it finds there first. Every journaled op is idempotent, so replaying a batch that already
finished changes no bytes, and a journal nobody can parse is logged and cleared rather than left to
block every future write. Nothing is said unless a repair actually happens.

### The research loop — what a spent token has to leave behind

> **Every token spent on research must leave behind a durable, structured, correctable fact — and
> nothing situational is ever stored.** (CLAUDE.md § Standing bars.)

Research is the most expensive thing Sotto does, so it is the one loop that must compound. Three
grounded call shapes go out through the ONE search seam (`_shared/scripts/web_research.py` — per
capability, the first provider with a key present wins; the three ladders are in
[MODELS.md §5c](MODELS.md)); all three come back through
`persist_prep.py` into `knowledge_update.apply()`:

| Call | What it buys | Where it lands | Read back as |
|---|---|---|---|
| Pass A — profile (5 attendees/call) | title, company, bio, one-line company summary | person fact, conf 0.55, `source: web_research`, + `last_researched` | the person's `known` line, and `profile_is_fresh` skips them for 30 days |
| Pass B — 90-day recency sweep (3/call) | dated, source-URLed activity + public personal texture | person facts, conf 0.6, `source_ref` = the page | the person's `known` line, injected as "do NOT repeat any of this" |
| Focus — `--focus`, ONE person, ONE call | what the company builds, the founder story, the market, traction | **the COMPANY file**: `## About` (replaced) + `## News` (URL-deduped), `updated_by: web_research`, `last_researched` | `knowledge_update.company_knowledge()` → the focus prompt's "already on file, do NOT re-derive it" block |

The company half is why the deep dive is worth its call twice: it is knowledge about an
*organization*, so parking it on whichever human was researched that morning means the next person
from that company arrives cold. `company_knowledge()` is its one read side — for the next research
run, and (through `knowledge_query.py`) for the brief's **Company Context** block.

### What the brief reads back — `knowledge_query.py`

The read side of all of the above, and the one place the retrieval question is answered:

| Emits | From | Gated by |
|---|---|---|
| `person_knowledge` — the compact packed block per person | `knowledge/people/*.md` | Current participants from consented `--local`, `--gmail`, `--calendar` and active loops, resolved through canonical aliases. The `--relevant-days 7` fallback applies only when no current participant identifiers can be obtained. |
| `company_knowledge` — About + the 3 newest news lines, ≤5 companies | `knowledge/companies/*.md`, via `knowledge_update.company_knowledge()` | today's attendee email domains + the packed people's `company`, deduped by file |
| `contact_index` — the identity map | EVERY person file | ungated: it is what resolves a phone and an email to one person |

`mtime` says when a file was last *rewritten*, which was never the same question as "does this
person matter today" — under it, someone who emailed you this morning packed nothing and the model
re-derived what the graph already knew.

`personal_context._participant_records` is the shared permission and exact-identity projection
for the cohort and its topical retrieval hints. `knowledge_query.pack_person` ranks and budgets
whole assertions once, including selected summary references and labeled facts from one existing
relationship edge. Those related people do not enter `memory_participants`. Notifications supply
the current event subject/text to the same reader; chat supplies the requested topic, while brief
and prep gathers supply current source records. Each uses the same graph and correction path in
Cloud and self-host, independent of delivery channel. The reader does not persist a profile view.
Source disconnect stops current-source context; durable memory still requires an explicit archive
or correction. The [retrieval rules](HOW-SOTTO-DECIDES.md#memory-retrieval) state the read budgets.

Two things deliberately do NOT persist, and both are correct:

- **Situational output** — talking points, openers, angles. The meeting-prep prompt writes them
  fresh from durable facts every run, for free. The research prompts no longer *ask* for them
  (`conversation_hooks` was deleted at the schema, not hidden at render time), because inventing
  advice costs output tokens and restates the fact it points at.
- **`$SOTTO_DATA/cache/research_<date>.json`** — a 7-day render cache for the dashboard's research
  cards. The durable half of that same output already went to the graph.
- **Recent X Posts and matching owner bookmarks** — fetched only for upcoming attendees and handed
  to the current brief/prep through mode-0600 `/tmp/sotto_x_context.json`. They never enter the
  graph, research cache, relationship state, or event ledger; the next run overwrites the handoff.

**Correctability** is the constraint that keeps the loop honest. A person fact is corrected with
`knowledge_edit.py --op correct`, and the one thing research can never learn — that an unsaved phone
number belongs to a person — is supplied with `--op add-identifier`, which refuses an identifier
already on someone else's file rather than moving it (two files sharing one identifier is what the
auto-merge reads as proof they are one human). A company has no facts map (its on-disk shape stays
byte-compatible with the Mac app's `knowledge_files.rs`), so its one correctable field is the
About paragraph — `--op company-about`, through the same `apply()` lane research writes through,
from chat or from the dashboard's company page. The write stamps `updated_by: user_edit`, and
research declines to overwrite it: a correction you made stays made. The same rule covers the
*name-merge suggestions*: `--op merge-dismiss` ("these really are two people") tombstones the pair
in `knowledge/merge_suggestions.json`'s `dismissed` list, because the suggestions are recomputed
from the files on disk on every apply and would otherwise ask again tomorrow.

## Where the schedule lives

`adapters/hermes/crons.json` is the ONE schedule declaration and
`adapters/hermes/reconcile_crons.py` is the ONE Hermes desired-state implementation. Cloud boot and
the receiver's live timezone change both call that reconciler; it replaces Sotto system jobs by
parsed job id and always fences `user-*` routines. The install adapters consume the same declaration
for their host-specific setup. See [adapters/README.md](../adapters/README.md) for its field contract.

**Who runs a job is a field, not a second file.** All Sotto system rows currently use
`"runner": "receiver"` in both Cloud and receiver-based self-host: morning/evening briefs,
weekly relationship pulse, proactive watcher and midday digest. The receiver handles fixed daily
`M H * * *`, fixed weekly `M H * * D`, and `*/N * * * *` intervals on its minute heartbeat.
Briefs use `brief_runner.py`; proactive checks, digest and pulse use `procedure_runner.py`.
The proactive runner requires one eligibility check per nudge and exactly one global nudge-off guard,
checks both before composing, and preserves both for the outbox's final delivery check. Missing,
duplicated or unknown guards refuse composition.
Proactive gathering and scanning run before Hermes, including quiet ticks that maintain holds and
intentions. Composer availability is checked before admission and again before the scan; only
valid, nonempty accepted results start it. Accepted source content lives in a private 0600 bundle
whose path is passed to the composer, and the worker removes it on completion or cancellation. Both stages
share the work ID, cached decisions, usage receipt and process group, so retries do not charge
interrupts again and shutdown stops their children. Wake and scheduled triggers retain their
existing cadence; no time-only shortcut can skip a newly due nudge. Other skill jobs use the
adapter-owned runner. All enter the durable work queue and existing outbox. The reconciler removes
old Hermes registrations for these system rows while preserving user-created `user-*` routines.

## Managed pilot model and messaging boundary

The isolated Cloud pilot uses the same pinned Hermes runtime as self-host. `adapters/hermes/hermes.commit` is the actual source pin, passed to the vendored installer and verified at build and managed boot. `managed_config.py` reconciles the Photon plugin, owner allowlist, custom chat route, and removes stale direct model keys from Hermes' persisted environment. The launcher removes the keys before the receiver starts. `sotto_photon/` wraps the upstream adapter through its plugin registration API; owner DMs establish the activation receipt, groups/strangers are rejected, and adapter sends are limited to the owner or the recorded owner DM. This is an adapter policy, not an OS sandbox against a model with shell access.

`adapters/hermes/send.py` requires an unskipped JSON success with a provider message ID for every receiver outbox send in managed and self-host modes. It proves provider acceptance, not delivery to a device. Direct receiver startup runs the same capability preflight as boot and installation; there is no exit-code-only fallback.

Managed boot keeps the root supervisor while `managed_exec.py` drops receiver and gateway to the
shared unprivileged `sotto` UID, preserving their existing 0600 shared files. `control_vault.py`
captures and removes the control credential before runtime imports; the receiver becomes
nondumpable before importing its code and resolves managed skills only from root-owned `/app`.
This protects the control secret and executable code from the gateway. It is not a sandbox against
same-UID modification of tenant data or denial of service; the Linux built-image smoke is required.

`photon_probe_compat.py` applies one hash-checked compatibility patch to the pinned sidecar's synthetic message read classifier: only the exact GUID validation carrying Photon proxy provenance and gRPC INVALID_ARGUMENT is accepted as a server round trip. Authentication, transport and other failures remain inconclusive. A different source hash fails closed; upgrading the pin requires reviewing/removing this patch.

`cloud/model-proxy/server.py` is a separate pilot service and volume. Its `proxy.sqlite3` ledger stores tenant ID, route, model, timestamp, reservation, status and token counts, never prompts or responses. Two fixed surfaces share one tenant bearer: `/openai/v1/chat/completions` for Hermes; `/native/v1beta/models/{model}:generateContent` for all Sotto pipeline calls. `gemini_transport.py` changes destination and authentication only and refuses direct-key fallback in managed mode. Admission accounting is deliberately not an invoice; the owner has disabled the pilot allowance cutoff while retaining usage records. The receiver renews the existing bearer’s expiry to 30 days through `/v1/lease/renew` using an independent control credential. The proxy stores only the lease expiry and token hash; `enabled: false` blocks both requests and renewal. Bearer-byte rotation remains an operator change.


System jobs use the receiver in Cloud and receiver-based self-host. The weekly relationship pulse runs at 9 a.m. Monday in the user’s timezone. All managed scheduled jobs wait for messaging activation and a connected context source, then share the receiver’s outbox and silence handling.

Managed Google authorization lives in `adapters/hermes/google_setup.py`: owner-approved Gmail/Calendar/Contacts read/write grants, managed web consent with a desktop loopback fallback, persisted PKCE state, and atomic 0600 Hermes-compatible token files. The receiver records only granted sources after its authenticated OAuth exchange. Self-host keeps its existing Google setup path. No runtime dependency installation occurs in the managed auth adapter.

Personal Cloud Bridge deployments additionally read local `cloud-policy.json` from the existing Sotto support directory. An explicit source allowlist gates each reader before database access; every supported Bridge reader, including Contacts, can be explicitly enabled; unconsented readers and Bridge sends are blocked. The Sotto identity exclusion covers iMessage history, per-handle, unread and event reads as well as phone calls, FaceTime and WhatsApp identities. Bridge’s Cloud onboarding and grouped source settings write this policy at mode 0600 and report consent plus successful reads to the receiver. A destination change clears the prior destination credential before deleting its policy, so returning to self-host cannot briefly reconnect with the old Cloud credential.

Bridge cold-start pairing is queued until its store and menu-bar anchor exist. Supervisor restarts invalidate old child-exit callbacks, preventing an old credential from spawning a second outbound engine after pairing.

### Shared brief execution

`receiver._managed_brief` dispatches wake, cron and dashboard brief jobs in both supported hosting
modes to `runtime/trigger-receiver/brief_runner.py`. The procedure stores private resumable inputs
and artifacts under `events/work-inputs/brief-<key>/`. It gathers current local and Google context,
reuses prepared research/notes, queries participant memory, resolves continuity, and composes.
Knowledge and continuity writes run as `learn_step.py --phase essential`; ancillary learning is a
separately queued job. Only the composer's exact chat text reaches the outbox.

Retries reuse the saved artifact and original observation cutoff. Current permission and Calendar
revisions, including invalidated prior generations, determine when a fresh composition is needed.
The receiver owns job IDs, duplicate claims, declared delivery time, acceptance receipts and retries.
Explicit nonstandard runner overrides retain their compatibility path. The shared `lib/google_cli.py`
decoder recognizes Hermes' empty Gmail-search sentinel without swallowing authentication errors.

The proxy settles successful standard 3.8 chat reservations to reported input plus the full
model output ceiling at verified 2026 introductory prices. Missing usage, failed/ambiguous
calls and native/grounded calls retain their original reservation. This is conservative
admission accounting, not an invoice; the user’s overall $50 test authorization is unchanged.

Concurrent Gmail attachment fetches each own and close their Google API client. Sharing its httplib2 transport across worker threads caused TLS memory corruption in the live personal Cloud gather. The cohort size, attachment budget and result order are unchanged.

### Managed iMessage presentation and Gmail draft capability

The managed Photon registration uses the shared `chatfmt.to_imessage` renderer for both interactive
replies and standalone receiver deliveries. Startup copies that one library beside the plugin; no
second formatter is maintained. Messages receives plain text, readable web URLs and compact handled
lists, while canonical brief archives keep their complete content. Encoded email links are replaced
with a copy-to-Gmail instruction. The registration also overrides upstream's Markdown capability hint.

`_shared/lib/message_targets.py` owns messaging destination validation for `action_links.py`,
the shared chat formatter and the receiver's scheduled delivery boundary. Phone formatting may be
removed only from a phone-shaped address. Apple ID emails and bare SMS short codes remain intact;
contact IDs, masked numbers, group identities, WhatsApp LIDs and RCS business handles never become
phone numbers. Unsupported addresses produce no link or offered-draft receipt. Delivery removes
invalid links and their empty tap prompts while retaining the draft. The same helper is installed
beside Photon and copied into the receiver image. Address syntax does not establish identity:
the draft skill must resolve the target from the actual thread or contact before using it.

Explicit owner instructions use the same opt-in Mac send capability in Cloud and self-host.
Reading messages does not grant sending: the app reports `allow_send` separately and the managed
receiver checks it before forwarding. HTTP MCP writes also require an independent per-boot chat
credential held only in the receiver and gateway memory. The adapter injects it only for the
loopback Bridge MCP endpoint; shared config and child environments never contain it. Managed
processes are nondumpable so peer jobs cannot read it from `/proc`. Scheduled children can neither
call nor discover `send_message` with their read credential. The local Bridge independently
enforces its opt-in and unattended gate. This separates process authority, not intent: resolving
the exact approved recipient and text remains the chat skill's job.
Each send carries an operation ID, with a private durable Mac receipt binding the ID to the exact
payload. Messages acceptance is not recipient delivery. A crash after dispatch leaves an uncertain
receipt that cannot be automatically replayed, including after a Bridge restart. The same ID cannot
be reused for different content. Unsupported recipients stay as copyable drafts; an uncertain send
never triggers an automatic retry or a second send link.

`google_action.py capabilities` reports the saved OAuth grants without making a network call or
exposing tokens. The draft skill checks this before offering Gmail storage. Managed `gmail-draft`
refuses before contacting Google unless compose/modify access is granted, and a successful save
requires a Google draft ID. Read-only Gmail is still connected; missing draft access does not
misreport it as disconnected. Failed saves preserve the text instead of returning a mailto link.

### Cloud account and device connection


`CloudConnectionView` shares the existing app with self-host and stores the product tier separately from local/remote
agent topology. Local source choices write the existing fail-closed cloud-policy file; the app
reports successful source reads and consent through authenticated `/cloud/consent`. The receiver
rejects new event/wake payloads from disabled sources before staging or triage. Offline Macs retain
established capability; a configured source without a successful read does not open brief readiness.
Per-device relay credentials can now be revoked through the receiver’s control-authenticated API.
This revokes Bridge access, not Google consent, historical data or existing browser sessions.
Historical source-specific purge, dashboard exchange and the iCloud mirror remain deferred.


The shared `SOTTO_REACTIONS` setting also controls Photon processing Tapbacks through `sotto_photon`: a bounded local text match selects 🔎 for research/prep, 📝 for writing, 📅 for calendar requests, 🧠 for memory, or 💭 for general questions. A short standalone thanks keeps ❤️. On completion the adapter sets ✅ for a successful turn, ⚠️ for failure, or ⏸️ for interruption directly on the original message, without an `/unreact` call first. Identical updates are skipped and reactions stay scoped to their triggering message. No model call is added. These are best-effort conversational status indicators, never evidence that a draft was saved or an email sent. The transport sends one replacement operation; notification presentation still belongs to Apple Messages.

`provider_error_compat.py` applies a hash-checked adaptation to the pinned Hermes shared gateway
boundary used by Telegram, WhatsApp, Photon and the other chat adapters. It patches two files in
the Hermes checkout pinned by `adapters/hermes/hermes.commit` — `gateway/run.py` and
`gateway/run_turn_runner.py` — and the two SHA256 constants are those files at that commit.
A provider failure is recognised from the START of the body: an upstream failure marker followed
by a status code, a JSON payload or a per-attempt retry log, whatever the body's total length or
line count. Ordinary assistant prose that merely discusses an HTTP error — including a write-up
that opens with one — carries no such payload behind the marker and remains unchanged.
The final reply distinguishes durable allowance stops (the proxy's own code, and each provider's
own quota/billing wording, which no amount of waiting clears) from temporary rate limits,
authentication, policy and availability failures, and never claims that a send failed or another
retry is running. The lifecycle-status path reports the same plain-language sentence rather than
staying silent, because a failed turn publishes no stream payload on a natively streaming surface;
the sanitizer treats its own copy as ordinary text, so crossing both boundaries repeats one
sentence instead of producing a second, different-sounding failure.
A different Hermes source fails startup so an upgraded classifier must be reviewed before this
compatibility patch is changed or removed. The image build runs the same check in `--check` mode,
which writes nothing, so a pin bump fails the build instead of every container at boot; container
boot and the local Hermes installer then apply the adaptation and report a mismatch as one
`[sotto] FATAL` line naming the expected and found hashes.
The native-stream finalizer applies that sanitizer before sealing its authoritative final payload;
otherwise Photon could publish raw provider JSON before the later non-streaming boundary cleaned it.

The shared Photon adapter delays typing for two seconds, then uses Hermes' turn-owned refresh
loop until completion or interruption. Isolated startup/progress callbacks cannot flash the
indicator. The loop refreshes every two seconds without Photon's upstream five-second throttle,
while retaining Hermes' approval pauses, bounded calls and stop/cancellation cleanup. Quick replies
use only the Tapback and reply. This behavior applies to Cloud and self-host.

The model proxy distinguishes a durable pilot allowance stop (`402`, `sotto_budget_exhausted`) from upstream rate limits (`429`). The managed Hermes adapter gives the allowance stop its own truthful message and coalesces repeated budget notices during a gateway session; a successful ordinary reply re-arms that notice. Failed notification sends remain retryable. The allowance is not reset or increased by this error handling.

A tenant can explicitly set `budget_cents` to null to disable admission budget enforcement. Accounting continues unchanged; authentication, credential expiry and model/route restrictions still apply.

Cloud and self-host expose the same 11 Mac readers. Managed source consent maps Chrome/Safari IDs to their history and search-query payload fields and ties contact totals to Contacts consent; unknown sources remain rejected. That consent is applied when the tool call arrives, not after the Mac has run it: the relay asks `managed.validate_tool_request` before forwarding, and a tool managed mode does not permit (including `send_message` without separate send consent) comes back as a JSON-RPC `-32002` "tool not permitted for this connection" with no forward. The same decision filters `tools/list`, so a denied tool is never advertised to the model, and it is re-applied to the result, which is still inspected field by field (an unscoped `read_local` is admitted at request time precisely because its payload is filtered on the way back). Both gates read one allow decision, and both default to allow in self-host, which forwards every tool as before. Enabling a source changes future reads without resetting conversation memory or learning. The personal comparison pilot enables all 11 readers and the existing proactive-scan and midday-digest schedules. Availability is reported from actual reads; enabling a reader does not claim its database is present.

The personal Cloud comparison uses the established Telegram agent settings: the identical Sotto persona block without the stock Hermes identity preamble, Gemini 3.8 Flash, medium reasoning and 60 agent turns. Its previous canonical people/company/continuity records, explicit preferences and writing-style samples are imported using the existing graph and style writers; newer Cloud records win conflicts. Credentials, outbound queues and raw Telegram chat transcripts are not part of that import. Granola receives a separate Cloud OAuth connection.

Managed persona refresh belongs to `managed_config.reconcile`; the legacy append/refresh path is self-host-only.


#### Source observations and end-to-end checks

Source permission, access and extraction are distinct. The Bridge health response covers all 11
Mac sources; only an actual `read_local` response updates the extraction receipt in the existing
`config/source-state.json`. A healthy access probe cannot erase a failed extraction or refresh an
old read. Access and read receipts retain separate ordering timestamps. Relay-requested observations
also retain the server's request-start time through snapshot replay, so a corrected Mac clock does
not pin availability or let a delayed relayed read replace a newer one. Older unsolicited wake
uploads lack that marker and retain Mac-clock ordering; their ordering across a clock correction
remains ambiguous. Displayed freshness is bounded by server arrival. A disable reported by a probe takes effect on arrival whatever
either clock says; only a strictly later probe restores access, and a completed read never does, so
neither clock skew nor a clock correction can hold a disable back or let a delayed read undo it.
An unreadable or invalid consent receipt fails closed for readers and is left untouched by metadata
writers; the brief still composes with every local source withheld and says so.
Style learning rejects unknown channel labels; channel-less legacy rows follow iMessage consent,
and email aliases follow Gmail consent. A drift test checks the Python receipt vocabulary against
the statuses the Rust Bridge emits.
The Mac serializes status-file writers and keeps capability probes separate from extraction outcomes.
`status.json` also carries `checked_at`, advanced only by a real Bridge access probe after health or
extraction, never by a connection heartbeat. The menu calls the heartbeat Last connected. Full Disk
Access is Ready only when the app grant and Bridge probe agree; after the existing one-time permission
restart it stays Checking until a newer observation arrives. A blocked child gets quit/reopen and
Full Disk Access recovery instructions; partial/degraded reader hints remain visible even with an
app grant. The menu's View activity opens the existing authenticated `/app#record` with no credentials
in the URL. The Record's expandable detail joins delivery to prior triage decisions by explicit
`decision_ids` within the existing bounded ledger response. Missing evidence stays absent; later
verdicts cannot explain earlier sends. This is a read-only projection, with no new store or writer.
Source switches and consent behavior are unchanged; current access failures
and disabled toggles take effect immediately. Schema/query failures report degradation, not an
FDA request that cannot fix them. The authenticated dashboard reports disabled, unverified, empty, partial/degraded,
unavailable and stale states. Its metadata includes only field counts and timestamps; the public
`/health` response does not expose source activity. Freshness uses the same 24-hour bound as local
snapshot reuse. Empty successful reads and disabled sources do not produce dashboard warnings.
A missing optional Chrome or WhatsApp source is `not_present` until it has been available;
subsequent loss is reported. Built-in Mac sources still report first-run access failures.
Failed deferred-unread reads mark their parent messaging source partial without dropping valid data.

The same source-state receipt holds X request status and the last successful request, without
handles, Posts, bookmarks or provider error payloads. X uses optional owner credentials in both
hosting modes, separate from Bridge source switches. Authenticated diagnostics distinguish
unconfigured, unverified, successful and degraded X requests. Account-wide failures stop the
remaining paid requests in that run; source failure does not block an otherwise useful brief.
Protected timeline errors, including HTTP 200 resource-authorization problems, stay per-attendee
in prep and brief context without a connection-wide dashboard alarm. Protected-only runs prove
neither a successful connection nor recovery from a prior connection failure.
Rate limits stop further requests to that endpoint, retaining linked identities and other context.
Current credentials gate staged X inputs at composition and through the existing delivery-effects
permission check. Bookmarks have their own permission ID so losing user access cannot leave
private saved Posts in a brief just because public access still works. No new store or scheduler.

Chrome preserves readable profiles while reporting partial coverage when another fails. Notes
and Screen Time query failures are distinct from empty results. Spotlight command failures do
not prove there are no files; failed last-used metadata means the file's open status is unknown,
including file/meeting matches. One ten-second `mdfind -0 -attr` query returns paths, last-used
dates and download origins from the same indexed records. This avoids a separate `mdls` path
lookup that can fail even when Spotlight finds existing files. NUL record boundaries preserve
multiline names and origin arrays. Malformed metadata marks coverage partial without poisoning
successful peers; an explicit null last-used date alone means unopened.
Partial reads keep valid current fields, including empty lists, without blending cached sibling
fields. All source projections count when deciding whether a snapshot is live. Current consent
is checked again before style or Contacts updates from cached observations are written, including
welcome/setup prewarm; channel-less style samples use the same iMessage consent as ingestion.
First-brief composition fills availability per source, so one disabled source cannot hide another
source's partial coverage.

Browser history, recent-file metadata, screen time, notes and reminders supply current brief
context. They do not add another history backfill or guarantee a nudge for each observation.
Existing compose/extract logic decides what is useful; the ordinary graph and ledger remain the
durable stores. Fixture tests exercise each Mac source through the authenticated Bridge response,
the real brief prompt and the source manifest in both hosting modes, then revoke access and
verify that held input is removed. Rust fixtures cover reader failures; dashboard tests prove
diagnostic metadata requires authentication. These tests run in the existing verification suites.

#### One product core, two deployment modes

Cloud and self-host consume the same `sotto-chief-of-staff/` skill pack, receiver, contracts and Bridge source. The Cloud pilot branch is a staging branch for PR #755, not a permanent release fork. Product fixes belong in shared scripts; transport quirks belong in the channel adapter regardless of hosting. `sotto_photon/` provides shared formatting and Photon behavior, with managed-only tenant guards. `photon_setup.py` installs this adapter for either mode; container startup applies the same idle-stream policy for any Photon delivery channel. Native Google RSVP follows credential availability, not hosting mode. Each instance still owns its own personal data; importing a baseline is not ongoing cross-instance memory synchronization.

`relationship_importance.py` reads dated evidence stored by the existing relationship pulse in `knowledge/relationship_state.json` and explicit VIP preferences. The proactive scanner and People skill consume that same classification. No second model, daemon or relationship database is added. Deployment mode and chat channel do not affect gift eligibility.

Each relationship-pulse calculation builds the people index once and shares that snapshot across email identity and per-person graph lookups. The next calculation rebuilds it, so edits remain visible without repeatedly parsing the entire graph for every contact.


`_shared/references/relevance.md` owns the semantic bar across briefing and nudging. The shared
`relevance.py` loader injects it into the brief's existing native extraction; the critic quotes the
expanded template. The loader also owns assistant provenance: a structured sender-role field on a
source row (`is_bot`, `sender_type` and their variants — honoured when present, though no current
reader emits one) excludes that row before proactive extraction. Every other row, whatever its
handle or transport, uses the shared model judgment's `sender_role` rather than a handle pattern
or a display-name list. Tier 1 and the digest's bounded conversation review call the same
judgment helper through the existing native model route. Digest review happens before the delivery cap and
considers subsequent outgoing answers. No new persistent store or tenant-specific skill copy exists.

PR #755 targets the canonical `main` branch. The release path is: review and merge the pilot work
there, then generate the public self-host distribution with `prepare-public-repo.sh`; deployments
choose the same reviewed source revision. The generator now fails if any skill, reference prompt
or backend implementation differs between source and distribution. Cloud-only auth/provisioning
stays in adapters/control-plane code. Updating a pilot branch does not upgrade existing self-host
instances; those adopt the next published release, with their own credentials and personal state.

## First useful look and continuous context (pilot implementation)

`receiver._background_context_tick` rides the existing minute heartbeat. Once context and the
owner's delivery channel are ready, `onboarding.tick` reserves a first-use run and invokes
`brief_runner.py` with `kind=welcome` in either hosting mode. It gives local voice and identity seeders one shared 20-second allowance
before the normal composer; slower learning continues in the ancillary job. Public attendee research stays on the ordinary prep
schedule; the welcome selection is empty. The composer requests up to three useful findings, a
proposal in observed voice, and one explicit next-step command. It omits routine loop/update
appendices. An existing morning/evening delivery marker identifies an established installation.
The welcome has its own archive/claim; only channel acknowledgement completes onboarding. A
20-minute onboarding reservation, 30-minute retry spacing and three attempts per UTC day bound
first-use admission; the work queue separately leases and retries accepted execution. The
scheduled brief is held while that first look is arriving, without claiming the normal daily slot.

Every 15 minutes the same heartbeat starts the quiet `memory_cycle.py` process, with the same
unattended environment as other receiver work. It returns `NO_NUDGES`. No second agent, scheduler,
provider adapter, or hosting-specific skill pack is introduced. Native Gemini remains the pipeline
provider in both modes. Each cycle reads at most three pages, rotating iMessage/WhatsApp/Gmail.
For self-host, `gemini_transport.background_learning()` uses a request-local context to route only
this cycle through the existing native model proxy and adds the finite-budget requirement. Missing
proxy credentials hold before any source fetch; HTTP 402 preserves the current page cursor and
skips the rest of that cycle, including Dreamer. Foreground requests remain on their configured
direct BYOK routes. The exact owner choice `SOTTO_BACKGROUND_UNMETERED=true` permits direct
unmetered background BYOK. Managed routing and its explicit null-budget pilot policy are unchanged.
Proxy-backed cycles first authenticate to `/v1/capabilities/background-budget`, a content-free
versioned read of the same ledger and supported native model list. Missing support, an unsupported
effective primary model, exhausted finite allowance or invalid credentials holds before source
access. Self-host also requires a finite budget; managed tenants retain their explicit null-budget
option. The self-host request header repeats the finite-budget requirement inside the actual
reservation transaction so concurrent spend cannot race the preflight. Each background extraction
or curation makes one upstream attempt, including with explicit unmetered BYOK. Transient failures
return to the existing cycle's retry clock instead of buying an immediate retry/fallback chain.
Foreground brief resilience is unchanged.
The initial window is 42 days; subsequent frozen windows collect new context. Bridge `read_history`
uses row-id keyset cursors and stable source IDs, separate from live watcher cursors. A blank or
excluded-message page can advance. Gmail uses its actual API page token and the existing token
builder, with no attachments or writes. Failed reads and wholly invalid extractions keep the old checkpoint; partially valid extractions advance with explicit rejection counts.

`context_learning.py` binds durable facts to supplied message references. Discussions, decisions,
commitments and historical asks are not stored; actionable work remains owned by the live ledger. It reviews
up to 160 direct-message records per page (Gmail pages have up to 100), with at most 1,400 text
characters per record. Group history contributes to the existing activity/voice readers where
authorship is known; semantic extraction currently excludes groups. The receipt distinguishes rows
fetched from messages reviewed and records rejected person-result counts without private text. Each person is validated as a whole before the shared graph write; invalid people cannot poison unrelated valid results. Duplicate output subjects are rejected together, and conflicting source-reference records fail before model access. The existing history-state file holds last-extraction and cumulative rejection counts; there is no separate repair store or extra model call. History never enters `events/queue.jsonl` or creates continuity
rows. Current commitments still use the existing live triage/brief/resolve path. Contacts, calendar,
Granola and other sources keep their existing consumers; this is not an all-source history index.

On each work-driven heartbeat, after at most three rotating history turns, `dreamer.py` reviews one
unfinished batch of up to eight people with changed semantic evidence
and 40 active facts per person. Bookkeeping-only timestamp/writer changes do not trigger another
curation call. The reviewed signature is committed only if the post-curation file still matches
the writer's full-file hash; concurrent new evidence remains unreviewed. It can select four existing facts for a summary and flag three
conflicting pairs. `knowledge_update.consolidate` validates IDs and the live file hash under the
graph lock, archives exact duplicates, and retains original evidence. It never fabricates summary
prose or treats curation as a new observation. User-corrected facts do not decay; automatic updates
cannot overwrite them. A repeated source reference cannot increase confidence. Differing historical
assertions stay separate until resolved. Full provenance ranking, typed-slot resolution, procedural
inference and automatic code changes remain future work. The model proxy's configured per-tenant
spending ceiling remains the admission authority for this work; there is no separate daily allowance.
The proxy's fixed reservation is a conservative admission allowance, not an invoice; its existing
SQLite ledger remains the only model-spend ledger.

The cycle freezes one Dreamer candidate batch before its model call and uses that same batch for the
retry fingerprint and graph hash checks. Malformed/unbound responses and native HTTP 400/422 receive
two attempts for an unchanged prompt/schema/model-route revision and semantic evidence fingerprint,
then park until either changes. Availability, network, timeout, HTTP 429 and HTTP 503 failures remain
retriable. The retry metadata lives in `knowledge/history-state.json`; it adds no daily counter or
second Dreamer store.

A rejected nonempty Gmail page token may restart its frozen window once. That reset marker survives
adapter and deployment changes and clears only when the window completes, so a release cannot
repeatedly replay already learned first pages.



`usefulness_feedback.py` binds explicit useful/not-useful ratings to retained delivered briefs
and writes through `log_outcome.py`. Item identity is a hashed positional locator, with a unique
excerpt required when recording an item rating. `personal_context.py` resolves at most 320
characters per item from the authorized archive for the latest eight recent examples. Every
recorded source permission must still be enabled. Missing provenance, a changed/deleted archive
or revoked source suppresses the excerpt. Offered-draft archives currently lack source provenance,
so their text is excluded from this reuse. The bounded listing and recording use the same reader. Explicit rules win; ratings never change mutes,
approval gates or loop state. Silence and legacy draft-dismissal receipts are not negative ratings.

The first-day native-model replay exercises an empty memory directory and frozen invented history,
including author-verified voice seeding and different completed/pending invitations. Shared
relevance requires an evidenced link before joining conversations; matching times alone do not
identify the same event. `style_apply` accepts a fallback context bucket only when there is no
known recipient context, so the welcome can quote both work and personal writing samples.

### Persistence, recovery and shared release checks

`.sotto-volume.json` binds a managed mount to its tenant; `.sotto-runtime.lock` excludes another live
instance and an offline backup at the same time. `config/model-lease.json` contains expiry and
content-free renewal status. The existing proxy SQLite holds renewal leases, and the existing
pairing SQLite holds only per-device bearer hashes and revocation generations. These extend
existing responsibilities; they add no services or competing canonical memory stores.

The [recovery runbook](../adapters/hermes/RECOVERY.md) describes verified full-volume export and
restore to an empty replacement. Restore checks hashes and SQLite integrity and holds startup
until explicit review/resume. Its synthetic cold-restore test preserves explicit memory, credentials,
queued work and pending delivery. It does not establish an automated backup schedule or recovery
of the separate proxy control state; those remain operator launch checks.

`sotto-chief-of-staff/tools/verify.py` is the common credential-free backend/release gate used by
local shipping, private CI and generated public CI. It includes proxy tests in
addition to the shared pipeline, receiver, adapters, lint and available publication guards.
The proxy and instance base images use the same pinned Python image digest. Managed boot
compares installed Sotto skills/bundle against the shipped artifact; `user-*` routines remain
outside system reconciliation. Receiver-based Cloud and self-host share procedures; standalone
host adapters are supported separately and do not inherit the receiver’s durability guarantee.

Calendar and post-meeting synthetic dispatch acknowledge the `job_id` already committed by triage, wake the durable worker, and stop. They never also stage and launch the legacy path for that same decision.

## Comparison follow-through (September 9)

The Hermes adapter installs `web_provider.py` as the `sotto-web` plugin and reconciles both web
backends in `web_config.py`. It translates the pinned Hermes search/extract protocol into the
existing `web_research.research` / `fetch_url` calls. Provider selection, credentials, native
Gemini transport and fallback stay in the shared module. Hermes keyless rescue/fallback is disabled
for this adapter so a failed shared lookup cannot silently route to another provider. A synthesized answer is separate from
its citation list; it is never attributed verbatim to one page. Failures return an unavailable
result, without exposing raw provider errors. Both boot and the self-host installer use this path.

`calendar_context.meeting_events` is applied by the Google gather, composer, receiver calendar
cache/diff and proactive prep scanner. It joins only explicitly labelled context notes to a unique
non-context event with matching normalized subject and exact timezone-aware start/end. An unrelated
overlap, ambiguous match or separately timed prep remains separate. Notes and their event IDs stay
attached as evidence; no calendar event is edited or deleted.

History extraction uses a constant native Gemini response shape with bounded inner evidence
arrays. Page-sized outer array limits and dynamic subject/reference enums caused native HTTP 400
rejections; those constraints are enforced by the existing writer instead. Cross-person citations and
repeated subjects are still rejected before any writes. Failed pages keep their cursor and frozen
window; the receipt records a safe stage and validation code, never the raw response or email text.
Successful retries clear those diagnostics. Native HTTP 400/422 extraction failures latch a
request revision (schema, prompt, native client implementation and model route) in the checkpoint instead of repeating the same
rejected request. Changing that contract releases the latch; transient transport failures keep the
existing hourly retry. A missing or unchanged source cursor is rejected before semantic extraction,
so the bounded retry does not spend a model call. An unchanged malformed page or nonadvancing cursor
gets two attempts, then one compatibility probe every 24 hours; a repaired Bridge therefore resumes
without manual checkpoint edits. Availability, network, timeout, HTTP 429 and HTTP 503 failures keep
their ordinary retry path. No page is skipped and no error body or URL is stored.
For Gmail only, the adapter — not the cycle — decides that a continuation token is dead. A
page-token request the provider rejects as an invalid argument raises
`source_context.HistoryContinuationError`, carrying a code and never the provider's message;
every other deterministic Gmail rejection, including the adapter's own
`HistoryProtocolError('invalid_history_window')`, parks in the ordinary protocol latch with no
replay. Two such token rejections clear the cursor and restart the same frozen window from page
one. `window_page_base` records the lifetime page count when a new window starts.
`continuation_reset` records the lifetime page count at the reset and the depth that attempt
reached within this window. A later restart requires deeper progress within the same window;
pages from completed windows cannot inflate that threshold. Older checkpoints with no baseline
may restart once with unknown depth, which cannot earn a further reset. Older reset markers
without both progress counts remain consumed. No migration resets a cursor or grants another
replay by itself. A window whose restart budget is spent records
`continuation_restart_exhausted`, and the blocked history receipt carries that code in a new
`reason` field so a stuck window is visible without reading the state file.

The composer normalizes quiet-day boilerplate to time-neutral wording and removes redundant
calendar-preview instructions. Coming Up keeps its five schedule-line cap. The critic,
recipient guards, continuity writers and delivery markers retain their existing responsibilities.


`source_catalog.py` is the shared Bridge ID/payload map used by source readers and the receiver's
consent gates; the image copies that same leaf beside the receiver. Contacts alone do not establish
context for a personal brief. The device- or control-authenticated `/cloud/status` exposes only existing
messaging activation, context readiness and onboarding delivery phase, with no private content.

The Mac first-run flow lives in `SetupWizard.swift`: Welcome → Google → Mac sources / Full Disk
Access → Messages → First brief. `AppConfig` persists hosting choice, stage, source confirmation
and completion independently of host/token presence. Legacy configured installs retain
settings/status and source choices. One reusable Sotto window serves first run, settings and
`sotto-bridge://cloud`; it no longer opens a second Cloud window. Every supported Mac source starts
on for new installs. The shared grouped source list is visible before the first read, and the same
Full Disk Access card serves both hosting modes. Google pairing alone cannot launch the Bridge or
report Mac consent: startup, reconnect and setting changes share `mayCollectMacSources`. Continue
on the access step confirms the selected sources; explicit skip disables all Mac readers. Self-host
uses Connect → Choose sources → Disk access, with the same defaults and confirmation gate.
The pilot Mac Messages step reads its existing owner activation receipt; shared-number confirmation
in the Mac and transport activation remain later integration gates.


## Optional card presentation

`_shared/lib/visual_brief.py` owns the common brief/prep templates and licensed local fonts. `visual_delivery.py` selects presentation at the receiver seam and supplies fixed fallback reason codes to the existing delivery receipt; the Hermes gallery adapter sends one multipart message through the existing outbox. Temporary PNGs/manifests use `cache/visual-briefs/` and the seven-day staged-artifact retention rule. [Visual briefs](VISUAL-BRIEFS.md) documents the opt-in and test gates.

The shared writing policy lives at `sotto-chief-of-staff/_shared/references/writing-style.md`. Existing provider request builders attach it to system instructions, and Hermes installation/startup appends that same file to the persona. Cloud and self-host share this policy; no channel-specific prose policy or new persistent store is introduced.

Focused iMessage meeting-prep replies use that same renderer at the Photon final-send boundary. A private acceptance receipt alongside the rendered manifest prevents duplicate sends across Hermes retries/restarts; Hermes retains the response obligation. Morning/evening scheduled delivery continues through the receiver outbox.


### Shared audit corrections (pending release)

Calendar changes from one refresh enter triage as one batch; the comparison checkpoint preserves
individual handled IDs and retries omissions. Each candidate's first-observed timestamp is saved
before dispatch, keeping its work identity stable after a timeout or checkpoint failure.
Before retrying, calendar changes consult the same work ownership and queue/drop receipts as
meeting taps; recovered changes are acknowledged without repeating triage. An unknown receipt
lookup pauses the batch. A crash between the queue append and its receipt remains a replay window.
A meeting tap reserves its daily slot before dispatch. Pending reservations count toward the
existing cap, survive write failure and restart,
and reconcile through existing work ownership or completed queue/drop receipts without another
triage. Unreadable checkpoints or unknown ownership pause admission instead of resetting the
allowance. Unresolved pending taps that leave the lookback keep their slot until local midnight;
uncertainty must not restore a possibly spent allowance.

Ancillary learning extracts observed style before grading drafts, so a newly observed verbatim
send can confirm its sample in the same pass. Older executed outcomes retry missing confirmation
within their original matching window without appending duplicate outcome rows. Hash-only action
markers in `style.json` make that confirmation one-shot even after the capped prompt sample rotates
out. Extraction prunes markers against the locked retained draft ledger, reusing its existing
age and size limits. An unreadable ledger never counts as evidence that a marker expired.

An exhausted background-memory bucket is consumed until the next bucket; temporary admission
failures still retry. Atomic receiver writes use unique same-directory temporary files per
operation. Diagnostic append/rotation, retention truncation and explicit log erasure coordinate
through the same sidecar lock, preserving bounded storage and the owner's erase operation.
Diagnostic lock acquisition has the same ten-second bound as the other shared file locks;
best-effort diagnostics swallow a timeout instead of hanging a brief.

## Part B: proof and personal attention in the existing loop

`_shared/scripts/release_one_proof.py` is a read-only operator report over retained files. It owns
no persistence and makes no model calls. `learn_step.py` stores exact resolver proposal outcomes
inside the existing `briefs/<date>.<kind>.learned.json` receipt. Unsupported historical correlations
remain unavailable. `compose_brief.py` reads consented history checkpoints for a plain health line.

`relationship_pulse.py` remains the relationship evidence writer. Shared
`relationship_importance.py` derives separate owner/counterpart reply signals from bounded,
deduplicated, completed same-channel conversation turns. Existing `history[canonical_id].engagement`
rows retain the samples. Research depth never enters ranking. Learned weights reuse the graph's
decay clock; stated authority is separate. The existing Granola ancillary Learn step passes its already gathered, admitted one-to-one meetings
through `relationship_pulse.observe_history` after canonical graph writes. It preserves the current
attention queue and makes no extra gather. Meetings count only when their end is observed and exactly one counterpart remains after excluding the owner,
and strengthen existing reciprocal evidence rather than establishing importance by themselves.

The deterministic brief artifact carries `_represented_loops` with exact IDs and versions.
`brief_runner.py` carries stable obligation identity (never wording or the evidence list) through
essential learning and stages it, with the current version, as non-gating `loop_surfaced`
bookkeeping. Finalize checks the same identity: a restatement in between still counts, a closed
row or a different debt is skipped; it never invalidates delivery. A narrative line counts when it
names the person, carries the model's `<!--loop:ID-->` marker and shows all of the ask's content
words; an unmarked line must quote the ledger summary verbatim. Free paraphrases may undercount
rather than credit a different obligation with shared action words. Accepted outbox effects call the continuity writer to update the
existing loop's `delivery_surface` provenance. Stable run IDs make replay idempotent. The count
means transport acceptance, not read receipt. Captures, failed sends, aggregate mentions and legacy
counters cannot increase it. Interactive skill delivery without a receiver acknowledgment remains
uncounted; it cannot masquerade as an accepted brief.

`review_candidates.py` reads the same relationship state and current explicit preferences. It
selects one existing command for Friday's explicit appended-question allowance, shared with
standing-rule confirmation. Quoted questions in source prose do not spend that allowance. Candidate delivery stamps
are written under the same JSON lock in `relationship_state.json.review_candidate_offers` by the
accepted-delivery finalizer. They do not grant authority. The user-confirmed `master_file.py
prioritize` command adds one line atomically, preserving the existing set; VIP uses `preferences.py`.
No other preference writer, persistent store, scheduler, model call or tuning environment is added.

### Notification evidence and document targets

The direct notification composer reuses `personal_context.current_conversation`, the active
ledger view (including group IDs) and its existing Google Calendar read before its bounded
writer decides whether an old ask still needs attention. `delivery_effects` compares exact source
message references against terminal ledger rows at composition and send time. It never joins
obligations by person or meeting title. Calendar context informs relevance, not automatic closure.

`calendar_context.work_attendees` owns the work-address policy used by automatic research and prep.
The managed Photon adapter clarifies a bare unbound download request after owner authorization and before dispatching model work. The
DocSend reader validates complete URLs, accepts branded subdomains, and retains that host through
the gate and page reads. Its cloud session does not inherit Mac browser verification. No new store,
scheduler, model call or environment variable is introduced.

### Meeting and photo readback

The existing Ask skill invokes two read-only projections. `_shared/scripts/meeting_context.py`
combines fresh Granola notes with canonical continuity rows by occurrence; the prep composer uses
the same obligation reader for resolved and outstanding context. It does not extract, write or
resolve commitments. `_shared/scripts/brief_detail.py` reads immutable visual manifests plus the
existing outbox acceptance receipts or interactive gallery receipts. Acceptance keeps an opaque
artifact ID and recipient hash after payload deletion. Render records the permission fingerprint
and current owner-channel hash, so a default-channel receipt cannot follow a change of owner.
No additional store or daemon is introduced. Existing cache retention and forget still own deletion.
Learn preserves content-free extraction failures; the proof reader adds completed-local-day coverage.
