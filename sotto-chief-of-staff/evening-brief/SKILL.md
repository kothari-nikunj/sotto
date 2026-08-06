---
name: sotto-evening-brief
description: Use when the user says "good evening", asks for their evening brief / end-of-day wrap / "how did today go" / "what's still open", when it's evening-brief time, or when the Bridge pushes an evening_ready trigger — produce the user's evening briefing. This is THE way to produce any evening brief — never hand-write a summary instead of running this skill.
metadata:
  hermes:
    tags: [brief, chief-of-staff, sotto]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace]
    requires_tools: [execute_code]
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
run the FLEX extraction in `sotto-morning-brief`'s `references/extraction-prompt.md` — do not improvise
a freeform recap. Deliver as **Sotto**, never "Hermes Agent".

## Procedure
Follow `sotto-morning-brief` steps 1–6 (including step 6's Deliver: claim the deliver-once gate first with `brief_marker.py --claim evening` — if it prints `already`, STOP, the other path delivered today's evening brief — then send `brief_text` with tap-links, as in the morning skill), but:
- **Pass `--type evening`** to `compose_brief.py` (not `morning`). This is what turns on the evening-only **Evening Accountability** section — checking this morning's commitments against today's data for follow-through. With the wrong type that section silently disappears.
- **Emphasize open loops**: lead with what's *still open* — read it with the read-only view `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/loops_query.py"` (returns `{you_owe:[…], waiting_on_them:[…], counts}`, oldest/most-overdue first). Never use `continuity_resolve.py` as the read — it WRITES the ledger (resolves/ages/expires loops); it runs exactly once, in the Learn step.
- **Close the day**: surface what got handled, what slipped (the evening-only **Still Pending**
  section), and what's queued for tomorrow.
- **Granola gather (step 1) is the same one command** — `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_granola.py"` → `/tmp/sotto_granola.json`. Its default `--transcripts-since-hours 36` already covers today's ended meetings, which is what feeds the merged followup pass below — don't fetch Granola via MCP tools by hand.
- **The evening brief is the ONE end-of-day report — it carries the post-meeting followup content.**
  The standalone 16:45 followup cron is retired; the same commitments/drafts extraction
  (`sotto-followup`'s `compose_followup.py` pass over meetings that ended today) feeds the evening
  pipeline, so commitments still land in the continuity ledger and drafts are offered here. Do not
  ALSO run `sotto-followup` as a separate message at brief time.
- Keep tomorrow's first meetings + prep visible.

Deliver as Sotto. Honor `sotto-approval-tiers` for any actions.
