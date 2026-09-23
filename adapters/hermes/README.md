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
| `managed_identity.py` · `check_identity.py` | Protect managed product identity from replacement while keeping tenant state writable; offline Linux image regression with the real runtime UID |
| `managed_volume.py` · `recovery.py` | Managed mount identity and verified offline tenant recovery; see [RECOVERY.md](RECOVERY.md) |
| `model_lease.py` | Existing receiver heartbeat renews the proxy model lease; sanitized expiry receipt only |
| `provider_error_compat.py` | Hash-checked pinned Hermes gateway adaptation: one plain-language provider failure at the shared chat boundaries, never the raw provider payload (see [Provider-error gateway pin](#provider-error-gateway-pin)) |
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

Managed Cloud owns the product identity in `SOUL.md`. Its file, Hermes directory,
data mount and default-home alias are protected against workload replacement.
Boot reconciles that identity from the image; the runtime still writes sessions,
learned memory and owner preferences. Owner standing instructions belong in
`knowledge/master.md`, not the product persona. Self-host persona customization
is unchanged. The image build runs `check_identity.py` offline to verify permission
denials and successful normal Hermes initialization across an existing-volume upgrade.

Photon processing Tapbacks are owned by `sotto_photon/__init__.py` for both hosting modes.
They select a small contextual working icon locally, then replace it on the same message
without first retracting it. `test_photon_feedback.py` covers replacement, interruption,
duplicate callbacks and failed reactions alongside the typing lifecycle.

## Provider-error gateway pin

`provider_error_compat.py` edits two files inside the third-party Hermes checkout —
`gateway/run.py` and `gateway/run_turn_runner.py` — and refuses to touch either unless its
whole-file SHA256 matches `PINNED_SOURCE_SHA256` / `PINNED_TURN_RUNNER_SHA256`. **Those two
hashes are the gateway files of the Hermes commit in [`hermes.commit`](hermes.commit)**
(`245e4800`); changing that commit invalidates both.

`provider_error_pinned_excerpt.py.txt` and `provider_error_turn_runner_excerpt.py.txt` are
**hand-written test fixtures, not the pinned bytes.** They reproduce only the functions this seam
patches, so the tests can exercise the patched behaviour in a container that has no Hermes source.
They have their own hashes in `test_provider_error_compat.py`. The only test that touches the real
pin is `test_actual_checkout_matches_declared_pin_when_available`, which skips unless
`$HERMES_PINNED_CHECKOUT` (or `/usr/local/lib/hermes-agent`) holds a full reviewed checkout.

Three places enforce the pin: the image build (`Dockerfile`, `--check`), container boot
(`start.sh`) and the local installer (`install.sh`). The build check exists so that a pin bump
fails the image rather than crash-looping every container.

When bumping `hermes.commit`:

1. `python3 adapters/hermes/provider_error_compat.py --check <new-checkout>/gateway/run.py` —
   it names the expected and found hashes for whichever file moved.
2. Re-read both `gateway/run.py` and `gateway/run_turn_runner.py` in the new checkout: the
   upstream classifier, its reply table and the stream finalizer must still mean what this seam
   assumes before the patch is re-applied.
3. Update `PINNED_SOURCE_SHA256` / `PINNED_TURN_RUNNER_SHA256`, and the `OLD_*` anchor constants
   if the surrounding code moved. `--check` fails loudly for a hash that matches with an anchor
   that no longer resolves.
4. Update the two `.py.txt` fixtures and their hashes in `test_provider_error_compat.py` if the
   functions they mirror changed.
5. `HERMES_PINNED_CHECKOUT=<new-checkout> python3 -m pytest adapters/hermes/test_provider_error_compat.py`
   so the real-pin test runs instead of skipping.

Both the container boot and local installer apply the reviewed provider-error gateway boundary and reconcile Hermes' per-platform
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
the authenticated receiver bootstrap. The adapter remains the only component that knows Hermes'
credential files. The active token is private JSON on the tenant volume, not application-encrypted.
Desktop loopback setup remains the manual pilot fallback before a web-client connection; subsequent
managed reconnects use Bridge.

