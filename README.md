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

For **self-hosted** use, two principles: **Sotto drafts; you decide.** It can send an explicitly approved message when the relevant
connection and send permission are enabled. It never treats a drafted suggestion as approval. And
**self-hosted Sotto runs on YOUR infrastructure** — your Railway container, your API key, your Mac.
That path has no Sotto account or Sotto-operated runtime; its memory — the people, the open loops,
your voice — is markdown and JSON on a volume you own: readable, movable, and deletable. Sotto Cloud
is a separate, invite-only hosted service; its infrastructure and providers process data under the
boundaries described in [docs/DATA-FLOW.md](docs/DATA-FLOW.md).

**For self-hosted deployments:** your data still passes through the infrastructure and the model
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
you ⇄ iMessage (your Photon line) · Telegram (private owner bot)
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

Start with the [Bridge setup guide](ONBOARDING.md). Sotto Cloud opens directly to Google sign-in;
self-hosting is under **Other setup options**. Conversation stays in iMessage, with Telegram an
optional self-host choice.

**Sotto Cloud:** choose Continue with Google and allow Gmail or Calendar in the same authorization.
Grant Full Disk Access, return to choose Mac sources, then send the prepared hello in iMessage.
There is no separate account-only sign-up, infrastructure screen or AI key to supply. Cloud is
currently invite-only with pre-created isolated tenants and Photon lines; automatic public signup
provisioning is not yet connected. Cloud's optional Telegram connection is not yet available.

**Self-hosted Railway:** Continue with Railway → choose a workspace and create a fresh project →
enter one Google AI key → configure your Photon iMessage line (the default) or pair a dedicated
Telegram bot → review charges and create your server. Photon credentials and line setup are manual
for now. Bridge saves credentials and operation intent in Keychain and resumes the same setup.
After connecting the Mac, grant Full Disk Access **before** choosing sources, then verify your
conversation. Both hosting paths require that grant; neither offers a skip action. Selected readers
start only after source choices are saved.

New server creation requires a signed Bridge release with a qualified digest-pinned runtime image;
Railway OAuth additionally needs the registered native client configuration bundled into that
release. Source implementation is not a released installer or proof of live qualification. Without
OAuth configuration, API-token setup remains under Advanced. Existing servers can still pair through
their private setup link. Server upgrades remain separate from Bridge app updates.

Want an AI coding agent to guide setup? Follow
[`ONBOARDING.md`](ONBOARDING.md#agent-guided-setup). It keeps credentials out of chat and
separates connection checks from seeing an actual reply and brief on your device.

**What it costs to run:** your Railway hosting and model provider usage are billed separately.
Check their current plans and budgets before deploying. Self-hosting has no Sotto subscription.

Bridge is the guided onboarding surface. Once setup is complete, server-side Gmail and Calendar
reading continues while your Mac sleeps; selected Mac sources resume when it wakes.

## The dashboard

Your deploy URL is also a private web dashboard: open **`https://<your-domain>/app`** (sign in once
with your setup code) to see the machine — today at a glance, every open loop with one-tap
resolve, the archive of every delivered brief, everything Sotto knows about every person with
per-fact confidence and sourcing (correct anything that's wrong — a dashboard fix sticks exactly
like texting it), and the voice + preference rules it has learned. It also carries every lever —
snooze, mute, VIP, merge, add a loop, promote a held nudge, run a brief — without opening chat.
Text stays the primary interface; the dashboard is the window.

## The model

Gemini runs everything on a supervised install, with the one direct key from your setup config:

- **Chat** (Ask Sotto, nudge replies) uses the chat-model id in that config; the runtime accepts
  only the `google` provider.
- **Briefs, meeting prep, follow-ups and triage** use the shared provider seam. Its code can also
  compose with OpenAI or Anthropic models; see [docs/MODELS.md](docs/MODELS.md) for those limits.
  The supervised launch passes no key for another family, so it composes on Gemini.
- **Web research** uses Gemini grounding. The research code prefers Exa or Parallel when their keys
  are present, and the supervised launch does not pass them.
- **X attendee context** is read-only and optional in the code (exact handles and recent public
  Posts for upcoming attendees, plus matching bookmarks); it needs X credentials the supervised
  launch does not pass, so it reports `unconfigured`.

What is fixed and what can be overridden: [CHANNELS.md](CHANNELS.md#switching-the-model) and the
[environment reference](RAILWAY.md#environment-variables--full-reference).

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
| [Agent-guided setup](ONBOARDING.md#agent-guided-setup) | Bridge-guided Railway or invited Sotto Cloud setup |
| [docs/MAINTAINER-RAILWAY-HELPER.md](docs/MAINTAINER-RAILWAY-HELPER.md) | Maintainer-only source-build helper and recovery reference |
| [tools/RUNTIME-RELEASE.md](tools/RUNTIME-RELEASE.md) | Qualify and publish the immutable runtime artifact before signed Bridge promotion |
| [docs/BEHAVIOR-INVENTORY.json](docs/BEHAVIOR-INVENTORY.json) | Current local behavior contracts, Telegram limits, deferred work, and separate live-evidence status |
| [Source-grounded memory experiment](sotto-chief-of-staff/evals/README.md#source-grounded-memory-experiment) | Isolated Dreamer comparison, evidence-binding contracts and explicit live-model qualification |
| [adapters/sotto/VOLUME.md](adapters/sotto/VOLUME.md) | Tenant volume operations: replacement epoch, model lease continuity and scoped device access (no restore path) |
| [RAILWAY.md](RAILWAY.md) | Every setting, env var, and troubleshooting table for the cloud deploy |
| [docs/DATA-FLOW.md](docs/DATA-FLOW.md) | **Where your data goes** — every destination, every file written, how long each stays, and the gaps stated plainly. Read this before installing |
| [LICENSE](LICENSE) | Sotto source is MIT; external dependencies and model packs retain their own licenses. The Bridge binary is proprietary and explicitly out of scope |
| [docs/HOW-SOTTO-DECIDES.md](docs/HOW-SOTTO-DECIDES.md) | Why you get nudged (or don't): the triage funnel, budgets, quiet hours, and the digest — in plain rules |
| [docs/MODELS.md](docs/MODELS.md) | What changes if you don't use Gemini: every LLM call site, the measured prompt sizes, a five-model comparison (Gemini · Sonnet · GPT-5.x · Kimi · DeepSeek), and exactly what's missing for each |
| [History discovery handoff](docs/solutions/first-connect-observation-handoff.md) | Bounded current-work discovery, mute exclusions and synthetic acceptance limits |
| [Research question premises](docs/solutions/research-question-premises.md) | Preserve source limits in meeting questions and talking points |
| [docs/BOUNDED-MODEL-WORK.md](docs/BOUNDED-MODEL-WORK.md) | Direct background composition, durable attempt ownership, usage reports and deployment checks |
| [Advance meeting preparation](docs/verification/early-meeting-preparation-oct26.md) | Source-backed advance work, safe named prep offers, synthetic evidence and acceptance limits |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Includes the pure calendar/prep projection helper and accepted location receipt ownership. The runtime map: modules, daemon threads, subprocess boundaries, and shared files on the volume |
| [Shared helper ownership](docs/solutions/shared-helper-ownership.md) | Exact helper policies, canonical constants and source/image import boundaries |
| [runtime/sotto-chat/README.md](runtime/sotto-chat/README.md) | Supervised TypeScript chat runtime: local checks, explicit credentials, current boundaries and rollout gaps |
| [runtime/local-reader/README.md](runtime/local-reader/README.md) | Standalone offline HTML extraction foundation; no URL fetch or registered chat tool |
| [runtime/local-speech/README.md](runtime/local-speech/README.md) | Standalone offline Kokoro synthesis foundation; explicit model provisioning, no channel delivery |
| [runtime/hosted-speech/README.md](runtime/hosted-speech/README.md) | Gemini/OpenAI transcription and speech using an explicit existing key; awaiting runtime integration |
| [runtime/browser-harness/README.md](runtime/browser-harness/README.md) | Pinned Browser Harness driver under Mastra; fresh profiles, no cloud key, containment gate before registration |
| [docs/DOCSEND-COVERAGE.md](docs/DOCSEND-COVERAGE.md) | Page coverage, incomplete reads and legacy DocSend caches |
| [adapters/sotto/README.md](adapters/sotto/README.md) | The Sotto runtime adapter: what the receiver loads, delivery receipts, fixed managed procedures and channel agreement |
| [adapters/sotto/SELFHOST.md](adapters/sotto/SELFHOST.md) | Supervised self-host profiles (Photon Cloud, Telegram): the root-private operator file, one-shot `--prepare`, `--run`, and the Railway setup-helper mode |
| [adapters/sotto/STARTUP-FIXTURE.md](adapters/sotto/STARTUP-FIXTURE.md) | Offline startup fixture: how a private manifest renders the strict Node config and receiver projection |
| [Runtime capabilities](docs/RUNTIME-CAPABILITIES.md) | Code-checked mounted tools, standalone foundations, channel limits and evidence boundaries |
| [sotto-chief-of-staff/evals/README.md](sotto-chief-of-staff/evals/README.md) | Developer verification, including the continuous tracking probe and its current gaps |
| [docs/playground-architecture.html](docs/playground-architecture.html) | **The interactive map** — the same machine, explorable: a layered node map with saved views, a drawer per module, and every number interpolated from the drift-guarded rules island ([and the loops playground](docs/playground-feedback-loops.html)). Open the file, or visit `/static/playground-architecture.html` on your deploy |
| **[CHANNELS.md](CHANNELS.md)** | **Choosing your channel and model** — Photon iMessage (supported) and the limited Telegram profile, what each needs and how complete it is; plus what the model choice covers |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Connecting services (Granola, …): the one-click Connect tiles + the four-lane doctrine |
| [ONBOARDING.md §If something's off](ONBOARDING.md#if-somethings-off) | Troubleshooting, and removing the Mac app cleanly |

## For developers

The backend has a portable core (skills + Python pipeline over MCP and agent skills) with thin
adapters. The supervised Sotto runtime uses the TypeScript chat service, and the root Dockerfile
builds that self-host image. No other agent runtime is supported.

| Directory | What |
|---|---|
| `sotto-chief-of-staff/` | The shared skill pack + Python pipeline (extraction, knowledge, continuity, voice, relevance and continuous memory) over `$SOTTO_DATA` |
| `runtime/trigger-receiver/` | HTTP receiver: Bridge pairing, first useful look, scheduled delivery, quiet memory work + live triage; `setup_markup.py` owns pure setup markup |
| [`runtime/sotto-chat/`](runtime/sotto-chat/README.md) | Supervised TypeScript/Chat SDK runtime; the self-host helper builds it from the root Dockerfile and still needs separate live acceptance |
| [`runtime/local-reader/`](runtime/local-reader/README.md) | Independently installed HTML extraction package; not launched by the supervisor |
| [`runtime/local-speech/`](runtime/local-speech/README.md) | Independently installed offline speech worker; not launched by the supervisor |
| [`runtime/hosted-speech/`](runtime/hosted-speech/README.md) | Documentation for hosted speech adapters in sotto-chat; no extra service |
| [`runtime/browser-harness/`](runtime/browser-harness/README.md) | Independently installed browser driver; not launched by the supervisor |
| `adapters/` | Runtime wiring: the Sotto adapter, supervisor and bootstraps under `adapters/sotto/` — see [adapters/README.md](adapters/README.md) |
| `adapters/sotto/tenant_lifecycle.py` | Root supervisor's Linux lifecycle: tenant flock, empty-namespace proof, role launch/stop and replacement epoch; synthetic fixture: `adapters/sotto/test_fixtures/run-lifecycle.sh` |
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

The root Dockerfile's build also runs the packaged runtime checks: it typechecks and builds the
chat runtime, loads every packaged skill, validates an offline runtime config, and imports the
receiver with the five fixed procedures and the Sotto runner fence. These checks prove the
package, not live provider availability or device display. GitHub must separately enable required checks to prevent bypassing
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

These locks cover the supervised runtime's Python dependencies; its npm dependencies come from `runtime/sotto-chat/package-lock.json`.

### Local capability foundations

The standalone [reader](runtime/local-reader/README.md) and [speech worker](runtime/local-speech/README.md)
have separate pinned dependencies and installation instructions. They do not change the supervised
runtime's model provider, web-read routing, preferences, channel delivery, or deployment image.
The reader consumes already supplied HTML; URL fetching, Chromium, source admission and browser
egress policy remain integration work. Speech produces a private WAV artifact from an explicitly
provisioned model pack; audio preferences, source revocation and delivery receipts remain integration work.

The recommended target is Gemini with one Google model key for chat, image understanding,
transcription and optional spoken output. The offline worker is an advanced option.
[Hosted speech](runtime/hosted-speech/README.md) and the
[chat provider factory](runtime/sotto-chat/README.md#standalone-provider-adapters) are callable
adapters; protected configuration and incoming-audio/outbox integration remain gated on the
active owner-runtime migration. [Browser Harness](runtime/browser-harness/README.md) supplies
browser control under Mastra, not another agent loop. Its alpha upstream package is pinned and
fixture-tested; container/network containment and Chromium packaging remain release gates.

Run their documented contract checks separately from the shared verification command. Source CI
checks the reader on the minimum supported Node version and Node 26, and runs the speech contract
tests on Python 3.12. The public CI runs both foundation contracts too. Contract tests do not certify
native speech performance or successful audio delivery; the speech README provides an explicit real-model smoke check.

Dependency updates are reviewed changes, never runtime installs. Source Dependabot proposes npm
updates for the chat runtime and reader, grouping the model framework/adapters and the channel
adapters. Keep the reader lockfile paired with its manifest. Regenerate the speech worker's own
hashed lock using its documented command and re-run its provisioned-model smoke before changing a
native dependency or model pack. Model assets stay outside Git, and their complete checksums and
license inventory belong to the provisioned pack. Nothing in these foundations implements automatic
self-host upgrades or activates a paid service.

Release CI and the publisher also run `bash tools/verify-public-image.sh <distribution-tree>`.
This requires a running Docker daemon and the Buildx plugin. It uses BuildKit with plain progress
output and loads the verified image locally. Docker Desktop includes Buildx; with Homebrew, run
`brew install docker-buildx` and follow the plugin setup instructions from `brew info docker-buildx`.
The first build warms a new cache; subsequent builds reuse it. The npm dependency installs run in
their own build stages from the committed lock file, ahead of app code, so routine releases reuse
them.
The build checks the exact files that users deploy: it typechecks and builds the chat runtime, loads every packaged skill and imports the receiver. It does not export or publish an image or start a configured Sotto instance.

(Working in the monorepo? `docs/ADDING-A-SOURCE.md` there covers adding a new Bridge data source —
it edits Bridge source, so it deliberately doesn't ship in this repo.)

The Bridge app ships signed [on Releases](https://github.com/kothari-nikunj/sotto/releases/latest);
its macOS data readers are not part of this source tree. It updates itself: a daily check against
that same Releases page, an **"Update available"** item in its menu, and a one-click in-place install
that keeps your Full Disk Access grant.


Cloud and self-host use the same shared runtime and skill pack.

Four-photo iMessage briefs, focused meeting backgrounds, and saved text on request: [visual briefs, privacy, and testing](docs/VISUAL-BRIEFS.md).

Run `bin/check` from the repository root for the same full gate. The shared gate also checks C90 complexity at 15 and PLR rules, Vulture findings at confidence 80, and source file byte/line limits. Existing file/rule exemptions can hide new occurrences of that rule in the same file. Static size caps allow regrowth up to their recorded values after a shrink; tighten caps manually when appropriate. `bin/check --python /absolute/path/to/qualified/python` supports CI environments that installed the pinned development lock outside `.venv`.
