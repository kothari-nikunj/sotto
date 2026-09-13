# Hermes adapter

Wires the portable Sotto backend into a [Hermes](https://hermes-agent.nousresearch.com/) host.

| File | Role |
|---|---|
| `install.sh` | one-command wiring (idempotent, `--dry-run`) — copy skills (tap fallback), bundle, persona, MCP, cron |
| `config.template.yaml` | `~/.hermes/config.yaml` template (model + `mcp_servers` + `scheduler`) |
| `configure_mcp.py` | merge the `sotto-local` Bridge into `config.yaml` (robust drop-in) |
| `web_config.py` · `web_provider.py` | install the Hermes web provider over the shared Sotto search and URL-reading capabilities; same keys, native Gemini proxy and failure behavior in both modes |
| `notification_config.py` | shared boot/install policy: keep gateway shutdown, restart, startup and interrupted-cron notices in operator logs |
| `sotto.bundle.yaml` | Hermes skill-bundle → `~/.hermes/skill-bundles/sotto.yaml` (exposes `/sotto`) |
| `sotto-persona.md` | additive chief-of-staff persona, appended to `~/.hermes/SOUL.md` |
| `start.sh` | cloud boot: seed the `/data` volume, register the reverse-relay Bridge MCP, set model + scheduler, start the trigger receiver and Hermes |
| `runtime_api.py` · `send.py` | Hermes argv, structured send acceptance, session/cron/setup boundaries; the receiver keeps generic work and delivery ownership |
| `runtime_lock.py` · `supervise.sh` · `process_group.py` | One tenant writer, retained essential process IDs, exit status and process-group shutdown |
| `managed_exec.py` · `control_vault.py` | Managed unprivileged process launch, nondumpable receiver and process-private control credential |
| `managed_volume.py` · `recovery.py` | Managed mount identity and verified offline tenant recovery; see [RECOVERY.md](RECOVERY.md) |
| `model_lease.py` | Existing receiver heartbeat renews the proxy model lease; sanitized expiry receipt only |
| `managed_config.py` · `photon_setup.py` · `photon_probe_compat.py` · `sotto_photon/` | Managed model/channel reconciliation and the pinned Photon adapter compatibility seam |
| `wa_pair.py` | drives `hermes whatsapp` non-interactively under a PTY (headless/cloud QR pairing) |

Run: `bash adapters/hermes/install.sh` (local stdio Bridge) or `BRIDGE_TOKEN=<bearer> bash
adapters/hermes/install.sh` (cloud reverse relay), then `/sotto setup`. Flags: `--dry-run`,
`--token=<bearer>`, `--port=<relay-port>`, `--dedicated` (also sets the chat model to Gemini).
The trigger receiver is host-neutral (`runtime/trigger-receiver/`). The supported Cloud and
receiver-based self-host modes run the same deterministic daily, digest and relationship procedures.
Managed boot fixes `SOTTO_CRON_DELIVER=photon`; self-host resolves its configured delivery channel.
Hermes still owns interactive chat and tool discretion; its generic one-shot boundary accepts
`SOTTO_RUN_SKILL="hermes -z"`. Standalone adapter installs retain their host scheduler and are not a
claim of receiver-level durable delivery parity.

Both the container boot and local installer reconcile Hermes' per-platform
`gateway_restart_notification: false` for Telegram, Photon and configured channels.
This suppresses infrastructure lifecycle chatter at its origin, preserving ordinary replies,
typing, Tapbacks and actionable source/provider failures. The digest procedure invokes
`digest_check.py` without a mode flag: checking is its default CLI action, and an empty
successful review completes silently. Tests exercise that real subprocess boundary.

Every receiver outbox send in managed and self-host modes goes through `send.py` and requires a
structured, unskipped provider success with a message ID. Direct `receiver.py` startup performs the
same adapter capability preflight as boot and installation, so it cannot fall back to exit-code-only
acceptance. This receipt proves provider acceptance, not delivery to the user's device.

Managed personal-pilot Google consent uses `google_setup.py` with `--auth-url --services email,calendar`, `--auth-code`, and `--check`. It requests the selected granted scopes and writes the token format consumed by Hermes and the deterministic gather. The existing desktop client can be reused with fresh owner consent; no multi-user OAuth service is required for this pilot.

Managed reconciliation declares the Sotto state, timezone, unattended/run identity and
tenant model proxy pair in Hermes’ `terminal.env_passthrough`. Its code kernel otherwise
strips them. Bridge/setup roots and upstream provider keys are never declared there.
This controls environment inheritance, not access by an agent with unrestricted shell
permissions to the tenant’s own files or processes.

Managed web Google consent is installed by `google_setup.install_cloud_google`, invoked through
the authenticated receiver bootstrap. The account broker never learns existing tenant credentials;
the adapter remains the only component that knows Hermes' credential files. The active token is
private JSON on the tenant volume, not application-encrypted. Desktop loopback setup remains the
manual pilot fallback before a web-client connection; subsequent managed reconnects use Bridge.
