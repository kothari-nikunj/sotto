# sotto-chief-of-staff (skill tap)

The git **tap** carries Sotto's shared `SKILL.md` procedures, extraction prompts and Python
algorithms. The Sotto chat runtime loads the skills (indexed by `skills.sh.json`) in interactive
chat and runs their commands through `run_sotto_script`. The receiver invokes the same scripts for
scheduled briefs, digests and relationship pulses in both Cloud and self-host. Native Gemini is
the pipeline's model path; hosting changes its endpoint and credential, not its payload semantics.

- How it all fits: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) (end-to-end data flow, process boundaries) · why you get nudged: [`../docs/HOW-SOTTO-DECIDES.md`](../docs/HOW-SOTTO-DECIDES.md)


> The tap root only carries `skills.sh.json` + the skill dirs below.

The sibling `../runtime/trigger-receiver/setup_markup.py` owns pure setup page shells, navigation and tile markup. The receiver keeps setup state and HTTP orchestration.

## Layout
```
skills.sh.json                       # the skill index the Sotto chat runtime loads
morning-brief/                       # THE morning brief (communications-first; never improvised)
  SKILL.md
  references/extraction-prompt.md    # PORT: api/src/services/gemini-flex.ts (the FLEX prompt)
  references/research-prompt.md      # PORT: gemini-research.ts (host-native attendee web search)
  scripts/select_attendees.py        # CLI over the shared research attendee selector
  scripts/continuity_resolve.py      # PORT: continuity.rs + deterministic.ts + reconciler.ts + tests
evening-brief/SKILL.md               # end-of-day wrap: accountability + tomorrow (carries the merged followup)
meeting-prep/                        # standalone "prep me for the people in my meetings ahead"
  SKILL.md
  references/meeting-prep-prompt.md  # PORT: registry.ts MEETING_PREP_PROMPT + claude-flex.ts buildMeetingResearch
  scripts/compose_meeting_prep.py    # joins external attendees -> research + knowledge graph + Granola, one message + tests
  scripts/persist_prep.py            # persist attendee research into the graph (people AND companies) + the 30d freshness filter
followup/                            # post-meeting: commitments + ready-to-send follow-up drafts
  SKILL.md
  references/followup-prompt.md      # the followup extraction prompt (grounded-only, verbatim emails)
  scripts/compose_followup.py        # meetings that JUST ended (Granola transcripts) -> commitments + drafts
  scripts/apply_commitments.py       # write extracted commitments straight into the continuity ledger
  scripts/capture_commitments.py     # shared cached extract+apply; Learn retries late/edited notes
proactive/                           # ~15-min watcher: meeting-prep/commitment/chase/birthday/handoff nudges
  SKILL.md
  scripts/proactive_scan.py          # what is due (lead window, dedup) → the funnel decides who hears it
event-triage/                        # real-time event funnel (Bridge/Gmail events -> act now or stay silent) — skill: sotto-event
  SKILL.md
  scripts/triage_event.py            # Tier 0+1 of the funnel (deterministic gate + cheap LLM triage)
  scripts/poll_gmail.py              # cloud-side email events (receiver shells out to it — not an orphan)
  scripts/digest_check.py            # adaptive midday catch-up gate
relationship-pulse/                  # weekly "who am I losing touch with / who's waiting on me"
  SKILL.md
  scripts/relationship_pulse.py      # PORT: relationship_analytics.rs; writes relationship_state.json + tests
ask/SKILL.md                         # "Ask Sotto" — Q&A over the exhaust + live tools (PORT: ask.ts)
draft-reply/SKILL.md                 # draft a reply/message in the user's voice (never auto-send)
feedback/SKILL.md                    # corrections -> preferences/graph fixes ("stop surfacing newsletters")
loops/SKILL.md                       # "what am I waiting on / what do I owe" + clearing the stale ones
people/SKILL.md                      # the people in the user's life (attention, history, facts)
routines/SKILL.md                    # personal recurring jobs said in plain language (`user-` crons only)
intentions/SKILL.md                  # one-shot plain-language reminders/checks; schedule_wakeup.py wake-ups on the 15-min heartbeat, no cron
schedule/SKILL.md                    # find time / book meetings (calendar write via approval tiers)
triage/SKILL.md                      # "triage my inbox" — the cross-channel needs-you queue
setup/SKILL.md                       # guided first run: health() check → seed memory+voice → schedule → first brief
_shared/
  references/approval-tiers.md       # the autonomy policy every skill honors (PORT: approval-policy.ts)
  references/relevance.md            # ONE relevance judgment for briefs, digests and nudges
  lib/relevance.py                  # shared policy loader + native-model judgment seam
  lib/personal_context.py           # ONE conversation renderer (thread keys, message caps) for triage, digest and learning; --feedback-only prints the owner's usefulness examples
  lib/history_coverage.py           # content-free extraction windows and gaps, shared with dashboard, person-context and topic-outcome reads
  lib/brief_evidence.py             # observed-reference binding shared by composition and delayed brief learning
  lib/source_context.py             # authenticated Bridge reads, observation receipts and consent projection
  lib/source_authority.py           # shared live runtime-authority transport; workflow checkpoints stay local
  lib/source_catalog.py             # Bridge source IDs, labels and payload projections shared by readers and consent gates
  lib/source_corrections.py         # read-only compatibility for retained correction provenance; admission retired
  lib/pipeline_checkpoint.py        # shared started/completed stage receipts; each pipeline owns its authority guard
  lib/local_snapshot.py             # consent-filtered local snapshot resolution shared by brief composition and its runner
  lib/work_queue.py                 # durable work, lease ownership and unmounted routine cursor/slot admission
  lib/cleanup_selection.py          # pure complete-snapshot and bounded numeric-answer predicates for owner task cleanup
  lib/owner_loop_capability.py      # private identity-bound explicit owner-command capability
  lib/routine_cursor.py             # pure pinned-native cursor planning; no scheduler or activation
  lib/routine_cursor.NOTICE         # upstream scheduler MIT attribution
  lib/calendar_prep_projection.py   # pure day-cache rows and prep notification text; no reads or writes
  lib/calendar_gather.py            # one independent Calendar worker and source receipt for the ordinary Google gather
  lib/calendar_context.py           # shared human attendees and explicit calendar participation
  lib/calendar_cache_contract.py    # calendar producer/consumer cache path, source IDs and refresh protocol
  lib/delivery_effects.py           # current-item eligibility and replayable post-acceptance effects
  lib/attended_web_read.py          # one admitted public URL through the original provider ladder; immutable contact proof
  lib/brief_detail_selection.py     # original gallery discovery retention and owner-local acceptance dates
  lib/contact_snapshot.py           # bounded, lossless Google People contact arrays for attended review/readback
  lib/ledger_provenance.py          # pure fail-closed provenance projection for continuity read tools; legacy rows withheld
  lib/owner_continuity.py           # bounded owner-only continuity projections (routes and model tools are separate policy)
  lib/protected_brief_detail.py     # bounded future-only retained gallery text projection; no HTTP or grant writer
  lib/protected_ledger_io.py        # bounded local ledger input for protected projections; no grants or writes
  lib/retained_brief_proof.py       # integrity binding for retained brief text; never grants source or route access
  references/voice.md                # Sotto's voice/persona notes shared by the skills
  references/writing-style.md        # plain-language rules for all user-facing prose (briefs, replies, drafts)
  assets/                            # card fonts (Inter + OFL licence) and the bundled wax-seal mark with its recorded prompt
  lib/attachments.py                 # email attachments → Markdown, LOCALLY (anydoc); owns the lane's three caps
  lib/brief_validate.py              # deterministic post-hoc brief validator
  lib/relationship_importance.py     # stable VIP/VVIP importance (reciprocal activity, held 1:1 meetings; gates birthday/gift offers) + finite reply-engagement hints
  lib/chatfmt.py                     # the ONE markdown→chat-text transform (to_chat) every surface shares
  lib/visual_brief.py                # deterministic optional image cards of the SAME composed brief/prep; no model calls
  lib/message_targets.py             # supported messaging addresses and invalid-link removal shared by builders and delivery
  lib/connector_tokens.py            # read/refresh per-service OAuth tokens from the receiver's /setup
  lib/gemini_transport.py            # identical native Gemini payloads; managed tenant proxy or self-host key
  lib/google_cli.py                  # strict Google CLI JSON decoder, including the empty Gmail-search sentinel
  lib/model_work.py                 # durable attempts per operation; content-free request receipts
  lib/usage_accounting.py           # canonical cached-input/thinking-aware estimates; packaged into proxy
  lib/gemini.py                      # direct Gemini REST call (call_gemini) + retryable-error classification + diagnostics
  lib/jsonstore.py                   # locked JSON read/write for shared state files (pending_offer, preferences)
  lib/jsonfiles.py                   # optional JSON input loading for triage/proactive, preserving null/default behavior
  lib/envutil.py                     # shared integer environment parsing for proactive/triage/digest
  lib/utc_time.py                    # UTC epoch formatter shared by dashboard/calendar records
  lib/funnel_policy.py               # shared budget exemptions, valve bounds, chase cap, proactive source and claim staleness
  lib/keys.py                        # queue/style-sample ids — VENDORED byte-identical into runtime/trigger-receiver/
  lib/mcp_client.py                  # minimal Streamable-HTTP MCP client for deterministic gathers
  lib/metrics.py                     # per-run cost/latency accumulator ([brief-cost] lines)
  lib/google_calendar_read.py        # consent-bound calendar selection and bounded shared-calendar reads
  lib/gmail_read.py                 # shared full Gmail body reader and recursive MIME extraction
  lib/render_local.py                # per-source formatSourceForLLM-style renderers
  lib/sotto_log.py                   # shared diagnostics -> stderr + $SOTTO_DATA/logs/compose_brief.log
  lib/textutil.py                    # string/identifier/domain and exact source-envelope adapters
  lib/canonical_identity.py         # byte-stable ID minting; callers keep their normalization/selection
  lib/research_contract.py          # shared research qualification, caps and retained-evidence serialization
  lib/research_providers.py         # shared provider keys/capabilities, without provider execution
  lib/prep_selection.py             # research audience and owner meeting-occurrence selection
  lib/prep_identity.py              # exact-address naming with explicit source evidence
  lib/timeutil.py                    # timezone/date/timestamp helpers (delegates the zone to tzchain)
  lib/tzchain.py                     # data-root and timezone chain (SOTTO_TIMEZONE → TZ → settings.json → UTC); copied beside the receiver in the image
  scripts/compose_notification.py    # direct proactive/event renderer and bounded native writer
  scripts/render_cards.py            # render an existing brief/prep file as cards; never researches, sends or changes memory
  scripts/compose_brief.py           # the FLEX extraction engine + critic + tap-links + escalation (PORT: gemini-flex.ts/brief-critic.ts/generate.ts)
  lib/review_candidates.py           # Friday candidate selection and accepted offer receipts in existing relationship state
  scripts/brief_detail.py            # accepted owner photo text, permission-checked and paginated
  scripts/meeting_context.py         # read-only meeting notes and canonical obligation outcomes
  scripts/release_one_proof.py        # read-only retained-history coverage and proposal counters; no model call
  scripts/learn_step.py              # essential knowledge/continuity, then separately queued ancillary writers; one merged receipt
  scripts/memory_cycle.py            # bounded history progress, changed-person curation and weekly stale-fact archival
  scripts/context_learning.py        # cited durable facts from consented direct-message history
  scripts/dreamer.py                 # bounded evidence selection, duplicate archival and conflict flags
  scripts/gather_google.py           # deterministic Gmail+Calendar gather (CLI or MCP normalize)
  scripts/loop_context.py            # bounded Gmail refresh of carried-over loops' exact threads/counterparts; context, never a loop-state writer
  scripts/gather_granola.py          # deterministic Granola gather (MCP lane + REST break-glass)
  scripts/google_action.py           # granted capabilities + gated Gmail draft/send and calendar writes
  scripts/research_attendees.py      # batched two-pass attendee research (deep_research capability)
  scripts/web_research.py            # THE search seam: the provider resolver + web_search / deep_research / fetch_url
  scripts/docsend_fetch.py           # email gate + Gemini vision; cached page coverage is explicit, full-deck completeness unknown
  scripts/granola_graph.py           # Granola attendance → dated, sourced facts on the people you met
  scripts/prewarm_graph.py           # setup-time graph seed (stubs + research) + --sync-contacts (identifiers/notes/birthdays)
  scripts/style_extract.py           # PORT: style-profile.ts (fingerprint v2)
  scripts/style_apply.py             # PORT: style-profile.ts formatStyleForWorker (verbatim sample injection)
  scripts/preferences.py             # explicit preference memory (mute/tone rules)
  scripts/log_outcome.py             # outcomes + analytics (parity C2)
  scripts/usefulness_feedback.py     # "that was useful / not useful" bound to a delivered brief or offered draft; stores reference + rating only
  scripts/draft_outcomes.py          # draft→outcome matcher: offered drafts × what you actually sent
  scripts/schedule_wakeup.py         # one-shot intentions (create/list/cancel; serviced by the 15-min heartbeat)
  scripts/action_links.py            # deep-link / tap-to-send URL builder (+ the offered-drafts ledger)
  scripts/brief_marker.py            # delivered-once gate (cloud cron ↔ Bridge wake-push)
  scripts/pending_offer.py           # delivered question/receipt binding, ambiguity handling and authorized reply resolution
  scripts/ledger_io.py               # shared READ helpers for the continuity ledger
  scripts/loops_query.py             # open-loops/action-ledger read view (sotto-loops, proactive)
  scripts/identity_diagnose.py       # read-only operator check: why same-name open loops on two channels stay two people
  scripts/retune_scan.py             # read-only stale-loop scan behind sotto-loops §B
  scripts/retune_apply.py            # WRITE side of sotto-loops §B (clear/defer a loop)
  scripts/triage_queue.py            # cross-channel "needs you" queue for sotto-triage
  scripts/triage_read.py             # fresh bounded triage inputs through the original gather and queue builders
  scripts/ask_research_pipeline.py   # one graph-first Ask research job: shared research, persistence and answer runtime
  scripts/followup_pipeline.py       # on-demand follow-up extraction and canonical capture, uncertain stages held
  scripts/granola_history.py         # bounded Granola MCP discovery or one exact meeting; no extraction or ledger writes
  scripts/meeting_prep_pipeline.py   # shared complete meeting-prep orchestration; callers own admission and delivery
  scripts/relationship_pulse_pipeline.py  # attended relationship pulse; uncertain state writes are never repeated
  scripts/x_connectivity.py          # X attendee context: exact-handle identity resolution + ephemeral prep signals
  knowledge/                         # THE memory subsystem — graph models, writers and query/edit surfaces
    knowledge.py                     # graph model, serializer and live conflict-pair projection
    knowledge_correction.py         # fact correction/profile state and research coverage helpers; no file writes
    knowledge_update.py              # apply an extraction (dedup/decay/prune) + the ONE relations and company writer
    knowledge_query.py               # canonical participants from local, email, calendar and active loops; standalone mtime fallback
    knowledge_edit.py                # user-initiated writes (facts, company About, loops, merges, relations) for chat + dashboard
    master_file.py                   # the ONE writer/reader for knowledge/master.md (the always-in-context master memory file)
evals/                               # brief-quality eval harness
  README.md                          # how to run it + the Golden Corpus doctrine
  run_evals.py                       # fixture evals (shape + invariants, stubbed LLM)
  run_relevance.py                   # opt-in live judgment probe; invented regression examples
  run_golden.py                      # Golden Corpus replay: the owner's real history, scored vs his labels
  run_memory_evidence.py             # isolated current/grounded Dreamer comparison; scripted default, explicit paid mode
  memory_evidence.py                 # experiment-only fact/source-version and citation binding; no production activation
  inspect_commitment_journey.py      # five isolated scripted runs using the existing journey fixture; inspect brief/history/reminder output
  run_first_day.py                   # first-day probe: a fresh volume's welcome look creates no historical nudges or loops
  replay_tracking.py                 # continuous offline month; reports unmet tracking contract, not a quality score
  inspect_source_coverage.py          # isolated synthetic receiver/state/prompt coverage receipts; no model or Mac claim
  LABELING.md                        # the one-hour labeling runbook (evals/README.md § The Golden Corpus)
  fixtures/ · baselines/             # canned inputs + committed expected outputs
  corpus/                            # NEVER committed, NEVER shipped — real user data, built locally
tests/                               # pytest: parity fixtures in → expected exhaust out (conftest sets sys.path)
  test_notification_batch_context.py # current-batch replies, context mutes, exact threads and quiet notification admission
  test_notification_current_mutes.py # current person/sender mutes through composition and restarted outbox delivery
tools/                               # skill-tree tooling
  vulture_whitelist.py               # AST-only references for current callback/fixture argument contracts
  verify.py                          # dependency lock, lint, dead-code/size checks and shared backend/release gate: pipeline, receiver, adapter, proxy, guards
  dry_run.py                         # offline full-loop rehearsal (no LLM, no network; fixtures/brief_bundle.json)
  fixtures/brief_bundle.json         # dry_run.py's default input bundle
  validate_skills.py                 # SKILL.md lint (one of the checks verify.py runs; also usable on its own)
  forget.py                          # delete the exhaust (snapshot, caches, logs, receipts) — never the memory
  build_golden_corpus.py             # builds the Golden Corpus from $SOTTO_DATA (runs where the data lives)
```

The root supervisor's process lifecycle lives outside this skill pack at
`../adapters/sotto/tenant_lifecycle.py`; its synthetic private-container fixture is
`../adapters/sotto/test_fixtures/run-lifecycle.sh`. It adds no skill, scheduler,
or enabled routine.


## Rules
- Every `SKILL.md` is valid agentskills (frontmatter `name`+`description` — the validator enforces both). It describes how the skill runs on Sotto: only command IDs and tools the runtime exposes, no script paths or host shell steps (`../runtime/sotto-chat/test/skill-command-drift.test.ts`).
- Python scripts read/write the exhaust at `$SOTTO_DATA` (the Sotto data volume); keep person/company `.md` + `style.json` schemas **byte-compatible** with today's Sotto files.
- Ship parity tests with fixtures for every script (cite the ported source file in a header comment).


`_shared/lib/source_catalog.py` owns the Bridge source IDs and payload projections used by both the receiver consent gate and shared readers.

Optional visual output: `_shared/lib/visual_brief.py` holds deterministic templates; `_shared/assets/card-fonts/` holds licensed fonts; `_shared/scripts/render_cards.py` renders saved brief/prep text without sending or calling a model.

Shared writing rules: [`_shared/references/writing-style.md`](_shared/references/writing-style.md), used by the pipeline and the chat persona across hosting modes and channels.

The repository-level `../tools/runtime_release_artifact.py` qualifies the fresh Railway image and
emits its immutable descriptor; `../tools/bundle_runtime_descriptor.py` validates and seals that
artifact into the Bridge bundle before signing. Their maintainer runbook is
[`../tools/RUNTIME-RELEASE.md`](../tools/RUNTIME-RELEASE.md). Neither tool starts a pilot or grants
permission to send owner messages.

Focused meeting prep leads with the named person and meeting, then the stated purpose, known
private history, and open decisions. Questions should explain the evidence that makes them useful;
missing or partial source coverage is disclosed without treating missing history as first contact.
Prep cards retain sourced paragraphs and put clickable links in the companion text. A prep that
fits the existing 200-word card budget, or has fewer than four substantive sections, stays in text
instead of splitting questions into thin pages. Public research retains actual provider evidence,
including empty searched results; absent grounding is reported as unavailable, never reconstructed
from a model-written URL.

The brief model selects advance preparation on the existing calendar action, with its exact event,
source attendee and visible preparation request. The existing coherence review flags a prep offer
on an email-reply action; it preserves that obligation and asks for a supported calendar selection
or no offer. Delivery, identity and permission gates remain unchanged.

`_shared/lib/deferred_queue.py` owns atomic multi-event deferred JSONL admission under triage's
existing lock. Single events retain the bounded append/fsync path.
