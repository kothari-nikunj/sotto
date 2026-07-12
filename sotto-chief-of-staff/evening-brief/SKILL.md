---
name: sotto-evening-brief
description: Use when the user says "good evening", asks for their evening brief / end-of-day wrap / "how did today go" / "what's still open", when it's evening-brief time, or when the Bridge pushes an evening_ready trigger — produce the user's evening briefing. This is THE way to produce any evening brief — never hand-write a summary instead of running this skill.
metadata:
  hermes:
    tags: [brief, chief-of-staff, sotto]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace, granola]
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
Follow `sotto-morning-brief` steps 1–6 (including step 6's Deliver + the deliver-once marker claim), but:
- **Pass `type:"evening"`** to the extraction (not `"morning"`). This is what turns on the evening-only **Evening Accountability** section — checking this morning's commitments against today's data for follow-through. With the wrong type that section silently disappears.
- **Emphasize open loops**: lead with what's *still open* from `continuity_resolve.py` (active items, sorted by how long they've been waiting / `times_surfaced`).
- **Close the day**: surface what got handled, what slipped, and what's queued for tomorrow.
- Keep tomorrow's first meetings + prep visible.

Deliver as Sotto. Honor `sotto-approval-tiers` for any actions.
