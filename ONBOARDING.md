# Set up Sotto in Bridge

Bridge guides **Sotto Cloud or self-hosting in your Railway account**. iMessage is the default
conversation; Telegram is an optional self-host choice. The Mac shares sources you approve and
does not host the agent. There is no web chat.

**Qualification:** these instructions describe supported setup paths. A working existing pilot
does not establish a qualified fresh installation. Before putting another customer's retained data
on a release, qualify its exact signed Bridge/runtime pair, restart and upgrade behavior, useful
first delivery, and data restoration. See [runtime capabilities](docs/RUNTIME-CAPABILITIES.md)
for current use limits and [volume operations](adapters/sotto/VOLUME.md) for recovery constraints.

## Sotto Cloud

1. Open Sotto Bridge and choose **Continue with Google**. This signs you up and connects your
   Google context in one authorization: allow **Gmail or Google Calendar**, or both. Identity-only
   or contacts-only consent cannot finish setup. If you decline, retry the same connection; your
   existing account and setup remain saved.
2. **Allow Full Disk Access.** Open System Settings → Privacy & Security → Full Disk Access,
   enable Sotto Bridge, then return. This is required before the source choices appear; there is
   no skip action. The permission alone does not start collection.
3. **Choose what Sotto reads.** Review the source toggles, then choose **Save sources and continue**.
   Only selected sources are shared. Existing source choices survive reconnects.
4. **Open iMessage**, send the prepared hello from your connected owner account, and check that
   Sotto replies in the same conversation. Bridge observes the connection automatically. Finish
   setup once it is connected; the first brief can finish in the background.

Cloud supplies hosting, model access and the Photon iMessage connection. You do not see Railway,
enter infrastructure credentials, supply a Google AI key, or configure a Google OAuth client.
The current Cloud wizard uses iMessage; an optional Cloud Telegram connection is not yet available.

**Cloud availability:** this is still an invited-account flow backed by pre-created, isolated
Railway tenants and prepared Photon lines. Signing in adopts or resumes that tenant; public
sign-up does not yet allocate one automatically. The combined Google step is implemented in
source, not evidence that a new Cloud release or pilot migration has happened.

## Self-host on Railway

Choose **Other setup options → Self-host Sotto** in Bridge.

1. **Connect Railway.** Choose **Continue with Railway**, sign in on Railway's consent screen,
   and select your workspace. Bridge creates a fresh empty Sotto project when you choose
   **Create Sotto project**. You do not need to prepare a project or paste an account API token
   in the normal flow. Existing API-token setup remains under Advanced.
2. **Connect Gemini.** Enter your Google AI API key. Conversation and audio share this key;
   Google bills your usage. Gmail and Calendar OAuth is a separate self-host connection.
3. **Choose your conversation.** iMessage is selected by default. Arrange an active Photon
   project and dedicated iMessage line, then enter the project ID, project secret, Sotto line
   and your owner phone number. These Photon steps are manual for now. For optional Telegram,
   create a dedicated bot with BotFather, enter its token, open the pairing link and press Start
   privately. Bridge observes the pairing automatically; no numeric owner ID is needed.
4. **Review and create.** Review provider charges and the background-work option, then choose
   **Create Sotto on Railway**. Bridge creates the service, persistent volume and domain,
   prepares the fresh volume and starts Sotto. Keep Bridge open during preparation. Resume
   observes saved operations without repeating an uncertain create or a second initializer.
5. **Connect this Mac**, grant **Full Disk Access**, and return to Bridge. This grant is required
   before source choices and cannot be skipped. Review sources, then **Save & Connect**.
6. Open your conversation and send an owner hello. Check the real reply and first brief on your
   device. **Connect Google** opens your server's private setup page when you want Gmail or
   Calendar; that consent is separate from the Google AI model key.

**Release status:** these native flows are implemented in source. New server creation requires a
signed Bridge release containing a qualified runtime image pinned by digest. Railway sign-in also
requires a registered native OAuth client bundled into that release. This change does not claim
that registration, image qualification or signed release has happened. Without the image descriptor,
creation is unavailable; without OAuth configuration, advanced API-token setup remains available.
Existing deployments can still pair through their private setup link. See
[release qualification](tools/RUNTIME-RELEASE.md).

There is no repository checkout, local Python, Railway CLI or local inference install in this user
flow. Railway and provider usage are billed by those providers; background work is opt-in. Secrets
and provisioning progress are saved in this Mac's Keychain. Resume retains the selected account and
protected deployment journal. A different Telegram bot can be paired before provisioning starts.
Existing or former Hermes volumes are never adopted.

If Railway definitively rejects the project request, Bridge says so; a timed-out or lost response
stays unconfirmed and is only observed, never sent again. **Start over** asks for confirmation, then
removes only the Railway sign-in, project request, deployment journal and Telegram pairing saved
in this Mac's Keychain. It is refused while a setup step is running and deletes nothing in Railway
or Telegram: the confirmation names any project Bridge knows about so you can remove it in the
Railway dashboard, where it is billed until deleted.

For agent assistance, follow [agent-guided setup](#agent-guided-setup). Maintainers retain a
[separate source-build helper reference](docs/MAINTAINER-RAILWAY-HELPER.md); it is not a prerequisite
for the native onboarding flow.

The remaining sections describe the existing receiver setup page, also available to installations
created before native provisioning. It manages connections and consent; conversation remains in
Telegram or iMessage.

## 1 · Open your setup link

Railway → your service → **Deployments → View logs** → find the line starting **`[sotto] Setup link`**
and open it. That's your `/setup` wizard plus its private access code — only someone with this link can
see your pairing token. Open it once and your browser is remembered for the rest of the
wizard; lose it and it reprints on every boot (it's also on the volume at `/data/setup_code`).
- *Link says `localhost`?* The service has no public domain yet. The setup helper creates it before
  the first deployment, so inspect that exact service, then use the freshly printed link.
  *Without a setup credential or owner dashboard session, a bare `/setup` returns **403**.*

One page, five numbered tiles (the fifth is optional). Each tile's state label reads **TO DO** until
it's finished and **DONE** after — reload the page (the wizard's own **recheck** link) to see one flip.

## 2 · The wizard, tile by tile

**① Link your Mac** — [Download Sotto Bridge.app](https://github.com/kothari-nikunj/sotto/releases/latest),
drag it to `/Applications`, open it.
- **For self-host, choose the Railway option.** The new native flow does not require a Bridge
  invite code. A completed self-host install stays unlocked if a legacy code expires. This does
  not grant Cloud entitlement or Mac source access. Legacy code recovery remains available.
- **For invited Sotto Cloud accounts, choose Continue with Google instead.** Your verified Google
  invitation unlocks Cloud setup without a Bridge access code. After Google consent, the browser
  returns to Sotto Bridge automatically; if your browser asks, allow it to open the app. If the
  return does not happen, choose **Open Sotto Bridge** on that browser page. No secret code is copied.
- **Cloud follows Google → Full Disk Access → source choices → iMessage.** Google sign-in and
  Gmail-or-Calendar consent are one step. It does not begin reading this Mac. Cloud's current
  invited account must already have a prepared tenant and Photon line.
- **Self-host follows server setup → Full Disk Access → source choices → conversation.**
  Connect an existing server through **Open in Sotto Bridge** or its pairing link. Bridge checks
  its private status route; older receivers require an update. A saved URL alone is not readiness.
  Model/provider editing on an existing runtime is separate from fresh provisioning.
- **Full Disk Access comes before source selection in both hosting modes.** Click **Open Full Disk
  Access** in Bridge, drag **Sotto Bridge.app** from the card into the Full Disk Access list in
  System Settings, then turn it on. You can also use the Add button in System Settings. Return to
  Bridge and wait for **Full Disk Access enabled** before reviewing and saving your source choices.
  Saving those choices starts the selected readers.
- **Conversation.** Open the configured iMessage line or Telegram bot and confirm a reply in the
  intended conversation. Setup reports the first brief's state while it finishes asynchronously;
  a provider receipt does not prove that it appeared on your device.
- A source toggle does not claim its data has already been read. Meeting-note services still
  connect through their own authorization on the host's `/setup` page.
- **Success signal on the cloud side:** reload `/setup` — tile ① now reads **DONE** with
  "Your Mac is linked and reachable."
- **It keeps itself current.** The Bridge checks daily for a new signed release; when there is one
  the menu shows **"Update available"** and one click installs it in place — no re-download, no
  re-granting Full Disk Access, nothing to re-pair.
- **Start at login** is enabled for you when you finish the wizard — flip it off later in
  Settings → This Mac if you prefer. Done — the app dials *out* to your cloud, so there's no tunnel,
  no port, nothing to keep alive. Closing and reopening your laptop needs nothing from you.
- Optional: **"Send my brief when I wake my Mac"** is on by default — open your laptop after 7am and
  the morning brief arrives moments later (the 6:30 cloud schedule covers the closed-laptop case;
  they coordinate, you never get two).

**② Connect Google (self-host only)** — you create your own OAuth client once (this keeps you off Google's verification
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
4. **Credentials → Create credentials → OAuth client ID** → **Web application**. Add the exact
   redirect URI shown in `/setup` under **Authorized redirect URIs**:
   `https://<RAILWAY_PUBLIC_DOMAIN>/google/oauth/callback`. Download the client JSON.
5. Paste the downloaded JSON into the Google box in `/setup`, then click **Continue with Google**.
   Approve Gmail and Calendar access on Google's page; the browser returns automatically to Sotto
   and shows a clean **Google connected** success page. The callback is PKCE/state protected,
   bound to the browser that started authorization, and single-use. There is no localhost page and
   no code to copy and paste.
   *Existing connected Desktop-client tokens keep working. New authorizations from a Desktop
   client require replacing it with the Web client in `/setup`.*

**③ Connect iMessage** — nothing to click: Bridge already configured Photon from your protected
input, and this tile reports the owner hello. It reads that Photon is configured for your (masked)
number until an authenticated direct message from that exact owner phone arrives, then that the
owner message was received (reload to see it flip). Send one hello from the owner phone to your
Sotto line and confirm the reply appears in Messages; the tile's receipt alone does not prove the
reply was displayed. See [Photon iMessage](CHANNELS.md#photon-imessage).
> **On the Telegram profile?** The tile reads **Link Telegram** and reports the owner link your
> setup captured before the runtime started; see
> [SELFHOST.md § Telegram owner bot](adapters/sotto/SELFHOST.md#telegram-owner-bot).

**④ Timezone** — the zone from your setup config (`sotto_runtime.timezone`); the tile reads
"Timezone set to &lt;your zone&gt;". Nothing to click. (The page detects your browser's zone only on an
installation with no configured zone.)

**⑤ Connected services (optional)** — one-click OAuth for extra sources. Click **Connect →** next to
**Granola (meeting notes)**: your browser opens Granola's consent screen, you approve, and you land
back on a page headed **"Connected"** — meeting notes + transcripts now feed your briefs, meeting
prep, and follow-ups.
It works on **any Granola plan** (it's your login, not an API key), the tokens live only on your
`/data` volume, and if anything fails the page tells you exactly which step broke. Skip freely —
everything else works without it. More lanes + adding other services:
[INTEGRATIONS.md](INTEGRATIONS.md).

## 3 · Check your first brief

Once a context source and your message channel are connected, Sotto prepares its first brief
automatically. The setup page shows whether it is preparing, waiting to send, delayed for a retry,
or sent. You do not need to request a second brief. Self-hosted runtimes can use selected Mac
sources or Granola without Google. Cloud onboarding requires Gmail or Calendar; its Mac access
and source review remain separate required Bridge steps. After setup, server-side Google reading
continues while the Mac sleeps.

You can message your Sotto line in iMessage (or your bot, on the Telegram profile) to ask for
help. Your own Gemini key pays for the model calls. You do not need a
Sotto Cloud subscription.

Confirm the first brief actually appears on your device. Photon can send four photos when the
layout and provider support it; Telegram uses text. A send receipt alone
does not prove device display. If an interactive tool requests permission, review the specific
action; blanket approval is not a setup step, and the automatic first brief needs no second chat run.

The guided setup reports connection and delivery states; check each against the actual probe or
device result. The receiver uses recent context; progressive history learning has separate spending
controls described below. Do not start a second welcome run while the first look is pending. An
ordinary brief can be requested later by saying **"good morning"**.

From here it's all conversation:

Capabilities vary by channel; the examples below describe the Photon path. Telegram now supports
accepted-brief readback and explicit fresh brief requests. Other attended mutations remain
limited; see [the capability inventory](docs/BEHAVIOR-INVENTORY.json).

- *"good morning"* / *"good evening"* — the briefs
- *"prep me for my 2pm"* / *"follow up on my meetings"*
- *"find 30 min with Alex next week"* / *"accept my 3pm"* — proposes from your real calendar, then books/RSVPs on your OK
- *"triage my inbox"* / *"what am I waiting on"*
- *"draft a reply to Sarah"* — in your voice; saved as a draft unless you explicitly approve sending
- *"who am I losing touch with"* / *"what do I know about Alex"*
- *"what can you do?"* — the full map, any time

And if a brief ever looks thin or wrong: say **"that's wrong about X"** (fixes its memory), **"stop
surfacing newsletters"** (mutes), or **"clean up stale loops"**. Friday evening briefs also include
a short unfinished-work review when anything stale remains; there is no separate Sunday message.

## Your dashboard

The same `/setup` page is part of a private web dashboard: once your tiles are green, click
**"Open your dashboard"** (or visit `https://<your-domain>/app` — first visit asks for your setup
code once, then remembers you for 30 days). It shows today at a glance, your open loops (resolve or
dismiss in one tap), the archive of every delivered brief, everyone Sotto knows — with per-fact
confidence and sources, editable in place — and what it has learned about your voice and
preferences. Works great on your phone.

## Your first 24 hours

Day one is quiet on purpose — quiet ≠ broken:

- **Your first brief** is prepared automatically once a context source and your delivery channel
  are ready. Regular morning and evening briefs follow the installed schedule.
- **Few or no nudges at first** — the triage funnel is deliberately strict; only a real ask from
  someone you know or a missed call gets through.
- **The 12:30 digest stays silent on light days** by design — no news is the feature.
- **The relationship pulse** first fires Monday at 9am.
- **The dashboard fills as briefs run** — the Briefs tab starts with your first delivered brief, and
  the Learned page (voice + preferences) populates after the first brief runs.

Three rules to know on day one:

- **No bot joins your calls.** Meeting notes come from Granola reading the notes you already take.
  Nothing of Sotto's dials into a meeting, and nothing records one.
- **Replies default to drafts.** Sending to another person requires your explicit approval and
  an enabled send capability. The Mac Bridge refuses sends unless started with `--allow-send`.
  Scheduled briefs and nudges to you use the linked delivery channel.
- **Every silence is auditable.** Nudged, queued, or dropped, each event gets a ledger row with its
  reason, readable in the dashboard's **Record** view (`/app#record`).

The rules behind all three: [docs/HOW-SOTTO-DECIDES.md](docs/HOW-SOTTO-DECIDES.md).

## Background memory needs a separate spending choice

The receiver's first useful look uses recent context. Progressive history learning and Dreamer
need a budgeted model proxy, or a deliberate unmetered opt-in, and the self-host launch configures
neither, so on a helper-created install background learning reports a hold while ordinary chat and
briefs continue. Do not assume six weeks of history were reviewed just because the first brief
arrived. Check `knowledge/history-state.json` and
[RAILWAY.md](RAILWAY.md#background-history-learning-on-self-host).

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

All of it lands on **one** channel — your owner channel, fixed by your setup. There is no per-job
override by design, and on the supervised image these schedules always run; their defaults are in
[RAILWAY.md](RAILWAY.md#environment-variables--full-reference).

## If something's off

The **[RAILWAY.md](RAILWAY.md) troubleshooting table** covers the common ones: setup link says
`localhost`, no reply on iMessage (send from the exact owner phone in your config), briefs
never arrive (`/debug/brief-log`), Google dying after a week (consent
screen left in Testing — step 3②.3). **Replies feel stale, or Sotto starts echoing a delivered brief
back at you:** send **`/new`** in the chat — it starts a fresh session on the latest persona.
Sessions also rotate nightly; `/new` is the one you control. **Uninstalling the Mac app:** quit Sotto Bridge from its menu-bar
icon, drag `/Applications/Sotto Bridge.app` to the Trash, and remove its Full Disk Access entry in
System Settings → Privacy & Security. That's everything — it keeps no other state on the Mac.

**On a managed Mac (ThreatLocker, CrowdStrike, Jamf, …):** endpoint security will get in the way
twice, and the symptoms look nothing alike. If the app is killed at launch, its *execution* is
blocked. If it launches, pairs, and then reports no messages, its *file access* is blocked —
reading `~/Library/Messages` is exactly the shape of the thing those tools exist to stop. Ask
whoever runs the policy to allow both **by signing certificate**, not by file: Sotto Bridge and its
bundled engine are signed `Developer ID Application: Nikunj Deepak Kothari (2YQN7TTF85)`. A
per-binary approval dies at the next auto-update; a certificate rule covers every version.

**Staying current:** your server tells you when a newer Sotto is published — one quiet line at
the foot of your `/setup` (Integrations) page. A helper-created service has no repository source
to redeploy from, and the helper only creates fresh installs, so applying that update is not
automated. The Bridge
flags updates in its own menu and installs them itself after verifying the signature (pairing +
permissions persist). The full picture — including how a rebuild updates the runtime — is
**RAILWAY.md § Staying updated**.

## Agent-guided setup

Sotto has two hosting choices: invited Sotto Cloud or self-hosted Railway. Bridge is the setup
surface; conversation stays in iMessage or Telegram. No local runtime or user-facing CLI is needed
in the native path. Account sign-ins, provider purchases, Google consent and Mac permissions belong
to the user. Never paste credentials into chat, logs, command arguments or version control.

### Copy-paste prompt

> Help me set up Sotto in Bridge. Follow AGENTS.md and ONBOARDING.md#agent-guided-setup. Ask which hosting
> option I want if I have not chosen. Guide the account connections and verify the actual reply
> and brief on my device before claiming success.

### Self-hosted Railway

Use [the native self-host steps](#self-host-on-railway). The native wizard follows Railway authorization and
workspace/project creation → Gemini key → iMessage via Photon (default) or optional Telegram →
review and create server → Full Disk Access → source choices → conversation.

New server creation is available only when the signed Bridge contains a qualified digest-pinned
runtime descriptor. Normal Railway sign-in additionally requires a registered native OAuth client
bundled into that release. Neither registration nor a qualified signed release is claimed by source
implementation. Without the descriptor, creation is disabled; existing servers can still pair.
Without OAuth configuration, API-token setup is available under Advanced. Do not turn a missing
release artifact into instructions to install Python, Git or Railway CLI.

Guide **Continue with Railway**, workspace selection and **Create Sotto project**. Bridge creates
an empty project; the paid service and volume are created only after the final review. The user
enters one Google AI key privately in Bridge. Photon setup is manual for now: arrange an active
project and dedicated iMessage line, then enter the project ID, secret, Sotto line and owner phone.
For Telegram, use a dedicated BotFather token and the owner's private Start message. Bridge observes
pairing automatically and stops polling before the runtime takes over. Never ask for a numeric
Telegram owner ID.

Credentials and operation intent are persisted in Keychain before provider mutations. Resume
checks the same target; never delete protected state to force a retry, adopt an old volume, or
create a second initializer. Keep Bridge open while preparation is observed. An uncertain mutation
stays pending for investigation rather than being sent again.

After the server is reachable, choose **Connect this Mac**, then require Full Disk Access before
showing or saving source choices. The user enables Sotto Bridge in System Settings and returns to
the app; there is no skip route in either hosting mode. The grant alone does not start collection:
the user then reviews source toggles and saves the choices. **Connect Google** opens the receiver's
private setup surface for self-host Google OAuth when wanted; that connection is distinct from the
Google AI model key.

Do not send a test message, perform a paid model probe, start another chat process, or touch a
pilot as an installation probe. Let the user send one owner hello and confirm the reply. Observe
the same pending welcome attempt; do not request duplicates to test delivery. A fresh requested
brief is separately supported on Telegram and Photon and is not scheduled-delivery evidence.
Telegram remains a limited profile: attended meeting prep, offered replies and owner skill requests
are still Photon-only. See [BEHAVIOR-INVENTORY.json](docs/BEHAVIOR-INVENTORY.json) for tested boundaries.

### Invited Sotto Cloud

Choose this only for an invited account with its tenant and Photon line already prepared. Bridge
starts at **Continue with Google**: account sign-up and Gmail-or-Calendar consent happen in the same
authorization. Require at least one readable source and durable Google connection; contacts-only
or identity-only sign-in cannot complete setup. If consent is declined, offer reconnect without
erasing the existing account, tenant or saved setup. An older runtime without explicit Google
readiness must be updated; Mac-only context or a previous brief cannot substitute for this grant.

Next, require Full Disk Access, then show source choices. Reading begins after the user saves those
choices. Open the prepared hello in iMessage and let the user send it from the connected owner
account. The app observes the channel connection; the first brief can finish asynchronously.
iMessage is the current Cloud wizard's conversation. Optional Cloud Telegram pairing is not yet
available; do not send Cloud users through the self-host bot setup as a substitute.

Railway is Cloud's backend, never a customer setup step. Cloud users need no Railway access,
project, Gemini key, Photon credentials or Google OAuth client JSON. Sotto supplies those managed
services. The existing broker adopts/resumes pre-created tenants; automatic public signup
allocation is not wired up. Do not claim that signing in creates a new Railway tenant.
Google consent and macOS source permissions remain user actions. The hosted service and its
providers process data as described in [`docs/DATA-FLOW.md`](docs/DATA-FLOW.md).

If you are operating the hosted service rather than joining as an invited user, this public guide
does not cover operator provisioning.

### What counts as verified

Keep these claims separate in the final report: exact deployment success and `/health`; Google
OAuth success page; Bridge connected with selected sources and grants; channel owner pairing/message received;
first brief visibly delivered; and recovery of the same pending/failed attempt without duplicates.
Do not infer first-welcome or scheduled-delivery success from an ordinary manual brief, or infer
device appearance from a provider receipt alone. This guide describes the intended released path;
they do not claim every end-to-end configuration has been live-tested.

### Optional: connect other assistants

At the end of either Bridge setup path, **Use Sotto with other assistants?** opens
this installation's connection page. It is also available in Account settings and
on the receiver's setup page. The owner can skip it without affecting Sotto.

For assistants supporting remote MCP with OAuth, copy the displayed server URL into
the assistant's custom connector settings, sign in to Sotto, and approve read-only
access. Do not ask for a shared API key, put a pairing code in the connector URL, or
run operator enrollment scripts. Each owner uses their own deployment and source
permissions. The page explains expiration and provides Disconnect. Availability of
custom connectors depends on the client; do not claim a successful connection until
the actual client has read a permitted source. A skill pack is not required.

## Source-created server reference

These details apply to source-built self-host installations. Use the [maintainer Railway helper reference](docs/MAINTAINER-RAILWAY-HELPER.md#self-hosted-on-railway-supervised-sotto-photon) for the protected source-build config and resume procedure. This reference is not a prerequisite for native Bridge setup or invited Sotto Cloud; use their separate paths above.

### Source-created server: private setup surface

Railway → your service → **Deployments → View logs** → find the line starting **`[sotto] Setup
link`** and open it privately. That is your `/setup` wizard plus its access code: the setup pages
(`/setup`, `/pair`, `/google/*`, `/debug/google`) are gated behind it. Open it once and a cookie
covers the rest. Lost it? It reprints on every boot, and it is on the volume at `/data/setup_code`.
A signed-in self-host owner can also open `/setup` from the dashboard with the existing
session. That path does not reveal the bootstrap pairing link; use the original setup link
to pair a new Mac. Without either credential, a bare `/setup` returns **403**.

> Don't set `PORT` yourself — Railway injects it, and the self-host bootstrap requires `8080`.

### Self-host Google recovery and diagnostics

Use the browser flow from `/setup`:
1. **Create a Web application OAuth client** (one-time): [console.cloud.google.com](https://console.cloud.google.com) →
   new project → enable **Gmail API** + **Google Calendar API** → **OAuth consent screen** in the left
   nav (newer consoles call this page **Google Auth Platform**) → audience **External** → **Publish
   app** so the status reads **In production** → Credentials → **OAuth client ID → Web application**.
   > ⚠️ **The day-8 trap:** a consent screen left in **Testing** issues refresh tokens that **expire
   > after ~7 days** — Google silently disconnects and every brief loses Gmail/Calendar on day 8.
   > Set the app to **In production** (Google does *not* require verification for you using your own
   > data — ignore the scary "needs verification" banner).

   Under **Authorized redirect URIs**, enter the exact callback displayed in `/setup`:
   `https://<RAILWAY_PUBLIC_DOMAIN>/google/oauth/callback`. Download the Web client JSON.
   *(Workspace accounts: if your org blocks unverified apps, allowlist the client in Admin console, or use a personal account.)*
2. Paste the JSON into the `/setup` Google box and click **Continue with Google**. Approve access
   on Google's page; the browser returns automatically to Sotto and shows a clean success page.
   Authorization uses browser-bound, single-use PKCE and state. No code is copied from a
   `localhost` page. The token persists in `/data/sotto-home` and auto-refreshes. If sign-in fails
   because the saved client's secret was rotated or deleted, it belongs to another project, or the
   callback is not registered, choose **Use a different Google client** in `/setup` or on the retry
   page; an existing token keeps working until the new client connects.
3. **Verify any time:** open **`/debug/google`** (setup code/cookie or owner dashboard session required) — it returns `{"google_connected": true}` when the
   token works. Google is **server-side and Bridge-independent**: if this says connected, every
   scheduled brief gets fresh Gmail + Calendar even with the Mac asleep. (Local data is the only
   thing that's cached as an offline backup; Google is always fetched live.)
   This self-host flow leaves the Mac Bridge unchanged and uses no Sotto OAuth broker.

### Connected-service protocol and diagnostics

Granola connects from the **`/setup` wizard → Connected services** tile: click **Connect**, approve
on Granola's consent screen in your browser, done. Under the hood the receiver registers *itself*
with Granola's remote MCP (OAuth 2.1 Dynamic Client Registration + PKCE — no pre-registered client,
no secret) and stores the access+refresh tokens on `/data` (`connectors/granola.json`); briefs,
meeting prep, and follow-ups then gather meeting notes + **transcripts** headlessly. Works on **any
Granola plan** — it's your login, not a Business-only API key. No variables, no redeploy. If the
flow fails, the error page names the exact step (discovery / registration / state / exchange) + the
provider's error. Full doctrine: [INTEGRATIONS.md](INTEGRATIONS.md).

Granola is optional — fine to skip and ship Gmail + Calendar + the Bridge first.

**Key-based providers** (Exa, Parallel, Browser Use Cloud, X) are read from the receiver's
environment, and the Connections page shows them read-only. The supervised launch builds that
environment from the launch envelope alone (see the [environment reference](RAILWAY.md#environment-variables--full-reference)),
so a helper-created install runs without them: research uses Gemini grounding, and X attendee
context reports `unconfigured` in `/api/overview`.

### Source-created server: Photon receipt

The helper already configured Photon, the exact owner identity and the physical line from your
protected config. Send one hello **from the owner phone** to the Sotto line, then confirm Sotto's
reply appears in Messages. The `/setup` iMessage tile shows missing provider setup, waiting for the
owner's first direct message, or that the owner message was received; it marks connected only after
an authenticated owner direct message, and that receipt alone does not prove the reply appeared.
The receipt is bound to the Photon project. Delivery does not require an always-awake Mac.

### Source-created server: first brief

Once a context source (Google, Granola or the Bridge) and the Photon owner hello are in place,
Sotto prepares its first brief automatically. `/setup` shows whether it is preparing, waiting to
send, delayed for a retry, or sent. Confirm it appears on your device; do not start a second
welcome run while the first one is pending. See [first-brief checks](#3--check-your-first-brief).

### Source-created server: Bridge diagnostics
Gmail + Calendar come from Google; your **local** signals come from the Sotto Bridge on your Mac. It
connects **tunnel-free**: the Mac dials *out* to this Railway host over the reverse link
(`/bridge/poll`, `/bridge/respond`), so there's nothing to expose — no Cloudflare, no inbound port
on the Mac. How the reverse link works end to end: **[runtime architecture](docs/ARCHITECTURE.md)** (the relay module).

1. **The token is already set.** The helper generated the Bridge bearer inside the launch envelope;
   you never see or type it. The `/setup` pairing link carries it. *(The wake-push uses the same
   bearer.)*
2. **On your Mac:** install the signed **Sotto Bridge** menu bar app — [download it from GitHub
   Releases](https://github.com/kothari-nikunj/sotto/releases/latest); it ships as a signed DMG
   only (the Bridge source isn't in this repo) — then **pair it in one click**:
   - Choose **Railway** for self-host setup. No Bridge invitation code is required. The
     server's private setup link still authorizes pairing ([native setup](#self-host-on-railway)).
   - Open the **setup link from the deploy logs** (see [private setup surface](#source-created-server-private-setup-surface)) on that Mac
     → click **“Open in Sotto Bridge”**. It fills the host + token (no typing, and the host always
     carries `https://`).
   - *Fallback:* copy the **pairing code** in the wizard and paste it into the app's **“Paste pairing
     link”** field.
   - Grant **Full Disk Access** when prompted; flip **Start at login** on.
   The app supervises `sotto-bridged --connect …` (dials out) — the menu bar dot turns solid when
   connected, and reloading `/setup` shows tile ① as **DONE** ("Your Mac is linked and reachable").
   - **Diagnose a source before connecting:** run `sotto-bridged --doctor` — a read-only, per-source
     readout (each local source prints `ok (N rows readable)` / `needs Full Disk Access` /
     `unavailable`, plus the exact "grant FDA to the *right* app" fix). It writes nothing and touches
     no network; exit `0` means every enabled source reads. **`ok (0 rows readable)` means nothing
     matched in the last 6 hours** — the probe window, not a lifetime count — so a source you haven't
     used since lunch is still `ok`. See the troubleshooting table below.

> The auto wake-push (brief fires the instant your Mac wakes) is optional — the **6:30/17:30
> schedule** fires the brief regardless, and you can ask for one anytime. If the Bridge is offline
> at brief time, the brief degrades to the last cached snapshot. And if a triggered brief dies
> mid-run, its claim goes stale after **30 minutes** and the next trigger retries it — no silently
> lost briefs.

