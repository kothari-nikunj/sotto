# Sotto — your chief of staff, on your own infrastructure

Sotto reads everything you'd check yourself — iMessage, WhatsApp, email, calendar, missed calls,
meeting notes — and turns it into a few moments a day that actually matter:

- **A morning and evening brief** in your chat app: who's waiting on you, what's at stake, what you
  promised, what's coming up — written like a sharp chief of staff, not a notification digest.
- **Real-time nudges** when something genuinely needs you: a friend texts with a real ask, a missed
  call from someone you know — you get one short message *with a reply already drafted in your
  voice*, one tap to send. Everything less urgent waits quietly for a midday catch-up or the next
  brief.
- **A memory.** Sotto keeps a knowledge graph of your people and companies, tracks open loops
  ("you said you'd send the deck Tuesday"), notices who you're losing touch with, and learns your
  writing style from how you actually text and email.
- **Ask it anything, in chat:** *"prep me for my 2pm"* · *"what am I waiting on?"* · *"draft a reply
  to Sarah"* · *"find 30 min with Alex next week"* · *"who am I losing touch with?"*

Two principles, everywhere: **Sotto drafts, you send** — it never sends a message on its own. And
**it runs on YOUR infrastructure** — your Railway container, your API key, your Mac. There is no
Sotto server and no Sotto account: nothing phones home to us, because there is no us to phone.
And the memory it builds — the people, the open loops, your voice — is markdown and JSON on a
volume you own: readable, movable, deletable, never trapped in someone's cloud.

**What that does *not* mean.** Your data still passes through the infrastructure and the model
provider *you* configure. To write a brief, Sotto sends the gathered material — including the text
of your messages, emails, notes and calendar events — to your chosen LLM provider, and it is
processed on that provider's servers under their terms. Self-hosted means **no third party of
ours** in the path; it does not mean the data stays on your machine. If that distinction matters to
you, read [docs/DATA-FLOW.md](docs/DATA-FLOW.md) before installing — it names every place your data
goes, what is written to disk, and how long it stays.

Preferences are instructions you state in chat or Memory: mutes, VIPs, tone and cadence.
Using a draft does not grant more permission. The optional maintainer label editor is documented
in the [evaluation guide](sotto-chief-of-staff/evals/README.md); it is not part of setup.

## How it works

Two pieces:

```
you ⇄ Telegram / WhatsApp / iMessage
         │
   Sotto agent  — a container YOU deploy (Railway). Runs the brains: briefs, memory,
         │        drafts, schedules. Connects natively to Gmail + Google Calendar.
         │
   Sotto Bridge — a tiny Mac menu-bar app. READ-ONLY: it reads iMessage, WhatsApp,
                  calls, contacts, notes locally and streams them to YOUR agent —
                  with a per-source toggle for anything you'd rather not share.
```

The Bridge **dials out** to your agent (no tunnels, no open ports, nothing to keep alive) and
pushes new messages within seconds, so nudges are real-time. Close your laptop and everything
degrades gracefully: cloud-side briefs and email still work; when the Mac wakes, the Bridge
catches up quietly — old messages go to the digest, never a barrage of stale pings.

## Install

About **35 minutes** the first time — only ~15 of it active; the rest is waiting on builds. Full
walkthrough with screenshots-level detail: **[ONBOARDING.md](ONBOARDING.md)**. The shape:

1. **Deploy the agent** on Railway from your own copy of this repo — four settings and three
   variables, all listed in [ONBOARDING.md § 1](ONBOARDING.md) (a
   [Gemini API key](https://aistudio.google.com/apikey) is the only key Sotto needs, plus a
   [@BotFather](https://t.me/BotFather) bot token, a `/data` volume and a public domain). *One-click
   Deploy is in there too — same result, two prompts.* *Telegram and Gemini are the **defaults**, not
   requirements — WhatsApp and iMessage delivery, and other models, are a variable each:*
   **[Choosing your channel and model](CHANNELS.md)**.
2. **Link your Mac** — [download Sotto Bridge from Releases](https://github.com/kothari-nikunj/sotto/releases/latest),
   drag to /Applications, and open the **setup link** printed in your deploy logs — pairing is one
   click from the `/setup` page it opens. First run asks for an **access code** — the distributed Bridge binary's first-run access is invite-only for now (self-host deployment
   itself is not), the code is checked on your Mac and never sent anywhere, and if you don't
   have one you can ask in [Issues](https://github.com/kothari-nikunj/sotto/issues).
   **Already running an older Bridge?** Updating to **1.2.7 or newer** asks your existing install for
   a code once, and it stops streaming to your cloud until you enter one — nothing else is lost
   (pairing, Full Disk Access and your settings all survive), and the same
   [Issues](https://github.com/kothari-nikunj/sotto/issues) link is where you ask for a code.
   A 3-step wizard in the app then covers **disk access → connect → privacy toggles**, in that
   order, and nothing is read or sent until you press **Save & Connect** at the end. Connect Google
   on the same setup page — which also reports your channel's link (and shows the WhatsApp QR if
   that's your channel).
3. **Say "set up Sotto"** in chat. It verifies every connection honestly, seeds its memory and your
   writing voice from ~6 weeks of history, and offers your first brief on the spot.

**Staying updated:** your server checks once a day and, when a newer Sotto is published, says so
three quiet ways — a line on its `/setup` page, a line on the dashboard, and one line in your next
brief (once per version, never a separate message). Merge Railway's update PR (or hit **Sync fork**
on GitHub) and the redeploy is the update, Hermes included. Details:
**[RAILWAY.md § Staying updated](RAILWAY.md#staying-updated)**.

**What it costs to run:** Railway ~$5/mo, plus your own Gemini usage — typically **$1–1.5/day**
with the default models (briefs are the bulk; real-time triage runs on a model that costs pennies).
No subscription, no per-seat anything.

**Don't have a Mac, or don't want the Bridge?** Sotto still works — briefs from Gmail + Calendar
alone, no local messages. The Bridge is additive.

## The dashboard

Your deploy URL is also a private web dashboard: open **`https://<your-domain>/app`** (sign in once
with your setup code) to see the machine — today at a glance, every open loop with one-tap
resolve, the archive of every delivered brief, everything Sotto knows about every person with
per-fact confidence and sourcing (correct anything that's wrong — a dashboard fix sticks exactly
like texting it), and the voice + preference rules it has learned. It also carries every lever —
snooze, mute, VIP, merge, add a loop, promote a held nudge, run a brief — without opening chat.
Text stays the primary interface; the dashboard is the window.

## The model

Gemini is the **default**, and it is what the brief pipeline calls today. The honest split, by layer:

- **Briefs, meeting prep, follow-ups, triage** — a `GOOGLE_AI_API_KEY`, full stop: this half is
  deterministic Python posting to Gemini's REST API. It needs a **1M-context model**; default
  **Gemini 3.8 Flash**, with automatic fallback to `gemini-3-flash-preview` (cheaper, separate
  rate-limit bucket, same key) on quota errors. `SOTTO_GEMINI_MODEL` / `SOTTO_FALLBACK_MODEL` pick
  which Gemini model — not which vendor.
- **The chat layer** (Ask Sotto, nudge replies) — Hermes' model, so Anthropic, OpenAI, Kimi,
  DeepSeek, xAI and OpenRouter all work with that provider's key and no Sotto code change. One
  caveat on the cloud container, stated in [CHANNELS.md](CHANNELS.md#switching-the-chat-model).
- **Web research** — independent of both: set `EXA_API_KEY` and/or `PARALLEL_API_KEY` and attendee
  research stops going through Gemini at all.
- **X attendee context** — optional and read-only: the owner's `X_BEARER_TOKEN` lets meeting prep
  confirm exact handles and pull recent public Posts only for upcoming attendees; an OAuth2
  `X_USER_ACCESS_TOKEN` + `X_OWNER_USER_ID` also folds in the owner's matching bookmarks. No feed
  mirror, people-search, Chat decryption, or API sends.

Every call site, the measured prompt sizes and a five-model comparison: [docs/MODELS.md](docs/MODELS.md).

## Integrations

Beyond Google and the Bridge, services connect in **one click** from the `/setup` wizard's
**Connected services** tiles: Sotto registers *itself* with a service's remote MCP (OAuth 2.1
Dynamic Client Registration + PKCE) and keeps the tokens on your volume — no broker, no third
party in the loop. Granola meeting notes are the first tile; the full doctrine (keys, OAuth,
local material, and the escape hatch) plus how to add a service:
**[INTEGRATIONS.md](INTEGRATIONS.md)**.

## Docs

| Doc | What |
|---|---|
| **[ONBOARDING.md](ONBOARDING.md)** | The setup walkthrough (start here) |
| [cloud/accounts/README.md](cloud/accounts/README.md) | Invited Cloud accounts, browser continuation and existing-tenant adoption |
| [adapters/hermes/RECOVERY.md](adapters/hermes/RECOVERY.md) | Managed volume migration, model lease continuity, scoped device access and offline tenant restore |
| [RAILWAY.md](RAILWAY.md) | Every setting, env var, and troubleshooting table for the cloud deploy |
| [docs/DATA-FLOW.md](docs/DATA-FLOW.md) | **Where your data goes** — every destination, every file written, how long each stays, and the gaps stated plainly. Read this before installing |
| [LICENSE](LICENSE) | MIT, for everything in this repo. The Bridge binary is proprietary and explicitly out of scope |
| [docs/HOW-SOTTO-DECIDES.md](docs/HOW-SOTTO-DECIDES.md) | Why you get nudged (or don't): the triage funnel, budgets, quiet hours, and the digest — in plain rules |
| [docs/MODELS.md](docs/MODELS.md) | What changes if you don't use Gemini: every LLM call site, the measured prompt sizes, a five-model comparison (Gemini · Sonnet · GPT-5.x · Kimi · DeepSeek), and exactly what's missing for each |
| [docs/CLOUD-PILOT.md](docs/CLOUD-PILOT.md) | Managed pilot contracts, verification and remaining launch gates |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The runtime map: modules, daemon threads, subprocess boundaries, and shared files on the volume |
| [sotto-chief-of-staff/evals/README.md](sotto-chief-of-staff/evals/README.md) | Developer verification, including the continuous tracking probe and its current gaps |
| [docs/playground-architecture.html](docs/playground-architecture.html) | **The interactive map** — the same machine, explorable: a layered node map with saved views, a drawer per module, and every number interpolated from the drift-guarded rules island ([and the loops playground](docs/playground-feedback-loops.html)). Open the file, or visit `/static/playground-architecture.html` on your deploy |
| [LOCAL-SETUP.md](LOCAL-SETUP.md) | Run everything on your Mac instead — no cloud, no hosting bill |
| **[CHANNELS.md](CHANNELS.md)** | **Choosing your channel and model** — Telegram (default) · WhatsApp · iMessage, each with what it costs to set up and how tested it is; plus which model layer needs which key |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Connecting services (Granola, …): the one-click Connect tiles + the four-lane doctrine |
| [docs/BLUEBUBBLES.md](docs/BLUEBUBBLES.md) | Optional: give Sotto its own iMessage identity |
| [ONBOARDING.md §If something's off](ONBOARDING.md#if-somethings-off) | Troubleshooting, and removing the Mac app cleanly |

## For developers

The backend is **host-agnostic**: a portable core (skills + Python pipeline over open standards —
MCP, agentskills) with thin per-runtime adapters. It runs today on
[Hermes](https://hermes-agent.nousresearch.com/) and has an [OpenClaw adapter](adapters/openclaw/README.md).

| Directory | What |
|---|---|
| `sotto-chief-of-staff/` | The shared skill pack + Python pipeline (extraction, knowledge, continuity, voice, relevance and continuous memory) over `$SOTTO_DATA` |
| `runtime/trigger-receiver/` | HTTP receiver: Bridge pairing, first useful look, scheduled delivery, quiet memory work + live triage |
| `adapters/` | Per-host wiring (Hermes, OpenClaw) — see [adapters/README.md](adapters/README.md) |
| `contracts/` | LocalData JSON Schema + the on-disk data layout |

Cloud and receiver-based self-host use the same backend and skill procedures. The receiver admits
work to a small queue on the tenant volume, saves completed artifacts, and hands them to the existing
delivery outbox. Required knowledge and continuity writes precede a brief; optional learning runs
separately. Source consent applies to cached context as well as live reads, and delivery retries
recheck whether the underlying item is still current. See [the runtime map](docs/ARCHITECTURE.md)
and [storage/retention](docs/DATA-FLOW.md) for the exact boundaries and recovery limits.

**Running the shared verification.** Python 3.12 (the image's version) and the pinned development
dependencies, in a virtualenv next to this tree — Homebrew's Python refuses system-wide installs, and
`tools/ship.sh` uses `.venv` when it exists:

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python sotto-chief-of-staff/tools/verify.py
```

CI, the public distribution and the release script use that command. It runs Ruff, skill validation,
pipeline, receiver, adapter, model-proxy and accounts tests, shell parsing and available publication
guards. Tests use synthetic sources and local fixtures; none call paid models or deploy services.
Some tests bind a local loopback server. Bridge compilation and macOS integration remain a separate
platform verification step; backend test success alone does not certify the desktop app.

(Working in the monorepo? `docs/ADDING-A-SOURCE.md` there covers adding a new Bridge data source —
it edits Bridge source, so it deliberately doesn't ship in this repo.)

The Bridge app ships signed [on Releases](https://github.com/kothari-nikunj/sotto/releases/latest);
its macOS data readers are not part of this source tree. It updates itself: a daily check against
that same Releases page, an **"Update available"** item in its menu, and a one-click in-place install
that keeps your Full Disk Access grant.

Cloud account and onboarding contracts: [account service](cloud/accounts/README.md). Cloud and self-host use the same shared runtime and skill pack.
