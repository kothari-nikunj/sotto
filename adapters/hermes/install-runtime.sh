#!/usr/bin/env bash
# Install the reviewed runtime without replacing an unrelated Hermes installation.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PIN="$(tr -d '\r\n' < "$HERE/hermes.commit")"
DEST="${HERMES_INSTALL_DIR:-${HERMES_HOME:-$HOME/.hermes}/hermes-agent}"
LAUNCHER="$(command -v hermes || true)"
# The upstream installer writes this launcher even when ~/.local/bin is not on PATH.
SAVED_LAUNCHER="$HOME/.local/bin/hermes"
if [ -z "$LAUNCHER" ] && { [ -e "$SAVED_LAUNCHER" ] || [ -L "$SAVED_LAUNCHER" ]; }; then
  LAUNCHER="$SAVED_LAUNCHER"
fi

if [ -e "$DEST" ] || [ -n "$LAUNCHER" ]; then
  # A launcher elsewhere might belong to another installation. Never repoint it,
  # checkout a different commit, or run the upstream updater on the user's behalf.
  if [ "$(git -C "$DEST" rev-parse HEAD 2>/dev/null || true)" != "$PIN" ]; then
    echo "Existing Hermes is not Sotto's reviewed runtime. Nothing was changed." >&2
    echo "Use the container setup, or back up and deliberately replace your existing Hermes first." >&2
    exit 1
  fi
  if [ -n "$LAUNCHER" ] && ! grep -Fq -- "$DEST/hermes" "$LAUNCHER"; then
    echo "Cannot verify that your Hermes launcher uses the reviewed source. Nothing was changed." >&2
    echo "Use the container setup or inspect your existing launcher before configuring Sotto." >&2
    exit 1
  fi
  python3 "$HERE/provider_error_compat.py" --check "$DEST/gateway/run.py"
  echo "Reviewed Hermes source already exists at $DEST. No runtime update performed."
else
  bash "$HERE/hermes-install.sh" --dir "$DEST" --commit "$PIN" --force-commit --skip-setup
  if [ "$(git -C "$DEST" rev-parse HEAD)" != "$PIN" ]; then
    echo "Hermes installation did not reach Sotto's reviewed commit. Stop before configuring Sotto." >&2
    exit 1
  fi
  python3 "$HERE/provider_error_compat.py" --check "$DEST/gateway/run.py"
fi

echo 'Next: activate the Sotto Python environment, put ~/.local/bin on PATH, then run adapters/hermes/install.sh.'
