# Maintainer Railway helper reference

This source-build helper is retained for maintainers and offline acceptance. New users onboard
in Bridge; it is not the user-facing installation path. The native release must pass
[the runtime qualification lane](../tools/RUNTIME-RELEASE.md) before distribution.

### Self-hosted on Railway: supervised Sotto Photon

Select this for a **fresh** self-host installation; the helper refuses to adopt any existing
service, volume or setup state it did not create. It builds the repository's root `Dockerfile`
(the supervised self-host image: Node chat runtime, trigger receiver and root bootstrap under Tini)
from your clean public checkout on Railway, so no published image is needed.

This path uses your Railway project, Gemini API key, and your own Photon Spectrum Cloud project for
iMessage delivery. Start with a **fresh, single-environment Railway project** selected by its exact
project and environment IDs. The helper refuses projects containing unrelated services and never
adopts an unmarked service. Supply an explicit physical Photon line; the owner's phone number does
not establish that line.

The helper requires **Python 3.11 or newer**, Git, and a signed-in Railway CLI that supports
`railway api`. The agent should reuse an installed compatible Python interpreter and check these
prerequisites before collecting credentials or creating resources.

Before running the helper, the human must sign in to Railway and prepare the provider accounts and
credentials. Railway entry in the new Bridge flow does not require a Bridge invite code.
The server's private setup link remains a separate capability. Invited Cloud users unlock the Bridge through
Google sign-in and do not need this code. The agent should inspect existing local authentication and
Railway targets first; ask only for the exact fresh project/environment selection, owner phone,
Photon line, or provider secrets it cannot safely retrieve. Provider signup, billing, and purchases
require human approval.

From the clean public repository root, inspect the exact source receipt:

```sh
python3 tools/setup_selfhost_railway.py --inspect-source .
```

The JSON output contains `source_commit` and `source_manifest_sha256`. The agent derives these pins
instead of asking the user to calculate them, and confirms the checkout is the intended public
source and clean (gitignored caches and virtualenvs do not count; the helper uploads only the pinned
tracked files). Then create a mode-`0600` JSON file outside the checkout with exactly these keys,
collecting any unavailable credentials with a local non-echoing prompt:

```json
{
  "project_id": "11111111-1111-4111-8111-111111111111",
  "environment_id": "22222222-2222-4222-8222-222222222222",
  "service_name": "sotto-selfhost",
  "source_path": "/absolute/path/to/clean/public/sotto",
  "source_commit": "40-character-commit-from-inspect-source",
  "source_manifest_sha256": "64-character-sha256-from-inspect-source",
  "tenant_id": "stable-local-tenant-id",
  "owner_phone": "+15555550123",
  "photon_line_number": "+15555550987",
  "photon_project_id": "YOUR_PHOTON_SPECTRUM_CLOUD_PROJECT_ID",
  "photon_project_secret": "ENTER_PRIVATELY",
  "gemini_api_key": "ENTER_PRIVATELY",
  "sotto_runtime": {
    "account_id": "stable-owner-account-id",
    "line_key": "stable-photon-line-key",
    "instructions": "Owner instructions for this runtime.",
    "chat_model_id": "gemini-3.8-flash",
    "native_media_model_id": "gemini-3.8-flash",
    "timezone": "America/Los_Angeles",
    "background_unmetered": false
  }
}
```

Replace every example ID, phone number, and placeholder with reviewed values. Never use a shell
command containing literal credentials. Ensure Railway CLI is signed in to the intended account.
`background_unmetered` is an optional JSON boolean, default `false`. Ask the owner whether to
allow progressive history learning and Dreamer to spend against their direct Gemini key without
a configured dollar cap; set it to `true` only for that explicit choice. It does not grant access
to any source. Ambient service variables cannot enable it. Both this choice and the timezone are
fixed for the volume when it is first installed: the helper refuses a changed config for an
existing install, so changing either means a fresh installation. A conflicting timezone POST is
rejected rather than acknowledged and ignored.
Keep the config and state outside the checkout with mode `0600`, then run and, if interrupted,
rerun the same command with the same files:

```sh
python3 tools/setup_selfhost_railway.py \
  --config /absolute/private/path/sotto-setup.json \
  --state /absolute/private/path/sotto-setup-state.json
```

The helper creates its own marked service, `/data` volume, and public domain (targeting port
8080), stores the private launch envelope (manifest, generated runtime, Bridge and control tokens,
and the Photon and Gemini credentials) as the service's `SOTTO_SELFHOST_LAUNCH` variable, and sets
the root Dockerfile, root directory and `/health` check. It then uploads the pinned source twice.
The first upload builds and runs a separate initializer deployment (`--prepare` start command) that
validates and prepares a truly empty ext4 `/data` volume. Its temporary `/health` response proves
only that the initializer is holding after preparation. After verifying that deployment and the
saved preparation receipt, the helper switches the start command to `--run` and uploads the same
pinned source again for the runtime deployment on the same volume. It never sends an owner message,
creates a Google OAuth client, authorizes Google, or makes a paid model probe.

The helper outputs `receiver_reachable`, `model_status=unverified`, `setup_status=pending`, the
service, volume, domain, deployment, image digest and state path, never the credential values. It
resumes from recorded state by reconciling each step with what Railway shows: it adopts the one
resource it can prove is its own (its marked service, the sole `/data` volume on that service, that
service's domain, the deployment carrying its setup marker, phase and source commit) and stops for
inspection when the state is ambiguous. A create, upload or start-command setting that was sent but
never confirmed is only observed on a rerun with the **same config and state**
(`create_unconfirmed`, exit 2); the helper re-sends it only when you rerun with
`--retry-unconfirmed` after checking Railway still shows none, because a second initializer
deployment could interrupt the first one's preparation. Do not delete the state or improvise a
second deploy after an error; inspect the exact target and continue with the same inputs. Cold
source builds can take several minutes. The helper reports deployment status changes and waits up
to 30 minutes before returning `deployment_pending` (exit 2); resume the same command and state.

The initializer's `/health` listener is temporary: it serves for **15 minutes** after preparation
and then exits. The helper polls it for up to five minutes, since a fresh Railway domain can lag
DNS/TLS behind the deployment. If that misses, the root-owned `/data/.sotto-volume.json` marker is
the proof; the helper reads it with Railway's volume file commands while the initializer still
runs. Rerun within that window. After it (`preparation_unconfirmed`), the initializer never runs
again: preparation is one-shot and refuses a prepared volume, so redeploying it cannot help.
Check that its deployment logs show no `self-host bootstrap unavailable` line, then rerun with
`--retry-unconfirmed`. The runtime deployment then replaces the stopped initializer, and setup
reports success only after the running runtime shows the exact volume marker. A crashed
initializer, a missing marker or a marker for another tenant/volume stops for manual inspection.

The helper installs; it does not upgrade. It refuses any service, volume or state it did not
create, and no command in this repository updates an installed service.

The helper's reachable status still requires live verification of the Tini/PID and mount topology,
provider metadata, loaded Photon activation and owner reply, model access, and the scheduled brief's
accepted receipt and appearance before calling the installation usable. See
[`adapters/sotto/SELFHOST.md`](../adapters/sotto/SELFHOST.md) for the runtime boundaries.

### Finish the self-host route

After it reports `receiver_reachable`, retrieve the setup link from the **exact deployment's boot
logs** and open that full link privately. It includes that installation’s private setup code; the helper's
`/health` origin alone is not an authorized setup URL. The agent should open the link in the user's
browser without printing, quoting, storing in chat, or including the code in a report or screenshot.
The supervisor passes a one-use output descriptor for that setup-link line only; general receiver
and Node output stay suppressed. Treat deployment logs as private.
If logs are inaccessible, ask the user to authenticate to Railway or open the link themselves; do
not try a bare `/setup` URL or guess a code. Then complete:

1. Create a Google OAuth **Web application** client in your Google project. Register the exact
   callback shown in `/setup`, in the form
   `https://<RAILWAY_PUBLIC_DOMAIN>/google/oauth/callback`. Download the Web client JSON, paste it
   into `/setup`, choose **Continue with Google**, approve the requested access, and wait for the
   browser to return to Sotto's clean success page. The callback is PKCE/state protected, bound to
   the authorizing browser, and single-use. No localhost page or authorization-code copy/paste is
   part of normal setup. Existing connected Desktop-client tokens continue working; replace the
   configured client with the Web client when a new authorization is needed.
2. Install the signed Sotto Bridge and choose **Other setup options → Self-host Sotto**. This
   entry continues without a Bridge access code; it grants no Cloud entitlement and no Mac-source
   consent. Pair through the link on `/setup`, select only the Mac sources you want, and grant
   the requested macOS permissions yourself. Keep the pairing link and its credentials out of
   chat. Keep exactly one active consumer for that Bridge identity.
3. The helper has already configured Photon, the exact owner identity, the physical provider line
   and first-contact proof from the protected input. In `/setup`, the iMessage tile shows missing
   provider setup, waiting for the owner's first direct message, or that the owner message was
   received; it masks the configured
   number and does not ask for it again. The receipt is bound to that Photon project, so changing
   projects requires a new owner hello.
   Send one hello from that exact owner identity to the Sotto line, then confirm Sotto's reply
   appears in the intended Messages conversation. The tile marks connected only after receiving
   an authenticated owner direct message; that receipt alone does not prove the reply appeared.
4. Observe the automatic first brief after source and delivery readiness. Verify the brief is
   visible on the device and record its attempt/receipt identity. For a scheduled welcome, require
   a positively accepted provider receipt for that same attempt before calling adoption successful;
   a marker or health response alone is not proof. If it is pending or ambiguous, hold and inspect
   that same attempt before retrying. Ordinary manual briefs and scheduled first-welcome briefs are
   distinct evidence, and automatic welcome behavior remains unverified until directly observed.
