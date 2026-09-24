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

Two principles, everywhere: **Sotto drafts; you decide.** It can send an explicitly approved message when the relevant
connection and send permission are enabled. It never treats a drafted suggestion as approval. And
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
   Sotto Bridge — a tiny Mac menu-bar app. Reads your selected local sources.
                  Per-source toggles control sharing. Sending from your Mac
                  needs the separate send switch and your explicit request.
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
   requirements. WhatsApp, iMessage and other models have their own setup requirements:*
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
   A 3-step wizard in the app covers **Connect → Choose sources → Disk access**, in that
   order, and nothing is read or sent until you press **Save & Connect** at the end. Connect Google
   on the same setup page — which also reports your channel's link (and shows the WhatsApp QR if
   that's your channel).
3. **Say "set up Sotto"** in chat. It verifies connections and reports the first useful look from
   recent context. Progressive history learning is separate: receiver-based self-hosts need a
   budgeted model proxy, or an explicit `SOTTO_BACKGROUND_UNMETERED=true` opt-in to direct-key
   background spending. Without either, chat and briefs work but history learning stays held.
   Check `knowledge/history-state.json` for actual coverage. See [RAILWAY.md](RAILWAY.md).

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

Gemini is the default. The model settings are separate for each layer:

- **Briefs, meeting prep, follow-ups and triage** use the shared provider seam. Gemini uses
  `GOOGLE_AI_API_KEY`; `SOTTO_BRIEF_MODEL=openai/<model>` or `anthropic/<model>` selects another
  family with its credentials. `SOTTO_TRIAGE_MODEL` configures triage separately. Brief models
  must meet the context requirement; see [docs/MODELS.md](docs/MODELS.md) for limits and
  capabilities that still require Gemini. Changing the chat model alone does not change these calls.
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
| [adapters/hermes/RECOVERY.md](adapters/hermes/RECOVERY.md) | Managed volume migration, model lease continuity, scoped device access and offline tenant restore |
| [RAILWAY.md](RAILWAY.md) | Every setting, env var, and troubleshooting table for the cloud deploy |
| [docs/DATA-FLOW.md](docs/DATA-FLOW.md) | **Where your data goes** — every destination, every file written, how long each stays, and the gaps stated plainly. Read this before installing |
| [LICENSE](LICENSE) | MIT, for everything in this repo. The Bridge binary is proprietary and explicitly out of scope |
| [docs/HOW-SOTTO-DECIDES.md](docs/HOW-SOTTO-DECIDES.md) | Why you get nudged (or don't): the triage funnel, budgets, quiet hours, and the digest — in plain rules |
| [docs/MODELS.md](docs/MODELS.md) | What changes if you don't use Gemini: every LLM call site, the measured prompt sizes, a five-model comparison (Gemini · Sonnet · GPT-5.x · Kimi · DeepSeek), and exactly what's missing for each |
| [docs/BOUNDED-MODEL-WORK.md](docs/BOUNDED-MODEL-WORK.md) | Direct background composition, durable attempt ownership, usage reports and deployment checks |
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
python3.12 -m venv .venv && .venv/bin/pip install --require-hashes -r requirements-dev.txt
.venv/bin/python sotto-chief-of-staff/tools/verify.py
```

CI, the public distribution and the release script use that command. It runs Ruff, skill validation,
pipeline, receiver, adapter, model-proxy tests, shell parsing and available publication
guards. Tests use synthetic sources and local fixtures; none call paid models or deploy services.
Some tests bind a local loopback server. Bridge compilation and macOS integration remain a separate
platform verification step; backend test success alone does not certify the desktop app.

The Docker build also runs [`check_runtime.py`](runtime/trigger-receiver/check_runtime.py) as
the managed user in the installed filesystem layout. The same contract runs in both deployment
modes in the receiver suite: a nonempty scheduled scan must produce a nudge receipt, and morning
and evening briefs must deliver four photos through the real outbox and adapters. It checks
missing provider IDs, retry recovery, duplicate suppression, completed effects and disabled nudges.
Synthetic CLI and loopback sidecar fixtures replace only the external transport; no real messages
or model calls occur. Regression tests deliberately restore the missing-import and rejected-nudge
bugs and require the gate to fail. These checks prove our delivery contract, not live provider
availability or device display. GitHub must separately enable required checks to prevent bypassing
the gate at merge time.

The `.in` dependency manifests hold reviewed direct pins. The generated `.txt` files include
all transitive pins and package hashes; development uses the same runtime versions as the image.
Edit the manifests, then run `bash tools/lock-requirements.sh` with `uv` installed and review the
lock changes. Reproducibility comes from the *existing* `.txt` files, not from the manifests alone
— `uv pip compile` prefers whatever version they already pin, so running the script with them in
place regenerates them byte-for-byte; deleting one first (or otherwise running with no lock to
prefer) lets every unpinned transitive drift to whatever is newest today. The script refuses to run
against a missing lock unless you pass `--upgrade`, which is also how to deliberately let unpinned
transitives move to their newest compatible version — a `.in` edit alone always keeps everything
else exactly where it was.

`verify.py`'s `verify_locks` always rejects a stale direct pin, a missing hash, and runtime/test
version drift between the lock files, file-based and offline. When its resolver is available, it
also closes each lock against its manifest; its output states whether that exact check ran:
- **`uv` available and its local cache can resolve everything offline:** regenerates each lock into
  a scratch copy (the same command `lock-requirements.sh` runs, plus `--offline`) and requires the
  result to match the committed file byte-for-byte. It catches an orphaned
  transitive left behind by a removed direct dependency, an unrequested package hand-added to the
  lock, an incompatible transitive pin, and a new direct dependency added without resolving its
  own transitives. Compatible existing pins remain preferred; this is not an upgrade check.
- **Otherwise** (`uv` missing, or its offline cache can't resolve something): keeps the deterministic
  file checks above and explicitly reports that the full closure check was skipped. It does not try
  to reconstruct the graph from installed package metadata: these are universal locks, so dependencies
  selected only on another platform or by an extra may correctly be absent from this interpreter.
  Run the gate where `uv` has a warm cache, or regenerate and review the locks, for the exact closure
  guarantee. Resolver conflicts and malformed inputs fail verification; only an explicit missing-cache
  diagnostic permits the offline check to be skipped.

These locks cover Sotto's Python dependencies; Hermes manages its own runtime dependencies.

Release CI and the publisher also run `bash tools/verify-public-image.sh <distribution-tree>`.
This requires a running Docker daemon and the Buildx plugin. It uses BuildKit with plain progress
output and loads the verified image locally. Docker Desktop includes Buildx; with Homebrew, run
`brew install docker-buildx` and follow the plugin setup instructions from `brew info docker-buildx`.
The first build warms a new cache; subsequent builds reuse it. The image keeps the Node/Photon
installation and dependency permission changes ahead of app code so routine releases do not
reinstall dependencies or copy their entire tree into another permission-only layer.
The build checks the exact files that users deploy, including Hermes compatibility and message delivery. It does not publish an image or start a configured Sotto instance.

(Working in the monorepo? `docs/ADDING-A-SOURCE.md` there covers adding a new Bridge data source —
it edits Bridge source, so it deliberately doesn't ship in this repo.)

The Bridge app ships signed [on Releases](https://github.com/kothari-nikunj/sotto/releases/latest);
its macOS data readers are not part of this source tree. It updates itself: a daily check against
that same Releases page, an **"Update available"** item in its menu, and a one-click in-place install
that keeps your Full Disk Access grant.


Cloud and self-host use the same shared runtime and skill pack.

Four-photo iMessage briefs, focused meeting backgrounds, and saved text on request: [visual briefs, privacy, and testing](docs/VISUAL-BRIEFS.md).
