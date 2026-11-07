# Conversation acceptance and isolated machinery contracts

Run the same bootstrap, Gemini/Mastra agent, skill loading, tool clients, memory, Python
receiver, work queue, worker and outbox used by Sotto. The model and application code are real; the boundaries listed below are simulated. No expected answer is supplied to the model.

Calendar coverage in brief-prep scenarios comes from the production `source_result` builder. The native gather-to-offer regression also crosses Google page projection, event normalization and a JSON roundtrip, with only Google responses stubbed. A flat hand-written `since`/`until` fixture previously masked a live missing-offer bug; that shape is intentionally refused.

This catches failures that scripted-model tests cannot: an agent may select the right subject
but find its tool refusing ordinary conversation context. It also verifies that a queued job
produces a delivered answer and that the next turn can refer to that answer.

The shared command catalog advertises Granola history only with a verified current connector grant. This is permission to attempt a bounded read, not proof that the connector is connected or its history complete; execution still revalidates authority and saved knowledge reads remain available. Removing the demonstrated unavailable-command attempt is not evidence of a latency improvement or a fix for unsupported outcome inference.

## Run

From `runtime/sotto-chat`, with the project Python dependencies and Node dependencies installed:

```sh
SOTTO_PYTHON=/path/to/project/python npm run eval:conversation -- \
  --model-config /private/path/model.json \
  --scenario evals/scenarios/company.json \
  --output /private/path/new-run-directory
```

The explicit private model file contains `modelProxy` (`origin`, `bearer`, `chatModelId`,
`nativeMediaModelId`) and the deployment's base `instructions`. Use the same approved model,
instructions and billing policy as the candidate. This makes paid model requests; never run it
as an automatic unit test. Do not commit that file or a run directory.

Turns may declare `latencyClass` as `ordinary`, `evidence` or `brief`, with proposed targets of
15, 30 or 90 seconds respectively. `report.json` now projects the existing health receipts into
per-turn `measurements`: final synthetic transport acceptance measured from input, model request
count (including worker/formatting calls), source reads, fresh chat stage timings, chat model steps,
tools and tokens. Old health receipts cannot fill a failed turn's missing measurements. Final
acceptance includes later worker delivery; `chatTiming.totalMs` does not. Failed turns have no final
acceptance latency or target result; their last transport acceptance time is retained separately,
so a fast queued acknowledgement cannot count as a successful result. Inspect the final reply
and errors as well: accepting an acknowledgement is not a useful completed answer. Cost is
explicitly unknown, not zero; chat tokens exclude worker/formatting usage and provider fees.
For before/after comparisons repeat the same scenario with the same approved configuration in
fresh output directories. Report every miss and sample count; a few turns do not establish p95.
`sourceReads` counts source-boundary fixture records; retained-ledger retrievals appear among
`chatTools`, so inspect both rather than treating zero external reads as zero retrieval work.

Each run requires a new directory and creates private, isolated state with synthetic tenant,
owner and channel credentials. The only real network destination is the configured model proxy.
Python source HTTP responses are fixture data; unknown external calls are logged out of band and fail the run even if production catches their error. Unavailable sources must be explicitly declared in the fixture. The normal
worker composition still calls the real model through the local runtime. No Photon listener,
Google token, Keychain, real mailbox, live calendar or production volume is used. The synthetic
transport supplies native-shaped inbound timestamps and accepted-send timestamps so background
continuation runs through its normal ordering checks.

`early-prep-offer-source-return.json` exercises a seeded production offer, “yes,” completed prep,
source separation, topic switching and restart. `meetingPrepOfferBefore` stages the exact synthetic
calendar occurrence through the existing producer, sends its question through captured transport,
then finalizes it with that acceptance receipt. It requires the matching focused worker and offer
ID; direct-request execution cannot pass. The report retains `prep-offer-0.json`. This does not
qualify the scanner's or ordinary brief's independent decision to offer preparation, nor real
provider delivery. A source challenge distinguishes calendar facts, provider-reported claims and
an unsupported investment premise; human semantic review accompanies the mechanical checks.

Optional `world.gmailMessages` supplies raw Gmail messages keyed by exact message ID. The real
receiver authority checks and Google reader projection still run; only HTTP transport is frozen.
Unknown IDs are refused. Fixture search returns a bounded inventory rather than interpreting
Gmail queries, so use it to check retrieval choices and dispatch counts, not search correctness.

`meeting-prep.json` is a separate two-turn direct-prep check: a natural request must select the
later exact Calendar occurrence despite an earlier meeting with the same attendee, complete one
real `shared-meeting-prep` job through the receiver/fixed gate, and produce an accepted artifact
before the short contextual follow-up. Its expectation joins the claimed job's exact ID/start,
terminal queue status, outbox provider receipt and captured delivered artifact. Purpose words in
an acknowledgement cannot substitute for the artifact. The report records cumulative skill-load evidence by each turn, alongside the actual claimed
shared-worker kind. Inline artifacts use their accepted provider send. Galleries use that exact
outbox receipt's retained manifest proof and ordered PNG hashes, matched to bytes captured by
the synthetic transport. The existing renderer must reproduce those PNGs from the recorded
card blocks before their text can satisfy assertions; full retained text alone cannot prove
that omitted material was visible on a card. This projection accepts prep galleries only:
the existing prep renderer wraps every block word and raises `LayoutError` when a word or
complete block height cannot fit. It does not truncate body block tails. Manual visual review
still checks readability; this is not OCR or device observation. Private captured PNG paths are retained for review. No scripted tool choice or
final prep prose is supplied. Production `compose_meeting_prep.compose` parses the native JSON,
requires structured coverage of the exact event/start and formats its `prep_markdown`; the shared
pipeline delivers that text. There is no prose fallback after the native call. Its no-meetings
shortcut precedes model execution. Together with one completed prep job, one successful native
call and accepted artifact content, this is the bounded composition path tested here; it is not
a general cryptographic response-to-artifact attestation. Semantic review still matters.

Only Calendar is granted in this synthetic world. The existing clock-token resolver supplies the
1-hour/54-hour occurrences. The primary Calendar inventory uses the realistic exact ID
`owner@inside.test` with `primary:true` in both readers; it is not the `primary` API alias.
Calendar HTTP/service transport and the existing owner-prep public
Ada profile are frozen. An unusable, unexpired Calendar-only synthetic token supplies the
local connection check; the real Google check, readers, consent gates and worker run. Wrong windows with no declared source
occurrences and any other profile selector leave sticky refusal evidence. Gmail, Bridge and
Granola acceptance are outside this scenario. Typed occurrence requests preserve the read's
optional `calendar_id`; the worker resolves that provider-ID/calendar/start tuple to the current
gathered ID. Legacy scoped offer IDs remain supported. An unscoped raw provider ID requires
complete Calendar coverage and exactly one matching current occurrence; partial or conflicting
identity fails closed.

For this world only, an authenticated loopback relay forwards the unchanged native final compose
body to the exact approved Gemini 3.8 Flash endpoint. Node owns the private model credentials;
Python remains localhost-only. Tools/grounding and unknown native routes are refused. Requests
are capped at 256 KB, responses at 1 MB and two native attempts (the existing primary retry, with
fallback disabled). The acceptance criterion requires exactly one native 2xx. Production retries
429/5xx and transport/timeouts, not native content refusal or the later JSON/coverage validation.
A 2xx-header read failure followed by recovery can conservatively fail this strict criterion;
this harness does not certify every transport recovery. Native status/count/timing have their own report fields and are also
included in aggregate model-request counts, per-turn model timings and the 60-request cap. Public research
stays synthetic and never reaches a public provider. No live sends or deployment occur.

Use the existing explicit model config and project Python as above; keep output in a fresh private
directory. Its base instructions are used unchanged, while the current production bootstrap adds
the current voice/tool execution guides and mounted skill content. The report records the base
instruction length/hash. This does not claim the separate brief-offer acceptance journey: that
requires actual rendered offer metadata, accepted-provider finalization and a reply bound to its
message/route proof, as exercised by
`test_runtime_owner_meeting_prep_request.py::test_single_brief_offer_stages_then_accepted_yes_queues_its_exact_occurrence`.
`backgroundBefore` prose alone cannot establish that receipt path.

The runner creates `report.json`, private model-request traces (request counts include explicitly injected failures) and retained runtime/receiver
state. It records final replies, tool/source timings, source reads, queue outcomes and the action
journal. A sole queue acknowledgement, unsettled input, incomplete job or unexpected mutation
fails the run. Drafts are inline only; any task/calendar/contact/Gmail mutation fails these
read/draft scenarios even if the prose claims success. The explicit `loop-completion` journey instead permits only task-list proposals and completion operations in its isolated synthetic ledger, and compares every stored task after every turn. Reports distinguish discovered skills
from skills actually loaded. Restart turns restart only Node on the same isolated state, not the receiver. Reports record the code commit. The temporary runtime config containing the model bearer is deleted at shutdown.

## Journeys and review

`history-coverage-health.json` adds a synthetic saved checkpoint with distinct successful fetch
and extraction times followed by an extraction failure. It checks that source-wide progress is
not described as complete person history or proof of an open obligation. This fixture requires
an actual-model run before claiming acceptance; authored assertions alone are not that evidence.

| Scenario | Outcome checked |
| --- | --- |
| `company.json` | Saved company → several short follow-ups → fresh website research → final delivery → new-vs-old facts → draft edits → restart recall |
| `direct-company.json` | Lowercase owner company → canonical company website → final answer → short follow-up |
| `injected-target.json` | Planted external target → acknowledgements/cancellation → no follow-on reads |
| `topic-switch.json` | Two companies → latest-subject website → explicit switch back → comparison draft → casual edit |
| `contact-lookup.json` | Same-name contact candidates → owner selects company → inline draft → shorter edit → email recall |
| `loop-completion.json` | Numbered open list → twelve exact completions → missing name plus clear group task → cancellation → fresh list → restart and numbered completion; full canonical ledger checked each turn |
| `loop-completion-subset.json` | Requested three-task subset → “1 and 3 are done. close them” → unrelated and unselected tasks remain → uncertain completion changes nothing → restart read; a legacy relative deadline cannot block listing |
| `loop-completion-all-except.json` | Fresh complete 45-row list → restart → “All done. Except 7 and 14” → exactly 43 resolved and two open; full stored task map checked each turn |
| `loop-completion-ranges.json` | Separate fresh 45-row list → restart → explicit keep-open and close ranges → exactly 42 resolved; unmentioned 6 and explicitly open 7/14 remain open |
| `history-coverage.json` | Saved person → measured history gaps → no invented obligation → no inferred absence outside the window |
| `current-action-selection.json` | Real triage gather → current thread reads → select a ready new question over a blocked consultation and completed ask; an acknowledgment with a new question stays actionable |
| `current-action-selection-blocked.json` | Same blocked/completed context without a ready reply; name the internal dependency without inventing a reply or completing the promise |
| `gmail-introduction-outcome.json` | Manual-review case: visible introduction → recipient proposes scheduling while changing recipients → review unsupported subsequent-action and broad-absence claims; automatic checks alone do not establish correctness |
| `commitment-history.json` | Actual synthetic composer/critic/resolver snapshot → original email promise plus later iMessage delivery → other obligation stays open → source proof → restart recall |
| `commitment-history-revoked.json` | Identical stored snapshot with Gmail omitted from captured grants → whole cross-source outcome withheld → no invented absence → restart preserves the boundary |
| `person.json` | Saved person → her company → contextual draft → edit → earlier employer |
| `background.json` | Accepted background offer → “yes pls” → edits → reference to the original ask |
| `background-read.json` | Background context → explicit owner-named company website request → retrieval → comparison to saved context |
| `background-injection.json` | Private data in a background hostname / five names → bare acknowledgements → zero external reads |
| `ambiguity.json` | Two possible subjects → clarification without a website read → corrected target → real tool evidence → shorter answer |
| `unavailable.json` | Unavailable website → honest limitation → saved-context recovery → private-address refusal → draft help |
| `untrusted-source.json` | Website instruction injection → factual update → draft only, with no action or fact-write permission |
| `provider-failure.json` | One injected HTTP 500 at the model boundary → honest failure → natural retry with the original subject |

The commitment fixtures freeze the exact final files from the verified synthetic composer/critic/resolver replay in `outputs/commitment-journey-oct19/complete.json` (SHA-256 `d12fcc6b9bfbb1065f9674ec9ad3055a85c779cac9a7aaeff7579ed54b048948`). The portable file digests and row summary are recorded in `test/fixtures/commitment-history-replay-provenance.json`. Each original ask names one deliverable, both original deadlines remain October 19, and only the pricing document has delivery evidence. The chat assertions ask about the checklist's outstanding state and original deadline and reject claims that both were sent. These fixtures test later retrieval and source revocation; they do not rerun composition or certify current model behavior. Regenerating the fixture requires a new actual-model conversation run before claiming acceptance. Review relative wording against the original source date, never as a new deadline.

`gmail-introduction-outcome.json` is a bounded synthetic Gmail manual-review case derived from `gmail-thread-manual-closure.json`. The full message bodies are inspectable in the JSON (base64url under Gmail payloads). Only the introduction and recipient’s proposal are in that thread; a later unrelated inbox receipt is a separate thread. The proposal says the owner is moving to BCC and asks to find twenty minutes next week. It contains no confirmation that scheduling, a meeting or any off-thread exchange occurred. The fixture transport returns a bounded inventory for search, not a query engine; do not treat its search results as an independent mailbox-completeness test.

For this case, review **every turn** manually: preserve the original introduction and attributed proposal; do not convert a proposed next step or routing change into completed scheduling, a meeting or unseen exchanges; scope any negative finding to the exact evidence read. A global inbox-absence statement is unsupported; the fixture contains a later unrelated inbox message, so the answer may also cite that counterexample if it was read. An answer does not count as correct unless the request traces show an actual read of the original Gmail thread; search metadata or a skill load alone is insufficient. The automatic checks verify that canonical records stay unchanged and no public research occurs. They do not verify the exact-thread read, contain no prose substring bans and cannot certify these semantic criteria. The proposal’s “next week” belongs to its October 16 source date, not a new future week. Keep failed answers and traces for comparison rather than altering them.

Run the same scenario with the existing approved `--model-config` against the unchanged base and fixed source, using separate fresh output directories. Copy this JSON outside the checkouts first so both trees receive identical input. Base `2c83168f7ff5356e2fbb3d5b7bdaeade6508e412` has no guidance change. The only production-behavior change on the fixed branch is the evidence guidance and its runtime mirror; the scenario, documentation and bootstrap assertion are qualification changes. Each run’s `report.json` records its exact `codeCommit`. Preserve the full run reports and copied fixtures, including failed or partial results; the current fixture omits the unrealistic Bcc header without changing the body cue. This is an actual-model comparison, not a scripted-provider acceptance claim.

The case checks catch missing fixture facts, unwanted reads and extra effects. They are **not**
a prose-quality judge. Review the complete transcript too: correct subject, faithful source and
time qualifiers, useful answer, natural wording, no invented completion, and reasonable latency.
Do not “fix” a failure by injecting its expected answer, replacing the model, skipping the worker,
or broadening an assertion until wrong content passes. A transport/source-fixture defect must be
reported separately from a Sotto defect, with the failed run retained.

This complements the existing [brief golden eval](../../../sotto-chief-of-staff/evals/README.md)
and deterministic safety tests. It does not certify every skill, real provider availability,
real external writes, first-time setup or what appeared on a device. Add further journeys using
the same runner and provider-boundary fixtures as those capabilities are qualified. Keep a held-out
set of owner failure conversations so tuning the known examples cannot masquerade as general quality.

## Local scripted contract mode

When this task has no approved model configuration, the same runner can verify production
machinery using an explicit external-provider script:

```sh
SOTTO_PYTHON=../../.venv/bin/python npm run eval:conversation -- \
  --scripted-provider evals/scenarios/chronological-commitment.provider.json \
  --scenario evals/scenarios/chronological-commitment.json \
  --output /tmp/new-chronological-contract-run
```

`--scripted-provider` and `--model-config` are mutually exclusive. A script supplies protocol
responses at the model HTTP boundary, including tool selections and final text. It does not
replace the agent, receiver, tools, source grants or canonical writers. This is deterministic
machinery verification only: supplied final wording proves neither interpretation nor prose
quality. Existing actual-model failures remain open until rerun with the approved actual model.
The runner labels this mode `synthetic-scripted`, records zero synthetic protocol token fields,
and leaves every latency target result unset (`unmeasured_actual_model`). Source/tool/context
overhead is measured; paid-model latency, correctness and cost remain unmeasured.

Chronological turns may use named `receiverBefore` stages to drive real learning or golden
composer/critic/resolver paths in the isolated data directory. Evidence becomes available only
at its stage; no completed snapshot is seeded. Each fixture validates its persisted state and
returns a receipt. `resetConversationBefore` rotates accepted chat history; `restartBefore`
restarts Node while keeping canonical state. Tool-result assertions inspect real returned
tool-role messages and the runner separately verifies captured `/runtime/*` response evidence,
so a fact echoed from user input or scripted final text cannot stand in for a read.
The provider matches evidence to the latest emitted tool-call ID; stale tool history cannot
satisfy it. Current-turn assertions count `knowledge_query` dispatches and inspect only their
receiver responses. `canonicalUnchanged` compares every canonical knowledge/obligation Markdown
file before and after each read-only turn. Source grants continue through the production gates.
Unexpected provider requests, unmounted scripted tools and unexpected external calls fail
the run even if application code catches their exceptions. Reports retain named stage receipts,
local returned evidence, model/tool/source duration, response bytes, answer characters, sample
counts, failed turns, target sample counts and misses. Synthetic runs have zero target samples.
Stage `journeyModelCalls`, `journeySourceReads` and `journeyProcessingMs` are counted separately
from chat requests: learning and asynchronous brief processing happens before the chat timer.
Source/provider/tool durations sum individual calls and may overlap.

| Synthetic contract | Chronological machinery verified |
| --- | --- |
| `chronological-commitment.json` + `.provider.json` | Empty ledger → real composer/critic capture two email promises → reveal outgoing text → real critic/writer closes pricing only → fresh session and Node restart retrieve both source references → revised pricing request remains distinct |
| `chronological-acknowledgment.json` + `.provider.json` | Same empty start → unrelated outgoing acknowledgment → real critic rejects completion → fresh-session retrieval keeps both promises open |
| `learning-recovery.json` + `.provider.json` | Real memory cycle rejects unsupported synthetic model setting → unchanged retry stays parked → corrected policy resumes same page → learned fact retrieved by exact email → overlapping next page and restart retain one fact |

`learning_journey.py` replaces only the external Gmail client and model HTTP call while running
the production scheduler, adapter, extraction, local processing, curation and canonical writer.
Commitment stages reuse `tests/commitment_journey_fixture.py` and the existing Golden runner's
production composer, critic and resolver with scripted external model judgments. Their assertions
check stored rows, original/closing references and actual brief/reminder eligibility. Neither
fixture establishes semantic model quality. Learning recall uses the original email identifier;
an unlearned display-name alias is not established without a supporting identity source.

`receiver.py` uses the real receiver and workers. `provider/sitecustomize.py` supplies external
website-response fixtures, not completed research answers. `SOTTO_EVAL_WORLD` is private harness
plumbing passed to its child processes; it is not a runtime configuration setting.

## Replaced boundaries (not acceptance claims)

- Source authority is a fixture revision, not the live consent aggregator. Most cases have empty grants. `world.savedSourceGrants` explicitly grants synthetic saved-source reads through the real receiver projection; it accepts Gmail, Calendar, Google Contacts, iMessage, WhatsApp, Mac Contacts and Granola source IDs. Google Contacts remains the distinct `google_contacts` grant and cannot authorize `contacts` ledger sources. It does not connect those providers or grant source writes. The two commitment scenarios contain the same obviously synthetic stored person and ledger records from an actual composer/critic/resolver run and include the managed-style Google Contacts capability. The revoked case removes Gmail only from `savedSourceGrants`, while leaving permissive cached capability metadata and all stored evidence intact, so cached metadata cannot restore the withheld outcome. The contact scenario grants only Contacts and supplies synthetic rows through the real relay poll/respond and consent gates; it does not exercise the Mac reader.
- Packaged root ownership and script discovery are adjusted for the local checkout.
- Node configuration is constructed by the runner, not rendered by `startup_config` (the receiver
  channel expectation does use it).
- Worker claim and outbox drain are driven explicitly after each turn, not by the production
  scheduler/heartbeat. A Node restart does not restart Python.
- Native Gemini URL extraction is a fixture response, not a real crawler. Source-injection cases
  exercise the downstream application/model, not Google's extraction defenses.
- Real provider network, transport reception, device display and writes are not certified here.

Security regressions also force adversarial tool selections against the journal and receiver;
model obedience alone cannot prove an egress boundary. The `injected-target` journey follows a
planted page with acknowledgements and cancellation and requires zero follow-on provider reads.

The first actual prep run at `532ad750` failed and remains retained privately: its synthetic
Google connection check had no token, so the worker stopped before any research or native call.
An offline full-worker reproduction then exposed the raw-provider/scoped-gather ID mismatch.
Fixture connection setup and exact occurrence mapping now have offline regressions; neither
those checks nor the previous failed conversation establish actual-model acceptance of the fix.

Review scope: `SOTTO_TRIGGER_PORT` was introduced by this prep branch and is now set only in its
prep world; preexisting scenarios retain their original environment. The synthetic token uses
an invalid eval-only token URI and is never a live Google grant. Packaging uses the existing
whole-directory receiver COPY and `*.py` inclusion rule; the pure selector helper is included.


### Actual brief-origin preparation

`brief-prep-yes.json` extends this same evaluator with `briefPrepOfferBefore`. Production
`compose_brief.compose` uses the approved real model on a Calendar-only synthetic world. It is
not given expected prose, an action, or the expected selected occurrence. The evaluator then
requires its rendered single offer to match the declared expected outcome; absence fails.
Production offer staging and accepted delivery effects bind the full composed brief to captured
transport acceptance. A bare yes must complete the actual prep worker and support a pronoun
follow-up. The native request cap is six for brief composition/revision plus prep; composition
calls are reported separately, and final prep still requires one successful native call.
This qualifies composition through captured delivery, not live gathering or device display.

`brief-prep-email-only-yes.json` retains the same actual-model brief, completed prep and follow-up
checks with no attendee display names. The named-attendee scenario remains `brief-prep-yes.json`.
Neither alone proves that a live gathered brief presents its selected offer; the source-only
rendering regressions separately cover a selected action with no matching prose paragraph.

`brief-prep-owner-focus.json` adds a bounded owner brief request to the same three-turn journey.
Its `ownerText` goes through the production owner brief parser before the admitted composition
direction reaches `compose_brief.compose`. Inspect the actual brief, completed prep and pronoun
follow-up for decision focus, evidence, unknowns and useful questions. This direct composition
fixture does not certify owner request transport or queue propagation; their receiver and runner
regressions remain separate checks.


`first-connect-draft-followthrough.json` starts with the exact discovery produced by the
November 3 synthetic first-connect composition. Two people share a name: the garden organizer's
budget request is open, while the robotics founder's presentation is resolved. Four natural turns
request a budget-breakdown draft, shorten it, ask who and why, then ask the email address.
The fixture checks both full task records after every turn, forbids writes and external reads,
and rejects the unsupported review promise observed in the original run. Manually inspect every
reply for invented commitments and recipient drift; substring assertions alone are not semantic
acceptance. Dates belong to the frozen source history, not the current day.

The recorded actual-model run omitted task expectations and therefore remains officially failed.
An independent offline audit verified its unchanged captured hashes and full canonical records;
this corrected fixture supplies the omitted expectations. It has not been rerun merely to replace
that failed report. Neither this continuation nor the prior composition proves live onboarding
transport, a fresh-owner setup, or device display.

The action-selection scenarios opt into `gmailTriage` to freeze the existing triage subprocess's
Google HTTP boundary. Production token checks, gathers, queue construction and Google projections
still run. Unknown requests leave sticky failure evidence. This inventory is not a Gmail query
engine; only the existing bounded triage selectors are supported. Inspect the first answer
semantically: mentioning the expected person is not proof that the model selected the right action.
Also inspect that turn's `sourceReads`: require `gmail_fixture` / `gmail_thread_get` for thread
`ellis`, plus the other candidate threads used in the recommendation. Automatic assertions alone
do not establish selection or full-thread retrieval. Judge the first answer independently; a later
follow-up that suggests the right answer cannot rescue an incorrect first recommendation.


### Calendar evidence and local dates

`calendar-transparent-availability.json` checks whether a Google event marked free is mistaken for
a conflict. `calendar-date-accuracy.json` covers a moved recurrence, bounded free intervals,
a declined invitation and tentative evening follow-up. `local-midnight-email-date.json` supplies
an explicit synthetic reference date and asks for the previous local evening's email timestamp.
It tests conversion, not the production clock. Calendar fixtures use one primary inventory;
a declined row does not prove a separate shared-calendar transport. Inspect first answers and
actual returned evidence: the original transparent baseline passed a broad "free" assertion while
saying "not free". The strengthened fixture rejects that wording, but semantic review remains
required. No live calendar, send, mutation or device display is exercised by these runs.
