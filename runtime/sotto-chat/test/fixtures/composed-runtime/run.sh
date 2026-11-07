#!/usr/bin/env bash
set -euo pipefail

fixture_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
repo_root="$(cd -- "$fixture_dir/../../../../.." && pwd -P)"
platform="${PLATFORM:-linux/arm64}"
base_image="${BASE_IMAGE:-sotto-chat-runtime:composed-base}"
fixture_image="${FIXTURE_IMAGE:-sotto-chat-runtime:composed-test}"
runtime_dockerfile="$repo_root/runtime/sotto-chat/Dockerfile.runtime"
fixture_dockerfile="$fixture_dir/Dockerfile"

docker build --platform "$platform" --pull=false \
  --file "$runtime_dockerfile" --tag "$base_image" "$repo_root"
docker build --platform "$platform" --pull=false \
  --build-arg "BASE_IMAGE=$base_image" \
  --file "$fixture_dockerfile" --tag "$fixture_image" "$repo_root"
# Static refusal and low-level inherited-lock cleanup are separate from the actual-receiver negative smoke.
docker run --rm --init --platform "$platform" --network none --read-only \
  --user 0:0 --cap-drop ALL --cap-add SETUID --cap-add SETGID \
  --cap-add CHOWN --cap-add KILL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --env SUPERVISOR_SOURCE_ROOT=/app \
  --entrypoint /usr/local/bin/python3 "$fixture_image" \
  /app/runtime/sotto-chat/test/fixtures/composed-runtime/supervisor_preflight_smoke.py
docker run --rm --platform "$platform" --network none --read-only \
  --user 0:0 --cap-drop ALL --cap-add SETUID --cap-add SETGID \
  --cap-add CHOWN --cap-add KILL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
  --tmpfs /data:rw,noexec,nosuid,nodev,size=32m,mode=0755 \
  --entrypoint /usr/local/bin/python3 "$fixture_image" \
  /app/runtime/sotto-chat/test/fixtures/composed-runtime/composed_failure_smoke.py
