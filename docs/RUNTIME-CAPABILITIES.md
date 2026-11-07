# Runtime capabilities

This is a code reference, reconciled November 3, 2026. It distinguishes mounted runtime
capabilities from standalone adapters and unsupported behavior. Presence in source, a connected
fixture, or a healthy endpoint does not prove live provider delivery or a qualified release.
Implementation priorities live only in the internal `docs/plans/ROADMAP.md`.

Use [CHANNELS](../CHANNELS.md) for installation choices, [the behavior inventory](BEHAVIOR-INVENTORY.json)
for named contracts and their test evidence, and [ARCHITECTURE](ARCHITECTURE.md) for process ownership.
The tables below describe configured, authorized installations. A source tool still requires its
current grant; a mutation still requires the specific owner request and confirmation contract.

## Daily-use and release limits

The existing compatible owner pilot has scoped evidence for retrieval, drafting, task operations
and brief → yes → completed prep. It is suitable for attended use with review of consequential
answers. This does not qualify a fresh installation for another customer. A completed data-restore
drill, exact signed app/image setup and upgrade acceptance, and fresh-owner useful delivery are
still release gates. Source corrections from historical edits/unsends remain disabled.

Conversation recall is bounded to 20 recent messages and 65,536 characters. Saved canonical
knowledge and original sources remain separately retrievable with tools; arbitrary old chat is not
semantic recall. Chat generation is serialized, and queue wait is separate from active execution.
New input can cancel active generation; it does not reverse a provider action already contacted.
Unknown effects stay held until proof settles them. Delivery acceptance is not phone display.

## Mounted capabilities

Paths in this table are relative to the `sotto-agent` project root. `chat/` means `runtime/sotto-chat/`;
`receiver/` means `runtime/trigger-receiver/`; `skills/` means `sotto-chief-of-staff/`.
Test references identify existing evidence to inspect or rerun, not tests run by this document.

| Capability | Code boundary | Evidence and limits |
| --- | --- | --- |
| Owner chat and delivery | `chat/src/conversation.ts`, `channels/chat.ts`, `bootstrap/main.ts` | `chat/test/conversation.test.ts` and inventory `photon_owner_admission` cover durable owner admission. Only the configured owner's direct messages command Sotto. Delivery has its own durable receipt; admission is not delivery. |
| Google and consented Mac reads | `chat/src/agent/google-read.ts`, `agent/bridge.ts`, `agent/source-authority.ts`; `bootstrap/main.ts` assembles permitted tools | Read authority is checked against the current source revision. A channel credential is not a Google or Mac source grant. See [DATA-FLOW](DATA-FLOW.md). |
| Knowledge, loops, retune, Granola history, style and triage reads | `chat/src/tools/knowledge-command.ts`; `receiver/runtime_knowledge_read.py:validate` and `perform` | `chat/test/knowledge-command-connected.test.ts`, `receiver/test_runtime_knowledge_read.py`. Fixed operations include a graph snapshot without query-time migration writes; this is not an arbitrary script executor. |
| Tasks, explicit preferences and reminders | `chat/src/tools/owner-task-tool.ts`, `owner-preference-execution.ts`, `owner-reminder-tool.ts`; `skills/_shared/scripts/schedule_wakeup.py:create_tool_admitted` | `chat/test/delayed-basic-actions.test.ts`, `skills/tests/test_intentions.py`; inventory records per-action uncertainty and receipt behavior. Reads, proposed changes and committed changes are distinct outcomes. |
| Fresh personal routines | `chat/src/tools/owner-routine-tool-contract.ts:routineScheduleSchema` and `ownerRoutineToolArgsSchema`; `receiver/fresh_routines.py` | `chat/test/owner-routine-tool-connected.test.ts`. Creation, listing, cancellation and schedule adjustment exist. Schedules include hourly, daily, selected weekdays, weekdays and first-of-month. Schema destinations include Photon and Telegram; changes require their reviewed proposal/confirmation. This does not activate legacy imports or accept arbitrary cron. |
| Attended Google effects | `chat/src/bootstrap/main.ts` routes reviewed draft, Gmail send, Calendar and Contacts commands to receiver-owned capabilities | `chat/test/owner-gmail-draft-connected.test.ts`, `gmail-reply-send-connected.test.ts`, `gmail-new-send-connected.test.ts`, `calendar-reschedule-connected.test.ts`, `secondary-calendar-connected.test.ts`, `owner-contacts-connected.test.ts`. Exact supported operations have individual scope, confirmation and recovery rules; a general Google mutation tool is not implied. |
| Attended web and DocSend | `chat/src/tools/owner-web-read.ts`, `read-admission.ts` and `read-policy.json` (shared with `receiver/runtime_read_admission.py`); `receiver/runtime_owner_web_read.py:perform`; `skills/_shared/scripts/docsend_fetch.py:AttendedHttp` and `read_deck` | `chat/test/web-read-connected.test.ts`, `skills/tests/test_docsend_coverage.py`. A contextual read needs a current owner request within 15 minutes of the accepted context; assistant or background prose never grants read authority. A tracked DocSend contact requires the existing exact-owner-request/review contract. Retained or partial text is not a complete-deck claim. See [DOCSEND-COVERAGE](DOCSEND-COVERAGE.md). |
| Attended shared procedures | `chat/src/bootstrap/main.ts` routes `meeting-prep`, `ask-research`, `followup`, `relationship-pulse` and `proactive` through the shared skill command | `chat/test/owner-skill-request.test.ts` and inventory `photon_explicit_prep`, `photon_scoped_prep`, `photon_owner_skill_terminal_feedback`. Scope must come from the owner's request. Telegram does not inherit the full Photon attended profile. |
| Receipt-bound chat prep offers | `chat/src/tools/owner-meeting-prep-offer-tool.ts`; `receiver/runtime_owner_meeting_prep_request.py` | One exact current Calendar occurrence and invited attendee; the delivered canonical question activates the existing offer receipt, and acceptance queues focused prep. Explicit offer → yes → completed prep → contextual follow-up was observed on the #914 pilot. November 3 focused brief acceptance also retained one exact occurrence through a delayed yes and source follow-up. This is scoped provider/content evidence, not all meeting selection or device-display acceptance. |
| Attended brief requests | `chat/src/tools/owner-brief-tool.ts` (`owner_brief_request`) and the fast path in `chat/src/conversation.ts`; `receiver/runtime_owner_brief_request.py:perform` | `chat/test/owner-brief-request.test.ts`, `receiver/test_runtime_owner_brief_request.py`. Admits the existing brief queue only for a current affirmative owner request; the brief arrives separately and "queued" needs a committed receipt. |
| Scheduled briefs, events and memory | Receiver queue/scheduler and fixed runners; `skills/_shared/scripts/learn_step.py`, `memory_cycle.py`, `dreamer.py` | `skills/tests/test_continuous_memory.py`, `test_dreamer_progress.py`, `test_curation_scope.py`, `test_memory_context_contract.py`. The receiver owns scheduling, canonical writes and outbox effects; Node chat is not a second scheduler. Model judgment and live source quality need separate evidence. |
| Learning from observed sends | `skills/_shared/scripts/draft_outcomes.py:run`, `style_extract.py:confirm_sample` | `skills/tests/test_draft_outcomes.py`, `test_style_seed.py`. Observed outcomes and explicit preferences have different authority; silence cannot become permission. |
| Optional external-client MCP | `receiver/client_mcp.py`, `client_mcp_backend.py`, `client_oauth.py` | Read-only saved knowledge, commitments, Gmail, selected calendars, consented Mac snapshot/history/Notes, Google Contacts and Granola wrappers. Source permission, reader support and each client's granted tools are distinct. OAuth/native grants cannot invoke the Sotto agent or writes. Local-client and hosted Claude retrieval have scoped evidence; hosted expiry/reconnect, actual ChatGPT and added-reader live acceptance remain separate. |
| Management dashboard and evaluation tools | `receiver/dashboard.py`; `skills/tools/build_golden_corpus.py`; `skills/evals/run_golden.py:judge_day` and `_baseline_gate` | `receiver/test_dashboard.py`, `skills/tests/test_golden_corpus.py`. Existing controls and labeling/replay/judge machinery are implemented. A reviewed corpus or measured live baseline is not implied. The dashboard is not a web chat product. |

## Channel boundaries

Photon is the complete current attended profile. Telegram has protected owner pairing, durable
polling, core private owner tools and routines, the fixed scheduled procedures, requested briefs,
and accepted-brief readback. The inventory explicitly marks Telegram attended meeting prep,
owner skill requests, offered replies and reuse of its route proof for the full attended profile
as deferred; `chat/src/bootstrap/main.ts` also mounts attended web and DocSend reads for Photon
only. Do not infer Telegram parity from a shared TypeScript module or destination enum.

Unknown effects remain uncertain until the action's exact proof settles them. The inventory
distinguishes the implemented Photon original-action notices and bounded proof recovery from
other action/channel policies. Each delayed-action kind in `chat/src/delayed-outcome-policy.ts`
(per-kind policy) re-inspects its receipt every 60 seconds and makes at most two notice attempts. Never
repeat an external effect merely because its response was lost.

## Implemented foundations that are not live runtime features

| Foundation | Code and tests | Current boundary |
| --- | --- | --- |
| Other self-host chat providers | `chat/src/agent/selfhost-chat-provider.ts:createSelfHostChatModel`; `chat/test/agent.selfhost-chat-provider.test.ts` | Google/OpenAI/Anthropic/Qwen adapters exist, but this factory has no production bootstrap caller. Current supervised chat/media configuration is Google. Python brief-provider support in `skills/_shared/lib/gemini.py` does not supply missing launch credentials or role configuration. See [MODELS](MODELS.md). |
| Hosted STT and TTS | `chat/src/agent/hosted-speech.ts:createHostedSpeech`; `chat/test/agent.hosted-speech.test.ts` | Independently callable Google/OpenAI adapters; not wired into channel ingestion, Cloud admission/proxy, onboarding or outbound audio delivery. Native model media input is a separate capability. See [hosted speech](../runtime/hosted-speech/README.md). |
| Keyless browser driver | `runtime/browser-harness/adapter.py`, `tests/test_browser_harness_adapter.py`, `smoke.py` | Isolated fresh-profile driver foundation; not packaged or registered as a tenant tool. Enforced egress isolation, resource limits, tenant authority and authenticated-session handling remain outside the adapter. Its URL checks are not an SSRF boundary. See [browser harness](../runtime/browser-harness/README.md). |
| Optional CPU speech synthesis | `runtime/local-speech/speech.py`, `tests/test_local_speech.py` | Standalone sherpa-onnx/Kokoro TTS with an explicitly installed model pack; no STT, onboarding integration or message delivery. See [local speech](../runtime/local-speech/README.md). |

## Unsupported contracts

No general shell/code-execution tool, arbitrary model-selected script path, autonomous code
editor, arbitrary legacy routine activation, or unrestricted Google/Bridge write surface follows
from the mounted operations above. An adapter supporting a provider does not establish that
provider's context size, speech features, model availability or Cloud budget routing.

Keep the reference current by checking the production caller, receiver authority boundary,
canonical writer or delivery owner, and named regression test together. Record live acceptance
separately; do not turn historical deployment receipts into evidence for a different image or flow.
