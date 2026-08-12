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

## How it works

Two pieces:

```
you ⇄ WhatsApp / Telegram / iMessage
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

About **20 minutes** with the deploy button; ~35 the first time on the manual path — most of it
waiting on builds. Full walkthrough with screenshots-level detail: **[ONBOARDING.md](ONBOARDING.md)**.
The shape:

1. **Deploy the agent** — one click via the Deploy button in [ONBOARDING.md](ONBOARDING.md)
   (Railway sets up the container, storage, and secrets; you add a
   [Gemini API key](https://aistudio.google.com/apikey) — the only key Sotto needs — and your
   WhatsApp number). *WhatsApp and Gemini are the **defaults**, not requirements — Telegram and
   iMessage delivery, and other models, are a variable each:*
   **[Choosing your channel and model](CHANNELS.md)**.
2. **Link your Mac** — [download Sotto Bridge from Releases](https://github.com/kothari-nikunj/sotto/releases/latest),
   drag to /Applications, and open the **setup link** printed in your deploy logs — pairing is one
   click from the `/setup` page it opens. First run asks for an **access code** — Sotto Bridge is
   invite-only for now, the code is checked on your Mac and never sent anywhere, and if you don't
   have one you can ask in [Issues](https://github.com/kothari-nikunj/sotto/issues).
   **Already running an older Bridge?** Updating to **1.2.7 or newer** asks your existing install for
   a code once, and it stops streaming to your cloud until you enter one — nothing else is lost
   (pairing, Full Disk Access and your settings all survive), and the same
   [Issues](https://github.com/kothari-nikunj/sotto/issues) link is where you ask for a code.
   A 3-step wizard
   in the app covers disk access and privacy toggles. Connect Google + scan the WhatsApp QR on the
   same setup page.
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
  **Gemini 3.6 Flash**, with automatic fallback to `gemini-3-flash-preview` (cheaper, separate
  rate-limit bucket, same key) on quota errors. `SOTTO_GEMINI_MODEL` / `SOTTO_FALLBACK_MODEL` pick
  which Gemini model — not which vendor.
- **The chat layer** (Ask Sotto, nudge replies) — Hermes' model, so Anthropic, OpenAI, Kimi,
  DeepSeek, xAI and OpenRouter all work with that provider's key and no Sotto code change. One
  caveat on the cloud container, stated in [CHANNELS.md](CHANNELS.md#switching-the-chat-model).
- **Web research** — independent of both: set `EXA_API_KEY` and/or `PARALLEL_API_KEY` and attendee
  research stops going through Gemini at all.

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
| [RAILWAY.md](RAILWAY.md) | Every setting, env var, and troubleshooting table for the cloud deploy |
| [docs/DATA-FLOW.md](docs/DATA-FLOW.md) | **Where your data goes** — every destination, every file written, how long each stays, and the gaps stated plainly. Read this before installing |
| [LICENSE](LICENSE) | MIT, for everything in this repo. The Bridge binary is proprietary and explicitly out of scope |
| [docs/HOW-SOTTO-DECIDES.md](docs/HOW-SOTTO-DECIDES.md) | Why you get nudged (or don't): the triage funnel, budgets, quiet hours, and the digest — in plain rules |
| [docs/MODELS.md](docs/MODELS.md) | What changes if you don't use Gemini: every LLM call site, the measured prompt sizes, a five-model comparison (Gemini · Sonnet · GPT-5.x · Kimi · DeepSeek), and exactly what's missing for each |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The runtime map: the five modules, four daemon threads, five subprocess boundaries, and every shared file on the volume |
| [docs/playground-architecture.html](docs/playground-architecture.html) | **The interactive map** — the same machine, explorable: click a module, walk a real event through the funnel's gates, watch the feedback loops close ([and the loops playground](docs/playground-feedback-loops.html)). Open the file, or visit `/static/playground-architecture.html` on your deploy |
| [LOCAL-SETUP.md](LOCAL-SETUP.md) | Run everything on your Mac instead — no cloud, no hosting bill |
| **[CHANNELS.md](CHANNELS.md)** | **Choosing your channel and model** — WhatsApp (default) · Telegram · iMessage, each with what it costs to set up and how tested it is; plus which model layer needs which key |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Connecting services (Granola, …): the one-click Connect tiles + the four-lane doctrine |
| [docs/BLUEBUBBLES.md](docs/BLUEBUBBLES.md) | Optional: give Sotto its own iMessage identity |
| [docs/UNINSTALL.md](docs/UNINSTALL.md) | Removing the Mac app cleanly |

## For developers

The backend is **host-agnostic**: a portable core (skills + Python pipeline over open standards —
MCP, agentskills) with thin per-runtime adapters. It runs today on
[Hermes](https://hermes-agent.nousresearch.com/) and has an [OpenClaw adapter](adapters/openclaw/README.md).

| Directory | What |
|---|---|
| `sotto-chief-of-staff/` | The processing: 16 skills + deterministic Python (extraction, knowledge graph, continuity ledger, style, triage) over `$SOTTO_DATA` |
| `runtime/trigger-receiver/` | HTTP receiver: Bridge pairing, wake triggers, real-time event ingestion + triage funnel |
| `adapters/` | Per-host wiring (Hermes, OpenClaw) — see [adapters/README.md](adapters/README.md) |
| `contracts/` | LocalData JSON Schema + the on-disk data layout |

**Running the tests.** Python 3.11+ and three pinned dev dependencies; no services, no keys, no
network — every test is hermetic:

```bash
python3 -m pip install -r requirements-dev.txt
cd sotto-chief-of-staff && python3 -m pytest tests -q && python3 tools/validate_skills.py
cd .. && python3 -m pytest runtime/trigger-receiver -q
```

CI runs exactly these three, in this order, on every push and pull request — so green locally means
green there. A handful of tests exercise release tooling that lives outside this repo; they skip
themselves rather than fail, which is why a clean clone goes green with nothing pending.

The Bridge app ships signed [on Releases](https://github.com/kothari-nikunj/sotto/releases/latest);
its macOS data readers are not part of this source tree. It updates itself: a daily check against
that same Releases page, an **"Update available"** item in its menu, and a one-click in-place install
that keeps your Full Disk Access grant.
