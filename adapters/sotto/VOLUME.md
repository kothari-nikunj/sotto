# Tenant volume operations

There is no tenant export, cold restore or role-repair path, and no migration from an older
volume. A lost or damaged volume is replaced by a fresh installation (`prepare_fresh.py`, then the
root supervisor). The supervisor keeps `.sotto-runtime.lock` for its lifetime and refuses a second
consumer on the same volume.

Before either runtime role starts, the root supervisor publishes a private
`.sotto-epoch.json`. It records the replacement epoch that fresh routines bind to.
A later fresh start refuses that file, including malformed files and dangling links; the
supervisor instead re-enters the same epoch after verifying its identity fields (tenant, volume
marker, profile, receiver owner/line identity and tenant directory inode, not `st_dev`). Rotated
credentials, model or timezone overrides and a release's re-rendered config are re-staged and keep
the same epoch; a changed identity, or a record from an older epoch version, is refused and never
cleared or upgraded automatically. The epoch is not a
provider acceptance receipt, and the on-volume marker does not prove a persistent mount: managed
deployment must verify its attachment independently.

## Model credential continuity

The receiver renews the configured model bearer's lease through the existing proxy, initially and
within 72 hours of expiry, with hourly retry after failure. The lease extends to 30 days and keeps
the same bearer bytes so running clients continue to work. This is expiry renewal, not key
rotation. `config/model-lease.json` records only configuration identity, expiry and attempt status.

The proxy tenant entry must contain `renewal_token_sha256`, the SHA-256 of the independent receiver
`SOTTO_CONTROL_TOKEN`. That control credential is excluded from Node and unattended worker
environments; the model credential cannot renew itself. Proxy `enabled: false` blocks new
requests and renewal. Changing the model token hash revokes the old bearer; rotating bearer bytes
still requires coordinated instance configuration and restart.

## Device access

Managed enrollment issues a bearer per Bridge signing key. The control-authenticated
`POST /cloud/devices` accepts `{"operation":"list"}` or
`{"operation":"revoke","device_id":"DEVICE_ID"}`. Revocation stops future Bridge reads,
responses, event uploads and consent calls; an in-flight long poll rechecks before returning data.
The receiver also checks each Bridge tool response against the server-held pending request and the
current managed source consent. An older Bridge cannot return history for a source that is disabled.
A fresh Google sign-in can re-enroll the device; old grants and bearers remain invalid.

Legacy shared Bridge access remains valid only until the first device credential is issued.
Shared operator credentials are not individually revocable device credentials. This API revokes
Bridge access only: it does not erase historical data, revoke Google consent or terminate existing
browser sessions. Newly enrolled devices receive no shared setup code.

