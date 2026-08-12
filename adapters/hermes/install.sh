#!/usr/bin/env bash
# Hermes adapter installer (SPEC §5.2/§5.3). Idempotent. Supports --dry-run.
# Wires the PORTABLE Sotto backend (../../sotto-chief-of-staff skills + scripts, ../../sotto-bridge,
# ../../runtime/trigger-receiver) into a Hermes host. OpenClaw has its own adapter (../openclaw).
#
# Env: BRIDGE_TOKEN     set → CLOUD mode (reverse relay); unset → LOCAL mode (stdio Bridge)
#      SOTTO_BRIDGE_BIN LOCAL mode only — an explicit path to a `sotto-bridged` engine, which wins
#                       over both auto-probed locations (§5). Unset is the normal case.
set -euo pipefail

DRY_RUN=0
BRIDGE_MISSING=0                                          # set by §5 when LOCAL mode finds no engine
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
# scheduler.enabled: Hermes v0.20 dropped the key and warns on it — probe with `config get` and set
# only where supported. (hermes-missing is dry-run only — the guard above exits otherwise — so keep
# the old dry-run output in that case.)
if ! command -v hermes >/dev/null 2>&1 || hermes config get scheduler.enabled >/dev/null 2>&1; then
  run hermes config set scheduler.enabled true
else
  note "[sotto] note: scheduler.enabled not supported by this Hermes version — skipped"
fi

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
  # LOCAL mode: Hermes spawns the engine as a stdio child, so the installer has to hand it an
  # ABSOLUTE path to a `sotto-bridged` binary. Three places one can be, probed in this order:
  #   1. $SOTTO_BRIDGE_BIN — explicit override, for an engine kept anywhere else. It wins.
  #   2. built from source — monorepo checkouts only. The PUBLIC repo ships no Bridge source
  #      (sotto-bridge/ is stripped by the publish generator), so this path simply doesn't exist
  #      there — probing only this one is what made a public-repo install print "Done" with no
  #      Bridge registered.
  #   3. bundled inside the installed app — "Sotto Bridge.app" from the Releases page. The layout
  #      is build-app.sh's: it copies the engine to "$APP/Contents/Resources/$BIN_NAME"
  #      (BIN_NAME=sotto-bridged), and install-local.sh ditto's the bundle to the stable
  #      /Applications path. That makes this the one engine a Releases user has.
  BRIDGE_SRC="$ROOT/sotto-bridge/core/target/release/sotto-bridged"
  BRIDGE_APP="/Applications/Sotto Bridge.app/Contents/Resources/sotto-bridged"
  BRIDGE_BIN=""
  for cand in ${SOTTO_BRIDGE_BIN:+"$SOTTO_BRIDGE_BIN"} "$BRIDGE_SRC" "$BRIDGE_APP"; do
    [ -x "$cand" ] || continue
    BRIDGE_BIN="$cand"
    break
  done
  if [ -n "$BRIDGE_BIN" ]; then
    note "register sotto-local MCP (stdio: $BRIDGE_BIN) in $HERMES_HOME/config.yaml"
    if [ "$DRY_RUN" -eq 0 ]; then
      python3 "$HERE/configure_mcp.py" --name sotto-local --command "$BRIDGE_BIN" \
        --env "SOTTO_CHAT_DB=$HOME/Library/Messages/chat.db" --config "$HERMES_HOME/config.yaml"
    fi
  else
    # Do NOT abort here: everything after this point (Google guidance, crons) still installs
    # cleanly, and re-running the installer is idempotent. Record the failure and let the run
    # finish — then exit non-zero with the full report instead of printing "Done".
    BRIDGE_MISSING=1
    echo "! sotto-local NOT registered — no Bridge engine found. Full report at the end of this run."
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
#    The job list itself comes from crons.json — the ONE source every registrar reads (start.sh, this
#    installer, the OpenClaw installer, receiver._sotto_cron_jobs). Gates: SOTTO_PROACTIVE=0 drops the
#    mostly-silent ~15-min nudge watcher, SOTTO_DIGEST=0 drops the adaptive 12:30 catch-up digest.
#    No follow-up cron: post-meeting follow-ups run inside the 17:30 evening brief (Sprint 0); the
#    sotto-followup skill stays installed for on-demand use.
SOTTO_CRON_DELIVER="${SOTTO_CRON_DELIVER:-whatsapp}"   # platform-only → uses the gateway home channel
cron_rows() {   # name<TAB>schedule<TAB>prompt<TAB>skill for each job whose env gate is ON
  python3 -c 'import json, os, sys
for j in json.load(open(sys.argv[1])):
    if j.get("gate") and os.environ.get(j["gate"], "1") != "1":
        continue
    sched = os.environ.get(j.get("schedule_env") or "", "") or j["schedule"]
    print("\t".join([j["name"], sched, j["prompt"], j["skill"]]))' "$HERE/crons.json"
}
crons="$(hermes cron list 2>/dev/null || true)"
while IFS="$(printf '\t')" read -r cname csched cprompt cskill; do
  [ -n "$cname" ] || continue
  case "$crons" in
    *"$cname"*|*"$cprompt"*) note "cron $cname exists — skip" ;;
    *) run hermes cron create "$csched" "$cprompt" --skill "$cskill" --name "$cname" --deliver "$SOTTO_CRON_DELIVER" ;;
  esac
done <<EOF
$(cron_rows)
EOF
echo "NOTE: scheduled briefs deliver via '--deliver $SOTTO_CRON_DELIVER' — they reach you only while the"
echo "      Hermes gateway runs with that channel connected ('hermes gateway'; WhatsApp = scan the QR,"
echo "      Telegram = bot token — 'hermes gateway setup'). Interactive chat ('hermes') needs no channel."
echo "      Set SOTTO_CRON_DELIVER=local (and re-run) only if you deliberately want cron briefs kept in the CLI."

# "Done" is a claim, and it may only be made when everything this script set out to do happened.
# A LOCAL-mode run with no Bridge registered is a HALF install — Hermes has the skills and the crons
# but cannot read a single message off this Mac — so it reports that, names every path it looked in,
# and exits non-zero. Same in --dry-run: the probe reads the real filesystem, so a dry run that
# would have failed says so.
if [ "$BRIDGE_MISSING" -eq 1 ]; then
  cat >&2 <<EOF

== NOT done. sotto-local (the Mac Bridge) was NOT registered. ==

Everything else installed (skills, bundle, persona, crons), but LOCAL mode needs a 'sotto-bridged'
engine to spawn over stdio, and there is no executable at any of the paths probed:

  \$SOTTO_BRIDGE_BIN   ${SOTTO_BRIDGE_BIN:-(unset)}
  built from source   $BRIDGE_SRC
  installed app       $BRIDGE_APP

Two ways to fix it, then re-run this installer (it is idempotent):

  A. Download "Sotto Bridge.app" from the Releases page and drag it to /Applications.
     The signed app bundles the engine at the "installed app" path above. This is the option that
     works from the public repo, which ships no Bridge source.

  B. Build it from source — monorepo checkouts only, where sotto-bridge/core/ exists:
       (cd "$ROOT/sotto-bridge/core" && cargo build --release)

  (Engine somewhere else entirely? Point SOTTO_BRIDGE_BIN at it and re-run.)

For CLOUD mode you don't need a local engine at all: set BRIDGE_TOKEN (the bearer you enter in the
Mac app) and re-run — the Bridge dials out to the reverse relay instead.
EOF
  exit 1
fi

echo "== Done. In chat: '/sotto setup' (or 'Sotto, set up') — it verifies health(), seeds your memory + writing voice, and offers your first brief. =="
