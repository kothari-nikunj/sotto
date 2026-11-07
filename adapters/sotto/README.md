# Sotto runtime adapter

This is the receiver's only runtime adapter. Configure the local service explicitly with `SOTTO_CHAT_RUNTIME_URL` (an IPv4 loopback `http://127.0.0.1:<port>` URL) and `SOTTO_CHAT_RUNTIME_TOKEN` (the private bearer). The receiver checks the URL and bearer shape without making an HTTP request. A live health check cannot establish a missing capability.

The adapter binds the receiver's text, gallery and late receipt calls to `ChatClient`. Text delivery requires the outbox's stable row ID and charged attempt number. The same ID is reused across attempts; the attempt number advances only when the outbox explicitly charges another try. Gallery uses its stable row ID and remains held after uncertain acceptance until a provider receipt resolves it. Provider acceptance requires a message ID and does not imply device display.

Ordinary receiver startup outside the fixed-procedure profiles below stops before serving health or intake. General unattended tool parity remains incomplete.

The adapter rotates sessions through the private service. The receiver's existing nightly clock remains the sole scheduler. Rotation deletes operational conversation history; there is no `/resume`. Canonical knowledge remains receiver-owned.

`run.py` sends one authenticated unattended request. It requires the receiver's unattended flag and durable run ID, prints only final text, and writes optional normalized usage atomically; the receiver's procedure runner reuses its usage helpers. It does not deliver the result. The receiver runs only its declared Python procedure runners, each with its single request argument; general unattended tool parity is not provided.

`google_api.py` supplies the verbs used by existing Sotto Google consumers. It reads the current credential through the shared Google service builder; OAuth setup and credential writes use the single `adapters/sotto/google_setup.py` implementation. The adapter uses the fixed packaged CLI and never searches an install tree. The Node runtime adds four strictly typed, read-only Gmail/Calendar operations to interactive chat through the receiver's authenticated `/runtime/google-read` route; they are gated by current source authority and are absent from unattended runs. Google mutations remain unavailable as Node model tools. Fake-service tests establish shape and boundary behavior, not real consent or provider availability.

CLI Gmail searches retain five independent metadata clients and the full requested query,
pagination, count and provider order. A process-wide pacer spaces metadata request starts at
least 250 ms apart, including sequential searches; it grants no burst credit after idle time.
At 20 quota units per `messages.get`, four starts per second use about 4,800 units/minute,
leaving headroom under [Google's documented 6,000-unit per-user rate for new projects](https://developers.google.com/workspace/gmail/api/reference/quota).
Actual project quotas and competing reads may differ. This adds no retry: a failed metadata
read still fails the whole search, closes all clients and stops queued reads.
Pacing preserves coverage at the cost of time: 1,767 metadata reads require at least about
442 seconds, before listing and other work, so a full 42-day pulse cannot complete in 30 seconds.

Every Google credential path requires an explicit absolute `SOTTO_HOME` (the supervisor passes
`/data/sotto-home`); a missing or malformed value never falls back to another directory.
Setup/status, first-use source checks and OAuth entrypoints report Google unavailable instead of
inspecting ambient files or raising a server error. The shared Google reader
(`_shared/lib/gmail_read.py`) enforces the same root rule for the fixed Python procedures and the
packaged CLI, and the Google setup writer (`google_setup.py`) requires it too. This path rule does
not itself prove token freshness; the child handoff separately requires `SOTTO_HOME` to be exactly
`$SOTTO_DATA/sotto-home`.

## Granola OAuth disconnect fence

The Granola connect start, callback, and disconnect flow share the existing connector-token sidecar
lock. A callback holds it across pending-state revalidation/consumption, the bounded OAuth exchange,
token write, and managed-consent enablement. Disconnect holds the same lock while recording
consent=false, cancelling pending Granola states, unlinking the token, and syncing the containing
directories. A successful disconnect therefore leaves no credential or earlier pending callback
that can later recreate one. A stale callback after disconnect fails its one-time-state check.

If consent revocation, state cleanup, token removal, or lock acquisition fails, the handler reports
failure instead of claiming that disconnect succeeded. The OAuth exchange is bounded to 20 seconds;
the existing lock acquisition wait is 10 seconds, so a competing request can fail closed while a
callback is still in flight. The fence is local coordination: it does not revoke a provider-side
authorization code or prove that provider access was revoked. Verification uses synthetic state and
mocked provider HTTP only.

## Shared bootstrap and diagnostic contracts

`bootstrap_runtime.py` owns the same bounded health-only server and environment cleaning used by managed/self-host entrypoints. Their manifest validation and secret-file cleanup order remain separate. `google_read_contract.py` loads the finite diagnostic JSON from immutable source or compiled runtime code paths; it never searches tenant data. This changes no source grants, diagnostic vocabulary or model configuration.

## Offline configuration staging

The [native Railway setup boundary](NATIVE-SETUP.md) documents the shared configuration
compiler and the remaining authenticated provisioning protocol. Bridge can check an already
prepared self-host deployment through the receiver's read-only `/selfhost/status` route;
compilation and status checks do not provision a fresh installation.

`startup_config.py` converts an explicit private deployment manifest into Node configuration
and a separate receiver URL/token projection. It performs no credential discovery or service
startup, and does not change readiness. It accepts paired Telegram or a Photon Cloud project with either a matching activated
receipt/fixed thread or explicit first-owner-DM mode and physical sending line. First-contact
rendering does not create an activation receipt; Node admission and the existing receiver
writer still perform that handoff. See
[the startup fixture contract](STARTUP-FIXTURE.md) for inputs, UID ownership, private staging
and the caller-attestation boundary.


`process_handoff.py` is the pure child projection for the managed Photon, self-host Photon and
self-host Telegram profiles, using that same renderer. It preserves explicit receiver proxy/lease,
activation and owner-selected model/timezone inputs, and returns argv, the complete allowlisted
child environments (nothing ambient is copied), UID metadata and a config digest. It does
not install files or launch either process. By default the receiver remains loopback-only. An
optional `public_receiver_authority` requires a separately trusted `trusted_service_id`, exact
tenant, provider service ID, DNS hostname and target port matching the rendered receiver port.
It projects only `PORT` and `RAILWAY_PUBLIC_DOMAIN` into the receiver, so the existing listener
and managed setup/OAuth/Bridge paths can share one port. This validates input shape and internal
binding, not the provider's current domain attachment; a trusted supervisor must verify that
fact independently before calling it. The helper itself starts nothing; see the startup fixture
contract above.

`native_replacement.py` checks the closed handoff against staged UID10000 Node configuration
and UID10001 receiver identity bytes, then requires the original lifecycle owner's tenant lock.
It retains that lock while launching the two roles and writes the replacement epoch
(`.sotto-epoch.json`) before either child starts. Startup requires one of the fixed-five profiles
below; any other selection is refused. Synthetic Linux fixtures exercise the process transitions.

## Fixed-five procedure profiles

The root supervisor selects `managed-photon-fixed-five-v1`, `self-host-photon-fixed-five-v1` or
`self-host-telegram-fixed-five-v1` from the manifest's deployment and channel, and passes it to
`start_configured_replacement(..., limited_profile=...)`. It derives the receiver's profile
environment from the validated handoff. Setting an environment variable alone is not a deployment
procedure.

A first start requires a fresh volume. Empty files cannot prove that no historical delivery
occurred. Preflight refuses any outbox history, unsupported active work and personal-routine
slots/cursors on a first start. Each profile admits the five image-defined schedules—morning/evening
briefs, relationship pulse, proactive check and midday digest—and their closed packaged
background/event procedures, and validates work again before execution. Arbitrary scripts and
general model-facing tools are excluded; owner-approved personal routines use their own receiver
writer and cursor path.

Stage a root-owned `SOTTO_DATA` with group 10001 and mode 01771. Its `node-config`, `node-state`
and `node-media` directories belong to UID/GID10000 with mode 0700; receiver-private `runtime` and the
`sotto-home` credential home (`SOTTO_HOME`, holding the Google token) belong to UID/GID10001 with mode 0700. Node configuration contains its own explicit channel credentials. All persistent role state stays inside
the tenant directory. Preflight verifies this layout without changing permissions.

Node starts first. The receiver then verifies its private expected identity against loaded
Node configuration before starting workers or serving requests. Activation ACK comes later:
the receiver remains its sole writer. Until the current ACK is verified, the cron tick does
not consume slots or admit background work. No source consent is inferred from activation.

A first start through `start_configured_replacement()` rejects existing outbox history. The
[root supervisor](#explicit-root-supervisor-command) also supports same-volume restart through the
persisted epoch and per-record recovery checks. Public-domain attachment, live provider acceptance
and live deployment authorization remain separate gates.

Before the children start, the supervisor publishes the private version-three replacement epoch.
It binds the verified configuration and child projection, receiver identity, physical tenant root
identity, and exact root-owned volume marker read through the original root descriptor. The marker
does not attest a persistent mount; the trusted deployment caller must verify that independently.
An older-version epoch is refused, not upgraded.

There is no export, restore or role-repair path: a lost or damaged volume is replaced by a
fresh one.

The same explicit manifest can project `receiver_channel_expectation(config)`: bounded Photon
owner/account/tenant/line, project fingerprint, route mode and local port, with no runtime bearer,
project secret, model or source grants. The trusted supervisor must stage only this projection as
the receiver UID with `stage_receiver_channel_expectation()` in a private directory. It is a
separate `expected-channel.json`, not a copy of the Node UID's private runtime config. The
`runtime_channel_proof.check_current_channel()` reads that caller-explicit path and
crosschecks the receiver's current credentials, Photon receipt and timezone with Node's
authenticated activation ACK. A successful result includes the exact resolved thread and v1
route digest, but grants no routine execution or send. The receiver and the skill scripts it runs
both use UID 10001 in the image; this projection does not isolate a script from receiver secrets.

The Node journal records the resolved scheduled-send route digest before provider
contact and emits it with the immutable payload hash only for an accepted receipt. Schema-6
upgrades cannot reconstruct the historical route of a schema-5 row. If a scheduled text or
gallery row lacks that digest and remains prepared, dispatching, unknown or rejected, startup
stops before intake and service until explicit offline reconciliation. Accepted/cancelled legacy
rows and interactive replies are exempt; accepted legacy history still has no route proof. No
current destination is used to backfill an old route. New scheduled-text rows retain the normal
96-attempt policy, with no change to receiver outbox retention.

## Explicit Telegram link credential

In the Telegram profile, `telegram_bot_token()` reads only
`$SOTTO_DATA/telegram-link.json`, the private record written by `telegram_link.persist()`.
It checks the captured positive owner ID, bounded ASCII bot-token shape and exact agreement
with any configured `TELEGRAM_ALLOWED_USERS` or `TELEGRAM_HOME_CHANNEL`. It never falls back to
`TELEGRAM_BOT_TOKEN`, a dotenv file or HOME. Unsupported, missing or
ambiguous input returns unavailable without exposing the token.

The reader anchors the data directory, requires current effective-UID ownership of the private
regular link file, and rejects symlinks, hardlinks, duplicate fields, oversize data and observed
file/root replacement or permission changes. Root-owned sticky managed data roots are accepted.
These observations do not form a transaction or prove fresh pairing, current Node bot identity,
or running Telegram delivery. The receiver still performs its existing token-bound owner check
and verifies the loaded Node identity before workers start.


## Loaded channel configuration agreement

`ChatClient.verify_configured_identity(expected_config)` checks the authenticated Node
`POST /v1/configured-identity` endpoint against explicit rendered configuration. Bootstrap derives
the bounded response from the configuration it actually loaded. A domain-separated, length-prefixed
HMAC binds channel, tenant, owner, logical line, target, fixed route or first-contact physical line,
and exact channel credential fields without returning raw credentials. The expected runtime bearer
must match the client's bearer. Missing, malformed and mismatched evidence is unavailable.

This is static channel configuration agreement. It does not establish provider credential validity,
connection health, dynamic owner-DM binding or activation ACK, model policy, source grants, installed
config-file bytes or full startup-manifest parity. Direct legacy server constructors without a
loaded-config identity return unavailable. The verifier is an explicit helper; it does not relax
receiver readiness or automatically authorize a consumer handoff.


Startup rendering for each profile also derives a separate capability for the literal
`/loop add <text>` command. The coordinator sends an exact journal-bound owner intent to the
receiver's private loopback writer, whose current activation receipt and timezone remain
independent checks. The write capability stays out of both projected child environments and the
agent factory configuration. It cannot be substituted by the ordinary read bearer.

Each profile also accepts literal owner-sent `/reminder at <ISO8601-with-offset>
<text>`, `/reminder list`, and `/reminder cancel <int_id>` commands. Reminder text is limited to
500 Unicode codepoints and the timestamp needs an explicit `Z` or numeric offset. The coordinator
journals the admitted owner event; the receiver verifies the current route and records a separate
reminder-domain receipt in the existing one-shot intentions log. The proactive 15-minute cadence
and normal eligibility and delivery checks apply, so delivery at the exact requested minute is
not promised. The list shows only pending owner-authored reminders. Cancellation prevents future
scheduling but cannot recall a send already past its delivery check.


`ChatClient.activation_state(expected_config)` and `runtime_api.activation_state(expected_config)`
read the private dynamic Node journal state against explicit rendered Cloud Photon configuration.
The default adapter timeout is three seconds and the response bound is 8 KiB; redirects, duplicate
JSON fields, nonfinite values and mismatched identity are refused. `absent` and `pending` grant
no activation. `acknowledged` records Node's durable owner-DM binding and ACK, including after
raw input retention; it does not replace the receiver's current policy or sole Photon receipt.
This helper is read-only and does not itself authorize personal routines or startup.


The canonical Google token writer adds an opaque `sotto_source_generation` at explicit connect
and preserves it during refresh. The pinned Google credentials parser accepts this extra field.
The receiver uses it for self-host conversation-context revocation; it is not a grant, a successful
source read, or a reason to skip consent. Existing tokens are not silently assigned a generation.


Gallery delivery across process UIDs uses authenticated `POST /v1/send-gallery-bytes`. The receiver
reads exactly four ordered private renderer PNGs through bounded no-follow descriptors, then sends
canonical base64 bytes. Node checks the body/image bounds, names and PNG signature before hashing
the exact bytes into the existing gallery payload and durable dispatch receipt. The receiver's
artifact files remain private to its UID; Node needs no read access to that volume. The existing
path endpoint is retained for explicit standalone use. A contacted gallery with unknown acceptance
remains held for receipt reconciliation; this transfer introduces no automatic resend or second
artifact store. These are local fixture contracts; live provider acceptance is separate evidence.


## Typed Google read boundary

The Google read route requires explicit `SOTTO_HOME`. It independently checks self-host token generation or, for managed mode, latest-applied bootstrap lineage and tenant grants before and after the provider request. The shared source-authority response is version 2 and carries separate Bridge and Google source sets; a Bridge health failure does not erase a current Google grant. Provider wire/output byte caps and request budget are documented in the [runtime README](../../runtime/sotto-chat/README.md); these controls do not alter OAuth grants.


## Explicit root supervisor command

Every fixed-five profile runs under the root entrypoint in `root_supervisor.py`; the self-host
and managed bootstraps stage its operator file from their launch envelope and call it (the boot
sequence is described once in [docs/ARCHITECTURE.md § Boot](../../docs/ARCHITECTURE.md#boot)).
For a manually managed container, the deployment operator must place a private, root-owned `0600`
operator file after independently verifying the tenant's `/data` volume attachment,
service identity and public receiver domain. The file provides the same explicit
manifest and authority inputs used to render Node and receiver configuration; it is
never a model input. Launch this image with a private PID namespace and tini as PID 1:

```sh
docker run --network "$NETWORK_NAME" --mount "type=volume,source=$VOLUME_NAME,target=/data" \
  --mount type=bind,source=<operator-private-directory>,target=/run/sotto-operator,readonly \
  --entrypoint /usr/bin/tini "$RUNTIME_IMAGE" -- \
  python3 -I -B /app/adapters/sotto/root_supervisor.py \
  --operator-file /run/sotto-operator/supervisor.json
```

This is a command shape, not a substitute for the deployment's PID, UID, capability,
network and volume policy. The supervisor acquires the original tenant flock, refuses
existing consumers, stages only role-specific private configuration, then creates the
durable replacement epoch before starting either runtime role. On supervised stop, it
sends the verified Node leader an internal `SIGUSR2` quiesce request while keeping the
receiver available for Node's provider and journal cleanup. Once Node exits, or its
35-second grace expires, the supervisor stops the remaining tree; a stuck Node is
killed, then the receiver gets up to five seconds under `SIGTERM` before final
escalation within the 45-second stop deadline. The tenant flock stays held until
every owned process is reaped.
Operators should stop the root supervisor, not send `SIGUSR2` to arbitrary
processes or treat the signal as a provider acceptance receipt. A clean process restart
on the same volume verifies the persisted epoch's identity (tenant, volume marker, profile,
receiver identity, tenant directory inode) and reuses it
unchanged; rotated credentials, overrides and a release's Node config are re-staged. Fresh installation requires
no receiver work/outbox history. Restart admits structurally supported history for
the receiver and Node to recover per record; it never turns an unknown send into an
accepted one. A changed owner, line or bot is a fresh installation, not a restart.

### Photon shared projects

Managed provisioning can explicitly set `sotto_runtime.photon_route_mode` to
`shared_project` for a Photon shared project. The corresponding protected manifest
uses `firstContact: {"mode":"owner_dm_shared_project"}`. The default remains the
physical-line mode. Shared mode binds the configured project's credentials, exact
owner, account, tenant and captured direct-message thread; it admits the SDK's
`shared` route only under that explicit mode. The configured phone number is display
metadata in shared mode, not provider proof of a physical sending or receiving line.
Owner activation, source consent and accepted-delivery receipts remain required.
A physical-mode journal cannot be converted by editing its immutable discovery seed.


Google calendar lists default to visible readable calendars; hidden calendars require an explicit selection. The management page's Preferences control
can narrow reads to explicit IDs; `primary` selects the main calendar. This uses the existing
`config/settings.json` store and binds saved choices to Google consent generation. Reconnects
require resaving an existing choice. No calendar write permission changes. Calendar inventory and
event pages are bounded; failed/revoked calendars produce partial coverage. Typed reads preserve
`calendar_id` with each provider event ID; brief gathering uses calendar-scoped internal IDs and
retains the original provider ID, recurrence identity and date/time-zone fields.

All-calendar inventory requires Google Calendar list access. If discovery fails before returning any calendars, Sotto still attempts the existing primary-calendar read and reports incomplete coverage. A legacy events-only grant therefore keeps its primary events; reconnect Google or explicitly choose known calendar IDs (including `primary`) in Preferences for complete coverage of that selection. Explicit selections, including an empty selection, never expand to primary. Reads do not broaden OAuth or write permissions.
