#!/usr/bin/env bash
# Cloud boot: register the reverse-relay Bridge MCP, set the model + scheduler, start the trigger
# receiver and Hermes. Env (set on Railway/Render): GOOGLE_AI_API_KEY, BRIDGE_TOKEN (the Bridge's
# shared bearer), SOTTO_TRIGGER_TOKEN (optional wake-push), gateway token. Tunnel-free.
set -euo pipefail

# ── Named constants (defaults matter — see CLAUDE.md; these are NOT env knobs) ───────────────────
# Parallel attendee-research children (Hermes `delegation.max_concurrent_children`). Five keeps a
# meeting-heavy day's research from serializing without stampeding the Gemini quota.
RESEARCH_CONCURRENCY=5

# ── Retired settings: say so, don't fail ─────────────────────────────────────────────────────────
# Aug 2026 turned 17 tuning knobs into named constants (CLAUDE.md — defaults matter). A Railway
# variable nothing reads is worse than useless: it looks like a setting, and the value it names is
# quietly ignored. So name each one at boot, with the constant that replaced it, and carry on —
# a leftover variable is never a reason not to boot. Same list as RAILWAY.md § Removed settings;
# delete the variable in Railway and the line goes away.
for retired in \
  "SOTTO_ESCALATION_WINDOW_MIN=ESCALATION_WINDOW_MIN_DEFAULT (event-triage/scripts/triage_event.py)" \
  "SOTTO_EVENT_MAX_AGE_MIN=EVENT_MAX_AGE_MIN (event-triage/scripts/triage_event.py)" \
  "SOTTO_VIP_PRIORITY=VIP_PRIORITY_MIN (event-triage/scripts/triage_event.py)" \
  "SOTTO_VALVE_MAX_AGE_MIN=VALVE_MAX_AGE_MIN (event-triage/scripts/triage_event.py)" \
  "SOTTO_VALVE_MAX_PER_HOUR=VALVE_MAX_PER_HOUR (event-triage/scripts/triage_event.py)" \
  "SOTTO_VALVE_INTERVAL_SECS=VALVE_INTERVAL_SECS_DEFAULT (trigger-receiver/receiver.py)" \
  "SOTTO_TAP_GRACE_MIN=TAP_GRACE_MIN_DEFAULT (trigger-receiver/calcache.py)" \
  "SOTTO_TAP_LOOKBACK_MIN=TAP_LOOKBACK_INTERVALS (trigger-receiver/calcache.py)" \
  "SOTTO_TAP_SKIP_INTERNAL=TAP_SKIP_INTERNAL (trigger-receiver/calcache.py)" \
  "SOTTO_PROACTIVE_LEAD_MIN=PROACTIVE_LEAD_MIN (proactive/scripts/proactive_scan.py)" \
  "SOTTO_RETUNE_OFFER_MIN=RETUNE_OFFER_MIN (proactive/scripts/proactive_scan.py)" \
  "SOTTO_RETUNE_OFFER_COOLDOWN_DAYS=RETUNE_OFFER_COOLDOWN_DAYS (proactive/scripts/proactive_scan.py)" \
  "SOTTO_STALE_AGE_DAYS=STALE_AGE_DAYS (_shared/scripts/retune_scan.py)" \
  "SOTTO_STALE_SURFACED=STALE_SURFACED (_shared/scripts/retune_scan.py)" \
  "SOTTO_RESEARCH_RECENCY_DAYS=DEFAULT_RECENCY_DAYS (_shared/scripts/research_attendees.py)" \
  "SOTTO_PREWARM_MAX=MAX_PREWARM (_shared/scripts/prewarm_graph.py)" \
  "SOTTO_RESEARCH_CONCURRENCY=RESEARCH_CONCURRENCY (this file)" ; do
  retired_name="${retired%%=*}"
  if [ -n "${!retired_name:-}" ]; then
    echo "[sotto] NOTE: $retired_name is set but NO LONGER READ — it is now the constant ${retired#*=}."
    echo "[sotto]       Remove it from your variables (RAILWAY.md § Removed settings)."
  fi
done

# 0) Persist Hermes state on the /data volume so REDEPLOYS don't wipe your WhatsApp login, config, SOUL,
#    or the knowledge graph. The image bakes skills into /root/.hermes; we seed the volume from it on the
#    first boot, always refresh the Sotto skills/bundle from the (possibly updated) image, then point
#    ~/.hermes at the volume. Defensive (|| true): if the volume is missing, Hermes still boots, just
#    non-persistent. Must run BEFORE any `hermes …` call below (they read $HOME/.hermes).
HSTATE="${SOTTO_DATA:-/data}/hermes"
if [ ! -d "$HSTATE" ]; then
  mkdir -p "$HSTATE"
  cp -a /root/.hermes/. "$HSTATE/" 2>/dev/null || true          # first boot: seed everything from image
  cp -a /app/hermes-image-version.txt "$HSTATE/.image-version" 2>/dev/null || true
fi
mkdir -p "$HSTATE/skills" "$HSTATE/skill-bundles"
rm -rf "$HSTATE/skills/sotto" 2>/dev/null || true                # always refresh skills from the image
cp -a /root/.hermes/skills/sotto "$HSTATE/skills/" 2>/dev/null || true
cp -a /root/.hermes/skill-bundles/sotto.yaml "$HSTATE/skill-bundles/" 2>/dev/null || true
# Hermes runtime upgrade (opt-in): the volume's ~/.hermes copy is seeded ONCE, so if the installer
# keeps any runtime under ~/.hermes, a rebuilt image with newer Hermes can be shadowed by the stale
# volume copy. SOTTO_REFRESH_HERMES=1 re-seeds every INSTALLER-owned top-level entry (from the
# build-time manifest) from this image, while a denylist protects user state (WhatsApp login,
# sessions, config, SOUL, credentials, crons, and the Sotto skills — refreshed above anyway).
# Flow: bump HERMES_REFRESH in the Dockerfile → redeploy → set SOTTO_REFRESH_HERMES=1 → redeploy →
# check the boot log's version line → unset. Opt-in so an ordinary boot can never wipe state.
if [ "${SOTTO_REFRESH_HERMES:-0}" = "1" ] && [ -s /app/hermes-image-manifest.txt ]; then
  echo "[sotto] SOTTO_REFRESH_HERMES=1 — refreshing installer-owned Hermes entries from this image"
  KEEP=" config.yaml SOUL.md .env setup_code skills skill-bundles skill_bundles sessions session \
 state data logs log credentials credentials.json whatsapp telegram discord cron crons memory \
 gateway history db cache.db "
  while IFS= read -r entry; do
    [ -n "$entry" ] || continue
    case "$KEEP" in *" $entry "*) continue ;; esac
    if [ -e "/root/.hermes/$entry" ]; then
      rm -rf "${HSTATE:?}/$entry" 2>/dev/null || true
      cp -a "/root/.hermes/$entry" "$HSTATE/" 2>/dev/null || true
    fi
  done < /app/hermes-image-manifest.txt
  cp -a /app/hermes-image-version.txt "$HSTATE/.image-version" 2>/dev/null || true
  echo "[sotto]   refresh done — you can unset SOTTO_REFRESH_HERMES now."
fi
# Refresh the Sotto persona block in the persisted SOUL.md too — otherwise persona/guardrail changes
# never take effect on a redeploy (the volume copy is seeded once and goes stale). Strip the old Sotto
# block (everything from its marker to EOF, since it's appended last) and re-append the current one.
# This is ALSO the only way a standing instruction reaches gateway sessions — they load SOUL.md (see
# the reply-prefix note further down), and nothing else in this file talks to them. So a rule the
# gateway must always carry (e.g. "a bare 'sure' with no referent → pending_offer.py get") belongs in
# sotto-persona.md, which the Dockerfile and both install.sh's append the same way. One source.
if [ -f "$HSTATE/SOUL.md" ] && [ -f /app/adapters/hermes/sotto-persona.md ]; then
  sed -i '/chief-of-staff persona/,$d' "$HSTATE/SOUL.md" 2>/dev/null || true
  printf '\n' >> "$HSTATE/SOUL.md"
  cat /app/adapters/hermes/sotto-persona.md >> "$HSTATE/SOUL.md"
fi
rm -rf /root/.hermes && ln -s "$HSTATE" /root/.hermes            # ~/.hermes → volume (sessions persist)

# Version visibility: every boot log states the Hermes actually RUNNING vs the one this image was
# built with. If they differ, the volume seed is shadowing a newer image — SOTTO_REFRESH_HERMES=1
# adopts it (see above). This line is the first thing to check when "is my Hermes current?" comes up.
IMG_HVER="$(cat /app/hermes-image-version.txt 2>/dev/null | head -1 || echo unknown)"
RUN_HVER="$( { hermes --version 2>/dev/null || hermes version 2>/dev/null || echo unknown; } | head -1)"
echo "[sotto] hermes running: ${RUN_HVER:-unknown} | image built with: ${IMG_HVER:-unknown}"
if [ -n "$RUN_HVER" ] && [ -n "$IMG_HVER" ] && [ "$RUN_HVER" != "unknown" ] && \
   [ "$IMG_HVER" != "unknown" ] && [ "$RUN_HVER" != "$IMG_HVER" ]; then
  echo "[sotto] WARNING: running Hermes differs from this image's — the volume seed is stale."
  echo "[sotto]          Set SOTTO_REFRESH_HERMES=1 and redeploy once to adopt the image's Hermes."
fi
# Same two strings, where the Integrations page can read them: the boot log is the right place to
# check "is my Hermes current?" from a terminal, and $SOTTO_DATA/cache/hermes-version.json is the
# right place to check it from the browser. Rewritten every boot, read by nothing else, never state.
# (Quotes/backslashes stripped so the hand-built JSON can't be broken by a version string.)
mkdir -p "${SOTTO_DATA:-/data}/cache" 2>/dev/null || true
printf '{"running":"%s","image":"%s"}\n' \
  "$(printf '%s' "$RUN_HVER" | tr -d '"\\')" "$(printf '%s' "$IMG_HVER" | tr -d '"\\')" \
  > "${SOTTO_DATA:-/data}/cache/hermes-version.json" 2>/dev/null || true

# Brief resilience: default an AUTOMATIC fallback model for the brief's direct Gemini call.
# compose_brief.py's call_gemini activates the fallback when SOTTO_FALLBACK_MODEL alone is set — it
# reuses GOOGLE_AI_API_KEY unless SOTTO_FALLBACK_API_KEY is also set — so no second key is needed:
# a 429/5xx/timeout on gemini-3.8-flash retries on gemini-3-flash-preview (cheaper: $0.50/$3.00 vs
# $0.75/$3.75 per 1M tokens, and a separate per-model rate-limit bucket). Must be exported BEFORE the
# receiver starts below so every `hermes -z` brief run inherits it. Override with your own
# SOTTO_FALLBACK_MODEL (keep it 1M-context — the brief prompt runs 100K–140K chars).
export SOTTO_FALLBACK_MODEL="${SOTTO_FALLBACK_MODEL:-gemini-3-flash-preview}"

# Your own email address. Everything that has to answer "is this me?" reads it: attendee research
# skips you and your colleagues, and the post-meeting tap counts the OTHER humans in the room. The
# briefs run headlessly, so nothing passes --user-email — the chain is SOTTO_USER_EMAIL (an
# OVERRIDE) → google_account_email on the volume, which the receiver DERIVES at the Google connect
# (the `From` of your own sent mail; it also backfills at boot for deploys that connected earlier).
# So the only case worth a line is "neither yet" — and even then the calendar cache's own inference
# (calcache._infer_self_email, ≥2 peopled events agreeing) covers the tap, and the brief simply
# researches a few people it needn't have.
if [ -z "${SOTTO_USER_EMAIL:-}" ] \
   && ! grep -q '"google_account_email": *"[^"]' "${SOTTO_DATA:-/data}/config/settings.json" 2>/dev/null; then
  echo "[sotto] note: Sotto doesn't know your own address yet — connect Google on /setup and it"
  echo "[sotto]       learns it (or set SOTTO_USER_EMAIL in Railway to override). See RAILWAY.md."
fi

# 0.4) The delivery channel, decided ONCE and exported to everything below.
# One sentence: Sotto delivers to Telegram unless this volume already holds a paired WhatsApp session
# and no TELEGRAM_BOT_TOKEN is set — so a deploy that was WhatsApp-first never loses its channel on a
# redeploy, and everyone else gets the one-variable path. SOTTO_CRON_DELIVER set in Railway always
# wins. Exported because the receiver (started below), reconcile_crons.py, `hermes cron create
# --deliver` and the gateway must all agree: the channel has ONE decider, and the log line says which.
# WhatsApp costs a boot-time QR wait, so its gateway is enabled only when it IS the channel.
WA_CREDS="$HOME/.hermes/platforms/whatsapp/session/creds.json"
if [ -n "${SOTTO_CRON_DELIVER:-}" ]; then
  CHANNEL_WHY="SOTTO_CRON_DELIVER is set"
elif [ -f "$WA_CREDS" ] && [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  SOTTO_CRON_DELIVER="whatsapp"; CHANNEL_WHY="this volume is already paired with WhatsApp"
else
  SOTTO_CRON_DELIVER="telegram"; CHANNEL_WHY="the default"
fi
export SOTTO_CRON_DELIVER
if [ "$SOTTO_CRON_DELIVER" = "whatsapp" ]; then
  export WHATSAPP_ENABLED="${WHATSAPP_ENABLED:-true}"
else
  export WHATSAPP_ENABLED="${WHATSAPP_ENABLED:-false}"
fi
echo "[sotto] delivery channel: $SOTTO_CRON_DELIVER ($CHANNEL_WHY); whatsapp gateway: $WHATSAPP_ENABLED"

# 0.5) Start the trigger receiver IMMEDIATELY so Railway's /health healthcheck passes within seconds —
#      before the slower boot steps below (Google auth makes network calls). Otherwise a slow first boot
#      can time out the healthcheck and Railway marks the deploy crashed. The receiver only needs $PORT
#      + $SOTTO_DATA (the volume), not Hermes — safe to start first. (`hermes -z` = the scriptable
#      one-shot the receiver uses to run a brief; there is no `hermes run`.)
# SOTTO_MCP_TOKEN lets the receiver's reverse-MCP relay authenticate the Mac's outbound link + Hermes'
# /mcp calls. Reuse BRIDGE_TOKEN so there's one secret to set.
# The Gemini key under all three names, EXPORTED before the receiver starts: every brief runs in a
# child of the receiver and reads GOOGLE_AI_API_KEY from that inherited environment, so a deploy
# that set GEMINI_API_KEY (Google's own name for it) passed the boot probe and then failed every
# brief with "GOOGLE_AI_API_KEY not set" — the step-3.5 fan-out below only ever reached
# ~/.hermes/.env (Day-0 simulation, Sep 2026).
GKEY="${GEMINI_API_KEY:-${GOOGLE_API_KEY:-${GOOGLE_AI_API_KEY:-}}}"
if [ -n "$GKEY" ]; then
  export GOOGLE_AI_API_KEY="$GKEY" GEMINI_API_KEY="$GKEY" GOOGLE_API_KEY="$GKEY"
fi
SOTTO_MCP_TOKEN="${BRIDGE_TOKEN:-}" SOTTO_RUN_SKILL="hermes -z" python3 /app/trigger-receiver/receiver.py &

# The receiver gates its whole setup surface (/setup, /whatsapp/qr, /google/auth, /debug/google…)
# behind a per-deploy setup code — a bare URL now 403s. Any setup link WE print must carry
# ?code=<code> (env override, else the code the receiver persists to the volume at boot).
# That code is resolved HERE and nowhere else, because step 5a passes it to the Telegram linker as
# the pairing phrase: a per-deploy secret only whoever reads the deploy log has, which is exactly who
# is allowed to own this deploy.
setup_code() {
  local code="${SOTTO_SETUP_CODE:-}"
  if [ -z "$code" ]; then
    # The receiver (step 0.5, a background process) mints this file after its own imports; the
    # first reader here used to race it and, losing, told the user to set SOTTO_SETUP_CODE and
    # redeploy — for a race, not a misconfiguration. Wait for it, briefly.
    # …and wait ONCE per boot: this is called three times, and on a box with no writable volume
    # the file never appears, so three waits would be 90 s of boot for the same empty answer.
    if [ -z "${SETUP_CODE_WAITED:-}" ]; then
      SETUP_CODE_WAITED=1
      local tries=0
      while [ ! -s "${SOTTO_DATA:-/data}/setup_code" ] && [ "$tries" -lt 30 ]; do
        sleep 1; tries=$((tries + 1))
      done
    fi
    code="$(cat "${SOTTO_DATA:-/data}/setup_code" 2>/dev/null | tr -d '[:space:]' || true)"
  fi
  printf '%s' "$code"
  return 0
}
setup_qs() {
  local code
  code="$(setup_code)"
  if [ -n "$code" ]; then printf '?code=%s' "$code"; fi
  return 0
}

# Hermes v0.20 dropped some config keys (scheduler.enabled, code_execution.timeout) and warns on
# unrecognized ones. Probe with `config get` first; set only what this Hermes version knows.
hermes_set_if_supported() {
  if hermes config get "$1" >/dev/null 2>&1; then
    hermes config set "$1" "$2" >/dev/null 2>&1 || true
  else
    echo "[sotto] note: $1 not supported by this Hermes version"
  fi
}

# 1) Register the sotto-local MCP at the LOCAL reverse relay. The Mac dials OUT to the receiver
#    (/bridge/poll); Hermes points at the always-up local endpoint, so it never 530s. Tunnel-free —
#    just set BRIDGE_TOKEN (the shared bearer). No BRIDGE_URL, no Cloudflare.
if [ -n "${BRIDGE_TOKEN:-}" ]; then
  # --derive-mcp: Hermes is handed HMAC(root, "sotto-mcp"), never the root — the agent talks to
  # prompt-injectable content, and with only the derived bearer it cannot act as the Bridge.
  python3 /app/adapters/hermes/configure_mcp.py --url "http://127.0.0.1:${PORT:-8787}/mcp" \
    --token "$BRIDGE_TOKEN" --derive-mcp --config "$HOME/.hermes/config.yaml"
  echo "[sotto] sotto-local → reverse relay (tunnel-free); the Mac dials out to /bridge/poll."
fi

# 2) Model + scheduler (dedicated cloud instance → Gemini 1M as the driver too).
#    Use the NATIVE Gemini model id (not the OpenRouter-style "google/…", which would route via
#    OpenRouter and need OPENROUTER_API_KEY). The key is set as GEMINI_API_KEY/GOOGLE_API_KEY below.
hermes config set model gemini-3.8-flash || true
hermes_set_if_supported scheduler.enabled true
# Sotto's scheduled output is already written as the exact user-facing message. Hermes wraps cron
# deliveries by default with "Cronjob Response", the job id, and a management footer; that turns a
# one-line human nudge into a system notification. Disable the global wrapper so the native cron
# fallback lands through the same clean presentation as Bridge-triggered briefs and nudges. This also
# keeps personal `user-` routines clean. Older Hermes builds may not expose the key, so use the same
# capability probe as the legacy scheduler setting instead of making boot depend on it.
hermes_set_if_supported cron.wrap_response false
# Timezone — Hermes cron + the system-prompt time injection default to UTC. Set the user's IANA zone so
# the 6:30/17:30 briefs fire at their LOCAL morning/evening, AND so `hermes cron create` below doesn't
# block on an interactive timezone PROMPT at boot (a non-interactive boot fails the prompt → NO cron
# created → no briefs). Set SOTTO_TIMEZONE in Railway (e.g. America/Los_Angeles); defaults to UTC.
# The setup WIZARD also captures the browser-detected zone to $SOTTO_DATA/config/settings.json, so the
# Railway var is OPTIONAL — fall back to it here (the cron hour then self-heals on the next boot).
# CANONICAL TZ ORDER (the same chain everywhere: receiver._configured_tz_name, dashboard._local_today,
# the skills' timeutil.configured_tz): SOTTO_TIMEZONE → TZ → $SOTTO_DATA/config/settings.json →
# server local. TZ used to be missing HERE, so a deploy that set only TZ registered its crons in UTC
# while every rendered date was local.
if [ -z "${SOTTO_TIMEZONE:-}" ] && [ -n "${TZ:-}" ]; then
  SOTTO_TIMEZONE="$TZ"
  echo "[sotto] timezone from TZ: $SOTTO_TIMEZONE (no SOTTO_TIMEZONE var set)"
fi
if [ -z "${SOTTO_TIMEZONE:-}" ]; then
  SETTINGS_TZ="$(python3 - <<'PY' 2>/dev/null || true
import json, os
p = os.path.join(os.environ.get("SOTTO_DATA", "/data"), "config", "settings.json")
try:
    print((json.load(open(p)) or {}).get("timezone", "") or "")
except Exception:
    print("")
PY
)"
  if [ -n "$SETTINGS_TZ" ]; then
    SOTTO_TIMEZONE="$SETTINGS_TZ"
    echo "[sotto] timezone from setup wizard: $SOTTO_TIMEZONE (no SOTTO_TIMEZONE var set)"
  else
    echo "[sotto] WARNING: SOTTO_TIMEZONE/TZ unset and no wizard zone yet — cron briefs fire in UTC until you"
    echo "[sotto]          finish setup at /setup (auto-detects your zone) or set SOTTO_TIMEZONE in Railway."
  fi
fi
hermes config set timezone "${SOTTO_TIMEZONE:-UTC}" || true
# Brief composition runs the FLEX extraction AND a critic pass inside ONE execute_code call — two
# Gemini calls that together can exceed Hermes' default 300s code_execution timeout, getting the
# script KILLED mid-run (after which the agent improvises a freehand, low-quality brief). Raise the
# ceiling so a 2–4 min brief always finishes. (The desktop brief took 2+ min; this matches.)
hermes_set_if_supported code_execution.timeout 600
# execute_code is HARD-BLOCKED in cron/scheduled runs (upstream: hermes-agent#38585 — no approval can
# carry into an unattended job). The skills therefore fall back to the `terminal` tool for their
# deterministic CLI scripts on cron runs (see the persona rule) — but terminal's DEFAULT timeout is
# 180s, which a 2–4 min brief would blow through mid-script. Match the execute_code ceiling.
hermes config set terminal.timeout 600 || true
# Google client lib sanity check: google_api.py (the brief's Gmail/Calendar fetch) needs googleapiclient.
# It's baked into the image, but if the brief's python3 differs from the build python3 the import can be
# missing — which silently degrades every brief to local-only and makes the agent improvise `pip install`.
# Verify against the SAME python3 the brief uses; self-heal once if absent so we don't depend on a redeploy.
if ! python3 -c "import googleapiclient" >/dev/null 2>&1; then
  echo "[sotto] WARNING: googleapiclient missing for $(command -v python3) — installing (Gmail/Calendar need it)…"
  python3 -m pip install --quiet --no-cache-dir google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2 2>&1 | sed 's/^/[sotto]   pip: /' || \
    echo "[sotto]   pip install FAILED — briefs will be local-only until this python has googleapiclient."
fi
python3 -c "import googleapiclient" >/dev/null 2>&1 \
  && echo "[sotto] googleapiclient OK ($(command -v python3)) — Gmail/Calendar fetch can run." \
  || echo "[sotto] googleapiclient STILL missing — Gmail/Calendar will be empty."

# ── Isolation: protect Sotto's deterministic layer from Hermes' self-modification ──────────────────
# Sotto's quality IS the pinned, image-sourced sotto-* skills + compose_brief.py. We do NOT want the
# agent rewriting them (self-improving skills via skill_manage) or the Curator archiving them as
# "unused" (it can, and our cloud skills are COPIED, not hub-installed, so they're not auto-exempt).
# Our skills already auto-heal from the image each boot — but that only fixes it on the NEXT redeploy,
# so we also disable at the source on this Sotto-focused instance. Opt out with SOTTO_ALLOW_SELF_IMPROVE=1
# (e.g. a shared general-purpose Hermes where you still want self-improvement for non-Sotto work).
if [ "${SOTTO_ALLOW_SELF_IMPROVE:-0}" != "1" ]; then
  hermes config set skills.write_approval true   >/dev/null 2>&1 || true  # no silent skill self-writes
  hermes config set curator.prune_builtins false >/dev/null 2>&1 || true  # don't archive our skills
  hermes config set curator.consolidate false    >/dev/null 2>&1 || true
  # NOTE: `hermes curator pin <skill>` now exists as a first-class per-skill protection — a
  # finer-grained complement to the blanket pause below if you ever re-enable the curator.
  hermes curator pause                            >/dev/null 2>&1 || true  # belt-and-suspenders
  echo "[sotto] isolation: skill self-writes gated + curator paused (SOTTO_ALLOW_SELF_IMPROVE=1 to allow)."
fi
# Sub-agent research: the brief/meeting-prep fan out attendee research to parallel delegate_task children
# (one per external attendee). Lift the concurrency cap from Hermes' default 3 so a meeting-heavy day's
# research doesn't serialize. Named constant, not a knob (see CLAUDE.md — defaults matter).
hermes config set delegation.max_concurrent_children "$RESEARCH_CONCURRENCY" >/dev/null 2>&1 || true
# Route auxiliary side-tasks to the main Gemini model so they don't fall back to unconfigured
# nous/openrouter ("no Nous authentication" / "payment/credit error" warnings — and a broken
# web_extract degrades attendee research). Hermes keys auxiliary PER TASK (auxiliary.<task>.provider),
# NOT a flat auxiliary.provider — so we set each known task to provider "main" (the main chat model =
# Gemini) with an empty model. Write it straight into config.yaml (authoritative; survives version key
# drift) and also try the CLI form. Tasks per the Hermes docs: vision, web_extract, tts_audio_tags,
# session_search, plus compression, title_generation, approval, skills_hub, mcp, triage_specifier.
python3 - "$HOME/.hermes/config.yaml" <<'PY' || true
import sys, yaml
p = sys.argv[1]
try:
    cfg = yaml.safe_load(open(p)) or {}
except Exception:
    cfg = {}
if not isinstance(cfg, dict):
    cfg = {}
aux = cfg.get("auxiliary")
if not isinstance(aux, dict):
    aux = {}
for task in ("vision", "web_extract", "tts_audio_tags", "session_search",
             "compression", "title_generation", "approval", "skills_hub", "mcp",
             "triage_specifier"):
    t = aux.get(task)
    if not isinstance(t, dict):
        t = {}
    t["provider"] = "main"   # the main chat model (Gemini), never nous/openrouter
    t["model"] = ""          # empty = use the main model
    aux[task] = t
aux.pop("provider", None)    # drop the bad flat keys a prior boot may have written
aux.pop("model", None)
cfg["auxiliary"] = aux
yaml.safe_dump(cfg, open(p, "w"), default_flow_style=False, sort_keys=False)
print("[sotto] auxiliary tasks routed to main (Gemini)")
PY
for task in vision web_extract tts_audio_tags session_search \
            compression title_generation approval skills_hub mcp triage_specifier; do
  hermes config set "auxiliary.$task.provider" main >/dev/null 2>&1 || true
done

# Gateway reply prefix ("⚕ Hermes Agent"): the documented knob is `whatsapp.reply_prefix` (the
# WHATSAPP_REPLY_PREFIX env works too) — a custom string replaces the default header, and an empty
# string disables it. (Upstream #26596 asked to rename the whole gateway identity and was closed
# not-planned, but the prefix is independently configurable — and gateway sessions DO load SOUL.md in
# current code, so the voice is already Sotto's.) Default: brand replies as *Sotto*;
# SOTTO_HIDE_AGENT_NAME=1 drops the prefix entirely instead.
if [ "${SOTTO_HIDE_AGENT_NAME:-0}" = "1" ]; then
  hermes config set whatsapp.reply_prefix "" >/dev/null 2>&1 || true
  echo "[sotto] whatsapp reply prefix: none (SOTTO_HIDE_AGENT_NAME=1)"
else
  hermes config set whatsapp.reply_prefix $'*Sotto*\n' >/dev/null 2>&1 || true
  echo "[sotto] whatsapp reply prefix: *Sotto* (set SOTTO_HIDE_AGENT_NAME=1 for none)"
fi
# Progress UX (owner, Aug 2026): a chat channel is a place for RESULTS, not a terminal. By default
# NOTHING streams mid-turn — no model narration ("thinking" text), no tool breadcrumbs; the
# platform's native typing indicator (hermes default: on) plus the "⏳ Working — N min" heartbeat
# cover the wait, and the first thing the user reads is the deliverable. One knob restores the old
# streaming: SOTTO_TOOL_PROGRESS=new → plain-language narration + ONE edit-in-place tool bubble
# (cleaned up on delivery); =all/verbose → full breadcrumbs for debugging. `off` is the default.
TP="${SOTTO_TOOL_PROGRESS:-off}"
if [ "$TP" = "off" ]; then
  hermes config set display.interim_assistant_messages false >/dev/null 2>&1 || true
  echo "[sotto] progress stream: off (typing indicator only; SOTTO_TOOL_PROGRESS=new restores narration)"
else
  hermes config set display.interim_assistant_messages true >/dev/null 2>&1 || true
  echo "[sotto] progress stream: $TP (narration + tool bubble)"
fi
hermes config set display.tool_progress "$TP" >/dev/null 2>&1 || true
hermes config set display.tool_progress_grouping accumulate >/dev/null 2>&1 || true
for k in whatsapp telegram discord; do
  hermes config set "display.platforms.$k.cleanup_progress" true >/dev/null 2>&1 || true
done
# Tapbacks (owner ask, Aug 2026): with the progress stream off, the reaction IS the acknowledgment
# — Hermes reacts on YOUR message: 👀 when it starts working, ✅ when the reply lands, ❌ on an
# error (Telegram Bot API replaces the bot's reaction atomically, so you only ever see one).
# Telegram-only: hermes has no bot-reaction support on WhatsApp, and the key is ignored where
# unsupported. Hermes ships it off; Sotto turns it on — SOTTO_REACTIONS=0 restores off.
if [ "${SOTTO_REACTIONS:-1}" = "1" ]; then
  hermes config set telegram.reactions true >/dev/null 2>&1 || true
  echo "[sotto] tapbacks: on — 👀 working · ✅ replied · ❌ error (telegram; SOTTO_REACTIONS=0 to disable)"
else
  hermes config set telegram.reactions false >/dev/null 2>&1 || true
  echo "[sotto] tapbacks: off (SOTTO_REACTIONS=0)"
fi

# Session reset tied to DEPLOYS, not the clock (owner, Aug 2026). The clock-driven reset had to go
# because its "◐ Session automatically reset… ◆ Model/Provider/Context" broadcast has no silence
# key in any Hermes version or doc (re-checked Aug 2026; scheduling it at 6:00 so the brief would
# bury the notice failed — it stood alone on the phone). But a NEVER-resetting session would
# freeze the persona: sessions snapshot their system prompt, so the SOUL.md this boot just
# refreshed only reaches a NEW session. Deploys are exactly when freshness matters — new code, new
# session — so: mode none kills the daily banner, and every boot archives the gateway sessions
# below. The next message then starts a fresh session SILENTLY (no reset event fires, so nothing
# is broadcast) carrying this deploy's persona. Deploy cadence alone turned out not to bound the
# transcript (a week between deploys, Sep 2026, and replies began copying delivered briefs back
# out), so the receiver runs this same silent archive nightly too — receiver._session_archive_tick:
# a session lasts a day or a deploy, whichever comes first. /new in chat remains the manual reset;
# /resume can still reopen an archived transcript. Sotto's REAL memory never lived in the transcript anyway (graph + master
# file — the persona persists feedback via sotto-feedback precisely because chat is disposable).
hermes_set_if_supported session_reset.mode none
# Best-effort and never fatal: ids in `sessions list` are hex/uuid tokens (the same shape the cron
# reconciler matches). `archive` keeps the transcript — this is "start fresh", never "destroy
# history". If the CLI shape drifts, nothing archives and the only cost is a stale persona
# snapshot until the user types /new — which the log line below says out loud.
if hermes sessions list >/dev/null 2>&1; then
  # ONE implementation, shared with the receiver's nightly archive: the hex/uuid grep this loop
  # used to run matched none of Hermes' real ids (20260903_033026_65967d, cron_…), so it archived
  # 0 sessions at every boot for a week and no session ever reset (Sep 3, 2026).
  python3 /app/trigger-receiver/sessions.py || echo "[sotto] fresh-per-deploy: session archive failed — persona updates reach chat only after /new"
else
  echo "[sotto] fresh-per-deploy: sessions CLI unavailable — persona updates reach chat only after /new"
fi
# Voice (read + listen). Enable Hermes-native TTS so Sotto can deliver a SPOKEN brief and voice replies
# (and transcribe voice notes you send — two-way). Default `edge` (Microsoft Edge TTS — free, no key,
# good quality); set SOTTO_TTS_PROVIDER=gemini to use the Google key you already have (voice via
# gemini-2.5-flash-preview-tts). Set SOTTO_TTS=0 to keep briefs text-only.
if [ "${SOTTO_TTS:-1}" = "1" ]; then
  TTS_PROVIDER="${SOTTO_TTS_PROVIDER:-edge}"
  hermes config set tts.provider "$TTS_PROVIDER" >/dev/null 2>&1 || true
  if [ "$TTS_PROVIDER" = "edge" ]; then
    hermes config set tts.edge.voice "${SOTTO_TTS_VOICE:-en-US-AriaNeural}" >/dev/null 2>&1 || true
  elif [ "$TTS_PROVIDER" = "gemini" ]; then
    hermes config set tts.gemini.model "gemini-2.5-flash-preview-tts" >/dev/null 2>&1 || true
    hermes config set tts.gemini.voice "${SOTTO_TTS_VOICE:-Kore}" >/dev/null 2>&1 || true
  fi
fi

# 3) Cron fallback (the Bridge wake-push fires the real brief).
# PRIOR BUG: deploys before the idempotency guard piled up DOZENS of duplicate sotto crons. They all
# fired at 6:30/17:30 simultaneously, hammering Gemini → HTTP 429 RESOURCE_EXHAUSTED → briefs never
# delivered for days. The old `case` guard only stopped NEW dupes; it never removed the historical
# pile. So we now FIRST remove every existing sotto job by id, then recreate exactly one of each —
# fully idempotent + self-healing. Recreation also sets a stable --name and the resolved channel's
# --deliver target instead of the default "local" (which never reaches the user).
#
# ONE SOURCE: the job list lives in adapters/hermes/crons.json (name/schedule/prompt/skill, plus an
# optional `gate` env var and `schedule_env` override). Every registrar reads that file — this boot,
# receiver.py's _sotto_cron_jobs (the timezone re-registration) and both adapters' install.sh — so a
# schedule can never drift between them again. `--deliver` is NOT per job: SOTTO_CRON_DELIVER (step
# 0.4 resolved it) is the one delivery target for all of them.
CRONS_JSON="${SOTTO_CRONS_JSON:-/app/adapters/hermes/crons.json}"
# One reconciler owns boot convergence and live timezone changes. It removes only crons.json system
# jobs (plus retired Sotto markers), fences every user-* routine, and recreates the enabled spec.
python3 /app/adapters/hermes/reconcile_crons.py \
  --spec "$CRONS_JSON" --deliver "$SOTTO_CRON_DELIVER" \
  || echo "[sotto] WARNING: cron reconciliation did not complete; next boot will retry"

# Dump the registered crons so cron is OBSERVABLE (empty list, UTC next-run, or "Deliver: local" are
# all bugs visible at a glance). Capped with `head` — the old uncapped dump of dozens of dupes hit
# Railway's 500-logs/sec limit ("Messages dropped"). After dedup it's ~3 jobs, so the cap rarely bites.
echo "[sotto] cron scheduler: $(hermes cron status 2>/dev/null | head -1 || echo '?') tz=${SOTTO_TIMEZONE:-UTC} deliver=${SOTTO_CRON_DELIVER}; registered crons:"
hermes cron list 2>&1 | head -40 | sed 's/^/[sotto]   /' || echo "[sotto]   (hermes cron list failed)"

# 3.5) Configure the gateway NON-INTERACTIVELY. Hermes reads messaging-platform settings from
#      ~/.hermes/.env (NOT config.yaml), and denies all users until an allowlist is set — without this
#      the gateway logs "No messaging platforms enabled". We upsert the keys from Railway env each boot
#      so Railway stays the source of truth. Telegram needs only TELEGRAM_BOT_TOKEN (step 5 captures
#      the chat id); WhatsApp needs WHATSAPP_ALLOWED_USERS + WHATSAPP_HOME_CHANNEL, your number, e.g.
#      15551234567. Every gateway variable travels the one prefix loop below — none is special-cased.
ENVF="$HOME/.hermes/.env"
touch "$ENVF"
upsert_env() {  # replace any existing KEY= line, then append the new value
  grep -v "^$1=" "$ENVF" > "$ENVF.tmp" 2>/dev/null || true
  mv "$ENVF.tmp" "$ENVF"
  printf '%s=%s\n' "$1" "$2" >> "$ENVF"
}
drop_env() {    # remove any existing KEY= line, leaving nothing behind
  grep -v "^$1=" "$ENVF" > "$ENVF.tmp" 2>/dev/null || true
  mv "$ENVF.tmp" "$ENVF"
}
# The Gemini key: Sotto's brief reads GOOGLE_AI_API_KEY, but Hermes' gemini provider reads
# GEMINI_API_KEY / GOOGLE_API_KEY. Map whichever the user set in Railway to all three.
GKEY="${GEMINI_API_KEY:-${GOOGLE_API_KEY:-${GOOGLE_AI_API_KEY:-}}}"
if [ -n "$GKEY" ]; then
  upsert_env GOOGLE_AI_API_KEY "$GKEY"   # Sotto compose_brief.py (AI Studio REST)
  upsert_env GEMINI_API_KEY    "$GKEY"   # Hermes gemini provider (chat/agent model)
  upsert_env GOOGLE_API_KEY    "$GKEY"   # Hermes also accepts this name
fi
# Boot sanity check: ONE cheap GET against the Generative Language API proves the key is valid AND the
# configured model exists — a bad key/model otherwise only surfaces hours later as a silently failed
# brief. Non-fatal by construction (`|| true` inside the substitution guards set -euo pipefail; 10s cap
# so a network blip can't stall boot). Exactly one log line either way.
GMODEL="${SOTTO_GEMINI_MODEL:-gemini-3.8-flash}"
if [ -n "$GKEY" ]; then
  GCHECK="$(curl -s -m 10 -o /dev/null -w '%{http_code}' \
    "https://generativelanguage.googleapis.com/v1beta/models/${GMODEL}?key=${GKEY}" 2>/dev/null || true)"
  if [ "$GCHECK" = "200" ]; then
    echo "[sotto] Gemini key OK (model ${GMODEL} available)"
  else
    echo "[sotto] WARNING: Gemini key/model check failed (HTTP ${GCHECK:-000}) — briefs will fail; check GOOGLE_AI_API_KEY and SOTTO_GEMINI_MODEL"
  fi
else
  echo "[sotto] WARNING: Gemini key/model check failed (HTTP 000, no key set) — briefs will fail; check GOOGLE_AI_API_KEY and SOTTO_GEMINI_MODEL"
fi
[ -n "${GATEWAY_ALLOW_ALL_USERS:-}" ] && upsert_env GATEWAY_ALLOW_ALL_USERS "$GATEWAY_ALLOW_ALL_USERS"
# Every gateway's settings reach Hermes the same way: BY PREFIX, not by name. Hermes owns these names
# (WHATSAPP_*, TELEGRAM_*, DISCORD_*, SIGNAL_*, SLACK_*, BLUEBUBBLES_*), so forwarding by prefix means
# no deployer waits on this script to learn a new key name — and no channel is special-cased here.
# WHATSAPP_ENABLED rides the same loop because step 0.4 exported it. This adds no Sotto variable: set
# none of them and the loop does nothing. Pair it with SOTTO_CRON_DELIVER=<channel> (where the briefs
# go). See CHANNELS.md.
while IFS='=' read -r gwk gwv; do
  [ -n "$gwk" ] || continue
  upsert_env "$gwk" "$gwv"
  echo "[sotto] gateway variable forwarded to Hermes: $gwk"
done < <(env | grep -E '^(WHATSAPP|TELEGRAM|DISCORD|SIGNAL|SLACK|BLUEBUBBLES)_[A-Za-z0-9_]*=' || true)

# 3.7) Google Workspace auth — DETERMINISTIC + headless. Doing this through the agent breaks: every
#      `--auth-url` mints a NEW PKCE verifier, so a re-run invalidates a code you got from an earlier URL
#      ("Invalid code verifier"). Here `--auth-url` runs at most ONCE (guarded by the pending file), and
#      `--auth-code` runs once against that same persisted verifier. Set GOOGLE_OAUTH_CLIENT_JSON (the
#      Desktop OAuth client JSON contents) in Railway; authorize at /google/auth; set GOOGLE_AUTH_CODE and
#      redeploy. Token persists on /data and auto-refreshes.
GAUTH_URL_FILE="${SOTTO_DATA:-/data}/google-auth-url.txt"
if [ -n "${GOOGLE_OAUTH_CLIENT_JSON:-}" ]; then
  # Same search bases as receiver._google_setup_py — keep the two in step. (/root/.hermes is not
  # listed: HOME is /root in this image, so "$HOME/.hermes" already covers it.)
  GSETUP_PY=$(find "$HOME/.hermes" /usr/local/lib/hermes-agent -path '*google-workspace/scripts/setup.py' 2>/dev/null | head -1)
  PYBIN=$(command -v python || command -v python3)
  if [ -z "$GSETUP_PY" ]; then
    echo "[sotto] Google: setup.py not found (google-workspace skill missing?) — skipping."
  elif "$PYBIN" "$GSETUP_PY" --check >/dev/null 2>&1; then
    echo "[sotto] Google: already connected ✓"
    rm -f "$GAUTH_URL_FILE" 2>/dev/null || true
  else
    CS="$HOME/.hermes/google_client_secret.json"
    printf '%s' "$GOOGLE_OAUTH_CLIENT_JSON" > "$CS"
    "$PYBIN" "$GSETUP_PY" --client-secret "$CS" >/dev/null 2>&1 || true
    if [ -n "${GOOGLE_AUTH_CODE:-}" ]; then
      echo "[sotto] Google: exchanging auth code…"
      if "$PYBIN" "$GSETUP_PY" --auth-code "$GOOGLE_AUTH_CODE" --format json; then
        echo "[sotto] Google: connected ✓  (now clear GOOGLE_AUTH_CODE from Railway)"
        rm -f "$GAUTH_URL_FILE" 2>/dev/null || true
      else
        echo "[sotto] Google: code exchange FAILED — unset GOOGLE_AUTH_CODE, redeploy for a fresh URL, retry."
      fi
    else
      # No code yet. Generate the URL ONCE (only if there's no pending verifier), else reuse it.
      if [ ! -f "$HOME/.hermes/google_oauth_pending.json" ]; then
        echo "[sotto] Google: generating auth URL (one time)…"
        "$PYBIN" "$GSETUP_PY" --auth-url --services email,calendar --format json || true
      fi
      [ -f "$HOME/.hermes/google_oauth_last_url.txt" ] && cp "$HOME/.hermes/google_oauth_last_url.txt" "$GAUTH_URL_FILE" 2>/dev/null || true
      if [ -n "${RAILWAY_PUBLIC_DOMAIN:-}" ]; then
        GQS="$(setup_qs)"
        echo "[sotto] ➜ Authorize Google: https://${RAILWAY_PUBLIC_DOMAIN}/google/auth${GQS}"
        [ -n "$GQS" ] || echo "[sotto]   (if that says Forbidden, open the [sotto] Setup link from these logs first)"
      fi
    fi
  fi
fi

# 3.8) Granola (optional). Preferred path: the "Connected services" tile on the /setup wizard — the
#       receiver runs OAuth 2.1 DCR+PKCE against Granola's remote MCP and stores the token at
#       $SOTTO_DATA/connectors/granola.json, where the deterministic gathers pick it up headlessly
#       (see INTEGRATIONS.md). GRANOLA_MCP_CMD remains as the custom-stdio-server escape hatch.
if [ -f "${SOTTO_DATA:-/data}/connectors/granola.json" ]; then
  echo "[sotto] granola connector: linked"
elif [ -n "${GRANOLA_MCP_CMD:-}" ]; then
  read -ra GTOK <<< "$GRANOLA_MCP_CMD"
  GARGS=()
  for a in "${GTOK[@]:1}"; do GARGS+=("--arg=$a"); done   # =form handles args starting with '-'
  if python3 /app/adapters/hermes/configure_mcp.py --name granola --command "${GTOK[0]}" "${GARGS[@]}" \
       --env "GRANOLA_API_TOKEN=${GRANOLA_API_TOKEN:-}" \
       --env "ACAI_GRANOLA_API_TOKEN=${GRANOLA_API_TOKEN:-}" \
       --env "GRANOLA_DOCUMENT_SOURCE=remote" \
       --config "$HOME/.hermes/config.yaml"; then
    echo "[sotto] Granola MCP registered (custom server, cmd: $GRANOLA_MCP_CMD)."
  fi
else
  echo "[sotto] granola: connect it from /setup (Connected services)"
fi

# 4) (Trigger receiver already started in step 0.5 so /health is up immediately.)

# 5) Link the delivery channel BEFORE the gateway — both channels need their identity in
#    ~/.hermes/.env before `hermes gateway` starts, and both are bounded waits that boot survives.
#
# 5a) Telegram: you paste a bot token, tap the pairing link the linker prints, and this captures the
#     chat id. The handshake has ONE owner — trigger-receiver/telegram_link.py (`--boot` = reuse the
#     id on the volume, else capture and persist it); nothing about the Bot API is re-implemented
#     here. Skipped when you set TELEGRAM_ALLOWED_USERS yourself (explicit configuration wins) and
#     after the first capture. The wait is the linker's own LINK_TIMEOUT_SECS — one named timeout, no
#     second env var — and a timeout is NOT fatal: boot carries on, the brief still composes, and the
#     next boot tries again.
#     A bot's @username is discoverable, so the capture only accepts a message carrying this deploy's
#     SETUP CODE — the one secret that already gates /setup, passed in explicitly so the linker never
#     has to reach into the receiver. No code resolved yet, no capture: linking a stranger is worse
#     than not linking at all.
if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -z "${TELEGRAM_ALLOWED_USERS:-}" ]; then
  # Before anything else, including the reason we might not link at all: an id the PREVIOUS token
  # captured may not survive a boot that fails to re-link, or it reads as linked and starts a gateway
  # that eats the next pairing message. Re-added below only on success.
  drop_env TELEGRAM_ALLOWED_USERS
  drop_env TELEGRAM_HOME_CHANNEL
  TG_PHRASE="$(setup_code)"
  if [ -z "$TG_PHRASE" ]; then
    echo "[sotto] telegram NOT linked — no setup code resolved yet, and pairing without one would"
    echo "[sotto]   hand your briefs to whoever finds the bot first. Set SOTTO_SETUP_CODE, redeploy."
  else
    echo "[sotto] telegram: linking your chat — tap the link below (nothing to paste back)."
    TG_ID="$(python3 /app/trigger-receiver/telegram_link.py --token "$TELEGRAM_BOT_TOKEN" \
      --phrase "$TG_PHRASE" --boot || true)"
    if [ -n "$TG_ID" ]; then
      upsert_env TELEGRAM_ALLOWED_USERS "$TG_ID"
      upsert_env TELEGRAM_HOME_CHANNEL "$TG_ID"
      echo "[sotto] telegram linked ✓ — briefs and nudges deliver to chat $TG_ID"
    else
      echo "[sotto] telegram NOT linked yet — tap the pairing link above, then restart this deploy."
    fi
  fi
fi

# 5b) Pair WhatsApp. `hermes gateway` refuses to start unpaired ("WhatsApp enabled but
#    not paired") and exits — pairing is a SEPARATE command (`hermes whatsapp`) that prints a QR. On first
#    boot (no creds.json) we run it; scan the QR from the deploy logs (WhatsApp ▸ Linked Devices ▸ Link a
#    Device). creds.json lands in the /data-backed session dir, so later boots skip straight to the gateway.
#    Runs only when WHATSAPP_ENABLED is true (step 0.4: when WhatsApp is the channel, or you asked for it) —
#    it is the only way to pair WhatsApp on Railway, where there is no interactive shell.
if [ "$WHATSAPP_ENABLED" = "true" ] && [ ! -f "$WA_CREDS" ]; then
  echo "[sotto] WhatsApp not paired — starting pairing."
  if [ -n "${RAILWAY_PUBLIC_DOMAIN:-}" ]; then
    QRQS="$(setup_qs)"
    echo "[sotto] ➜ OPEN THIS TO SCAN A CLEAN QR:  https://${RAILWAY_PUBLIC_DOMAIN}/whatsapp/qr${QRQS}"
    [ -n "$QRQS" ] || echo "[sotto]   (if that says Forbidden, open the [sotto] Setup link from these logs first)"
  fi
  echo "[sotto] (a QR also prints below, but the web page renders it undistorted)."
  # `hermes whatsapp` needs an interactive terminal AND is a wizard (asks mode [1/2], then shows a QR).
  # wa_pair.py gives it a PTY, auto-answers the mode prompt (SOTTO_WHATSAPP_MODE, default 2 = self-chat),
  # and relays the QR to these logs. Override with SOTTO_WHATSAPP_MODE=1 for a separate bot number.
  python3 /app/adapters/hermes/wa_pair.py &
  WA_PID=$!
  # Wait for the scan as long as the pairer itself is allowed to run: derive the loop from the SAME
  # env var wa_pair.py honors (SOTTO_WHATSAPP_PAIR_TIMEOUT, default 900s), so raising it actually
  # buys more time — a fixed count here once silently capped the var at 15 min. Non-numeric → 900.
  WA_WAIT="${SOTTO_WHATSAPP_PAIR_TIMEOUT:-900}"
  case "$WA_WAIT" in ''|*[!0-9]*) WA_WAIT=900 ;; esac
  for _ in $(seq 1 $(( (WA_WAIT + 4) / 5 ))); do
    [ -f "$WA_CREDS" ] && { echo "[sotto] WhatsApp paired ✓"; break; }
    kill -0 "$WA_PID" 2>/dev/null || break
    sleep 5
  done
  kill "$WA_PID" 2>/dev/null || true
  pkill -f "whatsapp" 2>/dev/null || true   # stop any lingering external bridge so the gateway owns it
fi

# 6) Gateway (agent loop + gateway + scheduler), SUPERVISED.
#    A fresh Railway deploy briefly runs the new container alongside the old one. When the new
#    container's WhatsApp link replaces the old one's, the gateway can exit once on a "stream
#    conflict"/reconnect blip. As the container's main process, that single exit would fail the whole
#    deploy (crash email) even though a restart fixes it. So supervise it: retry a few times IN-PROCESS
#    (Railway sees one healthy container, no crash email) and forward SIGTERM so intentional redeploys
#    shut down cleanly. The receiver (step 0.5) keeps serving /health throughout.
#    (No reconnect watchdog needed in reverse mode: the relay's /mcp is always up locally, so Hermes
#    never loses the sotto-local binding — a sleeping Mac just means tool calls return "offline".)
# Re-print the setup link LAST: the receiver printed it in step 0.5, but ~400 lines of boot log +
# the ASCII QR bury it, and ONBOARDING tells users to find this exact line in the deploy logs.
# Same composition as receiver.py main(): Railway domain when public, else localhost:$PORT.
SQS="$(setup_qs)"
if [ -n "${RAILWAY_PUBLIC_DOMAIN:-}" ]; then
  echo "[sotto] Setup link (open in a browser): https://${RAILWAY_PUBLIC_DOMAIN}/setup${SQS}"
else
  echo "[sotto] Setup link (open in a browser): http://localhost:${PORT:-8787}/setup${SQS}"
fi
GW_PID=""
term() { [ -n "$GW_PID" ] && kill -TERM "$GW_PID" 2>/dev/null || true; exit 0; }
trap term TERM INT

# The gateway does not start for an unlinked Telegram. One sentence: an unlinked channel cannot
# deliver anything, and a running gateway long-polls getUpdates with the SAME bot token — so it would
# swallow the pairing message the next boot's capture is waiting for, and "tap the link, then
# restart" could never work. Telegram counts as linked once a chat id reached ~/.hermes/.env: the
# allowlist you set (step 3.5 forwards it), the id step 5a just captured, or one a previous boot
# wrote there — AND a bot token beside it, because a recipient with no bot delivers nothing. Every
# other channel starts the gateway as before.
START_GATEWAY=1
if [ "$SOTTO_CRON_DELIVER" = "telegram" ] \
   && { ! grep -q '^TELEGRAM_ALLOWED_USERS=.' "$ENVF" 2>/dev/null \
        || ! grep -q '^TELEGRAM_BOT_TOKEN=.' "$ENVF" 2>/dev/null; }; then
  START_GATEWAY=0
  echo "[sotto] telegram gateway NOT started — no chat is linked, so nothing can be delivered."
  echo "[sotto]   Your pairing message waits on Telegram's servers while it stays down: tap the"
  echo "[sotto]   pairing link above, then restart this deploy and the capture picks it up."
fi
# No gateway to supervise: hold the container open so /health, /setup and the briefs keep working.
if [ "$START_GATEWAY" != "1" ]; then
  # The receiver (step 0.5) is the only background job, and on this path it is the whole
  # service: /health, /setup, the briefs. Its exit status IS the container's — `exit 0` here
  # told Railway's ON_FAILURE policy that a dead receiver was a clean shutdown, so nothing
  # restarted it (Day-0 simulation, Sep 2026). The TERM trap still ends a deploy cleanly.
  rc=0; wait || rc=$?
  echo "[sotto] receiver exited ($rc) with no gateway to hold the container — exiting $rc"
  exit "$rc"
fi
gw_tries=0
while :; do
  hermes gateway & GW_PID=$!
  gw_code=0; wait "$GW_PID" || gw_code=$?
  [ "$gw_code" = "0" ] && { echo "[sotto] gateway exited cleanly"; break; }
  gw_tries=$((gw_tries + 1))
  if [ "$gw_tries" -ge 5 ]; then
    echo "[sotto] gateway exited ($gw_code) $gw_tries times — giving up so Railway can recycle the container"
    exit "$gw_code"
  fi
  echo "[sotto] gateway exited ($gw_code); restarting in 5s ($gw_tries/5)…"
  sleep 5
done
