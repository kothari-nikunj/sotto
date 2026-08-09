# Host adapters

**Which host should you pick?** (full setup guides linked)

| Host | Setup | Cost | Always-on | Guide |
|---|---|---|---|---|
| **Cloud Hermes (Railway)** | ~10 steps, ~15 min | hosting bill | yes — fires even with the Mac asleep | [ONBOARDING.md](../ONBOARDING.md) · [RAILWAY.md](../RAILWAY.md) |
| **Local Hermes (Mac)** | `install.sh`, ~5 min | $0 | only while the Mac is awake | [LOCAL-SETUP.md](../LOCAL-SETUP.md) |
| **OpenClaw** | `adapters/openclaw/install.sh` + wire 3 CLI lines | depends | depends | [openclaw/README.md](openclaw/README.md) |

The **Sotto backend is host-agnostic.** The portable core has zero dependency on any specific
agent runtime — it's all open standards:

| Portable core (no host coupling) | Built on |
|---|---|
| **Sotto Bridge** (Mac app) | **MCP** server (stdio + HTTP) — any MCP client |
| `sotto-chief-of-staff/` skills + scripts | **agentskills** `SKILL.md` + `execute_code` over `$SOTTO_DATA` |
| `runtime/trigger-receiver/` | plain HTTP; host-neutral for briefs (`$SOTTO_RUN_SKILL`). The setup wizard, dashboard writes, and the event funnel additionally locate the skills tree via `$SOTTO_SKILLS_ROOT` or the Hermes layout |
| `contracts/` | JSON Schema + the exhaust file layout |

A **host adapter** is the thin, swappable glue that wires the core into one runtime. Nothing in the
core imports or assumes a host; everything host-specific lives here:

| Adapter file | What it covers | Hermes | OpenClaw |
|---|---|---|---|
| bundle file | "load these skills under one slash command" | `sotto.bundle.yaml` (skill-bundle) | no bundle equivalent — the bundle's `instruction` goes into `workspace/AGENTS.md` (`## Sotto`) |
| persona | name the chief-of-staff identity | `sotto-persona.md` → `~/.hermes/SOUL.md` | `sotto-persona.md` → `workspace/SOUL.md` (+ name in `workspace/IDENTITY.md`) |
| config template | model + `mcp_servers` + scheduler | `config.template.yaml` | `~/.openclaw/openclaw.json` (JSON5) |
| MCP registration | register `sotto-local` | `configure_mcp.py` (writes config.yaml) | `openclaw mcp add`/`mcp set` (JSON5 snippet printed as fallback) |
| scheduler / cron | the fallback timer | `hermes cron create … --skill` | `openclaw cron add "<cron>" "<prompt>"` (prompt-based, no `--skill`) — printed by `install.sh` |

**The cron schedule has ONE source: `adapters/hermes/crons.json`** (host-neutral despite living under
`hermes/`). It is an array of `{name, schedule, prompt, skill}` plus two optional keys — `gate`, the
env var that must not be `0` for the job to register (`SOTTO_PROACTIVE`, `SOTTO_DIGEST`), and
`schedule_env`, an env var that overrides the schedule (`SOTTO_PROACTIVE_CRON`). Delivery is *not*
per job: `SOTTO_CRON_DELIVER` is the one target for all of them. Four registrars read this file —
`adapters/hermes/start.sh` (cloud boot), both `install.sh`s, and `receiver._sotto_cron_jobs` (the
timezone re-registration) — so a schedule can never drift between them.

**The `user-` namespace is reserved and off-limits to every registrar.** Crons named `user-<slug>`
are the owner's personal routines (the `sotto-routines` skill); they never appear in `crons.json`,
boot cleanup skips them by prefix, and the timezone re-registration drops them. A host adapter that
adds its own scheduler wiring must keep that fence: touch only the jobs `crons.json` names.
| skill-run command | how the trigger receiver runs a skill (one-shot: prompt in, final text out) | `SOTTO_RUN_SKILL="hermes -z"` | `SOTTO_RUN_SKILL="openclaw agent -m"` |
| installer | one command to wire it all | `adapters/hermes/install.sh` | `adapters/openclaw/install.sh` (validated end to end against a live OpenClaw, Aug 2026 — the short "Still unverified" list is in [openclaw/README.md](openclaw/README.md)) |

**Rule:** if a change would only make sense on one runtime, it belongs in `adapters/<host>/`, never in
the core. Adding a new host = a new `adapters/<host>/`, not a fork.
