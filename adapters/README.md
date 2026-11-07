# Adapters

Sotto has one runtime: the supervised Sotto runtime, built by the repository's root `Dockerfile`
(and `runtime/sotto-chat/Dockerfile.runtime` underneath it). The skills tree is host-agnostic.
Nothing under `sotto-chief-of-staff/` imports an agent runtime: it is `SKILL.md` + scripts over
`$SOTTO_DATA`, the Bridge's JSON-RPC tools for the Mac, plain HTTP for the receiver.

For self-host, Google onboarding runs in the receiver's `/setup` browser page using the owner's Web
application OAuth client and the exact HTTPS callback shown there. The callback is browser-bound,
PKCE/state protected, and single-use; it returns automatically to Sotto. The Mac Bridge is
unchanged and self-host does not use a Sotto OAuth broker for this flow.

| Portable core (no host coupling) | Built on |
|---|---|
| **Sotto Bridge** (Mac app) | Mac source reader that dials **out** to the receiver's relay over the reverse link (`/bridge/poll`, `/bridge/respond`); the chat runtime reaches its MCP-shaped JSON-RPC tools through the relay's loopback `/mcp` |
| `sotto-chief-of-staff/` skills + scripts | **agentskills** `SKILL.md` + scripts over `$SOTTO_DATA` |
| `runtime/trigger-receiver/` | plain HTTP: scheduling, accepted work, delivery, the setup wizard and dashboard |
| `contracts/` | JSON Schema + the exhaust file layout |

## `adapters/sotto/`

The receiver's runtime adapter, the root supervisor, the
managed and self-host bootstraps, startup configuration and Google setup. See
[sotto/README.md](sotto/README.md) and [sotto/SELFHOST.md](sotto/SELFHOST.md). It also holds:

| Files | Used for |
|---|---|
| `managed_exec.py` · `control_vault.py` · `runtime_lock.py` | Drop workloads to their UIDs, capture the control credential before runtime imports, hold the tenant lock |
| `tenant_lifecycle.py` · `replacement_epoch.py` | The root supervisor's Linux lifecycle and the replacement epoch fresh routines bind to |
| `model_lease.py` | The receiver heartbeat renews the proxy model lease |
| `crons.json` | The one system schedule (below); the receiver reads it directly |
| `photon_owner.py` | Photon owner policy and the activation receipt the receiver writes |
| `.env.template` | The Railway service variables the setup helper writes (everything else: RAILWAY.md) |
| `VOLUME.md` | Tenant volume operations |

**The cron schedule has ONE source: `adapters/sotto/crons.json`**.
It is an array of `{name, schedule, prompt, skill}` plus two optional keys — `gate`, the env var
that must not be `0` for the job to run (`SOTTO_PROACTIVE`, `SOTTO_DIGEST`), and `schedule_env`, an
env var that overrides the schedule (`SOTTO_PROACTIVE_CRON`). `receiver._cron_tick` fires every row
on the receiver's own 60-second heartbeat,
so a scheduled run takes the identical spawn → outbox → deliver-once path a wake-push brief takes
(retries, a receipt, one brief per day, and the `NO_NUDGES` silence seam). It reads fixed daily
`M H * * *`, weekly and `*/N * * * *` interval schedules and the configured zone every tick, so a
timezone change moves the next fire with nothing to re-register. Delivery is not per job:
`SOTTO_CRON_DELIVER` is the one target for all of them.

**The `user-` namespace is reserved.** Routines named `user-<slug>` are the owner's personal
routines (the `sotto-routines` skill); they never appear in `crons.json`.

**Rule:** if a change would only make sense on one runtime boundary, it belongs in `adapters/`,
never in the core.
