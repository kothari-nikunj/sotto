#!/bin/sh
# Synthetic processes only: never share the host PID namespace or tenant volume.
set -eu
: "${LIFECYCLE_IMAGE:?Set LIFECYCLE_IMAGE to an already-built local runtime image}"
fixture_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
source_root=$(CDPATH= cd -- "$fixture_dir/../../.." && pwd)
case "${LIFECYCLE_CONTRACT:-start}" in
  start) contract=test_tenant_lifecycle.py ;;
  replacement) contract=test_native_replacement.py ;;
  *) echo 'LIFECYCLE_CONTRACT must be start or replacement' >&2; exit 2 ;;
esac
exec docker run --rm --pull=never --network none --read-only --user 0:0 \
  --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add KILL \
  --cap-add CHOWN --cap-add DAC_OVERRIDE \
  --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777 \
  --mount "type=bind,source=$source_root,target=/snapshot,readonly" \
  --env PYTHONPATH=/snapshot/adapters/sotto:/snapshot/runtime/trigger-receiver \
  --env PYTHONDONTWRITEBYTECODE=1 --entrypoint /usr/bin/tini \
  "$LIFECYCLE_IMAGE" -- python3 "/snapshot/adapters/sotto/$contract"
