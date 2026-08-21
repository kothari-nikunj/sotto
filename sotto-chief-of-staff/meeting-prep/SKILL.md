---
name: sotto-meeting-prep
description: Use when the user says "prep me for my meetings" / "who am I meeting" / "meeting prep" / "brief me on my calendar" / "who's in my meetings today/this week", NAMES one person or meeting ("prep Spencer", "prep me for my 2:15"), says YES to a nudge that offered prep for a specific meeting, asks about an upcoming attendee, or when the Bridge pushes a meeting_prep trigger — prep the user for the people in their meetings ahead. ONE meeting in play — a named person, a named time, or a yes to a prep offer — is a deep dive on that meeting alone (their thread with you first, then what the company builds, the founder, traction, the space, then angles) and lists no other meetings; only "prep my meetings" gets the compact sweep with research, context, and talking points for every meeting with outside people in the next 3 days. This is THE way to produce meeting prep — never hand-write attendee notes instead of running this skill.
metadata:
  hermes:
    tags: [meeting-prep, chief-of-staff, sotto]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace]
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

**Two modes, one pipeline — and depth is the default.** If the user is asking about ONE meeting —
they named a person or a time, or they're answering an offer to prep a specific meeting — that is a
deep dive on that meeting alone; the compact sweep is only for "prep my meetings" across the days
ahead. In the deep-dive case run the same steps below and pass `--focus "<the name>"` (or their
email) to steps 2 and 3: that one person gets an extra grounded call covering their company and
space, and the prep becomes a deep dive on their soonest meeting — their thread with you first,
angles last. A `--focus` that matches nobody on the calendar ahead falls back to the sweep on its
own; never invent a match.

**Where the name comes from when they just said "yes".** A proactive nudge that offered prep NAMED
the person ("You're meeting Spencer Kim in ~40 min — want me to pull full prep on him?"), so
"yes" / "sure" / "do it" means `--focus "Spencer Kim"` — the name is in the offer you just sent,
not something to ask for again. Same for "prep me for my 2:15": focus on whoever is on that meeting.
A yes to a prep offer is NEVER the sweep, and a focused prep never lists the user's other meetings.
If the name is genuinely lost, the meeting the offer was about is the SOONEST one with outside
people on `/tmp/sotto_cal.json` (the nudge only fires inside a ~45-minute lead window) — focus on
its first external attendee and say in one line which meeting you're prepping; never ask "which
meeting?" and never fall back to the sweep.

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
> - Your host's session/recall search (if it has one) is for continuity (open loops, "last time you discussed X"), **never** for
>   inferring who someone is. A familiar-sounding fact you can't point to in the gathered data is a guess.
> - If you didn't run research and have no graph entry, deliver a **bare schedule** — time, meeting
>   title, and the attendee names + grounded company only — and say "I don't have backgrounds on them
>   yet; restart the Bridge or ask me to research them." That honest line beats a confident wrong bio.

## Procedure

> **Script paths:** every script lives under `$HOME/.hermes/skills/sotto/`. Use the **absolute** path,
> e.g. `python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/compose_meeting_prep.py"`.

1. **Gather** — Calendar deterministically, the rest as usual:
   - **Calendar (next 3d)** — `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --skip-gmail` → writes `/tmp/sotto_cal.json` (already in the shape this skill expects) and prints `[gather_google] 0 emails, M events …`. **Host-agnostic fallback:** if it says the CLI **isn't this host's Google path** (`google_api.py not found … FALLBACK …`), the host may have Calendar as an **MCP** — list the next 3 days with the host's Calendar tool, dump the raw result to `/tmp/sotto_cal_raw.json`, then normalize: `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --skip-gmail --from-mcp-calendar /tmp/sotto_cal_raw.json`. Only if neither exists is Calendar unavailable — say so, don't hand-fetch.
   - **`read_local`** → `/tmp/sotto_local.json`, **`knowledge_query.py --calendar /tmp/sotto_cal.json`** → `/tmp/sotto_know.json` (run after the calendar gather — the flag packs everyone on the calendar ahead even if their graph file hasn't been touched in weeks).
   - **Granola (with transcripts)** — deterministic, ONE command (don't hand-fetch via MCP tools): `execute_code` →
     `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_granola.py" --days 30 --transcripts-since-hours 168`
     It reads the Granola connector token from `/setup` (or falls back to `GRANOLA_API_KEY` REST) and writes `/tmp/sotto_granola.json` as `{"meetings":[{meeting_id, title, start, end, date, time, attendee_emails, your_notes, ai_summary[, transcript]}]}` — 30 days of history with the same people, plus full **transcripts** for meetings from the last week. The transcript is what makes prep deep — *"last time you met, you committed to X / they pushed back on Y"* — not just a one-line summary; older history still arrives as `ai_summary`. If it warns Granola isn't connected, it writes an empty file — prep from the knowledge graph instead.
2. **Research attendees — ONE batched, grounded command** (ported from the Mac's `gemini-research.ts`). `execute_code` → `python3 "$HOME/.hermes/skills/sotto/morning-brief/scripts/select_attendees.py"` with `{google, local}` to get the external attendees worth researching (excludes you, same-domain colleagues, people already in the graph; capped at 25). Write them to `/tmp/sotto_research_in.json`. Then gather the user's **own email threads with each attendee** — this is what marries web research with private context (bios alone are half a prep):
   `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/gather_google.py" --attendee-comms /tmp/sotto_research_in.json --comms-out /tmp/sotto_attendee_comms.json`
   (per attendee it searches Gmail `from:<email> OR to:<email> newer_than:30d` and writes `{email: [{date,subject,snippet,from_me}]}`; without Google it degrades to an empty file and the prep still runs). Then **skip the people the graph already knows fresh** (researched < 30 days ago — no point paying for the same lookup twice):
   `python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/persist_prep.py" --filter-fresh /tmp/sotto_research_in.json`
   **In focus mode add `--keep "<the name the user said>"`** — the focused person survives the freshness filter even if the morning brief researched them yesterday, because that run produced no `company_deep` and the deep dive needs one. Everyone else is still skipped, so focus costs one extra call, not a re-research of the calendar.
   (rewrites the file in place, dropping only people whose graph profile carries a `last_researched` stamp < 30 days old — the stamp step 4's persist writes; profiles without the stamp, including legacy ones, are re-researched once. Prints `{kept, skipped_fresh}` — skipped people still get prepped from the graph in step 3). Then research whoever's left:
   `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/research_attendees.py" --attendees /tmp/sotto_research_in.json --context /tmp/sotto_cal.json --comms /tmp/sotto_attendee_comms.json --out /tmp/sotto_research.json` (the script writes `--out` itself — no shell redirect; `--comms` feeds the per-attendee threads just gathered — 30 days of real relationship context per attendee, a strictly better disambiguation signal than the morning's 24h global gmail file; the script tolerates a missing/empty file.)
   **In focus mode add `--focus "<the name the user said>"`** to that same `research_attendees.py` command: it fires ONE additional grounded call for that ONE person covering what their company builds, the founder's background and origin story, traction signals, and the market landscape / why now, returned as `company_deep` on their entry. It never fans out — one person, one extra call — and `SOTTO_RESEARCH_DEEP=0` disables it along with the recency sweep.
   **Run this command even when `kept` is 0** — with an empty list it writes a fresh `{"attendees":[]}` to `/tmp/sotto_research.json`, which is what stops the afternoon prep from silently reusing the MORNING brief's research file as stale bios.
   It batches 5 attendees per Gemini Search-Grounding call (concurrent), dedupes, caps at 25, and returns `{attendees:[{email,title,company,relevance,summary}]}`, grounded in real web results — uses the Google key you already have, no extra key. If empty, pass `attendee_research: []` — known attendees still get prepped from the knowledge graph. Research only — never draft or schedule here.
3. **Compose — this step IS the prep. Run ONE command; do not write the prep yourself.** Save each source to a temp file, then run the script:
   1. Calendar (next 3d) → `/tmp/sotto_cal.json`  ·  `read_local` → `/tmp/sotto_local.json`  ·  `knowledge_query.py` output → `/tmp/sotto_know.json`  ·  Granola → `/tmp/sotto_granola.json`  ·  attendee research (step 2) → `/tmp/sotto_research.json`  ·  attendee comms (step 2) → `/tmp/sotto_attendee_comms.json`
   2. `execute_code` (absolute path):
      ```bash
      python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/compose_meeting_prep.py" \
        --calendar /tmp/sotto_cal.json --local /tmp/sotto_local.json \
        --knowledge /tmp/sotto_know.json --granola /tmp/sotto_granola.json \
        --attendee-research /tmp/sotto_research.json \
        --attendee-comms /tmp/sotto_attendee_comms.json
      ```
      **In focus mode append `--focus "<the name the user said>"`** — the composer then preps only that person's soonest upcoming meeting (ambiguity → the soonest, and the prep says so) and switches to the FOCUSED PREP prompt variant: their thread with you first, then what the company builds / the founder / traction / the space, then angles. Without the flag the sweep is byte-identical to before.
      It prints JSON: `prep_markdown` + `meetings[]` (each with `attendees` and `talking_points`). `--attendee-comms` is what surfaces the user's PRIVATE context per attendee — their real email threads (`thread:`), texts (`text:`), open continuity loops (`loop:`), and last meeting together (`granola:`); the prompt REQUIRES a "Your thread:" line whenever that context exists, so never omit the flag when the file was gathered.
   3. Deliver `prep_markdown` **verbatim**. It's already one skimmable message, in Sotto's voice, with talking points per meeting.
   - *Native fallback (only if `execute_code` is unavailable):* `read_file` `references/meeting-prep-prompt.md` and run it with your model over the same assembled inputs — the file carries both output variants, fenced by `<!-- SWEEP OUTPUT … -->` and `<!-- FOCUSED PREP … -->` markers; use exactly one (FOCUSED PREP when the user named a person).
4. **Persist the research (so nobody gets re-researched next run).** `execute_code`:
   ```bash
   python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/persist_prep.py" \
     --research /tmp/sotto_research.json --attendees /tmp/sotto_research_in.json
   ```
   It writes each researched attendee's title/company/summary into the knowledge graph as clearly-sourced, LOW-confidence (0.55) "Per web search: …" facts that decay — **grounded in the research output only, never invented** (same pattern as setup's `prewarm_graph.py`; the brief's Learn step promotes facts as they're confirmed). Attendees the research returned nothing for are skipped entirely. Idempotent; if research was empty this is a no-op.
   **The same command persists what it learned about the COMPANY to the company** (`knowledge/companies/<slug>.md`): the focus pass's deep dive — what it builds, the founder story, the market — becomes that company's `## About`, and each source-URLed traction signal a `## News` line. That is why the deep dive you pay for once is there for the *next* person from that company, and why the next `--focus` run is asked only for what's new. A persistence failure is reported in the output and never fails the prep.
5. **Deliver** — send `prep_markdown` as **Sotto** (never "Hermes Agent"). It is already chat-ready (every `<!--…-->` marker stripped, bold in WhatsApp `*single-asterisk*` syntax) — do not re-format it. Where a meeting link or attendee `mailto:`/`https://wa.me/` helps, include the real URL inline.

## Notes
- **The deep dive opens with the private thread.** Who they are and how you're connected — intro
  chain, quotes, past meetings, open ledger items — is the part no web search can produce, so it
  leads. Depth follows focus: six sections × eight meetings is unreadable, one meeting earns it.
- **Every suggested angle fuses a private fact with a researched one where both exist.** An angle
  that only restates research is a question anyone could ask; one that only restates the thread is
  small talk. When one side is genuinely empty, angles from the other alone are fine — the missing
  half is never fabricated.
- **External only.** Internal/solo meetings are intentionally excluded — this is about the outside
  people you're meeting, not your whole calendar. If there are none in the next 3 days, the script
  says so in one line.
- **Never invent.** If someone has no public profile and nothing in the graph, the prep says "no
  background found — worth a quick intro question" rather than guessing a role or employer.
- **Research persists (step 4), extraction doesn't.** This skill writes ONLY the grounded attendee
  research into the graph (low-confidence, decaying, "Per web search" facts) — the brief's Learn step
  remains the sole writer of extracted knowledge. The freshness filter (step 2) and this persist are
  the same bookends the morning brief now runs, so the rule is global: **an external attendee is
  researched at most once per 30 days, and every research run feeds the graph.**
