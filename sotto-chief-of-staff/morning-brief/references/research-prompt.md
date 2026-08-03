# Attendee Research (host-native web search)

> PORT SOURCE: `api/src/services/gemini-research.ts` (the Mac backend's `researchBatch` prompt).
> The Mac app ran this as batched Gemini calls with Google Search grounding. On Hermes the host
> already has native web search and a Gemini model — so **you** run the research directly with your
> web-search tool instead of calling a separate service. Same prompt, same output contract.

## When to run
During the morning brief, after gathering the calendar. Research only the EXTERNAL attendees that
`scripts/select_attendees.py` returns (it already excludes the user, same-domain colleagues, and
people you already know — capped at 25). If that list is empty, skip research entirely.

## Context to ground relevance (optional but recommended)
Before searching, skim the user's **recent emails** (last ~50) and each meeting's **agenda/description**
for any thread that involves the attendee or their company. The Mac backend fed this `ResearchContext`
(recent emails + meeting descriptions) into the researcher so the `relevance` bullets reflect what's
actually live between the user and this person ("their email Tuesday continues the pricing thread"),
not just a generic bio. Use it only to sharpen `relevance`; never invent an interaction that isn't there.

## Task
For EACH attendee, search the web with **grounded, cited** results — prefer
`execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/web_research.py" "<query>"`, which
runs **Gemini Search Grounding** (uses the Google key we already have — no extra key) and returns
`{text, citations:[{title,uri}]}`: the model actually reads the pages, so bios are deeper and every claim
is backed by a citation. (Fall back to your host's `web_search` only if `web_research.py` is unavailable.)
Start with **"[Name] [Company/Domain] LinkedIn"** and **"[Company name] product"** (use the email domain
to identify the company when the name alone is ambiguous). Then return one entry per attendee with exactly
these fields:

- **email** — their email address, exactly as given in the input (this is the join key; do not alter it).
- **title** — current job title, or `null` if you can't find one.
- **company** — full company name.
- **relevance** — 1–2 short bullets on why this person/company matters to the meeting (use the
  meeting title/agenda for context). Empty array if nothing specific.
- **summary** — a 3–4 sentence professional bio: current company focus, what they do, 2–3 past
  roles, and funding stage if relevant. Do NOT include how the user knows them or any email context.

If you find nothing for a person: `title=null`, `summary="No public profile found."`, `relevance=[]`.

## Rules
- Stay factual. Use only what public sources actually say — never guess a title, employer, or
  funding stage. A thin-but-accurate entry beats a confident wrong one.
- **No unsourced numbers.** Do NOT state specific dollar figures — ARR, valuation, raise size,
  acquisition/exit price — unless they appear in the grounded result's **citations**. A named stage
  ("raised a Series A") is fine if reported; "$2.4M ARR" or "a $115M exit" is allowed ONLY if a citation
  backs it — otherwise omit the number (write "acquired by X", not "acquired by X for $Y"). These figures
  get written into the user's permanent knowledge graph, so an invented one persists — when unsure, leave
  it out. (web_research.py returns citations precisely so you can apply this rule.)
- One entry per attendee, no duplicates. Preserve the input email verbatim so the brief can match
  the research back to the right meeting.
- This is research only: do not draft messages, schedule anything, or take any action here.
- If web search isn't available, return `[]` and note that research was skipped — do not fabricate.

## Output
A JSON array, passed straight into `compose_brief` as the `attendee_research` input:

```json
[
  {
    "email": "taylor@startup.com",
    "title": "Co-founder & CEO",
    "company": "Startup Inc.",
    "relevance": ["Raising a Series A in the space you invest in"],
    "summary": "Co-founder and CEO of Startup Inc., a developer-tools company focused on CI/CD. Previously a staff engineer at BigCo and an early PM at MidCo. The company raised a seed round in 2024 and is now raising a Series A."
  }
]
```
