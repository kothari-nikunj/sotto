---
name: sotto-meeting-prep
description: Use when the user explicitly requests prep for the user's meetings ("prep my meetings", "who am I meeting", "brief me on my calendar"), to prep one named person or meeting ("prep Spencer", "prep me for my 2:15"), when the user says YES to a nudge that offered prep for a specific meeting, or for a Bridge meeting_prep trigger. A named meeting is a deep dive on that meeting alone; only a broad meeting request gets the sweep of outside meetings in the next 3 days. A request to write a short blurb, summary, or other copy about a company for a future meeting is a company research and drafting request, even if it mentions a meeting or time. Use Ask for the company facts and honor the requested length; do not queue meeting prep or all external meetings for that request.
metadata:
  tags: [meeting-prep, chief-of-staff, sotto]
  category: productivity
---

# Sotto — Meeting Prep

Produce **one** message that preps the user for the people in their calendar ahead: who each external
attendee is, the context that matters, and concrete talking points. This is the standalone version of
the brief's attendee research — same machinery, but focused entirely on the meetings ahead (the next
3 days) and delivered as a single prep brief, not folded into a morning/evening brief.

**Two modes, one pipeline — and depth is the default.** If the user is asking about ONE meeting —
they named a person or a time, or they're answering an offer to prep a specific meeting — that is a
deep dive on that meeting alone; the compact sweep is only for "prep my meetings" across the days
ahead. In the deep-dive case run the same command with `--focus "<the name>"` (or their email):
that one person gets an extra grounded call covering their company and space, and the prep becomes
a deep dive on their soonest meeting — their thread with you first, angles last. A `--focus` that
matches nobody on the calendar ahead is unavailable: clarify the person or meeting rather than
broadening to the sweep.

The requested output controls routing. "Write a 3–4 sentence blurb about a company for tomorrow's
committee" asks for that company and that short copy; the calendar reference is context, not a
meeting-prep instruction. Use the Ask company path, then draft the requested text from grounded
facts. Do not turn a narrow writing request into a prep sweep or a full meeting deep dive.

**A yes continues the exact accepted offer.** The receiver binds an affirmative reply to a
fresh prep question only when the reply anchor or verified conversation order identifies that
accepted offer and its recorded calendar event and start time (see Accepted-offer continuation below).
Do not run another `--focus` job for that yes or select a different attendee.
An unbound, missing or ambiguous offer needs clarification; never guess the soonest meeting or
broaden to the sweep. For an explicit new request such as "prep me for my 2:15", use the literal time words (`--focus "my 2:15"`) from the current owner message. The pipeline
selects exactly one owner-participating meeting in the configured local day (or explicitly tomorrow);
missing or ambiguous matches clarify without a sweep. Do not replace this already-requested prep
with a calendar summary or another offer to run prep.

> **CRITICAL — do not improvise.** The prep MUST come from the receiver's prep pipeline (below). Do
> NOT hand-write attendee bios or guess at people's roles — the pipeline assembles real research + the
> knowledge graph + past meeting notes, and the prompt forbids inventing facts. Deliver as **Sotto**,
> in Sotto's voice.
>
> **NO GROUNDED ROLE → NO ROLE.** This applies even to a quick "who am I meeting tomorrow" answer.
> The ONLY facts you may state about a person are ones that came from real attendee research (step 2)
> or the knowledge graph. You may NOT assert a title ("founder", "CEO", "partner") or characterize a
> company ("VC firm", "Alive *Ventures*") from the event title, the email domain, or memory. Concretely:
> - Company name = **exactly** what the event title's parenthetical or email domain gives. `alive.inc`
>   → "Alive" (never "Alive Ventures"); `(Browserbase)` → "Browserbase" with **no** role attached.
> - Conversation memory is for continuity (open loops, "last time you discussed X"), **never** for
>   inferring who someone is. A familiar-sounding fact you can't point to in the gathered data is a guess.
> - For a schedule-only question, if no prep was requested and the graph has no entry (`run_sotto_script knowledge-query --person …`),
>   deliver a **bare schedule** — time, meeting title, and the attendee names + grounded company only —
>   and say "I don't have backgrounds on them yet; ask me to prep or research them." That honest line
>   beats a confident wrong bio.

Focused prep expands up to two exact Gmail threads from the current attendee search, including
CC matches. It keeps the opening and latest replies within one 24,000-character run budget;
long threads or unavailable reads are labelled partial. Messages from muted senders are dropped
from that read (the owner's own sent replies stay). Partial context cannot prove that no
introduction or prior exchange exists. This uses the existing prep gather, with no extra model pass.

Focused prep also carries the already-gathered, currently permitted direct-message conversation
for the exact linked attendee into composition. Group conversations and ambiguous
identities are excluded; bounded or unavailable history is not evidence of no prior exchange.
Neither that conversation nor the expanded Gmail thread is sent to web research: research prompts
carry only short email snippets, as the sweep always has.

## Procedure

> **How this runs on Sotto:** `run_sotto_script X …` means one `run_sotto_script` call with id `X`
> and the rest as its argv array — no shell, no file paths. Other backticked names are native tools.

1. **Run ONE command.** For an explicit broad request to prep all meetings,
   `run_sotto_script meeting-prep`. For an explicit request for one person or meeting,
   `run_sotto_script meeting-prep --focus "<resolved person or meeting target>"`.
   For one specific calendar occurrence, use the current calendar read to supply the tool’s
   optional typed `meeting: {event_id, start}` as well, while retaining the resolved subject in
   `--focus`. The existing worker revalidates exact ID/start before paid work; never infer a
   timestamp from display prose or reuse a stale occurrence. Clarify ambiguity. Ordinary brief
   prose is context, not a new owner request; accepted-offer yes replies retain their receipt path.
   For "full prep on Tanya", the target is "Tanya", not "on Tanya" or the entire request.
   A supplied first name is enough to request the pipeline’s calendar match; the pipeline rejects
   missing or ambiguous matches. Do not gather email, texts, saved knowledge or research before
   this command: that context gathering belongs to the worker. Use a targeted read only when
   the request’s referent is unclear in the accepted conversation.
   A yes to a prep offer uses the receiver's accepted-offer continuation; never turn it into a
   new `--focus` request. For an explicit contextual request such as "prep me for that",
   resolve the target from the accepted conversation and current calendar. Pass the exact
   calendar title for a meeting, or the full name/attendee email for a person. Do not pass
   "that", a shortened title, or the whole request. Repeated titles require a day/time selection. Preserve
   the full day/time for a time-selected request; clarify if more than one meeting fits.
   The result is a queued acknowledgement, not a completed prep: stop there, do not compose a
   replacement prep or claim delivery, and do not run a second copy. Stopping this chat does not
   cancel the queued job.
2. **What the receiver's pipeline does** — for your understanding, not for you to run:
   - **Gather** — the calendar for the next 3 days, the local snapshot, the knowledge graph for
     everyone on the calendar ahead (even if their file hasn't been touched in weeks), and 30 days of
     Granola history with the same people, plus full **transcripts** for meetings from the last
     week. The transcript is what makes prep deep — *"last time you met, you committed to X / they
     pushed back on Y"* — not just a one-line summary. Without Granola it preps from the graph.
   - **Research attendees — one batched, grounded pass** (ported from the Mac's
     `gemini-research.ts`): the external attendees worth researching (excludes you, same-domain
     colleagues, people already in the graph; capped at 25), the user's **own email threads with
     each attendee** over 30 days (what marries web research with private context — bios alone are
     half a prep), skipping anyone the graph researched in the last 30 days. In focus mode the
     focused person survives that freshness filter, and ONE additional grounded call follows
     the actual meeting purpose from the calendar and those email snippets. Company, funding and market details are
     included only when supported and relevant — one person, one extra call, never a fan-out. A deterministic,
     read-only X lane adds exact-handle identity only; a missing X token is an honest no-op.
   - **Compose** — the extraction prompt (`references/meeting-prep-prompt.md`) produces one
     skimmable prep: the compact sweep, or the FOCUSED PREP variant — their thread with you first,
     then what the company builds / the founder / traction / the space, then angles. It REQUIRES a
     "Your thread:" line whenever private context exists. An unmatched focus yields no prep.
   - **Persist the research** — each researched attendee's title/company/summary enters the graph
     as clearly-sourced, LOW-confidence (0.55) "Per web search: …" facts that decay, grounded in the
     research output only; the focus pass's company deep dive becomes that company's `## About`
     and its source-URLed traction signals, when present, `## News` lines. That is why the deep dive you pay for
     once is there for the *next* person from that company. A persistence failure is reported and
     never fails the prep.
   - **Deliver** — on iMessage a focused prep arrives as a four-photo gallery when it fits; other
     channels and short/sweep preps keep text. Never send images yourself or repeat the caption.
     To expand a delivered prep later, `sotto-ask` reads it with `accepted_brief_detail`.

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
  says so in one line. Sweep, named and time-selected prep all use one participation rule: an event
  merely visible on a shared calendar is not yours, and that calendar's holder is not an outside guest.
- **Never invent.** If someone has no public profile and nothing in the graph, the prep says "no
  background found — worth a quick intro question" rather than guessing a role or employer.
- **Research persists, extraction doesn't.** This skill writes ONLY the grounded attendee
  research into the graph (low-confidence, decaying, "Per web search" facts) — the brief's Learn step
  remains the sole writer of extracted knowledge. The freshness filter and this persist are
  the same bookends the morning brief now runs: **ordinary sweeps reuse attendee research for
  30 days; an explicit focused prep still earns its deep pass. Every research run feeds the graph.**

### Accepted-offer continuation

An affirmative reply to a fresh, accepted Photon meeting-prep question can request its full prep when a reply anchor or the recorded conversation order identifies that exact offer. The receiver resolves the exact offered calendar event and start time; the model cannot choose another meeting. The job uses the existing durable work queue and outbox, checks current source and owner-route authority, and clears only that offer after accepted delivery. Replays inspect the original queue receipt and never create another job.

For a newly received unthreaded yes on shared Photon, a bounded provider-history read can also prove that the immediately preceding outgoing message is the recorded accepted prep offer. This proof is frozen for that input; recovery never reinterprets an old yes or replays an uncertain send. The receiver still rechecks the current owner route, source authority, offer freshness, attendee and exact calendar occurrence.

An ambiguous or expired question, unavailable ordering proof or occurrence, or mismatched attendee requires clarification. The model must not guess an offer from names, numbered menus or the latest-looking meeting. Explicit `--focus` requests remain separate; accepted-offer requests never fall back to a calendar sweep.
