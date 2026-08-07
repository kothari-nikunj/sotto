"""The public-repo guards, enforced at TEST time — a guard that only fires during ship.sh
lands in the OWNER's terminal mid-release (it has, twice). Same regexes as
tools/prepare-public-repo.sh; distributable paths only."""
import os
import re
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Assembled from parts so this file's own source never matches the pattern it enforces.
_PARTS = ["AI" + "za", r"\bsk" + "-", "gh" + "p_", "PRIVATE" + " KEY", "AK" + "IA",
          "@gm" + "ail", "@fpv" + "ventures"]
SECRET_RE = re.compile("|".join(_PARTS))


def test_no_secret_patterns_in_distributable_tree():
    """Mirrors prepare-public-repo.sh's SECRETS GUARD. Use a fake freemail like
    yahoo.com in fixtures — the guard greps the whole shipped tree, tests included."""
    hits = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".pytest_cache")]
        for fn in filenames:
            if not fn.endswith((".py", ".md", ".json", ".yaml", ".yml", ".sh")):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if SECRET_RE.search(line):
                            hits.append(f"{os.path.relpath(path, ROOT)}:{i}: {line.strip()[:80]}")
            except (OSError, UnicodeDecodeError):
                continue
    assert not hits, (
        "secrets-guard patterns in the distributable tree (these WILL fail ship.sh):\n  "
        + "\n  ".join(hits)
    )
