# Set up Sotto — step by step (cloud)

The friendliest path: an always-on Sotto in the cloud + the read-only Mac Bridge. Budget **~35 minutes
the first time** — only ~15 of it active (four Railway settings, four variables, then one wizard page);
the rest is waiting on builds. You'll finish with a morning brief in WhatsApp and a Mac link that
survives sleep, redeploys, and laptop lids. (WhatsApp is the **default**, not a requirement — see the
choice in §0 below.)

> **One honest note before you start:** the one-click Deploy button below is a **placeholder** — the
> Railway template for this repo isn't published yet, so the button 404s. **Step 1 below is the
> manual path, and it is the working path today.** It is four settings and four variables; nothing
> about the rest of this guide changes.

> **The model in one line:** Sotto = skills + persona running on a cloud **agent** (Hermes on Railway),
> fed your local Mac signals by the **Bridge** menu-bar app. The cloud writes the briefs; your Mac only
> reads data and nudges timing. The Bridge never sends anything — replies are drafts you tap to send.

## 0 · What you'll need

| You need | Why | Where it goes |
|---|---|---|
| A **Railway** account (**paid/verified plan** — volumes + always-on need it) | hosts the agent + storage | — |
| A **Gemini API key** — [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | the LLM (only key Sotto needs) | once, into Railway |
| Your **WhatsApp number** *(default channel — or a Telegram bot token instead)* | where briefs are delivered | once, into Railway |
| A **Google OAuth client** (5-minute console task, step 3②) | Gmail + Calendar | pasted in the wizard |
| The signed **Sotto Bridge.app** — [download from Releases](https://github.com/kothari-nikunj/sotto/releases/latest) | reads your Mac | drag to /Applications |

Everything else — linking your Mac, connecting Google, the WhatsApp QR, your timezone, and optional
extras like Granola — happens on **one wizard page** (`/setup`), no redeploys.

**Two defaults you can change, if you want to — decide now, it's a variable each.** Full detail (and
how tested each one is) in **[CHANNELS.md — Choosing your channel and model](CHANNELS.md)**:

| Your channel | | Your model |
|---|---|---|
| **WhatsApp** — default. No bot account, no token; scan one QR. The path this guide follows. | | **Gemini** — default, and what the brief pipeline calls today. One key covers everything. |
| **Telegram** — a bot token from @BotFather, no phone pairing. Right call when WhatsApp pairing is painful. ~5 min and **four variables** (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_HOME_CHANNEL`, `SOTTO_CRON_DELIVER=telegram`) — one command inside your container works out all four for you. Less tested than WhatsApp. | | **Anthropic / OpenAI / Kimi / DeepSeek** — for the **chat** layer only (Ask Sotto, nudge replies). The briefs still need a Gemini key. |
| **iMessage (BlueBubbles)** — blue bubbles, but an always-on Mac + Firebase + a tunnel. Hand-wired recipe, hours not minutes. | | **Exa / Parallel** — web research only, and independent of the rest: set the key and research stops going through Gemini. |

## 1 · Deploy the backend on Railway

**The working path today is manual — four settings, in this order.** Get this repo where Railway can
see it: **Fork** it on GitHub (recommended — that's what **Sync fork** later updates), or point
Railway straight at the public repo. Then in [Railway](https://railway.app): **New Project → Deploy
from GitHub repo** → pick it. Set **all four** before the first deploy finishes; each one fails
*quietly* if you skip it:

1. **Settings → Root Directory**: leave blank — the Dockerfile is at the repo root (leave *Dockerfile Path* blank too; both are auto-detected).
   *Get this wrong and the build dies with `COPY … not found` — the Dockerfile's `COPY` paths are
   relative to the build context this setting picks.*
2. **Variables → New Variable** — add four:
   - `GOOGLE_AI_API_KEY` = your Gemini key
   - `WHATSAPP_ALLOWED_USERS` = your number, country code, no `+` (e.g. `15551234567`)
   - `WHATSAPP_HOME_CHANNEL` = the same number
   - `BRIDGE_TOKEN` = a long random secret — run `openssl rand -hex 24` and paste the output.
     *Without this, the Mac pairing link carries an empty token and pairing silently fails.*
   *(Delivering to Telegram instead? Set `GOOGLE_AI_API_KEY` + `BRIDGE_TOKEN` now and
   `WHATSAPP_ENABLED=false` in place of the two `WHATSAPP_*` lines, finish this step, then do
   **[CHANNELS.md § Telegram setup](CHANNELS.md#telegram-setup)** — its one command runs **inside**
   your deployed container, so it has to come after the first deploy, and it prints the four
   `TELEGRAM_*` / `SOTTO_CRON_DELIVER` lines for you to paste back here.)*
3. **⌘K → Add Volume**, mount path `/data` (your knowledge graph + WhatsApp session live here).
   *No volume = every redeploy wipes your WhatsApp login, Google token and memory.*
4. **Settings → Networking → Generate Domain** — do this **before** opening the setup link in step 2,
   then redeploy once. Without a domain the logged link falls back to a dead `localhost` URL.

Deploy and wait for the build (the container installs Hermes + Sotto automatically). **Success signal
in the deploy logs:** a line reading `[sotto] Gemini key OK (model … available)`. A
`[sotto] WARNING: Gemini key/model check failed` there instead means the key is wrong or out of
quota — fix it before going further, because every brief depends on it.

**Later: the one-click button.** Once a Railway template is published for this repo, the button below
does all four of the above and prompts only for your Gemini key and WhatsApp number. **It is a
placeholder right now and will 404** — the manual path above is the whole story until then.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/lvprWx)

## 2 · Open your setup link

Railway → your service → **Deployments → View logs** → find the line starting **`[sotto] Setup link`**
and open it. That's your `/setup` wizard plus its private access code — only someone with this link can
see your pairing token or WhatsApp QR. Open it once and your browser is remembered for the rest of the
wizard; lose it and it reprints on every boot (it's also on the volume at `/data/setup_code`).
- *Link says `localhost`?* You have no public domain yet — go back and do step 1.4, redeploy, and use
  the freshly printed link. *A bare `https://<your-domain>/setup` with no `?code=` returns **403** by
  design.*

One page, five numbered tiles (the fifth is optional). Each tile's state label reads **TO DO** until
it's finished and **DONE** after — reload the page (the wizard's own **recheck** link) to see one flip.

## 3 · The wizard, tile by tile

**① Link your Mac** — [Download Sotto Bridge.app](https://github.com/kothari-nikunj/sotto/releases/latest),
drag it to `/Applications`, open it.
- **First run asks for an access code** — Sotto Bridge is invite-only for now; paste the code from
  your invite (it's verified on your Mac, nothing is sent anywhere), and if you don't have one yet,
  ask in [Issues](https://github.com/kothari-nikunj/sotto/issues).
- **Updating an existing Bridge to 1.2.7 or newer asks for a code too**, once, and that install stops
  streaming to your cloud until you enter one — your pairing, Full Disk Access and settings are all
  untouched, so entering the code resumes everything with nothing to re-pair. Same
  [Issues](https://github.com/kothari-nikunj/sotto/issues) link if you don't have a code yet.
- **Then a 3-step wizard opens, in this order — Disk access → Connect → Privacy.** Nothing is read
  and nothing is sent until you press **Save & Connect** on the last step, so it is safe to walk
  through it before deciding anything.
  1. **Disk access.** Click **"Open System Settings…"** — it deep-links you to the right pane; add
     **Sotto Bridge** under Privacy & Security → Full Disk Access. **Success signal:** come back and
     the card reads **Full Disk Access · Granted** — it updates by itself, no relaunch.
     *Skipped it? You can grant it later from the menu-bar item's **Grant** button; the app restarts
     its engine on its own to pick the grant up.*
  2. **Connect.** On the `/setup` page **on this Mac**, click **"Open in Sotto Bridge →"** — the app
     fills host + token in one click. **Success signal:** *Continue* stops being greyed out.
     *On a different Mac from the browser? Copy the pairing link from that same tile and paste it
     into the app's **`sotto-bridge://pair?…`** field, then click **Pair**.*
  3. **Privacy.** Every data source the app can read (Messages, calls, contacts, browser history, …)
     is listed with a per-source toggle, all on — **turn off anything you don't want shared, here,
     before the first connect.** Then **Save & Connect**.
     *At the bottom of that list — once the app knows your host — a **"Meeting notes (Granola) &
     more"** row with an **Open…** button jumps to your `/setup` page, where cloud sources connect;
     they are never read from this Mac. If that page says Forbidden, open the `[sotto] Setup link`
     from your deploy logs once in the same browser first.*
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

**③ Link WhatsApp** — click **Show WhatsApp QR →** → on your phone: WhatsApp → **Linked Devices → Link a
Device** → scan. **Success signal:** reload `/setup` and tile ③ reads "WhatsApp is linked".
*Missed the ~15-minute window, or the page says "No pairing in progress"? Neither is fatal — redeploy
and pairing reopens on the next boot with a fresh QR.*
> **On Telegram instead?** Don't scan anything — do
> **[CHANNELS.md § Telegram setup](CHANNELS.md#telegram-setup)** now (five steps, ~5 minutes: make a
> bot with @BotFather, run one command in your container, paste the four variables it prints into
> Railway, redeploy). Come back here afterwards: with `SOTTO_CRON_DELIVER=telegram` set, this tile
> stays **TO DO** forever and that is correct — it does **not** block the wizard from completing, and
> your dashboard hero link appears once tiles ①②④ are done. **On iMessage/BlueBubbles?** Same: skip
> this tile, [CHANNELS.md § iMessage via BlueBubbles](CHANNELS.md#imessage-via-bluebubbles) is a
> hand-wired recipe measured in hours, not minutes.

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

Message yourself on WhatsApp (self-chat mode — you are the bot): **"set up Sotto."** *(On Telegram,
message your bot instead — same words.)*

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

All of it lands on **one** channel — `SOTTO_CRON_DELIVER`, which defaults to `whatsapp` (your
`WHATSAPP_HOME_CHANNEL`). Point it at `telegram` and every line above moves with it; there is no
per-job override by design. Tune or disable any of them with the `SOTTO_PROACTIVE*` variables in
[RAILWAY.md](RAILWAY.md).

## If something's off

The **[RAILWAY.md](RAILWAY.md) troubleshooting table** covers the common ones: setup link says
`localhost` (generate a domain), no reply in self-chat (allowlist number must match exactly), briefs
never arrive (check the boot key-check line + `/debug/brief-log`), Google dying after a week (consent
screen left in Testing — step 3②.3). **Uninstalling the Mac app:** quit Sotto Bridge from its menu-bar
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
redeploy *is* the update. (Deployed with the one-click template instead? Railway opens the update as
a pull request on your repo and merging it does the same thing — but that path only exists once the
template is published, which it isn't yet.) The Bridge
flags updates in its own menu and installs them itself after verifying the signature (pairing +
permissions persist). The full picture — including upgrading the underlying Hermes runtime — is
**RAILWAY.md § Staying updated**.

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
