#!/usr/bin/env bash
# Build the exact public distribution tree's self-host image (the root Dockerfile) before it is
# published or accepted by CI. That image's RUN steps typecheck and build the Node chat runtime, load
# every packaged skill, validate an offline runtime config and import the receiver, adapters and
# Sotto runner fence, so a green build proves the packaged runtime, not only the Dockerfile syntax.
set -euo pipefail

[ $# -eq 1 ] || { echo "Usage: $0 <distribution-tree>|--check" >&2; exit 2; }
CONTEXT="$1"
command -v docker >/dev/null 2>&1 \
  || { echo "ERROR: Docker is required to verify the public image before publishing" >&2; exit 1; }
docker info >/dev/null 2>&1 \
  || { echo "ERROR: the Docker daemon is unavailable; start Docker before publishing" >&2; exit 1; }
docker buildx version >/dev/null 2>&1 \
  || { echo "ERROR: Docker Buildx is required. Docker Desktop includes it; with Homebrew, run brew install docker-buildx and follow its plugin setup instructions." >&2; exit 1; }
[ "$CONTEXT" = "--check" ] && exit 0

[ -d "$CONTEXT" ] || { echo "ERROR: distribution tree '$CONTEXT' does not exist" >&2; exit 1; }
[ -f "$CONTEXT/Dockerfile" ] || { echo "ERROR: distribution tree '$CONTEXT' has no Dockerfile" >&2; exit 1; }

VERSION="$(tr -d '[:space:]' < "$CONTEXT/VERSION" 2>/dev/null || echo dev)"
SAFE_VERSION="$(printf '%s' "${VERSION:-dev}" | tr -c 'A-Za-z0-9_.-' '-')"
echo "[verify-public-image] building the self-host image from exact distribution context: $CONTEXT"
# Check the Dockerfile build, including its RUN checks, without exporting or running the final image.
# Avoid keeping a second copy of the resulting layers in Docker's image store.
docker buildx build --output type=cacheonly --progress=plain --file "$CONTEXT/Dockerfile" \
  --tag "sotto-public-verify:${SAFE_VERSION:-dev}" "$CONTEXT"
echo "[verify-public-image] image build passed"
