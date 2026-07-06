# Set up Sotto — step by step (cloud)

The friendliest path: an always-on Sotto in the cloud + the read-only Mac Bridge. About **20 minutes**,
most of it waiting on builds. You'll finish with a morning brief in WhatsApp and a Mac link that
survives sleep, redeploys, and laptop lids.

> **The model in one line:** Sotto = skills + persona running on a cloud **agent** (Hermes on Railway),
> fed your local Mac signals by the **Bridge** menu-bar app. The cloud writes the briefs; your Mac only
> reads data and nudges timing. The Bridge never sends anything — replies are drafts you tap to send.

## 0 · What you'll need

| You need | Why | Where it goes |
|---|---|---|
| A **Railway** account (**paid/verified plan** — volumes + always-on need it) | hosts the agent + storage | — |
| A **Gemini API key** — [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | the LLM (only key Sotto needs) | once, into Railway |
| Your **WhatsApp number** | where briefs are delivered | once, into Railway |
| A **Google OAuth client** (5-minute console task, step 3②) | Gmail + Calendar | pasted in the wizard |
| The signed **Sotto Bridge.app** — [download from Releases](https://github.com/kothari-nikunj/sotto/releases/latest) | reads your Mac | drag to /Applications |

Everything else — linking your Mac, connecting Google, the WhatsApp QR, your timezone — happens on
**one wizard page** (`/setup`), no redeploys.

## 1 · Deploy the backend on Railway

> A one-click template is coming; until it's live, the manual deploy below is the path — it's four
> settings. Click-by-click screenshots: **[RAILWAY.md](RAILWAY.md)**.

In [Railway](https://railway.app): **New Project → Deploy from GitHub repo** → pick this repo, then set
**all four** of these (each one is required; the deploy half-works without them and fails silently):

1. **Settings → Root Directory** = `sotto-hermes` (leave *Dockerfile Path* blank — it's auto-detected).
2. **Variables** — add four:
   - `GOOGLE_AI_API_KEY` = your Gemini key
   - `WHATSAPP_ALLOWED_USERS` = your number, country code, no `+` (e.g. `15551234567`)
   - `WHATSAPP_HOME_CHANNEL` = the same number
   - `BRIDGE_TOKEN` = a long random secret — run `openssl rand -hex 24` and paste the output.
     *Without this, the Mac pairing link carries an empty token and pairing silently fails.*
3. **Add a Volume** mounted at `/data` (your knowledge graph + WhatsApp session live here).
4. **Settings → Networking → Generate Domain** — do this **before** opening the setup link in step 2;
   without a domain the logged link falls back to a dead `localhost` URL.

Deploy and wait for the build (the container installs Hermes + Sotto automatically). The boot log
prints `[sotto] Gemini key OK` if your key works — if you see a `WARNING` there instead, fix the key
before going further.

## 2 · Open your setup link

Railway → your service → **Deployments → View logs** → find the line starting **`[sotto] Setup link`**
and open it. That's your `/setup` wizard plus its private access code — only someone with this link can
see your pairing token or WhatsApp QR. Open it once and your browser is remembered; lose it and it
reprints on every boot.

One page, four tiles, each flipping to ✓ live as you finish it.

## 3 · The wizard, tile by tile

**① Link your Mac** — [Download Sotto Bridge.app](https://github.com/kothari-nikunj/sotto/releases/latest),
drag it to `/Applications`, open it.
- **Before connecting**, the app shows the data sources it can read (Messages, calls, contacts,
  browser history, …) — **turn off anything you don't want shared** right there.
- On the wizard, click **"Open in Sotto Bridge"** — the app fills the host + token in one click
  (different Mac? paste the pairing link into the app instead).
- Click **Grant Full Disk Access** — the app deep-links you to the right Settings pane; flip the
  switch, and the app restarts its engine automatically to pick up the grant.
- Toggle **Start at login** on. Done — the app dials *out* to your cloud, so there's no tunnel, no
  port, nothing to keep alive. Closing and reopening your laptop needs nothing from you.
- Optional: **"Send my brief when I wake my Mac"** is on by default — open your laptop after 7am and
  the morning brief arrives moments later (the 6:30 cloud schedule covers the closed-laptop case;
  they coordinate, you never get two).

**② Connect Google** — you create your own OAuth client once (this keeps you off Google's verification
wall — it's your own data):
1. [console.cloud.google.com](https://console.cloud.google.com) → **New Project** "Sotto" → select it.
2. **APIs & Services → Enable APIs** → enable **Gmail API** and **Google Calendar API**.
3. **OAuth consent screen** → **External** → app name "Sotto", your email → and then **publish the app
   to "In production."** *This matters: left in "Testing," Google expires your access after ~7 days and
   your briefs quietly lose email + calendar on day 8. "In production" needs no Google review for
   personal use.*
4. **Credentials → Create credentials → OAuth client ID** → **Desktop app** → Create → **Download JSON**.
5. Paste the JSON into the wizard's Google box → **Save client** → **Authorize** → on the "unverified
   app" screen click **Advanced → Continue → Allow** → you land on a `localhost:1/?code=…` page that
   won't load — that's expected; copy the `code` value from the URL, paste it into the wizard,
   **Connect**. You'll see "✅ Google connected."

**③ Link WhatsApp** — click **Show WhatsApp QR** → on your phone: WhatsApp → **Linked Devices → Link a
Device** → scan. (Missed the ~15-minute window? The container recycles and pairing reopens on the next
boot — "No pairing in progress" is transient, just redeploy.)

**④ Timezone** — auto-detected from your browser when the page loads; shows ✓ with your zone.

## 4 · Say hello

Message yourself on WhatsApp (self-chat mode — you are the bot): **"set up Sotto."**

The guided setup verifies every connection and reports honestly:

> Here's where you stand:
> - **Bridge** (Mac: messages, calls, contacts) — ✓ connected
> - **Google** (Gmail + Calendar) — ✓ connected
> - **Granola** (meeting notes) — – optional, skipped
> Briefs are scheduled for 6:30am and 5:30pm.

It then seeds your memory and **writing voice** from ~6 weeks of history (a few minutes — it narrates
as it goes) and offers your first brief on the spot. Say **"good morning"** and you're running.

From here it's all conversation:
- *"good morning"* / *"good evening"* — the briefs
- *"prep me for my 2pm"* / *"follow up on my meetings"*
- *"triage my inbox"* / *"what am I waiting on"*
- *"draft a reply to Sarah"* — in your voice; **you** always send
- *"who am I losing touch with"* / *"what do I know about Alex"*
- *"what can you do?"* — the full map, any time

And if a brief ever looks thin or wrong: say **"that's wrong about X"** (fixes its memory), **"stop
surfacing newsletters"** (mutes), or **"clean up stale loops"** (retunes).

## If something's off

The **[RAILWAY.md](RAILWAY.md) troubleshooting table** covers the common ones: setup link says
`localhost` (generate a domain), no reply in self-chat (allowlist number must match exactly), briefs
never arrive (check the boot key-check line + `/debug/brief-log`), Google dying after a week (consent
screen left in Testing — step 3②.3). Uninstalling the Mac app:
**[docs/UNINSTALL.md](docs/UNINSTALL.md)**.

---

## Already have a Hermes or OpenClaw? (existing host)

You just add the Sotto layer — no redeploy of your host:

- **Existing Hermes:** `BRIDGE_TOKEN=<your-token> ./adapters/hermes/install.sh` — adds the skills,
  persona, `sotto-local` MCP, and schedule without touching your global model. Cloud host → set
  `BRIDGE_TOKEN` in its env; local host → the Bridge runs over stdio (pick **"This Mac"** in the app).
- **OpenClaw:** `./adapters/openclaw/install.sh` — copies the skills, writes the persona + operating
  rules into the agent workspace (`SOUL.md` / `IDENTITY.md` / `AGENTS.md`), and registers the Bridge
  MCP via the OpenClaw CLI. See [adapters/openclaw/README.md](adapters/openclaw/README.md) for the
  validation checklist.

## Local instead of cloud?

Everything runs on your Mac over stdio — no Railway, no pairing, no hosting bill; briefs only fire
while the Mac is awake. Full walkthrough: **[LOCAL-SETUP.md](LOCAL-SETUP.md)**.
