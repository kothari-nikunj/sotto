# Attendee Research — two-pass grounded pipeline (reference documentation)

> REFERENCE ONLY — the live prompts are inlined in `_shared/scripts/research_attendees.py`
> (`_build_prompt` for Pass A, `_build_deep_prompt` for Pass B); this file documents how they work
> and the contract their output honors, the same way `morning-brief/SKILL.md` describes the step.
> PORT SOURCE: `api/src/services/gemini-research.ts` (the Mac backend's `researchBatch` prompt),
> upgraded with a recency-and-texture second pass. For an ad-hoc one-off grounded lookup use
> `_shared/scripts/web_research.py`.

## When it runs
During the morning brief and meeting prep, after gathering the calendar. Only the EXTERNAL
attendees that `scripts/select_attendees.py` returns are researched (it already excludes the user,
same-domain colleagues, and known contacts, and only looks at meetings in the next 72h — so the
expensive Pass B is naturally horizon-gated). Capped at 25 people. Meeting prep additionally skips
people whose graph profile carries a `last_researched` stamp < 30 days old
(`persist_prep.py --filter-fresh`).

## The two passes (both batched, both grounded, run concurrently)

### Pass A — profile (batch of 5, 60s/batch)
Who they are: `email` (join key, verbatim), `title` (null if unfound), `company`, `relevance`
(1–2 bullets keyed to the meeting context), `summary` (3–4 sentence professional bio), and
`company_summary` (1–2 sentences on what their company does).

**Degradation ladder — person → company → nothing.** Every corporate email domain gets a company
search even when the person has no public profile: the entry comes back as
`summary="No public profile found."` PLUS a filled `company`/`company_summary`, so the brief can
say "Cobalt Research does X; their role isn't public" instead of "No background found". A truly
empty entry is reserved for freemail addresses (gmail/outlook/…) with unsearchable names.

**Comms + graph context in.** Each attendee line can carry what the user already knows (packed
graph facts) and recent comms with the user (subjects/snippets from the already-gathered gmail
file, passed via `--comms /tmp/sotto_gmail.json`). The prompt uses them ONLY to disambiguate which
person to research and to sharpen `relevance` — never as facts to restate, never to invent an
interaction that isn't there.

### Pass B — recency & texture sweep (batch of 3, 90s/batch, its own token budget)
The quality upgrade: a separate grounded call that hunts the last `DEFAULT_RECENCY_DAYS`
(90) days for what is genuinely NEW — the model is instructed to run MULTIPLE distinct
searches per person (`"[Name] blog"`, `"[Name] podcast OR talk"`, `"[Name] Twitter OR X OR
LinkedIn"`, `"[Name] [Company] <year>"`) and return:

- **recent_activity** (≤4) — `{when, what, source_url}`: a new blog/Substack/Medium post they
  wrote, a podcast/talk/interview appearance, an X/LinkedIn thread with real traction, a launch,
  funding, job change, or press. `when` may be approximate ("late July 2026"); `source_url` is
  REQUIRED.
- **personal** (≤2) — PUBLIC personal texture they shared themselves (ran a marathon, wrote about
  a hobby, moved cities), each string ending with its source URL. Tasteful only: nothing about
  health, politics, or family members who didn't post it themselves.

Facts only: the sweep is never asked what to SAY. Openers, angles and talking points are situational
— they cost output tokens to invent, they restate the fact they point at, and they could never be
persisted. The prep prompt writes them fresh from these facts each time.

**Novelty relative to the graph.** Each person's existing packed facts (their `known` string from
the knowledge graph / `knowledge_query.py --person`) are injected with *"do NOT repeat any of
this — return only what is new relative to it"*, so the sweep surfaces beyond-the-resume material
instead of re-finding what the user already knows.

Env knobs: `SOTTO_RESEARCH_DEEP=0` disables Pass B entirely. The window itself is the named
constant `DEFAULT_RECENCY_DAYS` in `research_attendees.py`, not an env var.

## Grounding rules (enforced in BOTH prompts, and deterministically in code)
- Stay factual. Use only what public sources actually say — never guess a title, employer, or
  funding stage. A thin-but-accurate entry beats a confident wrong one.
- **No unsourced numbers.** Do NOT state specific dollar figures — ARR, valuation, raise size,
  acquisition/exit price — unless they appear in the grounded result's **citations**. A named stage
  ("raised a Series A") is fine if reported; "$2.4M ARR" or "a $115M exit" is allowed ONLY if a citation
  backs it — otherwise omit the number (write "acquired by X", not "acquired by X for $Y"). These figures
  get written into the user's permanent knowledge graph, so an invented one persists — when unsure, leave
  it out. (This rule is carried verbatim into both live prompts.)
- **Deterministic post-filters** (in `research_attendees.py`, not just the prompt): a
  `recent_activity` item without a real `source_url` is dropped; a `personal` item without an
  explicit public source URL in the string is dropped. "If nothing genuinely recent exists, return
  empty lists — an empty list beats a stretch."
- One entry per attendee, no duplicates. Preserve the input email verbatim — it is the join key.
- This is research only: no drafting, scheduling, or actions.
- If search is unavailable, the script returns `[]` — nothing is fabricated.

## Output
One JSON object, passed straight into `compose_brief` / `compose_meeting_prep` as the
`attendee_research` input:

```json
{
  "attendees": [
    {
      "email": "taylor@startup.com",
      "title": "Co-founder & CEO",
      "company": "Startup Inc.",
      "relevance": ["Raising a Series A in the space you invest in"],
      "summary": "Co-founder and CEO of Startup Inc., a developer-tools company focused on CI/CD. Previously a staff engineer at BigCo and an early PM at MidCo.",
      "company_summary": "Startup Inc. builds CI/CD pipelines for monorepos; ~40 engineering-team customers, seed-funded in 2024.",
      "recent_activity": [
        {"when": "late July 2026", "what": "Published \"Agent memory is a database problem\" on his Substack.", "source_url": "https://taylor.substack.com/p/agent-memory"}
      ],
      "personal": ["Ran the SF Marathon and posted his splits (https://x.com/taylor/status/123)"]
    }
  ]
}
```

## Persistence & provenance (`meeting-prep/scripts/persist_prep.py`)
The bio (conf 0.55) and each dated+sourced `recent_activity`/`personal` item (conf 0.6,
`source_ref` = the item's URL) are written to the knowledge graph as "Per web search: …" facts
labeled `source: "web_research"` — never as authoritative identity fields.

The focus pass's `company_deep` persists too, but to the **company** file
(`knowledge/companies/<slug>.md`): what it builds / the founder story / the market become the
company's `## About`, and each source-URLed traction signal becomes a `## News` line. See
`docs/ARCHITECTURE.md` § the research loop.
