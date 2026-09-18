#!/usr/bin/env bash
# Regenerate the Python dependency locks (requirements.txt, requirements-dev.txt) from the .in manifests.
#
# Reproducibility here comes from the *existing* .txt files, not from the manifests alone: `uv pip
# compile` treats an already-present output file as a preference source and keeps every version it
# can still satisfy, so re-running this script with the committed locks in place regenerates them
# byte-for-byte. Delete a .txt first (or otherwise run this with no committed lock to prefer) and
# that guarantee is gone -- uv freely re-resolves every unpinned transitive to whatever is newest
# and compatible today, silently landing a different lock (a `bash tools/lock-requirements.sh` run
# during this file's own review picked up urllib3 2.8.0 in place of the reviewed 2.7.0 this way).
#
# So: to bump one or more direct versions, edit the .in file(s) and run this script normally -- the
# existing locks keep everything else pinned exactly where it was. To deliberately let every
# unpinned transitive move to its newest compatible version (a dependency-upgrade pass, not a
# version bump), pass --upgrade; plain `rm requirements*.txt && bash tools/lock-requirements.sh` is
# not a supported way to do that, and this script refuses to run against a missing lock without it.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v uv >/dev/null || { echo 'Install uv to regenerate Python dependency locks.' >&2; exit 1; }

upgrade=
if [ "${1:-}" = '--upgrade' ]; then
  upgrade=--upgrade
elif [ "${1:-}" != '' ]; then
  echo "verify: unknown argument '$1' (the only flag is --upgrade)" >&2
  exit 1
fi
if [ -z "$upgrade" ]; then
  for lock in requirements.txt requirements-dev.txt; do
    [ -f "$lock" ] || {
      echo "verify: $lock is missing, so there is no preference source to reproduce it from." >&2
      echo 'Restore it from git for a deliberate, minimal-diff regeneration, or re-run with' >&2
      echo '--upgrade to intentionally let every unpinned transitive move to latest.' >&2
      exit 1
    }
  done
fi

uv pip compile --python-version 3.12 --universal --generate-hashes --no-build --no-annotate \
  $upgrade --custom-compile-command 'bash tools/lock-requirements.sh' \
  requirements.in -o requirements.txt
uv pip compile --python-version 3.12 --universal --generate-hashes --no-build --no-annotate \
  $upgrade --custom-compile-command 'bash tools/lock-requirements.sh' \
  -c requirements.txt requirements-dev.in -o requirements-dev.txt
