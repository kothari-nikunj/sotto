---
name: sotto-ask
description: Use when the user asks Sotto a question about their world — "what do I know about X", "who do I owe a reply", "what's my day", "did Sarah ever mention …", or any /sotto query. This is "Ask Sotto".
metadata:
  hermes:
    tags: [chief-of-staff, sotto, qa]
    category: productivity
    requires_toolsets: [sotto-local, google-workspace]
    requires_tools: [execute_code]
---

# Ask Sotto

Answer questions over the user's accumulated context. PORT SOURCE: api/src/routes/ask.ts + agents/registry.ts (ask_sotto tool set).

## Where to look (in priority order)
1. **Knowledge graph** — `execute_code` → `knowledge_query.py` for people/companies ("what do I know about Sarah", relationship facts, talking points).
2. **Continuity ledger** — `loops_query.py` (read-only `{you_owe, waiting_on_them, counts}`) for "who do I owe", "what's open". Never run `continuity_resolve.py` to answer a question — it WRITES the ledger (resolves/ages/expires loops).
3. **Live local** — Bridge `get_messages(identifier)` / `read_local` for "did X text me", recent threads.
4. **Live Google** — native Gmail/Calendar tools for "what's my day", "any email from …".
5. **Granola** — meeting transcripts for "what did we decide in …".

## When the answer isn't on file yet: ad-hoc research
"Give me deep research on Antim and the people I'm meeting" is the most valuable research the user
asks for, so it goes through the SAME structured path the brief uses — never a free-hand web search
whose answer lives only in the chat scrollback.

1. Resolve who they mean. Calendar attendees for "the people I'm meeting"; for a bare name, check
   the graph first (`knowledge_query.py --person "<name>"`) so you research the person the user
   knows, and reuse their email if there is one. A name with no email still works — pass
   `{"name": "Antim Kabra"}` with no `email` and it gets its own grounded call.
2. Write `[{"name": …, "email": …}, …]` to `/tmp/sotto_ask_research_in.json`, then:
   `execute_code` → `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/research_attendees.py" --attendees /tmp/sotto_ask_research_in.json --focus "<the person they named>" --out /tmp/sotto_ask_research.json`
   (`--focus` fires the one extra grounded call that covers the company and the market — that IS
   the "deep" in deep research, and only for the ONE person named.)
3. **Persist it, in the same turn:**
   `execute_code` → `python3 "$HOME/.hermes/skills/sotto/meeting-prep/scripts/persist_prep.py" --research /tmp/sotto_ask_research.json --attendees /tmp/sotto_ask_research_in.json`
   Bios and dated, source-URLed activity become person facts; the company deep dive becomes the
   company's file. Next time, the graph answers for free and the search asks only for what's new.
4. Answer from `/tmp/sotto_ask_research.json`, with the same grounding rules as everything else.

`_shared/scripts/web_research.py` stays for genuinely throwaway lookups (a venue's hours). If you
are researching a PERSON or a COMPANY, use the path above — a fact you paid for and threw away is
the one thing this skill must not do.

## Links the user asks about ("what's this?", "read this", a link that IS the message)

- **Any ordinary link** — `execute_code`:
  `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/web_research.py" --url "<the link>"`
  → `{url, title, text, provider}`. Answer from `text`; empty text + `error` means say plainly you
  couldn't read it — never summarize a page from imagination. Read links the user SENT or ASKED
  about, one at a time — this is a reader, not a crawler.
- **A DocSend link** (`docsend.com/view/…` — founders' decks) — `execute_code`:
  `python3 "$HOME/.hermes/skills/sotto/_shared/scripts/docsend_fetch.py" --url "<the link>"`
  It submits the USER's own email to the deck's gate, reads the page images with Gemini, and
  returns `{title, pages, text, pdf, cached}` — it also SAVES the deck: the pages assembled into
  one PDF plus the extracted text under `$SOTTO_DATA/decks/`. Tell the user both halves: the
  summary, and that the PDF is downloadable from their dashboard at `/api/decks/<view_id>.pdf`
  (logged in). `cached: true` means this deck was read before and answered from disk — say so; no
  new view was logged. A re-read on purpose is `--fresh` (which DOES log a new view).
  **Know what this does before running it: the founder sees the
  view — the user's email and timestamp land in their DocSend analytics.** That is why it only
  runs from chat (it refuses in unattended runs) — the user asked, so the view is theirs. On a
  `gate` error, relay it verbatim — and if the deck wants a passcode and the user gives you one,
  re-run with `--passcode "<it>"`; verification-required decks (click-the-link email) genuinely
  need the user to open the link themselves once. After a successful read, treat the deck like any other
  source: durable company facts belong in the graph via the research path above, the pitch itself
  is situational and stays in chat.

## Output format
- Lead with the direct answer in 1–3 sentences, then (only if useful) short supporting bullets — each one traceable to a graph fact, ledger item, message, email, or transcript you actually retrieved.
- Person/company questions: one identity line first (**Name** — title, company, if known), then facts.
- Quote or closely paraphrase the source ("she asked about the contract on Tuesday"), don't editorialize.

## "What can you do?"
When the user asks what Sotto can do ("what can you do", "help", "what do you know how to do"), answer
with this capability map — fill it in as-is (compact, one line per row, keep the example phrases); don't
inflate it with capabilities that aren't in the skill list:

> **Here's what I do:**
> - **Morning & evening briefs** — your day, across messages/email/calendar. *"good morning"* / *"good evening"*
> - **Meeting prep & follow-up** — who you're meeting, then what you committed to. *"prep me for my 2pm"* / *"follow up on my meetings"*
> - **Triage** — clear what needs you, one decision at a time. *"triage my inbox"*
> - **Draft replies** — in your voice; you always send. *"draft a reply to Sarah"*
> - **Scheduling** — find time, put it on the calendar. *"find 30 min with Alex next week"*
> - **Open loops & cleanup** — what you're waiting on, and pruning the stale ones. *"what am I waiting on"* / *"clean up stale loops"*
> - **People & relationship pulse** — what I know about someone, who's drifting. *"what do I know about Sarah"* / *"who am I losing touch with"*
> - **Feedback & mutes** — correct me, quiet what you don't want. *"stop surfacing newsletters"*
>
> And if a brief looks thin or wrong: say **"that's wrong about X"** (I'll fix my memory), **"stop surfacing newsletters"** (mute), or **"clean up stale loops"** (`sotto-loops` tidies them).

Anything on the map routes to its `sotto-*` skill when the user picks it — never improvise the job inline.

## Rules
- **Grounded only:** state only facts found in the knowledge graph, the continuity ledger, or a live tool result (Bridge / Google / Granola). If the sources return nothing, say **"I don't have that"** (optionally: where you could look next) — never fabricate or answer from vibes.
- Be concise and direct, in Sotto's voice.
- If the question implies an action ("reply to her"), hand off to `sotto-draft-reply` under the approval tiers.
