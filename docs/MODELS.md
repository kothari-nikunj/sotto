# Models — what changes if you don't use Gemini

**Supported today: Gemini, full stop.** The brief pipeline is not model-agnostic — it POSTs directly
to Google's Generative Language API (`generativelanguage.googleapis.com/v1beta/models/<model>:generateContent?key=…`),
sends Gemini-shaped fields (`systemInstruction`, `generationConfig.responseSchema`), reads a
Gemini-shaped response. A key from any other provider does nothing for briefs.

**The one exception, and it is new: web research.** Search is no longer Gemini's alone — the seam
(`_shared/scripts/web_research.py`) resolves a provider by key presence, `web_search`: **Exa →
Gemini grounding**, `deep_research`: **Parallel → Exa → Gemini grounding**, `fetch_url` (read a
link someone sent): **Exa → Gemini url_context → Browser Use Cloud**. That is the hole §5(c)
used to describe, and it is closed; everything else below still needs the Gemini key.

**What already runs on other models today: the chat layer.** Hermes — not Sotto — owns the
conversational model, and it ships first-class providers for Anthropic, OpenAI, Kimi/Moonshot,
DeepSeek, xAI, OpenRouter and more. Point `model.provider` at one of those, give it that provider's
key, and "prep me for my 2pm" / "draft a reply to Sarah" / every nudge reply runs on it with **zero
Sotto code changes**. You still need the Gemini key for the briefs themselves — and on the cloud
container there is one boot-time pin to know about (§5a). The short version of this whole page, for
someone choosing at setup time, is in [CHANNELS.md](../CHANNELS.md).

**What's coming: one OpenAI-compatible client beside `call_gemini`.** That single change puts the
brief pipeline on GPT-5.x, Kimi, DeepSeek and anything else that speaks `/chat/completions` — the
scope is one file. The honest blocker used to be grounded search; the search seam (§5c) now stands
on its own key, so what remains is genuinely just the client.

---

## 1. Where the LLM calls actually are

| # | Call site | What it does | How often | Prompt size (measured) |
|---|---|---|---|---|
| 1 | `_shared/scripts/compose_brief.py` → `gemini.call_gemini` | **Brief extraction** — the whole day in, brief + actions + knowledge out | 2×/day (06:30, 17:30) + midday digest | **141K–306K chars** (see §2) |
| 2 | `compose_brief.run_critic` | Quality critic over the generated brief | ≤2×/day; auto-skipped on quiet days (`payload < 15,000` chars **and** ≤5 actions) | 20,199 chars |
| 3 | `compose_brief.critique_and_revise` | Revise pass applying the critic's patches | only when the critic found something | 11,548 chars |
| 4 | `_shared/scripts/research_attendees.py` | **Attendee research** — the `deep_research` capability (Parallel → Exa → Gemini grounding), 5 attendees/batch, cap 25 | ≤5 batches per brief (+ deep/focus passes) | 2,360 chars/batch |
| 5 | `_shared/scripts/web_research.py` | **THE search seam** — the resolver, plus `web_search` (Exa → Gemini grounding: ad-hoc lookups, venue checks) and `fetch_url` (Exa → Gemini url_context → Browser Use Cloud: read a link someone sent) | on demand | query/URL only (~10² chars) |
| 6 | `meeting-prep/scripts/compose_meeting_prep.py` | Meeting prep sweep / focused prep | ~1×/day | 27,838 chars (4 meetings, 8 researched attendees) |
| 7 | `followup/scripts/compose_followup.py` | Evening follow-up extraction | 1×/day (evening) | 3,707-char template + the day's ended meetings |
| 8 | `event-triage/scripts/triage_event.py` → `_gemini_once` | **Tier-1 triage** on every inbound event that survives the deterministic gates | event-driven — Gmail polled every 90 s + Bridge pushes; not capped (the caps are on *delivered* nudges) | ≤2,378 chars (`TIER1_TEXT_MAX` 1,500 + 878 scaffolding) |
| 9 | Hermes' own chat/agent model | Ask Sotto, nudge replies, tool use, `web_extract`, compression, titles | every conversational turn | Hermes-owned |
| 10 | Hermes TTS (`tts.provider`) | Voice notes | opt-in (`brief_audio`) | **not an LLM** — default `edge`, free, no key |

Sites 1–8 are deterministic Python that calls Gemini's REST endpoint directly. Site 9 is a separate,
**already-switchable** layer. Site 10 is model-independent unless you set `SOTTO_TTS_PROVIDER=gemini`.

---

## 2. The measured sizes

**Method.** These are exact character counts produced by running the real renderer — `compose_brief.build_prompt`
on the repo's own eval fixtures and on a synthetic heavy day built at the pipeline's own gather caps
(inbox 40 / bodies 12 = `gather_google.py --max/--bodies` defaults; sent 15/10 = `SENT_MAX`/`SENT_BODIES`;
attendee research cap 25 = `MAX_ATTENDEES_TO_RESEARCH`). Character counts are measurements. **Token
figures are derived at 4 chars/token** and marked `~` throughout — the real counts come back per run in
`usageMetadata` and land in the `[brief-cost]` line in `compose_brief.log`.

### The static half

| Piece | Chars |
|---|---:|
| `morning-brief/references/extraction-prompt.md`, raw | 51,083 |
| …after maintainer-comment strip (what's actually sent) | 48,316 |
| → `systemInstruction` (static policy, **byte-stable across runs**) | **41,991** |
| → user template, before any data | 6,235 |

The 41,991-char system block is byte-stable on purpose, so Gemini's *implicit* prefix caching applies.
Note this is close to worthless in practice: two brief calls a day against a ~1 h cache TTL almost
never hit. Don't count cache discounts in any model comparison for the brief path.

### Real days, real renderer

| Day | emails | events | iMsg | WA | user prompt | + system = total |
|---|---:|---:|---:|---:|---:|---:|
| `quiet_day` fixture | 3 | 1 | 0 | 0 | 6,555 | **48,546** |
| `rich_day` fixture | 22 | 6 | 4 | 1 | 14,571 | **56,562** |
| `edge_day` fixture | 0 | 0 | 8 | 3 | 7,597 | **49,588** |

### A heavy day, at the pipeline's own caps

40 inbox emails (12 with full bodies) + 15 sent (10 bodied), 8 meetings × 4 attendees, 80 iMessage +
40 WhatsApp messages, 12 researched external attendees, 40 packed knowledge-graph people. The only
free variable is average email body length, so it is shown as a curve rather than a single number:

| avg email body | user prompt | + system | ~tokens |
|---:|---:|---:|---:|
| 500 chars | 99,317 | **141,308** | ~35,300 |
| 1,000 | 110,317 | 152,308 | ~38,100 |
| 2,000 | 132,317 | **174,308** | ~43,600 |
| 3,000 | 154,317 | 196,308 | ~49,100 |
| 5,000 | 198,317 | 240,308 | ~60,100 |
| 8,000 | 264,317 | **306,308** | ~76,600 |

**Ceiling:** all 22 bodied emails at `EMAIL_BODY_MAX` (30,000 chars each) → **790,308 chars ≈ ~198K tokens.**
Pathological, but it is what the code permits, and it is the only reason a 1M-context model is the
stated requirement.

The 141K figure at short bodies lands squarely inside the "100K–140K chars" already quoted in
RAILWAY.md — that line is about a real heavy day, and it checks out.

### Where the mass is (heavy day, 2,000-char bodies)

| Block | Chars | Share |
|---|---:|---:|
| Gmail (40 inbox + 15 sent) | 58,958 | 45% |
| Attendee research (12 people) | 29,030 | 22% |
| Knowledge graph (40 packed people) | 17,898 | 14% |
| iMessage + WhatsApp threads | 15,311 | 12% |
| Calendar (8 meetings) | 5,345 | 4% |

**Marginal cost per record**, measured by differencing the real renderer: one inbox email = 173 chars
of scaffolding + its snippet + its body (bodies pass through ~1:1, capped at 30,000); one calendar
event with 4 attendees = 411 chars; one iMessage = 196 chars; one fully-populated researched attendee
≈ 2,400 chars.

### The headline

> **A realistic heavy day is ~35K–77K input tokens, not a million.** The 1M requirement is a margin
> for the long-email tail, not the working size. Every model in §3 fits a heavy day today.

---

## 3. The five-model matrix

Sizing target throughout: **~44K tokens** for a normal heavy day, **~77K** for a heavy day with long
emails, **~198K** for the pathological ceiling.

| | **Gemini 3.x Flash** *(default, baseline)* | **Claude Sonnet 4.6** | **GPT-5.2 class** | **Kimi K2.6 (Moonshot)** | **DeepSeek V3.2** |
|---|---|---|---|---|---|
| **API shape** | Native Google GenLang REST — what the code speaks today | Anthropic Messages (`anthropic_messages`) | OpenAI-compatible | OpenAI-compatible (`api.moonshot.ai/v1`) | OpenAI-compatible |
| **Max context vs our biggest call** | 1M — fits everything incl. the 198K ceiling ✅ | 1M — fits everything ✅ | 400K — fits heavy (77K) and the ceiling ✅ | 256K — fits heavy ✅, ceiling fits but tight | 163K — fits heavy (77K) ✅, **ceiling truncates** ⚠️ |
| **Search story** | **Irrelevant to the model choice now:** sites 4–5 go through the search seam, which answers on `EXA_API_KEY` / `PARALLEL_API_KEY` / `GOOGLE_AI_API_KEY`, whichever is present | No native web search in the pipeline's call shape; Anthropic's server-side web-search tool exists but is not wired here | No native grounding in our call shape | None | None |
| **Structured JSON for extraction** | `responseSchema` (OpenAPI subset) + `response_mime_type` — used today, strongest guarantee | ⚠️ Sonnet **4.6** is not on Anthropic's structured-outputs model list, and assistant prefill 400s on it — you'd fall back to strict tool use or prompt-only JSON. Sonnet 5 / Opus 4.8+ do support `output_config.format` | `response_format: json_schema` (strict) — equivalent guarantee | JSON mode (`json_object`); strict `json_schema` not guaranteed → validate client-side | JSON mode only, **no** `json_schema`; documented occasional empty-content responses → needs retry + validation |
| **~$/day at our volumes** *(see §4)* | **$0.18** primary (3.7 Flash intro rates through Dec 31, 2026; $0.36 at 2027 standard rates) · **$0.13** on the cheaper fallback | **$0.72** | **$0.55** | **$0.21** | **$0.04** |
| **TTS impact** | none | none | none | none | none |
| **THE GAP — pipeline (sites 1–3, 6–8)** | — (works) | Needs an `anthropic_messages` client beside `call_gemini`: different auth header, `system` as a top-level field, `content[].text` response shape, `usage.input_tokens`, and a schema story (see above) | Needs an OpenAI-compatible client: base URL + `Authorization: Bearer`, `messages[]`, `choices[0].message.content`, `usage.prompt_tokens` | same as GPT-5.2 | same as GPT-5.2 |
| **THE GAP — research (sites 4–5)** | — (works) | **None, as of the search seam.** Set `EXA_API_KEY` (search) and/or `PARALLEL_API_KEY` (deep research) and sites 4–5 run with no Gemini key at all; unset, they fall back to Gemini grounding as before | same | same | same |
| **THE GAP — Hermes chat (site 9)** | — | **none** — `provider: anthropic`, `ANTHROPIC_API_KEY` | **none** — `provider: openai-api`, `OPENAI_API_KEY` | **none** — `provider: kimi-coding`, `KIMI_API_KEY` | **none** — `provider: deepseek`, `DEEPSEEK_API_KEY` |

**Why DeepSeek and not Grok for the fifth slot:** DeepSeek is the informative column. It is an
order of magnitude cheaper than everything else *and* its 163K window is the only one in the set that
our measured ceiling actually breaks — it draws the line the other columns don't. Grok is worth a
footnote rather than a column: Hermes drives xAI over the `codex_responses` transport, a third API
shape on top of the two the pipeline would already need, so it is the *most* expensive of the five to
add for the least new information.

**Model-name drift:** the columns name a generation, not a pinned SKU. Vendor pricing and context
windows move; treat the numbers as of **August 2026** and re-check before quoting them. The Gemini
rates are the ones in `_shared/lib/metrics.py` `PRICE_TABLE` (which carries its own verification
dates); the others are public list prices verified against vendor and aggregator pages in August 2026.

---

## 4. How the $/day figures were derived

Measured input, stated-assumption output — the formula is shown so you can substitute your own rates.

**Daily input tokens (derived from §2 measurements, heavy day at 2,000-char bodies):**

| Call | ×/day | ~tok each | ~tok/day |
|---|---:|---:|---:|
| Brief extraction | 2 | 43,600 | 87,200 |
| Critic | 2 | 5,050 | 10,100 |
| Revise | 2 | 2,890 | 5,780 |
| Attendee research (Pass A) | 10 | 590 | 5,900 |
| Meeting prep | 1 | 7,000 | 7,000 |
| **Total input** | | | **~116,000** |

**Output is not measured here** (the brief call sets no `maxOutputTokens`; real counts land in the
`[brief-cost]` log line). The figures above assume **~25,000 output tokens/day** — two briefs, the
critic/revise pair, ten grounded research batches (`MAX_OUTPUT_TOKENS` 8,192 each) and one prep.

    $/day  ≈  0.116 × ($/M input)  +  0.025 × ($/M output)

Tier-1 triage sits outside this: on the cheap triage model at ~60 events/day it is **~$0.02/day**, and
it is the one line that scales with *your* inbox rather than with the brief. It is also the one place
prompt caching would pay off, and none of it is cached today.

This is below the "$1–1.5/day" headline in the README, which is a lived figure covering chat turns,
grounded-search result tokens and busier days — none of which this table models. Use the table for
*relative* model comparison, and the meter for absolute spend.

---

## 5. The verdict

### (a) What works today with zero code

**The Hermes chat layer, on any provider Hermes ships.** Verified against Hermes' own provider
documentation: Anthropic (`ANTHROPIC_API_KEY`, `anthropic_messages`), OpenAI (`OPENAI_API_KEY`),
Kimi/Moonshot (`KIMI_API_KEY`), DeepSeek (`DEEPSEEK_API_KEY`), xAI (`XAI_API_KEY`, `codex_responses`),
OpenRouter, Nous Portal, Bedrock and others — all `chat_completions` except where noted.

```yaml
# ~/.hermes/config.yaml
model:
  provider: "anthropic"
  default: "claude-sonnet-4-6"
```

That switches Ask Sotto, every nudge reply, and Hermes' auxiliary tasks. **It does not switch the
briefs** — those are `execute_code` Python calling Gemini's REST endpoint with `GOOGLE_AI_API_KEY`,
and they neither know nor care what the chat model is.

**One caveat, and it is a real one on the cloud container:** `adapters/hermes/start.sh` runs
`hermes config set model gemini-3.7-flash` **on every boot** (and routes every auxiliary task to
`main`), so one key covers everything out of the box — but it also means a `config.yaml` edit is
reverted by the next redeploy. On Railway, switching the chat model is a one-line edit to that file
in your fork, not a config change. On a **local or pre-existing** Hermes it genuinely is config only:
`adapters/hermes/install.sh` sets the model exclusively under `--dedicated`, and otherwise says so
("Leaving the global model untouched") and leaves your provider alone. Don't promise a knob that
isn't there.

### (b) The one cheapest code change that unlocks the most

**An OpenAI-compatible client beside `call_gemini` — one function, one file.** `_gemini_once` is ~50
lines: build a URL, POST JSON, pull text out of the response, hand `usageMetadata` to `metrics`.
An `_openai_once` alongside it (base URL + bearer, `messages[]`, `choices[0].message.content`,
`usage.prompt_tokens`/`completion_tokens`) covers **GPT-5.x, Kimi, DeepSeek, xAI-via-OpenRouter,
Grok-via-OpenRouter and every local/self-hosted server that speaks the same shape** in one go. Every
call site already funnels through `call_gemini` / `_gemini_once`, so nothing above it changes; the
schema argument maps to `response_format`, and `system` maps to a `system` role message.

**Does Anthropic's OpenAI-compatibility endpoint make Sonnet free-riders on that same client?**
Partly, and not well enough to rely on. It would get a request through, but the two things this
pipeline leans on hardest are exactly where a compatibility shim is thinnest: the structured-output
contract (Sonnet 4.6 has neither `output_config.format` nor prefill) and token accounting for
`metrics.PRICE_TABLE`. If Claude is a real target, budget a small native `anthropic_messages` client
and pick a model that supports structured outputs. **The OpenAI-compatible client is the change worth
making; the Anthropic one is a separate, later decision.**

### (c) Research when Gemini is absent — SOLVED

> **Rule: research uses the first provider whose key is present — `web_search`: Exa → Gemini
> grounding; `deep_research`: Parallel → Exa → Gemini grounding; `fetch_url`: Exa → Gemini
> url_context → Browser Use Cloud (last on purpose: a real hosted browser, real credits — only for
> pages the crawler rungs can't render) — and with none of them the brief says the research is
> unavailable rather than inventing it.**

This section used to say the opposite ("there is no second search provider in this repo"). That is
no longer true. `_shared/scripts/web_research.py` is now the seam: one resolver, three capabilities
(`web_search` · `deep_research` · `fetch_url`), four providers, selection **by key presence only**
— no `SOTTO_SEARCH_PROVIDER`, no per-capability override, nothing to keep in sync. `research_attendees.py` calls the same seam for its batched
structured research, so sites 4 AND 5 run with no Gemini key at all.

The new variables are `EXA_API_KEY`, `PARALLEL_API_KEY`, and `BROWSER_USE_API_KEY` — credentials,
which is the one kind of variable a default cannot serve. Provider
details and the lane-1 rationale: [INTEGRATIONS.md](../INTEGRATIONS.md).

So a non-Gemini deployment has two honest options, and both work today:

1. **carry a Gemini key anyway, for research only** (still the cheapest: research is ~5,900 input
   tok/day, cents), or
2. **set `EXA_API_KEY` and/or `PARALLEL_API_KEY`** — better search than grounding, and no Google
   relationship at all.

With none of the three, "runs without Gemini" still means a brief with no attendee background: the
pipeline degrades safely (an empty research result makes the brief omit rather than invent) and the
log says `[research] skipped — no search provider connected …` so the omission is narrated, not
silent.

### (d) What we should NOT support

- **Anything under ~64K context.** Not because a heavy day needs more — it doesn't, it needs ~44K —
  but because the margin between 44K and a bad day is the whole safety story. A model that only fits
  the median day fails on the day the user most needs the brief.
- **Anything that would require a chunking subsystem.** Splitting the extraction across calls means
  splitting the *judgement*: the brief's entire value is cross-source correlation (the same person on
  three channels, the escalation, the cross-source index), and a chunk boundary is exactly where that
  correlation dies. Add map-reduce, per-chunk state, a merge step and a dedupe pass, and you have a
  second pipeline to reason about — against a standing bar that every rule be explainable in one
  sentence. Not worth it for a context problem that only appears in the pathological long-email tail.
  If the ceiling ever becomes a real problem, cap `EMAIL_BODY_MAX` — one constant — before building
  anything.
- **Models with JSON mode but no schema enforcement, for the brief path, without a validator.**
  DeepSeek's price is extraordinary and its context is sufficient; its JSON story is not. If it ships,
  it ships with client-side schema validation and a retry, not with hope.

---

## 6. The recommended next change

One file, one new function, no call-site churn:

1. `_shared/lib/gemini.py` (or a sibling) gains `_openai_once(model, key, prompt, system, schema)` —
   base URL from env, `Authorization: Bearer`, `messages[]`, `response_format`, and a `usage`
   adapter into `metrics.record`.
2. `call_gemini` dispatches on the configured provider and keeps its existing retry/fallback ladder
   unchanged.
3. `metrics.PRICE_TABLE` gains rows for the models actually offered (an unpriced model already yields
   `est=n/a` rather than a guess, so this is additive).
4. Research needs nothing from this change — the search seam already stands on its own keys, per
   rule (c). That is what makes this the *only* remaining blocker to a non-Gemini deployment.

Scope: one new function plus a dispatch line, its tests, and the three docs that name the model
(RAILWAY.md's env table, `.env.template`, and this file).
