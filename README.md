# Sotto — Chief of Staff (host-agnostic)

A standalone Sotto backend that turns an MCP + agentskills agent runtime — **[Hermes](https://hermes-agent.nousresearch.com/) or OpenClaw** — into a chief-of-staff assistant with **all of Sotto's goodness** (brief pipeline, people/company knowledge graph, continuity ledger, style fingerprint, approval tiers, signal correlation), delivered through chat (Telegram/iMessage/WhatsApp) instead of a Mac app.

> **This folder is a self-contained, movable project.** Nothing here depends on any parent repo at runtime.

## Architecture — portable core + swappable host adapters

The **Sotto backend is host-agnostic** (open standards only); a thin **adapter** wires it into one runtime. Building for OpenClaw = adding `adapters/openclaw/`, not a fork. See [`adapters/README.md`](adapters/README.md).

| Portable core (no host coupling) | What |
|---|---|
| **Sotto Bridge** (Mac app) | thin macOS **menu bar app** — **read-only**: reads local context and serves it as an **MCP server**. Tunnel-free: it **dials OUT** to the host (reverse link), or runs over stdio for a local host. Sending replies is one-tap **deep links** (`imessage:`/`sms:`/`wa.me`/`mailto:`), so the Bridge never sends. No models, no storage, no pipeline. |
| `sotto-chief-of-staff/` | the **processing**: agentskills `SKILL.md` + Python `execute_code` scripts (knowledge-graph dedup, continuity, style, signals, preferences) over `$SOTTO_DATA`. The extraction prompt is run by the host's Gemini. |
| `runtime/trigger-receiver/` | host-neutral HTTP receiver for the Bridge "I'm up" push; runs a skill via `$SOTTO_RUN_SKILL`. |
| `contracts/` | LocalData JSON Schema + the exhaust file layout. |

| Host adapter (thin, per-runtime) | |
|---|---|
| `adapters/hermes/` | bundle, persona→SOUL.md, config template, `configure_mcp.py`, `install.sh`, cron. |
| `adapters/openclaw/` | the same shape for OpenClaw (written; not yet validated against a live build). |

The host runs on Railway, connects to the Bridge over MCP, gets Gmail/Calendar/Granola natively, stores the knowledge graph on a volume, and delivers via a gateway. The brief fires on the Bridge's wake push (cron is a fallback).

## The model: a 1M-context Gemini model

The brief's big multi-source extraction needs a **1M-context model**, and `compose_brief.py` calls the **Gemini API directly** (via `GOOGLE_AI_API_KEY` — the same key the host manages; no second key store). Get a **Gemini API key** ([Gemini 3 Flash](https://aistudio.google.com/apikey) — cheap, 1M, a great general driver too). Override the model with `SOTTO_GEMINI_MODEL`; set an **optional** backup (`SOTTO_FALLBACK_MODEL` / `SOTTO_FALLBACK_API_KEY`, also 1M-context) and the brief falls back to it on a quota/5xx error. (Non-Gemini providers aren't wired into the brief's direct call today — the fallback is Gemini-API-shaped.)

## Setup

Two ways to run it — same backend, skills, and knowledge graph; only the transport differs:

| | Where Hermes runs | Best for | Guide |
|---|---|---|---|
| **Cloud** | Railway container (always-on) | briefs fire on schedule even with the laptop closed | **[ONBOARDING.md](ONBOARDING.md)** (friendly) · Railway click-by-click: **[RAILWAY.md](RAILWAY.md)** |
| **Local** | your Mac (stdio, no tunnel) | privacy / offline / no hosting bill | **[LOCAL-SETUP.md](LOCAL-SETUP.md)** |

### Your first brief — 3 steps ([ONBOARDING.md](ONBOARDING.md) has the full walkthrough)
1. **Deploy** the host on Railway with your **Gemini key** + the four required settings — Root Directory, variables (incl. a `BRIDGE_TOKEN` you generate), the `/data` volume, a public domain ([RAILWAY.md](RAILWAY.md) checklist; one-click template coming). The container installs Hermes, the skills, the persona, and the morning/evening crons for you.
2. **On your Mac:** [download the signed **Sotto Bridge** app](https://github.com/kothari-nikunj/sotto/releases/latest) → open it, review the **data-source toggles** (turn off anything you don't want shared *before* connecting) → open the **setup link from the deploy logs** (`[sotto] Setup link` — the `/setup` wizard plus its access code) and click **“Open in Sotto Bridge”** (fills host + token, nothing typed) → grant **Full Disk Access** (the app restarts its engine to pick it up) → toggle **Start at login**. Connect WhatsApp (QR) + Google — all on the same wizard page.
3. **In chat, say "set up Sotto."** The guided setup verifies every connection and reports each one honestly, **seeds your memory + writing voice** from ~6 weeks of history (so day 1 isn't cold), confirms your brief schedule, and offers your first brief on the spot. Then just say **"good morning."**

That's it — everything after is conversational ("who am I meeting?", "draft Dhruv", "who am I losing touch with?").

**Close your laptop and reopen it?** Nothing to do. The Mac app dials *out* to Hermes and owns the connection, so when your Mac wakes the Bridge just resumes polling — no tunnel to recover, no host restart. While the Mac is asleep, the host's relay stays up and tool calls return "offline"; the brief degrades to the last cached local snapshot.

- **You don't have Hermes yet?** That's fine. For **cloud**, you never install Hermes on your laptop —
  deploying the container *is* the Hermes install (the Dockerfile runs Nous Research's installer + bakes
  in the Sotto layer). For **local**, you run `curl …/install.sh | bash` once, then `adapters/hermes/install.sh`.
- **Delivery channel:** Hermes' gateway supports 20+ — **WhatsApp** (native, QR), **Telegram** (bot
  token), or **iMessage** (via a BlueBubbles server on your Mac). WhatsApp is easiest; full comparison +
  setup in **[CHANNELS.md](CHANNELS.md)**. Reply *sending* is deep links regardless of channel.
- **The Bridge menu bar app:** [download the signed app from Releases](https://github.com/kothari-nikunj/sotto/releases/latest)
  (the signed app is on the Releases page).
  It supervises the engine, shows a plain-language connection status (wrong token vs host down vs
  offline), and offers **Start at login** — no LaunchAgent. Removal: [docs/UNINSTALL.md](docs/UNINSTALL.md).
- **Only key you need:** a 1M-context model key — a [Gemini API key](https://aistudio.google.com/apikey).


Docs: ONBOARDING.md (setup) · RAILWAY.md (deploy reference) · LOCAL-SETUP.md (local mode)

## The one-paragraph picture
```
you ⇄ WhatsApp/Telegram/iMessage ── Agent host (Hermes or OpenClaw; Railway, Gemini 3 Flash key, cron, TTS, volume)
                    │  runs the /sotto skills: extraction prompt + dedup/continuity scripts
                    ├── native: Gmail · Calendar · Granola · web
                    └── MCP (reverse link) ⇠ sotto-bridge (Mac, read-only): read_local · get_messages · health
        replies = deep links you tap on your phone · the host is a swappable adapter (adapters/<host>/)
```
