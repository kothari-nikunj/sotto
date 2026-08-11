# OpenClaw adapter

OpenClaw is an MCP client and follows the open **agentskills** standard, so most of the portable
Sotto core runs on it as-is — the Bridge (MCP), the `SKILL.md` skills, the exhaust, and the trigger
receiver are all reused. Two things do **not** carry over unchanged and are rewritten by
`install.sh` at install time (see [Host transform](#host-transform)).

**Validation status:** every CLI command and config shape below was executed against a **live
`openclaw` 2026.7.1-2** (npm `openclaw`, Node ≥ 24.15) running headless. What could not be tested
here is listed under [Still unverified](#still-unverified) — that list is the whole of it.

## Install

```bash
./adapters/openclaw/install.sh            # local stdio Bridge (build it first), or
BRIDGE_TOKEN=<token> ./adapters/openclaw/install.sh   # cloud reverse-relay Bridge
```

Paths follow **OpenClaw's own env vars** — `OPENCLAW_HOME` (the home directory OpenClaw resolves
from; **not** `~/.openclaw`), `OPENCLAW_STATE_DIR` (default `<home>/.openclaw`),
`OPENCLAW_WORKSPACE_DIR` (default `<state>/workspace`). `OPENCLAW_BIN`, `OPENCLAW_SKILLS_DIR` and
`OPENCLAW_AGENT` override the CLI, the managed-skills root and the agent id.

> Changed: earlier versions of this installer used `OPENCLAW_HOME` to mean `~/.openclaw`. That
> collides with OpenClaw's real variable, which means the *home directory*. Set
> `OPENCLAW_STATE_DIR` if your state root is non-standard.

## What the core makes host-agnostic (no per-host code)

- ✅ **Skills load unchanged** — every `sotto-*` skill registers from
  `<state>/skills/sotto/<skill>/SKILL.md` and reports `✓ ready` in `openclaw skills list` (source
  `openclaw-managed`), visible to the model and available as commands. OpenClaw discovers a skill
  wherever a `SKILL.md` appears under a skill root (grouped layouts, ≤ 6 levels deep), so the
  collection layout is supported, and the multi-line `metadata.hermes:` block parses fine — OpenClaw
  parses frontmatter as YAML first and flattens nested `metadata` blocks to JSON5.
- ✅ **Google** — `_shared/scripts/gather_google.py` is adaptive: google-workspace CLI **or** a
  Gmail/Calendar MCP **or** provisioned creds. No host-specific Google code.
- ✅ **Bridge** — a plain MCP server in both topologies. Verified live: the stdio Bridge registers
  and is spawned + probed by `openclaw mcp add`; the cloud reverse relay probes clean and exposes
  its 4 tools to OpenClaw.
- ✅ **Trigger receiver** — host-neutral for briefs (`SOTTO_RUN_SKILL`). Its cron view
  (`_sotto_cron_jobs`) reads the same `adapters/hermes/crons.json` and matches the jobs this
  installer creates on OpenClaw's scheduler, job for job. Set `SOTTO_SKILLS_ROOT` to
  `<state>/skills/sotto` — verified to resolve every script the receiver, dashboard and calendar
  cache shell out to (`triage_event.py`, `poll_gmail.py`, `gather_google.py`, `knowledge_edit.py`,
  `preferences.py`, `style_extract.py`). Without it, briefs still run and the rest degrades quietly.

## Host transform

`install.sh` rewrites two things in the **copied** skills tree (never in the core — the rule is
"transform in the adapter"). Both counts are printed on install:

| Rewrite | Why |
|---|---|
| `$HOME/.hermes/skills/sotto/…` → `<state>/skills/sotto/…` | Every script invocation in the SKILL.md procedures is hard-coded to the Hermes install path, which does not exist here. Without this the skills load and then tell the model to run a path that isn't there. |
| `execute_code` → `exec` | OpenClaw has **no `execute_code` tool**. Its local shell tool is `exec`. (`code_execution` exists but is a *remote xAI sandbox with no access to local files* — the wrong tool.) |

At the time of writing that is ~78 path references and ~61 tool references across the SKILL.md
files; the installer counts and prints the live numbers, so it stays true as the tree changes.
Verified after transform: every skill still loads, and a `python3 "…"` command copied **verbatim**
out of a transformed `SKILL.md` runs against a fixture `$SOTTO_DATA`.

The persona gets the same treatment on the way into `SOUL.md`: the "> Append this block to
`~/.hermes/SOUL.md`" install note is dropped (it is an instruction to the installer, not the agent)
and the "never edit files under `~/.hermes`" guardrail is repointed at this host's state dir.

## What `install.sh` does directly

- Copies the `sotto-*` skills into OpenClaw's shared managed skills root (`<state>/skills/sotto`),
  minus `__pycache__`/`.pytest_cache`. There is **no CLI shortcut**: `openclaw skills install
  ./sotto-chief-of-staff --as sotto` fails with `archive is missing SKILL.md` (git/local installs
  expect a single skill at the source root), and installing skill-by-skill would strip the shared
  `_shared/scripts` the skills call.
- Appends the bundle's operating rules to **`workspace/AGENTS.md`** under a marked `## Sotto`
  section. OpenClaw has **no bundle/manifest equivalent**; AGENTS.md is loaded every session.
  Idempotent — verified across repeat runs.
- Appends the Sotto persona to **`workspace/SOUL.md`** (loaded every session) and sets the agent
  name via `openclaw agents set-identity --agent main --name Sotto --emoji 🎩`, which writes
  `agents.list[].identity` in `openclaw.json` (the authoritative surface; `IDENTITY.md` is the
  human-authored input). The name also brands the WhatsApp self-chat reply prefix
  `[{identity.name}]`. An identity that is already set to something else is reported, never
  clobbered. *(This is a fix: `openclaw setup`/`onboard` always seeds a placeholder `IDENTITY.md`,
  so the old "only write it if the file is missing" test never fired and the name was never set.)*
- Registers the `sotto-local` Bridge MCP under `mcp.servers.<name>` in `<state>/openclaw.json`
  (JSON5 — comments, unquoted keys and trailing commas all verified to parse) via
  `openclaw mcp add … --command … --env …` (stdio) or `openclaw mcp set … '<json>'` (HTTP). Both
  exit non-zero on failure, so the printed JSON5 fallback only fires when registration really
  failed.
  **The HTTP entry must carry `"transport": "streamable-http"`.** With only `{url, headers}`
  OpenClaw defaults to SSE and the probe fails with `SSE error: Non-200 status code (404)` — the
  reverse relay is a `POST /mcp` JSON-RPC endpoint with no SSE stream. With the transport set, the
  same relay probes clean.

## What still needs YOUR OpenClaw CLI (printed by `install.sh`)

- **Model** → `openclaw config set agents.defaults.model google/gemini-3-flash-preview`
  (provider-qualified; `gemini-flash` is OpenClaw's alias for the same model).
- **Scheduler** → OpenClaw cron jobs are **prompt-based** (there is **no `--skill` flag**); `--tz`
  takes an IANA zone, and `--announce --channel --to` deliver the result:
  ```bash
  openclaw cron add "30 6 * * *" "Run my morning brief (use the sotto-morning-brief skill)" \
    --name sotto-morning-brief --declaration-key sotto:sotto-morning-brief \
    --tz America/Los_Angeles --announce --channel whatsapp --to +15551234567
  ```
  **`--declaration-key` is not optional in practice:** cron *names* are not unique on OpenClaw, so
  without it a second run of these commands silently creates a duplicate job (reproduced). With it,
  a re-run updates the existing job in place (reproduced).
  The channel must already be connected (`openclaw channels add`) or the job registers with
  `Unsupported channel` in `openclaw cron list`.
  **The schedule itself is not documented here** — `adapters/hermes/crons.json` is the one source
  every registrar reads, and `SOTTO_CRON_DELIVER` is the single delivery target for all jobs (same
  as Hermes). Gates: `SOTTO_PROACTIVE=0` drops the mostly-silent `*/15` nudge watcher,
  `SOTTO_DIGEST=0` drops the adaptive midday catch-up. No standalone follow-up cron. The Bridge push
  fires the real brief; cron is the fallback — SPEC §4.1.
- **Skill-run** → `openclaw agent --agent main -m "<text>"`. A session selector
  (`--agent` / `--to` / `--session-key` / `--session-id`) is **required** — a bare
  `openclaw agent -m "<text>"` always errors with `No target session selected`, and the receiver
  spawns fire-and-forget, so it would fail *silently*. Add `--deliver` to push the reply to the
  session's channel. There is no `openclaw run`.
- **Receiver env** → `SOTTO_SKILLS_ROOT=<state>/skills/sotto`, and in the cloud topology
  `SOTTO_MCP_TOKEN=<the same BRIDGE_TOKEN>` — the receiver's relay reads `SOTTO_MCP_TOKEN`, not
  `BRIDGE_TOKEN`, and without it every OpenClaw `/mcp` call gets `{"error": "unauthorized"}`
  (reproduced).

## Access control (who may talk to Sotto)

OpenClaw has **no `WHATSAPP_ALLOWED_USERS`-style env vars** — allowlisting lives in
`<state>/openclaw.json`. Verified against the live config schema
(`channels.whatsapp.dmPolicy` ∈ `pairing|allowlist|open|disabled`, default `pairing`):

```json5
channels: {
  whatsapp: {
    dmPolicy: "allowlist",
    allowFrom: ["+15551234567"],   // E.164, with the +
  },
  telegram: {
    botToken: "…",                 // or the TELEGRAM_BOT_TOKEN env fallback
  },
}
```

## Skill isolation (protecting Sotto's pinned pipeline)

Sotto's quality is the deterministic, pinned `sotto-*` skills. OpenClaw **does** have a curator
(`openclaw skills curator`) — it stales skills unused for 30 days and archives them at 90 — but it
only touches skills created by Skill Workshop proposals; **manually authored skills are never
curated**, and the sotto tree is copied in by this installer, so there is nothing to pause. The
control surfaces that matter:

- `skills.workshop.approvalPolicy: "pending"` (the **default**) gates agent-authored skill changes
  behind your approval, so the agent can't silently rewrite the sotto skills.
- To disable agent skill-authoring entirely, keep `skill_workshop` out of the active `tools.allow`.
- Per-agent `agents.list[].skills` allowlists exist if you want an agent restricted to the sotto set.
- `openclaw skills curator pin <skill>` exists as a belt if you ever hand a sotto skill to Workshop.

## Still unverified

Everything above was executed live. These four could not be, and why:

1. **Cron delivery actually reaching a chat** (`--announce --channel whatsapp --to …`) — needs a
   linked WhatsApp/Telegram account; the lab had no channel, so jobs register with
   `Unsupported channel`. The job creation, schedule and IANA zone were verified.
2. **A full brief end-to-end through `openclaw agent`** — needs a model API key. The invocation was
   verified to reach the model layer (it fails only on `No API key found for provider`).
3. **Google via OpenClaw's Gmail/Calendar MCP** (the SKILL.md `--from-mcp-*` fallback branch) —
   needs a connected Google account.
4. **macOS specifics** — the stdio Bridge binary and `SOTTO_CHAT_DB=~/Library/Messages/chat.db`;
   the lab is Linux, so the stdio registration was proven with a stand-in MCP server that logged
   its argv and env (both arrived correctly).

## Known gaps in the core, not in this adapter

These are *not* fixed by `install.sh` and will surface on OpenClaw (reported, not patched here):

- **`sotto-setup`, `sotto-routines` and `sotto-relationship-pulse` print `hermes cron
  create … --skill … --deliver <chan>` commands.** None of that exists on OpenClaw (`cron add`, no
  `--skill`, and `--deliver` is a deprecated *boolean*). The "Sotto, set up" flow's scheduling step
  and the whole personal-routines skill are broken on this host until those SKILL.md files learn a
  host-neutral cron form.
- **`receiver._reregister_sotto_crons()` shells out to `hermes cron remove/create` and
  `hermes config set timezone`.** On OpenClaw those subprocesses fail and the exceptions are
  swallowed, so the first-night timezone re-registration is a silent no-op. `_sotto_cron_jobs()`
  itself is host-neutral and correct.
- **`morning-brief/SKILL.md` names a `terminal` tool as the fallback transport.** OpenClaw has no
  `terminal` tool either; after the `execute_code → exec` transform the primary transport works, so
  the fallback branch is dead rather than harmful.

## Notes

- The standard `name`/`description` skill fields work as-is; never change the core skills to fit a
  host — transform in `install.sh`, as above.
- Caveat (design doc §9b): OpenClaw's persistent in-memory state is harder to run off-Mac, so the
  recommended *hosting* for the always-on cloud topology is still Hermes. The Bridge serves both.
