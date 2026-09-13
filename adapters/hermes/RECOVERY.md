# Tenant volume recovery

The shipped container starts through `runtime_lock.py`. It holds `.sotto-runtime.lock` for its
lifetime, refuses a second instance on the same volume, and refuses a restored volume until an
operator reviews and resumes it. `start.sh` supervises the receiver and gateway together: either
essential process exiting stops both process groups and fails the container for platform restart.

## Managed volume migration

Use explicit project, service and environment IDs on every Railway subcommand, and verify returned
identity before any write. In particular, use `railway variable list --project PROJECT_ID --service
SERVICE_ID --environment ENVIRONMENT_ID --json`; flags on the parent `variable` command can be
ignored by the CLI. Never rely on the checkout’s linked default service.

Before deploying this version to an existing managed instance, set `SOTTO_VOLUME_ID` to the actual
attached Railway volume ID. On that mounted volume, explicitly initialize its tenant identity:

```bash
python3 /app/adapters/hermes/managed_volume.py initialize --data /data --tenant TENANT_ID --volume VOLUME_ID
```

Initialization is idempotent for the same identity and refuses a different existing tenant or
volume identity. Normal boot verifies a real mount point, the identity receipt and a durable
write/read probe. It never creates that receipt automatically or silently falls back to ephemeral
storage. Self-host keeps its existing path behavior.

## Export and cold restore

Stop the tenant runtime and its writers first. Run the tool from the same image against the mounted
volume using a maintenance command that does not launch the runtime. The exporter refuses an active
runtime lock and checks for file changes during capture. It includes the full tenant volume:
canonical memory, explicit preferences, source state, credentials, accepted work and pending delivery.
The archive is mode 0600, **not application-encrypted**; store it in access-controlled, encrypted
backup storage. No archive or personal data belongs in the repository.

```bash
python3 /app/adapters/hermes/recovery.py export --data /data --tenant TENANT_ID --archive /backup/tenant.tgz
python3 /app/adapters/hermes/recovery.py restore --data /replacement-data --tenant TENANT_ID --volume NEW_VOLUME_ID --archive /backup/tenant.tgz
```

The archive destination must be new and outside the source volume. The restore destination must be
new or empty. Restore verifies tenant identity, every file hash, SQLite integrity, safe paths and
internal file links before publishing the restored files. External links and directory links are
refused. `--volume` explicitly binds a replacement managed mount to its new volume ID.

Restore prints a content-free manifest receipt and writes `.sotto-recovery-hold.json`. Keep the
replacement stopped while checking the tenant identity, Google connection, canonical memory and
pending work/outbox against that receipt. Reconcile any send whose provider acceptance is uncertain
before resuming: restoring an older snapshot cannot prove what happened afterward. Ensure the old
instance remains stopped. Then activate the reviewed restore:

```bash
python3 /app/adapters/hermes/recovery.py resume --data /replacement-data --tenant TENANT_ID --manifest-sha256 RESTORE_RECEIPT_HASH
```

Start the shipped container with the replacement volume and matching managed configuration.
Existing successful deliveries retain their receipts; pending work retains its original deadlines.
Post-delivery state effects retry five times without resending the accepted message. A permanent
failure is then shown as `effects_failed` in receiver health and retained with the provider receipt
and replayable effect metadata until ordinary outbox retention removes the row.
A provider acceptance receipt proves acceptance, not that the user read the message.

`test_recovery.py` rehearses a cold restore using synthetic explicit memory, credentials, a queued
job and a pending outbox row, including the activation hold. This is an offline tenant-volume
contract, not evidence of a live pilot backup, an automatic backup schedule, or recovery of the
separate accounts/proxy services. Operators must also protect those services' SQLite volumes and
external configuration, including the accounts encryption key and tenant/control token hashes.
Fleet-wide recovery remains a launch gate until that complete drill has been performed.

## Model credential continuity

The receiver renews the configured model bearer's lease through the existing proxy, initially and
within 72 hours of expiry, with hourly retry after failure. The lease extends to 30 days and keeps
the same bearer bytes so running Hermes clients continue to work. This is expiry renewal, not key
rotation. `config/model-lease.json` records only configuration identity, expiry and attempt status.

The proxy tenant entry must contain `renewal_token_sha256`, the SHA-256 of the independent receiver
`SOTTO_CONTROL_TOKEN`. That control credential is excluded from Hermes gateway and unattended worker
environments and from its persisted `.env`; the model credential cannot renew itself. Proxy
`enabled: false` blocks new requests and renewal. Changing the model token hash revokes the old
bearer; rotating bearer bytes still requires coordinated instance configuration and restart.

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
browser sessions. Newly enrolled devices receive no shared setup code; additional web setup needs
an existing browser login or an operator login URL until dashboard session exchange is implemented.
