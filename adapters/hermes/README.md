# Hermes adapter

Wires the portable Sotto backend into a [Hermes](https://hermes-agent.nousresearch.com/) host.

| File | Role |
|---|---|
| `install.sh` | one-command wiring (idempotent, `--dry-run`) — copy skills (tap fallback), bundle, persona, MCP, cron |
| `config.template.yaml` | `~/.hermes/config.yaml` template (model + `mcp_servers` + `scheduler`) |
| `configure_mcp.py` | merge the `sotto-local` Bridge into `config.yaml` (robust drop-in) |
| `sotto.bundle.yaml` | Hermes skill-bundle → `~/.hermes/skill-bundles/sotto.yaml` (exposes `/sotto`) |
| `sotto-persona.md` | additive chief-of-staff persona, appended to `~/.hermes/SOUL.md` |
| `start.sh` | cloud boot: seed the `/data` volume, register the reverse-relay Bridge MCP, set model + scheduler, start the trigger receiver and Hermes |
| `wa_pair.py` | drives `hermes whatsapp` non-interactively under a PTY (headless/cloud QR pairing) |

Run: `bash adapters/hermes/install.sh` (local stdio Bridge) or `BRIDGE_TOKEN=<bearer> bash
adapters/hermes/install.sh` (cloud reverse relay), then `/sotto setup`. Flags: `--dry-run`,
`--token=<bearer>`, `--port=<relay-port>`, `--dedicated` (also sets the chat model to Gemini).
The trigger receiver is host-neutral (`runtime/trigger-receiver/`); it runs the brief via the host's
scriptable one-shot — `SOTTO_RUN_SKILL="hermes -z"` (Hermes has no `hermes run`; `-z` is "prompt in,
final text out"). The receiver hands it a prompt naming the brief skill + the staged payload path.
