# Delivery channels — WhatsApp vs iMessage (BlueBubbles) vs Telegram

The **delivery channel** is where the morning/evening brief lands and where you chat with Sotto. It's
independent of **reply sending**, which is always one-tap deep links (`imessage:` / `sms:` / `wa.me` /
`mailto:`) you tap on your phone — so your *contacts* always receive real iMessage/SMS no matter which
channel delivers the brief to *you*. This doc only covers how the brief reaches you.

Hermes' gateway supports 20+ surfaces (`hermes gateway setup`). The three that matter here:

## Comparison

| | **WhatsApp** | **iMessage (BlueBubbles)** | **Telegram** |
|---|---|---|---|
| Runs fully in the cloud | ✅ yes | ❌ needs an always-on Mac | ✅ yes |
| Extra infrastructure | none | BlueBubbles server + Firebase project | none |
| Setup time | ~2 min (scan a QR) | ~30–45 min | ~5 min (make a bot) |
| Feels like a normal contact | ✅ | ✅ (blue bubbles) | ⚠️ a bot, not a contact |
| Cost | free | free (but a Mac powered 24/7) | free |
| Best for | **most people / fastest** | you specifically want native iMessage | quick/dev setups |

**TL;DR:** **WhatsApp is the easiest and the recommendation.** Choose **iMessage** only if blue-bubble,
"texts me like a person" delivery is the point and you're willing to run BlueBubbles on an always-on Mac.

## WhatsApp setup (recommended)
**In the cloud, pairing is automated — you don't run any command.** On first boot `start.sh` runs the
pairing step for you (`hermes whatsapp` via `wa_pair.py`) and serves a clean QR on a web page. Open the
`/setup` wizard from the deploy logs (`[sotto] Setup link`) and click **"Show WhatsApp QR"** (tile ③ —
it opens `/whatsapp/qr`), then on your phone: WhatsApp ▸ **Linked Devices** ▸ Link a Device ▸ scan.
(Scan from that web page, not the deploy logs — Railway's log viewer distorts the terminal QR.) Done —
the brief now arrives as a WhatsApp message from your linked number. No Mac involvement, no token. The
session persists on the `/data` volume, so later boots skip pairing.

*(`hermes gateway setup` is the manual gateway wizard — use it for **local** Hermes or **other**
channels below, not for cloud WhatsApp, which the container pairs automatically.)*

## iMessage via BlueBubbles setup
iMessage has **no cloud API**, so a Mac signed into iMessage must always be running. BlueBubbles is the
open-source bridge that exposes that Mac's Messages over an authenticated API for Hermes to use.

What it requires (per BlueBubbles docs):
1. **An always-on Mac** signed into your iMessage account, connected to power + internet 24/7.
2. **BlueBubbles Server** app installed, granted **Full Disk Access** + **Accessibility**.
3. A **Google Firebase** project (free) — BlueBubbles uses Firebase Cloud Messaging for push; you create
   the project and drop its `google-services.json` / admin JSON into the server.
4. A **server password** and a public URL — BlueBubbles has **built-in Ngrok/Cloudflare proxying**, so no
   port-forwarding. (You can reuse the same Mac that runs the Sotto Bridge.)
5. In Hermes: `hermes gateway setup` ▸ **BlueBubbles**, then enter the **server URL + password**.

Trade-off vs WhatsApp: you get native blue-bubble delivery, but you're now maintaining an always-on Mac
+ a Firebase project + the BlueBubbles server. If that Mac sleeps or loses power, delivery stops. (Note:
this is the *delivery* half only; the Sotto Bridge stays read-only and reply-sending stays deep links.)

## Local (Mac) delivery
Everything above assumes the cloud container, but the gateway is the same on a **local** Hermes
(`LOCAL-SETUP.md`): run `hermes gateway setup` then `hermes gateway` on the Mac and scheduled briefs
deliver over the same channels (the local installer's crons default to `--deliver whatsapp`;
`SOTTO_CRON_DELIVER` overrides). Interactive CLI chat (`hermes`) needs no channel at all. Caveat: local
delivery only runs while the Mac is awake.

## Switching later
The channel is just a Hermes gateway — you can add or change it anytime with `hermes gateway setup`
without touching the Sotto backend, the Bridge, or the knowledge graph.

Sources: [BlueBubbles install](https://bluebubbles.app/install/) · [BlueBubbles manual setup](https://docs.bluebubbles.app/server/installation-guides/manual-setup) · [Hermes messaging gateway](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)
