---
name: sotto-evening-brief
description: Use when the user says "good evening", asks for their evening brief / end-of-day wrap / "how did today go" / "what's still open", when it's evening-brief time, or when the Bridge pushes an evening_ready trigger — produce the user's evening briefing. This is THE way to produce any evening brief — never hand-write a summary instead of running this skill.
metadata:
  hermes:
    tags: [brief, chief-of-staff, sotto]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace]
    requires_tools: [execute_code, terminal]
required_environment_variables:
  - name: GOOGLE_AI_API_KEY
    prompt: Gemini API key (for the brief extraction)
    help: https://aistudio.google.com/apikey
    required_for: brief extraction
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: knowledge graph + continuity
---

# Sotto — Evening Brief

Same machinery as `sotto-morning-brief`, with an end-of-day lens. The same **CRITICAL** rule applies:
the brief comes from `compose_brief.py` — do not improvise a freeform recap. Deliver as **Sotto**,
never "Hermes Agent".

## Procedure
Follow `sotto-morning-brief` steps 1–6 (including step 6's Deliver: claim the deliver-once gate first with `brief_marker.py --claim evening` — if it prints `already`, STOP, the other path delivered today's evening brief — then send `brief_text` with tap-links, as in the morning skill), but:
- **Pass `--type evening`** to `compose_brief.py` (not `morning`). This is what turns on the evening-only **Evening Accountability** section — checking this morning's commitments against today's data for follow-through. With the wrong type that section silently disappears.
- **Emphasize open loops**: nothing extra to run — `compose_brief.py` already reads the ledger itself (via `ledger_io`) and renders the open loops into the extraction. Do NOT run `loops_query.py` here and do not add its output to the brief: the composer owns that section, and a second copy pasted in is a second version of the truth.
- **The two continuity passes apply here unchanged** (morning steps 3 and 4.2): `continuity_resolve.py --resolve-only` runs BEFORE `compose_brief.py` so the evening brief reasons about loops closed by today's data, and `--merge-only` runs after it in the Learn step to record the brief's own items. Both write the ledger, so each runs exactly once — never as a "read".
- **Close the day**: surface what got handled, what slipped (the evening-only **Still Pending**
  section), and what's queued for tomorrow.
- **Don't re-tell today's nudges.** The composer reads today's delivered nudges from the surfaced
  ledger and renders an **Already Nudged Today** prompt section; the extraction compresses each
  still-open one to a single line naming when it was sent ("nudged you at 3:05 — Sarah's ask") and
  never leads with it. That is automatic — you add nothing.
- **"What moved today" is written by the composer, not by you.** `compose_brief.py` appends a
  deterministic **What moved today** block to the evening brief — read from the record (chases
  delivered, loops closed, interruptions held, people prepped, follow-ups offered), naming who moved
  what, and rendered only for the counts that are nonzero. It reports outcomes, never throughput, and
  a day where nothing moved gets no block at all. Do not write your own version of it.
- **The trailing "make that a standing rule?" question is composer-written too.** When today's
  transcripts showed the user stating a durable rule, `compose_brief.py` appends at most ONE
  `Heard you say: "…" — make that a standing rule?` question and records a `pending_offer` of kind
  `procedure`. Leave it in place — never strip it as an "extra section". The user's later "yes" is
  handled by the gateway persona (it writes the rule to the master file via `master_file.py` and
  clears the offer); nothing for this run to do beyond delivering the brief verbatim.
- **A new Sotto version is one composer-written line too.** When the daily update check finds a newer
  published build, `compose_brief.py` adds ONE line to whichever brief comes next (morning or evening)
  and never mentions that version again. It is housekeeping — never a separate message, never a nudge.
- **The open-items contract holds here too** (see `sotto-morning-brief`'s Notes): only an **urgent**
  open loop — overdue, due within 24 hours, or already chased with no answer — earns its own line,
  exactly once, as an ask with its age or in Already Handled with today's evidence. Every other open
  loop is ONE quiet count line pointing at `/app#loops`; the composer appends the urgent misses (one
  line per person) and that count. Never write your own list of what is still open — the evening
  brief that inventoried 14 rows for 7 real debts is the bug this rule closes. A quiet evening says
  so in one line instead of omitting the sections.
- **Granola gather (step 1) is the same one command** — `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_granola.py"` → `/tmp/sotto_granola.json`. Its default `--transcripts-since-hours 36` already covers today's ended meetings, which is what feeds the merged followup pass below — don't fetch Granola via MCP tools by hand.
- **The evening brief is the ONE end-of-day report — it carries the post-meeting followup content.**
  The standalone 16:45 followup cron is retired; the same commitments/drafts extraction
  (`sotto-followup`'s `compose_followup.py` pass over meetings that ended today) feeds the evening
  pipeline, so commitments still land in the continuity ledger and drafts are offered here. Do not
  ALSO run `sotto-followup` as a separate message at brief time.
- Keep tomorrow's first meetings + prep visible.

Deliver as Sotto. Honor the approval tiers (`_shared/references/approval-tiers.md`) for any actions.
