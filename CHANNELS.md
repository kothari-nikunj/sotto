# Choosing your channel and model

Sotto ships with two **defaults**, not two requirements: briefs land in **Telegram**, and the brief
pipeline runs on **Gemini**. Both are choices. This page is the choice, made once, in about a minute
— and it is written for a human *or* an agent (Claude Code, or Sotto itself) setting this repo up.

**Your channel** — where the brief lands and where you chat with Sotto:

- **Telegram** — *the default.* One variable: a bot token from [@BotFather](https://t.me/BotFather).
  You tap the link the boot log prints and the capture does the rest — no user-id hunting, no second
  and third variable, no phone pairing. Pick this unless you have a reason not to.
- **WhatsApp** — *a real contact instead of a bot, at the price of a QR.* Two variables plus
  `WHATSAPP_ENABLED=true`, then scan a QR from the `/setup` page with your phone. The container still
  pairs and probes it for you; it is simply no longer what you get by default.
- **iMessage (Photon)** — cloud delivery using your own Photon project and messaging connection.
  Sotto installs the adapter for you; you supply the provider credentials and your exact sender
  identity. Your Mac does not have to stay awake for delivery. [Full setup below](#photon-imessage-and-photo-briefs).
- **iMessage (BlueBubbles)** — *blue bubbles, at a price:* an always-on Mac, a Firebase project and a
  tunnel. Nothing here automates it; it is a hand-wired recipe. Hours, not minutes.

**Your model** — one sentence each, the full study is [docs/MODELS.md](docs/MODELS.md):

- **Gemini** — *the default, and what the brief pipeline actually calls today.* One
  `GOOGLE_AI_API_KEY` covers briefs, chat, triage and research.
- **Anthropic / OpenAI** can compose briefs with `SOTTO_BRIEF_MODEL` and the matching key;
  context and capability limits are in [docs/MODELS.md](docs/MODELS.md).
- **Hermes chat** separately supports Anthropic, OpenAI, Kimi, DeepSeek and xAI. Changing chat
  does not change brief composition. See "Switching the chat model" below.
- **Exa / Parallel** — *web research, independent of both.* Set `EXA_API_KEY` and/or
  `PARALLEL_API_KEY` and attendee/company research stops going through Gemini entirely.

Replies default to drafts or links you send yourself. An explicit approved send requires a
configured sending capability; Bridge sends additionally require `--allow-send`. The channel
that delivers your brief does not grant permission to message another person.
Without Google connected, email falls back to a `mailto:` link like everything else.

## The honest status of each channel

Hermes' gateway supports 20+ surfaces (`hermes gateway setup`). What *this repo* does for each:

| Channel | What you supply | Delivery while your Mac sleeps | Photo briefs |
|---|---|---|---|
| Telegram | Your bot token; link your chat using the boot-log pairing link | Yes | Text by default |
| WhatsApp | Your number settings and QR pairing | Yes | Text by default |
| iMessage / Photon | Your own working Photon project, connection, credentials and exact owner identity | Yes | Four photos when the provider and layout support it |
| iMessage / BlueBubbles | An always-on Mac, BlueBubbles and its server connection | No | Not the Photon gallery path |

Telegram is the default template path. For iMessage, use Photon if you have provider access;
confirm its availability and charges separately. BlueBubbles is a different, manually configured
alternative. The Sotto Bridge is an optional source reader, not either messaging provider.

## Photon iMessage and photo briefs

This path is included in the public self-host runtime. You need your **own Photon project and
working iMessage connection**, including the address people message to reach it. Creating a
Railway service or installing the Mac Bridge does not allocate that connection. Check provider
access and pricing before starting; no Photon account or number is included with this repository.

1. Sign in at [Photon](https://app.photon.codes/) and prepare a project, register your iMessage
   phone number, and obtain the assigned line plus runtime project ID and secret. If you need
   guided provisioning, [Hermes' Photon setup](https://github.com/NousResearch/hermes-agent/blob/245e48008fa814b3251f50755eb656bd9fb86cb1/plugins/platforms/photon/README.md#first-time-setup)
   performs these steps. An operator can run it in an isolated Hermes environment; Sotto does not
   require installing Hermes on your Mac. Use a separate project/connection for this installation.
   Setup can rotate the project secret, so do not run it against someone else's live project.
2. Follow [ONBOARDING step 1](ONBOARDING.md#1--deploy-the-backend-on-railway)'s **manual GitHub
   deployment** using the public repository. The current Railway template is Telegram-oriented
   and requests a bot token; it is not an iMessage provisioning template. Skip BotFather and
   Telegram pairing. Keep the Gemini key, unique `BRIDGE_TOKEN`, `/data` volume and public domain.
3. Set these additional Railway variables before deployment:

   | Variable | Value |
   |---|---|
   | `SOTTO_CRON_DELIVER` | `photon` |
   | `PHOTON_PROJECT_ID` | Your Photon project ID |
   | `PHOTON_PROJECT_SECRET` | That project's secret; keep it in Railway Variables |
   | `PHOTON_HOME_CHANNEL` | **Your registered phone number**, including `+` and country code |
   | `PHOTON_ALLOWED_USERS` | The same exact owner identity; no wildcard |

   The last two values identify **you**, the person receiving briefs and sending requests. They
   are not Sotto's provider-assigned number. For this setup, choose the registered phone number
   in Messages' **Start new conversations from** setting, rather than an Apple ID email. Leave
   `SOTTO_DEPLOYMENT_MODE` unset for self-host and omit Telegram credentials.
4. Deploy. Boot installs and enables Sotto's Photon plugin automatically. Check that the gateway
   connects without authentication errors. No separate Hermes install on your Mac is required.
5. From the configured owner identity, send a hello to your Photon connection's iMessage address.
   This first inbound message also allows a shared Photon line to reply to you; it cannot initiate
   a new conversation by itself. Confirm a reply arrives. A configured channel or a healthy HTTP endpoint alone does not prove
   messaging works; check the project/connection and exact sender identity if it stays silent.
6. Continue at [ONBOARDING step 2](ONBOARDING.md#2--open-your-setup-link): open the private setup
   link, check timezone, and connect Google, selected Mac sources or Granola. If adding the Mac
   Bridge, choose **Use my own server instead**, even though your delivery channel is iMessage.
7. Wait for the automatic first brief and confirm it appears in Messages. Eligible briefs use
   four photos; an ineligible layout can fall back to text. Check the dashboard Record for send
   status and any presentation fallback. Reopening setup should not send another welcome brief.

Protect the private setup link and provider secret. Do not enable open access to get around a
sender mismatch. Drafting a reply does not authorize sending it to another person.

The Mac Bridge supplies local context independently of delivery. While your Mac sleeps, Photon
can still deliver using server-side sources; local context may become stale. Updating the Bridge
does not provision or change your delivery provider.

## Telegram setup (default)

**One variable, and you never look up an id.** Set `TELEGRAM_BOT_TOKEN` in Railway, deploy, and tap
the pairing link boot prints: the message that link sends writes your chat id into Hermes as
`TELEGRAM_ALLOWED_USERS` + `TELEGRAM_HOME_CHANNEL` and remembers it on the `/data` volume. No
redeploy, no second and third variable, no @userinfobot.

**Why a link and not "text your bot":** a bot's @username is discoverable, so a capture that accepted
whoever messaged first would hand your briefs to a stranger who guessed it. The pairing link carries
this deploy's **setup code** — the same per-deploy secret that gates `/setup`, printed only in your
deploy log — and a message that doesn't carry it is ignored.

1. **Make the bot.** In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → give it a
   name and a username → it replies with a **bot token**.
2. **Set `TELEGRAM_BOT_TOKEN`** in Railway → Variables (with `GOOGLE_AI_API_KEY`; see
   [ONBOARDING.md § 1](ONBOARDING.md#1--deploy-the-backend-on-railway)) and deploy.
3. **Tap the pairing link** in the deploy log while it boots. It opens your bot and sends
   `/start <your setup code>`; that message is you, so your numeric id is **captured**, not looked
   up (group messages, other bots, non-message updates, and any message without the code are
   ignored). The deploy log says so:

   ```
   [sotto] delivery channel: telegram (the default); whatsapp gateway: false
   [sotto] telegram: linking your chat — tap the link below (nothing to paste back).
   [sotto] telegram: ➜ TAP THIS TO LINK YOUR CHAT:  https://t.me/your_bot?start=Xk2p-9fQzR7t
   [sotto] telegram linked ✓ — briefs and nudges deliver to chat 8675309
   ```

   Boot waits **five minutes**. Miss it and nothing breaks: the log says `telegram NOT linked yet`
   with the link to tap, the brief still composes, and the Telegram gateway is deliberately left
   **down** until you are linked — an unlinked channel can't deliver anyway, and a running gateway
   would swallow your pairing message before the next boot could see it. Tap the link, then restart
   the deploy and the capture picks it up.
4. **Check it landed.** `deliver=telegram` on the `[sotto] cron scheduler:` line, and tile ③ of the
   `/setup` wizard reads **Link Telegram · DONE**. Message your bot **"set up Sotto"** — the guided
   setup verifies your connections and reports the delivery channel it found. The 6:30/17:30 briefs
   then arrive as messages from your bot, and Sotto acknowledges every message you send with a
   tapback (👀 while it works, ✅ when it's replied, ❌ on an error; `SOTTO_REACTIONS=0` turns them
   off). Tapbacks are a Telegram perk: Hermes has no bot-reaction support on WhatsApp.

### Linking by hand (the fallback)

The capture is a convenience, not a dependency. Set `TELEGRAM_ALLOWED_USERS` yourself and boot skips
the capture entirely — explicit configuration always wins. The `TELEGRAM_*` names belong to
**Hermes**, not Sotto: `start.sh` forwards *every* variable whose name starts with `TELEGRAM_` (or
`WHATSAPP_` / `DISCORD_` / `SIGNAL_` / `SLACK_` / `BLUEBUBBLES_`) into `~/.hermes/.env` on boot, where
Hermes reads messaging-platform settings from — so a name Hermes doesn't know is simply inert. If
your Hermes version wants different ones, `hermes gateway setup` ▸ Telegram is the authority.

| Variable | Value | Why |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | the token @BotFather replied with | how Hermes signs in as your bot — **the only one you must set** |
| `TELEGRAM_ALLOWED_USERS` | your numeric Telegram user id | who may use the bot — deny-all until set. **Captured for you** at boot from the pairing link; set it yourself and the capture never runs (and the gateway starts straight away) |
| `TELEGRAM_HOME_CHANNEL` | the same id | where proactive delivery lands: the 6:30/17:30 briefs, nudges and follow-ups. Captured with the one above |
| `SOTTO_CRON_DELIVER` | `telegram` | the one lever that moves the briefs, the midday digest, the weekly pulse, your personal `user-` routines and the proactive watcher between channels — they are all registered with `--deliver "$SOTTO_CRON_DELIVER"`, and there is no per-job override by design. **Defaults to `telegram`**, so you only set it to choose something else |

There is also a **linker you can run by hand** —
`python3 /app/trigger-receiver/telegram_link.py --token <YOUR_BOT_TOKEN> --phrase <ANY_PHRASE>`
inside your container (Railway ▸ your service ▸ ⋮ ▸ Shell, or `railway ssh`). It is the same code
boot runs: it validates the token, tells you *which* bot you pasted, prints the one-tap link for the
phrase you chose, waits for a message carrying it, and prints the four lines above ready to paste.
There is no default phrase — a capture without one would link whoever finds the bot first. Use it when you want to see the handshake happen, or to link a bot without a redeploy.

**What is and isn't verified.** Sotto's *own* channel-awareness is real and unit-tested: the boot
channel decision (including the migration rule below), the chat-id capture and its forwarding, the
cron `--deliver` target, the nudge delivery-gate, and the `/setup` wizard's per-channel tile and
completion gate. What has **not** been run end to end by this project is a live Telegram deploy — the
bot token reaching Hermes and a brief landing in a Telegram chat. Known rough edge either way: the
boot-time key check only ever validates the Gemini key, never the gateway. If you hit something here,
it is a gap worth reporting, not a mystery — the delivery target is one variable and the boot log
states it.

## WhatsApp setup (opt-in)

WhatsApp is not the default any more, and nothing about it was removed: the container still pairs it
for you, serves a clean QR, and probes the link before spending a nudge.

Set **three** variables in Railway → Variables:

| Variable | Value |
|---|---|
| `WHATSAPP_ENABLED` | `true` — the boot-time pairing step (and the gateway) run only when this is on, or when WhatsApp is already your channel |
| `WHATSAPP_ALLOWED_USERS` | your number, country code, no `+` (e.g. `15551234567`) — Hermes denies all users until it is set |
| `WHATSAPP_HOME_CHANNEL` | the same number — where proactive delivery lands |

…plus `SOTTO_CRON_DELIVER=whatsapp` to move the briefs there. Then deploy and **scan the QR**: open
the `/setup` wizard from the deploy logs (`[sotto] Setup link`) and click **"Show WhatsApp QR"**
(tile ③ — it opens `/whatsapp/qr`), then on your phone: WhatsApp ▸ **Linked Devices** ▸ Link a Device
▸ scan. (Scan from that web page, not the deploy logs — Railway's log viewer distorts the terminal
QR.) The session persists on the `/data` volume, so later boots skip pairing. Miss the ~15-minute
window and a redeploy reopens pairing with a fresh QR.

**Already delivering to WhatsApp? Nothing changes.** One sentence, and it is pinned by a test: *the
channel is Telegram unless this volume already holds a paired WhatsApp session and no
`TELEGRAM_BOT_TOKEN` is set.* So an instance that was set up before Telegram became the default keeps
delivering to WhatsApp across redeploys without setting anything — and the boot log names the channel
and the reason every time. Paste a bot token and you have chosen Telegram; that wins.

*(`hermes gateway setup` is the manual gateway wizard — for **local** Hermes or the **other**
channels below, not for the cloud, which links your channel automatically.)*

## iMessage via BlueBubbles

This alternative uses BlueBubbles to expose a Mac's Messages over an authenticated API for Hermes.
That Mac must stay awake. It is separate from Photon cloud delivery above; Sotto's installers do
not automate the BlueBubbles setup.

What it requires (per BlueBubbles docs):
1. **An always-on Mac** signed into your iMessage account, on power + internet 24/7.
2. **BlueBubbles Server** installed, granted **Full Disk Access** + **Accessibility**.
3. A **Google Firebase** project (free) — BlueBubbles uses Firebase Cloud Messaging for push.
4. A **server password** and a public URL — BlueBubbles has built-in Ngrok/Cloudflare proxying, so no
   port-forwarding. (You can reuse the same Mac that runs the Sotto Bridge.)
5. In Hermes: `hermes gateway setup` ▸ **BlueBubbles**, then the **server URL + password**; any
   `BLUEBUBBLES_*` variables you set in Railway are forwarded on boot like the Telegram ones.
6. Finally `SOTTO_CRON_DELIVER=<the channel name that wizard registered>`.

Trade-off: native blue-bubble delivery, but you now maintain an always-on Mac, a Firebase project and
the BlueBubbles server. If that Mac sleeps, delivery stops. (This is the *delivery* half only — the
Sotto Bridge stays read-only and reply-sending stays deep links.) Giving Sotto its own Apple ID and
its own blue bubble is a further step, with its own risks: [docs/BLUEBUBBLES.md](docs/BLUEBUBBLES.md).

## Switching the chat model

Two layers, and only one of them is switchable without editing a file:

- **Briefs, prep and follow-ups** have their own provider setting, `SOTTO_BRIEF_MODEL`.
  **Triage** uses `SOTTO_TRIAGE_MODEL`. Gemini is the default; OpenAI and Anthropic are supported
  with the limits in [docs/MODELS.md](docs/MODELS.md). Background history learning has a separate
  proxy/spend requirement, described in [RAILWAY.md](RAILWAY.md).
- **The chat layer is Hermes', and Hermes ships Anthropic, OpenAI, Kimi/Moonshot, DeepSeek, xAI and
  OpenRouter.** On a **local or existing** Hermes, `model.provider` + that provider's key switches
  Ask Sotto and every nudge reply with zero Sotto changes (the installer leaves your global model
  alone unless you pass `--dedicated`). On the **cloud container**, note that `start.sh` runs
  `hermes config set model gemini-3.8-flash` on **every** boot, so a config edit is reverted by the
  next redeploy — switching there means editing that one line in your fork. Say so out loud rather
  than promising a knob that isn't there.
- **Research is independent of both.** `EXA_API_KEY` (search) and `PARALLEL_API_KEY` (deep research)
  are used ahead of Gemini grounding whenever present, chosen by key presence alone.

The full call-site map, measured prompt sizes and a five-model comparison:
[docs/MODELS.md](docs/MODELS.md).

## Local (Mac) delivery

Everything above assumes the cloud container, but the gateway is the same on a **local** Hermes
([LOCAL-SETUP.md](LOCAL-SETUP.md)): activate the Sotto Python environment, run `hermes gateway setup`, then `hermes gateway` on the Mac and
scheduled briefs deliver over the same channels (the local installer's crons default to
`--deliver whatsapp` — a local Hermes pairs WhatsApp interactively, so that stays the laptop default;
`SOTTO_CRON_DELIVER` overrides, and the cloud boot resolves the channel for you instead). Interactive
CLI chat (`hermes`) needs no channel at all. Caveat: local delivery only runs while the Mac is awake.

## Switching later

The channel is just a Hermes gateway — add or change it anytime with `hermes gateway setup` plus the
matching `SOTTO_CRON_DELIVER`, without touching the Sotto backend, the Bridge, or the knowledge graph.

Sources: [BlueBubbles install](https://bluebubbles.app/install/) · [BlueBubbles manual setup](https://docs.bluebubbles.app/server/installation-guides/manual-setup) · [Hermes messaging gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)
