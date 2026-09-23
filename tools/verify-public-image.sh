#!/usr/bin/env bash
# Build the exact public distribution tree before it is published or accepted by CI.
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
echo "[verify-public-image] building exact distribution context: $CONTEXT"
docker buildx build --load --progress=plain --tag "sotto-public-verify:${SAFE_VERSION:-dev}" "$CONTEXT"
echo "[verify-public-image] image build passed"
