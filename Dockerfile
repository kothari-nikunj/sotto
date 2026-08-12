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
      git curl ca-certificates xz-utils build-essential ripgrep ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

# Install Hermes (Nous Research's official installer — also pulls Python/Node into its own runtime).
# No account/license needed; it just needs an LLM key at runtime (we pass GOOGLE_AI_API_KEY).
#
# HERMES_REFRESH is a pure cache-bust knob: Docker reuses this layer (and its baked Hermes) as long
# as the RUN line is byte-identical, so a routine code push does NOT upgrade Hermes. To pull the
# latest Hermes, bump the value to any new string (e.g. today's date) and redeploy — that invalidates
# the layer and re-runs the installer. See RAILWAY.md § Staying updated.
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

# Sotto layer. ONE copy of the Hermes adapter: /app/adapters/hermes/ holds start.sh, the persona,
# crons.json, configure_mcp.py and wa_pair.py, and every consumer reads them from there (start.sh
# already referenced its siblings by that path). No second copy at /app/ to drift from it.
COPY sotto-chief-of-staff/ /root/.hermes/skills/sotto/
COPY adapters/hermes/sotto.bundle.yaml /root/.hermes/skill-bundles/sotto.yaml
COPY runtime/trigger-receiver/ /app/trigger-receiver/
COPY adapters/hermes/ /app/adapters/hermes/
# The two interactive playgrounds live in docs/ (one source of truth) and are SERVED from
# /static/* — so they are copied in beside the frontend assets at build time. That keeps
# dashboard.py's whitelist an exact-name lookup against a single root; the alternative (a second
# static root resolved at runtime) has no single relative path that is correct both here and in
# the source tree. Whitelisted in dashboard.STATIC_FILES; they carry no user data.
COPY docs/playground-architecture.html docs/playground-feedback-loops.html /app/trigger-receiver/static/
# Which published build this image IS: `YYYY-MM-DD.<short-sha>`, stamped into the distribution tree
# by tools/prepare-public-repo.sh on every publish. The receiver compares it against the same file
# in the public repo and flags an available update on the Integrations page (RAILWAY.md § Staying
# updated). In the monorepo the file reads `dev` — an unstamped build never checks and never flags.
COPY VERSION /app/VERSION

# Required at runtime (set as Railway/Render env, do NOT bake): GOOGLE_AI_API_KEY, SOTTO_TRIGGER_TOKEN,
# the gateway token, and BRIDGE_TOKEN (the reverse-relay bearer configure_mcp.py registers on boot).
# Seed the Sotto persona into SOUL.md at build time; start.sh refreshes it on the volume every boot.
RUN cat /app/adapters/hermes/sotto-persona.md >> /root/.hermes/SOUL.md 2>/dev/null || true

# Two processes: the trigger receiver (HTTP) + Hermes (agent loop + gateway + scheduler).
# Railway exposes $PORT → the receiver. Hermes runs alongside. tini is PID 1 so the background
# receiver/pairing/whatsapp-bridge children are reaped and SIGTERM is forwarded on redeploy.
RUN chmod +x /app/adapters/hermes/start.sh
ENTRYPOINT ["tini", "--"]
CMD ["/app/adapters/hermes/start.sh"]
