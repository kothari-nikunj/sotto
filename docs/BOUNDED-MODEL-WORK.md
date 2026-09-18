# Bounded background model work

The execution contract is per admitted operation, never a daily dollar limit. Cheap idle scans, real
incoming work, and user-directed chat keep their existing schedules and admission rules. Existing
tenant monetization/allowance code is separate and unchanged.

## Notification execution

`procedure_runner.py` gathers and admits proactive candidates, then calls
`_shared/scripts/compose_notification.py` directly. The receiver uses that same composer for event
bundles. These paths have no Hermes process, tool-discovery loop, delegate, or model fallback
ladder. Quiet/empty scans make no composition calls. Intentions, meeting reminders and handoff
questions use deterministic text. Remaining admitted copy is one structured request; there are at
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
when required. The current Google gatherer reads only the primary calendar. Even a complete primary
read cannot establish availability: scheduling stays a grounded question with no acceptance draft.
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
route revisions release work after a repair. HTTP 400/422 parks the same request contract across job
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
| `brief` | 12 | 65,536 | unchanged |
| `brief_extract` | 6 | 65,536 | unchanged |
| `brief_critic` | 3 | 65,536 | unchanged |
| `brief_revise` | 3 | 65,536 | unchanged |
| `followup` | 2 | 8,192 | low |
| `research` | 4 | 8,192 | low |

The `brief` row is the parent envelope: extraction owns six requests (two semantic extraction
attempts, each with the existing three-request provider ladder), critic owns three and revision owns
three. These stages cannot consume each other's allowance. The optional evening follow-up has its
separate two-request child allowance. Core brief output retains the proxy's previous 65,536-token
ceiling and provider-default thinking. OpenAI-compatible and Anthropic output parameters retain
their pre-change behavior; these native Gemini ceilings do not silently alter those providers.
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
The database prunes when opened. Validated notification/triage/research artifacts contain private
output, live in the tenant volume with mode 0600, expire through the existing seven-day retention
sweep, and are removed by `forget.py --caches`. There are at most 256 content-free lock files per
artifact family.

## Accounting and reports

`usage_accounting.py` is canonical in the shared library; `tools/sync-usage.py` packages an
identical copy into the independently built model proxy. Verification rejects drift. Native Gemini
output is candidates plus thoughts; compatible-chat completion totals already include reasoning.
Cached input is priced separately. Chat responses missing cache details retain an unknown exact cost
but also show token-cost lower/upper bounds (all input cached versus uncached); never label the
upper bound an invoice amount. Native blocked responses with a usable total retain their billed
usage. Missing usage, unknown prices and expired introductory prices produce unknown cost, never
zero. Estimates exclude paid search/image/audio categories and other provider fees. The proxy's
allowance is not used as an invoice estimate.

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

Interactive requests without an adapter-provided per-turn ID remain `unknown`; session totals are
not user-turn counts. Their context components are measured, but this change does not modify Hermes
turn limits, compression, conversation history, or auxiliary routing. Per-turn hooks,
usefulness/outcome joins, trend diagnostics and cheaper-model quality evaluation remain follow-up
work requiring pinned-runtime and post-rollout evidence. Do not claim account-wide invoice
reconciliation or an exact savings percentage from these token estimates.

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
settlement. Cheaper routing and interactive context changes need quality evidence before a separate
release; low-activity days should be cheap without cutting off useful busy days.
