# Agent-guided Sotto setup

Give Codex or Claude Code this repository and the prompt below. The agent can inspect your setup,
prepare protected configuration, run the supported setup path, and guide the remaining browser and
Mac steps. Account sign-ins, consent, device permissions, and provider purchase approval still need
you. Setup does not send a message or prove a brief appeared; verify those outcomes on your device.

## Copy-paste prompt

> Set up Sotto from this repository for me. Follow `AGENTS.md` and
> `docs/AGENT-ONBOARDING.md`. Ask whether I want self-hosted or invited Sotto Cloud,
> do the setup work you can, and guide me through the sign-ins and permissions.
> Verify the connection and first brief before calling setup complete.

## Agent contract

Help me set up Sotto from this repository. First read `AGENTS.md`, this guide, and the relevant
setup docs. Ask whether I want **self-hosted** or **invited Sotto Cloud** before collecting any
credentials. Inventory available authentication and target IDs without printing secrets. Never
paste credentials into chat, command arguments, logs, or version control. For self-hosting, use my
explicitly selected fresh Railway project/environment and the public self-host helper; prepare a
clean public-source checkout and derive its commit/manifest pins yourself. Use credentials already
available through the user's authenticated local tools when safe. Request only missing inputs,
collect secrets through a non-echoing local prompt, and write the config and helper state outside
the repository with owner-only permissions. Resume from
that same state if interrupted. Do not send a test message, run a paid model probe, or start a
standalone general-purpose Hermes gateway as a probe; the supported Sotto runtime includes the
gateway it needs. After the exact deployment is healthy, guide me through `/setup`,
Google Web OAuth, Bridge pairing, OS source permissions, and one owner hello. Then observe the
automatic first brief and verify its actual appearance and recovery behavior without creating
duplicate attempts. Request a manual brief only if I explicitly ask. Treat setup as complete only
after the scheduled welcome has a positively accepted delivery and the owner confirms it appeared;
a manual brief is separate evidence. For invited Cloud, use the hosted sign-in and invitation flow; do not ask me for Railway
credentials or self-host OAuth JSON. Stop at each human consent or account-approval screen and let
me complete it. Report each completed check with observed evidence, and call anything unverified
unverified.

## Choose a path

### Self-hosted on Railway

This path uses your Railway project, Gemini API key, and your own Photon connection for iMessage
delivery. Start with a **fresh, single-environment Railway project** selected by its exact project
and environment IDs. The helper refuses projects containing unrelated services and never adopts an
unmarked service. Keep its input and state files outside the checkout, mode `0600`, and retain both
for safe resume. Do not place their contents in chat or commit them.

The helper requires **Python 3.11 or newer**, Git, and a signed-in Railway CLI that supports
`railway api`. The agent should reuse an installed compatible Python interpreter and check these
prerequisites before collecting credentials or creating resources.

Before running the helper, the human must sign in to Railway and prepare the provider accounts and
credentials. The signed self-host Bridge currently also needs a **Bridge access code**; arrange
that privately with the operator before building, following [the Bridge setup steps](../ONBOARDING.md).
This is separate from the server's private setup link. Invited Cloud users unlock the Bridge through
Google sign-in and do not need this code. The agent should inspect existing local authentication and
Railway targets first; ask only for the exact fresh project/environment selection, owner phone, or
provider secrets it cannot safely retrieve. The agent derives source pins from `--inspect-source`
instead of asking the user to calculate them. For iMessage, the owner creates or obtains their Photon project credentials and
chooses the one exact owner phone number. Provider signup, billing, and purchases require human
approval. If Hermes CLI is already available in an isolated, private environment and the owner has
approved provider setup, it can guide `hermes photon setup --phone <owner-number>` and capture its
generated credentials privately; see the [pinned Photon setup instructions](https://github.com/NousResearch/hermes-agent/blob/245e48008fa814b3251f50755eb656bd9fb86cb1/plugins/platforms/photon/README.md#first-time-setup).
Do not install Hermes on the Mac just for setup, launch a standalone general-purpose gateway as a
probe, or proceed through a purchase without approval. The supported Sotto runtime includes the
gateway it needs. Gemini remains the configured model provider. The helper creates the Sotto
Railway service, `/data` volume, public domain (targeting port 8080), required variables (including a generated Bridge
token), uploads the pinned source, and waits for exact deployment success and `/health`. It does not
create a Google OAuth client, authorize Google, send messages, or make a paid model request.

From the clean public repository root, inspect the exact source receipt:

```sh
python3 tools/setup_selfhost_railway.py --inspect-source .
```

The JSON output contains `source_commit` and `source_manifest_sha256`. The agent should confirm the
checkout is the intended public source and clean (gitignored caches and virtualenvs do not count;
the helper uploads only the pinned tracked files), then create a mode-`0600` JSON file with exactly
these keys, collecting any unavailable credentials with a local non-echoing prompt:

```json
{
  "project_id": "RAILWAY_PROJECT_UUID",
  "environment_id": "RAILWAY_ENVIRONMENT_UUID",
  "service_name": "sotto-selfhost",
  "source_path": "/absolute/path/to/clean/public/sotto",
  "source_commit": "40-character-commit-from-inspect-source",
  "source_manifest_sha256": "64-character-sha256-from-inspect-source",
  "owner_phone": "+15555550123",
  "photon_project_id": "YOUR_PHOTON_PROJECT_ID",
  "photon_project_secret": "YOUR_PHOTON_PROJECT_SECRET",
  "gemini_api_key": "YOUR_GEMINI_API_KEY"
}
```

Never use a shell command containing literal credentials. Ensure Railway CLI is signed in to the
intended account. Then run and, if interrupted, rerun the same command with the same files:

```sh
python3 tools/setup_selfhost_railway.py \
  --config /absolute/private/path/sotto-setup.json \
  --state /absolute/private/path/sotto-setup-state.json
```

The helper outputs `receiver_reachable`, `model_status=unverified`, `setup_status=pending`, the
service, volume, domain, deployment, and state path, never the credential values. HTTP health proves
only that the receiver answers; it does not prove Gemini, Google, Bridge, or delivery readiness. The
helper resumes from recorded state by reconciling each step with what Railway shows: it adopts the
one resource it can prove is its own (its marked service, the sole `/data` volume on that service,
that service's domain, the deployment carrying its source identity) and stops for inspection when
the state is ambiguous. A create or upload that was sent but never confirmed is never sent again on
its own, because an empty Railway listing does not prove it was refused: the helper returns
`create_unconfirmed` (exit 2). Rerun the same command later; only if Railway still shows none of
that resource for this project, rerun it once with `--retry-unconfirmed`. Do not delete the state or
improvise a second deploy after an error; inspect the exact target and continue with the same inputs.
Cold image builds can take several minutes. The helper reports deployment status changes and waits
up to 30 minutes before returning `deployment_pending` (exit 2). Resume that same command and state;
it observes the saved deployment without uploading or creating another service.

After it reports `receiver_reachable`, retrieve the setup link from the **exact deployment's boot
logs** and open that full link privately. It includes that installation’s private setup code; the helper's
`/health` origin alone is not an authorized setup URL. The agent should open the link in the user's
browser without printing, quoting, storing in chat, or including the code in a report or screenshot.
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
2. Install the signed Sotto Bridge and unlock it with the privately supplied self-host access code.
   Pair through the link on `/setup`, select only the Mac sources you want, and grant the requested
   macOS permissions yourself. Keep the pairing link and its credentials out of chat. Keep exactly
   one active consumer for that Bridge identity.
3. The helper has already configured Photon and the exact owner number and allowlist from the one
   protected input. In `/setup`, the iMessage tile shows missing provider setup, waiting for the
   owner's first direct message, or that the owner message was received; it masks the configured
   number and does not ask for it again. The receipt is bound to that Photon project, so changing
   projects requires a new owner hello. Keep `PHOTON_ALLOW_ALL_USERS=false`.
   Send one hello from that exact owner identity to the Sotto line, then confirm Sotto's reply
   appears in the intended Messages conversation. The tile marks connected only after receiving
   an authenticated owner direct message; that receipt alone does not prove the reply appeared.
4. Observe the automatic first brief after source and delivery readiness. Verify the brief is
   visible on the device and record its attempt/receipt identity. For a scheduled welcome, require
   a positively accepted provider receipt for that same attempt before calling adoption successful;
   a marker or health response alone is not proof. If it is pending or ambiguous, hold and inspect
   that same attempt before retrying. Ordinary manual briefs and scheduled first-welcome briefs are
   distinct evidence, and automatic welcome behavior remains unverified until directly observed.

### Invited Sotto Cloud

Choose this only if the owner has invited your account. Sign in through the hosted Sotto account
flow linked from your Sotto Cloud invitation, accept the invitation, and complete the in-app Google
and Bridge steps. Cloud users do not need Railway access, a Railway
project, a Gemini key, a Photon project, or a Google OAuth client JSON. Sotto supplies the Photon
connection and handles the gateway handshake internally. Google consent and macOS source permissions
remain user actions. The hosted service and its providers process data as described in
[`docs/DATA-FLOW.md`](DATA-FLOW.md); this is not a self-hosted deployment.

If you are operating the hosted service rather than joining as an invited user, this public guide
does not cover operator provisioning.

## What counts as verified

Keep these claims separate in the final report: helper deployment success and `/health`; Google
OAuth success page; Bridge connected with selected sources and grants; Photon owner hello received;
first brief visibly delivered; and recovery of the same pending/failed attempt without duplicates.
Do not infer first-welcome or scheduled-delivery success from an ordinary manual brief, or infer
device appearance from a provider receipt alone. This guide and helper describe the supported path;
they do not claim every end-to-end configuration has been live-tested.
