# Quickstart

The canonical setup guides — pick your path:

- **New, cloud (easiest):** [ONBOARDING.md](ONBOARDING.md) — deploy to Railway (manual GitHub deploy
  today; one-click template coming) + one-click Mac pairing.
- **Cloud, click-by-click reference:** [RAILWAY.md](RAILWAY.md) — start with its manual-deploy checklist.
- **Local (Hermes on your Mac, stdio, no tunnel):** [LOCAL-SETUP.md](LOCAL-SETUP.md).
- **OpenClaw host:** [adapters/openclaw/README.md](adapters/openclaw/README.md).
- **Get the Mac Bridge app:** [download the signed app](https://github.com/kothari-nikunj/sotto/releases/latest)
  (recommended).

## What works today
- Brief (Gemini extraction via `compose_brief.py`) + the knowledge-graph / continuity / style loop.
- Gmail + Calendar (host-agnostic: google-workspace CLI **or** a Gmail/Calendar MCP), Granola, cron
  briefs, Ask Sotto, delivery to WhatsApp/Telegram.
- Local reads (iMessage/WhatsApp/calls/contacts) via the read-only Bridge.
- **Two-way send:** email + calendar (cloud), and iMessage/SMS via the Bridge **opt-in** (`--allow-send`);
  WhatsApp is one-tap deep links. Principle: **auto-draft, never auto-send.**

