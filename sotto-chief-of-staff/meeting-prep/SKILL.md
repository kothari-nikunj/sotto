---
name: sotto-meeting-prep
description: Use when the user says "prep me for my meetings" / "who am I meeting" / "meeting prep" / "brief me on my calendar" / "who's in my meetings today/this week", asks about an upcoming attendee, or when the Bridge pushes a meeting_prep trigger — prep the user for the people in their meetings ahead. Produces ONE message — attendee research, context, and talking points for every meeting with outside people in the next 3 days. This is THE way to produce meeting prep — never hand-write attendee notes instead of running this skill.
metadata:
  hermes:
    tags: [meeting-prep, chief-of-staff, sotto]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace, granola]
    requires_tools: [execute_code]
required_environment_variables:
  - name: GOOGLE_AI_API_KEY
    prompt: Gemini API key (for the prep synthesis)
    help: https://aistudio.google.com/apikey
    required_for: meeting-prep synthesis
  - name: SOTTO_DATA
    prompt: Path to the Sotto exhaust volume (e.g. /data)
    required_for: knowledge graph (prior context on attendees)
---

# Sotto — Meeting Prep

Produce **one** message that preps the user for the people in their calendar ahead: who each external
attendee is, the context that matters, and concrete talking points. This is the standalone version of
the brief's attendee research — same machinery, but focused entirely on the meetings ahead (the next
3 days) and delivered as a single prep brief, not folded into a morning/evening brief.

> **CRITICAL — do not improvise.** The prep MUST come from the extraction script (step 3). Do NOT
> hand-write attendee bios or guess at people's roles — the script assembles real research + the
> knowledge graph + past meeting notes, and the prompt forbids inventing facts. Deliver as **Sotto**,
> in Sotto's voice — never "Hermes Agent".
>
> **NO GROUNDED ROLE → NO ROLE.** This applies even to a quick "who am I meeting tomorrow" answer.
> The ONLY facts you may state about a person are ones that came from real attendee research (step 2)
> or the knowledge graph. You may NOT assert a title ("founder", "CEO", "partner") or characterize a
> company ("VC firm", "Alive *Ventures*") from the event title, the email domain, or memory. Concretely:
> - Company name = **exactly** what the event title's parenthetical or email domain gives. `alive.inc`
>   → "Alive" (never "Alive Ventures"); `(Browserbase)` → "Browserbase" with **no** role attached.
> - `session_search`/recall is for continuity (open loops, "last time you discussed X"), **never** for
>   inferring who someone is. A familiar-sounding fact you can't point to in the gathered data is a guess.
> - If you didn't run research and have no graph entry, deliver a **bare schedule** — time, meeting
>   title, and the attendee names + grounded company only — and say "I don't have backgrounds on them
>   yet; restart the Bridge or ask me to research them." That honest line beats a confident wrong bio.

## Inputs
- **Calendar** — next 3 days, fetched **deterministically** (don't hand-fetch — that's the #1 cause of an
  empty prep). (The horizon is 72h; only meetings with **external** attendees — anyone outside the user's
  email domain — are prepped.)
- **Local context** — `read_local` from the Bridge (for Apple Contacts + Granola notes + the
  knowledge graph). Optional but recommended; skip if the Bridge is unreachable.
- **Prior knowledge** — `knowledge_query.py` output (what the user already knows about attendees).
- **Granola** — past meeting notes (history with the same people).

## Procedure

> **Script paths:** every script lives under `$HOME/.hermes/skills/sotto/`. Use the **absolute** path,
> e.g. `python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/compose_meeting_prep.py"`.

1. **Gather** — Calendar deterministically, the rest as usual:
   - **Calendar (next 3d)** — `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --skip-gmail` → writes `/tmp/sotto_cal.json` (already in the shape this skill expects) and prints `[gather_google] 0 emails, M events …`. **Host-agnostic fallback:** if it says the CLI **isn't this host's Google path** (`google_api.py not found … FALLBACK …`), the host may have Calendar as an **MCP** — list the next 3 days with the host's Calendar tool, dump the raw result to `/tmp/sotto_cal_raw.json`, then normalize: `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --skip-gmail --from-mcp-calendar /tmp/sotto_cal_raw.json`. Only if neither exists is Calendar unavailable — say so, don't hand-fetch.
   - **`read_local`** → `/tmp/sotto_local.json`, **`knowledge_query.py`** → `/tmp/sotto_know.json`.
   - **Granola (with transcripts)** → `/tmp/sotto_granola.json`. Via the Granola MCP: list recent meetings, and **for each external attendee in today's meetings, fetch the full TRANSCRIPT** of your most recent meeting with them (the MCP's get-transcript tool). Write `[{title, date, attendee_emails, ai_summary, transcript}]`. The transcript is what makes prep deep — *"last time you met, you committed to X / they pushed back on Y"* — not just a one-line summary. If the Granola MCP isn't configured, skip and prep from the knowledge graph.
2. **Research attendees — ONE batched, grounded command** (ported from the Mac's `gemini-research.ts`). `execute_code` → `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/select_attendees.py"` with `{google, local}` to get the external attendees worth researching (excludes you, same-domain colleagues, people already in the graph; capped at 25). Write them to `/tmp/sotto_research_in.json`, then **skip the people the graph already knows fresh** (researched < 30 days ago — no point paying for the same lookup twice):
   `python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/persist_prep.py" --filter-fresh /tmp/sotto_research_in.json`
   (rewrites the file in place, dropping anyone whose `people/*.md` is < 30 days old with a company/title or prior research fact; prints `{kept, skipped_fresh}` — skipped people still get prepped from the graph in step 3). Then research whoever's left:
   `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/research_attendees.py" --attendees /tmp/sotto_research_in.json --context /tmp/sotto_cal.json` → `/tmp/sotto_research.json`.
   It batches 5 attendees per Gemini Search-Grounding call (concurrent), dedupes, caps at 25, and returns `{attendees:[{email,title,company,relevance,summary}]}`, grounded in real web results — uses the Google key you already have, no extra key. If empty, pass `attendee_research: []` — known attendees still get prepped from the knowledge graph. Research only — never draft or schedule here.
3. **Compose — this step IS the prep. Run ONE command; do not write the prep yourself.** Save each source to a temp file, then run the script:
   1. Calendar (next 3d) → `/tmp/sotto_cal.json`  ·  `read_local` → `/tmp/sotto_local.json`  ·  `knowledge_query.py` output → `/tmp/sotto_know.json`  ·  Granola → `/tmp/sotto_granola.json`  ·  attendee research (step 2) → `/tmp/sotto_research.json`
   2. `execute_code` (absolute path):
      ```bash
      python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/compose_meeting_prep.py" \
        --calendar /tmp/sotto_cal.json --local /tmp/sotto_local.json \
        --knowledge /tmp/sotto_know.json --granola /tmp/sotto_granola.json \
        --attendee-research /tmp/sotto_research.json
      ```
      It prints JSON: `prep_markdown` + `meetings[]` (each with `attendees` and `talking_points`).
   3. Deliver `prep_markdown` **verbatim**. It's already one skimmable message, in Sotto's voice, with talking points per meeting.
   - *Native fallback (only if `execute_code` is unavailable):* `read_file` `references/meeting-prep-prompt.md` and run it with your model over the same assembled inputs.
4. **Persist the research (so nobody gets re-researched next run).** `execute_code`:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/persist_prep.py" \
     --research /tmp/sotto_research.json --attendees /tmp/sotto_research_in.json
   ```
   It writes each researched attendee's title/company/summary into the knowledge graph as clearly-sourced, LOW-confidence (0.55) "Per web search: …" facts that decay — **grounded in the research output only, never invented** (same pattern as setup's `prewarm_graph.py`; the brief's Learn step promotes facts as they're confirmed). Attendees the research returned nothing for are skipped entirely. Idempotent; if research was empty this is a no-op.
5. **Deliver** — send `prep_markdown` as **Sotto** (never "Hermes Agent"). If delivering on a **text channel** (WhatsApp/SMS/Telegram), strip any `<!--…-->` markers first (`sed -E 's/<!--[^>]*-->//g'`). Where a meeting link or attendee `mailto:`/`https://wa.me/` helps, include the real URL inline.

## Notes
- **External only.** Internal/solo meetings are intentionally excluded — this is about the outside
  people you're meeting, not your whole calendar. If there are none in the next 3 days, the script
  says so in one line.
- **Never invent.** If someone has no public profile and nothing in the graph, the prep says "no
  background found — worth a quick intro question" rather than guessing a role or employer.
- **Research persists (step 4), extraction doesn't.** This skill writes ONLY the grounded attendee
  research into the graph (low-confidence, decaying, "Per web search" facts) — the brief's Learn step
  remains the sole writer of extracted knowledge. Combined with the step-2 freshness filter, a person
  is researched at most once every ~30 days.
