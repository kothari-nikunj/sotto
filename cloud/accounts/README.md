# Sotto Cloud accounts

This service adopts pre-created, isolated tenants. It does not create Railway projects, allocate
Photon identities or admit a public cohort. `registry.py` uses verified Google issuer/subject as
account identity, with one active tenant per account. Email is a one-time admission only; a changed
email cannot transfer a tenant. The existing pilot owner and route are migrated before accepting
new callbacks. Tenants keep running when this service is down.

`POST /v1/signin/start` accepts a P-256 DER public key and a device challenge. It returns an
account-service URL and an independent read-only polling bearer; it returns no confirmation code.
The browser completes Google OIDC/PKCE, then the callback displays an eight-character code. The
user enters that code in Bridge, which submits it to `POST /v1/signin/confirm` with the polling
bearer. The Google URL is never returned to the Mac, and the browser receives neither the Bridge
credential nor the Google refresh token. Bridge polls `/v1/signin/status`, then signs the device-bound grant and exchanges it at the
instance's `/cloud/pair`. Repeated sign-ins reuse the existing tenant and memory.

Google consent requests OpenID identity, Gmail read/write (`gmail.modify`), Calendar read/write (`calendar`) and Google Contacts read/write (`contacts`). Gmail access also
permits sending at Google's API; Sotto's attended approval policy still applies. Denying data
permissions is valid sign-in, with those sources unavailable. Existing data consent is replaced
by the new grant. A source must be explicitly enabled and successfully read before it opens the
scheduled-brief gate. Turning it off blocks new HTTP event/wake payloads from that source.

The code permits five native confirmation attempts during the 15-minute session. Confirmation is
bound to both the callback browser's hashed cookie and the polling bearer, and a callback state or
forwarded Google code cannot substitute for it. The broker
stores encrypted pending handoffs on its volume for at most 15 minutes. Status polling
retries tenant delivery with one request ID. Every authorization receives a monotonically increasing per-account credential generation. The
receiver commits the generation before changing the credential file, rejects a delayed older grant,
and returns completed retries without reinstalling credentials. An interrupted installation can
retry only while its generation is still current; a crash cannot reopen older permissions.
The broker requires the receiver to echo the generation. Acceptance erases its Google credential copy; cleanup
removes expired sessions on the next sign-in. The instance writes the active authorized-user JSON
as a mode-0600 file on its private Railway volume because the current Hermes Google skill reads
that format. Application-level encryption of this active token is not implemented; do not claim it
is. Pairing grants are one logical operation, device-signed and retryable for ten minutes so a
lost HTTP response does not force a new authorization. They cannot rebind the account or device.

## Deployment

Deploy the receiver with credential-generation support first, then this directory with a private
`/data` volume, port 8080 and `/health` health check. Preserve both account and receiver state in
backups; rolling back only the generation ledger after newer consent could re-enable old grants.
`SOTTO_ACCOUNT_CONFIG` is a managed secret containing:

```json
{
  "origin": "https://accounts.example.com",
  "instance": "https://tenant.example.com",
  "tenant": "opaque-tenant-id",
  "owner_email": "owner@example.com",
  "client": {"client_id": "web-client-id", "client_secret": "web-client-secret"},
  "state_key": "Fernet-key-generated-by-the-operator",
  "control_token": "independent-random-instance-bootstrap-credential"
}
```

The Google Web application client must allow exactly `origin + /oauth/google/callback`.
Configure the tenant with the same `SOTTO_CONTROL_TOKEN` and its `SOTTO_IMESSAGE_NUMBER`.
These are operator settings; customers enter neither Railway credentials nor pairing secrets.
Preserve the state encryption key across deployments/backups. Rotate the control credential on
both services together; old pending grants will fail and require a new sign-in. No Railway account
API token is deployed in this personal-pilot service.

Cloud is selected in the same Bridge app as self-host. Legacy `mode=cloud` means a remote server;
it is not reinterpreted as managed hosting. Managed identity is persisted separately as
`deployment=managed`. Google and Bridge consent are separate from Full Disk Access. New Mac installations show all
sources enabled before the dedicated disk-access step. Collection and Mac consent reporting start
only after that step is confirmed; existing source choices survive reconnects.

The receiver now issues individually revocable Bridge bearers. The control-authenticated
`/cloud/devices` list/revoke API is scoped to Bridge access; it does not erase stored data, revoke
Google grants or invalidate existing browser sessions. Existing shared pilot access remains until
the first device credential is issued. Newly enrolled devices receive no shared setup code; extra
web setup uses an existing login or operator login URL until dashboard exchange is implemented.
See [the operations runbook](../../adapters/hermes/RECOVERY.md).

Fleet provisioning, active-token encryption, dashboard session exchange and
an optional iCloud mirror remain later work. Disabling a source stops new ingestion; historical
memory and existing snapshots retain the current retention policy, with source-specific purge
still pending. The pilot is not a claim of production readiness for multiple customers.

The account-service home page offers invited Google sign-in in the browser and a
`sotto-bridge://cloud` link to the same Sotto setup/settings window. Bridge's sign-in task survives
closing its window while Google is open.

Broker token exchange and tenant handoff refuse HTTP redirects so credentials cannot follow a
misconfigured endpoint to another host.

## Device-optional account connection

Existing requests to `/v1/signin/start` retain the P-256 key/challenge contract. Browser requests
use `entry=browser`, the initiating browser cookie, an exact same-origin POST and a CSRF proof.
They need no device key and return no polling bearer. The same source handoff installs Google
without a Bridge capability. A later Mac sign-in enrolls its device in the existing account.

`browser.py` stores a hashed, 15-minute `__Host-sotto_session` cookie with Secure, HttpOnly,
SameSite=Lax and Path=/ attributes. The Google callback must present its initiating cookie;
callback state or a copied link alone cannot authenticate another browser. Verified authorization
rotates the cookie and CSRF proof atomically with the handoff. A new OAuth start supersedes an
earlier tab; signing out invalidates a pending callback. A signed-in browser must sign out before
changing Google accounts. Suspension rejects its existing sessions.
Browser/OAuth associations use a separate table, preserving the existing six-column sign-in
session layout for older account-service releases. This preserves their insertion contract; it
does not replace the coordinated gateway/receiver backup and restore checks.

Anonymous Mac starts retain at most 50 unverified pending sessions. The broker retains at most 500
sign-in sessions overall and evicts only the oldest anonymous pending row when capacity is needed;
device-confirmed, provisioning, ready and browser-authenticated work is preserved. These FIFO
bounds prevent cheap anonymous starts from holding every slot for the full TTL;
they are capacity controls, not DDoS protection.

The current account's journey survives session expiry/restarts; reauthentication resumes it.
`GET /v1/journey` derives readiness from the same receiver `/cloud/status` facts used by Bridge.
That metadata-only endpoint accepts the tenant control credential as well as individual Bridge
credentials; control access to status does not authorize Mac consent changes. The broker filters
the response and never exposes tenant control credentials, Google tokens or arbitrary upstream
fields. Handoff acceptance, usable context, messaging activation and first delivery remain separate
facts. An unavailable receiver reports unknown readiness, rather than claiming setup is complete.

`pages.py` renders `/setup` without client-side scripts. Same-origin CSRF-protected forms connect
Google, request a challenge, confirm the observed sender and sign out. API equivalents are
`POST /v1/channel-links` and `POST /v1/channel-links/confirm`. An authenticated account supplies no
tenant ID or arbitrary destination. Setup responses have no-store and no-referrer headers.

This implements browser continuation, not live shared-number onboarding: the future authenticated
gateway supervisor must publish a ready line before any link is offered. There is no public API
for setting that line or submitting claimed provider events. Until transport integration exists,
the page says Messages setup is being prepared. An approved binding alone does not mark delivery
active or send a first brief. Text-first invitation capture, Mac confirmation, provider routing and
signed-app/live verification remain release gates.

## Register a pre-created tenant

On the account-service host, run `python server.py --register-tenant` with a JSON object on stdin:
`tenant`, `origin` (HTTPS origin), `control_token`, and the invited `email`. Keep credentials out of
shell arguments/history. The command encrypts the control credential in the existing database and
prints only registration status and tenant ID. It cannot reassign an existing tenant's admission;
route/credential rotation retains account ownership and suspension state. The original
`SOTTO_ACCOUNT_CONFIG` remains the legacy pilot registration and OAuth/state-key configuration.

Registration is not shared-number enrollment. Until the gateway release gate passes, do not give
another tenant the pilot's Photon project stream or remove the owner guard. No Railway fleet token
belongs in this service.

## Messaging connection contract

`linking.py` implements independent account approval and verified DM possession. A random
12-character Base32 challenge expires after 10 minutes; only its hash and encrypted resumable copy
are stored. A sender gets at most five proof attempts in that window. Groups cannot establish
proof. The intended line is fixed at issuance; a proof on another line is rejected. The first exact
provider sender/conversation is pinned for explicit account confirmation; a different sender
cannot replace it. Email and phone aliases are not inferred. Active sender routes are unique
across accounts and route versions advance on revocation/reconnection. Completed/revoked challenge
payloads are erased; expired payloads are erased on the next challenge start. Older unscoped
pending challenges are revoked during migration.
Rejecting a displayed candidate cancels that challenge and issues a new one; it neither transfers
an existing binding nor lets another sender replace an already displayed proof.

Only trusted transport code may call `observe`; it is deliberately not exposed as an unauthenticated
HTTP route. Browser account sessions now authorize challenge issuance and explicit confirmation;
the messaging gateway still needs to supply authenticated observation and activate the receiver
route. A passed unit test is not a verified live Photon receipt.
