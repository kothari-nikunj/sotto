# Integrations — how Sotto connects to services

Sotto's promise is **your infrastructure, nobody in the loop**: your container, your keys, your
tokens, no third-party broker holding your credentials. Every integration fits one of four lanes,
in order of preference. Google is the outlier, not the template — its user-owned OAuth client
exists only because Google demands app verification for sensitive scopes; most modern services
don't have that wall.

## The four lanes

**Lane 1 — paste a key.** A `/setup` tile or env var + a deterministic gather script. Simplest,
fully deterministic, no third party. Right for services that hand out personal API tokens (Linear
PATs, many SaaS) — and the break-glass fallback for everything else.

**Lane 2 — remote MCP with OAuth 2.1 + Dynamic Client Registration** *(the durable general
answer)*. The MCP auth spec was designed for exactly the self-hosted case: nothing pre-registered,
because **the client registers itself at connect time**. "What's the OAuth client?" — *your own
container*, with DCR minting its identity on the fly and PKCE carrying the proof (no stored
secret). The one leg OAuth needs that a headless box lacks — a browser for the consent screen —
is already covered: the receiver's public `/setup` page hosts the **Connected services** tiles,
and its `/connect/oauth/callback` is the `redirect_uri`. Click **Connect**, approve in your
browser, done — the access+refresh tokens land per-service on your volume
(`$SOTTO_DATA/connectors/<service>.json`) and every later gather runs headlessly. Any
standards-compliant remote MCP (Linear, Notion, Stripe, …) becomes one click, with no vendor
relationship and no broker.

**Lane 3 — the Bridge as local credential/material collector.** The Bridge (the Mac menu-bar app)
is Sotto's first-party MCP *server* for Mac data; its integration role extends to Mac-resident
material — desktop-app login tokens, local files — each behind an explicit consent toggle. The
Bridge is deliberately NOT the MCP client to remote services: your Mac sleeps, so 24/7 token
custody belongs in the container. The browser does the consent leg wherever it is; the container
owns the tokens.

**Lane 4 — brokers (Composio, Nango, …): the escape hatch, never the default.** They solve
hostile-OAuth + no-MCP + no-key services by holding tokens in *their* cloud — which breaks the
"nobody in the loop" principle. Use only if a must-have service fits no other lane, knowingly.

## Worked example: Granola (lane 2)

1. Open your `/setup` wizard → **Connected services** → **Connect** next to *Granola (meeting
   notes)*.
2. Your browser lands on Granola's consent screen (the receiver has already discovered Granola's
   OAuth endpoints and registered itself via DCR). Approve.
3. You bounce back to the wizard's callback with a ✓ — tokens are now on your volume, and briefs,
   meeting prep, and follow-ups gather meeting notes + transcripts headlessly
   (`_shared/scripts/gather_granola.py`) from the next run.

This works on **any Granola plan** — it's your login, not a Business-only API key. If anything
fails, the error page names the exact step (discovery / registration / state / exchange) plus the
provider's error, so the first click is also the diagnosis.

**Break-glass fallback (lane 1):** Granola's official REST API at
`https://public-api.granola.ai/v1` (Bearer keys prefixed `grn_`, minted on Business/Enterprise
plans). Set `GRANOLA_API_KEY` in your deploy's variables and the gather uses REST instead of the
connector. There is also a legacy `GRANOLA_MCP_CMD` path for registering your own stdio MCP
server — see [RAILWAY.md](RAILWAY.md).

## Worked example: Exa + Parallel — web research (lane 1)

Sotto researches the people you're about to meet and the things you ask it to look up. That search
used to be Gemini Search Grounding and nothing else, which meant a deploy without a Google key had
no research at all. Two lane-1 keys close that, and beat grounding when they're set.

**The rule, in one sentence:** *for each capability Sotto uses the first provider whose key is
present — `web_search`: **Exa → Gemini grounding**; `deep_research`: **Parallel → Exa → Gemini
grounding**; `fetch_url` (read a link someone sent): **Exa → Gemini url_context → Browser Use
Cloud** (last on purpose — a real hosted browser, real credits, only for pages the crawler rungs
can't render; `BROWSER_USE_API_KEY`) — and a provider that errors falls through to the next, with
nothing left reported as "research unavailable" rather than answered from memory.*

Where it says so: the brief's **Data Source Availability** line names *Attendee Research (web
search)* as unavailable, exactly as it does a meeting-notes gather that failed. A day with no
research reads as a day with no research — never as "nobody worth researching".

- Set `EXA_API_KEY` (from exa.ai) and/or `PARALLEL_API_KEY` (from parallel.ai) in your deploy's
  variables. That is the entire setup: **selection is by key presence**, so there is no provider
  setting to pick, no per-capability override, and nothing to keep in sync with the code.
- `web_search` is a one-shot lookup — the venue address the scheduling skill needs, an ad-hoc
  question. `deep_research` is the multi-step, schema-shaped work: attendee profiles, the 90-day
  recency sweep, the focused company dive. Both live in `_shared/scripts/web_research.py`; it is the
  single place that knows the order.
- Every provider answers in the shape the capability asks for — Exa `/search` (`type:"deep"` +
  `outputSchema` for the structured one), Parallel `/v1/tasks/runs` with an `output_schema`, Gemini
  `generateContent` with the `google_search` tool — so nothing downstream knows or cares who
  answered. Parallel's run is fetched with the blocking `…/result?timeout=<seconds>` call bounded by
  the research budget that batch already had, so a slow run falls through instead of stalling a brief.
- **Visible with the others, still set as keys.** They appear on the `/setup` Connections page in
  the same tile as Granola — connected or not, what each one does, and the live ladder ("first one
  set answers: web search: Exa → Gemini grounding"). "Where do I see Exa?" now has an answer in the
  place you'd look. What that tile does *not* do is take a key: a paste-a-key field would need an
  on-volume secret store and a way to hand that secret to cron-launched subprocesses that read the
  environment — a new mechanism, and a second home for a credential, for two services. Your host's
  environment already does this and keeps the secret in its own encrypted store, so setting stays
  there and the page stays read-only. It renders *whether* a key is set, never the key.
- One registry, two kinds. `connectors.py` holds both: `SERVICES` (lane 2 — an OAuth server exists,
  so there is a Connect button) and `KEY_PROVIDERS` (lane 1 — no OAuth server exists, so there is a
  variable). The receiver image cannot import the skills tree, so its copy of the ladder is
  duplicated exactly once and guarded — `tests/test_docs_drift.py` fails the suite if the page's
  providers or their precedence ever disagree with `web_research.py`, because a page that names a
  provider the ladder wouldn't use is worse than no page.

## Under the hood (lane 2 mechanics)

- **Discovery**: RFC 9728 (`/.well-known/oauth-protected-resource` on the MCP origin →
  `authorization_servers[0]`) then RFC 8414 (`/.well-known/oauth-authorization-server`, falling
  back to `/.well-known/openid-configuration`), and finally the service's conventional
  `/oauth2/{register,authorize,token}` paths. Cached per-service on the volume.
- **DCR**: the container POSTs the registration endpoint (`client_name: "Sotto"`,
  `token_endpoint_auth_method: "none"` — public client, PKCE only) and caches the `client_id`;
  it re-registers automatically if the provider prunes the registration.
- **Flow**: PKCE S256 + a single-use `state` (10-minute TTL) stored on the volume; the code
  exchange sends the verifier, never a secret; the token request carries `resource=<mcp_url>`
  (RFC 8707) when the provider advertises it.
- **Storage**: `$SOTTO_DATA/connectors/<service>.json`, written atomically with mode 0600:
  `{service, access_token, refresh_token, expires_at, token_endpoint, client_id, resource,
  mcp_url, obtained_at}`. Refresh is handled centrally by the gather layer
  (`connector_tokens.py`); nothing else ever needs to re-run the browser flow.

## Adding a new service

One entry in the registry (`runtime/trigger-receiver/connectors.py`):

```python
SERVICES = {
    "granola": {
        "label": "Granola (meeting notes)",
        "mcp_url": "https://mcp.granola.ai/mcp",
        "auth_fallback": "https://mcp-auth.granola.ai",
    },
    # "linear": {"label": "Linear (issues)", "mcp_url": "https://mcp.linear.app/mcp",
    #            "auth_fallback": "https://mcp.linear.app"},
}
```

That's it — the `/setup` tile, the OAuth flow, token storage, and status all derive from the
entry. Then write the service's gather script (copy `gather_granola.py`) and wire it into the
skills that need it. If a provider pins redirect hosts and rejects DCR, that service falls back
to lane 1 (a key tile) — document it and move on.

## What we'd connect next, and why

Ranked by **what Sotto cannot see or do today**, not by what has a shiny MCP. Every row has to
survive one question — *what would Sotto do differently tomorrow morning because this is
connected?* If the answer is "show you more data", it is a rejection, not a roadmap item, and the
rejections are listed under the table with their reasons.

| # | Service (lane) | Capability it adds | Tomorrow morning, Sotto would… | Honest cost / blocker |
|---|---|---|---|---|
| 1 | **Slack** (2 — official remote MCP, user-token OAuth) | An entire communication channel the briefs are blind to: DMs, threads, mentions | …chase the DM you left unanswered on Tuesday, and merge a Slack thread with the same person's email into ONE item instead of two | Workspace-admin approval, and a wide read scope set (channel/group/im history + search). Slack is the loudest channel there is: it must enter through the triage funnel with conservative mute defaults or it eats the whole interrupt budget. DCR is unverified — if Slack pins redirect hosts this becomes a lane-1 user token |
| 2 | **Google Drive + Docs** (a scope on the OAuth you already granted) | The documents attached to today's meetings | …open the agenda doc on your 2pm invite and prep from what it actually says, not from the invite title | Cheapest real capability here — no new service, no new token, one more consent scope. But re-consent for everyone already connected, and a bigger blast radius on a token you already hold |
| 3 | **Affinity / Attio** (1 — Affinity workspace key · 2 — Attio's OAuth MCP) | The authoritative relationship + pipeline graph a fund already maintains | …tell you Dana is in your seed pipeline, last touched by your partner six weeks ago, instead of handing you a web bio of her | **Merge, one direction only, read-only**: CRM facts enter the knowledge graph as sourced low-confidence facts exactly like web research does, and Sotto never writes back — "log this to the CRM" is data labeling, which the outcomes doctrine forbids. An Affinity workspace key sees the whole firm's pipeline, not just yours |
| 4 | **Firecrawl** (1 — key) | Read the link someone actually sent you | …summarize the memo behind the link in Dana's email instead of reporting that Dana sent a link | Adopt only if Exa's content fetch proves insufficient — the search seam may already cover it. Must be fenced to links the user was SENT; a general crawler is a scraper, not a chief of staff. Paywalled and authed pages stay unreadable either way |
| 5 | **Telegram** (3 — the Bridge, same lane as iMessage) | The second communication channel this user's world actually runs on | …see the founder who pinged you on Telegram on Sunday and never got an answer | Another Mac-resident store behind its own consent toggle, another identity to unify onto the same person. Desktop-app storage formats change without notice; iMessage's `attributedBody` lesson applies |
| 6 | **Notion** (2 — remote MCP, OAuth) | Where commitments and docs live for many users | …put the task assigned to you in Notion into the same open-loops ledger as the promise you made over email | Notion is a swamp: unfenced, it floods the ledger. Needs a "which database" choice — a genuine user choice, so a genuine setting — plus a mapping from a Notion task to a ledger anchor |
| 7 | **Fathom / Fireflies** (1 — key) | Meeting notes without a single-vendor dependency | …say "last time you met you promised the term sheet" for people who don't run Granola | Coverage, not new capability: each is another gather script and another notes shape. Worth building when a real user's notes live there, not before. Transcript access sits behind paid tiers on both |
| 8 | **Linear** (2 — remote MCP; the registry entry is already written, commented out) | The work you owe, with due dates attached | …surface the issue you own that's due today and hasn't moved, next to the email chasing it | Nearly free — one line in `SERVICES` plus a gather. Pays off only for engineering-led users; a fund owner has no Linear, so ship it on request rather than on spec |
| 9 | **Apple Notes / Reminders** (3 — the Bridge) | Your own written commitments, in the place you wrote them | …notice that your own note says "send Dana the memo" and that you haven't | Same Bridge consent mechanics as iMessage. Low signal-to-noise: notes are half grocery lists, so it needs a real extraction gate before anything reaches the ledger |
| 10 | **Microsoft 365** (Outlook + Teams) (2/1) | Not a feature — an audience: everyone Sotto cannot serve at all today | …exist for a user whose mail and calendar aren't Google's | A second full gather path and a second OAuth verification story. Only worth it as a deliberate market decision, never as an integration |

**Rejected, and why** — each of these fails the tomorrow-morning test:

- **Calendly-likes (Cal.com, Calendly).** Sotto already proposes and books through your calendar.
  A booking page adds a link to send, not a decision to make.
- **GitHub.** "PR waiting on your review" is outcome-shaped, but it is Linear's row for a narrower
  audience — and for this product's user, zero of their day is in GitHub. Reconsider only if a
  developer-heavy user asks.
- **Stripe.** Revenue numbers in a brief are a dashboard: you cannot act on MRR at 6:30am. A chief
  of staff reports what moved and what you owe, not a metric.
- **DocSend.** *Viewer analytics* ("someone spent eight minutes on your deck") stays out: there is
  no public API to read it from, and the rest of that product is a dashboard — feasibility kills it
  before doctrine does. *Reading a deck someone sent you* is different and ships today:
  `docsend_fetch.py` opens the link through its email gate, reads the pages with Gemini vision, and
  hands you a PDF + summary — chat-only, because a fresh view is visible to the deck's sender.
- **X / LinkedIn.** What Sotto needs from them — what this person said or shipped recently —
  already arrives through the search seam above. Direct integration means paid API tiers (X) or
  scraping against terms (LinkedIn) for material we already have.
- **Brave / Tavily / Perplexity as search.** The seam is full: Exa, Parallel and Gemini grounding
  cover fast lookup and deep research. A fourth provider is a rung with no new capability.
- **Zapier / Composio-style brokers** for any of the above. Lane 4 exists for a must-have service
  that fits no other lane. Nothing on this list is one.
