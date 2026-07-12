#!/usr/bin/env bash
# OpenClaw adapter installer — mirrors adapters/hermes/install.sh. The PORTABLE Sotto core
# (../../sotto-chief-of-staff skills + scripts, ../../sotto-bridge, ../../runtime/trigger-receiver) is
# host-agnostic and runs on OpenClaw unchanged; only this thin glue differs from Hermes.
#
# HONEST SCOPE: written against OpenClaw's live docs — config is ~/.openclaw/openclaw.json (JSON5,
# MCP servers under mcp.servers.<name>), the persona/rules live in the agent WORKSPACE
# (~/.openclaw/workspace/{SOUL,IDENTITY,AGENTS}.md, loaded every session), cron jobs are PROMPT-based
# (`openclaw cron add`, no --skill flag), and the one-shot runner is `openclaw agent -m`. Not yet
# validated against a live build — see the README's validation checklist. Set OPENCLAW_BIN /
# OPENCLAW_HOME if yours differ.
set -euo pipefail

DRY_RUN=0
OPENCLAW_BIN="${OPENCLAW_BIN:-openclaw}"            # the OpenClaw CLI on PATH
OPENCLAW_HOME="${OPENCLAW_HOME:-$HOME/.openclaw}"   # OpenClaw's config home (openclaw.json, skills, workspace)
SKILLS_DIR="${OPENCLAW_SKILLS_DIR:-$OPENCLAW_HOME/skills}"
WORKSPACE="${OPENCLAW_WORKSPACE:-$OPENCLAW_HOME/workspace}"  # agent workspace — SOUL/IDENTITY/AGENTS, loaded every session
BRIDGE_TOKEN="${BRIDGE_TOKEN:-${SOTTO_BRIDGE_TOKEN:-}}"
RELAY_PORT="${SOTTO_TRIGGER_PORT:-8787}"
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

# 1) Portable skills — copy the agentskills-standard sotto-* skills into OpenClaw's managed skills dir
#    (~/.openclaw/skills is a real managed location). CLI alternative if you prefer OpenClaw to manage
#    the install: `openclaw skills install ./sotto-chief-of-staff --as sotto`. These skills are
#    vendor-neutral — but see the README's validation checklist: OpenClaw's frontmatter parser is
#    single-line-key only, so the skills' multi-line `metadata.hermes:` hint block needs a live check.
note "install skills → $SKILLS_DIR/sotto   (or: $OPENCLAW_BIN skills install $ROOT/sotto-chief-of-staff --as sotto)"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$SKILLS_DIR"
  rm -rf "$SKILLS_DIR/sotto"
  cp -a "$ROOT/sotto-chief-of-staff" "$SKILLS_DIR/sotto"
fi

# 2) Operating rules — OpenClaw has NO bundle/manifest equivalent (a Hermes skill-bundle YAML dropped
#    into a directory does nothing here). The equivalent surface is workspace/AGENTS.md: per-session
#    operating rules. Append the bundle's `instruction` block under a marked "## Sotto" section,
#    idempotently (skip if the marker already exists).
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

# 3) Persona — belongs in the agent WORKSPACE, not $OPENCLAW_HOME: workspace/SOUL.md is the persona
#    file OpenClaw loads every session. Additive + idempotent (keep OpenClaw general otherwise).
SOUL_FILE="$WORKSPACE/SOUL.md"
note "append persona → $SOUL_FILE (idempotent)"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$WORKSPACE"
  touch "$SOUL_FILE"
  grep -q "chief-of-staff mode called \*\*Sotto\*\*" "$SOUL_FILE" 2>/dev/null || \
    cat "$HERE/../hermes/sotto-persona.md" >> "$SOUL_FILE"
fi
# Agent name — workspace/IDENTITY.md holds the agent's name/emoji and also brands the self-chat reply
# prefix (`[{identity.name}]`), so setting it to "Sotto" is what makes replies arrive as [Sotto].
# Create it if missing; if you already have one, we don't clobber it — rename it yourself.
IDENTITY_FILE="$WORKSPACE/IDENTITY.md"
if [ ! -f "$IDENTITY_FILE" ]; then
  note "set agent name → $IDENTITY_FILE (name: Sotto)"
  [ "$DRY_RUN" -eq 0 ] && printf '# IDENTITY\n\nname: Sotto\nemoji: 🎩\n' > "$IDENTITY_FILE"
elif ! grep -qi "sotto" "$IDENTITY_FILE" 2>/dev/null; then
  echo "! $IDENTITY_FILE exists with a different identity — set its name to \"Sotto\" yourself if you"
  echo "  want replies branded [Sotto] (IDENTITY.md's name also prefixes self-chat replies)."
fi

# 4) Bridge MCP — OpenClaw reads MCP servers from mcp.servers.<name> in $OPENCLAW_HOME/openclaw.json
#    (JSON5) — NOT a Hermes-style config.yaml, so the shared configure_mcp.py doesn't apply here.
#    Prefer the CLI (`openclaw mcp add` for stdio, `openclaw mcp set` for HTTP+bearer — both transports
#    are supported upstream); if the CLI is unavailable or errors, print the exact JSON5 to merge.
OPENCLAW_JSON="$OPENCLAW_HOME/openclaw.json"
if [ -n "$BRIDGE_TOKEN" ]; then
  RELAY_URL="http://127.0.0.1:$RELAY_PORT/mcp"
  MCP_JSON="{\"url\":\"$RELAY_URL\",\"headers\":{\"Authorization\":\"Bearer $BRIDGE_TOKEN\"}}"
  note "register sotto-local MCP (reverse relay: $RELAY_URL)"
  if [ "$DRY_RUN" -eq 1 ]; then
    note "$OPENCLAW_BIN mcp set sotto-local '<json with url + Bearer header>'"
  elif [ "$HAVE_CLI" -eq 1 ] && "$OPENCLAW_BIN" mcp set sotto-local "$MCP_JSON"; then
    note "registered via '$OPENCLAW_BIN mcp set'"
  else
    echo "! CLI registration failed — merge this into $OPENCLAW_JSON (JSON5) under mcp.servers:"
    cat <<EOF
    mcp: {
      servers: {
        "sotto-local": {
          url: "$RELAY_URL",
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
      note "registered via '$OPENCLAW_BIN mcp add'"
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
#    --tz takes an IANA zone; --announce --channel --to deliver the result to your chat.
echo
echo "== Wire these with the OpenClaw CLI (then you're done) =="
echo "  • Model:     set the agent model to gemini-3-flash-preview (1M ctx) in openclaw.json."
echo "  • Scheduler: create the fallback crons (the Bridge push fires the real brief). Prompt-based —"
echo "    no --skill flag; --tz is an IANA zone (e.g. America/Los_Angeles):"
echo "      $OPENCLAW_BIN cron add \"30 6 * * *\"   \"Run the sotto-morning-brief skill\"      --name sotto-morning-brief      --tz <zone> --announce --channel whatsapp --to <your-number>"
echo "      $OPENCLAW_BIN cron add \"30 17 * * *\"  \"Run the sotto-evening-brief skill\"      --name sotto-evening-brief      --tz <zone> --announce --channel whatsapp --to <your-number>"
echo "      $OPENCLAW_BIN cron add \"0 9 * * 1\"    \"Run the sotto-relationship-pulse skill\" --name sotto-relationship-pulse --tz <zone> --announce --channel whatsapp --to <your-number>"
echo "      $OPENCLAW_BIN cron add \"*/15 * * * *\" \"Run the sotto-proactive skill\"          --name sotto-proactive          --tz <zone>   # optional: mostly-silent nudges, auto-draft never auto-send"
echo "      $OPENCLAW_BIN cron add \"45 16 * * *\"  \"Run the sotto-followup skill\"           --name sotto-followup           --tz <zone> --announce --channel whatsapp --to <your-number>   # post-meeting follow-up (silent when nothing's actionable)"
echo "  • Trigger receiver (host-neutral): the one-shot runner is \`$OPENCLAW_BIN agent -m \"<text>\"\`"
echo "    (there is no \`openclaw run\`), so:"
echo "      SOTTO_RUN_SKILL='$OPENCLAW_BIN agent -m' SOTTO_DATA=/data python3 $ROOT/runtime/trigger-receiver/receiver.py"
echo
echo "== Done (portable layer). In OpenClaw chat: 'Sotto, set up'. =="
