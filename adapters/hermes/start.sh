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
# a 429/5xx/timeout on gemini-3.7-flash retries on gemini-3-flash-preview (cheaper: $0.50/$3.00 vs
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

# 0.5) Start the trigger receiver IMMEDIATELY so Railway's /health healthcheck passes within seconds —
#      before the slower boot steps below (Google auth makes network calls). Otherwise a slow first boot
#      can time out the healthcheck and Railway marks the deploy crashed. The receiver only needs $PORT
#      + $SOTTO_DATA (the volume), not Hermes — safe to start first. (`hermes -z` = the scriptable
#      one-shot the receiver uses to run a brief; there is no `hermes run`.)
# SOTTO_MCP_TOKEN lets the receiver's reverse-MCP relay authenticate the Mac's outbound link + Hermes'
# /mcp calls. Reuse BRIDGE_TOKEN so there's one secret to set.
SOTTO_MCP_TOKEN="${BRIDGE_TOKEN:-}" SOTTO_RUN_SKILL="hermes -z" python3 /app/trigger-receiver/receiver.py &

# The receiver gates its whole setup surface (/setup, /whatsapp/qr, /google/auth, /debug/google…)
# behind a per-deploy setup code — a bare URL now 403s. Any setup link WE print must carry
# ?code=<code> (env override, else the code the receiver persists to the volume at boot).
setup_qs() {
  local code="${SOTTO_SETUP_CODE:-}"
  if [ -z "$code" ]; then
    code="$(cat "${SOTTO_DATA:-/data}/setup_code" 2>/dev/null | tr -d '[:space:]' || true)"
  fi
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
  python3 /app/adapters/hermes/configure_mcp.py --url "http://127.0.0.1:${PORT:-8787}/mcp" \
    --token "$BRIDGE_TOKEN" --config "$HOME/.hermes/config.yaml"
  echo "[sotto] sotto-local → reverse relay (tunnel-free); the Mac dials out to /bridge/poll."
fi

# 2) Model + scheduler (dedicated cloud instance → Gemini 1M as the driver too).
#    Use the NATIVE Gemini model id (not the OpenRouter-style "google/…", which would route via
#    OpenRouter and need OPENROUTER_API_KEY). The key is set as GEMINI_API_KEY/GOOGLE_API_KEY below.
hermes config set model gemini-3.7-flash || true
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
# Progress UX. We want what the Mac app showed: human-readable phase updates ("Pulling your calendar…",
# "Researching the people you're meeting…"), NOT raw tool spam ("execute_code", "pip install",
# "iteration N/60"), and NOT dead silence for 2–3 min. Two levers:
#  • interim_assistant_messages=on → the agent's own plain-language narration streams LIVE (this is the
#    Mac-app-style progress; the skills are instructed to narrate each phase).
#  • tool_progress=new → a lightweight heartbeat so it's never silent even if the model under-narrates;
#    `accumulate` keeps it to ONE edit-in-place bubble, and cleanup_progress deletes it once the brief
#    lands — so the end state is just the narration + the brief. Set SOTTO_TOOL_PROGRESS=off for
#    narration-only (no tool bubble), or =all/verbose for debugging.
hermes config set display.interim_assistant_messages true >/dev/null 2>&1 || true

# Hermes's daily session reset (mode/idle_minutes/at_hour — no silence key in any version we've
# seen) BROADCASTS "◐ Session automatically reset…" to the home channel when it fires. Sotto keeps
# no memory in chat history, so the reset itself is good hygiene — but at the default 4:00 the
# notice stands alone in the middle of the night. Move it to 6:00: the notice attaches to the next
# session activity (the 6:30 brief run), which buries it, and the day's first run starts fresh.
hermes_set_if_supported session_reset.at_hour 6
TP="${SOTTO_TOOL_PROGRESS:-new}"
hermes config set display.tool_progress "$TP" >/dev/null 2>&1 || true
hermes config set display.tool_progress_grouping accumulate >/dev/null 2>&1 || true
for k in whatsapp telegram discord; do
  hermes config set "display.platforms.$k.cleanup_progress" true >/dev/null 2>&1 || true
done
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
# fully idempotent + self-healing. Recreation also sets a stable --name and --deliver target so the
# briefs go to the WhatsApp home channel instead of the default "local" (which never reaches the user).
#
# ONE SOURCE: the job list lives in adapters/hermes/crons.json (name/schedule/prompt/skill, plus an
# optional `gate` env var and `schedule_env` override). Every registrar reads that file — this boot,
# receiver.py's _sotto_cron_jobs (the timezone re-registration) and both adapters' install.sh — so a
# schedule can never drift between them again. `--deliver` is NOT per job: SOTTO_CRON_DELIVER below
# is the one delivery target for all of them.
CRONS_JSON="${SOTTO_CRONS_JSON:-/app/adapters/hermes/crons.json}"
SOTTO_CRON_DELIVER="${SOTTO_CRON_DELIVER:-whatsapp}"   # platform-only → uses WHATSAPP_HOME_CHANNEL
cron_rows() {   # name<TAB>schedule<TAB>prompt<TAB>skill for each job whose env gate is ON
  python3 -c 'import json, os, sys
for j in json.load(open(sys.argv[1])):
    if j.get("gate") and os.environ.get(j["gate"], "1") != "1":
        continue
    sched = os.environ.get(j.get("schedule_env") or "", "") or j["schedule"]
    print("\t".join([j["name"], sched, j["prompt"], j["skill"]]))' "$CRONS_JSON"
}
#
# THE USER-ROUTINE FENCE: personal routines (the `sotto-routines` skill) are registered as crons named
# `user-<slug>` and are NOT ours to remove or to be shadowed by. This cleanup exists to de-duplicate
# the crons.json SYSTEM jobs; a user routine must survive every redeploy untouched. Both halves below
# are fenced — the removal loop skips any block carrying a `user-` name, and the recreate guard reads a
# SYSTEM-ONLY view of the list (its `case` also matches on prompt text, so a routine quoting a system
# prompt would otherwise suppress that system job forever). System names never start with `user-`.
CRON_LIST_SYSTEM="$(mktemp 2>/dev/null || echo /tmp/sotto-cron-system.txt)"
python3 - "$CRONS_JSON" "$CRON_LIST_SYSTEM" <<'PY' || echo "[sotto] cron dedup skipped (parse/list error)"
import json, re, subprocess, sys
# Every name/prompt in crons.json (gates IGNORED here — a job turned OFF must still be removed),
# plus the RETIRED registrations older deploys may still carry.
MARKERS = ["sotto-followup", "Run my followup"]
try:
    for j in json.load(open(sys.argv[1])):
        MARKERS += [j["name"], j["prompt"]]
except Exception as e:
    print(f"[sotto] cron dedup: could not read {sys.argv[1]}: {e}"); raise SystemExit(0)


def cron_list():
    return subprocess.run(["hermes", "cron", "list"], capture_output=True, text=True,
                          timeout=60).stdout


try:
    out = cron_list()
except Exception as e:
    print(f"[sotto] cron dedup: `cron list` failed: {e}"); raise SystemExit(0)
# Job ids in `cron list` are hex (12-char, or a full uuid). Treat the text from each id to the next as
# that job's block; if the block names a sotto skill/prompt, the job is ours → remove it.
ID = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,})\b")
# A `user-` name anywhere in a block fences the whole block off (the FENCE above). Deliberately
# fail-safe: the worst case of a false positive is one un-deduped system job; the worst case of a
# false negative is silently deleting something the user asked for.
USER_FENCE = re.compile(r"(?<![A-Za-z0-9_-])user-[a-z0-9]")


def blocks(text):
    ms = list(ID.finditer(text))
    for i, m in enumerate(ms):
        yield m.group(1), text[m.start():(ms[i + 1].start() if i + 1 < len(ms) else len(text))]


ids, seen, fenced = [], set(), 0
for jid, block in blocks(out):
    if USER_FENCE.search(block):
        fenced += 1; continue                     # a personal routine — never ours to remove
    if any(mk in block for mk in MARKERS) and jid not in seen:
        seen.add(jid); ids.append(jid)
print(f"[sotto] cron dedup: removing {len(ids)} existing sotto job(s) before recreating"
      + (f"; leaving {fenced} user routine(s) alone" if fenced else ""))
for jid in ids:
    try:  # answer any "are you sure?" prompt non-interactively; never hang the boot
        subprocess.run(["hermes", "cron", "remove", jid], input="y\ny\n",
                       capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"[sotto] cron dedup: remove {jid} failed: {e}")
# Post-dedup, SYSTEM-ONLY listing for the recreate guard below. Always written (leading marker line,
# so an empty-but-present file is distinguishable from "the dedup never ran" — see the [ -s ] test).
try:
    keep = "".join(b for _, b in blocks(cron_list()) if not USER_FENCE.search(b))
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write("# sotto: system-only view of `hermes cron list` (user- routines fenced out)\n" + keep)
except Exception as e:
    print(f"[sotto] cron dedup: system-only list unavailable ({e}) — recreate guard uses the raw list")
PY
# Recreate exactly one of each gate-ON job from crons.json. The `case` guard is a backstop: if dedup
# above failed to parse the list, this still avoids ADDING fresh dupes (it just can't fix a stale
# "local" deliver until dedup works). Stable --name makes future removes/edits addressable by name.
# Times use the tz set above. Gates (crons.json `gate`): SOTTO_PROACTIVE=0 drops the mostly-silent
# ~15-min nudge watcher; SOTTO_DIGEST=0 drops the adaptive 12:30 catch-up digest — the dedup above
# still removes a stale registration of either. Post-meeting follow-up cron RETIRED (Sprint 0): its
# content now runs inside the 17:30 evening brief, and "sotto-followup"/"Run my followup" stay in the
# dedup MARKERS so registrations from earlier deploys are removed on every boot.
# The list it matches against is the fenced, system-only view when the dedup produced one; only when
# that step failed outright do we fall back to the raw list (backstop beats fence in that corner).
if [ -s "$CRON_LIST_SYSTEM" ]; then
  crons="$(cat "$CRON_LIST_SYSTEM")"
else
  crons="$(hermes cron list 2>/dev/null || true)"
fi
rm -f "$CRON_LIST_SYSTEM" 2>/dev/null || true
cron_rows | while IFS="$(printf '\t')" read -r cname csched cprompt cskill; do
  [ -n "$cname" ] || continue
  case "$crons" in *"$cname"*|*"$cprompt"*) continue ;; esac
  hermes cron create "$csched" "$cprompt" --skill "$cskill" --name "$cname" \
    --deliver "$SOTTO_CRON_DELIVER" 2>&1 | sed "s|^|[sotto] cron-create $cname: |" || true
done
# Dump the registered crons so cron is OBSERVABLE (empty list, UTC next-run, or "Deliver: local" are
# all bugs visible at a glance). Capped with `head` — the old uncapped dump of dozens of dupes hit
# Railway's 500-logs/sec limit ("Messages dropped"). After dedup it's ~3 jobs, so the cap rarely bites.
echo "[sotto] cron scheduler: $(hermes cron status 2>/dev/null | head -1 || echo '?') tz=${SOTTO_TIMEZONE:-UTC} deliver=${SOTTO_CRON_DELIVER}; registered crons:"
hermes cron list 2>&1 | head -40 | sed 's/^/[sotto]   /' || echo "[sotto]   (hermes cron list failed)"

# 3.5) Enable the WhatsApp gateway NON-INTERACTIVELY. Hermes reads messaging-platform settings from
#      ~/.hermes/.env (NOT config.yaml), and denies all users until an allowlist is set — without this
#      the gateway logs "No messaging platforms enabled". We upsert the keys from Railway env each boot
#      so Railway stays the source of truth. Set WHATSAPP_ALLOWED_USERS (and WHATSAPP_HOME_CHANNEL for
#      proactive brief delivery) to your number, e.g. 15551234567, in Railway → Variables.
ENVF="$HOME/.hermes/.env"
touch "$ENVF"
upsert_env() {  # replace any existing KEY= line, then append the new value
  grep -v "^$1=" "$ENVF" > "$ENVF.tmp" 2>/dev/null || true
  mv "$ENVF.tmp" "$ENVF"
  printf '%s=%s\n' "$1" "$2" >> "$ENVF"
}
upsert_env WHATSAPP_ENABLED "${WHATSAPP_ENABLED:-true}"
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
GMODEL="${SOTTO_GEMINI_MODEL:-gemini-3.7-flash}"
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
[ -n "${WHATSAPP_ALLOWED_USERS:-}" ] && upsert_env WHATSAPP_ALLOWED_USERS "$WHATSAPP_ALLOWED_USERS"
[ -n "${WHATSAPP_HOME_CHANNEL:-}" ]  && upsert_env WHATSAPP_HOME_CHANNEL "$WHATSAPP_HOME_CHANNEL"
[ -n "${GATEWAY_ALLOW_ALL_USERS:-}" ] && upsert_env GATEWAY_ALLOW_ALL_USERS "$GATEWAY_ALLOW_ALL_USERS"
# WhatsApp is the DEFAULT channel, not the only one — but until now it was the only one whose
# settings reached Hermes: every other gateway's variables sat in the Railway environment and were
# never written to ~/.hermes/.env, which is where Hermes reads messaging-platform settings from. So
# forward the rest the same way, BY PREFIX rather than by name — Hermes owns these names
# (TELEGRAM_*, DISCORD_*, SIGNAL_*, SLACK_*, BLUEBUBBLES_*), and forwarding by prefix means a
# Telegram deployer never waits on this script to learn a new key name. This adds no Sotto variable
# and changes nothing for a WhatsApp deploy: set none of them and the loop does nothing. Pair it
# with SOTTO_CRON_DELIVER=<channel> (where the briefs go) and, if you don't want the WhatsApp
# pairing step at all, WHATSAPP_ENABLED=false. See CHANNELS.md.
while IFS='=' read -r gwk gwv; do
  [ -n "$gwk" ] || continue
  upsert_env "$gwk" "$gwv"
  echo "[sotto] gateway variable forwarded to Hermes: $gwk"
done < <(env | grep -E '^(TELEGRAM|DISCORD|SIGNAL|SLACK|BLUEBUBBLES)_[A-Za-z0-9_]*=' || true)

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

# 5) Pair WhatsApp BEFORE the gateway. `hermes gateway` refuses to start unpaired ("WhatsApp enabled but
#    not paired") and exits — pairing is a SEPARATE command (`hermes whatsapp`) that prints a QR. On first
#    boot (no creds.json) we run it; scan the QR from the deploy logs (WhatsApp ▸ Linked Devices ▸ Link a
#    Device). creds.json lands in the /data-backed session dir, so later boots skip straight to the gateway.
WA_CREDS="$HOME/.hermes/platforms/whatsapp/session/creds.json"
if [ "${WHATSAPP_ENABLED:-true}" = "true" ] && [ ! -f "$WA_CREDS" ]; then
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
  for _ in $(seq 1 180); do    # up to ~15 min to scan, or until the pairing process exits / creds appear
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
