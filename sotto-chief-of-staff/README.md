# sotto-chief-of-staff (skill tap)

The git **tap** that carries Sotto's brain: `SKILL.md` procedures + the extraction prompt + the deterministic Python algorithms. Hermes installs this and runs it; its Gemini does the LLM work, `execute_code` runs the scripts over the exhaust on the Hermes volume.


> The persona + bundle live in `../adapters/hermes/` (`sotto-persona.md`, `sotto.bundle.yaml`), not in the tap root. The tap root only carries `skills.sh.json` + the skill dirs below.

## Layout
```
skills.sh.json                       # Hub categories (tap root manifest)
ask/SKILL.md                         # "Ask Sotto" — Q&A over the exhaust + live tools (PORT: ask.ts)
morning-brief/
  SKILL.md
  references/extraction-prompt.md    # PORT: api/src/services/gemini-flex.ts (the FLEX prompt)
  references/research-prompt.md      # PORT: gemini-research.ts (host-native attendee web search)
  scripts/select_attendees.py        # PORT: processCalendarEvents needs-research filter (72h/external/cap 25)
  scripts/knowledge_query.py         # PORT: knowledge_files.rs (pack person/company for the LLM)
  scripts/knowledge_update.py        # PORT: knowledge_files.rs (dedup/decay/prune) + tests
  scripts/continuity_resolve.py      # PORT: continuity.rs + deterministic.ts + reconciler.ts + tests
evening-brief/SKILL.md
meeting-prep/                        # standalone "prep me for the people in my meetings ahead"
  SKILL.md
  references/meeting-prep-prompt.md  # PORT: registry.ts MEETING_PREP_PROMPT + claude-flex.ts buildMeetingResearch
  scripts/compose_meeting_prep.py    # joins external attendees -> research + knowledge graph + Granola, one message + tests
relationship-pulse/                  # weekly "who am I losing touch with / who's waiting on me"
  SKILL.md
  scripts/relationship_pulse.py      # PORT: relationship_analytics.rs (cadence + losing_touch/waiting_on_you) over a 6-week read_local window; writes relationship_state.json for the daily brief + tests
draft-reply/SKILL.md
approval-tiers/
  SKILL.md                           # PORT: approval-policy.ts
  scripts/learn_preferences.py       # PORT: preference-learner.ts + feedback.ts (parity C1)
people/SKILL.md
setup/SKILL.md                       # guided first run: health() check → seed memory+voice → schedule → first brief
_shared/
  references/audio-script-prompt.md  # PORT: audio-brief.ts narration (parity C3)
  scripts/compose_brief.py           # the FLEX extraction engine + critic + tap-links + escalation (PORT: gemini-flex.ts/brief-critic.ts/generate.ts)
  scripts/correlate_signals.py       # PORT: signals.ts (cross-source matchings)
  scripts/style_extract.py           # PORT: style-profile.ts (fingerprint v2)
  scripts/style_apply.py             # PORT: style-profile.ts formatStyleForWorker (verbatim sample injection)
  scripts/log_outcome.py             # outcomes + analytics (parity C2)
  scripts/action_links.py            # deep-link / tap-to-send URL builder
tests/                               # pytest: parity fixtures in → expected exhaust out
```

## Rules
- Every `SKILL.md` is valid agentskills (frontmatter `name`+`description`, `requires_toolsets`).
- Python scripts read/write the exhaust at `$SOTTO_DATA` (the Hermes volume); keep person/company `.md` + `style.json` schemas **byte-compatible** with today's Sotto files.
- Ship parity tests with fixtures for every script (cite the ported source file in a header comment).
