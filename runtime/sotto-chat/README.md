# Sotto Chat Runtime

Each active model turn combines owner cancellation with a 300-second abort deadline, longer than
the 190-second receiver deadline of an attended DocSend read. The signal
reaches authority reads, media, model execution and tools. Chat remains serialized until Mastra
settles; a provider that ignores cancellation can still delay that queue. Both Agent constructors
explicitly set `maxRetries: 0`, also the pinned version's default, to prevent automatic model retries.

The bearer-protected `chatTiming` snapshot optionally reports `queueMs` for the runtime's
serialization wait, outside the existing active-turn `totalMs`, and `promptPreparationMs` for
native media interpretation and quoted/background context checks before generation, inside
`totalMs`. `toolSetupMs` overlaps `generationMs`, so those durations must not be added together.
The two optional durations saturate at 300,000 ms. Node health and the Python adapter accept legacy
six-field snapshots and validate every supplied duration as an integer from 0 through 300,000 ms; an invalid field
or unknown key withholds the snapshot. These are runtime timings, not full intake-to-delivery
latency: conversation queueing, provider-history reads, media hydration, accepted-history projection
and reply delivery remain outside them. All existing source-authority checks remain in place.

Every owner action kind in `src/delayed-outcome-policy.ts` (18, from calendar, Contacts and Gmail
changes to brief, reminder, task and skill requests) that ends uncertain gets a fixed honest notice
and is never reapplied automatically; see [Uncertain owner requests](#uncertain-owner-requests).
Acknowledgement exceptions emit a fixed content-free log line. Explicit calendar/contact refusals
before provider contact are terminal rejections; uncertainty after contact remains held.

Managed budget errors carry only a bounded period enum into owner-facing text: monthly tenants
are told about the next UTC-month reset, lifetime tenants about operator action, and older proxies
get generic guidance. Provider error prose is never echoed. Calendar, Contacts, Gmail and Telegram
proof checks trim and lowercase the runtime selector consistently with the receiver.

This package is Sotto's only conversation runtime. The fresh managed Photon, self-host Photon and self-host Telegram profiles connect the root supervisor, Node runtime and Python receiver ([boot sequence](../../docs/ARCHITECTURE.md#boot)). Their startup, chat, five procedures and restart/crash paths are locally verified with synthetic external services; they have not been live verified.

Each [fixed-five profile](../../adapters/sotto/README.md#fixed-five-procedure-profiles)
can compose this runtime with the actual Python receiver for a trusted fresh installation.
It supports five packaged receiver schedules and closed internal procedures. Fresh daily/weekly
Photon routines passed the packaged managed lifecycle at candidate `9d3646d0`.
Fresh Telegram routines passed their packaged lifecycle at candidate `6fb5f7f9`, including
one delivery, unchanged restart replay and cancellation. This does not mark the newer HEAD
as managed-package verified. General executors
and migration from existing native delivery history remain excluded. Same-volume
restart and crash recovery of this profile are implemented and locally verified. The standalone
image starts Node only; `Dockerfile.managed` adds the root Tini/bootstrap entrypoint for managed
provisioning.


The profile keeps UID10000 Node config/state/media in private directories inside the same tenant
backup root as UID10001 receiver state. Loaded Node identity is checked before receiver workers
start; first-contact activation ACK is a later receiver-owned step. The durable handoff hold and
restore hold are not removed by a successful cold-start check.

For review, follow [managed_bootstrap.py](../../adapters/sotto/managed_bootstrap.py) →
[root_supervisor.py](../../adapters/sotto/root_supervisor.py) →
[Node bootstrap](src/bootstrap/main.ts), which mounts the Mastra agent and one Chat SDK adapter.
[application.ts](src/application.ts) and [conversation.ts](src/conversation.ts) coordinate owner
work in the [operational journal](src/journal/index.ts). The Python
[receiver](../trigger-receiver/receiver.py) owns scheduling and the
[outbox](../trigger-receiver/outbox.py); [Bridge tools](src/agent/bridge.ts) and
[source authority](src/agent/source-authority.ts) enforce current source grants.


## Internal transport and diagnostics

Owner mutation clients share identity/options in `src/tools/owner-contract.ts` and bounded loopback HTTP in `owner-tool-transport.ts`. Each client still owns its domain schema, receipt matching, limits, deadline and error wording. Only explicitly selected error statuses have their bounded bodies inspected; legacy failures remain immediate. A pre-aborted request never invokes fetch. This does not establish that the previous implementation made network contact with an aborted signal.

`src/agent/tool-diagnostics.ts` owns the existing bounded content-free collector and health projection, separate from model orchestration. `src/tools/google-read-contract.json` owns the finite Google stage/phase vocabulary consumed by Node and the Python adapter/receiver; it is compiled into `dist/tools` with the existing JSON policy assets. Permissions and canonical writes remain receiver-owned.

The intentionally failed-provider-stop bootstrap fixture runs in a child process. Production poller ownership remains held until provider stop is proven; tests no longer require that scenario to be last in a shared process.

## Local development

Requires Node.js `>=22.18 <27`; `node:sqlite` is built into that runtime. From this directory:

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm run test
npm run typecheck
npm run build
```

`npm run check` runs all three verification commands. Tests use local fixtures and temporary databases; they do not connect to chat providers, models, or hosted services.

## Experimental local startup

For standalone Node development, create a private JSON config file (`chmod 600 runtime.json`) with the exact owner, channel, destination, model proxy, and credentials. Credentials live only in that file; startup does not read Keychain or channel/model credential environment variables. Relative state, media, and skill paths resolve beside the config file.

```json
{
  "version": 1,
  "channel": "telegram",
  "stateDirectory": "./state",
  "mediaRoot": "./media",
  "bearerToken": "replace-with-a-private-token-at-least-32-characters",
  "port": 0,
  "owner": { "accountId": "telegram-owner", "ownerId": "123456789" },
  "lineKey": "primary",
  "target": "owner",
  "providerThreadId": "telegram:123456789",
  "credentials": {
    "telegramToken": "BOT_TOKEN",
    "telegramLinkFile": { "bot_token": "BOT_TOKEN", "allowed_user": 123456789 }
  },
  "instructions": "Answer the owner clearly and briefly.",
  "modelProxy": {
    "origin": "https://approved-proxy.example",
    "bearer": "PROXY_BEARER",
    "chatModelId": "chat-model",
    "nativeMediaModelId": "media-model"
  }
}
```

For Photon, set `channel` to `photon`, set the owner account/thread explicitly, and use `credentials` with either `{ "mode": "cloud", "photonProjectId": "…", "photonProjectSecret": "…" }` or `{ "mode": "self_host", "photonServerUrl": "…", "photonApiKey": "…" }`. Optional read-only Bridge access is configured with `bridge: { "url": "http://127.0.0.1:<port>/mcp", "readBearer": "…" }`; attended owner chat also mounts reviewed Mac sending when the private owner-command receiver is configured. Optional `skillPackRoot` points to the packaged `skills.sh.json` root; the runtime loads only the indexed skill files.

After building, run `npm start -- --check /absolute/path/runtime.json` to validate the strict config and optional skill index without creating the state directory or connecting to anything. Start the local process with `npm start -- --config /absolute/path/runtime.json`. It acquires single-process ownership, constructs one explicit adapter shared by send and receive, opens the operational journal and recovers interrupted work before starting provider intake, stores Chat SDK adapter state in a separate `chat-sdk.sqlite`, then binds authenticated HTTP on `127.0.0.1`. The printed port is local-only. `SIGINT`/`SIGTERM` request provider and application shutdown; Spectrum 12.14 no longer installs process-exit handlers. A shutdown signal alone still never proves Photon consumer handoff, and unresolved work stays subject to journal recovery. On process exit the OS releases the process-held lock. The diagnostic marker remains for the next process to reacquire the kernel lock; legacy or unidentified markers still require supervised recovery.

For cloud Photon activation, optional `tenantId` binds the configured managed tenant; omit it only for a self-host receiver with an empty tenant identity. The project fingerprint and exact owner/account/line/thread are bound to the journal before intake. Changing or removing that identity requires a separate migration; startup cannot repurpose old owner evidence. Photon server URL/API-key mode does not currently support this activation handoff.

## Standalone experimental container

`Dockerfile.runtime` builds the standalone Node-user image from the `sotto-agent` build context; the root `Dockerfile` repeats that build and adds the self-host root overlay. From `sotto-agent/`, use:

```sh
docker build --platform linux/arm64 -f runtime/sotto-chat/Dockerfile.runtime -t sotto-chat-runtime:local .
```

The image builds TypeScript using the pinned Node 22 image, installs runtime-only npm dependencies from `package-lock.json`, and uses the existing hash-locked Python `requirements.txt`. Its build checks imports, the packaged skill index, private config validation and the run, Google API and Google setup CLI help paths without opening provider/model connections. It copies packaged skills, `adapters/sotto/`, the Python trigger receiver, its shared-library compatibility files (`tzchain`, `work_queue`, `routine_cursor`, `calendar_context`, `source_catalog`, and `message_targets`), and the `adapters/sotto/` modules used by the receiver process wrapper, model lease, Photon activation and system-cron manifest. These are immutable code only: the image entrypoint still starts the Node runtime alone and never invokes `managed_exec.py` or starts the receiver. The image smoke imports the receiver, resolves the five packaged receiver-owned jobs, checks the Sotto runner fence, and verifies that UID/GID 10001 is named `sotto` as expected by `managed_exec.py`; it does not start a listener, worker, model, or provider. The sole Google OAuth implementation lives in `adapters/sotto/google_setup.py`. `SOTTO_HOME` must be an explicit absolute path for Google reads and OAuth setup/writes; missing tokens mean disconnected and invalid or empty roots fail closed. No token is moved or copied by this rule.

At runtime, mount an explicit private config at `/run/sotto/runtime.json`; it must be a regular file with mode `0600` or stricter and owned by UID 10000 inside the container. The runtime process uses UID/GID 10000; its state directory is created private beneath the configured location. Mount persistent state and media paths with ownership that allows UID 10000 to use them. No credentials or config are copied into the image or discovered from environment variables/Keychain. The image binds its authenticated service to loopback and does not publish a port; it is a standalone package check, not a receiver deployment. UID/GID 10001 is named `sotto` to match the receiver wrapper’s fixed drop target, but no script runner or receiver sibling is wired, `setpriv` is not installed, and there is no general skill-script execution. The chat-only preference command adapter described below calls the admitted receiver directly.

## Managed Photon supervisor and provisioning

`managed_bootstrap.py` is the root entrypoint in the managed overlay image. The Cloud operator provisioner always selects it, using one protected launch envelope derived from its exact service, volume and domain receipts. `--prepare` validates and initializes a fresh volume once, then runs a bounded health-only listener; a separate deployment uses `--run`. Run mode never initializes storage. The initializer and runtime use the same pinned image and volume.


The packaged `root_supervisor.py` retains the tenant flock while composing UID 10000 Node and UID 10001 receiver processes. `role_stager.py` publishes separate private configurations for each UID using the canonical `startup_config.py` renderer. The supervisor requires Tini as PID 1 and itself as its direct child in a private PID namespace; it verifies the mounted volume and durable epoch before first launch or same-volume reentry. A stopped container must be confirmed before crash recovery starts.

Node's private configuration owns the channel credential, local API bearer, model-proxy authority and derived read-only Bridge bearer. The receiver separately retains the root MCP capability, runtime API capability and model authority needed by fixed Python procedures. Its control credential is captured by the existing control vault, not inherited by skill subprocesses. Explicit child environments exclude the root launch envelope; each role remains denied the other role's private state. Readiness proves configured Node identity independently of owner activation or source consent.

Start Node first so it acquires ownership, recovers journal obligations, initializes the one provider consumer, and binds its authenticated loopback service before the receiver can issue scheduled sends. Then start the Python receiver as the existing receipt and activation writer, with only its required runtime projection and existing native procedure authorities. Do not start a second consumer for a channel owned by Node.

First-contact Photon needs two different startup conditions. The receiver must start once Node's authenticated HTTP service is listening and its local provider lifecycle has initialized, so it can observe activation evidence and run the existing receipt writer. Waiting for full `/health.ready` before starting the receiver deadlocks first contact: readiness remains false until the receiver writes and ACKs the activation receipt. Starting the receiver at lifecycle initialization does not open scheduled delivery; the Node journal's activation ACK gate continues to return `not_attempted` until that exact ACK is durable. `providerFlow: "not_observed"` remains an honest limit: local lifecycle initialization does not prove events are flowing.

The packaged synthetic lifecycle covers fresh preparation, owner activation/chat, all five procedures, clean restart and accepted-before-response crash recovery. Unknown effects remain held while unrelated safe work resumes. Actual Railway launch topology, provider connection behavior and device-visible acceptance remain live-pilot checks. General replacement profiles remain gated; the explicit managed Photon selection does not enable legacy routine import, migration or another channel.

## Current boundaries

Owner chat also accepts `/quiet for 2h`, `/quiet for 90m`, and `/quiet off` on the three supported fixed-five profiles. Positive integer durations are bounded to 24 hours. These literal commands change only the existing nudge pause; briefs and ordinary quiet hours keep their existing rules. The receiver stores the scalar and exact admitted-event receipt atomically in private preferences state. A lost response requires inspection of that receipt, and later quiet mutations wait while the earlier effect is uncertain. Ordinary chat can continue. The saved admission time and installation timezone determine the end time, so retry does not extend a pause.

The protected read boundary has an owner-only retune projection at
`POST /runtime/owner-retune`. Its typed `owner_stale_loops` tool is mounted alongside owner
open-loop reads in configured interactive chat. It is unavailable to unattended runs. It excludes preferences, mutes, source-backed and legacy rows, and
labels empty results as partial coverage. It shares the bounded receiver HTTP client with
the mounted owner-loops tool; neither projection can mutate the ledger.
Protected owner reads reject duplicate YAML mappings before provenance filtering; ambiguous
source or proof fields return unavailable rather than an apparently owner-only result.

- The transport factories wrap the pinned Chat SDK Telegram and Photon iMessage adapters. Imports do not start polling or connect to a provider.
- Telegram text uses the pinned SDK's plain-text mode: Markdown/HTML punctuation is literal, with no formatting entities to split. `channels/telegram-text.ts` preflights at most 32,000 UTF-16 units and splits rendered text at grapheme boundaries into at most eight 4,096-unit posts; existing SDK emoji placeholders are converted before splitting. Media captions must fit 1,024 units and are rejected before contact if too long. Oversized or blank-only chunks also reject before contact. Posts run sequentially; the full dispatch is accepted only after every post returns a distinct root provider message ID. Accepted chunk receipts survive journal reopen without a second send. Any uncertain chunk stops remaining posts and holds the whole dispatch as unknown across restart, even if a prefix was accepted; it does not automatically resume or resend that prefix.
- Paired Railway Telegram chat can read back accepted brief/prep text through `accepted_brief_detail`, using the same receiver-owned protected projection as iMessage. Every read requires current source consent plus the protected Telegram owner/bot link and independently authenticated loaded runtime identity; changed pairing withholds content. The tool remains unavailable to unattended runs and cannot queue a new brief. New requested briefs, offered-prep acceptance and owner skill execution still require their separate attended Telegram contracts.
- A settled partial text send retains its known accepted prefix IDs in the journal and scheduled receipt's `message_ids`, with `acceptance: unknown` and no accepted route/payload proof or whole-dispatch `message_id`. These IDs are diagnostic only. A process crash, or an interactive stop that wins the wait, before transport settlement can still leave the dispatch unknown without saved prefix IDs; recovery holds that dispatch and never infers which parts to resend.
- `/stop` aborts future Telegram chunk contacts through a separate delivery signal. A provider request already in flight can finish, but its later chunks do not start; the original dispatch stays held and the stop acknowledgement remains a separate receipted send. Model corrections and graceful shutdown retain the existing contacted-send semantics.
- Channel credentials are passed explicitly to `createTelegramSdkAdapter`, `createPhotonSdkAdapter`, or `createChannelSdkAdapter`. The gateway accepts that same adapter instance, so transport and consumer configuration cannot silently diverge. Channel constructors do not use ambient environment fallbacks. Telegram requires an explicit bot token plus a pairing record whose `bot_token` matches and whose `allowed_user` identifies the owner. Its polling options preserve existing webhooks and pending updates. Photon configuration must provide one complete explicit mode: Spectrum Cloud project ID and secret, or server URL and API key.
- The ingress wrapper calls a durable-admission callback before Chat SDK deduplication, queueing, or handler dispatch. A coordinator-owned handoff suppresses a second SDK handler path. A synchronous callback can commit through SQLite before returning to a non-awaiting adapter.
- Admission conflicts that can never succeed on retry (a changed redelivery of an already admitted provider event ID, or a second owner chat after the first-contact destination is bound, such as SMS after iMessage on the same line) are denied, so the provider acknowledges them. Transient journal failures still reject: Telegram keeps the update for retry, and Photon's non-awaited callback logs one content-free line instead of raising an unhandled rejection. Telegram owner edits (`edited_message`) are denied and acknowledged; edits are not re-processed and the original admitted text stands. Inbound text truncation at 16,000 UTF-16 units never splits a surrogate pair.
- `startOwnerGateway` currently supports one explicitly configured owner, account, line, channel, and provider thread per gateway. It rejects non-owner, non-DM, other-thread, receipt-only, and contentless events before calling the application. Ingress commits bounded text and provider media references; it does not fetch provider URLs. Telegram albums retain ordered file IDs, Photon groups/replies retain bounded content paths, and excess attachments receive an explicit omission count. Its `lifecycle` reports SDK start/stop lifecycle, while `providerFlow: "not_observed"` makes clear that a started listener is not evidence that events are flowing. A terminated Photon listener cannot be revived in-process, so once it has stopped `lifecycle` reports `failed` and the runtime needs a supervised restart. Bearer `/v1/health` adds content-free `providerIntake` counters (received and handed-off events, plus Photon listener starts/stops and stream/handler errors); the Python adapter relays them as `provider_intake`.
- `startConfiguredRuntime` validates a private, non-symlink, strict JSON config; holds one runtime-directory lock before opening agent storage; starts the application and recovers interactive obligations before provider intake; reuses the exact adapter object for send and receive; and only opens the loopback HTTP listener after the gateway initializes. Config check is offline and does not create state.
- Telegram's automatic pre-admission typing hook is disabled through its public protected override; turn-owned delayed typing is described below; Telegram read marking remains unimplemented. SDK loggers are silent so raw provider exceptions cannot expose authenticated URLs.
- Gateway shutdown explicitly calls Telegram `stopPolling()` because Chat SDK shutdown logs and swallows adapter disconnect errors. It stops provider intake before closing the application, allowing the application to abort coordinator and HTTP unattended work before stores close. Photon manual shutdown memoizes and awaits the first public `app.stop()` call; this proves local consumer shutdown only, since Spectrum can swallow remote cleanup failures. Signal-triggered shutdown, listener abort alone, or a failed stop retains ownership until process death. A provider stop that cannot be confirmed retains the runtime lock/resources. A listener failure is surfaced as a `failed` lifecycle; this experimental gateway does not claim autonomous reconnection or provider downtime replay.
- The process-level runtime lock is a separate SQLite `BEGIN EXCLUSIVE` handle retained for the process lifetime and released only after clean teardown; kernel lock release after process death allows supervised restart without PID/TTL guesses. An older directory-only lock marker is not removed automatically.
- The Chat SDK state adapter persists SDK cache, subscription, lock, list, and queue state in caller-owned SQLite tables. It does not own or close the database and is not the operational inbox journal.
- Configured Bridge and Google reads require a fresh receiver source-authority revision. The version-2 projection independently reports Bridge source policy and Google grants: Bridge reads require the existing Mac permission receipts and, in managed mode, tenant grant ledger; Google reads require an explicit `SOTTO_HOME` token root plus current token-generation lineage (and, in managed mode, the latest applied tenant bootstrap). Missing, legacy, corrupt or unreachable proof exposes no corresponding tools. Each typed read checks authority before and after provider contact. Bridge health failure does not revoke an independently current Google grant, and vice versa. Discovery exposes neither unknown tools nor send permission.
- Both Mastra agents explicitly set model and processor retry budgets to zero. HTTP 500, mapped 503, admission/budget refusals, cancellation and connection failures are tested through the installed managed-proxy adapter; a single failed turn contacts it once. Provider-history normalization remains enabled.
- Generation uses call-time read-only Memory. The application projects admitted owner text and accepted journal reply payloads with deterministic native message IDs; rejected/unknown sends and discarded model acknowledgments never become assistant history. Checkpoints contain only identities, not another transcript. Startup, accepted delivery and the next turn repair interrupted projection without sending. First adoption clears the old generation-written transcript and rebuilds the session that was current from retained, currently authorized journal evidence; journal events from sessions the previous binary had already rotated, nightly or by `/new`, stay excluded. Unknown source authority defers all pending memory projection. Rotation excludes old event identities, including later acceptance, and completed checkpoints prevent replay after source or retention purging. Request-only quoted/review/web/media context is not stored as owner input.
- Bearer health includes the latest bounded `modelMetrics` snapshot: mounted tool count and input-schema bytes, maximum serialized prompt bytes, standing-context characters, model/step/tool durations, processor retries, reported token usage and normalized finish reasons. Schema conversion failures are counted separately; absent provider usage is zero, not a charge estimate. Snapshots retain no prompts, arguments, results or call IDs, and telemetry failure cannot change a turn. Native memory recalls at most 20 recent admitted/accepted messages. Each recalled message's prompt copy is bounded to 32,768 characters with an explicit truncation marker, and recall as a whole to 65,536 characters: the immediately preceding owner/reply pair is kept, older pairs fall out with an explicit omission notice. Stored native rows, the journal and delivered text are not rewritten. This bounds conversation history separately from current instructions and source context; a large current source snapshot cannot evict the immediately preceding answer through a whole-prompt token budget. Source-proof, session and retention checks still apply before recall. The regression corpus uses scripted model/provider fixtures and does not certify live reasoning quality or latency.
- Before loading chat history, a verified changed revision or missing verified baseline clears all Mastra threads. Temporarily unavailable authority suspends native recall and projection without deleting the last verified cache or baseline. A private sidecar records only which revision the transcript was cleaned against, after deletion succeeds; it grants no permissions. Removing the Bridge reader also changes the baseline and purges source-derived history; stable owner-only configurations keep their own history across restarts. A second check withholds results if authority changes during generation. Ordinary owner-only chat can continue with native history disabled when source authority is unavailable. Same-revision recovery may use the unchanged cache; a confirmed revision change still purges it before recall. Session resets and retention deletions are never reconstructed. These are boundary checks, not an immediate push-based cancellation service.
- Prepared interactive replies retain their source revision and recheck it before provider contact, including recovery after restart. Schema 2 preserves older pending replies with an unknown proof; startup holds those replies instead of sending them, including after removing Bridge configuration. Only explicitly owner-only replies bypass external source proof. `/stop` can cancel an uncontacted held reply. Transcript rotation never regenerates it. A completed provider contact retains its receipt regardless of later permission changes.
- Exact `/stop` cancels earlier work immediately, then sends one fixed, owner-only acknowledgement through the interactive reply journal. It cannot undo a contacted action or an uncertain send. If an active reply is already in provider contact, stopping settles its wait as unknown and never resends it; the original provider call may still finish. `/new` drains prior work, rotates conversation memory, and sends one fixed acknowledgement; canonical records and delivery proofs remain. Prepared control replies resume after restart without repeating rotation. A control send still unconfirmed after ten seconds becomes unknown and is never retried; unrelated safe chat can continue. Photon may mark the command read and add a completion reaction after accepted delivery, but skips the ordinary thinking reaction and model typing for these controls.
- The current journal is schema 6, adding exact scheduled-send route digests to schema 5's admitted owner-command boundaries and permanent write fences, schema 4's discovery seed, schema 3's activation identity/obligation and schema 2's reply source proof. Upgraded route-null scheduled text/gallery rows that remain prepared, dispatching, unknown or rejected stop startup before service/intake until offline reconciliation; accepted/cancelled history and interactive replies are exempt. Historical routes are never backfilled from current configuration. Older experimental binaries refuse it. Reverting requires a binary that understands schema 6 and its unresolved obligations; restoring an older database copy can lose accepted input or receipt evidence and is not a rollback procedure.
- A cloud Photon owner DM records activation evidence in the same transaction as inbound admission. The receiver's existing cron tick pulls metadata through authenticated `/v1/activation-evidence`, validates its tenant/owner/project and restricted allowlist, calls the existing Photon receipt writer, and ACKs the exact event through `/v1/activation-ack`. A lost ACK retries that writer idempotently. Pending evidence survives restart, rotation, cancellation and retention. After ACK, a new owner DM can repair a lost receiver receipt; replaying the acknowledged event cannot reopen it. This neither grants source access nor starts a second scheduler. The handoff is locally verified through the packaged managed Photon profile; live activation remains unverified.
- Receipts distinguish provider acceptance, definite rejection, uncertain outcomes, and sends not attempted. The pinned transports require a bounded root outbound-message ID for acceptance; nested attachment or derived group-part IDs do not qualify. Once an SDK send is invoked, a thrown exception remains unknown: a generic HTTP-status field is not proof of non-acceptance. Local validation can still reject before contact. The journal/caller owns recovery and retry policy.
- Unknown Calendar, Contacts, Gmail, owner task, reminder, and preference actions use exact receipt inspection and at most two pending-outcome notice attempts, spaced by a durable one-minute inspection cadence. An accepted or unknown notice is never resent; a later confirmed effect gets its own acknowledgement without rerunning the action. Task proposal previews also retain their existing recovery path. Task, reminder, and preference rejections are not currently reconciled from an unknown action and remain held. Inspection runs on recovery or a new owner turn, not from an autonomous timer, and follows the same one-minute cadence before and after the notice (one probe per process before it). A fresh safe turn takes precedence over a pending notice; the notice then follows in the same running turn once only held input remains. A failed acknowledgement after a contacted reply holds only that owner input. `/stop` closes only the newest open request: when that is the uncertain action's input, it closes it without erasing the uncertain action or sending a late acknowledgement; an older input still awaiting delayed proof stays open, so an unrelated `/stop` cannot suppress its final confirmation.
- Photon gallery delivery uploads attachments before its final multipart-message RPC. The public-client regression exercises synthetic retryable gRPC errors after a simulated effect for text, multipart and upload: each makes one RPC with the current pinned dependency chain, while its final public error still says `retryable: true`. This is observed behavior of those versions, not a provider idempotency guarantee or a promise for dependency upgrades. No SDK retry patch is installed.
- The authenticated `/v1/retention` route accepts only ordered past UTC delivery/action cutoffs from the receiver's existing 3:30 AM sweep. It prunes terminal operational rows after the canonical 90/180-day windows, retaining unresolved contacts, pending covered replies and gallery receipts. It does not delete Mastra transcripts or create another timer.
- The same retention call removes only already-expired Chat SDK cache, list, queue and lock entries; permanent entries and subscriptions retain their existing semantics.
- The same retention call then compacts without deleting any fence or receipt: settled (accepted, rejected or cancelled) gallery dispatches older than the delivery cutoff lose only their payload, keeping route digest, payload hash, attempts and provider IDs for receipt lookups and duplicate-send refusal; terminal model-invoked owner-tool actions older than the action cutoff keep id, kind, run, source and payload hashes and status as their lifetime idempotency fence while `result_json` becomes a digest (a proposal consumer keeps only its proposal link). Threads with pending input, a running run or unresolved action proof are not compacted. Unknown dispatch and reply content older than the action cutoff is scrubbed with its proof keys retained. The response shape is unchanged.
- Admission binds an open proposal to the next owner event by reading only proposal-kind actions not yet evaluated since their last result change (an additive `proposal_checked` column, partial index and trigger; schema version unchanged and compatible with existing journals). Settled history is no longer parsed inside every admission transaction.
- Photon galleries decode and canonically validate the Chat SDK thread, then resolve the public Spectrum space with its raw chat GUID and exact physical phone suffix. They use one grouped Spectrum send with bounded manifest validation. Text sends retain the adapter postMessage path. The existing scheduled-delivery route remains owned by the operational delivery journal.
- The [runtime tool capability audit](../../docs/RUNTIME-CAPABILITIES.md) inventories documented skill operations and the runtime's mounted capabilities and remaining read-only candidates; it is not an authorization to execute a skill's shell examples.

## Explicit first-contact Photon binding

A fresh Photon Cloud configuration may omit `providerThreadId` only with
`firstContact: { "mode": "owner_dm", "expectedProviderLine": "+15550002222" }`.
The expected phone is the assigned physical sending line; `lineKey` remains the logical
runtime identity. Telegram and Photon server/API-key mode still require a fixed thread.

The same gateway admits only a nonempty incoming owner DM on that physical line, using the
pinned adapter's public decoder, encoder and DM predicate. The first admission atomically
stores its full provider thread, inbound event and activation obligation. Binding survives
restart and message retention; changing the owner, project, tenant, line or destination,
or converting fixed/discovery mode, requires a separately reconciled state directory.
The model cannot choose or replace the binding.

Interactive replies may use the captured thread after durable admission. Scheduled text and
galleries return `not_attempted` without creating a dispatch until the receiver has ACKed
the exact activation event. Health reports bound, pending and acknowledged states; `ready`
remains false until binding and ACK, while authenticated health/activation routes remain
reachable. A later admitted owner DM creates receipt-repair evidence and closes this gate
until its new ACK. During an ACK outage, these `not_attempted` results still consume the
receiver's existing charged retry budget; this code does not change that policy.

Scheduled text retains its stable outbox and journal dispatch IDs across process
replacement. A definitive `not_attempted` or `rejected` outcome may use the existing bounded
retry policy. Once any contacted attempt has an unknown outcome, a higher attempt number cannot
send again; the receiver polls the read-only receipt and holds that row until provider acceptance
or a trusted definitive no-effect result is recorded. A later rejected attempt does not erase an
earlier unknown attempt. The hold applies to that dispatch only, so unrelated queued work can
continue.

The receiver remains the sole activation receipt writer. This channel gate does not grant
source consent or authorize deployment. Media recovery
uses the exact saved thread and one persistent hydration slot, including when an SDK lookup
outlives cancellation. Local fixtures cover the actual Python receipt writer and lost ACK;
live fresh onboarding and provider replay remain unverified.

## Incoming media

Configured startup hydrates media after durable admission and a current-run fence. Telegram
uses a fixed official API origin and validated returned file paths. Photon uses the existing
adapter's public message lookup, checks the exact returned owner/thread/message/content
reference, then uses its public stream. Arbitrary attachment URLs and unbounded `read()`
helpers are never used. Four attachment positions, 8 MiB per item and 16 MiB per turn bound
processing. Failures and omitted content are explicit in the model prompt. Cancellation
fences late bytes before a model call; prepared-reply recovery never downloads or regenerates.

Each provider read has a 30-second caller deadline. Photon message lookup and stream
acquisition cannot be aborted through the SDK: after timeout or cancellation, their single
operation slot stays occupied until the underlying promise settles, and late streams are
cancelled. Acquired streams are cancellable. This bounds concurrent outstanding reads;
it does not promise that an underlying SDK lookup stops at the caller deadline.

Supported types are PNG, JPEG, WebP, GIF, WAV, MPEG audio, Ogg and WebM. Signature checks
establish only MIME/container consistency, not decodability or safety. Unsupported voice
encodings are reported as uninterpreted. Downloaded bytes stay transient; no extra spool or
cache is created. Restart re-reads the exact provider reference or reports unavailable content.
Provider retention, device formats and interpretation quality still require live acceptance.

## Acceptance and rollout still required

The managed Photon profile has packaged local receiver/supervisor integration. There has been no live Telegram or Photon account acceptance, Keychain integration, volume migration, or deployment/canary. Photon 3.2.0 calls the Chat SDK ingress without awaiting its Promise, so asynchronous admission alone cannot guarantee durable commit before the provider callback returns; strict commit-before-ack requires a synchronous commit at that boundary or owning the provider receive/ack path. Chat SDK state/deduplication is not the operational inbox journal, and no adapter-level queue promise is treated as replay or provider acknowledgement. The runtime must not replace the current process until isolated live acceptance, recovery behavior, and rollback are verified.

## Owner feedback and turn presence

Supervised manifests preserve `turnPresence: false` because their saved configuration is immutable across restarts. Read acknowledgements and tapbacks do not depend on this flag. Hand-written private runtime configurations also default to `false`. When explicitly enabled,
typing begins two seconds after durable owner/model-run admission and refreshes two seconds
after each settled pulse. Fast turns never flash typing. Cancellation, replacement, `/new`
and shutdown end the turn's presence; prepared-reply recovery does not start it. Photon uses
public Spectrum Space start/stop methods on the decoded chat and phone line. Telegram uses
its public start method and has no stop primitive.

Fresh durable Photon owner-DM admission triggers a best-effort read mark and the existing local request-hint tapback. The provider read mark is conversation-wide. An accepted reply replaces the hint with ✅; thanks keeps ❤️. Held/unknown replies do not get a completion tapback. Rejected task changes and partially completed multi-task changes also withhold it, while their accepted explanation still resolves the incoming request. Foreign, self, group, duplicate, and recovered historical input do not trigger this feedback. Each feedback call has a one-second waiting bound and no automatic retry. Uncertainty withholds further feedback for that owner message; a newly admitted owner message can make its own feedback attempt. Late lookups remain fenced, and an already-contacted old call settling later cannot disable the new message. These signals never authorize effects or prove delivery, and provider failure does not block the owner reply. Actual device presentation still requires live verification.

The bearer-protected `/v1/health` response includes an optional in-memory `feedback` snapshot for the latest fresh owner turn. It reports fixed read, processing, and completion stages, local lookup/effect timings, accepted-reply callback status, and uncertainty. A timed-out stage stays timed out when its call settles later; late settlement is reported separately. The snapshot resets on restart and includes no message IDs, content, emoji, credentials, provider bodies or error text. It is diagnostic evidence, not confirmation of what a device displayed.

Each provider operation has a one-second caller deadline. An uncertain operation disables
further typing for that thread until restart; a late start settlement triggers best-effort
stop. This bounds waiting, not the underlying SDK call, and cannot prove remote dots stopped.
Typing never blocks durable model cleanup. Shutdown closes intake and fences new typing,
then drains bounded local presence work before provider stop. A timed-out start may settle
later and trigger a best-effort compensating stop; this cannot prove zero late provider calls.
Provider stop/visible presentation require live
acceptance. Config and history-proof reads reject FIFOs without blocking before file checks.

## Continuity read groundwork

The pure `ledger_provenance.py` helper validates a versioned digest over parsed frontmatter,
requires a complete canonical source union, and filters before rendering/counting through an
injected permission callback. Callback failure invalidates the whole projection. The digest
detects unstamped content changes; it is not a permission grant or signature. All legacy rows
remain withheld, even with old source references, because past writers could change summaries
without updating those references.

`owner_continuity.read_owner_continuity` supplies a bounded owner-only read.
It requires an explicit absolute root and aware clock, opens regular non-symlink files through
directory descriptors, and caps directory entries, file count, individual/aggregate input and
response bytes. Observed file/directory mutation invalidates the read; this is not a transactional
snapshot. Provenance filtering precedes canonical active/snooze selection and loops/retune
rendering. Results say `coverage: owner_authored_only`; source-backed and unproved rows stay
withheld, and the retune view does not load ambient preferences.

The `op_loop_add_admitted_owner_text` callable stamps a new manual row from an exact
normalized owner text span, a bounded event reference and a trusted aware clock. The explicit
owner-command path below proves admission and direct owner origin before calling it. The event reference is
an audit reference, not authentication. It accepts no source-derived contact/deadline fields.
Existing model-callable CLI additions and retargets remain unproved; existing rows are never
backfilled, and later content changes invalidate the digest. Complete writer coverage and current
source authority remain required before exposing general continuity read tools.

The admitted-writer precheck runs under the canonical ledger lock and uses strict bounded
file reads, rejecting malformed or duplicate-key YAML, symlinks and incomplete enumeration.
A retained event with matching valid owner provenance and exact text reuses its row across
date rollover; altered or ambiguous proof is held. A new event cannot stamp an existing
source-backed or legacy anchor. This is not permanent event acceptance: field removal,
file deletion and the resolver's 30-day terminal pruning can remove that record. The explicit owner-command path uses an outer durable journal to fence the event ID and immutable
text digest for the full retry lifetime. The ordinary parser and legacy CLI behavior are unchanged.


When the existing Bridge receiver connection is configured, chat gains `owner_open_loops` and
`owner_stale_loops`. They call only `POST /runtime/owner-loops` and `POST /runtime/owner-retune`
on the exact configured loopback origin using the
existing derived read bearer. The receiver requires a loopback peer, a fixed-five profile
and a strict small JSON request; its own clock supplies the owner's local date. Results are
capped at 64 KiB on the wire, with a five-second full-body client deadline and cancellation.
Late/error bodies are cancelled, invalid UTF-8/schema/counts are rejected, and failures expose
no source payload or private URL. Output states explicitly that empty lists do not establish
there are no other obligations. Both tools require interactive run/owner-event context, are absent
from unattended execution, and remain available when Bridge source discovery fails; that
failure grants no Bridge source tools. It does not expose the writer, enable a script runner,
or backfill legacy rows. Neither tool is mounted when Bridge receiver
configuration is absent. The ordinary complete ledger views remain the supported full view.

The isolated pinned-Telegram regression uses the actual polling adapter, gateway and SQLite
operational journal with synthetic fetches. It checks durable admission or retry-state storage
before offset advancement, injected-duplicate deduplication and retry recovery after restart, and rejects
unexpected sends, typing or webhook mutations. This local ordering evidence does not replace
live pairing, provider downtime or migration/rollback acceptance.

### Bridge send boundary

Configured bootstrap keeps raw Bridge MCP sending unavailable. Attended chat instead uses
`bridge_send_review` and `bridge_send_confirm` through the private owner-command receiver.
The receiver stores the exact recipient/body; only a distinct clear yes to the accepted review
can send. Intervening messages, `/stop`, `/new`, changed details and expired reviews invalidate
approval. Managed permission and the Mac's opt-in remain required at dispatch.

A durable receiver claim precedes the relay call, and the Mac binds its receipt to the same
request ID and payload. Known pre-dispatch failures are reported as rejected. Unknown outcomes
remain held, including after restart: inspection reads saved receiver receipts and never replays
`send_message`. If the Mac receipt never reaches the receiver, automatic recovery cannot confirm
it; the owner must check Messages. Messages acceptance is not confirmed recipient delivery.


### Explicit owner-write proof

Journal schema 5 records a run's admitted-event boundary. The experimental owner-loop intent
helper reads exact saved owner input within that boundary, including a second coalesced event;
it accepts one literal `/loop add <exact text>` or `/loop close <anchor>` command and takes no
model-supplied event ID, quote, or selected target. Mixed explicit commands are held without effect. The managed-Photon coordinator connects this proof to the private receiver writer as
described below. Earlier running rows without an admission boundary cannot mint this proof.

The special action retains its event/text hashes for the retry lifetime, beyond ordinary action
retention and canonical loop pruning. Once all action/reply/activation obligations resolve, raw
quotes are scrubbed while identity and compact receipt evidence remain. Running threads retain
their inbound rows so SQLite row-id reuse cannot admit a later command into an earlier run.
A write interrupted after it starts remains unknown until trusted reconciliation; missing ledger
rows never authorize another attempt. Only typed canonical pre-effect conflicts can close an
executing intent as rejected. A ledger receipt establishes the mutation, not a delivered reply.


The maintained canonical-writer fixture runs real Python code and requires the locked PyYAML
dependency; missing dependencies fail verification rather than skipping it. `Dockerfile.test`
installs that one dependency from the existing hash lock in an isolated test venv. The writer
syncs file and directory entries before success, and injected post-replacement sync failure
leaves the action unknown after restart. These checks verify ordering and recovery behavior;
they do not simulate hardware power loss.


### Experimental explicit owner command

Managed Photon and the fixed self-host Photon/Telegram profiles rendered by the experimental startup helper include a separate
receiver capability for `/loop add <text>` (up to 500 characters) and `/loop close <anchor>`
(up to 256 characters in the exact anchor). The coordinator handles the
literal admitted owner command before model work and acknowledges only a canonical write receipt.
It does not expose a model mutation tool or grant the ordinary read bearer write access. Pending
writes with an uncertain result stay held; restart can reconcile a matching canonical row without
writing again. A changed timezone/capability or missing row keeps the operation held. This path
has local fixture coverage only; general receiver readiness remains closed. The limited fresh-volume
profile described above is an explicit experimental exception.

Closing a task requires a unique active canonical row with valid owner-only provenance. The
receiver prepares its current digest, which the journal binds with the timezone before any write.
The writer rechecks that digest under the canonical ledger lock, then resolves the row through the
existing canonical persistence path. It preserves the original creation attribution and adds a
separate event-bound close receipt. A source-backed, legacy, changed or ambiguous row cannot be
converted into owner authority by closing it. A lost write response is reconciled only from that
exact terminal receipt; absence from the open list alone does not prove success. The fixed close
acknowledgement is sent through the ordinary durable reply path. Replaying an admitted add or close
cannot recreate or reclose a task. This does not enable natural-language, bulk, deadline or retarget
writes, and it does not establish live verification.

Photon owners can accept an available meeting-prep offer by replying directly to its accepted
message with a clear “yes”. The original provider reply target selects the offer; an unthreaded
or time-only reply does not. Existing routine and Gmail review confirmations retain priority,
including when their review needs renewal. The private admitted-event endpoint queues one prep
through the existing receiver work and delivery pipeline; the chat acknowledgement says only
that it is queued. Unknown admission is inspected from its exact durable proof, never re-created,
and unrelated safe chat can continue. This source-tested path does not establish live verification.

On Photon, replying directly to an accepted source-loop offer with “no” or “not now” records a
decline without changing the loop's business fields. An exact “no” can also decline an accepted
meeting-prep, procedure or intention offer; “done” and “drop it” cannot change those offers. “Done” or “handled” resolves that loop;
“let it go” or “drop it” dismisses it, only when the receiver can validate the original source
snapshot and current authority. An unthreaded, mixed or ambiguous reply cannot select a loop.
Existing routine and Gmail proposal replies retain priority. The acknowledgement reports only
the saved result; a lost response stays held for exact receipt inspection while safe chat continues.
This path does not establish live verification.

Those profiles also accept literal owner-sent `/reminder at <ISO8601-with-offset> <text>`,
`/reminder list`, and `/reminder cancel <int_id>` commands. Reminder text is limited to 500
Unicode codepoints; the timestamp must include `Z` or a numeric offset, and the receiver stores
the due instant in UTC. The private owner-command capability binds an admitted event and current
route to a separate reminder action domain and receiver-only `/runtime/owner-reminder-intent`
writer. Listing returns at most 50 pending owner-command or owner-tool reminders, excluding legacy intentions. Due
reminders use the existing proactive cadence and delivery gates, so the requested minute is not
a delivery guarantee. Cancellation prevents future scheduling but may not recall a send already
past its delivery check. Uncertain sends remain held; accepted sends retain their confirmed result.

For one clear attended owner message, the chat model can also use `owner_reminder_context` and
`owner_reminder_create` to record an unconditional one-time reminder in ordinary language. The
context tool supplies the configured timezone and current clock. Ambiguous day/time requires
clarification; private reminder bookkeeping does not require a second approval. The coordinator
supplies the original admitted owner event and current-run authority, and the receiver stores
distinct `owner_tool` provenance through `/runtime/owner-reminder-tool`. Model arguments contain
only the explicit-offset due time and reminder text. Coalesced multi-message turns cannot use
this mutation tool. Current source authority is checked before contact; a post-write source
change can invalidate model prose but cannot recreate the effect. Acknowledgements use only
the verified saved status, without exposing internal IDs or UTC timestamps. A lost response retains exact arguments for
inspection without another create, while unrelated safe owner chat can continue. The existing
list/cancel commands and proactive delivery apply. An explicitly requested conditional reminder can bind one exact current waiting-on loop selected through protected loops-query, with the owner's condition quoted verbatim. The original due scanner rechecks that identity and current source grants: resolved loops cancel the reminder; missing, snoozed, ambiguous or revoked context holds it. This grants no arbitrary condition evaluator, recurring jobs, external writes or outbound-send approval.

Ordinary owner chat also exposes `owner_task_add` and `owner_task_close` for one explicit private
task correction per admitted message. Add extracts a trimmed, exact span of the owner's words
(up to 500 Unicode codepoints), preserving canonical owner authorship rather than recording a
model paraphrase as a fact. Add can also preserve an explicit `you` or `them` direction,
with optional contact and identifier copied exactly from the same owner message; unknown
details stay unknown. The original Loops `knowledge-edit --op loop-add` command uses this
same path. A waiting-on item is created directly by the original writer, and a protected
readback verifies its lane before the reply claims visibility. Capture itself sends no nudge.
Close reads `owner_open_loops` and selects one exact current
owner-authored anchor; ambiguous matches require clarification. Direct private corrections need
no second approval. The receiver's `/runtime/owner-loop-tool` reuses the existing canonical
writer, records a distinct original-event proof, and retains terminal receipts. It never invents a
literal slash command. Lost responses are inspected with saved arguments without repeating the
write; unrelated safe chat continues. The same owner input cannot perform both a task mutation
and a reminder or preference mutation. Inferred completion and external actions are outside these direct add/close tools.
Source corrections and selected cleanup use the protected paths below. Existing literal task commands remain available.

The loaded Loops skill can also snooze one exact current obligation through the
`retune-apply` command. It first reads `loops-query`, then supplies the row's anchor,
version and identity, the owner's exact request, and a positive number of calendar days.
The receiver reuses the original snooze transform and canonical writer. It preserves the
obligation's history and source dependencies; snoozing never means resolving or dismissing.
The original admitted local date is retained in the receipt, including across timezone
changes. A lost response is inspected without another write or a later snooze date.
Current protected readback determines whether the acknowledgement can confirm that the
item is hidden. This single-item command does not allow arbitrary source edits; live use
requires the separately verified deployment.

The original Loops “I still care” action uses `retune-apply keep` with the same
exact row binding and owner request. An explicit protected `loops-query --parked`
read can select a parked obligation. The original keep transformation unparks a
user obligation and refreshes its park clock, or resets all chase fields for a
waiting-on obligation. It preserves the original evidence date and any snooze;
a waiting-on row that was parked remains parked, matching the original writer.
The acknowledgement follows the actual receipt and protected readback rather
than promising that every kept item is visible immediately. Recorded outcomes
recover by inspection, and an uncertain write is never repeated. This enables
one explicit keep action without inferred completion.

Multi-item stale cleanup uses the same `owner_loop_tool` action family and the original
scan and per-row transforms. `retune-apply ["list"]` returns a canonical numbered list
(up to 20, with any remaining count disclosed). The runtime retains its exact accepted
delivery. The next owner message can explicitly select dismiss, snooze with a day count,
or keep through `retune-apply ["cleanup", JSON.stringify({items})]`; each item contains
`index`, `action`, the exact `correction` clause, and `days` only for snooze. The whole
message must be supported explicit clauses joined with separators; negation, vague approval,
unmatched instructions and ambiguous references refuse rather than selecting extra items.
After an intervening owner turn, obtain a fresh list. Model arguments cannot supply the
saved list, anchors or proof. These explicit private choices need no further yes.

Ordinary open-task lists use `owner_task_list` with the same accepted-list receipt and
`scope: open`, showing up to 50 rows within the message bound and disclosing the remainder.
The immediately next owner reply can mark multiple displayed numbers done, including
“1, 4 and 22 are done”. Numbers always bind to that saved list, never a fresh query order.
Named completion batches validate each target independently: unavailable or ambiguous names
remain unchanged while clear selections close. New tool calls mark partial handling explicitly;
already-admitted legacy requests retain their original all-or-nothing preflight semantics.

One event receipt in the existing preferences map retains each selected item's outcome.
One-shot effect receipts in that map remain for the life of the preferences file so a later
owner event cannot reuse a consumed proposal, including after ordinary preference changes.
The newest 100 settled scalar preference and quiet-command receipts remain available for
event retries; previously pruned one-shot receipts cannot be reconstructed from current state.
Current target/source checks precede each write; stale targets are refused individually.
Original snooze and direction-specific keep behavior is preserved; dismiss uses the original
termination transform with the admitted request time. Process loss during an edit leaves that
item uncertain and later pending items skipped; selections already refused remain refused. Inspection finalizes the recorded partial outcome
without invoking a row writer, and unrelated chat can continue. The acknowledgement names
applied, refused, uncertain and skipped item numbers; it never treats a recorded aggregate
as proof that every edit succeeded. This is sequential cleanup, with durable partial results.

Ordinary owner chat exposes `owner_preferences` to read explicit preferences and
`owner_preference_change` to add/remove sender, person and section mutes, tone notes and VIP
people, clear explicit tone notes, or set the existing nudge budget to fewer/no/more. A clear
explicit private request needs no second confirmation. Adds preserve an exact span of owner
text; removals select an exact stored value from the read projection. Ambiguity requires
clarification, and source content cannot authorize these changes. The same admitted message
can make only one task, reminder or preference change. Exact saved arguments and original
receipts support inspection-only recovery and factual acknowledgement after later changes.
Nudge limits leave scheduled briefs and digests available. This preference path is verified by
source and real-receiver tests and the complete packaged managed lifecycle. Self-host preference
packaged acceptance and live verification remain separate.

Temporary nudge quieting uses the original `preferences.resolve_snooze_spec` at
the admitted owner-local instant. Commands bind the requested duration or local
time to the current owner's words; the model cannot supply a calculated timestamp.
The existing atomic preference writer saves `nudge_snooze_until` and its original
receipt together. Recovery returns that historical lift time without extending
the pause, even after later preference or timezone changes. Existing nudge
consumers honor the scalar; scheduled briefs and digests remain available.
Explicit resume clears only temporary quieting. “Back to normal” does so when the
temporary pause is active and there is no separate reduced permanent limit;
otherwise the reply asks which setting the owner means. No permanent budget is
silently changed, and no new scheduler or preference store is introduced.

Fresh Photon and paired self-host Telegram personal routines use `owner_routine_propose`, `owner_routines`,
`owner_routine_create`, and `owner_routine_cancel`. The receiver chooses the installation timezone
and owner destination. Creation requires an accepted question describing the exact hourly, daily, Monday–Friday, single-weekday, weekday-set, or first-of-month
schedule and standalone prompt, followed by a distinct clear yes. The model cannot select or change
creation arguments during confirmation. A new request, `/new`, `/stop`, changed source authority,
or uncertain question delivery prevents confirmation of the old proposal. Cancellation selects one
unambiguous listed routine. No system schedule or imported native routine is changed.

`owner_routine_adjust_propose` reviews a new supported schedule for one listed personal
routine; `owner_routine_adjust_confirm` applies only that accepted review after a separate yes.
The review shows the old and new schedule, captured timezone, full task and destination.
The receiver preserves the task, name, timezone and destination, and atomically cancels the
old immutable definition and creates its replacement in the existing routine store. The old
receipts and occurrence records remain intact; queued or uncertain old work is not moved or
retried. The replacement's first occurrence is after the confirmation's admitted time.

The existing receiver queue runs these prompts through the stateless unattended agent. It exposes
read-only owner-authored open loops with explicit partial coverage, alongside permitted Bridge reads;
it receives no private mutation, scheduling or send tools. Original create/cancel receipts remain
available for inspection after a lost response. Schedule adjustment preserves the routine's captured
timezone; a separately created routine uses the current installation timezone. Fresh schedules use the first occurrence of a repeated local clock
time and skip a scheduled wall-clock slot when that time does not exist during a clock change.
Daily/single-weekday routines are implemented for Photon and paired self-host Telegram.
The additive `weekday: "weekdays"` schedule uses the same fixed local hour/minute,
proposal, receipt, cursor and delivery path; it maps only to cron `1-5` and advances
Friday to Monday. Existing daily/weekly definition bytes and identities are unchanged.
An explicitly requested weekday set uses `weekday: [0..6, ...]` with one to seven unique
days in ascending Sunday=0 order, such as `[1,3,5]` for Monday/Wednesday/Friday.
The full set appears in create and adjustment reviews; the receiver uses the same
confirmed proposal, cursor and delivery path with canonical cron `1,3,5`.
Unsorted, repeated and out-of-range days are rejected. Existing scalar and literal
schedule hashes remain unchanged; equivalent set/scalar/daily/weekday forms cannot
create a second routine with the same task. No source-read capability is added to unattended runs.
The additive `weekday: "first_of_month"` uses that same path and emits only
`minute hour 1 * *`. Creation and adjustment reviews say “On the first day of every month.”
Its successor advances calendar months in the captured timezone, never a fixed number of days.
A nonexistent first-day time skips that month; a repeated time runs only its first occurrence.
The additive `{hour: null, minute: 0..59, weekday: null}` schedule runs every local hour
at the reviewed minute through the same proposal, adjustment, cursor and delivery path.
Its successor is strictly after completion, with no replay of missed hours; repeated clock
times use the first occurrence and nonexistent slots are skipped, including half-hour clock changes.
Day-limited hourly variants, subhourly schedules, arbitrary monthly days and routine task
editing remain unavailable. Existing schedule definitions and hashes are unchanged, and
hourly metadata does not make native migration/import accept hourly routines. Source and connected tests for these added
cadences are separate from the earlier packaged lifecycle evidence below.
Managed Photon packaged routine acceptance is verified at `9d3646d0`; paired Telegram
packaged routine acceptance is verified at `6fb5f7f9`. Broader schedules and live verification remain excluded.

While an explicit add or close write remains uncertain, its source event stays durable and is
excluded from later model input and safe-reply coverage, allowing unrelated owner chat and reads
to continue. For an add with changed effect authority, isolation still requires an intact pending
original command with the same owner, account, channel, line and run; it performs no inspection,
write or acknowledgement. If that partition cannot be established, the thread remains held. A
second explicit mutation may remain held by the existing single-command boundary. If any interactive reply send becomes uncertain, its exact covered
inputs remain pending and excluded from later model input and reply coverage. New owner input
can proceed without replaying that send. `/new` can rotate the transcript while preserving the
held input, dispatch and receipt evidence; it cannot resolve or retry the uncertain send.

The maintained Linux composition failure fixture and its invocation are documented in
[test/fixtures/composed-runtime/README.md](test/fixtures/composed-runtime/README.md). Its lightweight
export-path regression runs in the ordinary Node test suite; full Docker checks are separate.


## Durable activation state

The private `POST /v1/activation-state` endpoint accepts only an empty object and returns the
loaded configured-channel proof together with `absent`, `pending`, or `acknowledged` journal
state. A fixed configured destination alone is `absent`; a verified owner DM creates the
obligation, and the receiver's exact event ACK supplies its durable completion timestamp.
After ACK, compact binding and obligation metadata survive raw inbound retention. A later
admitted owner event may require receipt repair and return the state to `pending`.

The Python client checks the same explicit rendered configuration, credential proof, project,
owner, account, line and destination. The bounded response contains no raw credentials or
message text. Acknowledged Node state still requires the receiver's current owner policy and
sole Photon receipt before use. Legacy, Telegram and self-host Photon constructors do not
synthesize this managed-Photon evidence. The endpoint does not install the routine authority
resolver or relax runtime readiness.


Self-host conversation source authority includes generations stored in the existing Google and
Granola credential records. Reconnect changes the revision even when revoke/regrant happens
between observations; refresh preserves the generation. Present unversioned credentials withhold
source authority until explicit reconnect. Managed mode retains its existing consent-ledger basis.
An external byte-for-byte credential restore is not detected by this credential-local scheme.
The experimental Photon chat tool `accepted_brief_detail` uses a separate future-only provenance
chain. The receiver binds saved composition text and immutable PNG hashes to the source revision
and actual Node-accepted route/payload receipt, then checks current source and acknowledged route
around each read. `latest:true` selects at most one eligible artifact; an exact artifact ID pages it
in at most 4,000 Unicode code points. Withheld or missing never means no historical brief existed.
Legacy/sidecar-only artifacts and unknown acceptance remain unavailable. The canonical accepted
outbox receipt must still exist: normal seven-day terminal receipt pruning also ends this access.

A trusted supervisor must stage the nonsecret expectation at
`$SOTTO_DATA/runtime/expected-channel.json` as the receiver UID; missing or changed evidence
withholds reads. No generic script executor or unattended brief tool is mounted. Fixed trusted
receiver procedures share its role; a future model-invocable general runner must be isolated from
receiver credentials and proof stores. 


Gallery delivery across process UIDs uses authenticated `POST /v1/send-gallery-bytes`. The receiver
reads exactly four ordered private renderer PNGs through bounded no-follow descriptors, then sends
canonical base64 bytes. Node checks the body/image bounds, names and PNG signature before hashing
the exact bytes into the existing gallery payload and durable dispatch receipt. The receiver's
artifact files remain private to its UID; Node needs no read access to that volume. The existing
path endpoint is retained for explicit standalone use. A contacted gallery with unknown acceptance
remains held for receipt reconciliation; this transfer introduces no automatic resend or second
artifact store. These are local fixture contracts until supported composed startup and live
provider acceptance are demonstrated.


## Attended Gmail reply drafts

Configured owner chat exposes `gmail_draft_propose({message_id,body})` and
`gmail_draft_save({})`. The receiver reads the actual selected Gmail message and resolves one
recipient, its thread, subject and reply headers; the model cannot supply an address or approval
metadata. This saves one threaded, single-recipient plain-text draft. Gmail read consent and a
separate draft grant are both required, with current owner-route and source-revision checks.
The receiver checks the source revision again immediately before a reviewed draft reaches Gmail.
If access changes after reservation but before contact, it records a stable no-effect refusal;
an uncertain provider contact remains held for inspection.

The owner receives the exact recipient, subject and full body before a distinct, immediately
subsequent clear yes can save it. Edits require another complete review. The entire review must fit
4000 UTF-16 units; oversized content is refused rather than truncated. Intervening input, `/new`,
`/stop`, uncertain review delivery, changed targets or stale permission cannot approve a save.
No email is sent, and these tools are absent from unattended runs.

Routine and Gmail confirmations preserve an adapter's exact reply target when available and
require it to address the accepted question in the same owner chat. Unbound confirmations retain
the immediate-next-event rule. Provider timestamps are retained as metadata, not compared with
local completion clocks. Shared pending-offer selectors are present, but general detached-offer
reading and consumption are not exposed through the Node owner chat yet.

A confirmed provider receipt produces “Saved your Gmail draft. Nothing was sent.” An uncertain
save stays held for exact receipt inspection; it never creates another draft automatically. The
prior accepted full review remains visible. The shared delayed-outcome policy now gives an immediate honest uncertainty notice without claiming that the draft was saved.
Unrelated safe chat remains available. Source tests and connected actual Mastra→receiver tests
with a synthetic Gmail provider verify the read/review/yes/save path, exact threaded MIME,
restart/replay, grant/target refusals and uncertain recovery. Packaged draft acceptance is pending;
live Gmail/OAuth/account writes are unverified. This does not enable Bridge sends, Gmail sends or
inbox changes, Calendar writes, attachments, CC/BCC, or legacy migration.


## Attended Google Contacts

Original People commands use `google-action contacts-search/get` to resolve an exact current
contact. `contacts-create/update/delete` produce a complete readable review; only a separate
owner yes permits `confirm-contacts`. The original Google People writer supports names, email
addresses, phone numbers, organizations, birthdays, addresses and biographies. Each supplied
category replaces its full current contents; omitted categories stay unchanged. Preserve entries
when adding another value. Current Contacts consent, owner route and source revision are checked.

Update pins the reviewed contact/version; delete checks that exact contact immediately before
requesting deletion, but that preflight is not an atomic conditional delete. Uncertain effects
remain held for inspection while unrelated safe chat continues. Complete reviews beyond the
existing reply capacity are refused without truncation or a write. Synthetic configured-runtime
checks establish original read/review/yes/writer and restart recovery; they do not prove live
account changes or universal autonomous selection.

## Known secondary Calendar events

For an event on a known secondary calendar, `calendar_get` and the existing move, RSVP,
detail-update and combined review tools accept `calendar_id` alongside `event_id`.
The original `google-action calendar-update` and `calendar-rsvp` commands accept
`--calendar ID`; the matching confirmation takes no replacement fields. The full review
shows the exact calendar ID, and the separate owner yes binds that calendar/event pair
through the original writer, conditional PATCH, fresh readback and receipt recovery.
RSVP comments are not mounted and are explicitly refused.

Omitting the selector retains the existing primary-calendar arguments and receipt hashes.
For typed mutations, omit `calendar_id` for primary; the command alias also normalizes
`--calendar primary` to omission. Calendar permissions and current source consent still
apply. This does not add calendar discovery, secondary-calendar listing, creation or
deletion. Unknown contacted effects remain held; recovery never retries the mutation.

## Attended Calendar rescheduling

`calendar_move_review({event_id,start,end})` reads one exact timed event on the selected
Calendar (primary when omitted) and shows its current and proposed times. `calendar_move_confirm({})` applies only the
accepted full review after a separate affirmative owner message. The model cannot supply approval
metadata, change the accepted calendar, or supply arbitrary event fields. All-day events and recurrence masters
are refused; an exact timed occurrence can be moved.

The original Schedule writer updates the same event, preserving guests, conferencing and other
fields. Its conditional request refuses an event changed since review. Current Calendar consent,
write capability, owner route and source revision are required. The review says guests will be
notified; it makes no unverified claim about availability. Edits require another complete review.

A confirmed provider result and exact-time readback precede a durable moved receipt. A lost
response is inspected using that receipt without repeating the update. A contact with an unknown
outcome remains held while unrelated safe chat can continue. Calendar creation has its own
review below; deletion is not exposed. Synthetic source and packaged verification are tracked in release
acceptance evidence; they do not authorize or prove a real Calendar write.


## Attended Calendar RSVP

`calendar_rsvp_review({event_id,response})` reads an exact selected Calendar invitation and presents
its title, times, attendee address, current/new response and notification intent.
`calendar_rsvp_confirm({})` applies that full accepted review only after a separate affirmative
owner message. Responses are accepted, declined or tentative. The move and RSVP confirmations
cannot approve each other's reviews. Timed and all-day invitations are supported; all-day end
dates remain exclusive. A recurring occurrence stays distinct from an explicitly reviewed
entire recurring series. A series response does not prove propagation to every exception.

The original gated RSVP writer resolves the actual self attendee, checks the reviewed event and
ETag again on its own fresh read, and changes only that attendee's response with a conditional
update. Ambiguous attendee identity, incomplete guest lists, changed events and already-equal
responses are refused before writing. Notifications are requested; delivery is not asserted.
Confirmed provider output and fresh readback precede the existing Calendar receipt. Recovery
returns committed receipts without reapplying; uncertain contacted effects remain held while
unrelated safe chat can continue. These synthetic checks do not authorize real invitation changes.


## Attended Calendar creation

`calendar_create_review` prepares a complete primary-calendar event review: title, start and
end with timezone offsets, the actual guest list, location, agenda and notification intent.
`calendar_create_confirm({})` requires a separate affirmative owner message bound to that
accepted review. The original Schedule skill requires real Calendar reads before proposing
available times and a resolved venue address before approval. Creation can use an explicit
full address or permitted Calendar evidence; ambiguous venues need clarification. General
venue search remains a separate gap. The adapter does not add an availability engine.

The original gated creation writer and packaged Google CLI perform the booking. An optional
stable event ID, derived from the admitted proposal, survives transport retries; ordinary
legacy callers retain their existing defaults. The reviewed owner address is checked before
the writer can add it to the guest list. Personal events without guests retain their original
behavior. The complete review is never truncated; changes require another review.

A matching provider result and readback precede the durable receipt and Calendar link.
Notifications are requested, not claimed delivered. A lost committed response is recovered
without creating again. An uncertain contacted creation stays held while unrelated safe chat
continues. This path covers the original timed, one-off primary-calendar creation fields;
all-day/recurring creation, conferencing and deletion are not added. Source, packaged and
live verification are recorded separately in acceptance evidence.

## Attended Calendar detail updates

`calendar_update_review({event_id,fields})` uses the original Schedule writer to update
selected `summary`, `location` and `description` fields. The complete before/after review
identifies the selected calendar and event, its time and whether it is one occurrence or an
entire recurring series. Timed and all-day events are supported. Omitted fields remain
unchanged; an explicit empty string clears a field. Only a distinct owner yes permits
`calendar_update_confirm({})` to apply the accepted review.

The original `google-action calendar-update --event-id ID --fields-json JSON` command
reaches this same review flow; `calendar-update-confirm` takes no flags. Time-only edits
use the existing move tools; strict time-only original `calendar-update` fields route to
that same move review, then empty `calendar-move-confirm` after a distinct yes. Combined time/detail changes on one timed event or exact
occurrence use `calendar_move_update_review` and, after distinct owner approval,
`calendar_move_update_confirm({})`. The same original command routes combined fields
when JSON includes both `start` and `end` as `{"dateTime":"ISO-with-offset"}` plus
at least one selected detail; `calendar-move-update-confirm` takes no flags. Its full
review shows old/new times and all selected old/new details before one conditional
PATCH. Series/all-day time changes, guest and conferencing edits are
outside this path; mixed requests are never silently split into writes.

Before applying, the receiver rechecks the current source grant and reconstructs the
review from the exact event. The original conditional PATCH sends only selected fields,
preserving unselected fields, guests, conferencing and recurrence; guest notifications
are requested. Combined updates verify both the reviewed times and selected details.
Exact readback precedes a committed receipt. Lost or uncertain effects remain held and
are inspected without repeating the PATCH. A review that cannot be delivered in full is
refused rather than truncated. These guarantees require source and packaged verification;
they do not establish a live Google write.

## Experimental typed Google reads

When the explicit Bridge receiver configuration is present, interactive chat may receive four
read-only Google tools: `gmail_search`, `gmail_get`, `calendar_list`, and `calendar_get`.
They reach only the fixed authenticated receiver route `/runtime/google-read`; inputs, projected
fields and results are strictly typed and bounded. Calendar list reads all readable calendars by
default, or the explicit selection saved in management Preferences, in a UTC window of at most
31 days. It returns at most 100 events with calendar/event identity and per-calendar coverage;
failed calendars and bounded continuation are reported as partial.
Gmail search is limited to 20 results. Attended Gmail drafts and the confirmed Calendar
reschedule, detail update, RSVP and creation flows above are the mounted Google write paths. Gmail sends/inbox changes,
Calendar deletion, Drive, and unattended Google tools are not included. Google Contacts uses the separate attended command family above. Unattended receiver procedures keep their existing Python-owned
Google/model paths.

The receiver requires a fixed-five profile and nonempty `SOTTO_HOME`; it never falls
back to another token root for this tool. Self-host reads require a current versioned token
generation. Managed reads additionally require the token generation to match the latest applied
Cloud bootstrap and current tenant grant. The receiver snapshots this authority before and after
each provider call; the Node tool independently checks the same revision and current grant before
and after, validates the strict projection, and discards results on change. A changed or unverifiable
source revision also causes the existing transcript guard to purge prior conversation history
before a later turn can reuse it. This is source revocation fencing, not a provider OAuth revoke.

The receiver caps each decompressed provider response at 256 KiB before JSON parsing and the
projected response at 64 KiB; the Node receiver client also caps the response body at 64 KiB. The
provider path has a 15-second monotonic work budget and per-request socket timeouts bounded by the
remaining budget. This is not a strict wall-clock deadline: DNS/connect behavior or token refresh may
outlast that budget. The Node client uses a 30-second request deadline. Empty and partial outcomes
remain distinct from failure. This experimental read path does not enable selected-runtime
readiness or establish live Google consent/provider availability.

For an offline check of an already-built image, run from `sotto-agent`:

```sh
sh runtime/sotto-chat/test/fixtures/packaged-image/run-packaged-smoke.sh IMAGE linux/arm64
```

Use `linux/amd64` for that architecture. The fixture requires the image receiver to match the
current checkout's SHA-256, imports actual packaged modules and checks Bridge-only authority
plus compiled activation contracts. It uses a read-only container with networking disabled,
synthetic state and no image pulls. See the fixture README for its scope.

The maintained [fixed-profile process fixture](../../smoke/fixed-cross/README.md) exercises
compiled Node with fake provider/model boundaries and the actual managed receiver in separate
UIDs. It covers both a manual composition and the actual native replacement helper. The latter
substitutes only Node's final entrypoint after canonical argv verification; this is synthetic
control-path evidence, not a deployed provider or production-entrypoint acceptance test.

A short unthreaded “yes” to a meeting-prep offer, or “no/not now” to a supported offer, can use the most recent unambiguous accepted message on the same owner route. The journal freezes that selection at fresh admission and requires original provider timestamps from both send and ingress; missing evidence, an intervening owner message, uncertain or overlapping sends cannot authorize it. A read-only preview preserves ordinary conversational answers. Local routine and Gmail reviews retain priority; source-loop “done/drop” and numeric choices still require explicit replies. This path is locally source/receiver tested; live behavior needs its separate package and owner check.
For a fresh, single, unquoted shared-Photon owner message whose local ordering is blocked, a recent accepted scheduled message permits one bounded provider-history read. A complete fifteen-minute window must contain the exact current owner ingress and exactly one immediately preceding outgoing message. That message must already be accepted in this route's journal, and accepting a meeting offer still requires the receiver to validate that exact unexpired offer and current calendar authority. Only identifiers/timestamps are frozen; provider-returned text is not persisted or sent to the model. For ordinary follow-ups, the existing context lookup can then include the exact journaled background body, subject to its original source revision, current authority and session boundary. This lets a request such as “shorter pls” refer to a delivered research answer instead of only its earlier queue acknowledgment. Once such an exact selection is frozen, the accepted-history projector imports the original scheduled text (at most 16,000 characters) once under a dispatch-derived message ID, original acceptance time and source revision, after checking the current configured route. Later turns can remember that delivered result and its source wording. The existing native history bounds, session exclusions, source purge and completed-projection checkpoints remain; no old result is resurrected after a purge. This supplies context, never action approval. Truncation, ambiguous timestamps, mismatched identities, unavailable reads, restarts and intervening owner input cannot add new proof. Existing uncertain deliveries stay held. Dedicated Photon and Telegram do not use this rescue. “Yes pls”, “full prep pls” and named “prep for Dana” forms use these same checks; a different named person cannot accept the offer.


This additive journal upgrade uses schema 7. Older schema-6 admissions and receipts do not gain continuation evidence; rollback must use an image compatible with schema 7 after a volume has been opened by this runtime.

The shared feedback skill can now be loaded through Mastra's native `skill` and
`skill_read` tools. Canonical skill names, bodies and bundled references are preserved;
all 17 indexed skills load, but that does not enable every script they describe.
Chat mounts `run_sotto_script` for the existing preference command family only:
show, sender/person/section mute and unmute, tone add/remove/clear, VIP/unVIP, and
nudge-budget fewer/no/more. It uses the same framework-neutral execution handler
as the existing typed preference tools, with the original owner admission, source
checks, one-write fence and inspect-only recovery. No shell or root launcher runs.
Results explicitly identify the receiver backend: bounded explicit preferences or
an immutable recorded/rejected delta, rather than CLI stdout. Temporary quieting
also exposes `snooze-nudges SPEC` and `unsnooze-nudges`, as described below. Audio
remains unavailable; absent removals can reject. Verification for each capability
is recorded separately rather than inferred from the presence of this adapter.

For managed and self-host Photon, the common `run_sotto_script` command router also
accepts `meeting-prep`: no arguments requests the existing sweep, while
`["--focus", "resolved person or meeting"]` requests focused preparation. The model
resolves this read-only subject from the owner request and accepted conversation context;
code does not replace it with a literal pronoun. Current owner admission, immutable arguments,
source consent, and the pipeline's unique upcoming-attendee/occurrence check still apply.
A bare accepted-offer confirmation retains its separate exact-occurrence path. The real loaded
meeting-prep skill guides the model; receiver-owned shared orchestration gathers,
researches, composes and persists through the existing scripts in a private
workspace. The command acknowledges only a durable queued receipt, with the actual
result delivered separately by the existing queue/outbox. Admission-response loss
recovers by inspection; unrelated chat remains available. `/stop` stops the chat
turn and does not cancel a committed background job. Telegram retains its existing
commands but this new meeting-prep command is unavailable there. Native skill →
real HTTP admission/queue and loss/reopen recovery are source-tested; packaged
execution and live provider behavior are separate verification.

The same Photon shared command boundary accepts `ask-research` with
`["--person", "exact owner words"]` or `["--company", "exact owner words"]`.
It loads the existing Ask skill and queues research/persistence followed by a
grounded answer from the existing stateless model path. The answer arrives separately
and does not inherit the attending chat's history. Company mode uses one grounded
company deep dive with saved novelty context, the original field/traction filters and
canonical company writer, without a person record. Only a bare domain supplied by the
owner can become a domain association; model-invented domains are not accepted.
Provider-reported ambiguity or unusable research leaves no company update. This is
provider-grounded identity evidence, not an independent identity verification.
Corrected About text stays protected by the original writer, and unavailable research
or persistence is disclosed. The existing deep-research disable setting is honored.
This is not arbitrary query parity. Existing exact arguments, one-write
fencing, queued ACK and inspect-only recovery remain unchanged. Native skill →
real receiver/queue admission and loss/reopen recovery are source-tested; full
pipeline packaged and live verification remain separate.

The declared research procedure passes its answer-only prompt through the existing
stateless read-only `/run` operation. It does not request a named runtime selector:
managed startup keeps that selector allowlist empty, and the agent already loads
the skill pack. Source checks, persistence checkpoints and delivery remain outside
this model call.

For these Photon owner skill requests, `queued` confirms admission, not a finished
result. The worker saves an answer before the existing outbox sends it. An eligible
terminal failure or expiry within the existing four-hour freshness window instead
queues one content-free outcome notice, subject to delivery holds and retry limits; if the original
delivery might already have been accepted, the notice says its arrival is
unconfirmed and does not resend the result. A completed proactive check may
intentionally say nothing. This applies to newly admitted jobs only: previously
failed jobs with no feedback metadata are not backfilled. Newly admitted feedback stores the real verified owner chat ID and route digest, then rechecks the current route before enqueue and provider contact. Old owner-name aliases without route proof remain withheld; accepted and uncertain sends retain their receipt fences. Ask research keeps its
per-stage `started` checkpoint and never recontacts an unconfirmed stage on an
automatic retry. Future worker diagnostics can identify only which of research,
persistence, or answer is unconfirmed; that label does not identify the cause.
Local receiver tests cover these outcomes, not live provider delivery.

The common command router also exposes `knowledge-query` to authenticated owner chat
with a current complete source revision, independently of private mutation capability.
It executes the existing canonical `knowledge_query.py` read inline: `--person`,
`--person-coverage`, `--editable-person`, `--topic`, and `--relevant-days` (1–365),
with unique flags and no input file paths. The same attending model receives the
actual bounded JSON and continues the loaded Ask skill; no research, graph migration,
write, background job, or extra answer model is started. Canonical memory remains
readable with empty fresh-source grants; history reads retain their separate consent.
Native skill loading and real receiver graph-read integration are source-tested;
packaged and live validation remain separate.

The common command `master-file` with `["accept-offer"]` accepts only the original
standing-rule procedure offered in an accepted owner message. The journal supplies
its exact reply target or eligible frozen continuation; the model supplies no rule,
section, or offer selector. A read-only match check leaves other offer kinds available
to their existing tools. Python appends the original rule to the existing master
file's Procedures section; the saved receipt produces the acknowledgement. Lost
responses recover by inspection, with unrelated safe chat available, and the next
shared composer prompt reads the saved rule. Exact and unthreaded native skill/real
receiver integration is source-tested; packaged and live verification are tracked
separately in acceptance evidence.

Direct owner-stated rules use `master-file` with
`["append", "--section", "Procedures", "--text", "exact owner words"]`.
This prepares a complete review without changing the master file. A separate
affirmative owner message can use `["confirm-procedure"]` to append that exact
rule through the original master-file writer. The rule must be a single line
from the admitted owner message, and the complete review must fit the channel;
neither is truncated. Confirmation is bound to the accepted review and current
owner thread, without requiring a connected Google source. The original file
limit remains in force: capacity refusals identify the largest section and ask
what to trim, without deleting anything. Durable receipts recover a lost response;
uncertain writes remain held instead of appending again. The next attended
generation reads the saved rule through the existing uncached master-context
reader. Source, packaged and live verification are recorded separately.

An explicit complete set of priorities uses `master-file` with
`["set", "--section", "Priorities", "--text", "exact owner lines"]`.
The original limit of one to three lines applies. The owner sees the complete
current and proposed sets before a separate affirmative message can use
`["confirm-priorities"]`. The original `master_file.set_section` writer replaces
only Priorities. A change to that section after review refuses the replacement;
concurrent changes to other sections are preserved. The full review must fit the
channel, and the existing file capacity limit still applies. No automatic trimming,
general priority append, or arbitrary section replacement is enabled. Recorded outcomes
recover by inspection after a lost response; uncertain writes are not repeated.

The original weekly command “prioritize staying in touch with <name>” authorizes
one addition directly. Use `master-file` with `["prioritize", "--text",
"Staying in touch with <exact name>"]`. The admitted owner message must be that
complete command, with the same exact name; a bare yes or source quotation is
insufficient. The original `master_file.add_priority` writer preserves existing
priorities, treats an existing entry as a duplicate, and refuses a fourth entry
without replacing or trimming anything. No second confirmation is added for
this specific original command. The same durable receipts and inspect-only
recovery apply.

Explicit owner-stated context uses the same command with
`["append", "--section", "About" or "People", "--text", "exact owner line"]`.
The review names the destination section and shows the complete line. A separate
affirmative message may use `["confirm-context", "--section", "About" or "People"]`
only for that same accepted review and section. The original append writer
preserves existing context and all other sections. Source-derived or inferred
facts do not grant this capability; the line must occur in the admitted owner
message. File capacity and full-review limits remain in force, and lost or
uncertain outcomes use the existing receipt and inspection behavior.

Each attended generation reads the current owner master file through the existing
private read boundary and receives the original shared standing-context framing.
This works in fresh chat memory after restart, without fresh connector grants or a
preference mutation capability. `run_sotto_script` with `master-file` and `["get"]`
returns the same turn's actual `{text, chars, cap, prompt}` read; no persistent cache,
write, or background job is created. If the read is unavailable, ordinary chat
continues with that limit stated to the model instead of remembered stale rules.
Native fresh-memory and real receiver integration are source-tested; packaged and
live verification are tracked separately in acceptance evidence.

The common read command `loops-query` accepts `[]` or `["--parked"]` for the original
Ask and People skills. It returns the shared ledger's `you_owe`, `waiting_on_them`,
counts, and optional parked rows after current source attribution and consent
filtering. Empty results cover only currently permitted attributable canonical
rows; parked reads do not restore anything. Calendar fields display a real ISO day
or unknown: legacy relative or invalid dates do not remove a task or block another
task's selection/readback. Original ledger values and identity/version proofs remain
unchanged, and an unknown deadline does not imply overdue. The owner-authored task tool is
unchanged. Native skill → real receiver reads, filtered counts, unchanged ledger,
and zero actions/jobs are source-tested; packaged and live evidence are separate.

After aggregate completion, an explicit “any test tasks left?” inquiry does not
print unrelated open obligations. If no manual tasks remain in the current permitted
list, the reply says no other tasks the user added are open in that checked list;
otherwise it states that the remaining tasks are not identified as tests. No test
category is inferred from task wording, and an explicit global open-list inquiry
still shows the currently permitted source and owner tasks.

The common `retune-scan` command accepts no arguments and runs the original stale-loop
scanner over the same currently permitted canonical rows. Original age, accepted-delivery
counts, overdue dates, direction and exhausted-chase suggestions determine what appears.
It also returns the original explicit settings and timezone; mute suggestions remain empty.
Unknown or revoked sources are withheld before labels or counts, and empty results do not
establish an all-source all-clear. Scanning changes no ledger or preference.

After the owner explicitly chooses one item, a fresh `loops-query` supplies its current
identity/version to the existing dismiss, snooze or keep writer. The original correction
receipt and readback determine what can be acknowledged; uncertain writes remain held for
inspection. One owner event still permits one correction, so same-message bulk cleanup is
not supported. `owner_stale_loops` remains the owner-authored-only fallback when complete
source authority is unavailable. Source, packaged and live verification are separate.

Source authority also projects the existing Granola connector grant separately
from Bridge and Google. Legacy responses without `connector_sources` grant no
connectors. Current consent and protected credential generation bind the revision,
so reconnects invalidate prior conversation source context. Canonical loop reads
include Granola obligations only while every source dependency is permitted;
this projection adds no transcript or historical meeting-reading command. Native
real-authority/loop integration is source-tested; packaged and live evidence remain
separate.

The existing `accepted_brief_detail` tool can discover retained accepted galleries
by owner-timezone date, literal text query, and brief/prep kind. Discovery covers
the last seven days; latest and exact-ID paging retain their existing behavior.
Multiple date/query matches return only eligible titles, dates, kinds and IDs;
the model asks the owner to choose before reading that exact gallery and following
its page offsets. Current source and route proofs still control eligibility, so
withheld candidates reveal no title or count. Native original Ask → real receiver
ambiguity → owner choice → exact paged answer is source-tested; packaged and live
verification are tracked separately.

The common `granola-history` command reads the existing Granola meeting pipeline in
attended chat: `[]` or unique `--days 1..90` (default 14), optionally `--meeting-id ID`.
Discovery returns metadata only; Ask can ask which meeting before retrieving that
exact occurrence’s original notes, recorded follow-through and bounded transcript
excerpt. Undated records retain their original uncertainty. It requires a current
Granola grant and matching source revision, creates no action or queued answer, and
cancellation closes the isolated read worker. Packaged and live verification are
tracked separately in acceptance evidence.

The common `web-read --url URL` command reads one ordinary public DNS URL token
from the current admitted owner message through the original shared provider ladder.
An ordinary question's trailing `?` may be omitted only when it is an empty query
delimiter; nonempty queries, fragments and encoded `%3F` remain exact. Original
owner text, selected arguments and receipt URLs are retained without rewriting.
With no URL in that message, it can read the single distinct URL in the exact accepted
Sotto reply that the owner explicitly replied to. The saved ingress and accepted
dispatch bind that quote; its source revision must match current complete authority.
Multiple links or an unavailable target require clarification. Any current URL takes
precedence. Local destinations, literal IPs and DocSend are outside this command.
It returns bounded page evidence into the same Ask conversation. Each owner event
binds one paid request, including its source revision (nullable only on the existing
current-message path). Quoted evidence is authenticated runtime journal evidence,
not an independent provider-history verification. Lost or uncertain responses are inspected, never fetched
again; unrelated safe chat continues. Browser cleanup stays in the owning Python
request even if the attending turn is cancelled. Packaged and live verification are
tracked separately in acceptance evidence.

The separate `docsend-fetch --url URL [--passcode VALUE] [--fresh]` command uses
original `docsend_fetch.py` for the email gate, extraction, cache and saved PDF.
An exact URL in the current owner message authorizes the normal cache-first read;
it uses the configured viewer email, never a model-selected identity. A fresh view
or a follow-up whose target is absent from the current message first presents the
exact deck and viewer email for a separate yes through `confirm-docsend`. A passcode
must come from the owner and is never echoed in the review. No view occurs during
that review. Email verification remains unsupported; there is no browser fallback.

DocSend results say whether the answer came from cache, and offer the authenticated
`/api/decks/<id>.pdf` path only if the original reader actually saved a PDF. The
existing web-action receipt holds an uncertain contacted view for inspection;
restart never silently opens the deck again. Current source authority and viewer
identity are rechecked during bounded HTTP and original model execution. DocSend
has separate elapsed-time and response limits; blocking system DNS resolution is not
a hard deadline guarantee. Ordinary web-read limits and identities are unchanged.
Source, actual-package and live verification remain separate acceptance gates.

The common `relationship-importance` command exposes the original People reader:
`[]` lists saved VIPs; `["--person", NAME]` explains one person. It applies the original
rolling activity window and explicit VIP choices to saved relationship evidence,
without gathering, writing, queuing work or granting permission to act. Ambiguous
names do not borrow a rank. This does not expose the relationship attention queue.
Packaged and live verification are tracked separately in acceptance evidence.

The `relationship-attention` command takes `[]` and reads the original saved
`attention_queue` through the existing knowledge-read authority checks. It returns
at most twelve ordered suggestions with only display name, type and reason. This
uses the retained canonical-memory contract, not proof of each row's past source
grants or current contact coverage. Freshness is always unknown: file modification
time can reflect unrelated history updates. Missing and empty saved queues remain
distinct, and neither proves nobody needs attention. For current advice, use the
original relationship pulse. Malformed or oversized data is unavailable rather
than reported empty. No gather, model call, action, job, write or new store is added.

The common `followup` command admits the existing recent or backlog follow-up pipeline only for an affirmative current owner request (for example, “who do I owe a reply?”). Both the journal and receiver refuse stale context, quotations, reported requests, negation and unbound “yes” before new work. If the Granola credential is absent, a meeting-promise request says the notes are not connected and does not suggest that retrying alone will fix it; incomplete or changed source authority keeps the generic source-unavailable refusal. The separate bound-offer route and already-contacted receipt recovery retain their original authority. Grounded commitments must be recorded before output; backlog answers use the post-write open-loop snapshot. This admission does not send email or bypass the separate draft review and save approval. Packaged and live verification are tracked separately in acceptance evidence.

The attended `proactive` command accepts no arguments and queues the original
urgent-check procedure. Successful admission completes silently; only an eligible
notification from the original scanner, composer and outbox is delivered. Quiet
hours, nudge preferences, interrupt budgets, candidate reservations and delivery
deduplication still apply. Empty results never produce an “all clear.” Rejected
admission remains a refusal, and uncertain admission is inspected without queuing
again. This uses the existing owner skill queue and independent Sotto records.

The loaded loops skill can apply an explicit owner correction through the shared `knowledge-edit` command: mark an exact current obligation done or dismissed, or move it between the owner's and the other person's lane. Existing canonical writers and the action receipt preserve one effect across uncertain responses. The reply reports current available counts only after a fresh protected readback; a failed or revoked readback reports the recorded correction without claiming current counts. Packaged and live verification are tracked separately in acceptance evidence.

The original Feedback identifier-teaching flow uses `knowledge-edit --op add-identifier
--slug <canonical_id> --identifier <exact owner span>`. First resolve the owner-named
person through the protected knowledge query and clarify ambiguity. This command prepares
an exact association review only. After a distinct owner yes, `--op confirm-identifier`
selects the accepted review and invokes the original identifier writer for that one person; it
does not create people, transfer identifiers, merge records or edit provider contacts.
The original normalization and collision rules apply. An already-known identifier is a
no-op on the graph and is acknowledged as already known. The existing preferences
receipt map records the exact owner request and readback; uncertain outcomes remain
held for inspection without another write. Fresh knowledge queries resolve the added
identifier through the original shared identity map. New legacy direct `add_identifier`
actions are rejected at both admission and the backend writer boundary; historical committed
receipts and uncertain actions remain inspectable without replay. This deliberately adds a
review and separate confirmation to teaching. It prevents a mistaken lookup command from
silently writing; it does not classify the owner's natural-language intent.

The original Feedback relationship commands use the same owner task channel.
`knowledge-edit --op relation-add|relation-remove --slug <canonical_id>
--other-slug <canonical_id> --type <original relation type>` prepares a full review;
an optional `--date` is available for adding. Both people must already exist and
resolve uniquely. A separate owner confirmation lets `--op confirm-relation` select
the exact accepted review from the journal. The original relationship writer updates
both people, with its existing graph journal and the existing owner receipt map.
Changed targets or source authority refuse before writing; uncertain writes stay held
for inspection. Duplicate relationships retain their original metadata, and the reply
distinguishes a recorded change from an already-present or already-absent relationship.
This does not expose person creation, merging, blanket removal, or implicit graph repair.


An explicit reply to a locally retained, accepted interactive message supplies that exact message as bounded contextual data for the attended conversation. It does not select the latest message, query provider history, or grant general permission to act on quoted values. The ordinary `web-read` path above can read its single distinct link when the current owner message has no URL and current complete source authority still matches. The current message must itself be an affirmative read request, and the quoted message must have been accepted within the last 15 minutes. Missing, conflicting, ambiguous or no-longer-authorized targets ask for clarification. The original owner text remains the admission proof; accepted-offer and separate-confirmation behavior is unchanged. Packaged and live verification are tracked separately in acceptance evidence.

A verified managed model spending refusal produces a fixed allowance notice through the existing durable owner reply path. Completed actions retain their factual acknowledgment, and uncertain actions remain held for inspection. The notice does not retry paid model work; an uncertain notice delivery is recovered without regenerating or duplicating it.

Photon text formatting uses the canonical library installed in the runtime image when the optional agent skill-pack path is omitted. An explicit skill-pack path is honored exactly; formatting failure still refuses before provider contact rather than sending raw text.

The installed runtime loads the canonical 17-skill pack when its optional path is omitted; configuration checking uses the same selection. Local runtimes without an explicit pack retain their empty skill list. Explicit paths remain strict, and a missing or malformed installed pack refuses startup.

The attended `triage-queue` command runs the original cross-channel queue builder over current consented Bridge messages (72 hours) and the original bounded Gmail `newer_than:1d` gather, including sent-message reconciliation. It reads message bodies without attachment downloads or paid conversion. Coverage reports partial, disabled and unavailable sources; groups have no draft address. Selecting an email uses a current exact Gmail message in the selected thread, then the existing full review and separate save approval. A saved draft is not a sent reply or a cleared item: a fresh queue read reports what still needs a response. Packaged and live verification are tracked separately in acceptance evidence.

The attended `usefulness-feedback` command connects the original Feedback skill to
retained delivered briefs: `list`, or `useful` / `not_useful` with `--reference REF`
and an optional unique verbatim `--excerpt` of at most 320 characters. The adapter
binds a selection from the current authorized list to its archive version and
source revision. A clear owner rating can be recorded directly; ambiguous feedback
needs clarification. A rating does not mute a sender, close a loop, create a rule
or authorize a message. Optional `--reason` is length-checked and discarded.

The original outcomes writer stores the reference, rating and hashed item locator.
Original brief, relevance and drafting prompts resolve bounded examples from the
retained archive; they do not receive a copied archive from the outcome record.
Later ratings replace the same item's earlier decision in the original reader.
The existing preference receipt and action journal recover committed results and
hold uncertain writes without appending again. Managed source-revocation behavior,
packaged verification and live verification are tracked separately in acceptance
evidence; exposing examples to a prompt is not a guarantee of model compliance.

### Fresh Gmail messages

The original `google-action gmail-send --to EMAIL [--subject SUBJECT] --body BODY`
command and `gmail_new_send_review` present the same complete From/To/Subject/body
review. They do not send immediately. A distinct subsequent owner yes allows
`gmail-send-confirm` (no flags) or `gmail_send_confirm` (empty input). Resolve the
recipient from the owner's request or protected sources; clarify ambiguity rather
than invent an address. Replies retain `gmail_send_review` with the actual message ID.

Fresh sends reuse the original Google MIME writer and single-attempt send transport,
without reply headers or an invented thread ID. The receipt requires matching SENT
message readback; Gmail supplies the resulting thread ID and may group the message
with an existing thread. An uncertain result remains held and is never sent again
automatically. Draft-save approval does not authorize sending, and scheduled work
cannot use this attended path. No CC/BCC, attachment or sender-alias inputs are added.

### Offered Gmail reply choices

The original Draft-reply skill uses `gmail_draft_pair` to present both Accept and
Decline for an actual Gmail message. The receiver resolves the recipient and thread,
freezes the two original offered-draft rows, and returns the complete visible pair.
Only acceptance of that exact owner response permits the original offered-row
writer to record them. This needs Gmail read access, not draft or send permission.
It creates neither an email nor a link and records no choice on the owner's behalf.

Selecting a choice produces the existing full single-draft review. Its edits and
confirmed outcome retain the original offered timestamp and key. Save/send approval
remains a distinct later owner event. Local recording can recover from the saved
accepted response without delivering it again; uncertain provider sends retain their
existing hold. Recording failure does not block unrelated safe chat. This is an
extension of the existing Gmail ledger and delivery journal, not a separate store.
Synthetic package and live verification are recorded separately in acceptance evidence.

### Saved company context

`knowledge-query --company NAME_OR_DOMAIN` exposes the original company reader in
attended Ask. Names, aliases and domains resolve to the same saved record. Chat reads
the same 3,000-character pages as the MCP, including About, news and saved context.
Use the returned `next_cursor` with `--company-page` when more evidence is needed.
A changed record invalidates the cursor so pages from different versions stay separate. A missing
match, ambiguous selector and unreadable store have distinct results. Current
source authority and the existing read-only graph-lock boundary still apply.
Fresh company research uses `ask-research --company SELECTED_COMPANY` and remains separate from this saved-memory read.
A question about a focused text prep can reuse its company when the accepted delivery, focus domain and
unique saved company agree. This uses the existing company research worker. Other research selections
come from the current owner message or accepted owner context under
the existing fifteen-minute route/session/source bounds. An elliptical question such as “Any
competitors to know?” can continue the immediately preceding owner request; it need not contain
a prescribed pronoun. A bare domain or homepage immediately following an owner read request can
supply its missing company identity. The journal constructs the `prior_request` evidence and the
receiver checks the current owner-supplied domain and previous request before queueing. Assistant
text cannot supply that identity. Cancellation, unrelated intervening turns and expired evidence
cannot carry a request forward. The saved company lookup also returns bounded `company_identity` metadata (slug, name,
domain and last research date) so chat does not have to rediscover a stored website. This metadata
does not grant external-read authority. Company records and their write policy are unchanged.

## Standalone provider adapters

`src/agent/selfhost-chat-provider.ts` supplies `createSelfHostChatModel(connection)` for
Google, OpenAI Chat Completions, Anthropic Messages, and Qwen hosted Chat Completions. The
returned AI SDK model can be supplied to the existing Mastra runtime. Credentials and model
IDs are explicit; origin, route, authorization header and model are bound at construction.
Qwen requires an explicit deployment region and, outside the US endpoint, workspace ID.
It does not accept arbitrary endpoints or discover environment credentials.

This factory is **not wired into bootstrap or the private operator configuration yet**.
The existing deployed self-host path remains Google-only. Wiring follows the active
owner-runtime PRs so setup, Python background work, model admission and usage receipts
move together. Managed Cloud still uses its existing proxy. Callers must set `maxRetries: 0`,
provide deadlines, and perform workload admission; changing a transport is not qualification
of every tool, structured-output schema, image capability or context window.

The recommended preset will reuse one Google key across chat, image/audio understanding and
speech generation. [Hosted speech](../hosted-speech/README.md) has separate transcription and
synthesis model IDs and independently supports Google and OpenAI. Claude has no speech role
in this implementation. Qwen audio uses a different API and is not implemented here. Keep
transcription provider selection visible when choosing a text-only chat model. No automatic
cross-provider failover is introduced.

The user's 1M target refers to context **tokens**, not parameters. Before enabling an alternative
model, record its verified input/output limits and reserve output/tool space under Sotto's
existing model-work admission. A provider family name alone never grants a 1M budget. Test
chat and tool schemas, the actual brief workload, image/DocSend input, audio transcription,
cancellation, accounting and failure behavior against the selected model. These source tests
validate wire contracts with synthetic fetch responses, not real model quality or account access.

Provider packages are exact pins in the existing npm lock and existing Dependabot model-runtime
group. Update the SDK and adapter fixtures together; do not silently upgrade selected model IDs.
Current Qwen regional routes follow the [official compatibility reference](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope).

The Gmail provider-schema contract captures real SDK request bodies and checks managed
OpenAI-compatible bodies against the production Python proxy settlement predicate. A local HTTP
fixture exercises a tool call and continuation; unsupported request fields remain ineligible for
receipt settlement. Run this test with the pinned Python development environment on `PATH`.
This Gmail schema check does not change the channel; the current Spectrum alignment is described below.

## Uncertain owner requests

`delayed-outcome-policy.ts` contains only notice wording, allowed proof status, inspection cadence and notice-attempt data for the 17 supported action kinds. Existing typed readers and settlers still validate each receipt; the table cannot authorize an effect. The original uncertain request gets a fixed honest notice without another message. One unrefed coordinator wakeup runs at the existing one-minute cadence while a relevant action remains unknown or its final reply awaits redelivery, serializes through existing thread queues and inspects saved proof without repeating the action or calling the model for held-only input. A validated late result receives a distinct final acknowledgement. If that final reply is definitely not delivered (rejected, or cancelled before contact), the wakeup retries it up to the policy attempt bound; an accepted, unknown or dispatching final reply is never resent. The next owner message also drains such a reply and is then answered itself. `/stop`, shutdown and maintenance stop the wakeup as appropriate; unknown delivery receipts are never resent. Unsupported rejection proofs remain held. Local contracts are recorded in the behavior inventory; live verification is separate.


Research subjects and ordinary public URLs come from the current owner request or, for a follow-up
such as “their website” or “her background”, from the owner's own earlier text in the same session.
`acceptedReadContexts` in `src/journal/index.ts` checks at most 32 preceding inbound rows on the
same thread received within 15 minutes (`contextWindowMs` 900000 in `src/tools/read-policy.json`),
each with an accepted reply on the matching route under the current source revision. Only
owner-written text supplies a subject: assistant prose and background messages never grant research
or URL-read permission, although they remain available to native memory for recall and drafting.
`/new` or a cancellation (“nvm”, “scratch that”, “don't …”) cuts off earlier context; session
rotation, rejected/uncertain delivery and source revocation also exclude it. Meeting-prep yes
replies keep the separate receipt-bound offer path. A native quoted reply stays pinned to its exact
target, without falling back to another message. The model must clarify genuinely ambiguous
references; a phrase grammar does not decide which entity the owner meant.

Admission needs a current affirmative request. `src/tools/read-admission.ts` and
`src/tools/read-policy.json`, shared by Node and the receiver's Python
(`../trigger-receiver/runtime_read_admission.py`), refuse negated, reported (“she said…”) and
personal-recall (“how was lunch with Dana?”) messages. A follow-up that relies on earlier context
must also refer back to it or immediately follow that owner message (“it”, “their”, “more”), so a bare yes/ok cannot select a target.
Research subjects must be short names, not sentences or street addresses. The receiver
(`../trigger-receiver/runtime_owner_skill_request.py`) rechecks the current request, lookup
identity and source revision, plus thread and freshness for research from earlier context. It does
not independently verify runtime delivery receipts; the runtime owns provenance. Company homepage
retrieval uses the existing URL reader and research worker. No new model call runs merely to
remember a topic. Existing contacted receipts remain inspectable and uncertain operations are never
replayed. Calendar/contact changes, draft saves/sends and DocSend approvals retain their separate
effect-specific confirmation paths. Remembered text is context, not consent.

Actual-model conversation acceptance is documented in [evals/README.md](evals/README.md).
It uses this runtime and the real receiver/workers with isolated source fixtures and captured
transport. It proves runtime journeys independently of Photon, not provider routing or device display.

Explicit multi-loop completion accepts a unique current contact's possessive task phrase
(for example “Dana’s lease reply and Alex’s family photo plans are both done. Close those two.”).
The complete owner phrase is preserved. Descriptive words must occur in that current row's
summary/ask, allowing simple plurals; contact names are never matched fuzzily and multiple
active rows for one person still require clarification. The authenticated loop reader includes
a bounded ask for this check; source, version and identity are checked again by the writer.
Bare “those two are done” is not a bound selection and still requires the task names.

Photon adapter 3.2.0 and the gallery/presence helpers share Spectrum 12.14.0 through
adapter-scoped dependency overrides. Both the send client and bounded history reader resolve
Advanced iMessage 2.4.0's explicit `createGrpcClient` API. The installed RPC/signal fixtures
resolve from the actual adapter dependency chain; `photon-adapter-contract.test.ts` additionally
checks the real adapter send path over trusted loopback TLS, and the signal fixture verifies
application cleanup completes before exit; the history fixture checks timestamp,
owner and pagination evidence over loopback. The adapter still declares Spectrum 10 upstream,
so upgrades must retain clean-install package-identity and transport contract checks.
The adapter does not forward Spectrum's heartbeat observer; this upgrade adds no heartbeat
health claim. Existing ownership, uncertain-send and recovery rules remain unchanged.

The shared Photon text formatter selects an executable Python from fixed system locations:
`/usr/bin/python3` on macOS; `/usr/local/bin/python3` then `/usr/bin/python3` on Linux. It never
searches `PATH` or accepts a model-selected executable. The child retains isolated mode, an empty
environment, its five-second timeout and byte bounds. Missing Python or a failed formatter refuses
delivery before provider contact; it does not retry with another interpreter after execution.


### Accepted-text shortening and current work admission

A one-off shortening request receives the exact previous provider-accepted reply from the same
conversation/session, or its explicitly quoted accepted reply. The runtime rechecks source
authority and disables tools/skills for that edit; it does not discover or fetch source content
again. Missing/stale text asks for clarification. The offline latency benchmark covers the
first shortening after a cold start as well as subsequent turns through provider acceptance.

The journal and receiver both require current affirmative owner intent before admitting prep
or a brief. Already committed or uncertain actions retain their immutable recovery proofs.
A model-triggered brief acknowledgement shares the normal durable reply with any already-generated
independent answer, preserving that answer's source proof.
