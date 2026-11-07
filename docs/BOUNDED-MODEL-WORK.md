# Bounded background model work

The execution contract is per admitted operation, never a daily dollar limit. Cheap idle scans, real
incoming work, and user-directed chat keep their existing schedules and admission rules. Existing
tenant monetization/allowance code is separate and unchanged.

## Notification execution

`procedure_runner.py` gathers and admits proactive candidates, then calls
`_shared/scripts/compose_notification.py` directly. The receiver uses that same composer for event
bundles. These paths have no chat-engine turn, tool-discovery loop, delegate, or model fallback
ladder. Quiet/empty scans make no composition calls. Intentions and handoff
questions use deterministic text. A meeting reminder's identity, countdown and offer are
deterministic; one or two sentences (60 words / 500 characters) of grounded context come from the
same writer only when stored memory, the invitation, conversation text, cached attendee mail or
identifier-joined open items supply evidence. Once the job's batch writer has run or failed, the
reminder keeps its basic text rather than opening a second request. Remaining admitted copy is one structured request; there are at
most two attempts per admitted job and evidence/model/policy revision, including worker retries
(plus the bounded interruption recovery below). Transient provider errors wait for the existing
queue backoff; only malformed copy gets an immediate repair attempt. Validated copy is saved before
delivery and reused under a file lock. Empty selections are not cached; later admitted ticks may
reconsider them. The writer sees projected human evidence without ledger control fields or action
identifiers. Echoed identifiers, internal terminology and unresolved slot markers are rejected
before caching. Each push selects one primary item, ordered by urgency and deadline; other
candidates retain coverage. Optional person-fact read failures do not discard a candidate. A writer
failure cannot discard a deterministic reminder. Source text cannot select a recipient, create an
executable action, or introduce candidate IDs. The writer has a 48,000-character total context limit
(including instructions and schema), a 4,096-token output ceiling and low thinking on known Gemini 3
models. Oversized or invalid work stays undelivered for existing retry/brief coverage.

Context reads are fixed: live open items, matching person facts, writing style, and calendar only
when required. The Google gatherer reads the selected readable calendars and reports partial failures.
A selected subset or failed inventory cannot establish all-calendar availability: scheduling stays a grounded question with no acceptance draft.
The slot calculator requires explicit all-calendar coverage before offering times, plus an explicit
day and duration within the covered interval. Unknown constraints remain an open question.
Calendar-dependent text retains the existing permission and freshness checks. Post-meeting events
select one matching meeting by title/start and invoke the existing follow-up composer without its
ledger-apply CLI, presenting one grounded recipient draft. Missing/ambiguous notes do not produce
invented discussion; existing open items can still surface.

Recipients and links come from admitted source identities. Group messages never become links to one
participant. Email drafts are offered for review, with recipient/body/thread in the pending offer;
they are not sent. Existing action-link and approval helpers retain their authorization rules.
Offers activate only after acknowledged delivery, and omitted candidates cannot earn
intention/chase/handoff receipts. Existing source permission and freshness checks remain at send.

Selected notification copy (text plus draft and decline) is limited to **1,200 characters**.

## Attempt ownership and recovery

`_shared/lib/model_work.py` claims attempts in `events/model-work.sqlite3` before dispatch.
Operation identity hashes evidence, task policy, model and tenant. Procedures additionally include
the stable receiver job ID, so queue retries share a budget while a later scheduled job can
legitimately run again. Standalone procedure invocations get their own occurrence. Native client and
route revisions release work after a repair. HTTP 400/413/422 parks the same request contract across job
occurrences, including when another fallback shape has since run. Changed request/evidence/policy
can release it.

Dead-worker claims, explicit process interruption, or expired claim leases allow at most two
replacement requests. Original requests remain `interrupted_unknown`: they may have been billed, and
their usage is never refunded or represented as zero. Healthy in-flight claims are not refunded.
After those recoveries are spent, work remains held for inspection or changed evidence. Exhaustion
never advances a source cursor or counts as successful learning. Partial `MAX_TOKENS` output is
rejected. A hold during the brief critic/revise stage delivers the draft and records the gate as
held, never as passed: a quality stage may not cost the user the day's brief.

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

The `brief` row is the parent envelope: extraction owns six requests (two semantic extraction
attempts, each with the existing three-request provider ladder), critic owns three and revision owns
three. These stages cannot consume each other's allowance. Follow-up allows three calls so its
primary, one transient retry and same-family fallback ladder is reachable; the optional evening
follow-up uses that separate child allowance. The full source context, structured output, critic and
closure-review checks remain unchanged; low thinking is a latency preference, not a hard token or
time limit. OpenAI-compatible and Anthropic output parameters retain their pre-change behavior;
these native Gemini ceilings do not silently alter those providers. Meeting prep, grounded web
search/fetch, and deck reading now use the same durable operation ledger. Each invocation retains
its existing model and thinking behavior; successful grounded research still persists facts through
its existing path. A retry is a new attempt under the same operation identity, while a fresh
delivery run receives a fresh identity. Repeated identical URL/search work in one run reuses its
complete in-memory result within the same data root; failures are never cached. A fixed set of
process locks makes concurrent identical requests share that result instead of paying twice.
Low-thinking overrides are limited to tested request contracts; a priced model is not automatically
considered compatible.

Research precomputes its profile/recency/focus batch plan before starting the existing five-worker
pool. Each batch allows one Parallel attempt, one Exa attempt and Gemini's two request shapes, when
those providers are configured. Completed batch artifacts coalesce identical concurrent
requests/retries; existing person-profile freshness and explicit focus behavior are unchanged. The
relationship pulse is deterministic and does not need a model call. Memory's existing cursors,
changed-fingerprint checks and three-page cycle limit remain the owners of incremental work.

Attempt receipts contain opaque IDs, request-shape hashes, component sizes and normalized usage, not
prompts. Model-work receipts retain **90 days**. An unknown worker claim has a **900-second** lease,
with at most **2** interruption replacements per operation. Artifact families use **256** lock
shards. Notification context is capped at **48,000 characters**, including instructions and schema.
The database prunes on open at most once an hour per process. Validated notification/triage/research
artifacts contain private output, live in the tenant volume with mode 0600, expire through the
existing seven-day retention sweep, and are removed by `forget.py --caches`. There are at most 256
content-free lock files per lock kind; research artifacts have five kinds plus one DocSend lock, at
most 1,281 in all.

## Accounting and reports

`usage_accounting.py` is canonical in the shared library; `tools/sync-usage.py` packages an
identical copy into the independently built model proxy. Verification rejects drift. Native Gemini
output is candidates plus thoughts. Google's OpenAI-compatible `completion_tokens` excludes thinking;
the proxy uses a validated `total_tokens - prompt_tokens` receipt for future text-chat settlement and
keeps older rows or incomplete receipts held. The provider-specific normalizer preserves these
reported counts without changing the generic adapter's assumptions.
Cached input is priced separately. Chat responses missing cache details retain an unknown exact cost
but also show token-cost lower/upper bounds (all input cached versus uncached); never label the
upper bound an invoice amount. Native blocked responses with a usable total retain their billed
usage. Missing usage, unknown prices and expired introductory prices produce unknown cost, never
zero. Estimates exclude paid search/image/audio categories and other provider fees. The proxy's
allowance is not used as an invoice estimate.

For an explicitly authorized temporary cap increase, record the original `budget_cents`,
`budget_period`, `budget_started_at`, and cumulative allowance used in that period before changing
the cap. Restore the original configured cap afterward without deleting call rows or changing the
period start. Usage incurred under the temporary cap remains counted: if cumulative usage plus
the next 200-cent reservation exceeds the restored cap, admission remains exhausted. Report that
state accurately; restoring a cap does not restore spendable allowance. A lifetime budget does
not reset by waiting; a calendar-month budget follows its existing UTC month boundary. Any further
increase needs its own explicit authorization. These are configuration steps for the authorized
operator, not a reason to reset accounting or treat held reservations as proven provider charges.

Proxy metadata records application and caller deployment from trusted tenant configuration (optional
`application` and `deployment` fields), the separate proxy deployment, authenticated tenant/owner,
workload, opaque operation/parent/run IDs, attempt number and context component sizes. Internal
attribution headers are consumed by the proxy and never forwarded to Google. Call rows remain the
authoritative billable attempt for proxy traffic; the local attempt journal is a join/diagnostic
source, not an additional bill.

Run on the model-proxy service or an authorized local copy of its SQLite ledger:

```sh
python cloud/model-proxy/report.py /path/to/ledger.sqlite3 --days 7 --timezone America/Los_Angeles
```

The read-only report groups daily calls by application/caller deployment/owner/proxy
deployment/workload/model/route. The default day boundary is UTC; pass the billing timezone
explicitly. It includes distinct operations, attempts per operation, median/p95 input and component
sizes, cache fraction, output/thinking totals, known estimated costs, failures, and explicit unknown
counts. An optional `--billed-total` shows the residual to known token costs; use an identical
billing interval/currency/ project scope and allow provider lag. Today is explicitly partial. No
scheduled report or AI call is introduced.

Interactive requests without an adapter-provided per-turn ID have no proven turn identity; session
totals are not user-turn counts. The managed gateway now labels requests to its exact proxy chat endpoint as
`chat`, adds an opaque request ID, and hashes the pinned transport's session ID when available.
Compression and other auxiliary calls can share the chat lane; a request ID is not a user-turn ID.
Legacy rows are not retroactively attributed. Usefulness/outcome joins and cheaper-model quality
evaluation still require post-rollout evidence. Do not claim account-wide invoice reconciliation
or an exact savings percentage from these token estimates.

## Shared provider admission and managed chat context

The proxy checks admission atomically in its existing SQLite ledger for both the tenant and the
shared upstream-key pool. In a rolling minute it allows at most 60 requests and 12 million input
characters; background work is limited to 48 requests and 6 million characters. At most eight
calls may be active, with five available to background work. Due brief stages, meeting prep and
chat receive the foreground reserve. Unlabelled native requests use the background lane. One tenant
may use at most three quarters of each shared limit (45 requests, six active calls, and nine
million input characters; background input is capped at 4.5 million). This includes the first
request, so one tenant cannot consume the entire pool. Multiple tenants can still fill it. A single
request larger than its tenant lane's input limit is refused
with HTTP 413, since waiting cannot admit it. These are local backpressure limits, not claims about
a provider quota or exact token counts.

Workload headers are scheduling hints from the authenticated runtime, not an authorization
boundary. A modified runtime can label work as foreground, but cannot change its authenticated
tenant, allowance, or tenant-wide limits. Header labels do not grant permission to read sources
or send messages.

A temporary capacity refusal never reaches the provider and consumes no model-attempt allowance.
It returns a typed HTTP 429 response and jittered Retry-After; native callers leave recovery to their
existing durable work owner instead of immediately trying another model. Original job deadlines
and source cursors still apply; jobs admitted without a deadline get a one-hour maximum age for
this deferral. Triage refused before judgment remains explicitly unreviewed and qualifies for digest
review even as a single item. Scheduled attendee research also defers on typed capacity refusal;
successful research batches are reused. If prep has not finished when a brief is due, composition
uses available context and explicitly marks attendee research pending instead of reporting an
empty research pass as complete. A bounded digest review cannot advance its coverage timestamp past
unreviewed overflow; the queue and accepted-item IDs retain the remainder. Active slots release after completion or client failure, with a
six-minute lease for crash recovery. Startup preserves active leases because another process may
still own an in-flight call. An ambiguous provider call keeps its metering reservation.

Chat history is the Node runtime's Mastra conversation memory (`runtime/sotto-chat/src/agent/runtime.ts`):
each turn sees the last 20 messages (`lastMessages: 20`, no token limiter), with semantic recall,
working memory and observational memory off, and completed tool traces are reduced to their text
before they are stored. No summarizer runs over the history; the receiver rotates sessions nightly
and `/new` rotates on request. This bounds repeated tool payloads, but it is not a hard
request-token ceiling: instructions, skills and the current turn's tool results also count.

## Deployment ownership and rollout

Keep a private inventory with one record per application/deployment:

```json
[{"billing_project":"project-id","application":"sotto","deployment":"pilot-service-id",
  "owner":"tenant-id","key_identity":"key-resource-id-not-secret","active":true,
  "measured":true,"scheduled_workloads":["brief","proactive","memory","digest"]}]
```

The report's `owner` column is the proxy tenant id, which is the inventory's `owner`; `application`
and `deployment` are the labels the tenant config declares, so the two join on those three
columns. Before release, refresh this inventory from actual provider service status and run `python
tools/check-workload-owners.py /private/path/caller-inventory.json`. It is read-only and rejects
duplicate active scheduler ownership or incomplete caller identity. An intentional move requires
explicitly retiring the old owner before starting the replacement. Existing cron reconciliation and
queue fences remain in force; this is not another distributed lease system.

Always specify the intended service explicitly. Keep retired instances stopped and disconnected from
their deployment source. Never infer the deploy target from a CLI's remembered link. Other
applications retain their own caller inventory; they are not forced through Sotto.

After merge and an intentional rollout, compare a complete day, then seven complete days by admitted
bundles, changed memory pages and delivered briefs. Check timely delivery, unresolved choices, draft
approval and memory backlog alongside input, attempts and cost. Review billing residuals after
settlement. Cheaper routing needs quality evidence before a separate release; evaluate the managed
compression changes against retained source context after rollout. Low-activity days should be cheap
without cutting off useful busy days.

## Provider-blocked history pages

An explicit `PROHIBITED_CONTENT`, `SAFETY`, or `BLOCKLIST` response excludes that
history page from model learning. No alternate prompt, model, or safety setting retries its
contents. Traversal advances to the validated next cursor, while `reviewed` and `last_success`
do not advance for the excluded page. `history-state.json` retains a metadata-only
`excluded_pages` receipt (window, cursors, row count, time and fixed reason), never message bodies.
The brief distinguishes current message access from paused learning and discloses historical gaps.

A page parked by the earlier retry policy with one of those explicit reasons is migrated using
its frozen window and cursor: an ordinary source read obtains the continuation, without a model
resubmission. Source availability backoff and permission checks still apply. An invalid continuation
is held for repair, not treated as permission to skip a different page. Other malformed, empty,
truncated or failed model responses retain the existing bounded retry policy; they are not exclusions.
