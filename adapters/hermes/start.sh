#!/usr/bin/env bash
# Cloud boot: register the reverse-relay Bridge MCP, set the model + scheduler, start the trigger
# receiver and Hermes. Env (set on Railway/Render): GOOGLE_AI_API_KEY, BRIDGE_TOKEN (the Bridge's
# shared bearer), SOTTO_TRIGGER_TOKEN (optional wake-push), gateway token. Tunnel-free.
set -euo pipefail

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
if [ -f "$HSTATE/SOUL.md" ] && [ -f /app/sotto-persona.md ]; then
  sed -i '/chief-of-staff persona/,$d' "$HSTATE/SOUL.md" 2>/dev/null || true
  printf '\n' >> "$HSTATE/SOUL.md"
  cat /app/sotto-persona.md >> "$HSTATE/SOUL.md"
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
hermes config set model gemini-3-flash-preview || true
hermes config set scheduler.enabled true || true
# Timezone — Hermes cron + the system-prompt time injection default to UTC. Set the user's IANA zone so
# the 6:30/17:30 briefs fire at their LOCAL morning/evening, AND so `hermes cron create` below doesn't
# block on an interactive timezone PROMPT at boot (a non-interactive boot fails the prompt → NO cron
# created → no briefs). Set SOTTO_TIMEZONE in Railway (e.g. America/Los_Angeles); defaults to UTC.
# The setup WIZARD also captures the browser-detected zone to $SOTTO_DATA/config/settings.json, so the
# Railway var is OPTIONAL — fall back to it here (the cron hour then self-heals on the next boot).
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
    echo "[sotto] WARNING: SOTTO_TIMEZONE unset and no wizard zone yet — cron briefs fire in UTC until you"
    echo "[sotto]          finish setup at /setup (auto-detects your zone) or set SOTTO_TIMEZONE in Railway."
  fi
fi
hermes config set timezone "${SOTTO_TIMEZONE:-UTC}" || true
# Brief composition runs the FLEX extraction AND a critic pass inside ONE execute_code call — two
# Gemini calls that together can exceed Hermes' default 300s code_execution timeout, getting the
# script KILLED mid-run (after which the agent improvises a freehand, low-quality brief). Raise the
# ceiling so a 2–4 min brief always finishes. (The desktop brief took 2+ min; this matches.)
hermes config set code_execution.timeout 600 || true
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
# (one per external attendee). Lift the concurrency cap from the default 3 so a meeting-heavy day's
# research doesn't serialize. Override with SOTTO_RESEARCH_CONCURRENCY.
hermes config set delegation.max_concurrent_children "${SOTTO_RESEARCH_CONCURRENCY:-5}" >/dev/null 2>&1 || true
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
SOTTO_CRON_DELIVER="${SOTTO_CRON_DELIVER:-whatsapp}"   # platform-only → uses WHATSAPP_HOME_CHANNEL
python3 - <<'PY' || echo "[sotto] cron dedup skipped (parse/list error)"
import re, subprocess
MARKERS = ("sotto-morning-brief", "sotto-evening-brief", "sotto-relationship-pulse", "sotto-proactive",
           "sotto-followup",
           "Run my morning brief", "Run my evening brief", "Run my relationship pulse",
           "Run my proactive check", "Run my followup")
try:
    out = subprocess.run(["hermes", "cron", "list"], capture_output=True, text=True, timeout=60).stdout
except Exception as e:
    print(f"[sotto] cron dedup: `cron list` failed: {e}"); raise SystemExit(0)
# Job ids in `cron list` are hex (12-char, or a full uuid). Treat the text from each id to the next as
# that job's block; if the block names a sotto skill/prompt, the job is ours → remove it.
ID = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{12,})\b")
ms = list(ID.finditer(out))
ids, seen = [], set()
for i, m in enumerate(ms):
    block = out[m.start():(ms[i + 1].start() if i + 1 < len(ms) else len(out))]
    jid = m.group(1)
    if any(mk in block for mk in MARKERS) and jid not in seen:
        seen.add(jid); ids.append(jid)
print(f"[sotto] cron dedup: removing {len(ids)} existing sotto job(s) before recreating")
for jid in ids:
    try:  # answer any "are you sure?" prompt non-interactively; never hang the boot
        subprocess.run(["hermes", "cron", "remove", jid], input="y\ny\n",
                       capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"[sotto] cron dedup: remove {jid} failed: {e}")
PY
# Recreate exactly one of each. The `case` guard is a backstop: if dedup above failed to parse the
# list, this still avoids ADDING fresh dupes (it just can't fix a stale "local" deliver until dedup
# works). Stable --name makes future removes/edits addressable by name. Times use the tz set above.
crons="$(hermes cron list 2>/dev/null || true)"
case "$crons" in *sotto-morning-brief*|*"Run my morning brief"*) ;; *) hermes cron create "30 6 * * *"  "Run my morning brief"  --skill sotto-morning-brief --name sotto-morning-brief --deliver "$SOTTO_CRON_DELIVER" 2>&1 | sed 's/^/[sotto] cron-create morning: /' || true ;; esac
case "$crons" in *sotto-evening-brief*|*"Run my evening brief"*) ;; *) hermes cron create "30 17 * * *" "Run my evening brief"  --skill sotto-evening-brief --name sotto-evening-brief --deliver "$SOTTO_CRON_DELIVER" 2>&1 | sed 's/^/[sotto] cron-create evening: /' || true ;; esac
case "$crons" in *sotto-relationship-pulse*|*"relationship pulse"*) ;; *) hermes cron create "0 9 * * 1" "Run my relationship pulse" --skill sotto-relationship-pulse --name sotto-relationship-pulse --deliver "$SOTTO_CRON_DELIVER" 2>&1 | sed 's/^/[sotto] cron-create pulse: /' || true ;; esac
# Proactiveness Phase 1: a frequent, mostly-SILENT watcher that nudges only on time-sensitive items
# (meeting about to start, commitment due, birthday) with a draft ready — never auto-sends. Conservative
# by default (quiet hours + lead window + dedup live in proactive_scan.py). Default ON; SOTTO_PROACTIVE=0
# disables it (the dedup above still removes a stale one). Interval via SOTTO_PROACTIVE_CRON.
if [ "${SOTTO_PROACTIVE:-1}" = "1" ]; then
  case "$crons" in *sotto-proactive*|*"Run my proactive check"*) ;; *) hermes cron create "${SOTTO_PROACTIVE_CRON:-*/15 * * * *}" "Run my proactive check" --skill sotto-proactive --name sotto-proactive --deliver "$SOTTO_CRON_DELIVER" 2>&1 | sed 's/^/[sotto] cron-create proactive: /' || true ;; esac
fi
# Post-meeting follow-up: a light EVENING cron (16:45 local — after the workday, before the evening
# brief) so follow-ups run without being asked. It processes only meetings that ENDED since its last
# run (followup_cron.py windows + marks the state) and stays SILENT when nothing is actionable — never
# auto-sends a draft. Default on; SOTTO_FOLLOWUP=0 disables it. Time via SOTTO_FOLLOWUP_CRON.
if [ "${SOTTO_FOLLOWUP:-1}" = "1" ]; then
  case "$crons" in *sotto-followup*|*"Run my followup"*) ;; *) hermes cron create "${SOTTO_FOLLOWUP_CRON:-45 16 * * *}" "Run my followup" --skill sotto-followup --name sotto-followup --deliver "$SOTTO_CRON_DELIVER" 2>&1 | sed 's/^/[sotto] cron-create followup: /' || true ;; esac
fi
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
GMODEL="${SOTTO_GEMINI_MODEL:-gemini-3-flash-preview}"
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

# 3.7) Google Workspace auth — DETERMINISTIC + headless. Doing this through the agent breaks: every
#      `--auth-url` mints a NEW PKCE verifier, so a re-run invalidates a code you got from an earlier URL
#      ("Invalid code verifier"). Here `--auth-url` runs at most ONCE (guarded by the pending file), and
#      `--auth-code` runs once against that same persisted verifier. Set GOOGLE_OAUTH_CLIENT_JSON (the
#      Desktop OAuth client JSON contents) in Railway; authorize at /google/auth; set GOOGLE_AUTH_CODE and
#      redeploy. Token persists on /data and auto-refreshes.
GAUTH_URL_FILE="${SOTTO_DATA:-/data}/google-auth-url.txt"
if [ -n "${GOOGLE_OAUTH_CLIENT_JSON:-}" ]; then
  GSETUP_PY=$(find "$HOME/.hermes" /usr/local/lib/hermes-agent /root/.hermes -path '*google-workspace/scripts/setup.py' 2>/dev/null | head -1)
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

# 3.8) Granola MCP (optional). Granola has no official public API; headless/remote MCPs are community
#       and fragile, and their token env-var name varies. So we register whatever stdio server you point
#       GRANOLA_MCP_CMD at (e.g. "uvx some-granola-mcp" or "acai serve"), passing your token under the
#       common names. Set GRANOLA_API_TOKEN + GRANOLA_MCP_CMD in Railway. See RAILWAY.md for options.
if [ -n "${GRANOLA_API_TOKEN:-}" ]; then
  if [ -n "${GRANOLA_MCP_CMD:-}" ]; then
    read -ra GTOK <<< "$GRANOLA_MCP_CMD"
    GARGS=()
    for a in "${GTOK[@]:1}"; do GARGS+=("--arg=$a"); done   # =form handles args starting with '-'
    if python3 /app/adapters/hermes/configure_mcp.py --name granola --command "${GTOK[0]}" "${GARGS[@]}" \
         --env "GRANOLA_API_TOKEN=$GRANOLA_API_TOKEN" \
         --env "ACAI_GRANOLA_API_TOKEN=$GRANOLA_API_TOKEN" \
         --env "GRANOLA_DOCUMENT_SOURCE=remote" \
         --config "$HOME/.hermes/config.yaml"; then
      echo "[sotto] Granola MCP registered (cmd: $GRANOLA_MCP_CMD)."
    fi
  else
    echo "[sotto] Granola: GRANOLA_API_TOKEN set but GRANOLA_MCP_CMD is not — set it to a remote Granola"
    echo "[sotto]          MCP server command (Granola has no official headless API; see RAILWAY.md)."
  fi
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
