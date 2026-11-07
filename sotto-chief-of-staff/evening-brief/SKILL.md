---
name: sotto-evening-brief
description: Use when the user says "good evening", asks for their evening brief / end-of-day wrap / "how did today go" / "what's still open", when it's evening-brief time, or when the Bridge pushes an evening_ready trigger — produce the user's evening briefing. This is THE way to produce any evening brief — never hand-write a summary instead of running this skill.
metadata:
  tags: [brief, chief-of-staff, sotto]
  category: productivity
---

# Sotto — Evening Brief

Same machinery as `sotto-morning-brief`, with an end-of-day lens. The same **CRITICAL** rule applies:
the brief comes from the receiver's composer — do not improvise a freeform recap. Deliver as **Sotto**.

## How this runs on Sotto
The evening brief is **receiver-owned**, exactly like the morning one (see `sotto-morning-brief`'s
"How this runs on Sotto"): the receiver's evening schedule or the `owner_brief_request` tool
with kind evening runs the same pipeline, including its deliver-once gate. For chat requests,
call the tool and stop after the receipt; never promise a queued brief without it or compose a
replacement recap yourself. The evening pass
differs in these ways:
- **It composes with the evening type.** That is what turns on the evening-only **Evening
  Accountability** section — checking this morning's commitments against today's data for
  follow-through. With the wrong type that section silently disappears.
- **Emphasize open loops**: nothing extra to run — the composer already reads the ledger itself (via `ledger_io`) and renders the open loops into the extraction. Do not add a `loops-query` read to the brief: the composer owns that section, and a second copy pasted in is a second version of the truth.
- **The two continuity passes apply here unchanged** (morning steps 3 and 4): the resolve-only pass runs BEFORE composition so the evening brief reasons about loops closed by today's data, and the merge pass runs after it inside the Learn step (one step, one receipt) to record the brief's own items. Both write the ledger, so each runs exactly once — never as a "read".
- **Close the day**: surface what got handled, what slipped (the evening-only **Still Pending**
  section), and what's queued for tomorrow.
- **Don't re-tell today's nudges.** The composer reads today's delivered nudges from the surfaced
  ledger and renders an **Already Nudged Today** prompt section; the extraction compresses each
  still-open one to a single line naming when it was sent ("nudged you at 3:05 — Sarah's ask") and
  never leads with it. That is automatic — you add nothing.
- **"What moved today" is written by the composer, not by you.** The composer appends a
  deterministic **What moved today** block to the evening brief — read from the record (chases
  delivered, loops closed, interruptions held, people prepped, follow-ups offered), naming who moved
  what, and rendered only for the counts that are nonzero. It reports outcomes, never throughput, and
  a day where nothing moved gets no block at all. Do not write your own version of it.
- **The trailing "make that a standing rule?" question is composer-written too.** When today's
  transcripts showed the user stating a durable rule, the composer appends at most ONE
  `Heard you say: "…" — make that a standing rule?` question and records a `pending_offer` of kind
  `procedure`. It stays in place — never stripped as an "extra section". The user's later "yes" is
  `run_sotto_script master-file accept-offer` in chat (it writes only the offered rule to the master
  file and clears the offer); nothing for the brief run to do beyond delivering the brief verbatim.
- **A new Sotto version is one composer-written line too.** When the daily update check finds a newer
  published build, the composer adds ONE line to whichever brief comes next (morning or evening)
  and never mentions that version again. It is housekeeping — never a separate message, never a nudge.
- **The open-items contract holds here too** (see `sotto-morning-brief`'s Notes): only an **urgent**
  open loop — overdue, due within 24 hours, or already chased with no answer — earns its own line,
  exactly once, as an ask with its age or in Already Handled with today's evidence. Every other open
  loop is ONE quiet count line pointing at `/app#loops`; the composer appends the urgent misses (one
  line per person) and that count. Never write your own list of what is still open — the evening
  brief that inventoried 14 rows for 7 real debts is the bug this rule closes. A quiet evening says
  so in one line instead of omitting the sections.
- **The Granola gather (step 1) is the same** — its default 36-hour transcript window already covers today's ended meetings, which is what feeds the merged followup pass below.
- **The evening brief is the ONE end-of-day report — it carries the post-meeting followup content.**
  The standalone 16:45 followup job is retired; the same commitments/drafts extraction
  (`sotto-followup`'s pass over meetings that ended today) feeds the evening pipeline, so
  commitments still land in the continuity ledger and drafts are offered here. Do not ALSO run
  `sotto-followup` as a separate message at brief time.
- Keep tomorrow's first meetings + prep visible.

Deliver as Sotto. Honor the approval tiers (`_shared/references/approval-tiers.md`) for any actions.
