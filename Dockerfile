# Cloud Hermes + Sotto (Railway/Render/Fly). Runs the agent, the skills (with their Python scripts),
# and the trigger receiver. A persistent volume mounts at /data ($SOTTO_DATA) for the exhaust.
#
# BUILD CONTEXT = the folder holding this Dockerfile (its COPY paths are relative to it). In a
# standalone Sotto repo this folder IS the repo root → Railway needs no Root Directory / Dockerfile
# Path at all (auto-detected). In the dailybrief monorepo, set Railway Root Directory = sotto-hermes.

# Base image: pinned to an EXACT patch tag *and* its multi-arch manifest digest. The tag says which
# Python this is for a human; the digest is what actually gets pulled, so a rebuilt image is the same
# bytes even after upstream re-tags `3.12-slim-bookworm` at the next patch. Both were resolved from
# Docker Hub on 2026-08-12 (`docker-content-digest` for python:3.12.13-slim-bookworm, which was also
# what the floating 3.12-slim-bookworm tag pointed at that day).
# To move to a newer Python: change BOTH halves together — a tag without its matching digest is a lie
# in the file, and the digest is what wins.
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2

# ROOT, deliberately, and this is a KNOWN GAP rather than an oversight: the Hermes installer below
# writes to /root/.hermes, and the skills, the bundle, SOUL.md, start.sh and the volume's first-boot
# copy all address that path. Running as a non-root user means relocating the agent's entire home,
# which is upstream's layout, not ours to redefine — so the container runs as root and the mitigation
# lives elsewhere (the process holds no host mounts beyond /data, and the platform isolates it).
# Whoever revisits this: it is one migration — a `USER sotto` with $HOME=/home/sotto — and it must be
# done for the installer, the skills tree, and start.sh in the SAME change, or it half-works.

# Prereqs for Hermes' installer (per Nous docs: git, curl, xz-utils; build tools; ripgrep/ffmpeg the
# agent uses) + tini as a proper init (reaps the receiver/pairing/bridge child processes and forwards
# signals). Without these the install.sh below fails — so we do NOT mask its exit code.
# `--no-install-recommends` keeps the list to exactly what is named here.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates xz-utils build-essential ripgrep ffmpeg tini \
 && rm -rf /var/lib/apt/lists/*

# Install Hermes (Nous Research's official installer — also pulls Python/Node into its own runtime).
# No account/license needed; it just needs an LLM key at runtime (we pass GOOGLE_AI_API_KEY).
#
# The installer is VENDORED at adapters/hermes/hermes-install.sh — the build never fetches
# https://hermes-agent.nousresearch.com/install.sh. Upstream serves that URL MUTABLY and replaced
# the script in place three times in Aug 2026: twice between our review and the build (the sha pin
# we used then failed those builds closed, correctly), and once more after a cache eviction turned
# the stale pin into every build failing until someone re-verified. Vendoring ends the recurrence:
# the bytes that run are the bytes reviewed in git, and a build needs no network to stay honest.
#
# To upgrade Hermes, fetch the script fresh, review the diff, and commit it:
#   curl -fsSL -A "OpenAI File Downloader, XaiImageApiFetch/1.0" \
#     -o adapters/hermes/hermes-install.sh https://hermes-agent.nousresearch.com/install.sh
# (the -A string matters — the host serves different bytes to unknown user agents), or run
# tools/ship.sh --refresh-hermes, which does exactly that. The COPY below busts the Docker cache
# precisely when the file changes: a routine code push never re-runs the installer, a committed
# upgrade always does. The Hermes version an image actually carries is recorded at build time in
# /app/hermes-image-version.txt (see below).
# (Vendored 2026-08-31, sha256 2076946edc23b3aed4a82ccb2e6b38ab593575626206dbdd192384e375b6d57c.)
COPY adapters/hermes/hermes-install.sh /tmp/hermes-install.sh
RUN bash /tmp/hermes-install.sh \
 && rm -f /tmp/hermes-install.sh
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

# Python runtime deps, EXACTLY pinned in requirements.txt (pyyaml for skills front-matter; the Google
# Workspace client libs for the bundled google-workspace skill's google_api.py). WITHOUT the Google
# libs, `google_api.py gmail/calendar` dies with `ModuleNotFoundError: No module named 'googleapiclient'`
# even though the OAuth token is valid (setup.py --check only validates the token, not the client lib) —
# so briefs silently fall to local-only and the agent improvises `pip install` mid-run. Baked into the
# image → always present, no per-run install, no improvisation.
#
# Installed with `python3 -m pip` against the PATH python (the same interpreter execute_code/gather_google
# use — the Hermes install above may have put a different one first), falling back to plain `pip`.
# Deliberately AFTER the Hermes layer: editing a pin then costs one pip layer, not a re-download and
# re-run of the installer.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements.txt \
 || pip install --no-cache-dir -r /tmp/requirements.txt

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
