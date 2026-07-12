# Deploying Sotto to Railway — click-by-click

This is the exact Railway setup for the cloud Sotto host (Hermes + skills + trigger receiver). Two ways
in: the **one-click Deploy button** ([jump to it](#one-click-deploy-railway-template)) sets up build +
`/data` volume + `BRIDGE_TOKEN` and just prompts for two values; or the **manual GitHub deploy** below —
the fallback, or for a repo without a published template. The Mac side (Bridge) is tunnel-free —
[download the signed app from GitHub Releases](https://github.com/kothari-nikunj/sotto/releases/latest),
then see §8.

> **New to this? Start with [ONBOARDING.md](ONBOARDING.md)** — the friendly fresh-cloud walkthrough.
> This page is the click-by-click reference behind it. Honest budget for the manual deploy: **~15
> minutes** — four Railway settings, four variables, then the one-page `/setup` wizard (paste your
> Google client JSON + auth code, scan one WhatsApp QR). Call it a dozen clicks and copy-pastes end
> to end for the manual path; the **one-click template** cuts that to ~4 (two prompts + the wizard).

## 0. Before you start (prerequisites)

- **A Railway account on a paid (or verified) plan.** Volumes and always-on services are not
  available on the free/trial tier, and Sotto needs both — the `/data` volume keeps your WhatsApp
  session + memory, and the 6:30/17:30 cron briefs need the container running around the clock.
- **A Gemini API key** ([aistudio.google.com](https://aistudio.google.com) → Get API key).
- **This repo on your GitHub** (fork or push it) so Railway can deploy from it.

## Manual-deploy checklist — 4 REQUIRED settings, in order

The one-click template automates exactly these; on a manual deploy **you** do them, and each one
fails *quietly* if skipped:

1. **Root Directory: leave blank** (the Dockerfile is at the repo root) — Settings ▸
   Build ▸ Builder. Details: [step 2](#2-builder--build-context-critical).
2. **Variables** — Settings ▸ Variables ([step 5](#5-variables)):
   - `GOOGLE_AI_API_KEY` = your Gemini key
   - `WHATSAPP_ALLOWED_USERS` = your number, country code, no `+` (e.g. `15551234567`)
   - `WHATSAPP_HOME_CHANNEL` = same number
   - `BRIDGE_TOKEN` = a long random secret — generate one: `openssl rand -hex 24`. **Not optional.**
     Without it the Mac pairing link on `/setup` carries an **empty token** — the app looks paired
     but pairing **silently fails**. Set it *before* you first open the setup link.
3. **Volume mounted at `/data`** — [step 3](#3-add-a-volume-persistent-storage--required). Without
   it every redeploy wipes the WhatsApp login, Google token, and all of Sotto's memory.
4. **Generate the public domain BEFORE opening the setup link** —
   [step 4](#4-generate-a-public-domain-so-the-mac-can-reach-it). Without a domain, the setup link
   printed in the deploy logs **falls back to `http://localhost:…`** (useless), and the Mac pairing
   link has no reachable host. Generate the domain, redeploy, then open the freshly logged link.

With those four in place, steps 1–8 below are the full click-by-click.

## 1. Create the service
Railway dashboard ▸ **New Project** ▸ **Deploy from GitHub repo** ▸ pick your repo.

## 2. Builder + build context (CRITICAL)
The `Dockerfile` sits at the **root of the Sotto folder**, and its `COPY` lines are relative to that
folder — in this standalone repo that folder *is* the repo root:

- **Settings ▸ Build ▸ Builder** → **Dockerfile**
- Leave **Root Directory** and **Dockerfile Path** blank. Railway auto-detects `./Dockerfile`; context = repo root. Done.


> Why blank: Railway's "Dockerfile Path" is an **absolute path from the repo root** and does *not* follow
> the Root Directory — a value there is the #1 source of confusion. With the `Dockerfile` at the (Root)
> directory it's auto-detected and the context is that folder, so `COPY sotto-chief-of-staff/ …` resolves.

## 3. Add a Volume (persistent storage — required)
The knowledge graph, continuity ledger, style profile, briefs, **and the WhatsApp login session** live
here. Without it, every restart wipes them.

- In the service, press **⌘K / Ctrl+K** (or right-click the service card) ▸ **Add Volume**.
- **Mount path:** `/data`
- Save → it prompts a redeploy.

> Railway only persists **runtime** writes to a volume (build-time writes don't stick). Sotto writes at
> runtime and `$SOTTO_DATA=/data` is already set in the image, so this just works. `start.sh` also routes
> Hermes' own state (session/config/SOUL) onto `/data` so redeploys don't force a WhatsApp re-scan.

## 4. Generate a public domain (so the Mac can reach it)
The Bridge on your Mac pushes "I'm awake" events to the cloud, so the container needs a public URL.

- **Settings ▸ Networking ▸ Generate Domain** → gives `https://<app>.up.railway.app`.
- Your trigger endpoint is that URL + `/sotto/trigger`. Use it as `--trigger-url` on the Mac.
- **Do this before opening the setup link** (step 6). The `[sotto] Setup link` line in the deploy
  logs is built from the public domain — with no domain it falls back to `http://localhost:…` and
  nothing on it (wizard, pairing link, QR page) is reachable. Generate the domain, redeploy, use the
  new logged link.

> Don't set `PORT` yourself — Railway injects it and the trigger receiver binds `0.0.0.0:$PORT`. The
> generated domain routes to that receiver.

## 5. Variables
**Settings ▸ Variables:**
- `GOOGLE_AI_API_KEY` = your Gemini key (any 1M-context model).
- `WHATSAPP_ALLOWED_USERS` = your number with country code, no `+` (e.g. `15551234567`). Hermes
  **denies all users until this is set** — without it the brief can't reach you.
- `WHATSAPP_HOME_CHANNEL` = same number — where the brief is delivered proactively.
- `BRIDGE_TOKEN` = the Mac↔cloud shared bearer — **required on a manual deploy**; generate it with
  `openssl rand -hex 24` (a future template deploy auto-generates it). Plainly: without it the
  pairing link the `/setup` wizard renders carries an **empty token**, so Mac pairing **silently
  fails** — set it before you open the setup link. No `BRIDGE_URL` exists anymore — the Mac dials
  *out* to this host's relay.
  *(The wake-push authenticates with `BRIDGE_TOKEN` too — `SOTTO_TRIGGER_TOKEN` exists only if you want a separate bearer for it.)*

> `start.sh` writes these (plus `WHATSAPP_ENABLED=true`) into `~/.hermes/.env` on boot — Hermes reads
> messaging-platform settings from `.env`, not `config.yaml`. For quick testing you can instead set
> `GATEWAY_ALLOW_ALL_USERS=true` (open access — anyone who messages the linked WhatsApp can use it).

## 6. Deploy + pair WhatsApp
- **Deploy.** Watch the **Deploy logs** — Hermes installs here (a failure is loud by design). Grab the
  **setup link** they print (the line starting **`[sotto] Setup link`**): the setup pages (`/setup`,
  `/whatsapp/qr`, `/google/*`, `/debug/google`) are gated behind that link's access code. Open it once
  and a cookie covers the rest; lost it? it reprints on every boot (or read `/data/setup_code`).
  `SOTTO_SETUP_CODE` optionally pins the code. Old bare bookmarks (no `?code=`) now return 403.
- On first boot `start.sh` runs `hermes whatsapp` (the pairing step — the gateway itself won't pair) and
  prints a QR. **Scan it from the clean web page, not the deploy logs** (Railway's log viewer distorts the
  terminal QR):
  - open **`/whatsapp/qr`** via the logged setup link (or from the `/setup` wizard — the code rides along),
  - WhatsApp ▸ **Linked Devices** ▸ Link a Device ▸ scan.
  `creds.json` persists on `/data`, so later boots skip pairing and go straight to the gateway.

## 6b. Connect Google (Gmail + Calendar) — deterministic, headless
Do **not** do this through the chat — the agent regenerates the auth URL and you get "Invalid code verifier."
Use the built-in flow instead:
1. **Create a Desktop OAuth client** (one-time): [console.cloud.google.com](https://console.cloud.google.com) →
   new project → enable **Gmail API** + **Google Calendar API** → OAuth consent screen (External) →
   **publish it to "In production"** → Credentials → **OAuth client ID → Desktop app** → **Download JSON**.
   > ⚠️ **The day-8 trap:** a consent screen left in **Testing** issues refresh tokens that **expire
   > after ~7 days** — Google silently disconnects and every brief loses Gmail/Calendar on day 8.
   > Set the app to **In production** (Google does *not* require verification for you using your own
   > data — ignore the scary "needs verification" banner).

   *(Workspace accounts: if your org blocks unverified apps, allowlist the client in Admin console, or use a personal account.)*
2. **Paste that JSON into the `/setup` wizard's Google box → Save client** (open the wizard via the
   setup link from the deploy logs — step 6). No Railway variable, no redeploy. *(Legacy fallback: set
   `GOOGLE_OAUTH_CLIENT_JSON` in Railway Variables + redeploy.)*
3. In the wizard, click **Authorize** (or open **`/google/auth`** — same setup code) → "unverified app"
   → Advanced → Continue → Allow.
4. You land on a `localhost:1/?code=…` page that won't load. Copy the **`code`** value.
5. **Paste the code into the wizard → Connect.** It exchanges live and shows "✅ Google connected" —
   **no redeploy**. The token persists on `/data` and auto-refreshes.
   *(Fallback if the live exchange errors: set `GOOGLE_AUTH_CODE` in Railway → Variables → redeploy →
   clear it, as before.)*
6. **Verify any time:** open **`/debug/google`** (setup code/cookie required) — it returns `{"google_connected": true}` when the
   token works. Google is **server-side and Bridge-independent**: if this says connected, every cron
   brief gets fresh Gmail + Calendar even with the Mac asleep. If a brief says "Google isn't connected"
   but this says `true`, the agent skipped the gather — not an auth problem. (Local data is the only
   thing that's cached as an offline backup; Google is always fetched live.)

## 6c. Connect Granola (optional, fiddly)
**Honest caveat:** Granola has **no official public API**. The official Granola MCP uses a browser OAuth
flow (no good in a headless container), the most popular community one (`granola-mcp`) reads the **local
Granola app's cache** (which doesn't exist in the cloud), and the remote/token MCPs are community-built,
varied, and ride Granola's private backend — so this is the least reliable integration. `start.sh`
registers whatever stdio MCP you point it at:
1. **Railway → Variables:** `GRANOLA_API_TOKEN` = your Granola token, and `GRANOLA_MCP_CMD` = the command
   to run a **remote-capable** Granola MCP server (e.g. `uvx <some-remote-granola-mcp>` or `acai serve`).
2. Redeploy. `start.sh` writes a `mcp_servers.granola` entry (passing your token under the common env-var
   names + `GRANOLA_DOCUMENT_SOURCE=remote`) → log shows `Granola MCP registered`.

If you only set `GRANOLA_API_TOKEN` without `GRANOLA_MCP_CMD`, nothing is registered (the log tells you).
Granola is the least-critical source — fine to skip and ship Gmail + Calendar + the Bridge first.

## 7. Talk to Sotto
You chose **personal number / self-chat** (`SOTTO_WHATSAPP_MODE=2`), so *your own WhatsApp is the bot* —
you talk to Sotto by **messaging yourself**:
- WhatsApp ▸ new chat ▸ **"Message Yourself"** (your name with "(You)"), or search your own number.
- Send **"hi"** → the agent replies, prefixed ***Sotto*** (so you can tell its messages from yours —
  `start.sh` sets `whatsapp.reply_prefix`; set `SOTTO_HIDE_AGENT_NAME=1` for no prefix at all).
- A reply confirms the full round-trip (WhatsApp → Hermes → Gemini). A real brief also needs Google
  connected (step 6) and the Mac **Bridge** ([ONBOARDING.md](ONBOARDING.md) §5) for local data.

*(Prefer a dedicated bot number instead of self-chat? Set `SOTTO_WHATSAPP_MODE=1` and pair a second
WhatsApp number — then people message that number directly.)*

## 8. Connect the Mac Bridge (local iMessage/SMS/calls)
Gmail + Calendar come from Google; your **local** signals come from the Sotto Bridge on your Mac. It
connects **tunnel-free**: the Mac dials *out* to this Railway host, so there's nothing to expose — no
Cloudflare, no domain, no inbound port.

1. **Railway → Variables:** make sure `BRIDGE_TOKEN` is set — the shared bearer. Template deploys
   generate it automatically (you never see or type it); on a manual deploy, set a long random secret
   you pick (`openssl rand -hex 24`) and redeploy. `start.sh` registers `sotto-local` at the host's own
   always-up relay (`/mcp`), so Hermes never 530s even when your Mac is asleep. Either way you won't
   type the token into the Mac app — the pairing link below carries it. *(The wake-push uses
   `BRIDGE_TOKEN` too; `SOTTO_TRIGGER_TOKEN` only exists to give it a separate bearer.)*
2. **On your Mac:** install the signed **Sotto Bridge** menu bar app — [download it from GitHub
   Releases](https://github.com/kothari-nikunj/sotto/releases/latest) — then **pair it in one click**:
   - Open the **setup link from the deploy logs** (step 6 — `/setup` needs its access code) on that Mac
     → click **“Open in Sotto Bridge”**. It fills the host + token (no typing, and the host always
     carries `https://`).
   - *Fallback:* copy the **pairing code** in the wizard and paste it into the app's **“Paste pairing
     link”** field. Or enter **Host URL** + **Bridge token** (`BRIDGE_TOKEN`) manually.
   - Grant **Full Disk Access** when prompted; flip **Start at login** on.
   The app supervises `sotto-bridged --connect …` (dials out) — the menu bar dot turns solid when
   connected. Then message **"Sotto, set up"** (expect `fda: ok`).
   *(Prefer no GUI? Run it directly: `sotto-bridged --connect https://your-app.up.railway.app --token <BRIDGE_TOKEN>`.)*
   - **Diagnose a source before connecting:** run `sotto-bridged --doctor` — a read-only, per-source
     readout (each local source prints `ok` / `needs Full Disk Access` / `unavailable`, plus the exact
     "grant FDA to the *right* app" fix). It writes nothing and touches no network; exit `0` means every
     enabled source reads. See the troubleshooting table below.

> The auto wake-push (brief fires the instant your Mac wakes) is optional — the **6:30/17:30 cron**
> fires the brief regardless, and you can ask for one anytime. If the Bridge is offline at brief time,
> the brief degrades to the last cached snapshot. And if a triggered brief dies mid-run, its claim goes
> stale after **30 minutes** and the next trigger retries it — no silently lost briefs.

## One-click deploy (Railway template)

The button below collapses the whole manual checklist above — build, `/data` volume, and
`BRIDGE_TOKEN` — into one click, leaving just two prompts (your Gemini key + WhatsApp number). It's the
recommended path; the manual checklist above is the fallback.

[![Deploy on Railway](https://railway.com/button.svg)](REPLACE_WITH_TEMPLATE_URL)

**(Repo owner only)** To (re)publish the template: Railway dashboard ▸ **Settings → Templates → New
Template** ▸ add this repo, then in the template's **Variables** pre-declare these so the friend types
as little as possible:

| Variable | Template setting |
|---|---|
| `BRIDGE_TOKEN` | **default = generated secret**, e.g. `${{ secret(48) }}` — so it's auto-created, never typed |
| `GOOGLE_AI_API_KEY` | prompt (their Gemini key) |
| `WHATSAPP_ALLOWED_USERS` | prompt (their number) |
| `WHATSAPP_HOME_CHANNEL` | prompt (their number) |
| `GOOGLE_OAUTH_CLIENT_JSON` | **no longer needed** — paste the client JSON in the `/setup` wizard instead (no var, no redeploy) |

The template also bundles the **Dockerfile build** + the **`/data` volume** (mount `/data`) so those
aren't manual steps. After deploy, the friend generates a domain, then opens the **setup link from
the deploy logs** (the line starting `[sotto] Setup link` — it's the `/setup` wizard plus its access
code): one page that links the Mac, connects Google (paste client → authorize → paste code, all
live), shows the WhatsApp QR, and auto-detects the timezone.

## Environment variables — full reference
| Variable | Purpose | When |
|---|---|---|
| `GOOGLE_AI_API_KEY` | LLM key (Gemini, 1M ctx). `start.sh` maps it to `GEMINI_API_KEY`/`GOOGLE_API_KEY` for Hermes' provider too. | **required** (step 5) |
| `WHATSAPP_ALLOWED_USERS` | who may use the bot — your number, country code, no `+` (e.g. `15551234567`). Deny-all until set. | **required** |
| `WHATSAPP_HOME_CHANNEL` | where the brief is delivered proactively — your number. **Required for scheduled/proactive delivery:** the 6:30/17:30 crons, proactive nudges, and follow-ups deliver to this channel — unset, they have nowhere to land (interactive chat still replies). | **required** (delivery) |
| `SOTTO_TIMEZONE` | IANA zone (e.g. `America/Los_Angeles`) for the **6:30 morning / 17:30 evening** cron briefs + time injection. **Now optional** — the `/setup` wizard auto-detects your zone from the browser and persists it to `/data/config/settings.json` (the cron hour self-heals on the next boot). Set this only to override the auto-detected zone. | optional |
| `SOTTO_CRON_DELIVER` | where cron briefs are delivered — defaults to `whatsapp` (the WhatsApp home channel). Set to `local`, `telegram`, etc. to override. | optional |
| `SOTTO_GEMINI_MODEL` | override the brief's Gemini model (default `gemini-3-flash-preview`). Must be 1M-context. | optional |
| `SOTTO_CRITIC` | the brief's second-pass Gemini **critic + revise** quality gate (`auto` \| `always` \| `off`, default `auto`). `auto` skips the two extra Gemini calls on a **small/low-risk** brief (rendered source payload `<15000` chars AND `≤5` actions) and runs them otherwise; `always` = every brief; `off` = never. | optional (quality) |
| `SOTTO_FALLBACK_MODEL` | optional backup **1M-context** Gemini model id (e.g. `gemini-2.5-pro`) the brief falls back to on a 429/5xx/timeout. The brief prompt is 100K–140K chars, so the backup MUST be 1M-context. | optional (resilience) |
| `SOTTO_FALLBACK_API_KEY` | optional second Gemini key (different project) used for the fallback — dodges per-project quota (the 429 storm). Can be set alone (same model, backup key) or with `SOTTO_FALLBACK_MODEL`. | optional (resilience) |
| `SOTTO_ALLOW_SELF_IMPROVE` | `1` to allow Hermes' skill self-writes + Curator on this instance. Default (unset) **protects** Sotto's skills: gates `skills.write_approval`, disables curator pruning. Set `1` only on a shared general-purpose Hermes. | optional |
| `SOTTO_REFRESH_HERMES` | `1` for **one boot** adopts the image's Hermes runtime onto the `/data` volume (see *Keeping Sotto up to date*). A denylist protects WhatsApp login, sessions, config, SOUL, and the knowledge graph. Unset after the version line confirms the upgrade. | optional (upgrade) |
| `SOTTO_RESEARCH_CONCURRENCY` | parallel attendee-research sub-agents (`delegation.max_concurrent_children`). Default `5`. | optional |
| `SOTTO_PROACTIVE` | `1` (default) runs the mostly-silent proactive nudge cron (meeting-about-to-start / due commitment / birthday, draft-ready, never auto-send). `0` disables it. | optional |
| `SOTTO_PROACTIVE_CRON` | proactive watcher interval (default `*/15 * * * *`). | optional |
| `SOTTO_QUIET_START` / `SOTTO_QUIET_END` | proactive quiet-hours window (defaults `21` / `7` — no nudges 9pm–7am). | optional |
| `SOTTO_PROACTIVE_LEAD_MIN` | how many minutes before a meeting to nudge (default `45`). | optional |
| `SOTTO_FOLLOWUP` | `1` (default) runs the light evening follow-up cron (16:45 local — drafts post-meeting follow-ups from meetings ended since its last run, silent when nothing's actionable, never auto-sends). `0` disables it. | optional |
| `SOTTO_FOLLOWUP_CRON` | follow-up cron schedule (default `45 16 * * *` — 16:45 local). | optional |
| `SOTTO_FOLLOWUP_DEFAULT_HOURS` / `SOTTO_FOLLOWUP_MIN_HOURS` / `SOTTO_FOLLOWUP_MAX_HOURS` | follow-up look-back windowing knobs: first-run bootstrap window and the clamp on the since-last-run gap (defaults `36` / `1` / `72`). | optional |
| `SOTTO_WAKE_PUSH` | **BRIDGE-side** (set where the Bridge runs, not Railway) — fires the brief/nudge the moment your Mac wakes. Default **on**; `=0` (or `false`) disables (the 6:30/17:30 cron still fires). The cron↔wake-push deliver-once gate makes double-delivery impossible. | optional (Bridge) |
| `SOTTO_WAKE_MORNING_MIN` / `SOTTO_WAKE_MORNING_CUTOFF` | **Bridge-side** morning wake-push window in minutes-past-midnight — wake past this and before the cutoff triggers the morning brief (defaults `420` / `1080` = **7:00 – 18:00**). | optional (Bridge) |
| `SOTTO_WAKE_EVENING_MIN` / `SOTTO_WAKE_EVENING_CUTOFF` | **Bridge-side** evening wake-push window (defaults `1050` / `1380` = **17:30 – 23:00**). | optional (Bridge) |
| `SOTTO_TTS` / `SOTTO_TTS_PROVIDER` / `SOTTO_TTS_VOICE` | voice (read + listen). `SOTTO_TTS=1` (default) enables Hermes TTS; provider `edge` (default, free, no key) or `gemini` (uses your Google key); voice id override. `SOTTO_TTS=0` for text-only. | optional |
| `SOTTO_WHATSAPP_MODE` | `2` self-chat (default) · `1` dedicated bot number (needs a 2nd WhatsApp number). | optional |
| `SOTTO_HIDE_AGENT_NAME` | `1` drops the ***Sotto*** reply prefix on WhatsApp messages entirely (default: prefixed, so you can tell its messages from yours in self-chat). | optional |
| `SOTTO_TOOL_PROGRESS` | tool-progress heartbeat while Sotto works: `new` (default — one edit-in-place bubble, cleaned up on delivery) · `off` (narration only) · `all`/`verbose` (debugging). | optional (UX) |
| `GATEWAY_ALLOW_ALL_USERS` | `true` = open access (testing only). | optional |
| `GOOGLE_OAUTH_CLIENT_JSON` | **optional now** — paste the client JSON in the `/setup` wizard instead (no var, no redeploy). This var remains as a legacy/headless fallback (loaded at boot). | optional (legacy) |
| `GOOGLE_AUTH_CODE` | the one-time code from `/google/auth`; **clear it** after `Google: connected ✓`. | during Google connect |
| `GRANOLA_API_TOKEN` + `GRANOLA_MCP_CMD` | optional Granola MCP (step 6c) — token + a remote-capable server command. | optional |
| `BRIDGE_TOKEN` | shared bearer between the Mac Bridge, the relay, and Hermes. Manual deploys: pick a long random secret (`openssl rand -hex 24`) **before opening the setup link** — unset, the pairing link carries an empty token and pairing silently fails. (A template deploy will auto-generate it, `${{ secret(48) }}`.) You never type it into the Mac app — the `/setup` pairing link carries it. | **required** for the Bridge |
| `SOTTO_TRIGGER_TOKEN` | separate bearer for the Bridge → cloud wake-push. Unset = the wake-push authenticates with `BRIDGE_TOKEN` (one shared bearer; wake-push is on by default in the Mac app). | optional |
| `SOTTO_SETUP_CODE` | pin the access code gating the setup surface (`/setup`, `/pair`, `/whatsapp/qr`, `/google/*`, `/debug/google`). Unset = auto-generated once and persisted on `/data`; the full setup link prints in every boot's deploy logs. | optional |
| *(do not set)* `PORT` | injected by Railway; the receiver binds it. | — |

## Keeping Sotto up to date

Three update axes, each independent:

**1. Sotto itself (skills, prompts, pipeline) — automatic.** Railway redeploys on every push to the
repo it tracks, and `start.sh` refreshes the Sotto skills + persona from the image on **every boot**.
You never hand-update these; a redeploy is the update. Your data (`/data` volume: knowledge graph,
WhatsApp login, Google token) survives every redeploy untouched.

**2. The Hermes runtime — on request.** Docker caches the Hermes install layer, so routine code
pushes deliberately do NOT change your Hermes version. Every boot log prints a line like
`[sotto] hermes running: <ver> | image built with: <ver>` — that's your version check. To upgrade:

1. Bump `ARG HERMES_REFRESH=<any new string>` in the `Dockerfile` (e.g. today's date) and push —
   the rebuild re-runs the installer and bakes the latest Hermes.
2. Set `SOTTO_REFRESH_HERMES=1` in Railway variables and redeploy **once** — the boot adopts the new
   runtime on the volume (a denylist protects your WhatsApp login, sessions, config, and knowledge).
3. Confirm the boot log's version line, then **unset** `SOTTO_REFRESH_HERMES`.

If the boot log ever warns `running Hermes differs from this image's`, that's the signal to do step
2–3 (the image already has a newer Hermes than your volume).

*Deploying from a repo you don't maintain?* Step 1 is the maintainer's (they bump the knob and the
repo sync triggers your rebuild) — you only ever do steps 2–3, and only when the boot log warns.

**3. The Mac Bridge — download the new build.** Bridge updates ship as signed DMGs on
[Releases](https://github.com/kothari-nikunj/sotto/releases/latest). Download the latest, drag
**Sotto Bridge.app** over the old one in /Applications, and relaunch — your pairing (host + token) and
Full Disk Access persist (the signing identity is stable, so macOS treats it as the same app).

## Troubleshooting
| Symptom | Fix |
|---|---|
| Build: `COPY … not found` | Wrong build context — leave Root Directory blank so Railway builds from the repo root (step 2). |
| Build: `Dockerfile does not exist` | Clear the Dockerfile Path so Railway auto-detects `./Dockerfile` at the repo root. |
| `hermes: command not found` at boot | Hermes install/PATH in the image — capture the build-log line. |
| Deploy log: `No messaging platforms enabled` | `start.sh` enables WhatsApp via `~/.hermes/.env`; redeploy on the latest `main`. |
| Deploy log: `No user allowlists configured` | Set `WHATSAPP_ALLOWED_USERS` (step 5), or `GATEWAY_ALLOW_ALL_USERS=true` to test. |
| Deploy log: `WhatsApp enabled but not paired` | First boot — `start.sh` runs `hermes whatsapp`; scan the QR in the deploy logs within ~15 min. |
| Setup link in the logs says `localhost` | No public domain yet — the logged link falls back to `http://localhost:…`. **Settings ▸ Networking ▸ Generate Domain** (step 4), redeploy, use the freshly printed link. |
| Missed the ~15-min QR window | Not fatal — the container recycles and pairing reopens on the next boot. Redeploy (or restart) and scan the fresh QR at `/whatsapp/qr`. A transient "No pairing in progress" on that page just means the pairing step hasn't (re)started yet — wait for the boot to reach it. |
| Briefs never arrive (chat may still reply) | Usually a bad Gemini key or exhausted quota. Check the boot key-check log line — `[sotto] Gemini key OK (model … available)` vs `[sotto] WARNING: Gemini key/model check failed (HTTP …)` — and tail **`/debug/brief-log`** for the last brief attempt (bearer-protected: `curl -H "Authorization: Bearer $BRIDGE_TOKEN" https://<app>.up.railway.app/debug/brief-log`). Fix `GOOGLE_AI_API_KEY` / `SOTTO_GEMINI_MODEL`, or wait out / raise the quota (see `SOTTO_FALLBACK_API_KEY`). |
| No reply when messaging yourself (self-chat) | Confirm `WHATSAPP_ALLOWED_USERS` matches your number **exactly** (country code, no `+`, no spaces) — a mismatch is silently denied. Check the gateway logs for a deny line. Still nothing? Try `SOTTO_WHATSAPP_MODE=1` with a second WhatsApp number to rule self-chat delivery in/out. |
| Google disconnects after ~a week (day 8) | OAuth consent screen left in **Testing** — its refresh tokens expire after ~7 days. Publish the app to **In production** (§6b — no Google verification needed for your own data), then reconnect once. |
| `/setup` (or `/whatsapp/qr`, `/google/*`) returns **403 Forbidden** | The setup surface needs its access code — open the full link from the deploy logs (`[sotto] Setup link`); a cookie then covers the other pages. Old bare bookmarks 403 by design. |
| Lost the setup link | It reprints on **every boot** (redeploy and check the logs), or read `/data/setup_code` on the volume. `SOTTO_SETUP_CODE` pins it. |
| WhatsApp QR re-prompts every deploy | Volume not mounted at `/data` (step 3), or `start.sh` state-persist step failed. |
| Mac can't reach the trigger | No public domain (step 4), or `SOTTO_TRIGGER_TOKEN` mismatch. |
| Local data missing from briefs (messages/calls/contacts empty) | Run `sotto-bridged --doctor` on the Mac — it names each source `ok` / `needs Full Disk Access` / `unavailable` and prints the exact FDA fix (grant Full Disk Access to the *right* app: the `.app` for GUI runs, the terminal for CLI runs). Exit `0` = all sources read. |
