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

## 6c. Connect Granola (optional, one click)
Granola connects from the **`/setup` wizard → Connected services** tile: click **Connect**, approve
on Granola's consent screen in your browser, done. Under the hood the receiver registers *itself*
with Granola's remote MCP (OAuth 2.1 Dynamic Client Registration + PKCE — no pre-registered client,
no secret) and stores the access+refresh tokens on `/data` (`connectors/granola.json`); briefs,
meeting prep, and follow-ups then gather meeting notes + **transcripts** headlessly. Works on **any
Granola plan** — it's your login, not a Business-only API key. No env vars, no redeploy; the boot
log shows `granola connector: linked` once connected. If the flow fails, the error page names the
exact step (discovery / registration / state / exchange) + the provider's error. Full doctrine:
[INTEGRATIONS.md](INTEGRATIONS.md).

Fallbacks, only if you want them:
- `GRANOLA_API_KEY` — break-glass **official REST API** mode (`https://public-api.granola.ai/v1`;
  Business/Enterprise plans mint the `grn_…` keys in Granola's settings).
- `GRANOLA_MCP_CMD` (+ `GRANOLA_API_TOKEN`) — legacy: register your own stdio Granola MCP server;
  `start.sh` writes the `mcp_servers.granola` entry and passes the token under the common env names.

Granola is optional — fine to skip and ship Gmail + Calendar + the Bridge first.

## 7. Talk to Sotto
You chose **personal number / self-chat** (`SOTTO_WHATSAPP_MODE=2`), so *your own WhatsApp is the bot* —
you talk to Sotto by **messaging yourself**:
- WhatsApp ▸ new chat ▸ **"Message Yourself"** (your name with "(You)"), or search your own number.
- Send **"hi"** → the agent replies, prefixed ***Sotto*** (so you can tell its messages from yours —
  `start.sh` sets `whatsapp.reply_prefix`; set `SOTTO_HIDE_AGENT_NAME=1` for no prefix at all).
- A reply confirms the full round-trip (WhatsApp → Hermes → Gemini). A real brief also needs Google
  connected (step 6) and the Mac **Bridge** (step 8 below; friendly walkthrough:
  [ONBOARDING.md](ONBOARDING.md) §3 tile ① *Link your Mac*) for local data.

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
`BRIDGE_TOKEN` — into one click, leaving just two prompts (your Gemini key + WhatsApp number). Once
the template is published it's the fastest path; until then, the manual checklist above is the way in.

[![Deploy on Railway](https://railway.com/button.svg)](REPLACE_WITH_TEMPLATE_URL)

*(Button 404s? The template isn't published for this repo yet — use the manual checklist above.)*

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

## The dashboard

Your deploy URL doubles as a private web dashboard: **`https://<your-domain>/app`** — today at a
glance, open loops with one-tap resolve, the archive of every delivered brief, everyone Sotto knows
(editable, with per-fact confidence), and its learned voice + preferences. Sign in once with your
**setup code** — the same code in the `[sotto] Setup link` line of your boot logs (also on the volume
at `/data/setup_code`) — and the session lasts **30 days**. Five wrong codes lock the login for **60
seconds**: wait a minute and retry with the code from your boot log. `SOTTO_DASHBOARD=0` turns the
whole surface off (`/app` and `/api/*` answer 404).

## Environment variables — full reference
| Variable | Purpose | When |
|---|---|---|
| `GOOGLE_AI_API_KEY` | LLM key (Gemini, 1M ctx). `start.sh` maps it to `GEMINI_API_KEY`/`GOOGLE_API_KEY` for Hermes' provider too. | **required** (step 5) |
| `WHATSAPP_ALLOWED_USERS` | who may use the bot — your number, country code, no `+` (e.g. `15551234567`). Deny-all until set. | **required** |
| `WHATSAPP_HOME_CHANNEL` | where the brief is delivered proactively — your number. **Required for scheduled/proactive delivery:** the 6:30/17:30 crons, proactive nudges, and follow-ups deliver to this channel — unset, they have nowhere to land (interactive chat still replies). | **required** (delivery) |
| `SOTTO_TIMEZONE` | IANA zone (e.g. `America/Los_Angeles`) for the **6:30 morning / 17:30 evening** cron briefs + time injection. **Now optional** — the `/setup` wizard auto-detects your zone from the browser and persists it to `/data/config/settings.json` (the cron hour self-heals on the next boot). Set this only to override the auto-detected zone. | optional |
| `SOTTO_CRON_DELIVER` | where cron briefs are delivered — defaults to `whatsapp` (the WhatsApp home channel). Set to `local`, `telegram`, etc. to override. Also the delivery-channel gate for nudges: the "WhatsApp must be linked" check applies only while this is `whatsapp`. | optional |
| `SOTTO_USER_EMAIL` | your own email address; used to exclude yourself from attendee research and post-meeting taps. | optional |
| `SOTTO_GEMINI_MODEL` | override the brief's Gemini model (default `gemini-3.6-flash`). Must be 1M-context. | optional |
| `SOTTO_CRITIC` | the brief's second-pass Gemini **critic + revise** quality gate (`auto` \| `always` \| `off`, default `auto`). `auto` skips the two extra Gemini calls on a **small/low-risk** brief (rendered source payload `<15000` chars AND `≤5` actions) and runs them otherwise; `always` = every brief; `off` = never. | optional (quality) |
| `SOTTO_FALLBACK_MODEL` | backup **1M-context** Gemini model id the brief falls back to on a 429/5xx/timeout. Defaults to `gemini-3-flash-preview` (cheaper than the primary, and a separate per-model rate-limit bucket — works with your existing key, no second key needed); set it to override (e.g. `gemini-2.5-pro`). The brief prompt is 100K–140K chars, so the backup MUST be 1M-context. | optional (resilience) |
| `SOTTO_FALLBACK_API_KEY` | optional second Gemini key (different project) used for the fallback — dodges per-project quota (the 429 storm). Can be set alone (same model, backup key) or with `SOTTO_FALLBACK_MODEL`. | optional (resilience) |
| `SOTTO_ALLOW_SELF_IMPROVE` | `1` to allow Hermes' skill self-writes + Curator on this instance. Default (unset) **protects** Sotto's skills: gates `skills.write_approval`, disables curator pruning. Set `1` only on a shared general-purpose Hermes. | optional |
| `SOTTO_REFRESH_HERMES` | `1` for **one boot** adopts the image's Hermes runtime onto the `/data` volume (see *Staying updated*). A denylist protects WhatsApp login, sessions, config, SOUL, and the knowledge graph. Unset after the version line confirms the upgrade. | optional (upgrade) |
| `SOTTO_RESEARCH_CONCURRENCY` | parallel attendee-research sub-agents (`delegation.max_concurrent_children`). Default `5`. | optional |
| `SOTTO_RESEARCH_DEEP` | `1` (default) runs the second, recency-focused attendee-research pass (recent posts/news, not just a bio); `0` keeps only the cheap first pass. | optional |
| `SOTTO_RESEARCH_RECENCY_DAYS` | how recent "recent" is for that second pass (default `90` days). | optional |
| `SOTTO_PREWARM_RESEARCH` | first-run seed: background-researches your most-frequent contacts while pre-warming the knowledge graph (stored as clearly-labeled low-confidence notes). Default **on**; `=0` skips the research and seeds plain identity stubs only. | optional |
| `SOTTO_PREWARM_MAX` | how many top contacts the first-run seed pre-warms (default `12`). | optional |
| `SOTTO_DASHBOARD` | `0` disables the web dashboard entirely — `/app` and `/api/*` answer 404 (default: on). See *The dashboard* above. | optional |
| `SOTTO_UPDATE_CHECK` | `0` turns off the once-a-day "a newer Sotto is published" check (default: on — one GET of the repo's `VERSION`, flagged only on `/setup`). See *Staying updated* below. | optional |
| `SOTTO_PROACTIVE` | `1` (default) runs the mostly-silent proactive nudge cron (meeting-about-to-start / due commitment / birthday, draft-ready, never auto-send). `0` disables it. | optional |
| `SOTTO_PROACTIVE_CRON` | proactive watcher interval (default `*/15 * * * *`). | optional |
| `SOTTO_QUIET_START` / `SOTTO_QUIET_END` | quiet-hours window, shared by the proactive watcher AND the real-time event funnel + release valve (defaults `21` / `7` — no nudges 9pm–7am; a missed call from a VIP is the one carve-out). | optional |
| `SOTTO_PROACTIVE_LEAD_MIN` | how many minutes before a meeting to nudge (default `45`). | optional |
| `SOTTO_RETUNE_OFFER_MIN` / `SOTTO_RETUNE_OFFER_COOLDOWN_DAYS` | when the proactive watcher offers a loop cleanup: at least this many stale loops (default `6`), at most once every this many days (default `7`). | optional |
| `SOTTO_STALE_AGE_DAYS` / `SOTTO_STALE_SURFACED` | what `sotto-retune` calls a stale loop — this many days old (default `4`) or surfaced this many times (default `3`). | optional |
| `SOTTO_FOLLOWUP_MERGED` | `1` (default) — post-meeting follow-ups run **inside the 17:30 evening brief** (one end-of-day report; the old standalone 16:45 cron is retired, and any leftover registration becomes a no-op). Set `0` only to restore the standalone-cron behavior. The on-demand "follow up on my meetings" skill ignores this. | optional |
| `SOTTO_FOLLOWUP_DEFAULT_HOURS` / `SOTTO_FOLLOWUP_MIN_HOURS` / `SOTTO_FOLLOWUP_MAX_HOURS` | look-back window for the follow-up section of the evening brief: first-run bootstrap window and the clamp on the since-last-run gap (defaults `36` / `1` / `72`). | optional |
| `SOTTO_EVENTS` | **Bridge-side** kill switch for real-time event push (`/bridge/events`). Default on; `0` stops the watcher entirely. | optional (events) |
| `SOTTO_EVENTS_TICK_SECS` | **Bridge-side** event-watcher tick interval in seconds (default `3`). | optional (events) |
| `SOTTO_EMAIL_POLL_SECS` | cloud-side Gmail poll interval feeding new inbox mail into the triage funnel (default `90`; `0` disables email events). | optional (events) |
| `SOTTO_TRIAGE_MODEL` | the cheap Tier-1 event-triage model (default `gemini-3.5-flash-lite`). | optional (events) |
| `SOTTO_EVENT_COOLDOWN_MIN` | per-thread agent-nudge cooldown — at most one event nudge per thread per this many minutes (default `20`; suppressed events queue for the digest). | optional (events) |
| `SOTTO_NUDGE_BUDGET` | cross-thread daily interrupt budget — at most this many event nudges per **local day** (default `4`), counting release-valve promotions. Beyond it, agent verdicts queue for the digest/next brief (class `budget`). Missed calls, escalations and post-meeting taps are exempt — **taps have their own cap** (`SOTTO_TAP_MAX_PER_DAY`, default `3`), so interrupts spend the daily budget, taps spend the tap cap, and neither eats the other. Per-thread cooldowns stop one sender repeating; this is what stops ten senders in an hour. | optional (events) |
| `SOTTO_TAP_MAX_PER_DAY` | post-meeting taps per **local day** (default `3`), enforced once at dispatch. Independent of `SOTTO_NUDGE_BUDGET`. | optional (events) |
| `SOTTO_MEETING_TAP` | `1` (default) fires the post-meeting tap ("your 2:00 PM with Sarah wrapped — want me to send the follow-up?"); `0` disables it. | optional (events) |
| `SOTTO_TAP_GRACE_MIN` | how many minutes after a meeting ends before its tap may fire — you're out of the room, not packing up (default `5`). | optional (events) |
| `SOTTO_TAP_LOOKBACK_MIN` | how far back each calendar tick looks for a meeting-end it hasn't tapped yet (default 2× `SOTTO_CALENDAR_REFRESH_SECS`, floor `30`) — wide enough that one skipped tick still delivers, narrow enough that a restart doesn't replay the whole day. | optional (events) |
| `SOTTO_TAP_SKIP_INTERNAL` | `1` (default) skips taps for internal-only standups/syncs (title shape × everyone on your own email domain); `0` taps them too. | optional (events) |
| `SOTTO_ESCALATION_WINDOW_MIN` | the cross-channel escalation window (default `45`) — the same known person reaching you on a **second** channel inside it, with a call or a real ask on either side, gets one nudge that skips the cooldown and the daily budget (once per window). | optional (events) |
| `SOTTO_EVENT_MAX_AGE_MIN` | reconnect grace — an event older than this many minutes (or any message in a Bridge catch-up batch) never nudges in real time; it queues for the digest/next brief. Missed calls are exempt. Default `30`. | optional (events) |
| `SOTTO_VIP_PRIORITY` | the attention-queue priority at which a contact counts as a VIP (default `10`) — VIPs are the only senders whose missed call clears quiet hours. A "family" mention in their knowledge-graph file also qualifies. | optional (events) |
| `SOTTO_USER_NAME` | your name, used to detect an @-mention in a group chat. Unset → group messages **always** queue and never nudge (the conservative default). | optional (events) |
| `SOTTO_VALVE` | `1` (default) runs the release valve — the heartbeat that lets an event held by cooldown/quiet hours/catch-up/budget/a meeting out as a nudge once the hold lifts. `0` disables it (held events then wait for the digest or the next brief). | optional (events) |
| `SOTTO_VALVE_INTERVAL_SECS` | how often the release valve ticks (default `900` = every 15 min). | optional (events) |
| `SOTTO_VALVE_MAX_PER_HOUR` | most promotions the valve may make per hour (default `2`; also ≤2 per tick). Promotions spend `SOTTO_NUDGE_BUDGET` like any other nudge. | optional (events) |
| `SOTTO_VALVE_MAX_AGE_MIN` | how old a held event may be and still be promoted (default `240` = 4h — a real ask from 3h ago still deserves a nudge; a 2-day-old one doesn't). Events held by the in-meeting hold skip this check: a long meeting must not expire an ask Sotto itself held. | optional (events) |
| `SOTTO_CALENDAR_REFRESH_SECS` | how often the receiver refreshes today's calendar (default `900` = 15 min). Powers the in-meeting hold, the dashboard's calendar, and post-meeting tap detection; `0` disables all three. A cache older than 2 intervals — or stamped with another day — is never used to hold a nudge. | optional (events) |
| `SOTTO_DIGEST_MIN` | how many queued **signals from people you know** (Tier-1 `ambient`, plus anything held by cooldown/quiet hours/catch-up/budget/snooze/a meeting) it takes for the 12:30 midday digest to deliver — default `8`; below it the digest stays silent. The window starts at the last brief that actually delivered, so the digest can't repeat it. | optional (events) |
| `SOTTO_DIGEST` | `1` (default) registers the `sotto-midday-digest` cron (12:30 local, adaptive catch-up). `0` disables it. | optional (events) |
| `SOTTO_WAKE_PUSH` | **BRIDGE-side** (set where the Bridge runs, not Railway) — fires the brief/nudge the moment your Mac wakes. Default **on**; `=0` (or `false`) disables (the 6:30/17:30 cron still fires). The cron↔wake-push deliver-once gate makes double-delivery impossible. | optional (Bridge) |
| `SOTTO_WAKE_MORNING_MIN` / `SOTTO_WAKE_MORNING_CUTOFF` | **Bridge-side** morning wake-push window in minutes-past-midnight — wake past this and before the cutoff triggers the morning brief (defaults `420` / `1080` = **7:00 – 18:00**). | optional (Bridge) |
| `SOTTO_WAKE_EVENING_MIN` / `SOTTO_WAKE_EVENING_CUTOFF` | **Bridge-side** evening wake-push window (defaults `1050` / `1380` = **17:30 – 23:00**). | optional (Bridge) |
| `SOTTO_TTS` / `SOTTO_TTS_PROVIDER` / `SOTTO_TTS_VOICE` | voice (read + listen). `SOTTO_TTS=1` (default) enables Hermes TTS; provider `edge` (default, free, no key) or `gemini` (uses your Google key); voice id override. `SOTTO_TTS=0` for text-only. | optional |
| `SOTTO_WHATSAPP_MODE` | `2` self-chat (default) · `1` dedicated bot number (needs a 2nd WhatsApp number). | optional |
| `SOTTO_WHATSAPP_PAIR_TIMEOUT` | how long the boot-time QR pairing step stays open, in seconds (default `900` = 15 min). | optional |
| `SOTTO_HIDE_AGENT_NAME` | `1` drops the ***Sotto*** reply prefix on WhatsApp messages entirely (default: prefixed, so you can tell its messages from yours in self-chat). | optional |
| `SOTTO_TOOL_PROGRESS` | tool-progress heartbeat while Sotto works: `new` (default — one edit-in-place bubble, cleaned up on delivery) · `off` (narration only) · `all`/`verbose` (debugging). | optional (UX) |
| `GATEWAY_ALLOW_ALL_USERS` | `true` = open access (testing only). | optional |
| `GOOGLE_OAUTH_CLIENT_JSON` | **optional now** — paste the client JSON in the `/setup` wizard instead (no var, no redeploy). This var remains as a legacy/headless fallback (loaded at boot). | optional (legacy) |
| `GOOGLE_AUTH_CODE` | the one-time code from `/google/auth`; **clear it** after `Google: connected ✓`. | during Google connect |
| *(none — use `/setup`)* | **Granola connects with zero variables**: the wizard's Connected-services tile (step 6c) runs the OAuth flow and stores tokens on `/data`. | — |
| `GRANOLA_API_KEY` | break-glass Granola **REST** mode (official API, Business/Enterprise plans) instead of the Connect tile. | optional |
| `GRANOLA_API_TOKEN` + `GRANOLA_MCP_CMD` | legacy custom stdio Granola MCP (step 6c fallbacks) — token + a remote-capable server command. | optional (legacy) |
| `BRIDGE_TOKEN` | shared bearer between the Mac Bridge, the relay, and Hermes. Manual deploys: pick a long random secret (`openssl rand -hex 24`) **before opening the setup link** — unset, the pairing link carries an empty token and pairing silently fails. (A template deploy will auto-generate it, `${{ secret(48) }}`.) You never type it into the Mac app — the `/setup` pairing link carries it. | **required** for the Bridge |
| `SOTTO_TRIGGER_TOKEN` | separate bearer for the Bridge → cloud wake-push. Unset = the wake-push authenticates with `BRIDGE_TOKEN` (one shared bearer; wake-push is on by default in the Mac app). | optional |
| `SOTTO_SETUP_CODE` | pin the access code gating the setup surface (`/setup`, `/pair`, `/whatsapp/qr`, `/google/*`, `/debug/google`). Unset = auto-generated once and persisted on `/data`; the full setup link prints in every boot's deploy logs. | optional |
| *(do not set)* `SOTTO_DATA` | the exhaust volume path — already `=/data` in the image, and every script reads it. Changing it moves all of Sotto's memory. | — |
| *(do not set)* `SOTTO_RUN_SKILL` | the host's one-shot skill runner the receiver spawns — `start.sh` sets it to `hermes -z`. Only an alternate host (e.g. OpenClaw) overrides it. | — |
| *(do not set)* `SOTTO_MCP_TOKEN` | the reverse-MCP relay bearer — `start.sh` sets it from `BRIDGE_TOKEN`. | — |
| *(do not set)* `SOTTO_TRIGGER_PORT` / `SOTTO_TRIGGER_BIND` | receiver port + bind address, used only when Railway's `PORT` is absent (local runs: `8787` on `127.0.0.1`). | — |
| *(do not set)* `PORT` | injected by Railway; the receiver binds it. | — |
| *(do not set)* `RAILWAY_PUBLIC_DOMAIN` | set by Railway once you **Generate Domain** (step 4) — the setup/Google/QR links in the deploy logs are built from it. If it's absent (no domain yet), those links fall back to `http://localhost:…`. | — |

## Staying updated

**The rule, in one sentence: Sotto tells you when it's out of date, and one redeploy is the update.**
Your server checks the published `VERSION` once a day and, when a newer build exists, says so in one
line at the foot of `/setup` (your Integrations page) — nowhere else. Nothing updates itself; you
choose when to redeploy, and your `/data` volume (knowledge graph, WhatsApp login, Google token)
survives every redeploy untouched.

**1. If you deployed with the one-click button (Railway template).** When the template's repo is
updated, Railway opens a **pull request** on your copy of the repo with the new code. Merge it and
Railway redeploys automatically — that's the whole update. (This path only exists once the template
is published for this repo; until then the Deploy button 404s and you're on the manual path below.)

**2. If you deployed manually, from a fork or your own copy.** On GitHub, open your repo → **Sync
fork** → **Update branch**. That lands the new code on your `main`, and Railway — which redeploys on
every push to the repo it tracks — rebuilds and restarts on its own. (Deployed straight from the
public repo rather than a fork? Then you're already tracking it: hit **Deploy** in Railway, or push
nothing at all and wait for the next upstream push.)

Either way `start.sh` refreshes the Sotto skills + persona from the freshly built image on **every
boot**, so there is never anything to hand-update.

**3. Hermes comes with the build.** The Hermes runtime is installed while the image is built, so a
rebuild is also a Hermes install. Two caveats, both already handled:

- Docker **caches** the install layer, so a routine code push usually reuses the Hermes already
  baked — the maintainer bumps `ARG HERMES_REFRESH` in the `Dockerfile` when a Hermes upgrade is
  actually intended, which busts the cache and re-runs the installer on the next build.
- Your `/data` volume holds a first-boot copy of `~/.hermes`, which can **shadow** a newer image.
  Every boot log prints `[sotto] hermes running: <ver> | image built with: <ver>`, and `/setup`
  shows the same pair. When they differ, set `SOTTO_REFRESH_HERMES=1` (see the variables table
  above) and redeploy **once** — the boot adopts the image's runtime, protecting your WhatsApp
  login, sessions, config and knowledge — then unset it.

**4. The Mac Bridge updates itself.** The Bridge checks
[Releases](https://github.com/kothari-nikunj/sotto/releases/latest) once a day and shows
**Update available** in its menu when a newer signed build exists; one click downloads the DMG,
verifies the signature (same Apple Team as the app you're running — anything else is refused),
installs in place and relaunches. Your pairing (host + token) and Full Disk Access persist (the
signing identity never changes, so macOS treats it as the same app). Prefer manual? Downloading the
DMG and dragging it over the old app still works exactly the same way.

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
