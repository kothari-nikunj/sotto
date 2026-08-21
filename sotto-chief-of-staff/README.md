# sotto-chief-of-staff (skill tap)

The git **tap** that carries Sotto's brain: `SKILL.md` procedures + the extraction prompt + the deterministic Python algorithms. Hermes installs this and runs it; its Gemini does the LLM work, `execute_code` runs the scripts over the exhaust on the Hermes volume.


> The persona + bundle live in `../adapters/hermes/` (`sotto-persona.md`, `sotto.bundle.yaml`), not in the tap root. The tap root only carries `skills.sh.json` + the skill dirs below.

## Layout
```
skills.sh.json                       # Hub categories (tap root manifest)
morning-brief/                       # THE morning brief (communications-first; never improvised)
  SKILL.md
  references/extraction-prompt.md    # PORT: api/src/services/gemini-flex.ts (the FLEX prompt)
  references/research-prompt.md      # PORT: gemini-research.ts (host-native attendee web search)
  scripts/select_attendees.py        # PORT: processCalendarEvents needs-research filter (72h/external/cap 25)
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
proactive/                           # ~15-min watcher: meeting-prep/commitment/chase/birthday/handoff/tidy-up nudges
  SKILL.md
  scripts/proactive_scan.py          # what is due (lead window, dedup) → the funnel decides who hears it
event-triage/                        # real-time event funnel (Bridge/Gmail events -> act now or stay silent)
  SKILL.md
  scripts/triage_event.py            # Tier 0+1 of the funnel (deterministic gate + cheap LLM triage)
  scripts/poll_gmail.py              # cloud-side email events (receiver shells out to it — not an orphan)
  scripts/digest_check.py            # adaptive midday catch-up gate
relationship-pulse/                  # weekly "who am I losing touch with / who's waiting on me"
  SKILL.md
  scripts/relationship_pulse.py      # PORT: relationship_analytics.rs; writes relationship_state.json + tests
approval-tiers/
  scripts/learn_preferences.py       # PORT: preference-learner.ts + feedback.ts (parity C1) — not a skill
ask/SKILL.md                         # "Ask Sotto" — Q&A over the exhaust + live tools (PORT: ask.ts)
draft-reply/SKILL.md                 # draft a reply/message in the user's voice (never auto-send)
feedback/SKILL.md                    # corrections -> preferences/graph fixes ("stop surfacing newsletters")
loops/SKILL.md                       # "what am I waiting on / what do I owe" + clearing the stale ones
people/SKILL.md                      # the people in the user's life (attention, history, facts)
routines/SKILL.md                    # personal recurring jobs said in plain language (`user-` crons only)
schedule/SKILL.md                    # find time / book meetings (calendar write via approval tiers)
triage/SKILL.md                      # "triage my inbox" — the cross-channel needs-you queue
setup/SKILL.md                       # guided first run: health() check → seed memory+voice → schedule → first brief
_shared/
  references/approval-tiers.md       # the autonomy policy every skill honors (PORT: approval-policy.ts)
  references/audio-script-prompt.md  # PORT: audio-brief.ts narration (parity C3)
  references/voice.md                # Sotto's voice/persona notes shared by the skills
  lib/brief_validate.py              # deterministic post-hoc brief validator
  lib/chatfmt.py                     # the ONE markdown→chat-text transform (to_chat) every surface shares
  lib/connector_tokens.py            # read/refresh per-service OAuth tokens from the receiver's /setup
  lib/gemini.py                      # direct Gemini REST call (call_gemini) + retryable-error classification + diagnostics
  lib/keys.py                        # queue/style-sample ids — VENDORED byte-identical into runtime/trigger-receiver/
  lib/mcp_client.py                  # minimal Streamable-HTTP MCP client for deterministic gathers
  lib/metrics.py                     # per-run cost/latency accumulator ([brief-cost] lines)
  lib/render_local.py                # per-source formatSourceForLLM-style renderers
  lib/sotto_log.py                   # shared diagnostics -> stderr + $SOTTO_DATA/logs/compose_brief.log
  lib/textutil.py                    # string/identifier/domain normalization primitives
  lib/timeutil.py                    # timezone/date/timestamp helpers
  scripts/compose_brief.py           # the FLEX extraction engine + critic + tap-links + escalation (PORT: gemini-flex.ts/brief-critic.ts/generate.ts)
  scripts/gather_google.py           # deterministic Gmail+Calendar gather (CLI or MCP normalize)
  scripts/gather_granola.py          # deterministic Granola gather (MCP lane + REST break-glass)
  scripts/google_action.py           # the WRITE side of google-workspace (Gmail draft/send, calendar writes)
  scripts/research_attendees.py      # batched two-pass attendee research (deep_research capability)
  scripts/web_research.py            # THE search seam: the provider resolver + web_search / deep_research / fetch_url
  scripts/docsend_fetch.py           # read a DocSend deck via its email gate + Gemini vision (chat-only — a view is visible to the sender)
  scripts/prewarm_graph.py           # setup-time graph seed (stubs + default-on low-conf research)
  scripts/style_extract.py           # PORT: style-profile.ts (fingerprint v2)
  scripts/style_apply.py             # PORT: style-profile.ts formatStyleForWorker (verbatim sample injection)
  scripts/preferences.py             # explicit preference memory (mute/tone rules)
  scripts/log_outcome.py             # outcomes + analytics (parity C2)
  scripts/action_links.py            # deep-link / tap-to-send URL builder
  scripts/brief_marker.py            # delivered-once gate (cloud cron ↔ Bridge wake-push)
  scripts/pending_offer.py       # the one question Sotto is waiting on an answer to (proactive writes, gateway reads+clears)
  scripts/ledger_io.py               # shared READ helpers for the continuity ledger
  scripts/loops_query.py             # open-loops/action-ledger read view (sotto-loops, proactive)
  scripts/retune_scan.py             # read-only stale-loop scan behind sotto-loops §B
  scripts/retune_apply.py            # WRITE side of sotto-loops §B (clear/defer a loop)
  scripts/triage_queue.py            # cross-channel "needs you" queue for sotto-triage
  knowledge/                         # THE memory subsystem — one directory, four jobs
    knowledge.py                     # graph core: model + serializer (PORT: knowledge_files.rs schemas)
    knowledge_update.py              # apply an extraction (dedup/decay/prune) + the ONE relations and company writer
    knowledge_query.py               # read + pack for the LLM (--calendar exempts today's cast)
    knowledge_edit.py                # user-initiated writes (facts, company About, loops, merges, relations) for chat + dashboard
    master_file.py                   # the ONE writer/reader for knowledge/master.md (the always-in-context master memory file)
evals/                               # brief-quality eval harness (run_evals.py + fixtures/baselines)
  run_golden.py                      # Golden Corpus replay: the owner's real history, scored vs his labels
  LABELING.md                        # the one-hour labeling runbook (evals/README.md § The Golden Corpus)
  corpus/                            # NEVER committed, NEVER shipped — real user data, built locally
tests/                               # pytest: parity fixtures in → expected exhaust out (conftest sets sys.path)
tools/                               # dry_run.py (offline full-loop) + validate_skills.py (SKILL.md lint)
  forget.py                          # delete the exhaust (snapshot, caches, logs, receipts) — never the memory
  build_golden_corpus.py             # builds the Golden Corpus from $SOTTO_DATA (runs where the data lives)
```

## Rules
- Every `SKILL.md` is valid agentskills (frontmatter `name`+`description`, `requires_toolsets`).
- Python scripts read/write the exhaust at `$SOTTO_DATA` (the Hermes volume); keep person/company `.md` + `style.json` schemas **byte-compatible** with today's Sotto files.
- Ship parity tests with fixtures for every script (cite the ported source file in a header comment).
