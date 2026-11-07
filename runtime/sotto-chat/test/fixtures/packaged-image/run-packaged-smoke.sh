#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 IMAGE PLATFORM" >&2
    exit 2
fi
image=$1
platform=$2
case "$platform" in
    linux/arm64|linux/amd64) ;;
    *) echo "unsupported platform" >&2; exit 2 ;;
esac

fixture_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_root=$(CDPATH= cd -- "$fixture_dir/../../../../.." && pwd)
receiver_source="$source_root/runtime/trigger-receiver/receiver.py"
if command -v sha256sum >/dev/null 2>&1; then
    expected_receiver_sha256=$(sha256sum "$receiver_source" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    expected_receiver_sha256=$(shasum -a 256 "$receiver_source" | awk '{print $1}')
else
    echo "sha256sum or shasum is required" >&2
    exit 2
fi
case "$expected_receiver_sha256" in
    *[!0-9a-f]*|'') echo "could not compute receiver source SHA-256" >&2; exit 2 ;;
esac

docker run --rm \
    --pull=never \
    --network none \
    --platform "$platform" \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,size=256m,uid=10000,gid=10000,mode=1777 \
    --user 10000:10000 \
    --cap-drop=ALL \
    --security-opt no-new-privileges \
    --mount "type=bind,source=$fixture_dir,target=/tmp/sotto-fixture,readonly" \
    --env PATH=/usr/local/bin:/usr/bin:/bin \
    --env HOME=/tmp/sotto-home \
    --env SOTTO_DEPLOYMENT_MODE=managed \
    --env SOTTO_DATA=/tmp/sotto-receiver-package-smoke \
    --env SOTTO_SKILLS_ROOT=/app/sotto-skills \
    --env SOTTO_PROACTIVE=1 \
    --env 'SOTTO_PROACTIVE_CRON=*/15 * * * *' \
    --env SOTTO_DIGEST=1 \
    --env SOTTO_FIXTURE_ROOT=/tmp/sotto-fixture \
    --env "SOTTO_EXPECTED_RECEIVER_SHA256=$expected_receiver_sha256" \
    --env PYTHON=python3 \
    --entrypoint sh \
    "$image" -ceu '\
        python3 -I -B /tmp/sotto-fixture/hermes-retirement-check.py; \
        python3 -I -B /tmp/sotto-fixture/package-runtime-smoke.py; \
        node /tmp/sotto-fixture/activation-image-contract.mjs'
