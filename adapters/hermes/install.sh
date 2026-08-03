#!/usr/bin/env bash
# Hermes adapter installer (SPEC §5.2/§5.3). Idempotent. Supports --dry-run.
# Wires the PORTABLE Sotto backend (../../sotto-chief-of-staff skills + scripts, ../../sotto-bridge,
# ../../runtime/trigger-receiver) into a Hermes host. OpenClaw has its own adapter (../openclaw).
set -euo pipefail

DRY_RUN=0
BRIDGE_TOKEN="${BRIDGE_TOKEN:-${SOTTO_BRIDGE_TOKEN:-}}"   # the shared bearer (reverse relay)
RELAY_PORT="${SOTTO_TRIGGER_PORT:-8787}"
TAP="${SOTTO_TAP:-sotto-ai/chief-of-staff}"             # hub fallback, used only if the local skills copy is absent
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HERE="$(cd "$(dirname "$0")" && pwd)"      # adapters/hermes
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

echo "== Sotto · Hermes adapter (dry-run=$DRY_RUN) =="

command -v hermes >/dev/null 2>&1 || {
  echo "Hermes not found. Install it first: curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"
  [ "$DRY_RUN" -eq 1 ] || exit 1
}

# 1) Model + scheduler.
#    The brief ALWAYS runs on Gemini via _shared/scripts/compose_brief.py (needs GOOGLE_AI_API_KEY in
#    the env), so we never touch the user's global model — clean drop-in on an existing agent.
#    --dedicated optionally sets the conversational driver to Gemini too (for a Sotto-only instance).
#    Use the NATIVE Gemini model id, same as start.sh — the OpenRouter-style "google/…" id would route
#    via OpenRouter and need OPENROUTER_API_KEY; the native id uses the gemini provider with your
#    GEMINI_API_KEY/GOOGLE_API_KEY.
DEDICATED=0
for a in "$@"; do [ "$a" = "--dedicated" ] && DEDICATED=1; done
[ "$DEDICATED" -eq 1 ] && run hermes config set model gemini-3.6-flash \
  || echo "! Leaving the global model untouched (brief uses Gemini via compose_brief.py + GOOGLE_AI_API_KEY)."
run hermes config set scheduler.enabled true

# 1.5) Gemini key names. Sotto's brief reads GOOGLE_AI_API_KEY, but Hermes' gemini provider reads
#      GEMINI_API_KEY / GOOGLE_API_KEY. Map whichever the user set (in the environment, or already in
#      ~/.hermes/.env) to all three — the SAME mapping as the cloud boot (start.sh) — so the chat/agent
#      driver and compose_brief.py both find the key. Without this, --dedicated sets a Gemini model the
#      driver has no key for.
ENVF="$HERMES_HOME/.env"
upsert_env() {  # replace any existing KEY= line, then append the new value
  grep -v "^$1=" "$ENVF" > "$ENVF.tmp" 2>/dev/null || true
  mv "$ENVF.tmp" "$ENVF"
  printf '%s=%s\n' "$1" "$2" >> "$ENVF"
}
env_file_get() { [ -f "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF" | tail -1 || true; }  # missing file → empty, never fails
GKEY="${GEMINI_API_KEY:-${GOOGLE_API_KEY:-${GOOGLE_AI_API_KEY:-}}}"
[ -n "$GKEY" ] || GKEY="$(env_file_get GEMINI_API_KEY)"
[ -n "$GKEY" ] || GKEY="$(env_file_get GOOGLE_API_KEY)"
[ -n "$GKEY" ] || GKEY="$(env_file_get GOOGLE_AI_API_KEY)"
if [ -n "$GKEY" ]; then
  note "map Gemini key → $ENVF (GOOGLE_AI_API_KEY + GEMINI_API_KEY + GOOGLE_API_KEY)"
  if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$HERMES_HOME" && touch "$ENVF"
    upsert_env GOOGLE_AI_API_KEY "$GKEY"   # Sotto compose_brief.py (AI Studio REST)
    upsert_env GEMINI_API_KEY    "$GKEY"   # Hermes gemini provider (chat/agent model)
    upsert_env GOOGLE_API_KEY    "$GKEY"   # Hermes also accepts this name
  fi
else
  echo "! No Gemini key found (env or $ENVF). Put GOOGLE_AI_API_KEY=<key> in $ENVF (LOCAL-SETUP.md"
  echo "  step 3) or export it, then re-run — the chat model AND the brief need it."
fi

# 2) Portable skills. Prefer the LOCAL copy shipped with this repo (../../sotto-chief-of-staff):
#    copy it to ~/.hermes/skills/sotto — the same shape the Docker image bakes and start.sh refreshes
#    each boot (and the same copy adapters/openclaw does). Only when a local copy is absent do we fall
#    back to tapping the hub (override the tap with SOTTO_TAP) — and a failed tap warns instead of
#    aborting the install (everything else here still works; skills can be added later).
if [ -d "$ROOT/sotto-chief-of-staff" ]; then
  note "install skills (local copy) → $HERMES_HOME/skills/sotto   (hub tap skipped)"
  if [ "$DRY_RUN" -eq 0 ]; then
    mkdir -p "$HERMES_HOME/skills"
    rm -rf "$HERMES_HOME/skills/sotto"
    cp -a "$ROOT/sotto-chief-of-staff" "$HERMES_HOME/skills/sotto"
  fi
else
  run hermes skills tap add "$TAP" || {
    echo "! Skill tap '$TAP' failed — continuing without skills. Fix: use a checkout where"
    echo "  $ROOT/sotto-chief-of-staff exists (preferred), or set SOTTO_TAP to a reachable hub and re-run."
  }
fi

# 3) Hermes bundle → ~/.hermes/skill-bundles/sotto.yaml  (exposes /sotto)
note "install bundle → $HERMES_HOME/skill-bundles/sotto.yaml"
if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$HERMES_HOME/skill-bundles"
  cp "$HERE/sotto.bundle.yaml" "$HERMES_HOME/skill-bundles/sotto.yaml"
fi

# 4) Persona (additive — keep Hermes general)
note "append persona → $HERMES_HOME/SOUL.md (idempotent)"
if [ "$DRY_RUN" -eq 0 ]; then
  touch "$HERMES_HOME/SOUL.md"
  grep -q "chief-of-staff mode called \*\*Sotto\*\*" "$HERMES_HOME/SOUL.md" 2>/dev/null || \
    cat "$HERE/sotto-persona.md" >> "$HERMES_HOME/SOUL.md"
fi

# 5) Bridge MCP — two topologies, auto-selected:
#    CLOUD: BRIDGE_TOKEN set → register sotto-local at the LOCAL reverse relay (tunnel-free); the Mac
#           dials OUT to the receiver, Hermes points at the always-up local endpoint.
#    LOCAL (this Mac is the agent): no BRIDGE_TOKEN → register the built Bridge binary as a STDIO MCP
#           directly, so the user never hand-types `hermes mcp add` with a project-specific path.
if [ -n "$BRIDGE_TOKEN" ]; then
  note "register sotto-local MCP (reverse relay) in $HERMES_HOME/config.yaml"
  if [ "$DRY_RUN" -eq 0 ]; then
    python3 "$HERE/configure_mcp.py" --url "http://127.0.0.1:$RELAY_PORT/mcp" --token "$BRIDGE_TOKEN" \
      --config "$HERMES_HOME/config.yaml"
  else
    echo "+ python3 adapters/hermes/configure_mcp.py --url http://127.0.0.1:$RELAY_PORT/mcp --token *** --config $HERMES_HOME/config.yaml"
  fi
else
  BRIDGE_BIN="$ROOT/sotto-bridge/core/target/release/sotto-bridged"
  if [ -x "$BRIDGE_BIN" ]; then
    note "register sotto-local MCP (stdio: $BRIDGE_BIN) in $HERMES_HOME/config.yaml"
    if [ "$DRY_RUN" -eq 0 ]; then
      python3 "$HERE/configure_mcp.py" --name sotto-local --command "$BRIDGE_BIN" \
        --env "SOTTO_CHAT_DB=$HOME/Library/Messages/chat.db" --config "$HERMES_HOME/config.yaml"
    fi
  else
    echo "! sotto-local not registered. For LOCAL mode, build the Bridge first:"
    echo "    (cd \"$ROOT/sotto-bridge/core\" && cargo build --release)   # → $BRIDGE_BIN"
    echo "  then re-run this installer. For CLOUD mode, set BRIDGE_TOKEN (the bearer you enter in the Mac app)."
  fi
fi

# 6) Google Workspace + Granola — connect with whatever this host supports. The brief is host-agnostic:
#    gather_google.py uses the google-workspace CLI if connected (`hermes setup`), else falls back to a
#    Gmail/Calendar MCP. So EITHER `hermes setup` OR a Google MCP works — no GOOGLE_OAUTH_CLIENT_JSON
#    needed where Google is already connected.
#    Granola is NOT in the Hermes MCP catalog (only linear/n8n/unreal-engine), so there's no
#    `hermes mcp install granola` — register a community Granola MCP as stdio via configure_mcp.py
#    instead (see RAILWAY.md §6c / GRANOLA_MCP_CMD in start.sh).
note "connect Google: 'hermes setup' (CLI) OR register a Gmail/Calendar MCP — either is fine. Granola (optional): a community stdio MCP via configure_mcp.py — see RAILWAY.md §6c."

# 7) Trigger receiver (host-neutral; loopback only). The adapter sets SOTTO_RUN_SKILL.
note "run: SOTTO_RUN_SKILL='hermes -z' SOTTO_TRIGGER_TOKEN=... SOTTO_DATA=/data python3 $ROOT/runtime/trigger-receiver/receiver.py"

# 8) Cron windows (fallback path; the Bridge push fires the real brief — SPEC §4.1). Idempotent: skip
#    a job that's already registered (by name OR prompt) so re-running the installer never piles up
#    duplicates. Stable --name makes them addressable for later edit/remove (parity with the cloud boot).
#    --deliver matters: without it the brief lands in the default "local" sink and never reaches the
#    user — the exact bug the cloud boot fixed. Same default as start.sh; SOTTO_CRON_DELIVER overrides.
SOTTO_CRON_DELIVER="${SOTTO_CRON_DELIVER:-whatsapp}"   # platform-only → uses the gateway home channel
crons="$(hermes cron list 2>/dev/null || true)"
case "$crons" in *sotto-morning-brief*|*"Run my morning brief"*) note "cron sotto-morning-brief exists — skip" ;; *) run hermes cron create "30 6 * * *"  "Run my morning brief"  --skill sotto-morning-brief  --name sotto-morning-brief  --deliver "$SOTTO_CRON_DELIVER" ;; esac
case "$crons" in *sotto-evening-brief*|*"Run my evening brief"*) note "cron sotto-evening-brief exists — skip" ;; *) run hermes cron create "30 17 * * *" "Run my evening brief"  --skill sotto-evening-brief  --name sotto-evening-brief  --deliver "$SOTTO_CRON_DELIVER" ;; esac
case "$crons" in *sotto-relationship-pulse*|*"relationship pulse"*) note "cron sotto-relationship-pulse exists — skip" ;; *) run hermes cron create "0 9 * * 1"   "Run my relationship pulse" --skill sotto-relationship-pulse --name sotto-relationship-pulse --deliver "$SOTTO_CRON_DELIVER" ;; esac
# Proactiveness (mostly-silent ~15-min nudge watcher; auto-draft never auto-send). Default on; SOTTO_PROACTIVE=0 to skip.
if [ "${SOTTO_PROACTIVE:-1}" = "1" ]; then
  case "$crons" in *sotto-proactive*|*"Run my proactive check"*) note "cron sotto-proactive exists — skip" ;; *) run hermes cron create "${SOTTO_PROACTIVE_CRON:-*/15 * * * *}" "Run my proactive check" --skill sotto-proactive --name sotto-proactive --deliver "$SOTTO_CRON_DELIVER" ;; esac
fi
# Post-meeting follow-up (light EVENING cron, 16:45 local; processes only meetings ended since its last
# run, silent when nothing's actionable, never auto-sends). Default on; SOTTO_FOLLOWUP=0 to skip.
if [ "${SOTTO_FOLLOWUP:-1}" = "1" ]; then
  case "$crons" in *sotto-followup*|*"Run my followup"*) note "cron sotto-followup exists — skip" ;; *) run hermes cron create "${SOTTO_FOLLOWUP_CRON:-45 16 * * *}" "Run my followup" --skill sotto-followup --name sotto-followup --deliver "$SOTTO_CRON_DELIVER" ;; esac
fi
echo "NOTE: scheduled briefs deliver via '--deliver $SOTTO_CRON_DELIVER' — they reach you only while the"
echo "      Hermes gateway runs with that channel connected ('hermes gateway'; WhatsApp = scan the QR,"
echo "      Telegram = bot token — 'hermes gateway setup'). Interactive chat ('hermes') needs no channel."
echo "      Set SOTTO_CRON_DELIVER=local (and re-run) only if you deliberately want cron briefs kept in the CLI."

echo "== Done. In chat: '/sotto setup' (or 'Sotto, set up') — it verifies health(), seeds your memory + writing voice, and offers your first brief. =="
