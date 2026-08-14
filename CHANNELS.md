# Choosing your channel and model

Sotto ships with two **defaults**, not two requirements: briefs land in **WhatsApp**, and the brief
pipeline runs on **Gemini**. Both are choices. This page is the choice, made once, in about a minute
— and it is written for a human *or* an agent (Claude Code, or Sotto itself) setting this repo up.

**Your channel** — where the brief lands and where you chat with Sotto:

- **WhatsApp** — *the default.* No bot account, no token: scan one QR on the `/setup` page. The only
  channel this repo pairs, probes and verifies for you. Pick this unless you have a reason not to.
- **Telegram** — *a bot token from @BotFather, no phone pairing.* The right call when WhatsApp
  pairing is painful (no phone handy, a linked-devices limit, a number you don't want linked). Costs
  ~5 minutes and two Railway variables. Works, but is **less tested than WhatsApp** — see the honest
  status below.
- **iMessage (BlueBubbles)** — *blue bubbles, at a price:* an always-on Mac, a Firebase project and a
  tunnel. Nothing here automates it; it is a hand-wired recipe. Hours, not minutes.

**Your model** — one sentence each, the full study is [docs/MODELS.md](docs/MODELS.md):

- **Gemini** — *the default, and what the brief pipeline actually calls today.* One
  `GOOGLE_AI_API_KEY` covers briefs, chat, triage and research.
- **Anthropic / OpenAI / Kimi / DeepSeek / xAI** — *available for the chat layer* (Ask Sotto, nudge
  replies), because Hermes owns that model and ships those providers. **Not** for the briefs: the
  brief pipeline POSTs Gemini's REST API directly. On the cloud container there is one more catch —
  see "Switching the chat model" below.
- **Exa / Parallel** — *web research, independent of both.* Set `EXA_API_KEY` and/or
  `PARALLEL_API_KEY` and attendee/company research stops going through Gemini entirely.

Reply *sending* is unaffected by all of this. An **email** draft is offered into your Gmail drafts
("want this in your Gmail drafts?" — it lands in the right thread, and you press send yourself);
every other channel is a one-tap deep link (`imessage:` / `sms:` / `wa.me`) you tap on your phone,
so your contacts receive a real iMessage or SMS no matter which channel delivers the brief to you.
Without Google connected, email falls back to a `mailto:` link like everything else.

## The honest status of each channel

Hermes' gateway supports 20+ surfaces (`hermes gateway setup`). What *this repo* does for each:

| | **WhatsApp** | **Telegram** | **iMessage (BlueBubbles)** |
|---|---|---|---|
| Status here | **first-class — the tested path** | **works, less tested** | **recipe only, not automated** |
| Runs fully in the cloud | ✅ | ✅ | ❌ needs an always-on Mac |
| Setup | ~2 min, scan a QR | ~5 min, make a bot | ~30–45 min + a Firebase project |
| Paired for you on boot | ✅ `start.sh` runs pairing, serves the QR at `/whatsapp/qr` | ❌ you set variables; no pairing needed | ❌ hand-wired |
| A `/setup` wizard tile | ✅ tile ③ | ❌ (the wizard still shows the WhatsApp tile — ignore it; it does **not** block the wizard from completing) | ❌ |
| `SOTTO_CRON_DELIVER` target | ✅ default | ✅ | ✅ (whatever name `hermes gateway setup` registered) |
| Nudge delivery-gate | probes the live WhatsApp link before spending a nudge | nothing to probe → nudges always dispatch | nothing to probe |
| Feels like a normal contact | ✅ | ⚠️ a bot, not a contact | ✅ blue bubbles |
| Cost | free | free | free, but a Mac powered 24/7 |

**TL;DR:** **WhatsApp is the recommendation.** Take Telegram when WhatsApp pairing is the problem.
Take iMessage only if blue-bubble delivery is itself the point and you'll maintain a Mac for it.

## WhatsApp setup (default)

**Pairing is automated — you run no command.** On first boot `start.sh` runs the pairing step and
serves a clean QR on a web page. Open the `/setup` wizard from the deploy logs (`[sotto] Setup link`)
and click **"Show WhatsApp QR"** (tile ③ — it opens `/whatsapp/qr`), then on your phone: WhatsApp ▸
**Linked Devices** ▸ Link a Device ▸ scan. (Scan from that web page, not the deploy logs — Railway's
log viewer distorts the terminal QR.) Done — the brief arrives as a WhatsApp message from your linked
number. No Mac involvement, no token. The session persists on the `/data` volume, so later boots skip
pairing.

Variables: `WHATSAPP_ALLOWED_USERS` and `WHATSAPP_HOME_CHANNEL`, both your number with country code
and no `+`. `SOTTO_CRON_DELIVER` can stay unset — `whatsapp` is its default.

*(`hermes gateway setup` is the manual gateway wizard — for **local** Hermes or the **other**
channels below, not for cloud WhatsApp, which the container pairs automatically.)*

## Telegram setup

Five minutes, no phone pairing, and it is a genuinely followable path — but it is **not the path this
repo exercises**, so read the caveats at the end before you commit to it.

1. **Make the bot.** In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` → give it a
   name and a username → it replies with a **bot token**. Then message your new bot once (`/start`) so
   it is allowed to message you back.
2. **Set the variables in Railway.** The token variable and the allow/home-channel variables belong to
   **Hermes**, not Sotto — run `hermes gateway setup` ▸ Telegram once (locally, or in a container
   shell) to see the exact names it wants for your Hermes version, then set those same names in
   Railway → Variables. Every variable you set whose name starts with `TELEGRAM_` is forwarded into
   `~/.hermes/.env` on boot, which is where Hermes reads messaging-platform settings from — the same
   mechanism the WhatsApp keys use. Your chat id is what the home-channel variable wants (ask
   [@userinfobot](https://t.me/userinfobot) for it).
3. **Point delivery at it:** `SOTTO_CRON_DELIVER=telegram`. This is the one lever that moves the
   briefs, the midday digest, the weekly pulse, your personal `user-` routines and the proactive
   watcher off WhatsApp — they are all registered with `--deliver "$SOTTO_CRON_DELIVER"`, and there is
   no per-job override by design.
4. **Skip WhatsApp entirely (optional):** `WHATSAPP_ENABLED=false`. Without this, first boot still
   runs the WhatsApp pairing step and waits up to 15 minutes for a QR scan that will never happen.
   With it, boot goes straight to the gateway.
5. **Redeploy**, then check the boot log for `[sotto] gateway variable forwarded to Hermes:
   TELEGRAM_…` and `deliver=telegram` on the `[sotto] cron scheduler:` line. Message your bot
   **"set up Sotto"** — the guided setup verifies your connections and reports the delivery channel it
   found. When it works, the 6:30/17:30 briefs arrive as messages from your bot.

**What is and isn't verified.** Sotto's *own* channel-awareness is real and unit-tested: the cron
`--deliver` target, the nudge delivery-gate (a Telegram deployer is never denied the release valve or
the post-meeting tap the way an unlinked WhatsApp deployer is), and the `/setup` wizard's completion
gate, which does not block on WhatsApp when you deliver elsewhere. What has **not** been run end to
end by this project is a live Telegram deploy — the bot token reaching Hermes, and a brief landing in
a Telegram chat. Two known rough edges either way: the `/setup` wizard still renders a "Link WhatsApp"
tile that a Telegram deployer should ignore, and the boot-time key check only ever validates the
Gemini key, never the gateway. If you hit something here, it is a gap worth reporting, not a
mystery — the delivery target is one variable and the boot log states it.

## iMessage via BlueBubbles

iMessage has **no cloud API**, so a Mac signed into iMessage must always be running. BlueBubbles is
the open-source bridge that exposes that Mac's Messages over an authenticated API for Hermes to use.
Nothing in Sotto's installers automates any of this.

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

- **The briefs (and triage, prep, follow-ups) require `GOOGLE_AI_API_KEY`.** They are deterministic
  Python calling Gemini's REST endpoint directly — no other provider's key does anything for them
  today. `SOTTO_GEMINI_MODEL` picks *which* Gemini model; it cannot pick a different vendor.
- **The chat layer is Hermes', and Hermes ships Anthropic, OpenAI, Kimi/Moonshot, DeepSeek, xAI and
  OpenRouter.** On a **local or existing** Hermes, `model.provider` + that provider's key switches
  Ask Sotto and every nudge reply with zero Sotto changes (the installer leaves your global model
  alone unless you pass `--dedicated`). On the **cloud container**, note that `start.sh` runs
  `hermes config set model gemini-3.7-flash` on **every** boot, so a config edit is reverted by the
  next redeploy — switching there means editing that one line in your fork. Say so out loud rather
  than promising a knob that isn't there.
- **Research is independent of both.** `EXA_API_KEY` (search) and `PARALLEL_API_KEY` (deep research)
  are used ahead of Gemini grounding whenever present, chosen by key presence alone.

The full call-site map, measured prompt sizes and a five-model comparison:
[docs/MODELS.md](docs/MODELS.md).

## Local (Mac) delivery

Everything above assumes the cloud container, but the gateway is the same on a **local** Hermes
([LOCAL-SETUP.md](LOCAL-SETUP.md)): run `hermes gateway setup` then `hermes gateway` on the Mac and
scheduled briefs deliver over the same channels (the local installer's crons default to
`--deliver whatsapp`; `SOTTO_CRON_DELIVER` overrides). Interactive CLI chat (`hermes`) needs no
channel at all. Caveat: local delivery only runs while the Mac is awake.

## Switching later

The channel is just a Hermes gateway — add or change it anytime with `hermes gateway setup` plus the
matching `SOTTO_CRON_DELIVER`, without touching the Sotto backend, the Bridge, or the knowledge graph.

Sources: [BlueBubbles install](https://bluebubbles.app/install/) · [BlueBubbles manual setup](https://docs.bluebubbles.app/server/installation-guides/manual-setup) · [Hermes messaging gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)
