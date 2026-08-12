#!/usr/bin/env bash
# OpenClaw adapter installer — mirrors adapters/hermes/install.sh. The PORTABLE Sotto core
# (../../sotto-chief-of-staff skills + scripts, ../../sotto-bridge, ../../runtime/trigger-receiver)
# is host-agnostic; this thin glue is everything OpenClaw needs on top of it.
#
# VALIDATED against a live `openclaw` 2026.7.1-2 (npm `openclaw`, Node >=24.15): every command and
# flag below was executed against a real headless instance — skills registered, crons created on
# OpenClaw's own scheduler, the Bridge MCP probed over both transports. What could NOT be checked
# here (no chat channel, no model key) is listed in the README's "Still unverified" section.
#
# PATHS (OpenClaw's real env model — do not confuse these):
#   OPENCLAW_HOME        = the HOME directory OpenClaw resolves from. It is NOT ~/.openclaw.
#   OPENCLAW_STATE_DIR   = state root (config + managed skills). Default: <home>/.openclaw
#   OPENCLAW_WORKSPACE_DIR = agent workspace (SOUL/IDENTITY/AGENTS.md). Default: <state>/workspace
# Set those (OpenClaw's own vars) and this installer follows them.
set -euo pipefail

DRY_RUN=0
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"            # the OpenClaw CLI on PATH
OC_HOME="${OPENCLAW_HOME:-$HOME}"                   # OpenClaw's effective home (see note above)
STATE_DIR="${OPENCLAW_STATE_DIR:-$OC_HOME/.openclaw}"
SKILLS_DIR="${OPENCLAW_SKILLS_DIR:-$STATE_DIR/skills}"       # shared managed skills root
WORKSPACE="${OPENCLAW_WORKSPACE_DIR:-${OPENCLAW_WORKSPACE:-$STATE_DIR/workspace}}"
AGENT_ID="${OPENCLAW_AGENT:-main}"                  # `openclaw agents list` default agent
BRIDGE_TOKEN="${BRIDGE_TOKEN:-${SOTTO_BRIDGE_TOKEN:-}}"
RELAY_PORT="${SOTTO_TRIGGER_PORT:-8787}"
CRON_DELIVER="${SOTTO_CRON_DELIVER:-whatsapp}"      # ONE delivery target for every job (as on Hermes)
HERE="$(cd "$(dirname "$0")" && pwd)"      # adapters/openclaw
ROOT="$(cd "$HERE/../.." && pwd)"          # project root (portable core)

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --token=*) BRIDGE_TOKEN="${arg#*=}" ;;
    --port=*) RELAY_PORT="${arg#*=}" ;;
  esac
done

run() { echo "+ $*"; [ "$DRY_RUN" -eq 1 ] || "$@"; }
note() { echo "+ $*"; }

echo "== Sotto · OpenClaw adapter (dry-run=$DRY_RUN) =="
HAVE_CLI=1
command -v "$OPENCLAW_BIN" >/dev/null 2>&1 || {
  HAVE_CLI=0
  echo "! OpenClaw CLI '$OPENCLAW_BIN' not on PATH — set OPENCLAW_BIN. (Filesystem steps still run;"
  echo "  CLI steps print the exact config to merge instead.)"
}
# Prefer OpenClaw's OWN answer for the workspace when it can give one (a configured
# agents.defaults.workspace, a non-default profile, or a per-agent workspace all land here).
if [ "$HAVE_CLI" -eq 1 ] && [ -z "${OPENCLAW_WORKSPACE_DIR:-${OPENCLAW_WORKSPACE:-}}" ]; then
  CFG_WS="$("$OPENCLAW_BIN" config get agents.defaults.workspace 2>/dev/null | tail -n 1)"
  case "$CFG_WS" in /*) WORKSPACE="$CFG_WS" ;; esac
fi

# 1) Portable skills — copy the agentskills-standard sotto-* skills into OpenClaw's shared managed
#    skills root. VERIFIED: OpenClaw discovers a skill wherever a SKILL.md appears under a skill root
#    (grouped layouts, up to 6 levels), so all 18 sotto-* skills load from skills/sotto/<skill>/, and
#    `openclaw skills list` reports every one of them "ready" from source `openclaw-managed`.
#    There is NO CLI shortcut for this: `openclaw skills install <dir> --as sotto` installs ONE skill
#    and fails here with "archive is missing SKILL.md" (our tree is a collection, and a per-skill
#    install would also strip the shared _shared/scripts the skills call). Copying is the supported
#    path for a grouped tree.
note "install skills → $SKILLS_DIR/sotto"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$SKILLS_DIR"
  rm -rf "$SKILLS_DIR/sotto"
  cp -a "$ROOT/sotto-chief-of-staff" "$SKILLS_DIR/sotto"
  find "$SKILLS_DIR/sotto" -type d \( -name __pycache__ -o -name .pytest_cache \) \
    -prune -exec rm -rf {} + 2>/dev/null || true
fi

# 1b) HOST TRANSFORM (the adapter's job — never edit the core skills to fit a host). The SKILL.md
#     procedures are written for the Hermes layout and Hermes' tool names; two mechanical rewrites
#     make them true on OpenClaw. Both are applied to the COPY only:
#       • script paths  `$HOME/.hermes/skills/sotto/…`  →  this install's skills dir
#       • tool name     `execute_code`                  →  `exec`  (OpenClaw's local shell tool;
#         `execute_code` does not exist here, and OpenClaw's `code_execution` is a REMOTE xAI sandbox
#         with no access to local files — the wrong tool entirely.)
if [ "$DRY_RUN" -eq 0 ]; then
  HITS_PATH=$(grep -ro 'HOME/\.hermes/skills/sotto' --include=SKILL.md "$SKILLS_DIR/sotto" | wc -l)
  HITS_TOOL=$(grep -ro 'execute_code' --include=SKILL.md "$SKILLS_DIR/sotto" | wc -l)
  # sed -i.bak + delete works on BOTH GNU sed and macOS/BSD sed (bare `-i` does not).
  find "$SKILLS_DIR/sotto" -name SKILL.md -exec sed -i.bak \
      -e "s|\$HOME/\.hermes/skills/sotto|$SKILLS_DIR/sotto|g" -e 's|execute_code|exec|g' {} +
  find "$SKILLS_DIR/sotto" -name 'SKILL.md.bak' -delete
  note "host transform: $HITS_PATH script paths → $SKILLS_DIR/sotto, $HITS_TOOL execute_code → exec"
else
  note "host transform: rewrite \$HOME/.hermes/skills/sotto → $SKILLS_DIR/sotto and execute_code → exec in the copied SKILL.md files"
fi

# 2) Operating rules — OpenClaw has NO bundle/manifest equivalent (a Hermes skill-bundle YAML dropped
#    into a directory does nothing here). The equivalent surface is workspace/AGENTS.md: per-session
#    operating rules. Append the bundle's `instruction` block under a marked "## Sotto" section,
#    idempotently (skip if the marker already exists). VERIFIED: re-running leaves exactly one.
AGENTS_FILE="$WORKSPACE/AGENTS.md"
note "append operating rules → $AGENTS_FILE (idempotent, '## Sotto' section)"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$WORKSPACE"
  touch "$AGENTS_FILE"
  if ! grep -q '^## Sotto' "$AGENTS_FILE" 2>/dev/null; then
    {
      printf '\n## Sotto\n\n'
      # Extract the bundle's `instruction: |` block (host-neutral operating rules) from the YAML.
      awk '/^instruction: \|/{f=1;next} f && !/^  / && NF{f=0} f{sub(/^  /,"");print}' \
        "$HERE/../hermes/sotto.bundle.yaml"
    } >> "$AGENTS_FILE"
  fi
fi

# 3) Persona — workspace/SOUL.md is the persona file OpenClaw loads every session. Additive +
#    idempotent (keep OpenClaw general otherwise). Two host transforms on the way in: drop the
#    file's "> Append this block to ~/.hermes/SOUL.md" install note (it is instructions to the
#    installer, not to the agent) and repoint the "never edit files under ~/.hermes" guardrail at
#    this host's state dir.
SOUL_FILE="$WORKSPACE/SOUL.md"
note "append persona → $SOUL_FILE (idempotent)"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$WORKSPACE"
  touch "$SOUL_FILE"
  grep -q "chief-of-staff mode called \*\*Sotto\*\*" "$SOUL_FILE" 2>/dev/null || \
    grep -v '^> ' "$HERE/../hermes/sotto-persona.md" \
      | sed -e "s|~/\.hermes|$STATE_DIR|g" -e 's|hermes …|openclaw …|g' >> "$SOUL_FILE"
fi

# 3b) Agent name — `openclaw agents set-identity` is the authoritative surface (it writes
#     agents.list[].identity in openclaw.json); workspace/IDENTITY.md is the human-authored input.
#     The name also brands the self-chat reply prefix `[{identity.name}]`, so this is what makes
#     replies arrive as [Sotto]. NOTE: `openclaw setup/onboard` ALWAYS seeds a placeholder
#     IDENTITY.md, so "does the file exist" is never the right test — test for a real name.
#     (agents.list[0] is the default agent on a single-agent install — the documented setup. On a
#     multi-agent host, set OPENCLAW_AGENT and run set-identity yourself if this reads the wrong one.)
if [ "$HAVE_CLI" -eq 1 ]; then
  CUR_NAME="$("$OPENCLAW_BIN" config get "agents.list[0].identity.name" 2>/dev/null | tail -n 1 || true)"
  case "$CUR_NAME" in
    Sotto) note "agent identity already Sotto — unchanged" ;;
    ""|*"not found"*)
      run "$OPENCLAW_BIN" agents set-identity --agent "$AGENT_ID" --name Sotto --emoji 🎩 ;;
    *)
      echo "! agent '$AGENT_ID' is named '$CUR_NAME' — left alone. For replies branded [Sotto]:"
      echo "  $OPENCLAW_BIN agents set-identity --agent $AGENT_ID --name Sotto --emoji 🎩" ;;
  esac
else
  note "set agent name: $OPENCLAW_BIN agents set-identity --agent $AGENT_ID --name Sotto --emoji 🎩"
fi

# 4) Bridge MCP — OpenClaw reads MCP servers from mcp.servers.<name> in <state>/openclaw.json
#    (JSON5 — comments/unquoted keys/trailing commas all VERIFIED to parse), so the shared
#    configure_mcp.py (which writes a Hermes config.yaml) does not apply here.
#    `openclaw mcp set` for HTTP, `openclaw mcp add` for stdio; both exit non-zero on failure, so the
#    printed-JSON5 fallback below only fires when the CLI genuinely could not register.
OPENCLAW_JSON="$STATE_DIR/openclaw.json"
if [ -n "$BRIDGE_TOKEN" ]; then
  RELAY_URL="http://127.0.0.1:$RELAY_PORT/mcp"
  # transport is NOT optional: with only {url, headers} OpenClaw picks SSE and the probe fails with
  # "SSE error: Non-200 status code (404)" — the reverse relay is a POST /mcp JSON-RPC endpoint with
  # no SSE stream. With "streamable-http" the same relay probes clean (4 Bridge tools). VERIFIED.
  MCP_JSON="{\"url\":\"$RELAY_URL\",\"transport\":\"streamable-http\",\"headers\":{\"Authorization\":\"Bearer $BRIDGE_TOKEN\"}}"
  note "register sotto-local MCP (reverse relay, streamable-http: $RELAY_URL)"
  if [ "$DRY_RUN" -eq 1 ]; then
    note "$OPENCLAW_BIN mcp set sotto-local '<json: url + transport + Bearer header>'"
  elif [ "$HAVE_CLI" -eq 1 ] && "$OPENCLAW_BIN" mcp set sotto-local "$MCP_JSON"; then
    note "registered via '$OPENCLAW_BIN mcp set'"
  else
    echo "! CLI registration failed — merge this into $OPENCLAW_JSON (JSON5) under mcp.servers:"
    cat <<EOF
    mcp: {
      servers: {
        "sotto-local": {
          url: "$RELAY_URL",
          transport: "streamable-http",
          headers: { Authorization: "Bearer $BRIDGE_TOKEN" },
        },
      },
    },
EOF
  fi
else
  BRIDGE_BIN="$ROOT/sotto-bridge/core/target/release/sotto-bridged"
  if [ -x "$BRIDGE_BIN" ]; then
    note "register sotto-local MCP (stdio: $BRIDGE_BIN)"
    if [ "$DRY_RUN" -eq 1 ]; then
      note "$OPENCLAW_BIN mcp add sotto-local --command $BRIDGE_BIN --env SOTTO_CHAT_DB=\$HOME/Library/Messages/chat.db"
    elif [ "$HAVE_CLI" -eq 1 ] && "$OPENCLAW_BIN" mcp add sotto-local \
           --command "$BRIDGE_BIN" --env "SOTTO_CHAT_DB=$HOME/Library/Messages/chat.db"; then
      note "registered via '$OPENCLAW_BIN mcp add' (it spawns and probes the Bridge before saving)"
    else
      echo "! CLI registration failed — merge this into $OPENCLAW_JSON (JSON5) under mcp.servers:"
      cat <<EOF
    mcp: {
      servers: {
        "sotto-local": {
          command: "$BRIDGE_BIN",
          env: { SOTTO_CHAT_DB: "$HOME/Library/Messages/chat.db" },
        },
      },
    },
EOF
    fi
  else
    echo "! sotto-local not registered. LOCAL: build the Bridge ((cd \"$ROOT/sotto-bridge/core\" && cargo build --release)); CLOUD: set BRIDGE_TOKEN."
  fi
fi

# 5) Google — host-agnostic, nothing to do here. gather_google.py uses OpenClaw's Gmail/Calendar MCP
#    (or a google-workspace CLI if present); see _shared/scripts/gather_google.py. No client JSON needed
#    where Google is already connected to OpenClaw.
note "Google: connect Gmail/Calendar to OpenClaw (MCP) — the brief auto-detects it. No extra Sotto step."

# 6) Host-specific bits — PRINTED for you to confirm/run (they need YOUR zone + delivery target).
#    OpenClaw cron jobs are PROMPT-based: there is no --skill flag, so the prompt names the skill.
echo
echo "== Wire these with the OpenClaw CLI (then you're done) =="
echo "  • Model:     $OPENCLAW_BIN config set agents.defaults.model google/gemini-3-flash-preview"
echo "               (provider-qualified id; 'gemini-flash' is OpenClaw's alias for the same model)"
echo "  • Scheduler: create the fallback crons (the Bridge push fires the real brief). Prompt-based —"
echo "    no --skill flag; --tz is an IANA zone (e.g. America/Los_Angeles); --declaration-key makes"
echo "    re-running these UPDATE the job instead of adding a duplicate (without it a second run"
echo "    silently creates a second job — cron names are not unique on OpenClaw). Schedules come from"
echo "    the ONE source every registrar reads, adapters/hermes/crons.json (host-neutral despite the"
echo "    path); delivery is not per job — SOTTO_CRON_DELIVER ($CRON_DELIVER) is the one target:"
# Prompt-based host: OpenClaw has no --skill flag, so crons.json's `skill` is folded INTO the prompt
# ("<prompt> (use the <skill> skill)"). Gates (SOTTO_PROACTIVE / SOTTO_DIGEST) honored as on Hermes,
# and --announce --channel is the OpenClaw spelling of Hermes' `--deliver $SOTTO_CRON_DELIVER`.
python3 -c 'import json, os, sys
cli, spec, deliver = sys.argv[1], sys.argv[2], sys.argv[3]
for j in json.load(open(spec)):
    if j.get("gate") and os.environ.get(j["gate"], "1") != "1":
        continue
    sched = os.environ.get(j.get("schedule_env") or "", "") or j["schedule"]
    print("      %s cron add \"%s\" \"%s (use the %s skill)\" --name %s --declaration-key sotto:%s "
          "--tz <zone> --announce --channel %s --to <your-number>"
          % (cli, sched, j["prompt"], j["skill"], j["name"], j["name"], deliver))' \
  "$OPENCLAW_BIN" "$ROOT/adapters/hermes/crons.json" "$CRON_DELIVER"
echo "    (The channel must already be connected — 'openclaw channels add' — or cron list shows the"
echo "     job with 'Unsupported channel'. sotto-proactive is a mostly-silent watcher that drafts and"
echo "     never auto-sends; sotto-midday-digest is adaptive and stays quiet on a light day; both"
echo "     still register with the same delivery target Hermes gives them. No sotto-followup cron:"
echo "     post-meeting follow-ups run inside the evening brief — the sotto-followup skill stays"
echo "     installed for on-demand use. Same schedule as the Hermes adapter.)"
echo "  • Trigger receiver (host-neutral): the one-shot runner is"
echo "    \`$OPENCLAW_BIN agent --agent $AGENT_ID -m \"<text>\"\` — a session selector is REQUIRED"
echo "    (\`--agent\`/\`--to\`/\`--session-key\`/\`--session-id\`); a bare \`agent -m\` always errors with"
echo "    \"No target session selected\", and the receiver spawns fire-and-forget, so it would fail"
echo "    silently. Add --deliver if you want the reply pushed to the session's channel:"
if [ -n "$BRIDGE_TOKEN" ]; then
  echo "      SOTTO_RUN_SKILL='$OPENCLAW_BIN agent --agent $AGENT_ID -m' \\"
  echo "      SOTTO_SKILLS_ROOT='$SKILLS_DIR/sotto' SOTTO_MCP_TOKEN='<BRIDGE_TOKEN>' SOTTO_DATA=/data \\"
  echo "      python3 $ROOT/runtime/trigger-receiver/receiver.py"
  echo "    (SOTTO_MCP_TOKEN must be the SAME bearer registered above, or the relay 401s every"
  echo "     OpenClaw /mcp call — the receiver reads SOTTO_MCP_TOKEN, not BRIDGE_TOKEN.)"
else
  echo "      SOTTO_RUN_SKILL='$OPENCLAW_BIN agent --agent $AGENT_ID -m' \\"
  echo "      SOTTO_SKILLS_ROOT='$SKILLS_DIR/sotto' SOTTO_DATA=/data \\"
  echo "      python3 $ROOT/runtime/trigger-receiver/receiver.py"
fi
echo "    (SOTTO_SKILLS_ROOT is what makes triage, the Gmail poll, the calendar cache and the"
echo "     dashboard's /api writes find the skills tree on a non-Hermes layout.)"
echo
echo "== Done (portable layer). In OpenClaw chat: 'Sotto, set up'. =="
echo "   Heads-up: the sotto-setup / sotto-routines / sotto-relationship-pulse skills print \`hermes cron\`"
echo "   commands for scheduling — on this host use the \`$OPENCLAW_BIN cron\` lines above."
