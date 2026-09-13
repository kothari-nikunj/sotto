# Set up Sotto — step by step (cloud)

The friendliest path: an always-on Sotto in the cloud + the read-only Mac Bridge. Budget **~30 minutes
the first time** — only ~15 of it active (a few Railway settings, three variables, then one wizard
page); the rest is waiting on builds. You'll finish with a morning brief in Telegram and a Mac link
that survives sleep, redeploys, and laptop lids. (Telegram is the **default**, not a requirement —
WhatsApp is the appendix at the end of this page.)

> **The one-click Deploy link** at the end of step 1 sets up the build, the `/data` volume and
> `BRIDGE_TOKEN` for you and prompts for exactly two values: your Gemini key and your Telegram bot
> token. The manual path below is the same result, click by click — do either.

> **The model in one line:** Sotto = skills + persona running on a cloud **agent** (Hermes on Railway),
> fed your local Mac signals by the **Bridge** menu-bar app. The cloud writes the briefs; your Mac only
> reads data and nudges timing. The Bridge never sends anything — replies are drafts you tap to send.

## 0 · What you'll need

| You need | Why | Where it goes |
|---|---|---|
| A **Railway** account (**paid/verified plan** — volumes + always-on need it) | hosts the agent + storage | — |
| A **Gemini API key** — [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | the LLM (only key Sotto needs) | once, into Railway |
| A **Telegram bot token** — [@BotFather](https://t.me/BotFather) → `/newbot`, ~1 min *(default channel — WhatsApp instead? the appendix)* | where briefs are delivered | once, into Railway |
| A **Google OAuth client** (5-minute console task, step 3②) | Gmail + Calendar | pasted in the wizard |
| *(optional)* The signed **Sotto Bridge.app** — [download from Releases](https://github.com/kothari-nikunj/sotto/releases/latest) — if you have a Mac and want iMessage, WhatsApp, calls, Notes and Contacts in your briefs; without it Sotto runs on Gmail + Calendar alone, a supported deploy | reads your Mac | drag to /Applications |

Everything else — linking your Mac, connecting Google, your channel, your timezone, and optional
extras like Granola — happens on **one wizard page** (`/setup`), no redeploys.

**Two defaults you can change, if you want to — decide now, it's a variable each.** Full detail (and
how tested each one is) in **[CHANNELS.md — Choosing your channel and model](CHANNELS.md)**:

| Your channel | | Your model |
|---|---|---|
| **Telegram** — default. **One variable** (`TELEGRAM_BOT_TOKEN`): you tap the pairing link boot prints and it captures your chat id — no id hunting, no phone pairing. The path this guide follows. | | **Gemini** — default, and what the brief pipeline calls today. One key covers everything. |
| **WhatsApp** — a real contact instead of a bot, at the price of a QR scan. Three variables (`WHATSAPP_ENABLED=true`, `WHATSAPP_ALLOWED_USERS`, `WHATSAPP_HOME_CHANNEL`) plus `SOTTO_CRON_DELIVER=whatsapp` — the **[appendix](#appendix--whatsapp-instead-of-telegram)** at the end of this page. | | **Anthropic / OpenAI / Kimi / DeepSeek** — for the **chat** layer only (Ask Sotto, nudge replies). The briefs still need a Gemini key. |
| **iMessage (BlueBubbles)** — blue bubbles, but an always-on Mac + Firebase + a tunnel. Hand-wired recipe, hours not minutes. | | **Exa / Parallel** — web research only, and independent of the rest: set the key and research stops going through Gemini. |

## 1 · Deploy the backend on Railway

**First, make your bot** (~1 min): in Telegram, message [@BotFather](https://t.me/BotFather) →
`/newbot` → name it → it replies with a **bot token**. That token is the only channel setting you
need; your chat id is captured for you on the first message you send the bot.

**Then the manual path — four settings, in this order.** Get this repo where Railway can
see it: **Fork** it on GitHub (recommended — that's what **Sync fork** later updates), or point
Railway straight at the public repo. Then in [Railway](https://railway.app): **New Project → Deploy
from GitHub repo** → pick it. Set **all four** before the first deploy finishes; each one fails
*quietly* if you skip it:

1. **Settings → Root Directory**: leave blank — the Dockerfile is at the repo root (leave *Dockerfile Path* blank too; both are auto-detected).
   *Get this wrong and the build dies with `COPY … not found` — the Dockerfile's `COPY` paths are
   relative to the build context this setting picks.*
2. **Variables → New Variable** — add three:
   - `GOOGLE_AI_API_KEY` = your Gemini key *(Google's own docs sometimes call it `GEMINI_API_KEY` or
     `GOOGLE_API_KEY` — Sotto accepts any of the three names, so paste it under whichever you copied)*
   - `TELEGRAM_BOT_TOKEN` = the token @BotFather gave you
   - `BRIDGE_TOKEN` = a long random secret — run `openssl rand -hex 24` and paste the output.
     *Without this, the Mac pairing link carries an empty token and pairing silently fails.*
   *(WhatsApp instead? [The appendix](#appendix--whatsapp-instead-of-telegram) — three variables, one
   QR, nothing else about this guide changes.)*
3. **⌘K → Add Volume**, mount path `/data` (your knowledge graph, your channel link and Google token
   live here). *No volume = every redeploy wipes your login and memory.*
4. **Settings → Networking → Generate Domain** — do this **before** opening the setup link in step 2,
   then redeploy once. Without a domain the logged link falls back to a dead `localhost` URL.

Deploy and wait for the build (the container installs Hermes + Sotto automatically), and **while it
boots, tap the pairing link in the logs** — the `[sotto] telegram: ➜ TAP THIS TO LINK YOUR CHAT:`
line. It opens your bot and sends a one-time code, which is how the deploy knows the chat is *yours*
and not a stranger's who guessed the bot's name. Two success signals in the deploy logs:
`[sotto] Gemini key OK (model … available)` and `[sotto] telegram linked ✓ — briefs and nudges
deliver to chat …`. A `[sotto] WARNING: Gemini key/model check failed` means the key is wrong or out
of quota — fix it before going further, because every brief depends on it. A
`[sotto] telegram NOT linked yet` just means you hadn't tapped within the five-minute window: tap the
link and restart the deploy, and it links then. Nothing else is lost either way.

**Or one click.** The button below does all four settings above and prompts for exactly two values —
your Gemini key and your bot token:

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/lvprWx)

## 2 · Open your setup link

Railway → your service → **Deployments → View logs** → find the line starting **`[sotto] Setup link`**
and open it. That's your `/setup` wizard plus its private access code — only someone with this link can
see your pairing token or your channel's QR. Open it once and your browser is remembered for the rest of the
wizard; lose it and it reprints on every boot (it's also on the volume at `/data/setup_code`).
- *Link says `localhost`?* You have no public domain yet — go back and do step 1.4, redeploy, and use
  the freshly printed link. *A bare `https://<your-domain>/setup` with no `?code=` returns **403** by
  design.*

One page, five numbered tiles (the fifth is optional). Each tile's state label reads **TO DO** until
it's finished and **DONE** after — reload the page (the wizard's own **recheck** link) to see one flip.

## 3 · The wizard, tile by tile

**① Link your Mac** — [Download Sotto Bridge.app](https://github.com/kothari-nikunj/sotto/releases/latest),
drag it to `/Applications`, open it.
- **First run asks for an access code** — access to the distributed Bridge binary is invite-only
  for now; self-host deployment itself is not. Paste the code from
  your invite (it's verified on your Mac, nothing is sent anywhere), and if you don't have one yet,
  ask in [Issues](https://github.com/kothari-nikunj/sotto/issues).
- **Updating an existing Bridge to 1.2.7 or newer asks for a code too**, once, and that install stops
  streaming to your cloud until you enter one — your pairing, Full Disk Access and settings are all
  untouched, so entering the code resumes everything with nothing to re-pair. Same
  [Issues](https://github.com/kothari-nikunj/sotto/issues) link if you don't have a code yet.
- **Choose hosting in the one Sotto setup window.** Cloud is an invite-only pilot for registered accounts. Cloud uses Google → Mac sources / Full Disk
  Access → Messages → First brief. Google sign-in alone does not start reading this Mac.
- **For self-host, the steps are Connect → Choose sources → Disk access.**
  1. **Connect.** On your host's `/setup` page, click **Open in Sotto Bridge** or paste its pairing
     link. Continue becomes available once the host and token are present.
  2. **Choose sources.** Every supported Mac reader starts on, just like the existing Mac app.
     Turn off anything you do not want shared before continuing. Existing users keep saved choices.
  3. **Disk access.** Click **Enable Full Disk Access…**, add **Sotto Bridge** under System Settings
     → Privacy & Security → Full Disk Access, then return. The live card shows **Granted**.
     **Save & Connect** starts the selected readers. **Skip Mac sources for now** turns all readers
     off and completes setup; enable sources later in Settings after granting access.
- Cloud uses that same source list and disk-access card before the Messages step. Continue starts
  selected Mac readers only after the grant; startup and reconnect cannot bypass source confirmation.
  A source being on does not claim it is available or has already been read. Meeting-note services
  still connect through their own authorization on the host's `/setup` page.
- **Success signal on the cloud side:** reload `/setup` — tile ① now reads **DONE** with
  "Your Mac is linked and reachable."
- **It keeps itself current.** The Bridge checks daily for a new signed release; when there is one
  the menu shows **"Update available"** and one click installs it in place — no re-download, no
  re-granting Full Disk Access, nothing to re-pair.
- **Start at login** is enabled for you when you finish the wizard — flip it off later in
  Settings → General if you prefer. Done — the app dials *out* to your cloud, so there's no tunnel,
  no port, nothing to keep alive. Closing and reopening your laptop needs nothing from you.
- Optional: **"Send my brief when I wake my Mac"** is on by default — open your laptop after 7am and
  the morning brief arrives moments later (the 6:30 cloud schedule covers the closed-laptop case;
  they coordinate, you never get two).

**② Connect Google** — you create your own OAuth client once (this keeps you off Google's verification
wall — it's your own data):
1. [console.cloud.google.com](https://console.cloud.google.com) → **New Project** "Sotto" → select it.
2. **APIs & Services → Enable APIs** → enable **Gmail API** and **Google Calendar API**.
3. **OAuth consent screen** in the left nav (newer consoles land you on **Google Auth Platform** —
   same place) → app name "Sotto", your email, audience **External** → then hit **Publish app** so
   the status reads **In production**, not **Testing**.
   *Do not skip this. Left in **Testing**, Google expires your refresh token after ~7 days and your
   briefs quietly lose email + calendar on day 8 — the single most common way a self-hosted Sotto
   dies. **In production** needs no Google review for you using your own data; the "needs
   verification" banner is about publishing to strangers, not about you.*
4. **Credentials → Create credentials → OAuth client ID** → **Desktop app** → Create → **Download JSON**.
5. Paste the JSON into the wizard's Google box → **Save client →** → **Authorize Google →** →
   **1 — Authorize Gmail + Calendar →** → on the "unverified app" screen click **Advanced →
   Continue → Allow** → you land on a `localhost:1/?code=…` page that won't load — that's expected;
   copy the `code` value out of the URL (everything after `code=`, before any `&`), paste it into
   the box on that same page and click **Connect**. **Success signal:** a page headed **"Google
   connected"**.
   *"Invalid code verifier"? You used a code from an older authorize link. Click **Authorize** again
   for a fresh URL and use that one's code. Never do this step through chat — the agent mints a new
   link each time, which is exactly what breaks it.*

**③ Link Telegram** — nothing to click: this tile reports the handshake your deploy already did. It
reads **"Waiting for your first message to @yourbot"** until you tap the pairing link in the deploy
logs, and **"Telegram is linked"** once boot has captured your chat id (reload to see it flip).
*Still waiting after you tapped it?* The capture runs at **boot** — restart the deploy (Railway ▸
Deployments ▸ ⋮ ▸ Restart) and it links within seconds; your message is still waiting on Telegram's
servers, because an unlinked deploy leaves its gateway down rather than consuming it. Details and the by-hand fallback:
**[CHANNELS.md § Telegram setup](CHANNELS.md#telegram-setup-default)**.
> **On WhatsApp instead?** This tile becomes **Link WhatsApp** with a **Show WhatsApp QR** button —
> see the [appendix](#appendix--whatsapp-instead-of-telegram). **On iMessage/BlueBubbles?** The tile
> says there is nothing to link and never blocks the wizard;
> [CHANNELS.md § iMessage via BlueBubbles](CHANNELS.md#imessage-via-bluebubbles) is a hand-wired
> recipe measured in hours, not minutes.

**④ Timezone** — auto-detected from your browser when the page loads; the tile reads "Timezone set to
&lt;your zone&gt;" and the page reloads itself. Nothing to click.

**⑤ Connected services (optional)** — one-click OAuth for extra sources. Click **Connect →** next to
**Granola (meeting notes)**: your browser opens Granola's consent screen, you approve, and you land
back on a page headed **"Connected"** — meeting notes + transcripts now feed your briefs, meeting
prep, and follow-ups.
It works on **any Granola plan** (it's your login, not an API key), the tokens live only on your
`/data` volume, and if anything fails the page tells you exactly which step broke. Skip freely —
everything else works without it. More lanes + adding other services:
[INTEGRATIONS.md](INTEGRATIONS.md).

## 4 · Say hello

Message your bot on Telegram: **"set up Sotto."** *(On WhatsApp, message yourself in self-chat —
same words.)*

**One-time approval:** the first time Sotto runs its pipeline it asks permission to run code
(`execute_code`). Approve with **always** (reply `/approve always`) so it never re-asks. This is a
one-time, interactive-only step — scheduled briefs run the same scripts through the terminal
tool, which needs no approval.

The guided setup verifies every connection, then seeds your memory and **writing voice** from ~6
weeks of history — a few minutes, up to ~8 the very first time (it's researching the people in your
calendar), walks you through it step by step — schedules the briefs, and closes with an honest checklist:

> Here's where you stand:
> - **Bridge** (Mac: messages, calls, contacts) — ✓ connected
> - **Google** (Gmail + Calendar) — ✓ connected
> - **Granola** (meeting notes) — – optional, skipped
> Briefs are scheduled for 6:30am and 5:30pm.

Then it offers your first brief on the spot. Say **"good morning"** and you're running.

From here it's all conversation:
- *"good morning"* / *"good evening"* — the briefs
- *"prep me for my 2pm"* / *"follow up on my meetings"*
- *"find 30 min with Alex next week"* / *"accept my 3pm"* — proposes from your real calendar, then books/RSVPs on your OK
- *"triage my inbox"* / *"what am I waiting on"*
- *"draft a reply to Sarah"* — in your voice; **you** always send
- *"who am I losing touch with"* / *"what do I know about Alex"*
- *"what can you do?"* — the full map, any time

And if a brief ever looks thin or wrong: say **"that's wrong about X"** (fixes its memory), **"stop
surfacing newsletters"** (mutes), or **"clean up stale loops"** (retunes).

## Your dashboard

The same `/setup` page is part of a private web dashboard: once your tiles are green, click
**"Open your dashboard"** (or visit `https://<your-domain>/app` — first visit asks for your setup
code once, then remembers you for 30 days). It shows today at a glance, your open loops (resolve or
dismiss in one tap), the archive of every delivered brief, everyone Sotto knows — with per-fact
confidence and sources, editable in place — and what it has learned about your voice and
preferences. Works great on your phone.

## Your first 24 hours

Day one is quiet on purpose — quiet ≠ broken:

- **Your first brief** arrives at the next 6:30am / 5:30pm — or the moment you wake your Mac after 7am.
- **Few or no nudges at first** — the triage funnel is deliberately strict; only a real ask from
  someone you know or a missed call gets through.
- **The 12:30 digest stays silent on light days** by design — no news is the feature.
- **The relationship pulse** first fires Monday at 9am.
- **The dashboard fills as briefs run** — the Briefs tab starts with your first delivered brief, and
  the Learned page (voice + preferences) populates after the first brief runs.

And three things Sotto will never do, worth knowing on day one:

- **No bot joins your calls.** Meeting notes come from Granola reading the notes you already take.
  Nothing of Sotto's dials into a meeting, and nothing records one.
- **Sotto can't send as you.** The Mac Bridge refuses to send at all unless you start it with
  `--allow-send`, and every message — brief, nudge, reply, follow-up — is a draft **you** send.
- **Every silence is auditable.** Nudged, queued, or dropped, each event gets a ledger row with its
  reason, readable in the dashboard's **Record** view (`/app#record`).

The rules behind all three: [docs/HOW-SOTTO-DECIDES.md](docs/HOW-SOTTO-DECIDES.md).

## What runs on its own

Once set up, Sotto works in the background without being asked — **everything below is draft-only; it
never auto-sends**:

- **Morning + evening briefs** — 6:30am and 5:30pm your time (and the instant you open your Mac past
  7am; the two paths coordinate so you never get a double).
- **Real-time nudges** — new messages and calls stream from your Mac within seconds and go through a
  strict triage funnel; only the genuinely interruption-worthy (a real ask from someone you know, a
  missed call) becomes a nudge — **with a reply drafted in your voice, one tap to send**. Everything
  quieter waits for an adaptive **12:30 catch-up digest** (silent on light days) or the next brief.
  If your Mac was offline for hours, the backlog drains silently — no barrage of stale pings.
- **Scheduled watchers** — a mostly-silent check every ~15 min for time-sensitive items (a meeting
  about to start, a due commitment, a birthday), with a draft ready when it does speak up.
- **Follow-up drafts** — part of the 5:30pm evening brief, for meetings that ended that day; silent
  when there's nothing to send.
- **Relationship pulse** — Mondays at 9am (who you're losing touch with).

All of it lands on **one** channel — `SOTTO_CRON_DELIVER`, which defaults to `telegram` (the chat
your bot captured). Point it at `whatsapp` and every line above moves with it; there is no
per-job override by design. Tune or disable any of them with the `SOTTO_PROACTIVE*` variables in
[RAILWAY.md](RAILWAY.md).

## If something's off

The **[RAILWAY.md](RAILWAY.md) troubleshooting table** covers the common ones: setup link says
`localhost` (generate a domain), no reply from your bot (the allowlist must be the numeric chat id —
the boot capture gets it right), briefs
never arrive (check the boot key-check line + `/debug/brief-log`), Google dying after a week (consent
screen left in Testing — step 3②.3). **Replies feel stale, or Sotto starts echoing a delivered brief
back at you:** send **`/new`** in the chat — it starts a fresh session on the latest persona.
Sessions also reset nightly and on every redeploy; `/new` is the one you control. **Uninstalling the Mac app:** quit Sotto Bridge from its menu-bar
icon, drag `/Applications/Sotto Bridge.app` to the Trash, and remove its Full Disk Access entry in
System Settings → Privacy & Security. That's everything — it keeps no other state on the Mac.

**On a managed Mac (ThreatLocker, CrowdStrike, Jamf, …):** endpoint security will get in the way
twice, and the symptoms look nothing alike. If the app is killed at launch, its *execution* is
blocked. If it launches, pairs, and then reports no messages, its *file access* is blocked —
reading `~/Library/Messages` is exactly the shape of the thing those tools exist to stop. Ask
whoever runs the policy to allow both **by signing certificate**, not by file: Sotto Bridge and its
bundled engine are signed `Developer ID Application: Nikunj Deepak Kothari (2YQN7TTF85)`. A
per-binary approval dies at the next auto-update; a certificate rule covers every version.

**Staying current:** Sotto's skills update themselves on every redeploy, and your server tells you
when a newer Sotto is published — one quiet line at the foot of your `/setup` (Integrations) page.
On GitHub hit **Sync fork → Update branch** on your copy; Railway redeploys on the push, and that
redeploy *is* the update. (Deployed with the one-click link instead? Railway opens the update as
a pull request on your repo — merging it does the same thing.) The Bridge
flags updates in its own menu and installs them itself after verifying the signature (pairing +
permissions persist). The full picture — including upgrading the underlying Hermes runtime — is
**RAILWAY.md § Staying updated**.

---

## Appendix · WhatsApp instead of Telegram

WhatsApp is still first-class — the container pairs it for you, serves a clean QR, and holds a nudge
until the link is live. It is simply no longer the default. Take it when you'd rather Sotto reached
you as a **contact** than as a bot, and you don't mind scanning a QR.

**Three variables in step 1.2 instead of `TELEGRAM_BOT_TOKEN`:**

| Variable | Value |
|---|---|
| `WHATSAPP_ENABLED` | `true` — turns the boot-time pairing step and the WhatsApp gateway back on |
| `WHATSAPP_ALLOWED_USERS` | your number, country code, no `+` (e.g. `15551234567`). Hermes **denies everyone** until it matches |
| `WHATSAPP_HOME_CHANNEL` | the same number — where briefs and nudges are delivered |

…plus `SOTTO_CRON_DELIVER=whatsapp`, which moves the briefs, the midday digest, the weekly pulse, the
proactive watcher and your own routines together. (`WHATSAPP_ENABLED=true` alone would pair WhatsApp
for *chat* while the briefs still went to Telegram.)

**Then scan the QR.** Tile ③ of the `/setup` wizard becomes **Link WhatsApp**: click
**Show WhatsApp QR →**, then on your phone: WhatsApp → **Linked Devices → Link a Device** → scan.
**Success signal:** reload `/setup` and tile ③ reads "WhatsApp is linked". *Missed the ~15-minute
window, or the page says "No pairing in progress"? Neither is fatal — redeploy and pairing reopens on
the next boot with a fresh QR.* Scan from the web page rather than the deploy logs; Railway's log
viewer distorts the terminal QR.

Everything else in this guide is identical, except that you message **yourself** on WhatsApp
(self-chat mode — you are the bot) where it says to message your bot. Two WhatsApp-only knobs:
`SOTTO_WHATSAPP_MODE=1` uses a second, dedicated number instead of self-chat, and
`SOTTO_HIDE_AGENT_NAME=1` drops the ***Sotto*** prefix on its replies. Tapbacks (👀 ✅ ❌) are
Telegram-only — Hermes has no bot reactions on WhatsApp.

**Already on WhatsApp from an earlier deploy?** Nothing to do, and nothing to fear on the next
redeploy: *the channel is Telegram unless this volume already holds a paired WhatsApp session and no
`TELEGRAM_BOT_TOKEN` is set.* Your paired session speaks for you; the boot log names the channel it
chose and why.

---

## Already have a Hermes or OpenClaw? (existing host)

You just add the Sotto layer — no redeploy of your host:

- **Existing Hermes:** `BRIDGE_TOKEN=<your-token> ./adapters/hermes/install.sh` — adds the skills,
  persona, `sotto-local` MCP, and schedule without touching your global model. Cloud host → set
  `BRIDGE_TOKEN` in its env; local host → the Bridge runs over stdio (pick **"This Mac"** in the app).
- **OpenClaw:** `./adapters/openclaw/install.sh` — copies the skills, writes the persona + operating
  rules into the agent workspace (`SOUL.md` / `IDENTITY.md` / `AGENTS.md`), and registers the Bridge
  MCP via the OpenClaw CLI. See [adapters/openclaw/README.md](adapters/openclaw/README.md) for what
  was validated live and the short "Still unverified" list.

## Local instead of cloud?

Everything runs on your Mac over stdio — no Railway, no pairing, no hosting bill; briefs only fire
while the Mac is awake. Full walkthrough: **[LOCAL-SETUP.md](LOCAL-SETUP.md)**.
