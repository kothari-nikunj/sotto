# Cloud Hermes + Sotto (Railway/Render/Fly). Runs the agent, the skills (with their Python scripts),
# and the trigger receiver. A persistent volume mounts at /data ($SOTTO_DATA) for the exhaust.
#
# BUILD CONTEXT = the folder holding this Dockerfile (its COPY paths are relative to it). In a
# standalone Sotto repo this folder IS the repo root → Railway needs no Root Directory / Dockerfile
# Path at all (auto-detected). In the dailybrief monorepo, set Railway Root Directory = sotto-hermes.
FROM python:3.12-slim

# Prereqs for Hermes' installer (per Nous docs: git, curl, xz-utils; build tools; ripgrep/ffmpeg the
# agent uses) + tini as a proper init (reaps the receiver/pairing/bridge child processes and forwards
# signals). Without these the install.sh below fails — so we do NOT mask its exit code.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates xz-utils build-essential ripgrep ffmpeg openssh-client tini \
 && rm -rf /var/lib/apt/lists/*

# Install Hermes (Nous Research's official installer — also pulls Python/Node into its own runtime).
# No account/license needed; it just needs an LLM key at runtime (we pass GOOGLE_AI_API_KEY).
#
# HERMES_REFRESH is a pure cache-bust knob: Docker reuses this layer (and its baked Hermes) as long
# as the RUN line is byte-identical, so a routine code push does NOT upgrade Hermes. To pull the
# latest Hermes, bump the value to any new string (e.g. today's date) and redeploy — that invalidates
# the layer and re-runs the installer. See RAILWAY.md § Keeping Sotto up to date.
ARG HERMES_REFRESH=2026-07-09
RUN echo "hermes refresh: ${HERMES_REFRESH}" \
 && pip install --no-cache-dir pyyaml \
 && curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
# The installer puts `hermes` on PATH for the install user; make common locations explicit for start.sh.
ENV PATH="/root/.local/bin:/root/.hermes/bin:${PATH}"
# Snapshot what the INSTALLER owns inside ~/.hermes (captured BEFORE any Sotto skills are copied) and
# the Hermes version this image was built with. start.sh uses these to (a) print the running vs image
# version in every boot log and (b) refresh the installer-owned entries on the /data volume when
# SOTTO_REFRESH_HERMES=1 — without them, an upgraded image can be silently shadowed by the volume's
# first-boot copy of ~/.hermes.
RUN mkdir -p /app \
 && (ls -A /root/.hermes 2>/dev/null || true) > /app/hermes-image-manifest.txt \
 && { hermes --version 2>/dev/null || hermes version 2>/dev/null || echo unknown; } | head -1 > /app/hermes-image-version.txt

# Google Workspace client libs for the bundled google-workspace skill's google_api.py. WITHOUT these,
# `google_api.py gmail/calendar` dies with `ModuleNotFoundError: No module named 'googleapiclient'` even
# though the OAuth token is valid (setup.py --check only validates the token, not the client lib) — so
# briefs silently fall to local-only and the agent improvises `pip install` mid-run. Installed with
# `python3 -m pip` against the PATH python (the same interpreter execute_code/gather_google use) so the
# dep is actually importable where google_api.py runs. Baked into the image → always present, no
# per-run install, no improvisation.
RUN python3 -m pip install --no-cache-dir \
      google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2 \
 || pip install --no-cache-dir \
      google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2

ENV SOTTO_DATA=/data
RUN mkdir -p /data ~/.hermes/skills ~/.hermes/skill-bundles

# Sotto layer
COPY sotto-chief-of-staff/ /root/.hermes/skills/sotto/
COPY adapters/hermes/sotto.bundle.yaml /root/.hermes/skill-bundles/sotto.yaml
COPY runtime/trigger-receiver/ /app/trigger-receiver/
COPY adapters/hermes/ /app/adapters/hermes/

# Required at runtime (set as Railway/Render env, do NOT bake): GOOGLE_AI_API_KEY, SOTTO_TRIGGER_TOKEN,
# the gateway token, and the Bridge mcp url+bearer (write via configure_mcp.py on boot).
# Append the Sotto persona to SOUL.md and register the Bridge MCP if BRIDGE_URL/BRIDGE_TOKEN are set.
COPY adapters/hermes/sotto-persona.md /app/
RUN cat /app/sotto-persona.md >> /root/.hermes/SOUL.md 2>/dev/null || true

# Two processes: the trigger receiver (HTTP) + Hermes (agent loop + gateway + scheduler).
# Railway exposes $PORT → the receiver. Hermes runs alongside. tini is PID 1 so the background
# receiver/pairing/whatsapp-bridge children are reaped and SIGTERM is forwarded on redeploy.
COPY adapters/hermes/start.sh /app/start.sh
RUN chmod +x /app/start.sh
ENTRYPOINT ["tini", "--"]
CMD ["/app/start.sh"]
