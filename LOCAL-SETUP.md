# Local setup: Hermes on your Mac

Run Sotto and its Bridge on your Mac, with no Railway account or tunnel. Hermes starts the
Bridge as a local child process over stdio. Your Mac must stay awake with the gateway running
for scheduled briefs. For an always-on self-hosted server, use [ONBOARDING.md](ONBOARDING.md).

**Local hosting still uses your model provider.** Briefs and prep send relevant messages, notes
and calendar context to the provider you configure. Sotto does not ship a fully offline model.
The skills and memory code are shared with the container; the standalone Hermes scheduler does
not provide the receiver's durable work queue, retries or delivery receipts.

## Before starting

- A Mac and Python 3.12. The downloaded Bridge needs no Rust toolchain.
- A model API key. Gemini is the default; see [docs/MODELS.md](docs/MODELS.md) for other families
  and feature limits. Keys stay in your Hermes configuration, never in this repository.
- Run these commands from the directory containing `requirements.txt` and `adapters/`.

## 1. Install the Bridge and check access

Download **Sotto Bridge.app** from [Releases](https://github.com/kothari-nikunj/sotto/releases/latest)
and drag it to `/Applications`. The signed app's first launch requires an invitation code;
[ask for one in Issues](https://github.com/kothari-nikunj/sotto/issues). Hosting your own server
requires no Sotto account, but it does not bypass the app's invitation requirement.

The installer finds the bundled engine automatically. If yours lives elsewhere, set
`SOTTO_BRIDGE_BIN` to its absolute path before step 4.

For this stdio path, grant **Full Disk Access to your terminal app** in System Settings,
Privacy & Security, then fully quit and reopen the terminal. Hermes and the Bridge are children
of that terminal. Verify access:

```bash
"/Applications/Sotto Bridge.app/Contents/Resources/sotto-bridged" --doctor
```

Read each source's result. `ok` with zero rows can mean no recent activity. `needs Full Disk Access`
requires the permission above; `unavailable` can mean that app is not installed. A nonzero exit
alone does not tell you which source needs attention. Local stdio mode is read-only by default.

## 2. Install Sotto's Python dependencies and pinned Hermes

Sotto's Python environment is separate from Hermes' own environment. Install the locked
runtime dependencies, including image rendering and Google clients:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
source .venv/bin/activate
bash adapters/hermes/install-runtime.sh
export PATH="$PWD/.venv/bin:$HOME/.local/bin:$PATH"
```

The wrapper installs exactly `adapters/hermes/hermes.commit` and checks its compatibility.
It refuses to replace an incompatible existing Hermes installation. If that happens, use
Sotto's container, or back up and deliberately replace your existing installation yourself.
Do not run `hermes update` independently: use a reviewed Sotto release with its matching pin.

## 3. Configure the key, data directory, timezone and channel

For the default Gemini and Telegram setup:

```bash
mkdir -p "$HOME/SottoData" ~/.hermes
printf 'GOOGLE_AI_API_KEY=%s\nSOTTO_DATA=%s\nSOTTO_TIMEZONE=%s\nSOTTO_CRON_DELIVER=%s\n' \
  '<your-gemini-key>' "$HOME/SottoData" 'America/Los_Angeles' 'telegram' >> ~/.hermes/.env
export SOTTO_CRON_DELIVER=telegram
```

Set your own IANA timezone, for example `Europe/London`. Local setup cannot detect it from a
browser. If repeating these steps, edit existing values instead of appending duplicate keys.
For WhatsApp, use `whatsapp` in both places. Without an explicit choice the local installer
keeps its historical WhatsApp default. The container defaults to Telegram.

## 4. Wire Sotto into Hermes

With the Python environment from step 2 still active:

```bash
bash adapters/hermes/install.sh --dedicated
hermes mcp list
```

`--dedicated` sets Hermes chat to the default Gemini model. Omit it to keep your chat model.
Brief composition has its own provider setting. The installer checks Python dependencies and
Hermes delivery compatibility before modifying configuration, copies the skills, registers
`sotto-local`, and installs the enabled schedules from `adapters/hermes/crons.json`.
If it reports **NOT done**, fix the named missing Bridge path before continuing.

## 5. Connect and run

```bash
hermes setup             # connect Google and configure your model
hermes gateway setup     # link the same channel selected in step 3
hermes                  # ask: "Sotto, set up", then "Sotto, morning brief"
```

For scheduled delivery, leave the gateway running in a terminal with Sotto's Python active:

```bash
source .venv/bin/activate
export PATH="$PWD/.venv/bin:$HOME/.local/bin:$PATH"
hermes gateway
```

Reactivate that environment after opening a new terminal. A separately installed launch service
does not automatically inherit it. Check `hermes cron list` for the actual schedules and channel.
Telegram briefs are text by default. The four-photo presentation requires the configured Photon
channel; see [CHANNELS.md](CHANNELS.md).

Google and local context can feed the first requested brief. Granola is optional; for local
credentials or a custom MCP server, see [RAILWAY.md](RAILWAY.md)'s Granola fallbacks.
Email replies default to Gmail drafts. The local stdio Bridge configured here has no send
permission; use the offered draft or link to send yourself.

## Memory and automation limits

Ordinary briefs learn from the context they read. The standalone install does not run the
receiver's automatic welcome, progressive six-week history scan or Dreamer heartbeat. It also
lacks the receiver's live event push, Google polling, dashboard and durable delivery retries.
An empty historical graph does not mean you have never spoken to someone.

In a **receiver-based self-host**, background history learning additionally needs a budgeted
model proxy. A deliberate `SOTTO_BACKGROUND_UNMETERED=true` permits direct-key background calls
at your expense; it does not add a receiver to this standalone install. Without either, that
background work stays held while ordinary chat and briefs continue. Check
`knowledge/history-state.json` for coverage rather than assuming six weeks were learned.
See [RAILWAY.md](RAILWAY.md) for proxy configuration and spend controls.

Choose the always-on container if you need the full scheduled product. It can send briefs while
your Mac sleeps, using cloud sources and the last permitted local snapshot. It cannot refresh
local data from a sleeping Mac.
