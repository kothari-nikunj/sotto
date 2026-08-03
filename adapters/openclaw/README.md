# OpenClaw adapter

OpenClaw is an MCP client and embraces the open **agentskills** standard, so the **portable Sotto core
runs on it unchanged** — the Bridge (MCP), the `SKILL.md` skills + Python scripts, the exhaust, and the
trigger receiver are all reused as-is. Only this thin adapter differs from Hermes.

## Install

```bash
./adapters/openclaw/install.sh            # local stdio Bridge (build it first), or
BRIDGE_TOKEN=<token> ./adapters/openclaw/install.sh   # cloud reverse-relay Bridge
```

Override paths/CLI for your OpenClaw build via env: `OPENCLAW_BIN`, `OPENCLAW_HOME`,
`OPENCLAW_SKILLS_DIR`, `OPENCLAW_WORKSPACE`.

## What the core already makes host-agnostic (no per-host code)
- ✅ **Skills** — agentskills-standard `sotto-*` skills run unchanged (but see the validation
  checklist below re: OpenClaw's single-line frontmatter parser and the `metadata.hermes` hint block).
- ✅ **Google** — `_shared/scripts/gather_google.py` is adaptive: google-workspace CLI **or** a
  Gmail/Calendar MCP **or** provisioned creds. No host-specific Google code, no client JSON where Google
  is already connected.
- ✅ **Bridge** — a plain MCP server, in both stdio (local) and reverse-relay (cloud) topologies.
- ✅ **Trigger receiver** — host-neutral; selects the executor via `SOTTO_RUN_SKILL`.

## What `install.sh` does directly (portable, real)
- Copies the `sotto-*` skills into OpenClaw's managed skills dir (`~/.openclaw/skills`). CLI
  alternative: `openclaw skills install ./sotto-chief-of-staff --as sotto`.
- Appends the bundle's operating rules to **`workspace/AGENTS.md`** under a marked `## Sotto` section
  (idempotent). OpenClaw has **no bundle/manifest equivalent** — AGENTS.md (loaded per session) is the
  right surface for "act as Sotto, use the scripts, honor approval tiers".
- Appends the Sotto persona to **`workspace/SOUL.md`** (created if missing; loaded every session) and
  sets the agent name to **Sotto** in **`workspace/IDENTITY.md`** (name/emoji — also brands the
  self-chat reply prefix `[Sotto]`). An existing IDENTITY.md is never clobbered.
- Registers the `sotto-local` Bridge MCP via the CLI — `openclaw mcp add sotto-local --command …`
  (stdio) or `openclaw mcp set sotto-local '<json>'` (HTTP + `Authorization: Bearer` header). OpenClaw
  reads MCP servers from `mcp.servers.<name>` in `~/.openclaw/openclaw.json` (JSON5); if the CLI is
  unavailable, the installer prints the exact JSON5 snippet to merge by hand.

## What still needs YOUR OpenClaw CLI (printed by `install.sh`, not guessed)
These need your timezone + delivery target, so the installer prints them instead of guess-executing:
- **Model** → set OpenClaw's agent model to `gemini-3-flash-preview`.
- **Scheduler** → OpenClaw cron jobs are **prompt-based** (there is **no `--skill` flag**); `--tz`
  takes an IANA zone, and `--announce --channel --to` deliver the result to your chat:
  ```bash
  openclaw cron add "30 6 * * *" "Run the sotto-morning-brief skill" \
    --name sotto-morning-brief --tz America/Los_Angeles --announce --channel whatsapp --to +15551234567
  openclaw cron add "45 16 * * *" "Run the sotto-followup skill" \
    --name sotto-followup --tz America/Los_Angeles --announce --channel whatsapp --to +15551234567
  ```
  (Same shape for `sotto-evening-brief` at `30 17 * * *`, `sotto-relationship-pulse` Mon 9:00, and the
  optional `sotto-proactive` at `*/15`. The `sotto-followup` job at `45 16` is the post-meeting
  follow-up the Hermes installer also creates. The Bridge push fires the real brief; cron is the
  fallback — SPEC §4.1.)
- **Skill-run** → the one-shot runner is `openclaw agent -m "<text>"` (there is **no `openclaw run`**),
  so the trigger receiver takes `SOTTO_RUN_SKILL="openclaw agent -m"`.

## Access control (who may talk to Sotto)
OpenClaw has **no `WHATSAPP_ALLOWED_USERS`-style env vars** — allowlisting lives in
`~/.openclaw/openclaw.json`:

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
Sotto's quality is the deterministic, pinned `sotto-*` skills — on Hermes we pause the curator; OpenClaw
**has no curator**, and there is nothing to "pause". Its control surface is the **Skill Workshop**:
- `skills.workshop.approvalPolicy: "pending"` (the **default**) gates agent-authored skill changes
  behind your approval — so out of the box, the agent can't silently rewrite the sotto skills.
- To disable agent skill-authoring entirely, exclude `skill_workshop` from `tools.allow`.
- Per-agent `agents.list[].skills` allowlists exist if you want an agent restricted to the sotto set.

## Validation checklist (not yet run against a live OpenClaw build)
1. **Frontmatter parsing (top risk):** OpenClaw's frontmatter parser accepts **single-line keys only**;
   the sotto skills' multi-line `metadata.hermes:` blocks may be dropped or fail to parse. Needs a live
   check — if a skill doesn't load, that block is the first suspect. (It's only a Hermes toolset hint —
   do **not** edit the SKILL.md files preemptively; confirm on a real build first.)
2. `openclaw mcp add` / `openclaw mcp set` flags match your build (else merge the printed JSON5).
3. The cron `--announce --channel whatsapp --to` delivery reaches your chat.
4. `openclaw agent -m "Run the sotto-morning-brief skill"` runs the skill end-to-end (receiver path).

## Notes
- The standard `name`/`description` skill fields work as-is; never change the core skills to fit a
  host — transform in `install.sh` if something differs.
- Caveat (design doc §9b): OpenClaw's persistent in-memory state is harder to run off-Mac, so the
  recommended *hosting* for the always-on cloud topology is still Hermes. The Bridge serves both.
