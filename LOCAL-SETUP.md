# Local setup — Hermes on your Mac (no cloud, no tunnel)

Run the whole thing on your Mac. The Bridge talks to Hermes over **stdio** (a child process), so there's
**no tunnel, no Railway, no HTTP bearer** — Hermes just spawns the Bridge binary and reads local data
directly. Pick this if you want **no cloud host in the path** — no Railway, no container, no volume
off your machine. Trade-off: briefs only run while your Mac is awake. The cloud path is
[ONBOARDING.md](ONBOARDING.md) (friendly) / [RAILWAY.md](RAILWAY.md) (reference).

> **This is not "your messages never leave the Mac."** It removes the *host*, not the *model*. A
> brief is one call to your LLM provider, and the prompt for that call contains the material the
> brief is about — message text, email bodies, notes, calendar events. That leaves your Mac and is
> processed on your provider's servers under their terms. The only way to keep everything local is a
> local model, which Sotto does not ship today (`docs/DATA-FLOW.md` § What fully local would take).

Same backend either way — identical skills, scripts, knowledge graph, continuity, style. Only the
transport differs (stdio here vs the tunnel-free reverse dial-out relay in the cloud).

## Prereqs
- A Mac, Python 3.
- A **1M-context model key** — a Google **Gemini API key** ([aistudio.google.com/apikey](https://aistudio.google.com/apikey);
  Gemini 3.6 Flash is cheap, 1M, and a fine driver). Sotto stores no keys — this goes in Hermes' native
  `~/.hermes/.env`. Any 1M model works; Gemini is the recommendation.

## Steps

All commands run from the repo root.

**1. Get the Bridge + grant Full Disk Access** (the only manual permission).

Get a `sotto-bridged` engine onto this Mac. Step 4's installer
finds it on its own and registers it — you never type a path.

*Install the app (no toolchain needed).* Download **"Sotto Bridge.app"**
from the Releases page and drag it to **/Applications**. The signed app bundles the engine inside
itself at:
```
/Applications/Sotto Bridge.app/Contents/Resources/sotto-bridged
```
That is the path `install.sh` probes second, so nothing else is needed.


*(Engine kept somewhere else? Export `SOTTO_BRIDGE_BIN=/path/to/sotto-bridged` before step 4 — an
explicit override beats both probes. If none of the three exists, the installer refuses to say
"Done": it names every path it looked in and exits non-zero.)*

Grant **Full Disk Access to your terminal app** (Terminal/iTerm — System Settings ▸ Privacy & Security ▸
**Full Disk Access**), then fully quit and reopen it. Why the terminal: in stdio mode macOS attributes
the `chat.db` read to the process that *spawns* the Bridge — and both the verify command below and
Hermes-launched runs are children of your terminal, so the terminal is the TCC principal. Adding
`/Applications/Sotto Bridge.app/Contents/Resources/sotto-bridged` itself is belt-and-braces only. Verify:
```bash
# the installed app:
"/Applications/Sotto Bridge.app/Contents/Resources/sotto-bridged" --doctor
```
One line per source: `ok (N rows readable)` means that source works; `needs Full Disk Access` means
the grant above didn't take (the exact fix is printed at the bottom); `unavailable` just means that
app isn't on this Mac. The command exits 0 only when every enabled source is `ok` (or `disabled`), so
an `unavailable` source makes it exit non-zero too — read the per-source lines before assuming trouble:
a non-zero exit can simply mean an app you don't use isn't installed, **not** that Full Disk Access is
broken. Only a `needs Full Disk Access` line points at the grant.

**2. Install Hermes** (skip if you already run it):
```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

**3. Secrets + exhaust dir** — do this BEFORE the installer (step 4 reads the key from here):
```bash
mkdir -p "$HOME/SottoData" ~/.hermes
# single-quote the key so a '$' in it isn't shell-expanded; keep "$HOME" double-quoted so it expands
printf 'GOOGLE_AI_API_KEY=%s\nSOTTO_DATA=%s\nSOTTO_TIMEZONE=%s\n' \
  '<your-gemini-key>' "$HOME/SottoData" 'America/Los_Angeles' >> ~/.hermes/.env
```
`SOTTO_TIMEZONE` is your IANA zone (`America/New_York`, `Europe/London`, …). The cloud auto-detects it
from the setup wizard; **local does not** — leave it unset and the 6:30/17:30 briefs compute "today" in UTC.

**4. Run the Sotto installer:**
```bash
./adapters/hermes/install.sh --dedicated
```
`--dedicated` sets the chat model to the native `gemini-3.6-flash` — the right default for a
Sotto-dedicated local Hermes. The installer also copies the repo's local skills to
`~/.hermes/skills/sotto` (no hub tap needed; `SOTTO_TAP` overrides the fallback), maps your Gemini key
to all three names Hermes/Sotto read (`GOOGLE_AI_API_KEY` + `GEMINI_API_KEY` + `GOOGLE_API_KEY`),
registers the Bridge from step 1 as a stdio MCP (no `BRIDGE_TOKEN` set → local mode), and
creates five crons: `sotto-morning-brief` (6:30), `sotto-evening-brief` (17:30 — its end-of-day
report includes post-meeting follow-up drafts; no standalone follow-up cron exists anymore),
`sotto-relationship-pulse` (Mon 9:00), `sotto-proactive` (`*/15`, mostly-silent nudges — set
`SOTTO_PROACTIVE=0` to skip), and `sotto-midday-digest` (12:30, an adaptive catch-up that runs the
`sotto-event` skill in digest mode and stays silent on quiet days — set `SOTTO_DIGEST=0` to skip;
`SOTTO_DIGEST_MIN`, default 8, sets how heavy a day must be before it delivers).

*Sharing Hermes with other work?* Drop `--dedicated` — the installer then leaves your global model
untouched (the brief still runs on Gemini via `compose_brief.py`); just make sure your own chat
model/key is already configured.

**5. Verify the Bridge MCP** — confirm `sotto-local` got registered:
```bash
hermes mcp list        # → sotto-local
```
If step 4 ended with **"NOT done"** instead of "Done", it found no engine at any of the three paths
it names — do step 1 (option A is the quick one) and re-run the installer.

**6. Connect Google + a channel, then run:**
```bash
hermes setup            # connect Google Workspace — interactive OAuth via the google-workspace CLI
                        # (Granola: a community stdio MCP — see RAILWAY.md §6c)
hermes gateway setup    # WhatsApp (scan a QR) or Telegram (bot token) — for scheduled-brief delivery
                        # WhatsApp is only the default; the choice is in CHANNELS.md
hermes                  # then: "Sotto, set up"  →  "Sotto, morning brief"
```

You get a brief from Gmail/Calendar + local messages, knowledge seeding into `~/SottoData`. Ask
follow-ups ("Sotto, what do I know about <name>?"). An email reply is offered into your **Gmail
drafts** (say yes and it's there, in the thread, ready for you to send); every other channel is a
one-tap **deep link** (`imessage:` / `sms:` / `wa.me`) — tap on your phone to send. Nothing sends
from the Mac.

## How briefs reach you locally
- **Interactive** — always works, no channel: run `hermes` and ask ("Sotto, morning brief").
- **Scheduled** — the installer's crons are created with `--deliver whatsapp` (same as the cloud;
  `SOTTO_CRON_DELIVER` overrides). They reach you only while `hermes gateway` is running on the Mac with
  that channel connected (step 6). Set `SOTTO_CRON_DELIVER=local` before running the installer only if
  you deliberately want cron briefs kept in the CLI session instead.

## What local mode doesn't have
- **No browser `/setup` wizard** — that's the cloud receiver. Local setup is the CLI steps above.
- **No timezone auto-detect** — the wizard captures your browser's zone in the cloud; locally you set
  `SOTTO_TIMEZONE` yourself (step 3).
- **No headless `/google/auth` flow** — connect Google with `hermes setup` (the google-workspace CLI's
  interactive OAuth), which is easier locally anyway: the browser is right there.
- **No briefs while the laptop is closed** — crons and the gateway only run while the Mac is awake.
- **No real-time event push** — the Bridge's event watcher (`/bridge/events` → triage funnel →
  instant nudges) and the cloud-side Gmail polling run in **cloud mode only** (`SOTTO_EVENTS`,
  `SOTTO_EMAIL_POLL_SECS`, `SOTTO_TRIAGE_MODEL`, `SOTTO_EVENT_COOLDOWN_MIN` — see RAILWAY.md).
  Locally you still get the `sotto-midday-digest` cron (silent unless the queue is heavy) and the
  `*/15` proactive watcher.

## Local vs cloud — when to switch
- **Stay local** if privacy-of-messages or offline matters most, or you don't want an always-on bill.
- **Go cloud** (`ONBOARDING.md` / `RAILWAY.md`) if you want briefs to fire on schedule even when the
  laptop is closed. The same `install.sh` + skills move over — tunnel-free: the Bridge app on your Mac
  dials *out* to the Railway host (`sotto-bridged --connect …`), so there's nothing to expose.
